# CUDAMicroBench 是什么：14 个微基准的项目地图

## 1. 本讲目标

读完本讲，你应该能够：

1. 用两三句话向别人解释 CUDAMicroBench 是什么、从哪来（配套论文）、以什么许可发布。
2. 说出 README 中总结的三大类 CUDA 性能挑战，并把 14 个基准逐一归入对应类别。
3. 拿到仓库里任意一个基准目录，能立刻认出它的代码骨架（host 主程序、kernel 文件、Makefile、test.sh、自带的结果文件）。
4. 发现"文档说的"与"仓库里实际有的"之间的几处偏差（目录数、test.sh 缺失、Common 的实际使用者），学会用工具核实而不是只信 README。

本讲是整个学习手册的起点，**不需要任何 GPU，也不需要编译任何代码**——全部实践都是"读地图"式的源码阅读实践。

## 2. 前置知识

本讲假设你几乎没有写过 CUDA 程序，但需要以下最基础的概念：

- **CPU 与 GPU 的分工**：CPU 擅长复杂的串行逻辑，GPU 擅长用成百上千个小核心同时做大量相似计算。把计算搬到 GPU 上（异构编程）是 CUDA 的核心思想。
- **kernel（核函数）**：在 GPU 上运行的函数。CPU 端的程序负责准备数据、启动 kernel、取回结果。
- **host 与 device**：CUDA 术语中，host 指 CPU 及其内存，device 指 GPU 及其显存。两者内存独立，数据要显式搬运。
- **微基准（microbenchmark）**：不追求模拟真实应用，而是用一个极小的、可控的程序把**某一个**性能因素（比如分支发散、内存对齐）孤立出来测量。理解了微基准，就理解了该因素在真实程序里如何起作用。
- **反模式（anti-pattern）**：一种常见的、会带来性能损失的写法。本仓库的每个基准都是"一对"程序：先展示反模式有多慢，再展示优化写法有多快。

不需要预先了解 warp、共享内存、统一内存等术语——它们会在后续讲义中逐一展开，本讲只需要你在地图上"认出这些名字"。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| [README.md](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md) | 项目唯一的全局文档：定位说明、14 个基准的性能模式汇总表、环境要求、Common 目录说明、论文与许可信息。本讲的主线就是精读它 |
| [LICENSE_BSD.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/LICENSE_BSD.txt) | 3 条款 BSD 许可证全文，含版权方与资助信息 |
| [NOTICE](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/NOTICE) | 美国能源部 / 劳伦斯利弗莫尔国家实验室（LLNL）的官方声明 |
| [Common/README.md](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Common/README.md) | 说明 Common 目录来自 CUDA Samples，服务于哪几个基准 |
| 仓库根目录下的 16 个基准目录 | 本讲的"测绘对象"：WarpDivRedux、DynParallel、Conkernels、TaskGraph、Shmem、CoMem_AXPY、CoMem_SpMM、MemAlign、GSOverlap、Shuffle、BankRedux、HDOverlap、ReadOnlyMem_1D_Texture、ReadOnlyMem_2D_Texture、UniMem、MiniTransfer_SpMV |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- 4.1 项目定位、配套论文与许可
- 4.2 README 性能模式表格：三大性能挑战
- 4.3 基准名到目录的映射：14 个基准名、16 个代码目录
- 4.4 Common 目录与 CUDA Samples 血统

### 4.1 项目定位、配套论文与许可

#### 4.1.1 概念说明

在学习任何仓库之前，先回答三个问题：**它是什么、从哪来、我能怎么用**。

CUDAMicroBench 是一套**教学用途的 CUDA 微基准集合**：十四个小程序，每个都演示一类"让 GPU 跑不快"的编程陷阱（反模式），并配一个优化后的版本。它不是一个框架、不是一个库——没有统一的 `main` 入口，你**不能在根目录 `make`**，而是进入单个基准目录单独编译运行。

它来自一篇论文（IPDPSW 2021），由 UNC Charlotte 的 HPCAS Lab 与 LLNL 合作完成，以 3 条款 BSD 许可开源。这意味着你可以自由地学习、修改、再分发这些代码（保留版权声明即可），把它作为自己实验的起点。

#### 4.1.2 核心流程

使用本仓库学习的一般流程：

```text
打开 README → 在汇总表中找到感兴趣的"性能问题"
    → 进入对应的基准目录 → 读该目录的 Makefile 了解编译方式
    → 用 test.sh（如果有的话）运行并采集性能指标
    → 对比"反模式版本"与"优化版本"的输出
```

#### 4.1.3 源码精读

README 开头说明了项目要解决的困难——GPU 核心极多、必须提供足够并行度，且异构系统的内存层次又深又复杂，这两点是全部十四个基准的共同背景：

- [README.md:L4-L8](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L4-L8) —— 说明 GPU 高性能编程的两大难点（并行度、深存储层次），并给出项目定义："a collection of fourteen microbenchmarks that demonstrate performance challenges in CUDA programming and techniques to optimize the CUDA programs"，同时提到它包含 shuffle、动态并行等高级 CUDA 特性的示例。
- [README.md:L10](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L10) —— 列出四个用途：评估 GPU 体系结构与内存系统、评估编译器和性能工具、帮助理解异构系统、指导性能优化。这解释了为什么仓库里会附带在多台集群上采集的 `.output.txt` 结果文件。

配套论文信息（想深入动机时去检索原文）：

- [README.md:L117-L119](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L117-L119) —— Yi, Stokes, Yan, Liao, "CUDAMicroBench: Microbenchmarks to Assist CUDA Performance Programming", IPDPSW 2021, pp. 397-406（编号 LLNL-CONF-819919）。

许可与出处：

- [LICENSE_BSD.txt:L1-L7](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/LICENSE_BSD.txt#L1-L7) —— 版权归 HPCAS Lab（UNC Charlotte）与 LLNL 共同所有，代码编号 LLNL-CODE-825202。
- [LICENSE_BSD.txt:L14-L23](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/LICENSE_BSD.txt#L14-L23) —— 标准 3 条款 BSD 的三条义务（保留声明、二进制分发重现声明、不得用版权方名义背书）。
- [NOTICE:L1-L3](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/NOTICE#L1-L3) —— LLNL 在美国能源部合同 DE-AC52-07NA27344 下产出的声明。

#### 4.1.4 代码实践

1. **实践目标**：确认你对项目"是什么、从哪来、怎么用"三问有自己的答案。
2. **操作步骤**：
   - 通读 [README.md:L100-L111](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L100-L111) 的 Features、Prerequisite、Experiment 三节。
   - 用浏览器或 `git log --oneline` 看仓库的历史（共 9 次提交，从 "Create NOTICE" 到 "Corrected errors and updated test results for warp divergence"），感受这是一个伴随论文发布、之后小修的项目。
3. **需要观察的现象**：Prerequisite 一节明确要求 NVIDIA GPU 与 CUDA，且"动态并行、memcpy_async 等新特性需要支持 CUDA 11 的 GPU"——这预告了 DynParallel 与 GSOverlap 两个基准的硬件门槛。
4. **预期结果**：你能不看资料说出：十四个微基准、IPDPSW 2021 论文配套代码、BSD 三条款许可、根目录没有统一入口。

#### 4.1.5 小练习与答案

**练习 1**：这个项目能用来做什么？列出 README 提到的至少两种用途。

**参考答案**：评估 GPU 体系结构与内存系统性能；评估编译器和性能分析工具的有效性；通过示例帮助理解异构 GPU 系统的复杂性；指导用户做性能优化（见 [README.md:L10](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L10)）。

**练习 2**：我可以在公司项目里复用这里的代码吗？有什么义务？

**参考答案**：可以。3 条款 BSD 允许自由使用、修改和再分发（包括商用），义务是：源码分发时保留版权与免责声明，二进制分发时在文档中重现它们，且不得用版权方名义为衍生产品背书（[LICENSE_BSD.txt:L14-L23](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/LICENSE_BSD.txt#L14-L23)）。

### 4.2 README 性能模式表格：三大性能挑战

#### 4.2.1 概念说明

README 的核心是一张三列表格：**基准名 / 性能低效的模式（反模式）/ 优化技术**。表格被三个分组标题切成三段，每段对应 GPU 异构编程的一大类挑战。这张表就是整本学习手册的"课程大纲"：

1. **充分利用 GPU 的大规模并行能力**——GPU 有成百上千核心，如果程序不能持续"喂饱"它们（分支发散、工作负载无法预划分、kernel 之间串行执行、提交开销太大），算力就被浪费。
2. **有效利用 GPU 内部的深存储层次**——数据放在全局内存、共享内存、寄存器、纹理/常量缓存中的哪一层，访问是否连续、是否对齐、是否冲突，直接决定吞吐。
3. **合理安排 CPU 与 GPU 之间的数据搬运**——PCIe 带宽远低于显存带宽，传了多少无用数据、传输是否与其他工作重叠，往往比 kernel 本身更影响总时间。

#### 4.2.2 核心流程

把表格当作"查字典"的流程：

```text
遇到一个性能名词（如 bank 冲突）
  → 在表格第二列找到含该关键词的行
  → 读同一行第三列的优化技术
  → 进入第一行基准名对应的目录看源码
```

三大类的基准数量分布是 **4 + 6 + 4 = 14**。

#### 4.2.3 源码精读

表格的表头定义了三列语义：

- [README.md:L12-L18](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L12-L18) —— "Summary of the CUDAMicroBench microbenchmarks" 表格与三列表头：Benchmark name、Pattern of Performance Inefficiency、Optimization techniques。

三个分组标题（各自占一整行、横跨三列）：

- [README.md:L20](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L20) —— 第一组：Optimizing Kernels to Saturate the Massive Parallel Capability of GPUs。
- [README.md:L43](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L43) —— 第二组：Effectively Leveraging the Deep Memory Hierarchy Inside GPU。
- [README.md:L76](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L76) —— 第三组：Properly Arranging Data Movement Between CPU and GPU。

每组下代表性条目（其余各行结构完全相同）：

- [README.md:L23-L26](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L23-L26) —— WarpDivRedux：反模式是"线程遇到控制流语句时进入不同分支"，优化是"改变算法，以 warp 大小为步长"。
- [README.md:L51-L54](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L51-L54) —— CoMem：反模式是"线程间跨步或随机访问数组造成非合并访存"，优化是"让线程连续访问内存"。
- [README.md:L89-L92](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L89-L92) —— UniMem：反模式是"内存访问密度低"，优化是"把数据放进统一内存、只拷贝必要的页面"。

一个值得注意的细节：README 原文里有不少拼写笔误（如 "serveral"、"uncoaleasced"、"Mallocation…unaliged adress"、"acclerate"），阅读时按上下文理解即可：several、uncoalesced、unaligned address、accelerate。

#### 4.2.4 代码实践

1. **实践目标**：不看资料，从表格中随机指一个基准名，说出它的类别、反模式与优化手段。
2. **操作步骤**：
   - 打开 [README.md:L19-L98](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L19-L98)，数一数每个分组下各有多少个基准（不含分组标题行）。
   - 为三个分组各挑一个你最好奇的条目，抄下它的三列内容。
3. **需要观察的现象**：第二列描述的都是"症状"（什么行为导致慢），第三列都是"处方"（换什么写法）。几乎所有处方都落在两类：要么换数据组织方式，要么换 CUDA 提供的机制（stream、graph、texture、unified memory、shuffle……）。
4. **预期结果**：第一组 4 个（WarpDivRedux、DynParallel、Conkernels、TaskGraph），第二组 6 个（Shmem、CoMem、MemAlign、GSOverlap、Shuffle、BankRedux），第三组 4 个（HDOverlap、ReadOnlyMem、UniMem、MiniTransfer）。

#### 4.2.5 小练习与答案

**练习 1**：Shuffle 基准要解决的反模式是什么？优化技术是什么？

**参考答案**：反模式是"线程之间的数据交换"（传统做法要经过共享内存中转）；优化是"用 shuffle 指令让同一 warp 内的线程直接在寄存器之间共享部分结果"（[README.md:L66-L69](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L66-L69)）。

**练习 2**：HDOverlap 和 GSOverlap 的名字里都有 "Overlap"，它们重叠的东西有什么不同？

**参考答案**：HDOverlap 重叠的是**主机与设备之间**的传输与其他工作（用 `cudaMemcpyAsync`，属于第三类 CPU-GPU 搬运问题，[README.md:L79-L82](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L79-L82)）；GSOverlap 重叠的是**全局内存到共享内存的拷贝**与 kernel 计算（用 CUDA 11 的 memcpy_async，属于第二类 GPU 内部存储层次问题，[README.md:L61-L64](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L61-L64)）。

**练习 3**：三大类挑战中，哪一类与 PCIe 带宽的关系最密切？

**参考答案**：第三类（Properly Arranging Data Movement Between CPU and GPU）。CPU 与 GPU 之间通过 PCIe（或 NVLink）互连，带宽远低于 GPU 显存，所以减少无用传输、异步化传输是关键；前两类都发生在 GPU 内部。

### 4.3 基准名到目录的映射：14 个基准名、16 个代码目录

#### 4.3.1 概念说明

README 的表格里有 **14 个基准名**，但仓库根目录下有 **16 个基准代码目录**（外加 Common、case_study_shuffle 和本教程目录）。对不上号的原因是：两个基准各拆成了两个实例目录——

- **CoMem** 有两个实例：`CoMem_AXPY`（向量运算，入门友好）与 `CoMem_SpMM`（稀疏矩阵乘，更接近真实负载）；
- **ReadOnlyMem** 有两个实例：`ReadOnlyMem_1D_Texture`（一维纹理）与 `ReadOnlyMem_2D_Texture`（二维纹理 + 常量内存）；
- **Shuffle** 是一个目录，但内部又分 `cuda_global` 与 `cuda_shuffle` 两个可独立编译的对照子项目。

另外要认识每个目录的"骨架"。绝大多数基准遵循同一套三件套 + 配方：

```text
<名字>.c        host 主程序：初始化数据、串行基线、计时、结果校验
<名字>kernel.cu device 代码：cudaMalloc/cudaMemcpy/kernel 启动/释放
<名字>.h        两者的接口声明
Makefile        用 nvcc 编译
test.sh         以不同数据规模运行，并用 nvprof 采集指标
*.output.<集群名>.txt   作者在 Carina / Fornax 集群上跑出的历史结果
```

但也有不少目录**并不**符合这套骨架（单文件一体、缺 test.sh 等），这正是"地图要与实地核对"的意义。

#### 4.3.2 核心流程

核对本节地图的流程：

```text
ls 仓库根目录 → 数出基准目录
  → 对每个目录 ls 一次，记录骨架文件类型
  → 与 README 表格的 14 个基准名配对
  → 标注：有无 test.sh？有无历史结果文件？是否单文件一体？
```

#### 4.3.3 源码精读

README 的 Experiment 一节给出了官方的运行指引——"查看每个目录的 Makefile 了解编译方式，再用 .sh 文件执行"：

- [README.md:L110-L111](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L110-L111) —— 官方使用方式：每个目录独立编译、用 shell 脚本运行。

把 16 个目录的实际内容与 README 配对，得到下面这张总表（依据是对每个目录执行 `ls` 的结果）：

| README 基准名（类别） | 仓库目录 | 骨架形态 | 有无 test.sh | 自带结果文件 |
|---|---|---|---|---|
| WarpDivRedux（一） | `WarpDivRedux/` | .c + .cu + .h 三件套 | 有 | warpDivergenceTest_cuda.output.carina.txt |
| DynParallel（一） | `DynParallel/` | 两个对照单文件 .cu（非动态 / 动态并行） | **无** | 无 |
| Conkernels（一） | `Conkernels/` | 单文件 .cu + 两个 Makefile | **无** | 无 |
| TaskGraph（一） | `TaskGraph/` | 单文件 .cu | **无** | 无 |
| Shmem（二） | `Shmem/` | .c + .cu + .h（OpenMP 对照） | **无**（有 testResults.txt） | testResults.txt |
| CoMem（二） | `CoMem_AXPY/` | .c + .cu + .h 三件套 | 有 | axpy_cuda.output.carina.txt |
| CoMem（二） | `CoMem_SpMM/` | .c + .cu + .h 三件套 | **无** | carina 与 fornax 两个输出 |
| MemAlign（二） | `MemAlign/` | .c + .cu + .h 三件套 | 有 | axpy_cuda.output.carina.txt |
| GSOverlap（二） | `GSOverlap/` | 单文件 .cu | **无** | 无 |
| Shuffle（二） | `Shuffle/cuda_global/` 与 `Shuffle/cuda_shuffle/` | .cpp + .cu + .h，两套对照 | 有 | 各自 result.txt |
| BankRedux（二） | `BankRedux/` | .c + .cu + .h 三件套 | 有 | sum_cuda.output.carina.txt |
| HDOverlap（三） | `HDOverlap/` | 仅一个 .cu（host 与 kernel 合一） | 有 | results.txt |
| ReadOnlyMem（三） | `ReadOnlyMem_1D_Texture/` | .c + .cu + .h 三件套 | 有 | carina 与 fornax 两个输出 |
| ReadOnlyMem（三） | `ReadOnlyMem_2D_Texture/` | .c + .cu + .h 三件套 | 有 | carina 与 fornax 两个输出 |
| UniMem（三） | `UniMem/` | .cu（CUDA 版）+ .c（OpenMP 对照）+ .h | 有（test.sh 与 test2.sh） | carina 两个输出 |
| MiniTransfer（三） | `MiniTransfer_SpMV/` | .c + .cu + .h 三件套 | 有 | SpMV_cuda.output.carina.txt |

由此可以直接读出三个"README 没有明说"的事实：

1. 16 个目录 = 14 个基准名 + CoMem 与 ReadOnlyMem 各多出的一个实例目录。
2. **6 个目录没有 test.sh**（DynParallel、Shmem、Conkernels、GSOverlap、TaskGraph、CoMem_SpMM），与 [README.md:L110-L111](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L110-L111) "用 .sh 执行"的说法并不完全一致——这 6 个要直接读 Makefile 手动运行。
3. 没有历史结果文件的目录恰好多是 CUDA Samples 出身或对照型程序（详见 4.4）。

还有一个容易困惑的空目录：根目录下的 `case_study_shuffle/` 是空的。用 `git ls-files -s case_study_shuffle` 可以看到它的 git mode 是 `160000`（gitlink，指向外部仓库的某个提交），且仓库没有配套的 `.gitmodules`，所以克隆下来只是一个空占位目录；Shuffle 案例真正可编译的代码在 `Shuffle/` 的两个子目录里。

#### 4.3.4 代码实践

1. **实践目标**：亲手完成"基准名 → 目录 → 类别 → 反模式/优化 → 骨架"的完整测绘，产出一张属于你自己的地图表。
2. **操作步骤**：
   1. 在仓库根目录执行 `ls -d */`，列出全部目录。
   2. 逐个进入并执行 `ls`，确认上表第三列的骨架形态（例如 `ls CoMem_AXPY` 应看到 axpy.h、axpy_cuda.c、axpy_cudakernel.cu、Makefile、test.sh）。
   3. 执行 `ls */test.sh */*/test.sh 2>/dev/null`，让 shell 自己告诉你哪些目录有测试脚本。
   4. 对照 [README.md:L19-L98](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L19-L98) 的表格，为每个目录写一行：`目录名 | 所属大类 | 反模式 | 优化技术 | 是否来自 CUDA Samples`。
3. **需要观察的现象**：目录总数与基准名数量不一致；同样的 "axpy" 骨架出现在 CoMem_AXPY、MemAlign、HDOverlap、ReadOnlyMem_1D_Texture 四个目录中（它们是同一个教学载体在不同内存通路上的变体）。
4. **预期结果**：得到一张 16 行的表，其中 12 行能从 README 表格直接抄到反模式与优化技术；`CoMem_AXPY`、`CoMem_SpMM` 两行共用 CoMem 的条目，`ReadOnlyMem_1D_Texture`、`ReadOnlyMem_2D_Texture` 两行共用 ReadOnlyMem 的条目；GSOverlap、Conkernels、TaskGraph 三行标注"来自 CUDA Samples"（依据见 4.4）。
5. 本实践不涉及编译运行，无需 GPU；若要进一步验证 `git ls-files -s case_study_shuffle` 的输出为 `160000`，在本地仓库执行即可（本讲义编写时已验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 README 说 14 个微基准，目录却有 16 个？

**参考答案**：CoMem 拆成 CoMem_AXPY 与 CoMem_SpMM 两个实例（同一合并访存主题、两种负载），ReadOnlyMem 拆成 1D_Texture 与 2D_Texture 两个实例（两种只读内存通路），因此 14 个基准名对应 16 个代码目录。

**练习 2**：如果我想运行 Conkernels，按 README 说的"用 .sh 执行"可行吗？

**参考答案**：不可行。Conkernels 目录下没有 test.sh，只有 Makefile 与 Makefile_serialized 两个编译脚本（另有一体化的 concurrentKernels.cu），需要读 Makefile 自行编译运行。仓库中没有 test.sh 的目录共 6 个：DynParallel、Shmem、Conkernels、GSOverlap、TaskGraph、CoMem_SpMM。

**练习 3**：哪四个目录复用了同一套 AXPY 代码骨架？这说明作者采用了什么编排手法？

**参考答案**：CoMem_AXPY、MemAlign、HDOverlap、ReadOnlyMem_1D_Texture。手法是"固定一个极简单的计算载体（y = a·x + y），每次只改一个内存相关的变量"（对齐、传输方式、只读通路），从而把单一性能因素孤立出来——这正是微基准方法论的核心。

### 4.4 Common 目录与 CUDA Samples 血统

#### 4.4.1 概念说明

NVIDIA 官方的 CUDA Samples 是一套示例程序库，里面附带一批 helper 头文件（`helper_cuda.h`、`helper_timer.h`、`helper_functions.h` 等），提供错误检查、计时、参数解析等便利函数。本仓库有三个基准直接改写自 CUDA Samples，它们 include 这些头文件；为了能独立编译，作者把所需的头文件连同部分依赖（FreeImage、UtilNPP）整体复制进了 `Common/` 目录。

识别"Samples 出身"的三个肉眼特征：目录里有 Samples 风格的 README（Description / Key Concepts / Supported SM Architectures 分节）、Visual Studio 工程文件（`*_vs2015/2017/2019.sln/.vcxproj`）、NsightEclipse.xml。

#### 4.4.2 核心流程

确认某目录依赖 Common 的方法：

```text
在该目录的 Makefile 中搜索 "Common"
  → 找到形如 INCLUDES := -I../Common 的行
  → 再在源码中搜索 #include "helper_..."
  → 即可断定它依赖 Common 里的 helper 头文件
```

#### 4.4.3 源码精读

README 的 Common 一节是官方说明：

- [README.md:L113-L115](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L113-L115) —— 明确列出 GSOverlap、ConKernels、Taskgraph 三个基准派生自 CUDA Samples，所需头文件存于 Common（同样来自 Samples）。
- [Common/README.md:L1](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Common/README.md#L1) —— Common 目录的自述，与上句一致。

Samples 出身的直接证据（Conkernels 保留了 Samples 的原始 README）：

- [Conkernels/README.md:L1-L13](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/README.md#L1-L13) —— 典型 CUDA Samples 格式："This sample demonstrates the use of CUDA streams for concurrent execution of several kernels on GPU device"，并列出 SM 3.5 到 SM 8.6 的支持架构。

Makefile 中对 Common 的实际引用（本讲义编写时用 grep 核实）：

- [GSOverlap/Makefile:L279-L280](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/Makefile#L279-L280) —— `INCLUDES := -I../Common`，指向仓库根的 Common，路径正确。
- [TaskGraph/Makefile:L273-L274](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/Makefile#L273-L274) —— 同样是 `-I../Common`，路径正确。
- [Conkernels/Makefile:L273-L274](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L273-L274) —— 写的是 `-I../../Common`：从 `Conkernels/` 出发要向上两级，解析到**仓库之外**。标准克隆下那里并没有 Common，编译时可能报找不到头文件，需要改成 `-I../Common` 才能通过——这大概率是从 Samples 更深的原始目录层级拷贝时遗留的路径。待本地验证。
- [Shuffle/cuda_shuffle/Makefile:L242-L243](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/Makefile#L242-L243) 与 [Shuffle/cuda_global/Makefile:L242-L243](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_global/Makefile#L242-L243) —— Shuffle 的两套代码同样 `-I../../Common`（从 `Shuffle/cuda_*` 向上两级恰是仓库根，路径正确）。也就是说，**实际引用 Common 的是 4 个基准**，比 [README.md:L113-L115](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L113-L115) 写的 3 个多出 Shuffle——文档又一次与代码有出入。

#### 4.4.4 代码实践

1. **实践目标**：不依赖 README 的说法，用 grep 独立确定 Common 的全部使用者，并找出其中的路径疑点。
2. **操作步骤**：
   1. 在仓库根目录执行：`grep -n "Common" */Makefile */*/Makefile`。
   2. 记录每个命中的目录与 `-I` 后的相对路径。
   3. 对每个目录，从其 Makefile 所在位置出发心算相对路径，判断解析结果是否落在仓库内的 `Common/`。
   4. 再执行 `grep -ln "helper_cuda.h\|helper_timer.h" */*.cu */*.c */*.cpp 2>/dev/null`（示意命令，可按需调整），看哪些源文件真正 include 了 helper 头文件。
3. **需要观察的现象**：命中 5 处（GSOverlap、TaskGraph、Conkernels、Conkernels 的 Makefile_serialized、Shuffle 的两个子目录，共 6 个 Makefile）；其中 Conkernels 的两处相对路径多了一级 `../`。
4. **预期结果**：一张"Makefile → INCLUDES 路径 → 是否解析到仓库内 Common"的小表；Conkernels 两行标红。若你随后尝试编译 Conkernels（需要 CUDA 环境），预期会因找不到 `helper_cuda.h` 等头文件而失败——待本地验证。
5. 本实践的 grep 部分不需要 GPU，任何机器都可完成（本讲义编写时已验证命中情况）。

#### 4.4.5 小练习与答案

**练习 1**：README 说 Common 服务于哪三个基准？你 grep 出来的结果是什么？

**参考答案**：README 说是 GSOverlap、ConKernels、Taskgraph；grep Makefile 显示使用者还包括 Shuffle（cuda_global 与 cuda_shuffle 两个子项目）。文档少算了一个。

**练习 2**：除了读 Makefile，还有哪些肉眼特征能判断 Conkernels 来自 CUDA Samples？

**参考答案**：目录里有 Samples 风格的 README（Description/Key Concepts/Supported SM Architectures 分节，[Conkernels/README.md:L1-L13](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/README.md#L1-L13)）、三套 Visual Studio 工程（vs2015/vs2017/vs2019 的 .sln 与 .vcxproj）以及 NsightEclipse.xml；GSOverlap、TaskGraph 目录同样如此。

**练习 3**：Common 目录里的头文件是作者自己写的吗？使用它们要注意什么？

**参考答案**：不是，它们复制自 NVIDIA CUDA Samples（[Common/README.md:L1](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Common/README.md#L1)），随 Samples 自身的许可条款发布；引用时相对路径必须正确（Conkernels 的 `-I../../Common` 即为反例），二次开发时也可参照第 u6-l3 讲做依赖最小化。

## 5. 综合实践

**任务：产出一份《CUDAMicroBench 全景测绘报告》**，把本讲四个模块串起来。在你的机器上（无需 GPU）完成：

1. **清点**：`ls -d */` 列出全部目录，区分基准目录（16 个）、公共依赖（Common）、占位（case_study_shuffle）、本教程（CUDAMicroBench-tutorial）。
2. **分类**：对照 [README.md:L19-L98](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L19-L98) 的表格，为 16 个基准目录各写一行：`目录 | README 基准名 | 三大类归属 | 反模式 | 优化技术 | 骨架形态 | 有无 test.sh | 有无历史结果 | 是否 Samples 出身`。
3. **核伪**：用 `grep -n "Common" */Makefile */*/Makefile` 与 `git ls-files -s case_study_shuffle` 验证本讲指出的三处"文档与实地偏差"（目录数 16≠14；6 个目录无 test.sh；Common 实际服务 4 个基准）。
4. **选路**：在报告末尾写下你最想先深入的一个大类与一个基准，并说明理由（提示：若你是 CUDA 初学者，第二类的 `CoMem_AXPY` 载体最简单；若关心新特性，第一类的 `TaskGraph`/`DynParallel` 展示 CUDA 11 能力）。

验收标准：报告能回答——"UniMem 的反模式是什么？它的目录里没有 test.sh 吗？它是不是 Samples 出身？"（参考答案：低访问密度；有 test.sh 与 test2.sh；不是。）

## 6. 本讲小结

- CUDAMicroBench 是 IPDPSW 2021 论文配套的 14 个 CUDA 性能微基准，3 条款 BSD 许可，由 UNC Charlotte HPCAS Lab 与 LLNL 合作开发；没有统一入口，必须进入单个目录编译运行。
- README 的汇总表把性能问题分成三大类：饱和大规模并行（4 个）、利用 GPU 内部深存储层次（6 个）、安排 CPU-GPU 数据搬运（4 个），每行都是"反模式 + 优化技术"的配对。
- 14 个基准名对应 16 个代码目录：CoMem 与 ReadOnlyMem 各有两个实例目录，Shuffle 内部含两套对照子项目；`case_study_shuffle` 是无 `.gitmodules` 的 gitlink 占位，本地为空。
- 典型骨架是 host `.c` + kernel `.cu` + `.h` + Makefile + test.sh + 集群历史结果文件；但 6 个目录没有 test.sh，DynParallel、Conkernels、TaskGraph、GSOverlap、HDOverlap、UniMem 采用单文件一体或对照双文件形态。
- GSOverlap、Conkernels、TaskGraph 派生自 CUDA Samples 并依赖 Common 里的 helper 头文件；grep 显示 Shuffle 实际也引用 Common，且 Conkernels 的 `-I../../Common` 相对路径疑似多了一级。
- 读文档要动手核验：目录数、脚本有无、头文件引用，三处偏差都是靠 `ls`、`grep`、`git ls-files` 发现的。

## 7. 下一步学习建议

下一讲（u1-l2「环境准备、编译与运行」）将带你确认 `nvcc` 与 GPU 计算能力，进入 `CoMem_AXPY` 目录完成第一次 `make` 与 `test.sh` 运行，并解释 `sm_30`、`-rdc=true` 等编译选项。

在继续之前，建议先自己浏览两处源码热身：

- [CoMem_AXPY/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/Makefile) 与 [CoMem_AXPY/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh)——下一讲的编译运行主角，先混个眼熟。
- [Common/README.md](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Common/README.md)——理解这个"来自 Samples 的工具箱"为什么存在，为后续编译 Samples 出身的基准做准备。

# BLAS 三级抽象与 GEMM

## 1. 本讲目标

本讲聚焦 Vitis 加速库家族中的 `blas`（基础线性代数子程序）库。读完后你应该能够：

- 说清 `blas` 库「**module / kernel / software-API**」三级抽象各自解决什么问题、产物是什么、对应仓库的哪一层目录。
- 区分两套容易混淆的「层级」概念：Vitis 工程意义上的 **L1/L2/L3** 与数学意义上的 **BLAS Level 1/2/3**。
- 读懂 L1 层 GEMM 计算模块（脉动阵列）的模板签名与 `DATAFLOW` 组合方式。
- 读懂 L3 层 GEMM 软件 API（`xfblasCreate` / `xfblasGemm`）的指令式调用模型，并跑通 `gemm_test.cpp`。
- 用 `run_test.py` 测试总线 + `Makefile` 批量驱动 L1 测试，并解释 `blas_gen.mk` 如何编译「测试用例生成器」。

本讲承接 u3-l2（HLS pragma 如何映射硬件，尤其是 `DATAFLOW`/`UNROLL`/`PIPELINE`）与 u5-l3（L3 多内核流水线组合）。在 `blas` 库里，你会同时看到这两讲的心智模型：L1 用 `DATAFLOW` 把多个原语缝成一次 GEMM，L3 用「指令队列」把多次 GEMM 缝成一个软件 API。

## 2. 前置知识

### 2.1 什么是 BLAS

**BLAS**（Basic Linear Algebra Subprograms，基础线性代数子程序）是科学计算领域的一套事实标准接口，按运算对象分三个数学层级：

| 数学层级 | 运算对象 | 典型运算 | 复杂度（N 维） |
| --- | --- | --- | --- |
| Level 1 | 向量–向量 | `dot`（点积）、`axpy`（向量加）、`nrm2`（范数） | \(O(N)\) |
| Level 2 | 矩阵–向量 | `gemv`（矩阵乘向量）、`symv`、`trmv` | \(O(N^2)\) |
| Level 3 | 矩阵–矩阵 | `gemm`（矩阵乘矩阵） | \(O(N^3)\) |

GEMM 是其中最核心、性能收益最大的一类：\(C \leftarrow \alpha AB + \beta C\)。几乎所有深度学习、科学计算的算力都消耗在 Level 3 上，因此硬件加速库里 GEMM 几乎是「旗舰内核」。

### 2.2 一个必须警惕的术语陷阱

Vitis 仓库里**同时存在两套「层级」编号**，它们毫不相干，混用会理解错位：

- **Vitis 工程层级 L1/L2/L3**：仓库目录约定。L1 = 可复用算法原语（HLS C++），L2 = 可上板内核 + 主机，L3 = 应用流水线。这是贯穿全库的目录骨架（见 u1-l3）。
- **BLAS 数学层级 Level 1/2/3**：运算分类（向量 / 矩阵–向量 / 矩阵–矩阵），与目录无关。

本讲的标题「BLAS 三级抽象」指的是 `blas` 库 README 里说的**第三套**概念——**module / kernel / software-API** 三个加速层次，它恰好映射到 Vitis 的 L1/L2/L3 目录。请始终在脑子里区分：「这是数学 Level（dot/gemv/gemm）还是工程 L（哪一层目录）还是抽象层（module/kernel/API）」。

### 2.3 矩阵乘与脉动阵列的直觉

朴素 GEMM 三重循环 \(C_{ij} = \sum_k A_{ik}B_{kj}\) 天然可并行。FPGA 上最经典的实现是**脉动阵列（systolic array）**：把一组处理单元（PE）排成网格，A 沿列方向流动、B 沿行方向流动，每个 PE 周期性地「乘加 + 把数据传给邻居」，数据像心脏泵血一样节律流过阵列（systolic 即「搏动」之意）。这样每个数据被多个 PE 复用，单位时间能完成的乘加数随阵列规模线性增长。本讲 L1 模块的 `SystolicArray` 类就是这一思路的 HLS 实现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [blas/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/README.md) | 库定位与「module/kernel/API」三级抽象的官方说明、运行方式 |
| [blas/L1/include/hw/xf_blas.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/include/hw/xf_blas.hpp) | L1 顶层汇总头件，按 BLAS Level 1/2/3 聚合所有原语 |
| [blas/L1/include/hw/xf_blas/gemm.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/include/hw/xf_blas/gemm.hpp) | GEMM 计算模块（`Gemm` 类 + 自由函数重载） |
| [blas/L1/include/hw/xf_blas/gemm/systolicArray.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/include/hw/xf_blas/gemm/systolicArray.hpp) | 脉动阵列核心（PE 网格的 HLS 实现） |
| [blas/L3/include/sw/xf_blas.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/include/sw/xf_blas.hpp) | L3 顶层头件，引入软件 API 包装 `wrapper.hpp` |
| [blas/L3/include/sw/xf_blas/wrapper.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/include/sw/xf_blas/wrapper.hpp) | L3 软件 API 实现（`xfblasCreate`/`xfblasGemm` 等） |
| [blas/L3/include/sw/xf_blas/gemm_host.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/include/sw/xf_blas/gemm_host.hpp) | GEMM 主机端：把调用翻译成一条指令 |
| [blas/L3/include/sw/utility/utility.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/include/sw/utility/utility.hpp) | 状态码 / 引擎 / 操作类型枚举 |
| [blas/L3/tests/gemm/gemm_test.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/tests/gemm/gemm_test.cpp) | L3 GEMM 端到端测试（含黄金参考与误差比对） |
| [blas/L1/tests/sw/python/run_test.py](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/sw/python/run_test.py) | L1 测试总线（Python 驱动器） |
| [blas/L1/tests/Makefile](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/Makefile) | 把 `make run OP=...` 翻译成 `run_test.py` 调用 |
| [blas/L1/tests/blas_gen.mk](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/blas_gen.mk) | 编译「测试用例生成器」二进制（`blas_gen_bin`） |
| [blas/L1/tests/hw/gemm/uut_top.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/hw/gemm/uut_top.cpp) | GEMM 的 L1 DUT（用 `DATAFLOW` 串联搬运器与 `gemm`） |

## 4. 核心概念与源码讲解

### 4.1 三级抽象：module / kernel / software-API

#### 4.1.1 概念说明

`blas` 库 README 开篇就声明它面向**三类不同的使用者**，提供**三个加速层次**。这套「三级抽象」是理解整个库的钥匙：

1. **module level（模块级）**：BLAS 函数的纯 C++ 实现，给 HLS 用户用作搭积木的零件，自己组合成 FPGA 逻辑。位于 `L1/include/hw`。
2. **kernel level（内核级）**：预定义的、已经封装好的内核，演示「如何用 L1 原语搭出一个完整内核」，供任何 Vitis 用户直接调用。位于 `L2`。
3. **software APIs level（软件 API 级）**：建在 XRT（Xilinx 运行时）之上的高层 API，让**软件工程师**无需写任何运行时代码或硬件配置就能用上 BLAS 加速。位于 `L3`。

这三层恰好就是 Vitis 的 L1/L2/L3 目录（u1-l3 讲过的工程层级），但 README 用「面向使用者的抽象层」重新命名了一遍。换句话说：**`blas` 是全仓库里 L1/L2/L3 三层最齐全、命名最清晰的库之一**，正好拿来印证 L1/L2/L3 的设计哲学。

#### 4.1.2 核心流程

三层的递进关系可以画成一条「封装链」：

```text
数学原语 (dot/axpy/gemv/gemm ...)
      │  聚合成头件 xf_blas.hpp          ── module level   (L1)
      ▼
预定义内核 (streamingKernel/memKernel)
      │  打包成 XO/xclbin + 主机框架      ── kernel level   (L2)
      ▼
软件 API (xfblasCreate/xfblasGemm ...)
      │  屏蔽 XRT/缓冲/指令细节           ── software-API    (L3)
      ▼
软件工程师: 只看到类似 cuBLAS 的 C++ 接口
```

沿这条链，**对使用者的暴露面越来越窄、易用性越来越高，但对硬件的控制力越来越弱**：

- 在 module 级，你直接调 `xf::blas::gemm<...>(...)`，自己管 `hls::stream` 与 `DATAFLOW`（u3-l1/l2）。
- 在 software-API 级，你只需 `xfblasGemm(OP_N, OP_N, m, n, k, ...)`，连 xclbin 路径都由 API 帮你加载。

#### 4.1.3 源码精读

README 用三句话把三层抽象写死了，这是全库最权威的定义：

[blas/README.md:9-13](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/README.md#L9-L13) —— README 列出三个加速层次：module level 面向 HLS 用户、kernel level 是预定义内核、software-API level 建在 XRT 之上让软件工程师零运行时代码使用。

随后 README 把这三层明确绑定到 `L1/L2/L3` 三个目录：

[blas/README.md:44-61](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/README.md#L44-L61) 说明：`L1` 跑 HLS 流程做功能/资源/时序检查；`L2` 把内核与 OpenCL/XRT 主机组装成 xclbin；`L3` 演示多内核流水线应用。这段同时点明了每层用的命令：L1 用大写 `TARGET=csim/csynth/...`（u2-l3 讲过的 HLS 五档），L2/L3 用小写 `TARGET=hw_emu/hw`（u5-l1 讲过的 Vitis 三档）。

#### 4.1.4 代码实践

**实践目标**：亲手核对「三层抽象 ↔ 三个目录」的对应关系，建立空间记忆。

**操作步骤**：

1. 打开 [blas/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/README.md)，找到 "three levels of acceleration" 那段。
2. 对照仓库实际目录：`blas/L1/include/hw/`（module）、`blas/L2/src/streamingKernel/`（kernel）、`blas/L3/include/sw/`（software-API）。
3. 画一张三列对照表：左列写 README 的抽象名（module/kernel/software-API），中列写对应目录，右列写该层「面向谁」。

**需要观察的现象**：三层的目录与 README 描述一一对应，且每层目录里都能找到 GEMM 相关文件（L1 的 `gemm.hpp`、L2 的 `streamingKernel`、L3 的 `wrapper.hpp`）。

**预期结果**：你会确认 GEMM 在三个抽象层都有体现——这正是后续 4.2、4.3 要展开的。

#### 4.1.5 小练习与答案

**练习 1**：BLAS 数学 Level 3（矩阵–矩阵运算）和 Vitis 工程层级 L3（应用流水线）是同一个概念吗？

**参考答案**：不是。前者是按运算对象分类的数学层级（Level 1 向量、Level 2 矩阵–向量、Level 3 矩阵–矩阵），与目录无关；后者是仓库的工程目录约定（L1 原语 / L2 内核 / L3 应用）。巧合的是 `blas` 库的「software-API 抽象层」落在了工程 L3 目录里，二者编号相同但含义不同。

**练习 2**：一个完全不懂 FPGA 的算法工程师想用 BLAS 加速，应该从哪一层入手？

**参考答案**：software-API 级（工程 L3）。该层把 XRT、缓冲、xclbin 加载全部封装在 `xfblasCreate/xfblasGemm` 后面，调用方式类似 cuBLAS，不需要写任何运行时或硬件配置代码。

---

### 4.2 xf_blas 模块：L1 数学原语与 GEMM 计算核

#### 4.2.1 概念说明

「xf_blas 模块」指 module level 的全部数学原语，集中在 `L1/include/hw/xf_blas/` 下，由顶层头件 `xf_blas.hpp` 汇总。这里的关键认知是：**顶层头件是按 BLAS 数学 Level 1/2/3 分组组织的**——你能在同一个文件里看到两套「层级」编号的交汇点。

GEMM 的**计算核**（真正做乘加的硬件）也住在这里：一个模板化的 `Gemm` 类，其内部是一个 `SystolicArray`（脉动阵列）。注意它只负责「算」，不负责「搬数据」——数据进出由 `hls::stream` 完成，搬运器在测试里另写（见 4.2.3 的 `uut_top.cpp`）。

#### 4.2.2 核心流程

L1 层 GEMM 的计算流程：

```text
p_As (A 的打包流)  ─┐
                    ├─▶  Gemm::gemm()  ──▶  p_sum (乘加结果流)
p_Bs (B 的打包流)  ─┘
                          内部：SystolicArray.process()
                                A/B 在 PE 网格中流动并累加
```

带 \(\alpha/\beta\) 的完整 GEMM（\(R = \alpha AB + \beta C\)）则用 `DATAFLOW` 把四个原语串成一条任务级流水（u3-l2 讲过的 dataflow）：

```text
gemm (算 AB)  ──▶  gemmBufferC (缓存)  ──┐
                                          ├─▶  axpy (alpha·AB + beta·C) ──▶ R
                          scal (算 beta·C) ─┘
```

这正是「L1 原语互相组合」的典型范式：GEMM 自身复用了 `axpy`（向量加）和 `scal`（标量乘）两个更底层的原语。

#### 4.2.3 源码精读

顶层汇总头件按数学 Level 分组，是观察两套层级交汇的最佳位置：

[blas/L1/include/hw/xf_blas.hpp:31-55](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/include/hw/xf_blas.hpp#L31-L55) —— 注释 `BLAS L1/L2/L3 function modules` 分三段聚合原语：Level 1（amax/amin/asum/axpy/copy/dot/scal/swap/nrm2，向量运算）、Level 2（gemv/gbmv/symv/trmv，矩阵–向量）、Level 3（gemm，矩阵–矩阵）。注意这里的 `L1/L2/L3` 注释指的是**数学 Level**，不是工程目录。

GEMM 计算核的类签名，展示了 module 级原语典型的「模板参数化 + WideType 打包」风格：

[blas/L1/include/hw/xf_blas/gemm.hpp:36-57](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/include/hw/xf_blas/gemm.hpp#L36-L57) —— `Gemm` 类模板参数：`t_DataType`（元素类型）、`t_KBufferDim`（K 维缓存大小）、`t_ParEntriesM`/`t_ParEntriesN`（M/N 方向并行度，即每周期处理几个元素，类似 u6-l1 的 SSR 并行）、`t_MacDataType`（乘加中间类型，常用于放宽带宽防溢出）。输入输出均为 `hls::stream`，这是 u3-l1 讲过的流式约定。

`float` 特化版本把工作直接委托给脉动阵列，并用 `DATAFLOW` 包裹：

[blas/L1/include/hw/xf_blas/gemm.hpp:130-157](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/include/hw/xf_blas/gemm.hpp#L130-L157) —— 对 `Gemm<float,...>` 的偏特化，`gemm()` 内部 `#pragma HLS DATAFLOW` 后构造 `SystolicArray` 并调用 `process(p_As, p_Bs, p_sum)`。这正是 u3-l2 讲的「dataflow 把子任务经 stream 串成流水」的标准写法。

脉动阵列本身的入口：

[blas/L1/include/hw/xf_blas/gemm/systolicArray.hpp:31-52](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/include/hw/xf_blas/gemm/systolicArray.hpp#L31-L52) —— `SystolicArray` 类，模板参数与 `Gemm` 一致。`process()` 内部声明 `l_sum[t_ParEntriesM]`、`l_dataA[t_ParEntriesM]` 两组数组并 `ARRAY_PARTITION ... complete` 完全拆分，对应 PE 网格的每一行；`#pragma HLS stream depth=...` 为 PE 间数据通路指定 FIFO 深度。这就是「搏动」数据流的物理来源。

带 \(\alpha/\beta\) 的自由函数重载，演示了原语复用：

[blas/L1/include/hw/xf_blas/gemm.hpp:194-224](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/include/hw/xf_blas/gemm.hpp#L194-L224) —— 计算 \(R = \alpha AB + \beta C\)：先 `Gemm::gemm` 算出 \(AB\) 到 `l_sum`，再 `gemmBufferC` 缓存、`scal` 算 \(\beta C\)、最后 `axpy` 做 \(\alpha AB + \beta C\)。整段用 `DATAFLOW` 串联，四步任务级并发。注意它复用了同库的 `scal` 与 `axpy` 两个 Level 1 原语——**L1 原语之间是互相调用的**。

module 级原语怎么被测？看 L1 DUT，它把矩阵搬运和计算用 `DATAFLOW` 缝起来：

[blas/L1/tests/hw/gemm/uut_top.cpp:17-37](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/hw/gemm/uut_top.cpp#L17-L37) —— DUT `uut_top`：用 `gemmMatAMover`/`gemmMatBMover` 把数组 `p_A/p_B` 转成流，`readVec2Stream` 把 `p_C` 转成流，喂给 `gemm<>`，再用 `writeStream2Vec` 把结果流写回 `p_R`。`#pragma HLS DATAFLOW` 让搬运与计算并发。这就是 module 级原语被「包成可综合顶层」的标准模式（u3-l1 的 extern DUT 思路，这里 DUT 名为 `uut_top`）。

#### 4.2.4 代码实践

**实践目标**：理解 `t_ParEntries`（并行度）如何决定每周期吞吐，并跑一个 GEMM L1 csim。

**操作步骤**：

1. 进入 `blas/L1/tests/hw/gemm/tests/`，观察预生成配置目录名（如 `Dfloat_m32_n16_k64_par8`），命名里 `par8` 即 `BLAS_parEntries=8`。
2. 选一个配置目录，查看其 `params.hpp`（由 `test.mk` 生成的 `#define` 集合），确认 `BLAS_parEntries` 的值。
3. 若环境就绪（已 source Vitis/XRT，见 u2-l1），在该目录执行 `make run TARGET=csim`（待本地验证）。

**需要观察的现象**：csim 会读 `data/matA.bin`、`matB.bin`、`matC.bin`、`golden.bin`，调用 `uut_top`，再把结果与 `golden.bin` 比对。

**预期结果**：终端打印 `Pass!`（见 [test.cpp:40-46](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/hw/gemm/test.cpp#L40-L46)）。若改动 `BLAS_parEntries`，需同时保证 `m/n/k` 是其整数倍（`gemm.hpp` 第 186–188 行的 `assert` 强制了这点），否则断言失败。**待本地验证**：因本环境无 Vitis，无法实跑，请在你自己的环境确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Gemm` 类要单独为 `float` 做一个偏特化？

**参考答案**：通用模板用一个手写的窗口/移位寄存器实现（`WindowRm`/`TriangSrl`，见 `gemm.hpp` 第 62–127 行），而 `float` 特化直接复用优化好的 `SystolicArray` 并加 `DATAFLOW`。对不同数据类型用不同实现，是 HLS 库常见的「按类型选最优微架构」策略。

**练习 2**：带 \(\alpha/\beta\) 的 `gemm` 自由函数复用了哪两个 Level 1 原语？为什么这里要用 `DATAFLOW`？

**参考答案**：复用了 `scal`（算 \(\beta C\)）和 `axpy`（算 \(\alpha AB + \beta C\)）。用 `DATAFLOW` 是为了让 `gemm`(算 \(AB\))、`gemmBufferC`(缓存)、`scal`、`axpy` 四步任务级并发——只要相邻任务间用 `hls::stream` 传递，就能形成流水线，端到端吞吐由最慢的一步决定（u3-l2 的 dataflow 心智模型）。

---

### 4.3 GEMM L3 软件 API：指令式调用模型

#### 4.3.1 概念说明

到了 software-API 级（工程 L3），库提供给使用者的是一组形似 cuBLAS 的高层 C++ 函数：`xfblasCreate`、`xfblasMallocRestricted`、`xfblasSetMatrixRestricted`、`xfblasGemm`、`xfblasGetMatrixRestricted`、`xfblasDestroy`。这些函数把 XRT 的 device/kernel/bo（u4-l2/l3）和内核指令细节全部藏在背后。

这一层最反直觉、也最关键的设计是：**`xfblasGemm` 并不立即执行矩阵乘**。它只是把「这次 GEMM 的参数」翻译成一条指令，追加到一个指令缓冲里；真正的执行由 `xfblasExecute` / `xfblasExecuteAsync` 触发。这是一种**命令队列（command queue）/ 指令驱动**的模型，与 u4-l2 里「start/wait 即时执行」的朴素模型不同，更接近 GPU 的异步提交。

#### 4.3.2 核心流程

L3 GEMM 一次完整调用的生命周期：

```text
xfblasCreate(xclbin, config_info.dat, XFBLAS_ENGINE_GEMM)
   │   读 config_info.dat → ConfigDict
   │   加载 xclbin → XFpga
   │   为每个 kernel 创建 GEMMHost
   ▼
xfblasMallocRestricted(A/B/C)         ── 在设备上登记矩阵、算偏移
xfblasSetMatrixRestricted(A/B/C)      ── 主机 → 设备搬数据
   ▼
xfblasGemm(OP_N, OP_N, m, n, k, ...)  ── 翻译成一条 GemmArgs 指令，入队
   ▼
(隐式或显式 execute)                    ── 内核按指令执行
   ▼
xfblasGetMatrixRestricted(C)          ── 设备 → 主机取结果
xfblasFree / xfblasDestroy            ── 释放与销毁
```

其中 `xfblasGemm` → `GEMMHost::addGEMMOp` → `addInstr` 的链路，是把「面向人的矩阵指针」翻译成「面向硬件的设备偏移 + 维度参数」的关键一跳。

#### 4.3.3 源码精读

L3 顶层头件极其精简，只引入软件 API 包装并 `using namespace`：

[blas/L3/include/sw/xf_blas.hpp:25-27](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/include/sw/xf_blas.hpp#L25-L27) —— 只 `#include "xf_blas/wrapper.hpp"` 并 `using namespace xf::blas`。所有 API 实现都在 `wrapper.hpp`。

状态码、引擎、操作类型三个枚举，是 API 的「词汇表」：

[blas/L3/include/sw/utility/utility.hpp:26-41](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/include/sw/utility/utility.hpp#L26-L41) —— `xfblasStatus_t`（SUCCESS=0、NOT_INITIALIZED=1、INVALID_VALUE=2、NOT_SUPPORTED=4……）、`xfblasEngine_t`（GEMM/GEMV/FCN 三种引擎）、`xfblasOperation_t`（OP_N/OP_T/OP_C，即不转置/转置/共轭转置）。后续几乎所有 API 都返回 `xfblasStatus_t`。

`xfblasCreate` 是「创建句柄」的入口，体现了 ConfigDict + XFpga + GEMMHost 三件套：

[blas/L3/include/sw/xf_blas/wrapper.hpp:56-88](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/include/sw/xf_blas/wrapper.hpp#L56-L88) —— 先 `buildConfigDict` 把 `config_info.dat` 解析进单例 `ConfigDict`，再用 `XFpga` 加载 xclbin（对应 u4-l2 的 device/load_xclbin），最后按 `kernelNumber` 创建若干 `GEMMHost` 句柄存进单例 `BLASHostHandle`。若 config 里 `BLAS_runGemm != "1"` 则拒绝创建。

`xfblasGemm` 是本层的核心，但注意它**能力受限且不立即执行**：

[blas/L3/include/sw/xf_blas/wrapper.hpp:641-684](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/include/sw/xf_blas/wrapper.hpp#L641-L684) —— 只接受 `transa==OP_N && transb==OP_N && alpha==1 && beta==1`（即 \(C=AB\)，不转置、无缩放），其余一律返回 `XFBLAS_STATUS_NOT_SUPPORTED`；维度若不是 `minSize` 整数倍则自动 padding。满足条件时取出 `GEMMHost` 句柄，调用 `addGEMMOp(...)`——注意函数名是 **add**，即「追加一条指令」，而非「执行」。

`addGEMMOp` 把主机指针翻译成设备偏移并打包成指令结构体：

[blas/L3/include/sw/xf_blas/gemm_host.hpp:130-161](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/include/sw/xf_blas/gemm_host.hpp#L130-L161) —— `GemmArgs` 结构体把 `OpGemm` 操作码、A/B/C/X 四个偏移、m/k/n 维度、leading dimension、postScale/postShift 打包成定长字节块（`asByteArray`/`sizeInBytes`），这就是发给内核的「一条指令」。

[blas/L3/include/sw/xf_blas/gemm_host.hpp:171-219](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/include/sw/xf_blas/gemm_host.hpp#L171-L219) —— `addGEMMOp` 先校验四个矩阵指针都已分配（`m_bufHandle.find`），再把它们的设备地址减去基地址、除以 `PAGE_SIZE` 得到页偏移，构造 `GemmArgs`，调用 `addInstr` 入队并 `enableRun`。这一段把 u4-l3 的 buffer object 抽象具体化为「指令里的偏移字段」。

端到端测试把整条 API 链走一遍，并自带黄金参考：

[blas/L3/tests/gemm/gemm_test.cpp:87-92](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/tests/gemm/gemm_test.cpp#L87-L92) —— `xfblasCreate(xclbin, configFile, XFBLAS_ENGINE_GEMM, numKernel)` 创建句柄，失败则打印状态码退出。

[blas/L3/tests/gemm/gemm_test.cpp:122-153](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/tests/gemm/gemm_test.cpp#L122-L153) —— 典型的 L3 调用序列：`xfblasMallocRestricted` 登记 A/B/C → `xfblasSetMatrixRestricted` 把数据搬到设备 → `xfblasGemm(OP_N, OP_N, m=64, n=64, k=64, 1, a, k, b, n, 1, c, n, ...)` 提交一次矩阵乘 → `xfblasGetMatrixRestricted` 取回结果。注意 `m=n=k=64` 在文件头 `#define`（第 27–29 行）。

[blas/L3/tests/gemm/gemm_test.cpp:176-180](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/tests/gemm/gemm_test.cpp#L176-L180) —— `compareGemm(c, goldenC)` 判定 PASS/FAIL。`goldenC` 由 `getGoldenMat`（第 33–46 行）在 CPU 上算 \(C = AB + C\) 得到，比对时用相对/绝对双阈值（`compareGemm` 第 48–68 行，`p_TolRel=1e-3`、`p_TolAbs=1e-5`）——**不是 bit 精确**，这点与 u6-l1 的 FFT、u7-l1 的分解测试一致：浮点/定点硬件结果只能按误差阈值判定。

#### 4.3.4 代码实践

**实践目标**：把 `gemm_test.cpp` 的 API 调用序列与 4.3.2 的流程图逐行对照，确认你理解了「指令式」语义。

**操作步骤**：

1. 打开 [blas/L3/tests/gemm/gemm_test.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/tests/gemm/gemm_test.cpp)。
2. 用笔在 `main` 里标注每行属于流程图的哪一步（Create / Malloc / Set / Gemm / Get / Free / Destroy）。
3. 找到 `xfblasGemm` 的调用，确认它传的是 `XFBLAS_OP_N, XFBLAS_OP_N` 与 `alpha=1, beta=1`，并解释为什么换成 `XFBLAS_OP_T` 会被 `wrapper.hpp` 第 660 行的判断拒绝。

**需要观察的现象**：调用序列严格遵循「先 Malloc 再 Set 再 Gemm 再 Get」的顺序，且每步都检查返回的 `xfblasStatus_t`。

**预期结果**：你会注意到 `xfblasGemm` 之后没有显式 `xfblasExecute`——本测试依赖 `addGEMMOp` 内部的 `enableRun` 与后续 `Get` 触发的隐式同步。**待本地验证**：实跑需 Alveo 卡 + 预先构建的 `gemx.xclbin` 与 `config_info.dat`（见 usage 注释第 18 行）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `xfblasGemm` 不直接执行矩阵乘，而是 `addGEMMOp`「追加指令」？

**参考答案**：为了支持**指令队列 / 批量提交**。软件 API 级面向的是「连续做很多线性代数运算」的场景（如神经网络推理）。把每次 GEMM 翻译成一条指令累积起来，再一次性 `execute`，能减少主机–设备往返、让内核连续执行多条指令，提升吞吐。这比 u4-l2 的「每次 start/wait」更适合密集运算。

**练习 2**：`xfblasGemm` 对 `alpha`、`beta`、转置有哪些限制？违反了返回什么状态码？

**参考答案**：只支持 `OP_N/OP_N`（都不转置）且 `alpha==1 && beta==1`，即仅 \(C=AB\)。任何其它组合（转置、缩放）都返回 `XFBLAS_STATUS_NOT_SUPPORTED`（枚举值 4）。若需要 \(\alpha/\beta\) 或转置，目前需回退到更底层（L1/L2）自行实现。

---

### 4.4 run_test.py 测试总线

#### 4.4.1 概念说明

`run_test.py` 是 `blas` 库 L1 测试的「**测试总线**（test bus）」——一个 Python 驱动器，把「描述测试的 profile」翻译成「实际的 csim/csynth/cosim 运行 + 结果收集」。它取代了别处库「每个用例自带 Makefile 直接 make」的简单做法，原因是 `blas` 的 L1 原语需要**笛卡尔积式**地测多种数据类型、多种维度、多种并行度，手写 Makefile 不现实，必须由脚本按 profile 批量生成与运行。

它通过软链接进入测试根目录（`blas/L1/tests/run_test.py -> sw/python/run_test.py`），由 `blas/L1/tests/Makefile` 调用。配套的 `blas_gen.mk` 则负责编译一个 C++「测试用例生成器」二进制（`blas_gen_bin`），用于产出输入数据与指令。

#### 4.4.2 核心流程

`make run OP=dot` 到结果落盘的完整链路：

```text
make run OP=dot CSIM=1                          (blas/L1/tests/Makefile)
   │  PFLAGS = --override --operator dot --xpart <part> --csim ...
   ▼
python run_test.py $(PFLAGS)                    (run_test.py)
   │  --operator dot  →  ./hw/dot/profile.json
   │  RunTest(profile).parseProfile()           读 profile.json 的 dataTypes/dims/...
   │  RunTest.run()                              按笛卡尔积生成并跑 csim/cosim
   ▼
statistics.rpt                                  汇总每个 op 的 csim/cosim 数与 Passed/Failed
   │  若有 Failed → sys.exit(1)，否则 exit(0)
```

`run_test.py` 既能按 `--operator` 批量选算子，也能按 `--profile` 直接给一组 `profile.json`；还支持 `--parallel` 多线程并发跑多个 op。

#### 4.4.3 源码精读

`run_test.py` 的命令行契约，定义了它接受哪些参数：

[blas/L1/tests/sw/python/run_test.py:133-197](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/sw/python/run_test.py#L133-L197) —— `argparse` 定义：`--makefile`（默认 `blas_gen.mk`）、`--profile`（一组 profile.json）与 `--operator`（一组算子名，二者互斥且必选其一）、`--xpart`（默认 `xcvu9p-flgb2104-2-i`）、`--csim/--csynth/--cosim/--benchmark` 开关、`--parallel`（并发数，默认 1）、`--override`。第 192–195 行把 `--operator dot` 展开成 `./hw/dot/profile.json`。

`main` 负责调度与汇总：

[blas/L1/tests/sw/python/run_test.py:96-130](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/sw/python/run_test.py#L96-L130) —— 对每个 profile 构造 `RunTest` 对象；`--parallel==1` 时串行 `process`，否则用 `ThreadPoolExecutor` 并发；最后用 `list2File` 把 `statList` 写成 `statistics.rpt`（或 `statistics_<id>.rpt`），统计里 Failed 的个数非零则 `sys.exit(1)`——这是 CI 判定通过与否的依据。

`process` 是单个 op 的执行+统计单元：

[blas/L1/tests/sw/python/run_test.py:43-93](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/sw/python/run_test.py#L43-L93) —— 调 `rt.parseProfile()` 解析 profile、`rt.run()` 跑测试，成功则在 `statList` 追加 `{Op Name, No.csim, No.cosim, Status: Passed}`；失败则按 `OP_ERROR/BLAS_ERROR/HLS_ERROR` 异常分别打印，最终标记 `Failed`。`csim = rt.numSim * rt.hls.csim` 把「仿真次数」与「是否开 csim」相乘得到实际 csim 次数。

Makefile 把 `make` 翻译成 `run_test.py` 调用：

[blas/L1/tests/Makefile:81-119](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/Makefile#L81-L119) —— `PFLAGS = --override --operator $(OP) --xpart $(XPART)`，再按 `CSIM/CSYNTH/COSIM/BENCHMARK` 变量追加 `--csim/--csynth/--cosim/--benchmark`；`runhls` 目标执行 `$(PYTHON) run_test.py $(PFLAGS)`。第 81 行 `BENCHMARK ?= 1` 表示默认开 benchmark。所以 `make run OP=dot CSIM=1` 等价于 `python3 run_test.py --override --operator dot --xpart <part> --csim --benchmark`。

`blas_gen.mk` 是「测试用例生成器」的构建脚本，把一堆 `BLAS_*` 参数编译进生成器：

[blas/L1/tests/blas_gen.mk:62-87](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/blas_gen.mk#L62-L87) —— 定义 module 级编译期参数：`BLAS_argInstrWidth=8`（指令字段位宽）、`BLAS_pageSizeBytes=4096`（与 u4-l1 的页对齐一致）、`BLAS_instrSizeBytes=8`、四个页索引（instr/param/stats/data）、`BLAS_maxNumInstrs=64`、`BLAS_memWidthBytes=64`、`BLAS_parEntries=4`，以及可覆盖的 `BLAS_dataType/BLAS_resDataType`（默认 int）。这些参数经 `DFLAGS = -D...` 注入 C++ 编译。

[blas/L1/tests/blas_gen.mk:112-123](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/blas_gen.mk#L112-L123) —— 两个构建目标：`blas_gen_bin.so`（由 `blas_gen_wrapper.cpp` 编译的共享库，供 Python 通过 `blas_gen_bin.py` 调用）与 `blas_gen_bin.exe`（由 `blas_gen_bin.cpp` 编译的可执行）。两者都是「测试用例生成器」，用来按 profile 产出输入 `.bin` 与指令。注意它用 `${XILINX_VIVADO}/tps/lnx64/gcc-6.2.0` 与自带的 boost（第 24、89–90 行），与库的 Vitis 2022.2+ 基线对齐。

一个真实的 profile 长什么样：

[blas/L1/tests/hw/dot/profile.json:1-27](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/hw/dot/profile.json#L1-L27) —— dot 算子的 profile：`b_csim/b_synth/b_cosim` 三个开关、`dataTypes`/`retTypes`（要测的类型组合 `float64/uint16/int32`）、`op` 名、`logParEntries`（并行度对数）、`vectorDims`（要测的维度 `[1024,4096,8192]`）、`valueRange`（取值范围）、`numSimulation`（每种组合重复几次）。`run_test.py` 会把这些字段做笛卡尔积，生成大量子测试。

#### 4.4.4 代码实践

**实践目标**：跟踪 `make run OP=dot` 的完整命令翻译链，并解释 `run_test.py` 与 `blas_gen.mk` 各自负责什么。

**操作步骤**：

1. 读 [blas/L1/tests/Makefile:81-119](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/Makefile#L81-L119)，写出 `make run OP=dot CSIM=1 CSYNTH=0 COSIM=0` 最终执行的那条 `python` 命令。
2. 读 [blas/L1/tests/sw/python/run_test.py:96-130](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/sw/python/run_test.py#L96-L130)，回答：`--operator dot` 是怎么变成 profile 路径的？结果写进哪个文件？
3. 读 [blas/L1/tests/blas_gen.mk:62-87](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/blas_gen.mk#L62-L87)，列出 `BLAS_dataType` 默认值，并说明 `DFLAGS` 的作用。

**需要观察的现象**：Makefile 只是「参数装配器」，真正干活的是 Python；Python 又把「按 profile 生成数据」的脏活外包给 C++ 生成器（`blas_gen_bin`）。

**预期结果**：你会得出——`run_test.py` 负责**调度与判定**（读 profile、跑仿真、写 statistics.rpt、决定 exit code），`blas_gen.mk` 负责**编译生成器**（把 `BLAS_*` 参数编进 `blas_gen_bin`，后者按 profile 产出 `.bin` 输入）。两者一软一硬，共同构成「批量生成 + 批量运行」的 L1 测试总线。

#### 4.4.5 小练习与答案

**练习 1**：如果想让 `run_test.py` 同时跑 `dot` 和 `gemv` 两个算子，命令该怎么写？如果想并发跑呢？

**参考答案**：`python run_test.py --override --operator dot gemv --xpart <part> --csim`（`--operator` 接 `nargs='*'`，可列多个）。并发跑加 `--parallel N`（N 为线程数），`main` 会用 `ThreadPoolExecutor(max_workers=N)` 同时调度多个 op 的 `process`。

**练习 2**：CI 系统怎么知道 L1 测试有没有全过？

**参考答案**：看 `run_test.py` 的 exit code。`main` 在写完 `statistics.rpt` 后统计 `Status=='Failed'` 的条目，非零则 `sys.exit(1)`，否则 `sys.exit(0)`（第 126–130 行）。CI 只需检查该进程退出码：0 = 全过，非 0 = 有失败。

---

## 5. 综合实践

**任务**：沿「三级抽象」自上而下追踪一次 GEMM，把本讲四个最小模块串成一条线。

请按以下步骤完成一份「GEMM 全链路追踪笔记」：

1. **software-API 级（L3）**：打开 [blas/L3/tests/gemm/gemm_test.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/tests/gemm/gemm_test.cpp)，列出 `main` 里从 `xfblasCreate` 到 `xfblasDestroy` 的完整 API 调用顺序，并标注每次调用返回的状态码变量。
2. **指令翻译**：跳到 [blas/L3/include/sw/xf_blas/wrapper.hpp:641-684](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/include/sw/xf_blas/wrapper.hpp#L641-L684) 与 [gemm_host.hpp:171-219](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L3/include/sw/xf_blas/gemm_host.hpp#L171-L219)，解释 `xfblasGemm` 如何把主机指针变成指令里的页偏移。
3. **module 级（L1）**：跳到 [blas/L1/include/hw/xf_blas/gemm.hpp:130-157](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/include/hw/xf_blas/gemm.hpp#L130-L157)，指出真正执行乘加的是哪个类、它的输入输出是什么类型。
4. **测试总线**：最后说明，若你要给 GEMM 的 L1 原语加一组「`int32`、`parEntries=8`、`m=n=k=64`」的测试，应该改 `hw/gemm/test.mk` 里的哪些变量、用哪个 `make` 目标生成、再用 `run_test.py` 还是直接 `make run`（提示：GEMM 的 L1 测试结构与 `dot` 不同，见 `hw/gemm/test.mk` 的 `generate` 目标）。

**预期产出**：一份能回答「GEMM 的计算在 L1 哪个类、软件 API 在 L3 哪个函数、二者怎么通过指令连起来、L1 测试怎么批量生成」的笔记。这正是本讲四个最小模块（三级抽象 / xf_blas 模块 / GEMM L3 API / run_test.py）的合流点。

> 说明：本综合实践为「源码阅读型实践」，无需硬件即可完成；若要在 csim 层实跑，需先 source Vitis/XRT 环境（u2-l1）并预生成测试数据，具体运行结果**待本地验证**。

## 6. 本讲小结

- `blas` 库 README 定义的**三级抽象**（module / kernel / software-API）恰好映射到 Vitis 的 **L1/L2/L3** 目录：module 在 `L1/include/hw`，kernel 在 `L2`，software-API 在 `L3/include/sw`。
- 必须区分三套「层级」：BLAS **数学 Level 1/2/3**（向量/矩阵–向量/矩阵–矩阵）、Vitis **工程 L1/L2/L3**（目录）、`blas` 的**抽象层**（module/kernel/API）——三者编号会重叠但含义不同。
- **GEMM 跨多层提供**：计算核（脉动阵列 `SystolicArray`）在 **L1 模块级**（`gemm.hpp`/`gemm/systolicArray.hpp`），软件 API（`xfblasGemm`）在 **L3 软件 API 级**（`wrapper.hpp`），中间的 L2 内核级把它封成可被 XRT 调用的硬件内核。
- L1 的 GEMM 用 `DATAFLOW` 把 `gemm`(算 \(AB\)) + `gemmBufferC` + `scal` + `axpy` 串成任务级流水，**原语之间互相复用**（\(R=\alpha AB+\beta C\) 复用了 `scal`/`axpy`）。
- L3 的 `xfblasGemm` 是**指令式**而非即时执行：它经 `addGEMMOp` 把矩阵指针翻译成页偏移指令入队，真正的执行由后续同步触发；且当前只支持 \(C=AB\)（`OP_N/OP_N`、\(\alpha=\beta=1\)），其余返回 `NOT_SUPPORTED`。
- L1 测试用 `run_test.py` **测试总线** + `Makefile` + `blas_gen.mk` 三件套实现「按 profile 笛卡尔积批量生成 + 批量运行 + 汇总 `statistics.rpt` + 用 exit code 判定 CI」；它取代了别处「每用例一 Makefile」的简单做法。

## 7. 下一步学习建议

- **横向对比另一套 L3 范式**：u5-l3 讲过 vision 库用「硬件内 `DATAFLOW` 拼接」实现 L3，而 `blas` 用「主机侧指令队列 API」实现 L3。建议重读 u5-l3，对比两种 L3 组合方式的取舍。
- **深入 GEMM 微架构**：若对脉动阵列感兴趣，可精读 [blas/L1/include/hw/xf_blas/gemm/systolicArray.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/include/hw/xf_blas/gemm/systolicArray.hpp) 的完整 `process()`，结合 `doubleBuffer.hpp`/`matrixBuffer.hpp` 理解双缓冲如何隐藏 DDR 延迟。
- **DSP 的 GeMM（AIE 路线）**：u6-l2 介绍了 `dsp` 库 AIE 的 `matrix_mult` 内核。建议对比 PL 路线（本讲的 `SystolicArray`）与 AIE 路线（`matrix_mult`）实现同一运算的不同范式。
- **测试基础设施进阶**：u14-l1 会系统讲解 `description.json`/`hls_config` 模板与 CI。本讲的 `run_test.py` 是 `blas` 特有的 Python 总线，可与 u14-l1 的通用 HLS 测试基础设施对照阅读。
- **更高层应用**：`blas` 的 GEMM 软件 API 是上层框架（如推理引擎）的底层砖块。学完本讲后，可关注 `blas/L3/benchmarks/gemm` 如何评测 GEMM 吞吐，把「API 易用」与「性能数字」对应起来。

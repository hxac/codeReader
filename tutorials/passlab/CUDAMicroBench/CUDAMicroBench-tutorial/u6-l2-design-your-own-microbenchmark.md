# 动手设计一个新的微基准：复用项目骨架

## 1. 本讲目标

前面五个单元里，我们一直是「读者」：解剖 CUDAMicroBench 现成的 14 个基准，理解它们各自演示的性能反模式与优化技术。本讲转换角色——你要成为「作者」，亲手设计并落地一个属于自己的 CUDA 微基准。

学完本讲，你应该能够：

1. 把 CoMem_AXPY 的「三件套 + 两个脚本」骨架当作可复用模板，在半小时内搭出一个能编译、能运行、能校验的新基准。
2. 把一个模糊的性能直觉（「跨步访问好像会变慢」）翻译成一组**只差一个变量**的对照 kernel。
3. 让实验结论站得住脚：用 nvprof 的 kernel 时间和硬件计数器指标支撑判断，而不是只看程序打印的 wall time；同时把正确性校验设计成真正有语义的探针。

本讲的交付物是一个完整的 **strided-saxpy 微基准**：`stride=1`（合并访问）与 `stride=k`（跨步访问）两个 kernel，共享同一串行基线与 check 校验，带 warmup 与 10 次平均计时，配好 Makefile 与 test.sh。

## 2. 前置知识

本讲是综合实战，密集使用前面各讲已建立的事实，这里只做要点回顾，细节请回看对应讲义。

**骨架解剖（u1-l3）**：每个微基准是三件套——host 主程序（`.c`，实验控制器）、kernel 文件（`.cu`，GPU 实现）、接口头文件（`.h`，双方契约）。host 程序遵循五阶段模板：数据初始化 → 串行基线 → warmup → 计时循环（`num_runs=10` 次取平均）→ check 校验。

**基线与校验（u2-l4）**：串行基线是单线程参考实现，不参与计时；`check()` 计算相对 L1 误差；CoMem_AXPY 实测 checksum 约 36~39，这是「计时多轮叠加、内部多 kernel、两侧调用次数不对称」的结构性产物，不是浮点误差——这一教训直接决定了本讲 4.3 节的设计。

**合并访问（u4-l2）**：合并的判断单位是 warp 同一时刻发出的 32 个地址；扇区（32 字节）是显存传输的最小粒度。`1perThread` 下标等于全局线程编号，天然 100% 合并。u4-l2 的综合实践留下的作业——「自写带 stride 参数的 kernel 做干净单变量实验」——正是本讲要交付的项目。

**测量口径（u1-l4）**：程序打印的 time 是十轮完整调用的平均墙钟，含显存分配与拷贝，远大于纯 kernel 时间（Carina 实测 56.80ms 中三个 kernel 合计仅约 9.55µs）；判定 kernel 快慢必须看 nvprof 的 GPU activities 行，`nvprof --metrics` 提供硬件计数器视角。

## 3. 本讲源码地图

本讲的模板主体是 CoMem_AXPY 全部五个文件；另外三个文件作为「实验设计模式」的仓库内先例被引用。

| 文件 | 作用 | 本讲中的角色 |
|---|---|---|
| `CoMem_AXPY/axpy.h` | 接口契约 + `REAL` 精度开关 | 模板第 1 件：改名的重点，含双定义陷阱 |
| `CoMem_AXPY/axpy_cuda.c` | host 主程序：计时、串行基线、check | 模板第 2 件：五阶段实验控制器 |
| `CoMem_AXPY/axpy_cudakernel.cu` | kernel 定义 + 主机包装函数 | 模板第 3 件：被测变量的所在 |
| `CoMem_AXPY/Makefile` | 单行式编译规则 | 模板第 4 件：可执行文件命名 |
| `CoMem_AXPY/test.sh` | 多规模扫描实验脚本 | 模板第 5 件：实验设计即脚本 |
| `WarpDivRedux/test.sh` | 带 `--metrics branch_efficiency` 的扫描 | `--metrics` 用法的仓库先例 |
| `UniMem/test.sh` | 28 档 stride 扫描 | stride 扫描脚本的仓库先例 |
| `Shuffle/cuda_shuffle/reduction.cpp` | `whichKernel` 命令行选 kernel | 多 kernel 组织的另一种模式 |

## 4. 核心概念与源码讲解

### 4.1 模板骨架复用：从「读骨架」到「套骨架」

#### 4.1.1 概念说明

复用骨架的前提是把模板代码分成两层：

- **脚手架层（照抄）**：计时函数、初始化、串行基线调用方式、check、warmup 机制、cudaMalloc/cudaMemcpy/cudaFree 流程、Makefile 与 test.sh 的形状。这一层与「你测什么」无关，是所有 14 个基准共享的公共设施。
- **被测变量层（重写）**：kernel 函数体、kernel 启动配置、串行基线的循环体、接口签名。这一层编码你的性能假设。

分层的判据是一个思想实验：**「我想研究的那个因素，在这行代码里吗？」** 在 `axpy_cudakernel_1perThread` 里，`y[i] += a*x[i]` 这一行同时承载计算和访问模式；而 `read_timer_ms()` 里没有任何一个字节承载你的假设——它是脚手架。

这套模板的真正价值在于：它是被 14 个基准反复验证过的**控制变量基础设施**。你自己从零写，很容易漏掉 warmup、忘记多轮平均、或把校验和计时搅在一起；复用模板则把这些方法论约束「焊死」在代码结构里。

#### 4.1.2 核心流程

克隆 CoMem_AXPY 目录生成新基准的七步改造清单：

```text
1. 复制目录：cp -r CoMem_AXPY StridedSaxpy（放在仓库外或新目录，勿动源码）
2. 重命名文件与接口：三个源文件改名；.h 中的函数声明、.c/.cu 中的定义与调用同步改名
3. 写串行基线：把 axpy() 的循环体换成你的参考实现（stride 作为参数）
4. 写 kernel：删掉模板的 4 个 kernel，写入你的对照 kernel 组 + warmingup
5. 改启动配置：包装函数里的 <<<grid, block>>> 与 kernel 调用序列
6. 改 Makefile：nvcc -o 的目标名 = 新可执行文件名
7. 写 test.sh：把你想要扫描的自变量（这里是 stride）逐档列出
```

其中第 2 步最容易漏改，同步点包括：`#include` 的头文件名、Makefile 里两个 `.c/.cu` 文件名、test.sh 里的 `./可执行文件名`、输出字符串。漏掉任何一处，要么编译失败（好事，立刻发现），要么脚本跑的是旧二进制（坏事，静默出错）。

#### 4.1.3 源码精读

**接口契约层**。头文件只有两件事：精度开关和 extern "C" 包裹的函数签名：

- [CoMem_AXPY/axpy.h:6-14](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h#L6-L14)：`#define REAL double` 加 `extern "C"` 包裹的 `axpy_cuda` 声明。`extern "C"` 关闭 C++ 的名字修饰，让 `.c`（按 C 编译）与 `.cu`（按 C++ 编译）链接到同一个符号——这是 u1-l2 讲过的混编机制。克隆时把 `axpy_cuda` 全局改名，但**必须保留 extern "C" 结构**。
- 陷阱：`REAL` 在 [CoMem_AXPY/axpy_cuda.c:21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L21) 又独立定义了一次。两处必须同步修改：`extern "C"` 只对齐符号名、不做类型检查，若一边 `float` 一边 `double`，链接照常通过，kernel 会按错误宽度解释内存（u2-l4 的教训）。克隆模板时建议顺手在两处各加一行注释互相提醒。

**host 控制器层**。main 函数是五阶段模板的标准样本：

- [CoMem_AXPY/axpy_cuda.c:77-80](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L77-L80)：固定种子 `srand48(1<<12)` 后初始化 `x` 与 `y_cuda`，再 `memcpy(y, y_cuda, ...)` 让串行路径与 GPU 路径**从同一初值出发**——这是 u2-l4 强调的正确性前提，照抄。
- [CoMem_AXPY/axpy_cuda.c:82](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L82)：串行基线在计时窗口之外调用一次。
- [CoMem_AXPY/axpy_cuda.c:85-89](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L85-L89)：`num_runs = 10`，墙钟计时取平均——脚手架，照抄。
- [CoMem_AXPY/axpy_cuda.c:91-92](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L91-L92)：check + 统一格式输出 `axpy(%d): checksum: %g, time: %0.2fms`。这个「名字(规模): checksum, time」的输出格式是全仓库的约定（u1-l4 靠它读归档结果），新基准请沿用，方便日后把你的 `.output.txt` 与仓库现存结果放进同一张表。
- 辅助函数也是照抄层：[CoMem_AXPY/axpy_cuda.c:14-18](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L14-L18) 的 `read_timer_ms`、L33-39 的 `init`、L51-60 的 `check`。只有 L42-48 的 `axpy` 串行循环体属于被测变量层。

**kernel 与包装层**：

- [CoMem_AXPY/axpy_cudakernel.cu:8-22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L8-L22)：`warmingup` 与 `1perThread` 函数体完全相同（u2-l1），这是模板「warmup 独立成 kernel」的写法；克隆时 warmingup 也要跟着你的新 kernel 改写。
- [CoMem_AXPY/axpy_cudakernel.cu:52-73](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L52-L73)：包装函数五段式——cudaMalloc（L54-55）→ H2D（L57-58）→ 逐 kernel 启动并同步（L61-68）→ D2H（L70）→ cudaFree（L71-72）。注意 L61-68 把 4 个 kernel **串在同一份 d_y 上顺序执行**，这个设计的利弊是 4.2 节的主题。

**构建与实验脚本层**：

- [CoMem_AXPY/Makefile:1-2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/Makefile#L1-L2)：全仓库最常见的一行式规则，`-o axpy_cuda` 决定产物名。克隆时只改文件名列表与目标名。
- [CoMem_AXPY/test.sh:1-4](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh#L1-L4)：四档规模的 nvprof 扫描。u1-l2 说过：test.sh 就是基准的实验设计文档。你的 test.sh 要扫的自变量从「规模 n」换成「stride」。

#### 4.1.4 代码实践

**实践 1：克隆模板并跑通空壳**

1. **实践目标**：验证「改名清单」完整，得到一个能编译运行的空白基准。
2. **操作步骤**：
   - 在仓库**之外**的目录（例如 `~/mybench/StridedSaxpy`，切勿写回仓库）复制五个文件，按七步清单改名：`axpy.h` → `saxpy_strided.h`，函数 `axpy_cuda` → `saxpy_strided_cuda`，依此类推。
   - 先不做任何语义修改，直接 `make` 并运行 `./saxpy_strided_cuda 1024000`。
3. **需要观察的现象**：编译一次通过；输出格式与原版一致（数值也应一致，因为逻辑没改）。
4. **预期结果**：输出形如 `saxpy_strided(1024000): checksum: 36.x, time: xx.xxms`（checksum 仍是原版的结构性数值，4.3 节会修正它）。若链接报 `undefined reference`，说明第 2 步改名有漏网——用 `grep -rn axpy .` 找出残留旧名。

**待本地验证**：需要一台装有 CUDA 工具链的机器；无 GPU 环境下 `make` 可以通过（编译无需 GPU，u1-l2），但运行必须有 GPU。

#### 4.1.5 小练习与答案

**练习 1**：克隆后你只把 `axpy.h` 里的 `REAL` 改成 `float`，`axpy_cuda.c` 里保持 `double`，会发生什么？
**答案**：编译和链接都通过——`extern "C"` 只对齐符号名不做类型检查，`saxpy_strided_cuda` 的签名在两边按各自宏展开。运行时 host 按 `double`（8 字节）分配和串行计算，kernel 按 `float`（4 字节）解释同一段内存，读出的是错位的垃圾值，check 显著非零。这就是「接口契约靠人维持」的含义：精度切换必须两处同步。

**练习 2**：把可执行文件从 `saxpy_strided_cuda` 改名为 `ssaxpy`，需要同步修改哪些文件？
**答案**：Makefile 的 `-o` 目标名（对应模板 [CoMem_AXPY/Makefile:2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/Makefile#L2)）和 test.sh 中的 `./可执行文件名`（对应 [CoMem_AXPY/test.sh:1](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh#L1)）。漏改 Makefile 则 `make` 产出旧名二进制、test.sh 找不到文件；漏改 test.sh 同理。

**练习 3**：`VEC_LEN`（[CoMem_AXPY/axpy_cuda.c:22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L22)）和命令行 `argv[1]` 是什么关系？
**答案**：`VEC_LEN=1024000` 是默认规模，`argc >= 2` 时被 `atoi(argv[1])` 覆盖（L70-72）。这个「默认值 + 可覆盖」的小模式正是 test.sh 能做规模扫描的机制——你的 strided-saxpy 要把它扩展成 stride 与规模两个参数。

### 4.2 对照实验设计：把一个性能假设变成一组 kernel

#### 4.2.1 概念说明

微基准的科学性全在**对照**二字：你观察到的任何性能差异，必须能归因到你想研究的那一个因素，而不是别的什么东西混了进来。设计流程是三步翻译：

```text
性能直觉：「跨步访问显存会变慢」
    ↓ 翻译成可控制的变量
单一变量：warp 集体访问地址的跨步 k（其余一切不变）
    ↓ 翻译成代码
两个 kernel：saxpy_strided_kernel(stride=1) 与 saxpy_strided_kernel(stride=k)
```

模板 CoMem_AXPY 在这里给出了一个**反面教材 + 两个正面模式**：

- **反面**：它把 4 个 kernel 串在同一份 `d_y` 上顺序执行（[axpy_cudakernel.cu:61-68](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L61-L68)）。每个 kernel 读写的是前一个 kernel 的输出，且 `block`/`cyclic` 版本的线程配置也不同（`(n+255)/256` vs `1024`），单独归因任何一个 kernel 的耗时都做不到——u2-l2 指出过验证单个 kernel 必须隔离运行。
- **正面模式一（命令行选 kernel）**：Shuffle 基准用 `whichKernel` 参数从命令行选择要运行的 kernel，默认值 3（[Shuffle/cuda_shuffle/reduction.cpp:424-438](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L424-L438)），进入一个 switch 分发（[reduction_kernel.cu:410-421](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L410-L421)）。**一次只运行一个 kernel**，天然隔离。
- **正面模式二（脚本扫自变量）**：UniMem 的 test.sh 把 stride 从 1 逐档扫到 134217728 共 28 档（[UniMem/test.sh:1-28](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/test.sh#L1-L28)），每组参数一次独立进程。进程级隔离是最彻底的隔离。

我们的 strided-saxpy 采用「模式一 + 模式二」的组合：kernel 选择用命令行参数，stride 扫描交给 test.sh。

#### 4.2.2 核心流程

**先做理论预测，再写代码。** 设元素为 8 字节的 double，warp 内 32 个线程按 `i * stride` 访问 `x`：

- 一次 warp 加载指令真正需要的字节数固定为 \(32 \times 8 = 256\) 字节；
- 32 个地址的跨度为 \((31k+1) \times 8\) 字节，每个地址落入一个 32 字节扇区，故触碰的扇区数为

\[ S(k) = \min\left(\left\lceil \frac{(31k+1)\times 8}{32} \right\rceil,\ 32\right) \]

（上限 32 是因为每个线程至多把 1 个扇区变成「必须搬运」）；
- 加载效率（即 nvprof 时代的 `gld_efficiency` 口径）为

\[ \eta(k) = \frac{256}{32 \times S(k)} \]

| stride k | 地址跨度 | 触碰扇区 S(k) | 理论效率 η(k) |
|---|---|---|---|
| 1 | 256 B | 8 | 100% |
| 2 | 504 B | 16 | 50% |
| 4 | 1000 B | 32 | 25% |
| ≥ 4 | ≥ 1000 B | 32（封顶） | 25% |

这个表格是实验的**假设**：写完代码后用 `--metrics` 检验实测效率是否符合，再讨论时间与效率的偏差来自哪里（L2 缓存、DRAM 预取、TLB）。注意 25% 是 8 字节元素下的下限——扇区里剩余的 24 字节全被浪费；这正是 u4-l2 「利用率只给出劣化下界，实测受缓存调制」的量化版本。

**工作量控制是本设计的第二个关键。** 若固定数组长度 n、令 `m = n/stride`，stride 越大 m 越小——访问模式和**工作量**两个变量同时变化，时间差异无法归因（与 u4-l2 批评 block 分布「混杂并行度变量」同构）。正确做法是**固定 m**：两个 kernel 都处理 m 个元素、做 m 次乘加、写 m 个 `y[i]`，唯一差别是读 `x` 的地址模式。`x` 按 `n = m × k_max` 分配即可。

**strided-saxpy 的完整数据流**：

```text
main(argc: stride, m)
 ├─ 分配 x[n=m×k_max]、y0[m]、y_ref[m]、y_cuda[m]，固定种子初始化
 ├─ 路径 A（校验，4.3 节）：串行 saxpy_strided(x,y_ref,m,stride,a)
 │                            与 GPU 单次运行对比 → 期望 ~1e-16
 └─ 路径 B（计时）：warmup → 10 次 saxpy_strided_cuda 取平均
包装函数 saxpy_strided_cuda（每次调用）
 ├─ cudaMalloc d_x/d_y（+暖身用的 d_scratch）
 ├─ cudaMemcpy H2D
 ├─ warmingup kernel（写在 d_scratch 上，不污染 d_y）
 ├─ 被测 kernel <<<（m+255)/256, 256>>>
 ├─ cudaMemcpy D2H → cudaFree
```

#### 4.2.3 源码精读

**参照系 kernel**。`1perThread` 就是我们要的 stride=1 版本——下标恰好等于全局线程编号，warp 地址连续，100% 合并（[CoMem_AXPY/axpy_cudakernel.cu:16-22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L16-L22)）。把它加一个 `stride` 参数就是我们的被测 kernel，stride=1 时逐字退化回原版——「优化版是反模式的特例」这种包含关系，是对照实验里最干净的参照系设计。

**kernel 选择参数的仓库先例**。`whichKernel` 默认 3，可被命令行 `kernel=` 覆盖（[Shuffle/cuda_shuffle/reduction.cpp:424-438](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L424-L438)），再经 switch 分发到 reduce0~reduce6（[Shuffle/cuda_shuffle/reduction_kernel.cu:410-421](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L410-L421)）。u6-l1 也提醒过这个机制的坑：test.sh 没有传 `kernel=`，于是归档数据全是默认 kernel 3 的——你的 test.sh 必须显式传参，别让默认值悄悄决定你测的是什么。

**stride 扫描的仓库先例**。UniMem 的 test.sh 逐档放大 stride（[UniMem/test.sh:1-6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/test.sh#L1-L6) 从 1 到 64，直至 L26-28 的千万级），每行一次独立进程。我们的扫描档位取 {1, 2, 4, 8, 32}——按理论表，4 之后效率封顶，重点是验证拐点而非堆档位。

**被测 kernel（示例代码，非仓库原有）**：

```cuda
// 示例代码：strided-saxpy 的被测 kernel，改写自 axpy_cudakernel_1perThread
__global__
void saxpy_strided_kernel(REAL* x, REAL* y, int m, int stride, REAL a)
{
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i < m) y[i] += a * x[i * stride];   // stride=1 时与 1perThread 完全等价
}
```

注意守卫只需 `i < m` 一个：令 \(m = \lfloor n/k \rfloor\)，则 \((m-1)\times k \le n - k < n\)，`i*stride` 自动不越界。若改用向上取整就必须补 `i*stride < n`（u2-l1 的边界检查原理）。

**共享的串行基线（示例代码，非仓库原有）**：

```c
/* 示例代码：stride 作参数，stride=1 与 stride=k 共用同一基线 */
void saxpy_strided(REAL* x, REAL* y, int m, int stride, REAL a) {
    for (int i = 0; i < m; i++) y[i] += a * x[i * stride];
}
```

「共享同一基线」不是复制两份代码，而是把变量提为参数——一份代码覆盖所有档位，这本身就在防止两份拷贝悄悄分叉。

#### 4.2.4 代码实践

**实践 2：写出两个对照 kernel 并接线**

1. **实践目标**：让 `stride=1` 与 `stride=k` 在同一程序内可切换，且除访问模式外一切相同。
2. **操作步骤**：
   - 在 `.cu` 中写入 `saxpy_strided_kernel`（上文示例代码）与作用在 scratch 缓冲上的 `saxpy_strided_warmingup`（函数体相同，见 4.3.3）。
   - 包装函数里启动配置用 `<<<(m+255)/256, 256>>>`，向上取整技巧与模板一致（对照 [axpy_cudakernel.cu:63](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L63)）。
   - main 按 `argv` 读入 stride 与 m，`n = m * stride` 分配 x。
   - 暂时先只做路径 B（计时），跑通 `./ssaxpy 1 1024000` 与 `./ssaxpy 4 1024000`。
3. **需要观察的现象**：两次运行的 kernel 都正确完成 m 个元素更新；nvprof 概览中每次调用应只看到 2 个 kernel（warmingup + 被测）。
4. **预期结果**：stride=4 的 kernel 时间高于 stride=1（幅度待本地验证）；差异方向先记下，4.3 节补齐指标与校验后再下结论。

#### 4.2.5 小练习与答案

**练习 1**：如果图省事固定 n、令 `m = n/stride`，实验混入了什么第二变量？后果是什么？
**答案**：混入了工作量——stride 翻倍则 m 减半，乘加次数、y 写入量、kernel 时长都会随之下降。此时「stride=32 比 stride=1 快」可能只是因为做的事变少了，访问模式的效应和工作量的效应无法拆分。固定 m 才能保证时间差异只反映访问模式。

**练习 2**：为什么 `y` 的写入在本实验中是「内置于实验的负对照」？
**答案**：两个 kernel 写 `y[i]` 的方式完全相同（i 连续、天然合并），理论 `gst_efficiency` 恒约 100%。若实测中 `gst_efficiency` 也随 stride 漂移，说明实验被污染（例如布局被改、kernel 没选对），可以立即发现。用一个不变的量给实验「打底」，是廉价的自我检查。

**练习 3**：理论上 stride 从 4 增到 32，加载效率都不再下降（都是 25%），那时间还会变吗？
**答案**：效率口径封顶在 25%，但其他效应仍在变化：更大的 stride 意味着 warp 地址跨度更大（32 倍 stride 时单 warp 跨 8KB），TLB 页表行走、DRAM 行切换的代价上升；同时总活跃数据集相同（都采样同一段 x）。所以时间可能在效率不变的情况下继续缓慢变差——这正是「效率只给下界」的含义。具体幅度待本地验证。

### 4.3 测量与校验：让数字站得住脚

#### 4.3.1 概念说明

一个结论要能写进实验报告，需要三层证据，缺一不可：

| 层 | 工具 | 回答的问题 | 陷阱 |
|---|---|---|---|
| ① wall time | 程序打印的 time | 端到端大概多慢 | 混入 cudaMalloc、拷贝、首次上下文创建（u1-l4：56.80ms 对 9.55µs） |
| ② kernel time | nvprof 概览 GPU activities | kernel 本身多慢 | 仍只是时间，解释不了「为什么」 |
| ③ 硬件计数器 | `nvprof --metrics` | 慢在哪个机制上 | 指标口径要与理论模型对齐（如扇区口径的效率） |

只用第①层下结论是初学者最常见的错误：对一个 kernel 占 wall time 不到 1% 的基准，wall time 的变化几乎全是噪声。第③层是「归因」的关键——我们 predicted η(k)，就用 `gld_efficiency` 来对表，实测与理论吻合后，时间差异才有了解释。

校验则是另一条独立防线，核心原则：**check 的语义必须是明确的**。模板的 check 打印出 36~39 这种数（u2-l4），它能证明「没有算出垃圾」，但不能证明「误差在浮点噪声内」。原因在于 `y += a*x` 是**非幂等**操作：GPU 路径被重复执行（warmup + 10 轮计时 + 内部多 kernel），串行只跑一次，两边的「执行次数」这个隐藏变量破坏了对比。自己设计基准时，应当把校验路径与计时路径分开，让校验回到「单次对单次」。

#### 4.3.2 核心流程

**校验/计时分离**的设计决策链：

```text
观察：y += a*x 非幂等，重复执行 ≠ 单次执行
    ↓ 若坚持在同一份数据上边计时边校验
checksum = 结构性叠加值（模板的做法，assert 被注释掉）
    ↓ 改为两条路径
路径 A（校验）：y_ref 与 y_cuda 从同一初值出发，各执行一次，check → 期望 1e-16 量级
路径 B（计时）：warmup + 10 次平均，结果不参与校验
    ↓ warmup 怎么办（它也会污染 d_y）
让 warmingup kernel 写在 scratch 缓冲上：同代价、不碰 d_y
```

**warmup 的位置**也要想清楚：模板把 warmingup 放在包装函数内部、计时窗口之外（[axpy_cudakernel.cu:61-62](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L61-L62)），10 轮计时中每一轮都重新 warmup——这没问题但偏保守；更常见的是循环前单独 warmup 一次。两种都合法，关键是**写进报告说明白**。

#### 4.3.3 源码精读

**计时三件套**（照抄层）：

- [CoMem_AXPY/axpy_cuda.c:14-18](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L14-L18)：`read_timer_ms` 用 `ftime` 取毫秒墙钟。
- [CoMem_AXPY/axpy_cuda.c:85-89](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L85-L89)：10 次循环除以 `num_runs` 取平均——多轮平均压制单次抖动。
- warmup 的必要性来自首轮的一次性开销（上下文创建、模块加载，u1-l4），与多轮平均配合才能得到稳定的均值。

**校验样本**：

- [CoMem_AXPY/axpy_cuda.c:51-60](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L51-L60)：`check` 累计 `|A[i]-B[i]|` 与 `|B[i]|` 之比值，是与规模无关的相对 L1 误差——这个函数本身设计得很好，照抄。
- [CoMem_AXPY/axpy_cuda.c:91-93](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L91-L93)：`printf` 之后那行被注释的 `assert`——作者明知 checksum 结构性地大，阈值判定形同虚设。你的新基准用「校验/计时分离」让这个 assert 可以重新启用。

**不污染 d_y 的 warmup（示例代码，非仓库原有）**：

```cuda
// 示例代码：与被测 kernel 同代价，但把结果写进 scratch，d_y 保持初值
__global__
void saxpy_strided_warmingup(REAL* x, REAL* y_scratch, int m, int stride, REAL a)
{
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i < m) y_scratch[i] += a * x[i * stride];
}
```

包装函数里为它另 `cudaMalloc` 一块 `d_scratch` 并在末尾一并释放。这样路径 A 中 GPU 侧对 `y_cuda` 的净效果恰好是「被测 kernel 执行一次」，与串行单次严格对齐（u2-l4 的「两侧调用次数对称」原则）。

**main 的两条路径（示例代码，非仓库原有）**：

```c
/* 示例代码：校验与计时分离的 main 骨架 */
int stride = 1, m = 1024000;
if (argc >= 2) stride = atoi(argv[1]);
if (argc >= 3) m      = atoi(argv[2]);
int n = m * stride;                       /* x 的长度随 stride 扩大，m 恒定 */

srand48(1 << 12);
init(x, n);  init(y0, m);                 /* 固定种子，可复现 */

/* 路径 A：单次对单次，check 才有严格语义 */
memcpy(y_ref,  y0, m * sizeof(REAL));
memcpy(y_cuda, y0, m * sizeof(REAL));
saxpy_strided(x, y_ref, m, stride, a);
saxpy_strided_cuda(x, y_cuda, m, stride, a);      /* 内部 warmup 走 scratch */
printf("stride=%d check: %g\n", stride, check(y_cuda, y_ref, m));

/* 路径 B：warmup 已在首次调用中完成，10 次平均计时 */
double elapsed = read_timer_ms();
for (int i = 0; i < num_runs; i++) saxpy_strided_cuda(x, y_cuda, m, stride, a);
elapsed = (read_timer_ms() - elapsed) / num_runs;
printf("saxpy_strided(m=%d, stride=%d): checksum: %g, time: %0.2fms\n",
       m, stride, check(y_cuda, y_ref, m), elapsed);   /* 此处 checksum 仅供监测 */
```

**`--metrics` 的用法先例**：

- [WarpDivRedux/test.sh:1-2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/test.sh#L1-L2)：同一规模先跑一遍 `nvprof`（拿时间拆解）、再跑一遍 `nvprof --metrics branch_efficiency`（拿计数器），交替进行。WarpDivRedux 量化的是分支发散所以选 `branch_efficiency`；我们量化访存合并，对应选 `gld_efficiency`（与 `gst_efficiency` 一起采，后者是 4.2.5 练习 2 说的负对照）。指标要与假设同构，这是选指标的一般原则。新工具链上 nvprof 已被 nsys/ncu 取代（u1-l4），ncu 的 memory workload 分析可拿到同口径的扇区数据，报告里注明工具与版本即可。

#### 4.3.4 代码实践

**实践 3：补齐校验路径与指标采集**

1. **实践目标**：让 check 输出回到浮点噪声量级；把 nvprof 三层证据全部拿到手。
2. **操作步骤**：
   - 按 4.3.3 的示例代码改造 main 与包装函数（warmingup 写 scratch）。
   - test.sh（示例代码，非仓库原有）：

     ```bash
     for s in 1 2 4 8 32; do
         nvprof ./ssaxpy $s 1024000
         nvprof --metrics gld_efficiency,gst_efficiency ./ssaxpy $s 1024000
     done
     ```

   - 逐档运行，记录：程序打印的 check 值、GPU activities 中被测 kernel 的耗时、两个效率指标。
3. **需要观察的现象**：路径 A 打印的 check 应为 `1e-16`~`1e-15` 量级（double 精度的浮点噪声）；`gld_efficiency` 随 stride 的变化应接近理论表（100% → 50% → 25% 后走平）；`gst_efficiency` 各档位基本不变。
4. **预期结果**：理论效率与实测效率的偏差应在几个百分点内；kernel 时间随 stride 上升但幅度通常小于效率比的倒数（缓存与预取的缓和作用）。**待本地验证**——具体倍数依赖 GPU 架构与代际，这正是报告要讨论的部分。若 check 出现 `1e-2` 以上量级，先查 kernel 选择参数与 scratch 是否接对，再查 m/stride 的边界。

#### 4.3.5 小练习与答案

**练习 1**：为什么模板的 checksum 是 36~39，而你的新基准可以做到 1e-16？
**答案**：模板中 GPU 路径对 y 的净修改次数 = 10 轮 ×（warmingup + 4 个 kernel）= 50 次，串行只修改 1 次，49 次额外叠加造成结构性差异；新设计里 warmingup 写 scratch、被测 kernel 单次执行，两侧严格「一次对一次」，check 度量的才是真正的浮点舍入误差。

**练习 2**：把 warmingup 放在包装函数内（每轮计时都 warmup 一次，模板做法）与放在计时循环前（只 warmup 一次）各有什么代价？
**答案**：模板做法让每轮都包含一次预热 kernel 的执行时间（好在它在计时窗口外，不影响计时读数，但拉长了程序总时长）；循环前预热更高效，但要确认首次调用确实完成了你想要的预热（上下文创建、模块加载）。两者对计时读数本身都无害，差别在效率与语义清晰度——无论选哪种，报告里写明即可。

**练习 3**：报告里为什么要附 nvprof 的 kernel 行而不是只给程序 time？
**答案**：程序 time 是包装函数全流程的平均墙钟，含 cudaMalloc、H2D/D2H 拷贝与首次调用的上下文创建（u1-l4：56.80ms 中 kernel 仅约 9.55µs）；对这个访存受限的小 kernel，wall time 的档间差异可能被这些固定开销淹没甚至反转。kernel 行直接度量被测对象，才支撑「跨步访问降低合并效率」这类归因结论。

## 5. 综合实践

把三个模块合起来，交付完整的 strided-saxpy 微基准并写一份实验报告。

**交付物清单**（放在仓库外的独立目录，例如 `~/mybench/StridedSaxpy/`）：

```text
StridedSaxpy/
 ├── saxpy_strided.h          # REAL（两处同步！）+ extern "C" 声明
 ├── saxpy_strided_cuda.c     # read_timer_ms/init/check 照抄 + saxpy_strided 串行基线
 │                            #   + 双路径 main（校验一次对一次，计时 10 轮平均）
 ├── saxpy_strided_cudakernel.cu  # warmingup(scratch) + saxpy_strided_kernel + 包装函数
 ├── Makefile                 # nvcc -o ssaxpy saxpy_strided_cuda.c saxpy_strided_cudakernel.cu
 └── test.sh                  # stride ∈ {1,2,4,8,32} × {nvprof, nvprof --metrics gld_efficiency,gst_efficiency}
```

**实验报告模板**（每档 stride 一行）：

| stride | m | check（路径 A） | gld_efficiency | gst_efficiency | kernel 时间 | wall time |
|---|---|---|---|---|---|---|
| 1 | 1024000 | ~1e-16 | 待验证 | 待验证 | 待验证 | 待验证 |
| 2 | 同上 | … | … | … | … | … |
| 4 / 8 / 32 | … | … | … | … | … | … |

报告需回答四个问题：

1. 实测 `gld_efficiency` 与理论表 \(\eta(k)\) 的吻合度如何？偏差在哪些档位、可能来自哪里（缓存行口径 vs 扇区口径、指标定义差异）？
2. kernel 时间的劣化倍数与效率倒数（1×、2×、4×）相比是更大还是更小？用缓存/预取解释。
3. `gst_efficiency` 是否如负对照预期保持平稳？若否，实验哪里被污染了？
4. 把你的结果与仓库归档（如 `BankRedux/sum_cuda.output.carina.txt`）的体例对齐：机器、CUDA 版本、nvprof 版本各占一行——u6-l4 会展开讲跨平台比较的纪律。

**自查清单**（提交前逐项打勾）：

- [ ] `REAL` 在 `.h` 与 `.c` 两处一致；
- [ ] 固定 `m`，只让 stride 变化；
- [ ] warmingup 写 scratch，不污染被测数据；
- [ ] check 是「一次对一次」的相对误差，量级 ~1e-16；
- [ ] 计时为 warmup + 10 次平均；
- [ ] test.sh 显式传 stride（不依赖默认值）；
- [ ] 结论由 kernel 时间 + 效率指标共同支撑，wall time 只作参考；
- [ ] 报告注明软硬件环境与工具版本。

## 6. 本讲小结

- 微基准代码分两层：**脚手架层照抄**（计时、初始化、check、包装函数、Makefile、test.sh），**被测变量层重写**（kernel、启动配置、串行循环体、接口签名）；判据是「我想研究的因素在这行代码里吗」。
- 克隆模板的七步清单中，改名同步点（头文件、符号、Makefile 目标、脚本引用）是最易漏的环节；`REAL` 双定义与 `extern "C"` 无类型检查两个陷阱要求接口契约靠人来维持。
- 对照实验的设计核心是**单一变量**：性能直觉 → 可控变量 → 一组 kernel；固定 m 使工作量恒定，让时间差异只反映访问模式；命令行选 kernel（Shuffle 的 `whichKernel` 模式）+ 脚本扫自变量（UniMem 的 test.sh 模式）优于模板「多 kernel 串在同一份状态上」的做法。
- 结论需要三层证据：wall time → nvprof kernel time → `--metrics` 硬件计数器；指标要与理论假设同构（我们预测扇区口径效率，就去采 `gld_efficiency`，并用恒定的 `gst_efficiency` 做负对照）。
- 校验与计时必须分离：非幂等操作在「重复执行的 GPU 路径」与「单次执行的串行基线」之间制造结构性 checksum（模板的 36~39）；warmup 写 scratch、被测 kernel 单次执行，让 check 回到 1e-16 的浮点噪声语义。
- 先写理论预测（\(\eta(k)\) 表）再写代码，实测与预测的偏差本身就是一个可讨论的实验结果。

## 7. 下一步学习建议

- **u6-l3（Common 依赖与 CUDA Samples 工程化）**：如果你的新基准想用计时器、错误检查等现成设施，下一讲分析 `Common/` 目录的 helper 头文件如何被 GSOverlap、TaskGraph 等基准复用，以及如何把依赖最小化。
- **u6-l4（实验方法论与结果解读）**：本讲的报告模板只是起点；下一讲系统讨论 warmup 的必要性、计时口径、跨平台（Carina/Fornax）结果的比较纪律，帮你把 strided-saxpy 的报告写成规范的基准文档。
- 继续阅读源码的建议：精读 [Shuffle/cuda_shuffle/reduction.cpp](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp) 的命令行解析与 `getNumBlocksAndThreads`，它是仓库里最完整的「一个程序承载多个被测 kernel」的工程化样例；再对照 u6-l1 的七步优化阶梯，思考你的 strided-saxpy 还能叠加哪些实现级优化（向量化 `double4` 读取、循环展开）作为第二层对照。

---

*本讲覆盖的最小模块：模板骨架复用、对照实验设计、测量与校验。*

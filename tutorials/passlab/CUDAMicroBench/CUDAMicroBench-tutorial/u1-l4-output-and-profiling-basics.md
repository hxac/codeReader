# 读懂基准输出与性能分析入门：nvprof 与结果文件

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂任何一个微基准打印的标准输出行，例如 `sum(102400): checksum: 0.0820312, time: 56.80ms`，并能准确说出 `checksum` 和 `time` 两个数字在源码里是怎么算出来的、各自包含了哪些开销。
2. 使用 `nvprof` 把一次运行拆解成 **GPU activities**（kernel 与设备端拷贝）和 **API calls**（主机端 CUDA 函数）两张表，并用 `Calls` 列反向核对程序结构。
3. 使用 `nvprof --metrics branch_efficiency` 采集硬件计数器指标，理解它与墙钟时间的互补关系。
4. 从仓库自带的 `*.output.carina.txt` / `*.output.fornax.txt` 结果文件中提取 GPU 时间信息，作为没有 GPU 环境时的"云实验数据"。

本讲是单元一的收尾：u1-l2 学了"怎么编译运行"，u1-l3 学了"代码骨架长什么样"，本讲解决"跑完之后输出到底说了什么、怎么看得更细"。

## 2. 前置知识

用通俗语言解释几个本讲要用到的概念。

- **墙钟时间（wall time）与 GPU 时间**：墙钟时间是从程序外面用秒表量出来的真实经过时间，包含 CPU 侧做的一切事（分配显存、发起拷贝、启动 kernel、等结果）。GPU 时间是 kernel 真正在 GPU 上执行的时间。u1-l3 已经给过结论：host 程序打印的 time 是墙钟时间，**大于**纯 kernel 时间。本讲会用真实数据量化这个差距。
- **性能分析器（profiler）**：一个"旁观者"程序，它启动你的程序，并在运行过程中记录每个 kernel 的起止时间、每次显存拷贝的耗时、每个 CUDA API 调用的耗时。`nvprof` 是 CUDA 工具链自带的命令行性能分析器。注意：CUDA 11 起 `nvprof` 已被逐步弃用，由 Nsight Systems（`nsys`，时间线概览）和 Nsight Compute（`ncu`，kernel 级指标）取代；较新的工具链里可能没有 `nvprof` 命令。本仓库所有 `test.sh` 用的都是 `nvprof`（见 u1-l2），若你的环境没有它，可用 `nsys profile ./sum_cuda 102400` 得到类似概览，指标采集则用 `ncu`（指标名可能不同，需 `--query-metrics` 查询，结果待本地验证）。
- **stdout 与 stderr**：C 程序有两个输出流。`printf` 走 stdout；`fprintf(stderr, ...)` 走 stderr。直接在终端运行时两者混在一起；但重定向到文件时 stdout 变成全缓冲（进程退出才刷新）、stderr 无缓冲，所以**转录文件里两流的先后顺序与真实时间顺序可能不一致**。这解释了后面结果文件里一些"错位"的排版。
- **硬件计数器（metrics）**：GPU 硬件里有一组计数器，统计分支指令、cache 命中、内存事务等事件。`nvprof --metrics 指标名` 让分析器在 kernel 运行时读取这些计数器，输出与时间无关的"行为指标"。时间告诉你"快不快"，计数器告诉你"为什么快/慢"。
- **Carina 与 Fornax**：仓库作者采集实验结果所用的两台 HPC 集群（输出文件第一行有 `Test on Carina`、提示符里有 `cci-carina` 字样）。不同机器的 GPU 架构、驱动、工具链都不同，数字只能同机对比。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [BankRedux/sum_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c) | host 主程序：计时、串行基线、打印 `checksum` 与 `time` 输出行 |
| [BankRedux/sum_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu) | 三个 kernel（`sum_warmingup`、`sum_cudakernel`、`sum_cudakernel_bc`）与包装函数 `sum_cuda`，是 nvprof 表格里出现的三个名字的出处 |
| [BankRedux/sum.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum.h) | 接口头文件：`REAL`、`ThreadsPerBlock=256`、`VEC_LEN=1024000` 宏 |
| [BankRedux/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/Makefile) | 一行式编译规则，产出可执行文件 `sum_cuda` |
| [BankRedux/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/test.sh) | BankRedux 的实验设计：4 个规模下逐个运行 `nvprof ./sum_cuda <n>` |
| [BankRedux/sum_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt) | 作者在 Carina 集群上执行 `sh test.sh` 的终端转录，本讲最重要的"参考答案" |
| [WarpDivRedux/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/test.sh) | 仓库中 `nvprof --metrics branch_efficiency` 的出处（每个规模先 nvprof 再加指标采集各跑一遍） |
| [README.md](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md) | 项目总说明：Experiment 一节指明"看各目录 Makefile 编译、用 .sh 文件执行" |

## 4. 核心概念与源码讲解

### 4.1 基准的标准输出：checksum 与 time 从哪里来

#### 4.1.1 概念说明

每个微基准跑完都会打印一行类似 `sum(102400): checksum: 0.0820312, time: 56.80ms` 的结果。这行输出携带两个信息：

- **checksum（校验和）**：GPU 结果与串行参考结果之间的差值，用来回答"算得对不对"。
- **time（平均耗时）**：`num_runs` 次完整调用取平均后的墙钟时间，用来粗看"跑得快不快"。

读懂这一行是读懂所有基准的第一步——因为它就是你亲手运行程序时唯一得到的反末。而要真正读懂它，必须回到源码看这两个数是怎么算出来的。

#### 4.1.2 核心流程

以 BankRedux 为例，输出行的产生流程：

```text
main
 ├── 读命令行参数 n（缺省用 VEC_LEN=1024000）
 ├── init(x, n)                用固定种子 srand48(1<<12) 填充随机向量
 ├── answer = sum(n, x)        串行基线（计时窗口之外）
 ├── elapsed = read_timer_ms() ── 计时开始 ──
 ├── 循环 10 次调用 sum_cuda(n, x, result_cuda)
 │     每次内部：cudaMalloc ×2 → H2D 拷贝 → 3 个 kernel（各自同步）→ D2H 拷贝 → cudaFree ×2
 ├── 把 result_cuda[1..块数-1] 累加回 result_cuda[0]（块间归并）
 ├── elapsed = (read_timer_ms() - elapsed) / 10 ── 计时结束 ──
 └── printf("sum(%d): checksum: %g, time: %0.2fms\n",
             n, result_cuda[0] - answer, elapsed)
```

两个关键算式：

\[ \text{checksum} = \text{result\_cuda}[0] - \text{answer} \]

\[ \text{time} = \frac{t_{\text{结束}} - t_{\text{开始}}}{10} \]

注意计时窗口覆盖的是 **10 次完整的 `sum_cuda` 调用加最后的块间归并**——显存分配、两个方向的拷贝、3 个 kernel、释放，全部算在里面。这就是"墙钟时间 > kernel 时间"的直接原因。

#### 4.1.3 源码精读

**输出行的唯一出处**——[BankRedux/sum_cuda.c:96-L96](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c#L96-L96)：打印 `n`、`result_cuda[0]-answer`（即 checksum）和 `elapsed`（即 time）。格式符 `%g` 与 `%0.2f` 解释了为什么 checksum 有时是 `0.0820312` 有时是 `2`（有效数字自适应），time 恒为两位小数。

**串行基线**——[BankRedux/sum_cuda.c:44-L51](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c#L44-L51)：`sum` 函数用 float 顺序累加得到 `answer`，在计时窗口之前完成（[第 83-84 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c#L83-L84)）。GPU 的树状归约与 CPU 的顺序累加**求和顺序不同**，而浮点加法不满足结合律：

\[ (a + b) + c \;\neq\; a + (b + c) \quad \text{（float 一般不完全相等）} \]

所以两者结果本就会有微小差异，这正是 checksum 不为零的第一来源。

**计时逻辑**——[BankRedux/sum_cuda.c:87-L95](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c#L87-L95)：`num_runs = 10`；[第 14-18 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c#L14-L18) 的 `read_timer_ms` 用 `ftime` 取毫秒级墙钟。多轮取平均是为了平滑抖动（u1-l3 讲过五阶段骨架，此处是它的具体实现）。

**一个值得批判性阅读的细节**——[BankRedux/sum_cuda.c:92-L93](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c#L92-L93)：块间归并循环的上界用的是编译期常量 `VEC_LEN`（1024000，来自 [BankRedux/sum.h:8-L8](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum.h#L8-L8)）而不是命令行传入的 `n`。而 GPU 每次只写入 `(n+255)/256` 个块结果（见 [BankRedux/sum_cudakernel.cu:69-L72](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L69-L72)）。当 `test.sh` 传入 `n=102400` 时只写了 400 个块，归并却累加 4000 个元素——后 3600 个是 `malloc` 出来、从未初始化的内存。因此**当 n < VEC_LEN 时，打印出的 checksum 混入了未定义的垃圾值，只能当"结果大致对不对"的探针，不能当严格的误差度量**（两种因素的相对占比待本地验证）。另外 [第 54-63 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c#L54-L63) 定义了归一化误差函数 `check`（\(\frac{\sum|A_i-B_i|}{\sum|B_i|}\)），但本文件的 `main` 并没有调用它——这是 u1-l3 五阶段骨架在 BankRedux 里的简化变体。

**程序自己打印的 Usage**——[BankRedux/sum_cuda.c:72-L75](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c#L72-L75)：无论有没有传参，程序都先向 **stderr** 打一行 `Usage: sum <n>`，然后才解析 `argv[1]`。记住这行来自 stderr，后面读结果文件时就不会被它的位置迷惑。

#### 4.1.4 代码实践

1. **实践目标**：把一行输出与源码逐字段对上。
2. **操作步骤**：
   - 进入 `BankRedux` 目录，`make` 编译（规则见 [BankRedux/Makefile:2-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/Makefile#L2-L2)，产物为 `sum_cuda`）。
   - 运行 `./sum_cuda 102400`，观察终端上的两行输出（stderr 的 Usage + stdout 的结果行）。
   - 再运行 `./sum_cuda`（不带参数），对照 [BankRedux/sum.h:8-L8](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum.h#L8-L8) 确认此时 `n` 取缺省值 1024000。
   - 运行 `./sum_cuda 102400 > out.txt`，然后 `cat out.txt`，观察 Usage 是否出现在文件里（它走 stderr，不会进文件）。
3. **需要观察的现象**：结果行的三个字段随 `n` 变化；Usage 行永远先出现；重定向后文件里只有结果行。
4. **预期结果**：每次运行打印一行 `sum(<n>): checksum: <某小数>, time: <某值>ms`。具体数值与本机 GPU、驱动相关，且 checksum 含 4.1.3 所述的未初始化内存因素，**待本地验证**。
5. 重复运行同一命令两三次，记录 time 的波动范围，体会为什么源码要取 10 次平均。

#### 4.1.5 小练习与答案

**练习 1**：输出行里的 `time: 56.80ms`（Carina 上 n=102400 的记录）中，三个 kernel 的执行时间大约占多少？
**答案**：从 [结果文件](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L12-L16) 可算：三个 kernel 合计 30.176+31.487+33.856 ≈ 95.5µs（10 次调用总计），平均到每次运行约 9.55µs，占 56.80ms 的比例不足 0.02%。其余几乎全是 cudaMalloc（首次调用含上下文创建，单次最高 308.16ms，摊到 10 次后每次约 31ms）等 API 开销。

**练习 2**：为什么 `./sum_cuda 102400` 的 checksum 通常不为 0？这是"算错了"吗？
**答案**：不一定是错误。两个来源：(a) GPU 树状归约与 CPU 顺序累加的浮点求和顺序不同，非结合性导致微小差异；(b) 4.1.3 指出的 `VEC_LEN` 越界归并会累加未初始化内存。所以本基准的 checksum 只能作定性探针，与 `n=VEC_LEN` 时的行为对比才有参考意义。

**练习 3**：把 `fprintf(stderr, "Usage...")` 改成 `printf` 会让程序变快还是变慢？
**答案**：都不会有可测影响。这个练习的要点是输出流的选择影响的是**输出的去处与缓冲行为**，不是性能；但它直接决定了 4.4 节结果文件里行的排列顺序。

### 4.2 nvprof 概览：把耗时拆解成 GPU activities 与 API calls

#### 4.2.1 概念说明

`nvprof` 不带任何选项直接运行程序时，输出一张**汇总表**，分两段：

- **GPU activities**：发生在 GPU 上的事——每个 kernel、每次设备端内存拷贝（`[CUDA memcpy HtoD]`/`[CUDA memcpy DtoH]`）的耗时。
- **API calls**：发生在主机侧的 CUDA 运行时/驱动 API 函数——`cudaMalloc`、`cudaMemcpy`、`cudaLaunchKernel`、`cudaDeviceSynchronize` 等的耗时。

这正是 4.1 节那个"墙钟时间里到底装了什么"问题的答案工具：程序只告诉你 56.80ms，nvprof 告诉你其中 kernel 只有几微秒、malloc 占了多少毫秒。

#### 4.2.2 核心流程

```text
$ nvprof ./sum_cuda 102400
程序正常执行（stdout/stderr 照常打印）
结束后 nvprof 追加打印：
    Type  Time(%)   Time   Calls   Avg   Min   Max   Name
 GPU activities:  <每个 kernel / memcpy 一行>
      API calls:  <每个主机端 CUDA 函数一行>
```

各列含义：

| 列 | 含义 |
| --- | --- |
| Type | 行的类别：GPU activities 或 API calls |
| Time(%) | 该行耗时在本段（GPU 或 API）总耗时中的百分比 |
| Time / Avg / Min / Max | 总耗时，以及按 Calls 数平均/最小/最大的单次耗时 |
| Calls | 该活动被观测到的次数 |
| Name | kernel 名（含参数签名）或 API 函数名 |

`Calls` 列特别有价值：它可以**反向核对程序结构**。BankRedux 每次 `sum_cuda` 调用启动 3 个 kernel、2 次 malloc、2 次 free、1 次 H2D、1 次 D2H、3 次同步；跑 10 轮后，每个 kernel 应各 10 次、`cudaMalloc` 应 20 次、`cudaDeviceSynchronize` 应 30 次——与实测完全一致才算真正读懂了程序。

#### 4.2.3 源码精读

**实验脚本的全部内容**——[BankRedux/test.sh:1-L4](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/test.sh#L1-L4)：四行命令，分别在 `n = 102400 / 204800 / 409600 / 1024000` 四个规模下运行 `nvprof ./sum_cuda <n>`。README 的 [Experiment 一节](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L110-L111) 明确了这套约定：每个目录看 Makefile 编译、用 .sh 文件执行。也就是说 **test.sh 就是每个基准的实验设计文档**——改规模扫描还是加指标采集，都体现在这几行里。

**三次 kernel 启动的出处**——[BankRedux/sum_cudakernel.cu:65-L70](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L65-L70)：`sum_cuda` 包装函数按顺序启动 `sum_warmingup`、`sum_cudakernel`、`sum_cudakernel_bc` 三个 kernel，每个后面跟一次 `cudaDeviceSynchronize`。这就是 GPU activities 表里三个 kernel 名与 `Calls=10`（10 轮 × 各 1 次）的来源。三个 kernel 的本体分别在 [sum_cudakernel.cu:8-L22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L8-L22)、[第 24-38 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L24-L38)、[第 40-55 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L40-L55)——前两个是同一份无冲突折半归约（一个作 warmup 一个作被测），第三个 `_bc` 是带 bank 冲突的对照版（细节留到 u4-l5，本讲只关心它出现在 nvprof 表里）。

**与实测对照**（[结果文件 GPU activities 段，n=102400](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L12-L16)）：

| GPU activity | Time(%) | 总耗时 | Calls | Avg |
| --- | --- | --- | --- | --- |
| [CUDA memcpy HtoD] | 76.45% | 367.61us | 10 | 36.761us |
| sum_cudakernel_bc | 7.04% | 33.856us | 10 | 3.3850us |
| sum_warmingup | 6.55% | 31.487us | 10 | 3.1480us |
| sum_cudakernel | 6.28% | 30.176us | 10 | 3.0170us |
| [CUDA memcpy DtoH] | 3.68% | 17.696us | 10 | 1.7690us |

而 [API calls 段](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L17-L28) 里 `cudaMalloc` 占 97.85%、总耗时 309.70ms、20 次调用中最大一次 308.16ms——首次分配触发了 CUDA 上下文创建，这个一次性开销被摊进 10 轮平均后，成了程序打印的 `time: 56.80ms` 的主要成分。**两张表合起来，才是一份能下结论的测量。**

#### 4.2.4 代码实践

1. **实践目标**：用 nvprof 验证你对程序结构的预测。
2. **操作步骤**：
   - 编译后运行 `nvprof ./sum_cuda 102400`。
   - 先在纸上写下预测：每个 kernel 的 Calls、HtoD/DtoH 的 Calls、`cudaMalloc`/`cudaFree`/`cudaDeviceSynchronize`/`cudaLaunchKernel` 的 Calls 各是多少（依据 [sum_cudakernel.cu:57-L75](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L57-L75) 与 `num_runs=10`）。
   - 与 nvprof 实际输出的 Calls 列逐项对照。
3. **需要观察的现象**：GPU activities 有 5 行（3 kernel + 2 memcpy）；`cudaLaunchKernel` 与 `cudaDeviceSynchronize` 都是 30 次。
4. **预期结果**：Calls 列应为：每个 kernel 10、HtoD 10、DtoH 10、cudaMalloc 20、cudaFree 20、cudaLaunchKernel 30、cudaDeviceSynchronize 30。kernel 的 Avg 应在微秒量级，远小于程序打印的 time。具体耗时数值与 GPU 相关，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 n=1024000 时 HtoD 的 Time(%) 升到 93.94%（对照 n=102400 时的 76.45%）？
**答案**：输入向量大小与 n 成正比（[sum_cudakernel.cu:63-L63](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L63-L63) 每轮整体 H2D 拷贝一次），而 kernel 时间随 n 增长得更慢（归约的树高只随块内线程数变化，块数增多带来的并行度掩盖了工作量增长），所以拷贝在 GPU 时间里的占比随 n 上升。

**练习 2**：nvprof 里 `sum_warmingup` 和 `sum_cudakernel` 的 Avg 几乎相同（3.148us vs 3.017us），这说明什么？
**答案**：两者的代码逐行相同（对照 [第 8-22 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L8-L22) 与 [第 24-38 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L24-L38)），耗时相近印证了 warmup kernel 的意义：它让设备进入稳定状态（首次启动的冷开销被它吸收），被测 kernel 的 10 次计时更干净。同时它也提示：程序打印的 time 里其实并没有把 warmup 排除在外——每轮 `sum_cuda` 都重新执行这三个 kernel。

### 4.3 nvprof --metrics：采集 branch_efficiency 等硬件计数器

#### 4.3.1 概念说明

时间回答"哪个慢"，计数器回答"为什么慢"。`nvprof --metrics <指标名> ./程序` 会让分析器在 kernel 执行时读取 GPU 硬件计数器，程序结束后按 kernel 打印指标值。

本仓库用到的指标是 `branch_efficiency`（分支效率），定义为一个 kernel 的分支指令中**未发生 warp 内发散**的比例：

\[ \text{branch\_efficiency} = \frac{N_{\text{分支总数}} - N_{\text{发散分支数}}}{N_{\text{分支总数}}} \times 100\% \]

同一 warp 的 32 个线程如果在一个分支语句处走向不同方向，硬件只能先执行一支再执行另一支（发散），吞吐减半。branch_efficiency 越接近 100%，说明分支越"整齐"。它是 WarpDivRedux 基准的核心度量，也可以套用在任何含 `if` 的 kernel 上（比如 BankRedux 的两个归约 kernel）。

#### 4.3.2 核心流程

```text
$ nvprof --metrics branch_efficiency ./sum_cuda 102400
程序正常执行
nvprof 追加打印 "Metric result" 段：每个 kernel 一行，给出该指标的值
```

与 4.2 的概览模式互不替代：概览给时间拆解，`--metrics` 给行为计数；同一个实验里两者各跑一遍（test.sh 正是这么做的）。

#### 4.3.3 源码精读

**仓库中的标准用法**——[WarpDivRedux/test.sh:1-L10](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/test.sh#L1-L10)：对每个规模执行两行——先 `nvprof ./warpDivergenceTest_cuda <n>` 再 `nvprof --metrics branch_efficiency ./warpDivergenceTest_cuda <n>`。[第 2 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/test.sh#L2-L2) 就是 `--metrics` 用法模板。WarpDivRedux 的两个被测 kernel 一个用 `tid%2` 制造发散、一个用算术改写消除分支（深入分析在 u3-l1），因此 branch_efficiency 会拉开明显差距——这是该指标最典型的应用场景。

**BankRedux 里可供统计的分支**——[BankRedux/sum_cudakernel.cu:30-L34](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L30-L34)：`if (cacheIndex < i)` 每步归约都执行一次；[第 46-50 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L46-L50) 的 `_bc` 版本分支条件是 `index < blockDim.x`。虽然 BankRedux 的 `test.sh` 没有采集这个指标，但两个 kernel 的活跃线程划分方式不同，branch_efficiency 理应有所差异——这正好可以作为你第一次使用 `--metrics` 的实验对象。

#### 4.3.4 代码实践

1. **实践目标**：第一次采集硬件计数器指标，并对比同一程序里的多个 kernel。
2. **操作步骤**：
   - 在 BankRedux 目录执行 `nvprof --metrics branch_efficiency ./sum_cuda 102400`。
   - 在输出的 Metric result 段里找到三个 kernel 各自的 branch_efficiency 值。
   - 换 `n=1024000` 再跑一次，观察指标值是否随规模变化。
3. **需要观察的现象**：指标按 kernel 分行报告；`sum_cudakernel` 与 `sum_cudakernel_bc` 的数值差异。
4. **预期结果**：两个归约 kernel 的分支都以"线程编号是否小于阈值"为条件，前几步整个 warp 同进同出、后几步块内只剩极少数线程活跃，branch_efficiency 预计较高但低于 100%，且两个 kernel 不完全相同；具体数值**待本地验证**。若还想看"教科书级"的对比，去 WarpDivRedux 目录按其 test.sh 的方式运行，`warpDivergence` 与 `noWarpDivergence` 两个 kernel 的指标差距会非常直观（深入留到 u3-l1）。
5. 若提示不支持该指标或 nvprof 不存在，改用 `ncu` 并先用 `--query-metrics` 查询可用指标名，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么不用 time 而要用 branch_efficiency 来评价分支发散？
**答案**：kernel 耗时受访存、占用率、时钟频率等众多因素干扰，一次发散的代价可能被其他因素掩盖；branch_efficiency 直接统计分支指令的行为，是"因果链上游"的证据。下性能结论时应当指标与时间互相印证。

**练习 2**：`nvprof --metrics branch_efficiency` 运行完后还能同时得到 4.2 节那张耗时表吗？
**答案**：通常不能同时得到完整的两段汇总——指定 `--metrics` 后输出以指标结果为主（时间列不再完整出现）。所以 test.sh 对每个规模把两条命令各跑一遍，正是为了分别拿到时间拆解和指标值。

### 4.4 仓库自带的 .output.txt 结果文件：结构与信息提取

#### 4.4.1 概念说明

仓库在各基准目录里散落着一批采集好的结果文件，命名规律是 `<可执行文件名>.output.<机器名>.txt`，例如 `BankRedux/sum_cuda.output.carina.txt`。它们是作者在 Carina/Fornax 集群上执行 `sh test.sh` 的**终端转录**——等于把"在一个没有 GPU 的机器上做实验"变成可能：你可以从里面提取 nvprof 表格数据做分析，也可以日后在自己的机器上复测并对照。

#### 4.4.2 核心流程

一个结果文件的解剖（以 [sum_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt) 为例）：

```text
第 1 行     "Test on Carina"                        —— 机器标注
第 5 行     ...$ sh test.sh                          —— 执行命令的 shell 提示符
之后按 test.sh 的每个规模重复一个"块"：
    Usage: sum <n>                                  —— 程序 stderr
    ==PID== NVPROF is profiling process PID, ...    —— nvprof 横幅（stderr）
    sum(n): checksum: ..., time: ...ms              —— 程序 stdout
    ==PID== Profiling application: ...
    ==PID== Profiling result:
    Type  Time(%)  Time  Calls ... Name             —— GPU activities 表
             ...                                    —— API calls 表
```

提取信息的定位技巧：搜索 `Profiling result:` 找到表头，其下以 `GPU activities:` 开头的一行以及紧随的缩进行就是设备侧数据；程序自己那行结果则搜 `checksum`。

两个阅读陷阱：

- **stdout/stderr 错位**：如 4.4.1 之前所述，转录时 stdout 全缓冲、stderr 无缓冲，所以 `Usage:` 与 nvprof 横幅可能出现在程序结果行之前，尽管真实时间上程序先打印 Usage。
- **历史痕迹**：[第 5 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L5-L5) 的提示符显示当时目录叫 `CUDAMemBench/Reduction_bank_conflicts`——仓库早期名称，与现在的 `CUDAMicroBench/BankRedux` 对应，读旧结果时不必困惑。

#### 4.4.3 源码精读

**完整可用的数据源**：[BankRedux/sum_cuda.output.carina.txt:5-L28](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L5-L28) 是 n=102400 的完整一块；[第 29-51 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L29-L51)、[第 52-74 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L52-L74)、[第 75-97 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L75-L97) 依次是 n=204800/409600/1024000 的块，与 [test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/test.sh#L1-L4) 的四行一一对应。

**仓库里的全部结果文件**（用 `Glob **/*.txt` 核实）：BankRedux、CoMem_AXPY、MemAlign、MiniTransfer_SpMV、WarpDivRedux 各有一个 carina 文件，UniMem 有两个 carina 文件；CoMem_SpMM、ReadOnlyMem_1D_Texture、ReadOnlyMem_2D_Texture 同时有 carina 和 fornax 两份；另有 HDOverlap/results.txt、Shmem/testResults.txt、Shuffle 的两个 result.txt 等非 nvprof 转录的结果（合计 17 个基准结果文件）。两个平台都有的基准（如 CoMem_SpMM 的 [carina](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.carina.txt) 与 [fornax](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.fornax.txt)）是后续讨论跨平台结论的素材（u6-l4）。

**一个可以直接从文件得出的结论**：把四个规模下 `sum_cudakernel`（无冲突）与 `sum_cudakernel_bc`（有冲突）的 Avg 相除，得到 bank 冲突版相对正常版的耗时比随规模缓慢上升——这正是 BankRedux 这个基准存在的意义（详细机理在 u4-l5）。

#### 4.4.4 代码实践（无需 GPU）

1. **实践目标**：从归档结果文件中提取数据并做比值分析。
2. **操作步骤**：
   - 打开 [sum_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt)，在四个规模块中分别找到 `sum_cudakernel` 与 `sum_cudakernel_bc` 两行的 Avg 值。
   - 计算每个规模下 \( r = \text{Avg}_{bc} / \text{Avg}_{normal} \)。
   - 再把四个规模的 HtoD Avg 与 n 相除，估算每浮点数（4 字节）的传输成本是否恒定。
3. **需要观察的现象**：r 是否大于 1 且随 n 变化；HtoD 是否近似线性于 n。
4. **预期结果**（由文件数据直接计算，可核对）：

| n | sum_cudakernel Avg | sum_cudakernel_bc Avg | 比值 r |
| --- | --- | --- | --- |
| 102400 | 3.0170us | 3.3850us | 1.12 |
| 204800 | 4.4310us | 5.2540us | 1.19 |
| 409600 | 6.9590us | 8.4670us | 1.22 |
| 1024000 | 14.7230us | 18.2360us | 1.24 |

带 bank 冲突的归约稳定慢 12%–24%。HtoD 方面，36.761us→69.429us→186.46us→778.00us 随 n 增长（1024000 档因数据量超过某些缓存阈值而超线性），**具体归因待进一步实验**。

#### 4.4.5 小练习与答案

**练习 1**：为什么这些文件只能"同机对比"，不能把 Carina 的 3.017us 与你在自己机器上的测量直接比大小？
**答案**：GPU 架构（SM 数量、频率、显存带宽）、驱动与工具链版本、以及 nvprof 本身的插桩开销都影响绝对数值。跨机器比较只能比**相对结论**（如"bc 版比正常版慢 12%–24%"这种比值），绝对时间没有可比性。

**练习 2**：文件里每个块的 `time: XX.XXms` 与 GPU activities 总时间差距很大，结合 4.2 的知识解释原因。
**答案**：程序打印的是 10 轮完整 `sum_cuda` 的墙钟平均，其中首次 `cudaMalloc` 的上下文创建（单次 308.16ms）摊入后占主导；GPU activities 段统计的是纯设备侧活动。这正是"读输出必须知道它量的是什么口径"的最好例证。

## 5. 综合实践

把本讲三个工具（裸运行、nvprof 概览、--metrics）串成一个完整实验，以 BankRedux 为对象：

1. **实践目标**：对同一次工作负载建立"程序口径 → 时间拆解 → 行为指标"三层测量，并与归档结果对照。
2. **操作步骤**：
   - 编译：`cd BankRedux && make`。
   - 第一层：`./sum_cuda 102400`，记录程序打印的 checksum 与 time，并按 4.1 的方法在源码里定位它们的算法。
   - 第二层：`nvprof ./sum_cuda 102400`，抄下 GPU activities 中三个 kernel（`sum_warmingup`、`sum_cudakernel`、`sum_cudakernel_bc`）与两个 memcpy 的 Avg 和 Calls。
   - 第三层：`nvprof --metrics branch_efficiency ./sum_cuda 102400`，记录三个 kernel 的指标值。
   - 制表：把第二层得到的三个 kernel 时间与 [sum_cuda.output.carina.txt:12-L16](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L12-L16) 的记录并排成对照表，形式如下：

| kernel（Avg） | 你的机器（待测） | Carina 归档值 |
| --- | --- | --- |
| sum_warmingup | 待本地验证 | 3.1480us |
| sum_cudakernel | 待本地验证 | 3.0170us |
| sum_cudakernel_bc | 待本地验证 | 3.3850us |

3. **需要观察的现象**：三层测量的数值口径完全不同（几十毫秒 / 几微秒 / 百分比）；对照表中两台机器的**绝对值**差异较大，但"bc 版最慢、warmingup 与 normal 版接近"的**次序**应当一致。
4. **预期结果**：程序 time 远大于三个 kernel 之和（差约三个数量级）；nvprof 的 Calls 列与代码结构核对无误；branch_efficiency 三个 kernel 均不超过 100% 且两两不完全相等。所有具体数值**待本地验证**。
5. 若本机无 GPU 或无 nvprof：完成 4.4.4 的归档数据分析作为替代，并把 4.1/4.2 的"预测表"写出来留待以后核验。

## 6. 本讲小结

- 程序打印的一行结果中，`checksum = result_cuda[0] - answer`（含浮点非结合性与 `VEC_LEN` 越界归并两个噪声源），`time` 是 10 轮完整 `sum_cuda` 调用的墙钟平均，包含显存分配与拷贝，**不代表 kernel 时间**。
- Carina 上的实测：56.80ms 的 wall time 里三个 kernel 合计每次仅约 9.55µs，大头是首次 `cudaMalloc` 的上下文创建摊销——墙钟与 GPU 时间的差距必须靠分析器量化。
- `nvprof` 概览输出 GPU activities 与 API calls 两段表；`Calls` 列可以反向核对程序结构（10 轮 × 3 kernel = 每 kernel 10 次等）。
- `nvprof --metrics branch_efficiency` 采集"未发散分支占比"，与时间拆解互补；仓库的标准用法见 WarpDivRedux/test.sh：每个规模时间与指标各跑一遍。
- 仓库自带 17 个基准结果 `.txt`，命名 `<程序>.output.<机器>.txt`，是终端转录而非结构化数据；提取时注意 stdout/stderr 错位，跨机器只能比相对结论。
- 从归档数据即可得出 BankRedux 的核心事实：带 bank 冲突的归约比无冲突版慢 12%–24%（随规模上升）。

## 7. 下一步学习建议

本讲完成后，单元一（项目地图、构建运行、代码骨架、输出与测量）就齐了。接下来进入单元二的 CUDA 基础：建议先读 [u2-l1 第一个 CUDA kernel：AXPY 与线程索引模型](u2-l1-first-cuda-kernel-axpy.md)，理解 `blockIdx/threadIdx` 与 `<<<grid, block>>>` 之后，再回头看本讲 nvprof 表里的 kernel 名与启动配置就完全通透了。对 branch_efficiency 背后的 warp 发散机理感兴趣的，可以预习 u3-l1（WarpDivRedux）；对 bank 冲突归约的慢 12%–24% 想追根问底的，目标是 u4-l5（BankRedux）。想先了解 nvprof 的现代替代品（nsys/ncu）用法的读者，可在具备 CUDA 11+ 环境时对照本讲的命令做迁移练习。

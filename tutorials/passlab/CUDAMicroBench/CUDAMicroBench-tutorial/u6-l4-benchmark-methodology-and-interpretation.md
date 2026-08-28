# 实验方法论与结果解读：跨平台输出、计时陷阱与报告写作

## 1. 本讲目标

这是学习手册的最后一讲，也是把前面 27 讲的「技术视角」切换到「方法论视角」的一讲。前面各讲都在回答「这个基准演示了什么优化技术」，本讲回答一个更根本的问题：**你凭什么相信你测出来的数字？**

学完本讲，你应该能够：

1. 把仓库自带的 `.output.carina.txt` / `.output.fornax.txt` 终端转录当作「无 GPU 环境下的云实验数据」来使用，并用 nvprof 表格的 `Calls` 列鉴定归档结果出自哪个版本的二进制。
2. 识别计时口径（timing scope）、warmup、平均次数这三个因素如何悄悄决定一个微基准结论的成色，并亲手算出「首次 `cudaMalloc` 的上下文创建开销被 10 次平均摊薄后仍然占墙钟一半以上」这类账。
3. 从 nvprof 的两张表（GPU activities / API calls）中分离 **kernel 时间、memcpy 时间、API 时间** 三类时间，并说明为什么判断「哪个 kernel 更快」必须依据 kernel 行而不是程序打印的 time。
4. 按论文/报告规范组织一份跨平台基准结果：环境清单、口径声明、数据表、结论适用范围，并列举至少三处可能造成跨平台差异的因素。

本讲大量承接 u1-l4（nvprof 表格读法、checksum 噪声源）、u5-l4（UniMem 的访问密度与交叉点）、u4-l3（SpMM 两平台 52~69 倍结论）已建立的结论，不再从零重复推导，而是在其上叠加「如何报告与如何避免过度泛化」这一层。

## 2. 前置知识

本讲需要的概念在前面讲义中都已出现过，这里只做一句话回顾：

- **wall time（墙钟时间）**：主机端 `read_timer_ms()` 测得的毫秒数，覆盖计时窗口内的全部主机代码、CUDA API 调用与等待（u1-l3、u2-l4）。
- **kernel 时间**：GPU 上 `__global__` 函数的真实执行时间，只能由 profiler（nvprof 及其继任者 nsys/ncu）给出（u1-l4）。
- **GPU activities / API calls 两张表**：nvprof 概览的上下两段，前者是 GPU 侧活动（kernel + `[CUDA memcpy ...]`），后者是主机侧 API 调用耗时（u1-l4）。
- **warmingup kernel**：每个基准里与被测 kernel 同构、用于消化首次执行一次性开销的预热 kernel（u1-l3、u2-l1）。
- **num_runs 多轮平均**：`for (i=0; i<num_runs; i++) ...` 后除以次数，抑制单次抖动（u2-l4）。
- **carina / fornax**：仓库作者采集归档结果的两台集群机器；Carina 的 GPU 在 UniMem 归档中显示为 Tesla V100-PCIE-32GB（u1-l4、u5-l4）。
- **访问密度** \(\rho = 1/s\)（\(s\) 为 stride）：衡量「搬运全量、只用少量」的浪费程度（u5-l4）。

如果你尚未读过 u1-l4，强烈建议先回去补「nvprof 概览表怎么读」那一节——本讲 §4.3 直接建立在它之上。

## 3. 本讲源码地图

本讲的主角不是某个 kernel，而是**结果文件与计时代码**。下表列出本讲涉及的全部文件：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md) | 项目总表与配套论文信息，是「结果该怎么报告」的参照 |
| [BankRedux/sum_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c) | BankRedux 的 host 主程序：计时循环、多轮平均、checksum 打印 |
| [BankRedux/sum_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu) | 三个归约 kernel + `sum_cuda` 包装函数（分配/拷贝/启动/释放全在里面） |
| [BankRedux/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/test.sh) | BankRedux 的实验脚本：4 个规模 + nvprof |
| [BankRedux/sum_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt) | Carina 上的完整终端转录（本讲最重要的数据源之一） |
| [CoMem_SpMM/SpMM_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c) | SpMM 的 host 主程序：计时循环被注释，只打印 check 值 |
| [CoMem_SpMM/SpMM_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu) | 4 个 kernel（含 2 个 warmingup），是「两平台二进制版本不一致」的证据 |
| [CoMem_SpMM/SpMM_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.carina.txt) | Carina 上的 SpMM 结果：4 个 kernel 各 1 次调用 |
| [CoMem_SpMM/SpMM_cuda.output.fornax.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.fornax.txt) | Fornax 上的 SpMM 结果：只有 2 个 kernel，且带 auto boost 警告 |
| [UniMem/LowAccessDensityTest_cuda.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu) | UniMem 基准：离散/统一两条路径，两条路径计时口径不对称 |
| [UniMem/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/test.sh) | 固定 n=134217728、扫 stride 的实验设计 |
| [UniMem/test2.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/test2.sh) | 「固定触碰元素数」的对照实验设计（stride 与 n 同步放大） |
| [UniMem/LowAccessDensityTest_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt) | Carina 上的 UniMem 结果：前半无 profiler、后半带 nvprof 与 UM 迁移表 |
| [UniMem/LowAccessDensityTest_cuda_fixed_access_time.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda_fixed_access_time.output.carina.txt) | test2.sh 对应的「固定访问量」结果 |

> 文件名勘误：规划大纲里本讲的源码清单写过 `CoMem_SpMM/SpMV_cuda.output.fornax.txt`，仓库中并不存在该文件。实际存在的是 [CoMem_SpMM/SpMM_cuda.output.fornax.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.fornax.txt)（本讲采用）与 [MiniTransfer_SpMV/SpMV_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.output.carina.txt)（仅 Carina，无 Fornax 版）。这本身就是本讲主题的一个活例子：**引用数据文件之前先 `ls` 核验**。

## 4. 核心概念与源码讲解

本讲的四个最小模块是：**多平台结果文件、warmup 与平均、计时口径、结果解读与报告**。

### 4.1 多平台结果文件：仓库自带的「云实验数据」与法医学鉴定

#### 4.1.1 概念说明

CUDAMicroBench 的每个基准目录里几乎都躺着若干 `*.output.*.txt` 文件——它们是作者在 Carina、Fornax 两台集群机器上跑 `test.sh` 时**把终端输出原样粘贴下来的转录**，包含 shell 提示符、nvprof 的完整表格、甚至 nvprof 的警告行。

对没有 GPU 的读者（比如正在读这本手册的你），这些文件就是「云实验数据」：u1-l4 已经用 `sum_cuda.output.carina.txt` 做过一次云实验，本讲把这套做法系统化，并补上三个此前没展开的纪律：

1. **转录 ≠ 数据库**。终端转录里 stdout 与 stderr 常常错位（u1-l4 提过 `Usage:` 行穿插的现象），跨行拼接时要靠 `==PID==` 前缀对齐。
2. **归档结果出自的二进制未必是当前源码编译出来的**。仓库在演化，`.txt` 不会跟着重新生成——引用之前必须做法医学鉴定（§4.1.2 的 Calls 列鉴定法）。
3. **两台机器的结果只有相对结论可比**。「CSC 比 CSR 快 52 倍」这种比值可以跨机器讨论；「CSR 要 48ms」这种绝对值换个 GPU、驱动、时钟状态就作废。

#### 4.1.2 核心流程

读取一份归档转录的标准动作：

1. 读**头部元数据**：SpMM 的转录开头就写明了实验参数。
2. 读**程序自身的打印**（checksum / time 行），这是「程序视角」。
3. 读 **nvprof 两张表**（GPU activities / API calls），这是「profiler 视角」。
4. **用 `Calls` 列反向鉴定二进制**：`Calls` 是该 kernel/API 在整个进程生命周期内被调用的总次数，它由代码结构唯一决定，因此可以当「指纹」用：
   - 设计时循环为 \(R\) 次（`num_runs`），则 kernel 总调用数应等于「包装函数内 kernel 数 \(\times\) (预热调用 + \(R\))」这类由源码结构推出的预测值；
   - 预测值与归档值对不上 ⇒ 归档时的源码/参数与当前仓库不同。
5. 最后才做跨平台对比，且只比**相对量**（比值、趋势、交叉点位置）。

用伪代码表达鉴定逻辑：

```text
由当前源码推出: expected_calls(kernel) = f(代码结构, num_runs)
若 archive_calls(kernel) != expected_calls(kernel):
    结论: 归档结果出自旧版本二进制, 引用时必须注明
```

#### 4.1.3 源码精读

**(a) SpMM 的两份归档：同一名义实验、两个二进制版本。**

Carina 版转录的头部写明了实验参数（[CoMem_SpMM/SpMM_cuda.output.carina.txt:L1-L6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.carina.txt#L1-L6)）：`num_rows = 100`、`nnz = 1024`，并说明两个 kernel 的格式差异。当前源码里确实有 **4 个 kernel**——两个 warmingup 加两个被测 kernel（[CoMem_SpMM/SpMM_cudakernel.cu:L8-L96](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu#L8-L96)，四个 `__global__` 定义分别位于 L8、L31、L54、L76）。Carina 的 GPU activities 表与此吻合：4 个 kernel **各调用 1 次**（[CoMem_SpMM/SpMM_cuda.output.carina.txt:L17-L20](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.carina.txt#L17-L20)）。

Fornax 版的 GPU activities 表里**只有 2 个 kernel，各 1 次调用，warmingup 不见了**（[CoMem_SpMM/SpMM_cuda.output.fornax.txt:L18-L19](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.fornax.txt#L18-L19)）。源码结构对不上 ⇒ **Fornax 归档出自加 warmingup 之前的旧版源码**。更硬的证据在 API 表：Fornax 上 `cuDeviceTotalMem` 被调用 4 次、`cuDeviceGetAttribute` 388 次，而 Carina 上分别是 1 次和 97 次（[CoMem_SpMM/SpMM_cuda.output.fornax.txt:L24-L25](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.fornax.txt#L24-L25) 对比 [CoMem_SpMM/SpMM_cuda.output.carina.txt:L25-L26](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.carina.txt#L25-L26)）——两台机器的 nvprof/驱动版本也不同。结论：这两份转录**连工具链都不一致**，绝对时间更没有可比性。

两平台的可比如下（只比比值）：

| 平台 | spmm_csr_kernel | spmm_csc_csr_kernel | CSC 加速比 |
| --- | --- | --- | --- |
| Carina | 48.011 ms | 0.91577 ms | 52.4× |
| Fornax | 354.30 ms | 5.1618 ms | 68.6× |

CSR 版绝对时间差 7.4 倍，但「CSC 远快于 CSR」的定性结论与 50~70 倍的量级在两台机器上**同时成立**——这正是 u4-l3 已经得出的「跨平台相对结论稳定、绝对值不可比」的原始出处。另外注意 Fornax 表格上方那行 nvprof 警告（[CoMem_SpMM/SpMM_cuda.output.fornax.txt:L10](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.fornax.txt#L10)）：

> `Warning: Auto boost enabled on device 0. Profiling results may be inconsistent.`

Fornax 的 GPU 没锁频，auto boost 开着，nvprof 自己都警告结果可能不一致。这是一条价值极高的归档证据：**没有锁频的机器上，绝对时间连同一台机器内部都未必稳定**。

**(b) UniMem 的归档：`Calls` 列暴露 `num_runs` 漂移。**

当前源码写的是 `num_runs = 100`（[UniMem/LowAccessDensityTest_cuda.cu:L142](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L142)）。据此推算 kernel 调用指纹：main 先做 1 次离散路径预热调用（[L145](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L145)），再计时循环 \(R\) 次离散 + \(R\) 次统一（[L147-L154](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L147-L154)），每条路径各含 1 个 kernel，故总调用数为 \(2R+1\)。若 \(R=100\)，应为 201 次。而 Carina 归档里 kernel 只被调用了 **21 次**、`[CUDA memcpy HtoD]` 只有 **11 次**（[UniMem/LowAccessDensityTest_cuda.output.carina.txt:L102-L103](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L102-L103)）。\(2R+1=21 \Rightarrow R=10\)；离散路径每次有一个显式 `cudaMemcpy` 而统一路径没有，\(1+R=11\) 也吻合。**归档时的 `num_runs` 是 10，当前源码改成了 100**——引用这份数据时必须注明，否则别人用当前源码复现会看到调用次数差一个量级。

**(c) 转录连脚本都对不上。**

仓库里的 [UniMem/test.sh:L1-L28](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/test.sh#L1-L28) 是 28 行裸命令（无 nvprof），而归档后半部分的每条命令都被 `==PID== NVPROF is profiling ...` 包裹（如 [UniMem/LowAccessDensityTest_cuda.output.carina.txt:L96](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L96)），且 shell 提示符里的目录名从 `LowAccessDensity` 变成了 `LowAccessDensity2`。说明归档实验用的是作者本地一份包了 nvprof 的脚本变体，没有回传仓库。方法论教训：**报告要附上真正用来跑实验的脚本**，否则「按 test.sh 复现」这句话本身不可复现。

#### 4.1.4 代码实践：用 Calls 列做一次二进制法医学鉴定

1. **实践目标**：不运行任何程序，仅凭源码结构与归档 nvprof 表格，判断三份归档各自出自什么版本的代码/参数。
2. **操作步骤**：
   - 打开 [CoMem_SpMM/SpMM_cuda.c:L272-L287](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L272-L287)，确认 `num_runs = 5` 但两个计时 `for` 循环都被注释（L275、L279），每个 CUDA 包装函数只执行 1 次；再数一数 [SpMM_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cudakernel.cu) 里两个包装函数各启动几个 kernel。
   - 预测：Carina 上应看到 4 个 kernel 各 1 次；与 [SpMM_cuda.output.carina.txt:L17-L20](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.carina.txt#L17-L20) 对照。
   - 打开 [UniMem/LowAccessDensityTest_cuda.cu:L142-L154](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L142-L154)，用 \(2R+1\) 公式从 [归档的 21 次 kernel 调用](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L102) 反解 \(R\)，与源码里的 100 对比。
3. **需要观察的现象**：Carina SpMM 表格恰好 4 行 kernel、每行 Calls=1；UniMem 表格 kernel 行 Calls=21、memcpy 行 Calls=11。
4. **预期结果**：SpMM/Carina 与当前源码结构一致；SpMM/Fornax 少 2 个 warmingup（旧版）；UniMem/Carina 反解出 \(R=10\) 而当前源码为 100（参数漂移）。本实践的全部证据都来自静态阅读，无需 GPU，可直接完成（结论已在 §4.1.3 给出，可自行核验）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Calls` 列比 `Time` 列更适合做「二进制指纹」？

**答案**：`Time` 受机器、驱动、时钟、负载影响，同一二进制两次运行也会不同；而 `Calls` 由代码结构（包装函数里启动了几个 kernel、循环跑几次）唯一决定，与运行环境无关，所以预测值与归档值不一致时可以干净地归因于「代码/参数版本不同」。

**练习 2**：Fornax 的 SpMM 转录里 `cuDeviceGetAttribute` 被调用 388 次，Carina 只有 97 次。这说明了什么？它影响你引用哪一类数字？

**答案**：说明两台机器的 nvprof/CUDA 工具链版本不同（不同版本的 profiler 初始化时查询的设备属性数量不同）。它提醒你两份归档连测量工具都不一致，因此绝对时间（48ms vs 354ms）不可直接比较，只能比较同一次运行内部的相对量（如 CSC/CSR 比值）。

**练习 3**：如果你想让自己机器上的结果可以和 Fornax 归档对话，最小限度的做法是什么？

**答案**：做不到完全可比——Fornax 未锁频且工具链不同。务实的做法是：记录自己的环境清单（GPU 型号、驱动、CUDA 版本、nvprof 版本、是否锁频），只引用相对结论（比值、趋势），并明确声明「绝对值不可跨平台比较」。

### 4.2 warmup 与多轮平均：计时窗口里的隐藏大头

#### 4.2.1 概念说明

warmup（预热）与多轮平均（`num_runs`）是微基准计时的两件标配，但它们**防不住所有的一次性开销**：

- warmingup kernel 防的是 kernel 首次执行的开销（指令缓存冷、JIT 等）；
- 多轮平均防的是单次抖动；
- 但**首次 `cudaMalloc` 触发的 CUDA 上下文创建**（数百毫秒量级）发生在计时窗口内的第一次循环里，只能被「摊薄」而不能被「消除」——除非你在计时开始前额外做一次不计时的完整调用。

摊薄的数学：设第一次迭代含一次性开销 \(T_{\text{first}}\)，稳态每次 \(T_{\text{steady}}\)，共 \(R\) 轮，则打印值为

\[
\bar T = \frac{T_{\text{first}} + (R-1)\,T_{\text{steady}}}{R}
       = \frac{T_{\text{first}}}{R} + \frac{R-1}{R}T_{\text{steady}}
\]

当 \(T_{\text{first}}\) 高达数百毫秒而 \(R\) 只有 10 时，\(T_{\text{first}}/R\) 这一项可以轻松超过被测对象本身几个数量级。**平均次数 \(R\) 不是越大越「科学」，它直接改写你测到的量的构成**。

#### 4.2.2 核心流程

以 BankRedux 为例，把「程序打印的 time」拆账的流程：

1. 从源码确认计时窗口的起止与窗口内包含哪些调用。
2. 从 nvprof 的 API calls 表找到一次性开销（看 `Max` 列远大于 `Avg` 的行——那是首调用）。
3. 用上面的公式算出 \(T_{\text{first}}/R\) 在 \(\bar T\) 中的占比。
4. 用 kernel 行的稳态时间对比占比，得出「打印的 time 里有多少比例在被测对象身上」。
5. 顺带用「源码完全相同的两个 kernel」标定噪声水平。

#### 4.2.3 源码精读

**(a) 计时窗口里有什么。**

[BankRedux/sum_cuda.c:L87-L96](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c#L87-L96)：`num_runs = 10`，`read_timer_ms()` 取表于循环之前，循环内每次调用 `sum_cuda(n, x, result_cuda)`，最后除以 10 打印。注意两点：

- `read_timer_ms` 是毫秒精度的 `ftime` 墙钟（[BankRedux/sum_cuda.c:L14-L18](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c#L14-L18)）；
- **主机端的块间归并循环也被计进了窗口**（[BankRedux/sum_cuda.c:L92-L93](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c#L92-L93) 位于两次 `read_timer_ms()` 之间）——它遍历的是 `VEC_LEN`（1024000）对应的块数而非命令行 `n`，这也是 u1-l4 指出的 checksum「越界归并」噪声源的姊妹问题。

而 `sum_cuda` 包装函数内部每次都完整执行 cudaMalloc → H2D → 三个 kernel（各跟一次同步）→ D2H → cudaFree（[BankRedux/sum_cudakernel.cu:L57-L75](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L57-L75)）。也就是说：**分配、拷贝、释放全部在计时窗口内反复发生**，warmingup kernel 也在窗口内（每次调用都跑一遍）。

**(b) 拆账：56.80 ms 到底是什么。**

Carina 归档 n=102400 这一行（[BankRedux/sum_cuda.output.carina.txt:L8](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L8)）打印 `time: 56.80ms`。查 API 表（[L17](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L17)）：`cudaMalloc` 20 次共 309.70 ms，**Max 单次 308.16 ms**——第一次 `cudaMalloc` 创建了 CUDA 上下文。代入公式：

\[
\frac{T_{\text{first}}}{R} = \frac{308.16\ \text{ms}}{10} \approx 30.8\ \text{ms}
\]

占打印值 56.80 ms 的 **54%**。再看被测对象：三个 kernel 的稳态时间合计约 \(3.017+3.148+3.385 \approx 9.55\,\mu s\) 每次（[L13-L15](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L13-L15)），占打印值不足 **0.02%**。u1-l4 说过「大头是首次 cudaMalloc 的上下文创建摊销」，这里把账算到了个位数。

这个账还解释了一个反直觉现象：打印的 time 对 n **不单调**——56.80 → 47.30 → 42.40 → 48.30 ms（n 从 102400 涨到 1024000，见 [L8](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L8)、[L31](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L31)、[L54](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L54)、[L77](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L77)）：数据变大 10 倍，墙钟反而先降后升，因为它由摊销后的固定开销主导，与被测变量几乎解耦。

**(c) 噪声标尺：一对同卵 twin kernel。**

[sum_cudakernel.cu:L8-L22 与 L24-L38](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L8-L38) 的 `sum_warmingup` 与 `sum_cudakernel` 函数体**逐字相同**。Carina 上它们测得 3.148 µs 与 3.017 µs，差 4.3%（n=1024000 时差 1.5%，见 [L83-L84](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L83-L84)）。这就是免费的**噪声标尺**：源码零差异的两个 kernel 尚且差 1.5%~4%，凡小于这个幅度的差异都应视为噪声。对照被测差异——带 bank 冲突的 `sum_cudakernel_bc` 相对 `sum_cudakernel` 慢 12.2% → 18.6% → 21.7% → 23.9%（随 n 增长，由四个规模的 kernel 行算出）——远超噪声标尺，结论才站得住。u5-l2 在纹理基准里用过同一招，这里给出它的定量版本。

#### 4.2.4 代码实践：给 BankRedux 做拆账并加一次「计时外预热」

1. **实践目标**：亲测「首次 cudaMalloc 占比」并验证「计时窗口外的预热调用」能否洗掉它。
2. **操作步骤**（示例代码，修改仅用于本地实验，不要提交回仓库）：
   - 按原样编译运行并记录：`cd BankRedux && make && ./sum_cuda 102400`（编译命令见 [BankRedux/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/Makefile#L1-L2)）。
   - 在 [sum_cuda.c:L89](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c#L89) 的 `elapsed = read_timer_ms();` 之前加一行 `sum_cuda(n, x, result_cuda);`，重新编译运行同一 n。
   - 用 `nvprof ./sum_cuda 102400`（来自 [BankRedux/test.sh:L1](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/test.sh#L1)）对照 API 表中 `cudaMalloc` 的 Max 值变化。
3. **需要观察的现象**：加预热前打印约 50~60 ms 量级；加预热后应显著下降（Carina 数据推算约降到 20 ms 以下），`cudaMalloc` 的 Max 从 ~308 ms 掉到微秒~毫秒量级（因为上下文创建已发生在计时窗口外的那次预热调用里）。
4. **预期结果**：打印的 time 大幅下降，但 **kernel 行时间基本不变**——证明被洗掉的是测量噪声而非真实优化。本环境无 GPU，具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把 `num_runs` 从 10 改成 100（其余不动），打印的 time 会怎么变？这会让结果更接近 kernel 真实时间吗？

**答案**：\(T_{\text{first}}/R\) 从 30.8 ms 降到 3.1 ms，打印值会明显下降并趋于稳态单次开销（分配+拷贝+kernel+释放）。但它永远**不会**收敛到 kernel 时间，因为窗口内还包含每次迭代的 cudaMalloc/cudaMemcpy/cudaFree；kernel 时间只能从 nvprof 的 kernel 行读。另注意 UniMem 的教训：改 `num_runs` 会改变 `Calls` 指纹，发布数据时要同步说明。

**练习 2**：BankRedux 里的 warmingup kernel 每次调用 `sum_cuda` 都会执行一遍（共 10 遍）。既然第 2 遍起早已「热」了，它还起作用吗？

**答案**：作为「预热」它只需一遍，后 9 遍是冗余计算；但作为**噪声标尺**（与 `sum_cudakernel` 同源不同名）它每遍都在免费提供一次重复测量，§4.2.3(c) 正是这么用它。一个设计上的副产品变成了方法论工具。

**练习 3**：为什么打印的 time 对 n 不单调（56.80→47.30→42.40→48.30 ms）反而是「合理」的？

**答案**：因为打印值的主要成分是 \(T_{\text{first}}/R\)（约 30.8 ms 的摊销固定开销）加每次迭代的分配/拷贝开销，真正的被测 kernel 只占 ~0.02%；固定项不随 n 变化，其余项在 n≤10^6 时也只有毫秒级，于是噪声就能造成非单调。这提示：**当打印值对被测变量不敏感时，它已经在告诉你它测的不是你想测的东西**。

### 4.3 计时口径：wall / GPU / API 三类时间与不对称计时

#### 4.3.1 概念说明

「计时口径」指**计时器的起止位置之间到底包住了什么**。本项目的归档数据里至少并存三种口径：

1. **程序墙钟**：`read_timer_ms()` 包住计时循环，涵盖主机代码、API 调用、同步等待（BankRedux、UniMem 打印的 time）。
2. **profiler 的 GPU 时间**：nvprof 概览上表 `GPU activities`，含 kernel 行与 `[CUDA memcpy HtoD/DtoH]` 行——注意 **memcpy 也是 GPU 活动**（由 DMA 引擎执行），算「GPU 时间」但不等于「kernel 时间」。
3. **profiler 的 API 时间**：下表 `API calls`，主机侧发起每个调用的耗时，反映 CPU 侧开销（u3-l4 曾用它论证 CUDA Graph 的收益在 CPU 侧）。

判断「哪个 kernel 更快」时，三类时间的适用性排序是：**kernel 行（必要）→ 硬件计数器指标（佐证机制）→ API/墙钟（只用于端到端论证）**。混用口径是微基准报告最常见的错误。

比「选错口径」更隐蔽的是**不对称口径**：同一个基准里两条被比较的路径，计时窗口不一致，比值因此失真。

#### 4.3.2 核心流程

审计算口径的清单（对任何一份微基准代码过一遍）：

1. 计时器在哪里启动、哪里停止？（两个 `read_timer_ms()` 之间）
2. 窗口内有哪些 API？（分配、拷贝、启动、同步、释放）
3. 被比较的两条路径的窗口是否**同构**？（同样包含/排除分配与释放？）
4. 有没有「不计时」的注释（如 `//initial unified memory, should not count time here`）？
5. profiler 视角下，同一实验的 kernel/memcpy/API 三类时间各是多少？与程序打印值互相印证了吗？

#### 4.3.3 源码精读

**(a) 三类时间的实景：BankRedux n=1024000。**

程序打印 48.30 ms（墙钟，[L77](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L77)）；GPU activities 表里 memcpy HtoD 占 7.78 ms/10 次、三个 kernel 合计约 47.9 µs/次、DtoH 2.26 µs/次（[L81-L85](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L81-L85)）；API 表里 cudaMalloc 242.51 ms、cudaMemcpy 9.78 ms（[L86-L87](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L86-L87)）。三张口径讲三个故事：墙钟讲「用户等多久」，GPU 表讲「设备在干嘛」，API 表讲「主机在干嘛」。要回答「折半归约与交错归约谁快」，只有 kernel 行有发言权（14.723 µs vs 18.236 µs，bc 版慢 23.9%），且要用 §4.2.3(c) 的噪声标尺判显著性。

**(b) 计时器还在、数据全靠 profiler：SpMM。**

[CoMem_SpMM/SpMM_cuda.c:L272-L287](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.c#L272-L287)：`num_runs = 5`、`elapsed` 照常计算，但两个 `for` 循环都被注释（L275、L279），`elapsed` 算完**从未打印**，程序只输出四个 check 值。也就是说 SpMM 的所有性能数据 100% 来自 nvprof（转录里也确实只有 check 行 + profiler 表，[SpMM_cuda.output.carina.txt:L10-L20](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_SpMM/SpMM_cuda.output.carina.txt#L10-L20)）。这是一种自觉的口径选择：与其打一个混入串行实现的墙钟，不如只留 profiler 的 kernel 行。代价是「没有 nvprof 就没有数据」。

**(c) 不对称口径：UniMem 的两条路径。**

离散路径（[UniMem/LowAccessDensityTest_cuda.cu:L54-L66](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L54-L66)）不返回时间，由 main 的外层计时器包住（[L147-L149](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L147-L149)）——窗口含 `cudaMalloc`、H2D、kernel、同步、`cudaFree` **全套**。

统一内存路径（[L69-L92](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L69-L92)）自己计时并返回 `elapsed1 + elapsed2`：`elapsed1` 只包 `cudaMallocManaged`（[L71-L74](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L71-L74)），主机侧填充 `x2` 被显式排除（注释 `should not count time here`，[L76-L77](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L76-L77)），`elapsed2` 包分配 d_y + kernel + 同步（[L79-L85](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L79-L85)），而 **`cudaFree` 在窗口之外**（[L88-L89](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L88-L89)）。

于是并排打印的两行（[L156-L157](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L156-L157)）口径不同：离散含释放、统一不含；统一排除主机填充（有注释说明理由：数据迁移发生在 kernel 执行中，填充本身不属于被测的传输策略）、离散的对应填充在计时开始前完成。u5-l4 已把这列为三处口径不对称之一。方法论上的正确姿势不是「修正后才能看」，而是：**报告里写清楚每行数字的窗口边界**，比较时只在同口径内比，或干脆用 profiler 的 kernel/memcpy/迁移表做交叉验证。

**(d) memcpy 也是 GPU 活动：UniMem stride=1 的构成。**

Carina 归档 nvprof 段 n=134217728、stride=1：GPU activities 里 `[CUDA memcpy HtoD]` 11 次共 1.494 s、kernel 21 次共 2.336 s（[L102-L103](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L102-L103)）。若把「GPU 时间」当「kernel 时间」，会把 39% 的 DMA 拷贝时间误记到 kernel 头上。分离之后才能看清：离散路径的成本大头是那次 512 MB 的整体 H2D（µs 级的 kernel 才是被测对象之外的小头），统一路径的成本藏在**迁移表**里——`Unified Memory profiling result` 显示 63050 次 HtoD 迁移、平均 45 KB、总量 2.706 GB、迁移耗时 412.70 ms、6136 个 GPU 缺页组、15355 次主机缺页（[L118-L123](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L118-L123)）。这张表**只在 profiler 下存在**，是「按需迁移并不免费」的直接证据。

#### 4.3.4 代码实践：一分钟口径审计

1. **实践目标**：对 UniMem 输出的两行 time 各写一句「窗口声明」，并用 profiler 数字交叉验证。
2. **操作步骤**：
   - 读 [UniMem/LowAccessDensityTest_cuda.cu:L147-L157](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L147-L157)，分别列出两行打印的窗口内 API 清单（提示：离散 = malloc 显存+H2D+kernel+sync+free；统一 = MallocManaged+（d_y 分配+kernel+sync），不含主机填充与 free）。
   - 在有 GPU 的机器上运行 `./LowAccessDensityTest_cuda 1 134217728`（来自 [test.sh:L1](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/test.sh#L1)）记录两行打印；再用 `nvprof ./LowAccessDensityTest_cuda 1 134217728` 记录 kernel 行、memcpy 行与迁移表。
   - 把打印值与 profiler 三类时间并排放一行，检查数量级能否互相解释。
3. **需要观察的现象**：打印的两行 time 与 profiler 的 kernel/memcpy/迁移时间分属不同数量级；迁移表的 Total Size（2.7 GB 量级）远大于数组本身（512 MB），说明按页迁移存在放大。
4. **预期结果**：写出两句窗口声明（离散含 free、统一不含 free 且排除主机填充），并指出两行 time **不能**当作「kernel 对决」的证据。具体数值待本地验证；Carina 的参考值：无 profiler 时离散 121.54 ms / 统一 172.70 ms（[L5-L6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L5-L6)）。

#### 4.3.5 小练习与答案

**练习 1**：nvprof 概览的 `GPU activities` 表里 `[CUDA memcpy HtoD]` 占了 60%+,能不能说「这个程序的计算被内存传输瓶颈化」？

**答案**：不能直接说。memcpy 行属于 GPU 活动但由拷贝引擎执行，与 kernel 是否访存受限是两回事；该行占比大只说明**端到端时间**里传输占大头（对 UniMem 离散路径这是设计使然：整体搬运 512 MB）。要论证 kernel 本身受限，应看 kernel 行时间与硬件计数器（如 `gld_efficiency`、DRAM 吞吐），u6-l2 的三层证据法就是为此设计的。

**练习 2**：SpMM 的 host 计时代码还在（`elapsed` 计算了）但从不打印。这种「留着但不用的计时器」有什么风险？

**答案**：一是误导读者以为程序会输出时间；二是窗口本身就不干净（L274-L282 之间还包含两个串行 SpMV 实现），真打开了也混入大量无关开销；三是死代码暗示演化痕迹——恰好与 Fornax 归档的「无 warmingup」互证：这段代码经历过实验性修改。

**练习 3**：如果把 UniMem 两条路径的口径统一成「都含分配与释放、都排除主机填充」，你预期 stride=1 时统一/离散的比值会变大还是变小？

**答案**：变大（统一的劣势更明显）。当前口径把统一路径的 `cudaFree(x2)` 排除在外，而统一内存的释放可能触发迁移回收，是真实成本的一部分；把它排除等于给统一路径减负。反之离散路径已含释放。统一口径后统一路径增加的成本项多于离散路径。此为分析推断，**待本地验证**（可给两条路径套同一对 timer 事件再比较）。

### 4.4 结果解读与报告写作：证据层级、对照设计与结论适用范围

#### 4.4.1 概念说明

前三个模块都在「读数据」，本模块讲「下结论与写报告」。三条核心原则：

1. **证据层级**：判断「A 比 B 快」时，证据强度从弱到强为——程序墙钟 < nvprof kernel 行 < kernel 行 + 硬件计数器 + 机制解释。u6-l1 的七级归约阶梯、u3-l1 的 branch_efficiency 都是「机制解释」的范例：不仅说快了多少，还说**为什么**快。
2. **对照设计**：单一变量是微基准的生命线。UniMem 的 test2.sh 是仓库里最好的对照设计范本：固定「触碰元素数」、只放大「搬运总量」，把传输策略这一个变量隔离出来。
3. **结论适用范围**：每条结论都应附带「在什么条件下成立」。跨平台数据的基本法：**相对结论（比值、趋势、交叉点的存在性）可比，绝对值不可比**。

还有一条容易忽略的原则：**profiler 本身会扰动被测系统**。归档数据里恰好有现成的量化证据（§4.4.3(c)）。

#### 4.4.2 核心流程

组织一份合格微基准报告的流程：

1. **环境清单**：GPU 型号与数量、驱动、CUDA 工具链与 profiler 版本、是否锁频、编译选项（`-arch`、`-G`）、数据规模与迭代次数。
2. **口径声明**：每张表的时间是什么口径（墙钟/kernel/API/迁移表），两条被比路径的窗口是否同构。
3. **数据**：至少含 kernel 行时间与均值/最小值；规模扫描给趋势而非单点。
4. **机制解释**：差异对应到硬件计数器或体系结构原因（bank 冲突、缺页、事务数……）。
5. **适用范围**：「在 V100、CUDA x.y、该数据规模下成立」；指出未验证的维度。
6. **可复现性**：附真正跑实验的脚本（记住 §4.1.3(c) 的教训：UniMem 归档用的脚本没有回传仓库）。

#### 4.4.3 源码精读

**(a) 对照设计范本：test2.sh 的「固定访问量」实验。**

[UniMem/test2.sh:L1-L21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/test2.sh#L1-L21) 每行 `./LowAccessDensityTest_cuda <stride> <n>` 中 n 始终是 stride 的 1024 倍：\(n = 1024\,s\)。kernel 只触碰 \(n/s = 1024\) 个元素（[LowAccessDensityTest_cuda.cu:L46-L52](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L46-L52) 的 `if (i < (n/stride))`），于是**计算量恒定、搬运量随 n 线性放大**——离散路径必须整体 `cudaMemcpy` 全量（[L59](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L59)），统一路径只按需迁触碰过的页。转录尾部的说明也点明了设计意图（[LowAccessDensityTest_cuda_fixed_access_time.output.carina.txt:L70-L71](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda_fixed_access_time.output.carina.txt#L70-L71)）。

结果（同文件）：n=1024 时两者 0.40 / 0.35 ms（噪声级，[L5-L6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda_fixed_access_time.output.carina.txt#L5-L6)）；n=2^30 时离散 1199.85 ms、统一 104.25 ms，差 **11.5 倍**（[L65-L66](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda_fixed_access_time.output.carina.txt#L65-L66)）。离散时间随 n 近似线性增长（搬运量主导），统一时间在百 ms 量级进入平台期——平台期的机制解释是：大 stride 下 1024 个触碰元素分散在约 1024 个不同 4 KB 页上，成本从「带宽」转为「缺页次数 × 迁移延迟」，与 §4.3.3(d) 迁移表中 4 KB 最小迁移粒度一致（此为分析推断）。对比 test.sh 的固定 n 扫 stride（[test.sh:L1-L28](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/test.sh#L1-L28)）给出交叉点（u5-l4：ρ 在 25%~50% 之间，stride=1 时统一慢 42%，见 [主归档 L5-L12](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L5-L12)），test2.sh 则证明「只传必要数据」的收益上不封顶、代价是页粒度税。**两个脚本合起来才构成完整的结论**——单看任何一个都会片面。

**(b) 相对结论的跨平台稳定性。**

把本讲所有跨平台/跨口径数据放进一张表（全部来自仓库归档）：

| 结论 | Carina | Fornax | 相对结论跨平台成立？ |
| --- | --- | --- | --- |
| SpMM：CSC 格式快于 CSR | 52.4×（48.011/0.916 ms） | 68.6×（354.30/5.162 ms） | 成立，量级一致（u4-l3：52~69×） |
| BankRedux：bank 冲突版更慢 | +12.2%~+23.9%（随 n） | 无归档 | 单平台，需本机复现 |
| UniMem：stride=1 时统一更慢 | +42%（172.70 vs 121.54 ms） | 无归档 | 单平台，需本机复现 |
| UniMem：极稀疏时统一反超 | 147.24 → 1.08 ms（s=n） | 无归档 | 单平台 |

规律很清楚：**比值与趋势在换了 GPU、驱动、二进制版本之后仍然保序**（SpMM 两平台的工具链与代码版本都不一致，52× 与 69× 仍同量级），而绝对值换了环境就面目全非（CSR 48 ms → 354 ms）。这就是「只泛化相对结论」这条纪律的经验基础。

**(c) profiler 的扰动要量化，不要假装不存在。**

同一台 Carina、同一命令 `./LowAccessDensityTest_cuda 1 134217728`：

| 条件 | 离散 | 统一 |
| --- | --- | --- |
| 无 profiler（[主归档 L5-L6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L5-L6)） | 121.54 ms | 172.70 ms |
| nvprof 下（[主归档 L97-L98](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L97-L98)） | 140.70 ms | 236.20 ms |
| 相对膨胀 | +15.8% | +36.8% |

小数组端更夸张：n=1024 的统一路径无 profiler 为 0.35 ms（[fixed 归档 L5-L6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda_fixed_access_time.output.carina.txt#L5-L6)），nvprof 下 2.80 ms（[L77-L78](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda_fixed_access_time.output.carina.txt#L77-L78)），**8 倍**。两个教训：其一，被 profiler 观测越重的路径（统一内存要拦截每次缺页迁移）膨胀越大，**profiler 会系统性地偏袒某一方**；其二，「裸跑打印值」与「profiler 表格值」必须分列两栏报告，不能混抄进同一列。

**(d) 报告的环境清单：归档里缺了什么。**

对照 §4.4.2 的清单审计仓库归档：有 shell 主机名（carina/fornax）、有 nvprof 表格（可推工具行为），但 **GPU 型号只有 UniMem 的迁移表顺带披露了 `Tesla V100-PCIE-32GB`（[主归档 L119](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L119)），Fornax 的 GPU 型号在所有归档中均未出现（待确认）**；驱动版本、CUDA 版本、锁频状态（Fornax 反而警告了 auto boost）、编译选项一概没有。README 的论文条目（[README.md:L119](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L119)）对应的正式发表版本应当补全这些——这也是本讲实践要求你为自己的机器建立环境清单的原因。

#### 4.4.4 代码实践：为「纹理 vs 全局内存」写一段结论适用范围声明

1. **实践目标**：把 u5-l2 的结论（Carina 上纹理慢 1%~4%、Fornax 上反而快数倍）改写成一段带完整限定语的报告文字。
2. **操作步骤**：
   - 重读 [ReadOnlyMem_1D_Texture/axpy_cuda.output.fornax.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/ReadOnlyMem_1D_Texture/axpy_cuda.output.fornax.txt) 与 u5-l2 讲义中的 Carina 数据表。
   - 按 §4.4.2 的六要素（环境/口径/数据/机制/适用范围/脚本）起草一段不超过 200 字的结论。
   - 自查：有没有出现「纹理内存更快」这类无限定陈述？两台机器的结论冲突时是否明确写出「依赖架构代际」？
3. **需要观察的现象**：同一组数据，加限定语前后结论的「可引用性」差异。
4. **预期结果**：合格版本应形如「在完美合并的顺序 AXPY 负载上，纹理路径相对全局内存路径的收益强烈依赖 GPU 架构：Carina（V100）上为 -1%~-4%（噪声级），Fornax 上为正收益；因此『只读数据放纹理』是条件命题而非通用规则，现代替代是 `__ldg()`/只读缓存」。具体百分比以你复核归档为准。

#### 4.4.5 小练习与答案

**练习 1**：test2.sh 为什么把 n 设成 stride 的 1024 倍而不是固定 stride=1024 只放大 n？

**答案**：固定 stride 放大 n 会同时放大「触碰元素数」（\(n/s\) 随 n 增长），计算量和迁移页数都变，出现两个变量；而 \(n=1024s\) 使触碰元素数恒为 1024，唯一变化的是离散路径的整体搬运量——单一变量的对照设计。

**练习 2**：你的机器上复现 BankRedux 得到「bc 版只慢 3%」，与 Carina 的 12%~24% 矛盾。列出至少四种可能的解释，按可能性排序。

**答案**：(1) 用了程序打印的 wall time 而非 kernel 行（wall time 被 cudaMalloc 摊销项淹没，差异被稀释——最常见）；(2) GPU 架构不同，共享内存 bank 组织或冲突处理不同（如某些架构对广播/多播的优化）；(3) 编译选项不同（如开了影响访存调度的优化，或 `-G` 反而抹平）；(4) n 太小，冲突代价未超过噪声标尺（§4.2.3(c) 的 1.5%~4%）。排查顺序即按此排列：先核对口径，再核对环境。

**练习 3**：nvprof 已在 CUDA 11 之后被 nsys/ncu 取代。本讲的哪些方法能直接迁移，哪些要换工具？

**答案**：可直接迁移的是「方法论」——Calls 指纹鉴定、三类时间分离、噪声标尺、口径审计、相对结论原则，这些只依赖「表格有 Calls/Time 列」这一结构。要换工具的是具体采集：概览与 API 时间对应 `nsys profile` + `nsys stats`，kernel 行与硬件计数器对应 `ncu --set full` 或 `ncu --metrics`（如 branch_efficiency），统一内存迁移表对应 nsys 的 `cuda_gpu_mem_time_sum`/UM 相关统计。项目 Makefile 无需改动，只改测量命令。

## 5. 综合实践

**任务：在本机复现 BankRedux 与 UniMem，与仓库自带的 Carina/Fornax 归档并排成表，完成一次规范的跨平台报告。**（本环境无 GPU，以下为完整操作规程，数值均标注「待本地验证」；无 GPU 的读者可把「本机」列替换为「从归档提取的第三组数据」，同样完成全部分析步骤。）

### 5.1 实践目标

把本讲四个模块（归档鉴定、warmup/平均拆账、口径审计、报告写作）串成一次完整演练。

### 5.2 操作步骤

**第一步：建立环境清单。** 记录 GPU 型号（`nvidia-smi --query-gpu=name,driver_version,clocks.max.sm --format=csv`，示例命令）、CUDA 与 nvprof 版本（`nvcc --version`、`nvprof --version`）、是否锁频（如 `nvidia-smi -lgc <freq>`，示例命令，需管理员权限；无法锁频则如实记录「未锁频」——记住 Fornax 的 auto boost 警告）。

**第二步：复现 BankRedux。**

1. `cd BankRedux && make`（编译命令见 [Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/Makefile#L1-L2)）。
2. 按 [test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/test.sh#L1-L4) 的四个规模分别运行 `./sum_cuda <n>` 与 `nvprof ./sum_cuda <n>`，记录：程序打印的 time、kernel 行（三个 kernel 的 Avg）、`cudaMalloc` 行的 Max。
3. 用 §4.2.3(b) 的公式计算本机的 \(T_{\text{first}}/R\) 占比；用 warmingup 与 sum_cudakernel 的差标定本机噪声标尺；计算 bc 版相对减速比。

**第三步：复现 UniMem。**

1. `cd UniMem && make`。
2. 选 test.sh 的一个子集（建议 stride ∈ {1, 2, 4, 8, 32, 65536, 134217728}，全跑 28 档太慢），每档记录两行打印的 time。
3. 对 stride=1 与 stride=134217728 两档加跑 nvprof，记录 kernel 行、memcpy 行、迁移表三处数字。
4. 跑一遍 [test2.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/test2.sh#L1-L21) 的首尾两档（n=1024 与 n=1073741824），记录两路径时间。

**第四步：并排成表。** 模板：

| 基准/指标 | Carina（归档） | 本机 | Fornax（归档，仅 SpMM 有） |
| --- | --- | --- | --- |
| BankRedux wall time（n=1024000） | 48.30 ms | 待本地验证 | — |
| BankRedux sum_cudakernel | 14.723 µs | 待本地验证 | — |
| BankRedux bc 版减速 | +23.9% | 待本地验证 | — |
| BankRedux cudaMalloc Max | 240.14 ms | 待本地验证 | — |
| UniMem 离散/统一（s=1） | 121.54 / 172.70 ms | 待本地验证 | — |
| UniMem 交叉点 | s∈(2,4) | 待本地验证 | — |
| UniMem 固定访问量末档 | 1199.85 / 104.25 ms | 待本地验证 | — |

**第五步：写分析。** 完成三件事：

1. 列出**至少三处**可能造成你与 Carina 差异的 Factors，并说明每一处影响哪一列数字。参考答案（任选三）：① GPU 架构代际（V100 vs 你的卡——影响 kernel 行与迁移表，如 HBM 带宽、缺页迁移粒度）；② 驱动/工具链版本（影响 API 表与 `Calls` 指纹的可比性，见 §4.1.3）；③ 时钟状态（未锁频时 wall time 与 kernel 行都会抖，Fornax 的 auto boost 警告是现成证据）；④ 二进制/参数版本（当前 `num_runs=100` vs 归档的 10——影响调用次数与摊销比例）；⑤ 计时口径（你是否用了 kernel 行而归档某些行是 wall time——直接不可比）；⑥ profiler 扰动（nvprof 下统一路径膨胀 36.8% 的归档证据）。
2. 给出「哪个 kernel 更快」的判定依据：**必须引用 kernel 行时间（必要条件）+ 噪声标尺判显著性 + 机制解释**。例如 BankRedux 的正确句式是：「`sum_cudakernel` 快于 `sum_cudakernel_bc` 12%~24%（kernel 行，Carina 四个规模），超过同源 kernel 1.5%~4.3% 的噪声标尺，机制是交错寻址的 2~8 路 bank 冲突（u4-l5）」；而「wall time 更小」不能作为依据（§4.2.3(b) 已证明 wall time 对被测变量几乎不敏感）。
3. 写一句结论适用范围声明，格式：「在 <GPU 型号、驱动、CUDA 版本、锁频状态、n 范围、口径=kernel 行> 条件下成立；未验证 <维度>」。

### 5.3 需要观察的现象

- 本机 wall time 是否同样对 n 不敏感、且被 `cudaMalloc` Max 摊销项主导；
- 本机 bc 版减速是否落在两位数百分比、是否超过噪声标尺；
- UniMem 交叉点位置与本机迁移表的 Count/Avg Size 与 Carina 的 63050 次 / 45 KB 是否同量级；
- profiler 前后两行打印的膨胀率是否也是「统一路径膨胀更多」。

### 5.4 预期结果

- BankRedux：kernel 行随 n 线性增长，bc 版减速为正且随 n 增大；wall time 数值与 Carina 不同（跨平台）但「远大于 kernel 合计」的结构性关系应复现。
- UniMem：离散时间对 stride 不敏感（水平线）、统一时间随 stride 下降，存在交叉点；固定访问量实验中离散随 n 线性、统一进入平台期。
- 以上趋势性结果的可复现性高于绝对数值——这正是本讲的核心论点。所有具体数值待本地验证。

## 6. 本讲小结

- **归档转录是云实验数据，但要先做法医学鉴定**：nvprof 的 `Calls` 列是二进制指纹——用它揪出了 Fornax SpMM 归档缺 warmingup（旧版源码）、UniMem 归档的 `num_runs` 是 10 而当前源码是 100，且归档用的脚本并未回传仓库。
- **warmup 与多轮平均防不住计时窗口内的首次 `cudaMalloc`**：Carina 上 308.16 ms 的上下文创建摊到 10 次平均后仍占墙钟 56.80 ms 的 54%，被测 kernel 只占 0.02%——wall time 对被测变量不敏感时，它测的就不是你想测的东西。
- **三类时间三个故事**：墙钟讲用户等待、GPU activities（kernel 与 memcpy 分开数）讲设备行为、API calls 讲主机开销；判断 kernel 快慢只认 kernel 行，且要用「同源 twin kernel」的差（1.5%~4.3%）当噪声标尺。
- **口径要对齐再比较**：UniMem 两条路径一个含释放一个不含、一个排除主机填充一个不排除；SpMM 干脆不打印时间、全靠 profiler——报告必须写清每行数字的窗口边界。
- **跨平台只有相对结论可比**：SpMM 的 CSC 加速比在两台工具链、二进制版本都不同的机器上同为 52×/69×（保序），而绝对时间差 7.4 倍；profiler 本身还会系统性地偏袒被观测更重的路径（统一内存膨胀 36.8% vs 离散 15.8%）。
- **对照设计与报告六要素**：test2.sh 用 \(n=1024s\) 固定触碰元素数隔离传输变量，是仓库里最好的单一变量范本；报告须含环境清单、口径声明、数据、机制解释、适用范围与真实脚本。

## 7. 下一步学习建议

本讲是学习手册的收官。三条继续深入的路径：

1. **回到论文**：带着本讲的方法论重读 README 引用的 IPDPSW 2021 论文（[README.md:L119](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L119)），核对论文中的表格如何报告环境与口径，与本讲 §4.4.2 的清单逐项对照。
2. **产出你自己的基准**：把 §5 的复现报告与 u6-l2 的 strided-saxpy 基准合并成一份完整实验报告（环境清单 + 单变量设计 + 三层证据 + 适用范围），这几乎是任何 GPU 性能论文实验章节的微缩版。
3. **工具链迁移**：在本机用 `nsys profile` / `ncu --set full` 重做 §5 的测量，练习把「Calls 指纹、三类时间、噪声标尺」映射到新工具的输出；再选读 CUDA 官方文档中 profiling 与锁频（clock locking）章节，把 Fornax 的 auto boost 警告变成你实验流程里的一条固定检查项。

至此，你已完成从「跑通第一个微基准」到「能独立设计、测量、报告一个 CUDA 微基准」的全部旅程。

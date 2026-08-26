# uBench 是什么：FPGA 内存系统微基准的动机与全景

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 uBench 的定位：一套基于 HLS 的微基准（microbenchmark）套件，用来**定量测量** Xilinx Alveo U200（DDR4）、Alveo U280（HBM2）和 ZCU104（嵌入式 DDR4）三类 FPGA 平台的内存系统性能。
2. 列出影响内存带宽的五个关键因素：**内核频率、并发内存端口数、端口数据位宽、最大突发访问长度、连续访问数据量**，并能指出每个因素在仓库源码中的定义位置。
3. 区分仓库中的三类微基准（片外带宽、流带宽、片外延迟）与两个案例研究（KNN、SpMV），知道它们各自扫描哪些参数、输出什么指标。

本讲是整本手册的第一讲，不要求你已经会写 HLS 代码或 OpenCL 主机程序——那些是第二单元的内容。本讲只要求你建立一张「项目全景地图」。

## 2. 前置知识

在开始之前，用通俗语言解释几个本讲会反复出现的术语。不需要任何 FPGA 开发经验。

- **FPGA（现场可编程门阵列）**：一种可以「用代码重新 wiring」的芯片。你用硬件描述语言（或本仓库使用的 HLS）写出的程序，最终会变成 FPGA 上的电路。它在数据中心里常被用作加速器（accelerator）。
- **Alveo U200 / U280**：Xilinx 的数据中心 FPGA 加速卡。U200 使用 **DDR4** 内存（大容量、传统通道结构）；U280 使用 **HBM2** 内存（很多个并行的小 bank 堆叠在芯片旁，带宽潜力更大）。可以类比为：DDR 像「少数几条很宽的高速公路」，HBM 像「很多条并行的普通公路」。
- **ZCU104**：一块嵌入式开发板（Zynq UltraScale+ MPSoC），上面有 ARM CPU 和 FPGA。它代表「嵌入式 FPGA」场景，内存通过 HP/HPC 端口连接，与 Alveo 的内存 bank 模型不同。
- **片上存储 vs 片外存储**：FPGA 内部有 BRAM/URAM（快但容量小，KB~MB 级）；FPGA 外部的 DDR/HBM（慢一个量级但容量是 GB 级）。**数据在片外内存和内核之间搬运的速度，往往决定了整个加速器的性能上限**——这正是 uBench 要测的东西。
- **HLS（高层次综合）**：用 C/C++ 写内核，由 Vitis HLS 工具自动综合成电路。本仓库所有内核都是 `*.cpp` 文件加 `#pragma HLS` 编译指示写成的。
- **AXI 突发传输（burst）**：内核访问片外内存走 AXI 协议。一次「突发」可以在一个地址基础上连续搬运多个数据（burst length），比一个一个地址去取效率高得多。类比：去仓库搬货，「一次拿一箱」和「一次拿 16 箱」的差别。
- **主机程序（host program）**：运行在 CPU 上的 C++ 程序，负责把比特流加载到 FPGA、准备数据、启动内核、计时并打印结果。本仓库用 OpenCL/XRT API 写主机程序。
- **微基准（microbenchmark）**：不跑完整应用，而是用一个极小的、目的单一的测试程序隔离测量系统的某一项性能（如「读带宽」）。它是计算机体系结构研究的经典手段——CPU 世界里的 STREAM 基准就是例子。

## 3. 本讲源码地图

本讲以阅读和定位为主，涉及的文件如下：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目总入口：项目定位、五个带宽因素、三类微基准与两个案例的描述、论文引用 |
| `ubench/offchip_bandwidth/datacenter/README.md` | 片外带宽微基准的详细使用说明，含六节「手动改参数」指南 |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h` | 读带宽示例工程的内核配置头：位宽、迭代次数 |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp` | 读带宽示例的主机程序：端口数宏、payload 扫描循环、带宽公式 |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp` | 读带宽内核：突发长度 pragma 所在处 |
| `ubench/offchip_bandwidth/datacenter/auto_collect/config.py` | 自动生成流水线的参数空间定义（五因素 + 访问类型 + 内存类型） |
| `ubench/streaming_bandwidth/datacenter/2ports_512bit/src/krnl_config.h` | 流带宽工程的配置头（多了流类型 `pkt`） |
| `ubench/streaming_bandwidth/datacenter/2ports_512bit/src/host.cpp` | 流带宽主机程序：更大的 payload 上限 |
| `ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_config.h` | 延迟工程的配置头（32bit 窄端口） |
| `ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp` | 延迟主机程序：随机访问数组大小扫描 |
| `case_study/KNN/README.md`、`case_study/SpMV/README.md` | 两个案例研究的说明（各含四个设计变体目录） |

## 4. 核心概念与源码讲解

### 4.1 微基准测试动机

#### 4.1.1 概念说明

一句话概括 uBench 的动机：**软件程序员想把算法搬到 FPGA 上时，往往不知道这片 FPGA 的内存系统到底能跑多快、在什么条件下才能跑满**。数据手册给出的是理论峰值，而实际可达带宽取决于内核怎么写（端口多宽、突发多长、访问多连续）。uBench 用一组可复现、可调参的微基准把这件事量化出来。

README 开头一句就是项目的正式定义：

> uBench is a set of HLS-based microbenchmarks to quantitatively evaluate the performance of the Xilinx Alveo FPGA memory systems...

这个动机来自 SFU HiAccel 实验室发表在 **FPGA 2021**（ACM/SIGDA 国际 FPGA 研讨会）的论文。论文通过这套微基准系统性地「祛魅」（demystify）了现代数据中心 FPGA 的内存系统，并给出了面向软件程序员的优化指南。仓库中的两个案例研究（KNN、SpMV）就是论文中「把内存洞察用于真实加速器设计」的实证部分。

为什么要专门做这件事？三点直觉：

1. **内存是瓶颈**：FPGA 上的计算单元可以堆很多，但数据要从片外 DDR/HBM 搬进来。搬运速度不够，算力就是摆设。
2. **理论峰值不可达**：标称带宽是「频率 × 位宽 × 通道数」的理想值，实际还受突发长度、访问连续性、地址映射到哪个 bank 等因素制约。
3. **没有现成工具**：CPU 有 STREAM、lmbench 等成熟的内存基准；FPGA 侧缺少一套公开、系统、可复现的对应物——uBench 填补的就是这个空缺。

#### 4.1.2 核心流程

uBench 的方法论就是经典的**控制变量法**，每个微基准工程的执行流程可以抽象为：

```text
for 每个参数组合（频率 / 端口数 / 位宽 / 突发长度 / 连续数据量）:
    1. （可能需要）重新生成/修改内核与主机代码
    2. Vitis v++ 编译内核 → 链接 → 生成 xclbin 比特流
    3. 主机程序加载 xclbin，把缓冲区绑定到指定内存 bank
    4. 主机按 payload 从小到大循环，启动内核并计时
    5. 输出该参数组合下的带宽（GB/s）或延迟
```

改参数有两条路：

- **手动路径**：拷贝一个示例工程目录，按各微基准 README 的指南逐处修改源码；
- **自动路径**：用 `auto_collect/` 下的 Python 脚本，在 `config.py` 里声明参数取值列表，脚本自动生成整批工程并产出 `runAll.sh` 批跑脚本。

#### 4.1.3 源码精读

先看根 README 中项目的自我定义与论文引用：

[README.md:3-6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/README.md#L3-L6)

这两段做了两件事：第 3 行给出项目的一句话定义，并列出影响内存带宽的五个因素（下一节详讲）；第 5-6 行给出论文引用信息（作者、会议、年份），这是学术上「这个基准套件的结论出处」。

再看被测硬件平台与软件工具链：

[README.md:14-26](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/README.md#L14-L26)

这里声明了三类目标硬件（U200=DDR4 云端卡、U280=HBM2 云端卡、ZCU104=嵌入式板）和两套软件（Vitis 2020.2 与 XRT 2020.2）。**记住版本号很重要**：本仓库所有 Makefile、pragma 写法都以 Vitis 2020.2 为准，换版本可能出现行为差异。

最后看「三类微基准 + 两个案例」的总述段落：

[README.md:30-58](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/README.md#L30-L58)

这一大段是仓库的「目录说明书」：第 32-38 行讲片外带宽（读/写 × DDR/HBM 各有示例工程），第 40-46 行讲流带宽，第 48-50 行讲片外延迟，第 52-58 行讲 KNN 与 SpMV 两个案例研究。我们将在 4.3 节逐类展开。

> ⚠️ 一个阅读小坑：根 README 第 50 行的延迟微基准链接指向 `ubench/off-chip_latency`，但仓库中实际目录名是 `ubench/offchip_latency`（无连字符）。按链接点会 404，自己 `ls` 一下即可找到。

#### 4.1.4 代码实践

**实践目标**：熟悉根 README 的信息结构，学会从仓库文档定位关键事实。

**操作步骤**：

1. 打开仓库根目录的 `README.md`，通读一遍（只有 71 行）。
2. 用下面的命令只看各级小节标题：

   ```bash
   grep -n "^## " README.md
   ```

3. 找到论文引用（第 5-6 行），记下会议名称与年份。
4. 用 `ls ubench/` 与 `ls case_study/` 验证 README 中提到的目录都真实存在。

**需要观察的现象**：

- `grep` 会列出 `## Introduction`、`## Environmental Setup`、`## FPGA Memory System Microbenchmarking using uBench`、`## Case Study Benchmarking Algorithms` 等小节，与 4.1.3 中的行号区间对应。
- `ls ubench/` 的输出是 `offchip_bandwidth  offchip_latency  streaming_bandwidth`——注意没有 `off-chip_latency` 这种带连字符的目录，印证上面的链接小坑。

**预期结果**：你能在 1 分钟内从 README 回答三个问题——项目测什么（FPGA 内存系统）、在什么硬件上测（U200/U280/ZCU104）、结论发表在哪里（FPGA 2021 论文）。

#### 4.1.5 小练习与答案

**练习 1**：uBench 测的是「FPGA 计算逻辑的性能」还是「FPGA 内存系统的性能」？依据是什么？

<details>
<summary>参考答案</summary>

是内存系统的性能。依据是 README 第 3 行的定义：uBench 定量评估的是 "the Xilinx Alveo FPGA memory systems"，其内核做的只是搬运数据（读、写、流转发、随机访问），几乎不做有意义的计算。
</details>

**练习 2**：为什么论文标题里强调 "for Software Programmers"？

<details>
<summary>参考答案</summary>

因为目标读者是习惯 CPU/GPU 编程、想把程序移植到 FPGA 的软件人员。他们不了解 FPGA 内存层次（DDR/HBM bank、AXI 突发、端口位宽）对性能的影响；微基准把这些影响量化成可查的表格和曲线，指导他们做端口数量、位宽、突发长度等设计决策（案例研究 KNN/SpMV 就是示范）。
</details>

### 4.2 五个带宽影响因素

#### 4.2.1 概念说明

README 第 3 行列出的五个因素，是贯穿整个仓库的「世界观」，每个目录名、每个配置项都围绕它们展开：

| # | 因素 | 直觉解释 | 理论上的作用 |
| --- | --- | --- | --- |
| 1 | **加速器时钟频率** | 电路每秒跳多少拍 | 频率翻倍，每拍能发起的访问次数不变但每秒总次数翻倍 |
| 2 | **并发内存端口数** | 多少条独立的 AXI 通道同时搬数据 | 近似线性扩展带宽，直到内存控制器饱和 |
| 3 | **端口数据位宽** | 每拍每条通道搬多少比特（32/64/128/256/512bit） | 位宽翻倍，每拍数据量翻倍 |
| 4 | **最大突发长度** | 一次突发连续搬多少拍数据（AXI burst length） | 越长，地址/命令开销摊薄得越充分 |
| 5 | **连续访问数据量** | 内核一次连续读/写多大的数据块（1KB~1MB） | 太短则突发还没加速完成就结束，达不到峰值 |

前三个因素决定**理论峰值**；后两个因素决定**实际能达到峰值的百分比**。

#### 4.2.2 核心流程

理想情况下，读带宽的理论上限是：

\[ BW_{peak} = f \times N_{port} \times W \]

其中 \( f \) 是内核频率（Hz），\( N_{port} \) 是并发端口数，\( W \) 是每个端口的位宽（bit）。例如 300MHz × 2 端口 × 512bit：

\[ BW_{peak} = 300 \times 10^6 \times 2 \times 512\ \text{bit/s} = 38.4\ \text{GB/s} \]

但实测带宽 \( BW_{actual} \) 通常低于理论值，定义带宽利用效率：

\[ \eta = \frac{BW_{actual}}{BW_{peak}} \]

因素 4 和 5 通过两种机制压低 \( \eta \)：

- **命令开销摊薄不足**（因素 4）：每次 AXI 突发都有固定的地址与握手开销。若突发长度为 \( B \)，则开销近似被摊到 \( B \) 拍数据上，\( B \) 越小固定开销占比越高。
- **加速爬坡被截断**（因素 5）：内存控制器和流水线需要若干拍才能进入满速状态。若一次连续访问只有 \( S \) 字节，爬坡时间 \( t_{ramp} \) 占总时间 \( t \) 的比例随 \( S \) 增大而减小；\( S \) 太小（如 1KB）时大部分时间都在「起步」。

uBench 的实验方式就是固定其中四个因素、扫描第五个，从而画出「带宽 vs 某因素」的曲线。这也是每个示例工程里 payload 循环存在的原因。

#### 4.2.3 源码精读

五个因素在示例工程 `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/`（目录名本身就是参数组合：读方向、DDR 内存、2 端口、512bit 位宽）中各有落点。

**因素 3（位宽）与放大系数**——配置头文件：

[src/krnl_config.h:4-7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L4-L7)

`DWIDTH = 512` 定义端口位宽为 512bit；`INTERFACE_WIDTH` 即 `ap_uint<512>`，是内核参数使用的任意精度宽类型；`WIDTH_FACTOR = DWIDTH/32 = 16` 表示一个宽端口数据相当于 16 个 32bit int（主机程序换算数据量时要用它）；`NUM_ITERATIONS = 10000` 让内核把同一批数据反复访问一万次，把执行时间放大到主机计时精度之上。

**因素 2（端口数）**——主机程序顶部的宏：

[src/host.cpp:15-16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L15-L16)

`NUM_KERNEL = 1` 表示 1 个内核实例，`NUM_PORT = 2` 表示该内核有 2 个并发内存端口——与目录名 `2ports_512bit` 一一对应。主机为 `NUM_KERNEL*NUM_PORT` 个端口各分配一个绑定到指定 bank 的缓冲区。

**因素 4（突发长度）**——内核源码中的 INTERFACE pragma：

[src/krnl_ubench.cpp:5-6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L5-L6)

`m_axi` 编译指示把 `in0`/`in1` 声明为 AXI 主端口（各占一个 `bundle=gmem0`/`gmem1`），`max_read_burst_length=16` 就是最大读突发长度——本示例用的是 Vivado HLS 默认值 16，把它改成 256 是论文中最有效的优化之一（案例研究中 suboptimal 与 optimal 设计的唯一差异就在这里）。

**因素 5（连续访问数据量）**——主机程序的 payload 扫描循环：

[src/host.cpp:100-104](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L100-L104)

`payload` 以 256 个 int（1KB）起步、每次翻倍，直到 262144 个 int（1MB）。每一档 payload 就是一次「连续访问数据量」实验；仿真模式（`xcl::is_emulation()`）下固定为 1KB 以缩短仿真时间。第 172 行的带宽公式 `payload * 4 * 0.000010000 / kernel_time_in_sec * NUM_KERNEL * NUM_PORT` 把测得时间换算成 GB/s（其中 `0.000010000` 隐含了 `NUM_ITERATIONS = 10000` 次重复）。

**因素 1（频率）**——示例工程的 Makefile 里默认不指定频率（用工具默认值），手动修改方法记录在微基准 README 中：

[ubench/offchip_bandwidth/datacenter/README.md:74-78](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/README.md#L74-L78)

即在链接阶段给 `v++` 传 `--kernel_frequency 300`。该 README 的六节指南（第 4、33、39、48、54、74 行起的 1~6 节）分别对应：端口数、位宽、突发长度、连续数据量、DDR/HBM 连接与内核布局、频率——正好是五个因素外加连接配置。

**自动化路径中的五因素**——`auto_collect` 的参数空间定义：

[auto_collect/config.py:6-14](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/config.py#L6-L14)

七个配置项中前五个（`KERNEL_FREQ`、`NUM_CONCURRENT_PORT`、`PORT_WIDTH`、`MAX_BURST_LENGTH`、`CONSECUTIVE_DATA_SIZE`）与五因素一一对应；后两个（`ACCESS_TYPE`、`MEMORY_TYPE`）额外选择读/写方向与 DDR/U200、HBM/U280 平台。脚本对所有取值做笛卡尔积，每个组合生成一个完整工程目录。频率如何进入构建可参见生成器：

[auto_collect/makefile_gen.py:75](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py#L75)

这一行把 `config.py` 里的 `KERNEL_FREQ` 拼进生成的 Makefile 的 `--kernel_frequency` 链接选项。

#### 4.2.4 代码实践

**实践目标**：亲手在仓库中把「五个因素」各自映射到具体文件和行号，建立参数-源码的条件反射。

**操作步骤**：

1. 进入示例工程目录：

   ```bash
   cd ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit
   ```

2. 依次执行以下定位命令并记录输出：

   ```bash
   grep -n "DWIDTH\|WIDTH_FACTOR\|NUM_ITERATIONS" src/krnl_config.h   # 因素3
   grep -n "NUM_PORT\|NUM_KERNEL" src/host.cpp | head -4              # 因素2
   grep -n "max_read_burst_length" src/krnl_ubench.cpp                # 因素4
   grep -n "for (int payload" src/host.cpp                            # 因素5
   grep -n "kernel_frequency" ../../README.md                         # 因素1
   ```

3. 打开 `ubench/offchip_bandwidth/datacenter/auto_collect/config.py`，把七个配置项抄进你的笔记，并标注哪些对应五因素、哪些是额外维度。

**需要观察的现象**：

- 四条 grep 分别命中 `krnl_config.h:4-7`、`host.cpp:15-16`、`krnl_ubench.cpp:5-6`、`host.cpp:100`，与 4.2.3 的讲解一致。
- 最后一条 grep 命中 datacenter README 第 78 行的 `--kernel_frequency 300`。

**预期结果**：你得到一张「因素 → 文件:行号」对照表（本讲综合实践的半成品）。注意 `NUM_PORT` 在 `host.cpp` 中是 `#define` 宏，而端口真正出现在内核函数签名 `krnl_ubench.cpp` 的 `in0`/`in1` 参数上——改端口数要两处联动，这个细节在第三单元会展开。

#### 4.2.5 小练习与答案

**练习 1**：按 4.2.2 的公式计算「300MHz、2 端口、每端口 512bit」的理论峰值带宽，并说明它与 `host.cpp:172` 实测公式的区别。

<details>
<summary>参考答案</summary>

理论峰值 \( 300\times10^6 \times 2 \times 512 / 8 = 38.4 \) GB/s。区别在于：理论公式是「每拍都满载」的上限；`host.cpp:172` 的公式用实测时间换算，时间中包含内核启动开销与流水爬坡，因此结果只会低于或接近理论上限，两者的比值就是利用效率 \( \eta \)。
</details>

**练习 2**：如果只把 `krnl_config.h` 中的 `DWIDTH` 从 512 改成 256，`WIDTH_FACTOR` 会变成多少？主机端「同一 payload 对应的宽口拍数」怎么变？

<details>
<summary>参考答案</summary>

`WIDTH_FACTOR = DWIDTH/32 = 256/32 = 8`。主机传给内核的 `size = dataSize / WIDTH_FACTOR`（流带宽工程 `host.cpp:182` 的写法），因此同一 `payload` 下宽口拍数翻倍——位宽减半后需要两倍拍数搬运同样字节，这正是因素 3 影响带宽的机制。
</details>

**练习 3**：为什么 `NUM_ITERATIONS` 要设成 10000 这样的大数？

<details>
<summary>参考答案</summary>

微基准用主机端的 `std::chrono` 高精度时钟对「入队到 `q.finish()` 返回」计时。若内核只跑几微秒，内核启动开销（数十微秒量级）会淹没真实访存时间；让内核把同样大小的数据块重复访问 10000 次，可把访存时间放大到远大于启动抖动，使测得的带宽更接近稳态值。
</details>

### 4.3 三类微基准与两个案例研究

#### 4.3.1 概念说明

仓库 `ubench/` 下按「测什么」分为三类，`case_study/` 下是两个案例：

| 类别 | 目录 | 测量指标 | 扫描的参数 |
| --- | --- | --- | --- |
| **片外带宽** | `ubench/offchip_bandwidth/` | 内核 ↔ 片外 DDR/HBM 的读/写带宽（GB/s） | 五因素全扫 + 读/写方向 + DDR/HBM |
| **流带宽** | `ubench/streaming_bandwidth/` | 内核 ↔ 内核之间 AXIS 流的带宽（GB/s） | 频率、流端口数、位宽、流数据量（无突发概念） |
| **片外延迟** | `ubench/offchip_latency/` | 随机访问片外内存的平均延迟 | 端口位宽、随机访问数组大小 |
| **案例：KNN** | `case_study/KNN/` | 端到端加速器性能 | 不扫描——四个固定配置的设计变体 |
| **案例：SpMV** | `case_study/SpMV/` | 端到端加速器性能 | 不扫描——四个固定配置的设计变体 |

每类微基准内部再按「数据中心（datacenter）/ 嵌入式（embedded）」分两层，datacenter 又按方向（read/write）和内存类型（DDR/HBM）细分。**目录名即参数组合**：比如 `read/HBM/2ports_512bit` 表示「读方向 + HBM + 2 端口 + 512bit 位宽」。

两个案例研究的作用是「验收」：用微基准得到的洞察（如突发 256 才能跑满、512bit 宽口性价比最高）指导 KNN/SpMV 加速器的端口与 PE 设计，各提供 baseline / suboptimal / optimal / aggressive 四个设计点作对照。

#### 4.3.2 核心流程

三类微基准的测量流程同构，差异只在「内核做什么」：

```text
片外带宽:  主机准备连续缓冲区 → 内核 for j: temp = in[j]（或 out[j]=常量）→ 计时 → GB/s
流带宽:    两个内核成对（streamWrite → AXIS 流 → streamRead）→ 计时 → GB/s（×2 系数）
片外延迟:  主机生成打乱的下标数组 → 内核按下标随机访问 → 总时间/访问次数 → 延迟
```

仓库为每类微基准提供了**手动示例工程**（一个固定参数组合的完整目录）和**自动生成脚本**（`auto_collect/`，声明参数列表后批量生成工程），两条路在 4.1.2 已述。

#### 4.3.3 源码精读

**片外带宽**——README 对其参数与示例配置的描述：

[README.md:32-38](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/README.md#L32-L38)

五因素齐全，示例为 300MHz / 2 端口 / 512bit / 突发 16（默认值）/ 连续 1KB~1MB，并给出手动与自动两条调参路径的链接。其 payload 扫描即 4.2.3 精读过的 [src/host.cpp:100](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L100)。

**流带宽**——README 描述与主机循环：

[README.md:40-46](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/README.md#L40-L46)

流带宽只受四个参数影响（频率、并行流端口数、位宽、流数据量）——**没有突发长度**，因为数据在片上流通道直传、不经过内存控制器。主机侧 payload 上限放大了 4 倍：

[streaming/.../src/host.cpp:108-112](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/src/host.cpp#L108-L112)

上限 `262144*4` 个 int（4MB/轮），配合内核内部 `NUM_ITERATIONS = 10000` 次重复（见 [krnl_streamWrite.cpp:19-26](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/src/krnl_streamWrite.cpp#L19-L26) 的双层循环），实现 README 所说的「~10MB 到 ~10GB」流数据量。流接口类型定义在配置头中：

[streaming/.../src/krnl_config.h:7-11](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/src/krnl_config.h#L7-L11)

`pkt` 即 `ap_axiu<512,0,0,0>`，是带侧带信号的 AXIS 数据包类型——它与 `hls::stream<pkt>` 一起构成内核间的流端口（第四单元详讲）。

**片外延迟**——README 描述：

[README.md:48-50](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/README.md#L48-L50)

只扫两个参数：每次访问位宽（示例 32bit）与随机访问数组大小（64B~1MB）。配置头证实了窄端口：

[latency/.../src/krnl_config.h:4-7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_config.h#L4-L7)

`DWIDTH = 32`（目录名 `32bit_per_access` 即由此来），`WIDTH_FACTOR = 1`。主机侧数组大小扫描循环：

[latency/.../src/host.cpp:99-101](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp#L99-L101)

payload 从 16 个 int（64B）起步翻倍到 262144 个 int（1MB），与 README 的「64B 到 1MB」一致。

**两个案例研究**——KNN 说明：

[case_study/KNN/README.md:1-4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/README.md#L1-L4)

KNN 来自 Rodinia 基准套件，目录下有 `baseline_14PE`、`suboptimal_14PE`、`optimal_14PE`、`aggressive_11PE` 四个设计变体（后缀数字是处理单元数量）。SpMV 说明：

[case_study/SpMV/README.md:1-3](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/README.md#L1-L3)

SpMV 来自 MachSuite 套件，四个变体为 `baseline_30PE`、`suboptimal_4PE`、`optimal_4PE`、`aggressive_4PE`。两个 README 都指向论文中的表 2 与表 4 获取详细配置——这也是本讲综合实践要求你结合论文阅读的原因。

#### 4.3.4 代码实践

**实践目标**：用几条只读命令把仓库的「三类微基准 × 平台 × 参数组合」目录结构数清楚。

**操作步骤**：

```bash
# 1. 片外带宽的所有示例工程目录（datacenter 侧）
find ubench/offchip_bandwidth/datacenter -mindepth 3 -maxdepth 3 -type d

# 2. 三类微基准各自的 auto_collect 脚本族
find ubench -type d -name auto_collect | sort

# 3. 两个案例研究的设计变体目录
ls case_study/KNN case_study/SpMV

# 4. 对比三处 payload 循环的起点与上限
grep -n "for (int payload" \
  ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp \
  ubench/streaming_bandwidth/datacenter/2ports_512bit/src/host.cpp \
  ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp
```

**需要观察的现象**：

- 第 1 条命令输出 4 个示例目录（read/DDR、read/HBM、write/DDR、write/HBM 各一个 `2ports_512bit`）。注意 `auto_collect` 位于第 2 层深度，不会被 `-mindepth 3` 匹配到。
- 第 2 条命令输出 4 个 `auto_collect` 目录：offchip_bandwidth 与 streaming_bandwidth 各自的 datacenter、embedded 版本。**offchip_latency 没有自动生成脚本**，只有手工示例工程——这是三类微基准中的例外。
- 第 3 条命令输出两个案例各 4 个变体目录加一个 README。
- 第 4 条命令显示三处循环分别为 `256..262144`、`256..262144*4`、`16..262144`。

**预期结果**：你能不假思索地回答「仓库里有几类微基准、各在哪些平台有示例、自动脚本覆盖哪几类」。以上输出均已在本仓库当前 HEAD（`57fc9b5`）下核实。

#### 4.3.5 小练习与答案

**练习 1**：流带宽微基准为什么**不**扫描突发长度，而片外带宽微基准要扫？

<details>
<summary>参考答案</summary>

突发（burst）是 AXI 总线访问**片外内存控制器**时的机制：一次地址握手搬运多拍数据以摊薄命令开销。流带宽测的是内核到内核的片上 AXIS 流直连通路，数据不经内存控制器，逐拍直传，没有「突发长度」这个旋钮；README 第 42 行列出的流带宽参数（频率、流端口数、位宽、流数据量）也没有它。
</details>

**练习 2**：目录 `ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit` 与 `read/DDR/2ports_512bit` 的**内核源码**预计几乎相同，差异主要会在哪里？

<details>
<summary>参考答案</summary>

差异主要在**连接配置与主机 bank 绑定**：`ubench.ini` 中 `sp` 行把端口接到 `DDR[0]` 还是 `HBM[0]`，以及 `host.cpp` 中 `cl_mem_ext_ptr_t` 的 flags 用 `XCL_MEM_DDR_BANK*` 还是 HBM 的 topology 标志。内核本身只是两个读端口循环，与内存类型无关（第三单元会用 diff 验证这一点）。
</details>

**练习 3**：KNN 的四个变体目录名后缀为什么是 `14PE/14PE/14PE/11PE` 而 SpMV 是 `30PE/4PE/4PE/4PE`？猜测其含义。

<details>
<summary>参考答案</summary>

`PE` 指 Processing Element（处理单元/内核实例数）。KNN 的 baseline/suboptimal/optimal 用 14 个窄端口 PE，aggressive 改用 11 个 512bit 宽端口 PE（端口变宽后不需要那么多 PE 就能吃满带宽，甚至受资源限制要减少）；SpMV 的 baseline 用 30 个 PE 铺满 4 个 DDR bank，其余三个变体用 4 个更宽的 PE。这正是「端口位宽 × PE 数量」两种扩展方向互换的实例，第六单元详讲。
</details>

## 5. 综合实践

本讲的综合实践是规格中的核心任务：**绘制一张「三类微基准 × 扫描参数 × 定义位置」总表**，把前两节的知识固化成一张可查阅的速查表。

**任务描述**：

1. 重读根 `README.md` 第 30-58 行与 FPGA 2021 论文引用（第 5-6 行；论文可按题名检索）。
2. 对三类微benchmark（片外带宽、流带宽、片外延迟），列出它们各自扫描的全部参数。
3. 对每个参数，标注它在仓库中的定义位置（文件 + 行号），参考下面的模板：

| 微基准 | 扫描参数 | 示例取值 | 定义位置 |
| --- | --- | --- | --- |
| 片外带宽 | 端口位宽 | 512bit | `read/DDR/2ports_512bit/src/krnl_config.h:4`（`DWIDTH`） |
| 片外带宽 | 并发端口数 | 2 | `read/DDR/2ports_512bit/src/host.cpp:16`（`NUM_PORT`） |
| 片外带宽 | 最大突发长度 | 16 | `read/DDR/2ports_512bit/src/krnl_ubench.cpp:5-6`（pragma） |
| 片外带宽 | 连续访问数据量 | 1KB~1MB | `read/DDR/2ports_512bit/src/host.cpp:100`（payload 循环） |
| 片外带宽 | 内核频率 | 300MHz | `datacenter/README.md:78`（`--kernel_frequency`）或 `auto_collect/config.py:7` |
| 流带宽 | 流端口数 / 位宽 / 数据量 | …… | 由你补全（提示：`streaming/.../src/` 与其 `auto_collect/config.py`） |
| 片外延迟 | 每次访问位宽 / 数组大小 | 32bit / 64B~1MB | 由你补全（提示：`latency/.../src/krnl_config.h:4` 与 `host.cpp:99`） |
| （进阶）案例研究 | 端口位宽、PE 数 | 见论文表 2/表 4 | `case_study/*/<变体>/src/krnl_config.h` |

4. **进阶**：从 `case_study/KNN` 与 `case_study/SpMV` 任选一个变体，打开其 `src/krnl_config.h`，用 4.2.2 的公式 \( BW_{peak} = f \times N_{port} \times W \) 估算其理论聚合带宽上限（频率可从该设计目录的 Makefile 或论文表格中查得，查不到就标注「待本地验证」）。

**验收标准**：表中每个单元格都能被一条 `grep -n` 或一次文件打开验证；完成后你会得到一张贯穿全仓库的参数地图——后面每一讲讲参数修改时都会回到这张表。

## 6. 本讲小结

- uBench 是 SFU HiAccel 实验室的 FPGA 2021 论文配套开源套件，用 HLS 微基准定量测量 Alveo U200（DDR4）、U280（HBM2）、ZCU104 的**内存系统**性能。
- 影响内存带宽的五个因素：**频率、并发端口数、端口位宽、最大突发长度、连续访问数据量**；前三个决定理论峰值 \( BW_{peak} = f \times N_{port} \times W \)，后两个决定实际能达到的百分比。
- 五因素在示例工程中各有明确落点：`krnl_config.h` 的 `DWIDTH`/`NUM_ITERATIONS`、`host.cpp` 的 `NUM_PORT` 宏与 payload 循环、`krnl_ubench.cpp` 的 `max_read_burst_length` pragma、Makefile 的 `--kernel_frequency`。
- 仓库分三类微基准：**片外带宽**（五因素全扫）、**流带宽**（无突发，多流数据量维度）、**片外延迟**（随机访问，位宽 + 数组大小），每类都有 datacenter/embedded 之分，目录名即参数组合。
- 修改参数有两条路：按各 README 六节指南**手动**改示例工程，或用 `auto_collect/config.py` 声明参数空间后**自动**生成整批工程。
- `case_study/` 下的 KNN（Rodinia）与 SpMV（MachSuite）各含 baseline/suboptimal/optimal/aggressive 四个设计变体，用于验证微基准洞察如何转化为加速器设计决策。

## 7. 下一步学习建议

下一讲（`u1-l2-repo-structure-tour.md`）将带你逐层走一遍仓库目录树，弄清每个微 benchmark 目录里 Makefile、`utils.mk`、`ubench.ini`、`src/` 四类文件的分工。在继续之前，建议你：

1. 自己把 4.3.4 的目录普查命令再跑一遍，试着不看讲义说出每个目录名代表的参数组合。
2. 打开 `ubench/offchip_bandwidth/datacenter/README.md` 通读六节手动调参指南，对「改一个参数要动哪些文件」建立初步印象。
3. 有条件的话检索并浏览 FPGA 2021 论文 "Demystifying the Memory System of Modern Datacenter FPGAs for Software Programmers through Microbenchmarking"，把论文中的图表与仓库目录对应起来（无法获取论文不影响后续学习）。

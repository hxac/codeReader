# u2-l3 测量方法学：payload 扫描、计时与带宽公式

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 payload 倍增循环（256 → 262144 个 32bit int，即 1KB → 1MB）如何逐档扫描「连续访问数据量」这一带宽因素。
2. 逐项推导带宽公式 `bw = payload * 4 * 0.000010000 / kernel_time_in_sec * NUM_KERNEL * NUM_PORT` 中每一个数字与变量的单位来源，特别是魔数 `0.000010000` 里隐藏的两个假设。
3. 画出主机端计时的精确起止边界，指出它包含内核启动开销这一系统性偏差，并定量估计该偏差对大、小 payload 的不同影响。
4. 动手把计时方式改写为基于 `cl_event` 的内核级计时（利用队列上已经打开的 `CL_QUEUE_PROFILING_ENABLE`），从而把「启动开销」从测量结果中剥离。

本讲是 u2-l1（内核侧如何产生流量）、u2-l2（主机侧如何启动内核）之后的收口一讲：前两讲回答「流量从哪来、怎么发」，本讲回答「测得准不准、算得对不对」。

## 2. 前置知识

阅读本讲前，请确认理解以下概念（均在前两讲建立）：

- **微基准的测量对象是内存系统，不是计算**。内核循环体只做 `temp = in0[j]` 这样的空读，读带宽本身就是要测的「计算结果」。
- **NUM_ITERATIONS（内核外层循环次数，本工程为 10000）的作用是放大执行时间**：单次读 1KB 只要十几微秒，主机时钟根本测不准；重复一万次后时间被放大到毫秒~秒量级。详见 [krnl_config.h:7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L7)。
- **理论峰值带宽 = 频率 × 端口数 × 位宽 ÷ 8**。本工程（300MHz、2 端口、512bit）为 38.4 GB/s，这是任何测量结果都不应超过的天花板。
- **DATAFLOW 让两个端口的读循环并发执行**（见 [krnl_ubench.cpp:17-31](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L17-L31)），因此「总时间」是并发的墙钟时间，而「总字节数」要把两个端口的字节数相加——这是带宽公式里乘 `NUM_PORT` 的物理依据。
- **`WIDTH_FACTOR = DWIDTH/32 = 16`**：主机侧的 `dataSize` 以 int 计，内核侧的 `size` 参数以 512bit 宽字计，二者相差 16 倍（见 [krnl_config.h:4-6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L4-L6)）。
- 一个提醒（承接 u1-l3 的结论）：sw_emu 仿真的带宽数值没有物理意义，本讲的公式分析会给出其中一个具体原因。

补充两个本讲新用到的术语：

- **计时窗口（timing window）**：程序里 `start = now()` 与 `end = now()` 两条语句之间真正被计入耗时的那段操作。哪些操作落在窗口内、哪些落在窗口外，直接决定你测的是什么。
- **内核启动开销（launch overhead）**：从主机调用 `enqueueTask` 到内核真正在 FPGA 上开始取第一条数据之间，XRT 驱动、命令队列、CU（计算单元）配置等一系列软件环节的耗时，典型量级为几百微秒。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [src/host.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp) | payload 倍增循环、计时起止边界、带宽公式与打印 |
| [src/krnl_config.h](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h) | `DWIDTH`/`WIDTH_FACTOR`/`NUM_ITERATIONS` 三个常量的定义与被消费的位置 |
| [src/krnl_ubench.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp) | 内核侧如何用 `size`（宽字个数）与 `NUM_ITERATIONS` 产生实际读流量 |
| [common/includes/xcl2/xcl2.hpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.hpp) | `OCL_CHECK` 宏与 C++ binding 的加载（实践环节会用到它的错误检查风格） |

三个工程文件的角色关系回顾（u1-l4 已建立）：`krnl_config.h` 是内核与主机的共享契约；主机负责扫描参数、搬数据、计时、算带宽；内核只负责按 `size` 反复读。

## 4. 核心概念与源码讲解

### 4.1 payload 扫描：从 256 个 int 到 1MB 连续访问

#### 4.1.1 概念说明

「payload」指每端口每次内核迭代所读取的数据量，单位是 **int 的个数**（源码中是循环变量 `payload` 本身，不是字节）。它对应五大带宽因素中的最后一项——**连续访问数据量**：

- payload 很小时（1KB），AXI 突发的地址建立开销、流水线爬坡（ramp-up）在总时间里占比高，实际带宽远低于峰值；
- payload 足够大时（1MB），这些一次性开销被摊薄，带宽逐渐逼近 `频率 × 端口数 × 位宽` 的理论上限。

所以微基准不能只测一个点，而要**倍增扫描**一整条曲线，观察带宽随连续数据量的爬升过程——这正是实验报告中那条经典曲线的来源。

#### 4.1.2 核心流程

主机端对每个 payload 档位执行一轮完整流程：

```text
for payload in {256, 512, 1024, ..., 262144}        # int 个数，倍增 11 档
    dataSize = payload（仿真模式下强制 256）
    生成主机随机数据 → 为 NUM_KERNEL*NUM_PORT 个端口各建一个缓冲
    enqueueMigrateMemObjects 把数据搬到设备 + finish      # ← 计时窗口之外
    启动计时（chrono）
    把 dataSize 除以 WIDTH_FACTOR 得到宽字个数，setArg
    enqueueTask 启动所有内核实例 + finish               # ← 计时窗口之内
    停止计时
    用 payload（注意：不是 dataSize）计算并打印带宽
```

payload 档位与实际字节数的换算：每档字节数 = payload × 4B（`int` 为 4 字节）。

| 第几档 | payload（int 个数） | 字节数 | 宽字个数（÷16，内核 `size`） |
| --- | --- | --- | --- |
| 1 | 256 | 1KB | 16 |
| 2 | 512 | 2KB | 32 |
| 3 | 1024 | 4KB | 64 |
| ... | ... | ... | ... |
| 10 | 131072 | 512KB | 8192 |
| 11 | 262144 | **1MB** | 16384 |

即 \(2^8\) 到 \(2^{18}\) 共 \(18-8+1 = 11\) 档，字节数从 \(256 \times 4 = 1024\text{B}\) 到 \(262144 \times 4 = 1\text{MB}\)。

#### 4.1.3 源码精读

**payload 倍增循环与仿真短路**，见 [host.cpp:99-104](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L99-L104)：

```cpp
int dataSize(0);
for (int payload(256); payload <= 262144; payload*=2){
    dataSize = payload;
    if (xcl::is_emulation()) {
        dataSize = 256; //1KB
    }
```

- `payload*=2` 且 `<= 262144`：从 256（2⁸）倍增到 262144（2¹⁸），共 11 档。
- 仿真分支把 `dataSize` 钉死在 256：仿真器要逐拍解释执行，若真跑 10000 次 × 16384 宽字的循环会慢到无法忍受，所以只测 1KB。**但注意**：后面带宽公式用的是 `payload` 而不是 `dataSize`，于是在 sw_emu 下分子（假想的数据量）照样翻倍、分母（真实只读了 1KB 的时间）不变——这从公式层面解释了 u1-l3 的结论「仿真带宽数值无物理意义」。

**数据搬运在计时窗口之外**，见 [host.cpp:142-147](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L142-L147)：

```cpp
err = q.enqueueMigrateMemObjects({source_in_buffer[i]}, 0 /* 0 means from host*/);
...
q.finish();
```

`enqueueMigrateMemObjects` 把主机数据经 PCIe 搬到 DDR bank，随后的 `q.finish()` 确保搬运完成后才开始计时。这是方法学上正确的一步：**我们要测内核读带宽，不能把 H2D 传输时间混进来**。

**单位换算发生在启动内核之前**，见 [host.cpp:154-161](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L154-L161)：

```cpp
//Setting the compute kernel arguments
dataSize = dataSize / WIDTH_FACTOR;
...
OCL_CHECK(err, err = cmpt_krnl[i].setArg(j, dataSize));
```

缓冲区创建时用的 `dataSize` 是 int 个数（见 [host.cpp:138](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L138) 的 `sizeof(int) * dataSize`），而传给内核的 `size` 被除以 `WIDTH_FACTOR=16`，变成 512bit 宽字个数——内核循环 `in0[j]` 的步长是一个宽字 64 字节，见 [krnl_ubench.cpp:20-23](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L20-L23)。

由此得到一条贯穿内核与主机的单位链：

\[ \text{每端口每轮外层循环的字节数} = \text{payload} \times 4\text{B} = \text{size} \times \text{WIDTH\_FACTOR} \times 4\text{B} = \text{size} \times 64\text{B} \]

#### 4.1.4 代码实践

**实践目标**：不动源码，只通过「读数 + 推算」验证你对 payload 档位与单位换算的掌握。

1. 打开 [host.cpp:100](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L100)，手写 payload 的全部 11 个取值。
2. 对每一档计算三个量：字节数（payload×4）、内核宽字个数（payload÷16）、单端口单轮外层循环的 AXI beat 数（II=1 时每拍一个宽字，故等于宽字个数）。
3. 用 `grep -n "NUM_ITERATIONS\|WIDTH_FACTOR\|DWIDTH" krnl_config.h krnl_ubench.cpp host.cpp` 查看三个常量分别在哪些文件被消费，画出「定义点 → 消费点」的对照表。

**需要观察的现象 / 预期结果**：你会得到一张 11 行的表格，第一行 256 / 1KB / 16 / 16，最后一行 262144 / 1MB / 16384 / 16384；对照表会显示 `NUM_ITERATIONS` 只在内核侧出现（外层循环），`WIDTH_FACTOR` 只在主机侧出现（单位换算），`DWIDTH` 通过 `INTERFACE_WIDTH` 决定内核端口类型——三者各管一段，这正是 u1-l4「契约头」的具体体现。

#### 4.1.5 小练习与答案

**练习 1**：如果把循环条件改成 `payload <= 1048576`，扫描范围变成多大？多跑几档的代价是什么？

**答案****：1048576 = 2²⁰，即从 2⁸ 到 2²⁰ 共 13 档，最大档从 1MB 变为 4MB。代价是最大档的内核执行时间大约变为原来的 4 倍（流量与 payload 成正比），整个扫描的运行时间明显变长；收益是能观察更大连续访问下带宽是否继续爬升。

**练习 2**：为什么把 `enqueueMigrateMemObjects` 和它后面的 `q.finish()` 放在计时起点之前是正确的？

**答案**：本微基准测量的是**内核从 DDR 读数据的带宽**。H2D 搬运走的是 PCIe 通道，其带宽（约十几 GB/s）与被测的 DDR 读带宽是两个独立的量；若计入窗口，测得的是「PCIe 传输 + 内核读」的混合值，且小 payload 时会被传输时间淹没。先 `finish()` 再起表，保证窗口内只有内核相关的操作。

**练习 3**：仿真分支强制 `dataSize = 256`，为什么带宽输出仍然随 payload 翻倍而变化？

**答案**：因为带宽公式（下文 4.2.3）的分子用的是循环变量 `payload`，它与实际分配/读取的 `dataSize` 在仿真模式下脱钩了——分子按假想的大数据量计算，分母却只对应 1KB 的真实读取时间，所以仿真输出的「带宽」随 payload 虚假增长，不可信。

### 4.2 带宽换算公式：逐项单位推导

#### 4.2.1 概念说明

带宽的定义是 \(\text{bw} = \dfrac{\text{传输的总字节数}}{\text{总时间}}\)。这个定义简单，难的是把「总字节数」数对——必须把**所有并发端口、所有内核实例、所有重复迭代**的字节都算进去，而时间用**并发执行的墙钟时间**。数错任何一项，结果就系统性偏差若干倍。

#### 4.2.2 核心流程

先从第一性原理推导，再与源码对照。设 `t` 为测得的秒数：

```text
单端口每轮外层循环读的字节 = payload × 4
单端口全程读的字节        = payload × 4 × NUM_ITERATIONS        (= payload×4×10000)
全部端口全程读的字节      = payload × 4 × NUM_ITERATIONS × NUM_KERNEL × NUM_PORT

带宽 (GB/s, 十进制) = 全部字节数 ÷ t ÷ 1e9
                  = payload × 4 × (NUM_ITERATIONS/1e9) ÷ t × NUM_KERNEL × NUM_PORT
                  = payload × 4 × 0.000010000 ÷ t × NUM_KERNEL × NUM_PORT      ← 与源码完全一致
```

关键一步是把魔数还原：

\[ 0.000010000 = 10^{-5} = \frac{\text{NUM\_ITERATIONS}(=10000)}{10^{9}} \]

即魔数里**同时硬编码了两个事实**：内核外层循环重复 10000 次，以及采用十进制 GB（\(10^9\) 字节）作单位。

#### 4.2.3 源码精读

**公式本体**，见 [host.cpp:172](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L172)：

```cpp
double bw_result = payload * 4 * 0.000010000 / kernel_time_in_sec * NUM_KERNEL * NUM_PORT;
```

逐项标注单位：

| 公式片段 | 含义 | 单位 |
| --- | --- | --- |
| `payload` | 每端口缓冲的 int 个数（原始循环变量） | 个 |
| `* 4` | 每个 int 4 字节 | 字节 |
| `* 0.000010000` | × 10000 次迭代 ÷ 1e9 换算成 GB | GB/端口 |
| `/ kernel_time_in_sec` | 除以墙钟时间 | GB/s/端口 |
| `* NUM_KERNEL * NUM_PORT` | 聚合所有内核实例的所有端口（本工程 1×2） | GB/s |

其中 `NUM_KERNEL`、`NUM_PORT` 定义在 [host.cpp:15-16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L15-L16)（本工程均为常量 1 和 2）；`NUM_ITERATIONS` 定义在 [krnl_config.h:7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L7)。

**为什么乘 NUM_PORT 是合法的**：两个读循环在 `#pragma HLS DATAFLOW` 之下并发执行（[krnl_ubench.cpp:17-31](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L17-L31)），墙钟时间 `t` 约等于**单个**端口跑完 10000 轮的时间（两端口对称、同时开始同时结束），而字节数是两端口之和，所以聚合带宽 = 2× 单端口带宽。若两个循环是串行的（去掉 DATAFLOW），时间会翻倍，这个公式就会把带宽高估一倍——**公式正确性依赖于内核的并发结构**。

**隐藏的三个坑**：

1. **`NUM_ITERATIONS` 被硬编码进魔数**。若把 [krnl_config.h:7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L7) 改成 50000，内核耗时翻倍而公式分子仍按 10000 次计——输出带宽会**无声地**变成真实值的一半。正确写法应是 `payload * 4 * NUM_ITERATIONS / 1e9 / kernel_time_in_sec * ...`，直接引用契约头里的常量。
2. **打印 payload 的语句是坏的**，见 [host.cpp:173](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L173)：
   ```cpp
   std::cout << "Payload Size: " << i*4/(1024.0*1024.0) << "MB - Bandwidth = " << bw_result << "GB/s"<< std::endl;
   ```
   双重循环结束后 `i == NUM_KERNEL == 1`，所以每档打印的都是 `1*4/1048576 ≈ 0.0000038 MB`，与 payload 无关。应为 `payload * 4 / (1024.0*1024.0)`。另外此处用 1024 进制 MB、公式用 10⁹ 进制 GB，单位口径也不统一——读结果时务必留意。
3. **仿真模式下分子分母脱钩**（4.1.3 已析）：分子用 `payload`、实际数据量被钉在 1KB，sw_emu 输出不可比。

#### 4.2.4 代码实践

**实践目标**：亲手复原魔数，验证公式自洽。

1. 在纸上按 4.2.2 的推导，把 `0.000010000` 拆成 `NUM_ITERATIONS / 1e9` 两个因子。
2. 代入理想数值做 sanity check：假设测得 1MB 档（payload=262144）`t = 0.55s`，代入公式算 `bw_result`；再与理论峰值 38.4 GB/s 比较。
3. （可选）把 [host.cpp:172](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L172) 中的魔数改写为 `NUM_ITERATIONS / 1e9`（`krnl_config.h` 已被 host 包含，直接可用），并顺手修复 L173 的打印 bug——这是后续综合实践的一部分。

**预期结果**：第 2 步中 `bw = 262144 × 4 × 1e-5 / 0.55 × 1 × 2 ≈ 38.1 GB/s`，略低于 38.4 GB/s 的理论峰值——量级与方向都合理（真实硬件还会更低）。若你算出几百 GB/s 或零点几 GB/s，说明单位链某处数错了。真实硬件上的实测值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把 `NUM_ITERATIONS` 从 10000 改为 50000（只改 krnl_config.h，不改公式），对 1MB 档的输出带宽有什么影响？真实带宽呢？

**答案**：内核执行时间约翻倍，公式分子不变，输出带宽**减半**；而真实带宽（硬件实际达到的吞吐）不变——读同样多的数据花两倍时间，吞吐率是一样的。这说明公式与 `NUM_ITERATIONS` 必须同步，魔数写法是隐患。

**练习 2**：为什么公式分子乘 `NUM_PORT` 而不是除？什么情况下这个乘法是错的？

**答案**：分子是**所有端口传输的字节总和**，端口越多字节越多，所以相乘。前提是这些端口**并发**工作（DATAFLOW + 独立 bundle + 各自绑 bank），墙钟时间不随端口数增加。若端口实际串行工作（例如共享一个 bundle 导致互斥，或去掉 DATAFLOW），时间同样随端口数增加，此时分子乘 `NUM_PORT` 会高估带宽。

**练习 3**：公式算出的单位是 GiB/s 还是 GB/s？依据是什么？

**答案**：是十进制 GB/s（\(10^9\) 字节/秒），依据是魔数 `0.000010000 = 10000/10^9`，其中 \(10^9\) 正是 GB 的换算因子。对照 [host.cpp:173](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L173) 打印 payload 用的是 \(1024^2\) 进制，两处口径不一致，横向对比数据时要统一。

### 4.3 计时误差来源：主机端计时窗口的系统性偏差

#### 4.3.1 概念说明

主机用 `std::chrono::high_resolution_clock` 在**内核启动之前、结束之后**各取一个时间点。问题在于，「启动之前」取得太早了——窗口里混进了若干既不是数据搬运也不是内核执行的主机/驱动开销。对带宽这类「字节÷时间」的指标，分母被撑大多少，结果就被低估多少。

同时也要看到硬币的另一面：时钟本身的精度（Linux 上 chrono 纳秒级粒度）相对毫秒级的被测时间完全够用，**真正的误差源不是时钟，而是窗口边界画错了位置**。此外每个 payload 只测一次、没有重复取样取中位数，抖动无从消除。

#### 4.3.2 核心流程

计时窗口的精确边界：

```text
（窗口外）生成随机数据 → 建缓冲 → MigrateMemObjects(H2D) → finish
┌──────────────────────── 计时窗口开始 ────────────────────────┐
│  ① setArg × (NUM_PORT+1) 次      —— 主机写内核参数，微秒级     │
│  ② enqueueTask                    —— 命令提交 + XRT 调度 +    │
│                                      CU 启动，约 0.1~1ms 量级  │
│  ③ 内核真正执行（想测的部分）                                    │
│  ④ q.finish() 返回                —— 中断/轮询返回延迟         │
└──────────────────────── 计时窗口结束 ────────────────────────┘
kernel_time_in_sec = ①+②+③+④
```

**误差的量级估算**（按 300MHz、II=1、两端口并发的理想假设，纯推演，实测**待本地验证**）：

内核执行时间近似为 \(\;t_{kernel} \approx \dfrac{\text{NUM\_ITERATIONS} \times (\text{payload}/16)}{300\text{MHz}} \)

| payload | 内核理想时间 | 启动开销占比（按 0.3ms 估） |
| --- | --- | --- |
| 256（1KB） | ≈ 0.53 ms | **≈ 35%+**（与开销同量级，甚至反超） |
| 4096（16KB） | ≈ 8.5 ms | ≈ 3.5% |
| 262144（1MB） | ≈ 0.55 s | ≈ 0.05%（可忽略） |

结论清晰可见：**同一份代码、同一个误差源，对小 payload 档位的影响比对大 payload 档位严重两三个数量级**。更微妙的是，小 payload 档位本来就有「突发摊薄不足、流水爬坡占比高」的物理性带宽下降，测量偏差叠加在物理效应之上，让曲线的低端**额外**偏低——单看曲线你无法区分这两类原因，这正是需要内核级计时的理由。

还有一个隐蔽的一次性开销：**第一档 payload 恰好是内核加载 xclbin 后的首次启动**，驱动/CU 的初次配置往往比后续启动更慢，而这笔账正好记在最经不起误差的最小档位头上。

#### 4.3.3 源码精读

**计时起点**——在 setArg 之前就按下了秒表，见 [host.cpp:149-164](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L149-L164)：

```cpp
// Start timer
double kernel_time_in_sec = 0;
std::chrono::duration<double> kernel_time(0);
auto kernel_start = std::chrono::high_resolution_clock::now();

//Setting the compute kernel arguments
dataSize = dataSize / WIDTH_FACTOR;
...
OCL_CHECK(err, err = cmpt_krnl[i].setArg(j, dataSize));
//Invoking the compute kernels
OCL_CHECK(err, err = q.enqueueTask(cmpt_krnl[i]));
```

注意起表（L152）与 `enqueueTask`（L163）之间还夹着 `setArg` 调用——虽然 `setArg` 本身只有微秒级，但它说明作者划窗口时的颗粒度是「整个启动阶段」，而非「内核执行」。

**计时终点与换算**，见 [host.cpp:165-171](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L165-L171)：

```cpp
q.finish();
// Stop timer
auto kernel_end = std::chrono::high_resolution_clock::now();
kernel_time = std::chrono::duration<double>(kernel_end - kernel_start);
kernel_time_in_sec = kernel_time.count();
std::cout << "Execution time = " << kernel_time_in_sec << std::endl;
```

`q.finish()` 阻塞到内核完成，终点包含了命令完成通知从设备回到主机的延迟。

**一个被浪费的好配置**：队列创建时其实已经打开了 profiling，见 [host.cpp:56-61](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L56-L61)：

```cpp
q = cl::CommandQueue(context,
                     {device},
                     CL_QUEUE_OUT_OF_ORDER_EXEC_MODE_ENABLE |
                         CL_QUEUE_PROFILING_ENABLE,
                     &err));
```

`CL_QUEUE_PROFILING_ENABLE` 会让运行时为每条命令记录设备侧时间戳（cl_event 的 profiling 信息），但这份 host.cpp 从未创建过 `cl::Event`、也从未读取过时间戳——开关打开了，数据却被丢弃。这正是下一节实践要补上的东西。

#### 4.3.4 代码实践：改写为基于 cl_event 的内核级计时

**实践目标**：把计时窗口从「主机启动阶段 + 内核执行」缩小到「设备侧纯内核执行」，并量化两种口径的差值（即启动开销）。

**操作步骤**（示例代码，基于本工程 host.cpp 修改；队列已带 `CL_QUEUE_PROFILING_ENABLE`，无需改 [host.cpp:56-61](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L56-L61)）：

1. 在计时区附近声明事件对象：

   ```cpp
   // 示例代码：事件收集内核级时间戳
   std::vector<cl::Event> krnl_events(NUM_KERNEL);
   ```

2. 把 `enqueueTask` 改为带事件的版本（替换 [host.cpp:163](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L163)）：

   ```cpp
   // 示例代码
   OCL_CHECK(err, err = q.enqueueTask(cmpt_krnl[i], NULL, &krnl_events[i]));
   ```

3. `q.finish()` 之后读取设备侧时间戳（单位为**纳秒**）：

   ```cpp
   // 示例代码
   cl_ulong t_submit = 0, t_dev_start = 0, t_dev_end = 0;
   krnl_events[0].getProfilingInfo(CL_PROFILING_COMMAND_SUBMIT, &t_submit);
   krnl_events[0].getProfilingInfo(CL_PROFILING_COMMAND_START, &t_dev_start);
   krnl_events[0].getProfilingInfo(CL_PROFILING_COMMAND_END,   &t_dev_end);
   double launch_overhead_sec = (t_dev_start - t_submit) * 1e-9; // 提交→开跑
   double kernel_only_sec     = (t_dev_end   - t_dev_start) * 1e-9; // 纯内核执行
   ```

4. 用 `kernel_only_sec` 替换 [host.cpp:172](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L172) 公式里的 `kernel_time_in_sec`，同时保留 chrono 口径的输出，逐档打印两者及差值。

**需要观察的现象**：
- 每一档都打印 `chrono 时间`、`event 纯内核时间`、`差值（≈启动开销）`；
- 差值在不同 payload 档位间应大致恒定（它是与数据量无关的固定开销）；
- 小 payload 档位差值占比高、大 payload 档位差值占比低，与 4.3.2 的估算表吻合。

**预期结果**：用 event 口径算出的带宽在**小 payload 档位显著高于** chrono 口径（分母去掉了固定开销），大 payload 档位两者趋于重合。若在 sw_emu 下验证功能，时间戳机制可用但数值仍无物理意义；真机上的具体差值**待本地验证**。无硬件时，请先完成 4.3.2 的推演表并写出上述伪代码，作为「纸面实践」。

#### 4.3.5 小练习与答案

**练习 1**：列出落在计时窗口内、但不属于内核执行的至少三件事。

**答案**：① 多次 `setArg` 的主机侧参数写入；② `enqueueTask` 的命令提交与 XRT/CU 调度（启动开销主体）；③ `q.finish()` 等待内核完成通知返回主机的延迟。此外首档还叠加首次启动的一次性驱动初始化。

**练习 2**：为什么说「换更高精度的时钟」解决不了这个问题？

**答案**：误差不是时钟分辨率造成的——chrono 在 Linux 上可达纳秒粒度，而被测时间是毫秒到秒级。误差来自**窗口边界**：窗口内混入了与数据量无关的固定开销，把分母撑大。正确做法是换测量点（event 时间戳）或换窗口（只测内核），而不是提高时钟精度。

**练习 3**：`CL_PROFILING_COMMAND_SUBMIT` 到 `CL_PROFILING_COMMAND_START` 的间隔、`START` 到 `END` 的间隔，分别测量的是什么？

**答案**：SUBMIT→START 是命令从提交到内核真正在设备上开跑的间隔，即**启动/调度开销**的设备侧度量；START→END 是内核在设备上的**纯执行时间**。chrono 口径减去 START→END，应当约等于启动开销加上主机侧往返延迟——这正是实践任务要验证的关系。

## 5. 综合实践

**任务：给 read/DDR 微基准产出一份数据可信度更高的测量报告。** 把本讲三个模块串起来，对 `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit` 做如下改造（均在 host.cpp 内，或复制的副本目录内进行，不要改动内核与仓库原文件）：

1. **修公式**：把 [host.cpp:172](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L172) 的魔数 `0.000010000` 替换为 `NUM_ITERATIONS / 1e9`（krnl_config.h 已被包含），消除与契约头的隐性耦合。
2. **修打印**：把 [host.cpp:173](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L173) 的 `i*4/(1024.0*1024.0)` 修正为 `payload*4/(1024.0*1024.0)`。
3. **双口径计时**：按 4.3.4 加入 cl_event 计时，每档输出一行表格：`payload(int) | 字节 | chrono 时间 | event 内核时间 | 启动开销 | chrono 带宽 | event 带宽`。
4. **重复取样**：把每档改为重复 5 次取中位数（注意每档先跑 1 次预热再计时，避免首启开销污染）。
5. **分析**：对比两种口径的带宽曲线——哪几档差异最大？event 口径下小 payload 带宽是否明显上抬？剩余的爬升还剩多少可归因于突发摊薄等物理因素？

无硬件时的交付物：改动后的 host.cpp、完整的推演表格、以及预测「两种口径差异随 payload 变化」的曲线示意图与理由。有 Vitis 环境时可先用 `make check TARGET=sw_emu` 验证编译与功能链路（数值仍不具物理意义），真机数据**待本地验证**。

## 6. 本讲小结

- payload 倍增循环从 256 个 int（1KB）扫到 262144 个 int（1MB）共 11 档，对应「连续访问数据量」因素；内核侧的 `size` 是除以 `WIDTH_FACTOR=16` 后的宽字个数。
- 带宽公式逐项拆解后：`payload×4` 是每端口每轮字节数，魔数 `0.000010000 = NUM_ITERATIONS/1e9` 隐藏了「重复 10000 次」与「十进制 GB」两个硬编码假设——改 `NUM_ITERATIONS` 不改公式会无声地出错。
- 乘 `NUM_PORT`（与 `NUM_KERNEL`）合法的前提是 DATAFLOW 下各端口并发执行、墙钟时间不随端口数增长。
- 主机 chrono 计时窗口包含 setArg、enqueueTask 启动开销与 finish 返回延迟；该固定开销对小 payload（内核时间仅亚毫秒级）造成百分之几十量级的低估，对 1MB 档（约半秒）可忽略。
- 队列已打开 `CL_QUEUE_PROFILING_ENABLE` 却从未使用；用 cl_event 的 SUBMIT/START/END 时间戳可把启动开销与纯内核时间分离。
- 顺带确认的三个仓库瑕疵：仿真模式分子分母脱钩、payload 打印用了循环残留变量 `i`、MB（1024 进制）与 GB（10⁹ 进制）口径混用。

## 7. 下一步学习建议

本讲补全了「公共编程模型」单元（u2）的最后一块拼图。接下来：

- **进入 u3-l1（读带宽内核逐行精读）**：本讲把内核当作流量发生器黑盒，u3-l1 会逐行拆开它——volatile 防优化、双端口循环结构，以及 DDR 版与 HBM 版内核为什么完全相同。
- **进入 u3-l2 / u3-l3**：本讲的 payload 维度只是五因素之一，后续两讲分别讲端口数/位宽/突发长度的手动调参，以及 ubench.ini 的 bank 连接配置。
- **提前思考 u7-l3（测量方法学批判）**：本讲的 event 计时实践是它的前置练习；那会进一步讨论 xrt.ini 的 profile/timeline_trace 与 Makefile 的 perf_analyze 流程，作为比手工 event 更系统的剖析手段。
- 建议阅读顺序上，若你想立刻动手，可先完成本讲第 5 节综合实践，再带着「双口径数据」进入 u3 的参数扫描实验。

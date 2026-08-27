# HLS 内核编程基础：INTERFACE pragma、ap_uint 与流水线

## 1. 本讲目标

本讲以 uBench 中最简单的读带宽内核 `krnl_ubench`（read/DDR/2ports_512bit 工程）为样板，讲清 Vitis HLS 内核的三块基石。学完后你应该能够：

1. 解释 `#pragma HLS INTERFACE m_axi` 的三个关键参数——`bundle`、`offset`、`max_read_burst_length`——分别决定了什么硬件行为，以及 `s_axilite` 控制接口的作用。
2. 说明 `krnl_config.h` 中 `DWIDTH → INTERFACE_WIDTH（ap_uint<512>）→ WIDTH_FACTOR` 这条派生链如何让「一次访问搬多少数据」只由一个常量控制。
3. 理解 `#pragma HLS DATAFLOW` 与 `#pragma HLS PIPELINE II=1` 在带宽测试内核中的确切含义：前者让两个读端口并发工作，后者让每个端口每个时钟周期都发起一次访存。
4. 独立完成一次参数修改实验：把 `max_read_burst_length` 从 16 改为 256、`DWIDTH` 从 512 改为 256，并列出所有需要联动检查的位置。

本讲是整个手册的「公共地基」之一：后面所有微基准和 KNN/SpMV 案例的内核，用的都是这三块积木。

## 2. 前置知识

在读源码之前，先用通俗语言建立四个直觉。

### 2.1 HLS（高层次综合）是什么

Vitis HLS 让你用 C++ 写内核，工具把 C++ 综合成 FPGA 上的硬件电路（RTL）。但 C++ 是「顺序执行」的语言，硬件是「空间上铺开、周期上并行」的电路——两者之间有一道语义鸿沟。HLS 提供两类手段来填这道沟：

- **任意精度类型**（如 `ap_uint<512>`）：告诉工具「这个变量是一根 512 位宽的线」，而普通 `int` 只有 32 位。
- **编译指示（pragma）**：写在代码里的「给综合工具的指令」，不影响 C++ 语义（用 g++ 编译时它们只是注释），但会改变生成的电路。本讲的主角 `INTERFACE`、`DATAFLOW`、`PIPELINE` 都是 pragma。

### 2.2 AXI 协议三分钟速成

内核与片外内存（DDR/HBM）之间走的是 AXI 总线协议。只需要知道三件事：

- **beat（拍）**：一次数据传输单位，宽度等于端口位宽。512 bit 端口的一个 beat 就是 64 字节。
- **burst（突发）**：地址通道上发一次地址，数据通道上连续传多个 beat。`max_read_burst_length=16` 表示一次突发最多 16 个 beat。突发越长，发地址、握手等一次性开销被摊得越薄——这正是 uBench 五因素中的「最大突发长度」。
- **master / slave**：发起读写的一方叫 master。内核的内存端口是 AXI master（内核主动去读内存）；主机 CPU 通过 AXI Lite（`s_axilite`）以 master 身份配置内核，此时内核是 slave。

### 2.3 两类接口的分工

一个 HLS 内核函数的每个指针参数、标量参数，最终都要落到某个硬件接口上：

| 接口类型 | 方向 | 作用 | 本讲例子 |
|---|---|---|---|
| `m_axi` | 内核 → 片外内存 | 真正搬数据的通道，决定带宽 | `in0`、`in1` |
| `s_axilite` | 主机 CPU → 内核 | 写参数（指针基地址、size）、启动/等待内核 | `size`、`return` |
| `axis`（流） | 内核 → 内核 | 片上流数据，本讲不涉及，见 u4-l1 | — |

### 2.4 并发的两个层次

- **循环流水线（PIPELINE）**：让一次循环的多次迭代像工厂流水线一样重叠。`II=1`（Initiation Interval，启动间隔 = 1）表示每个时钟周期就能开始一次新的迭代，即每拍发起一次访存。
- **任务级数据流（DATAFLOW）**：让两个相互独立的循环/函数**同时**运行，各自占一块硬件。读带宽内核正是靠它让两个读端口并发工作。

## 3. 本讲源码地图

本讲涉及的关键文件（均属 `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/` 工程）：

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [src/krnl_config.h](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h) | 内核与主机共用的参数头文件（全文仅 7 行） | `DWIDTH`、`INTERFACE_WIDTH`、`WIDTH_FACTOR`、`NUM_ITERATIONS` 四个常量的派生关系 |
| [src/krnl_ubench.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp) | 被测 HLS 内核（全文仅 37 行） | INTERFACE pragma、volatile、DATAFLOW、PIPELINE |
| [src/host.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp) | OpenCL 主机程序 | 只看三处联动点：`NUM_PORT` 宏、`WIDTH_FACTOR` 换算、带宽公式（完整讲解在 u2-l2/u2-l3） |

回顾 u1-l4 建立的认知：这个工程是「五件套」骨架，其中 `krnl_config.h` 被内核和主机**同时** include，是两侧保持参数一致的桥梁——本讲会反复看到这一点。

## 4. 核心概念与源码讲解

### 4.1 模块一：INTERFACE pragma——内核与外界的门户

#### 4.1.1 概念说明

HLS 默认会为函数参数猜测接口类型，猜测结果往往不产生真正的内存端口。`#pragma HLS INTERFACE` 让我们**显式声明**每个参数映射到什么硬件接口。读带宽内核用了其中两种：

- `m_axi port=in0 offset=slave bundle=gmem0 max_read_burst_length=16`：把指针 `in0` 变成一个 AXI master 内存端口。
- `s_axilite port=size bundle=control`：把标量 `size` 变成控制寄存器里的一个 32 位寄存器。

三个 m_axi 参数的含义：

- **`bundle=gmem0`**：端口分组。**同一个 bundle 名下的所有指针共享一个 AXI master 端口；不同 bundle 名各自得到独立端口**。内核里 `in0` 用 `gmem0`、`in1` 用 `gmem1`，正是为了得到两个物理上独立的并发端口——uBench 五因素中的「并发端口数」就是这么来的。bundle 名还会出现在链接配置 `ubench.ini` 的 `sp` 行里（`krnl_ubench_1.in0:HBM[0]` 中的 `in0` 即此端口），详见 u3-l3。
- **`offset=slave`**：指针基地址不写死在电路里，而是由主机在运行时通过 `s_axilite` 控制寄存器写入。这就是为什么内核里还必须给 `in0`、`in1` 各加一行 `s_axilite`——指针本身（64 位基地址）也成了控制寄存器中的一份数据。
- **`max_read_burst_length=16`**：该端口一次读突发最多 16 个 beat。512 bit × 16 beat = 1024 字节，即一次突发最多连续搬 1 KB。

`s_axilite` 侧则形成一个名为 `control` 的寄存器组：`in0` 基地址、`in1` 基地址、`size` 值、`return`（内核启动/完成握手位）。主机 `setArg` 的每一站最终都落到这里（u2-l2 详述）。

#### 4.1.2 核心流程

一个指针参数从 C++ 声明到硬件端口的映射过程：

```text
C++ 参数: volatile INTERFACE_WIDTH* in0
    │
    ├─ #pragma INTERFACE m_axi  port=in0 bundle=gmem0 offset=slave max_read_burst_length=16
    │       └─ 生成 AXI master 端口 in0（512 bit 数据通道，读突发 ≤ 16 beat）
    │           └─ 端口名 in0 进入 ubench.ini 的 sp 行，绑定到某个 DDR/HBM bank
    │
    └─ #pragma INTERFACE s_axilite port=in0 bundle=control
            └─ 在控制寄存器组里生成「in0 基地址」寄存器（运行时由主机写入）
```

内核启动时的完整握手：

1. 主机通过 `s_axilite` 写入 `in0` 基地址、`in1` 基地址、`size`。
2. 主机写 `return` 对应的启动位，内核开始运行。
3. 内核通过两个 `m_axi` master 端口并发读内存。
4. 内核函数返回，`return` 上的完成位翻转，主机 `q.finish()` 返回。

#### 4.1.3 源码精读

内核函数签名——两个 `INTERFACE_WIDTH` 指针参数就是两个读端口，`size` 是每个端口要读的「宽字」个数：

[krnl_ubench.cpp:3-4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L3-L4)
```cpp
extern "C" {
    void krnl_ubench(volatile INTERFACE_WIDTH* in0, volatile INTERFACE_WIDTH* in1, const int size) {
```
`extern "C"` 关闭 C++ 名字修饰，保证综合出的内核名就是 `krnl_ubench`——主机端 `cl::Kernel(context, program, "krnl_ubench")` 按这个名字查找。

两条 m_axi 指示——注意 `bundle` 名不同（`gmem0`/`gmem1`），`max_read_burst_length` 均为 16：

[krnl_ubench.cpp:5-6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L5-L6)
```cpp
#pragma HLS INTERFACE m_axi port=in0 offset=slave bundle=gmem0   max_read_burst_length=16 
#pragma HLS INTERFACE m_axi port=in1 offset=slave bundle=gmem1   max_read_burst_length=16 
```

s_axilite 控制 组——四行分别把两个指针的基地址、`size`、函数返回映射进同一个 `control` 寄存器组：

[krnl_ubench.cpp:8-12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L8-L12)
```cpp
#pragma HLS INTERFACE s_axilite port=in0 bundle=control
#pragma HLS INTERFACE s_axilite port=in1 bundle=control
#pragma HLS INTERFACE s_axilite port=size bundle=control
#pragma HLS INTERFACE s_axilite port=return bundle=control
```

值得注意的细节：如果**漏写** `s_axilite port=in0/in1` 这两行，`offset=slave` 的基地址就没有地方放，综合会报错或行为异常——这是新手改这类内核时最常见的坑。改端口数量时（u3-l2），新增指针参数必须同步新增 m_axi 与 s_axilite 两行 pragma。

#### 4.1.4 代码实践

**实践目标**：数清 control 寄存器组的内容，并理解 burst 参数改哪里。

**操作步骤**（源码阅读型，无需硬件）：

1. 打开 [krnl_ubench.cpp:5-12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L5-L12)，把每行 pragma 对应的「寄存器或端口」填进下表：

   | pragma 行 | 生成的硬件 |
   |---|---|
   | m_axi in0, bundle=gmem0 | AXI master 端口 in0（512 bit，burst ≤ 16） |
   | m_axi in1, bundle=gmem1 | ？（自己填） |
   | s_axilite in0 | control 组里的 in0 基地址寄存器 |
   | s_axilite in1 | ？ |
   | s_axilite size | ？ |
   | s_axilite return | ？ |

2. 在主机侧验证你的答案：查看 [host.cpp:157-163](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L157-L163)，数一数 `setArg` 的调用——`setArg(0)`、`setArg(1)` 写两个 buffer（即两个基地址），`setArg(2)` 写 `dataSize`。三次 setArg 正好对应 control 组里的三个数据寄存器。

3. 把第 5、6 行的 `max_read_burst_length=16` 改成 `256`（只改 pragma，其余不动），记下改动位置。

**需要观察的现象**：步骤 2 中 setArg 次数与 s_axilite 数据寄存器个数一一对应。

**预期结果**：control 组 = {in0 基地址, in1 基地址, size, 启动/完成位}；改 burst 只影响 m_axi 端口行为，不增减寄存器。综合后报告中的差异（如「Max Read Burst Length」字段从 16 变 256）**待本地验证**（需要 Vitis 环境，可运行 `make kernel TARGET=sw_emu` 后查看编译日志，sw_emu 下功能不变）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `in0` 和 `in1` 的 bundle 都写成 `bundle=gmem0`，会发生什么？

**答案**：两个指针共享同一个 AXI master 端口，物理上只剩一个并发读通道。带宽理论上限从「2 × 端口带宽」降为「1 × 端口带宽」，读请求还会在共享端口上排队。这正是 uBench 用不同 bundle 名制造并发端口的原因。

**练习 2**：`offset=slave` 与 `offset=direct` 有何区别？为什么 uBench 选 `slave`？

**答案**：`direct` 会把基地址偏移计算电路综合进内核（偏移在综合期确定或作为标量输入参与内核内地址计算）；`slave` 则把基地址做成运行时由主机写入的控制寄存器。uBench 每次运行都要对不同的 `cl::Buffer` 启动内核，基地址必须运行时可变，所以选 `slave`。

**练习 3**：`max_read_burst_length=16`、端口位宽 512 bit 时，一次突发最多连续搬多少字节？改成 256 后是多少？

**答案**：512 bit = 64 B，16 beat × 64 B = 1024 B（1 KB）；256 beat × 64 B = 16384 B（16 KB）。突发越长，地址/握手开销摊得越薄，越容易在大 payload 下逼近峰值带宽（这正是 KNN 案例中 suboptimal 与 optimal 的唯一差异，见 u6-l2）。

### 4.2 模块二：ap_uint 宽类型——一次搬 64 字节

#### 4.2.1 概念说明

`ap_uint<W>` 是 Vitis HLS 的任意精度无符号整数类型（声明于 `ap_int.h`），`W` 可以是任意正整数，不受 CPU 寄存器 32/64 位的限制。`ap_uint<512>` 在硬件上就是一根 512 位的数据线，映射到 AXI 端口上就是 64 字节宽的数据通道——**端口位宽**这个 uBench 五因素，在代码里完全由它承载。

普通 C++ 写法 `unsigned long long` 最多 64 位；想一次访问 256、512、1024 位，就必须用 `ap_uint`。它支持正常的赋值、比较、位运算，所以内核循环体写起来和普通类型几乎一样。

关键在于 uBench 用一条**派生链**把位宽参数化了：

```text
DWIDTH (512)  ──宏展开──▶  INTERFACE_WIDTH = ap_uint<512>   （内核指针/变量的类型）
      │
      └──常量表达式──▶  WIDTH_FACTOR = DWIDTH/32 = 16       （1 次宽访问 = 16 个 32bit int）
```

想改端口位宽，只改 `DWIDTH` 一个常量，类型和换算因子自动跟着变——这是本讲综合实践的核心机制。

#### 4.2.2 核心流程

端口位宽如何进入理论带宽公式。理论峰值带宽由「频率 × 端口数 × 位宽」决定：

\[
\text{峰值带宽（字节/秒）} = f_{\text{kernel}} \times N_{\text{port}} \times \frac{\text{DWIDTH}}{8}
\]

代入本工程（内核频率 300 MHz、2 端口、512 bit）：

\[
300 \times 10^6 \times 2 \times \frac{512}{8} = 38.4 \text{ GB/s}
\]

而主机侧与内核侧的「数据量语言」不同，需要 `WIDTH_FACTOR` 翻译：

- 主机世界的单位是 **32 bit int 的个数**（`payload`、`dataSize`，缓冲区按 `sizeof(int) * dataSize` 分配）；
- 内核世界的单位是 **宽字（INTERFACE_WIDTH）的个数**（参数 `size`，循环下标 `j`）。

翻译发生在启动内核前一刻：`dataSize = dataSize / WIDTH_FACTOR`（见下节源码）。例如 payload = 256 个 int（1 KB）时，`size = 256/16 = 16` 个 512 bit 宽字，内核每个端口读 16 个宽字，`16 × 64 B = 1 KB`，两侧账目吻合。

#### 4.2.3 溯源精读

`krnl_config.h` 全文只有 7 行，却同时被内核和主机 include（这就是五件套中的「参数桥梁」）：

[krnl_config.h:1-7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L1-L7)
```cpp
#include "ap_int.h"          // Vitis HLS 任意精度类型库
#include <inttypes.h>

const int DWIDTH = 512;                          // 端口位宽：唯一的“总开关”
#define INTERFACE_WIDTH ap_uint<DWIDTH>           // 宽类型别名
const int WIDTH_FACTOR = DWIDTH/32;               // 1 个宽字 = 16 个 int
const int NUM_ITERATIONS = 10000;                 // 外层重复次数（见 4.3）
```

内核里所有涉及宽类型的位置都写 `INTERFACE_WIDTH` 而不是具体的 `ap_uint<512>`：

[krnl_ubench.cpp:4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L4) 的参数 `volatile INTERFACE_WIDTH* in0, volatile INTERFACE_WIDTH* in1`，以及 [krnl_ubench.cpp:14-15](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L14-L15) 的临时变量 `volatile INTERFACE_WIDTH temp_data_0; volatile INTERFACE_WIDTH temp_data_1;`——位宽变化时这些代码**一行都不用改**。

主机侧的翻译点（启动内核前把 int 个数换成宽字个数）：

[host.cpp:154-161](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L154-L161)
```cpp
//Setting the compute kernel arguments
dataSize = dataSize / WIDTH_FACTOR;     // int 个数 → 宽字个数
...
OCL_CHECK(err, err = cmpt_krnl[i].setArg(j, source_in_buffer[i*NUM_PORT+j])); 
OCL_CHECK(err, err = cmpt_krnl[i].setArg(j, dataSize));   // size 参数 = 宽字个数
```

缓冲区大小则始终按 int 个数计算，与位宽无关：

[host.cpp:133-140](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L133-L140) 中 `cl::Buffer(..., sizeof(int) * dataSize, ...)`——`dataSize` 此时还是 int 个数（除以 `WIDTH_FACTOR` 发生在 L155，位于建缓冲之后），所以改 `DWIDTH` 不需要动这里。

#### 4.2.4 代码实践

**实践目标**：把 `DWIDTH` 从 512 改为 256，验证「派生链让改动集中在一处」，并推演所有联动影响。

**操作步骤**：

1. 复制工程目录 `read/DDR/2ports_512bit` 为 `read/DDR/2ports_256bit`（遵循 u1-l2 讲过的「目录名即参数组合」约定，不要原地改）。
2. 在新目录的 `krnl_config.h` 里把 `DWIDTH = 512` 改为 `DWIDTH = 256`。其余文件先一律不动。
3. 逐文件核对，填这张清单（答案在「预期结果」）：

   | 位置 | 是否需要改 | 原因 |
   |---|---|---|
   | `krnl_config.h` 的 `INTERFACE_WIDTH`、`WIDTH_FACTOR` | 否 | 宏/常量表达式自动派生为 `ap_uint<256>`、`256/32=8` |
   | `krnl_ubench.cpp` 的签名与 `temp_data_*` | 否 | 都写的是 `INTERFACE_WIDTH` |
   | `host.cpp` 的 `NUM_PORT`（[L16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L16)） | 否 | 端口**个数**没变（仍是 2），位宽与个数是独立参数 |
   | `host.cpp` 的缓冲区大小（L138） | 否 | 按 `sizeof(int)*dataSize` 分配，与位宽无关 |
   | `host.cpp` 的 `WIDTH_FACTOR` 换算（L155） | 否 | 自动变为除以 8；同一 payload 下 `size` 翻倍 |
   | `ubench.ini` 的 `sp` 行 | 否 | 端口名 `in0/in1` 未变，bank 绑定与位宽无关 |

4. 用纸笔推演：payload = 256 时，新工程传给内核的 `size` 是多少？（256/8 = 32 个宽字，每个 32 B，共 1 KB——总数据量不变。）
5. 按公式重算单端口理论峰值：300 MHz × 256/8 B = 9.6 GB/s，双端口 19.2 GB/s——恰好是 512 bit 版本的一半。

**需要观察的现象**：若装有 Vitis，可在新目录 `make check TARGET=sw_emu`（`DEVICE` 按环境填写，见 u1-l3），确认编译通过、运行时打印的带宽量级减半。

**预期结果**：真正必须修改的只有 `DWIDTH` 一行；这体现了 `krnl_config.h` 派生链的设计意图。sw_emu 的带宽数值本身无物理意义（u1-l3 结论），只看能否跑通；真机带宽减半**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`DWIDTH=256` 后 `WIDTH_FACTOR` 是多少？payload = 262144（循环上限）时传给内核的 `size` 是多少、对应多少字节？

**答案**：`WIDTH_FACTOR = 256/32 = 8`；`size = 262144/8 = 32768` 个宽字；`32768 × 32 B = 1 MB`。连续访问数据量上限仍是 1 MB，位宽改变不改变 payload 扫描的字节范围。

**练习 2**：为什么内核不直接用 `unsigned int*`（32 bit 端口）再循环 16 次凑出 64 字节？

**答案**：那样端口本身仍是 32 bit 宽，AXI 数据通道每 beat 只有 4 字节，理论峰值带宽只有 512 bit 端口的 1/16；「用 16 次 32 bit 访问」在时间上串行或占用多个 beat，无法达到「一拍搬 64 B」的效果。位宽是硬件资源（端口宽度），不是软件循环能弥补的。

**练习 3**：`INTERFACE_WIDTH` 用 `#define` 而 `DWIDTH`/`WIDTH_FACTOR` 用 `const int`，这个差异重要吗？

**答案**：对本仓库的使用方式而言不重要——两者在编译期都有确定值，HLS 综合时都能常量折叠。`#define` 的类型别名在 C++ 里更地道的写法是 `typedef`/`using`，这里沿用 C 风格宏是历史习惯。真正重要的是「单一定义点 + 派生」这个结构。

### 4.3 模块三：流水线与数据流——让端口每拍都在搬数据

#### 4.3.1 概念说明

有了宽端口，还要让它**不停歇**。本模块讲四个互相配合的机制：

- **`#pragma HLS PIPELINE II=1`（内层循环）**：把内层 `j` 循环做成每时钟周期启动一次迭代的流水线。对 `temp_data_0 = in0[j]` 来说，即每拍向端口 `in0` 发出一个宽字读请求。II=1 是带宽的上限工况：任何一拍空闲都是浪费。
- **`#pragma HLS DATAFLOW`（函数体级）**：让区域内的多个循环/任务**并行执行**，各自生成独立的数据通路。本内核的两个循环被拆成两个并发的硬件块：一个驱动 `in0`，一个驱动 `in1`，两端口同时读——这是「并发端口数 = 2」在时序上的兑现。
- **`volatile`**：防止读被优化掉。`temp_data_0 = in0[j]` 读进来的值从未被使用，正常编译器会判定为死代码直接删除（DCE，dead code elimination），整个测试就失效了。`volatile` 告诉编译器「这块内存可能被外部改变、每次读写都必须真实发生」，于是每次 `in0[j]` 都产生真实的 AXI 读事务。指针 `in0/in1` 与临时变量 `temp_data_0/temp_data_1` 都加了 `volatile`，双保险。
- **`NUM_ITERATIONS = 10000`（外层循环）**：把整个读过程重复一万遍，把内核执行时间放大到毫秒量级，让主机端的 `std::chrono` 计时误差（时钟粒度、启动开销）相对变小。带宽公式里那个神秘的 `0.000010000` 正是 `NUM_ITERATIONS / 10⁹`（见 u2-l3）。

一句话串联：**DATAFLOW 让两个端口并发，PIPELINE II=1 让每个端口每拍都在请求，volatile 保证读不被删，NUM_ITERATIONS 把时间放大到可测**。

#### 4.3.2 核心流程

DATAFLOW 区域内的执行时序（两个循环硬件化为并行数据通路）：

```text
时间 ──────────────────────────────────────────▶

数据通路 A（循环一）:  in0 ████████████████████████   NUM_ITERATIONS × size 次读
数据通路 B（循环二）:  in1 ████████████████████████   （与 A 同时开始、同时结束）

端口 in0 的读请求流:   |w0|w1|w2|w3|...   ← PIPELINE II=1：每拍一个宽字请求
                       （实际由 AXI 组合成 ≤ 16 beat 的突发）
```

带宽换算的账目（对齐 [host.cpp:172](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L172) 的公式）：

\[
\text{bw (GB/s)} = \underbrace{\text{payload} \times 4}_{\text{每端口每轮字节数}} \times \underbrace{0.000010000}_{= 10000/10^9} \Big/ t_{\text{sec}} \times N_{\text{kernel}} \times N_{\text{port}}
\]

分子展开即 `payload × 4 B × NUM_ITERATIONS × 端口数 / 10⁹` = 总传输字节的 GB 数，除以耗时即为 GB/s。公式成立的隐含前提是：DATAFLOW 真的让两个端口并发跑完了各自的 `NUM_ITERATIONS × size` 次读——如果两个循环被串行化，实测「带宽」会虚高（时间翻倍但公式仍按双端口计数），所以这个 pragma 不只是优化，而是**测量有效性的前提**。

#### 4.3.3 源码精读

DATAFLOW 指示与两个 volatile 临时变量：

[krnl_ubench.cpp:14-17](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L14-L17)
```cpp
volatile INTERFACE_WIDTH temp_data_0;
volatile INTERFACE_WIDTH temp_data_1;

#pragma HLS DATAFLOW
```

第一个读循环——外层 `i` 重复 NUM_ITERATIONS 次，内层 `j` 以 II=1 扫完 size 个宽字：

[krnl_ubench.cpp:19-24](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L19-L24)
```cpp
for (int i = 0; i < NUM_ITERATIONS; i++) {
    for (int j = 0; j < size; j++) {
#pragma HLS PIPELINE II=1
        temp_data_0 = in0[j];
    }
}
```

第二个读循环与第一个**完全对称**，只是换成了 `in1`/`temp_data_1`：

[krnl_ubench.cpp:26-31](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L26-L31)
```cpp
for (int i = 0; i < NUM_ITERATIONS; i++) {
    for (int j = 0; j < size; j++) {
#pragma HLS PIPELINE II=1
        temp_data_1 = in1[j];
    }
}
```

两个循环之间**没有任何数据依赖**（各写各的临时变量），这是 DATAFLOW 合法化的前提；如果循环二依赖循环一的输出，HLS 会拒绝并行化甚至报错。`NUM_ITERATIONS` 定义在 [krnl_config.h:7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L7)，内外层循环结构画成伪代码就是：

```text
并行执行（DATAFLOW）:
    通路 A: repeat NUM_ITERATIONS 次: 以 II=1 连读 size 个宽字 from in0
    通路 B: repeat NUM_ITERATIONS 次: 以 II=1 连读 size 个宽字 from in1
```

#### 4.3.4 代码实践

**实践目标**：通过「缩短实验」验证 NUM_ITERATIONS 的放大作用，并论证两个循环为什么不能合并。

**操作步骤**：

1. 在自己复制的实验目录里，把 [krnl_config.h:7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L7) 的 `NUM_ITERATIONS` 从 10000 改为 2（仅用于观察，测带宽时必须改回）。
2. 若装有 Vitis：`make check TARGET=sw_emu` 运行（emulation 下 payload 固定为 256，见 [host.cpp:102-104](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L102-L104)），观察打印的 `Execution time` 与带宽的变化方向。
3. 论证题（纸笔）：假如把两个循环合并成一个——`for j: {temp0=in0[j]; temp1=in1[j];}`——去掉 DATAFLOW，带宽测量会差在哪里？提示：合并后单循环体里有两个 load，II=1 仍可能成立，两端口还是并发的；但想一想两个端口的**读序列长度**是否还是 `NUM_ITERATIONS × size`、以及这种写法是否还能推广到「端口数作为参数」的模板（见 u3-l2 的端口扩展指南与 u5-l2 的 kernelcode_gen.py，生成器正是按「每端口一个循环」的模式产码的）。

**需要观察的现象**：NUM_ITERATIONS 改小后，同一 payload 的 `Execution time` 显著缩短；sw_emu 下时间数值无物理意义，只看趋势。

**预期结果**：时间大致按 NUM_ITERATIONS 比例缩短（真机上近似线性；sw_emu 中趋势性成立，具体数值**待本地验证**）。论证题参考结论：合并循环对 2 端口也能并发读，但「每端口一个独立循环」的结构让端口数成为可枚举展开的模板参数——这是 auto_collect 代码生成器（u5-l2）能按任意端口数自动产码的结构基础；此外独立循环也天然保证两端口的访问序列互不干扰。

#### 4.3.5 小练习与答案

**练习 1**：删掉两处 `volatile`（指针和临时变量都删），C++ 语义层面会发生什么？对带宽测量有什么后果？

**答案**：C++ 语义不变（程序仍然「正确」）。但综合/编译器会做激进的死代码消除——读入的值从未使用，`in0[j]` 的加载可被整体删除，最终内核可能什么都不读、瞬间返回，「带宽」变成天文数字。`volatile` 是让读操作在硬件层面真实发生的保险栓。

**练习 2**：`PIPELINE II=1` 放在内层循环而非外层，为什么？

**答案**：带宽由**连续访存的节拍**决定。内层循环以 II=1 展开成每拍一个宽字请求，才能打满端口；外层 `i` 只是整体重复，其迭代间隔（受内存延迟影响）不直接决定带宽。若只想流水化外层，内层访存仍可能每拍只发少量请求，端口利用率大幅下降。

**练习 3**：为什么带宽公式（host.cpp L172）里要乘 `NUM_PORT`，而这个乘法在内核代码里找不到对应物？

**答案**：因为「每个端口都搬了 payload×4×NUM_ITERATIONS 字节」这一事实是 DATAFLOW 并行结构**隐式**保证的——内核源码里没有 `NUM_PORT` 这个概念，端口数体现为指针参数/循环的个数。主机侧用 `NUM_PORT` 宏把这份结构性知识补进公式。这也解释了为什么改端口数时必须同时改内核（加参数加循环）、`ubench.ini`（加 sp 行）和主机（改 `NUM_PORT`），三处缺一不可——详见 u3-l2。

## 5. 综合实践

把本讲三个模块串成一次完整的参数修改实验：**「位宽减半、突发加长」**。

背景设定：假设你在 U200 上做实验，想验证「512 bit 端口 + 短突发」与「256 bit 端口 + 长突发」哪个实测带宽更高。

任务步骤：

1. **建实验目录**：复制 `read/DDR/2ports_512bit` 为 `read/DDR/2ports_256bit_burst256`（自己定名，但要能从目录名读出参数组合）。
2. **改内核**：在新目录 [krnl_ubench.cpp:5-6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L5-L6) 把两处 `max_read_burst_length=16` 改为 `256`。
3. **改位宽**：把 [krnl_config.h:4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L4) 的 `DWIDTH` 改为 `256`。
4. **写改动清单**：对照 4.2.4 的表格，逐文件记录「改/不改及原因」，形成一份 diff 说明文档（重点解释：为什么 `host.cpp` 与 `ubench.ini` 这次都不用动）。
5. **预算理论值**：用 4.2.2 的公式算出新设计的双端口理论峰值（300 MHz × 2 × 32 B = 19.2 GB/s），并对比原设计（38.4 GB/s）；再计算两种设计单次突发的最大数据量（原：16 × 64 B = 1 KB；新：256 × 32 B = 8 KB）。
6. **验证**（可选，需 Vitis）：`make check TARGET=sw_emu` 确认编译运行通过；真机带宽对比**待本地验证**。
7. **思考题**：新设计单突发可搬 8 KB（是原来的 8 倍），但理论峰值减半。据此预测：payload 处于哪个范围时新设计可能反超？依据是 u1-l1 讲过的「突发开销摊薄 vs 流水爬坡」权衡（小 payload 时长突发的摊薄优势更明显，但峰值天花板更低）。

## 6. 本讲小结

- **INTERFACE pragma 是内核的门户**：`m_axi` 的 `bundle` 决定端口分组（不同 bundle = 独立并发端口），`offset=slave` 把指针基地址交给主机运行时写入，`max_read_burst_length` 设定单次突发的 beat 上限；`s_axilite` 组成 control 寄存器组（两个基地址 + size + 启动/完成位），与主机 `setArg` 一一对应。
- **ap_uint 派生链让位宽单点可改**：`DWIDTH(512) → INTERFACE_WIDTH(ap_uint<512>) → WIDTH_FACTOR(16)`；改位宽只需改一处，内核签名/临时变量自动适配，主机 `dataSize/WIDTH_FACTOR` 换算自动同步。
- **DATAFLOW + PIPELINE II=1 是带宽工况**：DATAFLOW 让两个读循环并行（兑现「并发端口数 = 2」），PIPELINE II=1 让每端口每拍发一个宽字请求；二者同时是带宽公式成立的前提。
- **volatile 与 NUM_ITERATIONS 是测量保险**：volatile 防止读被死代码消除，NUM_ITERATIONS=10000 把时间放大到可测，公式中的 0.000010000 就是 10000/10⁹。
- **理论峰值 = 频率 × 端口数 × 位宽/8**：本工程 300 MHz × 2 × 512 bit = 38.4 GB/s，实际能达到多少由突发长度与 payload 大小决定——正是后续讲义的实验对象。

## 7. 下一步学习建议

- **下一讲 u2-l2（主机端编程模型）**：本讲只看了 `setArg` 与 control 寄存器的对接，下一讲从 `host.cpp` 第一行讲起——`xcl::get_xil_devices` 发现设备、`read_binary_file` 加载 xclbin、`cl::Buffer` 创建与 `cl_mem_ext_ptr_t` 的 bank 绑定、`enqueueTask` 启动。内核侧与主机侧在此闭合。
- **u2-l3（测量方法学）**：深入拆解本讲埋下的伏笔——payload 倍增扫描、`std::chrono` 计时窗口、带宽公式每一项的单位，以及主机计时包含内核启动开销的系统性误差。
- **提前翻阅 u3-l1（读带宽内核逐行精读）**：如果想在读带宽这条线上继续深挖 DDR 与 HBM 两个版本的对照，可提前阅读；本讲已覆盖其大部分语法点，u3-l1 侧重「扩展到 3 个端口」的手術式操作。
- **源码自测**：合上讲义，尝试只看 [krnl_ubench.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp) 的 37 行源码，逐行说出每条 pragma 的作用——能全部说清，即可进入下一讲。

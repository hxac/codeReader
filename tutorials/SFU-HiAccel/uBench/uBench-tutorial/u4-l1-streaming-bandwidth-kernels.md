¤ 流带宽微基准：内核到内核的 hls::stream 数据通路

¤¤ 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `hls::stream<pkt>`（`pkt = ap_axiu<512,0,0,0>`）加上 `¤pragma HLS INTERFACE axis` 如何在硬件里生成一条 AXI4-Stream（AXIS）端口，以及它与 `m_axi` 内存端口的本质区别。
2. 解读 `ubench.ini` 中 `stream_connect=krnl_streamWrite_1.kout1:krnl_streamRead_1.kin1` 的「名字配对」机制，并说出它与 `sp=`/`slr=`/`nk=` 各自管什么。
3. 说明流带宽公式里 `* 2` 系数的来历（两条并发流）、payload 扫描范围为什么放大到 `262144*4`（对应每次运行最多约 40GB 每流的数据量），并能独立推导整个带宽公式。
4. 动手把本微基准从 2 条流扩展为 4 条流（内核、ini、主机三处联动），并核算新的理论峰值带宽。

本讲是「片外带宽」系列（u3）的对照篇：u3 测的是**内核 ↔ DDR/HBM** 的内存通路，本讲测的是**内核 ↔ 内核**的片上/跨 SLR 流通路——这是流水线式 FPGA 加速器（后文的 KNN 归并、两级 load/compute 内核）最常用的数据搬运方式。

¤¤ 2. 前置知识

在进入源码前，先用通俗语言补齐几个概念。前三个在 u2-l1、u3-l3 已经建立，这里只做一句话回顾；第四、五个是本讲的新主角。

**（1）`m_axi` 端口回顾（u2-l1）**：`¤pragma HLS INTERFACE m_axi` 把内核的指针参数变成 AXI4 内存映射（Memory-Mapped）主端口，带地址通道、按突发（burst）传输，必须连到 DDR/HBM 等片外内存。它的吞吐由「频率 × 端口数 × 位宽」决定上限，由「突发长度 × 连续数据量」决定逼近程度。

**（2）链接期连线回顾（u3-l3）**：`ubench.ini` 的 `⟦connectivity⟧` 段在 `v++ -l` 链接阶段生效：`slr=` 把内核实例（CU）放到某个 SLR 超级逻辑区域，`sp=` 把内存端口接到某个内存通道，`nk=` 决定每种内核实例化几个、实例名叫什么（如 `krnl_streamWrite_1`）。主机程序必须与这些名字手工对齐。

**（3）主机骨架回顾（u2-l2）**：`xcl::get_xil_devices()` 枚举设备 → `read_binary_file` 加载 xclbin → 按「内核名:⟦CU名⟧」创建 `cl::Kernel` → `setArg` → `enqueueTask` → `finish`。本讲主机代码 90% 与读带宽版相同，只讲差异。

**（4）新概念：AXI4-Stream（AXIS）与 `hls::stream`**。

AXI4 家族里有两大类协议：

- **AXI4 内存映射（AXIMM）**：每次传输要带地址，主设备（内核）发出地址，内存控制器译码、拆成突发。适合访问片外内存。
- **AXI4-Stream（AXIS）**：**没有地址**。数据像流水一样从源头（source）顺序流向终点（sink），握手信号只有 `TVALID`/`TREADY`/`TLAST`（以及数据 `TDATA`）。只要下游 `TREADY` 拉高，上游每拍都能送一个字。适合「生产者—消费者」直连，不需要经过内存。

在 Vitis HLS 里，一个 C++ 的 `hls::stream<pkt>&` 函数引用参数，配上 `¤pragma HLS INTERFACE axis port=...`，就会综合成内核上的一个 AXIS 端口。`hls::stream` 在硬件里实现为一个**有限深度的 FIFO 队列**，它的两个基本操作都是**阻塞**的：

```text
write(v)  : FIFO 满 → 阻塞等待（下游反压 / back-pressure）
read()    : FIFO 空 → 阻塞等待（上游无数据）
```

这层阻塞语义非常重要：**流的两端内核必须同时运行**，否则生产者把 FIFO 写满后就永远卡住。后文会看到主机端为此把命令队列设成了乱序模式。

**（5）生产者—消费者的直觉**。把数据从内核 A 搬到内核 B，有两种做法：A 写回 DDR、B 再从 DDR 读回（两次片外带宽、两倍延迟）；或者 A 直接通过流把数据「递」给 B（不占 DDR、零内存往返）。uBench 用本微基准量化后一种通路能跑到多快，从而回答「我的多级流水线加速器，级间数据通路会不会成为瓶颈」。

¤¤ 3. 本讲源码地图

本讲涉及的核心文件都在 `ubench/streaming_bandwidth/datacenter/2ports_512bit/` 下：

| 文件 | 作用 |
| --- | --- |
| `src/krnl_config.h` | 契约头：定义 `DWIDTH=512`、`INTERFACE_WIDTH`、流载荷类型 `pkt`、`WIDTH_FACTOR=16`、`NUM_ITERATIONS=10000`，被内核与主机共用 |
| `src/krnl_streamWrite.cpp` | **生产者内核**：向 2 条 AXIS 流（`kout1`/`kout2`）反复写常量数据 |
| `src/krnl_streamRead.cpp` | **消费者内核**：从 2 条 AXIS 流（`kin1`/`kin2`）反复读并「消费」数据 |
| `ubench.ini` | 链接期连线表：`slr=`/`sp=`/`nk=` + 两条 `stream_connect=`，把生产者的流端口与消费者的流端口配成对 |
| `src/host.cpp` | 主机程序：创建一对内核对象、绑 bank、成对启动、计时并按公式输出流带宽 |
| `Makefile` | 构建脚本：把两个内核各编成 `.xo`，再用 `--config ./ubench.ini` 链接成 `ubench.xclbin` |

参考材料（不必逐行读）：

- `ubench/streaming_bandwidth/datacenter/README.md`：官方给出的「加流端口 / 改位宽 / 改数据量」三步指南，本讲综合实践基本是它的展开。
- `auto_collect/connectivity_gen.py` 与 `auto_collect/kernelcode_gen.py`：自动生成版 ini 与内核源码的模板，可与手写版互相印证（u5-l3 展开）。

与前几讲的「五件套」相比，这里只有一处结构性替换：**读/写带宽版的单个内核 + `m_axi` 双端口，换成了「一对内核 + 2 条流」**；契约头、Makefile 骨架、主机骨架全部沿用。

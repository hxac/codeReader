# 流带宽微基准：内核到内核的 hls::stream 数据通路

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `ap_axiu<512,0,0,0> pkt` 与 `hls::stream<pkt>` 如何共同构成一个 AXIS（AXI4-Stream）流接口，以及它与前几讲 `m_axi` 内存端口的本质区别。
2. 解读 `ubench.ini` 中 `stream_connect=krnl_streamWrite_1.kout1:krnl_streamRead_1.kin1` 的配对机制：端口名来自哪里、CU 名来自哪里、为什么这条连线只在链接期生效。
3. 说明流带宽公式中 `×2` 系数的含义（并发流端口数），以及 payload 上限被放大到 `262144*4`（单次 4MB、总量约 40GB）的原因。
4. 独立完成「2 个流端口扩展为 4 个」的改造，并核对每条流两端端口名一致。

本讲聚焦 `ubench/streaming_bandwidth/datacenter/2ports_512bit/` 工程。它与前三讲的片外带宽微基准同属「五件套」骨架，但数据通路完全不同：**数据不再经过 DDR，而是在两个内核之间通过片上流直传**。

## 2. 前置知识

### 2.1 从「内核—内存」到「内核—内核」

前三讲的微基准里，数据路径是 `主机内存 → DDR/HBM → 内核`，内核通过 `m_axi` 端口读写片外存储。而真实的大型 FPGA 设计（比如第 6 单元的 KNN）常常把多个内核串成流水线：上一个内核的输出直接流给下一个内核，**不落片外存储、不过主机**。这条通路叫内核间流（accelerator-to-accelerator streaming），它的带宽上限由片上连线和 SLR 间互连决定，与 DDR/HBM 无关——所以流带宽微基准的参数空间里没有 `MAX_BURST_LENGTH`，也没有 `MEMORY_TYPE`（DDR/HBM 之分）。

### 2.2 AXIS 协议的直觉

AXI4-Stream（AXIS）是 AXI 家族里最简单的协议：**没有地址**，只有一对握手信号（发送方 `TVALID`、接收方 `TREADY`）加一排数据线（`TDATA`，另可选 `TLAST`、`TKEEP` 等信号）。发送方每拍（beat）送出一个数据字，接收方准备好才放行。因为没有地址通道，也就不存在「突发长度」这个调参维度——数据是无条件的连续拍。这直接解释了流版 README 中参数只剩三项（并发端口数、端口位宽、数据量）。

### 2.3 需要回顾的前几讲结论

- **u2-l1**：`#pragma HLS INTERFACE` 决定内核每个参数综合成什么硬件端口；`DATAFLOW` 让多个无依赖循环并行执行；`PIPELINE II=1` 让每个时钟拍发一个数据字。
- **u3-l3**：`ubench.ini` 是 `v++ -l --config` 的链接期配置，`sp`/`slr`/`nk` 分别管存储连线、SLR 放置与实例命名；主机端 CU 名「内核名:{内核名_N}」必须与之严格对齐。
- **u2-l3**：主机端 `std::chrono` 计时窗口包含内核启动开销；带宽公式里的魔数 `0.000010000` = `NUM_ITERATIONS / 1e9`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ubench/streaming_bandwidth/datacenter/2ports_512bit/src/krnl_config.h` | 契约头：定义 `DWIDTH`、`INTERFACE_WIDTH`、流数据包类型 `pkt`、`WIDTH_FACTOR`、`NUM_ITERATIONS`，被两个内核与主机三方共用 |
| `ubench/streaming_bandwidth/datacenter/2ports_512bit/src/krnl_streamWrite.cpp` | 发送内核：生成数据并通过两条 `hls::stream<pkt>` 流出 |
| `ubench/streaming_bandwidth/datacenter/2ports_512bit/src/krnl_streamRead.cpp` | 接收内核：从两条流读入数据并消费 |
| `ubench/streaming_bandwidth/datacenter/2ports_512bit/ubench.ini` | 链接配置：`slr` 放置、`sp` 存储连线、`stream_connect` 流配对、`nk` 实例数 |
| `ubench/streaming_bandwidth/datacenter/2ports_512bit/src/host.cpp` | 主机程序：创建成对内核、分配缓冲、成对启动、计时并计算流带宽 |
| `ubench/streaming_bandwidth/datacenter/2ports_512bit/Makefile` | 把两个内核源文件分别编成 `.xo` 再链接成 `ubench.xclbin`（本讲用于确认编译链接链路） |
| `ubench/streaming_bandwidth/datacenter/README.md` | 官方调参指南（有三处与代码不一致的文档滞后，本讲会逐一指出） |

## 4. 核心概念与源码讲解

### 4.1 AXIS 流接口：ap_axiu 与 hls::stream

#### 4.1.1 概念说明

在 Vitis HLS 中，要让一个内核拥有「流端口」，需要两样东西配合：

1. **数据包类型**：`ap_axiu<512, 0, 0, 0>` 定义了一个 AXIS 数据拍（beat）的位级布局。四个模板参数依次是数据宽度、`user` 信号宽度、`id` 信号宽度、`dest` 信号宽度。后三个置 0 表示这个端口**不带任何 side-band 信号**，只保留 `TDATA`（512 bit）、`TKEEP`/`TSTRB`（512/8 = 64 bit）和 `TLAST`（1 bit）。
2. **流对象**：函数签名里的 `hls::stream<pkt> &kout1` 是 HLS 的流抽象——`write()` 阻塞式发送、`read()` 阻塞式接收。配上 `#pragma HLS INTERFACE axis` 后，它综合成内核边界上一组实实在在的 AXIS 引脚。

为什么用 `ap_axiu` 而不是直接 `hls::stream<ap_uint<512>>`？因为内核到内核的连线最终要变成真实的 AXIS 硬件接口，`ap_axiu` 提供协议要求的完整信号集；裸 `ap_uint` 流在跨内核连接时缺乏标准信号语义。

#### 4.1.2 核心流程

发送侧每个端口的节拍：

```
初始化: temp_data = 100 (volatile)
重复 NUM_ITERATIONS 次:
    重复 size 次:            # II=1, 每拍一个 512bit 字
        v.data = temp_data   # 打包一个 beat
        kout.write(v)        # TVALID/TREADY 握手发出
```

接收侧与之镜像：`pkt v = kin.read(); temp_data = v.data;`。

关键量之间的关系（单个端口一次内核调用）：

\[ \text{流过字节数} = \text{size} \times \frac{\text{DWIDTH}}{8} = \frac{\text{payload}}{\text{WIDTH\_FACTOR}} \times 64\,\text{B} = \text{payload} \times 4\,\text{B} \]

其中 `size` 是主机传给内核的参数（单位：宽字个数），`payload` 是主机循环变量（单位：32bit int 个数）。理论峰值带宽沿用五因素模型：

\[ \text{BW}_{theory} = f \times N_{port} \times \frac{\text{DWIDTH}}{8} \]

注意：本工程的 Makefile 没有 `--kernel_frequency` 选项（那是 auto_collect 生成版才有的，见 u3-l2 的结论），频率由综合工具默认决定，实际峰值以待本地验证的实测频率为准。

#### 4.1.3 源码精读

先看契约头，它同时是两端内核和主机的唯一事实来源：

[ubench/streaming_bandwidth/datacenter/2ports_512bit/src/krnl_config.h:L1-L11](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/src/krnl_config.h#L1-L11)

- 第 4-5 行引入 `ap_axi_sdata.h`（`ap_axiu` 定义）与 `hls_stream.h`（`hls::stream` 定义）。
- 第 9 行 `typedef ap_axiu<DWIDTH, 0, 0, 0> pkt;` 是本讲的核心类型：512 bit 数据、无 user/id/dest。
- 第 10 行 `WIDTH_FACTOR = DWIDTH/32 = 16`，用于主机把 int 个数换算成宽字个数。

再看发送内核的接口与主体：

[ubench/streaming_bandwidth/datacenter/2ports_512bit/src/krnl_streamWrite.cpp:L4-L17](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/src/krnl_streamWrite.cpp#L4-L17)

- 第 4 行签名：`in0` 是 m_axi 端口，`kout1`、`kout2` 是两条流。**注意：`in0` 在整个内核体内从未被解引用**——发送的数据是常量 100，不是从 DDR 读来的。这个 m_axi 端口（连同 ini 里它的 `sp` 连线和主机为它分配的缓冲）是模板对称性留下的「空挂」端口，实际测量的是纯流通路。
- 第 8-9 行 `#pragma HLS INTERFACE axis port = kout1/kout2`：把 `hls::stream<pkt>` 映射为内核边界的 AXIS 端口。每个流参数一条 pragma，与 `m_axi` 用 `bundle` 分组是两套机制。
- 第 14-15 行两个 `volatile` 临时变量：每个并发循环独占一个，这是 DATAFLOW 区内两段循环无共享依赖的前提（与 u3-l1 读带宽内核完全同构）。

发送循环本体：

[ubench/streaming_bandwidth/datacenter/2ports_512bit/src/krnl_streamWrite.cpp:L19-L35](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/src/krnl_streamWrite.cpp#L19-L35)

- 内层 `PIPELINE II=1`（第 21 行）：每端口每拍发出一个 512 bit 的 beat，这是流带宽的吞吐上限来源。
- 第 22-24 行三步：构造 `pkt`、赋 `.data`、`kout1.write(v1)`。流写入是副作用，不会被编译器消除。
- 两段循环（第 19-26、28-35 行）在 `DATAFLOW`（第 17 行）下并行执行，分别驱动 `kout1` 和 `kout2`。

接收内核是精确的镜像：

[ubench/streaming_bandwidth/datacenter/2ports_512bit/src/krnl_streamRead.cpp:L4-L33](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/src/krnl_streamRead.cpp#L4-L33)

`kin1.read()`（第 22 行）在流空时阻塞等待；读到的值赋给 `volatile` 变量（第 23 行）防止读操作被死代码消除。`out0` 与发送内核的 `in0` 一样是空挂的 m_axi 端口。

最后一点工程细节：两个内核共用同一个 `krnl_config.h` 的 `typedef`，保证 `stream_connect` 两端的数据拍位宽严格一致（512 bit 对 512 bit）。

#### 4.1.4 代码实践

**实践目标**：验证位宽是单点配置，并推演位宽变化的影响面。

**操作步骤**：

1. 打开 `krnl_config.h`，把第 7 行改为 `const int DWIDTH = 256;`。
2. 在纸上推演所有派生量的变化：`INTERFACE_WIDTH` 变为 `ap_uint<256>`、`pkt` 变为 `ap_axiu<256,0,0,0>`、`WIDTH_FACTOR` 变为 8。
3. 检查两个内核源文件与 `host.cpp`：确认不需要改动任何其他代码（`host.cpp` 第 182 行的 `dataSize/WIDTH_FACTOR` 会自动按 8 换算）。
4. 若本机装有 Vitis 2020.2，可执行 `make check TARGET=sw_emu DEVICE=<平台>` 验证功能；否则做纯推演。

**需要观察的现象**：

- 每档 payload 实际字节数（`payload×4`）不变——位宽换算被 `WIDTH_FACTOR` 吸收（承接 u3-l2 的结论）。
- 2 端口 256 bit 的理论峰值降为原来的一半；若同时把端口扩成 4 个，理论峰值恢复不变（乘积原理）。

**预期结果**：改动只落在 `krnl_config.h` 一行。带宽公式不需要动（它与位宽解耦）。真实硬件上的实测带宽变化**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`ap_axiu<512, 0, 0, 0>` 的三个 0 分别消掉了哪些信号？对带宽测量有什么好处？

**答案**：三个 0 是 `user`、`id`、`dest` 三个 side-band 信号的宽度，置 0 即端口不综合出 `TUSER/TID/TDEST` 引脚，只剩 `TDATA(512)`、`TKEEP/TSTRB(64)`、`TLAST(1)`。好处是连线资源几乎全给数据线，测得的带宽最接近纯数据通路的物理极限，不受 side-band 负担干扰。

**练习 2**：为什么流微基准的参数空间里没有 `MAX_BURST_LENGTH`？

**答案**：AXI4 的突发（burst）是地址通道协议的概念——读事务在 `AR` 通道发一个地址、`R` 通道返回一串 beat，突发长度是这串 beat 的上限。AXIS 没有地址通道，数据是无条件的连续拍，自然没有「突发长度」可调。这正是 u1-l1 中「流带宽没有突发维度」的协议层原因。

**练习 3**：如果把发送端 `pkt` 定义成 512 bit、接收端定义成 256 bit，会发生什么？

**答案**：`stream_connect` 两端的端口位宽不匹配，链接期无法直接配对（需要插宽度转换器，Vitis 不会自动为内核间流做这件事）。两端必须共享同一份 `typedef`——本工程用公共 `krnl_config.h` 从机制上保证了一致。

### 4.2 stream_connect 配对机制：链接期连线与命名契约

#### 4.2.1 概念说明

两个内核的流端口在源码里只是「悬空」的 `hls::stream` 参数，**谁连谁完全由链接期决定**。`ubench.ini` 里的 `stream_connect` 指令把一个 CU（计算单元）的输出流端口连到另一个 CU 的输入流端口。它和 u3-l3 讲过的 `sp`/`slr`/`nk` 同住 `[connectivity]` 段、同在 `v++ -l --config` 时生效，对主机运行期透明——主机无法在运行时改变流的连接关系。

命名是理解配对的关键：`stream_connect=krnl_streamWrite_1.kout1:krnl_streamRead_1.kin1` 中，`krnl_streamWrite_1` 是 CU 实例名（由 `nk` 指令生成），`kout1` 是**内核 C++ 函数的参数名**。也就是说，ini 引用的端口名与源码签名硬绑定，改名任何一端都会断链。

#### 4.2.2 核心流程

从源码到连线的推导链：

```
nk=krnl_streamWrite:1                    → 生成 1 个实例，命名为 krnl_streamWrite_1
krnl_streamWrite.cpp 签名参数 kout1/kout2 → 实例暴露流端口 krnl_streamWrite_1.kout1 / .kout2
stream_connect=...kout1:...kin1          → 链接期把两端焊死为一组 AXIS 连线
主机 "krnl_streamWrite:{krnl_streamWrite_1}"  → 运行期按 CU 名实例化（仅用于启动，不涉及流）
```

约束清单（改配置时的核对表）：

1. `stream_connect` 左右两端的 CU 名必须存在于 `nk` 生成的实例集合中。
2. 端口名必须与内核函数参数名逐字一致。
3. 一条流的两端方向必须一写一读（`kout` 是 `hls::stream<pkt>&` 且只 `write`，`kin` 只 `read`）。
4. 流端口不需要也**不能** `setArg`——它们不是主机可见参数，连接关系在链接期已定死。

#### 4.2.3 源码精读

本工程的完整连接配置：

[ubench/streaming_bandwidth/datacenter/2ports_512bit/ubench.ini:L1-L12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/ubench.ini#L1-L12)

- 第 2-3、5-6 行：`slr` 把两个内核分别放进 SLR0 和 SLR1，`sp` 给两个空挂的 m_axi 端口各分一条 DDR 通道（`in0→DDR[0]`、`out0→DDR[1]`）。
- 第 8-9 行：两条 `stream_connect`，把写内核的两个输出流一一对应接到读内核的两个输入流。**这两条流都要跨 SLR 边界**（SLR0 → SLR1），测出的是含跨 die 互连开销的流带宽——这是刻意选择的测量口径，贴近真实大型设计中数据跨 SLR 流动的场景。
- 第 11-12 行：`nk` 声明每种内核只要 1 个实例，实例名后缀 `_1` 由此而来。

Makefile 侧确认两个内核如何进入链接：

[ubench/streaming_bandwidth/datacenter/2ports_512bit/Makefile:L73-L104](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/Makefile#L73-L104)

- 第 73 行 `LDCLFLAGS += --config ./ubench.ini`：连接配置只在链接这一步进入编译流。
- 第 81-82 行：`.xo` 对象有两个——`krnl_streamWrite.xo` 与 `krnl_streamRead.xo`。
- 第 96-101 行：两个内核源文件各有一条 `v++ -c -k <内核名>` 规则；第 102-104 行把它们链接成 `ubench.xclbin`。

主机侧的 CU 命名契约（与 u3-l3 同构，但这里是**成对**内核）：

[ubench/streaming_bandwidth/datacenter/2ports_512bit/src/host.cpp:L74-L92](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/src/host.cpp#L74-L92)

- 第 77-80 行按「内核名:{内核名_N}」拼出 `krnl_streamWrite:{krnl_streamWrite_1}` 与 `krnl_streamRead:{krnl_streamRead_1}`，字符串里的 `_1` 后缀与 ini 的 `nk` 严格耦合。
- 第 90-91 行创建绑定到具体 CU 的 Kernel 对象。注意创建顺序与流方向无关——流的连接早已在链接期焊死。

#### 4.2.4 代码实践

**实践目标**：推演「内核对数从 1 扩到 2」时 ini 与主机的全部联动改动（这是 auto_collect 中 `NUM_KERNEL` 维度的手工版，为 u5-l3 做铺垫）。

**操作步骤**：

1. 修改 `ubench.ini` 第 11-12 行：`nk=krnl_streamWrite:2`、`nk=krnl_streamRead:2`。
2. 推演新的 CU 名（`krnl_streamWrite_1/_2`、`krnl_streamRead_1/_2`），并写出需要新增的 `slr`、`sp`、`stream_connect` 行：第二对应放在哪些 SLR、空挂端口接到哪条 DDR、`kout1/kin1`、`kout2/kin2` 如何在 `_2` 实例间配对。
3. 检查 `host.cpp`：把第 15 行 `NUM_KERNEL_PAIRS` 改为 2，逐段确认缓冲区向量（第 120-123 行）、内核对象向量（第 40-41 行）、CU 名拼接循环（第 74 行 `i < NUM_KERNEL_PAIRS`）是否已被参数化覆盖。
4. 特别检查第 202 行带宽公式：它是否自动包含了内核对数？

**需要观察的现象**：主机代码几乎处处按 `NUM_KERNEL_PAIRS` 参数化，**唯一漏网的是带宽公式的硬编码系数**——第 202 行的 `×2` 只代表每对 2 个流端口，不含内核对数。

**预期结果**：改动清单 = ini 的 `nk`/`slr`/`sp`/`stream_connect` 扩展 + `NUM_KERNEL_PAIRS` 改 2 + 公式系数 `×2` 改 `×4`（2 对 × 2 端口）。漏改公式则带宽报一半（静默失真）。硬件上的带宽提升**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`krnl_streamWrite_1` 里的 `_1` 是谁定的？改 `nk` 后它会怎么变？

**答案**：由 `nk=krnl_streamWrite:1` 的实例计数决定，Vitis 按从 1 开始编号生成实例名。改成 `nk=krnl_streamWrite:2` 后出现 `_1`、`_2` 两个实例，ini 的 `stream_connect`/`slr`/`sp` 行和主机第 77-80 行的 CU 名拼接都要同步引用新名字。

**练习 2**：`sp=` 和 `stream_connect=` 的本质区别是什么？

**答案**：`sp` 把内核的 `m_axi` 端口连到片外存储通道（DDR/HBM bank），数据要进出片外存储；`stream_connect` 把两个内核的 AXIS 端口直接相连，数据停留在片上、不经过存储控制器也不经过主机。前者受存储带宽约束，后者受片上连线（含跨 SLR 互连）约束。

**练习 3**：为什么本工程故意把两个内核放在不同 SLR？换成同 SLR 会测出什么不同的东西？

**答案**：跨 SLR（SLR0→SLR1）的数据必须经过 die 间互连，带宽上限通常低于 SLR 内部连线，这是大规模设计里更常见的真实瓶颈；同 SLR 版本测的则是 SLR 内布线极限。两者是不同的测量口径——`slr` 行本身就是这个实验的自变量之一（综合实践会用到）。

### 4.3 流带宽计量：payload 扫描、×2 系数与计时窗口

#### 4.3.1 概念说明

流带宽的定义是「单位时间内流过片上通路的字节数」。测量思路与 u2-l3 同构：payload 倍增扫描刻画「连续数据量」因素，`NUM_ITERATIONS` 放大单次执行时长，主机 `chrono` 卡窗口计时，最后代入公式换算 GB/s。三个新要点需要特别注意：

1. **`×2` 系数**：本工程有 2 条并发流（`kout1+kout2`），公式末尾的 `×2` 就是端口数。它是硬编码字面量，不含在 `krnl_config.h` 或主机的任何常量里——改端口数时必须手动同步（与 u3-l4 写带宽版的系数核算问题同源）。
2. **payload 上限放大**：片外版扫描到 262144（1MB/次），流版本上限是 `262144*4`（4MB/次）。因为流通路没有访存延迟，吞吐接近 `II=1` 的连线极限，需要更长的计时窗口来摊薄主机计时里的启动开销（u2-l3 的误差分析在这里影响更大）；而放大 payload 只增大主机缓冲（最大 4MB/个），设备侧开销极低。
3. **乱序队列是正确性前提**：这是流微基准与片外版最大的行为差异，详见 4.3.2。

#### 4.3.2 核心流程

一次测量的时序：

```
payload ∈ {256, 512, ..., 1048576}         # 13 档, 单位: int 个数
    dataSize = payload (仿真时固定 256)
    分配 read_source/write_source 主机缓冲并填随机数
    创建设备缓冲 (in→DDR BANK0, out→DDR BANK1, 与 ini 的 sp 对齐)
    enqueueMigrateMemObjects(in 缓冲); q.finish()   ← 不计时
    ── 计时开始 ──
    write_krnl.setArg(0, in 缓冲); setArg(1, dataSize/16)
    enqueueTask(write_krnl)                          ┐ 乱序队列,
    read_krnl.setArg(0, out 缓冲); setArg(1, ...)    │ 两内核并发驻留
    enqueueTask(read_krnl)                           ┘
    q.finish()
    ── 计时结束 ──
    bw = payload×4×0.000010000 / t × 2
```

**为什么乱序队列是生死攸关的**：`enqueueTask` 是异步的，两个内核几乎同时被调度到 FPGA 上。写内核往流里灌数据，流的 FIFO 深度有限，灌满后 `write()` 阻塞等待读内核排水。`CL_QUEUE_OUT_OF_ORDER_EXEC_MODE_ENABLE` 保证读内核与写内核并发运行，排水持续进行。对比：u3-l4 的写带宽版用乱序队列只是为了让两个内核**同时测**（串行不会错，只是测法不对）；而流版若用串行队列，读内核必须等写内核完成才开始，写内核又在等排水——**死锁**。这是生产者-消费者模式对命令队列的硬性要求。

带宽公式逐项推导：

\[ \text{BW} = \underbrace{\text{payload} \times 4}_{\text{单端口单次字节数}} \times \underbrace{0.000010000}_{= \text{NUM\_ITERATIONS}/10^9} \div t \times \underbrace{2}_{\text{并发流端口数}} \;\text{GB/s} \]

数据量核对（单端口总量 = `payload×4×10000`）：

| payload | 单次 | 单端口总量（×10000） | 全通路总量（×2 端口） |
| --- | --- | --- | --- |
| 256 | 1KB | ~10.24MB | ~20.5MB |
| 262144 | 1MB | ~10.49GB | ~21GB |
| 1048576（代码上限） | 4MB | ~41.94GB | ~84GB |

README 声称数据量「~10MB 到 ~10GB」对应的是旧上限 262144；代码实际上限 `262144*4` 把最高档推到了 ~40GB——这是本讲发现的第 1 处文档滞后（README 第 44-47 行给出的代码片段还是 `payload <= 262144` 的旧版循环）。

#### 4.3.3 源码精读

payload 扫描与仿真截断：

[ubench/streaming_bandwidth/datacenter/2ports_512bit/src/host.cpp:L107-L112](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/src/host.cpp#L107-L112)

- 第 108 行：`payload <= 262144*4` 即上限 1048576，13 档倍增。
- 第 110-112 行：仿真模式固定 `dataSize = 256`，让 sw_emu 快速跑通功能（仿真带宽数值无物理意义，u1-l3 已确认）。

乱序命令队列的创建：

[ubench/streaming_bandwidth/datacenter/2ports_512bit/src/host.cpp:L56-L62](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/src/host.cpp#L56-L62)

第 60-61 行的 `CL_QUEUE_OUT_OF_ORDER_EXEC_MODE_ENABLE | CL_QUEUE_PROFILING_ENABLE`：前者如上所述是避免流死锁的关键；后者顺便开启了 `cl_event` 时间戳能力（可用来做 u2-l3 提出的内核级计时改进）。

缓冲区分支与迁移（不在计时窗口内）：

[ubench/streaming_bandwidth/datacenter/2ports_512bit/src/host.cpp:L127-L174](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/src/host.cpp#L127-L174)

- 第 127-136 行（仿真）把两个缓冲都放 `XCL_MEM_DDR_BANK1`；第 137-146 行（真机）`in→XCL_MEM_DDR_BANK0`、`out→XCL_MEM_DDR_BANK1`，与 ini 第 3、6 行的 `sp` 对齐（sw_emu 下 bank 绑定不生效，故仿真分支取值无关紧要）。
- 第 151-173 行创建 `CL_MEM_READ_ONLY`（in）与 `CL_MEM_WRITE_ONLY`（out）缓冲，并迁移输入数据；第 174 行 `q.finish()` 确保迁移完成后才开始计时。注意：由于两个 m_axi 端口在内核体内从未被使用，这次迁移实际上是「无用功」——好在我们刚确认过它在计时窗口之外，不影响结果。

计时窗口与成对启动：

[ubench/streaming_bandwidth/datacenter/2ports_512bit/src/host.cpp:L177-L203](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/src/host.cpp#L177-L203)

- 第 179 行计时开始；第 195 行 `q.finish()` 等两内核都结束；第 198-200 行计时结束。窗口内含 `setArg`、两次 `enqueueTask` 与 `finish` 返回延迟——最小档（单次 1KB、总量约 20MB）被启动开销污染最严重（承接 u2-l3）。
- 第 182 行 `dataSize = dataSize / WIDTH_FACTOR;`：把 int 个数换成 512bit 宽字个数后作为 `size` 实参（第 186、191 行）。
- 第 185-193 行：先设写内核参数并 `enqueueTask`，再设读内核参数并 `enqueueTask`。两个内核各只有 2 个标量/m_axi 参数需要 `setArg`——**流参数（arg 2、3）完全不出现在主机代码里**，再次印证流连接是链接期契约。
- 第 202 行：`bw_result = payload * 4 * 0.000010000 / kernel_time_in_sec * 2;` ——本讲的公式主角。
- 第 203 行打印 `payload*4/(1024.0*1024.0)` 标注为 "MB"：这是二进制 MiB 值且只算单次（不含 ×10000 与 ×2），而公式用的是十进制 GB——单位口径混用 + 打印口径偏小，与 u2-l3 指出的读带宽版同款瑕疵，读结果时须自行换算。

一个数值演算（示例演算，非实测）：假设某档 `payload = 262144`、实测 `t = 0.55s`，则

\[ \text{BW} = 262144 \times 4 \times 0.000010000 / 0.55 \times 2 \approx 38.1 \;\text{GB/s} \]

恰好逼近 300MHz × 2 端口 × 64B = 38.4GB/s 的理论峰值的 99%——流通路无访存延迟，利用率天然高于片外版本，这也是 4.3.1 中「需要放大 payload 拉长计时窗口」的底气。

顺带指出第 2、3 处文档滞后：流版 README 第 38 行说位宽定义在 `krnl_ubench.h`，实际文件名是 `krnl_config.h`；第 3 行的三参数描述与代码一致，但第 44 行的数据量口径（~10GB）对应旧循环上限。读 README 时一律以 `src/` 代码为准。

#### 4.3.4 代码实践

**实践目标**：把硬编码系数改造成常量驱动，消除静默失真隐患。

**操作步骤**：

1. 在 `host.cpp` 顶部（`NUM_KERNEL_PAIRS` 旁）新增 `#define NUM_PORT 2`。
2. 把第 202 行改为 `double bw_result = payload * 4 * 0.000010000 / kernel_time_in_sec * NUM_PORT;`
3. 进一步把魔数改写为 `NUM_ITERATIONS / 1000000000.0`（`NUM_ITERATIONS` 已在 `krnl_config.h` 第 11 行定义），使公式每个因子都有名字。
4. 对每档 payload 手算预期带宽（用 `38.4GB/s × 利用率` 做上限参照），制成表格。

**需要观察的现象**：改写前后数值应完全一致（纯重构）；对照表格可发现小 payload 档明显低于大 payload 档（启动开销占比大）。

**预期结果**：公式可读性提升，且改端口数时只需动一处常量。真机上的分档带宽曲线**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：扩展到 4 个流端口后忘了把 `×2` 改成 `×4`，输出会怎样？

**答案**：程序正常运行、计时正确，但报告带宽只有真实值的一半——静默失真。系数必须等于「真实并发通路数 = 内核对数 × 每对流端口数」，这正是 u3-l4 提出的核算规则的再次应用。

**练习 2**：本工程的计时窗口里混入了哪些非内核时间？怎么剥离？

**答案**：两次 `setArg`、两次 `enqueueTask` 的下发、`finish` 的返回延迟。队列已带 `CL_QUEUE_PROFILING_ENABLE`，可改用 `cl_event` 的 `CL_PROFILING_COMMAND_START/END` 时间戳得到纯内核执行时间（u2-l3 的双口径计时方案，本工程可直接套用）。

**练习 3**：为什么乱序队列在流微基准里是正确性问题，而在片外带宽微基准里只是测量方式问题？

**答案**：片外版每个内核自给自足（读自己的端口、写自己的端口），串行队列只会让各内核不能同时测，不会卡死；流版是生产者-消费者：写内核灌满流 FIFO 后阻塞等排水，若读内核因串行队列尚未启动，双方互相等待形成死锁。所以 `CL_QUEUE_OUT_OF_ORDER_EXEC_MODE_ENABLE` 在这里是程序能跑完的前提。

## 5. 综合实践

**任务**：把流微基准从 2 个流端口扩展为 4 个（`4ports_512bit`），走完「内核 → ini → 主机」的完整联动改造，并用乘积原理预测理论峰值。

**步骤 1 —— 拷贝样板目录**：

```bash
cp -r ubench/streaming_bandwidth/datacenter/2ports_512bit \
      ubench/streaming_bandwidth/datacenter/4ports_512bit
```

**步骤 2 —— 改发送内核 `krnl_streamWrite.cpp`**：

- 签名追加两个流参数（放在现有流参数之后即可——流参数不参与 `setArg`，追加不影响主机的参数编号契约；对比 u3-l2 中 m_axi 指针必须放在 `size` 之前的规则，思考为什么这里有区别）：

```c++
// 示例代码：4 端口签名
void krnl_streamWrite(int* in0, const int size,
                      hls::stream<pkt> &kout1, hls::stream<pkt> &kout2,
                      hls::stream<pkt> &kout3, hls::stream<pkt> &kout4) {
```

- 为 `kout3`、`kout4` 各补一条 `#pragma HLS INTERFACE axis port = ...`。
- 补两个 `volatile INTERFACE_WIDTH temp_data_3/4;`（每个并发循环独占一个）。
- 复制第三、第四段发送循环（每段只写自己的流和自己的临时变量）。

**步骤 3 —— 改接收内核 `krnl_streamRead.cpp`**：完全镜像（`kin3`、`kin4` + 两段接收循环）。

**步骤 4 —— 核对 `krnl_config.h`**：确认没有需要改的端口数量常量——流版本的端口数只由「内核签名 + ini」表达，`krnl_config.h` 里根本没有 `NUM_PORT`（对比：片外版的端口数落在主机 `NUM_PORT` 宏里）。这是流版与片外版框架的一个真实差异点。

**步骤 5 —— 改 `ubench.ini`**：追加两条连线，并核对全表：

```ini
stream_connect=krnl_streamWrite_1.kout3:krnl_streamRead_1.kin3
stream_connect=krnl_streamWrite_1.kout4:krnl_streamRead_1.kin4
```

核对清单：① `stream_connect` 行数 = 流端口数 = 4；② 每条流两端端口名与内核参数名逐字一致；③ CU 名后缀 `_1` 与 `nk=...:1` 一致；④ 两个内核仍在不同 SLR（沿用 SLR0/SLR1）。

**步骤 6 —— 改 `host.cpp`**：把公式第 202 行的 `×2` 改为 `×4`（或按 4.3.4 的重构引入 `NUM_PORT`）。其余主机代码无需改动：`setArg` 只碰参数 0/1，流参数对主机透明。

**步骤 7 —— 验证与推演**：

- 若本机装有 Vitis 2020.2：`make check TARGET=sw_emu DEVICE=<平台>` 走通功能链路（仿真数值无物理意义）。**待本地验证**。
- 推演理论峰值：4 端口 × 512bit × 300MHz = 76.8GB/s。但 4 条流都要跨 SLR0→SLR1 互连，跨 die 带宽可能成为新瓶颈——实测是否达到理论上限**待本地验证**。
- 进阶实验：把 `slr` 行改成两个内核同放 SLR0，对比跨 SLR 与同 SLR 两种口径的实测差异（4.2.5 练习 3 的落地）。

**预期结果**：理解「流端口数」这个参数的完整落点清单——内核签名、axis pragma、临时变量、并发循环数、ini 的 `stream_connect` 行数、主机公式系数，共六处联动；而 `krnl_config.h`、主机 `setArg` 序号、缓冲区、CU 命名全部不动。

## 6. 本讲小结

- `ap_axiu<512,0,0,0>` + `hls::stream<pkt>` + `#pragma HLS INTERFACE axis` 三件套把内核参数变成物理 AXIS 端口；side-band 全 0 让连线几乎全是数据线，无地址通道也就没有突发维度——流版参数空间天然少两维（突发、内存类型）。
- `stream_connect` 在链接期把「CU 名.参数名」对焊接死，`nk` 定实例名、内核签名定端口名、主机按同一契约拼 CU 名；流参数对主机完全透明，不需要也无法 `setArg`。
- 本工程实测的是**跨 SLR** 的纯流通路：两个内核分居 SLR0/SLR1，数据源头是常量 100，两个 m_axi 端口（`in0`/`out0`）及其 DDR 连线、缓冲迁移都是模板遗留的空挂物，迁移在计时窗口外、不影响结果。
- 带宽公式 `payload×4×0.000010000/t×2` 中 `×2` 是并发流端口数，`0.000010000` 仍是 `NUM_ITERATIONS/1e9`；payload 上限放大到 `262144*4`（单次 4MB、单端口总量约 40GB）是为了在连线级吞吐下拉开计时窗口。
- 乱序命令队列在流微基准中是**防死锁的正确性前提**（生产者阻塞等排水），不同于片外版的「并发测量」优化角色。
- 仓库文档滞后三处：README 数据量口径是旧循环上限（~10GB vs 代码 ~40GB）、位宽文件名写成 `krnl_ubench.h`（实际 `krnl_config.h`）、打印的 "MB" 实为单次 MiB 且与公式的十进制 GB 口径混用。

## 7. 下一步学习建议

- **下一讲 u4-l2（片外延迟微基准）**：转向另一条微基准线——随机访问与延迟估计，你会看到同一套五件套如何换成「下标数组 + 随机遍历」的访问模式，以及延迟如何从总时间反推。
- **u5-l3（流版 auto_collect）**：本讲手工做的端口数/内核对数扩展，在 `ubench/streaming_bandwidth/datacenter/auto_collect/` 里有脚本化版本（`NUM_CONCURRENT_PORT`、`NUM_KERNEL` 维度），可对照检验你的改造清单是否完整。
- **延伸阅读源码**：Xilinx 头文件 `ap_axi_sdata.h` 中 `ap_axiu` 的完整字段定义（`data/keep/strb/last/user/id/dest`），以及第 6 单元 KNN 的 `krnl_partialKnn → krnl_globalSort` 流连接——本讲的 `stream_connect` 配对机制将在那里以 14 路 流归并的规模复现。

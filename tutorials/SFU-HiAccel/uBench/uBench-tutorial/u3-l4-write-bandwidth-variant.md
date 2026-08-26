# 写带宽变体：同一框架下的对称实现

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行对照写内核（`out0[j] = 常量`）与读内核（`temp = in0[j]`），说清两者在接口、循环结构与并发组织上的全部差异。
2. 说出 `max_write_burst_length` 的设置位置、它与 AXI 写通路的对应关系，以及单次写突发最大字节数的计算方法。
3. 解释写带宽主机计分公式中 `NUM_KERNEL × NUM_PORT` 系数的真实含义——写版本的并发来自「2 个内核实例 × 1 个写端口」，与读版本的「1 个内核 × 2 个读端口」构成同一乘积的两种分解；据此推导「不乘 NUM_PORT（或乘错）时如何修正」。
4. 独立设计一个读写混合微基准，并推导其带宽公式。

本讲不引入新的工程骨架——五件套（`krnl_config.h` + 内核 + `host.cpp` + `ubench.ini` + `Makefile`）与 u1-l4、u2-l1、u2-l2 完全一致。本讲的看点是：**同一套框架如何用「镜像」的方式测写方向**，以及框架中哪些地方真正变了、哪些只是模板复制。

## 2. 前置知识

阅读本讲前，请确保已理解前序讲义的以下结论（本讲直接承接，不再重复推导）：

- **五件套骨架与契约头**（u1-l4）：`krnl_config.h` 中的 `DWIDTH`、`WIDTH_FACTOR`、`NUM_ITERATIONS` 同时被内核与主机包含；`size` 参数的单位是「宽字个数」，主机送参前要除以 `WIDTH_FACTOR`。
- **m_axi 接口与 bundle**（u2-l1、u3-l1）：`bundle` 名是端口分组开关，异名即独立并发端口；`PIPELINE II=1` 让每端口每拍发出一个宽字访问；连续地址自动合并成 AXI 突发。
- **ini 连接配置与主机 bank flag 的跨工具契约**（u3-l3）：`sp=` 行把内核端口连到内存通道，主机 `cl_mem_ext_ptr_t.flags` 必须与之逐端口对齐，`nk=` 决定实例个数与 CU 命名。
- **测量方法学**（u2-l3）：payload 从 256 倍增到 262144 个 int（1KB→1MB），带宽公式中的魔数 `0.000010000 = NUM_ITERATIONS / 1e9`；主机 chrono 计时窗口含内核启动开销。

再补充两个本讲要用到的 AXI 基础概念：

- **AXI4 的读与写是两套独立握手通道**。读走 AR（读地址）+ R（读数据返回）通道；写走 AW（写地址）+ W（写数据）+ B（写响应）通道。`max_read_burst_length` 约束 AR 突发，`max_write_burst_length` 约束 AW 突发。写通路每次突发还要等 B 通道的响应回包，这是读写行为差异的协议根源。
- **「死代码消除」对写不构成威胁**。读基准必须用 volatile 防止读被优化掉（读到的值没人用）；而写是对 volatile 指针的存储，本身就是不可消除的副作用。这一点决定了写内核的防优化压力更小。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp) | 写带宽 HLS 内核：单写端口、双层循环重复写常量 |
| [ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp) | 写带宽主机程序：`NUM_KERNEL=2 / NUM_PORT=1`、写缓冲、计时与公式 |
| [ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/ubench.ini](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/ubench.ini) | 链接期连接配置：2 个 CU 实例、各自 1 个写端口连 DDR[0] |
| [ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_config.h](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_config.h) | 契约头，与 read 版逐字节相同 |
| [ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp) | 读内核，本讲的对照基准 |
| [ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp) | 读主机程序，对照 `NUM_KERNEL/NUM_PORT` 语义 |
| [ubench/offchip_bandwidth/datacenter/write/HBM/2ports_512bit/src/krnl_ubench.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/HBM/2ports_512bit/src/krnl_ubench.cpp) | HBM 版写内核，验证「内存类型差异不在内核源码」在写方向同样成立 |

## 4. 核心概念与源码讲解

### 4.1 写内核对照：同一框架下的对称实现

#### 4.1.1 概念说明

写带宽微基准要回答的问题与读版对称：**当内核持续向片外内存写连续数据时，内存系统能吞下多少 GB/s？** 它与读版共享同一目标目录族（`ubench/offchip_bandwidth/datacenter/write/` 下同样分 `read`/`write` 之外的维度：DDR/HBM × 端口数 × 位宽），共享同一个 `krnl_config.h`、同一个 Makefile、同一套主机骨架。

但「写」与「读」在硬件行为上并不对称，所以内核组织方式也不同。最关键的一点是**并发的组织方式**：

- 读版把并发放进**一个内核**：两个异名 bundle（`gmem0`/`gmem1`）配两条读循环，靠 `DATAFLOW` 并行。
- 写版把并发放到**两个内核实例**：每个内核只有 1 个写端口，靠 `nk=2` 链接出两个 CU，由主机分别 `enqueueTask`，在乱序命令队列上并发执行。

两种方式最终都得到「2 条并发访问通道」，这正是目录名 `2ports_512bit` 的真实含义——**目录名只约束「总端口数 = nk × 每核端口数」这个乘积**（u1-l2 已指出该约定，本讲是它在写方向的具体实例）。

#### 4.1.2 核心流程

写内核的执行流程可以概括为：

```text
krnl_ubench(out0, size):
    temp_data_0 = 100                        # 要写的常量（volatile）
    重复 NUM_ITERATIONS(=10000) 次:
        对 j = 0 .. size-1 (PIPELINE II=1):
            out0[j] = temp_data_0            # 连续写 size 个 512-bit 宽字
    return
```

一次测量的全流程：

1. 主机按 payload 档位分配 `NUM_KERNEL × NUM_PORT` 个主机缓冲并填充随机数。
2. 创建 `CL_MEM_WRITE_ONLY` 的设备缓冲（绑定到 bank），**不做 host→device 迁移**——没有输入数据要送。
3. 计时开始：对 2 个 CU 分别 `setArg`（out 缓冲、宽字个数）并 `enqueueTask`，`q.finish()` 等全部完成。
4. 用 `payload × 4 × 10000 × (NUM_KERNEL × NUM_PORT) / t / 1e9` 换算写带宽。

#### 4.1.3 源码精读

先看写内核本体。[krnl_ubench.cpp:4-9](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp#L4-L9) 是内核签名与接口 pragma：单个 `out0` 指针参数，`m_axi` 用的是 **`max_write_burst_length=16`**（读版对应位置是 `max_read_burst_length`），bundle 名为 `gmem0`，`s_axilite` 组成 control 寄存器组：

```cpp
void krnl_ubench(volatile INTERFACE_WIDTH* out0, const int size) {
#pragma HLS INTERFACE m_axi port=out0 offset=slave bundle=gmem0 max_write_burst_length=16
#pragma HLS INTERFACE s_axilite port=out0 bundle=control
```

[krnl_ubench.cpp:11-18](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp#L11-L18) 是全部计算逻辑：一个 volatile 常量、双层循环、每拍写一个宽字。注意**没有 `DATAFLOW`**——只有一个循环体，没有可并行的兄弟循环：

```cpp
volatile INTERFACE_WIDTH temp_data_0 = 100;

for (int i = 0; i < NUM_ITERATIONS; i++) {
    for (int j = 0; j < size; j++) {
        #pragma HLS PIPELINE II=1
            out0[j] = temp_data_0;
    }
}
```

与读版逐条对照（读版见 [read 版 krnl_ubench.cpp:4-31](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L4-L31)）：

| 维度 | 读版（u3-l1） | 写版（本讲） |
| --- | --- | --- |
| 指针参数 | `in0`、`in1` 两个，均在 `size` 之前 | 仅 `out0` 一个，同样在 `size` 之前 |
| 循环体语句 | `temp_data_0 = in0[j];`（读，赋给 volatile 临时量） | `out0[j] = temp_data_0;`（写，volatile 常量做源） |
| `DATAFLOW` | 有——两条无依赖读循环并行驱动双端口 | 无——单循环无需数据流并行 |
| 防优化双保险 | volatile 指针 + volatile 临时变量 | volatile 指针即可（写是副作用，天然不可消除）；volatile 常量属模板对称的冗余 |
| 突发 pragma | `max_read_burst_length=16` | `max_write_burst_length=16` |
| 并发来源 | 单内核 × 2 端口 | 2 内核实例 × 1 端口（见 4.1.3 末尾的 ini） |

写方向的 volatile 还有一个读版没有的细节：`temp_data_0` 初始化为常量 `100`，没有任何计算——写带宽测试刻意让**数据来源为零开销**，保证测到的全部时间都属于内存写通路本身。

再看 [ubench.ini](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/ubench.ini#L1-L8)。这里能看到并发组织方式的落点：`nk=krnl_ubench:2` 生成两个 CU 实例，各自的 `out0` 都连到 `DDR[0]`，都放在 `SLR0`：

```ini
slr=krnl_ubench_1:SLR0
sp=krnl_ubench_1.out0:DDR[0]

slr=krnl_ubench_2:SLR0
sp=krnl_ubench_2.out0:DDR[0]

nk=krnl_ubench:2
```

对照读版的 [ubench.ini](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L1-L6)：读版是 `nk=krnl_ubench:1` 加两行 `sp=`（`in0`、`in1` 都连 `DDR[1]`）。两版目录名都叫 `2ports_512bit`，乘积都是 2，分解方式却相反——这是本讲最值得记住的一个事实。

最后验证「内存类型差异不在内核源码」在写方向同样成立：[write/HBM 版 krnl_ubench.cpp:4-16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/HBM/2ports_512bit/src/krnl_ubench.cpp#L4-L16) 与 DDR 版逐行相同（仅临时变量名为 `temp_data` 而非 `temp_data_0`），差异全部落在 [write/HBM 版 ubench.ini:3](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/HBM/2ports_512bit/ubench.ini#L3) 的 `HBM[0]` 与主机 flag 上——与 u3-l1、u3-l3 的结论完全一致。

#### 4.1.4 代码实践

**实践目标**：用 diff 建立读写两版内核的完整差异清单，并亲手把写内核改成「单核双写端口」形态，体会两种并发组织方式的互换。

**操作步骤**：

1. 在仓库根目录执行只读对比（不要修改源码，把改动留到复制的目录里做）：
   ```bash
   diff ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp \
        ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp
   ```
2. 把 `write/DDR/2ports_512bit` 整个目录复制到仓库外的实验目录（如 `~/mixed/2k_2p_write`），在副本中把内核改为单核双写端口（示例代码）：
   ```cpp
   // 示例代码：单核双写端口形态（仿 read 版的双循环结构）
   void krnl_ubench(volatile INTERFACE_WIDTH* out0, volatile INTERFACE_WIDTH* out1,
                    const int size) {
   #pragma HLS INTERFACE m_axi port=out0 offset=slave bundle=gmem0 max_write_burst_length=16
   #pragma HLS INTERFACE m_axi port=out1 offset=slave bundle=gmem1 max_write_burst_length=16
       volatile INTERFACE_WIDTH c = 100;
   #pragma HLS DATAFLOW
       for (int i = 0; i < NUM_ITERATIONS; i++)
           for (int j = 0; j < size; j++) {
   #pragma HLS PIPELINE II=1
               out0[j] = c;
           }
       for (int i = 0; i < NUM_ITERATIONS; i++)
           for (int j = 0; j < size; j++) {
   #pragma HLS PIPELINE II=1
               out1[j] = c;
           }
   }
   ```
3. 同步改副本的 `host.cpp`（`NUM_KERNEL 1`、`NUM_PORT 2`）与 `ubench.ini`（`nk=krnl_ubench:1`，两行 `sp=` 分别连 `out0`、`out1`）。

**需要观察的现象**：diff 输出中读版多出的 `DATAFLOW`、第二个循环与两条 `m_axi` pragma；改形态后，并发从「两 CU」变为「一 CU 内两端口」。

**预期结果**：总并发写通道数保持 2（`nk × 每核端口数 = 1 × 2`），理论峰值不变（300 MHz × 2 × 512 bit / 8 = 38.4 GB/s）。RTL 层面两种形态是否等价（DATAFLOW 双写循环的时序收敛、CU 间共享 AXI 互通仲裁的差异）**待本地验证**——需要 Vitis 综合后对比 `v++` 报告中的端口数与时序。

#### 4.1.5 小练习与答案

**练习 1**：写内核为什么不需要 `DATAFLOW`，也不太需要担心死代码消除？

**答案**：`DATAFLOW` 的作用是让多个无依赖的循环体并行，写内核只有一条循环，没有可并行的对象。死代码消除方面，`out0` 是 `volatile INTERFACE_WIDTH*`，对它的存储是 observable side effect，编译器不能删除；读版则不同——读到的值如果没人消费，整个读循环可能被删除，所以读版必须再加 volatile 临时变量来「消费」数据。

**练习 2**：目录名都叫 `2ports_512bit`，read 版与 write 版的 `nk` 与每核端口数各是多少？分别依据哪个文件确认？

**答案**：read 版 `nk=1`、每核 2 端口；write 版 `nk=2`、每核 1 端口。依据是各自的 `ubench.ini`（`nk=` 行数与 `sp=` 行数）与 `host.cpp` 顶部的 `NUM_KERNEL`/`NUM_PORT` 宏：read 版为 `1/2`（[read host.cpp:15-16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L15-L16)），write 版为 `2/1`（[write host.cpp:15-16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L15-L16)）。两版乘积都是 2。

**练习 3**：写内核把同一个缓冲反复写了 `NUM_ITERATIONS=10000` 遍，会不会因为「数据已在缓存里」而虚高带宽？

**答案**：不会。FPGA 的 `m_axi` 通路没有 CPU 意义上的 cache，每次写都会真正发往片外内存控制器；这与 u3-l1 中读版「重复读不污染测量」的结论同理。

### 4.2 写突发配置：max_write_burst_length

#### 4.2.1 概念说明

AXI4 的写事务同样以突发（burst）为单位：主机先在 AW 通道发一个写地址（含突发长度），再在 W 通道连续发若干拍数据，最后从 B 通道收到写响应。`max_write_burst_length` 告诉 Vitis 链接器：这个端口允许合并出的**单次写突发最长多少拍（beat）**。

它对写带宽的作用机理与读方向一致但更敏感：

- **每次突发只有一次地址开销**。突发长度越短，同样数据量要发越多次 AW 握手与 B 响应，有效带宽越低。
- **写还有响应回包开销**。每个写突发结束后 slave 要经 B 通道回一个响应，这是读方向没有的额外往返；突发太短时这部分占比被放大。
- **单次突发最大字节数** = `burst × DWIDTH / 8`。本工程为 \( 16 \times 512/8 = 1024 \) 字节，即 1KB——连续写超过 1KB 的区间会被自动拆成多个 1KB 突发。

AXI4 协议上限为 256 拍，因此 512-bit 端口的单突发上限是 \( 256 \times 64 = 16384 \) 字节。仓库把手写示例统一放在 16（最小档），把 burst 扫描留给 `auto_collect` 生成的工程去做（u5-l1 的 `MAX_BURST_LENGTH` 参数）。

#### 4.2.2 核心流程

写突发的生成与拆分过程：

```text
写循环 (II=1, 每拍产出一个 512-bit 字, 地址连续递增)
    │
    ▼
端口级突发合并器（受 max_write_burst_length=16 约束）
    │  连续地址每凑满 16 拍 → 发出 1 个 AW + 16 个 W
    │  地址不连续或到达 size 末尾 → 提前结束当前突发
    ▼
每 16 拍等待 1 个 B 响应 → 继续下一突发
```

推论：payload 档位小时（例如 1KB = 16 个宽字），整个档位恰好凑成一个突发，突发长度参数根本没有发挥余地；只有当连续访问数据量远大于单突发字节数时，`max_write_burst_length` 的差异才会体现在带宽上。这正是五因素中「突发长度」与「连续访问数据量」两个维度耦合的原因。

#### 4.2.3 源码精读

设置位置只有一处：内核源码中该端口的 `m_axi` pragma。[krnl_ubench.cpp:5](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp#L5)：

```cpp
#pragma HLS INTERFACE m_axi port=out0 offset=slave bundle=gmem0 max_write_burst_length=16
```

这一行同时决定了四件事：`out0` 是 AXI master 内存端口（`m_axi`）、基地址由主机运行时写入（`offset=slave`）、它独占名为 `gmem0` 的端口（`bundle`）、单次写突发最长 16 拍（`max_write_burst_length`）。

端口位宽来自契约头。[krnl_config.h:4-6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_config.h#L4-L6) 与读版逐字节相同：`DWIDTH=512`、`INTERFACE_WIDTH = ap_uint<512>`、`WIDTH_FACTOR=16`。所以改位宽同样只动这一行，突发字节数随之变为 `burst × DWIDTH/8`：

```cpp
const int DWIDTH = 512;
#define INTERFACE_WIDTH ap_uint<DWIDTH>
const int WIDTH_FACTOR = DWIDTH/32;
```

与读方向对比的落点差异：读版每个端口 pragma 写 `max_read_burst_length`（[read 版 krnl_ubench.cpp:5-6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L5-L6)）；读写混合内核则要**在同一个内核里同时声明两类突发参数**（见第 5 节综合实践）。还要注意 `max_write_burst_length` 是链接期参数、对主机完全透明——主机代码中没有任何与之对应的宏或调用，这一点继承自 u3-l2 的结论。

#### 4.2.4 代码实践

**实践目标**：量化「突发长度 × 位宽」对单突发数据量的影响，并预演一次 burst 参数修改。

**操作步骤**：

1. 用一张表推演以下组合的单次写突发最大字节数（公式 `burst × DWIDTH / 8`）：

   | burst \ DWIDTH | 256 | 512 | 1024 |
   | --- | --- | --- | --- |
   | 16 | 512 B | ？ | ？ |
   | 64 | ？ | ？ | ？ |
   | 256 | ？ | ？ | ？ |

2. 把上一节复制出的实验目录中 `max_write_burst_length` 从 16 改为 256（所有写端口的 pragma 都要同步改；本工程只有一个）。
3. 若本机装有 Vitis 2020.2，在实验目录里跑一次 `make build TARGET=sw_emu DEVICE=<平台>` 确认能通过编译；有真机则再对比两档 burst 在 payload=262144（1MB）档的带宽数值。

**需要观察的现象**：推演表中 512-bit × 256 拍 = 16KB，恰好等于 AXI4 协议上限；改 burst 后主机源码**一个字符都不用动**。

**预期结果**：小 payload 档（1KB、2KB）两档 burst 的带宽几乎无差别（数据量不足一个长突发）；大 payload 档 256 拍应不低于 16 拍。真机上的具体差值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么说写方向对突发长度比读方向更敏感？

**答案**：每个写突发除地址（AW）与数据（W）通道外，还要在 B 通道收一个写响应才能完成；突发越短，同等数据量下 B 响应与 AW 握手的次数越多，开销占比越高。读方向只有 AR 一次地址开销，没有逐突发的响应回包。

**练习 2**：`DWIDTH` 改为 256 后，`max_write_burst_length=16` 对应的单突发字节数是多少？想要保持 1KB 的单突发大小该怎么改？

**答案**：\( 16 \times 256/8 = 512 \) 字节。要保持 1KB 需把突发长度翻倍到 32 拍。可见「单突发字节数」由 burst 与位宽共同决定，扫参数时二者要一起记录。

**练习 3**：`max_write_burst_length` 修改后，`host.cpp`、`ubench.ini`、`krnl_config.h` 各需要改什么？

**答案**：都不需要改。突发长度只出现在内核 `m_axi` pragma 里，是综合/链接期参数，对主机与 ini 完全透明（改端口**数量**才需要动 ini 的 `sp=` 行和主机的 `NUM_PORT`，见 4.1 与 u3-l2）。

### 4.3 主机公式修正：NUM_KERNEL/NUM_PORT 语义与系数核算

#### 4.3.1 概念说明

先给结论：**写版主机的带宽公式代码与读版逐字符相同**，真正的差异在两个宏的语义——同一个 `NUM_KERNEL × NUM_PORT` 乘积，读版是 `1 × 2`（1 个 CU、2 个端口），写版是 `2 × 1`（2 个 CU、各 1 个端口）。公式之所以成立，是因为它只关心「**共有多少条并发的内存访问通道**」，不关心这些通道是长在一个内核里还是分在多个 CU 上：

\[ \text{带宽} = \frac{\text{payload} \times 4\,\text{B} \times \text{NUM\_ITERATIONS} \times (\text{NUM\_KERNEL} \times \text{NUM\_PORT})}{t \times 10^9} \ \text{GB/s} \]

其中 `payload × 4` 是每条通道一次内层循环写出的字节数，`NUM_ITERATIONS = 10000` 把它放大一万倍，除以 \(10^9\) 折成十进制 GB——这正是魔数 `0.000010000` 的来历（u2-l3 已推导）。

由此得到本模块的核心修正规则：**公式里的并发系数必须等于真实并发通道数 `nk × 每核端口数`**。任何一端改动而另一端没跟上，带宽就会按比例失真：

- 给写内核加端口却忘改 `NUM_PORT`（仍为 1）：实际写了 N 路却按 1 路计分，带宽**低报** N 倍。
- 改 `nk` 却忘改 `ubench.ini` 的 `nk=` 行：主机按不存在的 CU 名创建 Kernel 对象，运行期报错（u3-l3 讲过的跨工具契约，无编译期检查）。
- 反过来，若公式「不乘 NUM_PORT」（例如有人把双端口内核当单端口计分），修正方法就是把系数补回真实乘积，或等价地在公式里显式乘 `nk × ports_per_kernel`。

#### 4.3.2 核心流程

写版主机一次测量的控制流（与读版骨架相同的部分标注「同读版」）：

```text
main:
  解析 xclbin 路径, 发现设备, 建 Context/乱序+profiling 队列   （同读版, u2-l2）
  按「krnl_ubench:{krnl_ubench_i}」创建 NUM_KERNEL=2 个 Kernel 对象
  for payload in 256 .. 262144 (×2):
      分配 NUM_KERNEL*NUM_PORT = 2 个主机缓冲 write_source[i]   （写版：数组-of-vector）
      设备缓冲: CL_MEM_WRITE_ONLY | EXT_PTR | USE_HOST_PTR, flag 按 is_emulation 选 bank
      （写版没有 enqueueMigrateMemObjects —— 无输入数据要送）
      计时开始
      for i in 0..NUM_KERNEL-1:
          setArg(0, out 缓冲[i]); setArg(1, dataSize/WIDTH_FACTOR)
          enqueueTask(cmpt_krnl[i])            ← 两个 CU 先后入队, 乱序队列并发执行
      q.finish()                               ← 等两个 CU 全部完成
      计时结束
      带宽 = payload*4*0.00001 / t * NUM_KERNEL * NUM_PORT
```

两个 CU 的并发由两个条件共同保证：命令队列带 `CL_QUEUE_OUT_OF_ORDER_EXEC_MODE_ENABLE`（u2-l2 讲过），且 `nk=2` 让两个 CU 物理上是独立的两份硬件。

#### 4.3.3 源码精读

并发语义的声明点在 [host.cpp:15-16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L15-L16)：`NUM_KERNEL 2`、`NUM_PORT 1`——与读版（`1/2`，[read host.cpp:15-16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L15-L16)）恰好互为转置：

```cpp
#define NUM_KERNEL 2
#define NUM_PORT 1
```

主机缓冲的分配在 [host.cpp:107-111](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L107-L111)：按 `NUM_KERNEL*NUM_PORT` 个缓冲、每个装 `dataSize` 个随机 int。写版用「数组 of vector」而读版是单个 vector，因为写版有 2 个独立目的地（每 CU 一个），读版的两个端口共用同一份输入数据：

```cpp
std::vector<int, aligned_allocator<int>> write_source[NUM_KERNEL*NUM_PORT];
for (int i = 0; i < NUM_KERNEL*NUM_PORT; i++) {
    write_source[i].resize(dataSize);
    std::generate(write_source[i].begin(), write_source[i].end(), std::rand);
}
```

bank 绑定在 [host.cpp:118-131](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L118-L131)。注意**写版的两个分支不再相同**（读版两分支都是 `BANK1`，u2-l2 判定为模板空骨架）：仿真分支写 `XCL_MEM_DDR_BANK1`（[host.cpp:122](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L122)），真机分支写 `XCL_MEM_DDR_BANK0`（[host.cpp:129](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L129)），后者与 `ubench.ini` 的 `DDR[0]` 对齐——真机分支才是有效契约，仿真分支不校验 bank：

```cpp
else{
    for (int i = 0; i < NUM_KERNEL*NUM_PORT; i++) {
        source_out_ext[i].obj = write_source[i].data();
        source_out_ext[i].param = 0;
        source_out_ext[i].flags = XCL_MEM_DDR_BANK0;   // 与 ini 的 sp=...DDR[0] 对齐
    }
}
```

设备缓冲创建在 [host.cpp:136-145](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L136-L145)。关键差异是 `CL_MEM_WRITE_ONLY`（读版为 `CL_MEM_READ_ONLY`，[read host.cpp:136](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L136)），且**创建后紧接 `q.finish()` 而没有任何 `enqueueMigrateMemObjects`**——读版在这里要把数据搬上卡（[read host.cpp:142-145](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L142-L145)），写版没有输入可搬：

```cpp
source_out_buffer[i] =
    cl::Buffer(context,
                CL_MEM_WRITE_ONLY | CL_MEM_EXT_PTR_XILINX |
                    CL_MEM_USE_HOST_PTR,
                sizeof(int) * dataSize,
                &source_out_ext[i],
                &err);
```

计时与计分在 [host.cpp:149-172](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L149-L172)。`dataSize` 先除以 `WIDTH_FACTOR` 换算成宽字个数（[host.cpp:154](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L154)）；`i×j` 双层循环对每个 CU 先设缓冲参数（`j` 恰好从 0 到 `NUM_PORT-1`，此处即 arg 0）再设 `size`（arg 1），随后 `enqueueTask`（[host.cpp:156-163](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L156-L163)）——指针参数必须在 `size` 之前的 setArg 编号契约再次得到体现。公式行（[host.cpp:171](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L171)）与读版逐字符相同：

```cpp
double bw_result = payload * 4 * 0.000010000 / kernel_time_in_sec * NUM_KERNEL * NUM_PORT;
```

写版系数核算：`NUM_KERNEL × NUM_PORT = 2 × 1 = 2`，与读版的 `1 × 2 = 2` 相同，所以两版公式给出的都是「2 条并发通道的总带宽」——目录名里的 `2ports` 在公式层面同样兑现为乘积 2。

最后是继承自读版的已知瑕疵（u2-l3 已定性，此处只是复现确认）：[host.cpp:172](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L172) 的 `Payload Size` 打印用的是循环结束后 `i` 的值（此处 `i == NUM_KERNEL == 2`），输出约 `8/1048576 ≈ 7.6e-6 MB`，与真实 payload 无关，读数时应以 payload 档位序列（1KB→1MB）自行对照；且 `GB/s` 为十进制口径。另外注意：写版从头到尾没有把 written 数据迁回主机校验，`CL_MEM_WRITE_ONLY` 只是不阻止内核写，并不做任何数据正确性检查。

#### 4.3.4 代码实践

**实践目标**：通过「改形态不改总量」的假想实验，掌握公式系数与真实并发通道数的对齐方法。

**操作步骤**：

1. 核算现状：写出 4.1.4 中两种形态（原版 `nk=2×1port`、改造版 `nk=1×2ports`）各自的 `NUM_KERNEL`、`NUM_PORT`、乘积、公式代码改动量。
2. 假想三种失误场景，逐个推算带宽报表会被怎样扭曲：
   - a) 内核改成双端口，`host.cpp` 忘了把 `NUM_PORT` 从 1 改成 2；
   - b) `ubench.ini` 的 `nk=` 忘了从 2 改成 1（配合改造版内核）；
   - c) 公式被误删 `* NUM_PORT`（原版工程）。
3. 为每处修正写出对应的最小 patch 行（只写文字说明改哪一行、改成什么，不动源码）。

**需要观察的现象**：场景 a 的带宽**低报一半**（实际 2 路并发只按 1 路计分）；场景 b 在创建 `krnl_ubench_2` Kernel 对象时运行期失败（xclbin 里只有 1 个 CU）；场景 c 同样低报一半——因为写版 `NUM_PORT=1`，删掉它数值上恰好不变，这是个容易漏掉的陷阱。

**预期结果**：三种失误中只有场景 b 会报错，a、c 都会**静默给出错误的数字**。这正是 u3-l3 强调「跨工具契约无编译期检查」在主机公式上的延伸：公式系数也是一种需要人工对齐的契约。本实践为纯推演，无需硬件，结论可从公式直接得出。

#### 4.3.5 小练习与答案

**练习 1**：写版带宽公式与读版逐字符相同，为什么对两版都成立？

**答案**：公式只依赖「并发访问通道总数 × 每通道流量」。读版 2 条通道来自 `NUM_KERNEL=1 × NUM_PORT=2`，写版来自 `NUM_KERNEL=2 × NUM_PORT=1`，乘积同为 2；每条通道一次内层循环的流量都是 `payload × 4` 字节、都重复 `NUM_ITERATIONS` 次，时间口径都是同一个 chrono 窗口。

**练习 2**：写版主机为什么没有 `enqueueMigrateMemObjects`？这是否影响测量有效性？

**答案**：写基准没有输入数据，`CL_MEM_WRITE_ONLY` 缓冲无需 host→device 迁移，省掉迁移正好把这部分时间排除在计时窗口外。有效性不受影响；反而要注意的是写完的数据从未迁回校验——本基准只关心吞吐，不校验内容，这是设计取舍而非缺陷。

**练习 3**：读版主机的 bank 两个分支完全相同（都是 `BANK1`），写版却一个 `BANK1` 一个 `BANK0`。哪个分支是必须与 ini 对齐的？为什么？

**答案**：真机（`else`）分支必须对齐：写版真机分支 `XCL_MEM_DDR_BANK0` 与 `ubench.ini` 的 `sp=krnl_ubench_*.out0:DDR[0]` 一一对应，错了会在运行期把缓冲放进与端口不同的通道。仿真分支（`BANK1`）在 sw_emu 下不做严格的 bank 校验，属模板残留。注意写版 ini 连的是 `DDR[0]`、读版连的是 `DDR[1]`，两版 bank 编号本就不同，不能互相照抄。

## 5. 综合实践：设计一个读写混合微基准

**任务**：以 `write/DDR/2ports_512bit` 为底板，造一个 `mixed_rw` 微基准——每个内核同时做一次连续读和一次连续写，读出与写入的数据量相同。完成后推导混合带宽公式。这是把本讲三个模块（内核对照、写突发、公式系数）串起来的收官任务，也是走向 u7-l2「自建微基准」的热身。

**操作步骤**（在仓库外的副本目录中操作，保持源码仓库只读）：

1. **复制底板**：`cp -r ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit ~/mixed_rw`，并把目录里的目标名（可执行文件名）按需改为 `mixed_rw`。
2. **改内核**（示例代码）：新增一个读端口 `in0`，读循环与写循环放在 `DATAFLOW` 下并行；两个端口用异名 bundle，各自声明对应方向的突发参数：
   ```cpp
   // 示例代码：mixed_rw 内核
   void krnl_ubench(volatile INTERFACE_WIDTH* in0, volatile INTERFACE_WIDTH* out0,
                    const int size) {
   #pragma HLS INTERFACE m_axi port=in0  offset=slave bundle=gmem0 max_read_burst_length=16
   #pragma HLS INTERFACE m_axi port=out0 offset=slave bundle=gmem1 max_write_burst_length=16
   #pragma HLS INTERFACE s_axilite port=in0 bundle=control
   #pragma HLS INTERFACE s_axilite port=out0 bundle=control
   #pragma HLS INTERFACE s_axilite port=size bundle=control
   #pragma HLS INTERFACE s_axilite port=return bundle=control
       volatile INTERFACE_WIDTH temp = 0;
       volatile INTERFACE_WIDTH c = 100;
   #pragma HLS DATAFLOW
       for (int i = 0; i < NUM_ITERATIONS; i++)
           for (int j = 0; j < size; j++) {
   #pragma HLS PIPELINE II=1
               temp = in0[j];
           }
       for (int i = 0; i < NUM_ITERATIONS; i++)
           for (int j = 0; j < size; j++) {
   #pragma HLS PIPELINE II=1
               out0[j] = c;
           }
   }
   ```
   注意三个契约点：两个指针参数都必须在 `size` 之前（setArg 编号）；`temp` 必须是 volatile（否则读循环被死代码消除）；若把 `in0` 与 `out0` 写成同一个 bundle 名，两者会合并到**一个** AXI4 端口上分时读写——那也是一个有意义的变体（测共享端口的读写争用），但通道数计为 1 而不是 2。
3. **改 host.cpp**：为输入/输出各建一组缓冲与 ext_ptr（`source_in_*` 用 `CL_MEM_READ_ONLY` 并 `enqueueMigrateMemObjects`，`source_out_*` 保持 `CL_MEM_WRITE_ONLY`、不迁移）；保持 `NUM_KERNEL=2`、`NUM_PORT=1`，但缓冲组数从 1 组变 2 组；setArg 顺序为 `in 缓冲 → out 缓冲 → size`。
4. **改 ubench.ini**：给每个 CU 补两行 `sp=`，例如 `sp=krnl_ubench_1.in0:DDR[0]` 与 `sp=krnl_ubench_1.out0:DDR[0]`（或把 `out` 拆到 `DDR[1]` 做跨通道变体），`nk=krnl_ubench:2` 不变。
5. **验证**：装有 Vitis 2020.2 时执行 `make check TARGET=sw_emu DEVICE=<平台>` 走通功能链路；无 Vitis 则手写一份「源码 → xo → xclbin → 可执行」的命令流程说明（参照 u1-l3），标注 sw_emu 数值无物理意义。
6. **推导公式**（见下）。

**公式推导**：每个 CU 一次测量搬移的总字节数变为「读 + 写」双向：

\[ \text{总流量} = \underbrace{\text{payload} \times 4 \times \text{NUM\_ITERATIONS}}_{\text{读}} + \underbrace{\text{payload} \times 4 \times \text{NUM\_ITERATIONS}}_{\text{写}} = \text{payload} \times 8 \times 10000 \ \text{字节} \]

故**混合总带宽**（GB/s，十进制）：

\[ \text{BW}_{\text{mixed}} = \frac{\text{payload} \times 8 \times 0.000010000 \times \text{NUM\_KERNEL} \times \text{NUM\_PORT}}{t} \]

若想分别报告读、写两个方向的带宽，各取一半即可（因为两方向流量相等）。对照原写版公式 `payload * 4 * 0.000010000 / t * NUM_KERNEL * NUM_PORT`，唯一变化是系数 `4 → 8`——这一步正是 4.3 模块「系数必须等于真实流量」规则的直接应用：新增一条读通路就要把它的流量计入分子，而不是套用旧公式。

**预期结果**：sw_emu 下程序能跑完 11 档 payload 并打印 Execution time 与 Bandwidth（数值无物理意义）；真机上理想情况是混合带宽接近读、写单向带宽之和，若明显低于之和，说明读写在内存控制器或共享通道上互相争用——这本身就是值得记录的测量结论。真机数值**待本地验证**。

## 6. 本讲小结

- 写内核是读内核的镜像：`out0[j] = 常量` 取代 `temp = in0[j]`，单循环取代双循环，`max_write_burst_length` 取代 `max_read_burst_length`；`DATAFLOW` 与 volatile 临时变量的防优化压力都比读方向小（写本身是不可消除的副作用）。
- 并发组织方式相反：读版「1 内核 × 2 端口（DATAFLOW）」，写版「2 内核实例 × 1 端口（nk=2 + 乱序队列）」；目录名 `2ports` 只约束乘积 `nk × 每核端口数 = 2`。
- 主机侧三处实质差异：缓冲为 `CL_MEM_WRITE_ONLY`、创建后无 `enqueueMigrateMemObjects`（无输入可搬、写完也不迁回校验）、bank 分支不再是空骨架（真机 `BANK0` 与 ini 的 `DDR[0]` 对齐）。
- 带宽公式逐字符继承自读版，成立条件是系数 `NUM_KERNEL × NUM_PORT` 等于真实并发通道数；改端口数、改 nk、改流量方向（如混合读写）时必须同步核算，否则报表静默失真。
- 单次写突发最大字节数 = `burst × DWIDTH / 8`（本工程 1KB，AXI4 上限 256 拍）；写方向因逐突发 B 响应开销对突发长度更敏感；burst 是链接期参数，对主机与 ini 透明。
- 继承的仓库瑕疵同样存在：`Payload Size` 打印用了循环变量 `i` 的残留值，输出无意义；`GB/s` 为十进制口径。

## 7. 下一步学习建议

本讲完成了 `offchip_bandwidth` 的读/写两个方向，下一讲（u4-l1）将离开片外内存，进入 `ubench/streaming_bandwidth`：数据经 `hls::stream`（AXIS 流）从一个内核直达另一个内核，不经过片外内存——你会发现那套工程同样是「成对内核 + stream_connect 连线 + 主机成对启动」的变体，而本讲的 `nk` 多实例与 ini 连线知识会直接复用。若你想先把带宽线收尾，可先读 `write/HBM` 工程对照 [ubench.ini](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/HBM/2ports_512bit/ubench.ini#L1-L8) 的 `HBM[0]` 与主机 bank 表的配合（u3-l3 的拓扑式 flag），再进入流带宽；若对自动化批量生成这些读写变体感兴趣，可跳到 u5-l1 的 `auto_collect`。

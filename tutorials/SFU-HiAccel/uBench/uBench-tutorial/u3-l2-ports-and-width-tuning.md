# 调参数实验：端口数量、数据位宽与突发长度的手动修改

## 1. 本讲目标

上一讲（u3-l1）我们逐行精读了读带宽内核，理解了「bundle 异名 = 独立端口」「volatile 防优化」「DATAFLOW 并发」这些机制。本讲把这些机制变成**可操作的手艺**：按照 `ubench/offchip_bandwidth/datacenter/README.md` 给出的官方调参指南，学会手动修改微基准的三个核心参数——

1. **并发端口数**：如何在内核里加一个读端口、在主机里改 `NUM_PORT`、在 ini 里补一条 `sp` 连线；
2. **端口数据位宽**：如何只改 `krnl_config.h` 里的一行 `DWIDTH`，并理解主机端 `dataSize / WIDTH_FACTOR` 换算为什么能自动跟上；
3. **最大突发长度**：如何修改 `max_read_burst_length` / `max_write_burst_length`，以及它与 AXI 协议突发传输的关系。

读完本讲，你应当能独立回答：**「我想测 4 端口 × 256bit × burst 64 的组合，到底要动哪几个文件的哪几行？」**——这正是综合实践中「手动 auto_collect」任务的全部内容，也是下一单元理解自动生成脚本在替你做什么的前提。

## 2. 前置知识

本讲假设你已读过 u3-l1（读带宽内核精读）。下面把会用到的概念用通俗语言快速复习一遍：

- **m_axi 端口与 bundle**：内核函数的指针参数加上 `#pragma HLS INTERFACE m_axi ... bundle=gmemX` 后，会综合成一个 AXI 主端口直连片外内存。**bundle 名字不同 = 不同的物理端口**；同一个 bundle 名下的多个参数共享一个端口。
- **理论峰值公式**：\(\text{峰值带宽} = \text{频率} \times \text{端口数} \times \text{端口位宽} / 8\)。例如 300 MHz × 2 端口 × 512 bit ÷ 8 = 38.4 GB/s。本讲会反复用到它来核算每个参数改动的「理论收益」。
- **AXI 突发（burst）**：AXI 协议里，主设备发出**一次**地址请求后，可以连续接收/发送多个数据拍（beat），不必每个数据都重新发地址。一次突发最多传输的数据拍数由协议和 pragma 共同限制——这就是 `max_read_burst_length` 控制的东西。突发越长，发地址的命令开销被摊得越薄。
- **beat（拍）**：突发里的一次数据传输，宽度等于端口位宽。512 bit 端口的一次 beat 是 64 字节。
- **宽字（wide word）与 int 的换算**：内核循环变量 `j` 每走一步，搬运的是一个 `INTERFACE_WIDTH`（`ap_uint<DWIDTH>`）宽字，而不是一个 32 位 int。主机端缓冲区仍按 int 分配，所以送进内核前要除以 `WIDTH_FACTOR = DWIDTH/32`。
- **`volatile` 双保险与 DATAFLOW**：每条读循环要独占一个 `volatile` 临时变量（防止读被优化掉），且循环之间不能有数据依赖（DATAFLOW 才能并行）。这两点是「加端口」时必须复制的东西。

另外提醒一个上一讲的结论：仓库里「目录名只是参数乘积」。例如 `write/DDR/2ports_512bit` 实际是 **2 个内核实例 × 每内核 1 个端口**（见 4.1.3 的实测证据），而不是 1 内核 2 端口。改参数时以 `src/` 代码和 ini 为准，不要信目录名的字面分解。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [ubench/offchip_bandwidth/datacenter/README.md](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/README.md) | 官方调参指南 | 六节操作清单（本讲用到第 1、2、3、4、6 节） |
| [ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h) | 内核与主机共用的参数契约头 | `DWIDTH` / `WIDTH_FACTOR` 单点改位宽 |
| [ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp) | 读带宽 HLS 内核 | 端口签名、bundle、临时变量、并发循环、突发 pragma |
| [ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp) | OpenCL 主机程序 | `NUM_PORT`、payload 扫描、`WIDTH_FACTOR` 换算、带宽公式 |
| [ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini) | 链接期连接配置 | 加端口后必须补 `sp=` 行 |
| [ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp) | 写带宽内核（对照用） | `max_write_burst_length` 的位置 |
| [ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile) | 构建脚本 | README 第 6 节频率参数的真实落点 |

先给出本讲最核心的一张**参数 → 联动文件清单**总表，后面三个模块分别展开：

| 要改的参数 | 改哪里 | 必须联动的文件 |
| --- | --- | --- |
| 并发端口数 N | 内核签名加参数、每参数一行 m_axi pragma（新 bundle 名）、一行 s_axilite、一个 volatile 临时变量、一条并发读循环；主机 `NUM_PORT`；ini 加 `sp=` | `krnl_ubench.cpp`、`host.cpp`、`ubench.ini` |
| 端口位宽 | `krnl_config.h` 的 `DWIDTH`（`WIDTH_FACTOR` 自动派生） | 仅 `krnl_config.h`（主机经 `WIDTH_FACTOR` 自动跟随） |
| 最大突发长度 | m_axi pragma 的 `max_read_burst_length` / `max_write_burst_length` | 仅 `krnl_ubench.cpp`（每端口一行） |
| 连续访问数据量 | 主机 payload 循环边界 | 仅 `host.cpp` |
| DDR/HBM 连接与放置 | 主机 bank flag + ini 的 `sp`/`slr`/`nk` | `host.cpp`、`ubench.ini`（详见 u3-l3） |
| 内核频率 | `--kernel_frequency` 编译选项 | Makefile（注意：仅 auto_collect 生成的 Makefile 里有，见 4.3.3） |

## 4. 核心概念与源码讲解

### 4.1 端口数扩展

#### 4.1.1 概念说明

「并发端口数」指内核**同时**驱动多少个独立的 AXI 主端口去读内存。它是五因素里最直接的一个：理论峰值与端口数成线性关系——每多一个端口，理论上每秒多搬 `频率 × 位宽 / 8` 字节。

但端口不是一个数字开关，而是**四个必须同步的东西**：

1. **内核签名里的指针参数**（`in0`, `in1`, ...）——每个参数一个端口；
2. **每个参数的 m_axi pragma**——且 bundle 必须**取新的、不重复的名字**（`gmem0`, `gmem1`, ...），同名 bundle 会把多个参数合并回一个端口；
3. **每条循环独占的 volatile 临时变量**（`temp_data_0`, `temp_data_1`, ...）——既是防死代码消除，也是 DATAFLOW 无依赖的前提；
4. **相同结构的并发循环**——每个端口一条自己的双层循环。

主机侧则要改 `NUM_PORT` 宏，ini 侧要为每个新端口补一条 `sp=` 连线。漏掉任何一处，结果不是编译错误就是**测量语义悄悄变化**（比如两个端口共享一个 bundle，测出来还是单端口带宽）。

#### 4.1.2 核心流程

README 第 1 节给出的官方流程（按内核 → 主机两步）：

```text
加端口三连（krnl_ubench.cpp）
  ① 签名加 volatile INTERFACE_WIDTH* inK
     + #pragma HLS INTERFACE m_axi  port=inK offset=slave bundle=gmemK max_read_burst_length=16
     + #pragma HLS INTERFACE s_axilite port=inK bundle=control
  ② 加 volatile INTERFACE_WIDTH temp_data_K;
  ③ 复制一条 for(i<NUM_ITERATIONS){ for(j<size){ #pragma HLS PIPELINE II=1; temp_data_K = inK[j]; } }

主机（host.cpp）
  ④ #define NUM_PORT 2  →  新端口数

连接（ubench.ini）
  ⑤ 补 sp=krnl_ubench_1.inK:DDR[1]
```

流程上还要理解主机侧为什么「只改一个宏就够了」：`host.cpp` 里所有与端口数相关的容器都是按 `NUM_KERNEL*NUM_PORT` 大小创建的向量，`setArg` 也是双层循环（外层内核、内层端口），所以它们天然参数化。唯一手工对齐的点是 **ini 的 `sp` 行数**——链接器不会自动为新端口分配内存通道。

#### 4.1.3 源码精读

**（a）内核端口三件套的现状。** 这是当前 2 端口版本的定义处：

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp:L4-L12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L4-L12)

```cpp
void krnl_ubench(volatile INTERFACE_WIDTH* in0, volatile INTERFACE_WIDTH* in1, const int size) {
#pragma HLS INTERFACE m_axi port=in0 offset=slave bundle=gmem0   max_read_burst_length=16 
#pragma HLS INTERFACE m_axi port=in1 offset=slave bundle=gmem1   max_read_burst_length=16 

#pragma HLS INTERFACE s_axilite port=in0 bundle=control
#pragma HLS INTERFACE s_axilite port=in1 bundle=control
```

- L4：签名里两个指针参数 `in0`/`in1` 加尾部的 `size`——**参数顺序很重要**，主机 `setArg` 的编号就是按这个顺序排的（见 (d)）。
- L5-L6：两行 m_axi pragma。注意 bundle 一个叫 `gmem0`、一个叫 `gmem1`——**异名才独立**。`max_read_burst_length=16` 是每个端口各写一遍的（4.3 节的主角）。加第三个端口就是照抄一行：`port=in2 ... bundle=gmem2`。
- L8-L9：每个指针参数还要一行 `s_axilite`，把该端口的基地址寄存器挂到 control 组，主机才能通过 `setArg` 写入地址。

**（b）临时变量与并发循环。**

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp:L14-L31](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L14-L31)

- L14-L15：两个 `volatile INTERFACE_WIDTH` 临时变量，一个循环一个，互不共享——这是 u3-l1 讲过的「防优化双保险」的消费者一侧，也是让两条循环互相无数据依赖的关键。
- L17：`#pragma HLS DATAFLOW` 让下面两条循环成为并行进程；新加的循环只要同样不依赖别人的结果，就会自动加入并行。
- L19-L24 与 L26-L31：结构完全相同的两条双层循环，只是 `temp_data_0 = in0[j]` 换成 `temp_data_1 = in1[j]`。加端口 = 再抄一份换成 `in2` / `temp_data_2`。

**（c）主机端的端口数开关。**

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp:L15-L16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L15-L16)

```cpp
#define NUM_KERNEL 1
#define NUM_PORT 2
```

这两行就是 README 第 1 节主机步骤的全部。README 的示例片段与真实代码一致（[README.md:L26-L31](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/README.md#L26-L31)）。

`NUM_PORT` 之所以只改这一处，看两个被它参数化的位置：

- [host.cpp:L110-L111](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L110-L111)：`std::vector<cl_mem_ext_ptr_t> source_in_ext(NUM_KERNEL*NUM_PORT);` 和 buffer 向量——每端口一个设备缓冲；
- [host.cpp:L133-L140](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L133-L140)：循环 `for (i < NUM_KERNEL*NUM_PORT)` 逐个创建缓冲并搬运数据，每缓冲大小 `sizeof(int) * dataSize`。

**（d）setArg 的编号与内核签名顺序的隐式契约。**

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp:L155-L164](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L155-L164)

```cpp
dataSize = dataSize / WIDTH_FACTOR;
int i, j = 0;
for (i = 0; i < NUM_KERNEL; i++) {
    for (j = 0; j < NUM_PORT; j++) {
        OCL_CHECK(err, err = cmpt_krnl[i].setArg(j, source_in_buffer[i*NUM_PORT+j])); 
    }
    OCL_CHECK(err, err = cmpt_krnl[i].setArg(j, dataSize)); 
    OCL_CHECK(err, err = q.enqueueTask(cmpt_krnl[i]));
}
```

内层循环先把第 0..NUM_PORT-1 号参数依次设为各端口的 buffer，循环退出时 `j == NUM_PORT`，正好把 `size` 设到第 NUM_PORT 号参数上。这依赖内核签名「先所有端口指针、最后 size」的排列。所以**加端口时新指针必须加在 `size` 之前**，编号契约才成立。

**（e）ini 连线是第四处联动。**

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini:L1-L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L1-L6)

```ini
[connectivity]
slr=krnl_ubench_1:SLR1
sp=krnl_ubench_1.in0:DDR[1]
sp=krnl_ubench_1.in1:DDR[1]

nk=krnl_ubench:1
```

每端口一行 `sp=`，把 `in0`/`in1` 各自连到 `DDR[1]` 通道。加 `in2` 必须补 `sp=krnl_ubench_1.in2:DDR[1]`（或你想测的其它通道——那是 u3-l3 的主题）。**主机 flags（[host.cpp:L115-L128](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L115-L128) 的 `XCL_MEM_DDR_BANK1`）要与 ini 的 sp 行保持同一通道**，且该循环按 `NUM_KERNEL*NUM_PORT` 遍历，改宏后自动覆盖新端口。

**（f）反例佐证：目录名是乘积。** `write/DDR/2ports_512bit` 的内核只有一个写端口：

[ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp:L4-L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp#L4-L6)

```cpp
void krnl_ubench(volatile INTERFACE_WIDTH* out0, const int size) {
#pragma HLS INTERFACE m_axi port=out0 offset=slave bundle=gmem0 max_write_burst_length=16
```

它的主机是 `#define NUM_KERNEL 2`、`#define NUM_PORT 1`（[write host.cpp:L15-L16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L15-L16)），ini 里是两个实例各连一个 `out0`（`nk=krnl_ubench:2`）。**同样是「2ports」，实现方式可以是 1 内核 × 2 端口（read 版）或 2 内核 × 1 端口（write 版）**——这也说明端口扩展与内核实例扩展（`nk` + `NUM_KERNEL`）是两条正交的加带宽路线。

#### 4.1.4 代码实践

**实践目标**：不动源码仓库，在草稿上完成「read 内核扩到 3 端口」的完整改动清单，检验你对五处联动的掌握。

**操作步骤**：

1. 复制 `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/` 到仓库外的实验目录（如 `/tmp/3ports_512bit/`）；
2. 在 `krnl_ubench.cpp` 中：签名加 `volatile INTERFACE_WIDTH* in2`（放在 `const int size` 之前）；加 m_axi 行（bundle=`gmem2`）与 s_axilite 行；加 `volatile INTERFACE_WIDTH temp_data_2;`；照抄第三条双层循环；
3. 在 `host.cpp` 中把 `NUM_PORT` 改为 3；
4. 在 `ubench.ini` 中补 `sp=krnl_ubench_1.in2:DDR[1]`；
5. （可选，需本机装 Vitis）`make build TARGET=sw_emu DEVICE=<平台>` 验证能过编译链接。

**需要观察的现象**（有 Vitis 环境时）：sw_emu 能运行且输出 11 档带宽行；无 Vitis 时做静态检查：`grep -c gmem src/krnl_ubench.cpp` 应为 3，`grep -c "sp=" ubench.ini` 应为 3。

**预期结果**：理论峰值变为 \(300\,\text{MHz} \times 3 \times 64\,\text{B} = 57.6\,\text{GB/s}\)（若三端口仍连同一 `DDR[1]` 通道，实测大概率受通道争用限制达不到——这正是本套微基准要暴露的现象）。带宽数值本身**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把新端口 `in2` 的 pragma 写成 `bundle=gmem0`（与 in0 同名），会发生什么？

**答案**：`in0` 与 `in2` 会共享同一个 AXI 主端口（同一 bundle 合并），端口数实际还是 2。编译能通过、测量照常出数，但带宽语义已变——这是最危险的「静默错误」。bundle 异名是端口独立的充要开关。

**练习 2**：为什么每条读循环必须有自己独占的 `temp_data_K`，不能三条循环共用一个临时变量？

**答案**：两个原因。其一，防死代码消除需要每次读到的值被「消费」，共用变量时最后一条循环的赋值会覆盖前面的消费痕迹；更根本的是其二：共用变量会让三条循环之间产生写后写（WAW）数据依赖，DATAFLOW 无法把它们拆成并行进程，退化为串行执行，带宽测量失效。

**练习 3**：`read/DDR/2ports_512bit` 与 `write/DDR/2ports_512bit` 目录名同为 2ports，两者的实现差异是什么？

**答案**：read 版是 1 个内核实例（`nk=krnl_ubench:1`）× 每内核 2 个读端口（`NUM_KERNEL 1` / `NUM_PORT 2`）；write 版是 2 个内核实例（`nk=krnl_ubench:2`）× 每内核 1 个写端口（`NUM_KERNEL 2` / `NUM_PORT 1`）。2 = 核数 × 端口数的乘积相同，分解不同。

### 4.2 位宽修改

#### 4.2.1 概念说明

端口位宽由 `krnl_config.h` 里的一个 `const int DWIDTH` **单点控制**。这是整个微基准设计里最优雅的一处：因为内核与主机 include 同一个头文件，类型 `INTERFACE_WIDTH = ap_uint<DWIDTH>` 与换算因子 `WIDTH_FACTOR = DWIDTH/32` 都从这一个常量派生，改位宽理论上只需要改一行。

为什么主机端必须有 `WIDTH_FACTOR` 换算？因为**两边的计数单位不同**：

- 主机世界：缓冲区按 32 位 int 分配，`payload`、`dataSize` 都以 int 为单位；
- 内核世界：内层循环每步搬运一个 `ap_uint<DWIDTH>` 宽字，`size` 以宽字为单位。

换算关系：

\[
\text{宽字个数} = \frac{\text{int 个数} \times 32}{\text{DWIDTH}} = \frac{\text{dataSize}}{\text{WIDTH_FACTOR}}, \qquad \text{WIDTH_FACTOR} = \frac{\text{DWIDTH}}{32}
\]

同时位宽进入理论峰值公式：位宽翻倍，单端口每拍搬运字节翻倍，峰值线性上升。所以「端口数 × 位宽」这个**乘积**才是理论峰值的决定量——4 端口 × 256 bit 与 2 端口 × 512 bit 的理论峰值完全相同（都是 38.4 GB/s @300 MHz），这是一个非常值得记住的对称性。

#### 4.2.2 核心流程

```text
改位宽（理论上单点）
  krnl_config.h:  const int DWIDTH = 512;  →  新位宽
    ├─ INTERFACE_WIDTH = ap_uint<DWIDTH>   自动跟随（类型）
    ├─ WIDTH_FACTOR = DWIDTH/32            自动跟随（主机换算）
    └─ 内核签名/临时变量都用 INTERFACE_WIDTH，自动跟随

改完必须人工核对的三件事
  ① DWIDTH 必须是 32 的整数倍（WIDTH_FACTOR 为整数，ap_uint 位宽合法）
  ② 主机每档 payload ÷ WIDTH_FACTOR 后 ≥ 1（最小档 256 int 不会被除成 0）
  ③ 缓冲区字节数 = sizeof(int)*dataSize 与端口位宽的对齐要求（宽字跨界读）
```

注意 README 第 2 节说改的是 `krnl_ubench.h`（[README.md:L33-L37](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/README.md#L33-L37)），**实际文件名是 `src/krnl_config.h`**——这是仓库的一处文档滞后，代码以真实文件为准。

#### 4.2.3 源码精读

**（a）单点契约。** 整个位宽机制的四行：

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h:L1-L7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L1-L7)

```cpp
#include "ap_int.h"
#include <inttypes.h>

const int DWIDTH = 512;
#define INTERFACE_WIDTH ap_uint<DWIDTH>
const int WIDTH_FACTOR = DWIDTH/32;
const int NUM_ITERATIONS = 10000;
```

- L4：`DWIDTH = 512` —— 唯一要改的行；
- L5：宽类型是宏，所有 `INTERFACE_WIDTH` 出现处（内核签名、临时变量）在编译期展开为 `ap_uint<512>`；
- L6：`WIDTH_FACTOR = 512/32 = 16`，供主机换算；
- L7：`NUM_ITERATIONS` 与位宽无关，但记得它以魔数 `0.000010000` 的身份藏进主机带宽公式（u2-l3 讲过），此处不动它。

**（b）主机侧的换算点。**

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp:L99-L107](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L99-L107)

payload 扫描循环每档把 `dataSize = payload`（int 个数），仿真模式钉在 256。缓冲区 `std::vector<int, ...> read_source(dataSize)` 按 int 分配——**缓冲区大小不随位宽变**，位宽只改变「这些 int 被几个宽字搬走」。

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp:L155](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L155)

```cpp
dataSize = dataSize / WIDTH_FACTOR;
```

送参前除以 `WIDTH_FACTOR`，把 int 个数换算成宽字个数。以 DWIDTH=512 为例：payload=262144 个 int（1MB）→ size=16384 个 64B 宽字。**这行在 payload 循环体内、且每档开头 `dataSize` 都被重新赋值为 `payload`（[L100-L101](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L100-L101)），所以除法不会跨档累积**——这是读这段代码时容易疑惑的一点。

**（c）为什么带宽公式不用改。**

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp:L172](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L172)

```cpp
double bw_result = payload * 4 * 0.000010000 / kernel_time_in_sec * NUM_KERNEL * NUM_PORT;
```

公式以 `payload * 4`（字节数）为起点，从未出现 `WIDTH_FACTOR`——因为换算已经在送参时做过了，无论位宽多少，每档实际搬运的字节都是 `payload × 4 × NUM_PORT`。这保证了**改 DWIDTH 不需要动公式**，也保证不同位宽的测量结果可以直接按「GB/s」互相比较。

#### 4.2.4 代码实践

**实践目标**：把 DWIDTH 改为 64，在纸上推演所有受影响的数值，验证「单点改动、全局跟随」。

**操作步骤**：

1. 复制工程到实验目录，改 `krnl_config.h` 的 `DWIDTH` 为 64；
2. 逐项填写下面这张推演表（先自己算，再对答案）：

| 项 | DWIDTH=512（原） | DWIDTH=64（改后） |
| --- | --- | --- |
| `WIDTH_FACTOR` | 16 | ？ |
| `INTERFACE_WIDTH` | `ap_uint<512>`（64B/宽字） | ？ |
| payload=262144 档的 `size`（宽字个数） | 16384 | ？ |
| 每端口每内层循环全量搬运字节 | 1MB | ？ |
| 单端口理论峰值 @300MHz | 19.2 GB/s | ？ |

3. 核对最小档：payload=256 时 `size = 256/2 = 128` 个宽字，不为零，安全；
4. （可选，需 Vitis）`make check TARGET=sw_emu DEVICE=<平台>` 跑仿真。

**需要观察的现象**：sw_emu 输出的 11 档 Execution time 与带宽（数值无物理意义，只验证链路通）。

**预期结果**：表中答案依次为 2、`ap_uint<64>`（8B/宽字）、131072、1MB（不变！）、2.4 GB/s。关键认知：**每档实际访问的字节数与位宽无关**（都由 payload 决定），位宽改变的只是「搬运这些字节的通道宽度」。真机带宽**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 DWIDTH 改成 100，会发生什么？

**答案**：`WIDTH_FACTOR = 100/32 = 3`（整数除法截断），而一个宽字实际是 100/8 = 12.5 字节——换算彻底失真，内核读的字节数与主机以为的不一致，带宽数值错误。此外非 2 的幂位宽也让地址对齐变复杂。所以 DWIDTH 必须取 32 的整数倍（实践中取 2 的幂：32/64/128/256/512/1024）。

**练习 2**：DWIDTH 从 512 降到 256、其它不变，主机代码里有几行需要改？

**答案**：零行。`WIDTH_FACTOR` 由头文件自动派生，`host.cpp` 只是使用它；payload 循环、缓冲区分配、带宽公式都与位宽解耦。这正是「契约头」设计的价值——唯一的改动点是 `krnl_config.h` 的 `DWIDTH`。

**练习 3**：4 端口 × 256 bit 与 2 端口 × 512 bit，哪个理论峰值高？

**答案**：一样高，都是 300 MHz × (4×32B) = 300 MHz × (2×64B) = 38.4 GB/s。理论峰值只看乘积「端口数 × 位宽」。但**实测**两者可能显著不同（通道争用分布、突发效率、布线资源），这正是要用微基准实测的原因——也是综合实践让你亲手造 `4ports_256bit` 来对比的动机。

### 4.3 突发长度调整

#### 4.3.1 概念说明

AXI 突发传输的直觉：**发一次地址，搬一串数据**。地址相位（发命令、经互联、仲裁）是有固定开销的；数据相位每拍连续流动。若每次只搬一小段就要重新发地址，带宽大量损耗在命令开销上。

`max_read_burst_length=N`（及写侧的 `max_write_burst_length=N`）告诉 Vitis HLS：这个端口综合出的 AXI 通路，单次突发最多允许 N 个数据拍（beat）。每拍宽度 = 端口位宽，所以：

\[
\text{单次突发最多搬运字节} = \text{burst} \times \frac{\text{DWIDTH}}{8}
\]

\[ \text{512 bit} \times 16 = 64\,\text{B} \times 16 = 1\,\text{KB}, \qquad 256\,\text{bit} \times 64 = 32\,\text{B} \times 64 = 2\,\text{KB} \]

三个要点：

1. **默认值是 16**。README 明确说示例工程的 16 是「Vivado HLS 默认值」（[README.md:L2](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/README.md#L2)）；
2. **AXI4 协议上限 256 拍**——burst 不能无限加长，协议层面 ARLEN/AWLEN 字段 8 位，最多 256 beat；
3. **突发只在连续地址上生效**。内核内层循环 `j` 顺序递增、II=1 每拍发一个请求（u3-l1 讲过），HLS/互联把这些连续请求合并成突发，单突发长度封顶在 pragma 值。这就是为什么突发长度与「连续访问数据量」（payload）两个因素互相耦合：payload 只有 1KB 时，512bit×16 的突发刚够铺满一次扫描；突发再长也没有连续地址可合并。

#### 4.3.2 核心流程

```text
改突发长度（每端口独立）
  读端口：#pragma HLS INTERFACE m_axi ... max_read_burst_length=<N>
  写端口：#pragma HLS INTERFACE m_axi ... max_write_burst_length=<N>

生效链条
  pragma → v++ -c 综合 AXI 通路 → 请求合并上限 → 单突发字节数 → 命令开销摊薄程度 → 实测带宽

数量感觉（搬运 1MB 连续数据、512bit 端口）
  burst=16  → 1MB / 1KB  = 1024 次突发（1024 次地址相位）
  burst=256 → 1MB / 16KB =   64 次突发（  64 次地址相位）
```

改动位置极小（pragma 里一个数字），但它是对实测带宽影响最大的参数之一——KNN 案例里 `suboptimal_14PE` 与 `optimal_14PE` 两个设计的内核**唯一差别就是 16 → 256**（u6-l2 会精读）。

#### 4.3.3 源码精读

**（a）读侧 pragma。**

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp:L5-L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L5-L6)

```cpp
#pragma HLS INTERFACE m_axi port=in0 offset=slave bundle=gmem0   max_read_burst_length=16 
#pragma HLS INTERFACE m_axi port=in1 offset=slave bundle=gmem1   max_read_burst_length=16 
```

每个读端口一行，当前都是 16。README 第 3 节（[README.md:L39-L46](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/README.md#L39-L46)）给出的修改方式与之一致：直接改 pragma 数字。注意不需要动主机——突发长度纯粹是内核/通路属性，对主机完全透明。

**（b）写侧 pragma（对照）。**

[ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp:L4-L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp#L4-L6)

写内核用的是 `max_write_burst_length=16`。读写是两条独立的 AXI 通路（读地址/读数据 与 写地址/写数据各有自己的通道），所以两个参数分开控制；做读写混合设计时要各自设置。

**（c）README 第 6 节的频率参数——一处文档滞后。** README 说频率在 Makefile 里改：

[ubench/offchip_bandwidth/datacenter/README.md:L74-L79](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/README.md#L74-L79)

```ini
# Kernel compiler global settings
CLFLAGS += -t $(TARGET) --platform $(DEVICE) --save-temps --kernel_frequency 300
```

但手写工程的真实 Makefile 里**没有** `--kernel_frequency`：

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile:L65-L69](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L65-L69)

```makefile
# Kernel compiler global settings
CLFLAGS += -t $(TARGET) --platform $(DEVICE) --save-temps 
ifneq ($(TARGET), hw)
	CLFLAGS += -g
endif
```

`--kernel_frequency` 只出现在 auto_collect 自动生成的 Makefile 里（由 [ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py:L75](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py#L75) 拼出 `'CLFLAGS += ... --kernel_frequency ' + str(kernel_freq)`）。想给手写工程指定频率，需自行在 Makefile L66 行尾追加该选项——频率与突发长度同属「影响每秒能搬多少字节」的一类，但一个走时序约束、一个走协议参数，机制完全不同。

**（d）为什么突发对带宽公式透明。** 回看 [host.cpp:L172](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L172)：公式只数「搬了多少字节、花了多少时间」，突发长度改变的只是**搬同样字节的效率**（时间变短 → 带宽变大）。所以调突发不需要动任何主机代码，效果直接反映在 `kernel_time_in_sec` 里。

#### 4.3.4 代码实践

**实践目标**：建立突发长度的数量直觉——不跑硬件，纯推演几组「每 MB 数据需要多少次突发」。

**操作步骤**：

1. 复制 read/DDR 工程到实验目录，把两条 pragma 的 `max_read_burst_length` 从 16 改为 256；
2. 完成下表（512 bit 端口，搬运 1MB 连续数据）：

| burst | 单突发字节 | 每 MB 突发次数 | 地址相位数 |
| --- | --- | --- | --- |
| 16 | 1KB | 1024 | 1024 |
| 64 | ？ | ？ | ？ |
| 256 | 16KB | ？ | ？ |

3. 再为 256 bit 端口重算 burst=64 一行，观察「位宽减半 + burst 翻 4 倍」时单突发字节如何变化；
4. （可选，需 Vitis + 真机）分别在 burst=16 与 256 下 `make check TARGET=hw`，比较 1MB 档带宽。

**需要观察的现象**：真机上 1MB 档的带宽差（小 payload 档差异会被其它开销掩盖）。无硬件时此步**待本地验证**。

**预期结果**：表中答案：burst=64 → 单突发 4KB、256 次/MB；burst=256 → 64 次/MB。256 bit × burst 64 = 2KB 单突发字节（是 512bit×16 的两倍）。一般规律：burst 加长后大 payload 档带宽上升、趋近理论峰值；小 payload 档（1-4KB）因扫描本身短于一个长突发，几乎无收益。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `max_read_burst_length=256` 在 payload=256（1KB）档几乎不会有任何效果？

**答案**：该档每端口总共只有 1KB 连续数据 = 512 bit 端口的 16 拍。就算允许突发 256 拍，也没有那么多连续地址可合并，实际突发长度受数据量封顶在 16 拍。突发长度因素只有在「连续访问数据量」足够大时才被激活——五因素是耦合的，这正是微基准要按 payload 扫描的原因。

**练习 2**：把 burst 从 16 改到 256，主机代码要改吗？带宽公式要改吗？

**答案**：都不用。突发长度是 AXI 通路属性，只存在于内核 pragma；主机数的是字节数与时间，burst 的效果通过缩短 `kernel_time_in_sec` 自动进入带宽结果。这也是三个参数里改动成本最低的一个（每端口改一个数字）。

**练习 3**：突发长度为什么不能无限加大（比如 4096 拍）？

**答案**：AXI4 协议限制单次突发最多 256 拍（地址通道的长度字段只有 8 位）。此外超长突发会长时间占用互联与内存控制器，加剧其它主设备的仲裁延迟，还可能触碰从设备的约束。所以有效取值范围是 1..256，且要结合位宽核算「单突发字节数」是否已经超过单次扫描的数据量。

## 5. 综合实践：手动 auto_collect —— 亲手造一个 `4ports_256bit`

auto_collect 脚本族（u5 单元精读）能按参数组合批量生成微基准目录；本实践让你**手动**当一次生成器，把 `2ports_512bit` 改造成 `4ports_256bit`，从而把本讲三个模块的所有改动点串成一条流水线。这正是 auto_collect 里 `kernelcode_gen.py` / `hostcode_gen.py` / `connectivity_gen.py` 替你做的事情——做完手工版，读脚本时会处处会心一笑。

**任务规格**：端口数 4、位宽 256 bit、burst 64，其余（300 MHz、DDR、读访问、payload 扫描）不变。

**步骤 1：复制样板。**

```bash
cp -r ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit /tmp/4ports_256bit
cd /tmp/4ports_256bit
```

（放 `/tmp` 或任何仓库外位置，避免污染源码树。）

**步骤 2：按联动清单逐文件修改。**

| 文件 | 改动 | 依据（本讲章节） |
| --- | --- | --- |
| `src/krnl_config.h` | `DWIDTH` 512 → 256（`WIDTH_FACTOR` 自动变 8） | 4.2 |
| `src/krnl_ubench.cpp` | 签名加 `in2`/`in3`（在 `size` 前）；m_axi 各一行，bundle=`gmem2`/`gmem3`，`max_read_burst_length=64`（四行全部改 64）；s_axilite 各一行；`temp_data_2`/`temp_data_3` 各一个；再抄两条读循环 | 4.1 + 4.3 |
| `src/host.cpp` | `NUM_PORT` 2 → 4（仅此一处；`NUM_KERNEL` 保持 1） | 4.1 |
| `ubench.ini` | 补两行：`sp=krnl_ubench_1.in2:DDR[1]`、`sp=krnl_ubench_1.in3:DDR[1]` | 4.1 |

改完的内核签名应当是：

```cpp
// 示例代码：由你按上述规格修改后的内核形态（非仓库原有内容）
void krnl_ubench(volatile INTERFACE_WIDTH* in0, volatile INTERFACE_WIDTH* in1,
                 volatile INTERFACE_WIDTH* in2, volatile INTERFACE_WIDTH* in3,
                 const int size)
```

**步骤 3：核算 payload 循环对应的实际连续访问字节数。**

payload 循环（[host.cpp:L100](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L100)）从 256 到 262144、每次 ×2，共 11 档。每档：

\[
\text{每端口连续访问字节} = \text{payload} \times 4\,\text{B} = 1\,\text{KB} \sim 1\,\text{MB}
\]

位宽改成 256 后这个范围**不变**：`dataSize / WIDTH_FACTOR = payload / 8` 个 32B 宽字，字节数仍是 payload×4。验证两个端点：payload=256 → size=32 个宽字（32×32B=1KB）；payload=262144 → size=32768 个宽字（1MB）。四个端口全开时每档总搬运量 = payload×4×4B，即 4KB ~ 4MB。

**步骤 4：核算理论峰值并预判。**

\[
300\,\text{MHz} \times 4\,\text{端口} \times \frac{256}{8}\,\text{B} = 38.4\,\text{GB/s}
\]

与样板 `2ports_512bit` 的理论峰值相同（乘积不变）。但四个端口在 ini 里仍连**同一个** `DDR[1]` 通道，共享通道争用会更激烈——你应当预判实测带宽低于 38.4 GB/s，且与 2 端口版本的差距本身就是一次有意义的测量（想拆到多通道？那是 u3-l3 的主题）。单突发字节 = 64 拍 × 32B = 2KB，是样板（1KB）的两倍，大 payload 档的突发效率应当更好。

**步骤 5：验证。**

- 无 Vitis：静态自查——`grep -c "bundle=gmem" src/krnl_ubench.cpp` = 4；`grep -c "sp=" ubench.ini` = 4；`grep NUM_PORT src/host.cpp` 显示 4；`grep max_read_burst_length src/krnl_ubench.cpp` 四行都是 64。
- 有 Vitis：`make check TARGET=sw_emu DEVICE=<平台>` 跑通功能链路（sw_emu 的带宽数值无物理意义，见 u1-l3）；真机带宽**待本地验证**。

## 6. 本讲小结

- **端口数扩展是四处联动**：内核签名+pragma（新 bundle 名）、独占的 volatile 临时变量、并发读循环、主机 `NUM_PORT`，外加 ini 的 `sp=` 行与 bank flag；bundle 异名是端口独立的开关，内核签名「先端口后 size」的顺序是 setArg 编号契约。
- **位宽是单点改动**：只改 `krnl_config.h` 的 `DWIDTH`，类型 `INTERFACE_WIDTH` 与换算因子 `WIDTH_FACTOR` 自动派生；主机送参前 `dataSize / WIDTH_FACTOR` 把 int 个数换成宽字个数，保证每档实际访问字节数（payload×4）与带宽公式都和位宽解耦。DWIDTH 必须是 32 的整数倍。
- **突发长度是每端口一个数字**：`max_read_burst_length` / `max_write_burst_length` 限定单次突发的最大 beat 数（AXI4 上限 256），单突发字节 = burst × 位宽/8；它对主机完全透明，效果全部体现在执行时间里，且只在连续数据量足够大时才被激活。
- **理论峰值只看乘积**：频率 × 端口数 × 位宽/8；4×256bit 与 2×512bit 理论峰值同为 38.4 GB/s，差异要靠实测分晓。
- **仓库有两处文档滞后**：README 说位宽定义在 `krnl_ubench.h`（实际是 `src/krnl_config.h`）；README 第 6 节的 `--kernel_frequency` 只存在于 auto_collect 生成的 Makefile（makefile_gen.py:L75），手写工程 Makefile 里没有。
- **目录名是参数乘积不是分解方式**：read 版 2ports = 1 内核×2 端口，write 版 2ports = 2 内核×1 端口，改参数永远以 `src/` 与 ini 代码为准。

## 7. 下一步学习建议

- **下一讲 u3-l3（DDR 与 HBM：ubench.ini 连接配置与内核布局）**：本讲我们始终让所有端口连在同一个 `DDR[1]` 上；下一讲深入 `sp`/`slr`/`nk` 三条指令与主机 `cl_mem_ext_ptr_t` flags 的配合，看把端口拆到不同内存 bank/通道后带宽如何变化——那是端口数实验真正发挥威力的地方。
- **回头对照**：拿本讲的「手动 4ports_256bit」经验去读 [auto_collect/kernelcode_gen.py](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py)（u5-l2），看脚本如何用字符串拼接自动完成你刚才的手工改动。
- **延伸阅读**：KNN 案例中 `suboptimal_14PE` 与 `optimal_14PE` 唯一差别就是 burst 16→256（u6-l2），是突发长度价值的真机证据；Vitis 官方文档 UG1393（README 第 5 节给出的链接）有 m_axi 接口全部参数的权威说明。

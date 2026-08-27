# 解剖一个最小微基准工程：krnl_config.h + 内核 + 主机 + ini + Makefile

## 1. 本讲目标

前两讲我们看清了仓库的目录地图（u1-l2）和构建运行链路（u1-l3）。本讲把镜头推近，**以一个具体工程为标本做解剖**：`ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/`——仓库中最简单、最具代表性的读带宽微基准。

学完本讲，你应该能够：

1. 说出一个微基准工程由哪**五个文件**构成，以及它们之间「谁生成、谁消费」的流水线关系。
2. 理解 [krnl_config.h](#42-配置头文件krnl_configh-是内核与主机的契约) 中 `DWIDTH`、`WIDTH_FACTOR`、`NUM_ITERATIONS` 三个宏如何**同时**影响 HLS 内核和主机程序——这是整个工程唯一把两端粘合起来的「契约文件」。
3. 跟踪一次完整的运行数据流：`host` 加载 xclbin → 创建缓冲 → `setArg` → `enqueueTask` → 内核返回 → 计算带宽。

## 2. 前置知识

本讲假设你已了解 u1-l2 的「五件套骨架」和 u1-l3 的构建目标（`sw_emu`/`hw_emu`/`hw`）。在此之外，只需补充三个通俗概念：

- **内核（kernel）与主机（host）是两个独立的编译目标**。内核源码 `krnl_ubench.cpp` 由 Vitis 的 `v++` 编译成 FPGA 比特流容器（`.xclbin`）；主机源码 `host.cpp` 由普通 `g++` 编译成 Linux 可执行文件 `ubench`。两者运行在不同的「世界」（FPGA vs x86 CPU），只能通过 OpenCL/XRT 运行时通信。
- **头文件是纯文本复用**。`krnl_config.h` 没有任何魔法，它只是一份被两端 `#include` 的常量定义——`v++` 和 `g++` 各自把它展开进自己的编译单元。这正是「一份参数，两端生效」的原因。
- **AXI 端口与物理连线**。内核函数里每个 `m_axi` 指针参数对应 FPGA 上一条通往片外内存（DDR/HBM）的通道；这条通道具体接到哪个内存通道（如 `DDR[1]`），不在 C++ 代码里定，而在链接期的 `ubench.ini` 里定。

> 术语速查：**CU（Compute Unit，计算单元）** 是内核的一个硬件实例。一个内核可以实例化多份（由 ini 的 `nk` 控制），每份独立占用端口资源。

## 3. 本讲源码地图

本讲围绕同一目录下的五个文件展开（另有 `utils.mk` 已在 u1-l3 讲过，此处不重复）：

| 文件 | 角色 | 一句话职责 |
|---|---|---|
| `src/krnl_config.h` | 参数契约 | 定义 `DWIDTH`/`WIDTH_FACTOR`/`NUM_ITERATIONS`，被内核与主机共同 include |
| `src/krnl_ubench.cpp` | 被测对象（HLS 内核） | 双端口读循环，制造可控的片外读流量 |
| `src/host.cpp` | 测试驱动（OpenCL 主机） | 加载 xclbin、分配缓冲、启动内核、计时、算带宽 |
| `ubench.ini` | 物理连线表 | 告诉链接器内核放哪个 SLR、端口接哪个内存 bank、实例化几份 |
| `Makefile` | 装配线 | 把上面四者组织成两条编译流水线（`v++` 出 xclbin，`g++` 出可执行文件） |

此外会顺带引用公共库 [common/includes/xcl2/xcl2.hpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.hpp)（主机端的设备发现等辅助函数，深入讲解留给 u2-l2）。

## 4. 核心概念与源码讲解

### 4.1 五文件流水线

#### 4.1.1 概念说明

为什么需要五个文件？因为一个微基准本质上是一次**受控实验**，它把实验参数分散在三个层面：

- **算法层**（内核源码）：端口怎么读、循环怎么排；
- **物理层**（ini）：数据从哪片内存来、内核放在哪块芯片区域；
- **流程层**（主机）：何时启动、跑多久、怎么换算成带宽。

而 `krnl_config.h` 是贯穿三层的「实验参数登记表」，`Makefile` 则是把这一切装配起来的流水线。理解这五者的**生成/消费关系**，比理解任何单个文件的细节都重要——仓库里其余几十个工程全部复制这套骨架。

#### 4.1.2 核心流程

五个文件在构建期与运行期的数据流可以画成：

```text
构建期（make all）：

  krnl_config.h ──#include──┐
                            ├──▶ v++ -c ──▶ krnl_ubench.xo ─┐
  krnl_ubench.cpp ──────────┘                              │
                                                   v++ -l --config ubench.ini
  ubench.ini ──────────────────────────────────────────────┤
                            ├──▶ g++ ──▶ ubench(可执行文件)  ├──▶ ubench.xclbin
  krnl_config.h ──#include──┘                              │
  host.cpp ─────────────────┘                              │
  (xcl2.cpp 等公共库，经 .mk 注入)                           │
                                                           ▼
运行期（./ubench ubench.xclbin）：                    加载 xclbin

  host.cpp 的 main() ──启动──▶ krnl_ubench 内核（其物理形态由 ini 决定）
        ▲                                              │
        └────────────── q.finish() 等待完成 ────────────┘
```

关键点：**ini 只在构建期被消费一次**（链接成 xclbin 时决定物理连线），运行期主机通过 `cl_mem_ext_ptr_t` 的 bank flag 与之「隔空对齐」——两边写错一边，数据就会走错内存。

#### 4.1.3 源码精读

先看 Makefile 里两条流水线的交汇点。

**① 内核流水线：源码 → .xo → .xclbin**

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile:80-81](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L80-L81) 登记了产物与中间物：`ubench.xclbin` 是最终比特流容器，`krnl_ubench.xo` 是内核的编译中间对象。

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile:95-100](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L95-L100) 是两条 `v++` 规则：第一条 `v++ -c -k krnl_ubench` 把内核源码编译成 `.xo`；第二条 `v++ -l` 把 `.xo` 链接成 `.xclbin`。

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile:73](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L73) 这行把 `ubench.ini` 挂到链接flags 上——**这就是 ini 被消费的唯一入口**：

```make
LDCLFLAGS += --config ./ubench.ini
```

**② 主机流水线：源码 → 可执行文件**

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile:47-56](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L47-L56) 先 include 两个公共 `.mk` 片段（注入 OpenCL 头文件路径与 `xcl2.cpp` 源码），然后把本工程的主机源码加进来：

```make
HOST_SRCS += src/host.cpp src/krnl_config.h
```

注意 `krnl_config.h` 也被列进 `HOST_SRCS`——头文件本不参与链接，把它写在这里的真实意图是**充当依赖**：改了参数头，主机可执行文件会触发重新编译。

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile:103-104](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L103-104) 用 `$(CXX)`（即 g++）把 `HOST_SRCS` 编成 `ubench` 可执行文件。

**③ ini：三行指令定乾坤**

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini:1-6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L1-L6) 全文只有三条有效指令：

```ini
slr=krnl_ubench_1:SLR1        ; 把 CU 实例 krnl_ubench_1 放到 SLR1 区域
sp=krnl_ubench_1.in0:DDR[1]   ; 端口 in0 接到 DDR 通道 1
sp=krnl_ubench_1.in1:DDR[1]   ; 端口 in1 也接到 DDR 通道 1
nk=krnl_ubench:1              ; 内核 krnl_ubench 实例化为 1 份
```

对照着看：`nk=1` ⇒ 只有一个 CU，名字按 `{kernel}_{序号}` 规则是 `krnl_ubench_1`；两条 `sp` 恰好对应内核签名的两个指针参数 `in0`/`in1`。**ini 里的端口名必须与内核函数参数名逐字一致**，拼错会在链接期报「找不到端口」。

#### 4.1.4 代码实践

**实践：亲手验证五件套的引用关系（源码阅读型，5 分钟）**

1. **实践目标**：用工具证明上面讲的生成/消费关系不是纸上谈兵。
2. **操作步骤**：在 `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/` 目录下执行：
   ```bash
   # 谁消费了 krnl_config.h？
   grep -n "krnl_config.h" src/*.cpp
   # ini 在哪被消费？
   grep -n "ubench.ini" Makefile
   # 内核源码在哪被消费？
   grep -n "krnl_ubench.cpp" Makefile
   ```
3. **需要观察的现象**：第一条命令应在 `krnl_ubench.cpp` 和 `host.cpp` 里各命中一次 `#include`；第二条应命中 Makefile 第 73 行；第三条应命中第 95 行的 `v++ -c` 规则。
4. **预期结果**：三条 grep 的命中位置与 4.1.3 的讲解完全对应，即可确认「契约被两端共享、ini 只进链接器、内核只进 v++」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ubench.ini` 的 `nk=krnl_ubench:1` 改成 `nk=krnl_ubench:2`，但 `host.cpp` 不动，会发生什么？

**答案**：链接器会生成两个 CU（`krnl_ubench_1`、`krnl_ubench_2`），但主机端 `NUM_KERNEL` 仍为 1，只创建了绑定 `krnl_ubench_1` 的 `cl::Kernel` 对象，第二个 CU 从头到尾不会被启动。要让改动生效，必须同步把 host.cpp 第 15 行的 `NUM_KERNEL` 改为 2（并相应扩充 ini 的 `sp` 行）。这体现了五件套的「联动改」特性。

**练习 2**：`ubench.ini` 属于构建期还是运行期的文件？删掉它之后 `make` 的哪一步会失败？

**答案**：构建期。它只被 Makefile 第 73 行的 `LDCLFLAGS += --config ./ubench.ini` 消费，删除后 `v++ -l` 链接步骤（Makefile 第 98-100 行）报错；已经编好的 `ubench.xclbin` 和主机可执行文件不受影响，仍能运行。

### 4.2 配置头文件：krnl_config.h 是内核与主机的契约

#### 4.2.1 概念说明

内核用 512bit 宽的端口读数据，主机却按 32bit `int` 准备缓冲区——两边的「计量单位」差 16 倍。谁来负责换算一致？就是这份只有 7 行的头文件。它解决的问题是：**让「端口位宽」和「重复次数」这两个实验参数在两个独立编译的目标里保持同一取值**，避免手工同步出错。

#### 4.2.2 核心流程

全文逻辑是一次简单的派生：

\[ \textit{WIDTH\_FACTOR} = \textit{DWIDTH} / 32 \]

- `DWIDTH = 512`：端口位宽（bit），对应目录名里的 `512bit`；
- `INTERFACE_WIDTH` 即 `ap_uint<512>`：内核指针的元素类型（一个元素 64 字节）；
- `WIDTH_FACTOR = 512/32 = 16`：一个宽元素等于 16 个 32bit int；
- `NUM_ITERATIONS = 10000`：内核把整段数据重复读多少遍（放大执行时间，便于主机计时）。

由此得到贯穿两端的单位换算链，设主机按 int 计的负载为 \( p \)（即代码里的 `payload`）：

\[ \text{字节数} = 4p, \qquad \text{宽元素数（内核 size 参数）} = p / \textit{WIDTH\_FACTOR} \]

#### 4.2.3 源码精读

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h:1-7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L1-L7) 全文：

```cpp
#include "ap_int.h"          // Vitis HLS 任意位宽类型库
#include <inttypes.h>

const int DWIDTH = 512;                       // 端口位宽
#define INTERFACE_WIDTH ap_uint<DWIDTH>       // 内核端口元素类型
const int WIDTH_FACTOR = DWIDTH/32;           // = 16
const int NUM_ITERATIONS = 10000;             // 重复读取遍数
```

它在两端的消费点：

- **内核侧**：[krnl_ubench.cpp:1](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L1) include 后，`INTERFACE_WIDTH` 决定了函数签名（第 4 行）和临时变量（第 14-15 行）的类型，`NUM_ITERATIONS` 决定两个外层循环的次数（第 19、26 行）。
- **主机侧**：[host.cpp:13](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L13) include 后，`WIDTH_FACTOR` 出现在第 155 行的换算处（详见 4.3.3）。

注意一个细节：主机是普通 g++ 编译，为什么能 include 用到 `ap_int.h` 的头？因为 `INTERFACE_WIDTH` 是宏，主机侧不展开它就不会触碰 `ap_int.h`；而 Makefile 通过 `xcl2_CXXFLAGS` 注入的 include 路径也使得该头可被找到。**宏只在使用处生效**，这是这份契约能「一鱼两吃」的小技巧。

#### 4.2.4 代码实践

**实践：改一个数，推演全链路（纸上验证型）**

1. **实践目标**：体会「改 DWIDTH 一处，两端多处联动」。
2. **操作步骤**：
   - 假设把 `DWIDTH` 从 512 改为 256（**只在脑中或草稿上改**，本实践不修改源码）；
   - 逐项写下新取值：`WIDTH_FACTOR`、内核 `size` 参数在 `payload=256` 时的值、一次完整内层循环读取的字节数。
3. **需要观察的现象 / 预期结果**：
   - `WIDTH_FACTOR = 256/32 = 8`；
   - `payload=256` 时传给内核的 `size = 256/8 = 32` 个 256bit 元素；
   - 字节数 \( = 32 \times 32\text{B} = 1024\text{B} \)——**不变**！因为 `payload` 的单位始终是「32bit int 的个数」，位宽改变的是「一个元素多大、要读几个元素」，不改变数据总量。
   - 进一步推演：若真要落地这个改动，还需同步修改目录名（`2ports_512bit` → `2ports_256bit`）以遵守仓库命名约定，并核对带宽公式系数仍成立。
4. 本实践为纯推演，**无需运行命令**；结论可在 u3-l2 的手动改参实验中上真机验证。

#### 4.2.5 小练习与答案

**练习 1**：`payload=256`（即 1KB 数据）时，内核实际收到的 `size` 是多少？

**答案**：`dataSize/WIDTH_FACTOR = 256/16 = 16`，即 16 个 512bit 元素（16 × 64B = 1024B，正好 1KB）。

**练习 2**：`NUM_ITERATIONS` 在这个微基准里起哪两个作用？

**答案**：其一，把一次「读完 1KB~1MB」的短暂访问重复 10000 遍，把执行时间放大到毫秒量级，让主机端 `std::chrono` 的计时误差相对可忽略；其二，它是主机带宽公式里系数 `0.000010000` 的来源之一（\( 10000 \times 10^{-9} \)，见 4.3.3），改它必须两端同步意识到。

**练习 3**：为什么 `INTERFACE_WIDTH` 用 `#define` 而不是 `const int`？

**答案**：因为它是一个**类型**（`ap_uint<DWIDTH>`），C++ 的 `const` 变量不能当作类型使用，只有宏能在编译前完成文本替换、拼出一个类型名。

### 4.3 运行数据流：从 main() 到内核返回

#### 4.3.1 概念说明

主机程序是整个实验的「导演」：它按固定剧本走完 **初始化 → 逐档负载测量** 两大幕。第一幕做一次（找设备、加载 xclbin、创建内核对象）；第二幕是一个 `payload` 倍增循环，每档负载经历「备数据 → 搬上板 → 计时启动内核 → 等完成 → 算带宽」五步。理解这个流程后，仓库里所有 datacenter 主机程序你都能直接读懂——它们结构完全相同。

#### 4.3.2 核心流程

一次完整运行的时序（编号对应下文源码精读的分组）：

```text
main(argv: xclbin 路径)
 │
 ├─ [A] 初始化（只做一次）
 │    xcl::get_xil_devices()            枚举 Xilinx 设备
 │    xcl::read_binary_file(xclbin)     读入比特流
 │    for 每个设备尝试:
 │        cl::Context / cl::CommandQueue(PROFILING)
 │        cl::Program(bins)              加载 xclbin ← 失败则试下一台设备
 │        for i < NUM_KERNEL:
 │            cl::Kernel("krnl_ubench:{krnl_ubench_1}")   ← 名字与 ini 的 nk 对应
 │
 ├─ [B] 负载扫描：for payload = 256; ≤262144; ×2   （11 档：1KB→1MB）
 │    ├─ dataSize = payload             （仿真模式固定 256）
 │    ├─ 主机缓冲 read_source[dataSize] 随机填充
 │    ├─ [B1] 为 NUM_KERNEL*NUM_PORT 个缓冲构造 cl_mem_ext_ptr_t
 │    │        flags = XCL_MEM_DDR_BANK1               ← 与 ini 的 sp=...DDR[1] 对齐
 │    ├─ [B2] 创建等量 cl::Buffer(CL_MEM_READ_ONLY|EXT_PTR|USE_HOST_PTR)
 │    │        enqueueMigrateMemObjects → q.finish()    （H2D 搬运，不计时）
 │    ├─ [B3] kernel_start = chrono::now()              （计时开始）
 │    ├─        dataSize /= WIDTH_FACTOR                 （int 个数 → 宽元素个数）
 │    ├─ [B4] for i < NUM_KERNEL:
 │    │            setArg(0..NUM_PORT-1, buffer_i*NUM_PORT+j)   挂端口
 │    │            setArg(NUM_PORT, dataSize)                    挂 size
 │    │            q.enqueueTask()                              启动内核
 │    ├─        q.finish()                              （阻塞到内核返回）
 │    ├─ [B5] kernel_end = chrono::now()；算带宽并打印
 │
 └─ return EXIT_SUCCESS
```

内核侧被启动后做的事（其执行与主机 `q.finish()` 并发）：

```text
krnl_ubench(in0, in1, size)
 ├─ #pragma DATAFLOW：下面两段循环尽量并行执行
 ├─ 循环体1：for NUM_ITERATIONS 遍 { for j < size { temp_data_0 = in0[j] } }  ← 走 gmem0
 └─ 循环体2：for NUM_ITERATIONS 遍 { for j < size { temp_data_1 = in1[j] } }  ← 走 gmem1
```

带宽的换算思路：一次内层循环从每个端口读 \( 4p \) 字节，重复 `NUM_ITERATIONS` 遍、共 `NUM_KERNEL×NUM_PORT` 条端口在读，除以耗时 \( t \) 并按 \( 10^9 \) 字节折算成 GB：

\[ \text{bw} = \frac{4p \times \textit{NUM\_ITERATIONS} \times \textit{NUM\_KERNEL} \times \textit{NUM\_PORT}}{t \times 10^{9}} = \frac{4p \times 0.000010000}{t} \times \textit{NUM\_KERNEL} \times \textit{NUM\_PORT} \]

（计时误差的批判留给 u2-l3，本讲先记住公式形态。）

#### 4.3.3 源码精读

**[A] 初始化段**

[host.cpp:30-48](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L30-L48)：检查命令行参数（只收一个 xclbin 路径），随后调用 `xcl::get_xil_devices()` 枚举设备、`xcl::read_binary_file()` 读入比特流——这两个函数来自公共库 [xcl2.hpp:79-82](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.hpp#L79-L82)。

[host.cpp:15-16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L15-L16) 定义本工程的组织参数：`NUM_KERNEL 1`、`NUM_PORT 2`——注意它们**不**在 krnl_config.h 里，是主机私有的；但必须与 ini 的 `nk=1` 和内核的两个端口参数保持一致。

[host.cpp:52-84](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L52-L84)：逐台设备尝试创建 Context/CommandQueue/Program，成功后按 `krnl_ubench:{krnl_ubench_<i+1>}` 的全名创建 Kernel 对象——这个名字正是 ini 里 `nk=krnl_ubench:1` 产生的 CU 名 `krnl_ubench_1`，**主机与 ini 的第二个对齐点**。

**[B] 负载扫描循环头**

[host.cpp:100-104](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L100-L104)：`payload` 从 256 倍增到 262144（256×4B=1KB 起，262144×4B=1MB 止，共 11 档）；仿真模式下 `dataSize` 被钉在 256 以加速。

**[B1]/[B2] 缓冲与 bank 绑定**

[host.cpp:110-128](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L110-L128)：为 `NUM_KERNEL*NUM_PORT = 2` 个缓冲逐个填 `cl_mem_ext_ptr_t`，`flags = XCL_MEM_DDR_BANK1`——与 ini 的 `sp=...:DDR[1]` 遥相呼应（两端都指向 DDR 通道 1）。两个 `if/else` 分支目前内容相同，是留给仿真/真机差异化的骨架。

[host.cpp:133-147](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L133-L147)：创建设备侧 Buffer（只读、使用主机指针、扩展定位），随后 `enqueueMigrateMemObjects` 把数据搬到 FPGA，`q.finish()` 确保搬完——**搬运在计时窗口之外**。

**[B3]/[B4] 计时与启动**

[host.cpp:150-165](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L150-L165) 是全程序最核心的十行：

```cpp
auto kernel_start = std::chrono::high_resolution_clock::now();  // 计时开始

dataSize = dataSize / WIDTH_FACTOR;      // ★ 契约宏在此发挥作用
int i, j = 0;
for (i = 0; i < NUM_KERNEL; i++) {
    for (j = 0; j < NUM_PORT; j++) {
        OCL_CHECK(err, err = cmpt_krnl[i].setArg(j, source_in_buffer[i*NUM_PORT+j]));
    }
    OCL_CHECK(err, err = cmpt_krnl[i].setArg(j, dataSize));   // j 退出循环后 = 2
    OCL_CHECK(err, err = q.enqueueTask(cmpt_krnl[i]));        // 启动！
}
q.finish();                                                    // 等内核返回
```

三个细节值得咀嚼：

- `dataSize / WIDTH_FACTOR`：主机缓冲按 32bit int 计数，内核按 512bit 元素计数，第 155 行完成单位转换——这正是 4.2 讲的契约在运行期的兑现点。
- `setArg` 的参数索引就是内核签名的参数序号：`in0`→0、`in1`→1、`size`→2。内层 `j` 循环退出后恰好等于 `NUM_PORT=2`，于是顺势用它设置 `size`——写法紧凑，但也意味着**改 NUM_PORT 时这段索引逻辑自动适配**。
- `enqueueTask` 只是提交，真正阻塞等内核返回的是后面的 `q.finish()`。

**[B5] 计时与带宽**

[host.cpp:168-173](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L168-L173)：结束计时、按 4.3.2 的公式算带宽并打印。

**内核侧**

[krnl_ubench.cpp:4-12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L4-L12)：签名两个 `volatile INTERFACE_WIDTH*` 指针加一个 `size`；pragma 为每个指针开一个 `m_axi` 端口（`bundle=gmem0/gmem1`、`max_read_burst_length=16`），控制类参数（含指针偏移与返回）走 `s_axilite`。逐条 pragma 的语义在 u2-l1 展开，本讲只需知道「两个指针 = 两条独立内存通道」。

[krnl_ubench.cpp:14-31](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L14-L31)：`DATAFLOW` 让两段循环成为并行进程；每段外层 `NUM_ITERATIONS` 遍、内层 `size` 个宽元素、`PIPELINE II=1` 让读取流水化。两个 `volatile` 临时变量是「防优化」的关键——读到的数据必须有去处，否则 HLS 可能直接把读操作删掉。这些技巧的深入分析是 u3-l1 的主题。

#### 4.3.4 代码实践

**实践：跑通并观察一次启动流程（可选运行 / 阅读型）**

1. **实践目标**：亲眼看到 [A]→[B] 的完整流程在真实输出中的样子。
2. **操作步骤**：
   - **有 Vitis 2020.2 环境**：在本目录执行 `make check TARGET=sw_emu DEVICE=<你的平台>`，观察标准输出（命令细节回顾 u1-l3）。
   - **无环境**：纯阅读 host.cpp 第 30-178 行，按 4.3.2 时序图的编号在代码旁做 [A]/[B1]…[B5] 标注。
3. **需要观察的现象**（有环境时）：
   - `Trying to program device[0]...` 与 `program successful!`（初始化 [A]）；
   - `Creating a kernel [krnl_ubench:{krnl_ubench_1}] for CU(1)`（CU 名与 ini 的 `nk` 对应）；
   - 11 行 `Execution time = ...` 与 `Bandwidth = ...`（负载扫描 [B]，仿真下数值无物理意义，仅确认流程）。
4. **预期结果**：输出条数与结构同上；若某档 `Bandwidth` 异常大，回顾 u1-l3 结论——sw_emu 只验证功能链路。
5. 无硬件环境时本实践为阅读型，运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：计时窗口（`kernel_start` 到 `kernel_end`）内包含哪些**不属于**内核执行的开销？

**答案**：`setArg` 的参数下发、`enqueueTask` 的命令提交、运行时队列调度，以及 `q.finish()` 的轮询/中断返回路径。数据搬运（H2D）刻意排除在外。这些系统性偏差的定量分析与改进方案是 u2-l3、u7-l3 的内容。

**练习 2**：第 173 行打印的 `Payload Size: i*4/(1024.0*1024.0)` 是真实的负载大小吗？请结合此时 `i` 的取值回答。

**答案**：不是。此处 `i` 是第 157 行循环结束后的残留值 `NUM_KERNEL = 1`，所以恒打印 \( 4/1048576 \approx 3.8\times10^{-6} \) MB，与 `payload` 无关。这是仓库的一个已知瑕疵——正确的写法应基于 `payload*4` 计算。它不影响带宽数值本身（带宽用的是 `payload` 变量），只影响日志可读性，但也提醒我们：**读代码时要以变量为准，不能轻信打印**。

**练习 3**：为什么 [B2] 的 `enqueueMigrateMemObjects` 要在计时开始**之前**完成？

**答案**：本基准测的是「内核从 DDR 读数据的带宽」，主机到设备的初始搬运属于实验准备，混入会显著污染小负载档（1KB 档）的测量结果。

## 5. 综合实践

**任务：绘制 read/DDR/2ports_512bit 的完整调用时序图，并标注契约常量的落点。**

这是本讲规格指定的主实践，把 4.1-4.3 串成一张图：

1. **准备**：打开 [host.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp)、[krnl_ubench.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp)、[krnl_config.h](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h) 三份源码（纸笔或任意画图工具均可）。
2. **画图**：以 4.3.2 的文字时序为底稿，画两条生命线（`main()` 与 `krnl_ubench`），补全一条从 `enqueueTask` 到 `q.finish()` 的箭头表示内核执行期，并在内核生命线内画出两个并发的读循环进程。
3. **标注常量落点**（核心步骤）——在图上每个使用处贴标签：
   - `DWIDTH`/`INTERFACE_WIDTH`：内核签名（krnl_ubench.cpp:4）、临时变量（14-15）；
   - `WIDTH_FACTOR`：主机单位换算（host.cpp:155）；
   - `NUM_ITERATIONS`：内核两个外层循环（krnl_ubench.cpp:19、26），以及隐式藏进带宽系数 `0.000010000`（host.cpp:172）；
   - `NUM_KERNEL`/`NUM_PORT`（主机私有）：Kernel 创建循环（73）、缓冲数量（110-133）、setArg/enqueueTask 循环（157-163）、带宽乘子（172）；
   - `XCL_MEM_DDR_BANK1`（host.cpp:119、126）与 ini 的 `sp=...DDR[1]`（ubench.ini:3-4）：在图旁画一条虚线把两处连起来，注明「跨文件对齐」。
4. **自查**：图上每条标注都能指出对应文件与行号，即为完成。若发现某常量你找不到落点，回到 4.2/4.3 的源码精读复核。
5. **预期成果**：一张可存档的时序图。它同时也是你后续学习 u2（编程模型）、u3-l2（手动改参）时的「改动影响地图」——改任何一个参数，看图即知要动哪些文件。

## 6. 本讲小结

- 一个微基准工程是**五件套流水线**：`krnl_config.h`（契约）+ `krnl_ubench.cpp`（内核）+ `host.cpp`（主机）+ `ubench.ini`（物理连线）+ `Makefile`（装配），其余几十个工程同构。
- `krnl_config.h` 是唯一被 `v++` 与 `g++` 两个独立编译目标共享的文件：`DWIDTH` 定端口位宽，`WIDTH_FACTOR`（\( =\textit{DWIDTH}/32 \)）负责 int↔宽元素的单位换算，`NUM_ITERATIONS` 既放大执行时间又隐式出现在带宽公式系数里。
- 运行数据流分两幕：一次性初始化（设备→xclbin→Kernel 对象），以及 `payload` 256→262144 的 11 档倍增扫描，每档经历备数据→H2D（不计时）→`setArg`→`enqueueTask`→`finish`→算带宽。
- 主机与 ini 有**两个必须手工对齐的点**：CU 名（`nk=krnl_ubench:1` ↔ `krnl_ubench:{krnl_ubench_1}`）与内存通道（`sp=...DDR[1]` ↔ `XCL_MEM_DDR_BANK1`）。
- 读代码要带批判性：`Payload Size` 的打印用的是循环残留变量 `i`，与真实负载无关——以变量语义为准，不轻信输出。

## 7. 下一步学习建议

本讲拆的是「骨架」，下一单元进入「血肉」：

- **u2-l1（HLS 内核编程基础）**：逐条解读本讲一笔带过的 `m_axi`（`bundle`/`offset`/`max_read_burst_length`）与 `s_axilite` pragma、`ap_uint` 宽类型、`DATAFLOW`/`PIPELINE II=1` 的语义。
- **u2-l2（主机端编程模型）**：深入 `xcl2.hpp` 封装与 `cl::Context/Program/Kernel/Buffer` 的对象生命周期，把本讲的 [A] 段讲透。
- **u2-l3（测量方法学）**：正面回答本讲埋下的问题——主机计时窗口的误差来源与带宽公式的完整推导。

若想先动手，可直接跳到 u3-l2 的手动改参实验，把本讲的「联动改」认知用于实践。

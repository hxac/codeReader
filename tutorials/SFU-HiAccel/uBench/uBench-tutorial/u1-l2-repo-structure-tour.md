# u1-l2 仓库目录结构导览：三大微基准与案例研究的地图

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 uBench 仓库 `ubench/`、`case_study/`、`common/` 三大目录的树状图，并说出每个二级目录（如 `datacenter`/`embedded`、`read`/`write`、`DDR`/`HBM`）对应的含义。
2. 说出每个微基准工程目录中 `Makefile`、`utils.mk`、`ubench.ini`、`src/` 四类文件各自的职责，以及它们之间的消费关系。
3. 理解「目录名即参数组合」这一仓库组织约定：`read/write` 对应访问类型、`DDR/HBM` 对应内存类型、`2ports_512bit` 对应端口数与位宽、`32bit_per_access` 对应每次访问位宽。
4. 学会用 `find`/`grep` 在仓库中快速定位所有微基准工程，并亲手核对「目录名 ↔ 五参数」的映射关系（包括发现目录名与代码实际参数不一致的地方）。

## 2. 前置知识

本讲建立在 u1-l1 的两个结论之上（不熟悉请先回看）：

- **五因素模型**：影响 FPGA 内存带宽的五个参数——内核频率、并发端口数、端口位宽、最大突发长度、连续访问数据量。前三者决定理论峰值，后两者决定实际利用效率。
- **三类微基准 + 两个案例**：片外带宽、流带宽、片外延迟三类微基准，加上 KNN、SpMV 两个案例研究。

在此之外，本讲只需要两个很基础的概念：

1. **目录树**：用缩进表示的层级结构，例如 `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit` 表示从仓库根出发依次进入 5 层目录。目录的**深度**在本仓库有实际意义——它决定了 Makefile 里指回仓库根的相对路径长度（后面 4.2.3 会看到）。
2. **一个 Vitis 工程的构成**：每个微基准都同时包含「FPGA 侧」（HLS 内核源码，被 `v++` 编译成 `.xo` 再链接成 `.xclbin`）和「主机侧」（C++ OpenCL 程序，被 `g++` 编译成可执行文件）。两类代码放在同一个工程目录里，由同一个 `Makefile` 驱动。理解了这一点，下面看到 `src/` 里同时有 `krnl_*.cpp` 和 `host.cpp` 就不会奇怪。

> 术语提示：**SLR**（Super Logic Region）是 Xilinx 大型 FPGA 的物理分区，U200 有 2 个 SLR、U280 有 3 个；把内核放到靠近某个内存控制器的 SLR 可以缩短布线。本讲只在读配置文件时提到它，细节留到 u3-l3。

## 3. 本讲源码地图

本讲是一张「地图」，涉及的是仓库的组织文件而非算法代码：

| 文件/目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/README.md#L28-L58) | 仓库总入口：声明三类微基准与两个案例研究的定位 |
| [ubench/offchip_bandwidth/datacenter/README.md](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/README.md#L1-L80) | 片外带宽微基准手册：逐条讲解五参数在哪些文件中改 |
| [ubench/streaming_bandwidth/datacenter/README.md](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/README.md#L1-L48) | 流带宽微基准手册 |
| [ubench/offchip_latency/datacenter/README.md](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/README.md#L1-L36) | 片外延迟微基准手册 |
| [ubench/offchip_bandwidth/embedded/README.md](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/embedded/README.md#L1-L41) | 嵌入式（ZCU104）版 auto_collect 参数说明 |
| [case_study/KNN/README.md](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/README.md#L1-L5) | KNN 案例四个设计变体的说明 |
| [case_study/SpMV/README.md](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/README.md#L1-L4) | SpMV 案例四个设计变体的说明 |
| [ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L1-L164) | 本讲的「样板工程」：五件套骨架的解剖对象 |
| [common/includes/xcl2/xcl2.mk:1-4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.mk#L1-L4) 与 [common/utility/makefile_gen/makegen.py:1-55](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/makefile_gen/makegen.py#L1-L55) | 公共主机库（`.mk` 注入机制）与仓库维护工具的代表 |

## 4. 核心概念与源码讲解

### 4.1 顶层目录地图

#### 4.1.1 概念说明

uBench 仓库根目录只有四个条目：

- `ubench/` —— 三类微基准本体，是仓库的核心。
- `case_study/` —— KNN 与 SpMV 两个完整加速器，用来验证微基准洞察对真实设计的作用。
- `common/` —— 从 Xilinx Vitis 示例仓库继承来的公共主机库和工具脚本。
- `README.md` —— 项目说明与论文引用。

这个「微基准 + 案例研究」的双层结构直接对应论文的方法论：先用微基准量出内存系统的性能边界（带宽、延迟随五因素怎么变），再用案例研究证明这些边界可以指导加速器设计。所以在仓库里迷路时，先问自己「我现在在测量还是在应用？」

#### 4.1.2 核心流程

从根 README 出发定位到具体微基准的路径：

1. 根 README 列出三类微基准与两个案例 → 确定你要研究的现象（带宽/流带宽/延迟）。
2. 进入 `ubench/<微基准类型>/` → 选择 `datacenter`（Alveo U200/U280，x86 主机）或 `embedded`（ZCU104，aarch64 主机）。
3. （仅片外带宽）再选 `read`/`write` 与 `DDR`/`HBM` → 落到具体参数组合目录。
4. 目录里的 README（如有）给出手动调参指南；`auto_collect/` 给出自动批量生成入口。

#### 4.1.3 源码精读

根 README 在 [README.md:30-50](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/README.md#L30-L50) 声明了三类微基准，每一段都描述了该微基准扫描的参数维度和示例配置：

- [README.md:32-38](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/README.md#L32-L38)：**Off-chip Memory Bandwidth**——完整扫描五因素；示例为 300MHz、2 端口、512bit、突发 16、连续访问 1KB–1MB。
- [README.md:40-46](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/README.md#L40-L46)：**Streaming Bandwidth**——流是内核到内核的片上直传，不经过内存控制器，所以没有「突发长度」维度，只扫频率、流端口数、位宽、流数据量。
- [README.md:48-50](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/README.md#L48-L50)：**off-chip Memory Latency**——随机访问延迟只取决于位宽与被访问数组的大小，所以只扫这两个维度。
- [README.md:52-58](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/README.md#L52-L58)：**Case Study**——KNN（源自 Rodinia）与 SpMV（源自 MachSuite），各含论文 Table 2 / Table 4 中的四个设计。

据此可以画出完整的目录树（配合 `find` 实测，见 4.1.4）：

```text
uBench/
├── README.md
├── ubench/                          # 三类微基准
│   ├── offchip_bandwidth/           # ① 片外带宽（五因素全扫）
│   │   ├── datacenter/              #    Alveo U200/U280 (x86)
│   │   │   ├── read/DDR/2ports_512bit/    # 样板工程：读 + DDR
│   │   │   ├── read/HBM/2ports_512bit/    # 读 + HBM
│   │   │   ├── write/DDR/2ports_512bit/   # 写 + DDR
│   │   │   ├── write/HBM/2ports_512bit/   # 写 + HBM
│   │   │   └── auto_collect/        #    自动批量生成脚本（Python 2）
│   │   └── embedded/                #    ZCU104 (aarch64)，只有 auto_collect
│   │       └── auto_collect/
│   ├── streaming_bandwidth/         # ② 流带宽（内核间 AXIS 直传）
│   │   ├── datacenter/2ports_512bit/     # 一对 streamWrite/streamRead 内核
│   │   │   └── auto_collect/
│   │   └── embedded/auto_collect/   #    嵌入式版只有自动生成
│   └── offchip_latency/             # ③ 片外延迟（随机访问）
│       ├── datacenter/DDR/32bit_per_access/
│       ├── datacenter/HBM/32bit_per_access/
│       └── embedded/32bit_per_access/    # 唯一带手写示例工程的嵌入式目录
├── case_study/                      # 两个案例 × 各四个设计变体
│   ├── KNN/    (baseline_14PE | suboptimal_14PE | optimal_14PE | aggressive_11PE)
│   └── SpMV/   (baseline_30PE | suboptimal_4PE  | optimal_4PE  | aggressive_4PE)
└── common/                          # Vitis 示例仓库风格的公共设施
    ├── utils.mk
    ├── includes/                    # xcl2/opencl/cmdparser/logger/... 主机库
    └── utility/                     # readme_gen/makefile_gen/md2rst/... 工具
```

三个值得注意的「不对称」，读地图时容易踩坑：

1. **`datacenter` 与 `embedded` 的内容不对称**：片外带宽和流带宽的 `embedded/` 下只有 `auto_collect/`（没有手写示例工程），而片外延迟的 `embedded/32bit_per_access/` 是一个完整的手写工程。所以「嵌入式怎么跑」的手写参考只在延迟这条线上。
2. **README 链接与实际目录大小写/连字符不一致**：[README.md:50](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/README.md#L50) 指向 `ubench/off-chip_latency`，实际目录是 `ubench/offchip_latency`（无连字符）；另外 [README.md:36](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/README.md#L36) 等处的详细指南链接指向 `tree/2020.2/...` 分支而非 `main`。浏览仓库时以实际目录为准。
3. **案例研究目录名后缀即 PE（处理单元/内核实例）数**：`baseline_14PE` 的 `14` 是 `krnl_config.h` 里的 `NUM_KERNEL`；这是「目录名即参数」约定在 `case_study` 中的延伸（4.2.3 会看到源码证据）。

#### 4.1.4 代码实践

**实践目标**：不借助 IDE，用两条命令生成上面那张目录树，并验证三个不对称点。

**操作步骤**：

1. 在仓库根目录执行：

   ```bash
   find ubench -type d | sort
   find case_study -maxdepth 2 -type d | sort
   ```

2. 把输出与 4.1.3 的树状图对照，标出 `datacenter/embedded` 在三条微基准线上的内容差异。
3. 执行 `find ubench -name README.md | sort`，确认每条线的手册文件位置。

**需要观察的现象**：

- `ubench/offchip_bandwidth/embedded/` 下只有 `README.md` 和 `auto_collect/`，没有任何手写工程目录；
- `ubench/offchip_latency/embedded/32bit_per_access/` 下却有 `Makefile`、`src/`、`run_app.sh` 等完整工程文件；
- `case_study` 每个算法下恰好有 4 个 `*_PE` 目录。

**预期结果**：`find ubench -type d` 输出 21 个目录（含 `ubench` 自身），且与树状图逐行吻合。本实践只读不写，随时可做。

#### 4.1.5 小练习与答案

**练习 1**：你的目标是测量 U280 上 HBM 的**读带宽**随突发长度的变化，应该进入哪个目录？如果还想自动扫描，又要去哪里？

答案：手写示例在 `ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/`（改 `krnl_ubench.cpp` 里的 `max_read_burst_length`）；自动扫描入口在 `ubench/offchip_bandwidth/datacenter/auto_collect/`（改 `config.py` 的 `MAX_BURST_LENGTH` 列表，`MEMORY_TYPE` 选 `BANK_TYPE:'HBM'` 那项）。

**练习 2**：为什么流带宽微基准没有 `read/`、`write/`、`DDR/`、`HBM/` 这一层子目录？

答案：流带宽测的是内核到内核的 AXIS 片上直传（`stream_connect`），数据不进不出片外内存，因此不存在访问类型（读/写）和内存类型（DDR/HBM）这两个维度；它的参数维度只有频率、流端口数、位宽、流数据量，所以直接以 `2ports_512bit` 作为工程目录名。

**练习 3**：`case_study/SpMV/baseline_30PE` 里的 `30PE` 是什么参数？在哪核实？

答案：`30` 是该设计中部分 SpMV 内核的实例数，对应 [case_study/SpMV/baseline_30PE/src/krnl_config.h:17](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_config.h#L17) 的 `#define NUM_KERNEL (30)`；「PE」（Processing Element）在本文语境指一个承担部分计算的内核实例。

### 4.2 微基准目录骨架

#### 4.2.1 概念说明

剥掉参数差异后，所有手写微基准工程都是同一副骨架，我们称之为「五件套」：

| 文件 | 角色 | 被谁消费 |
| --- | --- | --- |
| `src/krnl_config.h` | 参数头：位宽 `DWIDTH`、迭代次数 `NUM_ITERATIONS` 等，**内核与主机共用** | 两边都 `#include` |
| `src/krnl_*.cpp` | HLS 内核源码（FPGA 侧） | `v++ -c` 编译成 `.xo` |
| `src/host.cpp` | OpenCL 主机程序（CPU 侧） | `g++` 编译成可执行文件 |
| `ubench.ini` | 链接期连接配置：SLR 放置、端口-内存 bank 映射、内核实例数 | `v++ -l --config` |
| `Makefile` + `utils.mk` | 构建脚本 + 环境检查片段 | `make` |

`Makefile` 与 `utils.mk` 的分工：`Makefile` 描述「编译什么、链接什么」，`utils.mk` 描述「编译前的环境合法性」（Vitis/XRT 环境变量、交叉编译工具链、平台名检查），是每个工程目录里紧挨着 `Makefile` 的那个本地片段（注意不要与仓库根的 `common/utils.mk` 混淆，后者是全局共享版本）。

#### 4.2.2 核心流程

一次 `make all` 在这个骨架里发生的事情（构建细节在 u1-l3 展开）：

1. `utils.mk` 检查 `XILINX_VITIS`、`XILINX_XRT`、`DEVICE` 等是否就绪，不满足直接 `$(error)` 退出。
2. `v++ -c` 把 `src/krnl_*.cpp`（内含 `krnl_config.h`）编译为 `.xo` 内核对象。
3. `v++ -l --config ./ubench.ini` 把 `.xo` 链接成 `.xclbin`，`ini` 里的 `slr`/`sp`/`nk` 在这一步生效。
4. `g++` 把 `src/host.cpp` 与 `common/` 里的公共库（xcl2 等）编译成主机可执行文件 `ubench`。
5. 运行时主机加载 `.xclbin`、分配绑 bank 的缓冲、启动内核并计时。

#### 4.2.3 源码精读

**（a）参数头——五因素中「位宽」的落点。** 以样板工程为例，[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h:1-7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L1-L7) 定义了 `DWIDTH = 512`、`INTERFACE_WIDTH = ap_uint<512>`、`WIDTH_FACTOR = 512/32 = 16`（一个宽端口拍等价于 16 个 32bit 整数）和 `NUM_ITERATIONS = 10000`（放大内核执行时间便于计时）。目录名里的 `512bit` 就落在 `DWIDTH` 上。同一头文件被内核和主机同时包含，所以改一处两端同步。

**（b）连接配置——「内存类型」与「内核实例数」的落点。** DDR 版 [ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini:1-6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L1-L6) 把内核放进 `SLR1`、两个端口 `in0`/`in1` 都接到 `DDR[1]`、声明 1 个内核实例（`nk=krnl_ubench:1`）。HBM 版 [ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/ubench.ini:1-6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/ubench.ini#L1-L6) 结构完全相同，只是 bank 换成 `HBM[0]`、SLR 换成 `SLR0`——**目录名里的 `DDR`/`HBM` 就落在这两行 `sp=` 上**。

**（c）Makefile——工程深度决定回根路径。** [ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile:28-31](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L28-L31) 用 `COMMON_REPO = ../../../../../` 指回仓库根（该目录深 5 层）；对比流带宽工程 [ubench/streaming_bandwidth/datacenter/2ports_512bit/Makefile:29](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/Makefile#L29) 是 `../../../`（深 3 层）、延迟工程 [ubench/offchip_latency/datacenter/DDR/32bit_per_access/Makefile:29](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/Makefile#L29) 是 `../../../../`（深 4 层）。**移动/复制工程目录时这行必须同步改**，否则找不到 `common/` 里的库。[Makefile:47-54](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L47-L54) 随后 include 两个公共片段（opencl.mk、xcl2.mk）把头文件路径和库注入编译选项；[Makefile:73](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L73) 的 `LDCLFLAGS += --config ./ubench.ini` 则把（b）中的连接配置交给链接器。

**（d）utils.mk——环境门卫。** [ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk:6-11](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L6-L11) 提供 `PROFILE := yes` 时追加 `--profile_kernel` 的开关；[utils.mk:28-31](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L28-L31) 检查 `XILINX_VITIS`；[utils.mk:41-43](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L41-L43) 定义 `check-xrt` 目标检查 `XILINX_XRT`；它还调用 `common/utility/parse_platform_list.py` 解析平台名（[utils.mk:14](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L14)）——这是微基准工程与 `common/utility/` 之间最直接的一条依赖线。

**（e）骨架的三种变体。** 五件套是共同抽象，三条微基准线各有替换件：

- **流带宽**：内核换成成对的 `krnl_streamWrite`/`krnl_streamRead`（[src/krnl_streamWrite.cpp:3-9](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/src/krnl_streamWrite.cpp#L3-L9) 的函数签名里出现两个 `hls::stream<pkt>&`，配 `#pragma HLS INTERFACE axis`）；参数头多了 `typedef ap_axiu<DWIDTH,0,0,0> pkt`（[src/krnl_config.h:9](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/src/krnl_config.h#L9)）；`ini` 里多了 `stream_connect` 行把写内核的输出流接到读内核的输入流（[ubench.ini:8-9](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/streaming_bandwidth/datacenter/2ports_512bit/ubench.ini#L8-L9)）。
- **片外延迟**：内核签名多出 `int* in0_index` 下标数组端口（[src/krnl_ubench.cpp:4-7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_ubench.cpp#L4-L7)），片上开 `local_in0_index[524288]` 缓存随机下标（[src/krnl_ubench.cpp:13](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_ubench.cpp#L13)）；目录名 `32bit_per_access` 对应其参数头 `DWIDTH = 32`。
- **嵌入式（ZCU104）**：唯一的手写嵌入式工程 `ubench/offchip_latency/embedded/32bit_per_access/` **不用**上述 `common/` 模板，而是自包含 Makefile 直接驱动 `v++ -c/-l/-p` 打包 `sd_card.img`（[Makefile:2-27](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/Makefile#L2-L27)），连接配置改放在 [src/zcu104.cfg:8-11](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/zcu104.cfg#L8-L11)（`sp=` 的目标是 Zynq 的 `HP0/HP1/HP2` 端口而非 `DDR[n]` bank），`src/` 里还多了 `host.h`、`my_timer.h`，目录下多了 `run_app.sh`（板上运行脚本）与 `xrt.ini`。

**（f）案例研究的同构性。** `case_study` 下每个变体也是同一副五件套，只是 `ini` 改名为 `knn.ini`/`spmv.ini`、内核改名为 `krnl_partialKnn`/`krnl_globalSort`/`krnl_partialspmv`。例如 [case_study/SpMV/baseline_30PE/src/krnl_config.h:17-25](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_config.h#L17-L25) 集中定义了 `NUM_KERNEL(30)`、矩阵规模 `N(8400)`、`ROWS_PER_TILE(8)`、`UNROLL_FACTOR(1)`；KNN 的 [case_study/KNN/baseline_14PE/src/krnl_config.h:8-12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_config.h#L8-L12) 定义 `DWIDTH = 32` 与 `NUM_KERNEL = 14`。你在本讲学到的骨架阅读法可以无缝迁移。

**（g）自动生成线。** `auto_collect/` 目录（片外带宽数据中心的在 [ubench/offchip_bandwidth/datacenter/auto_collect/config.py:7-14](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/config.py#L7-L14)）用七个 Python 列表/字典定义五因素的参数空间（`KERNEL_FREQ`、`NUM_CONCURRENT_PORT`、`PORT_WIDTH`、`MAX_BURST_LENGTH`、`CONSECUTIVE_DATA_SIZE`、`ACCESS_TYPE`、`MEMORY_TYPE`），由四个 `*_gen.py` 生成器按交叉积产出成批的「五件套」工程。也就是说：**手写工程是生成器的模板原型，生成器是手写工程的参数化复制**。细节在单元 5 展开。

#### 4.2.4 代码实践

**实践目标**：验证「目录名即参数」约定，并亲身发现一处**目录名与代码不一致**的案例——这正是本讲规格里「检验目录命名与参数的对应关系」的核心。

**操作步骤**：

1. 对比读、写两个 DDR 工程的端口实现方式：

   ```bash
   grep -n 'INTERFACE m_axi' \
     ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp \
     ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp
   grep -n 'NUM_KERNEL\|NUM_PORT' \
     ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp \
     ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp
   cat ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/ubench.ini
   ```

2. 记录两边「2 个并发端口」分别是怎么实现的。

**需要观察的现象**：

- 读版内核签名是 `(...in0, ...in1, const int size)`，两条 `m_axi` pragma（`bundle=gmem0/gmem1`），主机 `NUM_KERNEL 1`、`NUM_PORT 2`，`ini` 中 `nk=krnl_ubench:1`；
- 写版内核签名只有 `(...out0, const int size)`，只有一条 `m_axi`（见 [write/DDR/src/krnl_ubench.cpp:4-5](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp#L4-L5)），主机 `NUM_KERNEL 2`、`NUM_PORT 1`（[write/DDR/src/host.cpp:15-16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L15-L16)），`ini` 中 `nk=krnl_ubench:2`。

**预期结果**：两个目录都叫 `2ports_512bit`，且都真的有 2 个并发 DDR 端口，但**实现路径不同**——读版是「1 个内核 × 2 个端口」，写版是「2 个内核实例 × 各 1 个端口」。总并发度相同（\(\text{NUM\_KERNEL} \times \text{NUM\_PORT} = 2\)），所以目录名只约束乘积，不约束分解方式。这说明读地图时目录名能告诉你「测什么参数」，但「怎么测的」必须回到 `src/` 与 `ini` 核实。本实践只读不写，随时可做。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/` 整个复制到 `ubench/offchip_bandwidth/datacenter/` 下改名，`Makefile` 里哪一行必须改？改成什么？

答案：`COMMON_REPO` 那行（原为五层 `../../../../../`，[Makefile:29](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L29)）。新位置只深三层，应改为 `../../../`，否则 include 不到 `common/includes/` 下的 `opencl.mk` 和 `xcl2.mk`。

**练习 2**：datacenter 版带宽 README 让你「改端口位宽就更新 `krnl_ubench.h` 里的 `DWIDTH`」，但你在这个工程里找不到 `krnl_ubench.h`，为什么？

答案：README 的说法与实际文件名不符——示例工程中该头文件实际叫 `src/krnl_config.h`（见 [ubench/offchip_bandwidth/datacenter/README.md:33-37](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/README.md#L33-L37) 的说法与 [src/krnl_config.h:4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L4) 的实况）。这是仓库文档滞后于代码的又一例，读 README 时要以 `src/` 为准。

**练习 3**：`ubench.ini`、`knn.ini`、`spmv.ini`、`zcu104.cfg`、`xrt.ini` 都是 `.ini/.cfg` 文件，它们扮演的角色相同吗？

答案：不同。前三者是 **Vitis 链接期连接配置**（`v++ -l --config`，内容是 `slr`/`sp`/`nk`/`stream_connect`）；`zcu104.cfg` 是嵌入式工程的等价物，但把平台名、时钟频率（150MHz）、连接、profile 开关合并在一个文件里（[zcu104.cfg:1-14](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/zcu104.cfg#L1-L14)）；`xrt.ini` 则是 **运行期 XRT 运行时配置**（打开 `profile`/`timeline_trace` 等），与编译链接无关。

### 4.3 common 公共库与工具

#### 4.3.1 概念说明

`common/` 是从 Xilinx Vitis 官方示例仓库（Vitis_Examples）继承下来的公共设施，分两块：

- **`common/includes/`** —— 7 个可复用的主机侧 C++ 库，每个库一个目录，目录里除了 `.h/.cpp` 还有一个同名 `.mk` 片段；
- **`common/utility/`** —— 仓库维护用的脚本（README 生成、Makefile 生成、格式检查、平台列表解析等）。

对 uBench 而言，`includes/` 里真正被微基准工程用到的是 `xcl2` 和 `opencl` 两个；`cmdparser`、`logger`、`oclHelper`、`bitmap`、`lodepng`、`simplebmp` 是 Vitis 示例生态的通用件（命令行解析、日志、图像读写），uBench 的微基准并不使用，但保留它们让仓库与 Vitis 示例的构建约定保持一致。

#### 4.3.2 核心流程

`.mk` 片段机制是理解 `common/` 的钥匙：

1. 每个库目录里的 `.mk` 只定义若干以库名为前缀的变量（如 `xcl2_SRCS`、`xcl2_CXXFLAGS`），不直接产生构建动作；
2. 工程 `Makefile` 用 `include` 把片段引进来，再把变量累加到全局的 `HOST_SRCS`/`CXXFLAGS`/`LDFLAGS`；
3. 这样「链接一个库」就被拆成「给三个变量加值」，工程侧不需要知道库文件的具体路径。

#### 4.3.3 源码精读

**（a）xcl2 的注入方式。** [common/includes/xcl2/xcl2.mk:1-4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.mk#L1-L4) 定义了 `xcl2_SRCS`（指向 `xcl2.cpp`）、`xcl2_HDRS` 和 `xcl2_CXXFLAGS`（头文件搜索路径）。样板工程在 [Makefile:47-51](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L47-L51) include 它并把这三个变量并入全局——`host.cpp` 里那些 `xcl::get_xil_devices()`、`xcl::read_binary_file()` 便捷函数就来自这个库（精读在 u2-l2）。

**（b）opencl 片段解析安装路径。** [common/includes/opencl/opencl.mk:1-16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/opencl/opencl.mk#L1-L16) 根据 `XILINX_XRT`（或交叉编译时的 `SYSROOT`）推出 OpenCL 头文件与库的位置，产出 `opencl_CXXFLAGS`/`opencl_LDFLAGS`（`-lOpenCL -lpthread`）。注意它读的正是 `utils.mk` 里 `check-xrt` 检查的那个环境变量——检查与使用构成闭环。

**（c）utility 的实际消费者。** 仓库维护脚本大多服务于「Xilinx 示例仓库规范」而非微基准本身，但有一个例外你在 4.2.3 已经见过：每个工程的 `utils.mk` 都调用 [common/utility/parse_platform_list.py:1-13](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/parse_platform_list.py#L1-L13)（调用点在 [utils.mk:14](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L14)）把 `DEVICE` 平台名换算成文件系统友好的名字。其余工具按目录归类：`readme_gen/`（由 `description.json` 生成 README，核心是 `readme_gen.py`，另有 `gs_summary*.py` 汇总指南）、`makefile_gen/`（`makegen.py`/`descgen.py` 批量生成各示例的 Makefile）、`md2rst/`（Markdown 转 reStructuredText）、以及一批 `check_*.sh|py`（许可证、README、Makefile、JSON 规范检查）和 `device_list.py`、`create_catalog.py`、`Consolidation.py`、`build_what.sh`。此外 [common/utils.mk](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utils.mk#L1-L11) 是仓库级共享的检查/工具函数库，本仓库各工程实际使用的是工程内那份 `utils.mk`，两者内容同源。

#### 4.3.4 代码实践

**实践目标**：亲眼看清「include 一个 `.mk` = 给变量加值」的注入机制，画出样板工程的 include 依赖链。

**操作步骤**：

1. 在仓库根执行：

   ```bash
   cat common/includes/xcl2/xcl2.mk
   grep -n 'include\|xcl2_\|opencl_' \
     ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile
   ```

2. 用纸笔画出依赖链：`工程 Makefile` → `./utils.mk`（本地检查）+ `common/includes/opencl/opencl.mk` + `common/includes/xcl2/xcl2.mk`（变量注入）→ 全局 `CXXFLAGS/LDFLAGS/HOST_SRCS`。
3. 顺手验证「哪些库没被用」：

   ```bash
   grep -rn 'cmdparser\|lodepng' ubench/ case_study/ --include='*.mk' --include='Makefile' | head
   ```

**需要观察的现象**：

- `xcl2.mk` 全文只有三行变量定义，没有任何构建规则；
- 工程 `Makefile` 的 include 行（L37、L47、L48）之后紧跟 `CXXFLAGS += $(xcl2_CXXFLAGS)` 等累加语句；
- 第 3 步的 grep 在 `ubench/`、`case_study/` 的构建文件里找不到 `cmdparser`/`lodepng` 的引用。

**预期结果**：依赖链与 4.3.2 描述一致；`cmdparser`、`lodepng` 等库确认是「随仓库继承但未被微基准使用」的通用件。本实践只读不写。

#### 4.3.5 小练习与答案

**练习 1**：为什么 uBench 的工程 `Makefile` 不直接写 `-I../../../../../common/includes/xcl2`，而要通过 `xcl2.mk` 片段？

答案：片段把「库在哪、要加哪些编译/链接选项」封装在库自己的目录里，工程只引用变量名。库路径或选项变化时只改一处 `.mk`，所有工程自动生效；同时多个工程累加多个片段不会互相干扰（各有命名前缀）。这是 Vitis 示例生态的通用约定。

**练习 2**：想确认 `xcl::get_xil_devices` 这个函数的实现在哪，应该怎么找？

答案：它是 `common/includes/xcl2/` 库的函数，声明在 `xcl2.hpp`、实现在 `xcl2.cpp`。可用 `grep -n 'get_xil_devices' common/includes/xcl2/xcl2.hpp` 定位声明；其行为精读安排在 u2-l2。

**练习 3**：`common/utility/readme_gen/readme_gen.py` 这类工具和微基准的构建运行有关系吗？

答案：没有直接关系。它们是仓库维护工具（从 `description.json` 等元数据自动生成/刷新各目录的 README），服务于 Xilinx 示例仓库的文档规范；微基准的构建运行链路只依赖 `common/includes/`（xcl2、opencl）和 `common/utility/parse_platform_list.py`。

## 5. 综合实践

**任务：全仓库微基准普查——统计、映射、找茬。** 这是本讲规格指定的综合实践，把 4.1–4.3 的三张地图合成一份可核查的清单。

**实践目标**：用 `find`/`grep` 统计全仓库的微基准工程，建立「目录名 → 五参数」映射表，并核对目录名与代码实况的一致性。

**操作步骤**：

1. 统计两类标志文件的数量与分布：

   ```bash
   find ubench -name 'krnl_ubench.cpp' | sort
   find ubench -name 'ubench.ini' | sort
   ```

2. 对每个查到的工程，抽取五个参数的证据（示例，针对 read/DDR）：

   ```bash
   grep -n 'DWIDTH'  <工程>/src/krnl_config.h                 # 位宽
   grep -n 'm_axi'   <工程>/src/krnl_ubench.cpp               # 端口数 + 突发长度
   grep -n 'NUM_KERNEL\|NUM_PORT' <工程>/src/host.cpp         # 内核×端口分解
   grep -n 'payload(' <工程>/src/host.cpp                     # 连续数据量扫描范围
   grep -n 'sp=\|nk=' <工程>/ubench.ini                       # 内存类型与实例数
   ```

3. 把证据填进映射表（下表给出参考答案，`—` 表示该维度不适用）：

   | 工程（相对 `ubench/`） | 访问类型 | 内存/bank | 端口实现 | 位宽 | 突发 | 连续数据量 | 频率 |
   | --- | --- | --- | --- | --- | --- | --- | --- |
   | offchip_bandwidth/.../read/DDR/2ports_512bit | RD | DDR[1] | 1 核 × 2 口 | 512 | 16（read） | 256→262144（1KB→1MB） | README 称 300MHz；Makefile 未显式传 `--kernel_frequency` |
   | offchip_bandwidth/.../read/HBM/2ports_512bit | RD | HBM[0] | 1 核 × 2 口 | 512 | 16 | 同上 | 同上 |
   | offchip_bandwidth/.../write/DDR/2ports_512bit | WR | DDR[0] | 2 核 × 1 口 | 512 | 16（write） | 同上 | 同上 |
   | offchip_bandwidth/.../write/HBM/2ports_512bit | WR | HBM[0] | 2 核 × 1 口 | 512 | 16 | 同上 | 同上 |
   | streaming_bandwidth/datacenter/2ports_512bit | 流（AXIS） | —（片上直传） | 1 对内核 × 2 流 | 512 | — | 256→262144*4，再乘 `NUM_ITERATIONS` 放大 | 同 read |
   | offchip_latency/datacenter/DDR/32bit_per_access | 随机 RD | DDR[0] | 1 核 × 1 数据口（+1 下标口） | 32 | 2 | 16→262144（64B→1MB） | — |
   | offchip_latency/datacenter/HBM/32bit_per_access | 随机 RD | HBM[0] | 同上 | 32 | 2 | 同上 | — |
   | offchip_latency/embedded/32bit_per_access | 随机 RD | HP0/HP1/HP2（cfg） | 同上（+`sum` 口） | 32 | 2 | 同上 | cfg 中 150MHz |

4. 标注所有「目录名 ↔ 代码」不一致处（找茬）。

**需要观察的现象**：

- 第 1 步两个 `find` 各命中 **7 个**文件：7 个 `krnl_ubench.cpp` 分布为片外带宽 4 个（read/write × DDR/HBM）+ 延迟 3 个（DDR/HBM datacenter + embedded）；7 个 `ubench.ini` 分布为片外带宽 4 个 + 延迟 datacenter 2 个 + 流带宽 1 个。两者集合并不重合——嵌入式延迟工程用 `src/zcu104.cfg` 顶替了 `ubench.ini`，而流带宽工程没有 `krnl_ubench.cpp`（内核叫 `krnl_streamRead/Write.cpp`）。
- 第 3 步表格中 write 版与 read 版端口分解不同（4.2.4 已核）；延迟版 payload 实际从 16（64B）起步（[host.cpp:99](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp#L99)），而其 README 代码示例写的是从 256 起步（[README.md:12-15](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/README.md#L12-L15)）。
- 手写工程 `Makefile` 均无 `--kernel_frequency`；只有 `auto_collect/makefile_gen.py` 生成的 Makefile 和 `case_study/SpMV` 的 Makefile 显式传了 `--kernel_frequency 300`。

**预期结果**：得到一张 7 行映射表；确认「目录名约束的是参数组合的乘积语义，具体分解（核数×端口数）与扫描起点以 `src/`、`ini` 代码为准」；至少找到 3 处文档/目录名与代码不一致（write 的端口分解、延迟 payload 起点、频率是否显式设置）。全部步骤只读不写，无需 Vitis 环境；若对某工程想进一步验证行为，标注「待本地验证」即可。

## 6. 本讲小结

- 仓库 = `ubench/`（三类微基准 × datacenter/embedded）+ `case_study/`（KNN、SpMV 各四变体）+ `common/`（Vitis 示例式公共设施）；目录名即参数组合（`read/write`、`DDR/HBM`、`2ports_512bit`、`32bit_per_access`、`*_PE`）。
- 每个手写微基准工程都是「五件套」骨架：`src/krnl_config.h`（参数头，两侧共用）、`src/krnl_*.cpp`（HLS 内核）、`src/host.cpp`（OpenCL 主机）、`ubench.ini`（SLR/bank/实例数连接配置）、`Makefile` + `utils.mk`（构建与环境检查）。
- 工程 `Makefile` 通过 `COMMON_REPO` 相对路径指回仓库根（层数 = 目录深度），并 include `common/includes/opencl|opencl/xcl2` 的 `.mk` 片段为编译变量注入库；`v++ -l --config ./ubench.ini` 让连接配置在链接期生效。
- 三条微基准线在骨架上做替换：流带宽换成成对 stream 内核 + `stream_connect`；延迟版多出下标数组端口与片上下标缓存；嵌入式 ZCU104 工程则是自包含 Makefile + `zcu104.cfg`（HP 端口）+ `run_app.sh`。
- `auto_collect/` 的 `config.py` 定义七维参数空间，四个 `*_gen.py` 生成器按交叉积批量产出同样的五件套——手写工程是模板原型，生成器是参数化复制。
- 读地图的两条纪律：目录名告诉你「测什么」，`src/` 与 `ini` 才告诉你「怎么测」；仓库存在文档滞后（README 提到的 `krnl_ubench.h` 实为 `krnl_config.h`、根 README 的 `off-chip_latency` 链接与实际目录名不符），一切以代码为准。

## 7. 下一步学习建议

下一讲 **u1-l3 环境搭建与构建运行** 将把本讲的静态地图变成动态流程：精读 `Makefile` 的 `build/check/sd_card` 目标与 `utils.mk` 的平台检查，理解 `sw_emu/hw_emu/hw` 三种 `TARGET` 的区别，并在无真机条件下走通 `make check TARGET=sw_emu` 的仿真路径。在那之前，建议你先自己通读一遍 [ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L1-L164) 的 `all/build/check/sd_card` 四个目标，遇到不熟悉的变量（`XSA`、`TEMP_DIR`、`EMCONFIG_DIR`）先猜再验证——这正是下一讲的入口。随后 u1-l4 将解剖这个样板工程的五件套协作时序。

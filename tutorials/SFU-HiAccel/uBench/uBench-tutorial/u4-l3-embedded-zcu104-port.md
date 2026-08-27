# 嵌入式平台移植：ZCU104 上的带宽与延迟测试

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出嵌入式（ZCU104，aarch64）微基准工程在 `host.h`、`my_timer.h`、`run_app.sh`、`xrt.ini` 四个文件上的结构，以及它们与数据中心版本 `host.cpp` 的差异（设备发现、xclbin 加载、缓冲分配、计时方式）。
2. 理解嵌入式 Makefile 的 `sd_card` 打包流程：交叉编译 `app.exe` → `v++ -c/-l` 生成 xclbin → `v++ -p` 把 BOOT.BIN、Image、rootfs 与应用打成 `sd_card.img`，以及板上 `run_app.sh` 的启动序列。
3. 解释 ZCU104 的 HP/HPC 端口模型与 Alveo 的 DDR/HBM bank 模型在配置上的差别：`sp=` 行的目标从 `DDR[0]`/`HBM[n]` 变成 `HP0`/`HPC0` 这样的 PS 端口名，主机端则完全不需要 `cl_mem_ext_ptr_t` bank 绑定。

本讲以 `ubench/offchip_latency/embedded/32bit_per_access` 为主要解剖对象，它是仓库中结构最完整的嵌入式手写工程。

## 2. 前置知识

**ZCU104 与 Zynq UltraScale+ MPSoC。** ZCU104 是 Xilinx 的评估板，核心是一颗 Zynq UltraScale+ MPSoC（ZU7EV）芯片。这类芯片把两类东西封进同一块硅片：

- **PS（Processing System，处理系统）**：一组 ARM Cortex-A53 核心，外接板上的 DDR4 内存，运行 PetaLinux 等嵌入式 Linux。
- **PL（Programmable Logic，可编程逻辑）**：传统的 FPGA 结构，我们用 Vitis HLS 综合出的内核就放在这里。

**PS 与 PL 如何共享内存？** PL 里的内核若要访问 DDR，必须穿过 PS 提供的一组 AXI 从机端口（slave port）。其中性能最高的是：

- **HP 端口**（High Performance，HP0–HP3）：高性能、非 cache 一致，数据宽度最高 128 bit。
- **HPC 端口**（High Performance Coherent，HPC0/HPC1）：与 HP 类似，但经过 CCI 互联，可与 ARM 核保持 cache 一致。

这就是本讲标题里「HP/HPC 端口模型」的含义——在 Alveo 卡上，内核的 m_axi 端口直连 FPGA 侧的 DDR/HBM 控制器，用 bank 名（`DDR[0]`、`HBM[15]`）寻址；在 ZCU104 上，「片外内存」是 PS 侧那根与 ARM 共享的 DDR4，内核端口要先接到 HP/HPC 端口，再经 PS 内部互联到达 DDR 控制器。

**交叉编译与 SD 卡启动。** 嵌入式 Linux 不是从硬盘启动的：我们把引导文件（BOOT.BIN）、Linux 内核镜像（Image）、根文件系统（rootfs.ext4）和应用文件写进 SD 卡，板卡上电后依次加载。可执行文件 `app.exe` 必须用 aarch64 交叉编译器（在 x86 主机上生成 ARM64 代码）编译，这解释了 Makefile 里对 `SDKTARGETSYSROOT` 的检查。

**承接前面几讲。** 本讲假设你已读过：

- u1-l3：TARGET（sw_emu/hw_emu/hw）、DEVICE、`v++ -c/-l` 两步编译、`emconfig.json` 的作用。
- u3-l1：m_axi 端口、bundle、volatile 防优化、`max_read_burst_length`。
- u3-l3：`ubench.ini` 的 `slr/sp/nk` 三条指令与主机 `cl_mem_ext_ptr_t` 的配合。
- u4-l2：延迟微基准的「洗牌下标表 + 串行依赖链」机制。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ubench/offchip_latency/embedded/32bit_per_access/src/host.cpp` | 嵌入式主机程序：平台遍历式设备发现、map/unmap 缓冲、timespec 计时 |
| `ubench/offchip_latency/embedded/32bit_per_access/src/host.h` | 嵌入式主机的 OpenCL 头封装（cl2.hpp + aligned_allocator），代替 datacenter 版的 `xcl2.hpp` |
| `ubench/offchip_latency/embedded/32bit_per_access/src/my_timer.h` | 自定义 timespec 计时器（tic/toc），代替 datacenter 版的 `std::chrono` |
| `ubench/offchip_latency/embedded/32bit_per_access/src/krnl_ubench.cpp` | 嵌入式延迟内核：多一个 `sum` 输出端口，突发参数与 datacenter 版不同 |
| `ubench/offchip_latency/embedded/32bit_per_access/src/krnl_config.h` | 契约头：DWIDTH=32、NUM_ITERATION=10000（注意是单数拼写） |
| `ubench/offchip_latency/embedded/32bit_per_access/src/zcu104.cfg` | 嵌入式连接配置：平台名、150MHz 时钟、sp= 连到 HP0/HP1/HP2（代替 `ubench.ini`） |
| `ubench/offchip_latency/embedded/32bit_per_access/Makefile` | 自包含构建脚本：交叉编译 + `v++ -p` 打包 sd_card.img |
| `ubench/offchip_latency/embedded/32bit_per_access/run_app.sh` | 板上运行脚本：挂载 SD、写 /etc/xocl.txt、设环境变量、启动 app.exe |
| `ubench/offchip_latency/embedded/32bit_per_access/xrt.ini` | 运行时调试配置：打开 profile 与 timeline_trace |
| `ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp` | datacenter 对照组：xcl2 设备发现、bank 绑定、chrono 计时、带宽公式 |
| `ubench/offchip_bandwidth/embedded/README.md` | 嵌入式带宽线的参数说明：PORT_NAMES 用 HP/HPC 名、频率 150MHz |
| `ubench/offchip_bandwidth/embedded/auto_collect/config.py` | 嵌入式 auto_collect 参数空间（HP/HPC 模型的脚本侧落点） |
| `ubench/offchip_bandwidth/embedded/auto_collect/connectivity_gen.py` | 生成 `zcu104.cfg` 的脚本：sp 行按端口表拼 HP/HPC 名 |

一句话概括整体差异：**datacenter 工程是「Makefile + utils.mk 引用 common 仓库 + x86 本地运行」，embedded 工程是「自包含 Makefile + 交叉编译 + 打 SD 卡上板运行」**；配置文件从 `ubench.ini` 换成带平台名的 `zcu104.cfg`，主机侧从 `xcl2.hpp` 换成 `host.h + my_timer.h`。

## 4. 核心概念与源码讲解

### 4.1 嵌入式主机框架

#### 4.1.1 概念说明

datacenter 版主机（u2-l2 精读过）依赖仓库 common 的 `xcl2.hpp` 提供 `get_xil_devices`、`read_binary_file`、`aligned_allocator` 等封装，并用 `cl_mem_ext_ptr_t` 把缓冲绑到指定 DDR/HBM bank——因为 x86 主机内存与 FPGA 卡上内存物理分离，必须显式声明「数据放哪一侧、哪个 bank」。

嵌入式平台没有这个问题：ARM 核与 PL 共享同一根 PS DDR（统一内存），OpenCL 运行时分配的设备缓冲天然就在 PL 可达的内存里，**bank 绑定整块代码消失**。于是作者直接从 Vitis 官方 vadd 示例裁了一个更朴素的主机骨架：

- `host.h` 承担 `xcl2.hpp` 的最小子集：OpenCL 头与版本宏、`aligned_allocator`。
- `my_timer.h` 承担计时：`tic()`/`toc()` 基于 `clock_gettime(CLOCK_REALTIME)`。
- `host.cpp` 自己写平台遍历、用 `ifstream` 读 xclbin、用 `enqueueMapBuffer`/`enqueueUnmapMemObject` 管理 host 缓冲。

值得提前指出：这个 `host.cpp` 的版权头写着 "VITIS vector addition"，变量名 `krnl_vector_add` 也是 vadd 遗留——阅读真实仓库时要能识别这类模板痕迹，避免把示例遗留当成刻意设计。

#### 4.1.2 核心流程

嵌入式主机的执行流程：

```text
main(argv[1] = xclbin 文件名)
  ├─ 1. 设备发现：cl::Platform::get 枚举所有平台
  │      找 CL_PLATFORM_NAME == "Xilinx" 的平台
  │      取其第 0 个 CL_DEVICE_TYPE_ACCELERATOR 设备
  ├─ 2. 创建 Context 与 CommandQueue（CL_QUEUE_PROFILING_ENABLE）
  ├─ 3. ifstream 读 xclbin 到 char* buf → cl::Program::Binaries → cl::Program
  ├─ 4. cl::Kernel(program, "krnl_ubench")
  └─ 5. for payload in [256, 512, ..., 262144]:        # 11 档，1KB→1MB
        ├─ 建 buffer_a（数据）/ buffer_b（下标）/ buffer_sum（结果）
        ├─ setArg(0..3)                                # in0, in0_index, size, sum
        ├─ enqueueMapBuffer 填数据：ptr_a=rand()，ptr_b=0..N-1 后 random_shuffle
        ├─ enqueueMigrateMemObjects({a,b})
        ├─ tic() → enqueueTask → finish() → toc() 打印 "Execution time"
        └─ enqueueUnmapMemObject × 3
```

与 datacenter 版的关键分叉只在 1、3 和缓冲管理方式上，计时窗口的语义则完全相同（都包含 `enqueueTask` 的启动开销，见 u2-l3 的分析）。

#### 4.1.3 源码精读

**(1) 设备发现：平台名遍历代替 `xcl::get_xil_devices`。**

[ubench/offchip_latency/embedded/32bit_per_access/src/host.cpp:82-100](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/host.cpp#L82-L100)：这段代码先 `cl::Platform::get(&platforms)` 拿到全部 OpenCL 平台，逐个比较 `CL_PLATFORM_NAME` 是否等于字符串 `"Xilinx"`，命中后取该平台下第一个 `CL_DEVICE_TYPE_ACCELERATOR` 设备并 `break`。对照 datacenter 版的 [host.cpp:43](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp#L43)（`xcl::get_xil_devices()` 一行完成同样的事）——嵌入式版把封装展开写了一遍，功能等价，但没有「逐卡重试编程」的循环（datacenter 版有 `valid_device` 计数与失败退出）。

**(2) xclbin 加载：手工 `ifstream` 代替 `xcl::read_binary_file`。**

[ubench/offchip_latency/embedded/32bit_per_access/src/host.cpp:107-119](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/host.cpp#L107-L119)：用 `seekg/tellg` 求文件长度、`new char[nb]` 读入、拼成 `cl::Program::Binaries` 后构造 `cl::Program`。这就是 `xcl2.hpp` 里 `read_binary_file` 的手写版。

**(3) `host.h`：xcl2 的最小替代品。**

[ubench/offchip_latency/embedded/32bit_per_access/src/host.h:48-53](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/host.h#L48-L53)：三行 `CL_HPP_*` 宏把 C++ OpenCL 绑定锁定在 OpenCL 1.2 语义，再 include `<CL/cl2.hpp>`。[host.h:56-71](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/host.h#L56-L71) 提供 `aligned_allocator`（4096 字节对齐分配器）——注意本工程的 `host.cpp` 实际用的是 map/unmap 流程，这个分配器并没有被用到，属于从 vadd 模板带来的冗余。

**(4) `my_timer.h`：timespec 计时器。**

[ubench/offchip_latency/embedded/32bit_per_access/src/my_timer.h:38-51](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/my_timer.h#L38-L51)：`tic()` 调 `clock_gettime(CLOCK_REALTIME, ...)` 记下起点；`toc()` 取当前时刻，用 [my_timer.h:9-20](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/my_timer.h#L9-L20) 的 `diff()`（处理纳秒字段借位）算差值，经 `printTimeSpec` 以 `秒.纳秒` 格式打印，最后把起点刷新为当前时刻（方便连续分段计时）。它与 datacenter 版 `std::chrono::high_resolution_clock`（[datacenter host.cpp:176](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp#L176)）在测量语义上等价：都是主机端墙钟计时，计时窗口同样覆盖内核启动开销。一个小差别是 `CLOCK_REALTIME` 会被 NTP 校时影响，严格说不如 `CLOCK_MONOTONIC` 稳妥。

**(5) 缓冲管理与「消失的 bank 绑定」。**

[ubench/offchip_latency/embedded/32bit_per_access/src/host.cpp:132-150](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/host.cpp#L132-L150)：三个 `cl::Buffer` 直接用 `CL_MEM_READ_ONLY/READ_WRITE/WRITE_ONLY` 创建，**没有任何 `cl_mem_ext_ptr_t`、没有 `CL_MEM_EXT_PTR_XILINX`、没有任何 bank flag**；随后 `enqueueMapBuffer` 把设备缓冲映射进主机地址空间直接填数，`ptr_b` 填 `0..N-1` 后 `random_shuffle` 洗成随机下标表——这正是 u4-l2 讲过的「洗牌下标」机制。对照 datacenter 版 [host.cpp:115-142](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp#L115-L142) 整段的 `source_in_ext[i].flags = XCL_MEM_DDR_BANK1`——统一内存平台上这段没有存在必要。`enqueueMigrateMemObjects`（[host.cpp:153](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/host.cpp#L153)）在共享内存平台上通常退化为很小的搬运甚至空操作（具体行为取决于 XRT 版本，待本地验证）。

**(6) 计时与「消失的带宽公式」。**

[ubench/offchip_latency/embedded/32bit_per_access/src/host.cpp:155-160](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/host.cpp#L155-L160)：`tic()` → `enqueueTask` → `finish()` → `toc(&timer, "Execution time")`。注意嵌入式版**到此为止**——它不像 datacenter 版 [host.cpp:194-195](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp#L194-L195) 那样再算 `bw_result` 并打印，只输出原始耗时。换算成平均延迟要读者自己做：

\[
\text{latency\_ns} = \frac{T_{\text{sec}} \times 10^{9}}{\text{NUM\_ITERATION} \times \text{size}}
\]

其中 `size = payload / WIDTH_FACTOR`（`WIDTH_FACTOR=1`，见 [host.cpp:53-54](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/host.cpp#L53-L54) 里本地定义的 `#define WIDTH_FACTOR (32/32)`——原本的 `#include "krnl_config.h"` 被注释掉了）。`NUM_ITERATION = 10000` 定义在 [krnl_config.h:7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/krnl_config.h#L7)（注意拼写是单数，datacenter 版是 `NUM_ITERATIONS`）。

另有一个 payload 起点的仓库细节：嵌入式代码从 256（1KB）起扫到 262144（1MB）（[host.cpp:127](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/host.cpp#L127)），与 [embedded README 第 12 行](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/README.md#L12) 一致；但同一 README 的[第 3 行](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/README.md#L3)又写「from 64B」，自相矛盾——以代码为准。

#### 4.1.4 代码实践：两版 host.cpp 差异清单

**实践目标**：用 diff 工具系统地列出 datacenter 与 embedded 两版 `host.cpp` 的结构差异，形成一张对照表。

**操作步骤**：

1. 在仓库根目录执行（只读操作）：

   ```bash
   diff -u ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp \
           ubench/offchip_latency/embedded/32bit_per_access/src/host.cpp
   ```

2. 按下面五个维度整理 diff 输出：设备发现、xclbin 加载、缓冲分配与 bank 绑定、计时、结果输出。
3. 对每个维度，在表里注明两版各自的头文件依赖（`xcl2.hpp` vs `host.h`/`my_timer.h`）。

**需要观察的现象**：diff 中大段删除的 `cl_mem_ext_ptr_t` 代码、`std::chrono` 代码与 `bw_result` 公式；新增的平台遍历循环与 `tic/toc` 调用。

**预期结果**：应得到类似下表的清单（答案速览）：

| 维度 | datacenter 版 | embedded 版 |
| --- | --- | --- |
| 设备发现 | `xcl::get_xil_devices()`，逐卡尝试编程 | 手写平台遍历，找名为 "Xilinx" 的平台取第 0 个加速器设备 |
| xclbin 加载 | `xcl::read_binary_file` | 手写 `ifstream` + `tellg/read` |
| 缓冲分配 | `aligned_allocator` vector + `cl_mem_ext_ptr_t` + bank flag | `cl::Buffer` + `enqueueMapBuffer`/`Unmap`，无 bank 绑定 |
| 计时 | `std::chrono::high_resolution_clock`，算秒数 | `my_timer.h` 的 `tic/toc`（`CLOCK_REALTIME`），打印 `秒.纳秒` |
| 结果输出 | `bw_result = payload*4*0.000010000/t*NUM_KERNEL`，打印 GB/s | 仅打印 Execution time，无带宽/延迟换算 |
| 队列属性 | 乱序 + profiling | 仅 profiling |

**待本地验证**：无需硬件即可完成（纯源码阅读型实践）。

#### 4.1.5 小练习与答案

**练习 1**：嵌入式 `host.cpp` 为什么可以完全没有 `cl_mem_ext_ptr_t`？datacenter 版为什么不行？

**答案**：ZCU104 上 ARM 主机与 PL 内核共享同一根 PS DDR（统一内存），OpenCL 运行时分配的设备缓冲天然在 PL 经 HP/HPC 端口可达的内存中，无需指定通道；Alveo 上 x86 主机内存与卡上 DDR/HBM 物理分离，且 U200 有 2 条 DDR 通道、U280 有 32 个 HBM 伪通道，必须用 `cl_mem_ext_ptr_t` 的 flags 显式声明缓冲放在哪个 bank，并与 `sp=` 行对齐。

**练习 2**：嵌入式版变量名 `krnl_vector_add`、未使用的 `error_message` 与 `DATA_SIZE = 4096` 说明了什么？改代码时应注意什么？

**答案**：说明这份 `host.cpp` 是从 Vitis 官方 vadd（向量加法）示例裁剪而来的，变量名与注释是模板遗留，不代表微基准语义。阅读与修改时要凭内核签名（`krnl_ubench`）和 setArg 编号来理解参数契约，不要被 vadd 注释误导；清理它们是安全的，但要注意 `argc != 2` 检查（xclbin 参数）是有效逻辑，不能当遗留删除。

**练习 3**：`my_timer.h` 的 `toc()` 在打印之后执行 `*start_time = current_time;`，这有什么用？

**答案**：把计时起点滚动到本次结束时刻。这样连续多次调用 `toc(&timer, ...)` 时，每段打印的是「上一结束点到本次结束点」的时长，即自动分段计时；本讲工程里每个 payload 档只调用一次，该特性未被利用。

### 4.2 sd_card 打包

#### 4.2.1 概念说明

datacenter 工程构建的终点是一个 x86 可执行文件和 `.xclbin`，`make check` 直接在本机跑（u1-l3）。嵌入式工程的终点则是一张**能插进 ZCU104 的 SD 卡镜像**：交叉编译出的 aarch64 程序、FPGA 比特流所在的 xclbin、Linux 内核与根文件系统都要打进去。为此嵌入式 Makefile 放弃了 common 仓库的 `utils.mk` 体系，变成一个仅 38 行的自包含脚本，新增了两样 datacenter 流程没有的东西：

- **`v++ -p`（package）步骤**：从平台取启动文件合成 BOOT.BIN，把 rootfs 与所有 `--package.sd_file` 文件组装成 `package/sd_card.img`。
- **板上运行脚本 `run_app.sh`**：Linux 起来后在串口终端里 source 它，完成挂载、平台注册与环境变量设置后启动应用。

`xrt.ini` 则被当作 `--package.sd_file` 之一放进镜像，让板上运行的 XRT 打开性能剖析开关。

#### 4.2.2 核心流程

从源码到板上运行的完整链条：

```text
x86 主机（已 source 交叉编译环境，设置 SDKTARGETSYSROOT 与 ROOTFS）
  make TARGET=hw
  ├─ app.exe            ：$(CXX)（交叉编译器）编 src/host.cpp → ARM64 可执行
  ├─ krnl_ubench.xo     ：v++ -c --config src/zcu104.cfg（HLS 综合）
  ├─ krnl_ubench.xclbin ：v++ -l --config src/zcu104.cfg（链接比特流）
  ├─ emconfig.json      ：emconfigutil --platform xilinx_zcu104_base_202020_1 --nd 1
  └─ package/sd_card.img：v++ -p，打包 BOOT.BIN + Image + rootfs + 6 个 sd_file
        （krnl_ubench.xclbin / Image / xrt.ini / emconfig.json / app.exe / run_app.sh）

ZCU104 板上
  SD 卡上电引导 → BOOT.BIN → 加载 Image + rootfs → Linux 启动 → 串口得到 shell
  source /mnt/sd-mmcblk0p1/run_app.sh
  ├─ mount /dev/mmcblk0p1 /mnt && cd /mnt
  ├─ cp platform_desc.txt /etc/xocl.txt     # 向 XRT 注册平台描述
  ├─ export XILINX_XRT=/usr
  ├─ export XILINX_VITIS=/mnt
  └─ ./app.exe（应带参数 krnl_ubench.xclbin，见下文「已知问题」）
```

#### 4.2.3 源码精读

**(1) 变量守门：`ndef`。**

[ubench/offchip_latency/embedded/32bit_per_access/Makefile:2](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/Makefile#L2)：`ndef = $(if $(value $(1)),,$(error $(1) must be set prior to running))`——变量未定义即报错。它在两处被调用：`app.exe` 目标检查 `SDKTARGETSYSROOT`（[Makefile:7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/Makefile#L7)，交叉编译器 sysroot 就位的证据），`package/sd_card.img` 目标检查 `ROOTFS`（[Makefile:19](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/Makefile#L19)，指向 PetaLinux 根文件系统目录）。这与 datacenter 版 utils.mk 检查 `XILINX_VITIS/XILINX_XRT/DEVICE` 是同一思想、不同变量集。

**(2) 交叉编译主机程序。**

[ubench/offchip_latency/embedded/32bit_per_access/Makefile:6-10](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/Makefile#L6-L10)：`$(CXX)` 编 `src/host.cpp`，链接 `-lOpenCL -lpthread -lrt -lstdc++`，头文件 `-I/usr/include/xrt`。注意 Makefile 本身没有写 `--sysroot` 或 `aarch64-` 前缀——它假设你先 source 了 Vitis/PetaLinux 的 `environment-setup-aarch64-xilinx-linux` 脚本，使 `$(CXX)` 指向交叉编译器（`SDKTARGETSYSROOT` 检查就是守这个前提）。

**(3) v++ 的 -c/-l/-p 三步。**

[Makefile:12-16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/Makefile#L12-L16)：编译与链接都带 `--config ./src/zcu104.cfg`——对嵌入式工程，平台名、时钟频率、sp 连接**全部**写死在这个 cfg 里，而不是像 datacenter 那样由命令行 `DEVICE=` 传入、连接写在独立的 `ubench.ini`。[Makefile:18-28](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/Makefile#L18-L28) 是本讲核心的打包目标：`v++ -p` 以 xclbin 为输入，`--package.rootfs ${ROOTFS}/rootfs.ext4` 提供根文件系统，六个 `--package.sd_file` 把 xclbin、`Image`（Linux 内核镜像，来自 ROOTFS 目录）、`xrt.ini`、`emconfig.json`、`app.exe`、`run_app.sh` 一并放进镜像的启动分区。`v++ -p` 还会从平台生成 BOOT.BIN 与 `platform_desc.txt`（具体镜像分区布局为 2020.2 典型行为：FAT 启动分区 + ext4 rootfs 分区，待本地验证）。

**(4) emconfig 与默认目标。**

[Makefile:30-31](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/Makefile#L30-L31)：`emconfigutil --platform xilinx_zcu104_base_202020_1 --nd 1` 生成 `emconfig.json`（u1-l3 讲过它的仿真角色；此处它被打进镜像随行）。[Makefile:4](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/Makefile#L4) 即 `all: package/sd_card.img`——默认目标直奔 SD 卡镜像，**没有 `check`/`exe` 之类本地运行目标**，仿真路径在这个手写工程里并未接通。`TARGET ?= hw`（[Makefile:37](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/Makefile#L37)）默认直接出硬件比特流。

**(5) `run_app.sh`：板上五步。**

[ubench/offchip_latency/embedded/32bit_per_access/run_app.sh:8-16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/run_app.sh#L8-L16)：① `mount /dev/mmcblk0p1 /mnt` 挂载 SD 卡第一个分区；② `cd /mnt` 进入应用所在目录；③ `cp platform_desc.txt /etc/xocl.txt` 把平台描述注册给 XRT（`v++ -p` 生成的 `platform_desc.txt` 与 xclbin 的 UUID 对应，XRT 靠它确认平台匹配）；④ `export XILINX_XRT=/usr`、`export XILINX_VITIS=/mnt`；⑤ `./app.exe` 启动应用。

**(6) `xrt.ini`：随行的剖析开关。**

[ubench/offchip_latency/embedded/32bit_per_access/xrt.ini:1-3](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/xrt.ini#L1-L3)：`[Debug]` 段设 `profile=true` 与 `timeline_trace=true`，让板上 XRT 在应用工作目录产出 profile 汇总与时间线 trace（CSV）。由于 `run_app.sh` 先 `cd /mnt` 再启动、且 `XILINX_VITIS=/mnt`，XRT 能在 `/mnt` 找到这份 xrt.ini（这是 u7-l3 将展开的内核级剖析入口，本讲先记住它的「搭载方式」：作为 `--package.sd_file` 上卡）。

**(7) 已知问题（真实仓库状态）。**

- [run_app.sh:3](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/run_app.sh#L3) 注释写的是 "ZCU102 board"，而全工程平台是 `xilinx_zcu104_base_202020_1`——注释滞后。
- [run_app.sh:16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/run_app.sh#L16) 的 `./app.exe` **不带参数**，但 [host.cpp:65-68](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/host.cpp#L65-L68) 要求 `argc == 2`（`Usage: app <xclbin>`），直接照抄会 `return EXIT_FAILURE`。上板实操时应写成 `./app.exe krnl_ubench.xclbin`（待本地验证）。

#### 4.2.4 代码实践：从 SD 卡到 benchmark 的完整步骤

**实践目标**：把 `run_app.sh` 的 8 行脚本展开成一份「从给 ZCU104 插卡上电到看到 Execution time 输出」的操作说明，并修掉缺参问题。

**操作步骤**：

1. 重读 [Makefile:18-28](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/Makefile#L18-L28)，列出 `package/sd_card.img` 里最终包含的全部文件及来源（xclbin、Image、rootfs 来自哪条命令/变量）。
2. 重读 [run_app.sh](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/run_app.sh#L1-L19)，给每行加中文注释，说明它解决什么问题（提示：挂载、平台注册、XRT 环境两大变量、启动）。
3. 对照 host.cpp 的 `argc` 检查，指出脚本第 16 行的缺陷，写出修正版（`./app.exe krnl_ubench.xclbin`）。
4. 用 `dd if=package/sd_card.img of=/dev/sdX bs=4M status=progress` 类命令把镜像写入 SD 卡（ destructive 操作，确认设备名后再执行；无硬件则跳过，写出命令即可）。

**需要观察的现象**：板上串口（115200）先输出 U-Boot/Linux 启动日志，shell 出现后 source 脚本，应看到 `Loading: 'krnl_ubench.xclbin'` 与每档 payload 一行 `Execution time: X.XXXXXXXXX`。

**预期结果**：得到一份含「烧卡 → 上电 → 串口登录 → source run_app.sh → app.exe krnl_ubench.xclbin → 读 Execution time」六步的说明文档，并标注脚本两处缺陷（ZCU102 注释、缺 xclbin 参数）。

**待本地验证**：无 ZCU104 与 Vitis 环境时，本实践为源码阅读型：完成步骤 1-3 即可，步骤 4 仅落纸面。

#### 4.2.5 小练习与答案

**练习 1**：嵌入式 Makefile 为什么不需要（也没有）`DEVICE` 变量？

**答案**：平台已经写死在 [zcu104.cfg 第 1 行](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/zcu104.cfg#L1)（`platform=xilinx_zcu104_base_202020_1`），`v++ -c/-l/-p` 三步都通过 `--config ./src/zcu104.cfg` 读它；datacenter 版则把平台作为命令行 `DEVICE=` 传给每步 v++ 并用于产物目录命名，因而需要 utils.mk 检查它。

**练习 2**：`cp platform_desc.txt /etc/xocl.txt` 删掉会发生什么？

**答案**：XRT 找不到平台描述，无法把加载的 xclbin 与当前平台对上号，`cl::Program` 构造（加载 xclbin）大概率失败。`platform_desc.txt` 由 `v++ -p` 打包时生成、随镜像落在 SD 分区，`/etc/xocl.txt` 是 XRT 在嵌入式系统上的约定注册位置。

**练习 3**：为什么 `xrt.ini` 要靠 `--package.sd_file` 带上板，而不是像 datacenter 那样放在工程目录即可？

**答案**：板上运行时的工作目录是 SD 卡挂载点（脚本 `cd /mnt`），x86 工程目录在板上并不存在；把 `xrt.ini` 打进镜像并配合 `export XILINX_VITIS=/mnt`，XRT 才能在板上找到它，从而启用 profile/timeline_trace 输出。

### 4.3 HP/HPC 端口模型

#### 4.3.1 概念说明

第三个模块回答「内核到底连到哪」。u3-l3 讲过 Alveo 的答案：`sp=krnl_ubench_1.in0:DDR[0]`，目标是一条内存通道。ZCU104 的答案换了词汇表：`sp=` 的目标是 **PS 的 AXI 从机端口名**（HP0/HP1/…/HPC0/HPC1）。

两种模型对应的硬件拓扑：

```text
Alveo U280：  PL 内核 --m_axi--> HBM/DDR 控制器 --> 内存颗粒
ZCU104：     PL 内核 --m_axi--> PS 的 HP/HPC 从机端口 --> PS 互联/CCI --> PS DDR 控制器
```

由此派生出一连串差异：

| 维度 | Alveo U200/U280（datacenter） | ZCU104（embedded） |
| --- | --- | --- |
| 片外内存 | 卡上 DDR4 / HBM2，FPGA 私有 | PS 侧 DDR4，与 ARM 核共享 |
| `sp=` 目标名 | `DDR[0]`、`HBM[0..31]` | `HP0..HP3`、`HPC0/HPC1` |
| 可用「通道」数 | U200 双 DDR；U280 32 伪通道 | 6 个端口（见 config.py 的 PORT_NAMES） |
| 端口数据宽度上限 | 512 bit | 128 bit |
| 主机缓冲绑定 | `cl_mem_ext_ptr_t` + bank flag | 不需要（统一内存） |
| SLR 摆放 | `slr=` 指令（多 die） | 无（单 die，cfg 中无 slr 行） |
| 典型内核频率 | 300 MHz | 150 MHz |
| 连接配置文件 | `ubench.ini`（仅连接） | `zcu104.cfg`（平台+时钟+连接+profile 一体） |

理论峰值带宽也随之缩水一个量级：datacenter 读带宽样板为 \(300\,\text{MHz} \times 2 \times 512/8 = 38.4\,\text{GB/s} \)；嵌入式 auto_collect 默认参数（4 端口 × 128 bit × 150 MHz）为

\[
150 \times 10^{6} \times 4 \times \frac{128}{8} = 9.6 \times 10^{9}\,\text{B/s} = 9.6\,\text{GB/s}
\]

#### 4.3.2 核心流程

嵌入式连接配置由三层落点共同实现：

```text
config.py  MEMORY_TYPE = [{'BANK_TYPE':'DDR',
                           'PORT_NAMES':['HP0','HP1','HP2','HP3','HPC0','HPC1'],
                           'DEVICE_NAME':'xilinx_zcu104_base_202020_1'}]
     │
     ▼ connectivity_gen.py.generateConnectivity(freq, device, access, nport, port_names)
逐端口拼一行：sp=krnl_ubench_1.in{j}:{PORT_NAMES[j]}     # RD 用 in 前缀，WR 用 out 前缀
外加 platform= / [clock] defaultFreqHz= / [profile] 段 → 写出 zcu104.cfg
     │
     ▼ v++ -c / v++ -l 都带 --config zcu104.cfg
端口在链接期焊死到 PS 的 HP/HPC 从机端口；主机侧零配合（无 bank flag）
```

#### 4.3.3 源码精读

**(1) 手写工程的 `zcu104.cfg`。**

[ubench/offchip_latency/embedded/32bit_per_access/src/zcu104.cfg:1-14](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/zcu104.cfg#L1-L14)：第 1 行平台名；[第 5-6 行](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/zcu104.cfg#L5-L6) `[clock] defaultFreqHz=150000000` 把内核频率定为 150 MHz（对比 datacenter 的 300 MHz）；[第 8-11 行](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/zcu104.cfg#L8-L11) 是本模块核心——三条 `sp=` 把延迟内核的三个 m_axi 端口分别焊到 `HP0`（数据口 in0）、`HP1`（下标口 in0_index）、`HP2`（结果口 sum）；[第 13-14 行](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/zcu104.cfg#L13-L14) 的 `[profile] data=all:all:all` 是 v++ 层的数据搬运监控（与运行期 xrt.ini 分属两层）。注意**没有 `slr=` 行**（ZCU104 单 die 无 SLR 概念）、**没有 `nk=` 行**（缺省即 1 个实例，CU 名仍按 `krnl_ubench_1` 生成）。

**(2) 内核侧的配套差异。**

[ubench/offchip_latency/embedded/32bit_per_access/src/krnl_ubench.cpp:4-19](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/krnl_ubench.cpp#L4-L19)：签名比 datacenter 版多一个输出指针 `int* sum`（占用 `bundle=gmem2`，正好对应 cfg 里的 `sp=...sum:HP2`）；数据口的突发参数缩到 [第 10 行](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/krnl_ubench.cpp#L10) 的 `num_read_outstanding=1 max_read_burst_length=2`（datacenter 版是 16/16，见 [datacenter 内核第 5 行](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_ubench.cpp#L5)）——随机访问本来就合并不出长突发，经 PS 端口的通路更不必留大突发余量。防优化手法也换了口味：[第 21-38 行](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/embedded/32bit_per_access/src/krnl_ubench.cpp#L21-L38) 里 `temp_data_0` 不再是 volatile，改为把每次读到的低 32 位累进 `temp_sum` 并最终写 `sum[0]`——「消费读到的值」这一步从 volatile 临时变量换成了真实的输出依赖。片上下标数组缩为 `local_in0_index[262144]`（datacenter 版为 524288，见 [datacenter 第 13 行](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_ubench.cpp#L13)），且装载循环没有 `#pragma HLS PIPELINE II=1`（datacenter 版有，[第 17 行](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_ubench.cpp#L17)）——装载不计时，慢一点不影响测量。

**(3) auto_collect 侧的 HP/HPC 模型。**

[ubench/offchip_bandwidth/embedded/auto_collect/config.py:7-12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/embedded/auto_collect/config.py#L7-L12)：参数空间把 datacenter 版的 `MEMORY_TYPE = DDR/HBM` 换成了带 `PORT_NAMES:['HP0','HP1','HP2','HP3','HPC0','HPC1']` 与 `DEVICE_NAME:'xilinx_zcu104_base_202020_1'` 的字典；`KERNEL_FREQ = [150]`。[embedded README 第 33 行](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/embedded/README.md#L33) 对同一配置的散文说明。生成器侧，[connectivity_gen.py:18-22](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/embedded/auto_collect/connectivity_gen.py#L18-L22) 按端口序号拼 `sp=krnl_ubench_1.in{j}:{port_names[j]}`（写基准用 `out` 前缀），所以 `NUM_CONCURRENT_PORT=4` 会恰好吃掉 `HP0..HP3` 四个端口；[connectivity_gen.py:10-15](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/embedded/auto_collect/connectivity_gen.py#L10-L15) 同时拼出平台行与时钟行——这就是嵌入式版连接文件叫 `zcu104.cfg` 而非 `ubench.ini`、且多出平台与时钟段的来历。由于 HP/HPC 是端口名而非内存通道名，嵌入式参数空间里也没有 `MAX_BURST_LENGTH` 维度的用武之地（突发仍可在内核 pragma 里设，但端口表本身不涉及）。

**(4) 一致性提示。** HP 与 HPC 的选择有语义后果：走 HPC 端口的数据可与 ARM 核 cache 一致（经 CCI），适合主机与内核频繁共享小块数据的场景；HP 不一致但路径更直接。对「顺序灌数据、内核独占扫描」的带宽/延迟基准，两者测出的都是 PL→DDR 通路的吞吐/延迟特性，把端口从 HP 换到 HPC 本身就是一个值得做的对照实验。

#### 4.3.4 代码实践：改写端口映射

**实践目标**：通过修改 `sp=` 行体会「端口名即连接目标」，并核算嵌入式理论带宽。

**操作步骤**：

1. 复制 `ubench/offchip_latency/embedded/32bit_per_access` 到临时目录（不要动源码树），打开 `src/zcu104.cfg`。
2. 把 `sp=krnl_ubench_1.in0:HP0` 改为 `sp=krnl_ubench_1.in0:HPC0`，保持其余两行不动；写出重新 `v++ -l` 后数据口与下标口分别落在哪个 PS 端口。
3. 为嵌入式带宽基准设计一个 4 端口方案：写出四条 `sp=` 行（提示：按 `connectivity_gen.py` 的拼接规则，端口名依次取 `HP0..HP3`），并计算 150 MHz、4×128 bit 的理论峰值带宽。
4. 推演：若把 `NUM_CONCURRENT_PORT` 提到 6，`PORT_NAMES` 表会被吃空吗？若提到 7 会发生什么（结合表长 6 讨论，待本地验证）。

**需要观察的现象**：`sp=` 行的目标一改，内核端口到 PS 端口的焊点随之改变，而主机代码零改动——对比 u3-l3 中 datacenter 版改 sp 必须同步改主机 bank flag，体会「统一内存 + 端口名模型」把配置收敛到了链接期一处。

**预期结果**：

- 步骤 2：数据口 in0 → HPC0（一致性端口），下标口 in0_index 仍 → HP1，sum 仍 → HP2。
- 步骤 3：四行 `sp=krnl_ubench_1.in0:HP0` … `sp=krnl_ubench_1.in3:HP3`；理论峰值 \(150 \times 10^6 \times 4 \times 16\,\text{B} = 9.6\,\text{GB/s}\)。
- 步骤 4：6 端口恰好用满 `['HP0','HP1','HP2','HP3','HPC0','HPC1']`；7 端口会越界访问 `port_names[6]`（脚本层面无检查，属 latent bug，待本地验证）。

**待本地验证**：重新综合需 Vitis + ZCU104 平台文件；无环境时完成纸面推演即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `zcu104.cfg` 里没有 `slr=` 和 `nk=` 行，而 datacenter 的 `ubench.ini` 有？

**答案**：`slr=` 做 CU 的跨 die 摆放，ZCU104 的 Zynq 单芯片没有 SLR 划分，无从摆放；`nk=` 指定实例数与命名，嵌入式延迟基准只用 1 个实例，Vitis 对缺省情况自动生成 `krnl_ubench_1` 这个 CU 名（cfg 的 `sp=` 行里正是引用它），故可省略。

**练习 2**：嵌入式延迟内核用 `sum[0] = temp_sum` 防优化，datacenter 版用 `volatile INTERFACE_WIDTH temp_data_0`，两者共同点是什么？

**答案**：都在制造「读到的值必须被消费」的依赖，防止综合器把读循环判为死代码消除。区别是嵌入式版把消费落到真实输出端口（sum，还顺带占了一个 HP2 端口），datacenter 版靠 volatile 限定符向编译器承诺副作用。

**练习 3**：同一套 uBench 五因素（频率、端口数、位宽、突发、连续数据量）在嵌入式平台哪几项的上限明显更低？

**答案**：频率（150 vs 300 MHz）、位宽（HP/HPC 每端口最高 128 bit vs Alveo 512 bit）、可用并发端口数（6 个 PS 端口 vs U280 的 32 个 HBM 伪通道）；理论峰值因此从几十 GB/s 降到个位数 GB/s。突发长度维度在嵌入式参数空间中未单独扫描。

## 5. 综合实践

**任务：给嵌入式延迟基准补上「延迟输出」。**

背景：嵌入式 `host.cpp` 目前只打印 `Execution time: 秒.纳秒`，没有换算成 ns/access，测量结果无法直接与 datacenter 版（打印 GB/s，u4-l2 已指出其口径问题）对照。请你闭环完成：

1. **补计时捕获**（源码阅读 + 设计）：`my_timer.h` 的 `toc()` 只打印不返回时长。设计一个最小改动方案：增加一个 `double toc_sec(timespec*)` 之类的辅助函数（或在 host.cpp 里直接对 `diff()` 结果换算 `tv_sec + tv_nsec*1e-9`），使每个 payload 档拿到秒制时长 `T`。标注为「示例代码」，不要真的修改仓库源码，把补丁写在讲义/笔记里。
2. **补换算公式**：利用 `krnl_config.h` 的 `NUM_ITERATION=10000` 与 `input_size`，按 \[ \text{latency\_ns} = \frac{T \times 10^{9}}{\text{NUM\_ITERATION} \times \text{input\_size}} \] 推导每档输出，并写出与 datacenter 版 `bw_result`（GB/s）的互验关系 \[ \text{latency\_ns} = \frac{\text{WIDTH\_FACTOR} \times 4}{\text{bw\_result (GB/s)}} \] （即 u4-l2 的换算式）。
3. **补端口方案**：参照 4.3.4，写出本工程三个端口（in0/in0_index/sum）分别走 HP0/HP1/HPC0 的 `zcu104.cfg` 修改版，并说明哪条通路的一致性属性变了。
4. **全链路文档**：把 4.2.4 的六步上板说明与上述修改合并，形成一份《ZCU104 延迟微基准移植手册》：从 `make TARGET=hw`（需 `SDKTARGETSYSROOT`/`ROOTFS`）到板上 `./app.exe krnl_ubench.xclbin`，最终每档 payload 同时输出 Execution time 与 latency_ns。

**验收标准**（纸面即可）：公式量纲正确（秒→ns/次）、修改点只落在 host.cpp 与 zcu104.cfg、上板命令携带 xclbin 参数。全部运行结果均待本地验证（需 ZCU104 与 Vitis 2020.2 环境）。

## 6. 本讲小结

- 嵌入式主机框架 = `host.h`（cl2.hpp 最小封装）+ `my_timer.h`（timespec tic/toc）+ 手写的平台遍历设备发现与 ifstream 加载；统一内存使 `cl_mem_ext_ptr_t` bank 绑定整块消失，改用 map/unmap 管理缓冲。
- 计时语义与 datacenter 版等价（主机墙钟、含内核启动开销），但嵌入式版只打印 Execution time，带宽/延迟换算留给读者，公式为 \( T \times 10^9 / (\text{NUM\_ITERATION} \times \text{size}) \)。
- sd_card 打包 = 交叉编译 `app.exe`（`SDKTARGETSYSROOT` 守门）→ `v++ -c/-l --config zcu104.cfg` → `v++ -p` 连同 rootfs、Image、xrt.ini 等 6 个 `--package.sd_file` 打成 `package/sd_card.img`；板上 `run_app.sh` 五步：挂载、cd、注册 `platform_desc.txt`、导出 XRT 环境、启动应用（注意脚本缺 xclbin 参数、注释误写 ZCU102）。
- HP/HPC 端口模型：`sp=` 目标从 `DDR[n]/HBM[n]` 换成 PS 的 `HP0..HP3/HPC0/HPC1`（上限 6 个、128 bit、150 MHz，理论峰值约 9.6 GB/s）；连接配置升级为含平台与时钟的 `zcu104.cfg`，无 `slr=`/`nk=` 行；auto_collect 用 `PORT_NAMES` 表驱动 `connectivity_gen.py` 拼接。
- 嵌入式内核相对 datacenter 版的实质差异：多 `sum` 输出端口（消费读值防优化）、突发参数缩为 2、下标数组减半、装载循环不流水。
- 真实仓库阅读素养：vadd 模板遗留（`krnl_vector_add`、未用变量）、README 内部矛盾（64B vs 1KB 起点）、脚本与代码的参数契约缺口，都要以 `src/` 代码为准。

## 7. 下一步学习建议

- 下一讲 u5-l1 将进入 auto_collect 自动化流水线：本讲见到的 `config.py` 的 `PORT_NAMES`/`KERNEL_FREQ` 会作为七维参数空间的成员，被 `generate_microbenchmarks.py` 展开成成批的微基准目录，建议先记住本讲「config → 生成器 → zcu104.cfg」的链路。
- 若想巩固本讲的对照方法，可自行 diff `ubench/offchip_bandwidth/embedded` 与 `datacenter` 的同类工程，验证「五件套 + HP/HPC 替换件」规律是否处处成立。
- 阅读顺序建议：`ubench/streaming_bandwidth/embedded/auto_collect/config.py`（流版嵌入式参数）→ `ubench/offchip_bandwidth/embedded/auto_collect/generate_microbenchmarks.py`（批量生成主流程）。
- 对测量精度感兴趣的读者可以预习 u7-l3：本讲的 `xrt.ini`（profile/timeline_trace）与 `zcu104.cfg` 的 `[profile]` 段将在那里展开为内核级计时的完整方案，用来批判主机端计时窗口的误差。

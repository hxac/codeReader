# 目录结构与源码地图

## 1. 本讲目标

通过本讲，你将能够：

- 把 Nyuzi 仓库的「目录结构」与「功能模块」对应起来，看到 `hardware`、`software`、`tools`、`tests` 四个一级目录就知道它们各自装着什么。
- 知道硬件核心、FPGA 外设、模拟器主程序、软件库（libc/libos/librender）、内核、应用、测试框架分别落在哪条具体路径下。
- 在后续阅读任何一篇讲义时，能根据关键词（例如「L2 缓存」「TLB」「渲染」「cosimulation」）快速跳到正确的源码目录，而不是在仓库里盲目翻找。

本讲是「导航课」：它本身不深入任何一处实现，但它给你一张地图，让你在后面的每一讲里都不会迷路。

## 2. 前置知识

在学习本讲之前，请确认你已经在脑中建立了上一讲（u1-l1 项目定位与整体架构）给出的两个认知：

1. **Nyuzi 的五大组成**：可综合的 SystemVerilog 硬件、C 指令集模拟器、LLVM C/C++ 工具链、软件栈、多层次测试。这五部分分别住在仓库的不同目录里，本讲就是把它们一一安顿到目录树中。
2. **两件构建产物**：`bin/nyuzi_emulator`（C 指令集模拟器，快但非周期精确）与 `bin/nyuzi_vsim`（Verilator 编译出的周期精确硬件模型）。它们由 `hardware`、`tools` 两个目录的源码产出，理解目录就能理解它们「从哪里来」。

如果你还没有这两个认知，建议先回到 u1-l1。本讲会用到的少量术语如下：

- **可综合（synthesizable）**：能被综合工具转成真实芯片电路的硬件描述；与之相对的是「只为仿真存在」的代码。
- **周期精确（cycle-accurate）**：仿真器逐时钟周期地复现硬件行为，连流水线、缓存时序都对得上。
- **testbench**：硬件仿真里的「测试台」，负责给被测模块喂激励、接假外设。
- **顶层模块（top-level module）**：整个硬件设计的最外层，里面实例化了所有子模块，Nyuzi 的顶层模块名为 `nyuzi`。

## 3. 本讲源码地图

本讲主要阅读四个 README 与若干目录清单，它们是仓库里最权威的「目录说明书」：

| 文件 | 作用 |
|------|------|
| [hardware/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/README.md) | 说明硬件三大子目录 `core`/`fpga`/`testbench` 的职责，以及仿真/综合选项。 |
| [software/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/README.md) | 说明软件目录的内容，以及每个程序自带的运行脚本。 |
| [tests/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/README.md) | 说明测试如何运行、如何新增，以及五类测试策略的分工。 |
| [tools/emulator/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/README.md) | 说明指令集模拟器的定位、命令行选项与调试用法。 |

> 提示：这些 README 是「活文档」，后续讲义中引用的很多细节（命令行参数、测试约束、设备寄存器）都以它们为源头。养成「先读目录 README，再进子目录」的习惯，会让你少走很多弯路。

## 4. 核心概念与源码讲解

本讲把仓库拆成四个最小模块来讲：**硬件目录**、**软件目录**、**工具目录**、**测试目录**。每个模块都遵循「概念 → 目录布局 → 源码精读 → 实践 → 练习」的节奏。

仓库的一级目录全景如下（只列与学习相关的目录）：

```
NyuziProcessor/
├── hardware/      # 4.1 硬件：SystemVerilog 设计（core/fpga/testbench）
├── software/      # 4.2 软件：libs/apps/benchmarks/bootrom/kernel
├── tools/         # 4.3 工具：emulator、serial_boot、misc 脚本等
├── tests/         # 4.4 测试：五类测试策略 + 通用 test_harness.py
├── cmake/         # CMake 辅助脚本（如 nyuzi.cmake 生成 run_* 脚本）
├── scripts/       # 安装脚本（setup_tools.sh、VCS 脚本等）
└── CMakeLists.txt # 根构建文件，串起上面四个子项目
```

### 4.1 硬件目录（hardware/）

#### 4.1.1 概念说明

`hardware/` 存放处理器的 SystemVerilog 硬件实现，是整个项目「能变成芯片」的那一半。它本身又分三个职责完全不同的子目录：

- **`core/`**：Nyuzi 的 GPGPU 核心本身，是「真正属于 Nyuzi」的可综合设计。顶层模块叫 `nyuzi`，下面挂着流水线、缓存、TLB 等所有微结构。可配置参数（核数、缓存大小、相联度）放在 `core/config.svh`（README 里写作 `config.sv`，实际文件是 `config.svh`）。
- **`fpga/`**：一个「快速搭出来的片上系统（SoC）」测试环境，目的是让 Nyuzi 能在 FPGA 上跑起来。它**不属于 Nyuzi 核心**，只是把核心和 SDRAM 控制器、VGA 控制器、AXI 互连、串口等外设拼到一起。
- **`testbench/`**：仿真支持代码，包括给仿真用的「假外设」（mock peripherals）和波形/跟踪相关设施。

关键直觉：**`core/` 是「处理器」，`fpga/` 是「装着处理器的一块板子」，`testbench/` 是「让处理器在电脑里假装运行起来的脚手架」**。这三者职责分离，意味着你可以只学 `core/` 就理解 CPU 微结构，而不必同时被 FPGA 外设干扰。

#### 4.1.2 核心流程

当你想定位硬件里某个功能时，按下面的决策树走：

1. 要找的功能是 **CPU 微结构**（流水线、缓存、TLB、执行单元）吗？→ 去 `hardware/core/`。
2. 要找的是 **FPGA 板上的外设**（串口、SDRAM、VGA、SPI）吗？→ 去 `hardware/fpga/common/`。
3. 要找的是 **某块具体 FPGA 板**（如 DE2-115）的顶层连接吗？→ 去 `hardware/fpga/de2-115/`。
4. 要找的是 **仿真专用的假外设或测试台顶层**吗？→ 去 `hardware/testbench/`。

`hardware/core/` 内部文件命名很有规律，基本是「阶段名.sv」：

```
hardware/core/
├── nyuzi.sv                  # 顶层模块：多核 + L2 + IO 互连 + 调试器
├── core.sv                   # 单核流水线顶层（各级 stage 实例化在这里）
├── config.svh                # 可配置参数（核数/线程数/缓存/TLB）
├── defines.svh               # 由 config 派生的类型与常量（贯穿全项目）
├── ifetch_tag_stage.sv       # 取指：PC + ITLB + I-Cache 标签
├── ifetch_data_stage.sv      # 取指：I-Cache 数据读出
├── instruction_decode_stage.sv
├── thread_select_stage.sv    # 线程选择 + 记分牌
├── operand_fetch_stage.sv    # 操作数 fetch（读寄存器/掩码）
├── int_execute_stage.sv      # 整数执行 + 分支解析
├── fp_execute_stage1~5.sv    # 浮点五级流水线
├── dcache_tag_stage.sv       # L1 数据缓存：标签级
├── dcache_data_stage.sv      # L1 数据缓存：数据级
├── l2_cache.sv               # L2 缓存（四阶段流水线）
├── tlb.sv                    # 软件管理 TLB
├── control_registers.sv      # 控制寄存器 + 中断
├── on_chip_debugger.sv       # 片上调试器（JTAG）
└── ...                       # 还有 sram_*.sv、sync_fifo.sv、scoreboard.sv 等
```

> 上面这份 `core/` 清单是基于真实文件列表整理的「典型文件」，并非全部。本讲的代码实践会带你亲手核对这些路径。

#### 4.1.3 源码精读

硬件目录的三分法，由 `hardware/README.md` 开头一段明确写定：

[hardware/README.md:L1-L15](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/README.md#L1-L15) —— 这段说明 `hardware/` 下有 `core/`、`fpga/`、`testbench/` 三个目录，并分别点明：`core/` 是 GPGPU 本体（顶层模块 `nyuzi`），`fpga/` 是 SoC 测试环境（含 SDRAM/VGA/AXI/串口，DE2-115 的 makefile 在 `fpga/de2-115`），`testbench/` 是仿真支持。

这份 README 还提醒了两件影响「可综合性」的事，这正是 `core/` 与 `fpga/`、`testbench/` 的本质差别所在：

[hardware/README.md:L21-L24](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/README.md#L21-L24) —— 仿真时会定义预处理宏 `SIMULATION`，用来在综合时关掉那些只为仿真存在的代码。

[hardware/README.md:L27-L29](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/README.md#L27-L29) —— 设计使用了参数化的存储（FIFO 与 SRAM 块）：`core/sram_1r1w.sv`、`core/sram_2r1w.sv`、`core/sync_fifo.sv`。默认实例化的是「仿真版」，并不能（高效地）综合。

换句话说：**`core/` 里的设计是「可综合的」，但里面的存储宏默认指向仿真实现；要真上 FPGA，得靠 `VENDOR_ALTERA` 之类宏切换成厂商宏。** 这也解释了为什么 `fpga/` 和 `testbench/` 要分开——它们各自带了一套「存储/外设」的现实实现。

具体到顶层模块，`hardware/core/nyuzi.sv` 第 26 行就是整个设计的最外层：

[hardware/core/nyuzi.sv:L26](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L26) —— `module nyuzi`，顶层模块声明，内部实例化多核、L2 缓存、IO 互连与片上调试器。

而 L2 缓存（后续 u6-l3 会精读）落在：

[hardware/core/l2_cache.sv:L39](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache.sv#L39) —— `module l2_cache`，物理索引/物理标签的四阶段流水线缓存。

#### 4.1.4 代码实践

**实践目标**：亲手核对 `hardware/core/` 的文件清单，建立「文件名 ↔ 功能」的直觉。

**操作步骤**：

1. 在仓库根目录执行 `ls hardware/core/*.sv`，把所有 `.sv` 文件名列出来。
2. 对照本讲 4.1.2 里给出的清单，把文件名按功能归类（取指 / 解码 / 调度 / 执行 / 缓存 / 内存 / 调试）。
3. 执行 `ls hardware/fpga/common/*.sv` 与 `ls hardware/testbench/*.sv`，确认它们的内容确实与 `core/` 不同（前者是外设，后者是仿真假外设）。

**需要观察的现象**：

- `hardware/core/` 下应有 40 余个 `.sv` 文件，且能看到 `ifetch_*`、`*_execute_stage*`、`dcache_*`、`l2_cache*` 等成族命名。
- `hardware/fpga/common/` 下应是 `uart.sv`、`sdram_controller.sv`、`vga_controller.sv`、`axi_interconnect.sv` 这类「板级外设」。
- `hardware/testbench/` 下应是 `soc_tb.sv`、`sim_sdram.sv`、`sim_sdmmc.sv`、`trace_logger.sv` 这类「仿真专用」文件。

**预期结果**：你会清楚地看到三个子目录的文件「画风」完全不同，从而验证 README 里「核心 / 板级 / 仿真」的三分法。

> 待本地验证：若你的环境未 clone 完整仓库，文件数量可能与本讲略有出入，以你本地 `ls` 的实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Nyuzi 要把 `fpga/` 和 `core/` 分开，而不是把外设控制器也放进 `core/`？

**参考答案**：因为 `core/` 是「Nyuzi 处理器核心」，追求可综合、可移植、专注微结构；而 `fpga/` 是「为了让核心在某块具体板子上跑起来」而临时拼的 SoC（SDRAM/VGA/串口等与厂商强相关）。分开后，换一块板子只需要改 `fpga/`，核心设计不受影响；同时学核心的人也不会被板级细节干扰。

**练习 2**：README 说存储模块默认实例化的是「仿真版，不能高效综合」。那么仿真构建产物 `nyuzi_vsim` 为什么能直接用这些仿真版存储？

**参考答案**：因为 Verilator 本身就是在做仿真（把 RTL 编译成可执行的 C++），它不需要可综合性，反而正需要这些行为级的仿真存储模型。可综合性只在真正要流片/上 FPGA 时才要求，那时再靠 `VENDOR_ALTERA` 等宏切换成厂商宏。

### 4.2 软件目录（software/）

#### 4.2.1 概念说明

`software/` 存放「跑在 Nyuzi 处理器上的软件」。它和 `hardware/` 是镜像关系：一边是机器，一边是跑在机器上的程序。`software/README.md` 开头点明它包含 **libraries、apps、benchmarks**，但实际目录里还有两个不在简介里却很重要的子目录：**bootrom**（上电启动 ROM）和 **kernel**（一个简易操作系统内核）。

各子目录职责：

| 子目录 | 职责 |
|--------|------|
| `libs/` | 基础库：`libc`（C 标准库）、`libos`（线程调度/并行，分 bare-metal 与 kernel 两套）、`librender`（软件 3D 渲染库）、`libconsole`。 |
| `apps/` | 示例与演示程序：`hello_world`、`sceneview`、`mandelbrot`、`doom`、`plasma`、`quakeview`、`rotozoom`、`colorbars`、`consoletest`、`shadow_map`。 |
| `benchmarks/` | 性能基准：`dhrystone`、`conj_grad`、`hash`、`membench`。 |
| `bootrom/` | 上电启动代码（`start.S`、`boot.c`、`boot.ld`），FPGA 从串口接收程序用的引导器。 |
| `kernel/` | 简易操作系统内核：进程/虚拟内存/线程/文件系统（`main.c`、`trap.c`、`vm_*.c`、`thread.c` 等）。 |

关键直觉：**`libs/` 是「造程序的积木」，`apps/` 是「用积木搭出来的成品」，`benchmarks/` 是「测性能的成品」，`bootrom` 和 `kernel` 是「让成品能被加载、能受保护地运行的基础设施」。**

#### 4.2.2 核心流程

软件栈的分层依赖（上层依赖下层）大致是：

```
apps/  ──────────────┐
benchmarks/ ─────────┼──►  libs/  (libc, libos, librender, libconsole)
                     │
kernel/ ──► libs/libos(kernel 变体) ──► libc
bootrom/ （独立，极简，不依赖上层）
```

也就是说，一个普通裸机程序（如 `apps/hello_world`）会链接 `libc`（提供 `printf`）和 `libos` 的 bare-metal 变体（提供启动 `crt0.S` 与堆）；而 `kernel/` 自己就是「宿主」，它提供系统调用，让用户程序通过 `libos` 的 kernel 变体来请求服务。

定位软件代码时，先问「这是库、应用、基准、还是系统软件？」，答案直接决定你去哪个子目录。

#### 4.2.3 源码精读

`software/README.md` 的开头界定了这个目录的范围：

[software/README.md:L1-L3](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/README.md#L1-L3) —— 说明 `software/` 包含运行在 Nyuzi 上的库、应用与基准测试。

这份 README 还给出了每个程序目录里都自带的一组运行脚本，这组脚本正是连接「软件目录」与「工具/硬件目录」的桥梁：

[software/README.md:L6-L22](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/README.md#L6-L22) —— 列出 `run_emulator`（在模拟器跑，可弹帧缓冲窗口）、`run_verilator`（在 Verilator 仿真跑）、`run_vcs`（在 VCS 跑）、`run_fpga`（经串口下到 FPGA 板）、`run_debug`（在模拟器里跑并接 lldb 调试）。

具体到「libc 在哪」「渲染库在哪」，真实路径如下（可在仓库里直接核对）：

- **libc**：源码 `software/libs/libc/src/`（含 `stdio.c`、`vfprintf.c`、`string.c`、`dlmalloc.c` 等），头文件 `software/libs/libc/include/`（`stdio.h`、`stdlib.h`、`string.h` 等标准头）。
- **librender**：`software/libs/librender/`（含 `RenderContext.cpp`、`Surface.cpp`、`Texture.cpp`、`Rasterizer.cpp`、`TriangleFiller.cpp` 等）。
- **libos**：`software/libs/libos/bare-metal/` 与 `software/libs/libos/kernel/` 两套变体，共享 `software/libs/libos/schedule.h` 等头文件。
- **kernel**：`software/kernel/`（`main.c`、`trap.c`、`trap_entry.S`、`syscall.c`、`vm_address_space.c`、`thread.c` 等）。

> 注意：`software/README.md` 的简介只提了 libraries/apps/benchmarks 三类，**没有**提 `bootrom` 和 `kernel`，但它们确实存在于目录中。这正是「README 是入口而非穷举」的典型例子——遇到简介没覆盖的内容，要以实际目录为准。

#### 4.2.4 代码实践

**实践目标**：定位 libc 与 librender 的真实文件，验证「库 = 一组源文件 + 一组头文件」的组织方式。

**操作步骤**：

1. 执行 `ls software/libs/libc/src/` 与 `ls software/libs/libc/include/`。
2. 执行 `ls software/libs/librender/`。
3. 打开 `software/libs/libc/include/stdio.h`，找到 `printf` 的声明；再打开 `software/libs/libc/src/stdio.c`，看它的实现是如何走向 `vfprintf` 的（u9-l1 会精读这条链）。

**需要观察的现象**：

- libc 的 `src/` 与 `include/` 分离：实现放 `src/`，对外头文件放 `include/`，这是 C 库的标准布局。
- librender 是纯 C++（`.cpp`/`.h`），且能看到 `RenderContext`、`Surface`、`Texture`、`Rasterizer`、`TriangleFiller` 这几个核心类，对应后续渲染管线讲义（u9-l3、u13）。

**预期结果**：你能在不看本讲的情况下，凭目录结构判断「`stdio.c` 是 printf 实现」「`RenderContext.cpp` 是渲染入口」。

#### 4.2.5 小练习与答案

**练习 1**：`software/README.md` 的简介说该目录包含 libraries、apps、benchmarks，但目录里还有 `bootrom` 和 `kernel`。这两个目录分别是干嘛的？

**参考答案**：`bootrom/` 是上电引导器（FPGA 上电后最先运行的极简代码，负责从串口接收用户程序并跳转执行，见 `start.S`/`boot.c`）；`kernel/` 是一个简易操作系统内核，提供进程加载、虚拟内存、线程、系统调用与文件系统（见 `main.c`/`trap.c`/`vm_*.c`）。它们是「让应用能被加载和受保护运行」的基础设施，所以和普通 app 分开。

**练习 2**：一个普通裸机程序 `apps/hello_world` 会用到 `libos` 的哪一套变体？kernel 自身又用哪一套？

**参考答案**：`hello_world` 是裸机程序，链接 `libos` 的 **bare-metal** 变体（`software/libs/libos/bare-metal/`，含 `crt0.S` 启动代码）。而 `kernel` 自身不需要别人给它提供系统调用，它链接的是同一套库的内核侧实现；用户态程序想用系统调用时，则走 `libos` 的 **kernel** 变体（`software/libs/libos/kernel/`），由它代为陷入内核。

### 4.3 工具目录（tools/）

#### 4.3.1 概念说明

`tools/` 存放「开发与调试 Nyuzi 时使用的宿主工具」。注意区分：`software/` 是「跑在 Nyuzi 上的程序」，`tools/` 是「跑在你的电脑上、用来开发/调试/加载 Nyuzi 程序的工具」。两者运行环境完全不同。

实际子目录：

| 子目录 | 职责 |
|--------|------|
| `emulator/` | **C 指令集模拟器**，构建产物 `bin/nyuzi_emulator`，是本目录的主角。 |
| `NyuziToolchain/` | LLVM C/C++ 工具链子模块（`clang`、`elf2hex`、`lldb` 等），由 `setup_tools.sh` 拉取。 |
| `verilator/` | Verilator 子模块（用于把 RTL 编译成 `nyuzi_vsim`）。 |
| `serial_boot/` | 经串口把程序下载到 FPGA 的工具（`serial_boot.c`）。 |
| `misc/` | 各种辅助脚本，如 `profile.py`（剖析）、`extract_mems.py`（导出存储规格）。 |
| `mkfs/` | 在虚拟块设备上创建文件系统的工具。 |
| `repak/` | 资源打包工具。 |
| `logic_analyzer/` | 逻辑分析仪相关。 |
| `visualizer/` | 可视化工具（配合 `+statetrace` 看线程状态）。 |

关键直觉：**`tools/` 里最该先认识的是 `emulator/`——它是软件开发的主力，也是协同仿真的「参考模型」。**

#### 4.3.2 核心流程

工具与前面几个目录的协作关系：

```
NyuziToolchain (clang + elf2hex)  ──编译──►  program.elf ──► program.hex
                                                                   │
                                  ┌────────────────────────────────┤
                                  ▼                                ▼
                       tools/emulator (nyuzi_emulator)      hardware/core (经 Verilator → nyuzi_vsim)
                                  │                                │
                                  └──────── 协同仿真锁步比对 ───────┘
                                                       (tests/cosimulation)

serial_boot ──经串口──► FPGA (hardware/fpga)
profile.py  ◄── +profile 采样 ◄── nyuzi_vsim / nyuzi_emulator
```

模拟器主程序在 `tools/emulator/main.c`，核心执行循环在 `tools/emulator/processor.c`，外设仿真在 `device.c`/`fbwindow.c`/`sdmmc.c`，协同仿真在 `cosimulation.c`，远程调试在 `remote-gdb.c`。这些会在 u8-l1、u8-l2、u8-l3、u11-l3 逐步精读。

#### 4.3.3 源码精读

模拟器的定位由 `tools/emulator/README.md` 开头一段写定，这段是理解整个 `tools/emulator/` 的钥匙：

[tools/emulator/README.md:L1-L17](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/README.md#L1-L17) —— 说明它是「Nyuzi 指令集模拟器，非周期精确，不模拟流水线与缓存」，并列举三大用途：协同验证的参考模型、软件开发（可接符号调试器）、性能建模；此外还仿真 FPGA/Verilog 环境的外设（视频输出、大容量存储设备）。

它的命令行选项（决定你怎么用这个工具）在同文件稍后：

[tools/emulator/README.md:L22-L37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/README.md#L22-L37) —— 选项表：`-v`（详细打印寄存器传输）、`-m`（模式：normal/cosim/gdb）、`-f`（帧缓冲窗口）、`-d`（dump 内存）、`-b`（挂载虚拟块设备）、`-t`（每核线程数，默认 4）、`-p`（核数，默认 1）等。

模拟器主程序入口与执行核心的真实路径：

- `tools/emulator/main.c` —— 入口、命令行解析、镜像加载。
- `tools/emulator/processor.c` —— 线程调度与指令执行循环。
- `tools/emulator/instruction-set.h` —— 与硬件共享的 ISA 定义（操作码、操作类型枚举）。

> 关键认知：**`instruction-set.h` 是模拟器与硬件「共用同一套 ISA 定义」的桥梁**。硬件侧用 SystemVerilog 的 `defines.svh` 定义操作类型，模拟器侧用 C 的 `instruction-set.h`；两者编码必须一致，否则协同仿真会立刻报错。这是后面 u2（ISA）、u8（模拟器）反复用到的纽带。

#### 4.3.4 代码实践

**实践目标**：定位模拟器的入口与命令行选项，验证「README 选项表 ↔ main.c 实现」的对应关系。

**操作步骤**：

1. 执行 `ls tools/emulator/*.c tools/emulator/*.h`，确认主程序文件清单。
2. 打开 `tools/emulator/main.c`，搜索命令行参数解析（如 `getopt` 或对 `-m`、`-v`、`-t` 的处理），对照 4.3.3 引用的选项表，看每一项是怎么落到代码里的。
3. 打开 `tools/emulator/instruction-set.h`，扫一眼里面的枚举（如 `alu_op_t`、`memory_op_t` 之类），感受「ISA 定义集中在一个头文件」。

**需要观察的现象**：

- `main.c` 里应有对 `-m`/`-v`/`-f`/`-d`/`-b`/`-t`/`-p` 等选项的处理，与 README 选项表一一对应。
- `instruction-set.h` 里会看到大量 `#define` 或 `enum`，把指令操作编码成符号名。

**预期结果**：你能指着 README 选项表里的某一项，说出它在 `main.c` 的哪段代码被解析。这就是「文档与代码对得上」的信心来源。

> 待本地验证：`main.c` 的具体行号与解析方式以你本地代码为准；本实践只要求确认对应关系存在，不要求背诵行号。

#### 4.3.5 小练习与答案

**练习 1**：为什么模拟器「非周期精确、不模拟流水线和缓存」反而有用？

**参考答案**：因为它的三大用途都不需要周期精度：①作为协同验证的参考模型，只需「每条指令的副作用（寄存器/内存写）」正确即可；②软件开发与调试，速度快比时序准更重要；③性能建模靠的是指令/访存统计，而非逐周期时序。周期精确的工作交给 Verilator 编译出的 `nyuzi_vsim`。

**练习 2**：`tools/NyuziToolchain/` 和 `tools/verilator/` 为什么是子模块，而不是直接放在仓库里？

**参考答案**：它们是两个独立的、体量较大的上游项目（基于 LLVM 的工具链、Verilator 仿真器），有自己的版本与发布节奏。用 git submodule 引入，既能锁定兼容版本，又不把庞大源码塞进主仓库。`scripts/setup_tools.sh` 的工作之一就是拉取这两个子模块并编译安装。

### 4.4 测试目录（tests/）

#### 4.4.1 概念说明

`tests/` 存放「验证硬件与软件是否正确的测试」。它是整个项目的「安全网」：每次改设计或代码，跑一遍测试就能知道有没有弄坏东西。`tests/README.md` 开头说它包含「core 本身的测试，以及 tools 和软件库的测试」。

仓库里实际的测试子目录（按 README 描述的五类策略归类）：

| 子目录 | 类别（README 五分法） | 说明 |
|--------|------------------------|------|
| `unit/` | ① 模块级硬件单元/集成测试 | 针对单个 Verilog 模块，可见内部信号、周期精确。 |
| `core/` | ② 系统级定向功能测试 | 覆盖所有主要指令格式/异常类型，自校验。含 `isa`、`mmu`、`trap`、`cache_control`、`perf_counter`、`multicore`。 |
| `cosimulation/` | ③ 约束随机协同仿真 | 随机生成汇编，逐条指令副作用与模拟器比对，多线程压测。 |
| `stress/` | ④ 合成压力测试 | 验证原子访问、MMU 等协同仿真不易覆盖的场景。 |
| `whole-program/`、`kernel/`、`render/` | ⑤ 整机程序测试 | 跑真实程序（哈希、内核、渲染），比对控制台输出。 |
| `csmith/` | ⑤ 整机（随机整程序） | 用 csmith 生成随机 C 程序，跨字宽有已知限制。 |
| 其他 | 工具/专项 | `libc/`、`compiler-rt/`、`tools/`、`device/`、`jtag-debug/`、`float/`、`fpga/`、`fail/`。 |

此外，`tests/` 根下有两个跨目录共享的关键文件：

- **`tests/test_harness.py`**：测试框架，被所有子目录的 `runtest.py` 导入，提供编译、运行、`CHECK`/`CHECKN` 自校验、测试注册等功能。
- **`tests/asm_macros.h`、`tests/one-segment.ld`**：汇编宏与链接脚本，被很多测试程序共用。

关键直觉：**`tests/` 不是一堆散乱的测试，而是一个「五层互补」的验证体系**——单元测试看细节、定向测试保功能、随机测试抓竞态、压力测试补盲区、整机测试验真实场景。这套体系会在 u15 整单元精读。

#### 4.4.2 核心流程

测试如何运行（来自 `tests/README.md`）：

1. 在任意测试子目录执行 `./runtest.py`，会跑该目录下所有测试；加测试名可只跑指定测试，如 `./runtest.py jtag_idcode jtag_bypass`。
2. 默认在 `verilator` 和 `emulator` 两个目标上跑（若测试支持），可用 `--target emulator` 限定，`--list` 列全部，`--debug` 打印诊断输出。
3. 顶层 `make test` 会跑各子项目的测试（CI 用），但有部分测试因耗时/环境原因被跳过。
4. 临时产物（汇编出的二进制等）放在 `tests/work/`。

每个测试本质是一个 Python 函数，用 `@test_harness.test` 装饰器注册，失败时抛 `TestException`。自校验靠 `check_result`：它扫描源文件里的 `CHECK`/`CHECKN` 注释，验证程序输出里是否包含（或不包含）指定字符串。

#### 4.4.3 源码精读

测试目录的范围由 `tests/README.md` 开头界定：

[tests/README.md:L1-L3](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/README.md#L1-L3) —— 说明 `tests/` 包含项目所有测试，既有 core 本身的，也有 tools 与软件库的。

测试体系最精华的部分是「五类测试策略」，这是理解整个 `tests/` 目录布局的总纲：

[tests/README.md:L104-L152](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/README.md#L104-L152) —— 「Test Approach」一节，把验证策略分为五类：①模块级硬件单元/集成测试（`tests/unit`，可见内部信号、周期精确）；②系统级定向功能测试（`tests/core`，覆盖所有指令/异常，自校验）；③约束随机协同仿真（`tests/cosimulation`，逐条比对模拟器，多线程压测，只在 Verilog 仿真跑）；④合成压力测试（`tests/stress`，验证原子/MMU 等）；⑤整机程序测试（`tests/whole-program`/`kernel`/`render`，真实程序比对输出）。

这份 README 还诚实地说出哪些测试不进 CI 及原因，这对理解目录「为什么这样组织」很有帮助：

[tests/README.md:L41-L56](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/README.md#L41-L56) —— 跳过表：`stress/mmu/` 太慢；`tools/lldb/` CI 容器没装 lldb；`core/multicore/` 需要把硬件模型改成 8 核重建；`csmith/` 64 位宿主与 Nyuzi 字宽不同导致校验和不同；`kernel/` 只在模拟器跑（verilator 会挂，issue #119）；`render/` 只在模拟器跑（verilator 太慢）等。

测试框架本体在 `tests/test_harness.py`，它的模块文档字符串就说明了它的定位：

[tests/test_harness.py:L17-L20](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L17-L20) —— 「Utility functions for functional tests. This is imported into test runner scripts in subdirectories under this one.」即它是功能测试的工具函数集合，被所有子目录的测试脚本导入。

#### 4.4.4 代码实践

**实践目标**：定位测试框架，并看清「五类测试」在目录里是如何落地的。

**操作步骤**：

1. 执行 `ls tests/`，把所有子目录和根文件列出来。
2. 对照本讲 4.4.1 的「子目录 ↔ 五类策略」表，给每个子目录贴上类别标签。
3. 打开 `tests/core/isa/`（若存在 `runtest.py`），看它是如何 `import test_harness` 并用 `@test_harness.test` 注册测试的。
4. 在 `tests/README.md` 提到的某个 `CHECK` 用法（如源码注释里写 `// CHECK: ...`）附近，找一个真实测试程序，确认自校验机制确实嵌在源码注释里。

**需要观察的现象**：

- `tests/` 下确实有 `unit/`、`core/`、`cosimulation/`、`stress/`、`whole-program/`、`kernel/`、`render/`、`csmith/` 等子目录，与五类策略一一对应。
- 每个测试子目录里基本都有一个 `runtest.py`，且都会导入 `test_harness`。
- `test_harness.py` 位于 `tests/` 根目录，是所有子目录共享的「单一框架」。

**预期结果**：你能凭目录名判断某个测试属于哪一类策略、为什么要在那个目标（verilator/emulator）上跑。

> 待本地验证：`tests/core/isa/` 的具体测试文件名以你本地为准；若 `runtest.py` 内容与描述略有出入，以本地代码为准。

#### 4.4.5 小练习与答案

**练习 1**：`core/multicore/` 测试为什么默认不进 CI？

**参考答案**：因为它需要把硬件模型改成 8 核并**重新构建**硬件模型才能跑（见 `tests/README.md` 跳过表）。CI 默认是单核构建，重建多核模型既慢又与默认配置不一致，所以单独跳过。这也提示我们：多核是个需要专门构建的配置（u10-l3 会讲）。

**练习 2**：`tests/cosimulation/` 的随机测试为什么「只在 Verilog 仿真跑」，而不能用模拟器代替？

**参考答案**：协同仿真的本质是「让周期精确的硬件模型（Verilator 的 `nyuzi_vsim`）和非周期精确的模拟器（`nyuzi_emulator`）锁步执行，逐条比对指令副作用」。模拟器本身就是「参考模型」一方，不可能自己跟自己比——必须有一个周期精确的硬件实体在另一侧。所以这类测试天然只能在 Verilog 仿真上跑。

## 5. 综合实践

**任务**：亲手绘制一张 Nyuzi 仓库目录树，并在上面完成「五个定位」。

**步骤**：

1. 在仓库根目录执行下面这条命令，拿到所有一级与二级目录（仅目录，不含文件）：

   ```bash
   find . -maxdepth 2 -type d -not -path './.git*' -not -path './NyuziProcessor-tutorial*' | sort
   ```

2. 把输出整理成一棵树，标注每个一级目录（`hardware`/`software`/`tools`/`tests`/`cmake`/`scripts`）的职责。

3. 在树上**标出以下五个关键功能的具体路径**（这是本讲的核心练习，也是后续所有讲义的「导航锚点」）：

   | 功能 | 你应给出的具体路径 |
   |------|--------------------|
   | **L2 缓存** | `hardware/core/l2_cache.sv`（及其 `l2_cache_*_stage.sv`） |
   | **模拟器主程序** | `tools/emulator/main.c`（入口）与 `tools/emulator/processor.c`（执行核心） |
   | **libc** | `software/libs/libc/`（`src/` 实现 + `include/` 头文件） |
   | **渲染库** | `software/libs/librender/`（`RenderContext.cpp`、`Surface.cpp`、`Texture.cpp` 等） |
   | **测试框架** | `tests/test_harness.py` |

4. 最后，用一句话验证你的地图是否画对了：**「我要看 L2 缓存实现，去 `hardware/core/l2_cache.sv`；我要跑模拟器，用 `tools/emulator/` 编出的 `bin/nyuzi_emulator`；我要看 printf 实现，去 `software/libs/libc/src/stdio.c`；我要看渲染入口，去 `software/libs/librender/RenderContext.cpp`；我要加一个测试，去 `tests/` 导入 `test_harness.py`。」** 如果这五句话你都能脱口而出，本讲就过关了。

> 如果某条路径在你本地对不上，先以 `find`/`ls` 的真实输出为准，再回过头检查本讲哪里与实际有出入——这种「文档 vs 实际」的核对本身就是源码阅读的基本功。

## 6. 本讲小结

- 仓库一级目录分四块：`hardware/`（硬件实现）、`software/`（跑在 Nyuzi 上的软件）、`tools/`（宿主开发工具）、`tests/`（验证体系），另加 `cmake/`、`scripts/` 两个辅助目录。
- `hardware/` 内部分三层：`core/`（可综合的 GPGPU 核心，顶层模块 `nyuzi`）、`fpga/`（板级 SoC 与外设）、`testbench/`（仿真假外设）；三者职责分离，学核心不会被外设干扰。
- `software/` 分 `libs/`（libc/libos/librender/libconsole）、`apps/`、`benchmarks/`、`bootrom/`、`kernel/`；库提供积木，应用是成品，bootrom/kernel 是加载与运行基础设施。
- `tools/` 的主角是 `emulator/`（C 指令集模拟器，产物 `bin/nyuzi_emulator`），其 `instruction-set.h` 与硬件 `defines.svh` 共享同一套 ISA 编码；`NyuziToolchain/`、`verilator/` 是两个子模块。
- `tests/` 是「五层互补」的验证体系：unit（单元）、core（定向）、cosimulation（随机协同）、stress（压力）、whole-program/kernel/render（整机）；统一框架是 `tests/test_harness.py`。
- 五个最常用的「导航锚点」：L2 缓存 = `hardware/core/l2_cache.sv`；模拟器 = `tools/emulator/{main,processor}.c`；libc = `software/libs/libc/`；渲染库 = `software/libs/librender/`；测试框架 = `tests/test_harness.py`。

## 7. 下一步学习建议

有了这张地图，建议接下来：

1. **跑通一个程序，把地图「走活」**：进入下一讲 u1-l4「运行第一个程序」，在模拟器里跑 `apps/hello_world`，亲眼看到 `software/` 的程序如何被 `tools/emulator/` 加载、又如何写控制寄存器停机。这会把本讲的静态目录变成动态数据流。
2. **先建立 ISA 与流水线骨架**：跑通程序后，按大纲进入 u2（指令集架构入门）和 u3（硬件顶层与流水线全景），那时你会反复用到本讲的锚点——例如 u3 会带你打开 `hardware/core/nyuzi.sv` 和 `core.sv`。
3. **后续每讲都回到这张地图**：当某讲提到「L2 缓存」「TLB」「渲染」「cosimulation」时，先在本讲地图上定位它属于哪个一级目录、哪个子目录，再深入读源码。养成这个习惯，能让你在大型项目里始终保持方向感。

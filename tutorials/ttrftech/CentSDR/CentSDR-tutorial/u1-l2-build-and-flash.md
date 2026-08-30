# 构建与烧录：把 CentSDR 固件跑起来

## 1. 本讲目标

上一讲我们认识了 CentSDR 是什么、代码如何组织。这一讲解决一个最实际的问题：**这些源码如何变成一块能跑在 STM32F303 芯片里的固件，并且烧进开发板运行起来**。

学完本讲，你应该能够：

1. 安装并配置 `arm-none-eabi` 交叉工具链，理解它和普通 PC 编译器的区别。
2. 读懂 [Makefile](Makefile)：它是如何把 ChibiOS 操作系统、CMSIS-DSP 库和项目自有源码「装配」成一个固件的。
3. 使用 OpenOCD、st-util、Nucleo ST-Link（以及 DFU）这几条通道之一，把编译产物烧录到目标板并验证运行。
4. 即使没有硬件，也能通过 `arm-none-eabi-size` / `arm-none-eabi-objdump` 分析固件的段大小，判断它能否放进 STM32F303 的 Flash 和 RAM。

## 2. 前置知识

本讲会用到几个嵌入式开发的基础概念，先用通俗语言解释清楚：

- **交叉编译（cross compiling）**：你的电脑是 x86-64 架构，而目标芯片是 ARM Cortex-M4。在电脑上编译出「给另一种 CPU 运行的程序」就叫交叉编译。工具链的名字 `arm-none-eabi-` 可以拆开读：`arm`（目标架构）— `none`（不运行操作系统，即裸机）— `eabi`（ARM 嵌入式应用二进制接口）。
- **git 子模块（submodule）**：一个仓库里「钉住」另一个仓库某个固定提交的机制。CentSDR 自己不包含 ChibiOS 的源码，只记录「ChibiOS 仓库的哪个提交」，克隆后必须执行 `git submodule update --init` 才能拿到真正的代码。
- **三种固件产物格式**：
  - `.elf`：带调试信息和符号表的完整可执行文件，gdb 烧录和调试时用它；
  - `.bin`：纯粹的二进制内存镜像，从地址 `0x08000000`（STM32 内部 Flash 的起点）开始逐字节对应；
  - `.hex`：Intel HEX 文本格式，每行带地址校验，很多烧录工具偏爱它。
- **text / data / bss 三个段**：
  - `text`：代码和常量，最终放在 Flash 里；
  - `data`：有初值的全局变量，占用 Flash（存初值）+ RAM（运行时存放）；
  - `bss`：无初值的全局变量，只占 RAM，启动时被清零。
- **SWD 与 ST-Link**：SWD（Serial Wire Debug）是 ARM 芯片的两线调试接口，ST-Link 是 ST 官方的调试探针。OpenOCD 和 st-util 都是「把 SWD 封装成 gdb 远程协议服务」的中间层程序。
- **DFU**：Device Firmware Upgrade，STM32 内置 USB bootloader 支持的固件升级协议，不需要 SWD 探针，只用一根 USB 线。

承接上一讲（u1-l1）的一个结论：**CentSDR 仓库是平铺布局，所有自有源文件的清单就写在 Makefile 的 `CSRC` 变量里**。本讲我们从构建系统的角度再把这条线走一遍。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [Makefile](Makefile) | 构建中枢：工具链设置、引入 ChibiOS/CMSIS 模块、列出项目源文件、提供 `flash` 与 `release` 目标 |
| [build.sh](build.sh) | 构建入口脚本，只是 `make` 的薄包装 |
| [.gitmodules](.gitmodules) | 声明 ChibiOS 子模块的路径与来源仓库 |
| [flash-openocd.gdb](flash-openocd.gdb) | 通过 OpenOCD（端口 3333）烧录的 gdb 脚本 |
| [flash-stutil.gdb](flash-stutil.gdb) | 通过 st-util（端口 4242）烧录的 gdb 脚本 |
| [prog.sh](prog.sh) | 通过 USB DFU 烧录 `build/ch.bin` 的备用脚本 |
| [STM32F303xB.ld](STM32F303xB.ld) | 链接脚本（本讲用于核对 Flash/RAM 容量，深入讲解在 u5-l3） |
| [README.md](README.md) | 官方构建与烧录说明 |

## 4. 核心概念与源码讲解

### 4.1 构建入口与交叉工具链配置

#### 4.1.1 概念说明

嵌入式项目的构建通常由 `make` 驱动：Makefile 描述「用哪个编译器、编译哪些文件、按什么选项编译」。CentSDR 额外提供了一个 [build.sh](build.sh)，它只是把 `make` 包了一层，方便 CI 或一键脚本调用。

「交叉工具链」是本模块的核心概念：编译器本身在你的电脑上运行，但它产出的机器码是 Cortex-M4 的指令。所有工具都用统一前缀 `arm-none-eabi-` 调用（`gcc` 编译、`objcopy` 转换格式、`size` 看段大小、`objdump` 反汇编、`gdb` 调试）。

#### 4.1.2 核心流程

```text
build.sh / make
   │
   ▼
读取 Makefile ──► 确定工具链前缀 TRGT、目标 MCU、编译选项
   │
   ▼
include ChibiOS 的 rules.mk（真正的编译规则）
   │
   ▼
编译所有 CSRC/ASMSRC 中的源文件 ──► 链接（用 LDSCRIPT 指定的 STM32F303xB.ld）
   │
   ▼
输出 build/ch.elf / build/ch.bin / build/ch.hex
```

#### 4.1.3 源码精读

先看入口脚本，全部内容只有 5 行：

[build.sh:1-5](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/build.sh#L1-L5)

它做了两件事：`set -e` 保证任何一步失败立即退出；`set -x` 打印执行的命令方便排查；然后就是 `make`。

再看 Makefile 里「工具是什么」的部分：

[Makefile:176-193](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L176-L193)

这段定义了：目标 CPU 是 `cortex-m4`；所有工具都加 `arm-none-eabi-` 前缀（`TRGT`）；`CP` 是 objcopy，负责把 ELF 转成 `HEX`（`-O ihex`）和 `BIN`（`-O binary`）两种格式；`OD`、`SZ` 分别是反汇编和查段大小的工具——它们在本讲实践中会用到。

编译选项方面有四个值得注意的开关：

[Makefile:7-9](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L7-L9)

`USE_OPT = -O2 -ggdb -fomit-frame-pointer -falign-functions=16`：`-O2` 优化等级（DSP 实时处理必须够快）；`-ggdb` 保留调试信息供 gdb 使用（所以 `.elf` 才能带符号烧录）；`-falign-functions=16` 让函数起始地址 16 字节对齐，配合 Cortex-M4 的取指特性提升性能。

[Makefile:36-39](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L36-L39)

`USE_THUMB = yes`：Cortex-M4 只执行 Thumb 指令集，不是传统的 ARM 指令集，代码密度更高。

[Makefile:60-63](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L60-L63)

`USE_FPU = hard`：STM32F303 带硬件浮点单元（FPU），选择 `hard` 表示浮点运算直接用 FPU 指令、浮点参数也用 FPU 寄存器传递，这是性能最好的方式（配合 [Makefile:216](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L216) 里的 `-DARM_MATH_CM4 -D__FPU_PRESENT -D__FPU_USED` 宏，让 CMSIS-DSP 也走硬件浮点路径）。

最后，真正的编译规则并不在这个 Makefile 里，而是最后一行 include 进来的 ChibiOS 通用规则：

[Makefile:234-235](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L234-L235)

`rules.mk` 是 ChibiOS 提供的通用构建规则（位于子模块内），它负责把上面所有变量翻译成一条条 gcc 命令，并把产物输出到 `build/` 目录。项目名由 [Makefile:86](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L86) 的 `PROJECT = ch` 决定，所以产物是 `build/ch.elf`、`build/ch.bin`、`build/ch.hex`（这也被 `make flash` 间接引用，见 4.3 节）。

#### 4.1.4 代码实践

1. **实践目标**：装好工具链，让 `make` 在本地跑通（或至少理解它为何跑不通）。
2. **操作步骤**：
   - 安装工具链：macOS 可按 README 用 `brew cask install gcc-arm-embedded`（见 [README.md:27](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/README.md#L27)）；Linux 用户发行版包名通常为 `gcc-arm-none-eabi`；装完用 `arm-none-eabi-gcc --version` 验证。
   - 在仓库根目录执行 `./build.sh`（等价于 `make`）。
3. **需要观察的现象**：
   - 如果还没执行 4.2 节的 submodule 初始化，构建会立刻报错找不到 `ChibiOS/os/.../startup_stm32f3xx.mk` 之类的文件——这是预期行为，说明构建系统依赖子模块；
   - submodule 就绪后，终端会滚过一串 `arm-none-eabi-gcc ... -c ...` 命令，最后是链接和两次 objcopy。
4. **预期结果**：`build/` 目录下生成 `ch.elf`、`ch.bin`、`ch.hex` 三个文件。**待本地验证**（本讲义编写环境未安装工具链，未实际执行构建）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `-ggdb` 保留的调试信息不会让烧进 Flash 的固件变大？

**答案**：调试信息放在 ELF 文件的独立调试段里，`objcopy -O binary` 生成 `.bin` 时只会取出属于 Flash 内存映像的段（text/data），调试段被丢弃；而且用 gdb `load` 命令烧录时，gdb 也只把可加载段写入目标内存。

**练习 2**：如果把 `USE_FPU` 从 `hard` 改成 `softfp` 或 `no`，程序还能运行吗？会有什么变化？

**答案**：仍能运行（这只是调用约定和指令选择的差异，需全工程一致地重编）。`no` 时浮点运算由编译器拆成软件浮点库调用，DSP 中用到 `sqrtf` 等浮点运算的地方会明显变慢；`softfp` 用软传参但可用 FPU 指令，介于两者之间。对本项目的 5ms 实时解调周期来说，`hard` 是安全且最快的选择。

**练习 3**：`make flash`（[Makefile:245-247](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L245-L247)）为什么先写 `flash: all` 而不是直接写命令？

**答案**：`all` 是默认目标（完整构建）。把 `flash` 声明为依赖 `all`，保证每次烧录前自动检查产物是否最新、需要时先重新编译，避免把旧固件烧进板子。

### 4.2 源码装配：ChibiOS 子模块、CMSIS-DSP 与项目文件清单

#### 4.2.1 概念说明

CentSDR 的固件 = **RTOS 内核与驱动框架（ChibiOS）** + **定点 DSP 算法库（CMSIS-DSP 的精选子集）** + **项目自有代码（约 20 个 .c 文件）**。三者的来源方式不同：

- ChibiOS 通过 **git 子模块** 引入——第三方代码体量大、独立版本演进，钉住一个已知可用的提交（本仓库钉住的 gitlink 提交为 `fe0ba10`，来源是作者的分叉仓库 `edy555/ChibiOS` 而非官方仓库）；
- CMSIS-DSP 直接以源码形式**复制进了本仓库**的 `CMSIS/` 目录，但只挑了 5 个需要的文件；
- 项目自有代码平铺在根目录，逐个列在 `CSRC`。

Makefile 通过 include 一系列 `.mk` 片段完成「装配」：每个片段把一批源文件路径追加到约定变量（`STARTUPSRC`、`KERNSRC`、`HALSRC`……），最后 `CSRC` 把所有变量汇总。

#### 4.2.2 核心流程

```text
.gitmodules 声明 ChibiOS 子模块
        │  git submodule update --init --recursive
        ▼
ChibiOS/ 目录就位（Makefile 第 89 行 CHIBIOS = ChibiOS）
        │
        ├── include startup_stm32f3xx.mk ──► STARTUPSRC（启动文件、向量表）
        ├── include hal.mk / platform.mk ──► HALSRC / PLATFORMSRC（外设驱动）
        ├── include osal.mk / rt.mk / port_v7m.mk ──► OSALSRC / KERNSRC / PORTSRC（RTOS 内核）
        ├── include streams.mk / shell.mk ──► STREAMSSRC / SHELLSRC（shell 与流）
        ├── include NANOSDR_STM32_F303/board.mk ──► BOARDSRC（板级定义）
        ├── DSPLIBSRC（5 个 CMSIS-DSP 文件）
        └── 项目自有 .c 文件（手写清单）
        │
        ▼
CSRC = 以上全部之和 ──► 交给 rules.mk 编译
```

#### 4.2.3 源码精读

子模块声明只有三行：

[.gitmodules:1-3](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/.gitmodules#L1-L3)

它告诉 git：`ChibiOS` 这个路径是个子模块，代码来自 `edy555/ChibiOS`。克隆主仓库后该目录默认是空的（本讲义的编写环境里就是如此），必须执行 [README.md:37-38](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/README.md#L37-L38) 的 `git submodule update --init --recursive` 才会检出钉住的提交。

Makefile 的装配部分：

[Makefile:88-105](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L88-L105)

注意第 96-97 行：官方的 `boards/NANOSDR_STM32_F303/board.mk` 被注释掉，改用仓库内的本地版本。看这个本地 board.mk：

[NANOSDR_STM32_F303/board.mk:3-7](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/NANOSDR_STM32_F303/board.mk#L3-L7)

它把 `BOARDSRC`/`BOARDINC` 指向项目里的 `NANOSDR_STM32_F303/` 目录（用 `PROJ = .` 拼出相对路径）。也就是说 CentSDR 的板级定义（引脚映射等）放在主仓库里维护，而不是跟着 ChibiOS 子模块走——改板子配置不用动子模块。

CMSIS-DSP 只取了 5 个文件：

[Makefile:111-117](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L111-L117)

分别是：`arm_biquad_cascade_df1_q15.c`（IIR 双二阶级联滤波，SSB/CW/AM 滤波用）、`arm_cfft_radix4_init_q31.c` 与 `arm_cfft_radix4_q31.c`（radix-4 FFT，频谱显示用）、`arm_bitreversal.c`（FFT 位反转）、`arm_common_tables.c`（公共查表数据）。**按需挑选而不是整库编入**，是嵌入式的常见省空间手段——整库会让 text 段显著膨胀。

最后是项目自有源码清单：

[Makefile:121-134](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L121-L134)

第 121-130 行是把上面各 `.mk` 片段累积的变量（`STARTUPSRC`、`KERNSRC`、`PORTSRC`、`OSALSRC`、`HALSRC`、`PLATFORMSRC`、`BOARDSRC`、`STREAMSSRC`、`SHELLSRC`、`DSPLIBSRC`）拼进 `CSRC`；第 131-134 行就是上一讲认识的全部自有文件：USB 配置、本振驱动、编解码器、UI、显示、字库、DSP、主程序、Flash 持久化和自定义启动文件 `crt2.c`。

#### 4.2.4 代码实践

1. **实践目标**：把子模块就位，让「装配」完整成立，并亲手核对源文件清单。
2. **操作步骤**：
   - `git clone https://github.com/ttrftech/CentSDR centsdr && cd centsdr`（见 [README.md:33](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/README.md#L33)）；
   - `git submodule update --init --recursive`；
   - `ls ChibiOS/os/hal` 确认子模块内容已检出；
   - 回到 Makefile 第 131-134 行，给每个自有 `.c` 文件加一条注释，写上你在 u1-l1 学到的职责（例如 `dsp.c /* 六种解调算法 */`）。
3. **需要观察的现象**：submodule 初始化会输出一系列 `Submodule 'ChibiOS' ...` 进度；`git submodule status` 会显示一个 SHA 前缀（应为 `fe0ba10...`）且不带 `+`/`-` 前缀（`-` 表示未检出，`+` 表示检出提交与主仓库记录不一致）。
4. **预期结果**：`make` 能通过「找不到 .mk 文件」这一关，进入真正的编译。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么用子模块钉住 `edy555/ChibiOS` 这个分叉，而不是直接依赖官方 ChibiOS 或把代码复制进主仓库？

**答案**：ChibiOS 官方仓库不含 `NANOSDR_STM32_F303` 这块自定义板子的支持，作者的分叉里加了它（虽然本项目又把 board.mk 覆盖为本地版本）；子模块把「第三方代码的精确版本」记录在主仓库的 git 索引里，任何人任何时间检出都能得到完全相同的构建输入，这是可复现构建的关键。复制进主仓库则会让 diff 噪音巨大、难以跟进上游。

**练习 2**：如果你新写了一个 `myfilter.c` 放在仓库根目录，构建系统能自动发现它吗？

**答案**：不能。`USE_SMART_BUILD = yes`（[Makefile:48-50](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L48-L50)）只是跳过「配置中未用到的模块」以加速，并不会自动扫描新文件。必须手动把 `myfilter.c` 追加到 [Makefile:131-134](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L131-L134) 的清单里——这也是 u5-l4（二次开发）要走的固定步骤之一。

**练习 3**：`DSPLIBSRC` 为什么必须包含看起来「只是个表」的 `arm_common_tables.c`？

**答案**：CMSIS-DSP 的 CFFT 等函数运行时需要库内预计算的特殊因子表（如 twiddle factor），这些表就定义在 `arm_common_tables.c` 里；不链接它会出现「未定义符号」的链接错误。

### 4.3 烧录通道一：OpenOCD 与 st-util 的 gdb 脚本

#### 4.3.1 概念说明

编译产物躺在硬盘上，要进入芯片的 Flash 需要**烧录器**。CentSDR 的主通道是「调试探针 + gdb」：

- **OpenOCD**：开源片上调试器框架，把 ST-Link 探针封装成一个 gdb 远程服务（默认端口 **3333**）；
- **st-util**：stlink 工具集里的轻量 gdb 服务（默认端口 **4242**）；
- **gdb 远程协议**：gdb 通过 TCP 连上上述服务后，`load` 命令会把 ELF 的可加载段写入目标 Flash，`continue` 让芯片开始执行。

两条通道各自对应一个 6-8 行的 gdb 脚本，这是本模块要精读的主角。

#### 4.3.2 核心流程

```text
终端 1：启动服务层                终端 2：gdb 烧录
─────────────────────           ─────────────────────
openocd -f board/stm32f3discovery.cfg
        │ 监听 :3333
或                              arm-none-eabi-gdb -x flash-openocd.gdb
st-util                          │
        │ 监听 :4242             ├─ target extended-remote <端口>
                                 ├─ exec build/ch.elf   # 载入符号
                                 ├─ load                # 写 Flash
                                 └─ continue / quit
```

#### 4.3.3 源码精读

OpenOCD 版脚本全文：

[flash-openocd.gdb:1-9](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash-openocd.gdb#L1-L9)

逐行解释：`target extended-remote :3333` 连接本机 3333 端口的 OpenOCD；`exec build/ch.elf` 把 ELF 的符号与段信息载入 gdb（注意用的是带调试信息的 `.elf`，而不是 `.bin`——gdb 需要知道每段该放到哪个地址）；`load` 执行真正的写入；`continue` 烧完立即让固件跑起来；`quit` 退出。脚本头两行注释提醒你必须先在另一个终端把 OpenOCD 跑起来，OpenOCD 的启动命令是 `openocd -f board/stm32f3discovery.cfg`（见 [README.md:54](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/README.md#L54)，用的是 STM32F3 Discovery 板配置，因为探针接线兼容）。

st-util 版脚本全文：

[flash-stutil.gdb:1-7](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/flash-stutil.gdb#L1-L7)

结构与前者相同，只有两处差异：端口是 **4242**（st-util 的默认端口）；**没有 `continue`**，烧完直接退出，固件留在复位后的状态，下次上电或按复位键才运行。

Makefile 把 st-util 版做成了默认的 `make flash` 目标：

[Makefile:243-247](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L243-L247)

当前生效的是第二行（st-util），第三行 OpenOCD 版被注释着——想切换通道，把两行注释互换即可。README 还记录了不写脚本、直接在 gdb 交互界面手动敲这四条命令的做法：[README.md:58-62](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/README.md#L58-L62)。

#### 4.3.4 代码实践

1. **实践目标**：走通一条 gdb 烧录通道；没有硬件则建立「脚本每一行对应一个动作」的精确理解。
2. **操作步骤**（有硬件）：
   - 用 SWD 线把 ST-Link 探针接到目标板（SWDIO/SWCLK/GND/3V3）；
   - 终端 1：`openocd -f board/stm32f3discovery.cfg`（或 `st-util`）；
   - 终端 2：`arm-none-eabi-gdb -x flash-openocd.gdb --silent`（或 `make flash`）。
3. **操作步骤**（无硬件，源码阅读型实践）：
   - 把 [flash-openocd.gdb](flash-openocd.gdb) 与 [flash-stutil.gdb](flash-stutil.gdb) 并排打开，逐行写出「这行让谁、对什么、做了什么」；
   - 特别思考：为什么两个脚本一个有 `continue` 一个没有？这对「烧完之后板子的状态」意味着什么？
4. **需要观察的现象**（有硬件）：gdb 输出 `Loading section .isr_vector, ... `、`Loading section .text, ...` 等进度，最后 `Start address 0x08000xxx, load size xxxxx`；OpenOCD 版会继续运行，LCD 背光点亮、出现频率显示。
5. **预期结果**：固件在板上运行；串口/USB shell 可应答（下一讲 u1-l4 详述）。**待本地验证**（无目标硬件）。

#### 4.3.5 小练习与答案

**练习 1**：`load` 烧录时为什么用 `.elf` 而不是 `.bin`？

**答案**：`.bin` 只是从起始地址开始的裸字节流，不含「哪段放哪」的信息；`.elf` 里每个段都带目标地址，gdb 据此把 `.isr_vector`、`.text`、`.data` 各自写到正确位置（向量表必须在 `0x08000000`，否则芯片复位后找不到入口）。

**练习 2**：烧录中途拔掉探针，Flash 里会是什么状态？

**答案**：已写入的页是新的、未写到的页保持旧内容，两者拼接成不完整的固件，通常表现为上电无反应或跑飞。因为 STM32 的 Flash 写入按页擦除/半字编程逐块进行，不具备事务性——这也是为什么烧录失败后重烧一遍即可恢复。

**练习 3**：想把默认通道从 st-util 换成 OpenOCD，改哪里？

**答案**：把 [Makefile:246-247](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L246-L247) 中两行互换注释：让 `flash:` 目标执行 `arm-none-eabi-gdb -x flash-openocd.gdb --silent`。

### 4.4 烧录通道二：DFU 与 Nucleo U 盘拖拽，以及 release 打包

#### 4.4.1 概念说明

不是每个人手上都有 ST-Link 探针。STM32 芯片出厂内置了一个 **USB DFU bootloader**：把特定 boot 引脚置高后上电，芯片自己枚举成一个 USB 设备（ST 的 DFU 设备 ID 为 `0483:df11`），此时无需任何调试探针，用一根 USB 线就能升级固件。`dfu-util` 是 PC 端常用的 DFU 客户端工具。

另一条更「平民」的通道：**Nucleo 开发板自带的 ST-Link v2.1** 固件会把目标 Flash 映射成一个 U 盘（大容量存储设备），把 `.bin` 拖进去就完成烧录。

#### 4.4.2 核心流程

```text
通道 A（DFU）：
  boot0 拉高 ──► 上电进入 bootloader ──► USB 枚举 0483:df11
        ──► ./prog.sh
              └─ dfu-util -d 0483:df11 -a 0 -s 0x08000000:leave -D build/ch.bin

通道 B（Nucleo 拖拽）：
  Nucleo 的 ST-Link 枚举为 U 盘 ──► cp build/ch.bin <挂载卷> ──> 拖完自动烧写并运行
```

#### 4.4.3 源码精读

DFU 脚本全文：

[prog.sh:1-3](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/prog.sh#L1-L3)

三个细节值得注意：

1. `DFU_UTIL=../dfu-util/src/dfu-util`：作者假设你在仓库**同级目录**编译了一份 dfu-util。如果你用包管理器安装的 dfu-util，需要把这一行改成 `DFU_UTIL=dfu-util`。
2. `-d 0483:df11`：只匹配 ST 的 DFU 设备（vendor `0483` = STMicroelectronics，product `df11` = DFU 类设备），避免误操作其他 USB 设备。
3. `-s 0x08000000:leave`：把镜像写入从 `0x08000000` 开始的内部 Flash（正是 [STM32F303xB.ld:22](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld#L22) 定义的 flash0 起点），`leave` 表示**写完立即离开 bootloader、跳转执行新固件**——相当于免复位的「烧完就跑」。

Nucleo 通道在 README 里只有两句话：

[README.md:82-84](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/README.md#L82-L84)

把 `build/ch.bin` 复制进 Nucleo 枚举出的 U 盘卷即可（注意用 `.bin`，不是 `.elf`——U 盘烧录方式没有 ELF 解析器，只接受裸镜像）。

最后是发布打包目标：

[Makefile:249-253](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L249-L253)

`make release` 依赖 `all`，把 `build/ch.bin`、`build/ch.hex`、`build/ch.elf` 三件套打进一个以当天日期（`date +%Y%m%d`）命名的 zip。同时提供三种格式，是为了让拿到发布包的用户无论用哪种烧录工具（U 盘拖拽要 bin、ST-LINK Utility 常用 hex、gdb 调试要 elf）都能直接上手。

#### 4.4.4 代码实践

1. **实践目标**：理解并（如有条件）使用免探针烧录通道。
2. **操作步骤**（有 Nucleo 或可进 DFU 的板子）：
   - DFU 路线：按板子说明把 boot0 拉高、上电，`lsusb` 应看到 `STMicroelectronics ... DFU` 设备，然后 `./prog.sh`（若 dfu-util 是系统安装的，先按上文改第一行）；
   - Nucleo 路线：插上 Nucleo，把 `build/ch.bin` 拖入枚举出的 U 盘。
3. **操作步骤**（无硬件，源码阅读型实践）：执行 `make release`（只需工具链，不需要硬件），用 `unzip -l centsdr-*.zip` 查看包内三个文件的体积，并解释三者大小关系。
4. **需要观察的现象**：`.elf` 通常显著大于 `.bin`（调试信息），`.hex` 因文本编码会再大约 2-3 倍。
5. **预期结果**：烧录后板子自动复位运行（`:leave` / Nucleo 拖拽完成即运行）。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`prog.sh` 里的 `:leave` 去掉会有什么不同？

**答案**：写完后芯片停留在 DFU bootloader 里不跳转，表现为「烧完了但板子没反应」，需要手动把 boot0 拉低再复位才运行新固件。`leave` 让 dfu-util 在传输结束时向 bootloader 发送「离开」请求。

**练习 2**：DFU 烧录和 SWD 烧录最本质的能力差别是什么？

**答案**：SWD 是调试接口，除烧录外还能读写寄存器与 RAM、单步、断点（OpenOCD/st-util + gdb 的全部能力）；DFU 只是升级协议，仅能按地址写 Flash（一般也能读/擦除），不能调试。所以开发调试阶段用 SWD 通道，成品升级才用 DFU/拖拽。

**练习 3**：为什么 `DIST_FILES` 里 `.elf` 是必备的，即便最终用户从不需要它？

**答案**：`.elf` 携带符号表和行号信息，是现场调试（gdb 连板子对照源码）与事后分析（addr2line 解析死机地址）的唯一依据；发布它可以让别人在不重新编译的前提下调试你发布的这个精确版本。

## 5. 综合实践

把本讲四个模块串成一个完整流程。**有硬件的读者走 A 线，没有硬件的读者走 B 线（同样覆盖全部知识点）**。

**A 线（有硬件）**：

1. 安装 `arm-none-eabi` 工具链与 openocd（或 stlink）；
2. `git submodule update --init --recursive`，确认 `ChibiOS/` 非空；
3. `make` 产出 `build/ch.elf` / `ch.bin` / `ch.hex`；
4. 任选一条通道烧录（`make flash`、gdb 脚本、DFU、Nucleo 拖拽）；
5. 验证运行：上电后 LCD 应点亮并显示频率；接上 USB 应出现虚拟串口（下一讲用 shell 验证）。

**B 线（无硬件，本讲的核心实践任务）**：

1. 完成上面 1-3 步（编译不需要硬件）；
2. 用 size 工具查看段大小：

   ```sh
   arm-none-eabi-size build/ch.elf
   ```

3. 记录 `text`、`data`、`bss` 三个数值（单位字节）；
4. 对照链接脚本里的内存定义做容量判断：

   [STM32F303xB.ld:20-38](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld#L20-L38)

   | 段 | 落在哪 | 容量约束 |
   |----|--------|----------|
   | text + data（初值） | flash0：`0x08000000`，128k | \( (text + data) < 131072 \) |
   | data + bss + 栈 + 堆 | ram0：`0x20000000`，40k | 总和 \( < 40960 \) |
   | （可选）CCM | ram4：`0x10000000`，8k | 本讲先不涉及，u5-l3 详述 |

5. 用 objdump 看看最大的几个符号住在哪里：

   ```sh
   arm-none-eabi-objdump -h build/ch.elf        # 各段地址与大小
   arm-none-eabi-nm --size-sort build/ch.elf | tail -20   # 最大的 20 个符号
   ```

6. 把结果写成一张小表，回答：Flash 用掉了百分之几？RAM 放得下吗？`bss` 里最大的符号是什么（大概率是 DSP 缓冲区和显示缓冲，结合 u1-l1 的 `nanosdr.h` 印证你的猜测）？

**预期结果**（B 线）：能明确说出「固件共占 Flash X 字节 / 128k，RAM Y 字节 / 40k，其中最大的符号是 Z」。具体数值**待本地验证**——不同工具链版本编译出的体积略有差异，这正是你要亲自测量的原因。

## 6. 本讲小结

- CentSDR 的构建由 [Makefile](Makefile) 驱动，入口脚本 [build.sh](build.sh) 只是 `make` 的包装；全部工具带 `arm-none-eabi-` 前缀，目标是 Cortex-M4 + 硬件浮点（`USE_FPU = hard`）+ Thumb 指令集。
- 源码三来源：ChibiOS 子模块（[.gitmodules](.gitmodules) 钉住 `edy555/ChibiOS` 的提交 `fe0ba10`，克隆后必须 `git submodule update --init --recursive`）、CMSIS-DSP 精选的 5 个文件（`DSPLIBSRC`）、以及平铺在根目录、逐个列在 `CSRC` 的项目自有代码。
- 主烧录通道是「探针 + gdb 脚本」：[flash-openocd.gdb](flash-openocd.gdb) 走 OpenOCD 的 3333 端口且烧完 `continue`；[flash-stutil.gdb](flash-stutil.gdb) 走 st-util 的 4242 端口、烧完即退出；`make flash` 默认使用后者。
- 免探针通道：[prog.sh](prog.sh) 用 dfu-util 经 USB DFU（设备 `0483:df11`）把 `ch.bin` 写到 `0x08000000` 并 `:leave` 跳转运行；Nucleo 板载 ST-Link 则支持直接把 `.bin` 拖进 U 盘。
- `make release` 把 `.bin/.hex/.elf` 三种格式打包成日期命名的 zip，覆盖 U 盘拖拽、烧录工具、gdb 调试三类用户的需求。
- 没有硬件也能完成本讲实践：`arm-none-eabi-size`/`objdump`/`nm` 分析段大小，对照 [STM32F303xB.ld](STM32F303xB.ld) 的 128k Flash / 40k RAM 判断容量。

## 7. 下一步学习建议

固件已经能跑起来了，下一讲 **u1-l3「固件入口：main() 初始化流程逐行走读」**将打开 [main.c](main.c)，看 `halInit`、`chSysInit` 之后各外设按什么顺序被拉起、用户配置如何从 Flash 恢复、三个线程如何各就各位——那是理解整个固件运行时结构的钥匙。

若你对本讲出现的链接脚本意犹未尽（128k Flash 怎么分页、`0x0801f800` 配置页为什么在末页、CCM RAM 是什么），可以提前浏览 [STM32F303xB.ld](STM32F303xB.ld)，完整讲解安排在 **u5-l3「内存的版图：链接脚本与启动文件」**。烧录验证用到的 USB 虚拟串口与 shell 命令，则在 **u1-l4** 详细展开。

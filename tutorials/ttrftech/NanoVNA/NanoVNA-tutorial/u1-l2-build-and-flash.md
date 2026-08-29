# 搭建工具链：编译、烧录与 CI 流程

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `make` 一条命令背后发生了什么：ChibiOS 子模块、板级文件、链接脚本、业务源码是如何被拼装成 `build/ch.bin` 的。
2. 在自己的电脑上（哪怕没有安装任何 ARM 工具链）用 docker 镜像 `edy555/arm-embedded:8.2` 完成一次完整编译，并读懂 `arm-none-eabi-size` 输出的 Flash/RAM 占用。
3. 理解 `VERSION` 字符串是如何从 make 变量一路进入固件的 `Config→Version` 菜单和 `version` shell 命令的，并能自己改一个版本号验证。
4. 掌握 DFU 模式的进入方式与 `dfu-util` 烧录命令的含义，看懂 CircleCI 是如何自动构建并把固件发布到 GitHub Release 的。

## 2. 前置知识

在动手之前，先澄清几个本讲会用到的概念。上一讲（u1-l1）已经知道 NanoVNA 固件跑在 STM32F072（Cortex-M0）上，本讲解决"怎么把源码变成机器里的固件"。

- **交叉编译（cross compiling）**：你的电脑是 x86-64 架构，目标芯片是 Cortex-M0 架构，两者的指令集完全不兼容。所以不能用电脑自带的 `gcc`，必须用 `arm-none-eabi-gcc` 这类"在 x86 上运行、生成 ARM 机器码"的工具链。`none-eabi` 的意思是目标系统没有操作系统、没有标准 C 运行环境（嵌入式裸机）。
- **Make 与 Makefile**：`make` 是一个根据"依赖关系"决定编译顺序的工具。Makefile 里定义了源文件列表、编译选项、链接脚本，`make` 会按规则把 `.c` 编成 `.o`，再链接成 `.elf`，最后转出 `.hex`/`.bin`。
- **RTOS 与 HAL**：ChibiOS 是一个实时操作系统（RTOS），提供线程、信号量、USB 协议栈；HAL（Hardware Abstraction Layer，硬件抽象层）是它对 STM32 外设（I2C、SPI、ADC……）的统一封装。这些代码不在本仓库里，而是以 **git 子模块（submodule）** 形式引用。
- **git 子模块**：一个仓库里"钉住"另一个仓库某个提交的引用。克隆本仓库后 `ChibiOS/` 目录是空的，必须执行 `git submodule update --init` 才有内容，否则 `make` 第一步就会报找不到 `.mk` 文件。
- **DFU（Device Firmware Upgrade）**：USB 协议栈里专门用来升级固件的标准类。STM32 出厂内置了一段 USB DFU bootloader，上电时拉高 BOOT0 引脚（或在固件菜单里选择）即进入该模式，此时芯片表现为一个 USB DFU 设备，主机用 `dfu-util` 就能把固件写进内部 Flash，不需要任何外部编程器。
- **链接脚本（linker script）**：告诉链接器"代码放哪个地址、变量放哪个地址、Flash 和 RAM 各有多大"的说明书。嵌入式开发中它直接决定了程序能否正确启动。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Makefile](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile) | 构建中枢：编译选项、源文件清单、ChibiOS 模块引入、`flash`/`dfu` 快捷目标 |
| [README.md](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/README.md) | 官方构建说明：工具链安装、docker 构建、DFU 烧录步骤 |
| [.circleci/config.yml](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/.circleci/config.yml) | CircleCI 流水线：自动编译、清理、打 tag 时发布 GitHub Release |
| [STM32F072xB.ld](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/STM32F072xB.ld) | 链接脚本：划分 96K 程序 Flash、32K 校准数据 Flash、16K RAM |
| [prog.sh](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/prog.sh) | 早期烧录脚本，是 `make flash` 的雏形 |
| [.gitmodules](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/.gitmodules) | 声明 ChibiOS 子模块的来源与分支 |
| `NANOVNA_STM32_F072/board.mk` | 板级支持包清单：告诉构建系统 board.c/board.h 在哪里 |
| [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c) | 本讲只看一小段：`VERSION` 宏如何变成屏幕上/串口里的版本号 |

## 4. 核心概念与源码讲解

### 4.1 Makefile 构建系统

#### 4.1.1 概念说明

`Makefile` 是整个项目的构建入口。它要回答四个问题：

1. **用什么编译**——工具链前缀 `arm-none-eabi-`、目标架构 `cortex-m0`。
2. **编译什么**——ChibiOS 的启动代码/内核/HAL + 本项目的 12 个 `.c` 业务文件。
3. **怎么编译**——`-O2` 优化、`--specs=nano.specs` 精简 C 库等选项。
4. **链接到哪**——`STM32F072xB.ld` 链接脚本。

注意一个贯穿全文件的习惯写法：所有选项都包在 `ifeq ($(USE_OPT),)` 里，意思是"如果外部没传，就用这里的默认值"。这让你可以在命令行上临时覆盖任何选项，而不用改文件——后面改 `VERSION` 就是用的这个机制。

#### 4.1.2 核心流程

在项目目录敲下 `make` 后的完整流程：

```text
make
 ├─ 读取 Makefile，确定 PROJECT=ch、CHIBIOS=ChibiOS
 ├─ 依次 include 8 个 ChibiOS .mk 文件
 │    （启动代码、内核、HAL、STM32F0 平台、板级、OSAL、RTOS、流式 IO）
 │    → 得到 STARTUPSRC/KERNSRC/HALSRC... 等源文件清单
 ├─ 把业务源文件追加进 CSRC
 ├─ include ChibiOS 的 rules.mk → 获得编译/链接/格式转换规则
 ├─ 对每个 .c：arm-none-eabi-gcc -mthumb -O2 ... -c → build/obj/*.o
 ├─ 链接：arm-none-eabi-gcc -T STM32F072xB.ld ... → build/ch.elf
 └─ 格式转换：objcopy → build/ch.hex、build/ch.bin
```

产物统一输出在 `build/` 目录（`build/obj` 放目标文件、`build/lst` 放反汇编清单——CI 脚本里会清理这两个子目录，可以反推出来）。

#### 4.1.3 源码精读

**① 编译选项：为小内存 Cortex-M0 量身定制**

[Makefile:L7-L9](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L7-L9) 定义了核心编译选项：

```make
USE_OPT = -O2 -fno-inline-small-functions -ggdb -fomit-frame-pointer -falign-functions=16 --specs=nano.specs -fstack-usage
```

逐项理解：

| 选项 | 目的 |
| --- | --- |
| `-O2` | 较高优化等级，Cortex-M0 主频只有 48MHz，性能和体积都靠它 |
| `-fno-inline-small-functions` | 禁止小函数内联——内联会复制代码，Flash 紧张时得不偿失 |
| `-ggdb` | 保留调试信息，便于用 GDB 调试 |
| `-fomit-frame-pointer` | 省掉栈帧指针寄存器，多出一个通用寄存器 |
| `-falign-functions=16` | 函数 16 字节对齐，取指更快 |
| `--specs=nano.specs` | 使用 newlib-nano 精简版 C 库，`printf` 家族体积大幅缩小 |
| `-fstack-usage` | 为每个源文件生成 `.su` 栈使用报告（第 5 单元资源分析会用到） |

**② 栈大小：Cortex-M 的两类栈**

[Makefile:L66-L74](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L66-L74)：

```make
USE_PROCESS_STACKSIZE = 0x200
USE_EXCEPTIONS_STACKSIZE = 0x200
```

Cortex-M 有两个栈：**主栈（MSP）**给中断/异常用（`EXCEPTIONS_STACKSIZE`），**进程栈（PSP）**给 `main()` 线程用（`PROCESS_STACKSIZE`）。各 512 字节——16K RAM 的机器上每一字节都要精打细算。

**③ 工具链与目标架构**

[Makefile:L167-L190](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L167-L190)：

```make
MCU  = cortex-m0
TRGT = arm-none-eabi-
TOPT = -mthumb -DTHUMB
```

`MCU = cortex-m0` 决定了 `-mcpu` 参数；`TOPT = -mthumb` 表示全部用 Thumb 指令集（M0 只支持 Thumb，这是必选项）。后面 `SZ = $(TRGT)size` 说明查看体积就用 `arm-none-eabi-size`。

**④ 链接数学库与用户宏**

[Makefile:L207](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L207) 和 [Makefile:L219](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L219)：

```make
UDEFS = -DSHELL_CMD_TEST_ENABLED=FALSE -DSHELL_CMD_MEM_ENABLED=FALSE -DARM_MATH_CM0 -DVERSION=\"$(VERSION)\"
ULIBS = -lm
```

`UDEFS` 是传给所有源文件的 C 宏：关掉 shell 的 test/mem 命令、告诉 CMSIS-DSP 当前是 CM0 内核，而最关键的 `-DVERSION=\"$(VERSION)\"` 把 make 变量 `VERSION` 注入成 C 字符串宏——这是本讲实践的伏笔。`-lm` 链接数学库，FFT、`sqrtf` 都需要它。

**⑤ 一切的拼装点：CSRC 源文件清单**

[Makefile:L117-L126](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L117-L126)：

```make
CSRC = $(STARTUPSRC) \
       $(KERNSRC) \
       $(PORTSRC) \
       $(OSALSRC) \
       $(HALSRC) \
       $(PLATFORMSRC) \
       $(BOARDSRC) \
       $(STREAMSSRC) \
       usbcfg.c \
       main.c si5351.c tlv320aic3204.c dsp.c plot.c ui.c ili9341.c numfont20x22.c Font5x7.c flash.c adc.c
```

前 8 个变量来自下面 4.2 节 include 进来的 ChibiOS 模块；后半段就是上一讲"项目地图"里的全部业务源文件——**以后想给固件加新源文件，就是改这一行**。`PROJECT = ch`（[Makefile:L89](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L89)）决定了产物名叫 `build/ch.elf`、`build/ch.bin`。

**⑥ VERSION：从 git tag 到固件字符串**

[Makefile:L56-L58](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L56-L58)：

```make
ifeq ($(VERSION),)
  VERSION="$(shell git describe --tags)"
endif
```

没显式传 `VERSION` 时，取 `git describe --tags` 的输出（形如 `1.0.68-10-g1a2b3c4`，即"最近 tag + 之后提交数 + 哈希"）。它经 `UDEFS` 变成 `-DVERSION="..."` 宏后，在 [main.c:L2018-L2022](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2018-L2022) 落地：

```c
#ifndef VERSION
#define VERSION "unknown"
#endif

const char NANOVNA_VERSION[] = VERSION;
```

如果编译时没传宏，兜底为 `"unknown"`。这个字符串最终出现在两处：`Config→Version` 菜单的 `info_about[]` 数组（[main.c:L91-L104](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L91-L104)，`"Version: " VERSION` 与 `"Build Time: " __DATE__ " - " __TIME__` 拼在一起），以及 `version` shell 命令（[main.c:L2024-L2029](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2024-L2029)）。

#### 4.1.4 代码实践：docker 编译并记录体积

**实践目标**：不改任何源码，用官方 docker 镜像完成一次完整编译，拿到固件体积基线，供后续讲义对比。

**操作步骤**：

```bash
# 1. 克隆并初始化子模块（没有子模块 make 必失败，原因见 4.2）
git clone https://github.com/ttrftech/NanoVNA.git
cd NanoVNA
git submodule update --init --recursive

# 2. 用 docker 镜像编译（无需本机安装任何 ARM 工具链）
docker run -it --rm -v $(PWD):/work edy555/arm-embedded:8.2 make

# 3. 查看体积（宿主机若装了工具链可直接跑；否则继续借 docker）
docker run -it --rm -v $(PWD):/work edy555/arm-embedded:8.2 arm-none-eabi-size build/ch.elf

# 4. 确认产物
ls -l build/ch.bin build/ch.hex build/ch.elf
```

**需要观察的现象**：

- `arm-none-eabi-size` 输出四列 `text data bss dec`。**Flash 占用 = text + data**（代码 + 已初始化数据，后者也要存在 Flash 里）；**RAM 占用 = data + bss**（可写变量）。
- 对照下一节链接脚本的限额：Flash 程序区只有 96K、RAM 只有 16K，看看固件离上限还有多少余量。

**预期结果**：编译在数分钟内完成，`build/ch.bin` 生成；体积数字与你的本机构建环境、git tag 状态有关，**具体数值待本地验证**，建议先记录下来作为基线：

| 指标 | 你的测量值 | 上限 |
| --- | --- | --- |
| Flash（text+data） | 待本地验证 | 96K（程序区） |
| RAM（data+bss） | 待本地验证 | 16K |

**⑤ 补充观察**：`build/obj/` 下应能看到每个 `.c` 对应的 `.o` 和 `.su` 文件（`-fstack-usage` 的产物）。

#### 4.1.5 小练习与答案

**练习 1**：为什么这个项目要用 `--specs=nano.specs`，而去掉它固件可能放不下？

**答案**：默认的 newlib 把 `printf`、文件 IO 等完整实现都链进来，Cortex-M0 只有 96K 程序 Flash 装不下；nano 版是面向嵌入式的精简实现（更小的 printf、更省内存的 malloc），是小型固件的标配。

**练习 2**：`USE_PROCESS_STACKSIZE` 和 `USE_EXCEPTIONS_STACKSIZE` 分别给谁用？为什么不能合并成一个？

**答案**：前者是 `main()` 所在线程使用的进程栈（PSP），后者是中断和异常处理使用的主栈（MSP）。中断可能在任何线程代码处异步打断，必须有自己独立的栈，否则中断会把被打断线程的栈数据踩坏。

**练习 3**：不修改 Makefile，如何在命令行把优化等级降到 `-O0` 方便调试？

**答案**：利用 `ifeq ($(USE_OPT),)` 的外部覆盖机制：`make USE_OPT="-O0 -ggdb --specs=nano.specs"`（docker 场景则是 `docker run -it --rm -v $(PWD):/work edy555/arm-embedded:8.2 make USE_OPT="-O0 -ggdb --specs=nano.specs"`）。注意要保留 `--specs=nano.specs`，否则可能超 Flash。

### 4.2 ChibiOS 子模块与板级支持包

#### 4.2.1 概念说明

上一讲说过固件跑在 ChibiOS RTOS 上，但你在仓库里搜不到线程调度代码——它在 `ChibiOS/` 子模块里。子模块的本质是"把另一个 git 仓库的某个提交钉进本仓库"，好处是固件与 RTOS 版本可以独立演进、分别提交。

特别注意：NanoVNA 用的不是 ChibiOS 官方仓库，而是作者的 **fork、`I2SFULLDUPLEX` 分支**——因为官方 HAL 不支持 I2S 全双工 DMA，而音频采集恰恰需要它（u2-l3 会用到）。这解释了为什么不能随手换成官方 ChibiOS。

**板级支持包（BSP）** `NANOVNA_STM32_F072/` 则声明"这块板子叫什么、LED 和按键接在哪些引脚、外设初始电平是什么"。它在本仓库里而不在 ChibiOS 里，因为这块板子是 NanoVNA 自制的。

#### 4.2.2 核心流程

构建系统通过一条 include 链把 ChibiOS 各层"装配"起来：

```text
Makefile
  ├─ include ChibiOS/.../startup_stm32f0xx.mk   → 启动文件（向量表、复位入口）→ STARTUPSRC
  ├─ include ChibiOS/.../hal.mk                 → HAL 核心 → HALSRC
  ├─ include ChibiOS/.../STM32F0xx/platform.mk  → STM32F0 外设驱动 → PLATFORMSRC
  ├─ include NANOVNA_STM32_F072/board.mk        → 本板 board.c → BOARDSRC、BOARDINC
  ├─ include ChibiOS/.../osal/rt/osal.mk        → RTOS 适配层 → OSALSRC
  ├─ include ChibiOS/.../rt/rt.mk + port_v6m.mk → 内核 + M0 端口 → KERNSRC/PORTSRC
  └─ include ChibiOS/.../streams.mk             → chprintf 等流式 IO → STREAMSSRC
```

#### 4.2.3 源码精读

**① 子模块声明：指向 fork 的特殊分支**

[.gitmodules:L1-L4](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/.gitmodules#L1-L4)：

```ini
[submodule "ChibiOS"]
	path = ChibiOS
	url = https://github.com/edy555/ChibiOS.git
	branch = I2SFULLDUPLEX
```

三个字段分别是：挂载路径、来源仓库、跟踪分支。官方 README 的获取步骤与之对应：[README.md:L43-L49](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/README.md#L43-L49) 要求 `git submodule update --init --recursive`。

**② 板级文件挂载**

[NANOVNA_STM32_F072/board.mk:L1-L7](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/NANOVNA_STM32_F072/board.mk#L1-L7)：

```make
BOARDSRC = ${PROJ}/NANOVNA_STM32_F072/board.c
BOARDINC = ${PROJ}/NANOVNA_STM32_F072
```

只做两件事：把 `board.c` 加进编译清单、把该目录加进头文件搜索路径。而被注释掉的 `ST_STM32F072B_DISCOVERY` 行说明这套构建体系换一块板只需换一个 board.mk——这是 ChibiOS 工程的标准做法。

**③ include 链在 Makefile 中的位置**

[Makefile:L96-L108](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L96-L108)：

```make
include $(CHIBIOS)/os/common/startup/ARMCMx/compilers/GCC/mk/startup_stm32f0xx.mk
include $(CHIBIOS)/os/hal/hal.mk
include $(CHIBIOS)/os/hal/ports/STM32/STM32F0xx/platform.mk
#include $(CHIBIOS)/os/hal/boards/ST_STM32F072B_DISCOVERY/board.mk
include NANOVNA_STM32_F072/board.mk
include $(CHIBIOS)/os/hal/osal/rt/osal.mk
include $(CHIBIOS)/os/rt/rt.mk
include $(CHIBIOS)/os/common/ports/ARMCMx/compilers/GCC/mk/port_v6m.mk
include $(CHIBIOS)/os/hal/lib/streams/streams.mk
```

所有路径都基于 `CHIBIOS = ChibiOS`（[Makefile:L93](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L93)）——这就是"子模块没初始化就编译失败"的原因：这些 `.mk` 文件根本不存在。注意 `port_v6m.mk` 中的 v6m 即 ARMv6-M，正是 Cortex-M0 的架构名。

**④ 链接脚本与构建规则**

[Makefile:L112-L113](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L112-L113) 指定链接脚本（本来可以从 ChibiOS 的 `STARTUPLD` 目录取同款，作者选择用仓库根目录这份以便修改）；[Makefile:L225-L226](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L225-L226) `include $(RULESPATH)/rules.mk` 是最后一块拼图——ChibiOS 通用的编译规则（把 `.c` 变 `.o`、链接、objcopy）全部来自它，所以整个 Makefile 里看不到一条显式的编译命令。

#### 4.2.4 代码实践：注入自定义版本号并验证

**实践目标**：亲眼看一次"make 变量 → C 宏 → 固件字符串"的完整链路。

**操作步骤**：

```bash
# 1. 用自定义版本号重新编译（覆盖 Makefile 里 git describe 的默认值）
docker run -it --rm -v $(PWD):/work edy555/arm-embedded:8.2 make VERSION=\"mytest-1.0\"

# 2. 在 ELF 里搜版本字符串（bin 是纯机器码没有符号，要用 elf 查）
docker run -it --rm -v $(PWD):/work edy555/arm-embedded:8.2 \
    sh -c 'strings build/ch.elf | grep -i mytest'
```

说明：`VERSION=\"mytest-1.0\"` 的转义引号经过 `UDEFS` 的 `-DVERSION=\"$(VERSION)\"`（[Makefile:L207](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L207)）最终变成 C 字符串宏。如果 shell 转义写起来别扭，`make VERSION=mytest-1.0`（不带引号）在本项目中同样有效。

**需要观察的现象**：`strings` 应至少命中两类字符串——裸版本号 `mytest-1.0`（来自 `NANOVNA_VERSION[]`，[main.c:L2022](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2022)）和拼接后的 `Version: mytest-1.0`（来自 `info_about[]`，[main.c:L96](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L96)）。因为相邻的字符串字面量会被编译器合并存放。

**预期结果**：两条（或以上）匹配输出。**有真机的读者**可继续 `make flash` 烧录，在屏幕 `Config→Version` 菜单看到 `Version: mytest-1.0`，或 USB 串口终端里敲 `version` 命令验证（shell 命令细节在第 5 单元讲）。无真机则 `strings` 验证即为完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 NanoVNA 用作者自己的 ChibiOS fork 而不用官方版？

**答案**：fork 的 `I2SFULLDUPLEX` 分支添加了 I2S 全双工 DMA 支持，NanoVNA 依赖它同时采样参考/采样两路音频信号（u2-l3 详述）；官方 HAL 缺这一能力，等官方合入前只能先用 fork。

**练习 2**：本地开发用 `git submodule update --init --recursive`，而 CI（下一节）用 `git submodule update --remote`，两者语义差在哪？

**答案**：`--init` 检出到主仓库记录的固定提交，保证可复现；`--remote` 拉取 `.gitmodules` 中 `branch = I2SFULLDUPLEX` 分支的**最新**提交。所以 CI 产物可能比你本地按记录提交构建的更新——排查"CI 固件和本地行为不一样"时先想到这一点。

**练习 3**：如果要把固件移植到另一块 STM32F072 板子，最少要动哪些文件？

**答案**：新建一个板级目录（仿照 `NANOVNA_STM32_F072/` 的 `board.c/board.h/board.mk`，按新板原理图改引脚定义），然后把 [Makefile:L101](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L101) 的 include 换成新板级路径。其余 ChibiOS 层不用动。

### 4.3 链接脚本 STM32F072xB.ld 与内存布局

#### 4.3.1 概念说明

链接脚本回答"什么东西放在哪个地址"。STM32F072（B 型）共有 **128K Flash + 16K RAM**。这份脚本做了件值得注意的事：把 Flash 切成两段——前 96K 放程序，最后 32K（`0x08018000` 起）留作**校准数据保存区**，即上一讲提到的 `flash.c` 掉电存储的目标区域（u3-l4 详读）。这样保存校准不会破坏程序，程序升级也不会抹掉校准。

#### 4.3.2 核心流程

地址空间分配图：

```text
0x08000000 ┌────────────────────────┐
           │  flash0：程序区 96K     │ 向量表、代码、常量、data 的初值
0x08018000 ├────────────────────────┤
           │  flash7：校准区 32K     │ .calsave 段（NOLOAD，不写镜像）
0x08020000 └────────────────────────┘
0x20000000 ┌────────────────────────┐
           │  ram0：16K             │ data + bss + 堆 + 双栈
0x20004000 └────────────────────────┘
```

#### 4.3.3 源码精读

**① MEMORY：芯片资源的账本**

[STM32F072xB.ld:L20-L38](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/STM32F072xB.ld#L20-L38)：

```
MEMORY
{
    flash0  : org = 0x08000000, len = 96k
    ...
    flash7  : org = 0x08018000, len = 32k
    ram0    : org = 0x20000000, len = 16k
    ...
}
```

`org`（origin）是起始地址，`len` 是长度。验算：`0x08000000 + 96k = 0x08018000`，恰好是 flash7 的起点；两段相加 128K，与芯片手册一致。

**② REGION_ALIAS：把逻辑段名映射到物理区**

[STM32F072xB.ld:L44-L79](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/STM32F072xB.ld#L44-L79) 用别名把向量表、代码、只读数据等逻辑段全部指到 flash0，RAM 相关段指到 ram0。其中最特殊的一行是 [STM32F072xB.ld:L66-L67](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/STM32F072xB.ld#L66-L67)：

```
/* Flash region to be saved calibration data */
REGION_ALIAS("CALDATA_FLASH", flash7);
```

**③ .calsave 段：校准数据的家**

[STM32F072xB.ld:L90-L96](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/STM32F072xB.ld#L90-L96)：

```
SECTIONS
{
    .calsave (NOLOAD) : ALIGN(4)
    {
        *(.calsave)
    } > CALDATA_FLASH
}
```

`NOLOAD` 的含义是：这个段**不占用固件镜像**——`ch.bin` 里没有它，烧录时也不会被覆盖，只有固件运行时由 `flash.c` 主动写入。`ALIGN(4)` 对齐到 4 字节是因为 STM32 的 Flash 编程按半字（16 位）进行、擦除按页进行，对齐是硬件要求。

**④ 历史脚本 prog.sh**

[prog.sh:L1-L3](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/prog.sh#L1-L3)：

```sh
DFU_UTIL=../chibios-stm/dfu-util/src/dfu-util
$DFU_UTIL -d 0483:df11 -a 0 -s 0x08000000:leave -D build/ch.bin
```

早期作者要自己编译 dfu-util 才能烧录；现在这条命令已原样搬进 `make flash`（见 4.4），此脚本只是留档。

#### 4.3.4 代码实践：核对固件没有越界

**实践目标**：把 4.1 编译出的 `build/ch.elf` 与链接脚本的限额对照，确认程序区没超。

**操作步骤**：

```bash
# 查看各段的最终地址与大小
docker run -it --rm -v $(PWD):/work edy555/arm-embedded:8.2 \
    sh -c 'arm-none-eabi-size build/ch.elf && arm-none-eabi-objdump -h build/ch.elf | head -30'
```

**需要观察的现象**：

1. `arm-none-eabi-size` 的 `text+data` 合计应远小于 96K（`0x18000`）。
2. `objdump -h` 的段表中应找不到 `.calsave`（因为 NOLOAD 段在未运行时长度为 0），而 `.isr_vector`（向量表）的地址应从 `0x08000000` 附近开始——这正是芯片上电后取前 8 个字（初始栈指针 + 复位向量）的地方。

**预期结果**：`.text` 起始地址约 `0x08000000`，`.data`/`.bss` 落在 `0x20000000` 之后的 16K 内。若 `.text+.data` 真超过 96K，链接器会报 `region 'flash0' overflowed` 错误。具体段地址**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么把校准数据放在 `0x08018000` 而不是紧跟在程序后面？

**答案**：程序大小随版本变化，若校准区紧随其后地址会漂移；固定在最后一页起，程序升级重烧 `0x08000000` 开始的镜像时不会擦到校准区（`dfu-util` 只写入镜像覆盖的范围），用户的校准数据得以保留。

**练习 2**：`.calsave` 段的 `NOLOAD` 如果去掉会发生什么？

**答案**：该段会进入固件镜像，`ch.bin` 体积变大，且每次烧录/升级都会把校准区写成镜像里的初始值——相当于每次升级都清空用户校准。

**练习 3**：`USE_PROCESS_STACKSIZE = 0x200` 和 `USE_EXCEPTIONS_STACKSIZE = 0x200`（合计 1K）最终落在链接脚本的哪个内存区？

**答案**：ram0（16K）。它们经 ChibiOS 的 `rules.ld`（被 [STM32F072xB.ld:L88](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/STM32F072xB.ld#L88) `INCLUDE rules.ld` 引入）展开成 `_stacks` 区域的一部分，与 data/bss 共享同一个 16K RAM——这也解释了 4.1 实践里 RAM 占用为何寸土寸金。

### 4.4 DFU 烧录与 CircleCI 流水线

#### 4.4.1 概念说明

编译出的 `build/ch.bin` 要进入芯片的 Flash 有两条路：SWD/JTAG 编程器（要额外硬件），或 **STM32 内置的 USB DFU bootloader**（只要一根 USB 线）。后者是官方推荐方式。

让芯片进入 DFU 模式的方法（README 明确列出）：

- 上电时用跳线把 **BOOT0 引脚拉高**（硬件方式，任何固件版本都适用）；
- 或在正常运行固件的 **Config→DFU 菜单**里选择（固件自杀式复位进 bootloader，见下文 `make dfu`）。

CI（持续集成）方面，项目用 CircleCI：每次 push 自动编译并把 `build/` 存为构建产物；当推送形如 `1.2.34` 的 **git tag** 时，再把 `.bin/.hex/.elf` 打包发布到 GitHub Release。你从 Release 页下载的固件就是这么来的。

#### 4.4.2 核心流程

```text
push 代码 / 推 tag
 └─ CircleCI workflow "main"
     ├─ job: build（仅 tag 过滤条件见下）
     │   ├─ docker 镜像 edy555/arm-embedded:8.2（和本地构建完全同源！）
     │   ├─ git submodule update --remote
     │   ├─ make
     │   ├─ rm -r build/obj build/lst（清理中间产物）
     │   └─ store_artifacts + persist_to_workspace
     └─ job: publish-github-release（仅 /^\d+\.\d+\.\d+$/ 的 tag 触发）
         ├─ zip 打包 build/*.bin *.hex *.elf
         └─ ghr 上传到 GitHub Release
```

#### 4.4.3 源码精读

**① `make flash`：一条命令完成烧录**

[Makefile:L228-L232](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L228-L232)：

```make
flash: build/ch.bin
	dfu-util -d 0483:df11 -a 0 -s 0x08000000:leave -D build/ch.bin

dfu:
	-printf "reset dfu\r" >$(DEVICE) && sleep 1
```

`dfu-util` 参数逐个拆解（与 [README.md:L64-L77](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/README.md#L64-L77) 的说明对应）：

| 参数 | 含义 |
| --- | --- |
| `-d 0483:df11` | 只匹配该 VID:PID 的 USB 设备。`0483` 是 ST 的厂商 ID，`df11` 是其 DFU bootloader 的产品 ID |
| `-a 0` | alt setting 0，对应 STM32 的内部 Flash |
| `-s 0x08000000:leave` | 从 Flash 基地址开始写；`leave` 表示传完后令 bootloader 退出并跳转执行新固件 |
| `-D build/ch.bin` | Download，即把文件从主机写入设备 |

`dfu` 目标则是"让正在运行的固件进 DFU 模式"的便捷方式：向串口设备（`DEVICE`，[Makefile:L85](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/Makefile#L85) 默认 `/dev/cu.usbmodem401`，Linux 用户需改成 `/dev/ttyACM0`）发送 `reset dfu\r` 这条 shell 命令，等 1 秒后设备即出现在 DFU 模式。开头的 `-` 让 make 忽略该命令可能的失败。

**② CI 镜像与本地构建同源**

[.circleci/config.yml:L4-L15](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/.circleci/config.yml#L4-L15)：

```yaml
build:
  docker:
    - image: edy555/arm-embedded:8.2
  steps:
    - checkout
    - run:
        name: "Pull Submodules"
        command: |
          git submodule init
          git submodule update --remote
    - run:
        name: "Build Firmware"
        command: make
    - run:
        name: "Remove obj/lst files"
        command: rm -r build/obj build/lst
```

注意 CI 用的镜像就是本讲实践用的 `edy555/arm-embedded:8.2`——**你本地 docker 构建的结果与官方 CI 环境一致**，这是"以 docker 为准"的好处。`rm -r build/obj build/lst` 印证了 4.1 说的产物目录结构。

**③ 发布到 GitHub Release**

[.circleci/config.yml:L26-L37](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/.circleci/config.yml#L26-L37)：

```yaml
publish-github-release:
  ...
      - run:
          name: "Publish Release on GitHub"
          command: |
            go get github.com/tcnksm/ghr
            zip nanovna-firmware-${CIRCLE_TAG}.zip build/*.bin build/*.hex build/*.elf
            ghr -t ${GITHUB_TOKEN} -u ${CIRCLE_PROJECT_USERNAME} -r ${CIRCLE_PROJECT_REPONAME} -c ${CIRCLE_SHA1} -delete ${CIRCLE_TAG} nanovna-firmware-${CIRCLE_TAG}.zip
```

用 `ghr` 工具把三种格式固件打包上传，`GITHUB_TOKEN` 存在 CircleCI 的环境变量里。

**④ 触发条件：tag 正则**

[.circleci/config.yml:L38-L55](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/.circleci/config.yml#L38-L55)：

```yaml
workflows:
  version: 2
  main:
    jobs:
      - build:
          filters:
            tags:
              only: /^\d+\.\d+\.\d+$/
      - publish-github-release:
          requires:
            - build
          filters:
            branches:
              ignore: /.*/
            tags:
              only: /^\d+\.\d+\.\d+$/
```

只有形如 `1.0.0` 的纯三段数字 tag 才会触发发布；`publish` job 还用 `branches: ignore: /.*/` 排除一切分支，保证发布只来自 tag。`requires: build` 保证了先编译后发布的顺序依赖。

#### 4.4.4 代码实践：真机烧录（可选，需硬件）

**实践目标**：把 4.2 里打好 `mytest-1.0` 版本号的固件烧进真机并验证。

**操作步骤**：

```bash
# 0. 本机需安装 dfu-util（如 ubuntu: sudo apt install -y dfu-util，见 README L22-L41）
# 1. 让设备进入 DFU 模式（二选一）：
#    a) 断电，跳线拉高 BOOT0，重新上电；
#    b) 设备正常运行时，在屏幕上选 Config->DFU 菜单。
# 2. 烧录
make flash
#    （Linux 下如需权限，先 sudo 或配置 udev 规则）
# 3. 设备自动重启后进入 Config->Version 菜单，或 USB 串口敲 version 命令
```

**需要观察的现象**：`dfu-util` 依次打印找到设备、传输百分比、`done`；烧完因 `:leave` 设备自动复位重启，屏幕正常出图。

**预期结果**：`Config→Version` 菜单显示 `Version: mytest-1.0`。本实践需要真机，**无硬件的读者跳过即可**，用 4.2 的 `strings` 验证等价确认了版本号链路。烧录过程**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`-s 0x08000000:leave` 里的 `leave` 去掉会怎样？

**答案**：固件仍会被完整写入 Flash，但传输结束后 bootloader 会停在 DFU 模式不跳转，需要手动拔插电源（注意 BOOT0 跳线要取下）才能运行新固件。`leave` 让升级一步到位。

**练习 2**：为什么 `make dfu` 目标里 `printf` 前面有个 `-`，且 `DEVICE` 默认值是 macOS 风格的 `/dev/cu.usbmodem401`？

**答案**：`-` 前缀告诉 make 忽略命令失败——设备可能没接、串口名可能不对，这只是个便捷入口，失败不应中断。默认值是作者本机的设备名，Linux 用户应改为 `/dev/ttyACM0`（Makefile 里留有注释行）。

**练习 3**：你给项目打了 tag `v2.1-beta`，CI 会发布 Release 吗？

**答案**：不会。tag 过滤正则是 `/^\d+\.\d+\.\d+$/`，只匹配 `2.1.0` 这种纯三段数字，`v2.1-beta` 不匹配；而且 publish job 还显式 `ignore: /.*/` 所有分支。要发布必须打 `2.1.0` 式的 tag。

## 5. 综合实践

把本讲三个环节串成一条完整流水线，模拟一次"发行工程师"的工作：

1. **环境**：克隆仓库并初始化子模块（4.1/4.2 的步骤）。
2. **基线构建**：docker 编译默认版本，记录 `arm-none-eabi-size` 的 text/data/bss，填入 4.1 的表格，并用 `objdump -h` 确认段地址符合 4.3 的内存布局图。
3. **定制构建**：`make VERSION=\"learn-<你的名字>-1.0\"` 重新编译，`strings` 验证版本号确实编入。
4. **对照 CI**：打开项目的 CircleCI 页面或最近一次 GitHub Release，对比官方产物的 `.bin` 大小与你本地的是否接近；思考差异来源（提示：`git describe --tags` 的输出不同、CI 用 `--remote` 拉子模块，见 4.2 练习 2）。
5. **（有真机）**：`make flash` 烧录，在 `Config→Version` 里看到自己的版本号，拍照留档。

完成后你应当得到：一张体积基线表、一份 `strings` 输出、（若有）一张真机版本号照片——它们是后续所有"改代码 → 看体积变化"实验的参照系。

## 6. 本讲小结

- `Makefile` 是构建中枢：`arm-none-eabi-` 工具链 + `-mthumb -O2 --specs=nano.specs` 选项 + CSRC 里 12 个业务源文件 + ChibiOS 的 `rules.mk` 通用规则，产出 `build/ch.elf/.hex/.bin`。
- ChibiOS 以子模块引入（edy555 fork 的 `I2SFULLDUPLEX` 分支），新克隆必须先 `git submodule update --init`；板级支持在仓库内的 `NANOVNA_STM32_F072/board.mk`。
- 链接脚本把 128K Flash 切成 96K 程序区 + 32K 校准区（`0x08018000` 起的 `.calsave` NOLOAD 段），RAM 仅 16K，双栈各 0x200。
- `VERSION` 的链路是：make 变量（默认 `git describe --tags`）→ `-DVERSION` 宏 → `NANOVNA_VERSION[]`/`info_about[]` → `version` 命令与 `Config→Version` 菜单。
- 烧录走 STM32 内置 USB DFU bootloader：`dfu-util -d 0483:df11 -a 0 -s 0x08000000:leave -D build/ch.bin`，即 `make flash`。
- CircleCI 用与本地相同的 docker 镜像构建，仅纯三段数字 tag 触发 GitHub Release 发布。

## 7. 下一步学习建议

构建链路打通后，下一讲（u1-l3《固件入口：main() 初始化流程与线程模型》）将第一次真正走进 [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c)：从 `halInit` 到主循环的十几步初始化、sweep 线程与 shell 线程的分工。建议在继续之前：

- 打开 [NANOVNA_STM32_F072/board.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/NANOVNA_STM32_F072/board.h) 浏览一遍引脚宏定义，它会反复出现在后续外设初始化代码里。
- 如果你成功编译出了固件，用 `arm-none-eabi-objdump -d build/ch.elf | less` 随便翻翻反汇编，感受一下 `-O2` 下 Cortex-M0 代码的样子——不需要看懂，混个眼熟即可。

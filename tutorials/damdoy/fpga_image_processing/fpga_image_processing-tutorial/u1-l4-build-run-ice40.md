# 构建并运行：iCE40 硬件模式

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 iCE40 FPGA 的「四步开发流程」：综合（yosys）→ 布局布线（arachne-pnr）→ 打包（icepack）→ 烧录（iceprog）。
- 读懂 `ice40/hdl/Makefile` 中每个 `make` 目标对应的命令，知道 `top.bin` 这个比特流是怎么一步步从 Verilog 源码变出来的。
- 看懂 `ice40/hdl/io.pcf`，把 `LED_R/LED_G/LED_B`、`clk`、SPI 四根信号线逐一对应到芯片物理引脚。
- 理解 `build_ice40.sh` 如何编译出主机软件 `soft_ice40`，以及 `-DICE40` 宏、`-lftdi` 链接选项各自的作用。
- 区分 `make prog`（SRAM 烧录）与 `make prog_flash`（FLASH 烧录）两种下载方式的本质差异。

本讲只聚焦「如何把硬件后端跑起来」，不深入 SPI 从机协议、SPRAM 拼装、状态机等内部实现——那些留给第 6 单元。

## 2. 前置知识

本讲面向第一次接触 FPGA 实物工具链的读者。先建立三个直觉。

### 2.1 仿真 vs. 硬件：两条后端回顾

在前一讲（u1-l3）里，我们用 Verilator 把 Verilog「翻译成 C++」，在普通电脑上运行仿真。那是**软件模拟**。

本讲要进入**真实硬件**：把同一份核心模块 `hdl/image_processing.v` 真正「写进」一块 iCE40 UltraPlus FPGA 芯片里，让它通电后自己干活。仿真模式用 `main_loop_clk()` 手动翻转时钟；硬件模式则由板上晶振提供真实时钟，主机通过 SPI 与芯片通信。

> 与 u1-l3 的呼应：仿真用 `-DSIMULATION` 宏在 `main.cpp` 里选 `Image_processing_simulation` 后端；本讲用 `-DICE40` 宏选 `Image_processing_ice40` 后端。机制完全对称。

### 2.2 FPGA 是什么、为什么需要「工具链」

CPU 是固定电路，靠软件指令工作；**FPGA（Field-Programmable Gate Array，现场可编程门阵列）**是「空白的硬件」，内部有大量可重组的逻辑单元（LUT，Look-Up Table 查找表）和连线。你要做的是**用硬件描述语言（HDL，如 Verilog）描述电路，再用一套工具把它「铺」到芯片上**。

这套「铺电路」的工具就叫**工具链（toolchain）**。本项目用的是开源的 **IceStorm 工具链**（yosys + arachne-pnr + icepack + iceprog），专门针对 Lattice iCE40 系列。

### 2.3 iCE40 UltraPlus 与开发板

本项目目标芯片是 **iCE40 UltraPlus 5K**（型号 `iCE40UP5K`），封装为 **SG48**（48 引脚的 QFN 封装）。它片上有约 5000 个逻辑单元和 1Mbit RAM。程序运行在一块「breakout board（ breakout 开发板）」上，板上还有：

- 一个**晶振**：提供 `clk` 时钟。
- 三色 **LED**：红/绿/蓝，用于调试指示。
- 一颗 **FTDI USB 芯片**：把电脑的 USB 转成 SPI，作为主机与 FPGA 通信（以及烧录）的桥梁。

理解这三个角色，引脚约束（`io.pcf`）里的信号才说得通。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 归属 |
|------|------|------|
| [ice40/hdl/Makefile](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile) | 硬件比特流的构建与烧录脚本（综合/布线/打包/烧录） | 硬件后端 |
| [ice40/hdl/io.pcf](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/io.pcf) | 引脚约束文件，把逻辑信号名映射到芯片物理引脚号 | 硬件后端 |
| [ice40/hdl/top.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v) | 顶层模块，被综合的入口；声明 `clk`/`LED_*`/`SPI_*` 等端口 | 硬件后端 |
| [build_ice40.sh](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_ice40.sh) | 编译主机软件 `soft_ice40` 的脚本 | 主机软件 |
| [software/main.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp) | 主机程序入口，用 `#ifdef` 在仿真/硬件后端间切换 | 主机软件 |
| [ice40/software/spi_lib/spi_lib.h](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.h) | 底层 SPI 库的接口，依赖 libftdi | 主机软件 |

注意一个关键事实：**`Makefile` 综合的是 `top.v`，不是核心模块 `image_processing.v`**。在 [top.v:1](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L1) 里，核心模块是通过 `` `include "../../hdl/image_processing.v" `` 文本包含进来的，所以 `top.v` 一份文件就把三个模块（`image_processing` + `ram_interface` + `spi_interface`）整体送进综合。

## 4. 核心概念与源码讲解

### 4.1 iCE40 工具链总览：从 Verilog 到比特流的四步

#### 4.1.1 概念说明

把 Verilog 变成可写进芯片的 0/1 序列，要经过四个阶段，每个阶段产出一个中间文件：

```
top.v (Verilog 源码)
   │  ① 综合 yosys
   ▼
top.blif (逻辑网表 BLIF 格式)
   │  ② 布局布线 arachne-pnr  (+ io.pcf 引脚约束)
   ▼
top.asc (ASCII 文本配置)
   │  ③ 打包 icepack
   ▼
top.bin (二进制比特流)
   │  ④ 烧录 iceprog  (经 FTDI)
   ▼
FPGA 芯片 (电路生效)
```

各阶段含义：

- **① 综合（Synthesis）**：把「行为级」的 Verilog（`if`、`case`、`+`）翻译成「门级」网表（与门、或门、触发器）。相当于把菜谱翻译成具体食材清单。
- **② 布局布线（Place & Route, P&R）**：把网表里的逻辑单元**放置**到芯片上具体的物理位置，并把它们之间的信号**连线**走通。需要 `io.pcf` 来决定每个顶层端口落在哪个引脚。相当于把食材摆进厨房、接好管线。
- **③ 打包（Pack）**：把布线结果打包成二进制配置数据。
- **④ 烧录（Program）**：通过 FTDI USB 芯片把配置数据写进 FPGA。

#### 4.1.2 核心流程

整个四步流程在 [ice40/hdl/Makefile](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile) 的 `build` 目标里一气呵成。伪代码：

```
build:
    yosys      synth_ice40  → top.blif     # 综合
    arachne-pnr -d 5k -P sg48 -p io.pcf    # 布局布线
                 → top.asc
    icepack    top.asc → top.bin           # 打包
```

之后 `prog` 或 `prog_flash` 目标再用 `iceprog` 把 `top.bin` 写进芯片。下面逐个模块拆解。

---

### 4.2 yosys 综合与 synth_ice40

#### 4.2.1 概念说明

`yosys` 是一个开源的综合工具。它本身是「通用」的，但通过加载不同的**综合脚本/流程**可以面向不同芯片。本项目用的是 `synth_ice40`，这是 yosys 内置的、专门为 iCE40 系列优化的综合流程——它知道 iCE40 有哪些原语（如 `SB_SPRAM256KA` 单口 RAM、`SB_SPI` 硬件 SPI 块），并把 Verilog 映射到这些原语上。

综合的输出是 **BLIF**（Berkeley Logic Interchange Format）文件，一种经典的网表文本格式。

#### 4.2.2 核心流程

```
输入: top.v          (含 image_processing.v / ram_interface.v / spi_interface.v)
命令: yosys -p "synth_ice40 -blif top.blif" top.v
输出: top.blif       (门级网表)
```

`-p "..."` 表示把引导内的脚本直接作为命令传入（而不是写成一个脚本文件）。`-blif top.blif` 指定综合结果以 BLIF 格式写出。

#### 4.2.3 源码精读

Makefile 的变量定义和 `build` 目标第一行：

[ice40/hdl/Makefile:1-2](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile#L1-L2) 定义 `filename = top`、`pcf_file = io.pcf`，这两个变量决定了后续所有命令都围绕 `top` 这个顶层文件展开。

[ice40/hdl/Makefile:5](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile#L5) 是综合命令：

```makefile
yosys -p "synth_ice40 -blif $(filename).blif" $(filename).v
```

含义：对 `top.v` 调用 `synth_ice40` 流程，输出 `top.blif`。注意这里只传了 `top.v` 一个源文件——前面说过，`top.v` 用 `` `include `` 把另外两个模块文本拉了进来，所以一个文件就够了。

#### 4.2.4 代码实践

**实践目标**：理解 `synth_ice40` 是 iCE40 专用流程，并观察综合产物。

**操作步骤**：

1. 在 `ice40/hdl/` 目录执行 `make build`（或直接 `make`）。
2. 观察是否生成 `top.blif`。
3. 用文本编辑器打开 `top.blif`，搜索 `SB_SPRAM256KA` 或 `SB_SPI`，确认 yosys 把高层描述映射成了 iCE40 专用原语。

**需要观察的现象**：`top.blif` 是一个很大的文本文件，里面是 `.names`、`.gate`、`.subckt` 等网表语句；你能找到 `SB_*` 开头的 iCE40 原语实例。

**预期结果**：能找到 `SB_SPI`（对应 [spi_interface.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v) 里的硬件 SPI 块）和 `SB_SPRAM256KA`（对应 [ram_interface.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v) 里的片上 RAM）。

> 若本地未安装 yosys，明确为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `yosys` 命令只传入 `top.v`，却能把 `image_processing.v` 也综合进去？

**参考答案**：因为 [top.v:1-3](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L1-L3) 用 `` `include `` 指令把 `../../hdl/image_processing.v`、`ram_interface.v`、`spi_interface.v` 三个文件**文本插入**到 `top.v` 里。综合器看到的是合并后的一份源码，所以只需指明 `top.v`。

**练习 2**：`synth_ice40` 和通用的 `synth` 有什么区别？

**参考答案**：`synth_ice40` 是 yosys 针对 iCE40 架构定制的综合流程，会把设计映射到 iCE40 特有的原语（如 `SB_SPRAM256KA` RAM、`SB_SPI`、`SB_IO`），并执行 iCE40 专有的优化。通用 `synth` 只产出与工艺无关的网表，不能直接用于 iCE40 布局布线。

---

### 4.3 arachne-pnr 布局布线（-d 5k -P sg48）

#### 4.3.1 概念说明

综合只确定了「逻辑」，布局布线（Place & Route, P&R）才确定「物理」：每个逻辑单元落到芯片哪个位置、每根信号走哪条线。`arachne-pnr` 是 IceStorm 工具链里的开源 P&R 工具。

它必须知道两件事：

1. **芯片型号**——逻辑单元数量、封装引脚数不同，映射规则就不同。
2. **引脚约束（PCF）**——每个顶层端口（如 `clk`、`SPI_SCK`）必须落在指定的物理引脚上，否则板上走线接不上。

#### 4.3.2 核心流程

```
输入: top.blif  +  io.pcf
命令: arachne-pnr -d 5k -P sg48 -p io.pcf top.blif -o top.asc
输出: top.asc   (ASCII 文本配置)
```

关键选项：

- `-d 5k`：选择 **iCE40 5K** 器件（即 `iCE40UP5K`，约 5000 个逻辑单元）。
- `-P sg48`：选择 **SG48 封装**（48 引脚 QFN）。这必须和板上实物一致——不同封装的引脚编号完全不同。
- `-p io.pcf`：指定引脚约束文件。
- `-o top.asc`：输出 ASC（ASCII 文本格式的配置数据）。

#### 4.3.3 源码精读

[ice40/hdl/Makefile:6](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile#L6)：

```makefile
arachne-pnr -d 5k -P sg48 -p $(pcf_file) $(filename).blif -o $(filename).asc
```

这行就是 P&R 命令。注意它同时吃进 `io.pcf`（约束）和 `top.blif`（网表），产出 `top.asc`。

紧接着 [ice40/hdl/Makefile:7-8](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile#L7-L8) 是两行**被注释掉的 `nextpnr-ice40`**。`nextpnr` 是 arachne-pnr 的现代继任者，注释说明作者尝试过但发现它与「HW SPI module（硬件 SPI 模块）」配合不好，于是回退到 arachne-pnr。这是一个真实的工程取舍记录，值得留意。

> 术语：`nextpnr` 注释里的 "HW SPI module" 指 [spi_interface.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v) 里调用的 iCE40 内置 `SB_SPI` 硬件原语。

#### 4.3.4 代码实践

**实践目标**：理解 `-d 5k -P sg48` 两个选项为什么必须和实物芯片对上。

**操作步骤**：

1. 阅读本讲 [io.pcf:1](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/io.pcf#L1) 的注释，确认板子是 `iCE40UP5K-QFN`。
2. 假设把 `-d 5k` 误改成 `-d 1k`（iCE40 1K 器件只有约 1000 个逻辑单元），思考会发生什么。

**需要观察的现象**：若器件选错，P&R 阶段会因「资源不足」（逻辑单元、RAM 块不够）或「封装引脚不匹配」而报错。

**预期结果**：本项目用了 128KB 片上 RAM（4 片 `SB_SPRAM256KA`）和较复杂的状态机，1K 器件根本装不下，P&R 会失败。这正是 `-d 5k` 不可随意改动的根本原因——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果板子换成 38 引脚封装（如 `qn48` 之外的其他封装），需要改 Makefile 里哪个参数？

**参考答案**：改 `-P sg48` 为对应封装名（如 `-P qn84` 等）。封装不同，物理引脚编号不同，`io.pcf` 里的引脚号也得相应重写。`-d 5k`（器件）通常不用改，只要还是 5K 系列。

**练习 2**：作者为什么放弃了 `nextpnr`？

**参考答案**：见 [Makefile:8](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile#L8) 的注释："doesn't seem to work with the HW SPI module"。`nextpnr` 在处理 `SB_SPI` 这个硬件 SPI 原语时表现异常，因此作者回退到 `arachne-pnr`。

---

### 4.4 icepack 打包与 iceprog 烧录（SRAM / FLASH 两种模式）

#### 4.4.1 概念说明

P&R 输出的 `top.asc` 是人类可读的 ASCII 文本配置，但芯片需要的是**二进制比特流**。`icepack` 把 `.asc` 打包成 `.bin`。

`iceprog` 则是烧录工具，通过板上的 **FTDI USB 芯片**把比特流送进 FPGA。它支持两种写法，对应两个 Makefile 目标：

| Makefile 目标 | 命令 | 写入位置 | 是否掉电保留 | 典型用途 |
|---|---|---|---|---|
| `prog` | `iceprog -S top.bin` | FPGA 的**配置 SRAM** | 否（掉电丢失） | 快速迭代、临时测试 |
| `prog_flash` | `iceprog top.bin` | 板载 **SPI FLASH** 芯片 | 是（上电自动加载） | 最终固化、长期使用 |

关键区别：`-S`（skip flash）让 `iceprog` **跳过 FLASH，直接写 FPGA 内部的易失 SRAM**，速度极快但断电即失；不带 `-S` 则写入板载 FLASH，FPGA 每次上电都从 FLASH 加载配置，所以持久。这正对应本讲学习目标里要区分的两种烧录。

#### 4.4.2 核心流程

```
打包:  icepack top.asc → top.bin

烧录（二选一）:
  make prog        →  iceprog -S top.bin    (写 SRAM, 立即生效, 掉电丢失)
  make prog_flash  →  iceprog top.bin       (写 FLASH, 上电自动加载, 持久)
```

#### 4.4.3 源码精读

打包命令 [ice40/hdl/Makefile:9](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile#L9)：

```makefile
icepack $(filename).asc $(filename).bin
```

两种烧录目标 [ice40/hdl/Makefile:11-15](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile#L11-L15)：

```makefile
prog:
	iceprog -S $(filename).bin

prog_flash:
	iceprog $(filename).bin
```

注意 `prog` 带 `-S`，`prog_flash` 不带——这是两者唯一的命令差异，却决定了「易失 vs. 持久」。

> 顺带一提，[Makefile:17-18](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile#L17-L18) 的 `clean` 目标只删 `top.blif`/`top.asc`/`top.bin` 三个中间产物，不碰源码。

#### 4.4.4 代码实践

**实践目标**：体会在迭代开发中选哪种烧录方式更划算。

**操作步骤**：

1. 想象你在反复修改 `image_processing.v`、每次都要重新验证。
2. 比较：每次都 `make prog_flash`（写 FLASH）vs. 每次 `make prog`（写 SRAM）。

**需要观察的现象**：写 SRAM 通常只需一两秒；写 FLASH 需要先擦除再写，明显更慢。

**预期结果**：调试阶段用 `make prog`（SRAM）快速迭代；最终确认无误后用 `make prog_flash`（FLASH）固化，这样拔掉 USB 重启也能跑。这是 iCE40 开发的常见工作流——**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：你用 `make prog` 烧录后程序正常运行，但拔掉 USB 重新插上，FPGA 却「空了」。为什么？

**参考答案**：`make prog` 用的是 `iceprog -S`，只写入了 FPGA 的配置 SRAM（易失存储），掉电即丢失。重新上电后 SRAM 是空的。要让配置持久，需用 `make prog_flash` 写入板载 FLASH。

**练习 2**：从源码看，`prog` 和 `prog_flash` 的命令差别是什么？这一字之差背后的物理意义是什么？

**参考答案**：差别就是 `-S` 标志（[Makefile:12](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile#L12) vs [Makefile:15](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile#L15)）。`-S` = skip flash，绕过 FLASH 直写 FPGA SRAM（快、易失）；不带 `-S` 则写 FLASH（慢、持久，上电自动加载）。

---

### 4.5 io.pcf 引脚约束：信号如何落到物理引脚

#### 4.5.1 概念说明

`io.pcf`（Physical Constraints File，本项目用 IceStorm 的 PCF 格式）解决一个问题：**顶层 Verilog 里的逻辑信号名（如 `clk`、`SPI_SCK`）要接到芯片的哪个物理引脚上**。

Verilog 本身只描述逻辑，不关心引脚；但 P&R 阶段必须知道「`clk` 是第 35 号脚」，否则编译出来的比特流无法和开发板的实际走线对应。PCF 就是这层映射。

#### 4.5.2 核心流程

PCF 的语法极简，每行一条 `set_io <信号名> <引脚号>`。`top.v` 顶层端口声明的每个信号，都要在 PCF 里有对应行（除常量外）。P&R 工具（arachne-pnr）读 PCF，把信号锁定到指定引脚。

#### 4.5.3 源码精读

[io.pcf:1](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/io.pcf#L1) 注释点明目标板：`iCE40 UltraPlus (iCE40UP5K-QFN) Breakout Board`。

LED 与按键、时钟 [io.pcf:3-10](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/io.pcf#L3-L10)：

```
set_io LED_R 41      # 红色 LED
set_io LED_G 40      # 绿色 LED
set_io LED_B 39      # 蓝色 LED
set_io clk  35       # 主时钟（板上晶振）
```

这三色 LED 在 [top.v:5](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L5) 是 `output` 端口，在 [top.v:9-12](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L9-L12) 用 `~led[0/1/2]` 反相驱动——**LED 是低电平点亮**（active low），这是开发板常见设计。

SPI 四线 [io.pcf:22-25](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/io.pcf#L22-L25)：

```
set_io SPI_SS   16   # Slave Select  片选
set_io SPI_SCK  15   # Serial Clock 时钟
set_io SPI_MOSI 17   # Master Out Slave In  主机→FPGA
set_io SPI_MISO 14   # Master In Slave Out  FPGA→主机
```

这四根线就是主机（电脑）与 FPGA 通信的全部物理通道。它们在 [top.v:5](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L5) 顶层声明（`SPI_SCK/SPI_SS/SPI_MOSI` 是 `input`，`SPI_MISO` 是 `output`），最终连到 [top.v:42-44](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L42-L44) 的 `spi_interface` 实例。

> 引脚映射对照表（本讲核心实践任务的一部分）：
>
> | 逻辑信号 | 物理引脚 | 方向 | 作用 |
> |---|---|---|---|
> | `LED_R` | 41 | output（低有效） | 红色调试指示 |
> | `LED_G` | 40 | output（低有效） | 绿色调试指示 |
> | `LED_B` | 39 | output（低有效） | 蓝色调试指示 |
> | `clk` | 35 | input | 主时钟 |
> | `SPI_SS` | 16 | input | SPI 片选 |
> | `SPI_SCK` | 15 | input | SPI 时钟 |
> | `SPI_MOSI` | 17 | input | 主机出→FPGA 入 |
> | `SPI_MISO` | 14 | output | FPGA 出→主机入 |

#### 4.5.4 代码实践

**实践目标**：核对 PCF 里的每个信号是否都能在 `top.v` 端口里找到对应。

**操作步骤**：

1. 打开 [top.v:5](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L5)，记下 `module top(...)` 的全部端口。
2. 打开 [io.pcf](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/io.pcf)，逐行比对。
3. 特别注意：`io.pcf` 里还有 `SW[0..3]`（拨码开关）和 `IOT_39A` 等 bank0 信号（[io.pcf:6-19](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/io.pcf#L6-L19)），其中 `SW` 在 `top.v` 端口里有声明，但 `IOT_39A` 等并没有直接出现在 `top.v` 顶层端口里。

**需要观察的现象**：PCF 里部分信号（如 `IOT_39A`）在 `top.v` 的端口列表里找不到同名声明。

**预期结果**：这说明它们是**预留/未使用**的引脚约束（可能与烧录时的特殊功能或预留扩展有关），当前 `top.v` 没有用到。核心使用的信号 `clk`、`LED_*`、`SPI_*`、`SW[*]` 都能在 [top.v:5](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L5) 一一对应。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `SPI_MOSI` 是 `input`、`SPI_MISO` 是 `output`？

**参考答案**：方向是相对 FPGA 而言的。主机（电脑侧 FTDI）是 SPI Master，FPGA 是 Slave。MOSI = Master Out Slave In，数据从主机流向 FPGA，所以对 FPGA 是 `input`；MISO = Master In Slave Out，数据从 FPGA 流向主机，所以对 FPGA 是 `output`。见 [top.v:5](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L5)。

**练习 2**：如果 `io.pcf` 里漏写了 `set_io clk 35`，会发生什么？

**参考答案**：arachne-pnr 会因为 `clk` 信号没有指定引脚而报错（或随机分配一个引脚，导致时钟根本接不到晶振），FPGA 上电后没有时钟，整个电路无法运行。所以 PCF 与顶层端口必须严格对应。

---

### 4.6 build_ice40.sh：编译主机软件 soft_ice40

#### 4.6.1 概念说明

硬件烧好后，还需要一个**主机程序**来「喂」图像、读结果。这就是 `soft_ice40`。它由 [build_ice40.sh](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_ice40.sh) 编译。

这跟仿真模式（u1-l3）形成对照：仿真模式编译 `main.cpp` + `image_processing_simulation.cpp` 得到 `simu`；硬件模式编译 `main.cpp` + `image_processing_ice40.cpp` + `spi_lib.c` 得到 `soft_ice40`。**同一份 `main.cpp`**，靠宏切换后端。

主机软件通过 **FTDI** 与 FPGA 通信：电脑 USB → 板上 FTDI 芯片（用其 MPSSE 引擎产生 SPI 时序）→ FPGA 的 SPI 引脚。

#### 4.6.2 核心流程

```
g++ -DICE40  spi_lib.c  image_processing_ice40.cpp  main.cpp  -o soft_ice40  -lftdi
     ^^^^^^^                                            ^^^^^^^^^^^^^   ^^^^^^
     选后端宏                                            输出可执行文件   链接 FTDI 库
```

`-DICE40` 在 `main.cpp` 的两处起作用（与 u1-l3 的 `-DSIMULATION` 完全对称）：

1. **选头文件**：[software/main.cpp:14-18](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L14-L18)
   ```c
   #ifdef SIMULATION
   #include "../simulation/image_processing_simulation.hpp"
   #elif ICE40
   #include "../ice40/software/image_processing_ice40.hpp"
   #endif
   ```
2. **选后端对象**：[software/main.cpp:226-230](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L226-L230)
   ```c
   #ifdef SIMULATION
   img_proc = new Image_processing_simulation();
   #elif ICE40
   img_proc = new Image_processing_ice40();
   #endif
   ```

因为定义了 `-DICE40`、未定义 `SIMULATION`，预处理器选中 `#elif ICE40` 分支，于是 `main.cpp` include 的是 ice40 头文件、`new` 的是 `Image_processing_ice40`——这就是 `-DICE40` 让 `main.cpp` 选择 ice40 后端的机制。

至于 `-lftdi`：底层 SPI 库 [spi_lib.h:3](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.h#L3) 直接 `#include <ftdi.h>`，且 `spi_lib.c` 调用了大量 `ftdi_*` 函数（`ftdi_init`、`ftdi_usb_open`、`ftdi_write_data` 等，见 [spi_lib.c:5](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c#L5)、[spi_lib.c:170](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c#L170)）。这些符号由 **libftdi** 提供，链接时必须 `-lftdi` 才能解析，否则报「未定义引用」。

> [spi_lib.h:5](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.h#L5) 的注释点明：这套 FTDI 初始化代码取自开源 `iceprog`。FTDI 芯片的 **MPSSE（Multi-Protocol Synchronous Serial Engine）**能把 USB 数据直接转成 SPI/UART 等串行时序，省去手动 bit-bang。

#### 4.6.3 源码精读

[build_ice40.sh:1-4](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_ice40.sh#L1-L4)：

```bash
echo "TODO"

g++ -DICE40 ice40/software/spi_lib/spi_lib.c ice40/software/image_processing_ice40.cpp software/main.cpp -o soft_ice40 -lftdi
# g++ -DICE40 ice40/software/spi_lib/spi_lib.c ice40/software/image_processing_ice40.cpp  -o soft_ice40
```

两点值得说明（如实指出，不粉饰）：

1. 第 1 行 `echo "TODO"` 会先打印一个 `TODO` 字样。这是一个**遗留提示**，并不影响功能——真正的编译靠第 3 行（未被注释）的 `g++` 命令完成。运行脚本时你会看到 `TODO` 后紧接着就是编译输出。
2. 第 4 行是**被注释掉的旧版**编译命令（没有 `-lftdi`，且没带 `main.cpp`），现已弃用。

这条 `g++` 把三个源文件编在一起：

- `ice40/software/spi_lib/spi_lib.c`：FTDI/MPSSE 底层 SPI 收发。
- `ice40/software/image_processing_ice40.cpp`：把高层 `send_image`/`send_add` 等调用翻译成 SPI 事务。
- `software/main.cpp`：业务逻辑（选哪个测试、读写图像）。

输出 `soft_ice40`，运行它会通过 FTDI 向 FPGA 收发数据。

#### 4.6.4 代码实践

**实践目标**：验证 `-DICE40` 与 `-lftdi` 各自的必要性。

**操作步骤（源码阅读型）**：

1. 在 `software/main.cpp` 中定位 [第 14-18 行](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L14-L18) 和 [第 226-230 行](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L226-L230)，确认 `-DICE40` 命中 `#elif ICE40` 两个分支。
2. 在 [spi_lib.c](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c) 里统计 `ftdi_` 开头的函数调用数量，确认它强依赖 libftdi。
3. 思考：去掉 `-lftdi` 会怎样？

**需要观察的现象**：

- 若去掉 `-DICE40`：`#ifdef SIMULATION` 和 `#elif ICE40` 都不成立，`main.cpp` 既不 include 任何后端头文件、也不 `new` 任何对象，编译会报「`Image_processing` 未定义」「`img_proc` 未声明」等错误。
- 若去掉 `-lftdi`：编译（编译阶段）能过，但**链接阶段**报 `undefined reference to ftdi_init` 之类的错误——因为 `spi_lib.c` 用到的 FTDI 符号找不到实现。

**预期结果**：两个选项都不可或缺。`-DICE40` 控制后端选择（编译期），`-lftdi` 提供底层 USB/SPI 库符号（链接期）——**待本地验证**。

#### 4.6.5 小练习与答案

**练习 1**：为什么仿真模式的 `build_simulation.sh` 不需要 `-lftdi`，而硬件模式的 `build_ice40.sh` 需要？

**参考答案**：仿真后端（[simulation/image_processing_simulation.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp)）通过 Verilator 生成的 C++ 类直接在内存里模拟，根本不碰 USB/SPI，所以不需要 libftdi。硬件后端要经 FTDI USB 芯片与真实 FPGA 通信，`spi_lib.c` 调用了大量 `ftdi_*` 函数，必须 `-lftdi`。

**练习 2**：`build_ice40.sh` 第 1 行的 `echo "TODO"` 会不会让脚本失败？

**参考答案**：不会。`echo "TODO"` 只是打印一行提示文本，执行成功，脚本继续往下执行第 3 行的 `g++`。这是一个遗留提示，并非阻塞性错误。

---

## 5. 综合实践

### 实践任务：从源码到上板的完整链路走查

把本讲的四步工具链和主机软件串起来，画出 iCE40 硬件模式的**完整构建与运行链路**。

**步骤 1：构建硬件比特流（在 `ice40/hdl/`）**

```
make build     # yosys 综合 → arachne-pnr 布线 → icepack 打包 → top.bin
```

追踪产物链：`top.v` →（[Makefile:5](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile#L5)）→ `top.blif` →（[Makefile:6](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile#L6)）→ `top.asc` →（[Makefile:9](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile#L9)）→ `top.bin`。

**步骤 2：烧录到 FPGA（二选一）**

```
make prog        # 调试用：iceprog -S top.bin  写 SRAM，快速但掉电丢失
make prog_flash  # 固化用：iceprog top.bin     写 FLASH，持久
```

**步骤 3：编译主机软件（在仓库根目录）**

```
./build_ice40.sh   # 产出 soft_ice40（注意会先 echo "TODO"）
```

对应 [build_ice40.sh:3](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_ice40.sh#L3)。

**步骤 4：运行并查看结果**

```
./soft_ice40      # 通过 FTDI 与 FPGA 通信，生成 output.dat
./run_gnuplot.sh  # 用 gnuplot 把 output.dat 显示为灰度图（与仿真模式共用）
```

**最终产出**：一张与 [u1-l3](u1-l3-build-run-simulation.md) 仿真模式格式相同的灰度图，但这次数据来自真实 FPGA。

### 对照仿真与硬件两条构建路径

完成下表（答案已给，便于核对）：

| 维度 | 仿真模式（u1-l3） | 硬件模式（本讲） |
|---|---|---|
| 构建脚本 | `build_simulation.sh` | `ice40/hdl/Makefile`（比特流）+ `build_ice40.sh`（软件） |
| 核心工具 | Verilator | yosys + arachne-pnr + icepack |
| 后端选择宏 | `-DSIMULATION` | `-DICE40` |
| 产物 | `simu`（可执行文件） | `top.bin`（比特流）+ `soft_ice40`（软件） |
| 通信方式 | C++ 内存直连 | FTDI → MPSSE → SPI |
| 时钟来源 | `main_loop_clk()` 手动翻转 | 板上晶振 |
| 结果输出 | `output.dat` | `output.dat`（同格式） |

> 若你没有实物开发板，整个综合/布线/打包链路仍可在本地跑（只要装了 IceStorm 工具链），只是 `make prog` 和 `./soft_ice40` 需要真实硬件——这两步标注「待本地验证」。

## 6. 本讲小结

- iCE40 硬件开发有四步：**综合（yosys/synth_ice40）→ 布局布线（arachne-pnr）→ 打包（icepack）→ 烧录（iceprog）**，产物链是 `top.v → top.blif → top.asc → top.bin`。
- `-d 5k -P sg48` 必须和实物芯片（iCE40UP5K、SG48 封装）严格对应，否则资源或引脚不匹配。
- `io.pcf` 把逻辑信号锁定到物理引脚：`LED_R/G/B = 41/40/39`、`clk = 35`、`SPI_SS/SCK/MOSI/MISO = 16/15/17/14`。
- `make prog`（`iceprog -S`）写易失 SRAM、快但掉电丢失；`make prog_flash`（`iceprog`）写 FLASH、持久。调试用前者、固化用后者。
- `build_ice40.sh` 用 `-DICE40` 让同一份 `main.cpp` 选中 `Image_processing_ice40` 后端，用 `-lftdi` 链接 FTDI 库以驱动 USB↔SPI 通信。
- 硬件模式与仿真模式（u1-l3）共用 `main.cpp`、`output.dat` 和 `run_gnuplot.sh`，差异仅在「宏 + 通信方式 + 时钟来源」。

## 7. 下一步学习建议

本讲只解决了「怎么把硬件后端跑起来」，但刻意回避了三个黑盒：

1. **`top.v` 内部怎么连线**：`image_processing`、`ram_interface`、`spi_interface` 三块如何拼成完整系统 → 见 **u6-l2 iCE40 硬件顶层与 SPRAM 接口**。
2. **`spi_interface.v` 如何把 SPI 包翻译成命令**：`SB_SPI` 硬件原语、SPI 从机初始化 → 见 **u6-l3 SPI 从机接口与 SB_SPI 硬件块**。
3. **主机侧 `soft_ice40` 如何发 SPI 事务**：`image_processing_ice40.cpp` + `spi_lib.c` 的命令封装与重试机制 → 见 **u6-l4 主机 SPI 软件：FTDI 与命令封装**。

如果想先理解核心模块本身（命令 FSM、双缓冲、运算状态机），可以先跳到 **第 3、4 单元**（`u3-l1` 起），再回来看第 6 单元的硬件后端实现。

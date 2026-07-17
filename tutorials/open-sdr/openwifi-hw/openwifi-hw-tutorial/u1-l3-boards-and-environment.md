# 支持的板卡与开发运行环境

## 1. 本讲目标

本讲是「从零认识 openwifi-hw」单元的第三篇。上一篇（u1-l2）我们已经看清仓库的目录结构：`ip/` 放 6 个自定义 WiFi IP，`boards/` 按 `BOARD_NAME` 每板一个目录，并且认识了 `adi-hdl`、`openofdm_rx` 两个子模块。

读完本讲，你应当能够：

- 说出 openwifi-hw 支持哪些 `BOARD_NAME`，以及每块板卡搭配的 FPGA 与射频前端。
- 解释「Vivado 许可」这一列为什么有的板卡 `Need`、有的 `NO need`。
- 理解 `BOARD_NAME` 这个**环境变量**是如何驱动整个构建流程的（它不只是个名字，而是决定使用哪套 ADI 参考设计、生成哪些条件编译宏的关键开关）。
- 列出在动手编译前必须准备好的软件环境：Vivado 2022.2 + Vitis、Viterbi 译码器许可、Ubuntu 版本、`libtinfo5` 等。

本讲不涉及任何 Verilog 代码，全部基于 `README.md` 与构建脚本阅读。

## 2. 前置知识

在进入板卡清单前，先回顾三个上一篇已经建立、本讲会反复用到的概念：

- **PS / PL**：Xilinx Zynq SoC 把 ARM 处理器（PS，Processing System）和 FPGA 可编程逻辑（PL，Programmable Logic）集成在同一颗芯片上。openwifi-hw 是**纯 PL 侧**设计，它的产物是 `.xsa`/`.ltx` 硬件镜像，交给另一侧的 PS（运行 Linux 与驱动）去用。
- **AD9361 / AD9364**：Analog Devices（ADI）的射频收发器芯片，是 openwifi 的「射频前端」。它在 PL 与天线之间完成数字 I/Q 与模拟射频之间的转换（ADC/DAC、混频、滤波、增益控制）。
- **ADI HDL 参考设计（adi-hdl 子模块）**：ADI 官方为各评估板提供的 FPGA 底座工程，已经把 AD9361/9364 的数据通路、时钟、AXI DMA 等接好。openwifi-hw 并不从零搭射频底座，而是**复用 adi-hdl 中对应板卡的工程**，再把自己的 WiFi IP 叠加上去。

一句话总结：**板卡 = 某款 Zynq FPGA（PS+PL）+ 某款 ADI 射频芯片**。openwifi-hw 能支持的板卡，就是「adi-hdl 已提供底座工程、且 openwifi-hw 在 `boards/` 下准备了顶层工程」的那些组合。

## 3. 本讲源码地图

本讲只读两个核心文件，外加一个脚本作为补充：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md) | 列出全部 `BOARD_NAME` 及其 Vivado 许可需求；给出构建前置条件（Vivado 版本、Viterbi 许可、Ubuntu、libtinfo5）；说明基带时钟可选值。 |
| [prepare_adi_board_ip.sh](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_board_ip.sh) | 把 `BOARD_NAME` 映射到 `adi-hdl` 中对应的参考设计目录，并调用 `make` 生成该板卡的 ADI IP。这是理解「环境变量如何驱动构建」的关键。 |
| [boards/create_ip_repo.sh](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh) | （补充）从**当前所在目录名**推导出 `BOARD_NAME`，并把它写进每个 IP 的 `_pre_def.v` 条件编译宏。下一讲（u1-l4）会详读，本讲只看它与 `BOARD_NAME` 的关系。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **板卡清单**：都有哪些板卡、各自搭配什么 FPGA 与射频、为什么有的需要 Vivado 许可。
2. **BOARD_NAME 如何驱动构建**：这个名字在构建链路里到底被谁消费。
3. **运行环境**：编译前必须准备的 Vivado/Vitis 版本与系统依赖。

### 4.1 板卡清单：BOARD_NAME、FPGA 与射频前端

#### 4.1.1 概念说明

openwifi-hw 不是「一份代码跑所有板子」，而是「**每块板卡一个 `BOARD_NAME`，对应一套顶层工程**」。`BOARD_NAME` 同时是：

- 一个**环境变量**（你在 shell 里 `export BOARD_NAME=zc706_fmcs2`）；
- `boards/` 下一个**真实目录名**（`boards/zc706_fmcs2/`）；
- 后续会被写进 Verilog 的一个**条件编译宏**（`` `define zc706_fmcs2 ``）。

README 用一张表把所有官方支持的 `BOARD_NAME`、板卡描述和「是否需要 Vivado 许可」列了出来。理解这张表是本讲的核心。

「Vivado 许可」这一列的含义：openwifi 的 OFDM 接收机用到了 Xilinx 的 **Viterbi 译码器 IP 核**（卷积码译码，属于付费 IP）。如果你的板卡那一列写 `Need`，说明该工程的某个器件/特性需要你安装并激活 Vivado 许可才能编译通过；写 `NO need` 的板卡则可以使用免费（WebPACK/评估）许可完成编译。

#### 4.1.2 核心流程

把 README 的板卡表抽象成两类信息：

```
板卡描述 = FPGA 开发板 + 射频前端子卡/模块
           \__________/   \________________/
           决定 PL 规模      决定 ADI 参考设计
           (Zynq / RFSoC)   (fmcomms2 / adrv9364z7020 / ...)
```

- 凡是带 `fmcs2` 后缀的（如 `zc706_fmcs2`、`zcu102_fmcs2`），都是「Xilinx 主板 + FMCOMMS2/3/4 FMC 子卡」的组合，射频走 ADI 的 `fmcomms2` 参考工程，射频芯片为 AD936x 系列。
- 凡是 `adrv9361z7035` / `adrv9364z7020` 这类，板名本身就编码了 FPGA 与射频：`z7035`/`z7020` 指明 Zynq 的规模（Z-7035 / Z-7020），`adrv9361`/`adrv9364` 指明射频芯片。
- `antsdr` / `e310v2` / `antsdr_e200` / `sdrpi` / `neptunesdr` 等是社区/厂商做成的小型一体化板，射频多为 AD9361，走的是 `adrv9364z7020/ccbob_lvds` 这套 ADI 参考工程。

> 提示：`rfsoc4x2` 与 `LibreSDR` 虽然出现在 README 的表里，但 `boards/` 目录下**并没有**对应的工程目录，`prepare_adi_board_ip.sh` 也**没有**为它们做映射（详见 4.2.3）。这意味着它们的支持情况与表中其他板卡不完全相同，使用前需额外确认。

#### 4.1.3 源码精读

README 的板卡表（核心信息全部在这里）：

[README.md:L26-L42](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L26-L42) —— 这 17 行就是 `BOARD_NAME` 的全部官方取值，第三列标注了是否需要 Vivado 许可。

逐行挑几条有代表性的看：

- [README.md:L30](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L30) —— `zc706_fmcs2`：Xilinx ZC706 主板 + FMCOMMS2/3/4 子卡，**Need** 许可。这是 openwifi 最经典的开发平台之一。
- [README.md:L31-L32](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L31-L32) —— `zed_fmcs2`、`adrv9364z7020`：标注 **NO need**，说明它们的器件规模落在免费许可范围内。
- [README.md:L33](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L33) —— `adrv9361z7035`：板名直接编码了 Zynq Z-7035 + AD9361，**Need** 许可（7035 规模较大，超出免费档）。
- [README.md:L35-L38](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L35-L38) —— `antsdr` / `e310v2` / `antsdr_e200` / `sdrpi`：MicroPhase、HexSDR 等社区小型板，均为 **NO need**，且各自带有 `Notes` 链接（如 `kernel_boot/boards/antsdr/notes.md`）说明烧录细节。
- [README.md:L39](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L39) —— `zcu102_fmcs2`：UltraScale+ 系列的 ZCU102 主板，**Need** 许可；它也是少数能把基带时钟跑到 240MHz 的板卡（见 4.3）。
- [README.md:L40](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L40) —— `rfsoc4x2`：RFSoC 板卡，**Need** 许可。
- [README.md:L41-L42](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L41-L42) —— `neptunesdr`、`LibreSDR`：标注 **(Unofficial!)** 的低成本 Zynq 7020 + AD9361 板，由社区维护。

把上面整理成一张速查表（射频芯片一列中，能从板名/README 直接确认的写明，无法从本仓库源码确认的标「待确认」）：

| BOARD_NAME | FPGA（PL） | 射频前端 | Vivado 许可 |
|------------|-----------|---------|------------|
| zc706_fmcs2 | ZC706（Zynq-7） | FMCOMMS2/3/4（AD936x） | Need |
| zed_fmcs2 | ZedBoard（Zynq Z-7020） | FMCOMMS2/3/4（AD936x） | NO need |
| zc702_fmcs2 | ZC702（Zynq Z-7020） | FMCOMMS2/3/4（AD936x） | NO need |
| adrv9361z7035 | Zynq Z-7035 | AD9361 | Need |
| adrv9364z7020 | Zynq Z-7020 | AD9364 | NO need |
| zcu102_fmcs2 | ZCU102（Zynq UltraScale+） | FMCOMMS2/3/4（AD936x） | Need |
| rfsoc4x2 | RFSoC | 待确认（RFSoC 集成 RF 数据转换器） | Need |
| antsdr / antsdr_e200 / e310v2 | 待确认（社区板） | AD9361 系 | NO need |
| sdrpi | 待确认（社区板） | 待确认 | NO need |
| neptunesdr / LibreSDR | Zynq Z-7020（README 明示） | AD9361（README 明示） | NO need |

> 说明：表中「待确认」项无法仅凭本仓库的 README 与脚本断言，需查阅板卡厂商页面。其中 `rfsoc4x2`、`LibreSDR` 在 `boards/` 下无工程目录，使用前尤其要确认其支持状态。

#### 4.1.4 代码实践

**实践目标**：用本讲的方法，独立读懂 README 板卡表中任意一行。

**操作步骤**：

1. 打开 [README.md:L26-L42](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L26-L42)。
2. 任选一块板卡（推荐选 `adrv9361z7035`，因为板名信息最全）。
3. 整理出：① `BOARD_NAME`；② 对应的 FPGA（从板名/README 描述）；③ 射频前端；④ 是否需要 Vivado 许可；⑤ 该板卡在 README 中是否带有 `Notes` 链接。

**需要观察的现象**：你会发现板名往往「自带说明书」——`adrv9361z7035` 拆开就是「ADRV9361 射频 + Z-7035 FPGA」。

**预期结果**（以 `adrv9361z7035` 为例）：

- `BOARD_NAME`：`adrv9361z7035`
- FPGA：Zynq Z-7035
- 射频前端：AD9361（+ ADRV1CRR-BOB/FMC 载板）
- Vivado 许可：Need
- Notes 链接：无（README 中未附带 notes.md）

#### 4.1.5 小练习与答案

**练习 1**：README 里 `zc702_fmcs2` 和 `zc706_fmcs2` 都是「Xilinx 主板 + FMCOMMS 子卡」，为什么一个 `NO need`、一个 `Need`？

**参考答案**：因为二者 FPGA 规模不同。`zc702` 是较小规模的 Zynq Z-7020（落在 Vivado 免费许可范围内），`zc706` 是较大规模的 Zynq（Z-7045 级别），超出免费许可范围，需要付费/评估许可才能编译。

**练习 2**：板名 `adrv9364z7020` 里同时包含了哪两条信息？

**参考答案**：`adrv9364` 表示射频芯片是 AD9364；`z7020` 表示 FPGA 是 Zynq Z-7020。这也解释了它为什么 `NO need` 许可（7020 规模小）。

### 4.2 BOARD_NAME 如何驱动构建

#### 4.2.1 概念说明

`BOARD_NAME` 不只是给人看的标签，它在构建链路里被三处「消费」：

1. **`prepare_adi_board_ip.sh`** 用它选择 adi-hdl 里哪一套参考设计工程去编译（生成该板卡的 ADI IP）。
2. **`boards/` 目录结构** 用它定位顶层工程：你必须 `cd` 进 `boards/$BOARD_NAME/` 才能继续构建。
3. **`create_ip_repo.sh`** 直接从**当前目录名**读出 `BOARD_NAME`，并把它写成条件编译宏 `` `define $BOARD_NAME ``，让 Verilog 代码能按板卡裁剪行为。

这一节重点看第 1 点和第 3 点，它们最能说明「环境变量如何驱动构建」。

#### 4.2.2 核心流程

```
export BOARD_NAME=zc706_fmcs2
        │
        ▼
prepare_adi_board_ip.sh $XILINX_DIR $BOARD_NAME
        │  把 BOARD_NAME 映射成 adi-hdl 里的目录
        │  例: zc706_fmcs2 -> adi-hdl/projects/fmcomms2/zc706/
        ▼
在该 ADI 参考工程目录里 make，生成板卡级 ADI IP
```

随后进入 `boards/$BOARD_NAME/` 跑 `create_ip_repo.sh`，它会**从目录名反推** `BOARD_NAME`（不需要你再传一次），并把它写进宏文件，供后续 IP 打包使用。

#### 4.2.3 源码精读

先看 `prepare_adi_board_ip.sh` 的核心：一张 `BOARD_NAME → adi-hdl 工程` 的映射表。

[prepare_adi_board_ip.sh:L1-L5](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_board_ip.sh#L1-L5) —— 脚本要求恰好 2 个参数 `$XILINX_DIR $BOARD_NAME`，否则直接退出。

[prepare_adi_board_ip.sh:L7-L14](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_board_ip.sh#L7-L14) —— 校验 `$XILINX_DIR` 下确实存在 `Vivado/` 目录，避免把路径写错。

[prepare_adi_board_ip.sh:L20-L39](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_board_ip.sh#L20-L39) —— **本节最重要的代码**：一连串 `if/elif` 把 `BOARD_NAME` 映射到 `ADI_PROJECT_DIR`。关键几行：

- L20-L23：`zcu102_fmcs2`（以及 README 表里没有、但脚本支持的 `zcu102_9371`）→ `adi-hdl/projects/fmcomms2/zcu102/`。
- L24-L29：`zc706_fmcs2` / `zc702_fmcs2` / `zed_fmcs2` → 各自对应的 `fmcomms2/<board>/` 工程。
- L30-L33：`adrv9361z7035` → `adi-hdl/projects/adrv9361z7035/ccbob_lvds/`。
- L34-L35：`adrv9364z7020` **以及** `antsdr` / `antsdr_e200` / `e310v2` / `sdrpi` / `neptunesdr` → **共用** `adi-hdl/projects/adrv9364z7020/ccbob_lvds/` 这一套参考设计。这说明这些社区板的射频底座与 `adrv9364z7020` 同源。
- L36-L39：其余任何值都会走到 `else`，报错 `\$BOARD_NAME is not correct` 并退出。

> 注意上面 L36-L39 的 `else`：`rfsoc4x2`、`LibreSDR` 并未被这条 `if/elif` 覆盖，所以对它们直接运行该脚本会报错退出。这印证了 4.1.2 的提示——这两块板的支持路径与其他板不同。

[prepare_adi_board_ip.sh:L47](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_board_ip.sh#L47) —— `source $XILINX_DIR/Vivado/2022.2/settings64.sh`，这里把 Vivado 版本（2022.2）写死进了路径，这正是下一节要讲的「运行环境」硬性要求。

[prepare_adi_board_ip.sh:L49-L50](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_board_ip.sh#L49-L50) —— `cd` 进映射出的 ADI 工程目录并执行 `make`，由 adi-hdl 的 Makefile 生成该板卡的 ADI IP（README 提示：看到 `Building ABCD project [` 后即可中断，不必等它跑完）。

再看 `create_ip_repo.sh` 如何从目录名反推 `BOARD_NAME`：

[boards/create_ip_repo.sh:L29-L30](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L29-L30) —— `BOARD_NAME=${PWD##*/}`，用 bash 参数展开剥掉路径前缀，只留当前目录名。**所以你必须先 `cd` 进 `boards/$BOARD_NAME/` 再运行它**——它根本不接收 `BOARD_NAME` 参数，完全靠你所在的目录推断。

[boards/create_ip_repo.sh:L32-L33](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L32-L33) —— 期待 `$XILINX_DIR/Vitis/2022.2/settings64.sh` 存在（注意是 **Vitis** 路径，不是 Vivado），再次固定了 2022.2 这个版本。

[boards/create_ip_repo.sh:L48](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L48) —— `echo "\`define $BOARD_NAME" >> $filename_to_write`，把板名作为宏写进每个 IP 的 `_pre_def.v`。Verilog 代码里就能用 `` `ifdef zc706_fmcs2 `` 这样的写法按板卡做条件编译（这套机制会在 u7-l2 详讲）。

#### 4.2.4 代码实践

**实践目标**：确认 `BOARD_NAME` 与 adi-hdl 参考设计的对应关系。

**操作步骤**：

1. 打开 [prepare_adi_board_ip.sh:L20-L39](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_board_ip.sh#L20-L39)。
2. 选定 `BOARD_NAME=antsdr`，找出它会用哪条 `elif`、映射到哪个 `ADI_PROJECT_DIR`。
3. 再选定 `BOARD_NAME=zc706_fmcs2`，做同样的事。
4. 思考：如果有人误把 `BOARD_NAME` 写成 `zc706`（漏掉 `_fmcs2`），脚本会怎样？

**需要观察的现象**：`antsdr` 并没有自己专属的 `antsdr/...` 目录，而是和 `adrv9364z7020` 共用一条 `elif`。

**预期结果**：

- `antsdr` → L34 那一条 → `./adi-hdl/projects/adrv9364z7020/ccbob_lvds/`。
- `zc706_fmcs2` → L24 → `./adi-hdl/projects/fmcomms2/zc706/`。
- 写成 `zc706`（漏后缀）会落入 L36 的 `else`，报错 `$BOARD_NAME is not correct. Please check!` 并 `exit 1`。

#### 4.2.5 小练习与答案

**练习 1**：为什么运行 `create_ip_repo.sh` 时不需要（也不能）把 `BOARD_NAME` 作为参数传进去？

**参考答案**：因为脚本第 L29 行用 `BOARD_NAME=${PWD##*/}` 从**当前所在目录名**直接推导。它假定你已经 `cd` 进了 `boards/$BOARD_NAME/`，所以目录名就是 `BOARD_NAME`。

**练习 2**：README 表里的 `rfsoc4x2` 能否直接用 `prepare_adi_board_ip.sh zcu102_fmcs2` 那样的方式生成 ADI IP？为什么？

**参考答案**：不能直接套用。`prepare_adi_board_ip.sh` 的 `if/elif` 没有 `rfsoc4x2` 这一分支，会走到 `else` 报错退出；且 `boards/` 下也没有 `rfsoc4x2` 目录。它的支持路径需要另行确认。

### 4.3 运行环境：Vivado/Vitis 版本与系统依赖

#### 4.3.1 概念说明

openwifi-hw 是 FPGA 工程，编译（生成 bitstream）必须在 Xilinx Vivado 里完成。和纯软件项目「装个编译器就能跑」不同，FPGA 工具链对**版本、许可、操作系统、甚至某个系统库**都很敏感。README 的「Build FPGA → Pre-conditions」小节把这些硬性要求列得很清楚，任何一个不满足都会在构建中途报错。

#### 4.3.2 核心流程

构建前的环境检查清单（按 README 顺序）：

```
1. Vivado 2022.2 + Vitis（注意：不是 Vitis_HLS！）
2. 安装 Xilinx Viterbi 译码器评估许可
3. 操作系统：Ubuntu 18 / 20 / 22 LTS
4. 系统库：libtinfo5（Ubuntu 24 需手动安装）
   └─ 这些都满足后，才能跑 prepare_*.sh / create_ip_repo.sh / openwifi.tcl
```

其中 Vivado 版本之所以「卡得很死」，是因为 `prepare_adi_lib.sh` 把 adi-hdl 子模块 checkout 到 `2022_R2` 分支、各脚本都 `source .../2022.2/settings64.sh`——整个工具链是围绕 **2022.2 / 2022_R2** 这一组版本联调的。

#### 4.3.3 源码精读

README 的前置条件原文：

[README.md:L46-L56](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L46-L56) —— 全部 Pre-conditions。逐条拆解：

- [README.md:L47-L48](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L47-L48) —— **Vivado 2022.2 with Vitis**，并且特别强调安装目录里要有 `Vitis`（**不是 Vitis_HLS**）。如果只装了 Vivado 没装 Vitis，可以通过系统开始菜单的「Xilinx Design Tools → Add Design Tools for Devices 2022.2」补装。
- [README.md:L49](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L49) —— 安装 **Xilinx Viterbi Decoder** 的**评估许可**到 Vivado。这一条对应 4.1 里「Need 许可」的板卡——OFDM 接收机的卷积码译码用到这个 IP。
- [README.md:L50](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L50) —— 操作系统：在 **Ubuntu 18/20/22 LTS** 上测试通过；其他 OS「might also work」但不保证。
- [README.md:L51-L56](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L51-L56) —— 需要安装 `libtinfo5`（`sudo apt install libtinfo5`）。**Ubuntu 24 LTS** 默认只有 libtinfo6，会不工作，需要按给出的 `wget` + `dpkg -i` 命令手动安装 18.04 的那个 `.deb` 包。

把「版本被写死」这件事和脚本对照看，会更清楚为什么不能随便换版本：

[prepare_adi_board_ip.sh:L47](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_board_ip.sh#L47) —— `source $XILINX_DIR/Vivado/2022.2/settings64.sh`，路径里写死 `2022.2`。

[boards/create_ip_repo.sh:L32-L33](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L32-L33) —— `$XILINX_DIR/Vitis/2022.2/settings64.sh`，同样写死 `2022.2`。

最后看一条和板卡/环境都相关的「进阶参数」——基带时钟。它不在 Pre-conditions 里，但属于「运行环境可调项」：

[README.md:L107-L111](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L107-L111) —— 默认基带时钟 100MHz；可在 `openwifi.tcl` 开头改 `NUM_CLK_PER_US`。可选值与板卡相关：`zcu102` 支持 240/100MHz；`zc706` 和 `adrv9361z7035` 支持 100/200MHz；其余板卡只有 100MHz。改完需重新 `source openwifi.tcl`。

#### 4.3.4 代码实践

**实践目标**：在动手编译前，自检运行环境是否齐全（不实际编译，只做检查）。

**操作步骤**：

1. 确认 Vivado 版本：检查你的安装目录下是否存在 `Vivado/2022.2/settings64.sh` 与 `Vitis/2022.2/settings64.sh`（这正好是两个脚本 `source` 的路径）。
2. 确认 Viterbi 许可：在 Vivado 里查看 License Manager，确认 Viterbi Decoder 许可已激活。
3. 确认系统库：执行 `dpkg -l | grep libtinfo`，看是否有 `libtinfo5`。
4. 记录你的 Ubuntu 版本：`lsb_release -a`。

**需要观察的现象**：步骤 1 的两个 `settings64.sh` 必须都存在；缺 `Vitis` 那个就是 README L47 警告的「装了 Vitis_HLS 而非 Vitis」的常见错误。

**预期结果**：四项全部满足，才可以进入下一讲的构建流程。若 `libtinfo5` 缺失，按 [README.md:L54-L55](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L54-L55) 的两条命令手动安装。

> 待本地验证：本实践只做环境探测，不运行任何会改变工程的命令；具体路径取决于你的 `$XILINX_DIR`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 README 强调要装的是 `Vitis` 而不是 `Vitis_HLS`？

**参考答案**：因为 `create_ip_repo.sh` 依赖的脚本是 `$XILINX_DIR/Vitis/2022.2/settings64.sh`（见 [create_ip_repo.sh:L32-L33](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L32-L33)）。`Vitis_HLS` 是另一个工具，目录不同，无法提供该脚本。

**练习 2**：为什么换用 Vivado 2021.2 大概率会失败？

**参考答案**：因为多个脚本把 `2022.2` 写死进了 `settings64.sh` 路径，且 adi-hdl 子模块被 checkout 到 `2022_R2` 分支，整套工具链是围绕 2022.2 联调的。换版本需要走 README「Migrate」小节描述的迁移流程（会在 u7-l5 讲）。

**练习 3**：一台 Ubuntu 24.04 的机器，直接 `sudo apt install libtinfo5` 会成功吗？

**参考答案**：通常不会。Ubuntu 24 LTS 默认源里是 libtinfo6，README 要求按 [README.md:L54-L55](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L54-L55) 给出的方式，手动下载 18.04 的 `.deb` 包再用 `dpkg -i` 安装 libtinfo5。

## 5. 综合实践

把本讲三个模块串起来，完成一份「板卡构建档案」。

**任务**：假设你要在 `zc706_fmcs2` 上编译 openwifi-hw，请回答并整理成一份小报告：

1. **板卡识别**：从 [README.md:L30](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L30) 说出它的主板、射频前端、是否需要 Vivado 许可。
2. **环境变量如何生效**：在 [prepare_adi_board_ip.sh:L20-L39](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_board_ip.sh#L20-L39) 里找到 `zc706_fmcs2` 映射到的 adi-hdl 工程；并说明随后 `create_ip_repo.sh` 是从哪里得到 `BOARD_NAME` 的（[create_ip_repo.sh:L29-L30](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L29-L30)）。
3. **环境自检**：列出编译前必须满足的 4 项前置条件（[README.md:L46-L56](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L46-L56)），并指出 `zc706_fmcs2` 因为许可列是 `Need`，所以特别要确保哪一项已激活。
4. **可选进阶**：查 [README.md:L107-L111](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L107-L111)，说出 `zc706` 支持的基带时钟可选值。

**参考要点**：

1. 主板 Xilinx ZC706 + FMCOMMS2/3/4 子卡，**Need** 许可。
2. 映射到 `adi-hdl/projects/fmcomms2/zc706/`；`create_ip_repo.sh` 用 `BOARD_NAME=${PWD##*/}` 从当前目录名（`boards/zc706_fmcs2/`）反推得到。
3. 四项：Vivado 2022.2 + Vitis、Viterbi 评估许可、Ubuntu 18/20/22、libtinfo5。因为 `Need`，必须特别确保 **Viterbi 译码器许可**已激活。
4. `zc706` 支持 100MHz 或 200MHz（修改 `openwifi.tcl` 中的 `NUM_CLK_PER_US` 后重新 `source`）。

## 6. 本讲小结

- openwifi-hw 以 `BOARD_NAME` 为单位组织板卡支持；README 的板卡表列出了全部官方取值与 Vivado 许可需求。
- 板卡 = Zynq/RFSoC FPGA + ADI 射频前端（AD936x）；板名常自带 FPGA 规模与射频芯片信息（如 `adrv9364z7020`）。
- `BOARD_NAME` 是真正的构建开关：`prepare_adi_board_ip.sh` 用它选 adi-hdl 参考工程，`create_ip_repo.sh` 从目录名反推它并写进条件编译宏。
- 运行环境硬性要求：Vivado **2022.2 + Vitis**（非 Vitis_HLS）、Viterbi 译码器许可、Ubuntu 18/20/22 LTS、`libtinfo5`（Ubuntu 24 需手动装）。
- 注意支持边界：`rfsoc4x2`、`LibreSDR` 虽在 README 表中，但 `boards/` 下无目录、`prepare_adi_board_ip.sh` 未覆盖，使用前需额外确认。
- 基带时钟 `NUM_CLK_PER_US` 是与板卡相关的可调项（zcu102 可 240/100MHz，zc706/adrv9361z7035 可 100/200MHz，其余 100MHz）。

## 7. 下一步学习建议

本讲只看了「有哪些板卡、要什么环境」，还没有真正跑通一次构建。下一讲 **u1-l4 FPGA 构建全流程实战** 会把 `prepare_adi_lib.sh` → `prepare_adi_board_ip.sh` → `get_ip_openofdm_rx.sh` → `create_ip_repo.sh` → `openwifi.tcl` → `sdk_update.sh` 这条完整脚本链路走一遍，回答「从克隆仓库到得到 `system_top.xsa` 要依次执行哪些命令」。

建议提前：

- 重读本讲的 [README.md:L58-L95](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L58-L95)（Build FPGA 小节），带着本讲对 `BOARD_NAME` 与环境变量的理解，先自己把命令顺序理一遍。
- 想进一步了解板卡烧录细节的，可以挑一块带 `Notes` 的社区板（如 README [L35](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L35) 里 `antsdr` 那行附带的 `Notes` 链接）。注意：README 里这些 `Notes` 用的是 `kernel_boot/boards/...` 相对路径，但 `kernel_boot/` 目录并不在 openwifi-hw 仓库内（本仓库不含该目录），它们属于 PS 侧烧录内容，需到对应的软件/内核仓库或社区资料里查找，作为拓展阅读即可。

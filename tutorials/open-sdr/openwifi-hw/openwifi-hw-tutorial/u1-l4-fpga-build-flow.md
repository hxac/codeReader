# FPGA 构建全流程实战（脚本链路）

## 1. 本讲目标

本讲解决一个最实际的问题：**从一个刚克隆下来的 openwifi-hw 仓库，到最终得到可被软件仓库使用的 `system_top.xsa` 硬件镜像，中间到底要依次跑哪些脚本？每一步又产出了什么？**

读完本讲，你应当能够：

- 用一张图说清 openwifi-hw 的「四阶段」构建链路。
- 解释 `prepare_adi_lib.sh`、`prepare_adi_board_ip.sh`、`get_ip_openofdm_rx.sh` 三个准备脚本各自干什么。
- 解释 `boards/create_ip_repo.sh` 如何触发 `ip_repo_gen.tcl`，再触发 `openwifi.tcl`，把 6 个自定义 IP 打包并组装成顶层工程。
- 解释 `sdk_update.sh` 如何把 `.xsa`/`.ltx` 导出给软件仓库。
- 独立列出从克隆到镜像的完整命令序列及每条命令的中间产物。

本讲只讲**脚本链路**（shell + Tcl 之间的调用关系），不深入 Verilog 内部逻辑（那是 u3/u4/u5 的事），也不深入条件编译宏的完整体系（那是 u7-l2 的事）。

## 2. 前置知识

本讲承接 u1-l1、u1-l2、u1-l3，假定你已知：

- openwifi-hw 是 **FPGA 侧（PL）** 的硬件仓库，产物是 `.xsa`/`.ltx`，不是可执行程序；PS 侧的 Linux 驱动在另一个仓库 openwifi。
- 仓库有两个 git 子模块：`adi-hdl`（Analog Devices 的 HDL 参考设计，提供 AD9361 射频底座）与 `ip/openofdm_rx`（OFDM 接收机）。
- `BOARD_NAME` 既是环境变量、目录名，又是条件编译宏，是驱动整条构建链路的开关。

此外需要一点通用基础，下面用大白话点一下：

- **git 子模块（submodule）**：在主仓库里嵌套引用另一个仓库的「某个 commit」。`git submodule init && git submodule update` 才会真正把那个 commit 的内容下载下来；否则子模块目录是空的。openwifi-hw 的两个依赖都是子模块。
- **Vivado 工程**：Xilinx 的 FPGA 开发软件。一个工程里通常有「源文件 + 约束 + 目标器件」，经过**综合（synthesis）→ 实现（implementation）→ 生成比特流（bitstream）** 三步得到烧录 FPGA 的 `.bit` 文件。
- **IP（Intellectual Property）核**：可复用的硬件模块。openwifi-hw 把 WiFi 功能拆成 6 个自定义 IP（xpu/tx_intf/rx_intf/openofdm_tx/openofdm_rx/side_ch）。打包好的 IP 会放进一个「IP 仓（ip_repo）」供顶层工程调用。
- **`.xsa`**：Vivado「导出硬件」时生成的压缩包，包含比特流、寄存器地址映射等，供 Vitis/软件侧使用。
- **`.ltx`**：调试探针文件，配合 ILA（片上逻辑分析仪）抓波形用；只有插入了 ILA 调试核时才会生成。
- **block design**：Vivado IP Integrator 里用图形化方式把多个 IP 连起来的设计，详见 u2。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md) | `Build FPGA` 小节是构建流程的「权威说明书」，所有脚本顺序都来自这里。 |
| [prepare_adi_lib.sh](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_lib.sh) | 拉取并构建 adi-hdl 的通用 HDL 库（只跑一次）。 |
| [prepare_adi_board_ip.sh](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_board_ip.sh) | 为某块板卡生成 ADI 参考设计的板级 IP（每块板跑一次）。 |
| [get_ip_openofdm_rx.sh](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/get_ip_openofdm_rx.sh) | 拉取 `ip/openofdm_rx` 子模块。 |
| [boards/create_ip_repo.sh](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh) | 由目录名反推 `BOARD_NAME`，生成条件编译宏文件，并启动 Vivado 跑 `ip_repo_gen.tcl`。 |
| [boards/ip_repo_gen.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl) | 生成各类参数化 `.v`（时钟/规模/git 版本），循环打包 6 个 IP 进 `ip_repo`，最后 `source openwifi.tcl`。 |
| [boards/openwifi.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl) | 创建并配置顶层 Vivado 工程 `openwifi_$BOARD_NAME`。 |
| [boards/sdk_update.sh](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/sdk_update.sh) | 把 `.xsa`/`.ltx`/git 信息拷贝到镜像目录，供软件仓库消费。 |

辅助文件（顺带会用到）：[get_git_rev.sh](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/get_git_rev.sh)、[ip/parse_board_name.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl)。

---

## 4. 核心概念与源码讲解

### 4.1 构建链路总览：四阶段与中间产物

#### 4.1.1 概念说明

openwifi-hw 的构建不是「一条命令搞定」，而是**一条脚本接力链**。之所以这么设计，是因为它站在 ADI（Analog Devices）参考设计的肩膀上：射频相关的底层设计来自 adi-hdl 子模块，WiFi 物理层来自 openofdm_rx 子模块，6 个自定义 IP 又由本仓库维护。三部分要分别准备好，再组装到一起。

理解这条链，比死记命令重要。因为它解释了「为什么改一个 IP 要重跑 `create_ip_repo.sh`」「为什么换板卡要重跑 `prepare_adi_board_ip.sh`」这类常见疑问。

#### 4.1.2 核心流程

整条链可以分成 **4 个阶段**，每个阶段都有明确的「输入 → 输出」：

```
阶段①  准备依赖（只跑一次 / 每板一次）
   prepare_adi_lib.sh     ──► adi-hdl/library 编译产物（ADI 通用库）
   prepare_adi_board_ip.sh ──► adi-hdl/projects/... 板级参考设计 IP
   get_ip_openofdm_rx.sh  ──► ip/openofdm_rx 子模块内容
                │
                ▼
阶段②  生成 IP 仓 + 搭建顶层工程（在 boards/$BOARD_NAME 下）
   create_ip_repo.sh
        ├─ 生成 ip_config/*_pre_def.v（条件编译宏）
        └─ vivado -source ip_repo_gen.tcl
                ├─ 生成 clock_speed.v / fpga_scale.v / git_rev.v ...
                ├─ 循环 package 6 个 IP → ip_repo/
                └─ source openwifi.tcl ──► 顶层工程 openwifi_$BOARD_NAME
                │
                ▼
阶段③  在 Vivado GUI 人工操作
   Generate Bitstream ──► system_top.bit
   Export Hardware (Include bitstream) ──► system_top.xsa（+ .ltx）
                │
                ▼
阶段④  导出镜像
   sdk_update.sh ──► $OPENWIFI_HW_IMG_DIR/boards/$BOARD_NAME/sdk/
                     （system_top.xsa + .ltx + git_info.txt）
```

这 4 个阶段全部对应 README 的 `Build FPGA` 小节：[README.md:L44-L95](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L44-L95)。

一个关键细节：阶段②其实**内嵌**了阶段③的一部分准备——`ip_repo_gen.tcl` 最后会 `source ../openwifi.tcl`，README 在阶段③下方也注明 `(This step is invoked automatically by previous create_ip_repo.sh)`（[README.md:L89](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L89)）。也就是说 `create_ip_repo.sh` 跑完后，顶层工程已经建好并自动跑过一轮综合/实现，但「点 Generate Bitstream」和「Export Hardware」这两步仍需在 GUI 里确认。

#### 4.1.3 源码精读

四阶段的总纲就在 README 这一段：[README.md:L44-L95](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L44-L95)。其中：

- 阶段①三脚本：[README.md:L58-L74](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L58-L74)。
- 阶段②`create_ip_repo.sh`：[README.md:L75-L80](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L75-L80)。
- 阶段③ Vivado GUI：[README.md:L82-L89](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L82-L89)。
- 阶段④ `sdk_update.sh`：[README.md:L90-L95](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L90-L95)。

注意 README 明确标注了三处「only run once」：ADI 库全局一次、板级 ADI IP 每板一次、openofdm_rx 在 openofdm 更新后一次。这些是「缓存」点，正常迭代 firmware 时不必重跑。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立「四阶段」直觉，而不是死记命令。
2. **操作步骤**：打开 [README.md:L44-L95](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L44-L95)，把每一条命令分别归入阶段①②③④。
3. **需要观察的现象**：注意哪些命令前有 `only run once` 字样。
4. **预期结果**：你应能识别出阶段①的三条命令全部带 `only run once`，阶段②的 `create_ip_repo.sh` 是真正「每次改设计都要重跑」的命令，阶段④ `sdk_update.sh` 是最后交付动作。
5. 待本地验证（无需运行 Vivado，纯阅读即可）。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `prepare_adi_lib.sh` 标注「only run once」，而 `create_ip_repo.sh` 没有？
  - **参考答案**：前者构建的是 adi-hdl 的通用 HDL 库，与板卡和你的改动无关，装一次就够；后者每次重新打包自定义 IP 并重建顶层工程，改了 IP 源码后必须重跑才能让改动生效。
- **练习 2**：阶段③的「Export Hardware」属于脚本自动完成还是人工完成？
  - **参考答案**：人工完成。README 说明 `openwifi.tcl`（即工程创建）会被 `create_ip_repo.sh` 自动触发，但点 `Generate Bitstream` 和 `Export Hardware → Include bitstream` 仍需在 GUI 手动操作（[README.md:L82-L89](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L82-L89)）。

---

### 4.2 准备依赖：ADI 库、板级 ADI IP 与 openofdm_rx 子模块

#### 4.2.1 概念说明

阶段①要做的事，本质是「把两个空的 git 子模块填满，并编译出 ADI 的库与板级参考设计」。

- `adi-hdl` 子模块：提供 AD9361 射频芯片相关的 AXI IP、数据通路（DMA、AXI Streaming 等）。openwifi 的自定义 IP 要和它们对接，所以必须先有 ADI 库。
- `ip/openofdm_rx` 子模块：OFDM 接收机（包检测/同步/FFT/均衡/Viterbi），由 open-sdr 维护的 openofdm fork 提供。
- **板级参考设计**：ADI 为每款「FPGA + 射频」组合预制了一个顶层参考工程（在 `adi-hdl/projects/...`）。openwifi 不从零画射频连线，而是基于这套参考设计再加自己的 IP。

#### 4.2.2 核心流程

```
prepare_adi_lib.sh $XILINX_DIR
   git submodule init/update adi-hdl   ──► 下载子模块
   checkout 2022_R2                     ──► 锁定到 ADI 2022_R2 版本
   cd adi-hdl/library && make           ──► 编译 ADI 通用 HDL 库

prepare_adi_board_ip.sh $XILINX_DIR $BOARD_NAME
   BOARD_NAME → ADI_PROJECT_DIR         ──► 映射到 adi-hdl/projects/... 对应目录
   cd $ADI_PROJECT_DIR && make          ──► 生成该板的参考设计 IP

get_ip_openofdm_rx.sh
   cd ip && submodule init/update openofdm_rx  ──► 填充 OFDM 接收机子模块
```

注意版本耦合：`prepare_adi_lib.sh` 把 adi-hdl **硬锁到 `2022_R2` 分支**，这与 u1-l3 提到的 Vivado 2022.2 是配套的。换 Vivado 版本时这里也要改，正是 u7-l5「迁移」要处理的问题。

#### 4.2.3 源码精读

**`prepare_adi_lib.sh`**——拉子模块并锁定版本：[prepare_adi_lib.sh:L21-L29](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_lib.sh#L21-L29) 做了 `git submodule init/update adi-hdl`，然后 `git reset --hard` + `git checkout 2022_R2`，把 adi-hdl 钉在固定版本。随后 [prepare_adi_lib.sh:L32-L34](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_lib.sh#L32-L34) `source` 了 Vivado 的 `settings64.sh`，进入 `library/` 执行 `make` 编译库。

**`prepare_adi_board_ip.sh`**——`BOARD_NAME` 到 ADI 工程目录的映射表：[prepare_adi_board_ip.sh:L20-L39](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_board_ip.sh#L20-L39)。这里用一串 `if/elif` 把 `BOARD_NAME` 翻译成 `ADI_PROJECT_DIR`。几个值得注意的点：

- `zcu102_fmcs2` → `adi-hdl/projects/fmcomms2/zcu102/`。
- `zc706_fmcs2` → `adi-hdl/projects/fmcomms2/zc706/`。
- 一批小板（`adrv9364z7020`、`antsdr`、`antsdr_e200`、`e310v2`、`sdrpi`、`neptunesdr`）**共用同一个底座** `adi-hdl/projects/adrv9364z7020/ccbob_lvds/`（[prepare_adi_board_ip.sh:L34-L35](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_board_ip.sh#L34-L35)），这与 u1-l3 提到的「社区板多与 adrv9364z7020 共用底座」一致。
- 未列出的板名会直接报错退出（[prepare_adi_board_ip.sh:L36-L39](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_board_ip.sh#L36-L39)）。

随后 [prepare_adi_board_ip.sh:L47-L50](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_board_ip.sh#L47-L50) 进入该目录执行 `make`。README 还贴心提示「不必等到 make 跑完，看到 `Building ABCD project [...` 就可以中断」——因为只要 ADI 的 IP 已经生成到本地仓就够 openwifi 用了（[README.md:L69](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L69)）。

**`get_ip_openofdm_rx.sh`**——填充 openofdm_rx 子模块：[get_ip_openofdm_rx.sh:L6-L8](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/get_ip_openofdm_rx.sh#L6-L8)，只有 `cd ip/` 然后 `git submodule init/update openofdm_rx`。注意脚本里没有再 `checkout dot11zynq`（被注释掉了，[get_ip_openofdm_rx.sh:L9-L11](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/get_ip_openofdm_rx.sh#L9-L11)），说明 `.gitmodules` 里已经把该子模块指向了正确的 commit（dot11zynq 分支），直接 update 即可。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：验证「`BOARD_NAME` → adi-hdl 参考工程」的映射，并理解版本锁定。
2. **操作步骤**：
   - 在 [prepare_adi_board_ip.sh:L20-L39](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_board_ip.sh#L20-L39) 中，为 `zcu102_fmcs2`、`zc706_fmcs2`、`antsdr` 三个板名分别找出对应的 `ADI_PROJECT_DIR`。
   - 在 [prepare_adi_lib.sh:L26-L29](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_lib.sh#L26-L29) 中确认 adi-hdl 被锁定的分支名。
3. **需要观察的现象**：哪些板名共用同一个 `ADI_PROJECT_DIR`。
4. **预期结果**：`zcu102_fmcs2→fmcomms2/zcu102`、`zc706_fmcs2→fmcomms2/zc706`、`antsdr→adrv9364z7020/ccbob_lvds`；adi-hdl 锁定 `2022_R2`。
5. 待本地验证（纯阅读）。

#### 4.2.5 小练习与答案

- **练习 1**：如果你新加了一块基于 `adrv9364z7020` 底座的社区板，阶段①需要改哪些脚本？
  - **参考答案**：主要改 `prepare_adi_board_ip.sh` 的映射表，把新 `BOARD_NAME` 加进共用 `adrv9364z7020/ccbob_lvds` 的那条 `elif`（[prepare_adi_board_ip.sh:L34-L35](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/prepare_adi_board_ip.sh#L34-L35)）；`prepare_adi_lib.sh` 与 `get_ip_openofdm_rx.sh` 一般不用动。
- **练习 2**：为什么 README 说 `prepare_adi_board_ip.sh` 跑到一半就能 `Ctrl-C` 停？
  - **参考答案**：openwifi 只需要 ADI 生成的板级 IP 已落盘，并不需要 adi-hdl 参考工程自身的 bitstream；看到 `Building ABCD project [...` 说明 IP 已就绪，继续等只是白费时间（[README.md:L69](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L69)）。

---

### 4.3 生成 IP 仓并搭建顶层工程：create_ip_repo.sh → ip_repo_gen.tcl → openwifi.tcl

#### 4.3.1 概念说明

这是整条链最核心、也最容易绕晕的一段。它其实是「**一个 shell 脚本嵌套两个 Tcl 脚本**」：

- `create_ip_repo.sh`（shell）：负责「参数准备」——由当前目录名反推 `BOARD_NAME`，把用户传入的条件编译宏写成 `ip_config/*_pre_def.v`，最后启动 Vivado 跑 `ip_repo_gen.tcl`。
- `ip_repo_gen.tcl`（Tcl）：负责「生成参数化文件 + 循环打包 6 个 IP + 启动顶层工程」。它会生成一批带宏的 `.v`（时钟、规模、git 版本…），把每个自定义 IP 打包进 `ip_repo/`，**末尾再 `source openwifi.tcl`**。
- `openwifi.tcl`（Tcl）：负责「创建顶层工程 `openwifi_$BOARD_NAME`」，并会**覆盖**前面生成的 `clock_speed.v`（因为顶层工程才是时钟的最终决定者）。

理解这段链路的关键，是看清楚「谁调用谁、谁覆盖谁」。

#### 4.3.2 核心流程

```
[在 boards/$BOARD_NAME 目录下]
create_ip_repo.sh $XILINX_DIR [可选: $IP1 $DEF1 ...]
  ① BOARD_NAME = 当前目录名 ${PWD##*/}
  ② 检查 Vitis/2022.2/settings64.sh 存在
  ③ 为 6 个 IP 生成 ip_config/<ip>_pre_def.v（写入 `define $BOARD_NAME 及用户宏）
  ④ vivado -source ../ip_repo_gen.tcl
          │
          ▼
  ip_repo_gen.tcl
   ① BOARD_NAME = pwd 末段；source parse_board_name.tcl（得到 part_string、fpga_size_flag...）
   ② 建 ip_repo/，拷入 board_def.v
   ③ 生成 openwifi_hw_git_rev.v / has_side_ch_flag.v / fpga_scale.v / clock_speed.v / spi_command.v
   ④ 循环 ip_name_list：source package_ip_complex.tcl 把每个 IP 打包进 ip_repo/<ip>
        （openofdm_rx 例外：用 append 追加 pre_def，避免覆盖其自带定义）
   ⑤ source ../openwifi.tcl
          │
          ▼
  openwifi.tcl
   ① source set_files.tcl、parse_board_name.tcl
   ② 覆盖生成 clock_speed.v（NUM_CLK_PER_US=100，按 fpga_size_flag 加 SMALL_FPGA）
   ③ 拷 clock_speed.v 进 tx_intf/rx_intf/xpu 的 src
   ④ 创建工程 openwifi_$BOARD_NAME
   ⑤ （GUI）Generate Bitstream → Export Hardware → system_top.xsa
```

一个关键原理——**板卡规模如何变成 Verilog 宏**：`parse_board_name.tcl` 用 `fpga_size_flag`（0=小，1=大）描述 FPGA 容量。小 FPGA 会触发 `SMALL_FPGA` 和 `SIDE_CH_LESS_BRAM` 两个宏，用以缩减缓冲深度、节省 BRAM。这条「板名 → 规模标志 → 条件编译宏」的链路，正是 openwifi 能用同一份源码适配从 zynq 7020 到 zcu102 等不同规模器件的关键。

#### 4.3.3 源码精读

**`create_ip_repo.sh`——从目录名反推 `BOARD_NAME`**：[create_ip_repo.sh:L29](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L29) 是一句 `BOARD_NAME=${PWD##*/}`——取当前目录的最后一段作为板名。这就是为什么 README 要求先 `cd openwifi-hw/boards/$BOARD_NAME/` 再跑脚本（[README.md:L77-L79](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L77-L79)）。

**生成条件编译宏**：[create_ip_repo.sh:L42-L49](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L42-L49) 先为每个 IP 写一行 `` `define $BOARD_NAME ``（注释里解释：最终单一 Vivado 工程里不允许出现多份内容不同的 `pre_def.v`）。随后 [create_ip_repo.sh:L51-L73](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L51-L73) 解析命令行里「IP 名 + 若干 DEF」，把每个 DEF 写成 `` `define ${MODULE_NAME}_${ARGUMENT} ``，例如传 `xpu ENABLE_DBG` 会生成 `` `define XPU_ENABLE_DBG ``。这与 README 的用法说明完全对应（[README.md:L144-L156](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L144-L156)）。脚本末尾 [create_ip_repo.sh:L75-L78](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L75-L78) `source` Vitis 环境并执行 `vivado -source ../ip_repo_gen.tcl`。注意：`create_ip_repo.sh` 检查的是 **Vitis**（不是 Vivado）的 settings（[create_ip_repo.sh:L32-L40](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/create_ip_repo.sh#L32-L40)），呼应 u1-l3 强调的「Vitis 而非 Vitis_HLS」。

**`ip_repo_gen.tcl`——生成参数化文件 + 打包 IP**：

- 取板名并解析器件参数：[ip_repo_gen.tcl:L9-L11](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L9-L11)，同样从 `pwd` 取 `BOARD_NAME`，再 `source ../../ip/parse_board_name.tcl`。
- 建仓 + 拷入板级定义：[ip_repo_gen.tcl:L14-L16](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L14-L16)（拷 `board_def.v`）。
- 生成 git 版本宏：[ip_repo_gen.tcl:L19-L22](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L19-L22)，调用 [get_git_rev.sh:L5-L11](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/get_git_rev.sh#L5-L11) 取 `git log -1 --pretty=%h`，写成 `` `define OPENWIFI_HW_GIT_REV (32'h...) ``，把固件版本烙进硬件。
- 生成规模/时钟宏：[ip_repo_gen.tcl:L38-L53](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L38-L53)，其中 `fpga_size_flag==0`（小器件）会写出 `SIDE_CH_LESS_BRAM` 和 `SMALL_FPGA`。
- 循环打包 6 个 IP：[ip_repo_gen.tcl:L72-L99](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L72-L99)，`ip_name_list` 为 `openofdm_rx openofdm_tx rx_intf tx_intf xpu side_ch`。对每个 IP，先把生成的 `.v` 拷进它的 `src`（[ip_repo_gen.tcl:L81-L89](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L81-L89)），再 `source ../package_ip_complex.tcl` 打包。**openofdm_rx 是例外**：它不在拷贝名单里（因为它是外部子模块，自带这些定义），改用 `cat ... >>` **追加**方式写入 `pre_def`（[ip_repo_gen.tcl:L93-L95](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L93-L95)）。这个「追加而非覆盖」正是近期一次提交「Change new to append mode for _pre_def.v: to avoid overwrite previous defines」的用意——避免抹掉 openofdm_rx 自带的定义。
- 最后启动顶层工程：[ip_repo_gen.tcl:L105](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L105) `source ../openwifi.tcl`。

**`openwifi.tcl`——顶层工程 + 覆盖时钟**：

- 加载文件清单与板级参数：[openwifi.tcl:L13](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L13) `source ./set_files.tcl`，[openwifi.tcl:L16-L18](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L16-L18) 取板名并 `source parse_board_name.tcl`。
- **覆盖** `clock_speed.v`：[openwifi.tcl:L20-L30](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L20-L30)，注释明说 `This overrides the value in ip_repo_gen.tcl!`。这里把 `NUM_CLK_PER_US` 设为 100（默认 100MHz 基带时钟），重写 `clock_speed.v` 并拷给 `tx_intf/rx_intf/xpu`。要换基带时钟，就改这里的 `NUM_CLK_PER_US`（详见 README「Change the baseband clock」，[README.md:L107-L111](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L107-L111)）。
- 创建顶层工程：[openwifi.tcl:L44-L47](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L44-L47)，工程名 `openwifi_$BOARD_NAME`。

`parse_board_name.tcl` 给出的器件参数见 [parse_board_name.tcl:L7-L80](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl#L7-L80)，例如 `zc706_fmcs2` 对应 `xc7z045ffg900-2`、`fpga_size_flag 1`（大），`zcu102_fmcs2` 对应 UltraScale+ `xczu9eg-ffvb1156-2-e`、`ultra_scale_flag 1`，而 `zed_fmcs2`/`zc702_fmcs2`/各社区小板则 `fpga_size_flag 0`（小）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：追踪「板名 → 器件参数 → 条件编译宏 → 打包进 IP」这条链。
2. **操作步骤**：
   - 在 [parse_board_name.tcl:L7-L80](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl#L7-L80) 中查出 `zc706_fmcs2` 的 `part_string` 与 `fpga_size_flag`。
   - 据此判断 [ip_repo_gen.tcl:L38-L53](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L38-L53) 会写出哪些宏。
   - 在 [ip_repo_gen.tcl:L81-L95](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L81-L95) 中确认：`openofdm_rx` 与其他 5 个 IP 在处理 `pre_def` 时的差别。
3. **需要观察的现象**：`fpga_size_flag` 为 0 和 1 时生成的宏有何不同；openofdm_rx 为何用 `cat >>` 而不是 `cp`。
4. **预期结果**：`zc706_fmcs2` 是大器件（`fpga_size_flag=1`），不会写 `SMALL_FPGA`/`SIDE_CH_LESS_BRAM`；openofdm_rx 用追加模式保留其自带定义。
5. 待本地验证（纯阅读）。

#### 4.3.5 小练习与答案

- **练习 1**：如果我把基带时钟从 100MHz 改成 200MHz（假设板卡支持），要改哪里？是否要重跑阶段①？
  - **参考答案**：改 [openwifi.tcl:L21](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L21) 的 `set NUM_CLK_PER_US 100`（改为 200，含义见 README [L107-L111](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L107-L111)），然后重跑 `openwifi.tcl`（即从阶段②起）。不必重跑阶段①，因为时钟是 openwifi 自有逻辑，与 ADI 库无关。
- **练习 2**：为什么 `openofdm_rx` 不能像其他 5 个 IP 那样直接 `cp clock_speed.v` 进它的 `src`？
  - **参考答案**：`openofdm_rx` 是外部子模块，自带一套 `pre_def`/参数定义；直接覆盖会抹掉它的定义。所以脚本对它用 `cat ... >>` 追加（[ip_repo_gen.tcl:L93-L95](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L93-L95)），且 [ip_repo_gen.tcl:L81-L89](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L81-L89) 的拷贝循环用 `if {$ip_name != "openofdm_rx"}` 把它排除在外。

---

### 4.4 导出硬件镜像：sdk_update.sh

#### 4.4.1 概念说明

阶段②③产出 `system_top.xsa`（含比特流的硬件导出）和可选的 `system_top.ltx`（调试探针）。但这两个文件还躺在 Vivado 工程目录深处，软件仓库（openwifi）并不知道去哪儿拿。`sdk_update.sh` 的职责就是把它们**规整地搬到一个固定的镜像目录**，顺带记录 git 版本信息，供软件构建环境「按板卡取用」。

#### 4.4.2 核心流程

```
sdk_update.sh $BOARD_NAME $OPENWIFI_HW_IMG_DIR
  目标目录 = $OPENWIFI_HW_IMG_DIR/boards/$BOARD_NAME/sdk/
  拷入：
    openwifi_$BOARD_NAME/system_top.xsa          ──► 硬件镜像
    .../impl_1/system_top.ltx（若存在）          ──► ILA 调试探针
  写入：
    git_info.txt                                  ──► 主仓库 + openofdm_rx 的分支/最近 3 条 commit
```

这与 u1-l1 提到的「`openwifi-hw-img` 仓库」是对应的：`$OPENWIFI_HW_IMG_DIR` 通常就是那个镜像仓库的本地路径，`sdk_update.sh` 把产物「投递」过去。

#### 4.4.3 源码精读

- 参数与目标目录：[sdk_update.sh:L15-L26](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/sdk_update.sh#L15-L26)，目标固定为 `$OPENWIFI_HW_IMG_DIR/boards/$BOARD_NAME/sdk/`，不存在则创建，并先清空。
- 拷贝 xsa 与 ltx：[sdk_update.sh:L29-L34](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/sdk_update.sh#L29-L34)。`.xsa` 来自 `openwifi_$BOARD_NAME/system_top.xsa`；`.ltx` 来自 `.../openwifi_$BOARD_NAME.runs/impl_1/system_top.ltx`，若没插 ILA 则打印 `No debug probe file found.`（这正是 `.ltx`「可选」的体现，与 u1-l1 所述一致）。
- 记录 git 信息：[sdk_update.sh:L36-L46](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/sdk_update.sh#L36-L46)，分别写入主仓库（`git branch`、`git log -3`）和 openofdm_rx 子模块（`git --git-dir ../ip/openofdm_rx/.git ...`）的分支与最近 3 条提交，便于事后溯源「这份镜像到底是用哪个 commit 构建的」。

README 把它定位为「store the FPGA files to a specific directory」：[README.md:L90-L95](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L90-L95)。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：搞清镜像交付目录结构与溯源信息。
2. **操作步骤**：阅读 [sdk_update.sh:L20-L46](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/sdk_update.sh#L20-L46)，列出最终 `sdk/` 目录里会有哪些文件，并指出 `.ltx` 在什么情况下缺失。
3. **需要观察的现象**：`git_info.txt` 同时记录了哪两个仓库的信息。
4. **预期结果**：`sdk/` 含 `system_top.xsa`、（可选）`system_top.ltx`、`git_info.txt`；后者记录主仓库与 `ip/openofdm_rx` 两个仓库的分支和 commit。
5. 待本地验证（纯阅读）。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `.ltx` 有时不存在？
  - **参考答案**：`.ltx` 是 ILA 调试探针文件，只有构建时插入了 ILA 调试核（例如通过 `ENABLE_DBG` 宏，见 u7-l2/u7-l6）才会生成。脚本里用 `if [ -f ... ]` 判断，缺失时只打印提示而不报错（[sdk_update.sh:L30-L34](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/sdk_update.sh#L30-L34)）。
- **练习 2**：`sdk_update.sh` 写入的 git 信息有什么用？
  - **参考答案**：把「这份硬件镜像由哪个版本的 openwifi-hw + openofdm_rx 构建」烙进交付物，便于出现问题时溯源复现（[sdk_update.sh:L36-L46](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/sdk_update.sh#L36-L46)）。

---

## 5. 综合实践

**任务**：按照 README 的 `Build FPGA` 小节，写出从「克隆仓库」到「得到 `system_top.xsa`」的完整命令序列，并逐条标注它产出的中间产物。

请先自己填写下表的「命令」和「中间产物」两列，再对照后面的参考答案。

| 阶段 | 命令（在仓库根目录执行） | 中间产物 |
|------|--------------------------|----------|
| 0 准备 | `git clone ...` 并 `cd openwifi-hw` | （子模块为空） |
| ① | 1. `export XILINX_DIR=...` 后 `./prepare_adi_lib.sh $XILINX_DIR` | ? |
| ① | 2. `export BOARD_NAME=zc706_fmcs2` 后 `./prepare_adi_board_ip.sh $XILINX_DIR $BOARD_NAME` | ? |
| ① | 3. `./get_ip_openofdm_rx.sh` | ? |
| ② | 4. `cd boards/$BOARD_NAME && ../create_ip_repo.sh $XILINX_DIR` | ? |
| ③ | 5. Vivado GUI：`source ../openwifi.tcl` → Generate Bitstream → Export Hardware(Include bitstream) | ? |
| ④ | 6. `cd .. && ./sdk_update.sh $BOARD_NAME $OPENWIFI_HW_IMG_DIR` | ? |

**参考答案**：

1. **`prepare_adi_lib.sh`** → `adi-hdl` 子模块填充并锁定到 `2022_R2`，`adi-hdl/library` 编译出 ADI 通用 HDL 库。
2. **`prepare_adi_board_ip.sh`** → `adi-hdl/projects/fmcomms2/zc706`（zc706_fmcs2 映射）下 `make` 生成该板的 ADI 参考设计 IP。
3. **`get_ip_openofdm_rx.sh`** → `ip/openofdm_rx` 子模块填充（OFDM 接收机源码到位）。
4. **`create_ip_repo.sh`** → 生成 `ip_config/*_pre_def.v`，跑 `ip_repo_gen.tcl` 生成 `clock_speed.v`/`fpga_scale.v`/`openwifi_hw_git_rev.v` 等并打包 6 个 IP 到 `ip_repo/`，再 `source openwifi.tcl` 创建顶层工程 `openwifi_zc706_fmcs2`。
5. **Vivado GUI** → `system_top.bit`（比特流），Export Hardware 得到 **`system_top.xsa`**（含比特流）和可选 `system_top.ltx`。
6. **`sdk_update.sh`** → `$OPENWIFI_HW_IMG_DIR/boards/zc706_fmcs2/sdk/` 下落地 `system_top.xsa`、（可选）`system_top.ltx`、`git_info.txt`。

> 说明：以上命令需在本机 Vivado 2022.2 + Vitis 环境中实际运行才能验证产物；本实践以「能正确列出命令与产物」为达标。Viterbi 译码器评估许可、`libtinfo5` 等前置条件见 u1-l3。

## 6. 本讲小结

- openwifi-hw 的构建是**四阶段接力链**：准备依赖 → 生成 IP 仓并搭顶层工程 → GUI 综合/实现/导出 → 导出镜像。
- 阶段①三脚本对应「ADI 通用库 / 板级 ADI 参考 IP / openofdm_rx 子模块」，多标注 `only run once`；`prepare_adi_lib.sh` 把 adi-hdl 锁定到 `2022_R2`。
- `prepare_adi_board_ip.sh` 用一张 `if/elif` 表把 `BOARD_NAME` 映射到 `adi-hdl/projects/...`，多个社区小板共用 `adrv9364z7020/ccbob_lvds` 底座。
- `create_ip_repo.sh` 由「当前目录名」反推 `BOARD_NAME`，生成条件编译宏 `_pre_def.v`，再启动 `ip_repo_gen.tcl`。
- `ip_repo_gen.tcl` 生成时钟/规模/git 版本等参数化 `.v`，循环打包 6 个 IP 进 `ip_repo/`（openofdm_rx 用追加模式保留自带定义），末尾 `source openwifi.tcl`。
- `openwifi.tcl` 创建顶层工程并**覆盖** `clock_speed.v`（基带时钟的最终决定点）；`sdk_update.sh` 把 `.xsa`/`.ltx`/git 信息投递到镜像目录供软件仓库消费。

## 7. 下一步学习建议

- 想看顶层工程长什么样、6 个 IP 如何拼成 `openwifi_ip` 层级，进入 **u2-l1（system_top 与 block design）** 和 **u2-l2（openwifi_ip 层级）**。
- 想理解「板名 → 器件 → 时钟宏」的更细节，看 **u2-l4（板级配置与时钟体系）**。
- 想深入条件编译宏（`SMALL_FPGA`、`HAS_SIDE_CH`、`ENABLE_DBG` 等）的完整体系与如何用命令行注入，看 **u7-l2（条件编译与 Verilog 宏体系）**。
- 后续若要修改单个 IP 再集成回顶层，可结合本讲的阶段②与 **u7-l4（修改并打包自定义 IP）**。

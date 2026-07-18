# 仓库结构与设备目录组织

> 承接上一篇：你已经知道 pcileech-fpga 是一套基于 Xilinx Artix-7 的 SystemVerilog 固件工程，作为 PCIe DMA 硬件端，配合 PCILeech / MemProcFS 工作，并以 `PCIeSquirrel/` 作为主参考工程。本篇不再重复这些定位，而是带你「打开仓库」，看懂它的目录是怎么组织的。

## 1. 本讲目标

学完本讲，你应当能够：

- 说出仓库顶层「一个设备一个目录」的组织方式，并把每个目录对应到 readme 的设备行。
- 在任意一个设备子目录里，分辨 `src/`、`ip/`、约束文件（`.xdc`）、TCL 脚本各自存放什么、起什么作用。
- 区分**跨设备复用的公共 HDL 文件**（如 `pcileech_com.sv`、`pcileech_fifo.sv`）与**设备特定文件**（如 `*_top.sv`、`*.xdc`、通信核心）。
- 识别「较新设备带 `bar_controller` + `tlps128_cfgspace_shadow`」与「较老设备使用旧版 `pcie_cfgspace_shadow`」的源码差异。

## 2. 前置知识

上一篇讲过了 PCIe / DMA / TLP / FPGA / HDL。本篇会再用到几个与「工程组织」相关的概念，先用大白话解释：

- **工程（Project）/ Vivado 工程**：Xilinx Vivado 把「源码 + 约束 + IP 核 + 构建脚本」打包成一个工程，最终产出可以烧进 FPGA 的比特流（bitstream / `.bin`）。你可以把一个工程理解成「一块板子的完整固件配方」。
- **SystemVerilog 源文件（`.sv` / `.svh`）**：本项目的 HDL 源码几乎都是 SystemVerilog。`.sv` 是普通源文件，`.svh` 是被多个 `.sv` 用 `` `include `` 共享的头文件（里面常放接口 `interface` 定义和宏）。
- **Xilinx IP 核（`.xci`）**：IP 是 Xilinx 提供的「可参数化的现成电路模块」，比如 FIFO、BRAM（块存储）、PCIe 硬核。`.xci` 是这个 IP 的配置描述文件，Vivado 据此重新生成 IP。
- **内存初始化文件（`.coe`）**：一种文本格式，用来给 BRAM/ROM 这类存储 IP 设定初始内容（例如配置空间的默认值）。
- **约束文件（`.xdc`）**：告诉综合/实现工具「某个信号连到芯片的哪个物理引脚」「哪条路径要做时序豁免」。它把抽象的 HDL 信号绑定到具体板卡的硬件引脚上。
- **TCL 脚本（`.tcl`）**：Vivado 的命令语言。本项目用 TCL 脚本自动「生成工程」和「构建比特流」，避免手动点 GUI。

> 一句话记忆：**`.sv` 是电路逻辑，`.xci`/`.coe` 是 Xilinx 现成模块及其初始值，`.xdc` 是引脚/时序约束，`.tcl` 是构建自动化脚本。**

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| `readme.md`（仓库根） | 顶层说明：支持的全部设备清单（含连接方式、速率、FPGA 型号）。 |
| `PCIeSquirrel/src/pcileech_squirrel_top.sv` | 主参考工程的**顶层模块**，例化了 com / fifo / pcie 三大子系统。本篇用它说明「顶层文件为何是设备特定的」。 |
| `PCIeSquirrel/src/pcileech_header.svh` | 公共头文件，定义跨设备复用的 `interface`（本篇引用其一说明「公共契约」的概念）。 |
| `CaptainDMA/readme.md` | 多设备家族说明，其固件表展示了「同一目录下按 FPGA 型号 / lane 数再分子工程」的组织方式。 |

## 4. 核心概念与源码讲解

### 4.1 仓库顶层目录：「一设备一目录」的组织

#### 4.1.1 概念说明

pcileech-fpga 支持十几种不同的硬件设备，它们用的 FPGA 型号、封装、对外连接方式（USB3 / 以太网 / Thunderbolt）、PCIe 通道数都不一样。仓库采用最直观的组织方式：**顶层每一个目录就是一种设备（或一个设备家族）**，目录里装着「为这块板子量身定做的完整 Vivado 工程所需的全部文件」。

这样做的好处是：不同板卡的引脚映射、物理约束差别极大，强行混在一起会非常混乱；一个目录 = 一个独立可构建的工程，互不干扰，也方便你「找一个最接近自己硬件的工程当模板」。

#### 4.1.2 核心流程

阅读仓库的顺序建议是：

1. 先看根 `readme.md` 的设备表，建立「设备名 → FPGA 型号 → 连接方式 → 速率」的全局印象。
2. 根据你感兴趣的设备，进入对应目录。
3. 在该目录里再看它自己的 `readme.md` / `build.md`，了解购买、烧录、构建细节。

#### 4.1.3 源码精读

根 `readme.md` 顶部就给出了「当前支持设备」的完整对照表，每个设备名都直接是顶层的一个目录名：

[readme.md:12-23](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L12-L23) —— 当前支持设备表：列出 ZDMA、GBOX、CaptainDMA 家族、LeetDMA、Enigma X1、AC701/FT601 等的连接方式、传输速率、FPGA 型号与 PCIe 版本，表中的设备名（如 `ZDMA`、`CaptainDMA`）就是仓库顶层目录名。

根 `readme.md` 还有「旧设备（Older Devices）」表，列出了资料较旧但仍可用的设备：

[readme.md:57-64](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/readme.md#L57-L64) —— 旧设备表：包含 PCIeScreamer、SP605/FT601、Acorn/FT2232H、NeTV2、Screamer PCIe Squirrel、ScreamerM2 等。注意主参考工程 **Screamer PCIe Squirrel 对应的目录名是 `PCIeSquirrel/`**。

因此仓库顶层共有约 11 个设备目录，例如：`CaptainDMA/`、`EnigmaX1/`、`GBOX/`、`NeTV2/`、`PCIeSquirrel/`、`ScreamerM2/`、`ZDMA/`、`ac701_ft601/`、`acorn_ft2232h/`、`pciescreamer/`、`sp605_ft601/`。

#### 4.1.4 代码实践

1. **目标**：建立「顶层目录 ↔ readme 设备行」的对应关系。
2. **步骤**：打开仓库根目录与 `readme.md`，逐一核对：顶层每个目录名，能在 readme 的两张设备表里找到对应的行吗？
3. **观察**：你会发现几乎所有顶层目录都对应表里一个设备；少数目录（如 `CaptainDMA/`）是一个**家族**，内部还会再分。
4. **预期结果**：能画出一张「目录名 → 设备 → 连接方式 → 速率」的小对照表（这与上一篇的练习一脉相承，但本篇强调目录与设备的**一一/一对多**关系）。

#### 4.1.5 小练习与答案

- **练习**：`PCIeSquirrel/` 目录对应 readme 里的哪个设备名？它属于「当前支持」还是「旧设备」？
- **答案**：对应 **Screamer PCIe Squirrel**，出现在「Older Devices（旧设备）」表中；它仍是后续讲义的主参考工程（资料最全、性价比最佳）。

---

### 4.2 单设备子目录的职责划分：src / ip / 约束 / tcl

#### 4.2.1 概念说明

进入任意一个「单工程设备」目录（如 `PCIeSquirrel/`），你会看到几个固定的组成部分。理解它们的分工，是看懂任何 pcileech-fpga 设备工程的关键：

| 组成 | 典型位置 / 后缀 | 存放内容 |
| --- | --- | --- |
| **SystemVerilog 源码** | `src/*.sv`、`src/*.svh` | 电路逻辑（顶层、通信、FIFO 控制、PCIe、TLP 处理等）。 |
| **Xilinx IP 定义** | `ip/*.xci` | FIFO / BRAM / ROM / PCIe 硬核等可参数化模块的配置。 |
| **IP 初始化数据** | `ip/*.coe` | 给 BRAM/ROM 设定初始值（如配置空间默认内容）。 |
| **约束文件** | `src/*.xdc` | 引脚分配（PACKAGE_PIN / IOSTANDARD）与时序约束。 |
| **TCL 脚本** | `vivado_generate_project.tcl`、`vivado_build.tcl` | 自动生成工程、综合实现并产出比特流。 |
| **说明文档** | `readme.md`、`build.md` | 购买、烧录、构建步骤说明。 |

> 注意：在本仓库里约束文件 `.xdc` 通常和 `.sv` 一起放在 `src/` 下（而不是单独的 `xdc/` 目录）；`ip/` 则专门放 `.xci` 和 `.coe`。规格里提到的「src/ip/xdc/tcl」指的是这**四类内容**的职责划分，不一定是四个物理目录。

#### 4.2.2 核心流程

一个设备工程从「源码」到「比特流」的组成关系可以这样理解：

```
设备目录/
├── src/                  ← 你要读/改的 HDL 逻辑与引脚约束
│   ├── *_top.sv          ← 顶层模块（整个工程的入口）
│   ├── pcileech_*.sv     ← 各功能子模块
│   ├── pcileech_header.svh ← 公共接口/宏定义
│   └── *.xdc             ← 引脚 + 时序约束
├── ip/                   ← Xilinx IP 配置与初始数据（构建时重新生成）
│   ├── *.xci
│   └── *.coe
├── vivado_generate_project.tcl  ← 把上面这些组装成一个 Vivado 工程
├── vivado_build.tcl             ← 跑综合/实现，产出 .bin
└── readme.md / build.md         ← 给人看的说明
```

#### 4.2.3 源码精读

以 `PCIeSquirrel/` 为例，顶层文件 `pcileech_squirrel_top.sv` 的文件头就说明了自己是「哪类板子的顶层」：

[PCIeSquirrel/src/pcileech_squirrel_top.sv:1-8](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L1-L8) —— 文件头注释明确写出这是「用于多种 35T-484 x1 Artix-7 板卡的顶层模块」。这正说明**顶层文件是设备特定的**：它绑定了一类板卡的 FPGA 型号（35T）、封装（484）、PCIe 通道数（x1）。

顶层模块对外暴露的端口，就是这块板卡的**物理引脚**——系统时钟、FT601 USB 芯片数据线、PCIe 差分对、LED、按键等：

[PCIeSquirrel/src/pcileech_squirrel_top.sv:19-52](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L19-L52) —— 顶层端口列表：`clk`/`ft601_clk` 是两个时钟域的来源；`pcie_tx_p/n`、`pcie_rx_p/n`、`pcie_clk_p/n`、`pcie_perst_n` 是 PCIe 物理引脚；`ft601_data`/`ft601_be`/`ft601_rxf_n` 等是与 FT601 USB3 芯片相连的数据/控制引脚。这些端口必须和 `.xdc` 里的引脚约束一一对应。

而顶层内部只是**例化三大子系统**，把它们用 `interface` 连起来：

[PCIeSquirrel/src/pcileech_squirrel_top.sv:98-98](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L98-L98) —— 例化 `pcileech_com i_pcileech_com`（通信核心，连 FT601）。

[PCIeSquirrel/src/pcileech_squirrel_top.sv:122-122](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L122-L122) —— 例化 `pcileech_fifo i_pcileech_fifo`（FIFO 控制中枢，做路由与寄存器管理）。

[PCIeSquirrel/src/pcileech_squirrel_top.sv:146-146](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L146-L146) —— 例化 `pcileech_pcie_a7 i_pcileech_pcie_a7`（PCIe 核心）。

这三个被例化的模块（`pcileech_com`、`pcileech_fifo`、`pcileech_pcie_a7`）的源文件都在同一个 `src/` 目录里，是**跨设备复用的公共 HDL**（见 4.3）。

#### 4.2.4 代码实践

1. **目标**：在 `PCIeSquirrel/` 里把「四类内容」各找一个代表。
2. **步骤**：进入 `PCIeSquirrel/`，分别定位：① 一个顶层 `.sv`；② 一个 `.xdc` 约束；③ `ip/` 下的一个 `.xci` 和一个 `.coe`；④ 两个 `.tcl` 脚本。
3. **观察**：`src/` 下既有 `.sv` 也有 `.xdc`；`ip/` 下全是 `.xci`/`.coe`；两个 TCL 脚本分别负责「生成工程」和「构建」。
4. **预期结果**：你能说出 `pcileech_squirrel_top.sv`（顶层）、`pcileech_squirrel.xdc`（约束）、`pcileech_cfgspace.coe`（配置空间初值）、`vivado_generate_project.tcl` / `vivado_build.tcl`（构建脚本）分别属于哪一类。

#### 4.2.5 小练习与答案

- **练习 1**：约束文件 `pcileech_squirrel.xdc` 为什么必须随设备而变？
- **答案**：因为不同板卡把同一个逻辑信号（如 `clk`、`ft601_data[0]`、`pcie_rx_p`）连到了 FPGA 不同的物理引脚上，`.xdc` 里的 `PACKAGE_PIN`/`IOSTANDARD` 必须与具体板卡走线一致，否则综合出的比特流无法在硬件上工作。
- **练习 2**：`.coe` 和 `.xci` 是什么关系？
- **答案**：`.xci` 描述一个 Xilinx IP（如某块 BRAM）的配置；`.coe` 是给该 IP 中存储部分提供的**初始内容**。Vivado 重新生成 IP 时会读取 `.xci`，并按其中的设定加载对应的 `.coe`。

---

### 4.3 公共文件 vs 设备特定文件：新旧设备的源码差异

#### 4.3.1 概念说明

虽然每个设备工程自成一体，但你会发现**大量 `.sv` 文件在不同设备目录里同名重复出现**。这些是项目作者刻意抽取的**公共 HDL**：它们实现与具体板卡无关的核心逻辑（如何收发数据、如何路由、如何处理 TLP），所以可以原样复用到不同设备。

与之相对，**设备特定文件**只属于某一块板子，主要分三类：

1. **顶层模块 `*_top.sv`**：端口绑定具体引脚，名字带设备标识（如 `pcileech_squirrel_top.sv`、`pcileech_netv2_top.sv`）。
2. **约束文件 `*.xdc`**：引脚与时序，完全是板卡特定的。
3. **通信核心**：取决于这块板子用什么芯片连主机——USB3 用 FT601（`pcileech_ft601.sv`）、以太网用 `pcileech_eth.sv`、USB2 用 FT2232H 等。

此外，项目存在一条**演进分界线**：

- **较新设备**：同时带有 `pcileech_tlps128_cfgspace_shadow.sv`（自定义配置空间影子）和 `pcileech_tlps128_bar_controller.sv`（BAR PIO 控制器），TLP 处理能力更强、可仿真设备。
- **较老设备**：只有旧版的 `pcileech_pcie_cfgspace_shadow.sv`（注意是 `pcie_` 前缀，不是 `tlps128_`），且**没有** `bar_controller`。

识别这条分界线，是判断「某个设备工程能不能用来做设备仿真/二次开发」的关键。

#### 4.3.2 核心流程

判断一个 `.sv` 文件属于「公共」还是「特定」的速查法：

```
文件名以 pcileech_ 开头 + 描述通用功能(com/fifo/mux/pcie/pcie_cfg/pcie_tlp)
   → 多半是【跨设备复用的公共 HDL】
文件名带设备标识(_squirrel / _netv2 / _35t325_x1 ...)
   → 【设备特定的顶层】
文件名是 pcileech_ft601 / pcileech_eth / ...
   → 【设备特定的通信核心】（取决于主机连接方式）
约束 *.xdc
   → 【设备特定】
```

#### 4.3.3 源码精读

公共 HDL 的「公共性」体现在：它们靠 `interface`（接口）而非硬连线互相连接，因此与具体引脚解耦。接口定义集中在公共头文件里：

[PCIeSquirrel/src/pcileech_header.svh:19-35](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L19-L35) —— 定义了 `IfComToFifo` 接口及其 `mp_com` / `mp_fifo` 两个 modport（方向视图）。这种「接口 + modport」就是 com 模块与 fifo 模块之间的连接契约，与板卡引脚无关，所以 `pcileech_com.sv`、`pcileech_fifo.sv` 能被各设备原样复用。

顶层把这些接口实例化后，分发给三大子系统：

[PCIeSquirrel/src/pcileech_squirrel_top.sv:66-73](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L66-L73) —— 顶层声明了 5 个 interface 实例：`dcom_fifo`（com↔fifo）、`dcfg`/`dtlp`/`dpcie`（fifo↔pcie 的配置/TLP/核心三组）、`dshadow2fifo`（配置空间影子↔fifo）。公共子模块就靠它们互通，顶层只负责「连线」。

**设备家族的组织差异**——`CaptainDMA/` 不是单工程，而是一个**按 FPGA 型号 + PCIe lane 数再细分**的多工程家族：

[CaptainDMA/readme.md:77-84](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/readme.md#L77-L84) —— 固件表把每款 CaptainDMA 硬件映射到一个具体的「FPGA Project」子目录，如 CaptainDMA M2 x1 → `35t325_x1`、CaptainDMA 75T → `75t484_x1`、CaptainDMA 100T → `100t484_x1`。

对应到仓库里，`CaptainDMA/` 下确实有多个自成体系的子工程目录，命名规律大致是 `<FPGA 型号简写><封装引脚数>_<PCIe 通道数>`，例如：

| 子目录 | 含义 |
| --- | --- |
| `CaptainDMA/35t325_x1` | Artix-7 35T、325 封装、PCIe x1 |
| `CaptainDMA/35t325_x4` | Artix-7 35T、325 封装、PCIe x4 |
| `CaptainDMA/35t484_x1` | Artix-7 35T、484 封装、PCIe x1 |
| `CaptainDMA/75t484_x1` | Artix-7 75T、484 封装、PCIe x1 |
| `CaptainDMA/100t484-1` | Artix-7 100T、484 封装、x1 变体 |

每个子目录都自带完整的 `src/` + `ip/` + `.tcl`，是一个**独立可构建的工程**。这和 `PCIeSquirrel/`（单工程）形成对比：有的设备一个目录就是一个工程，有的设备一个目录是一个家族、内含多个工程。

**新旧设备的源码分界**（用 grep 全仓搜索可验证）：

- **较新设备**（同时含两个新文件）：`PCIeSquirrel`、`ScreamerM2`、`EnigmaX1`、`GBOX`、`ZDMA/100T`、`ac701_ft601`、以及 `CaptainDMA/` 下全部子工程 —— 都带 `pcileech_tlps128_cfgspace_shadow.sv` 与 `pcileech_tlps128_bar_controller.sv`。
- **较老设备**（只有旧文件、无 bar_controller）：`NeTV2`、`pciescreamer`、`acorn_ft2232h` —— 用旧版 `pcileech_pcie_cfgspace_shadow.sv`，没有 `bar_controller`。

> 这条分界线很重要：本手册第 4 单元（配置空间影子与 BAR 设备仿真）的内容，只适用于「较新设备」那批工程。

#### 4.3.4 代码实践

1. **目标**：用文件名规律，快速判定「公共 vs 特定」与「新 vs 旧」。
2. **步骤**：在仓库里挑两个设备（如 `PCIeSquirrel` 和 `pciescreamer`），列出各自 `src/` 下的 `.sv` 文件名；按 4.3.2 的速查法分类。
3. **观察**：两边的 `pcileech_com.sv`、`pcileech_fifo.sv`、`pcileech_mux.sv`、`pcileech_pcie_cfg_a7.sv`、`pcileech_pcie_tlp_a7.sv` 名字完全一致（公共）；而顶层（`*_top.sv`）和约束（`*.xdc`）各不相同（特定）。
4. **预期结果**：你能指出 `pciescreamer` 属于「较老设备」（只有 `pcileech_pcie_cfgspace_shadow.sv`，没有 `bar_controller`），而 `PCIeSquirrel` 属于「较新设备」（带 `tlps128_cfgspace_shadow` + `tlps128_bar_controller`）。

#### 4.3.5 小练习与答案

- **练习 1**：`pcileech_ft601.sv` 是公共文件还是设备特定文件？为什么？
- **答案**：它是**设备特定的通信核心**。FT601 是 USB3 桥接芯片，只有「用 FT601 连主机」的板子（如 PCIeSquirrel、ScreamerM2、CaptainDMA、ac701_ft601、pciescreamer）才有它；用以太网的 NeTV2 改用 `pcileech_eth.sv`，根本没有这个文件。
- **练习 2**：怎样一眼判断某设备工程是否支持「BAR 设备仿真」？
- **答案**：看它的 `src/` 里有没有 `pcileech_tlps128_bar_controller.sv`（并搭配 `pcileech_tlps128_cfgspace_shadow.sv`）。有就是较新设备、支持 BAR PIO；只有旧版 `pcileech_pcie_cfgspace_shadow.sv` 的则不支持。

## 5. 综合实践

**任务**：对比 `PCIeSquirrel/` 和 `NeTV2/` 两个设备目录的 `src/`，列出文件异同，并指出 NeTV2 多出和缺少哪些文件（提示：关注通信核心 `eth` 与 `ft601`，以及配置空间影子的新旧差异）。

**操作步骤**：

1. 分别列出 `PCIeSquirrel/src/` 与 `NeTV2/src/` 下的全部文件。
2. 按「两边都有（公共）/ 仅 PCIeSquirrel 有 / 仅 NeTV2 有」三类归并。
3. 重点解释「仅一方有」的功能性差异（顶层与约束天然不同，可归为一类说明，不必逐个纠结）。

**预期结果（基于仓库实际文件清单，可直接核对）**：

- **两边都有（跨设备复用的公共 HDL，共 7 个）**：
  `pcileech_com.sv`、`pcileech_fifo.sv`、`pcileech_header.svh`、`pcileech_mux.sv`、`pcileech_pcie_a7.sv`、`pcileech_pcie_cfg_a7.sv`、`pcileech_pcie_tlp_a7.sv`。
- **仅 PCIeSquirrel 有（NeTV2 缺少）**：
  - `pcileech_ft601.sv` —— FT601 USB3 通信核心。
  - `pcileech_tlps128_cfgspace_shadow.sv` —— **新版**自定义配置空间影子。
  - `pcileech_tlps128_bar_controller.sv` —— **新版** BAR PIO 控制器。
  - （以及设备特定的 `pcileech_squirrel_top.sv`、`pcileech_squirrel.xdc`。）
- **仅 NeTV2 有（NeTV2 多出）**：
  - `pcileech_eth.sv` —— **以太网（UDP/IP）通信核心**，替代了 FT601 的角色（这正是提示里的「eth」）。
  - `pcileech_pcie_cfgspace_shadow.sv` —— **旧版**配置空间影子（注意 `pcie_` 前缀，区别于新版的 `tlps128_`），且**没有**对应的 `bar_controller`。
  - （以及设备特定的 `pcileech_netv2_top.sv`、`netv2.xdc`。）

**结论性观察**：

1. **通信核心可替换**：PCIeSquirrel 走 USB3（FT601），NeTV2 走以太网（eth），所以一个有 `pcileech_ft601.sv`、另一个有 `pcileech_eth.sv`，互不共存。
2. **新旧代际差异**：PCIeSquirrel 是「较新设备」（新版 cfgspace shadow + bar_controller），NeTV2 是「较老设备」（旧版 cfgspace shadow、无 bar_controller）——这与 4.3 的全仓结论完全一致。
3. **核心数据通路一致**：两边的 com/fifo/mux/pcie/pcie_cfg/pcie_tlp 七个公共文件同名，说明无论用什么通信介质，**主机↔FIFO↔PCIe 的核心骨架是共享的**，只是换了「通信核心」这一层。

> 说明：以上文件清单是直接核对仓库得到的确定性结果，无需运行即可复现；如果你想亲手验证，用 `ls PCIeSquirrel/src/ NeTV2/src/` 对照即可。

## 6. 本讲小结

- 仓库顶层采用「**一设备一目录**」的组织，目录名与 `readme.md` 设备表一一对应；个别目录（如 `CaptainDMA/`）是家族，内部再按「FPGA 型号 + 封装 + PCIe lane」细分多个独立工程。
- 单设备工程由四类内容构成：`src/`（`.sv`/`.svh` 逻辑 + `.xdc` 约束）、`ip/`（`.xci` IP 配置 + `.coe` 初始数据）、`.tcl`（生成与构建脚本）、`readme.md`/`build.md`（说明）。本仓库约束文件与源码同放 `src/`。
- **顶层 `*_top.sv` 与 `*.xdc` 是设备特定的**（绑定具体引脚）；`pcileech_com/fifo/mux/pcie/pcie_cfg/pcie_tlp` 等**同名文件是跨设备复用的公共 HDL**，靠 `interface` 解耦。
- **通信核心随主机连接方式而变**：USB3→`pcileech_ft601.sv`、以太网→`pcileech_eth.sv`、USB2→FT2232H 系列。
- 存在一条**新旧分界线**：较新设备带 `pcileech_tlps128_cfgspace_shadow.sv` + `pcileech_tlps128_bar_controller.sv`；较老设备只有旧版 `pcileech_pcie_cfgspace_shadow.sv`、无 `bar_controller`。这决定了能否做 BAR 设备仿真。
- PCIeSquirrel 与 NeTV2 的对比典型地展示了上述全部规律：共享七件公共 HDL，差异集中在通信核心（ft601 vs eth）与配置空间影子代际（新 vs 旧）。

## 7. 下一步学习建议

- **下一篇 u1-l3（构建与烧录）**：本篇只看了「目录里有什么」，下一篇会讲 `vivado_generate_project.tcl` 与 `vivado_build.tcl` 如何把这些文件组装成工程、产出比特流，以及不同设备的烧录方式（OpenOCD / CH347 等）。
- **顺带阅读**：进入 `PCIeSquirrel/`，通读它自己的 `readme.md` 与 `build.md`，对照本篇的「四类内容」分类，确认你能在真实工程里找到每一类的代表文件。
- **后续衔接**：当你读到第 2 单元（系统级通信与控制中枢）时，会逐个深入本篇提到的公共文件（`pcileech_com.sv`、`pcileech_fifo.sv`、`pcileech_mux.sv`）；第 4 单元则专门讲解较新设备独有的 `cfgspace_shadow` 与 `bar_controller`。记住本篇的「公共 vs 特定」「新 vs 旧」两条线索，它们是后续全部源码阅读的导航图。

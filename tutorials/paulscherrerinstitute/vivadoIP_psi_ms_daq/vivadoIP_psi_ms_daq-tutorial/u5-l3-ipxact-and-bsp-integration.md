# IP-XACT 描述与驱动 BSP 集成

## 1. 本讲目标

学完本讲后，读者应该能够：

- 说清 `component.xml` 是什么、它在 IP-XACT（IEEE 1685-2009）标准里的地位，以及它由谁生成（`scripts/package.tcl` + `bd/bd.tcl`）。
- 在 `component.xml` 中识别出 **aximm（主/从）、axis（16 路）、clock（18 个）、reset（2 个）、interrupt（1 个）** 五类总线接口，并指出每一类来自打包脚本的哪一条命令。
- 看懂 16 路 AXI-Stream 接口的「端口使能条件」与 `package.tcl` 中 `add_port_enablement_condition` 的一一对应。
- 讲清驱动被 Vitis/XSDK BSP（Board Support Package，板级支持包）识别的三件套：`.mdd` 声明外设名、`.tcl` 生成 `xparameters.h`、`Makefile` 把 `*.c` 编进 `libxil.a`。
- 把「打包 → 生成 IP-XACT → BSP 选中驱动 → 生成 xparameters.h → 编入 libxil.a → 应用 `#include`」串成一条端到端链路。

本讲是参考设计单元的收尾，也是整个学习路线的「验收点」：它回答一个最实际的问题——**为什么在 Vitis 工程里写一句 `#include "psi_ms_daq.h"` 就能编译链接通过、并且能用 `XPAR_...` 基地址去初始化 IP？**

## 2. 前置知识

本讲是 expert 层，默认读者已经读完前置讲义。这里只把几个最关键的概念用通俗语言再点一遍：

- **IP-Core**：在 Vivado 里可以被当作一个「黑盒元器件」拖进 Block Design（BD，块设计）反复例化的模块。它对外暴露标准的总线接口（如 AXI），而不是一堆裸信号。
- **IP-XACT（IEEE 1685-2009）**：一种 XML 格式标准，用来描述一个 IP 的「元数据」——它有哪些参数、哪些总线接口、哪些端口、依赖哪些源文件、顶层实体叫什么。Vivado 用一个 `component.xml` 文件来承载这些信息。
- **VLNV**：Vendor-Library-Name-Version（厂商-库-名字-版本），是 IP-XACT 里定位任何一个 IP 的「身份证号」。本仓库 IP 的 VLNV 是 `psi.ch : PSI : psi_ms_daq_axi : 1.2`。
- **BSP（Board Support Package）**：Vitis/XSDK 里为某块板子生成的「软件底座」，包含处理器初始化、标准库（`libxil.a`）和外设驱动。BSP 会根据硬件设计里「实际例化了哪些 IP」自动挑选对应的驱动源码编进去。
- **`xparameters.h`**：BSP 自动生成的头文件，里面是每个外设实例的基地址、高端地址、设备号等宏（形如 `XPAR_PSI_MS_DAQ_AXI_BASEADDR`）。C 应用靠它知道「IP 在地址空间的哪里」。
- **「源 vs 产物」**（承接 u1-l4）：`scripts/package.tcl` 是人写的「源」，根目录的 `component.xml`、`xgui/*.tcl` 是 `package_ip` 命令自动生成的「产物」。改 IP 行为只改源、改产物会被下次打包覆盖。本讲大量篇幅在「读产物」，但读者要始终记得它来自哪里。

> 本讲承接 **u1-l4（IP 打包流程总览）**，并把 **u5-l1（参考设计 Vivado 工程）**、**u5-l2（端到端 C 应用）** 里出现的 `XPAR_...`、`#include "psi_ms_daq.h"`、`libxil.a` 等概念补全其生成机制。

## 3. 本讲源码地图

| 文件 | 作用 | 在本讲中的角色 |
|------|------|----------------|
| `component.xml` | IP-XACT 总描述（7093 行），由 `package_ip` 生成 | **核心产物**：总线接口、地址空间、文件组、参数全集都在这里 |
| `scripts/package.tcl` | 人写的打包脚本，调用 PsiIpPackage | **源头**：它的每条命令对应 `component.xml` 里的一个区块 |
| `bd/bd.tcl` | Block Design 参数传播钩子脚本 | 与 `package.tcl` 一起决定 AXI Slave 的 `ID_WIDTH` 等接口参数 |
| `drivers/psi_ms_daq_axi/data/psi_ms_daq_axi.mdd` | 驱动描述文件（MDD） | 声明 `supported_peripherals`，让 BSP 认得这个驱动 |
| `drivers/psi_ms_daq_axi/data/psi_ms_daq_axi.tcl` | 驱动生成脚本（Tcl） | 在 BSP 生成时把 `C_BASEADDR/C_HIGHADDR` 写进 `xparameters.h` |
| `drivers/psi_ms_daq_axi/src/Makefile` | 驱动编译 Makefile | 把 `*.c` 编译并归档进 `libxil.a`，把 `*.h` 拷到 include |

四个必记路径（承接 u1-l2）：RTL 唯一文件 `hdl/psi_ms_daq_vivado.vhd`、打包脚本 `scripts/package.tcl`、驱动头 `drivers/psi_ms_daq_axi/src/psi_ms_daq.h`、参考应用入口 `refdesign/ZCU102/Sdk/app/src/main.c`。本讲聚焦后三者之外的两个「产物/集成」文件：`component.xml` 与 `drivers/.../data/` 下的 `.mdd`、`.tcl`。

## 4. 核心概念与源码讲解

### 4.1 component.xml 全景：IP-XACT 产物与参数概览

#### 4.1.1 概念说明

`component.xml` 是一个 IP-Core 的「说明书 + 装箱单」。当 Vivado 把这个 IP 加进 IP 仓库后，它读这个文件来知道：

- 这个 IP 叫什么（VLNV）、由谁出品；
- 它能被怎样配置（有哪些用户参数 `Streams_g`、`AxiDataWidth_g` 等）；
- 它对外提供哪些**总线接口**（AXI Slave/Master、AXI-Stream、时钟、复位、中断）；
- 它的地址空间多大（Master 能寻址多大内存、Slave 暴露多大寄存器窗口）；
- 它的顶层实体叫什么（`psi_ms_daq_vivado`）、由哪些源文件组成；
- 它附带哪些软件资产（驱动、数据手册、Logo）。

这份文件**不是人手写的**，而是 u1-l4 讲过的 `scripts/package.tcl` 跑到最后一行 `package_ip` 时由 Vivado 自动产出的。因此本节的写法是「先看产物长什么样，再回头对照源（`package.tcl`）里的哪条命令产出了它」。

#### 4.1.2 核心流程

`component.xml` 的顶层结构是一棵固定的 XML 树，按出现顺序大致是：

```text
<spirit:component>                      ← 一个 IP 的根
  ├─ vendor / library / name / version  ← VLNV 身份证
  ├─ <busInterfaces>                    ← 总线接口（39 个）
  ├─ <addressSpaces>                    ← Master 能寻址的空间
  ├─ <memoryMaps>                       ← Slave 暴露的寄存器窗口
  ├─ <model>                            ← 顶层实体 + 源文件视图（views）
  │    └─ <views> → 引用 <fileSets>
  ├─ <fileSets>                         ← 装箱单：6 组文件
  ├─ <description>
  └─ <parameters>                       ← 用户可调参数全集（≈123 个泛型）
</spirit:component>
```

它和 `package.tcl` 的对应关系：

| `package.tcl` 里的命令 | `component.xml` 里产出的区块 |
|------------------------|------------------------------|
| `init $IP_NAME ...` / `set_description` | `<vendor/library/name/version>` + `<description>` |
| `gui_create_parameter` / `gui_add_parameter` | `<parameters>` 里的每个 `<spirit:parameter>` |
| `add_sources_relative` / `add_lib_relative` | `<model><views>` 引用的 `xilinx_anylanguagesynthesis_view_fileset` |
| 端口命名约定（`S_Axi_*`/`M_Axi_*`/`StrNN_*`） + `add_port_enablement_condition` | `<busInterfaces>` 与 `<ports>`（含使能条件） |
| `add_drivers_relative` | `xilinx_softwaredriver_view_fileset` 文件组 |
| `set_logo_relative` / `set_datasheet_relative` | `xilinx_utilityxitfiles_view_fileset` / `xilinx_datasheet_view_fileset` |
| `package_ip` | 把以上全部落盘成 `component.xml` + `xgui/*.tcl` |

#### 4.1.3 源码精读

文件头给出 VLNV（`psi.ch : PSI : psi_ms_daq_axi : 1.2`），这正是 BSP 在 IP 仓库里查找该 IP 的钥匙：

[component.xml:L3-L6](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L3-L6) — vendor/library/name/version 四行即 VLNV，与 `package.tcl` 里 `IP_NAME psi_ms_daq_axi` / `IP_VERSION 1.2` / `IP_LIBRARY PSI` 完全一致。

[component.xml:L6421-L6427](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L6421-L6427) — `<description>` 与第一个用户参数 `Streams_g`（默认值 `3`，正是 ZCU102 参考设计里启用的流数）。`spirit:resolve="user"` 表示这个值可由用户在 BD 里改，`minimum/maximum` 对应 `package.tcl` 里 `gui_parameter_set_range 1 16`。

参数全集的组织方式是「按 GUI 页顺序平铺」：先 7 个 General、再 4 个 AXI Master、再 16 路流 × 7 个参数。以流 0 的 7 个参数为例：

[component.xml:L6478-L6487](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L6478-L6487) — `Stream0Width_g`（下拉框，默认 32）与 `Stream0Prio_g`（默认 2），`choiceRef` 指向下拉选项列表，对应 `package.tcl` 的 `gui_parameter_set_widget_dropdown`。

> **关于参数数量**：整个 `component.xml` 含 **154 个 `<spirit:parameter>` 元素**，但其中只有顶层 `<spirit:parameters>` 块里的约 **123 个**是「用户可调泛型」（7 General + 4 AXI + 16×7 Stream），其余 31 个分散在各总线接口与端口上（如时钟的 `ASSOCIATED_BUSIF`、复位的 `POLARITY`、中断的 `SENSITIVITY`）。看到「154」时不要误以为有 154 个 GUI 旋钮。

地址空间与寄存器窗口的定义（这是 BSP 生成 `C_BASEADDR/C_HIGHADDR` 的依据）：

[component.xml:L1779-L1800](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L1779-L1800) — `<addressSpaces>` 声明 Master `M_Axi` 可寻址 `0x1_0000_0000`（4 GB，即 DDR 上限）、数据宽度 64；`<memoryMaps>` 声明 Slave `S_Axi` 暴露一个 `reg0` 地址块、基址 `0x0`、范围 `0x1_0000`（64 KB）、宽度 32。这个 64 KB 窗口正好覆盖 u3-l1 讲过的全部寄存器区（通用 0x000 / 逐流录制 0x200 / 逐流上下文 0x1000 / 窗口 0x4000）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立「`package.tcl` 命令 ↔ `component.xml` 区块」的对照表。
2. **操作步骤**：打开 `scripts/package.tcl` 第 88~164 行（GUI 参数定义），对照 `component.xml` 第 6422 行起的 `<parameters>` 块，逐个核对：每个 `gui_create_parameter "Xxx_g"` 是否都对应一个 `<spirit:name>Xxx_g</spirit:name>`，且 `gui_parameter_set_range` / `gui_parameter_set_widget_dropdown` 的取值是否与 XML 里的 `minimum/maximum` 或 `choiceRef` 一致。
3. **需要观察的现象**：你会发现二者严格一一对应、连顺序都一致——这印证了「`component.xml` 是 `package.tcl` 的产物」。
4. **预期结果**：例如 `package.tcl` 里 `Streams_g` 的 `set_range 1 16`，在 `component.xml` 第 6426 行就是 `minimum="1" maximum="16"`；`IntDataWidth_g` 的 `dropdown {64 128 256}` 对应 XML 里的 `choiceRef`。
5. 本步骤为纯阅读，无需运行 Vivado，**待本地验证**（若你有 Vivado，可重新跑 `package.tcl` 后 `git diff component.xml` 应无变化）。

#### 4.1.5 小练习与答案

**练习 1**：`component.xml` 里 `Streams_g` 的默认值是 3，这个 3 从哪来？改它会怎样？
**答**：它是上一次 `package_ip` 时 GUI 里填的值被固化进了 XML。改它只影响「下次拖进 BD 时的初值」，不影响 RTL 行为；真正约束流数的是综合后 `Streams_g` 的实际取值。

**练习 2**：为什么说改 `component.xml` 是「徒劳」的？
**答**：因为它是 `package_ip` 的产物，下次重跑 `scripts/package.tcl` 会被覆盖；正确做法是改 `package.tcl`（源）后重新打包。

---

### 4.2 五类总线接口：来源与组织（aximm/axis/clock/reset/interrupt）

#### 4.2.1 概念说明

一个 IP 要能在 BD 里和别的模块「连线」，它的端口必须被**分组打包成总线接口（busInterface）**。Vivado 内置了一批标准总线协议（`axis`、`aximm`、`clock`、`reset`、`interrupt` 等），每个总线接口声明：

- 自己用的是哪种协议（`busType`）和哪个抽象（`abstractionType`）；
- 是主（master）还是从（slave）；
- 逻辑信号（如 `TDATA`、`AWADDR`、`CLK`）到物理端口（如 `Str00_TData`、`S_Axi_AwAddr`）的映射（`portMaps`）；
- 一些接口级参数（时钟的 `ASSOCIATED_BUSIF`、复位的 `POLARITY`、中断的 `SENSITIVITY`）。

本 IP 共有 **39 个总线接口**，正好分成五类：16 个 `axis`（流输入）+ 2 个 `aximm`（Slave 配置 + Master 写内存）+ 18 个 `clock`（2 个 AXI 时钟 + 16 个流时钟）+ 2 个 `reset`（2 个 AXI 复位）+ 1 个 `interrupt`（IRQ）。这些接口的「名字」和「方向」几乎完全由 RTL（`hdl/psi_ms_daq_vivado.vhd`）的端口命名约定决定，Vivado 在打包时按命名规则自动识别；而 AXI Slave 的 `ID_WIDTH` 这类参数则由 `bd.tcl` 在 BD 连线时动态传播。

#### 4.2.2 核心流程

打包时，Vivado 扫描顶层实体的端口名，套用命名模板推断总线接口。简化伪代码：

```text
for 每个端口 p in entity:
    if p 名字形如 S_Axi_<信号>:           → 归入 aximm Slave "S_Axi"
    if p 名字形如 M_Axi_<信号>:           → 归入 aximm Master "M_Axi"
    if p 名字形如 StrNN_TData/TLAST/...:  → 归入 axis Slave "StrNN"
    if p 名字以 _Aclk 结尾:               → 归入 clock "XXX_Aclk"
    if p 名字以 _Aresetn 结尾:            → 归入 reset "XXX_Aresetn"
    if p == Irq:                          → 归入 interrupt "Irq"
# 端口使能条件（add_port_enablement_condition）则额外给
# StrNN_* 等接口打上 "$Streams_g > i" 的存在条件。
```

接口级参数则由 `package_ip` 按总线协议默认值填充（如复位 `POLARITY=ACTIVE_LOW`、中断 `SENSITIVITY=LEVEL_HIGH`），其中 AXI4 的 `ID_WIDTH` 由 `bd.tcl` 在连线时从上游主设备拷过来。

#### 4.2.3 源码精读

**AXI Master（写内存）** —— `M_Axi`，注意 `<spirit:master>` 与 `addressSpaceRef`：

[component.xml:L760-L766](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L760-L766) — `M_Axi` 是 aximm **主**设备，引用地址空间 `M_Axi`（即 4 GB DDR），它把采集数据直写内存。其 portMap 把逻辑信号 `AWADDR/WDATA/...` 映射到物理端口 `M_Axi_AwAddr/M_Axi_WData/...`。注意 Master 的 portMap 里**没有 `AWID`/`BID`/`ARID`**——它不产生 AXI ID（u2-l1 讲过 Master 端 ID 全部绑 `'0'`）。

**AXI Slave（寄存器配置）** —— `S_Axi`，注意它有 `AWID`：

[component.xml:L1018-L1024](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L1018-L1024) — `S_Axi` 是 aximm **从**设备，引用 memoryMap `S_Axi`（64 KB 寄存器窗口）。它的 portMap 里第一个就是 `AWID → S_Axi_AwId`：

[component.xml:L1028-L1032](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L1028-L1032) — Slave 带 ID 端口，其位宽由 `C_S_Axi_ID_WIDTH`（即 VHDL 泛型 `C_S_Axi_ID_WIDTH`）决定，而该泛型的值由 `bd.tcl` 从上游主设备传播而来（见下文 4.2 末与 u2-l3）。

**复位（reset，ACTIVE_LOW）**：

[component.xml:L1308-L1328](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L1308-L1328) — `M_Axi_Aresetn` 把物理端口 `M_Axi_Aresetn` 映射为逻辑 `RST`，并标注 `POLARITY=ACTIVE_LOW`（低有效复位，AXI 惯例）。`S_Axi_Aresetn` 同理。

**时钟（clock，带 ASSOCIATED_BUSIF）**：

[component.xml:L1352-L1376](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L1352-L1376) — `M_Axi_Aclk` 映射 `CLK → M_Axi_Aclk`，并通过 `ASSOCIATED_BUSIF=M_Axi` 告诉 Vivado「这个时钟管辖 `M_Axi` 接口」、`ASSOCIATED_RESET=M_Axi_Aresetn` 指明配对复位。这条「关联」是 Vivado 做 CDC（跨时钟域）分析与连接检查的依据。16 个流时钟 `Str00_Clk..Str15_Clk` 各自 `ASSOCIATED_BUSIF=StrNN`，让每条流可以跑在独立时钟域（承接 u5-l1）。

**中断（interrupt，LEVEL_HIGH）**：

[component.xml:L1756-L1776](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L1756-L1776) — `Irq` 是 interrupt **主**（IP 是中断的发出方），把物理端口 `Irq` 映射为逻辑 `INTERRUPT`，并标注 `SENSITIVITY=LEVEL_HIGH`（电平敏感、高有效）。这条声明正是 u5-l2 里 GIC 必须配「触发类型 0x1（电平）」、且 `HandleIrq` 必须 W1C 写 `IRQVEC` 应答的根因——硬件中断线是「高电平持续有效」，靠软件清中断源拉低。

**bd.tcl 如何传播 ID_WIDTH**（决定 `S_Axi` 的 AWID 位宽）：

[bd/bd.tcl:L4-L29](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/bd/bd.tcl#L4-L29) — `init` 钩子对名为 `S00_AXI` 的 Slave 接口，用 `bd::mark_propagate_only` 把 `C_S00_AXI_ID_WIDTH` 标记为「仅由传播决定」；随后 `pre_propagate`/`propagate` 两个钩子在 BD 连线时，把对端 AXI4 主设备的 `ID_WIDTH` 拷到本 IP 的 `C_S_Axi_ID_WIDTH`，最终落到 VHDL 泛型、决定 `S_Axi_AwId` 向量宽度（对端 ID_WIDTH=0 时空向量被综合消除，见 u2-l3）。

> 因此 `component.xml` 里五类总线接口是**两个脚本协作**的产物：接口的「存在与名字/方向」来自 `package.tcl`（连同 RTL 命名约定），接口的「运行时参数（如 ID_WIDTH）」来自 `bd.tcl` 的传播钩子。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：验证「Master 无 ID、Slave 有 ID」这一不对称设计。
2. **操作步骤**：在 `component.xml` 中分别定位 `M_Axi`（L760 起）与 `S_Axi`（L1018 起）两个 busInterface 的 portMaps，搜索二者是否包含 `AWID`/`BID`/`ARID`。
3. **需要观察的现象**：`S_Axi` 的 portMap 第一项是 `AWID → S_Axi_AwId`，而 `M_Axi` 的 portMap 里完全没有 ID 类信号。
4. **预期结果**：与 u2-l1 讲过的 RTL 一致——Master 端 `M_Axi_*Id` 绑定到 `'0'` 默认值、对外不出现在接口里；Slave 端则保留 ID 端口以兼容带 ID 的上游主设备。
5. 纯阅读验证，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `M_Axi_Aclk` 要写 `ASSOCIATED_BUSIF=M_Axi`？不写会怎样？
**答**：它告诉 Vivado「这个时钟管辖哪个总线接口」，用于时序分析（CDC 检查）和 BD 自动连线。不写的话，Vivado 无法确定 `M_Axi` 由哪个时钟驱动，会报警告甚至拒绝自动连接复位。

**练习 2**：`Irq` 接口的 `SENSITIVITY=LEVEL_HIGH` 与 u5-l2 里 GIC 配置的「触发类型 0x1」有什么关系？
**答**：二者必须匹配。IP 发出的是高电平有效信号，GIC 端就得按电平敏感配置；再加上软件必须 W1C 清 `IRQVEC` 才能拉低电平、结束中断，否则中断会反复重入。

---

### 4.3 16 路 AXI-Stream 接口与端口使能条件

#### 4.3.1 概念说明

`psi_ms_daq` 最多接 16 路 AXI-Stream 输入，但用户在 GUI 里把 `Streams_g` 设成 3 时，只有 `Str00/01/02` 三路应当出现、其余 13 路必须「消失」。IP-XACT 用**端口/接口使能条件（enablement condition）**实现这一点：给每个可选端口/接口挂一个引用了某参数的布尔表达式，当表达式为假时，该端口/接口在 BD 里不可见、也不被综合。

这层逻辑的「源」是 `package.tcl` 里的 `add_port_enablement_condition` / `add_interface_enablement_condition`；它的「产物」是 `component.xml` 里每个 busInterface 与 port 上的 `<xilinx:enablement><xilinx:isEnabled ... dependency="..."></xilinx:enablement>`。

#### 4.3.2 核心流程

`package.tcl` 用一个 16 次循环，给每一路的 6 个流信号 + 接口本身各挂一条使能条件：

```text
for i in 0..15:
    add_port_enablement_condition "Str{i}_TData"  "$Streams_g > i"
    add_port_enablement_condition "Str{i}_Ts"     "($Streams_g > i) && $Stream{i}UseTs_g && $TsPerStream_g"
    add_port_enablement_condition "Str{i}_TValid" "$Streams_g > i"
    add_port_enablement_condition "Str{i}_TReady" "$Streams_g > i"
    add_port_enablement_condition "Str{i}_TLast"  "$Streams_g > i"
    add_port_enablement_condition "Str{i}_Clk"    "$Streams_g > i"
    add_interface_enablement_condition "Str{i}"   "$Streams_g > i"
# 此外两个全局可选端口：
add_port_enablement_condition "StrX_Ts" "!$TsPerStream_g"      # 共用时间戳端口
add_port_enablement_condition "Trig"    "!$UseLastAsTrigger_g" # 外部触发端口
```

当 `Streams_g=3` 时，`$Streams_g > i` 对 i=0,1,2 为真、对 i≥3 为假，于是 `Str03..Str15` 整组接口连同其 6 个端口都被禁用。时间戳端口还要额外满足「该流启用时间戳（`Stream{i}UseTs_g`）且每流独立时间戳（`TsPerStream_g`）」；若 `TsPerStream_g=false`，则改用全局 `StrX_Ts` 端口（对应 `!$TsPerStream_g`）。触发端口 `Trig` 仅在「不用 TLast 当触发」时才出现——这与 u2-l2 讲的 `g_trig`/`g_ntrig` 二选一完全呼应。

#### 4.3.3 源码精读

**源（package.tcl）—— 使能条件的定义**：

[scripts/package.tcl:L171-L182](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L171-L182) — 16 次循环给每路流的 6 个信号与接口挂 `$Streams_g > $i` 条件，再额外给 `StrX_Ts` 挂 `!$TsPerStream_g`、给 `Trig` 挂 `!$UseLastAsTrigger_g`。

**产物（component.xml）—— 接口级使能**：

[component.xml:L47-L53](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L47-L53) — `Str00` 接口的 `vendorExtensions` 里写着 `isEnabled dependency="$Streams_g > 0"`，默认值 `true`（因为 `Streams_g` 默认 3 > 0）。

[component.xml:L188-L194](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L188-L194) — `Str03` 接口的使能条件是 `$Streams_g > 3`，默认值 `false`（3 不大于 3），所以默认情况下 `Str03..Str15` 不出现。注意 XML 里这些默认 `true/false` 是上次打包时的快照，运行时由 BD 按 `dependency` 表达式实时重算。

**产物（component.xml）—— 端口级使能（复合条件）**：

[component.xml:L3185-L3189](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L3185-L3189) — `Str00_Ts` 端口的使能条件是 `($Streams_g > 0) && $Stream0UseTs_g && $TsPerStream_g`，三条全为真时这个时间戳端口才出现（XML 中转义为 `&amp;&amp;`）。这是三参数联动的典型例子。

[component.xml:L3083-L3085](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L3083-L3085) — `Trig` 端口使能条件 `!$UseLastAsTrigger_g`，默认 `true`（因为默认不用 TLast 当触发），与 `package.tcl` 第 182 行一一对应。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：亲手验证「`Streams_g` 如何裁剪 16 路接口」。
2. **操作步骤**：在 `component.xml` 里搜索 `BUSIF_ENABLEMENT.Str`，把 16 个 `Str00..Str15` 接口的 `dependency` 与默认 `isEnabled` 列成表；再对照 `package.tcl` 第 171~180 行的循环。
3. **需要观察的现象**：`StrNN` 的 dependency 全部是 `$Streams_g > N`；默认 `isEnabled` 在 N=0,1,2 为 `true`、N≥3 为 `false`，边界恰好在「默认 `Streams_g=3`」处。
4. **预期结果**：得到一张 16 行表，证明接口存在性完全由 `Streams_g` 单参数线性决定。
5. 纯阅读验证，**待本地验证**（若改 `Streams_g` 重新打包，这张表的默认 true/false 边界会平移）。

#### 4.3.5 小练习与答案

**练习 1**：若用户在 GUI 设 `Streams_g=3` 且 `Stream0UseTs_g=true`，但 `TsPerStream_g=false`，`Str00_Ts` 端口会出现吗？取而代之出现的是哪个端口？
**答**：`Str00_Ts` 不会出现（条件含 `$TsPerStream_g`，为假）。取而代之的是全局 `StrX_Ts`（条件 `!$TsPerStream_g` 为真），即所有流共用一个时间戳输入端口，对应 u2-l2 的 `g_ntsstr` 分支。

**练习 2**：`Trig` 端口与 `StrNN_TLast` 端口能否同时都不出现？为什么？
**答**：不能同时都不出现。`Trig` 仅在 `UseLastAsTrigger_g=true` 时消失（此时用 TLast 当触发，`StrNN_TLast` 必须出现）；反之 `Trig` 出现。二者是互斥的触发来源（u2-l2 的 `g_trig`/`g_ntrig`）。

---

### 4.4 驱动 BSP 集成三件套：.mdd / .tcl / Makefile

#### 4.4.1 概念说明

`component.xml` 的 `xilinx_softwaredriver_view_fileset` 文件组（见 4.1.3 引用的 L6397~L6419）把五个驱动文件打进 IP 包：`data/psi_ms_daq_axi.mdd`、`data/psi_ms_daq_axi.tcl`、`src/Makefile`、`src/psi_ms_daq.c`、`src/psi_ms_daq.h`。当用户在 Vitis 里为含本 IP 的硬件导出 BSP 时，BSP 生成器会执行这「三件套」：

1. **`.mdd`（Driver Description，驱动描述文件）**：声明「本驱动服务于哪个外设名」。BSP 扫描硬件设计里的 IP 实例，当某个实例的 VLNV 名字匹配 `supported_peripherals` 时，就把这个驱动选中、编进 BSP。
2. **`.tcl`（驱动生成脚本）**：在 BSP 生成时被调用，按本 IP 实例的实际地址参数，把 `C_BASEADDR`/`C_HIGHADDR` 等宏写进 `xparameters.h`。
3. **`Makefile`**：把 `src/*.c` 编译成 `.o` 并归档进 BSP 的总库 `libxil.a`，同时把 `src/*.h` 拷到 BSP 的 include 目录，供应用 `#include`。

`psi_ms_daq.c/.h` 本身的寄存器操作逻辑在 u3 整章已讲透，本节只看「它们是怎么被 BSP 拣选、编译、链接的」。

#### 4.4.2 核心流程

```text
Vitis「生成 BSP」按钮被按下
   │
   1. 扫描硬件设计，得到所有 IP 实例的 VLNV
   │
   2. 对每个驱动候选，读它的 .mdd：
        supported_peripherals = (psi_ms_daq_axi)
      若硬件里有 VLNV name == "psi_ms_daq_axi" 的实例 → 选中本驱动
   │
   3. 对每个被选中的实例，调用驱动的 .tcl → generate(drv_handle)：
        xdefine_include_file → 往 xparameters.h 写
            #define XPAR_PSI_MS_DAQ_AXI_NUM_INSTANCES
            #define XPAR_PSI_MS_DAQ_AXI_..._DEVICE_ID
            #define XPAR_PSI_MS_DAQ_AXI_..._BASEADDR   (= C_BASEADDR)
            #define XPAR_PSI_MS_DAQ_AXI_..._HIGHADDR   (= C_HIGHADDR)
   │
   4. 跑驱动的 src/Makefile：
        libs:  编译 *.c → *.o → ar -r 进 libxil.a
        include: cp *.h → BSP include 目录
   │
   5. 应用工程里 #include "psi_ms_daq.h" + 链接 libxil.a → 成功
```

#### 4.4.3 源码精读

**.mdd —— 外设声明**：

[drivers/psi_ms_daq_axi/data/psi_ms_daq_axi.mdd:L5-L10](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/data/psi_ms_daq_axi.mdd#L5-L10) — `OPTION supported_peripherals = (psi_ms_daq_axi)` 是关键：BSP 用这个名字去匹配硬件设计里 IP 实例的 VLNV name（`component.xml` 第 5 行 `<spirit:name>psi_ms_daq_axi</spirit:name>`）。二者必须字符串完全一致，否则驱动不会被选中。`copyfiles = all` 表示把驱动全部源文件拷进 BSP 工程。

**.tcl —— xparameters.h 生成**：

[drivers/psi_ms_daq_axi/data/psi_ms_daq_axi.tcl:L3-L5](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/data/psi_ms_daq_axi.tcl#L3-L5) — `generate` 过程调用 Xilinx 内建命令 `xdefine_include_file`，参数依次是：驱动句柄、目标文件名 `xparameters.h`、外设名 `psi_ms_daq_axi`，以及要导出的四个字段 `NUM_INSTANCES / DEVICE_ID / C_BASEADDR / C_HIGHADDR`。Xilinx 工具会把它展开成一组 `#define XPAR_PSI_MS_DAQ_AXI_*` 宏。其中 `C_BASEADDR` 来自 `component.xml` 里 `<memoryMaps>` 的 `reg0` 基址加上 BD 分配的地址偏移——这就是 u5-l2 里 `Init()` 传给驱动的 `XPAR_PSI_MS_DAQ_BASEADDR` 的源头。

**Makefile —— 编入 libxil.a**：

[drivers/psi_ms_daq_axi/src/Makefile:L6-L14](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/Makefile#L6-L14) — `LIB=libxil.a` 是 BSP 总库名；`RELEASEDIR=../../../lib` 指向 BSP 的 lib 目录（每个驱动在自己的 `src/` 下，向上三级回到 BSP 根再进 `lib/`）；`LIBSOURCES=*.c` 收集所有 C 源；`OBJECTS` 用 `$(wildcard *.c)` 自动枚举出 `psi_ms_daq.c`。

[drivers/psi_ms_daq_axi/src/Makefile:L17-L24](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/Makefile#L17-L24) — `libs` 目标先用编译器编 `*.c`，再用归档器 `$(ARCHIVER) -r` 把 `.o` 追加进 `libxil.a`（`-r` = replace/insert），最后 `make clean` 删掉中间 `.o`；`include` 目标把 `*.h` 拷到 `INCLUDEDIR=../../../include`，这样应用工程的 `-I` 搜索路径就能找到 `psi_ms_daq.h`。

> 三个文件的「契约点」是字符串 `psi_ms_daq_axi`：`.mdd` 的 `supported_peripherals`、`.tcl` 的外设名参数、`component.xml` 的 VLNV name 必须三方一致，驱动才能从「打包进 IP」一路走到「链接进应用」。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：追踪 `XPAR_PSI_MS_DAQ_AXI_BASEADDR` 这个宏「从哪里来」。
2. **操作步骤**：按下面的链路阅读——
   - `component.xml` L1787-L1800：`<memoryMaps>` 给出 `S_Axi` 寄存器窗口（基址占位 `0x0`、范围 `0x1_0000`）。
   - `drivers/.../data/psi_ms_daq_axi.tcl` L4：`generate` 把 `C_BASEADDR` 列入导出字段。
   - 在 ZCU102 参考设计的 `main.c`（u5-l2）里找到 `PsiMsDaq_Init(..., XPAR_PSI_MS_DAQ_BASEADDR, ...)` 的调用点。
3. **需要观察的现象**：地址值的传递路径是「BD 给 IP 实例分配地址 → 写入硬件手工规范文件 → BSP 生成时 `.tcl` 读取并展开成 `xparameters.h` 宏 → C 应用拿宏当 `baseAddr` 传给 `PsiMsDaq_Init`」。
4. **预期结果**：能画出一条从 `component.xml` 的 `reg0` 到 `PsiMsDaq_Init` 第一个实参的完整数据流。
5. 纯阅读验证；**待本地验证**（实际宏值需在 Vitis 生成 BSP 后查看 `xparameters.h`）。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `.mdd` 里的 `supported_peripherals = (psi_ms_daq_axi)` 改成 `(foo)`，会发生什么？
**答**：BSP 扫描硬件时找不到名为 `foo` 的 IP 实例，本驱动不会被选中，`psi_ms_daq.c` 不会编进 `libxil.a`，应用链接时会出现 `undefined reference to PsiMsDaq_Init` 之类的错误。

**练习 2**：`Makefile` 里 `RELEASEDIR=../../../lib` 为什么是向上三级？
**答**：BSP 集成时驱动被摆在 `<bsp_root>/drivers/psi_ms_daq_axi/v1_0/`（约定路径），其 `src/` 在该目录下；从 `src/` 向上一级到 `v1_0/`、再上一级到 `psi_ms_daq_axi/`、再上一级到 `drivers/`（即 BSP 根附近），`lib/` 与 `include/` 就在 BSP 根下，故 `../../../lib`、`../../../include`。

---

## 5. 综合实践

**任务**：把本讲四个模块串成一条「从打包脚本到 C 应用编译链接成功」的完整链路，画出数据流图并写出每一步对应的关键文件与行号。

请按下面五个检查点，逐点写出「发生了什么 / 由哪个文件的哪段代码负责 / 产物是什么」：

1. **打包（源 → 产物）**：`scripts/package.tcl` 跑 `package_ip`（[L189](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L189)），落盘出 `component.xml` 与 `xgui/*.tcl`。请指出 `package.tcl` 里哪几条命令分别产出了 `component.xml` 的 `<busInterfaces>`、`<parameters>`、`xilinx_softwaredriver_view_fileset`。
2. **总线接口声明**：硬件主设备经 `bd.tcl`（[L4-L29](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/bd/bd.tcl#L4-L29)）把 `ID_WIDTH` 传播到 `C_S_Axi_ID_WIDTH`；`component.xml` 里 `S_Axi` 接口（[L1018-L1024](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L1018-L1024)）据此带 `AWID` 端口。请说明 `Streams_g=3` 时哪几个 `StrNN` 接口被使能条件裁掉。
3. **BSP 选中驱动**：`.mdd`（[L6](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/data/psi_ms_daq_axi.mdd#L6)）用 `supported_peripherals=(psi_ms_daq_axi)` 匹配 VLNV name（[component.xml:L5](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L5)），选中本驱动。
4. **生成 xparameters.h**：`.tcl` 的 `generate`（[L4](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/data/psi_ms_daq_axi.tcl#L4)）把 `C_BASEADDR/C_HIGHADDR` 写成 `XPAR_PSI_MS_DAQ_AXI_*` 宏。请说明 `C_BASEADDR` 的原始数值由 `component.xml` 哪个区块（`<memoryMaps>`）定义、又由 BD 在哪一步赋予实际地址。
5. **编入 libxil.a**：`Makefile`（[L17-L24](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/Makefile#L17-L24)）把 `psi_ms_daq.c` 编进 `libxil.a`、把 `psi_ms_daq.h` 拷进 include。最终在 u5-l2 的 `main.c` 里 `#include "psi_ms_daq.h"` 并调用 `PsiMsDaq_Init(XPAR_PSI_MS_DAQ_BASEADDR, ...)` 能编译链接通过。

**交付物**：一张链路图（5 个箭头串联上述 5 步）+ 一张「关键字符串 `psi_ms_daq_axi` 出现位置」的核对表（应至少出现在 VLNV name、`.mdd`、`.tcl` 三处，三者一致是链路打通的必要条件）。

> 本实践纯阅读即可完成，是整个学习路线（u1~u5）的总验收：它同时用到 u1-l4（打包流程）、u2-l3（ID_WIDTH 传播）、u3-l1（寄存器地址模型）、u5-l1（参考设计时钟域）、u5-l2（端到端 C 应用）的全部前置知识。

## 6. 本讲小结

- `component.xml` 是 IP-XACT（IEEE 1685-2009）产物，由 `scripts/package.tcl` 的 `package_ip` 自动生成；它是「装箱单 + 说明书」，承载 VLNV、39 个总线接口、地址空间、文件组与约 123 个用户参数（全文共 154 个 `<spirit:parameter>` 元素，多出的是接口/端口级参数）。
- 五类总线接口（16 axis + 2 aximm + 18 clock + 2 reset + 1 interrupt = 39）由「`package.tcl` + RTL 命名约定 + `bd.tcl` 传播钩子」共同生成：存在性与方向来自打包，运行时参数（如 `ID_WIDTH`、`POLARITY=ACTIVE_LOW`、`SENSITIVITY=LEVEL_HIGH`）来自协议默认值与 BD 传播。
- 16 路 AXI-Stream 接口的存在性由端口/接口使能条件 `$Streams_g > i` 线性裁剪；时间戳与触发端口还叠加 `UseTs_g`/`TsPerStream_g`/`UseLastAsTrigger_g` 的复合条件，与 u2-l2 的 `generate` 二选一完全呼应。
- 驱动被 BSP 识别靠「三件套」：`.mdd` 用 `supported_peripherals=(psi_ms_daq_axi)` 匹配 VLNV name 选中驱动；`.tcl` 的 `generate` 把 `C_BASEADDR/C_HIGHADDR` 写进 `xparameters.h`；`Makefile` 把 `*.c` 归档进 `libxil.a`、把 `*.h` 拷进 include。
- 三个文件的契约点是字符串 `psi_ms_daq_axi`：VLNV name、`.mdd` 的 supported_peripherals、`.tcl` 的外设名参数必须三方一致，否则链路在「选中驱动」或「生成宏」环节断裂。
- 端到端链路：`package.tcl` 打包 → `component.xml`（含 `memoryMaps`/`softwaredriver fileset`）→ BSP 据 `.mdd` 选中 → `.tcl` 生成 `xparameters.h`（`XPAR_PSI_MS_DAQ_AXI_BASEADDR`）→ `Makefile` 编入 `libxil.a` → 应用 `#include "psi_ms_daq.h"` 链接成功。

## 7. 下一步学习建议

- **横向对照其它 PSI IP**：本仓库的 `package.tcl`/`bd.tcl`/`component.xml`/三件套驱动是 PSI FPGA Library 的通用模板。可去上游 `psi_common`、`psi_multi_stream_daq` 等仓库看同类结构，巩固「源 vs 产物」与 BSP 集成范式。
- **回到 u5-l2 的 `main.c`**：带着本讲的认知重读参考应用，重点看 `PsiMsDaq_Init(XPAR_PSI_MS_DAQ_BASEADDR, ...)` 这一行的宏来自哪里、链接为什么能成功——你会发现自己已经能完整解释「从一行 `#include` 到寄存器被写」的全过程。
- **动手打包一次（进阶）**：若本机有 Vivado，按 u1-l3 先拉齐依赖，再跑 `scripts/package.tcl`，用 `git diff` 观察 `component.xml` 与 `xgui/*.tcl` 的变化；尝试改一个 `gui_parameter_set_range` 后重新打包，验证「改源才有效、改产物被覆盖」。
- **深入 IP-XACT 标准**：若想理解 `addressSpaceRef`/`memoryMapRef`/`portMap` 的完整语义，可阅读 IEEE 1685-2009 规范，或 Xilinx UG1118（Creating and Packaging Custom IP）对 `component.xml` 各字段的官方说明。

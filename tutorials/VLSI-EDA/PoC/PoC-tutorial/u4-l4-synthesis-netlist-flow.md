# 上下文外综合与 netlist 流程

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「把一个 PoC 核综合成网表（netlist）」整件事在概念上发生了什么，以及它和「把整个 FPGA 设计综合成比特流」有什么区别。
- 跟踪一条真实命令（如 `poc.ps1 xst PoC.arith.prng --board=KC705`）从入口脚本一路到产出 `.ngc` 文件的全过程，并能指出过程中被消费的每一类配置文件。
- 读懂 `xst/Series-7.xst` 这类「按器件系列」选择的 XST 选项模板，理解其中的 `{占位符}` 是如何被 pyIPCMI 替换的。
- 区分三类模板文件——XST 选项模板（`.xst`）、Core Generator 工程模板（`template.cgc`）、源码清单（`.files`）——并理解它们在两种综合路径（XST 直接综合 / CoreGen 生成后再综合）中的不同角色。

## 2. 前置知识

本讲建立在你已经掌握以下两讲内容的基础上，这里只做最小回顾：

- **u1-l3 获取、运行与配置 PoC**：`poc.sh` / `poc.ps1` 是极薄的包装脚本，把控制权委托给 Python 基础设施 pyIPCMI（位于 `lib/pyIPCMI`）。目标硬件由 `my_config.vhdl` 里的 `MY_BOARD` / `MY_DEVICE` 描述。
- **u3-l2 厂商选择与可移植机制**：`config.vhdl` 把器件字符串前缀解析成 `T_DEVICE_INFO` 记录（含 `Vendor`、`Device`、`DevFamily` 等字段）；pyIPCMI 在 `.files` 清单里按 `DeviceVendor` / `DeviceSeries` 做**编译时**文件选择。

在进入源码前，先澄清三个名词：

1. **网表（netlist）**：综合工具把寄存器传输级（RTL）的 VHDL 翻译成「底层原语 + 它们之间连线」的中间表示。Xilinx ISE 时代的网表文件后缀是 `.ngc`，Vivado 时代是 `.dcp`（设计检查点）。网表已经和具体的器件绑定，但还没做布局布线。
2. **上下文外综合（out-of-context synthesis）**：把一个 IP 核**以它自己为顶层**单独综合，周围不带上整个 FPGA 设计的上下文，产出一个可被更大设计直接例化的「预制网表」。PoC 文档把它叫做「生成网表（generating netlists）」，本质就是这件事。
3. **器件系列（device series / device family）**：Xilinx 把器件分成 Series-7（Artix/Kintex/Virtex-7）、Spartan-6、Spartan-3 等家族。同一个家族共享同一套综合选项与底层原语，所以 PoC 为每个家族准备了一份 `.xst` 模板。

> 一个关键直觉：PoC 的可移植性有三层。u3-l2 讲的是**展开期 `generate` 选择厂商子实体**（语法兼容的差异）；u4-l3 讲的是**编译前 `.files` 按 VHDL 版本选文件**（语法不兼容的差异）；本讲讲的是**综合期按器件系列选网表选项模板**（同一个核、不同器件家族产出不同网表）。三层都由同一份 `MY_DEVICE` 驱动。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [netlist/README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/README.md) | 网表生成流程的官方说明：支持的工具、命令行用法、两种生成路径。 |
| [netlist/template.cgc](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/template.cgc) | Xilinx Core Generator 工程模板（SPIRIT/XML 格式），内含器件占位符。 |
| [xst/README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/xst/README.md) | XST 目录说明（注：当前仅一句「No documentation available」，本讲用真实文件补全）。 |
| [xst/Series-7.xst](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/xst/Series-7.xst) | Series-7 器件家族的 XST 选项模板，`{占位符}` 形式。 |
| [src/common/common.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files) | 全库共享的源码编译清单，被每个核自己的 `.files` 通过 `include` 串进来。 |
| [.pyIPCMI/config.defaults.ini](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.defaults.ini) | pyIPCMI 的全局默认配置：目录命名、XST 选项默认值、模板文件定位规则。 |
| [.pyIPCMI/config.entity.ini](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.entity.ini) | 每个 PoC 核的「实体级」配置：声明它有哪些测试台、哪些网表任务、用什么 generic。 |
| [src/arith/arith_prng.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_prng.files) | 一个具体核 `arith_prng` 的源码清单，是跟踪综合流程的最佳样本。 |

## 4. 核心概念与源码讲解

### 4.1 网表综合流程：从一条命令到一份 .ngc

#### 4.1.1 概念说明

为什么需要把核单独综合成网表？设想你在做一个大型 FPGA 工程，其中用到了 PoC 的 `arith_prng`（伪随机数发生器）。你有两条路：

- **路径 A——源码集成**：把 `arith_prng.vhdl` 及其依赖直接拉进你的工程一起综合。好处是可读、可改；代价是每次综合全工程都要重新编译它，而且综合器每次可能给出略有不同的结果。
- **路径 B——网表集成**：事先把 `arith_prng` 以自己为顶层单独综合成 `arith_prng.ngc`，工程里只例化这份网表。好处是综合结果稳定、编译更快、可以分发二进制 IP；代价是网表已绑定器件、不可读。

PoC 同时支持这两种方式，本讲聚焦路径 B 的**生产侧**——即 PoC 自己如何把核「预制」成网表，放到 `netlist/` 目录供下游使用。这也就是「上下文外综合」：核脱离了任何外部工程的上下文，被独立综合。

PoC 把这件事抽象成两类生成任务（见 [netlist/README.md:46-96](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/README.md#L46-L96)）：

1. **CoreGen 路径**：把厂商预配置 IP（`.xco` 文件，如 ChipScope、MIG 内存控制器）编译成网表。
2. **XST 路径**：把一组 PoC 自己的 VHDL 源码（bundle）直接综合成网表。

两条路径都通过同一个入口 `poc.[sh|ps1]` 触发，由 pyIPCMI 统一调度。

#### 4.1.2 核心流程

以文档里的标准例子（[docs/UsingPoC/Synthesis.rst](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/docs/UsingPoC/Synthesis.rst)）为准，一条 XST 综合命令的完整流程如下：

```
poc.ps1 xst PoC.arith.prng --board=KC705
   │
   ├─(1) poc.ps1 包装脚本：解析自身路径，委托给 py/PoC.py（pyIPCMI 服务工具）
   │
   ├─(2) pyIPCMI 解析参数：
   │      综合器 = xst      模块 = PoC.arith.prng      板 = KC705
   │
   ├─(3) 板名 → 器件：KC705 → XC7K325T-2FFG900 → DeviceSeries = "Series-7"
   │
   ├─(4) 查实体配置 .pyIPCMI/config.entity.ini：
   │      [IP.arith.prng] 声明了 nl2 = XSTNetlist  → 触发一次 XST 网表任务
   │
   ├─(5) 收集源码：读取 src/arith/arith_prng.files
   │      └─ include src/common/common.files  （公共包 + my_config）
   │         + src/arith/arith.pkg.vhdl       （命名空间包）
   │         + src/arith/arith_prng.vhdl      （顶层核）
   │
   ├─(6) 选模板：XSTOptionsFile = xst/Series-7.xst（由 DeviceSeries 决定）
   │      用 XSTOption.* 默认值 + 器件/顶层信息替换 {占位符}
   │
   ├─(7) 调用 XST：-top arith_prng -p XC7K325T-2-FFG900 ... → 产出 arith_prng.ngc
   │
   └─(8) PostCopyRules：把 .ngc 拷到 netlist/XC7K325T-2FFG900/arith/
```

注意第 5 步：综合用的源码清单和仿真用的清单是**同一套 `.files` 机制**（u2-l1、u3-l1 已讲），区别只在于 pyIPCMI 对 `Environment`/`ToolChain` 条件求值不同——综合时不会引入 `src/sim/` 仿真包。

#### 4.1.3 源码精读

**支持的工具与命令行骨架**——[netlist/README.md:8-19](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/README.md#L8-L19) 列出 PoC 支持的网表生成工具：Altera Quartus、Lattice Diamond LSE、Xilinx Core Generator（coregen）、Xilinx XST。README 还预告了 Vivado 等 Planned 支持。

**网表总是绑定具体器件**——这是理解整个流程的关键前提。[netlist/README.md:28-34](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/README.md#L28-L34) 说明：网表必须为某个具体平台（FPGA 的精确器件名）编译，由 `--device=<DEVICE>` 或 `--board=<BOARD>` 指定；`--board` 是糖，因为 PoC 记得每块已知开发板上焊的 FPGA 型号。

**XST 路径的命令**——[netlist/README.md:87-96](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/README.md#L87-L96) 给出把 PoC 核 bundle 综合成网表的命令，并点明关键事实：**「IP 核的 filelist（`.files`）和 XST 选项文件（`.xst`）都存放在 `<PoCRoot>\xst` 目录」**。这是 bundled-netlist 路径的约定（下文 4.3 会看到，默认情况下 `.files` 其实取自 `src/`，但 bundled 路径会把专用的 `.files` 放进 `xst/`）。

**实体级配置：声明一个核要产出哪些网表**——这是 pyIPCMI 真正消费的「任务清单」。看 [.pyIPCMI/config.entity.ini:95-107](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.entity.ini#L95-L107)：

```ini
[IP.arith.prng]
Description =       Pseudo Random Number Generator (PRNG)
tb =                VHDLTestbench
nl1 =               QuartusNetlist
nl2 =               XSTNetlist
nl3 =               LSENetlist
nl4 =               VivadoNetlist
[TB.arith.prng.tb]
...
[XST.arith.prng.nl2]
```

`nl2 = XSTNetlist` 这一行就是「这个核要被 XST 综合成网表」的声明，对应的 `[XST.arith.prng.nl2]` 段（这里为空，表示全用默认）承载这一任务的细节。四家厂商各一个 `nl*` 任务，体现「同一份源码、多厂商产出网表」。

**一份完整源码清单长什么样**——[src/arith/arith_prng.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_prng.files) 是流程第 5 步的实物，只有三行有效内容（[L8-L12](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_prng.files#L8-L12)）：

```
include      "src/common/common.files"     # load common packages
vhdl  PoC    "src/arith/arith.pkg.vhdl"    # PoC.arith package
vhdl  PoC    "src/arith/arith_prng.vhdl"   # Top-Level
```

它先把公共包整批拉进来（`common.files` 内部又 `include` 了 `my_config.files`，并按 `VHDLVersion` 选 `fileio.v93/v08`），再编译命名空间包 `arith.pkg.vhdl`（命名空间包模式，见 u3-l1），最后才是顶层核本身。这份清单就是 XST 的输入工程文件（`-ifn {prjFile}`，见 4.2.3）。

**公共包清单如何被串进来**——[src/common/common.files:8-17](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files#L8-L17) 里，第 8 行 `include "tb/common/my_config.files"` 先按板名挑出对应的 `my_config_<board>.vhdl`（综合 KC705 时就是 `my_config_KC705.vhdl`），随后第 11–17 行依次编译 utils→config→math→strings→vectors→physical→components 七个公共包。再往下 [common.files:19-28](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files#L19-L28) 用 `ToolChain`/`VHDLVersion` 条件决定要不要编译 `fileio`——综合时若用 XST（非 Altera/Lattice 工具链），这条分支会按 VHDL 版本编译 `fileio.v93` 或 `protected.v08 + fileio.v08`。

#### 4.1.4 代码实践

**实践目标**：不实际跑工具链，仅靠源码阅读，复现「`poc.ps1 xst PoC.arith.prng --board=KC705`」会被编译的全部源文件清单。

**操作步骤**：

1. 打开 [src/arith/arith_prng.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_prng.files)，记下它直接列出的文件与 `include`。
2. 顺着 `include "src/common/common.files"` 打开 [src/common/common.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files)，记下它列出的 7 个公共包。
3. 再顺着第 8 行 `include "tb/common/my_config.files"` 打开 [tb/common/my_config.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files)，找到 `BoardName = "KC705"` 分支（[L79-L80](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files#L79-L80)），确认它会编译哪份 `my_config_*.vhdl`。

**需要观察的现象**：三个 `.files` 嵌套 `include` 形成一棵依赖树；`common.files` 里的 `if (ToolChain not in [...])` 与 `if (VHDLVersion < 2002)` 条件决定了 `fileio` 是否、以哪个版本进入清单。

**预期结果**：最终编译进 XST 工程的源文件至少包含 `my_config_KC705.vhdl`、`my_project.vhdl`、7 个公共包、`fileio.v08.vhdl`+`protected.v08.vhdl`（假设 VHDL-2008）、`arith.pkg.vhdl`、`arith_prng.vhdl`。

> 本地若无 ISE/XST，可用 pyIPCMI 的 dry-run 选项（`--dryrun`，见 Synthesis.rst 选项表）让它把「将要执行的命令和文件清单」打印出来而不真正综合——这是观察流程最省事的方式。具体能否运行取决于本地是否已配置 pyIPCMI 与工具链，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么网表必须绑定具体器件，而不能产出一份「通用网表」？

> **答案**：网表里的原语（如 Xilinx 的 `RAMB36E1`、Altera 的 `altsyncram`）是器件底层硬核，不同器件家族的硬核名称、容量、端口都不同；且网表里的布线资源、时序也都按具体器件刻画。脱离器件就没有意义。

**练习 2**：`--board=KC705` 和 `--device=XC7K325T-2FFG900` 两种指定方式有何关系？

> **答案**：等价的两种入口。`--board` 通过 PoC 内置的板→器件映射表（u2-l3 的 `C_BOARD_INFO_LIST`）查出焊在该板上的 FPGA 完整器件名；`--device` 直接给出器件名。两者最终都解析成同一份 `T_DEVICE_INFO`，后续流程一致。

---

### 4.2 XST 配置：按器件系列选择选项文件

#### 4.2.1 概念说明

XST（Xilinx Synthesis Tool）是一个命令行综合器，它读一份**选项文件**（`-ifn` 指定）来决定综合行为：优化目标是速度还是面积、要不要把 RAM 推断成 BRAM、要不要用 DSP48、寄存器要不要复制等等，选项多达五十多个。

问题在于：不同器件家族支持的原语和选项略有不同。例如 Series-7 有 `DSP48E1`，所以选项里有 `-use_dsp48`；Spartan-3 没有，选项集就不同。PoC 的做法是**为每个家族维护一份 XST 选项模板**，放到 `xst/` 目录，文件名即家族名：

```
xst/Series-7.xst     ← Artix-7 / Kintex-7 / Virtex-7 / Zynq-7000
xst/Spartan-6.xst    ← Spartan-6
xst/Spartan-3.xst    ← Spartan-3 / Spartan-3E
```

（这三份文件在本仓库都真实存在，可用 `ls xst/*.xst` 核对。）

注意：[xst/README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/xst/README.md) 当前只有一句 `*No documentation available.*`，所以这部分知识要从真实的 `.xst` 文件和 pyIPCMI 配置里反推——这正是本讲要做的。

#### 4.2.2 核心流程

「按器件系列选模板」这件事不是靠人去记，而是 pyIPCMI 自动完成的。规则定义在 [.pyIPCMI/config.defaults.ini](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.defaults.ini) 的 `[XST.DEFAULT]` 段：

```
器件名 XC7K325T-2FFG900
   │
   ├─ pyIPCMI 解析出 DeviceSeries = "Series-7"
   │
   ├─ XSTOptionsFile = ${PoC:XSTDir}/${SPECIAL:DeviceSeries}.xst
   │                   = xst/Series-7.xst        ← 自动选中
   │
   ├─ 读取 [XST.DEFAULT] 里的 XSTOption.* 默认值
   │      例：UseDSP48 = Auto, OptimizationMode = Speed, RAMExtract = YES ...
   │
   ├─ 用这些值替换 Series-7.xst 里的 {占位符}
   │      -use_dsp48 {UseDSP48}   →   -use_dsp48 Auto
   │      -p {Part}               →   -p XC7K325T-2-FFG900
   │      -top {TopModuleName}    →   -top arith_prng
   │
   └─ 把替换后的选项文件交给 XST 执行
```

整套机制是「**模板 + 占位符替换**」：模板提供结构与器件家族相关的选项骨架，pyIPCMI 用运行期解析出的器件、顶层名、以及一整套 `XSTOption.*` 默认值去填充。换一个器件家族，只是换一份模板；换一个核，只是换 `{TopModuleName}` 等少数值。

#### 4.2.3 源码精读

**一份 XST 选项模板的真面目**——看 [xst/Series-7.xst:1-9](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/xst/Series-7.xst#L1-L9)：

```
set -xsthdpdir "xst"
run
-ifn {prjFile}
-use_new_parser {UseNewParser}
-ifmt {InputFormat}
-ofn {OutputName}
-ofmt {OutputFormat}
-p {Part}
-top {TopModuleName}
```

每一行就是 XST 的一个命令行选项，花括号里的 `{...}` 全是占位符：`{Part}` 是器件完整型号、`{TopModuleName}` 是顶层实体名、`{prjFile}` 是源码工程文件（即 4.1 里的 `.files` 转换结果）、其余 `{UseNewParser}`/`{InputFormat}`/`{OutputFormat}` 等都对应 `[XST.DEFAULT]` 里的 `XSTOption.*` 默认。

往下翻，[Series-7.xst:43](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/xst/Series-7.xst#L43) 这一行是 Series-7 特有的能力：

```
-use_dsp48 {UseDSP48}
```

它告诉 XST 自动把乘加运算推断到 DSP48 硬核上。Spartan-3 的 [xst/Spartan-3.xst](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/xst/Spartan-3.xst) 没有这一行，体现了「不同家族不同选项集」。可以对比两份文件的差异（Spartan-3 多了 `-verilog2001`、`-mux_style` 等，少了 `-use_dsp48`、`-dsp_utilization_ratio`、`-power`）。

[XST 目录还有一个 README 提到的搜索目录选项](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/xst/Series-7.xst#L20) `-sd {SearchDirectories}`——它对应 [netlist/README.md:69-70](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/README.md#L69-L70) 提到的「Xilinx ISE 流程需要扩展 IP 核搜索目录（`-sd` 选项）」，也就是让大工程能找到 `netlist/` 里那些预制网表。

**模板定位与默认值都来自 pyIPCMI 配置**——[config.defaults.ini:279-280](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.defaults.ini#L279-L280) 是本讲最关键的两行：

```ini
XSTOptionsFile =          ${PoC:XSTDir}/${SPECIAL:DeviceSeries}.xst
XSTFilterFile =           ${PoC:XSTDir}/default.filter
```

第一行用 `${SPECIAL:DeviceSeries}` 把「器件系列」直接拼进文件名——这就是「按器件系列选择」的引擎。`${PoC:XSTDir}` 解析到仓库根的 `xst/` 目录（其根值由 [config.defaults.ini:69](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.defaults.ini#L69) `ISESynthesisFiles = xst` 给出）。

紧接其下，[config.defaults.ini:284-338](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.defaults.ini#L284-L338) 给出了全部 `XSTOption.*` 默认值，节选几条与 `.xst` 占位符一一对应的：

```ini
XSTOption.OutputFormat =                    NGC
XSTOption.OptimizationMode =                Speed
XSTOption.OptimizationLevel =               2
XSTOption.RAMExtract =                      YES
XSTOption.UseDSP48 =                        Auto
XSTOption.IOBuf =                           NO
```

它们会分别填进 `Series-7.xst` 的 `{OutputFormat}`、`{OptimizationMode}`、`{UseDSP48}` 等位置。`OutputFormat = NGC` 正好呼应「产出 `.ngc` 网表」。

**网表产出到哪里**——[config.defaults.ini:267](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.defaults.ini#L267) 的 `PostCopyRules` 把综合产物从临时目录搬到正式位置：

```ini
PostCopyRules =           ${SPECIAL:OutputDir}/${TopLevel}.ngc -> ${PoC:NLDir}/${SPECIAL:Device}/${RelDir}/${TopLevel}.ngc
```

即 `netlist/<器件>/<相对目录>/<顶层>.ngc`，与 [netlist/README.md:67-68](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/README.md#L67-L68) 说的 `netlist/XC7K325T-2FFG900/xil/` 一致（`NLDir` 根值由 [config.defaults.ini:36](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.defaults.ini#L36) `NetlistFiles = netlist` 给出）。

#### 4.2.4 代码实践

**实践目标**：给定一个目标器件，确定 pyIPCMI 会选哪份 `.xst` 模板，以及某个具体选项的最终取值。

**操作步骤**：

1. 任选一块 Xilinx 板：例如 Spartan-6 的 Atlys、Series-7 的 KC705。
2. 查 [.pyIPCMI/config.defaults.ini:279](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.defaults.ini#L279)，按 `${SPECIAL:DeviceSeries}.xst` 规则写出会被选中的模板文件名。
3. 打开那份 `.xst`，找到 `-use_dsp48`（或 Spartan-6 的对应行），再到 [config.defaults.ini:284-338](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.defaults.ini#L284-L338) 查 `XSTOption.UseDSP48` 的默认值，写出替换后的最终 XST 选项行。

**需要观察的现象**：换板（换器件系列）后，选中的 `.xst` 文件随之改变；同一份 `XSTOption.*` 默认值填进不同模板，会因模板里有没有对应占位符而产生不同的最终选项集。

**预期结果**：KC705 → `xst/Series-7.xst`，`-use_dsp48 {UseDSP48}` → `-use_dsp48 Auto`；Atlys → `xst/Spartan-6.xst`，该模板同样含 `-use_dsp48 {UseDSP48}`（[Spartan-6.xst:43](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/xst/Spartan-6.xst#L43)），结果也是 `-use_dsp48 Auto`；而 Spartan-3 没有 DSP48，模板里根本没有此行。

> 若想强制某个核用 DSP48（而不是 Auto），可在 `.pyIPCMI/config.entity.ini` 对应的 `[XST.<ns>.<core>.nl]` 段里覆盖 `XSTOption.UseDSP48 = YES`——这是「实体级覆盖全局默认」的入口，**待本地验证**其语法在当前 pyIPCMI 版本下是否仍为该写法。

#### 4.2.5 小练习与答案

**练习 1**：如果将来要支持 Xilinx UltraScale（新家族），按现有机制需要新增哪些文件？

> **答案**：在 `xst/` 下新增一份 `UltraScale.xst`（沿用 `{占位符}` 风格、补上 UltraScale 特有选项），并让 pyIPCMI 在解析 UltraScale 器件名时把 `DeviceSeries` 置为 `UltraScale`。由于选项定位完全由 `XSTOptionsFile = ${PoC:XSTDir}/${SPECIAL:DeviceSeries}.xst` 驱动，机制本身无需改动。

**练习 2**：`XSTOption.IOBuf = NO`（[config.defaults.ini:326](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.defaults.ini#L326)）对「上下文外综合」为何重要？

> **答案**：`IOBuf = NO` 让 XST 不要为顶层端口自动插入 IOB（管脚缓冲器，如 IBUF/OBUF）。上下文外综合产出的网表是要被更大设计**例化到内部**使用的，不应带管脚缓冲；只有真正顶层的完整工程才需要 IOB。这正是 out-of-context 与全工程综合在选项上的核心区别。

---

### 4.3 模板文件：.xst、template.cgc 与占位符替换

#### 4.3.1 概念说明

4.2 已经引出 PoC 综合流程的核心设计模式：**一份模板 + 占位符替换 = 适配多种器件/核**。本节把这个模式抽出来看清楚，它至少出现在三类文件上：

| 模板文件 | 服务对象 | 占位符示例 | 由谁替换 |
| --- | --- | --- | --- |
| `xst/<Series>.xst` | XST 综合选项 | `{Part}` `{TopModuleName}` `{UseDSP48}` | pyIPCMI（XST 路径） |
| `netlist/template.cgc` | Xilinx Core Generator 工程 | `{name}` `{device}` `{devicefamily}` `{package}` `{speedgrade}` | pyIPCMI（CoreGen 路径） |
| `<entity>.files` | 源码编译清单 | 无占位符，但有 `include` 与条件分支 | pyIPCMI 在编译前展开 |

它们共同的特点是：**文件本身不是可直接执行的输入，而是带变量/条件的「配方模板」**，pyIPCMI 读它、求值条件、替换占位符，再把生成的具体文件（一份 `.xst` 命令文件、一份 `.cgc` 工程文件、一份展开的源码列表）交给底层厂商工具。这与 u4-l3 讲的「`.files` 在编译前被读取挑选」是同一种哲学——把可移植性从源码层挪到构建层。

#### 4.3.2 核心流程

占位符替换的执行顺序（以 CoreGen 路径为例，它最能体现「模板」二字）：

```
1. 用户：poc.ps1 coregen PoC.xil.ChipScopeICON_1 --board=KC705
2. pyIPCMI 解析板 → device=XC7K325T, package=FFG900, speedgrade=2, family=Series-7
3. 取 netlist/template.cgc，把：
     {name}          ← 实例/组件名（如 ChipScopeICON_1）
     {device}        ← XC7K325T
     {devicefamily}  ← kintex7（家族小写名）
     {package}       ← FFG900
     {speedgrade}    ← -2
   替换进 XML
4. 生成具体 .cgc 工程文件，调用 coregen 产出 .ngc + .xco + .ncf
5. PostCopyRules 把产物搬到 netlist/<器件>/xil/
```

关键观察：`template.cgc` 里**同一批 `{device}/{package}/{speedgrade}` 占位符出现了两次**（`componentInstance` 内一次、顶层 `vendorExtensions` 内一次，[template.cgc:49-54](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/template.cgc#L49-L54) 与 [L104-L108](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/template.cgc#L104-L108)），替换器只需做一次全局字符串替换即可同时填好两处——这正是用占位符而非硬编码的价值。

#### 4.3.3 源码精读

**CoreGen 工程模板**——[netlist/template.cgc:1-6](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/template.cgc#L1-L6) 表明它是 SPIRIT 1685-2009 schema 的 XML（`spirit:design`），vendor 是 xilinx.com，库是 `project`，名是 `coregen`：

```xml
<spirit:design ... xmlns:xilinx="http://www.xilinx.com" >
   <spirit:vendor>xilinx.com</spirit:vendor>
   <spirit:library>project</spirit:library>
   <spirit:name>coregen</spirit:name>
   <spirit:version>1.0</spirit:version>
```

它具体描述的是一个 ChipScope VIO 核（[template.cgc:10](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/template.cgc#L10) 的 displayName 是 `VIO (ChipScope Pro - Virtual Input/Output)`），但器件相关字段全部留空成占位符。[template.cgc:49-54](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/template.cgc#L49-L54) 是器件定位块：

```xml
<xilinx:part>
    <xilinx:device>{device}</xilinx:device>
    <xilinx:deviceFamily>{devicefamily}</xilinx:deviceFamily>
    <xilinx:package>{package}</xilinx:package>
    <xilinx:speedGrade>{speedgrade}</xilinx:speedGrade>
</xilinx:part>
```

实例名同样是占位符：[template.cgc:9](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/template.cgc#L9) `<spirit:instanceName>{name}</spirit:instanceName>`。产物类型写死成 NGC（[template.cgc:63](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/template.cgc#L63) `<xilinx:implementationFileType>Ngc</xilinx:implementationFileType>`），与 XST 路径一致——两条路径殊途同归，最终都产出 `.ngc`。

> 补充：README 与 `[CG.DEFAULT]` 配置显示，CoreGen 路径日常以 `.xco` 文件为输入（`CoreGeneratorFile = ${SrcDir}/${TopLevel}.xco`，见 [config.defaults.ini:187](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.defaults.ini#L187)）。`template.cgc` 则是「工程级」的 SPIRIT/XML 模板形态，演示了同一种占位符思想。README 对 bundled-netlist 段落也明确标注「Documentation is still incomplete」，所以这里只对**文件中可读到的内容**做精读，不臆测其完整调用约定。

**占位符风格对比**——把三类模板的占位符并排看：

- `xst/Series-7.xst` 用 `{CamelCase}` 风格（`{TopModuleName}`、`{UseDSP48}`、`{SearchDirectories}`），与 `XSTOption.*` 键名一一对应。
- `netlist/template.cgc` 用 `{lowercase}` 风格（`{device}`、`{devicefamily}`、`{speedgrade}`），与 SPIRIT schema 字段对应。
- `.files` 不用值占位符，而用 `include` + `if (BoardName = "...")` / `if (VHDLVersion < 2002)` 这类**条件分支**来表达「按情况选文件」。

**约束文件也是模板化的「可选配件」**——XST 流程还会带一份约束文件（`.xcf`）。[config.defaults.ini:276-278](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.defaults.ini#L276-L278) 定义了默认约束：

```ini
XSTNoConstraintsFile =    ${PoC:XSTDir}/empty.xcf
XSTConstraintsFile =      ${XSTNoConstraintsFile}
```

即默认指向 [xst/empty.xcf](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/xst/empty.xcf)——一个真实的空文件（0 字节），表示「综合时不加额外时序约束」。需要约束的核可在自己的 `[XST....nl]` 段覆盖 `XSTConstraintsFile`。同理 [xst/default.filter](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/xst/default.filter) 也是 0 字节空文件，作为 `XSTFilterFile` 的默认占位。这两个空文件是典型的「默认值需要一个实体文件兜底」的设计。

**实体级如何覆盖默认 `.files` 位置**——再看 bundled 路径里把 `.files` 放进 `xst/` 的真实例子。[config.entity.ini:1175-1180](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.entity.ini#L1175-L1180) 的 MIG 内存控制器（两步：先 coregen 后 xst）：

```ini
[XST.xil.mig.Atlys_1x128.nl]
Dependencies =        CG.xil.mig.Atlys_1x128.cg
# use .files file from "xst" directory
FilesFile =           ${XSTDir}/${IP.%{Parent}:EntityPrefix}_${IP.%{Parent}:Name}.files
RulesFile =           ${DefaultRulesFile}
XSTConstraintsFile =  ${XSTDir}/${TopLevel}.xcf
```

注释直白地写明「use .files file from "xst" directory」——这里把 `FilesFile` 从默认的 `src/...` 显式改成 `xst/...`，对应 [xst/xil/mig/mig_Atlys_1x128.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/xst/xil/mig/mig_Atlys_1x128.files) 这份清单（它列出的是 coregen 已经生成、落在 `netlist/.../xil/mig/` 下的 Verilog 网表源文件）。这就是 [netlist/README.md:91-92](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/README.md#L91-L92) 说的「`.files` 和 `.xst` 存在 `xst/` 目录」的真实含义：当核的源码本身就是厂商生成的网表（而非手写 VHDL）时，清单文件就放进 `xst/`，由实体级配置显式指向。

#### 4.3.4 代码实践

**实践目标**：把三类模板的占位符/条件机制用一张替换表显式化。

**操作步骤**：

1. 在 [xst/Series-7.xst](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/xst/Series-7.xst) 里找出全部 `{...}` 占位符，列出一张「占位符 → 来源」表（来源指：器件解析结果、实体配置、或 `XSTOption.*` 默认）。
2. 在 [netlist/template.cgc](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/netlist/template.cgc) 里找出 `{name}`、`{device}`、`{devicefamily}`、`{package}`、`{speedgrade}` 各自出现的行号与次数。
3. 对照 [tb/common/my_config.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files) 的 `if (BoardName = ...)` 分支，体会「`.files` 用条件分支代替值占位符」的不同做法。

**需要观察的现象**：`.xst` 的占位符名与 `XSTOption.*` 键名严格对齐（去掉前缀）；`.cgc` 的占位符是器件四元组（device/family/package/speedgrade），与板→器件解析输出对齐。

**预期结果**：得到两张替换表，例如 `{Part}←XC7K325T-2FFG900`、`{UseDSP48}←Auto`、`{devicefamily}←kintex7` 等。这说明「模板」把易变部分（器件、核名、优化旋钮）参数化，把稳定部分（选项结构、XML schema、编译顺序）固化为文件。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `empty.xcf` 和 `default.filter` 要作为 0 字节实体文件存在，而不是让缺省值为「空字符串」？

> **答案**：XST 工具本身要求这些选项（`-uc`、filter）指向一个**存在的文件**；给空字符串会让 XST 报「文件找不到」。因此 pyIPCMI 用真实的空文件做「什么约束都不加」的占位，既满足工具的「必须是个文件」要求，又语义清晰。这是一种典型的「用空文件代替 null」的工程兜底。

**练习 2**：MIG 的例子为什么要「先 coregen 再 xst」两步（见 [src/xil/mig/README.md:11-15](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/xil/mig/README.md#L11-L15)），而不能一步到位？

> **答案**：MIG 是 Xilinx 私有 IP，其 RTL 源码必须先用 CoreGen/MIG 生成（并打补丁），产物是一堆 Verilog 文件；这些生成出来的文件再交给 XST 综合成 `.ngc`。两步对应两种工具的职责：CoreGen 负责「生成 IP 源码」，XST 负责「把源码综合成网表」。`[XST.xil.mig.Atlys_1x128.nl]` 里的 `Dependencies = CG.xil.mig.Atlys_1x128.cg` 就声明了这种先后依赖。

---

## 5. 综合实践

把本讲三个模块串起来，完成一份**完整的「综合配方」卡片**。任选一个在 [config.entity.ini](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.entity.ini) 里声明了 `XSTNetlist` 的真实 PoC 核（推荐 `PoC.io.uart.fifo` 或 `PoC.mem.ocram.sp`，它们都有完整的四厂商网表任务和 `HDLParameters`）。

任务：为该核产出一张卡片，至少包含以下字段，且每一项都要给出**来源文件与行号**：

1. **触发命令**：写完整 `poc.ps1 xst <实体全名> --board=<某块 Xilinx 板>`。
2. **实体配置段**：在 `config.entity.ini` 里找到的 `[IP....]` 与 `[XST.....nl]` 段，列出它声明的 `HDLParameters`（这些 generic 会在综合时覆盖核的默认值）。
3. **器件系列**：由板名推出器件，再推出 `DeviceSeries`。
4. **选中的 `.xst` 模板**：按 `XSTOptionsFile` 规则写出文件名，并列出其中两个你最关心的占位符及其默认取值。
5. **源码清单来源**：是默认的 `src/<ns>/<entity>.files`，还是被覆盖到 `xst/...`？打开该 `.files`，列出它的顶层 `include` 与顶层 `vhdl` 行。
6. **约束文件**：默认走 `empty.xcf` 还是被实体配置覆盖成了具体 `.xcf`？
7. **网表落地路径**：按 `PostCopyRules` 写出最终 `.ngc` 的目录（`netlist/<器件>/<相对目录>/`）。

完成后，你应当能仅凭静态文件阅读，预测一次综合任务的全部输入与输出位置——这正是 pyIPCMI 把综合流程「数据化」的价值：综合不再是一串手敲的命令，而是一份可读、可 diff、可复现的配置。

> 若本地具备 ISE 14.7 与已配置的 pyIPCMI，可用 `--dryrun` 实际打印出替换后的 `.xst` 与文件清单来核对你的卡片；否则本任务以源码阅读为准。**待本地验证**。

## 6. 本讲小结

- PoC 的「网表生成」本质是**上下文外综合**：把核以自身为顶层单独综合成 `.ngc`，存入 `netlist/<器件>/...`，供更大设计经 `-sd` 搜索目录例化。
- 两条生成路径——CoreGen（`.xco` 厂商 IP）与 XST（PoC 自己的 VHDL bundle）——共用入口 `poc.[sh|ps1]`，由 pyIPCMI 统一调度，终点都是 `.ngc`。
- XST 选项按**器件系列**自动选模板：`XSTOptionsFile = xst/${DeviceSeries}.xst`；`Series-7.xst` / `Spartan-6.xst` / `Spartan-3.xst` 各对应一个家族。
- 三类模板——`.xst`（XST 选项）、`template.cgc`（CoreGen 工程）、`.files`（源码清单）——都遵循「**模板 + 占位符/条件替换**」模式，由 pyIPCMI 在编译/综合前展开成具体输入。
- `.xst` 的 `{占位符}` 与 `[XST.DEFAULT]` 里的 `XSTOption.*` 默认值一一对应；实体级 `[XST.....nl]` 段可覆盖默认值（如 `FilesFile`、`XSTConstraintsFile`、`HDLParameters`）。
- `empty.xcf` / `default.filter` 这类 0 字节实体文件是「空值需要一个存在文件兜底」的工程约定，呼应 PoC 把每个构建输入都做成显式、可追溯实体的设计哲学。

## 7. 下一步学习建议

- **紧接本讲**：阅读 [ucf/README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/README.md) 与 `ucf/MetaStability.ucf`，进入下一讲 **u4-l5 板级约束与 FPGA 目标**，把「网表产出后如何被约束、如何落到具体开发板」补全。
- **向下一层**：本讲多次提到 pyIPCMI 但只从配置文件侧观察它。要理解 `.files` 是如何被解析、占位符是如何被替换的，需要进入 **u5-l1 pyIPCMI 基础设施与命令行前端**，读 `lib/pyIPCMI` 子模块的 Python 源码。
- **横向印证**：用本讲学到「实体级配置声明任务」的视角，回头读 [config.entity.ini](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.pyIPCMI/config.entity.ini) 里某个有 `QuartusNetlist`/`LSENetlist`/`VivadoNetlist` 的核（如 `arith.prng`），对比四家厂商在网表生成上的异同，巩固「同一份源码、多厂商产出」的整体图景。

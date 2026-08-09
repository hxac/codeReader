# 库结构与多厂商依赖：library.mk

## 1. 本讲目标

学完本讲后，你应该能够：

- 看懂任意一个 `library/<ip>/Makefile` 中 `GENERIC_DEPS / XILINX_DEPS / INTEL_DEPS / LATTICE_DEPS` 四个依赖桶的分工。
- 解释 `library/scripts/library.mk` 如何根据这三个厂商各定义一套构建目标，以及它们各自产出的「打包产物」是什么（`.timestamp_intel`、`component.xml`、`ltt/metadata.xml`）。
- 理解 `XILINX_LIB_DEPS`、`INTEL_LIB_DEPS` 这类「跨库依赖」是如何在 Make 依赖图里被自动展开并递归构建的。
- 区分「库侧打包」与上一讲（u3-l2）讲过的「工程侧打包」在 `component.xml` 上的衔接关系。

本讲是第 4 单元「IP 库系统」的第一篇，承接 u3-l2（工程构建 Makefile 内部）。在 u3-l2 里我们只说了「工程 Makefile 用 `LIB_DEPS` 把每个依赖的库翻译成 `component.xml`」，但库本身是怎么产出 `component.xml` 的，本讲给出完整答案。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**为什么要分厂商？** 同一段 Verilog RTL（比如 `axi_dmac`）本身与厂商无关，但要把它「打包成一个可被工程拖拽复用的 IP」，三家 FPGA 厂商的工具链各有一套打包格式与脚本机制：AMD Xilinx Vivado 用 `*_ip.tcl` 产出 `component.xml`；Intel Quartus 用 `*_hw.tcl`；Lattice Radiant/Propel 用 `*_ltt.tcl`。因此一个库模块必须声明「我针对每家厂商分别需要哪些额外文件」。这就是四个依赖桶存在的根本原因。

**库与工程的分工回顾。** 在 u3-l2 我们看到，工程 Makefile（`project-xilinx.mk`）只负责「声明我依赖哪些库」，并通过一条模式规则把依赖转成对库目录 `make xilinx` 的调用（带 `flock` 串行化）。真正「跑 `vivado` 把 RTL 打包成 IP」的工作发生在库目录内部，由本讲的 `library.mk` 完成。可以把工程看作「组装者」，把库看作「零件厂」。

**Make 依赖驱动回顾。** 整个 ADI HDL 构建链都是 GNU Make 的依赖驱动模型：声明一个产物（target）和它的前置条件（prerequisites），Make 会自动递归地把前置条件也构建出来。本讲会反复看到「把 `XILINX_LIB_DEPS` 里的库名，用 `foreach` 展开成对 `<lib>/component.xml` 的依赖」这种把「名字」翻译成「文件依赖」的手法，这正是 u3-l1、u3-l2 已建立的惯用法在库侧的复用。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `library/axi_dmac/Makefile` | 单个库模块的 Makefile「样板」，声明四个依赖桶，最后 `include` 公共脚本。 |
| `library/scripts/library.mk` | 所有库共享的公共脚本，定义 `intel`/`xilinx`/`lattice` 三套目标、跨库依赖展开与清理规则。 |
| `library/Makefile` | 库层的递归调度入口，自动发现所有库子目录并逐一构建。 |
| `library/scripts/lattice_tool_set.mk` | Lattice 专属的工具名与安装路径设置（被 `library.mk` include）。 |

此外会少量引用 u3-l2 的 `projects/scripts/project-xilinx.mk`（工程侧如何调用库）作为对照。

## 4. 核心概念与源码讲解

### 4.1 多厂商依赖分组

#### 4.1.1 概念说明

一个库模块要把同一份 RTL 打包给三家厂商，就要分别告诉 Make：「打包给 Xilinx 时需要这些文件，打包给 Intel 时需要另外一些文件」。`library.mk` 约定了四个标准变量名（依赖桶）：

- `GENERIC_DEPS`：与厂商无关的公共依赖，主要是 `.v`/`.vh` 源码与 `adi_env.tcl`。
- `XILINX_DEPS`：只有打包 Xilinx IP 才需要的文件（`*_ip.tcl`、`*.ttcl` 约束、`bd/bd.tcl`、`.xml` 接口声明等）。
- `INTEL_DEPS`：只有打包 Intel IP 才需要的文件（`*_hw.tcl`、`*.sdc` 约束等）。
- `LATTICE_DEPS`：只有打包 Lattice IP 才需要的文件（`*_ltt.tcl` 等）。

关键直觉是：**桶不是互斥的，而是叠加的**。`GENERIC_DEPS` 里放的是「三家用得着的公共源码」，而各家厂商桶里只放「这家专属的额外文件」。在 `library.mk` 里，每个厂商桶都会被自动并入 `GENERIC_DEPS`（后面 4.2 会看到具体代码）。所以你在单库 Makefile 里通常不会重复把 `.v` 源码写进 `XILINX_DEPS`。

一个佐证整体支持度的真实数据：对当前 HEAD 统计各厂商桶在所有 `library/*/Makefile` 里的出现情况——`GENERIC_DEPS` 出现 84 次、`XILINX_DEPS` 83 次、`INTEL_DEPS` 仅 23 次、`LATTICE_DEPS` 仅 6 次。这说明绝大多数库只做了 Xilinx 的完整打包，Intel 次之，Lattice 最少。这也是为什么本讲会以 Xilinx 为主线。

#### 4.1.2 核心流程

单个库 Makefile 的组织流程：

1. 设定 `LIBRARY_NAME`（本模块名，如 `axi_dmac`）。
2. 用 `+=` 不断追加文件到四个依赖桶。
3. 最后一行 `include ../scripts/library.mk`，把所有规则交给公共脚本。

用伪代码描述：

```
LIBRARY_NAME := axi_dmac
GENERIC_DEPS += <厂商无关的 .v/.vh/adi_env.tcl>
XILINX_DEPS  += <Xilinx 专属: _ip.tcl / .ttcl / bd.tcl / .xml>
XILINX_LIB_DEPS += <依赖的其他库: util_axis_fifo ...>
INTEL_DEPS   += <Intel 专属: _hw.tcl / .sdc>
LATTICE_DEPS += <Lattice 专属: _ltt.tcl>
include ../scripts/library.mk   # 交棒
```

#### 4.1.3 源码精读

先看 `axi_dmac` 如何声明 `LIBRARY_NAME` 并开始填充 `GENERIC_DEPS`：

[library/axi_dmac/Makefile:L7-L7](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L7-L7) —— 声明本库的名字，这个名字后面会被 `library.mk` 用来命名日志、IP 工程目录等。

[library/axi_dmac/Makefile:L9-L10](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L9-L10) —— `GENERIC_DEPS` 的前两项是从 `library/common` 引用的公共模块（`ad_mem_asym.v`、`up_axi.v`），说明「厂商无关」的桶里也可能包含跨目录引用的公共源码，而不只是本目录文件。

[library/axi_dmac/Makefile:L11-L39](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L11-L39) —— 一长串本模块自身的 Verilog 源文件（`axi_dmac.v`、`data_mover.v`、`src_axi_mm.v`、`dmac_sg.v` 等）都进 `GENERIC_DEPS`，因为无论哪家厂商打包，这些 RTL 都不可少。

接着是三个厂商桶的对比，这是本模块的重点。注意三者的「风格」差异：

[library/axi_dmac/Makefile:L41-L56](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L41-L56) —— Xilinx 桶：列的是打包脚本（`axi_dmac_ip.tcl`）、约束（`*.ttcl`）、块设计片段（`bd/bd.tcl`）和接口声明（`.xml`）；并把对 `util_axis_fifo`、`util_cdc` 的依赖放进 `XILINX_LIB_DEPS`（**跨库依赖**，下面 4.3 讲），而**不**直接列出它们的 `.v` 源码。换句话说，Xilinx 的打包是「引用其他已打包好的 IP」。

[library/axi_dmac/Makefile:L58-L64](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L58-L64) —— Intel 桶：风格相反，它**直接列出** `../util_axis_fifo/*.v`、`../util_cdc/*.v` 的源文件路径，再加 `axi_dmac_constr.sdc`（约束）和 `axi_dmac_hw.tcl`（打包脚本）。也就是说 Intel 的 `hw.tcl` 把依赖模块的源码「扁平地」嵌入，而不是引用一个打包产物。

[library/axi_dmac/Makefile:L66-L74](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L66-L74) —— Lattice 桶：同样扁平列出 `util_*.v`，并额外引入了 `../common/ad_mem.v`（注意 `ad_mem.v` 并没有出现在 `GENERIC_DEPS` 里，`GENERIC` 里只有 `ad_mem_asym.v`），打包脚本是 `axi_dmac_ltt.tcl`。

[library/axi_dmac/Makefile:L76-L76](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L76-L76) —— 最后一行 `include ../scripts/library.mk`，把所有规则与目标的定义权交给公共脚本。这是所有库 Makefile 的统一收尾动作。

> 这四处对比得到一个重要结论：**同一个库，三家厂商的依赖组织方式并不对称**——Xilinx 走「打包好的 IP 互相引用」（`XILINX_LIB_DEPS`），Intel/Lattice 走「源码扁平嵌入」。理解这一点能帮你解释后续 4.2 中三家目标产物为何不同。

#### 4.1.4 代码实践

**实践目标**：亲手把 `axi_dmac` 的四个桶分类填出来，建立对「依赖分组」的肌肉记忆。

**操作步骤**：

1. 打开 `library/axi_dmac/Makefile`。
2. 准备一张四列表格，表头为 `GENERIC / XILINX / INTEL / LATTICE`。
3. 逐行扫描 `L9-L74`，把每个 `+=` 后的文件归入对应桶。
4. 特别留意三类「特殊条目」：跨目录公共模块（`../common/*`）、跨库引用（`XILINX_LIB_DEPS` 里的 `util_*`）、接口声明（`.xml`）。

**需要观察的现象**：

- `GENERIC_DEPS` 里没有 `axi_dmac_ip.tcl` 这类脚本（脚本是厂商专属的）。
- `INTEL_DEPS` 和 `LATTICE_DEPS` 里都直接出现了 `util_axis_fifo.v` 的路径，而 `XILINX_DEPS` 里没有。
- `ad_mem.v` 只在 LATTICE 桶出现，`ad_mem_asym.v` 只在 GENERIC 桶出现。

**预期结果**：你会得到一张能清楚说明「公共源码 vs. 厂商专属打包资产」的对照表。结论待本地验证（不同分支的列表可能随版本变化）。

#### 4.1.5 小练习与答案

**练习 1**：如果一个库只想支持 Xilinx、完全不做 Intel 打包，它的 Makefile 里需要保留 `INTEL_DEPS` 吗？

**参考答案**：不需要。`library.mk` 用 `ifneq ($(INTEL_DEPS),)` 来决定是否生成 `intel` 目标（见 4.2.3）。只要不定义 `INTEL_DEPS`，就不会有 Intel 相关的目标，`make intel` 也会因为没有规则而跳过。这正是「桶」带来的按需启用机制。

**练习 2**：为什么 `XILINX_DEPS` 里列了 `.xml` 接口文件，而 `INTEL_DEPS` 里没有？

**参考答案**：Xilinx 的 IP 打包用 `.xml`（在 `library/interfaces/` 下，如 `fifo_rd.xml`）来声明总线接口（VLNV），供块设计自动识别连接类型；Intel 的 `hw.tcl` 用另一种方式（`add_interface` 等命令）在脚本内部声明接口，不需要独立的 `.xml` 清单。所以接口资产是厂商专属的。

---

### 4.2 library.mk 的三厂商目标

#### 4.2.1 概念说明

`library.mk` 是所有库 Makefile 通过 `include` 引入的「公共引擎」。它的核心职责是：根据用户敲下的 `make xilinx` / `make intel` / `make lattice`（或 `make all`），分别驱动三家工具链产出该厂商的「IP 打包产物」。三家产物的形态完全不同：

| 厂商 | 触发目标 | 最终产物 | 用什么工具生成 | 依赖脚本 |
| --- | --- | --- | --- | --- |
| Intel | `intel` | `.timestamp_intel`（仅时间戳） | （不直接调 Quartus） | `adi_ip_intel.tcl` |
| Xilinx | `xilinx` | `component.xml`（完整 IP 描述） | `vivado -mode batch -source` | `adi_ip_xilinx.tcl` |
| Lattice | `lattice` | `ltt/metadata.xml`（IP 元数据） | `tclsh`（纯 Tcl 解释器） | `adi_ip_lattice.tcl` |

注意 Intel 的产物最「轻」——只是一个 `.timestamp_intel` 文件（`touch` 出来的时间戳），它并不真的在库目录里跑 Quartus；真正的 Intel IP 资产是在工程构建阶段由 Quartus/Qsys 消费 `*_hw.tcl` 生成的。这是本讲一个反直觉但重要的点。

#### 4.2.2 核心流程

`library.mk` 的整体流程：

1. **自定位与公共引入**：算出 `HDL_LIBRARY_PATH`，`include` 进 `quiet.mk`（提供 `build`/`skip_if_missing`/`clean` 宏，见 u3-l1）与 `lattice_tool_set.mk`。
2. **注入公共依赖**：把 `adi_env.tcl` 加进 `GENERIC_DEPS`，让所有库都依赖环境脚本。
3. **定义聚合目标**：`all: intel xilinx lattice`，`clean` / `clean-all`。
4. **三家条件块**：分别用 `ifneq($(<VENDOR>_DEPS),)` 包住，仅当该厂商桶非空时才生成对应目标与规则。

用伪代码描述三家目标各自的产物路径：

```
make intel   → .timestamp_intel        ← 仅 touch（最轻）
make xilinx  → component.xml           ← vivado 跑 *_ip.tcl（最完整）
make lattice → ltt/metadata.xml        ← tclsh 跑 *_ltt.tcl
make all     → intel + xilinx + lattice
```

#### 4.2.3 源码精读

先看自定位与公共依赖注入：

[library/scripts/library.mk:L7-L10](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L7-L10) —— 用 `$(lastword $(MAKEFILE_LIST))` 拿到自身路径，反算出 `HDL_LIBRARY_PATH`；随后 `include` 两份公共文件：`quiet.mk`（日志与宏）和 `lattice_tool_set.mk`（Lattice 工具设置）。

[library/scripts/library.mk:L61-L61](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L61-L61) —— 把 `adi_env.tcl` 注入 `GENERIC_DEPS`，保证任何库重新打包时都会跟随环境脚本变化。`adi_env.tcl` 的作用见 u1-l3。

[library/scripts/library.mk:L63-L65](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L63-L65) —— 声明 phony 目标清单，并定义 `all` 一次性构建三家。

**Intel 块（最轻量）**：

[library/scripts/library.mk:L77-L92](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L77-L92) —— 仅当 `INTEL_DEPS` 非空时启用。`L79-L81` 把 `GENERIC_DEPS`、`EXTERNAL_DEPS` 和 `adi_ip_intel.tcl` 并入 `INTEL_DEPS`（这就是 4.1 说的「桶叠加」）。目标 `.timestamp_intel` 依赖所有 Intel 文件与跨库时间戳（`L86`），其 recipe 仅 `touch $@`（`L87`）——只盖一个时间戳，不调用任何工具。`L89-L90` 是跨库依赖的递归构建（见 4.3）。

**Xilinx 块（最完整）**：

[library/scripts/library.mk:L94-L102](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L94-L102) —— `xilinx` 目标依赖 `external_dependencies` 和 `component.xml`。注意 `L98` 同样把 `adi_ip_xilinx.tcl` 并入依赖。

[library/scripts/library.mk:L116-L125](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L116-L125) —— `component.xml` 的核心规则：先 `rm -rf` 清掉旧产物，再用 `quiet.mk` 的 `build` 宏调用 `vivado -mode batch -source axi_dmac_ip.tcl`（变量展开为 `$(LIBRARY_NAME)_ip.tcl`）真正打包。`build` 宏把冗长输出收进 `*_ip.log`、终端只留一行 OK/FAILED，这与 u3-l1/u3-l2 完全一致。这条规则产出的 `component.xml` 正是工程侧 `project-xilinx.mk` 等待的产物——库与工程在这里接上。

**Lattice 块**：

[library/scripts/library.mk:L137-L152](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L137-L152) —— `LATTICE_TARGETS` 主目标为 `./ltt/metadata.xml`（`L145`）；若设置了 `LATTICE_DEFAULT_PATHS=1`，还会把 IP 复制安装到用户主目录下的 `PropelIPLocal`（见 4.2.3 末）。`L150` 的 `.NOTPARALLEL: lattice` 显式禁止 lattice 目标并行——因为它的安装路径会写到共享目录，并行会冲突。

[library/scripts/library.mk:L156-L165](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L156-L165) —— `metadata.xml` 的 recipe：用 `$(LATTICE_IP_TOOL)`（即 `tclsh`）跑 `$(LIBRARY_NAME)_ltt.tcl`。注意这里不像 Xilinx 用 `vivado`，而是用纯 Tcl 解释器生成元数据。

Lattice 工具名来自：

[library/scripts/lattice_tool_set.mk:L6-L6](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/lattice_tool_set.mk#L6-L6) —— `LATTICE_IP_TOOL := tclsh`，定义 Lattice 打包用系统自带的 `tclsh`。

[library/scripts/lattice_tool_set.mk:L10-L18](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/lattice_tool_set.mk#L10-L18) —— 当 `LATTICE_DEFAULT_PATHS=1` 时，确定 `PropelIPLocal` 的安装根目录（区分 Cygwin 与 Linux 路径写法）。

#### 4.2.4 代码实践

**实践目标**：搞清楚 `make xilinx` 与 `make intel` 在 `library.mk` 里到底触发了什么不同的目标。

**操作步骤**：

1. 在 `library.mk` 中定位三个 `ifneq` 条件块，分别对应 Intel、Xilinx、Lattice。
2. 对每家，追踪「phony 目标 → 文件目标 → recipe 用的命令」三步。
3. 对照填写下表（先自己填，再对答案）：

| 命令 | phony 目标 | 文件产物 | recipe 关键命令 |
| --- | --- | --- | --- |
| `make xilinx` | ? | ? | ? |
| `make intel` | ? | ? | ? |
| `make lattice` | ? | ? | ? |

**需要观察的现象**：

- `make xilinx` 会真的调用 `vivado`；`make intel` 不会调用任何外部工具，只是 `touch`。
- `make xilinx` 失败时会在终端打印 `component.xml ... FAILED` 并指向 `*_ip.log`。
- 若库没有定义 `LATTICE_DEPS`，`make lattice` 没有规则（因为整个块被 `ifneq` 跳过）。

**预期结果（参考答案）**：

| 命令 | phony 目标 | 文件产物 | recipe 关键命令 |
| --- | --- | --- | --- |
| `make xilinx` | `xilinx` | `component.xml` | `vivado -mode batch -source <lib>_ip.tcl` |
| `make intel` | `intel` | `.timestamp_intel` | `touch .timestamp_intel` |
| `make lattice` | `lattice` | `ltt/metadata.xml` | `tclsh <lib>_ltt.tcl` |

#### 4.2.5 小练习与答案

**练习 1**：为什么 Intel 目标产物只是一个时间戳，而不是像 Xilinx 那样产出一个完整的 IP 描述文件？

**参考答案**：因为 Intel 的真正 IP 资产（Qsys 组件）是在**工程构建阶段**由 Quartus 直接消费 `*_hw.tcl` 生成的，库目录这一层不需要提前产出文件。库侧的 `.timestamp_intel` 只是用来「声明依赖关系已经满足、源文件已就绪」的时间戳标记，供依赖它的上层（其他库或工程）判断是否需要重建。Xilinx 则相反，`component.xml` 是 Vivado 块设计所必需的独立产物，必须在库侧提前打包好。

**练习 2**：`all: intel xilinx lattice`（`L65`）在一个只定义了 `XILINX_DEPS` 的库里执行 `make all` 会发生什么？

**参考答案**：`all` 依赖三个目标，但 Intel 块和 Lattice 块都因 `ifneq` 条件不成立而**没有定义** `intel`/`lattice` 规则，Make 对这些目标会报「No rule to make target」吗？——不会报错中断，因为这三个是 `.PHONY` 且 `all` 会尝试依次构建；对于无规则的目标，Make 视为「目标已是最新/无需构建」而跳过，最终只有 `xilinx`（→`component.xml`）真正执行。结论待本地验证（不同 Make 版本行为可能略有差异）。

---

### 4.3 跨库依赖传递

#### 4.3.1 概念说明

很多库不是孤立的——`axi_dmac` 依赖 `util_axis_fifo` 和 `util_cdc`。这种「库依赖库」的关系必须被翻译成 Make 能理解的文件依赖，否则当你打包 `axi_dmac` 时，它依赖的 `util_axis_fifo` 可能还没打包好，导致缺文件失败。

`library.mk` 用两个变量表达跨库依赖：

- `XILINX_LIB_DEPS`：列出所依赖的其他**库名**（不是文件）。每个名字会被展开成「那个库的 Xilinx 产物」，即 `<lib>/component.xml`。
- `INTEL_LIB_DEPS`：同理，展开成 `<lib>/.timestamp_intel`。

注意名字的对称性：跨库依赖要跟厂商走——Xilinx 的跨库依赖找的是对方的 `component.xml`，Intel 的找的是对方的 `.timestamp_intel`。这跟 4.2 讲的「每家产物不同」直接对应。

#### 4.3.2 核心流程

跨库依赖展开与递归构建的流程：

1. 用 `foreach` 把 `XILINX_LIB_DEPS` 里的每个库名，拼成 `<HDL_LIBRARY_PATH><lib>/component.xml` 路径，得到一组文件目标 `_XILINX_LIB_DEPS`。
2. 把这组文件目标加进 `component.xml` 的 prerequisites。
3. 给每个跨库目标写一条规则：`cd` 进对应库目录，`make xilinx`（Intel 则 `make intel`）。
4. Make 自动递归：要构建本库 `component.xml`，就得先保证依赖库的 `component.xml` 存在。

用伪代码描述这条翻译链：

```
XILINX_LIB_DEPS = util_axis_fifo util_cdc
        │ foreach
        ▼
_XILINX_LIB_DEPS = library/util_axis_fifo/component.xml
                   library/util_cdc/component.xml
        │ 作为 prerequisite 加入
        ▼
component.xml: ... $(XILINX_DEPS) $(_XILINX_LIB_DEPS)
        │ 每个跨库目标的 recipe
        ▼
flock <lib>/.lock  →  make -C <lib> xilinx   （递归、串行化）
```

#### 4.3.3 源码精读

先回到 `axi_dmac` 的声明：

[library/axi_dmac/Makefile:L53-L54](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L53-L54) —— 声明 `axi_dmac` 的 Xilinx 跨库依赖是 `util_axis_fifo` 和 `util_cdc`（只写库名，不带路径、不带后缀）。

接着看 `library.mk` 如何把这些名字翻译成文件目标：

[library/scripts/library.mk:L99-L100](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L99-L100) —— `L99` 用 `foreach` 把 `XILINX_LIB_DEPS` 的每个库名拼成 `<库目录>/component.xml`，存入 `_XILINX_LIB_DEPS`；`L100` 类似地把 `XILINX_INTERFACE_DEPS`（接口目录，如 `axi_dmac/interfaces`）拼成路径 `_XILINX_INTF_DEPS`。

[library/scripts/library.mk:L116-L116](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L116-L116) —— `component.xml` 的 prerequisites 同时包含 `$(XILINX_DEPS)`（本库文件）、`$(_XILINX_INTF_DEPS)`（接口）和 `$(_XILINX_LIB_DEPS)`（跨库产物）。Make 见到这些前置条件，会自动先把它们构建出来。

跨库目标的递归构建规则（关键）：

[library/scripts/library.mk:L129-L131](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L129-L131) —— 这是本模块最重要的一段。`FORCE:` 是一个始终会被认为「需要重建」的伪目标；`$(_XILINX_LIB_DEPS): FORCE` 让每个跨库 `component.xml` 目标**每次都被重新求值**（不靠时间戳短路），其 recipe 用 `flock <lib>/.lock -c "make -C <lib> xilinx"` 进入依赖库目录执行 `make xilinx`。

**为什么要 `flock`？** 因为多个库（或多个工程）可能同时依赖同一个 `util_axis_fifo`。如果不加锁，`make -j` 并行时两个进程会同时打包 `util_axis_fifo`，写到同一目录，产生损坏的 `component.xml`。`flock` 在库目录上加排他锁，把对同一 IP 的打包强制串行化。**这与 u3-l2 工程侧 `project-xilinx.mk` 的 flock 是同一个模式的另一面**——工程侧锁住从工程对库的调用，库侧锁住从库对库的调用，两者共同保证「同一个 IP 在任何时刻只有一个打包进程」。

作为对照，Intel 的跨库依赖要轻得多：

[library/scripts/library.mk:L82-L90](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L82-L90) —— `_INTEL_LIB_DEPS` 展开成 `<lib>/.timestamp_intel`，其 recipe 只是 `make -C <lib> intel`（`L89-L90`），不需要 `flock`，因为 Intel 产物只是个时间戳，没有真正的并发写冲突。这再次印证 Intel 库侧打包的「轻量」本质。

> 把 4.2 与 4.3 串起来看：跨库依赖之所以能传递，靠的是「把库名 foreach 成对应厂商的产物文件，再给每个产物写一条递归 `make` 规则」。这是 ADI HDL 构建系统能自动处理「库依赖库依赖库……」多层链的根本机制。

#### 4.3.4 代码实践

**实践目标**：跟踪一条真实的跨库依赖链，理解「库名 → 文件依赖 → 递归构建」的翻译过程。

**操作步骤**：

1. 确认 `axi_dmac` 的 `XILINX_LIB_DEPS` 为 `util_axis_fifo` 和 `util_cdc`（`L53-L54`）。
2. 手动套用 `library.mk` `L99` 的 `foreach` 公式：把每个库名前缀加上 `library/`，后缀加上 `/component.xml`，写出 `_XILINX_LIB_DEPS` 的展开结果。
3. 设想执行 `make -C library/axi_dmac xilinx`，按依赖顺序列出会被触发的目标。
4. 对比工程侧：回顾 u3-l2 中 `project-xilinx.mk` 的 `L138-L146`（工程如何 `flock` 调库），体会两处 flock 的对称性。

**需要观察的现象**：

- 即便 `util_axis_fifo/component.xml` 已存在，`FORCE` 也会让规则被触发，但真正进入子库后，若 `component.xml` 已是最新，`make` 会很快返回（增量）。
- 两个工程同时打包 `axi_dmac` 时，对 `util_axis_fifo` 的打包因 `flock` 不会冲突。

**预期结果**：得到一条「`axi_dmac/component.xml` → `util_axis_fifo/component.xml` + `util_cdc/component.xml` → 各自递归 `make xilinx`」的依赖链，且每个节点都受 flock 保护。结论待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Xilinx 跨库依赖用 `FORCE`（每次都重建），而 Intel 跨库依赖不用？

**参考答案**：Xilinx 侧 `component.xml` 的存在与时间戳不能可靠反映「上游源码是否变了」（因为 `FORCE` 强制触发，但进入子库后 Make 仍会按子库自己 `component.xml` 的真实时间戳决定是否重跑打包，所以不会无意义地全量重打包，只是确保依赖被检查到）。Intel 侧产物本身就是一个时间戳，再叠一层 `FORCE` 没有意义，且 `.timestamp_intel` 的更新即可表达「需要重建」。两种设计的差异源于产物语义不同。

**练习 2**：如果 `util_cdc` 自己也声明了 `XILINX_LIB_DEPS`，执行 `make -C library/axi_dmac xilinx` 时会发生什么？

**参考答案**：会形成多层递归——`axi_dmac` 的 `component.xml` 依赖 `util_cdc/component.xml`；构建后者时，`util_cdc` 的 `library.mk` 又会把它自己的 `XILINX_LIB_DEPS` 展开并递归构建。GNU Make 天然支持这种多层递归（recursive make），所以整条链会被自动展开到底层。这正是「跨库依赖传递」一词中「传递」的含义：依赖关系可以跨任意多层库自动蔓延。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务。

**任务**：以 `axi_dmac` 为样本，绘制一张完整的「库构建依赖图」，并预测 `make -C library/axi_dmac xilinx` 的执行轨迹。

**步骤**：

1. **列依赖桶**：从 `library/axi_dmac/Makefile` 提取 `GENERIC_DEPS`、`XILINX_DEPS`、`XILINX_LIB_DEPS`、`XILINX_INTERFACE_DEPS`，分类整理。
2. **标注合并**：在 `GENERIC_DEPS` 上额外标注「`library.mk` 还会并入 `adi_env.tcl`（`L61`）」，在 `XILINX_DEPS` 上标注「会并入 `GENERIC_DEPS`、`EXTERNAL_DEPS`、`adi_ip_xilinx.tcl`（`L96-L98`）」。
3. **画跨库链**：把 `XILINX_LIB_DEPS` 的两个名字按 `L99` 公式展开成 `library/util_axis_fifo/component.xml`、`library/util_cdc/component.xml`，并标注它们由 `L130-L131` 的 `flock + make -C xilinx` 触发。
4. **预测执行顺序**：写出 Make 的构建顺序（先递归把两个 util 库的 `component.xml` 建好，再回来建 `axi_dmac/component.xml`）。
5. **对比工程侧**：回顾 u3-l2 的 `project-xilinx.mk` `L83`（`M_DEPS += $(foreach dep,$(LIB_DEPS),.../component.xml)`），说明工程依赖 `axi_dmac` 时，最终也是落到同一个 `component.xml`，从而把库侧与工程侧接成一个完整闭环。

**交付物**：

- 一张包含 4 类节点（源文件 / 接口 / 跨库 IP / 本库产物 `component.xml`）的依赖草图。
- 一段 3～5 行的「执行轨迹」描述。
- 一句话回答：「库侧 `component.xml` 与工程侧 `component.xml` 是不是同一个文件？」（答案：是同一个——工程从不自己打包库 IP，而是触发库侧生成，再消费同一个 `component.xml`）。

如果本地装有 Vivado 且版本匹配（见 u1-l3），可尝试在 `library/axi_dmac` 下实际运行 `make xilinx`，观察终端的 `OK/FAILED` 单行输出与 `axi_dmac_ip.log`；否则按「源码阅读型实践」完成上述推理即可。

## 6. 本讲小结

- 单库 Makefile 用 `GENERIC_DEPS / XILINX_DEPS / INTEL_DEPS / LATTICE_DEPS` 四个桶把「厂商无关源码」与「各厂商专属打包资产」分组；桶之间通过 `library.mk` 的 `+=` 自动叠加。
- `library.mk` 为三家厂商各定义一套目标：`intel`→`.timestamp_intel`（仅时间戳，最轻）、`xilinx`→`component.xml`（跑 `vivado`，最完整）、`lattice`→`ltt/metadata.xml`（跑 `tclsh`）。每套都由 `ifneq($(<VENDOR>_DEPS),)` 按需启用。
- 当前仓库中 Xilinx 支持最广（83 库）、Intel 次之（23 库）、Lattice 最少（6 库），印证 Xilinx 是主线。
- 跨库依赖通过 `XILINX_LIB_DEPS`/`INTEL_LIB_DEPS` 表达：用 `foreach` 把库名翻译成对应厂商的产物文件，再给每个产物写递归 `make` 规则，从而实现依赖的自动传递。
- Xilinx 跨库依赖用 `FORCE`+`flock` 串行化打包，与 u3-l2 工程侧的 flock 是同一模式的镜像，共同防止多进程并发打包同一 IP 时损坏产物。
- 库侧产出的 `component.xml` 正是工程侧 `project-xilinx.mk` 所等待的产物——库是「零件厂」，工程是「组装者」，二者在 `component.xml` 上衔接。

## 7. 下一步学习建议

- 下一篇 **u4-l2《Xilinx IP 打包：adi_ip_xilinx.tcl 与 *_ip.tcl》** 会钻进 `component.xml` 的内部：`adi_ip_create`、`adi_ip_files`、`adi_ip_properties` 等原语如何把 Verilog 打包成 Vivado IP，以及接口自动推断机制。本讲只回答了「谁触发打包」，下一篇回答「打包内部怎么做的」。
- 之后 **u4-l3《Intel 与 Lattice 的 IP 打包》** 会对照 `*_hw.tcl` 与 `*_ltt.tcl`，把本讲中 Intel/Lattice「轻量」的差异讲透。
- 若想立刻验证本讲的依赖图，建议阅读 `library/Makefile`（`L14` 用 `find` 自动发现所有库子目录、`L29` 的 `lib` 目标逐一构建），这是从仓库根 `make lib` 进入库构建的入口。

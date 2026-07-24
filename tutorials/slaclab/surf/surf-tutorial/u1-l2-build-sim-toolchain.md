# 构建与仿真工具链：ruckus、Makefile、GHDL 与 CI

## 1. 本讲目标

上一篇我们建立了对 SURF「是什么」的认知：一个跨板卡复用的 VHDL/IP 基础设施库。本篇解决紧接着的下一个问题——**这么多分散在各子目录里的 `.vhd` 文件，到底靠什么被「拼」进一次 FPGA 构建？又靠什么在没有 Vivado 的情况下被快速检查语法、跑回归测试？**

读完本讲，你应当能够：

1. 把 `ruckus.tcl` 看作一份「构建清单（manifest）」，看懂 `loadRuckusTcl` / `loadSource` / `getFpgaArch` 三件套如何决定「哪些源码进构建」。
2. 看懂根目录 `Makefile` 如何用 `--std=08 --ieee=synopsys` 等参数把 VHDL 喂给开源仿真器 GHDL，并理解它把真正的构建规则委托给外部 `ruckus/system_ghdl.mk`。
3. 说出 `.github/workflows/surf_ci.yml` 里 `lint`、`test`、`docs` 三条流水线各自负责什么，以及它们如何串起「改了 HDL → 自动校验」的闭环。
4. 在本地亲手跑一次 `make MODULES=$PWD analysis`，对全仓库做一次 VHDL 语法分析。

## 2. 前置知识

在进入源码前，先用大白话对齐几个概念：

- **Vivado 与 GHDL 的分工。** Xilinx Vivado 是商业综合/实现工具，体积大、授权贵，最终下板子离不开它；GHDL 是开源的 VHDL 仿真器，轻量、可在 CI 里秒级安装。SURF 的策略是：**Vivado 负责真的「造比特流」，GHDL 负责「快速检查与回归仿真」**。本讲的 Makefile 与 CI 主要服务于后者。
- **构建清单（build manifest）。** 一个大型 FPGA 工程不可能把目录里所有 `.vhd` 一股脑丢给综合器——有家族专用文件、有仿真模型、有 IP 核。需要一个机制声明「这次构建到底要哪些文件」。在 SURF 里，这个机制就是每个目录下的 `ruckus.tcl`。
- **ruckus 是一个独立工具。** 注意：`ruckus.tcl` 文件本身在 SURF 仓库里，但解析它的 `loadRuckusTcl`、`loadSource`、`getFpgaArch` 等 Tcl 过程并不在 SURF 里——它们来自一个名叫 **ruckus** 的外部子模块（`ruckus/` 目录）。SURF 只负责「写清单」，ruckus 负责「读清单」。
- **VHDL-2008 与 Synopsys IEEE 库。** `--std=08` 指定使用 VHDL-2008 标准；而 `--ieee=synopsys` 让 GHDL 提供 Synopsys 版的 `std_logic_arith` / `std_logic_unsigned` 等非标准但业界通用的程序包。AGENTS.md 明确要求「沿用 Makefile/GHDL 流程的 VHDL-2008，不要顺手把旧的 `std_logic_arith` 改写成 `numeric_std`」，本讲的 Makefile 就是这条约定的源头。

> 承接上一篇：根 `ruckus.tcl` 加载的 `axi / base / dsp / devices / ethernet / protocols / xilinx` 七大子树，正是上一篇「仓库地图」里参与 HDL 综合的七块拼图。本篇解释「加载」这两个字在源码层面到底意味着什么。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它讲什么 |
| --- | --- | --- |
| `ruckus.tcl`（根） | 顶层构建清单，加载七大子树 | `loadRuckusTcl` 的递归加载、子模块版本校验 |
| `ethernet/GigEthCore/ruckus.tcl` | 一个核的目录级清单 | `getFpgaArch` 按 FPGA 家族选 PHY 目录 |
| `ethernet/GigEthCore/core/ruckus.tcl`、`gth7/ruckus.tcl` | 叶子清单 | `loadSource` 真正把 `.vhd` / `.dcp` 加入构建 |
| `Makefile`（根） | GHDL 流程的入口 | `analysis` 目标、`GHDL_BASE_FLAGS`、`include system_ghdl.mk` |
| `.github/workflows/surf_ci.yml` | GitHub Actions CI | lint / test / docs 三条流水线 |
| `pip_requirements.txt` | Python 依赖清单 | CI 与本地共用的工具链版本钉 |
| `scripts/vsg_linter.sh` | VHDL 风格检查脚本 | CI 里「VHDL Linter Checking」这一步的实现 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**ruckus 清单**、**Makefile/GHDL**、**CI 流水线**。三者形成一条链：ruckus 决定「有哪些源码」→ Makefile/GHDL 决定「怎么分析这些源码」→ CI 决定「每次提交自动跑哪些检查」。

### 4.1 ruckus 清单：用 Tcl 描述「哪些 HDL 进构建」

#### 4.1.1 概念说明

`ruckus.tcl` 是一份用 Tcl 写的构建清单。它不综合、不仿真，只回答一个问题：**这次构建要收集哪些 HDL 文件？** 这个回答会被 ruckus 工具读取，再转交给 Vivado（造比特流）或 GHDL（做语法分析/仿真）。

清单的核心是三件套：

- `source $::env(RUCKUS_PROC_TCL)`：加载 ruckus 工具提供的 Tcl 过程库（`loadRuckusTcl`、`loadSource`、`getFpgaArch` 等都来自这里）。几乎每个 `ruckus.tcl` 第一行都是它。
- `loadRuckusTcl "<目录>"`：递归地进入一个子目录，执行该子目录里的 `ruckus.tcl`。用它把清单「拼装」起来。
- `loadSource -lib surf -dir "<目录>"` / `-path "<文件>"`：真正把一个目录或单个文件（含 `.dcp` 布线后检查点）登记进当前 `surf` 工作库。
- `getFpgaArch`：返回当前目标 FPGA 的家族（如 `kintex7`、`zynquplus`），用来在不同家族的 PHY 目录之间做条件选择。

> AGENTS.md 把 `ruckus.tcl` 明确定义为「构建清单」，并要求「新增/移动/删除 HDL 时，在同一个改动里更新最近的 `ruckus.tcl`」。

#### 4.1.2 核心流程

根清单的加载流程可以画成：

```text
根 ruckus.tcl
  ├─ source RUCKUS_PROC_TCL        （拿到 loadRuckusTcl / loadSource / getFpgaArch）
  ├─ SubmoduleCheck ruckus 4.9.0    （确保外部 ruckus 工具版本正确）
  └─ loadRuckusTcl axi / base / dsp / devices / ethernet / protocols / xilinx
        └─ 进入每个子树的 ruckus.tcl
              └─ 继续递归，直到叶子目录用 loadSource 登记真实文件
```

而在一个「速率核 + 家族 PHY」的典型目录里，选择逻辑是：

```text
某核/ruckus.tcl
  ├─ loadRuckusTcl core/            （与家族无关的通用 RTL，永远加载）
  └─ set family [getFpgaArch]
        if family == kintex7   → loadRuckusTcl gtx7/
        if family == virtex7   → loadRuckusTcl gth7/
        if family == zynquplus → loadRuckusTcl gthUltraScale+/  (+ gtyUltraScale+)
        ...
              └─ 叶子 ruckus.tcl 用 loadSource 登记该家族专用的 .vhd / .dcp
```

这套 `core/`（通用）+ 家族目录（PHY 专用）的拆分，正是上一篇 u1-l3 要展开的「目录约定」在源码层的体现。

#### 4.1.3 源码精读

**根清单加载七大子树。** 先 `source` 拿到过程库，再做子模块版本校验，最后逐个 `loadRuckusTcl`：

[ruckus.tcl:1-2](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ruckus.tcl#L1-L2) 加载 ruckus 的 Tcl 过程库：

```tcl
# Load RUCKUS environment and library
source $::env(RUCKUS_PROC_TCL)
```

[ruckus.tcl:5-12](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ruckus.tcl#L5-L12) 用 `SubmoduleCheck` 校验外部 ruckus 工具的版本是否为 `4.9.0`，并支持用环境变量 `OVERRIDE_SUBMODULE_LOCKS` 跳过（注意：根 Makefile 默认就把这个变量置为 `1`，见 4.2.3）：

```tcl
if { [SubmoduleCheck {ruckus} {4.9.0} ] < 0 } {exit -1}
```

[ruckus.tcl:15-21](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ruckus.tcl#L15-L21) 正是上一篇「仓库地图」里七块拼图的加载入口——这里少一个 `python` 和 `tests`，因为它们不进 HDL 构建：

```tcl
loadRuckusTcl "$::DIR_PATH/axi"
loadRuckusTcl "$::DIR_PATH/base"
loadRuckusTcl "$::DIR_PATH/dsp"
loadRuckusTcl "$::DIR_PATH/devices"
loadRuckusTcl "$::DIR_PATH/ethernet"
loadRuckusTcl "$::DIR_PATH/protocols"
loadRuckusTcl "$::DIR_PATH/xilinx"
```

**用 `getFpgaArch` 在家族目录间选择。** 以 `GigEthCore` 为例，先无条件加载与家族无关的 `core/`，再按家族挑 PHY：

[ethernet/GigEthCore/ruckus.tcl:4-8](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/ruckus.tcl#L4-L8)：

```tcl
# Load the Core
loadRuckusTcl "$::DIR_PATH/core"

# Get the family type
set family [getFpgaArch]
```

[ethernet/GigEthCore/ruckus.tcl:10-48](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/ruckus.tcl#L10-L48) 用一连串 `if` 把家族映射到 PHY 目录——`artix7→gtp7`、`kintex7→gtx7`、`virtex7→gth7`、UltraScale+ 家族还会同时加载 `gthUltraScale+`、`gtyUltraScale+` 与 `lvdsUltraScale`：

```tcl
if { ${family} eq {kintex7} } {
   loadRuckusTcl "$::DIR_PATH/gtx7"
}
if { ${family} eq {virtex7} } {
   loadRuckusTcl "$::DIR_PATH/gth7"
}
if { ${family} eq {kintexuplus} ||
     ${family} eq {zynquplus} ||
     ${family} eq {zynquplusRFSOC} } {
   loadRuckusTcl "$::DIR_PATH/gthUltraScale+"
   loadRuckusTcl "$::DIR_PATH/gtyUltraScale+"
   loadRuckusTcl "$::DIR_PATH/lvdsUltraScale"
}
```

> 细节值得注意：`zynq` 家族还会用正则 `XC7Z(015|012).*` 在 `PRJ_PART` 上区分小型号，决定走 `gtp7` 还是 `gtx7`（见同文件 L18-24）。这就是「清单可以写得很有表达力」的体现。

**叶子清单用 `loadSource` 登记真实文件。** `core/ruckus.tcl` 只有一行，把整个 `rtl/` 目录登记进 `surf` 库：

[ethernet/GigEthCore/core/ruckus.tcl:5](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/core/ruckus.tcl#L5)：

```tcl
loadSource -lib surf -dir  "$::DIR_PATH/rtl"
```

而家族目录 `gth7/ruckus.tcl` 展示了两个进阶用法——按 Vivado 版本 gating，以及用 `-path` 登记单个 `.dcp` 布线检查点：

[ethernet/GigEthCore/gth7/ruckus.tcl:5-9](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/gth7/ruckus.tcl#L5-L9)：

```tcl
# Load Source Code
if { $::env(VIVADO_VERSION) >= 2016.4 } {
   loadSource -lib surf -dir  "$::DIR_PATH/rtl"
   loadSource -lib surf -path "$::DIR_PATH/images/GigEthGth7Core.dcp"
} else {
   puts "\n\nWARNING: $::DIR_PATH requires Vivado 2016.4 (or later)\n\n"
}
```

把上面三段连起来读，你就能复述 ruckus 的完整机制：**`loadRuckusTcl` 递归下钻 → `getFpgaArch` 在分叉处选家族 → `loadSource` 在叶子处登记文件**。

#### 4.1.4 代码实践

这是一个「源码阅读 + 手动演练」型实践，帮助你在脑子里跑通 `getFpgaArch` 的分发逻辑。

1. **实践目标**：不看源码地复述出「一个千兆以太网核在不同 FPGA 上会加载哪些 PHY 目录」。
2. **操作步骤**：
   - 打开 [ethernet/GigEthCore/ruckus.tcl](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/ruckus.tcl#L1-L48)。
   - 假设 `getFpgaArch` 分别返回 `kintex7`、`virtex7`、`zynquplus`、`virtexuplus`，在纸上列出每种情况下被 `loadRuckusTcl` 的子目录。
   - 对其中 `zynquplus` 这一支，再打开它选中的 `gthUltraScale+/ruckus.tcl`，确认它最终用 `loadSource` 登记了哪些 `.vhd`。
3. **需要观察的现象**：UltraScale+ 家族会**同时**加载 `gthUltraScale+` 与 `lvdsUltraScale` 两个目录；这是「一个家族可能需要多套 PHY」的真实例子。
4. **预期结果**：你能口头复述「family → PHY 目录列表」的映射，并理解 `core/` 永远被加载、家族目录按需加载。
5. 关于 `getFpgaArch` 在 GHDL（非 Vivado）流程下返回什么值：这取决于外部 ruckus 工具的实现与传入的环境变量，**待本地验证**——在纯 GHDL 流程里它通常退化为一个固定值，因而所有家族分支可能都不命中。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `core/` 目录用 `loadRuckusTcl`，而 `core/` 内部真正的 `.vhd` 用 `loadSource`？两者职责有何不同？

> **参考答案**：`loadRuckusTcl` 负责「进入子目录并执行它的 `ruckus.tcl`」，是清单之间的递归装配；`loadSource` 负责「把具体源码文件登记进工作库」，是清单的叶子动作。前者管「结构」，后者管「文件」。

**练习 2**：如果新增了一个只用于 `virtex7` 的 `.vhd`，你应当改哪些文件？

> **参考答案**：先确认它属于哪个核的 `gth7/`（virtex7 对应 PHY）目录；把文件放进对应 `rtl/`；若该 `rtl/` 已被某个 `loadSource -dir` 覆盖则无需改清单，否则在最近的 `ruckus.tcl` 里加一行 `loadSource -lib surf -path ...`。AGENTS.md 要求这一步与 HDL 改动在同一个提交里完成。

---

### 4.2 Makefile 与 GHDL：把 VHDL 喂给开源仿真器做语法分析

#### 4.2.1 概念说明

根目录的 `Makefile` 是 GHDL 流程的入口。它本身很短——只做两件事：**定参数**（GHDL 用什么标准、什么 IEEE 库、警告怎么过滤）和**委托**（把真正的构建规则 `include` 自外部 ruckus 工具的 `system_ghdl.mk`）。

关键点：`Makefile` 里并没有 `analysis:` 或 `import:` 这样的目标定义——它们来自被 `include` 进来的 `ruckus/system_ghdl.mk`。所以读这个 Makefile 时，重点不是「目标怎么做」，而是「环境与参数怎么设」。

#### 4.2.2 核心流程

```text
make MODULES=$PWD analysis
  └─ Makefile 先导出环境：
       MODULES / RUCKUS_DIR / TOP_DIR / PROJ_DIR / OUT_DIR=build
       GHDL_CMD=ghdl
       GHDL_BASE_FLAGS = --workdir=build --std=08 --ieee=synopsys -frelaxed-rules -fexplicit
       GHDL_WARNING_FLAGS = （按 GHDL 实际支持的警告名，关掉 elaboration/hide/specs/shared）
  └─ include ruckus/system_ghdl.mk   ← analysis / import 目标来自这里
  └─ analysis 目标：用 GHDL 分析 ruckus 收集到的全部 .vhd
```

参数直觉：

- `--std=08`：VHDL-2008，SURF 的目标标准。
- `--ieee=synopsys`：用 Synopsys 版 IEEE 库，兼容仓库里仍在用的 `std_logic_arith` / `std_logic_unsigned`。
- `-frelaxed-rules` / `-fexplicit`：放宽若干严格性检查、显式化重载解析，避免对历史代码的误报。
- 警告过滤：`GHDL_OPTIONAL_WARNINGS = elaboration hide specs shared`，再用一段 `$(shell ghdl --help-warnings ...)` 动态判断当前 GHDL 是否支持这些警告名，**支持才加 `-Wno-<warn>`**。这样换 GHDL 版本时不会因「不认识的警告名」而报错。

#### 4.2.3 源码精读

**默认目标与目录变量。** [Makefile:12-21](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/Makefile#L12-L21)：

```makefile
# Define default target
target: analysis

ifndef MODULES
export MODULES = $(abspath $(PWD)/../)
endif

export RUCKUS_DIR = $(MODULES)/ruckus
export TOP_DIR    = $(abspath $(PWD))
export PROJ_DIR   = $(abspath $(PWD))
export OUT_DIR    = $(PROJ_DIR)/build
```

`target: analysis` 说明直接敲 `make` 等价于敲 `make analysis`。`MODULES` 默认指向上一级目录（因为 SURF 通常作为子模块被一个更大的板卡工程包含，板卡工程的同级还有 `ruckus/` 等）。CI 里则用 `make MODULES=$PWD ...` 把 `MODULES` 显式钉在 SURF 根目录。

**关闭子模块锁。** [Makefile:23-24](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/Makefile#L23-L24)：

```makefile
# Override the submodule check because ruckus external of this repo
export OVERRIDE_SUBMODULE_LOCKS = 1
```

这正好呼应 4.1.3 里根 `ruckus.tcl` 的 `OVERRIDE_SUBMODULE_LOCKS` 判断——在 SURF 自身仓库里运行时，ruckus 不一定按 `4.9.0` 锁定版本，所以强制跳过该校验。

**GHDL 命令与核心参数。** [Makefile:26-35](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/Makefile#L26-L35)：

```makefile
ifndef GHDL_CMD
export GHDL_CMD = ghdl
endif

export GHDL_BASE_FLAGS = \
	--workdir=$(OUT_DIR) \
	--std=08 \
	--ieee=synopsys \
	-frelaxed-rules \
	-fexplicit
```

**动态警告过滤。** [Makefile:37-40](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/Makefile#L37-L40) 用一段 shell 探测当前 GHDL 支持的警告名，只对支持的告警加 `-Wno-`：

```makefile
export GHDL_OPTIONAL_WARNINGS = elaboration hide specs shared
export GHDL_SUPPORTED_WARNING_NAMES := $(shell $(GHDL_CMD) --help-warnings 2>/dev/null | awk ...)
export GHDL_WARNING_FLAGS := $(strip $(foreach warn,$(GHDL_OPTIONAL_WARNINGS),$(if $(filter ...),-Wno-$(warn))))
export GHDLFLAGS = $(GHDL_BASE_FLAGS) $(GHDL_WARNING_FLAGS)
```

**委托真正的规则。** [Makefile:42-43](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/Makefile#L42-L43)：

```makefile
# Load the common makefile library
include $(MODULES)/ruckus/system_ghdl.mk
```

`analysis`、`import` 等目标都来自这个被 `include` 的文件。该文件位于**外部 ruckus 子模块**（`ruckus/system_ghdl.mk`），不在 SURF 仓库内，其内部实现细节以 ruckus 上游为准。我们只关心它对外的两个可观察行为：

- `analysis`：用 GHDL 对 ruckus 收集到的全部 VHDL 做语法/ elaboration 分析（CI 里这一步的名字就叫「VHDL Syntax Checking」，见 4.3.3）。
- `import`：执行 ruckus 的源码导入，生成供 cocotb 测试使用的源缓存（CI 在跑 pytest 前会先 `make ... import`，见 4.3.3）。

> AGENTS.md 也建议「改完 ruckus 结构后，尽可能跑一次 `make MODULES="$PWD" import` 确认导入图仍能解析」。

#### 4.2.4 代码实践

本讲的主实践任务（与讲义规格一致）：**对全仓库跑一次 VHDL 语法分析**。

1. **实践目标**：亲手触发一次 GHDL 语法分析，理解 `analysis` 目标的输入与产物。
2. **操作步骤**：
   1. 确保已安装 `make`、`ghdl`、`tclsh`，并克隆/链接了 ruckus（CI 里的做法是 `git clone https://github.com/slaclab/ruckus.git ruckus`，见 4.3.3）。
   2. 在 SURF 根目录执行：
      ```bash
      make MODULES=$PWD analysis
      ```
   3. 若有报错，记下报错信息里出现的 `.vhd` 文件路径，以及它属于哪个 `ruckus.tcl` 清单（沿目录向上找最近的 `ruckus.tcl`）。
3. **需要观察的现象**：`build/` 目录下会生成 GHDL 的工作文件（`--workdir=$(OUT_DIR)`）；终端可能打印若干被 `-Wno-` 关闭的告警类别。
4. **预期结果**：在没有改动源码的干净 HEAD 上，`analysis` 应当成功退出（exit 0）。若失败，通常意味着某份 HDL 真的存在语法/ elaboration 问题，或 ruckus 清单与实际文件不一致。
5. **是否一定通过**：取决于本机 GHDL 版本与 ruckus 版本是否匹配，**待本地验证**。建议优先复现 CI 的安装步骤（见 4.3.3 的依赖安装段）以减少环境差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GHDL_OPTIONAL_WARNINGS` 要先用 `--help-warnings` 探测再决定是否加 `-Wno-`？直接写死 `-Wno-elaboration` 会怎样？

> **参考答案**：不同 GHDL 版本支持的告警名集合不同。写死可能在旧版/新版上因「未知告警名」而报错退出。探测后只对当前版本支持的告警加 `-Wno-`，换版本仍能工作。

**练习 2**：`--ieee=synopsys` 解决了什么问题？如果去掉它会怎样？

> **参考答案**：它让 GHDL 提供 Synopsys 版的 `std_logic_arith` / `std_logic_unsigned`。SURF 仓库里有历史代码 `use` 了这些非标准包；去掉后这些文件会在分析阶段因找不到包而报错。这也解释了 AGENTS.md 为何不让人顺手把它们改成 `numeric_std`——会让全仓库在 GHDL 流程下行为不一致。

---

### 4.3 CI 流水线：lint / test / docs 三条线

#### 4.3.1 概念说明

`.github/workflows/surf_ci.yml` 定义了 SURF 的 GitHub Actions CI，在每次 `push` 时触发。它把「代码质量」拆成三条独立并行的 job：

- **lint（静态检查）**：空格/Tab、Python（flake8）、C/C++（cpplint）、VHDL 风格（vsg）、VHDL 语法（GHDL `analysis`）。
- **test（回归测试）**：`make import` 后用 `pytest + cocotb + GHDL` 跑并行回归，并收集代码覆盖率。
- **docs（文档）**：用 Doxygen 生成文档，打 tag 时部署到 GitHub Pages。

此外还有两个**可复用工作流**（`gen_release`、`conda_build_lib`），它们 `needs: [lint, test, docs]`——也就是前三条全绿之后才发版、才发 conda 包。

#### 4.3.2 核心流程

```text
push
  ├─ lint     ─┐
  ├─ test     ─┤  三条并行
  ├─ docs     ─┘
  └─ gen_release / conda_build_lib  （needs: lint+test+docs，复用 ruckus 的 workflow）
```

依赖安装是三条 job 共享的关键前置步骤：装系统包（`make python3 python3-pip tclsh ghdl`）→ 装 SURF 的 `pip_requirements.txt` → 若 `ruckus/` 不存在就 `git clone` → 再装 ruckus 自己的 `pip_requirements.txt`。这保证了「ruckus 工具」与「SURF 代码」两边依赖都到位。

#### 4.3.3 源码精读

**触发与并发抑制。** [.github/workflows/surf_ci.yml:16-20](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml#L16-L20)：每次 `push` 触发，并用 `cancel-in-progress: true` 取消同分支上旧的运行——这样连续推送不会堆积任务。

**lint job 的依赖安装。** [.github/workflows/surf_ci.yml:36-49](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml#L36-L49) 是「环境就位」的范本，也是本地复现 CI 的参考：

```yaml
sudo apt-get install -y make python3 python3-pip tclsh ghdl
python -m pip install -r pip_requirements.txt
if [ ! -d ruckus ]; then
  git clone https://github.com/slaclab/ruckus.git ruckus
fi
python -m pip install -r ruckus/scripts/pip_requirements.txt
```

注意它还会处理 `ruckus` 是坏软链接的情况（`-L ruckus && ! -d ruckus`），这是子模块在浅 checkout 下的常见问题。

**空格/Tab 检查。** [.github/workflows/surf_ci.yml:51-61](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml#L51-L61)：用两个 `grep` 扫描 `.vhd/.tcl/.py` 里的行尾空格与 Tab 字符，命中就让 CI 失败。这是最低成本也最常踩的格式门禁。

**Python / C / VHDL 风格检查。** [.github/workflows/surf_ci.yml:63-74](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml#L63-L74)：

```yaml
- name: Python Linter Checking
  run: |
    python -m compileall -f python/ scripts/ tests/
    flake8 --count python/ scripts/ tests/
- name: C/C++ Linter Checking
  run: |
    find . -name '*.h' -o -name '*.cpp' -o -name '*.c' | xargs cpplint
- name: VHDL Linter Checking
  run: |
    source scripts/vsg_linter.sh
```

其中 VHDL 风格检查委托给 `scripts/vsg_linter.sh`（VSG = VHDL Style Guide）。该脚本会[scripts/vsg_linter.sh:46-55](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/scripts/vsg_linter.sh#L46-L55)按 CPU 核数把所有 `.vhd` 分块并行检查，并在 [scripts/vsg_linter.sh:19-28](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/scripts/vsg_linter.sh#L19-L28) 排除一批第三方/厂商文件（如 `protocols/i2c/rtl` 下带非 SLAC license 的导入库、`EthCrc32Pkg.vhd` 等），避免对不归自己管的代码报风格错。

**VHDL 语法检查 = 本讲的 `analysis`。** [.github/workflows/surf_ci.yml:76-78](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml#L76-L78)：

```yaml
- name: VHDL Syntax Checking
  run: |
    make MODULES=$PWD analysis
```

这正是 4.2.4 那个实践任务在 CI 里的真实形态。

**test job：并行回归 + 覆盖率。** [.github/workflows/surf_ci.yml:107-115](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml#L107-L115)：

```yaml
- name: Parallel Regression Tests
  run: |
    make MODULES=$PWD import
    python -m pytest --cov -v -n auto --dist=worksteal tests/axi tests/base tests/dsp tests/protocols
- name: Code Coverage
  run: |
    codecov
    coverage report -m
```

两个要点：先 `make import` 生成 cocotb 需要的源缓存，再用 `pytest-xdist` 的 `-n auto --dist=worksteal` 跨多进程并行跑 `tests/` 下的若干子系统。`--cov` 开启覆盖率，随后上传到 codecov。这套 `pytest + cocotb + GHDL + ruckus` 正是 AGENTS.md 钉死的「预期回归栈」，会在单元九（u9）展开。

**docs job 与发版门禁。** [.github/workflows/surf_ci.yml:119-141](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml#L119-L141) 用 Doxygen 生成文档，且仅当 `github.ref` 以 `refs/tags/` 开头时部署到 Pages。而 [.github/workflows/surf_ci.yml:144-160](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml#L144-L160) 的 `gen_release` 与 `conda_build_lib` 都带 `needs: [lint, test, docs]`，并复用 ruckus 仓库里的工作流——**前三条不全绿，就不发版、不发 conda 包**。

**工具链版本钉。** [pip_requirements.txt:1-11](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/pip_requirements.txt#L1-L11) 把 CI 与本地共用的 Python 工具钉死：`flake8`、`cocotb`、`cocotbext-axi`、`cocotb-test`、`coverage`/`codecov`、`pytest`/`pytest-cov`/`pytest-xdist`、`cpplint`、`vsg`。记住这个清单，看 CI 里任何一步都能对上号。

#### 4.3.4 代码实践

1. **实践目标**：把 CI 的 lint 流水线在本地复现一小步，理解「空格/Tab 门禁」有多容易被触发。
2. **操作步骤**：
   1. 在仓库根执行（与 CI 同样的两条 grep）：
      ```bash
      grep -rnI '[[:blank:]]$' --include=*.{vhd,tcl,py} . | head
      grep -rnI $'\t' --include=*.{vhd,tcl,py} . | head
      ```
   2. 若有输出，挑一行，用编辑器把行尾空格删掉、把 Tab 改成空格。
   3. 重新跑这两条 grep，确认该行不再出现。
3. **需要观察的现象**：第一条 grep 命中「行尾多余空格」，第二条命中「用 Tab 缩进」。
4. **预期结果**：在一个符合规范的文件上，两条 grep 都应无输出；CI 的对应步骤也是无输出即通过。
5. **注意**：不要为了练习去改动你不负责的源码；建议在自己的临时分支上验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `gen_release` 要写 `needs: [lint, test, docs]`？如果删掉会怎样？

> **参考答案**：这是发版门禁——保证只有 lint/test/docs 全绿的提交才发版。删掉后发版可能与某个失败的检查并行执行，导致发出带语法错误或测试不过的版本。

**练习 2**：test job 里为什么是 `make import` 而不是 `make analysis`？两者差别是什么？

> **参考答案**：`analysis` 只做 GHDL 语法分析（lint 用）；`import` 会执行 ruckus 的源码导入、生成 cocotb 测试消费的源缓存。回归测试需要后者提供的可仿真源码集合，所以 test job 用 `import`。

**练习 3**：`vsg_linter.sh` 为什么要排除 `protocols/i2c/rtl` 下的一批文件？

> **参考答案**：那些是带非 SLAC license 的第三方/导入库（AGENTS.md 也把 `protocols/i2c/rtl` 列为外部代码），其风格不归 SURF 管，强行用 SURF 的 VSG 规则检查只会产生噪音与误报。

---

## 5. 综合实践

把三个最小模块串起来，完成一次「**改一行 HDL → 看着 CI 各步分别作出反应**」的推演（建议在自己的临时分支，且不要提交到主仓库）：

1. **改清单**：参考 4.1.3，在一个你熟悉的核（例如 `ethernet/GigEthCore/core/`）下，确认其 `ruckus.tcl` 用 `loadSource -dir rtl` 登记了整个 `rtl/` 目录。在纸上说明：如果你新建一个 `rtl/Foo.vhd`，是否需要改 `ruckus.tcl`？为什么？（答案：不需要，`-dir` 已覆盖整个目录。）
2. **过语法**：执行 `make MODULES=$PWD analysis`，确认新增文件被 GHDL 分析到（若文件故意留语法错误，应在这里被报出来；修好后再次通过）。
3. **过风格**：执行 `source scripts/vsg_linter.sh`，确认新文件符合 VSG 规则；再跑 4.3.4 的两条 grep，确认无空格/Tab。
4. **过测试**：执行 `make MODULES=$PWD import`，再挑一个子系统跑 `python -m pytest -q tests/base/fifo`（或你改动的模块对应的测试目录），观察 pytest 是否收集并执行用例。
5. **对照 CI**：把你在第 2~4 步本地做的事，与 `.github/workflows/surf_ci.yml` 里 lint/test 两 job 的步骤一一对应，确认你理解了「CI 里每一步在我本地对应哪条命令」。

完成后，你应当能用一句话讲清：**SURF 用 `ruckus.tcl` 声明源码集合，用 `Makefile`+GHDL 做语法分析，用 CI 把 lint/test/docs 自动化，三者构成「改 HDL → 自动校验」的闭环。**

## 6. 本讲小结

- `ruckus.tcl` 是构建清单：`loadRuckusTcl` 递归装配子目录、`getFpgaArch` 按 FPGA 家族选 PHY、`loadSource -lib surf` 在叶子处登记真实 `.vhd`/`.dcp`。
- 根 `ruckus.tcl` 加载七大 HDL 子树（`axi/base/dsp/devices/ethernet/protocols/xilinx`），并用 `SubmoduleCheck` 校验外部 ruckus 工具版本。
- 根 `Makefile` 很短：只设环境与 GHDL 参数（`--std=08 --ieee=synopsys -frelaxed-rules -fexplicit` + 动态告警过滤），再把规则 `include` 自外部 `ruckus/system_ghdl.mk`。
- `--ieee=synopsys` 是为了兼容仓库里仍在用的 Synopsys 版 `std_logic_arith/unsigned`，这也是 AGENTS.md 不让人顺手改写为 `numeric_std` 的原因。
- CI 分 lint / test / docs 三条并行 job，另有 `gen_release`/`conda_build_lib` 以 `needs` 串成发版门禁。
- 本地复现 CI 的关键命令：`make MODULES=$PWD analysis`（语法）、`make MODULES=$PWD import` + `pytest`（回归）。

## 7. 下一步学习建议

- **下一步学 u1-l3（目录结构与文件分类约定）**：本讲多次出现的 `core/` + 家族 PHY 目录拆分、`rtl/sim/tb/wrappers` 分类，正是 u1-l3 的主题，读完能更立体地理解 `getFpgaArch` 为什么那样分目录。
- **随后学 u1-l4（StdRtlPkg 基础类型）**：理解了 GHDL 流程后，下一站是 GHDL 实际分析的第一个核心包 `StdRtlPkg.vhd`。
- **延伸阅读**：想了解 `import` 与 cocotb 如何消费源缓存，可先扫一眼 `tests/README.md`，单元九（u9-l1/u9-l2）会系统讲解 cocotb 工具链。
- **外部依赖**：`loadRuckusTcl`/`loadSource`/`getFpgaArch` 与 `analysis`/`import` 目标都来自外部 ruckus 工具（`ruckus/` 子模块、`ruckus/system_ghdl.mk`）。本讲只描述了它们在 SURF 侧的可观察行为；想看实现细节请到 [slaclab/ruckus](https://github.com/slaclab/ruckus) 仓库，版本以 SURF 锁定的 `4.9.0` 为准（**待确认**当前主分支是否仍兼容）。

# 仿真驱动脚本与 PsiSim 流程

## 1. 本讲目标

本讲聚焦 `scripts/` 下三个仿真驱动脚本：`runModelsim.tcl`、`runGhdl.tcl`、`runVivado.tcl`。学完后你应当能够：

- 说清这三个脚本「长成几乎一样」的共同骨架，以及它们各自服务于哪一款仿真器。
- 解释 PsiSim 框架是如何被加载进来的（`source` + `namespace import` 两步）。
- 描述 PsiSim 的五步仿真流水线：**init → configure → compile → run → check**。
- 看懂三个脚本在三处细微差异：`init` 参数（`-ghdl` / `-vivado` / 默认）、`source` 是否带 `-quiet`、被启用的库集合不同。
- 能仿照模板，自己写出一个新的仿真驱动脚本。

> 本讲只讲「流水线骨架与 PsiSim 的调用方式」。哪个库在哪款仿真器下兼容、为什么 power_sink 不仿真，属于下一讲 u2-l3 的主题，本讲只在需要时点到为止。

---

## 2. 前置知识

阅读本讲前，最好先具备以下直觉（不熟悉也没关系，下面会顺带解释）：

1. **集合仓库与 submodule（u1-l1 / u1-l2）**：`psi_fpga_all` 本身几乎不含代码，它用 git submodule 把各 FPGA 库挂到固定目录。本讲的三个脚本里出现的 `../TCL/PsiSim/PsiSim.tcl`、`../VHDL/psi_common/sim/config.tcl` 等，**都是 submodule 内部的文件**，本仓库并不直接包含它们的内容。所以这些被 `source` 进来的文件，其内部实现本讲一律标注「待确认」——我们能确定的是「脚本在这里 source 了它」，而不是「它内部怎么实现」。

2. **驱动脚本模板（u2-l1）**：你已经知道 `scripts/` 下的脚本都遵循 `set myPath [pwd]` → `cd` 到子模块目录 → `source` 内部脚本 → `cd $myPath` 回起点的编排模式。本讲就是把这套模板套到「仿真」这件事上。

3. **一点点 TCL 语法**：
   - `source <文件>`：把另一个 TCL 文件读进来并在当前作用域执行，相当于「把那段代码粘贴到这里」。
   - `cd` / `pwd`：切换 / 获取当前工作目录，和 shell 里一样。
   - `set 变量 值`：给变量赋值。
   - `namespace`：TCL 的命名空间，用来把一组命令圈在一个前缀下，避免重名。下面 4.1 会详细讲。

---

## 3. 本讲源码地图

本讲真正会读到的源码文件只有三个，全部在 `scripts/` 下：

| 文件 | 作用 | 服务的仿真器 |
|---|---|---|
| [scripts/runModelsim.tcl](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl) | 默认（ModelSim）仿真驱动 | Mentor ModelSim / Questa |
| [scripts/runGhdl.tcl](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runGhdl.tcl) | GHDL 仿真驱动 | GHDL（开源 VHDL 仿真器） |
| [scripts/runVivado.tcl](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl) | Vivado 仿真驱动 | Xilinx Vivado xsim |

它们各自又「远程」依赖两个不在本仓库里的 submodule 内部脚本（内容「待确认」）：

| 被 source 的文件 | 所在位置（submodule） | 角色 |
|---|---|---|
| `PsiSim.tcl` | `TCL/PsiSim`（对应 `.gitmodules` 里 `path = TCL/PsiSim`） | PsiSim 框架本体，提供 `init` / `compile_files` / `run_tb` / `run_check_errors` 等命令 |
| 各库的 `config.tcl` | 例如 `VHDL/psi_common/sim/config.tcl` | 每个库自己的仿真配置：登记源文件、testbench、仿真参数等 |

记住这张地图，后面所有讲解都围绕「驱动脚本如何把 PsiSim 框架和各库 config 串成一条流水线」展开。

---

## 4. 核心概念与源码讲解

三个脚本的整体结构可以抽象成同一条流水线：

```
[加载 PsiSim 框架]   source PsiSim.tcl + namespace import      ← 前置（不算五步之一）
        │
        ▼
[1. init]        选择仿真器后端（默认 / -ghdl / -vivado）
        │
        ▼
[2. configure]   逐库 cd 到 <lib>/sim 后 source 该库 config.tcl
        │
        ▼
[3. compile]     compile_files -all -clean
        │
        ▼
[4. run]         run_tb -all
        │
        ▼
[5. check]       run_check_errors "###ERROR###"
```

下面按三个最小模块拆开讲：框架加载、init + configure、三段执行。

### 4.1 PsiSim 框架加载：source 与 namespace import

#### 4.1.1 概念说明

驱动脚本本身并不实现「怎么编译 VHDL、怎么跑 testbench」——这些是 **PsiSim 框架**的职责。PsiSim 是一个用 TCL 写的仿真框架（住在 `TCL/PsiSim` 这个 submodule 里），它对外提供一批命令，比如：

- `init`：初始化仿真上下文、选定仿真器后端；
- `compile_files`：编译登记过的源文件；
- `run_tb`：运行 testbench；
- `run_check_errors`：扫描日志、报告错误。

驱动脚本要做的第一件事，就是把这些命令「搬」进自己的作用域，让后续代码可以直接写 `init`、`compile_files`，而不必每次写全名。这需要两步：

1. `source ../TCL/PsiSim/PsiSim.tcl` —— 把框架代码加载进来、定义好所有命令；
2. `namespace import psi::sim::*` —— 把 `psi::sim` 命名空间下的全部命令导入当前命名空间，使它们可以被「裸名」调用。

#### 4.1.2 核心流程

这里需要先理解 TCL 的 **命名空间（namespace）**。PsiSim 把自己的命令放在 `psi::sim::` 这个前缀下，于是框架内部定义的命令全名其实是：

- `psi::sim::init`
- `psi::sim::compile_files`
- `psi::sim::run_tb`
- `psi::sim::run_check_errors`
- ……（其余 `psi::sim::*`）

如果不导入，调用时必须写全名 `psi::sim::init`。执行：

```
namespace import psi::sim::*
```

后，`*` 通配匹配该命名空间下所有命令，把它们一一导入当前作用域。之后就可以直接写：

```
init
compile_files -all -clean
```

加载流程如下：

```
source ../TCL/PsiSim/PsiSim.tcl   ──►  PsiSim.tcl 在当前作用域执行，
                                        定义了 psi::sim::init 等命令
        │
        ▼
namespace import psi::sim::*      ──►  把 psi::sim::* 导入当前命名空间，
                                        此后 init / compile_files 可裸名调用
```

#### 4.1.3 源码精读

`runModelsim.tcl` 的开头两行就是这两步：

[scripts/runModelsim.tcl:7-9](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L7-L9) —— 加载 PsiSim 框架并导入命令：

```tcl
#Load dependencies
source ../TCL/PsiSim/PsiSim.tcl
namespace import psi::sim::*
```

`runGhdl.tcl` 的对应部分一字不差：

[scripts/runGhdl.tcl:7-9](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runGhdl.tcl#L7-L9) —— 同样的 `source` + `namespace import`。

`runVivado.tcl` 唯一的差别是给 `source` 加了 `-quiet` 选项，并多写了一行注释：

[scripts/runVivado.tcl:7-11](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl#L7-L11) —— `-quiet` 抑制 `source` 时可能产生的回显，保持 Vivado Tcl 控制台整洁：

```tcl
#Load dependencies
source -quiet ../TCL/PsiSim/PsiSim.tcl

#Import psi::sim library
namespace import psi::sim::*
```

> 说明：`PsiSim.tcl` 的内部实现（它如何定义 `init` 等命令、`-quiet` 具体压制了哪些输出）位于 `TCL/PsiSim` submodule 内，本仓库不含，**待确认**。

#### 4.1.4 代码实践

这是一个「源码阅读型实践」，目标是亲手确认三个脚本在框架加载上的异同。

1. **实践目标**：确认三个脚本都用「source + namespace import」加载 PsiSim，并找出唯一的差异点。
2. **操作步骤**：
   - 打开 [scripts/runModelsim.tcl:7-9](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L7-L9)、[scripts/runGhdl.tcl:7-9](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runGhdl.tcl#L7-L9)、[scripts/runVivado.tcl:7-11](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl#L7-L11)。
   - 逐行比对这三段。
3. **需要观察的现象**：前两个脚本完全相同；第三个脚本多了 `-quiet` 和一行 `#Import psi::sim library` 注释。
4. **预期结果**：你应当能用一句话总结——「三个脚本都用 `source` 加载 `PsiSim.tcl` 并 `namespace import psi::sim::*`，区别仅在于 Vivado 版给 `source` 加了 `-quiet`」。
5. 如需在本地进一步验证（例如想看 `namespace import` 前后命令是否可用），可在已装好 Vivado 的 Tcl Console 里 `source` 一份 PsiSim.tcl 后分别执行 `psi::sim::init`（全名）与 `namespace import psi::sim::*` 后的 `info commands init`。具体环境未在 CI 中验证，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么需要 `namespace import psi::sim::*`？如果不写这行，后面的 `init` 要怎么调用？

> **答案**：不导入的话，命令在 `psi::sim::` 命名空间下，必须用全名 `psi::sim::init` 调用。`namespace import psi::sim::*` 把该命名空间下所有命令导入当前作用域，之后就能裸名写 `init`，脚本更简洁。

**练习 2**：`runVivado.tcl` 的 `source` 为什么多了一个 `-quiet`？

> **答案**：`-quiet` 是 TCL `source` 命令的标准选项，用来抑制「再次 source 同一文件时」可能产生的回显或警告，保持 Vivado Tcl 控制台输出干净。PsiSim.tcl 内部是否会产生这类输出**待确认**，但 `-quiet` 的语义是确定的。

---

### 4.2 init 初始化与逐库 configure（config.tcl）

#### 4.2.1 概念说明

框架加载完之后，要进入正式仿真流程，先做两件事：

1. **init（初始化）**：告诉 PsiSim「这一轮用哪款仿真器」，并建立仿真上下文。三款仿真器对应三种调用方式：
   - `init`（不带参数）→ 默认，即 ModelSim；
   - `init -ghdl` → GHDL；
   - `init -vivado` → Vivado xsim。
2. **configure（配置）**：把「这一轮要仿真哪些库」登记给 PsiSim。办法是**逐库** `cd` 到该库的 `sim/` 目录，再 `source` 该库自带的 `config.tcl`。`config.tcl` 内部会向 PsiSim 登记这个库的源文件、testbench、顶层、仿真参数等（具体登记内容**待确认**，因为 config.tcl 在各 submodule 内）。

这里有一个关键设计：**跳过某个库 = 把它的 `cd` + `source` 两行整块注释掉**。这就是为什么同一份模板能同时服务三款仿真器——区别只在于「哪些库被注释掉了」和「init 带什么参数」。详细的兼容性矩阵留给 u2-l3，本讲只看机制。

#### 4.2.2 核心流程

configure 阶段的固定套路（来自 u2-l1 的模板）：

```
set myPath [pwd]                 # 记录起点（应为 scripts/）

cd $myPath/../VHDL/psi_common/sim
source config.tcl                # 登记 psi_common

cd $myPath/../VHDL/psi_fix/sim
source config.tcl                # 登记 psi_fix

...（更多库，或被 # 注释跳过）...

cd $myPath                       # 回到起点 scripts/
```

几个要点：

- **`set myPath [pwd]` 锚定起点**：所有库路径都写成 `$myPath/../<类>/<lib>/sim`，即从 `scripts/` 上一级（仓库根）再进各库。这样脚本不依赖被 source 时的绝对路径，只要执行时 `pwd` 是 `scripts/` 即可。最后 `cd $myPath` 回到起点，给后续阶段一个干净的工作目录。
- **`cd` 与 `source` 成对出现**：`source config.tcl` 用的是相对路径，所以必须先 `cd` 到该库的 `sim/` 目录，让 `config.tcl` 里可能存在的相对路径（引用本库的 VHDL 源文件）能正确解析。
- **跳过一个库**：用 `#` 把它的 `cd` 和 `source` 两行整块注释。典型例子见 runGhdl.tcl 里被注释掉的 `psi_multi_stream_daq`（理由是「TB not GHDL compatible!」，详见 u2-l3）。

#### 4.2.3 源码精读

先看 **init 的三种写法**：

[scripts/runModelsim.tcl:11-12](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L11-L12) —— 默认（ModelSim）：

```tcl
#Initialize Simulation
init
```

[scripts/runGhdl.tcl:11-12](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runGhdl.tcl#L11-L12) —— 选用 GHDL：

```tcl
#Initialize Simulation
init -ghdl
```

[scripts/runVivado.tcl:13-14](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl#L13-L14) —— 选用 Vivado xsim：

```tcl
#Initialize Simulation
init -vivado
```

再看 **configure 阶段的逐库登记**，以 `runModelsim.tcl` 为标杆（它启用的库最全）。先是 `set myPath` 锚点与前几个 VHDL 库：

[scripts/runModelsim.tcl:14-24](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L14-L24) —— 锚定起点后，依次登记 `psi_common`、`psi_fix`、`psi_multi_stream_daq`：

```tcl
#Configure
set myPath [pwd]

cd $myPath/../VHDL/psi_common/sim
source config.tcl

cd $myPath/../VHDL/psi_fix/sim
source config.tcl

cd $myPath/../VHDL/psi_multi_stream_daq/sim
source config.tcl
```

之后继续登记一批 `VivadoIp/` 下的库（`vivadoIP_axis_data_gen`、`vivadoIP_clock_measure`、`vivadoIP_data_rec`、`vivadoIP_mem_test`、`vivadoIP_spi_simple`、`vivadoIP_i2c_devreg`、`vivadoIP_axi_mm_reader`），写法完全一致，这里不重复贴。

值得专门看的是 **被注释跳过的 power_sink**：

[scripts/runModelsim.tcl:47-48](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L47-L48) —— power_sink 没有 self-checking TB，所以整块注释、不参与仿真：

```tcl
#cd $myPath/../VivadoIp/vivadoIP_power_sink/sim
#Does not have a self-checking TB because power consumption/toggling/optimization cannot be simulated!
```

注释解释了原因：功耗（power consumption）、信号翻转率（toggling）、综合后的功耗优化都无法在 RTL 仿真里体现，所以 power_sink 没法做自检 testbench。**三个脚本都把 power_sink 注释掉**，因此它在任何仿真器下都不跑（详见 u2-l3）。

最后，configure 阶段收尾：回到起点目录，为下一阶段做准备：

[scripts/runModelsim.tcl:50](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L50) —— `cd $myPath` 回到 `scripts/`。

对比看一下 **GHDL 里「跳过某库」的实际写法**：

[scripts/runGhdl.tcl:23-25](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runGhdl.tcl#L23-L25) —— `psi_multi_stream_daq` 的 TB 不兼容 GHDL，整块注释：

```tcl
#TB not GHDL compatible!
#cd $myPath/../VHDL/psi_multi_stream_daq/sim
#source config.tcl
```

这就是「同一个模板，靠注释差异适配不同仿真器」的具体体现。

> **一处需要诚实指出的源码细节**（`runVivado.tcl`）：在该脚本末尾的 `vivadoIP_i2c_devreg` 处，[scripts/runVivado.tcl:50-52](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl#L50-L52) 显示——`cd` 那一行被注释了，但紧随其后的 `source config.tcl` **并没有**被注释。这意味着该 `source` 会在「上一个仍有效的目录」下执行（即 `VHDL/psi_common/sim`），而不是 `vivadoIP_i2c_devreg/sim`。这看起来像是维护时漏改的一处，本讲如实记录，不替它「修正」——你在阅读真实源码时应保留这种警觉。其确切后果取决于 `config.tcl` 内部实现，**待确认**。

#### 4.2.4 代码实践

1. **实践目标**：亲手比对三个脚本，看懂「init 参数差异」与「跳过库的注释写法」。
2. **操作步骤**：
   - 打开三个脚本的 init 行：[runModelsim.tcl:12](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L12)、[runGhdl.tcl:12](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runGhdl.tcl#L12)、[runVivado.tcl:14](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl#L14)。
   - 在 runGhdl.tcl 与 runVivado.tcl 里各找一处「用 `#TB not ... compatible!` 注释跳过某库」的代码块（例如 [runGhdl.tcl:23-25](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runGhdl.tcl#L23-L25)）。
   - 在 runVivado.tcl 里定位 4.2.3 末尾提到的那处 `i2c_devreg` 异常（[runVivado.tcl:50-52](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl#L50-L52)）。
3. **需要观察的现象**：init 参数随仿真器变化；跳过库用 `#` 整块注释；runVivado 里有一处 `cd`/`source` 注释不对称。
4. **预期结果**：你能用自己的话讲清「init 参数怎么选」「怎么跳过一个库」，并且能指出 runVivado 里那处疑似漏改的异常。
5. 该实践仅阅读源码，不运行，**无运行结果待确认问题**。

#### 4.2.5 小练习与答案

**练习 1**：`set myPath [pwd]` 这一句为什么不能省？省了会怎样？

> **答案**：它把执行起点（`scripts/`）记进 `myPath`，使所有库路径都能写成相对形式 `$myPath/../<类>/<lib>/sim`，并在最后用 `cd $myPath` 回起点。若省略，脚本就只能依赖「当前 pwd 恰好是 scripts/」这一隐含前提，且无法在 configure 结束后回到起点，鲁棒性变差。

**练习 2**：要让某一款仿真器「跳过」一个不兼容的库，脚本作者采用的办法是什么？

> **答案**：用 `#` 把该库对应的 `cd ...` 和 `source config.tcl` 两行整块注释掉，通常还附一行 `#TB not <simulator> compatible!` 说明原因。这是同一份模板适配多仿真器的关键手法。

---

### 4.3 三段执行：compile / run / check

#### 4.3.1 概念说明

configure 完成后，PsiSim 已经知道「这一轮要仿真哪些库、哪些 testbench」。接下来是真正干活的三个命令，构成流水线的最后三步：

1. **compile（编译）**：`compile_files -all -clean`，把所有登记过的 VHDL 源文件编译一遍。
   - `-all`：处理全部已登记的库，而不是某一个。
   - `-clean`：编译前先清理上一轮的编译产物，保证干净重建（避免旧目标文件干扰）。
2. **run（运行）**：`run_tb -all`，运行全部登记过的 testbench。
   - `-all`：跑所有 TB，而不是某一个。
3. **check（检查）**：`run_check_errors "###ERROR###"`，扫描仿真输出日志，若出现字符串 `###ERROR###` 则判定有 testbench 自检失败。

`###ERROR###` 是 PSI 各 testbench 约定的**自检失败标志**：self-checking TB 在内部断言失败时会主动打印这个字符串。`run_check_errors` 就是靠它来把「仿真跑完」和「仿真通过」区分开——仿真不出错地跑完 ≠ 设计正确，只有日志里不出现 `###ERROR###` 才算通过。这就是这套流水线能做「批量回归测试」的根本机制。

三个 `puts "----"` 只是打印分节标题，让人在控制台里一眼看出当前走到哪一步，没有功能作用。

> 说明：`compile_files` / `run_tb` / `run_check_errors` 的内部实现位于 `PsiSim.tcl`（submodule 内），本仓库不含，**待确认**。本讲只依据它们的命名、参数与调用位置解释其作用。

#### 4.3.2 核心流程

```
cd $myPath                       # configure 结束后回到起点

puts "-- Compile"                #（仅打印分节标题）
compile_files -all -clean        # [3] 干净地编译全部已登记源文件

puts "-- Run"
run_tb -all                      # [4] 运行全部 testbench

puts "-- Check"
run_check_errors "###ERROR###"   # [5] 扫描日志，若出现 ###ERROR### 则报错
```

> **为什么 compile / run / check 要分开？** 因为编译、执行、判定三件事失败原因不同：编译失败通常是语法/接口问题；运行失败可能是仿真崩溃；只有 check 失败才说明「设计行为不符合 TB 预期」。把它们拆成三步、并打印分节标题，便于在长日志里快速定位问题出在哪一段。

#### 4.3.3 源码精读

三个脚本的三段执行几乎完全一致。以 `runModelsim.tcl` 为例：

[scripts/runModelsim.tcl:52-65](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L52-L65) —— compile → run → check 三段：

```tcl
#Run Simulation
puts "------------------------------"
puts "-- Compile"
puts "------------------------------"
compile_files -all -clean
puts "------------------------------"
puts "-- Run"
puts "------------------------------"
run_tb -all
puts "------------------------------"
puts "-- Check"
puts "------------------------------"

run_check_errors "###ERROR###"
```

`runGhdl.tcl` 的对应段落在 [scripts/runGhdl.tcl:54-67](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runGhdl.tcl#L54-L67)，`runVivado.tcl` 在 [scripts/runVivado.tcl:59-72](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl#L59-L72)——三段命令与分节标题完全相同。这正是它们「同骨架」的最直接体现：只有 configure 阶段（启用了哪些库）和 init 参数随仿真器不同，真正的执行段是共享的。

> 注意：`run_check_errors "###ERROR###"` 接收的是一个**字符串参数**（错误标志）。理论上如果某项目用了别的错误标志，改这个参数即可——但 PSI 各 TB 约定统一使用 `###ERROR###`，所以三个脚本都传同一个值。

#### 4.3.4 代码实践

1. **实践目标**：确认三个脚本的「执行段」字面一致，并理解 `###ERROR###` 的判定逻辑。
2. **操作步骤**：
   - 并排打开 [runModelsim.tcl:52-65](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L52-L65)、[runGhdl.tcl:54-67](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runGhdl.tcl#L54-L67)、[runVivado.tcl:59-72](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl#L59-L72)。
   - 用 diff 思维比对这三段（它们应当一致）。
   - 想象一个 testbench 在断言失败时打印了 `###ERROR### you_expected_5_but_got_3`，思考 `run_check_errors` 会怎么反应。
3. **需要观察的现象**：三段命令逐字相同；错误标志是字符串字面量 `"###ERROR###"`。
4. **预期结果**：你应当能解释——只要日志里出现子串 `###ERROR###`，`run_check_errors` 就会把它当作失败上报；所以 PSI 的 self-checking TB 在自检不过时统一打印这个标志。
5. 若想本地实跑验证（需要已克隆全部 submodule 并装好某款仿真器），可在 Vivado Tcl Console 里 `source scripts/runVivado.tcl`，观察控制台是否按 `-- Compile / -- Run / -- Check` 三节输出，以及最后一节是否报 `###ERROR###`。本仓库 CI 未执行该流程，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`compile_files -all -clean` 里的 `-clean` 起什么作用？为什么需要它？

> **答案**：`-clean` 在编译前先清理上一轮的编译产物（如旧的目标/临时文件），保证每次都是干净重建，避免「改了源码但旧产物残留」导致的假通过/假失败。其精确实现位于 `PsiSim.tcl`，**待确认**，但语义与命名一致。

**练习 2**：`run_check_errors "###ERROR###"` 是怎么判断仿真「失败」的？仿真「跑完没崩」是否就等于「通过」？

> **答案**：不等于。`run_check_errors` 扫描仿真输出日志，只要出现字符串 `###ERROR###` 就判定失败——这是 self-checking TB 在内部断言失败时主动打印的标志。仿真「跑完没崩」只说明流程走通，只有日志里**不出现** `###ERROR###` 才算设计行为通过校验。

**练习 3**：三段执行之间穿插的 `puts "----"` / `puts "-- Compile"` 等语句有什么作用？

> **答案**：纯粹是给人看的分节标题，让长长的仿真日志里一眼能区分 Compile / Run / Check 三段，便于定位问题。它们没有功能影响，删掉不影响仿真结果。

---

## 5. 综合实践

把本讲内容串起来，完成下面这个贯穿性任务。

### 任务一：把 runModelsim.tcl 拆成五段并逐段注释

打开 [scripts/runModelsim.tcl](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl)，按下表把它拆成「init → configure → compile → run → check」五段（框架加载作为前置），并在每一段旁边写一句中文说明它做什么：

| 段 | 对应行（参考） | 你要写的中文注释 |
|---|---|---|
| 前置·加载框架 | 第 7-9 行 | （示例）加载 PsiSim 框架并导入 `psi::sim::*` 命令 |
| 1. init | 第 11-12 行 | |
| 2. configure | 第 14-50 行（含被注释的 power_sink） | |
| 3. compile | 第 52-56 行 | |
| 4. run | 第 57-60 行 | |
| 5. check | 第 61-65 行 | |

> 参考答案要点：init 段「调用 `init` 选定默认 ModelSim 后端」；configure 段「`set myPath` 锚定起点后，逐库 `cd` 到 `<lib>/sim` 并 `source config.tcl`，power_sink 因无 self-checking TB 被注释」；compile 段「`compile_files -all -clean` 干净编译全部已登记源文件」；run 段「`run_tb -all` 运行全部 testbench」；check 段「`run_check_errors "###ERROR###"` 扫描日志、依据 `###ERROR###` 标志判定失败」。

### 任务二：仿照模板，写一个 runXxx.tcl

**实践目标**：在 configure 阶段额外加入一个当前三个脚本都未列出的库，其余保持与 runModelsim.tcl 一致。

**操作步骤**：

1. 复制一份 [scripts/runModelsim.tcl](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl)，命名为 `runXxx.tcl`（**示例代码**，仅用于练习，不提交到仓库）。
2. 在 configure 阶段（参照第 17-24 行的写法）**额外**加入一个当前未列出的库。这里以 `psi_tb`（PSI 的 testbench 辅助库，属 VHDL 类，见 u1-l3）为例，按现有相对路径约定插入：

```tcl
# 示例代码：仿照 runModelsim.tcl 第 17-18 行的写法新增 psi_tb
cd $myPath/../VHDL/psi_tb/sim
source config.tcl
```

3. 其余部分（init、compile、run、check）保持与 runModelsim.tcl 完全一致。
4. 自检：确认新加的 `cd` 与 `source` 成对出现、路径前缀是 `$myPath/../VHDL/psi_tb/sim`、且没有破坏最后的 `cd $myPath` 收尾。

**需要观察的现象**：
- 新脚本结构与 runModelsim.tcl 完全平行，只在 configure 段多了一个库。
- 路径严格遵守「`$myPath/../<类>/<lib>/sim` + `source config.tcl`」的约定，对应 u2-l1 讲的「`cd`+`source` 成对、`cd` 到约定工作目录」模板。

**预期结果**：
- 你产出的 `runXxx.tcl` 能在已克隆全部 submodule 的环境里被 ModelSim Tcl 控制台 `source` 执行，多仿真一个 `psi_tb` 库，其余行为不变。
- 因为 `PsiSim.tcl` 与 `VHDL/psi_tb/sim/config.tcl` 都在对应 submodule 内（本仓库不含），该库是否真有可用的 `sim/config.tcl`、其登记内容如何，**待确认**；若该库当前没有 self-checking TB 或 config.tcl，`source` 会在运行时报错——这是运行期事实，**待本地验证**。

---

## 6. 本讲小结

- 三个仿真驱动脚本（`runModelsim.tcl` / `runGhdl.tcl` / `runVivado.tcl`）共享同一条流水线：**加载框架 → init → configure → compile → run → check**。
- 框架加载是两步：`source ../TCL/PsiSim/PsiSim.tcl` 定义命令，`namespace import psi::sim::*` 让命令可裸名调用；Vivado 版多了 `-quiet`。
- `init` 用参数选仿真器后端：默认（ModelSim）/ `-ghdl` / `-vivado`。
- configure 阶段靠 `set myPath [pwd]` 锚定起点，逐库 `cd` 到 `<lib>/sim` 再 `source config.tcl`；跳过一个库 = 把 `cd`+`source` 整块注释。
- 三段执行 `compile_files -all -clean` → `run_tb -all` → `run_check_errors "###ERROR###"` 在三个脚本里几乎逐字相同；`###ERROR###` 是 self-checking TB 的自检失败标志。
- 三个脚本真正的差异只在两处：**init 参数** 与 **configure 阶段启用了哪些库**（兼容性矩阵详见 u2-l3）；本讲还如实记录了 runVivado.tcl 里 `vivadoIP_i2c_devreg` 处 `cd`/`source` 注释不对称的一处源码细节。

---

## 7. 下一步学习建议

- **下一讲 u2-l3（仿真器兼容性矩阵）**：本讲多次提到「某库在某仿真器下被注释跳过」，下一讲会专门把「库 × 仿真器」整理成一张完整矩阵，并解释 power_sink 为何在三种仿真器下都不跑。建议紧接着学。
- **u2-l4（Vivado IP 批量打包）**：对比 `packageAllIp.tcl`，你会发现它和本讲的仿真脚本用的是**同一套** `myPath`/`cd`/`source`/`cd` 回模板，只是 source 的是各库的 `package.tcl` 而非 `config.tcl`，且没有 init/compile/run/check 流程。读完本讲再看 u2-l4，会更深体会「驱动脚本只做编排」的设计。
- **深入 PsiSim 本体**：若想真正理解 `init` / `compile_files` / `run_tb` / `run_check_errors` 的内部实现，需要进入 `TCL/PsiSim` submodule 阅读其 `PsiSim.tcl`（本仓库不含，需 `git submodule update --init` 检出后阅读）。

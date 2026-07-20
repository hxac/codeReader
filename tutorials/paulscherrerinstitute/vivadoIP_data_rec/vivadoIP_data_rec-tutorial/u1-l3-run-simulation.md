# 在仿真中跑起来：PsiSim/Modelsim 回归测试

## 1. 本讲目标

读完上一讲，我们已经知道本仓库的代码放在哪里、依赖哪些 PSI FPGA 库。本讲要回答下一个问题：**这些 VHDL 代码写完之后，怎么验证它是对的？**

PSI 的答案是「回归仿真（regression simulation）」：用一套统一的 TCL 框架把所有源码、测试平台组织起来，一键编译、一键运行所有测试用例、一键判定成功还是失败。本讲学完后你应当能够：

1. 在 `sim/` 目录下用 `source ./run.tcl` 跑起完整的回归仿真，并知道它依次做了哪几件事。
2. 读懂 `sim/config.tcl` 是如何把「外部依赖库 / 项目源码 / 测试平台」分门别类组织进一个仿真库的。
3. 说清楚 `scripts/ciFlow.py` 是如何读 Modelsim 的 transcript（日志）、用三个不同的退出码区分「测试断言失败」「仿真异常中断」「全部通过」的。

本讲只聚焦**仿真流程**本身，不展开讲测试平台内部怎么写激励、怎么校验数据——那是后续 u6 单元（验证体系）的内容。本讲是把「跑起来」这件事彻底讲透。

## 2. 前置知识

在进入源码之前，先建立几个本讲会反复用到的概念。

**仿真器与命令行模式**
Modelsim / QuestaSim 是常用的 VHDL 仿真器。它既可以打开图形界面（GUI）用鼠标点，也可以用 `vsim -c` 以**命令行模式（batch mode）**启动：没有窗口、读 TCL 脚本、把所有输出写到一个叫 transcript 的日志文件里。CI（持续集成）环境通常没有图形界面，所以一定走 `-c` 模式。

**TCL**
TCL（Tool Command Language）是一种脚本语言，Modelsim / Vivado 都用 TCL 作为内嵌脚本接口。本讲的 `run.tcl`、`config.tcl`、`ci.do` 全是 TCL 脚本。

**测试平台（testbench, TB）**
testbench 是一段「不用被综合成硬件、只为仿真而存在」的 VHDL 代码。它给被测设计（Design Under Test, DUT）施加激励（时钟、复位、数据、触发），然后检查 DUT 的输出对不对。本项目的 DUT 是 `data_rec_vivado_wrp`，测试平台在 `testbench/top_tb/` 下，共有 6 个用例（case0 ~ case5）。

**回归测试（regression test）**
「回归」的意思是：每次改完代码，都把这一整套测试**重新跑一遍**，确保新的改动没有破坏原本正确的功能（没有「回归」到坏的版本）。本项目把这整套回归测试做成了「一条命令」。

**两的幂（power of two）与非两的幂**
\[ 2^n \quad (n=0,1,2,\dots) \]
形如 1、2、4、8、16、32、64…… 的数叫「两的幂」。在硬件里，用两的幂作存储深度时，地址回绕（绕回起点）只需截断高位，极其简单；而非两的幂（比如 30）则需要显式做减法判断借位，容易出 bug。本项目 v2.3.2 刚好修了一个「非两的幂存储深度回绕」的 bug，所以仿真里特意用了一个非两的幂深度（30）来覆盖这条路径。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `sim/run.tcl` | **批处理入口**：加载 PsiSim 框架、读配置、编译、跑 TB、查错误 | 三段式流程 |
| `sim/config.tcl` | **仿真配置**：声明仿真库、把源码按 tag 分组、定义 TB 运行 | 库 / 源码 / TB 的组织 |
| `sim/ci.do` | **CI 执行脚本**：执行 `run.tcl` 后退出 Modelsim | 给 `vsim -c` 用的薄包装 |
| `sim/interactive.tcl` | **交互式入口**：只搭好环境并编译，把仿真器留给用户调试 | 与批处理的差异 |
| `scripts/ciFlow.py` | **CI 判定逻辑**：拉起 Modelsim、解析日志、给出退出码 | 三个退出码的含义 |

此外会顺带提到：

| 文件 | 作用 |
|---|---|
| `README.md` | 告诉用户「在 `sim/` 目录执行 `source ./run.tcl`」 |
| `testbench/top_tb/top_tb.vhd` | 顶层测试平台，串行调用 case0~case5 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 PsiSim 框架（`psi::sim::*`）** —— 一套封装了「编译 / 运行 / 查错」的 TCL 框架，是整个流程的引擎。
- **4.2 仿真配置（`sim/config.tcl`）** —— 告诉框架「要编译哪些文件、放进哪个库、跑哪些 TB」。
- **4.3 CI 流程（`run.tcl` / `ci.do` / `ciFlow.py`）** —— 把前两者串成一条命令，并用退出码判定成败。

### 4.1 PsiSim 框架（psi::sim::*）

#### 4.1.1 概念说明

PsiSim 是 PSI 开发的一个**与具体仿真器无关的 TCL 仿真框架**（依赖见 README：`TCL/PsiSim`，2.1.0 或更高，仅开发期需要）。它的核心思想是：

> 用户用一套统一的 `psi::sim::*` 命令描述「我要编译什么、跑什么」，PsiSim 在底层把这些命令翻译成 Modelsim（或其它仿真器）的具体命令。

这样做的好处是：换仿真器时只改 PsiSim 的后端，项目的 `config.tcl` / `run.tcl` 不用动。本讲你不需要读 PsiSim 的源码（它是外部仓库 `TCL/PsiSim/PsiSim.tcl`），只需要记住它对外暴露的几个高层命令：

| 命令 | 作用 |
|---|---|
| `psi::sim::init` | 初始化仿真环境（清状态、设变量） |
| `psi::sim::add_library <名字>` | 声明一个仿真库（VHDL library） |
| `psi::sim::add_sources <路径> {文件列表} -tag <标签>` | 把一组源码加入编译列表，并打标签分组 |
| `psi::sim::compile -all -clean` | 先清理再编译全部已声明的源码 |
| `psi::sim::create_tb_run <tb名>` | 开始定义一次 TB 运行 |
| `psi::sim::tb_run_add_arguments <...>` | 给这次运行附加 generic 参数 |
| `psi::sim::add_tb_run` | 提交这次 TB 运行 |
| `psi::sim::run_tb -all` | 运行所有已定义的 TB 运行 |
| `psi::sim::run_check_errors <标记>` | 扫描日志，若出现该标记则报错 |

#### 4.1.2 核心流程

一次完整的 PsiSim 批处理流程是固定五步，可以画成下面这条流水：

```
┌─────────┐  ┌────────────┐  ┌──────────┐  ┌────────┐  ┌──────────┐
│  init   │→ │ configure  │→ │ compile  │→ │ run_tb │→ │ check_err│
│ 初始化  │  │ (config.tcl)│  │ -all-clean│  │ -all   │  │ ###ERROR │
└─────────┘  └────────────┘  └──────────┘  └────────┘  └──────────┘
```

伪代码描述：

```
source PsiSim.tcl          # 加载框架（拿到 psi::sim::* 命令）
psi::sim::init             # 1. 初始化
source ./config.tcl        # 2. 配置：声明库、加源码、定义 TB 运行
psi::sim::compile -all -clean  # 3. 编译
psi::sim::run_tb -all      # 4. 运行所有 TB
psi::sim::run_check_errors "###ERROR###"  # 5. 扫描日志查错
```

注意第 5 步：PsiSim **不会自己去判断 VHDL 逻辑对错**。它做的是一件很朴素的事——扫描整个 transcript，只要里面出现了字符串 `###ERROR###`，就认为「有错」。那 `###ERROR###` 是谁打出来的？是测试平台侧的断言工具（`psi_tb` 库里的 `axi_single_expect` 等过程）：当期望值和实际值对不上时，它就往日志里写一行带 `###ERROR###` 的报告。所以 PsiSim 的「查错」本质是「按约定标记搜日志」。

#### 4.1.3 源码精读

PsiSim 框架本身是外部依赖，但 `run.tcl` 就是它的「使用说明书」，逐行读 `run.tcl` 就能看到上面那五步：

框架加载与初始化——[sim/run.tcl:8-11](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/run.tcl#L8-L11)：先 `source` 进 PsiSim.tcl 拿到 `psi::sim` 命名空间，再 `init`：

```tcl
source ../../../TCL/PsiSim/PsiSim.tcl
psi::sim::init
```

这里的相对路径 `../../../TCL/PsiSim/` 正好印证了上一讲讲的依赖文件夹结构：从 `sim/` 往上跳三层回到仓库根目录的同级，再进入 `TCL/PsiSim`。

读配置——[sim/run.tcl:14](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/run.tcl#L14)：

```tcl
source ./config.tcl
```

编译——[sim/run.tcl:20](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/run.tcl#L20)：`-clean` 表示先清理上次的编译产物，`-all` 表示编译全部已声明源码：

```tcl
psi::sim::compile -all -clean
```

运行全部 TB——[sim/run.tcl:24](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/run.tcl#L24)：

```tcl
psi::sim::run_tb -all
```

扫描日志查错——[sim/run.tcl:29](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/run.tcl#L29)：把 `###ERROR###` 这个标记交给框架去搜：

```tcl
psi::sim::run_check_errors "###ERROR###"
```

#### 4.1.4 代码实践

**实践目标**：确认 PsiSim 框架的加载路径与上一讲讲的目录约定一致。

**操作步骤**：

1. 在仓库根目录用 `git ls-files sim/ scripts/` 列出仿真相关脚本（你会看到 `run.tcl`、`config.tcl`、`ci.do`、`interactive.tcl`）。
2. 打开 `sim/run.tcl`，找到 `source ../../../TCL/PsiSim/PsiSim.tcl` 这一行。
3. 用上一讲的目录树推算：`sim/` 上跳一层是仓库根 `vivadoIP_data_rec/`，再上跳两层到达 `<根目录>/VivadoIp/` 的上一层，于是 `../../../TCL/PsiSim/` 指向的就是与 `VivadoIp/` 平级的 `TCL/PsiSim/` 目录。

**需要观察的现象**：路径里的「上跳 3 层」与上一讲 `component.xml`、`package.tcl` 里的相对路径约定是同一套。

**预期结果**：PsiSim 必须放在 `<根目录>/TCL/PsiSim/`，否则 `source` 这一行会找不到文件、`run.tcl` 第 8 行直接报错。这也再次说明：**README 里的依赖清单就是目录结构的真相源**。

> 说明：本实践是源码阅读型，不需要 Modelsim 即可完成。

#### 4.1.5 小练习与答案

**练习 1**：如果把 PsiSim 的版本降到 2.1.0 以下，会发生什么？
**答**：README 明确要求 PsiSim「2.1.0 or higher」。版本过低时 `psi::sim::*` 的某些命令（如 `run_check_errors` 的行为）可能与脚本预期不一致，轻则告警，重则脚本在第 29 行报错或漏掉错误检测。应保持 ≥ 2.1.0。

**练习 2**：`run_check_errors "###ERROR###"` 为什么要把标记字符串当作参数传进去，而不是写死在框架里？
**答**：把标记参数化，不同的项目/测试套件就可以用不同的错误标记约定；框架本身保持通用，只负责「搜字符串」。本项目选择 `###ERROR###` 作为自己的错误标记。

### 4.2 仿真配置（sim/config.tcl）

#### 4.2.1 概念说明

`config.tcl` 是整个仿真的「配料表」：它告诉 PsiSim **把哪些文件编译进哪个库、分几组、跑哪些 TB、用什么参数**。理解它，就理解了本项目仿真到底覆盖了什么。

它的内容可以分成四块：

1. **库声明**：建一个名叫 `data_rec` 的仿真库。
2. **依赖库源码（tag = lib）**：编译 `psi_common` 和 `psi_tb` 里被实际用到的那些文件。
3. **项目源码（tag = src）**：编译 `hdl/` 下的三个核心 RTL 文件。
4. **测试平台（tag = tb）**：编译 `testbench/` 下的公共包与 6 个用例包及顶层 TB。
5. **TB 运行定义**：声明要跑 `top_tb`，并给定 `MemoryDepth_g` 的两组取值。

「tag（标签）」的作用是给源码分组，方便框架按类别处理（例如只重编译某一组）。

#### 4.2.2 核心流程

config.tcl 的组织逻辑可以用一张分层图表达：

```
仿真库: data_rec
│
├─ [tag lib]  依赖库源码
│   ├─ psi_common/hdl:  array_pkg, math_pkg, logic_pkg,
│   │                   pulse_cc, simple_cc, status_cc,
│   │                   tdp_ram, pl_stage, axi_slave_ipif
│   └─ psi_tb/hdl:      txt_util, compare_pkg, axi_pkg
│
├─ [tag src]  项目源码 (../hdl)
│   ├─ data_rec_register_pkg.vhd
│   ├─ data_rec.vhd
│   └─ data_rec_vivado_wrp.vhd
│
└─ [tag tb]   测试平台 (../testbench)
    ├─ top_tb_pkg.vhd            公共过程
    ├─ top_tb_case0~5_pkg.vhd    六个用例
    └─ top_tb.vhd                顶层 TB（串行调用六个用例）

TB 运行:
    top_tb  ×  {MemoryDepth_g=32, MemoryDepth_g=30}
```

最值得注意的一点在最后：**`top_tb` 会被以两组不同的 `MemoryDepth_g` 运行**——`32`（两的幂）和 `30`（非两的幂）。这不是巧合，而是为了同时覆盖「两的幂」与「非两的幂」两条存储深度代码路径。结合 Changelog 里 v2.3.2「修复非两的幂 `MemoryDepth_g` 回绕 bug」的记录，就能理解为什么必须专门跑一次 `30`。

#### 4.2.3 源码精读

库路径常量与仿真库声明——[sim/config.tcl:8-11](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/config.tcl#L8-L11)：`LibPath` 指向仓库根的同级目录（上跳三层），用于定位 `psi_common` / `psi_tb`：

```tcl
set LibPath "../../.."
psi::sim::add_library data_rec
```

抑制无关警告——[sim/config.tcl:14-15](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/config.tcl#L14-L15)：把一些已知的、与功能无关的编译/运行告警屏蔽掉，让日志更干净（数字是 Modelsim 的告警编号）：

```tcl
psi::sim::compile_suppress 135,1236
psi::sim::run_suppress 8684,3479,3813,8009,3812
```

依赖库源码（psi_common）——[sim/config.tcl:18-28](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/config.tcl#L18-L28)：注意这里**只编译了 9 个 psi_common 文件**，而不是整个 psi_common 仓库——只挑项目真正用到的（跨时钟域、双端口 RAM、流水线寄存器、AXI slave 等）：

```tcl
psi::sim::add_sources "$LibPath/VHDL/psi_common/hdl" {
    psi_common_array_pkg.vhd
    psi_common_math_pkg.vhd
    ...
    psi_common_axi_slave_ipif.vhd
} -tag lib
```

测试支持库（psi_tb）——[sim/config.tcl:31-35](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/config.tcl#L31-L35)：文本工具、比较、AXI 三个包，都是测试平台写激励和校验时用的：

```tcl
psi::sim::add_sources "$LibPath/VHDL/psi_tb/hdl" {
    psi_tb_txt_util.vhd
    psi_tb_compare_pkg.vhd
    psi_tb_axi_pkg.vhd
} -tag lib
```

项目源码——[sim/config.tcl:38-42](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/config.tcl#L38-L42)：上一讲提到的「核心 RTL 只有 3 个文件」在此得到证实：

```tcl
psi::sim::add_sources "../hdl" {
    data_rec_register_pkg.vhd
    data_rec.vhd
    data_rec_vivado_wrp.vhd
} -tag src
```

测试平台——[sim/config.tcl:45-54](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/config.tcl#L45-L54)：公共包 + 6 个用例包 + 顶层 TB：

```tcl
psi::sim::add_sources "../testbench" {
    top_tb/top_tb_pkg.vhd
    top_tb/top_tb_case0_pkg.vhd
    ...
    top_tb/top_tb_case5_pkg.vhd
    top_tb/top_tb.vhd
} -tag tb
```

TB 运行定义（两组深度）——[sim/config.tcl:57-60](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/config.tcl#L57-L60)：这是 config 里最关键的三行，声明跑 `top_tb`，并通过 `tb_run_add_arguments` 给出 `MemoryDepth_g` 的两组取值。PsiSim 会为每组参数各跑一次：

```tcl
psi::sim::create_tb_run "top_tb"
psi::sim::tb_run_add_arguments \
    "-gMemoryDepth_g=32" \
    "-gMemoryDepth_g=30"
psi::sim::add_tb_run
```

> 注意：`32` 与 `30` 之外，`top_tb` 的其它 generic（如 `MemoryDepth_g` 的默认值 32，见 [testbench/top_tb/top_tb.vhd:28-30](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb.vhd#L28-L30)）由实体声明里的默认值提供，`-g` 参数只是覆盖其中 `MemoryDepth_g`。

#### 4.2.4 代码实践

**实践目标**：验证「两组深度」的存在，并理解它们覆盖了哪两条代码路径。

**操作步骤**：

1. 打开 [sim/config.tcl:57-60](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/config.tcl#L57-L60)，确认 `tb_run_add_arguments` 里有两个 `-gMemoryDepth_g=` 条目。
2. 打开 `Changelog.md`，找到 v2.3.2「Fixed wrapping issue for non power-of-two `MemoryDepth_g`」一条。
3. 计算确认：\(32 = 2^5\) 是两的幂；\(30\) 不是两的幂（\(30 = 2 \times 3 \times 5\)）。

**需要观察的现象**：仿真配置里的「30」与 Changelog 里「非两的幂回绕 bug」直接对应。

**预期结果**：你能说清——`MemoryDepth_g=32` 跑的是「两的幂」简单回绕路径，`MemoryDepth_g=30` 跑的是「非两的幂」需借位判断的路径（后者正是 u3-l5 会专讲的 `NonPwr2MemDepth_c` 分支）。两组都必须通过，回归才算成功。

> 说明：本实践是源码阅读型，不需要 Modelsim。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `add_sources` 给 psi_common 也只列了 9 个文件，而不是把 `psi_common/hdl` 整个目录都编译进去？
**答**：只编译项目真正例化到的文件，可以加快编译、减少无关告警、并让依赖关系显式可见（看 config 就知道项目用了 psi_common 的哪些 IP）。这是一种良好的仿真工程管理习惯。

**练习 2**：如果有人在 `hdl/` 下新增了第 4 个 RTL 文件并被 `data_rec_vivado_wrp` 引用，但忘记加进 config.tcl 的 `-tag src` 列表，会发生什么？
**答**：该文件不会被编译进 `data_rec` 库，仿真时顶层引用它会报「找不到设计单元 / 未声明」的错误。所以**新增源码必须同步更新 config.tcl**。

### 4.3 CI 流程（run.tcl / ci.do / ciFlow.py）

#### 4.3.1 概念说明

有了 PsiSim（4.1）和 config.tcl（4.2），「跑一次仿真」对人是 `source ./run.tcl` 一条命令。但 CI 服务器还需要自动完成两件人事不会做的事：

1. **不开 GUI 地跑**：用 `vsim -c` 命令行模式启动 Modelsim。
2. **自动判定成败**：跑完之后给 shell 一个退出码（exit code），CI 系统（如 GitLab CI / GitHub Actions）据此决定这条流水线是「绿」还是「红」。

本模块的三个文件各司其职：

| 文件 | 角色 | 一句话 |
|---|---|---|
| `ci.do` | 「跑完就退」 | 给 `vsim -c` 用：source `run.tcl`，然后 `quit` |
| `run.tcl` | 真正的活儿 | init → config → compile → run → check |
| `ciFlow.py` | 「裁判」 | 拉起 vsim，读日志，给退出码 |

另外还有一个「人用的」入口 `interactive.tcl`：它只做 init + config + compile，**不 run**，把仿真器留在交互状态方便你单步调试。它与 `run.tcl` 的差别就在于「跑到哪一步停」。

#### 4.3.2 核心流程

CI 的端到端流程是从 `scripts/ciFlow.py` 这个 Python 脚本发起的：

```
scripts/ciFlow.py
   │
   │ 1) chdir 到 ../sim
   │ 2) os.system("vsim -c -do ci.do")
   ▼
vsim -c  ──执行──►  ci.do
                      │
                      │ source run.tcl   (init/config/compile/run/check)
                      │ quit
                      ▼
              写出 Transcript.transcript
                      │
   ┌──────────────────┘
   ▼
ciFlow.py 读取 Transcript.transcript，按规则给退出码：

   含 "###ERROR###"                        → exit(-1)   测试断言失败
   不含 "SIMULATIONS COMPLETED SUCCESSFULLY" → exit(-2)   仿真异常中断
   否则                                     → exit(0)    全部通过
```

这里最关键的是**三个退出码的判定规则**，下一节逐行讲。

#### 4.3.3 源码精读

**`ci.do`——给命令行用的薄包装**。全文只有两行有效内容，[sim/ci.do:7-8](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/ci.do#L7-L8)：

```tcl
source run.tcl
quit
```

`vsim -c -do ci.do` 的语义是「以命令行模式启动 vsim，然后执行 ci.do 里的命令」。所以 ci.do 就是「把人用的 run.tcl 跑一遍，跑完立刻退出 Modelsim」。

**`interactive.tcl`——调试用的入口**。[sim/interactive.tcl:11-19](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/interactive.tcl#L11-L19)：

```tcl
source ../../../TCL/PsiSim/PsiSim.tcl
namespace import psi::sim::*
init
source ./config.tcl
compile_files -all -clean
```

注意两点：①它 `namespace import psi::sim::*`，所以后面可以不带前缀直接写 `init`、`compile_files`（而 `run.tcl` 保留了 `psi::sim::` 前缀）；②它**只编译不运行**——没有 `run_tb`，也没有 `run_check_errors`。于是仿真器停在一个「已编译、可随时 `vsim work.top_tb` 手动加载波形」的状态，方便你调试单个用例。

**`ciFlow.py`——CI 裁判**。这是本模块的核心。先看它如何拉起仿真并读日志，[scripts/ciFlow.py:9-16](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/ciFlow.py#L9-L16)：

```python
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(THIS_DIR + "/../sim")          # 切到 sim 目录
os.system("vsim -c -do ci.do")          # 命令行跑 Modelsim
with open("Transcript.transcript") as f:
    content = f.read()                  # 读全部日志
```

注意 `os.chdir` 把工作目录切到了 `sim/`——这正是 README 要求「在 `sim` 目录里执行」的原因，`run.tcl` 里所有相对路径（`./config.tcl`、`../hdl`、`../../../TCL/PsiSim`）都以此为基准。

然后是三个退出码判定，[scripts/ciFlow.py:18-27](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/ciFlow.py#L18-L27)：

```python
#Expected Errors
if "###ERROR###" in content:
    exit(-1)
#Unexpected Errors
if "SIMULATIONS COMPLETED SUCCESSFULLY" not in content:
    exit(-2)
#Success
exit(0)
```

**如何区分「预期错误」与「意外错误」**——这正是本讲的实践任务要解释的点：

- **`exit(-1)`（注释 `Expected Errors`，预期错误）**：日志里出现了 `###ERROR###` 标记。这个标记是测试平台侧的断言工具（如 `psi_tb_axi_pkg` 的 `axi_single_expect`）在「期望值 ≠ 实际值」时主动打印的，并被 `psi::sim::run_check_errors "###ERROR###"` 捕获。也就是说，这条路径捕获的是**「按约定、被框架预期并能够检测到」的失败**——测试发现了一个功能错误，并如预期那样报了出来。在 CI 眼里，这属于「测试失败（FAIL）」。

- **`exit(-2)`（注释 `Unexpected Errors`，意外错误）**：日志里**没有** `###ERROR###`，但**也缺少** `SIMULATIONS COMPLETED SUCCESSFULLY` 这条成功横幅。PsiSim 在所有 TB 运行都正常跑完、且没发现错误时会打印这条横幅。它若缺席，说明仿真**根本没有正常走完**——可能是编译报错、VHDL 里 `severity failure` 的致命断言直接把仿真打停、或脚本中途异常退出。这类错误**没有留下约定的标记**，所以叫「意外错误」。

- **`exit(0)`（成功）**：日志里没有错误标记、且有成功横幅——所有用例、所有深度配置都通过了。

一句话总结这套判定的精妙之处：它不依赖任何「知道哪个用例该过」的业务逻辑，只靠两个字符串的有无就把「明确失败 / 异常崩溃 / 全部通过」三种状态干净地区分开。

> 补充：`Transcript.transcript` 是 Modelsim 的日志文件名，被 `sim/.gitignore` 用 `*.transcript` 规则忽略（见 [sim/.gitignore:13](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/.gitignore#L13)），所以每次本地跑出来的日志不会污染 git。

#### 4.3.4 代码实践

**实践目标**：在本地（或 CI）真正跑一次回归仿真，记录输出，并亲手解释 `ciFlow.py` 的三条判定。

**操作步骤**：

1. 确认依赖目录就位（参考上一讲）：`TCL/PsiSim`、`TCL/PsiIpPackage`、`VHDL/psi_common`、`VHDL/psi_tb`，本仓库在 `VivadoIp/vivadoIP_data_rec/`。也可用 `psi_fpga_all` 一键获得。
2. 在装有 Modelsim/QuestaSim 的机器上，进入 `sim/` 目录，在 Modelsim Tcl 控制台执行（README 给出的命令）：
   ```tcl
   source ./run.tcl
   ```
3. 观察终端依次打印 `-- Compile`、`-- Run`、`-- Check` 三段，以及 top_tb 在 `MemoryDepth_g=32` 与 `=30` 两组配置下的运行情况。
4. （可选，CI 方式）从仓库根目录运行：
   ```bash
   python scripts/ciFlow.py
   echo $?    # 打印退出码
   ```

**需要观察的现象**：

- 终端会逐个跑出 case0 ~ case5 的执行痕迹（顶层 TB 在 [testbench/top_tb/top_tb.vhd:218-229](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb.vhd#L218-L229) 里按 0→1→2→3→4→5 顺序串行调用）。
- 全部通过时，日志末尾应出现 `SIMULATIONS COMPLETED SUCCESSFULLY` 横幅（由 PsiSim 框架打印）。

**预期结果**：

- 若全部用例、两组深度都通过：`python scripts/ciFlow.py` 退出码为 `0`。
- 若某个 `axi_single_expect` 比对失败：日志含 `###ERROR###`，退出码为 `255`（即 `-1` 在 shell 里的表现）。
- 若编译失败或仿真崩溃：日志既无 `###ERROR###` 也无成功横幅，退出码为 `254`（即 `-2`）。

> 待本地验证：具体的横幅文案、case 打印格式以及 shell 下负退出码的显示值，取决于你本地的 Modelsim 版本与 shell，请以实际 transcript 为准。本讲不假设你已经跑过。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ciFlow.py` 要先检查 `###ERROR###`（exit −1），再检查「缺成功横幅」（exit −2），顺序不能反过来？
**答**：因为「断言失败」和「仿真没跑完」可能同时发生——比如某个用例断言失败后，仿真继续跑但中途又被一个致命错误打断，导致最终也没打印成功横幅。把 `###ERROR###` 判断放前面，可以优先把「测试发现的功能错误」这条最具体的原因报出来（exit −1），而不是把它笼统归为「异常中断」（exit −2）。先报最具体、最可定位的错误。

**练习 2**：`run.tcl` 和 `interactive.tcl` 都加载了 PsiSim 并 source 了 config.tcl，它们对仿真器的最终状态有什么不同？
**答**：`run.tcl` 跑完「编译 + 运行 + 查错」全套，适合回归测试与 CI；`interactive.tcl` 只做到「编译」，把仿真器留在交互状态，不运行 TB、不查错，方便开发者手动加载波形、单步调试某个用例。

**练习 3**：如果有人误把 `sim/.gitignore` 里的 `*.transcript` 规则删掉，`git status` 会多出哪些文件？
**答**：每次本地跑仿真生成的 `Transcript.transcript`（以及 `ciFlow.py` 读取的那个日志）会被 git 当作未跟踪文件显示出来，污染工作区状态。这正是该 ignore 规则存在的原因。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「当一次 CI」的小任务：

**背景**：假设你接手维护这个 IP，某天 CI 突然红了（失败）。你需要仅凭 `scripts/ciFlow.py` 给出的退出码和 `Transcript.transcript`，快速定位问题属于哪一类。

**任务**：

1. **画流程图**：在一张图上画出 `ciFlow.py → vsim -c → ci.do → run.tcl → {init, config.tcl, compile, run_tb, run_check_errors} → Transcript.transcript → ciFlow.py 判定` 的完整调用链，并标出每个文件负责的步骤。
2. **退出码分类表**：仿照下表，把三种退出码填全（含触发条件、典型成因、下一步排查方向）：

   | 退出码 | 名称 | 日志特征 | 典型成因 | 排查方向 |
   |---|---|---|---|---|
   | `0` | 成功 | 有成功横幅、无错误标记 | —— | —— |
   | `-1` | ? | ? | ? | ? |
   | `-2` | ? | ? | ? | ? |

3. **配置追踪**：打开 `sim/config.tcl`，数出 `add_sources` 一共被调用了几次、各自的 `-tag` 是什么，并解释为什么 `psi_common` 的源码路径要用 `$LibPath` 变量而不是写死的绝对路径。
4. **深度覆盖论证**：用一句话说明「为什么 `MemoryDepth_g=30` 这一组运行对回归测试不可或缺」，并指出它对应的 Changelog 版本号与后续哪一讲（提示：u3-l5）会深入这条代码路径。

> 待本地验证：如果你本地有 Modelsim，可在跑完 `source ./run.tcl` 后，故意把某个 `axi_single_expect` 的期望值改错（仅用于学习，记得改回），观察退出码是否从 `0` 变成 `-1`，并核对日志里是否真的出现了 `###ERROR###`。**不要提交这个破坏性修改。**

## 6. 本讲小结

- 本项目的回归仿真由 **PsiSim** 这个 TCL 框架驱动，`sim/run.tcl` 把流程固定为 `init → config → compile → run_tb → run_check_errors` 五步。
- `sim/config.tcl` 是「配料表」：把依赖库（psi_common/psi_tb）、项目 RTL（3 个文件）、测试平台（公共包 + 6 个用例）按 `-tag` 分组编译进 `data_rec` 库。
- `top_tb` 被以**两组 `MemoryDepth_g`（32 两的幂、30 非两的幂）**运行，专门覆盖两条存储深度代码路径，其中「30」对应 v2.3.2 修复的非两的幂回绕 bug。
- PsiSim 不判断逻辑对错，只按约定标记 `###ERROR###` 搜日志；该标记由测试平台的断言工具（如 `axi_single_expect`）在比对失败时打印。
- CI 由 `scripts/ciFlow.py` 发起：`vsim -c -do ci.do` → `ci.do` 跑 `run.tcl` 并退出 → 读 `Transcript.transcript`，用三个退出码区分**测试断言失败（−1）/ 仿真异常中断（−2）/ 全部通过（0）**。
- `interactive.tcl` 是调试入口，只 init + 编译、不运行，把仿真器留给开发者手动加载波形。

## 7. 下一步学习建议

本讲你已经能让整套仿真「跑起来」并看懂它的成败判定。接下来：

1. **进入第二单元（u2）**：学习顶层端口与寄存器地图（`data_rec_register_pkg`），这是后续阅读所有源码的「索引」。
2. **若你想先理解测试平台内部**：可以跳到 u6-l1（测试平台架构）与 u6-l2（六个用例的覆盖设计），看 case0~case5 各自在测什么——但建议先过 u2/u3 建立对 DUT 的认知，再回来看 TB 会更顺。
3. **动手延伸**：尝试在本讲综合实践里手画完整的 CI 调用链流程图，并试着回答「如果新增第 7 个用例，config.tcl 和 top_tb.vhd 各要改哪里」——这是走向 u6-l4（二次开发）的第一步。

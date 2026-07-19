# 仿真与回归测试框架

## 1. 本讲目标

psi_fix 的每一行可综合 VHDL 都必须能被「一键回归」自动验证。本讲带你读懂这一键回归背后那套 TCL 脚本。

学完后你应当能够：

- 说清 `sim/` 目录下 `config.tcl`、`run.tcl`、`runGhdl.tcl`、`ci.do` 各自的职责与调用关系。
- 看懂 `config.tcl` 里 `add_sources`（声明源码）、`create_tb_run`/`tb_run_add_arguments`（声明测试台与多组参数）、`tb_run_add_pre_script`（编译/运行前钩子）这几类命令的含义。
- 理解一次回归从「编译 → 跑全部测试台 → 扫描 `###ERROR###`」的完整链路，以及 `ciFlow.py` 如何据此判定 CI 通过/失败。
- 解释为什么 FIR 这类测试必须先跑 `preScript.py` 生成数据，而 `lut_gen` 的脚本又为什么必须在**编译之前**运行。

本讲只读脚本与文档，不改动任何源码，是 u1-l2 目录结构在「如何把库跑起来」这一维度上的延续。

## 2. 前置知识

阅读本讲前，建议你已经读过 u1-l1（项目定位）和 u1-l2（目录结构）。这里补充两个本讲会用到的概念：

- **回归测试（regression test）**：把库里所有测试台一次性全部跑一遍，任何一条失败就认为整次回归失败。它的价值在于：你改动了某个公共组件后，能立刻知道有没有「殃及池鱼」地破坏其他组件。
- **位真双模型（bittrue dual model）**：每个 VHDL 组件都配一个逐位一致的 Python 模型。测试台让 VHDL 跑出输出，再和 Python 模型预先算好的「黄金输出」逐位比对，不一致就报错。回归脚本并不理解信号含义，它只看日志里有没有出现错误标志字符串。

还需要知道一点工程背景：psi_fix 依赖一套**外部 TCL 框架 PsiSim**（随 `psi_fpga_all` 一起发布）。它不在本仓库里，而是放在仓库的上一级目录的 `TCL/PsiSim/` 下。所以本讲会反复看到 `source ../../../TCL/PsiSim/PsiSim.tcl` 这样的相对路径——这要求你按 u1-l1 介绍的「并排摆放（side-by-side）」目录结构来 checkout 各个仓库。

## 3. 本讲源码地图

本讲涉及的关键文件都集中在 `sim/` 目录，外加一份文档和一个 CI 脚本：

| 文件 | 作用 |
| --- | --- |
| `sim/config.tcl` | 回归的「声明式配置」：声明要编译哪些源、跑哪些测试台、每组用什么 generics、运行前调哪个脚本。是本讲的主角。 |
| `sim/run.tcl` | Modelsim 入口：加载 PsiSim → 读 config → 编译 → 跑全部 TB → 扫描错误。 |
| `sim/runGhdl.tcl` | GHDL 入口：与 `run.tcl` 几乎一致，只是用 `init -ghdl` 切到开源仿真器。 |
| `sim/interactive.tcl` / `sim/interactiveGhdl.tcl` | 交互式开发入口：只编译、不自动跑，留给开发者手动 `run_tb -contains xxx`。 |
| `sim/ci.do` | 给 Modelsim 用的 batch do 文件，`onerror {exit}` 后 `source run.tcl`，供 CI 调用。 |
| `doc/files/introduction.md` | 规定了 `###ERROR###` 错误标志约定、Modelsim/GHDL 两种跑法、贡献新测试台的要求。 |
| `scripts/ciFlow.py` | CI 总入口：调 `vsim -batch -do ci.do`，再解析 `Transcript.transcript` 判定通过/失败，最后跑 Python 单测。 |

另外会引用两个「被回归脚本调用的 Python 脚本」作为 pre_script 的真实样例：

| 文件 | 作用 |
| --- | --- |
| `testbench/psi_fix_fir_dec_ser_nch_chtdm_conf_tb/Scripts/preScript.py` | 典型的**数据生成型** pre_script：用位真 Python 模型算出 FIR 的输入、系数、期望输出，写成 `Data/*.txt`。 |
| `testbench/psi_fix_lut_gen_tb/Script/fir_design_test.py` | 典型的**代码生成型** pre_script：用 `psi_fix_lut.Generate` 直接生成一份 `.vhd` 文件，因此必须在编译前运行。 |

## 4. 核心概念与源码讲解

### 4.1 PsiSim 框架总览

#### 4.1.1 概念说明

PsiSim 是一套用 TCL 写的「仿真回归框架」，由 PSI 的 `psi_fpga_all` 仓库提供。它把「声明要编译/跑什么」和「具体用哪个仿真器跑」解耦：

- 你只要在 `config.tcl` 里**声明**源码清单和测试台清单；
- PsiSim 负责把它们喂给 Modelsim 或 GHDL，编译、运行、收集日志、扫描错误。

所有 PsiSim 命令都位于 `psi::sim::` 命名空间下，所以每个入口脚本开头都有一行 `namespace import psi::sim::*`，把 `init`、`add_sources`、`compile_files`、`run_tb`、`run_check_errors` 等命令直接引入当前作用域。

需要强调：**PsiSim 本身不在 psi_fix 仓库内**。`run.tcl` 第 8 行通过相对路径 `../../../TCL/PsiSim/PsiSim.tcl` 去加载它，这正是 u1-l1 强调的「并排摆放」目录结构的体现——如果目录摆得不对，这一行就会找不到文件。

#### 4.1.2 核心流程

一次完整的 Modelsim 回归，由 `run.tcl` 编排成五步：

```text
1. source PsiSim.tcl + namespace import   # 加载框架
2. init                                     # 初始化（Modelsim 模式）
3. source ./config.tcl                      # 声明源码与测试台
4. compile_files -all -clean                # 全量重新编译
5. run_tb -all                              # 跑全部测试台
6. run_check_errors "###ERROR###"           # 扫描日志里的错误标志
```

GHDL 路径完全一样，唯一差别是第 2 步用 `init -ghdl`。交互式开发则只做到第 4 步，把控制权留给开发者。

#### 4.1.3 源码精读

先看 Modelsim 入口 `run.tcl`，它短小精悍，几乎就是上面流程图的直译：

[sim/run.tcl:7-12](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/run.tcl#L7-L12) —— 加载外部 PsiSim 框架并导入其命名空间。注意 `../../../TCL/PsiSim/PsiSim.tcl` 是相对上一级目录的依赖路径。

[sim/run.tcl:14-21](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/run.tcl#L14-L21) —— 读入配置（`source ./config.tcl`）后，`compile_files -all -clean` 做一次干净的全量编译，`-clean` 表示先清掉旧编译产物。

[sim/run.tcl:23-30](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/run.tcl#L23-L30) —— `run_tb -all` 跑全部声明的测试台运行（TB run），最后 `run_check_errors "###ERROR###"` 在仿真日志里搜索错误标志字符串（详见 4.3 节）。

再看 GHDL 入口，除了第 6 行几乎完全相同：

[sim/runGhdl.tcl:6](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/runGhdl.tcl#L6) —— `init -ghdl` 把 PsiSim 切换到开源 GHDL 仿真器后端。`doc/files/introduction.md` 说明用 GHDL 时需要先安装 GHDL 并把它和一个 TCL 解释器（`tclsh`）加入 PATH，然后在 `tclsh` 里 `source ./runGhdl.tcl`。

交互式入口只编译、不跑，方便开发者反复调一个组件：

[sim/interactive.tcl:14-19](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/interactive.tcl#L14-L19) —— `init` 后 `source ./config.tcl` 再 `compile_files -all -clean`，把 PsiSim 命令留在 TCL 控制台，开发者随后可用 `compile_files -contains <串>`、`run_tb -contains <串>` 选择性重编重跑（见 `doc/files/introduction.md` 的 Working Interactively 一节）。

#### 4.1.4 代码实践

**实践目标**：在只读不跑的前提下，确认四种入口脚本的差异点，建立「同一个 config、多个入口」的直觉。

**操作步骤**：

1. 打开 `sim/run.tcl` 与 `sim/runGhdl.tcl`，用眼睛 diff 这两个文件，找出**唯一**的实质差别。
2. 打开 `sim/interactive.tcl`，对比它和 `run.tcl` 缺少了哪两步（`run_tb` 与 `run_check_errors`）。
3. 打开 `sim/ci.do`，看它如何把 `run.tcl` 包成一个可被 `vsim -batch -do` 调用的 batch 任务。

**需要观察的现象**：

- `run.tcl` 与 `runGhdl.tcl` 只在第 6 行的 `init`/`init -ghdl` 上不同。
- `interactive.tcl` 没有 `run_tb -all`，因为它把「跑哪个」的决定权交给开发者。

**预期结果**：你能用一句话说清「换仿真器只需改一个 `-ghdl` 标志」，并理解 `ci.do` 只是 `run.tcl` 外面套了一层 `onerror {exit}`（[sim/ci.do:7-9](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/ci.do#L7-L9)）。

> 本实践为纯源码阅读，无需安装任何仿真器即可完成。

#### 4.1.5 小练习与答案

**练习 1**：如果只想用 GHDL 交互式地调试某个组件，应该 `source` 哪个脚本？
**答案**：`sim/interactiveGhdl.tcl`。它用 `init -ghdl` 切到 GHDL，并且只编译不自动运行，随后在 TCL 控制台用 `run_tb -contains <组件名>` 单独跑。

**练习 2**：`run.tcl` 第 8 行的相对路径 `../../../TCL/PsiSim/PsiSim.tcl` 暗示了什么样的目录布局？
**答案**：psi_fix 仓库位于 `<Root>/VHDL/psi_fix`，而 PsiSim 框架位于 `<Root>/TCL/PsiSim`，两者共享同一个 `<Root>`（u1-l1 推荐命名为 `psi_lib`）。这正是「并排摆放」结构。

### 4.2 config.tcl 配置

#### 4.2.1 概念说明

`config.tcl` 是回归的**声明式配置文件**——它不包含任何仿真逻辑，只告诉 PsiSim「编译什么、跑什么、怎么跑」。它的内容可以分成三大块：

1. **编译清单**：用 `add_sources` 声明所有要编译的 VHDL 文件，并用 `-tag` 给它们分组（`lib`=外部库、`libtb`=外部库测试台、`src`=本项目源码、`tb`=本项目测试台）。
2. **测试台运行清单**：用 `create_tb_run` 声明一个测试台运行，用 `tb_run_add_arguments` 给它一组或多组 generics（多组 = 同一个测试台跑多次），最后 `add_tb_run` 把这次运行登记进回归。
3. **运行钩子**：用 `tb_run_add_pre_script` 在某次测试台运行前先执行一个脚本（通常是生成刺激数据的 Python 脚本）；用 `tb_run_add_time_limit` 给长测试设超时。

这份文件是「组件清单 + 测试矩阵」的单一事实来源：新增一个组件的测试台，只需在这里加几行。

#### 4.2.2 核心流程

一个测试台运行（TB run）的声明套路如下：

```tcl
create_tb_run "<tb_entity_name>"                    # 声明要跑哪个测试台实体
tb_run_add_pre_script "python3" "preScript.py" "<dir>"   # （可选）运行前先跑这个脚本
set dataDir [file normalize "<Data 目录绝对路径>"]       # 把相对路径转成仿真器能用的绝对路径
tb_run_add_arguments "<一组 generics>" \                   # 第 1 次运行的参数
            "<另一组 generics>"                             # 第 2 次运行的参数（同名 TB 跑两次）
add_tb_run                                           # 把这次运行登记进回归
```

关键点：

- `tb_run_add_arguments` 接收**可变个参数**，每个参数串对应**一次独立的仿真运行**。所以你常看到一个测试台用不同 `duty_cycle_g`、`ratio_g`、`ram_behavior_g` 连跑好几轮——这就是「参数矩阵」。
- 源码声明里，`psi_fix_pkg.vhd` 必须排在所有实体之前，因为每个实体都 `use work.psi_fix_pkg.all`（详见 u1-l2 的编译顺序纪律）。

#### 4.2.3 源码精读

**(a) 依赖路径与库设置**

[sim/config.tcl:8-14](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L8-L14) —— `LibPath` 设为 `"../.."`（即仓库根的上一级，那里并排摆着 en_cl_fix、psi_common、psi_tb 等依赖仓库）；`add_library psi_fix` 把所有文件编译进名为 `psi_fix` 的 VHDL 库。

[sim/config.tcl:16-18](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L16-L18) —— `compile_suppress`/`run_suppress` 抑制仿真器特定编号的烦人告警（如 135、1236、1073 等），让日志聚焦在真正的错误上。

**(b) 编译清单分组**

[sim/config.tcl:27-43](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L27-L43) —— 用 `add_sources $LibPath {...} -tag lib` 声明外部依赖源（en_cl_fix 包、psi_common 的一系列公共组件）。`-tag lib` 仅作分组标记，便于在交互模式下按 tag 选择性编译。

[sim/config.tcl:51-53](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L51-L53) —— 单独把 `psi_fix_pkg.vhd` 列为第一批 `src`，确保它先于所有依赖它的实体被编译。

[sim/config.tcl:64-105](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L64-L105) —— 第二批 `src`：所有可综合组件实体（mov_avg、cordic、cic、fir、dds 等）。

[sim/config.tcl:108-160](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L108-L160) —— `-tag tb`：所有测试台实体（注意 FIR 测试台还配套编译了 `*_tb_pkg.vhd`、`*_case0_pkg.vhd` 等参数包，用于把不同测试用例的系数打包成常量数组）。

**(c) 测试台运行清单与参数矩阵**

先看两个「无需 pre_script、无需额外数据」的简单测试台：

[sim/config.tcl:163-167](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L163-L167) —— `en_cl_fix_pkg_tb` 与 `psi_fix_pkg_tb` 是最朴素的形态：`create_tb_run` 后直接 `add_tb_run`，不传任何额外参数，用测试台的默认 generics 跑一次。

再看「带参数矩阵」的例子——FIR 串行多通道可配置滤波器：

[sim/config.tcl:207-213](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L207-L213) —— `tb_run_add_pre_script` 让 PsiSim 在跑这个 TB 之前先执行 `preScript.py`（生成 Data 文件，见 4.3 节）；随后三组 `tb_run_add_arguments` 让同一个测试台分别用 `duty_cycle_g=32/RBW`、`duty_cycle_g=4/RBW`、`duty_cycle_g=4/WBR` 跑三轮，覆盖高吞吐与低吞吐、两种 RAM 读写行为。

FIR 半并行版本更复杂，连跑八轮，覆盖通道数、抽头数、乘法器数、抽取比、RAM 行为等：

[sim/config.tcl:229-241](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L229-L241) —— 注意第 232 行还用了 `tb_run_add_time_limit "5000 us"` 给这个长测试设了 5 ms 仿真时长的上限，避免它卡死整个回归。

最后看一个用 `idle_cycles` 模拟握手压力的例子：

[sim/config.tcl:260-270](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L260-L270) —— CIC 插值测试通过不同的 `gin_idle_cycles_g`/`gout_idle_cycles_g` 组合，分别模拟「输入饥饿」「输出阻塞」「两者同时」等握手场景（注释在第 263 行说明）。这正是「参数矩阵」的典型用法：用同一份测试台代码覆盖多种工况。

#### 4.2.4 代码实践

**实践目标**：通过阅读 `config.tcl`，统计本次回归会执行多少个测试台、每个测试台跑几轮，体会「参数矩阵」的规模。

**操作步骤**：

1. 在 `sim/config.tcl` 中搜索所有 `create_tb_run` 出现的位置。
2. 对每一个 `create_tb_run`，数一数它后面 `tb_run_add_arguments` 里有几个参数串（每个串 = 一轮运行）。
3. 特别记录 `psi_fix_cic_int_fix_1ch_tb`（第 260 行起）和 `psi_fix_fir_dec_semi_nch_chtdm_conf_tb`（第 229 行起）各跑了几轮。

**需要观察的现象**：

- 整份 `config.tcl` 共声明了 40 多个 `create_tb_run`（约 43 个测试台实体）。
- 多数 FIR/CIC 测试台一个就跑 3–8 轮，而 `resize_pipe`、`param_ram`、`comparator` 这类简单组件只跑 1 轮（[sim/config.tcl:424-428](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L424-L428)、[sim/config.tcl:471-472](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L471-L472)）。

**预期结果**：你会意识到「跑一次回归」其实意味着上百次独立的仿真运行，这就是为什么需要 `###ERROR###` 这种统一错误标志来自动判定（见 4.3 节）。具体数字待本地按上面步骤核对。

> 本实践为源码阅读型，无需运行仿真器。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `psi_fix_pkg.vhd` 在 `add_sources` 里被单独列成一组（第 51–53 行），而不是和其他实体混在一起？
**答案**：因为 `psi_fix_pkg` 是所有实体都 `use` 的基础包，必须最先被编译。把它单独列出并放在所有实体之前，是保证 VHDL 编译顺序正确的最简单做法（参见 u1-l2 的编译顺序纪律）。

**练习 2**：`tb_run_add_arguments "-gA=1" "-gA=2"` 和写成两行 `create_tb_run` 有什么区别？
**答案**：前者是**同一个测试台实体跑两轮**，每轮用不同 generics；后者会创建两个独立的测试台运行记录。前者更适合「同一组件换参数」的回归，代码更紧凑。

### 4.3 回归判定与 pre_script

#### 4.3.1 概念说明

PsiSim 自己**不懂**信号对错，它只做两件事：把测试台跑完，然后在日志里搜一个约定好的错误标志字符串。psi_fix 选择的标志是 `###ERROR###`，这条约定写在 `doc/files/introduction.md` 里：测试台只有在真出问题时才用 `error`/`failure` 级别上报，且错误消息**必须**以 `###ERROR###:` 开头。

这套约定之所以成立，靠的是 psi_fix 的位真自检测试台：测试台读入 Python 模型预先算好的「黄金输出」文本，逐位和 VHDL 实际输出比对，一旦不一致就由 `psi_tb` 工具包（外部依赖）代为打印 `###ERROR###`。于是「日志里有没有 `###ERROR###`」就等价于「VHDL 与位真模型是否逐位一致」。

而那些「黄金输出」文本从哪来？来自 **pre_script**——在测试台运行**之前**先跑的 Python 脚本。它调用位真 Python 模型，生成刺激（input）和期望输出（output）两个文本文件。本节要讲清两类 pre_script 的区别，这是理解「为什么 FIR 测试需要 pre_script」的关键。

#### 4.3.2 核心流程

回归判定的链路分两层：

```text
【仿真层 run.tcl】
  run_tb -all            # 跑完全部 TB，各自把结果写进 transcript
  run_check_errors "###ERROR###"   # PsiSim 扫描 transcript，命中 => 回归失败

【CI 层 ciFlow.py】
  vsim -batch -do ci.do            # 跑完上面的 run.tcl，日志落盘 Transcript.transcript
  读 Transcript.transcript：
    含 "###ERROR###"                      => exit(-1)  （期望之内的失败：位真比对不过）
    不含 "SIMULATIONS COMPLETED SUCCESSFULLY" => exit(-2)  （意外失败：仿真崩了/没跑完）
  否则继续跑 Python 单元测试
```

pre_script 则分两类，运行时机不同：

| 类型 | 触发方式 | 运行时机 | 产物 | 典型例子 |
| --- | --- | --- | --- | --- |
| **数据生成型** | `tb_run_add_pre_script` | 该 TB run **运行之前**（编译之后） | `Data/*.txt` 刺激与期望输出 | FIR/CIC/DDS 等所有位真比对测试 |
| **代码生成型** | 直接写在 `config.tcl` 顶部 | **编译之前** | 一个新的 `.vhd` 源文件 | `lut_gen` 的 `fir_design_test.py` 生成 `psi_fix_lut_test1.vhd` |

为什么 FIR 测试需要（数据生成型）pre_script？因为 FIR 的输入是上千个随机样本、系数是用 `scipy.signal.firwin` 现场设计的、期望输出要用位真模型逐位算出来——这些数据量大、且能由 Python 模型**确定性地重新生成**（固定随机种子），所以不进 git，而在每次跑测试前现算。`lut_gen` 则不同，它的脚本生成的是要被编译的 VHDL 代码，所以必须更早——在 `compile_files` 之前就跑完。

#### 4.3.3 源码精读

**(a) `###ERROR###` 约定的出处**

[doc/files/introduction.md:86-94](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md#L86-L94) —— 明确规定：位真 Python 模型与自检测试台是入库门槛；测试台只在真出问题时上报 `error/failure`；**错误消息必须以 `###ERROR###:` 开头**，因为回归脚本会搜这个串；对 psi_fix，测试台还必须调用 Python 模型做位真比对。

**(b) 仿真层判定**

[sim/run.tcl:30](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/run.tcl#L30) —— `run_check_errors "###ERROR###"`：PsiSim 在跑完全部 TB 后扫描 transcript，一旦命中 `###ERROR###` 就判定回归失败。

**(c) CI 层判定**

[scripts/ciFlow.py:15-26](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/scripts/ciFlow.py#L15-L26) —— CI 先 `vsim -batch -do ci.do` 跑完回归并落盘 `Transcript.transcript`，然后读这个文件：含 `###ERROR###` 就 `exit(-1)`（位真比对失败）；**不含** `SIMULATIONS COMPLETED SUCCESSFULLY` 就 `exit(-2)`（仿真没正常跑完，比如编译失败或崩了）。两个标志各司其职：前者抓「功能错」，后者抓「环境/崩溃」。

[scripts/ciFlow.py:28-36](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/scripts/ciFlow.py#L28-L36) —— 仿真通过后，CI 还会跑 `unittest/` 下的 Python 单元测试（`psi_fix_pkg_test`），单独再校一层 Python 模型本身的正确性。

**(d) 数据生成型 pre_script（FIR 样例）**

[sim/config.tcl:207-209](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L207-L209) —— `tb_run_add_pre_script "python3" "preScript.py" "<Scripts 目录>"`：PsiSim 在跑这个 TB 之前，先 `cd` 到 Scripts 目录并用 `python3` 执行 `preScript.py`。

[testbench/psi_fix_fir_dec_ser_nch_chtdm_conf_tb/Scripts/preScript.py:14-19](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_fir_dec_ser_nch_chtdm_conf_tb/Scripts/preScript.py#L14-L19) —— 准备 `Data/` 输出目录（`os.mkdir`，已存在则忽略）。

[testbench/psi_fix_fir_dec_ser_nch_chtdm_conf_tb/Scripts/preScript.py:36-54](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_fir_dec_ser_nch_chtdm_conf_tb/Scripts/preScript.py#L36-L54) —— 位真建模的核心：用 `scipy.signal.firwin` 设计 12 抽头系数并 `psi_fix_from_real` 量化；`np.random.seed(0)` 固定种子后生成两路输入；用 `psi_fix_fir` 模型的 `Filter` 方法算出期望输出。固定种子保证每次重生成完全一致。

[testbench/psi_fix_fir_dec_ser_nch_chtdm_conf_tb/Scripts/preScript.py:59-74](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_fir_dec_ser_nch_chtdm_conf_tb/Scripts/preScript.py#L59-L74) —— 把浮点定点值用 `psi_fix_get_bits_as_int` 转成**整数位表示**写入 `input.txt`/`coefs.txt`/`output.txt`。注意写的是整数（位的十进制值），不是浮点数——这样 VHDL 测试台读回后能直接当成 `signed` 向量逐位比对，无需关心小数点位置。这就是「位真」落地为文本的方式。

测试台这一侧，读文本 + 比对由 `psi_tb` 工具包的两个过程完成：

[testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd:140-146](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd#L140-L146) —— `ApplyTextfileContent` 读 `input.txt`，按 `duty_cycle_g` 节拍把样本喂给 DUT 的输入握手信号。

[testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd:161-166](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd#L161-L166) —— `CheckTextfileContent` 读 `output_<模式>.txt`（即 preScript 写出的期望输出），和 DUT 实际输出逐位比对；不一致时由 `psi_tb` 包打印 `###ERROR###`。这就把 pre_script 生成的文本和回归判定串成了一个闭环。

**(e) 代码生成型 pre_script（lut_gen 样例）**

[sim/config.tcl:20-24](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L20-L24) —— 在 `config.tcl` 一开头、任何 `compile_files` 之前，先 `cd` 到 lut_gen 的 Script 目录执行 `python3 fir_design_test.py`。注释（第 20 行）写明：这是「在编译之前生成代码」。

[testbench/psi_fix_lut_gen_tb/Script/fir_design_test.py:76-86](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_lut_gen_tb/Script/fir_design_test.py#L76-L86) —— 调用 `psi_fix_lut.Generate(cfg, path, fileName)` 直接生成一份 `psi_fix_lut_test1.vhd` 源文件。这份生成的 `.vhd` 随后被当作普通源码编译（见 [sim/config.tcl:143-144](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L143-L144) 里列出的 `psi_fix_lut_test1.vhd`）。因为它产出的是**要被编译的代码**，所以必须在编译之前运行——这是它与 FIR 那类数据生成型 pre_script 的本质区别。

[sim/config.tcl:370-374](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L370-L374) —— 对应的 `psi_fix_lut_gen_tb` 运行声明里**没有**再调 `tb_run_add_pre_script`，注释（第 371 行）解释了原因：「Pre-Script 在编译前执行，因为它生成的是要被编译的代码」。代码已经在顶部生成好了，这里只需跑测试台。

#### 4.3.4 代码实践

**实践目标**：跑一次完整回归（若环境允许），记录哪些测试台被执行；并从源码层面解释为什么 FIR 测试必须有 pre_script。这是本讲的主实践任务。

**操作步骤（运行路径，需要本地环境）**：

1. 按 u1-l1 的目录结构，把 en_cl_fix、psi_common、psi_tb、PsiSim 等依赖并排 checkout 好，并安装 Modelsim（或 GHDL + tclsh）与 Python 3 + SciPy/NumPy。
2. 在 Modelsim 的 TCL 控制台 `cd` 到 `sim/` 目录，执行 `source ./run.tcl`（GHDL 用户则在 `tclsh` 里 `source ./runGhdl.tcl`）。
3. 观察输出：依次出现 `-- Compile`、`-- Run`、`-- Check` 三段；`-- Run` 段会逐个打印正在跑的测试台名和它用的 generics。
4. 回归结束后查看是否出现 `###ERROR###`，以及结尾是否有 `SIMULATIONS COMPLETED SUCCESSFULLY`。

**操作步骤（源码阅读路径，无需环境，推荐先用这条）**：

1. 在 `sim/config.tcl` 里找出所有调了 `tb_run_add_pre_script` 的测试台（FIR、CIC、DDS、CORDIC、mov_avg、bin_div 等），列表记录。
2. 打开 `testbench/psi_fix_fir_dec_ser_nch_chtdm_conf_tb/Scripts/preScript.py`，确认它写出了哪三个文本文件（input/coefs/output）。
3. 打开 `testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd`，找到 `ApplyTextfileContent`（读 input）和 `CheckTextfileContent`（比对 output）两处，把 pre_script → 文本 → 测试台这条链路串起来。

**需要观察的现象（运行路径）**：

- 每个带 `tb_run_add_pre_script` 的测试台运行前，控制台会先闪过一次 `python3 preScript.py` 的执行。
- 一个测试台若有多组 `tb_run_add_arguments`，会被连续跑多轮，每轮 generics 不同。
- 全部跑完后，`run_check_errors "###ERROR###"` 会报告是否扫到错误标志。

**预期结果（运行路径）**：在没有改动源码的情况下，回归应当顺利结束且日志中不含 `###ERROR###`，并打印 `SIMULATIONS COMPLETED SUCCESSFULLY`。具体输出与耗时**待本地验证**（取决于机器与仿真器）。

**关于「为什么 FIR 测试需要 pre_script」的答案**：FIR 的输入是上千个随机样本、系数是 `scipy.signal.firwin` 现场设计的滤波器、期望输出要由位真 `psi_fix_fir` 模型逐位算出（见 [preScript.py:40-54](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_fir_dec_ser_nch_chtdm_conf_tb/Scripts/preScript.py#L40-L54)）。这些数据体积大、又能用固定随机种子（`np.random.seed(0)`）确定性地重新生成，所以不入库，而在每次跑测试前由 pre_script 现算并写成整数位表示文本，供测试台 `CheckTextfileContent` 逐位比对。没有 pre_script，测试台就没有「黄金输出」可比，位真验证就无从谈起。

> 若本地没有仿真器，请走「源码阅读路径」完成本实践——它能让你同样完整地理解 pre_script 的作用，只是不产生实际仿真日志。

#### 4.3.5 小练习与答案

**练习 1**：`ciFlow.py` 里 `exit(-1)` 和 `exit(-2)` 分别代表什么失败？为什么要分两种？
**答案**：`exit(-1)` 对应日志里出现了 `###ERROR###`，即某个测试台的位真比对失败（**功能错**）；`exit(-2)` 对应日志里**没有** `SIMULATIONS COMPLETED SUCCESSFULLY`，即仿真根本没正常跑完（**环境/崩溃错**，比如编译失败、仿真器异常退出）。分开判定让人一眼看出是组件本身有 bug 还是测试环境出了问题。

**练习 2**：`tb_run_add_pre_script`（数据生成型）和 `config.tcl` 顶部的 `exec python3 fir_design_test.py`（代码生成型）分别在哪个阶段运行？为什么后者不能也用 `tb_run_add_pre_script`？
**答案**：前者在该 TB run **运行之前**（编译之后）运行，产出 `Data/*.txt`；后者在**编译之前**运行，产出一份要被编译的 `.vhd` 源文件。后者产出的是代码，必须赶在 `compile_files` 之前就位，否则编译时找不到这个文件，所以不能用「运行前」的 `tb_run_add_pre_script`，而要直接写在 `config.tcl` 最前面。

## 5. 综合实践

把本讲三块内容串起来，完成下面这个「新增一个测试台到回归」的纸面推演任务（无需真改代码）：

> 场景：假设你给 psi_fix 新加了一个组件 `psi_fix_gain`（纯乘常数），并写好了它的 VHDL 实体、Python 位真模型 `model/psi_fix_gain.py` 和自检测试台 `testbench/psi_fix_gain_tb/psi_fix_gain_tb.vhd`。现在要把它接入回归。

请回答并写出（在草稿上即可，**不要**真的修改 `config.tcl`）：

1. **编译清单**：你需要在 `config.tcl` 的哪两个 `add_sources` 块里分别加上 `psi_fix_gain.vhd` 和 `psi_fix_gain_tb/psi_fix_gain_tb.vhd`？分别用什么 `-tag`？（参考 [sim/config.tcl:64-105](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L64-L105) 与 [sim/config.tcl:108-160](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L108-L160)）
2. **测试台运行**：写一段 `create_tb_run "psi_fix_gain_tb"` + `tb_run_add_pre_script`（指向你的 `Scripts/preScript.py`）+ 两组 `tb_run_add_arguments`（一组满占空比 `duty_cycle_g=1`，一组低占空比 `duty_cycle_g=5`）+ `add_tb_run` 的 TCL 片段，模仿 mov_avg 的写法（参考 [sim/config.tcl:298-304](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L298-L304)）。
3. **错误判定**：说明如果 VHDL 实现和 Python 模型不一致，错误会以什么字符串、被哪一层（`run.tcl` 还是 `ciFlow.py`）捕获。
4. **pre_script**：你的 `preScript.py` 应当写出哪几个文本文件？为什么写的是整数（用 `psi_fix_get_bits_as_int`）而不是浮点数？

**参考答案要点**：

1. `psi_fix_gain.vhd` 加进第二批 `src`（`-tag src`），`psi_fix_gain_tb.vhd` 加进 `tb` 块（`-tag tb`）。
2. 片段形如：

   ```tcl
   create_tb_run "psi_fix_gain_tb"
   tb_run_add_pre_script "python3" "preScript.py" "../testbench/psi_fix_gain_tb/Scripts"
   set dataDir [file normalize "../testbench/psi_fix_gain_tb/Data"]
   tb_run_add_arguments   "-gfile_folder_g=$dataDir -gduty_cycle_g=1" \
               "-gfile_folder_g=$dataDir -gduty_cycle_g=5"
   add_tb_run
   ```
   （这是依据 mov_avg 写法推导的**示例片段**，实际 generics 名需与你的实体一致。）

3. 由 `psi_tb` 的 `CheckTextfileContent` 在比对失败时打印 `###ERROR###`；仿真层 `run.tcl` 的 `run_check_errors "###ERROR###"` 先捕获，CI 层 `ciFlow.py` 再读 `Transcript.transcript` 命中并 `exit(-1)`。
4. 应写出 `input.txt`（刺激）和 `output.txt`（期望输出）。写整数位表示是为了让 VHDL 测试台能把它直接当 `signed` 向量逐位比对，省去小数点对齐——这正是「位真」的核心（见 [preScript.py:59-74](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_fir_dec_ser_nch_chtdm_conf_tb/Scripts/preScript.py#L59-L74)）。

这个任务把「声明源码 → 声明测试台运行 → pre_script 生成数据 → `###ERROR###` 判定」整条链路走了一遍，对应 u10-l1「贡献新组件」时会再次用到。

## 6. 本讲小结

- psi_fix 的回归由**外部 PsiSim TCL 框架**驱动，入口脚本是 `run.tcl`（Modelsim）与 `runGhdl.tcl`（GHDL，仅差一个 `init -ghdl`），二者都 `source` 同一份 `config.tcl`。
- 一次回归的五步是：`init` → `source config.tcl` → `compile_files -all -clean` → `run_tb -all` → `run_check_errors "###ERROR###"`。
- `config.tcl` 是声明式配置：`add_sources -tag` 声明编译清单（`psi_fix_pkg.vhd` 必须最先编译），`create_tb_run`/`tb_run_add_arguments`/`add_tb_run` 声明测试台运行及其参数矩阵。
- 回归判定靠 `###ERROR###` 字符串约定（规定于 `doc/files/introduction.md`）：测试台位真比对失败时由 `psi_tb` 打印它，PsiSim 与 `ciFlow.py` 据此判定失败；`ciFlow.py` 还用 `SIMULATIONS COMPLETED SUCCESSFULLY` 区分「功能错」与「环境崩溃」。
- pre_script 分两类：**数据生成型**（`tb_run_add_pre_script`，TB 运行前生成 `Data/*.txt`，FIR/CIC 等都靠它）与**代码生成型**（写在 `config.tcl` 顶部，编译前生成 `.vhd`，如 `lut_gen`）。
- FIR 测试需要 pre_script，是因为其输入/系数/期望输出数据量大且可由位真模型确定性重生（固定随机种子），不入库而在测试前现算并写成整数位表示文本。

## 7. 下一步学习建议

- 下一讲 **u1-l4 定点数格式与握手约定** 会进入 `[s,i,f]` 定点格式与 AXI-S 握手的细节——理解它之后，你就能看懂 pre_script 里那些 `psi_fix_fmt_t(1,0,16)` 和测试台里的 `vld_i/rdy_o` 到底在表达什么。
- 想深入看一个完整测试台的 stim/check 双进程，可直接读 `testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd`（本讲已引用其 140–166 行），它是最简洁的自检测试台样板，**u3-l2 测试台与协同仿真流程**会以它为主角展开。
- 想了解 PsiSim 框架本身的命令全集（`compile_files -contains`、`run_tb -contains`、`run_check_errors` 的更多用法），可阅读外部仓库 `psi_fpga_all` 下的 PsiSim 文档——这超出 psi_fix 本仓库范围，标注为「外部资料，待确认」。
- 对 CI 自动化（`ciFlow.py` 如何被触发、`Transcript.transcript` 如何解析、Python 单测覆盖哪一层）感兴趣的，可先跳读 **u10-l2 CI 流程与文档/依赖自动化**，本讲的 `ciFlow.py` 片段在那里会有完整展开。

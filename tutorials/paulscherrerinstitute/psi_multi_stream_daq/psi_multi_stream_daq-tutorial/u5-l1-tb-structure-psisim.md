# 测试平台结构与 PsiSim 仿真流程

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `sim/` 目录下四个脚本（`config.tcl`、`run.tcl`、`ci.do`、`interactive.tcl`）各自扮演什么角色，以及它们之间的调用关系。
- 看懂 `config.tcl` 这张「配料表」：它如何用 `add_sources ... -tag lib/src/tb` 把依赖库、项目源码、testbench 分成三组，又如何用 `create_tb_run` / `tb_run_add_arguments` / `add_tb_run` 配置每次仿真运行。
- 复述 `run.tcl` 的「编译 → 运行 → 检查」三段式，并解释 `run_check_errors "###ERROR###"` 是如何发现失败用例的。
- 理解 `scripts/ciFlow.py` 如何用 `vsim -batch -do ci.do` 跑批处理回归，并能说出 `-1`、`-2`、`0` 三个退出码分别代表什么。
- 读懂 VHDL 实体声明里的 `$$ ... $$` 元注解（`testcases`、`processes`、`tbpkg`、`constant`、`proc` 等），明白它们如何描述一个 testbench 的覆盖面、进程/时钟域划分与依赖包。

本讲是第五单元（仿真验证）的第一篇，不展开任何一个 testbench 的内部实现，只讲「仿真如何被组织、如何被驱动、如何判定成败」这套工程骨架。具体的用例分析见 [u5-l2](u5-l2-module-testbenches.md) 与 [u5-l3](u5-l3-toplevel-multistream-tb.md)。

## 2. 前置知识

在学习本讲前，建议你已经具备以下认知（这些在 [u1-l2 仓库结构与仿真/构建运行方式](u1-l2-repo-and-simulation.md) 中已经建立）：

- **Testbench（测试平台）/ DUT（被测器件）**：用 VHDL 写一段「激励 + 自检」代码，把待测模块包起来，在仿真器里跑，看输出是否符合预期。本项目的 DUT 是 `hdl/` 下的 7 个模块，testbench 在 `tb/` 下。
- **仿真器**：本项目面向 Mentor 的 **Modelsim / QuestaSim**（命令行入口是 `vsim`）。
- **Tcl**：仿真器的脚本语言。`sim/` 下的 `.tcl` / `.do` 文件都是 Tcl 脚本。
- **PsiSim**：PSI 自研的 Tcl 仿真框架，提供 `init`、`add_sources`、`compile_files`、`run_tb`、`run_check_errors` 等高层命令，把「建库、加文件、编译、跑、检查」这套繁琐流程封装成几行调用。它不是本仓库的一部分，而是作为依赖放在平级目录 `../../../TCL/PsiSim/` 下。
- **回归测试（regression）**：一次性把所有 testbench 在所有 generic 组合下跑一遍，任何一个失败都算整体失败。这就是 `ci.do` + `ciFlow.py` 做的事。
- **`###ERROR###` 约定**：所有 testbench 在断言失败时统一打印这个字符串，PsiSim 的 `run_check_errors` 靠扫描它来发现失败——这是整个回归流程的「成败信号」。

如果你还不清楚 `psi_common` / `psi_tb` / `PsiSim` 这三个依赖是如何被拉取和放置的，请先回顾 [u1-l2](u1-l2-repo-and-simulation.md)。本讲直接假设它们已经平级放在 `../../../VHDL/` 与 `../../../TCL/` 下。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| `sim/config.tcl` | 仿真「配料表」：声明库依赖、源码、testbench 与每次运行的 generic | 核心精读对象 |
| `sim/run.tcl` | 仿真主流程：加载 PsiSim → init → source config → 编译 → 运行 → 检查 | 核心精读对象 |
| `sim/ci.do` | CI 入口：`onerror {exit}` + `source run.tcl` + `quit` | 一层薄封装 |
| `sim/interactive.tcl` | 交互调试入口：只编译不运行，留在仿真器里手动调试 | 辅助说明 |
| `scripts/ciFlow.py` | 批处理编排：调 `vsim -batch`，读日志，给退出码 | 核心精读对象 |
| `hdl/psi_ms_daq_input.vhd` | 输入逻辑实体，含 `$$` 元注解示例 | 注解约定讲解 |

> 说明：`sim/` 下还有一个 `.gitignore`，用于忽略仿真产生的临时文件（库映射、编译产物、`Transcript.transcript` 日志等），不在本讲讨论范围。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先讲「配料表」`config.tcl`，再讲「主流程」`run.tcl`，接着讲「CI 批处理」`ci.do` + `ciFlow.py`，最后讲贯穿所有 testbench 的「`$$` 元注解约定」。

### 4.1 仿真配料表：sim/config.tcl

#### 4.1.1 概念说明

`config.tcl` 是整个仿真的「配料表」：它本身不执行仿真，只负责**声明**——声明用哪个库、依赖哪些外部文件、本项目有哪些源码、有哪些 testbench、每个 testbench 要用哪些 generic 跑几遍。真正「执行」这些声明的是 PsiSim 框架（由 `run.tcl` 驱动）。

PsiSim 的设计哲学是「声明式配置 + 框架执行」：你只管把文件和运行参数列出来，框架按声明顺序编译、按声明参数逐个运行 testbench、统一收集结果。这样一份 `config.tcl` 既能被 `run.tcl`（全自动回归）使用，也能被 `interactive.tcl`（交互调试）使用，配置与执行解耦。

#### 4.1.2 核心流程

`config.tcl` 自上而下分四段：

1. **设置依赖路径与库**：定义外部 VHDL 依赖所在的相对路径，并给本项目源码建立一个名为 `psi_ms_daq` 的仿真库。
2. **抑制无害告警**：声明编译期和运行期要屏蔽的 Modelsim 消息编号，保持日志干净。
3. **三组 `add_sources`**：按 `-tag lib`、`-tag src`、`-tag tb` 三组，分别声明依赖库文件、项目源码、testbench 文件。这三组的顺序就是编译顺序。
4. **`create_tb_run` 配置运行**：为每个 testbench 声明一次或多次运行（不同的 generic 组合），用 `add_tb_run` 注册。

伪代码如下：

```
set LibPath "../../../VHDL"          # 外部依赖根目录
add_library psi_ms_daq               # 建本项目库
compile_suppress / run_suppress ...  # 屏蔽噪声消息

add_sources $LibPath { ...依赖库文件... } -tag lib   # 第 1 组：库
add_sources "../hdl"  { ...本项目源码... } -tag src   # 第 2 组：源码
add_sources "../tb"   { ...testbench...  } -tag tb    # 第 3 组：TB

create_tb_run "xxx_tb"                # 开始声明一个 TB 运行
tb_run_add_arguments "-gA=1" "-gA=2"  # 可选：多组 generic，每组一次运行
add_tb_run                           # 注册该 TB 的所有运行
```

#### 4.1.3 源码精读

**设置依赖路径与库**——外部依赖放在相对路径 `../../../VHDL` 下（即仓库平级目录），本项目源码归入仿真库 `psi_ms_daq`：

[sim/config.tcl:7-14](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L7-L14) — 设 `LibPath`、导入 `psi::sim` 命名空间、用 `add_library psi_ms_daq` 建库。这里 `../../../VHDL` 的相对基准是 `sim/` 目录，所以依赖必须按 [u1-l2](u1-l2-repo-and-simulation.md) 所述平级放置。

**抑制无害告警**——Modelsim 对未连接端口、被优化的寄存器等会打印大量编号告警，这里把编号写死屏蔽掉：

[sim/config.tcl:16-18](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L16-L18) — `compile_suppress` 管编译期消息（如 135、1236），`run_suppress` 管运行期消息（如 8684、3479）。

**第 1 组 `add_sources ... -tag lib`**——声明所有外部依赖文件，来自 `psi_tb` 与 `psi_common` 两个库。注意顺序：先基础包（`psi_tb_txt_util`、`psi_common_math_pkg`、`psi_common_logic_pkg`），再依赖它们的实体（`psi_common_sdp_ram`、各类跨时钟域组件、AXI 组件等）。这个顺序就是编译顺序，被依赖的文件必须排在前面：

[sim/config.tcl:20-43](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L20-L43) — `psi_tb`（文本工具、比较、AXI 激励、activity 检测）与 `psi_common`（数学包、各类 RAM/FIFO/CC/AXI 主从组件）的所有依赖文件，共 22 个，全部打 `-tag lib`。

**第 2 组 `add_sources "../hdl" ... -tag src`**——本项目 7 个源文件（与 [u1-l2](u1-l2-repo-and-simulation.md) 列出的 7 个文件一致）：

[sim/config.tcl:45-54](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L45-L54) — 先 `psi_ms_daq_pkg`（公共包，被所有模块 use），再 `input`、`daq_sm`、`daq_dma`、`axi_if`、`reg_axi`，最后顶层 `psi_ms_daq_axi`。包必须最先编译，顶层必须最后。

**第 3 组 `add_sources "../tb" ... -tag tb`**——所有 testbench 及其拆分的 case 文件，按模块分子目录：

[sim/config.tcl:56-95](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L56-L95) — 包含 `psi_ms_daq_input/`（1 个 tb_pkg + 8 个 case + 1 个 TB）、`psi_ms_daq_daq_sm/`（1 个 tb_pkg + 7 个 case + 1 个 TB）、`psi_ms_daq_daq_dma/`（1 个 tb_pkg + 8 个 case + 1 个 TB）、`psi_ms_daq_axi/`（1 个 tb_pkg + 4 个 `str*_pkg` 数据包 + 1 个顶层 TB）、`psi_ms_daq_axi_1s/`（1 个 `str0_pkg` + 1 个单流变体 TB）。每个子目录里 `*_tb_pkg.vhd` 必须排在 `*_tb.vhd` 之前，因为它定义了 TB 用到的过程。

**`create_tb_run` 配置运行**——这部分最关键，它决定了「实际上要跑多少次仿真」。`psi_ms_daq_input_tb` 用了 6 组 generic，其余 4 个 TB 各跑一次（用默认 generic）：

[sim/config.tcl:97-118](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L97-L118) — `input_tb` 的 6 组 generic 对 `StreamWidth_g`（8/16/32/64）与 `VldPulsed_g`（true/false，只在 8 和 64 两个极端做）做了参数扫描；`daq_sm_tb`、`daq_dma_tb`、`axi_tb`、`axi_1s_tb` 各自只调用 `create_tb_run` + `add_tb_run`，不带额外参数。

把运行次数加起来：`input_tb` 6 次 + 其余 4 个各 1 次 = **每次回归共跑 10 次仿真**。这 10 次里任何一次打印 `###ERROR###`，整个回归都判失败。

> **关于 `create_tb_run` / `tb_run_add_arguments` / `add_tb_run` 三件套**：`create_tb_run "名字"` 开启一个 TB 的运行声明；`tb_run_add_arguments` 为它追加若干组 generic 字符串，每组字符串对应一次独立运行（没调用它就只跑一次默认运行）；`add_tb_run` 把当前声明收尾登记。这是 PsiSim 对「同一个 TB 用不同参数跑多遍」的统一写法。

#### 4.1.4 代码实践

**实践目标**：理解 `config.tcl` 的「三组 + 运行配置」结构，能数出一次回归到底跑多少次仿真。

**操作步骤**：

1. 打开 [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl)。
2. 找到三处 `add_sources`，分别确认它们的 `-tag` 是 `lib` / `src` / `tb`，并数出每组各有多少个文件。
3. 找到 `create_tb_run "psi_ms_daq_input_tb"`，数 `tb_run_add_arguments` 后面跟了几个字符串（每个字符串 = 一次运行）。
4. 找到其余四个 `create_tb_run`，确认它们都没有 `tb_run_add_arguments`（即各跑 1 次）。

**需要观察的现象**：

- 第 1 组（lib）有 22 个文件，全部来自 `psi_tb/` 和 `psi_common/`。
- 第 2 组（src）正好 7 个文件，与 `hdl/` 目录一致。
- 第 3 组（tb）按 5 个子目录组织，case 文件数量与对应实体 `$$ testcases` 注解的条目数一一对应（见 4.4 节）。
- `input_tb` 有 6 组 generic，其余各 1 组。

**预期结果**：一次完整回归 = 6 + 1 + 1 + 1 + 1 = 10 次仿真运行。

**待本地验证**：如果你本地装了 Modelsim/QuestaSim 并按 [u1-l2](u1-l2-repo-and-simulation.md) 摆好了依赖，运行回归后日志里应能看到 10 次仿真记录。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `add_sources "../hdl" ... -tag src` 整组挪到 `add_sources ... -tag lib` 之前，会发生什么？

**参考答案**：编译会失败。`-tag` 的声明顺序就是编译顺序，而项目源码（如 `psi_ms_daq_input.vhd`）`use` 了 `psi_common_math_pkg`、`psi_common_sdp_ram` 等依赖库文件。依赖必须先编译，所以 lib 组必须在 src 组之前。

**练习 2**：`create_tb_run "psi_ms_daq_daq_sm_tb"` 之后直接 `add_tb_run`，没有 `tb_run_add_arguments`，这意味着什么？

**参考答案**：意味着这个 TB 只跑一次，使用实体声明的默认 generic（`Streams_g=4`、`StreamPrio_g=(1,2,3,1)`、`Windows_g=4` 等，见 [hdl/psi_ms_daq_daq_sm.vhd:33-38](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L33-L38)）。`daq_sm_tb` 不做参数扫描，所有覆盖面靠内部多个 case 文件实现，而不是靠多组 generic。

---

### 4.2 仿真主流程：sim/run.tcl

#### 4.2.1 概念说明

`config.tcl` 只负责「声明」，`run.tcl` 才负责「执行」。`run.tcl` 是一次完整回归的入口脚本：它加载 PsiSim 框架、初始化、读入 `config.tcl` 的声明，然后按「编译 → 运行 → 检查」三段式把整个回归跑完。

`run.tcl` 只有 30 行，但它体现了 PsiSim 的核心抽象：**把繁琐的 Modelsim 底层命令（vlib/vmap/vcom/vsim）封装成 `compile_files`、`run_tb`、`run_check_errors` 三个高层命令**，让用户脚本极简。

#### 4.2.2 核心流程

```
source ../../../TCL/PsiSim/PsiSim.tcl   # 1. 加载 PsiSim 框架
namespace import psi::sim::*            #    导入框架命令
init                                     # 2. 初始化（建工作库映射等）
source ./config.tcl                      # 3. 读入配料表（仅声明，不执行）
compile_files -all -clean                # 4. 按 lib/src/tb 顺序编译全部，先清空
run_tb -all                              # 5. 跑全部声明的 TB 运行
run_check_errors "###ERROR###"           # 6. 扫描日志，发现该串则报错
```

注意 `source ./config.tcl` 这一步**只是执行声明**（把文件和运行参数登记到 PsiSim 的内部数据结构里），真正编译和运行发生在后面的 `compile_files` 和 `run_tb`。

#### 4.2.3 源码精读

**加载框架 + 初始化 + 读配置**：

[run.tcl:7-15](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/run.tcl#L7-L15) — `source` 加载 PsiSim，`namespace import` 把 `init`、`compile_files`、`run_tb`、`run_check_errors` 等命令引入当前作用域，`init` 做框架初始化，`source ./config.tcl` 执行配料表声明。

**编译**——`-all` 表示编译所有已声明的文件，`-clean` 表示先清空旧的编译产物（保证干净回归）：

[run.tcl:17-21](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/run.tcl#L17-L21) — 编译阶段。PsiSim 会按 `-tag lib/src/tb` 的声明顺序、每组内文件列出顺序，依次调用 Modelsim 的 `vcom` 编译所有 VHDL 文件。

**运行**——`-all` 表示跑所有 `create_tb_run` 声明的运行（前面算出的 10 次）：

[run.tcl:22-25](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/run.tcl#L22-L25) — 运行阶段。PsiSim 对每个 TB 运行调用 `vsim` 加载顶层、应用对应 generic、`run -all` 跑完，并把输出写进日志。

**检查**——这是成败判定的关键一行：

[run.tcl:26-30](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/run.tcl#L26-L30) — `run_check_errors "###ERROR###"` 扫描所有 TB 运行的日志，只要任一日志里出现 `###ERROR###` 字符串，PsiSim 就认为有用例失败并报错。这个字符串是所有 testbench 的统一约定——断言失败时由 `psi_tb_txt_util` 打印。

> **`interactive.tcl` 与 `run.tcl` 的区别**：[interactive.tcl:14-19](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/interactive.tcl#L14-L19) 只做前四步（加载、init、source config、compile_files），**不调用 `run_tb`**。它用于交互调试：编译完后把控制权留给 Modelsim 控制台，让你手动 `vsim` 某个 TB、加波形、单步运行。日常开发用 `interactive.tcl`，CI 回归用 `run.tcl`。

#### 4.2.4 代码实践

**实践目标**：理解「声明」与「执行」的分离，确认 `source config.tcl` 自身不触发编译。

**操作步骤**：

1. 对比 [run.tcl](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/run.tcl) 与 [interactive.tcl](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/interactive.tcl)。
2. 找出两者共同的前四步，以及 `run.tcl` 多出的两步（`run_tb -all` 和 `run_check_errors`）。
3. 思考：如果只想验证「我的源码能编译通过」，用哪个脚本最省事？

**需要观察的现象**：两个脚本前半段完全相同，差异只在最后是否运行 TB。

**预期结果**：只想验证编译用 `interactive.tcl`（编译完即停在控制台），想做完整回归用 `run.tcl`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `source ./config.tcl` 不会立刻编译文件？

**参考答案**：因为 `config.tcl` 里的 `add_sources` 只是**声明**（把文件登记进 PsiSim 内部列表），并不调用 `vcom`。真正触发编译的是后面的 `compile_files -all -clean`，它会遍历登记列表、按顺序调用 `vcom`。这种「先声明后执行」的模式让同一份 `config.tcl` 既能驱动全自动回归（`run.tcl`），又能驱动交互调试（`interactive.tcl`）。

**练习 2**：`run_check_errors "###ERROR###"` 里的字符串是谁打印的？

**参考答案**：是 testbench 自己。本项目所有 TB 在断言失败时，通过依赖库 `psi_tb_txt_util`（见 [config.tcl 第 1 组](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L20-L43) 引入的 `psi_tb/hdl/psi_tb_txt_util.vhd`）打印 `###ERROR###`。这是一个跨所有 TB 的统一约定，`run_check_errors` 只是扫描它。

---

### 4.3 CI 批处理与退出码：sim/ci.do + scripts/ciFlow.py

#### 4.3.1 概念说明

`run.tcl` 适合人在 Modelsim 图形界面里手动 `source`。但在 CI（持续集成）环境里，需要的是「一条命令跑完、给个退出码」的批处理方式。本项目用两层封装实现：

- **`sim/ci.do`**：Modelsim 的 `.do` 入门脚本，极薄，只做「出错即退出 + source run.tcl + 退出仿真器」。
- **`scripts/ciFlow.py`**：Python 编排器，切换到 `sim/` 目录，以批处理模式调 `vsim`，然后读日志、判定成败、给出进程退出码。

CI 平台（如 GitHub Actions）只看 Python 脚本的退出码：`0` = 通过，非 `0` = 失败。

#### 4.3.2 核心流程

```
ciFlow.py:
  os.chdir(脚本目录/../sim)                      # 1. 切到 sim 目录
  os.system("vsim -batch -do ci.do -logfile ...") # 2. 批处理跑 vsim，执行 ci.do
  读取 Transcript.transcript                      # 3. 读完整日志
  if "###ERROR###" in 日志:  exit(-1)             # 4a. 有用例失败
  if "SIMULATIONS COMPLETED SUCCESSFULLY" 不在日志: exit(-2)  # 4b. 异常中断
  exit(0)                                          # 4c. 全部通过

ci.do:
  onerror {exit}      # 任何 Tcl 出错都中止（如编译失败）
  source run.tcl      # 跑完整回归
  quit                # 退出 vsim
```

退出码语义是关键：`-1` 表示「仿真跑了，但有断言失败」；`-2` 表示「仿真根本没正常跑完」（比如编译失败、`vsim` 崩溃、`onerror` 触发提前退出，导致成功标志串没打印）；`0` 表示一切正常。

#### 4.3.3 源码精读

**`ci.do`——三层结构**：

[ci.do:7-9](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/ci.do#L7-L9) — `onerror {exit}` 让任何 Tcl 错误（如 `vcom` 编译失败）立刻退出 vsim，而不是卡在错误提示符；`source run.tcl` 跑完整回归；`quit` 退出仿真器，把控制权交回 `ciFlow.py`。

**`ciFlow.py`——切换工作目录**：脚本可能从仓库任意位置被调用，所以先算出自己的绝对路径，再切到 `../sim`，保证 `ci.do` 里的相对路径（`source run.tcl` → `source ./config.tcl` → `../../../VHDL`）能正确解析：

[ciFlow.py:7-11](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/scripts/ciFlow.py#L7-L11) — `THIS_DIR` 取脚本所在绝对路径，`os.chdir(THIS_DIR + "/../sim")` 切到 `sim/` 目录。

**`ciFlow.py`——批处理调用 vsim**：`-batch` 表示无图形界面、批处理模式；`-do ci.do` 表示启动后执行 `ci.do`；`-logfile Transcript.transcript` 把所有输出重定向到这个日志文件，供后面读取：

[ciFlow.py:13](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/scripts/ciFlow.py#L13) — 以批处理模式运行 vsim，执行 `ci.do`，日志写入 `Transcript.transcript`。

**`ciFlow.py`——读日志**：打开上一步生成的日志全文：

[ciFlow.py:15-16](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/scripts/ciFlow.py#L15-L16) — 读 `Transcript.transcript` 全文到 `content`。

**`ciFlow.py`——两层判定与退出码**：这是本讲最需要记住的片段。先查「预期错误」（断言失败），再查「意外缺失」（成功标志没出现）：

[ciFlow.py:18-24](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/scripts/ciFlow.py#L18-L24) — 日志含 `###ERROR###` 返回 `-1`（有用例失败）；日志不含 `SIMULATIONS COMPLETED SUCCESSFULLY` 返回 `-2`（异常中断/编译失败）。

[ciFlow.py:26-27](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/scripts/ciFlow.py#L26-L27) — 都没问题返回 `0`（全部通过）。

> **为什么需要 `-2` 这一层？** 因为 `run_check_errors` 发现 `###ERROR###` 时，PsiSim 会打印失败信息但**仍会继续跑完**并在最后打印 `SIMULATIONS COMPLETED SUCCESSFULLY`。所以「有 ERROR」和「正常跑完」是两个独立信号。如果 `vsim` 因为编译失败在 `onerror {exit}` 处提前退出，那么 `###ERROR###` 可能没出现（TB 还没开始跑），但成功标志也一定没出现——这时 `-2` 兜底，把它识别为失败而不是误判通过。两层判定是「不漏报任何失败」的设计。

#### 4.3.4 代码实践

**实践目标**：掌握三个退出码的判定逻辑，能预测各种失败场景下的退出码。

**操作步骤**：

1. 打开 [scripts/ciFlow.py](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/scripts/ciFlow.py)，对照下面的「场景—退出码」表逐行验证。
2. 打开 [sim/ci.do](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/ci.do)，确认 `onerror {exit}` 在编译失败时会提前退出 vsim。

**需要观察的现象 / 预期结果**（场景表）：

| 场景 | 日志是否含 `###ERROR###` | 日志是否含成功标志 | `ciFlow.py` 退出码 |
| --- | --- | --- | --- |
| 全部用例通过 | 否 | 是 | `0` |
| 某用例断言失败 | 是 | 是（PsiSim 仍跑完） | `-1` |
| 某文件编译失败（`onerror` 提前退出） | 否 | 否 | `-2` |
| vsim 崩溃 / 找不到依赖 | 否 | 否 | `-2` |

**待本地验证**：若本地有仿真环境，可以故意在某 TB 里加一句 `assert false report "###ERROR###"` 重新跑回归，确认 `ciFlow.py` 返回 `-1`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ciFlow.py` 要先判 `-1` 再判 `-2`，顺序不能反？

**参考答案**：因为一次「有断言失败但正常跑完」的回归，日志里会**同时**含有 `###ERROR###` 和成功标志。如果先判 `-2`（成功标志在不在），这种情况下成功标志在，会跳过 `-2`，然后……实际上先判哪个在这里结果一样，因为含 ERROR 时第一层 `if` 就已经 `exit(-1)` 了。真正不能反的逻辑意义在于：`-1` 是更具体的「明确知道哪个用例失败了」，应优先报告；`-2` 是兜底的「不知道哪坏了，反正没跑完」。把更具体的诊断优先，符合「能说清失败原因就别说含糊的」原则。

**练习 2**：如果某次回归 `vsim` 因为 license 问题根本没启动，`Transcript.transcript` 文件可能不存在或为空，`ciFlow.py` 会怎样？

**参考答案**：日志为空时，`"###ERROR###" in content` 为假（跳过 `-1`），`"SIMULATIONS COMPLETED SUCCESSFULLY" not in content` 为真（命中 `-2`），返回 `-2`。这正确地把「仿真器根本没跑起来」识别为失败。若 `Transcript.transcript` 文件不存在，`open` 会抛 `FileNotFoundError` 使 Python 以非零码退出（ traceback 退出码为 1），CI 同样判失败——只是错误信息不如 `-2` 清晰。

---

### 4.4 实体 $$ 元注解约定

#### 4.4.1 概念说明

在 PSI HDL Library 的所有 IP 核里，每个**被测实体**（DUT）的声明上方和端口行末，都有一系列形如 `-- $$ key=value $$` 的注释。这些不是普通注释，而是 **PsiSim 与文档生成工具共同约定的「元注解」（meta-annotation）**，用来机器可读地描述：

- 这个 DUT 由哪些 testbench 用例覆盖（`testcases`）；
- 这个 DUT 内部有哪些进程/时钟域（`processes`）；
- TB 依赖哪些包（`tbpkg`）；
- 哪些 generic 在 TB 里被固定（`constant`）；
- 每个端口属于哪个进程/时钟域、是不是时钟或复位（`proc` / `type=clk` / `type=rst` / `freq` / `lowactive`）。

这些注解对人也是文档：看一眼实体声明，就能知道它的覆盖面和时钟域结构，不必翻进 testbench。

#### 4.4.2 核心流程

注解分三个层级：

```
-- $$ testcases=a,b,c $$          ← 实体级：列出覆盖该实体的所有 TB 用例
-- $$ processes=stream,daq $$      ← 实体级：列出 DUT 内部的进程/时钟域分组
-- $$ tbpkg=work.psi_tb_txt_util $$ ← 实体级：列出 TB 用到的包
entity psi_ms_daq_input is
  generic(
    StreamWidth_g : ... := 16; -- $$ constant=... $$   ← generic 级：该 generic 在 TB 中固定值
    StreamBuffer_g: ... := 1024; -- $$ constant=32 $$  ←   （用于文档与参数扫描区分）
  );
  port(
    Str_Clk : in std_logic; -- $$ type=clk; freq=125e6; proc=stream $$  ← 端口级：时钟、频率、所属进程
    RstMem  : in std_logic; -- $$ type=rst; clk=Clk $$                  ← 端口级：复位、关联时钟
    Daq_Vld : out std_logic; -- $$ proc=daq $$                          ← 端口级：仅标注所属进程
  );
```

`testcases` 的值与 `tb/` 下 `*_tb_case_<名字>.vhd` 文件名严格一一对应——这是验证「声明与实现一致」的快捷检查。

#### 4.4.3 源码精读

以 `psi_ms_daq_input` 为例。**实体级三条注解**——声明该模块被 8 个用例覆盖、内部有 `stream` 与 `daq` 两个进程/时钟域、TB 用到 `psi_tb_txt_util` 包：

[hdl/psi_ms_daq_input.vhd:26-28](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L26-L28) — `testcases=single_frame,multi_frame,timeout,ts_overflow,trig_in_posttrig,always_trig,backpressure,modes`，这 8 个名字与 [config.tcl 第 3 组](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L56-L95) 里 `psi_ms_daq_input/` 子目录下的 8 个 `*_tb_case_*.vhd` 文件一一对应。

**generic 级 `constant` 注解**——标注每个 generic 在 TB 中被固定的值（与默认值不同，表示 TB 用了非默认参数）：

[hdl/psi_ms_daq_input.vhd:31-36](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L31-L36) — 例如 `StreamBuffer_g` 默认 1024，但 `$$ constant=32 $$` 表示 TB 里把它固定成 32（小缓冲利于快速触发溢出/反压场景）；`StreamTimeout_g` 默认 1.0e-3 秒，TB 里固定成 10.0e-6 秒（短超时便于测超时逻辑）；`StreamWidth_g` 标 `$$ export=true $$` 表示它**不固定**，而是被 `config.tcl` 的 `tb_run_add_arguments` 做 6 组参数扫描（见 4.1 节）。

**端口级 `proc` / `type` 注解**——把每个端口归到一个进程/时钟域，时钟和复位额外标注类型：

[hdl/psi_ms_daq_input.vhd:41-46](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L41-L46) — `Str_Clk` 标 `type=clk; freq=125e6; proc=stream`（流时钟，125 MHz，属于 stream 进程）；`Str_Vld/Str_Data/Str_Trig/Str_Ts` 都标 `proc=stream`。

[hdl/psi_ms_daq_input.vhd:60-61](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L60-L61) — `ClkMem` 标 `type=clk; freq=200e6; proc=daq,stream`（内存时钟，200 MHz，被 daq 和 stream 两个进程共享，对应 [u2-l2](u2-l2-input-interface-clocks.md) 讲的三时钟域跨域）；`RstMem` 标 `type=rst; clk=Clk`（复位，关联到 Clk）。

> **其他模块的注解对比**——三个被测模块的注解结构一致，只是覆盖面与进程划分不同：
> - [hdl/psi_ms_daq_daq_sm.vhd:28-30](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L28-L30)：7 个 testcase（single_simple, priorities, single_window, multi_window, enable, irq, timestamp），4 个进程（control, dma_cmd, dma_resp, ctx），用到 `psi_tb_txt_util` + `psi_tb_compare_pkg`。
> - [hdl/psi_ms_daq_daq_dma.vhd:28-30](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L28-L30)：8 个 testcase（aligned, unaligned, no_data_read, input_empty, empty_timeout, cmd_full, data_full, errors），4 个进程（control, input, mem_cmd, mem_dat），用到三个 TB 包（多了 `psi_tb_activity_pkg`）。
>
> 把这三组 `testcases` 与 [config.tcl](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L56-L95) 里的 case 文件名交叉核对，会发现它们完全一致——这就是「实体注解 ↔ TB 实现 ↔ 仿真配置」三方自洽的契约。

#### 4.4.4 代码实践

**实践目标**：验证「实体 `testcases` 注解 ↔ `tb/` 下 case 文件 ↔ `config.tcl` 第 3 组」三者一一对应。

**操作步骤**：

1. 打开 [hdl/psi_ms_daq_input.vhd:26](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L26)，记下 8 个 testcase 名字。
2. 在 [config.tcl:58-67](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L58-L67) 里找到 `psi_ms_daq_input/` 子目录下的 8 个 `*_tb_case_<名字>.vhd`，确认 `<名字>` 与注解完全对应。
3. 对 `daq_sm`（7 个）和 `daq_dma`（8 个）重复上述核对。

**需要观察的现象**：每个 testcase 名字都能在 `config.tcl` 里找到一个同名的 `*_tb_case_*.vhd` 文件，反之亦然。

**预期结果**：input 8 个、daq_sm 7 个、daq_dma 8 个，三处（注解、文件名、config.tcl 条目）数量与名字完全一致。

#### 4.4.5 小练习与答案

**练习 1**：`StreamWidth_g` 的注解是 `$$ export=true $$`，而 `StreamBuffer_g` 是 `$$ constant=32 $$`，这俩注解的含义区别是什么？

**参考答案**：`export=true` 表示该 generic **不固定**、对外「导出」为可扫描参数——对应 [config.tcl](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L98-L106) 里 `input_tb` 的 6 组 `tb_run_add_arguments`，把 `StreamWidth_g` 扫过 8/16/32/64。`constant=32` 表示该 generic 在 TB 里**固定**为 32（虽然实体默认值是 1024），所有运行用同一个值，不做扫描。

**练习 2**：端口注解 `proc=daq,stream`（如 `ClkMem`）说明什么？

**参考答案**：说明这个端口同时被 `daq` 和 `stream` 两个进程/时钟域使用。对 `ClkMem` 而言，它既是 DAQ 输出侧（`proc=daq` 的 `Daq_Vld`/`Daq_Data` 等）的时钟，也参与 stream 侧的跨时钟域同步（见 [u2-l2](u2-l2-input-interface-clocks.md) 讲的 `ClkMem` 域）。`proc` 字段是 PsiSim 给端口分组、生成波形与文档时用的归类信息。

---

## 5. 综合实践

**任务**：为一个**假想**的新模块 `psi_ms_daq_xxx`（以及它的 testbench）在 `sim/config.tcl` 里增加仿真声明，并完整推演 PsiSim 会如何编译、运行它，以及它失败时 `ciFlow.py` 的退出码。

> 提示：这是一个「源码阅读 + 配置编写」型实践，不需要你真的有这个模块。重点是练熟 `config.tcl` 的三组声明与运行配置。

**步骤 1 —— 声明源码（第 2 组 src）**

在 [config.tcl 第 2 组](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L45-L54) 的文件列表里，按依赖顺序追加（如果 `psi_ms_daq_xxx` 依赖 `psi_ms_daq_pkg`，就必须排在 `psi_ms_daq_pkg.vhd` 之后、顶层 `psi_ms_daq_axi.vhd` 之前或之后视是否被顶层例化而定）：

```tcl
# 示例代码 —— 假想在 config.tcl 第 2 组追加
add_sources "../hdl" {
    psi_ms_daq_pkg.vhd \
    ...
    psi_ms_daq_xxx.vhd \
    ...
} -tag src
```

**步骤 2 —— 声明 testbench（第 3 组 tb）**

在第 3 组追加 TB 文件（`tb_pkg` 必须在 `tb` 与 case 文件之前）：

```tcl
# 示例代码 —— 假想在 config.tcl 第 3 组追加
add_sources "../tb" {
    ...
    psi_ms_daq_xxx/psi_ms_daq_xxx_tb_pkg.vhd \
    psi_ms_daq_xxx/psi_ms_daq_xxx_tb_case_basic.vhd \
    psi_ms_daq_xxx/psi_ms_daq_xxx_tb.vhd \
} -tag tb
```

**步骤 3 —— 声明运行配置**

```tcl
# 示例代码 —— 假想在 config.tcl 运行配置段追加
create_tb_run "psi_ms_daq_xxx_tb"
add_tb_run
```

**步骤 4 —— 推演编译顺序**

回答：PsiSim 按 `compile_files -all` 时登记的 `-tag` 顺序编译，即 **lib → src → tb**。因此：

1. 先编译第 1 组 22 个依赖库文件（`psi_common_*`、`psi_tb_*`）；
2. 再编译第 2 组项目源码（`psi_ms_daq_pkg` 最先，`psi_ms_daq_xxx` 排在它之后，顶层最后）；
3. 最后编译第 3 组 testbench（`psi_ms_daq_xxx_tb_pkg` 先于 `psi_ms_daq_xxx_tb` 和 case 文件）。

如果 `psi_ms_daq_xxx.vhd` 被排在了 `psi_ms_daq_pkg.vhd` 之前，编译会因为找不到 `psi_ms_daq_pkg` 而失败。

**步骤 5 —— 推演退出码**

- 若 `psi_ms_daq_xxx_tb` 所有断言通过：日志不含 `###ERROR###`、含成功标志 → `ciFlow.py` 返回 **`0`**。
- 若 `psi_ms_daq_xxx_tb` 某断言失败（打印 `###ERROR###`）：`ciFlow.py` 返回 **`-1`**。
- 若 `psi_ms_daq_xxx.vhd` 有语法错误导致编译失败：`ci.do` 的 `onerror {exit}` 触发提前退出，日志不含成功标志 → `ciFlow.py` 返回 **`-2`**。

**验收**：你能口头复述「加一个新模块要改 config.tcl 哪三处、编译顺序是什么、三种失败各对应哪个退出码」，本实践即完成。

## 6. 本讲小结

- `sim/config.tcl` 是仿真「配料表」，用 `add_sources ... -tag lib/src/tb` 把依赖库（22 个）、项目源码（7 个）、testbench 分成三组，组的顺序即编译顺序；用 `create_tb_run` / `tb_run_add_arguments` / `add_tb_run` 配置每个 TB 的运行，本项目一次回归共跑 **10 次**仿真（input 6 次 + 其余 4 个各 1 次）。
- `sim/run.tcl` 是回归主流程：加载 PsiSim → `init` → `source config.tcl`（仅声明）→ `compile_files -all -clean`（编译）→ `run_tb -all`（运行）→ `run_check_errors "###ERROR###"`（扫描失败标志）。`source config.tcl` 本身不触发编译。
- `sim/interactive.tcl` 与 `run.tcl` 共享前四步，但**不调用 `run_tb`**，用于交互调试（编译完停在 Modelsim 控制台）。
- `sim/ci.do` 是 CI 入口的薄封装：`onerror {exit}` + `source run.tcl` + `quit`，任何 Tcl 错误（如编译失败）都提前退出。
- `scripts/ciFlow.py` 切到 `sim/` 目录，以 `vsim -batch -do ci.do` 跑批处理，读 `Transcript.transcript` 日志做两层判定：含 `###ERROR###` 返回 `-1`（用例失败），缺 `SIMULATIONS COMPLETED SUCCESSFULLY` 返回 `-2`（异常/编译失败），否则返回 `0`（通过）。
- 实体声明里的 `$$ ... $$` 元注解（`testcases`/`processes`/`tbpkg`/`constant`/`proc`/`type=clk`/`type=rst`）机器可读地描述了每个 DUT 的覆盖面、进程/时钟域、TB 依赖与端口归属；`testcases` 的值与 `tb/` 下 `*_tb_case_*.vhd` 文件名、`config.tcl` 第 3 组条目三方一一对应。

## 7. 下一步学习建议

本讲只讲了「仿真骨架」，没有进入任何 testbench 的内部实现。建议接下来：

1. **[u5-l2 模块级 testbench 实例分析：input/dma/sm](u5-l2-module-testbenches.md)**：以 input、daq_dma、daq_sm 三个模块的 testbench 为例，看 `*_tb_pkg.vhd` 如何提供 `ApplyStrData` / `CheckAcqData` 等激励与校验过程，case 文件如何按场景拆分，TB 如何用 `Generics_t` 跑多组参数化用例。
2. **[u5-l3 顶层多流 testbench：psi_ms_daq_axi_tb](u5-l3-toplevel-multistream-tb.md)**：看顶层 TB 如何用 AXI Slave 激励驱动寄存器、用 `str0~str3` 数据包模拟 4 路并发流、在共享内存模型中做端到端校验。

如果你对 PsiSim 框架本身的 `init` / `compile_files` / `run_tb` 内部实现好奇，可以去看依赖目录 `../../../TCL/PsiSim/PsiSim.tcl`（不在本仓库内），但那对本项目来说属于外部工具，理解到「声明式配置 + 框架执行」这一层已足够阅读和修改本项目所有仿真脚本。

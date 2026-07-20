# 仿真与回归测试运行方式

## 1. 本讲目标

学完本讲，你应该能够：

- 在 Modelsim（Vsim）中，从 `sim` 目录用 `source ./run.tcl` 跑通整个回归测试。
- 读懂 `run.tcl`、`config.tcl`、`ci.do`、`interactive.tcl` 四个 Tcl 脚本各自的职责与执行顺序。
- 理解 PsiSim 仿真框架如何把「声明库/源码/testbench → 编译 → 运行 → 查错」串成一条流水线。
- 解释 `scripts/ciFlow.py` 如何在命令行模式下驱动仿真，以及它用哪两个字符串判定成功与失败。

## 2. 前置知识

### 2.1 功能仿真与回归测试

FPGA 设计写的是 VHDL 硬件描述代码，但代码不能直接「运行」。**功能仿真（functional simulation）** 是用软件（仿真器）模拟这些硬件代码的行为，验证逻辑是否正确。

**回归测试（regression test）** 是一组固定的、可重复执行的测试。每次改完代码都重新跑一遍，确保「之前能通过的现在依然能通过」——也就是没有把老功能改坏。本项目的回归测试由 `tb/top_tb.vhd` 这个测试平台（testbench）承担。

### 2.2 Modelsim / Vsim

Modelsim 是 Mentor（现 Siemens EDA）公司的 HDL 仿真器。`vsim` 是它的核心可执行命令：图形界面下叫 Modelsim，命令行下（`vsim -c`）就是纯文字模式。本项目既支持图形界面交互（`interactive.tcl`），也支持命令行回归（`ci.do`）。

### 2.3 Tcl

Tcl（Tool Command Language）是一种脚本语言。Vivado、Modelsim 都内置 Tcl 解释器。本项目的仿真流程就是用一系列 `.tcl` 脚本驱动的。`source ./run.tcl` 表示「读取并执行 run.tcl 里的每一条命令」。

### 2.4 transcript（仿真日志）

仿真器运行时会把所有输出（编译信息、运行信息、断言报错）写进一个日志文件，叫 **transcript**。本项目 CI 判定成败，靠的就是扫描这份日志里的关键字符串。

### 2.5 仿真三步：编译 → 运行 → 查错

HDL 仿真大致分三步：

1. **编译（compile）**：把 `.vhd` 源码翻译成仿真库里的中间形式。
2. **运行（run）**：加载 testbench 顶层，跑一段仿真时间，testbench 里的检查会在失败时报错。
3. **查错（check）**：扫描日志，看有没有报错。

PsiSim 框架就是把这三步封装成 Tcl 命令的。

### 2.6 承接前两讲

- **u1-l2** 已指出仿真主线的入口：`tb/top_tb.vhd` 由 `sim/run.tcl` 启动；testbench 例化 `hdl/spi_vivado_wrp.vhd` 作为被测件（DUT）。
- **u1-l3** 已列出仿真所需依赖：TCL 层的 PsiSim（仿真框架），VHDL 层的 psi_common（运行期组件）、psi_tb（测试库）。这些依赖都被 `config.tcl` 引用，且要求 psi_fpga_all 那套固定目录结构。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
|---|---|---|
| `sim/run.tcl` | 29 | 回归测试主入口：加载 PsiSim → 初始化 → 读 config → 编译 → 运行 → 查错 |
| `sim/config.tcl` | 51 | 声明仿真要用到的库、源码、testbench 以及一次 testbench run |
| `sim/ci.do` | 8 | CI 用的 do 文件：执行 run.tcl 后立刻 quit |
| `sim/interactive.tcl` | 19 | 图形界面交互脚本：只做初始化和编译，把控制权留给用户 |
| `scripts/ciFlow.py` | 27 | Python 脚本：命令行调用 vsim 跑 ci.do，再扫描 transcript 判定退出码 |
| `sim/.gitignore` | 16 | 忽略仿真生成的库文件与 transcript |

## 4. 核心概念与源码讲解

### 4.1 PsiSim 仿真框架与 run.tcl 流程

#### 4.1.1 概念说明

PsiSim 是 PSI 提供的一个 Tcl 仿真框架（u1-l3 提到的 TCL 层依赖之一）。它本身**不是**一个仿真器，而是 Modelsim 之上的一层「胶水」：把「建库、加源码、编译、运行、查错」这些原本需要手敲一长串 Modelsim 命令的操作，封装成 `psi::sim::` 命名空间下的一组简短命令。

`run.tcl` 是回归测试的主入口。README 明确告诉我们：在 Modelsim 里、在 `sim` 目录下执行 `source ./run.tcl` 即可。

#### 4.1.2 核心流程

`run.tcl` 的执行是一条直线流水线，伪代码如下：

```
加载 PsiSim 框架          # source PsiSim.tcl
初始化仿真                # psi::sim::init
读配置                    # source config.tcl
编译所有源码（先清旧）     # psi::sim::compile -all -clean
运行所有 testbench run    # psi::sim::run_tb -all
扫描日志查错              # psi::sim::run_check_errors "###ERROR###"
```

关键点：

- `-clean` 表示编译前先清掉旧的编译产物，保证回归可重复。
- `-all` 表示编译/运行所有声明的源码和 testbench run。
- `run_check_errors` 接收一个字符串 `"###ERROR###"`：只要 transcript 里出现这个串，就算失败。

#### 4.1.3 源码精读

run.tcl 第一件事是加载 PsiSim 框架本体：

[加载 PsiSim 框架 - sim/run.tcl:L7-L8](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/run.tcl#L7-L8)

注意路径 `../../../TCL/PsiSim/PsiSim.tcl`：从 `sim/` 往上三级到仓库聚合根（u1-l3 说的 psi_fpga_all 目录结构），再进 `TCL/PsiSim/`。这正是「固定目录结构」的体现——脚本靠相对路径找依赖。

接着初始化，然后加载配置：

[初始化并加载 config.tcl - sim/run.tcl:L10-L14](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/run.tcl#L10-L14)

`psi::sim::init` 清空 PsiSim 的内部状态，准备开始一次新的仿真流程；`source ./config.tcl` 把「用什么库、哪些源码、哪个 testbench」的声明加载进来（见 4.2）。

然后是编译、运行、查错三步：

[编译-运行-查错三步 - sim/run.tcl:L16-L29](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/run.tcl#L16-L29)

中间穿插的 `puts` 只是打印分隔横幅（`-- Compile` / `-- Run` / `-- Check`），让输出更易读，不影响逻辑。

#### 4.1.4 代码实践

实践目标：手动复述 run.tcl 的完整执行顺序，确认你理解每一步做了什么。

操作步骤：

1. 打开 `sim/run.tcl`，从第 7 行读到第 29 行。
2. 用一张表把每条 `psi::sim::*` 命令及其紧邻的注释对应起来。
3. 在脑子里「单步执行」一遍：先 init，再 source config，再 compile，再 run_tb，最后 run_check_errors。

需要观察的现象：你应该能说清楚，为什么 `run.tcl` 必须先 `source config.tcl` 才能 `compile`——因为编译的对象（源码清单）正是在 config.tcl 里声明的。

预期结果：你能不查文件，按顺序默写出六个步骤：`source PsiSim` → `init` → `source config` → `compile -all -clean` → `run_tb -all` → `run_check_errors "###ERROR###"`。

如果你本机装了 Modelsim 且按 u1-l3 搭好了 psi_fpga_all 目录结构，可在 `sim/` 下 `source ./run.tcl` 实跑一次，观察 transcript 里逐条打印的横幅；无法运行则按上表阅读即可，不强制。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `run.tcl` 第 14 行的 `source ./config.tcl` 删掉，`compile -all` 会发生什么？
**答案**：compile 找不到任何源码清单（库、源码、testbench 都是在 config.tcl 里声明的），编译对象为空，回归测不到任何东西。

**练习 2**：`run_check_errors "###ERROR###"` 里的字符串为什么要和 testbench 报错时打印的串一致？
**答案**：这是「契约」——PsiSim 扫描 transcript 是否出现该串来判断成败。psi_tb 的检查宏在断言失败时打印 `###ERROR###`，二者必须匹配，否则 CI 永远查不到失败。

**练习 3**：`-clean` 参数去掉会怎样？
**答案**：可能残留旧的编译产物，导致回归结果不可重复（改了代码但旧库没重建）。回归测试要求每次从干净状态开始。

---

### 4.2 config.tcl 的库/源码/run 声明

#### 4.2.1 概念说明

`config.tcl` 是一份「仿真清单」。它告诉 PsiSim：

- 把仿真库叫什么名字；
- 抑制哪些无关紧要的告警信息；
- 要编译哪些第三方库源码（psi_common、psi_tb）；
- 要编译哪些本项目源码；
- 要把哪个 testbench 作为一次 run 来跑。

它本身不做编译和运行，只做「声明」；真正消费这些声明的是 run.tcl 里的 `compile` / `run_tb` 命令。

#### 4.2.2 核心流程

config.tcl 的组织逻辑：

```
设置公共变量 LibPath = "../../.."
导入 psi::sim 命名空间
新建仿真库 spi_simple
声明需要抑制的编译/运行期告警
add_sources：psi_common     (tag lib)
add_sources：psi_tb         (tag lib)
add_sources：本项目 RTL     (tag src)
add_sources：本项目 testbench (tag tb)
create_tb_run "top_tb"  ->  add_tb_run
```

`-tag` 是给一组源码打标签（lib/src/tb），方便后续按标签批量操作，也让人一眼看清来源。`add_tb_run` 把「一次 testbench 运行」登记进 PsiSim 的 run 队列，run.tcl 里的 `run_tb -all` 就是把这个队列全跑一遍。

#### 4.2.3 源码精读

首先是公共变量与命名空间导入：

[LibPath 与命名空间导入 - sim/config.tcl:L7-L9](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/config.tcl#L7-L9)

`LibPath "../../.."` 与 run.tcl 里 `../../../TCL/PsiSim/` 同理：从 sim 向上三级到聚合根。下面的依赖源码路径 `$LibPath/VHDL/psi_common/hdl` 就是 u1-l3 说的 psi_common 运行期依赖。

然后建库与抑制告警：

[建库与抑制告警 - sim/config.tcl:L11-L16](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/config.tcl#L11-L16)

`compile_suppress` / `run_suppress` 后面跟的是 Modelsim 的告警编号（如 135、8684）。这些是第三方库里常见的、对本项目无意义的告警，抑制掉能让 transcript 更干净，避免淹没真正的错误。

接着是三组源码声明。先看第三方库 psi_common：

[psi_common 源码声明 - sim/config.tcl:L18-L28](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/config.tcl#L18-L28)

注意这里只挑了 psi_common 的部分文件（`psi_common_spi_master.vhd`、`psi_common_axi_slave_ipif.vhd`、`psi_common_sync_fifo.vhd` 等）——也就是本项目 RTL 真正用到的运行期组件。psi_tb 同理：

[psi_tb 源码声明 - sim/config.tcl:L30-L35](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/config.tcl#L30-L35)

这里挑了文本工具（`psi_tb_txt_util`）、比较包（`psi_tb_compare_pkg`）、AXI 包（`psi_tb_axi_pkg`），是 testbench 写断言和驱动 AXI 总线要用的。

然后是本项目自己的 RTL（综合主线那三个文件，u1-l2 已介绍）：

[本项目 RTL 源码声明 - sim/config.tcl:L37-L42](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/config.tcl#L37-L42)

和 testbench：

[testbench 声明 - sim/config.tcl:L44-L47](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/config.tcl#L44-L47)

最后，把 top_tb 登记为一次 run：

[创建并登记一次 testbench run - sim/config.tcl:L49-L51](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/config.tcl#L49-L51)

#### 4.2.4 代码实践

实践目标：搞清楚「一份源码从哪个目录来、打了什么标签」。

操作步骤：

1. 打开 config.tcl，画一张表：目录来源 | 文件列表 | tag。
2. 对照 u1-l3 的依赖清单，确认 psi_common、psi_tb 各自的目录路径与 u1-l3 描述一致。
3. 找出哪些 psi_common 文件被仿真实际编译，哪些没被编译（即整个 psi_common 库里没出现在清单中的文件）。

需要观察的现象：被编译的 psi_common 文件，恰好是 `spi_simple` / `spi_vivado_wrp` 顶层用到的（spi_master、axi_slave_ipif、sync_fifo、sdp_ram、pl_stage、各 pkg）。

预期结果：你能解释「为什么 config.tcl 只列 8 个 psi_common 文件而不是整个库」——因为只编译用得到的，加快回归、减少无关告警。

#### 4.2.5 小练习与答案

**练习 1**：`tag lib`、`tag src`、`tag tb` 三种标签分别给谁用？
**答案**：lib 给第三方库（psi_common、psi_tb），src 给本项目 RTL，tb 给 testbench。标签方便后续按类别批量操作，也让人一眼看清来源。

**练习 2**：如果给 spi_simple 增加了一个新的 VHDL 源文件 `spi_extra.vhd`，config.tcl 要改哪里？
**答案**：在 `tag src` 那组 `add_sources "../hdl"` 的文件列表里加上 `spi_extra.vhd`，否则它不会被编译进仿真库。

**练习 3**：`compile_suppress 135,1236` 去掉会怎样？
**答案**：transcript 里会多出大量第三方库的无关告警，真正的 `###ERROR###` 更容易被淹没，不利于查错。

---

### 4.3 CI 自动化与结果判定

#### 4.3.1 概念说明

「CI」即持续集成（Continuous Integration）：每次提交代码，机器自动跑一遍回归，用**退出码（exit code）** 告诉人「通过/失败」。本项目的 CI 由三个文件协作完成：

- `ci.do`：一段最短的 Modelsim do 文件，跑完 run.tcl 后自动退出。
- `ciFlow.py`：Python 编排脚本，命令行调用 vsim，再读 transcript 判定退出码。

人也可以直接 `source ./run.tcl` 在图形界面里看输出；CI 只是把这件事自动化、并用退出码给出机器可读的结论。

#### 4.3.2 核心流程

CI 的完整流程：

```
ciFlow.py:
  chdir 到 sim/
  调用 vsim -c -do ci.do         # 命令行模式，执行 ci.do
  打开并读取 Transcript.transcript
  若含 "###ERROR###"             -> exit(-1)   # 有断言失败
  若不含 "SIMULATIONS COMPLETED SUCCESSFULLY" -> exit(-2)  # 没正常跑完
  否则                            -> exit(0)    # 成功

ci.do:
  source run.tcl                  # 执行整个回归
  quit                            # 退出 vsim
```

退出码的含义可用分段函数表示：

\[
\text{exit\_code} =
\begin{cases}
-1 & \text{transcript 含 } \texttt{###ERROR###} \\
-2 & \text{transcript 不含成功串} \\
0 & \text{否则（成功）}
\end{cases}
\]

判定顺序很重要：先查「有没有期望内的测试失败」（`###ERROR###`），再查「是不是正常跑完了」（成功串）。一个正常通过的项目里，transcript 应当既没有 `###ERROR###`、又包含成功串。

#### 4.3.3 源码精读

先看 ci.do，它只有两行有效内容：

[ci.do：跑完即退 - sim/ci.do:L7-L8](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/ci.do#L7-L8)

`source run.tcl` 执行完整回归（init → compile → run → check）。最后一行 `quit` 是关键：`vsim -c` 命令行模式下，跑完 do 文件后 vsim 不会自动退出，会停在交互提示符等待输入，这会让 CI 永远卡住。所以必须显式 `quit` 把控制权交还给 ciFlow.py。

再看 ciFlow.py 如何编排：

[ciFlow.py：切目录并命令行跑仿真 - scripts/ciFlow.py:L9-L13](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L9-L13)

`os.chdir` 让脚本无论从哪里启动，都切到 `sim/` 目录（因为 ci.do 和 run.tcl 用的是相对路径）。`vsim -c -do ci.do` 以命令行（`-c`）模式启动 vsim 并执行 ci.do。

接着读取 transcript 并判定。首先是「期望错误」（即测试断言失败）：

[判定 1：断言失败 - scripts/ciFlow.py:L15-L20](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L15-L20)

注意读取的文件名是 `Transcript.transcript`（大写 T、带 `.transcript` 扩展名），这是 PsiSim 框架输出的日志，和 Modelsim 默认的小写 `transcript` 不同（`sim/.gitignore` 同时忽略了 `*.transcript` 和 `transcript` 两种）。只要里面出现 `###ERROR###`（来自 psi_tb 检查失败），就 `exit(-1)`。

然后是「意外错误」（没正常跑完）：

[判定 2：是否正常跑完 - scripts/ciFlow.py:L21-L23](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L21-L23)

PsiSim 在所有 testbench run 跑完后会打印 `SIMULATIONS COMPLETED SUCCESSFULLY`。如果 transcript 里没这条，说明仿真中途崩溃或被中断，`exit(-2)`。

最后是成功：

[成功退出 - scripts/ciFlow.py:L26-L27](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L26-L27)

补充：交互模式脚本 interactive.tcl 与 ci.do 形成对照——它只做初始化和编译，**不运行、不退出**，把控制权留给坐在图形界面前的用户：

[interactive.tcl：只初始化+编译 - sim/interactive.tcl:L10-L19](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/interactive.tcl#L10-L19)

注意它用了 `namespace import psi::sim::*`（第 12 行），所以后面能直接写 `init`、`compile_files` 而不带 `psi::sim::` 前缀；它调用的是 `compile_files`（编译）而不是 `run_tb`，因此停在「随时可以手动 run」的状态。

#### 4.3.4 代码实践

实践目标：走查 CI 判定逻辑，并用具体 transcript 内容推演退出码。

操作步骤：

1. 打开 `sim/ci.do`，确认它就两件事：`source run.tcl` + `quit`。
2. 打开 `scripts/ciFlow.py`，把两个 `if` 和最终 `exit(0)` 对应的退出码 -1 / -2 / 0 标出来。
3. 设想三种 transcript 内容，分别推演退出码：
   - (a) 含 `###ERROR###`，也含成功串；
   - (b) 不含 `###ERROR###`，含成功串；
   - (c) 不含 `###ERROR###`，也不含成功串（仿真中途崩溃）。

需要观察的现象：因为「判定 1」先于「判定 2」执行，情况 (a) 的退出码是 -1 而不是 0。

预期结果：(a) → -1，(b) → 0，(c) → -2。

#### 4.3.5 小练习与答案

**练习 1**：为什么 ci.do 末尾必须有 `quit`？
**答案**：`vsim -c` 跑完 do 文件后会停在交互提示符等待输入；没有 `quit`，ciFlow.py 里的 `os.system("vsim ...")` 会一直阻塞，CI 永远不返回。

**练习 2**：如果某次仿真既没有 `###ERROR###`，也没有 `SIMULATIONS COMPLETED SUCCESSFULLY`（比如 vsim 启动就报错退出），退出码是多少？代表什么？
**答案**：退出码 -2，代表「意外错误」——仿真没有正常跑完（可能编译失败、vsim 报错、脚本异常）。

**练习 3**：ciFlow.py 为什么先 `os.chdir` 到 sim 再跑，而不是直接用绝对路径调 ci.do？
**答案**：因为 run.tcl、config.tcl 内部大量使用相对路径（`./config.tcl`、`../hdl`、`../../../TCL/PsiSim`），只有工作目录是 sim 时这些相对路径才正确。切目录是最简单可靠的办法。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿任务（即本讲规格要求的实践）：

**任务**：从 `sim/run.tcl` 出发，画出「人在图形界面手动跑」和「CI 自动跑」两条路径的完整时序，并解释 ci.do 为什么会自动退出。

要求产出一个流程图（文字版即可），至少包含以下节点：

1. **人工路径**：Modelsim 图形界面 → `cd sim` → `source ./run.tcl` →（init → source config → compile → run_tb → run_check_errors）→ 人看 transcript。
2. **CI 路径**：`python scripts/ciFlow.py` → `chdir sim` → `vsim -c -do ci.do` → `ci.do` 内 `source run.tcl`（同上五步）→ `quit` 退出 vsim → 读 `Transcript.transcript` → 按 `###ERROR###` / 成功串判定 → 退出码 -1/-2/0。

并回答两个问题：

- 两条路径在哪一步汇合？（提示：都在 `source run.tcl` 这一步执行同一套 PsiSim 流水线。）
- ci.do 为什么会自动退出？（提示：因为它末尾有 `quit`；`-c` 命令行模式不会自己退出，必须显式 quit。）

如果你本机没有 Modelsim，本任务作为**源码阅读型实践**完成即可，不要假装运行过命令。

## 6. 本讲小结

- 回归测试入口是 `sim/run.tcl`，在 Modelsim 的 `sim` 目录下 `source ./run.tcl` 即可跑通。
- `run.tcl` 是一条直线流水线：加载 PsiSim → init → source config → `compile -all -clean` → `run_tb -all` → `run_check_errors "###ERROR###"`。
- `config.tcl` 是仿真清单：声明库、抑制告警、列第三方库（psi_common/psi_tb）与本项目 RTL（src）和 testbench（tb）、登记一次 top_tb run。
- `interactive.tcl` 是图形界面交互版，只做 init + 编译，不运行、不退出，把控制权留给用户。
- `ci.do` 只有两行：`source run.tcl` + `quit`；`quit` 是命令行模式自动返回的关键。
- `ciFlow.py` 切到 sim、用 `vsim -c -do ci.do` 跑回归，再读 `Transcript.transcript`：含 `###ERROR###` 退 -1，不含成功串退 -2，否则退 0。

## 7. 下一步学习建议

- 进入第二单元（进阶层）前，建议先翻一下 `tb/top_tb.vhd` 的开头，看看 testbench 是怎么例化 `spi_vivado_wrp` 的——这正是 config.tcl 里 `tag tb` 那个文件。
- 下一讲 **u2-l1「寄存器地图与常量定义包」** 将打开 `hdl/definitions_pkg.vhd`，开始真正的 RTL 源码阅读；届时你会理解 testbench 里那些 AXI 读写地址的含义。
- 如果想深入了解 PsiSim 命令（`init` / `compile` / `run_tb` / `run_check_errors`）的内部实现，可去聚合仓库的 `TCL/PsiSim/` 目录阅读 PsiSim.tcl（超出本讲范围）。

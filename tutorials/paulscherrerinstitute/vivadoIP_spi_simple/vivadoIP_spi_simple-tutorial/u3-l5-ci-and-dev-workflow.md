# CI、回归与开发工作流

## 1. 本讲目标

本讲是专家层的最后一讲，也是整个学习手册的收尾。前面 u1-l4 已经带你跑通过一次 `source ./run.tcl`，u3-l4 也讲清楚了如何把 RTL 打包成 Vivado IP。本讲把这两条线缝合成一条**可重复的工程化工作流**：当你改了 `spi_simple` 的哪怕一行代码，从「拿到干净代码」到「CI 给出绿勾/红叉」之间，到底依次跑了哪些脚本、靠什么判定成败。

学完后你应当能够：

1. 说清 CI 回归的三层结构（Python 编排器 → Tcl do 文件 → PsiSim 回归），以及三个退出码 `0 / -1 / -2` 各自代表什么。
2. 说清 transcript 中**哪两个字符串**决定了退出码，以及判定的先后顺序。
3. 解释 `dependencies.py` 如何以 README 为唯一数据源解析依赖，为什么真正的逻辑不在本仓库里。
4. 评估 `scripts/refactoring/` 下重构脚本的用途、能力边界与风险，知道什么情况下该用、什么情况下别碰。

---

## 2. 前置知识

本讲默认你已经读过：

- **u1-l4 仿真与回归测试运行方式**：PsiSim 框架、`run.tcl` 的 `init → compile → run_tb → run_check_errors` 流水线、`config.tcl` 用 `-tag` 给源码分组的声明式写法。
- **u3-l4 IP 打包与发布流程**：`package.tcl` 作为唯一数据源（SSOT）、`component.xml` 的 IP-XACT 结构、PSI FPGA 库家族（`psi_common` / `psi_tb` / `PsiSim` / `PsiIpPackage`）。

此外，几个本讲会用到的通用概念，先一句话铺垫：

- **CI（持续集成，Continuous Integration）**：每次提交代码，机器自动跑一遍编译+测试，给出「通过/失败」的客观结论，避免「我本地能跑」的自欺欺人。
- **退出码（exit code）**：进程结束时返回给操作系统的整数。惯例是 `0` 表示成功，非 `0` 表示失败。CI runner（如 GitHub Actions）就是靠这个数决定任务标绿还是标红。
- **transcript**：Modelsim/Questa 仿真时写出的文本日志。本项目的成败判定不读任何结构化返回值，而是**全文搜 transcript 里的字符串**——这是 Tcl 世界和 Python 世界之间唯一的契约面。
- **子进程（subprocess）**：一个程序启动另一个程序。本讲里 `ciFlow.py`（Python）会启动 `vsim`（Tcl 解释器）作为子进程。
- **批处理模式（batch mode）**：`vsim -c` 让仿真器不弹 GUI、纯命令行运行，靠 `-do` 给的脚本驱动，跑完不自动退出（所以要手动 `quit`）。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用它讲什么 |
| --- | --- | --- |
| [scripts/ciFlow.py](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py) | CI 编排器（27 行 Python） | 切目录、起 vsim、读 transcript、定退出码 |
| [sim/ci.do](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/ci.do) | 传给 `vsim -do` 的入口脚本 | 在批处理下 source 回归脚本并 `quit` |
| [sim/run.tcl](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/run.tcl) | 回归主体（人/CI 共用） | PsiSim 四步流水线 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/config.tcl) | 仿真清单声明 | 库/源码/testbench/run 的声明 |
| [scripts/dependencies.py](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/dependencies.py) | 依赖获取薄客户端（16 行） | 从 README 解析依赖并拉取 |
| [README.md](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md) | 项目说明 | 依赖段的解析边界注释 |
| [scripts/refactoring/refactor_library_and_testbench.py](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/refactor_library_and_testbench.py) | 重构驱动脚本 | 把改名批量应用到 hdl/tb/config.tcl |
| [scripts/refactoring/hdlrefactor.py](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py) | 重构函数库 | 实体/例化/符号/Tcl generic 的正则改写 |
| [scripts/refactoring/alpha.json](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/alpha.json) | 改名数据库 | 旧名 → 新名的映射表 |

---

## 4. 核心概念与源码讲解

本讲的三个最小模块对应开发工作流的三个环节：**回归自检（CI）→ 准备依赖（依赖解析）→ 一次性大批量改名（重构脚本）**。三者都是「写一次、反复用」的工程基础设施。

### 4.1 CI 回归与结果判定

#### 4.1.1 概念说明

u1-l4 已经让你在 Modelsim 里手动 `source ./run.tcl` 跑过回归。但 CI 环境里没有人在终端前敲命令——需要一台机器**无人值守地**完成三件事：

1. **启动仿真器并跑完回归**（不能弹 GUI，不能停下来等输入）。
2. **把「跑得好不好」从 Tcl 世界搬到 Python/Shell 世界**（CI runner 只认退出码，不认 Tcl 变量）。
3. **给出明确的退出码**：成功、测试失败、或者「根本没跑起来」要能区分。

本项目用一个三层结构解决它：

| 层 | 文件 | 语言 | 职责 |
| --- | --- | --- | --- |
| 编排器 | `scripts/ciFlow.py` | Python | 切到 sim 目录、起 vsim、读 transcript、定退出码 |
| 入口 | `sim/ci.do` | Tcl | 在批处理模式下 `source run.tcl` 然后 `quit` |
| 主体 | `sim/run.tcl` | Tcl | PsiSim 的 init/compile/run/check 流水线 |

关键设计：**run.tcl 是人和 CI 共用的同一份脚本**，CI 只是在外面包了一层「自动退出」的 `ci.do` 和一层「判定退出码」的 `ciFlow.py`。这样手动调试和 CI 验证永远不会跑出不一样的结果。

#### 4.1.2 核心流程

CI 一次回归的执行链：

```
CI runner 调用:  python scripts/ciFlow.py
        │
        ▼
[ciFlow.py]  os.chdir(sim 目录)          ← 让后续相对路径生效
        │
        ▼
[ciFlow.py]  os.system("vsim -c -do ci.do")   ← 启动批处理仿真器
        │
        ▼
[ci.do]      source run.tcl                ← 进入回归主体
        │
        ▼
[run.tcl]    PsiSim: init → config → compile → run_tb → run_check_errors
        │        └─ 把断言失败写成 "###ERROR###" 到 transcript
        │        └─ 跑完写 "SIMULATIONS COMPLETED SUCCESSFULLY"
        ▼
[ci.do]      quit                          ← 关键！让 vsim 退出，否则子进程挂起
        │
        ▼
[ciFlow.py]  读 Transcript.transcript，按两个字符串判定退出码
        │
        ▼
退出码 0 / -1 / -2
```

退出码的判定逻辑（三选一，**按顺序短路**）：

| 优先级 | 条件 | 退出码 | 含义 |
| --- | --- | --- | --- |
| 1 | transcript 含 `###ERROR###` | `-1` | 回归里有断言失败 / 显式报错 |
| 2 | transcript **不含** `SIMULATIONS COMPLETED SUCCESSFULLY` | `-2` | 仿真没正常跑完（编译错、崩溃、被超时杀） |
| 3 | 其它 | `0` | 成功 |

判定顺序很重要：如果 transcript 里**两个字符串都在**（既报了错又印了完成串），`-1` 优先——因为只要存在任何 `###ERROR###`，就应当判定失败，不能被「跑完了」掩盖。

#### 4.1.3 源码精读

先看编排器 `ciFlow.py` 全貌，它只有 27 行，但每一行都不可省：

切到 sim 目录——这一行是所有相对路径的锚点：

[scripts/ciFlow.py:9-13](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L9-L13) — `THIS_DIR` 求出 `scripts/` 的绝对路径，`os.chdir` 进 `../sim`，然后才 `os.system("vsim -c -do ci.do")`。`ci.do` 是相对名，只有 CWD 在 sim 下才找得到。

启动批处理仿真器：

[scripts/ciFlow.py:13](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L13) — `vsim -c` 是无 GUI 的命令行模式；`-do ci.do` 让仿真器启动后立刻执行 `ci.do`。注意 `os.system` 会**阻塞**直到子进程退出，所以 ciFlow.py 自然等仿真跑完。

读 transcript：

[scripts/ciFlow.py:15-16](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L15-L16) — 文件名是 `Transcript.transcript`（大写 T、`.transcript` 扩展名），这是 PsiSim 约定的日志文件名，也是 Tcl 与 Python 之间的契约面。

判定退出码（核心）：

[scripts/ciFlow.py:19-23](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L19-L23) — 先查 `###ERROR###` 命中则 `exit(-1)`；否则查成功串缺失则 `exit(-2)`。这两处 `exit` 就是「transcript 里哪两个字符串决定退出码」的答案。

成功路径：

[scripts/ciFlow.py:26](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L26) — 都不命中，`exit(0)`。

再看入口 `ci.do`，只有两行、但第二行是 CI 能自动返回的关键：

[sim/ci.do:7-8](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/ci.do#L7-L8) — `source run.tcl` 复用人/CI 共用的回归主体；`quit` 让 `vsim -c` 子进程退出。**没有 `quit`，`vsim -c` 跑完会停在 vsim 命令提示符等待输入，CI 会一直挂起直到超时被杀**（此时 transcript 里没有成功串，ciFlow.py 会判 `-2`，但真正原因是挂起而非崩溃——这是一个容易误判的坑）。

最后是回归主体 `run.tcl` 的四步流水线（u1-l4 已细讲，这里只点出与 CI 判定相关的两处）：

[sim/run.tcl:24](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/run.tcl#L24) — `run_tb -all` 跑完所有 testbench run；成功跑完时 PsiSim 会往 transcript 写 `SIMULATIONS COMPLETED SUCCESSFULLY`（即 ciFlow.py 查的成功串）。

[sim/run.tcl:29](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/run.tcl#L29) — `run_check_errors "###ERROR###"` 扫描 transcript 里是否出现 `###ERROR###`；这个标记由 testbench 里的断言失败（如 `StdlvCompareStdlv`）产生，正是 ciFlow.py 查的失败串。

于是退出码的两个决定性字符串就闭环了：

| 字符串 | 由谁产生 | ciFlow.py 据此判 |
| --- | --- | --- |
| `###ERROR###` | `run_check_errors` 扫描到的断言失败标记 | 命中 → `-1` |
| `SIMULATIONS COMPLETED SUCCESSFULLY` | PsiSim 跑完所有 run 后打印 | 缺失 → `-2` |

#### 4.1.4 代码实践

**实践目标**：不启动 vsim，纯靠阅读 `ciFlow.py` 的判定逻辑，预测三种 transcript 内容对应的退出码，从而验证你真的理解了「两个字符串决定退出码」。

**操作步骤**：

1. 打开 `scripts/ciFlow.py`，定位第 19–26 行的三段 `if/exit`。
2. 假设你在 sim 目录下手动构造了三份 `Transcript.transcript`（仅思考，不必真跑）：
   - **(a)** 内容含一行 `###ERROR###`，也含 `SIMULATIONS COMPLETED SUCCESSFULLY`。
   - **(b)** 内容只含 `SIMULATIONS COMPLETED SUCCESSFULLY`，没有 `###ERROR###`。
   - **(c)** 内容两者都没有（例如只有一堆编译报错信息）。
3. 对每份逐行套用 ciFlow.py 的 if 顺序，写出退出码。

**需要观察的现象 / 预期结果**：

- (a) → `-1`。因为第 19 行先命中 `###ERROR###` 短路退出，成功串虽在但不被检查。
- (b) → `0`。第 19 行不命中，第 22 行成功串存在故条件为假，落到第 26 行 `exit(0)`。
- (c) → `-2`。第 19 行不命中，第 22 行成功串缺失故 `not in` 为真，`exit(-2)`。

> 待本地验证：若你已装好 Modelsim/Questa 与 PsiSim，可在 sim 下 `python ../scripts/ciFlow.py` 实跑，用 `echo $?`（bash）查看退出码与本预测是否一致；未装环境时本实践为「源码阅读型」，结论已由源码逻辑确定。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ci.do` 里的 `quit` 对 CI 至关重要，删掉它会发生什么？

> **答案**：`vsim -c` 跑完 `-do` 指定的脚本后，不会自动退出，而是停在 vsim 命令提示符等待输入。CI 里无人交互，子进程会一直挂起直到被 runner 超时强杀。此时 transcript 里既没有成功串（也没机会判），ciFlow.py 会判 `-2`，但真正原因不是崩溃而是挂起——排查时容易被误导。`quit` 强制让 vsim 干净退出，`os.system` 才能返回。

**练习 2**：如果 testbench 既有断言失败、PsiSim 又照常打印了完成串，CI 最终退出码是多少？为什么不是 `-2`？

> **答案**：`-1`。因为 ciFlow.py 第 19 行的 `###ERROR###` 检查在前，命中即 `exit(-1)` 短路，根本不会走到第 22 行的成功串检查。设计上「只要存在错误就判失败」优先于「是否跑完」。

**练习 3**：`ciFlow.py` 为什么必须先 `os.chdir` 到 sim 目录，而不是直接 `os.system("vsim -c -do sim/ci.do")`？

> **答案**：因为 `ci.do`、`run.tcl`、`config.tcl` 全部用**相对路径**互相 source（`ci.do` 写 `source run.tcl`，`run.tcl` 写 `source ./config.tcl`），且 transcript 写到 CWD。这些相对路径都以「CWD = sim」为前提。若不 chdir，`source run.tcl` 在 `sim/ci.do` 执行时会在错误的 CWD 找不到 `run.tcl`。chdir 把工作目录锚定到 sim，所有相对路径才一致生效。

---

### 4.2 依赖解析机制

#### 4.2.1 概念说明

u1-l3 讲过本项目不是孤岛，它依赖 PSI FPGA 库家族的四个外部仓库（`PsiSim` / `PsiIpPackage` / `psi_common` / `psi_tb`）。问题是：**这份依赖清单写在哪、谁来读？**

最直觉的做法是在某个脚本里硬编码一份依赖数组。但本项目反其道而行——**README 才是唯一数据源（SSOT）**，依赖脚本不硬编码任何依赖，而是去解析 README 里那段给人看的依赖说明。好处是：人读到的依赖列表和脚本拉取的依赖列表**永远不可能不一致**，因为它们是同一份文本。

这套机制由两部分组成：

- **README 里的两行 HTML 注释**：作为解析边界，圈出「这一段是机器可解析的依赖清单」。
- **`scripts/dependencies.py`**：一个 16 行的薄客户端，把解析和拉取的真实逻辑委托给外部 Python 包 `PsiFpgaLibDependencies`。

#### 4.2.2 核心流程

```
人写 README 的依赖段（夹在两行 HTML 注释之间）
        │
        ▼
[dependencies.py]  Parse.FromReadme(README.md)
        │            └─ 找到两行注释边界，解析 markdown 列表
        ▼
        得到 dependencies 对象（仓库 + 版本 + 是否仅开发用）
        │
        ▼
[dependencies.py]  Actions.ExecMain(repo, dependencies)
        │            └─ 外部包 PsiFpgaLibDependencies 真正执行 clone/checkout
        ▼
依赖被放到正确目录（TCL/、VHDL/ 等固定结构）
```

要点：

- **README 是 SSOT**：改依赖只改 README，脚本零改动。
- **逻辑外置**：解析算法和拉取动作都不在本仓库，而在须预先 `pip install` 的 `PsiFpgaLibDependencies` 包里（1.1.0 版本引入，见 Changelog）。本仓库的 `dependencies.py` 只负责「指路」。

#### 4.2.3 源码精读

先看 README 的解析边界——这两行 HTML 注释既是给人看的提示，也是机器解析的哨兵：

[README.md:18](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L18) — `<!-- DO NOT CHANGE FORMAT: this section is parsed to resolve dependencies -->` 是**起始哨兵**，告诉解析器「从下一行开始读」。

[README.md:35](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L35) — `<!-- END OF PARSED SECTION -->` 是**终止哨兵**。两行之间（19–34 行）就是被解析的依赖清单。

夹在中间的依赖清单（人读 + 机器读，同一份文本）：

[README.md:26-33](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L26-L33) — 四个外部依赖：`PsiSim`（≥2.1.0，仅开发）、`PsiIpPackage`（2.0.0，仅开发）、`psi_common`（≥3.0.0，**运行期必需**）、`psi_tb`（≥3.0.0，仅开发）。注意只有 `psi_common` 是使用 IP 时也必需的运行期依赖，其余三者标注 `for development only`。

再看薄客户端 `dependencies.py` 全 16 行：

[scripts/dependencies.py:7](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/dependencies.py#L7) — `from PsiFpgaLibDependencies import *`：把外部包的 `Parse`、`Actions` 等名字全部导入。本仓库**不实现任何解析逻辑**。

定位 README 与本仓库根：

[scripts/dependencies.py:11-14](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/dependencies.py#L11-L14) — `THIS_DIR` 是 `scripts/`，所以 `../README.md` 指向仓库根的 README；`repo` 是仓库根的绝对路径，作为依赖拉取的基准目录。

解析 + 执行（全部两行）：

[scripts/dependencies.py:13-16](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/dependencies.py#L13-L16) — `Parse.FromReadme` 读 README、按哨兵切段、解析 markdown 列表，返回依赖对象；`Actions.ExecMain(repo, dependencies)` 把每个依赖 clone/checkout 到 `repo` 之外的固定结构目录里（如 `TCL/PsiSim`、`VHDL/psi_common`，与 run.tcl 里 `source ../../../TCL/PsiSim/PsiSim.tcl` 的路径呼应）。

使用方式 README 自己也写明了：

[README.md:37-43](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L37-L43) — `python dependencies.py -help` 看帮助；并明确「必须先安装 `PsiFpgaLibDependencies` 包」才能跑——因为真正的逻辑在那里。

#### 4.2.4 代码实践

**实践目标**：验证你理解了「README 是依赖解析的 SSOT」，并能区分运行期依赖与开发期依赖。

**操作步骤**：

1. 打开 `README.md`，找到第 18 行和第 35 行的两行 HTML 注释，确认它们圈住了 26–33 行的依赖清单。
2. 打开 `scripts/dependencies.py`，确认它**没有**出现 `PsiSim` / `psi_common` 等任何依赖名字——所有依赖都来自 README。
3. 做一个思想实验：如果把第 35 行 `<!-- END OF PARSED SECTION -->` 删掉，解析器（`Parse.FromReadme`）会读到什么？
4. 再做一个思想实验：如果要把 `psi_common` 的最低版本从 3.0.0 提到 4.0.0，你需要改哪几个文件？

**需要观察的现象 / 预期结果**：

- 步骤 2：`dependencies.py` 里确实没有硬编码任何仓库名，印证「薄客户端 + SSOT」。
- 步骤 3（待本地验证具体行为）：终止哨兵丢失后，解析器无法确定清单结束位置。最可能的两种行为是「读到文件末尾」或「报错」——具体取决于 `PsiFpgaLibDependencies` 的实现（本仓库不可见）。这正说明**两行哨兵缺一不可**，修改 README 依赖段时不能动它们。
- 步骤 4：**只需改 README.md 第 30 行一处**。`dependencies.py` 无需改动——这就是 SSOT 的回报。

> 待本地验证：步骤 3 的精确报错形态需在装好 `PsiFpgaLibDependencies` 后实跑观察；本实践主要结论（SSOT、运行期 vs 开发期）已由源码与 README 文本确定。

#### 4.2.5 小练习与答案

**练习 1**：本项目有四个外部依赖，哪一个是「使用 IP 时也必需」的运行期依赖？依据是什么？

> **答案**：`psi_common`（≥3.0.0）。依据是 README 第 30 行**没有** `for development only` 标注，而 `PsiSim`、`PsiIpPackage`、`psi_tb` 三者都标注了。原因是 `psi_common` 提供 `psi_common_spi_master`、`psi_common_sync_fifo`、`psi_common_axi_slave_ipif` 等运行期 RTL，综合时就要；其余三者只在仿真或重新打包时用。

**练习 2**：为什么 `dependencies.py` 选择从 README 解析依赖，而不是自己硬编码一份列表？

> **答案**：为了让人读到的依赖清单和脚本拉取的清单**保持单一数据源（SSOT）**。硬编码会出现「README 改了、脚本忘改」的双源不一致问题；从 README 解析则二者天然同源。代价是 README 的依赖段格式不能乱改（所以加了 `DO NOT CHANGE FORMAT` 警告哨兵）。

**练习 3**：`dependencies.py` 第 7 行 `from PsiFpgaLibDependencies import *` 用了 `import *`。如果用户没装这个包，运行脚本会怎样？

> **答案**：Python 会在第 7 行抛 `ModuleNotFoundError: No module named 'PsiFpgaLibDependencies'` 立即终止。README 第 43 行也明确提示「必须先安装该包」。这说明本仓库的依赖获取能力**完全外置**于这个 pip 包，仓库自身不带解析/拉取实现。

---

### 4.3 代码重构脚本与工作流

#### 4.3.1 概念说明

`scripts/refactoring/` 下三个文件解决的是另一类问题：**当依赖库的命名规范发生不兼容变更时，如何批量改写所有引用它的源码**。

具体背景：PSI 的 `psi_common` 库从 v2.x.x 升到 v3.x.x 时，把 VHDL 命名规范从 **PascalCase**（如 `Clk`、`Rst`、`Size_g`）整体改成了 **snake_case + 方向后缀**（如 `clk_i`、`rst_i`、`width_g`）。本项目大量例化 `psi_common_*` 组件，升级依赖后所有端口/generic 名都得跟着改。手改既慢又易漏，于是 PSI 写了一套**基于正则的 VHDL 文本改写工具**，配合一份「旧名 → 新名」的映射数据库，一键完成。

这套工具是提交 `cf05975 REFACTOR: Adapt to library` 引入的（作者 Benoît Stef / Radoslaw Rybaniec），那次提交同时改了 `spi_simple.vhd`、`spi_vivado_wrp.vhd` 并新增了三个重构文件——也就是说，仓库里的 RTL 已经是改写后的结果，重构脚本保留下来是为了**将来再遇到类似升级时可复用**。

⚠️ 风险提示：正则改写 VHDL 本质是「盲改文本」，不理解语法语义。它适合一次性大批量、改完人工 review 的场景；**不适合**纳入日常 CI 或随便对生产代码运行。本节会同时讲它的能力和它的坑。

#### 4.3.2 核心流程

```
alpha.json （改名数据库：旧名 → 新名）
     │
     ▼
[hdlrefactor.py]
  set_refactor_database("./alpha.json")
     │  └─ 载入数据库，补小写副本、补 _tb 后缀变体、补特殊映射
     ▼
[refactor_library_and_testbench.py]  （驱动）
  对每个 .vhd* 文件依次跑：
     entity_declaration_refactor   ← 改实体声明里的端口名
     instantiation_refactor        ← 改例化 port/generic map 里的关联名
     symbol_refactor               ← 改文件里所有符号（最激进）
  再对 sim/config.tcl 跑：
     tcl_generics_refactor         ← 改 Tcl 里 -g<generic> 的名字
     ▼
  写回原文件（输入=输出路径）
```

改名查询的核心是查表：给定组件名 + 旧符号名，返回新名；查不到就原样返回（保守）。

#### 4.3.3 源码精读

先看驱动脚本 `refactor_library_and_testbench.py`，它最短、最能体现整体意图：

载入改名数据库：

[scripts/refactoring/refactor_library_and_testbench.py:14](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/refactor_library_and_testbench.py#L14) — `set_refactor_database("./alpha.json")` 载入映射表。注意这是相对路径，**必须在 `scripts/refactoring/` 目录下运行**该脚本才能找到 `alpha.json`。

对 hdl 目录的每个 VHDL 文件跑三连改写：

[scripts/refactoring/refactor_library_and_testbench.py:16-21](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/refactor_library_and_testbench.py#L16-L21) — `Path('../../hdl').rglob('*.vhd*')` 从 `scripts/refactoring/` 向上两级到仓库根，再进 `hdl`，匹配所有 `.vhd`/`.vhd` 文件；每个文件依次跑 `entity_declaration_refactor` → `instantiation_refactor` → `symbol_refactor`，且**输入输出同路径**（原地覆盖，无备份）。

⚠️ 本仓库的一个真实坑——testbench 目录路径对不上：

[scripts/refactoring/refactor_library_and_testbench.py:23-28](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/refactor_library_and_testbench.py#L23-L28) — 这里写的是 `Path('../../testbench')`，但**本仓库的 testbench 目录名叫 `tb`，不叫 `testbench`**（仓库根只有 `tb/`，无 `testbench/`，已核实）。`rglob` 在不存在的路径上**不报错、静默返回空**，所以这段循环在本仓库里实际是空操作——testbench 文件不会被改写。这说明该脚本更像是为 PSI 通用库结构（用 `testbench` 命名）写的模板，搬到本仓库后路径没对齐。复用时务必先核对目录名。

对 Tcl 仿真配置改 generic 名：

[scripts/refactoring/refactor_library_and_testbench.py:32-34](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/refactor_library_and_testbench.py#L32-L34) — 对 `../../sim/config.tcl` 跑 `tcl_generics_refactor`，把仿真 run 里 `-g<OldName>=...` 的 generic 名一并改掉，保证 RTL 改名后仿真配置同步。

再看函数库 `hdlrefactor.py` 的几个关键点。

全局字典 `DICT` 是改名表的内存形态：

[scripts/refactoring/hdlrefactor.py:12](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L12) — 模块级 `DICT = {}`；未调用 `set_refactor_database` 前为空。

载入并扩充数据库（理解能力边界的关键）：

[scripts/refactoring/hdlrefactor.py:14-57](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L14-L57) — `set_refactor_database` 不仅载入 `alpha.json`，还做三件扩充：
- `fix_case`（[L19-25](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L19-L25)）：为每个旧名补一份**小写键**副本，让查询大小写不敏感；
- `add_tb`（[L26-33](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L26-L33)）：为每个组件名补一份 `<原名>_tb` 后缀变体，使 testbench 里的例化也能查到；
- `add_dict`（[L34-52](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L34-L52)）：硬编码一批 `psi_common_axi_master_*_tb_*` 的特殊归并映射（把多个 case 文件归到对应 `_tb_pkg`），是针对 PSI 内部 testbench 结构的特例。

改名查表的核心函数：

[scripts/refactoring/hdlrefactor.py:59-77](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L59-L77) — `conv_fun(comp_name, signal, ...)`：先用 `(comp_name, signal)` 查 `DICT`；查不到时，若 `use_all=True` 再查全局 `#ALL#` 表；仍查不到就**原样返回** `signal`（保守，不乱改）。第 68 行是查表核心 `ret = DICT[comp_name][signal_lower_case]`。

数据库里的两种条目——以 `alpha.json` 开头为例：

[scripts/refactoring/alpha.json:2-22](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/alpha.json#L2-L22) — `#ALL#` 是**全局表**：不分组件，任何文件里出现的 `ZerosVector`→`zeros_vector`、`ReduceOr`→`reduce_or`、`ClockRatioN_g`→`clock_ratio_n_g` 等通用函数/类型名都按此改。

[scripts/refactoring/alpha.json:23-30](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/alpha.json#L23-L30) — `psi_common_arb_priority` 是**组件专表**：仅当改写该组件的例化/声明时生效，如 `Size_g`→`width_g`、`Clk`→`clk_i`、`Rst`→`rst_i`、方向后缀 `_i`/`_o` 体现了 v3 规范的输入/输出标识约定。

四类改写函数（按激进程度递增）：

- [scripts/refactoring/hdlrefactor.py:210](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L210) — `entity_declaration_refactor`：只改 `entity ... port(...)` 声明里的端口名，作用域最小、最安全。
- [scripts/refactoring/hdlrefactor.py:80](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L80) — `instantiation_refactor`：用正则匹配 `generic map`/`port map` 块内的 `左名 => 右名` 关联，改左名（形式端口名）。注释剥离靠 `l.split('--')` 取首段（[L126-131](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L126-L131)）。
- [scripts/refactoring/hdlrefactor.py:263](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L263) — `symbol_refactor`：对**每行所有标识符**做 `re.sub` 替换（[L304](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L304)），最激进——信号名、变量名、过程调用都会被扫到，风险最高，必须人工 review。
- [scripts/refactoring/hdlrefactor.py:317](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L317) — `tcl_generics_refactor`：匹配 Tcl 里的 `-g<Name>` 模式（[L327](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L327)），用 `#` 分隔注释，把 generic 名同步改名。

#### 4.3.4 代码实践

**实践目标**：在不运行脚本的前提下，依据 `alpha.json` 的映射规则，手动预测一段 VHDL 改名后的样子，并定位本仓库里驱动脚本的一个真实缺陷。

**操作步骤**：

1. 打开 `alpha.json`，在 `#ALL#` 段（第 2–22 行）找到 `IntToStdLogic`、`ReduceOr` 的映射；在某个组件专表（如 `psi_common_arb_priority`，第 23–30 行）找到 `Clk`、`Rst`、`Size_g` 的映射。
2. 假设有一行 VHDL（示例代码，非项目原有）：

   ```vhdl
   -- 示例代码：演示 alpha.json 改名规则
   my_arb : entity work.psi_common_arb_priority
   port map (
       Clk   => Clk,
       Rst   => Reset,
       Size_g => 4
   );
   y <= ReduceOr(x);
   ```

   依据映射规则，逐个符号写出改写结果。注意 `Clk => Clk` 里左右两个 `Clk` 哪个会被 `instantiation_refactor` 改、哪个会保留。
3. 打开 `refactor_library_and_testbench.py` 第 23 行，确认它引用的 `../../testbench` 在本仓库并不存在（仓库根是 `tb/`）。思考：这段循环在本仓库运行时会发生什么？

**需要观察的现象 / 预期结果**：

- 步骤 2 预期（示例代码，按 alpha.json 规则推演）：
  - 例化的形式端口名（`=>` 左侧）`Clk`→`clk_i`、`Rst`→`rst_i`、generic `Size_g`→`width_g`，因为 `instantiation_refactor` 改的是关联左名；
  - `=>` 右侧的实际信号名（如 `Reset`）不在 `psi_common_arb_priority` 表里，保留不变；右侧的 `Clk`（用户信号）也保留（除非它在 `#ALL#` 表里——`Clk` 不在 `#ALL#`，故保留）；
  - `ReduceOr` 属于 `#ALL#` 表 → `reduce_or`。
  - 最终大致为：`clk_i => Clk, rst_i => Reset, width_g => 4`、`y <= reduce_or(x);`。
- 步骤 3：`Path('../../testbench').rglob('*.vhd*')` 在本仓库返回空迭代器（路径不存在，rglob 静默不报错），循环体不执行——**testbench 不会被改写**。这是真实的隐患：若有人指望脚本能改 tb，会得到「看似成功、实际没改」的假象。

> 待本地验证：步骤 2 的精确改写结果需实际跑 `symbol_refactor`/`instantiation_refactor` 才能完全确认（正则边界条件多）；本实践重点在理解查表规则与 `=>` 左右两侧的差异，结论由 alpha.json 与 hdlrefactor.py 逻辑确定。步骤 3 的空迭代行为是 Python `pathlib` 标准行为，已确认。

#### 4.3.5 小练习与答案

**练习 1**：`conv_fun` 查不到某个符号时会怎么做？为什么这个设计是「安全」的？

> **答案**：查不到就**原样返回**该符号（hdlrefactor.py 第 74、76 行的两处 `ret = signal`）。这保证数据库里没登记的名字不会被乱改——只有显式登记的旧名才会被替换，未登记的（包括本项目自有的信号名、无关单词）保持不变。这让脚本可以放心地对整个文件跑一遍而不破坏未知内容。

**练习 2**：`symbol_refactor` 比 `entity_declaration_refactor` 激进得多，为什么驱动脚本还要把三个都跑一遍？能不能只跑 `symbol_refactor`？

> **答案**：`symbol_refactor` 对每行所有标识符无差别替换，理论上能覆盖实体声明和例化。但它的「无差别」也是风险：会误伤与旧名同形的本地信号、注释外的文本、甚至部分字符串。保留三种各司其职的改写器，是「先精准改（声明/例化），再兜底改（符号）」的分层策略，便于事后定位哪一类改写出问题。即便如此，改完仍需人工 review 与仿真回归——这类脚本绝不应盲信。

**练习 3**：本仓库的 testbench 目录叫 `tb`，但驱动脚本写的是 `../../testbench`。如果你要修复这个问题，最小改动是什么？修复前为什么必须先备份？

> **答案**：最小改动是把 `refactor_library_and_testbench.py` 第 23 行的 `'../../testbench'` 改成 `'../../tb'`（与 `sim/config.tcl` 第 45 行 `../tb`、本仓库实际目录一致）。修复前必须备份，因为该脚本是**原地覆盖**（输入输出同路径，见第 19–21 行 `xxx_refactor(path, path)`），一旦正则误改，原文件就被覆盖、无 git 未提交版本可救（除非已 commit）。正确流程是：先 `git status` 确认干净 → 改路径 → 跑脚本 → `git diff` 逐行 review → 不满意就 `git checkout` 还原。

---

## 5. 综合实践

**任务**：设计一个「修改 `spi_simple` 后」的最小改动验证流程，把本讲三个模块串成一条从依赖获取到 CI 判定的完整工作流。

**背景**：假设你改了 `hdl/spi_simple.vhd` 里某个内部信号的处理逻辑（不动接口），需要验证改动没破坏功能。

**要求产出**：

1. **依赖获取**：列出你应运行的脚本命令、它读哪个文件、把依赖放到什么结构。说明如果只是改 RTL、不增减依赖，这一步是否真的必要。
2. **仿真回归**：列出从 sim 目录手动跑回归的命令，以及它内部依次执行的 PsiSim 步骤顺序（参考 u1-l4 与 run.tcl）。
3. **CI 判定**：列出等价的 CI 命令（用 ciFlow.py），并明确指出 transcript 中**哪两个字符串**决定退出码，各自映射到哪个退出码。

**参考答案（流程设计）**：

| 步骤 | 命令（在仓库根执行） | 读/写什么 | 说明 |
| --- | --- | --- | --- |
| ① 依赖获取 | `python scripts/dependencies.py` | 读 `README.md` 依赖段（哨兵 18/35 行），拉取到 `TCL/`、`VHDL/` 等固定结构 | 仅当依赖未就绪时需要；只改 RTL 不增减依赖时，若环境已搭好（如用 `psi_fpga_all`），此步可跳过 |
| ② 手动回归 | 在 `sim/` 下 `source ./run.tcl` | source `config.tcl`、编译 hdl/tb/psi_common/psi_tb、跑 `top_tb` | 步骤顺序：`init` → `source config.tcl` → `compile -all -clean` → `run_tb -all` → `run_check_errors "###ERROR###"` |
| ③ CI 判定 | `python scripts/ciFlow.py` | chdir 到 sim、跑 `vsim -c -do ci.do`、读 `Transcript.transcript` | 等价于 ② 但无人值守并给退出码 |

**决定退出码的两个字符串**：

| 字符串 | 出现位置 | 缺失/命中 | 退出码 |
| --- | --- | --- | --- |
| `###ERROR###` | testbench 断言失败时由 `run_check_errors` 扫到 | **命中** → `-1`（回归失败） |
| `SIMULATIONS COMPLETED SUCCESSFULLY` | PsiSim 跑完所有 run 后打印 | **缺失** → `-2`（没正常跑完） |

两者都不触发则 `exit(0)`（成功）。判定顺序：先查 `###ERROR###`（`-1` 优先），再查成功串缺失（`-2`），最后 `0`。

**延伸思考**：步骤 ② 和 ③ 共用同一份 `run.tcl`，这是本项目的关键设计——手动调试和 CI 验证不可能跑出不一致的结果。如果你在 ② 里看到 `###ERROR###`，可以断定 ③ 的 CI 也会判 `-1`。

> 待本地验证：完整跑通 ①→③ 需要已安装 Modelsim/Questa、PsiSim、`PsiFpgaLibDependencies` 以及正确目录结构的四个依赖仓库。无环境时，本实践为「流程设计型」，结论由 ciFlow.py / run.tcl / dependencies.py 的源码逻辑确定。

---

## 6. 本讲小结

- **CI 是三层结构**：`ciFlow.py`（Python 编排器）→ `ci.do`（Tcl 入口）→ `run.tcl`（PsiSim 回归主体）。run.tcl 人/CI 共用，ci.do 只多了一句让 vsim 自动退出的 `quit`。
- **退出码靠 transcript 里两个字符串决定**：命中 `###ERROR###` → `-1`（断言失败）；缺失 `SIMULATIONS COMPLETED SUCCESSFULLY` → `-2`（没跑完）；否则 `0`。判定顺序先查错误再查完成。
- **`os.chdir` 到 sim 是相对路径的锚点**：ci.do / run.tcl / config.tcl / Transcript.transcript 全靠 CWD=sim 才能互相找到。
- **依赖解析以 README 为 SSOT**：`dependencies.py` 是 16 行薄客户端，不硬编码任何依赖，靠两行 HTML 注释哨兵圈出 README 的依赖段，真正解析/拉取逻辑在外部 `PsiFpgaLibDependencies` 包。
- **重构脚本是「正则盲改 VHDL」工具**：`alpha.json` 是旧名→新名映射表，`hdlrefactor.py` 提供四类改写函数（声明/例化/符号/Tcl generic），驱动脚本批量原地覆盖；适合一次性升级（如 psi_common v2→v3），改完必须人工 review + 回归。
- **本仓库的真实坑**：驱动脚本第 23 行引用的 `../../testbench` 目录不存在（仓库用 `tb`），rglob 静默返回空，testbench 不会被改写；且所有改写输入输出同路径、无备份，复用前必须先确保 git 干净。

---

## 7. 下一步学习建议

本讲是学习手册的最后一篇，你已经走完了从「项目是什么」到「怎么扩展与工程化」的完整路线。建议从以下方向继续深入：

1. **端到端跑一遍真实 CI**：在装好 Modelsim/Questa + PsiSim 的环境中，从 `psi_fpga_all` 聚合仓库克隆完整结构，实跑 `python scripts/ciFlow.py` 并用 `echo $?` 观察三个退出码场景，把本讲的「源码阅读型结论」变成「亲手验证的肌肉记忆」。
2. **阅读 PsiSim 与 PsiFpgaLibDependencies 源码**：本仓库只是薄客户端，真正的 `psi::sim::init/compile/run_tb/run_check_errors` 实现和 `Parse.FromReadme/Actions.ExecMain` 实现都在外部仓库——去 [paulscherrerinstitute/PsiSim](https://github.com/paulscherrerinstitute/PsiSim) 和 [PsiFpgaLibDependencies](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies) 看它们如何被本仓库的薄封装调用。
3. **回顾整条主线**：回到 u2-l2（spi_simple 核心架构）→ u2-l3（AXI 接口）→ u2-l8（testbench 自检），把「RTL 内部机制」与「本讲的 CI 回归」对上号——你会理解 testbench 里每一处 `StdlvCompareStdlv` 断言失败，正是 CI transcript 里那个 `###ERROR###` 的来源，也是退出码 `-1` 的最终根因。
4. **尝试一次安全的小重构**：在 git 干净、有回归保护的前提下，仿照 `alpha.json` 写一张只含一两个映射的小表，对某个 `.vhd` 文件试跑 `entity_declaration_refactor`，用 `git diff` 审查改动——这是理解「正则改写工具能力与风险」最直接的方式。

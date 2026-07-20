# CI、回归与开发工作流

## 1. 本讲目标

本讲是专家层的工程化收尾篇。学完后你应当能够：

- 说清一次 RTL 改动从「敲代码」到「CI 给出绿/红灯」之间，到底经过了哪些脚本、哪些命令、它们各自负责什么；
- 解释 `ciFlow.py` 为什么只靠**两个字符串**和一个**纯文本 transcript** 就能判定回归成败，以及这套设计的脆弱点在哪里；
- 说清 `dependencies.py` 与 README 之间的「机器可解析边界」契约，以及为什么 README、`config.tcl`、`dependencies.py` 三处必须对同一套目录结构达成一致；
- 识别 `scripts/refactoring/` 下那套正则重命名脚本的用途、来源与风险，知道它何时该用、何时绝不该碰。

> 本讲承接 u1-l4（仿真与回归运行方式）、u1-l3（工具链与依赖）、u3-l4（IP 打包）。那三讲已经建立过「run.tcl 流水线长什么样」「dependencies.py 是 16 行的胶水」「package.tcl 是参数化唯一数据源」的宏观认知。本讲**不再重复这些结论**，而是钻进这三条工程化链路的**内部机制、耦合点与失败模式**，并补上 u1-l3/u1-l4 完全未涉及的「代码重构脚本」这一块。

## 2. 前置知识

在进入源码前，先用通俗语言对齐三个工程化概念：

- **回归测试（Regression Test）**：每改一次代码，就把之前所有测试用例整套重跑一遍，确保新改动没有「打翻」旧功能。对 FPGA 项目而言，就是重新编译全部 RTL + testbench，跑完所有 TB run，看有没有断言失败。
- **Transcript**：Modelsim/Questa 在运行时往控制台和日志文件里输出的全部文本。本项目的成败判定，本质上是「在 transcript 里找特定字符串」。
- **一次性迁移脚本（One-shot Migration Script）**：专门为某次大版本升级（如依赖库 2.x→3.x 全量改名）写的脚本，跑一次、提交结果、之后基本不再用。它和「每次提交都跑的 CI」是两类完全不同的工具，混用会出事。

还需要知道两个 u1-l4 已建立的契约：testbench 用 `report` 语句或 psi_tb 的比较函数在断言失败时向 transcript 写入 `###ERROR###`；PsiSim 的 `run_tb` 在所有仿真正常结束后会打印 `SIMULATIONS COMPLETED SUCCESSFULLY`。这两个字符串是本讲一切判定逻辑的基石。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用它讲什么 |
| --- | --- | --- |
| `scripts/ciFlow.py` | CI 入口，27 行 | Tcl↔Python 的 transcript 桥接、三段式退出码 |
| `sim/ci.do` | 命令行模式 do 文件，2 行实质 | 为什么 `quit` 是命令行自动返回的关键 |
| `sim/run.tcl` | 回归流水线 | PsiSim 命令链与 `###ERROR###` 扫描层 |
| `sim/config.tcl` | 仿真清单声明 | 消息抑制（suppress）为何是工程必需 |
| `scripts/dependencies.py` | 依赖拉取入口，16 行 | README 解析契约、repo 根耦合 |
| `README.md` | 项目说明 | 依赖段的「机器可解析边界」 |
| `scripts/refactoring/hdlrefactor.py` | 正则符号重命名库 | JSON 驱动改名、四级 pass、风险点 |
| `scripts/refactoring/refactor_library_and_testbench.py` | 重命名编排脚本 | 一次性迁移工作流、目录路径陷阱 |
| `scripts/refactoring/alpha.json` | 改名数据库 | `#ALL#` 全局命名空间与按组件映射 |

---

## 4. 核心概念与源码讲解

### 4.1 CI 回归与结果判定

#### 4.1.1 概念说明

u1-l4 已经讲过「ciFlow.py 切到 sim 跑 vsim，再读 transcript 判退出码」这件事的结论。本节要回答的是更深的问题：**为什么一个 Python 脚本要靠「在一个文本文件里找两个字符串」来决定 CI 成败？这种设计稳不稳？**

答案是：Tcl（跑在 vsim 里）和 Python（跑在操作系统里）是两个独立的进程，它们之间**没有结构化 API**，唯一的通信介质就是 vsim 写出来的 transcript 文本文件。于是 CI 判定被强行简化成「字符串匹配」——这是「跨语言、跨进程、靠文件传话」的典型工程妥协。

理解了这一点，后续所有「为什么是 `###ERROR###`、为什么顺序不能换、为什么 suppress 很重要」都会迎刃而解。

#### 4.1.2 核心流程

一次 CI 回归的实际控制流是**三层嵌套调用**：

```text
python scripts/ciFlow.py            # 第 1 层：Python，操作系统进程
   └─ chdir 到 sim/，调用 vsim -c -do ci.do
        └─ vsim 执行 sim/ci.do      # 第 2 层：Tcl，vsim 内部
             └─ source run.tcl      # 第 3 层：PsiSim 框架命令链
                  ├─ init
                  ├─ source config.tcl   （声明库/源/TB/run）
                  ├─ compile -all -clean
                  ├─ run_tb -all          （打印成功横幅）
                  └─ run_check_errors "###ERROR###"
        └─ quit                       （ci.do 第 2 行，让 vsim 退出）
   └─ 读 sim/Transcript.transcript，做字符串匹配 → exit(0 / -1 / -2)
```

退出码的三段判定逻辑（**顺序敏感**）：

| transcript 内容 | 退出码 | 含义 |
| --- | --- | --- |
| 含 `###ERROR###` | `-1` | 有断言失败（expected error），回归失败 |
| 不含 `###ERROR###`，但**也没有** `SIMULATIONS COMPLETED SUCCESSFULLY` | `-2` | 没跑完（编译错、崩溃、被 kill），回归异常 |
| 不含 `###ERROR###`，**且** 含 `SIMULATIONS COMPLETED SUCCESSFULLY` | `0` | 正常跑完且无断言失败，回归通过 |

注意判定顺序：**先查 `###ERROR###`，再查成功横幅**。这保证了一次「断言失败但仿真仍跑到末尾」的运行（两者同时出现在 transcript 里）会被正确判为 `-1` 而不是被成功横幅掩盖成 `0`。

#### 4.1.3 源码精读

先看第 1 层 Python 入口。`ciFlow.py` 的全部实质逻辑只有三段——切目录、跑仿真、判文本：

[scripts/ciFlow.py:11-13](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L11-L13) 切到 `sim/` 目录并以命令行模式（`-c`，无 GUI）启动 vsim，`-do ci.do` 让它进入后立即执行该 do 文件：

```python
os.chdir(THIS_DIR + "/../sim")
os.system("vsim -c -do ci.do")
```

> 这里用的是 `os.system` 而非 `subprocess`，意味着调用是**阻塞**的，且 `vsim` 的退出码被丢弃——ciFlow.py **完全不依赖 vsim 自己的返回值**，只信 transcript。这是一个有意的设计取舍：不同版本 vsim 的退出码语义不一致，而 transcript 文本是稳定的。

[scripts/ciFlow.py:15-16](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L15-L16) 把整个 transcript 读成一个字符串，这就是 Tcl 与 Python 之间唯一的「IPC」通道：

```python
with open("Transcript.transcript") as f:
    content = f.read()
```

[scripts/ciFlow.py:19-23](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L19-L23) 是成败契约本身——先 `###ERROR###` 退 `-1`，否则缺成功横幅退 `-2`：

```python
if "###ERROR###" in content:
    exit(-1)
if "SIMULATIONS COMPLETED SUCCESSFULLY" not in content:
    exit(-2)
```

[scripts/ciFlow.py:27](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L27) 两条都不触发才 `exit(0)`。

再看第 2 层 do 文件，它只有两行实质内容，但缺一不可：

[sim/ci.do:7-8](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/ci.do#L7-L8)：

```tcl
source run.tcl
quit
```

- `source run.tcl` 复用人工调试时用的同一条流水线，保证「CI 跑的」和「人跑的」是同一套；
- `quit` 是命令行模式自动返回的关键。u1-l4 已点出这一点，这里补充**为什么**：`vsim -c` 跑完 do 文件后**不会自动退出**，会停在 Tcl 提示符等待输入，`os.system` 就会永远阻塞。`quit` 显式关掉 vsim，控制权才回到 ciFlow.py 继续读 transcript。

第 3 层 `run.tcl` 的命令链已在 u1-l4 讲过，这里只强调**两层 `###ERROR###` 扫描**的分工：

[sim/run.tcl:29](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/run.tcl#L29) 是 PsiSim 在 Tcl 侧做的**软扫描**——它会把命中的 `###ERROR###` 行高亮、计数、打印警告，但不会终止脚本：

```tcl
psi::sim::run_check_errors "###ERROR###"
```

而 [scripts/ciFlow.py:19-20](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L19-L20) 是 Python 侧的**硬扫描**——同样的字符串，但命中就直接 `exit(-1)`。两层扫描、同一个字符串，前者是「给人看的提示」，后者是「给 CI 用的判据」。

最后看一条 u1-l4 没展开、但对「判定可靠性」至关重要的工程细节——消息抑制。[sim/config.tcl:15-16](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/config.tcl#L15-L16)：

```tcl
psi::sim::compile_suppress 135,1236
psi::sim::run_suppress 8684,3479,3813,8009,3812
```

这些数字是 Modelsim 的 message ID。抑制它们的目的是**保持 transcript 干净**：把已知的、良性的警告（如某段代码未用、某优化提示）压掉，让真正的 `###ERROR###` 和断言报告在日志里一眼可见。如果不禁噪，几百行噪声里混一个真错误，人工排查和自动扫描都会受干扰。

#### 4.1.4 代码实践

**实践目标**：亲手体会「transcript 是唯一的成败通信介质」，并验证三段式退出码。

**操作步骤（源码阅读 + 推演型，待本地验证）**：

1. 打开 [scripts/ciFlow.py](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py)，确认它对 vsim 的退出码**完全无视**（`os.system` 返回值未被接收）。
2. 假设你在 testbench 里故意把一处期望值改错，让 `StdlvCompareStdlv` 失败。推断：该比较函数会向 transcript 写入 `###ERROR###`。
3. 推演此时 transcript 的内容：会同时出现 `###ERROR###`（断言失败）**和** `SIMULATIONS COMPLETED SUCCESSFULLY`（仿真仍跑到末尾）。
4. 对照 [scripts/ciFlow.py:19-23](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L19-L23)，确认因为 `###ERROR###` 判定在前，退出码是 `-1` 而非 `-2` 或 `0`。
5. 再推演另一种故障：把 `config.tcl` 里某个源文件名写错导致编译失败。此时 `run_tb` 根本没执行，transcript 里**两个字符串都没有**，退出码应为 `-2`。

**需要观察的现象**：三种故障（断言失败 / 编译失败 / 正常通过）对应三种不同退出码（`-1` / `-2` / `0`），且区分依据纯粹是两个字符串的有无。

**预期结果**：能复述「先查 `###ERROR###`、再查成功横幅」的顺序为何能正确区分「断言失败但跑完」与「没跑完」。

#### 4.1.5 小练习与答案

**练习 1**：假如未来某版 PsiSim 把成功横幅从 `SIMULATIONS COMPLETED SUCCESSFULLY` 改成了别的措辞，CI 会怎样？为什么？

**参考答案**：所有回归都会变成 `-2`（异常退出）。因为 [scripts/ciFlow.py:22](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L22) 硬编码了这个字符串，PsiSim 不再打印它就会被判「没跑完」。这是「跨仓库靠字面量耦合」的典型脆弱点——成功检测依赖一个上游库的措辞不变。

**练习 2**：为什么 `ci.do` 第二行的 `quit` 不能省？省掉会发生什么？

**参考答案**：`vsim -c` 执行完 do 文件后停在 Tcl 提示符等输入，不会自动退出。省掉 `quit`，`os.system` 会一直阻塞，ciFlow.py 永远读不到 transcript、给不出退出码，CI 任务会挂死直到被超时杀掉（而非返回 `-2`）。

**练习 3**：`run_check_errors`（Tcl 侧）和 ciFlow.py 的 `###ERROR###` 检查（Python 侧）扫的是同一个字符串，为什么要做两遍？

**参考答案**：职责不同。Tcl 侧是「软提示」——在交互调试时把错误行高亮计数、提醒工程师，但不中断；Python 侧是「硬判据」——CI 模式下命中即 `exit(-1)`。一个面向人，一个面向机器，复用同一个字符串契约避免了两套规则漂移。

---

### 4.2 依赖解析机制

#### 4.2.1 概念说明

u1-l3 已经讲过 `dependencies.py` 是 16 行胶水、真正逻辑在 `PsiFpgaLibDependencies` 包里。本节要讲清楚的是这 16 行胶水**和 README 之间的契约**：README 的依赖段既是写给人看的 Markdown，又是被脚本解析的「数据文件」。这是一种「单一数据源（SSOT）」设计——同一份依赖清单同时服务人类和机器，代价是清单的格式成了**机器契约**，不能随便改。

本节还要揭示一个容易被忽略的工程约束：本项目的目录结构不是约定俗成，而是被**三处独立代码**共同锁定的硬约束。

#### 4.2.2 核心流程

依赖解析的流程可以拆成「解析」和「执行」两段：

```text
【解析段】Parse.FromReadme(README.md)
   ├─ 在 README 里定位两个 HTML 注释哨兵之间的区域
   ├─ 解析树形列表：TCL / VHDL / VivadoIp 三组
   │     每项 = (仓库名, URL, 版本约束, 是否 dev-only)
   └─ 返回结构化依赖列表

【执行段】Actions.ExecMain(repo_root, dependencies)
   └─ 以本仓库的父目录为根，按 TCL/ / VHDL/ 文件夹名克隆/检出依赖
```

三处代码对**同一个三级目录结构**的锁定关系：

| 代码位置 | 表达方式 | 共同约束 |
| --- | --- | --- |
| README 依赖段 | 「required folder structure」文字说明 + 树形列表 | 依赖必须按 `TCL/`、`VHDL/` 放在本仓库的**同级** |
| `sim/config.tcl` | `set LibPath "../../.."` | 从 `sim/` 往上三级找到依赖根 |
| `scripts/dependencies.py` | `repo = abspath(THIS_DIR + "/..")` | 以本仓库根作为 ExecMain 的工作目录，依赖检出为它的兄弟目录 |

三者只要有一处对不上（比如把仓库克隆到别的层级，或改了文件夹名），`config.tcl` 就找不到 psi_common，回归立刻编译失败。

#### 4.2.3 源码精读

先看契约的「数据源」——README 里被解析的那一段。[README.md:18](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L18) 是**起始哨兵**，措辞本身就是警告：

```markdown
<!-- DO NOT CHANGE FORMAT: this section is parsed to resolve dependencies -->
```

[README.md:26-31](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L26-L31) 是被解析的树形主体：

```markdown
* TCL
  * [PsiSim](...) (2.1.0 or higher, for development only)
  * [PsiIpPackage](...) (2.0.0, for development only )
* VHDL
  * [psi\_common](...) (3.0.0 or higher)
  * [psi\_tb](...) (3.0.0 or higher, for development only)
```

[README.md:35](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L35) 是**终止哨兵**：

```markdown
<!-- END OF PARSED SECTION -->
```

> 注意三个解析语义点：(1) `TCL`/`VHDL`/`VivadoIp` 这三个分组名**同时是要求的检出目录名**——SSOT 连目录结构一起锁了；(2) 括号里的版本约束 `(2.1.0 or higher)`、`(2.0.0)` 是机器可读的版本语法；(3) `for development only` 不是普通散文，而是被解析的**语义标签**，它告诉 resolver 哪些依赖是可选的（仅打包/仿真需要，运行期不需要）。u1-l3 已区分过「运行期必需 vs 仅开发需要」，这里给出了这个区分在 README 里的落点。

再看胶水脚本本体。[scripts/dependencies.py:7-16](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/dependencies.py#L7-L16) 是全部 16 行的实质：

```python
from PsiFpgaLibDependencies import *
...
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
dependencies = Parse.FromReadme(THIS_DIR + "/../README.md")
repo = os.path.abspath(THIS_DIR + "/..")
Actions.ExecMain(repo, dependencies)
```

逐行对应 4.2.2 的两段流程：

- `Parse.FromReadme(...)` 是**解析段**——输入 README 路径，输出结构化依赖列表。脚本本身**不硬编码任何依赖**，这就是 u1-l3 强调的「单一数据源」：加了新依赖只改 README，脚本零改动。
- `repo = abspath(THIS_DIR + "/..")` 算出本仓库根目录，传给 `Actions.ExecMain(repo, ...)` 作为**执行段**的工作根。依赖会被检出为该根的**兄弟目录**（`TCL/`、`VHDL/`），正好对上 `config.tcl` 里 `LibPath "../../.."`（从 `sim/` 往上三级）。

#### 4.2.4 代码实践

**实践目标**：验证「README 依赖段 = 机器契约」，并体会改格式即破坏解析。

**操作步骤（源码阅读 + 推演型）**：

1. 对照 [README.md:18](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L18) 与 [README.md:35](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L35) 两个哨兵，确认被解析区域恰好是夹在它们之间的依赖树。
2. 假设有人把 `* VHDL` 改成 `### VHDL 依赖`（Markdown 美化），推断 `Parse.FromReadme` 会找不到 `VHDL` 分组，导致 `psi_common` / `psi_tb` 不被检出。
3. 假设有人删掉了起始哨兵那一行 HTML 注释，推断解析器失去定位边界，可能解析整篇 README（误把正文里的链接当依赖）或直接报错。
4. 验证三处目录耦合：确认 `config.tcl` 的 `../../..`（[sim/config.tcl:8](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/config.tcl#L8)）与 `dependencies.py` 的 `THIS_DIR + "/.."`（[scripts/dependencies.py:14](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/dependencies.py#L14)）指向的是同一个「仓库根」概念。

**需要观察的现象**：README 依赖段里**任何**格式变化（分组名、缩进、哨兵）都可能让依赖拉取失败或拉错。

**预期结果**：能说清「为什么 README 顶部要写 DO NOT CHANGE FORMAT」，以及这套 SSOT 的代价是「人类文档的格式自由度被牺牲」。

#### 4.2.5 小练习与答案

**练习 1**：`for development only` 这个标注对 resolver 意味着什么？如果用户只想**使用**这个 IP（不重新打包、不仿真），需要拉哪些依赖？

**参考答案**：它是被解析的语义标签，标记该依赖为可选。对照 [README.md:30](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L30)，只有 `psi_common (3.0.0 or higher)` 没带 `for development only`，所以**仅使用** IP 时只需 `psi_common`；`PsiSim`、`PsiIpPackage`、`psi_tb` 都是开发期才需要。

**练习 2**：为什么不把依赖清单直接写进 `dependencies.py`，而要从 README 解析？

**参考答案**：为了单一数据源。README 本来就要给人看依赖列表，若脚本里再维护一份，两份会逐渐不一致。从 README 解析让「人类文档」和「机器输入」是同一份文本，改一处即可。代价是 README 该段格式被冻结为契约。

**练习 3**：`repo = abspath(THIS_DIR + "/..")` 算的是哪个目录？为什么 ExecMain 要用它而不是 `THIS_DIR` 本身？

**参考答案**：算的是**本仓库的根目录**（`scripts/` 的上一层）。ExecMain 要在这个根的**同级**检出 `TCL/`、`VHDL/` 等依赖，所以工作根必须是仓库根而非 `scripts/`。这也和 `config.tcl` 从 `sim/` 往上三级找依赖根的布局互洽。

---

### 4.3 代码重构脚本与工作流

#### 4.3.1 概念说明

这是 u1-l3/u1-l4 完全没涉及的一块。`scripts/refactoring/` 下有三个文件：`alpha.json`（改名数据库）、`hdlrefactor.py`（正则重命名库）、`refactor_library_and_testbench.py`（编排脚本）。它们是**一次性迁移工具**，诞生于提交 `cf05975 "REFACTOR: Adapt to library"`——那次把 `spi_simple` 从旧的 `psi_common` 2.x 命名（PascalCase：`Clk`、`Rst`、`Width_g`、`InData`）迁移到新的 3.x 命名（snake_case：`clk_i`、`rst_i`、`width_g`、`in_dat_i`）。

要建立两个关键认知：

1. **这是一类完全不同于 CI 的工具**。CI 每次提交都跑、必须稳定可重复；重构脚本跑一次、改完代码就提交、之后基本不再用。把重构脚本塞进日常流程是误用。
2. **它是正则驱动的文本改写，不是基于语法树的精确重构**。这意味着它快、但会误伤（注释里的同名词、字符串字面量、巧合的同名局部变量都可能被改）。所以它只适合在大规模、机械、可事后用回归验证的迁移里用，且**必须**在跑完后用 CI 回归兜底。

#### 4.3.2 核心流程

`hdlrefactor.py` 的改名模型是一个「数据库驱动的查表替换」，核心数据结构是：

```text
DICT[组件名][旧符号] = 新符号
   ├─ "#ALL#" 分组：全局通用的工具函数/泛型，对所有组件生效
   │     例：ReduceOr → reduce_or, IntToStdLogic → int_to_std_logic
   └─ 具体组件分组：仅在该组件作用域内生效
         例：psi_common_async_fifo.InData → in_dat_i
```

查找函数 `conv_fun` 采用**三级回退**：

```text
1. 精确组件匹配：DICT[当前组件][符号]  → 命中则替换
2. 全局匹配（仅 use_all=True）：DICT["#ALL#"][符号] → 命中则替换
3. 都不命中：原样返回（不改）
```

编排脚本对每个 `.vhd` 文件施加**四级 pass，作用域逐级扩大、风险逐级升高**：

| pass | 函数 | 作用域 | 风险 |
| --- | --- | --- | --- |
| 1 | `entity_declaration_refactor` | 仅 `entity ... port(...)` 内的端口名 | 低（结构化区域） |
| 2 | `instantiation_refactor` | 仅 `port map(...)` / `generic map(...)` 关联 | 低 |
| 3 | `symbol_refactor` | **整文件所有 `\w+` 单词** | 高（会扫到注释/字面量/同名局部变量） |
| 4 | `tcl_generics_refactor` | 仅 `sim/config.tcl` 里 `-gName` 泛型 | 低（仅一个文件） |

#### 4.3.3 源码精读

先看改名数据库的全局命名空间。[scripts/refactoring/alpha.json:2-21](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/alpha.json#L2-L21) 的 `#ALL#` 分组定义了一批对所有组件通用的旧→新映射：

```json
"#ALL#": {
   "ReduceOr": "reduce_or",
   "ReduceAnd": "reduce_and",
   "IntToStdLogic": "int_to_std_logic",
   ...
}
```

> 这些是 `psi_common_logic_pkg` / `psi_common_math_pkg` 里的工具函数，在许多组件里都会被调用，所以放全局。这印证了 alpha.json 是**为整套 psi_common 生态**编写的，不是 spi_simple 专属。

再看库的入口初始化。[scripts/refactoring/hdlrefactor.py:14](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L14) 的 `set_refactor_database` 在加载 JSON 后做了两件关键的预处理：

```python
def set_refactor_database(db_file_i, fix_case = True, add_tb = True):
```

- `fix_case=True`：为每个映射额外生成一份**全小写副本**（[hdlrefactor.py:19-25](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L19-L25)），让匹配大小写不敏感——VHDL 本身大小写不敏感，这一步必须做。
- `add_tb=True`：为每个组件克隆一份带 `_tb` 后缀的映射（[hdlrefactor.py:26-33](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L26-L33)），让 testbench 实体（如 `psi_common_spi_master_tb`）也能被改名。

[scripts/refactoring/hdlrefactor.py:34-52](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L34-L52) 还硬编码了一批 `psi_common_axi_master*` 的特殊情况映射（`add_dict`）。这是一条重要线索：**这套脚本最初是给 psi_common 自己的 AXI master testbench 写的**（那批 `_case_simple_tf`、`_case_axi_hs` 是 psi_common 的测试用例实体名），后来被复用到 spi_simple 迁移里。它不是为 spi_simple 量身定做的。

查找函数的三级回退在 [scripts/refactoring/hdlrefactor.py:59-77](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L59-L77)：

```python
def conv_fun(comp_name, signal, make_lower_case=False, use_all=False):
    ...
    try:
        ret = DICT[comp_name][signal_lower_case]      # ① 精确组件匹配
    except:
        try:
            if use_all:
                ret = DICT['#ALL#'][signal_lower_case] # ② 全局匹配
            else:
                ret = signal
        except:
            ret = signal                               # ③ 原样返回
    return ret
```

> 注意它用 `try/except` 而非 `in` 判断——「找不到键」被当作正常控制流（异常即回退）。这是该脚本一贯的风格：**快而糙**，依赖事后回归兜底。

四级 pass 里风险最高的是 `symbol_refactor`。[scripts/refactoring/hdlrefactor.py:263-304](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L263-L304) 对**整行**做 `re.sub`，把每个 `\w+` 单词都过一遍 `conv_fun(..., use_all=True)`：

```python
symbol_plus = re.compile(r'(\w+)', ...)
...
l = re.sub(symbol_plus, lambda m: conv_fun(comp_name, m.group(1), True, True), l)
```

这意味着注释里、字符串里的同名词也会被替换。它之所以「能用」，是因为迁移目标（`ReduceOr`→`reduce_or` 这类）在注释里出现也无伤大雅，且跑完有 CI 回归兜底。但这恰恰说明：**绝不能在日常开发里随手跑这个 pass**。

最后看编排脚本，它暴露了一个**真实的目录陷阱**。[scripts/refactoring/refactor_library_and_testbench.py:14-34](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/refactor_library_and_testbench.py#L14-L34)：

```python
set_refactor_database("./alpha.json")

for path in Path('../../hdl').rglob('*.vhd*'):        # ✓ 本仓库存在 hdl/
    ...
for path in Path('../../testbench').rglob('*.vhd*'):  # ✗ 本仓库是 tb/，不是 testbench/
    ...
path = "../../sim/config.tcl"
tcl_generics_refactor(path,path)
```

> **待确认/值得注意**：第二个循环引用 `../../testbench`，但用 `git ls-files` 查本仓库顶层目录，结果是 `bd / doc / drivers / hdl / scripts / sim / tb / xgui`——**只有 `tb/`，没有 `testbench/`**。所以这个脚本**按原样在本仓库里跑，第二个循环找不到任何文件**，testbench 不会被重构。这进一步证实它是从别的 PSI 仓库（那里测试目录叫 `testbench/`，例如 psi_common 自身）原样拷来的「借来工具」，并未针对本仓库的 `tb/` 布局调整。`../../hdl` 和 `../../sim/config.tcl` 这两条路径则是对的。结论：把它当历史迁移遗迹来读，不要当成可日常运行的工具。

#### 4.3.4 代码实践

**实践目标**：理解重构脚本「数据库驱动 + 正则全量替换」的工作方式与风险，学会判断它何时该用。

**操作步骤（源码阅读 + 推演型）**：

1. 在 [alpha.json](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/alpha.json) 里找一个 `spi_simple` 实例化的组件（如 `psi_common_spi_master` 或 `psi_common_sync_fifo`），记下它的几个旧→新端口映射。
2. 对照 `hdl/spi_simple.vhd` 里该组件的 `port map`，确认现在的端口名（如 `clk_i`、`rst_i`）确实是 alpha.json 里的「新名」——这是 `cf05975` 那次迁移的结果。
3. 推演：如果把 `symbol_refactor` 对一个**当代**的、已经是 snake_case 的 `.vhd` 文件再跑一遍会怎样？由于 `conv_fun` 查的是「旧名→新名」，已经是新名的符号查不到，会被原样返回，理论上幂等无害——但 `#ALL#` 里若恰好有冲突条目，仍可能误伤。**待本地验证**。
4. 验证目录陷阱：用 `git ls-files` 确认本仓库无 `testbench/` 目录，推断 [refactor_library_and_testbench.py:23](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/refactor_library_and_testbench.py#L23) 的第二个循环在本仓库是空跑。

**需要观察的现象**：迁移后的端口名与 alpha.json 的「新名」一一对应；脚本对当前已是新命名的代码基本是幂等的；testbench 循环因目录名不符而空跑。

**预期结果**：能说清「这套脚本是 `cf05975` 一次性迁移的产物，借自 psi_common 生态，目录路径未对本仓库校准，不应在日常流程里运行」。

#### 4.3.5 小练习与答案

**练习 1**：`symbol_refactor` 为什么危险？给一个可能误伤的具体场景。

**参考答案**：它对整行所有 `\w+` 单词做替换，不区分代码/注释/字符串。假如注释里写着「-- TODO: use ReduceOr here」，`#ALL#` 里又有 `ReduceOr→reduce_or`，这条注释也会被改成 `-- TODO: use reduce_or here`。功能虽不受影响，但说明它不是语法树级精确重构，只适合大规模机械迁移。

**练习 2**：`set_refactor_database` 的 `fix_case=True` 为什么是必须的？

**参考答案**：VHDL 大小写不敏感，源码里可能写作 `ReduceOr`、`reduceor`、`REDUCEOR` 任一形式。若不生成全小写副本做大小写不敏感匹配，只能命中数据库里登记的精确大小写，会漏掉大量实际写法。`fix_case=True`（[hdlrefactor.py:19-25](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/hdlrefactor.py#L19-L25)）就是为此补一份小写键的映射。

**练习 3**：本仓库里 [refactor_library_and_testbench.py:23](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/refactor_library_and_testbench.py#L23) 的 `../../testbench` 循环会生效吗？为什么？

**参考答案**：不会。本仓库顶层只有 `tb/` 目录（`git ls-files` 可证），没有 `testbench/`，`Path('../../testbench').rglob(...)` 匹配零个文件，循环体不执行。这条路径暴露了脚本是从测试目录名为 `testbench/` 的别的 PSI 仓库原样拷来的，未按本仓库布局校准。

---

## 5. 综合实践

**任务**：你刚改完 `hdl/spi_simple.vhd`（比如调整了一个 FIFO 的默认深度），请设计一条「依赖获取 → 仿真回归 → CI 判定」的最小验证流程，并指出 transcript 中**哪两个字符串**决定退出码。

**推荐流程（三条命令，待本地验证）**：

1. **依赖获取**（仅在首次或依赖更新后需要）：

   ```bash
   python scripts/dependencies.py
   ```

   前提：已 `pip install` 了 `PsiFpgaLibDependencies` 包（见 [README.md:43](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L43)）。它按 [README.md:18-35](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L18-L35) 的契约解析依赖，并以本仓库根为工作目录把 `psi_common` 等检出为兄弟目录，对上 `config.tcl` 的 `LibPath "../../.."`。

2. **仿真回归 + CI 判定**（二合一，每次改 RTL 都跑）：

   ```bash
   python scripts/ciFlow.py
   ```

   它会自动 `chdir` 到 `sim/`、跑 `vsim -c -do ci.do`（内部 `source run.tcl` 跑完整条 PsiSim 流水线）、再读 `Transcript.transcript` 判退出码。看脚本返回值即可：`0` 通过、`-1` 断言失败、`-2` 没跑完。

3. **（可选）交互式排错**：若上一步非 0，进 GUI 复跑定位：

   ```bash
   cd sim
   vsim -do interactive.tcl
   ```

   `interactive.tcl`（[sim/interactive.tcl](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/interactive.tcl)）只做到 `compile_files -all -clean` 后停下，把控制权交给你在 Tcl 控制台手动 `run_tb`、看波形。

**决定退出码的两个字符串**（见 [scripts/ciFlow.py:19-23](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/ciFlow.py#L19-L23)）：

- `###ERROR###` —— 出现则 `exit(-1)`（断言失败）；
- `SIMULATIONS COMPLETED SUCCESSFULLY` —— **不出现**则 `exit(-2)`（没跑完）；两者都不触发则 `exit(0)`（通过）。

> 原则上步骤 2 一条命令就把回归和判定都做了。把它拆成「依赖获取」和「ciFlow」两步，是为了凸显本讲的两个最小模块；步骤 1 仅在依赖缺失时才需要，常态开发里往往是省略的（依赖已在 `psi_fpga_all` 聚合仓库里就位）。

## 6. 本讲小结

- CI 判定本质是「Tcl 与 Python 之间靠纯文本 transcript 传话」：`ciFlow.py` 完全无视 vsim 退出码，只信 `Transcript.transcript` 里两个字符串的有无。
- 退出码三段式且**顺序敏感**：先 `###ERROR###`→`-1`，再查成功横幅缺失→`-2`，否则→`0`；这个顺序保证「断言失败但跑完」不会被误判为通过。
- `###ERROR###` 被两层扫描复用：Tcl 侧 `run_check_errors` 是给人看的软提示，Python 侧是给 CI 的硬判据；`config.tcl` 的消息抑制保持 transcript 干净，让真错误可见。
- 依赖解析是 SSOT 设计：README 依赖段被两个 HTML 哨兵夹住既是人读文档又是机器数据源，文件夹分组名同时是检出目录名；README、`config.tcl`、`dependencies.py` 三处共同锁定同一套三级目录结构。
- `scripts/refactoring/` 是一次性迁移工具（`cf05975`，psi_common 2.x→3.x 改名），数据库驱动 + 正则全量替换，`symbol_refactor` 风险最高；它和 CI 是两类工具，不可混用。
- 重构脚本是借自 psi_common 生态的「外来工具」：硬编码的 axi master 特例、以及 `../../testbench` 路径在本仓库（实为 `tb/`）空跑，都说明它未针对本仓库校准，应作历史遗迹对待。

## 7. 下一步学习建议

- **回归本手册已建立的宏观图景**：本讲把「CI/依赖/重构」三条工程化链路讲透了，建议回头对照 u3-l4（IP 打包流程）的 `package.tcl`，体会「改一处 RTL + 跑 ciFlow.py 验证 + 必要时重打 IP」这条完整开发闭环。
- **想深挖依赖解析的真实实现**：`Parse.FromReadme` 与 `Actions.ExecMain` 的具体逻辑在外部仓库 [PsiFpgaLibDependencies](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies)，可去那里读解析器如何处理版本约束与 `for development only` 标签。
- **想理解 PsiSim 命令链的内部**：`init`/`compile`/`run_tb`/`run_check_errors` 的实现在外部仓库 [PsiSim](https://github.com/paulscherrerinstitute/PsiSim) 的 `PsiSim.tcl`，能看清成功横幅 `SIMULATIONS COMPLETED SUCCESSFULLY` 到底在哪里、以什么措辞被打印——这正是 ciFlow.py 判定最脆弱的耦合点。
- **若要做二次开发**：建议建立「日常改动走 ciFlow.py、绝不碰 refactoring/」的纪律；如确需大规模改名迁移，应先备份、在隔离分支跑重构脚本、再用 ciFlow.py 回归兜底。

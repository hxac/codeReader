# 运行仿真：PsiSim / Modelsim 回归测试

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚在 `sim/` 目录下执行 `source ./run.tcl` 之后，后台到底按什么顺序做了哪些事（加载框架 → 初始化 → 读配置 → 编译 → 跑 TB → 查错误）。
- 读懂 `config.tcl` 里对四类源码（`psi_common`、`psi_tb`、项目 RTL、testbench）的声明方式，并理解 `../../..` 这个相对路径的含义。
- 解释 CI 是怎么用一行 `vsim -c -do ci.do` 跑完回归测试的，以及 `ciFlow.py` 用哪**两条规则**判定一次仿真到底是「通过」还是「失败」。

本讲是入门单元的最后一讲，承接 [u1-l2 仓库结构与外部依赖](u1-l2-repo-structure-and-dependencies.md)：上一讲我们知道了「这个仓库必须放在公共根目录的 `VivadoIp/vivadoIP_mem_test/` 下、并依赖 `PsiSim` 框架」，本讲就把这些前置条件串起来，真正跑一次仿真。

## 2. 前置知识

在动手之前，先用大白话建立几个概念：

- **回归测试（regression test）**：把一个项目里所有的 testbench（TB，测试平台）一次性全部编译、运行，确认「没有任何一个测试出错」。FPGA 工程里常见的做法是：跑完一轮，如果 Transcript（Modelsim 的输出日志）里没有任何 `###ERROR###` 标记，就算通过。
- **Modelsim / QuestaSim**：常用的 VHDL/Verilog 仿真器。它既能在图形界面里点按钮跑，也能用 TCL 脚本驱动跑。本项目的仿真完全靠 TCL 脚本驱动，所以也能在无界面的命令行模式下跑（CI 就是这样做的）。
- **PsiSim**：PSI 公共库自研的一个 TCL 仿真框架（依赖 `PsiSim`，见上一讲）。它把「建库、加源文件、编译、跑 TB、查错误」这些重复劳动封装成 `psi::sim::xxx` 一系列命令。本讲的 `run.tcl`、`config.tcl` 本质上就是按 PsiSim 的约定在调命令。
- **`vsim -c -do <do文件>`**：让 Modelsim 以命令行模式（`-c`）启动，并执行一个 `.do` 文件里的 TCL 指令。CI 用这种方式在无人工介入的情况下跑仿真。
- **Transcript**：Modelsim 的日志文件。脚本运行期间打印的所有内容（`puts`、编译警告、运行断言失败等）最终都会落到一个 `*.transcript` 文件里，CI 正是靠扫描它来判通过/失败的。

> 名词速查：**TB** = Testbench（测试平台，给被测对象喂激励、检查输出的顶层 VHDL 文件）；**TCL** = Tool Command Language，EDA 工具的通用脚本语言。

## 3. 本讲源码地图

本讲只看 `sim/` 和 `scripts/` 下的几个脚本，不碰任何 RTL（那些留给后续讲义）。

| 文件 | 作用 | 是否在本讲精读 |
| --- | --- | --- |
| [sim/run.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/run.tcl) | 「一键回归」主脚本：加载 PsiSim、读配置、编译、跑 TB、查错误。 | ✅ |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/config.tcl) | 声明仿真要用到的库与源文件清单，以及要跑哪个 TB。 | ✅ |
| [sim/ci.do](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/ci.do) | CI 入口 do 文件：`source run.tcl` 然后退出。 | ✅ |
| [scripts/ciFlow.py](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/ciFlow.py) | CI 驱动脚本：调用 `vsim`，再扫描 Transcript 判定通过/失败。 | ✅ |
| [sim/interactive.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/interactive.tcl) | 交互式调试用的脚本：只编译、不自动跑 TB，留给用户在 TCL 控制台手动操作。 | ✅ |
| [sim/.gitignore](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/.gitignore) | 忽略仿真产生的中间文件（库文件、`*.transcript`、`*.wlf` 波形）。 | 略读 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**① 仿真运行脚本（run.tcl）**、**② 库与源文件配置（config.tcl）**、**③ CI 仿真入口（ci.do + ciFlow.py + interactive.tcl）**。

### 4.1 仿真运行脚本：run.tcl

#### 4.1.1 概念说明

`run.tcl` 是「人工一键跑回归测试」的入口。你只要在 Modelsim 的 TCL 控制台里 `source ./run.tcl`，它就会从头到尾把整个仿真流程跑完。它不自己写逻辑，而是按固定的四步调用 PsiSim 框架提供的命令。

#### 4.1.2 核心流程

`run.tcl` 的执行流程是一条直线，没有分支：

```text
加载 PsiSim 框架
        │
   psi::sim::init        ← 初始化仿真环境（清状态、设变量）
        │
   source ./config.tcl   ← 把「库/源文件/TB」清单读进来
        │
   psi::sim::compile -all -clean   ← 干净地编译全部源文件
        │
   psi::sim::run_tb -all           ← 跑全部已声明的 TB
        │
   psi::sim::run_check_errors "###ERROR###"   ← 扫描 ###ERROR### 标记
```

这里有两点直觉性的设计：

1. **`-clean` 意味着「干净编译」**：每次都先清掉旧的编译产物再重编，保证你跑出来的结果是当前的源码，而不是上一次的残留。代价是慢一点，但对于回归测试来说「确定性」比「快」更重要。
2. **「错误」由标记决定**：PsiSim 不会自己去判断对错。它依赖 testbench 在发现错误时打印一个固定的字符串 `###ERROR###`，`run_check_errors` 就是去 Transcript 里 grep 这个字符串。这是本项目「定义通过/失败」的核心约定，4.3 节的 CI 也复用了同一约定。

#### 4.1.3 源码精读

`run.tcl` 全文只有 29 行，逻辑全部摆在外面：

先加载外部 PsiSim 框架——注意这个相对路径 `../../../TCL/PsiSim/PsiSim.tcl`，它正是上一讲强调的「公共根目录」结构（从 `sim/` 往上三级回到公共根）：

[sim/run.tcl:7-14](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/run.tcl#L7-L14) — 加载 PsiSim 框架、调用 `psi::sim::init` 初始化，随后 `source ./config.tcl` 读入库与源文件清单。

接着是「编译 → 跑 TB → 查错误」三段式，中间用 `puts` 打印分隔横幅，方便你在 Transcript 里一眼看清当前进度：

[sim/run.tcl:17-29](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/run.tcl#L17-L29) — 依次执行干净编译、运行全部 TB、最后用 `run_check_errors "###ERROR###"` 扫描错误标记。

注意 `run.tcl` 里没有任何「失败就 exit」的逻辑——它只是跑、只是扫。是否把扫到的错误升级成「CI 失败」，是 `ciFlow.py` 的职责（见 4.3 节）。所以手动跑 `run.tcl` 时，即便有错误，脚本本身也不会中断，你需要自己在 Transcript 里看 `###ERROR###`。

#### 4.1.4 代码实践

> **实践目标**：读懂 `run.tcl` 的四步流程，并验证「错误靠 `###ERROR###` 标记判定」这一约定。

操作步骤：

1. 打开 [sim/run.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/run.tcl)。
2. 把四步命令（`init` / `compile` / `run_tb` / `run_check_errors`）在脑海里和 4.1.2 的流程图对应一遍。
3. 用 `Grep` 在整个 `tb/` 目录里搜索 `###ERROR###`，确认 testbench 确实会主动打印这个标记（这就是 `run_check_errors` 能扫到它的原因）。

需要观察的现象：

- 你会发现 `###ERROR###` 这个字符串在 `run.tcl`、`ciFlow.py`、`tb/top_tb.vhd` 三处都出现，串起了一条「TB 报错 → 脚本扫描 → CI 判定」的链路。

预期结果：三个文件里的 `###ERROR###` 含义一致，都是「测试失败」的唯一信号源。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `psi::sim::compile -all -clean` 改成不带 `-clean`，会发生什么风险？

> **答案**：不带 `-clean` 时，PsiSim 会复用上次编译过的库，可能在改了源码却没被检测到变更时，仍然仿真旧的编译产物，得到「假的通过」。`-clean` 强制全量重编，牺牲速度换确定性，回归测试里值得。

**练习 2**：为什么 `run.tcl` 自己不写「发现 `###ERROR###` 就 `exit 1`」？

> **答案**：因为 `run.tcl` 面向人工交互，作者希望即使有错误，脚本也把整个回归跑完，让工程师一次性在 Transcript 里看到**所有**失败的用例，而不是在第一个错误处就中断。把「中断并报失败」这件事留给 CI 的 `ciFlow.py` 去做，实现了「人工模式 vs CI 模式」的职责分离。

### 4.2 库与源文件配置：config.tcl

#### 4.2.1 概念说明

如果说 `run.tcl` 是「流程骨架」，那 `config.tcl` 就是「内容清单」。它告诉 PsiSim 三件事：编译到哪个库、要编译哪些源文件（分成依赖库、项目源、TB 三类）、最后要跑哪个 TB。

#### 4.2.2 核心流程

`config.tcl` 是顺序声明的脚本，按「建库 → 屏蔽噪声 → 加四类源 → 声明 TB run」组织：

```text
set LibPath "../../.."          # 公共根目录（往上三级）

add_library mem_test            # 在 Modelsim 里建一个叫 mem_test 的库

compile_suppress / run_suppress # 屏蔽一些已知的、无害的警告/消息

add_sources  psi_common (8 个文件, -tag lib)   # 运行依赖：AXI 主从机等
add_sources  psi_tb      (3 个文件, -tag lib)   # 仿真依赖：断言比对等
add_sources  ../hdl      (3 个文件, -tag src)   # 本项目 RTL
add_sources  ../tb       (1 个文件, -tag tb)    # 本项目 TB

create_tb_run "top_tb"          # 声明一个 TB run，名为 top_tb
add_tb_run                      # 把这个 run 提交，使 run_tb -all 能跑到它
```

几个关键约定：

- **`-tag` 给源文件分组**：`lib`（外部依赖库）、`src`（本项目 RTL）、`tb`（本项目 TB）。这是 PsiSim 的惯例，方便后续按需只编译某一类（例如只重编 `src`）。
- **相对路径围绕公共根展开**：`psi_common` 在 `"$LibPath/VHDL/psi_common/hdl"`（即 `../../../VHDL/psi_common/hdl`），而项目自己的 RTL 在 `../hdl`、TB 在 `../tb`。这再次印证上一讲的结论——**仓库不能独立运行，必须摆在公共根目录下**。
- **屏蔽消息（suppress）**：Modelsim 会打印大量编号的提示/警告，`compile_suppress 135,1236` 和 `run_suppress 8684,3479,...` 把已知无害的编号压掉，让 Transcript 只留下真正需要关注的内容。

#### 4.2.3 源码精读

先把公共根路径和建库两步看完——`LibPath` 就是上一讲提到的公共根：

[sim/config.tcl:7-11](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/config.tcl#L7-L11) — 设置公共根相对路径 `../../..`，并创建名为 `mem_test` 的 Modelsim 库。

接着是消息屏蔽，编号对应 Modelsim 的 message ID：

[sim/config.tcl:13-15](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/config.tcl#L13-L15) — 编译期屏蔽 135、1236，运行期屏蔽 8684、3479、3813、8009、3812 等无害提示。

然后是四类源文件清单。先是 `psi_common`，注意它正好就是 RTL 里 wrapper 实例化的那几个组件（AXI 主机、AXI 从机、同步 FIFO、单口 RAM、流水线寄存器等）——**仿真需要的依赖和综合需要的依赖是同一套**：

[sim/config.tcl:18-34](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/config.tcl#L18-L34) — 声明 `psi_common` 的 8 个组件（含 `axi_master_simple`、`axi_slave_ipif`）和 `psi_tb` 的 3 个辅助包。

再是项目自己的 RTL 与 TB。本项目 RTL 极简，只有 3 个文件，TB 只有 1 个：

[sim/config.tcl:37-46](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/config.tcl#L37-L46) — 声明 `mem_test_pkg.vhd`、`mem_test.vhd`、`mem_test_wrapper.vhd` 三个 RTL 与 `top_tb.vhd` 一个 TB。

最后声明要跑哪个 TB。这两行是「让 `run.tcl` 里的 `run_tb -all` 真正有事可做」的关键——没有 `create_tb_run` / `add_tb_run`，`run_tb -all` 不会运行任何 TB：

[sim/config.tcl:48-50](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/config.tcl#L48-L50) — 创建 `top_tb` 的 TB run 并提交，使其进入待运行列表。

#### 4.2.4 代码实践

> **实践目标**：核对 `config.tcl` 声明的依赖与项目实际使用的依赖是否一致。

操作步骤：

1. 打开 [sim/config.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/config.tcl)。
2. 用 `Grep` 在 `hdl/mem_test_wrapper.vhd` 里搜索 `psi_common_axi_master_simple` 和 `psi_common_axi_slave_ipif`，确认它们确实被实例化。
3. 把 `hdl/` 目录下实际存在的 `.vhd` 文件名与 `config.tcl` 第 37–41 行声明的三个文件比对。

需要观察的现象：

- `config.tcl` 声明的项目源文件 = `hdl/` 下全部 RTL 文件（不多不少）。
- 依赖库里的 `axi_master_simple` / `axi_slave_ipif` 在 wrapper 里确实被用到，不是无谓声明。

预期结果：清单与实际「严丝合缝」。如果将来项目新增了一个 RTL 文件却忘了加进 `config.tcl`，仿真会编译失败——这就是「清单必须人工维护」的代价。

#### 4.2.5 小练习与答案

**练习 1**：`config.tcl` 里 `psi_common`、`psi_tb` 都带 `-tag lib`，而项目 RTL 带 `-tag src`、TB 带 `-tag tb`。这种分组有什么用？

> **答案**：PsiSim 允许按 tag 选择性操作，例如只重编译 `src` 而不动 `lib`，从而加快「改 RTL → 重跑」的迭代速度；同时 tag 也方便一眼在清单里区分「外部依赖」和「自有代码」。

**练习 2**：`LibPath` 设成 `"../../.."`。如果你把整个仓库挪到公共根下别的子目录（不再是 `VivadoIp/vivadoIP_mem_test/`），`config.tcl` 还能正常工作吗？

> **答案**：不能。`../../..` 是从 `sim/` 往上数三级到公共根的硬编码假设。一旦相对位置变了，`psi_common`、`psi_tb` 的路径就指空，编译会因找不到源文件而失败。这也是上一讲反复强调「必须放在 `VivadoIp/vivadoIP_mem_test/`」的原因。

### 4.3 CI 仿真入口：ci.do + ciFlow.py + interactive.tcl

#### 4.3.1 概念说明

CI（持续集成）需要在无人值守的服务器上跑仿真并自动判定通过/失败。本项目用一个极简的三件套实现这件事：

- `ci.do`：给 Modelsim 的入口 do 文件，内容只有「跑 `run.tcl`，然后退出」。
- `ciFlow.py`：CI 平台调用的总驱动，负责启动 `vsim`、读取日志、按规则给 CI 返回成功/失败。
- `interactive.tcl`：与本讲主线无关，是「交互式调试」的快捷脚本，留着 4.3.4 节顺带讲。

#### 4.3.2 核心流程

CI 判定逻辑可以用下面的伪代码概括（对应 `ciFlow.py` 的真实逻辑）：

```text
ciFlow.py:
    chdir 到 ../sim
    运行: vsim -c -do ci.do            # ci.do 内部: source run.tcl; quit
    读取 Transcript.transcript 全文

    规则一(预期错误): 内容里出现 "###ERROR###"        => 失败 (exit -1)
    规则二(意外未完成): 内容里没有 "SIMULATIONS COMPLETED SUCCESSFULLY"
                                                          => 失败 (exit -2)
    否则                                                  => 成功 (exit 0)
```

两条规则的分工非常重要，理解了它们就等于理解了整个 CI 的判据：

- **规则一是「主动报错」**：testbench 发现行为与预期不符时，会主动打印 `###ERROR###`（借助 `psi_tb` 的比对辅助过程）。一旦日志里有这个标记，无论仿真有没有跑完，都判定失败。
- **规则二是「兜底确认」**：`SIMULATIONS COMPLETED SUCCESSFULLY` 是 PsiSim 框架在所有 TB run 正常跑完后打印的结束语（不在本项目源码里，由 `run.tcl` 调用的 `psi::sim::run_tb -all` 触发框架输出）。如果连这句话都没有，说明仿真**根本没跑完**（比如编译失败、脚本中途异常），同样判定失败，但用不同的退出码 `-2` 区分「这是「没跑完」而不是「跑完发现错误」」。

退出码的区分让 CI 能在日志里一眼看出是哪种失败，便于排错。

#### 4.3.3 源码精读

先看 `ci.do`，它短到只有两行——`source run.tcl` 复用人工模式的那套流程，`quit` 让 `vsim -c` 跑完就退出，把控制权交还 CI：

[sim/ci.do:7-8](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/ci.do#L7-L8) — `source run.tcl` 后 `quit`，构成 CI 的 Modelsim 入口。

再看 `ciFlow.py`。先定位目录、启动 `vsim` 命令行模式跑 `ci.do`——注意 `os.chdir` 让脚本无论从哪里调用都能正确进入 `sim/`：

[scripts/ciFlow.py:7-13](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/ciFlow.py#L7-L13) — 切换到 `sim/` 目录，以命令行模式 `vsim -c -do ci.do` 跑仿真。

然后是读取日志和两条判定规则——这就是本讲的「核心约定」，也是练习题的考点：

[scripts/ciFlow.py:15-27](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/ciFlow.py#L15-L27) — 读取 `Transcript.transcript`，出现 `###ERROR###` 则 `exit(-1)`，缺失 `SIMULATIONS COMPLETED SUCCESSFULLY` 则 `exit(-2)`，否则 `exit(0)` 表示通过。

> 补充：`Transcript.transcript` 是 Modelsim 的运行日志。`sim/.gitignore` 里的 `*.transcript` 规则说明这类文件是**生成的中间产物**，不入库——所以它只有在仿真真正跑过之后才存在，`ciFlow.py` 能读到它的前提就是上一步 `vsim` 确实跑起来了。

#### 4.3.4 代码实践

> **实践目标**：掌握 CI 判定通过/失败的两条规则；在有环境时亲手跑一次回归。

**情况 A：本地装了 Modelsim 且已按上一讲拉好依赖**

操作步骤：

1. 用命令行进入项目的 `sim/` 目录（**必须**在 `sim/` 下，因为脚本里全是相对路径）。
2. 在 Modelsim TCL 控制台执行：
   ```tcl
   source ./run.tcl
   ```
3. 等待「Compile → Run → Check」三段全部跑完。
4. 翻看 Transcript，定位最后几行。

需要观察的现象：

- 日志里出现框架打印的 `SIMULATIONS COMPLETED SUCCESSFULLY`（或等价的 PsiSim 完成语）。
- 全文搜索 `###ERROR###`，应**没有**任何匹配。

预期结果：两条都满足，相当于 `ciFlow.py` 会走到 `exit(0)`，CI 绿灯。待本地验证（无环境请转情况 B）。

**情况 B：没有 Modelsim 环境（纯源码阅读型实践）**

操作步骤：

1. 打开 [scripts/ciFlow.py](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/ciFlow.py) 第 18–27 行。
2. 用自己的话把「成功」和「失败」各需要满足的条件写出来。
3. 思考：如果有人不小心在 `run.tcl` 里把 `psi::sim::run_check_errors` 那行删掉，CI 还能正确拦截失败吗？

需要观察的现象（思考题答案）：

- 即便删掉 `run.tcl` 里的 `run_check_errors`，CI 仍能拦截——因为 TB 打印的 `###ERROR###` 字符串本身已经写进了 `Transcript.transcript`，`ciFlow.py` 的规则一就是直接扫这个文件，并不依赖 `run_check_errors` 是否被调用。`run_check_errors` 只是给「人工模式」一个即时可见的错误提示，CI 有自己独立的扫描。

预期结果：你能说出 `exit(-1)` / `exit(-2)` / `exit(0)` 分别对应哪种结局，以及它们各自由哪条规则触发。

#### 4.3.5 小练习与答案

**练习 1**：请用一句话写出 `ciFlow.py` 判定「成功」的充要条件。

> **答案**：`Transcript.transcript` 里**没有** `###ERROR###` **且包含** `SIMULATIONS COMPLETED SUCCESSFULLY`，二者同时成立才 `exit(0)`。

**练习 2**：为什么 CI 用 `vsim -c` 而不是图形界面的 `vsim`？

> **答案**：CI 服务器通常是无图形界面的 Linux 机器，`-c` 让 Modelsim 以纯命令行批处理模式运行，不依赖任何 GUI、不需要人工点击，跑完即退——这正是自动化流水线需要的形态。

**练习 3**：`interactive.tcl` 和 `run.tcl` 的关键区别是什么？

> **答案**：`run.tcl` 是「全自动」——编译、跑 TB、查错误一条龙；而 [sim/interactive.tcl:10-19](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/interactive.tcl#L10-L19) 只做 `init` + `source config.tcl` + `compile_files -all -clean`，**编译完就停下**，把控制权留给工程师，方便在 TCL 控制台手动加载波形、单步跑 TB、查看信号——这是调试用的「半自动」模式。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**「画一张 CI 仿真闭环图」**的小任务：

1. **画流程**：用方框 + 箭头画出从「CI 平台触发」到「CI 得到绿/红灯」的完整链路，要求至少包含这些节点：`ciFlow.py` → `vsim -c` → `ci.do` → `run.tcl` → （PsiSim 的 `init`/`compile`/`run_tb`/`run_check_errors`）→ `Transcript.transcript` → 回到 `ciFlow.py` 的两条判定规则。
2. **标信号**：在图上标出「`###ERROR###`」由谁产生（`tb/top_tb.vhd`）、流经哪里、被谁扫描（`ciFlow.py` 规则一）；「`SIMULATIONS COMPLETED SUCCESSFULLY`」由谁产生（PsiSim 框架）、被谁扫描（`ciFlow.py` 规则二）。
3. **做断言**：假设你故意在 `tb/top_tb.vhd` 里制造一个错误断言，让它打印 `###ERROR###`。请预测：`run.tcl` 单独跑时 Transcript 长什么样？`ciFlow.py` 跑时退出码是多少？为什么？
4. **改一改（思考，不必真改源码）**：如果想把「失败时给出更友好的 CI 日志」，你会在 `ciFlow.py` 的哪一行后面加打印？为什么不能直接在 `exit(-1)` 之前依赖 `run.tcl` 已经打印过的信息？

> 参考答案要点：第 3 问——`run.tcl` 跑完后 Transcript 里会同时有 `###ERROR###` 和 `SIMULATIONS COMPLETED SUCCESSFULLY`（脚本不会因错误中断），但 `ciFlow.py` 规则一先命中 `###ERROR###`，于是 `exit(-1)`，CI 红灯。第 4 问——可以在 `if "###ERROR###" in content:` 命中后、`exit(-1)` 之前加打印，把命中的行号/上下文 dump 出来，方便排查；不能只依赖 `run.tcl` 的输出，因为 CI 看的是 `Transcript.transcript` 文件，`ciFlow.py` 是唯一能决定退出码的地方，加打印必须加在这里。

## 6. 本讲小结

- `run.tcl` 是「人工一键回归」入口，按 **加载 PsiSim → `init` → `source config.tcl` → `compile -all -clean` → `run_tb -all` → `run_check_errors "###ERROR###"`** 的固定四步执行，本身不判定通过/失败。
- `config.tcl` 是仿真「内容清单」，把 `psi_common`、`psi_tb`、项目 RTL、项目 TB 四类源文件按 `-tag` 分组声明，并用 `create_tb_run`/`add_tb_run` 声明要跑 `top_tb`；其中 `LibPath="../../.."` 再次印证「仓库须置于公共根目录」的前置条件。
- 错误判定的核心约定是字符串 `###ERROR###`：testbench 发现错误时打印它，脚本扫描它。
- CI 用 `ci.do`（`source run.tcl; quit`）作为 Modelsim 入口，用 `ciFlow.py` 调 `vsim -c -do ci.do` 跑完后扫描 `Transcript.transcript`。
- **CI 判定的两条规则**：出现 `###ERROR###` → `exit(-1)`（跑完但有用例失败）；缺少 `SIMULATIONS COMPLETED SUCCESSFULLY` → `exit(-2)`（没跑完）；否则 `exit(0)`（通过）。
- `interactive.tcl` 是「只编译、不自动跑 TB」的交互调试入口，与全自动的 `run.tcl` 互补。

## 7. 下一步学习建议

入门单元到此结束，你已经能跑通仿真并看懂 CI。接下来建议：

- **进入第二单元**：[u2-l1 寄存器地图：mem_test_pkg 详解](u2-l1-register-map.md)。在读懂 RTL 之前，先掌握软件是怎么通过寄存器配置这个 IP 的，这是理解 `tb/top_tb.vhd` 里控制进程在写哪些寄存器的前提。
- **顺带精读 testbench**：等学完 [u3-l3 主状态机](u3-l3-main-fsm.md) 后，回头重读 `tb/top_tb.vhd`，你会突然看懂本讲提到的「TB 主动打印 `###ERROR###`」到底是比对哪些信号失败时触发的——那是 `psi_tb_axi_pkg` 里断言辅助过程的功劳。
- **如果想自己加一个 TB run**：等熟悉 `config.tcl` 的 `create_tb_run` 机制后，可以尝试在 `tb/` 下新增一个 TB 文件并把它声明进 `config.tcl`，体会 PsiSim 的多 TB 管理方式。

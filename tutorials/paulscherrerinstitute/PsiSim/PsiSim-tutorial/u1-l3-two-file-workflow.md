# 两文件工作流与首次运行

## 1. 本讲目标

前两篇（u1-l1、u1-l2）我们认识了 PsiSim 的定位，也画出了 `PsiSim.tcl` 的“一页纸架构地图”——三大区块、17 个导出命令。但这些都是静态知识。本篇要带你**走第一条路线**：亲手把一次完整的回归测试跑起来。

读完本讲，你应当能够：

- **说清 `config.tcl` 与 `run.tcl` 各自的职责**，明白为什么 PsiSim 要把仿真脚本硬性拆成这两个文件；
- **默写一次完整回归测试的命令序列**：`init` → `source config.tcl` → `compile_files` → `run_tb` → `run_check_errors`，并能把这个序列与 `PsiSim.tcl` 里的对应 `proc` 一一对上号；
- **理解“嵌套”与“可合并性”从何而来**：为什么库级和项目级可以共用同一套 `config.tcl`，为什么这种写法天然对 Git 友好。

本讲是单元 1 的收尾，也是整个手册里第一次让你“动手”的一篇——后面的单元 2 会逐个命令拆细节，但在拆之前，你需要先从 30,000 英尺的高度看一次完整流程。

## 2. 前置知识

本讲假设你已经读过 u1-l1、u1-l2，知道：

- PsiSim 把全部命令封装在 `psi::sim` 命名空间下，用 `namespace import psi::sim::*` 把命令导入当前作用域后才能直接调用；
- 17 个导出命令分三类：General（`init`）、Configuration（建库/加源/定义测试运行/消息抑制）、Run（清理/编译/仿真/检查/调试）；
- `compile_files` 是 `compile` 的“安全外壳”，为了避免和 Modelsim 自带的 `compile` 命令重名。

除此之外，还需要几个最朴素的 TCL 概念（u1-l2 已介绍过，这里复习）：

- **`source <文件>`**：在当前 TCL 解释器里执行另一个 `.tcl` 文件的内容，相当于“把那个文件的代码原样粘贴到这里”。`source` **不会**新开一个干净的命名空间——被 `source` 的文件和你当前的环境共享变量与命令。这是理解“为什么 `config.tcl` 能直接调用 `add_library`”的关键。
- **`namespace import`**：把命名空间里被 `export` 的命令引入当前作用域，这样 `psi::sim::add_library` 就能简写成 `add_library`。
- **状态变量**：框架的“全局内存”。`init` 负责把它们清零，配置命令负责往里填数据，运行命令负责消费这些数据。

如果你对 VHDL 里 **testbench（测试台）** 和 **generic（类属参数）** 这两个词陌生，只需暂时记住：testbench 是“用来驱动并检查被测电路的顶层 VHDL 文件”，generic 是“在仿真开始前可以外部设定的参数”（例如时钟比例 `-gClockRatio_g=3`）。本讲主要看流程，不要求你立刻会写 VHDL。

## 3. 本讲源码地图

本讲只盯住“流程骨架”，不展开任何单个命令的内部实现（那是单元 2 的事）。涉及的关键源码只有两个文件：

| 文件 | 关键行 | 在本讲中的作用 |
| --- | --- | --- |
| `README.md` | 33–45、56–167 | 提供 `config.tcl` 与 `run.tcl` 的官方示例，是本讲的“教材原文” |
| `PsiSim.tcl` | 347–379（`init`）、381–389（`add_library`）、423–503（`add_sources`）、615–644（`create_tb_run`）、704–711（`add_tb_run`）、608–613（`compile_files`）、742–839（`run_tb`）、718–740（`run_check_errors`） | 把 `README` 示例里的每个命令对应到真实 `proc`，确认“示例里写的命令确实存在、确实这么干” |

> 本讲刻意**不**展开 `compile`、`run_tb`、`sal_*` 的内部逻辑。你只需要知道“这些命令被调用时大致发生了什么”，细节留给单元 2 和单元 3。

## 4. 核心概念与源码讲解

### 4.1 config.tcl 的职责

#### 4.1.1 概念说明

PsiSim 的核心设计哲学是：**“描述”与“执行”必须分离**。

- `config.tcl` 是**描述文件**：它只回答“这个工程里有哪些库、哪些源文件、哪些测试台、每个测试台要用哪些参数跑”。它**只填表，不干活**——里面的命令只修改框架的状态变量，绝不真正调用仿真器去编译或仿真。
- `run.tcl` 是**执行文件**：它负责生命周期管理——加载框架、初始化环境、把 `config.tcl` 读进来、然后真正去编译、仿真、检查结果。

为什么非要拆？因为 `config.tcl` 是**纯数据**，它不含任何与“当前在哪台机器、用哪个仿真器、从哪个目录跑”相关的信息。这意味着：

1. 同一个 `config.tcl` 可以被库自己的 `run.tcl` 用，也可以被项目顶层的 `run.tcl` 用（见 4.3 节的嵌套）；
2. `config.tcl` 几乎只有“加了哪些文件、跑了哪些参数”这种**业务事实**，Git diff 干净、合并冲突少——这正是 PsiSim 相对 Modelsim `.mpf` 工程文件最大的卖点（u1-l1 已提过）。

#### 4.1.2 核心流程

`README.md` 给出的 `config.tcl` 示例，按作用可以切成五段：

```text
config.tcl 的五大段
─────────────────────────────────────────────
① 准备        : set 常量 + namespace import   （导入命令）
② 建库        : add_library                   （声明库名）
③ 消息抑制    : compile_suppress / run_suppress（屏蔽烦人告警）
④ 加源文件    : add_sources ... -tag lib/src/tb（登记要编译的文件）
⑤ 定义测试运行: create_tb_run → tb_run_add_arguments → add_tb_run
                                                （登记要跑哪些 TB、用什么参数）
```

注意一个**关键事实**：`config.tcl` 里**没有 `init`**，也**没有任何 `compile_files` / `run_tb`**。它从头到尾只在做“登记”——把信息写进 `Sources`、`TbRuns` 这些状态变量里。真正读取并消费这些信息的，是 `run.tcl` 里的运行命令。

> 一句话记忆：**`config.tcl` 只负责“画菜”，`run.tcl` 才负责“下厨”。**

#### 4.1.3 源码精读

先看 `README` 对两文件分工的原话：

[README.md:33-45](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L33-L45) —— “通常写两个文件：`config.tcl` 描述要仿真哪些文件、测试台和运行；`run.tcl` 可以从 Modelsim 控制台运行，自动编译并执行所有仿真”，并明确指出拆分是为了支持**嵌套（nesting）**。

下面把 `config.tcl` 示例的五段，逐一对应到 `PsiSim.tcl` 里的真实 `proc`：

**① 准备**——`namespace import` 是 TCL 内置命令，本身不在 `PsiSim.tcl` 里。它依赖 `run.tcl` 先 `source PsiSim.tcl` 把 `psi::sim` 命名空间定义出来（见 4.2.3 节）。示例里的写法见 [README.md:61-62](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L61-L62)。

**② 建库**——`add_library psi_common`：

[README.md:64-65](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L64-L65) —— 示例建了一个名为 `psi_common` 的库。

[PsiSim.tcl:381-389](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L381-L389) —— `add_library` 的实现非常短：把库名追加进 `Libraries` 列表，同时把它设为 `CurrentLib`（“当前默认库”，后续没指定 `-lib` 的 `add_sources` 都会落到这里）。可以看到它**完全没碰仿真器**，只改状态变量——印证了“描述不干活”。

**③ 消息抑制**——`compile_suppress 135,1236` / `run_suppress ...`：

[README.md:66-69](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L66-L69) —— 把编译期/运行期不想看的告警编号登记进去。

[PsiSim.tcl:394-405](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L394-L405) —— `compile_suppress` 把编号拼到 `CompileSuppress` 字符串里（`run_suppress` 同理，[PsiSim.tcl:410-421](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L410-L421)）。同样是“只登记”，真正的抑制发生在后面的编译/运行命令里。

**④ 加源文件**——`add_sources <目录> { 文件清单 } -tag <标签>`：

[README.md:82-94](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L82-L94) —— 把工程源文件登记进库，并打上 `-tag src`。testbench 文件则打 `-tag tb`（[README.md:96-103](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L96-L103)）。标签是后面 `compile_files -tag`、`run_tb` 选择性编译/运行的依据。

[PsiSim.tcl:423-434](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L423-L434) —— `add_sources` 的文档注释，列出了 `-lib/-tag/-language/-version/-options` 全部可选参数。

[PsiSim.tcl:474-501](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L474-L501) —— 真正干活的循环：对每个文件名做 `glob` 展开通配符、构造一个 dict（含 `PATH/LIBRARY/TAG/LANGUAGE/VERSION/OPTIONS`）追加进 `Sources` 列表。注意第 487–493 行的“文件已存在”告警，和第 498–500 行“通配符没匹配到任何文件就跳过”的容错——这些都是初学者最容易踩的点（文件路径写错不会报错，只会 WARNING）。

**⑤ 定义测试运行**——`create_tb_run` → `tb_run_add_arguments` → `add_tb_run`：

[README.md:105-112](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L105-L112) —— 经典的“三段式”：先 `create_tb_run "psi_common_simple_cc_tb"` 开一个新运行，再用 `tb_run_add_arguments` 给它喂 **4 组**不同的 `ClockRatio_g`（意味着这个 TB 会被仿真 4 次，每次一组参数），最后 `add_tb_run` 把这个配置“封口”塞进 `TbRuns` 列表。

[PsiSim.tcl:615-644](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L615-L644) —— `create_tb_run` 初始化一个 `ThisTbRun` dict（注意 `TB_ARGS` 默认是 `[list ""]`，即“一组空参数，跑一次”）。

[PsiSim.tcl:676-683](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L676-L683) —— `tb_run_add_arguments` 直接把传入的参数列表覆盖到 `TB_ARGS`，所以传 4 个字符串就是 4 组参数。

[PsiSim.tcl:704-711](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L704-L711) —— `add_tb_run` 一行：把当前 `ThisTbRun` 追加到 `TbRuns`。这就是“封口”——封口后这个运行就不可再改了。

把上面五段连起来看，`config.tcl` 全程只往三个状态变量里写：`Libraries`（`add_library`）、`Sources`（`add_sources`）、`TbRuns`（`add_tb_run`），外加两个抑制串（`CompileSuppress`/`RunSuppress`）。**它完全没有调用仿真器。**

#### 4.1.4 代码实践

**实践目标**：通过阅读 `README` 示例，确认 `config.tcl` 里**没有**任何一条会触发仿真器的命令，从而亲手验证“描述与执行分离”。

**操作步骤**：

1. 打开 `README.md` 的 `config.tcl` 示例（第 56–136 行）。
2. 把示例里出现的每一条命令列出来：`set`、`namespace import`、`add_library`、`compile_suppress`、`run_suppress`、`add_sources`、`create_tb_run`、`tb_run_add_arguments`、`add_tb_run`。
3. 对照 4.3 节的命令分类表（u1-l2 已建立），标注每条命令属于“配置类”还是“运行类”。

**需要观察的现象**：

- 全部命令都属于**配置类**（或 TCL 内置的 `set`/`namespace import`）；
- 你**找不到** `init`、`compile_files`、`run_tb`、`run_check_errors` 中的任何一个——这些都是 `run.tcl` 的活。

**预期结果**：你会得到一张“`config.tcl` 只含配置命令”的清单。这是本讲的第一个铁律：**`config.tcl` 不含任何运行命令，也不含 `init`。**

> 待本地验证：如果你在自己的工程里发现 `config.tcl` 调用了 `compile_files` 或 `run_tb`，那它就破坏了 PsiSim 的设计约定，嵌套复用时大概率会出问题（详见 4.3 节）。

#### 4.1.5 小练习与答案

**练习 1**：示例 `config.tcl` 里 `add_sources` 出现了三次，分别打了 `-tag lib`、`-tag src`、`-tag tb`。如果不打 tag 会怎样？后面的编译/运行还能正常工作吗？

**参考答案**：能正常工作。tag 是**可选的**分组手段，不打 tag 时 `TAG` 字段是空字符串，`compile_files -all` 仍会编译它（`-all` 不关心 tag）。tag 的价值在于“选择性编译/运行”，例如 `compile_files -tag src` 只重编工程源码、跳过 testbench，从而加快迭代。细节在 u2-l2、u2-l5 展开。

**练习 2**：`create_tb_run` 之后如果没有调用 `tb_run_add_arguments` 就直接 `add_tb_run`（见示例第 134–135 行的 `psi_common_logic_pkg_tb`），这个 TB 会跑几次？

**参考答案**：跑 **1 次**，用默认参数。因为 `create_tb_run` 把 `TB_ARGS` 初始化成了 `[list ""]`（[PsiSim.tcl:634](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L634)），即“一组空参数”。`run_tb` 会为 `TB_ARGS` 里的每一组参数各仿真一次，所以空列表的 1 个元素 = 1 次仿真。

---

### 4.2 run.tcl 的标准命令序列

#### 4.2.1 概念说明

如果说 `config.tcl` 是“菜谱”，那么 `run.tcl` 就是“厨师按菜谱做菜的全过程”。`run.tcl` 的职责是**编排一次完整回归测试的生命周期**：先把厨房（框架环境）准备好，再把菜谱（`config.tcl`）读进来，然后依次编译、仿真、检查结果。

`README` 给出的 `run.tcl` 示例只有十几行，但这十几行就是 PsiSim 的“黄金序列”——几乎所有 PsiSim 工程的 `run.tcl` 都是它的变体。掌握这一节，你就掌握了 PsiSim 90% 的日常用法。

#### 4.2.2 核心流程

把 `run.tcl` 的黄金序列画成时间线：

```text
 run.tcl 执行时间线
═══════════════════════════════════════════════════════
 [1] source ../../../TCL/PsiSim/PsiSim.tcl
        └─ 把框架源码读进解释器，定义出 psi::sim 命名空间
 [2] namespace import psi::sim::*
        └─ 让 add_library / compile_files / ... 能直接用名字调用
 [3] init                       ←─── ① 必须第一个被调用：选仿真器 + 清零状态
 [4] source ./config.tcl        ←─── ② 把“菜谱”读进来：填满 Sources/TbRuns
 ────── 下面进入“执行”阶段 ──────
 [5] compile_files -all -clean  ←─── ③ 先清库，再按 Sources 顺序全部编译
 [6] run_tb -all                ←─── ④ 按 TbRuns 全部跑一遍仿真
 [7] run_check_errors "###ERROR###" ←─ ⑤ 扫 transcript，判定回归是否通过
═══════════════════════════════════════════════════════
```

七个步骤可以归纳成**三个阶段**：

| 阶段 | 步骤 | 作用 | 主要读写哪些状态变量 |
| --- | --- | --- | --- |
| 准备 | [1][2][3] | 加载框架 + 选仿真器 + 清零 | **写** `Simulator`/`Libraries`/`Sources`/`TbRuns`... |
| 配置 | [4] | `source config.tcl`，登记库/源/测试运行 | **写** `Libraries`/`Sources`/`TbRuns`/`*Suppress` |
| 执行 | [5][6][7] | 编译 → 仿真 → 检查 | **读** `Sources`、`TbRuns`、transcript 文件 |

注意阶段转换的关键点：步骤 [3] `init` 是**分水岭**。`init` 之前，状态变量还是“未定义/脏”的；`init` 之后，状态变成干净的初始值，配置命令才能安全地往里写。这也是为什么 `init` 必须是脚本里**第一个**被调用的 PsiSim 命令（u2-l1 会展开讲）。

#### 4.2.3 源码精读

`README` 的 `run.tcl` 示例原文：

[README.md:138-167](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L138-L167) —— 完整的 `run.tcl` 示例。下面逐行拆解。

**[1] 加载框架**：

[README.md:141](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L141) —— `source ../../../TCL/PsiSim/PsiSim.tcl`。这里的相对路径**取决于你的工程布局**，不是固定的。它的唯一目的是把 `PsiSim.tcl` 读进当前解释器。`source` 完成后，`psi::sim` 命名空间及其全部 `proc` 就存在了，但**还没有任何命令被实际执行**——状态变量也还没被初始化。

> 关键：`source PsiSim.tcl` 只“定义”了框架，没有“启动”它。启动靠下一步的 `init`。

**[2] 导入命令**：

[README.md:144](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L144) —— `namespace import psi::sim::*`。没有这一步，后面就得写全限定名 `psi::sim::init`、`psi::sim::compile_files`，啰嗦。导入后可以直接写 `init`、`compile_files`。

**[3] 初始化**：

[README.md:147](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L147) —— `init`（不带参数 = 默认用 Modelsim；要换 GHDL/Vivado 写 `init -ghdl` / `init -vivado`）。

[PsiSim.tcl:347-379](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L347-L379) —— `init` 的完整实现。重点看它做了三件事：

- **选仿真器**（第 354–367 行）：默认 `Simulator="Modelsim"`，根据 `-ghdl`/`-vivado` 改写；
- **清零状态**（第 368–373 行）：把 `Libraries`/`Sources`/`TbRuns` 重置为空列表、`CompileSuppress`/`RunSuppress` 清空、`CurrentLib` 设为一个哨兵值 `"NoCurrentLibrary"`；

[PsiSim.tcl:368-377](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L368-L377) —— 这一段是“干净初始状态”的来源。注意最后第 375、377 行还调了 `sal_init_simulator`（探测 Modelsim 版本）和 `clean_transcript`（清日志）。

**[4] 读取配置**：

[README.md:150](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L150) —— `source ./config.tcl`。这一行执行 `config.tcl` 里的全部配置命令，把 `Sources`、`TbRuns` 等填满。`source` 之后，框架状态里已经“记住了”所有要编译的文件和要跑的测试。

**[5] 编译**：

[README.md:156](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L156) —— `compile_files -all -clean`。`-all` 表示编译 `Sources` 里全部文件；`-clean` 表示先清库再编译（保证干净的“从零编译”）。

[PsiSim.tcl:608-613](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L608-L613) —— `compile_files` 只是个薄包装，它把参数 `join` 后 `eval "compile ..."`，调真正的 `compile`（[PsiSim.tcl:548-607](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L548-L607)）。`compile` 会遍历 `Sources`、按 `-all/-lib/-tag/-contains` 过滤、对每个文件调 SAL 的 `sal_compile_file`（真正去 `vcom`/`ghdl -a`/`xvhdl`）。本讲你只需知道“它遍历 `Sources` 并真正调用仿真器编译”，过滤细节在 u2-l5、SAL 细节在 u3-l3。

**[6] 仿真**：

[README.md:160](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L160) —— `run_tb -all`。遍历 `TbRuns`，对每个测试运行的每一组参数各仿真一次。

[PsiSim.tcl:742-751](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L742-L751) —— `run_tb` 的文档注释。注意第 742–743 行明确说：transcript 在跑 TB 前**自动清理**，前后脚本会自动执行。`run_tb` 内部（第 789 行起）对 `TbRuns` 里每个运行：检查过滤条件、检查 `SKIP`、跑 `PRESCRIPT`、对 `TB_ARGS` 每组参数调 `sal_run_tb`、最后跑 `POSTSCRIPT`。遍历细节在 u2-l6、SAL 细节在 u3-l4。

**[7] 检查**：

[README.md:165](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L165) —— `run_check_errors "###ERROR###"`。扫描 transcript 文件，看有没有匹配的错误串。

[PsiSim.tcl:718-740](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L718-L740) —— `run_check_errors` 的实现：读取 `./Transcript.transcript`、用 `regexp` 匹配传入的错误串（外加始终判错的 `Fatal:`），匹配到就打印 `!!! ERRORS OCCURED ...`，否则打印 `SIMULATIONS COMPLETED SUCCESSFULLY`。细节在 u2-l7。

把 [5][6][7] 连起来看：这三步真正在消费 `config.tcl` 阶段登记的数据——`compile_files` 读 `Sources`，`run_tb` 读 `TbRuns`，`run_check_errors` 读 transcript。**至此，“描述”被“执行”完整消费掉了。**

#### 4.2.4 代码实践

**实践目标**：把 `README` 的 `run.tcl` 示例压缩成一个“最小可运行”版本，并默写出在 Modelsim 控制台执行的命令顺序（本讲主实践的预热）。

**操作步骤**：

1. 复制 `README.md:138-167` 的 `run.tcl` 示例，删掉其中纯属排版的 `puts "----"` 分隔行。
2. 在剩下的 7 条核心命令旁边，用注释标出它属于“准备/配置/执行”哪个阶段。
3. 写下：假设你在 Modelsim 控制台里，要怎么启动这次回归？（提示：Modelsim 的 TCL 控制台本质上就是一个 TCL 解释器，所以你只需要 `do run.tcl` 或直接 `source run.tcl`）。

**需要观察的现象**：

- 去掉排版行后，核心命令恰好是 7 条，与 4.2.2 节的时间线一一对应；
- `init` 一定是第一个 PsiSim 命令，`source ./config.tcl` 紧随其后，编译/仿真/检查三条执行命令顺序固定不能换（必须先编译再仿真，最后检查）。

**预期结果**：你会得到一份带阶段注释的最小 `run.tcl`，以及一条“在 Modelsim 里执行 `run.tcl`”的命令。这就是后续综合实践的模板。

> 待本地验证：`do` 与 `source` 在 Modelsim 里都能执行 `.tcl` 文件，但 `do` 是 Modelsim 专属命令、`source` 是通用 TCL 命令。在没有 Modelsim 环境时，这一步只能“纸面推演”。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `run.tcl` 里 `init` 和 `source ./config.tcl` 的顺序对调（先 `source ./config.tcl` 再 `init`），会发生什么？

**参考答案**：会出问题。`config.tcl` 里的 `add_library`、`add_sources` 等命令会往 `Libraries`、`Sources` 等状态变量里写。如果 `init` 还没跑，这些变量要么未定义（TCL 会抛 `can't read ... no such variable`），要么还残留上一次运行的脏数据。随后 `init` 一旦执行，又把状态全部清零——`config.tcl` 登记的内容就**全丢了**。所以顺序必须是 `init` 在前、`source config.tcl` 在后。

**练习 2**：`compile_files -all -clean` 里的 `-clean` 是必须的吗？去掉它会怎样？

**参考答案**：不是必须的。`-clean` 的作用是在编译前先调 `clean_libraries` 清空库（[PsiSim.tcl:581-583](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L581-L583)），保证“从零编译”。去掉 `-clean` 就是**增量编译**——只编译 `Sources` 里登记的文件，库中已有且未改动的编译产物会保留，速度更快。日常迭代通常不带 `-clean`，只有在怀疑缓存污染、需要“干净复现”时才加上。

---

### 4.3 嵌套与多库调用

#### 4.3.1 概念说明

前面两节讲了**一个** `config.tcl` 配**一个** `run.tcl` 的单层用法。但 PsiSim 拆成两文件的最大回报，在于**嵌套（nesting）**：让库和项目能共用同一份配置，从而把“库的回归测试”和“项目的回归测试”统一管理。

真实场景是这样的：PSI 的 FPGA 工程通常由多个库组成（如 `psi_common`、`psi_tb`、项目自有库）。每个库：

- 有自己的 `config.tcl`，声明“我这个库有哪些源文件、哪些 testbench”；
- 有自己的 `run.tcl`，能**单独**跑通这个库的回归（开发库时只用跑这一层）。

而项目顶层还有一份 `run.tcl`，它不重写库的文件清单，而是直接 `source` 各个库的 `config.tcl`，把所有库的测试**汇总**进项目的回归。这就是 `README` 反复强调的“可合并性”在工程结构上的体现。

#### 4.3.2 核心流程

嵌套的调用关系是一棵树：

```text
项目顶层回归
─────────────────────────────────────
project/run.tcl
  ├── init                          （只在此层调一次！）
  ├── source libA/config.tcl        ← 把 libA 的库/源/测试登记进来
  ├── source libB/config.tcl        ← 把 libB 的库/源/测试登记进来
  ├── source project/config.tcl     ← 项目自有的源/测试
  ├── compile_files -all            ← 一次性编译所有库的文件
  ├── run_tb -all                   ← 一次性跑所有库的测试
  └── run_check_errors "###ERROR###"

库级单独回归（开发 libA 时）
─────────────────────────────────────
libA/run.tcl
  ├── source ../../PsiSim.tcl
  ├── namespace import psi::sim::*
  ├── init                          （库自己跑时，自己 init）
  ├── source ./config.tcl           ← 只读 libA 的配置
  ├── compile_files -all
  ├── run_tb -all
  └── run_check_errors "###ERROR###"
```

这里有两条铁律，理解了就理解了嵌套：

1. **`config.tcl` 不调 `init`**——它只登记数据。正因为它没有副作用，才能被任意层的 `run.tcl` 安全地 `source`。
2. **`init` 只在“当前要跑的那份 `run.tcl`”里调一次**——库级 `run.tcl` 自己 `init`；项目 `run.tcl` 也只 `init` 一次，然后连续 `source` 多个库的 `config.tcl`。**绝不能**在项目层 `source` 库的 `run.tcl`，否则会触发第二次 `init`，把前面 `source` 进来的库数据全部清掉。

至于“多库”，机制很简单：`add_library` 可以在一个 `config.tcl`（或被 `source` 的多个 `config.tcl`）里**多次调用**，每调一次就在 `Libraries` 列表里多一个库，并把 `CurrentLib` 切过去；后续的 `add_sources` 默认落到当前库。所以多库工程不需要新机制，只是多次 `add_library` + 分组 `add_sources` 而已。

> 多库工程的完整组织（含 `-tag` 分组、`-contains` 局部编译）是 **u4-l1** 的主题，本节只建立“嵌套可行”的直觉。

#### 4.3.3 源码精读

`README` 对嵌套的原话，是理解整个两文件拆分动机的“宪法级”说明：

[README.md:42-45](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L42-L45) —— “拆成两文件非常有用，因为它允许嵌套。例如每个库可以有自己的 `config.tcl` 和 `run.tcl`。如果只做库的开发，就执行库的 `run.tcl`；如果在项目里用这个库，项目的 `run.tcl` 可以 `source` 库的 `config.tcl`，把库的全部测试纳入项目回归。”

这条说明直接对应到 `config.tcl` 示例里第一个 `add_sources` 的写法：

[README.md:71-80](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L71-L80) —— 这里用 `set LibPath "../.."` 引用仓库根，再 `add_sources "$LibPath/psi_tb/hdl" {...}` 把**别的库**（`psi_tb`）的文件登记进当前工程。这正是“项目层 `source` 库的配置/文件”的雏形——`config.tcl` 通过相对路径引用其他库的源，而不重写它们。

再看多库的机制基础——`add_library` 确实支持多次调用：

[PsiSim.tcl:381-389](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L381-L389) —— 每次 `add_library` 都 `lappend Libraries $lib`（追加，不覆盖），所以调 3 次就有 3 个库；同时 `CurrentLib` 指向最新的库。这意味着一个 `config.tcl` 里完全可以写：

```tcl
add_library psi_common
add_sources ... -tag src
add_library psi_tb
add_sources ... -tag tb
```

两段文件会分别落到不同库，`compile_files -all` 会一起编译。

最后，`init` 的“清零”行为是嵌套铁律 2 的技术依据：

[PsiSim.tcl:368-373](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L368-L373) —— `init` 把 `Libraries`/`Sources`/`TbRuns` 全部重置为空列表。这就是为什么“在项目层 `source` 库的 `run.tcl`”会闯祸——库的 `run.tcl` 一旦执行 `init`，就会把你已经 `source` 进来的其他库的数据**一笔勾销**。

#### 4.3.4 代码实践

**实践目标**：在纸面上把“项目层 `run.tcl` 汇总两个库”的结构画出来，并指出其中唯一一次 `init` 出现在哪里。

**操作步骤**：

1. 假设有两个库 `libA`、`libB`，各自有 `config.tcl`。再假设项目自有源写在 `project/config.tcl`。
2. 写出 `project/run.tcl` 的核心命令序列（参照 4.3.2 节的树状图）。
3. 在你写的 `project/run.tcl` 里圈出 `init` 的位置，确认它只出现一次、且在所有 `source .../config.tcl` **之前**。
4. 思考：如果误把 `source libA/run.tcl` 写进了 `project/run.tcl`，哪一条命令会毁掉 `libB` 已登记的数据？

**需要观察的现象**：

- 正确的 `project/run.tcl` 里，`init` 只出现 1 次；
- 多个 `config.tcl` 是被**连续 `source`** 的，中间不能再有 `init`；
- 致命错误正是 `libA/run.tcl` 里那一行 `init`——它会把 `Libraries`/`Sources`/`TbRuns` 清空。

**预期结果**：你会得到一份“项目层汇总回归”的 `run.tcl` 草图，并在心里钉死两条嵌套铁律。这正是 u4-l1（嵌套配置与多库项目实践）要落地实现的模式。

> 待本地验证：本实践是源码阅读 + 结构设计型任务，无需仿真器即可完成。真正的多库工程搭建在 u4-l1 展开。

#### 4.3.5 小练习与答案

**练习 1**：为什么 PsiSim 强调 `config.tcl` “对版本控制友好、易于合并”，而 Modelsim 的 `.mpf` 工程文件不是？

**参考答案**：`config.tcl` 是纯文本、行级语义清晰（每行登记一个文件或一个测试运行），新增一个源文件就是新增一行 `add_sources`，Git diff 干净、合并时几乎不会冲突。而 `.mpf` 是 Modelsim 的私有工程文件，含绝对路径、二进制结构、内部 ID，改动一行源码可能导致整个文件大段变化，合并几乎必然冲突。这正是 PsiSim 拆出 `config.tcl` 的根本收益（u1-l1 已提过卖点，本节给出了工程结构上的落地）。

**练习 2**：项目层 `run.tcl` 想同时跑 `libA`、`libB`、自有代码三个来源的全部测试。下面两种写法哪个对？
（a）`init` → `source libA/config.tcl` → `source libB/config.tcl` → `source project/config.tcl` → `run_tb -all`
（b）`init` → `source libA/run.tcl` → `source libB/run.tcl` → `run_tb -all`

**参考答案**：（a）对。（b）错。`run.tcl` 内部会再次 `init`，把前面 `source` 进来的数据清空，最后只剩下 `libB` 的测试。（a）只 `init` 一次，然后连续 `source` 各库的 **`config.tcl`**（不含 `init`、不含运行命令），把三个来源的数据汇总到同一份 `TbRuns`，最后一次 `run_tb -all` 跑全部。这正是嵌套铁律 1、2 的直接应用。

---

## 5. 综合实践

**任务**：为一段已有的 VHDL 工程（哪怕只有 1–2 个源文件 + 1 个 testbench），亲手编写**最小化**的 `config.tcl` 和 `run.tcl`，并写出你在 Modelsim 控制台执行的命令顺序。这个任务把本讲三个模块（描述职责、执行序列、嵌套思想）全部串起来。

**准备**：假设你有如下最小工程（没有真实文件也可以用纸面推演，但建议找一两个真实 `.vhd` 文件手感更好）：

```text
my_project/
├── hdl/
│   └── counter.vhd          ← 被测电路
├── tb/
│   └── counter_tb.vhd       ← 测试台
├── config.tcl               ← 你要写
└── run.tcl                  ← 你要写
```

**要求**：

1. **写 `config.tcl`**，至少包含：一个 `add_library`、一个 `compile_suppress`（编个号，如 `135,1236`）、用两次 `add_sources` 分别登记 `hdl/`（打 `-tag src`）和 `tb/`（打 `-tag tb`）、一个 `create_tb_run` + `add_tb_run`（先不传额外参数，跑默认一次即可）。

2. **写 `run.tcl`**，严格按 4.2.2 节的黄金序列：`source PsiSim.tcl` → `namespace import psi::sim::*` → `init` → `source ./config.tcl` → `compile_files -all -clean` → `run_tb -all` → `run_check_errors "###ERROR###"`。注意 `source PsiSim.tcl` 的相对路径要按你实际放置 `PsiSim.tcl` 的位置调整。

3. **写出 Modelsim 控制台命令顺序**：从打开 Modelsim、`cd` 到工程目录、到执行 `run.tcl`，列出你会依次敲的命令。

**检查清单**：

- [ ] `config.tcl` 里**没有** `init`、`compile_files`、`run_tb`——它只登记数据；
- [ ] `run.tcl` 里 `init` 是**第一个** PsiSim 命令，且在 `source ./config.tcl` 之前；
- [ ] `run.tcl` 的执行三步顺序是 **编译 → 仿真 → 检查**，没有颠倒；
- [ ] 你能指出：如果将来这个工程要并入更大的项目，只需让大项目的 `run.tcl` `source` 这份 `config.tcl` 即可，**无需改动 `config.tcl` 本身**——这就是嵌套的回报。

**预期结果**：你会得到一份属于自己的最小 PsiSim 工程模板。以后每开一个新工程，都可以拿它当起点。等单元 2 学完各命令的细节，你还能用 `-tag`、`-contains`、`tb_run_add_arguments` 等把它打磨成生产级回归脚本。

> 待本地验证：完整跑通需要安装 Modelsim（或 GHDL/Vivado）。没有仿真器环境时，本实践可作为“纸面推演”完成——重点是 `config.tcl`/`run.tcl` 的结构与命令顺序正确。若用 GHDL，唯一区别是把 `run.tcl` 里的 `init` 改成 `init -ghdl`，并在独立 TCL 解释器（如 ActiveTCL）而非 Modelsim 控制台里运行（见 u4-l2）。

## 6. 本讲小结

- PsiSim 把仿真脚本硬性拆成两个文件：**`config.tcl`（描述）** 只登记库/源/测试运行到状态变量，不含 `init`、不含任何运行命令；**`run.tcl`（执行）** 负责加载框架、初始化、读取配置、编译、仿真、检查。
- `run.tcl` 的黄金七步序列：`source PsiSim.tcl` → `namespace import` → `init` → `source config.tcl` → `compile_files -all -clean` → `run_tb -all` → `run_check_errors "###ERROR###"`，可归纳为**准备 / 配置 / 执行**三阶段。
- `init` 是分水岭：它选仿真器并把 `Libraries`/`Sources`/`TbRuns` 等清零，因此**必须是第一个** PsiSim 命令，且必须早于 `source config.tcl`。
- `compile_files` 读 `Sources`、`run_tb` 读 `TbRuns`、`run_check_errors` 读 transcript——执行阶段消费配置阶段登记的数据。
- **嵌套**是两文件拆分的最大回报：库级有独立 `config.tcl`/`run.tcl` 可单独跑；项目层 `run.tcl` 只 `init` 一次，然后连续 `source` 各库的 `config.tcl` 汇总回归。铁律：`config.tcl` 不调 `init`，`init` 在每份被执行的 `run.tcl` 里只出现一次。
- 多库只需多次 `add_library`——每次调用追加一个库并切换 `CurrentLib`，无需新机制（完整实践见 u4-l1）。

## 7. 下一步学习建议

本篇你已经在“30,000 英尺”跑通了一次完整流程，但每个命令的内部我们都是一笔带过。单元 2 将带你**俯冲到地面**，按下面顺序逐个拆解：

- **u2-l1（命名空间、状态变量与 init）**：把你本讲只瞄了一眼的 `init` 彻底讲透——它到底重置了哪些变量、怎么解析 `-ghdl`/`-vivado`；
- **u2-l2（库与源文件管理）**：展开 `add_sources` 的 dict 数据模型与 `glob` 通配符；
- **u2-l3（测试运行定义）**：展开 `create_tb_run` → 配置命令 → `add_tb_run` 的两段式编程模型；
- **u2-l5（编译流程与过滤）**、**u2-l6（run_tb 与脚本钩子）**、**u2-l7（错误检查）**：分别拆解执行阶段的三条命令。

在读单元 2 之前，建议你：

- 把本讲的“最小 `config.tcl` + `run.tcl`”模板留在手边，单元 2 每讲一个命令，就回到模板里看它出现在哪个位置；
- 重读 `README.md` 的两份示例（[README.md:56-136](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L56-L136)、[README.md:138-167](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L138-L167)），现在你应该能看懂其中每一条命令的“阶段归属”了；
- 想提前了解多库与嵌套的工程实践，可跳读 **u4-l1**，但建议先把单元 2 的命令细节补齐。

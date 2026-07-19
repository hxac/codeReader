# 命名空间、状态变量与 init

## 1. 本讲目标

本讲是单元 2（进阶）的第一篇，我们从「会用」转入「读源码」。读完本讲，你应当能够：

- 说清 TCL 的 `namespace eval` / `variable` / `namespace export` 三件套在 PsiSim 里各自扮演什么角色，并能解释「为什么每个 `proc` 开头都要写一行 `variable X`」。
- 逐一列出 `psi::sim` 命名空间下的 10 个状态变量，说出它们存什么、由谁初始化、由谁填充、又被谁消费。
- 读懂 `init` 命令的参数解析（`-ghdl` / `-vivado`）与状态重置逻辑，并从源码角度解释「为什么 `init` 必须是脚本里第一个被调用的 PsiSim 命令」。

本讲只读一个源码文件：`PsiSim.tcl`，且只聚焦它的「骨架」——命名空间、状态变量和 `init`。具体的 `add_sources` / `create_tb_run` / `compile` 等业务命令留到 u2-l2 ~ u2-l7，SAL 抽象层留到单元 3。

## 2. 前置知识

在开始前，请确认你已经建立以下认知（它们在 u1-l1 ~ u1-l4 中讲过，这里只做最简回顾，不展开）：

- **PsiSim 是什么**：一个用 TCL 写的 VHDL 回归测试框架，支持 Modelsim / GHDL / Vivado 三种仿真器。
- **两文件工作流**：`config.tcl`（描述）只往状态变量里登记数据，`run.tcl`（执行）负责加载框架并跑流程。
- **黄金七步**：`source PsiSim.tcl` → `namespace import psi::sim::*` → `init` → `source config.tcl` → `compile_files -all -clean` → `run_tb -all` → `run_check_errors`。
- **三大分区**：`PsiSim.tcl` 在 `namespace eval psi::sim { ... }` 内分为 Namespace Variables（状态变量）、SAL（模拟器抽象层）、Interface Functions（导出命令）三块。

本讲要补充的，是上面这套结构「在 TCL 语言层面到底是怎么落地的」。如果你对 TCL 完全陌生，只需记住一句话：**TCL 里一切皆字符串，命令以空格分隔，`proc` 定义命令，`namespace` 给命令和变量分组**。剩下的我们在源码里边读边讲。

## 3. 本讲源码地图

本讲只涉及一个文件，但会在其中反复跳转：

| 文件 | 本讲关注的区域 | 作用 |
| --- | --- | --- |
| `PsiSim.tcl` | L14 `namespace eval psi::sim` | 整个框架的命名空间入口 |
| `PsiSim.tcl` | L16-L26 Namespace Variables | 10 个状态变量的声明 |
| `PsiSim.tcl` | L351-L378 `proc init` | 选择仿真器 + 重置状态 |
| `PsiSim.tcl` | L379 `namespace export init` | 把 `init` 标记为可导入 |
| `PsiSim.tcl` | L121-L147 `proc sal_init_simulator` | `init` 调用它来探测仿真器版本 |
| `PsiSim.tcl` | L713-L716 `proc clean_transcript` | `init` 调用它来清理 transcript |

> 小贴士：本讲的永久链接都指向当前 HEAD `434f6a9`，点击可直接跳到 GitHub 对应行。

## 4. 核心概念与源码讲解

### 4.1 namespace 机制与导出

#### 4.1.1 概念说明

TCL 的命名空间（namespace）是一个「容器」，用来把一组命令（proc）和变量（variable）圈在一起，避免和别人的代码撞名。PsiSim 把自己所有东西都装进 `psi::sim` 这个命名空间：

- `::` 是命名空间的分隔符，所以 `psi::sim` 表示「`psi` 命名空间里的 `sim` 子命名空间」。
- 一个 proc 一旦在 `namespace eval psi::sim { ... }` 内部定义，它的全名就是 `psi::sim::init`、`psi::sim::add_library`，调用时要么写全名，要么先 `namespace import` 再用短名。
- 一个变量一旦在命名空间里用 `variable` 声明，它就属于这个命名空间，所有 proc 共享同一份——这正是 PsiSim 用「全局状态变量」来串联配置阶段和运行阶段的基础。

这里有一个最容易让初学者困惑的点：**同一个关键字 `variable`，写在命名空间层和写在 proc 内部，作用并不完全相同**。下一节我们在源码里精确区分。

#### 4.1.2 核心流程

PsiSim 的命名空间生命周期可以概括成三步：

1. **定义（加载时）**：用户脚本执行 `source PsiSim.tcl`，解释器进入 `namespace eval psi::sim { ... }`，依次：
   - 用 `variable X` 声明若干命名空间变量（此时只是「注册名字」，未必有值）。
   - 用 `proc name {args} {body}` 定义一批命令（此时只是「登记命令」，并不执行命令体）。
   - 用 `namespace export name` 把某些命令标记为「可被 `namespace import` 导入」。
   - 整个 `source` 过程**不会运行任何业务逻辑**——所以即使在普通 `tclsh` 里 `source` 这个文件也不会报错（业务逻辑要等 `init` 才触发）。

2. **导入（运行前）**：用户脚本写 `namespace import psi::sim::*`，把所有**已导出**的命令以短名引入当前作用域，于是可以直接写 `init`、`add_library`，而不必写 `psi::sim::init`。

3. **使用（运行时）**：调用命令 → 命令体内部再用 `variable X` 把命名空间变量「链接」到 proc 的局部作用域 → 读写状态变量 → 驱动仿真器。

#### 4.1.3 源码精读

整个框架装在一个命名空间里，入口在：

[PsiSim.tcl:L14](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L14) —— `namespace eval psi::sim {` 开启命名空间，接下来 950 多行代码全部位于其中。

命名空间层用 `variable` 声明变量，注意这一段**只声明、不赋值**：

[PsiSim.tcl:L16-L26](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L16-L26) —— 连续 10 行 `variable`，把 `Libraries`、`CurrentLib`、`Sources`、`ThisTbRun`、`TbRuns`、`CompileSuppress`、`RunSuppress`、`Simulator`、`SimulatorVersion`、`TranscriptFile` 登记为命名空间变量。它们的「初值」要等到 `init` 才被填上（详见 4.3）。

对比一下 proc 内部的 `variable` 用法。任意一个会改状态的 proc 开头都会先把要用到的命名空间变量「链接」进来，例如 `init` 自己：

[PsiSim.tcl:L354](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L354) —— `variable Simulator "Modelsim"`。这里 `variable` 的作用是「在当前 proc 内把名字 `Simulator` 绑定到命名空间变量 `psi::sim::Simulator`，并顺便赋值为 `"Modelsim"`」。如果没有这一行，proc 内的 `Simulator` 会被当成一个不存在的局部变量。

再看导出机制。每个「对外命令」定义完，紧接着就有一行 `namespace export`：

[PsiSim.tcl:L379](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L379) —— `namespace export init`，把 `init` 加入导出列表，于是 `namespace import psi::sim::*` 之后用户就能直接写 `init`。`namespace export` 是**累加**的，每出现一次就把一个命令追加进导出列表。

**导出边界**是一个理解 PsiSim 架构的关键点，值得我们看一个「反面例子」：

- [PsiSim.tcl:L548](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L548) 定义了 `proc compile {args}`，但全文**没有** `namespace export compile`；导出的是它的包装 `compile_files`（[PsiSim.tcl:L613](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L613)）。原因已在 u1-l2 / u1-l4 提过：`compile` 与 Modelsim 自带命令重名，所以刻意不导出。
- [PsiSim.tcl:L713-L716](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L713-L716) 的 `clean_transcript` 也**没有** `namespace export`，注释直接写明 `# Internal Function`，因此它只能被框架内部（如 `init`、`run_tb`）以短名调用，用户脚本里若想用得写全名 `psi::sim::clean_transcript`。

> 一句话总结导出边界：**`namespace export` 决定了「公开 API」与「内部实现」的分界线**。PsiSim 共导出 17 个命令（见 u1-l4 的命令地图），未导出的 `compile` / `clean_transcript` 以及所有 `sal_*` 都属于内部实现。

#### 4.1.4 代码实践

**实践目标**：用纯 `tclsh`（不需要任何仿真器）验证「`source PsiSim.tcl` 只定义命名空间、不运行业务」这一论断，并亲手摸一摸命名空间内省命令。

**操作步骤**：

1. 在仓库根目录打开终端，启动纯 TCL 解释器（若未安装 TCL，可跳到步骤 4 做源码阅读型实践）：
   ```tcl
   tclsh
   ```
2. 在 `tclsh` 里加载框架：
   ```tcl
   source PsiSim.tcl
   ```
   预期：**没有任何输出**，也不会报错。因为整个文件只是定义命名空间。
3. 依次执行下列内省命令，观察输出：
   ```tcl
   namespace children ::psi        ; # 应能看到 sim 子命名空间
   namespace which -command ::psi::sim::init     ; # 确认 init 命令存在
   info commands ::psi::sim::*     ; # 列出所有命令（含未导出的 compile、clean_transcript、sal_*）
   namespace export -dictionary ::psi::sim::*    ; # 查看导出列表（应看到 17 个公开命令）
   ```
4. **若本机没有 `tclsh`**（源码阅读型实践）：打开 `PsiSim.tcl`，用编辑器搜索所有 `namespace export` 行，数一数一共有多少个，再与 `info commands ::psi::sim::*` 里你会预期的命令总数做差，指出哪些命令是「定义了却不导出」的内部命令。

**需要观察的现象**：

- 步骤 2 静默成功，证明加载阶段零副作用。
- 步骤 3 的 `info commands` 列表里**包含** `compile` 和 `clean_transcript`，但 `namespace export` 列表里**不包含**它们——这就是「定义 ≠ 导出」。

**预期结果**：导出命令数 = 17，`info commands` 的数量大于 17（多出来的至少有 `compile`、`clean_transcript` 以及 13 个 `sal_*`）。

> 说明：本实践不调用 `init`，因为 `init` 内部会调用 Modelsim 的 `vcom` 命令，纯 `tclsh` 里没有。命名空间层面的观察则完全安全。若你不确定本机 TCL 行为，相关结论标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：用户脚本里写 `namespace import psi::sim::*` 之后，为什么能直接用 `init` 而不必写 `psi::sim::init`？

> **答案**：`namespace import psi::sim::*` 会把该命名空间**已导出**的命令以短名引入当前作用域；`init` 在 [PsiSim.tcl:L379](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L379) 被 `namespace export init` 标记为导出，所以会被导入。

**练习 2**：如果用户脚本里不写 `namespace import`，还有别的方式调用 `add_library` 吗？

> **答案**：可以。直接用全名 `psi::sim::add_library mylib`。`namespace import` 只是把短名引进来，省去写全名的麻烦，并不是命令能被调用的前提。

**练习 3**：为什么 `sal_*` 开头的那些 proc（如 `sal_compile_file`）没有 `namespace export`？

> **答案**：它们属于 SAL 抽象层，是内部实现细节，只供框架自己的导出命令调用。不导出可以避免它们污染用户的命令空间，也明确表达了「这些不是公开 API，随时可能改动」。这是单元 3 的主题。

---

### 4.2 状态变量清单与用途

#### 4.2.1 概念说明

PsiSim 没有数据库，也没有配置文件解析器——它的全部「配置状态」都存在命名空间变量里。理解这 10 个变量，就理解了 PsiSim 的数据模型骨架。

可以把它想成一张「内存里的登记表」：

- **配置阶段**（`config.tcl` 里的命令）负责往这张表里**写**：登记有哪些库、有哪些源文件、有哪些测试运行、要抑制哪些告警。
- **运行阶段**（`compile_files` / `run_tb` / `run_check_errors`）负责从这张表里**读**，然后驱动仿真器。

`init` 的职责，就是在这张表被使用之前，把它**清成一张已知的白纸**。

#### 4.2.2 核心流程

10 个状态变量可以按职能分成四组：

1. **仿真器身份组**：`Simulator`、`SimulatorVersion`——决定 SAL 往哪条分支 dispatch、要不要带版本相关 flag。
2. **数据模型组**：`Libraries`、`CurrentLib`、`Sources`、`TbRuns`、`ThisTbRun`——承载用户声明的库/源/测试运行。
3. **消息抑制组**：`CompileSuppress`、`RunSuppress`——编译/运行时要屏蔽的告警编号。
4. **日志组**：`TranscriptFile`——transcript 文件的路径。

它们的生命周期是：`init` 清零 → 配置命令逐步填充 → 运行命令消费 →（下一次 `init` 再次清零）。

#### 4.2.3 源码精读

声明集中在 [PsiSim.tcl:L16-L26](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L16-L26)。下表把每个变量逐一对应到「存什么 / 谁初始化 / 谁填充 / 谁消费」：

| 变量 | 存什么 | `init` 里的初值 | 主要填充者 | 主要消费者 |
| --- | --- | --- | --- | --- |
| `Simulator` | 当前仿真器：`"Modelsim"` / `"GHDL"` / `"Vivado"` | `"Modelsim"`（可被 `-ghdl`/`-vivado` 覆盖） | `init` | 所有 `sal_*`（dispatch 依据） |
| `SimulatorVersion` | 仿真器版本号字符串 | 由 `sal_init_simulator` 设置 | `sal_init_simulator` | `sal_version_specific_flags` |
| `Libraries` | 库名列表（如 `psi_common psi_tb`） | `[list]`（空） | `add_library`（[L386](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L386)） | `clean_libraries`、Vivado 的 `sal_run_tb` |
| `CurrentLib` | 「当前默认库」，省略 `-lib` 时的落点 | `"NoCurrentLibrary"` | `add_library`（[L387](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L387)） | `add_sources`、`create_tb_run` |
| `Sources` | 源文件 dict 列表，每个含 PATH/LIBRARY/TAG/LANGUAGE/VERSION/OPTIONS | `[list]`（空） | `add_sources` | `compile` |
| `ThisTbRun` | 「半成品」测试运行 dict，正在被配置的那一个 | **`init` 不重置它** | `create_tb_run` 及一系列 `tb_run_*` | `add_tb_run` |
| `TbRuns` | 已定稿的测试运行 dict 列表 | `[list]`（空） | `add_tb_run`（[L709](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L709)） | `run_tb`、`launch_tb` |
| `CompileSuppress` | 编译期要抑制的告警编号，逗号串（如 `135,1236,`） | `""` | `compile_suppress` | `sal_compile_file` |
| `RunSuppress` | 运行期要抑制的告警编号，逗号串 | `""` | `run_suppress` | `run_tb` → `sal_run_tb` |
| `TranscriptFile` | transcript 文件的规范化路径 | 由 `clean_transcript` 间接设置 | `sal_set_transcript_file` | `sal_print_log`、`run_check_errors` |

**两个值得特别留意的细节**：

1. **`ThisTbRun` 不被 `init` 重置**。它是「正在编辑中的那一条测试运行」，只在 `create_tb_run` 时被整体重建（[PsiSim.tcl:L631](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L631)），定稿后由 `add_tb_run` 追加进 `TbRuns`。这是一种「半成品 / 成品」两段式编程模型，u2-l3 会专讲。
2. **`SimulatorVersion` 与 `TranscriptFile` 不在 `init` 的赋值块里直接出现**，而是分别通过 `sal_init_simulator`（[L375](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L375)）和 `clean_transcript`（[L377](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L377)）间接设置。这体现了 `init` 是个「协调者」：它自己只负责选仿真器和清数据模型，仿真器相关与日志相关的初始化分别委托给 SAL 和 transcript 子系统。

> 类型提示：`Libraries`、`Sources`、`TbRuns` 是 TCL **列表**（`lappend` 追加）；`Sources` / `TbRuns` / `ThisTbRun` 的元素是 **dict**（`dict set`/`dict get`）；`CompileSuppress` / `RunSuppress` 是**字符串**（字符串拼接）。这些数据结构细节会在 u2-l2、u2-l3、u2-l4 展开。

#### 4.2.4 代码实践

**实践目标**：本讲义规格指定的核心实践——为每个状态变量写一句注释，说明它存储什么，从而把上表内化为自己的理解。

**操作步骤**：

1. 打开 `PsiSim.tcl`，定位到 [L17-L26](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L17-L26)。
2. **不要修改源码**——在旁边打开一个笔记文件（或直接抄到本讲的实践记录里），在每一行 `variable X` 后面补一句中文注释。示例（前两条供参考，请你补全其余 8 条）：
   ```tcl
   variable Libraries        ; # 已登记的全部库名列表（list），由 add_library 追加
   variable CurrentLib       ; # 当前默认库：省略 -lib 时新源文件/测试台落入的库
   variable Sources          ; # TODO: 你来写
   variable ThisTbRun        ; # TODO: 你来写
   ...
   ```
3. 写完注释后，**回到 `init` 的赋值块** [PsiSim.tcl:L368-L373](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L368-L373)，圈出「哪些变量在这一块被显式重置」「哪些没有」。把没被重置的变量（至少有 `ThisTbRun`、`Simulator`、`SimulatorVersion`、`TranscriptFile`）逐一说明它们靠谁去初始化。

**需要观察的现象**：你会发现 `init` 的赋值块只覆盖了 6 个变量（`Libraries`、`Sources`、`TbRuns`、`CompileSuppress`、`RunSuppress`、`CurrentLib`），其余 4 个变量的初值来自 `init` 调用的子过程。

**预期结果**：你能不查表地说出每个变量「存什么、谁写的、谁读的」，并能解释「为什么 `ThisTbRun` 不需要 `init` 重置」（因为它只会在 `create_tb_run` 里被整体覆盖，不存在「残留」问题）。

> 本实践是纯源码阅读型，无需运行任何仿真器，结论可直接在源码中核实。

#### 4.2.5 小练习与答案

**练习 1**：`Sources` 和 `TbRuns` 都是「列表」，为什么 PsiSim 还要分两个变量，而不是合并成一个？

> **答案**：因为它们语义不同。`Sources` 描述「要编译哪些文件」，消费者是 `compile`；`TbRuns` 描述「要跑哪些测试运行」，消费者是 `run_tb` / `launch_tb`。两者生命周期也不同：先编译全部 `Sources`，再逐个跑 `TbRuns`。分开存放让每条命令只关心自己需要的数据。

**练习 2**：`CompileSuppress` 的初值为什么是空字符串 `""` 而不是空列表？

> **答案**：因为它后续是用**字符串拼接**的方式增长的（`compile_suppress` 里 `variable CompileSuppress $CompileSuppress$msg,`，见 [L401](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L401)），最后作为一段文本塞进 Modelsim 的 `-suppress` 参数。空字符串就是「不抑制任何告警」的自然初值。这是 u2-l4 的主题。

**练习 3**：如果用户在 `config.tcl` 里没调用过 `add_library` 就直接 `add_sources`（不带 `-lib`），会发生什么？

> **答案**：`CurrentLib` 仍是 `init` 设的哨兵值 `"NoCurrentLibrary"`，于是这些源文件会被登记到名为 `NoCurrentLibrary` 的「库」里，后续编译会出错。这正是 `init` 把 `CurrentLib` 重置成一个**显眼的非法值**（而不是空串）的设计意图——让错误尽早暴露、易于定位。

---

### 4.3 init 的参数解析与状态重置

#### 4.3.1 概念说明

`init` 是 PsiSim 唯一的 General 类命令，也是「分水岭」：它之前是 TCL/框架加载，它之后才是 PsiSim 业务。它干两件事：

1. **选仿真器**：通过 `-ghdl` / `-vivado` 两个开关，把 `Simulator` 设成 `GHDL` 或 `Vivado`；不写就默认 `Modelsim`。
2. **清状态**：把数据模型相关的状态变量重置成「空」，再把 `CurrentLib` 重置成哨兵值 `"NoCurrentLibrary"`，最后委托 SAL 探测版本、清理 transcript。

CommandRef 对它的描述很直白：「This command clears the PSI simulation environment… this command should be called once in every script before using any other commands.」（[CommandRef.md:L41-L44](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L41)）。

#### 4.3.2 核心流程

`init` 的执行流程伪代码如下：

```
proc init {args}:
    打印 "Initialize PsiSim"
    Simulator = "Modelsim"                  # 先设默认值
    遍历 args 里的每一个 token:
        若是 "-ghdl":   Simulator = "GHDL"
        若是 "-vivado": Simulator = "Vivado"
        否则:           打印 WARNING 并忽略
    Libraries       = 空列表
    Sources         = 空列表
    TbRuns          = 空列表
    CompileSuppress = ""
    RunSuppress     = ""
    CurrentLib      = "NoCurrentLibrary"
    sal_init_simulator()                    # 设置 SimulatorVersion
    clean_transcript()                      # 设置 TranscriptFile 并删旧 transcript
```

注意三个要点：

- **先设默认值，再用循环覆盖**：`Simulator` 一上来就是 `Modelsim`，循环只在看到开关时改写，所以「什么开关都不给」就等于选 Modelsim。
- **`-ghdl` 和 `-vivado` 都是「标志位」开关，不带取值**：解析循环遇到它们只设变量、不去读下一个 token（与 `add_sources` 里 `-lib <name>` 这种「带值参数」不同，那里会额外 `set i [expr $i+1]` 跳过取值）。
- **顺序敏感**：如果同时传 `-ghdl -vivado`，后出现的会覆盖前者，最终是 `Vivado`。这是一个不算 bug 但需要知道的行为。

#### 4.3.3 源码精读

[PsiSim.tcl:L351-L378](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L351-L378) 是 `init` 的完整实现。逐段看：

**a) 签名与默认仿真器**：

[PsiSim.tcl:L351-L354](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L351-L354) —— `proc init {args}` 用 `args` 接收任意数量参数；`set argList [split $args]` 把参数切成列表；先把 `Simulator` 默认设为 `"Modelsim"`。

**b) 参数解析循环**：

[PsiSim.tcl:L356-L367](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L356-L367) —— 手写的索引式解析器。逐个 token 判断：`-ghdl`→设 GHDL；`-vivado`→设 Vivado；其它 token 走 `sal_print_log "WARNING: ignored argument $thisArg"`。注意 `sal_print_log` 此时已经可用，因为 `Simulator` 已在上一行被赋值，SAL 的 dispatch 有据可依。

**c) 状态重置块**：

[PsiSim.tcl:L368-L373](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L368-L373) —— 把 `Libraries`/`Sources`/`TbRuns` 清成空列表，把 `CompileSuppress`/`RunSuppress` 清成空串，把 `CurrentLib` 重置为哨兵值 `"NoCurrentLibrary"`。这就是 CommandRef 说的「clears the PSI simulation environment」的真正落点。

**d) 委托给 SAL 与 transcript 子系统**：

[PsiSim.tcl:L375](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L375) —— 调 `sal_init_simulator` 探测版本号。以 Modelsim 为例，它会执行 `vcom -version`，把输出重定向到临时文件再读回，用正则 `\s([0-9\.]+)\s` 抠出版本号写入 `SimulatorVersion`（详见 [PsiSim.tcl:L121-L147](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L121-L147)，单元 3 会专讲这个「文件中转」技巧）。GHDL / Vivado 则直接写入占位字符串 `NotImplementedForGhdl` / `NotImplementedForvivado`。

[PsiSim.tcl:L377](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L377) —— 调 `clean_transcript`（内部命令，[L713-L716](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L713-L716)），它转调 `sal_clean_transcript`，删除旧的 `Transcript.transcript` 并把 `TranscriptFile` 指向新的 transcript 文件。

**那么，为什么 `init` 必须是第一个 PsiSim 命令？** 把上面四段连起来看，原因有三层：

1. **状态清零的必要性**：如果同一个 TCL 解释器里先跑过一次回归（残留了旧的 `Sources` / `TbRuns`），不 `init` 就会把这些陈旧数据带进新流程，产生难以定位的怪现象。`init` 的重置块（c 段）正是为了得到一张「已知白纸」。
2. **SAL dispatch 依赖 `Simulator`**：所有 `sal_*` 都靠 `if {$Simulator == "..."}` 来选择分支（如 [PsiSim.tcl:L34](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L34)）。不 `init`，`Simulator` 就没有被赋值，任何会触发 SAL 的命令都会走错分支或直接报错。
3. **版本探测与 transcript 必须先就位**：编译期 `sal_version_specific_flags` 要读 `SimulatorVersion`；运行期 `run_check_errors` 要读 `TranscriptFile` 指向的文件。这两个变量分别由 `sal_init_simulator` 和 `clean_transcript` 设置，而它们只在 `init` 里被调用。

所以 u1-l3 给出的「铁律」——`init` 必须是脚本里第一个 PsiSim 命令、且在每份被执行的 `run.tcl` 里只出现一次——并非随意规定，而是由源码里的数据依赖决定的。

#### 4.3.4 代码实践

**实践目标**：亲手跟踪 `init -ghdl` 调用后，10 个状态变量各自的取值，并用一句话回答「为什么 `init` 必须第一个调用」。

**操作步骤**：

1. 假设你在 GHDL 环境下（独立 `tclsh`）执行了 `init -ghdl`。对照源码 [L351-L378](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L351-L378)，填写下表第二列「调用后的值」：

   | 变量 | `init -ghdl` 之后的值 | 出处行号 |
   | --- | --- | --- |
   | `Simulator` | `"GHDL"` | L359 |
   | `SimulatorVersion` | `"NotImplementedForGhdl"` | L141 |
   | `Libraries` |  |  |
   | `Sources` |  |  |
   | `TbRuns` |  |  |
   | `CompileSuppress` |  |  |
   | `RunSuppress` |  |  |
   | `CurrentLib` |  |  |
   | `ThisTbRun` |  | （提示：未被 init 重置） |
   | `TranscriptFile` |  | L79（经 clean_transcript 设置） |

2. 在你的笔记里写一段话（3 句以内），从「状态清零 / SAL dispatch / 版本与 transcript 就位」三个角度，解释 `init` 为什么必须第一个调用。
3. **延伸思考（可选）**：如果把 `init` 里的 [L375 sal_init_simulator](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L375) 和 [L377 clean_transcript](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L377) 调用顺序对调，会出什么问题？（提示：`sal_clean_transcript` 内部在 GHDL/Vivado 分支会 `sal_set_transcript_file`，而 `sal_print_log` 的行为取决于 `Simulator`。）

**需要观察的现象**：你会确认 `init -ghdl` 之后，数据模型三件套（`Libraries`/`Sources`/`TbRuns`）为空、`CurrentLib` 是哨兵值、`SimulatorVersion` 是 GHDL 的占位串、`ThisTbRun` 维持原状（首次调用时是未定义/空）。

**预期结果**：表格第二列依次约为：`"GHDL"`、`"NotImplementedForGhdl"`、`{}`（空列表）、`{}`、`{}`、`""`、`""`、`"NoCurrentLibrary"`、未定义（首次）、`<规范化后的 ./Transcript.transcript 路径>`。

> 由于 `init` 在默认 Modelsim 分支会调用 `vcom`、在 GHDL/Vivado 分支也依赖外部可执行文件，本实践以「源码跟踪 + 表格填写」为主，标注为「待本地验证」的部分请你接入真实仿真器环境后再确认。

#### 4.3.5 小练习与答案

**练习 1**：`init` 的参数解析器为什么不支持 `init -ghdl -vivado` 同时生效？

> **答案**：因为 `-ghdl` 和 `-vivado` 都直接给 `Simulator` 赋一个不同的字符串（[L359](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L359) 与 [L361](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L361)），后执行的覆盖前者，`Simulator` 只能取一个值。PsiSim 一次回归只能针对一种仿真器，所以这是合理的。

**练习 2**：命令行里多打了一个未知参数 `init -ghdl -foo`，会发生什么？

> **答案**：`-foo` 既不是 `-ghdl` 也不是 `-vivado`，会进入 [L362-L365](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L362-L365) 的 `else` 分支，打印 `WARNING: ignored argument -foo`，然后继续。`init` 不会因此失败，`Simulator` 仍是 `GHDL`。这是一种「宽容解析」策略。

**练习 3**：从源码看，`init` 自己只显式重置了 6 个变量。如果用户**重复调用**两次 `init`，第二次会不会清掉第一次 `config.tcl` 登记的 `Sources`？

> **答案**：会。第二次 `init` 会再次执行 [L368-L373](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L368-L373) 的重置块，把 `Sources` 重新清成空列表。这正是「每份被执行的 run.tcl 里 `init` 只应出现一次」的内在原因——重复 `init` 会无声地丢掉之前登记的数据。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「预测状态」小任务。

**场景**：你在 GHDL 环境下，依次执行如下脚本片段（假设 `MyTb.vhd` 真实存在）：

```tcl
source PsiSim.tcl
namespace import psi::sim::*
init -ghdl
add_library psi_common
add_sources . {MyTb.vhd} -tag tb
```

**请你回答**：

1. 执行到 `source PsiSim.tcl` 之后、`init` 之前，`psi::sim::Simulator` 有没有值？为什么？（考察 4.1：加载阶段零副作用、`variable` 只声明不赋值。）
2. `init -ghdl` 之后，`Simulator`、`SimulatorVersion`、`CurrentLib`、`Libraries` 各是什么？（考察 4.3：参数解析与重置。）
3. `add_library psi_common` 之后，`Libraries` 和 `CurrentLib` 各变成什么？依据是 [L384-L388](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L384-L388) 的哪两行？（考察 4.2：状态变量由谁填充。）
4. `add_sources . {MyTb.vhd} -tag tb` 之后，`Sources` 里新增了一个 dict，请写出这个 dict 的 6 个键值（PATH/LIBRARY/TAG/LANGUAGE/VERSION/OPTIONS）。提示：`-lib` 未给，落点取决于 `CurrentLib`；其余缺省值见 [L439-L442](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L439-L442)。
5. 最后，用一句话总结：为什么上面这段脚本**必须**以 `init -ghdl` 开头，而不能直接从 `add_library` 开始？

**参考答案要点**：

1. 没有值（未定义）。`source` 只执行 `namespace eval` 体内的声明与定义，[L24](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L24) 的 `variable Simulator` 只声明、不赋值。
2. `Simulator="GHDL"`；`SimulatorVersion="NotImplementedForGhdl"`；`CurrentLib="NoCurrentLibrary"`；`Libraries={}`（空）。
3. `Libraries` 变成 `psi_common`；`CurrentLib` 变成 `psi_common`。依据 [L386](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L386)（`lappend Libraries $lib`）和 [L387](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L387)（`variable CurrentLib $lib`）。
4. `PATH=<规范化后的 MyTb.vhd 绝对路径>`、`LIBRARY=psi_common`、`TAG=tb`、`LANGUAGE=vhdl`、`VERSION=2008`、`OPTIONS=""`。（`LIBRARY` 取自 `CurrentLib`，其余取自 `add_sources` 的缺省值。）
5. 因为 `init` 同时完成了「选仿真器（设 `Simulator`，SAL dispatch 才有依据）」「清状态（保证 `Sources` 等从空开始，不带残留）」「探测版本与初始化 transcript」三件事；不先 `init`，`add_library` 虽能执行（`lappend` 到未初始化变量 TCL 会容忍），但 `Simulator` 未定义会让后续任何 SAL 调用出错，且状态不可预测。

> 提示：第 4 问涉及 `add_sources` 的内部实现，是下一讲 u2-l2 的主题；本讲只需你根据缺省值做出预测即可，精确行为可在 u2-l2 之后回头核实，相关结论标注为「待本地验证」。

## 6. 本讲小结

- PsiSim 全部实现装在 `namespace eval psi::sim { ... }` 里；命名空间层的 `variable` 只声明变量，proc 内的 `variable` 才把命名空间变量链接进局部作用域。
- `namespace export` 决定公开 API 边界：17 个命令被导出，`compile` / `clean_transcript` / 所有 `sal_*` 不导出，属于内部实现。
- 10 个状态变量按职能分成四组：仿真器身份（`Simulator` / `SimulatorVersion`）、数据模型（`Libraries` / `CurrentLib` / `Sources` / `ThisTbRun` / `TbRuns`）、消息抑制（`CompileSuppress` / `RunSuppress`）、日志（`TranscriptFile`）。
- `init` 干两件事：靠 `-ghdl` / `-vivado` 选仿真器（默认 Modelsim，后出现的覆盖前者）；显式重置 6 个数据模型相关变量，并把版本探测与 transcript 初始化分别委托给 `sal_init_simulator` 和 `clean_transcript`。
- 「`init` 必须第一个调用」是源码数据依赖的必然结果：状态需要清零、SAL dispatch 需要 `Simulator`、版本与 transcript 需要被初始化。
- `init` 并不重置 `ThisTbRun`——它是「半成品」变量，只在 `create_tb_run` 时整体重建，这是 u2-l3 两段式编程模型的基础。

## 7. 下一步学习建议

本讲搭好了「骨架」（命名空间 + 状态变量 + `init`）。接下来建议按以下顺序深入：

- **u2-l2 库与源文件管理（Sources 数据模型）**：精读 `add_library` 与 `add_sources`，看清 `Sources` 列表里每个 dict 的 6 个字段是怎么来的、`-lib` / `-tag` / `-language` / `-version` / `-options` 怎么解析、glob 通配符与去重警告如何工作。
- **u2-l3 测试运行定义（TbRuns 数据模型）**：精读 `create_tb_run` → `tb_run_*` → `add_tb_run` 的两段式模型，搞懂 `ThisTbRun` 这个「半成品」如何变成 `TbRuns` 里的「成品」。
- **u2-l4 消息抑制机制**：看 `CompileSuppress` / `RunSuppress` 这两个字符串是怎么拼接和去重的。
- 如果你对 SAL 更感兴趣，也可以直接跳到 **u3-l1 SAL 设计与 dispatch 模式**，但建议先读完 u2-l2 / u2-l3，这样进入 SAL 时你已经清楚它消费的是哪些状态变量。

建议继续阅读的源码入口：[PsiSim.tcl:L384-L388 `add_library`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L384-L388) 与 [PsiSim.tcl:L435-L503 `add_sources`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L435-L503)。

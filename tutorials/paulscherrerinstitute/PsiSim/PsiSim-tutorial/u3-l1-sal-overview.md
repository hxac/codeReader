# SAL 设计与 dispatch 模式

## 1. 本讲目标

本讲正式进入单元 3——模拟器抽象层（Simulator Abstraction Layer，下文统一简称 **SAL**）。前面两个单元我们一直把 `compile_files`、`run_tb`、`launch_tb` 这些命令背后「真正去敲仿真器命令」的那一段当作黑盒。本讲要打开这个黑盒，但只打开**最外面那一层壳**：SAL 的**设计思想**和**dispatch（分派）模式**。

读完本讲你应该能做到：

1. 说清楚 SAL **为什么存在**——它要屏蔽 Modelsim/GHDL/Vivado 三种仿真器之间的哪些差异，它的抽象边界在哪里（管什么、不管什么）。
2. 掌握 SAL 的核心实现手法——**在每个 `sal_*` proc 内部用 `if/elseif` 链读取命名空间变量 `Simulator`，分派到三套不同实现**。
3. 认识 SAL 里一套**统一却有点脆弱的错误处理范式**——`Unsupported Simulator` 提示，并理解它的代价。

本讲**只讲 SAL 的骨架与设计取舍**，不展开每个 `sal_*` proc 的具体命令翻译（`vcom` / `ghdl -a` / `xvhdl` 怎么拼、`vsim` / `--elab-run` / `xelab+xsim` 怎么跑）。那些细节是后续 u3-l2、u3-l3、u3-l4、u3-l5 四篇讲义的任务。换言之，本讲回答“**SAL 是个什么样的层、它怎么决定该走哪条路**”，后面四篇才回答“**每条路具体怎么走**”。

## 2. 前置知识

本讲默认你已经学完 u2-l1，熟悉下列概念。这里只做最简回顾，便于承接：

- **命名空间状态变量**：PsiSim 把全部状态放在 `namespace eval psi::sim { … }` 内的一组命名空间变量里。SAL 最关心的一个是 [`Simulator`](PsiSim.tcl#L24)（声明于 [PsiSim.tcl:24](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L24)），它记录「当前到底在用哪个仿真器」。
- **`init` 与 `Simulator` 的关系**：`init` 是唯一被导出的 General 类命令，靠 `-ghdl`/`-vivado` 开关选仿真器（默认 Modelsim），把结果写入 `Simulator`，并清零其它状态变量。详见 [PsiSim.tcl:351-378](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L351-L378)。
- **三层调用链**：用户脚本 → 接口函数（17 个导出命令，如 `compile_files` / `run_tb`）→ SAL（13 个 `sal_*` 内部 proc）→ 真实仿真器命令（`vcom`/`ghdl`/`xvhdl` 等）。SAL 处于中间层，**不导出、不对用户可见**。
- **TCL 的 `if/elseif`**：就是普通的条件分支；`{($a == "GHDL") || ($a == "Vivado")}` 这种用 `||` 连接的复合条件在 TCL 里必须整体用 `{}` 包起来。

> 一个直觉铺垫：Modelsim、GHDL、Vivado Simulator 三者**几乎没有共同命令**——编译一个 VHDL 文件，Modelsim 用 `vcom`，GHDL 用 `ghdl -a`，Vivado 用 `xvhdl`。SAL 存在的全部理由，就是把这「同一个意图、三种说法」收拢成「一个 proc 签名、内部分派」。

## 3. 本讲源码地图

本讲几乎只读一个文件：

| 文件 | 作用 | 本讲关注范围 |
| --- | --- | --- |
| [PsiSim.tcl](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl) | PsiSim 的唯一源码，全部实现集中在 `namespace eval psi::sim { … }` 内 | SAL 区块、`Simulator` 变量、`init` 对 `Simulator` 的赋值 |

PsiSim.tcl 内部与 SAL 相关的三个定位点：

- **状态变量声明**：[`variable Simulator`](PsiSim.tcl#L24)（SAL 分派的唯一依据）。
- **SAL 区块**：[PsiSim.tcl:28-342](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L28-L342)，以注释 `# Simulator Abstraction Layer (SAL)` 起头，到 `sal_open_wave` 结束，共 13 个 `sal_*` proc。
- **接口函数区**：[PsiSim.tcl:344](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L344) 起，以 `# Interface Functions (exported)` 起头。`init` 在这里把 `Simulator` 设定好。

一句话地图：**`Simulator` 变量是 SAL 的「总开关」，SAL 的 13 个 proc 各自读这个开关来决定走哪条分支**。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 SAL 的设计目标**、**4.2 dispatch 模式与 Simulator 变量**、**4.3 统一错误处理**。

### 4.1 SAL 的设计目标

#### 4.1.1 概念说明

SAL 是“**模拟器抽象层**”的缩写。抽象层的目的是**把变化点集中到一处**。

在 PsiSim 的语境里，“变化点”指的是：**同样的回归测试意图，落到三种仿真器上，命令完全不同**。举个最直观的对照：

| 意图 | Modelsim | GHDL | Vivado Simulator |
| --- | --- | --- | --- |
| 编译一个 VHDL 文件 | `vcom -2008 ...` | `ghdl -a --std=08 ...` | `xvhdl --2008 ...` |
| 跑一次仿真 | `vsim ... ; run -all` | `ghdl --elab-run ...` | `xelab ... ; xsim ...` |
| 清空日志 | `transcript off`/`transcript file` | 直接删文件 | 直接删文件 |

如果让每个接口函数（`compile`、`run_tb`、`launch_tb`）都各自写一遍 `if Modelsim ... elseif GHDL ... elseif Vivado ...`，那么：

- 同一套 `if/elseif` 会在多个地方重复，改一处要改多处；
- 接口函数会被仿真器细节淹没，失去可读性；
- 新增第 4 个仿真器时要找到所有散落的分支逐个改。

SAL 的解法是**把每个“意图”封装成一个 proc**（`sal_compile_file`、`sal_run_tb`、`sal_launch_tb`……），**proc 的入参是与仿真器无关的统一描述**（库名、路径、语言、版本……），proc **内部**才做 `if/elseif` 分派。于是接口函数只负责“准备统一描述、调用 SAL”，完全不需要知道仿真器差异。

> 用一句话定义 SAL 的边界：**SAL 只翻译“操作”，不管“数据”。** 数据模型（`Sources`、`TbRuns`）的建立与遍历是接口函数的职责（见 u2-l2、u2-l3、u2-l5、u2-l6）；SAL 只接受接口函数已经组装好的、与具体仿真器无关的参数，再翻译成对应的仿真器命令。

#### 4.1.2 核心流程

SAL 在整体调用链中的位置如下（伪流程）：

```
用户 config.tcl/run.tcl
        │  调用导出命令
        ▼
接口函数层（compile_files / run_tb / launch_tb / init ...）
        │  ① 组装与仿真器无关的统一参数（lib, path, language, version, ...）
        │  ② 调用 sal_*
        ▼
SAL（sal_compile_file / sal_run_tb / sal_launch_tb / ...）
        │  读取 Simulator 变量
        │  if/elseif 分派到 Modelsim / GHDL / Vivado 三套实现之一
        ▼
真实仿真器命令（vcom / ghdl -a / xvhdl；vsim / ghdl --elab-run / xelab+xsim）
```

要点：

1. **接口函数对仿真器无感**。比如 `compile`（[PsiSim.tcl:548-607](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L548-L607)）遍历 `Sources`、做过滤，最后把每个文件的统一字段交给 [`sal_compile_file`](PsiSim.tcl#L605)，自己不出现任何 `vcom`/`ghdl`/`xvhdl`。
2. **SAL proc 的签名是“统一契约”**。例如 [`sal_compile_file {lib path language langVersion fileOptions}`](PsiSim.tcl#L162) 这 5 个参数对三种仿真器含义一致，内部再各自翻译。
3. **SAL 不导出**。13 个 `sal_*` proc 都没有 `namespace export`，属内部实现，用户脚本不该也不需要直接调用它们。

#### 4.1.3 源码精读

先看 SAL 区块的起止注释和第一个 proc 的整体形状。SAL 区块以注释起头：

```tcl
#################################################################
# Simulator Abstraction Layer (SAL)
#################################################################
proc sal_print_log {text} {
    variable Simulator
    variable TranscriptFile
    if {$Simulator == "Modelsim"} {
        echo $text
    } elseif {($Simulator == "GHDL") || ($Simulator == "Vivado")} {
        puts $text
        ...
    } else {
        puts "ERROR: Unsupported Simulator - sal_print_log(): $Simulator"
    }
}
```

> SAL 区块头注释与首个 proc：[PsiSim.tcl:28-46](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L28-L46)。这里能看到 SAL 的标准写法：`variable Simulator` 把命名空间变量链入 proc，紧接着就是 `if/elseif/else` 分派。

再看一个“翻译同一个意图”的对照样例——清空/重建一个仿真库 [`sal_clean_lib`](PsiSim.tcl#L149-L160)：

```tcl
proc sal_clean_lib {lib} {
    variable Simulator
    if {$Simulator == "Modelsim"} {
        vlib $lib
        vdel -all -lib $lib
        vlib $lib
    } elseif {($Simulator == "GHDL") || ($Simulator == "Vivado")} {
        file delete -force $lib
    } else {
        puts "ERROR: Unsupported Simulator - sal_clean_lib(): $Simulator"
    }
}
```

> [PsiSim.tcl:149-160](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L149-L160)。

这段代码最能体现 SAL 的价值：**对外只暴露一个意图“清掉这个库”**，而 Modelsim 需要专门的三步 `vlib→vdel→vlib`，GHDL 和 Vivado 则只需 `file delete -force`。接口函数 `clean_libraries`（[PsiSim.tcl:510-537](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L510-L537)）只管遍历 `Libraries` 列表调 `sal_clean_lib`，对这层差异一无所知。

#### 4.1.4 代码实践

**实践目标**：体会“接口函数对仿真器无感、SAL 收拢差异”这一分层。

**操作步骤**：

1. 打开 [PsiSim.tcl](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl)，定位接口函数 `compile`（[PsiSim.tcl:548](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L548)）。
2. 通读它的循环体（[PsiSim.tcl:587-606](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L587-L606)），逐行判断：这里有没有出现 `vcom`、`vlog`、`ghdl`、`xvhdl` 中任何一个字样？
3. 再定位 SAL 的 [`sal_compile_file`](PsiSim.tcl#L162)，统计它内部出现了几种仿真器命令。

**需要观察的现象**：`compile` 里**完全没有**任何具体仿真器命令；所有具体命令都集中在 `sal_compile_file` 内部。

**预期结果**：你会确认——接口函数只负责“挑选哪些文件”，把“怎么编译”整个外包给了 SAL。这正是 SAL 设计目标达成的直接证据。

> 本实践为源码阅读型，不需要运行仿真器；“待本地验证”仅指若你想跑通，需要另行准备 VHDL 工程与对应仿真器环境。

#### 4.1.5 小练习与答案

**练习 1**：如果 PsiSim 要新增第 4 个仿真器（比如 Icarus Verilog），按当前架构，接口函数层（`compile`、`run_tb`）需要改动吗？

**参考答案**：理论上**不需要**。只要新仿真器能实现 SAL 已有的“统一契约”（即每个 `sal_*` proc 的入参语义），那么接口函数层可以原封不动。实际改动集中在 SAL——需要在每个相关 `sal_*` proc 的 `if/elseif` 链里新增一个分支。这正是抽象层“把变化点集中”的好处，也是它的代价（见 4.2、4.3）。

**练习 2**：SAL 的 proc 签名（如 `sal_compile_file {lib path language langVersion fileOptions}`）为什么强调“与仿真器无关”？

**参考答案**：因为签名是接口函数和 SAL 之间的**稳定契约**。如果签名里塞进了 `vcomOptions`、`ghdlStd` 这种仿真器专属概念，接口函数就被迫感知仿真器差异，分层就破了。把差异限定在 proc **体内**，签名才能保持中立。

---

### 4.2 dispatch 模式与 Simulator 变量

#### 4.2.1 概念说明

“dispatch（分派）”是 SAL 的实现核心。它的意思是：**程序运行到某个 `sal_*` proc 时，根据一个变量的当前值，选择走哪一段实现代码**。

在 PsiSim 里，这个“开关变量”就是命名空间变量 [`Simulator`](PsiSim.tcl#L24)。它的取值只有三种合法字符串：`"Modelsim"`、`"GHDL"`、`"Vivado"`。这三种值**只在一个地方被设定**——接口函数 `init`：

```tcl
variable Simulator "Modelsim"     ;# 默认
...
if {$thisArg == "-ghdl"}   { variable Simulator "GHDL" }
elseif {$thisArg == "-vivado"} { variable Simulator "Vivado" }
```

> 见 [PsiSim.tcl:354-361](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L354-L361)。

也就是说：**用户在 `run.tcl` 里写 `init` 还是 `init -ghdl` 还是 `init -vivado`，这一处选择决定了后续整个回归测试中所有 `sal_*` proc 走哪条分支**。`Simulator` 在 `init` 之后就保持不变（整个 `run.tcl` 生命周期里它是个“只被读、不再被改”的只读开关），直到下一次 `init` 才会重置。

> 这是一种典型的“**运行时按字符串分派**”手法。它不是面向对象里的多态、也不是函数指针表，而是最朴素的 `if/elseif` 串——胜在直白、易读，代价是分支逻辑在 13 个 proc 里重复出现。

#### 4.2.2 核心流程

SAL dispatch 的统一形状可以抽象成这样一个分段函数。设输入为 `Simulator` 的值，输出为“选中的实现分支”：

\[
\mathrm{branch}(s) =
\begin{cases}
\text{Modelsim 实现} & s = \text{"Modelsim"} \\
\text{GHDL 实现} & s = \text{"GHDL"} \\
\text{Vivado 实现} & s = \text{"Vivado"} \\
\text{Unsupported} & \text{其它}
\end{cases}
\]

PsiSim 的 13 个 `sal_*` proc 各自实现这个分段函数，但**不同 proc 对分支的“合并方式”不同**，主要有三种写法：

1. **三分支互不相同**：`if Modelsim ... elseif GHDL ... elseif Vivado ... else 错误`。
   典型：`sal_compile_file`、`sal_run_tb`、`sal_init_simulator`。
2. **GHDL 与 Vivado 合并**：`if Modelsim ... elseif (GHDL || Vivado) ... else 错误`。
   典型：`sal_print_log`、`sal_clean_lib`、`sal_clean_transcript`、`sal_exec_script` 等（因为这两个仿真器都是通过 `exec` 在命令行跑，行为相近）。
3. **只有一个分支**：只处理某一个仿真器，其余全部落到 `else` 报错。
   典型：`sal_launch_tb`（只 Modelsim）、`sal_open_wave`（只 GHDL）。

执行流程用文字描述就是：

```
进入 sal_xxx
  ↓
读取 variable Simulator
  ↓
if   Simulator == "Modelsim"            → 执行 Modelsim 实现
elseif Simulator == "GHDL"              → 执行 GHDL 实现
elseif (GHDL || Vivado) 之类合并条件     → 执行合并实现
elseif Simulator == "Vivado"            → 执行 Vivado 实现
else                                    → 打印 Unsupported Simulator
  ↓
proc 返回（无统一返回值约定）
```

#### 4.2.3 源码精读

先看 dispatch 的“电源”——`Simulator` 是怎么被 `init` 设定的（这段很关键，因为整个 SAL 都依赖它）：

```tcl
proc init {args} {
    puts "Initialize PsiSim"
    set argList [split $args]
    variable Simulator "Modelsim"
    set i 0
    while {$i < [llength $argList]} {
        set thisArg [lindex $argList $i]
        if {$thisArg == "-ghdl"} {
            variable Simulator "GHDL"
        } elseif {$thisArg == "-vivado"} {
            variable Simulator "Vivado"
        } else {
            sal_print_log "WARNING: ignored argument $thisArg"
            ...
        }
        set i [expr $i + 1]
    }
    ...
}
```

> [PsiSim.tcl:351-367](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L351-L367)。注意默认值是 `"Modelsim"`（[L354](PsiSim.tcl#L354)），`-ghdl`/`-vivado` 只是覆盖它；两个开关同时出现时，**后出现的覆盖先出现的**（因为只是顺序赋值）。

再看一个“三分支互不相同”的典型 dispatch——[`sal_compile_file`](PsiSim.tcl#L162) 的骨架（省略命令细节）：

```tcl
proc sal_compile_file {lib path language langVersion fileOptions} {
    variable Simulator
    variable CompileSuppress
    set vFlags [sal_version_specific_flags]
    if {$Simulator == "Modelsim"} {
        ...vcom / vlog...
    } elseif {$Simulator == "GHDL"} {
        ...ghdl -a...
    } elseif {$Simulator == "Vivado"} {
        ...xvhdl...
    } else {
        puts "ERROR: Unsupported Simulator - sal_compile_file(): $Simulator"
    }
}
```

> [PsiSim.tcl:162-205](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L162-L205)。这是最“对称”的 dispatch 形态——三种仿真器各有一段独立实现。

再看一个“GHDL 与 Vivado 合并”的典型——`sal_clean_lib`（已在 4.1.3 引用，[PsiSim.tcl:149-160](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L149-L160)），注意它的 `elseif` 条件是 `{($Simulator == "GHDL") || ($Simulator == "Vivado")}`，两个仿真器共用 `file delete -force $lib` 一段代码。

最后看“只有一个分支”的两种极端——

Modelsim 独占的 [`sal_launch_tb`](PsiSim.tcl#L309)：

```tcl
proc sal_launch_tb {lib tbName tbArgs suppressMsgNo wave} {
    variable Simulator
    if {$Simulator == "Modelsim"} {
        ...vsim... add wave...
    } else {
        puts "ERROR: Unsupported Simulator - sal_launch_tb(): $Simulator"
    }
}
```

> [PsiSim.tcl:309-333](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L309-L333)。

GHDL 独占的 [`sal_open_wave`](PsiSim.tcl#L335)：

```tcl
proc sal_open_wave {wave} {
    variable Simulator
    if {$Simulator == "GHDL"} {
        exec gtkwave -f $wave &
    } else {
        puts "ERROR: Unsupported Simulator - sal_open_wave(): $Simulator"
    }
}
```

> [PsiSim.tcl:335-342](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L335-L342)。

这两个 proc 之所以“只挂一个分支”，是因为它们背后的能力本身只属于某一种仿真器：交互式波形调试是 Modelsim 的原生工作流（GHDL 的调试走另一条路——先跑出 `.ghw` 波形文件再用 GTKWave 打开，见 u3-l5），而 `sal_open_wave` 正是为 GHDL 这条路准备的那一环。接口函数 `launch_tb` 在 [PsiSim.tcl:856](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L856) 做了前置过滤，所以正常流程里 `sal_launch_tb` 只会被 Modelsim 触发、`sal_open_wave` 只会被 GHDL 触发；proc 内部的 `else` 属于**防御性兜底**。

#### 4.2.4 代码实践

**实践目标**：把 13 个 `sal_*` proc 按 dispatch 形态分类，并验证“dispatch 的唯一判断依据是 `Simulator` 变量”。

**操作步骤**：

1. 用编辑器在 [PsiSim.tcl](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl) 里搜索字符串 `Unsupported Simulator`，你能定位到全部 13 处（每处对应一个 `sal_*` proc 的 `else` 兜底，见本讲 4.3 的源码核对）。
2. 对每个 `sal_*` proc，观察它的 `if/elseif` 条件，按下表分类填空（参考答案见 4.2.5）。
3. 在每个 proc 里确认：分派条件**只**依赖 `Simulator`，不依赖任何其它运行时变量（`CompileSuppress`、`TranscriptFile` 等只参与“实现内容”，不参与“走哪条分支”的判断）。

**需要观察的现象**：所有 13 个 proc 的第一行可执行代码都是 `variable Simulator`，紧接着就是基于 `$Simulator` 的条件判断。

**预期结果**：你会得到一张“proc → 形态”对照表（见 4.2.5）。这张表也是本讲“综合实践”的核心产出。

> 本实践为源码阅读型，待本地验证仅指若你想用 grep 复现搜索结果。

#### 4.2.5 小练习与答案

**练习 1**：把 13 个 `sal_*` proc 按 dispatch 形态分成三类（仅 Modelsim / 仅 GHDL / 三仿真器都覆盖）。参考答案：

| 类别 | proc | dispatch 形态 |
| --- | --- | --- |
| 仅 Modelsim（只挂 Modelsim 分支） | `sal_launch_tb` | `if Modelsim ... else 错误` |
| 仅 GHDL（只挂 GHDL 分支） | `sal_open_wave` | `if GHDL ... else 错误` |
| 三仿真器都覆盖（无论合并还是分开） | `sal_print_log`、`sal_transcript_off`、`sal_transcript_on`、`sal_set_transcript_file`、`sal_clean_transcript`、`sal_version_specific_flags`、`sal_init_simulator`、`sal_clean_lib`、`sal_compile_file`、`sal_exec_script`、`sal_run_tb` | `if Modelsim ... elseif GHDL/Vivado ... else 错误` 或三分支并列 |

其中“三仿真器都覆盖”的 11 个 proc 内部还可细分：`sal_compile_file` / `sal_run_tb` / `sal_init_simulator` 是**三分支并列**（GHDL 与 Vivado 各自独立）；其余 8 个是 **GHDL 与 Vivado 合并**（`elseif {($Simulator == "GHDL") || ($Simulator == "Vivado")}`）。

**练习 2**：为什么 GHDL 和 Vivado 经常被合并到同一个 `elseif`？

**参考答案**：因为它们在 PsiSim 里的执行方式高度相似——都是**通过 `exec` 在命令行调用外部可执行文件**（`ghdl`、`xvhdl`/`xelab`/`xsim`），日志统一走 `puts` + 写文件，没有 Modelsim 那套内建的 `transcript` 机制。所以凡是“日志输出、文件级清理、外部脚本执行”这类操作，两者实现一致，自然合并；而到了“具体编译命令/具体仿真命令”这种两者命令本质不同的地方（`sal_compile_file`、`sal_run_tb`），就必须拆成独立分支。

**练习 3**：dispatch 的判断依据是什么？如果有人误写 `init -GHDL`（大写），会发生什么？

**参考答案**：判断依据**唯一**是命名空间变量 `Simulator` 的字符串值，且**大小写敏感**（`==` 是精确字符串比较）。`init` 只识别小写的 `-ghdl`（[PsiSim.tcl:358](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L358)），写 `-GHDL` 会落入 `else` 被 `sal_print_log "WARNING: ignored argument ..."` 忽略，`Simulator` 保持默认值 `"Modelsim"`。于是用户以为在跑 GHDL，实际却在跑 Modelsim——这是字符串分派的一个典型坑。

---

### 4.3 统一错误处理（Unsupported Simulator）

#### 4.3.1 概念说明

SAL 里有一套**风格高度统一**的错误处理范式：**每个 `sal_*` proc 的 `if/elseif` 链最后，都挂一个 `else`，里面用 `puts` 打印一句格式完全一致的错误信息**：

```
ERROR: Unsupported Simulator - <proc名>(): <当前 Simulator 值>
```

这是 dispatch 模式的“安全网”。它的设计意图是：**万一 `Simulator` 取了一个意料之外的值（既不是 Modelsim/GHDL/Vivado，也没有被任何一个 `elseif` 接住），程序不会静默地走错路，而是打印一条带 proc 名和当前值的诊断信息**。

但要注意它**只是一条打印**，**不是抛异常、也不是中止脚本**。TCL 的 `puts` 之后，proc 会照常返回（通常返回空串），调用方（接口函数）并不会察觉这里出过错。这是 SAL 错误处理的一个明显弱点：**“Unsupported Simulator” 提示是“告知性”而非“阻断性”的**。

> 这个范式在 13 个 `sal_*` proc 里**逐字重复了 13 次**——同样的句式、同样的 `$Simulator` 插值，只是 proc 名不同。重复本身既是“风格统一”的好处，也是“缺乏抽象”的代价（见 4.3.5）。

#### 4.3.2 核心流程

```
sal_xxx 的条件链走到末尾
        ↓
没有命中任何已知仿真器分支
        ↓
进入 else
        ↓
puts "ERROR: Unsupported Simulator - sal_xxx(): $Simulator"
        ↓
proc 继续执行到结束（没有 error、没有 return 阻断）
        ↓
调用方拿到 proc 的（空）返回值，通常无感知
```

关键性质：

1. **统一格式**：13 处错误信息句式完全一致，便于在 `Transcript.transcript` 里用 `grep "Unsupported Simulator"` 一次性找全所有触发点。
2. **非阻断**：不使用 TCL 的 `error` 命令，所以不会被 `catch` 捕获、也不会中断 `run_tb` 的 `foreach` 循环。
3. **可观测性依赖日志**：因为不抛异常，错误是否被注意到，取决于 `sal_print_log` 写出的日志有没有被人或 CI 读到。这与 u2-l7 讲过的“`run_check_errors` 靠 grep transcript 判错”是一脉相承的设计取向——**PsiSim 普遍把成败判定推迟到事后读日志，而非运行时抛错**。

#### 4.3.3 源码精读

用一条 grep 命令就能看到全部 13 处范式的样本（这里列出几条代表）：

```tcl
# sal_print_log
puts "ERROR: Unsupported Simulator - sal_print_log(): $Simulator"

# sal_clean_lib
puts "ERROR: Unsupported Simulator - sal_clean_lib(): $Simulator"

# sal_compile_file
puts "ERROR: Unsupported Simulator - sal_compile_file(): $Simulator"

# sal_run_tb
puts "ERROR: Unsupported Simulator - sal_run_tb(): $Simulator"
```

> 错误行分别位于 [PsiSim.tcl:44](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L44)、[:55](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L55)、[:66](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L66)、[:77](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L77)、[:98](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L98)、[:115](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L115)、[:145](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L145)、[:158](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L158)、[:204](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L204)、[:219](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L219)、[:305](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L305)、[:331](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L331)、[:340](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L340)。

有两个细节值得特别指出：

**细节一：直接 `puts`，而不是 `sal_print_log`**。注意这些错误行用的是 TCL 内建 `puts`，而不是 SAL 自己的 `sal_print_log`。这是合理的防御——因为触发“Unsupported Simulator”时 `Simulator` 的值已经不可信，此时再去调 `sal_print_log`，它内部又要做一次 `if {$Simulator == ...}` 判定，结果很可能**再次**落入 `sal_print_log` 自己的 `else`，形成“错误里套错误”。直接 `puts` 保证至少诊断信息一定能打到控制台。

**细节二：错误行用 `puts`，未必进 `Transcript.transcript`**。`puts` 输出到 stdout，只有当用户把 stdout 重定向到日志文件时才会被 `run_check_errors` 读到（见 u2-l7）。这进一步印证：**Unsupported Simulator 是给人看的提示，不是程序级的失败信号**。

对比一下：接口函数 `launch_tb` 在 [PsiSim.tcl:856-859](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L856-L859) 里做了**前置拦截**（遇到非 Modelsim/GHDL 直接 `return`），这层检查用 `sal_print_log` 打印，且**带 `return` 阻断**后续动作。所以 PsiSim 的错误处理其实分两层：**接口函数层做阻断性检查**，**SAL 层做非阻断的兜底提示**。

#### 4.3.4 代码实践

**实践目标**：体会 SAL 错误处理“非阻断、可 grep、风格统一”的三个特征。

**操作步骤**：

1. 在 [PsiSim.tcl](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl) 中搜索 `Unsupported Simulator`，确认恰好 13 处，且每处都形如 `puts "ERROR: Unsupported Simulator - <proc>(): $Simulator"`。
2. 随便挑一个 proc（比如 `sal_clean_lib`），在脑中模拟 `Simulator` 被设成了一个非法值（如 `"modelsim"` 小写），跟踪它会如何一路落到 `else` 并打印，然后**继续返回**——注意 `else` 后面没有任何 `error` 或 `return`。
3. 想象在 `run_tb` 的 `foreach` 循环里（[PsiSim.tcl:789-836](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L789-L836)）某个 run 调到 `sal_run_tb` 命中了 Unsupported：循环会不会停？

**需要观察的现象**：错误提示打印后，proc 体并无中断语句；外层 `foreach` 会继续处理下一个 run。

**预期结果**：你会确认 Unsupported 提示**不会阻断回归流程**，只能靠事后读日志（或 `run_check_errors` 的 grep）发现。

> 待本地验证：若你想真实复现，可在本地 fork 中临时把 `init` 的默认值改成非法串再跑一次 `run_tb`，观察控制台与 `Transcript.transcript`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 SAL 的错误处理用 `puts` 而不是 TCL 的 `error` 命令？分别有什么后果？

**参考答案**：用 `puts` 是“告知性”的——打印后 proc 照常返回空串，调用方无感知，回归流程继续。若改用 `error`，会抛出 TCL 异常；除非调用方用 `catch` 包住，否则会**中断整个 `run.tcl`**。PsiSim 选择前者，与它“成败靠事后读 transcript 判定”的整体取向一致（见 u2-l7），代价是**运行期错误不会立即停下来**，可能被淹没在日志里。

**练习 2**：13 处 `Unsupported Simulator` 提示在句式上完全相同，只差 proc 名。这暴露了架构上的什么特点？如果要改进，你会怎么做？

**参考答案**：这暴露了 SAL “**用重复的 if/elseif 串代替抽象**”的特点——错误处理代码被复制了 13 遍。改进方向之一是把分派表化：例如用一个 `dict` 或数组把“仿真器名 → 该 proc 对应实现（或命令串）”登记好，dispatch 时查表，命中空槽再统一报错；这样新增仿真器或修改错误格式只需改一处。当然，TCL 里也可以用 `info commands` / 命名约定（如 `sal_clean_lib__Modelsim`）来模拟方法分派。这类重构正是 u4-l3（扩展新模拟器与架构取舍）要讨论的话题。

**练习 3**：`sal_launch_tb` 的 `else` 报错分支，在实际运行中真的会被触发吗？

**参考答案**：正常流程下**不会**。因为接口函数 `launch_tb` 在 [PsiSim.tcl:856](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L856) 已经做了前置过滤——只允许 Modelsim 和 GHDL 通过，且对 GHDL 走的是 `sal_run_tb`+`sal_open_wave` 这条路，根本不调 `sal_launch_tb`。所以 `sal_launch_tb` 实际上只在 Modelsim 下被调用。它的 `else` 属于**防御性兜底**，防止有人将来绕过接口函数、直接 `psi::sim::sal_launch_tb` 调用 SAL 时悄悄走错。同理 `sal_open_wave` 的 `else` 也是防御性的。

## 5. 综合实践

**综合任务**：为 PsiSim 制作一份《SAL dispatch 全景表》，作为你后续阅读 u3-l2 ~ u3-l5 的导航图。

请完成下面三件事：

1. **清点与分类**。通读 [PsiSim.tcl](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl) 的 SAL 区块（[L28-L342](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L28-L342)），列出全部 13 个 `sal_*` proc，按下表三列分类，并给出每个 proc 的源码行号区间。

   | 分类 | 含义 | proc 清单（含行号） |
   | --- | --- | --- |
   | 仅 Modelsim | 只有 `if Modelsim` 一个分支 | `sal_launch_tb`（L309-L333） |
   | 仅 GHDL | 只有 `if GHDL` 一个分支 | `sal_open_wave`（L335-L342） |
   | Modelsim+GHDL+Vivado 共用 | 三个仿真器都有对应实现（合并或分开均可） | 其余 11 个，请逐一填入行号 |

2. **说明 dispatch 判断依据**。用一两句话写清：所有分支判断的唯一依据是什么变量、这个变量在哪里被设定、设定后还会不会变化。引用 `init` 中设定 `Simulator` 的源码行（[PsiSim.tcl:354-361](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L354-L361)）作为证据。

3. **标注后续讲义落点**。对“三分支并列”的 `sal_compile_file`、`sal_run_tb`，分别标注它们的具体命令翻译将在哪一篇讲义展开（`sal_compile_file` → u3-l3；`sal_run_tb` → u3-l4）。这样这张表就成了你后续四篇讲义的索引。

**参考答案要点**（供自检，不要求逐字一致）：

- 仅 Modelsim：`sal_launch_tb`；仅 GHDL：`sal_open_wave`。
- 三仿真器共用 11 个：`sal_print_log`(L31)、`sal_transcript_off`(L48)、`sal_transcript_on`(L59)、`sal_set_transcript_file`(L70)、`sal_clean_transcript`(L82)、`sal_version_specific_flags`(L104)、`sal_init_simulator`(L121)、`sal_clean_lib`(L149)、`sal_compile_file`(L162)、`sal_exec_script`(L208)、`sal_run_tb`(L224)。
- dispatch 依据：命名空间变量 `Simulator`（[PsiSim.tcl:24](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L24)），由 `init` 在 [PsiSim.tcl:354-361](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L354-L361) 设定，设定后整个 `run.tcl` 生命周期内只被读、不再被改。
- `sal_compile_file` 的三种命令翻译见 u3-l3；`sal_run_tb` 的三种仿真命令见 u3-l4。

## 6. 本讲小结

- **SAL 是 PsiSim 的中间抽象层**，位于接口函数与真实仿真器之间，目的把“同一个意图、三种说法”的差异收拢到一处；它的边界是“**只翻译操作，不管数据**”——数据模型（`Sources`/`TbRuns`）仍由接口函数管理。
- **dispatch 模式**：每个 `sal_*` proc 开头 `variable Simulator`，紧跟 `if/elseif` 链，按 `Simulator` 的字符串值分派到 Modelsim/GHDL/Vivado 三套实现。`Simulator` 由 `init`（`-ghdl`/`-vivado`，默认 Modelsim）**一次性设定**，之后只读。
- **三种 dispatch 形态**：三分支并列（如 `sal_compile_file`/`sal_run_tb`/`sal_init_simulator`）、GHDL 与 Vivado 合并（多数日志/文件类 proc）、单分支独占（`sal_launch_tb` 仅 Modelsim、`sal_open_wave` 仅 GHDL）。
- **统一错误处理**：13 个 proc 各自的 `else` 都用 `puts` 打印同一句式的 `ERROR: Unsupported Simulator - <proc>(): $Simulator`，风格统一、便于 grep，但**非阻断**（不抛 `error`），成败仍靠事后读 transcript 判定。
- **代价与局限**：dispatch 靠 `if/elseif` 串实现，错误处理代码被复制 13 遍；`Simulator` 是大小写敏感的精确字符串比较，写错（如 `-GHDL`）会被静默忽略并回退到 Modelsim。
- **本讲只搭骨架**：具体每种仿真器的编译命令、运行命令、波形/调试路径，留给 u3-l2 ~ u3-l5 逐层展开。

## 7. 下一步学习建议

本讲建立了 SAL 的“分派骨架”，但每个分支**内部**到底执行什么命令还没展开。建议按以下顺序继续：

1. **u3-l2 transcript、日志与版本处理**：先读 `sal_print_log`、`sal_transcript_*`、`sal_clean_transcript`、`sal_version_specific_flags`、`sal_init_simulator` 这一组。它们是 SAL 里最基础的工具型 proc，其它 proc（编译、运行）都依赖它们打印日志。
2. **u3-l3 编译抽象（`sal_compile_file` / `sal_clean_lib`）**：进入三分支并列的编译命令翻译——`vcom`/`vlog`、`ghdl -a`（含 2002/2008 双编译）、`xvhdl`。
3. **u3-l4 仿真运行抽象（`sal_run_tb`）**：进入最复杂的三分支——`vsim`、`ghdl --elab-run`、`xelab+xsim`（含 Vivado 的 initfile workaround 与 generic 转换）。
4. **u3-l5 交互调试（`launch_tb` / 波形）**：收尾单分支独占的两个 proc——`sal_launch_tb`（Modelsim）与 `sal_open_wave`（GHDL+GTKWave），把调试工作流串起来。

读完这四篇，你就能把本讲那张《SAL dispatch 全景表》的每一格都填上具体命令，具备评估“新增第 4 个仿真器要改哪些地方”的能力——那是 u4-l3 的主题。

# 交互调试（launch_tb / 波形）

## 1. 本讲目标

本讲是单元 3（SAL 模拟器抽象层）的收尾篇。前面 u3-l3 讲了「怎么把源码编译进仿真库」，u3-l4 讲了「怎么把一个测试台一次性跑到结束（回归）」。本讲回答最后一个问题：**当某个测试台行为不对，你想停下来、看着波形、一步一步排错时，PsiSim 提供了什么入口？**

学完本讲，你应当能够：

- 说清 `launch_tb` 与 `run_tb` 的根本区别（启动而不执行 vs 启动并跑到结束）。
- 手动跟踪 `launch_tb -contains fifo -argidx 1 -wave -show` 这条命令的完整解析与分派过程。
- 解释为什么 Modelsim 走 `sal_launch_tb`（真正交互），而 GHDL 却复用 `sal_run_tb` 加 `.ghw` 波形文件（伪交互）。
- 复述 CommandRef 中给出的 GHDL/GTKWave 迭代调试工作流，并解释它为什么依赖「文件名稳定 + GTKWave Reload」。

---

## 2. 前置知识

本讲默认你已掌握以下内容（若陌生，请先回看对应讲义）：

- **TbRuns 数据模型（u2-l3）**：每个测试台 run 是一个 dict，关键字段包括 `TB_NAME`、`TB_LIB`、`TB_ARGS`（一组泛型参数串的列表）、`TIME_LIMIT`、`SKIP`。`TB_ARGS` 默认是 `[list ""]`，即长度为 1、只含一个空串的列表——这保证「即便用户没传泛型，也至少跑一次」。
- **`run_tb` 的执行模型（u2-l6 / u3-l4）**：`run_tb` 遍历 `TbRuns`，对每个 run 的**每一组** `TB_ARGS` 调用一次 `sal_run_tb`，跑到结束，不要波形。它是回归测试用的，输出全部落进 `Transcript.transcript` 供 `run_check_errors` 判错。
- **SAL 的 dispatch 模式（u3-l1）**：每个 `sal_*` proc 开头 `variable Simulator`，再用 `if/elseif` 按字符串 `Simulator`（取值 `"Modelsim"`/`"GHDL"`/`"Vivado"`）分派；`Simulator` 由 `init` 一次性设定后只读。
- **`sal_run_tb` 的签名（u3-l4）**：`sal_run_tb {lib tbName tbArgs timeLimit suppressMsgNo {wave ""}}`——第六个参数 `wave` 带默认空串，回归路径（`run_tb`）不传它，而本讲的 `launch_tb` 的 GHDL 分支正是复用它来落盘波形。

一个关键直觉：**Modelsim 是一个常驻的、可交互的 GUI 仿真器**（`vsim` 加载设计后可以手动 `run`、`restart`、看波形窗口）；**GHDL 是一个命令行的、批处理式的仿真器**（一次 `--elab-run` 跑到结束，没有可交互的仿真窗口，要看波形只能先落盘成文件再用 GTKWave 打开）。这个差异是本讲两条调试路径分叉的根本原因。

---

## 3. 本讲源码地图

本讲只涉及两个文件，集中在一个核心 proc 与两个 SAL proc：

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| `PsiSim.tcl` | `launch_tb`（L852–L964） | 唯一的接口函数：解析参数、选 run、按仿真器分派。 |
| `PsiSim.tcl` | `sal_launch_tb`（L309–L333） | SAL：**仅 Modelsim** 的交互启动（`vsim` 加载、可选自动加信号/跑/缩放）。 |
| `PsiSim.tcl` | `sal_open_wave`（L335–L342） | SAL：**仅 GHDL**，用 `gtkwave -f` 后台打开波形文件。 |
| `PsiSim.tcl` | `sal_run_tb`（L224–L307） | 已在 u3-l4 详解；本讲只看它的第六参 `wave` 如何在 GHDL 分支落成 `--wave=file.ghw`（L251–L253）。 |
| `CommandRef.md` | `launch_tb`（L496–L543） | 官方用法与 GHDL/GTKWave 工作流步骤（L537–L542）。 |

调用链一句话概括：

```
launch_tb  ──(Modelsim)──►  sal_launch_tb          （交互，不落波形文件）
            ──(GHDL)─────►  sal_run_tb(... wave)  （跑到结束，落 .ghw）
                         └► sal_open_wave         （gtkwave -f 后台打开）
```

注意：**Vivado 被 `launch_tb` 显式拒绝**（见 4.1.3），它没有调试路径。

---

## 4. 核心概念与源码讲解

### 4.1 launch_tb 的定位与参数解析

#### 4.1.1 概念说明

`launch_tb` 的定位写在它自己的注释里：

> Launch a testbench and keep the simulator window open for interactive debugging. Because this is meant for interactive debugging and not for regression test, neither pre- nor post-scripts are ran.

也就是说，它与 `run_tb` 有三处刻意不同：

1. **启动而不执行**：默认情况下它只把设计加载进仿真器，**不**自动 `run`，把控制权交还给人。
2. **不跑前后脚本**：回归时的 `PRESCRIPT`/`POSTSCRIPT` 在调试时被跳过（注释明说）。
3. **只启动第一个匹配**：`run_tb` 会遍历所有匹配的 run，而 `launch_tb` 命中第一个就 `return`（L959），因为人不可能同时盯两个波形窗口。

它支持的参数（与 `run_tb` 不同，是一套全新设计）：

| 参数 | 必填 | 含义 |
| --- | --- | --- |
| `-contains <str>` | **是** | 名字里含 `str` 子串的第一个 run（子串匹配，非正则）。 |
| `-argidx <int>` | 否 | 用 `TB_ARGS` 的第几组泛型（0 基）。省略则用「源码里的默认泛型」（传空串）。 |
| `-wave [<file>]` | 否 | Modelsim：可给一个 `.do` 文件恢复波形视图；不给参数则「加全部信号 + 跑完 + 缩放全图」。GHDL：落盘波形文件。 |
| `-show` | 否 | 仅 GHDL：跑完后用 GTKWave 打开波形。 |

#### 4.1.2 核心流程

`launch_tb` 的执行可以用下面这段伪代码概括：

```
launch_tb(args):
    if Simulator not in {Modelsim, GHDL}:   # Vivado 被拒
        报错返回
    解析 args → contains, argidx(默认"default"), wave(默认""), show(默认"")
    if contains 未设置:                       # -contains 是必填
        报错返回
    for run in TbRuns:
        if contains 不是 run.TB_NAME 的子串: continue
        打印 "*** Launch run.TB_LIB.run.TB_NAME"
        if run 被 skip（命中 Simulator 或 skip=="all"）: continue
        # 选泛型参数组
        if argidx == "default":  argsToUse = ""          # 源码默认泛型
        elif argidx >= len(TB_ARGS):  报错返回            # 越界保护
        else:                    argsToUse = TB_ARGS[argidx]
        if Simulator == "Modelsim":
            sal_launch_tb(lib, name, argsToUse, RunSuppress, wave)   # 交互
        if Simulator == "GHDL":
            if wave != "":  wave = "<name>_<argidx>.ghw"  # 决定文件名
            sal_run_tb(lib, name, argsToUse, timeLimit, RunSuppress, wave)  # 跑完落盘
            if show == "enable":  sal_open_wave(wave)     # gtkwave 后台打开
        return    # 只处理第一个匹配
    报错：-contains 没命中任何 run
```

四个值得记住的设计点：

- **`-contains` 是必填**：调试时你必须明确指出要调哪个 run，避免误启动一大堆。
- **`argidx == "default"`** 走空串路径（Changelog 2.4.0 的 bugfix「Make launch_tb with default arguments work (pass empty string)」正是修这里）。
- **GHDL 复用 `sal_run_tb`**：因为 GHDL 没有「加载但不跑」的概念，只能跑到结束并把波形落盘。
- **文件名 `<name>_<argidx>.ghw` 是稳定的**：只要你用同一个 `-argidx` 反复跑，就反复覆盖同一个文件名——这是 GTKWave「Reload Waveform」能生效的前提（见 4.3）。

#### 4.1.3 源码精读

**(1) 仿真器门禁——Vivado 被拒**

[`PsiSim.tcl:L854-L859`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L854-L859)：开头先用 `Simulator` 做白名单检查，只放行 Modelsim 和 GHDL，Vivado 直接报错返回。注意这与 SAL 内部「每个 proc 末尾 `else` 打印 Unsupported Simulator」是两层不同的防线——这里是接口层提前短路。

```tcl
variable Simulator
if {($Simulator != "Modelsim") && ($Simulator != "GHDL")} {
    sal_print_log "ERROR: launch_tb: this command is only implemented for Modelsim and GHDL"
    return
}
```

**(2) 参数解析——尤其 `-wave` 的三种形态**

[`PsiSim.tcl:L862-L896`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L862-L896)：用与 `run_tb`/`compile` 同款的朴素 `while` 循环逐个吃参数，未知参数只警告。四个本地变量初值很关键：`contains="All-regex"`、`argidx="default"`、`wave=""`、`show=""`。

最巧妙的是 `-wave` 分支 [`PsiSim.tcl:L878-L888`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L878-L888)：

```tcl
} elseif {$thisArg == "-wave"} {
    set i [expr $i + 1]
    set thisArg [lindex $argList $i]
    if {$thisArg == ""} {
        set wave "all"                  # -wave 后面没东西（命令末尾）→ 全信号模式
    } elseif {$thisArg == "-show"} {
        set wave "all"                  # -wave 紧跟 -show → 全信号 + 开 GTKWave
        set show "enable"
    } else {
        set wave $thisArg               # -wave foo.do → 用 foo.do 作为波形视图
    }
}
```

它先 `set i [expr $i + 1]` 把下标推进一位，再去读「下一个 token」。三种情况：

- 读到空串（说明 `-wave` 在命令末尾，下标越界，`lindex` 返回 `""`）→ `wave="all"`。
- 读到字面量 `-show`（即用户写的是 `-wave -show` 两个连在一起）→ `wave="all"` 且 `show="enable"`，并且把 `-show` 一并吃掉（不会再进下面的 `-show` 分支重复处理）。
- 读到别的字符串 → 当作 `.do` 文件名。

> 这段 `elseif` 链不是炫技，是修 bug 修出来的。Changelog 2.4.0 里有一条：「Option combinations in launch_tb did not work when -wave was used without filename」——说的就是不加这个判断时，`-wave` 不带文件名的组合会解析失败。

注意 `-show` 单独出现时 [`PsiSim.tcl:L889-L890`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L889-L890) 走另一个分支，只置 `show="enable"`。

**(3) `-contains` 必填校验**

[`PsiSim.tcl:L898-L902`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L898-L902)：如果解析完 `contains` 还是哨兵初值 `"All-regex"`，说明用户没传 `-contains`，报错返回。这是 `launch_tb` 唯一的必填参数。

**(4) 遍历选 run + 选泛型 + 越界保护**

[`PsiSim.tcl:L904-L937`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L904-L937)：遍历 `TbRuns`，用 `string first` 做子串匹配（与 `run_tb` 一致，非正则）。命中后先做 skip 检查（逻辑与 `run_tb` 完全相同，`lsearch` 大小写敏感），再选泛型：

```tcl
set argListLength [llength $allArgLists]
if {$argidx == "default"} {
    set argsToUse ""                              # 用源码默认泛型
} elseif {$argidx >= $argListLength} {
    set maxIdx [expr $argListLength-1]
    sal_print_log "ERROR: launch_tb: -argidx out of range 0 ... $maxIdx"
    return                                        # 越界保护
} else {
    set argsToUse [lindex $allArgLists $argidx]   # 取第 argidx 组
}
```

> 易错点：`TB_ARGS` 默认是 `[list ""]`，长度为 1。也就是说，如果你定义 run 时**没有**调 `tb_run_add_arguments`，那么 `argListLength==1`，此时传 `-argidx 1` 会触发 `1 >= 1` 的越界报错（合法范围只有 `0`）。想用 `-argidx 1`，run 必须至少定义了 2 组泛型。

**(5) 分派 + 只跑一个**

[`PsiSim.tcl:L939-L960`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L939-L960)：两条分支分别交给 4.2、4.3 详解。注意末尾的 `return`（L959）——无论 Modelsim 还是 GHDL，处理完**第一个**命中就退出整个 proc，绝不像 `run_tb` 那样继续遍历。

#### 4.1.4 代码实践

**实践目标**：手动模拟参数解析器，确认你对 `-wave` 三态的理解，而不依赖真实仿真器。

**操作步骤**：

1. 打开 `PsiSim.tcl` L862–L896，对照下面的 5 条命令，**在纸上**写下解析后 `contains / argidx / wave / show` 四个变量的值。
   - (a) `launch_tb -contains fifo`
   - (b) `launch_tb -contains fifo -wave`
   - (c) `launch_tb -contains fifo -wave view.do`
   - (d) `launch_tb -contains fifo -wave -show`
   - (e) `launch_tb -contains fifo -argidx 1 -wave -show`
2. 然后用一段最小 TCL 在任意 `tclsh` 里验证（不依赖 PsiSim，只验证解析逻辑）：

```tcl
# 示例代码：把 launch_tb 的 -wave 解析逻辑单独抽出来跑
proc parse_wave {args} {
    set wave ""; set show ""
    set argList [split $args]; set i 0
    while {$i < [llength $argList]} {
        set thisArg [lindex $argList $i]
        if {$thisArg == "-wave"} {
            set i [expr {$i + 1}]
            set thisArg [lindex $argList $i]
            if {$thisArg == ""}          { set wave "all"
            } elseif {$thisArg == "-show"} { set wave "all"; set show "enable"
            } else                        { set wave $thisArg }
        } elseif {$thisArg == "-show"} {
            set show "enable"
        }
        set i [expr {$i + 1}]
    }
    return [list wave $wave show $show]
}
puts [parse_wave -contains fifo -wave]          ;# 期望: wave all show ""
puts [parse_wave -contains fifo -wave -show]    ;# 期望: wave all show enable
puts [parse_wave -contains fifo -wave view.do]  ;# 期望: wave view.do show ""
```

**需要观察的现象**：第 (b) 条 `-wave` 在命令末尾时，`lindex` 越界返回空串，被识别为 `wave="all"`。

**预期结果**：

| 命令 | contains | argidx | wave | show |
| --- | --- | --- | --- | --- |
| (a) | fifo | default | "" | "" |
| (b) | fifo | default | all | "" |
| (c) | fifo | default | view.do | "" |
| (d) | fifo | default | all | enable |
| (e) | fifo | 1 | all | enable |

（真实在 Modelsim/GHDL 里的运行效果属于「待本地验证」，本表只保证解析阶段的取值。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `launch_tb` 把 `-contains` 设为必填，而 `run_tb` 把它设为可选（还提供 `-all`）？

**参考答案**：`run_tb` 面向回归，要能「一次跑全部」（`-all`）；`launch_tb` 面向单人调试，一次只启动一个 run，必须让用户**明确**指定调哪一个，否则容易误启动一堆 GUI，所以强制 `-contains`。

**练习 2**：用户执行 `launch_tb -contains fifo -argidx 5`，但该 run 只用 `create_tb_run` + `add_tb_run` 定义、没调 `tb_run_add_arguments`。会发生什么？

**参考答案**：该 run 的 `TB_ARGS` 是默认的 `[list ""]`，长度为 1。`argidx=5 >= 1` 命中越界分支，打印 `ERROR: launch_tb: -argidx out of range 0 ... 0` 并 `return`，不会启动仿真。

---

### 4.2 Modelsim 交互调试路径（sal_launch_tb）

#### 4.2.1 概念说明

Modelsim 是带 GUI 的常驻仿真器。它的交互调试模型是：

1. `vsim` 把设计加载进内存（**不**自动 `run`），仿真时间停在 0。
2. Modelsim 主窗口保持打开，人在里面手动敲 `run`、`restart`、`add wave`、看波形窗口。

所以 PsiSim 在 Modelsim 下专门写了 `sal_launch_tb`：它**不调** `sal_run_tb`（那是跑到结束的回归路径），而是直接构建一条 `vsim` 命令把设计加载进来，然后根据 `wave` 参数决定是否顺手帮你加信号、跑、缩放。

`launch_tb` 的 Modelsim 分支 [`PsiSim.tcl:L941-L944`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L941-L944)：

```tcl
if {"Modelsim" == $Simulator} {
    sal_launch_tb $runLib $runName $argsToUse $RunSuppress $wave
}
```

注意它**不**传 `timeLimit`，也不落盘波形文件——人都在 GUI 里了，要波形直接看窗口即可。

#### 4.2.2 核心流程

`sal_launch_tb` 的伪代码：

```
sal_launch_tb(lib, tbName, tbArgs, suppressMsgNo, wave):
    拼 vsim 命令:  vsim -quiet -t 1ps -msgmode both  [+nowarnXXX]  lib.tbName  tbArgs
    eval 这条命令                        # 加载设计，仿真时间停在 0
    关掉 std_logic/numeric_std 的告警     # StdArithNoWarnings=1, NumericStdNoWarnings=1
    if wave != "":
        if wave != "all":   do $wave              # 用户给的是 .do 文件 → 恢复波形视图
        else:                add wave -r /* ; run -all ; wave zoom full
                                            # 全信号模式：加全部信号 + 跑完 + 缩放全图
```

关键在 `wave` 的三种取值如何决定后续行为：

- `wave == ""`（用户没传 `-wave`）：`vsim` 加载后什么都不做，**仿真不跑**，完全交给用户手动控制。这是「最纯粹」的交互调试。
- `wave == "all"`（用户传了 `-wave` 但没给文件名）：自动 `add wave -r /*`（递归加全部信号）、`run -all`（跑到测试台自己停）、`wave zoom full`（缩放到能看到整段）。也就是「一键看完整体波形」。
- `wave == "<xxx.do>"`（用户给了 `.do` 文件）：执行 `do xxx.do`，恢复用户预先编排好的波形视图（比如只看某几个关键信号、分组、设断点颜色等）。

> 「启动而不执行」与「`-wave` 全信号模式会 `run -all`」看似矛盾，其实不冲突：默认（无 `-wave`）才是纯交互；`-wave` 是一个「顺手帮你跑到结束并铺好波形」的快捷方式，省去手动敲三条命令。

#### 4.2.3 源码精读

[`PsiSim.tcl:L309-L333`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L309-L333)：`sal_launch_tb` 是 SAL 里少数**只有 Modelsim 一个分支**的 proc——它的 `else` 直接报「Unsupported Simulator」，GHDL/Vivado 都不走这里（GHDL 走的是 4.3 的复用 `sal_run_tb` 路径）。

```tcl
proc sal_launch_tb {lib tbName tbArgs suppressMsgNo wave} {
    variable Simulator
    if {$Simulator == "Modelsim"} {
        set supp ""
        if {$suppressMsgNo != ""} {
            set supp +nowarn$suppressMsgNo            # 拼消息抑制（与 sal_run_tb 同款）
        }
        set cmd "vsim -quiet -t 1ps -msgmode both $supp $lib.$tbName $tbArgs"
        eval $cmd                                     # 加载设计，不 run
        set StdArithNoWarnings 1
        set NumericStdNoWarnings 1
        if {$wave != ""} {
            if {$wave != "all"} {
                sal_print_log "Restoring Waveform View $wave"
                set cmd "do $wave"                    # 恢复 .do 波形视图
            } else {
                sal_print_log "Adding all Signals to the Waveform View"
                set cmd "add wave -r /*; run -all; wave zoom full"   # 全信号快捷模式
            }
            eval $cmd
        }
    } else {
        puts "ERROR: Unsupported Simulator - sal_launch_tb(): $Simulator"
    }
}
```

三个细节：

- `vsim` 命令串里直接拼 `$tbArgs`，所以 `-argidx` 选中的那组泛型（如 `-gClockRatio_g=3`）会原样作为 vsim 的 generic 覆盖传入，与 `sal_run_tb` 的 Modelsim 分支一致（见 u3-l4）。
- `StdArithNoWarnings`/`NumericStdNoWarnings` 是 Modelsim 全局变量，关掉 `std_logic_1164` 和 `numeric_std` 的告警噪音，调试时眼根更清净。
- 注意对比 [`PsiSim.tcl:L224-L240`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L224-L240) 的 `sal_run_tb` Modelsim 分支：两者的 `vsim` 命令串**几乎一字不差**，区别只在 `sal_run_tb` 紧接着 `run $timeLimit`/`run -all` + `quit -sim`（加载即跑到结束并退出），而 `sal_launch_tb` 加载后留下一个活的设计给人交互。这是「回归」与「调试」在同一仿真器下的分水岭。

#### 4.2.4 代码实践

**实践目标**：理解 `wave` 三态如何改变 Modelsim 里的后续动作，学会为不同调试场景选对参数。

**操作步骤**：

1. 假设你已经在 Modelsim 里 `source` 了 PsiSim、`init`、`source config.tcl`、`compile_files -all`。
2. 对照下表，为每种调试意图挑出正确的 `launch_tb` 调用：

| 调试意图 | 应该用哪条命令？ |
| --- | --- |
| 只加载设计，自己手动 `run`/设断点 | `launch_tb -contains fifo` |
| 想立刻看到所有信号的整体波形 | `launch_tb -contains fifo -wave` |
| 用预先编好的 `view.do` 恢复一套波形视图 | `launch_tb -contains fifo -wave view.do` |
| 调第 2 组泛型（ClockRatio=1.01）下的行为 | `launch_tb -contains fifo -argidx 1 -wave` |

3. 阅读 [`PsiSim.tcl:L320-L329`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L320-L329)，确认「`wave==""` 时 `if` 块整体跳过、不跑 `run`」这一行为。

**需要观察的现象**：无 `-wave` 时 Modelsim 加载设计后仿真时间停在 0，主窗口等待你输入；带 `-wave`（无文件名）时会看到信号被自动加入波形窗口并跑到结束。

**预期结果**：上表四条命令对应的行为与 4.2.2 的三态一致。具体 Modelsim 版本上的窗口表现属于「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sal_launch_tb` 的 `else` 分支报「Unsupported Simulator」而不是像 `sal_run_tb` 那样支持 GHDL/Vivado？

**参考答案**：因为 GHDL 和 Vivado 没有「加载设计但停在 0 等人交互」的能力——GHDL 是批处理式的命令行仿真器，Vivado xsim 虽有 GUI 但 PsiSim 未接。GHDL 的「调试」是靠「跑到结束 + 落盘波形 + GTKWave 看」这套替代方案实现的（见 4.3），所以 `sal_launch_tb` 只服务 Modelsim。

**练习 2**：`launch_tb -contains fifo -wave` 在 Modelsim 下会执行 `run -all`，这是否与「launch_tb 启动而不执行」的说法矛盾？

**参考答案**：不矛盾。「启动而不执行」指的是**默认行为**（不传 `-wave` 时加载后不跑）。`-wave`（不带文件名）是一个明确的快捷指令，相当于用户主动要求「加载 + 加全部信号 + 跑完 + 缩放」，三条命令合并成一次调用，省事而已。

---

### 4.3 GHDL 波形路径与 GTKWave 迭代工作流（sal_open_wave）

#### 4.3.1 概念说明

GHDL 没有可交互的仿真窗口，所以 PsiSim 在 GHDL 下走了**完全不同**的调试路径：

1. **复用 `sal_run_tb`** 把测试台跑到结束（或到 `TIME_LIMIT`），但通过第六参 `wave` 让 GHDL 顺手把波形落盘成 `.ghw` 文件。
2. **用 `sal_open_wave` 调 GTKWave** 后台打开这个 `.ghw`，人看波形。

所以 GHDL 的「launch」本质是「run + 落波形 + 看波形」，而不是 Modelsim 那种「加载等人交互」。这带来一个后果：**改一行代码后想看新波形，必须重跑仿真**（GHDL 没法像 Modelsim 那样 `restart` 后增量继续）。为了不让这个迭代太痛苦，PsiSim 设计了一套「文件名稳定 + GTKWave Reload」的循环工作流。

> 文档与代码的一个**已知不一致**：CommandRef 的 `-wave` 参数说明 [`CommandRef.md:L528`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L528) 写的是生成 `.vcd` 文件，但实际代码 [`PsiSim.tcl:L948`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L948) 生成的是 `.ghw` 文件。以代码为准。`.ghw` 是 GHDL 的原生波形格式（GTKWave 直接支持），相比 VCD 的优势是能正确表达 VHDL 的复合类型（record 等）。

#### 4.3.2 核心流程

GHDL 分支 [`PsiSim.tcl:L945-L956`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L945-L956) 的伪代码：

```
if Simulator == "GHDL":
    timeLimit = run.TIME_LIMIT
    if wave != "":                     # 只有用户传了 -wave 才落波形
        wave = "<runName>_<argidx>.ghw"     # 文件名 = 测试台名_参数序号.ghw
        打印 "Writing Waveform: <wave>"
    sal_run_tb(lib, name, argsToUse, timeLimit, RunSuppress, wave)   # 第六参传 wave
    if show == "enable":               # 用户传了 -show
        sal_open_wave(wave)            # gtkwave -f <wave> &  （后台）
```

注意 `wave` 这个局部变量的**复用**：它进 GHDL 分支时是 `"all"`（来自 4.1 的解析），但这里被**重新赋值**成真正的文件名 `<runName>_<argidx>.ghw`，再传给 `sal_run_tb`。也就是说，`"all"` 这个值在 GHDL 路径里只起到「用户想要波形」的标记作用，真正的文件名是这里算出来的。

`sal_run_tb` 的 GHDL 分支怎么消费第六参 `wave`？见 [`PsiSim.tcl:L251-L254`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L251-L254)：

```tcl
if {$wave != ""} {
    set wave " --wave=$wave"            # 拼成 ghdl 的 --wave=file.ghw 开关
}
set cmd "ghdl --elab-run ... $tbName$tbArgs$stopTime$wave --ieee-asserts=disable"
```

即 `wave` 非空时，最终命令里会带上 `--wave=<runName>_<argidx>.ghw`，GHDL 跑完就把波形写进这个文件。

**GTKWave 迭代工作流**（来自 [`CommandRef.md:L537-L542`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L537-L542)）：

```
1. 第一次跑：launch_tb -contains fifo -wave -show
   → 跑仿真、落盘 fifo_1.ghw、GTKWave 后台打开 fifo_1.ghw
2. 改代码 / 改泛型后，重跑：launch_tb -contains fifo -wave      （注意：不带 -show）
   → 重新跑仿真、覆盖同一个 fifo_1.ghw
3. 在已打开的 GTKWave 里点：File → Reload Waveform
   → GTKWave 重新读 fifo_1.ghw，看到新波形
4. （可选）换 -argidx 对比不同泛型下的波形
```

这套流程能成立的**唯一原因**是文件名稳定：只要 `-argidx` 不变，每次都覆盖同一个 `<name>_<argidx>.ghw`，GTKWave 的 Reload 才能读到最新内容。这也是为什么 GHDL 分支用 `<argidx>` 而不是随机名/时间戳来命名波形文件。

#### 4.3.3 源码精读

**(1) `sal_open_wave`——后台 fork GTKWave**

[`PsiSim.tcl:L335-L342`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L335-L342)：

```tcl
proc sal_open_wave {wave} {
    variable Simulator
    if {$Simulator == "GHDL"} {
        exec gtkwave -f $wave &        # 关键：结尾的 & 把 GTKWave 放后台
    } else {
        puts "ERROR: Unsupported Simulator - sal_open_wave(): $Simulator"
    }
}
```

两个要点：

- 命令结尾的 `&` 让 `exec` **不阻塞**——GTKWave 窗口打开后，控制权立刻回到 `tclsh`，你可以继续敲命令重跑。这正是上面工作流第 2 步「重跑不带 `-show`」的前提：第一次已经把 GTKWave 开着了，后续只要 Reload 即可。
- `-f` 让 GTKWave 把参数当作波形文件路径读入。

**(2) GHDL 分支如何决定文件名 + 调 run_tb**

[`PsiSim.tcl:L945-L956`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L945-L956)：

```tcl
if {"GHDL" == $Simulator} {
    set timeLimit [dict get $run TIME_LIMIT]
    if {$wave != ""} {
        set wave "$runName\_$argidx\.ghw"      # 文件名：测试台名_参数序号.ghw
        sal_print_log "Writing Waveform: $wave"
    }
    sal_run_tb $runLib $runName $argsToUse $timeLimit $RunSuppress $wave
    if {$show == "enable"} {
        sal_open_wave $wave
    }
}
```

注意 `$argidx` 在「用户没传 `-argidx`」时是字符串 `"default"`（来自 4.1 的初值），所以文件名会变成 `<runName>_default.ghw`——这与 CommandRef 里 `<argidx|default>` 的写法对应。

**(3) 与 Modelsim 分支的对照**

| 维度 | Modelsim（4.2） | GHDL（4.3） |
| --- | --- | --- |
| 调用的 SAL proc | `sal_launch_tb`（专用，只加载不跑） | `sal_run_tb`（复用，跑到结束） |
| 是否落盘波形文件 | 否（看 GUI 窗口） | 是（`<name>_<argidx>.ghw`） |
| 看波形的方式 | Modelsim 自带波形窗口 | 外部 GTKWave，`sal_open_wave` 后台打开 |
| 改代码后看新波形 | `restart` 后增量继续 | 必须重跑 + GTKWave Reload |
| `timeLimit` | 不传（交互控制） | 传（决定跑到何时停） |
| `-show` 参数 | 被忽略 | 控制 是否调 `sal_open_wave` |

#### 4.3.4 代码实践

**实践目标**：把 CommandRef 的 GHDL/GTK 工作流与源码逐条对上号，确认「文件名稳定」是这套流程的支点。

**操作步骤**：

1. 阅读 [`CommandRef.md:L537-L542`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L537-L542) 的 5 步工作流。
2. 对照源码，为每一步找出对应的代码位置：
   - 「第一次用 `-wave` 和 `-show`」→ `launch_tb` 解析出 `wave="all"`、`show="enable"`（L878-L888）→ GHDL 分支算出文件名（L947-L949）→ `sal_run_tb` 落盘（L251-L253）→ `sal_open_wave` 后台开 GTKWave（L953-L955、L338）。
   - 「重跑不带 `-show`」→ `show` 仍是 `""`，所以 `sal_open_wave` **不**被调用（L953 的 `if` 不成立），但 `wave` 仍非空，仍会覆盖同一个 `.ghw`。
   - 「File → Reload Waveform」→ GTKWave 自己重读同名文件，PsiSim 代码里没有任何对应动作（纯用户操作）。
3. 回答：如果第二次重跑时改用了 `-argidx 2`，会发生什么？

**需要观察的现象**：GTKWave 第一次打开的是 `<name>_1.ghw`；重跑（同 `-argidx 1`、不带 `-show`）后磁盘上 `<name>_1.ghw` 被覆盖，Reload 能看到新波形。

**预期结果**：第 3 步——若改用 `-argidx 2`，文件名变成 `<name>_2.ghw`，**与 GTKWave 当前打开的不是同一个文件**，Reload 看不到新内容；GTKWave 里需要手动重新打开 `<name>_2.ghw`，或者再带一次 `-show` 让 PsiSim 开新窗口。这正好对应 CommandRef 第 5 步「Optional: Run with different -argidx to compare different waveforms」——对比不同泛型时你会得到多个不同文件名的 `.ghw`，可以同时打开多个 GTKWave 窗口对比。

（实际 GTKWave 行为属于「待本地验证」，本实践只保证源码层面的因果链。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `sal_open_wave` 里 `exec gtkwave -f $wave &` 末尾要加 `&`？去掉会怎样？

**参考答案**：加 `&` 是让 `exec` 后台运行 GTKWave、不阻塞 `tclsh`。去掉后 `exec` 会一直等待 GTKWave 进程退出（即等用户关掉波形窗口）才返回，期间 `tclsh` 完全卡住、没法敲重跑命令——GTKWave 迭代工作流的第 2 步就无从谈起。

**练习 2**：CommandRef 说 GHDL 生成 `.vcd`，代码却生成 `.ghw`，两者主要区别是什么？PsiSim 选 `.ghw` 有什么好处？

**参考答案**：`.vcd`（Value Change Dump）是通用格式但不能很好表达 VHDL 的复合类型（如 record）；`.ghw` 是 GHDL 原生格式，GTKWave 直接支持，能正确显示 record 等类型。PsiSim 选 `.ghw` 是为了让 VHDL 测试台里常用的 record 类型信号在波形里可读。应以代码（L948）为准，CommandRef 的 `.vcd` 表述是过时文档。

---

## 5. 综合实践

**任务**：完整跟踪 `launch_tb -contains fifo -argidx 1 -wave -show` 这条命令，分别在 Modelsim 和 GHDL 下的全过程，并复述 CommandRef 的 GHDL/GTK 迭代调试步骤。

**前提**：`config.tcl` 里有这样一个 run（参考 README 示例风格）：

```tcl
create_tb_run "psi_common_sync_fifo_tb"
tb_run_add_arguments \
    "-gAlmFullOn_g=true -gAlmEmptyOn_g=true -gDepth_g=32" \
    "-gAlmFullOn_g=false -gAlmEmptyOn_g=false -gDepth_g=128"
add_tb_run
```

即 `TB_ARGS` 是长度为 2 的列表（`-argidx 1` 合法）。注意 `fifo` 是 `psi_common_sync_fifo_tb` 的子串，能被 `-contains` 命中。

**操作步骤**：

1. **解析阶段**（对照 L862-L896）：在纸上写出四个变量的最终值。
   - `contains = "fifo"`，`argidx = "1"`，`wave = "all"`，`show = "enable"`。
2. **选 run 阶段**（对照 L904-L937）：`string first "fifo" "psi_common_sync_fifo_tb" != -1`，命中；假设未被 skip；`argListLength = 2`，`argidx=1 < 2`，故 `argsToUse = "-gAlmFullOn_g=false -gAlmEmptyOn_g=false -gDepth_g=128"`（第二组泛型）。
3. **Modelsim 路径**（对照 L941-L944 → L309-L333）：
   - 调 `sal_launch_tb(... "-gAlmFullOn_g=false ..." RunSuppress "all")`。
   - 执行 `vsim -quiet -t 1ps -msgmode both ... psi_common.sync_fifo_tb -gAlmFullOn_g=false -gAlmEmptyOn_g=false -gDepth_g=128`，加载设计。
   - `wave=="all"` → 执行 `add wave -r /*; run -all; wave zoom full`。
   - **结果**：Modelsim 里所有信号被加入波形、跑到测试台自停、缩放到全图，窗口保持打开。
4. **GHDL 路径**（对照 L945-L956 → L224-L307 → L335-L342）：
   - `wave != ""` → `wave = "psi_common_sync_fifo_tb_1.ghw"`，打印 `Writing Waveform: psi_common_sync_fifo_tb_1.ghw`。
   - 调 `sal_run_tb(... "-gAlmFullOn_g=false ..." timeLimit RunSuppress "psi_common_sync_fifo_tb_1.ghw")`，GHDL 命令带上 `--wave=psi_common_sync_fifo_tb_1.ghw`，跑到结束、落盘。
   - `show=="enable"` → `sal_open_wave "psi_common_sync_fifo_tb_1.ghw"` → `exec gtkwave -f psi_common_sync_fifo_tb_1.ghw &`，后台开 GTKWave。
   - **结果**：tclsh 立刻回到提示符，GTKWave 窗口显示该波形。
5. **复述 GHDL/GTK 迭代调试**（对照 CommandRef L537-L542）：
   - 第一次 `launch_tb -contains fifo -wave -show` → 落盘 `..._1.ghw` 并开 GTKWave。
   - 改代码后在 tclsh 里 `launch_tb -contains fifo -wave`（**不带** `-show`）→ 覆盖同一个 `..._1.ghw`。
   - 在 GTKWave 里 `File → Reload Waveform` → 看到新波形。
   - 可选：换 `-argidx`（如 `-argidx 0`）对比另一组泛型，得到 `..._0.ghw`，另开窗口对比。

**预期结果**：你能画出两条路径各自的调用链，并解释为什么 Modelsim 不需要「Reload」而 GHDL 需要。真实仿真器输出属于「待本地验证」。

---

## 6. 本讲小结

- `launch_tb` 是 PsiSim 的**交互调试**入口，与回归用的 `run_tb` 有三处刻意不同：启动而不执行（默认）、不跑前后脚本、只启动第一个 `-contains` 命中的 run。
- 参数 `-contains`（必填）/`-argidx`/`-wave`/`-show` 由 L862-L896 的朴素 `while` 解析；其中 `-wave` 的三态（空→`all`、`-show`→`all+enable`、文件名）是 Changelog 2.4.0 修 bug 的产物。
- `-argidx` 越界会被 L931-L934 拦下；`TB_ARGS` 默认长度为 1，故未定义多组泛型时 `-argidx 1` 会报 `out of range 0 ... 0`。
- **Modelsim 走 `sal_launch_tb`**（L309-L333）：`vsim` 加载设计、可选 `add wave -r /*; run -all; wave zoom full` 或 `do xxx.do`，窗口常驻、真正可交互。
- **GHDL 复用 `sal_run_tb`**（L945-L956 → L224-L307）：跑到结束并把波形落成 `<name>_<argidx>.ghw`（代码实际是 `.ghw`，CommandRef 写的 `.vcd` 是过时文档），再用 `sal_open_wave`（L335-L342）后台 `gtkwave -f` 打开。
- GTKWave 迭代工作流的支点是**文件名稳定**：同 `-argidx` 反复覆盖同一 `.ghw`，配合 GTKWave `File → Reload Waveform` 即可不重开窗口地刷新波形。
- Vivado 被 `launch_tb` 显式拒绝（L854-L859），没有调试路径。

---

## 7. 下一步学习建议

- **向左回顾**：若你对 `sal_run_tb` 的 GHDL/Vivado 分支细节还不熟，回到 **u3-l4** 把第六参 `wave` 之外的部分（`--elab-run`、Vivado 的 initfile workaround、generic 转换）补齐——本讲的 GHDL 调试路径完全建立在它之上。
- **向下进入单元 4**：
  - **u4-l2（GHDL/GTKWave 工作流深度实践）** 会把本讲的迭代调试放到一个完整可跑的 GHDL 工程里，并解释为什么用 `.ghw` 而非 `.vcd`、GHDL 双版本编译与库产物子目录如何影响波形落盘。
  - **u4-l3（扩展新模拟器与架构取舍）** 会让你思考：如果要给 Vivado 也加上调试路径，需要改 `launch_tb` 的门禁（L854-L859）、新增一个 `sal_launch_tb` 的 Vivado 分支，并评估当前「全局可变状态 + `eval` 拼命令」带来的风险。
- **源码延伸阅读**：把 `launch_tb`（L852-L964）与 `run_tb`（L752-L838）并排读一遍，对比两者在「过滤、skip、脚本、单/多 run、是否传 wave」上的差异，是理解 PsiSim「回归 vs 调试」两条主线设计的最佳练习。

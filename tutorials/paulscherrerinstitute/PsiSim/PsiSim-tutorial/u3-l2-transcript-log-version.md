# transcript、日志与版本处理

## 1. 本讲目标

本讲是单元 3 的第二篇，承接 u3-l1 搭好的 SAL 骨架，开始往 `sal_*` proc 的**分支内部**走。本篇挑的是 SAL 里最「工具型」的一组过程——**日志、transcript（仿真日志副本）与版本处理**。它们之所以先讲，是因为后面所有的编译、运行、调试 proc（u3-l3、u3-l4、u3-l5）都要靠它们来打印日志、靠 transcript 来判错。

读完本讲你应该能做到：

1. 说清楚 PsiSim 如何用一个统一的 `./Transcript.transcript` 文件，把 **Modelsim 自动记录**和 **GHDL/Vivado 手动追加**两种截然不同的日志来源收拢成同一条「事后可 grep 判错」的链路，并理解 `sal_print_log` 对 GHDL/Vivado 的「双写」（控制台 + 文件）做法。
2. 掌握 `sal_init_simulator` 探测 Modelsim 版本时用的「**文件中转**」技巧——为什么不能直接拿 `vcom -version` 的返回值，而要先重定向到临时文件、`after` 等待、再正则提取。
3. 理解 `sal_version_specific_flags` 如何按版本号返回 `-novopt` 编译开关，以及这个开关在哪里被消费。

本讲**只读一个源码文件** [`PsiSim.tcl`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl)，聚焦其中 7 个 proc：`sal_print_log`、`sal_transcript_off`、`sal_transcript_on`、`sal_set_transcript_file`、`sal_clean_transcript`、`sal_version_specific_flags`、`sal_init_simulator`。

## 2. 前置知识

本讲默认你已学完 u3-l1（SAL 骨架与 dispatch 模式）和 u2-l7（错误检查与 transcript）。这里做最简回顾：

- **dispatch 模式**：每个 `sal_*` proc 开头 `variable Simulator`，紧跟 `if/elseif` 链按 `Simulator` 字符串值分派到 Modelsim / GHDL / Vivado 三套实现，末尾挂一句非阻断的 `ERROR: Unsupported Simulator`。详见 [PsiSim.tcl:28-46](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L28-L46)。
- **transcript 与判错**：PsiSim 不向仿真器「问成败」，而是把所有输出汇集到一个磁盘纯文本文件 `./Transcript.transcript`，由 `run_check_errors` 用正则 grep 它来判定回归通过与否。详见 [PsiSim.tcl:721-740](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L721-L740)（u2-l7）。
- **运行环境差异**：Modelsim 在**自带的 TCL shell** 里跑；GHDL/Vivado 在**独立 TCL 解释器**（如 ActiveTCL 的 `tclsh`）里跑。这条差异是本讲所有「Modelsim 走自己的 transcript 命令、GHDL/Vivado 走手动文件追加」分叉的根因。
- **两个版本相关变量**：`Simulator`（当前用哪个仿真器）与 `SimulatorVersion`（该仿真器的版本号），分别声明于 [PsiSim.tcl:24](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L24) 与 [PsiSim.tcl:25](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L25)。

> 一个直觉铺垫：Modelsim 自带「transcript」概念——它有一条 `transcript` 命令，能把控制台里发生的一切自动记到文件里；而 GHDL、Vivado 跑在普通 `tclsh` 里，**根本没有 transcript 这种东西**，输出只往 stdout 流。PsiSim 要让三种仿真器产出**同一种格式**的日志文件供 `run_check_errors` 读取，就必须在 SAL 这一层「抹平」这个根本差异——这正是本讲要讲的事。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注范围 |
| --- | --- | --- |
| [PsiSim.tcl](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl) | PsiSim 唯一源码 | 7 个日志/版本相关 `sal_*` proc，以及 `init`、`run_tb`、`run_check_errors`、`compile` 中对它们的调用点 |

本讲涉及的定位点（全部在 `PsiSim.tcl` 内）：

| proc / 位置 | 行号 | 一句话职责 |
| --- | --- | --- |
| `sal_print_log` | [L31-L46](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L31-L46) | 统一日志输出：Modelsim 走 `echo`，GHDL/Vivado 走「控制台 + 追加文件」双写 |
| `sal_transcript_off` | [L48-L57](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L48-L57) | 关闭日志记录（仅 Modelsim 有实质动作） |
| `sal_transcript_on` | [L59-L68](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L59-L68) | 开启日志记录（仅 Modelsim 有实质动作） |
| `sal_set_transcript_file` | [L70-L80](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L70-L80) | 指定 transcript 文件；**对三种仿真器都**写入 `TranscriptFile` 变量 |
| `sal_clean_transcript` | [L82-L101](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L82-L101) | 清空并重建 `./Transcript.transcript`（含 batch_mode 分叉） |
| `sal_version_specific_flags` | [L104-L119](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L104-L119) | 按版本返回编译开关（Modelsim < 10.7 返回 `-novopt`） |
| `sal_init_simulator` | [L121-L147](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L121-L147) | 探测 Modelsim 版本号（文件中转 + 正则），写入 `SimulatorVersion` |

调用点（本讲会反复回到这几处接口函数）：

- `init` 依次调用 `sal_init_simulator`（[L375](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L375)）与 `clean_transcript`（[L377](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L377)）。
- `run_tb` 在开头调 `clean_transcript`（[L784](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L784)）、结尾调 `sal_transcript_off`（[L837](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L837)）。
- `run_check_errors` 开头调 `sal_transcript_off`（[L723](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L723)），再读取 `./Transcript.transcript`。
- `compile`（内部 proc）把 `sal_version_specific_flags()` 的返回值拼进编译参数（[L165-L167](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L165-L167)）。

一句话地图：**本讲讲的是 SAL 的「水电管道」——日志往哪儿写、transcript 怎么清、版本号怎么探、版本开关怎么用。**

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 日志与 transcript 抽象**、**4.2 Modelsim 版本探测**、**4.3 版本相关 flag**。

### 4.1 日志与 transcript 抽象

#### 4.1.1 概念说明

PsiSim 的回归判定（u2-l7 讲过的 `run_check_errors`）依赖一个前提：**整个仿真过程中所有重要输出，最终都汇落到同一个磁盘文本文件 `./Transcript.transcript`**。这样 `run_check_errors` 只要 `open` 这个文件、`regexp` 一下，就能判定通过与否。

问题在于：三种仿真器「把输出落到文件」的机制**完全不同**。

| 仿真器 | 运行环境 | 谁负责把输出记进文件 |
| --- | --- | --- |
| Modelsim | 自带 TCL shell | **Modelsim 自己**——它有一条 `transcript` 命令，控制台里 `vcom`/`vsim` 等命令的输出会自动被记录到 transcript 文件 |
| GHDL | 独立 `tclsh` | **没有人**——`tclsh` 只往 stdout 流，必须 PsiSim **手动**把要保留的文字追加写进文件 |
| Vivado | 独立 `tclsh`（或 Vivado shell） | 同 GHDL，需 PsiSim 手动追加 |

这就带来两条截然不同的实现路线：

- **Modelsim 路线**：PsiSim 不自己写文件，而是「**搭便车**」——用 Modelsim 的 `transcript off / on / file` 三件套来控制 Modelsim 自带的记录行为；PsiSim 自己想往 transcript 里塞的横幅、提示，则用 Modelsim 的 `echo` 命令（`echo` 会同时打到控制台和 transcript 文件）。
- **GHDL/Vivado 路线**：没有便车可搭，PsiSim 必须「**双写**」——一次 `puts` 到控制台给人看，一次 `open ... a`（追加）+ `puts` 到 `TranscriptFile` 给 `run_check_errors` 读。

把这两条路线统一封装起来的，就是 [`sal_print_log`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L31-L46)。它是整个 SAL 里**被调用最频繁**的 proc——编译横幅、运行横幅、警告、命令串、仿真输出，几乎都经它流出。

> 一个关键认识：对 GHDL/Vivado 而言，仿真器本身的输出（比如 `ghdl -a` 的编译信息）也不会自动进 transcript。它们在 `sal_run_tb` / `sal_exec_script` 里是用 `exec` 捕获后再交给 `sal_print_log` 二次写出的（如 [PsiSim.tcl:256-257](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L256-L257)）。这部分细节属于 u3-l3/u3-l4，本讲只需记住「GHDL/Vivado 一切日志都要手动追加」。

#### 4.1.2 核心流程

先看 `sal_print_log` 的分派逻辑（伪代码）：

```
sal_print_log(text):
  读 Simulator
  if Modelsim:
      echo text            # Modelsim 的 echo：控制台 + transcript 都写
  elseif GHDL 或 Vivado:
      puts text            # 控制台
      打开 TranscriptFile（追加模式）
      puts 文件 text       # transcript 文件
      关闭文件
  else:
      puts "Unsupported Simulator ..."
```

再看 transcript 文件在整个回归生命周期里的「清—写—读」节奏：

```
init()
  └─ sal_init_simulator()      # 探测版本（见 4.2）
  └─ clean_transcript()        # ① 首次清空 ./Transcript.transcript
        └─ sal_clean_transcript()
              └─ sal_set_transcript_file ./Transcript.transcript
                    └─ 同时把 TranscriptFile 变量指向它

compile_files -all             # 编译期：sal_print_log 追加编译横幅（GHDL/Vivado）

run_tb -all
  ├─ clean_transcript()        # ② 再次清空，丢掉编译期日志，只留运行期
  ├─ foreach run:
  │     ├─ sal_print_log 横幅
  │     ├─ sal_run_tb(...)     # 仿真器输出经 sal_print_log 落盘（GHDL/Vivado）
  │     └─ ...
  └─ sal_transcript_off()      # ③ 关闭记录（Modelsim 释放文件句柄）

run_check_errors "###ERROR###"
  ├─ sal_transcript_off()      # 防御性再关一次
  ├─ open ./Transcript.transcript; read; close   # ④ 读取整份日志
  └─ regexp 判错               # 见 u2-l7
```

要点：

1. **`./Transcript.transcript` 被清空两次**：`init` 里清一次（[L377](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L377)），`run_tb` 开头再清一次（[L784](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L784)）。第二次清空是有意为之——它把**编译期的日志丢掉**，让 `run_check_errors` 读到的文件只含**运行期**内容，避免编译告警干扰判错。
2. **`sal_transcript_off` 出现两次**：`run_tb` 结尾（[L837](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L837)）和 `run_check_errors` 开头（[L723](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L723)）。对 Modelsim 这意味着「别再往 transcript 里记了」，确保随后 `run_check_errors` 自己 `open` 文件读时，文件内容是稳定的。
3. **`TranscriptFile` 变量是 GHDL/Vivado 双写的「目标地址」**，由 `sal_set_transcript_file` 设定，存在命名空间变量里供 `sal_print_log` 读取。

#### 4.1.3 源码精读

**（a）`sal_print_log`——双写的核心**

[PsiSim.tcl:31-46](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L31-L46)：统一日志输出，Modelsim 用 `echo`，GHDL/Vivado 控制台与文件各写一遍。

```tcl
proc sal_print_log {text} {
    variable Simulator
    variable TranscriptFile
    if {$Simulator == "Modelsim"} {
        echo $text                       ; # Modelsim 内建命令，控制台+transcript 都写
    } elseif {($Simulator == "GHDL") || ($Simulator == "Vivado")} {
        puts $text                       ; # 控制台
        set fo [open $TranscriptFile a]  ; # 追加打开 transcript 文件
        puts $fo $text                   ; # 写文件
        close $fo                        ; # 立即关闭（每条日志独立 open/close）
    } else {
        puts "ERROR: Unsupported Simulator - sal_print_log(): $Simulator"
    }
}
```

三处值得注意：

- `echo` 是 **Modelsim 自带命令**（不是标准 TCL 的），所以这段代码只有在 Modelsim 的 TCL shell 里才能跑——这正好对应「Modelsim 必须在自带 shell 里运行」的约束。
- GHDL/Vivado 分支**每写一条日志就 open/close 一次文件**。这看起来低效，但好处是**无须自己维护文件句柄**，也不会因为脚本中途异常而留下未关闭的句柄——是一种「用性能换健壮」的取舍。
- 两条分支都把文字送到控制台，区别只在「文件谁来写」：Modelsim 由仿真器代劳，GHDL/Vivado 自己动手。

**（b）`sal_transcript_off` / `sal_transcript_on`——只对 Modelsim 有意义**

[PsiSim.tcl:48-68](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L48-L68)：开关 transcript 记录，GHDL/Vivado 分支是空操作。

```tcl
proc sal_transcript_off {} {
    variable Simulator
    if {$Simulator == "Modelsim"} {
        transcript off                   ; # 关闭 Modelsim 自带的 transcript 记录
    } elseif {($Simulator == "GHDL") || ($Simulator == "Vivado")} {
        #Nothing to do                  ; # GHDL/Vivado 根本没有 transcript 概念
    } else { puts "ERROR: Unsupported Simulator ..." }
}
```

`sal_transcript_on`（[L59-L68](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L59-L68)）结构完全对称，把 `transcript off` 换成 `transcript on`。这两个 proc 是 u3-l1 讲过的「**GHDL 与 Vivado 合并分支、且为空操作**」形态的典型例子。

**（c）`sal_set_transcript_file`——一个跨分支的赋值**

[PsiSim.tcl:70-80](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L70-L80)：设定 transcript 文件名，且**对三种仿真器都更新 `TranscriptFile` 变量**。

```tcl
proc sal_set_transcript_file {filename} {
    variable Simulator
    if {$Simulator == "Modelsim"} {
        transcript file $filename        ; # 告诉 Modelsim 把 transcript 写到哪
    } elseif {($Simulator == "GHDL") || ($Simulator == "Vivado")} {
        #Nothing to do
    } else { puts "ERROR: Unsupported Simulator ..." }
    variable TranscriptFile [file normalize $filename]   ; # 注意：这条在 if/elseif 之外！
}
```

关键细节：**最后一行 `variable TranscriptFile [file normalize $filename]` 位于 `if/elseif/else` 之外**，意味着无论哪个仿真器分支（含 `else`），它都会执行。这正是 GHDL/Vivado 的 `sal_print_log` 能拿到正确文件路径的来源——虽然它们的 `if` 分支是空操作，但这条「跨分支赋值」仍然把 `TranscriptFile` 指向了 `./Transcript.transcript`。`file normalize` 把相对路径 `./Transcript.transcript` 规整成绝对路径，避免后续 `open` 时的工作目录歧义。

> `TranscriptFile` 这个命名空间变量声明于 [PsiSim.tcl:26](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L26)。注意 `init` 的显式重置块（[L368-L373](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L368-L373)）里**并没有** `TranscriptFile`——它不是被 `init` 直接清零，而是经由 `init` → `clean_transcript` → `sal_clean_transcript` → `sal_set_transcript_file` 这条链路间接赋初值。

**（d）`sal_clean_transcript`——最复杂的一个，含 batch_mode 分叉**

[PsiSim.tcl:82-101](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L82-L101)：清空并重建 transcript 文件。

```tcl
proc sal_clean_transcript {} {
    variable Simulator
    sal_transcript_off                                ; # 先关记录（Modelsim 释放文件）
    if {$Simulator == "Modelsim"} {
        sal_set_transcript_file ./Dummy.transcript    ; # ① 把 Modelsim 的 transcript 引到临时空壳文件
        set bm [batch_mode]                           ; # Modelsim 内建命令：批量模式返回 1
        if {$bm == 0} {
            file delete ./Transcript.transcript       ; # ② 仅交互模式下显式删旧文件
        }
        sal_set_transcript_file ./Transcript.transcript ; # ③ 把 transcript 引回正式文件（重建空文件）
        file delete ./Dummy.transcript                ; # ④ 清理临时空壳
    } elseif {($Simulator == "GHDL") || ($Simulator == "Vivado")} {
        file delete ./Transcript.transcript           ; # 直接删旧文件
        sal_set_transcript_file ./Transcript.transcript ; # 仅设 TranscriptFile 变量
        return                                        ; # 提前返回，跳过末尾的 sal_transcript_on
    } else {
        puts "ERROR: Unsupported Simulator - sal_clean_transcript(): $Simulator"
    }
    sal_transcript_on                                 ; # 仅 Modelsim 分支会走到这里
}
```

逐步拆解 Modelsim 分支的「Dummy 中转」手法：

1. 先 `sal_transcript_off` 让 Modelsim 停止记录。
2. `sal_set_transcript_file ./Dummy.transcript`：把 Modelsim 的 transcript 输出**临时引到一个空壳文件 `Dummy.transcript`**。这一步的目的是**让 Modelsim 松开对 `./Transcript.transcript` 的占用**——只有先把它引开，后面才能删掉旧文件。
3. `set bm [batch_mode]`：`batch_mode` 是 Modelsim 内建命令，批量模式（如命令行 `vsim -c`）返回非 0，交互/GUI 模式返回 0。
4. `if {$bm == 0}`：**只在交互模式下**才 `file delete ./Transcript.transcript`。批量模式下跳过这次删除——这是本 proc 行为最微妙的地方，详见 4.1.4 的实践。
5. `sal_set_transcript_file ./Transcript.transcript`：把 transcript 引回正式文件，Modelsim 此时会**新建一个空的 `./Transcript.transcript`**，达到「清空」效果。
6. `file delete ./Dummy.transcript`：删掉临时空壳，不留痕迹。
7. 末尾 `sal_transcript_on`：重新打开记录。注意 GHDL/Vivado 分支用了 `return` 提前返回，**不会**走到这一行——因为它们没有「记录开关」可开。

GHDL/Vivado 分支则简单得多：删旧文件 + 设变量，完事。

> 顺带一个 TCL 小习惯：源码里多处出现 `set x [open ...]; list`、`lappend TbRuns $ThisTbRun; list` 这种「`; list`」结尾（如 [PsiSim.tcl:133-134](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L133-L134)）。`; list` 会求值一个空 `list`（返回空串），作用是**吞掉前一条命令的返回值**，避免它被当作 proc 的返回值——纯粹是代码整洁性的写法，对逻辑无影响。

#### 4.1.4 代码实践

**实践目标**：搞清楚 `sal_clean_transcript` 在 batch mode（批量模式）与交互模式下行为为何不同，并验证 transcript 的「清—写—读」链路。

**操作步骤**：

1. 打开 [PsiSim.tcl:82-101](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L82-L101)，定位 `set bm [batch_mode]` 与 `if {$bm == 0}` 这两行。
2. 在脑中分两种情形跟踪 Modelsim 分支：
   - **交互模式**（`bm == 0`）：会执行 `file delete ./Transcript.transcript`（②），随后 `transcript file ./Transcript.transcript`（③）让 Modelsim 新建一个空文件。
   - **批量模式**（`bm != 0`）：**跳过** ②，直接到 ③。
3. 思考：批量模式下没有显式删文件，为什么 `./Transcript.transcript` 仍能被「清空」？最合理的解释是——Modelsim 在批量模式下，当执行 `transcript file <同名文件>`（步骤 ③）时，会**自行截断/重建**该文件，因此无需 PsiSim 再手动 `file delete`；而在交互模式下，Modelsim 对已打开的同名文件**不会自动截断**（文件句柄仍占用旧内容），所以必须先引到 Dummy、再手动删、再引回。这正解释了「为什么 batch mode 下行为不同」。
4. 对照 GHDL/Vivado 分支：它们直接 `file delete` 后由 `sal_set_transcript_file` 仅更新变量（Modelsim 的 `transcript file` 命令对它们是空操作），随后 `return` 跳过 `sal_transcript_on`。

**需要观察的现象**：`batch_mode` 这一行只在 Modelsim 分支内出现——因为它是 Modelsim 专有命令，在独立 `tclsh`（GHDL/Vivado）里根本不存在，若放到分支外会直接报错。

**预期结果**：你会确认「batch mode 分叉」是专门为 Modelsim 的 transcript 文件句柄管理设计的 workaround，GHDL/Vivado 完全不走这条逻辑。

> 待本地验证：上述「批量模式下 `transcript file` 会自动截断」是依据代码意图与 Modelsim 常识的推断，**精确的 Modelsim 内部句柄语义建议在本地 Modelsim 环境实测**——可在交互模式与 `vsim -c` 批量模式下各跑一次 `init`，观察 `./Transcript.transcript` 是否都被清空、`Dummy.transcript` 是否都被删除。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sal_print_log` 对 GHDL/Vivado 要「双写」（`puts` 控制台 + 写文件），而对 Modelsim 只调一个 `echo`？

**参考答案**：因为日志有两个受众——**人**（看控制台）和 **`run_check_errors`**（读文件）。Modelsim 的 `echo` 命令天生「控制台 + transcript 文件」都写，一行搞定两个受众；而 GHDL/Vivado 跑在普通 `tclsh` 里，没有自动落盘机制，必须 PsiSim 自己 `puts` 到控制台、再 `open/puts/close` 到文件，分两次满足两个受众。

**练习 2**：`sal_set_transcript_file` 里，为什么 `variable TranscriptFile [file normalize $filename]` 这行要写在 `if/elseif/else` **外面**，而不是放进 Modelsim 分支里？

**参考答案**：因为 GHDL/Vivado 分支是空操作，如果把赋值放进 Modelsim 分支，GHDL/Vivado 就永远不会更新 `TranscriptFile`，导致 `sal_print_log` 里 `open $TranscriptFile a` 打开的是**未初始化/旧值**的路径。把这行放在 `if/elseif/else` 之外，保证**三种仿真器都**把 `TranscriptFile` 指向目标文件——对 Modelsim 是冗余但无害（它自己用 `transcript file`），对 GHDL/Vivado 则是必需。这是一种「跨分支共享副作用」的写法。

**练习 3**：`run_tb` 结尾有 `sal_transcript_off`，`run_check_errors` 开头**又**有 `sal_transcript_off`。重复关一次有必要吗？

**参考答案**：从 Modelsim 的角度看，这是**防御性**的——万一用户在 `run_tb` 和 `run_check_errors` 之间手动执行了 `transcript on`（或别的原因导致记录被重新打开），`run_check_errors` 开头再关一次能保证随后 `open ./Transcript.transcript` 读取时，文件内容不再被并发改写，读到的是稳定快照。对 GHDL/Vivado 这行是空操作，但保留它让接口函数代码在三种仿真器下一致。

### 4.2 Modelsim 版本探测

#### 4.2.1 概念说明

PsiSim 在编译时会按 Modelsim 版本决定是否加 `-novopt` 开关（见 4.3）。要做这件事，首先得**知道当前 Modelsim 的版本号**，并把它存进 `SimulatorVersion` 变量。

获取版本号听起来简单——执行 `vcom -version` 不就行了？但 PsiSim 遇到一个现实障碍：在 Modelsim 的 TCL shell 里，`vcom -version` **把版本信息打印到 stdout**，却**不把它作为一个可被 TCL 捕获的返回值**。也就是说，`set v [vcom -version]` 拿不到版本串。

PsiSim 的解决办法是「**文件中转**」（file relay）：先把 stdout 重定向到一个临时文件，等它写完，再从文件里读回来，最后用正则把版本号抠出来。这套手法封装在 [`sal_init_simulator`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L121-L147) 里。

> 为什么 GHDL/Vivado 不需要这一套？因为它们的版本号不影响任何编译开关（`sal_version_specific_flags` 对它们返回空串，见 4.3），所以 `SimulatorVersion` 直接赋一个占位字符串即可，不做真实探测。

#### 4.2.2 核心流程

`sal_init_simulator` 的 Modelsim 分支流程（伪代码）：

```
sal_init_simulator():
  读 Simulator
  if Modelsim:
      提示 ">>> Error expected ..."          # 预告：重定向 stdout 会触发 Modelsim 警告
      vcom -version >tempVersion.txt         # ① stdout 重定向到临时文件
      提示 ">>> ... until here."
      after 500                              # ② 等 500ms，让文件写盘
      打开 tempVersion.txt，read 进 versionStr，关闭
      删除 tempVersion.txt
      regexp {\s([0-9\.]+)\s} versionStr → versionNr   # ③ 正则抠版本号
      SimulatorVersion = versionNr
      打印 "ModelsimVersion: <版本号>"
  elseif GHDL:
      SimulatorVersion = "NotImplementedForGhdl"
  elseif Vivado:
      SimulatorVersion = "NotImplementedForvivado"     # 注意源码里 vivado 是小写 v
  else:
      puts "Unsupported Simulator ..."
```

要点：

1. **重定向**：`vcom -version >tempVersion.txt` 用 shell 风格的 `>` 把命令输出写到临时文件，绕开「拿不到返回值」的障碍。
2. **等待**：`after 500` 让 TCL 暂停 500 毫秒。因为文件写盘是异步的，立即读可能读到空文件或不完整内容。
3. **正则提取**：从整段版本说明文本里，用 `\s([0-9\.]+)\s` 抠出第一个「被空白包围、由数字和点组成」的 token——通常就是版本号（如 `10.6`、`2020.1`）。
4. **占位值**：GHDL/Vivado 不探测，直接赋占位串。

#### 4.2.3 源码精读

[PsiSim.tcl:121-147](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L121-L147)：探测 Modelsim 版本号并写入 `SimulatorVersion`。

```tcl
proc sal_init_simulator {} {
    variable Simulator
    if {$Simulator == "Modelsim"} {
        puts ">>> Error expected ..."
        vcom -version >tempVersion.txt               ; # ① 重定向 stdout 到临时文件
        puts ">>> ... until here."
        after 500                                    ; # ② 等文件写盘
        set txtFile [open tempVersion.txt]; list
        set versionStr [read $txtFile]; list
        close $txtFile
        file delete tempVersion.txt
        regexp {\s([0-9\.]+)\s} $versionStr dummy versionNr   ; # ③ 正则抠版本号
        variable SimulatorVersion $versionNr
        puts "ModelsimVersion: $versionNr"
    } elseif {$Simulator == "GHDL"} {
        variable SimulatorVersion "NotImplementedForGhdl"
    } elseif {$Simulator == "Vivado"} {
        variable SimulatorVersion "NotImplementedForvivado"     ; # 源码里 vivado 小写 v（笔误）
    } else {
        puts "ERROR: Unsupported Simulator - sal_init_simulator(): $Simulator"
    }
}
```

逐点剖析：

**关于 `>>> Error expected` 提示**：Modelsim 在把 stdout 重定向到文件时会打印一条警告（注释 [L126-L128](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L124-L128) 说明作者找不到抑制它的办法）。这条警告看起来像出错，会吓到用户，所以 PsiSim 用前后两行 `puts` 把它「包」起来，预告「这里会出现一条看似报错的信息，是正常的」。

**关于正则 `{\s([0-9\.]+)\s}`**：

- `\s` 匹配一个空白字符（TCL 的 ARE 语法支持 `\s`）。
- `([0-9\.]+)` 是**捕获组**，匹配「一个或多个数字或点号」——像 `10.6`、`2020.1` 这样的版本号。
- 末尾再一个 `\s`，要求版本号**两侧都是空白**，避免误抠到嵌在单词中间的数字。
- `regexp` 把整体匹配存进 `dummy`，把捕获组存进 `versionNr`。

`vcom -version` 的典型输出形如 `... ModelSim ... vcom 10.6 Compiler ...` 或 `QuestaSim ... vcom 2020.1 Compiler ...`。正则取**第一个**「空白+数字点+空白」的 token，即版本号 `10.6` / `2020.1`。

> 这套正则的前提是版本串里**版本号先于其它纯数字**（如日期里的 `16`、`2020`）出现。对 Modelsim/Questa 的标准输出格式成立，但若某版本输出格式异常，正则可能抠错——这是「按格式硬解析」的固有脆弱性。

**关于占位值的笔误**：Vivado 分支的 `"NotImplementedForvivado"` 里 `vivado` 是**小写 v**（[L143](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L143)），与 GHDL 分支的 `"NotImplementedForGhdl"`（大写 G）不一致。这是源码里一处真实的大小写不统一，因为该值仅作占位、不被任何逻辑比较，所以不影响运行，但读码时值得留意。

**与 `init` 的衔接**：`init` 在设定 `Simulator`、重置数据变量之后，调用 `sal_init_simulator`（[L375](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L375)）。注意 `SimulatorVersion` **不在** `init` 的显式重置块（[L368-L373](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L368-L373)）里，它的初值完全由 `sal_init_simulator` 赋予。

#### 4.2.4 代码实践

**实践目标**：跟踪「文件中转 + 正则」如何从 `vcom -version` 的输出里提取版本号。

**操作步骤**：

1. 打开 [PsiSim.tcl:121-147](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L121-L147)，按注释顺序标出 ① 重定向、② `after 500`、③ `regexp` 三处。
2. 想象 `vcom -version` 的输出写入 `tempVersion.txt` 后内容为（示例）：
   ```
   Model Technology ModelSim PE vcom 10.6 Compiler 2016.11 Nov 16 2016
   ```
3. 用正则 `{\s([0-9\.]+)\s}` 手工匹配这行：
   - 从左往右找「空白 + 数字/点 + 空白」：第一个命中是 ` 10.6 `（`10.6` 两侧都是空格）。
   - 捕获组 `versionNr` = `10.6`。
   - 注意 `2016.11` 虽也符合 `[0-9\.]+`，但 `10.6` 在前，`regexp` 只取第一个匹配。
4. 解释「文件中转」的必要性：若写成 `set v [vcom -version]`，在 Modelsim shell 里 `$v` 拿不到版本串（输出去了 stdout 而非返回值），所以必须 `>tempVersion.txt` 中转。

**需要观察的现象**：`after 500` 这一行若被删除，在文件较大或磁盘较慢时，`read` 可能读到空串或不完整串，导致 `regexp` 抠不到版本号、`SimulatorVersion` 为空，进而影响 4.3 的版本判断。

**预期结果**：你会确认版本探测是一套「重定向 → 等待 → 读回 → 正则」的四步文件中转流程，且只对 Modelsim 真实执行。

> 待本地验证：`vcom -version` 的确切输出格式依 Modelsim/Questa 版本而异。建议在本地 Modelsim 控制台直接执行一次 `vcom -version >tempVersion.txt`，查看实际文本，验证正则 `\s([0-9\.]+)\s` 抠出的确是版本号而非日期里的数字。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sal_init_simulator` 要在 `vcom -version >tempVersion.txt` 之后 `after 500`？去掉它会有什么后果？

**参考答案**：因为把 stdout 重定向到文件后，文件的写盘是异步的——TCL 继续往下执行时，文件可能还没刷完。`after 500` 暂停 500 毫秒给操作系统把缓冲区落盘。去掉它，紧接着的 `read` 可能读到空文件或不完整内容，`regexp` 抠不到版本号，`SimulatorVersion` 变成空串——这会让 4.3 的 `[expr $SimulatorVersion < 10.7]` 报错或误判。

**练习 2**：`sal_init_simulator` 里用 `vcom -version`，而注释（[L124](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L124)）却写「The vsim -version command...」。`vcom` 和 `vsim` 在这里可互换吗？

**参考答案**：注释里写成 `vsim -version` 属于注释与代码不一致（代码实际用的是 `vcom -version`）。二者都能输出版本信息，因为版本是整个 Modelsim 安装固有的、与具体子命令无关。所以「换用 `vsim -version` 也能拿到同样的版本号」在原理上成立，但**以源码实际执行为准**——代码用的是 `vcom`，注释是笔误。这也提醒我们：读码时遇到注释与代码冲突，**以代码为准**。

**练习 3**：GHDL 分支把 `SimulatorVersion` 设成 `"NotImplementedForGhdl"`，这个值之后会在哪里被用到？会不会出问题？

**参考答案**：之后唯一消费 `SimulatorVersion` 的是 `sal_version_specific_flags`（4.3）。但该 proc 的 GHDL/Vivado 分支是空操作、**根本不会读** `SimulatorVersion`，只有 Modelsim 分支才用 `[expr $SimulatorVersion < 10.7]` 做比较。所以 `"NotImplementedForGhdl"` 这个占位串永远不会被送进 `expr`，不会出问题——它存在的意义只是让 `SimulatorVersion` 变量**有个定义过的值**而非 `undefined`，方便调试时 `puts` 查看。

### 4.3 版本相关 flag

#### 4.3.1 概念说明

知道了版本号，接下来就要用它。[`sal_version_specific_flags`](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L104-L119) 的职责很单一：**根据当前仿真器与版本，返回一段「版本相关的编译开关字符串」**，供编译命令拼接。

目前这套机制里**只有 Modelsim 有版本相关开关**：当 Modelsim 版本 **小于 10.7** 时，返回 `"-novopt"`；否则返回空串 `""`。GHDL/Vivado 永远返回空串。

`-novopt` 的背景：Modelsim 在编译/优化阶段有一个 `vopt`（very optimize）步骤。在**旧版** Modelsim（< 10.7）里，PsiSim 需要加 `-novopt` 来**关闭**这个优化，以保证仿真的特定行为；而 10.7 及以后版本里 `vopt` 的处理方式变了（`-novopt` 不再适用/不再需要），所以不再加。这条开关最终被拼进 `vcom`/`vlog` 的参数里（详见 u3-l3 的 `sal_compile_file`）。

> 换句话说，`sal_version_specific_flags` 是 PsiSim 对「不同 Modelsim 版本编译行为不一致」这一历史包袱打的补丁，被集中收拢在 SAL 的一个 proc 里。

#### 4.3.2 核心流程

```
sal_version_specific_flags():
  读 Simulator, SimulatorVersion
  set args ""
  if Modelsim:
      if [expr SimulatorVersion < 10.7]:   # 数值比较
          args = args + " -novopt"
  elseif GHDL 或 Vivado:
      # 什么都不做（args 保持空串）
  else:
      puts "Unsupported Simulator ..."
  return args                              # "" 或 "-novopt"
```

下游消费：`sal_compile_file` 在最开头就调它（[L165](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L165)），把返回值拼进 Modelsim 的编译参数（[L167](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L167)）。对 GHDL/Vivado，这个返回值是空串，拼进去等于没拼。

#### 4.3.3 源码精读

[PsiSim.tcl:104-119](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L104-L119)：按版本返回编译开关。

```tcl
proc sal_version_specific_flags {} {
    variable Simulator
    variable SimulatorVersion
    set args ""
    if {$Simulator == "Modelsim"} {
        if {[expr $SimulatorVersion < 10.7]} {       ; # 数值比较：< 10.7 为真
            set args "$args -novopt"
        }
    } elseif {($Simulator == "GHDL") || ($Simulator == "Vivado")} {
        #Nothing to do
    } else {
        puts "ERROR: Unsupported Simulator - sal_version_specific_flags(): $Simulator"
    }
    return $args                                     ; # 返回 "" 或 "-novopt"
}
```

三处要点：

**（a）数值比较 `[expr $SimulatorVersion < 10.7]`**：`SimulatorVersion` 是 4.2 从 `vcom -version` 抠出的字符串（如 `"10.6"`）。`expr` 会把它当浮点数参与比较：`10.6 < 10.7` 为真 → 返回 `1` → 加 `-novopt`；`10.7 < 10.7` 为假 → 返回 `0` → 不加。这要求 `SimulatorVersion` 是**干净的数字串**——这正是 4.2 那套正则要保证的（只抠数字和点）。

> 严格说，`if {[expr ...]}` 里的 `expr` 是多余的——`if` 本身就会把条件当表达式求值，直接写 `if {$SimulatorVersion < 10.7}` 即可。这里多套一层 `expr` 不影响结果，属代码风格上的小冗余。

**（b）累积式拼接 `set args "$args -novopt"`**：用「`$args` + 新值」的方式累加。当前只有一个开关，所以 `args` 要么是 `""`、要么是 `" -novopt"`。这种写法便于将来扩展更多版本开关（虽然目前只有一个）。

**（c）消费点**：在 `sal_compile_file` 里（[L162-L167](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L162-L167)）：

```tcl
proc sal_compile_file {lib path language langVersion fileOptions} {
    variable Simulator
    variable CompileSuppress
    set vFlags [sal_version_specific_flags]          ; # 取版本开关
    if {$Simulator == "Modelsim"} {
        set args "-work $lib $vFlags -suppress $CompileSuppress $fileOptions -quiet $path"
        ...                                          ; # $vFlags 被拼进 vcom/vlog 参数
```

`$vFlags` 被原样插进 Modelsim 编译参数串里。若是空串，插进去相当于没有；若是 ` -novopt`，则带上该开关。GHDL/Vivado 分支根本不读 `$vFlags`，所以对它们无效。

#### 4.3.4 代码实践

**实践目标**：验证 `sal_version_specific_flags` 的返回值如何随版本号变化，并确认它只对 Modelsim 编译生效。

**操作步骤**：

1. 打开 [PsiSim.tcl:104-119](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L104-L119)，在脑中代入不同的 `SimulatorVersion`：
   - `Simulator = "Modelsim"`、`SimulatorVersion = "10.6"` → `[expr 10.6 < 10.7]` = 1 → `args = " -novopt"`。
   - `Simulator = "Modelsim"`、`SimulatorVersion = "10.7"` → `[expr 10.7 < 10.7]` = 0 → `args = ""`。
   - `Simulator = "Modelsim"`、`SimulatorVersion = "2020.1"` → `[expr 2020.1 < 10.7]` = 0 → `args = ""`。
   - `Simulator = "GHDL"` → 直接进 elseif 空操作 → `args = ""`。
2. 跟到消费点 [PsiSim.tcl:165-167](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L165-L167)：对版本 10.6 的 Modelsim，`vcom` 参数串会多出 ` -novopt`；对 10.7+ 则没有。
3. 思考边界：如果 4.2 的正则因输出格式异常把 `SimulatorVersion` 抠成了空串 `""`，这里 `[expr "" < 10.7]` 会怎样？

**需要观察的现象**：版本开关完全由 `SimulatorVersion < 10.7` 这一个数值比较决定；GHDL/Vivado 即便 `SimulatorVersion` 是占位串 `"NotImplementedForGhdl"`，也因走在 elseif 空操作分支而**不参与比较**，安全。

**预期结果**：你会确认 `-novopt` 只对「Modelsim 且版本 < 10.7」生效，且其效果仅体现在 Modelsim 的 `vcom`/`vlog` 参数里。

> 待本地验证：`[expr "" < 10.7]` 在 TCL 里会尝试把空串当数值，结果依 TCL 版本可能报错或按 0 处理。若担心 4.2 正则失败导致此处异常，可在本地 `tclsh` 里试跑 `expr "" < 10.7` 观察行为。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `sal_version_specific_flags` 对 GHDL/Vivado 直接返回空串，而不是也去探测它们的版本？

**参考答案**：因为 GHDL/Vivado **没有版本相关的编译开关**需要处理——它们的编译命令（`ghdl -a`、`xvhdl`）不依赖一个「< 某版本就加某开关」的逻辑。既然没有消费需求，就没有探测的必要，`SimulatorVersion` 给个占位串即可。这是「**按需探测**」的设计：只为会影响行为的仿真器（Modelsim）付出探测成本。

**练习 2**：`sal_version_specific_flags` 的返回值除了 `-novopt`，还可能扩展哪些版本相关开关？扩展时要注意什么？

**参考答案**：凡是「某 Modelsim 版本以上/以下需要不同编译参数」的需求都可加进来，比如某版本起 `vcom` 不再支持某旧选项、或需要新增某选项。扩展时要注意两点：① 仍用 `set args "$args <新开关>"` 的累积式拼接，保持多个开关可共存；② 比较仍依赖 `SimulatorVersion` 是干净数字串，所以 4.2 的正则提取必须可靠——若版本号格式变化导致抠错，这里的判断会连锁出错。这说明 4.2 与 4.3 是**紧耦合**的：版本探测的格式假设直接决定了版本开关的正确性。

**练习 3**：`sal_version_specific_flags` 在 `sal_compile_file` 里被调用（[L165](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L165)）。为什么是「每次编译一个文件就调一次」，而不是在 `init` 里调一次缓存起来？

**参考答案**：因为 `Simulator` 与 `SimulatorVersion` 在整个 `run.tcl` 生命周期内**不变**（`init` 一次性设定后只读），所以理论上缓存一次即可，每次重算是微小的冗余。当前「每文件重算」的写法更简单、无状态、不必引入额外的缓存变量，代价是每个源文件多一次 `expr` 比较——对编译性能影响可忽略。这是一种「**用重复计算换取无状态简洁**」的取舍，与 `sal_print_log` 每条日志独立 open/close 文件是同一种风格。

## 5. 综合实践

**综合任务**：绘制一张《transcript 与版本处理全链路图》，把本讲三个模块串起来，并验证它们在 `init` 里的执行顺序。

请完成下面三件事：

1. **画 transcript 生命周期时序**。以 `init → compile_files → run_tb → run_check_errors` 为时间轴，标出下列事件发生的时机与调用位置（给出行号）：
   - `sal_clean_transcript` 被调（共两处：`init` 的 [L377](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L377) 与 `run_tb` 的 [L784](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L784)）。
   - `sal_transcript_off` 被调（`run_tb` 结尾 [L837](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L837)、`run_check_errors` 开头 [L723](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L723)）。
   - `sal_print_log` 在各阶段追加日志（GHDL/Vivado 走双写）。
   - `./Transcript.transcript` 被读取（`run_check_errors` 的 [L724-L725](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L724-L725)）。

2. **梳理 `init` 里的版本与日志初始化顺序**。阅读 [PsiSim.tcl:351-378](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L351-L378)，确认 `init` 末尾两句 `sal_init_simulator`（[L375](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L375)）与 `clean_transcript`（[L377](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L377)）的先后，并解释为什么必须是这个顺序（提示：`clean_transcript` 内部的 `sal_set_transcript_file` 会写 `TranscriptFile` 变量，而 `sal_init_simulator` 探测版本时会产生 `>>> Error expected` 等控制台输出——两者有无依赖？）。

3. **填一张版本开关决策表**。按下表填空，给出 `sal_version_specific_flags` 在各种输入下的返回值：

   | `Simulator` | `SimulatorVersion` | 返回值 | 是否最终影响编译参数 |
   | --- | --- | --- | --- |
   | `"Modelsim"` | `"10.6"` | `" -novopt"`（待你确认） | 是 |
   | `"Modelsim"` | `"10.7"` | ? | ? |
   | `"Modelsim"` | `"2020.1"` | ? | ? |
   | `"GHDL"` | `"NotImplementedForGhdl"` | ? | ? |
   | `"Vivado"` | `"NotImplementedForvivado"` | ? | ? |

**参考答案要点**（供自检）：

- 时序图中，`./Transcript.transcript` 经历「init 清空 → 编译期写入 → run_tb 开头再次清空（丢编译日志）→ 运行期写入 → run_tb 结尾关闭 → run_check_errors 读取判错」。
- `init` 顺序：先 `sal_init_simulator`（探测版本，此时 transcript 尚未就位，版本探测只往控制台 `puts`、不依赖 transcript），再 `clean_transcript`（建立空 transcript 并设定 `TranscriptFile`）。两者无数据依赖，但逻辑上「先搞清楚身份/版本，再准备日志落点」是自然顺序。若反过来，`sal_init_simulator` 的控制台输出在 Modelsim 下可能被还没清空的旧 transcript 记录，污染日志。
- 决策表：`10.7` → `""`（不影响）；`2020.1` → `""`（不影响）；GHDL → `""`（不影响，elseif 空操作）；Vivado → `""`（不影响）。只有「Modelsim + 版本 < 10.7」返回 ` -novopt` 并影响编译参数。

## 6. 本讲小结

- **日志统一靠 `sal_print_log`**：Modelsim 走内建 `echo`（控制台 + transcript 都写），GHDL/Vivado 走「`puts` 控制台 + `open/puts/close` 追加文件」的**双写**——根因是 Modelsim 自带 transcript 机制，而 GHDL/Vivado 跑在独立 `tclsh` 里没有。
- **transcript 三件套**（`sal_transcript_off/on/set_file`）只对 Modelsim 有实质动作；`sal_set_transcript_file` 末尾那条 `variable TranscriptFile [file normalize $filename]` 位于 `if` 之外，保证三种仿真器都更新文件路径变量。
- **`sal_clean_transcript` 是最复杂的一个**：Modelsim 分支用「Dummy 中转」手法释放文件句柄、按 `batch_mode` 决定是否显式删文件；GHDL/Vivado 分支直接删文件 + 设变量 + 提前 `return` 跳过 `sal_transcript_on`。`./Transcript.transcript` 在 `init` 与 `run_tb` 开头各被清空一次。
- **版本探测用「文件中转」**：`sal_init_simulator` 把 `vcom -version` 的 stdout 重定向到临时文件、`after 500` 等写盘、`read` 回读、用正则 `{\s([0-9\.]+)\s}` 抠出版本号写入 `SimulatorVersion`；GHDL/Vivado 仅赋占位串（注意源码 `"NotImplementedForvivado"` 小写 v 的笔误）。
- **版本开关 `sal_version_specific_flags`**：仅 Modelsim 且版本 `< 10.7` 时返回 ` -novopt`，由 `sal_compile_file` 拼进 `vcom`/`vlog` 参数；GHDL/Vivado 恒返回空串，不生效。
- **整体取向**：PsiSim 把「抹平三种仿真器日志差异」「按版本打补丁」这类**与具体仿真器/版本强相关的脏活**全部收拢在 SAL 的几个工具型 proc 里，让上层的接口函数（`compile`、`run_tb`、`run_check_errors`）保持仿真器无关——这正是 u3-l1 讲过的「SAL 只翻译操作」原则的具体落地。

## 7. 下一步学习建议

本讲把 SAL 的「水电管道」（日志、transcript、版本）铺好了，后续 proc 都要靠 `sal_print_log` 打印、靠 transcript 判错。建议按以下顺序继续：

1. **u3-l3 编译抽象（`sal_compile_file` / `sal_clean_lib`）**：本讲的 `sal_version_specific_flags` 返回值就是被 `sal_compile_file` 在 [L165](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L165) 消费的。下一步去看它如何把统一的 `(lib, path, language, version, options)` 翻译成 Modelsim `vcom`/`vlog`、GHDL `ghdl -a`（2002/2008 双编译）、Vivado `xvhdl`。
2. **u3-l4 仿真运行抽象（`sal_run_tb`）**：去看三分支并列最复杂的一个——`vsim`、`ghdl --elab-run`、`xelab+xsim`，注意其中 GHDL 分支用 `exec` 捕获仿真输出再交给本讲的 `sal_print_log` 落盘（[L256-L257](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L256-L257)），这正是本讲「GHDL/Vivado 手动追加 transcript」的实际应用。
3. **u3-l5 交互调试（`launch_tb` / 波形）**：收尾 `sal_launch_tb`（仅 Modelsim）与 `sal_open_wave`（仅 GHDL+GTKWave），把调试工作流串起来。

读完这三篇，你就能把 u3-l1 那张《SAL dispatch 全景表》里每个 proc 的分支内部都填上具体命令，具备评估「新增第 4 个仿真器时要改哪些日志/版本/编译/运行逻辑」的能力——那是 u4-l3 的主题。

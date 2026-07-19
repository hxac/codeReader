# 扩展新模拟器与架构取舍

## 1. 本讲目标

PsiSim 现在支持三种仿真器：Modelsim、GHDL、Vivado。如果有一天我们要接入第四种——比如开源的 Icarus Verilog——需要改哪些地方？改动是不是集中、安全、可回归？

学完本讲，你应该能够：

- 列出为 PsiSim 增加一种全新仿真器时，**必须修改的全部 proc 与各自要新增的 dispatch 分支**。
- 看懂 SAL（模拟器抽象层）里 `if/elseif` dispatch 的三种形态，判断新仿真器该挂到哪一类。
- 识别 PsiSim 当前架构的四个核心取舍——**全局可变状态、`eval` 字符串拼接、非阻断式错误处理、零测试覆盖**——并说出它们各自的风险与改进方向。

本讲是单元 4 的收尾篇，不再讲新机制，而是把前几讲积累的源码认知**反过来用**：站在「改造者」的视角审视这套单文件框架的可维护性。

## 2. 前置知识

本讲假设你已读完下列讲义（依赖链 u3-l1 → u3-l3 → u3-l4）：

- **u3-l1 SAL 设计与 dispatch 模式**：知道每个 `sal_*` proc 开头 `variable Simulator`，再用 `if/elseif` 按字符串值分派；`Simulator` 由 `init` 一次性设定后只读。
- **u3-l3 编译抽象**：知道 `sal_compile_file` 把统一的五元组 `(lib path language langVersion fileOptions)` 翻译成 Modelsim `vcom`、GHDL `ghdl -a`、Vivado `xvhdl` 三条不同命令。
- **u3-l4 仿真运行抽象**：知道 `sal_run_tb` 把统一运行意图翻译成 `vsim`、`ghdl --elab-run`、`xelab+xsim`。

两个背景概念先讲清楚：

- **dispatch（分派）**：同一段「意图」（比如「编译一个 VHDL 文件」）在不同仿真器下是不同命令。PsiSim 用一条 `if/elseif` 链，按 `Simulator` 变量的值挑选对应实现，这种写法叫 dispatch。
- **可变全局状态（mutable global state）**：PsiSim 把所有运行期信息放在 10 个命名空间变量里（`Libraries`、`Sources`、`TbRuns`……），这些变量在整个解释器里谁都能读、谁都能改，没有隔离。

> 提示：本讲引用的源码全部来自 [PsiSim.tcl](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl)。建议在另一个窗口打开该文件，边读边对照行号。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
|------|------|----------------|
| `PsiSim.tcl` | 唯一源码，966 行，全部封装在 `namespace eval psi::sim` | 13 个 `sal_*` proc 的 dispatch、`init` 的参数解析、`launch_tb` 的接口层分派、8 处 `eval`、10 个状态变量 |
| `Changelog.md` | 版本演进记录 | 印证「加仿真器」的真实历史（GHDL 在 1.4.0 加入，Vivado 在 2.2.0 加入），用来推断扩展工作量 |

讲义内引用格式为 `[文件:行号](永久链接)`，点击直达 GitHub 上对应 HEAD 的源码行。

## 4. 核心概念与源码讲解

### 4.1 新仿真器扩展清单

#### 4.1.1 概念说明

要把一种新仿真器接入 PsiSim，本质是回答一个问题：**「同一个操作，这个仿真器的命令是什么？」** PsiSim 已经为 Modelsim/GHDL/Vivado 各自回答了 13 遍——每回答一遍，就对应一个 `sal_*` proc 里的一组分支。新加一种仿真器，意味着把这 13 道题再答一遍（外加 `init` 里增加一个开关）。

历史能印证这件事的规模：[Changelog.md](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/Changelog.md) 显示，GHDL 在 1.4.0 版本加入，Vivado 在 2.2.0 版本加入，二者都是「minor」级（新增功能）更新，而非小修小补——说明接入一种新仿真器是一个跨越多个 bugfix 版本才稳定下来的工程。

#### 4.1.2 核心流程

接入新仿真器（以假想的 `Icarus` 为例）的完整改动清单：

```
1. init            增加 -icarus 开关  →  variable Simulator "Icarus"
2. 13 个 sal_*     在每个 proc 的 if/elseif 链里增加 Icarus 分支
3. launch_tb       接口层 guard + dispatch 增加对 Icarus 的放行与分派
4. tb_run_skip     无需改代码，但 "Icarus" 成为合法的 skip 取值
```

其中第 2 步是主体工作量。13 个 `sal_*` proc **不是平均用力**——下一节（4.2）会按 dispatch 形态把它们分成三类，这里先给出总览。

#### 4.1.3 源码精读

**第一步：`init` 增加开关。** 仿真器身份的源头在 [PsiSim.tcl:351-379](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L351-L379) 的 `init` proc。它用朴素 `while` 循环逐个解析开关，目前只认 `-ghdl` 和 `-vivado`：

```tcl
# PsiSim.tcl:356-367 （节选）
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
```

要支持 Icarus，就在这里加一条 `elseif {$thisArg == "-icarus"} { variable Simulator "Icarus" }`。这是「身份登记」的唯一入口——后面所有 dispatch 都依赖 `Simulator` 这个字符串。

**第二步：13 个 `sal_*` proc 各加分支。** 这 13 个 proc 集中在 [PsiSim.tcl:31-342](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L31-L342) 的 SAL 区块。最典型的「三分支并列」形态出现在 `sal_compile_file`（编译）与 `sal_run_tb`（运行）里——以 `sal_init_simulator` 为例，它为三种仿真器各写了一段完全不同的逻辑：

```tcl
# PsiSim.tcl:121-147 （骨架，省略实现细节）
proc sal_init_simulator {} {
    variable Simulator
    if {$Simulator == "Modelsim"} {
        # ... vcom -version 重定向到文件，正则抠版本号 ...
    } elseif {$Simulator == "GHDL"} {
        variable SimulatorVersion "NotImplementedForGhdl"
    } elseif {$Simulator == "Vivado"} {
        variable SimulatorVersion "NotImplementedForvivado"
    } else {
        puts "ERROR: Unsupported Simulator - sal_init_simulator(): $Simulator"
    }
}
```

要支持 Icarus，就在最后的 `else` 之前插一条 `elseif {$Simulator == "Icarus"} { ... }`。同样的手术要做在所有 13 个 proc 上（详见 4.2 的分类表）。

**第三步：`launch_tb` 接口层。** `launch_tb` 在 SAL 之外、接口函数层还有一个**额外的白名单 guard**，[PsiSim.tcl:852-859](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L852-L859)：

```tcl
# PsiSim.tcl:856-859
if {($Simulator != "Modelsim") && ($Simulator != "GHDL")} {
    sal_print_log "ERROR: launch_tb: this command is only implemented for Modelsim and GHDL"
    return
}
```

这个 guard 与 `sal_launch_tb`（仅 Modelsim）、`sal_open_wave`（仅 GHDL）是**两套独立检查**。若要让 Icarus 也能调试，必须三处都改：放行 guard、在 [PsiSim.tcl:941-956](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L941-L956) 的 dispatch 里加 `if {"Icarus" == $Simulator}` 分支、并在对应的 `sal_*` 里实现。

**第四步：`tb_run_skip` 无需改代码。** [PsiSim.tcl:698-702](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L698-L702) 的 `tb_run_skip` 只是把字符串写进 `SKIP` 字段，运行期再由 `lsearch $skip $Simulator` 比对。所以一旦 `Simulator` 多了 `"Icarus"` 这个值，`tb_run_skip "Icarus"` 自动生效——但前提是字符串大小写完全一致（见 4.3.3）。

#### 4.1.4 代码实践

**实践目标**：用「Icarus」这个假想仿真器，逐条核对扩展清单，确保没有遗漏。

**操作步骤**：

1. 打开 PsiSim.tcl，用搜索定位所有 `proc sal_` 开头的定义（共 13 个），记下每个的行号。
2. 对每个 `sal_*` proc，问自己两个问题：① Icarus 要不要在这里干活？② 要干的话，Icarus 的命令是什么？
3. 另外单独检查 `init`（开关）、`launch_tb`（接口层 guard + dispatch）、`tb_run_skip`（文档取值）。

**预期结果**：你应该得到一张「proc → 该加什么分支」的表。下面是参考答案（Icarus 假定为类似 GHDL 的「命令行 exec 型」仿真器，用 `iverilog` 编译、`vvp` 运行）：

| proc | 行号 | Icarus 分支要做什么 |
|------|------|---------------------|
| `init` | 351 | 加 `-icarus` 开关 → `Simulator "Icarus"` |
| `sal_print_log` | 31 | 归入 GHDL‖Vivado 组（puts + 写文件） |
| `sal_transcript_off/on` | 48 / 59 | 归入 GHDL‖Vivado 组（无操作） |
| `sal_set_transcript_file` | 70 | 归入 GHDL‖Vivado 组（无操作） |
| `sal_clean_transcript` | 82 | 归入 GHDL‖Vivado 组（删文件） |
| `sal_version_specific_flags` | 104 | 归入 GHDL‖Vivado 组（无操作） |
| `sal_init_simulator` | 121 | 加 `elseif`，赋 `SimulatorVersion` 占位串 |
| `sal_clean_lib` | 149 | 归入 GHDL‖Vivado 组（`file delete -force`） |
| `sal_compile_file` | 162 | **加独立分支**：`exec iverilog ...` |
| `sal_exec_script` | 208 | 归入 GHDL‖Vivado 组（exec） |
| `sal_run_tb` | 224 | **加独立分支**：`exec vvp ...` |
| `sal_launch_tb` | 309 | 仅 Modelsim；若要支持须加 Icarus 分支 |
| `sal_open_wave` | 335 | 仅 GHDL；若 Icarus 用 GTKWave 可复用 |
| `launch_tb`（接口层） | 852 | 放行 guard + 加 dispatch 分支 |

> 说明：上表是「源码阅读型实践」的结论，不需要真正运行 Icarus。表中「归入某组」的具体改法见 4.2。

#### 4.1.5 小练习与答案

**练习 1**：如果 `init` 里加了 `-icarus` 分支，但忘了在 `sal_compile_file` 里加 Icarus 分支，会怎样？

**参考答案**：`sal_compile_file` 的 `if/elseif` 链一路走到 `else`，执行 [PsiSim.tcl:204](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L204) 的 `puts "ERROR: Unsupported Simulator - sal_compile_file(): Icarus"`，**但不会中断**。源文件实际上不会被编译，而后续 `run_tb` 仍会尝试运行——见 4.3 关于「非阻断错误」的讨论。

**练习 2**：为什么 `tb_run_skip` 不需要改任何代码就能支持 Icarus？

**参考答案**：`tb_run_skip` 只是写字符串到 `SKIP` 字段（[PsiSim.tcl:698-702](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L698-L702)），运行期用 `lsearch $skip $Simulator` 比对（[PsiSim.tcl:807](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L807)）。只要 `init -icarus` 让 `Simulator == "Icarus"`，`tb_run_skip "Icarus"` 自动命中。它的耦合点是「字符串相等」，而非硬编码列表。

### 4.2 dispatch 扩展点

#### 4.2.1 概念说明

13 个 `sal_*` proc 的 dispatch 并不长一个样。仔细看会发现三种形态：

- **形态 A：三分支并列**——Modelsim、GHDL、Vivado 各写一段独立逻辑。这种 proc 的三种仿真器行为差异大，无法合并。
- **形态 B：Modelsim 独立 + GHDL‖Vivado 合并**——因为 GHDL 和 Vivado 都是「命令行 exec 型」仿真器（没有自带 TCL 解释器），很多操作对它们是一样的「什么都不做」或「调文件 API」，所以用 `($Simulator == "GHDL") || ($Simulator == "Vivado")` 合成一条。
- **形态 C：单仿真器独占**——目前只有 Modelsim（`sal_launch_tb`）和 GHDL（`sal_open_wave`）出现过。

这个分类直接决定「加 Icarus 时该改哪里」：形态 A 要插一个新 `elseif`，形态 B 要把合并条件再加一项，形态 C 要决定是否新增。

#### 4.2.2 核心流程

判断「Icarus 该挂到哪一类」的决策流程：

```
该 proc 三种仿真器行为差异大吗？
├─ 是 → 形态 A：新增 elseif {$Simulator == "Icarus"}，独立实现
└─ 否 → 形态 B：Icarus 行为是否和 GHDL/Vivado 一样？
        ├─ 是 → 把合并条件改成 (GHDL || Vivado || Icarus)
        └─ 否 → 其实属于形态 A，应独立实现
```

关键直觉：**exec 型仿真器天然适合形态 B**，因为「日志写文件、transcript 无内建命令、清理即删目录」这些操作对它们都成立。

#### 4.2.3 源码精读

**形态 B 的典型：`sal_clean_lib`。** [PsiSim.tcl:149-160](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L149-L160) 把 GHDL 和 Vivado 合并，因为它们都是「库 = 一个目录」，删库就是删目录：

```tcl
# PsiSim.tcl:151-159 （骨架）
if {$Simulator == "Modelsim"} {
    vlib $lib; vdel -all -lib $lib; vlib $lib
} elseif {($Simulator == "GHDL") || ($Simulator == "Vivado")} {
    file delete -force $lib
} else {
    puts "ERROR: Unsupported Simulator - sal_clean_lib(): $Simulator"
}
```

Icarus 同样是「库 = 目录」语义，所以加 Icarus 只需把第二行条件扩成 `($Simulator == "GHDL") || ($Simulator == "Vivado") || ($Simulator == "Icarus")`——**无需新逻辑**。同样的扩法适用于 8 个形态 B 的 proc。

**形态 A 的典型：`sal_compile_file`。** [PsiSim.tcl:162-206](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L162-L206) 里三种仿真器命令完全不同，无法合并：

```tcl
# PsiSim.tcl:166-205 （骨架）
if {$Simulator == "Modelsim"} {
    ... vcom/vlog ...
} elseif {$Simulator == "GHDL"} {
    ... exec ghdl -a --std=08 ...
} elseif {$Simulator == "Vivado"} {
    ... eval exec xvhdl ...
} else {
    puts "ERROR: Unsupported Simulator - sal_compile_file(): $Simulator"
}
```

Icarus 要编译就得新插一段 `elseif {$Simulator == "Icarus"} { exec iverilog ... }`，写它自己的命令。`sal_run_tb`（[PsiSim.tcl:224-307](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L224-L307)）同理，要插 `exec vvp ...`。

**形态 C 的典型：`sal_open_wave`。** [PsiSim.tcl:335-342](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L335-L342) 只对 GHDL 实现（用 GTKWave）：

```tcl
# PsiSim.tcl:337-341
if {$Simulator == "GHDL"} {
    exec gtkwave -f $wave &
} else {
    puts "ERROR: Unsupported Simulator - sal_open_wave(): $Simulator"
}
```

若 Icarus 也用 GTKWave 看波形，可以把条件扩成 `(GHDL || Icarus)`；若它用别的查看器，就改成形态 A。

**dispatch 总览表**（核对自源码行号）：

| 形态 | proc（行号） | 加 Icarus 的改法 |
|------|--------------|------------------|
| A 三分支并列 | `sal_init_simulator`(121)、`sal_compile_file`(162)、`sal_run_tb`(224) | 新增 `elseif` 分支，独立实现 |
| B Modelsim ‖ GHDL+Vivado | `sal_print_log`(31)、`sal_transcript_off`(48)、`sal_transcript_on`(59)、`sal_set_transcript_file`(70)、`sal_clean_transcript`(82)、`sal_version_specific_flags`(104)、`sal_clean_lib`(149)、`sal_exec_script`(208) | 合并条件加 `\|\| ($Simulator == "Icarus")` |
| C 单仿真器独占 | `sal_launch_tb`(309，仅 Modelsim)、`sal_open_wave`(335，仅 GHDL) | 视情况合并或新增 |

#### 4.2.4 代码实践

**实践目标**：学会用 dispatch 形态快速判断改动量。

**操作步骤**：

1. 在 PsiSim.tcl 里搜索所有 `$Simulator ==` 出现的位置（共约 25 处）。
2. 对每个 `sal_*` proc，数它的 `elseif` 个数与是否带 `||`：带 `||` 的是形态 B，三个独立 `elseif` 的是形态 A，只有一条 `if` 的是形态 C。
3. 假设 Icarus 是「类似 GHDL 的 exec 型仿真器」，预测：哪些 proc 只改一行条件、哪些必须写新逻辑？

**需要观察的现象**：你会发现**真正需要写新逻辑的只有 3 个 proc**（编译、运行、init 仿真器），其余 10 个要么复用、要么空操作。这就是 dispatch 集中化的收益——也是它的代价（见 4.3）。

**预期结果**：8 个形态 B 的 proc 一行改完，3 个形态 A 的 proc 要写实质逻辑，2 个形态 C 的 proc 视调试需求决定。**待本地验证**：若你装了 Icarus，可临时在某个形态 B proc 里插入 `puts "Icarus branch hit"` 验证 dispatch 是否走到。

#### 4.2.5 小练习与答案

**练习 1**：`sal_init_simulator`（[PsiSim.tcl:121](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L121)）为什么是形态 A 而不是形态 B？

**参考答案**：因为 GHDL 和 Vivado 在这里赋的占位串不同（`"NotImplementedForGhdl"` vs `"NotImplementedForvivado"`，见 [PsiSim.tcl:140-143](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L140-L143)），且 Modelsim 有完全不同的「文件中转抠版本号」逻辑。三者行为各异，无法合并。顺带注意：这两个占位串大小写不一致（`Ghdl` vs `vivado`），是历史遗留的细节瑕疵。

**练习 2**：如果 Icarus 的库语义**不是**「目录」而是「单个文件」，`sal_clean_lib` 还能归入形态 B 吗？

**参考答案**：不能。那时 `file delete -force $lib` 对 Icarus 是错的，必须为 Icarus 单独写删除逻辑，于是它对 `sal_clean_lib` 退化为形态 A。dispatch 形态取决于**行为是否真的相同**，而非仿真器名字。

### 4.3 架构取舍与改进方向

#### 4.3.1 概念说明

前面两节讲的是「怎么改」，这一节讲「为什么改起来有风险」。PsiSim 是一个 966 行的单文件框架，它的简洁来自于几个贯穿全局的设计选择，而这些选择各有代价：

- **全局可变状态**：10 个命名空间变量充当「内存登记表」，谁都能改，没有隔离。
- **`eval` 字符串拼接**：为了把命令拼成一行再执行，大量使用 `eval $cmd`，把字符串重新当 TCL 解析。
- **非阻断式错误处理**：所有 `else` 分支只 `puts` 一行错误，不抛异常、不中断。
- **零测试覆盖**：`git ls-files` 显示仓库只有 6 个文件，没有任何测试、CI 或示例。

这四点决定了「加一种新仿真器」看似只是「复制粘贴 13 段」，实则暗藏陷阱。

#### 4.3.2 核心流程

四个取舍各自的风险传导路径：

```
全局可变状态 ──→ 忘调 init / 顺序错 → 旧状态泄漏到新回归
eval 字符串拼接 ──→ 路径含空格 / generic 含特殊字符 → 命令拼错或被注入
非阻断错误 ──→ dispatch 漏写分支 → 仿真静默失败，最终仍报 SUCCESS
零测试 ──→ 改 dispatch 无法自动验证 → 只能手动跑三种商业/开源仿真器
```

这四条叠加的后果是：**一个「看似成功」的回归可能实际什么都没编译**。

#### 4.3.3 源码精读

**取舍一：全局可变状态。** 10 个变量声明在 [PsiSim.tcl:16-26](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L16-L26)，`init` 显式重置其中 6 个（[PsiSim.tcl:368-377](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L368-L377)）：

```tcl
# PsiSim.tcl:368-377 （节选）
variable Libraries [list]
variable Sources [list]
variable TbRuns [list]
variable CompileSuppress ""
variable RunSuppress ""
variable CurrentLib "NoCurrentLibrary"
```

注意 `ThisTbRun` **不在重置列表里**——它是「草稿」变量（见 u2-l3）。这意味着：① 在同一解释器里跑两套回归，第二套若忘了 `init`，第一套的 `Sources`/`TbRuns` 会残留；② 即便调了 `init`，`ThisTbRun` 仍是上一次的草稿，若某次 `create_tb_run` 之前的状态泄漏，可能产生半成品 run。**没有任何机制阻止「忘调 init」**，框架完全依赖调用者遵守约定。

**取舍二：`eval` 字符串拼接（8 处）。** 全文共 8 处 `eval`，最典型的是 `sal_run_tb` 的 Modelsim 分支 [PsiSim.tcl:226-232](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L226-L232)：

```tcl
# PsiSim.tcl:231-232
set cmd "vsim -quiet -t 1ps -msgmode both $supp $lib.$tbName $tbArgs"
eval $cmd
```

这里 `$tbArgs` 是 generic 值（如 `-gClockRatio_g=3`），被直接字符串插值进命令再 `eval`。**问题**：如果某个 generic 值或文件路径里含空格、`[`、`$`、`;`、`"`，`eval` 会把它们当 TCL 语法重新解析。例如 generic 值为 `foo;file delete -force /home` 时，`eval` 会先执行删除命令——这是一条**命令注入**通道（虽然 config.tcl 通常受信，但仍是脆弱点）。即便无恶意，路径含空格就会导致 `vsim` 收到错误分词。`compile_files` 包装也有同样写法（[PsiSim.tcl:609-612](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L609-L612) 的 `eval "compile $jonedArgs"`）。相比之下，`sal_compile_file` 的 Modelsim 分支用 `vcom {*}$args`（[PsiSim.tcl:170](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L170)）才是**列表展开**的安全写法——可见作者知道正确做法，只是没处处贯彻。

**取舍三：非阻断式错误。** 每个 `else` 都只 `puts "ERROR: Unsupported Simulator"`（如 [PsiSim.tcl:204](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L204)），既不 `error` 也不 `return -code error`。更要命的是 `run_check_errors` 只 grep 用户传入的 errorString 和硬编码 `Fatal:`（[PsiSim.tcl:730-731](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L730-L731)），**不包含 `"ERROR: Unsupported Simulator"`**。于是：你加了 `init -icarus` 却漏改某个 `sal_*`，该 proc 打印 ERROR 后继续，`run_tb` 照常「跑完」，最后 `run_check_errors` 判定 `SIMULATIONS COMPLETED SUCCESSFULLY`——一个**完全没编译任何东西的回归被报告为通过**。这是 PsiSim 最危险的一条链。

**取舍四：零测试。** `git ls-files` 输出仅 6 个文件：`Changelog.md`、`CommandRef.md`、`LGPL2_1.txt`、`License.txt`、`PsiSim.tcl`、`README.md`。没有任何 `tests/`、没有 `.tcl` 测试、没有 CI 配置、没有 `examples/`。这意味着 13×3 = 39 组 dispatch 分支**没有任何自动回归保障**，加一个分支只能靠人手在三种仿真器（其中两种是商业软件）上逐一验证。Changelog 里大量的「Bugfixes: GHDL ... / Vivado ...」条目（如 2.5.0、2.4.0）正是这种「靠人工发现」模式的副产物。

#### 4.3.4 代码实践

**实践目标**：亲手指出「全局可变状态 + eval 拼接」带来的至少两个风险（对应总实践任务的第二问）。

**操作步骤**：

1. 在 [PsiSim.tcl:231-232](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L231-L232) 处，设想 `tbArgs` 的值是字符串 `-gName=a b c`（含空格），跟踪 `eval $cmd` 会把 `vsim` 收到几个参数。再设想值是 `-gX=[exit]`，跟踪会发生什么。
2. 在 [PsiSim.tcl:368-377](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L368-L377) 处，设想用户在 `run.tcl` 里漏写了 `init`，直接 `source config.tcl`，问：`Sources`、`TbRuns`、`ThisTbRun` 分别是什么状态？

**预期结果（两个风险，均可在源码层面确认，无需运行）**：

- **风险 A（eval 拼接 → 分词错误与注入）**：`tbArgs` 含空格时，`vsim` 会把 `a`、`b`、`c` 当三个独立参数，generic 设置错位；含 `[...]` 或 `;` 时，`eval` 会执行其中的 TCL 命令。根因是字符串被二次解析，而非按列表展开。
- **风险 B（全局可变状态 → 状态泄漏）**：漏调 `init` 时，`Sources`/`TbRuns` 保留上一次回归的内容（或为空但 `ThisTbRun` 是陈旧草稿），新 `config.tcl` 的声明会与残留数据混合，且没有任何报错。

**附加风险（非阻断错误 + 零测试）**：见 4.3.3 取舍三、四——漏写 dispatch 分支会被报为 SUCCESS。

> 说明：本实践是「源码阅读型」，结论来自对 `eval` 语义与 `init` 重置列表的静态分析，**待本地验证**的是你若真构造含空格的 generic 跑一次，能否复现分词错误。

#### 4.3.5 小练习与答案

**练习 1**：`compile_files` 为什么用 `eval "compile $jonedArgs"` 而不是直接调 `compile`？（[PsiSim.tcl:609-612](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L609-L612)）

**参考答案**：因为内部 `compile` 与 Modelsim 自带命令同名，刻意不 `namespace export`，对外只暴露 `compile_files` 这个包装。包装要把不定参数（`-all`、`-lib x`、`-tag y`…）原样转发给 `compile`，作者选择了 `join` 成字符串再 `eval` 的写法。代价同上：参数含特殊字符会被二次解析。更安全的写法是 `compile {*}$args`（列表展开）。

**练习 2**：如果要给 PsiSim 加最小测试，你会先测什么？

**参考答案**：先测 **dispatch 契约**——用一个「假仿真器」替换 `Simulator` 的值，在每个 `sal_*` 里塞一个 `lappend dispatched "$proc:$Simulator"`，然后断言：给定 `init -ghdl`，`compile_files -all` 一定走进了 `sal_compile_file` 的 GHDL 分支而非 Modelsim 分支。这样能把「漏写分支导致静默 SUCCESS」这类 bug 锁死，且不需要真的装商业仿真器。

**练习 3**：`init -icarus` 与 `init -Icarus`（大小写错）行为有何不同？

**参考答案**：[PsiSim.tcl:358-365](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L358-L365) 用 `$thisArg == "-icarus"` 做精确匹配，大小写敏感。`-Icarus` 不匹配，落入 `else` 打印 `WARNING: ignored argument`，`Simulator` 保持默认值 `"Modelsim"`。用户会以为在跑 Icarus，实际跑的是 Modelsim——又一个静默回退的例子。

## 5. 综合实践

**任务**：你是 PsiSim 的维护者，社区请求接入 Icarus Verilog。请产出一份完整的「接入方案 + 风险评估」文档（纯文字即可，不改源码）。

要求覆盖：

1. **改动清单**：列出所有要改的 proc 及每个该加的分支（参考 4.1.4 的表，并补充 Icarus 的实际命令）。提示：Icarus 用 `iverilog -o <out> <files>` 编译、`vvp <out>` 运行，库语义为目录，波形可用 `.vcd` + GTKWave。
2. **dispatch 分类**：把 13 个 `sal_*` 按形态 A/B/C 归类（参考 4.2.3 的表），指出哪些一行改完、哪些要写实质逻辑。
3. **风险清单**：指出当前「全局可变状态 + eval 拼接 + 非阻断错误 + 零测试」架构带来的至少两个具体风险（参考 4.3.4），并各给一条改进建议。
4. **回归策略**：在没有任何商业仿真器的前提下，你如何验证 Icarus 分支真的被走到？（提示：4.3.5 练习 2 的「假仿真器」思路）

**参考答案要点**：

- 改动清单：`init` 加 `-icarus`；8 个形态 B proc 合并条件加 `|| ($Simulator == "Icarus")`；`sal_compile_file` 加 `iverilog` 分支、`sal_run_tb` 加 `vvp` 分支、`sal_init_simulator` 加占位串；`sal_open_wave` 若复用 GTKWave 则合并条件，`sal_launch_tb` 视调试需求决定；`launch_tb` 接口层 guard（[PsiSim.tcl:856](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L856)）放行 Icarus 并加 dispatch。
- 风险与改进：① eval 拼接 → 命令注入/分词错误，改进为 `{*}` 列表展开；② 非阻断错误 → 漏分支报 SUCCESS，改进为 `error` 抛异常并让 `run_check_errors` 默认包含 `"ERROR:"` 哨兵；③ 全局状态 → 状态泄漏，改进为把状态收进一个 context dict 并在 `init` 重置全部变量（含 `ThisTbRun`）；④ 零测试 → 加 dispatch 契约测试。
- 回归：注入一个 `Simulator == "TEST"` 的假仿真器，在每个 `sal_*` 记录被调分支，断言 dispatch 走向正确，无需真实仿真器。

## 6. 本讲小结

- 接入一种新仿真器，**主体工作量在 13 个 `sal_*` proc**：要么插新 `elseif`（形态 A），要么扩合并条件（形态 B），要么单仿真器独占（形态 C）。其中只有 3 个 proc（编译、运行、init 仿真器）需要写实质逻辑。
- `init` 的 `-icarus` 开关是仿真器身份的唯一入口，所有 dispatch 都依赖 `Simulator` 这个字符串；大小写敏感的精确匹配会导致拼写错误时**静默回退到 Modelsim**。
- `launch_tb` 有 SAL 之外的第二道接口层 guard（[PsiSim.tcl:856](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L856)），支持调试需要同时改 guard、dispatch 与对应 `sal_*`。
- 四大架构取舍：**全局可变状态**（`ThisTbRun` 不被 `init` 重置、忘调 init 则状态泄漏）、**`eval` 字符串拼接**（8 处，含分词错误与命令注入风险）、**非阻断错误**（漏写 dispatch 分支会被报为 SUCCESS）、**零测试覆盖**（39 组分支无自动回归）。
- 改进方向：dispatch 表/按仿真器拆 namespace 替代 if/elseif 串、`{*}` 列表展开替代 `eval`、错误改抛异常并纳入 `run_check_errors` 哨兵、补 dispatch 契约测试。
- 历史印证：Changelog 显示 GHDL（1.4.0）与 Vivado（2.2.0）都是跨多个 bugfix 版本才稳定的 minor 级更新——接入新仿真器从来不是一次性工程。

## 7. 下一步学习建议

- **回到 u3 全单元横向对比**：现在你已看清 dispatch 的扩展代价，建议重读 u3-l3（编译抽象）与 u3-l4（运行抽象），用「如果是我加 Icarus，这段命令怎么写」的视角再过一遍 Modelsim/GHDL/Vivado 三条分支，体会「同一意图、三种说法」的翻译难度。
- **动手实验（可选）**：若你有 Icarus 环境，可 fork 仓库，按 4.1.4 的清单实现一个最小 Icarus 支持（先只做 `sal_compile_file` + `sal_run_tb` 两个形态 A proc），用一段 Verilog 测试台跑通，体会 dispatch 改造的真实手感。
- **延伸阅读**：对照 `sal_compile_file` 里 `vcom {*}$args`（安全写法，[PsiSim.tcl:170](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L170)）与 `sal_run_tb` 里 `eval $cmd`（危险写法，[PsiSim.tcl:232](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L232)），学习 TCL 中「列表展开 `{*}`」与「`eval` 字符串重解析」的本质区别——这是写出健壮 TCL 框架的关键功底。
- **手册完结**：本篇是 PsiSim 学习手册的最后一篇。至此你已从「项目概览」走到「架构批判」，完整覆盖了配置数据模型、运行流程、SAL 抽象层与二次开发。建议以本讲的「风险评估」为起点，尝试给 PsiSim 提交一个改进 PR（哪怕只是把 8 处 `eval` 中的 1 处改成 `{*}` 展开），把所学落到代码上。

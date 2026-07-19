# 仿真运行抽象（sal_run_tb）

## 1. 本讲目标

本讲打开 PsiSim 模拟器抽象层（SAL）中负责「跑一次仿真」的核心内部过程 `sal_run_tb`。它和上一篇 [u3-l3（编译抽象）](u3-l3-sal-compile.md) 是一对：编译产出库实体，仿真消费库实体。学完本讲，你应当能够：

- 说清楚 `sal_run_tb` 的 6 个参数 `(lib tbName tbArgs timeLimit suppressMsgNo {wave ""})` 各自从哪里来、为什么第 6 个参数 `wave` 有默认值；
- 独立写出同一个测试台运行（带 generics 与时间限制）在 Modelsim `vsim`、GHDL `--elab-run`、Vivado `xelab`+`xsim` 三种仿真器下被翻译成的真实命令；
- 解释 GHDL 分支如何用 `string map` 把 `"100 us"` 变成 `--stop-time=100us`、以及 `--wave` 参数如何只在这一个分支被消费；
- 解释 Vivado 分支为什么必须写一个 `psi_vivado_init.ini` 文件（`--lib` 开关失效的 workaround），以及它如何把 `-gClockRatio_g=3` 转换成 `-generic_top ClockRatio_g=3`；
- 说出三种仿真器把仿真输出送进 `Transcript.transcript` 的三种不同机制（这是 [u2-l7 run_check_errors](u2-l7-error-checking.md) 能统一判错的前提）。

本讲承接 [u3-l1（SAL 设计与 dispatch 模式）](u3-l1-sal-overview.md) 和 [u2-l6（run_tb 与脚本钩子）](u2-l6-run-tb-and-hooks.md)：u2-l6 把 `sal_run_tb` 当作「启动一次仿真的黑盒」，本讲打开这个黑盒。

## 2. 前置知识

在进入源码前，先用几段话回顾你必须带入本讲的认知（都在前置讲义里讲过，这里只做最小承接）：

- **SAL 与 dispatch**：所有跟具体仿真器打交道的逻辑都在 13 个 `sal_*` 内部过程里。每个过程开头 `variable Simulator`，紧跟 `if/elseif` 链按 `Simulator`（`"Modelsim"` / `"GHDL"` / `"Vivado"`）分派，`Simulator` 由 `init` 一次性设定后只读。`sal_run_tb` 是其中最大的一个 proc。
- **run_tb 如何调用 sal_run_tb**：[u2-l6](u2-l6-run-tb-and-hooks.md) 讲过，`run_tb` 遍历 `TbRuns`，对通过过滤、未 skip 的每个 run，先跑 pre-script，再 `foreach tbArgs $allArgLists` 逐组参数调一次 `sal_run_tb`，最后跑 post-script。也就是说，`sal_run_tb` 的入参不是凭空来的，而是 `TbRuns` 中某个 run 的字段投影。
- **编译产物在哪里**：[u3-l3](u3-l3-sal-compile.md) 讲过，GHDL 把库产物放进 `$lib/v08` 子目录（以及可选的 `$lib/v93`），Vivado 把库放进与库同名的目录（`--work $lib=$lib`），Modelsim 用自己的库管理。本讲会看到 `sal_run_tb` 如何「链接」这些产物。
- **Modelsim 的运行环境**：Modelsim 分支跑在 Modelsim 自带的 TCL 解释器里，`vsim`/`run`/`quit` 是内建命令，可直接调用；GHDL 与 Vivado 是外部可执行文件，必须用 TCL `exec` 调用，脚本要跑在独立 TCL 解释器里。

下表给出本讲会反复出现的几个仿真器命令行术语，先有个印象：

| 术语 | 含义 |
|------|------|
| `vsim` | Modelsim 的仿真加载命令（加载顶层实体并准备运行） |
| `run -all` / `run <time>` | Modelsim/xsim 的「跑到结束」/「跑指定时长」命令 |
| `ghdl --elab-run` | GHDL 的「编译链接 + 运行」一步到位命令 |
| `xelab` / `xsim` | Vivado 的「elaborate 生成快照」/「运行快照」两步命令 |
| `--stop-time` | GHDL 的仿真停止时刻参数 |
| `--wave` | GHDL 的波形输出文件参数 |
| `-g<Name>=<Value>` | Modelsim / GHDL 的顶层 generic 覆盖语法 |
| `-generic_top <Name>=<Value>` | Vivado xelab 的顶层 generic 覆盖语法（注意与上面不同） |
| `--initfile` | Vivado xelab 的库映射文件参数（本讲的 workaround 核心） |

## 3. 本讲源码地图

本讲涉及的关键源码点全部集中在 `PsiSim.tcl`，并在 `CommandRef.md` 里有两条相关命令文档。

| 文件 | 位置 | 作用 |
|------|------|------|
| `PsiSim.tcl` | `sal_run_tb`（L224–L307） | 本讲主角：把一次 TB 运行翻译成三种仿真器的仿真命令 |
| `PsiSim.tcl` | `sal_run_tb` Modelsim 分支（L226–L240） | `vsim` 流程与 `+nowarn` 抑制 |
| `PsiSim.tcl` | `sal_run_tb` GHDL 分支（L241–L257） | `--elab-run` 与 `--stop-time`/`--wave` |
| `PsiSim.tcl` | `sal_run_tb` Vivado 分支（L258–L303） | initfile workaround、`-generic_top` 转换、`xelab`+`xsim` 两步 |
| `PsiSim.tcl` | `run_tb` 调用点（L824–L827） | 回归路径调用方：5 个参数，不传 `wave` |
| `PsiSim.tcl` | `launch_tb` 调用点（L943、L952） | 调试路径调用方：Modelsim 走 `sal_launch_tb`，GHDL 走 `sal_run_tb` 并传 `wave` |
| `PsiSim.tcl` | `sal_launch_tb` / `sal_open_wave`（L309–L342） | 佐证：Modelsim 调试走另一条路；GHDL 波形用 `.ghw` |
| `CommandRef.md` | `run_tb`（L428–L464）、`launch_tb`（L496–L543） | 命令文档（注意 launch_tb 的 `.vcd` 描述已过时，见 4.3.3） |

## 4. 核心概念与源码讲解

本讲按「统一入口 → 三种仿真器分支」的顺序拆成 4 个最小模块。三个仿真器分支是重点（对应规格里要求的三组最小模块），入口模块为它们提供参数来源与调用方的上下文。

### 4.1 统一入口：sal_run_tb 的 6 参数签名、两个调用方与 wave 的双重身份

#### 4.1.1 概念说明

`sal_run_tb` 是 SAL 暴露给上层 `run_tb` / `launch_tb` 的「统一仿真入口」。它的核心思想与 [u3-l3 的 `sal_compile_file`](u3-l3-sal-compile.md) 完全一致：**上层只描述「要跑什么」，SAL 负责「怎么跑」**。上层传进来的是一组与仿真器无关的抽象参数——目标库、测试台名、参数串、时间限制、消息抑制编号、波形文件；`sal_run_tb` 拿到后，按 `Simulator` 把它翻译成 Modelsim、GHDL 或 Vivado 各自的实际命令。

但 `sal_run_tb` 比 `sal_compile_file` 多一个值得专门讲的点：它**同时服务两条上层路径**——回归测试（`run_tb`）和交互调试（`launch_tb` 的 GHDL 分支）。这两条路径对「波形」的需求不同，于是第 6 个参数 `wave` 被设计成带默认值的可选参数。这是理解整个 proc 形状的关键。

#### 4.1.2 核心流程

`sal_run_tb` 的执行流程：

1. `variable Simulator` 把命名空间状态变量链接进局部作用域。
2. 按 `Simulator` 分三路（Modelsim / GHDL / Vivado），每路各自构造并执行仿真命令。
3. 任一分支都未命中 → `puts` 打印 `ERROR: Unsupported Simulator`（非阻断）。

入参的来源链很重要，画出来是（以 `run_tb` 路径为例）：

```
TbRuns 中的一个 run (dict)
   ├── TB_LIB     ─┐
   ├── TB_NAME    ─┤
   ├── TB_ARGS    ─┼─→ run_tb (L822-L826) foreach tbArgs 取一组
   ├── TIME_LIMIT ─┤   ↓ 再加上 RunSuppress
   └── ...        ─┘
                      sal_run_tb $runLib $runName $tbArgs $timeLimit $RunSuppress
                                                                 (5 个参数，wave 缺省 "")
```

`launch_tb` 路径则多传一个 `wave`：

```
sal_run_tb $runLib $runName $argsToUse $timeLimit $RunSuppress $wave
                                                                (6 个参数)
```

参数对照表：

| 形参 | 来源（run_tb 路径） | 来源（launch_tb 路径） | 含义 |
|------|---------------------|------------------------|------|
| `lib` | `TB_LIB` | `TB_LIB` | 测试台所在的库 |
| `tbName` | `TB_NAME` | `TB_NAME` | 测试台顶层实体名 |
| `tbArgs` | `TB_ARGS` 的一组 | `allArgLists` 的第 `argidx` 组 | generic 覆盖串，如 `-gClockRatio_g=3` |
| `timeLimit` | `TIME_LIMIT` | `TIME_LIMIT` | 仿真时长，`"None"` 表示跑到结束 |
| `suppressMsgNo` | `RunSuppress` | `RunSuppress` | 运行期消息抑制编号串 |
| `wave` | **不传**（默认 `""`） | `.ghw` 文件名（仅 GHDL） | 波形输出文件 |

#### 4.1.3 源码精读

[PsiSim.tcl:L224-L225](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L224-L225) —— `sal_run_tb` 的签名：6 个参数，第 6 个 `wave` 用 `{wave ""}` 给了默认值空串。这是 TCL 的「带默认值的可选参数」写法——调用方传 5 个参数时，`wave` 自动取空串。

回归路径的调用方（`run_tb`）只传 5 个参数：

[PsiSim.tcl:L824-L827](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L824-L827) —— `run_tb` 在 `foreach tbArgs $allArgLists` 循环里调 `sal_run_tb $runLib $runName $tbArgs $timeLimit $RunSuppress`，**不传第 6 个参数**。这正是 [u2-l6](u2-l6-run-tb-and-hooks.md) 强调的「回归测试不要波形」——所以 `wave` 取默认空串，仿真不产出波形文件。

调试路径的调用方（`launch_tb`）则按仿真器分流：

[PsiSim.tcl:L941-L956](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L941-L956) —— Modelsim 走另一个 proc `sal_launch_tb`（L943），**不经过 `sal_run_tb`**；GHDL 才走 `sal_run_tb` 并传第 6 个参数 `wave`（L952）。这揭示了一个重要的不对称：**`sal_run_tb` 的 Modelsim 分支根本不读 `wave`**，`wave` 参数实际上只被 GHDL 分支消费（见 4.3.3）。Modelsim 的调试波形走的是 `sal_launch_tb` 里独立的 `do`/`add wave` 逻辑（L320–L329）。

注意 `launch_tb` 在最开头就排除了 Vivado：

[PsiSim.tcl:L856-L859](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L856-L859) —— `launch_tb` 只支持 Modelsim 与 GHDL，Vivado 直接报错返回。所以 Vivado 下 `sal_run_tb` 永远只走回归路径（`wave` 恒为空串）。

一句话总结 `wave` 的双重身份：**它是「调试路径才需要、且只有 GHDL 分支会用」的可选参数**；回归路径靠默认值空串把它屏蔽掉。

#### 4.1.4 代码实践

**实践目标**：确认 `sal_run_tb` 的 6 个形参与两个调用方实参的一一对应关系，特别是 `wave` 何时被传。

**操作步骤**：

1. 打开 `PsiSim.tcl`，定位 `sal_run_tb` 签名（L224），数清 6 个形参。
2. 定位 `run_tb` 的调用（L826），数一数传了几个实参。
3. 定位 `launch_tb` 的两个调用（L943 与 L952），分别数实参个数，并注意它们调的是不同的 proc。
4. 在 `sal_run_tb` 体内搜索 `wave`，确认它只在哪个分支被读取。

**需要观察的现象**：`run_tb` 传 5 个参数（`wave` 缺省）；`launch_tb` 的 GHDL 分支传 6 个参数（含 `wave`），Modelsim 分支则根本不调 `sal_run_tb`。

**预期结果**：`wave` 在 `sal_run_tb` 体内只出现在 GHDL 分支（L251–L253），Modelsim 与 Vivado 分支都不读它。

**待本地验证**：无（纯静态阅读即可确认）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `wave` 被设计成第 6 个带默认值的可选参数，而不是像前 5 个一样必填？

**参考答案**：因为 `sal_run_tb` 要同时服务两条上层路径。回归路径（`run_tb`）不需要波形，只传 5 个参数；调试路径（`launch_tb` 的 GHDL 分支）需要波形，传 6 个参数。把 `wave` 设成 `{wave ""}` 的默认参数，可以让回归路径的调用代码保持简洁（不必每次都写一个空 `wave`），同时调试路径能自然地多传一个参数。这是 TCL 用「带默认值的尾部参数」实现「可选行为」的惯用法。

**练习 2**：如果有人在 Modelsim 模式下通过 `launch_tb` 传了 `-wave`，`sal_run_tb` 的 Modelsim 分支会用到它吗？

**参考答案**：不会。Modelsim 模式下 `launch_tb` 走的是 `sal_launch_tb`（L943），根本不调 `sal_run_tb`；而即便强行调 `sal_run_tb`，它的 Modelsim 分支（L226–L240）也完全不读 `wave`。Modelsim 的波形是靠 `sal_launch_tb` 内部独立的 `do $wave`（恢复波形视图文件）或 `add wave -r /*`（添加全部信号）逻辑实现的。

---

### 4.2 Modelsim 分支：vsim 流程

#### 4.2.1 概念说明

Modelsim 是 PsiSim 的默认仿真器，也是支持最完整的一路。因为 PsiSim 脚本本身就跑在 Modelsim 的 TCL 解释器里，所以 `vsim`、`run`、`quit` 这些都是 Modelsim 内建命令，可以直接当 TCL 命令调用，不需要 `exec`。

Modelsim 分支把一次仿真拆成固定的四步：**加载（vsim）→ 关警告 → 运行 → 退出**。其中「加载」这一步把 generic 覆盖、消息抑制、库定位全部塞进一条 `vsim` 命令；「运行」这一步根据有无时间限制二选一（`run $timeLimit` 或 `run -all`）。

一个贯穿本分支的关键事实：**Modelsim 原生支持 `-gGenericName=Value` 语法**，所以上层传来的 `tbArgs`（如 `-gClockRatio_g=3`）被**原样**插进 `vsim` 命令，不需要任何转换。这与 Vivado 分支（4.4）要费力把 `-g` 转成 `-generic_top` 形成鲜明对比。

#### 4.2.2 核心流程

Modelsim 分支的执行顺序：

1. **构造消息抑制串** `supp`：若 `suppressMsgNo` 非空，拼成 `+nowarn$suppressMsgNo`（如 `+nowarn135,1236,`）；否则为空串。
2. **构造并执行 vsim 命令**：`vsim -quiet -t 1ps -msgmode both $supp $lib.$tbName $tbArgs`，用 `eval` 执行。
3. **关 std 警告**：把 Modelsim 全局变量 `StdArithNoWarnings` 与 `NumericStdNoWarnings` 都设为 1。
4. **运行**：`timeLimit != "None"` 时 `run $timeLimit`，否则 `run -all`。
5. **退出仿真**：`quit -sim`，释放当前仿真对象（不影响 Modelsim 进程本身）。

注意第 2 步用 `eval $cmd` 而不是直接 `vsim ...`——因为 `$tbArgs` 是一个可能含空格的字符串（如 `-gA=1 -gB=2`），用 `eval` 让它被正确地切分成多个 argv 元素传给 `vsim`。这与 [u3-l3 Vivado 分支](u3-l3-sal-compile.md) 用 `eval exec` 的动机同源。

#### 4.2.3 源码精读

[PsiSim.tcl:L226-L240](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L226-L240) —— Modelsim 分支整体：构造 `supp` → 拼 `vsim` 命令并 `eval` → 关两个 std 警告 → 按 `timeLimit` 二选一运行 → `quit -sim`。

逐项解释 vsim 命令的片段：

| 片段 | 作用 |
|------|------|
| `vsim` | Modelsim 仿真加载命令 |
| `-quiet` | 减少控制台输出 |
| `-t 1ps` | 时间分辨率为 1 皮秒 |
| `-msgmode both` | 消息同时输出到控制台与 transcript |
| `$supp` | 形如 `+nowarn135,1236,` 的运行期消息抑制（`+nowarn` 是 Modelsim 的抑制语法） |
| `$lib.$tbName` | 库限定的顶层实体，如 `mylib.tb_foo` |
| `$tbArgs` | generic 覆盖串，如 `-gClockRatio_g=3`，Modelsim 原生识别 |

`+nowarn` 与 [u2-l4 讲过的 RunSuppress](u2-l4-message-suppression.md) 衔接：`run_tb` 把 `RunSuppress`（形如 `135,1236,` 的拖尾逗号串）作为第 5 个参数 push 进来，这里被原样拼到 `+nowarn` 后面。注意 [u2-l4](u2-l4-message-suppression.md) 强调过：**消息抑制只对 Modelsim 生效**——GHDL 与 Vivado 分支都不读 `suppressMsgNo`，本讲会逐一验证这一点。

两个 std 警告开关（L233–L234）是 Modelsim 专属的全局变量：

- `StdArithNoWarnings = 1` 抑制 `std_logic_arith` 包的警告；
- `NumericStdNoWarnings = 1` 抑制 `numeric_std` 包的警告。

它们在 `vsim` 加载完之后才设置，目的是让仿真运行期间这些常见包的噪声警告安静下来。

时间限制的处理（L235–L239）很直白：`"None"` 是 `create_tb_run` 给 `TIME_LIMIT` 的默认值（见 [u2-l3](u2-l3-tb-run-definition.md)），表示测试台自己会停（用 `std.env.finish` 或 `assert ... severity failure` 之类）；只有当用户调过 `tb_run_add_time_limit "100 us"` 时，才走 `run $timeLimit` 强制限时。注意 Modelsim 的 `run` 命令**接受带空格的时长**`100 us`，这点与 GHDL 分支不同（见 4.3.3）。

最后 `quit -sim`（L240）只结束当前仿真对象，保留 Modelsim 进程，让后续的 `sal_run_tb` 调用可以继续加载下一个测试台。

举一个完整的例子：库 `mylib`、测试台 `tb_foo`、`tbArgs = "-gClockRatio_g=3"`、`RunSuppress = "868,"`、`timeLimit = "None"`，则实际执行序列约为：

```
vsim -quiet -t 1ps -msgmode both +nowarn868, mylib.tb_foo -gClockRatio_g=3
# （Modelsim 内部设置 StdArithNoWarnings=1; NumericStdNoWarnings=1）
run -all
quit -sim
```

Modelsim 的仿真输出会通过 [u3-l2 讲过的 transcript file 机制](u3-l2-transcript-log-version.md)（`sal_set_transcript_file ./Transcript.transcript`）自动落进 `Transcript.transcript`，供 `run_check_errors` 判错。

#### 4.2.4 代码实践

**实践目标**：手工推演一个带 generic、带消息抑制、带时间限制的 Modelsim 仿真序列。

**操作步骤**：

1. 假设条件：用户调过 `run_suppress 868`、`tb_run_add_arguments "-gClockRatio_g=3"`、`tb_run_add_time_limit "10 us"`，库 `mylib`、测试台 `tb_foo`。
2. 先按 [u2-l4](u2-l4-message-suppression.md) 算 `RunSuppress` 的值（提示：拖尾逗号）。
3. 按 L227–L230 算 `supp`。
4. 按 L231 拼出 `vsim` 命令字符串。
5. 按 L235–L239 判断走哪条 `run`。

**需要观察的现象**：`supp` 的 `+nowarn` 前缀、`tbArgs` 原样出现在命令尾部、`run` 带时间参数。

**预期结果**：

```
# RunSuppress = "868,"
# supp = "+nowarn868,"
vsim -quiet -t 1ps -msgmode both +nowarn868, mylib.tb_foo -gClockRatio_g=3
run 10 us
quit -sim
```

**待本地验证**：在 Modelsim 里实际跑一遍，`puts` 打印 `$cmd` 与你的推演对照；观察 transcript 里 `vsim` 回显行。

#### 4.2.5 小练习与答案

**练习 1**：Modelsim 分支为什么能把 `tbArgs`（`-gClockRatio_g=3`）原样插进 `vsim` 命令，而 Vivado 分支却要费力转换？

**参考答案**：因为 Modelsim 的 `vsim` 命令原生就用 `-gGenericName=Value` 语法覆盖顶层 generic，与 PsiSim 上层（`tb_run_add_arguments`）要求的 `-g` 形式完全一致，所以直接透传即可。而 Vivado 的 `xelab` 用的是另一套语法 `-generic_top Name=Value`，必须做字符串改写（见 4.4）。这是同一种「意图」（覆盖 generic）在不同仿真器里「说法」不同的典型例子，也正是 SAL 存在的意义。

**练习 2**：如果用户没有调过 `run_suppress`，`suppressMsgNo` 是什么？`supp` 会是什么？

**参考答案**：`run_suppress` 未调用时，`init`（L372）把 `RunSuppress` 清零为空串 `""`，经 `run_tb` 透传后 `suppressMsgNo = ""`。于是 L228 的 `if {$suppressMsgNo != ""}` 不成立，`supp` 保持空串 `""`，`vsim` 命令里 `$supp` 那一格为空（被 `eval` 折叠掉，不产生空参数）。

---

### 4.3 GHDL 分支：--elab-run 与 --stop-time / --wave

#### 4.3.1 概念说明

GHDL 是开源 VHDL 仿真器，作为外部可执行文件，必须用 TCL `exec` 调用，脚本要跑在独立 TCL 解释器里（见 [u2-l1](u2-l1-namespace-state-init.md)）。GHDL 分支用一条 `ghdl --elab-run` 命令把「elaborate（编译链接顶层）」和「run（运行）」合并成一步。

这个分支有三个值得专门讲的设计点：

1. **永远从 `$lib/v08` 启动**——直接消费 [u3-l3 编译阶段](u3-l3-sal-compile.md) 产出的 2008 库产物，与「双编译」机制首尾呼应；
2. **时间限制要去空格**——`"100 us"` 必须被 `string map` 变成 `100us` 才能塞进 `--stop-time`；
3. **波形参数只在这一个分支被消费**——`wave` 参数的真正归宿就是这里的 `--wave`。

和 Modelsim 一样，GHDL 也原生支持 `-gName=Value` 语法覆盖 generic，所以 `tbArgs` 同样原样透传，不做转换。

#### 4.3.2 核心流程

GHDL 分支的执行顺序：

1. **给 tbArgs 加前导空格**：若 `tbArgs` 非空，变成 `" $tbArgs"`（为后面无空格拼接留出分隔）。
2. **构造停止时间串** `stopTime`：若 `timeLimit != "None"`，用 `string map {" " ""} $timeLimit` 去掉所有空格，拼成 ` --stop-time=100us`；否则为空串。
3. **构造波形串**：若 `wave != ""`，拼成 ` --wave=$wave`；否则为空串。
4. **拼出并执行命令**：`ghdl --elab-run --ieee=synopsys --std=08 ... --workdir=$lib/v08 --work=$lib $tbName$tbArgs$stopTime$wave --ieee-asserts=disable`，用 `eval "exec $cmd"` 执行。
5. **把输出送进 transcript**：捕获 `exec` 的 stdout 到 `outp`，再 `sal_print_log $outp`。

注意第 4 步的命令是靠**字符串拼接**（不是列表）组装的：`$tbName$tbArgs$stopTime$wave` 四段直接连在一起，所以前面才要给 `tbArgs`/`stopTime`/`wave` 各自预留前导空格做分隔符。这是一种很 TCL 风格的「先拼字符串再 eval」写法。

#### 4.3.3 源码精读

[PsiSim.tcl:L241-L257](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L241-L257) —— GHDL 分支整体：处理 `tbArgs`/`stopTime`/`wave` 三段可选串 → 拼一条 `ghdl --elab-run` 命令 → `eval "exec $cmd"` 捕获输出 → 写进 transcript。

先看时间限制的去空格（L245–L250）：

[PsiSim.tcl:L245-L250](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L245-L250) —— `string map {" " ""} $timeLimit` 把 `"100 us"` 里的空格全部删掉变成 `100us`，再拼成 `--stop-time=100us`。`sal_print_log "Stop $timeLimit"` 顺带在 transcript 里打一行提示。

为什么要去空格？因为 `--stop-time=100 us`（带空格）会被 shell/argv 解析成两个独立参数 `--stop-time=100` 和 `us`，导致 GHDL 报错。GHDL 要求时间值紧跟在等号后且不含空格，如 `100us`、`1ms`、`500ns`。这是同一个 `timeLimit` 在三种仿真器下被格式化得不一样的根源——Modelsim 的 `run 100 us` 接受空格，GHDL 的 `--stop-time` 不接受。

再看波形串（L251–L253），这是 `wave` 参数的真正归宿：

[PsiSim.tcl:L251-L253](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L251-L253) —— 若 `wave` 非空，拼成 ` --wave=$wave`。回归路径下 `wave` 是默认空串，所以这一段不出现，仿真不产波形；调试路径（`launch_tb` 的 GHDL 分支）下 `wave` 是一个 `.ghw` 文件名，这一段才生效。

波形文件名由 `launch_tb` 命名：

[PsiSim.tcl:L947-L949](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L947-L949) —— `launch_tb` 在 GHDL 模式下把 `wave` 设为 `$runName\_$argidx\.ghw`，形如 `tb_foo_0.ghw` 或 `tb_foo_default.ghw`。

**注意一个文档与代码的不一致**：`CommandRef.md`（L528）描述 GHDL 的 `-wave` 选项时说生成的是 `.vcd` 文件（`<tb_name><argidx|default>.vcd`），但实际源码（L948）生成的是 `.ghw` 文件。`.ghw` 是 GHDL 的原生波形格式，能正确表达 VHDL 的 record 等复合类型（VCD 做不到），所以代码用 `.ghw` 是有意为之。**以源码为准**——这是阅读老项目时常见的「文档落后于代码」情形。

然后看主命令（L254）：

[PsiSim.tcl:L254](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L254) —— 完整的 GHDL 仿真命令。逐项解释：

| 片段 | 作用 |
|------|------|
| `ghdl --elab-run` | elaborate + run 一步到位 |
| `--ieee=synopsys` | 用 Synopsys 版 IEEE 包，行为更接近 Modelsim（与编译阶段一致） |
| `--std=08` | VHDL-2008（与编译阶段一致） |
| `-frelaxed-rules` | 放宽规则，兼容性更好 |
| `-Wno-shared` | 抑制共享变量警告 |
| `--workdir=$lib/v08` | **永远读 `v08` 子目录**——消费编译阶段的 2008 产物 |
| `--work=$lib` | 库名 |
| `$tbName` | 顶层实体名 |
| `$tbArgs` | generic 覆盖（如 ` -gClockRatio_g=3`，GHDL 原生识别 `-g`） |
| `$stopTime` | 停止时刻（如 ` --stop-time=100us`） |
| `$wave` | 波形输出（如 ` --wave=tb_foo_0.ghw`） |
| `--ieee-asserts=disable` | 关闭 IEEE assert 的失败终止行为 |

`--workdir=$lib/v08` 这一行是本分支与 [u3-l3 编译抽象](u3-l3-sal-compile.md) 的呼应点：编译阶段把所有文件（即便是声明为 2002 的，也额外按 2008 编一遍）最终都放进 `$lib/v08`；仿真阶段就只读这个 `v08`。两侧的 `v08` 必须严格对应，否则仿真找不到实体。

最后看输出捕获（L255–L257）：

[PsiSim.tcl:L255-L257](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L255-L257) —— 先 `sal_print_log $cmd` 把命令本身打进日志（方便调试），再用 `eval "exec $cmd"` 执行并把 stdout 捕获到 `outp`，最后 `sal_print_log $outp` 把仿真输出追加进 `Transcript.transcript`。

这一步揭示了 GHDL 分支把仿真输出送进 transcript 的机制：**`exec` 捕获 stdout → `sal_print_log` 手动追加**。对比 Modelsim 分支靠 Modelsim 自身的 `transcript file` 机制自动落盘，GHDL 必须手动中转——因为 GHDL 是外部进程，它的 stdout 不会自动进 Modelsim 的 transcript。这也是 [u3-l2 sal_print_log](u3-l2-transcript-log-version.md) 对 GHDL/Vivado 走「控制台 + 文件」双写的原因。

举一个完整的例子：库 `mylib`、测试台 `tb_foo`、`tbArgs = "-gClockRatio_g=3"`、`timeLimit = "100 us"`、`wave = ""`（回归路径），则打印并执行的命令约为：

```
Stop 100 us
ghdl --elab-run --ieee=synopsys --std=08 -frelaxed-rules -Wno-shared --workdir=mylib/v08 --work=mylib tb_foo -gClockRatio_g=3 --stop-time=100us --ieee-asserts=disable
```

注意 `--wave` 段没有出现（回归路径 `wave` 为空），`--stop-time=100us` 的空格被去掉了，`tbArgs` 前面有一个空格做分隔。

#### 4.3.4 代码实践

**实践目标**：理解 `string map` 去空格的必要性，以及 `wave` 何时出现。

**操作步骤**（示例代码，不是 PsiSim 原有代码）：

1. 在任意独立 TCL 解释器里跑下面这段，观察去空格前后命令长什么样。

```tcl
# 示例代码：复现 GHDL 时间限制的格式化
set timeLimit "100 us"
set tbName "tb_foo"
set tbArgs "-gClockRatio_g=3"
if {$tbArgs != ""} { set tbArgs " $tbArgs" }
set stopTime " --stop-time=[string map {" " ""} $timeLimit]"
set wave ""   ;# 回归路径
if {$wave != ""} { set wave " --wave=$wave" }
puts "ghdl --elab-run --workdir=mylib/v08 --work=mylib $tbName$tbArgs$stopTime$wave"
```

2. 再把 `set wave "tb_foo_0.ghw"`（调试路径）跑一遍，对比命令差异。
3. 把 `set timeLimit "None"` 跑一遍，确认 `stopTime` 段消失。

**需要观察的现象**：`100 us` 被压成 `100us`；`wave` 非空时多出 `--wave=tb_foo_0.ghw`；`timeLimit` 为 `None` 时 `--stop-time` 段完全消失。

**预期结果**：

```
# 回归路径（wave 空）
ghdl --elab-run --workdir=mylib/v08 --work=mylib tb_foo -gClockRatio_g=3 --stop-time=100us

# 调试路径（wave 非空）
ghdl --elab-run --workdir=mylib/v08 --work=mylib tb_foo -gClockRatio_g=3 --stop-time=100us --wave=tb_foo_0.ghw
```

**待本地验证**：若本机装了 GHDL，可实际执行该命令（需要一个已编译进 `mylib/v08` 的 `tb_foo`），观察 `--stop-time=100 us`（带空格）会报错而 `100us` 正常。

#### 4.3.5 小练习与答案

**练习 1**：为什么 GHDL 分支用 `string map {" " ""} $timeLimit` 去掉空格，而 Modelsim 分支的 `run $timeLimit` 直接用带空格的值？

**参考答案**：因为两边的命令行解析方式不同。Modelsim 的 `run` 是内建命令，`run 100 us` 把 `100 us` 当作一个时间字面量，能正确解析。而 GHDL 是外部进程，命令要经过 argv 解析，`--stop-time=100 us` 里的空格会被切分成 `--stop-time=100` 和 `us` 两个独立参数，导致 GHDL 报错。所以必须用 `string map` 把 `100 us` 压成 `100us` 紧贴在等号后。同一种「时长」语义，在两种仿真器下需要不同的格式化。

**练习 2**：如果想在 GHDL 回归测试里也产出波形，能直接靠 `run_tb` 做到吗？

**参考答案**：不能。`run_tb` 调 `sal_run_tb` 时只传 5 个参数（L826），`wave` 取默认空串，所以 GHDL 分支的 `--wave` 段不会出现。波形只在 `launch_tb`（调试路径）下、通过第 6 个参数传入才会生效。这是 PsiSim 有意的设计：回归测试只关心 pass/fail（靠 transcript 判错），不需要波形；波形是交互调试才需要的。

---

### 4.4 Vivado 分支：xelab/xsim、initfile workaround 与 -generic_top 转换

#### 4.4.1 概念说明

Vivado Simulator（`xsim`）是 Xilinx Vivado 自带的仿真器，在 PsiSim 里支持最弱（[u1-l1](u1-l1-project-overview.md) 提过它 VHDL-2008 支持差、仅作备选）。它的仿真分**两步**：先用 `xelab` 把设计 elaborate 成一个「快照（snapshot）」，再用 `xsim` 运行这个快照。这与 Modelsim 的 `vsim` 一步到位、GHDL 的 `--elab-run` 一步到位都不同。

Vivado 分支是本讲最复杂的一段，有三个独有设计：

1. **initfile workaround**：因为 `xelab` 的 `--lib` 开关不能正确映射库→目录，PsiSim 改用一个 ini 文件显式列出所有库的映射；
2. **`-g` → `-generic_top` 转换**：Vivado 不认 Modelsim/GHDL 的 `-g` 语法，必须把每个 `-gName=Value` 改写成 `-generic_top Name=Value`；
3. **用 TCL 批处理文件驱动 xsim**：把 `run` 命令写进一个 `psi_sim_run.tcl` 文件，再用 `xsim -tclbatch` 执行它，输出重定向到一个 txt 文件后读回日志。

#### 4.4.2 核心流程

Vivado 分支分六步：

1. **写 initfile**（workaround）：新建 `psi_vivado_init.ini`，对 `Libraries` 里每个库写一行 `lib=lib`（库名映射到同名目录）。
2. **扫描 generic 覆盖**：遍历 `tbArgs`，对每个匹配 `-g*` 的参数，用 `string range $param 2 end` 砍掉前两个字符 `-g`，包成 `-generic_top Name=Value` 收集起来，最后 `join` 成一个串。
3. **elaborate（xelab）**：拼 `xelab --initfile $initFile -s psi_sim_snapshot -debug typical [$genericOverrides] $lib.$tbName`，用 `eval "exec $cmd"` 执行，生成快照 `psi_sim_snapshot`。
4. **写仿真 TCL 文件**：新建 `psi_sim_run.tcl`，写一行 `run $timeLimit > psi_sim_output.txt;`（或 `run -all > ...`）和一行 `exit`。
5. **运行快照（xsim）**：`xsim psi_sim_snapshot -tclbatch psi_sim_run.tcl`，用 `eval "exec $cmd"` 执行。
6. **回读输出**：打开 `psi_sim_output.txt`，把内容 `sal_print_log` 进 transcript。

#### 4.4.3 源码精读

**第 1 步：initfile workaround（L259–L267）**

[PsiSim.tcl:L259-L267](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L259-L267) —— 写 `psi_vivado_init.ini`：`file delete` 先删旧文件，再 `open w+` 新建，对 `Libraries` 里每个 `$lib` 写一行 `puts $fo "$lib=$lib\n"`。

注释 L259 直接点明动机：`workaround because --lib switch of xelab does not work`。正常情况下 `xelab` 应该能用 `--lib <lib>=<dir>` 开关把库名映射到目录，但这个开关失效了，所以 PsiSim 改用一个 ini 文件。文件内容形如（假设有两个库 `mylib`、`psi_tb`）：

```
mylib=mylib

psi_tb=psi_tb

```

（`puts` 自带一个换行，加上格式串里的 `\n` 又一个，所以每行后面有一个空行。）每一行把库名映射到**同名目录**——这与 [u3-l3 编译阶段](u3-l3-sal-compile.md) Vivado 分支用的 `--work $lib=$lib`（L202，把库 `$lib` 编进同名目录）严格对应。编译把库放进 `$lib/` 目录，仿真用 ini 文件告诉 `xelab` 去那个目录找库。

注意这里 `variable Libraries`（L260）把命名空间状态变量链接进来——Vivado 分支需要知道**所有**库（不只是当前测试台的库），因为 elaborate 要链接整个设计可能依赖的全部库。

**第 2 步：generic 转换（L268–L275）**

[PsiSim.tcl:L268-L275](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L268-L275) —— 遍历 `tbArgs`，对每个 `-g*` 参数转换成 `-generic_top`。

逐步拆解 `-gClockRatio_g=3` 的转换：

1. `foreach param $tbArgs`：`tbArgs` 是一个字符串（如 `"-gClockRatio_g=3"` 或 `"-gA=1 -gB=2"`），`foreach` 把它当列表遍历。单 generic 时得到一个元素 `-gClockRatio_g=3`；多 generic（带空格）时得到多个元素。
2. `[string match "-g*" $param]`：检查是否以 `-g` 开头。`-gClockRatio_g=3` 匹配。
3. `[string range $param 2 end]`：取从下标 2 到末尾的子串。`-gClockRatio_g=3` 的下标 0 是 `-`、下标 1 是 `g`、下标 2 起是 `ClockRatio_g=3`，所以结果是 `ClockRatio_g=3`——正好砍掉了 `-g` 两个字符。
4. `lappend genericOverrides "-generic_top ClockRatio_g=3"`：包成 Vivado 语法。
5. `join $genericOverrides " "`：把多个 generic 用空格连成一个串。

对 `tbArgs = "-gA=1 -gB=2"`，结果 是 `-generic_top A=1 -generic_top B=2`。

这是本讲最核心的「同一意图、不同说法」的实例：Modelsim/GHDL 用 `-gName=Value`，Vivado 用 `-generic_top Name=Value`，SAL 用两个字符的 `string range` 把前者改写成后者。

**第 3 步：xelab elaborate（L276–L283）**

[PsiSim.tcl:L276-L283](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L276-L283) —— 拼 `xelab` 命令并执行。

| 片段 | 作用 |
|------|------|
| `xelab` | Vivado 的 elaborate 命令 |
| `--initfile $initFile` | 库映射文件（上面的 workaround） |
| `-s psi_sim_snapshot` | 快照名（固定为 `psi_sim_snapshot`） |
| `-debug typical` | 调试级别（典型） |
| `$genericOverrides` | generic 覆盖（如 `-generic_top ClockRatio_g=3`），为空则不拼 |
| `$lib.$tbName` | 库限定的顶层实体 |

注意 L278–L280 的 `if {$genericOverrides != ""}`：只有存在 generic 覆盖时才把它拼进命令，避免空串污染。最后 L283 的 `eval "exec $cmd"` 执行 elaborate，生成名为 `psi_sim_snapshot` 的快照。

**第 4 步：写仿真 TCL 文件（L284–L295）**

[PsiSim.tcl:L284-L295](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L284-L295) —— 新建 `psi_sim_run.tcl`，根据 `timeLimit` 写一行 `run`（输出重定向到 `psi_sim_output.txt`）和一行 `exit`。

带时间限制（`timeLimit = "10 us"`）时，`psi_sim_run.tcl` 内容为：

```
run 10 us > psi_sim_output.txt;
exit
```

无时间限制（`timeLimit = "None"`）时，内容为：

```
run -all > psi_sim_output.txt;
exit
```

注意 Vivado 的 `run` 命令（xsim 内建）**接受带空格的时长**`10 us`，与 Modelsim 一样、与 GHDL 不同——因为这里是写在 xsim 的 TCL 批处理文件里由 xsim 自己解析，不经过 argv 切分。

**第 5 步：xsim 运行（L296–L298）**

[PsiSim.tcl:L296-L298](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L296-L298) —— `xsim psi_sim_snapshot -tclbatch psi_sim_run.tcl`：加载第 3 步生成的快照，执行第 4 步写的 TCL 批处理文件。`eval "exec $cmd"` 启动仿真，`run` 的输出被重定向进 `psi_sim_output.txt`。

**第 6 步：回读输出（L299–L302）**

[PsiSim.tcl:L299-L302](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L299-L302) —— 打开 `psi_sim_output.txt`，把全部内容 `sal_print_log` 进 transcript。

这一步揭示了 Vivado 分支把仿真输出送进 transcript 的机制：**`run > txt` 重定向 → 手动读回 → `sal_print_log`**。对比 Modelsim 靠内建 transcript 自动落盘、GHDL 靠 `exec` 捕获 stdout，Vivado 是第三种机制——因为它通过一个 TCL 批处理文件间接驱动 xsim，输出不会自动进 PsiSim 的 transcript，必须借 `psi_sim_output.txt` 中转。三种仿真器、三种机制，但最终都把输出汇入同一个 `Transcript.transcript`，让 `run_check_errors` 能统一判错。

#### 4.4.4 代码实践

**实践目标**：手工推演 `-g` → `-generic_top` 的转换，确认 `string range $param 2 end` 砍掉的正是 `-g`。

**操作步骤**（示例代码，不是 PsiSim 原有代码）：

1. 在任意 TCL 解释器里跑下面这段，模拟 L268–L275 的逻辑。

```tcl
# 示例代码：复现 Vivado generic 转换
set tbArgs "-gClockRatio_g=3"
set genericOverrides ""
foreach param $tbArgs {
    if {[string match "-g*" $param]} {
        lappend genericOverrides "-generic_top [string range $param 2 end]"
    }
}
set genericOverrides [join $genericOverrides " "]
puts $genericOverrides
```

2. 再用 `set tbArgs "-gA=1 -gB=2"` 跑一遍，看多 generic 的结果。
3. 想一想：如果某个参数不以 `-g` 开头（比如未来想传别的开关），会发生什么？

**需要观察的现象**：单 generic 输出 `-generic_top ClockRatio_g=3`；多 generic 输出 `-generic_top A=1 -generic_top B=2`；非 `-g` 参数被静默忽略（不进 `genericOverrides`）。

**预期结果**：

```
-generic_top ClockRatio_g=3
```

**待本地验证**：若本机装了 Vivado，可手工跑一遍 `xelab ... -generic_top ClockRatio_g=3 mylib.tb_foo`，观察 generic 是否真的被覆盖（在测试台里 `report` 该 generic 的值）。

#### 4.4.5 小练习与答案

**练习 1**：`string range $param 2 end` 为什么从下标 2 开始，而不是 1？

**参考答案**：因为要砍掉的是 `-g` 这**两个**字符。`-gClockRatio_g=3` 的下标 0 是 `-`、下标 1 是 `g`，从下标 2 开始到末尾正好是 `ClockRatio_g=3`，去掉了 `-g` 前缀。如果从下标 1 开始，会留下一个 `g`，变成 `gClockRatio_g=3`，`xelab` 就找不到名为 `gClockRatio_g` 的 generic 了。TCL 的 `string range` 下标从 0 计起，所以「砍掉前两个字符」=「从下标 2 取到 end」。

**练习 2**：Vivado 分支为什么需要 `psi_vivado_init.ini`，能不能省掉？

**参考答案**：不能省，因为 `xelab` 的 `--lib` 开关（本应负责把库名映射到目录）失效了，源码注释 L259 明说这是 workaround。如果不写 ini 文件，`xelab` 就找不到编译阶段产出的库（它们在与库同名的目录里，见 [u3-l3](u3-l3-sal-compile.md) 的 `--work $lib=$lib`），elaborate 会失败。所以 PsiSim 用 ini 文件显式列出 `lib=lib` 的映射，交给 `xelab --initfile` 读取。这是「工具的开关坏了，用配置文件绕过」的典型补丁。

---

## 5. 综合实践

本任务把本讲三个仿真器分支串起来，是规格里指定的核心实践。

**任务**：假设库 `mylib` 里有一个测试台 `tb_clkgen`，它有一个 generic `ClockRatio_g`。用户这样定义了这个 run：

```tcl
create_tb_run tb_clkgen mylib
tb_run_add_arguments "-gClockRatio_g=3"
tb_run_add_time_limit "100 us"
add_tb_run
```

并且项目里还有另一个库 `psi_tb`（即 `Libraries = {mylib psi_tb}`），`RunSuppress` 为空串。请完成下面三件事。

### 第 1 步：写出 Vivado 分支把 `-g` 转成 `-generic_top` 的结果

参照 4.4.3 第 2 步的源码（L268–L275），逐步推演 `tbArgs = "-gClockRatio_g=3"` 的转换。

参考答案：

- `foreach param $tbArgs` → `param = "-gClockRatio_g=3"`；
- `[string match "-g*" $param]` → 真；
- `[string range $param 2 end]` → `"ClockRatio_g=3"`；
- `lappend genericOverrides "-generic_top ClockRatio_g=3"`；
- `join` → `genericOverrides = "-generic_top ClockRatio_g=3"`。

### 第 2 步：写出 initfile、xelab 命令、psi_sim_run.tcl 与 xsim 命令的内容

参照 4.4.3，按六步流程写出 Vivado 分支实际产生的全部文件与命令。

参考答案：

**`psi_vivado_init.ini` 内容**（因为 `Libraries = {mylib psi_tb}`）：

```
mylib=mylib

psi_tb=psi_tb

```

**xelab 命令**（elaborate，含 generic 转换）：

```
xelab --initfile psi_vivado_init.ini -s psi_sim_snapshot -debug typical -generic_top ClockRatio_g=3 mylib.tb_clkgen
```

**`psi_sim_run.tcl` 内容**（因为 `timeLimit = "100 us"`，非 `"None"`）：

```
run 100 us > psi_sim_output.txt;
exit
```

**xsim 命令**（运行快照）：

```
xsim psi_sim_snapshot -tclbatch psi_sim_run.tcl
```

仿真结束后，`psi_sim_output.txt` 的内容会被读回并写进 `Transcript.transcript`。

### 第 3 步：对比三种仿真器下同一 run 的命令差异

把 `tb_clkgen` / `-gClockRatio_g=3` / `100 us` 在三种仿真器下的关键命令填进下表（自检）。

| 维度 | Modelsim | GHDL | Vivado |
|------|----------|------|--------|
| 调用方式 | 内建命令，`eval $cmd` | `eval "exec $cmd"` | 两次 `eval "exec $cmd"`（xelab + xsim） |
| generic 语法 | `-gClockRatio_g=3`（原样） | `-gClockRatio_g=3`（原样） | `-generic_top ClockRatio_g=3`（转换） |
| 时间限制格式 | `run 100 us`（带空格） | `--stop-time=100us`（去空格） | `run 100 us`（写在 tcl 文件里，带空格） |
| 库定位 | Modelsim 库管理 | `--workdir=mylib/v08` | `--initfile psi_vivado_init.ini` |
| 消息抑制 | `+nowarn`（本例 RunSuppress 空，故无） | 忽略 | 忽略 |
| 输出进 transcript | 自动（transcript file） | `exec` 捕获后 `sal_print_log` | `run > txt` 重定向后读回 |
| 步数 | 1 步（vsim+run） | 1 步（--elab-run） | 2 步（xelab 然后 xsim） |

**预期结果**：你能不查源码地说出每个格子的来源，并能指出三条结论——(a) 只有 Vivado 需要转换 generic 语法；(b) 只有 GHDL 的时间限制要去空格；(c) 三种仿真器用三种不同机制把输出送进同一个 transcript。

**待本地验证**：若有任一仿真器环境，可实际用最小 `config.tcl` + `run.tcl`（见 [u1-l3](u1-l3-two-file-workflow.md)）跑一次这个 run，对照 transcript 里 `sal_print_log` 打印的命令（GHDL/Vivado 分支会打印 `$cmd`）与你的推演是否一致。

## 6. 本讲小结

- `sal_run_tb` 是 SAL 的统一仿真入口，签名 `(lib tbName tbArgs timeLimit suppressMsgNo {wave ""})`；第 6 个参数 `wave` 带默认值空串，因为它要同时服务回归路径（`run_tb`，5 参数，不要波形）和调试路径（`launch_tb` 的 GHDL 分支，6 参数，带波形）。
- **Modelsim 分支**直接调内建 `vsim -quiet -t 1ps -msgmode both +nowarn... lib.tb tbArgs`，再设两个 std 警告开关，按 `timeLimit` 走 `run $timeLimit` 或 `run -all`，最后 `quit -sim`；`tbArgs`（`-gName=Value`）原样透传，因为 Modelsim 原生支持 `-g`。
- **GHDL 分支**用 `ghdl --elab-run --workdir=$lib/v08 ...`，永远从 2008 库产物启动（与编译阶段首尾呼应）；时间限制用 `string map {" " ""}` 去空格变成 `--stop-time=100us`；`wave` 参数只在这一个分支被消费（拼成 `--wave=file.ghw`）；输出靠 `exec` 捕获后 `sal_print_log`。
- **Vivado 分支**最复杂：先写 `psi_vivado_init.ini`（`xelab --lib` 失效的 workaround），再把每个 `-gName=Value` 用 `string range $param 2 end` 转成 `-generic_top Name=Value`，然后 `xelab` 生成快照、写 `psi_sim_run.tcl`、`xsim -tclbatch` 运行，最后读回 `psi_sim_output.txt`。
- 一个贯穿全讲的结论：**同一个 `timeLimit`（如 `100 us`）在三种仿真器下被格式化得不一样**——Modelsim/xsim 接受带空格（内建命令解析），GHDL 必须去空格（argv 解析）；**同一个 generic 覆盖意图有三种说法**——Modelsim/GHDL 用 `-g`，Vivado 用 `-generic_top`。
- 另一个贯穿结论：**三种仿真器用三种不同机制把仿真输出汇入同一个 `Transcript.transcript`**（Modelsim 自动、GHDL exec 捕获、Vivado 文件中转），这是 `run_check_errors` 能用一套正则统一判错的前提。

## 7. 下一步学习建议

本讲讲完了「仿真运行」的 SAL 抽象，至此单元 3 的核心 `sal_*` 过程（编译、运行）都已打开。下一篇 [u3-l5（交互调试 launch_tb / 波形）](u3-l5-launch-tb-debug.md) 会专门讲调试路径——看 `launch_tb` 如何启动（但不执行）一个 TB，以及 Modelsim 的 `sal_launch_tb`（`do`/`add wave`）与 GHDL 的 `.ghw` + `sal_open_wave`（GTKWave）两条调试工作流的差异。阅读建议：

- 继续盯住 `PsiSim.tcl` 的 `launch_tb`（L852–L964）、`sal_launch_tb`（L309–L333）、`sal_open_wave`（L335–L342）。你会发现本讲讲的 `sal_run_tb` 的 `wave` 参数，在 `launch_tb` 里被进一步组装成 `.ghw` 文件名（L948）——本讲的 4.1 与 4.3 已经为它铺好了路。
- 带着一个问题去读下一篇：本讲强调「回归路径不传 `wave`、调试路径才传」，那么 `launch_tb` 是如何做到「启动但不执行」、把控制权交给交互式仿真器的？
- 如果你对「如何给 PsiSim 增加第四种仿真器」感兴趣，可以把本讲三个分支的 `if/elseif` 结构当作模板——尤其注意 generic 语法、时间格式、输出机制三处在三种仿真器下都不一样，新增仿真器时这三处都要补上对应分支。这会为 [u4-l3（扩展新模拟器与架构取舍）](u4-l3-extend-simulator.md) 做好铺垫。

# 仿真运行与脚本钩子（run_tb）

## 1. 本讲目标

学完本讲，读者应该能够：

- 说出 `run_tb` 的三个过滤维度（库、名字、包含子串）如何用「哨兵值 + `continue`」组成 AND 过滤，并解释 `-contains` 其实是子串匹配而非正则。
- 解释为什么 `run_tb` 的 `-all` 要同时重置「库」和「名字」两个维度（而 `compile` 的 `-all` 只重置「库」）。
- 准确描述 pre/post 脚本的执行时机：每个 run 各只跑一次，把「这个 run 的所有参数组仿真」整体包起来，而不是每组参数各跑一次。
- 跟踪一段含多个 run、含 skip、含多组参数的 TbRuns，画出 `sal_run_tb` 被调用的次数与参数，并说明 skip 在 Modelsim / GHDL 两种仿真器下的差异。

## 2. 前置知识

本讲紧承 [u2-l3（TbRuns 数据模型）](u2-l3-tb-run-definition.md)，只关心一件事：`run_tb` 如何**消费** `TbRuns`，把每个 run 真正「跑」出去。回顾 u2-l3 的关键结论：

- `TbRuns` 是一个 list，每个元素是一个 dict（即一个「run」）。
- 每个 run 的关键字段：`TB_NAME`、`TB_LIB`、`TB_ARGS`（参数组列表，默认 `[list ""]`，即「一组空参数」）、`PRESCRIPT_*` / `POSTSCRIPT_*`、`TIME_LIMIT`（默认 `"None"`，表示跑到底）、`SKIP`（默认 `"None"`）。
- 一个 run 触发的仿真次数 = `TB_ARGS` 的长度。
- `tb_run_skip` 写入 `SKIP`，缺省值 `"all"`，大小写敏感，须用精确拼写 `"GHDL"` / `"Vivado"` / `"Modelsim"` / `"all"`。

补充一个本讲要用到的 TCL 小知识：`continue` 在 `foreach` 循环里表示「跳过本轮剩余语句，直接进入下一轮」。`run_tb` 的全部过滤逻辑都靠它实现。另外，`run_tb` 本身不直接调用仿真器，而是把每组参数转交给 SAL 层的 `sal_run_tb`（SAL 的细节留到单元 3，本讲只把它当作「真正启动一次仿真的黑盒」）。

## 3. 本讲源码地图

| 文件 | 关键位置 | 作用 |
|---|---|---|
| PsiSim.tcl | `run_tb` 752–839 行 | 本讲主角，Run 类导出命令，遍历 `TbRuns` 并执行。 |
| PsiSim.tcl | `sal_exec_script` 208–222 行 | pre/post 脚本的实际执行者（cd → exec → cd 回）。 |
| PsiSim.tcl | `sal_run_tb` 224 行起 | SAL 层入口，启动一次仿真；`run_tb` 对每组参数调它一次。 |
| CommandRef.md | `run_tb` 428–464 行、`tb_run_skip` 292–317 行、`tb_run_add_pre_script` 253–286 行 | 官方参数说明与脚本钩子语义。 |

`run_tb` 是导出命令（见 [PsiSim.tcl:839](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L839) 的 `namespace export run_tb`），属于 Run 类命令，是回归测试「执行」阶段的核心。

## 4. 核心概念与源码讲解

### 4.1 run_tb 的过滤与遍历逻辑

#### 4.1.1 概念说明

`run_tb` 要解决的问题是：`TbRuns` 里可能登记了几十个 run，但用户有时只想跑「某一个库的」「名字含 fifo 的」「全部的」。所以 `run_tb` 需要一个过滤机制，先挑出本次要执行的 run 子集，再逐个执行。

它沿用和 `compile`（见 u2-l5）完全相同的套路：每个过滤维度配一个「哨兵值」表示「该维度不过滤」，循环里用 `continue` 跳过不符合的 run。三个维度做 AND：必须同时满足「库」「名字」「包含子串」三个条件，run 才会被执行。

#### 4.1.2 核心流程

```
run_tb 接收参数
  │
  ▼ 解析 -all / -lib / -name / -contains → 设 Library / Name / contains 三个过滤器
  │
  ▼ clean_transcript（清空日志）
  │
  ▼ foreach run in TbRuns:
  │      取 runLib, runName, skip
  │      if (Library 非全) 且 (runLib != Library):        continue   # 库过滤
  │      if (Name 非全)   且 (runName != Name):            continue   # 名字过滤
  │      if (contains 非全) 且 (runName 不含 contains):    continue   # 子串过滤
  │      打印 run 横幅
  │      if skip 命中当前 Simulator:                       continue   # skip（见 4.3）
  │      执行 pre-script（见 4.2）
  │      foreach tbArgs in run.TB_ARGS:                               # 多组参数（见 4.3）
  │          sal_run_tb(runLib, runName, tbArgs, timeLimit, RunSuppress)
  │      执行 post-script（见 4.2）
  │
  ▼ sal_transcript_off
```

#### 4.1.3 源码精读

**参数解析与哨兵初值** [PsiSim.tcl:755-782](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L755-L782)：三个过滤器初值分别是 `"All-Libraries"`、`"All-Names"`、`"All-regex"`，含义都是「不过滤」。注意 `-all` 分支 [PsiSim.tcl:762-764](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L762-L764) 同时把 `Library` 和 `Name` 都重置为「全」。

**过滤三连** [PsiSim.tcl:794-802](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L794-L802)：三段 `if ... continue` 串成 AND。其中 `-contains` 维度的哨兵虽然叫 `"All-regex"`，实现却是 `string first`（子串匹配），并不是正则——和 `compile` 的 `-contains` 行为一致（u2-l5 已点明，这是项目里命名与实现不完全对应的一处）。`-contains` 的匹配对象是 `runName`（即 `TB_NAME`），不是路径。

**一个执行顺序细节**：skip 检查 [PsiSim.tcl:807-810](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L807-L810) 位于三个过滤**之后**、但在打印横幅 [PsiSim.tcl:803-806](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L803-L806) **之后**。也就是说，被 skip 的 run 仍会先打印「Run ...」横幅，紧接着打印「Skipped」，再 `continue`；而被过滤掉的 run 连横幅都不会打印。看控制台时要注意区分「没出现」和「出现了但被跳过」。

**收尾** [PsiSim.tcl:837](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L837)：所有 run 处理完后调用 `sal_transcript_off` 关闭日志。

#### 4.1.4 代码实践

**实践目标**：确认 `-contains` 是子串匹配、`-name` 是精确匹配，并理解 `-all` 同时重置两个维度。

**操作步骤**（源码阅读型，无需仿真器）：

1. 在 PsiSim.tcl 中定位 `run_tb` 的 `-all` 分支（762–764 行）、`-name` 分支（769–772 行）、`-contains` 分支（773–776 行）。
2. 假设 `TbRuns` 里有三个 run，名字分别是 `fifo_sync_tb`、`fifo_async_tb`、`spi_tb`，都在同一个库里。
3. 手工推演下列三句分别会执行哪几个 run：
   - `run_tb -all`
   - `run_tb -contains fifo`
   - `run_tb -name fifo_sync_tb`

**需要观察的现象 / 预期结果**：

- `run_tb -all`：三个都执行（`-all` 把库和名字两个维度都重置为「全」）。
- `run_tb -contains fifo`：执行 `fifo_sync_tb`、`fifo_async_tb`（名字含子串 `fifo`）；`spi_tb` 不执行。
- `run_tb -name fifo_sync_tb`：只执行 `fifo_sync_tb`（`-name` 是 `==` 精确匹配，不是子串）。

**待本地验证**：若有 Modelsim 环境，可在 config.tcl 里登记上述三个 run，分别用 `-contains` 与 `-name` 各跑一次，对照控制台「Run ...」横幅的数量与名字。

#### 4.1.5 小练习与答案

**练习 1**：`run_tb -lib psi_common -contains fifo` 会执行满足什么条件的 run？

**答案**：同时满足「`TB_LIB == psi_common`」**且**「`TB_NAME` 含子串 `fifo`」的 run。两个维度做 AND。

**练习 2**：为什么 `run_tb` 的 `-all` 要同时重置 `Library` 和 `Name` 两个维度，而 `compile` 的 `-all`（[PsiSim.tcl:558-559](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L558-L559)）只重置 `Library`？

**答案**：因为源文件只有「库 / 标签 / 路径」这些维度，没有「名字」这一身份维度；而 run 同时有「库」和「名字」两个独立身份维度。设想一段脚本先 `run_tb -name tbA`、再 `run_tb -all`：如果 `-all` 只重置库、不重置名字，那么残留的 `Name=tbA` 会悄悄把第二次「全部」缩小成「只有 tbA」，造成「明明 `-all` 却只跑了一个」的隐蔽 bug。同时重置两个维度，才能保证 `-all` 真的跑全部。

---

### 4.2 pre/post script 执行时机

#### 4.2.1 概念说明

一个 run 可以挂一个「前置脚本」（pre-script）和一个「后置脚本」（post-script）。典型用途：pre-script 生成测试向量文件、post-script 比对仿真输出与黄金参考。

这里的关键问题是：如果一个 run 带了 N 组参数（要仿真 N 次），pre/post 脚本各跑几次？答案是**各只跑一次**——脚本把「这一整个 run 的所有仿真」整体包起来，而不是每组参数各包一次。这条语义在 `create_tb_run` 的官方说明里写得很清楚（「the scripts are ran only once before/after all simulations with different arguments」），本讲看它如何在 `run_tb` 里落地。

#### 4.2.2 核心流程

```
（过滤通过、skip 未命中后）
取 PRESCRIPT_CMD / PRESCRIPT_PATH / PRESCRIPT_ARGS
if PRESCRIPT_CMD != "":
    sal_exec_script(PATH, CMD, ARGS)          # 只调一次，在所有仿真之前

foreach tbArgs in TB_ARGS:                     # 仿真循环（N 次仿真）
    sal_run_tb(...)

取 POSTSCRIPT_CMD / POSTSCRIPT_PATH / POSTSCRIPT_ARGS
if POSTSCRIPT_CMD != "":
    sal_exec_script(PATH, CMD, ARGS)          # 只调一次，在所有仿真之后
```

注意 pre/post 两个 `sal_exec_script` 调用都在 `foreach tbArgs` 循环**之外**——这就是「只跑一次」的根本来源。

#### 4.2.3 源码精读

**pre-script** [PsiSim.tcl:812-818](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L812-L818)：从 run 取出三个字段，仅当 `PsCmd != ""` 时调用 `sal_exec_script`。它位于仿真循环 [PsiSim.tcl:824-827](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L824-L827) **之前**、循环体之外。

**post-script** [PsiSim.tcl:828-834](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L828-L834)：结构完全对称，位于仿真循环**之后**。读源码小提示：这段的注释写的是 `#Execute pre-script if required`，但代码取的却是 `POSTSCRIPT_*` 字段——是复制粘贴遗留的注释笔误，代码本身正确，读码时「认字段、不认注释」即可。

**`sal_exec_script`** [PsiSim.tcl:208-222](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L208-L222)：先 `cd` 到 `path`（工作目录），打印一行日志，用 TCL 的 `exec` 执行外部命令并把输出喂回 `sal_print_log`，最后 `cd` 回原目录。注意它打印的日志文案固定是 `"Running Pre Script"`（212 行），即使是 post-script 触发的也打这同一行——又一个文字层面的小不对称，不影响逻辑，但看日志时要心里有数。

#### 4.2.4 代码实践

**实践目标**：验证「一个 run 带 2 组参数时，pre/post 脚本各只跑一次、而仿真跑 2 次」。

**操作步骤**（源码阅读型 + 可选实跑）：

1. 阅读一段示例 config（示例代码，仿照 README 中 `tb_run_add_arguments` 多组 generics 的写法）：
   ```tcl
   # 示例代码
   create_tb_run "my_tb"
   tb_run_add_pre_script  "echo" "before"
   tb_run_add_arguments   "-gA=1" "-gA=2"
   tb_run_add_post_script "echo" "after"
   add_tb_run
   ```
2. 在 PsiSim.tcl 中定位 pre-script（812–818）、仿真循环（824–827）、post-script（828–834）三段，确认 pre/post 调用都在仿真循环之外。

**需要观察的现象 / 预期结果**：

- `sal_run_tb` 被调用 **2 次**（`-gA=1` 一次、`-gA=2` 一次）。
- `sal_exec_script` 被调用 **2 次**：一次 pre（在 2 次仿真之前）、一次 post（在 2 次仿真之后）。
- 因为 `sal_exec_script` 的日志文案不区分前后，控制台会出现**两行** `Running Pre Script`——分别对应 pre 与 post。

**待本地验证**：在 Modelsim 里实跑 `run_tb -all`，数 `Running Pre Script` 出现的次数与 `vsim` 启动的次数，验证「脚本 2 次、仿真 2 次」的比例关系。

#### 4.2.5 小练习与答案

**练习 1**：如果一个 run 既没有 pre-script 也没有 post-script，但有 3 组参数，`sal_exec_script` 和 `sal_run_tb` 各被调用几次？

**答案**：`sal_exec_script` **0 次**（`PsCmd == ""` 不触发）；`sal_run_tb` **3 次**。

**练习 2**：为什么 PsiSim 把 pre/post 设计成「每 run 一次」而不是「每组参数一次」？

**答案**：典型用途是「生成输入向量 / 比对输出」，这些动作与具体参数组无关，对整个 run 做一次即可；若每组参数各跑一次，既慢又可能让中间产物互相覆盖。若确实需要「每组参数前后做事」，正确做法是把它拆成多个 run（每个 run 一组参数），而不是寄希望于脚本钩子按参数组触发。

---

### 4.3 skip 与多组参数

#### 4.3.1 概念说明

两个收尾机制：

1. **skip**：某些 TB 在特定仿真器下会崩溃或不受支持（例如 Vivado 对 VHDL-2008 支持差），这时用 `tb_run_skip` 标记，让 `run_tb` 在该仿真器下跳过这个 run。skip 是**运行时**判断——`run_tb` 拿当前的 `Simulator` 状态变量去比对 run 的 `SKIP` 字段。

2. **多组参数**：一个 run 的 `TB_ARGS` 是一个列表，每个元素是一组参数（典型是一串 `-gXxx=Yyy` generics）。`run_tb` 对每组参数各调用一次 `sal_run_tb`，即「一个 run → 多次仿真」。

#### 4.3.2 核心流程

```
# skip 判断（过滤通过、横幅打印之后、pre-script 之前）
if  (lsearch $skip $Simulator 找到)  或  ($skip == "all"):
    打印 "!!! Skipped ..."
    continue                              # 跳过整个 run（含 pre/post 与全部仿真）

# 多组参数
allArgLists = run.TB_ARGS                 # 默认 [list ""]，即 1 组空参数
timeLimit   = run.TIME_LIMIT             # 默认 "None"（跑到 TB 自停）
foreach tbArgs in allArgLists:
    sal_run_tb(runLib, runName, tbArgs, timeLimit, RunSuppress)
```

仿真次数 \(=\) `llength(TB_ARGS)`。默认 `TB_ARGS = [list ""]` 长度为 1，所以「不调 `tb_run_add_arguments`」时默认就是「跑一次、不带额外参数」。

#### 4.3.3 源码精读

**skip 判断** [PsiSim.tcl:807-810](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L807-L810)：`[lsearch $skip $Simulator]` 把 `skip` 当作列表，在其中查找当前仿真器，找到（返回值 \(\ge 0\)）就跳过；另外用 `$skip == "all"` 兜底，处理 `tb_run_skip` 不带参数时写入的 `"all"`（u2-l3 已说明：不传参会静默全跳过）。

读码要点：`lsearch` 默认**大小写敏感**，而 `Simulator` 的取值是精确的 `"Modelsim"` / `"GHDL"` / `"Vivado"`，所以 `SKIP` 字符串必须用这些精确拼写。CommandRef 在 `tb_run_skip` 处给出的多仿真器示例 `tb_run_skip "Vivado Ghdl"`（[CommandRef.md:303](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L303)）里写的是 `"Ghdl"`，由于大写 `"GHDL"` 与之不等，实际**无法**匹配 GHDL——这是一个容易踩的坑，建议写成 `tb_run_skip "Vivado GHDL"`。

**多组参数循环** [PsiSim.tcl:822-827](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L822-L827)：`allArgLists` 取自 `TB_ARGS`、`timeLimit` 取自 `TIME_LIMIT`，对每组 `tbArgs` 调用一次 `sal_run_tb`。

**`sal_run_tb` 的签名** [PsiSim.tcl:224](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L224) 为 `{lib tbName tbArgs timeLimit suppressMsgNo {wave ""}}`。`run_tb` 调用时只传 5 个参数 [PsiSim.tcl:826](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L826)，第 6 个参数 `wave` 用默认空串——这正是 `run_tb`（回归测试，不要波形）与 `launch_tb`（交互调试，要波形）的分水岭。

**与 u2-l4 的衔接**：`RunSuppress` 在这里以参数形式 **push** 给 `sal_run_tb` [PsiSim.tcl:826](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L826)，与 `CompileSuppress` 被 `sal_compile_file` 自己 **pull** 式读取（见 u2-l4、u2-l5）形成风格不对称——但两者效果一致。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：给定一个含 3 个 run 的 `TbRuns`（其中一个 `skip=GHDL`，一个带 2 组 args），跟踪 `run_tb -all` 的执行顺序，画出每次 `sal_run_tb` 的调用次数与参数。

**TbRuns 定义**（示例代码，仿照 README 的 `create_tb_run` / `tb_run_add_arguments` / `tb_run_skip` 用法）：

```tcl
# 示例代码
# Run 1: 跳过 GHDL，默认 1 组空参数
create_tb_run "tbA"
tb_run_skip "GHDL"
add_tb_run

# Run 2: 2 组 generics，不跳过
create_tb_run "tbB"
tb_run_add_arguments "-gClockRatio_g=3" "-gClockRatio_g=1.01"
add_tb_run

# Run 3: 带 pre-script，默认 1 组空参数，不跳过
create_tb_run "tbC"
tb_run_add_pre_script "echo" "gen_vectors"
add_tb_run
```

**操作步骤**：

1. 假设 `RunSuppress = ""`（未调用 `run_suppress`），所有 run 的 `TIME_LIMIT` 为默认 `"None"`，`TB_LIB` 都相同。
2. 分别在 `Simulator = "Modelsim"` 与 `Simulator = "GHDL"` 两种情况下推演 `run_tb -all`，关注：每个 run 的 skip 是否命中、pre/post 是否跑、`sal_run_tb` 调用几次及参数是什么。

**预期结果（Modelsim 下）**：

| 顺序 | run | skip 命中? | pre-script | `sal_run_tb` 调用 | post-script |
|---|---|---|---|---|---|
| 1 | tbA | 否（`lsearch "GHDL" "Modelsim" = -1`） | 无 | 1 次：`tbA, "", "None", ""` | 无 |
| 2 | tbB | 否 | 无 | 2 次：`(tbB, "-gClockRatio_g=3", "None", "")` 与 `(tbB, "-gClockRatio_g=1.01", "None", "")` | 无 |
| 3 | tbC | 否 | 1 次（`echo gen_vectors`） | 1 次：`tbC, "", "None", ""` | 无 |

Modelsim 下 `sal_run_tb` 共调用 **4 次**。

**预期结果（GHDL 下）**：

| 顺序 | run | skip 命中? | 说明 |
|---|---|---|---|
| 1 | tbA | **是**（`lsearch "GHDL" "GHDL" = 0`） | 打印横幅后 `!!! Skipped`，**0 次** `sal_run_tb`，pre/post 均不跑 |
| 2 | tbB | 否 | 2 次 `sal_run_tb` |
| 3 | tbC | 否 | pre-script 1 次 + 1 次 `sal_run_tb` |

GHDL 下 `sal_run_tb` 共调用 **3 次**。两种仿真器相差的恰好是 tbA 被跳过的那一次。

**需要观察的现象**：skip 判断在「过滤通过 → 打印横幅 → skip 检查」这一顺序里，位于 pre-script **之前**，所以被 skip 的 run 连 pre/post 脚本都不会跑——这正是我们想要的：既然这个仿真器跑不了该 TB，就不该触发可能依赖它的脚本。

**待本地验证**：在 Modelsim 与 GHDL 两种环境下各跑一次 `run_tb -all`，对照控制台「Run ...」横幅、「!!! Skipped」行与仿真启动次数。

#### 4.3.5 小练习与答案

**练习 1**：若把上面 Run 1 改成 `tb_run_skip`（不带参数），在 Modelsim 下会发生什么？

**答案**：`SKIP` 变为缺省值 `"all"`，命中 `$skip == "all"` 分支，tbA 在**任何**仿真器下都被跳过——即 u2-l3 强调的「不传参会静默全跳过」陷阱。

**练习 2**：一个 run 的 `TB_ARGS` 默认值是什么？为什么这样设计？

**答案**：默认是 `[list ""]`，即「只含一个空串的列表」，长度为 1（见 [PsiSim.tcl:634](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L634) 的 `dict set ThisTbRun TB_ARGS [list ""]`）。这样 `foreach tbArgs $allArgLists` 至少跑一轮，保证用户即使没调 `tb_run_add_arguments`，TB 也会被仿真一次（用源码里的默认 generics）。

**练习 3**：`run_tb` 调 `sal_run_tb` 时只传了 5 个参数，第 6 个参数 `wave` 为什么不传？

**答案**：`wave` 有默认值空串（见签名 [PsiSim.tcl:224](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L224)）。`run_tb` 做回归测试，不需要波形文件；只有 `launch_tb`（交互调试）才会传 `wave`（GHDL 下生成 `.ghw`）。不传 = 用默认值 = 不出波形。

---

## 5. 综合实践

把本讲三个模块（过滤、pre/post 时机、skip 与多组参数）串成一个最小的回归脚本。

**要求**：

1. 在 config.tcl 里登记 4 个 run（示例代码）：
   ```tcl
   create_tb_run "tb_fifo_sync"
   tb_run_add_arguments "-gDepth_g=32" "-gDepth_g=128" "-gDepth_g=512"
   tb_run_add_post_script "echo" "compare_result"
   add_tb_run

   create_tb_run "tb_fifo_async"
   tb_run_add_arguments "-gDepth_g=32" "-gDepth_g=128"
   tb_run_skip "Vivado"
   add_tb_run

   create_tb_run "tb_spi"
   tb_run_add_pre_script "echo" "gen_vectors"
   add_tb_run

   create_tb_run "tb_debug_only"
   tb_run_skip                # 不带参数 → SKIP="all"
   add_tb_run
   ```
2. 写出对应的 run.tcl（参照 README 的执行脚本）：`source PsiSim.tcl` → `namespace import psi::sim::*` → `init` → `source ./config.tcl` → `compile_files -all -clean` → `run_tb -all` → `run_check_errors "###ERROR###"`。
3. 手工推演在 Modelsim 下 `run_tb -all` 的执行表：每个 run 是否执行、pre/post 是否跑、`sal_run_tb` 调用几次。
4. 再推演 `run_tb -contains fifo`：剩下哪几个 run？分别几次仿真？

**预期（Modelsim 下 `run_tb -all`）**：

| run | 执行? | pre | `sal_run_tb` | post |
|---|---|---|---|---|
| `tb_fifo_sync` | 是 | 无 | 3 次 | 1 次 |
| `tb_fifo_async` | 是（Modelsim 不在 skip 列表） | 无 | 2 次 | 无 |
| `tb_spi` | 是 | 1 次 | 1 次 | 无 |
| `tb_debug_only` | **跳过**（`SKIP="all"`） | 无 | 0 次 | 无 |

合计 `sal_run_tb` **6 次**。

`run_tb -contains fifo`：只剩 `tb_fifo_sync`（3 次）和 `tb_fifo_async`（2 次），共 **5 次**；`tb_spi`、`tb_debug_only` 因名字不含 `fifo` 被过滤掉。

**待本地验证**：在有仿真器的环境里实跑，对照控制台「Run ...」横幅、「!!! Skipped」行、「Running Pre Script」行的数量是否与上表一致。

## 6. 本讲小结

- `run_tb` 用三个哨兵维度（库 / 名字 / 包含子串）加 `continue` 组成 AND 过滤；`-contains` 是 `string first` 子串匹配、作用在 `TB_NAME` 上，不是正则。
- `run_tb` 的 `-all` 同时重置「库」和「名字」两个维度（与 `compile` 只重置库不同），避免上一次调用残留的过滤器悄悄缩小「全部」的范围。
- skip 检查在「过滤通过、横幅打印之后」执行，用 `lsearch $skip $Simulator` 或 `$skip == "all"` 判定；命中则连 pre/post 与全部仿真一起跳过。`lsearch` 大小写敏感，`SKIP` 必须用精确拼写 `"GHDL"` / `"Vivado"` / `"Modelsim"` / `"all"`。
- pre/post 脚本各只跑一次，把整个 run 的所有仿真「包」起来；两个 `sal_exec_script` 调用都位于 `foreach tbArgs` 循环之外，这是「只跑一次」的根本原因。
- 一个 run 的仿真次数 \(=\) `TB_ARGS` 的长度；默认 `[list ""]` 保证至少仿真一次。
- `run_tb` 把 `RunSuppress` 以参数形式 push 给 `sal_run_tb`（与 `CompileSuppress` 的 pull 风格不对称），且不传 `wave`（回归测试不要波形）。

## 7. 下一步学习建议

- `run_tb` 把每组参数交给了 `sal_run_tb`，但后者如何把它翻译成 Modelsim `vsim`、GHDL `--elab-run`、Vivado `xelab + xsim` 的具体命令？这正是单元 3 的主题，建议接着读 **u3-l4（仿真运行抽象 sal_run_tb）**。
- 想了解「带波形的运行」——即 `launch_tb` 如何复用同样的 skip 过滤与 `sal_run_tb`，并加上 GTKWave 调试路径，可读 **u3-l5（交互调试 launch_tb / 波形）**。
- 仿真跑完后如何判定回归通过 / 失败？`run_check_errors` 读取 `run_tb` 一路写入的 transcript 文件并做正则匹配，详见 **u2-l7（错误检查与 transcript）**。

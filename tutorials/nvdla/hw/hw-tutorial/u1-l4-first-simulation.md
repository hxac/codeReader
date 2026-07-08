# 运行第一次仿真：trace-player 与 run_sanity

## 1. 本讲目标

上一讲（u1-l3）我们搞清楚了「分散的 RTL 如何被 tmake 拧成可编译的设计」。本讲往前走一步：**把编译好的设计真正跑起来，看它对一组输入激励会输出什么**——也就是「仿真（simulation）」。

学完本讲，你应当能够：

- 说出 NVDLA 仿真流程的三个动作：**编译（build）→ 运行（run）→ 校验（checktest）**。
- 用 `make build` / `make run TESTDIR=...` 跑通一个最简单的 sanity 测试，并知道结果落在哪个目录。
- 看懂一个 trace（激励序列）文件长什么样，理解它如何「编程」加速器。
- 区分 `DUMP` 与 `DUMPER=VERDI` 两个波形开关，知道怎样打开 Verdi 看波形。
- 读懂 checktest 脚本如何扫描日志、给出 `PASSED` / `FAILED` 结论。
- 理解 `run_sanity` 这个一键脚本在背后做了什么。

本讲只讲「怎么跑、跑完怎么看结果」，**不深入 RTL 内部行为**——那是后续单元的事。

## 2. 前置知识

在动手之前，先用大白话对齐几个概念。

- **仿真（simulation）**：用软件（这里用的是 Synopsys **VCS**）模拟硬件电路在时钟驱动下逐拍的行为。RTL 本身只是「电路描述」，不跑就看不到结果。
- **测试平台（testbench, TB）**：包在被测设计（DUT, Design Under Test）外面的「驱动 + 观察」层。它给 DUT 喂时钟、复位、配置写、输入数据，再把 DUT 的输出和期望值比对。NVDLA 的 TB 在 `verif/synth_tb/` 下。
- **激励（stimulus）/ trace**：一连串「在哪个时刻、向哪个地址、写/读什么值」的指令序列。NVDLA 把激励存成纯文本文件（`input.txn`），TB 读它、翻译成总线事务。一个 trace ≈ 一个测试用例。
- **波形（waveform）**：仿真过程中每个信号随时间变化的记录。出 bug 时，工程师用 **Verdi** / DVE 这类工具打开波形逐拍排查。
- **make / Makefile**：GNU make 是任务编排器；Makefile 里写好「目标: 依赖 + 命令」，敲 `make <目标>` 就执行对应命令。本讲的命令几乎都是 `make xxx`。

承接 u1-l3：tmake 已经把 RTL 编译链准备好了；本讲的 `verif/sim/Makefile` 负责把「RTL + testbench + trace」三者编译成一个可执行仿真镜像 `simv`，再运行它。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `verif/sim/Makefile` | 仿真主控脚本：定义 build / run / check / regress / verdi 等 target，是本讲的「指挥中心」。 |
| `verif/traces/README.md` | trace 目录的极简使用说明（怎么跑、结果在哪）。 |
| `verif/traces/traceplayer/sanity0/input.txn` | 一个最简 trace 样例：对 BDMA 寄存器做一次写-读回环。 |
| `verif/traces/traceplayer/sanity0/plusargs.txt` | 该 trace 传给仿真的额外仿真参数。 |
| `verif/sim/checktest.pl` | 日志校验脚本（sv_tb / UVM 路径用）：扫 `UVM_ERROR` 等、比 dump 文件，输出 PASSED/FAILED。 |
| `verif/synth_tb/sim_scripts/checktest_synthtb.pl` | 日志校验脚本（synth_tb 默认路径实际调用）：扫含 `ERROR` 的行、比 `.raw2` dump 文件。 |
| `tools/bin/run_sanity` | 一键脚本：从模板生成 `tree.make`、跑 `tmake`、汇总 `build.log` 里的 checktest 结论。 |
| `verif/sim/checkcompile.pl` | 编译日志校验：`make build` 后判断编译是否真的成功（本讲略读）。 |

> 提醒：本仓库里同时存在两个 checktest 脚本。`make run` 默认走 `synth_tb` 测试平台，**实际调用的是 `checktest_synthtb.pl`**；而 `verif/sim/checktest.pl` 是给 sv_tb / UVM 流程用的「同款」校验器。两者逻辑相近、输出格式一致（都打印 `checktest : PASSED/FAILED : ...`）。本讲以 `checktest.pl` 为主讲清楚校验原理，并指出默认流程用的是哪一个，避免你照着源码找不到对应行为。

## 4. 核心概念与源码讲解

### 4.1 verif/sim/Makefile：仿真指挥中心

#### 4.1.1 概念说明

`verif/sim/Makefile` 是验证侧的「中央调度」。它把三件事缝在一起：

1. **编译（build）**：调用 VCS，把 RTL（`dut.f` 列出的设计文件）和 testbench（`tb_top.v` 及一批 `csb_master.v` / `axi_slave.v` / `memory.v` …）编进可执行镜像 `simv`。
2. **运行（run）**：把指定 trace 目录拷进工作区、生成存储配置、把 `input.txn` 转成仿真输入、启动 `simv` 跑起来。
3. **校验（checktest）**：跑完后调用 Perl 脚本扫日志，给出 PASSED/FAILED。

文件顶部用注释直接写好了「标准用法」，这是最权威的速查表。

#### 4.1.2 核心流程

一次最简单的单测试仿真，流程是：

```text
cd verif/sim
make build                                  # 1. VCS 编译 → 生成 ./simv（并 checkcompile）
make run TESTDIR=../traces/traceplayer/sanity0   # 2. 准备工作区 + 跑 simv + checktest
# 结果落在 verif/sim/sanity0/ 下的日志里
make check TESTDIR=../traces/traceplayer/sanity0 # 3. （可选）重新只跑校验
```

关键变量取值（默认情况下）：

- 测试平台 `TB_TARGET ?= synth_tb`（默认）。
- 默认测试 `TESTDIR ?= ../traces/traceplayer/sanity0`。
- 结果目录 `SIMTESTDIR := ${SIMDIR}/${TESTNAME}`，即 `verif/sim/sanity0/`。
- 日志名 `TESTLOG := test.log`。

`make run` 内部依次做：拷 trace 到工作区 → 生成 slave_mem 配置 → 把 `input.txn` 转 hexdump → 启动 `simv`（带 `+input_file`/`+input_dir` 等参数）→ 调 checktest。

#### 4.1.3 源码精读

文件头注释就是官方速查表，列出了 clean / build / run / 带波形调试的完整命令：

[verif/sim/Makefile:33-43](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L33-L43) —— 顶部注释，说明 `make clean` / `make build` / `make run TESTDIR=...` 以及 `DUMP=1 DUMPER=VERDI` 的波形用法。

默认测试目录与结果目录的推导（`TESTNAME` 取 `TESTDIR` 末尾一段，于是 `sanity0` 成为结果目录名）：

[verif/sim/Makefile:145-149](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L145-L149) —— `TESTDIR ?= ../traces/traceplayer/sanity0`、`SIMTESTDIR := ${SIMDIR}/${TESTNAME}${TESTSUFFIX}`。

`run` target 是仿真的真正入口。它先做 `TB_PREP`（建目录、拷 trace、软链 `simv`），再生成配置与输入，最后启动 `simv` 并立即调 checktest：

[verif/sim/Makefile:466-479](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L466-L479) —— `run` 目标：`${TB_PREP}` → 生成 `slave_mem.cfg` → `inp_txn_to_hexdump.pl` 转 input → `${VCS_EXECUTABLE} ... +input_file=... +input_dir=...` → `${CHECKTEST} ./$(TESTLOG)`。

注意 `run` 依赖 `${COMPILELOG} ${VCS_EXECUTABLE}`，所以**就算你只敲 `make run`，若 `simv` 不存在也会自动触发编译**。编译本身由 `build` target 完成，编完再跑 `checkcompile.pl` 自检：

[verif/sim/Makefile:583-584](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L583-L584) —— `build` 目标：调用 `$(VCS_HOME)/bin/vcs -f dut.f -o simv ...`，再 `./checkcompile.pl` 校验编译日志。

两个常用的「组合 target」很省事：

[verif/sim/Makefile:457-461](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L457-L461) —— `sanity: build run`（编译+跑单个测试）；`all: build regress`（编译+跑整组回归）。

#### 4.1.4 代码实践

**实践目标**：在不真正启动 VCS 的前提下，把 `make run` 背后的命令链读出来。

**操作步骤**：

1. `cd verif/sim`。
2. 用 `make -n run TESTDIR=../traces/traceplayer/sanity0`（`-n` = dry run，只打印命令不执行）观察它会执行哪些命令。
3. 在打印结果里定位 `${VCS_EXECUTABLE}`（即 `./simv`）那一行，确认它带了 `+input_file=.../input.txn` 与 `+input_dir=...` 两个参数。

**需要观察的现象**：dry-run 输出里依次出现 `mkdir`、`cp -Trf`（拷 trace）、`slave_mem.cfg.pl`、`inp_txn_to_hexdump.pl`、`./simv ... +input_file=...`、最后 `checktest_synthtb.pl ./test.log`。

**预期结果**：你能在不依赖 VCS license 的情况下，完整还原「run 一条 trace」的命令序列。

**待本地验证**：是否真能 `make run` 跑通，取决于本机是否装了 VCS（Makefile 里 `VCS_HOME` 等路径见下文 4.4）。本教学环境通常没有，故实际执行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么即使只敲 `make run`（不敲 `make build`），有时候也会先触发编译？

> **答案**：因为 `run` target 声明了依赖 `run: ${COMPILELOG} ${VCS_EXECUTABLE}`。当 `simv` 这个文件不存在时，make 会先去构建 `${VCS_EXECUTABLE}` target（即调用 VCS 编译），编译产物就绪后才执行 run 的命令体。

**练习 2**：`make check` 和 `make run` 末尾调用的 checktest 有何区别？

> **答案**：`make run` 跑完仿真后**自动**调一次 checktest；`make check` 则**不跑仿真**，只对已存在的结果目录重新执行 checktest，适合「仿真早就跑完了，只想再看一眼结论」的场景（见 [verif/sim/Makefile:492-494](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L492-L494)）。

### 4.2 trace 样例：用文本「编程」加速器

#### 4.2.1 概念说明

NVDLA 的测试理念很优雅：**把对硬件的编程序列写成纯文本**，TB 负责把文本翻译成真实的总线事务。这种文本叫 trace，目录在 `verif/traces/traceplayer/`。它的好处是——写测试不用改 RTL、不用重编译，换一个 `.txn` 文件就是换一个测试用例。

一个 trace 目录通常包含两个文件：

- `input.txn`：激励主体，一行一个事务。
- `plusargs.txt`：传给仿真的额外参数（`+xxx=yyy` 形式）。

#### 4.2.2 核心流程

trace 里的事务类型很简单（见 `sanity0/input.txn` 顶部注释）：

```text
load_mem(addr, offset, file)              # 把一个数据文件预加载到存储
write_reg(reg_addr, reg_data, misc_bits)  # 向寄存器写一个值
read_reg(reg_addr, expected, misc_bits)   # 读寄存器，并与期望值比对
```

TB 里的 `csb_master_seq`（在 u7-l2 详解）会逐行读 `input.txn`，把 `write_reg` / `read_reg` 翻译成 CSB 配置总线上的写/读事务，发给 DUT。于是「一份文本」就完成了「给加速器编程 + 校验」。

#### 4.2.3 源码精读

`verif/traces/README.md` 只有几行，却是 trace 使用的官方入口：

[verif/traces/README.md:1-9](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/README.md#L1-L9) —— 说明 `cd verif/sim; make run TESTDIR=<path/to/test>`，并指出结果在 `verif/sim/_test_`（这里的 `_test_` 是「测试名」的占位，实际即 `verif/sim/<testname>/`，例如 `verif/sim/sanity0/`）。

最简单的 `sanity0` 全文如下（它就是「冒烟测试」）：

[verif/traces/traceplayer/sanity0/input.txn:1-5](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/sanity0/input.txn#L1-L5) —— 三条事务：先读 BDMA 的 `CFG_DST_SURF_0`（期望复位值 `0xffffffe0`），再写入哨兵值 `0xf0a5a500`，再读回（期望读回刚写入的 `0xf0a5a500`）。

这其实验证了一条最小链路：**CSB 配置写能到达 BDMA 寄存器、寄存器能存住值、CSB 读能把值读回来**。哨兵值 `0xf0a5a500` 是一个特征明显的位图案（含 `0xa5`），方便人眼一眼认出。

该 trace 还带了一组仿真参数：

[verif/traces/traceplayer/sanity0/plusargs.txt:1](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/sanity0/plusargs.txt#L1) —— `+read_reg_poll_retries=10 +continue_on_fail`：读寄存器最多重试 10 拍；遇到失败也继续跑完（不中途卡死）。这两个参数会被 Makefile 的 `TESTDIR_PLUSARGS_ONE` 读出并附加到 `simv` 命令行。

仓库里还有更多 trace，覆盖更复杂的功能。回归列表 `FUNC_REGRESS_LIST` 给出了「官方推荐跑一遍」的清单：

[verif/sim/Makefile:302-309](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L302-L309) —— 功能回归清单：`sanity0/1/2/3`、`sanity3_cvsram`、`conv_8x8_fc_int16`、`pdp_max_pooling_int16`、`sdp_relu_int16`。

可以看到 trace 的命名很有规律：`sanity*` 是冒烟级、`*_cvsram` 走片上 CVSRAM、`conv_*`/`pdp_*`/`sdp_*` 分别打卷积/池化/激活后处理通路。

#### 4.2.4 代码实践

**实践目标**：看懂 `sanity0` 在验证什么，并尝试「写」一个自己的最小 trace。

**操作步骤**：

1. 打开 `verif/traces/traceplayer/sanity0/input.txn`，对照注释把三条事务的「地址、数据、期望」抄成一张表。
2. 在 `verif/traces/traceplayer/` 下看其它 trace（如 `sanity1`、`sdp_relu_int16`）的 `input.txn` 行数，体会「越复杂的功能、trace 越长」。
3. （可选）复制 `sanity0` 目录成 `sanity0_my`，把哨兵值从 `0xf0a5a500` 改成另一个值，预测读回结果。

**需要观察的现象**：`sanity0` 只有 3 条事务；功能 trace（如 `conv_8x8_fc_int16`）会有大量 `load_mem`（灌权重/特征图）+ `write_reg`（配置各引擎）+ `read_reg`（轮询 done 状态）。

**预期结果**：你能用一句话说清「sanity0 = 写一个 BDMA 寄存器再读回，确认配置通路通」。

**待本地验证**：自建 trace 能否 PASSED，需在有 VCS 的环境里 `make run TESTDIR=../traces/traceplayer/sanity0_my` 验证。

#### 4.2.5 小练习与答案

**练习 1**：`read_reg` 的第三个参数是什么意思？

> **答案**：是「期望读回值」。TB 读完寄存器后会把读回值和它比对，不一致就报错。`sanity0` 第 3 行期望 `0xf0a5a500`，正好是上一行写入的值，于是这条 read 既触发了一次读、又完成了自检。

**练习 2**：为什么 `plusargs.txt` 里要写 `+continue_on_fail`？

> **答案**：默认仿真遇到第一个失败可能就停下来，不利于一次性收集所有错误。`+continue_on_fail` 让仿真「遇到错误也继续」，方便在回归里看到全部失败点，而不是每次只看到第一个。

### 4.3 run_sanity：一键构建 + 汇总

#### 4.3.1 概念说明

`tools/bin/run_sanity` 是更高层的「一键」入口。注意：**它名字里没有 `.pl`，但其实是 Perl 脚本**（首行 `#!/usr/bin/env perl`）。它不直接调 VCS，而是：

1. 从模板 `tools/make/tree.make.vm` 恢复一份全新的 `tree.make`（u1-l3 讲过的全局环境配置）。
2. 调 `tools/bin/tmake -project nv_full`，按 `build.config` 把整个 `nv_full` 工程构建 + 回归一遍（这会顺带在 `verif/sim` 里跑各 trace）。
3. 读汇总日志 `outdir/build.log`，挑出所有 `checktest:...` 行，判断整体是否 PASSED。

适合「我什么都不想管，帮我从干净状态跑一遍 sanity」的场景。

#### 4.3.2 核心流程

```text
tools/bin/depth -abs_tot            # 取仓库根目录绝对路径(TOT)
cp tree.make.vm  TOT/tree.make      # 用模板重置环境配置
tmake -project nv_full              # 全量构建 + 跑 verif 回归，日志写 outdir/build.log
打开 outdir/build.log               # 逐行扫描以 "checktest" 开头的行
按 ":" 切分 → 取 result 字段          # 任一非 PASSED 即整体失败
exit(fail)                          # 用退出码反映成功/失败
```

#### 4.3.3 源码精读

脚本先定位仓库根、拿到模板路径，并强制把 `tree.make` 重置成模板版本（保证从干净环境出发）：

[tools/bin/run_sanity:13-20](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/run_sanity#L13-L20) —— `depth -abs_tot` 取 TOT；把 `tools/make/tree.make.vm` 拷成 `tree.make`；执行 `tools/bin/tmake -project nv_full`。

模板 `tree.make.vm` 本身很短，主要声明要构建哪个工程以及 cpp/java/perl 的路径：

[tools/make/tree.make.vm:9-24](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/tree.make.vm#L9-L24) —— `PROJECTS := nv_full`，以及 `CPP/JAVA/PERL` 工具路径。

随后脚本读 `outdir/build.log`，扫描其中以 `checktest` 开头的行，按 `:` 切分出 `result` 字段，只要有一个不是 `PASSED` 就把 `fail` 置 1：

[tools/bin/run_sanity:22-35](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/run_sanity#L22-L35) —— 逐行匹配 `^checktest`，`split(':')` 得到 `(undef, result, testname, desc)`；`$fail ||= ($result ne "PASSED") ? 1 : 0;`。

这说明 `build.log` 里每条 checktest 结论的格式是：

```text
checktest:<PASSED|FAILED>:<testname>:<描述>
```

最后用退出码上抛整体结论，便于 CI 判定：

[tools/bin/run_sanity:38-44](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/run_sanity#L38-L44) —— 若 `$fail` 为真则打印错误并指向 `build.log`，`exit($fail)`。

#### 4.3.4 代码实践

**实践目标**：在不跑全量构建的前提下，理解 `run_sanity` 的判定依据。

**操作步骤**：

1. 读 [tools/bin/run_sanity:26-34](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/run_sanity#L26-L34)，确认它只认 `build.log` 里 `checktest:` 开头的行。
2. 假设 `build.log` 里有这样三行，预测 `run_sanity` 的退出码：
   ```
   checktest:PASSED:sanity0:...
   checktest:PASSED:sanity1:...
   checktest:FAILED:sanity2:... see ...log for details
   ```
3. 对照 `$fail ||= ...` 这行逻辑验证你的预测。

**需要观察的现象**：只要出现一个 `FAILED`，`$fail` 就被「或」成 1 并保持。

**预期结果**：上面三行 → 退出码为 1（失败）。若三行全 `PASSED` → 退出码 0。

**待本地验证**：真实 `build.log` 的内容取决于本机能否完成全量构建，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`run_sanity` 自己编译 RTL 吗？

> **答案**：不直接编译。它调 `tmake -project nv_full`，由 tmake 按 `build.config` 依赖图去驱动各 sandbox 的 make（其中才真正调 VCS）。`run_sanity` 只负责「准备 tree.make + 触发 tmake + 汇总结论」。

**练习 2**：为什么它要先 `cp tree.make.vm tree.make`？

> **答案**：保证从一个干净、已知良好的环境配置出发，避免上一次实验残留在 `tree.make` 里的脏配置影响本次构建（比如 `PROJECTS` 或工具路径被改过）。这是「可复现构建」的常见做法。

### 4.4 checktest：从日志里给出 PASSED/FAILED

#### 4.4.1 概念说明

仿真跑完会留下一个日志文件（默认 `test.log` / `vcs.log`）。人工去翻几万行日志找错误不现实，于是有 checktest 脚本：**自动扫日志里的错误标志、比对待测输出与黄金输出，最后打印一行结论**。

本仓库有两个版本：

- **`verif/sim/checktest.pl`**：sv_tb / UVM 路径用。扫 `UVM_ERROR` / `UVM_FATAL`，统计「Starting transaction N」来估算进度，比对 `*chiplib_dump.raw` 与 `*chiplib_replay`。还能输出 `RUNNING`（没看到 UVM 结束消息、可能还在跑）。
- **`verif/synth_tb/sim_scripts/checktest_synthtb.pl`**：synth_tb 路径用（即 `make run` 默认调用的那个）。扫含 `ERROR` 的行，比对 `*chiplib_dump.raw2` 与 `*chiplib_replay`。

两者输出同一种格式：`checktest : PASSED : <test> ...` 或 `checktest : FAILED : <test> ...`。下面以 `checktest.pl` 讲清原理。

#### 4.4.2 核心流程

checktest 的判定逻辑可以概括为「四看 + 一比」：

```text
打开日志 → 逐行扫描：
  看 UVM_ERROR        → errors++
  看 UVM_FATAL : N    → fatalerrors += N
  看 "Starting transaction N" → 记录进度（用于估算 RUNNING 百分比）
  看 "Compiler ...; Mon dd hh:mm yyyy" → 记起始时间
  看 "CPU Time:" 后一行 → 记结束时间（算总耗时）
比 dump：对每个 *chiplib_dump.raw 找对应 *chiplib_replay，cmp 逐字节比对
综合判定：
  errors==0 且有 UVM 消息 且 dump 一致 → PASSED
  没有 UVM 消息                           → RUNNING（可能还在跑）
  否则                                    → FAILED
```

#### 4.4.3 源码精读

`make run` 默认（synth_tb）调用的 checktest 由 `CHECKTEST` 变量决定：

[verif/sim/Makefile:215-217](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L215-L217) —— synth_tb 分支里 `CHECKTEST := ${SYNTHTBDIR}/sim_scripts/checktest_synthtb.pl`。对照 sv_tb 分支（[verif/sim/Makefile:173](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L173)）则是 `./checktest.pl`。这就是「默认流程实际用 synthtb 版」的出处。

`checktest.pl` 先解析参数、定位日志文件（若日志不存在直接判 `NOTRUN`）：

[verif/sim/checktest.pl:42-62](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/checktest.pl#L42-L62) —— 第一个参数是测试目录或日志；找不到日志则打印 `checktest : NOTRUN : ... : no log found`。

核心扫描循环：累计 `UVM_ERROR`、`UVM_FATAL`，并记录事务进度与起止时间：

[verif/sim/checktest.pl:100-136](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/checktest.pl#L100-L136) —— 逐行匹配 `UVM_ERROR`、`UVM_FATAL\s*:\s*(\d+)`、`Starting transaction (\d+)`、`Compiler ...` 起始时间、`CPU Time:` 之后的结束时间。

dump 比对：对每个 `*chiplib_dump.raw`（仿真实际吐出的存储快照）找对应 `*chiplib_replay`（黄金期望），用系统 `cmp` 逐字节比：

[verif/sim/checktest.pl:165-186](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/checktest.pl#L165-L186) —— glob 出 `*chiplib_dump.raw`，替换得到 `*chiplib_replay`，`cmp` 比较；不一致则 `dumpdiff_passed=0`。

最终结论分三种（无错误且 dump 一致 → PASSED；没看到 UVM 消息 → RUNNING；否则 FAILED）：

[verif/sim/checktest.pl:191-218](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/checktest.pl#L191-L218) —— 综合判定并 `printf "checktest : PASSED|FAILED|RUNNING : ..."`。

对应的 synth_tb 版逻辑更简：只扫含 `ERROR` 的行、比 `.raw2`，结论只有 PASSED/FAILED/NOTRUN：

[verif/synth_tb/sim_scripts/checktest_synthtb.pl:88-91](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/checktest_synthtb.pl#L88-L91) —— `if ($line =~ m/.*ERROR.*/) { $errors++; }`。

[verif/synth_tb/sim_scripts/checktest_synthtb.pl:174-183](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/checktest_synthtb.pl#L174-L183) —— `errors==0` 且 dump 一致则 PASSED，否则 FAILED。

> 顺带一提：`make run` 跑出来的日志名，sv_tb 用 `test.log`，synth_tb 经 checktest_synthtb.pl 解析后实际读取的是工作区里的 `input.txn.log`（由其参数解析逻辑决定，见 [checktest_synthtb.pl:64-68](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/checktest_synthtb.pl#L64-L68)）。无论读哪个，结论行都长一个样。

#### 4.4.4 代码实践：开启波形调试（DUMP / VERDI）

仿真正常跑（`DUMP=0`）不产波形，速度快。调试时才开波形。Makefile 用两个变量控制：

- `DUMP=1`：开启波形采集。
- `DUMPER=VERDI|DVE|SILOTI`：选波形格式（默认 `DVE`，产 `vcdplus.vpd`；`VERDI` 产 `debussy.fsdb`）。

逻辑在两处：编译期注入 PLI（让 simv 能调波形 API）、运行期加 dump 参数。

[verif/sim/Makefile:105-110](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L105-L110) —— `DUMP ?= 0`、`DUMPER ?= DVE`，以及三种波形文件名（`vcdplus.vpd` / `debussy.fsdb` / `siloti`）。

[verif/sim/Makefile:324-340](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L324-L340) —— `DUMP=1` 时按 `DUMPER` 注入 Verdi/Siloti 的 novas.tab PLI；`DUMP=0` 则加 `+define+NO_DUMPS`。

[verif/sim/Makefile:401-411](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L401-L411) —— 运行期 `DUMP_ARGS`：`DUMPER=VERDI` 时为 `+dump_fsdb +fsdbfile+.../debussy.fsdb`；`DUMPER=DVE` 时为 `+dump_vpd +vpd_dump_name=.../vcdplus.vpd`。

**实践目标**：掌握「带波形跑 + 用 Verdi 打开」的标准三步。

**操作步骤**（命令来自文件头注释 [verif/sim/Makefile:38-42](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L38-L42)）：

```bash
cd verif/sim
make build      DUMP=1 DUMPER=VERDI
make vericom    DUMP=1 DUMPER=VERDI        # 生成 Verdi 需要的信号库
make run        DUMP=1 DUMPER=VERDI TESTDIR=../traces/traceplayer/sanity0
make verdi      DUMP=1 DUMPER=VERDI TESTDIR=../traces/traceplayer/sanity0
```

**需要观察的现象**：run 之后结果目录里多出 `debussy.fsdb`（波形文件）；`make verdi` 会拉起 Verdi 图形界面。

**预期结果**：在 Verdi 里能看到 `top` 层及其下各级信号随时间变化的波形，可定位到 sanity0 写 `0xf0a5a500` 那一拍。

**待本地验证**：需要本机有 VCS + Verdi license，且 `VCS_HOME` / `VERDI_HOME` 路径（[verif/sim/Makefile:48-54](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L48-L54) 指向 `/home/tools/...`）真实存在；本教学环境通常不具备，故实际执行待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：如果 checktest 输出 `checktest : RUNNING : ...`，最可能是什么情况？

> **答案**：`checktest.pl` 没在日志里发现任何 UVM 结束消息（`found_uvm_msgs==0`），说明仿真可能还没跑完（比如你边跑边 check）。它会用「已处理的 transaction 数 / input.txn 总行数」估算完成百分比。等仿真真正结束后再 check，就会变成 PASSED/FAILED。

**练习 2**：checktest 除了看错误日志，还做了哪一种「数据级」校验？为什么需要它？

> **答案**：它还把仿真实际吐出的存储快照 `*chiplib_dump.raw` 与黄金期望 `*chiplib_replay` 用 `cmp` 逐字节比对。仅看「有没有 ERROR 行」不够——有些错误不会打印 ERROR（比如某段存储内容和期望不符但仿真自身没报错）。逐字节比对能兜住这类「静默错误」，是功能正确性的硬校验。

## 5. 综合实践

把本讲四块知识串起来，完成一次「伪端到端」的仿真梳理（不要求真有 VCS，重在把流程讲清楚）。

**任务**：以 `sanity0` 为对象，回答下列问题，并把答案整理成一张「仿真事实卡片」。

1. **跑哪条命令**能编译 + 运行 `sanity0`？写出最短的一行（提示：组合 target）。
2. 运行后，**结果目录**的绝对路径长什么样（基于 `SIMDIR`/`TESTNAME` 推导）？
3. `sanity0` 这条 trace **到底验证了什么**（地址、写入值、期望读回值）？
4. 默认 `make run` 末尾调用的 checktest **是哪一个脚本**？它判 PASSED 的两个条件是什么？
5. 若想**带波形**复跑并用 Verdi 打开，需要哪四个 `make` 命令？

**参考答案要点**：

1. `cd verif/sim && make sanity TESTDIR=../traces/traceplayer/sanity0`（`sanity: build run`）。或分两步 `make build` + `make run TESTDIR=...`。
2. `verif/sim/sanity0/`（即 README 里的 `verif/sim/_test_` 占位）。
3. 对 BDMA 的 `CFG_DST_SURF_0` 寄存器：先读确认复位值 `0xffffffe0`，写入哨兵值 `0xf0a5a500`，再读回期望 `0xf0a5a500`——验证 CSB↔BDMA 寄存器读通路。
4. 默认是 `verif/synth_tb/sim_scripts/checktest_synthtb.pl`。PASSED 条件：日志中无 `ERROR` 行，且 `*chiplib_dump.raw2` 与 `*chiplib_replay` 逐字节一致。
5. `make build DUMP=1 DUMPER=VERDI` → `make vericom DUMP=1 DUMPER=VERDI` → `make run DUMP=1 DUMPER=VERDI TESTDIR=...` → `make verdi DUMP=1 DUMPER=VERDI TESTDIR=...`。

> 若你本地确有 VCS/Verdi 环境，强烈建议真正执行第 5 步并在 Verdi 中定位到「写入 `0xf0a5a500`」那一拍；否则本任务以「读懂流程」为准。

## 6. 本讲小结

- NVDLA 的标准仿真三步：**`make build`（VCS 编译出 `simv`）→ `make run TESTDIR=...`（跑 trace）→ checktest（校验）**；`make sanity` 是前两步的快捷组合。
- **trace 是纯文本激励**（`input.txn` + `plusargs.txt`）；TB 的 `csb_master_seq` 把每行 `write_reg`/`read_reg` 翻译成 CSB 总线事务。`sanity0` 用写-读回验证了 BDMA 寄存器配置通路。
- 默认 `make run`（`TB_TARGET=synth_tb`）实际调用的是 **`checktest_synthtb.pl`**；`verif/sim/checktest.pl` 是 sv_tb/UVM 版同款校验器，二者都打印 `checktest : PASSED/FAILED : ...`。
- checktest 的判定 = **日志无错误 + dump 与 golden 逐字节一致**；`checktest.pl` 还能区分 `RUNNING`（仿真未结束）。
- **`DUMP=1 DUMPER=VERDI`** 开波形（产 `debussy.fsdb`），`make verdi` 打开；不开则 `+define+NO_DUMPS`，跑得快。
- **`tools/bin/run_sanity`**（实为 Perl）是更高层一键入口：重置 `tree.make` → `tmake -project nv_full` → 扫 `outdir/build.log` 里的 `checktest:` 行汇总成败。

## 7. 下一步学习建议

本讲让你能「跑起来并看结果」，但有意跳过了两块内部细节：

- **TB 内部如何把 trace 翻译成 CSB 事务**：`csb_master.v` / `csb_master_seq.v` / 各类 fifo 如何工作 → 留给 **u7-l1（trace-player 测试平台架构）** 与 **u7-l2（CSB 激励序列与 trace 格式）**。
- **CSB 总线协议本身、寄存器如何组织**：进入 **单元 2（配置空间与寄存器子系统）**，从 **u2-l1（CSB 总线协议与 apb2csb 桥）** 开始。

如果你想趁热打铁，建议先读一遍 [verif/synth_tb/tb_top.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/tb_top.v)，对照本讲的 `TB_FILES` 列表，看看 DUT 和 `csb_master`/`axi_slave`/`memory` 是怎么连起来的——这正是 u7-l1 的主题。

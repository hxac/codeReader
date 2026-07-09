# 运行第一次仿真：trace-player 与 run_sanity

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `verif/sim/Makefile` 提供的 `make build` / `make run TESTDIR=...` 两步走通第一个 sanity 仿真；
- 说清楚一条 trace（激励）从文本文件到 DUT 内部 CSB 寄存器写的过程，理解「trace-player」这个名字的由来；
- 用 `tools/bin/run_sanity` 一键构建整个工程并汇总回归结果；
- 读懂 `checktest` 脚本输出的 `PASSED / FAILED / RUNNING / NOTRUN`，并知道去哪个目录找日志。

## 2. 前置知识

承接 u1-l3：你已经知道 NVDLA 用「顶层 Makefile + tmake + 每 sandbox 共享 Makefile + 三个生成器」把分散的 RTL 拧成可仿真模型。本讲把镜头推到 `verif/sim/` 这个 sandbox：它本身不生产 RTL，而是把已经生成好的 RTL 编译成 VCS 可执行文件 `simv`，再用一份「激励脚本」驱动 `simv` 跑起来。

需要先建立的几个概念：

- **DUT（Design Under Test，被测设计）**：这里就是 NVDLA 的 RTL 顶层 `NV_nvdla`。
- **Testbench（测试平台）**：包围 DUT 的外围代码，负责产生时钟/复位、喂激励、收结果。NVDLA 默认用 `verif/synth_tb/` 下的 `synth_tb`（可综合风格测试平台）。
- **激励（stimulus / trace）**：一段「先写哪个寄存器、写什么值、再读哪个寄存器、期望读到什么」的有序事务序列。NVDLA 把激励存成文本文件 `input.txn`。
- **trace-player**：把 `input.txn` 逐条「回放」成真实总线事务的机制——像播放器播放录像一样，所以叫 player。
- **CSB**：NVDLA 内部配置总线（u2-l1 详讲）。trace 里绝大多数事务就是一次 CSB 寄存器读写。
- **VCS**：Synopsys 的商业 SystemVerilog 仿真器，本仓库默认用它（`VCS_HOME` 指向 `mx-2015.09-SP2`）。
- **sandbox / TOT / tmake**：见 u1-l3，`verif/sim` 是 build.config 里的一个 sandbox，TOT（top of tree）是仓库根。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `verif/sim/Makefile` | 仿真 sandbox 的总入口：编译 `simv`、运行 trace、开波形、回归 |
| `verif/traces/README.md` | traces 目录说明，给出最简运行命令 |
| `verif/traces/traceplayer/sanity0/input.txn` | 一个最小 trace 样例，演示 read_reg/write_reg |
| `verif/traces/traceplayer/sanity0/plusargs.txt` | 该 trace 专用的仿真 plusarg |
| `verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl` | 把文本 `input.txn` 转成定宽十六进制 `input.txn.raw`，供 sequencer 回放 |
| `verif/sim/checktest.pl` | sv_tb 路径的日志检查器（解析 UVM 消息与 dump 比对） |
| `verif/synth_tb/sim_scripts/checktest_synthtb.pl` | synth_tb 默认路径的日志检查器（与上同源） |
| `verif/sim/checkcompile.pl` | 编译日志检查器，决定 `simv` 是否可用 |
| `tools/bin/run_sanity` | 一键脚本：拷 `tree.make`、跑 `tmake`、扫 `build.log` 汇总 |

## 4. 核心概念与源码讲解

### 4.1 verif/sim Makefile：仿真入口与目标体系

#### 4.1.1 概念说明

`verif/sim/Makefile` 是仿真的「驾驶舱」。它定义了三件事：用哪个仿真器、编译哪些文件、运行哪个 trace。整个文件的用法其实写在文件开头的注释里，这是最权威的「说明书」：

[verif/sim/Makefile:33-43](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L33-L43) —— 注释列出 `make clean`、`make build`、`make run TESTDIR=...` 以及带波形的 `DUMP=1 DUMPER=VERDI` 用法。

#### 4.1.2 核心流程

仿真的标准两步是「先编译、再运行」：

1. `make build`：调用 VCS 把 DUT 文件表 `dut.f` 加测试平台文件编译成可执行 `simv`，并跑 `checkcompile.pl` 校验。
2. `make run TESTDIR=../traces/traceplayer/sanity0`：把指定 trace 拷进结果目录、生成 `input.txn.raw`、启动 `simv`、最后跑 `checktest` 出结论。

关键变量与开关：

| 变量 | 默认值 | 含义 |
|------|--------|------|
| `TESTDIR` | `../traces/traceplayer/sanity0` | 要跑的 trace 目录 |
| `TB_TARGET` | `synth_tb` | 选哪套测试平台（`sv_tb` 标注 "Not supported yet"） |
| `DUMP` | `0` | 是否生成波形 |
| `DUMPER` | `DVE` | 波形格式：`DVE` / `VERDI` / `SILOTI` |

默认 trace 就指向 `sanity0`，所以不传 `TESTDIR` 时 `make run` 跑的就是它：

[verif/sim/Makefile:145](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L145) —— `TESTDIR ?= ../traces/traceplayer/sanity0`。

波形开关定义在此：

[verif/sim/Makefile:105-110](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L105-L110) —— `DUMP ?= 0`、`DUMPER ?= DVE`，以及三种波形文件名（`VPD_DUMP_NAME=vcdplus.vpd`、`VERDI_DUMP_NAME=debussy.fsdb`、`SILOTI_DUMP_NAME=siloti`）。

#### 4.1.3 源码精读

**编译目标**：`build` 调 VCS 生成 `simv`，随后用 `checkcompile.pl` 检查编译日志：

[verif/sim/Makefile:583-584](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L583-L584) —— `build : dut` 目标调用 `$(VCS_HOME)/bin/vcs -f ${DUTFILE} ... -o ${VCS_EXECUTABLE} ...`，结尾 `./checkcompile.pl ${COMPILELOG} ${VCS_EXECUTABLE}`。`checkcompile.pl` 只要日志里出现 `Error` 就删掉 `simv` 并报 `Compile FAILED`，否则报 `Compile Successful`。

**运行目标**：`run` 是本讲最重要的目标，它把「准备目录 → 生成激励 → 跑仿真 → 检查」串成一条流水：

[verif/sim/Makefile:466-479](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L466-L479) —— `run` 目标。关键几步：

- `${TB_PREP}`：建结果目录、把 trace 拷进去、`cd` 进去、软链 `simv`；
- `${SYNTHINPVECGEN} ${SIMTESTDIR}`：跑 `inp_txn_to_hexdump.pl` 把 `input.txn` 转成 `input.txn.raw`；
- `${VCS_EXECUTABLE} ... +input_file=... +input_dir=...`：启动 `simv`，把 trace 喂给测试平台里的 sequencer；
- `${CHECKTEST} ./$(TESTLOG)`：检查 `test.log`，打印 PASSED/FAILED。

结果目录由 `SIMTESTDIR` 决定：`SIMTESTDIR := ${SIMDIR}/${TESTNAME}`（L149），`TESTNAME := ${notdir ${TESTDIR}}`（L147）。所以对 `sanity0`，结果目录就是 `verif/sim/sanity0/`，日志是 `verif/sim/sanity0/test.log`（`verif/traces/README.md` 把它写作 `verif/sim/_test_`，实为以测试名命名的子目录）。

**波形分支**：当 `DUMP=1` 时，根据 `DUMPER` 选不同的 dump 参数。当前 HEAD 最近一次提交（8e06b1b）正是修这里：

[verif/sim/Makefile:401-411](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L401-L411) —— `DUMP_ARGS` 三分支：`SILOTI` 用 esdb、`VERDI` 用 fsdb、默认 `DVE` 用 vpd。当前 HEAD 的 `VERDI` 分支比旧版多了 `+dump_name=${SIMTESTDIR}/${VERDI_DUMP_NAME}`，正是这次提交让 fsdb 自动切换正常工作。

打开 Verdi 看波形的独立目标：

[verif/sim/Makefile:634-635](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L634-L635) —— `verdi` 目标调用 `$(VERDI_HOME)/bin/verdi ... -ssf ${SIMTESTDIR}/...fsdb`，把刚生成的 fsdb 加载进 Verdi（同样在 8e06b1b 里把 `-ssf` 的文件名从 `fsdb` 改成了 `vf`，与 fsdb 自动切换配套）。

#### 4.1.4 代码实践

1. 实践目标：在不实际编译的前提下，学会从 Makefile 读出「跑 sanity0 要敲什么、结果落在哪」。
2. 操作步骤：
   - 打开 `verif/sim/Makefile`，定位 `TESTDIR` 默认值（L145）与 `run` 目标（L466）。
   - 在 `run` 目标里找到 `SIMTESTDIR` 的用法（L149），反推结果目录路径。
3. 需要观察的现象：`SIMTESTDIR := ${SIMDIR}/${TESTNAME}`，而 `TESTNAME` 是 `TESTDIR` 的 basename，所以对 `sanity0` 结果目录应为 `verif/sim/sanity0/`。
4. 预期结果：能口述「跑 sanity0 = `cd verif/sim && make build && make run TESTDIR=../traces/traceplayer/sanity0`，结果在 `verif/sim/sanity0/test.log`」。
5. 待本地验证：实际目录是否生成、`test.log` 内容如何，需在有 VCS 的环境验证。

#### 4.1.5 小练习与答案

- 练习 1：`make build DUMP=1 DUMPER=VERDI` 相比普通 `make build` 多链接了什么？
  - 答：多了 `VERDI_PLI`（Novas PLI 的 `novas.tab` + `pli.a`）与 `VERDI_LD_FLAGS`（`-Wl,-rpath,...`），见 L324-L335；同时不再加 `+define+NO_DUMPS`（L338）。
- 练习 2：想一次跑完 `sanity0..3` 等一组 trace，用哪个目标？
  - 答：`make regress`（或 `make all`），它依赖 `FUNC_REGRESS_LIST`（L302-L309）里列出的 8 个 trace。

---

### 4.2 trace 样例与 trace-player 激励格式

#### 4.2.1 概念说明

trace 是 NVDLA 仿真最贴近「软件编程硬件」的一层。一份 trace 就是一串对 CSB 寄存器的读/写事务，外加偶尔的内存装载（`load_mem`）/转储（`dump_mem`）/等待（`wait`）。`verif/traces/traceplayer/` 下放了若干现成 trace，README 给出运行方式：

[verif/traces/README.md:1-9](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/README.md#L1-L9) —— 说明 `cd verif/sim && make run TESTDIR=<path/to/test>`，结果在 `verif/sim/<test>`。

可用的 trace 样例（来自回归列表 L302-L309 与目录实际内容）：

| trace | 用途 |
|-------|------|
| `sanity0` / `sanity1` / `sanity2` / `sanity3` | 基础冒烟测试（`sanity1/2/3` 各有 `_cvsram` 变体走片上 SRAM） |
| `conv_8x8_fc_int16` | int16 卷积 + 全连接 |
| `pdp_max_pooling_int16` | PDP 最大池化 |
| `sdp_relu_int16` | SDP ReLU 激活 |

#### 4.2.2 核心流程

trace 从文本到 DUT 的链路：

```
input.txn (文本)  ──inp_txn_to_hexdump.pl──▶  input.txn.raw (定宽十六进制)
                                                       │
                                          csb_master_seq 回放
                                                       ▼
                                              CSB 寄存器写/读 ──▶ DUT
```

`inp_txn_to_hexdump.pl` 定义了 7 类命令，每类用 3 bit 编码，每条命令定宽 120 bit：

[verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl:8-17](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl#L8-L17) —— 命令编码说明：`write_reg=000`、`read_reg=001`、`write_mem=010`、`read_mem=011`、`load_mem=100`、`dump_mem=101`、`wait=110`。

命令名到 hex 串的映射表：

[verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl:45-53](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl#L45-L53) —— `%command_hash` 把命令名映射成两位 hex（`write_reg→00`、`read_reg→01` … `wait→06`）。

以 `sanity0` 为例，它的完整激励只有 3 行有效事务：

[verif/traces/traceplayer/sanity0/input.txn:1-5](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/sanity0/input.txn#L1-L5) —— 先 `read_reg 0xffff100b`（读 `NVDLA_BDMA.CFG_DST_SURF_0`，期望默认值 `0xffffffe0`），再 `write_reg 0xffff100b 0xf0a5a500`（写一个魔数），最后再读回确认变成 `0xf0a5a500`。这是一次典型的「读默认值 → 写 → 读回校验」三步。

`write_reg` 的编码逻辑：把命令码、32 位地址、32 位数据拼起来，再按 `MSEQ_*` 位域补零到定宽：

[verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl:124-143](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl#L124-L143) —— `write_reg` 分支：取 `values[1]` 作地址、`values[2]` 作数据，拼成 `cmd + addr + data + padding`。

`read_reg` 更复杂，因为它要带「位掩码 + 比较模式 + 期望值 + 轮询次数」，sequencer 据此判断读到的是否符合预期：

[verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl:144-186](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl#L144-L186) —— `read_reg` 分支：支持 `==` / `<=` / `>=` 三种比较模式（编码 `00/01/02`），缺省轮询 50000 次；这就是 trace 能做「读回校验」的底层支持。

文件末尾写一个 `FF...` 结束标记告诉 sequencer 事务回放完毕：

[verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl:291-292](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/inp_txn_to_hexdump.pl#L291-L292) —— `my $end_of_test = "FF000000000000000000000000000000"`。

每个 trace 还可带专属 plusarg，`sanity0` 把读寄存器轮询次数降到 10、并允许失败继续：

[verif/traces/traceplayer/sanity0/plusargs.txt:1](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/traces/traceplayer/sanity0/plusargs.txt#L1) —— `+read_reg_poll_retries=10 +continue_on_fail`。Makefile 在 `run` 时通过 `TESTDIR_PLUSARGS_ONE`（L385-L387）把它读进来传给 `simv`。

#### 4.2.3 源码精读

（已在 4.2.2 中随流程引用关键代码点。）

#### 4.2.4 代码实践

1. 实践目标：手工把 `sanity0/input.txn` 的第一条 `read_reg` 翻译成 hex，验证你理解了编码规则。
2. 操作步骤：
   - 读 `input.txn` 第 3 行：`read_reg 0xffff100b 0xffffffe0 0x00000000`。
   - 按脚本规则：`cmd=01`，`addr=ffff100b`，`exp_data=ffffffe0`，`bitmask=00000000`，比较模式缺省 `==` → `00`，`poll_attempts` 缺省 50000 → `c350`（4 位 hex）。
   - 拼接：`01` + `ffff100b` + `ffffffe0` + `00000000` + `00` + `c350`。
3. 需要观察的现象：把结果与脚本实际生成的 `input.txn.raw` 第一行对比。
4. 预期结果：两者应一致（`01ffff100bffffffe00000000000c350`）。
5. 待本地验证：实际跑 `inp_txn_to_hexdump.pl` 后查看 `input.txn.raw` 确认（注意脚本默认从 `input.txn` 读、向当前目录写 `input.txn.raw`）。

#### 4.2.5 小练习与答案

- 练习 1：`sanity0` 为什么先读再写再读，而不是直接写？
  - 答：先读确认寄存器默认值（`0xffffffe0`）符合预期，写后再读确认写入生效——这是一次最小的「读改写校验」闭环，能同时验证读通路与写通路。
- 练习 2：`wait` 命令编码后为什么几乎全零？
  - 答：`wait` 只有命令码 `06`，其后 120 bit 数据位无意义，脚本用全零填充（L121-L123）。

---

### 4.3 run_sanity 脚本：一键构建与回归

#### 4.3.1 概念说明

`make build && make run` 只跑单个 trace。若想「把整个工程构建一遍、把所有 sanity trace 跑一遍、再把结果汇总」，就用 `tools/bin/run_sanity`。它本质上是 u1-l3 讲的 `tmake` 的一层薄包装：负责初始化 `tree.make`、调用 `tmake`、再扫描构建日志里的 `checktest` 行。

#### 4.3.2 核心流程

```
run_sanity
  ├─ tools/bin/depth -abs_tot        # 取 TOT（top of tree）绝对路径
  ├─ cp tree.make.vm → tree.make     # 用模板重置环境配置
  ├─ tools/bin/tmake -project nv_full# 按 build.config 构建整个工程
  ├─ 读 outdir/build.log             # 扫描所有 checktest 行
  └─ 任一非 PASSED → 退出码非零       # 汇总失败
```

#### 4.3.3 源码精读

初始化与构建：

[tools/bin/run_sanity:13-20](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/run_sanity#L13-L20) —— 取 TOT、校验 `tree.make.vm` 存在、`cp` 模板覆盖 `tree.make`、执行 `tmake -project nv_full`。这正是 u1-l3 描述的「tmake 按 build.config 拓扑驱动各 sandbox」的入口。

扫描日志判定成败：

[tools/bin/run_sanity:22-35](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/run_sanity#L22-L35) —— 打开 `outdir/build.log`，逐行匹配 `^checktest`，按 `:` 切出 `result` 字段，只要有一个 `result ne "PASSED"` 就把 `$fail` 置 1。

最终汇报：

[tools/bin/run_sanity:38-44](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/run_sanity#L38-L44) —— 若 `$fail` 为真，打印 `ERROR: simulation failed -> $log`；`exit ($fail)` 把成败作为进程退出码返回，方便上层脚本（如 CI）判断。

注意：`run_sanity` 自己不直接跑 `simv`，它依赖每个 sandbox 在 `tmake` 过程中产生的 `checktest:` 汇总行。所以它的成败口径完全取决于各 sandbox 的 `checktest` 脚本输出。

#### 4.3.4 代码实践

1. 实践目标：理解 `run_sanity` 的成败判定，不实际运行也能预测它的输出。
2. 操作步骤：在 `outdir/build.log`（若存在）里搜 `checktest` 行；若没有该文件，说明工程未构建过。
3. 需要观察的现象：每行形如 `checktest:PASSED:sanity0:...` 或 `checktest:FAILED:...`；脚本开启 `$debug=1`（L11）时会逐行打印 `line=...`/`result=...`/`fail=...`。
4. 预期结果：脚本退出码 = 是否存在非 PASSED 行；`final=0` 表示全过。
5. 待本地验证：实际 `tools/bin/run_sanity` 的输出需在有完整工具链的环境验证。

#### 4.3.5 小练习与答案

- 练习 1：`run_sanity` 与 `make regress` 的最大区别？
  - 答：`run_sanity` 走 `tmake` 构建 + 回归整树、从 `outdir/build.log` 汇总；`make regress` 只在 `verif/sim` sandbox 内对 `FUNC_REGRESS_LIST` 跑已编译好的 `simv`，不重建 RTL。
- 练习 2：脚本里 `$debug=1` 的作用？
  - 答：打开后逐行打印 `line=`/`result=`/`fail=`/`final=`（L29、L33、L42），便于排查哪条 checktest 出问题。

---

### 4.4 checktest 结果校验：从日志到 PASSED

#### 4.4.1 概念说明

仿真跑完不等于「过了」。`checktest` 系列脚本负责读 `simv` 产生的日志，按规则判定 PASSED/FAILED/RUNNING/NOTRUN。仓库里有两个同源脚本：

- `verif/sim/checktest.pl`：给 `TB_TARGET=sv_tb`（UVM 平台）用，识别 `UVM_ERROR`/`UVM_FATAL` 消息；
- `verif/synth_tb/sim_scripts/checktest_synthtb.pl`：给默认的 `synth_tb` 用，按 `ERROR` 关键字与 dump 比对判定。

Makefile 在 `TB_TARGET=sv_tb` 时把 `CHECKTEST` 指向 `./checktest.pl`：

[verif/sim/Makefile:173](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L173) —— sv_tb 分支 `CHECKTEST := ./checktest.pl`。

而默认 `synth_tb` 分支指向另一只：

[verif/sim/Makefile:217](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/Makefile#L217) —— `CHECKTEST := ${SYNTHTBDIR}/sim_scripts/checktest_synthtb.pl`。

本节以 `checktest.pl` 为例精读（它逻辑更完整），并指出两者差异。

#### 4.4.2 核心流程

`checktest.pl` 的判定逻辑可概括为：

1. 解析参数，定位日志文件（`<testdir>/<testfile>.log`，缺省 `input.txn.log`）；
2. 逐行扫日志，统计 `UVM_ERROR`、`UVM_FATAL`、`Starting transaction N`（进度）、编译起止时间；
3. 做 dump 比对：对每个 `*chiplib_dump.raw` 找对应 `*chiplib_replay`，用 `cmp` 比较；
4. 按下表给结论：

| 条件 | 结论 |
|------|------|
| `errors==0` 且发现 UVM 消息 且 dump 比对通过 | `PASSED` |
| 未发现任何 UVM 消息 | `RUNNING`（仿真可能还在跑或异常退出） |
| 有错误但非致命 | `FAILED ... with N errors` |
| 有致命错误 | `FAILED ... N fatal error` |
| 日志不存在 | `NOTRUN` |

「RUNNING」分支还会估算进度。设已处理事务数为 \(t\)、trace 总行数为 \(N\)，则进度百分比约为：

\[
P \approx 100 \times \frac{t}{N}
\]

#### 4.4.3 源码精读

参数与日志定位：

[verif/sim/checktest.pl:42-68](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/checktest.pl#L42-L68) —— 若第一个参数是目录，则日志为 `<dir>/<testfile>.log`；否则按 `路径/文件名` 拆分；缺省日志名 `input.txn.log`。日志不存在直接判 `NOTRUN`。

日志扫描循环：

[verif/sim/checktest.pl:100-136](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/checktest.pl#L100-L136) —— 统计 `UVM_ERROR`、`UVM_FATAL`（含计数）、`Starting transaction (\d+)` 记录最新事务号、`Compiler ...` 记录开始时间、`CPU Time:` 后记录结束时间，用于算运行时长。

dump 比对：

[verif/sim/checktest.pl:165-182](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/checktest.pl#L165-L182) —— 对每个 `*chiplib_dump.raw` 找对应 `*chiplib_replay`，用 `cmp` 比较；全部匹配则补一句 `(N of N dump files matched)`。

最终判定与输出：

[verif/sim/checktest.pl:191-218](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/sim/checktest.pl#L191-L218) —— 四分支输出 `PASSED`/`RUNNING`/`FAILED`，格式 `checktest : <状态> : <testdir> <详情>`。其中「无 UVM 消息」被判为 `RUNNING` 并打印进度百分比。

synth_tb 版的关键差异：它不要求 UVM 消息，只要 `errors==0` 且 dump 比对通过就 `PASSED`：

[verif/synth_tb/sim_scripts/checktest_synthtb.pl:174-183](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/verif/synth_tb/sim_scripts/checktest_synthtb.pl#L174-L183) —— `errors==0` 即 PASSED（再叠加 dump 比对），否则 FAILED；它扫的是 `.*ERROR.*` 而非 UVM 消息（L90）。

#### 4.4.4 代码实践

1. 实践目标：跑通 sanity0 并在结果目录确认 `checktest : PASSED`。
2. 操作步骤：
   - `cd verif/sim`
   - `make build`（首次会编译 `simv`，耗时较长）
   - `make run TESTDIR=../traces/traceplayer/sanity0`
   - 进入结果目录 `verif/sim/sanity0/`，打开 `test.log`
3. 需要观察的现象：终端末尾会打印一行 `checktest : PASSED : sanity0 ...`；`test.log` 里能看到 `read_reg`/`write_reg` 事务的执行记录。
4. 预期结果：状态为 `PASSED`；若仿真未正常跑完会显示 `RUNNING` 或 `NOTRUN`，需查 `vcs.log`/`test.log` 排错。
5. 待本地验证：本仓库环境无 VCS license，实际运行结果待本地验证；可先静态阅读 `sanity0/input.txn` 与 `checktest_synthtb.pl` 理解预期。

#### 4.4.5 小练习与答案

- 练习 1：`checktest.pl` 为什么在「没有 UVM 消息」时判 `RUNNING` 而不是 `FAILED`？
  - 答：UVM 平台正常结束必打印 `UVM_FATAL`/`UVM_ERROR` 汇总行；若无任何 UVM 消息，多半是仿真还在跑或异常中断（如崩溃、未到结束点），不能直接判错，故标 `RUNNING` 并报进度（L194-L206）。
- 练习 2：dump 比对失败但 `errors==0` 时，`checktest_synthtb.pl` 会怎么判？
  - 答：判 `FAILED ... dump_mem mismatched`（L175-L176）——即便没有 ERROR 行，内存转储与 golden 不符也算失败。

---

## 5. 综合实践

把本讲四块串起来，完成一次「只读」的端到端预演（无需 VCS）：

1. **选 trace**：打开 `verif/traces/traceplayer/sanity0/input.txn`，用 4.2 的编码规则手算第一条 `read_reg` 的 hex，记下来。
2. **跑通流程**：写出跑这个 trace 的完整命令序列（`cd verif/sim` → `make build` → `make run TESTDIR=...`），并指出结果目录与日志文件名（`verif/sim/sanity0/test.log`）。
3. **加波形**：把命令改成带 Verdi 波形的版本（`DUMP=1 DUMPER=VERDI`），说明这会让 `make build` 多链接 PLI、`make run` 多传 `+dump_fsdb` 系列 plusarg、生成的波形文件名是 `debussy.fsdb`，并用 `make verdi` 打开。
4. **预测结论**：根据 `sanity0` 是「读默认值 → 写魔数 → 读回校验」，预测 `checktest_synthtb.pl` 会输出 `PASSED`，并解释只要日志里没有 `ERROR` 且无 dump 比对失败即可。
5. **一键回归**：说明若改用 `tools/bin/run_sanity`，它会走 `tmake -project nv_full` 重建整树、从 `outdir/build.log` 汇总所有 `checktest:` 行。

把上述五步整理成一份「sanity0 仿真说明书」Markdown，作为本讲产出。

## 6. 本讲小结

- `verif/sim/Makefile` 是仿真驾驶舱：`make build` 编译 `simv`、`make run TESTDIR=...` 跑指定 trace，结果落在 `verif/sim/<testname>/test.log`。
- trace 是一串 CSB 寄存器读写事务，`inp_txn_to_hexdump.pl` 把文本 `input.txn` 转成定宽十六进制 `input.txn.raw` 供 sequencer 回放，这就是「trace-player」。
- `sanity0` 是最小冒烟 trace：读默认值 → 写魔数 → 读回校验，验证读写通路。
- `DUMP=1 DUMPER=VERDI` 开 fsdb 波形，最近提交 8e06b1b 修好了它的自动切换；波形文件 `debussy.fsdb` 用 `make verdi` 查看。
- `tools/bin/run_sanity` 是 `tmake` 的薄包装，一键构建整树并从 `outdir/build.log` 汇总 `checktest` 行。
- `checktest` 系列把日志判定为 `PASSED/FAILED/RUNNING/NOTRUN`；`checktest.pl`（sv_tb）认 UVM 消息，`checktest_synthtb.pl`（默认 synth_tb）认 `ERROR` 关键字 + dump 比对。

## 7. 下一步学习建议

- 下一讲 u1-l5「顶层 RTL：NV_nvdla.v 与分区结构」将进入 RTL 本体，本讲的 DUT 就是它。
- 想深入测试平台内部，可先读 `verif/synth_tb/tb_top.v`、`csb_master.v`、`csb_master_seq.v`（u7-l1 / u7-l2 会专讲 trace-player 的 RTL 实现）。
- 想理解 trace 里那些寄存器地址（如 `0xffff100b`）的来源，可看 `spec/manual/test.rdl`（u8-l2 专讲寄存器生成）。
- 若想用开源仿真器替代 VCS，可跳读 `verif/verilator/`（u7-l4）。

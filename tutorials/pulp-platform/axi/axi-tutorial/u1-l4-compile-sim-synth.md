# 如何编译、仿真与综合

## 1. 本讲目标

本讲承接「仓库结构与构建系统」（u1-l2），把抽象的 **Level 0–6 编译层级**落到可执行的操作上。学完后你应当能够：

- 用一条 `make` 命令完成「编译 → 仿真 → 综合」三大动作，并能解释每个 `.log` 文件对应哪一步。
- 说清楚 `compile_vsim.sh`、`run_vsim.sh`、`synth.sh`、`run_verilator.sh` 四个脚本各自的职责，以及它们各自调用哪个 EDA 工具、用哪个 Bender target。
- 理解本仓库「用日志内容当通过判据」的独特做法：脚本的成败不靠返回码，而靠在日志里 `grep "Errors: 0"`、`grep "Error:"`。
- 知道 `test/` 目录下 `tb_<dut>.sv` 的命名约定，以及如何只跑某一个测试台。

本讲不涉及 AXI 协议细节，也不修改任何 RTL；它只教你「如何把这套库跑起来」。

---

## 2. 前置知识

阅读本讲前，建议你已经了解以下概念（均在 u1-l2 建立）：

- **Bender**：本仓库使用的硬件包管理器。它能根据 `Bender.yml` 里的依赖关系，为不同 EDA 工具（vsim / synopsys / verilator）生成一份「按正确顺序编译这些文件」的脚本。
- **target（目标）**：Bender 用 `-t <name>` 选择一组源文件。本仓库定义了 `rtl`（可综合 RTL）、`test`（测试台）、`simulation`（仅仿真用模块，如 `axi_test`）、`synthesis`、`synth_test`（综合用例）等 target。
- **Level 0–6 层级**：`Bender.yml` 里手动标注的编译先后顺序，保证被 `import` 的 `package` 先编译。
- **测试台（testbench, TB）**：一段不可综合、用来给被测设计（Design Under Test, DUT）施加激励并检查响应的 SystemVerilog 代码，是仿真时的顶层模块。

如果这些词你还觉得陌生，先回去看一遍 u1-l2 再继续。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`Makefile`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L1-L91) | 总指挥。把编译/仿真/综合三件事包装成 `make` 目标，每个目标产出一个 `.log`，并用 `grep` 判断成功与否。 |
| [`scripts/compile_vsim.sh`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/compile_vsim.sh#L1-L47) | 用 Bender 生成 vsim 编译脚本，调用 Questa/vsim 把 RTL 编译进 `work` 库。 |
| [`scripts/run_vsim.sh`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L1-L281) | 跑仿真。对每个 TB 按一组随机种子 `sv_seed` 执行 `run -all`，并检查日志里的 `Errors: 0`。 |
| [`scripts/run_verilator.sh`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_verilator.sh#L1-L29) | 用开源 Verilator 做 lint（语法/可综合性静态检查），不真正产生仿真程序。 |
| [`scripts/synth.sh`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/synth.sh#L1-L31) | 用 Synopsys Design Compiler 对综合用例 `axi_synth_bench` 做 elaborate（精化），验证可综合性。 |
| [`test/axi_synth_bench.sv`](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/axi_synth_bench.sv#L1-L208) | 综合专用「测试台」。它不跑激励，而是用 `for` 循环把各种宽度/参数组合的模块实例化一遍，供综合器检验。 |

整条链路的因果关系是：

```
make compile.log  → compile_vsim.sh  （编出 work 库）
make sim-<tb>.log → run_vsim.sh      （依赖 compile.log，跑单个 TB）
make elab.log     → synth.sh          （综合 elaborate）
（CI 中）         → run_verilator.sh  （lint）
```

---

## 4. 核心概念与源码讲解

### 4.1 Makefile：构建流程的总指挥

#### 4.1.1 概念说明

`Makefile` 是整套构建的「菜单」。它的核心思想是：**每一步都产出一个 `.log` 文件，并用这个日志的内容（而不是命令的返回码）来判定是否成功**。这样做的好处是：即使某个工具本身不返回错误码，只要日志里出现了 `Error:` 或缺少 `Errors: 0`，构建就会失败——这比单纯依赖返回码更可靠。

Makefile 定义了一组「伪目标」（phony targets）：`help`、`all`、`sim_all`、`compile.log`、`elab.log`、`sim-%.log`、`clean`。其中带 `.log` 后缀的是真正干活的「文件目标」（file target），`make` 会把它们当作要生成的文件。

#### 4.1.2 核心流程

```text
make help            → 打印可用目标与 TB 列表
make compile.log     → 调 compile_vsim.sh 编译，日志存 compile.log
make sim-<tb>.log    → 调 run_vsim.sh 仿真某个 TB，存 sim-<tb>.log
make elab.log        → 调 synth.sh 综合 elaborate，存 elab.log
make all             → compile.log + elab.log + sim_all 一次性全跑
make clean           → 删 build/ 和所有 *.log
```

依赖链（`make` 据此决定执行顺序与增量重建）：

```text
elab.log    ──依赖──> Bender.yml + build/（目录）
compile.log ──依赖──> Bender.yml + build/
sim-%.log   ──依赖──> compile.log      （必须先编译才能仿真）
```

特别地，`sim-%.log` 是一个**模式规则**（pattern rule），`%` 会匹配 TB 名，例如 `make sim-axi_lite_regs.log` 会用 `% = axi_lite_regs`。

#### 4.1.3 源码精读

Makefile 首先根据所在机器选择 EDA 工具命令，方便在 ETH IIS 内部机器上自动切换版本：

- [Makefile:14-20](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L14-L20)：若检测到 `/etc/iis.version`（IIS 内部机器标志），就把 `VSIM`、`SYNOPSYS_DC` 指向带版本号的封装命令；否则用裸的 `vsim`/`dc_shell`。`?=` 表示「仅当环境未设置时才赋值」，所以你可以在命令行用 `VSIM=... make ...` 覆盖。

接着定义了所有可用 TB 的清单（**注意这里是「DUT 名」而非 `tb_` 文件名**）：

- [Makefile:22-40](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L22-L40)：`TBS` 变量列出 20 个测试目标，如 `axi_lite_regs`、`axi_xbar`、`axi_cdc` 等。这正是 `make help` 会打印的那份清单。
- [Makefile:42](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L42)：用 `addprefix sim-` 和 `addsuffix .log` 把每个 TB 名加工成 `sim-<tb>.log`，得到全部仿真子目标。

`help` 目标把用法打印出来：

- [Makefile:50-59](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L50-L59)：列出 `elab.log`、`compile.log`、`sim-#TB#.log`、`sim_all`、`clean` 各自的含义，并打印 TB 清单。

三个核心「文件目标」是本节重点，它们都遵循同一套模板：

- [Makefile:77-79](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L77-L79)：`compile.log` 目标——进入 `build/`，跑 `compile_vsim.sh`，用 `tee` 把输出同时存到上一级的 `compile.log`，最后一行 `(! grep -n "Error:" $@)` 是**通过判据**：`!` 取反，只有当日志里**找不到** `Error:` 时才返回成功。
- [Makefile:82-85](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L82-L85)：`sim-%.log` 模式目标——依赖 `compile.log`，跑 `run_vsim.sh --random-seed $*`（`$*` 是匹配到的 TB 名），通过判据是**同时** `! grep "Error:"` 且 `! grep "Fatal:"`。注意它依赖 `compile.log`，所以仿真前会自动确保已编译。
- [Makefile:72-74](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L72-L74)：`elab.log` 目标——跑 `synth.sh`（综合 elaborate），同样用 `! grep "Error:"` 判定。

> 关键认知：本仓库的「成功」=「日志里既无 `Error:` 也无 `Fatal:`」。后面你会看到脚本内部还有一层更细的 `Errors: 0` 检查，二者是配合关系。

#### 4.1.4 代码实践

1. **实践目标**：用 `make help` 自助了解可用目标，验证 TB 清单。
2. **操作步骤**：在仓库根目录执行 `make help`。
3. **需要观察的现象**：终端打印出 `elab.log`、`compile.log`、`sim-#TB#.log`、`sim_all`、`clean` 的说明，以及一长串 TB 名（如 `axi_lite_regs`、`axi_xbar`）。
4. **预期结果**：你能从这份清单里挑出本讲要仿真的 `axi_lite_regs`。
5. 待本地验证（需已安装 `make`，无需任何 EDA 工具即可看 `help`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sim-%.log` 要依赖 `compile.log`？如果不依赖会发生什么？

> **答案**：仿真需要先把 RTL 编译进 `work` 仿真库，`run_vsim.sh` 启动 vsim 时会去 `work` 里找已编译的模块。依赖 `compile.log` 让 `make` 在仿真前自动确保编译已完成；否则首次仿真会因为 `work` 里没有模块而报「模块未找到」错误。

**练习 2**：`(! grep -n "Error:" $@)` 里那个 `!` 的作用是什么？去掉它会怎样？

> **答案**：`!` 取反 `grep` 的退出码。`grep` 找到匹配返回 0（成功），取反后变成非 0（失败），于是 `make` 报错停止；`grep` 找不到匹配返回 1，取反后变成 0，`make` 继续。去掉 `!` 后逻辑完全颠倒——日志里有 `Error:` 反而被当成「通过」，构建会漏掉错误。

---

### 4.2 编译：compile_vsim.sh（RTL → 仿真库）

#### 4.2.1 概念说明

仿真前必须先把 SystemVerilog 源码「编译」成仿真器能加载的二进制库（Questa/vsim 称为 `work` 库）。`compile_vsim.sh` 干的就是这件事：它借助 Bender 生成一份 tcl 脚本，再交给 `vsim` 执行编译。

这里有一个**Bender target** 的关键知识点：编译 RTL 用于仿真时，需要同时带上 `-t test -t rtl` 两个 target——`rtl` 给出可综合模块，`test` 给出测试台。两个 target 取并集才是完整的仿真文件集。

#### 4.2.2 核心流程

```text
1. bender script vsim -t test -t rtl   →  生成 compile.tcl（含 vlog 编译命令，按 Level 0–6 排序）
2. （补丁）只对 axi_pkg.sv 加 -lint -pedanticerrors，做更严格的语法检查
3. vsim -c -do 'source compile.tcl'     →  逐个 vlog，结果写进 work 库
```

#### 4.2.3 源码精读

- [compile_vsim.sh:19](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/compile_vsim.sh#L19-L19)：`VSIM` 默认为 `vsim`，可被环境变量覆盖（与 Makefile 的 `?=` 思路一致）。
- [compile_vsim.sh:21-25](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/compile_vsim.sh#L21-L25)：核心命令 `bender script vsim -t test -t rtl`，输出一份 tcl 脚本到 `compile.tcl`。三个 `--vlog-arg` 给 `vlog`（编译器）附加参数：`-svinputport=compat`（SystemVerilog 端口兼容旧写法）、`-override_timescale 1ns/1ps`（统一时间精度）、`-suppress 2583`（屏蔽 2583 号告警）。第 26 行追加 `return 0` 保证 tcl 正常结束。

接着是一段「打补丁」逻辑——只对本仓库的 `axi_pkg.sv` 开启最严格的 lint：

- [compile_vsim.sh:28-45](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/compile_vsim.sh#L28-L45)：用 `awk` 扫描 `compile.tcl`，在 `src/axi_pkg.sv` 那一行前面插入 `-lint -pedanticerrors`。注释里坦诚说明「这种打补丁方式很丑」，因为 Bender 暂时无法对单个 target 单独加参数。**为什么只对 `axi_pkg`？** 因为它是全库共享的 `package`，类型定义一旦有歧义会污染所有依赖它的模块，所以对它单独施以最严格检查。

最后真正执行编译：

- [compile_vsim.sh:47](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/compile_vsim.sh#L47-L47)：`$VSIM -c -do 'exit -code [source compile.tcl]'`——以命令行模式（`-c`）启动 vsim，`source` 执行编译脚本，并用其返回码作为 vsim 的退出码。编译产物落进 `build/work` 库。

#### 4.2.4 代码实践

1. **实践目标**：理解 Bender 如何把「Level 0–6 层级」转化为具体的 `vlog` 命令顺序。
2. **操作步骤**：先只生成不执行——手动运行 `cd build && bender script vsim -t test -t rtl > compile.tcl`（需已安装 Bender），然后用 `less compile.tcl` 翻看内容。
3. **需要观察的现象**：`compile.tcl` 里是一行行 `vlog` 命令，文件顺序与 `Bender.yml` 的 Level 0→6 完全一致：`axi_pkg.sv` 最先，`axi_xp.sv`（Level 6）最后。
4. **预期结果**：你会看到「被依赖的文件一定先编译」这一规则在脚本里被忠实体现。
5. 待本地验证（需 Bender 与 vsim；若只想验证顺序，看 `Bender.yml` 的 sources 段即可）。

#### 4.2.5 小练习与答案

**练习 1**：为什么编译命令是 `-t test -t rtl` 而不是只用 `-t rtl`？

> **答案**：`-t rtl` 只包含可综合模块，不含 `test/` 下的测试台。仿真时顶层是测试台（如 `tb_axi_lite_regs`），它必须在编译集里才能被加载。两个 target 取并集，才同时覆盖被测模块和测试台。

**练习 2**：脚本为什么单独给 `axi_pkg.sv` 加 `-lint -pedanticerrors`，而不是给所有文件都加？

> **答案**：全库开启 `-pedanticerrors` 会让一些无害的、却严格违反规范的写法也变成致命错误，可能阻塞编译。`axi_pkg` 作为类型根基，正确性影响面最大，对它单独严格检查性价比最高；其它文件用默认宽松级别即可。

---

### 4.3 仿真：run_vsim.sh 与随机种子回归

#### 4.3.1 概念说明

仿真（simulation）是把测试台「跑起来」、让它驱动 DUT 并自检结果的过程。本仓库的仿真哲学叫 **directed random verification（定向随机验证）**：测试台用受约束的随机数产生激励，因此**同一个测试台换一个随机种子（seed）就是一次不同的激励**。`run_vsim.sh` 正是围绕「多个种子」组织起来的。

几个关键术语：

- **`sv_seed`**：SystemVerilog 标准里的随机种子，通过 vsim 的 `-sv_seed` 传入，`$urandom`/`$random` 都会用到它。
- **`Errors: 0`**：vsim 在每次 `run -all` 结束时打印的一行统计（形如 `# Errors: 0, Warnings: 5`），是仿真器自己累计的错误计数（含 `$fatal`/致命断言失败等）。
- **回归（regression）**：用多个种子反复跑同一组测试，以放大隐藏 bug 的概率。

#### 4.3.2 核心流程

```text
默认种子集 SEEDS=(0)        ← 0 是「回归一致」的基准种子
--random-seed 标志 → SEEDS+=(random)   ← 再追加一个真随机种子
对每个测试台、每个种子:
    echo "run -all" | vsim -sv_seed <seed> tb_<dut> ... | tee vsim.log
    grep "Errors: 0," vsim.log        ← 找不到就失败退出
```

脚本还有一个**大 `case` 分支**：针对某些 TB（如 `axi_xbar`、`axi_dw_downsizer`）会用嵌套 `for` 循环扫多个参数组合，一个 TB 实际跑很多次。

#### 4.3.3 源码精读

- [run_vsim.sh:19](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L19-L19)：`ROOT` 指向仓库根目录，供后面定位 `test/` 用。
- [run_vsim.sh:28](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L28-L28)：`SEEDS=(0)`——默认只跑种子 0。注释说明：0 永远包含在内，以保证「回归一致性」（同样的代码、同样的种子，结果应可复现）。
- [run_vsim.sh:30-35](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L30-L35)：`call_vsim` 函数——对每个种子执行 `vsim -sv_seed <seed>`，管道 `tee` 存日志，第 33 行 `grep "Errors: 0,"` 是**运行期判据**：若该行不存在（即错误数非 0），`grep` 返回失败，叠加 `set -euo pipefail`（第 18 行）会立即终止整个脚本。

`exec_test` 函数先检查测试台文件是否存在，再用 `case` 区分每个 TB 的参数扫描方式：

- [run_vsim.sh:37-41](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L37-L41)：若 `test/tb_<name>.sv` 不存在就报错退出。**这就是命名约定的来源**：脚本默认文件叫 `tb_<dut>.sv`。
- [run_vsim.sh:147-157](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L147-L157)：以 `axi_lite_regs` 为例——它在默认种子基础上**额外加种子 10 和 42**，再用三层 `for` 扫 `PrivProtOnly × SecuProtOnly × RegNumBytes` 共 12 个参数组合，每组跑一遍。这是「定向随机 + 参数扫描」的典型写法。
- [run_vsim.sh:178-192](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L178-L192)：`axi_xbar` 的扫描更夸张——五层嵌套循环覆盖「主数 × 从数 × ATOP × 独占访问 × UniqueIds」。
- [run_vsim.sh:244-246](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L244-L246)：`*)` 默认分支——对没有特殊要求的 TB，直接 `call_vsim tb_<dut> -t 1ns -coverage -voptargs="+acc +cover=bcesfx"`，开启覆盖率收集。

最后是参数解析与「无参数时跑全部 TB」的逻辑：

- [run_vsim.sh:254-256](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L254-L256)：`--random-seed` 标志（注意它不跟参数值）把 `random` 追加进种子集。
- [run_vsim.sh:267-276](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L267-L276)：若命令行没有给位置参数（没指定 TB），就用 `find` 扫 `test/tb_*.sv`（排除 `*_pkg.sv`），自动得到全部 TB 列表。

> 把 Makefile 和脚本串起来看：`make sim-axi_lite_regs.log` 展开成 `run_vsim.sh --random-seed axi_lite_regs`。这里 `--random-seed` 是开关、`axi_lite_regs` 是位置参数（被解析成「只跑这个 TB」），于是最终对 `axi_lite_regs` 这个 TB 用种子 `(0, 10, 42, random)` 各跑一遍。

#### 4.3.4 代码实践

1. **实践目标**：跑通一个真实测试台，并理解日志里 `Errors: 0` 的含义。
2. **操作步骤**：
   1. 先编译：`make compile.log`（会进入 `build/` 跑 `compile_vsim.sh`）。
   2. 再仿真：`make sim-axi_lite_regs.log`。
   3. 查看日志：`grep "Errors:" sim-axi_lite_regs.log`。
3. **需要观察的现象**：日志里会反复出现多行 `# Errors: 0, Warnings: <N>`，每个种子一行。`make` 最终返回成功（无 `Error:`/`Fatal:`）。
4. **预期结果**：`Errors: 0` 表示**仿真器在该次 `run -all` 期间没有累计任何错误**（如致命断言失败、`$fatal`）。注意：DUT 功能正确性的自检通常由测试台内部的 scoreboard 通过断言（`assert`）或 `$fatal` 完成——一旦检查不通过会触发 `$fatal`，从而让 `Errors` 不为 0、让 grep 失败。所以「`Errors: 0` 且 make 通过」=「本次随机激励下功能正确」。
5. 待本地验证（需 Questa/vsim 或兼容仿真器与 Bender；若本地无工具，可改为「源码阅读型实践」：对照第 4.3.3 节，口头复述 `make sim-axi_lite_regs.log` 会用哪几个种子、跑几组参数）。

#### 4.3.5 小练习与答案

**练习 1**：种子集为什么默认只放 `0`，而不是放一堆随机数？

> **答案**：种子 0 是「基准」——固定种子保证同样的代码每次回归得到同样的结果，便于定位「是不是这次改动引入了回归」。只有当用户显式加 `--random-seed` 时才追加一个真随机种子，兼顾「可复现」与「探索新激励」。

**练习 2**：`call_vsim` 里 `grep "Errors: 0,"`（带逗号）和 Makefile 里 `grep "Error:"`（带冒号）有什么区别？

> **答案**：前者（`Errors: 0,`）匹配 vsim 末尾的统计行，要求错误计数恰好为 0；后者（`Error:`）是泛匹配，捕获日志中任何单独的 `Error:` 报错行。两层检查互补：脚本层用 `Errors: 0,` 判定「仿真器自身无错」，Makefile 层用 `Error:`/`Fatal:` 兜底捕获任何显式报错文本。

---

### 4.4 静态检查与综合：run_verilator.sh、synth.sh 与 axi_synth_bench

#### 4.4.1 概念说明

仿真验证的是「行为对不对」，而下面两个脚本验证的是「代码写得规不规范、能不能被综合器吃下去」：

- **Verilator lint**（[run_verilator.sh](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_verilator.sh#L1-L29)）：用开源 Verilator 做静态语法/可综合性检查（`--lint-only`），不产生可执行程序，速度快，适合在 CI 里当「快速语法关」。
- **Synopsys DC elaborate**（[synth.sh](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/synth.sh#L1-L31)）：用工业级综合器 Design Compiler 把设计「精化」（elaborate）一遍——展开参数、生成门级网表的前置步骤，能发现「仿真能过但综合会炸」的问题。

两者都针对同一个顶层：`axi_synth_bench`。这是一个**特殊测试台**——它不施加任何激励，而是用 `for` 循环把模块的各种宽度/参数组合实例化一遍，让综合器/linter「看见」这些配置，从而一次性验证大量参数点都是可综合的。

#### 4.4.2 核心流程

**run_verilator.sh**：

```text
bender script verilator -t synthesis -t synth_test  →  生成 verilator.f（文件列表）
verilator --top-module axi_synth_bench --lint-only --timing -Wno-fatal -f verilator.f
```

**synth.sh**：

```text
bender script synopsys -t synth_test  →  生成 synth.tcl
echo 'elaborate axi_synth_bench'  >>  synth.tcl
dc_shell < synth.tcl  |  tee synth.log
grep -i "error:" synth.log  →  有则失败
```

注意两者用的 target 不同：Verilator 用 `-t synthesis -t synth_test`（带上 `synthesis` 才包含可综合验证模块），synth.sh 只用 `-t synth_test`（`synth_test` 自身已声明依赖 `synthesis`，Bender 会自动展开传递依赖）。

#### 4.4.3 源码精读

- [run_verilator.sh:22-24](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_verilator.sh#L22-L24)：`VERILATOR` 默认 `verilator`；`bender script verilator -t synthesis -t synth_test` 生成文件清单 `verilator.f`。
- [run_verilator.sh:26-29](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_verilator.sh#L26-L29)：`-Wno-fatal` 把 Verilator 的 warning 降级为非致命（否则很多无害告警也会让 lint 失败），`--lint-only` 只做检查不编译，`--timing` 启用时序检查，`--top-module axi_synth_bench` 指定顶层。

- [synth.sh:20](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/synth.sh#L20-L20)：`SYNOPSYS_DC` 默认 `synopsys dc_shell -64`，可被 Makefile/CI 覆盖。
- [synth.sh:22-24](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/synth.sh#L22-L24)：先 `remove_design -all` 清空，再让 Bender 生成 synopsys tcl，最后追加 `elaborate axi_synth_bench` 命令——注意只到 elaborate，不做真正的逻辑综合，目的是快速验证可综合性。
- [synth.sh:26-28](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/synth.sh#L26-L28)：`tee synth.log` 存日志，`grep -i "warning:"` 把告警打印出来（`|| true` 保证没告警也不报错），`grep -i "error:" && false` 保证只要有 error 就让脚本失败。
- [synth.sh:30](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/synth.sh#L30-L30)：`touch synth.completed` 留一个完成标志文件。

综合顶层 `axi_synth_bench` 如何「铺开」参数空间：

- [axi_synth_bench.sv:23-31](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/axi_synth_bench.sv#L23-L31)：用三组 `localparam` 数组枚举地址/ID/USER 宽度与主从数，再在 `for (genvar i ...)` 里例化 `synth_slice`——把数据宽度从 8 扫到 1024（`DW = (2**i)*8`）。每个 `synth_slice` 内部例化一组真实模块（如 `axi_to_axi_lite_intf`、`axi_lite_to_axi_intf`）。
- [axi_synth_bench.sv:46-59](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/axi_synth_bench.sv#L46-L59)：例化 `synth_axi_atop_filter`，双重循环扫「ID 宽度 × 最大写事务数」组合。整份文件就是一堆这样的 `for` 循环，目的是让综合器一次 elaborate 就覆盖到几十种参数配置。

> 设计意图：与其写很多带激励的测试台去综合（综合很慢），不如写一个「参数扫描器」把所有配置实例化，让 elaborate 阶段一次性确认「这些组合都能被综合器解析」。这是可综合性回归的常见手法。

#### 4.4.4 代码实践

1. **实践目标**：理解综合用例如何用 `for` 循环覆盖参数空间，并尝试跑一次 Verilator lint。
2. **操作步骤**：
   1. 阅读 [test/axi_synth_bench.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/axi_synth_bench.sv#L28-L31)，数一数 `synth_slice` 的数据宽度循环共覆盖多少种 DW。
   2. 若本地已装 Verilator 与 Bender：`cd build && ../scripts/run_verilator.sh`，观察是否无 error 退出。
3. **需要观察的现象**：`synth_slice` 循环里 `i` 从 0 到 7，对应 DW = 8,16,…,1024 共 8 种；Verilator 若通过则无输出且退出码为 0。
4. **预期结果**：能说出「`axi_synth_bench` 用 `for` 循环把宽度/ID/主从数等多种配置一次性实例化，供综合器验证」。
5. 待本地验证（Verilator/synth 部分需对应工具；阅读部分无需工具）。

#### 4.4.5 小练习与答案

**练习 1**：`synth.sh` 为什么用 `elaborate` 而不是完整的 `compile_ultra`（真正综合）？

> **答案**：完整综合很慢，不适合放进每次 CI。`elaborate` 只是把参数展开、生成设计的内部表示，足以发现「语法/可综合性」问题（如不可综合的写法、参数冲突），速度快得多。CI 的目标是「保证没引入可综合性回归」，elaborate 已足够。

**练习 2**：Verilator 加 `-Wno-fatal` 的目的是什么？

> **答案**：Verilator 默认把很多 warning 当作 fatal 直接终止。本仓库代码里有些写法会触发 Verilator 的告警但实际无害（且其他 EDA 工具能接受）。`-Wno-fatal` 让告警只打印不致命，避免「无害告警阻塞 CI」，同时仍保留对真正 error 的拦截。

---

## 5. 综合实践

把本讲四块知识串起来，完成一次「编译 → 单 TB 仿真 → 解读日志」的完整流程。这是你后续阅读任何模块源码前都应该先跑通的「冒烟测试」。

**任务**：

1. 在仓库根目录执行 `make help`，确认你能找到 `compile.log`、`sim-axi_lite_regs.log` 两个目标。
2. 执行 `make compile.log`，等待编译完成；若失败，打开 `compile.log` 搜 `Error:` 定位原因（常见：缺 Bender、缺 vsim）。
3. 执行 `make sim-axi_lite_regs.log` 跑 `axi_lite_regs` 测试台。
4. 用 `grep "Errors:" sim-axi_lite_regs.log` 查看每个种子/参数组合的仿真器错误统计。
5. 写一段话回答：
   - 这次仿真一共跑了多少组（种子 × 参数组合）？
   - 「`Errors: 0`」这一行由谁打印、代表什么？
   - 如果某次随机激励下功能出错，日志里最先会出现什么、`make` 最终如何感知？

**预期结论**：`axi_lite_regs` 会用种子 `0/10/42/random` 各跑 12 组（Priv × Secu × Bytes）参数；`Errors: 0` 由 vsim 在每次 `run -all` 结束打印；功能出错时测试台内部会 `$fatal`，使该次 `Errors` 不为 0，`run_vsim.sh` 的 `grep "Errors: 0,"` 失败并因 `set -e` 退出，最终 `make` 因 `(! grep "Error:")` 失败而报错。

> 待本地验证：以上流程需 Questa/vsim（或兼容仿真器）与 Bender；CI 中固定使用 `VSIM: questa-2025.1 vsim`（见 [.gitlab-ci.yml:3](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/.gitlab-ci.yml#L1-L4)）。若本地无工具，可改为纯阅读型实践——只做第 1 步与第 5 步，依据本讲 4.3 节推理出答案。

---

## 6. 本讲小结

- `Makefile` 把编译/仿真/综合包装成 `compile.log` / `sim-<tb>.log` / `elab.log` 三个文件目标，并用「日志里无 `Error:`/`Fatal:`」作为通过判据，而非依赖命令返回码。
- `compile_vsim.sh` 用 `bender script vsim -t test -t rtl` 生成按 Level 0–6 排序的编译脚本，并对 `axi_pkg` 单独开启最严格 lint。
- `run_vsim.sh` 以多 `sv_seed` 种子（默认含 0，可加 random）做定向随机回归，针对部分 TB 还用嵌套 `for` 扫描参数组合；其内部判据是 `grep "Errors: 0,"`。
- 测试台命名遵循 `tb_<dut>.sv`，`run_vsim.sh` 据此定位文件；`make sim-<dut>.log` 只跑指定 TB。
- `run_verilator.sh`（Verilator lint）与 `synth.sh`（Synopsys DC elaborate）都针对综合用例 `axi_synth_bench`，后者用 `for` 循环一次性实例化大量参数配置以回归可综合性。
- EDA 工具命令全部可通过环境变量（`VSIM`/`SYNOPSYS_DC`/`VERILATOR`）覆盖，CI 里固定为带版本号的封装命令。

---

## 7. 下一步学习建议

现在你已经能让整套库「跑起来」，接下来可以：

- **进入 u2（基础设施）**：本讲的编译流程会让你反复看到 `axi_pkg`、`axi_intf`、`include/axi/*.svh`——下一单元 U2-L1/U2-L3 会逐个拆解这些类型、接口与宏的真实内容。
- **学写自己的测试台**：本讲只教你「跑」别人写好的 TB；U3-L3（编写并运行一个测试台）会以 `tb_axi_lite_regs.sv` 为范本，教你自己接线、例化随机主从、搭 scoreboard。
- **深入随机验证方法学**：U16-L1 会从「为什么用 directed random」的高度，讲解 `run_vsim.sh` 这套种子回归在工业级验证中的定位。
- **继续阅读源码**：建议先读 [src/axi_pkg.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv)（它是 Level 0、一切根基），再配合 U2 系列讲义对照理解。

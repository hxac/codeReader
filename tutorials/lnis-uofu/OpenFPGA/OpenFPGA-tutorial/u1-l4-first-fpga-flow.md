# 运行第一个 FPGA 设计流

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `run-task`、`run-flow`、`rerun-task`、`goto-task` 等 `openfpga.sh` 快捷命令分别调用了哪个 Python 脚本、做了什么事。
- 读懂一个 `task.conf` 的各个段落（`[GENERAL]`、`[OpenFPGA_SHELL]`、`[ARCHITECTURES]`、`[BENCHMARKS]`、`[SYNTHESIS_PARAM]`），并算出它会展开成多少个任务作业（job）。
- 理解「批量任务调度器」`run_fpga_task.py` 与「单次流程执行器」`run_fpga_flow.py` 的分工，以及它们如何用目录编号、`latest` 软链接、笛卡尔积展开来组织运行结果。
- 独立跑通一个 `micro_benchmark`（如 `and2`），并能在 `run` 目录里定位到生成的 fabric 网表与比特流。

本讲是「动手」的一讲：不再讲编译，而是把上一讲编出来的 `openfpga` 二进制真正跑起来，完成一次从 Verilog 到比特流的完整流程。

## 2. 前置知识

在开始前，请确认你已经掌握（来自 u1-l1 ~ u1-l3）：

- **OpenFPGA 的端到端流程分四个阶段**：综合（Yosys）→ 布局布线打包（VPR）→ fabric 构建（OpenFPGA）→ 网表/比特流/约束生成（OpenFPGA）。本讲的脚本就是把这四个阶段串起来的「胶水」。
- **`openfpga` 是一个命令行 shell 程序**：它既能交互式运行，也能用 `-f 脚本.openfpga` 执行脚本（见 u2-l1）。本讲的 Python 脚本最终就是用 `-batch -f` 方式驱动它的。
- **`source openfpga.sh` 已经执行过**：它设置好了 `OPENFPGA_PATH` 等环境变量，并定义了一组 bash 函数。本讲大量使用这些函数。
- **Python 3**：本讲的两个核心脚本都是 Python 3 写的。

两个你可能不熟悉的 Python 概念，先打个预防针：

- **`ConfigParser` 的扩展插值（ExtendedInterpolation）**：配置文件里 `${PATH:OPENFPGA_PATH}` 这种写法，表示「引用 `[PATH]` 段落里的 `OPENFPGA_PATH` 变量」。OpenFPGA 用它来让 `task.conf` 里的路径随 `OPENFPGA_PATH` 自动变化，而不必写死绝对路径。
- **`string.Template` 的 `safe_substitute`**：把字符串里的 `${VAR}` 替换成对应值。OpenFPGA 的 `.openfpga` 脚本模板就是用这个机制在运行前注入文件路径等变量。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲定位 |
| --- | --- | --- |
| `openfpga.sh` | 供 `source` 的 bash 脚本，定义快捷函数与导出环境变量 | 用户实际输入的入口 |
| `openfpga_flow/tasks/.../config/task.conf` | 一个任务的「配方卡」，INI 格式 | 任务的定义 |
| `openfpga_flow/scripts/run_fpga_task.py` | 批量任务调度器：读 `task.conf`，展开成多个 job，多线程并行 | `run-task` 背后的引擎 |
| `openfpga_flow/scripts/run_fpga_flow.py` | 单次流程执行器：处理「一个架构 + 一个基准」，跑完 yosys + openfpga shell | 每个 job 实际执行的脚本 |
| `openfpga_flow/scripts/run_fpga_task.conf` | 调度器自身的全局配置（任务目录、默认脚本路径） | 调度器的「常量」 |
| `openfpga_flow/misc/fpgaflow_default_tool_path.conf` | CAD 工具（yosys/vpr/openfpga）的可执行路径与日志解析规则 | 单次流程的工具寻址 |
| `openfpga_flow/openfpga_shell_scripts/write_full_testbench_example_script.openfpga` | 任务默认调用的 openfpga shell 模板 | 单次流程最终驱动的脚本 |

记住一条调用链，本讲全部内容都围绕它展开：

```
你输入:  run-task basic_tests/full_testbench/configuration_chain
   │  (openfpga.sh 里的 bash 函数)
   ▼
run_fpga_task.py     ← 批量调度：读 task.conf，展开 N 个 job，多线程
   │  (每个 job 调一次)
   ▼
run_fpga_flow.py     ← 单次流程：yosys 综合 + 驱动 openfpga shell
   │  (内部调用)
   ▼
openfpga -batch -f <run.openfpga>   ← 真正的 fabric 构建 / 比特流 / 网表生成
```

## 4. 核心概念与源码讲解

### 4.1 openfpga.sh：把命令变短的环境脚本

#### 4.1.1 概念说明

`openfpga.sh` 不是用来「执行」的程序，而是用来「加载」的。你在终端里写 `source openfpga.sh`（或者 `. openfpga.sh`），它会在**当前这个 shell 会话**里做两件事：

1. 导出几个环境变量（最重要的 `OPENFPGA_PATH`，指向仓库根目录）。
2. 定义一组 bash 函数（`run-task`、`run-flow`、`goto-task` 等），让你不必每次手敲一长串 `python3 .../run_fpga_task.py ...`。

为什么用 `source` 而不是直接 `bash openfpga.sh`？因为函数和环境变量只有在 `source` 时才会留在你当前的终端里；直接执行的话，子进程一退出，函数就没了。这也是上一讲强调「`source openfpga.sh` 须在仓库根目录」的原因——它要用 `pwd` 来推断 `OPENFPGA_PATH`。

#### 4.1.2 核心流程

`source openfpga.sh` 时发生的事：

1. 检查 `OPENFPGA_PATH` 是否已设置；没有就用当前目录 `pwd` 兜底。
2. 推导出 `OPENFPGA_SCRIPT_PATH`（指向 `openfpga_flow/scripts`）和 `OPENFPGA_TASK_PATH`（指向 `openfpga_flow/tasks`）。
3. 定义一组快捷函数。
4. 为 `run-task` / `goto-task` 注册 tab 自动补全（补全任务名）。

随后你就能在终端里直接使用这些函数。最常用的几个：

| 函数 | 作用 | 等价命令 |
| --- | --- | --- |
| `run-task <任务名>` | 跑一个批量任务 | `python3 $OPENFPGA_SCRIPT_PATH/run_fpga_task.py <任务名>` |
| `run-flow ...` | 跑一次单流程（直接传参） | `python3 .../run_fpga_flow.py ...` |
| `rerun-task <任务名>` | 清掉旧 run 目录后重跑 | 先 `--remove_run_dir all`，再跑 |
| `goto-task <任务名> [run号]` | 进入任务的 run 结果目录 | 手动 `cd` 一长串路径 |
| `list-tasks` | 列出所有可用任务 | 用 `tree` 扫描 `task.conf` |
| `create-task <名> [模板]` | 从模板新建一个任务 | 拷贝模板目录 |
| `run-regression-local` | 本地跑回归测试 | `bash .github/workflows/*reg_test.sh` |

#### 4.1.3 源码精读

**环境变量初始化**——如果 `OPENFPGA_PATH` 没设，就用当前目录兜底，并推导两个子路径：

[openfpga.sh:8-16](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh#L8-L16) — 设置 `OPENFPGA_PATH`、`OPENFPGA_SCRIPT_PATH`、`OPENFPGA_TASK_PATH`，并默认 `PYTHON_EXEC=python3`。

**`run-task` 与 `rerun-task`**——这两个函数揭示了「快捷命令 = 一行 Python 调用」的本质：

[openfpga.sh:61-68](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh#L61-L68) — `rerun-task` 先带 `--remove_run_dir all` 跑一次（清空旧 run 目录），再正常跑一次；`run-task` 直接把所有参数原样传给 `run_fpga_task.py`。

**`run-flow`**——单次流程的入口，把参数透传给 `run_fpga_flow.py`：

[openfpga.sh:82-84](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh#L82-L84) — `run-flow` 等价于直接调用 `run_fpga_flow.py`。

**`goto-task`**——本讲实践要用的关键函数。它先拼出任务目录，选好 `runXXX`（或 `latest`），再 `cd` 进去，最后用一个 `select` 菜单让你挑某个基准的子目录：

[openfpga.sh:106-133](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh#L106-L133) — 解析任务名与 run 编号，进入对应的 `run_dir`，并通过 `select` 让你在多个 `arch` 子目录间切换。第 118 行把 `run号` 格式化成 `run%03d`（如 `run001`），传 `0` 或不传则用 `latest` 软链接。

#### 4.1.4 代码实践

1. **实践目标**：确认 `openfpga.sh` 已正确加载，环境变量与函数就位。
2. **操作步骤**：
   - 在仓库根目录执行 `source openfpga.sh`。
   - 执行 `echo $OPENFPGA_PATH`，确认它指向仓库根目录。
   - 执行 `echo $OPENFPGA_SCRIPT_PATH`，确认指向 `openfpga_flow/scripts`。
   - 执行 `list-tasks`，查看可用的任务列表。
3. **需要观察的现象**：终端会先打印 `OPENFPGA_PATH=...`；`list-tasks` 会列出一批任务路径（如 `basic_tests/full_testbench/configuration_chain` 等）。
4. **预期结果**：三个环境变量都有值，`list-tasks` 输出非空。如果 `list-tasks` 报 `tree: command not found`，说明系统缺少 `tree` 工具，可先 `sudo apt-get install tree`（待本地验证环境是否已预装）。

#### 4.1.5 小练习与答案

**练习 1**：如果不小心直接 `bash openfpga.sh` 而不是 `source openfpga.sh`，会发生什么？为什么？

**答案**：`bash openfpga.sh` 会在一个子 shell 里执行，函数与环境变量只存在于那个子进程；脚本一结束它们就消失了，你当前终端里仍然没有 `run-task` 可用。只有 `source`（或 `.`）才会在当前 shell 注入这些定义。

**练习 2**：`rerun-task` 和 `run-task` 的差别，对应到源码上是哪两行？

**答案**：`rerun-task` 多做了一步清理——先调 `run_fpga_task.py "$@" --remove_run_dir all` 删掉所有旧 `runXXX` 目录，再正常调一次 `run_fpga_task.py "$@"`。`run-task` 只做后者。差别就在 [openfpga.sh:61-64](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh#L61-L64) 那两行 Python 调用。

---

### 4.2 task.conf：一个任务的「配方卡」

#### 4.2.1 概念说明

在 OpenFPGA 里，一个**任务（task）**就是一个目录，目录里必须有一个 `config/task.conf` 文件。这个文件就是任务的「配方卡」——它用 INI 格式说明：「用哪些架构文件、跑哪些基准设计、用什么 openfpga shell 脚本模板、用什么综合参数」。

调度器 `run_fpga_task.py` 会读这张配方卡，把其中列出的「架构 × 基准 × 脚本参数」做笛卡尔积，展开成若干个独立的 **job**，每个 job 跑一次完整流程。

配方卡里的路径几乎都不写死，而是用 `${PATH:OPENFPGA_PATH}/...` 引用环境变量，这样任务在任何机器上都能跑（只要 `OPENFPGA_PATH` 设对了）。

#### 4.2.2 核心流程

以本讲要跑的任务 `basic_tests/full_testbench/configuration_chain` 为例，它的 `task.conf` 有五个段落，各司其职：

```
[GENERAL]            ← 流程级开关：用哪种 run_engine、是否做功耗分析、流程类型
   │
[OpenFPGA_SHELL]     ← openfpga shell 相关：脚本模板、openfpga_arch、仿真设置
   │
[ARCHITECTURES]      ← VPR 架构文件列表（arch0, arch1, ...）
   │
[BENCHMARKS]         ← 基准 Verilog 文件列表（bench0, bench1, ...）
   │
[SYNTHESIS_PARAM]    ← 每个基准的综合参数（top 模块名、通道宽度等）
   │
[SCRIPT_PARAM_*]     ← 额外脚本参数段（段名含 SCRIPT_PARAM 都算），影响 run 目录命名
```

job 总数由三段决定：

\[ \text{job 数} = (\text{架构数}) \times (\text{基准数}) \times (\text{SCRIPT\_PARAM 段数}) \]

对这个任务：1 个架构 × 3 个基准（`and2`/`or2`/`and2_latch`）× 1 个脚本参数段 = **3 个 job**。

#### 4.2.3 源码精读

**`[GENERAL]` 段**——流程级配置：

[configuration_chain/config/task.conf:9-16](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf#L9-L16) — 其中 `run_engine=openfpga_shell` 表示用 openfpga shell 驱动（而不是老的 vpr 流程）；`fpga_flow=yosys_vpr` 表示先 Yosys 综合再 VPR；`timeout_each_job = 20*60` 是单个 job 的超时（秒）。

**`[OpenFPGA_SHELL]` 段**——指定 openfpga shell 要用的三个输入文件：

[task.conf:18-23](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf#L18-L23) — `openfpga_shell_template` 指向 `write_full_testbench_example_script.openfpga`（4.4 节会讲它内部干了什么）；`openfpga_arch_file` 指向 `k4_N4_40nm_cc_openfpga.xml`（这是 u3 的主角，`cc` 表示配置链 scan_chain 协议）；`openfpga_sim_setting_file` 是仿真设置。后两项 `openfpga_vpr_device_layout=` 和 `openfpga_fast_configuration=` 故意留空，会在模板里替换成「无」。

**`[ARCHITECTURES]` 与 `[BENCHMARKS]` 段**——笛卡尔积的两个维度：

[task.conf:25-31](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf#L25-L31) — `arch0` 是唯一的 VPR 架构（`k4_N4_tileable_40nm.xml`，u3-l1 会讲它和 `openfpga_arch` 的区别）；`bench0`/`bench1`/`bench2` 是三个微基准，`and2` 就是本讲的「hello world」。

**`[SYNTHESIS_PARAM]` 段**——每个基准的 top 模块名与通道宽度，靠 `benchX_` 前缀与上面的 `benchX` 对应：

[task.conf:33-42](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf#L33-L42) — `bench0_top = and2` 把 `bench0` 的顶层模块设为 `and2`；`bench0_chan_width = 300` 设 VPR 布线通道宽度。`bench_read_verilog_options_common = -nolatches` 是所有基准共用的 Yosys 读入选项。

最后那个 `[SCRIPT_PARAM_MIN_ROUTE_CHAN_WIDTH]` 段（[task.conf:44-45](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf#L44-L45)）里只有空的 `end_flow_with_test=`，它会让结果目录多一层 `MIN_ROUTE_CHAN_WIDTH/` 命名（4.3 节解释）。

#### 4.2.4 代码实践

1. **实践目标**：读懂一张真实的 `task.conf`，并预测它会展开成几个 job。
2. **操作步骤**：
   - 打开 `openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf`。
   - 数一下 `[ARCHITECTURES]` 有几条、`[BENCHMARKS]` 有几条、文件里有几个段名含 `SCRIPT_PARAM`。
   - 用上面的公式算 job 数。
3. **需要观察的现象**：架构 1 条、基准 3 条、`SCRIPT_PARAM` 段 1 个。
4. **预期结果**：`1 × 3 × 1 = 3` 个 job。如果你把 `[ARCHITECTURES]` 再加一条 `arch1=...`（指向另一个 vpr_arch），job 数会变成 `2 × 3 × 1 = 6`。

#### 4.2.5 小练习与答案

**练习 1**：`task.conf` 里为什么几乎每个路径都写成 `${PATH:OPENFPGA_PATH}/...` 而不是绝对路径？

**答案**：为了让任务可移植。`${PATH:OPENFPGA_PATH}` 是 `ConfigParser` 的扩展插值，运行时会被替换成当前机器上的 `OPENFPGA_PATH` 值。这样无论仓库被 clone 到哪、叫什么名字，路径都能正确解析。

**练习 2**：`[OpenFPGA_SHELL]` 里的 `openfpga_arch_file` 指向 `k4_N4_40nm_cc_openfpga.xml`，文件名里的 `cc` 暗示了什么？

**答案**：`cc` 代表 configuration chain（配置链，即 `scan_chain` 配置协议）。它说明这个任务生成的 FPGA 用一条移位寄存器链来装载配置比特，这和 `bank`（memory_bank）、`frame`（frame_based）等是不同的配置协议（u3-l4 专题讲解）。

---

### 4.3 run_fpga_task.py：批量任务的调度器

#### 4.3.1 概念说明

`run_fpga_task.py` 是 `run-task` 背后的引擎。它解决的问题是：**一个任务往往包含多个「架构 × 基准」组合，手动一个个跑太累**。调度器读 `task.conf`，把所有组合展开成一张 job 清单，然后用多线程并行执行，每个 job 调一次 `run_fpga_flow.py`。跑完再把每个 job 的性能数据汇总成一张 CSV。

你可以把它理解成一个「批处理器 + 结果收集器」：它自己不跑任何 EDA 工具，只负责拆任务、派任务、收结果。

#### 4.3.2 核心流程

`run_fpga_task.py` 的主流程：

```
main()
  ├─ 对命令行传入的每个 task：
  │    ├─ generate_each_task_actions(task)
  │    │     ├─ 定位任务目录（本地 / 绝对 / 仓库 tasks 目录）
  │    │     ├─ 校验 task.conf 必含 [GENERAL][BENCHMARKS][ARCHITECTURES]
  │    │     ├─ 创建新的 runXXX 目录 + 更新 latest 软链接
  │    │     ├─ 读 [ARCHITECTURES][BENCHMARKS][SYNTHESIS_PARAM]
  │    │     └─ 三重循环展开 job 清单（arch × bench × script_param）
  │    │        并为每个 job 用 create_run_command() 组装命令行
  │    ├─ run_actions(job_list)        ← 多线程，Semaphore(maxthreads) 限流
  │    │     每个线程：subprocess 调 run_fpga_flow.py
  │    └─ collect_results(job_list)    ← 读各 job 的 vpr_stat.result，写 task_result.csv
```

几个关键设计：

- **run 目录编号**：每次跑任务新建 `run001`、`run002`……（取已有最大编号 +1），并维护一个 `latest` 软链接指向最新一次。这样多次运行的结果互不覆盖，`goto-task` 默认进 `latest`。
- **并发限流**：用 `threading.Semaphore(maxthreads)` 控制同时跑的 job 数（默认 `--maxthreads 2`），避免把机器跑爆。
- **结果目录命名**：每个 job 的结果落在 `runXXX/<架构名>/<top模块>/<脚本参数标签>/` 下，三段命名保证了不同组合互不冲突。

#### 4.3.3 源码精读

**主函数**——遍历任务、展开、执行、收集：

[run_fpga_task.py:120-136](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_task.py#L120-L136) — `main()` 对每个任务调 `generate_each_task_actions` 拿到 job 清单，若非 `--test_run` 就 `run_actions` 执行、`collect_results` 汇总。注意第 131 行：只有当 `fpga_flow != "yosys"` 时才收集结果（纯 yosys 流程没有 vpr 统计）。

**run 目录创建与编号**：

[run_fpga_task.py:226-246](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_task.py#L226-L246) — 扫描已有 `run*[0-9]` 目录取最大编号 +1，得到 `run%03d`（第 228 行），新建它并把 `latest` 软链接重指过来（第 239-241 行）。这就是 `goto-task` 能用 `latest` 找到最新结果的根源。

**笛卡尔积展开 job 清单**——本节的核心：

[run_fpga_task.py:403-446](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_task.py#L403-L446) — 三重循环 `for bench / for arch / for script_param`，每个组合调 `create_run_command()` 生成 `run_fpga_flow.py` 的命令行，存进 `flow_run_cmd_list`。第 440-444 行日志会打印「Found X Architectures Y Benchmarks & Z Script Parameters」和「Created total N jobs」——你在终端看到的这句就来自这里。

**组装单 job 命令行**——把 `task.conf` 的各段翻译成 `run_fpga_flow.py` 的参数：

[run_fpga_task.py:462-535](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_task.py#L462-L535) — `create_run_command()` 先放架构文件和基准文件，再依次加 `--top_module`、`--run_dir`、`--fpga_flow`，然后把 `[OpenFPGA_SHELL]` 段每个键值对都转成 `--<key> <value>`（第 498-500 行）。这就是为什么 `task.conf` 的 `[OpenFPGA_SHELL]` 段名要和 `run_fpga_flow.py` 的命令行参数名严格对应。

**多线程执行**：

[run_fpga_task.py:603-615](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_task.py#L603-L615) — `run_actions()` 给每个 job 起一个线程跑 `run_single_script`，由 `Semaphore(maxthreads)` 限制并发；最后 `join` 等全部结束。`run_single_script`（第 554-600 行）内部用 `subprocess.Popen` 调 `run_fpga_flow.py`。

**结果汇总**：

[run_fpga_task.py:618-656](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_task.py#L618-L656) — `collect_results()` 读每个 job 目录下的 `vpr_stat.result`，把关键路径延迟、布线面积等指标合并，写到任务目录的 `task_result.csv`。

#### 4.3.4 代码实践

1. **实践目标**：在不真正跑完流程的前提下，看清一个任务会展开成哪些 job。
2. **操作步骤**：
   - 执行 `run-task basic_tests/full_testbench/configuration_chain --test_run`。
   - `--test_run` 是干跑模式：它走 `generate_each_task_actions` 展开 job 清单，但**不真正执行**，而是用 `pprint.pprint(job_run_list)` 把每个 job 的命令行打印出来（见 [run_fpga_task.py:129-134](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_task.py#L129-L134)）。
3. **需要观察的现象**：终端打印出一个列表，里面有 3 个 job（对应 `and2`/`or2`/`and2_latch`），每个 job 的 `commands` 字段是一条完整的 `run_fpga_flow.py` 命令，`run_dir` 字段形如 `.../run001/k4_N4_tileable_40nm/and2/MIN_ROUTE_CHAN_WIDTH`。
4. **预期结果**：确认 job 数 = 3，且每个 job 的 `run_dir` 三段命名（架构名 / top 模块 / 脚本参数标签）正确。如果输出里 job 数不是 3，回头核对 `task.conf` 的三段计数。

#### 4.3.5 小练习与答案

**练习 1**：为什么调度器要用 `Semaphore(maxthreads)` 而不是给每个 job 直接起一个线程同时跑？

**答案**：因为一个任务可能展开出几十甚至上百个 job（大任务架构多、基准多），如果全部同时跑，会瞬间占满 CPU 和内存，反而更慢甚至把机器拖垮。`Semaphore(maxthreads)` 把同时运行的 job 数限制在 `--maxthreads`（默认 2），剩下的排队等空位，在「并行加速」和「资源占用」之间取得平衡。

**练习 2**：`run001` 这个编号是怎么决定的？如果我删掉 `run001` 只留 `run003`，下次跑会生成几号？

**答案**：编号取「已有 `run*[0-9]` 目录里的最大编号 + 1」（[run_fpga_task.py:227-236](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_task.py#L227-L236)）。所以删掉 `run001` 只留 `run003` 后，下次会生成 `run004`，而不会复用 `run001`——它只看最大值，不补空缺。

---

### 4.4 run_fpga_flow.py：单次设计流的执行器

#### 4.4.1 概念说明

`run_fpga_flow.py` 处理**单个**「一个架构 + 一个基准」组合，把 u1-l1 讲的四阶段流程真正跑完。调度器给它传一堆命令行参数（架构文件、基准文件、top 模块、各种开关），它依次完成：

1. **综合**：调 Yosys 把 Verilog 综合成 `.blif` 网表（`run_yosys_with_abc`）。
2. **驱动 openfpga shell**：把任务指定的 `.openfpga` 模板做变量替换，再调 `openfpga -batch -f <run.openfpga>`。

要特别强调一个容易混淆的点：**fabric 构建、比特流生成、Verilog/SDC 网表输出，都不是 `run_fpga_flow.py` 用 Python 实现的，而是由 `openfpga` 二进制执行那条 `.openfpga` 脚本完成的**。`run_fpga_flow.py` 只是个「编排者」——它跑 Yosys，然后把剩下的事全权交给 openfpga shell。这正好对应 u2 将要讲的：openfpga 的所有能力都通过 shell 命令暴露。

#### 4.4.2 核心流程

`run_fpga_flow.py` 的 `main()` 主干（`fpga_flow=yosys_vpr` 分支）：

```
main()
  ├─ read_script_config()          ← 读 CAD 工具路径（yosys/openfpga 在哪）
  ├─ validate_command_line_arguments()  ← 校验架构/基准文件存在
  ├─ prepare_run_directory(run_dir) ← 建 arch/、benchmark/ 子目录，复制文件，做 ${PATH:...} 替换
  │
  ├─ [fpga_flow == "yosys_vpr"]
  │    ├─ run_yosys_with_abc()       ← Yosys 综合 → top_yosys_out.blif
  │    └─ run_rewrite_verilog()      ← 生成综合后 Verilog（供后续自检对照）
  │
  ├─ run_openfpga_shell()           ← 模板替换 + 调 openfpga -batch -f
  │    └─ （在 openfpga 内部：vpr 布局布线 → build_fabric → 比特流 → write_fabric_verilog → ...）
  │
  └─ （可选）run_netlists_verification()  ← 用 iverilog 跑 testbench 自检
```

#### 4.4.3 源码精读

**主函数主干**：

[run_fpga_flow.py:342-399](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_flow.py#L342-L399) — `main()` 的骨架：读配置 → 校验 → 建目录 → 跑 yosys_vpr 流程 → 跑 openfpga shell → 统计耗时。

**`yosys_vpr` 分支**——综合阶段：

[run_fpga_flow.py:349-369](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_flow.py#L349-L369) — 先 `run_yosys_with_abc()` 综合；若开了功耗分析再跑 ACE2，否则把 `.blif` 拷贝成 VPR 需要的名字；最后 `run_rewrite_verilog()`。第 368-369 行 `run_openfpga_shell()` 才是重头戏。

**驱动 openfpga shell**——本节最关键的一段：

[run_fpga_flow.py:1021-1060](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_flow.py#L1021-L1060) — `run_openfpga_shell()` 读 `top_module + "_template.openfpga"` 模板，把 `${VPR_ARCH_FILE}`、`${OPENFPGA_ARCH_FILE}`、`${TOP_MODULE}` 等变量填进去（`safe_substitute`），写到 `top_module + "_run.openfpga"`，然后用 `[cad_tools["openfpga_shell_path"], "-batch", "-f", ...]` 调起 `openfpga` 二进制。注意第 1057 行的 `-batch -f`：批处理模式执行脚本，这正是 u2-l1 会讲的 openfpga 三种运行模式之一。

**运行目录准备**——解释了 `task.conf` 里的 `${PATH:OPENFPGA_PATH}` 是何时被替换的：

[run_fpga_flow.py:654-703](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_flow.py#L654-L703) — `prepare_run_directory()` 在 `run_dir` 下建 `arch/` 和 `benchmark/` 子目录，把架构 XML 与基准 Verilog 拷进去；第 671-682 行用 `Template(...).safe_substitute(script_env_vars["PATH"])` 把架构文件里的 `${PATH:...}` 变量替换成真实路径后另存。这一步保证了即便原始 arch 文件含变量占位符，落到 run 目录的也是可被 openfpga 直接读取的「干净」文件。

**任务默认调用的 shell 模板**——这就是 `openfpga -batch -f` 真正执行的脚本，里面是 u4-l1 会逐行讲的命令链：

[write_full_testbench_example_script.openfpga:1-68](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/write_full_testbench_example_script.openfpga#L1-L68) — 从 `vpr`（布局布线）开始，依次 `read_openfpga_arch` → `link_openfpga_arch` → `build_fabric` → `repack` → `build_architecture_bitstream` → `build_fabric_bitstream` → `write_fabric_bitstream` → `write_fabric_verilog` → `write_full_testbench` → `write_pnr_sdc` 等。本讲你只需建立印象：**这一整条命令链才是「fabric 网表 + 比特流」的真正生产者**，后续 u4~u8 会逐条拆解。

#### 4.4.4 代码实践

1. **实践目标**：看清「模板替换」这件事真的发生了——同一个 `.openfpga` 模板，替换前后的差异。
2. **操作步骤**：
   - 先看模板原文件：`openfpga_flow/openfpga_shell_scripts/write_full_testbench_example_script.openfpga`，注意第 3、6、13、55 行的 `${VPR_ARCH_FILE}`、`${OPENFPGA_ARCH_FILE}`、`${ACTIVITY_FILE}`、`${REFERENCE_VERILOG_TESTBENCH}` 等占位符。
   - 跑一遍 4.3.4 的任务（或综合实践的 `run-task`），跑完后用 `goto-task` 进入 `and2` 的 run 目录。
   - 打开该目录下的 `and2_run.openfpga`（由 [run_fpga_flow.py:1055-1056](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_flow.py#L1055-L1056) 生成）。
3. **需要观察的现象**：`and2_run.openfpga` 与模板内容几乎一样，但所有 `${...}` 占位符都变成了真实路径（如 `./arch/k4_N4_tileable_40nm.xml`、`and2_ace_out.act` 等）。
4. **预期结果**：替换后的脚本里不再有 `${VPR_ARCH_FILE}` 这样的占位符。这印证了「模板 + 变量替换 = 实际执行的脚本」。如果还残留 `${...}`，说明该变量没在 `script_env_vars["PATH"]` 里提供（`safe_substitute` 会原样保留未定义变量），可对照 [run_fpga_flow.py:1026-1048](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_flow.py#L1026-L1048) 排查。

#### 4.4.5 小练习与答案

**练习 1**：为什么说 `run_fpga_flow.py` 本身并不「生成 fabric 网表」？

**答案**：因为生成网表的命令（`build_fabric`、`write_fabric_verilog` 等）全在 `.openfpga` 脚本模板里，由 `openfpga` 二进制执行。`run_fpga_flow.py` 只负责跑 Yosys 综合、替换模板变量、然后用 `-batch -f` 调起 `openfpga` 去执行那条脚本。它是个编排者，真正的活儿在 openfpga shell 内部。

**练习 2**：CAD 工具（yosys、openfpga）的可执行文件路径，`run_fpga_flow.py` 是从哪里读到的？

**答案**：从 `--default_tool_path` 指向的 `fpgaflow_default_tool_path.conf` 读到的（[run_fpga_flow.py:548-559](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_flow.py#L548-L559) 的 `read_script_config`）。那个文件里 `[CAD_TOOLS_PATH]` 段把 `openfpga_shell_path`、`yosys_path` 等都指向 `${OPENFPGA_PATH}/build/...`——也就是上一讲 `make compile` 产出的二进制所在位置。这就是为什么必须先编译、再跑流程。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次真实的端到端运行。

**任务**：跑通 `basic_tests/full_testbench/configuration_chain`，追踪一个 `and2` 从 Verilog 到比特流的完整产出，并在结果目录里找到 fabric 网表与比特流。

**操作步骤**：

1. 准备环境（在仓库根目录）：
   ```bash
   source openfpga.sh
   ```
   确认 `echo $OPENFPGA_PATH` 指向仓库根目录（4.1）。

2. 先干跑，预览 job 清单（4.3）：
   ```bash
   run-task basic_tests/full_testbench/configuration_chain --test_run
   ```
   确认打印出 3 个 job、每个 job 的 `run_dir` 三段命名正确。

3. 正式跑（默认 2 线程；机器多核可加 `--maxthreads 3`）：
   ```bash
   run-task basic_tests/full_testbench/configuration_chain --maxthreads 3
   ```
   终端会打印「Created total 3 jobs」，然后逐个 job 输出 Yosys、openfpga shell 的日志。每个 job 受 `task.conf` 里 `timeout_each_job = 20*60`（20 分钟）保护。

4. 跑完后进入 `and2` 的结果目录（4.1 的 `goto-task`）：
   ```bash
   goto-task basic_tests/full_testbench/configuration_chain
   ```
   它会先 `cd` 到 `latest/runXXX`，再弹出一个 `select` 菜单列出各基准的 `arch` 子目录，选 `and2` 那一项进入。

5. 在 `and2` 的 run 目录里验收产物（4.4 模板里 `write_*` 命令的输出）：
   - `fabric_bitstream.bit` —— 配置比特流（`write_fabric_bitstream` 产出）。
   - `fabric_independent_bitstream.xml` —— 与 fabric 无关的比特流（`build_architecture_bitstream --write_file` 产出）。
   - `SRC/` 目录 —— fabric Verilog 网表与 testbench（`write_fabric_verilog`、`write_full_testbench` 产出）。
   - `SDC/`、`SDC_analysis/` —— 时序约束（`write_pnr_sdc`、`write_analysis_sdc` 产出）。
   - `and2_run.openfpga` —— 替换好变量的实际执行脚本（4.4.4 实践的对照物）。
   - `openfpgashell.log` —— openfpga shell 的完整运行日志。

**需要观察的现象**：

- 步骤 3 终端出现 `Openfpga_flow completed, Total Time Taken ...`，且没有 `Failed to run ... task` 报错。
- 步骤 5 能在目录里看到上述文件，且 `SRC/` 下有 `fabric_netlists.v` 之类的网表文件。

**预期结果**：

- 3 个 job 全部成功，任务目录下生成 `task_result.csv`（4.3 的 `collect_results` 产出）。
- `and2` 的 run 目录里同时存在比特流（`.bit`）、Verilog 网表（`SRC/`）、SDC 约束（`SDC/`）三类产物——它们正是 u1-l1 所说的「OpenFPGA 的输出」。

**如果失败**：先看 `openfpgashell.log` 末尾的报错；最常见原因是 `make compile` 没做完整（`build/openfpga/openfpga` 不存在），此时 `fpgaflow_default_tool_path.conf` 里指向的二进制找不到。可回到 u1-l3 重新编译。

> 说明：本实践涉及真实 Yosys/VPR/openfpga 运行，耗时取决于机器（小设计通常每个 job 数十秒到几分钟）。若运行结果与预期不符，请以上述日志文件为准排查，标注「待本地验证」的步骤以你本机实际现象为准。

## 6. 本讲小结

- `openfpga.sh` 是供 `source` 的脚本，它定义 `run-task`/`run-flow`/`goto-task` 等函数并导出 `OPENFPGA_PATH`，是用户与流程脚本之间的「快捷层」。
- 一个任务 = 一个含 `config/task.conf` 的目录；`task.conf` 用 INI 段落描述「架构 × 基准 × 脚本参数」，job 数 = 三者的笛卡尔积。
- `run_fpga_task.py` 是批量调度器：读 `task.conf` → 展开 job → 多线程（`Semaphore(maxthreads)` 限流）执行 → 汇总 `task_result.csv`；并用 `run001/run002/...` + `latest` 软链接组织多次运行结果。
- `run_fpga_flow.py` 是单次流程执行器：跑 Yosys 综合，再用模板替换 + `openfpga -batch -f` 把后续的 fabric 构建/比特流/网表生成交给 openfpga shell。
- fabric 网表与比特流**不是** Python 脚本生成的，而是 `openfpga` 二进制执行 `.openfpga` 模板（如 `write_full_testbench_example_script.openfpga`）里的命令链生成的——这是理解后续 u4~u8 的关键。
- 跑通一个 `micro_benchmark` 后，结果落在 `runXXX/<架构名>/<top模块>/<脚本参数标签>/` 下，用 `goto-task` 可快速进入。

## 7. 下一步学习建议

- **下一步学 u2**（OpenFPGA Shell 入口与命令）：本讲反复提到的 `openfpga -batch -f`、`.openfpga` 脚本里的 `build_fabric`/`write_fabric_verilog` 等命令，正是 u2 的主题。学完 u2 你就能看懂 `write_full_testbench_example_script.openfpga` 里每条命令的含义。
- **接着学 u3**（架构描述与输入文件）：本讲出现的两套架构文件（`vpr_arch/k4_N4_tileable_40nm.xml` 与 `openfpga_arch/k4_N4_40nm_cc_openfpga.xml`）的职责边界、`cc` 配置协议的含义，u3 会系统讲解。
- **延伸阅读源码**：
  - 想理解 `task.conf` 的 `${PATH:...}` 如何被解析，可读 `run_fpga_task.py:99-117` 的 `script_env_vars` 与 `run_fpga_task.conf`。
  - 想理解 CAD 工具路径如何定位，可读 `openfpga_flow/misc/fpgaflow_default_tool_path.conf`。
  - 想提前感受完整命令链，可通读 `openfpga_flow/openfpga_shell_scripts/example_script.openfpga`（u4-l1 会逐行解析）。

# 任务、基准测试与回归测试体系

## 1. 本讲目标

在 u1-l4 里，我们已经跑通了第一个任务 `run-task basic_tests/full_testbench/configuration_chain`，但当时把「任务是怎么被切成多个 job 的」「并行是怎么调度的」「基准设计放在哪」这些机制当作黑盒跳过了。本讲把这只黑盒打开。读完本讲你应该能够：

- 说清一个「任务（task）」在文件系统里长什么样，`task.conf` 的每一段各管什么。
- 解释 `run_fpga_task.py` 如何用「架构 × 基准 × 脚本参数」的笛卡尔积把一个任务拆成多个 job，并并行调度它们。
- 定位基准 Verilog 设计所在的目录，看懂一个最简基准（`and2`）的结构。
- 理解 OpenFPGA 的回归测试体系：`basic_tests/` 各子目录覆盖了哪些特性（`full_testbench`、`fabric_key`、`clock_network`、`k4_series` 下的 `busmux` 等），`.github/workflows/build.yml` 如何批量跑这些回归脚本，以及本次「Bus Based MUX」特性是如何落地成回归任务的。
- 能够照着一个现成 `task.conf` 复制、改一个参数（换一份 `openfpga_arch`），跑出一个属于自己的小任务。

## 2. 前置知识

本讲默认你已经掌握 u1-l4 的内容，尤其是以下三个结论（没掌握也没关系，下面会用到时再回看）：

1. **三层调用链**：`openfpga.sh` 里的快捷函数（`run-task` 等）→ 批量调度器 `run_fpga_task.py` → 单次流程执行器 `run_fpga_flow.py` → `openfpga -batch -f` 执行 `.openfpga` 脚本。
2. **一个任务 = 一个含 `config/task.conf` 的目录**，由 INI 段落描述。
3. **配置协议 `cc` 表示 scan_chain（配置链）**，这是 `task.conf` 通过 `openfpga_arch_file` 间接选定的。

本讲几乎不涉及 C++ 源码，主角是 Python 调度脚本、INI 配置、Verilog 基准和 bash 回归脚本，适合作为理解整个流程「编排层」的一讲。几个基础概念先说清：

- **INI 文件**：分段式的文本配置，形如 `[SECTION]` 下面跟 `key = value`。OpenFPGA 的 `task.conf` 用 Python 标准库 `configparser` 解析。
- **笛卡尔积（Cartesian product）**：把多个集合两两组合的所有结果。例如 2 种架构 × 3 个基准 = 6 个组合。本讲会用它来解释 job 数量。
- **回归测试（regression test）**：每次代码改动后，把一组已知「应该通过」的设计流再跑一遍，确保新改动没有破坏旧功能。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `openfpga_flow/scripts/run_fpga_task.py` | 批量任务调度器：读 `task.conf`、切 job、并行跑、汇总结果。本讲的主角。 |
| `openfpga_flow/scripts/run_fpga_task.conf` | 调度器自己的全局配置（任务根目录、默认脚本路径等）。 |
| `openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf` | 一个真实的任务配置，本讲拿它当样板逐段拆解。 |
| `openfpga_flow/tasks/basic_tests/k4_series/k4n4_frac_mult_busmux/config/task.conf` | 本次更新新增的「总线型 mux」任务，演示如何用一个 task 把新特性纳入回归。 |
| `openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/config/task.conf` | 配套的「黄金比特流分布」任务：把 busmux 的共享配置位锁定为黄金产出，靠 `git diff` 守护。 |
| `openfpga_flow/benchmarks/micro_benchmark/and2/and2.v` | 最简基准设计：一个 2 输入与门。 |
| `openfpga.sh` | 定义 `run-task`、`create-task`、`goto-task`、`run-regression-local` 等快捷函数。 |
| `openfpga_flow/regression_test_scripts/basic_reg_test.sh` | 一份真实的回归脚本，串起几十个 `run-task` 调用。 |
| `.github/workflows/build.yml` | CI 配置，矩阵式批量触发回归脚本。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**task.conf 任务模型**、**run_fpga_task.py 调度**、**benchmarks 基准**、**回归测试**。它们的关系是：`task.conf` 描述「要跑什么」→ 调度器把它切成 job 并行跑 → job 的输入来自 benchmarks → 回归测试把成百上千个任务组织起来反复跑。

---

### 4.1 task.conf 任务模型

#### 4.1.1 概念说明

一个「任务」在 OpenFPGA 里就是一个目录，目录里必须有 `config/task.conf`。这个 INI 文件描述了「用哪些架构、跑哪些基准、用哪些脚本参数」。调度器会把这些维度做笛卡尔积，每一份组合就是一个 **job**——一次完整的「Verilog → 比特流」流程。

`task.conf` 的段落是强制与可选并存的：`run_fpga_task.py` 会检查 `GENERAL`、`BENCHMARKS`、`ARCHITECTURES` 三段必须存在，缺一段就直接报错退出。

#### 4.1.2 核心流程

一份 `task.conf` 的典型段落及其用途：

| 段落 | 作用 | 是否必需 |
| --- | --- | --- |
| `[GENERAL]` | 全局开关：流程类型、是否做功耗分析、是否输出 SPICE/Verilog、每个 job 的超时。 | 必需 |
| `[OpenFPGA_SHELL]` | 喂给 openfpga shell 的参数：`.openfpga` 模板、`openfpga_arch`、仿真设置、设备布局等。 | 视流程而定 |
| `[ARCHITECTURES]` | VPR 架构 XML 列表（`arch0`、`arch1`…），每个是一份器件结构描述。 | 必需 |
| `[BENCHMARKS]` | 基准 Verilog 列表（`bench0`、`bench1`…），每个是待实现的设计。 | 必需 |
| `[SYNTHESIS_PARAM]` | 综合参数：每个基准的顶层模块名、通道宽度、yosys 选项等。 | 必需（被代码引用） |
| `[SCRIPT_PARAM_*]` | 流程脚本参数集，每多一段就多一个「参数维度」。 | 可选 |

job 总数 = 架构数 × 基准数 × 脚本参数集数。用数学式写就是：

\[
N_{\text{job}} = N_{\text{arch}} \times N_{\text{bench}} \times N_{\text{script\_param}}
\]

#### 4.1.3 源码精读

先看样板任务 `configuration_chain/config/task.conf` 的各段。`[GENERAL]` 段确定流程基调：`fpga_flow=yosys_vpr` 表示「先用 yosys 综合、再用 VPR 布局布线」，`timeout_each_job = 20*60` 是每个 job 的超时秒数（这里是 1200 秒）：

[openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf:L9-L16](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf#L9-L16) — `[GENERAL]` 段：流程类型 `yosys_vpr`、功耗分析开启、Verilog 输出开启、每个 job 超时。

`[OpenFPGA_SHELL]` 段最有信息量：它通过 `openfpga_arch_file` 指向 `k4_N4_40nm_cc_openfpga.xml`（注意文件名里的 `cc` = configuration chain），并通过 `openfpga_shell_template` 指定要执行的 `.openfpga` 模板脚本：

[openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf:L18-L23](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf#L18-L23) — `[OpenFPGA_SHELL]` 段：选定 cc 版 `openfpga_arch` 与 `write_full_testbench_example_script.openfega` 模板。

`[ARCHITECTURES]` 与 `[BENCHMARKS]` 各列出一个（这里各一个/三个），是笛卡尔积的两个维度：

[openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf:L25-L31](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf#L25-L31) — 一份 VPR 架构 `arch0`，三个基准 `bench0`/`bench1`/`bench2`。

`[SYNTHESIS_PARAM]` 段把基准与综合参数对齐：`bench0_top = and2` 表示 `bench0` 的顶层模块是 `and2`，`bench0_chan_width = 300` 给它指定布线通道宽度。前缀 `bench0_` 与 `[BENCHMARKS]` 里的键 `bench0` 严格对应：

[openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf:L33-L42](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf#L33-L42) — 每个基准的顶层模块名与通道宽度，靠 `benchN_` 前缀与基准对齐。

最后是 `[SCRIPT_PARAM_MIN_ROUTE_CHAN_WIDTH]`。注意它的段名以 `SCRIPT_PARAM` 开头——调度器正是靠「段名里含不含 `SCRIPT_PARAM`」来识别它是一个脚本参数集（详见 4.2.3）。这里的 `end_flow_with_test=` 是一个空值参数，会被当作 `--end_flow_with_test` 开关传下去：

[openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf:L44-L45](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/full_testbench/configuration_chain/config/task.conf#L44-L45) — 一个名为 `MIN_ROUTE_CHAN_WIDTH` 的脚本参数集。

按上面的公式，这个任务的 job 总数 = 1 架构 × 3 基准 × 1 脚本参数集 = **3 个 job**。

> 小贴士：`task.conf` 里反复出现 `${PATH:OPENFPGA_PATH}`，这是 `configparser` 的 `ExtendedInterpolation`（扩展插值）语法，会在解析时被替换成 `OPENFPGA_PATH` 环境变量的值。这跟 `.openfpga` 脚本里的 `${}`（Python `string.Template`）是两套不同的替换机制，别混了——前者发生在调度器读 `task.conf` 时，后者发生在 `run_fpga_flow.py` 填模板时。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「缺必需段会直接报错」这一行为，并亲手数出 job 数。

**步骤**：

1. 打开 `run_fpga_task.py`，定位必需段检查（见下条链接）。
2. 回到上面的 `configuration_chain/config/task.conf`，用公式 \(N_{\text{arch}}\times N_{\text{bench}}\times N_{\text{script\_param}}\) 算出 job 数。
3. 对照 4.2.3 里调度器打印的 `Created total %d jobs` 日志验证。

**预期结果**：算出来是 3 个 job。

**待本地验证**：跑一遍 `run-task basic_tests/full_testbench/configuration_chain --debug --show_thread_logs`，在日志里找 `Found X Architectures Y Benchmarks & Z Script Parameters` 与 `Created total N jobs` 两行，确认 N=3。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `[ARCHITECTURES]` 里再加一行 `arch1=...另一份 vpr_arch.xml`，job 数会变成多少？
**答案**：变成 1×3→2×3 = 6 个 job。架构维度从 1 变 2，job 数线性增长。

**练习 2**：`bench0_top = and2` 里的 `bench0` 前缀如果写成 `bench00`，会发生什么？
**答案**：调度器在 `[SYNTHESIS_PARAM]` 里是按 `bech_name in eachKey` 来匹配的，`bench00_top` 不会被 `bench0` 命中，于是 `bench0` 的顶层模块会 fallback 到默认值 `top`（见 `run_fpga_task.py` 的 `SynthSection.get(bech_name + "_top", fallback="top")`），综合时找不到 `top` 模块而报错。前缀必须严格对齐。

---

### 4.2 run_fpga_task.py 调度

#### 4.2.1 概念说明

`run_fpga_task.py` 是一个独立的 Python 调度器（不依赖 openfpga 二进制本身）。它做四件事：解析 `task.conf` → 切出 job 列表 → 用线程池并行跑每个 job（每个 job 调一次 `run_fpga_flow.py`）→ 把每个 job 的性能结果汇总成 CSV。它的所有行为都可以脱离 FPGA 知识来理解：本质是一个「配置驱动的批处理器」。

它从命令行接收一个或多个任务路径（相对 `OPENFPGA_TASK_PATH` 的路径），最重要的开关是 `--maxthreads`（并行线程数）。

#### 4.2.2 核心流程

调度器的整体流程可以用伪代码概括：

```
main():
    对每个 task:
        generate_each_task_actions(task)   # 解析 task.conf → job 列表 + 创建 run目录
        run_actions(job_list)              # 线程池并行跑每个 job
        collect_results(job_list)          # 汇总 vpr_stat.result → task_result.csv
```

其中 `generate_each_task_actions` 的关键步骤：

1. **定位任务目录**：依次在「当前目录」「绝对路径」「仓库 task 目录」三个地方找，找到则进入。
2. **创建 run 目录**：扫描已有 `runXXX`，取最大编号 +1，新建 `run%03d`（如 `run001`），并刷新 `latest` 软链接指向它。
3. **读 `task.conf`**：注入一批 `PATH:` 变量（如 `OPENFPGA_PATH`、`TASK_DIR`），再用 `ExtendedInterpolation` 解析。
4. **校验必需段**：缺 `GENERAL`/`BENCHMARKS`/`ARCHITECTURES` 即退出。
5. **三重循环切 job**：基准 × 架构 × 脚本参数集，每个组合生成一条 `run_fpga_flow.py` 命令。

并行调度用「信号量限流」的经典模式：一个 `threading.Semaphore(maxthreads)` 控制同时跑的 job 数，每个 job 一个线程，超出上限的线程在信号量上排队。

#### 4.2.3 源码精读

**入口与命令行参数**。`--maxthreads` 默认值是 2，注释提示它「通常 ≤ 机器 CPU 核数」：

[openfpga_flow/scripts/run_fpga_task.py:L60-L94](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/scripts/run_fpga_task.py#L60-L94) — 定义 `tasks`（必填，可多个）与 `--maxthreads`（默认 2）、`--remove_run_dir`、`--test_run`、`--debug`、`--continue_on_fail` 等开关。

**run 目录编号**。下面这段是 `run001`/`latest` 机制的来源：用列表推导取出所有 `run*[0-9]` 目录的末三位编号，取最大值 +1，再建新目录和软链接：

[openfpga_flow/scripts/run_fpga_task.py:L226-L246](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/scripts/run_fpga_task.py#L226-L246) — 扫描已有 run 目录、新建 `run%03d`、把 `latest` 软链接重指向新目录。

这就是为什么你每次重跑一个任务，结果都落在递增编号的 `run001`/`run002`/… 里，而 `latest` 永远指向最近一次——便于用 `goto-task` 直接跳到最新结果。

**必需段校验**：

[openfpga_flow/scripts/run_fpga_task.py:L255-L260](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/scripts/run_fpga_task.py#L255-L260) — `required_sec = ["GENERAL", "BENCHMARKS", "ARCHITECTURES"]`，缺任一段就 `clean_up_and_exit`。

**笛卡尔积切 job**。这是本讲最核心的一段：三重循环把基准、架构、脚本参数集组合起来，每份组合构造一条命令并放进 `flow_run_cmd_list`：

[openfpga_flow/scripts/run_fpga_task.py:L404-L438](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/scripts/run_fpga_task.py#L404-L438) — `for bench / for arch / for script_param` 三重循环；每份组合的 job 名形如 `00_and2_MIN_ROUTE_CHAN_WIDTH`，并记录 `run_dir`、`commands`、`finished`、`status` 等字段。

每个 job 的 `run_dir` 由 `get_flow_rundir` 决定，路径分三层：架构名 / 顶层模块名 / 脚本参数标签：

[openfpga_flow/scripts/run_fpga_task.py:L453-L459](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/scripts/run_fpga_task.py#L453-L459) — `run_dir = <arch名>/<top_module>/<script_param标签>/`，例如 `k4_N4_tileable_40nm/and2/MIN_ROUTE_CHAN_WIDTH/`。

所以一个 job 的完整输出路径长这样：`<task目录>/run001/k4_N4_tileable_40nm/and2/MIN_ROUTE_CHAN_WIDTH/`。记住这个结构，找产出文件就不会迷路。

**构造单条命令**。`create_run_command` 把 `[OpenFPGA_SHELL]` 段的每个键值对都转成 `--<key> <value>` 传给 `run_fpga_flow.py`——这就是 `task.conf` 的 `[OpenFPGA_SHELL]` 段如何「穿透」到实际流程的：

[openfpga_flow/scripts/run_fpga_task.py:L498-L500](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/scripts/run_fpga_task.py#L498-L500) — 当 `run_engine=openfpga_shell` 时，遍历 `[OpenFPGA_SHELL]` 的所有键，拼成 `--openfpga_arch_file ...`、`--openfpga_shell_template ...` 等参数。

**并行调度**。`run_actions` 用信号量限流，每个 job 起一个线程跑 `run_single_script`：

[openfpga_flow/scripts/run_fpga_task.py:L603-L615](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/scripts/run_fpga_task.py#L603-L615) — `threading.Semaphore(args.maxthreads)` 限流；逐个 job 起线程，最后 `join` 等全部完成。

每个线程内部用 `subprocess.Popen` 启动 `run_fpga_flow.py`，把输出写到以线程名命名的日志，并根据返回码标记 job 成败。注意一个细节：默认情况下（未加 `--continue_on_fail`）任意 job 失败会直接 `os._exit(1)` 整个进程：

[openfpga_flow/scripts/run_fpga_task.py:L554-L600](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/scripts/run_fpga_task.py#L554-L600) — `run_single_script`：启动子进程、流式写日志、按返回码置 `status`、失败时按 `--continue_on_fail` 决定是否立即退出。

**结果汇总**。`collect_results` 读取每个 job 的 `vpr_stat.result`（VPR 写出的统计文件），合并写进 `task_result.csv`。需要留意一个条件：仅当 `fpga_flow` 不是纯 `"yosys"` 时才汇总；并且若某 job 目录下没有 `*.result` 文件，就跳过它：

[openfpga_flow/scripts/run_fpga_task.py:L618-L656](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/scripts/run_fpga_task.py#L618-L656) — `collect_results`：逐 job 读 `vpr_stat.result`，汇总运行时间与各项指标到 `task_result.csv`。

> 小贴士：本讲的样板任务 `fpga_flow=yosys_vpr`（不是纯 `yosys`），所以 `collect_results` 会被调用；但能否真的写出 `task_result.csv`，取决于流程是否产出了 `vpr_stat.result`。`--test_run` 是个很有用的开关：它只切 job、打印命令而不真正执行，适合先看清楚调度器到底要跑什么。

#### 4.2.4 代码实践

**目标**：用 `--test_run` 干跑一次，观察调度器切出的 job 列表与 run 目录结构，不实际跑流程。

**步骤**：

1. `source openfpga.sh`。
2. 执行 `run-task basic_tests/full_testbench/configuration_chain --test_run`。
3. 观察输出（`pprint` 打印的 `job_run_list`）与新生成的 `runXXX` 目录。

**需要观察的现象**：

- 控制台会 `pprint` 出一个列表，元素个数应为 3（对应 3 个基准）。
- 每个元素的 `name` 形如 `00_and2_MIN_ROUTE_CHAN_WIDTH`、`01_or2_MIN_ROUTE_CHAN_WIDTH`、`02_and2_latch_MIN_ROUTE_CHAN_WIDTH`。
- 任务目录下会出现新的 `runXXX` 与指向它的 `latest` 软链接（即使 `--test_run` 也会创建 run 目录）。

**预期结果**：看到 3 个 job，`run_dir` 分别指向 `<arch>/and2/MIN_ROUTE_CHAN_WIDTH` 等。

**待本地验证**：由于 `--test_run` 不真正执行，`runXXX` 下不会有产出文件，只有 run 目录骨架被创建。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `run001`、`run002` 用三位数字编号？
**答案**：代码里用 `run%03d` 格式化（`"run%03d" % n`），且删除逻辑 `remove_run_dir` 用 `int(eachRun[-3:])` 取末三位解析编号。三位数保证排序与解析一致，也支持到 `run999`。

**练习 2**：`--maxthreads 1` 与默认 `--maxthreads 2` 的区别是什么？
**答案**：信号量容量变成 1，所有 job 串行执行；默认 2 表示最多 2 个 job 同时跑。job 之间没有数据依赖（各自独立的 run 目录），所以并行是安全的。

---

### 4.3 benchmarks 基准

#### 4.3.1 概念说明

基准（benchmark）就是拿来「喂」给 FPGA 流程的待实现设计，本讲里是一段段 Verilog。OpenFPGA 仓库自带了一套按规模分级的基准库，位于 `openfpga_flow/benchmarks/`。其中 `micro_benchmark/` 是最小的一批「微基准」，专门用来快速验证某项特性——一个 `and2` 只有十几行，跑完只需几秒，非常适合回归测试和教学。

#### 4.3.2 核心流程

基准如何参与流程：

1. `task.conf` 的 `[BENCHMARKS]` 段用 `bench0=/.../and2.v` 登记基准文件。
2. `[SYNTHESIS_PARAM]` 段用 `bench0_top = and2` 指定它的顶层模块名。
3. 调度器把基准路径与顶层模块名拼进 `run_fpga_flow.py` 的命令行。
4. `run_fpga_flow.py` 先用 yosys 把 Verilog 综合成网表，再交给 VPR 布局布线。

基准库的目录划分（按用途/规模）：

| 目录 | 内容 |
| --- | --- |
| `micro_benchmark/` | 微基准：and2、or2、adder、counters、FIR_filter 等小设计，跑得快，回归测试主力。 |
| `mcnc_big20/` | MCNC 大规模基准套件（20 个经典设计），常配合 ModelSim 仿真。 |
| `vtr_benchmark/` | VTR 基准套件。 |
| `iwls2005/` | IWLS 2005 基准。 |
| `MCNC_Verilog/` | MCNC 早期 Verilog 基准。 |
| `quicklogic_tests/` | Quicklogic 专用测试。 |

#### 4.3.3 源码精读

来看最简基准 `and2/and2.v`——一个 2 输入与门。整个文件就是一个 `module and2`，两个输入 `a`/`b`，一个输出 `c = a & b`：

[openfpga_flow/benchmarks/micro_benchmark/and2/and2.v:L7-L18](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/benchmarks/micro_benchmark/and2/and2.v#L7-L18) — `module and2(a,b,c)`，`assign c = a & b;`。这就是一个完整的、能被 OpenFPGA 流程吃下去的设计。

注意模块名 `and2` 与 `task.conf` 里 `bench0_top = and2`、`bench0=.../and2.v` 三处必须一致：文件名只是路径，yosys 真正找的是顶层模块名。`timescale` 行是仿真用的，对综合本身无影响。

调度器侧，基准文件在 `generate_each_task_actions` 里被解析：支持逗号分隔的多文件、支持 `glob` 通配符，找不到任何匹配文件就报错：

[openfpga_flow/scripts/run_fpga_task.py:L284-L296](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/scripts/run_fpga_task.py#L284-L296) — 遍历 `[BENCHMARKS]`，对每个基准路径做 `glob.glob`，无匹配则 `clean_up_and_exit`。

#### 4.3.4 代码实践

**目标**：在 `and2` 旁边找一个现成基准，登记进 `task.conf` 并干跑验证。

**步骤**：

1. 在 `openfpga_flow/benchmarks/micro_benchmark/` 下任选一个基准（如 `or2`、`and4`）。
2. 想象在某个 `task.conf` 的 `[BENCHMARKS]` 加 `bench3=${PATH:OPENFPGA_PATH}/openfpga_flow/benchmarks/micro_benchmark/or2/or2.v`。
3. 对应在 `[SYNTHESIS_PARAM]` 加 `bench3_top = or2` 与 `bench3_chan_width = 300`。
4. 用 `--test_run` 干跑，确认多切出一个 job。

**预期结果**：job 数 +1，新增 job 的 `name` 形如 `03_or2_MIN_ROUTE_CHAN_WIDTH`。

**待本地验证**：实际改一份本地 `task.conf` 副本后跑 `--test_run` 确认。

#### 4.3.5 小练习与答案

**练习 1**：如果 `[BENCHMARKS]` 写了一个不存在的路径，会在哪一步报错？
**答案**：在 `generate_each_task_actions` 解析基准时（上面的 `glob` 段），`glob.glob` 返回空列表，触发 `clean_up_and_exit("No files added benchmark ...")`，进程在切 job 阶段就退出，不会等到运行阶段。

**练习 2**：`bench0` 能否指向多个 Verilog 文件？
**答案**：可以。基准路径支持逗号分隔，每段都会 `glob`，结果合并成 `bench_files` 列表，最终全部拼进 `run_fpga_flow.py` 命令。这在多文件设计（如 `explicit_multi_verilog_files` 任务）里会用到。

---

### 4.4 回归测试

#### 4.4.1 概念说明

OpenFPGA 体量大、特性多，任何一处改动都可能波及很多配置组合。回归测试的思路很简单：维护一大组「已知应该通过」的任务，每次改动后重跑它们，通过即说明没破坏旧功能。这套体系分两层：

- **任务层**：`openfpga_flow/tasks/basic_tests/` 下按特性分子目录，每个子目录是一个任务，`task.conf` 各自独立。
- **编排骨**：`openfpga_flow/regression_test_scripts/*.sh`，每个脚本串起一组相关的 `run-task` 调用；CI（`.github/workflows/build.yml`）再以矩阵方式批量触发这些脚本。

#### 4.4.2 核心流程

回归脚本的模式高度统一：

```bash
#!/bin/bash
set -e
source openfpga.sh          # 加载 run-task 等函数
run-task <任务路径> $@       # $@ 把 --debug --show_thread_logs 等透传给每个任务
run-task <另一个任务路径> $@
...
```

`set -e` 保证任一 `run-task` 失败立即中止脚本（因为 `run-task` 失败时返回非零）。`$@` 让 CI 能统一加调试参数。

CI 侧，`.github/workflows/build.yml` 的 `linux_regression_tests` job 用一个矩阵（matrix）把 11 个回归脚本各跑一遍：

```
matrix.config:
  - basic_reg_yosys_only_test
  - basic_reg_test
  - fpga_verilog_reg_test
  - fpga_bitstream_reg_test
  - fpga_sdc_reg_test
  - fpga_spice_reg_test
  - micro_benchmark_reg_test
  - vtr_benchmark_reg_test
  - iwls_benchmark_reg_test
  - tcl_reg_test
```

#### 4.4.3 源码精读

先看 `basic_tests/` 按特性分的子目录（这里列出主要的几类）——它们就是回归覆盖面的一张「特性清单」：

```
basic_tests/
├── full_testbench/        # 各种配置协议（cc/frame/memory_bank/ql_memory_bank…）的 full testbench
├── preconfig_testbench/   # 跳过编程阶段的快速验证
├── clock_network/         # 可编程时钟网络架构
├── fabric_key/            # 安全/随机 fabric key
├── bus_group/             # 总线到引脚映射
├── tile_organization/     # tile 组织与 fabric tile
├── k4_series/             # K4 系列 FPGA 的各种变体（frac LUT、adder、BRAM…）
├── write_gsb/             # GSB 导出
├── io_constraints/        # IO 约束（PCF）
├── preload_unique_blocks/ # unique blocks 缓存读写
└── ...                    # 还有 mode_bit_format、module_naming、global_tile_ports 等
```

每一类下面又有十几个甚至几十个任务，对应同一特性的不同参数组合（如 `full_testbench/` 下有 `configuration_chain`、`configuration_frame`、`memory_bank`、`ql_memory_bank_flatten`、`ql_memory_bank_shift_register` 等等）。

来看一份真实回归脚本 `basic_reg_test.sh` 的开头与典型段落。它先 `source openfpga.sh`，再用 `run-task` 把一组任务串起来：

[openfpga_flow/regression_test_scripts/basic_reg_test.sh:L1-L18](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/regression_test_scripts/basic_reg_test.sh#L1-L18) — `set -e` + `source openfpga.sh`，然后逐个 `run-task basic_tests/... $@`。

注意第 11 行 `${OPENFPGA_PATH}/build/openfpga/openfpga -x "version; exit;"` 是一个冒烟测试：直接调 openfpga 二进制跑 `version`，确认可执行文件本身没坏，再开始跑任务。脚本末尾还有一段对「黄金网表（golden netlists）」做 `git diff` 的校验——确保 `no_time_stamp` 类任务下 `golden_outputs_no_time_stamp/` 里的黄金产出与仓库保存的基准逐字一致：

[openfpga_flow/regression_test_scripts/basic_reg_test.sh:L376-L389](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/regression_test_scripts/basic_reg_test.sh#L376-L389) — `git diff` 用通配路径 `no_time_stamp/*/golden_outputs_no_time_stamp/**` 检查所有 `no_time_stamp` 任务的黄金产出未被改动，变了就 `exit 1`。这是一种「输出锁定的回归测试」。

再来看 CI 如何触发这些脚本。`.github/workflows/build.yml` 的 `linux_regression_tests` job 用矩阵列出回归脚本名，关键执行步是 `source openfpga_flow/regression_test_scripts/${{matrix.config.name}}.sh`：

[.github/workflows/build.yml:L1095-L1144](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/.github/workflows/build.yml#L1095-L1144) — `linux_regression_tests` job：矩阵列出 11 个回归脚本，每个在独立容器里 `source` 对应 `.sh` 跑，失败时上传 `openfpga_flow/**/*.log`。

最后看 `openfpga.sh` 提供的两个回归相关函数。`run-task` 就是 `run_fpga_task.py` 的薄封装；`run-regression-local` 则是从仓库根目录触发本地回归（注意它指向 `.github/workflows/*reg_test.sh`，但本仓库实际的回归脚本集合在 `openfpga_flow/regression_test_scripts/` 下，CI 用的就是后者）：

[openfpga.sh:L66-L68](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga.sh#L66-L68) — `run-task` 函数：直接转发给 `run_fpga_task.py`。

[openfpga.sh:L99-L103](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga.sh#L99-L103) — `run-regression-local`：切到 `OPENFPGA_PATH` 后执行 `.github/workflows/*reg_test.sh`。

> 提醒：本仓库 `.github/workflows/` 下并没有 `*reg_test.sh` 文件（那里只有构建依赖安装脚本和 `.yml`），权威的回归脚本在 `openfpga_flow/regression_test_scripts/*.sh`。若要在本地完整复现 CI 的回归，直接 `source openfpga_flow/regression_test_scripts/basic_reg_test.sh --debug --show_thread_logs` 即可，这正是 CI 容器里跑的命令。

`create-task` 函数也值得一提——它能从一个模板任务复制出新任务，是本讲综合实践会用到的工具：

[openfpga.sh:L34-L59](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga.sh#L34-L59) — `create-task <名字> [模板]`：从 `template_tasks/` 或指定模板目录复制出一份新任务骨架。

**实战案例：一个新特性（Bus Based MUX）是如何落地成回归任务的。** 本次 HEAD（`97c06e27`，提交「Bus Based MUX (#2602)」）引入了对「总线型多路选择器」的支持：当 VPR 架构里某条 interconnect 标了 `<mux bus="true"/>` 时，OpenFPGA 不再为展开后的每一位单比特 mux 各配一个配置位，而是让整条 bus 共享**同一个配置存储器（一个共享 config bit）**。这个特性被设计成**两个**回归任务，正好是「新特性→回归覆盖」的标准范本，值得逐行看：

第一个任务 `basic_tests/k4_series/k4n4_frac_mult_busmux` 是「能跑通」的功能性回归——它复用 32 位可分数乘法器（frac_mult）那套架构，只把 `mult_32x32_slice` 的 `a2a` 互连换成 32 位宽的 bus mux，配上最小的 `and2` 基准，确认 fabric 能正确构建、比特流能正常生成。它在 `basic_reg_test.sh` 里紧贴在原 `k4n4_frac_mult` 之后，只多两行：

[openfpga_flow/regression_test_scripts/basic_reg_test.sh:L193-L196](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/regression_test_scripts/basic_reg_test.sh#L193-L196) — 一句 `echo -e` 说明 + 一行 `run-task basic_tests/k4_series/k4n4_frac_mult_busmux $@`，把新任务挂进回归链。

[openfpga_flow/tasks/basic_tests/k4_series/k4n4_frac_mult_busmux/config/task.conf:L1-L9](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/k4_series/k4n4_frac_mult_busmux/config/task.conf#L1-L9) — 文件头注释直接讲清了这个任务在测什么：32 个单比特 mux 必须共享一个配置位，而不是 32 个独立配置位。注意它的 `[ARCHITECTURES]` 指向带 `busmux` 后缀的 VPR 架构 `k4_frac_N4_tileable_adder_chain_mem1K_frac_dsp32_busmux_40nm.xml`，`[OpenFPGA_SHELL]` 则复用 `frame` 版 `openfpga_arch`——这正是 u3-l1 讲过的「同名绑定」：bus 属性在 VPR arch 侧，共享存储器电路在 openfpga_arch 侧。

第二个任务 `basic_tests/no_time_stamp/frac_dsp_busmux` 是更严格的「输出锁定」回归——它不满足于「跑通」，而是把「共享配置位」这个关键事实**固化成黄金产出**并用 `git diff` 守护。它的文件头注释把这个策略写得非常清楚：

[openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/config/task.conf:L1-L12](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/config/task.conf#L1-L12) — 注释说明：`bitstream_distribution.xml` 记录每个块的配置位数，一旦代码退回「32 个独立配置位」，DSP 网格块的位数就会改变，CI 的 `git diff` 立刻捕获并失败。

它的关键一行是 `[OpenFPGA_SHELL]` 段里的 `openfpga_output_dir` 指向任务目录下的 `golden_outputs_no_time_stamp`：

[openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/config/task.conf:L23-L28](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/config/task.conf#L23-L28) — 用 `report_bitstream_distribution_no_time_stamp_example_script.openfpga` 模板（禁用时间戳，使输出可复现），并把输出重定向到 `golden_outputs_no_time_stamp`，使黄金文件就落在会被 `git diff` 扫描的路径上。

这个任务在 `basic_reg_test.sh` 的 `no_time_stamp` 段里同样只占两行：

[openfpga_flow/regression_test_scripts/basic_reg_test.sh:L365-L366](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/regression_test_scripts/basic_reg_test.sh#L365-L366) — `run-task basic_tests/no_time_stamp/frac_dsp_busmux $@`，紧接着就会被脚本末尾那段 `git diff`（见上面 4.4.3 的黄金网表校验）覆盖。

**值得偷师的 `.gitignore` 技巧**：`golden_outputs_no_time_stamp/` 目录会被 `write_fabric_verilog` 写入大量网表文件，但只有三个文件是「黄金基准」，其余都该忽略。该目录下的 `.gitignore` 用「先全忽略、再白名单」的手法精确锁定这三个文件：

[openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/golden_outputs_no_time_stamp/.gitignore:L1-L17](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/golden_outputs_no_time_stamp/.gitignore#L1-L17) — `/*` 先忽略目录下一切，再用 `!/bitstream_distribution.xml`、`!/fabric_bitstream.xml`、`!/lb/.../mult_32x32_slice.v` 三条白名单放行。`fabric_bitstream.xml` 用 `--path_only`（只存路径不存数值），使共享存储器「出现一次、被 32 条 mux 路径引用」这一结构在 diff 中稳定可见。

> 小结：一个新特性落地成回归的标准动作是「**一个能跑通的功能任务 + 一个锁定关键产出的 no_time_stamp 任务 + 回归脚本里各加一行 `run-task`**」。busmux 正是这套范式的最新实例，也是本讲「回归测试」模块最值得照着抄的模板。

#### 4.4.4 代码实践（源码阅读型）

**目标**：搞清某项特性（例如 frame_based 配置协议）由哪些回归任务覆盖。

**步骤**：

1. 在 `basic_reg_test.sh` 里搜索 `frame`，列出所有相关 `run-task` 行。
2. 对应到 `openfpga_flow/tasks/basic_tests/full_testbench/` 下找出它们的 `task.conf`。
3. 对比 `configuration_frame` 与 `configuration_chain` 两个任务，找出唯一区别。

**预期结果**：会发现 `configuration_frame/config/task.conf` 与 `configuration_chain/config/task.conf` 几乎逐字相同，唯一实质区别在第 20 行——`openfpga_arch_file` 从 `k4_N4_40nm_cc_openfpga.xml` 换成了 `k4_N4_40nm_frame_openfpga.xml`。对应地，这两份 arch 的 `configuration_protocol` 一份是 `scan_chain`+`DFF`，一份是 `frame_based`+`LATCH`：

[openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml:L162-L164](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml#L162-L164) — cc 版：`<organization type="scan_chain" circuit_model_name="DFF"/>`。

[openfpga_flow/openfpga_arch/k4_N4_40nm_frame_openfpga.xml:L171-L173](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/openfpga_arch/k4_N4_40nm_frame_openfpga.xml#L171-L173) — frame 版：`<organization type="frame_based" circuit_model_name="LATCH"/>`。

这正是下一节综合实践的切入点。

#### 4.4.5 小练习与答案

**练习 1**：回归脚本里的 `$@` 起什么作用？为什么每个 `run-task` 都带它？
**答案**：`$@` 把脚本被调用时的所有参数（如 CI 传的 `--debug --show_thread_logs`）透传给每个 `run-task`。这样 CI 能统一控制所有任务的日志粒度，而脚本本身不必为每个任务硬编码这些开关。

**练习 2**：为什么 `basic_reg_test.sh` 末尾要 `git diff` 黄金网表？
**答案**：`no_time_stamp` 类任务刻意禁用输出里的时间戳，使输出可复现。仓库里保存了一份「黄金输出」，回归时若生成内容与黄金输出有差异，`git diff` 就会捕获并 `exit 1`，从而发现「代码改动悄悄改变了网表」这类回归。这是一种比「流程能否跑通」更严格的正确性校验。

**练习 3**：本次新增的 `frac_dsp_busmux` 任务为什么放在 `no_time_stamp/` 下、并且只把 `bitstream_distribution.xml` 等三个文件作为黄金产出，而不是整个 fabric 网表？
**答案**：bus mux 的核心不变量是「32 个单比特 mux 共享一个配置位」，这个事实最直接、最稳定地反映在 `bitstream_distribution.xml` 里 DSP 块的配置位**计数**上——若代码退回 32 个独立配置位，这个计数立刻变化。锁定这一个计数（外加 `fabric_bitstream.xml` 的路径结构与 DSP tile 网表）就足以守住不变量，又不会被无关的网表细节（每次都可能微调）误伤。把整个 fabric 网表都当黄金会过于脆弱（任何无关改动都触发 diff），所以用 `.gitignore` 白名单只锁关键产物——这是「精准锁定」回归的权衡。

---

## 5. 综合实践

**任务**：复制 `configuration_chain` 任务，改造成一个使用 frame 版 `openfpga_arch` 的小任务，跑通后验证产出，并与仓库里已有的 `configuration_frame` 任务对照（你实际上在重现它）。

**为什么做这个**：它把本讲四个模块串起来——读 `task.conf`（4.1）、理解调度（4.2）、用基准 `and2`（4.3）、对照回归任务（4.4）。而 cc→frame 的切换正好呼应 u3-l4 学过的配置协议，让你看到「改一行 `task.conf` 如何换掉整套配置存储电路」。

**步骤**：

1. **准备环境**（前置 u1-l3、u1-l4）：`source openfpga.sh`，确认 `openfpga` 已编译。

2. **复制任务**：用 `create-task` 从现成模板复制一份到本地（避免污染仓库的任务目录）：

   ```bash
   create-task my_frame_task basic_tests/full_testbench/configuration_chain
   ```
   这会在当前目录创建 `my_frame_task/`，内含一份与 `configuration_chain` 完全相同的 `config/task.conf`。

3. **改一行**：编辑 `my_frame_task/config/task.conf`，把 `[OpenFPGA_SHELL]` 段里的

   ```
   openfpga_arch_file=${PATH:OPENFPGA_PATH}/openfpga_flow/openfpga_arch/k4_N4_40nm_cc_openfpga.xml
   ```
   改成 frame 版：
   ```
   openfpga_arch_file=${PATH:OPENFPGA_PATH}/openfpga_flow/openfpga_arch/k4_N4_40nm_frame_openfpga.xml
   ```
   同时建议把 `openfpga_shell_template` 仍用 `write_full_testbench_example_script.openfpga`（保持与原任务一致）。基准保持 `and2`/`or2`/`and2_latch` 不变。

4. **干跑验证**（4.2 的技巧）：先确认 job 切分正确：

   ```bash
   run-task my_frame_task --test_run
   ```
   预期看到 3 个 job，`name` 形如 `00_and2_MIN_ROUTE_CHAN_WIDTH`。

5. **正式跑**：

   ```bash
   run-task my_frame_task --debug --show_thread_logs
   ```

6. **定位产出**：用 `goto-task my_frame_task` 跳进最新 run 目录，再选 `and2`。结合 4.2.3 学过的路径规则，最终进入：

   ```
   my_frame_task/run001/k4_N4_tileable_40nm/and2/MIN_ROUTE_CHAN_WIDTH/
   ```

7. **检查产出**：在该目录下应看到生成的 fabric Verilog 网表、比特流与 testbench（具体文件取决于 `write_full_testbench_example_script.openfpga` 模板的 `write_*` 命令）。

8. **对照验证**：把你的 `my_frame_task/config/task.conf` 与仓库里的 `openfpga_flow/tasks/basic_tests/full_testbench/configuration_frame/config/task.conf` 做一次 `diff`，预期二者仅模板/命名相关行不同，核心的 `openfpga_arch_file` 一致。再跑一次官方任务 `run-task basic_tests/full_testbench/configuration_frame`，对比两者的产出目录结构是否一致。

**需要观察的现象与预期结果**：

- 干跑 3 个 job，正式跑全部 `status=True`。
- cc 版与 frame 版产出最大的差异在比特流组织：cc 版是一条扫描链（bitstream 顺序串行写入），frame 版是帧寻址（bitstream 带帧地址）。如果你打开各自的 fabric 比特流文件，应能看到这种结构差异——这正是 `task.conf` 里改的那一行 arch 文件、进而改的 `configuration_protocol` 带来的下游影响。
- 与官方 `configuration_frame` 任务对照，产出目录结构一致即说明你的改造正确。

**待本地验证**：比特流文件的具体格式与文件名取决于本地实际生成的产物；若运行失败，先看 `run001` 下各线程日志（`*_out.log`）与 `--show_thread_logs` 的输出定位原因。

**可选变体（照搬 busmux 任务）**：如果你想体验「换 VPR 架构」而非「换 openfpga_arch」的那条路，可以直接复刻 4.4.3 案例里的 `k4n4_frac_mult_busmux`——它的 `[ARCHITECTURES]` 指向带 `busmux` 后缀的 VPR 架构（`k4_frac_N4_tileable_adder_chain_mem1K_frac_dsp32_busmux_40nm.xml`），基准同样是 `and2`。`create-task my_busmux basic_tests/k4_series/k4n4_frac_mult_busmux` 复制一份后干跑，预期仍是 1 个 job，产出路径形如 `my_busmux/run001/k4_frac_N4_tileable_adder_chain_mem1K_frac_dsp32_busmux_40nm/and2/MIN_ROUTE_CHAN_WIDTH/`。这一变体把本讲四个模块换了一个维度（架构侧而非电路协议侧）再串一遍。

## 6. 本讲小结

- 一个**任务**就是一个含 `config/task.conf` 的目录；`task.conf` 分 `GENERAL`/`OpenFPGA_SHELL`/`ARCHITECTURES`/`BENCHMARKS`/`SYNTHESIS_PARAM`/`SCRIPT_PARAM_*` 等段，其中前三段（`GENERAL`/`BENCHMARKS`/`ARCHITECTURES`）必需。
- **job 总数 = 架构数 × 基准数 × 脚本参数集数**，由 `run_fpga_task.py` 的三重循环切出；脚本参数集靠「段名含 `SCRIPT_PARAM`」识别。
- 每次运行落在递增编号的 `run001`/`run002`… 目录下，`latest` 软链接恒指向最新；单个 job 产出于 `<runXXX>/<arch>/<top_module>/<script_param标签>/` 子路径。
- 并行由 `--maxthreads`（默认 2）配 `threading.Semaphore` 限流；默认任一 job 失败即整批退出，加 `--continue_on_fail` 可放宽；`--test_run` 可只切 job 不执行。
- **基准**位于 `openfpga_flow/benchmarks/`，`micro_benchmark/` 下是最小的微基准（如 `and2`），是回归与教学主力；基准靠 `benchN_` 前缀在 `[BENCHMARKS]` 与 `[SYNTHESIS_PARAM]` 间对齐。
- **回归测试**分两层：`basic_tests/` 按特性分任务，`regression_test_scripts/*.sh` 串成脚本，CI（`build.yml`）矩阵式批量触发；`basic_reg_test.sh` 末尾还用 `git diff` 黄金网表做输出锁定校验。
- **新特性落地回归的标准范式**（本次「Bus Based MUX」的实例）：一个能跑通的功能任务（`k4_series/k4n4_frac_mult_busmux`）+ 一个锁定关键产出的 `no_time_stamp` 任务（`frac_dsp_busmux`，用 `.gitignore` 白名单只锁 `bitstream_distribution.xml` 等三个文件）+ 回归脚本里各加一行 `run-task`。

## 7. 下一步学习建议

- **向深处走（任务编排）**：本讲只到 `run_fpga_task.py`。它最终调用的是 `run_fpga_flow.py`，后者负责 yosys 综合、`.openfpga` 模板的 `${}` 变量替换（`safe_substitute`）与编排。读 `openfpga_flow/scripts/run_fpga_flow.py` 能补全「任务 → 单次流程」的最后一环。
- **向宽处走（特性覆盖面）**：`basic_tests/` 的子目录名就是一份特性清单。建议挑一两个你感兴趣的目录（如 `clock_network/`、`fabric_key/`、`tile_organization/`），对照各自 `task.conf` 与对应回归脚本段落，理解每项特性在测什么——这也是后续专家层讲义（u9 时钟网络、u9 fabric key/tile、u9 GSB）的实景地图。
- **承接配置协议**：本讲综合实践把 cc 换成了 frame，触及了 `configuration_protocol`。若想彻底搞懂 scan_chain / frame_based / memory_bank 的区别与下游影响，接着学 u3-l4（配置协议）与 u7（比特流生成）。
- **动手贡献**：当你给 OpenFPGA 加了新功能，按本讲的模式在 `basic_tests/` 下加一个任务、在对应 `*_reg_test.sh` 里加一行 `run-task`，就完成了一次回归测试覆盖。本次「Bus Based MUX」就是最佳范本——它同时加了功能性任务 `k4_series/k4n4_frac_mult_busmux` 和黄金锁定任务 `no_time_stamp/frac_dsp_busmux`，你可以直接照抄这套「双任务 + `.gitignore` 白名单」结构。贡献规范与格式化细节见 u10-l5。

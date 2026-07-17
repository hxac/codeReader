# 回归测试与 QoR 评估

## 1. 本讲目标

本讲是「共享库、测试与工程实践」单元的收尾，承接 u9-l2（单元测试 Catch2）。学完本讲后，读者应该能够：

- 说清 VTR 的「回归测试」到底是什么，它与 Catch2 单元测试各管什么。
- 看懂一个回归任务（task）的配置文件 `config.txt`，并知道 `task_list.txt` 如何把多个 task 串成一个测试套件。
- 跑通「运行 → 生成黄金结果 → 校验黄金结果」的完整回归闭环。
- 理解为什么 VTR 的回归判定是「足够接近」而非「精确相等」，以及 `Range / RangeAbs / Equal` 三种通过准则如何实现这一点。
- 会用 `qor_compare.py` 把两次运行的 QoR（质量结果）做成 Excel 比对表，并用几何均值（geomean）判断一次改动到底是改进还是回退。

一句话定位：**单元测试管「API 对不对」，回归测试管「整套 CAD 流程出来的质量指标有没有变坏」。**

## 2. 前置知识

在进入源码前，先建立三个直觉。

**① 为什么回归测试需要单独一套体系。** VPR 的打包、布局、布线都是启发式算法（贪心聚簇、模拟退火、协商式迷宫布线），它们的输出——关键路径延迟、布线线长、最小通道宽度——天然不是位级可复现的：换一台机器、换一个编译器版本、甚至换一个内存分配器（TBB vs glibc），结果都会在小范围内抖动。因此「测试通过」不能定义成「输出和上次一模一样」，只能定义成「输出和某个可信参考值足够接近」。这个「可信参考值」就是**黄金结果（golden results）**，这套「够不够近」的判定机制就是本讲的核心。

**② 什么是 QoR。** QoR = Quality of Results，质量结果。它不是单一数字，而是一组贯穿全流程的指标：综合深度（`abc_depth`）、聚簇块数（`num_clb`）、最小通道宽度（`min_chan_width`）、布线线长（`routed_wirelength`）、关键路径延迟（`critical_path_delay`）、总运行时间（`vtr_flow_elapsed_time`）、峰值内存（`max_vpr_mem`）等。这些指标大多「越小越好」（延迟短、面积小、跑得快），因此一套 benchmark 上各项指标的几何均值，是衡量「这次算法改动整体上是赚是亏」的头条数字。

**③ 几何均值为什么是「头条」。** 当你把 50 个 benchmark 的延迟都变成了 `新值/旧值` 的比值 \(r_i\)，算术平均会被一两个极端值带偏，而几何均值

\[
\text{geomean} = \left(\prod_{i=1}^{N} r_i\right)^{1/N}
\]

对「成比例变化」更稳健，也更贴合「整体典型改善倍数」的直觉。对「越小越好」的指标，比值 geomean < 1.0 即代表整体改进。后续会看到，回归体系用 `pass_requirements`（逐项范围判定）回答「合不合格」，而 QoR 评估用 geomean（整体趋势）回答「变好还是变坏」。

> 阅读提示：本讲大量路径在 `vtr_flow/scripts/python_libs/vtr/` 这个 Python 包内。入口脚本（`run_vtr_task.py`）通过 `sys.path.insert` 把这个包加进搜索路径（见 [`run_vtr_task.py:21-22`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/run_vtr_task.py#L21-L22)），所以脚本里 `from vtr import ...` 引用的就是这个包。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [`run_reg_test.py`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/run_reg_test.py) | 仓库根目录的**最顶层**回归驱动器，按套件名（如 `vtr_reg_strong`）跑一整套 task，并做黄金校验与 geomean。 |
| [`vtr_flow/scripts/run_vtr_task.py`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/run_vtr_task.py) | **中层**脚本，负责运行单个 task 或一个 task 列表，分派 run/parse/create_golden/check_golden/calc_geomean 五种动作。 |
| [`vtr_flow/scripts/python_libs/vtr/parse_vtr_task.py`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/parse_vtr_task.py) | **底层库**，真正实现解析、生成黄金、校验黄金、汇总 QoR、计算 geomean。 |
| [`vtr_flow/scripts/python_libs/vtr/log_parse.py`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/log_parse.py) | 解析正则模式 `ParsePattern`、通过准则 `PassRequirement`（Range/RangeAbs/Equal）、结果容器 `ParseResults` 的定义。 |
| [`vtr_flow/scripts/python_libs/vtr/task.py`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/task.py) | 任务配置 `TaskConfig`、单个作业 `Job`、`create_jobs` 把 task 展开成「电路 × 架构 × 脚本参数」的笛卡尔积。 |
| [`vtr_flow/scripts/python_libs/vtr/parse_vtr_flow.py`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/parse_vtr_flow.py) | 解析引擎：把正则模式套到日志文件上，抓出指标，打印成 TSV。 |
| [`vtr_flow/scripts/qor_compare.py`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/qor_compare.py) | 把两份 `parse_results.txt` 做成 Excel 比对表（原始值、比值、geomean、汇总）。 |
| [`doc/agents/testing.md`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/testing.md) | 官方测试指南，本讲代码实践的命令模板来源。 |
| 示例配置：[`strong_timing/config/config.txt`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/tasks/regression_tests/vtr_reg_strong/strong_timing/config/config.txt)、通过准则 [`common/pass_requirements.vpr_route_min_chan_width.txt`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/parse/pass_requirements/common/pass_requirements.vpr_route_min_chan_width.txt)、QoR 解析配置 [`qor_config/qor_standard.txt`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/parse/qor_config/qor_standard.txt) | 真实 task 的配置样本，用来对照源码。 |

## 4. 核心概念与源码讲解

### 4.1 回归任务体系与配置（回归任务配置）

#### 4.1.1 概念说明

VTR 把「回归测试」组织成一个**三层脚本 + 二层配置**的结构：

- **脚本三层**：`run_reg_test.py`（根目录，跑一整个套件）→ `run_vtr_task.py`（跑一个 task）→ `parse_vtr_task.py`（库，真正干活）。
- **配置两层**：`task_list.txt`（一个套件包含哪些 task）+ `config.txt`（一个 task 跑哪些电路、哪些架构、怎么解析）。

一个 **task（任务）**是回归体系的基本单元，它对应 `vtr_flow/tasks/regression_tests/<套件名>/<task名>/config/config.txt` 这样一个目录。一个 task 描述的是**电路 × 架构 × 脚本参数**的笛卡尔积——每个组合跑一次完整 VTR 流程，产生一份结果。task 再被 `task_list.txt` 汇总成套件，例如 `vtr_reg_strong` 的 `task_list.txt` 列了上百个 task（见 [`task_list.txt`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/tasks/regression_tests/vtr_reg_strong/task_list.txt)）。

这种分层的好处是：你可以只跑一个 task 调试（直接调 `run_vtr_task.py`），也可以一键跑整个套件做全面回归（调 `run_reg_test.py`）。

#### 4.1.2 核心流程

顶层 `run_reg_test.py` 不带任何标志运行一个套件时，经历三步：

```
run_reg_test.py vtr_reg_strong
   │
   ├─ 1) collect_task_list("vtr_reg_strong")  → 找到 task_list.txt 路径
   │
   ├─ 2) run_tasks(...)                        → 调 run_vtr_task.py -l task_list.txt -j N
   │       （真正把每个 task 的每条组合跑一遍 VTR 流程）
   │
   └─ 3) parse_single_test(..., check=True, calculate=True)
              → 调 run_vtr_task.py -l task_list.txt -check_golden -calc_geomean
              （解析结果 + 校验黄金 + 计算 geomean）
```

关键在于：**`run_reg_test.py` 自己几乎不实现逻辑，它只是把动作翻译成对 `run_vtr_task.py` 的两次调用**——一次负责「跑」，一次负责「解析+校验」。这与 u1-l2 讲过的「Makefile 是 CMake 的包装层」是同一种设计哲学：上层做编排，下层做实事。

而 `run_vtr_task.py` 用一个布尔标志区分五种互斥动作——运行、解析、生成黄金、校验黄金、算 geomean，见下一节的源码。

#### 4.1.3 源码精读

**① 顶层驱动器的分派逻辑。** [`run_reg_test.py:137-202`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/run_reg_test.py#L137-L202) 的 `vtr_command_main` 是入口。当不指定任何标志、且套件名不是 `parmys`/`odin` 前缀时，它落到这段默认分支：

```python
# run_reg_test.py:167-186 （节选）
# 默认分支：跑一遍 + 校验黄金 + 算 geomean
vtr_task_list_files = collect_task_list(reg_test)
if vtr_task_list_files:
    num_func_failures += run_tasks(args, vtr_task_list_files)          # 第 2 步：运行
if not args.skip_qor and vtr_task_list_files:
    num_qor_failures += parse_single_test(
        vtr_task_list_files, check=True, calculate=True)               # 第 3 步：校验+geomean
```

其中 `collect_task_list` 只是把套件名拼成 `task_list.txt` 的路径并校验存在（[`run_reg_test.py:337-342`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/run_reg_test.py#L337-L342)）；`run_tasks` 和 `parse_single_test` 都只是拼参数、再转调 `run_vtr_task`（[`run_reg_test.py:345-370`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/run_reg_test.py#L345-L370)）。注意退出码语义：`sys.exit(abs(总失败数))`，退出码 0 才算全过（[`run_reg_test.py:202`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/run_reg_test.py#L202)）。

**② 中层脚本的动作分派。** `run_vtr_task.py` 用一行布尔表达式决定要不要真正运行流程：

```python
# run_vtr_task.py:248
args.run = not (args.parse or args.create_golden or args.check_golden or args.calc_geomean)
# 运行时强制顺带解析
if args.run:
    args.parse = True
```

这行是理解整个体系的关键：五个标志互斥，**只要带了 `-create_golden`/`-check_golden`/`-calc_geomean`/`-parse` 中的任何一个，就不会重新跑流程**，只做后处理。真正的分派发生在 `run_tasks`：

```python
# run_vtr_task.py:292-345 （结构节选）
def run_tasks(args, configs):
    jobs = create_jobs(args, configs)              # 把 task 展开成 Job 列表
    ...
    if args.run:        num_failed = run_parallel(args, jobs, run_dirs)   # 跑流程
    if args.parse:      parse_tasks(configs, jobs, ...)                   # 解析日志
    if args.create_golden: create_golden_results_for_tasks(configs, ...)  # 生成黄金
    if args.check_golden:  num_failed += check_golden_results_for_tasks(...)  # 校验
    if args.calc_geomean:  summarize_qor(...); calc_geomean(...)          # 算 geomean
```

`run_parallel` 用 `multiprocessing.Pool` 按 `-j N` 并行跑各个 Job（[`run_vtr_task.py:348-375`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/run_vtr_task.py#L348-L375)），每个 Job 的流程输出写进各自的 `vtr_flow.out`。

**③ task 配置 `config.txt` 的结构。** 以真实 task `strong_timing` 为例，[`strong_timing/config/config.txt`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/tasks/regression_tests/vtr_reg_strong/strong_timing/config/config.txt) 内容如下（节选）：

```
circuits_dir=benchmarks/verilog
archs_dir=arch/timing
circuit_list_add=ch_intrinsics.v
arch_list_add=k6_frac_N10_mem32K_40nm.xml
parse_file=vpr_standard.txt        # 全量解析配置（用于黄金校验）
qor_parse_file=qor_standard.txt    # 精选 QoR 解析配置（用于 geomean）
pass_requirements_file=pass_requirements.txt
script_params_common = -track_memory_usage
```

`load_task_config` 解析它时，把键分成三类（[`task.py:209-300`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/task.py#L209-L300)）：

- **唯一键**（`circuits_dir`、`parse_file`、`pass_requirements_file` 等出现在 `unique_keys` 集合里）：只能写一次。
- **重复键**（名字含 `_list_add`）：可多次出现，攒成列表。
- **必填键**：`circuits_dir`、`archs_dir`、`circuit_list_add`、`arch_list_add`、`parse_file` 缺一不可。

一个细节值得注意：如果 `parse_file` 出现了两次，第二次会被自动存成 `second_parse_file`（[`task.py:263-264`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/task.py#L263-L264)），用于「一次 task 内做两次解析对比」的特殊流程（4.3 节会用到）。

**④ task 如何展开成 Job。** `create_jobs`（[`task.py:574-664`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/task.py#L574-L664)）做架构、电路（以及可选的 NoC 流量、脚本参数）的笛卡尔积，每个组合生成一个 `Job`。每个 Job 携带四条命令：`run_command`（跑流程）、`parse_command`（全量解析）、`second_parse_command`、`qor_parse_command`（见 [`task.py:102-206`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/task.py#L102-L206)）。

这里有一个**容易被忽略但很重要的细节**：黄金结果不只是用来校验，还会**反喂给下一次运行**。`create_job` 会从 `golden_results.txt` 读出历史的最小通道宽度和期望状态，据此给运行命令追加 `--min_route_chan_width_hint`（加速最小通道宽度二分搜索）和 `-expect_fail`（期望某电路故意失败）：

```python
# task.py:704-715 （节选）
expected_min_w = ret_expected_min_w(circuit, arch, golden_results, param)  # 读黄金里的 min_chan_width
...
if expected_min_w > 0:
    cmd += ["--min_route_chan_width_hint", str(expected_min_w)]
expected_vpr_status = ret_expected_vpr_status(arch, circuit, golden_results, param)
if expected_vpr_status not in ("success", "Unknown"):
    cmd += ["-expect_fail", expected_vpr_status]
```

`ret_expected_min_w` 直接从 `golden_results.metrics(...)` 取 `min_chan_width` 字段（[`task.py:791-799`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/task.py#L791-L799)）。所以：**黄金结果既是「判卷标准」，也是「下次考试的提示」**——这是回归体系闭环设计的精妙之处。

#### 4.1.4 代码实践

> **目标**：在不修改任何源码的前提下，把官方文档里的「新增一个回归任务」流程走一遍，理解三层脚本如何协作。

1. 激活 Python 虚拟环境（u9-l2、testing.md 都强调过）：`source .venv/bin/activate`。
2. 复制一个现成 task 作为模板（来自 [`doc/agents/testing.md:46-60`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/testing.md#L46-L60)）：

   ```shell
   cd vtr_flow/tasks/regression_tests/vtr_reg_strong
   mkdir -p strong_mytest/config
   cp strong_timing/config/config.txt strong_mytest/config/.
   ```

3. 先**只解析不运行**，验证配置能被正确加载（注意：不跑流程，所以即使没有编译 vpr 也能验证配置语法）：

   ```shell
   cd ../../..   # 回到仓库根
   ./run_reg_test.py vtr_reg_strong -display_qor
   ```

   预期：要么打印该套件已有的 `qor_geomean.txt` 表格，要么提示「QoR results do not exist」——两者都说明套件能被 `collect_task_list` 找到。
4. **观察点**：在 `run_reg_test.py` 的 `vtr_command_main` 里下断点（或加 `print`），分别用 `-create_golden`、`-check_golden`、`-parse` 跑，观察它们分别落到 `parse_single_test(..., create=True)`、`parse_single_test(..., check=True)`、还是默认运行分支。
5. 预期结果：你会看到 `-create_golden` 和 `-check_golden` 都**不会触发 `run_parallel`**（因为它们使 `args.run=False`），只有不带任何标志的默认调用才真正跑流程。
6. 完整跑通需要先 `make -j8 vpr` 编出 vpr 二进制；若本地未编译，此步标注**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`run_vtr_task.py` 同时带上 `-create_golden` 和 `-check_golden` 会怎样？
**答案**：看 `run_vtr_task.py:248`，`args.run = not (parse or create_golden or check_golden or calc_geomean)`，所以不会运行流程；而在 `run_tasks`（292-345）里 `create_golden` 与 `check_golden` 是两个独立的 `if`，会**依次执行**——先覆盖黄金，再用新黄金校验，结果必然通过。这正是为什么生成黄金后官方建议立刻 `-check_golden` 复核。

**练习 2**：为什么 `load_task_config` 要求 `parse_file` 必填，但 `pass_requirements_file` 不必填？
**答案**：`parse_file` 决定了「从日志里抓哪些指标」，没有它就无法产出 `parse_results.txt`，回归无从谈起，所以必填（见必填键集合 [`task.py:245-247`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/task.py#L245-L247)）。而 `pass_requirements_file` 缺失时，`check_golden_results_for_task` 只会打印一条「no pass requirements file ... QoR will not be checked」警告（4.3 节源码可见），不致命。

---

### 4.2 解析管道：从日志到指标（桥梁模块）

#### 4.2.1 概念说明

流程跑完后，「结果」散落在 `vpr.out`、`vpr.crit_path.out`、`output.txt` 等多个日志文件里。回归体系需要把这些散落的文本压平成一张「指标表」`parse_results.txt`（TSV 格式：每行一个电路/架构组合，每列一个指标）。这个「文本 → 指标」的转换由**解析管道**完成，它是黄金校验和 QoR 对比的共同地基。

管道的核心抽象是 `ParsePattern`（解析模式）：一条声明「在哪个文件里、用哪条正则、抓出叫什么名字的指标」。模式集中写在**解析配置文件**里（如 `vpr_standard.txt`、`qor_standard.txt`）。

注意有两套解析配置、对应两种产物：

| 配置键 | 解析配置文件 | 产物 | 用途 |
|--------|------------|------|------|
| `parse_file` | `vpr_standard.txt`（全量） | `parse_results.txt` | 黄金校验（逐项范围判定） |
| `qor_parse_file` | `qor_standard.txt`（精选） | `qor_results.txt` | geomean 汇总与 QoR 趋势 |

这套「全量 vs 精选」的分离是刻意设计：校验要全，趋势要精。

#### 4.2.2 核心流程

解析一次 task 的流程：

```
对 task 的每个 Job（一个 arch/circuit 组合）:
   parse_vtr_flow(parse_path=run_dir, parse_config, arch=.., circuit=.., script_params=..)
       │
       ├─ load_parse_patterns(config)   # 读解析配置，构 ParsePattern 表
       ├─ 每个模式设默认值（无默认则 "-1"）
       ├─ 按文件名分组，每个日志只读一次，逐行套正则
       └─ 打印：表头(arch circuit script_params 指标1 指标2 ...) + 一行数据
              （重定向到 Job 目录下的 parse_results.txt）
   ↓
parse_files(...)  # 把每个 Job 的 parse_results.txt 合并成 task 级 parse_results.txt
```

#### 4.2.3 源码精读

**① 解析模式与正则封装。** `ParsePattern` 把用户写的正则包成「可在行中任意位置匹配」的形式（[`log_parse.py:16-41`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/log_parse.py#L16-L41)）：

```python
# log_parse.py:23-25
# 在正则前后加 .* ，让模式匹配「出现在行中任何位置」（见 GitHub Issue #2743）
self._regex = re.compile(f"^.*{regex_str}.*$")
```

解析配置文件的格式由 `load_parse_patterns` 规定：每行 `name;filename;regex;[default]`，分号分隔，三或四段（[`log_parse.py:308-343`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/log_parse.py#L308-L343)）。看真实的 [`qor_config/qor_standard.txt`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/parse/qor_config/qor_standard.txt)：

```
num_clb;vpr.out;Netlist clb blocks:\s*(\d+)
min_chan_width;vpr.out;Best routing used a channel width factor of (\d+)
crit_path_delay;vpr.crit_path.out;Final critical path: (.*) ns
```

以 `#` 开头是注释、`%include` 拉入其它配置（如 `vpr_standard.txt` 顶部一串 `%include "common/..."`，见 [`vpr_standard.txt:4-18`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/parse/parse_config/vpr_standard.txt#L4-L18)）。这就解释了为什么一个解析配置文件能「组合」出几十个指标——靠 include 拼装。

**② 解析引擎主体。** [`parse_vtr_flow.py:46-98`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/parse_vtr_flow.py#L46-L98) 的 `parse_vtr_flow` 是核心。它做了三件聪明事：

```python
# parse_vtr_flow.py:84-91 （按文件名分组，每个日志只读一次）
parse_patterns_by_filename = defaultdict(list)
for parse_pattern in parse_patterns.values():
    parse_patterns_by_filename[parse_pattern.filename()].append(parse_pattern)
for filename, patterns in parse_patterns_by_filename.items():
    parse_file_and_update_results(str(Path(parse_path) / filename), patterns, results)
```

为什么按文件名分组？因为多个指标常来自同一个日志（如 `num_clb`、`min_chan_width` 都在 `vpr.out`），分组后**每个日志文件只打开扫一遍**，避免重复 IO。匹配逻辑在 `parse_file_and_update_results`（[`parse_vtr_flow.py:19-43`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/parse_vtr_flow.py#L19-L43)）：逐行、逐模式，命中则取第一个捕获组。

最后它打印「表头 + 一行数据」的 TSV（带前缀的 `arch/circuit/script_params`），通过 `redirect_stdout` 落盘成 Job 级 `parse_results.txt`（4.3 节的 `parse_task` 用了同样的重定向技巧）。

**③ 合并成 task 级结果。** [`parse_vtr_task.py:264-294`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/parse_vtr_task.py#L264-L294) 的 `parse_files` 把每个 Job 的两行文件（表头+数据）合并：

```python
# parse_vtr_task.py:281-287 （每个 Job 文件恰好 2 行：表头 + 数据）
assert len(lines) == 2
if header:
    print(lines[0], file=out_f, end="")   # 表头只取第一个 Job 的
    header = False
print(lines[1], file=out_f, end="")       # 每个 Job 贡献一行数据
```

合并后，task 目录下的 `parse_results.txt` 就是「一张表」：第一行是全部指标列名，其后每行一个 arch/circuit 组合。这张表正是黄金结果与 QoR 对比的统一输入。真实样本见 [`strong_timing/config/golden_results.txt`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/tasks/regression_tests/vtr_reg_strong/strong_timing/config/golden_results.txt)——它其实就是某次运行生成的 `parse_results.txt` 被「提拔」成黄金（4.3 节）。

#### 4.2.4 代码实践

> **目标**：理解「解析配置 → 正则 → 指标」的映射，不改源码，纯阅读 + 手动验证。

1. 打开 [`vtr_flow/parse/qor_config/qor_standard.txt`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/parse/qor_config/qor_standard.txt)，挑一条未注释的模式，比如 `crit_path_delay;vpr.crit_path.out;Final critical path: (.*) ns`。
2. 在任何一个**已经跑过的** task 运行目录里找到对应的 `vpr.crit_path.out`，搜索 `Final critical path:`。
3. 对照 `ParsePattern` 的正则封装 `^.*Final critical path: (.*) ns.*$`，确认捕获组抓到的数值。
4. 再打开同目录下的 `parse_results.txt`，找到 `critical_path_delay`（或 `crit_path_delay`）列，确认该值与第 3 步一致。
5. 预期结果：日志里的裸文本、正则捕获值、`parse_results.txt` 列值三者一致。
6. 若本地没有任何已跑过的 run 目录，则此步为**待本地验证**的源码阅读型实践——重点是建立「配置行 ↔ 正则 ↔ 指标列」的一一对应直觉。

#### 4.2.5 小练习与答案

**练习 1**：如果某条指标在日志里根本没出现，`parse_results.txt` 里对应单元格会是什么值？
**答案**：`parse_vtr_flow` 会先用默认值初始化每个模式——若解析配置里没给第 4 段默认值，就用 `"-1"`（[`parse_vtr_flow.py:71-75`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/parse_vtr_flow.py#L71-L75)）。这就是为什么黄金结果表里有大量 `-1`（表示该指标对该流程不适用，例如没开功耗时功耗列为空）。

**练习 2**：`safe` 地说，为什么 `qor_standard.txt` 用 `Final critical path: (.*) ns` 而不是 `(\d+\.\d+)`？
**答案**：因为延迟可能是科学计数法或带前导空格的浮点（如真实黄金里的 `1.9268`），`(.*)` 更宽容，后续 `RangePassRequirement.check_passed` 会用 `float()` 转换（4.3 节）。宽容捕获 + 严格转换，是这个管道的一贯作风。

---

### 4.3 黄金结果校验（黄金结果校验）

#### 4.3.1 概念说明

有了 `parse_results.txt`（当前运行）和 `golden_results.txt`（可信参考），剩下的问题就是「当前结果算不算合格」。这一节回答两个问题：

1. **黄金结果从哪来？** 答案出奇地简单：黄金结果就是「某次被信任的运行的 `parse_results.txt` 的拷贝」。生成黄金 = 把最新一次运行的 `parse_results.txt` 复制到 task 的 `config/golden_results.txt`，提交进仓库。
2. **「合格」怎么判定？** 因为结果不可位级复现，判定不是相等，而是「逐项落在容差区间内」。每个指标在**通过准则文件**（pass requirements）里声明自己的容差，有三种形式：

- `Equal()`：精确相等（只用于真正确定的量，如 `vpr_status`）。
- `Range(min, max)`：相对容差，要求 `新值/黄金值` 落在 `[min, max]`。
- `RangeAbs(min, max, abs)`：相对容差 **或** 绝对容差二选一通过，即 `新/黄金 ∈ [min,max]` **或** `|新 − 黄金| ≤ abs`。用于噪声大的指标（内存、运行时间）。

这套机制让回归既严格（关键指标如延迟的容差通常很窄）又鲁棒（噪声指标给了绝对宽容限）。

#### 4.3.2 核心流程

```
生成黄金：  最新 run 的 parse_results.txt  ──shutil.copy──>  config/golden_results.txt

校验黄金：  parse_results.txt（当前）
                │
                ├─ 都必须含主键 architecture/circuit/script_params
                ├─ 加载通过准则 pass_requirements（Range/RangeAbs/Equal）
                └─ 对黄金里的每个 case、准则里的每个 metric：
                       pass_requirements[metric].check_passed(黄金值, 当前值)
                       ── 返回 (是否通过, 原因) ── 累计失败数
```

注意校验的**主导方向**：遍历的是**黄金**里的 case 集合（[`parse_vtr_task.py:448`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/parse_vtr_task.py#L448)）。也就是说，黄金定义了「必须被覆盖且合格」的全部情形；当前运行多出来的 case 只给警告，少一个就直接判 Fail。

#### 4.3.3 源码精读

**① 生成黄金就是一次拷贝。** [`parse_vtr_task.py:304-313`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/parse_vtr_task.py#L304-L313) 短得惊人：

```python
def create_golden_results_for_task(config, alt_tasks_dir=None):
    run_dir = find_latest_run_dir(config, alt_tasks_dir)
    task_results = str(PurePath(run_dir).joinpath(FIRST_PARSE_FILE))   # 最新 run 的 parse_results.txt
    golden_results_filepath = str(PurePath(config.config_dir).joinpath("golden_results.txt"))
    shutil.copy(task_results, golden_results_filepath)                  # 直接拷过去
```

含义：黄金没有任何「计算」，它纯粹是「冻结一次可信运行」。所以 `testing.md` 反复强调——**只有在你确信 QoR 变化是「有意的」时**才重新生成黄金（[`doc/agents/testing.md:62-70`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/testing.md#L62-L70)）。否则你会把一次回退「洗白」成新基准。

**② 校验入口。** [`parse_vtr_task.py:327-371`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/parse_vtr_task.py#L327-L371) 的 `check_golden_results_for_task` 有两个分支：

- 一般情况：拿当前 `parse_results.txt` 对比 `config/golden_results.txt`。
- 特殊情况（`config.second_parse_file` 为真，即一次 task 内做两次解析）：对比同一次运行的两份结果文件——用于「同次运行、两种配置自洽性」检查。

两者最终都进 `check_two_files`。

**③ 逐项判定 `check_two_files`。** [`parse_vtr_task.py:375-495`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/parse_vtr_task.py#L375-L495) 是本节心脏。它的判定循环：

```python
# parse_vtr_task.py:468-494 （结构节选）
for metric in pass_requirements.keys():        # 只校验「准则里声明了」的指标
    ...
    metric_passed, reason = pass_requirements[metric].check_passed(
        second_metrics[metric],   # 黄金值（分母）
        first_metrics[metric],    # 当前值（分子）
        second_name)
    if not metric_passed:
        print("[Fail]\n{}/{}/{} {} {}".format(arch, circuit, script_params, metric, reason))
        num_qor_failures += 1
```

两个要点：第一，**只有出现在通过准则文件里的指标才会被强制校验**，其它指标随便变（这正是源码注释「We do not worry about non-pass_requirements elements being different or missing」的含义，[`parse_vtr_task.py:403-404`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/parse_vtr_task.py#L403-L404)）。第二，`check_passed` 的第一个参数是**黄金值**（分母），第二个是**当前值**（分子），方向别搞反。

**④ 通过准则的数学。** 以最常用的 `RangePassRequirement` 为例（[`log_parse.py:82-162`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/log_parse.py#L82-L162)）。核心是先把两个值转成浮点，再算相对比值：

\[
r = \frac{\text{当前值}}{\text{黄金值}}
\]

判定通过当且仅当 \(\text{min} \le r \le \text{max}\)（黄金值为 0 时特判，[`log_parse.py:138-150`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/log_parse.py#L138-L150)）。`RangeAbsPassRequirement`（[`log_parse.py:165-264`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/log_parse.py#L165-L264)）在此基础上加一个「或」：

\[
\text{通过} \iff (\text{min} \le r \le \text{max}) \;\lor\; |\text{当前值} - \text{黄金值}| \le \text{abs}
\]

准则文件的格式由 `load_pass_requirements`（[`log_parse.py:346-414`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/log_parse.py#L346-L414)）解析：`metric;Range(min,max)` / `RangeAbs(min,max,abs)` / `Equal()`。看真实样本 [`common/pass_requirements.vpr_route_min_chan_width.txt`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/parse/pass_requirements/common/pass_requirements.vpr_route_min_chan_width.txt)：

```
min_chan_width;Range(0.25,1.30)              # 通道宽度：当前可在黄金的 25%~130%
routed_wirelength;RangeAbs(0.60,1.50,5)       # 线长：相对 60%~150% 或绝对差 ≤5
max_vpr_mem;RangeAbs(0.5,2.0,102400)          # 内存：相对 50%~200% 或绝对差 ≤100MiB
```

最后一条的注释解释了为什么要给内存这么大宽容度——内存分配器（TBB vs glibc）和二分搜索路径都会让峰值内存显著抖动（[`pass_requirements.vpr_route_min_chan_width.txt:16-27`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/parse/pass_requirements/common/pass_requirements.vpr_route_min_chan_width.txt#L16-L27)）。这正是「RangeAbs 不是偷懒，而是承认物理噪声」的工程体现。

#### 4.3.4 代码实践

> **目标**：亲手走一遍「生成黄金 → 改动 → 校验黄金」闭环，体会容差判定。本实践需要先 `make -j8 vpr` 并能跑通流程；若本地无法编译，请按「源码阅读型」完成第 1–4 步。

参照 [`doc/agents/testing.md:14-23`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/testing.md#L14-L23) 的命令模板：

1. `source .venv/bin/activate`。
2. 跑一个已有 task 并生成黄金：

   ```shell
   cd vtr_flow/tasks
   ../scripts/run_vtr_task.py regression_tests/vtr_reg_strong/strong_timing
   ../scripts/python_libs/vtr/parse_vtr_task.py regression_tests/vtr_reg_strong/strong_timing -create_golden
   ../scripts/python_libs/vtr/parse_vtr_task.py regression_tests/vtr_reg_strong/strong_timing -check_golden
   ```

3. 预期：`-check_golden` 打印 `...[Pass]`（因为黄金刚刚从同次运行生成，必然全过）。此时 `config/golden_results.txt` 已更新——**但请不要把这个改动提交**，这只是练习。
4. 现在模拟一次「有意改动」：编辑 `strong_timing/config/config.txt`，给 `script_params_common` 加一个会轻微影响结果的参数（例如 `-seed 1`，改变模拟退火随机性），重新运行并 `-check_golden`：

   ```shell
   ../scripts/run_vtr_task.py regression_tests/vtr_reg_strong/strong_timing
   ../scripts/python_libs/vtr/parse_vtr_task.py regression_tests/vtr_reg_strong/strong_timing -check_golden
   ```

5. **观察点**：你会看到某些 `[Fail]` 行，形如 `strong_timing/.../min_chan_width relative value 1.4 outside of range [0.25,1.30] ...`。注意它给出的是相对比值 `新/黄金`，与 4.3.3 的公式一致。
6. 预期结果：绝大多数指标仍在容差内通过；个别对随机种子敏感的指标（如 `min_chan_width`、布线线长）可能接近容差边界。具体哪些 Fail、比值多少，**待本地验证**。
7. 收尾：练习结束后用 `git checkout` 还原你对 `config.txt` 和 `golden_results.txt` 的改动——**不要污染仓库**。

#### 4.3.5 小练习与答案

**练习 1**：某次校验报 `min_chan_width relative value 1.4 outside of range [0.25,1.30]`，请解释 1.4 是怎么算出来的、为什么判 Fail。
**答案**：`1.4 = 当前通道宽度 / 黄金通道宽度`（[`log_parse.py:144`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/log_parse.py#L144)）。准则 `min_chan_width;Range(0.25,1.30)` 要求比值在 `[0.25, 1.30]`，1.4 > 1.30，故 Fail——意味着这次改动让最小通道宽度变大了 40%，超出了「相对 25%~130%」的容忍范围。

**练习 2**：为什么 `max_vpr_mem` 用 `RangeAbs` 而不是 `Range`？如果用纯 `Range(0.5,2.0)` 会在什么情况下误判？
**答案**：小 benchmark 的峰值内存本身很小，一个几十 MiB 的绝对差异可能产生极大的相对比值（分母小），用纯 Range 会把无害的抖动误判为 Fail。`RangeAbs(0.5,2.0,102400)` 额外允许「绝对差 ≤100MiB 即放过」，正是为了吸收这种小基数下的相对噪声（见准则文件第 16–27 行的注释）。

**练习 3**：如果你想让一个新指标进入强制校验，需要改哪两个地方？
**答案**：第一，在解析配置（`vpr_standard.txt` 体系）里加一条 `name;file;regex` 让它被解析进 `parse_results.txt`；第二，在该 task 用的通过准则文件里加一行 `name;Range(...)`（或 RangeAbs/Equal）。否则它即使被解析出来，也不会被 `check_two_files` 校验（因为循环只遍历 `pass_requirements.keys()`）。

---

### 4.4 QoR 对比与几何均值（QoR 对比）

#### 4.4.1 概念说明

上一节的黄金校验回答「合不合格」（二值、逐项），本节的 QoR 对比回答「**整体变好还是变坏、好了多少**」（连续、聚合）。这是衡量一次算法改动价值的工具，也是 PR（pull request）评审时最常被引用的依据。

VTR 提供两条 QoR 评估路径，别混淆：

1. **套件级 geomean 文件**（`qor_geomean.txt`）：`parse_vtr_task.py` 的 `calc_geomean` 在套件内跨所有 task 计算每项 QoR 指标的几何均值，每次运行追加一行（含运行目录、日期、revision）。`run_reg_test.py -display_qor` 把它 pretty-print 出来。这是「看本套件当下的整体水平」。
2. **跨运行 Excel 比对**（`qor_compare.py`）：把两份 `parse_results.txt`（典型地：黄金 vs 最新）做成 Excel，含原始值、逐项比值、每项 geomean、汇总。这是「看我这次改动相对基准的得失」。

两条路径都用 geomean 作为头条数字，理由见第 2 节：比值用乘法语义，geomean 给出「典型改善倍数」。

#### 4.4.2 核心流程

**路径 1（套件 geomean）：**

```
每个 task 的 qor_results.txt（精选指标）
       │ summarize_qor：拼成 task_summary/<run>_summary.txt（每行一个 task）
       └─ calc_geomean：对每个指标列跨 task 算 geomean，追加一行到 qor_geomean.txt
```

`run_reg_test.py -display_qor` 读取 `qor_geomean.txt`，展示五项头条：`total_runtime`、`total_wirelength`、`num_clb`、`min_chan_width`、`crit_path_delay`（[`run_reg_test.py:205-246`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/run_reg_test.py#L205-L246)）。

**路径 2（跨运行比对）：**

```
qor_compare.py parse_results_baseline.txt parse_results_new.txt -o cmp.xlsx
       │
       ├─ 每份文件 → 一个原始值 sheet
       ├─ ratios sheet：new / baseline，每项末尾一行 GEOMEAN
       ├─ summary_data sheet：抽取各 sheet 的 geomean 行
       └─ summary sheet：转置，便于阅读
```

或用 `-t <task>` 模式，自动取该 task 的 `config/golden_results.txt`（基准）与 `latest/parse_results.txt`（新）比对——这正是 PR 工作流（[`qor_compare.py:380-399`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/qor_compare.py#L380-L399)）。

#### 4.4.3 源码精读

**① 默认关注的指标与主键。** `qor_compare.py` 用两个常量定义「比什么」和「按什么对齐」：

```python
# qor_compare.py:13-46 （DEFAULT_METRICS，按阶段分组，节选）
DEFAULT_METRICS = [
    "abc_depth", "num_pre_packed_blocks",            # ABC 阶段
    "num_post_packed_blocks", "num_clb", "num_memories", "num_mult",  # Pack 阶段
    "placed_wirelength_est", "placed_CPD_est",       # Place 阶段
    "min_chan_width", "routed_wirelength", "critical_path_delay",     # Route 阶段
    "vtr_flow_elapsed_time", "max_vpr_mem",          # 运行时
    ...
]
# qor_compare.py:48-51
DEFAULT_KEYS = ["arch", "circuit"]   # 行主键：按架构+电路对齐两次运行
```

`DEFAULT_KEYS` 决定「两次运行的哪两行算同一组」——按架构和电路名对齐。`DEFAULT_METRICS` 是「要比哪些列」，可用 `--qor_metrics` 覆盖。

**② 比值与 geomean 的生成。** `fill_ratio`（[`qor_compare.py:260-317`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/qor_compare.py#L260-L317)）对每个指标列，逐行写一个 Excel 公式 `新值 / 基准值`，并在列末追加一行 GEOMEAN：

```python
# qor_compare.py:305-310 （用 Excel 的 GEOMEAN 公式，而非在 Python 里算）
dest_cell.value = "=GEOMEAN({}:{})".format(start_cell.coordinate, end_cell.coordinate)
```

值得注意：geomean 是**写在单元格里作为 Excel 公式**，打开 `.xlsx` 时由表格软件实时计算。分母为 0 或 `-1`（缺失值）时用 `safe_ratio_ref` 输出空串避免除零（[`qor_compare.py:328-331`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/qor_compare.py#L328-L331)）。

**③ 套件级 geomean 的 Python 实现。** 与 Excel 公式不同，`parse_vtr_task.py` 的 `calc_geomean` 在 Python 里直接算（[`parse_vtr_task.py:529-594`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/parse_vtr_task.py#L529-L594)）。核心 `calculate_individual_geo_mean`：

```python
# parse_vtr_task.py:585-591 （累乘 + 计数，最后开 N 次方）
if float(current_value) > 0:
    geo_mean *= float(current_value)
    num += 1
...
geo_mean **= 1 / num   # 在 calc_geomean 里完成开方
```

即先连乘所有正数比值、再开 `num` 次方，正是第 2 节的 geomean 公式。非数值或缺失值会被记录但不计入连乘，避免污染均值。`summarize_qor`（[`parse_vtr_task.py:501-526`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/parse_vtr_task.py#L501-L526)）先把各 task 的 `qor_results.txt` 拼成 summary，`calc_geomean` 再消费它。

**④ 如何解读结果。** 对「越小越好」的指标（延迟、线长、面积、运行时间、内存、块数、通道宽度），比值 `new/baseline`：**< 1.0 表示改进，> 1.0 表示回退**。geomean 把这些比值聚合后，一个 < 1.0 的头条 geomean（如 `critical_path_delay` geomean = 0.97）意味着「整套 benchmark 上关键路径延迟平均改善约 3%」。这正是 PR 描述里常写「delay geomean 0.97×, area geomean 1.00×」的来源。

#### 4.4.4 代码实践

> **目标**：用 `qor_compare.py` 把两次运行的 QoR 做成比对表，读懂 geomean。

1. 延续 4.3.4 的实践：你已经有了黄金 `parse_results.txt`（可复制一份存为 `baseline.txt`）和加 `-seed 1` 后的新运行结果。先解析出新结果（若尚未生成 `parse_results.txt`，按 4.3.4 步骤运行+解析）。
2. 把两份 `parse_results.txt` 准备成两个文件，例如 `parse_results_baseline.txt` 与 `parse_results_new.txt`。
3. 生成比对表（命令来自 [`doc/agents/testing.md:21-23`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/testing.md#L21-L23)）：

   ```shell
   ./vtr_flow/scripts/qor_compare.py parse_results_baseline.txt parse_results_new.txt -o comparison.xlsx
   ```

4. 用 Excel/LibreOffice 打开 `comparison.xlsx`，找到 `ratios` sheet 末尾的 `GEOMEAN` 行。
5. **观察点**：对 `critical_path_delay`、`routed_wirelength`、`min_chan_width`、`vtr_flow_elapsed_time` 四列，看它们的 geomean 是 < 1 还是 > 1；并结合 4.3 的 `[Fail]` 行交叉验证——「校验 Fail 的指标」和「geomean 偏离 1 较多」通常是同一批。
6. 也可用 `-t` 模式自动取黄金 vs 最新（需在 `vtr_flow/tasks` 目录下执行）：

   ```shell
   cd vtr_flow/tasks
   ../scripts/qor_compare.py -t regression_tests/vtr_reg_strong/strong_timing -o cmp.xlsx
   ```

   该模式会自动定位 `config/golden_results.txt` 与 `latest/parse_results.txt`（[`qor_compare.py:380-399`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/qor_compare.py#L380-L399)）。
7. 预期结果：得到一份含 `ratios`/`summary_data`/`summary` 三个 sheet 的 Excel，每个指标列末尾有 GEOMEAN。具体数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`qor_compare.py` 的比值方向是 `new/baseline` 还是 `baseline/new`？基准是哪个文件？
**答案**：是 `new/baseline`，且**第一个传入的文件是基准**（分母）。见 `make_ratios` 取 `list(raw_sheets.keys())[0]` 作为 `ref_sheet`（[`qor_compare.py:237-239`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/qor_compare.py#L237-L239)），以及 `safe_ratio_ref` 里 `num/denom` = `新/基准`。所以命令行里**第一个文件决定分母**，顺序写反会让「改进」显示成「回退」。`-t` 模式则固定黄金为基准。

**练习 2**：为什么 geomean 列里有时会出现空单元格？
**答案**：`safe_ratio_ref` 在分母为 0 或为 `-1`（缺失指标）时输出空串（[`qor_compare.py:328-331`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/qor_compare.py#L328-L331)），GEOMEAN 公式自动忽略空值。这保证了「某个 benchmark 缺某指标」不会让整列 geomean 失效——与 `calculate_individual_geo_mean` 在 Python 侧跳过非正值（[`parse_vtr_task.py:585-591`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/scripts/python_libs/vtr/parse_vtr_task.py#L585-L591)）是同一思想。

## 5. 综合实践

把本讲四个模块串成一个完整的「**新增一个回归任务并评估其 QoR**」任务，模拟一次真实的算法改动 PR 流程。

**背景**：假设你调整了布线器的一个参数，想确认它没有让任何 benchmark 回退，并量化整体得失。

**步骤**：

1. **建任务**（4.1）：按 [`doc/agents/testing.md:46-60`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/testing.md#L46-L60) 复制 `strong_timing` 为 `strong_routing_tweak`，编辑 `config.txt`，在 `script_params_common` 里加上你想验证的 VPR 布线参数（例如 `--router_lookahead MAP`，参考 u6-l4）。
2. **跑通并冻结基准**（4.2 + 4.3）：`run_vtr_task.py` 运行 → `-create_golden` 生成黄金 → `-check_golden` 确认自洽通过。把这份黄金另存为 `baseline.txt`。
3. **引入改动**：在 `config.txt` 里把参数换成你的新值（或改回默认作为对照），重新运行 + 解析，得到 `new.txt`。
4. **合格性判定**（4.3）：`-check_golden`，记录所有 `[Fail]` 行及其相对比值，判断回退是否在可接受范围。
5. **整体得失**（4.4）：`qor_compare.py baseline.txt new.txt -o tweak.xlsx`，读取 `critical_path_delay`、`routed_wirelength`、`min_chan_width`、`vtr_flow_elapsed_time` 的 GEOMEAN，判断整体是改进还是回退。
6. **登记任务**：若决定保留该任务，把路径加进 [`task_list.txt`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/tasks/regression_tests/vtr_reg_strong/task_list.txt)（testing.md 第 59 行）。
7. **交付结论**：用一句话总结，例如「`critical_path_delay` geomean 0.98×（改善 2%），无 Fail，`vtr_flow_elapsed_time` geomean 1.01×（运行时间略增），建议合入」。

**验收标准**：能说清 (a) 你的改动让哪些指标 Fail、比值多少；(b) 整体 geomean 是 < 1 还是 > 1；(c) 据此给出合入/不合入的建议。具体数值依赖本地运行，**待本地验证**。

> 工程纪律提醒：练习产生的 `golden_results.txt`、`config.txt`、`comparison.xlsx` 等改动，除非确属你要提交的内容，否则请用 `git checkout` 还原，**不要把练习产物混进真实 PR**（这也呼应 CLAUDE.md「由人类决定提交内容」的要求）。

## 6. 本讲小结

- VTR 回归测试是**三层脚本**（`run_reg_test.py` → `run_vtr_task.py` → `parse_vtr_task.py`）+ **两层配置**（`task_list.txt` + `config.txt`）的分层结构，上层编排、底层干活。
- `run_vtr_task.py` 用 `args.run = not(其它标志)` 把 run/parse/create_golden/check_golden/calc_geomean 设为互斥动作；带上后处理标志就不会重跑流程。
- 一个 task = 电路 × 架构 × 脚本参数的笛卡尔积，由 `create_jobs` 展开成若干 `Job`；**黄金结果会反喂**成下次运行的 `--min_route_chan_width_hint` 与 `-expect_fail`。
- **解析管道**用 `name;file;regex` 模式把日志抓成 TSV；`parse_file` 产出全量 `parse_results.txt`（供校验），`qor_parse_file` 产出精选 `qor_results.txt`（供 geomean）。
- **黄金校验**不是相等而是容差：`Range`（相对）、`RangeAbs`（相对或绝对）、`Equal`（相等）三类准则；只强制校验准则里声明过的指标。
- **QoR 对比**用「逐项比值 + 几何均值」聚合整套 benchmark；对越小越好的指标，geomean < 1.0 = 改进。两条路径：`calc_geomean` 写 `qor_geomean.txt`（套件当下水平），`qor_compare.py` 出 Excel（相对基准得失）。

## 7. 下一步学习建议

- **回到上层、横向打通**：本讲聚焦回归脚本。建议回头读 u9-l2 的 Catch2 单元测试，建立「单元测试 vs 回归测试」的完整取舍观——何时该加单元测试、何时必须加回归任务。
- **深入解析配置生态**：浏览 `vtr_flow/parse/parse_config/` 与 `vtr_flow/parse/pass_requirements/` 全目录，理解不同流程（pack_only、route_only、ap、titan、analysis_only）各自配了哪些指标与容差，这能帮你为新流程编写合理的准则。
- **结合具体算法讲义**：当你改动某阶段算法时，对照 u4–u7 讲义，预判它会主要影响哪些 QoR 指标（如改聚簇影响 `num_clb`、改布线前瞻影响 `min_chan_width` 与 `critical_path_delay`），从而在 PR 里给出有依据的 QoR 预期。
- **进一步阅读源码**：若想理解运行期并行与失败回收，读 `run_vtr_task.py` 的 `run_parallel`/`run_vtr_flow_process`（475-504）；若想理解任务目录与多 run 管理，读 `util.py` 的 `get_next_run_dir`/`get_active_run_dir` 与 `RunDir`。

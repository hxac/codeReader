# CLI 入口与三大命令

## 1. 本讲目标

学完本讲你应该能够：

- 说清楚 `genai-bench` 这个终端命令是怎么从 Python 包里"冒出来"的（入口点机制）。
- 理解 click 的 group / command / option / pass_context 概念，并能看懂 `cli.py` 顶部那段装饰器。
- 区分 `benchmark` / `excel` / `plot` 三个子命令各自的职责，知道它们分别定义、注册在哪里。
- 看懂 `cli.add_command(...)` 的组织方式，理解 `excel`、`plot` 为什么被拆到独立的 `report.py`，以及它们如何复用 analysis 子系统。

## 2. 前置知识

需要先掌握（已在 u1-l1 建立）：

- genai-bench 是一个 Python 包，用 `pyproject.toml` + hatchling 构建。
- 它对外提供三大能力：CLI 工具、实时 UI 仪表盘、实验分析器。
- 项目入口 `__init__.py` 必须最先执行 `gevent.monkey.patch_all()`，以配合 Locust 的协程式并发。

本讲要补充的基础概念：

**CLI（命令行接口）**：在终端敲 `genai-bench benchmark ...` 这样的命令，背后其实是一个 Python 函数被调用。我们需要一个机制把"终端命令"和"Python 函数"对应起来。

**click**：一个流行的 Python CLI 库。核心思想是"装饰器即命令"——给普通函数加上 `@click.command()` 或 `@click.group()`，它就变成了一个命令或命令组；用 `@click.option(...)` 声明参数，click 会自动解析命令行、做类型转换、生成 `--help`。

**group 与 command 的关系**：group（命令组）像文件夹，command（命令）像文件。`genai-bench` 本身是一个 group，下面挂 `benchmark`、`excel`、`plot` 三个 command。所以用法是 `genai-bench <子命令>`。

**entry point（入口点）**：pip 安装包时，会在 Python 环境的可执行目录生成一个脚本。`pyproject.toml` 里的 `[project.scripts]` 表声明"脚本名 → 模块:函数"的映射，pip 据此生成 `genai-bench` 这个命令。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `pyproject.toml` | 声明 `genai-bench` 入口点，指向 `genai_bench.cli.cli:cli` |
| `genai_bench/cli/cli.py` | 定义 `cli` group 与 `benchmark` 命令，并在文件末尾注册三个子命令 |
| `genai_bench/cli/report.py` | 定义 `excel` 与 `plot` 两个报告命令，复用 analysis 子系统 |
| `genai_bench/cli/option_groups.py` | 把 benchmark 的大量选项拆成若干"选项组"装饰器（api_options 等） |
| `genai_bench/cli/validation.py` | 提供 callback 式校验，其中 `validate_api_backend` 会把选中的 user_class 放进 `ctx.obj` |
| `docs/getting-started/cli-guidelines.md` | 官方 CLI 用法文档，列出三命令的选项 |

## 4. 核心概念与源码讲解

### 4.1 click group 与 CLI 入口

#### 4.1.1 概念说明

本模块要回答的第一个问题是：**为什么在终端敲 `genai-bench` 就能运行 Python 代码？** 答案分两层：

1. **包安装层**：`pyproject.toml` 的 `[project.scripts]` 声明一个入口点，pip 安装时据此生成一个名为 `genai-bench` 的可执行脚本，该脚本会调用 `genai_bench.cli.cli` 模块里的 `cli` 函数。
2. **框架层**：`cli` 函数被 `@click.group()` 装饰，于是它成了一个"命令组"。命令组本身不做具体工作（函数体只有 `pass`），它的职责是承载子命令和公共选项（如 `--version`）。

#### 4.1.2 核心流程

从终端命令到代码的调用链：

```
终端: genai-bench benchmark ...
   │
   │  (pip 据入口点生成的脚本)
   ▼
入口点: genai_bench.cli.cli:cli
   │
   ▼
@click.group() 装饰后的 cli 函数
   ├── 解析 group 级选项 (--version / --help)
   └── 根据子命令名 "benchmark" 分发到对应 command
```

`--version` 是挂在 group 上的选项，所以 `genai-bench --version` 在任何子命令之前就能生效。

#### 4.1.3 源码精读

先看入口点声明。[pyproject.toml:43-44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L43-L44) 定义脚本 `genai-bench` 指向 `genai_bench.cli.cli:cli`，即"模块 `genai_bench.cli.cli` 里的 `cli` 对象"：

```toml
[project.scripts]
genai-bench = "genai_bench.cli.cli:cli"
```

接着看 group 的定义。[genai_bench/cli/cli.py:47-59](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L47-L59) 是整个 CLI 的根：

```python
@click.group()
@click.version_option(
    version=GENAI_BENCH_VERSION,
    prog_name="genai-bench",
    message="%(prog)s version %(version)s",
    help="Show the current version of genai-bench and exit.",
)
@click.pass_context
def cli(ctx):
    """Main CLI entry point for genai-bench."""
    pass
```

逐条解释：

- `@click.group()`：把 `cli` 变成命令组，能挂子命令。
- `@click.version_option(...)`：自动注册 `--version`，版本号取自 `GENAI_BENCH_VERSION`（u1-l1 讲过的单一数据源设计）。
- `@click.pass_context`：把 click 的上下文对象 `ctx` 传进函数。这个 `ctx` 是整条命令链共享的"口袋"，后面 benchmark 会用它传递数据（见 4.2）。
- 函数体只有 `pass`：group 不做具体事，只负责分发。

文件末尾是标准入口块，方便直接以模块方式运行。[genai_bench/cli/cli.py:672-673](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L672-L673):

```python
if __name__ == "__main__":
    cli()
```

#### 4.1.4 代码实践

实践目标：确认 group 与 `--version` 真实生效。

操作步骤：

1. 安装 genai-bench（见 u1-l2）。
2. 运行 `genai-bench --version`。
3. 运行 `genai-bench --help`。

需要观察的现象：

- `--version` 输出形如 `genai-bench version 0.0.5`，且与 `pyproject.toml` 的 `version` 字段一致。
- `--help` 顶部列出 `benchmark / excel / plot` 三个子命令。

预期结果：`--version` 由 `@click.version_option` 自动生成，`--help` 由 click 根据 group 下注册的子命令自动生成。若版本号与 pyproject.toml 不一致，说明运行的不是当前安装版本。

#### 4.1.5 小练习与答案

**Q1**：为什么 `cli` 函数体里只有 `pass`？
**A1**：因为 `cli` 是命令组（group），职责是承载子命令和公共选项（如 `--version`），具体工作由挂在它下面的 `benchmark/excel/plot` 子命令完成。

**Q2**：`@click.pass_context` 传进来的 `ctx` 有什么用？
**A2**：`ctx` 是命令链共享的上下文，可以用来在父命令与子命令之间、或多个参数校验回调之间传递数据（例如把选中的 user_class 放进 `ctx.obj`，见 4.2）。

### 4.2 三大子命令的注册与职责

#### 4.2.1 概念说明

`cli` group 下面挂三个子命令，职责各不相同：

| 子命令 | 职责 | 定义位置 |
|---|---|---|
| `benchmark` | 发起基准测试，压测模型服务并采集 token 级指标 | `cli.py` |
| `excel` | 把已跑完的实验结果导出成 Excel 报告 | `report.py` |
| `plot` | 把实验结果绘成图（支持预设 / 自定义配置） | `report.py` |

一个关键设计：`benchmark` 定义在 `cli.py` 里（它是核心，且依赖大量 cli 层的选项组与校验回调）；而 `excel`、`plot` 定义在独立的 `report.py` 里，再被 import 进 `cli.py`。这样 `report.py` 专注于"读结果 + 出报告"，复用 analysis 子系统，与压测主流程解耦。

#### 4.2.2 核心流程

注册在 `cli.py` 末尾一次性完成：

```
benchmark  ← 定义在 cli.py 本地
excel      ← 从 report.py import
plot       ← 从 report.py import
        │
        ▼  cli.add_command(...)
   cli group 持有三个子命令
```

import 语句在文件顶部。[genai_bench/cli/cli.py:32](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L32) 把两个报告命令引进来：

```python
from genai_bench.cli.report import excel, plot
```

三个 `add_command` 在文件末尾集中出现。[genai_bench/cli/cli.py:668-670](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L668-L670):

```python
cli.add_command(benchmark)
cli.add_command(excel)
cli.add_command(plot)
```

**benchmark 如何接收海量参数**：benchmark 的参数极多（认证、采样、存储、指标……）。若全写 `@click.option`，函数头上会堆几十个装饰器。项目用"选项组"来拆分。[genai_bench/cli/cli.py:62-73](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L62-L73) 把多个选项组装饰器叠在 benchmark 上：

```python
@click.command(context_settings={"show_default": True})
@api_options
@model_auth_options
@oci_auth_options
@server_options
@experiment_options
@sampling_options
@distributed_locust_options
@object_storage_options
@storage_auth_options
@metrics_options
@click.pass_context
def benchmark(ctx, api_backend, api_base, ...):
```

每个 `xxx_options` 是 `option_groups.py` 里的普通函数，内部用一连串 `click.option(...)(func)` 给函数加参数。这样 benchmark 的参数被按主题分组，便于维护。

> 关于装饰器顺序：Python 装饰器是**自下而上**应用的。`option_groups.py` 里也有一条约定注释提醒"新增选项请加在函数顶部，因为装饰器是倒序生效的"。不过对 benchmark 而言，click 会按选项名（`--api-backend` → `api_backend`，`-` 转 `_`）以**关键字参数**传给回调，不依赖位置顺序，所以函数签名里长长的参数列表顺序不必严格匹配装饰器顺序。

**benchmark 与子系统的衔接：ctx.obj**。benchmark 通过 `@click.pass_context` 拿到 ctx，然后读取两个关键值。[genai_bench/cli/cli.py:278-279](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L278-L279):

```python
user_class = ctx.obj.get("user_class")
user_task = ctx.obj.get("user_task")
```

这两个值是谁放进去的？是 click 的参数校验回调（callback）。当用户传 `--api-backend openai` 时，`validate_api_backend` 被触发，根据后端名查表得到对应的 User 类，存进 `ctx.obj`。[genai_bench/cli/validation.py:257-270](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L257-L270):

```python
def validate_api_backend(ctx, param, value):
    api_backend = value.lower()
    user_class = API_BACKEND_USER_MAP.get(api_backend)
    if not user_class:
        raise click.BadParameter(f"{value} is not a supported API backend.")
    if ctx.obj is None:
        ctx.obj = {}
    ctx.obj["user_class"] = user_class
    return api_backend
```

类似地，`--task` 会触发 `validate_task`，把选中的任务函数存进 `ctx.obj["user_task"]`（见 [validation.py:350](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L350)）。于是 benchmark 函数体里就能直接拿到"该用哪个 User 类、跑哪个任务"，而不必自己再查一次表。这就是 `ctx.obj` 作为"跨回调数据口袋"的价值。

#### 4.2.3 源码精读（补充）

benchmark 的函数体非常长（[cli.py:74-666](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L74-L666)），完整的压测主流程留给 u8-l1 讲。本讲只需理解一个衔接点：benchmark 在校验通过后，把 user_class 接上认证、host、后端名（[cli.py:282-284](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L282-L284)），再交给 Locust 环境与分布式 runner 执行。

```python
user_class.auth_provider = auth_provider
user_class.host = api_base
user_class.api_backend = api_backend
```

#### 4.2.4 代码实践

实践目标：亲手体验 `ctx.obj` 的传递机制，并验证后端校验。

操作步骤：

1. 读 [validation.py:257-270](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L257-L270)（validate_api_backend）与 [validation.py:313-352](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L313-L352)（validate_task）。
2. 运行 `genai-bench benchmark --help`，观察 `--api-backend` 与 `--task` 的可选值。

需要观察的现象：

- `--api-backend` 的可选值正是 `API_BACKEND_USER_MAP` 的 key（openai、oci-cohere、aws-bedrock 等）。
- 故意填一个不存在的 backend（如 `genai-bench benchmark --api-backend foobar ...`），会得到 `BadParameter` 报错，错误文案正是 validate_api_backend 里 `raise` 的那一句。

预期结果：校验回调在校验的同时把数据写进 `ctx.obj`，benchmark 函数体随后读取——这条链路完整闭环。

#### 4.2.5 小练习与答案

**Q1**：为什么 excel/plot 要拆到 `report.py`，而 benchmark 留在 `cli.py`？
**A1**：benchmark 是压测主流程，依赖 cli 层的大量选项组与校验回调；excel/plot 只负责"读结果出报告"，复用 analysis 子系统，与压测解耦，放独立文件更清晰，也方便单独维护与测试。

**Q2**：benchmark 函数签名有几十个参数，顺序写错会出问题吗？
**A2**：不会。click 按选项名（`--api-backend` → `api_backend`）以关键字参数传给回调，不依赖位置顺序，所以参数顺序与装饰器顺序不必严格对应。

### 4.3 report 模块：excel 与 plot 命令实现

#### 4.3.1 概念说明

`report.py` 是 CLI 层的"报告工厂"。它的两个命令都不发请求、不压测，而是：

1. 从实验目录读结果（analysis 子系统的 loader）。
2. 调用 analysis 子系统的报告生成器（excel_report / flexible_plot_report）出表或出图。

所以 `report.py` 本身很薄，真正的逻辑都在它 import 的 analysis 模块里。这正是"复用"的体现：benchmark 跑完后也会自动调用同一套 analysis 函数生成报告（见 [cli.py:545-570](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L545-L570)）；而 excel/plot 命令让用户可以对**任意旧实验**重新出报告、换百分位、换绘图配置，不必重跑压测。

#### 4.3.2 核心流程

excel 命令流程：

```
--experiment-folder --excel-name --metric-percentile --metrics-time-unit
        │
        ▼  load_one_experiment(folder)
   (experiment_metadata, run_data)
        │
        ▼  create_workbook(...)
   <excel-name>.xlsx
```

plot 命令流程：

```
--experiments-folder --group-key --preset/--plot-config --filter-criteria ...
        │
        ├── (可选) --list-fields：扫描数据列出可用字段后退出
        ├── 加载绘图配置 (preset > plot-config > 默认 2x4)
        ├── 加载实验数据 (单实验 / 多实验)
        ├── --validate-only：仅校验配置后退出
        └── plot_experiment_data_flexible(...) 出图
              └── 出错时 fallback 到 plot_experiment_data(...)
```

#### 4.3.3 源码精读

先看 report.py 的 import，它直接揭示了对 analysis 子系统的复用。[genai_bench/cli/report.py:1-15](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L1-L15):

```python
from genai_bench.analysis.excel_report import create_workbook
from genai_bench.analysis.experiment_loader import (
    load_multiple_experiments,
    load_one_experiment,
)
from genai_bench.analysis.plot_report import plot_experiment_data
from genai_bench.cli.validation import validate_filter_criteria
from genai_bench.logging import LoggingManager, init_logger
from genai_bench.utils import is_single_experiment_folder
```

**excel 命令**。[report.py:18-57](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L18-L57) 定义四个选项，函数体只有三行业务逻辑：

```python
def excel(ctx, experiment_folder, excel_name, metric_percentile, metrics_time_unit):
    """Exports the experiment results to an Excel file."""
    LoggingManager("excel")
    _ = init_logger("genai_bench.excel")
    excel_path = os.path.join(experiment_folder, excel_name + ".xlsx")
    experiment_metadata, run_data = load_one_experiment(experiment_folder)
    create_workbook(
        experiment_metadata, run_data, excel_path, metric_percentile, metrics_time_unit
    )
```

四个选项：

- `--metric-percentile`：从 `mean/p25/.../p99` 选一个统计量（默认 `mean`）。
- `--experiment-folder`：实验目录（required，click 会校验路径存在）。
- `--excel-name`：输出 Excel 文件名（代码会自动补 `.xlsx`）。
- `--metrics-time-unit`：延迟单位 `s/ms`（默认 `s`）。

excel 命令的"复用"体现在：它本身不解析指标、不画表格，全部委托给 `load_one_experiment`（读盘）和 `create_workbook`（写表）。

**plot 命令**。[report.py:60-141](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L60-L141) 定义九个选项（含 `--list-fields`、`--validate-only`、`--verbose` 三个 flag），函数体（[report.py:142-306](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L142-L306)）比 excel 复杂得多，因为它要支持多种工作模式。

配置加载有明确优先级。[report.py:235-243](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L235-L243):

```python
if preset:
    config = PlotConfigManager.load_preset(preset)
elif plot_config:
    config = PlotConfigManager.load_from_file(plot_config)
else:
    config = PlotConfigManager.load_preset("2x4_default")
```

即 `preset > plot-config > 默认 2x4`。数据加载区分单实验与多实验（[report.py:254-268](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L254-L268)），最终绘图委托给 `plot_experiment_data_flexible`（[report.py:289-295](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L289-L295)），失败时回退到旧的 `plot_experiment_data`（[report.py:301-305](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L301-L305)）。

#### 4.3.4 代码实践（本讲主实践）

实践目标：对比 excel 与 plot 的参数，并说明它们如何复用 analysis 子系统。

操作步骤：

1. 读 [report.py:18-57](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L18-L57)（excel）和 [report.py:60-141](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L60-L141)（plot）。
2. 运行 `genai-bench excel --help` 与 `genai-bench plot --help`，把帮助输出与源码里的 `@click.option` 逐一对照。
3. 需要回答两个问题（参考答案见下方）：
   - excel 接收哪些参数？plot 接收哪些参数？
   - 两者各自复用了 analysis 子系统的哪些函数？

预期结果（参考答案）：

| 命令 | 选项 | 复用的 analysis 函数 |
|---|---|---|
| excel | `--metric-percentile`、`--experiment-folder`、`--excel-name`、`--metrics-time-unit` | `load_one_experiment`、`create_workbook` |
| plot | `--experiments-folder`、`--group-key`、`--filter-criteria`、`--plot-config`、`--preset`、`--metrics-time-unit`、`--list-fields`、`--validate-only`、`--verbose` | `load_one_experiment`/`load_multiple_experiments`、`PlotConfigManager`、`plot_experiment_data_flexible`、`plot_experiment_data` |

复用说明（示例答案）：excel 与 plot 都先用 analysis 的 loader 把实验目录读成 `(experiment_metadata, run_data)`，再交给 analysis 的报告生成器——excel 给 `create_workbook` 出表，plot 给 `plot_experiment_data_flexible` 出图。CLI 命令本身只负责解析参数、组织调用顺序，不重复实现指标解析与绘图逻辑。这与 benchmark 跑完自动出报告用的是同一套 analysis 函数（见 [cli.py:545-570](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L545-L570)）。

#### 4.3.5 小练习与答案

**Q1**：plot 命令的配置加载优先级是什么？
**A1**：`--preset` 最高，其次 `--plot-config`，都没给就用默认的 `2x4_default`。

**Q2**：`--list-fields` 和 `--validate-only` 这两个 flag 分别有什么用？
**A2**：`--list-fields` 扫描真实实验数据，列出所有可用字段后直接退出（帮助用户写 plot 配置）；`--validate-only` 只校验绘图配置是否合法、不真正出图（用于调试配置）。

## 5. 综合实践

把本讲三块知识串起来，用"源码阅读 + 命令体验"还原 `genai-bench` 的命令体系。

任务：

1. 从 [pyproject.toml:43-44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L43-L44) 出发，说明 `genai-bench` 命令如何映射到 `cli` 函数。
2. 在 [cli.py:47-59](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L47-L59) 找到 group 定义，解释 `--version` 为何是 group 级选项。
3. 在 [cli.py:668-670](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L668-L670) 找到三命令注册，对照 [cli.py:32](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L32) 的 import，说明 excel/plot 来自哪里。
4. 运行 `genai-bench --help`、`genai-bench benchmark --help`、`genai-bench excel --help`、`genai-bench plot --help`，把输出与源码里的 `@click.option` 逐一对应，验证"装饰器即命令"。
5. 写一段 200 字以内的说明：benchmark 依赖 cli 层（选项组 + 校验），而 excel/plot 复用 analysis 子系统——这种拆分带来什么好处？

预期产出：一张"命令 → 定义文件 → 复用子系统"的对照表，以及对"`ctx.obj` 传递 user_class"这条链路的文字描述。

## 6. 本讲小结

- `genai-bench` 命令由 `pyproject.toml` 的 `[project.scripts]` 入口点映射到 `genai_bench.cli.cli:cli`。
- `cli` 是一个 `@click.group()`，承载 `--version` 等公共选项，函数体本身为空（只负责分发）。
- 三个子命令 `benchmark / excel / plot` 在 [cli.py:668-670](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L668-L670) 用 `cli.add_command(...)` 注册；benchmark 定义在 cli.py，excel/plot 定义在 report.py。
- benchmark 用选项组装饰器（api_options 等）管理海量参数，click 按选项名以关键字参数传入，顺序无关。
- 参数校验回调通过 `ctx.obj` 把 `user_class`/`user_task` 传递给 benchmark 函数体，是跨回调的数据口袋。
- report.py 的两个命令本质是 analysis 子系统的薄封装：loader 读结果 + 生成器出报告。

## 7. 下一步学习建议

- 想看 benchmark 主流程的全貌，进入 u8-l1（benchmark 主流程编排 capstone）。
- 想了解选项组的细节与跨参数校验，进入 u8-l2（CLI 选项分组与校验机制）。
- 想了解 excel/plot 背后的 analysis 实现，进入 u6（实验分析与报告）单元。
- 想了解 user_class 的多后端体系，进入 u3（User 后端体系与请求执行）单元。

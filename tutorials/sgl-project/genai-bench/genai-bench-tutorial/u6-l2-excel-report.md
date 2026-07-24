# Excel 报告生成

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `create_workbook` 的输入是什么、`percentile` 参数如何从一张「场景×并发」的指标网格里挑出一个统计量。
- 描述一份 Excel 报告里五张工作表（Summary / Appendix / Experiment Metadata / Aggregated Metrics for Each Run / Individual Request Metrics）各自的含义与行粒度。
- 解释「空列折叠」与「表头重命名」两道后处理的作用。
- 认识 `SCENARIO_MAP` 的真实职责（给场景字符串起人类可读的名字），并知道当前代码里并不包含「定价/成本」计算。
- 用一条 `genai-bench excel` 命令或一个最小脚本，把任意一次已落盘的实验结果重出一份 Excel，而不必重跑压测。

## 2. 前置知识

本讲是「纯读 + 产出报告」环节，不会发任何请求。你需要先掌握以下概念（它们都在前置讲义里讲过）：

- **实验 = 一张 scenario×concurrency 的二维网格**：详见 [u6-l1 实验结果加载](u6-l1-experiment-loader.md)。加载器把磁盘上的 JSON 读成嵌套字典 `run_data[scenario(str)][concurrency(int)] = {aggregated_metrics, individual_request_metrics}`（类型别名 `ExperimentMetrics`）。
- **指标分层模型**：详见 [u4-l3 指标数据模型与时间单位转换](u4-l3-metrics-models-and-time-units.md)。单请求是 `RequestLevelMetrics`，整轮 run 是 `AggregatedMetrics`，其中 `stats` 是一个 `MetricStats`，每个字段又是一个 `StatField`（含 `mean / p25 / p50 / … / p99`）。
- **`StatField` 支持下标取值**：`metrics.stats.ttft["p99"]` 这种写法等价于 `getattr`，正是 `percentile` 参数能「按名字挑一列」的关键。
- **时间单位转换**：`TimeUnitConverter` 只换算 `ttft / tpot / e2e_latency / output_latency` 这四个延迟量，token 数与吞吐原样保留。
- **openpyxl**：Python 操作 `.xlsx` 的库。一个 `Workbook` 含若干 `Worksheet`，`sheet.append(row)` 追加一行，单元格可设字体、对齐、数字格式、列宽、分组折叠等。

一句话回顾数据流：压测落盘 JSON → `load_one_experiment` 读成 `ExperimentMetrics` → `create_workbook` 写成 Excel。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [genai_bench/analysis/excel_report.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/excel_report.py) | 报告生成的全部核心逻辑：总装入口 `create_workbook`、五张工作表的构造函数、空列折叠 / 表头重命名 / 场景排序等辅助函数。 |
| [genai_bench/cli/report.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py) | `excel` 与 `plot` 两个 CLI 子命令的薄封装；`excel` 把 `create_workbook` 暴露成命令行。 |
| [examples/experiment_excel.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/examples/experiment_excel.py) | 最小示例脚本：加载一个实验目录、直接生成 Excel。 |

辅助但相关（不在本讲精读范围，但会被引用）：

- [genai_bench/analysis/experiment_loader.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/experiment_loader.py)：`load_one_experiment` 的来源（u6-l1 已详讲）。
- [genai_bench/metrics/metrics.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py)：`AggregatedMetrics / MetricStats / StatField` 的定义。
- [genai_bench/time_units.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/time_units.py)：`TimeUnitConverter`。
- [tests/analysis/test_excel_na.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/analysis/test_excel_na.py)：用 mock 数据构造 `create_workbook` 输入的范例，本讲实践会复用它。

---

## 4. 核心概念与源码讲解

### 4.1 workbook 构造：create_workbook 总装入口

#### 4.1.1 概念说明

`create_workbook` 是 Excel 报告的**唯一总装入口**。它的职责很纯粹：拿一个「已经读好的实验」（`experiment_metadata` + `run_data`），按指定统计口径（`percentile`）和时间单位（`metrics_time_unit`），拼出一个多 sheet 的 `Workbook` 并落盘。

关键在于它**完全不碰磁盘上的原始 JSON、也不重跑压测**——输入是内存里的对象，这正是「同一份实验结果可以反复换口径重出报告」的前提。

#### 4.1.2 核心流程

`create_workbook` 的执行可以分成五个阶段：

```text
1. 对齐时间单位
   source_time_unit = experiment_metadata.metrics_time_unit
   if source_time_unit != metrics_time_unit:
       用 TimeUnitConverter 把 run_data 里的延迟字段换算成目标单位

2. 新建 Workbook，依次「挂」上五张工作表
   create_summary_sheet(...)            # 摘要：每场景一行
   create_appendix_sheet(...)           # 附录：每 [场景,并发] 一行
   create_experiment_metadata_sheet(...)# 元数据：键值对
   create_aggregated_metrics_sheet(...) # 整轮聚合明细
   create_single_request_metrics_sheet(...)# 逐请求明细

3. 任务相关后处理
   if task == "text-to-speech":
       _rename_headers(...)   # TTFT -> TTFB

4. 通用后处理
   _group_empty_columns(wb)   # 折叠全空列
   del wb[wb.sheetnames[0]]   # 删掉 openpyxl 默认的空 "Sheet"

5. 保存 wb.save(output_file)
```

注意第 4 步的一个细节：openpyxl 的 `Workbook()` 会自带一张名为 `Sheet` 的空表，代码在所有业务表建完之后，用 `del wb[wb.sheetnames[0]]` 把它删掉。

#### 4.1.3 源码精读

总装入口的完整签名与前两个阶段：

[genai_bench/analysis/excel_report.py:85-124](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/excel_report.py#L85-L124) —— `create_workbook` 的签名与时间单位对齐。注意 `percentile` 默认 `"mean"`、`metrics_time_unit` 默认 `"s"`；换算时 `convert_metrics_dict(metrics_dict, metrics_time_unit, source_time_unit)` 的参数顺序是 `to_unit, from_unit`，即「从实验原始单位换算到本次想要的单位」。

接着是「挂五张表 + 后处理 + 删默认表 + 存盘」：

[genai_bench/analysis/excel_report.py:126-160](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/excel_report.py#L126-L160) —— 新建 `Workbook()` 后依次调用五个 `create_*_sheet`，TTS 任务做表头改名，统一折叠空列，删除首张默认表，最后 `wb.save`。

`percentile` 是怎么「挑一列」的？答案在 `StatField.__getitem__`：

[genai_bench/metrics/metrics.py:92-98](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L92-L98) —— `StatField` 实现了 `__getitem__`，所以 `metrics.stats.ttft[percentile]` 这种「下标取值」其实是转发到 `getattr`，于是 `"mean"` / `"p99"` 这样的字符串就能直接当键用。整个 Excel 模块正是靠这一点，把「选哪个百分位」做成一个贯穿所有表的字符串参数。

#### 4.1.4 代码实践

> 实践目标：不依赖真实实验，用 mock 数据亲手喂一次 `create_workbook`，生成一份 Excel 并用 openpyxl 读回，验证 sheet 名与某个单元格的值。

操作步骤（参考测试 [tests/analysis/test_excel_na.py:11-95](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/analysis/test_excel_na.py#L11-L95) 的构造方式）：

1. 在仓库根目录写一个临时脚本 `tmp_excel_demo.py`（**示例代码，非项目原有文件，运行后请自行删除**）：

   ```python
   # 示例代码
   import tempfile, os
   from openpyxl import load_workbook
   from genai_bench.analysis.excel_report import create_workbook
   from genai_bench.metrics.metrics import AggregatedMetrics, MetricStats, StatField
   from genai_bench.protocol import ExperimentMetadata

   scenario = "D(100,100)"
   metadata = ExperimentMetadata(
       cmd="genai-bench benchmark", benchmark_version="test", api_backend="openai",
       auth_config={}, api_model_name="m", model="m", task="text-to-text",
       num_concurrency=[1, 2], batch_size=None, iteration_type="num_concurrency",
       traffic_scenario=[scenario], additional_request_params={},
       server_engine="e", server_version="v1", server_gpu_type="A100",
       server_gpu_count="1", max_time_per_run_s=60, max_requests_per_run=10,
       experiment_folder_name="/tmp", metrics_time_unit="s",
   )

   def agg(conc, speed):
       return AggregatedMetrics(
           scenario=scenario, num_concurrency=conc, iteration_type="num_concurrency",
           stats=MetricStats(output_inference_speed=StatField(mean=speed),
                             ttft=StatField(mean=0.5), e2e_latency=StatField(mean=1.0)),
           mean_total_tokens_throughput_tokens_per_s=20.0,
       )

   run_data = {
       scenario: {
           1: {"aggregated_metrics": agg(1, 12.0), "individual_request_metrics": [{}]},
           2: {"aggregated_metrics": agg(2, 18.0), "individual_request_metrics": [{}]},
       }
   }

   with tempfile.TemporaryDirectory() as td:
       out = os.path.join(td, "demo.xlsx")
       create_workbook(metadata, run_data, out, percentile="mean")
       wb = load_workbook(out)
       print("sheetnames =", wb.sheetnames)
   ```

2. 运行：`python tmp_excel_demo.py`。

3. 需要观察的现象：打印出的 `sheetnames` 应包含 `Summary`、`Appendix`、`Experiment Metadata`、`Aggregated Metrics for Each Run`、`Individual Request Metrics` 五张表，且**不包含** openpyxl 默认的 `Sheet`。

4. 预期结果：五张业务表齐全、默认空表已被删除。若把某条 `output_inference_speed` 的 mean 改成低于阈值 `10 tokens/s`，Summary 里对应行会变成 `N/A`（见 4.2）。

> 提示：如果你本地已经跑过一次基准、有现成的实验目录，可以直接用 4.3 的真实命令，无需手造 mock 数据。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `create_workbook(..., percentile="p99")` 传进去，Summary / Appendix 表里的数值会怎样变化？

**答案**：所有形如 `metrics.stats.<field>[percentile]` 的取值，都会从「均值」切到「p99 分位」。由于 `StatField.__getitem__` 把字符串下标转发到属性访问，`"p99"` 直接命中 `StatField.p99`。注意 Summary 里 `Total Throughput (tokens/min)` 用的是 `mean_total_tokens_throughput_tokens_per_s * 60`，这是整轮聚合量、**不**受 `percentile` 影响。

**练习 2**：`create_workbook` 第 4 步为什么要 `del wb[wb.sheetnames[0]]`？删的会不会是业务表？

**答案**：`openpyxl.Workbook()` 构造时自带一张名为 `Sheet` 的空表，且它排在 `sheetnames[0]`。业务表都是用 `wb.create_sheet(...)` 追加的，排在它之后，所以删第一个只会删掉这张默认空表，不会误伤业务表。

---

### 4.2 sheet 与列处理：五张工作表 + 空列折叠 + 表头重命名

#### 4.2.1 概念说明

五张表按「粒度从粗到细」排列：

| 工作表 | 行粒度 | 用途 |
| --- | --- | --- |
| **Summary** | 每个**场景**一行 | 回答「该场景下，能扛住目标速度的最大并发/批大小是多少，对应总吞吐多少」 |
| **Appendix** | 每个**[场景, 并发]**一行 | 详细指标：TTFT、单请求推理速度、输出吞吐、端到端延迟、RPS、总吞吐 |
| **Experiment Metadata** | 每个**元数据字段**一行 | 把整次实验的「身份证」键值对原样铺出来 |
| **Aggregated Metrics for Each Run** | 每个**[场景, 并发]**一行 | 把 `AggregatedMetrics` 的字段 + 每个统计字段的 JSON 块全量铺开 |
| **Individual Request Metrics** | 每个**单条请求**一行 | 逐请求粒度的明细，最细 |

此外还有两个横跨所有表的「后处理」：

- **空列折叠**：把整列都是 `None / 0`（或全零 JSON）的列分组并隐藏，避免一堆无意义空列干扰阅读。
- **表头重命名**：TTS 任务里把 `TTFT`（time to first token）改成 `TTFB`（time to first byte/audio），因为语音任务吐出的是音频字节而非 token。

> 关于「定价信息」：学习目标里提到了它，但需要澄清——**当前 `excel_report.py` 中没有任何定价 / 成本计算**（对 `genai_bench/analysis` 全目录检索 `pricing/price/cost` 无命中）。`SCENARIO_MAP` 的真实职责只是「把机器场景字符串翻译成人类可读的名字」，下文 4.2.3 会讲清楚。

#### 4.2.2 核心流程

**Summary 的「选最优并发」逻辑**（chat 任务为例）：

```text
对每个场景 scenario:
    summary_value = -1                       # 还没找到合格并发
    遍历 sorted(并发档位) iteration:
        speed = metrics.stats.output_inference_speed[percentile]
        if speed 不是 None 且 speed > threshold(默认 10 tokens/s):
            if 已经有合格并发 且 当前并发更大:
                if 与上一档吞吐/RPS 相对差异 < 5%:   # is_within_relative_difference
                    break                      # 边际收益消失，停止上探
            summary_value = max(summary_value, 当前并发)
            total_tokens_per_minute = mean_total_tokens_throughput * 60
    if summary_value == -1:
        记 warning，该行写 "N/A"
    else:
        写 (GPU, 场景名, summary_value, total_tokens_per_minute)
```

阈值随任务变：`embedding` 用 `mean_total_tokens_throughput_tokens_per_s > 100`，其余 chat 类用 `output_inference_speed > 10`；`tts` 走另一条「取最大音频吞吐」的分支。

**空列折叠 `_group_empty_columns` 的逻辑**：

```text
只对 "Aggregated Metrics for Each Run" 和 "Individual Request Metrics" 两张表
对每一列 col:
    检查第 2 行起所有数据格:
        全是 None / 0 / 0.0          -> 视为空
        是 "{...}" JSON 且值全 None/0 -> 视为空
    若整列都空 -> 记入 empty_cols
把 empty_cols 里连续的列合并成分组区间，逐列设 outlineLevel=1 且 hidden=True
```

**表头重命名 `_rename_headers` 的逻辑**：遍历每个表第 1 行单元格，对单元格文本做子串替换（TTS 时 `TTFT→TTFB`、`ttft→ttfb`）。

#### 4.2.3 源码精读

**SCENARIO_MAP**：场景字符串 → 人类可读名称的固定映射表，目前只有 5 条：

[genai_bench/analysis/excel_report.py:18-25](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/excel_report.py#L18-L25) —— 例如 `"D(100,100)"` 映射到 `"Scenario 2: Chatbot/Dialog D(100,100)"`。映射不到的场景（如自定义场景）会原样回退（`SCENARIO_MAP.get(scenario, scenario)`）。注释里的 `# TODO: Add more` 也说明它是一张需要手工维护的小表，**不含任何价格信息**。

**场景排序 `reorder_scenarios`**：让「在 SCENARIO_MAP 里的场景」按映射表顺序排在前面，「新场景」排在后面：

[genai_bench/analysis/excel_report.py:653-662](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/excel_report.py#L653-L662) —— 这样 Summary / Appendix 等表会先展示标准场景，再展示读者自定义的场景，顺序稳定。

**Summary 的核心选并发逻辑**（chat 分支）：

[genai_bench/analysis/excel_report.py:256-306](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/excel_report.py#L256-L306) —— 注意 `metric_value > threshold` 的判定、用 `is_within_relative_difference` 判断「再加并发也不涨了」就 `break`、以及 `summary_value == -1` 时写 `"N/A"` 并打 warning。

**边际收益判定 `is_within_relative_difference`**：

[genai_bench/analysis/excel_report.py:713-749](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/excel_report.py#L713-L749) —— 当前后两档的「总吞吐」与「RPS」相对差异都 < 5% 时认为已到瓶颈，停止上探并发。

**Appendix 的逐行构造**（chat 分支）：

[genai_bench/analysis/excel_report.py:414-423](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/excel_report.py#L414-L423) —— 每个 `[场景, 并发]` 一行，依次填 GPU、场景名、并发、`ttft[percentile]`、`output_inference_speed[percentile]`、`mean_output_throughput_tokens_per_s`、`e2e_latency[percentile]`、`requests_per_second`、`mean_total_tokens_throughput_tokens_per_s`。

**通用布局 `_create_sheet_with_common_layout`**：

[genai_bench/analysis/excel_report.py:163-188](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/excel_report.py#L163-L188) —— Summary / Appendix 共用：建表 → 写表头 → 写数据行 → 合并「GPU Type」列 → 给非 A/B/C 列套千分位数字格式 → 自适应列宽 → 表头加粗。

**Aggregated Metrics 表把统计字段铺成 JSON**：

[genai_bench/analysis/excel_report.py:505-575](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/excel_report.py#L505-L575) —— 表头分三段：`metadata_headers`（scenario、iteration_type）+ `base_headers`（`AggregatedMetrics` 顶层字段，排除 stats 等）+ `stats_headers`（每个 `RequestLevelMetrics` 字段的统计 JSON）。延迟字段的表头会经 `TimeUnitConverter.get_unit_label` 加上单位，并用 `stats_field_mapping` 把「带单位的显示名」映射回真实字段名再取值。

**空列折叠 `_group_empty_columns`**：

[genai_bench/analysis/excel_report.py:28-72](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/excel_report.py#L28-L72) —— 只作用于上述两张「宽表」，识别全空列、合并连续区间、设 `outlineLevel=1` 与 `hidden=True`，在 Excel 里表现为「可展开的折叠分组」。

**表头重命名 `_rename_headers`**：

[genai_bench/analysis/excel_report.py:75-82](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/excel_report.py#L75-L82) —— 子串级替换，遍历所有表第 1 行。

#### 4.2.4 代码实践

> 实践目标：亲手验证「阈值未达标 → Summary 写 N/A」与「空列折叠」两件事。

操作步骤：

1. 复用 4.1.4 的脚本骨架，但把 `output_inference_speed` 的 mean 全部设成 `5.0`（低于阈值 10）。
2. 运行后用 `load_workbook` 打开输出，读 `wb["Summary"]` 第 2 列为 `"Scenario 2: Chatbot/Dialog D(100,100)"` 的那一行。
3. 观察第 3、4 列。

需要观察的现象 / 预期结果：第 3、4 列应均为字符串 `"N/A"`，与测试 [tests/analysis/test_excel_na.py:59-95](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/analysis/test_excel_na.py#L59-L95) 的断言一致；同时控制台会输出一条 warning，提示「请补更低的并发档位」。再打开 `Aggregated Metrics for Each Run` 表，应能看到某些全零的统计列被折叠隐藏（Excel 里点分组按钮可展开）。

#### 4.2.5 小练习与答案

**练习 1**：为什么空列折叠只对 `Aggregated Metrics for Each Run` 和 `Individual Request Metrics` 两张表生效，而不动 Summary？

**答案**：见 [genai_bench/analysis/excel_report.py:30-33](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/excel_report.py#L30-L33) 的 `target_sheets` 白名单。这两张是「宽表」——会把每个统计字段（甚至每个请求的每个字段）都摊成列，极易出现全空列（例如非 chat 任务的 `tpot`/`output_*`）。Summary / Appendix 是人工挑过的窄列集，没有折叠必要。

**练习 2**：Summary 里 `Total Throughput (tokens/min)` 的值是怎么来的？它受 `percentile` 影响吗？

**答案**：来自 `metrics.mean_total_tokens_throughput_tokens_per_s * 60`（见 4.2.3 的 Summary 片段），是把「每秒总 token 吞吐」换算成「每分钟」。这是整轮聚合量，**不受 `percentile` 影响**；受 `percentile` 影响的是「用来判定是否达标的 `output_inference_speed`」以及 Appendix 里的各项 `stats.<field>[percentile]`。

**练习 3**：TTS 任务为什么要把 `TTFT` 改名成 `TTFB`？

**答案**：TTS（`text-to-speech`）流式吐出的是音频字节而非文本 token，「首字节延迟」比「首 token 延迟」更贴切，故 [genai_bench/analysis/excel_report.py:150-151](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/excel_report.py#L150-L151) 在 `task == "text-to-speech"` 时做子串替换。

---

### 4.3 excel 命令封装：report.py 把 analysis 暴露成 CLI

#### 4.3.1 概念说明

`create_workbook` 是个普通 Python 函数，普通用户不会去写脚本调它。`genai_bench/cli/report.py` 里的 `excel` 子命令就是它的「命令行外壳」：用 click 把四个关键参数（百分位、实验目录、输出名、时间单位）接进来，调用加载器读结果，再调 `create_workbook` 落盘。

这正呼应了 [u1-l4](u1-l4-cli-entry-and-commands.md) 的结论：`excel` / `plot` 是 analysis 子系统的**薄封装**——只负责「读结果 + 出报告」，不重跑压测，因此可以对任意一次旧实验反复重出。

#### 4.3.2 核心流程

```text
genai-bench excel \
    --experiment-folder <实验目录>      # 必填，须存在
    --excel-name <名>                   # 必填，生成 <名>.xlsx
    --metric-percentile mean|p25|...|p99 # 默认 mean
    --metrics-time-unit s|ms            # 默认 s

执行:
  excel_path = experiment_folder / (excel_name + ".xlsx")
  experiment_metadata, run_data = load_one_experiment(experiment_folder)
  create_workbook(experiment_metadata, run_data, excel_path,
                  metric_percentile, metrics_time_unit)
```

注意输出文件**写在 `--experiment-folder` 里面**，与原始结果 JSON 放在一起。

#### 4.3.3 源码精读

`excel` 子命令的完整定义：

[genai_bench/cli/report.py:18-57](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L18-L57) —— 四个 `@click.option`：`--metric-percentile`（限定在 `mean/p25/p50/p75/p90/p95/p99`）、`--experiment-folder`（`exists=True` 校验、必填）、`--excel-name`（必填）、`--metrics-time-unit`（`s`/`ms`，默认 `s`）。函数体先初始化日志，拼出 `excel_path`，调 `load_one_experiment` 读结果，最后调 `create_workbook`。

`excel` 命令复用的就是 u6-l1 讲过的加载器入口：

[genai_bench/analysis/experiment_loader.py:56-121](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/experiment_loader.py#L56-L121) —— `load_one_experiment` 读 `experiment_metadata.json` 与各 run JSON，返回 `(experiment_metadata, run_data)`，正是 `create_workbook` 的两个主输入。

最小示例脚本（与 `excel` 命令等价，只是少了 CLI 解析）：

[examples/experiment_excel.py:1-21](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/examples/experiment_excel.py#L1-L21) —— 把 `folder_name` 指向真实实验目录，`load_one_experiment` 后直接 `create_workbook(..., "experiment_summary_sheet_5.xlsx")`。

#### 4.3.4 代码实践

> 实践目标：用真实命令（或等价脚本）对一份实验结果重出 Excel。

操作步骤：

1. 确认你有一个实验目录（含 `experiment_metadata.json` 与若干 `..._concurrency_<n>_time_<t>s.json`）。如果没有，可先按 [u1-l2](u1-l2-install-and-first-run.md) 跑一次最小基准，或直接复用 4.1.4 的 mock 数据脚本。
2. 运行命令（**待本地验证**：以下命令需有真实实验目录才能成功）：
   ```bash
   genai-bench excel \
       --experiment-folder path/to/your_experiment \
       --excel-name my_report \
       --metric-percentile p99 \
       --metrics-time-unit ms
   ```
3. 或运行示例脚本：把 `examples/experiment_excel.py` 里的 `folder_name` 改成你的实验目录后 `python examples/experiment_excel.py`。

需要观察的现象 / 预期结果：在实验目录下生成 `my_report.xlsx`（或 `experiment_summary_sheet_5.xlsx`），用 Excel / WPS / `load_workbook` 打开，能看到 4.2.1 列出的五张表；由于传了 `--metric-percentile p99`，Appendix 里的 TTFT / 推理速度等取的是 p99 分位；由于传了 `--metrics-time-unit ms`，延迟表头与数值都换算成了毫秒。

#### 4.3.5 小练习与答案

**练习 1**：`--excel-name` 给的是 `my_report`，最终文件名为什么是 `my_report.xlsx`？

**答案**：见 [genai_bench/cli/report.py:53-56](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L53-L56)，代码用 `os.path.join(experiment_folder, excel_name + ".xlsx")` 拼路径，会自动补 `.xlsx` 后缀，所以用户只需给主名。

**练习 2**：`--metric-percentile` 与 `--metrics-time-unit` 分别影响什么？把 `s` 改成 `ms`，token 计数列会变吗？

**答案**：`--metric-percentile` 影响所有 `stats.<field>[percentile]` 的取值（Summary 的达标判定 + Appendix / Aggregated 表的分位列）；`--metrics-time-unit` 只换算 `ttft / tpot / e2e_latency / output_latency` 四个延迟量（由 `TimeUnitConverter.LATENCY_FIELDS` 白名单决定），**token 计数、吞吐、RPS 都不变**。这与 [u4-l3](u4-l3-metrics-models-and-time-units.md) 讲的单位转换规则一致。

---

## 5. 综合实践

把本讲三块知识串起来：**用 mock 数据走完「加载 → 按指定口径出 Excel → 读回校验」全流程**，并解释每张表是怎么来的。

任务：

1. 仿照 4.1.4 的脚本，构造一个含 **2 个场景**（其中一个用自定义场景串如 `D(500,50)`，不在 `SCENARIO_MAP` 里）、**每个场景 2~3 个并发档**的 `run_data`。让其中一档的 `output_inference_speed` 均值跨过阈值 10、更高档位用接近的吞吐制造「边际消失」。
2. 调用 `create_workbook(metadata, run_data, out, percentile="p99", metrics_time_unit="ms")`。
3. 用 `load_workbook` 读回，编程式地完成下面四问并打印结果：
   - Summary 表里，标准场景那行的「Use Case」是不是 `SCENARIO_MAP` 给的可读名？自定义场景那行是不是原样回退？
   - Summary 表里两个场景各自选中的并发档分别是多少（或 `N/A`）？
   - Appendix 表里某行的 TTFT 数值，是不是 `StatField.p99` 换算成毫秒后的值？
   - `Aggregated Metrics for Each Run` 表里，是否存在被折叠隐藏的全零列？（提示：检查 `sheet.column_dimensions[letter].hidden`。）
4. 用一段话把「为什么这五张表分别长这样」讲清楚，重点说明 `percentile` 与 `metrics_time_unit` 各自穿透到了哪些表、哪些列。

> 预期结果（待本地验证）：标准场景显示可读名、自定义场景原样显示；跨过阈值的场景给出具体并发档、没跨过的给 `N/A`；Appendix 的 TTFT 是 p99×1000（若原始为秒）；宽表里的全零列 `hidden` 属性为 `True`。这道题同时检验了 4.1（总装）、4.2（五表 + 折叠 + 重命名）、4.3（口径参数如何穿透）三块内容。

## 6. 本讲小结

- `create_workbook` 是 Excel 报告的总装入口：输入是已加载好的 `experiment_metadata` + `run_data`，按 `percentile`（默认 `mean`）与 `metrics_time_unit`（默认 `s`）出报告，全程不碰原始 JSON、不重跑压测。
- 一份报告五张表，粒度由粗到细：Summary（每场景一行，选「能达标的最大并发」）、Appendix（每 [场景,并发] 一行的详细指标）、Experiment Metadata（元数据键值对）、Aggregated Metrics for Each Run（整轮字段 + 统计 JSON）、Individual Request Metrics（逐请求明细）。
- Summary 的「选最优并发」靠阈值判定（chat 默认 `output_inference_speed > 10 tokens/s`）+ `is_within_relative_difference`（吞吐/RPS 相对差异 < 5% 即认为到瓶颈停止上探）；不达标则写 `N/A` 并 warning。
- 两道横切后处理：`_group_empty_columns` 把宽表里的全空列折叠隐藏；`_rename_headers` 在 TTS 任务里把 `TTFT` 改成 `TTFB`。
- `SCENARIO_MAP` 只负责把场景字符串翻译成人类可读名（且需手工维护），**当前代码不含任何定价/成本计算**；`reorder_scenarios` 让标准场景排在前、自定义场景排在后。
- `genai-bench excel` 子命令是 `create_workbook` 的薄封装：`--experiment-folder` / `--excel-name` 必填，`--metric-percentile` / `--metrics-time-unit` 可调，输出文件落在实验目录内。

## 7. 下一步学习建议

- 想看「另一条出报告的路」？下一讲 [u6-l3 绘图配置系统 plot_config](u6-l3-plot-config-system.md) 会讲声明式绘图配置（`PlotSpec` / `PlotConfig` / preset），是 Excel 之外的图形成报告路径。
- 想理解 `percentile` 之外的统计字段（`min/max/stddev/p25~p99`）是怎么算出来的？回顾 [u4-l2 运行级聚合 AggregatedMetricsCollector](u4-l2-aggregated-metrics-collector.md)。
- 想把 Excel 报告接进自动化流程？可结合 [u8-l4 测试体系、CI 与发布](u8-l4-testing-ci-and-release.md)，在 CI 里跑 `genai-bench excel` 对历史实验批量重出报告。
- 建议动手扩展：仿照 `SCENARIO_MAP` 给你常用的自定义场景加可读名，观察 Summary / Appendix 的「Use Case」列如何随之变化。

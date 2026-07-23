# 灵活绘图与 plot 命令

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `FlexiblePlotGenerator` 如何消费一份 `PlotConfig`（u6-l3 讲过的声明式配置）和实验数据，真正画出 PNG；
- 区分三种 `group_key`（`none` / `traffic_scenario` / 自定义键）各自画出什么样的图、文件名长什么样；
- 理解「多线图（multi-line）」与「多分组」为什么会冲突、代码如何自动降级；
- 读懂 `plot_report.py` 提供的可复用绘图原语（`plot_graph` / `plot_error_rates` / 数据切片器），以及它和遗留实现 `plot_experiment_data` 的关系；
- 掌握 `genai-bench plot` 命令如何把「配置 + 数据 + 校验 + 生成」串成一条流水线，并知道何时会回退到遗留绘图。

本讲承接 [u6-l3 绘图配置系统](u6-l3-plot-config-system.md)：那里讲的是「画什么」的描述语言（`PlotConfig`/`PlotSpec`），本讲讲的是「怎么画出来」的执行引擎，以及暴露给用户的 `plot` 命令。

## 2. 前置知识

在进入源码前，先建立三块直觉。

**(1) 一份实验数据长什么样？**
经过 u6-l1 的实验加载器，磁盘上的实验目录会被读成内存里的 `run_data_list`，它的类型在 [genai_bench/analysis/experiment_loader.py:12-23](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/experiment_loader.py#L12-L23) 定义：

```python
MetricsData = (
    Dict[Literal["aggregated_metrics"], AggregatedMetrics]
    | Dict[Literal["individual_metrics"], List[RequestLevelMetrics]]
)
ExperimentMetrics = Dict[
    str,   # traffic-scenario（场景字符串，如 "D(100,100)"）
    Dict[
        int,        # concurrency-level（并发档位，如 1, 2, 4, 8）
        MetricsData,
    ],
]
```

也就是说，一次实验是一张 **场景 × 并发** 的二维网格，每个格子里装着 `aggregated_metrics`（一个 run 的聚合摘要）。绘图要做的，就是把这张网格里的数值「投影」到坐标轴上。

**(2) 声明式配置 vs 执行引擎。**
u6-l3 定义的 `PlotConfig` 是一份**纯描述**：「第 (0,0) 格画一张线图，X 取 `mean_output_throughput_tokens_per_s`，Y 取 `stats.ttft.mean`」。它本身不会画任何东西。本讲的 `FlexiblePlotGenerator` 才是**执行者**：它读这份描述，到实验数据里按字段路径（如 `stats.ttft.mean`）逐格取值，交给 matplotlib 画出来。

**(3) 「线」的两层含义：多分组 vs 多字段。**
这是本讲最容易混淆、也最关键的一点：

- **多分组（multiple groups）**：图上有几条「身份不同的线」，例如三个不同场景、或两个不同 `server_version`。它由 `group_key` 决定。
- **多字段（multi-line / multiple y_fields）**：一张子图里把同一份数据的多个指标叠在一起，例如同一条 `e2e_latency` 的 `mean / p90 / p99` 三条线。它由 `PlotSpec` 里写 `y_fields`（而非单个 `y_field`）决定。

`FlexiblePlotGenerator` 的核心难点，就是处理「一个子图既要分组、又是多字段」时的冲突。

**(4) matplotlib 术语速查。** `Figure` 是整张画布，`Axes`（复数 `axs`）是画布上的一个子图区域；`position=(row, col)` 决定一个 `PlotSpec` 落在网格的哪一格；`plot_graph` 画线/散点，`plot_error_rates` 画堆叠柱状图。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [genai_bench/analysis/flexible_plot_report.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py) | 执行引擎：`FlexiblePlotGenerator` 消费 `PlotConfig`，按 `group_key` 三态分发绘图；以及顶层入口 `plot_experiment_data_flexible` 与配置校验 `validate_plot_config_with_data`。 |
| [genai_bench/analysis/plot_report.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_report.py) | 可复用绘图原语库：`plot_graph`（单线）、`plot_error_rates`（错误率堆叠柱）、数据切片器 `get_scenario_data`/`get_group_data`/`extract_traffic_scenarios`；以及遗留实现 `plot_experiment_data`（2×4 硬编码，作为 fallback）。 |
| [genai_bench/cli/report.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py) | `genai-bench plot` 子命令的薄封装：组合「加载配置 → 加载数据 → 校验 → 灵活绘图」，失败时回退到遗留绘图。 |
| [genai_bench/analysis/plot_config.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_config.py) | （u6-l3 已详讲）`PlotConfig`/`PlotSpec`/`PlotConfigManager`，本讲作为输入引用。 |
| [examples/plot_configs/](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/examples/plot_configs) | 四份示例配置（如 `custom_2x2.json`、`comprehensive_multi_line.json`），是本讲实践的素材。 |

数据流一句话概括：**`plot` 命令 → `PlotConfigManager` 取配置 → `experiment_loader` 取数据 → `FlexiblePlotGenerator` 按 `group_key` 切片并调 `plot_graph`/`plot_error_rates` 出图 → 落盘 PNG**。

## 4. 核心概念与源码讲解

### 4.1 灵活绘图生成器 FlexiblePlotGenerator

#### 4.1.1 概念说明

`FlexiblePlotGenerator` 是整个绘图子系统的「引擎」。它把两样东西接在一起：

- 一份 `PlotConfig`（描述「画什么」）；
- 一份 `run_data_list`（描述「数据在哪」）。

然后按用户指定的 `group_key`，决定**怎样把数据切片**、**画几张图**、**每张图上放几条线**。它是 u6-l3 那套声明式配置的「消费者」，也是 u6-l1 实验加载结果的「消费者」，把两边捏成 PNG。

关键设计：它只负责「按配置驱动」，自己不硬编码任何指标或坐标轴——这就是它能被任意 JSON 配置驱动的根本原因。

#### 4.1.2 核心流程

引擎入口是 [generate_plots](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L33-L61)，它的核心是一个对 `group_key` 的**三态分发**：

```
group_key 的取值            →  调用                  →  产出
─────────────────────────────────────────────────────────────
"none"                      → _plot_single_analysis  →  每个场景一张图（最适合多字段）
"traffic_scenario"          → _plot_by_scenario      →  所有场景叠在一张图（多分组）
其它（如 server_version）    → _plot_by_group         →  每个场景一张图，图内按该键分组
```

三种模式都遵循同一个五步骨架：

1. **取源时间单位**：从第一条实验元数据 `metrics_time_unit` 读出数据原本存的是 `s` 还是 `ms`（[L41-L44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L41-L44)）。
2. **切数据**：把 `run_data_list` 切成「标签 → 并发档位映射 + 数据列表 + 标签列表」三件套（由 `get_scenario_data`/`get_group_data` 完成，见 4.2）。
3. **建画布**：`_create_figure` 按 `PlotLayout` 的 `rows×cols` 建 matplotlib figure（[L176-L191](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L176-L191)）。
4. **填子图**：`_plot_metrics` 遍历每个分组、每个 `PlotSpec`，按单线/多字段分发到 `_plot_single_line_metric` 或 `_plot_multi_line_metric`（[L193-L236](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L193-L236)）。
5. **落盘**：`_finalize_and_save_plots` 隐藏空格子，另存「合并大图」+「每子图单张 PNG」（[L789-L832](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L789-L832)）。

#### 4.1.3 源码精读

**三态分发**，注意 `none` 走的是完全不同的「单场景」分支：

```python
# genai_bench/analysis/flexible_plot_report.py:46-61
if group_key == "none":
    self._plot_single_analysis(...)        # 每个场景一张图
elif group_key == "traffic_scenario":
    self._plot_by_scenario(...)            # 所有场景叠在一张图
else:
    self._plot_by_group(...)               # 每个场景一张图，图内按 group_key 分组
```

**单场景模式 `_plot_single_analysis`**（[L63-L108](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L63-L108)）：它对**每一个**场景单独建一张图，并把 `labels` 设成 `[""]`（即「只有一个分组」）。这正是多字段图最理想的使用场景——一张图里画 `mean/p90/p99` 三条线，不存在「多分组」的混淆。文件前缀是 `single_analysis_{sanitize_string(scenario)}`。

**按场景叠图模式 `_plot_by_scenario`**（[L110-L135](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L110-L135)）：调用 `get_scenario_data` 把所有场景收成多条线、画在同一张图上，文件前缀 `traffic_scenario`。此时每个场景是一条线。

**自定义键模式 `_plot_by_group`**（[L137-L174](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L137-L174)）：先枚举所有 `traffic_scenario`，对**每个场景**画一张图，图内再按 `group_key`（如 `server_version`）分组。文件前缀 `{sanitize_string(traffic_scenario)}_group_by_{group_key}`。

**多字段 × 多分组冲突的自动降级**——这是引擎最聪明的地方，位于 [_plot_metrics](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L193-L236)：

```python
# genai_bench/analysis/flexible_plot_report.py:204-222
has_multi_line = any(plot.is_multi_line() for plot in self.config.plots)
has_multiple_groups = len(labels) > 1

if has_multi_line and has_multiple_groups:
    logger.warning(...)  # 多字段图最适合单场景，多个分组会糊成一团
    self._plot_metrics_single_line_fallback(...)
    return
```

道理：若一张子图本就要画 3 个字段（3 条线），而当前又有 4 个分组（4 个场景），那就是 12 条线挤在一个小格子里，无法阅读。所以代码**自动把多字段图降级为只画第一个字段的单线图**（[L238-L297](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L238-L297)），并打印 warning。这也解释了为什么内置 preset `multi_line_latency` / `single_scenario_analysis` 这类多字段配置，最好搭配 `--group-key none` 使用。

**取值与单位换算**——单线绘制 [_plot_single_line_metric](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L343-L423) 里，用 `PlotConfigManager.get_field_value` 按点分路径取数，并对延迟类字段做 `s↔ms` 换算：

```python
# genai_bench/analysis/flexible_plot_report.py:368-393（节选）
x_val = PlotConfigManager.get_field_value(metrics, plot_spec.x_field)   # 如 requests_per_second
y_val = PlotConfigManager.get_field_value(metrics, y_field_spec.field)  # 如 stats.ttft.mean
...
is_latency = TimeUnitConverter.is_latency_field(y_field_spec.field)
if is_latency and source_time_unit != metrics_time_unit:
    y_data = [TimeUnitConverter.convert_value(val, source_time_unit, metrics_time_unit)
              for val in y_data]
```

注意 `error_rate` + `bar` 是特例，会改走 `plot_error_rates`（[L396-L397](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L396-L397)）。

**多字段绘制 `_plot_multi_line_metric`**（[L464-L641](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L464-L641)）：为每个 `y_field` 取一条线，从 `tab10` colormap 取色、按 `["-","--","-_."," :"]` 轮换线型，图例放在子图外侧 `bbox_to_anchor=(1.05, 1)` 防遮挡；若任一字段含 `ttft` 且未显式设 `y_scale`，则默认对数轴（[L632-L636](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L632-L636)）。

**落盘 `_finalize_and_save_plots`**（[L789-L832](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L789-L832)）：先把不在 `used_positions` 的空格子隐藏，再调用 `_save_individual_subplots_multiline` 把每个有标题的子图单独存成 PNG（文件名用 `sanitize_string(title)`），最后存一张合并大图，命名为 `{前缀}_combined_plots_{rows}x{cols}.png`。

#### 4.1.4 代码实践

**实践目标**：用一份多字段配置验证「多字段 × 多分组」会触发降级。

**操作步骤**（示例代码，需本地有实验数据，否则标注待本地验证）：

```python
# 示例代码：不依赖真实数据，只演示降级判断逻辑
from genai_bench.analysis.plot_config import PlotConfigManager

# multi_line_latency 是多字段 preset
cfg = PlotConfigManager.load_preset("multi_line_latency")
print("是否含多字段图:", any(p.is_multi_line() for p in cfg.plots))
# 输出 True。当 labels 长度 > 1（多分组）时，引擎会打印 warning 并降级为单线。
```

**需要观察的现象**：当 `group_key` 不是 `none` 且数据里有多场景时，运行 `genai-bench plot` 会看到日志 `Multi-line plots detected with N groups/scenarios... Converting to single-line plots`。

**预期结果**：多字段图被自动改成「只画第一个 `y_field`」的单线图；要看到完整多字段效果，应改用 `--group-key none`。

> 待本地验证：真实多字段效果需一份含多个并发档位的实验目录，配合 `--group-key none --preset multi_line_latency` 运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么内置 preset `multi_line_latency` 推荐搭配 `--group-key none`？
**答**：它是多字段配置（同图多线）。若用 `traffic_scenario` 产生多分组，会触发 `_plot_metrics` 的降级分支，多字段被压成单字段，失去意义；`none` 走 `_plot_single_analysis`，每个场景单图、单分组，多字段能正确显示。

**练习 2**：`group_key="none"` 时，若实验有 3 个场景，会生成几张合并大图？
**答**：3 张。`_plot_single_analysis` 对每个场景各建一张 figure，前缀均为 `single_analysis_{场景}`。

---

### 4.2 单场景绘图原语 plot_report.py

#### 4.2.1 概念说明

`plot_report.py` 扮演两层角色：

1. **可复用绘图原语库**：`FlexiblePlotGenerator` 直接 import 使用的底层函数——`plot_graph`（画一条线/散点）、`plot_error_rates`（画错误率堆叠柱）、以及三个数据切片器 `get_scenario_data`/`get_group_data`/`extract_traffic_scenarios`。
2. **遗留实现**：`plot_experiment_data` 是「灵活绘图」出现之前的 2×4 硬编码版本，现在作为 `plot` 命令的兜底 fallback。

换句话说，本文件是「灵活绘图」的地基：灵活层只决定「把数据切成几组、每组配哪个 PlotSpec」，真正的「画一笔」动作仍委托给这里的 `plot_graph`。

#### 4.2.2 核心流程

三个数据切片器把 `run_data_list` 整理成统一的三元组 `(label_to_concurrency_map, concurrency_data_list, labels)`：

```
get_scenario_data(run_data_list)
  → 遍历每个实验的每个场景
  → labels 形如 "Scenario: D(100,100)"
  → 多个场景 = 多条线（多分组）

get_group_data(run_data_list, traffic_scenario, group_key)
  → 只取某个 traffic_scenario 下的数据
  → 按 metadata 的 group_key 字段（getattr）取标签
  → labels 形如 "server_version: v0.4.7"

extract_traffic_scenarios(run_data_list)
  → 收集所有出现过的 traffic_scenario（set 去重）
```

切片后，`plot_graph` 负责把一组 `(x_data, y_data)` 画成一个子图：当 X 轴是「Concurrency」时改成**等距刻度**（避免档位 1/2/4/8 间距不均），当 Y 轴标签含 `TTFT` 时自动切换**对数轴**。

#### 4.2.3 源码精读

**`plot_graph`——画一笔的核心**，[genai_bench/analysis/plot_report.py:20-107](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_report.py#L20-L107)。两处关键：

并发轴等距化（[L46-L51](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_report.py#L46-L51)）：

```python
# genai_bench/analysis/plot_report.py:46-51
if x_label == "Concurrency":
    x_positions = range(len(concurrency_levels))
    ax.set_xticks(x_positions)
    ax.set_xticklabels(concurrency_levels)
    x_data = x_positions   # 用等距位置画，但刻度文字仍是真实并发值
```

TTFT 自动对数轴（[L80-L87](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_report.py#L80-L87)）：当 `y_label` 含 `TTFT` 时设 `set_yscale("log")` 并配主次刻度定位器，便于看清跨数量级的首 token 延迟。

**数据切片器 `get_scenario_data`**，[genai_bench/analysis/plot_report.py:371-411](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_report.py#L371-L411)。它把「实验 × 场景」两层循环拍平成一个场景列表，每个场景对应一条线：

```python
# genai_bench/analysis/plot_report.py:405-410
for metadata, run_data in run_data_list:
    for scenario, concurrency_data in run_data.items():
        label = f"Scenario: {scenario}"
        label_to_concurrency_map[label] = sorted(concurrency_data.keys())
        concurrency_data_list.append(concurrency_data)
        labels.append(label)
```

**`get_group_data`**，[genai_bench/analysis/plot_report.py:414-449](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_report.py#L414-L449)。注意它用 `getattr(metadata, group_key, "Unknown")` 从实验元数据里取分组标签——这就是为什么 `--group-key` 可以填 `server_version`、`model` 等 `ExperimentMetadata` 的任意字段；而对特殊的 `experiment_folder_name` 还会取 basename（[L442-L443](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_report.py#L442-L443)）。

**`plot_error_rates`**，[genai_bench/analysis/plot_report.py:672-738](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_report.py#L672-L738)。按 HTTP 状态码做**堆叠柱**：对每个并发档位，把各状态码的 `count / num_requests` 堆上去，并用 `HTTPStatus(code).phrase` 把 `404` 渲染成 `404 Not Found`（[L701-L707](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_report.py#L701-L707)）。

**遗留实现 `plot_experiment_data`**，[genai_bench/analysis/plot_report.py:285-368](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_report.py#L285-L368)。文件顶部有 TODO：`# TODO: Remove this function when flexible plot report is fully tested.`（[L284](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_report.py#L284)）。它把 7 张固定的指标图硬编码进 `plot_metrics`（[L110-L281](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_report.py#L110-L281)），不可配置；它对 `group_key` 还有一道强校验——必须是 `ExperimentMetadata.model_fields` 里的字段，否则抛 `ValueError`（[L324-L327](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/plot_report.py#L324-L327)）。注意它**不认识 `none`** 这个取值。

#### 4.2.4 代码实践

**实践目标**：用 `experiment_plots.py` 示例直接驱动灵活绘图，观察产物文件名规律。

**操作步骤**：阅读 [examples/experiment_plots.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/examples/experiment_plots.py)，它演示了两类用法：

```python
# 示例：摘自 examples/experiment_plots.py:29-48
# ① 多实验、按 server_version 分组
plot_experiment_data_flexible(
    run_data_list, group_key="server_version", experiment_folder=folder_name
)
# ② 单实验、按场景叠图
plot_experiment_data_flexible(
    [[experiment_metadata, run_data]],
    group_key="traffic_scenario",
    experiment_folder=experiment_folder,
)
```

**需要观察的现象**：运行后，目标目录下会出现 `*_combined_plots_2x4.png` 合并图，以及按子图标题命名的单张 PNG。

**预期结果**：① 模式产物前缀形如 `<场景>_group_by_server_version`；② 模式产物前缀为 `traffic_scenario`。

> 待本地验证：脚本里的 `folder_name` 是占位符，需替换成真实实验目录才能跑通。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `plot_graph` 要在 X 轴是 Concurrency 时改用等距位置？
**答**：并发档位常是 1/2/4/8，数值间距不均，按真实值画会让低并发点挤在一起、高并发点拉得很开，读图困难。改成等距位置后视觉均匀，刻度文字仍标注真实档位。

**练习 2**：遗留 `plot_experiment_data` 与灵活版对 `group_key="none"` 的处理有何不同？
**答**：遗留版根本不识别 `none`，且要求 `group_key` 必须是 `ExperimentMetadata` 的字段，否则抛错；灵活版把 `none` 当作「单场景分析」的特殊模式，每个场景单独出图。

---

### 4.3 plot 命令封装 cli/report.py

#### 4.3.1 概念说明

`genai-bench plot` 是把前面所有零件组装起来的**薄封装**。它本身不画图，而是完成一条编排流水线：

```
解析 CLI 选项 → 加载 PlotConfig → 加载实验数据 → 校验配置 → 灵活绘图（失败则回退遗留绘图）
```

它的价值在于：把「配置来源」「数据来源」「校验」「生成」「兜底」这几件本可散落的事，收口到一个命令里，让用户不必写 Python 脚本也能出图。它和 `excel` 命令并列定义在 `report.py`（[L1-L15](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L1-L15) 导入 loader、analysis 等子系统）。

#### 4.3.2 核心流程

`plot` 命令的选项分几组，[genai_bench/cli/report.py:60-128](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L60-L128)：

| 选项 | 作用 |
| --- | --- |
| `--experiments-folder`（必填） | 单个实验目录，或含多个实验的目录（靠 `is_single_experiment_folder` 自动判断） |
| `--group-key`（必填） | `traffic_scenario` / `none` / 自定义键（如 `server_version`） |
| `--filter-criteria` | 过滤字典，如 `{'model': 'xxx'}`，由 `validate_filter_criteria` 解析 |
| `--plot-config` | 自定义 JSON 配置文件路径 |
| `--preset` | 5 个内置 preset 之一，**优先级高于 `--plot-config`** |
| `--metrics-time-unit` | `s` 或 `ms`，控制延迟轴显示单位 |
| `--list-fields` | 只列出实验里真实可用的字段，画完即退出 |
| `--validate-only` | 只校验配置不画图 |
| `--verbose` | 调试日志 |

命令体（[L130-L305](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L130-L305)）按以下顺序执行：

1. **`--list-fields` 短路**（[L150-L225](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L150-L225)）：加载数据后用 `PlotConfigManager.get_fields_from_data` 列出真实可用的 `stats.xx.mean` 等字段并退出；这是写自定义 JSON 前的「查字典」入口。
2. **加载配置**（[L228-L247](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L228-L247)）：`preset` > `plot-config` > 默认 `2x4_default`。
3. **加载数据**（[L250-L268](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L250-L268)）：单实验走 `load_one_experiment`，多实验走 `load_multiple_experiments`，都带上 `filter_criteria`。
4. **校验配置**（[L271-L285](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L271-L285)）：`validate_plot_config_with_data` 用真实数据核对每个 `PlotSpec` 的 `x_field`/`y_field`/`y_fields` 路径是否存在、`position` 是否越界；若 `--validate-only` 则到此为止。
5. **生成 + 兜底**（[L288-L305](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L288-L305)）：调 `plot_experiment_data_flexible`；一旦抛异常，回退到遗留 `plot_experiment_data`。

#### 4.3.3 源码精读

**配置加载的优先级**——`preset` 覆盖 `plot-config`：

```python
# genai_bench/cli/report.py:235-243
if preset:
    config = PlotConfigManager.load_preset(preset)
elif plot_config:
    config = PlotConfigManager.load_from_file(plot_config)
else:
    config = PlotConfigManager.load_preset("2x4_default")
```

**单/多实验的自动判别**，[genai_bench/cli/report.py:254-L265](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L254-L265)。判定函数 `is_single_experiment_folder`（[genai_bench/utils.py:19-34](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/utils.py#L19-L34)）只看「目录里有没有子目录」：没有子目录就当单实验，有就当多实验容器。据此把数据统一收成 `run_data_list`（单实验包成 `[(metadata, run_data)]`）。

**配置校验**——`validate_plot_config_with_data`（定义在 [flexible_plot_report.py:869-936](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L869-L936)）取一个真实 `aggregated_metrics` 样本，逐 `PlotSpec` 校验三件事：`x_field` 路径可达、每个 `y_field`/`y_fields` 路径可达、`position` 不越界。校验失败会返回错误列表（[L899-L934](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/flexible_plot_report.py#L899-L934)）。

**失败回退**——这是工程上的安全网：

```python
# genai_bench/cli/report.py:288-305
try:
    plot_experiment_data_flexible(run_data_list=..., group_key=group_key,
                                  experiment_folder=..., plot_config=config,
                                  metrics_time_unit=metrics_time_unit)
except Exception as e:
    logger.error(f"Error generating plots: {e}")
    logger.info("Falling back to original plotting system...")
    plot_experiment_data(run_data_list, group_key=group_key,
                         experiment_folder=experiments_folder)
```

注意回退时**不带** `plot_config` 和 `metrics_time_unit`——因为遗留版是硬编码 2×4、且自己从元数据读时间单位。这也是遗留版尚未删除的原因：它是一道保险。

**`--list-fields` 的字段发现**（[L183-L223](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L183-L223)）：用 `PlotConfigManager.get_fields_from_data` 从真实数据里抓出 `(字段路径, 值, 类型)`，按 `stats.` 前缀分成「直接指标」和「统计指标」两组打印，方便用户照着写自己的 JSON。失败时回退到静态全表 `get_available_fields`。

#### 4.3.4 代码实践

**实践目标**：用 `examples/plot_configs/custom_2x2.json` 跑通 `plot` 命令，对照 `PlotSpec` 解释每幅图。

**操作步骤**：

```bash
# 1.（可选）先探查可用字段
genai-bench plot \
  --experiments-folder <你的实验目录> \
  --group-key none \
  --list-fields

# 2. 用自定义 2x2 配置出图
genai-bench plot \
  --experiments-folder <你的实验目录> \
  --group-key none \
  --plot-config examples/plot_configs/custom_2x2.json \
  --metrics-time-unit ms
```

**对照 [custom_2x2.json](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/examples/plot_configs/custom_2x2.json) 解释四幅图**（均为 `--group-key none`，故每个场景出一组）：

| position | 标题 | X / Y 字段 | 含义 |
| --- | --- | --- | --- |
| (0,0) | Throughput vs Mean Latency | `mean_output_throughput_tokens_per_s` / `stats.e2e_latency.mean` | 吞吐—延迟曲线，看拐点 |
| (0,1) | RPS vs P99 Latency | `requests_per_second` / `stats.e2e_latency.p99` | 长尾延迟随 RPS 走势 |
| (1,0) | Concurrency vs TTFT | `num_concurrency` / `stats.ttft.mean` | 首 token 延迟 vs 并发（Y 自动对数轴） |
| (1,1) | Error Rate Analysis | `num_concurrency` / `error_rate` | 错误率堆叠柱（走 `plot_error_rates`） |

**需要观察的现象**：目录下生成 4 张子图 PNG + 1 张 `single_analysis_*.png` 合并图（`none` 模式每场景一组）；因 `--metrics-time-unit ms`，TTFT/延迟轴数值比 `s` 大 1000 倍。

**预期结果**：四幅图分别对应 JSON 里四个 `PlotSpec` 的 `position` 与字段；(1,0) 因 Y 含 `ttft` 自动对数轴；(1,1) 因 `error_rate`+`bar` 走堆叠柱而非普通线。

> 待本地验证：需一份含多并发档位的真实实验目录。无数据时可用 `--validate-only` 仅校验配置不画图。

#### 4.3.5 小练习与答案

**练习 1**：同时传 `--preset 2x4_default` 和 `--plot-config my.json`，哪个生效？
**答**：`--preset` 生效。代码里 `if preset:` 分支在前，`--plot-config` 只在未提供 preset 时使用（[report.py:235-243](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L235-L243)）。

**练习 2**：为什么 `plot` 命令在生成阶段要包一层 try/except 回退到 `plot_experiment_data`？
**答**：灵活绘图是较新的代码（`plot_report.py` 顶部 TODO 标注「等充分测试后删除遗留函数」）。回退是一道工程保险：若灵活路径因配置或数据异常崩溃，仍能用稳定的遗留 2×4 出图，保证命令不空手而归。

## 5. 综合实践

**任务：为一次 text-to-text 实验定制并跑通一条完整出图链路。**

1. **跑一次最小实验**（参考 u1-l2），得到一个实验目录，里面含 `experiment_metadata.json` 和若干 run JSON。
2. **探字段**：执行 `genai-bench plot --experiments-folder <目录> --group-key none --list-fields`，记下真实存在的 `stats.*` 路径。
3. **写配置**：仿照 `examples/plot_configs/multi_line_latency.json`，写一份自己的 `my_latency.json`，把 `e2e_latency` 的 `mean/p50/p90/p99` 放进同一个子图的 `y_fields`（多字段），布局 `1x1`。
4. **选对 group-key**：因为是多字段图，用 `--group-key none`（解释：若用 `traffic_scenario` 会触发降级）。
5. **出图并验证**：`genai-bench plot --experiments-folder <目录> --group-key none --plot-config my_latency.json --metrics-time-unit ms --validate-only` 先校验，再去掉 `--validate-only` 真出图。
6. **对照解释**：打开生成的 PNG，逐条说明图中每条线对应 JSON 里哪个 `y_field`、颜色/线型来自哪、为何 Y 轴是对数（若含 ttft）或线性。

**自检问题**：若把第 4 步误用 `--group-key traffic_scenario`，日志会出现什么？多字段线会怎样？（答案：出现 `Multi-line plots detected... Converting to single-line plots` 的 warning，多字段被压成只画第一个字段的单线。）

> 待本地验证：第 1 步需要可访问的目标模型服务；若暂时没有，可跳到第 2 步用任意旧实验目录练习配置与绘图。

## 6. 本讲小结

- `FlexiblePlotGenerator` 是 u6-l3 声明式 `PlotConfig` 的**执行引擎**：按 `group_key` 三态（`none`/`traffic_scenario`/自定义）分发，决定切几张图、每张图几条线。
- `none` 走 `_plot_single_analysis`（每场景一图、单分组），是多字段图的理想模式；`traffic_scenario` 把所有场景叠一图（多分组）；自定义键按 `getattr(metadata, group_key)` 在每个场景内分组。
- **多字段 × 多分组**会自动降级为单线并告警——这是引擎最关键的自我保护逻辑，也解释了多字段 preset 为何要配 `--group-key none`。
- `plot_report.py` 提供底层原语：`plot_graph`（并发轴等距化、TTFT 对数轴）、`plot_error_rates`（HTTP 状态码堆叠柱）、三个数据切片器；其 `plot_experiment_data` 是待删除的遗留 2×4 实现。
- `genai-bench plot` 是薄封装流水线：加载配置（preset > file > 默认）→ 加载数据（单/多实验自动判别）→ `validate_plot_config_with_data` 校验 → 灵活绘图，异常时回退遗留绘图。
- 时间单位 `metrics_time_unit` 从元数据读源单位，仅对 `ttft/tpot/e2e_latency/output_latency` 四个延迟字段换算，token/吞吐/计数原样保留。

## 7. 下一步学习建议

- **横向**：回到 [u6-l2 Excel 报告](u6-l2-excel-report.md)，对比「表格」与「绘图」两条出报告路径如何共用同一份 `run_data_list`，加深对 u6-l1 加载层作为统一输入的理解。
- **纵向（U7）**：本讲的「实时出图」是实验结束后的离线产物；下一单元 [u7-l2 实时仪表盘](u7-l2-live-dashboard.md) 讲压测**进行中**的实时 UI，可对比两者对 `AggregatedMetrics` 的不同消费方式。
- **工程**：阅读 `plot_report.py:284` 的 TODO，思考「灵活实现稳定后如何安全移除遗留 fallback」——这是 `plot` 命令 try/except 回退策略演进的终点，可作为一次小型重构练习。

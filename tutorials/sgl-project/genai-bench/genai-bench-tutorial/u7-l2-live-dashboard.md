# 实时仪表盘 Dashboard

## 1. 本讲目标

genai-bench 在压测「进行中」会把指标实时画到终端里，让你一边发请求一边看到 TTFT、吞吐、延迟分布和进度。这个实时画面由 `genai_bench/ui/` 子系统负责。本讲聚焦三个最小模块：

- **Dashboard 工厂与模式切换**：`create_dashboard` 如何根据环境变量 `ENABLE_UI` 在「真仪表盘 `RichLiveDashboard`」和「空操作 `MinimalDashboard`」之间切换。
- **面板与绘图更新**：指标面板、横向直方图、散点图分别是如何从 `LiveMetricsData` 数据流渲染出来的。
- **进度计算**：`handle_single_request` 如何把「时间进度」和「请求数进度」取较大值，驱动两条进度条。

学完本讲，你应该能：读懂 `ui/` 三个文件的分工；解释 `MinimalDashboard` 的 no-op 设计为何能让调用方零分支；并理解 `handle_single_request` 把「进度」和「指标刷新」合在一个入口背后的取舍。

## 2. 前置知识

本讲建立在以下已建立的认知上（不再重复细节）：

- **u4-l2 运行级聚合**：master 进程持有 `AggregatedMetricsCollector`，它内部维护一个 `_live_metrics_data` 字典（`ttft`/`input_throughput`/`output_throughput`/`output_latency` 四条序列 + 一个 `stats` 子字典），并通过 `get_live_metrics()` 把这个「实时快照」交出去。本讲的仪表盘就是这个快照的消费者。
- **u7-l1 DistributedRunner**：master 收到 worker 发来的 `request_metrics` 消息后，会 `gevent.spawn(self.dashboard.handle_single_request, ...)`。也就是说，**渲染发生在 master 进程**，worker 不画图。
- **rich 库**：Python 的终端美化库，能画面板（`Panel`）、布局（`Layout`）、进度条（`Progress`）。本讲的「图」其实不是 PNG，而是用字符拼出来的 `rich.text.Text`——这一点要特别记住，它决定了为什么散点图是用 `•` 点阵画的。

几个术语先对齐：

| 术语 | 含义 |
|---|---|
| `Layout` | rich 的可嵌套分屏容器，按「行/列」切割终端窗口，每块有名字（如 `input_latency`）。 |
| `Live` | rich 的「整屏刷新」上下文管理器，进入后用 `screen=True` 接管终端，每秒重绘若干次。 |
| no-op（空操作） | 方法体为 `pass`、什么都不做。`MinimalDashboard` 的所有方法都是 no-op。 |
| 数据快照 | 每次刷新时从 collector 拿一份「截至当前」的指标汇总，仪表盘只读不改它。 |

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
|---|---|---|
| [genai_bench/ui/dashboard.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/dashboard.py) | 两个 Dashboard 实现 + 工厂 `create_dashboard` | 核心主讲文件 |
| [genai_bench/ui/layout.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/layout.py) | 构建分屏 `Layout`、指标面板、进度条 | 渲染原语 |
| [genai_bench/ui/plots.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/plots.py) | 字符版横向直方图与散点图 | 渲染原语 |
| [genai_bench/metrics/aggregated_metrics_collector.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py) | 提供 `get_live_metrics()` / `get_ui_scatter_plot_metrics()` | 数据来源（上一讲） |
| [genai_bench/distributed/runner.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py) | master 收到请求指标后调 `handle_single_request` | 调用方（上一讲） |
| [genai_bench/cli/cli.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py) | 主循环里调用各 `dashboard.*` 方法编排一次实验 | 调用方 |
| [genai_bench/logging.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py) | 日志写进 layout 的 `logs` 块 | 横切配合 |

## 4. 核心概念与源码讲解

### 4.1 Dashboard 工厂与模式切换

#### 4.1.1 概念说明

genai-bench 的仪表盘有两种「人格」：

- **`RichLiveDashboard`**：真仪表盘，用 rich 的 `Live` 接管终端屏幕，每秒重绘 2 次，把面板、直方图、散点图、进度条、日志都铺到屏幕上。
- **`MinimalDashboard`**：空仪表盘，所有方法都是 `pass`，不分配屏幕、不渲染任何东西。

为什么需要两种？因为渲染实时 UI 会「接管终端」（`screen=True`），这在 CI 日志、非交互式终端、容器无 TTY、或者只想看最终 JSON 的场景里会捣乱。于是项目用一个开关 `ENABLE_UI` 让用户随时关掉它。**关键设计**是：两种人格实现**完全相同的接口**，调用方（runner / cli）不需要写任何 `if ENABLE_UI` 分支——想画就画，关了也只是静默无效。

这是经典的 **Null Object（空对象）模式**：与其用 `None` + 到处判空，不如提供一个「什么都不做的真实对象」，让接口契约保持一致。

#### 4.1.2 核心流程

```text
create_dashboard(metrics_time_unit)
        │
        ├─ 读取 os.getenv("ENABLE_UI", "true")
        │      └─ 默认 "true"：UI 默认开
        │
        ├─ 小写化后判断是否 ∈ {"true","1","yes","on"}
        │
        ├─ 是 ──> RichLiveDashboard(metrics_time_unit)   # 真渲染
        └─ 否 ──> MinimalDashboard(metrics_time_unit)    # 全 no-op
```

调用方拿到的对象类型注解统一是 `Dashboard = Union[RichLiveDashboard, MinimalDashboard]`，之后一律 `dashboard.xxx(...)`，不关心具体是哪一种。

`MinimalDashboard` 还有一个细节：它的 `live` 属性返回一个**动态构造的 no-op 上下文管理器**。这样 `cli.py` 里那句 `with dashboard.live:` 在两种模式下都能正常工作，不需要特判。

#### 4.1.3 源码精读

工厂函数 `create_dashboard` 是整个模式切换的唯一入口：

[genai_bench/ui/dashboard.py:L394-L403](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/dashboard.py#L394-L403) 读取 `ENABLE_UI` 环境变量，默认 `"true"`，按四元组判定真值后二选一返回：

```python
def create_dashboard(metrics_time_unit: str = "s") -> Dashboard:
    enable_ui_str = os.getenv("ENABLE_UI", "true").lower()
    enable_ui = enable_ui_str in ("true", "1", "yes", "on")
    return (
        RichLiveDashboard(metrics_time_unit)
        if enable_ui
        else MinimalDashboard(metrics_time_unit)
    )
```

注意 `.lower()` 让 `TRUE`/`On`/`YES` 等大小写变体都能识别；空串 `""` 不在真值集合里，所以显式设为空也等于关。

类型别名把两者收成一个类型：

[genai_bench/ui/dashboard.py:L391](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/dashboard.py#L391) — `Dashboard = Union[RichLiveDashboard, MinimalDashboard]`。

`MinimalDashboard` 的全部方法都是 no-op，这里看两个关键点。先看构造与「假 live」：

[genai_bench/ui/dashboard.py:L29-L44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/dashboard.py#L29-L44) 用 `type(...)` 动态造了一个只有 no-op `__enter__`/`__exit__` 的类实例，作为 `live`：

```python
self._live = type(
    "MinimalDashboardLive", (), {
        "__enter__": lambda x: None,
        "__exit__": lambda x, *args: None,
    },
)()  # a simple no-op context manager to work with dashboard.live in cli.py
...
@property
def live(self):
    return self._live
```

再看它的几个「更新」方法——全是 `pass`：

[genai_bench/ui/dashboard.py:L46-L79](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/dashboard.py#L46-L79)，包括 `update_metrics_panels`、`update_histogram_panel`、`update_scatter_plot_panel`、`handle_single_request` 等全部一行 `pass`；唯一的例外是 `calculate_time_based_progress` 直接 `return 0.0`，因为它的返回值会参与算术（取 `max`），给个确定的 0 比抛异常更安全。

对比之下，`RichLiveDashboard.__init__` 才会真正分配 rich 资源：

[genai_bench/ui/dashboard.py:L160-L165](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/dashboard.py#L160-L165) 创建 `Live` 上下文，`refresh_per_second=2`（每秒重绘 2 次）、`screen=True`（接管整屏）：

```python
self.live: Live = Live(
    self.layout,
    console=self.console,
    refresh_per_second=2,
    screen=True,
)
```

#### 4.1.4 代码实践

**实践目标**：验证 `create_dashboard` 在不同 `ENABLE_UI` 取值下返回的类型，并直观感受 no-op 的「零成本」。

**操作步骤**：

1. 在项目根目录启动 Python（已 `pip install -e .`）。
2. 依次设置环境变量并观察返回类型（示例代码，非项目原有）：

```python
# 示例代码
import os
from genai_bench.ui.dashboard import create_dashboard, RichLiveDashboard, MinimalDashboard

for val in ["true", "1", "YES", "on", "false", "0", "", "anything"]:
    os.environ["ENABLE_UI"] = val
    d = create_dashboard("s")
    print(f"{val!r:12} -> {type(d).__name__}")
```

3. 再造一个 `MinimalDashboard`，调用它所有方法，确认无任何输出也无异常：

```python
# 示例代码
d = MinimalDashboard("s")
d.update_metrics_panels({"stats": {}}, "s")
d.handle_single_request({"stats": {}}, 10, None)
with d.live:        # 假 live 上下文管理器
    pass
print("all no-op methods returned safely")
```

**需要观察的现象**：

- 前四个真值 → `RichLiveDashboard`；后四个 → `MinimalDashboard`。
- MinimalDashboard 的方法调用无任何终端输出，也不会抛错。

**预期结果**：与 `tests/ui/test_dashboard.py` 的参数化用例一致（`true/1/yes/on` → Rich，其余 → Minimal）。运行结果「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果用户把 `ENABLE_UI` 设成一个拼写错误的值，比如 `ENABLE_UI=tur`，会得到哪种仪表盘？为什么？

> **答案**：`MinimalDashboard`。因为 `"tur"` 不在 `{"true","1","yes","on"}` 集合里，判为假值。这是「白名单」式判定——凡是不认识的值一律视为关闭，避免误开 UI。

**练习 2**：为什么 `MinimalDashboard` 要专门造一个 `live` 属性返回 no-op 上下文管理器，而不是直接 `pass` 不提供 `live`？

> **答案**：因为 `cli.py` 里有 `with dashboard.live:` 和 `LoggingManager(..., dashboard.live, ...)`，这两种人格必须都能提供 `live` 且可作为 `with` 使用。直接不提供会让 `MinimalDashboard` 在这些调用点 `AttributeError`。Null Object 模式的精髓就是「假装自己是真对象」，接口一个都不能少。

---

### 4.2 面板与绘图更新

#### 4.2.1 概念说明

`RichLiveDashboard` 渲染三类内容，分别由三个方法负责：

| 方法 | 渲染内容 | 数据来源 | 触发频率 |
|---|---|---|---|
| `update_metrics_panels` | 输入/输出的「延迟面板」和「吞吐面板」（min/max/avg/p50/p90/p99 数值表） | `live_metrics["stats"]` | 每条成功请求 |
| `update_histogram_panel` | TTFT 与 output_latency 的横向直方图 | `live_metrics["ttft"]`、`live_metrics["output_latency"]` | 每条成功请求 |
| `update_scatter_plot_panel` | 吞吐 vs 延迟的散点图 | `get_ui_scatter_plot_metrics()` | **每个 run 结束一次** |

注意频率差异：前两个是「实时刷」（master 每收一条成功请求就刷），第三个是「定档刷」（一个 `[scenario, concurrency]` 跑完才落一个点）。这是因为散点图每个点代表「一整轮 run 的聚合」，而非单条请求。

所有内容都不是 PNG 图像，而是**字符画**——直方图用 `█` 方块，散点图用 `•` 点阵。这是为了在任意终端、不依赖图形库的情况下都能显示。

#### 4.2.2 核心流程

```text
handle_single_request(live_metrics, total_requests, error_code)
        │
        │ (error_code is not None 时，提前 return，不刷指标/直方图)
        │
        ├─ update_metrics_panels(live_metrics)
        │     ├─ 从 live_metrics["stats"] 取 ttft/input_throughput/output_latency/output_throughput
        │     ├─ create_metric_panel("Input", ...)  -> input 延迟面板 + 吞吐面板
        │     ├─ create_metric_panel("Output", ...) -> output 延迟面板 + 吞吐面板
        │     └─ layout["input_latency" / "input_throughput" / ...].update(panel)
        │
        └─ update_histogram_panel(live_metrics)
              ├─ create_horizontal_colored_bar_chart(live_metrics["ttft"], bin_width=0.01)
              ├─ create_horizontal_colored_bar_chart(live_metrics["output_latency"], ...)
              └─ layout["input_histogram" / "output_histogram"].update(Panel(...))

(另一个时机，每个 run 结束)
update_scatter_plot_panel(get_ui_scatter_plot_metrics(...), metrics_time_unit)
        ├─ 解包出 (ttft, output_latency, input_throughput, output_throughput)
        ├─ 追加进 self.plot_metrics 四条序列
        ├─ create_scatter_plot(input_throughput 序列, ttft 序列, ...)
        ├─ create_scatter_plot(output_throughput 序列, output_latency 序列, ...)
        └─ layout["ttft_vs_input_throughput" / "output_latency_vs_output_throughput"].update(...)
```

#### 4.2.3 源码精读

**指标面板**：`update_metrics_panels` 先做一道防御——`stats` 不存在或为空就直接返回，避免空数据渲染崩；然后兼容 `stats` 是 dict（正常）或其它格式（回退空列表）两种情况：

[genai_bench/ui/dashboard.py:L167-L199](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/dashboard.py#L167-L199) 把四个统计子字典分别交给 `create_metric_panel` 生成「延迟面板 + 吞吐面板」，再写进 layout 对应槽位：

```python
if isinstance(stats, dict):
    input_latency_panel, input_throughput_panel = create_metric_panel(
        "Input", stats.get("ttft", []), stats.get("input_throughput", []),
        metrics_time_unit)
    ...
self.layout["input_throughput"].update(input_throughput_panel)
self.layout["input_latency"].update(input_latency_panel)
...
```

`create_metric_panel` 是渲染细节，负责把统计字典变成 rich 数值表，并在 `ms` 模式下把延迟值乘 1000：

[genai_bench/ui/layout.py:L51-L98](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/layout.py#L51-L98)，延迟表展示 Avg/Min/Max/P50/P90/P99，吞吐表展示 Avg/Min/Max（吞吐永远是 `tokens/sec`，不随时间单位变）。关键片段：

```python
if metrics_time_unit == "ms":
    latency_values = {k: v * 1000 if v is not None else v
                      for k, v in latency_data.items()}
    time_unit_label = "ms"
else:
    latency_values = latency_data
    time_unit_label = "s"
```

这呼应了 u4-l3 的结论——**只换算延迟，吞吐不动**。注意这里的换算是「显示层」临时的，不改 collector 里存的数据。

**横向直方图**：`update_histogram_panel` 调两次 `create_horizontal_colored_bar_chart`，注意它传的 `bin_width=0.01`（秒），即把延迟按 10ms 一档分箱：

[genai_bench/ui/dashboard.py:L201-L228](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/dashboard.py#L201-L228) 把两幅直方图分别包进绿色/蓝色 `Panel`，写进 `input_histogram` / `output_histogram` 槽。

直方图本体的分箱与配色逻辑在 plots.py：

[genai_bench/ui/plots.py:L11-L64](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/plots.py#L11-L64)：用 `np.histogram` 分箱，按每档计数相对最大值的比例缩放出 `█` 方块数量；按占比三段配色——低于最大值 33% 红、33%–66% 黄、高于 66% 绿；`ms` 模式下标签乘 1000。核心配色逻辑：

```python
color = (
    "red"   if value < max_value_hist * 0.33 else
    "yellow" if value < max_value_hist * 0.66 else
    "green"
)
bar = f"{'█' * bar_length}"
```

**散点图**：`update_scatter_plot_panel` 是「每个 run 一个点」的累积器。它先把四元组解包追加进 `self.plot_metrics`（注意是**追加**，所以散点会随 run 增多而增多），再调两次 `create_scatter_plot`：

[genai_bench/ui/dashboard.py:L230-L275](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/dashboard.py#L230-L275)。注意它对空输入有早退（`if not ui_scatter_plot_metrics`），因为 `get_ui_scatter_plot_metrics` 在 `mean_ttft`/`mean_output_latency` 为 `None` 时会返回 `None`。

四元组从哪来？由 collector 的 `get_ui_scatter_plot_metrics` 产出，顺序固定为 `[ttft, output_latency, input_throughput, output_throughput]`：

[genai_bench/metrics/aggregated_metrics_collector.py:L344-L367](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py#L344-L367)，其中两个延迟走 `convert_value` 换算、两个吞吐原样返回——又一次体现「只动延迟」。

散点图本体是把 (x, y) 归一化到一个 `width × height` 字符网格、用 `•` 标点：

[genai_bench/ui/plots.py:L67-L99](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/plots.py#L67-L99) 归一化坐标到网格，`y_pos = height - ...` 是因为终端第 0 行在最上方而我们要让大值在上方：

```python
x_pos = int((x - x_min) / (x_max - x_min) * width)
y_pos = height - int((y - y_min) / (y_max - y_min) * height)
plot[y_pos][x_pos] = "•"
```

> 这里的 `metrics_time_unit` 只影响 y 轴标签的对齐宽度（`label_spacing = 9 if y_unit == "ms" else 7`，见 [plots.py:L74](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/plots.py#L74)），**不换算数值**——数值换算已在 collector 侧完成。

#### 4.2.4 代码实践

**实践目标**：在不跑压测的前提下，直接调用渲染原语，看字符画长什么样。

**操作步骤**：

```python
# 示例代码
from genai_bench.ui.plots import create_horizontal_colored_bar_chart, create_scatter_plot
from genai_bench.ui.layout import create_metric_panel

# 1) 直方图：模拟一批 TTFT（秒）
print(create_horizontal_colored_bar_chart([0.05, 0.08, 0.12, 0.12, 0.15, 0.2],
                                          bin_width=0.05, metrics_time_unit="s"))

# 2) 散点图：吞吐(x) vs TTFT(y)
print(create_scatter_plot([100, 200, 300, 400], [0.5, 1.0, 1.5, 2.0],
                          y_unit="s", x_unit="tokens/sec"))

# 3) 指标面板：模拟一段 stats
lat, thr = create_metric_panel("Input",
                               {"min":0.05,"max":0.2,"mean":0.1,"p50":0.09,"p90":0.18,"p99":0.2},
                               {"min":80,"max":120,"mean":100}, "s")
print(lat)
print(thr)
```

**需要观察的现象**：

- 直方图按 bin 分档，每档一行 `标签 | ███ 数量`，颜色随占比变红/黄/绿。
- 散点图是一个 `•` 点阵，带 y 轴刻度（标 `s` 单位）和 x 轴刻度（标 `tokens/sec`）。
- 指标面板是带边框的数值表，延迟单位随 `metrics_time_unit` 显示 `s` 或 `ms`。

**预期结果**：均能打印出字符画且不报错；具体排版「待本地验证」（受终端宽度影响）。

#### 4.2.5 小练习与答案

**练习 1**：`update_metrics_panels` 里为什么要写 `if isinstance(stats, dict): ... else: ...用空列表`？既然 `_live_metrics_data["stats"]` 一直是 dict，这个 else 分支岂不没用？

> **答案**：这是防御性编程。`LiveMetricsData` 是普通类型注解（[protocol.py:L5](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L5)），运行时不强制；跨进程/未来格式变化可能让 `stats` 不是 dict。else 分支用空列表回退，保证渲染不崩——宁可画空面板，也不要 `KeyError` 把整个 master 进程拖垮。

**练习 2**：散点图为什么用「追加」方式累积 `self.plot_metrics`，而直方图每次都用整条 `live_metrics` 重新画？

> **答案**：因为两者粒度不同。直方图反映「当前 run 内所有请求」的延迟分布，每来一条请求都要重算全分布，所以每次拿整条序列重画。散点图每个点代表「一整轮 run 的聚合结果」，只有 run 结束才新增一个点，所以要累积；而它在每个 scenario 开始时会被 `reset_plot_metrics` 清空（见 [dashboard.py:L334-L358](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/dashboard.py#L334-L358)），所以一张散点图展示的是「同一 scenario 下不同并发档位」的点。

---

### 4.3 进度计算

#### 4.3.1 概念说明

进度条有两条：

- **Total Progress（总进度）**：整个实验有多少个 run（`场景数 × 并发档位数`），跑完一个就推进一格。
- **Current Run Progress（当前 run 进度）**：当前这个 `[scenario, concurrency]` 跑到哪了。

难点在「当前 run 进度」怎么算。一个 run 的结束条件有两个（见 u1-l2 / cli.py 的 `manage_run_time`）：达到 `--max-time-per-run`，或达到 `--max-requests-per-run`。因此进度也应该同时参考「时间」和「请求数」，取**较大值**——这样无论先撞到哪个上限，进度条都能贴近真实。这就是 `handle_single_request` 里 `max(time_based, request_based)` 的由来。

`handle_single_request` 是 master 收到每条请求指标后的**唯一入口**，它把「推进进度条」和「刷新指标/直方图面板」合在一起。这种合并的好处是只在一个回调里集中渲染；代价是——请求失败时（`error_code is not None`）只推进度、不刷指标，避免用残缺数据画出误导性面板。

#### 4.3.2 核心流程

```text
handle_single_request(live_metrics, total_requests, error_code)
   │
   ├─ time_based    = (now - start_time) / run_time, 夹到 [0,1], ×100   # 时间维度
   ├─ request_based = min(total_requests / max_requests_per_run, 1) ×100  # 请求数维度
   ├─ progress_increment = max(time_based, request_based)               # 取较大值
   │
   ├─ update_benchmark_progress_bars(progress_increment)
   │     └─ benchmark_progress.update(task_id, completed=进度)
   │        + update_progress(layout, total, benchmark)  把进度条画进 row1
   │
   ├─ if error_code is not None: return       # 失败请求：到此为止，不刷指标
   │
   ├─ update_metrics_panels(live_metrics, ...)   # 成功请求：刷新数值面板
   └─ update_histogram_panel(live_metrics, ...)  # 成功请求：刷新直方图
```

进度状态的生命周期由三个方法配合：

- `create_benchmark_progress_task(run_name)`：进入一个新 run 时，建一条 total=100 的「当前 run」进度任务。
- `start_run(run_time, start_time, max_requests_per_run)`：记录这一轮的时间起算点和上限，供 `calculate_time_based_progress` 用。
- `update_total_progress_bars(total_runs)`：一个 run 跑完，删掉「当前 run」任务，把「总进度」推进 `100/total_runs`。

#### 4.3.3 源码精读

`handle_single_request` 是本模块最关键的方法，集中体现了「双维度取大 + 失败短路」：

[genai_bench/ui/dashboard.py:L306-L332](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/dashboard.py#L306-L332)：

```python
time_based_progress = self.calculate_time_based_progress()
assert self.max_requests_per_run is not None
request_based_progress = (
    min(total_requests / self.max_requests_per_run, 1) * 100
)
progress_increment = max(time_based_progress, request_based_progress)
self.update_benchmark_progress_bars(progress_increment)

# No need to update metrics panel or histogram panel when the request fails
if error_code is not None:
    return

self.update_metrics_panels(live_metrics, self.metrics_time_unit)
self.update_histogram_panel(live_metrics, self.metrics_time_unit)
```

两个细节：

- `min(... / max_requests_per_run, 1)` 把请求数进度夹到 100%，避免超发时进度条「爆表」。
- 失败短路在 `update_benchmark_progress_bars` **之后**——失败请求依然计入进度（它确实发生了），但不参与指标面板刷新。

时间进度的计算用 `time.monotonic()`（单调时钟，不受系统时间回拨影响）：

[genai_bench/ui/dashboard.py:L301-L304](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/dashboard.py#L301-L304)：

```python
def calculate_time_based_progress(self) -> float:
    assert self.start_time is not None and self.run_time is not None
    time_elapsed = time.monotonic() - self.start_time
    return min(time_elapsed / self.run_time, 1) * 100
```

「当前 run」进度条的创建与「总进度」的推进：

[genai_bench/ui/dashboard.py:L283-L294](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/dashboard.py#L283-L294)，`create_benchmark_progress_task` 给当前 run 建一条 `total=100` 的任务；`update_total_progress_bars` 先 `remove_task` 删掉旧 run 任务，再把总进度 `advance=(1/total_runs)*100`：

```python
def update_total_progress_bars(self, total_runs: int):
    self.benchmark_progress.remove_task(self.benchmark_progress_task_id)
    self.total_progress.update(
        self.total_progress_task_id, advance=(1 / total_runs) * 100
    )
    update_progress(self.layout, self.total_progress, self.benchmark_progress)
```

两条进度条最终被画进 layout 的 `row1`（左 `total_progress`、右 `benchmark_progress`），由 `update_progress` 统一回写：

[genai_bench/ui/layout.py:L116-L126](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/ui/layout.py#L116-L126)。

**调用时机**：在 cli 主循环里，这些方法被精心穿插在双层循环（scenario × concurrency）的各个节点：

- [cli.py:L412](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L412) `with dashboard.live:` 进入整屏渲染。
- [cli.py:L414](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L414) 每个 scenario 开始 `reset_plot_metrics()`（清散点）。
- [cli.py:L425](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L425) 每个并发档开始 `reset_panels()`。
- [cli.py:L430-L432](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L430-L432) `create_benchmark_progress_task(...)` 建当前 run 进度条。
- [cli.py:L446](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L446) `start_run(max_time_per_run, start_time, max_requests_per_run)`。
- [cli.py:L489-L494](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L489-L494) run 结束后 `update_scatter_plot_panel(get_ui_scatter_plot_metrics(...), ...)`。
- [cli.py:L519](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L519) `update_total_progress_bars(total_runs)`。

而 `handle_single_request` 的调用方在 runner：master 每收到一条 worker 上报的请求指标，就 `gevent.spawn` 它（非阻塞，不卡指标处理主链路）：

[genai_bench/distributed/runner.py:L296-L305](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L296-L305)：

```python
if self.dashboard and environment.runner and environment.runner.stats:
    live_metrics = self.metrics_collector.get_live_metrics()
    total_requests = environment.runner.stats.total.num_requests
    error_code = metrics.error_code
    gevent.spawn(
        self.dashboard.handle_single_request,
        live_metrics, total_requests, error_code,
    )
```

注意 `if self.dashboard` 这个判断——在 `MinimalDashboard` 时它依然是真值（对象非 None），所以这段代码**不需要为「关 UI」做特判**，no-op 方法被调用后什么都不做而已。这就是 Null Object 模式在调用侧带来的收益。

#### 4.3.4 代码实践

**实践目标**：用单测里同样的手法，验证 `handle_single_request` 的「失败短路」行为。

**操作步骤**（仿照 [tests/ui/test_dashboard.py:L45-L93](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/ui/test_dashboard.py#L45-L93)）：

```python
# 示例代码
import os
from unittest.mock import MagicMock
from genai_bench.ui.dashboard import create_dashboard, RichLiveDashboard

os.environ["ENABLE_UI"] = "true"
d = create_dashboard("s")
assert isinstance(d, RichLiveDashboard)
# 预置 handle_single_request 依赖的状态
d.benchmark_progress_task_id = 0
d.start_time = 0
d.run_time = 1
d.max_requests_per_run = 5

# 把三个被调方法换成 mock，便于断言是否被调用
d.update_benchmark_progress_bars = MagicMock()
d.update_metrics_panels = MagicMock()
d.update_histogram_panel = MagicMock()

live = {"stats": {"ttft": {"min":0.1,"max":0.1,"mean":0.1,"p50":0.1,"p90":0.1,"p99":0.1}}}

# 成功请求：指标面板应该被刷
d.handle_single_request(live, total_requests=3, error_code=None)
print("no-error -> metrics:", d.update_metrics_panels.call_count)   # 预期 1

# 失败请求：指标面板不应被刷
d.update_metrics_panels.reset_mock()
d.handle_single_request(live, total_requests=4, error_code=500)
print("with-error -> metrics:", d.update_metrics_panels.call_count) # 预期 0
```

**需要观察的现象**：

- 成功请求：`update_metrics_panels` 与 `update_histogram_panel` 各被调用一次。
- 失败请求：两者调用计数为 0；但 `update_benchmark_progress_bars` 两次都被调用（失败也推进度）。

**预期结果**：与上述断言一致；运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么时间进度用 `time.monotonic()` 而不是 `time.time()`？

> **答案**：`time.time()` 返回墙钟时间，可能因 NTP 校时或手动改系统时间而回退或跳跃，导致「进度倒退」或算出负数。`time.monotonic()` 是单调递增时钟，专为测量时间间隔设计，不受系统时间调整影响。`start_run` 里存的也是 `time.monotonic()`（[cli.py:L445](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L445) `start_time = time.monotonic()`），两者必须配套使用。

**练习 2**：如果用户既没设 `--max-time-per-run` 也没设 `--max-requests-per-run`，`calculate_time_based_progress` 会怎样？

> **答案**：会触发 `assert self.start_time is not None and self.run_time is not None`。`run_time` 由 `start_run(max_time_per_run, ...)` 注入，若 `max_time_per_run` 为 0 或 None，除以 `run_time` 还可能除零。实际上 CLI 层会强制这两个上限至少有一个有效（见 u1-l2 关于 run 结束条件的说明），仪表盘这里用 `assert` 做最后一道防线，假设上游已保证不变式。

**练习 3**：进度为何要 `max(时间进度, 请求数进度)` 而不是取平均？

> **答案**：因为一个 run 在「时间用满」或「请求数用满」**任一**条件满足时就结束。取较大值能让进度条始终贴近「离结束更近的那条线」——如果服务很快、请求数先到上限，请求数进度会先到 100%，取大值能正确反映「马上结束」；反之若服务很慢、时间先到，时间进度主导。取平均会让进度条在两种极端下都偏慢、误导用户。

## 5. 综合实践

**任务**：用环境变量切换两种仪表盘，对比它们在一次（哪怕失败的）基准里的行为差异，并用一句话总结 no-op 设计的好处。

**操作步骤**：

1. 准备一个 OpenAI 兼容端点（无可用服务时，用一个必然返回错误的占位地址 `--base-url http://127.0.0.1:1 --model dummy --model-tokenizer hf-internal-testing/tiny-random-llama` 也可，目的是观察 UI 行为而非拿真实指标）。收窄规模以快速观察：单场景单并发，例如 `--traffic-scenario "D(16,16)" --num-concurrency 1 --max-time-per-run 1`。

2. **第一次：开 UI（默认）**

   ```bash
   export ENABLE_UI=true
   genai-bench benchmark --api-backend openai --task text-to-text \
     --base-url http://127.0.0.1:1 --model dummy \
     --model-tokenizer hf-internal-testing/tiny-random-llama \
     --traffic-scenario "D(16,16)" --num-concurrency 1 --max-time-per-run 1
   ```

   观察：终端被整屏接管，能看到 Total / Current Run 两条进度条、输入/输出延迟与吞吐面板、字符直方图、日志面板（最后 10 行）。

3. **第二次：关 UI**

   ```bash
   export ENABLE_UI=false
   # 同样的命令
   genai-bench benchmark ...（同上）
   ```

   观察：终端**不被接管**，只有普通逐行日志输出；没有任何面板、进度条、直方图。但实验照常进行，run JSON 照常落盘到 `experiments/` 目录。

4. **对比要点**（填空）：
   - 两次都能在 `experiments/` 产出 run JSON 与 `_summary.xlsx` —— 说明 **no-op 仪表盘不影响实验产出**。
   - 关 UI 后日志不再被 Live 屏幕覆盖，可直接重定向到文件，**便于 CI 收集**。
   - runner 里那段 `if self.dashboard:` 的代码两次都执行了，只是第二次调的是 `pass` —— 说明 **调用方代码零分支**。

**预期结果**：开 UI 看到实时画面、关 UI 只剩日志，两次产物一致。具体画面「待本地验证」（取决于是否有可用端点与终端是否为 TTY）。

> 进阶可选：再设 `--metrics-refresh-interval 1`（见 [option_groups.py:L860-L869](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py#L860-L869)），观察面板刷新频率从「每条请求」变成「每秒一次」——它控制的是 collector 侧 `_update_live_metrics` 的节流，仪表盘只是被动消费节流后的快照。

## 6. 本讲小结

- 仪表盘有「真渲染」`RichLiveDashboard` 和「全 no-op」`MinimalDashboard` 两种人格，由 `create_dashboard` 读 `ENABLE_UI`（默认开、白名单判真）二选一，两者实现同一接口、类型统一为 `Dashboard`。
- Null Object 模式让调用方（runner、cli）**零分支**：`if self.dashboard:` 对 MinimalDashboard 同样为真，调用的只是 `pass`；连 `with dashboard.live:` 都靠一个动态构造的 no-op 上下文管理器兼容。
- `handle_single_request` 是 master 收到每条请求指标后的唯一渲染入口：用「时间进度」与「请求数进度」取较大值推进当前 run 进度条，失败请求（`error_code is not None`）只推进度、不刷指标面板。
- 三类画面各有节奏：指标面板 + 横向直方图「每条成功请求」刷一次（数据来自 `get_live_metrics()` 的 `stats`），散点图「每个 run」落一个点（数据来自 `get_ui_scatter_plot_metrics()`，按 scenario 累积、reset 清空）。
- 所有「图」都是字符画（直方图 `█`、散点图 `•`），不依赖图形库；延迟在显示层按 `metrics_time_unit` 临时换算，吞吐永不换算——与 u4-l3 的「只动延迟」一致。
- 渲染发生在 **master 进程**；UI 是「锦上添花」，关掉它不影响 JSON/Excel/PNG 等实验产物，这正是 no-op 设计的工程价值。

## 7. 下一步学习建议

- **u7-l3 日志系统**：本讲多次提到 layout 的 `logs` 块和「Live 上下文里日志会被屏幕覆盖」。下一讲讲 `RollingRichPanelHandler` 如何把最后 10 行日志写进 `logs` 块、`DelayedRichHandler` 为何要把日志缓冲到 Live 退出后再 flush——它会解释清楚 `dashboard.live` 与日志之间的那层「延迟 flush」关系（[logging.py:L15-L60](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L15-L60)）。
- **回看 u8-l1 主流程编排**：本讲的 `with dashboard.live:` 及其内部一连串 `dashboard.*` 调用，其实嵌在 cli.py 的双层实验循环里。学完 u7-l3 后，建议读 [u8-l1](u8-l1-benchmark-main-flow-capstone.md)，把认证→采样→分布式→双层循环→报告→上传这条主线和本讲的 UI 编排串成一张完整时序图。
- **动手扩展（可选）**：若想加一种新画面（如错误率柱状图），可参考 plots.py 的 `create_horizontal_colored_bar_chart` 写一个返回 `rich.text.Text` 的函数，再在 `RichLiveDashboard` 加一个 `update_xxx_panel` 方法、在 layout 里加一个具名块；同时记得在 `MinimalDashboard` 补一个同名 no-op 方法，维持接口对称。

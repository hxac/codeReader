# 指标数据模型与时间单位转换

## 1. 本讲目标

学完本讲后，你应该能够：

- 画出 genai-bench 指标数据模型的**分层结构**：`RequestLevelMetrics`（一条请求）→ `StatField`（单个字段的一组统计量）→ `MetricStats`（一组 `StatField`）→ `AggregatedMetrics`（一整轮 run 的摘要）。
- 读懂这套模型如何**序列化与校验**：自定义的 `model_dump` / `model_validate` 如何让 `stats` 在 Pydantic 对象与纯 dict 之间无损往返，以及 `RequestLevelMetrics` 的 `model_validator` 如何保证「成功请求的指标不许缺」。
- 说清楚 `TimeUnitConverter` 如何只对**延迟字段**做 `s↔ms` 换算，而 token 数、吞吐等保持不动。
- 解释 `metrics_time_unit` 这一个字符串如何从 CLI 一路**贯穿「保存 / 实时 UI / 分析报告」**三个出口。
- 自己动手用 `convert_metrics_dict` 把一份聚合指标从秒转成毫秒，并核对哪些数变了、哪些数没变。

## 2. 前置知识

本讲直接承接 [u4-l1 单请求指标计算 RequestMetricsCollector](u4-l1-request-metrics-collector.md) 与 [u4-l2 运行级聚合 AggregatedMetricsCollector](u4-l2-aggregated-metrics-collector.md)。建议你先确认已经掌握：

- **单请求指标 `RequestLevelMetrics`**：一条请求的 TTFT、TPOT、`e2e_latency`、`output_latency`、各类 token 计数与吞吐。它是分层的最底层。
- **一次 run**：由 `[scenario, concurrency]` 唯一界定的一组请求；u4-l2 把成百上千条 `RequestLevelMetrics` 聚合成了一份 `AggregatedMetrics`，本讲就来拆解这份聚合结果的**数据结构本身**。
- **Pydantic v2**：`BaseModel`、`Field`、`model_validator`、`model_dump` / `model_validate`、`model_fields`、`ClassVar`。

补充两个本讲会反复用到的小概念：

- **延迟（latency）字段**：衡量「时间长短」的指标，单位是时间（秒或毫秒），如 TTFT、`e2e_latency`。它们的共同特点是**可以做单位换算**。
- **计数 / 速率字段**：衡量「数量」或「每秒多少」的指标，如 `num_output_tokens`（个）、`output_throughput`（tokens/s）。它们与时间单位无关，**换算时必须原样保留**——这正是本讲核心练习要验证的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [genai_bench/metrics/metrics.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py) | 本讲主角之一。定义四个分层模型：`RequestLevelMetrics`、`StatField`、`MetricStats`、`AggregatedMetrics`。 |
| [genai_bench/time_units.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/time_units.py) | 本讲主角之二。`TimeUnitConverter` 工具类：在延迟字段上做 `s↔ms` 换算、改写标签、校验单位。 |
| [genai_bench/metrics/aggregated_metrics_collector.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py) | 在 `save` 落盘、`get_ui_scatter_plot_metrics` 喂 UI 时调用 `TimeUnitConverter`。 |
| [genai_bench/protocol.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py) | `ExperimentMetadata.metrics_time_unit`：整个时间单位的「单一事实来源」字符串。 |
| [genai_bench/analysis/excel_report.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/excel_report.py) | 报告出口之一：读实验时把 `source_time_unit` 换算成目标单位，并改写表头。 |
| [tests/test_time_units.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/test_time_units.py) | `TimeUnitConverter` 的单元测试，是本讲实践的「标准答案」。 |

一句话数据流：内部一切延迟都以**秒**计算 → 在「保存 / UI / 报告」三个出口，由 `TimeUnitConverter` 按需换算成 `ms`（或保持 `s`）→ 落盘 JSON / 画到屏幕 / 写进 Excel。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 指标模型分层**（数据结构长什么样）、**4.2 序列化与校验**（怎么安全地存取这坨嵌套结构）、**4.3 时间单位转换**（怎么按需改单位而不改坏数据）。

### 4.1 指标模型分层：四层金字塔

#### 4.1.1 概念说明

u4-l1 算出了一条请求的指标（`RequestLevelMetrics`），u4-l2 把一整轮 run 的所有请求聚合成了一份摘要（`AggregatedMetrics`）。但「聚合」这件事，到底**数据结构上**是怎么搭起来的？这就是本模块要讲清的分层。

genai-bench 用四个 Pydantic 模型搭出一座「四层金字塔」，从底到顶分别是：

| 层 | 模型 | 粒度 | 装的是什么 |
| --- | --- | --- | --- |
| 第 1 层（底） | `RequestLevelMetrics` | 一条请求 | 一条请求的原始指标值（TTFT、token 数……） |
| 第 2 层 | `StatField` | 一个字段的一组统计 | 某个字段在一批请求上的 `min/max/mean/stddev/sum/p25~p99` |
| 第 3 层 | `MetricStats` | 一组 `StatField` | 每个数值字段各配一个 `StatField` |
| 第 4 层（顶） | `AggregatedMetrics` | 一整轮 run | 运行元信息 + `MetricStats` + 运行级吞吐/错误率 |

关键直觉：**第 3 层 `MetricStats` 的结构，完全是第 1 层 `RequestLevelMetrics` 的「统计镜像」**——`RequestLevelMetrics` 有哪些数值字段，`MetricStats` 就有多少个 `StatField`，名字一一对应。前者存「这一条的值」，后者存「这一批的统计」。正是这种镜像关系，让聚合代码（u4-l2）可以拿 `RequestLevelMetrics.model_fields` 当目录，逐字段地往 `MetricStats` 里填 `StatField`。

而第 2 层 `StatField` 是最小积木：它就是「一坨统计量」的容器，本身不关心是 TTFT 还是 token 数。把它复用 12 次（每个数值字段一次），就拼出了第 3 层。

#### 4.1.2 核心流程

分层在代码里的拼装关系（自底向上）：

```
StatField（积木：min/max/mean/stddev/sum/p25/p50/p75/p90/p95/p99）
   ▲
   │ 复用 12 次，每个数值字段一个
   │
MetricStats（ttft=StatField, tpot=StatField, e2e_latency=StatField, …）
   ▲            └─ 字段名集合 == RequestLevelMetrics 的数值字段（排除 error_*）
   │
   │ 作为 stats 字段被包进顶层
   │
AggregatedMetrics（scenario / num_concurrency / run_duration /
                   mean_output_throughput / error_rate / num_requests /
                   stats: MetricStats）
```

注意几个要点：

- **字段一一对应**：`MetricStats` 的 12 个字段名，恰好是 `RequestLevelMetrics` 去掉 `error_code` / `error_message` 后的数值字段。聚合时按字段名 `getattr` 取值、再 `getattr(stats, key)` 写回对应 `StatField`（见 u4-l2）。
- **`AggregatedMetrics` 不只有 `stats`**：它还单独挂着 `run_duration`、各种 `mean_*_throughput`、`error_rate`、`num_requests` 等运行级标量——因为这些是「整轮只有一个数」的指标，不适合塞进逐字段的 `StatField`。
- **为什么分这么多层**：单一职责。`StatField` 只管「一组统计量怎么存」；`MetricStats` 只管「哪些字段需要统计」；`AggregatedMetrics` 只管「一次 run 的全部产出」。改任何一层都不牵连别的层。

#### 4.1.3 源码精读

先看最底层的积木 `StatField`——11 个统计槽位，外加 `__getitem__` / `__setitem__` 让它能像字典一样读写：

[genai_bench/metrics/metrics.py:77-98](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L77-L98)：`StatField` 定义。`min/max/mean/stddev/sum` 与六个百分位 `p25~p99` 全是 `Optional[float]`，默认 `None`（因为某字段可能全被过滤掉而没有值）。`__getitem__`/`__setitem__` 让外部代码既能 `stat.mean` 也能 `stat["mean"]` 访问，方便聚合循环里动态按字符串键赋值。

再看第 3 层 `MetricStats`——12 个 `StatField` 字段，名字与 `RequestLevelMetrics` 的数值字段对齐：

[genai_bench/metrics/metrics.py:101-115](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L101-L115)：`MetricStats` 的字段定义。注意它**没有** `error_code` / `error_message`（错误不做统计），每个字段都用 `default_factory=StatField` 造一个空积木。

最后是顶层 `AggregatedMetrics`——把运行元信息、运行级标量和 `MetricStats` 打包到一起：

[genai_bench/metrics/metrics.py:136-183](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L136-L183)：`AggregatedMetrics` 定义。`stats: MetricStats` 在 L180-L183 作为字段挂载；上面那些 `run_duration`、`mean_output_throughput_tokens_per_s`、`error_rate`、`num_requests` 等是「整轮只有一个数」的标量指标。

而最底层 `RequestLevelMetrics` 的字段集合，回顾：

[genai_bench/metrics/metrics.py:13-39](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L13-L39)：单请求的所有字段。你可以和上面 `MetricStats` 的字段逐个对照——`ttft`、`tpot`、`e2e_latency`、`output_latency`、`output_inference_speed`、`num_input_tokens`、`num_output_tokens`、`num_reasoning_tokens`、`total_tokens`、`input_throughput`、`output_throughput`、`audio_throughput`，正好 12 个一一对应（`error_code` / `error_message` 除外）。

> 小提示：`RequestLevelMetrics` 里还有两个 `ClassVar` 集合 `OUTPUT_METRICS_FIELDS` 和 `AUDIO_METRICS_FIELDS`（[metrics.py:42-52](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L42-L52)），用来标记「哪些字段是输出类 / 音频类」。它们在 u4-l1 的重置逻辑和 u4-l2 的聚合空值兜底里用到，`ClassVar` 表示这是类变量、不是实例字段，不会被 `model_dump` 序列化。

#### 4.1.4 代码实践

**实践目标**：亲手搭一遍四层金字塔，看清嵌套结构。

**操作步骤**（在项目根目录，确保已 `pip install -e .`）：

```python
# 示例代码：搭一遍指标分层
from genai_bench.metrics.metrics import (
    RequestLevelMetrics, StatField, MetricStats, AggregatedMetrics,
)

# 第 1 层：一条请求
req = RequestLevelMetrics(
    ttft=0.5, tpot=0.02, e2e_latency=2.0, output_latency=1.5,
    output_inference_speed=50.0, num_input_tokens=100, num_output_tokens=100,
    num_reasoning_tokens=0, total_tokens=200, input_throughput=200.0,
    output_throughput=66.7, audio_throughput=None,
)

# 第 2 层：单个字段的一组统计
sf = StatField(mean=0.5, p50=0.48, p99=0.9, min=0.3, max=1.0)
print("StatField 字典式访问 mean =", sf["mean"], "| 属性式访问 p99 =", sf.p99)

# 第 3 层：一组 StatField
ms = MetricStats(ttft=sf, e2e_latency=StatField(mean=2.0, p50=1.9, p99=3.5))

# 第 4 层：一整轮 run
agg = AggregatedMetrics(
    scenario="D(100,100)", num_concurrency=8,
    run_duration=10.0, mean_output_throughput_tokens_per_s=800.0,
    error_rate=0.0, num_requests=1000, stats=ms,
)
print(agg.model_dump()["stats"]["ttft"])   # {'min': 0.3, 'max': 1.0, 'mean': 0.5, ...}
```

**需要观察的现象**：

- `sf["mean"]` 与 `sf.mean` 返回同一个值——验证 `StatField` 的双式访问。
- `agg.model_dump()` 顶层能直接看到 `scenario`、`mean_output_throughput_tokens_per_s` 等标量，以及一个嵌套的 `stats`（它的形状下一模块细讲）。

**预期结果**：四层都能正常构造、打印；`stats["ttft"]` 是一个含 `min/max/mean/...` 的 dict。

**待本地验证**：`StatField` 槽位较多，若你只填了 `mean/p50/p99/min/max`，其余键在 `model_dump()` 后会以 `None` 出现。

#### 4.1.5 小练习与答案

**练习 1**：`MetricStats` 为什么没有 `error_code` 这个字段？
**答案**：`error_code` / `error_message` 是「这一条请求出错了吗」的状态标记，不是需要做 min/mean/p99 统计的数值指标。`MetricStats` 只镜像 `RequestLevelMetrics` 的**数值字段**，错误信息另有归处（`AggregatedMetrics.error_codes_frequency` 字典统计错误码出现频率）。

**练习 2**：`AggregatedMetrics` 把 `mean_output_throughput_tokens_per_s` 放成顶层标量，而不是塞进 `stats.output_throughput`，为什么？
**答案**：`stats.output_throughput` 是「逐请求 output_throughput 的统计分布」（u4-l1 已指出单请求吞吐恒等于 1/tpot）；而 `mean_output_throughput_tokens_per_s` 是**整轮的「总输出 token ÷ 时长」**（u4-l2），是一个「整轮只有一个数」的聚合标量，语义和来源都不同，故单独挂在顶层。

---

### 4.2 序列化与校验：让嵌套结构安全往返

#### 4.2.1 概念说明

四层金字塔搭好了，但它要能**存进 JSON 文件、再从 JSON 原样读回来**，才有用——因为压测结果要落盘、要跨进程（worker→master）传递、要喂给分析和绘图子系统。这就涉及两个问题：

1. **序列化形状要对**：`AggregatedMetrics` 里的 `stats` 是 `MetricStats`（嵌套 Pydantic 对象），但下游（`TimeUnitConverter`、实时 UI、Excel 报告）都期望它是一个**纯 dict** `{字段名: {统计键: 值}}`。所以 `AggregatedMetrics` 不能用 Pydantic 默认的 `model_dump`，得自定义。
2. **数据要完整**：一条「成功」的请求，不该有 `None` 的核心指标（否则聚合会出 `NoneType` 错误）。`RequestLevelMetrics` 用一个 `model_validator` 在构造时就守住这条底线。

本模块讲清这两道「安全阀」。

#### 4.2.2 核心流程

**序列化（写出）** 的关键：`AggregatedMetrics.model_dump` 被重写，把 `stats` 从 `MetricStats` 对象替换成 `self.stats.to_dict()` 的纯 dict；而 `to_dict` 又是用 `RequestLevelMetrics.model_fields` 当目录、排除 `error_*` 来生成的——保证 dict 的字段集合稳定且与第 1 层对齐：

```
AggregatedMetrics.model_dump()
   └─ data = super().model_dump()           # 其余字段正常 dump
   └─ data["stats"] = self.stats.to_dict()  # stats 单独换成纯 dict
         └─ 遍历 RequestLevelMetrics.model_fields，排除 {error_code, error_message}
              └─ 每个字段 StatField.model_dump() → {min,max,mean,...}
```

**反序列化（读回）** 是镜像过程：`AggregatedMetrics.model_validate` 被重写，发现 `stats` 是 dict 时，先用 `MetricStats.from_dict` 把它变回 `MetricStats` 对象，再交给父类校验：

```
AggregatedMetrics.model_validate(obj)
   └─ if obj["stats"] is dict:
         obj["stats"] = MetricStats.from_dict(obj["stats"])
              └─ 对每个 (field_name, field_stats)：StatField(**field_stats)
   └─ return super().model_validate(obj)
```

**校验（构造时）**：`RequestLevelMetrics.validate_metrics` 是一个 `mode="before"` 的校验器——在字段类型校验**之前**就跑。规则：如果 `error_code is None`（即成功请求），那么除了 `error_code` / `error_message` / 音频字段外，任何指标字段为 `None` 都直接抛 `ValueError`。这把「成功请求却缺指标」这种数据损坏拦在源头。

#### 4.2.3 源码精读

先看序列化的两个端点。`MetricStats.to_dict` / `from_dict` 是「dict ↔ 对象」的转换核：

[genai_bench/metrics/metrics.py:117-133](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L117-L133)：`to_dict` 用 `RequestLevelMetrics.model_fields` 当目录（注意是借第 1 层的字段表来驱动第 3 层的序列化，体现「镜像」），排除 `error_*`，每个字段调 `StatField.model_dump()`；`from_dict` 是逆操作，对每个 `{field: stats_dict}` 构造 `StatField(**stats)`。

再看顶层如何挂上这两个端点——这就是 `AggregatedMetrics` 的自定义 `model_dump` / `model_validate`：

[genai_bench/metrics/metrics.py:185-194](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L185-L194)：`model_dump` 先 `super().model_dump()` 再把 `stats` 换成 `to_dict()`；`model_validate` 是 `classmethod`，先探测 `stats` 是不是 dict，若是则 `from_dict` 还原，再走父类校验。二者互逆，构成 `对象 → dict → JSON → dict → 对象` 的无损闭环。

最后看校验这道安全阀：

[genai_bench/metrics/metrics.py:54-74](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L54-L74)：`validate_metrics`。注意三点：①`mode="before"`，跑在字段校验前，拿到的 `values` 是原始输入；②开头 `if not isinstance(values, dict): return values` 是保护——当输入已经是模型实例或其它类型时直接放行，不误伤；③豁免清单是 `{"error_code", "error_message"} | AUDIO_METRICS_FIELDS`（音频吞吐只对 TTS 任务有意义，故豁免）。这意味着：从落盘 JSON 重新构造 `RequestLevelMetrics` 时，这个校验会**再跑一次**，能自动揪出被改坏的明细数据。

> 为什么不在「默认 `model_dump` 就能产出嵌套 dict」的情况下还要重写？因为默认 dump 出的 `stats` 形状依赖 Pydantic 内部实现，且字段集合不受控；而 `to_dict` 用 `RequestLevelMetrics.model_fields` 显式驱动，**保证字段集合永远是那 12 个数值字段、顺序与第 1 层一致**。这正是 `convert_metrics_dict`（下一模块）能放心按固定键名去遍历的前提。

#### 4.2.4 代码实践

**实践目标**：验证「写出 → 读回」无损往返，以及校验器对缺字段的拦截。

**操作步骤**：

```python
# 示例代码：序列化往返 + 校验拦截
import json
from genai_bench.metrics.metrics import AggregatedMetrics, RequestLevelMetrics, MetricStats, StatField

agg = AggregatedMetrics(
    scenario="D(100,100)", num_concurrency=8, run_duration=10.0,
    num_requests=5, stats=MetricStats(ttft=StatField(mean=0.5, p99=0.9)),
)

# 1) 往返：对象 -> dict -> JSON -> dict -> 对象
d = agg.model_dump()
print("stats 形状:", list(d["stats"]["ttft"].keys()))   # 纯 dict
roundtrip = AggregatedMetrics.model_validate(json.loads(json.dumps(d)))
print("scenario 还原:", roundtrip.scenario, "| ttft.mean 还原:", roundtrip.stats.ttft.mean)

# 2) 校验器拦截：成功请求却把 ttft 留成 None
try:
    RequestLevelMetrics(ttft=None, e2e_latency=1.0, error_code=None)
except Exception as e:
    print("拦截成功:", type(e).__name__)
```

**需要观察的现象**：

- `d["stats"]["ttft"]` 是一个**扁平 dict**（`min/max/mean/...` 键），而不是嵌套的 Pydantic 对象——说明自定义 `model_dump` 生效。
- 往返后 `roundtrip.stats.ttft.mean == 0.5`，且 `roundtrip.stats` 是 `MetricStats` 实例（`isinstance` 为真）——说明 `model_validate` 还原成功。
- 第 2 步会抛异常——说明校验器拦住了「成功请求缺指标」。

**预期结果**：往返无损；构造 `ttft=None` 的成功请求会触发 `ValidationError` / `ValueError`。

**待本地验证**：第 2 步抛出的具体异常类型（Pydantic v2 会把 `ValueError` 包成 `ValidationError`）。

#### 4.2.5 小练习与答案

**练习 1**：`to_dict` 为什么用 `RequestLevelMetrics.model_fields` 而不是 `MetricStats.model_fields` 当目录？
**答案**：用第 1 层的字段表驱动，能在**一处**定义「哪些指标字段需要统计」，并保证第 3 层的序列化键集合与第 1 层严格对齐（且天然排除 `error_*`）。这也让「单请求有哪些指标」与「聚合统计有哪些条目」始终同步，不会一边加了字段、另一边漏掉。

**练习 2**：把 `RequestLevelMetrics` 的 `validate_metrics` 改成 `mode="after"` 会有什么不同？
**答案**：`mode="before"` 在字段类型校验**之前**拿到原始 dict，适合做「整体一致性」检查（这里检查字段是否齐全）。若改成 `mode="after"`，校验器拿到的是已实例化的对象，逻辑要改成访问属性而非字典键；更重要的是，缺值字段会被先填上默认 `None` 再校验，语义等价但写法不同。项目选 `before` 是因为输入通常是 dict（来自采样或 JSON），直接查字典最直接。

---

### 4.3 时间单位转换：只动延迟，不动 token

#### 4.3.1 概念说明

到这里有个关键事实：**genai-bench 内部所有延迟都用「秒」计算**——TTFT、TPOT、`e2e_latency`、`output_latency` 都是秒。但人看报告时常常更习惯毫秒（TTFT 0.5s 写成 500ms 更直观）。如果每个出口（保存、UI、报告）都各自手写 `× 1000`，很容易漏一个、错一个。

`TimeUnitConverter` 就是为了统一这件事而生的工具类。它的核心设计只有一句话：

> **只有延迟字段才需要换算；token 数、吞吐、错误率等一律不动。**

为什么 token 数和吞吐不能跟着换算？因为：

- `num_output_tokens` 是「个数」，单位是「个」，跟时间无关——100 个 token 在秒和毫秒制下都是 100。
- `output_throughput` 是「tokens/s」，是「每秒多少个」；它和「TTFT 多少秒」是**不同的物理量**。把 TTFT 从秒改成毫秒，是换时间单位；但 throughput 的分母「秒」是定义的一部分，不能因为延迟显示成毫秒就把它也改了（否则数值会错乱 1000 倍）。

所以 `TimeUnitConverter` 维护了一个**白名单** `LATENCY_FIELDS = {ttft, tpot, e2e_latency, output_latency}`，换算时只碰这四个字段（以及它们在 `stats` 下的 11 个统计键），其余字段原样保留。

而这一切的「开关」，是一个贯穿全流程的字符串 `metrics_time_unit`（取值 `"s"` 或 `"ms"`）。

#### 4.3.2 核心流程

换算的数学很简单：

- `s → ms`：\( \text{value}_{ms} = \text{value}_{s} \times 1000 \)
- `ms → s`：\( \text{value}_{s} = \text{value}_{ms} / 1000 \)
- 同单位：原值返回；`None`：原样返回 `None`。

`convert_metrics_dict` 处理一份「聚合指标 dict」时，分两段扫描：

```
convert_metrics_dict(metrics_dict, to_unit, from_unit="s"):
  ① 顶层直接字段：遍历 LATENCY_FIELDS，在 dict 里找到就 convert_value
  ② stats 嵌套字段：对 stats 下每个 LATENCY_FIELDS 字段，
     遍历它的 STATS_KEYS（min/max/.../p99）逐个 convert_value
  ③ 其余字段（token 数、throughput、error_* 等）：不动
```

而 `metrics_time_unit` 这个字符串，从 CLI 一路贯穿三个出口：

```
CLI 选项 --metrics-time-unit
   │  (经 validate_unit 归一化成 "s"/"ms")
   ▼
ExperimentMetadata.metrics_time_unit   ← 单一事实来源（默认 "s"）
   │
   ├──> 出口① 保存 save(file_path, metrics_time_unit)
   │        convert_metrics_dict / convert_metrics_list 换算后写 JSON，
   │        并在 JSON 里记 "_time_unit" 字段做标记
   │
   ├──> 出口② 实时 UI create_dashboard(metrics_time_unit)
   │        get_ui_scatter_plot_metrics 用 convert_value 换算散点坐标，
   │        直方图/面板按单位显示
   │
   └──> 出口③ 分析报告 excel/plot
            create_workbook 比较 source_time_unit(来自元数据) 与目标单位，
            不同则换算；get_unit_label 把表头 "(s)" 改成 "(ms)"
```

关键点：**保存时**内部是秒，按 `metrics_time_unit` 换算后落盘（JSON 里带 `_time_unit` 标记）；**报告时**先读出元数据里的「源单位」`source_time_unit`，再和用户当前想要的单位比较，不同才换算——所以你**不必重跑压测**，就能对同一份旧实验用不同单位重出报告。

#### 4.3.3 源码精读

先看换算的「宪法」——哪四个字段算延迟、哪 11 个键算统计：

[genai_bench/time_units.py:11-26](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/time_units.py#L11-L26)：`LATENCY_FIELDS = {"ttft", "tpot", "e2e_latency", "output_latency"}` 与 `STATS_KEYS`（11 个统计键）。注意 `output_inference_speed`、`*_throughput`、`num_*_tokens` 都**不在**白名单——这就是「token 数和吞吐不被换算」的代码依据。

再看最底层的单值换算 `convert_value`：

[genai_bench/time_units.py:28-54](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/time_units.py#L28-L54)：`None` 原样返回；同单位原值返回；`s→ms` 乘 1000，`ms→s` 除 1000；其它组合抛 `ValueError`。这是所有上层换算的原子操作。

核心的 `convert_metrics_dict`——按上面「两段扫描」处理一份聚合 dict：

[genai_bench/time_units.py:56-97](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/time_units.py#L56-L97)：① L76-L80 扫顶层 `LATENCY_FIELDS`；② L83-L95 扫 `stats` 嵌套（先 `.copy()` 避免改坏原 dict）；③ 其余字段不进循环，天然不动。注意 L70-L71：`to_unit == from_unit` 时**直接返回原 dict**（连拷贝都不做），所以默认 `"s"` 时是零开销。

`convert_metrics_list` 只是对逐请求明细列表套一层 `convert_metrics_dict`：

[genai_bench/time_units.py:99-116](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/time_units.py#L99-L116)：列表推导，对每个 dict 调 `convert_metrics_dict`。

现在看 `metrics_time_unit` 的「单一事实来源」——它在实验元数据里：

[genai_bench/protocol.py:273-276](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L273-L276)：`ExperimentMetadata.metrics_time_unit`，默认 `"s"`，描述写着「用于延迟指标的显示与导出」。

**出口① 保存**——在 `AggregatedMetricsCollector.save` 里调用换算：

[genai_bench/metrics/aggregated_metrics_collector.py:317-338](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py#L317-L338)：`save` 用 `convert_metrics_dict` 换算聚合结果、`convert_metrics_list` 换算逐请求明细，再 `json.dump` 写出三段式结构（`aggregated_metrics` / `individual_request_metrics` / `_time_unit`）。`_time_unit` 这个标记键让下游能知道「这份 JSON 现在是什么单位」。

**出口② UI**——散点图坐标的换算：

[genai_bench/metrics/aggregated_metrics_collector.py:344-366](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py#L344-L366)：`get_ui_scatter_plot_metrics` 只把 `mean_ttft`、`mean_output_latency` 两个**延迟**用 `convert_value` 换算，而 `mean_input_throughput` / `mean_output_throughput` 两个吞吐**原样返回**——又一次体现「只动延迟，不动吞吐」。

**出口③ 报告**——Excel 报告比较「源单位」与「目标单位」：

[genai_bench/analysis/excel_report.py:86-124](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/excel_report.py#L86-L124)：`create_workbook` 先取 `source_time_unit = experiment_metadata.metrics_time_unit`（L94），若与目标 `metrics_time_unit` 不同，就对每个 run 的 `aggregated_metrics` 调 `convert_metrics_dict`、对 `individual_request_metrics` 调 `convert_metrics_list`（L109-L120）。注意调用形参顺序是 `(metrics_dict, metrics_time_unit, source_time_unit)`——目标单位在前、源单位在后。

最后是两个辅助方法，报告里用来改表头和判别字段类型：

[genai_bench/time_units.py:118-136](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/time_units.py#L118-L136)：`get_unit_label` 用正则把标签里的 `(s)` / `(seconds)` 替换成 `(ms)`（或反向），所以 `"End-to-End Latency per Request (s)"` 会变成 `"...(ms)"`，而 `"TTFT"`（没有括号单位）原样不动。

[genai_bench/time_units.py:160-163](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/time_units.py#L160-L163)：`is_latency_field` 用子串匹配判断一个字段名是否属于延迟类（如 `"stats.ttft.mean"` 含 `ttft` 即判定为延迟），绘图时据此决定要不要换算该字段。注意它是**子串包含**而非精确匹配，所以能处理 `stats.ttft.mean` 这种带路径的字段串。

> 单位校验 `validate_unit`（[time_units.py:138-158](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/time_units.py#L138-L158)）会把 `sec/second/seconds` 归一成 `"s"`、`millisecond/milliseconds` 归一成 `"ms"`，不支持的抛错。CLI 选项 `--metrics-time-unit` 就靠它兜底。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：用 `convert_metrics_dict` 把一份聚合指标从秒转成毫秒，验证「延迟 × 1000，而 token 数和吞吐不变」。这正是规格里要求的核心练习。

**操作步骤**（在项目根目录，已 `pip install -e .`）：

```python
# 示例代码：秒 -> 毫秒换算，验证只有延迟变了
from genai_bench.time_units import TimeUnitConverter

# 模拟一份「秒制」的聚合指标 dict（形状对齐 AggregatedMetrics.model_dump 的产出）
metrics_s = {
    # 顶层：4 个延迟字段 + 若干非延迟字段
    "ttft": 0.5,                # 延迟，应被换算
    "e2e_latency": 2.0,         # 延迟，应被换算
    "tpot": 0.02,               # 延迟，应被换算
    "output_latency": 1.5,      # 延迟，应被换算
    "run_duration": 10.0,       # 非延迟（不是 LATENCY_FIELDS），不动
    "mean_output_throughput_tokens_per_s": 800.0,   # 吞吐，不动
    "num_requests": 1000,       # 计数，不动
    "error_rate": 0.0,          # 比率，不动
    "stats": {
        "ttft": {"mean": 0.5, "p50": 0.48, "p99": 0.9},          # 延迟统计，换算
        "e2e_latency": {"mean": 2.0, "p99": 3.5},                # 延迟统计，换算
        "num_output_tokens": {"mean": 100.0, "p99": 200.0},      # token 计数统计，不动
        "output_throughput": {"mean": 66.7, "p99": 90.0},        # 吞吐统计，不动
    },
}

metrics_ms = TimeUnitConverter.convert_metrics_dict(metrics_s, to_unit="ms", from_unit="s")

# 断言：延迟 × 1000
assert metrics_ms["ttft"] == 500.0
assert metrics_ms["e2e_latency"] == 2000.0
assert metrics_ms["tpot"] == 20.0
assert metrics_ms["output_latency"] == 1500.0
assert metrics_ms["stats"]["ttft"]["p99"] == 900.0
assert metrics_ms["stats"]["e2e_latency"]["mean"] == 2000.0

# 断言：token 数、吞吐、计数、比率全部不变
assert metrics_ms["num_requests"] == 1000
assert metrics_ms["mean_output_throughput_tokens_per_s"] == 800.0
assert metrics_ms["run_duration"] == 10.0
assert metrics_ms["error_rate"] == 0.0
assert metrics_ms["stats"]["num_output_tokens"]["mean"] == 100.0
assert metrics_ms["stats"]["output_throughput"]["mean"] == 66.7

print("✅ 延迟字段 ×1000，token 数 / 吞吐 / 计数 / 比率全部不变")

# 额外：原 dict 不应被改坏（convert_metrics_dict 内部做了 copy）
assert metrics_s["ttft"] == 0.5
print("✅ 原 dict 未被原地修改")

# 额外：同单位是零开销短路，返回的就是原对象
assert TimeUnitConverter.convert_metrics_dict(metrics_s, "s", "s") is metrics_s
```

**需要观察的现象**：

- 四个延迟字段（含它们 `stats` 下的 `mean/p50/p99` 等）全部放大 1000 倍。
- `num_requests`、`mean_output_throughput_tokens_per_s`、`run_duration`、`error_rate`、`stats.num_output_tokens.*`、`stats.output_throughput.*` **纹丝不动**。
- 原 `metrics_s` 的值没被改坏（说明换算不污染输入）。
- `to_unit == from_unit` 时返回的是同一个对象（短路）。

**预期结果**：所有断言通过，打印两个 ✅。这组断言与 `tests/test_time_units.py::test_metrics_dict_conversion`（[test_time_units.py:59-96](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/test_time_units.py#L59-L96)）的思路完全一致，你可以对照该测试理解「标准答案」。

**待本地验证**：若你的环境未安装项目，可只把 `time_units.py` 的逻辑（纯 Python，无第三方依赖）抄出来本地运行；断言数值如上。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `output_throughput`（tokens/s）不能跟着 `ttft` 一起从 `s` 换算到 `ms`？
**答案**：`ttft` 的单位是「时间」，`output_throughput` 的单位是「每秒 token 数」——是两种不同的物理量。把 TTFT 从秒改成毫秒是改**时间单位**；而 throughput 分母里的「秒」是它定义的一部分，如果也乘 1000，数值就变成「每毫秒 token 数」，语义全错。`LATENCY_FIELDS` 白名单只含 4 个时间量，正是为了把 throughput 等速率量挡在外面。

**练习 2**：`convert_metrics_dict` 在 `to_unit == from_unit` 时直接 `return metrics_dict`（不拷贝），这会不会有隐患？
**答案**：在这个项目里是安全的：调用方（如 `save`）拿到结果后只读、不就地改它。它的好处是默认 `"s"` 场景下零分配、零开销。但若你在自己的代码里对返回值就地修改，且 `from_unit==to_unit`，就会污染原 dict——所以下游约定是「只读使用」。当你确实要改时，应自己先 `.copy()`。

**练习 3**：`is_latency_field("stats.output_latency.mean")` 返回什么？为什么这种「子串包含」判别在这里是合理的？
**答案**：返回 `True`，因为字符串里含 `output_latency`。绘图子系统里字段常以路径串 `stats.字段.统计键` 形式出现，精确匹配会很麻烦；而项目里延迟字段名（`ttft/tpot/e2e_latency/output_latency`）足够独特，不会作为子串误出现在 token/throughput 字段名里，所以子串包含既简单又安全。

---

## 5. 综合实践

把三个模块串起来：**搭一份完整的「秒制」聚合指标 → 落盘成带单位的 JSON → 读回 → 用不同单位重出**。

**任务**：模拟「一次 run 结束后 save，之后用毫秒重出报告」的完整链路。

```python
# 示例代码：综合实践
import json, copy
from genai_bench.metrics.metrics import (
    AggregatedMetrics, MetricStats, StatField, RequestLevelMetrics,
)
from genai_bench.time_units import TimeUnitConverter

# ① 构造一份秒制的聚合结果（形状 = AggregatedMetrics.model_dump() 的产出）
agg = AggregatedMetrics(
    scenario="D(100,100)", num_concurrency=8,
    run_duration=10.0, mean_output_throughput_tokens_per_s=800.0,
    requests_per_second=100.0, error_rate=0.0,
    num_requests=1000, num_completed_requests=1000, num_error_requests=0,
    stats=MetricStats(
        ttft=StatField(mean=0.5, p50=0.48, p99=0.9, min=0.3, max=1.0),
        e2e_latency=StatField(mean=2.0, p50=1.9, p99=3.5),
        num_output_tokens=StatField(mean=100.0, p99=200.0),
        output_throughput=StatField(mean=66.7),
    ),
)
agg_dict_s = agg.model_dump()                       # 注意 stats 已是纯 dict

# ② 模拟 save：按 metrics_time_unit="ms" 换算后写出，带 _time_unit 标记
to_save = {
    "aggregated_metrics": TimeUnitConverter.convert_metrics_dict(agg_dict_s, "ms", "s"),
    "individual_request_metrics": [],
    "_time_unit": "ms",
}
print("落盘 ttft.mean =", to_save["aggregated_metrics"]["stats"]["ttft"]["mean"])  # 500.0

# ③ 模拟报告侧：读出元数据的源单位是 "ms"，但用户这次想用 "s" 出报告
source_unit = to_save["_time_unit"]                 # "ms"
target_unit = "s"
if source_unit != target_unit:
    back_to_s = TimeUnitConverter.convert_metrics_dict(
        to_save["aggregated_metrics"], target_unit, source_unit
    )
    # 还原成 AggregatedMetrics 对象
    restored = AggregatedMetrics.model_validate(back_to_s)
    print("还原 ttft.mean =", restored.stats.ttft.mean)        # 0.5（回到秒）
    print("还原 num_output_tokens.mean =", restored.stats.num_output_tokens.mean)  # 100.0（始终没变）
```

**需要观察的现象与预期结果**：

- 落盘时 `ttft.mean` 从 `0.5` 变成 `500.0`（秒→毫秒）。
- 报告侧再从毫秒换回秒，`restored.stats.ttft.mean` 回到 `0.5`；而 `num_output_tokens.mean` 在两次换算中**始终是 100.0**——证明 token 数从未被动过。
- `restored` 是合法的 `AggregatedMetrics` 对象（`model_validate` 成功，说明 4.2 的往返闭环成立）。

**待本地验证**：综合实践把 4.1 的分层构造、4.2 的序列化往返、4.3 的单位换算都跑了一遍；若环境未安装项目，可分别用前三个模块的独立脚本验证各段。

---

## 6. 本讲小结

- genai-bench 的指标数据是**四层金字塔**：`RequestLevelMetrics`（一条请求）→ `StatField`（一个字段的一组统计）→ `MetricStats`（一组 `StatField`，字段名镜像第 1 层）→ `AggregatedMetrics`（一整轮 run 的摘要，含运行级标量）。
- `MetricStats` 与 `RequestLevelMetrics` **字段一一对应**（排除 `error_*`），所以序列化用第 1 层的 `model_fields` 当目录来驱动第 3 层，保证形状稳定。
- `AggregatedMetrics` 自定义 `model_dump` / `model_validate`，借 `MetricStats.to_dict` / `from_dict` 让 `stats` 在**对象 ↔ 纯 dict** 之间无损往返，满足落盘、跨进程、下游（`TimeUnitConverter` / UI / 报告）对纯 dict 的期望。
- `RequestLevelMetrics.validate_metrics`（`mode="before"`）是数据完整性的安全阀：成功请求（`error_code is None`）的核心指标不许为 `None`，从 JSON 重建时也会再校验一次。
- 内部一切延迟都以**秒**计算；`TimeUnitConverter` 用白名单 `LATENCY_FIELDS={ttft,tpot,e2e_latency,output_latency}` 只换算这 4 个时间量，token 数 / 吞吐 / 计数 / 比率**原样保留**。
- `metrics_time_unit` 这一个字符串从 CLI 经 `ExperimentMetadata` **贯穿保存（`save` 带 `_time_unit` 标记）/ 实时 UI（散点坐标换算）/ 分析报告（比较 `source_time_unit` 后换算 + 改表头）**三个出口，且报告侧无需重跑压测即可换单位重出。

## 7. 下一步学习建议

- 本讲把「指标数据怎么存、怎么换算单位」讲透了。接下来指标子系统的收尾是看这些数据如何被**消费**：进入 [u6-l1 实验结果加载 experiment_loader](u6-l1-experiment-loader.md)，看 `load_one_experiment` 如何把本讲落盘的 run JSON 读回成 `scenario × concurrency` 结构，喂给后续 Excel / 绘图报告。
- 若想看时间单位在**绘图**侧的完整应用，可跳读 [u6-l3 绘图配置系统 plot_config](u6-l3-plot-config-system.md) 与 [u6-l4 灵活绘图与 plot 命令](u6-l4-flexible-plot-and-plot-command.md)，那里大量用到 `is_latency_field` / `convert_value` / `get_unit_label`。
- 若对实时 UI 如何按单位显示延迟感兴趣，可预习 [u7-l2 实时仪表盘 Dashboard](u7-l2-live-dashboard.md)，看 `create_dashboard(metrics_time_unit)` 如何把本讲的换算接到屏幕上。
- 想巩固本讲的换算逻辑，可直接阅读并运行单元测试 [tests/test_time_units.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/test_time_units.py)，它覆盖了 `convert_value` / `convert_metrics_dict` / `convert_metrics_list` / `get_unit_label` / `validate_unit` / `is_latency_field` 的全部行为。

# 运行级聚合 AggregatedMetricsCollector

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「一次 run」在 genai-bench 里到底指什么，以及它对应的数据范围。
- 读懂 `aggregate_metrics_data` 如何把成百上千条单请求指标压成一份统计摘要（百分位、均值、吞吐）。
- 解释 warmup/cooldown 过滤「按请求数、不按时间」的工作方式，以及它为何能剔掉冷启动抖动。
- 理解 `filter_metrics` 对极短输出导致的「假高 TPOT」的剔除逻辑，以及 `save` 写出的 JSON 结构。
- 自己构造一批 `RequestLevelMetrics` 喂给聚合器，并核对 `p50/p99` 与 `mean_output_throughput`。

## 2. 前置知识

本讲直接承接 [u4-l1 单请求指标计算 RequestMetricsCollector](u4-l1-request-metrics-collector.md)，建议先确认你已经掌握：

- **单请求指标 `RequestLevelMetrics`**：一条请求的 TTFT、TPOT、`e2e_latency`、`output_latency`、各类 token 计数与吞吐。它是本讲的「输入原料」。
- **时间戳三件套**：`start_time` / `time_at_first_token` / `end_time`，所有延迟指标都由它们换算而来。
- **非 chat 任务的「重置为 0」**：embeddings/rerank/image/tts 任务会把输出类指标置 0（而非 `None`），这个细节在本讲会再次出现——0 是「不适用」的哨兵。
- **Pydantic v2 基础**：`model_fields`、`Field`、`model_validator`、`model_dump` / `model_validate`。

补充两个本讲会用到的统计学概念：

- **百分位（percentile）**：把一组数从小到大排好，第 \(k\) 百分位表示「有 \(k\%\) 的值不超过它」。p50 就是中位数，p99 反映尾部（最慢的那些请求）。
- **标准差（stddev）**：衡量一组数的离散程度，越大说明请求之间快慢差异越悬殊。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [genai_bench/metrics/aggregated_metrics_collector.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py) | 本讲主角。收集单请求指标、做 warmup/cooldown 过滤、计算统计聚合、写出 JSON。 |
| [genai_bench/metrics/metrics.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py) | 定义指标数据模型：`RequestLevelMetrics`（输入）、`StatField`/`MetricStats`（统计容器）、`AggregatedMetrics`（输出）。 |
| [genai_bench/time_units.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/time_units.py) | `TimeUnitConverter`，保存时把秒级延迟换算成毫秒。 |
| [genai_bench/cli/cli.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py) | 主流程在哪里调用 `aggregate_metrics_data` 和 `save`。 |
| [genai_bench/cli/validation.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py) | `validate_warmup_cooldown_ratio_options`：校验 warmup/cooldown 比例之和必须小于 1。 |

一句话数据流：每条请求完成 → `add_single_request_metrics` 入库 → 整轮 run 结束 → `aggregate_metrics_data` 聚合 → `save` 落盘。

## 4. 核心概念与源码讲解

### 4.1 聚合流程：从一堆请求到一份摘要

#### 4.1.1 概念说明

在 genai-bench 里，**一次 run**（一次运行）由 `[scenario, concurrency]`（场景 × 并发档位）唯一界定。比如「场景 `N(480,240)/(300,150)`、并发 8」就是一次 run。一次 run 期间，会有成百上千条请求被发出去，每条都产出一条 `RequestLevelMetrics`。

把这一堆「逐请求」的数压成「这一轮整体表现如何」，就是 `AggregatedMetricsCollector` 的核心职责。它产出的 `AggregatedMetrics` 才是最终能进 Excel 报告、能画图、能跨 run 横向比较的东西。

#### 4.1.2 核心流程

整个聚合器有三个状态：

- `all_request_metrics`：一个列表，**按到达顺序**逐条累积所有 `RequestLevelMetrics`（成功和失败都要）。
- `aggregated_metrics`：一个 `AggregatedMetrics` 对象，存放聚合结果，初始为空。
- `_live_metrics_data`：给实时仪表盘用的轻量副本（本讲略讲，详见 u7-l2）。

聚合主流程 `aggregate_metrics_data(start_time, end_time, warmup_ratio, cooldown_ratio)` 的伪代码：

```
对 RequestLevelMetrics 的每一个数值字段 key（排除 error_code / error_message）：
    values = []
    for 每一条 metrics（带下标 i）:
        跳过有 error_code 的失败请求
        取 value = metrics[key]
        跳过 value 为 None 的
        若 i 落在 [warmup_number, 总数 - cooldown_number) 区间：
            values.append(value)
    用 numpy 对 values 算 min/max/mean/stddev/sum/p25..p99
    写入 aggregated_metrics.stats[key]

再算 run_duration = end_time - start_time
mean_output_throughput = Σ num_output_tokens / run_duration
error_rate、requests_per_second、num_requests 等元信息
```

注意吞吐量的定义：它**不是**「每条请求吞吐取平均」，而是「整轮总输出 token 数 ÷ 整轮时长」。这才是服务端真正的「每秒能吐多少 token」。

#### 4.1.3 源码精读

先看聚合器的构造，它把三个状态都初始化好：

[genai_bench/metrics/aggregated_metrics_collector.py:28-39](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py#L28-L39)：`__init__` 创建空的 `AggregatedMetrics`、空的请求列表，以及给 UI 用的 `_live_metrics_data`。

每条请求到来时，先进 `add_single_request_metrics`。它先做异常值过滤（4.3 节细讲），再把指标塞进列表，并按是否带 `error_code` 分别计数：

[genai_bench/metrics/aggregated_metrics_collector.py:41-55](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py#L41-L55)：失败请求累加进 `error_codes_frequency` 字典（统计每个错误码出现次数），成功请求让 `num_completed_requests` 加 1。

整轮结束后调用 `aggregate_metrics_data`。先确定要聚合哪些字段——直接遍历 `RequestLevelMetrics.model_fields`，排除两个非数值字段：

[genai_bench/metrics/aggregated_metrics_collector.py:172-176](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py#L172-L176)：`filtered_keys` 用模型字段名驱动循环，这样模型加字段，聚合自动跟上，无需手动维护两份字段清单。

随后对每个字段收集合法取值（跳过失败请求、跳过 `None`、跳过 warmup/cooldown 区间外的），再用 numpy 算统计量：

[genai_bench/metrics/aggregated_metrics_collector.py:216-228](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py#L216-L228)：`np.percentile(values, [25, 50, 75, 90, 95, 99])` 一次算出 6 个百分位，连同 `min/max/mean/stddev/sum` 一起写进对应的 `StatField`。

> 这里的 `getattr(self.aggregated_metrics.stats, key)` 拿到的是一个 `StatField` 实例。`StatField` 支持 `[key]` 风格的读写，所以既能 `stat_field.p50 = ...`，也能 `stat_field["p50"] = ...`，见 [metrics.py:92-98](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L92-L98)。

百分位计算可形式化为：

\[
p_k = \mathrm{percentile}(\{v_1, v_2, \dots, v_n\},\ k)
\]

其中 numpy 默认用线性插值。例如对 `ttft = [0.1, 0.2, 0.3, 0.4, 0.5]`，p50 = 0.3，p99 ≈ 0.496（位于最后两值之间）。

统计量算完后，计算整轮的吞吐与元信息。吞吐的分母是 `run_duration`：

[genai_bench/metrics/aggregated_metrics_collector.py:231-248](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py#L231-L248)：`run_duration = end_time - start_time`；`mean_output_throughput_tokens_per_s = num_output_tokens_sum / run_duration`。注意分子取的是 `stats.num_output_tokens.sum`（上面循环刚算出的总和），用 `or 0` 兜底 `None`。

吞吐公式：

\[
\text{mean\_output\_throughput} = \frac{\displaystyle\sum_{i} \text{num\_output\_tokens}_i}{\text{run\_duration}}
\]

最后算错误率与 RPS：

[genai_bench/metrics/aggregated_metrics_collector.py:250-280](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py#L250-L280)：`error_rate = num_error_requests / num_requests`（带除零保护），错误率 ≥ 0.5 会打告警日志；`requests_per_second = num_completed_requests / run_duration`。注意 `num_requests = num_completed_requests + num_error_requests` 在这里才被赋值，所以错误率那段用的是默认值 0——这些元信息之间存在依赖顺序，赋值顺序在源码里是精心排好的。

#### 4.1.4 代码实践

见本讲 **第 5 节综合实践**（构造一批 `RequestLevelMetrics` 喂给聚合器，核对 p50/p99 与吞吐）。

#### 4.1.5 小练习与答案

**练习 1**：`aggregate_metrics_data` 里，`filtered_keys` 为什么要排除 `error_code` 和 `error_message`？

**参考答案**：`error_code` 是整数、`error_message` 是字符串，对它们算 min/max/百分位/标准差都没有统计意义；错误信息已经以 `error_codes_frequency`（错误码→出现次数）和 `error_rate` 的形式单独聚合了。

**练习 2**：如果 `aggregate_metrics_data` 被调用时 `all_request_metrics` 为空会怎样？

**参考答案**：函数开头会判断 `if not self.all_request_metrics:`，打一条警告日志（提示可能运行时间太短、服务一条请求都没跑完）然后直接 `return`，不做任何聚合。

### 4.2 warmup/cooldown 过滤：剔掉冷启动与收尾噪声

#### 4.2.1 概念说明

压测刚开始时，服务端 KV-cache 是冷的、连接还在建立、调度器还在预热，前几条请求的延迟往往偏高；快结束时，少量在途请求的收尾又会引入统计噪声。这两段都不代表「稳态」性能。

genai-bench 用两个比例参数把头尾的请求从聚合里剔除：

- `--warmup-ratio`：开头多少比例的请求算预热，不计入统计。
- `--cooldown-ratio`：结尾多少比例的请求算收尾，不计入统计。

关键点：这里的「比例」是**按请求数**（`len(all_request_metrics)`）算的，**不是按时间**。也就是说，「剔除前 10% 请求」剔除的是最早完成的那 10% 条请求。

#### 4.2.2 核心流程

```
n = len(all_request_metrics)
warmup_number   = floor(n * warmup_ratio)     # 跳过的头部请求数
cooldown_number = floor(n * cooldown_ratio)   # 跳过的尾部请求数
保留区间：下标 i ∈ [warmup_number, n - cooldown_number)
```

用 `int(...)` 实现向下取整。边界校验在 CLI 层：

\[
r_w + r_c < 1.0
\]

即两者之和必须严格小于 1，否则连一条请求都留不下。

#### 4.2.3 源码精读

[genai_bench/metrics/aggregated_metrics_collector.py:178-192](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py#L178-L192)：把比例换算成「条数」并打日志，告诉你具体剔了几条。

真正起过滤作用的是收集取值时那一行区间判断：

[genai_bench/metrics/aggregated_metrics_collector.py:194-206](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py#L194-L206)：`if warmup_number <= i < len(self.all_request_metrics) - cooldown_number` 才把 `value` 加入 `values`。注意 `i` 是在**全集**（含失败请求）里的下标——失败请求会被前面 `if metrics.error_code: continue` 跳过，但它的下标位置仍然占位，所以 warmup/cooldown 是按请求的全局到达顺序切的。

CLI 侧的比例校验：

[genai_bench/cli/validation.py:422-431](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L422-L431)：`validate_warmup_cooldown_ratio_options` 在 `cooldown_ratio` 回调里读取已解析的 `warmup_ratio`，两者之和 `>= 1.0` 就报 `BadParameter`，提示必须 `< 1.0`。

#### 4.2.4 代码实践

**目标**：观察 warmup/cooldown 如何改变 `ttft` 的均值。

**操作步骤**：

1. 构造 10 条 `RequestLevelMetrics`，令 `ttft` 依次为 `[0.0, 0.0, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.0]`（首尾各两条设为 0，模拟冷启动/收尾异常值）。
2. 先用 `aggregate_metrics_data(0, 1, 0.0, 0.0)` 聚合，记录 `stats.ttft.mean`。
3. 再 `clear()` 后重新喂一遍，用 `aggregate_metrics_data(0, 1, 0.2, 0.2)` 聚合（剔除首尾各 `int(10*0.2)=2` 条），记录新的 `stats.ttft.mean`。

**需要观察的现象**：第 2 步均值被首尾的 0 拉低（10 条里 4 个 0，6 个 0.1，均值约 0.06）；第 3 步剔掉首尾各 2 条后，剩下的 6 条全是 0.1，均值变为 0.1。

**预期结果**：warmup/cooldown 把冷启动与收尾噪声过滤掉后，`ttft.mean` 从约 0.06 升到 0.1（更接近稳态）。（待本地验证）

#### 4.2.5 小练习与答案

**练习 1**：10 条请求、`warmup_ratio=0.2`、`cooldown_ratio=0.2`，保留区间包含哪些下标？

**参考答案**：`warmup_number = int(10*0.2) = 2`，`cooldown_number = 2`，保留下标 `i ∈ [2, 10-2) = [2, 8)`，即下标 2、3、4、5、6、7，共 6 条。

**练习 2**：为什么校验要求 `warmup_ratio + cooldown_ratio < 1.0` 而不是 `<= 1.0`？

**参考答案**：区间是半开 `[w, n-c)`。若 `w + c == n`（或更大），区间为空，一条请求都留不下，聚合就没意义了；`>=` 正好对应「留不下任何请求」的边界，所以触发报错。

### 4.3 异常值过滤与保存：`filter_metrics` 与 `save`

#### 4.3.1 概念说明

聚合要面对两类「脏数据」：

1. **极短输出导致的假高 TPOT**。当一次请求的 `output_latency` 极小（比如 0.0002s），TPOT 与 `output_inference_speed` 会对计时抖动极其敏感——分母接近 0，算出来的「每秒推理 token 数」可能高得离谱（例如几百万 tokens/s）。这种值一旦混进聚合，会把 p99、均值严重带偏。`filter_metrics` 负责把它们置为 `None`，让聚合循环自动跳过。
2. **空字段**。`aggregate_metrics_data` 在收集取值时，遇到 `None` 会跳过；遇到整字段一个值都没有，会抛 `ValueError`（因为「正常情况不该发生」）。

聚合完之后，`save` 把结果写成 JSON 文件，这是后续分析报告的输入。

#### 4.3.2 核心流程

`filter_metrics`（静态方法，在 `add_single_request_metrics` 入库前调用）：

```
若 output_latency < 0.001（1ms）且 tpot != 0：
    把 metrics.tpot 和 metrics.output_inference_speed 置为 None
    打一条 warning 日志
    返回 True（已过滤）
否则：
    返回 False
```

阈值 0.001s 的来历：正常 LLM 生成速度 10–200 tokens/s，绝大多数请求的 `output_latency` 远大于 1ms；低于 1ms 几乎只能是计时噪声。条件里 `tpot != 0` 很关键——它把非流式任务（embeddings/image 等，TPOT 被 u4-l1 重置为 0）排除在外，避免误伤。

`save` 的输出结构：

```json
{
  "aggregated_metrics": { ...聚合结果，stats 已转成 dict... },
  "individual_request_metrics": [ ...每条请求的明细... ],
  "_time_unit": "s"   // 或 "ms"
}
```

保存前会用 `TimeUnitConverter` 把所有秒级延迟字段（`ttft/tpot/e2e_latency/output_latency`，含它们 stats 下的 11 个统计键）换算成目标单位。

#### 4.3.3 源码精读

`filter_metrics` 的实现与注释解释了阈值依据：

[genai_bench/metrics/aggregated_metrics_collector.py:79-119](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py#L79-L119)：条件 `metrics.output_latency < 0.001 and metrics.tpot != 0` 满足时，把 `tpot`、`output_inference_speed` 设为 `None` 并 `return True`。注意它**就地修改**传入的 `metrics` 对象，所以后续该对象进列表时这两个字段已经是 `None`，聚合循环里 `if value is None: continue` 会跳过。

空值的兜底处理：

[genai_bench/metrics/aggregated_metrics_collector.py:208-214](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py#L208-L214)：若某字段一个值都没收集到，只要它不属于 `AUDIO_METRICS_FIELDS`（`audio_throughput`，只有语音任务才有），就抛 `ValueError("... This should never happen!")`。这是因为 `RequestLevelMetrics` 的 `model_validator` 保证非失败请求的所有字段都非 `None`（见 [metrics.py:54-74](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L54-L74)），所以非音频字段不可能「全空」——真发生了说明所有请求都失败了，是异常状态。

`save` 把结果序列化为 JSON，并做时间单位换算：

[genai_bench/metrics/aggregated_metrics_collector.py:317-338](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py#L317-L338)：先 `TimeUnitConverter.convert_metrics_dict` 转聚合结果、`convert_metrics_list` 转逐请求明细，再 `json.dump` 写出三段式结构（`aggregated_metrics` / `individual_request_metrics` / `_time_unit`）。

单位换算的实现：

[genai_bench/time_units.py:56-97](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/time_units.py#L56-L97)：`convert_metrics_dict` 遍历 `LATENCY_FIELDS = {ttft, tpot, e2e_latency, output_latency}`，把这些字段本身、以及它们在 `stats` 下嵌套的 11 个统计键全部按 `s→ms` 乘 1000 转换，token 计数与吞吐字段不受影响（u4-l3 会专门讲单位转换）。

主流程对 `ValueError` 的兜底——即便聚合失败，也先把逐请求明细存成 debug 文件方便排查：

[genai_bench/cli/cli.py:466-487](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L466-L487)：`aggregate_metrics_data` 抛 `ValueError` 时，先把该 run 明细存成 `debug_for_run_{场景}_{并发}.json`，再把异常抛给上层。正常路径则在 [cli.py:507-510](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L507-L510) 用 `{场景}_{task}_{iteration_type}_{iteration}_time_{总时长}s.json` 命名保存。

#### 4.3.4 代码实践

**目标**：亲手触发一次 `filter_metrics`，观察它把哪些字段改成了 `None`。

**操作步骤**：

1. 构造一条 `RequestLevelMetrics`，令 `output_latency=0.0002`（< 0.001）、`tpot=0.0000002`（极小，对应一个天文数字的 `output_inference_speed`），其余字段正常填。
2. 调用 `collector.add_single_request_metrics(metrics)`。
3. 从 `collector.all_request_metrics[0]` 读回，检查 `tpot` 与 `output_inference_speed`。

**需要观察的现象**：日志会打印一条 `Metric may have abnormal inference speed ... Filtering it out ...` 的 warning；读回的对象里 `tpot` 和 `output_inference_speed` 都变成了 `None`，而 `ttft`、`output_latency`、`num_output_tokens` 等其他字段保持原值。

**预期结果**：`stored.tpot is None` 且 `stored.output_inference_speed is None`，`stored.ttft == 0.1`。这正是 `tests/metrics/test_metrics.py` 里 `test_filter_metrics` 断言的行为。（待本地验证）

#### 4.3.5 小练习与答案

**练习 1**：`filter_metrics` 的条件里为什么要有 `metrics.tpot != 0`？去掉会怎样？

**参考答案**：非流式任务（embeddings、image 等）的 TPOT 被 u4-l1 的重置分支置为 0 作为「不适用」哨兵。若不加 `tpot != 0`，这些任务的请求只要 `output_latency < 0.001` 就会被误判为异常并改写，但其实它们根本没有「逐 token 输出」，TPOT 本就不该参与。加上这个条件，过滤器只针对真正在做流式生成、却因极短输出导致计时失真的请求。

**练习 2**：`save` 写出的 JSON 里，`stats` 为什么是嵌套字典而不是 `StatField` 对象？

**参考答案**：JSON 只能表示基本类型。`AggregatedMetrics.model_dump` 被覆写，把 `stats` 调用 `MetricStats.to_dict()` 转成 `{字段名: {统计键: 值}}` 的纯字典结构（见 [metrics.py:117-123](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L117-L123) 与 [metrics.py:185-194](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L185-L194)），读回时 `model_validate` 再用 `MetricStats.from_dict` 重建，形成闭环。

## 5. 综合实践

把本讲三个模块串起来：**构造请求 → 喂给聚合器 → 聚合 → 核对 p50/p99 与吞吐**。

> 这是一个可直接运行的「源码阅读 + 动手验证」型实践。代码基于 `tests/metrics/test_metrics.py` 的真实用法改写。若你已 `pip install -e .` 安装了项目，可直接 `python` 运行；否则请「待本地验证」。

**目标**：构造 5 条 TTFT 递增的请求，核对 `ttft` 的 `p50/p99/mean` 与 `mean_output_throughput_tokens_per_s`。

**操作步骤**：

写下面这段脚本并运行（示例代码）：

```python
# 示例代码：手动驱动 AggregatedMetricsCollector
from genai_bench.metrics.aggregated_metrics_collector import AggregatedMetricsCollector
from genai_bench.metrics.metrics import RequestLevelMetrics

collector = AggregatedMetricsCollector()

ttfts = [0.10, 0.20, 0.30, 0.40, 0.50]
for t in ttfts:
    # 注意：error_code 为 None 时，除 audio_throughput 外所有字段都必须非 None
    # （见 RequestLevelMetrics 的 model_validator）
    m = RequestLevelMetrics(
        ttft=t,
        tpot=0.2,
        e2e_latency=1.0,
        output_latency=0.8,
        output_inference_speed=5.0,
        num_input_tokens=10,
        num_output_tokens=10,
        num_reasoning_tokens=0,
        total_tokens=20,
        input_throughput=50.0,
        output_throughput=12.5,
    )
    collector.add_single_request_metrics(m)

# start_time=0, end_time=2 -> run_duration=2；warmup/cooldown 都为 0
collector.aggregate_metrics_data(0.0, 2.0, 0.0, 0.0)

ttft_stats = collector.aggregated_metrics.stats.ttft
print("ttft p50  =", ttft_stats.p50)
print("ttft p99  =", ttft_stats.p99)
print("ttft mean =", ttft_stats.mean)
print("mean_output_throughput =",
      collector.aggregated_metrics.mean_output_throughput_tokens_per_s)
```

**需要观察的现象**：5 条 TTFT 为 `[0.1, 0.2, 0.3, 0.4, 0.5]` 的请求被聚合。

**预期结果**（按公式手算，待本地验证）：

- `ttft p50 = 0.3`（中位数，正好是第 3 个值）
- `ttft p99 ≈ 0.496`（numpy 线性插值，落在 0.4 与 0.5 之间）
- `ttft mean = 0.3`
- `mean_output_throughput_tokens_per_s = 25.0`（总输出 token = 5×10 = 50，÷ run_duration 2.0）

**延伸**：在脚本末尾再加一行 `collector.save("/tmp/demo_run.json", "ms")`，打开文件确认 `aggregated_metrics.stats.ttft.p50` 变成了 `300.0`（秒→毫秒放大 1000 倍），而 `num_output_tokens` 仍为整数不变——验证 4.3 节的「只有延迟字段会被换算」。

## 6. 本讲小结

- 一次 **run** = 一组 `[scenario, concurrency]`，`AggregatedMetricsCollector` 把这一轮所有 `RequestLevelMetrics` 聚合成一份 `AggregatedMetrics` 摘要。
- 聚合由 `RequestLevelMetrics.model_fields` 驱动，对每个数值字段算 `min/max/mean/stddev/sum` 与 6 个百分位（p25~p99），排除 `error_code`/`error_message`。
- 整轮吞吐 = **总 token 数 ÷ run_duration**，不是单请求吞吐的平均；错误率、RPS 等元信息在统计量之后才计算，顺序有依赖。
- **warmup/cooldown 按请求数（不是按时间）剔除**头尾，区间为半开 `[warmup_number, n - cooldown_number)`，CLI 校验两者之和必须 `< 1.0`。
- `filter_metrics` 把 `output_latency < 0.001` 且 `tpot != 0` 的请求的 TPOT/推理速度置 `None`，剔除「假高 TPOT」；非流式任务的 `tpot=0` 哨兵不会被误伤。
- `save` 写出三段式 JSON（`aggregated_metrics` / `individual_request_metrics` / `_time_unit`），并用 `TimeUnitConverter` 只换算延迟类字段；聚合抛 `ValueError` 时主流程会先存 debug 明细。

## 7. 下一步学习建议

- 本讲的 `AggregatedMetrics` 通过 `save` 落盘后，就进入分析与报告环节。接下来读 [u4-l3 指标数据模型与时间单位转换](u4-l3-metrics-models-and-time-units.md)，深入 `RequestLevelMetrics/AggregatedMetrics/MetricStats/StatField` 的分层结构与 `TimeUnitConverter`，把本讲「为什么只有延迟字段被换算」彻底讲透。
- 随后进入 **U6 实验分析与报告**，从 [u6-l1 实验结果加载 experiment_loader](u6-l1-experiment-loader.md) 开始，看本讲写出的 run JSON 如何被读回、组织成 scenario×concurrency 结构并生成 Excel/绘图。
- 如果你对聚合器如何与 Locust 主从架构联动感兴趣（每条请求是怎么从 worker 跨进程送到 master 的 `add_single_request_metrics` 的），可以跳读 [u7-l1 DistributedRunner 主从架构](u7-l1-distributed-runner.md)。

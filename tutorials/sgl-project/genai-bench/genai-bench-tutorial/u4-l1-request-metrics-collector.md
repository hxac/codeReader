# 单请求指标计算 RequestMetricsCollector

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 TTFT、e2e_latency、output_latency、TPOT、各类吞吐（throughput）这几个核心指标的**计算公式**与**物理含义**。
- 看懂 `RequestMetricsCollector.calculate_metrics` 如何从一条 `UserResponse`（含三个时间戳和若干 token 计数）换算出 `RequestLevelMetrics`。
- 解释 TPOT 与 output_inference_speed、output_throughput 之间的**数学等价关系**，以及为什么 TPOT 公式里分母是 `num_output_tokens - 1`。
- 区分 chat / embeddings / rerank / image / text-to-speech 等不同任务在指标计算上的**分支差异**，尤其是「非聊天任务的指标重置」为什么要重置为 `0` 而不是 `None`。

本讲是「指标」单元的第一篇，只负责**单条请求**级别的指标计算；多条请求如何聚合成一次 run 的统计（p50/p99、warmup 过滤等）留到 [u4-l2](u4-l2-aggregated-metrics-collector.md)。

## 2. 前置知识

本讲默认你已掌握：

- **协议数据模型**（[u1-l5](u1-l5-protocol-models.md)）：`UserResponse` 基类携带 `status_code`、`start_time`、`time_at_first_token`、`end_time` 三个时间戳与 `num_prefill_tokens`；`UserChatResponse` 在此之上追加 `tokens_received`、`reasoning_tokens`。本讲的全部输入就是这些字段。
- **User 基类与 Locust 集成**（[u3-l1](u3-l1-base-user-and-locust.md)）：`BaseUser.collect_metrics()` 是指标上报的出口，它内部就会 `new` 一个 `RequestMetricsCollector`、调用 `calculate_metrics`，再把结果序列化发给 master。本讲解码这中间最关键的「换算」一步。

几个通俗概念先建立直觉：

- **流式输出**：LLM 不是一次性吐出整段回答，而是一个 token 一个 token 地「边想边说」。
- **TTFT（Time To First Token）**：模型「多久才开口」——从发出请求到收到第一个 token 的时间。它主要反映**输入阶段（prefill）**的处理速度：prompt 越长、prefill 越慢，TTFT 越大。
- **TPOT（Time Per Output Token）**：模型「开口后说得多快」——每生成一个新 token 平均花多少时间。它反映**输出阶段（decode）**的速度。
- 单看端到端延迟无法区分「久才开口」和「开口后很慢」，所以必须拆成 token 级指标。这正是 genai-bench 存在的核心动机（回顾 [u1-l1](u1-l1-project-overview.md)）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [genai_bench/metrics/request_metrics_collector.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/request_metrics_collector.py) | **本讲主角**。`RequestMetricsCollector` 把一条 `UserResponse` 换算成一组请求级指标。 |
| [genai_bench/metrics/metrics.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py) | 定义 `RequestLevelMetrics` 数据模型、`OUTPUT_METRICS_FIELDS`/`AUDIO_METRICS_FIELDS` 字段集合，以及一个 `model_validator`。是计算结果的「容器」。 |
| [genai_bench/user/base_user.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py) | `collect_metrics()` 调用本讲 collector 的上层入口，展示调用上下文。 |
| [docs/getting-started/metrics-definition.md](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/getting-started/metrics-definition.md) | 官方指标定义表，公式权威来源。 |
| [tests/metrics/test_metrics.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/metrics/test_metrics.py) | 单测，给出可直接核对的输入与期望输出。 |

---

## 4. 核心概念与源码讲解

### 4.1 指标公式与公共指标计算

#### 4.1.1 概念说明

每条请求的生命周期可以用一条**时间轴**上三个时刻来刻画：

```
start_time          time_at_first_token                 end_time
   |                        |                              |
   |<------- TTFT --------->|                              |
   |<----------------- e2e_latency ----------------------->|
                            |<------- output_latency ---->|
```

- `start_time`：请求发出的时刻。
- `time_at_first_token`：收到**第一个**输出 token 的时刻。
- `end_time`：收到完整响应的时刻。

从这三个时刻，可以推出**所有**延迟类指标。这是整个指标体系的几何骨架——只要这三个时间戳准确，其余都是加减法。

#### 4.1.2 核心流程

公共指标（所有任务都算）有四项，公式如下：

\[ \text{ttft} = \text{time\_at\_first\_token} - \text{start\_time} \]

\[ \text{e2e\_latency} = \text{end\_time} - \text{start\_time} \]

\[ \text{input\_throughput} = \frac{\text{num\_input\_tokens}}{\text{ttft}} \quad (\text{ttft} \neq 0) \]

\[ \text{total\_tokens} \leftarrow \text{num\_input\_tokens} \quad (\text{后续 chat 任务会再 } += \text{输出 token}) \]

其中 `num_input_tokens` 直接取自响应里的 `num_prefill_tokens`（prompt 的 token 数）。`input_throughput` 衡量 prefill 阶段「每秒吃进多少输入 token」，注意它对 `ttft == 0` 做了除零保护，回退为 `0`。

> 提示：`input_throughput` 的分母是 TTFT 而不是 e2e_latency，因为输入处理只发生在「开口前」这一段。

#### 4.1.3 源码精读

进入 `calculate_metrics`，第一步是**断言**四个字段非空——这是契约保护：上游解析器（[u3-l2](u3-l2-openai-user-response-parsing.md)）必须把时间戳和 prefill token 数填好，否则这里直接报错而不是静默算出错误指标：

[genai_bench/metrics/request_metrics_collector.py:36-43](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/request_metrics_collector.py#L36-L43) —— 四个 `assert` 校验 `num_prefill_tokens`、`time_at_first_token`、`start_time`、`end_time` 均不为 `None`。

紧接着是公共指标的「安全计算」段，对应上面三个公式：

[genai_bench/metrics/request_metrics_collector.py:46-56](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/request_metrics_collector.py#L46-L56) —— 设置 `num_input_tokens`、`ttft`、`e2e_latency`、`total_tokens`，并用三元表达式给 `input_throughput` 做除零保护。

这段的关键词是「Safely calculate common metrics」：无论后面是哪种任务，这四项都要先算出来。

公式对照表见官方文档，权威且完整：

[docs/getting-started/metrics-definition.md:14-27](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/getting-started/metrics-definition.md#L14-L27) —— Single Request Level Metrics 的公式表（TTFT / e2e_latency / TPOT / output_latency / 各类 throughput 等）。

#### 4.1.4 代码实践

**实践目标**：确认「公共指标」分支在非 chat 任务上也能正确工作，并体会断言的作用。

**操作步骤**：

1. 阅读单测 `test_request_level_metrics_calculation_with_embeddings_response`，它用 `MagicMock(spec=UserResponse)` 模拟一条 embeddings 响应。
2. 手算期望值：`start_time=1722986631`、`time_at_first_token=1722986741`、`end_time=1722986741`、`num_prefill_tokens=10`。

**需要观察的现象 / 预期结果**：

- \( \text{ttft} = 1722986741 - 1722986631 = 110 \) s
- \( \text{e2e\_latency} = 1722986741 - 1722986631 = 110 \) s（embeddings 几乎「请求即返回」，所以首 token 时间 ≈ 结束时间）
- \( \text{num\_input\_tokens} = 10 \)

这与测试断言完全一致：[tests/metrics/test_metrics.py:84-86](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/metrics/test_metrics.py#L84-L86)。

3. **可选动手**：把任意一个时间戳改成 `None`，重新构造响应并调用 `calculate_metrics`，观察 `assert` 抛出的错误信息（这是契约保护的直观体现）。

#### 4.1.5 小练习与答案

**练习 1**：某请求 `start_time=10.0`、`time_at_first_token=12.5`、`end_time=30.0`、`num_prefill_tokens=500`。求 ttft、e2e_latency、input_throughput。

**答案**：ttft \(= 12.5-10.0 = 2.5\) s；e2e_latency \(= 30.0-10.0 = 20.0\) s；input_throughput \(= 500/2.5 = 200\) tokens/s。

**练习 2**：为什么 `input_throughput` 的分母用 ttft 而不是 e2e_latency？

**答案**：输入（prompt）的处理只发生在 prefill 阶段，即「开口前」的这段时间，长度正好等于 TTFT。用 e2e_latency 会把毫无关系的输出阶段时长也算进分母，低估 prefill 的真实吞吐。

---

### 4.2 输出指标计算

#### 4.2.1 概念说明

只有 **chat 类**任务（`text-to-text`、`image-text-to-text` 等）才有「输出阶段」——模型逐 token 生成回答。此时除了公共指标，还要算四个**输出类指标**：

- `output_latency`：首 token 之后到结束的时长，即「纯生成」耗时。
- `tpot`：每多生成一个 token 平均花多少秒（s/token）。
- `output_inference_speed`：每秒能生成多少 token（tokens/s），是 TPOT 的倒数。
- `output_throughput`：单请求输出吞吐（tokens/s）。

后三者看起来重复，下面会揭示它们其实是**同一个量的三种写法**。

#### 4.2.2 核心流程

先定义 output_latency：

\[ \text{output\_latency} = \text{e2e\_latency} - \text{ttft} \]

> **为什么 TPOT 分母是 `num_output_tokens - 1`？**
> 设共生成了 \(N\) 个输出 token。第一个 token 在 `time_at_first_token` 时刻到达，它的时间已经被算进 TTFT 了；真正落在 output_latency 这段时间里的，是 token 2、token 3、…、token \(N\) 的到达。从第 1 个到第 \(N\) 个之间共有 \(N-1\) 个「相邻 token 间隔」，这些间隔的总时长正是 output_latency。因此：

\[ \text{tpot} = \frac{\text{output\_latency}}{\text{num\_output\_tokens} - 1} \]

进而：

\[ \text{output\_inference\_speed} = \frac{1}{\text{tpot}} = \frac{\text{num\_output\_tokens} - 1}{\text{output\_latency}} \]

\[ \text{output\_throughput} = \frac{\text{num\_output\_tokens} - 1}{\text{output\_latency}} \]

于是得到一个关键恒等式——**对单条请求而言，这三者数值完全相等**：

\[ \text{output\_throughput} \;=\; \text{output\_inference\_speed} \;=\; \frac{1}{\text{tpot}} \]

它们之所以都保留，是因为语义侧重不同（TPOT 是「每个 token 多久」，throughput 是「每秒多少 token」），并且**到了聚合层（u4-l2）它们的统计口径会分化**：聚合吞吐用 `sum(output_tokens)/run_duration`，而 `output_inference_speed` 用请求级 `1/tpot` 的均值。本讲只看单请求，所以三者恒等。

还有一个特殊处理：当 `num_output_tokens <= 1` 时无法算 TPOT（分母为 0 或负），代码会跳过计算并发一条告警日志。

#### 4.2.3 源码精读

输出指标在私有方法 `_calculate_output_metrics` 里计算，仅当响应是 `UserChatResponse` 时才被调用：

[genai_bench/metrics/request_metrics_collector.py:75-83](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/request_metrics_collector.py#L75-L83) —— 取 `tokens_received` 作为 `num_output_tokens`，把 `reasoning_tokens` 兜底为 0，`total_tokens += num_output_tokens`，并算出 `output_latency`。

注意 `num_reasoning_tokens` 用了 `response.reasoning_tokens or 0`：推理模型才会返回非零的 reasoning token（回顾 [u3-l2](u3-l2-openai-user-response-parsing.md)），非推理模型这里为 `None`/`0`。

接着是带除零保护与 `> 1` 守卫的核心计算：

[genai_bench/metrics/request_metrics_collector.py:86-101](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/request_metrics_collector.py#L86-L101) —— 当 `num_output_tokens > 1` 时计算 `tpot`、`output_inference_speed`、`output_throughput`；否则告警。

这里的 `> 1` 守卫正是为了避免 `output_latency / (num_output_tokens - 1)` 出现除以零。`output_throughput` 还额外对 `output_latency == 0` 做了三元保护，回退为 `0`。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：构造一个真实的 `UserChatResponse`，调用 `calculate_metrics`，亲手验证 ttft / tpot / throughput 的数值与上面的恒等式。

**操作步骤**：把下面脚本保存为 `verify_metrics.py` 并运行（需已 `pip install genai-bench`，见 [u1-l2](u1-l2-install-and-first-run.md)）。

```python
# verify_metrics.py —— 示例代码（非项目原有文件）
from genai_bench.protocol import UserChatResponse
from genai_bench.metrics.request_metrics_collector import RequestMetricsCollector

# 构造一条模拟的成功流式响应（单位：秒，数值故意取整方便手算）
resp = UserChatResponse(
    status_code=200,
    start_time=0.0,
    time_at_first_token=2.0,   # 第 2 秒开口
    end_time=12.0,             # 第 12 秒结束
    num_prefill_tokens=100,    # 输入 100 token
    tokens_received=11,        # 输出 11 token
    reasoning_tokens=None,     # 非推理模型
)

collector = RequestMetricsCollector()
collector.calculate_metrics(resp)
m = collector.metrics

print(f"ttft                   = {m.ttft}")             # 预期 2.0
print(f"e2e_latency            = {m.e2e_latency}")      # 预期 12.0
print(f"output_latency         = {m.output_latency}")   # 预期 10.0
print(f"num_input_tokens       = {m.num_input_tokens}") # 预期 100
print(f"num_output_tokens      = {m.num_output_tokens}")# 预期 11
print(f"total_tokens           = {m.total_tokens}")     # 预期 111  (=100+11)
print(f"input_throughput       = {m.input_throughput}") # 预期 50.0 (=100/2)
print(f"tpot                   = {m.tpot}")             # 预期 1.0  (=10/(11-1))
print(f"output_inference_speed = {m.output_inference_speed}")  # 预期 1.0 (=1/tpot)
print(f"output_throughput      = {m.output_throughput}")       # 预期 1.0 (=(11-1)/10)
```

**需要观察的现象**：

- `output_inference_speed` 与 `output_throughput` 打印值**完全相等**，且都等于 `1 / tpot`——验证 4.2.2 的恒等式。
- `total_tokens = 111`，等于输入 + 输出。

**预期结果**（关键行）：`ttft=2.0, tpot=1.0, output_throughput=1.0, total_tokens=111`。

**延伸**：把 `tokens_received` 改成 `1` 再跑，应看到一条 `‼️ num_output_tokens:1 is <= 1` 的告警，且 `tpot`/`output_inference_speed`/`output_throughput` 不再被赋值（保持默认）。

#### 4.2.5 小练习与答案

**练习 1**：若把上面示例的 `end_time` 改为 `22.0`（其余不变），重算 tpot 与 output_throughput。

**答案**：output_latency \(= 22-2 = 20\) s；tpot \(= 20/(11-1) = 2.0\) s/token；output_throughput \(= 10/20 = 0.5\) tokens/s。生成变慢了一倍。

**练习 2**：`output_inference_speed` 和 `output_throughput` 在单请求层永远相等，那为什么模型里要留两个字段？

**答案**：语义不同（「每 token 耗时」vs「每秒 token 数」），更重要的是在**聚合层**它们口径会分化——聚合 output_throughput 用 `sum(output_tokens)/run_duration`，而 inference_speed 是请求级 `1/tpot` 的均值。保留两个字段是为下游统计服务。详见 [u4-l2](u4-l2-aggregated-metrics-collector.md)。

---

### 4.3 非聊天任务的指标重置

#### 4.3.1 概念说明

并非所有任务都有「输出 token」：

- **embeddings / rerank**：只做编码/排序，没有逐 token 生成阶段。
- **text-to-image**：输出是图片，不是 token。
- **text-to-speech**：输出是音频字节，TTFT 被重新解释为「TTFB（首字节时间）」，吞吐用「音频字节/秒」。

对这些任务，`tokens_received` 这类字段没有意义。如果让它们停留在默认的 `None`，下游聚合层会出错（见 4.3.3）。因此 collector 对非 chat 响应统一走「重置输出指标」的分支，把输出类字段**显式置 0**，并用一个特殊的 `audio_throughput` 字段承载 TTS 的音频吞吐。

#### 4.3.2 核心流程

`calculate_metrics` 末尾按响应类型三分支分发：

```
if   isinstance(response, UserChatResponse):           → _calculate_output_metrics()   # 算输出
elif isinstance(response, UserTextToSpeechResponse):   → _reset_output_metrics() + 算 audio_throughput
elif isinstance(response, UserImageGenerationResponse):→ _reset_output_metrics()        # 同 embeddings
else: (embeddings / rerank / 其余)                     → _reset_output_metrics()
```

重置逻辑很简单——遍历 `OUTPUT_METRICS_FIELDS` 这个字段集合，逐个 `setattr(..., 0)`：

\[ \text{对每个 } f \in \text{OUTPUT\_METRICS\_FIELDS}: \quad \text{metrics}.f \leftarrow 0 \]

TTS 额外算音频吞吐（注意它仍要先 reset，因为音频不是 token）：

\[ \text{audio\_throughput} = \frac{\text{audio\_bytes}}{\text{e2e\_latency} - \text{ttft}} \quad (\text{output\_latency} > 0 \text{ 且 } \text{audio\_bytes} > 0) \]

这里 `e2e_latency - ttft` 正是 TTFB 之后的「流式音频传输」时长。

#### 4.3.3 源码精读

先看类型分发：

[genai_bench/metrics/request_metrics_collector.py:58-73](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/request_metrics_collector.py#L58-L73) —— 四分支 `isinstance` 判断，决定走「算输出」还是「重置输出」。

`OUTPUT_METRICS_FIELDS` 集合定义在数据模型里，重置与 filter 都依赖它：

[genai_bench/metrics/metrics.py:42-49](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L42-L49) —— `OUTPUT_METRICS_FIELDS = {tpot, output_latency, output_inference_speed, num_output_tokens, output_throughput, num_reasoning_tokens}`。

重置方法本身只有三行：

[genai_bench/metrics/request_metrics_collector.py:103-106](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/request_metrics_collector.py#L103-L106) —— `_reset_output_metrics` 遍历字段集合置 0。

**为什么是 `0` 而不是 `None`？** 这是本讲最关键的设计点，原因有二：

1. **规避校验器报错**。`RequestLevelMetrics` 有一个 `model_validator`，规定「只要 `error_code` 是 `None`，所有（非错误/非音频）字段就不得为 `None`」：

   [genai_bench/metrics/metrics.py:54-74](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L54-L74) —— `validate_metrics`：成功请求若某字段为 `None` 就抛 `ValueError`。

   一条成功的 embeddings 请求 `error_code` 为 `None`，若它的 `tpot` 也是 `None`，那么当这条 metrics 被序列化、跨进程发给 master、再被 `model_validate` 重建时（见 [base_user.py:90-92](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L90-L92) 的 `send_message("request_metrics", ...)`），校验器就会报错。置 `0` 正是为了让重建顺利通过——代码注释「reset output metrics to avoid NoneType Error in AggregatedMetricsCollector」说的就是这件事。

2. **`0` 是「不适用」的哨兵，区别于 `None`**。聚合层的 `filter_metrics` 会把「异常快」的请求（`output_latency < 0.001s`）的 `tpot`/`output_inference_speed` 置 `None` 以剔除噪声；但它用 `tpot != 0` 这个条件**跳过**非流式任务，正是认 `0` 这个「故意不适用」的标记：

   [genai_bench/metrics/aggregated_metrics_collector.py:101-105](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py#L101-L105) —— `if output_latency is not None and output_latency < 0.001 and tpot != 0:` 才触发过滤。

   于是形成了清晰的语义区分：`None` = 「值不可信，要剔除」；`0` = 「本任务没有这个指标，保留但不参与 token 类统计」。

最后看 image 生成分支的注释，它点明了「与 embeddings 同处理」的理由：

[genai_bench/metrics/request_metrics_collector.py:66-69](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/request_metrics_collector.py#L66-L69) —— image 生成（非流式）走 reset，避免被 `filter_metrics` 误置为 `None`。

#### 4.3.4 代码实践

**实践目标**：对比 chat 与 embeddings/image 响应，确认非聊天任务的输出字段被重置为 `0`（而非 `None`），并理解 `0` 的哨兵含义。

**操作步骤**：

1. 运行下面脚本（示例代码）：

```python
# verify_reset.py —— 示例代码（非项目原有文件）
from genai_bench.protocol import UserResponse, UserChatResponse
from genai_bench.metrics.request_metrics_collector import RequestMetricsCollector

def show(tag, resp):
    c = RequestMetricsCollector()
    c.calculate_metrics(resp)
    m = c.metrics
    print(f"[{tag}] tpot={m.tpot} output_latency={m.output_latency} "
          f"num_output_tokens={m.num_output_tokens} output_throughput={m.output_throughput}")

# embeddings：用基类 UserResponse 即落入 else 分支
show("embeddings", UserResponse(
    status_code=200, start_time=0.0, time_at_first_token=0.5,
    end_time=0.6, num_prefill_tokens=32))
```

2. 把 `UserResponse` 换成 `UserChatResponse` 并补上 `tokens_received=11`，再跑一次对比。

**需要观察的现象 / 预期结果**：

- embeddings 行：`tpot=0 output_latency=0 num_output_tokens=0 output_throughput=0`——全部是 `0`，不是 `None`。
- chat 行：`tpot`、`output_throughput` 为正常正数。

3. **阅读型验证**：对照 [tests/metrics/test_metrics.py:71-86](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/metrics/test_metrics.py#L71-L86)，确认单测只断言了 `ttft/e2e_latency/num_input_tokens`，而**没有**断言输出字段——因为它们对 embeddings 本就无意义（被置 0）。这正是「非聊天任务无输出指标」在测试层面的体现。

#### 4.3.5 小练习与答案

**练习 1**：如果 `_reset_output_metrics` 把字段置成 `None` 而不是 `0`，会在哪一步出问题？

**答案**：当这条成功（`error_code=None`）的 embeddings metrics 经 `model_dump_json()` 发给 master、再用 `model_validate` 重建时，`validate_metrics` 校验器会发现 `tpot` 等字段为 `None` 而抛 `ValueError`，导致聚合失败。

**练习 2**：text-to-speech 任务为什么不直接复用 embeddings 的 `else` 分支，而要单独一个 `elif`？

**答案**：因为 TTS 虽然没有 token 输出（仍需 reset token 类字段），但它有自己专属的 `audio_throughput`（字节/秒）需要计算，分母是 `e2e_latency - ttft`（即 TTFB 之后的音频流式时长）。单独分支才能在 reset 之后补算这个音频吞吐。

**练习 3**：聚合层 `filter_metrics` 用 `tpot != 0` 作为过滤前提，对 embeddings 请求意味着什么？

**答案**：意味着 embeddings 请求（`tpot=0`）不会被当作「异常快」而误剔除，它的指标会被原样保留参与其它统计（如 `num_input_tokens`、`ttft`），只是不参与 token 类输出统计。`0` 在这里充当了「本任务无此指标」的显式标记。

---

## 5. 综合实践

把整讲串起来：模拟一次「混合负载」并解释每条请求的指标来源。

**任务**：写一个函数 `explain(resp)`，接收任意一种响应（`UserChatResponse` / `UserResponse` / `UserTextToSpeechResponse` / `UserImageGenerationResponse`），调用 `RequestMetricsCollector.calculate_metrics`，然后打印一张「指标来源说明表」，对每个非零字段标注它来自**公共计算**还是**输出计算**还是**重置置零**。

**参考实现骨架**（示例代码）：

```python
from genai_bench.metrics.request_metrics_collector import RequestMetricsCollector
from genai_bench.metrics.metrics import RequestLevelMetrics

COMMON = {"ttft", "e2e_latency", "num_input_tokens", "input_throughput"}
OUTPUT = RequestLevelMetrics.OUTPUT_METRICS_FIELDS   # 含 tpot/output_latency/...

def explain(resp):
    c = RequestMetricsCollector()
    c.calculate_metrics(resp)
    m = c.metrics
    for name, val in m.model_dump().items():
        if name in {"error_code", "error_message"}:
            continue
        source = ("公共计算" if name in COMMON
                  else "输出计算/重置" if name in OUTPUT or name == "output_throughput"
                  else "其它")
        print(f"{name:24s} = {val!r:12s}  <- {source}")
```

**验收要点**：

1. 喂入一条 chat 响应，应看到 `ttft`/`e2e_latency` 标「公共计算」，`tpot`/`output_throughput` 标「输出计算」且有真实数值。
2. 喂入一条 embeddings 响应，应看到输出类字段值为 `0`，印证重置逻辑。
3. 对照 4.2 的恒等式，确认 chat 响应的 `tpot`、`output_inference_speed`、`output_throughput` 三者满足 \( \text{output\_throughput} = 1/\text{tpot} \)。

> 待本地验证：若未安装 genai-bench 或缺少依赖，以上脚本需先按 [u1-l2](u1-l2-install-and-first-run.md) 完成安装。

## 6. 本讲小结

- 三个时间戳（`start_time` / `time_at_first_token` / `end_time`）是全部延迟指标的几何骨架：`ttft`、`e2e_latency`、`output_latency` 都是它们的减法。
- 公共指标（`num_input_tokens`、`ttft`、`e2e_latency`、`input_throughput`、`total_tokens`）对所有任务都算，且对 `ttft=0` 做了除零保护。
- TPOT 分母是 `num_output_tokens - 1`，因为 \(N\) 个输出 token 只有 \(N-1\) 个「相邻间隔」落在 output_latency 内；由此推出单请求层 `output_throughput = output_inference_speed = 1/tpot`。
- 非聊天任务（embeddings / rerank / image / tts）走 `_reset_output_metrics`，把输出类字段置 **`0`**：一是规避 `model_validator` 在跨进程重建时报 `None` 错，二是让 `0` 充当「不适用」哨兵，使聚合层 `filter_metrics` 用 `tpot != 0` 跳过它们。
- `RequestLevelMetrics` 既是计算结果的容器，也是跨进程传递的契约——`calculate_metrics` 产出的对象会被 `model_dump_json` 发给 master 聚合（详见 [u4-l2](u4-l2-aggregated-metrics-collector.md)）。

## 7. 下一步学习建议

- 进入 [u4-l2 运行级聚合 AggregatedMetricsCollector](u4-l2-aggregated-metrics-collector.md)：看本讲产出的成百上千条 `RequestLevelMetrics` 如何被聚合成一次 run 的 p50/p99、均值吞吐，以及 warmup/cooldown 过滤与 `filter_metrics` 异常值剔除的完整逻辑。
- 再到 [u4-l3 指标数据模型与时间单位转换](u4-l3-metrics-models-and-time-units.md)：理解 `StatField`/`MetricStats`/`AggregatedMetrics` 的分层，以及 `TimeUnitConverter` 如何把本讲以「秒」为单位的延迟统一换算成毫秒展示。
- 若想回头看「这些时间戳和 token 数从哪来」，复习 [u3-l2 OpenAIUser 流式响应解析](u3-l2-openai-user-response-parsing.md)，重点是 `parse_chat_response` 如何在首个 content chunk 处记录 `time_at_first_token`、如何从 `usage` 取 token 数。

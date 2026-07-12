# 性能监控与构建发布

## 1. 本讲目标

本讲是手册的倒数第二篇，面向「把 lmdeploy 真正搬上生产」的两件事：**看得见**（可观测性）与**发得出去**（构建发布）。学完后你应当掌握：

- 看懂 lmdeploy 的四类运行时统计结构 `SchedulerStats / RequestStats / IterationStats / SpeculativeDecodingStats` 分别回答什么问题。
- 理解 `MetricsProcessor` 这个单例如何用「异步队列 + 后台任务」把引擎产出落账，并喂给文本日志与 Prometheus 两套 logger。
- 区分「在线 metrics」与「离线 profiler」：前者是服务常驻的轻量统计，后者是压测脚本用来算 TTFT/TPOT/ITL/吞吐 的重型采样。
- 能用 `monitoring/` 下的 prometheus + grafana 配置把指标接进监控大盘。
- 看懂 `setup.py` 如何用 `cmake_build_extension` 把 TurboMind 的 C++ 扩展 `_turbomind` 打进 wheel。

本讲承接 u8-l2（服务启动），并复用 u1-l3 已建立的 setup.py + CMake 基础认知，不再重复环境变量细节。

## 2. 前置知识

- **指标（metric）vs 日志（log）vs 链路追踪（trace）**：指标是「可聚合的数值时间序列」（如「过去 10 秒平均吞吐」），日志是「离散事件文本」，追踪是「一次请求穿越各组件的因果链」。本讲只涉及指标。
- **Prometheus 数据模型**：每个指标是一个「名字 + 一组标签」的时间序列。三种基本类型：`Counter`（只增不减的计数器，如已生成 token 总数）、`Gauge`（可增可减的瞬时值，如当前运行中的请求数）、`Histogram`（按 bucket 统计分布，如延迟分位 P50/P99）。Prometheus 服务器主动来「拉（scrape）」你的 `/metrics` 端点，而不是你推给它。
- **LLM 推理的关键延迟指标**：TTFT（首 token 延迟）、TPOT（每 token 时间，prefill 之后）、ITL（相邻 token 间隔）、E2E（端到端）。它们对应的源码概念在 u2-l1/u4-l3 已建立。
- **持续批处理下的统计难点**：一个请求的生命周期跨「API 排队 → 引擎 waiting → running → 完成」多个阶段，且同一 batch 里不同请求处于不同阶段，因此统计必须分「请求级」与「迭代级」两层。

## 3. 本讲源码地图

| 文件 | 作用 | 所属最小模块 |
|------|------|------|
| [lmdeploy/metrics/stats.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/metrics/stats.py) | 四类纯数据统计结构 | metrics stats |
| [lmdeploy/metrics/metrics_processor.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/metrics/metrics_processor.py) | 单例编排器：异步队列 + 后台任务 + logger 分发 | metrics_processor |
| [lmdeploy/metrics/loggers.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/metrics/loggers.py) | 文本 logger 与 Prometheus logger 的实现 | metrics_processor（桥梁） |
| [lmdeploy/profiler.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/profiler.py) | 离线压测用的 `Profiler`/`Session` | profiler |
| [lmdeploy/monitoring/prometheus.yaml](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/monitoring/prometheus.yaml) | Prometheus 抓取配置 | monitoring |
| [lmdeploy/monitoring/docker-compose.yaml](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/monitoring/docker-compose.yaml) | 一键拉起 prometheus + grafana | monitoring |
| [lmdeploy/serve/openai/api_server.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py) | 挂载 `/metrics` 端点 + lifespan 周期管理 | 监控接入 |
| [lmdeploy/serve/core/async_engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py) | 构造 logger、把引擎产出投递给 processor | 监控接入 |
| [setup.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py) | wheel 打包，含 CMake 编译 TurboMind | 构建发布 |

> 提示：`metrics/` 顶部的注释 `adapted from vllm/v1/metrics` 表明这套统计结构借鉴自 vLLM，命名与分桶方式高度相似，迁移知识时可作为参照。

## 4. 核心概念与源码讲解

### 4.1 metrics stats：四类统计的数据模型

#### 4.1.1 概念说明

`stats.py` 不做任何 IO，只定义「数据长什么样」。它把整个服务的可观测面切成四个互不重叠的视角，每个视角一个 dataclass：

- **`SchedulerStats`**——**全局快照**：「此刻」服务里有多少请求、成功/失败各多少、KV cache 用了多少、前缀缓存命中率多少。这是仪表盘上那些「当前值」面板的数据来源。
- **`RequestStats`**——**单请求档案**：一个请求从到达、排队、被调度、出首 token、到结束的各时间戳与 token 计数，用来算它自己的各段延迟。
- **`IterationStats`**——**单步迭代**：一次 forward（一个 engine step）产出了几个 token、这一步的 TTFT/TPOT/ITL 是多少。引擎每前进一步产生一个。
- **`SpeculativeDecodingStats`**——**投机解码专项**：草稿了多少 token、接受了多少、每个位置的接受率。仅启用投机解码时存在（参见 u9-l2）。

关键直觉：前两者是「按需快照/按请求」，后两者是「按引擎步」。把它们分开，是因为它们的更新频率与聚合方式完全不同。

#### 4.1.2 核心流程

`SchedulerStats` 的文档字符串里画了一张「请求状态轴视图」，把一个请求的生命周期切成两段：

```
API server 侧（自服务启动起的累计计数）:
|<────────────── completed ──────────────>|<── uncompleted ──>|
|<─ success ─>|<────── fail ─────────────>|<─ routed ─>|<waiting>|
              |<cancel>|<abort>|<error>|

Engine core 侧（当前瞬时值）:
|<───── running ─────>|<──── waiting ────>|
```

- API 侧全是「自启动以来的累计计数器」，由服务层在请求进入/结束时 `+= 1`。
- Engine 侧是「此刻瞬时值」，由调度器每步用 `ScheduleMetrics`（见 `messages.py:659`）回填。

回填入口是 `update_from_schedule_metrics`，把调度器上报的「空闲块/总块」换算成 KV cache 使用率：

\[ \text{gpu\_cache\_usage} = 1 - \frac{\text{free\_blocks}}{\text{total\_blocks}} \]

而 `RequestStats` 的各段时间间隔则由属性（`@property`）懒计算，避免在每次事件时都算一遍：

\[ \text{TTFT} = \text{first\_token\_time} - \text{arrival\_time} \]
\[ \text{prefill\_interval} = \text{first\_token\_time} - \text{scheduled\_time} \]
\[ \text{decode\_interval} = \text{finish\_time} - \text{first\_token\_time} \]

> 这里 `scheduled_time` 只在「首次」被调度时记录（`if self.scheduled_time == 0.0`），刻意忽略被抢占（preempt）后又重新调度的中间时刻，以免把抢占等待时间算进 prefill。

#### 4.1.3 源码精读

`SchedulerStats` 的字段与回填逻辑——注意 API 侧用字段、Engine 侧用 `@property` 派生，`update_from_schedule_metrics` 把调度器指标翻译成本结构：
[lmdeploy/metrics/stats.py:L44-L94](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/metrics/stats.py#L44-L94)（字段与 `gpu_cache_usage` 换算在 L93）。

`IterationStats.update_from_output` 是「每步更新」的核心：第一个 token 算 TTFT，之后的 token 算 ITL/TPOT，并在请求结束时打上 `finish_time`：
[lmdeploy/metrics/stats.py:L230-L261](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/metrics/stats.py#L230-L261)。

> 注意 L237 的兜底：当用户调用 `/abort_request` 端点时，`outputs.req_metrics` 可能为 `None`，这里直接 return，避免统计崩在已取消的请求上。

`RequestStats.update_from_events` 把 `EngineEvent`（u2-l1 引入的事件机制）翻译成时间戳，事件类型 `QUEUED`/`SCHEDULED` 来自 `lmdeploy.messages.EventType`（函数内延迟导入以防循环依赖）：
[lmdeploy/metrics/stats.py:L146-L158](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/metrics/stats.py#L146-L158)。

`SpeculativeDecodingStats` 用一个 `np.ndarray` 记录「每个草稿位置」的接受数，`__repr__` 里按惯例把「平均接受长度」加 1（含奖励 token）：
[lmdeploy/metrics/stats.py:L264-L316](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/metrics/stats.py#L264-L316)。

#### 4.1.4 代码实践

**目标**：亲手构造这些统计结构，理解字段含义。

**操作步骤**（纯源码阅读型，无需 GPU）：

1. 在仓库根目录起一个 Python 解释器（已 `pip install -e .` 即可）。
2. 执行下面这段「示例代码」（非项目原有代码）：

```python
# 示例代码：手动构造统计结构，观察字段
from lmdeploy.metrics.stats import SchedulerStats, RequestStats, IterationStats
from lmdeploy.messages import ScheduleMetrics

# 模拟调度器上报：1000 个块里用了 250 个
sm = ScheduleMetrics(active_seqs=4, waiting_seqs=2,
                     total_blocks=1000, free_blocks=750,
                     prefix_cache_hit_rate=0.6)
ss = SchedulerStats()
ss.update_from_schedule_metrics(sm)
print(ss.gpu_cache_usage)   # 期望 0.25
print(ss.prefix_cache_hit_rate)  # 期望 0.6

# 模拟一个请求从到达到结束
rs = RequestStats(prompt_tokens=128)
print(rs.e2e_latency)  # 还没 finish，会是个负数或接近 0
```

3. **需要观察的现象**：`gpu_cache_usage` 是否等于 `1 - 750/1000 = 0.25`；`RequestStats` 的各 `@property` 在时间戳未填满时返回什么。

**预期结果**：`gpu_cache_usage=0.25`、`prefix_cache_hit_rate=0.6`。

#### 4.1.5 小练习与答案

**练习 1**：`SchedulerStats.num_failed_reqs` 包含哪几类？为什么它是 `@property` 而不是字段？
**答案**：包含 `cancelled + aborted + errored` 三类（见 L59-60）。用 `@property` 派生而非字段，是为了避免「写三处、读一处」时计数不一致——只保留三个原始字段为唯一真相，失败总数随时由它们相加得出。

**练习 2**：为什么 `scheduled_time` 的更新带 `if self.scheduled_time == 0.0` 的守卫？
**答案**：一个请求可能因显存不足被抢占（preempt）后重新调度，会收到多次 `SCHEDULED` 事件。守卫保证只记录第一次被调度的时间，从而 prefill 间隔不被抢占等待污染。

---

### 4.2 metrics_processor：单例编排器与两套 logger

#### 4.2.1 概念说明

`stats.py` 的结构是「死数据」，需要一个「活进程」去填它，并在合适时机交给下游 sink。`MetricsProcessor` 就是这个中枢，它有三个职责：

1. **收集**：把引擎每步的产出（`EngineOutput`）与单请求统计打包，塞进一个 `asyncio.Queue`。
2. **落账**：起一个后台协程 `_run_metrics_handler` 不断从队列取数据，更新 `RequestStats`/`IterationStats`。
3. **分发**：把更新后的统计喂给 `stat_loggers` 列表里的每一个 logger——目前有两个：`LoggingStatLogger`（打文本日志）和 `PrometheusStatLogger`（写 Prometheus 指标）。

关键设计：它是 `@singleton`（来自 `lmdeploy.pytorch.utils`），全进程唯一实例，模块底部直接 `metrics_processor = MetricsProcessor()` 暴露为模块级变量。服务层（`async_engine.py`、`api_server.py`）直接 import 这个变量用，无需传递。

为什么要「队列 + 后台任务」而不是直接在请求协程里更新？因为更新统计涉及多个对象、可能要调 logger，放在推理热路径里会拖慢 forward；扔进队列让后台协程慢慢消费，实现「统计与推理解耦」。

#### 4.2.2 核心流程

整个数据流是一条单向链：

```
请求协程 generate()
   │  每步拿到 outputs（EngineOutput）
   ▼
metrics_processor.queue_update((outputs, req_stats, iteration_stats, specdecode_stats))
   │  put_nowait 进 asyncio.Queue（非阻塞，队列满则丢，见 queue_update 的 enable 开关）
   ▼
_run_metrics_handler()（后台协程，循环 await queue.get()）
   │  1. req_stats.update_from_events(...)       # 回放事件→时间戳
   │  2. iteration_stats.update_from_output(...) # 算 TTFT/TPOT/ITL
   │  3. specdecode_stats.update_from_output(..) # 算接受率
   │  for logger in stat_loggers:
   │     logger.record_iteration(iteration_stats)
   │     if 请求结束: logger.record_finish(req_stats)
   ▼
两套 logger：
   LoggingStatLogger.log()        → 每 10 秒 print 一行汇总
   PrometheusStatLogger.record_*  → 写入 prometheus_client 的 Gauge/Counter/Histogram
   ▼
Prometheus 服务器来 /metrics 端点 scrape → Grafana 大盘
```

另外有一条独立的「调度器指标」支线：服务层每 10 秒调一次 `update_schedule_stats`，直接同步更新 `scheduler_stats` 并 `record_schedule`（不经队列，因为这类指标变化慢、量小）。

#### 4.2.3 源码精读

`MetricsProcessor` 单例与生命周期方法 `start_metrics_handler`/`stop_metrics_handler`：
[lmdeploy/metrics/metrics_processor.py:L13-L43](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/metrics/metrics_processor.py#L13-L43)（构造时不启动任务，`start` 时才建队列与协程，`stop` 时 `cancel` 并 `await` 等待退出）。

后台消费循环 `_run_metrics_handler`——这是「落账 + 分发」的核心，注意 L74 对 `FINISH` 状态才调 `record_finish`：
[lmdeploy/metrics/metrics_processor.py:L45-L82](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/metrics/metrics_processor.py#L45-L82)。

> L79-82 的 `except asyncio.CancelledError: break` 配合 `except Exception` 兜底，保证后台任务既能在 `stop` 时干净退出，又不会因单条数据异常而整个任务死掉。

队列入口 `queue_update` 与「增减请求计数」的便捷方法——`enable_metrics=False` 或队列为空时直接 return，零开销短路：
[lmdeploy/metrics/metrics_processor.py:L91-L120](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/metrics/metrics_processor.py#L91-L120)。

模块级单例导出：
[lmdeploy/metrics/metrics_processor.py:L123](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/metrics/metrics_processor.py#L123)。

logger 桥梁：`PrometheusStatLogger.record_schedule` 把 `SchedulerStats` 的字段逐个 `.set()` 到对应 Gauge——这是「字段→指标名」的映射表：
[lmdeploy/metrics/loggers.py:L378-L387](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/metrics/loggers.py#L378-L387)。

logger 的注册发生在 `AsyncEngine._build_stat_loggers`：构造两个 logger 并「反向注入」到单例 processor 的 `stat_loggers`：
[lmdeploy/serve/core/async_engine.py:L211-L227](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L211-L227)（注意 L218：TurboMind 引擎的 metrics 不支持 dp，故 `dp_rank` 恒为 0）。

每步产出的投递点在 `generate()` 主循环里：每收到一个 `outputs` 就新建一个 `IterationStats` 并 `queue_update`：
[lmdeploy/serve/core/async_engine.py:L668-L679](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L668-L679)。

#### 4.2.4 代码实践

**目标**：理解「单例 + 队列 + 后台任务」如何被服务的生命周期驱动。

**操作步骤**（源码阅读型）：

1. 打开 [api_server.py 的 lifespan 处理器](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py#L1467-L1502)，找到三件事发生的精确位置：
   - 服务启动时 `metrics_processor.start_metrics_handler(enable_metrics=True)`；
   - 每 10 秒的 `_force_log` 协程：拉 `get_schedule_metrics` → `update_schedule_stats` → `do_log_stats`；
   - 服务退出时 `stop_metrics_handler`。
2. 对照上面 4.2.2 的流程图，确认这三处分别对应「收集/落账」链路的哪一段。

**需要观察的现象**：`_force_log` 里为什么把 `update_schedule_stats`（调度器指标）放在定时循环里，而不是像 `queue_update`（迭代指标）那样在每步触发？
**预期结果**：因为调度器指标（running/waiting 请求数、KV 占用）变化慢且是瞬时快照，每 10 秒采样一次足够；而 token 级指标必须每步记录才能算准 TTFT/ITL。注释 `periodically update schedule metrics, as they change less frequently than iteration stats`（L1485）正是此意。

#### 4.2.5 小练习与答案

**练习 1**：`MetricsProcessor` 是 `@singleton`，为什么还要把 `stat_loggers` 放到 processor 里、而不是各 logger 自己去监听？
**答案**：因为 logger 的「写」必须串行化在一个后台协程里（`_run_metrics_handler`），否则多请求并发写 Prometheus 指标会乱序。把 logger 列表挂在 processor 上，processor 就成了唯一的「写入调度者」，logger 退化为被动接收者，天然线程/协程安全。

**练习 2**：如果 `enable_metrics=False`，整套机制的开销是多少？
**答案**：接近零。`queue_update`（L93）第一行就是 `if not self.enable_metrics ...: return`，请求热路径只多一次属性判断；`start_metrics_handler` 不会建队列也不会起后台任务；logger 根本不构造。这是「按需付费」的设计。

---

### 4.3 profiler：离线压测与延迟分位

#### 4.3.1 概念说明

`profiler.py` 与前两节的「在线 metrics」是两套东西，不要混淆：

- **在线 metrics**（stats/processor）：服务常驻，开销极小，回答「服务现在健康吗、利用率多少」。
- **离线 profiler**（本节）：压测脚本里用，开销无所谓，回答「这个部署在给定并发下 TTFT/吞吐是多少」。

它只服务一个场景：`benchmark/profile_throughput.py`、`benchmark/profile_pipeline_api.py`、`benchmark/benchmark_guided.py` 这三个压测脚本。它的产物是一张终端表格 + 可选的 CSV，不进 Prometheus。

核心抽象有两个：`Session` 代表「一次请求的压测记录」，`Profiler` 汇总所有 session 算整体指标。`Session.tick(n_token)` 在每次收到 token 时打一个时间戳（`time.perf_counter()` 高精度单调钟）并记录累计 token 数，最后由 `Profiler.compute_metrics()` 算出 TTFT/TPOT/ITL/E2E 及其分位数。

#### 4.3.2 核心流程

```
压测主循环（每个并发请求）:
   sess = profiler.new_session(input_len, output_len)
   for 每次从服务收到 token:
       sess.tick(累计 token 数)        # 追加 (时间戳, token 数)
   sess.finish(SUCCESS/FAIL)

profiler.start()   # 记 t_start
... 所有请求跑完 ...
profiler.finish()  # elapsed_time = now - t_start

profiler.compute_metrics():
   遍历 SUCCESS 且生成长达标的 session:
     e2e  = ts[-1] - ts[0]
     ttft = ts[1]  - ts[0]          # 首 token（ts[0] 是请求发出时刻）
     tpot = (ts[-1]-ts[1]) / (ns[-1]-ns[1])
     itl  = 相邻时间戳差（去掉首段）
   算 mean 与 P{percentages} 分位
   output_throughput = total_output / elapsed_time
   rps = success / elapsed_time

profiler.summarize(...) → 打印表格
profiler.save_csv(...)  → 追加写 CSV
```

> 一个细节：`ttft = ts[1] - ts[0]`，其中 `ts[0]` 是 `tick` 第一次被调用（请求发出）、`ts[1]` 是首 token 到达。因此 TTFT 包含了网络往返与排队时间，是「用户视角」的首 token 延迟，而非纯引擎 prefill 时间。

#### 4.3.3 源码精读

`Session` 与 `Profiler` 的骨架——`tick` 用 `time.perf_counter()` 打时间戳：
[lmdeploy/profiler.py:L9-L46](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/profiler.py#L9-L46)。

指标计算核心 `compute_metrics`，注意 L70-73 对「非流式输出」的 TPOT 特殊处理（无中间时间戳时退化为用 `ts[0]`），以及 L84-88 对空列表用 `float('inf')` 兜底防 `np.percentile` 报错：
[lmdeploy/profiler.py:L48-L103](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/profiler.py#L48-L103)。

`summarize` 的表格输出——`stream_output=False` 时不打印 TTFT/ITL（因为没有逐 token 时间戳，算不准）：
[lmdeploy/profiler.py:L105-L137](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/profiler.py#L105-L137)。

CSV 导出 `save_csv`，首次写表头、之后追加，延迟值乘 1000 转毫秒：
[lmdeploy/profiler.py:L139-L173](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/profiler.py#L139-L173)。

压测脚本的调用样例（项目原有代码），依次调 `compute_metrics()`、`summarize()`、`save_csv()`：
[benchmark/profile_throughput.py:L438-L441](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/benchmark/profile_throughput.py#L438-L441)。

> 顺带一提：PyTorch 引擎还有另一个完全不同的 profiler——[lmdeploy/pytorch/engine/model_agent/profiler.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/profiler.py)，它包的是 `torch.profiler`，用来导出 Chrome trace 做 kernel 级火焰图分析，与本讲的压测 profiler 同名不同源，别搞混。

#### 4.3.4 代码实践

**目标**：用 `Profiler` 对一个「假数据」算一遍指标，理解 TTFT/TPOT 的算法。

**操作步骤**（示例代码，无需 GPU）：

```python
# 示例代码：模拟一次流式推理的 tick 序列
from lmdeploy.profiler import Profiler, Session
import time

p = Profiler(stream_output=True, percentages=[50, 99])
p.start()
sess = p.new_session(input_len=8, req_output_len=5)
# ts[0]=请求发出, 之后每收一个 token tick 一次
sess.tick(0)                      # 发出请求
time.sleep(0.05)                  # 模拟 prefill
for i in range(1, 6):
    time.sleep(0.01)
    sess.tick(i)                  # 每生成 1 个 token 打点
sess.finish(Session.SUCCESS)
time.sleep(0.01)
p.finish()
p.compute_metrics()
p.summarize(title='Demo')
```

**需要观察的现象**：表格里 `Time to First Token (TTFT)` 是否接近 0.05s；`Time per Output Token (TPOT)` 是否接近 0.01s。

**预期结果**：TTFT ≈ 0.05s，TPOT ≈ 0.01s（因机器与调度抖动有少量偏差）。若 `stream_output=False`，TTFT/ITL 行会消失。

#### 4.3.5 小练习与答案

**练习 1**：`compute_metrics` 里为什么要 `if ns[-1] < sess.req_output_len: continue`？
**答案**：跳过「没生成够目标长度」的请求——这类请求要么提前命中 stop token、要么出错被截断，纳入统计会拉偏 TPOT/E2E。只用「完整跑完」的请求才算公平样本。

**练习 2**：`output_throughput` 与「TPOT 的倒数」有什么关系？为什么它们不相等？
**答案**：`output_throughput = total_output / elapsed_time` 是「全服务聚合吞吐」，考虑了并发——并发越高它越大；而 `1/TPOT` 是「单请求逐 token 速率」。持续批处理下，多请求并行让聚合吞吐远大于单请求速率，这正是批处理的收益体现。

---

### 4.4 monitoring：Prometheus + Grafana 部署件

#### 4.4.1 概念说明

光有 `/metrics` 端点还不够，生产里要有一套「采集 + 存储 + 可视化」的外围设施。`lmdeploy/monitoring/` 目录就是这套设施的现成配置件，让你 `docker compose up` 一条命令拉起监控栈：

- **prometheus.yaml**：告诉 Prometheus 服务器「去哪里抓、多久抓一次」。
- **docker-compose.yaml**：编排 prometheus 与 grafana 两个容器，把配置文件挂进去。
- **grafana/**：Grafana 的数据源（指向 Prometheus）与仪表盘 JSON（lmdeploy 官方预制的大盘）。

lmdeploy 这套监控栈借鉴自 sglang（见 docker-compose.yaml 顶部注释），是社区常见做法。

#### 4.4.2 核心流程

```
┌─────────────┐  scrape /metrics (每 5s)   ┌────────────────────┐
│  lmdeploy   │ ◄────────────────────────  │   Prometheus       │
│  api_server │   (prometheus_client 暴露)  │  (时序数据库)       │
│  :23333     │                            │  :9090             │
└─────────────┘                            └─────────┬──────────┘
                                                     │ 查询 PromQL
                                                     ▼
                                            ┌────────────────────┐
                                            │   Grafana 大盘     │
                                            │  :3000             │
                                            └────────────────────┘
```

抓取目标默认指向 `127.0.0.1:23333`——这正是 `serve api_server` 的默认端口（u8-l2 已建立）。`scrape_interval: 5s` 意味着每 5 秒抓一次，足以捕捉 TTFT 这种秒级指标。

Grafana 容器通过环境变量 `GF_AUTH_ANONYMOUS_ENABLED=true` 开启免登录 Viewer 访问，并把 `lmdeploy-dashboard.json` 设为首页大盘，开箱即用。

#### 4.4.3 源码精读

Prometheus 抓取配置——`job_name: lmdeploy`、目标 `127.0.0.1:23333`、5 秒抓一次：
[lmdeploy/monitoring/prometheus.yaml:L1-L11](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/monitoring/prometheus.yaml#L1-L11)。

docker-compose 编排——prometheus 与 grafana 都用 `network_mode: host`（简化容器与宿主机服务的互通），grafana 挂载三处卷：数据源、仪表盘配置、仪表盘 JSON：
[lmdeploy/monitoring/docker-compose.yaml:L1-L30](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/monitoring/docker-compose.yaml#L1-L30)。

Grafana 数据源声明——指向本地 Prometheus，设为默认：
[lmdeploy/monitoring/grafana/datasources/datasource.yaml:L1-L9](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/monitoring/grafana/datasources/datasource.yaml#L1-L9)。

仪表盘自动加载配置——从 `/var/lib/grafana/dashboards` 目录读 JSON，每 10 秒刷新：
[lmdeploy/monitoring/grafana/dashboards/config/dashboard.yaml:L1-L12](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/monitoring/grafana/dashboards/config/dashboard.yaml#L1-L12)。

服务侧挂载 `/metrics` 端点——`mount_metrics` 用 `make_asgi_app(registry)` 把 prometheus_client 的全局 registry 包成 ASGI 应用，并改 `path_regex` 绕过 FastAPI 对 `/metrics` 的 307 重定向：
[lmdeploy/serve/openai/api_server.py:L1452-L1464](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py#L1452-L1464)。

> 启用前提：引擎配置的 `enable_metrics=True`（`PytorchEngineConfig` 与 `TurbomindEngineConfig` 默认就是 `True`，见 `messages.py:315` 与 `messages.py:461`）。`mount_metrics` 第一行若发现 `enable_metrics=False` 就直接 return，不挂路由。

#### 4.4.4 代码实践

**目标**：把监控栈跑起来，从 `/metrics` 端点读到真实指标。

**操作步骤**（需 GPU + Docker，部分待本地验证）：

1. 启动带 metrics 的服务（CLI 经 `--metrics` 透传 `enable_metrics`）：
   ```bash
   lmdeploy serve api_server <model_path> --metrics
   ```
2. 另起终端，curl 抓一次指标：
   ```bash
   curl http://127.0.0.1:23333/metrics | grep -E '^lmdeploy:(num_requests_running|gpu_cache_usage_perc|generation_tokens_total)'
   ```
3. （可选）拉起监控栈并打开 Grafana：
   ```bash
   cd lmdeploy/monitoring && docker compose up -d
   # 浏览器访问 http://localhost:3000（免登录 Viewer）
   ```

**需要观察的现象**：步骤 2 应能看到形如以下的行：
```
lmdeploy:num_requests_running{engine="0",model_name="..."} 0
lmdeploy:gpu_cache_usage_perc{engine="0",model_name="..."} 0.0
lmdeploy:generation_tokens_total{engine="0",model_name="..."} 0
```
发一次推理请求后，`generation_tokens_total`（Counter）单调递增，`num_requests_running`（Gauge）先升后降。

**预期结果**：3 个关键指标——
- `lmdeploy:num_requests_running`：当前 running 序列数（Gauge）；
- `lmdeploy:gpu_cache_usage_perc`：KV cache 占用率（Gauge，0~1）；
- `lmdeploy:generation_tokens_total`：累计生成 token 数（Counter，只增）。

> 若本地无 GPU，可只做源码侧练习：在 [loggers.py 的 PrometheusStatLogger 构造函数](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/metrics/loggers.py#L131-L376) 里数出 Gauge/Counter/Histogram 各有几个，作为「指标目录」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `num_requests_running` 用 Gauge 而 `generation_tokens_total` 用 Counter？
**答案**：running 请求数会随请求进出上下浮动（可增可减），必须用 Gauge；而「累计生成 token 数」是单调递增的总量，用 Counter 才能在 Prometheus 里用 `rate()` 函数算「每秒生成速率」，Counter 重启才归零。

**练习 2**：`prometheus.yaml` 里目标写死 `127.0.0.1:23333`，部署到别的机器要改什么？
**答案**：要么改 `prometheus.yaml` 里的 `targets` 为远端 lmdeploy 服务地址，要么把 Prometheus 容器与 lmdeploy 部署在同一网络。由于 compose 用了 `network_mode: host`，Prometheus 直接用宿主机网络栈访问 `127.0.0.1`，所以最简单的是「Prometheus 与 lmdeploy 跑在同一台宿主机」。

---

## 5. 综合实践：从源码打包一个带监控的服务

把本讲的「监控」与「构建发布」串起来，完成一个端到端任务。

### 任务一：阅读 setup.py，说清 TurboMind 扩展如何进 wheel

打开 [setup.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py)，回答以下问题（答案直接对照源码）：

1. **编译开关**：在 [L134](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L134)，条件是「设备为 cuda」且「未设 `DISABLE_TURBOMIND`」。两个条件任一不满足，`ext_modules=[]`，wheel 里就没有 `_turbomind`。
2. **扩展声明**：[L137-L156](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L137-L156) 用 `cmake_build_extension.CMakeExtension` 声明一个名为 `_turbomind` 的扩展，`install_prefix='lmdeploy/lib'` 决定编译产物最终落进 wheel 的 `lmdeploy/lib/` 目录（即 u6-l1 提到的 `_tm` pybind 扩展位置），`source_dir` 指向仓库根（CMakeLists.txt 所在）。
3. **构建器**：[L158](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L158) 把 `build_ext` 命令替换为 `cmake_build_extension.BuildExtension`，于是 `pip install` 时走的是 CMake 构建（u1-l3 已讲过的 FetchContent 拉 cutlass 等、按 CUDA 版本选 GPU 算力），而非默认的「单文件编译」。
4. **运行时依赖**：[L157](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L157) 的 `get_turbomind_deps()` 按检测到的 CUDA 大版本（[L35-L55](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L35-L55)）追加 `nvidia-nccl-cuXX`、`nvidia-cublas-cuXX` 等 CUDA 库到 `install_requires`，CUDA≥13 走无后缀的 `nvidia-*` 包名。
5. **打包元信息**：[L164-L195](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/setup.py#L164-L195) 的 `setup()` 把 `ext_modules` 与 `cmdclass` 传进去，并用 `entry_points` 注册 `lmdeploy` 命令行（u1-l5）。

**一句话总结**：`setup.py` 在「cuda 且未禁用」时，用 `CMakeExtension`（产物落 `lmdeploy/lib`）+ `BuildExtension`（走 CMake）+ `get_turbomind_deps`（带 CUDA 库）三件套，把 TurboMind 的 C++/CUDA 编译产物 `_turbomind.so` 与 NCCL/cuBLAS 依赖一起打进 wheel；否则只打纯 Python 包。

### 任务二：构建并验证（待本地验证，需 CUDA 环境）

```bash
# 1. 构建带 TurboMind 的 wheel（默认）
pip install -e .

# 2. 验证 C++ 扩展存在
python -c "from lmdeploy.lib import _turbomind; print('TurboMind ext OK')"

# 3. 对照：禁用 TurboMind 重装，验证扩展可缺席
DISABLE_TURBOMIND=1 pip install -e .
python -c "import lmdeploy; print('pytorch-only OK')"
# 此时 lmdeploy/lib 下不会有 _turbomind.so
```

**预期**：步骤 2 在默认安装下成功；步骤 3 在 `DISABLE_TURBOMIND=1` 下 `lmdeploy/lib` 无 `.so`，但 `import lmdeploy` 仍正常（PyTorch 后端不依赖它）。

### 任务三：启动服务并接入监控（待本地验证）

```bash
# 启动带 metrics 的服务
lmdeploy serve api_server <model_path> --metrics

# 另一终端，发几个请求制造流量后抓指标
curl -s http://127.0.0.1:23333/metrics | grep -E '^lmdeploy:'
# 拉起监控栈
cd lmdeploy/monitoring && docker compose up -d
# 访问 http://localhost:3000 看大盘
```

**串联验证**：你在 Grafana 看到的每条曲线，都能在 4.1 的 stats 结构 → 4.2 的 processor/logger → 4.4 的端点这条链上找到对应源头。例如「KV cache 使用率」面板 = `SchedulerStats.gpu_cache_usage` → `gauge_gpu_cache_usage.set(...)` → `/metrics` 里的 `lmdeploy:gpu_cache_usage_perc` → Grafana。

## 6. 本讲小结

- lmdeploy 的可观测性分两层：**在线 metrics**（`metrics/`，服务常驻、轻量）与**离线 profiler**（`profiler.py`，压测专用、重型），二者同名为「profiler/metrics」但职责完全不同。
- `stats.py` 用四个 dataclass 切分视角：`SchedulerStats`（全局快照）、`RequestStats`（单请求档案）、`IterationStats`（单步迭代）、`SpeculativeDecodingStats`（投机解码专项），它们是纯数据、不做 IO。
- `MetricsProcessor` 是 `@singleton` 单例，用「异步队列 + 后台 `_run_metrics_handler` 协程」把引擎产出解耦落账，再分发给 `LoggingStatLogger`（文本）与 `PrometheusStatLogger`（指标）两套 logger；`enable_metrics=False` 时零开销短路。
- 服务层在 lifespan 周期里驱动这套机制：启动起 handler、每 10 秒拉调度器指标与刷日志、退出停 handler；`/metrics` 端点由 `mount_metrics` 用 prometheus_client 的 ASGI app 挂载。
- `monitoring/` 提供开箱即用的 prometheus + grafana 配置，`docker compose up` 即可拉起监控栈，抓取目标默认指向 `:23333`。
- 构建发布由 `setup.py` 完成：在「cuda 且未禁用」时用 `CMakeExtension` + `BuildExtension` + `get_turbomind_deps` 把 TurboMind C++ 扩展打进 wheel 的 `lmdeploy/lib/`，否则只打纯 Python 包。

## 7. 下一步学习建议

- **回看 u9-l2（投机解码）**：本讲的 `SpeculativeDecodingStats` 与 loggers 里的 spec 解码计数器（`spec_decode_num_accepted_tokens_total` 等）正是度量投机解码收益的尺子，对照阅读能打通「机制—度量」闭环。
- **延伸阅读 `lmdeploy/serve/core/health.py` 与 u8-l3**：健康检查（`/health`）与 metrics 是生产可观测性的两条腿，前者回答「能不能用」、后者回答「用得怎么样」，建议一起读。
- **动手扩展一个自定义指标**：尝试在 `PrometheusStatLogger` 里加一个 `Counter`，并在 `record_iteration` 里 `.inc()`，重新打包（按综合实践任务二的流程）后从 `/metrics` 抓到它——这会逼你走通「stats → logger → 端点 → wheel」全链路。
- **下一讲 u10-l1（或回顾 u10 系列其余篇）**：本系列 U10 围绕二次开发与运维收尾；若要新增模型，可结合本讲的 profiler 做「接入前后的性能回归」，用数据验证重写是否真的更快。

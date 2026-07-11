# 可观测性体系

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 LMCache 中**两套并存的可观测体系**各自的适用场景：单进程引擎的 `lmcache/observability.py`（Pull 模型）与多进程（MP）模式的 `lmcache/v1/mp_observability/`（事件驱动 Push 模型）。
- 把 KV cache 的可观测指标归类为四大类：**健康类（health）**、**前缀命中类（prefix / lookup hit）**、**token 级吞吐**、**请求级时延与生命周期**。
- 解释 MP 模式的**事件总线 + 发布/订阅**模型：`Event` → `EventBus` 队列 → drain 线程 → 订阅者回调。
- 说明指标如何落盘到 Prometheus/Grafana：OTLP push（生产）与 Prometheus pull（开发调试）两条链路。
- 了解 **OpenTelemetry（OTel）追踪**与 `@enable_tracing` trace 记录子系统的差异与各自用途。

## 2. 前置知识

- **指标（metric）的三种基本类型**：
  - **Counter（计数器）**：只增不减，例如「累计命中 token 数」。Prometheus 用 `rate(...[5m])` 求增速。
  - **Gauge（仪表）**：可升可降的当前值，例如「当前 L1 缓存占用字节」。
  - **Histogram（直方图）**：把观测值分桶统计分布，例如「retrieve 延迟分布」。
- **命中率（hit rate）**：\(\text{hit rate} = \dfrac{\text{命中 token 数}}{\text{请求 token 数}}\)。LMCache 里要区分 **lookup 命中率**（只查不取，前缀命中 token 数 / 查询 token 数）与 **retrieve 命中率**（实际取回 token 数 / 请求 token 数），二者口径不同。
- **Prometheus 多进程模式**：当多个进程（如多个 vLLM worker）都要写指标时，需要设置 `PROMETHEUS_MULTIPROC_DIR` 环境变量，让每个进程把指标写到同一目录再聚合。
- **OpenTelemetry（OTel）**：厂商中立的遥测标准。它把「指标/追踪的产生（Meter/Tracer）」与「导出（Exporter）」解耦，导出端可以是 OTLP（推给 collector）、Prometheus（拉）等。
- **事件驱动（event-driven）**：生产者在热路径只做「往队列塞一个事件」，重活（算指标、写日志、建 span）交给后台线程的订阅者，从而不阻塞 GPU 关键路径。这正是 MP 可观测体系的核心思想。

如果你已学过 [u1-l4 进程入口](u1-l4-entry-points.md)（知道 `lmcache_server` / coordinator 等进程入口）和 [u1-l6 LMCacheEngine 公共 API](u1-l6-engine-public-api.md)（store/retrieve/lookup 三条主链路），本讲的指标来源就很容易对号入座。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `lmcache/observability.py` | **单进程引擎**的可观测模块：`LMCStatsMonitor`（区间计数器）+ `PrometheusLogger`（注册 `lmcache:` 前缀的 Prometheus 指标）+ `LMCacheStatsLogger`（后台线程定期把快照推给 Prometheus）。 |
| `lmcache/v1/mp_observability/event.py` | MP 体系的事件模型：`EventType` 枚举与 `Event` 数据类。 |
| `lmcache/v1/mp_observability/event_bus.py` | MP 体系的**事件总线**：队列 + drain 线程 + 订阅者分发，以及 `EventSubscriber` 基类。 |
| `lmcache/v1/mp_observability/otel_init.py` | OTel SDK 初始化：OTLP push 与 Prometheus pull 两模式，`register_gauge` 辅助函数。 |
| `lmcache/v1/mp_observability/config.py` | `ObservabilityConfig` 配置、CLI 参数、唯一入口 `init_observability()`（建 provider→建 bus→注册订阅者）。 |
| `lmcache/v1/mp_observability/subscribers/metrics/lookup.py` | 一个典型的 **metrics 订阅者**：从 `MP_LOOKUP_PREFETCH_END` 事件聚合出 L1+L2 命中率。 |
| `lmcache/v1/mp_observability/subscribers/tracing/mp_server.py` | **tracing 订阅者**：把 START/END 事件对还原成 OTel span。 |
| `lmcache/v1/mp_observability/trace/decorator.py` | `@enable_tracing` 装饰器，发布 `TRACE_CALL` 事件用于**离线回放**型 trace 记录。 |
| `lmcache/v1/multiprocess/server.py` | MP server 启动处，把 `--instance-id` 投影成 OTel `service.instance.id` 并调用 `init_observability()`。 |
| `lmcache/v1/cache_engine.py` | 单进程引擎在 `LMCacheEngineBuilder` 里创建 `LMCacheStatsLogger`（`log_interval=10` 秒）。 |
| `docs/design/v1/mp_observability/README.md` / `METRICS.md` / `EVENTS.md` | MP 可观测的架构总览、指标清单、事件元数据契约（镜像 `lmcache/` 包树，读代码前先读设计文档）。 |
| `examples/observability/` | 端到端示例栈：`docker-compose.yml`（collector + Tempo + Prometheus + Grafana）、`start-server.sh`。 |

## 4. 核心概念与源码讲解

### 4.1 两套可观测体系总览与指标分类

#### 4.1.1 概念说明

LMCache 目前有**两套独立的可观测体系**，理解它们的关系是本讲的总纲：

1. **单进程引擎体系**（`lmcache/observability.py`）：服务于 `LMCacheEngine`（即引擎和 cache 在**同一进程**内运行的场景，也是 legacy / 非 MP 路径）。它直接用 `prometheus_client` 注册指标，命名空间是 `lmcache:`（Prometheus 自动转成 `lmcache_`）。采用 **Pull 模型**——Prometheus 定时来 `/metrics` 抓取。

2. **多进程（MP）体系**（`lmcache/v1/mp_observability/`）：服务于 [u3-l1 多进程架构](u3-l1-mp-architecture-overview.md) 中那个独立的 cache daemon。命名空间是 `lmcache_mp.`（mp = multiprocess）。采用 **事件驱动 + Push 模型**——生产者只发事件，订阅者算指标，再通过 OTLP 推给 collector。

> **为什么要两套？** MP 模式下 cache 跑在独立进程，要观察的是「跨进程的 store/retrieve/lookup/prefetch/eviction 流」，且不能在 GPU 热路径上做重活。事件驱动天然适合这种「解耦 + 异步」需求。而单进程引擎沿用早先的 Prometheus 直出方式，简单直接。两者的指标前缀（`lmcache_` vs `lmcache_mp_`）不同，所以在 Grafana 里能一眼区分。

无论哪套，KV cache 的可观测指标都可归为四大类：

| 类别 | 回答的问题 | 代表指标（单进程） | 代表指标（MP） |
|---|---|---|---|
| **健康 health** | 服务活着吗？后端通吗？内存够吗？ | `lmcache:lmcache_is_healthy`、`local_cpu_evict_count`、ping 延迟 | `lmcache_mp.l1_allocation_failure`、`l1_read_failure`、`timeouts`、`event_bus.queue_depth` |
| **前缀命中 prefix hit** | 缓存到底省了多少 prefill？ | `lmcache:retrieve_hit_rate`、`lookup_hit_rate` | `lmcache_mp.lookup_hit` / `lookup_requested`（比值即命中率） |
| **token 级吞吐** | 读写速度有多快？ | `lmcache:retrieve_speed`、`store_speed`、`remote_read_bytes` | `lmcache_mp.l0_l1_store_throughput`、`l2_store_throughput`（GB/s） |
| **请求级时延/生命周期** | 单个请求多快？chunk 活多久？ | `lmcache:time_to_retrieve`、`request_cache_lifespan` | `lmcache_mp.l1_chunk_lifetime`、OTel `request` span 的 `hit_rate` 属性 |

#### 4.1.2 核心流程

```
        单进程引擎 (lmcache: namespace)            多进程 MP (lmcache_mp. namespace)
        ──────────────────────────────            ────────────────────────────────
  cache_engine 调 on_lookup_request 等           生产者(L1Manager/StorageManager/MPCacheServer)
        │                                              │ bus.publish(Event)
        ▼                                              ▼
  LMCStatsMonitor (区间累加)                      EventBus (deque 队列 + drain 线程)
        │ get_stats_and_clear() 每10s                  │ 分发给订阅者
        ▼                                              ├──► metrics 订阅者 → OTel counter
  LMCacheStats (快照)                                ├──► logging 订阅者 → logger.debug
        │ log_prometheus()                             └──► tracing 订阅者 → OTel span
        ▼                                              │
  prometheus_client Counter/Gauge/Histogram          ▼
  (lmcache: 前缀, 带 PROMETHEUS_MULTIPROC_DIR)     OTel MeterProvider/TracerProvider
        │                                              ├── OTLP push → collector → Prometheus/Tempo
        ▼                                              └── Prometheus pull fallback → /metrics
  Prometheus pull → /metrics
```

#### 4.1.3 源码精读

两套体系的命名空间约定，单进程见 `PrometheusLogger` 创建计数器时一律用 `lmcache:` 前缀：

[observability.py:935-951](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/observability.py#L935-L951) — 创建 `lmcache:num_retrieve_requests` / `num_store_requests` / `num_lookup_requests` 等计数器，文档串即指标含义。

MP 体系的命名空间约定见设计文档 METRICS.md 开头：

[METRICS.md:17-21](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/v1/mp_observability/METRICS.md#L17-L21) — 明确说明所有 MP 指标用 `lmcache_mp.` 前缀，与引擎的 `lmcache.` 命名空间区分；Prometheus 里 `.` 转 `_`、Counter 加 `_total` 后缀。

#### 4.1.4 代码实践

**实践目标**：建立「文件 → 命名空间 → 体系」的对应表。

**操作步骤**：

1. 在 `lmcache/observability.py` 中搜索 `lmcache:`，统计有多少个 Counter、Gauge、Histogram。
2. 在 `docs/design/v1/mp_observability/METRICS.md` 中搜索 `lmcache_mp.`，看 MP 体系有多少类指标。
3. 打开 Grafana 示例 dashboard（`examples/observability/grafana/provisioning/dashboards/lmcache.json`），看它查询的是 `lmcache_` 还是 `lmcache_mp_` 系列。

**需要观察的现象**：两套命名空间互不重叠；同一概念（如命中率）在两套里名字不同（`lmcache:retrieve_hit_rate` vs `lmcache_mp_lookup_hit_tokens_total` / `..._requested_tokens_total`）。

**预期结果**：你能用一句话回答「为什么 Grafana 上同时看到 `lmcache_` 和 `lmcache_mp_` 开头的指标」——因为前者来自引擎进程内观测，后者来自 MP cache daemon。

---

### 4.2 单进程引擎指标体系（observability.py）

#### 4.2.1 概念说明

这套体系由三个角色组成：

- **`LMCStatsMonitor`**（生产者侧累加器，单例）：引擎的 store/retrieve/lookup 主链路在关键节点调用它的 `on_*` 方法，把「这一段时间内」的计数（请求数、命中 token 数、淘汰次数……）累加进去。它**不直接写 Prometheus**，只维护内存里的区间值。
- **`LMCacheStats`**（快照）：每隔固定周期，`get_stats_and_clear()` 把区间值打包成一个不可变快照返回，并把内部累加器**清零**——所以它度量的是「上次清零到现在的增量」。
- **`PrometheusLogger`**（消费者侧）：持有所有 `prometheus_client` 的 Counter/Gauge/Histogram，`log_prometheus(stats)` 把快照里的增量灌进对应的 Prometheus 指标。
- **`LMCacheStatsLogger`**（调度器）：一个守护线程，周期性执行 `get_stats_and_clear() → log_prometheus()`。

这套设计的精髓是**区间增量模型**：累加器记增量、Prometheus Counter 本身又是单调递增的，两者叠加后 Prometheus 端看到的是「自进程启动以来的累计值」（符合 Counter 语义），而 `get_stats_and_clear` 的「清零」只影响下一次要上报多少增量。

#### 4.2.2 核心流程

```
on_retrieve_request(num_tokens)        # 引擎进入 retrieve
  → interval_retrieve_requests += 1
  → interval_requested_tokens += num_tokens
  → 返回一个 RetrieveRequestStats（带 request_id、start_time）
... retrieve 执行 ...
on_retrieve_finished(stats, num_hit)   # retrieve 结束
  → stats.end_time = now
  → interval_hit_tokens += num_hit
  → 若超阈值，rate-limited 打 warning
───────────── 每 10 秒 ─────────────
LMCacheStatsLogger.log_worker():
  stats = monitor.get_stats_and_clear()   # 算命中率 + 打包 + 清零
  prometheus_logger.log_prometheus(stats) # counter.inc(增量) / gauge.set(当前值) / histogram.observe(分布)
```

命中率的计算发生在快照时刻（不是单次请求），见 `get_stats_and_clear`：

\[
\text{retrieve\_hit\_rate} = \frac{\sum_{\text{finished}} \text{hit\_tokens}}{\sum_{\text{finished}} \text{requested\_tokens}}
\]

#### 4.2.3 源码精读

`LMCStatsMonitor` 是单例，`on_*` 方法都用 `@thread_safe` 装饰（多 worker 线程并发调用安全）：

[observability.py:376-396](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/observability.py#L376-L396) — `on_retrieve_request` 创建 `RetrieveRequestStats`、累加请求数与请求 token 数，并把它设为「当前进行中的 retrieve」（供后续细粒度 profile 用）。

[observability.py:398-417](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/observability.py#L398-L417) — `on_retrieve_finished` 记录命中 token 数、判断是否「慢 retrieve」（按绝对耗时或按 token/s 速度两个阈值）。

区间命中率在快照时计算（注意：没有完成的请求会一直留在 dict 里直到完成）：

[observability.py:665-701](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/observability.py#L665-L701) — `get_stats_and_clear` 里 retrieve / lookup 命中率的计算逻辑，分母为 0 时 retrieve 命中率取 1、lookup 命中率取 0。

`PrometheusLogger` 的多进程支持——这是单进程体系能用于「多 vLLM worker」的关键：

[observability.py:909-923](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/observability.py#L909-L923) — `_ensure_multiprocess_dir()` 在任何指标注册**之前**确保 `PROMETHEUS_MULTIPROC_DIR` 已设置（默认 `/tmp/lmcache_prometheus`），否则多进程指标会丢。

健康类指标的典型例子是一个**动态 gauge**（用回调取值，而非事件驱动）：

[observability.py:1530-1535](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/observability.py#L1530-L1535) — 注册 `lmcache:lmcache_is_healthy` 这个 gauge（`1=健康, 0=不健康`），属于 `_dynamic_metrics` 里通过 lambda 实时取值的一类。

调度线程把三者串起来：

[observability.py:1954-1982](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/observability.py#L1954-L1982) — `LMCacheStatsLogger.log_worker()` 的主循环：`get_stats_and_clear() → log_prometheus() → 用 shutdown_event.wait(log_interval)`（用 Event 而非 `time.sleep`，便于优雅关停时立即唤醒）。

引擎里在哪创建这个 logger（`log_interval=10` 秒）：

[cache_engine.py:2114-2118](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/cache_engine.py#L2114-L2118) — `LMCacheEngineBuilder.get_or_create` 在创建引擎实例时一并起一个 10 秒周期的 stats logger。

#### 4.2.4 代码实践

**实践目标**：跟踪「一次 retrieve 从被观察到变成 Prometheus 指标」的完整链路。

**操作步骤**：

1. 在 `cache_engine.py` 中搜索 `on_retrieve_request` / `on_retrieve_finished`，确认它们在 `LMCacheEngine.retrieve` 主链路里被调用（生产者侧）。
2. 在 `observability.py` 的 `log_prometheus`（[L1693](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/observability.py#L1693) 起）里找到 `interval_retrieve_requests` 灌进 `counter_num_retrieve_requests` 的那一行。
3. 注意 `get_stats_and_clear` 末尾 `self._clear()`（[L826](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/observability.py#L826)）——它清零区间累加器，但**不清** Prometheus Counter（Counter 是进程级单调的）。

**需要观察的现象**：累加器 `interval_*` 每次上报后归零；Prometheus 端 `lmcache_num_retrieve_requests_total` 持续上升。

**预期结果**：你能画出 `engine.retrieve → on_retrieve_* → interval 累加 → 10s 快照 → counter.inc → Prometheus` 的时序，并解释「为什么累加器要清零而 Counter 不清零」。

---

### 4.3 MP 事件总线与发布/订阅模型

#### 4.3.1 概念说明

MP 体系的核心是一个**事件总线（EventBus）**，它把「产生观测数据」和「消费观测数据」彻底解耦：

- **生产者**：`L1Manager`、`StorageManager`、`MPCacheServer` 等组件，在关键节点 `bus.publish(Event(...))`。
- **事件（Event）**：一个轻量 dataclass，只有四个字段：`event_type`（枚举）、`timestamp`（总线在 `publish` 时盖的墙钟）、`metadata`（扁平 key-value 负载）、`session_id`（关联 START/END 对用）。
- **EventBus**：内部一个 `collections.deque` 当队列 + 一个后台 drain 线程。`publish` 是**非阻塞热路径**——只往队尾塞、置 wake 信号就返回；真正的分发在 drain 线程做。
- **订阅者（EventSubscriber）**：声明「我关心哪些 EventType」，总线把对应事件回调给它。订阅者分三类，分别落在 `subscribers/metrics/`、`subscribers/logging/`、`subscribers/tracing/`。

这套设计让**生产者几乎零成本**（一次 append），重活（算指标、写 OTel）全在后台；即便某个订阅者抛异常，也只记录计数、不影响其他订阅者和生产者（容错）。

两个工程要点：

1. **背压策略：尾丢弃（tail-drop）**。队列满（默认 10000）时，新事件被静默丢弃，只打 rate-limited warning——宁可丢观测数据，也不阻塞 GPU 热路径。配套有「EventBus 自健康」指标（队列深度、drain 滞后、丢弃数、订阅者异常数）。
2. **CUDA 精确计时**：GPU 上的 store/retrieve 完成时刻 ≠ Python 调用时刻。`publish_on_stream` 把事件记录**排到 CUDA stream 上**当 host 回调，由 C++ 内核 `record_event_on_stream` 在 GPU 真正执行到那一步时盖时间戳，避免「CUDA driver 等 GIL」的死锁。

#### 4.3.2 核心流程

```
生产者热路径:
  bus.publish(Event(event_type=L1_READ_FINISHED, metadata={...}))
     │ 队列未满 → event.timestamp = time.time(); queue.append(event); wake.set()
     │ 队列已满 → _discard_count += 1; rate-limited warning; return（丢弃）
     ▼
drain 线程 (_run → _drain_all):
  while 事件队列非空:
     event = queue.popleft()
     for cb in subscribers[event.event_type]:
        try: cb(event)
        except: 记 subscriber_exception_counts; 继续（不传染）
```

CUDA 路径额外一步：`publish_on_stream` 先尝试 C++ `record_event_on_stream`（无 GIL），drain 时再从 `_lmc_ops.drain_recorded_events()` 把 C++ 缓存的事件搬进 Python 队列。

#### 4.3.3 源码精读

`EventType` 枚举约定命名 `<组件>_<操作>[_<阶段>]`，覆盖 L1/L2/SM/MP server/CB/trace 全链路：

[event.py:14-141](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/event.py#L14-L141) — 全部事件类型定义，例如 `L1_READ_FINISHED`、`MP_STORE_START/END`、`MP_LOOKUP_PREFETCH_END`、`TIMEOUT_RAISED`、`TRACE_CALL`、`CB_*`（CacheBlend）。

`Event` dataclass 的 `timestamp` 由总线在 publish 时盖戳（注释强调「不是 drain 线程处理时刻」），这对 CUDA host-callback 事件至关重要：

[event.py:144-163](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/event.py#L144-L163) — `Event` 四字段定义及 `timestamp` 语义说明。

`EventBus.publish` 的热路径（非阻塞 + 尾丢弃）：

[event_bus.py:196-221](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/event_bus.py#L196-L221) — 队列满时丢弃并打 rate-limited warning（每秒至多一次）；否则盖时间戳、append、wake。

CUDA stream 上的精确计时入口：

[event_bus.py:167-194](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/event_bus.py#L167-L194) — `publish_on_stream` 优先用 C++ `record_event_on_stream`（避免 GIL），把 metadata 拆成 str/int 两个 dict 传给内核；无原生 recorder 时退回 `stream.launch_host_func`。

drain 线程的分发与容错（一个订阅者挂了不影响其他）：

[event_bus.py:312-354](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/event_bus.py#L312-L354) — `_drain_all` 先把 C++ 缓存事件并入队列，再逐个 `popleft` 分发；每个回调 `try/except`，异常按订阅者类名计入 `_subscriber_exception_counts`。

EventBus 自身健康用 OTel observable gauge 暴露（pull 时回调取值）：

[event_bus.py:124-141](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/event_bus.py#L124-L141) — 在 `__init__` 里注册 `lmcache_mp.event_bus.queue_depth` 与 `drain_lag_seconds` 两个自监控 gauge。

全局单例与开关：

[event_bus.py:361-388](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/event_bus.py#L361-L388) — `_global_bus` 默认 `enabled=False`（import 时几乎零开销），`get_event_bus()` 取单例，`init_event_bus(config)` 替换并翻转 `_observability_enabled` 全局开关；`is_observability_enabled()` 供热路径快速判断要不要构造事件。

#### 4.3.4 代码实践

**实践目标**：跟踪一个 `L1_READ_FINISHED` 事件从发布到分发的全过程。

**操作步骤**：

1. 在仓库中 `grep -rn "L1_READ_FINISHED"` 找到它的**发布点**（生产者，通常在 `L1Manager` 相关代码）。
2. 在 `subscribers/metrics/l1.py` 中找到订阅它的回调，确认回调里做了 `counter.add(len(keys))` 之类的动作。
3. 对照 `docs/design/v1/mp_observability/EVENTS.md` 的 `L1_READ_FINISHED` 行，确认其 metadata 契约（`keys: list[ObjectKey]`）。

**需要观察的现象**：一个事件可被**多个订阅者**消费（如同时被 metrics 订阅者和 logging 订阅者）；事件类型名字里的「.` 分隔命名」会原样出现在 OTel 指标命名空间里。

**预期结果**：你能画出「`L1Manager.read → publish(L1_READ_FINISHED) → 队列 → drain → L1MetricsSubscriber._on_read_finished → lmcache_mp.l1_read counter.add`」的链路。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `publish` 要做成「队列满就丢弃」而不是「阻塞等待」？

> **答案**：`publish` 在 store/retrieve 的 GPU 热路径上，阻塞会让推理 worker 卡住，违背「no fate-sharing」（不与引擎共命运）原则。观测数据丢一点可以接受，卡住推理不能接受。丢弃数有指标（`event_bus.dropped_events_total`）可观测，运维能据此调大 `max_queue_size`。

**练习 2**：`Event.timestamp` 为什么要在 `publish` 时盖、而不是 drain 线程处理时盖？

> **答案**：drain 线程可能滞后（队列堆积、GIL 竞争）。盖在 publish 时（或更精确地，CUDA host 回调里盖）反映事件**真实发生时刻**，这样算出来的 retrieve 延迟、吞吐才准。盖在 drain 时会把「drain 滞后」误计入业务耗时。

---

### 4.4 MP 指标子系统与 OTel 导出

#### 4.4.1 概念说明

MP 的 metrics 订阅者把事件聚合成 **OpenTelemetry 指标**，再由 OTel SDK 导出。OTel 在这里充当「指标的中立中间层」——你写的是 `meter.create_counter("lmcache_mp.xxx")`，至于它最终去 Prometheus 还是去某个 collector，由 SDK 配置决定，订阅者代码不用关心。

两条导出链路（由 `ObservabilityConfig.otlp_endpoint` 是否设置决定）：

| `otlp_endpoint` | 模式 | 链路 |
|---|---|---|
| `http://host:4317` | **OTLP push**（生产） | LMCache → OTLP gRPC → OTel Collector → Prometheus（或别的后端） |
| `None` | **Prometheus pull**（开发调试） | LMCache 进程内起一个 `prometheus_client` HTTP server，Prometheus 来 `/metrics` 抓 |

`init_observability()` 是**唯一入口**：先建 OTel provider（保证订阅者 `__init__` 里的 `get_meter()` 能绑到真 provider），再建 EventBus，再按 config 开关注册三类订阅者，最后 `bus.start()`。这里有个**顺序约束**：provider 必须在订阅者之前就绪，否则订阅者模块级的 `get_meter()` 会绑到空 provider——这也是为什么订阅者把 `get_meter()` 写在 `__init__` 而非模块顶层。

**Resource 属性**：每个指标都带进程级身份 `service.instance.id`（MP 模式下投影自 `--instance-id`），用于在多实例部署里区分「这是哪个 cache daemon 的指标」。

#### 4.4.2 核心流程

```
init_observability(config):
  1. 确定 service.instance.id（config 或随机 UUID）
  2. 若 enabled & metrics_enabled → init_otel_metrics()  ← 建 MeterProvider
  3. 若 enabled & tracing_enabled  → init_otel_tracing()  ← 建 TracerProvider
  4. bus = init_event_bus(...)
  5. 若 metrics_enabled → 注册 ~15 个 metrics 订阅者（L1/L2/lookup/throughput/...）
  6. 若 logging_enabled → 注册 logging 订阅者
  7. 若 tracing_enabled → 注册 tracing 订阅者
  8. bus.start(); return bus

运行期（以命中率为例）:
  MPCacheServer.lookup_prefetch 结束 → publish(MP_LOOKUP_PREFETCH_END, {requested_tokens, hit_tokens, ...})
     → LookupMetricsSubscriber._on_lookup_prefetch_end
        → lmcache_mp.lookup_requested.add(requested_tokens, attrs={model_name, cache_salt})
        → lmcache_mp.lookup_hit.add(hit_tokens, attrs={model_name, cache_salt})

Prometheus 端命中率 = rate(lmcache_mp_lookup_hit_tokens_total[5m]) / rate(lmcache_mp_lookup_requested_tokens_total[5m])
```

注意：命中率的**分子分母由同一个事件同时推进**，所以即便在部分失败路径下比值也有意义。

#### 4.4.3 源码精读

OTel 初始化的两种模式分支：

[otel_init.py:44-119](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/otel_init.py#L44-L119) — `init_otel_metrics`：设了 `otlp_endpoint` 就用 `OTLPMetricExporter` + `PeriodicExportingMetricReader`（默认 10 秒导一次）走 OTLP push；否则用 `PrometheusMetricReader` + `prometheus_client.start_http_server` 走 pull fallback。

`register_gauge` 是个便捷包装，支持「单值」和「带属性的多值」两种回调形状（后者用于 per-adapter / per-tier 指标）：

[otel_init.py:162-219](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/otel_init.py#L162-L219) — `register_gauge` 隐藏 OTel 样板，回调可返回单个值或 `(value, attrs)` 列表；OTel 不可用时静默跳过。

`ObservabilityConfig` 是 MP 可观测的总开关集合：

[config.py:26-86](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/config.py#L26-L86) — `enabled`（总线总开关）、`metrics_enabled` / `logging_enabled` / `tracing_enabled`（三类订阅者开关）、`otlp_endpoint`（push vs pull）、`metrics_sample_rate`（生命周期直方图采样率，默认 1%）、`trace_level`（trace 记录）等字段。

唯一入口 `init_observability`，注意 provider 在订阅者之前建好：

[config.py:266-404](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/config.py#L266-L404) — 完整启动序列。`metrics_enabled` 分支里注册了约 15 个 metrics 订阅者（[L324-L359](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/config.py#L324-L359)）；`tracing_enabled=True` 但 `otlp_endpoint=None` 会在 [L257-L261](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/config.py#L257-L261) 抛 `ValueError`——trace 没有本地 fallback。

命中率订阅者（本讲最重要的指标）：

[lookup.py:52-93](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/subscribers/metrics/lookup.py#L52-L93) — `LookupMetricsSubscriber` 在 `__init__` 建 `lmcache_mp.lookup_requested` 与 `lmcache_mp.lookup_hit` 两个 counter（带 `model_name`/`cache_salt` 属性）；只订阅 `MP_LOOKUP_PREFETCH_END` 一个事件，分子分母同源。

MP server 启动处把 `--instance-id` 投影成 OTel Resource 身份并调入口：

[server.py:310-321](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/multiprocess/server.py#L310-L321) — `obs_config.service_instance_id = mp_config.instance_id`（让 telemetry 与 coordinator membership 共用一个 id），随后 `init_observability(obs_config, ...)`，再初始化 trace recorder。

#### 4.4.4 代码实践

**实践目标**：挑选 3 个对运维最有价值的指标，说明它们如何从事件总线聚合而来，并接上 Prometheus/Grafana。这是本讲的主实践任务。

**操作步骤**：

1. **选定 3 个指标**（参考 METRICS.md）：
   - **缓存命中率**：`lmcache_mp.lookup_hit` / `lmcache_mp.lookup_requested`（源事件 `MP_LOOKUP_PREFETCH_END`）。
   - **L1 写入吞吐（chunks）**：`lmcache_mp.l1_write`（源事件 `L1_WRITE_FINISHED`，`+len(keys)`）。
   - **L2 存储完成（每后端 ops）**：`lmcache_mp.l2_store_completed`（源事件 `L2_STORE_COMPLETED`，带 `l2_name` 属性）。

2. **追溯每个指标的「事件 → 订阅者 → OTel counter」**：在 `subscribers/metrics/` 下找到对应文件，确认 `get_subscriptions()` 订阅的 EventType 和 `counter.add(...)` 的计算式，与 METRICS.md 表格对照。

3. **对照 `examples/observability/docker-compose.yml`** 起栈：

   [docker-compose.yml:1-70](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/observability/docker-compose.yml#L1-L70) — 4 服务栈：`otel-collector`（:4320 收 OTLP gRPC，转发给 Tempo+Prometheus）、`tempo`（trace 存储）、`prometheus`（:9091，从 collector 抓指标）、`grafana`（:3000，匿名 Admin）。

   注意端口映射 `4320:4317`——LMCache 的 `--otlp-endpoint http://localhost:4320` 发到 collector 的 4317。

4. **启动并制造流量**（参见 `examples/observability/README.md`）：

   ```bash
   cd examples/observability && docker compose up -d
   MODEL=/your/model/path bash start-server.sh
   lmcache bench engine --engine-url http://localhost:8100 \
     --workload long-doc-qa --kv-cache-volume 1 --ldqa-query-per-document 10
   ```

5. **在 Grafana 验证**：打开 `http://localhost:3000`，用 PromQL 查命中率：
   ```
   rate(lmcache_mp_lookup_hit_tokens_total[5m])
   / rate(lmcache_mp_lookup_requested_tokens_total[5m])
   ```

**需要观察的现象**：

- 命中率：第一次查同一文档为 0（miss），后续查询趋近 1（hit）。
- L1 写入计数：随 store 请求上升。
- L2 存储完成：带 `{l2_name="fs"}` 或 `{l2_name="nixl_store"}` 等标签，能区分后端。
- OTel counter 只有在**首次自增后**才出现在 `/metrics`（见 METRICS.md 提示）——若只看到 Python runtime 指标，先触发一次 store/retrieve。

**预期结果**（若无法本地运行则标注「待本地验证」）：你能在 Grafana 上看到命中率曲线从 0 上升到接近 1，证明「事件 → 订阅者 → OTel → collector → Prometheus → Grafana」整条链路打通。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `--enable-tracing` 必须配 `--otlp-endpoint`，而 metrics 不必？

> **答案**：metrics 有 Prometheus pull 本地 fallback（`init_otel_metrics` 里 `otlp_endpoint=None` 分支起一个 `prometheus_client` HTTP server）；而 tracing（`init_otel_tracing`）在 `otlp_endpoint=None` 时直接 return，没有本地 fallback——span 无处可去。所以代码在 [config.py:257-261](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/config.py#L257-L261) 强制校验，缺端点就 fail-fast。

**练习 2**：命中率的分子分母为什么要用**同一个事件**推进？

> **答案**：保证二者总是成对前进、口径一致。若分子来自 `MP_LOOKUP_PREFETCH_END`、分母来自别的时刻（如 `START`），在部分失败/早退路径下分子分母可能错配，比值失真。同源事件让比值在任何路径下都有意义（早退查询两者都贡献 0，被放弃的查询两者都不贡献）。

**练习 3**：`metrics_sample_rate`（默认 1%）影响哪些指标、不影响哪些？

> **答案**：只影响**生命周期直方图**（如 `l1_chunk_lifetime`、`l0_block_lifetime`），因为逐 chunk 跟踪开销大，采样控制成本。**不影响 Counter**——METRICS.md 明确「Counters always count all events regardless of this setting」，命中率、读写次数这类计数器永远是全量。

---

### 4.5 MP 追踪子系统与 trace 记录

#### 4.5.1 概念说明

MP 体系里「追踪（tracing）」其实有两个**不同**的东西，初学者容易混淆：

1. **OTel 分布式追踪（span）**：由 `subscribers/tracing/` 下的订阅者从事件还原成 span。一个请求 = 一个根 span（`request`），下面嵌套 `mp.lookup_prefetch` / `mp.retrieve` / `mp.store` 子 span，最终推给 Tempo 在 Grafana 看瀑布图。这是**在线、实时**的，回答「一次请求内部各阶段耗时」。

2. **trace 记录（`lmcache trace` / `@enable_tracing`）**：由 `trace/` 子系统把函数调用（入参）记录成**二进制 trace 文件**，供**离线回放**重放 LMCache 操作。它只记入参、在函数入口发一个 `TRACE_CALL` 事件，输出值/异常靠回放时重跑得到。这是**离线、用于复现**的。

本节聚焦 OTel span 的还原机制（实践任务主路径），并简述 trace 记录。

**START/END 配对**是 OTel tracing 的核心机制：`MP_STORE_START` / `MP_STORE_END` 是一对，靠 `session_id` 关联。订阅者收到 START 建子 span、收到 END 关闭它，时间戳来自事件（CUDA 路径下是 GPU 精确时刻）。根 `request` span 在 `MP_REQUEST_START` 建、`MP_REQUEST_END` 关；但若此时 GPU store 还在飞，要**延迟**到最后的 `MP_STORE_END` 才关——否则 span 会漏掉 GPU 阶段。

命中率会作为**根 span 的属性**写入（`hit_tokens` / `requested_tokens` / `hit_rate`），这样在 Tempo 里可以用 `{ name = "request" && span.hit_rate < 0.5 }` 直接过滤低命中请求。

#### 4.5.2 核心流程

```
事件流（一次命中请求）:
  MP_REQUEST_START(session=sid)            → 建根 span "request"
  MP_LOOKUP_PREFETCH_START(sid)            → 建子 span "mp.lookup_prefetch"
  MP_LOOKUP_PREFETCH_END(sid, hit/tokens)  → 关子 span; 在根 span 写 hit_rate 属性
  MP_RETRIEVE_START(sid) ... MP_RETRIEVE_END(sid)  → retrieve 子 span
  MP_STORE_SUBMITTED(sid)                  → in-flight store 计数 +1
  MP_STORE_START(sid) ... MP_STORE_END(sid) → store 子 span; 计数 -1
  MP_REQUEST_END(sid)                      → 若计数=0 关根 span; 否则暂存时间戳，等最后 END 关

trace 记录路径（离线）:
  @enable_tracing 装饰 StorageManager 方法
    → 函数入口 publish(TRACE_CALL, {qualname, args, t_mono})
    → trace recorder 编码入参 → 写二进制文件
  离线: lmcache trace replay 读文件 → 重跑函数 → 观察实时行为
```

#### 4.5.3 源码精读

`MPServerTracingSubscriber` 的订阅表（START/END/请求生命周期全订阅）：

[mp_server.py:96-109](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/subscribers/tracing/mp_server.py#L96-L109) — `get_subscriptions` 把 `MP_REQUEST_START` / `MP_*_SUBMITTED` / `MP_REQUEST_END` / 三对 `START`+`END` 都接上。

START 事件建子 span（嵌在根 span 下，时间用事件时间戳）：

[mp_server.py:201-233](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/subscribers/tracing/mp_server.py#L201-L233) — `_on_start` 取/建根 span 上下文，用 `_tracer.start_span(name, context=root_ctx, start_time=event.timestamp)` 建子 span，把 metadata 全部写成 span 属性，并登记进共享 `SpanRegistry` 供其他订阅者嵌套。

END 事件关 span + 把命中率写上根 span：

[mp_server.py:268-279](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/subscribers/tracing/mp_server.py#L268-L279) — 收到 `MP_LOOKUP_PREFETCH_END` 时，在根 `request` span 上写 `hit_tokens` / `requested_tokens` / `hit_rate` 属性——这正是 Grafana/Tempo 能按 `span.hit_rate` 过滤的来源。

根 span 的延迟关闭（GPU store/retrieve 可能晚于 REQUEST_END）：

[mp_server.py:178-195](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/subscribers/tracing/mp_server.py#L178-L195) — `_on_session_end`：若 in-flight store/retrieve 计数为 0 立即关根 span，否则把 END 时间戳暂存，等最后一个 END 在 [L288-L294](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/subscribers/tracing/mp_server.py#L288-L294) 触发关闭。

trace 记录子系统的装饰器（近零开销，关闭时只一次布尔判断）：

[decorator.py:88-147](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/trace/decorator.py#L88-L147) — `enable_tracing` 装饰器：在装饰时用 `inspect.signature` 预算好要记的参数名（排除 `self`/`cls`、支持 `capture`/`redact`），运行期关闭时只一次 `_tracing_enabled` 布尔判断；开启时 `bind_partial` 取参、`publish_call_event` 发 `TRACE_CALL` 事件。

[decorator.py:59-85](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/trace/decorator.py#L59-L85) — `publish_call_event` 在发布时（非 drain 时）采 `time.monotonic()` 作 `t_mono`，与 `Event.timestamp` 对齐避免时钟漂移。

#### 4.5.4 代码实践

**实践目标**：用 OTel 追踪在 Grafana/Tempo 里看到一次请求的 span 瀑布图，并按命中率过滤。

**操作步骤**：

1. 用 `examples/observability/start-server.sh` 启动 LMCache+vLLM，确认它带了 `--enable-tracing --otlp-endpoint`（否则不会发 span）。
2. 按 README Step 3 发若干请求（同一文档查多次，制造从 miss 到 hit 的过渡）。
3. 在 Grafana → Explore → Tempo 数据源，依次试 README 给出的 TraceQL 查询：
   ```
   { name = "request" }                                  # 所有请求根 span
   { name = "request" && span.hit_rate < 0.5 }           # 低命中请求
   { name = "request" } >> { name = "mp.retrieve" }      # 有 retrieve（即命中过）的请求
   ```
4. 点开某条 trace 看瀑布：根 `request` 下应有 `mp.lookup_prefetch` / `mp.retrieve` / `mp.store` 子 span，根 span 属性里有 `hit_rate`。

**需要观察的现象**：第一次请求 `hit_rate≈0`、子 span 里 `mp.retrieve` 几乎没有有效搬运；后续请求 `hit_rate` 上升、`mp.lookup_prefetch` 与 `mp.retrieve` 占比变化。

**预期结果**（若本地无 GPU 等无法运行，标注「待本地验证」）：你能用一句话解释「`span.hit_rate` 这个属性是怎么从 `MP_LOOKUP_PREFETCH_END` 事件流到根 span 上的」——通过 `MPServerTracingSubscriber._on_end` 在 [mp_server.py:268-279](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/subscribers/tracing/mp_server.py#L268-L279) 写入。

#### 4.5.5 小练习与答案

**练习 1**：根 `request` span 为什么不能在 `MP_REQUEST_END` 到达时无条件关闭？

> **答案**：GPU 上的 store/retrieve 是异步的，`MP_REQUEST_END`（CPU 同步发出）可能先于 `MP_STORE_END`（GPU 回调盖戳）到达。若此时关根 span，store 子 span 会变成孤儿或落在根 span 之外。所以订阅者用 in-flight 计数（`_pending_store_count`/`_pending_retrieve_count`）守卫：计数非 0 就暂存 END 时间戳，等最后一个 END 才关。

**练习 2**：OTel span 追踪（`subscribers/tracing/`）和 trace 记录（`trace/`）有什么本质区别？

> **答案**：前者是**在线实时**的分布式追踪，把请求生命周期切成 span 推给 Tempo，看耗时分布；后者是**离线回放**用的，把函数入参录成二进制文件，用 `lmcache trace replay` 重放以复现/调试。前者发各类 `MP_*` 事件由 tracing 订阅者还原 span，后者只发 `TRACE_CALL` 事件由 recorder 落盘。

---

## 5. 综合实践

**任务**：为一个 MP 模式的 LMCache 部署，配出一条「从一次缓存命中到 Grafana 报警曲线」的完整可观测链路，并标注每个环节对应的源码。

要求产出一张链路图，至少包含以下节点，并给每个节点附上**源码位置**或**指标名**：

1. **生产者**：`MPCacheServer` 在 lookup_prefetch 结束时发什么事件？（`MP_LOOKUP_PREFETCH_END`，metadata 含 `requested_tokens`/`hit_tokens`/`model_name`/`cache_salt`）
2. **总线**：事件经过 `EventBus.publish` → 队列 → `_drain_all` 分发（[event_bus.py:196-221](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/event_bus.py#L196-L221)、[L312-L354](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/event_bus.py#L312-L354)）。
3. **聚合**：`LookupMetricsSubscriber` 把事件变成 `lmcache_mp.lookup_hit` / `lmcache_mp.lookup_requested` 两个 counter（[lookup.py:90-93](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/subscribers/metrics/lookup.py#L90-L93)）。
4. **导出**：OTLP push 经 `OTLPMetricExporter` 每 10 秒推一次（[otel_init.py:84-87](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/mp_observability/otel_init.py#L84-L87)）。
5. **后端**：collector → Prometheus（`docker-compose.yml`）。
6. **可视化/报警**：Grafana 用 PromQL `rate(hit)/rate(requested)` 画曲线；可加一条「命中率 < 0.3 持续 5 分钟」的报警规则。

**进阶**：再补一条「tracing 平行链路」——同一个 `MP_LOOKUP_PREFETCH_END` 事件被 `MPServerTracingSubscriber` 消费，把 `hit_rate` 写到根 span 属性，经 OTLP 推给 Tempo，在 Grafana 用 TraceQL `{ name = "request" && span.hit_rate < 0.5 }` 过滤。说明**一个事件如何同时喂 metrics 和 tracing 两条链路**——这正是事件驱动架构的复用红利。

> 提示：若本地无 GPU / 无法起 docker 栈，可只画链路图并标注「待本地验证」，重点是源码位置与数据流向正确。

## 6. 本讲小结

- LMCache 有**两套可观测体系**：单进程引擎用 `lmcache/observability.py`（`lmcache:` 前缀、Pull 模型、区间增量 + Prometheus 直出），MP 模式用 `lmcache/v1/mp_observability/`（`lmcache_mp.` 前缀、事件驱动 + OTel Push）。
- 单进程体系是 `LMCStatsMonitor`（累加）→ `get_stats_and_clear`（快照+清零）→ `PrometheusLogger.log_prometheus`（灌 Prometheus）三段式，由 `LMCacheStatsLogger` 守护线程每 10 秒驱动，靠 `PROMETHEUS_MULTIPROC_DIR` 支持多 worker。
- MP 体系核心是 **EventBus**：生产者非阻塞 `publish`（队列满尾丢弃、CUDA 路径用 C++ host 回调盖精确时间戳），drain 线程分发，订阅者分 metrics / logging / tracing 三类，互不传染。
- `init_observability()` 是 MP 唯一入口，严格按「先建 OTel provider → 再建 bus → 再注册订阅者」顺序，避免订阅者绑到空 provider。
- 指标可归为**健康 / 前缀命中 / token 吞吐 / 请求级时延**四类；命中率分子分母同源事件，比值在任何路径下都有意义。
- MP 的「追踪」有两套：OTel span（在线、START/END 配对还原、根 span 带 `hit_rate` 属性）与 `@enable_tracing` trace 记录（离线回放）。

## 7. 下一步学习建议

- 想深入**单进程指标的具体含义**，通读 `lmcache/observability.py` 的 `LMCStats` 字段表（[L37-L115](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/observability.py#L37-L115)）与 `log_prometheus` 的逐字段映射。
- 想了解**事件元数据契约**（每个 EventType 的 metadata 键），读 `docs/design/v1/mp_observability/EVENTS.md`；想看命中率推导细节读同目录 `DEBUG.md` / `L1_L2_HIT_RATE_PLAN.md`。
- 想自己**加一个新指标**，严格按 `docs/design/v1/mp_observability/README.md` 的「How to Add a New Event and Subscriber」六步走（定义 EventType → 发布 → 写订阅者 → 导出 → 注册 → 补 EVENTS.md）。
- 关于 MP 架构全貌，回看 [u3-l1 多进程架构总览](u3-l1-mp-architecture-overview.md) 与 [u3-l2 MP Server/Client 与进程间通信](u3-l2-mp-server-client-ipc.md)；后续可进入 [u4 分布式存储与 L2 适配器](u4-l2-distributed-storage.md)，那里会用到本讲的 per-adapter 指标（`l2_name`、`adapter_index`）做容量规划。

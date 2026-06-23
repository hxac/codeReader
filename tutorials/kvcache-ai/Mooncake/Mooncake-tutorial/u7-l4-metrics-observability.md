# 指标与可观测性（Metrics & Observability）

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清 Mooncake Store 的**三层指标体系**：客户端指标、主端指标、HA 指标分别覆盖什么、由谁采集、在哪里暴露。
2. 区分 Prometheus 的四种基础指标类型 **Counter / Gauge / Histogram / Summary**，并理解 Mooncake 在此之上扩展出的**混合指标（hybrid metric）**解决了什么问题。
3. 对照真实源码，指出一条具体指标（如 PutEnd 请求数、内存利用率、OpLog 复制延迟）是在**哪一行代码**被更新的。
4. 启动 `monitoring/` 下的 **Prometheus + Grafana** 监控栈，把运行中的 `mooncake_master` 接入抓取，并在 Grafana 里看到吞吐与内存利用率曲线。
5. 发现并修复仓库自带示例看板里的一个“指标名不匹配”问题，养成“看板查询必须对照源码指标名”的习惯。

## 2. 前置知识

- **Prometheus 指标模型**：Prometheus 用「指标名 + 若干标签（label）」唯一标识一条时间序列。例如 `http_requests_total{method="GET"} 42` 表示“GET 方法累计请求数为 42”。本讲会反复出现这种 `指标名{标签} 值` 的文本格式。
- **四种指标类型**（这是本讲核心，4.2 会详述）：
  - **Counter（计数器）**：只增不减，比如“累计 PutEnd 请求数”。
  - **Gauge（瞬时值）**：可增可减，比如“当前已用内存”。
  - **Histogram（直方图）**：把观测值落到预设的桶（bucket）里，用来算分位数（如 p95 延迟）。
  - **Summary（摘要）**：在客户端直接算出分位数（如 p50/p90/p99）。
- **拉模型（pull）抓取**：Prometheus 主动周期性地去目标服务的 `/metrics` HTTP 端点拉取文本格式指标，而不是服务端推送。这一点决定了 Mooncake 必须自己起一个 HTTP 服务来“应答”抓取。
- **ylt（yaLanTingLibs）指标库**：Mooncake 没有自己从零写指标，而是复用了 `ylt::metric` 命名空间下的 `counter_t`/`gauge_t`/`histogram_t`/`summary_t` 等模板类，它们能直接序列化成 Prometheus 文本格式。
- **依赖讲义**：本讲建立在 [u5-l2 Master 服务](u5-l2-master-service.md) 之上，假定你已经知道 Master 是“存元数据、分配 replica、协调 Put/Get”的中心节点。如果你对 `PutStart/PutEnd/GetReplicaList` 这些 RPC 还不熟悉，建议先读 u5-l2。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `mooncake-store/include/client_metric.h` | **客户端指标**定义。每个客户端进程一个实例，记录传输字节/延迟、RPC 调用、SSD 读写等。 |
| `mooncake-store/src/client_metric.cpp` | 客户端指标实现：环境变量解析、周期性 glog 日志上报线程。 |
| `mooncake-store/include/master_metric_manager.h` | **主端指标**单例管理器的接口，定义了上百个 Counter/Gauge/Histogram。 |
| `mooncake-store/src/master_metric_manager.cpp` | 主端指标实现：构造时给每个指标命名（即 Prometheus 里的指标名）、序列化成文本。 |
| `mooncake-store/include/ha_metric_manager.h` | **HA 指标**单例管理器接口，记录 OpLog 复制滞后、错误计数、Standby 状态机。 |
| `mooncake-store/src/ha_metric_manager.cpp` | HA 指标实现与命名。 |
| `mooncake-store/include/hybrid_metric.h` | **混合指标**模板：把“静态标签 + 动态标签”合在一起，是理解客户端 RPC/操作指标的关键。 |
| `mooncake-store/src/master_admin_service.cpp` | Master 的 HTTP admin 服务，注册并实现 `/metrics`、`/health` 等端点。 |
| `mooncake-store/src/rpc_service.cpp` | RPC 入口包装层，**实际调用 inc_xxx() 更新主端计数器的地方**。 |
| `monitoring/docker-compose.yml` | 一键拉起 Prometheus + Grafana 的容器编排文件。 |
| `monitoring/prometheus/prometheus.yml` | Prometheus 抓取配置，指明去哪里抓、多久抓一次。 |
| `monitoring/grafana/dashboards/mooncake.json` | Grafana 示例看板（注意：里面的指标名有坑，4.7 会讲）。 |

---

## 4. 核心概念与源码讲解

### 4.1 指标体系：三层指标管理器

#### 4.1.1 概念说明

一个分布式存储系统要回答的问题分布在不同位置：

- **客户端**最清楚“这次 Get 花了多久、传了多少字节”（端到端，含 RDMA 传输）。
- **Master** 最清楚“集群一共有多少 key、内存用了多少、各 RPC 调用了多少次”（全局视图）。
- **HA 层** 最清楚“Standby 落后 Primary 多少、etcd 写有没有失败”（主备一致性健康度）。

如果只在一个地方采指标，要么看不到端到端延迟，要么看不到集群容量。所以 Mooncake 设计了**三层、互相独立**的指标管理器，分别对应三类源码：

| 层 | 类 | 生命周期 | 指标名前缀 | 采集/暴露方式 |
|----|------|----------|-----------|--------------|
| 客户端 | `ClientMetric` | 每个客户端进程一个实例 | `mooncake_transfer_*` / `mooncake_client_rpc_*` / `mooncake_ssd_*` | 周期性写 glog 日志（非 HTTP） |
| 主端 | `MasterMetricManager` | 进程级单例 | `master_*` | HTTP `/metrics` + 周期 glog |
| HA | `HAMetricManager` | 进程级单例 | `ha_*` | HTTP `/metrics`（拼接在主端之后）|

**关键直觉**：客户端指标是“按实例”的（因为一台机器上可能跑多个客户端进程），而主端/HA 指标是“全局单例”的（因为一个 Master 进程就代表整个集群的元数据视图）。

#### 4.1.2 核心流程

```text
┌─────────────────────────────────────────────────────────────────┐
│                       Master 进程内部                            │
│                                                                  │
│   rpc_service.cpp          master_metric_manager.cpp             │
│   (每个 RPC 入口)   ──inc──►  MasterMetricManager (单例)          │
│                                   │                              │
│   ha/ (OpLog/Standby) ──inc──►  HAMetricManager (单例)           │
│                                   │                              │
│            master_admin_service.cpp                              │
│            /metrics 端点 ◄──serialize──┘                         │
└──────────────────────────────┬──────────────────────────────────┘
                               │ HTTP text/plain
                               ▼
                        Prometheus (pull, 每 5s)
                               │
                               ▼
                            Grafana 看板

┌─────────────── 客户端进程内部（独立于 Master）──────────────────┐
│  real_client.cpp (put/get) ──observe──► ClientMetric (每实例)     │
│                                            │                     │
│                               周期 glog 日志 (MC_STORE_CLIENT_    │
│                               METRIC_INTERVAL 控制)              │
└──────────────────────────────────────────────────────────────────┘
```

注意客户端指标**默认不走 HTTP**，而是写进 glog 日志（便于在没有 Prometheus 的环境里也能看），这与主端/HA 走 `/metrics` 的方式不同。

#### 4.1.3 源码精读

主端与 HA 都是单例，典型的“懒初始化 + 删除拷贝构造”写法：

[mooncake-store/src/master_metric_manager.cpp:15-19](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_metric_manager.cpp#L15-L19) —— 主端指标单例，C++11 起函数内 `static` 局部变量本身就是线程安全的：

```cpp
MasterMetricManager& MasterMetricManager::instance() {
    static MasterMetricManager static_instance;
    return static_instance;
}
```

[mooncake-store/include/master_metric_manager.h:15-23](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/master_metric_manager.h#L15-L23) —— 删除拷贝/移动构造，强制只能通过 `instance()` 访问。

[mooncake-store/include/client_metric.h:660-679](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/client_metric.h#L660-L679) —— 客户端指标**不是单例**，而是由工厂方法 `Create()` 返回 `unique_ptr`，并且可以被环境变量关闭（返回 `nullptr`）：

```cpp
static std::unique_ptr<ClientMetric> Create(
    const std::map<std::string, std::string>& labels = {},
    bool master_rpc_metrics_enabled = true);
```

[mooncake-store/src/client_metric.cpp:101-122](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_metric.cpp#L101-L122) —— 客户端指标的开关与采样间隔都来自环境变量 `MC_STORE_CLIENT_METRIC` / `MC_STORE_CLIENT_METRIC_INTERVAL` / `MC_STORE_CLIENT_METRIC_BANDWIDTH`，默认开启但默认不周期上报。

#### 4.1.4 代码实践

**实践目标**：先不依赖 Prometheus，直接用 `curl` 验证 Master 的 `/metrics` 端点能吐出三类指标。

**操作步骤**：

1. 参照 [monitoring/README.md:60-66](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/monitoring/README.md#L60-L66)，以开启指标的方式启动 master：

   ```bash
   ./build/mooncake_master --metrics_port=9003 --enable_metric_reporting=true
   ```

2. 抓取指标文本：

   ```bash
   curl -s http://localhost:9003/metrics
   ```

**需要观察的现象**：输出里应同时出现 `master_` 开头（主端）和 `ha_` 开头（HA）的指标行。注意**不会**出现 `mooncake_transfer_*`——因为客户端指标在客户端进程里，不在 Master 进程里。

**预期结果**：能看到类似 `# HELP master_put_end_requests_total ...` 与 `master_put_end_requests_total 0` 的行。**待本地验证**（取决于你是否已编译并运行 master）。

#### 4.1.5 小练习与答案

**练习 1**：为什么客户端指标不用单例，而主端/HA 指标用单例？

**参考答案**：一台机器上可能同时跑多个客户端进程（例如多个推理 worker），每个进程要有自己独立的传输/延迟统计，所以客户端指标按实例存在；而一个 Master 进程就代表整个集群的元数据视图，全局只需一份，用单例最自然，也避免多处累加导致重复计数。

**练习 2**：`/metrics` 端点的输出里，主端指标和 HA 指标是怎么拼到一起的？

**参考答案**：见 [master_admin_service.cpp:255-259](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_admin_service.cpp#L255-L259) 的 `BuildMetricsText()`，它调用 `AppendMetricSections()` 把 `MasterMetricManager::serialize_metrics()` 与 `HAMetricManager::serialize_metrics()` 两个字符串拼成一个响应体。

---

### 4.2 指标类型：Counter / Gauge / Histogram / Summary 与混合指标

#### 4.2.1 概念说明

Prometheus 规定了四种指标类型，Mooncake 通过 `ylt::metric` 全部用到。理解它们是读懂一切看板的前提：

| 类型 | 行为 | 典型用法 | Mooncake 例子 |
|------|------|----------|--------------|
| **Counter** | 单调递增（只能 inc，重启归零） | “累计”类统计 | `master_put_end_requests_total` |
| **Gauge** | 可增可减，反映当前瞬时值 | 容量、队列长度、连接数 | `master_allocated_bytes`、`ha_oplog_standby_lag` |
| **Histogram** | 把观测值分桶，存 `bucket / _sum / _count` | 延迟分布、对象大小分布 | `mooncake_transfer_get_latency`、`master_value_size_bytes` |
| **Summary** | 客户端直接算分位数 | 需要精确 p99 且不想靠桶估算 | `mooncake_ssd_read_latency_summary_us` |

**Histogram vs Summary 的取舍**（重点）：

- Histogram 把值落到**预设桶**，Prometheus 服务端再用 `histogram_quantile()` **估算**分位数。优点是多个实例的分位数可以在服务端**聚合**（把各桶相加再算）。
- Summary 在客户端用流式算法算出**精确**分位数（如 p50/p90/p99），但**无法跨实例聚合**（你不能把两台机器的 p99 相加得到集群 p99）。

Mooncake 的 SSD 延迟用 Summary（精确 p99 重要），而传输/RPC 延迟用 Histogram（可能跨多客户端聚合）。

**混合指标（hybrid metric）**：Prometheus 里一条指标可以带多个标签。Mooncake 在 `hybrid_metric.h` 里定义了一种“把**静态标签**和**动态标签**拼在一起”的类型：

- **静态标签**：进程生命周期内不变，比如 `cluster_id`（来自环境变量 `MC_STORE_CLUSTER_ID`）。
- **动态标签**：运行时才出现的，比如 RPC 名 `rpc_name="GetReplicaList"`、操作名 `op_name="put"`。

为什么要“混合”？因为 `cluster_id` 这种静态前缀如果每次动态写都拼接，会有重复字符串开销；hybrid 把静态部分预格式化成一个字符串缓存，序列化时直接拼上动态部分，既保留可区分性又省开销。

#### 4.2.2 核心流程

Histogram 的 `observe(value)` 流程（[hybrid_metric.h:253-261](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/hybrid_metric.h#L253-L261)）：

```text
observe(value)
  ├── 用 lower_bound 在 bucket_boundaries 里找第一个 >= value 的桶下标 i
  ├── sum.inc(value)            // 累加到 _sum
  └── bucket_counts[i].inc()    // 该桶计数 +1
```

序列化时，每个桶输出**累积**计数（小于等于该桶上界的总数），这是 Prometheus Histogram 的规范：

\[ \text{bucket}\{le=b_k\} = \sum_{j \le k} \text{count}_j \]

因此最后总有一个 `le="+Inf"` 的桶，它的值就是 `_count`。分位数估算公式为：

\[ q\text{-quantile} \approx b_k \quad \text{其中 } k \text{ 是使累积比例首次达到 } q \text{ 的桶} \]

延迟桶的设计是性能敏感的：桶太粗则 p95 失真，桶太细则内存和序列化开销大。Mooncake 针对 RDMA 的亚毫秒特性，把桶在 <1ms 区间做得特别密：

[mooncake-store/include/client_metric.h:22-30](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/client_metric.h#L22-L30) —— 传输/RPC 延迟桶（单位微秒），注释明确说“为 RDMA 调优，<1ms 区间细粒度，毫秒级尾部到 1s”：

```cpp
const std::vector<double> kLatencyBucket = {
    125, 150, 200, 250, 300, 400, 500, 750, 1000,   // sub-ms ~ 1ms
    1500, 2000, 3000, 5000, 7000, 15000, 20000,    // ms-level tail
    50000, 100000, 200000, 500000, 1000000};        // safeguards
```

#### 4.2.3 源码精读

[mooncake-store/include/client_metric.h:104-127](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/client_metric.h#L104-L127) —— `TransferMetric` 把四种类型都用上了：Counter（`total_read_bytes`/`total_write_bytes`）+ 多个 Histogram（各类延迟）。注意 Counter 的指标名带 `_bytes` 但类型仍是 counter（累计字节数单调递增）。

[mooncake-store/include/client_metric.h:89-100](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/client_metric.h#L89-L100) —— 一个通用计时模板 `execute_timed_operation`：执行操作 → 若成功回调 `observe_fn(latency_us, result)`。客户端 put/get 就靠它把延迟喂给 Histogram：

```cpp
template <typename Result, typename Operation, typename SuccessFn, typename ObserveFn>
Result execute_timed_operation(Operation&& operation, SuccessFn&& success_fn,
                               ObserveFn&& observe_fn) {
    const auto start_time = std::chrono::steady_clock::now();
    Result result = std::forward<Operation>(operation)();
    if (std::forward<SuccessFn>(success_fn)(result)) {
        std::forward<ObserveFn>(observe_fn)(elapsed_us_since(start_time), result);
    }
    return result;
}
```

[mooncake-store/include/hybrid_metric.h:231-261](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/hybrid_metric.h#L231-L261) —— 混合直方图 `basic_hybrid_histogram` 的 `observe()`：先二分找桶，再更新 `sum_` 和对应 `bucket_counts_`。它的 `static_labels_` 在构造时被预格式化进 `static_labels_str_`，序列化时直接拼接，省去每次重组字符串。

[mooncake-store/include/client_metric.h:535-543](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/client_metric.h#L535-L543) —— SSD 延迟用 Summary，分位数 `{0.5, 0.9, 0.99}` 即 p50/p90/p99，由客户端直接算出。

#### 4.2.4 代码实践

**实践目标**：通过阅读并运行一个单元测试，理解 Counter 与 Histogram 的行为差异，而不必启动整个系统。

**操作步骤**：

1. 打开 [mooncake-store/tests/client_metrics_test.cpp:21-60](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/client_metrics_test.cpp#L21-L60)，阅读 `TransferMetricsSummaryTest`。
2. 关注三处断言：
   - `metrics.total_read_bytes.inc(1024)` 后，摘要里出现 `Total Read: 1.00 KB`（Counter 累计 + 字节格式化）。
   - `get_latency_us.observe(150/200/300)` 三次后，摘要里出现 `Get: count=3`（Histogram 计数）。
   - 摘要里出现 `p95<` 和 `max<`（从桶估算出的分位数）。
3. 若已编译 store 测试，运行：

   ```bash
   cd build && ctest -R client_metrics_test --output-on-failure
   ```

**需要观察的现象**：测试通过，且 `count`、`p95`、`max` 与你手动按桶估算的一致。

**预期结果**：3 次 observe(150/200/300)，count=3；p95 应落在覆盖第 95 百分位（即第 3 个观测值 300μs）的那个桶上。**待本地验证**（依赖编译环境）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 SSD 延迟用 Summary、而传输延迟用 Histogram？

**参考答案**：SSD 延迟通常按单节点观察，需要精确 p99 来发现长尾，Summary 在客户端直接算精确分位数更合适；传输/RPC 延迟可能需要跨多个客户端实例聚合（比如算整个集群的 p95），Histogram 的桶可以在 Prometheus 服务端相加后再用 `histogram_quantile()` 估算，适合聚合场景。

**练习 2**：`execute_timed_operation` 为什么只在 `success_fn(result)` 为真时才 observe？

**参考答案**：失败请求的延迟分布通常由错误路径决定，混进成功请求的 Histogram 会污染“正常请求延迟”的统计。Mooncake 选择只统计成功请求的延迟，错误本身另有 Counter（如 `master_put_end_failures_total`）单独记录。

---

### 4.3 主端指标精读：MasterMetricManager

#### 4.3.1 概念说明

`MasterMetricManager` 是体量最大的指标管理器，覆盖了 Master 关心的所有全局状态：存储容量（内存/SSD/文件）、key 数量、各 RPC 的成功/失败计数、eviction、promotion、batch 操作等。它用单例保证全进程一份，并且**给每个指标显式命名**——这个名字就是 Prometheus 里看到的指标名。

#### 4.3.2 核心流程

1. **构造**：在 `MasterMetricManager()` 构造函数里，逐个初始化每个 `ylt::metric::xxx_t` 成员，第一个参数就是 Prometheus 指标名，第二个是 HELP 文本。
2. **更新**：业务代码（主要是 `rpc_service.cpp` 的 RPC 包装层和 `master_service.cpp`）在合适时机调用 `inc_xxx()` / `dec_xxx()` / `set_xxx()` / `observe_xxx()`。
3. **序列化**：抓取时 `serialize_metrics()` 把所有指标序列化成 Prometheus 文本。

#### 4.3.3 源码精读

[mooncake-store/src/master_metric_manager.cpp:22-30](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_metric_manager.cpp#L22-L30) —— 内存类 Gauge 命名：`master_allocated_bytes`（已分配）与 `master_total_capacity_bytes`（总容量），两者之比就是“内存利用率”，这是看板里最常画的一条线：

```cpp
mem_allocated_size_("master_allocated_bytes",
    "Total memory bytes currently allocated across all segments"),
mem_total_capacity_("master_total_capacity_bytes",
    "Total memory capacity across all mounted segments"),
```

[mooncake-store/src/master_metric_manager.cpp:66-77](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_metric_manager.cpp#L66-L77) —— PutStart/PutEnd 的请求与失败计数，指标名统一带 `_total` 后缀（Counter 规范）：

```cpp
put_start_requests_("master_put_start_requests_total", ...),
put_start_failures_("master_put_start_failures_total", ...),
put_end_requests_("master_put_end_requests_total", ...),
put_end_failures_("master_put_end_failures_total", ...),
```

[mooncake-store/src/rpc_service.cpp:241-256](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/rpc_service.cpp#L241-L256) —— **PutEnd 计数器的实际更新点**：`execute_rpc` 包装器接收两个回调，成功则 `inc_put_end_requests()`、失败则 `inc_put_end_failures()`。这就是“一条指标在哪行代码被更新”的答案：

```cpp
return execute_rpc("PutEnd",
    [&] { return master_service_.PutEnd(client_id, key, tenant_id, replica_type); },
    [&](auto& timer) { timer.LogRequest(...); },
    [] { MasterMetricManager::instance().inc_put_end_requests(); },
    [] { MasterMetricManager::instance().inc_put_end_failures(); });
```

[mooncake-store/src/master_service.cpp:5196-5197](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_service.cpp#L5196-L5197) —— 带 segment 标签的动态 Gauge 更新：恢复快照时按段累加内存容量 `inc_total_mem_capacity(segment.name, segment.size)`，这会让序列化出 `segment_total_capacity_bytes{segment="xxx"} <size>`。

#### 4.3.4 代码实践

**实践目标**：对照源码，自己推导出一条指标在 Prometheus 文本里的样子。

**操作步骤**：

1. 在 [master_metric_manager.cpp:58-60](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_metric_manager.cpp#L58-L60) 找到 `value_size_distribution_` 的桶定义 `{4096, 65536, ...}`。
2. 推测：当你 put 了一个 100KB 的对象，`observe_value_size(100*1024)` 会落到哪个桶。
3. 启动 master 后 `curl http://localhost:9003/metrics | grep value_size` 验证。

**需要观察的现象**：会看到 `master_value_size_bytes_bucket{le="262144"} ...`（因为 100KB=102400 落在 65536 和 262144 之间，取上界桶 262144），以及 `_sum`、`_count` 行。

**预期结果**：桶计数正确累加。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：看板上想画“PutEnd 每秒成功率”，应该用哪两个指标、怎么写 PromQL？

**参考答案**：用 `rate(master_put_end_requests_total[5m])` 作为成功速率，`rate(master_put_end_failures_total[5m])` 作为失败速率。成功率可写作 `rate(master_put_end_requests_total[5m]) / (rate(master_put_end_requests_total[5m]) + rate(master_put_end_failures_total[5m]))`。注意 Counter 只能用 `rate()`/`increase()` 取差值，不能直接画原始值。

**练习 2**：`inc_total_mem_capacity` 带了一个 `segment` 参数，这对应 Prometheus 里的什么？

**参考答案**：对应一个**标签维度**。序列化后会得到多条序列 `segment_total_capacity_bytes{segment="<name>"}`，每段一条，便于在看板里按段筛选或聚合。

---

### 4.4 客户端指标精读：ClientMetric

#### 4.4.1 概念说明

`ClientMetric` 记录“从客户端视角”的统计：传输了多少字节（读/写）、各类接口（put/get/batch_put/...）的延迟分布、对 Master 的 RPC 调用次数与延迟、SSD offload 的读写量与延迟。它由四个子结构组成：

| 子结构 | 记录内容 | 主要指标类型 |
|--------|----------|-------------|
| `TransferMetric` | 总读写字节、单次/批量 put/get 传输延迟 | Counter + Histogram |
| `MasterClientMetric` | 对 Master 的各 RPC 调用计数与延迟 | hybrid_counter + hybrid_histogram |
| `TransferOperationMetric` | 按接口类型（put/get/...）拆分的操作数/字节/延迟 | hybrid（按 op_name 标签）|
| `SsdMetric` | SSD 读写字节、ops、延迟（含 Summary 分位数）| Counter + Histogram + Summary |

#### 4.4.2 核心流程

```text
客户端调用 put(key, value)
  └── execute_timed_operation(...)
        └── 成功后回调 observe_fn:
              client_->ObserveTransferOperation(kWrite, "put", bytes, latency_us)
                    └── TransferOperationMetric.Observe(...)
                          ├── observed_write_ops_.insert("put")  // 记下出现过哪些 op
                          ├── write_op_count.inc({"put"})
                          ├── write_op_bytes.inc({"put"}, bytes)
                          └── write_op_latency_us.observe({"put"}, latency_us)
```

`MasterClientMetric` 与 `TransferOperationMetric` 都用 hybrid 指标，标签分别是 `rpc_name` 和 `op_name`，运行时才知道有哪些取值（GetReplicaList、PutStart、put、get、batch_put...）。

#### 4.4.3 源码精读

[mooncake-store/src/real_client.cpp:1721-1734](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/real_client.cpp#L1721-L1734) —— 客户端 `put` 的指标更新点：成功后用 op_name=`"put"`、字节数、延迟回调 `ObserveTransferOperation`：

```cpp
auto result = execute_timed_operation<tl::expected<void, ErrorCode>>(
    [&]() { return put_internal(key, value, config, client_buffer_allocator_); },
    [](const auto &ret) { return ret.has_value(); },
    [&](uint64_t latency_us, const auto &) {
        client_->ObserveTransferOperation(TransferOperationKind::kWrite,
                                          "put", value.size_bytes(), latency_us);
    });
```

[mooncake-store/include/client_metric.h:349-400](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/client_metric.h#L349-L400) —— `TransferOperationMetric::Observe`：按读写方向分发到对应的一组 hybrid 指标，用 `op_name` 作为标签，并用一个 `unordered_set` 记录“历史上出现过的 op”以便摘要枚举。

[mooncake-store/include/client_metric.h:41-52](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/client_metric.h#L41-L52) —— 静态标签注入：从环境变量 `MC_STORE_CLUSTER_ID` 读集群 id，`merge_labels()` 把它合并进所有指标的静态标签，使多集群环境下能区分来源。

[mooncake-store/src/client_metric.cpp:188-221](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/client_metric.cpp#L188-L221) —— 周期上报线程：以 `MC_STORE_CLIENT_METRIC_INTERVAL`（秒）为周期，把 `summary_metrics()` + 区间带宽报告写进 glog。注意默认 interval=0 表示“采集但不周期打印”。

#### 4.4.4 代码实践

**实践目标**：让客户端指标周期性打印到日志，观察 Put/Get 吞吐。

**操作步骤**：

1. 启动 master（同 4.1.4）。
2. 以 5 秒为周期运行一个会做 put/get 的客户端示例，并开启周期上报：

   ```bash
   export MC_STORE_CLIENT_METRIC_INTERVAL=5
   export MC_STORE_CLIENT_METRIC_BANDWIDTH=true
   # 运行任意会调用 put/get 的示例程序
   ```

3. 观察客户端进程的 stderr/glog 输出。

**需要观察的现象**：每 5 秒打印一段 `Client Metrics Report`，包含 `=== Transfer Metrics Summary ===`（Total Read/Write、平均吞吐）、`=== Interface Operation Metrics Summary ===`（put/get 的 count/bytes/p95/max）。

**预期结果**：随着你不断 put/get，`Total Write` 单调增长，`Write Throughput` 反映区间速率。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：客户端指标的 `op_name` 标签可能有哪些取值？从哪里能确认？

**参考答案**：从调用 `ObserveTransferOperation` 的实参看，如 `"put"`、`"get"`、以及 batch 变体；具体集合由 `real_client.cpp` 里各处调用决定（4.4.3 的调用点之一用了 `"put"`）。可在 `real_client.cpp` 全文搜 `ObserveTransferOperation` 枚举。

**练习 2**：为什么客户端指标默认 `MC_STORE_CLIENT_METRIC_INTERVAL=0`（不周期打印）？

**参考答案**：采集本身开销很小（只是原子 inc/observe），但周期性打印摘要并算带宽会有一定 I/O 与计算开销；默认不打印避免在高并发推理路径上引入日志噪声，需要排障时再显式开启。

---

### 4.5 HA 指标精读：HAMetricManager

#### 4.5.1 概念说明

当 Store 开启 HA（主备）时，Standby 通过消费 Primary 写入 etcd 的 OpLog 来追平状态。这一层的健康度由 `HAMetricManager` 衡量：复制滞后多少、etcd 写失败/重试多少次、watch 断连多少次、Standby 状态机当前处于哪个状态。这些指标对“主备倒换会不会丢数据”至关重要。（背景见 [u7-l1 HA Leader/Standby](u7-l1-ha-leader-standby.md)。）

#### 4.5.2 核心流程

```text
Primary 侧：每写一条 OpLog ──► set_oplog_last_sequence_id(seq)
                              └─► observe_oplog_etcd_write_latency_us(us)
Standby 侧：每应用一条 OpLog ──► set_oplog_applied_sequence_id(seq)
                              ├─► inc_oplog_applied_entries()
                              └─► observe_oplog_apply_latency_us(us)
滞后 = last_sequence_id - applied_sequence_id ──► set_oplog_standby_lag(lag)
状态机变化 ──► set_standby_state(<枚举整数>) + inc_state_transitions()
```

Standby 状态机用整数编码：`0=STOPPED, 1=CONNECTING, 2=SYNCING, 3=WATCHING, 4=RECOVERING, 5=RECONNECTING, 6=PROMOTING, 7=PROMOTED, 8=FAILED`，这样 Prometheus 能直接当一个 Gauge 画。

#### 4.5.3 源码精读

[mooncake-store/src/ha_metric_manager.cpp:18-30](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha_metric_manager.cpp#L18-L30) —— OpLog 序列号与滞后都是 Gauge，命名 `ha_oplog_last_sequence_id` / `ha_oplog_applied_sequence_id` / `ha_oplog_standby_lag`：

```cpp
oplog_last_sequence_id_("ha_oplog_last_sequence_id", ...),
oplog_applied_sequence_id_("ha_oplog_applied_sequence_id", ...),
oplog_standby_lag_("ha_oplog_standby_lag",
                   "Number of OpLog entries Standby is behind Primary"),
```

[mooncake-store/src/ha_metric_manager.cpp:68-78](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha_metric_manager.cpp#L68-L78) —— HA 延迟桶，etcd 写从 100μs 到 5s（etcd 可能慢），apply 从 10μs 到 100ms（内存应用很快），两组桶针对各自量级单独设计。

[mooncake-store/src/ha_metric_manager.cpp:81-88](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha_metric_manager.cpp#L81-L88) —— `ha_standby_state` 的 HELP 里直接写明了 0~8 的枚举含义，看板里可据此加阈值告警（如 `==8` 表示 FAILED）。

[mooncake-store/include/ha_metric_manager.h:67-122](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/ha_metric_manager.h#L67-L122) —— HA 错误计数器接口，覆盖 skipped entries、checksum failures、gap resolve、etcd 写失败/重试、watch 断连、dropped put_end 等，几乎每种异常路径都有独立 Counter。

#### 4.5.4 代码实践

**实践目标**：纯源码阅读型实践——把 `ha_oplog_standby_lag` 这条指标的“定义 → 更新 → 序列化”三处串起来。

**操作步骤**：

1. **定义**：[ha_metric_manager.cpp:23-24](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha_metric_manager.cpp#L23-L24) 命名为 `ha_oplog_standby_lag`。
2. **更新**：[ha_metric_manager.cpp:116-118](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha_metric_manager.cpp#L116-L118) `set_oplog_standby_lag` 调用 `update()`。
3. **序列化**：[ha_metric_manager.cpp:271-273](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/ha_metric_manager.cpp#L271-L273) `serialize_metrics()` 里 `serialize_metric(oplog_standby_lag_)`。

**需要观察的现象**：理解一条 Gauge 指标的“三段式”生命周期。

**预期结果**：能画出 `set_xxx → update → serialize` 的调用链。

#### 4.5.5 小练习与答案

**练习 1**：`ha_oplog_standby_lag` 持续大于 0 且不收敛，可能意味着什么？

**参考答案**：Standby 应用 OpLog 的速度跟不上 Primary 写入的速度，可能因 etcd 抖动、Standby 负载过高或 apply 路径阻塞。如果长期不收敛，主备倒换时可能丢失尚未追平的写入，需要告警。

**练习 2**：为什么把 Standby 状态机编码成整数 Gauge 而不是用字符串？

**参考答案**：Prometheus 指标值只能是数值，标签才用字符串；把状态编成整数即可直接作为时间序列画图/设阈值（如 `ha_standby_state == 8` 告警），字符串状态则只能在日志里看。HELP 文本里附带枚举说明方便人解读。

---

### 4.6 Prometheus + Grafana 监控栈

#### 4.6.1 概念说明

`monitoring/` 目录提供了一套开箱即用的监控栈：用 Docker Compose 同时拉起 **Prometheus**（时序数据库 + 抓取器，端口 9090）和 **Grafana**（可视化看板，端口 3000）。Prometheus 按 `prometheus.yml` 的配置，周期性地去 Master 的 `/metrics` 端点拉取指标；Grafana 通过“provisioning”机制自动配置好数据源和示例看板，无需手动在 UI 里点。

整套链路是：

```text
mooncake_master(:9003 /metrics)  ──pull──►  Prometheus(:9090)  ──query──►  Grafana(:3000 看板)
```

#### 4.6.2 核心流程

1. `docker-compose up -d` 启动两个容器（同一 `monitoring` 网络）。
2. Prometheus 读 `prometheus.yml`，发现要抓 `host.docker.internal:9003`（即宿主机上的 master），每 5s 抓一次。
3. Grafana 启动时读 `grafana/provisioning/datasources/` 自动建好 Prometheus 数据源，读 `grafana/provisioning/dashboards/` 自动加载 `mooncake.json` 看板。
4. 用户访问 `http://localhost:3000`（admin/admin）即可看板。

#### 4.6.3 源码精读

[monitoring/docker-compose.yml:4-20](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/monitoring/docker-compose.yml#L4-L20) —— Prometheus 容器：挂载 `prometheus.yml`，设 `--storage.tsdb.retention.time=15d`（保留 15 天），并用 `extra_hosts: host.docker.internal:host-gateway` 让容器能访问宿主机服务（Linux 上这一行很关键）。

[monitoring/docker-compose.yml:22-39](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/monitoring/docker-compose.yml#L22-L39) —— Grafana 容器：默认账号 admin/admin，挂载 provisioning 目录与 dashboards 目录，`depends_on: prometheus`。

[monitoring/prometheus/prometheus.yml:10-13](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/monitoring/prometheus/prometheus.yml#L10-L13) —— 抓取配置：job 名 `mooncake-master`，目标 `host.docker.internal:9003`，抓取间隔 5s（比全局 15s 更密，因为 master 状态变化快）：

```yaml
- job_name: 'mooncake-master'
  scrape_interval: 5s
  static_configs:
    - targets: ['host.docker.internal:9003']
```

[monitoring/grafana/provisioning/datasources/prometheus.yml:3-8](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/monitoring/grafana/provisioning/datasources/prometheus.yml#L3-L8) —— 自动建数据源，URL 指向容器名 `http://prometheus:9090`（同网络内可解析），设为默认。

[monitoring/grafana/provisioning/dashboards/dashboards.yml:3-11](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/monitoring/grafana/provisioning/dashboards/dashboards.yml#L3-L11) —— 告诉 Grafana 从 `/etc/grafana/dashboards` 目录加载 JSON 看板。

**⚠️ 一个真实存在的坑**：[monitoring/grafana/dashboards/mooncake.json:31-37](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/monitoring/grafana/dashboards/mooncake.json#L31-L37) —— 示例看板唯一的面板查询的是 `rate(mooncake_master_rpc_requests_total[5m])`，但**主端指标根本没有这个名字**！对照 [master_metric_manager.cpp:66-77](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_metric_manager.cpp#L66-L77)，真实指标名是 `master_put_start_requests_total` / `master_get_replica_list_requests_total` 等（前缀 `master_`，不是 `mooncake_master_`，也没有统一的 `rpc_requests_total`）。所以这个示例面板默认是空的，必须按真实指标名改写查询。这是 4.6.4 的实践内容。

#### 4.6.4 代码实践

**实践目标**：修复示例看板的指标名，让面板真正显示数据。

**操作步骤**：

1. 编辑 `monitoring/grafana/dashboards/mooncake.json`，把面板 `expr` 改成真实存在的指标。例如把单条改为多条 Get/Put 速率：

   ```json
   "targets": [
     {"expr": "rate(master_get_replica_list_requests_total[5m])", "legendFormat": "Get", "refId": "A"},
     {"expr": "rate(master_put_end_requests_total[5m])",          "legendFormat": "PutEnd", "refId": "B"}
   ]
   ```

2. 重启 grafana 容器（或等其自动重载 provisioning）：`docker restart grafana`。

**需要观察的现象**：访问 `http://localhost:3000`，打开 Mooncake Master 看板，面板里出现 Get/Put 的每秒速率曲线。

**预期结果**：当你对 master 发起 get/put 负载时，曲线上升；停止后回落。**待本地验证**（依赖 Docker 与运行中的 master）。

#### 4.6.5 小练习与答案

**练习 1**：在 Linux 上若 Prometheus 抓不到 master，最可能的原因是什么？

**参考答案**：`host.docker.internal` 在 Linux 上默认不可解析。`docker-compose.yml` 里已经加了 `extra_hosts: ["host.docker.internal:host-gateway"]` 来解决；如果你用的是裸 `docker run`，[monitoring/run.sh:25](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/monitoring/run.sh#L25) 里也显式带了 `--add-host host.docker.internal:host-gateway`。缺了这行，Prometheus 容器无法连到宿主机的 9003 端口。

**练习 2**：抓取间隔为什么 master 用 5s 而全局默认 15s？

**参考答案**：master 的请求速率、内存利用率等指标变化快，5s 抓取能在看板上呈现更平滑、更及时的曲线；而像 Prometheus 自监控这种慢变量用 15s 足够，可减少存储压力。

---

## 5. 综合实践

把本讲内容串成一个完整的“上线监控”小任务。

**目标**：启动 Prometheus + Grafana 栈，对运行中的 Store Master 抓取指标，在 Grafana 里观察 **Put/Get 吞吐**与**内存利用率**，并对照源码说明这些指标分别在哪段代码被更新。

**步骤**：

1. **编译并启动 master**（开启指标端点）：

   ```bash
   ./build/mooncake_master --metrics_port=9003 --enable_metric_reporting=true
   ```

2. **验证端点**：`curl -s http://localhost:9003/metrics | grep -E 'master_put_end_requests_total|master_allocated_bytes'`，确认有输出。

3. **启动监控栈**：

   ```bash
   cd monitoring && docker-compose up -d
   ```

4. **确认抓取成功**：访问 `http://localhost:9090` → Status → Targets，`mooncake-master` job 应为 UP。

5. **制造负载**：用一个客户端示例或 benchmark（如 `mooncake-store/benchmarks/`）持续 put/get。

6. **在 Grafana 建两条查询**（`http://localhost:3000`，admin/admin）：
   - Put/Get 吞吐：`rate(master_put_end_requests_total[5m])` 与 `rate(master_get_replica_list_requests_total[5m])`。
   - 内存利用率：`master_allocated_bytes / master_total_capacity_bytes`。

7. **对照源码写一份“指标溯源”**：
   - `master_put_end_requests_total` → [rpc_service.cpp:254](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/rpc_service.cpp#L254) 的 `inc_put_end_requests()`，命名在 [master_metric_manager.cpp:74-75](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_metric_manager.cpp#L74-L75)。
   - `master_allocated_bytes` → Gauge，由业务代码在分配/释放时 inc/dec，命名在 [master_metric_manager.cpp:24-26](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master_metric_manager.cpp#L24-L26)。
   - `master_get_replica_list_requests_total` → [rpc_service.cpp:172](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/rpc_service.cpp#L172) 的 `inc_get_replica_list_requests()`。

**预期结果**：负载期间吞吐曲线有值、内存利用率随 put 上升随 evict/remove 下降；源码溯源能对上号。**待本地验证**（依赖编译、Docker 与 RDMA/网络环境；无 RDMA 时可用 TCP 传输验证链路）。

---

## 6. 本讲小结

- Mooncake Store 有**三层互相独立的指标体系**：客户端（`ClientMetric`，按实例，写 glog）、主端（`MasterMetricManager`，单例，HTTP `/metrics`）、HA（`HAMetricManager`，单例，拼接进 `/metrics`）。
- 四种指标类型各有分工：**Counter** 计累计、**Gauge** 计瞬时值、**Histogram** 分桶算分位数（可聚合）、**Summary** 客户端算精确分位数（不可聚合）。
- **混合指标（hybrid）** 把静态标签（如 `cluster_id`）预格式化、动态标签（如 `rpc_name`/`op_name`）运行时拼接，兼顾区分性与开销。
- 主端计数器主要在 `rpc_service.cpp` 的 `execute_rpc` 包装层更新（成功/失败两个回调）；客户端在 `real_client.cpp` 经 `execute_timed_operation` 回调 `ObserveTransferOperation`。
- `/metrics` 端点由 `master_admin_service.cpp` 的 HTTP 服务提供，默认 9003 端口，输出 Prometheus 文本格式。
- `monitoring/` 提供开箱即用的 Prometheus(9090) + Grafana(3000) 栈，经 `host.docker.internal` 抓取 master；但**仓库自带的示例看板指标名不匹配**（`mooncake_master_rpc_requests_total` 实际不存在），需对照源码改成 `master_*` 真实名。

## 7. 下一步学习建议

- 想深入“主备如何用 OpLog 追平状态”以及 HA 指标背后的真实含义，读 [u7-l1 HA Leader/Standby](u7-l1-ha-leader-standby.md)。
- 想理解被指标衡量的那些操作（PutStart/PutEnd/GetReplicaList/eviction/promotion）本身，复习 [u5-l2 Master 服务](u5-l2-master-service.md)、[u6-l3 淘汰与租约](u6-l3-eviction-pin-lease.md)、[u6-l4 卸载与提升](u6-l4-offload-promotion.md)。
- 继续阅读源码：`mooncake-store/src/master_metric_manager.cpp` 的 `serialize_metrics()` / `get_summary_string()` 看主端摘要如何拼装；`mooncake-store/src/ha_metric_manager.cpp` 看 HA 摘要。
- 进阶实践：把客户端指标也接入 Prometheus（目前默认只走 glog），可在客户端进程额外起一个 HTTP 端点暴露 `ClientMetric::serialize()`，并把它作为新 scrape job 加入 `prometheus.yml`。

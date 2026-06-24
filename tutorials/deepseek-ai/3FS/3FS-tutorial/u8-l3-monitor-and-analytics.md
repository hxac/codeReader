# 监控、指标与 analytics

## 1. 本讲目标

3FS 是一个跑在几十上百个节点上的分布式系统。当集群出现「某个 storage 节点读延迟飙升」「meta 事务冲突重试变多」这类问题时，光靠人肉看日志是扛不住的——你需要一套**能跨节点聚合、能按时间序列查询**的可观测体系。

本讲讲解 3FS 的两套可观测机制，学完后你应当能够：

1. 画出一条指标从「业务代码里 `addSample`」到「ClickHouse 里一行记录」的完整采集链路，说清沿途每个组件的职责。
2. 区分三种 reporter（`clickhouse` / `log` / `monitor_collector`），理解 `monitor_collector` 作为**中央聚合器**的角色，并知道如何在 `admin_cli` 与各服务的 TOML 里配置监控地址。
3. 读懂 `analytics` 目录下的 `StructuredTraceLog` / `SerdeObjectWriter` / `SerdeObjectReader`，理解 3FS 如何把任意一个 serde 结构体（如 `MetaEventTrace`）周期性地落成 Parquet 文件，并在事后逐行读回，用于结构化 trace 分析。

本讲覆盖三个最小模块：**指标采集**、**ClickHouse 上报**、**结构化 trace**。

## 2. 前置知识

- **本讲依赖 `u1-l3`（部署与 admin_cli）**。你已经知道 3FS 集群由 monitor / admin_cli / mgmtd / meta / storage / FUSE client 等进程组成，并知道运行时配置由 mgmtd 托管。
- **监控是「旁路」的**：它不在数据热路径上。采集、聚合、上报都发生在独立线程里，业务线程只做一次极轻量的 `addSample`（见后文）。
- **什么是时序指标（time-series metric）**：一条指标 = `(名称, 一组标签 tags, 时间戳, 值)`。其中「值」分两类——计数器（counter，一个整数，如「过去 1 秒读了多少字节」）和分布（distribution，如「过去 1 秒所有读请求延迟的 p50/p99」）。3FS 把这两类分别落到 ClickHouse 的两张表里。
- **什么是 ClickHouse**：一个面向 OLAP（在线分析）的列式数据库，擅长对海量时序数据做聚合查询（group by、percentile）。3FS 用它存监控指标。
- **什么是 Parquet**：一种列式存储文件格式，带类型 schema 和压缩，适合把结构化记录批量落盘后再离线分析。3FS 用它存结构化 trace。
- **serde**：3FS 的序列化框架（见 `u2-l2`）。`analytics` 的妙处在于「复用 serde 的字段反射」自动生成 Parquet 列。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/common/monitor/Recorder.h` | 指标「探针」的基类与各类实现：`CountRecorder`（计数）、`LatencyRecorder`/`DistributionRecorder`（分布/延迟）、`ValueRecorder`（瞬时值）、`OperationRecorder`（操作计数+延迟+失败）。业务代码用它们打点。 |
| `src/common/monitor/Monitor.h` / `.cc` | 每个进程内嵌的 `MonitorInstance`：持有全局 `Collector`，周期性采集所有 Recorder，分发给若干 `Reporter`。 |
| `src/common/monitor/Sample.h` | 一条指标的数据结构 `Sample`（name/tags/timestamp/value）及 `TagSet`、`Distribution`。 |
| `src/common/monitor/Reporter.h` | Reporter 抽象接口 `init()` / `commit(samples)`。 |
| `src/common/monitor/MonitorCollectorClient.*` | Reporter 之一：把本地指标经 RPC 发给中央 `monitor_collector`。 |
| `src/common/monitor/ClickHouseClient.cc` | Reporter 之一：把指标直接写入 ClickHouse 的 `counters` / `distributions` 表。 |
| `src/monitor_collector/monitor_collector.cpp` | 中央聚合器进程入口。 |
| `src/monitor_collector/service/*` | 聚合器服务：接收 RPC、批量合并、再交给 ClickHouse。 |
| `deploy/sql/3fs-monitor.sql` | ClickHouse 建表 DDL（`counters` / `distributions`）。 |
| `configs/monitor_collector_main.toml` | 聚合器的部署配置（端口、ClickHouse 连接、批处理参数）。 |
| `src/analytics/StructuredTraceLog.h` | 结构化 trace 的核心：把任意 serde 结构体周期性落成 Parquet。 |
| `src/analytics/SerdeObjectWriter.h` / `SerdeObjectReader.h` | Parquet 的写端 / 读端，靠 serde 反射把结构体映射成列。 |
| `src/meta/event/Event.h` | 一个真实的 trace 结构体 `MetaEventTrace`，作为讲解范例。 |

## 4. 核心概念与源码讲解

### 4.1 指标采集：Recorder / Collector / Sample（在「每个服务」进程内）

#### 4.1.1 概念说明

3FS 的指标采集是**进程内、去中心化**的：mgmtd、meta、storage、FUSE client **每个进程**都各自跑着一个监控实例，本地采集、本地打包。要理解它，先记住三件事：

- **打点（Recorder）**：业务代码想记录「某事发生了」，就构造一个 `Recorder` 子类对象，它是进程内的一个全局变量。例如「统计队列里有多少待处理样本」就是一个 `CountRecorder`。
- **收集（Collector）**：一个全局的 `Collector` 持有一张 `Recorder` 注册表，一个专门的线程**周期性地**（默认 1 秒）把它们当前的值收集成一批 `Sample`。
- **上报（Reporter）**：另一个线程把这一批 `Sample` 交给一个或多个 `Reporter`。Reporter 的种类决定了指标最终去哪儿（ClickHouse、日志文件、或中央 collector）。

关键设计取舍：业务线程**绝不能阻塞**在监控上。所以 `Recorder` 内部用**线程局部存储（TLS）+ 原子变量**累加，采集时才汇总，避免多线程竞争。

#### 4.1.2 核心流程

```
业务线程:  recorder.addSample(1)            ┐
                                          │  TLS/原子累加，零竞争
                                          ┘
Collector 线程（每 collect_period，默认 1s）:
  for 每个 Recorder:
    sample = recorder.collect()   // 汇总 TLS，附带 host/pod 标签
    → Sample{name, tags, now, value}
  一批 Sample 入队 samplesQueue_

Reporter 线程:
  从 samplesQueue_ 取一批
  for 每个 reporter: reporter->commit(samples)
```

其中 `value` 是一个 `variant<int64_t, Distribution>`：整数走计数器语义，分布（含 p50/p99）走 distribution 语义。

#### 4.1.3 源码精读

**① 业务侧的打点入口。** `CountRecorder` 用 TLS 在每个线程里累加，采集时把所有线程的 TLS 汇总——这是「零竞争」的关键。[src/common/monitor/Recorder.h:L96-L154](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/monitor/Recorder.h#L96-L154) 说明了 TLS 结构与 `sum_` 原子累加。项目里 `CountRecorder` 就是它的别名：

[src/common/monitor/Recorder.h:L159](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/monitor/Recorder.h#L159) 把 `CountRecorder` 定义为带共享线程局部标签的计数器。延迟类指标用基于 TDigest 的 `LatencyRecorder`（[Recorder.h:L193-L209](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/monitor/Recorder.h#L193-L209)），它继承自 `DistributionRecorder`，能在很小的内存开销下估算百分位。一个真实打点例子见聚合器自身：`monitor_collector.num_queueing_samples`——每收到一批样本 `+1`、每消费一批 `-1`：

[src/monitor_collector/service/MonitorCollectorOperator.cc:L7](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/monitor_collector/service/MonitorCollectorOperator.cc#L7) 声明了一个全局 `CountRecorder`，这就是「打点」的全部写法。

**② 一条指标的数据结构。** [src/common/monitor/Sample.h:L96-L109](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/monitor/Sample.h#L96-L109) 定义了 `Sample`：`name`（指标名）、`tags`（`TagSet`，标签集）、`timestamp`、`value`（整数或 `Distribution`）。`Distribution`（[Sample.h:L80-L94](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/monitor/Sample.h#L80-L94)）含 `cnt/sum/min/max/p50/p90/p95/p99`。

**③ 采集时给样本贴 host/pod 标签。** `CountRecorder::collect` 在产出 `Sample` 前会把全局 hostname/podname 拼进 `TagSet`：

[src/common/monitor/Recorder.cc:L78-L90](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/monitor/Recorder.cc#L78-L90) 展示了 `collect` 汇总 TLS 的 `sum_`，并 `addTag("host", ...)` / `addTag("pod", ...)`。这些标签就是后面 ClickHouse 表里的 `host` / `pod` 列，让你能按节点筛选。

**④ 周期采集 + 上报的两个线程。** `MonitorInstance` 在 `start()` 里为每个 collector 起一对线程：`periodicallyCollect`（采集线程）和 `reportSamples`（上报线程），中间用一个有界队列 `samplesQueue_` 解耦。

[src/common/monitor/Monitor.cc:L119-L153](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/monitor/Monitor.cc#L119-L153) 是采集主循环：每 `collect_period` 调一次 `collectAll`，把结果 `write` 进队列并唤醒上报线程；每 300 秒（`kCleanPeriod`）做一次 `cleanInactive` 清理不再活跃的带标签 Recorder。[src/common/monitor/Monitor.cc:L155-L167](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/monitor/Monitor.cc#L155-L167) 是上报循环：从队列取一批，对每个 reporter 调 `commit`。

**⑤ 谁启动了这套监控？** 每个服务进程在启动时都会 `Monitor::start(cfg.monitor(), ...)`：

[src/common/app/Utils.cc:L173](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/Utils.cc#L173) 是所有 net::Server 类服务统一拉起监控的入口，`nodeId != 0` 时还会给 hostname 追加 `Node_N` 后缀以区分同机多实例。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：跟踪一个打点从「业务调用」到「进入上报队列」的链路。
2. **操作步骤**：
   - 打开 [MonitorCollectorOperator.cc:L7](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/monitor_collector/service/MonitorCollectorOperator.cc#L7)，确认 `numQueueingSamples` 是个全局 `CountRecorder`。
   - 在该文件内搜索 `numQueueingSamples.addSample`，确认它在 `write()`（入队 +1）和 `connThreadFunc`（出队 -1）里被调用。
   - 顺着 `CountRecorder` → `CountRecorderWithTLSTag::collect`（[Recorder.cc:L78](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/monitor/Recorder.cc#L78)）→ `Collector::collectAll`（[Monitor.cc:L83](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/monitor/Monitor.cc#L83)）→ `periodicallyCollect`（[Monitor.cc:L119](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/monitor/Monitor.cc#L119)）画出时序。
3. **需要观察的现象**：业务线程只做 `val_ += val`（TLS），**完全不进入**采集/上报逻辑；采集与上报由独立的 `Collector`/`Reporter` 线程完成。
4. **预期结果**：你能解释为什么这套设计在高并发下不会拖慢数据热路径。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CountRecorder` 用 TLS 累加，而不是直接给一个共享的 `std::atomic` 做 `fetch_add`？

**参考答案**：高频打点下，所有线程对同一个 cache line 的原子操作会引发严重的 cache 争用（cache line ping-pong）。TLS 让每个线程在自己私有的 cache line 上累加，采集时才一次性 `exchange(0)` 汇总，把争用从「每次打点」降到「每秒采集一次」。

**练习 2**：`Sample::value` 为什么用 `variant<int64_t, Distribution>` 而不是统一用一个结构体？

**参考答案**：计数器和分布的存储与查询语义不同。整数计数适合累加、求和；分布适合取百分位。3FS 把它们分开存进 ClickHouse 的两张表（`counters` / `distributions`），列结构不同、查询方式也不同，`variant` 在内存里用同一载体承载两类值。

---

### 4.2 ClickHouse 上报：monitor_collector 聚合器与表结构

#### 4.2.1 概念说明

上一节说每个进程都有 reporter。3FS 提供三种 reporter（见 [Monitor.h:L42-L47](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/monitor/Monitor.h#L42-L47)）：

| reporter 类型 | 去向 | 适用场景 |
|---------------|------|----------|
| `clickhouse` | 直接写本机/就近的 ClickHouse | 单节点、轻量 |
| `log` | 写到日志文件 | 调试、无 ClickHouse 环境 |
| `monitor_collector` | 经 RPC 发给中央聚合器 | **生产部署的标准做法** |

生产里，几十上百个 storage / meta / mgmtd 节点**不各自直连 ClickHouse**，而是统一把指标发给一个（或一组 VIP 后的多个）**`monitor_collector` 进程**，由它聚合后再批量写 ClickHouse。这样做的好处：

- 每个业务节点只需维护**一条**到 collector 的 TCP 连接，而不是每个节点都开 ClickHouse 连接；
- collector 做批量合并（`batch_commit_size`，默认 4096），摊薄 ClickHouse 的写入次数；
- collector 可水平扩展（多个实例挂在一个 VIP 后分摊流量，见部署说明）。

所以你要建立的心智模型是：**业务进程 = 生产者；monitor_collector = 中央聚合器 + ClickHouse 客户端**。两者通过 `MonitorCollector.write` 这个 RPC 连起来。

#### 4.2.2 核心流程

```
[storage 节点]                          [monitor_collector 进程]            [ClickHouse]
CountRecorder ...                          MonitorCollectorService
  ↓ collect (1s)                            .write(samples)   ← RPC (serviceId 194)
MonitorCollectorClient.commit ──TCP 10000──→ Operator.write ──→ sampleQueue_ (MPMC, 204800)
                                              ↓ connThread (×32) 批量合并 ≤4096
                                              过滤 blacklisted_metric_names
                                              ClickHouseClient.commit ──→ counters / distributions 表
```

批量合并的摊薄效应：若 N 个样本被攒成 1 次写入，则每样本的写入开销近似为

\[
\text{每样本开销} \approx \frac{C_{\text{write}}}{N} + C_{\text{per-row}}
\]

\(C_{\text{write}}\) 是一次 INSERT 的固定开销（建连、协议往返），\(N\) 越大摊得越薄——这正是 collector 把队列里最多 `batch_commit_size` 个样本攒成一批再 commit 的动机。

#### 4.2.3 源码精读

**① 生产者侧：`MonitorCollectorClient` 把一批 Sample 发成一次 RPC。** [src/common/monitor/MonitorCollectorClient.cc:L21-L25](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/monitor/MonitorCollectorClient.cc#L21-L25) 的 `commit` 就是调用 `MonitorCollector.write(ctx, samples)`。RPC 定义在 [src/fbs/monitor_collector/MonitorCollectorService.h:L13](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/monitor_collector/MonitorCollectorService.h#L13)：`SERDE_SERVICE_METHOD(write, 1, std::vector<Sample>, MonitorCollectorRsp)`，即 serviceId=194、methodId=1。生产者侧的配置只需一个 `remote_ip`（[MonitorCollectorClient.h:L15](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/monitor/MonitorCollectorClient.h#L15)）。

**② 聚合器入口。** [src/monitor_collector/monitor_collector.cpp:L5-L7](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/monitor_collector/monitor_collector.cpp#L5-L7) 是进程 `main`，注意它用的是**更轻量的 `OnePhaseApplication`**（单阶段），区别于 meta/storage/mgmtd 的 `TwoPhaseApplication` 两阶段骨架（见 u2-l1）——因为 monitor_collector 不需要联系 mgmtd、不参与集群成员管理。服务定义见 [MonitorCollectorServer.h:L8-L34](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/monitor_collector/service/MonitorCollectorServer.h#L8-L34)：固定监听 **TCP 10000**（控制面用 TCP，与数据面的 RDMA 区分开），`beforeStart` 里创建 Operator 并注册 serde 服务（[MonitorCollectorServer.cc:L13-L17](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/monitor_collector/service/MonitorCollectorServer.cc#L13-L17)）。

**③ 收到 RPC 后入队。** [MonitorCollectorService.cc:L9-L12](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/monitor_collector/service/MonitorCollectorService.cc#L9-L12) 的 `write` 把样本 `blockingWrite` 进 `sampleQueue_`（一个 `folly::MPMCQueue`，容量 `queue_capacity` 默认 204800，见 [MonitorCollectorService.h:L18-L26](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/monitor_collector/service/MonitorCollectorService.h#L18-L26)）。这把「收 RPC」和「写 ClickHouse」解耦——即使 ClickHouse 短暂变慢，队列也能吸收突发。

**④ 多线程批量合并 + 写 ClickHouse。** 这是聚合器的核心，[MonitorCollectorOperator.cc:L35-L86](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/monitor_collector/service/MonitorCollectorOperator.cc#L35-L86)：

- 启动时按 `conn_threads`（默认 32）个连接线程 + 1 个监控线程（[L13-L17](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/monitor_collector/service/MonitorCollectorOperator.cc#L13-L17)）；
- 每个连接线程按 `reporter.type` 创建一个 `Reporter`（默认 `clickhouse`），即一个 `ClickHouseClient`；
- 主循环从队列取一批，再尽力多取（最多 `batch_commit_size - 1` 批）合并成一大批，**过滤掉黑名单指标**（`blacklisted_metric_names`），最后 `reporter->commit(samples)`。

另有一个 `monitorThreadFunc`（[L88-L94](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/monitor_collector/service/MonitorCollectorOperator.cc#L88-L94)）每 5 秒打印队列水位，方便发现积压。

**⑤ ClickHouse 写入：拆分计数器/分布、按列拼 Block。** [ClickHouseClient.cc:L64-L160](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/monitor/ClickHouseClient.cc#L64-L160) 的 `commit` 先按 `sample.isNumber()` 把样本分成 `counters` 与 `distributions` 两组，分别构造列（`TIMESTAMP`/`metricName`/`val` 或 `TIMESTAMP`/`metricName`/`count`/`mean`/`min`/`max`/`p50..p99`），再调用 `createTagColumns`（[L32-L62](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/monitor/ClickHouseClient.cc#L32-L62)）把所有样本出现过的 tag key 各拼成一列，最后 `client_->Insert("counters"/"distributions", block)`。出错时置 `errorHappened_`，下次插入前 `ResetConnection` 重连。

**⑥ ClickHouse 表结构。** [deploy/sql/3fs-monitor.sql:L1-L51](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/sql/3fs-monitor.sql#L1-L51) 定义了两张 `MergeTree` 表，要点：

- 主键 / 排序键都是 `(metricName, host, pod, instance, TIMESTAMP)`——按指标名+节点聚簇，时序查询快；
- `PARTITION BY toDate(TIMESTAMP)` 按天分区，`TTL TIMESTAMP + toIntervalMonth(1)` 自动清理 1 个月前的数据；
- `host/pod/io/uid/thread/statusCode` 等标签列用 `LowCardinality(String)`（枚举压缩），几乎所有列都 `CODEC(ZSTD(1))`，`TIMESTAMP` 用 `DoubleDelta`——这是 ClickHouse 针对时序数据的标准压缩组合。

**⑦ 聚合器自身的部署配置。** [configs/monitor_collector_main.toml:L128-L141](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/configs/monitor_collector_main.toml#L128-L141) 给出 `batch_commit_size=4096`、`conn_threads=32`、`queue_capacity=204800`，以及 reporter 的 ClickHouse 连接占位（`host/db/user/passwd/port`，部署时填）。端口 10000 与 TCP 见 [L75-L88](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/configs/monitor_collector_main.toml#L75-L88)。各业务服务侧的配置则是 `[common.monitor.reporters.monitor_collector] remote_ip = "<collector>:10000"`，例如 storage（[configs/storage_main.toml:L48-L56](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/configs/storage_main.toml#L48-L56)）。

#### 4.2.4 代码实践（部署型，需本地/测试环境）

> 以下命令取自 `deploy/README.md`，是真实部署流程；若当前环境无 ClickHouse，标注「待本地验证」的部分请到测试集群执行。

1. **实践目标**：让 monitor_collector 把一组 storage 指标写进 ClickHouse，并在 ClickHouse 里查到它。
2. **操作步骤**：
   - **建库建表**（一次性）：按 [deploy/README.md:L57-L60](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L57-L60) 执行 `clickhouse-client -n < ~/3fs/deploy/sql/3fs-monitor.sql`。
   - **配 collector → ClickHouse**：编辑 `monitor_collector_main.toml` 的 `[server.monitor_collector.reporter.clickhouse]`，填入 host/db/user（见 [README.md:L74-L85](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L74-L85)）。
   - **启动 collector**：`systemctl start monitor_collector_main`（[README.md:L86-L90](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L86-L90)）。
   - **配业务服务 → collector**：在 storage/meta/mgmtd/fuse 的 `*_main.toml` 里设 `[common.monitor.reporters.monitor_collector] remote_ip = "<collector_ip>:10000"`（示例见 [README.md:L242-L245](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L242-L245)），重启服务。
   - **查询**（待本地验证）：
     ```sql
     SELECT metricName, host, val, TIMESTAMP
     FROM 3fs.counters
     WHERE TIMESTAMP > now() - INTERVAL 5 MINUTE
     ORDER BY TIMESTAMP DESC LIMIT 50;
     ```
3. **需要观察的现象**：约 1 秒（`collect_period`）后，ClickHouse 里开始出现以 `storage.*`、`meta.*` 为前缀的指标行，`host` 列能区分不同节点。
4. **预期结果**：`counters` 表有计数类指标、`distributions` 表有延迟类指标（p50/p99 等）。若查不到，按「业务服务 `remote_ip` → collector 10000 端口可达性 → collector 日志 `/var/log/3fs/monitor_collector_main.log` → ClickHouse 连接」顺序排查。

> **admin_cli 关联**：`admin_cli` 本身是瘦客户端，监控地址在它调用的各服务配置里。`list-nodes` 等命令查到节点后，可结合该节点的 `[common.monitor.reporters.monitor_collector]` 确认它把指标发往哪个 collector——这是「指标去哪了」与「节点是谁」的交叉验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 collector 要起 32 个 `conn_thread`，而不是 1 个？

**参考答案**：写 ClickHouse 是同步阻塞操作（建连、网络往返、服务端写入）。若只有一个线程，一旦某次 INSERT 慢，后续所有节点的样本都会堆在 `sampleQueue_` 里积压。多个连接线程既能并发写 ClickHouse 提高吞吐，又能让单次慢写入不阻塞其他线程的消费。它们共享同一个 `MPMCQueue`，故能安全并发取队。

**练习 2**：`blacklisted_metric_names` 这个配置有什么用？

**参考答案**：它让 collector 在写 ClickHouse **之前**丢弃指定名称的指标。适合在不出 rebuild 集群的前提下，临时屏蔽某个噪声大、体量大或敏感的指标，控制 ClickHouse 写入量和存储成本。见 [MonitorCollectorOperator.cc:L69-L74](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/monitor_collector/service/MonitorCollectorOperator.cc#L69-L74) 的 `remove_if` 过滤。

**练习 3**：`counters` 表为什么把 `metricName, host, pod` 放在排序键最前面？

**参考答案**：监控查询几乎都是「先选某指标名、再筛某节点、再看时间趋势」。把高频过滤维度放在 MergeTree 排序键前缀，使这些查询能快速定位到一小段连续的 granule（`index_granularity=8192`），避免全表扫描。

---

### 4.3 结构化 trace：analytics 的 Parquet 落盘与 SerdeObjectReader

#### 4.3.1 概念说明

上一节的「指标」是**聚合后**的数值（每秒一条），适合看趋势。但有时你需要**逐条事件**的明细——例如「这个 rename 操作具体改了哪个 inode、发生在哪个时刻」。这种「每条记录是一行结构化数据」的需求，就是 3FS `analytics` 目录要解决的：**结构化 trace**。

它的核心思想很巧妙：**复用 serde 的字段反射**。3FS 里几乎所有数据结构都用 `SERDE_STRUCT_FIELD` 声明字段并自带反射元信息（见 u2-l2）。`analytics` 提供一个模板类 `StructuredTraceLog<SerdeType>`：只要你给它任意一个 serde 结构体 `T`，它就能：

1. 反射出 `T` 的所有字段 → 自动推导出 Parquet 的列 schema；
2. 周期性地把一拨 `T` 实例按行写成 `.parquet` 文件；
3. 事后用对称的 reader 把 `.parquet` 逐行读回成 `T`。

「写」用 `SerdeObjectWriter`，「读」用 `SerdeObjectReader`，二者靠同一套 `visit()` 重载分派字段类型。一个真实例子：meta 服务把每个元数据事件（create / remove / rename …）记成 `MetaEventTrace`（[src/meta/event/Event.h:L50-L72](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/event/Event.h#L50-L72)）落盘。

#### 4.3.2 核心流程

```
业务代码:  traceLog.newEntry()  →  shared_ptr<MetaEventTrace>（析构时自动 append）
                                       ↓
StructuredTraceLog::append(msg):
   包成 StructuredTrace{trace_meta{ts,hostname}, _=msg}
   << writer (一个 SerdeObjectWriter，写 Parquet)
   每 dump_interval（release 30s / debug 60min）触发 flush：换文件、ZSTD 压缩

事后离线:
   SerdeObjectReader::open(path)  →  parquet::StreamReader
   while (reader >> obj) { ...逐行拿到 MetaEventTrace... }
```

文件落盘路径形如 `<trace_file_dir>/<日期>/<host>/<类型>.<host>.<时间戳>.<序号>.parquet`（见 `createNewWriter`）。

#### 4.3.3 源码精读

**① trace 日志主体：`StructuredTraceLog`。** 它是模板类，对外只关心「你给的 serde 类型」。配置项全部热更新（见 [StructuredTraceLog.h:L33-L45](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/StructuredTraceLog.h#L33-L45)）：`trace_file_dir`（落盘目录）、`enabled`、`dump_interval`（debug 默认 60 分钟、release 默认 30 秒）、`max_num_writers`、`max_row_group_length`。

每条记录被包成带 `trace_meta`（时间戳 + 主机名）的 `StructuredTrace`（[StructuredTraceLog.h:L24-L27](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/StructuredTraceLog.h#L24-L27)），再 `<< writer` 写入。`append`（[L92-L122](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/StructuredTraceLog.h#L92-L122)）在写满一个周期（`nextDumpTime_`）后异步 `flush`。`flush`（[L124-L195](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/StructuredTraceLog.h#L124-L195)）用一个 `test_and_set` 标志保证同一时刻只有一个 flush 在跑，逐个关闭旧 writer、打开新 writer（按时间滚动文件）。`createNewWriter`（[L221-L240](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/StructuredTraceLog.h#L221-L240)）拼出按 `<日期>/<host>/<类型>.<host>.<ts>.<idx>.parquet` 分层的文件名。

**② 写端：`SerdeObjectWriter`。** [SerdeObjectWriter.h:L24-L78](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/SerdeObjectWriter.h#L24-L78) 的 `open` 用 `SerdeSchemaBuilder<SerdeType>` 从 serde 反射出 Arrow schema，建一个 ZSTD 压缩的 Parquet writer。`operator<<`（[L80-L92](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/SerdeObjectWriter.h#L80-L92)）调 `visit("", v)` 逐字段写列，末尾 `EndRow`。字段类型分派很有讲究（[L103-L227](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/SerdeObjectWriter.h#L103-L227)）：

- 算术类型 → 直接写数值列；枚举 → 写成 `int32`；字符串 → 字符串列；
- `Result<T>`（即 `folly::Expected`）→ 拆成「值列」+「`<key>Error` 后缀列」记录错误码（[L183-L198](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/SerdeObjectWriter.h#L183-L198)）；
- `variant<Ts...>` → 写一个 `<key>ValIdx` 列存当前下标，再为每个备选类型写一列（[L200-L207](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/SerdeObjectWriter.h#L200-L207)）；
- `vector/set` → 序列化成 JSON 字符串占一列；`optional` → 空值写空串。

这套映射是「serde 结构体 ↔ Parquet 列」的契约，由 `analytics/Common.h` 里的两个列名后缀常量钉死：`kVariantValueIndexColumnSuffix="ValIdx"`、`kResultErrorTypeColumnSuffix="Error"`（[Common.h:L62-L64](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/Common.h#L62-L64)）。

**③ 读端：`SerdeObjectReader`。** [SerdeObjectReader.h:L24-L43](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/SerdeObjectReader.h#L24-L43) 的 `open` 用 `parquet::ParquetFileReader` 打开文件得到 `StreamReader`。`operator>>`（[L45-L58](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/SerdeObjectReader.h#L45-L58)）逐行 `visit` 回填结构体。它是 writer 的**镜像**：同样的类型分派（[L70-L230](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/SerdeObjectReader.h#L70-L230)），方向相反——例如读 `variant` 时先读 `<key>ValIdx` 下标，再用 `visitVariant` 遍历备选类型、按下标 `std::move` 赋值（[L182-L200](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/SerdeObjectReader.h#L182-L200)）；读 `Result` 时先读 Error 列判断成败（[L164-L180](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/SerdeObjectReader.h#L164-L180)）。两侧靠相同的列名约定配对。

**④ 真实 trace 内容范例：`MetaEventTrace`。** [src/meta/event/Event.h:L50-L72](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/event/Event.h#L50-L72) 是 meta 服务实际落盘的结构体——每个字段（`eventType`、`inodeId`、`parentId`、`entryName`、`length`、`oflags` …）都因 `SERDE_STRUCT_FIELD` 自带反射，从而自动成为 Parquet 的一列。meta 服务在配置里挂上它（[src/meta/base/Config.h:L66](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/base/Config.h#L66) 的 `event_trace_log`），`MetaStore` / `MetaOperator` 持有其引用（[MetaStore.h:L75](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/MetaStore.h#L75)）。

#### 4.3.4 代码实践（源码阅读型 + 可选运行）

1. **实践目标**：理解「serde 结构体如何变成 Parquet 列」，并能定位 trace 文件的落盘位置。
2. **操作步骤**：
   - 读 [Event.h:L50-L72](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/event/Event.h#L50-L72)，数一下 `MetaEventTrace` 有多少个字段——这 就是该 trace Parquet 文件的列数（外加 `trace_meta` 的 `timestamp`/`hostname`）。
   - 在 [SerdeObjectWriter.h:L107-L119](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/SerdeObjectWriter.h#L107-L119) 找到算术/枚举字段的写入分派，确认 `eventType`（枚举）会被写成 `int32` 列。
   - 在 [StructuredTraceLog.h:L221-L240](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/StructuredTraceLog.h#L221-L240) 找到文件命名规则，推出 trace 文件会出现在 `<trace_file_dir>/<YYYY-MM-DD>/<host>/MetaEventTrace.<host>.<时间戳>.<序号>.parquet`。
   - （可选，待本地验证）若有一个已落盘的 trace 文件，可用 Python 的 `pyarrow.parquet` 直接打开查看 schema 与行，因为格式是标准 Parquet：
     ```python
     import pyarrow.parquet as pq
     t = pq.read_table("MetaEventTrace.<host>.<ts>.1.parquet")
     print(t.schema); print(t.to_pandas().head())
     ```
3. **需要观察的现象**：列名与 `MetaEventTrace` 字段名一一对应；`Result`/`variant` 类型的字段会多出 `Error` / `ValIdx` 后缀列。
4. **预期结果**：你能解释「改一个 serde 字段，Parquet 列会怎么变」，以及为什么读端能凭列名约定无损还原。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Result<T>` 在 Parquet 里要拆成「值列 + Error 后缀列」两列，而不是合成一列？

**参考答案**：Parquet 是强类型列存，一列只能有一种类型。`Result<T>` 要么是 `T` 要么是错误码，二者类型不同。拆成两列后：成功时 Error 列为 `kOK`、值列填真实值；失败时值列填默认值、Error 列填错误码。读端（[SerdeObjectReader.h:L164-L180](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/analytics/SerdeObjectReader.h#L164-L180)）先读 Error 列判断成败再决定取值，无损还原。

**练习 2**：`StructuredTraceLog` 的 `flush` 用 `dumpingTrace_.test_and_set()` 做了什么保护？

**参考答案**：`flush` 是异步的（`std::launch::async`），可能被「周期触发」和「关闭」并发调用。`test_and_set()` 是个原子标志：第一个进入者拿到 true 继续，后来者直接 return，保证同一时刻只有一个 flush 在换文件，避免多个线程同时关闭/创建 writer 造成文件错乱。

**练习 3**：指标（Module 4.1/4.2）和结构化 trace（Module 4.3）的本质区别是什么？

**参考答案**：指标是**聚合**数据（每秒一条，丢掉了单条事件细节），适合用 SQL 做趋势/告警，存在 ClickHouse；结构化 trace 是**逐条**明细（每个元数据事件一行），适合事后精确复盘某次操作，存在本地 Parquet 文件。两者互补：前者看「整体健康度」，后者查「某条记录到底发生了什么」。

---

## 5. 综合实践

把三个模块串起来，完成一次「端到端可观测性」演练：

**场景**：你想确认「storage 节点的批量读延迟」是否正常，并能在出问题时定位到具体某次读。

**任务**：

1. **指标面**：部署 monitor_collector（按 4.2.4 步骤），在 ClickHouse 里查询 storage 的批量读延迟分布指标（在 `distributions` 表里按 `metricName` 前缀 `storage.*` 过滤），观察 p50/p99 随时间的变化曲线。
2. **交叉验证**：用 `admin_cli list-nodes`（见 u1-l3）确认相关 storage 节点的 `host`，再回到 ClickHouse 用 `WHERE host = '<该节点>'` 缩小范围，验证「指标里的 host」与「集群里的节点」对得上。
3. **trace 面**：对照 4.3，说明若要复盘「meta 侧某次 rename 的完整字段」，应去哪个目录找 `MetaEventTrace.*.parquet` 文件，并说明该文件有多少列（=`MetaEventTrace` 字段数 + trace_meta 的 2 列）。
4. **链路总结**：画出从「storage 业务线程 `LatencyRecorder.addSample`」到「ClickHouse `distributions` 表一行」的完整链路，标出每一跳的线程/进程/网络边界，以及批量合并发生的两个位置（业务进程内 `collect_period` 攒批、collector 内 `batch_commit_size` 攒批）。

> 若无测试集群，任务 1/2 标注「待本地验证」；任务 3/4 为纯源码阅读，可直接完成。

## 6. 本讲小结

- **指标采集是进程内、去中心的**：每个服务进程都跑一个 `MonitorInstance`，业务用 `CountRecorder`/`LatencyRecorder` 等 TLS 打点，`Collector` 线程每秒汇总成 `Sample`，`Reporter` 线程负责外发——业务热路径只做一次轻量累加。
- **`monitor_collector` 是中央聚合器**：生产部署里，业务服务用 `monitor_collector` reporter 把样本经 TCP 10000 发给 collector；collector 用 `MPMCQueue`（204800）削峰、32 个连接线程按 `batch_commit_size`（4096）批量合并、过滤黑名单后写 ClickHouse。
- **两种值两类表**：整数样本进 `counters`，分布样本进 `distributions`；表按 `(metricName, host, pod, instance, TIMESTAMP)` 排序、按天分区、1 个月 TTL，标签列用 `LowCardinality`+ZSTD 压缩。
- **配置位置**：业务服务在 `[common.monitor.reporters.monitor_collector] remote_ip` 指向 collector；collector 在 `[server.monitor_collector.reporter.clickhouse]` 指向 ClickHouse；`admin_cli` 通过各服务配置间接关联。
- **结构化 trace 复用 serde 反射**：`StructuredTraceLog<T>` 把任意 serde 结构体周期性落成按日期/主机分层的 ZSTD Parquet 文件；`SerdeObjectWriter`/`SerdeObjectReader` 靠同一套 `visit` 分派与列名约定（`ValIdx`/`Error` 后缀）实现读写对称。
- **指标 vs trace**：指标是聚合趋势（ClickHouse），trace 是逐条明细（本地 Parquet），二者互补。

## 7. 下一步学习建议

- 想看「业务代码到底打了哪些点」：到 `src/storage`、`src/meta`、`src/client` 里搜索 `CountRecorder`、`LatencyRecorder`、`OperationRecorder` 的实例化，对照本讲的采集链路理解每个指标的含义。
- 想深入监控采集的并发细节：精读 `Recorder.cc`（TLS 汇总、`getRecorderWithTag` 的动态标签子 recorder）、`Monitor.cc`（多 collector 分桶、`ObjectPool` 复用 SampleBatch）。
- 想扩展可观测性：参照 `analytics` 的 visitor 模式，把自定义的 serde 结构体接入 `StructuredTraceLog`，实现自己的结构化日志；或为 monitor_collector 增加新的 `Reporter` 后端（实现 `Reporter::init/commit`）。
- 本单元（u8）的下一篇 `u8-l4` 将讲解如何用 `simple_example` 模板新增一个 3FS 服务，以及 `admin_cli` / `tools` 的命令实现——可观测性（本讲）正是新服务必须顺手接好的能力。

# 监控指标与性能调优

Mooncake 提供了完善的可观测性体系，包括 Prometheus 指标暴露、周期性日志输出和关键性能指标监控。本讲介绍如何通过这些指标诊断性能瓶颈并进行参数调优。

## 1. 指标暴露

### 1.1 概念说明

指标暴露是指 Mooncake 将内部运行时状态和性能数据通过标准协议（Prometheus 文本格式）对外暴露，供监控系统采集和分析。这在以下场景中至关重要：

- **性能监控**：实时跟踪资源利用率、请求延迟和吞吐量
- **容量规划**：基于历史数据预测资源需求
- **故障诊断**：快速定位异常和瓶颈
- **SLA 验证**：验证服务等级协议承诺

### 1.2 伪代码或流程

Mooncake 的指标暴露遵循以下流程：

```
初始化阶段：
1. 创建 MasterMetricManager 单例
2. 注册所有指标（Counter/Histogram/Gauge）
3. 启动后台定时报告线程

运行阶段：
每 10 秒（可配置）：
  1. 采集所有当前指标值
  2. 计算速率（基于上次快照）
  3. 格式化为 Prometheus 文本格式
  4. 输出到日志和 HTTP 端点

HTTP 端点（/metrics）：
  1. 接收 GET 请求
  2. 序列化所有指标
  3. 返回文本格式响应
```

### 1.3 原理分析

Mooncake 使用 `ylt/metric` 库（基于 Prometheus C++ 客户端）管理四类指标：

**Counter（计数器）**：单调递增的累计值，用于请求计数、错误计数等。
- 特性：只增不减，重启后归零
- 用途：计算速率（requests per second）

**Gauge（仪表）**：可增可减的当前值，用于内存使用、连接数等。
- 特性：反映瞬时状态
- 用途：监控资源利用率

**Histogram（直方图）**：记录值分布到预定义桶中，用于延迟、对象大小等。
- 特性：提供分位数（p50/p95/p99）和总和
- 用途：分析尾部延迟

**Summary（摘要）**：直接计算滑动窗口分位数，用于精确的百分位统计。

指标计算的核心公式：

\[ \text{Rate} = \frac{\text{当前值} - \text{上次值}}{\Delta t} \]

对于直方图的 p95 分位数计算：

\[ \text{p95} = \min\{b \mid \text{累计计数} \geq 0.95 \times \text{总计数}\} \]

其中 \( b \) 是桶边界。

### 1.4 代码实践

**指标定义与注册**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_metric_manager.h#L505-L545](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_metric_manager.h#L505-L545)

这段代码定义了 Master 的核心 Gauge 指标，包括内存使用量、总容量、文件存储容量等。每个指标都有名称和帮助文本，符合 Prometheus 命名规范。

**延迟桶配置**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_metric.h#L22-L31](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_metric.h#L22-L31)

这里配置了 RDMA 传输延迟的直方图桶，针对亚毫秒级延迟进行细粒度划分（125-1000μs），同时对长尾延迟（>1ms）使用对数间隔覆盖到 1 秒。

**指标序列化**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/master_metric_manager.cpp#L1650-L1670](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/src/master_metric_manager.cpp#L1650-L1670)

`serialize_metrics()` 方法将所有内部指标序列化为 Prometheus 文本格式，通过 HTTP `/metrics` 端点暴露给监控系统抓取。

**HTTP 管理端点**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/observability.md#L115-L127](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/observability.md#L115-L127)

文档列出了所有可用的 HTTP 端点，包括 `/metrics`（Prometheus 格式）、`/metrics/summary`（人类可读摘要）、`/health`（健康检查）等。

### 1.5 练习题

1. **理解指标类型**：为什么请求计数使用 Counter 而内存使用使用 Gauge？如果用 Gauge 记录请求计数会有什么问题？

2. **延迟分布设计**：假设你的服务 99% 请求在 10ms 内完成，但有 1% 请求需要 1-5 秒，你会如何设计 Histogram 桶？

3. **指标计算**：当前内存使用 80 GB，10 秒前为 78 GB，当前请求数 10000，10 秒前为 9500，计算内存增长速率和请求速率。

4. **序列化格式**：Prometheus 文本格式中，`# TYPE mooncake_allocated_bytes gauge` 这行是什么作用？

### 1.6 答案

1. **答**：请求计数应该单调递增，Counter 语义正确且支持速率计算。用 Gauge 记录请求计数在服务重启后会归零，导致累计值丢失，且无法区分"减少"和"重启"。

2. **答**：应该针对 99% 正常请求（0-10ms）密集采样，如 1ms, 2ms, 5ms, 10ms，然后对长尾使用指数间隔：50ms, 100ms, 500ms, 1s, 2s, 5s。这样可以精确观察 p95/p99，同时捕捉极端情况。

3. **答**：
   - 内存增长速率：`(80 - 78) GB / 10s = 0.2 GB/s = 200 MB/s`
   - 请求速率：`(10000 - 9500) / 10s = 50 req/s`

4. **答**：这是 Prometheus 文本格式的元数据行，声明指标名称和类型，帮助监控系统理解数据语义。实际数据行如 `mooncake_allocated_bytes 85899345920` 紧随其后。

---

## 2. 性能监控

### 2.1 概念说明

性能监控是通过关键指标实时跟踪系统健康状况和性能表现的过程。Mooncake 监控的核心维度包括：

- **资源利用率**：内存、SSD、网络带宽使用情况
- **缓存效率**：命中率、驱逐频率
- **延迟分布**：p50/p95/p99 延迟
- **吞吐量**：每秒请求数、数据传输速率

### 2.2 伪代码或流程

```
监控数据采集循环（每 10 秒）：

1. 采集资源使用：
   memory_usage = 当前内存分配量
   ssd_usage = 当前 SSD 使用量
   memory_ratio = memory_usage / total_memory_capacity
   ssd_ratio = ssd_usage / total_ssd_capacity

2. 计算缓存效率：
   memory_hits = 内存缓存命中次数（自上次采集）
   ssd_hits = SSD 缓存命中次数
   total_gets = 总 GET 请求次数
   memory_hit_rate = memory_hits / total_gets
   ssd_hit_rate = ssd_hits / total_gets
   overall_hit_rate = (memory_hits + ssd_hits) / total_gets

3. 统计延迟分布：
   p50_lat = 计算第 50 百分位延迟
   p95_lat = 计算第 95 百分位延迟
   p99_lat = 计算第 99 百分位延迟

4. 输出监控摘要：
   格式化为人类可读的文本
   写入日志并暴露到 HTTP 端点
```

### 2.3 原理分析

**缓存命中率计算**：

单层命中率（如内存）：

\[ \text{HitRate}_{\text{mem}} = \frac{N_{\text{mem\_hits}}}{N_{\text{total\_gets}}} \]

整体命中率：

\[ \text{HitRate}_{\text{overall}} = \frac{N_{\text{mem\_hits}} + N_{\text{ssd\_hits}}}{N_{\text{total\_gets}}} \]

其中 \( N \) 表示计数。

**驱逐效率**：

驱逐成功率：

\[ \text{EvictionSuccessRate} = \frac{N_{\text{eviction\_success}}}{N_{\text{eviction\_attempts}}} \]

平均每次驱逐释放空间：

\[ \text{AvgEvictedSize} = \frac{\text{TotalEvictedBytes}}{N_{\text{evicted\_keys}}} \]

**带宽利用率**：

实际吞吐量：

\[ \text{Throughput} = \frac{\Delta \text{Bytes}}{\Delta t} \]

理论带宽上限由 RDMA 网卡或 SSD 顺序写性能决定，利用率：

\[ \text{Utilization} = \frac{\text{Throughput}}{\text{MaxBandwidth}} \times 100\% \]

**延迟分位数含义**：

- **p50**：一半请求的延迟低于此值，代表"典型"体验
- **p95**：95% 请求的延迟低于此值，代表"良好"体验
- **p99**：99% 请求的延迟低于此值，代表"可接受"尾部延迟

### 2.4 代码实践

**Master 指标摘要生成**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_metric_manager.h#L53-L74](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_metric_manager.h#L53-L74)

这里定义了缓存统计相关的枚举和计算方法，`calculate_cache_stats()` 返回内存/SSD 命中率、当前缓存对象数等指标。

**驱逐指标计数器**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_metric_manager.h#L261-L288](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_metric_manager.h#L261-L288)

这些方法在驱逐发生时被调用，记录成功/失败次数、驱逐的 key 数量和数据大小。这些指标用于监控驱逐频率和效率。

**客户端传输指标**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_metric.h#L104-L137](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_metric.h#L104-L137)

`TransferMetric` 结构记录了 RDMA 传输的总字节数、操作延迟分布，`summary_metrics()` 方法计算平均吞吐量和延迟分位数。

**SSD 性能指标**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_metric.h#L500-L558](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_metric.h#L500-L558)

`SsdMetric` 监控 SSD 读写字节数、操作次数和延迟，并提供 p50/p90/p99 分位数统计。这有助于判断 SSD 是否成为瓶颈。

**周期性日志输出示例**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/observability.md#L26-L30](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/observability.md#L26-L30)

日志示例显示了 Master 每 10 秒输出的摘要，包括内存/SSD 使用率、key 数量、请求速率、驱逐统计等信息。

### 2.5 练习题

1. **缓存效率分析**：内存命中率 80%，SSD 命中率 15%，整体命中率是多少？如果内存容量翻倍，预计整体命中率会提升多少？

2. **驱逐判断**：观察到 `AllocFail` 指标从 0 上升到 100/s，同时驱逐成功率从 95% 下降到 60%，说明什么问题？应该如何应对？

3. **延迟异常**：p50 延迟正常（1ms），但 p99 从 10ms 上升到 500ms，可能的原因是什么？需要检查哪些指标？

4. **带宽计算**：10 秒内传输了 5 GB 数据，网卡理论带宽 25 Gbps，实际利用率是多少？（1 Byte = 8 bits）

### 2.6 答案

1. **答**：
   - 整体命中率：80% + 15% = 95%
   - 内存容量翻倍后，预计整体命中率会提升，但幅度取决于访问模式。如果工作集大于原内存，提升可能显著；如果工作集已完全装入内存，提升有限。

2. **答**：`AllocFail` 上升说明内存不足，驱逐成功率下降说明碎片严重或大部分对象被 pin 保护无法驱逐。应对措施：
   - 检查 `soft_pin_key_count` 是否过高
   - 增加 segment 数量或容量
   - 调整驱逐策略（考虑碎片整理）

3. **答**：p50 正常但 p99 异常，说明多数请求正常但少数请求出现长尾延迟。可能原因：
   - GC 停顿或后台任务阻塞
   - 网络拥塞或重传
   - SSD 写放大或读延迟尖峰
   需要检查 `eviction_attempts`、`ssd_write_latency`、网络错误率等指标。

4. **答**：
   - 实际吞吐量：`5 GB / 10 s = 0.5 GB/s = 4 Gbps`
   - 利用率：`4 Gbps / 25 Gbps = 16%`
   - 带宽利用率较低，瓶颈可能不在网络而在其他环节（如 CPU、SSD、锁竞争）。

---

## 3. 瓶颈诊断

### 3.1 概念说明

瓶颈诊断是通过指标分析定位性能限制点的过程。Mooncake 中常见的性能瓶颈包括：

- **内存不足**：频繁驱逐、分配失败
- **SSD 瓶颈**：写延迟高、IOPS 饱和
- **网络瓶颈**：RDMA 带宽不足、重传率高
- **锁竞争**：某些操作延迟异常高
- **配置不当**：参数不匹配工作负载

### 3.2 伪代码或流程

```
瓶颈诊断决策树：

1. 检查内存使用：
   if memory_usage_ratio > 90%:
       if eviction_attempts > threshold:
           瓶颈 = "内存容量不足"
           建议 = "扩容内存或调整驱逐策略"
       elif alloc_fail_rate > threshold:
           瓶颈 = "内存碎片严重"
           建议 = "检查对象大小分布，考虑合并分配"

2. 检查 SSD 性能：
   if ssd_write_latency_p99 > threshold:
       if ssd_utilization > 90%:
           瓶颈 = "SSD IOPS 饱和"
           建议 = "升级 SSD 或增加并发度"
       else:
           瓶颈 = "SSD 延迟尖峰（可能写放大）"
           建议 = "检查 SSD 健康状态和预留空间"

3. 检查网络性能：
   if transfer_latency_p99 > threshold:
       if retransmission_rate > threshold:
           瓶颈 = "网络不稳定"
           建议 = "检查 RDMA 链路质量"
       elif bandwidth_utilization > 90%:
           瓶颈 = "网络带宽饱和"
           建议 = "升级网卡或增加链路"

4. 检查 RPC 延迟：
   for each rpc_type:
       if rpc_latency_p99 > threshold:
           瓶颈 = f"{rpc_type} 操作慢"
           检查该 RPC 对应的代码路径
```

### 3.3 原理分析

**内存瓶颈识别**：

内存压力指标组合：

\[ \text{Pressure}_{\text{mem}} = \alpha \cdot \frac{\text{MemUsage}}{\text{MemCapacity}} + \beta \cdot \text{EvictionRate} + \gamma \cdot \text{AllocFailRate} \]

当 `Pressure_mem` 超过阈值时，判断为内存瓶颈。其中 \(\alpha, \beta, \gamma\) 是权重系数。

**SSD 瓶颈识别**：

SSD 延异常分布分析：

\[ \text{如果 } \text{p99}_{\text{write}} \gg \text{p50}_{\text{write}} \text{ 且 } \text{IOPS}_{\text{actual}} \approx \text{IOPS}_{\text{max}} \]

则 SSD IOPS 饱和。如果 `IOPS` 远低于上限但 p99 高，可能是：
- 写放大（随机写导致）
- GC 干扰
- 队列深度不足

**网络瓶颈识别**：

RDMA 带宽利用率：

\[ \text{BW}_{\text{util}} = \frac{\text{Throughput}_{\text{actual}}}{\text{BW}_{\text{theoretical}}} \]

如果 `BW_util > 90%` 且延迟正常，说明带宽饱和。如果延迟高但带宽低，可能是：
- 网络拥塞（丢包重传）
- 小包过多（协议开销）
- 节点间负载不均衡

**锁竞争识别**：

如果某个 RPC 的 p99 延迟异常高，但 p50 正常，且该 RPC 涉及共享数据结构（如元数据查找、分配器），可能是锁竞争。通过火焰图或perf 验证。

### 3.4 代码实践

**缓存统计计算**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_metric_manager.h#L53-L74](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_metric_manager.h#L53-L74)

`calculate_cache_stats()` 方法返回一个包含内存/SSD 命中率和当前缓存对象数的字典，用于诊断缓存效率。

**分配失败监控**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_metric_manager.h#L133-L140](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_metric_manager.h#L133-L140)

`inc_put_start_alloc_failures()` 在 PutStart 因无法分配副本时被调用，高频调用说明内存不足或碎片严重。

**RPC 延迟监控**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_metric.h#L238-L347](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_metric.h#L238-L347)

`MasterClientMetric` 记录了所有 RPC 的调用次数和延迟分布，`summary_metrics()` 输出每个 RPC 的 count/p95/pmax 延迟，帮助定位慢操作。

**驱逐指标**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_metric_manager.h#L261-L288](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/master_metric_manager.h#L261-L288)

区分总驱逐、内存驱逐和 NoF（NVMe-on-Flash）驱逐，记录成功/失败/驱逐 key 数/数据大小。驱逐失败率高说明碎片严重或对象被 pin 保护。

**传输操作监控**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_metric.h#L349-L423](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_metric.h#L349-L423)

`TransferOperationMetric` 按接口类型（如 `rdma_read`、`tcp_write`）记录操作次数、字节数和延迟，帮助诊断特定传输路径的问题。

### 3.5 练习题

1. **综合诊断**：观察到以下指标：
   - 内存使用率 95%
   - 分配失败率 10 req/s
   - 驱逐成功率 40%
   - SSD 延迟正常（p99 < 1ms）
   - 网络 p95 延迟 5ms（正常 2ms）
   
   判断主要瓶颈和次要瓶颈，给出优化建议。

2. **SSD 瓶颈细分**：SSD 写 p99 = 500ms，p50 = 100μs，IOPS = 10000（理论上限 50000），带宽利用率 30%。分析可能原因和验证方法。

3. **网络问题诊断**：RDMA 传输 p50 延迟正常（1ms），但 p99 从 10ms 上升到 2s，同时吞吐量下降 50%。如何判断是网络拥塞还是其他问题？

4. **驱逐效率分析**：内存驱逐成功率 30%，平均每次驱逐只释放 2 个 key（总共 10000 个 key），说明什么？如何改进？

### 3.6 答案

1. **答**：
   - **主要瓶颈**：内存不足（使用率 95% + 分配失败率高 + 驱逐成功率低）
   - **次要瓶颈**：网络延迟上升（可能是内存压力导致的连锁反应）
   - **优化建议**：
     - 立即：扩容内存或增加 segment 数量
     - 中期：检查 `soft_pin_key_count`，减少不必要的 pin
     - 长期：启用 SSD offload 作为二级缓存，优化驱逐算法减少碎片

2. **答**：p99 远高于 p50 且 IOPS 远低于上限，说明有长尾延迟但未饱和。可能原因：
   - **写放大**：随机写导致 SSD 内部 GC，检查 `fst` 工具的 `write_amp` 指标
   - **队列拥堵**：IO 队列深度不足，尝试增加 `queue_depth`
   - **干扰**：其他进程占用 SSD，用 `iostat -x 1` 检查 `%util` 和 `await`
   - **验证方法**：运行 FIO 顺序写/随机写基准，对比实际延迟

3. **答**：判断方法：
   - **检查重传**：如果有 RDMA 重传计数器，检查是否上升
   - **检查吞吐量**：网络拥塞通常吞吐量也会下降；如果吞吐量正常但延迟高，可能是 GC 或锁竞争
   - **检查 CPU**：用 `perf top` 查看是否在内核自旋锁上消耗大量 CPU
   - **检查丢包**：用 `ethtool -S <dev>` 查看网卡丢包计数
   - 本题中吞吐量下降，更像网络拥塞或链路质量问题

4. **答**：驱逐成功率低 + 每次驱逐 key 数少，说明：
   - **碎片严重**：虽然总使用率高，但没有足够连续空间分配大对象
   - **Pin 保护**：大量 key 被 soft pin 保护无法驱逐
   - **改进措施**：
     - 检查 `soft_pin_key_count`，如果过高需优化 pin 使用策略
     - 考虑使用更紧凑的分配器（如针对特定对象大小的 arena）
     - 实现碎片整理机制（定期迁移对象）
     - 增加 segment 数量，降低单 segment 压力

---

## 4. 参数调优

### 4.1 概念说明

参数调优是通过配置项调整系统行为以匹配工作负载特征的过程。Mooncake 的调优维度包括：

- **RDMA 配置**：缓冲区大小、并发度、超时时间
- **内存分配**：Segment 容量、副本策略、驱逐策略
- **缓存策略**：SSD offload 阈值、promotion 参数
- **并发控制**：线程池大小、队列深度、批处理大小

### 4.2 伪代码或流程

```
参数调优流程：

1. 基线测试：
   使用默认配置运行基准测试，记录：
   - 吞吐量（ops/s）
   - 延迟分布（p50/p95/p99）
   - 资源利用率（CPU/内存/网络/SSD）

2. 瓶颈识别：
   根据瓶颈诊断结果，确定调优目标

3. 参数调整（单因素实验）：
   for 参数 in [rdma_buf_size, segment_count, eviction_watermark]:
       for 值 in [候选值列表]:
           修改配置
           运行测试
           记录结果
           恢复配置

4. 结果分析：
   绘制参数-性能曲线
   选择最优值或拐点

5. 交互验证：
   组合多个"好"参数
   验证无负交互（如性能提升但延迟变差）
```

### 4.3 原理分析

**RDMA 缓冲区调优**：

RDMA 接收缓冲区大小（`recv_buf_size`）影响：
- **过小**：频繁注册内存、增加 CPU 开销、可能丢包
- **过大**：内存占用高、NUMA 跨节点访问延迟

最优值估算：

\[ \text{OptimalSize} = \max(\text{MTU}, \text{BatchSize}) \times \text{Concurrency} \times \text{SafetyFactor} \]

其中 `SafetyFactor` 通常为 1.5-2。

**Segment 容量调优**：

Segment 太少的问题：
- 单 Segment 锁竞争严重
- 驱逐时"全部或无"波动大

Segment 太多的问题：
- 元数据开销增加
- 跨 Segment 路由延迟

经验公式（针对均匀对象大小）：

\[ N_{\text{segments}} = \min\left(\frac{N_{\text{cores}}}{2}, \frac{\text{TotalCapacity}}{128 \text{GB}}\right) \]

**驱逐水印调优**：

驱逐高水印（`high_watermark`）触发驱逐，低水印（`low_watermark`）停止驱逐。

水印差过小：
- 驱逐频繁、抖动大
- CPU 开销高

水印差过大：
- 驱逐后仍可能立即再次分配失败
- 内存利用率波动大

推荐配置：

\[ \text{HighWatermark} = 0.90 \sim 0.95 \]
\[ \text{LowWatermark} = \text{HighWatermark} - 0.10 \sim 0.15 \]

**SSD Offload 阈值调优**：

当对象大小超过 `offload_threshold` 时直接写入 SSD，不占用内存。

阈值选择：
- **过小**：小对象也落 SSD，增加 SSD 压力且未节省内存
- **过大**：大对象占内存，导致驱逐频繁

基于工作集分析：

\[ \text{Threshold} = \text{Percentile}_{80}(\text{ObjectSizeDistribution}) \]

### 4.4 代码实践

**延迟桶配置**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_metric.h#L22-L31](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_metric.h#L22-L31)

这些延迟桶定义影响监控精度。如果你的工作负载延迟集中在某个范围，应该调整桶边界以获得更精确的 p95/p99。

**SSD 延迟桶配置**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_metric.h#L500-L509](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/mooncake-store/include/client_metric.h#L500-L509)

SSD 延迟桶从 50μs 到 30s，覆盖从高端 NVMe 到网络存储的延迟范围。如果你的 SSD 更快或更慢，应该调整桶边界。

**分配策略选择**：

[https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/performance/allocation-strategy-benchmark-result.md#L32-L33](https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/performance/allocation-strategy-benchmark-result.md#L32-L33)

文档显示 `RandomAllocationStrategy` 吞吐更高但 segment 利用率不均衡，`FreeRatioFirstAllocationStrategy` 利用率更均衡但吞吐略低。根据你的优先级（吞吐 vs 利用率）选择。

**配置示例**：

Master 配置文件（`master.json`）关键参数：

```json
{
  "enable_metric_reporting": true,
  "metrics_port": 9003,
  "segment_count": 4,
  "segment_capacity": 1073741824000,
  "high_watermark": 0.90,
  "low_watermark": 0.80,
  "eviction_strategy": "lru"
}
```

RDMA 配置（环境变量）：

```bash
export MC_RDMA_RECV_BUF_SIZE=65536
export MC_RDMA_SEND_BUF_SIZE=65536
export MC_RDMA_MAX_SEND_WR=128
export MC_RDMA_MAX_RECV_WR=256
```

### 4.5 练习题

1. **RDMA 缓冲区调优**：当前 MTU=1500，并发度=8，平均请求大小=4KB，建议的 recv_buf_size 是多少？

2. **Segment 数量选择**：16 核 CPU，总容量 512 GB，对象大小 3MB（DSA workload），建议 segment 数量是多少？

3. **驱逐水印配置**：观察到内存使用率在 85%-95% 之间频繁波动，驱逐一直触发。如何调整水印？

4. **SSD Offload 阈值**：对象大小分布：20% < 1MB，50% 1-5MB，30% > 5MB，内存容量 100 GB，工作集 200 GB，建议 offload_threshold？

### 4.6 答案

1. **答**：
   - BatchSize = 4KB（单请求）
   - Concurrency = 8
   - OptimalSize = 4KB × 8 × 1.5 = 48KB
   - 向上对齐到页边界：**建议 64KB**（65536 字节）
   - 实际还需要考虑 NIC 限制和 NUMA，可能需要测试 32KB/64KB/128KB

2. **答**：
   - 基于核数：16 / 2 = 8 segments
   - 基于容量：512 GB / 128 GB = 4 segments
   - 取最小值：**建议 4 segments**
   - 但 DSA workload 有两个对象大小（KV 3MB + indexer 643KB），可能需要更多 segment 减少混合分配碎片，可以考虑 6-8 segments

3. **答**：内存使用率在 85%-95% 波动且驱逐频繁，说明水印差过小。
   - **当前配置猜测**：high=0.90, low=0.85
   - **问题**：使用率刚到 90% 就开始驱逐，降到 85% 停止，立即又涨到 90%，反复抖动
   - **建议调整**：
     - high_watermark = 0.95（允许更高利用率）
     - low_watermark = 0.80（增大差值到 15%）
   - 这样驱逐后降到 80%，有 15% 缓冲区，减少抖动

4. **答**：
   - **目标**：让 30% 大对象（>5MB）直接落 SSD，节省内存
   - **Threshold 选择**：5-10MB 之间
   - **考虑因素**：
     - Threshold=5MB：30% 对象落 SSD，节省 30% × 50% = 15% 内存
     - Threshold=10MB：可能 10-20% 对象落 SSD（取决于分布），节省略少
   - **建议**：**5-8 MB**
   - 验证方法：设置后观察内存使用率下降情况和 SSD 延迟是否增加

---

## 总结

本讲介绍了 Mooncake 的监控指标体系、性能监控方法、瓶颈诊断技巧和参数调优策略。关键要点：

1. **指标暴露**：使用 Prometheus 文本格式暴露 Counter/Gauge/Histogram 指标，支持日志输出和 HTTP 抓取
2. **性能监控**：关注资源利用率、缓存命中率、延迟分布和吞吐量四大维度
3. **瓶颈诊断**：通过指标组合（内存压力 + 驱逐失败、SSD 延迟 + IOPS、网络延迟 + 带宽）定位瓶颈
4. **参数调优**：基于工作负载特征调整 RDMA 缓冲区、Segment 数量、驱逐水印和 Offload 阈值

实际生产环境中，建议建立监控仪表盘（Grafana），设置告警规则（内存使用率 > 90%、驱逐失败率 > 10%、p99 延迟超过阈值），并定期进行性能基准测试和参数调优。

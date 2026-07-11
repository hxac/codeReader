# 指标监控与健康检查

## 1. 本讲目标

本讲是第七单元「分布式部署与扩展特性」的收尾篇，主题是**可观测性**——当一个 LightLLM 服务跑起来后，我们怎么知道它「现在忙不忙、健康不健康」。

学完后你应该掌握：

- 理解 **MetricServer / MetricClient** 这对基于 rpyc 的「中央账本 + 异步上报」模型，以及 `Monitor` 如何用 `prometheus_client` 登记指标。
- 认识 `lightllm_batch_current_size`、`lightllm_queue_size`、`lightllm_num_running_reqs`、`lightllm_batch_current_max_tokens` 等**关键 gauge** 的含义与上报时机。
- 理解 `/metrics` 端点如何把中央账本转成 Prometheus 文本格式暴露出去，以及可选的 `push_to_gateway` 推送。
- 区分两套**健康检查**机制：被动的 `/health` 端点（基于共享内存时间戳，秒级被动响应）与主动的 `health_monitor` 进程（周期性探活、连续失败即杀进程自愈）。

本讲承接 u2-l1（多进程架构总览）与 u2-l5（Router 调度循环），重点说明「指标数据从哪来、到哪去、怎么查」以及「健康怎么判定、谁来兜底」。

## 2. 前置知识

### 2.1 Prometheus 的三类指标原语

LightLLM 的指标体系直接建立在 [`prometheus_client`](https://github.com/prometheus/client_python) 之上。它有三类原语，理解它们的「单调性」差别是看懂本讲代码的前提：

| 类型 | 单调性 | 典型用途 | 本讲对应 |
| --- | --- | --- | --- |
| **Counter**（计数器） | 只增不减 | 累计请求数、累计 token 数 | `lightllm_request_count`、`lightllm_prompt_tokens_total` |
| **Gauge**（仪表盘） | 可增可减 | 当前瞬时值 | `lightllm_batch_current_size`、`lightllm_queue_size` |
| **Histogram**（直方图） | 累积分布 | 延迟、长度分布 | `lightllm_request_duration`、`lightllm_request_input_length` |

一句话记忆：**「累计了多少」用 Counter，「现在多少」用 Gauge，「分布在哪些桶里」用 Histogram**。

### 2.2 rpyc 与「中央账本」思路

复习 u2-l4：LightLLM 用 rpyc 做「需应答的远程调用」。指标采集沿用了这套思路，把所有进程的指标**汇聚到一个独立的 metric 进程**里：

- metric 进程持有一份唯一的 `CollectorRegistry`（中央账本），并以 rpyc 服务形式对外。
- Router、HttpServer 等数据生产者各自持有一个 `MetricClient`，通过 rpyc 把数值「写」进中央账本。
- 对外暴露 `/metrics` 时，再通过 rpyc 从中央账本「读」出汇总后的文本。

这样做的好处是：**任一进程崩溃，账本都不丢；Prometheus 只需 scrape 一个地址**。

### 2.3 共享内存 SharedInt（承接 u4-l3）

健康检查的「被动探活」依赖 u4-l3 讲过的 `SharedInt`——它把一个整数直接铺在共享内存上，跨进程零拷贝读写。本讲中 HttpServer 把「最近一次成功推理的时间戳」和「在跑请求数」写进两个 `SharedInt`，`/health` 端点读它们来做判断，无需任何进程间通信开销。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`lightllm/server/metrics/metrics.py`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/metrics.py) | `Monitor` 类：登记所有 Counter/Gauge/Histogram，提供 `counter_inc`/`gauge_set`/`histogram_observe`/`push_metrices`。还定义了 `MONITOR_INFO` 名称→说明的映射。 |
| [`lightllm/server/metrics/manager.py`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py) | `MetricServer`（rpyc 服务，持有 `Monitor`）、`MetricClient`（生产者侧异步上报线程）、`start_metric_manager`（进程入口）。 |
| [`lightllm/server/health_monitor/manager.py`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/health_monitor/manager.py) | 主动健康监控进程：周期 GET `/health`，连续失败即杀整棵进程树自愈。 |
| [`lightllm/server/api_http.py`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_http.py) | HTTP 端点：`/metrics`（拉取账本）、`/health`（被动探活），并构造 `MetricClient`。 |
| [`lightllm/server/router/manager.py`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py) | Router 调度循环里每拍 `gauge_set` 上报 batch/queue 负载。 |
| [`lightllm/utils/health_check.py`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/health_check.py) | 被动探活判定逻辑 `HealthObj.check`（grace_timeout + 在跑请求数）。 |
| [`lightllm/server/httpserver/manager.py`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py) | 维护两个健康标记 SharedInt，以及请求级 histogram/counter 上报。 |
| [`lightllm/server/api_start.py`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py) | 拉起 metric 进程与（可选）health_monitor 进程，并分配 `metric_port`。 |

## 4. 核心概念与源码讲解

本讲按数据流方向拆成三个最小模块：

1. **指标采集**：数据怎么从 Router/HttpServer 流进中央账本（生产者→账本）。
2. **指标查询**：账本怎么变成 Prometheus 文本被外部拉取（账本→外部）。
3. **健康检查**：服务可用性怎么判定、谁来自愈（被动探活 + 主动监控）。

### 4.1 指标采集：MetricClient → MetricServer → Monitor

#### 4.1.1 概念说明

LightLLM 的指标生产者很多（Router 上报 batch 大小、HttpServer 上报请求延迟、PD master 上报请求计数……），但账本必须只有一份，否则 Prometheus 会因为重复注册同名指标而报错。因此设计上把账本独占式地放在一个 **metric 进程**里，其余进程都是「只写不改结构」的客户端。

核心设计有三点：

- **生产者解耦**：`MetricClient` 是一个后台线程，暴露 `counter_inc`/`gauge_set` 等同步风格的方法，但内部只把「待执行的任务」塞进一个 `queue.Queue`，**立刻返回**——绝不阻塞调用方（Router 调度循环绝对不能因为上报指标而卡住）。
- **异步 rpyc**：真正的 rpyc 调用由后台线程串行执行；查询类（`generate_latest`）则用 `rpyc.async_` 包装成协程，可被 HttpServer 的 async 端点 `await`。
- **超时容错**：rpyc 默认 3 秒超时会被装饰器统一改写成 30 秒，且客户端 `run` 循环吞掉异常只记日志，保证「账本进程偶尔抖动也不会拖垮生产者」。

#### 4.1.2 核心流程

生产者上报一个 gauge 的完整路径：

```text
Router._step() 每拍调用
  └─ self.metric_client.gauge_set("lightllm_queue_size", n)
       └─ 内部构造闭包 inner_func，put_nowait 进 task_queue（满了只 warning 丢弃）
            └─ MetricClient.run() 后台线程循环
                 └─ task_func() = self.conn.root.gauge_set(...)   # 同步 rpyc 调用
                      └─ rpyc 跨进程序列化
                           └─ MetricServer.exposed_gauge_set(name, value)
                                └─ self.monitor.gauge_set(name, value)
                                     └─ monitor_registry[name].labels(model_name=...).set(value)
```

要点：**调用方写入的只是一个闭包，真正的网络调用发生在后台线程**；即使账本进程重启或卡顿，最坏情况是 `task_queue` 堆满（4096 条）后丢弃新任务并打印 warning，调度循环本身不受影响。

#### 4.1.3 源码精读

**① 账本本身：`Monitor` 与 `MONITOR_INFO`**

所有可用指标名及其说明集中在一个字典里，[metrics.py:7-35](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/metrics.py#L7-L35) 定义了它们。几个本讲重点关注的关键 gauge：

```python
# metrics.py:19-34（节选）
"lightllm_batch_next_size": "Batch size of the next new batch",       # histogram
"lightllm_batch_current_size": "Current batch size",                  # gauge
"lightllm_batch_pause_size": "The number of pause requests",          # gauge
"lightllm_queue_size": "Queue size",                                  # gauge
"lightllm_batch_current_max_tokens": "dynamic max token used for current batch", # gauge
"lightllm_num_running_reqs": "Number of running requests",            # gauge
```

`Monitor.__init__` 在 [metrics.py:46-65](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/metrics.py#L46-L65) 建立唯一的 `CollectorRegistry()`，并生成延迟桶边界。延迟桶用的是**等比数列**（不是等差），因为延迟跨数量级——从毫秒到分钟：

\[ b_k = 0.001 \times 1.5^{k}, \quad k = 0, 1, \dots, 34 \]

即从 0.001 秒起步、每桶乘 1.5、共 35 个桶，最大约 \(0.001 \times 1.5^{34} \approx 1000\) 秒。见 [metrics.py:48-54](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/metrics.py#L48-L54)。

`init_metrics` 在 [metrics.py:67-113](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/metrics.py#L67-L113) 把每类指标用对应的 `create_*` 工厂登记进 registry，其中所有指标都自动带一个固定 label `model_name`（取自 `--model_name`，默认 `default_model_name`），用以在同表里区分不同模型。注意输入长度桶、生成长度桶都按 `--max_req_total_len` 等分 100 档动态生成 [metrics.py:77-85](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/metrics.py#L77-L85)——所以指标桶边界会随启动参数自适应。

写入操作只有四种，全部落到 [metrics.py:129-145](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/metrics.py#L129-L145)，例如 `gauge_set`：

```python
# metrics.py:144-145
def gauge_set(self, name, value):
    self.monitor_registry[name].labels(model_name=self.model_name).set(value)
```

注意 `counter_inc`/`histogram_observe` 还接受一个可选 `label` 参数（用作 `method` 维度），用以区分 prefill/decode 两类推理步，见 [metrics.py:129-142](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/metrics.py#L129-L142)。

**② 服务端：`MetricServer`（rpyc Service）**

metric 进程的入口 [manager.py:149-163](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L149-L163) `start_metric_manager` 做三件事：注册优雅退出、设进程名 `lightllm::<server>::metric_manager`、启动 `ThreadedServer`。若配了 `--metric_gateway` 还会额外开一个推送线程。

`MetricServer` 作为 rpyc Service，把 `Monitor` 的四个写方法逐一暴露成 `exposed_*`，见 [manager.py:48-62](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L48-L62)：

```python
# manager.py:57-62
def exposed_gauge_set(self, name: str, value: float) -> None:
    return self.monitor.gauge_set(name, value)

def exposed_generate_latest(self) -> bytes:
    data = generate_latest(self.monitor.registry)   # 查询入口（见 4.2）
    return data
```

注意一个常被忽视的细节——**rpyc 默认 3 秒超时被统一改写成 30 秒**，见 [manager.py:20-29](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L20-L29)，用猴子补丁（monkey patch）替换 `SocketStream._connect` 的默认 `timeout`。这是为了在 `push_to_gateway` 等慢操作期间不至于误杀正常调用。

**③ 客户端：`MetricClient`（后台线程 + 任务队列）**

生产者侧的 `MetricClient` 继承 `threading.Thread`，构造时立即 `self.start()` 起一个后台守护线程，见 [manager.py:79-99](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L79-L99)：

```python
# manager.py:97-99
self.task_queue = queue.Queue(maxsize=4096)
self.daemon = True
self.start()
```

写入方法的实现套路一致——**只构造闭包塞队列，立即返回**，例如 `gauge_set`（[manager.py:126-131](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L126-L131)）：

```python
# manager.py:126-131
def gauge_set(self, *args, **kwargs):
    def inner_func():
        return self.conn.root.gauge_set(*args, **kwargs)
    self._append_task(inner_func)
    return
```

`_append_task` 在 [manager.py:133-138](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L133-L138) 用 `put_nowait`，队列满（4096）时只打 warning 并丢弃——这是刻意的**背压降级**：宁可丢指标，也不阻塞调度循环。

后台线程的 `run` 方法（[manager.py:140-146](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L140-L146)）就是一个 `while True: get(); call()` 循环，单条异常只记 error 不退出：

```python
# manager.py:140-146
def run(self):
    while True:
        task_func = self.task_queue.get()
        try:
            task_func()
        except Exception as e:
            logger.error(f"monitor error {str(e)}")
```

唯一的「读」方法 `generate_latest` 走不同路径——用 `rpyc.async_` + `asyncio.to_thread` 包装成协程（[manager.py:85-95](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L85-L95)），以便 `/metrics` 这个 async 端点能 `await` 它，4.2 节详述。

**④ 生产者：Router 每拍上报关键 gauge**

Router 在调度循环 `loop_for_fwd` 里，每拍 `_step` 之后、`asyncio.sleep` 之前，集中上报一组 gauge，见 [router/manager.py:247-259](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L247-L259)（实际在 router 模块）：

```python
# router/manager.py:247-259（节选）
self.metric_client.gauge_set("lightllm_batch_pause_size", self._get_paused_req_num())
...
self.metric_client.gauge_set("lightllm_batch_current_size", len(self.running_batch.reqs))
self.metric_client.gauge_set("lightllm_num_running_reqs", len(self.running_batch.reqs))
self.metric_client.gauge_set("lightllm_queue_size", self.req_queue.get_wait_req_num())
self.metric_client.gauge_set(
    "lightllm_batch_current_max_tokens",
    int(sum(...) * self.max_total_token_num),
)
```

> ⚠️ 上面的永久链接路径应为 `lightllm/server/router/manager.py`（此处为提示读者位置，下方统一用正确路径引用）。

正确链接：[lightllm/server/router/manager.py:247-259](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L247-L259)。这几个 gauge 的物理含义：

| Gauge | 含义 | 来源 |
| --- | --- | --- |
| `lightllm_batch_current_size` | 当前在跑 batch 里的请求数 | `len(running_batch.reqs)` |
| `lightllm_num_running_reqs` | 在跑请求数（与上者同值，语义冗余便于告警） | 同上 |
| `lightllm_queue_size` | 等待区里的请求数 | `req_queue.get_wait_req_num()` |
| `lightllm_batch_pause_size` | 因显存不足被暂停的请求数 | `_get_paused_req_num()` |
| `lightllm_batch_current_max_tokens` | 当前各 DP 组动态最大 token 之和（≈当前可承载上界） | `Σ dynamic_max_load × max_total_token_num` |

构造 `MetricClient` 的位置在 [router/manager.py:96](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L96) 与 [httpserver/manager.py:112](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L112)，都连到同一个 `args.metric_port`——这正是「多个生产者、一个账本」的体现。HttpServer 侧则在请求完成时上报延迟直方图与成功/失败计数，典型代码在 [httpserver/manager.py:766-783](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L766-L783)（如 `lightllm_request_first_token_duration`、`lightllm_cache_hit_rate`、`lightllm_gen_throughput`）。

#### 4.1.4 代码实践

**实践目标**：在源码层面追踪「Router 如何把一个 gauge 写进中央账本」，并对照 `MONITOR_INFO` 确认指标含义。

**操作步骤**：

1. 打开 [`metrics.py`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/metrics.py)，在 `MONITOR_INFO`（7-35 行）里找到 `lightllm_batch_current_size` 和 `lightllm_queue_size` 的说明字符串。
2. 打开 [`router/manager.py`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py)，定位 247-259 行的 `gauge_set` 调用块；注意它处在 `loop_for_fwd` 的 `if self.running_batch is None: ... else:` 的 else 分支里（即非空 batch 才上报真实值）。
3. 打开 [`manager.py`（metrics）](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py)，沿 `MetricClient.gauge_set` → `_append_task` → `run` → `self.conn.root.gauge_set` → `MetricServer.exposed_gauge_set` → `Monitor.gauge_set` 走一遍。
4. 额外：注意 [router/manager.py:260-267](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L260-L267) 的 `else` 分支——当 `running_batch is None` 时，每 300 拍才把上述 gauge 全部清零，避免账本里残留上一次运行的旧值。

**需要观察的现象**：`gauge_set` 链路上有「两次身份转换」——Router 侧的同步方法调用，被 `MetricClient` 转成异步队列任务，最终在后台线程里还原成对 rpyc 远端方法的同步调用。

**预期结果**：你能画出 4.1.2 节那张调用链图，并解释「为什么 4096 队列满了不会拖垮 Router」。若本地起服务并装了 prometheus_client，可对照 `/metrics` 输出确认这些 gauge 的 label 只有 `model_name`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MetricClient` 的写方法（`gauge_set` 等）用「塞闭包进队列」而不是直接 `rpyc` 同步调用？

**参考答案**：直接同步调用会让 Router 的调度循环（每拍仅约 30ms，见 u2-l5）被网络往返与账本进程的繁忙程度绑架；塞队列后调用立即返回，真正的 rpyc 调用交给后台守护线程串行执行，最坏情况是队列满后丢弃指标（`put_nowait` + warning），实现了「指标采集失败不影响推理」的降级。

**练习 2**：`lightllm_batch_current_size` 与 `lightllm_num_running_reqs` 在源码里取的是同一个值 `len(self.running_batch.reqs)`，为什么要登记两次？

**参考答案**：语义冗余但便于不同视角的告警与看板——前者强调「batch 维度」（配合 `batch_next_size`/`batch_pause_size` 看 batch 健康），后者强调「请求维度」（配合 `queue_size`、请求级 counter 看用户视角的并发）。同一份原始数据、两种命名语义，成本几乎为零。

---

### 4.2 指标查询：`/metrics` 端点与可选推送

#### 4.2.1 概念说明

中央账本攒下数据后，要变成外部可消费的形式。LightLLM 同时支持两种暴露方式：

- **Pull（拉取，默认）**：Prometheus server 周期性 GET 服务的 `/metrics` 端点，拉取 Prometheus exposition format 文本。这是云原生场景的标准做法。
- **Push（推送，可选）**：当服务在防火墙后或生命周期短（如批处理）时，由 metric 进程周期性把账本 push 到一个 `pushgateway`，再由 Prometheus 从 gateway 拉。

两种方式共用同一份 `CollectorRegistry`，互不冲突。

#### 4.2.2 核心流程

**Pull 路径**（`/metrics`）：

```text
Prometheus --GET /metrics--> FastAPI
  └─ metrics() async 端点
       └─ await g_objs.metric_client.generate_latest()   # 跨进程 rpyc async 调用
            └─ rpyc async -> MetricServer.exposed_generate_latest()
                 └─ prometheus_client.generate_latest(self.monitor.registry)  -> bytes
  ← Response(bytes, mimetype="text/plain")
```

**Push 路径**（仅当 `--metric_gateway` 非空时启用）：

```text
MetricServer.push_metrics() 后台线程（间隔 --push_interval 秒）
  └─ self.monitor.push_metrices()
       └─ push_to_gateway(gateway_url, job=job_name, grouping_key, registry, handler?)
```

#### 4.2.3 源码精读

**① `/metrics` 端点：异步跨进程读取**

[api_http.py:405-410](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_http.py#L405-L410) 是整个暴露逻辑的核心，只有四行：

```python
# api_http.py:405-410
@app.get("/metrics")
async def metrics() -> Response:
    data = await g_objs.metric_client.generate_latest()
    response = Response(data)
    response.mimetype = "text/plain"
    return response
```

关键点：

- 端点是 **`async`**，因为 `generate_latest` 要跨进程 rpyc，期间不能阻塞事件循环。
- `MetricClient.generate_latest` 在 [manager.py:101-103](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L101-L103) `await` 了构造期用 `rpyc.async_` 包装好的 `self._generate_latest`（[manager.py:95](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L95)）。这里复用了 u2-l4 讲过的 `async_wrap` 思路：`rpyc.async_(f)` 返回一个 `AsyncResult`，再用 `asyncio.to_thread(ans.wait)` 把「等待 rpyc 应答」这个阻塞动作丢到线程池，从而桥入 async 循环（[manager.py:85-93](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L85-L93)）。
- 返回的 `bytes` 就是标准 Prometheus 文本，`mimetype="text/plain"`（更严格应为 `text/plain; version=0.0.4`，但 Prometheus 容忍）。

服务端的 `exposed_generate_latest` 直接调用 `prometheus_client.generate_latest(self.monitor.registry)`（[manager.py:60-62](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L60-L62)），把整份 registry 序列化成文本。

**② Push 路径：`push_to_gateway`**

`push_metrics` 是一个独立后台线程（[manager.py:64-76](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L64-L76)），每隔 `--push_interval`（默认 10 秒，[api_cli.py:482](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L482)）调一次 `push_metrices`，并在累计满 60 秒时打一条 `push metrices success` 日志：

```python
# manager.py:64-76
def push_metrics(self):
    time_counter = 0
    while True:
        try:
            self.monitor.push_metrices()
            if time_counter >= 60:
                logger.info("push metrices success")
                time_counter = 0
        except:
            pass
        finally:
            time.sleep(self.interval)
            time_counter += self.interval
```

`Monitor.push_metrices` 在 [metrics.py:147-160](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/metrics.py#L147-L160) 根据 `--enable_monitor_auth` 决定是否带认证 handler——认证时用 `my_auth_handler` 从环境变量 `USERNAME`/`PASSWORD` 取账号密码（[metrics.py:38-43](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/metrics.py#L38-L43)）。注意整个 push 循环对异常**完全吞掉**（`except: pass`），因为 push 失败不应影响 metric 进程寿命。该线程只有在 `--metric_gateway` 非空时才会被创建（[manager.py:155-157](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L155-L157)）。

**③ 启动期相关 CLI 参数**

| 参数 | 默认 | 含义 | 位置 |
| --- | --- | --- | --- |
| `--model_name` | `default_model_name` | 所有指标的固定 label 值 | [api_cli.py:98-101](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L98-L101) |
| `--metric_gateway` | None | pushgateway 地址；为空则只走 pull | [api_cli.py:477](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L477) |
| `--job_name` | `lightllm` | push 时的 job 名 | [api_cli.py:478](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L478) |
| `--grouping_key` | `[]` | push 时的分组键（可多次 `key=value`） | [api_cli.py:480](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L480) |
| `--push_interval` | 10 | push 周期（秒） | [api_cli.py:482](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L482) |
| `--enable_monitor_auth` | False | push 是否启用 basic auth | [api_cli.py:552](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L552) |

注意：**`metric_port` 不是 CLI 参数**，它由 `api_start.py` 在启动期自动分配空闲端口并写回 `args.metric_port`（[api_start.py:370](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L370) 与 [api_start.py:397](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L397)），属内部端口，承接 u1-l5 的端口分配机制。

#### 4.2.4 代码实践

**实践目标**：理解 `/metrics` 暴露的文本格式，并区分 pull 与 push 两种路径的触发条件。

**操作步骤**：

1. 打开 [api_http.py:405-410](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_http.py#L405-L410)，确认 `/metrics` 的响应类型是 `text/plain`。
2. 沿 `generate_latest` 这一个**唯一的读路径**，确认它走的是 `rpyc.async_`（与写路径的「同步闭包 + 队列」不同），见 [manager.py:95](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L95) 与 [manager.py:101-103](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/metrics/manager.py#L101-L103)。
3. 若本地已启动服务（参考 u1-l2），执行 `curl http://127.0.0.1:8000/metrics`，观察输出。**待本地验证**——预期会看到形如：

   ```text
   # HELP lightllm_batch_current_size Current batch size
   # TYPE lightllm_batch_current_size gauge
   lightllm_batch_current_size{model_name="default_model_name"} 0.0
   ...
   # HELP lightllm_request_count The total number of requests
   # TYPE lightllm_request_count counter
   lightllm_request_count{model_name="default_model_name"} 3.0
   ```

4. 在请求期间（可先发一个 `/generate`）重复 `curl /metrics`，观察 `lightllm_queue_size`、`lightllm_num_running_reqs` 是否随请求起伏。

**需要观察的现象**：`# TYPE` 行标注了每个指标是 `counter`/`gauge`/`histogram`；histogram 还会附带 `_bucket`、`_sum`、`_count` 三类派生行。

**预期结果**：你能从 `/metrics` 输出里至少识别出本讲 4.1 表格中列出的 5 个 gauge，并理解它们的 label 只有 `model_name`。若无运行环境，则做源码阅读型实践——对照 `MONITOR_INFO` 逐一标注每个指标的类型。

#### 4.2.5 小练习与答案

**练习 1**：`generate_latest` 为什么不像 `gauge_set` 那样走「队列 + 后台线程」，而要用 `rpyc.async_`？

**参考答案**：因为 `/metrics` 是一个 HTTP 端点，调用方（FastAPI 路由）需要拿到返回值才能响应客户端，必须 `await`；而写路径是 fire-and-forget（fire 之后立即返回，不需返回值）。`rpyc.async_` + `asyncio.to_thread(ans.wait)` 让 HTTP 协程能非阻塞地等待 rpyc 应答，又不阻塞事件循环。

**练习 2**：什么情况下需要启用 `--metric_gateway`？

**参考答案**：当 LightLLM 部署在 Prometheus 无法主动 scrape 的网络位置（如 NAT/防火墙之后），或服务实例生命周期很短（难以被周期性 scrape 到）时，用 push 模式把指标先推到一个 pushgateway，再由 Prometheus 从 gateway 拉。常规暴露在固定端口的服务直接用 pull（`/metrics`）即可，无需配 gateway。

---

### 4.3 健康检查：被动 `/health` 与主动 `health_monitor`

#### 4.3.1 概念说明

健康检查与指标监控不同：指标回答「忙不忙」，健康检查回答「死没死」。LightLLM 设计了**两套互补**的健康检查机制，理解它们的分工是本模块的核心：

| 机制 | 触发方 | 谁判定 | 失败后果 | 用途 |
| --- | --- | --- | --- | --- |
| **被动 `/health` 端点** | 外部（k8s、LB、Prometheus blackbox）被动 GET | HttpServer 读两个共享内存标记 | 返回 200 或 503 | 存活探针，给负载均衡/k8s 用 |
| **主动 `health_monitor` 进程** | LightLLM 自己周期性自检 | 监控进程 GET 自己的 `/health` + 检查所有子进程存活 | 连续 3 次失败 → `SIGKILL` 整棵进程树自愈 | 自我兜底，防止「半死不活」僵尸服务 |

二者共用同一个 `/health` 端点与 `health_check` 判定函数，形成「被动判定逻辑被主动进程复用」的优雅复用。

#### 4.3.2 核心流程

**被动判定逻辑**（`/health` → `health_check`）：

```text
GET /health
  └─ healthcheck() 端点
       └─ 若 pd_master：直接 200
       └─ 否则 health_check(shm_req_manager)
            └─ HealthObj.check():
                 now = time.time()
                 若 (now - latest_success_infer_time) <= grace_timeout(默认200s)  → 健康(200)
                 否则 若 run_reqs_count==0 且 shm_req_manager.is_idle()         → 健康(200)
                 否则                                                              → 不健康(503)
```

直觉解释：**「最近还在正常出 token」就当健康**；超过宽限期还没成功，但当前没活儿在跑（可能是空闲），也算健康；只有「长时间没成功 + 还有卡住的在跑请求」才算病——这正是「半死卡死」的典型征兆。

**主动监控流程**（`health_monitor` 进程）：

```text
start_health_check_process（独立进程）
  └─ 先 sleep 6s 等服务起来
  └─ while True:
       health_monitor(url, all_process_ids):
         1. all_is_alive(all_process_ids)   # psutil 检查所有兄弟进程活着
         2. 若全活 → requests.get("/health", timeout=60)，200 才算过
         3. 失败 → consecutive_failures += 1
         4. 若 consecutive_failures >= 3 → SIGKILL 全部子进程 → SIGKILL 父进程 → 自 exit
       sleep(HEALTH_CHECK_INTERVAL_SECONDS, 默认88s)
```

#### 4.3.3 源码精读

**① 被动 `/health` 端点**

[api_http.py:190-205](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_http.py#L190-L205) 同时注册了 `/healthz`、`/health`（GET 与 HEAD）：

```python
# api_http.py:190-205
@app.get("/healthz", summary="Check server health")
@app.get("/health", summary="Check server health")
@app.head("/health", summary="Check server health")
async def healthcheck(request: Request):
    if g_objs.args.run_mode == "pd_master":
        return JSONResponse({"message": "Ok"}, status_code=200)

    if os.environ.get("DEBUG_HEALTHCHECK_RETURN_FAIL") == "true":
        return JSONResponse({"message": "Error"}, status_code=503)
    from lightllm.utils.health_check import health_check

    is_healthy = health_check(g_objs.httpserver_manager.shm_req_manager)
    return JSONResponse(
        {"message": "Ok" if is_healthy else "Error"},
        status_code=200 if is_healthy else 503,
    )
```

三个细节值得注意：

- **pd_master 直接返回 200**：因为 pd_master 不直接做推理，它只做调度配对，没有「成功推理时间戳」可读。
- **`DEBUG_HEALTHCHECK_RETURN_FAIL` 环境变量**：这是给运维/测试用的「强制失败」开关，设为 `true` 即让 `/health` 永远返回 503，用于演练主动监控的自愈逻辑。
- 还提供了更轻量的 `/liveness`、`/readiness`（[api_http.py:172-181](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_http.py#L172-L181)），它们恒返回 `{"status":"ok"}`，只表示「进程能响应」，不含推理健康判定，适合做 k8s 的 liveness/readiness probe。

**② 判定核心：`HealthObj.check`**

[health_check.py:16-46](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/health_check.py#L16-L46) 是整个被动检查的大脑：

```python
# health_check.py:25-42
def check(self, shm_req_manager) -> bool:
    try:
        now = time.time()
        last_success_time = self.latest_success_infer_time_mark.get_value()
        if now - last_success_time <= self.grace_timeout:        # 宽限期内出过 token
            return True
        elif self.run_reqs_count_mark.get_value() == 0 and shm_req_manager.is_idle():
            return True                                          # 没活儿，空闲健康
        else:
            logger.warning("Health check failed: no success for %ss and in-flight shm requests remain", ...)
            return False
    except Exception as e:
        logger.exception(str(e))
        return False
```

宽限期 `grace_timeout` 默认 200 秒，取自环境变量 `HEALTH_TIMEOUT`（[health_check.py:18](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/health_check.py#L18)）。两个标记是 `SharedInt`（[health_check.py:22-23](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/health_check.py#L22-L23)），名字带 `get_unique_server_name()` 前缀以做命名空间隔离（承接 u2-l3 的共享内存命名约定）。

**③ 健康标记谁来写**

被动检查读的两个标记由 HttpServer 进程写入：

- 构造期初始化为「当前时间」与 0：[httpserver/manager.py:122-127](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L122-L127)（保证服务刚启动、还没出过 token 时也算健康）。
- `run_reqs_count_mark`：请求开始处理时 `+1`（[httpserver/manager.py:335](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L335)），处理完成时 `-1`（[httpserver/manager.py:486](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L486)）。
- `latest_success_infer_time_mark`：每收到一个成功生成的 token 就刷新为当前时间：[httpserver/manager.py:723](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L723)。

于是「持续出 token」⟺「时间戳持续刷新」⟹「`/health` 持续 200」。

**④ 主动监控进程：自愈兜底**

[health_monitor/manager.py:21-56](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/health_monitor/manager.py#L21-L56) 是自愈核心。它先做「进程存活」检查（`all_is_alive`，用 psutil，[manager.py:59-74](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/health_monitor/manager.py#L59-L74)），全活才去 GET `/health`：

```python
# health_monitor/manager.py:24-33
all_processes_is_alive = all_is_alive(all_process_ids)
if all_processes_is_alive:
    response = requests.get(url, timeout=60)
    if response.status_code == 200:
        logger.info("Health check passed")
        consecutive_failures = 0
    else:
        raise Exception(f"Health check failed with status code: {response.status_code}")
else:
    raise Exception("not all processes is alive")
```

失败计数 `consecutive_failures` 是模块级全局变量（[manager.py:18](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/health_monitor/manager.py#L18)），任何异常都让它 `+1`；一旦达到 3 次即触发清理（[manager.py:41-55](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/health_monitor/manager.py#L41-L55)）：

```python
# health_monitor/manager.py:41-55（节选）
if consecutive_failures >= 3:
    import signal
    for pid in all_process_ids[1:]:      # 先杀子进程
        os.kill(pid, signal.SIGKILL)
    os.kill(all_process_ids[0], signal.SIGKILL)   # 再杀父进程
    sys.exit(-1)                          # 自杀
```

「关心的进程清单」由 `get_all_cared_pids` 用 psutil 递归枚举父进程的全部子进程（[manager.py:77-87](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/health_monitor/manager.py#L77-L87)），`all_process_ids[0]` 是父进程（`os.getppid()`）。注意它用 `SIGKILL`（不可拦截）而非 `SIGTERM`——因为目标是「确保死透」，靠外层（如 systemd/k8s）拉起新实例。

进程入口 `start_health_check_process`（[manager.py:90-111](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/health_monitor/manager.py#L90-L111)）先 `sleep(6)` 等服务起来，再以 `HEALTH_CHECK_INTERVAL_SECONDS`（默认 88 秒，环境变量可改）为周期循环探活。组合起来：**88s 探一次、连续 3 次失败（约 4~5 分钟无恢复）才杀进程**，既不会误杀短暂抖动，又能在真卡死时自愈。

**⑤ 启动期拉起**

被动 `/health` 无需单独进程——它就活在 HttpServer 里。主动监控则需要显式拉起，且**默认关闭**，由 `--health_monitor` 开启（[api_cli.py:475](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L475)）。拉起位置在 `normal_or_p_d_start` 末尾（[api_start.py:519-522](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L519-L522)）和 `pd_master_start`（[api_start.py:584-587](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L584-L587)）。metric 进程则总是被拉起（[api_start.py:478-483](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L478-L483)），且必须早于 router（router 构造期就连 `metric_port`，详见 u1-l5）。

#### 4.3.4 代码实践

**实践目标**：复现 `/health` 的三种返回状态（健康/空闲健康/不健康），并验证主动监控的「3 次失败自愈」阈值。

**操作步骤**：

1. 打开 [health_check.py:25-42](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/health_check.py#L25-L42)，把判定逻辑改写成下面的伪代码，确认三种分支：

   ```text
   if 最近成功 <= 200s:        健康分支A（活跃）
   elif 在跑==0 且 shm空闲:    健康分支B（空闲）
   else:                       不健康
   ```

2. 沿「谁写标记」确认 `latest_success_infer_time_mark` 在 [httpserver/manager.py:723](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L723) 每出一个 token 就刷新。
3. 若本地起服务，进行下面三组验证（**待本地验证**）：
   - **空闲健康**：服务刚启动不发请求，`curl -i http://127.0.0.1:8000/health` → 预期 200（命中分支 B：构造期时间戳被初始化为当前时间，且无在跑请求）。
   - **强制失败**：启动时设环境变量 `DEBUG_HEALTHCHECK_RETURN_FAIL=true`，再 `curl /health` → 预期 503（用于演练主动监控）。
   - **探活自愈**：以 `--health_monitor` 启动并设 `DEBUG_HEALTHCHECK_RETURN_FAIL=true`、`HEALTH_CHECK_INTERVAL_SECONDS=10`，观察日志——预期每 10s 出现一次 `Health check failed`，连续 3 次后出现 `kill all process` 并整体退出。
4. 源码阅读型补充：在 [health_monitor/manager.py:41-55](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/health_monitor/manager.py#L41-L55) 确认杀进程顺序是「先子后父再自杀」，并思考为什么用 `SIGKILL` 而非 `SIGTERM`。

**需要观察的现象**：被动检查的响应时间应是毫秒级（只读两个共享内存整数 + `is_idle()`），不含任何网络往返；主动监控的失败计数是累计的，单次成功即清零（`consecutive_failures = 0`）。

**预期结果**：你能说清「为什么服务空闲时也健康」（分支 B）以及「主动监控为什么要连失败 3 次才动手」（避免误杀抖动）。若无运行环境，则聚焦步骤 1、2、4 的源码追踪。

#### 4.3.5 小练习与答案

**练习 1**：`/health` 端点的判定依赖 `latest_success_infer_time_mark`，但服务刚启动时还没出过任何 token，这个标记的初值是什么？为什么不会一启动就被判 503？

**参考答案**：初值是 HttpServer 构造期的当前时间戳（[httpserver/manager.py:123-124](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L123-L124) 的 `set_value(int(time.time()))`）。由于刚启动时 `now - last_success_time` 必然小于 200s 宽限期，因此命中分支 A 直接判健康。这是一个刻意的「暖启动」设计，避免冷启动期间被 k8s 误判为不健康而重启。

**练习 2**：主动 `health_monitor` 进程为什么用 `SIGKILL` 而不是 `SIGTERM`，且要先杀子进程再杀父进程？

**参考答案**：服务已被判定为「连续 3 次健康检查失败」的病态，往往意味着某些子进程已经卡死、可能无法响应 `SIGTERM` 的优雅退出逻辑；用 `SIGKILL`（不可拦截、内核直接终止）是为了「确保死透」，把重启责任交给外层编排系统（systemd/k8s）。先杀子进程再杀父进程，是为了避免父进程在子进程尚未退出时先死、导致子进程变孤儿而漏网。

---

## 5. 综合实践

把三个最小模块串起来，完成一次「端到端可观测性」演练。

**任务**：启动一个 LightLLM 服务（参考 u1-l2），在不看源码的前提下，仅凭 `/metrics` 与 `/health` 两个端点，回答下面五个问题；然后再回到源码核对答案。

1. 当前服务在跑多少个请求？（查哪个 gauge？）
2. 当前等待区有多少请求排队？
3. 服务累计处理了多少个请求？成功了多少、失败了多少？（注意 counter 的名字）
4. 最近一次 prefill/decode 步的耗时大概在哪个桶？（观察 histogram 的 `_bucket`）
5. 服务现在健康吗？判定依据是分支 A 还是分支 B？

**操作建议**：

- 用两个终端：一个持续 `watch -n1 'curl -s http://127.0.0.1:8000/metrics | grep -E "lightllm_(batch_current_size|queue_size|request_count|request_success|request_failure|num_running_reqs)"'`；另一个发 `/generate` 请求制造负载（可用 `while true; do curl ...; done` 制造并发）。
- 同时 `curl -s http://127.0.0.1:8000/health` 观察健康状态。
- 记录下「请求进入 → queue_size 上升 → batch_current_size 上升 → 请求完成 → request_count/success 上升」的完整波形。

**预期结果**：你能把 `/metrics` 里看到的每个数值，反向追溯到 4.1.3 节中的某一行源码（例如 `lightllm_queue_size` ← `req_queue.get_wait_req_num()` ← [router/manager.py:252](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L252)），并能解释为什么 `/health` 在负载期间持续返回 200。若无法运行，请写出「源码阅读型」答案——即逐一指出每个问题对应 `MONITOR_INFO` 里的哪条指标、由哪个进程在哪行代码上报。

## 6. 本讲小结

- LightLLM 的指标体系基于 `prometheus_client`，用一个独立 **metric 进程**持有唯一 `CollectorRegistry`（中央账本），其余进程都是只写客户端——**多生产者、一个账本**。
- 生产者侧的 `MetricClient` 是后台线程 + `queue.Queue(4096)`：写方法只塞闭包立即返回（fire-and-forget），后台线程串行执行 rpyc 调用；队列满即丢弃并 warning，**绝不阻塞调度循环**。
- 关键 gauge 由 Router 每拍上报：`lightllm_batch_current_size`、`lightllm_num_running_reqs`、`lightllm_queue_size`、`lightllm_batch_pause_size`、`lightllm_batch_current_max_tokens`；请求级 counter/histogram 由 HttpServer 在请求完成时上报。
- `/metrics` 端点是**唯一的读路径**，用 `rpyc.async_` 把跨进程读取桥入 async 端点，输出标准 Prometheus 文本；`--metric_gateway` 非空时另起 push 线程周期推送 pushgateway。
- 健康检查分两层：**被动 `/health`** 端点读两个 `SharedInt` 标记（最近成功推理时间、在跑请求数），按「宽限期 / 空闲」三分支判定，秒级被动响应；**主动 `health_monitor`** 进程（需 `--health_monitor` 开启）周期 GET `/health` 并查子进程存活，连续 3 次失败即 `SIGKILL` 整棵进程树自愈。
- 健康标记由 HttpServer 维护：构造期暖启动为当前时间，每出一个 token 刷新 `latest_success_infer_time_mark`，请求进出时增减 `run_reqs_count_mark`。

## 7. 下一步学习建议

本讲是第七单元的收尾。至此你已经走完了从「项目认知」到「请求链路」「推理内核」「KV 缓存」「模型适配」「性能优化」「分布式部署与可观测性」的完整学习路径。建议的后续方向：

- **动手运维**：用本讲的 `/metrics` 端点接入一个真实的 Prometheus + Grafana，把 `lightllm_batch_current_size`、`lightllm_queue_size`、`lightllm_request_first_token_duration` 做成看板，体会「指标→告警→自愈」的闭环。
- **回看调度**：带着本讲的 `lightllm_batch_current_max_tokens`、`lightllm_num_running_reqs` 回到 u2-l5/u2-l6/u4-l3，从「指标出口」反推「调度决策」，理解 Router 的激进/保守切换如何在指标上体现。
- **PD 场景的可观测性**：若关注 u7-l1 的 PD 分离部署，可对比 `httpserver_for_pd_master/manager.py` 与普通 `httpserver/manager.py` 的指标上报差异，思考 pd_master 节点应关注哪些指标。
- **贡献新指标**：参照 `MONITOR_INFO` + `init_metrics` 的登记范式，尝试为某个尚未覆盖的维度（如单 DP 组粒度的队列长度）新增一个 gauge，完成「登记→上报→`/metrics` 暴露」的完整闭环——这是检验你是否真正读懂本讲的最佳练习。

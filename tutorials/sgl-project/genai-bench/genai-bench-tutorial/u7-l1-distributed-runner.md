# DistributedRunner 主从架构

## 1. 本讲目标

本讲是「专家层」的第一站，我们要拆开 genai-bench 的「发动机舱」，看清一次压测在多进程层面到底是怎么跑起来的。

学完后你应该能够：

- 说清 **master / worker / local 三种运行模式**各自的职责，以及 `DistributedConfig` 与 `setup()` 是如何根据 `num_workers` 分流的。
- 画出 master 与 worker 之间的 **消息收发图**：谁发送 `update_scenario` / `update_batch_size`，谁接收 `request_metrics`，以及 `_register_message_handlers` 为什么「按角色只注册需要的 handler」。
- 解释 **指标如何从 worker 的单条请求汇聚到 master 的 `AggregatedMetricsCollector`**，以及 worker 日志如何跨进程回传、CPU 亲和怎么做、进程结束时如何清理。

本讲只聚焦 `genai_bench/distributed/runner.py` 这一个文件，但会与 u3-l1（`BaseUser` 发送 `request_metrics`）和 u4-l2（`AggregatedMetricsCollector` 聚合）紧密衔接。

## 2. 前置知识

在进入源码前，先用通俗语言把几个基础概念讲清楚。

### 2.1 为什么单进程压测会「不够用」

genai-bench 建立在 [Locust](https://docs.locust.io/) 之上，而 Locust 用 **gevent** 实现「协程式并发」：在一个 Python 进程里，成百上千个「虚拟用户」（greenlet）轮流占用 CPU，发请求、等响应。这种模型对 **网络 I/O 密集** 的场景非常高效——大量时间花在「等远端服务器返回」，等待时 greenlet 让出 CPU 给别人。

但 gevent 是 **单线程、协作式** 的：所有 greenlet 共享 **一个进程、一颗 CPU 核**。当你要制造极高负载（比如每秒成千上万次请求、每个请求还带几万 token 的 payload）时，单进程本身会先把 CPU 跑满。此时会出现这样的日志告警：

```log
CPU usage above 90%! This may constrain your throughput and may even give inconsistent response time measurements!
```

意思是：**打满的不再是远端服务器，而是你这台压测机自己**。测出来的数字是「压测机的极限」而非「被测服务的极限」，结论失真。解决办法就是 **开多个进程**，让每个进程各占一颗 CPU 核，合力制造负载。`DistributedRunner` 正是用来做这件事的。

> Python 还有一条限制：受 GIL 约束，多 **线程** 无法真正并行 CPU 工作；而 gevent 又是单线程的。所以多核压测只能靠 **多进程（multiprocessing）**。

### 2.2 master / worker 是什么

借鉴 Locust 的分布式模型，genai-bench 把一次压测拆成两类角色：

- **worker 进程**：真正「干活」的人。每个 worker 内部跑着自己的虚拟用户群，不断发请求、收响应，把每条请求的指标算好（见 u4-l1 的 `RequestLevelMetrics`）发给 master。worker **不**负责汇总，也不跑主实验循环。
- **master 进程**：「总调度 + 记账员」。它把当前场景、batch size 下发给所有 worker，接收所有 worker 回传的指标，交给 `AggregatedMetricsCollector`（u4-l2）汇总，并驱动实时仪表盘。master 才跑 `benchmark` 的双层 `for` 循环（见 u8-l1）。

两类进程通过 Locust 内置的 master/worker 通信协议（默认监听 `127.0.0.1:5557`）互相发消息。此外还有一种 **local 模式**：不开 worker 进程，单进程既当 master 又当 worker，适合小规模、快速试跑。

### 2.3 你需要记得的两个上游结论

- **u3-l1**：`BaseUser.collect_metrics` 在每条请求结束时，会调用 `environment.runner.send_message("request_metrics", ...)`，把这条请求的 `RequestLevelMetrics` 序列化成 JSON 发出去。这正是 worker → master 指标流的「源头」。
- **u4-l2**：`AggregatedMetricsCollector` 用 `add_single_request_metrics` 收集逐请求指标，一个 `[scenario, concurrency]` 组合算「一次 run」。本讲要讲清「逐请求指标是怎么一条条进入 collector 的」。

## 3. 本讲源码地图

本讲几乎全部围绕下面这个文件，并辅以三处协作点：

| 文件 | 作用 |
| --- | --- |
| [genai_bench/distributed/runner.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py) | 本讲主角。定义 `DistributedConfig` 与 `DistributedRunner`，含进程创建、消息注册、指标/日志接收、CPU 亲和与清理。 |
| [genai_bench/cli/cli.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py) | `benchmark` 函数里构造 `DistributedConfig` / `DistributedRunner`、调用 `setup()`，并在主循环里 `update_scenario` / `update_batch_size`。 |
| [genai_bench/user/base_user.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py) | `collect_metrics` 发送 `request_metrics` 消息，是指标流的上游。 |
| [genai_bench/logging.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py) | `WorkerLoggingManager` / `WorkerRichHandler`，把 worker 日志经队列回传 master。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **进程模型与 setup**——三种模式如何分流、进程怎么建、谁来持有 collector。
2. **消息注册与处理**——master/worker 各注册哪些 handler、四类消息怎么收发。
3. **指标聚合、worker 日志与资源清理**——指标与日志的汇聚路径、CPU 亲和、退出清理。

### 4.1 进程模型与 setup

#### 4.1.1 概念说明

`DistributedRunner` 的核心设计是一个「**同一份代码，按进程角色走不同分支**」的模式。无论你是 master 还是 worker，跑的都是 `DistributedRunner` 这段代码；区别在于：每个进程在 setup 阶段会**根据自己当前持有的 runner 类型**（`MasterRunner` / `WorkerRunner` / local runner）走不同分支，注册不同 handler，承担不同职责。

这一点很关键：worker 进程是由 master 用 `multiprocessing.Process` fork/spawn 出来的，子进程拿到的是同一份 `DistributedRunner` 实例的副本，但它会在自己的 `_worker_process` 里把自己注册成 `WorkerRunner`；而 master 进程则把自己注册成 `MasterRunner`。两边的 `self.environment.runner` 在 setup 之后是不同类型——这就是后续所有 `isinstance` 判断的依据。

只有一个角色会持有 `AggregatedMetricsCollector`：**master 和 local**。worker 永远不持有 collector，它只负责「算单条指标 + 发出去」。

#### 4.1.2 核心流程

setup 的分流逻辑可以画成：

```
runner.setup()
   │
   ├── num_workers > 0 ？── 是 ──▶ _setup_distributed()
   │                              │
   │                              ├── 关闭 TOKENIZERS 并行 (环境变量)
   │                              ├── atexit 注册 cleanup
   │                              ├── _create_workers()  ──▶ fork N 个子进程
   │                              │       每个子进程执行 _worker_process(i):
   │                              │         · WorkerLoggingManager 装日志
   │                              │         · (可选) 设 CPU 亲和
   │                              │         · environment.create_worker_runner()
   │                              │         · 注册 handler（worker 那一份）
   │                              │         · runner.greenlet.join()  永久阻塞
   │                              │
   │                              └── 回到 master 进程：
   │                                    若自己是 WorkerRunner 则 return（防御）
   │                                    否则 _setup_master()
   │                                      · create_master_runner(host,port)
   │                                      · 新建 AggregatedMetricsCollector
   │                                      · sleep(wait_time) 等 worker 连上
   │                                      · 注册 handler（master 那一份）
   │                                      · spawn 日志消费 greenlet
   │
   └── num_workers == 0 ──▶ _setup_local()
                                · create_local_runner()
                                · 新建 AggregatedMetricsCollector
                                · 注册全部 handler
```

几个值得记住的设计决定：

- **「关闭 TOKENIZERS 并行」** 是分布式模式的第一个动作。transformers 底层的 Rust tokenizer 有自己的多线程，和 Python 的 `multiprocessing`（尤其是 fork）会冲突，产生告警甚至死锁，所以要显式关掉。
- **「先建 worker，再建 master」**：worker 进程启动后需要一点时间连上 master，所以 master 在 `create_master_runner` 之后会 `gevent.sleep(wait_time)`（默认 2 秒）等连接，再注册 handler。
- **worker 永久阻塞**：worker 在 `_worker_process` 末尾 `runner.greenlet.join()`，把自己的生命周期交给 Locust 的 worker greenlet，直到 master 发来退出信号或进程被 terminate。

#### 4.1.3 源码精读

先看配置对象。`DistributedConfig` 是一个普通 dataclass，几乎所有字段都有默认值，唯一必填的是 `num_workers`：[genai_bench/distributed/runner.py:L25-L47](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L25-L47)。它在 `__post_init__` 里做了一处健康检查：当 `num_workers` 超过 CPU 核数的 4 倍时打 warning，提醒你「进程太多反而互相抢资源」。

```python
def __post_init__(self):
    cpu_count = multiprocessing.cpu_count()
    if self.num_workers > cpu_count * 4:
        logger.warning(
            f"Number of workers ({self.num_workers}) is much higher than "
            f"available CPU cores ({cpu_count}). This might impact performance."
        )
```

其中 `pin_to_cores` / `cpu_affinity_map` 是实验性的 CPU 亲和开关，默认关闭，留到 4.3 再讲。

入口 `setup()` 极简，纯粹按 `num_workers` 分流：[genai_bench/distributed/runner.py:L132-L137](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L132-L137)。

分布式分支 `_setup_distributed` 是本模块的核心：[genai_bench/distributed/runner.py:L139-L156](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L139-L156)。注意它的执行者其实是 **master 进程**——它先关 TOKENIZERS 并行、注册退出清理、fork 出 N 个 worker 子进程，然后再判断自己是不是 worker（防御性判断，master 路径上恒为 False），最后走 `_setup_master`。

```python
def _setup_distributed(self) -> None:
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    atexit.register(self.cleanup)
    self._worker_processes = self._create_workers()
    # If this is a worker process, exit after setup
    if isinstance(self.environment.runner, WorkerRunner):
        return
    self._setup_master()
```

master 的具体装配在 `_setup_master`：[genai_bench/distributed/runner.py:L158-L171](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L158-L171)。这里能看到「只有 master 持有 collector」这一关键事实——`metrics_collector` 在此创建。

```python
def _setup_master(self) -> None:
    self.environment.create_master_runner(
        master_bind_host=self.config.master_host,
        master_bind_port=self.config.master_port,
    )
    self.metrics_collector = AggregatedMetricsCollector()   # 只有 master/local 有
    gevent.sleep(self.config.wait_time)                     # 等 worker 连上
    self._register_message_handlers()
    self.log_consumer = gevent.spawn(self._consume_worker_logs)
```

worker 进程的「一生」在 `_create_workers` + `_worker_process`：[genai_bench/distributed/runner.py:L216-L249](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L216-L249)。`_create_workers` 只是把 `multiprocessing.Process` 逐个 `start()`（非阻塞）；真正的 worker 逻辑在 `_worker_process` 里：装日志 →（可选）设 CPU 亲和 → 把自己注册成 `WorkerRunner` → 注册 handler → `join` 阻塞。

```python
def _worker_process(self, worker_id: int) -> None:
    try:
        WorkerLoggingManager(str(worker_id), self.worker_log_queue, self.config.log_dir)
        if self.config.pin_to_cores:
            self._set_cpu_affinity(worker_id)
        runner = self.environment.create_worker_runner(
            master_host=self.config.master_host, master_port=self.config.master_port
        )
        self._register_message_handlers()
        logger.info(f"Worker {worker_id} started successfully and connected to master")
        runner.greenlet.join()
    except Exception as e:
        logger.error(f"Worker {worker_id} failed: {str(e)}")
        return   # 注意：吞掉异常，避免被父进程当作「需要重启」
```

这里有个重要细节：worker 出错时 **`return` 而不是 `raise`**。`multiprocessing` 默认会对异常退出的子进程做重启，反复崩溃会形成「重启风暴」；吞掉异常让进程干净退出，是刻意的取舍。

最后是 local 分支，对比之下非常清爽：[genai_bench/distributed/runner.py:L209-L214](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L209-L214)。

调用方在 `cli.py` 的 `benchmark` 函数里：[genai_bench/cli/cli.py:L386-L401](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L386-L401)。可以看到一个对称的「worker 提前 return」逻辑——`setup()` 之后，如果当前进程是 worker，`benchmark` 函数直接 `return`，**不进入主实验循环**。这保证了只有 master/local 跑那个双层 `for`。

```python
runner.setup()
# Worker process doesn't need to run the main benchmark flow ...
if num_workers > 0 and isinstance(environment.runner, WorkerRunner):
    return
# Get metrics collector from runner for master/local mode
if not runner.metrics_collector:
    raise RuntimeError("Metrics collector not initialized")
aggregated_metrics_collector = runner.metrics_collector
```

#### 4.1.4 代码实践

**实践目标**：用单元测试的 mock 方式，亲手验证「`num_workers` 决定 setup 分流」与「worker 进程失败时优雅退出」这两件事，而不真正 fork 进程（避免在你的机器上真开多进程）。

**操作步骤**：

1. 打开 `tests/distributed/test_runner.py`，阅读 `test_local_mode_setup` 和 `test_distributed_mode_setup`：[tests/distributed/test_runner.py:L39-L65](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/distributed/test_runner.py#L39-L65)。注意它如何用 `@patch("multiprocessing.Process")` 和 `@patch("locust.runners.MasterRunner")` 把多进程和 Locust 都挡掉。
2. 运行这两个测试：

   ```bash
   pytest tests/distributed/test_runner.py::test_local_mode_setup \
          tests/distributed/test_runner.py::test_distributed_mode_setup -v
   ```

3. 再阅读 `test_worker_process_failure`：[tests/distributed/test_runner.py:L222-L242](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/distributed/test_runner.py#L222-L242)。它让 `create_worker_runner` 抛异常，断言 `_worker_process` 返回 `None`（即优雅退出而非抛错）。

**需要观察的现象**：

- `test_distributed_mode_setup` 断言 `mock_process.call_count == 2`，说明 `num_workers=2` 时确实创建了 2 个 `multiprocessing.Process`。
- `test_local_mode_setup` 断言 `len(distributed_runner._worker_processes) == 0`，说明 local 模式不建任何 worker 进程。

**预期结果**：三个测试全部通过。如果 `test_distributed_mode_setup` 因平台差异报错（mock 行为偶尔不稳定），可标注「待本地验证」。

> 说明：本实践是「读 + 跑现有测试」型，不修改源码。它让你在不开真实多进程的前提下，确认 setup 分流逻辑的正确性。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_setup_distributed` 里要把 `TOKENIZERS_PARALLELISM` 设成 `false`？如果忘了设会怎样？

> **答案**：transformers 的 Rust tokenizer 默认会用自己的多线程做并行分词，这与 Python `multiprocessing`（尤其 fork 启动方式）混用时会冲突，产生 `TOKENIZERS_PARALLELISM` 警告，极端情况下死锁。genai-bench 的 worker 进程内会跑 tokenizer（用于 token 数回退估算，见 u3-l2），所以必须先关掉。

**练习 2**：`num_workers=0` 时，`metrics_collector` 在哪里被创建？`num_workers=4` 时，master 和 4 个 worker 各有几个 collector？

> **答案**：`num_workers=0` 走 `_setup_local`，collector 在 [L213](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L213) 创建，共 1 个。`num_workers=4` 时只有 master 在 `_setup_master` 里创建 1 个 collector；4 个 worker 都 **不** 创建 collector（`_worker_process` 里没有创建它的代码）。

### 4.2 消息注册与处理

#### 4.2.1 概念说明

master 和 worker 是两个独立进程，不能直接调用对方的方法，只能靠 **「发消息」** 协作。genai-bench 复用了 Locust 提供的消息机制，核心是两个 API：

- `runner.register_message(name, handler)`：登记「当我收到名为 `name` 的消息时，用 `handler` 处理」。`handler` 的签名是 `handler(environment, msg, **kwargs)`，数据放在 `msg.data` 里。
- `runner.send_message(name, data)`：发出一条名为 `name`、载荷为 `data` 的消息。

整张消息表只有四类消息，方向分明：

| 消息名 | 方向 | 载荷 | 谁注册 handler |
| --- | --- | --- | --- |
| `update_scenario` | master → workers | 场景字符串，如 `"D(100,100)"` | worker（local 也注册） |
| `update_batch_size` | master → workers | 整数 batch size | worker（local 也注册） |
| `request_metrics` | worker → master | `RequestLevelMetrics` 的 JSON | master（local 也注册） |
| `worker_log` | （master 注册，但当前无发送方） | 日志字典 | 仅 master |

这里有个关键设计原则，源码注释里写得很明白：**「虽然把所有 handler 都在所有模式注册也是安全的（Locust 会负责路由），但我们刻意只在每种模式注册它需要的 handler，让消息流向一目了然。」** 同时，Locust 的消息在一个 greenlet 里串行处理，所以 handler 内部不需要加锁。

#### 4.2.2 核心流程

```
        ┌─────────────── master 进程 ───────────────┐
        │  注册: request_metrics  → _create_metrics_handler()
        │  注册: worker_log        → _create_log_handler()
        │                                            │
        │  发送: update_scenario(...)    ────┐       │
        │  发送: update_batch_size(...) ───┼───┐    │
        └────────────────────────────────────┼───┼────┘
                                             │   │   Locust
            ┌────────────────────────────────┘   │   master/worker
            ▼                                    ▼   协议 (5557)
        ┌─── worker 进程 ───┐              ┌─── worker 进程 ───┐
        │ 注册: update_scenario            │ 注册: update_scenario
        │       → _handle_scenario_update  │       → _handle_scenario_update
        │ 注册: update_batch_size          │ 注册: update_batch_size
        │       → _handle_batch_size_update│       → _handle_batch_size_update
        │                                  │
        │ 收到 update_scenario ⇒            │
        │   environment.scenario =          │
        │     Scenario.from_string(msg.data)│
        │                                  │
        │ 每条请求结束 (BaseUser.collect_metrics):
        │   发送 request_metrics(JSON) ────────────▶ 回到 master 的 handler
        └──────────────────────────────────┘
```

local 模式下没有进程间通信，同一个 runner 既是发送方又是接收方，所以三个 handler（`update_scenario` / `update_batch_size` / `request_metrics`）都注册在同一进程里，`send_message` 实际是「自己发给自己」。

#### 4.2.3 源码精读

注册的总开关是 `_register_message_handlers`：[genai_bench/distributed/runner.py:L365-L425](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L365-L425)。它先判 `runner` 是否存在，再按「是否分布式」+「自己是 master 还是 worker」分流注册：

```python
if self.config.num_workers > 0:
    if isinstance(self.environment.runner, WorkerRunner):
        self.environment.runner.register_message("update_scenario", self._handle_scenario_update)
        self.environment.runner.register_message("update_batch_size", self._handle_batch_size_update)
    if isinstance(self.environment.runner, MasterRunner):
        self.environment.runner.register_message("request_metrics", self._create_metrics_handler())
        self.environment.runner.register_message("worker_log", self._create_log_handler())
else:
    # local：三种都注册在自己身上
    self.environment.runner.register_message("update_scenario", self._handle_scenario_update)
    self.environment.runner.register_message("update_batch_size", self._handle_batch_size_update)
    self.environment.runner.register_message("request_metrics", self._create_metrics_handler())
```

注意两点：其一，master 和 worker 各自只注册「自己要收」的那几个；其二，local 模式 **不注册 `worker_log`**，因为单进程根本没有「别的 worker」要把日志转给自己。

发送方只有两个小方法，都做存在性判断后直接 `send_message`：[genai_bench/distributed/runner.py:L427-L435](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L427-L435)。

```python
def update_scenario(self, scenario_str: str) -> None:
    if self.environment.runner:
        self.environment.runner.send_message("update_scenario", scenario_str)

def update_batch_size(self, batch_size: int) -> None:
    if self.environment.runner:
        self.environment.runner.send_message("update_batch_size", batch_size)
```

这两个方法在主循环里被 master 调用（见 u8-l1，或 [genai_bench/cli/cli.py:L416](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L416) 与 [L435](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L435)）。Locust 的 `send_message` 在 master runner 上调用时，会 **广播给所有已连接的 worker**。

worker 侧的两个接收 handler 简短直接。场景更新会把字符串解析成 `Scenario` 对象挂到 `environment`，并 **重置 prefix 缓存**（呼应 u2-l4：切换场景后共享前缀必须重新生成）：[genai_bench/distributed/runner.py:L329-L338](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L329-L338)。

```python
def _handle_scenario_update(self, environment, msg) -> None:
    if not msg:
        raise RuntimeError("Received empty scenario message")
    environment.scenario = Scenario.from_string(msg.data)
    if hasattr(environment, "sampler") and hasattr(environment.sampler, "reset_prefix_cache"):
        environment.sampler.reset_prefix_cache()
```

batch size 更新直接改 `sampler.batch_size`：[genai_bench/distributed/runner.py:L437-L442](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L437-L442)。

最有趣的是「指标 handler」用 **工厂方法** 返回一个闭包，而不是直接传方法引用：[genai_bench/distributed/runner.py:L277-L307](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L277-L307)。它做了三件事：反序列化 JSON 并校验、喂给 collector、非阻塞地刷新仪表盘。

```python
def _create_metrics_handler(self) -> MessageHandler:
    def handler(environment, msg, **kwargs) -> None:
        try:
            metrics = RequestLevelMetrics.model_validate_json(msg.data)
        except ValidationError as e:
            logger.warning(f"Dropping invalid metrics record due to validation error: {e}")
            return
        if not self.metrics_collector:
            return
        self.metrics_collector.add_single_request_metrics(metrics)
        if self.dashboard and environment.runner and environment.runner.stats:
            live_metrics = self.metrics_collector.get_live_metrics()
            total_requests = environment.runner.stats.total.num_requests
            error_code = metrics.error_code
            gevent.spawn(self.dashboard.handle_single_request,
                         live_metrics, total_requests, error_code)
    return handler
```

读这段要抓三个要点：

1. **容错**：JSON 反序列化用 Pydantic 校验，万一某条指标损坏（比如字段类型不对），只 `warning` 后丢弃这一条，绝不让 master 崩溃——否则一条坏数据会害死整轮实验。
2. **路由**：`add_single_request_metrics` 是 u4-l2 里 collector 的入口，逐条指标就这样进入聚合管线。
3. **非阻塞 UI**：刷新仪表盘用 `gevent.spawn` 丢到后台 greenlet，不阻塞消息处理主线，保证指标接收不被渲染拖慢。

至于 `request_metrics` 的发送方，不在本文件，而在 u3-l1 讲过的 `BaseUser.collect_metrics`：[genai_bench/user/base_user.py:L90-L92](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L90-L92)。每个虚拟用户每完成一条请求，就把这条请求的 `RequestLevelMetrics` 序列化发走。

```python
self.environment.runner.send_message(
    "request_metrics", request_metrics_collector.metrics.model_dump_json()
)
```

这就是「指标流」的完整闭环：worker 内的 User 发 `request_metrics` → master 的 `_create_metrics_handler` 收 → 喂给 `AggregatedMetricsCollector`。

#### 4.2.4 代码实践

**实践目标**：验证「三种模式各自注册了正确数量的 handler」，并亲手触发一次 `ValidationError` 容错路径。

**操作步骤**：

1. 阅读 `test_message_handlers_registration`（local 模式应注册 3 个）与 `test_message_handlers_in_distributed_mode`（master、worker 各 2 个）：[tests/distributed/test_runner.py:L93-L124](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/distributed/test_runner.py#L93-L124)。
2. 运行它们：

   ```bash
   pytest tests/distributed/test_runner.py::test_message_handlers_registration \
          tests/distributed/test_runner.py::test_message_handlers_in_distributed_mode -v
   ```

3. 阅读 `test_metrics_handler_validation_error_handling`：[tests/distributed/test_runner.py:L338-L363](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/distributed/test_runner.py#L338-L363)。它喂给 handler 一段 `tpot=null` 但 `error_code=null` 的非法 JSON（成功请求却不带 tpot，违反 `RequestLevelMetrics` 的校验），断言 handler **只 warning 不抛异常**。

   ```bash
   pytest tests/distributed/test_runner.py::test_metrics_handler_validation_error_handling -v
   ```

**需要观察的现象**：

- local 模式 `register_message` 被调用 3 次；分布式模式下，无论把 runner mock 成 `WorkerRunner` 还是 `MasterRunner`，都恰好调用 2 次。
- 喂非法 JSON 后，`mock_logger.warning.assert_called_once()` 通过，且 warning 文案含 `"Dropping invalid metrics record due to validation error"`。

**预期结果**：三个测试通过。这证明了「按角色注册」与「坏数据不致命」两个设计。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_create_metrics_handler` / `_create_log_handler` 要写成「工厂返回闭包」，而 `_handle_scenario_update` 直接是普通方法？

> **答案**：闭包可以 **捕获注册时的状态**（如 `self`、`self.dashboard`），并让每个 runner 实例拿到一个独立的可调用对象。普通方法 `_handle_scenario_update` 不需要额外捕获状态，直接传方法引用即可。这是一种灵活与简洁的取舍。

**练习 2**：master 调用 `runner.update_scenario("D(100,100)")` 后，这条消息最终被几个进程的 handler 处理？

> **答案**：被 **所有 worker 进程** 各处理一次（Locust 广播）。master 自己 **不** 处理它——master 没有注册 `update_scenario` 的 handler。所以 master 要在自己的 `environment` 上单独维护场景吗？不需要：master 不发请求，场景对 master 无意义；场景只在 worker 的 sampler 里才用到。

### 4.3 指标聚合、worker 日志与资源清理

#### 4.3.1 概念说明

本模块把三件「收尾与运维」性质的事合在一起讲，因为它们都发生在 master 侧、且都围绕「多进程协作的副作用」：

1. **指标聚合的入口**：4.2 讲了消息怎么到 `_create_metrics_handler`，这里补完它如何驱动实时仪表盘，以及 `add_single_request_metrics` 在 collector 里做了什么。
2. **worker 日志的跨进程回传**：worker 是独立进程，如果各自往终端打印，会和 master 的实时仪表盘（基于 `rich.Live`）互相打架、刷屏错乱。所以 worker 的日志被 **收进队列、回传 master 统一输出**。
3. **CPU 亲和与资源清理**：可选地把 worker 钉到指定 CPU 核；进程退出时按「先礼后兵」的顺序终止 worker。

关于 worker 日志，有一个值得仔细分辨的细节：代码里同时存在 **两条** 日志通道。

- **实际生效的通道**：worker 进程里的 `WorkerRichHandler` 把每条日志塞进一个 `multiprocessing.Queue`（`worker_log_queue`），master 用一个后台 greenlet `_consume_worker_logs` 轮询这个队列，加上 `[Worker {id}]` 前缀后用 master 的 logger 打印。
- **已注册但当前无发送方的通道**：master 还注册了一个名为 `worker_log` 的 Locust 消息 handler（`_create_log_handler`），但全仓库 **没有任何 `send_message("worker_log", ...)` 调用**（见 4.2.3 的 grep 结果）。也就是说，这条基于 Locust 消息的日志通道目前是「接好了线、却没人按开关」，真正的日志走的是前一条直接队列通道。读源码时要能区分这两者，避免误以为 worker 日志是通过 Locust 消息传的。

#### 4.3.2 核心流程

指标与日志在 master 侧的流动：

```
worker 进程                                    master 进程
─────────────                                  ──────────
User.collect_metrics                           
  └─ send_message("request_metrics", JSON) ──▶ _create_metrics_handler
                                                  ├─ model_validate_json (容错)
                                                  ├─ collector.add_single_request_metrics
                                                  │     ├─ filter_metrics
                                                  │     ├─ all_request_metrics.append
                                                  │     └─ 更新 _live_metrics_data
                                                  └─ gevent.spawn(dashboard.handle_single_request)

logger.info(...)                                
  └─ WorkerRichHandler.emit                     
       └─ worker_log_queue.put({id,msg,level}) ──▶ _consume_worker_logs (后台 greenlet)
                                                       └─ logger.log("[Worker N] msg")
```

资源清理（进程退出时）：

```
cleanup()（由 atexit 触发，或主循环结束后调用）
  ├─ log_consumer.kill()              停掉日志消费 greenlet
  ├─ environment.runner.quit()        通知 Locust 退出，runner = None
  └─ 遍历 _worker_processes：
        ├─ process.terminate()        礼貌请求退出
        ├─ join(timeout=10)           等 10 秒
        └─ 仍存活则 process.kill()     强杀
```

#### 4.3.3 源码精读

先补完 4.2 没细讲的指标入口。`_create_metrics_handler` 在调用 `add_single_request_metrics` 之后，会取一份「实时指标快照」`get_live_metrics()` 连同总请求数一起丢给仪表盘：[genai_bench/distributed/runner.py:L296-L305](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L296-L305)。而 `add_single_request_metrics` 本身（u4-l2）会做四件事：`filter_metrics` 剔除不可信 TPOT、append 到逐请求列表、按错误码或完成数记账、维护 `_live_metrics_data` 供仪表盘取用：[genai_bench/metrics/aggregated_metrics_collector.py:L41-L77](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/aggregated_metrics_collector.py#L41-L77)。

接着看 worker 日志。worker 侧在 `_worker_process` 开头就装好了 `WorkerLoggingManager`：[genai_bench/distributed/runner.py:L228-L230](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L228-L230)。它给 worker 配两个 handler：一个写本地文件 `genai_bench_worker_{id}.log`，一个就是往队列塞日志的 `WorkerRichHandler`：[genai_bench/logging.py:L232-L268](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L232-L268)。

`WorkerRichHandler.emit` 极简——把日志记录打包成字典 `put` 进队列，**不直接打印**：[genai_bench/logging.py:L97-L105](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L97-L105)。

```python
def emit(self, record: logging.LogRecord):
    self.log_queue.put({
        "worker_id": self.worker_id,
        "message": record.getMessage(),
        "level": record.levelname,
    })
```

master 侧用一个常驻 greenlet 轮询队列并打印：[genai_bench/distributed/runner.py:L188-L207](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L188-L207)。队列空时 `gevent.sleep(0.1)` 让出 CPU，避免空转；收到 `KeyboardInterrupt` 时把队列里剩余日志排干（`_drain_log_queue`）再退出。

```python
def _consume_worker_logs(self):
    try:
        while True:
            if self.worker_log_queue.empty():
                gevent.sleep(0.1)
                continue
            log_data = self.worker_log_queue.get_nowait()
            self._process_log_data(log_data)
    except KeyboardInterrupt:
        logger.info("Log consumer shutting down")
        self._drain_log_queue()
```

`_process_log_data` 对数据做容错（空字典、缺字段都不崩），再带上 `[Worker {id}]` 前缀按原日志级别打印：[genai_bench/distributed/runner.py:L173-L186](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L173-L186)。

CPU 亲和是实验性功能，默认 `pin_to_cores=False`。开启后 `_set_cpu_affinity` 用 `psutil` 把 worker 钉到某颗核：[genai_bench/distributed/runner.py:L251-L275](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L251-L275)。映射规则是「优先用 `cpu_affinity_map` 自定义表，否则 `worker_id % cpu_count` 轮询」；映射非法时回退轮询，设亲和失败只 `warning` 不中断。注意注释明确「**仅 Linux** 有效」。

```python
if self.config.cpu_affinity_map:
    target_cpu = self.config.cpu_affinity_map.get(worker_id)
    if target_cpu is None or target_cpu >= cpu_count:
        logger.warning(f"Invalid CPU mapping for worker {worker_id}")
        target_cpu = worker_id % cpu_count
else:
    target_cpu = worker_id % cpu_count
process.cpu_affinity([target_cpu])
```

最后是清理 `cleanup`，采用「先礼后兵」的优雅终止：[genai_bench/distributed/runner.py:L340-L363](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L340-L363)。它先 kill 日志 greenlet，再 `runner.quit()` 让 Locust 通知所有 worker 自行退出，最后逐个 `terminate` → `join(10)` → 必要时 `kill` 强杀。

```python
def cleanup(self) -> None:
    if hasattr(self, "log_consumer"):
        self.log_consumer.kill()
    if self.environment.runner:
        self.environment.runner.quit()
        self.environment.runner = None
    for i, process in enumerate(self._worker_processes):
        try:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
                if process.is_alive():
                    process.kill()
                    process.join()
        except Exception as e:
            logger.error(f"Error terminating worker {i}: {e}")
```

这个 `cleanup` 在 `_setup_distributed` 里通过 `atexit.register(self.cleanup)` 注册（[L148](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L148)），所以即使主流程异常退出，进程结束前也会尝试回收 worker，避免留下僵尸进程。

#### 4.3.4 代码实践

**实践目标**：验证 worker 日志的「队列回传 + 容错」机制，以及 cleanup 的「优雅终止」顺序。

**操作步骤**：

1. 阅读 `test_log_consumer_processing`：[tests/distributed/test_runner.py:L259-L274](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/distributed/test_runner.py#L259-L274)。它往 `worker_log_queue` 塞入正常日志、`None`、缺字段字典，再调 `_drain_log_queue`，验证三条都能被「吞掉」不报错。
2. 阅读 `test_cleanup`：[tests/distributed/test_runner.py:L167-L186](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/distributed/test_runner.py#L167-L186)。它构造两个 mock 进程放进 `_worker_processes`，调 `cleanup()`，断言两个进程都被 `terminate()` 过一次，且 `environment.runner is None`。
3. 运行：

   ```bash
   pytest tests/distributed/test_runner.py::test_log_consumer_processing \
          tests/distributed/test_runner.py::test_cleanup \
          tests/distributed/test_runner.py::test_cpu_affinity_mapping -v
   ```

**需要观察的现象**：

- `test_log_consumer_processing` 即使喂入 `None` 和 `{"invalid": "log"}` 也不抛异常——证明日志消费是健壮的。
- `test_cpu_affinity_mapping` 覆盖了三种分支：合法自定义映射、非法映射回退、设亲和失败只 warning。

**预期结果**：测试通过。CPU 亲和测试依赖 `psutil`，若环境未安装会跳过/报错，标注「待本地验证」。

> 想在真机上看效果，也可以直接读 worker 落盘的日志文件 `genai_bench_worker_0.log`（由 `WorkerLoggingManager` 生成），对比它和 master 终端输出 `[Worker 0] ...` 前缀的对应关系。

#### 4.3.5 小练习与答案

**练习 1**：worker 日志回传用的是 `multiprocessing.Queue` 直接传字典，而不是 Locust 的 `send_message`。请结合「为什么不在 worker 终端直接打印」说出这样设计的两个好处。

> **答案**：(1) master 用 `rich.Live` 跑实时仪表盘，多个 worker 直接往同一终端打印会破坏 Live 的刷新、造成画面错乱；统一回传 master 输出可保持单一渲染源。(2) `multiprocessing.Queue` 是进程间原生的高效通道，不经过 Locust 的消息序列化与 greenlet 串行处理，对高频日志更轻量；同时还能让每条 worker 日志在本地文件留一份（`genai_bench_worker_{id}.log`）。

**练习 2**：`cleanup` 里为什么要 `terminate` 之后 `join(timeout=10)`，再判断 `is_alive()` 决定是否 `kill`？直接 `kill` 不更省事吗？

> **答案**：`terminate` 发的是 SIGTERM，给 worker 一个「自行收尾」（flush 日志、断开连接、保存状态）的机会；`join(10)` 给它最多 10 秒。只有超时仍不退出（比如卡死）才用 `kill`（SIGKILL）强杀。直接 `kill` 会丢失未 flush 的日志和未完成的收尾，是「先礼后兵」的优雅退出策略。

## 5. 综合实践

**任务**：把本讲三个模块串起来，画出一张完整的「master / worker 消息与数据流图」，并用代码验证你的理解。

**步骤**：

1. **读注册表**：打开 [genai_bench/distributed/runner.py:L365-L425](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L365-L425)，在一张纸上分两列写出：
   - **master 注册** 的 handler：`request_metrics`（`_create_metrics_handler`）、`worker_log`（`_create_log_handler`）。
   - **worker 注册** 的 handler：`update_scenario`（`_handle_scenario_update`）、`update_batch_size`（`_handle_batch_size_update`）。
2. **补发送方**：在图上标出每个消息的发送方——`update_scenario` / `update_batch_size` 由 master 的 `update_scenario()` / `update_batch_size()` 发（[L427-L435](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L427-L435)），`request_metrics` 由 worker 内的 `BaseUser.collect_metrics` 发（[base_user.py:L90](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L90)）。
3. **追一条指标**：从「worker 内一条请求结束」开始，画出 `collect_metrics → send_message("request_metrics") → master 的 _create_metrics_handler → model_validate_json → add_single_request_metrics → _live_metrics_data → dashboard.handle_single_request` 这条完整链路。
4. **跑测试确认**：执行整个分布式测试文件，确认你对消息数量与容错行为的判断和测试断言一致：

   ```bash
   pytest tests/distributed/test_runner.py -v
   ```

**预期产出**：一张包含三类进程角色（master / worker / local）、四类消息方向、一条指标链路、一条日志通道的流程图；以及一份「全绿」的测试输出。若某些用例因平台/依赖问题未跑通，在图旁标注「待本地验证」即可。

## 6. 本讲小结

- **进程模型**：`DistributedRunner` 用「同一份代码、按 `environment.runner` 类型走分支」的模式实现 master / worker / local 三态；worker 由 `multiprocessing.Process` fork，worker 内 `create_worker_runner` + `greenlet.join()` 永久阻塞，master 才跑主实验循环。
- **setup 分流**：`num_workers > 0` 走分布式（先关 `TOKENIZERS_PARALLELISM`、注册 `atexit` 清理、建 worker、再建 master 并 `sleep` 等连接），否则走 local。**只有 master/local 持有 `AggregatedMetricsCollector`**，worker 不持有。
- **消息注册**：刻意「按角色只注册需要的 handler」——master 收 `request_metrics`/`worker_log`，worker 收 `update_scenario`/`update_batch_size`，local 收全部三个（不含 `worker_log`）。Locust 在单 greenlet 内串行处理消息，无需加锁。
- **指标闭环**：worker 的 `BaseUser.collect_metrics` 发 `request_metrics` → master 的 `_create_metrics_handler` 反序列化（Pydantic 校验、坏数据只丢弃不崩溃）→ `add_single_request_metrics` 进入聚合 → 非阻塞刷新仪表盘。
- **worker 日志**：真正生效的是 `WorkerRichHandler` → `worker_log_queue`（`multiprocessing.Queue`）→ master 的 `_consume_worker_logs` greenlet 统一打印，避免多 worker 直接打印破坏 `rich.Live` 仪表盘；`worker_log` 这条 Locust 消息通道已注册但当前无发送方。
- **CPU 亲和与清理**：`pin_to_cores`（仅 Linux、实验性）按自定义表或轮询把 worker 钉核；`cleanup` 经 `atexit` 触发，「先礼后兵」地 terminate→join(10)→kill 回收 worker。

## 7. 下一步学习建议

- **u7-l2 实时仪表盘 Dashboard**：本讲多次提到 `dashboard.handle_single_request` 和 `rich.Live`，下一讲会讲清 `RichLiveDashboard` 如何消费 `get_live_metrics()` 的快照、以及 `ENABLE_UI=false` 时切到 no-op 的 `MinimalDashboard`。
- **u7-l3 日志系统**：本讲只讲了 worker 日志的「传输」，下一讲会深入 `RollingRichPanelHandler` / `DelayedRichHandler`，解释「为什么在 Live 上下文里要延迟 flush 日志」。
- **u8-l1 benchmark 主流程编排（capstone）**：把本讲的 `DistributedRunner` 放回 `cli.py` 的双层 `for` 循环里，看清 setup → `update_scenario` → `update_batch_size` → `aggregate_metrics_data` → `save` → `clear` 的完整编排，这是把全手册知识点串起来的总览讲。

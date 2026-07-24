# benchmark 主流程编排（capstone）

## 1. 本讲目标

本讲是整个 genai-bench 学习手册的 **capstone（顶点讲义）**。前面十几讲我们分别拆解了 CLI、任务、场景、采样、User、指标、认证、存储、分析报告、分布式、UI、日志等子系统；本讲要把它们**重新缝合成一条完整的数据流**，回答一个贯穿全局的问题：

> 当你在终端敲下 `genai-bench benchmark ...` 回车后，这一整次实验到底是怎么从头跑到尾的？

学完本讲，你应当能够：

1. 把 `benchmark` 函数划分为若干清晰的**阶段**（认证 → 数据/采样 → 实验目录/元数据 → 环境与分布式 → 双层循环 → 报告 → 上传），并说出每个阶段调用哪些子系统。
2. 读懂 **scenario × iteration 双层循环**：外层遍历场景、内层遍历并发档位（或 batch size），每个 `[scenario, iteration]` 组合就是一次 **run**，并理解每次 run 的启动、计时、停止、聚合、保存、清空这一整套动作。
3. 看懂实验结束后的**收尾环节**：清理进程、flush 日志、从磁盘重新加载结果、生成 Excel 与绘图、可选上传到对象存储。

本讲引用两个核心文件：`genai_bench/cli/cli.py`（主流程编排）和 `genai_bench/cli/utils.py`（运行计时、实验路径、迭代参数三个工具函数）。

## 2. 前置知识

本讲高度依赖前面的讲义，下面只做最简回顾，不再展开细节：

- **u1-l4 CLI 入口**：`benchmark` 是挂在 `cli` group 下的子命令，由一堆「选项组装饰器」（`api_options` / `sampling_options` / …）注入海量参数；校验回调会把选中的 `user_class` / `user_task` 塞进 `ctx.obj`，再由 benchmark 函数体读取。
- **u2-l4 采样器**：`Sampler.create(task, ...)` 按 `<input>-to-<output>` 任务字符串实例化采样器，把场景与数据集揉成一条条 `UserRequest`。
- **u3-l1 User 基类**：`BaseUser` 继承 Locust 的 `HttpUser`，靠 `sample()` 取请求、`collect_metrics()` 上报指标；`environment.sampler` 与 `environment.scenario` 是 Locust 引擎与 genai-bench 业务上下文的桥梁。
- **u4-l2 聚合指标**：`AggregatedMetricsCollector` 把成百上千条 `RequestLevelMetrics` 压成一份运行级摘要 `AggregatedMetrics`，并负责 warmup/cooldown 过滤与落盘。
- **u5-l1 认证工厂**：`UnifiedAuthFactory.create_model_auth` / `create_storage_auth` 按 provider 字符串分发，分别产出模型认证与存储认证。
- **u6-l4 绘图报告**：`load_one_experiment` 从实验目录读结果，`create_workbook` 出 Excel，`plot_experiment_data_flexible` 出 PNG。
- **u7-l1 分布式**：`DistributedRunner` 按 `num_workers` 分流为 master / worker / local 三态；只有 master / local 持有 `AggregatedMetricsCollector` 并执行实验主循环。

一个贯穿本讲的关键词是 **run**：一次 run 由 `[scenario, concurrency]`（或 `[scenario, batch_size]`）唯一界定——固定一个场景、固定一个并发档位，跑满时间或请求数后停下，产出一份聚合结果。一次「实验（experiment）」= 所有 run 的笛卡尔积。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `genai_bench/cli/cli.py` | 定义 `cli` group 与 `benchmark` 子命令；`benchmark` 函数体（约 490 行）是本讲的绝对主角，包含认证、数据、采样、目录、环境、双层循环、报告、上传全部阶段。 |
| `genai_bench/cli/utils.py` | 三个工具函数：`manage_run_time`（单次 run 的计时与提前退出）、`get_experiment_path`（实验目录命名）、`get_run_params`（把 iteration 翻译成表头/batch_size/并发数）。 |

主流程里被调用的子系统入口（不在本讲展开，仅标注「在哪个阶段被调用」）：

| 子系统入口 | 被调用的阶段 |
| --- | --- |
| `UnifiedAuthFactory.create_model_auth` | 阶段 1：认证 |
| `Sampler.create` / `DataLoaderFactory.load_data_for_task` | 阶段 2：数据与采样 |
| `ExperimentMetadata` + `model_dump_json` | 阶段 3：实验元数据 |
| `Environment` / `DistributedConfig` / `DistributedRunner.setup` | 阶段 4：环境与分布式 |
| `environment.runner.start/stop` / `AggregatedMetricsCollector` | 阶段 5：双层循环 |
| `load_one_experiment` / `create_workbook` / `plot_experiment_data_flexible` | 阶段 6：报告 |
| `UnifiedAuthFactory.create_storage_auth` / `StorageFactory.create_storage` | 阶段 7：上传 |

## 4. 核心概念与源码讲解

### 4.1 阶段编排：benchmark 函数的七段式骨架

#### 4.1.1 概念说明

`benchmark` 是一个被 click 装饰的长函数，函数签名极其庞大（[genai_bench/cli/cli.py:62-154](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L62-L154)）——它把十组选项组注入的几十个参数全部摊开成关键字参数。但**函数体本身并不复杂**：它是一条线性的「流水线」，可以切成 7 个阶段。理解本讲的第一步，就是建立这张阶段地图，把后面所有细节挂到这张地图上。

7 个阶段如下：

| 阶段 | 名称 | 关键产物 |
| --- | --- | --- |
| 0 | 初始化 UI 与日志 | `dashboard`、`LoggingManager`、`logger` |
| 1 | 认证 | `auth_provider`（ModelAuthProvider） |
| 2 | 数据与采样 | `tokenizer`、`data`、`sampler`；并把认证/host/api_backend 注入 `user_class` |
| 3 | 实验目录与元数据 | `experiment_folder_path`、`experiment_metadata.json`（先写盘） |
| 4 | 环境与分布式 | `Environment`、`DistributedRunner`；worker 在此后提前 return |
| 5 | 双层循环（见 4.2） | 每个 run 一份 `<scenario>_<task>_<iteration_type>_<iter>_time_<s>.json` |
| 6 | 报告（见 4.3） | `_summary.xlsx` + 若干 PNG |
| 7 | 上传（见 4.3） | 实验文件夹整体上传到对象存储 |

这条流水线最值得注意的两个设计特征：

1. **磁盘即检查点**：阶段 3 在跑任何请求**之前**就把 `experiment_metadata.json` 写到盘上；阶段 6 生成报告时**不是**用内存里的对象，而是 `load_one_experiment` 从磁盘重新读回来。这意味着即使中途崩溃，已完成的 run 结果不会丢，报告也能对「残缺数据」尽量生成。
2. **同一份代码、按角色分流**：阶段 4 调完 `runner.setup()` 后，worker 进程会被一个 `if` 提前 `return` 拦截（详见 4.2.2），只有 master / local 才继续走阶段 5–7。这是 u7-l1 三态模型在主流程里的落地。

#### 4.1.2 核心流程

下面是这条流水线的伪代码（仅展示阶段衔接，省略参数细节）：

```
benchmark(ctx, ...几十个参数...):
    dashboard = create_dashboard(metrics_time_unit)          # 阶段 0
    logging_manager = LoggingManager(...)
    auth_provider = UnifiedAuthFactory.create_model_auth(...) # 阶段 1
    user_class.auth_provider/host/api_backend = ...
    tokenizer = validate_tokenizer(...)                      # 阶段 2
    data = DataLoaderFactory.load_data_for_task(...)
    sampler = Sampler.create(task, ..., data, ...)
    path = get_experiment_path(...)                          # 阶段 3
    ExperimentMetadata(...).model_dump_json → 写 experiment_metadata.json
    environment = Environment(user_classes=[user_class])     # 阶段 4
    environment.sampler = sampler
    runner = DistributedRunner(environment, config, dashboard)
    runner.setup()
    if worker: return                                        # worker 到此为止
    aggregated = runner.metrics_collector
    for scenario in scenarios:        ┐
        for iter in iteration_values: ┘ 阶段 5（详见 4.2）
    runner.cleanup()                                         # 阶段 6
    meta, run_data = load_one_experiment(path)  # 从磁盘重读
    create_workbook(...)            # Excel
    plot_experiment_data_flexible(...)  # PNG
    if not upload_results: return    # 阶段 7（详见 4.3）
    storage.upload_folder(path, bucket, prefix)
```

#### 4.1.3 源码精读

**阶段 0：UI 与日志初始化。** [genai_bench/cli/cli.py:159-177](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L159-L177) 先建仪表盘，再把日志管理器挂到 dashboard 的 layout/live 上（这样日志能渲染进 Logs 面板，而不是刷花实时 UI——原因见 u7-l3）。注意这里还把所有入参逐条 `logger.info` 打印出来，方便排查「我到底传了什么」。

**阶段 1：认证。** [genai_bench/cli/cli.py:179-264](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L179-L264) 分两步：先按 `api_backend` 把零散的 CLI 凭据收拢进 `auth_kwargs` 字典，再用一张别名表把后端名归一化，最后交给统一工厂：

```python
auth_backend_map = {
    "oci-cohere": "oci", "oci-cohere-v2": "oci", "cohere": "oci",
    "oci-genai": "oci", "oci-openai": "oci",
    "vllm": "openai", "sglang": "openai",
}
auth_backend = auth_backend_map.get(api_backend, api_backend)
auth_provider = UnifiedAuthFactory.create_model_auth(auth_backend, **auth_kwargs)
```

这就是 u5-l1 讲的「别名归一化 + 统一工厂」在主流程里的调用点。`vllm`/`sglang` 被映射成 `openai`，对应 u3-l3 里「它们复用 OpenAIUser」的结论。

**阶段 2：数据与采样。** [genai_bench/cli/cli.py:266-333](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L266-L333) 做了四件事：(1) 从 `ctx.params` 重建一份 `cmd_line` 字符串，原样记进元数据以便复现；(2) 从 `ctx.obj` 取出校验阶段存好的 `user_class` / `user_task`，并把 `auth_provider` / `host` / `api_backend` 当作类属性注入（这是 u3-l3 里「benchmark 把 auth/host/api_backend 注入 user 类」的落点）；(3) 加载 tokenizer、校验前缀选项、加载数据集、`Sampler.create` 出采样器；(4) 一个小但重要的默认值逻辑——如果用户没给场景但给了数据集，就自动切到 `dataset` 模式：

```python
if not traffic_scenario and (dataset_path or dataset_config):
    traffic_scenario = ["dataset"]
```

另外注意 [genai_bench/cli/cli.py:333](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L333) 的 `max_time_per_run *= 60`：CLI 上 `--max-time-per-run` 的单位是**分钟**（见 option_groups 帮助文本），这里统一换算成秒，供后续 `manage_run_time` 使用。

**阶段 3：实验目录与元数据。** [genai_bench/cli/cli.py:335-377](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L335-L377) 调 `get_experiment_path` 生成目录、构造 `ExperimentMetadata`、`write_text(model_dump_json(...))` 把元数据 JSON **先写盘**。这里 `auth_config=auth_provider.get_config()` 会把脱敏后的认证配置写进元数据（脱敏细节见 u5-l2）。

**阶段 4：环境与分布式。** [genai_bench/cli/cli.py:380-406](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L380-L406) 构造 Locust `Environment`，把选中的任务方法赋给 user 类、把 sampler 挂到 `environment.sampler`，然后 `DistributedRunner(environment, config, dashboard).setup()`。setup 完成后，取出 `runner.metrics_collector`（只有 master/local 才有，否则抛 `RuntimeError`）。

> **小贴士**：`environment.scenario` 不在这里设置，而是在双层循环里由 `runner.update_scenario(scenario_str)` 经 master→worker 消息注入（见 4.2.3）。这是 Locust 引擎与 genai-bench 上下文解耦的体现。

#### 4.1.4 代码实践

**实践目标**：建立「函数体 = 七个阶段」的肌肉记忆，验证你对阶段边界的判断。

**操作步骤**：

1. 打开 `genai_bench/cli/cli.py`，从第 158 行（`# Set up the dashboard`）读到第 406 行（`aggregated_metrics_collector = runner.metrics_collector`）。
2. 用一张三列表格记录：**阶段编号 / 起止行号 / 一句话职责**。例如阶段 1 你应能写出「179–264：按 api_backend 收拢凭据 → 别名归一化 → 统一工厂产出 auth_provider」。
3. 特别核对 `auth_backend_map`（250–260 行）：把每个 OCI 系后端和 `vllm`/`sglang` 的归一化目标填进下表。

  | `api_backend`（CLI 输入） | `auth_backend`（传给工厂） |
  | --- | --- |
  | `oci-cohere` | `oci` |
  | `oci-openai` | ？ |
  | `cohere` | ？ |
  | `vllm` | ？ |
  | `sglang` | ？ |
  | `openai` | ？（不在表里，走 `get` 默认值） |

**需要观察的现象**：你会发现 OCI 系后端全部收敛到 `oci`，而 `vllm`/`sglang` 收敛到 `openai`；`openai` 本身不在 `auth_backend_map` 里，靠 `.get(api_backend, api_backend)` 的默认值原样通过。

**预期结果**：填表后，`oci-cohere/oci-cohere-v2/cohere/oci-genai/oci-openai → oci`，`vllm/sglang → openai`，`openai → openai`。这与 u5-l1、u3-l3 的结论一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么阶段 3 要在跑任何请求之前就把 `experiment_metadata.json` 写到磁盘？如果在实验全部结束后才写，会失去什么好处？

> **参考答案**：这是「磁盘即检查点」的设计。提前写盘意味着：即使后续某个 run 崩溃或进程被杀，元数据已经落地，且阶段 6 的 `load_one_experiment` 能据此找到目录、对已完成的 run 生成报告。若拖到最后才写，一旦中途崩溃就什么都留不下。

**练习 2**：`max_time_per_run *= 60`（第 333 行）这一行如果被误删，会出现什么现象？

> **参考答案**：`--max-time-per-run` 单位是分钟，`manage_run_time` 内部按秒计时。删掉这行会让「10 分钟」被当成「10 秒」，每个 run 几乎立刻结束，请求量严重不足，聚合统计失去意义。

---

### 4.2 双层运行循环：scenario × iteration

#### 4.2.1 概念说明

阶段 5 是整条流水线的「心脏」。它是一个**双层 for 循环**：

- 外层遍历 `traffic_scenario`（场景列表，例如 `["D(100,100)", "D(100,1000)"]`）。
- 内层遍历 `iteration_values`——当 `iteration_type == "num_concurrency"` 时是并发档位列表（如 `[1,2,4,8,...]`），当 `iteration_type == "batch_size"` 时是 batch size 列表。

每一次「外层 × 内层」的组合就是一次 **run**。run 的总数在循环前就算好了：

```python
iteration_values = batch_size if iteration_type == "batch_size" else num_concurrency
total_runs = len(traffic_scenario) * len(iteration_values)
```

`total_runs` 既是总进度条的分母，也是你预估实验时长的依据（见 u1-l2 提到的「默认 5 场景 × 9 并发 = 45 次 run」）。

#### 4.2.2 核心流程

整个双层循环被包在 `with dashboard.live:` 上下文里（实时 UI 在此期间接管终端）。一次 run 的生命周期如下：

```
外层 for scenario_str in traffic_scenario:
    reset 散点图指标
    sanitized = sanitize_string(scenario_str)        # D(100,100) -> D100_100
    runner.update_scenario(scenario_str)             # master -> worker 广播场景
    scenario_metrics = {"data": {}, "<iteration_type>": []}

    内层 for iteration in iteration_values:
        reset 面板; 建 run 进度条
        header, batch_size, concurrency = get_run_params(iteration_type, iteration)
        runner.update_batch_size(batch_size)         # 广播 batch size 给 sampler
        collector.set_run_metadata(...)              # 记录本次 run 的 scenario/concurrency
        start_time = monotonic()
        dashboard.start_run(...)
        environment.runner.start(concurrency, spawn_rate)   # Locust 开始压测
        total_run_time = manage_run_time(...)        # 阻塞计时，到时或到请求数退出
        environment.runner.stop()                    # Locust 停止
        collector.aggregate_metrics_data(start, end, warmup, cooldown)  # 聚合
        dashboard.update_scatter_plot_panel(...)
        collector.save(<run_name>.json)              # 落盘本次 run
        scenario_metrics["data"][iteration] = {...}  # 留作场景内绘图
        collector.clear()                            # 清空，准备下一次 run
        update 总进度条
        sleep(1)                                     # 等服务端清理中断请求

    # 内层结束后：用 scenario_metrics 画「该场景」的单场景图
    plot_single_scenario_inference_speed_vs_throughput(...)
```

两个关键设计：

1. **广播而非共享内存**：`runner.update_scenario` / `update_batch_size` 都是 `send_message`，把配置发给所有 worker（见 u7-l1）。worker 收到后设置 `environment.scenario` 与 `sampler.batch_size`，从而让每个 worker 在同一时刻跑同一个场景、同一档位。
2. **worker 提前 return**：在进入循环之前有一道「关卡」——

```python
if num_workers > 0 and isinstance(environment.runner, WorkerRunner):
    return
```

worker 进程是 fork 出来的子进程，它们在 `DistributedRunner.setup` 内部进入 `_worker_process`、`greenlet.join()` 永久阻塞，只发请求、收响应、回传指标，**不执行**实验主循环。上面这道 `if` 是主流程里对「我只在 master / local 跑循环」这一意图的防御性表达。

#### 4.2.3 源码精读

**双层循环骨架**：[genai_bench/cli/cli.py:408-434](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L408-L434)。注意外层进入时先 `dashboard.reset_plot_metrics()`（散点图按场景累积，切场景要清空——见 u7-l2），并 `sanitize_string` 把场景串里的括号、逗号清洗成文件名安全字符。

**单次 run 的启动与计时**：[genai_bench/cli/cli.py:444-464](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L444-L464)。`spawn_rate` 未指定时回退为 `concurrency`（对 LLM 这类慢请求，官方建议用更小的 spawn_rate 防止 worker 过载，见 option_groups 帮助）。`environment.runner.start` 是 Locust 的 API，`concurrency` 即要拉起的虚拟用户数。

**计时器 `manage_run_time`**（在 utils.py）：[genai_bench/cli/utils.py:14-53](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/utils.py#L14-L53)。它是一个 `while` 循环，每秒 `gevent.sleep(1)` 并检查已完成的请求数：

```python
while total_run_time < max_time_per_run:
    gevent.sleep(1)
    total_run_time += 1
    total_completed_requests = environment.runner.stats.total.num_requests
    if total_completed_requests >= max_requests_per_run:
        break
return int(total_run_time)
```

也就是「**到时**（`max_time_per_run`）或**到量**（`max_requests_per_run`）任一满足即结束本次 run」。返回值 `total_run_time` 会被写进 run 文件名 `..._time_{total_run_time}s.json`。

**聚合 + 异常兜底**：[genai_bench/cli/cli.py:466-487](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L466-L487)。`aggregate_metrics_data` 把本轮所有 `RequestLevelMetrics` 压成 `AggregatedMetrics`（含 warmup/cooldown 过滤，见 u4-l2）。如果它抛 `ValueError`（通常是数据异常），代码会先把明细存成 `debug_for_run_<scenario>_<concurrency>.json` 再把异常重新抛出——又一处「磁盘即检查点」：出了问题，现场证据已落盘。

**落盘与清空**：[genai_bench/cli/cli.py:502-522](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L502-L522)。run 文件名格式为 `{sanitized_scenario}_{task}_{iteration_type}_{iteration}_time_{total_run_time}s.json`。保存后把 `aggregated_metrics` 留一份在 `scenario_metrics["data"]`（供单场景绘图用），然后 `collector.clear()` 重置一切，进入下一次 run。末尾 `gevent.sleep(1)` 是给被 `stop` 中断的请求留出服务端清理时间。

**单场景绘图**：[genai_bench/cli/cli.py:524-531](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L524-L531)。内层循环走完（即某场景的所有并发档位都跑完）后，用内存里的 `scenario_metrics` 画一张该场景的「推理速度 vs 吞吐」图——这是实验过程中的**中间产物**，与阶段 6 的最终报告互补。

**`get_run_params` 的三态翻译**（utils.py）：[genai_bench/cli/utils.py:111-118](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/utils.py#L111-L118)。

```python
def get_run_params(iteration_type, iteration_value):
    if iteration_type == "batch_size":
        return "Batch Size", iteration_value, 1   # batch 变，并发恒为 1
    return "Concurrency", 1, iteration_value       # 并发变，batch 恒为 1
```

它把内层迭代的语义翻译成「表头显示文案 + 真正下发到 sampler 的 batch_size + 交给 Locust 的并发用户数」。在 batch_size 模式下，Locust 始终只起 1 个虚拟用户，靠请求体里的 `batch_size` 撑吞吐——这正是 embeddings/rerank 这类批量任务的迭代方式（见 u2-l1）。

#### 4.2.4 代码实践

**实践目标**：在不真跑压测的前提下，凭源码**预测**一次实验会产出哪些文件、跑多少次 run。

**操作步骤**：

1. 假设你执行（仅作参数推演，不必真连服务）：

   ```
   --task text-to-text
   --traffic-scenario "D(100,100)" --traffic-scenario "D(100,1000)"
   --num-concurrency 1 --num-concurrency 2
   --max-time-per-run 1 --max-requests-per-run 10
   ```

2. 先算 `iteration_values` 与 `total_runs`（对照 410–411 行）。
3. 再推演每个 run 的输出文件名（对照 503–506 行的命名模板，注意 `sanitize_string` 会把 `D(100,100)` 变成 `D100_100`，参见 u1-l2）。
4. 最后判断：`aggregate_metrics_data` 在什么条件下会触发 `debug_for_run_*.json`？

**需要观察的现象 / 预期结果**：

- `iteration_type` 默认为 `num_concurrency`（非 embeddings/rerank 任务），`iteration_values = [1, 2]`。
- `total_runs = 2 场景 × 2 并发 = 4`。
- 产出 4 个 run JSON，例如 `D100_100_text-to-text_num_concurrency_1_time_<N>s.json`、`D100_100_..._2_...json`、`D100_1000_..._1_...json`、`D100_1000_..._2_...json`，外加 1 个 `experiment_metadata.json`。
- `time_<N>s` 中的 N 受 `manage_run_time` 控制：因为 `--max-requests-per-run 10` 很小，很可能先到量退出，N 会小于 `max_time_per_run*60`。
- `debug_for_run_*.json` 仅在 `aggregate_metrics_data` 抛 `ValueError` 时产生（例如有效请求过少导致统计异常）。无法确定实际是否触发时，标注「待本地验证」。

> 说明：以上是**源码阅读型推演**，并未真正执行命令。若你在本地连了真实/模拟服务，可实际运行后对照文件名。

#### 4.2.5 小练习与答案

**练习 1**：内层循环结束后才调用 `plot_single_scenario_inference_speed_vs_throughput`，而不是每个 run 都画。为什么？

> **参考答案**：单场景图需要横跨「该场景的所有并发档位」才有意义（横轴并发、纵轴速度/吞吐）。每个 run 只有一个档位的数据，画不出趋势，所以必须等内层（全部档位）跑完、把各档位的 `aggregated_metrics` 收集进 `scenario_metrics["data"]` 后再统一画。

**练习 2**：`collector.clear()` 如果漏掉，下一次 run 会出现什么问题？

> **参考答案**：`AggregatedMetricsCollector` 会带着上一轮的 `all_request_metrics` 与 `aggregated_metrics` 进入下一轮，导致指标被「串台」累加，后续 run 的统计全部失真。`clear()` 负责重置聚合对象、清空逐请求列表与实时 UI 数据（见 aggregated_metrics_collector.py 的 `clear`）。

**练习 3**：为什么 `manage_run_time` 要在循环里 `assert environment.runner is not None`？

> **参考答案**：计时器要读取 `environment.runner.stats.total.num_requests` 来判断是否到量。runner 为 None 说明环境没正确初始化（理论上前面 setup 已保证非空），断言是把「不该发生的状态」尽早暴露，避免后续 `None.stats` 抛出令人困惑的 `AttributeError`。

---

### 4.3 收尾报告与上传

#### 4.3.1 概念说明

双层循环跑完，进入阶段 6–7。这一段做四件事：(1) 清理分布式进程与日志；(2) **从磁盘重新加载**全部 run 结果（而不是用内存对象）；(3) 生成 Excel 与绘图报告；(4) 如果开了 `--upload-results`，把整个实验文件夹上传到对象存储。

最反直觉但最重要的一点：阶段 6 调 `load_one_experiment(experiment_folder_abs_path)` 把刚刚写下去的 JSON 又读回来。这印证了「磁盘是唯一可信源」——报告子系统只认磁盘上的产物，与压测过程完全解耦。这也是为什么 `genai-bench excel` / `genai-bench plot` 两个独立命令（u6-l2、u6-l4）能对**任意旧实验**重出报告而不必重跑。

#### 4.3.2 核心流程

```
# 阶段 6 收尾
runner.cleanup()                                  # 杀 worker、quit runner、kill 日志 greenlet
delayed_log_handler.flush_buffer()                # 把缓冲日志吐到终端
experiment_metadata, run_data = load_one_experiment(path)  # 从磁盘重读
create_workbook(meta, run_data, ..._summary.xlsx, percentile="mean", ...)
tts_config = 2x4_tts preset  if task == "text-to-speech" else None
plot_experiment_data_flexible([(meta, run_data)], group_key="traffic_scenario", ...)

# 阶段 7 上传
if not upload_results: return                     # 默认不上传，直接结束
storage_provider_final = storage_provider or "oci"  # 向后兼容默认 OCI
构造 storage_auth_kwargs（按 provider 分支）
storage_auth = UnifiedAuthFactory.create_storage_auth(provider, **kwargs)
storage = StorageFactory.create_storage(provider, storage_auth, **storage_kwargs)
storage.upload_folder(path, bucket, prefix=...)
```

#### 4.3.3 源码精读

**清理与 flush**：[genai_bench/cli/cli.py:536-542](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L536-L542)。`runner.cleanup()`（u7-l1）杀掉 worker 进程、`runner.quit()`、终止日志消费 greenlet；随后 `delayed_log_handler.flush_buffer()` 把 Live 期间缓冲的日志一次性吐到终端（延迟 flush 的原因见 u7-l3——`rich.Live` 全屏重绘会刷花直接打印的日志，必须先 `live.stop`）。

**从磁盘重读 + Excel**：[genai_bench/cli/cli.py:544-556](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L544-L556)。

```python
experiment_metadata, run_data = load_one_experiment(experiment_folder_abs_path)
create_workbook(
    experiment_metadata, run_data,
    os.path.join(..., f"{Path(experiment_folder_abs_path).name}_summary.xlsx"),
    percentile="mean", metrics_time_unit=metrics_time_unit, task=task,
)
```

Excel 文件名固定为 `<目录名>_summary.xlsx`，落在实验目录内；`percentile="mean"` 表示 Summary 表用均值挑「达标的最大并发」（详见 u6-l2）。

**绘图**：[genai_bench/cli/cli.py:557-570](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L557-L570)。这里有个任务特判：`text-to-speech` 任务用专门的 `2x4_tts` preset，其余任务 `tts_config=None`（由 `plot_experiment_data_flexible` 内部决定默认配置）。`group_key="traffic_scenario"` 对应 u6-l4 讲的「所有场景叠在一张图」分组方式。

**上传分支**：[genai_bench/cli/cli.py:576-665](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L576-L665)。

- 早退：`if not upload_results: return`（577–578）。注意 `--upload-results` 是 flag，且校验回调 `validate_object_storage_options` 已强制要求开它时必须同时给 `--storage-bucket`（见 validation.py）。
- 向后兼容默认：`storage_provider_final = storage_provider or "oci"`（582）——`--storage-provider` 的 click 默认值就是 `"oci"`，这里再兜一层。
- 与阶段 1 对称的「按 provider 收拢凭据」：[genai_bench/cli/cli.py:587-641](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L587-L641)，覆盖 oci/aws/azure/gcp/github 五种存储。
- 造存储实例：[genai_bench/cli/cli.py:643-650](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L643-L650)。OCI 额外需要一个 `namespace`（u5-l4 讲过 namespace 是 OCI 对象存储寻址必需项）。
- 真正上传：[genai_bench/cli/cli.py:657-660](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L657-L660) 调 `storage.upload_folder(path, bucket, prefix=...)`，把整个实验目录递归搬上云端（u5-l3）。

#### 4.3.4 代码实践

**实践目标**：验证「报告 = 纯读磁盘」，以及上传是**可选**的最后一环。

**操作步骤**：

1. 读 [genai_bench/cli/cli.py:544-578](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L544-L578)，确认阶段 6 之后、阶段 7 之前有一句 `if not upload_results: return`。
2. 回顾 u6-l2/u6-l4：`genai-bench excel` / `genai-bench plot` 子命令接收 `--experiment-folder`，内部也是调 `load_one_experiment` + `create_workbook` / `plot_experiment_data_flexible`。
3. 思考：既然主流程结尾已经自动生成了 Excel 和 PNG，为什么还要单独提供 `excel` / `plot` 命令？把你的理由写下来（提示：换 percentile、换 plot 配置、换时间单位、对旧实验重出）。
4. （可选，待本地验证）如果你有一个已存在的实验目录，直接运行：

   ```
   genai-bench excel --experiment-folder <旧实验目录> --excel-name re-run.xlsx
   ```

   观察它是否**完全不发起任何模型请求**，仅凭磁盘 JSON 就生成 Excel。

**需要观察的现象**：`excel`/`plot` 命令执行极快、不连任何模型服务端；这与主流程结尾的自动报告走的是**同一套 analysis 子系统**。

**预期结果**：你能说出「报告子系统与压测过程解耦，磁盘 JSON 是唯一契约」这一架构特征，并解释这正是 `excel`/`plot` 能独立复用的根因。若本地无可用的旧实验目录，步骤 4 标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：阶段 6 明明内存里还留着最后一次 run 的 collector 数据，为什么还要 `load_one_experiment` 从磁盘读？

> **参考答案**：两个原因。其一，内存里的 collector 在每次 run 后都 `clear()` 了，只保留最后一个 run 的痕迹，拿不到全部 run；其二，「磁盘即唯一可信源」让报告子系统与压测过程彻底解耦——同一份加载逻辑既服务于主流程结尾，也服务于独立的 `excel`/`plot` 命令，避免维护两套数据路径。

**练习 2**：`storage_provider_final = storage_provider or "oci"`，`--storage-provider` 的 click 默认值已经是 `"oci"`，这行 `or "oci"` 还有意义吗？

> **参考答案**：主要是**显式兜底/可读性**。即便将来有人把 click 默认值改成 None，或在其它入口直接调用这段逻辑传 None，这行也能保证向后兼容地回落到 OCI（项目早期的默认存储就是 OCI）。属于防御式编程。

**练习 3**：如果 `--upload-results` 开了但忘了给 `--storage-bucket`，会在哪一步报错？

> **参考答案**：不会拖到主流程结尾。`--upload-results` 的校验回调 `validate_object_storage_options`（validation.py）会在 CLI 解析阶段就抛 `click.UsageError`，提示必须提供 `--storage-bucket`。这是「fail fast」——把错误前置到参数校验，而不是跑完整轮实验才在上传时失败。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**贯穿性任务**（即本讲规格里要求的实践）。

### 任务一：为 benchmark 的每个阶段打注释标签

在**你自己的分支/副本**上（**不要修改源码**，建议复制 `cli.py` 到临时文件或在笔记里操作），为 `benchmark` 函数体的关键位置插入中文阶段标签注释，形如：

```python
# ===== 阶段 0：初始化 UI 与日志 =====
dashboard = create_dashboard(metrics_time_unit)
...
# ===== 阶段 1：认证 =====
auth_kwargs = {}
...
# ===== 阶段 2：数据与采样 =====
cmd_line_parts = [sys.argv[0]]
...
# ===== 阶段 3：实验目录与元数据 =====
experiment_folder_path = get_experiment_path(...)
...
# ===== 阶段 4：环境与分布式 =====
environment = Environment(user_classes=[user_class])
...
# ===== 阶段 5：双层运行循环 =====
iteration_values = ...
with dashboard.live:
    ...
# ===== 阶段 6：报告 =====
runner.cleanup()
...
# ===== 阶段 7：上传 =====
if not upload_results:
    return
```

完成后自查：每个标签的起止行号应与你在 4.1.4 实践里填的阶段表一致。

### 任务二：画一张完整实验的数据流时序图

用文本/ASCII 画一张时序图，描述一次「2 场景 × 2 并发」实验里，**master、worker、Locust 引擎、磁盘、analysis 子系统**之间的交互。下面是一个**参考骨架**，请在 `…` 处补全细节（消息名、产物名），并可扩展 worker 数量：

```
用户           cli.benchmark          DistributedRunner/Locust      worker(s)        磁盘            analysis
 |  benchmark(...) |                        |                         |              |                  |
 |---------------->| 0.建 dashboard/logger  |                         |              |                  |
 |                 | 1.create_model_auth    |                         |              |                  |
 |                 | 2.Sampler.create(...)  |                         |              |                  |
 |                 | 3.写 experiment_metadata.json -----------------------------> |                  |
 |                 | 4.runner.setup()------>| fork workers ----------->|             |                  |
 |                 |                        | (master 注册 request_metrics handler) |                |
 |                 | 5.for scenario:        |                         |              |                  |
 |                 |   update_scenario----->| send_message ---------->| 设 environment.scenario |
 |                 |   for iter:            |                         |              |                  |
 |                 |     runner.start(N) -->| 拉起 N 个虚拟用户 ----->| 持续发请求    |                  |
 |                 |                        |<------- request_metrics (每条) --------|                  |
 |                 |                        | collector.add_single + 刷 dashboard   |                  |
 |                 |     manage_run_time    | (到时/到量)              |              |                  |
 |                 |     runner.stop()----->| 停止                    |              |                  |
 |                 |     aggregate_metrics_data                       |              |                  |
 |                 |     save run_<...>.json -----------------------------> |        |                  |
 |                 |     collector.clear()  |                         |              |                  |
 |                 | 6.runner.cleanup()     |                         |              |                  |
 |                 |   load_one_experiment <-----------------------------------------| 读回全部 JSON   |
 |                 |   create_workbook / plot_experiment_data_flexible --------------------------->| _summary.xlsx/PNG|
 |                 | 7.(可选)upload_folder --------------------------------------------->| 上传整个目录    |
 |<----------------| 完成                   |                         |              |                  |
```

**验收标准**：

1. 图中至少出现 `update_scenario`、`request_metrics`、`runner.start/stop`、`save`、`load_one_experiment`、`upload_folder` 六个关键动作。
2. 能在图上指出「磁盘写」发生在哪两处（阶段 3 写元数据、阶段 5 每个 run 写 JSON），以及「磁盘读」发生在哪一处（阶段 6 `load_one_experiment`）。
3. 能解释为何 worker 不出现在阶段 6/7（提前 return）。

> 若你无法本地连真实服务，本任务可纯做「源码阅读 + 画图」完成；时序图对照本讲引用的行号区间即可自检。

## 6. 本讲小结

- `benchmark` 函数是一条线性流水线，可清晰切成 **0 初始化 → 1 认证 → 2 数据/采样 → 3 目录/元数据 → 4 环境/分布式 → 5 双层循环 → 6 报告 → 7 上传** 八段（后三段是本讲三个最小模块）。
- **磁盘即检查点**：元数据在跑请求前先写盘；每个 run 独立落盘；报告从磁盘重读——这让中途中崩溃也不丢已完成的 run，并使 `excel`/`plot` 子命令能对任意旧实验重出报告。
- 核心是 **scenario × iteration 双层循环**：每个 `[场景, 并发/batch]` 组合就是一次 run，`total_runs = 场景数 × 档位数`；每次 run 经 `start → manage_run_time（到时或到量）→ stop → aggregate → save → clear` 闭环。
- `manage_run_time` 是「到时或到量」的双条件计时器，返回的秒数直接进 run 文件名；`get_run_params` 把迭代语义翻译成表头/batch_size/并发数（batch 模式下并发恒为 1）。
- 认证用「别名归一化 + `UnifiedAuthFactory`」，上传用「按 provider 收拢凭据 + `StorageFactory` + `upload_folder`」，两者对称；上传是可选的最后一环，默认不开。
- worker 进程在阶段 4 之后被 `if WorkerRunner: return` 拦截，不执行主循环——这是 u7-l1 三态模型在主流程的落地。

## 7. 下一步学习建议

本讲作为 capstone 已经把全局串通。接下来建议从两个方向深化：

1. **横向——把每段做厚**：回到 u8-l2（CLI 选项分组与校验机制）理解阶段 0 之前 click 是怎么把几十个参数组织起来并做跨参数校验的；回到 u8-l3（扩展指南）学习如何新增一个后端 User / 任务 / 场景，让你的自定义后端能跑通本讲的整条流水线。
2. **纵向——工程化**：阅读 u8-l4（测试体系、CI 与发布），用 `pytest` 跑一遍测试套件、看 `.github/workflows/ci.yml` 如何守护这条主流程、并用 `Dockerfile` 在容器里复现一次完整实验，验证你对本讲数据流的理解在真实环境里成立。

如果你想把本讲学到的「阶段地图」用起来，最好的练习是：挑一个真实后端，按本讲时序图**预测**它会产出哪些文件、跑多少次 run，再实际运行对照——能对上，就说明你已经真正读懂了 genai-bench 的主流程。

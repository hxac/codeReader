# pipeline 主流程：从 main() 到 worker 协程

## 1. 本讲目标

学完本讲，你应该能够：

1. **按时间顺序复述 `main()` 的完整时序**：从解析命令行参数，到创建 WorkQueue、收集 `--pdfs`、下载模型、启动 vLLM、拉起 workers，最后输出总指标——每一步都能说出对应的函数名和源码行号。
2. **理解 `use_internal_server` 分支**：同一条 `olmocr` 命令，为什么既能本地起 GPU 推理，也能对接外部 API 服务器？这个布尔值在哪些节点上改变了流程走向？
3. **理清 asyncio 任务编排**：`main()` 在事件循环上同时运行了哪几类协程？它们之间没有显式的调用关系，靠什么协作（共享队列、全局信号量、取消机制）？

本讲是整个单元 2 的「地图页」：后续七讲（输入收集、渲染、提示词、重试、HTTP 客户端、输出落盘）都是把本讲流程图中的某一个方框放大来讲。

## 2. 前置知识

本讲是全书第一次深入 `asyncio` 代码，先把这些概念用一段话讲清楚。已熟悉 asyncio 的读者可以跳到 2.4。

### 2.1 协程与事件循环

**协程（coroutine）** 是用 `async def` 定义的函数。调用它不会立刻执行函数体，而是返回一个"协程对象"——一段还没开跑的代码。**事件循环（event loop）** 是一个单线程调度器：它维护一张"待办清单"，轮流让各个协程跑到下一个 `await` 点，然后挂起，把线程让给别人。

关键直觉：**`await` 是"可以让出线程"的地方**。当一个协程 `await` 网络请求时，事件循环就去跑别的协程。所以单线程也能同时挂着上千个网络请求——olmocr 处理百万级 PDF 页面正是靠这一点。

### 2.2 Task、gather 与 TaskGroup

- `asyncio.create_task(coro)`：把协程**提交**给事件循环后台运行，立即返回一个 Task 对象，外层代码继续往下走。
- `await asyncio.gather(*tasks)`：阻塞当前协程，直到所有 Task 全部完成。
- `async with asyncio.TaskGroup() as tg:`（Python 3.11+）：把一组子任务放进一个"组"，**任何一个子任务抛异常，整个组一起取消**——`worker()` 用它保证一个工作组的所有 PDF 同生同死。
- `asyncio.run(main())`：创建事件循环、跑完 `main()`、再关闭循环。这是同步世界进入异步世界的唯一入口。

### 2.3 信号量（Semaphore）

`asyncio.BoundedSemaphore(n)` 是一个计数闸门：内部有 n 个令牌，协程 `async with sem:` 时取走一个令牌，离开时归还；令牌取光时后来的协程排队等待。它是不改写业务逻辑就能"限流"的标准手段。`main()` 用它限制全局并发请求数（本讲 4.3 节）。

### 2.4 生产者-消费者模式

`main()`（生产者）先把待处理 PDF 填进 WorkQueue，再拉起 N 个 `worker()` 协程（消费者）从队列里领活干，干完领下一件，直到队列空。生产与消费解耦后，加机器就是"多跑几个消费者"，这正是第 3 单元多机部署的基础（承接 u1-l3 讲过的 workspace 断点续跑语义）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [olmocr/pipeline.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py) | 总装配厂兼 CLI 入口（1528 行） | `main()`、`worker()`、`vllm_server_host/ready/task`、`download_model`、`metrics_reporter`、`cli_main`，以及模块级全局变量 |
| [olmocr/work_queue.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py) | 可断点续跑的工作队列 | 本讲只用到 `WorkQueue.populate_queue / initialize_queue / get_work / mark_done` 四个方法的语义，细节留给 u3-l1 |
| [olmocr/metrics.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/metrics.py) | 指标收集 | 模块级全局 `metrics`、`tracker` 的来历，细节在 u4-l4 |
| [olmocr/check.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/check.py) | 环境自检 | `check_poppler_version`、`check_torch_gpu_available` 两个被 `main()` 调用的检查（u1-l2 已讲过脚本本身） |

另外会零星提到 `olmocr/s3_utils.py` 的 `expand_s3_glob` / `get_s3_bytes`（u4-l3 详讲）和 `pypdf.PdfReader`（用于估算页数）。

## 4. 核心概念与源码讲解

### 4.1 main() 主流程：一条从 CLI 到结果落盘的装配线

#### 4.1.1 概念说明

`pipeline.py` 里有几十个函数，但真正的"总指挥"只有一个：`main()`。它不做任何具体的 OCR 工作——不渲染页面、不发 HTTP 请求、不解析模型输出。它只做三件事：

1. **搭台**：解析参数、准备环境（S3 客户端、模型权重、推理服务器）；
2. **派活**：把 PDF 填进 WorkQueue，启动 N 个 worker 协程去消费队列；
3. **收场**：等所有 worker 干完，关掉服务器和报表协程，打印总结。

这就是 u1-l4 里说的"总装配厂"含义：具体零件（渲染、提示词、重试、落盘）都在别的函数里，`main()` 负责按正确顺序把它们装到一起。理解了这一点，读 330 行的 `main()` 就不是读代码，而是读一张 checklist。

#### 4.1.2 核心流程

把 `main()` 从头到尾走一遍，可以切成 **10 个检查点（checkpoint）**：

```text
cli_main()                                          [L1526]
  └─ asyncio.run(main())                            [L1528]
       main()                                       [L1199]
        │
        ├─ ① 解析参数  parse_known_args()           [L1272]  得到 args + unknown_args
        │     └─ 可选：--disk_logging 开文件日志     [L1275]
        │     └─ 计算 use_internal_server            [L1286]
        │     └─ 建全局并发闸门 BoundedSemaphore     [L1289]
        │
        ├─ ② Beaker 集群环境适配（仅 BEAKER_JOB_NAME 存在时）
        │     写凭证 + 错峰 sleep                    [L1292-L1310]
        │
        ├─ ③ 创建 WorkQueue（S3 或 Local 后端）      [L1329-L1332]
        │
        ├─ ④ 收集 --pdfs 并填充队列 populate_queue  [L1334-L1412]
        │     ├─ s3 glob / 本地文件 / txt 列表 / tarball 分类
        │     ├─ 抽样 100 个 PDF 估算平均页数 → items_per_group
        │     ├─ ★ --stats 短路：print_stats() → return   [L1414-L1416]
        │     └─ ★ --beaker 短路：submit_beaker_job() → return [L1418-L1420]
        │
        ├─ ⑤ GPU 检查 + 模型下载 download_model()   [L1424-L1437]
        │     └─ use_internal_server=True 时改写 args.server / args.model
        │
        ├─ ⑥ initialize_queue()：读回队列并洗牌      [L1440-L1444]
        │     └─ 队列空 → 直接退出
        │
        ├─ ⑦ 启动 vLLM：create_task(vllm_server_host)
        │     + await vllm_server_ready()            [L1447-L1451]
        │
        ├─ ⑧ 启动报表协程 metrics_reporter
        │     + N 个 worker 协程
        │     + await asyncio.gather(*worker_tasks)  [L1453-L1462]
        │
        ├─ ⑨ 收尾：cancel vLLM 与报表，再 gather     [L1465-L1473]
        │
        └─ ⑩ 打印 FINAL METRICS SUMMARY             [L1476-L1523]
```

两个短路分支（④里的 ★）值得先记住：`--stats` 和 `--beaker` 都不会做任何推理，它们在队列填充完成**之后**提前 `return`。这就是 u1-l3 说"轻量环境也能建立并观察队列"的原因——①到④不需要 GPU。

#### 4.1.3 源码精读

下面按检查点顺序引用真实代码。所有行号基于当前 HEAD。

**入口：从同步世界到异步世界**

[olmocr/pipeline.py:1526-1528](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1526-L1528) 定义了同步入口 `cli_main()`——pyproject 里注册的 `olmocr` 命令指向它（u1-l3 已讲）。它只有一行实事：用 `asyncio.run` 把协程 `main()` 跑在全新的事件循环上。命令行是同步的，流水线是异步的，这一行就是两种世界的桥。

**① 解析参数（L1199-L1272）**

[olmocr/pipeline.py:1199-1272](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1199-L1272) 是完整的 argparse 定义：位置参数 `workspace`、`--pdfs`、`--model` 在最前面，随后是通用选项，再按 Server / VLLM / Beaker 分成三个参数组。这些参数的含义在 u1-l5 已经逐个讲过，这里只看收尾一行：

[olmocr/pipeline.py:1272](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1272) 用的是 `parse_known_args()` 而不是 `parse_args()`——不认识的参数不报错，而是收进 `unknown_args` 返回。这些"漏网之鱼"稍后在 ⑦ 被原样转发给 `vllm serve` 命令行（见 [olmocr/pipeline.py:833-834](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L833-L834)），这就是"任何 vLLM 参数都能透传"的实现基础。

紧接着的两小段：

- [olmocr/pipeline.py:1275-1280](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1275-L1280)：若指定 `--disk_logging`，给两个 logger 追加一个 DEBUG 级文件 handler——注意这是在解析参数之后第一件事，保证后续所有日志都能落盘。
- [olmocr/pipeline.py:1286-1289](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1286-L1289)：`use_internal_server = not args.server`（没给外部服务器地址就要自己起服务器）；`global` 声明后把模块级变量 `max_concurrent_requests_limit` 初始化为容量 `args.max_concurrent_requests`（默认 1600）的信号量。模块级定义在 [olmocr/pipeline.py:88-89](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L88-L89)，默认只是占位的 `BoundedSemaphore(1)`，真实容量由这里按参数设置。

**② Beaker 环境适配（L1292-L1310）**

[olmocr/pipeline.py:1292-1310](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1292-L1310)：当进程运行在 Beaker 集群里（环境变量 `BEAKER_JOB_NAME` 存在），把 AWS/GCS 凭证从环境变量写到本地文件，然后根据 `BEAKER_REPLICA_RANK` 计算一个错峰等待时间并 `await asyncio.sleep(sleep_time)`——目的是避免同一任务的多个 GPU 副本同时下载模型压垮网络。注意普通本地运行完全跳过这一段；它也是 `main()` 里第一个 `await` 点。

**③ 创建 WorkQueue（L1326-L1332）**

[olmocr/pipeline.py:1326-1332](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1326-L1332)：先 `check_poppler_version()` 确认 poppler 可用（渲染和输入收集都离不开它，u1-l2 讲过），然后按 workspace 是否以 `s3://` 开头，选择 `WorkQueue(S3Backend(...))` 或 `WorkQueue(LocalBackend(...))`。同一个 `WorkQueue` 类套不同后端，本地实验与多机协同共用一套代码——接口细节在 u3-l1/u3-l2 展开。

**④ 收集 --pdfs 并填充队列（L1334-L1412）**

这是 `main()` 里最长的一段，输入形态的分支细节留给 u2-l2，本讲只看骨架：

- [olmocr/pipeline.py:1339-1373](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1339-L1373)：遍历 `args.pdfs`，把每个输入分类进 `pdf_work_paths`（普通 PDF/图片）或 `tarball_paths`（tar 包）。s3 路径先用 `expand_s3_glob` 展开；本地 `.pdf/.png/.jpg/.jpeg` 文件还会读前 4 个字节判断是不是真 PDF（`b"%PDF"` 魔数）。
- [olmocr/pipeline.py:1378-1405](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1378-L1405)：随机抽最多 100 个 PDF，用 `PdfReader` 数页数，算出平均页数 `avg_pages_per_pdf`，再得到分组大小：

\[ \text{items\_per\_group} = \max\left(1,\ \left\lfloor \frac{\text{pages\_per\_group}}{\text{avg\_pages\_per\_pdf}} \right\rfloor\right) \]

直觉：`pages_per_group` 是"每个工作组目标页数"（默认 500，走外部 API 时降为 50，见 [olmocr/pipeline.py:1314-1315](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1314-L1315) 的条件默认值技巧，u1-l5 讲过），除以平均页数就得到"每组装几个文件"。采样失败时兜底 10 页（L1401-L1402）。
- [olmocr/pipeline.py:1408](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1408) 与 [olmocr/pipeline.py:1411-1412](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1411-L1412)：普通 PDF 按 `items_per_group` 分组入队；tarball 一包一组（分组大小 1）。

**④★ 两个短路分支（L1414-L1420）**

[olmocr/pipeline.py:1414-1420](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1414-L1420)：`--stats` 调 `print_stats()` 后 `return`，只打印 workspace 统计；`--beaker` 调 `submit_beaker_job(args)` 后 `return`，把任务提交到集群（u3-l4 详讲）。注意它们排在队列填充**之后**：`print_stats` 统计的正是刚填进去的内容；`submit_beaker_job` 提交的集群任务会在远端重跑同一条命令，远端那份运行到 ④ 时队里已经有数据了。

**⑤ GPU 检查与模型下载（L1422-L1437）**

走过短路分支，注释写得很直白："If you get this far, then you are doing inference and need a GPU"。

[olmocr/pipeline.py:1424-1425](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1424-L1425)：只在要起内部服务器时检查 torch 和 GPU（用外部服务器则本机不需要 GPU）。

[olmocr/pipeline.py:1430-1437](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1430-L1437) 是 `use_internal_server` 分支的核心，两路差异如下：

| | 内部服务器（本地 GPU） | 外部服务器（`--server` 已给） |
| --- | --- | --- |
| `download_model` | 调用，下载权重到本地 | 不调用，`model_name_or_path = None` |
| `args.server` | 改写为 `http://localhost:{port}/v1` | 保持用户给的 URL |
| `args.model` | 改写为 `"olmocr"`（内部服务器的统一模型名） | 保持用户给的模型名 |

`download_model` 本体在 [olmocr/pipeline.py:956-980](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L956-L980)：按前缀分三种情况——`s3://`/`gs://`/`weka://` 用 `download_directory` 拉到 `~/.cache/olmocr/model`；本地绝对路径直接用；否则当 HuggingFace repo 用 `snapshot_download`。失败按指数退避重试最多 5 次（退避细节见 4.1.5 练习 3）。

**⑥ 初始化队列（L1440-L1444）**

[olmocr/pipeline.py:1440-1444](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1440-L1444)：`initialize_queue()` 从后端读回工作索引、剔除已完成（有 done flag）的组、随机洗牌后构建内存队列，返回剩余数量。队列为 0 说明活全干完了，直接退出——这就是"重跑一条命令即断点续跑"的机关。

**⑦ 启动并等待 vLLM（L1447-L1451）**

[olmocr/pipeline.py:1447-1451](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1447-L1451)：内部服务器模式下 `asyncio.create_task(vllm_server_host(...))` 把服务器作为后台任务提交（不 `await`，main 继续往下走），随后 `await vllm_server_ready(args)` 轮询 `{server}/models` 直到返回 200（[olmocr/pipeline.py:929-953](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L929-L953)，最多等 `--max_server_ready_timeout` 秒）。`vllm_server_host`（[L911-926](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L911-L926)）是个最多 5 轮的重启外壳：服务器进程一旦退出就重新拉起 `vllm_server_task`，5 次仍失败则 `sys.exit(1)`。命令拼装与日志解析在 u3-l3 精读。

**⑧ 拉起 workers 并等待（L1453-L1462）**

[olmocr/pipeline.py:1453-1462](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1453-L1462)：先 `create_task(metrics_reporter(work_queue))`（每 10 秒打印队列余量、速率表和 worker 状态表的后台协程，[L983-989](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L983-L989)），再用一个 for 循环 `create_task(worker(args, work_queue, worker_id=i))` 创建 `args.workers` 个（默认 20）worker 协程，最后 `await asyncio.gather(*worker_tasks)` 等它们全部跑完。**从这一行开始，main() 只是旁观者**，真正干活的是 20 个 worker（见 4.3）。

**⑨⑩ 收尾与总结（L1465-L1523）**

[olmocr/pipeline.py:1465-1473](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1465-L1473)：对 vLLM 服务器任务和报表任务调用 `.cancel()`，然后 `await asyncio.gather(*tasks_to_wait, return_exceptions=True)` 等"取消"这件事真正生效（`return_exceptions=True` 让 `CancelledError` 不炸出来）。

[olmocr/pipeline.py:1476-1523](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1476-L1523)：从模块级 `metrics`（一个 `MetricsKeeper`，定义在 [olmocr/pipeline.py:78-79](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L78-L79)）取汇总，打印总耗时、输入/输出 token 数、成功/失败页数与失败率，以及"按尝试次数完成"（`finished_on_attempt_N`）分布——最后一项直接反映 u2-l6 重试阶梯的有效性，最后以 `Work done` 收尾。

#### 4.1.4 代码实践

**实践 A：给 10 个检查点标函数名（源码阅读型，10 分钟）**

1. **实践目标**：不看本讲 4.1.2 的流程图，独立把 `main()` 的执行顺序梳理成一张表。
2. **操作步骤**：
   - 打开 [olmocr/pipeline.py:1199](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1199)，从上往下通读 `main()`（到 L1523 为止）；
   - 每遇到一个函数调用，记下三列：**行号 | 函数名 | 它在这一步做了什么**；
   - 特别标出两个 `return` 提前退出的位置。
3. **观察现象**：你会发现 `main()` 的直接调用其实非常少——`check_poppler_version`、`populate_queue`、`print_stats`、`submit_beaker_job`、`check_torch_gpu_available`、`download_model`、`initialize_queue`、`vllm_server_host`、`vllm_server_ready`、`metrics_reporter`、`worker`，加上收尾的 `metrics.get_metrics_summary`。
4. **预期结果**：得到一张与本讲 4.1.2 检查点一一对应的表；把它保存下来，后面几讲会把其中 `populate_queue`（u2-l2）、`worker`（u2-l6/u2-l8）、`vllm_server_host`（u3-l3）逐个放大。

**实践 B：亲眼看到短路分支（可本地运行）**

1. **实践目标**：验证 `--stats` 短路发生在队列填充之后、GPU 检查之前。
2. **操作步骤**（轻量安装即可，无 GPU 要求）：

   ```bash
   mkdir -p /tmp/olmocr-u2l1
   olmocr /tmp/olmocr-u2l1 --pdfs tests/gnarly_pdfs/edgar.pdf --stats
   ```

   注意：`edgar.pdf` 是仓库自带的测试 PDF；若你的版本中没有它，先 `ls tests/gnarly_pdfs/` 挑一个真实存在的 `.pdf` 替换（如 `ambiguous.pdf`）。
3. **观察现象**：日志应依次出现 `Got --pdfs argument...`、`Found 1 regular pdf paths...`、`Calculated items_per_group...`，然后直接输出 workspace 统计并退出——**没有** `Starting pipeline with PID`（那在 L1427，短路点之后）。
4. **预期结果**：命令正常退出，`/tmp/olmocr-u2l1` 下生成 `work_index_list.csv.zstd`。若你环境里没有 `olmocr` 命令，可用 `python -m olmocr.pipeline` 触发同一入口。实际输出以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--stats` 和 `--beaker` 的短路分支写在 `--pdfs` 收集之后，而不是 `main()` 开头？

**答案**：`print_stats` 汇报的是 workspace 队列的内容，必须先把新 PDF 填进去统计才准确；`submit_beaker_job` 提交的集群副本会重新执行同一条命令，若本地不先填队列，远端运行时队列可能是空的（或者远端要自己再收集一遍）。放在填充之后，两种短路都能看到最新队列状态。

**练习 2**：`main()` 在哪里改写了用户传入的 `args.server` 和 `args.model`？为什么要改？

**答案**：[olmocr/pipeline.py:1430-1434](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1430-L1434)。起内部服务器时，`args.server` 被指向 `http://localhost:{args.port}/v1`，`args.model` 被改成 `"olmocr"`——因为 `vllm serve` 用 `--served-model-name olmocr` 注册了固定模型名（[L817-818](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L817-L818)），这样即便模型来自很长的本地缓存路径，下游所有请求代码只认 `"olmocr"` 一个名字。

**练习 3**（细节彩蛋）：`download_model` 里 [L978](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L978) 计算的 `sleep_time` 和 [L980](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L980) 实际 `sleep` 的时间是同一个值吗？

**答案**：不是。L978 用 `random.randrange(2, 20) * 2**retry` 算出的值只出现在日志消息里；实际睡眠用的是重新采样的 `random.randrange(10, 30) * 2**retry`。两者共享"随重试次数指数增长"（\( \text{delay} \propto 2^{r} \)）的意图，日志数字与真实等待有偏差，属于无害的小瑕疵——顺带提醒我们：读源码时日志里的数字未必等于实际行为，以 `await asyncio.sleep(...)` 的实参为准。

### 4.2 asyncio 任务编排：一台事件循环上的四类协程

> 说明：本讲义把大纲中的「use_internal_server 分支」并入 4.1（它本质是 ①⑤⑦ 三个检查点上的条件），把篇幅留给更核心的任务编排；分支本身已在上表与 4.1.3 ⑤ 中完整覆盖。

#### 4.2.1 概念说明

第 ⑧ 步之后，事件循环上同时跑着的协程可以分成四类，职责互不重叠：

| 协程 | 数量 | 生命周期 | 一句话职责 |
| --- | --- | --- | --- |
| `worker` | `args.workers`（默认 20） | 队列空即退出 | 从队列领工作组，处理并落盘结果 |
| `vllm_server_host` | 0 或 1 | 直到被 cancel（内部最多重启 5 次） | 看护 vLLM 子进程，崩了就拉起 |
| `metrics_reporter` | 1 | 直到被 cancel | 每 10 秒打印队列/速率/worker 状态 |
| `main` 本身 | 1 | 全程 | 装配 + `await gather(workers)` + 收尾 |

它们之间**几乎没有直接调用**——worker 从不调用 metrics_reporter，main 也不调用 worker 的内部函数。协作靠三样东西：

1. **共享的 WorkQueue**（worker 消费、main 生产）；
2. **模块级全局变量**：`metrics` / `tracker`（谁都能写入指标）、`vllm_queued_requests`（服务器日志解析出的排队深度，worker 重试决策会读它）、`max_concurrent_requests_limit`（全局并发闸门）；
3. **取消（cancel）**：main 在 gather 结束后统一 cancel 长驻协程。

这是 asyncio 生态里非常典型的"编排层"写法：**用 `create_task` 启动长驻协程，用共享状态通信，用 cancel 收尾**。

#### 4.2.2 核心流程

worker 一侧的运行时结构如下（内部每层的细节属于 u2-l6/u2-l8，这里只看编排骨架）：

```text
worker(args, work_queue, worker_id)         [L698]
  while True:
    work_item = await work_queue.get_work()  # 从队列领一个组
    ├─ None → 队列空，break 退出             [L703-L705]
    └─ 拿到组 →
         async with asyncio.TaskGroup():     [L711]
             对组内每个路径 create_task:
               tarball → process_tarball     [L716]
               普通   → process_pdf          [L718]
                       └─ 每页并发 → process_page → try_single_page（u2-l6）
         汇总 dolma_docs                     [L723-L739]
         写 results/output_{hash}.jsonl      [L742-L759]
         --markdown 时写 markdown/           [L766-L794]
         记录 finished token 指标            [L797-L800]
         await work_queue.mark_done(item)    [L802]
    （任何异常被 catch 并记日志，worker 不死，领下一件 [L803-L804]）
```

并发层级一览：**20 个 worker × 每组若干 PDF × 每 PDF 若干页**，同时还有全局信号量 `max_concurrent_requests_limit`（1600）在 HTTP 层兜底限流。真正的并发上限由最紧的那层决定。

#### 4.2.3 源码精读

**worker 主循环与退出条件**

[olmocr/pipeline.py:698-708](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L698-L708)：`get_work()` 返回 `None` 表示队列被取空，worker `break` 退出——这就是 `await asyncio.gather(*worker_tasks)` 最终能返回的前提：**所有 worker 都把队列喝干才会结束主流程**。注意 `get_work` 还会顺手拿 worker 锁（防止多机重复领件，u3-l1 详讲）。

**TaskGroup：一组 PDF 同生同死**

[olmocr/pipeline.py:710-721](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L710-L721)：组内每个路径一个子任务，全部并发跑；任何子任务抛异常，TaskGroup 取消其余子任务并向上抛 `ExceptionGroup`。

**结果收集与容错**

[olmocr/pipeline.py:723-739](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L723-L739)：逐个取子任务结果，`try/except` 把失败的置为 `None` 跳过；tarball 返回文档列表、普通 PDF 返回单个文档，用 `isinstance(result, list)` 归一。

**落盘与完成标记**

[olmocr/pipeline.py:742-763](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L742-L763)：先写本地临时 JSONL，再按 workspace 是 S3 还是本地目录选择 `upload_file` 或 `copyfile` 到 `results/output_{hash}.jsonl`，临时文件在 `finally` 里删除。`--markdown` 分支（[L766-L794](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L766-L794)）逐文档写 markdown 文件，路径由 `get_markdown_path` 清洗（u2-l8）。最后 [L802](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L802) `mark_done` 写 done flag——**只有整组成功落盘后才写**，中途崩溃的组下次重跑时会被 `initialize_queue` 重新捞出来，这正是 u1-l3 讲过的最终一致性来源。

**worker 的"不死"设计**

[olmocr/pipeline.py:803-804](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L803-L804)：整个处理体包在 `try/except Exception` 里，失败只 `logger.exception`，循环继续领下一件。坏一个组不会拖垮整条流水线——那个组没有 done flag，留给下次运行。

**全局状态的三处定义**

[olmocr/pipeline.py:78-89](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L78-L89)：`metrics = MetricsKeeper(window=60*5)`（5 分钟滑动窗口指标）、`tracker = WorkerTracker()`（每个 worker 当前在干嘛）、`vllm_queued_requests`（初始 None，由 vLLM 日志解析线程更新，见 [L876-880](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L876-L880)）、两个 `BoundedSemaphore`（渲染线程闸门按 CPU 数、请求闸门按 CLI 参数）。这些全局量就是四类协程之间的"公告板"。

**收尾的 cancel + gather 惯用法**

[olmocr/pipeline.py:1465-1473](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1465-L1473)：`cancel()` 只是**请求**取消，被取消协程要等到自己的下一个 `await` 点才真正退出；随后带 `return_exceptions=True` 的 gather 确保循环里不再挂着僵尸任务再退出进程。`metrics_reporter` 的 `while True`（[L984](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L984)）与 `vllm_server_host` 的重启循环（[L915](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L915)）都靠这一步停下来。

#### 4.2.4 代码实践

**实践：写一个 30 行的迷你编排器，复刻 main() 的任务结构（可本地运行）**

1. **实践目标**：用最小可运行代码体感 `create_task` / `gather` / `cancel` / `return_exceptions=True` 的协作方式。
2. **操作步骤**：把下面代码存为 `mini_orchestrator.py`（**示例代码**，非项目源码）并运行：

   ```python
   import asyncio, random

   async def worker(wid, queue):                    # 对应 pipeline.worker
       while True:
           item = await queue.get()
           if item is None:                         # 队列空哨兵
               print(f"worker{wid} exiting")
               break
           await asyncio.sleep(random.uniform(0.05, 0.2))  # 模拟处理一页
           print(f"worker{wid} done item{item}")

   async def reporter(queue):                       # 对应 metrics_reporter
       while True:
           print(f"[report] queue remaining: {queue.qsize()}")
           await asyncio.sleep(0.3)

   async def main():                                # 对应 pipeline.main
       queue = asyncio.Queue()
       for i in range(10):
           await queue.put(i)
       for _ in range(3):                           # 哨兵 = get_work 返回 None
           await queue.put(None)

       rep = asyncio.create_task(reporter(queue))
       workers = [asyncio.create_task(worker(i, queue)) for i in range(3)]
       await asyncio.gather(*workers)               # 对应 L1462
       rep.cancel()                                 # 对应 L1467
       await asyncio.gather(rep, return_exceptions=True)  # 对应 L1473
       print("Work done")                           # 对应 L1523

   asyncio.run(main())                              # 对应 cli_main
   ```

3. **观察现象**：三个 worker 交错消费；reporter 每 0.3 秒打印一次队列余量；所有 worker 退出后 reporter 被 cancel，程序打印 `Work done` 结束。
4. **预期结果**：输出顺序每次运行略有不同（并发本义），但 `Work done` 一定是最后一行。把它与 [olmocr/pipeline.py:1453-1473](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1453-L1473) 逐行对照，你会发现结构完全同构，只是 olmocr 的 `queue.get()` 换成了带锁的 `work_queue.get_work()`。

#### 4.2.5 小练习与答案

**练习 1**：如果某个 worker 处理一个组时抛了未捕获异常，整个流水线会崩吗？那个组后来怎么样了？

**答案**：不会崩。[olmocr/pipeline.py:803-804](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L803-L804) 捕获一切异常只记日志，worker 继续 `while True` 领下一件。该组没写 done flag，下次运行同一 workspace 时 `initialize_queue` 会把它重新入队，实现"失败留给下一轮"。

**练习 2**：`await asyncio.gather(*worker_tasks)` 为什么不会永远等下去？

**答案**：因为每个 worker 的退出条件确定：`get_work()` 返回 `None`（队列被 `initialize_queue` 剔除已完成项后的余量耗尽）就 `break`（[L703-L705](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L703-L705)）。队列是有限集合且只减不增（运行期间不再 populate），所以所有 worker 必然先后退出。

**练习 3**：为什么 main 结束前要 `cancel()` 之后再 `gather(..., return_exceptions=True)`，而不是直接退出进程？

**答案**：`metrics_reporter` 和 `vllm_server_host` 都是 `while True` 长驻协程，不 cancel 就永不结束，`asyncio.run` 关闭事件循环时会因仍有未完成任务而告警/报错。cancel 后协程要等到下一个 `await` 点才真正抛 `CancelledError`，所以还要 gather 一次等取消落地；`return_exceptions=True` 把 `CancelledError` 当普通结果收集而不是向上抛，保证收尾代码干净执行完。

### 4.3 检查点式流程梳理：拿日志当断点用

#### 4.3.1 概念说明

读异步代码的难点是"执行顺序不在文件顺序里"。一个实用技巧是：**作者埋的日志就是天然的检查点**——每条 `logger.info` 都对应流程图上一个节点。olmocr 的日志格式统一（时间戳 - 模块 - 级别 - 消息），把消息文本反查回源码行号，就能在不打断点的情况下"单步跟踪"一次运行。这张对照表也是日后排查"卡在哪一步"的第一工具。

#### 4.3.2 核心流程

一次完整的内部服务器运行，日志大致按此顺序出现（→ 表示必然先后依赖）：

```text
Got --pdfs argument... (L1335)
  → Found N regular pdf paths and M tarballs (L1375)
  → Sampling PDFs... 进度条 (L1384)
  → Calculated items_per_group: X (L1405)
  → Starting pipeline with PID xxx (L1427)
  → Downloading model with hugging face '...' (L971)
  → vllm server is ready. (L944)
  → Worker N processing work item <hash> (L707)   ×20 交错
  → Queue remaining: ... (L986)                   每 10 秒一条
  → Worker N exiting due to empty queue (L704)    ×20
  → FINAL METRICS SUMMARY (L1478) → Work done (L1523)
```

#### 4.3.3 源码精读

下表把关键日志锚点到源码（均为"打印这一行的那一行代码"）：

| 日志消息（节选） | 源码位置 | 含义 |
| --- | --- | --- |
| `Found {N} regular pdf paths and {M} tarballs to add` | [olmocr/pipeline.py:1375](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1375) | 输入分类完成，即将开始页数采样 |
| `Calculated items_per_group: X based on average pages...` | [olmocr/pipeline.py:1405](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1405) | ④ 的收尾，分组大小已定 |
| `Starting pipeline with PID ...` | [olmocr/pipeline.py:1427](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1427) | 走过短路分支的标志——**没看到它说明你走的是 --stats/--beaker** |
| `Using internal server at ...` / `Using external server at ...` | [olmocr/pipeline.py:1434](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1434) / [L1436](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1436) | use_internal_server 两路分叉的可见证据 |
| `Beaker job sleeping for N seconds...` | [olmocr/pipeline.py:1309](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1309) | 只在集群副本中出现，错峰下载 |
| `Downloading model with hugging face '...'` | [olmocr/pipeline.py:971](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L971) | download_model 的 HF 分支 |
| `vllm server is ready.` | [olmocr/pipeline.py:944](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L944) | ⑦ 探活成功，马上要拉 workers |
| `Worker {id} processing work item {hash}` | [olmocr/pipeline.py:707](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L707) | 某 worker 领到一个工作组 |
| `Got {N} docs for {hash}` | [olmocr/pipeline.py:739](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L739) | TaskGroup 完成，即将落盘 |
| `Queue remaining: ...` | [olmocr/pipeline.py:986](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L986) | metrics_reporter 每 10 秒的心跳 |
| `Worker {id} exiting due to empty queue` | [olmocr/pipeline.py:704](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L704) | 单个 worker 的终点 |
| `FINAL METRICS SUMMARY` / `Work done` | [olmocr/pipeline.py:1478](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1478) / [L1523](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1523) | ⑩ 总结与全剧终 |

#### 4.3.4 代码实践

**实践：日志-行号对照实验（可本地运行）**

1. **实践目标**：用一次真实（轻量）运行验证短路路径的日志顺序。
2. **操作步骤**：
   1. 选一个 `tests/gnarly_pdfs/` 下的 PDF（先 `ls` 确认文件名）；
   2. 运行 `olmocr /tmp/olmocr-u2l1 --pdfs <该PDF> --stats --disk_logging /tmp/u2l1.log`；
   3. 打开 `/tmp/u2l1.log`，把每一行日志在上表中找位置；
   4. 对找不到的行（如 stats 自己的输出），用消息文本在 pipeline.py 里 `grep -n` 反查行号。
3. **观察现象**：日志应在 `Calculated items_per_group` 或 `Found ... pdf paths` 之后直接进入统计输出，全程没有 `Starting pipeline with PID`、没有模型下载、没有 vLLM。
4. **预期结果**：你得到一张"本次运行实际经过的检查点"子集表，与 4.1.2 流程图中 ①→④ 的路径吻合。真实输出以本地为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：用户反馈"命令跑了但什么都没转换"，日志里出现了 `Starting pipeline with PID` 和 `No work to do, exiting`。问题出在哪一步？

**答案**：走到 ⑥（[olmocr/pipeline.py:1442-1444](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1442-L1444)）说明队列初始化后余量为 0。两种常见原因：workspace 里这些 PDF 之前已经处理完（done flag 齐全），属于正常断点续跑行为；或者 `--pdfs` 没传/没匹配到任何文件，但后者通常还会先出现 `Found 0 regular pdf paths` 日志，可据此区分。

**练习 2**：`Queue remaining` 日志由哪个协程打印？它为什么会自己停下来？

**答案**：`metrics_reporter`（[olmocr/pipeline.py:983-989](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L983-L989)），`while True` 每 10 秒一轮。它自己不会停，是 main 在 workers 全部退出后对它调用 `cancel()`（[L1467](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1467)）才停——这就是"长驻协程靠取消收尾"的模式。

**练习 3**：如何仅凭日志判断一次运行用的是内部还是外部服务器？

**答案**：看 `Using internal server at http://localhost:.../v1`（[L1434](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1434)）还是 `Using external server at ...`（[L1436](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1436)）；辅助证据是内部路径还会先打印模型下载日志（L960/L966/L971 之一），且 stdout 里混有 vLLM 子进程的服务器日志。

## 5. 综合实践

**任务：绘制属于你自己的 `main()` 全景流程图（本讲核心实践）**

1. **实践目标**：把 4.1.2 的文字流程图内化成一张你自己画的、带函数名的完整流程图。
2. **操作步骤**：
   1. 通读 [olmocr/pipeline.py:1199-1523](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1199-L1523)；
   2. 画一张图（纸笔、`mermaid` 或 draw.io 均可），**必须包含**这些节点并在每个节点旁标注对应函数名与行号：
      - WorkQueue 创建（`WorkQueue(S3Backend/LocalBackend)`）
      - `--pdfs` 收集（含 `expand_s3_glob`、`PdfReader` 采样、`populate_queue`）
      - `--stats`（`print_stats`）与 `--beaker`（`submit_beaker_job`）两个短路分支——用菱形决策框
      - GPU 检查（`check_torch_gpu_available`）与模型下载（`download_model`）
      - vLLM 启动（`vllm_server_host` → `vllm_server_task`）与就绪等待（`vllm_server_ready`）
      - workers 启动（`worker` × N）与等待（`asyncio.gather`）
      - 最终 metrics 汇总（`metrics.get_metrics_summary`）
   3. 给 `use_internal_server` 的两路画不同颜色的边；
   4. 用 4.3.4 的 `--stats` 运行日志验证：你实际走的那条路径在图上标绿。
3. **需要观察的现象**：画完后你会发现整张图只有一个"汇聚点"——`await asyncio.gather(*worker_tasks)`；短路分支都是"单向出口"。
4. **预期结果**：一张能挂在墙上、后续七讲每讲往一个方框里填细节的地图。完成后建议把它存到你的学习笔记里，从 u2-l2 起反复回看。

## 6. 本讲小结

- `main()` 是纯编排层：**搭台（参数、队列、模型、服务器）→ 派活（N 个 worker 消费 WorkQueue）→ 收场（cancel + 总结）**，本身不做任何 OCR 具体工作。
- 流程可切成 10 个检查点；`--stats` 与 `--beaker` 在队列填充**之后**短路返回，所以轻量环境也能填充并观察队列。
- `use_internal_server = not args.server` 这一个布尔值在 ①⑤⑦ 三处改变流程：是否查 GPU、是否下载模型、是否把 `args.server` 改写为 localhost 并把模型名统一为 `"olmocr"`。
- 运行时事件循环上有四类协程：**20 个 worker、0/1 个 vLLM 看护、1 个报表协程、main 自己**，靠共享队列、模块级全局变量（metrics/tracker/vllm_queued_requests/信号量）和 cancel 协作，几乎没有直接调用。
- worker 是"不死"消费者：异常只记日志不退出；done flag 只在整组落盘后写入，失败的组留给下一次运行——断点续跑语义的代码根源在 [olmocr/pipeline.py:803-804](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L803-L804)。
- 读异步代码的实用技巧：**拿日志当断点**，把消息文本反查回源码行号，即可无调试器单步跟踪。

## 7. 下一步学习建议

本讲是单元 2 的地图，接下来按流程图从左到右逐站精读：

1. **u2-l2 输入收集**：放大检查点 ④——五种输入形态（本地 PDF/图片、txt 列表、S3 glob、tarball）的分支细节与图片转 PDF。
2. **u2-l3 PDF 渲染**：放大 worker 内部的第一站 `render_pdf_to_base64png`，理解 72 像素/点坐标约定。
3. **u2-l6 单页处理与重试**：放大 `process_page` / `try_single_page`，看温度阶梯与旋转纠错如何使用本讲提到的 `finished_on_attempt_N` 指标。
4. 若你对第 ⑦ 步的服务器管理更感兴趣，可先跳 **u3-l3 vLLM 服务器生命周期**，再回来继续单元 2。

建议顺序：u2-l2 → u2-l3 → u2-l4 → u2-l5 → u2-l6 → u2-l7 → u2-l8，每讲对照本讲流程图定位"我现在在哪个方框里"。

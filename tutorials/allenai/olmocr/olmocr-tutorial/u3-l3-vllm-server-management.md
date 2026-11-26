# u3-l3 vLLM 服务器生命周期管理

## 1. 本讲目标

本讲聚焦 `olmocr/pipeline.py` 中「服务器看护三件套」：`vllm_server_task`、`vllm_server_host`、`vllm_server_ready`。学完本讲，你应该能够：

1. 说出 `vllm serve` 命令行是如何被逐段拼装出来的，并解释每个参数对 GPU 显存、内存、GPU 数量等资源占用的影响（尤其是 `--limit-mm-per-prompt {"video": 0}` 为什么能省出内存）。
2. 读懂 `process_line` 中两个 `re.search` 正则分别从服务器日志里捕获什么指标，以及全局变量 `vllm_queued_requests` 如何反过来影响 `process_page` 的并行重试决策。
3. 描述服务器的就绪探测机制（HTTP 轮询 `/v1/models`）与崩溃自动重启策略（最多 5 次，耗尽即终止整个流水线）。

承接 u2-l1（main() 的编排主线）：本讲深入其中「起 vLLM」这一个检查点的前后实现。

## 2. 前置知识

- **vLLM 是什么**：一个开源的大模型推理引擎。`vllm serve <模型路径>` 会启动一个兼容 OpenAI Chat Completions API 的 HTTP 服务器，提供 PagedAttention、连续批处理（continuous batching）等高吞吐能力。olmOCR 的推理请求（u2-l7 的 `apost`）就是发给它的。
- **为什么 olmOCR 要「自己管」服务器**：在默认模式下（不给 `--server`），olmOCR 不假设外部有人帮你把 vLLM 跑好，而是把 vLLM 当作一个**子进程**拉起，并额外做三件事：盯着它的 stdout 日志提取负载指标、崩溃后自动重启、自己退出时顺手杀掉子进程。这就是本讲的三个最小模块。
- **子进程管理**：`asyncio.create_subprocess_exec` 在事件循环里启动一个外部命令，并通过 PIPE 拿到它的 stdout/stderr，逐行读取。
- **健康探活**：判断「服务器好了没」不看进程是否存在，而是发一个真实的 HTTP GET 请求（`/v1/models` 端点），拿到 200 才算就绪。
- **模块级全局变量**：Python 中一个模块顶层的变量可以被同模块内所有函数读写（配合 `global` 声明）。这里用它充当「服务器看护协程」与「worker 协程」之间的单向信息通道。
- **术语**：KV cache（推理时缓存注意力中间结果的显存区域，越大越能同时服务更多请求）；TP/Tensor Parallel（把一个模型切到多张卡）；DP/Data Parallel（跑多个引擎副本）。

## 3. 本讲源码地图

本讲几乎全部内容都在 `olmocr/pipeline.py` 一个文件里：

| 位置 | 内容 | 在本讲中的角色 |
|---|---|---|
| [olmocr/pipeline.py:55-68](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L55-L68) | `logger` 与 `server_logger`（名为 "vllm"）的初始化 | 服务器原始日志的输出去向 |
| [olmocr/pipeline.py:81-82](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L81-L82) | 全局变量 `vllm_queued_requests = None` | 指标通道的**生产端**定义 |
| [olmocr/pipeline.py:334-374](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L334-L374) | `process_page` 的非旋转错误重试路径 | 指标通道的**消费端** |
| [olmocr/pipeline.py:807-908](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L807-L908) | `vllm_server_task` | 模块 4.1：拼命令、拉子进程、解析日志 |
| [olmocr/pipeline.py:911-926](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L911-L926) | `vllm_server_host` | 模块 4.2：崩溃重启循环 |
| [olmocr/pipeline.py:929-953](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L929-L953) | `vllm_server_ready` | 模块 4.3：HTTP 探活 |
| [olmocr/pipeline.py:1225](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1225) | `--max_server_ready_timeout` 参数定义（默认 600） | 探活超时的来源 |
| [olmocr/pipeline.py:1241-1258](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1241-L1258) | `--server`/`--api_key` 与 vLLM 参数组 | 各参数默认值来源 |
| [olmocr/pipeline.py:1286](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1286) | `use_internal_server = not args.server` | 「内部起服务器」还是「外部已有服务器」的分叉 |
| [olmocr/pipeline.py:1424-1467](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1424-L1467) | main() 中的装配与收尾 | 三件套的接线图 |

## 4. 核心概念与源码讲解

### 4.1 vllm_server_task：拼装命令、拉起子进程、解析日志

#### 4.1.1 概念说明

`vllm_server_task` 是「单次看护」的最小单元：它把 `vllm serve` 的命令行**逐段拼出来**，启动 vLLM 子进程，然后做两件长期工作——把服务器的每一行日志原样转录到 olmOCR 自己的日志体系里，并从日志中**用正则提取负载指标**（正在运行多少请求、队列里还压着多少请求）。

为什么值得专门写代码盯日志？因为 vLLM 会周期性打印一行类似 `Running: 4 reqs, Waiting: 12 reqs` 的指标日志，这是**免费得来的全局负载信息**：worker 协程可以据此判断「服务器现在闲不闲」，从而决定重试策略（见 4.1.4 实践 C）。不去解析日志的话，要获得同样信息就得额外调 vLLM 的 metrics API，成本更高。

#### 4.1.2 核心流程

```
vllm_server_task(model_name_or_path, args, unknown_args):
    1. 拼固定参数的命令行：
       vllm serve <模型路径> --port <port> --disable-log-requests
                  --uvicorn-log-level warning --served-model-name olmocr
                  --tensor-parallel-size <tp> --data-parallel-size <dp>
                  --limit-mm-per-prompt {"video": 0}
    2. 可选追加：--gpu-memory-utilization（仅当用户显式给值）
                 --max-model-len（仅当用户显式给值）
    3. 把 unknown_args（olmocr 未识别的 CLI 参数）原样追加 → 透传给 vllm serve
    4. create_subprocess_exec 启动子进程（PIPE 接管 stdout/stderr，OMP_NUM_THREADS=1）
    5. atexit 注册退出钩子：本进程退出前先 terminate 子进程（防孤儿进程霸占 GPU）
    6. 为 stdout、stderr 各起一个读行协程 read_stream → 逐行交给 process_line：
       a. 每行写入 server_logger（名字叫 "vllm" 的 logger）
       b. 看到 "Detected errors during sampling" → 认为模型权重损坏 → sys.exit(1)
       c. 第一次看到就绪标语（"The server is fired up and ready to roll!"
          或 "Starting vLLM API server"）→ 置位标志
       d. 正则 r"Running: (\d+)" → 更新正在运行的请求数（并记录峰值）
       e. 正则 r"(?:Waiting|Pending):\s*(\d+)" → 更新全局 vllm_queued_requests
    7. await proc.wait()   ← 整个函数阻塞在这里，子进程活着期间不返回
       （若被 cancel：terminate 子进程，最多再等 10 秒，然后把 CancelledError 抛回去）
```

#### 4.1.3 源码精读

**（a）命令拼装**——[olmocr/pipeline.py:807-825](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L807-L825)：这段代码构造了 `cmd` 列表。注意 `--limit-mm-per-prompt` 的值 `'{"video": 0}'` 是一个 Python 字符串；由于最终用 `create_subprocess_exec`（不经过 shell）执行，它会被原样作为**一个** argv 元素传给 vLLM，不存在 shell 引号转义问题。源码注释写明动机：禁用视频编码器省下的内存可以让给 KV cache（致谢 @charitarthchugh）。Qwen2.5-VL 是视觉语言模型，其多模态处理器默认会为「可能传入视频」预留编码器资源；OCR 场景每条请求只有一张图片，把 video 上限设为 0 即可让 vLLM 跳过这部分预留。

```python
cmd = [
    "vllm", "serve", model_name_or_path,
    "--port", str(args.port),
    "--disable-log-requests",
    "--uvicorn-log-level", "warning",
    "--served-model-name", "olmocr",
    "--tensor-parallel-size", str(args.tensor_parallel_size),
    "--data-parallel-size", str(args.data_parallel_size),
    "--limit-mm-per-prompt", '{"video": 0}',  # 禁用视频编码器，省内存给 KV cache
]
```

**（b）条件追加与透传**——[olmocr/pipeline.py:827-834](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L827-L834)：`--gpu-memory-utilization` 与 `--max-model-len` 只在用户显式给值时才追加到命令里（`args.gpu_memory_utilization` 的 argparse 默认是 `None`，即不传，此时沿用 vLLM 自身的默认值；而 `args.max_model_len` 默认 16384，所以几乎总会传）。最后 `unknown_args` 被原样拼接——这就是 u1-l5 讲过的「olmocr 未识别的参数自动转发给 vLLM」机制在服务器侧的落点。

**（c）子进程启动与退出钩子**——[olmocr/pipeline.py:836-851](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L836-L851)：用 `asyncio.create_subprocess_exec` 启动子进程并接管两个标准流。环境变量里强制 `OMP_NUM_THREADS=1`，源码注释解释：一台多 GPU 机器上同时跑多份 olmOCR 时，避免各进程的 CPU 线程互相争抢。`atexit.register(_kill_proc)` 保证：即使主进程走 `sys.exit(1)` 这类异常路径退出（本文件里有多处），也会先终止 vLLM 子进程，不会留下霸占显存的孤儿进程。

**（d）日志解析 process_line**——[olmocr/pipeline.py:857-880](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L857-L880)：每行日志先交给 `server_logger`（在 [olmocr/pipeline.py:59-60](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L59-L60) 创建，logger 名就叫 `"vllm"`，`propagate=False`，挂的是与主 logger 相同的 console/file handler，所以 vLLM 原始日志会完整出现在 olmOCR 的控制台与 `--disk_logging` 文件里）。随后四个分支：

```python
if "Detected errors during sampling" in line:        # 采样报错 → 权重疑似损坏，直接终止
    logger.error("..."); sys.exit(1)

if not server_printed_ready_message and ("The server is fired up and ready to roll!" in line
                                         or "Starting vLLM API server" in line):
    server_printed_ready_message = True               # 记录服务器打印过就绪标语

if match := re.search(r"Running: (\d+)", line):       # 正则一：正在运行的请求数
    current_running = int(match.group(1))
    if current_running > peak_running_req: ...        # 追踪历史峰值，仅用于日志
    last_running_req = current_running

if match := re.search(r"(?:Waiting|Pending):\s*(\d+)", line):  # 正则二：排队等待数
    global vllm_queued_requests
    last_queue_req = int(match.group(1))
    vllm_queued_requests = last_queue_req             # 写入全局变量，供 process_page 消费
```

两个正则的含义：

- **正则一 `r"Running: (\d+)"`**：捕获 vLLM 指标日志里 `Running: N` 中的 N，即**此刻正在被引擎处理的请求数**。代码用它维护 `last_running_req` 与峰值 `peak_running_req`（峰值只在刷新纪录时打一条日志，方便人眼观察吞吐上限）。
- **正则二 `r"(?:Waiting|Pending):\s*(\d+)"`**：捕获 `Waiting: N` 或 `Pending: N` 中的 N，即**在队列里排队、尚未被引擎接手的请求数**。同时兼容两种写法是因为 vLLM 不同版本的日志字段名有变化（旧版 `Waiting`、新版 `Pending`）；`\s*` 允许冒号后空白数不固定。捕获值写入模块级全局变量 `vllm_queued_requests`（其定义在 [olmocr/pipeline.py:81-82](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L81-L82)，初始为 `None`）——这就是「服务器看护协程 → worker 协程」的指标通道。

另有一个诚实的观察：`server_printed_ready_message` 这个标志在当前代码里只被写入、不参与任何后续控制流（真正的就绪门控由 4.3 的 HTTP 探测完成），可以把它理解为「看过就绪标语」的状态记录。

**（e）读流与退出处理**——[olmocr/pipeline.py:882-908](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L882-L908)：`read_stream` 逐行读取并解码，单行解析抛异常只告警跳过（防脏数据中断整个看护器）。主流程 `await proc.wait()`——**子进程活着期间，`vllm_server_task` 就停在这里**；只有当服务器进程退出（崩溃/被杀）时才返回。若收到 `CancelledError`（流水线正常收尾时 main() 会 cancel 本任务，见 [olmocr/pipeline.py:1464-1466](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1464-L1466)），则先 `proc.terminate()`，再最多等 10 秒让其退出，然后重新抛出 `CancelledError`。

**（f）指标如何被消费**——[olmocr/pipeline.py:343-363](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L343-L363)：在 `process_page` 的非旋转错误重试路径里，每失败一次后都会检查 `remaining_attempts = retry_attempts[i+1:]`；若还有剩余尝试**且** `vllm_queued_requests == 0`，就把剩余尝试一次性全部并行发射（`asyncio.as_completed` 谁先成功谁赢，其余取消）。

#### 4.1.4 代码实践

**实践目标**：把 `vllm_server_task` 拼出的完整命令整理成参数注释表；用可本地运行的正则实验验证两个指标提取正则；解释 `vllm_queued_requests` 对重试决策的影响。

**操作步骤**：

1. **参数表（源码阅读）**：对照 [olmocr/pipeline.py:807-834](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L807-L834) 与 argparse 定义 [olmocr/pipeline.py:1249-1258](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1249-L1258)，自己先写出完整命令与每个参数的注释，再对照下面的参考表（默认值情形）：

   ```text
   vllm serve <model_name_or_path> \
     --port 30024 \
     --disable-log-requests \
     --uvicorn-log-level warning \
     --served-model-name olmocr \
     --tensor-parallel-size 1 \
     --data-parallel-size 1 \
     --limit-mm-per-prompt {"video": 0} \
     --max-model-len 16384
   ```

   | 参数 | 默认 | 注释（资源影响） |
   |---|---|---|
   | `serve <模型路径>` | 位置参数 | 启动 OpenAI 兼容推理服务，加载 download_model 下载好的本地权重 |
   | `--port` | 30024 | HTTP 监听端口；main() 随后把 `args.server` 设为 `http://localhost:30024/v1` |
   | `--disable-log-requests` | 固定开 | 不逐请求打日志：百万页场景下日志量可控，指标行不被淹没 |
   | `--uvicorn-log-level warning` | 固定 warning | 压低 Web 层日志级别，进一步降噪 |
   | `--served-model-name olmocr` | 固定 olmocr | 对外把模型注册为 `olmocr`，配合 main() 中 `args.model = "olmocr"`（[olmocr/pipeline.py:1430-1434](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1430-L1434)），屏蔽奇怪的本地模型路径 |
   | `--tensor-parallel-size` | 1 | 单个引擎切分到几张 GPU（TP），决定一个副本占几张卡 |
   | `--data-parallel-size` | 1 | 跑几个引擎副本（DP），显存与算力随其线性翻倍 |
   | `--limit-mm-per-prompt {"video": 0}` | 固定 | 每条 prompt 的多模态输入上限：video 置 0 = 关闭视频编码器，省下的内存划给 KV cache，能同时服务更多请求 |
   | `--gpu-memory-utilization` | 未给则不传 | vLLM 可预占的显存比例；显式传了才追加（配合 [olmocr/pipeline.py:1282-1284](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1282-L1284) 启动时的调参提示） |
   | `--max-model-len` | 16384 | KV cache 按多大 token 上限分配；显存不够时调小它是最常见救急手段 |
   | `unknown_args` 逐个追加 | — | olmocr 不认识的 CLI 参数原样透传给 vllm serve |

2. **正则实验室（可本地运行，示例代码）**：新建 `regex_lab.py`，用几条模拟的 vLLM 指标日志行验证两个正则的捕获行为：

   ```python
   # 示例代码：非项目代码，仅用于验证 pipeline.py 中的两个正则
   import re

   lines = [
       "INFO ... Avg generation throughput: ... Running: 4 reqs, Waiting: 12 reqs",
       "INFO ... Running: 2 reqs, Pending: 7 reqs",   # 新版 vLLM 用 Pending
       "INFO ... Running: 0 reqs, Waiting: 0 reqs",
       "The server is fired up and ready to roll!",
   ]

   for line in lines:
       if m := re.search(r"Running: (\d+)", line):
           print("running =", m.group(1))
       if m := re.search(r"(?:Waiting|Pending):\s*(\d+)", line):
           print("queue   =", m.group(1))
   ```

3. **指标通道梳理（源码阅读）**：对照 [olmocr/pipeline.py:81-82](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L81-L82)、[olmocr/pipeline.py:876-880](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L876-L880)（生产端）与 [olmocr/pipeline.py:343-351](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L343-L351)（消费端），用自己的话写 3-5 句：`vllm_queued_requests` 从哪来、到哪去、什么时候是 `None`。

**需要观察的现象**：步骤 2 的脚本对前 3 行应各打出一对 `running`/`queue`，其中第 2 行走的是 `Pending` 分支；最后一行（就绪标语）两个正则都不匹配、什么都不打印。

**预期结果**：`running = 4 / queue = 12`、`running = 2 / queue = 7`、`running = 0 / queue = 0`。若把 `Running:` 改成 `Running :`（冒号前加空格），正则一不再匹配——它要求冒号后恰好一个空格；正则二因为用了 `\s*` 对空白更宽容。

**待本地验证**：真实 `vllm serve` 的指标日志格式（需要 `olmocr[gpu]` 环境与 GPU）请在本机跑一次流水线后，在 olmOCR 控制台中搜 `vllm running req` 观察；本讲不假装已运行过。

**`vllm_queued_requests` 如何影响 process_page（步骤 3 的参考结论）**：

- 它**只在内部服务器路径**被更新（`process_line` 只在 `vllm_server_task` 里运行），外部 `--server` 模式下永远是初始值 `None`。
- 在非旋转错误的重试循环里（u2-l6 讲过：默认串行、逐档升温），每次失败后多问一句「队列空了吗」：`vllm_queued_requests == 0` 意味着服务器马上要闲下来了，此时把剩余重试**并行**打出去几乎没有边际成本，还能让这一页尽快结束；反过来，队列还压着请求时并行重试会跟其他页面的首次尝试抢算力，得不偿失，于是维持串行。
- 初始值 `None` 时 `None == 0` 为 `False`，并行分支永不触发——这是一个**保守的安全默认**：信息不足时不激进。

#### 4.1.5 小练习与答案

1. **为什么 `--limit-mm-per-prompt` 要把 video 设为 0？**
   答：Qwen2.5-VL 的多模态处理器会为视频输入预留编码器与内存；OCR 每条请求只有一张图片，禁用视频编码器（`{"video": 0}`）让 vLLM 跳过这部分预留，省下的内存可划给 KV cache，提高并发容量。源码注释原话是 "Disabling video encoder saves RAM that you can put towards the KV cache"。
2. **使用外部服务器（`--server http://...`）时，`vllm_queued_requests` 会是什么值？后果是什么？**
   答：保持 `None`。因为只有内部服务器路径才会运行 `vllm_server_task`/`process_line`。后果是 `process_page` 的 `None == 0` 判定恒为 `False`，非旋转错误重试**全程串行**，不会触发「队列空时并行」的加速分支。
3. **`peak_running_req` 被用在哪里？**
   答：只在刷新纪录时打一条 `New peak running requests` 日志（[olmocr/pipeline.py:870-873](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L870-L873)），供运维观察这台服务器的历史吞吐上限，不参与任何控制流。
4. **`OMP_NUM_THREADS=1` 是为了什么？**
   答：源码注释说明：同一台多 GPU 机器上跑多份 olmOCR 时，如果每份的底层库都默认开满 CPU 线程，会发生线程争抢；钉死为 1 可避免这种 contention。

### 4.2 vllm_server_host：崩溃自动重启与最多 5 次

#### 4.2.1 概念说明

`vllm_server_host` 把「单次看护」升级为「自愈循环」。关键洞察是：`vllm_server_task` 只有在**子进程退出**时才会返回（见 4.1.3 (e)）——所以「函数返回了」就等价于「服务器死了」。host 的重启策略因此可以写得极简：返回一次就算一次失败，重启一次；连续失败满 5 次就放弃，终止整个流水线。

值得注意的设计取向：单页失败有 pdftotext 兜底、永不阻塞流水线（u2-l6），但**推理服务器死亡是全局致命事件**——没有它所有 worker 都干不了活，所以这里不做局部降级，而是快速失败（fail fast）。

还有一个跨模块的配合：服务器重启的几十秒窗口内，worker 的请求会连接失败，这由 u2-l7 的 `try_single_page_with_backoff` 指数退避（10·2ⁿ 秒）兜住——退避给了服务器重新起来的时间，两者合起来才构成完整的自愈故事。

#### 4.2.2 核心流程

```
vllm_server_host(model, args, unknown_args):
    MAX_RETRIES = 5, retry = 0
    while retry < MAX_RETRIES:
        await vllm_server_task(...)      # 返回 = 服务器进程退出（崩溃/被杀）
        logger.warning("VLLM server task ended")
        retry += 1                        # 注意：没有 sleep，立即重启
    # 循环耗尽：第 5 次启动的服务器也崩了
    logger.error("Ended up starting the vllm server more than {retry} times, cancelling pipeline")
    提示用户检查 vLLM 安装（给出官方文档链接）
    sys.exit(1)                           # 终止整个流水线进程
```

**数量核对**：`retry` 从 0 起，循环条件 `retry < 5`，因此服务器最多被启动 **5 次**（第 1 次启动后崩→retry=1；……第 5 次启动后崩→retry=5，循环条件不再满足）。错误消息里的 `{retry}` 此时的值恰好是 5。

#### 4.2.3 源码精读

[olmocr/pipeline.py:911-926](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L911-L926)：整个函数就是一个 while 循环加终止出口。逐行看：

- `await vllm_server_task(...)` 正常情况下**永不返回**（服务器一直活着就阻塞在 `proc.wait()`），所以这行的语义就是「看护一次，直到服务器退出」。
- 服务器退出后**立即**重启，没有任何退避间隔——vLLM 崩溃的常见原因是启动配置/显存问题，重试价值有限，所以设计上选择了「快速连试 5 次、不行就撤」。
- 5 次耗尽后先打两条 error（第二条给出 vLLM 官方安装文档链接，因为「反复起不来」大概率是 vLLM 版本/安装问题），然后 `sys.exit(1)`。
- 正常收尾路径：main() 在所有 worker 结束后 `vllm_server.cancel()`（[olmocr/pipeline.py:1464-1466](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1464-L1466)），`CancelledError` 传入 `vllm_server_task` 的 `await proc.wait()` 处，走 4.1.3 (e) 的 terminate 分支后重新抛出，host 任务随之安静结束——**不会**把这当成一次「崩溃重启」。这里印证一个 asyncio 细节：取消信号会穿透 `await` 链逐层传播。

#### 4.2.4 代码实践

**实践目标**：用一个不依赖 GPU 的模拟脚本，验证 host 循环的「最多启动 5 次」计数与退出行为。

**操作步骤**：新建 `host_sim.py`（示例代码，非项目代码）：

```python
# 示例代码：模拟 vllm_server_host 的重启循环（结构同 pipeline.py:911-926）
import asyncio

MAX_RETRIES = 5

async def fake_server_task(start_count):
    print(f"第 {start_count} 次启动服务器")
    await asyncio.sleep(0.05)   # 假装运行了一小会儿
    print("服务器崩溃退出")      # 子进程退出 → vllm_server_task 返回

async def host():
    retry = 0
    while retry < MAX_RETRIES:
        await fake_server_task(retry + 1)
        print("VLLM server task ended")
        retry += 1
    print(f"Ended up starting the vllm server more than {retry} times, cancelling pipeline")
    # 真实代码此处是 sys.exit(1)，脚本里用 print 代替

asyncio.run(host())
```

**需要观察的现象**：`第 N 次启动服务器` 与 `服务器崩溃退出`、`VLLM server task ended` 交替出现，统计「启动」出现的次数；最后一条是放弃消息。

**预期结果**：`第 1 次`…`第 5 次` 共 5 次启动，随后打印 `...more than 5 times...`。把 `fake_server_task` 改成「第 3 次起不再崩溃（改为 `await asyncio.sleep(3600)` 模拟健康运行）」，则只会看到 3 次启动（真实场景对应：流水线会一直陪着健康的服务器运行，直到 main() 收尾时 cancel 它）。

**待本地验证**：真实 vLLM 崩溃重启行为（例如故意把 `--max-model-len` 设到超出显存）请在有 GPU 的环境观察，本讲不假装已运行。

#### 4.2.5 小练习与答案

1. **host 判断「服务器崩溃」的依据是什么？**
   答：没有任何错误码或错误类型解析——`vllm_server_task` 返回即视为一次失败（其内部 `proc.wait()` 只在子进程退出时返回）。这是一种非常朴素的「进程活着=成功」心跳观。
2. **重启之间有退避吗？为什么这样设计？**
   答：没有，立即重启。崩溃通常是配置/显存/安装这类确定性原因，指数退避意义不大；设计取舍是「快速连试 5 次 + 硬上限」，上限防止无限重启掩盖环境问题，耗尽后快速失败并给出排查指引。
3. **流水线正常跑完时，服务器是怎么被关掉的？走 host 的重启分支吗？**
   答：不走。main() 末尾 `vllm_server.cancel()` 取消 host 任务，`CancelledError` 在 `vllm_server_task` 的 `await proc.wait()` 处被捕获：先 `proc.terminate()`，再最多等 10 秒，然后 `raise` 重新抛出。host 的 while 循环根本没机会执行 `retry += 1`。

### 4.3 vllm_server_ready：HTTP 探活与超时

#### 4.3.1 概念说明

「vLLM 子进程已经启动」不等于「推理 API 已经可用」——加载 7B 权重、初始化 KV cache、绑定端口需要几十秒甚至几分钟。`vllm_server_ready` 解决的问题是：**在服务器真正能服务之前，把流水线挡在起跑线上**。

它的判定标准很务实：不解析日志、不猜状态，直接发 HTTP GET 请求到 OpenAI 兼容 API 的 `/models` 端点，拿到 200 就算就绪。轮询固定每 1 秒一次，最多 `--max_server_ready_timeout`（默认 600）次，超时则抛异常终止流水线。

注意这个函数对**外部服务器模式同样生效**：main() 里 `await vllm_server_ready(args)` 是无条件调用的（[olmocr/pipeline.py:1451](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1451)），只是内部模式下探活对象是刚拉起的 localhost 服务器，外部模式下是你给的 `--server` 地址（还可带 `--api_key` Bearer 鉴权）。

#### 4.3.2 核心流程

```
vllm_server_ready(args):
    max_attempts = args.max_server_ready_timeout   # 默认 600
    delay_sec = 1                                  # 固定 1 秒，不是指数退避
    url = args.server.rstrip('/') + "/models"
    for attempt in 1..max_attempts:
        try:
            GET url  （若设置了 api_key，带 Authorization: Bearer 头）
            200  → "vllm server is ready." → return
            非200 → 记录"Unexpected status code"
        except → warning "Please wait for vllm server to become ready..."
        await asyncio.sleep(1)
    raise Exception("vllm server did not become ready after waiting.")
```

一个值得玩味的单位耦合：参数名叫 `max_server_ready_timeout`（单位按 help 文本是「秒」），实现上是「最多 N 次尝试 × 每次间隔 1 秒」，所以等待时长约等于参数值——**次数被当秒数用**，改 delay 就会悄悄改变语义。

#### 4.3.3 源码精读

**（a）探活循环**——[olmocr/pipeline.py:929-953](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L929-L953)：

```python
max_attempts = args.max_server_ready_timeout   # 默认 600（见 argparse）
delay_sec = 1
url = f"{args.server.rstrip('/')}/models"      # 如 http://localhost:30024/v1/models

for attempt in range(1, max_attempts + 1):
    try:
        headers = {}
        if args.server and hasattr(args, "api_key") and args.api_key:
            headers["Authorization"] = f"Bearer {args.api_key}"   # 外部鉴权服务器
        async with httpx.AsyncClient() as session:
            response = await session.get(url, headers=headers)
            if response.status_code == 200:
                logger.info("vllm server is ready.")
                return
            ...
    except Exception:
        logger.warning(f"Attempt {attempt}: Please wait for vllm server to become ready...")
    await asyncio.sleep(delay_sec)

raise Exception("vllm server did not become ready after waiting.")
```

要点：

- **`rstrip('/')`**：`http://host:30024/v1/` 和 `.../v1` 都能正确拼出 `/v1/models`，不产生 `//models`。
- **`httpx` 而非手写 `apost`**：u2-l7 讲过热路径用约 95 行手写 HTTP 客户端规避连接池死锁，而这里是低频路径（每秒最多一次），稳定与便利优先，用 `httpx.AsyncClient` 完全合理——同文件里「高频手工、低频库调」的对照非常清晰。
- **异常吞噬**：连接被拒（服务器还没绑上端口）会抛异常，这里只 warning 后继续下一轮——这本来就是「没就绪」的一种表现。
- **超时即失败**：循环耗尽抛普通 `Exception`，沿 main() 向上冒泡终止流水线；vLLM 子进程由 u2-l1 讲过的收尾逻辑与 `atexit` 钩子（4.1.3 (c)）清理。

**（b）相关默认值**——[olmocr/pipeline.py:1225](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1225)（`--max_server_ready_timeout` 默认 600，help 文本："Number of seconds to wait for vllm to become ready before exiting."）；[olmocr/pipeline.py:1241-1247](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1241-L1247)（`--server` 与 `--api_key` 的定义）。

**（c）main() 中的接线**——[olmocr/pipeline.py:1286](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1286) 定下分叉 `use_internal_server = not args.server`；[olmocr/pipeline.py:1424-1434](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1424-L1434) 内部模式先 `check_torch_gpu_available()`、下载模型、把 `args.server` 改写为 localhost；[olmocr/pipeline.py:1446-1451](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1446-L1451) 把 host 任务作为后台任务创建后**立刻探活**——探活通过之前，worker 一律不启动，避免对着没就绪的服务器白发请求。

#### 4.3.4 代码实践

**实践目标**：在没有任何 GPU/真实 vLLM 的机器上，用 `python3 -m http.server` 伪装一个「就绪的推理服务器」，直接调用**项目真实的** `vllm_server_ready` 函数，验证其成功路径与超时失败路径。

**操作步骤**：

1. 准备一个能对 `/v1/models` 返回 200 的假服务器（`http.server` 会把 URL 路径映射到本地文件）：

   ```bash
   mkdir -p /tmp/readylab/v1
   touch /tmp/readylab/v1/models      # 使 GET /v1/models 命中文件 → 200
   cd /tmp/readylab && python3 -m http.server 8000
   ```

2. 新建 `ready_probe.py`（示例代码，但调用的是真实的 olmocr 函数）：

   ```python
   # 示例代码：调用真实的 olmocr.pipeline.vllm_server_ready
   import argparse, asyncio
   from olmocr.pipeline import vllm_server_ready

   ns = argparse.Namespace(server="http://localhost:8000/v1",
                            api_key=None, max_server_ready_timeout=10)
   asyncio.run(vllm_server_ready(ns))
   print("=== 探活成功，流水线可以开工 ===")
   ```

3. 运行 `python3 ready_probe.py`，观察日志。
4. 做反例：把 `server` 改成一个没有服务的端口（如 `http://localhost:9999/v1`）、`max_server_ready_timeout=3`，再跑一次，数一数 warning 行数并用秒表感受节奏。

**需要观察的现象**：步骤 3 中应立刻（第 1 次尝试）出现 `vllm server is ready.`；步骤 4 中应出现 3 条 `Please wait for vllm server to become ready...`（每条约隔 1 秒），随后脚本报 `Exception: vllm server did not become ready after waiting.`。

**预期结果**：成功路径秒过；失败路径严格等于「N 次 × 1 秒」后抛异常——印证 4.3.2 的单位耦合分析。

**注意**：`import olmocr.pipeline` 需要轻量安装（u1-l2）加 httpx/boto3 等基础依赖；若提示缺 `tqdm`，执行 `pip install tqdm` 即可（该依赖在 `pyproject.toml` 中未显式声明，通常作为其他包的传递依赖出现）。

**待本地验证**：真实 vLLM 从启动到 200 的等待时长因硬件而异，请在有 GPU 的环境观察日志中 `Attempt N` 出现到消失的实际次数。

#### 4.3.5 小练习与答案

1. **把 `--max_server_ready_timeout` 从 600 改成 60，含义是什么？**
   答：最多进行 60 次探活、每次间隔 1 秒，即约 60 秒内服务器还不就绪就抛异常退出流水线。适合启动很快的小模型；对 7B 模型+慢盘的环境 600 秒更稳妥。
2. **为什么探 `/v1/models`，而不是只看「vLLM 进程是否存在」？**
   答：进程存在不代表权重加载完、KV cache 分配好、HTTP 路由挂载好；`/models` 是 OpenAI 兼容 API 的标准端点，返回 200 说明推理 API 已真正可用。就绪的判定必须基于「业务能应答」，这是分布式系统里典型的 readiness 探针思想。
3. **外部服务器模式（`--server` + `--api_key`）下，探活请求有什么不同？**
   答：[olmocr/pipeline.py:937-938](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L937-L938) 会在设置了 `api_key` 时给请求加上 `Authorization: Bearer <key>` 头，适配 DeepInfra 这类带鉴权的远程推理服务。

## 5. 综合实践

**任务：给「服务器看护三件套」做一次全链路故障推演。** 不需要任何环境，只需对照源码填写下表的第三、四列（这是把 4.1-4.3 串起来的最好方式）：

| 故障场景 | 关键源码位置 | 系统行为（自己先填） | 最终结局（自己先填） |
|---|---|---|---|
| ① 服务器启动后 30 秒崩溃（第 1 次） | `proc.wait()` 返回 → host 循环 | | |
| ② 服务器反复崩溃，连续 5 次 | host 循环耗尽 | | |
| ③ 服务器进程活着，但 600 秒内 API 一直不就绪 | `vllm_server_ready` 循环耗尽 | | |
| ④ 日志出现 `Detected errors during sampling` | `process_line` 分支 | | |
| ⑤ 所有任务正常完成，main() 收尾 | `vllm_server.cancel()` | | |

**参考答案**（做完再对）：

- **①** `vllm_server_task` 从 `await proc.wait()` 处返回，host 打 `VLLM server task ended`、`retry=1` 并**立即重启**服务器；重启窗口内 worker 的在途请求连接失败，由 u2-l7 的 `try_single_page_with_backoff` 以 10·2ⁿ 秒指数退避重试，等服务器回来。流水线整体几乎无感。
- **②** 第 5 次启动的实例也退出后 `retry=5`，循环条件失败：打印 `Ended up starting the vllm server more than 5 times, cancelling pipeline` 与 vLLM 官方安装文档链接，`sys.exit(1)` 终止整个流水线——服务器死亡被当作全局致命事件，不做局部降级。
- **③** `vllm_server_ready` 在第 600 次探活后抛 `Exception("vllm server did not become ready after waiting.")`，异常沿 main() 上抛终止流水线；vLLM 子进程由 atexit 钩子 terminate，不留孤儿进程。
- **④** `process_line` 检测到该字符串后打 error（"model is probably corrupt"）并 `sys.exit(1)`——权重损坏属于重试无意义的问题，直接快速失败；atexit 钩子同样负责清理子进程。
- **⑤** main() 对 host 任务调用 `.cancel()`，`CancelledError` 在 `vllm_server_task` 的 `await proc.wait()` 处被捕获：`proc.terminate()` → 最多等 10 秒 → 重新抛出。host 的 while 循环没把这次当成崩溃，服务器被干净地关闭。

（选做）如果你想在无 GPU 的机器上更进一步：仿照 4.2.4 的思路，把「假服务器进程 + 日志指标行 + host 重启循环 + 就绪探活」写成一个 50 行左右的迷你看护器，人为让假服务器第 2 次启动才「就绪」、第 3 次彻底崩溃，观察三件套各自的行为是否与本表一致。

## 6. 本讲小结

- **`vllm_server_task`**（[pipeline.py:807-908](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L807-L908)）是单次看护单元：拼 `vllm serve` 命令（含透传 unknown_args、条件追加显存参数、`{"video": 0}` 关视频编码器省内存给 KV cache）、拉子进程（`OMP_NUM_THREADS=1`、atexit 兜底清理）、逐行解析日志。
- **两个正则**是免费的负载遥测：`r"Running: (\d+)"` 抓在途请求数，`r"(?:Waiting|Pending):\s*(\d+)"` 抓排队请求数并写入全局 `vllm_queued_requests`，后者让 `process_page` 能在「队列空」时把剩余重试并行打出去。
- **`vllm_server_host`**（[pipeline.py:911-926](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L911-L926)）= 极简自愈循环：「`vllm_server_task` 返回即崩溃」，立即重启，硬上限 5 次，耗尽 `sys.exit(1)`；正常收尾靠 cancel 信号走 terminate 分支，不算崩溃。
- **`vllm_server_ready`**（[pipeline.py:929-953](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L929-L953)）以 HTTP GET `/v1/models` 是否 200 判定就绪，固定 1 秒轮询、默认最多 600 次，超时抛异常；内部与外部服务器两种模式共用它。
- 三件套与 u2-l7 的指数退避共同构成自愈体系：服务器重启窗口内 worker 靠连接退避活下来。
- 设计哲学对照：单页失败 → 局部兜底（pdftotext fallback）；服务器失败 → 全局快速失败。错误的严重度决定兜底的层级。

## 7. 下一步学习建议

- 下一讲 **u3-l4（Beaker 集群执行）** 讲的仍然是 `pipeline.py`，但视角从「单机看护一个服务器」升级为「把整套流水线（含本讲的三件套）提交到 N 个 GPU 副本的集群」，与本讲首尾相接。
- 回顾性建议：重读 u2-l6（`process_page` 的两条重试路径）中「队列空时并行」的那段，现在你应该能指出它的信息来源正是本讲的正则二。
- 延伸阅读：vLLM 官方文档中 `vllm serve` 的参数页与 OpenAI 兼容 server 文档，可以逐个对照本讲参数表里「未由 olmOCR 包装、只能靠 unknown_args 透传」的参数（如 `--kv-cache-dtype`）。

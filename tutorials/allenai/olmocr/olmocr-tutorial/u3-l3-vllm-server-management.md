# vLLM 服务器生命周期管理

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 olmOCR 如何把 vLLM 推理服务作为**子进程**管理：拼装命令行、启动、看护日志、优雅退出。
2. 逐个参数解释 `vllm serve` 命令，尤其是 `--limit-mm-per-prompt '{"video": 0}'` 为什么要关闭 video 通路。
3. 理解 `process_line` 中两个 `re.search` 正则如何从服务器日志中「白嫖」队列深度指标，以及全局变量 `vllm_queued_requests` 如何反过来影响 `process_page` 的并行重试决策。
4. 描述就绪探测（`vllm_server_ready`）的轮询逻辑与崩溃重启（`vllm_server_host`）的 5 次上限。

承接关系：本讲是 [u2-l1 pipeline 主流程](u2-l1-pipeline-main-flow.md) 中「起 vLLM」这个检查点的展开。你在 u2-l6 已经见过重试体系的全貌、在 u2-l7 见过请求闸门与退避，本讲补上的是**服务器这一侧**的生命周期管理——它们共同构成完整的推理热路径。

## 2. 前置知识

用通俗语言解释几个本讲会用到的概念：

- **子进程（subprocess）**：一个 Python 程序可以在运行中启动另一个程序（这里是 `vllm` 命令），两者是操作系统层面的父子关系。父进程可以拿到子进程的标准输出/错误流（管道 PIPE），逐行读取。
- **vLLM**：一个开源的大模型推理服务框架。执行 `vllm serve <模型>` 会启动一个 HTTP 服务，暴露与 OpenAI API 兼容的 `/v1/chat/completions` 等接口。olmOCR 的推理请求全部发给它。
- **KV cache**：大模型推理时缓存注意力键值对的显存区域，越大能同时处理的请求越多。显存总量固定，所以「模型权重 + 视觉编码器 + KV cache」是在抢同一块蛋糕。
- **日志即指标（log scraping）**：不给服务加监控接口，而是直接用正则从它打印的日志行里提取数字。简单粗暴，但零侵入。
- **就绪探测（readiness probe）**：进程活着 ≠ 服务可用。模型加载、KV cache 分配可能耗时数分钟，所以要反复发 HTTP 请求直到拿到 200 才认为「就绪」。
- **看护者模式（supervisor）**：一个循环包裹住子进程，子进程死了就重启，重启次数超上限就放弃并报错退出。
- **`atexit` 钩子**：Python 进程退出时自动执行的回调，用来「带走」子进程，防止孤儿进程残留。

一个重要的前置认知（来自 [u3-l1](u3-l1-workqueue-abstraction.md) 和 [u3-l2](u3-l2-local-and-s3-backends.md)）：**队列状态全在 workspace（本地或 S3）里，推理服务器是无状态的**。这是崩溃重启策略敢于「随便重启」的底气——重启后 worker 从队列继续领活即可，已完成的组有 done flag 兜底。

## 3. 本讲源码地图

本讲只涉及少量文件，但每个引用点的行号都要精确：

| 文件 | 作用 |
| --- | --- |
| [olmocr/pipeline.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py) | 本讲主战场：`vllm_server_task` / `vllm_server_host` / `vllm_server_ready` 三件套、全局变量 `vllm_queued_requests`、`main()` 的集成点、`process_page` 的消费点 |
| [olmocr/check.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/check.py) | `check_torch_gpu_available`：走内部服务器前的 GPU 体检 |
| [README.md](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md) | 外部服务器用法与独立 `vllm serve` 示例，是理解参数的对照材料 |

## 4. 核心概念与源码讲解

### 4.1 vllm_server_task：拼装并看护一个 vLLM 子进程

#### 4.1.1 概念说明

当用户**没有**提供 `--server` 参数时（即 `use_internal_server = not args.server`，见 [olmocr/pipeline.py:1286](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1286)），olmOCR 不去连远端 API，而是**自己拉起一个 vLLM 服务**当推理引擎。

`vllm_server_task` 是这个「拉起 + 看护」的最小单元，它承担四件事：

1. 把用户的 CLI 参数翻译成一条 `vllm serve` 命令行；
2. 用 `asyncio.create_subprocess_exec` 启动子进程，捕获 stdout/stderr；
3. 逐行读取子进程日志：转发到独立 logger、探测致命错误、用正则提取队列深度指标；
4. 响应取消（pipeline 结束时优雅终止子进程）。

注意一个分工细节：`vllm_server_task` **不做**重启，也不做就绪判断——那是另外两个函数的职责（4.2 与 4.3 节）。三个函数各管一段生命周期，像接力棒。

#### 4.1.2 核心流程

用默认参数（单卡、模型 `allenai/olmOCR-2-7B-1025-FP8`）时，最终拼出的完整命令等价于：

```bash
vllm serve allenai/olmOCR-2-7B-1025-FP8 \
  --port 30024 \
  --disable-log-requests \
  --uvicorn-log-level warning \
  --served-model-name olmocr \
  --tensor-parallel-size 1 \
  --data-parallel-size 1 \
  --limit-mm-per-prompt '{"video": 0}' \
  --max-model-len 16384
```

（`--gpu-memory-utilization` 默认不传，原因见下文。）

执行时序：

```text
main()
  └─ vllm_server_host(...)            # 4.2 节：外层重启循环
       └─ vllm_server_task(...)       # 本节：单个服务器的一生
            1. 拼命令行 cmd
            2. create_subprocess_exec(cmd, stdout=PIPE, stderr=PIPE, OMP_NUM_THREADS=1)
            3. atexit.register(_kill_proc)        # 父进程退出时带走子进程
            4. 起 read_stream(stdout)、read_stream(stderr) 两个日志协程
                 └─ 每行 → process_line(line)
                      ├─ server_logger.info(line)              # 转发日志
                      ├─ "Detected errors during sampling" → sys.exit(1)
                      ├─ 就绪消息 → server_printed_ready_message = True
                      ├─ re.search(r"Running: (\d+)")          # 正在运行的请求数
                      └─ re.search(r"(?:Waiting|Pending):\s*(\d+)") → 全局 vllm_queued_requests
            5. await proc.wait()                # 子进程退出（或被取消）
            6. 被 cancel 时：proc.terminate() → 等 10 秒 → re-raise
```

#### 4.1.3 源码精读

**（1）命令行拼装**。[olmocr/pipeline.py:807-825](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L807-L825) 构造固定部分的参数列表：

```python
cmd = [
    "vllm", "serve", model_name_or_path,
    "--port", str(args.port),
    "--disable-log-requests",
    "--uvicorn-log-level", "warning",
    "--served-model-name", "olmocr",
    "--tensor-parallel-size", str(args.tensor_parallel_size),
    "--data-parallel-size", str(args.data_parallel_size),
    "--limit-mm-per-prompt",
    '{"video": 0}',  # Disabling video encoder saves RAM that you can put towards the KV cache, thanks @charitarthchugh
]
```

逐参数注释（这是本讲实践任务的核心交付物）：

| 参数 | 默认值（argparse 处） | 作用与资源影响 |
| --- | --- | --- |
| `serve <model>` | 位置参数 `--model` | 启动 OpenAI 兼容 HTTP 服务。`model_name_or_path` 已经过 `download_model` 解析成本地路径或 HF repo id |
| `--port` | 30024（[L1258](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1258)） | HTTP 监听端口。`main()` 随后把 `args.server` 改写成 `http://localhost:{port}/v1` |
| `--disable-log-requests` | 恒传 | 关闭每个请求一条日志的 verbose 输出。百万级请求下既省 I/O，也让 stdout 只剩引擎状态行——**这正是下一步正则解析的前提** |
| `--uvicorn-log-level warning` | 恒传 | 压低 Web 框架层日志，进一步降噪 |
| `--served-model-name olmocr` | 恒传 | 对外暴露的模型名固定为 `olmocr`。与 [olmocr/pipeline.py:1433](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1433) 的 `args.model = "olmocr"` 对应——不管本地模型路径多「奇怪」，请求体里的 model 名永远是 `olmocr` |
| `--tensor-parallel-size` / `-tp` | 1（[L1256](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1256)） | 张量并行：把单次前向计算切到多张卡。`-tp 2` 会占用 2 张 GPU |
| `--data-parallel-size` / `-dp` | 1（[L1257](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1257)） | 数据并行：起多份引擎副本分摊请求。`-dp 2` 同样占 2 张卡 |
| `--limit-mm-per-prompt '{"video": 0}'` | 恒传 | **本讲的关键参数**：多模态模型默认会为每条提示预留 video 编码通路，而 OCR 只发图片。显式声明 video 数量为 0，把省下来的显存让给 KV cache（源码注释原话："Disabling video encoder saves RAM that you can put towards the KV cache"）——即「用不上就别占内存」 |

**（2）条件参数与未知参数透传**。[olmocr/pipeline.py:827-834](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L827-L834)：

```python
if args.gpu_memory_utilization is not None:
    cmd.extend(["--gpu-memory-utilization", str(args.gpu_memory_utilization)])

if args.max_model_len is not None:
    cmd.extend(["--max-model-len", str(args.max_model_len)])

if unknown_args:
    cmd.extend(unknown_args)
```

- `--gpu-memory-utilization` 的 argparse 定义**没有默认值**（[L1252-1254](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1252-L1254)），所以默认不传，交给 vLLM 自己决定——显存预算留给用户调优。
- `--max_model_len` 默认 16384（[L1255](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1255)），非 None，所以**总会**追加。它限制 KV cache 按多长上下文分配，调小能救活「KV cache is larger than available memory」的启动失败（[main() 在 L1282-1284 的提示日志](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1282-L1284)也印证了这一点）。
- `unknown_args` 是 `parse_known_args` 没认出的参数（机制见 [u1-l5](u1-l5-cli-arguments.md)），在这里**原样追加**给 `vllm serve`——用户因此能用 olmOCR 不认识的任何 vLLM 参数（比如 `--swap-space`），这是「薄封装」设计的落点。

**（3）子进程创建与环境变量**。[olmocr/pipeline.py:836-851](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L836-L851)：

```python
proc = await asyncio.create_subprocess_exec(
    *cmd,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    env={**os.environ, "OMP_NUM_THREADS": "1"},
)

def _kill_proc():
    try:
        proc.terminate()
    except:
        logger.info("VLLM Process already terminated")

atexit.register(_kill_proc)
```

两个细节：

- `OMP_NUM_THREADS=1`：OpenMP 线程数压到 1。源码注释解释：同一台多卡机器上跑多份 olmOCR（每份管自己那张卡）时，若每份都开满 CPU 线程会互相争抢——与其各自内卷，不如各留 1 线程。
- `atexit.register(_kill_proc)`：即使 Python 主进程异常退出，也尽力带走 vLLM 子进程，避免留下占着显存的孤儿。`except` 兜底意味着「已经死了也不报错」。

**（4）日志解析协程**。共享变量初始化在 [olmocr/pipeline.py:853-855](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L853-L855)：`last_running_req, peak_running_req, last_queue_req = 0, 0, 0` 与 `server_printed_ready_message = False`。

`process_line` 是每行日志的处理函数（[olmocr/pipeline.py:857-880](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L857-L880)），做四件事：

```python
async def process_line(line):
    nonlocal last_running_req, last_queue_req, peak_running_req, server_printed_ready_message
    server_logger.info(line)

    if "Detected errors during sampling" in line:
        logger.error("Cannot continue, sampling errors detected, model is probably corrupt")
        sys.exit(1)

    if not server_printed_ready_message and ("The server is fired up and ready to roll!" in line or "Starting vLLM API server" in line):
        server_printed_ready_message = True

    if match := re.search(r"Running: (\d+)", line):
        current_running = int(match.group(1))
        if current_running > peak_running_req:
            peak_running_req = current_running
            logger.info(f"New peak running requests: {peak_running_req}")
        last_running_req = current_running

    if match := re.search(r"(?:Waiting|Pending):\s*(\d+)", line):
        global vllm_queued_requests
        last_queue_req = int(match.group(1))
        vllm_queued_requests = last_queue_req
        logger.info(f"vllm running req: {last_running_req} queue req: {last_queue_req}")
```

逐条拆解：

1. **日志转发**：`server_logger` 是名为 `"vllm"` 的独立 logger（[olmocr/pipeline.py:59-68](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L59-L68)），`propagate = False` 使它不冒泡到根 logger，从而与 olmOCR 自身日志分开；开启 `--disk_logging` 时它也会写文件（[L1280](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1280)）。
2. **致命错误熔断**：日志出现 `Detected errors during sampling`（vLLM 检测到采样出错，通常意味着模型文件损坏）时直接 `sys.exit(1)` 终止整个进程。宁可整批停也不产出垃圾结果。
3. **就绪消息标记**：见到 `"The server is fired up and ready to roll!"` 或 `"Starting vLLM API server"` 就把 `server_printed_ready_message` 置 True。**值得注意**：在当前 HEAD 这个标志只写不读（全仓库仅 [L855/L865-866](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L865-L866) 三处出现），真正决定「可以发请求了」的是 4.3 节的 HTTP 探测——它是从「日志判就绪」演化为「探活判就绪」后留下的历史形态，也是阅读源码时常见的「化石代码」样本。
4. **两个正则提取指标**（本讲实践重点）：
   - `r"Running: (\d+)"`：捕获**正在被引擎处理**的请求数。副作用是维护历史峰值 `peak_running_req`，破纪录时打一条日志——一个零成本的容量观测。
   - `r"(?:Waiting|Pending):\s*(\d+)"`：捕获**排队等待**的请求数，兼容 `Waiting:` 与 `Pending:` 两种写法（不同 vLLM 版本的引擎状态行措辞不同），并写入全局变量 `vllm_queued_requests`——它的消费端在 4.4 节。

vLLM 引擎会周期性打印一行「引擎状态」（含吞吐、Running/Waiting 请求数等），olmOCR 不调任何监控 API，就从这行日志里持续抠出队列深度。前提正是前面 `--disable-log-requests` + `--uvicorn-log-level warning` 做的降噪。

`read_stream`（[olmocr/pipeline.py:882-891](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L882-L891)）是个循环：读一行、`decode("utf-8").rstrip()`、喂给 `process_line`；单行解析抛异常只警告不中断——**日志解析必须是 best-effort 的**，坏一行不能杀死看护循环。随后 stdout/stderr 各起一个协程（[L893-895](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L893-L895)）。

**（5）等待与取消**。[olmocr/pipeline.py:897-908](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L897-L908)：`await proc.wait()` 正常情况下**永不返回**（服务器一直活着）。唯一常态出口是被 cancel：先 `proc.terminate()`，再限时 10 秒等它退干净，超时只警告，最后**重新抛出 CancelledError**（这是 asyncio 取消协议的正确姿势，保证外层 `gather` 能感知）。结尾 `gather(stdout_task, stderr_task, return_exceptions=True)` 把两个日志协程收尾。

#### 4.1.4 代码实践

**实践目标**：不改任何源码，把「命令拼装 + 正则解析」两件事在无 GPU 的轻量环境里亲手复现一遍。

**操作步骤**：

1. 打开 [olmocr/pipeline.py:807-834](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L807-L834)，对照上面 4.1.3 的参数表，**亲手**画一张自己的参数注释表（以自己 repos 的默认参数为准算出完整命令）。
2. 写一个脚本，把 `process_line` 里的两个正则原样抄下来，喂给它几行模拟的 vLLM 引擎状态日志。以下为**示例代码**（非项目源码）：

```python
# regex_lab.py —— 复现 process_line 的两个正则（示例代码）
import re

lines = [
    'INFO 02-14 12:00:00 [EngineCore]: Avg prompt throughput: 123.4 tokens/s. Running: 12 reqs, Waiting: 34 reqs',
    'INFO 02-14 12:00:05 [EngineCore]: Avg prompt throughput: 120.0 tokens/s. Running: 15 reqs, Pending: 0 reqs',
    'INFO 02-14 12:00:10 [EngineCore]: Engine hit KV cache limit, will recompute prefix. Running: 8 reqs, Waiting: 2 reqs',
    'INFO 02-14 12:00:12 [Launcher] The server is fired up and ready to roll!',
]

vllm_queued_requests = None  # 模拟全局变量初值（pipeline.py:82）

for line in lines:
    if match := re.search(r"Running: (\d+)", line):
        print(f"running  = {match.group(1)}")
    if match := re.search(r"(?:Waiting|Pending):\s*(\d+)", line):
        vllm_queued_requests = int(match.group(1))
        print(f"queued   = {match.group(1)}  -> vllm_queued_requests = {vllm_queued_requests}")
    if "The server is fired up and ready to roll!" in line:
        print("ready message spotted")
```

3. 运行：`python regex_lab.py`。
4. 加一行 `'INFO ... Running: 99 reqs, Waiting: 1 reqs'` 再跑，观察第 3 行模拟日志里 `KV cache` 字样会不会干扰两个正则（答案：不会，正则只锚定 `Running:`/`Waiting|Pending:` 后面的数字）。

**需要观察的现象**：第 1 行同时命中两个正则；第 2 行用 `Pending:` 也能命中第二个正则（这正是 `(?:Waiting|Pending)` 兼容写法的意义）；第 4 行只触发 ready 消息分支。

**预期结果**：`vllm_queued_requests` 从 `None` 变为 34，再变为 0——注意它**最后一次被赋的值**会一直留存，这正是全局变量传递「最新观测」的语义。

（若想在真实服务器上验证，需要 `olmocr[gpu]` 环境与 GPU：直接跑 `olmocr ./localworkspace --pdfs tests/gnarly_pdfs/*.pdf` 并观察日志中的 `vllm running req: X queue req: Y` 行。**待本地验证**。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--gpu-memory-utilization` 默认不传，而 `--max-model-len` 默认总是传？

答案：`--gpu-memory-utilization` 的 argparse 没有 default（[L1252-1254](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1252-L1254)），值为 None 时跳过追加，把显存预算的决定权留给 vLLM 默认值或用户；而 `--max-model-len` 默认 16384 非 None，永远追加——它是 olmOCR 想主动钉死的上下文上限（OCR 页面远用不到 16k+），也直接控制 KV cache 的分配规模。

**练习 2**：如果不小心删掉了 `--disable-log-requests`，对本讲的日志解析会造成什么影响？

答案：vLLM 会为每个请求打一条日志。百万级请求意味着 stdout 洪水：日志协程 CPU 与 I/O 压力剧增，引擎状态行被请求日志稀释（正则仍能匹配到，只是噪音变大），`--disk_logging` 时文件体积失控。所以它不只是「日志好看」，而是日志解析链路的性能前提。

**练习 3**：`_kill_proc` 里的裸 `except` 为什么是合理（或至少可接受）的？

答案：这个钩子只在「进程已经在退出」时运行，唯一职责是尽力带走子进程。`proc.terminate()` 对已退出的进程可能抛 `ProcessLookupError`，此时唯一合理动作就是什么都不做——`except` 吞掉并记一条日志。在 atexit 语境里再抛异常没有接收方，反而可能掩盖真正的退出原因。

### 4.2 vllm_server_host：崩溃自动重启与 5 次上限

#### 4.2.1 概念说明

`vllm_server_task` 只描述「一个服务器实例的一生」。它正常情况下**永不返回**——所以一个朴素的推论成立：**它一旦返回，就说明服务器死了**。

`vllm_server_host` 就是利用这个推论的看护者：一个 `while` 循环不断重新拉起 `vllm_server_task`，直到重启次数达到上限 `MAX_RETRIES = 5`，然后放弃整个作业（`sys.exit(1)`）。

为什么敢重启、重启为什么便宜？承接 [u3-l1](u3-l1-workqueue-abstraction.md)/[u3-l2](u3-l2-local-and-s3-backends.md) 的结论：

- 待办活在 WorkQueue（内存 `asyncio.Queue` + 持久化索引），已完成组有 done flag，正在处理的组有 worker 锁（锁会超时过期、可被抢）；
- 服务器本身无状态——它死了，worker 的请求会失败，但 [u2-l7](u2-l7-async-http-client.md) 讲过的连接错误退避路径会扛住网络层抖动，等服务器回来后温度阶梯重试继续。

于是「服务器挂了」从致命故障降级为**一次可自动恢复的抖动**。5 次上限是保险丝：短时间连挂 5 次说明是环境问题（显存不够、vLLM 版本不对），继续重试只是烧钱。

#### 4.2.2 核心流程

```text
vllm_server_host(model, args, unknown_args)
  retry = 0
  循环（最多执行 5 次循环体）:
    vllm_server_task(...)        # 服务器活着时永远阻塞在这里
    ↓ 到这里 = 服务器退出了
    打 warning "VLLM server task ended"
    retry += 1
  循环耗尽:
    打 error（含 vLLM 安装文档链接）
    sys.exit(1)                  # 整个进程非零退出
```

用公式表达重启预算：设首次启动为第 1 次实例，则最多启动的实例数 \( N_{\max} = \text{MAX\_RETRIES} = 5 \)，即**首次启动 + 最多 4 次自动重启**。

#### 4.2.3 源码精读

完整源码很短（[olmocr/pipeline.py:911-926](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L911-L926)）：

```python
async def vllm_server_host(model_name_or_path, args, unknown_args=None):
    MAX_RETRIES = 5
    retry = 0

    while retry < MAX_RETRIES:
        await vllm_server_task(model_name_or_path, args, unknown_args)
        logger.warning("VLLM server task ended")
        retry += 1

    if retry >= MAX_RETRIES:
        logger.error(f"Ended up starting the vllm server more than {retry} times, cancelling pipeline")
        logger.error("")
        logger.error(
            "Please make sure vllm is installed according to the latest instructions here: https://docs.vllm.ai/en/stable/getting_started/installation/gpu.html"
        )
        sys.exit(1)
```

三个值得咀嚼的细节：

1. **`retry` 从 0 开始**，循环体最多执行 5 次（retry=0..4），每次执行一个完整的「服务器实例生命周期」。所以准确语义是「最多启动 5 个服务器实例」，报错日志里的 `more than {retry} times` 打出来是 5。
2. **`if retry >= MAX_RETRIES` 是防御式写法**：`while` 正常退出的唯一方式就是耗尽重试，此时该条件必然为真。它存在的意义是让「退出即失败」的语义显式化，防止未来有人在循环里加 `break`（比如「正常关停」路径）时误入报错分支。
3. **用 `sys.exit(1)` 而非抛异常**：SystemExit 会沿 asyncio 主循环传播并结束整个进程，让 worker、metrics 等所有协程一起终止，且 shell/Beaker/CI 能看到非零退出码。对比 [u2-l7](u2-l7-async-http-client.md) 的连接退避耗尽同样 `sys.exit(1)`——olmOCR 对「环境级不可恢复错误」统一选择硬退出。

还有一层关系值得注意：`main()` 里 `vllm_server_host` 被包成 task（[olmocr/pipeline.py:1447-1449](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1447-L1449)），与 worker 协程**并行**运行。服务器死亡重启期间，worker 不会停——它们发出的请求失败后进入 u2-l7 的指数退避，最长可累计约 2.84 小时，这给了看护者充裕的恢复窗口（前提是重启成功）。

#### 4.2.4 代码实践

**实践目标**：用一个无 GPU 的迷你 asyncio 程序复刻「看护者 + 随机死亡的工人」，验证重启计数与放弃逻辑。

**操作步骤**：以下为**示例代码**（非项目源码）：

```python
# supervisor_lab.py —— 复刻 vllm_server_host 的看护循环（示例代码）
import asyncio, random

async def worker_instance(n):
    # 模拟一个会随机死掉的服务器实例
    life = random.uniform(0.2, 0.8)
    await asyncio.sleep(life)
    print(f"  instance #{n} died after {life:.2f}s")
    return  # 返回 = 死亡，对应 vllm_server_task 返回

async def host():
    MAX_RETRIES = 5
    retry = 0
    while retry < MAX_RETRIES:
        await worker_instance(retry + 1)
        print(f"  host: warning, instance ended (retry={retry + 1})")
        retry += 1
    print(f"  host: giving up after {retry} instances, sys.exit(1) would fire here")

asyncio.run(host())
```

**需要观察的现象**：实例一个接一个地「死」，host 每次都打 warning 并计数；总共恰好 5 个实例后放弃。

**预期结果**：输出 5 次 `instance #N died`、5 次 warning、最后 1 次 giving up。再对照源码数一遍：`retry` 0→5，循环体恰好执行 5 次。

**进阶（可选）**：把 `MAX_RETRIES` 改成 3、把 `worker_instance` 改成永不返回（`await asyncio.sleep(100)`），观察 host 永远阻塞在第一个实例上——对应「服务器一直活着」的正常情形。

#### 4.2.5 小练习与答案

**练习 1**：服务器重启的瞬间，正在飞行中的请求会发生什么？谁负责兜住？

答案：连接会断开或超时，`apost` 抛出连接类异常（ConnectionError/OSError/TimeoutError），被 `try_single_page_with_backoff` 的指数退避接住（[u2-l7](u2-l7-async-http-client.md)），退避后重发；attempt 与温度不因退避递增，所以页面级进度不丢失。若退避 10 次都失败（约 2.84 小时）才会 `sys.exit(1)`。

**练习 2**：为什么重启上限是 5 而不是 50？

答案：上限的意义是区分「偶发崩溃」与「环境性故障」。偶发崩溃一两次就该恢复；连挂 5 次几乎必然是显存不足、版本不兼容等持续性问题，继续重试只会空烧机器时间。把上限设得过大，等于把确定性的失败拖成马拉松。（当然 5 本身是经验值，源码没有推导。）

**练习 3**：`vllm_server_host` 内部 `await vllm_server_task(...)` 没有 try/except。如果 `vllm_server_task` 抛出普通异常（而非正常返回），会发生什么？

答案：异常会沿 `await` 向上传播，跳出 while 循环、终结 `vllm_server_host` 这个 task，重启逻辑失效（但 `sys.exit(1)` 也不会执行，进程可能挂着其他协程继续跑）。看当前 `vllm_server_task` 实现：`proc.wait()` 与日志读取路径基本不抛普通异常，`read_stream` 又把单行异常吞成了 warning，所以「以返回代替死亡」的约定在实现上是成立的——这是隐式契约，值得在阅读时主动验证。

### 4.3 vllm_server_ready：HTTP 就绪探测

#### 4.3.1 概念说明

子进程起来了，只说明 Python 解释器加载了 vLLM；离「能处理请求」还差：加载 7B 模型权重、分配 KV cache、绑定端口。这一过程可能几分钟。

所以 `main()` 在拉起服务器 task 之后、启动 worker 之前，会**阻塞等待** `vllm_server_ready(args)`（[olmocr/pipeline.py:1446-1451](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1446-L1451)）：

```python
vllm_server = None
if use_internal_server:
    vllm_server = asyncio.create_task(vllm_server_host(model_name_or_path, args, unknown_args))

await vllm_server_ready(args)
```

探测方式很简单：反复 `GET {server}/models`，拿到 200 就认为就绪。注意这里用的是 `httpx`——承接 [u2-l7](u2-l7-async-http-client.md) 的结论：**低频调用用 httpx，热路径才用 apost**，每 1 秒一次的探活完全没必要手写 socket。

它对**外部服务器**同样适用：用户指定 `--server https://api.deepinfra.com/v1` 时（[README.md:320-324](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L320-L324)），同一函数探测远端可用性，还能带 `Authorization: Bearer` 头。

#### 4.3.2 核心流程

```text
url = args.server 去尾斜杠 + "/models"
max_attempts = args.max_server_ready_timeout   # 默认 600
for attempt in 1..max_attempts:
    GET url（可选带 Bearer api_key）
    ├─ 200  → log "vllm server is ready." → return
    ├─ 非200 → log "Unexpected status code"
    └─ 异常（连不上）→ warning "Please wait..."
    sleep 1 秒
全部失败 → raise Exception("vllm server did not become ready after waiting.")
```

等待时间的上界近似为 \[ T_{\max} \approx N_{attempts} \times \Delta t = 600 \times 1\,\text{s} \]（忽略每次请求本身的耗时，实际略长）。

**一个命名陷阱**：参数名叫 `max_server_ready_timeout`、help 写 "Number of seconds"（[olmocr/pipeline.py:1225](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1225)），但实现把它当**尝试次数**用，每次间隔固定 1 秒——两者数值上恰好近似等价（600 次 × 1 秒 ≈ 600 秒）。这是「名字说单位、实现数次数」的经典错位，读源码时要小心。

#### 4.3.3 源码精读

[olmocr/pipeline.py:929-953](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L929-L953)：

```python
async def vllm_server_ready(args):
    max_attempts = args.max_server_ready_timeout
    delay_sec = 1
    url = f"{args.server.rstrip('/')}/models"

    for attempt in range(1, max_attempts + 1):
        try:
            headers = {}
            if args.server and hasattr(args, "api_key") and args.api_key:
                headers["Authorization"] = f"Bearer {args.api_key}"

            async with httpx.AsyncClient() as session:
                response = await session.get(url, headers=headers)

                if response.status_code == 200:
                    logger.info("vllm server is ready.")
                    return
                else:
                    logger.info(f"Attempt {attempt}: Unexpected status code {response.status_code}")
        except Exception:
            logger.warning(f"Attempt {attempt}: Please wait for vllm server to become ready...")

        await asyncio.sleep(delay_sec)

    raise Exception("vllm server did not become ready after waiting.")
```

四个要点：

1. **`rstrip('/')`**：`http://host:8000/v1/` 与 `http://host:8000/v1` 统一，防止拼出 `//models`。
2. **`hasattr(args, "api_key")` 的防御**：如果 `args` 是外部构造的命名空间（比如测试里手工拼的对象），可能没有 `api_key` 属性——先 `hasattr` 再取值。
3. **`except Exception` 一网打尽**：连接拒绝、DNS 失败、超时……启动期的一切网络异常都只是「还没好」，警告一句继续下一轮。对比热路径对异常的精细分流（[u2-l7](u2-l7-async-http-client.md)）：**启动期的失败没有分类价值，只有「好/没好」两个状态**。
4. **耗尽后抛异常**（而不是 sys.exit）：这是「可预期的失败」，让 `main()` 的调用栈决定怎么处理；与 4.2 节「环境不可恢复才硬退」的分层一致。

**`/models` 为什么是好探针**：它是 OpenAI 兼容 API 的元数据端点，返回已加载模型列表。能返回 200 说明 HTTP 层、路由层、模型加载全部就位。

最后把 URL 链路接完整：内部服务器模式下，`main()` 在 [olmocr/pipeline.py:1430-1434](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1430-L1434) 把 `args.server` 改写为 `http://localhost:{args.port}/v1` 并把 `args.model` 固定为 `"olmocr"`（呼应 `--served-model-name olmocr`）；随后 `try_single_page` 在 [olmocr/pipeline.py:161-167](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L161-L167) 用 `{args.server}/chat/completions` 发推理请求。而这一步之前，`check_torch_gpu_available()`（[olmocr/pipeline.py:1424-1425](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1424-L1425)，实现在 [olmocr/check.py:30-42](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/check.py#L30-L42)，要求至少 15GB 显存）已经把「起服务器注定失败」的情形挡在前面。

收尾对称：所有 worker 完成后，`main()` 在 [olmocr/pipeline.py:1464-1473](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1464-L1473) `cancel()` 掉 vllm_server task 与 metrics task，再 `gather(..., return_exceptions=True)` 等它们真正结束——cancel 信号传到 `vllm_server_task` 的 `CancelledError` 分支，优雅 terminate 子进程（4.1.3 第 5 点），加上 atexit 兜底，双保险不留孤儿进程。

#### 4.3.4 代码实践

**实践目标**：用系统自带的 `http.server` 亲眼看一次「探活从失败到成功」的轮询过程（无需 GPU，轻量安装即可，`httpx` 是核心依赖）。

**操作步骤**：

1. 以下为**示例代码**（非项目源码），保存为 `ready_lab.py`：

```python
# ready_lab.py —— 复刻 vllm_server_ready 的轮询逻辑（示例代码）
import asyncio
import sys
import httpx

async def probe(url: str, max_attempts: int, delay_sec: float = 1.0):
    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient() as session:
                response = await session.get(url)
                if response.status_code == 200:
                    print(f"attempt {attempt}: 200 -> ready!")
                    return
                print(f"attempt {attempt}: unexpected status {response.status_code}")
        except Exception as e:
            print(f"attempt {attempt}: waiting... ({type(e).__name__})")
        await asyncio.sleep(delay_sec)
    raise Exception("server did not become ready after waiting.")

url = sys.argv[1]           # 例: http://localhost:39999
attempts = int(sys.argv[2]) # 例: 5
asyncio.run(probe(url, attempts))
```

2. 先探测一个没人监听的端口：`python ready_lab.py http://localhost:39999 5`。
3. 另开一个终端，启动一个静态服务器：`python -m http.server 39999`。
4. 再跑一遍：`python ready_lab.py http://localhost:39999 5`。

**需要观察的现象**：第 2 步里每轮都打印 `waiting... (ConnectError)`（对应源码的 `except Exception` 分支）；第 4 步第一轮就打印 `200 -> ready!`。

**预期结果**：失败侧 5 次尝试后抛出 `Exception`（对应源码 [L953](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L953) 的行为）；成功侧立即返回。注意 `python -m http.server` 对 `/models` 子路径会返回 404——若把 url 换成 `http://localhost:39999/models`，你会看到「unexpected status 404」循环，恰好复现源码里 200 以外的分支。

#### 4.3.5 小练习与答案

**练习 1**：为什么就绪探测放在 `vllm_server_host` task **之外**（`main()` 直接 await），而不是放进 task 内部？

答案：分离「启动服务器」与「确认就绪」两个关注点：`vllm_server_host` 是长驻看护协程，生命周期覆盖整个作业（包括后续重启）；就绪探测是一次性的启动期门槛。放外面还能让探测同时服务外部服务器场景——外部服务器没有 host task，但同样需要 `await vllm_server_ready`。

**练习 2**：探测失败耗尽抛的是普通 `Exception`，而 4.2 节重启耗尽用的是 `sys.exit(1)`。这个不对称是随意的吗？

答案：不是。就绪失败发生在作业「还没开始」时——`main()` 的调用栈（以及 Beaker/CI 的外层重试逻辑）可以捕获并决定处置；而重启耗尽发生在作业中途，其他协程还在飞行，唯有结束整个进程才能干净收场。失败时机不同、处置层级不同。

**练习 3**：如果把 `delay_sec` 从 1 秒改成 0.01 秒，会有什么副作用？

答案：等待上界从 ~600 秒缩到 ~6 秒（对大模型加载远远不够），更隐蔽的问题是「忙轮询」：数百次/秒的请求 + 每次新建 `AsyncClient`（含连接建立、TLS）对尚未就绪的服务器是雪上加霜。1 秒间隔是对「加载耗时分钟级」这个先验的合理折中。

### 4.4 vllm_queued_requests：日志指标反向控制并行重试

#### 4.4.1 概念说明

前两讲（[u2-l6](u2-l6-page-retry-and-rotation.md)）建立了重试体系：非旋转错误默认**串行**重试，只有一种情况会切换成**并行发射**剩余重试——服务器队列空了。本讲补上「队列空了」这个信息**从哪来**：就是 4.1 节 `process_line` 的第二个正则。

数据流是一条跨协程的单向链：

```text
vLLM 子进程 stdout
  → read_stream 协程（每行）
  → process_line 的 re.search(r"(?:Waiting|Pending):\s*(\d+)")
  → 全局变量 vllm_queued_requests          [写入端，pipeline.py:877-879]
  → process_page 的 if remaining_attempts and vllm_queued_requests == 0
                                            [消费端，pipeline.py:345]
  → asyncio.create_task 并行发射剩余重试    [pipeline.py:347-350]
```

为什么这个反馈值得做？重试的并行/串行选择本质是个调度决策：

- **队列非空**：服务器还有积压，你为一个失败页并行发 4 个请求是在给队列火上浇油，加剧排队；
- **队列为空**：服务器闲置，此时并行发射不再伤害任何人，而失败页能更快拿到多次采样，提高收敛速度。

没有这个指标时只能盲选（一直串行或一直并行）；有了它，就变成「看服务器脸色」的自适应策略——而且获取成本几乎为零（解析本来就存在的日志行）。

#### 4.4.2 核心流程

关键代码在 `process_page` 的非旋转错误路径（[olmocr/pipeline.py:334-363](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L334-L363)，u2-l6 已精读过重试语义，这里只看联动点）：

```python
for i, attempt in enumerate(retry_attempts):
    result = await try_single_page_with_backoff(...)
    ...
    # After each failed attempt, check if queue is empty - if so, fire remaining in parallel
    remaining_attempts = retry_attempts[i + 1 :]
    if remaining_attempts and vllm_queued_requests == 0:
        logger.info(f"Queue empty, firing {len(remaining_attempts)} parallel retries for ...")
        tasks = [asyncio.create_task(try_single_page_with_backoff(..., a, ...)) for a in remaining_attempts]
        ...
```

判定条件是两个布尔量的与：

1. `remaining_attempts` 非空——本轮之后还有重试额度；
2. `vllm_queued_requests == 0`——日志观测到的**最新**队列深度为零。

**一个微妙而重要的初值语义**：全局变量初值是 `None`（[olmocr/pipeline.py:81-82](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L81-L82)），而 `None == 0` 在 Python 里是 `False`。也就是说：**在日志还没打印过任何队列深度之前，并行重试永远不会触发**。这带来两个推论（属代码阅读结论，供你验证）：

- 内部 vLLM 模式：服务器启动后要先打出至少一行引擎状态（含 Waiting/Pending），联动才生效——通常几秒内就会发生；
- 外部 `--server` 模式：没有子进程、没有日志解析，`vllm_queued_requests` 恒为 `None`，并行重试**整场禁用**，非旋转错误一路串行。这是合理的保守策略：外部服务（DeepInfra 等）的内部队列本来就观测不到，而且按 token 计费，乱并行只烧钱。

#### 4.4.3 源码精读

**写入端**在 `process_line`（已在 4.1.3 引用过完整函数），核心两行见 [olmocr/pipeline.py:876-880](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L876-L880)：

```python
if match := re.search(r"(?:Waiting|Pending):\s*(\d+)", line):
    global vllm_queued_requests
    last_queue_req = int(match.group(1))
    vllm_queued_requests = last_queue_req
    logger.info(f"vllm running req: {last_running_req} queue req: {last_queue_req}")
```

注意 `global` 声明只在**这个分支**里出现——`process_line` 内的 `nonlocal` 管的是闭包变量，`global` 管的是模块级变量，两个关键字服务于两个作用域体系，混用会直接报语法/逻辑错。

**消费端**见 [olmocr/pipeline.py:343-350](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L343-L350)（上面已引）。补充一个阅读技巧：模块级全局变量是最「松」的协程间通信方式——没有锁、没有类型约束、写入端与消费端可能相隔 1000 行。定位这类变量的完整读写面，用一次全文搜索 `vllm_queued_requests` 即可：定义（L82）、写入（L879）、读取（L345），三点一线，别多别少。这是审读全局状态时的标准动作。

还有一点值得体会：这里**没有**用更「现代」的机制（如 `asyncio.Event` 或共享的指标对象）。原因很实际——`vllm_queued_requests` 的语义就是「最后一次观测值」，一个裸的 int 全局变量恰好是这个语义的最小实现；加 Event 反而要管理置位/清空的时序。

#### 4.4.4 代码实践

**实践目标**：完成一次「全局变量读写面」的源码追踪，并用一个无 GPU 的小实验验证 `None == 0` 初值语义对调度的影响。

**操作步骤**：

1. 在仓库根目录执行（只读操作）：

```bash
grep -n "vllm_queued_requests" olmocr/pipeline.py
```

2. 逐个打开命中的三处（预期：L82 定义、L879 写入、L345 读取），确认没有第四处。
3. 以下为**示例代码**（非项目源码），验证初值语义：

```python
# none_semantics_lab.py —— 验证 None 初值如何屏蔽并行重试（示例代码）
vllm_queued_requests = None   # 对应 pipeline.py:82 的初值

def should_fire_parallel(remaining: bool) -> bool:
    return remaining and vllm_queued_requests == 0  # 对应 pipeline.py:345 的判定

print(should_fire_parallel(True))   # 场景1：还有重试额度，但从未观测过队列
vllm_queued_requests = 5            # 模拟 process_line 抓到 Waiting: 5
print(should_fire_parallel(True))   # 场景2：队列有积压
vllm_queued_requests = 0            # 模拟 process_line 抓到 Waiting: 0
print(should_fire_parallel(True))   # 场景3：队列空
print(should_fire_parallel(False))  # 场景4：队列空但没有剩余额度
```

**需要观察的现象**：四个场景的输出分别是 `False / False / True / False`。

**预期结果**：场景 1 与 2 都为 False——前者因为 `None == 0` 不成立（外部服务器模式永远如此），后者因为队列有积压；只有「队列空 且 还有剩余重试」才触发并行。

4. （源码阅读型延伸）沿着 L345 往下读 15 行，确认并行发射用的是 `asyncio.as_completed`（[L352](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L352)）：谁先成功就先收谁，随后 cancel 掉其余 task——「并行赛跑，先到先得」。

#### 4.4.5 小练习与答案

**练习 1**：为什么写入端要同时维护 `last_queue_req`（nonlocal）和 `vllm_queued_requests`（global）两个变量？

答案：`last_queue_req` 是**日志解析循环内部的**最新观测，与 `last_running_req`、`peak_running_req` 同属一族闭包状态，还用来拼每行的汇总日志；`vllm_queued_requests` 是**跨协程的**广播通道，给 `process_page` 消费。一个面向看护协程自己，一个面向整个进程——职责不同所以两个都留。

**练习 2**：如果 vLLM 某个版本把日志改成 `Running: 12 reqs, Waiting: 34 reqs, GPU KV cache usage: 0.5`，本讲哪个环节会坏？

答案：两个正则都不受影响（它们各自独立匹配，多余字段无害）。真正的脆弱点是措辞整体变更——比如把 `Waiting` 改名为 `Queued` 且不同时保留 `Pending` 写法，这时 `vllm_queued_requests` 停止更新、保持旧值或 None，并行重试静默失效（页面处理不报错，只是永远串行）。这正是日志解析式监控的固有风险：**契约是隐式的，坏掉时不出声**。

**练习 3**：`vllm_queued_requests` 跨协程无锁读写，会有竞态问题吗？

答案：不会有实质性竞态。这是单个 int 的「最后写入者胜」语义，且事件循环内所有读写点都是非抢占的（没有 await 夹在读与用之间——L345 的读取和紧随的决策在同一同步段内完成）。最坏情形是消费端读到「略旧」的观测值（例如刚变 0 还没被读到），这只影响一次重试策略选择，不破坏正确性。

## 5. 综合实践

把三件套串成一个微缩「模拟器」，在**无 GPU 环境**里观察完整的生命周期协作。以下为**示例代码**（非项目源码），综合了 4.1 的日志解析、4.2 的看护循环、4.3 的探活轮询：

```python
# vllm_life_lab.py —— 微缩版 vLLM 生命周期模拟器（示例代码）
import asyncio, random, re

# ── 全局状态（对应 pipeline.py:81-82）────────────────────
vllm_queued_requests = None

# ── 模拟的"vLLM 服务器"：一个会打日志、会死、会被探活的进程 ──
async def fake_vllm_server(die_after: float):
    await asyncio.sleep(0.2)                     # 模拟模型加载
    print("[server] The server is fired up and ready to roll!")
    life = random.uniform(die_after * 0.5, die_after)
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < life:
        await asyncio.sleep(0.3)
        running, waiting = random.randint(0, 10), random.randint(0, 20)
        # 这行日志的形状就是两个正则的解析对象
        print(f"[server] INFO Engine: Running: {running} reqs, Waiting: {waiting} reqs")
        await asyncio.sleep(0.2)
    print("[server] (crashed)")

# ── 对应 process_line 的解析（4.1）─────────────────────────
def parse_line(line: str):
    global vllm_queued_requests
    if match := re.search(r"Running: (\d+)", line):
        pass  # 本模拟器不追踪峰值
    if match := re.search(r"(?:Waiting|Pending):\s*(\d+)", line):
        vllm_queued_requests = int(match.group(1))
        print(f"[parser] queue depth observed: {vllm_queued_requests}")

# ── 对应 vllm_server_host 的看护（4.2）────────────────────
async def host():
    MAX_RETRIES, retry = 3, 0
    while retry < MAX_RETRIES:
        await fake_vllm_server(die_after=1.5)
        print(f"[host] warning: server ended ({retry + 1}/{MAX_RETRIES})")
        retry += 1
    print("[host] exhausted retries, would sys.exit(1) here")

async def main():
    server_task = asyncio.create_task(host())

    # 对应 vllm_server_ready 的探活（4.3）：等"ready"出现
    for attempt in range(1, 11):
        if any("fired up" in str(t) for t in [globals().get("_seen", "")]):
            break
        # 简化：直接轮询一个会被 server 打印的标志
        await asyncio.sleep(0.1)
        if "[server] The server" in (out := ""):
            break
    print("[main] server ready (simulated), workers would start now")

    # 消费端演示（4.4）：模拟一次失败页的并行重试决策
    await asyncio.sleep(1.0)
    remaining = True
    if remaining and vllm_queued_requests == 0:
        print("[worker] queue empty -> fire parallel retries")
    elif vllm_queued_requests is None:
        print("[worker] never observed queue depth -> sequential (external-server behavior)")
    else:
        print(f"[worker] queue busy ({vllm_queued_requests}) -> sequential")

    await server_task

# 重定向 stdout 太繁琐，这里改为直接拦截 print 的简化版解析
async def run():
    real_print = print
    def spy_print(*a, **k):
        s = " ".join(str(x) for x in a)
        parse_line(s) if "[server] INFO" in s else None
        real_print(*a, **k)
    builtins_print = __builtins__["print"] if isinstance(__builtins__, dict) else __builtins__.print
    __builtins__["print"] if isinstance(__builtins__, dict) else None
    globals()["__builtins__"].print = spy_print if not isinstance(globals()["__builtins__"], dict) else globals()["__builtins__"]
    try:
        await main()
    finally:
        if not isinstance(globals()["__builtins__"], dict):
            globals()["__builtins__"].print = builtins_print

asyncio.run(run())
```

> ⚠️ 上面脚本里对 `print` 的拦截写法在不同 Python 版本下并不稳健（`__builtins__` 的形态随模块而异），仅供演示思路。**推荐做法**：删掉 `run()` 与拦截逻辑，改为让 `fake_vllm_server` 在打印日志行的同时直接调用 `parse_line(line)`——一两行的改动，行为等价且无兼容性坑。

**任务清单**：

1. 按上面的推荐做法修正脚本，跑通一遍，观察 `[parser]`/`[host]`/`[worker]` 三类输出如何交错。
2. 把 `die_after` 调大（比如 10 秒），验证看护循环会被 `main` 的 `await server_task` 一直阻塞——「服务器活着时 host 永不返回」。
3. 回答：这个模拟器里，`[worker]` 的决策分支分别对应真实代码里 `vllm_queued_requests` 的哪三种取值（None / 正数 / 0）？
4. **交付物**：把你修正后的脚本与 4.1.4 的参数注释表放在一起，就是本讲的完整学习档案。

**预期结果**（第 3 问答案）：None → 从未观测（外部服务器或刚启动）；正数 → 队列有积压，串行；0 → 队列空，并行发射剩余重试。

## 6. 本讲小结

- **三件套分工**：`vllm_server_task` 管单个服务器实例的一生（拼命令、起子进程、逐行解析日志、优雅退出）；`vllm_server_host` 是外层看护循环，崩溃自动重启、5 次耗尽后 `sys.exit(1)`；`vllm_server_ready` 是启动期 HTTP 探活（`GET /models` 轮询到 200，默认上限 600 次 × 1 秒）。
- **命令拼装**：固定参数 + 两个条件参数（`--gpu-memory-utilization` 默认不传、`--max-model-len` 默认恒传 16384）+ `parse_known_args` 剩余参数原样透传给 `vllm serve`；`--limit-mm-per-prompt '{"video": 0}'` 关闭用不上的 video 编码通路，把显存让给 KV cache。
- **日志即指标**：`--disable-log-requests` 与 `--uvicorn-log-level warning` 降噪后，两个正则从引擎状态行抠出 Running（在途）与 Waiting/Pending（排队）两个深度；后者写入全局变量 `vllm_queued_requests`。
- **指标反哺调度**：`process_page` 在非旋转错误重试时读取该全局变量——队列为 0 才并行发射剩余重试；初值 `None` 意味着外部服务器模式（无日志可解析）下并行重试整场禁用，保守且合理。
- **可重启的底气来自无状态**：队列与进度全在 workspace（done flag + worker 锁），服务器崩了只是抖动，worker 靠连接退避扛过窗口期；重启便宜，所以上限只设 5。
- **化石代码警训**：`server_printed_ready_message` 只写不读、`max_server_ready_timeout` 名为秒实为次数——读源码时要对「名字与行为错位」「残留状态位」保持警觉，用全文搜索验证读写面。

## 7. 下一步学习建议

- **下一讲 [u3-l4 Beaker 集群执行](u3-l4-beaker-cluster-execution.md)**：多个 GPU 副本各自管理自己的 vLLM 服务器时如何错峰下载模型、如何剥离参数——本讲的三件套会被多副本放大。
- **回看 [u2-l6 单页重试](u2-l6-page-retry-and-rotation.md) 与 [u2-l7 异步 HTTP](u2-l7-async-http-client.md)**：把「服务器侧生命周期」与「客户端侧重试/退避」拼成完整闭环，试着不查资料画出从 `process_page` 发起请求到 `process_line` 更新指标的完整时序图。
- **延伸阅读**：在装了 `olmocr[gpu]` 的机器上运行 `vllm serve --help`，对照 4.1.3 的参数表看 vLLM 还暴露了哪些与显存/并行相关的旋钮（如 `--swap-space`、`--enable-prefix-caching`），思考它们对 OCR 批处理各有什么利弊。
- **动手验证（可选，需 GPU）**：跑一次真实 pipeline，用 `--disk_logging` 落盘，然后在日志里 grep `queue req:` 画一条队列深度曲线，观察它何时归零、归零时是否伴随 `firing N parallel retries` 日志。**待本地验证**。

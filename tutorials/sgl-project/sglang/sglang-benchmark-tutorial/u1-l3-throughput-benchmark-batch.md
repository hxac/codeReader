# 第一次吞吐基准：合成数据的批量请求

## 1. 本讲目标

学完本讲，你应当能够：

- 理解 `benchmark/benchmark_batch/benchmark_batch.py` 这份「最小吞吐基准」的设计思路：用**合成的随机 token**当 prompt，去压测一个运行中的 SGLang 服务。
- 看懂它的完整执行链路：**多进程预生成随机 prompt → 用 `RuntimeEndpoint` 连接服务 → 向 `/generate` 端点分批发请求 → 用墙钟时间统计延迟与吞吐**。
- 区分「prefill（前缀/提示词处理）负载」与「decode（解码生成）负载」，并能通过改 `NUM_TOKENS` / `GEN_TOKENS` 两个旋钮把基准从一种负载切到另一种。
- 知道 `benchmark_tokenizer.py` 如何衡量分词/去分词的「单条 vs 批量」开销，理解分词预处理为何要并行化。
- 牢记一个关键区别：**本脚本是「顺序」压测**（请求一个接一个发），它衡量的是「连续批」的延迟与吞吐，而不是「并发饱和」下的极限吞吐。后者要等到 u5-l1 的 `bench_serving`。

承接前两讲：u1-l1 建立了「benchmark/ 下的脚本是**客户端**，去驱动一个由 `launch_server` 启动的**服务端**」的心智模型；u1-l2 讲了**精度评测**的标准模板（关心「答对率」）。本讲转向另一类——**吞吐基准**（关心「快不快、每秒能处理多少」），并给出一个不含任何真实数据集、完全靠随机文本构造的最小例子。

## 2. 前置知识

- **吞吐（throughput）**：单位时间内完成的工作量。对推理服务，常见单位是 `prompts/s`（每秒处理多少条请求）、`tokens/s`（每秒处理多少 token）。本脚本的默认单位是 `prompts/s`。
- **延迟（latency）**：单个请求从发出到收到完整响应所耗的时间，常用毫秒（ms）。本脚本用 `time.perf_counter()` 这种高精度墙钟来测。
- **prefill 与 decode**：LLM 推理分两个阶段。**prefill** 是「读 prompt」，把整段提示词一次性算出 KV cache，计算密集、可高度并行；**decode** 是「逐个吐 token」，每步只生成一个 token，访存密集。一个请求的总耗时大致是「prefill 越长越慢 + decode 越多越慢」。
- **合成数据（synthetic data）**：不依赖真实语料，直接用随机数生成输入。好处是可控、可复现、想造多大就造多大；缺点是没有语义。吞吐/延迟基准只关心「算力被吃满」，所以随机文本完全够用。
- **`ProcessPoolExecutor`**：Python 标准库的多进程池。用一个进程池把「生成 N 条 prompt」这件 CPU 密集的事分摊到多个 CPU 核心上并行做。
- **`RuntimeEndpoint`**：SGLang 前端里「连接到运行中服务」的后端对象，它把 `http://127.0.0.1:30000` 这样一个 URL 封装成一个可发请求的客户端（u1-l2 也用到它）。

一个直觉性结论先放这里：**吞吐基准不需要聪明的内容，只需要「确定大小」的内容**。所以这份脚本的核心是四个常量——`NUM_REQUESTS`（发几批）、`BATCH_SIZE`（每批几条 prompt）、`NUM_TOKENS`（每条 prompt 多少 token）、`GEN_TOKENS`（每条 prompt 生成几个 token）。改这四个数，就能把服务从「纯 prefill 压力」切换到「重 decode 压力」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [benchmark/benchmark_batch/benchmark_batch.py](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py) | 本讲主角：一份最小吞吐基准。多进程生成随机 prompt，分批打 `/generate`，统计延迟与吞吐。 |
| [benchmark/benchmark_batch/benchmark_tokenizer.py](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_tokenizer.py) | 配套脚本：衡量分词器 `encode`/`decode` 的「逐条 vs 批量」开销与加速比。 |
| [python/sglang/lang/backend/runtime_endpoint.py](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/python/sglang/lang/backend/runtime_endpoint.py) | `RuntimeEndpoint` 后端：构造时会先打 `/get_model_info` 做健康检查，并保存 `base_url` 供后续请求使用。 |

> 说明：本目录没有 README，运行方式直接看脚本顶部的 `CONFIG` 注释与 `main()`。所有压测都需要**先起一个 SGLang 服务**（例如 `python -m sglang.launch_server --model <模型> --port 30000`），再运行本脚本。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块。**4.1** 给出整体流程与配置旋钮；**4.2 / 4.3 / 4.4** 严格对应任务要求的四个最小模块（预生成、`RuntimeEndpoint`、`/generate` 请求、延迟与吞吐统计）；**4.5** 用 `benchmark_tokenizer.py` 补上「分词预处理并行化」这一环节。

主流程的伪代码（对应 `main()` 的执行顺序）：

```
endpoint = RuntimeEndpoint(ENDPOINT_URL)            # 连服务（含健康检查）
batched_prompts = prepare_all_prompts(...)          # 多进程生成随机 prompt，分批
for each batch in batched_prompts:                  # 顺序、一个一个发
    latency = send_batch_request(endpoint, batch)   # 打 /generate，记墙钟
results, total_latency = run_benchmark(...)
process_results(results, total_latency)             # 算平均延迟与吞吐，打印
```

注意上面那个 `for` 是**顺序**的：第 2 个请求要等第 1 个返回才发。这正是本脚本与 u5-l1 并发压测的本质区别。

### 4.1 配置旋钮与整体流程

#### 4.1.1 概念说明

整份脚本的可调参数都写死在顶部的 `CONFIG` 区。理解这四个常量，就理解了「这个基准到底在压什么」：

| 常量 | 含义 | 默认值 | 增大它的效果 |
| --- | --- | --- | --- |
| `NUM_REQUESTS` | 发多少批请求 | 10 | 总数据量更大，统计更稳 |
| `NUM_TOKENS` | 每条 prompt 含多少 token | 32000 | prefill 更重 |
| `BATCH_SIZE` | 每批发几条 prompt | 8 | 单批更大，prefill 并行度更高 |
| `GEN_TOKENS` | 每条 prompt 生成多少 token | 0 | decode 更重（默认 0 = 纯 prefill） |

默认配置 `NUM_TOKENS=32000, GEN_TOKENS=0` 是一个**纯 prefill** 负载：每批 8 条、每条 3.2 万 token 的随机文本，生成 0 个 token——专门压服务的「读长 prompt」能力。

#### 4.1.2 核心流程

```
1. RuntimeEndpoint 连服务（顺带健康检查）
2. 多进程生成 NUM_REQUESTS × BATCH_SIZE 条随机 prompt，切成 NUM_REQUESTS 批
3. 顺序遍历每一批，打 /generate，记录每批墙钟耗时
4. 汇总：平均每批延迟、平均每条 prompt 延迟、吞吐(prompts/s)
```

每批的「理论工作量」可以粗略写成：

\[
\text{work per batch} \;\approx\; \underbrace{\text{NUM\_TOKENS} \times \text{BATCH\_SIZE}}_{\text{prefill 输入}} \;+\; \underbrace{\text{GEN\_TOKENS} \times \text{BATCH\_SIZE}}_{\text{decode 输出}}
\]

把 `NUM_TOKENS` 调大、`GEN_TOKENS` 设 0，work 几乎全是 prefill；反过来把 `NUM_TOKENS` 调小、`GEN_TOKENS` 调大，work 就变成以 decode 为主。

#### 4.1.3 源码精读

配置区集中在脚本顶部：

[benchmark_batch/benchmark_batch.py:L17-L24](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py#L17-L24) —— 定义服务地址、本地分词器路径，以及四个基准常量 `NUM_REQUESTS / NUM_TOKENS / BATCH_SIZE / GEN_TOKENS`。注意 `GEN_TOKENS = 0`，说明默认只压 prefill。

`main()` 把上面四步串起来：

[benchmark_batch/benchmark_batch.py:L167-L189](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py#L167-L189) —— 依次 `RuntimeEndpoint(ENDPOINT_URL)` 连服务、`prepare_all_prompts(...)` 造数据、`run_benchmark(...)` 顺序发请求、`process_results(...)` 打印结果。第 177 行 `endpoint.flush_cache()` 被注释掉了——若想每批之间清空 KV cache（避免缓存命中干扰），可以打开它。

#### 4.1.4 代码实践

1. **目标**：在不运行的情况下，预测两个配置分别压的是什么。
2. **步骤**：阅读 `CONFIG` 区；写出两组配置：(A) `NUM_TOKENS=32000, GEN_TOKENS=0`；(B) `NUM_TOKENS=64, GEN_TOKENS=512`。
3. **需要观察的现象**：用上面的 work 公式估算 A、B 两组的 prefill / decode 工作量比例。
4. **预期结果**：A 组几乎 100% prefill；B 组以 decode 为主。
5. **结论标注**：本步为「源码阅读型实践」，无需运行，结论可对照 4.4 的实际吞吐验证。

#### 4.1.5 小练习与答案

**Q1**：把 `BATCH_SIZE` 从 8 调到 1，其它不变，每条 prompt 的平均延迟会变大还是变小？为什么？
**答**：单看一条 prompt 的纯计算量没变，但批变小后 GPU 并行度下降、每 token 的有效算力利用率降低，因此「平均每条 prompt 延迟」通常会**变大**（批次越大，单条分摊的固定开销越低）。

**Q2**：默认 `GEN_TOKENS=0` 时，服务端到底在做什么？
**答**：服务端仍要做 prefill——把 8 条各 3.2 万 token 的随机 prompt 一次性算出 KV cache，只是生成阶段长度为 0，不吐任何 token。所以测到的是纯 prefill 耗时。

### 4.2 ProcessPoolExecutor 预生成随机 prompt

#### 4.2.1 概念说明

为什么要「预生成」？因为 `NUM_TOKENS=32000` 的随机文本要先做 `tokenizer.decode` 把随机 token id 翻成字符串，这一步本身是 CPU 密集的——10 批 × 8 条 = 80 条、每条 3.2 万 token，串行做会很慢，且这部分耗时**不属于被测的服务性能**，必须提前算好、排除在计时之外。

脚本用标准库的 `ProcessPoolExecutor` 把这 80 条 prompt 的生成**并行**分摊到多个 CPU 核心上。注意是「多进程」而非「多线程」：`tokenizer.decode` 受 GIL 限制，多线程无法真正并行，所以必须用进程池。

#### 4.2.2 核心流程

```
total_prompts = NUM_REQUESTS × BATCH_SIZE
max_workers = min(cpu_count, total_prompts)
开一个进程池:
  对每条 prompt，submit(generate_random_prompt, i, ...)
  as_completed 收集结果，按原始 index 回填到 all_prompts[index]
把 all_prompts 切成 NUM_REQUESTS 个 BATCH_SIZE 大小的批
```

`generate_random_prompt` 单条逻辑：从词表里**均匀随机**抽 `num_tokens` 个 token id，`decode` 成文本，再前缀一句 `"Prompt {i}: "`。

#### 4.2.3 源码精读

单条 prompt 生成：

[benchmark_batch/benchmark_batch.py:L30-L40](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py#L30-L40) —— `random.randint(0, vocab_size-1)` 抽随机 token id（第 36 行），`tokenizer.decode(...)` 翻成字符串（第 37 行）。注意每条 prompt 内部都新建一个 tokenizer（第 32 行），因为这是在子进程里跑、不能共享父进程对象。

并行调度与切片：

[benchmark_batch/benchmark_batch.py:L43-L69](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py#L43-L69) —— `max_workers = min(os.cpu_count(), total_prompts)`（第 47 行）决定开几个进程；`ProcessPoolExecutor`（第 49 行）提交 `total_prompts` 个任务；`as_completed` 收结果时用 `futures.index(future)` 把结果按原始下标回填（第 59-60 行），保证顺序；最后第 62-64 行切成 `num_requests` 个批。

> 一个值得注意的细节：第 59 行 `futures.index(future)` 是 O(n) 查找，在任务量大时是性能瓶颈，但这里 `total_prompts` 最多几百，可忽略。

#### 4.2.4 代码实践

1. **目标**：感受「预生成」开销与并行加速。
2. **步骤**：把 `NUM_TOKENS` 临时改成一个较大值（如 32000 保持默认），观察脚本启动后 `Generating prompts` 进度条花费的时间；再把 `max_workers` 临时改成 `1`（强制单进程），对比同一数据量下的生成耗时。
3. **需要观察的现象**：单进程时 `Generating prompts` 明显更慢；默认多进程时该阶段被并行吃掉。
4. **预期结果**：多进程生成耗时约为单进程的 `1/min(cpu_count, total_prompts)`。
5. **标注**：待本地验证（具体加速比取决于机器 CPU 核数）。

#### 4.2.5 小练习与答案

**Q1**：为什么用 `ProcessPoolExecutor` 而不是 `ThreadPoolExecutor`？
**答**：`tokenizer.decode` 是 CPU 密集且受 Python GIL 约束，多线程无法真正并行；多进程绕开 GIL，才能真正利用多核。

**Q2**：结果回填为什么用 `all_prompts[index] = future.result()` 而不是 `append`？
**答**：`as_completed` 的完成顺序是随机的（谁先算完谁先返回），直接 `append` 会打乱 prompt 的原始编号；按下标回填能保持 `Prompt 0, 1, 2, …` 的顺序。

### 4.3 RuntimeEndpoint 与 /generate 批量请求

#### 4.3.1 概念说明

`RuntimeEndpoint` 是 SGLang 前端里「连接到一个正在跑的 SGLang 服务」的后端（u1-l2 的精度评测也用它）。它的关键职责有两件：构造时先打 `/get_model_info` 做**健康检查**（服务没起或地址错了，这里就会失败）；把传入的 URL 存为 `base_url`，之后所有请求都拼在它后面。

本脚本没有用 SGL 程序 + `run_batch` 那套高阶 API（那是 u1-l2 的精度评测路径），而是**直接用 `requests.post` 打 HTTP**——因为吞吐基准要把计时粒度完全攥在自己手里，从「请求发出」到「响应返回」逐毫秒记录。

#### 4.3.2 核心流程

```
sampling_params = {max_new_tokens, temperature=0.7, stop="\n"}
data = {"text": [prompt1, prompt2, ...], "sampling_params": sampling_params}
start = perf_counter()
response = POST(base_url + "/generate", json=data, timeout=3600)
elapsed = (perf_counter() - start) * 1000   # ms
avg_per_prompt = elapsed / len(prompts)
```

注意 `/generate` 的 `text` 字段是一个**列表**——一次请求就送进一整批 `BATCH_SIZE` 条 prompt，服务端会把它们当成一个 batch 一起算。`stop="\n"` 表示遇到换行就停（随机文本里几乎不会有换行，所以对纯 prefill 负载影响不大）。

#### 4.3.3 源码精读

构造采样参数与请求体：

[benchmark_batch/benchmark_batch.py:L75-L82](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py#L75-L82) —— `sampling_params` 含 `max_new_tokens=gen_tokens`、`temperature=0.7`、`stop="\n"`；`data = {"text": prompts, "sampling_params": ...}`，`prompts` 是一个长度为 `BATCH_SIZE` 的列表，整批一起送。

计时与发请求：

[benchmark_batch/benchmark_batch.py:L84-L95](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py#L84-L95) —— `start_time = time.perf_counter()`（第 84 行）在发请求**之前**记录；`requests.post(endpoint.base_url + "/generate", json=data, timeout=3600)`（第 86-88 行）真正打 HTTP，超时给到 1 小时（长 prompt prefill 可能很慢）；`elapsed_time = (perf_counter() - start_time) * 1000`（第 93 行）换算成毫秒；`avg_per_prompt = elapsed_time / len(prompts)`（第 94 行）算出本批「平均每条 prompt 延迟」。

`RuntimeEndpoint` 的健康检查与 `base_url` 保存：

[python/sglang/lang/backend/runtime_endpoint.py:L26-L47](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/python/sglang/lang/backend/runtime_endpoint.py#L26-L47) —— 构造函数把 `base_url`（第 37 行）保存下来，并立即打 `/get_model_info`（第 41-45 行）确认服务在线；若失败由 `_assert_success` 抛错。这正是本脚本第 169 行 `RuntimeEndpoint(ENDPOINT_URL)` 一启动就会去连通服务的原因。

#### 4.3.4 代码实践

1. **目标**：确认「整批一次送」与「健康检查」两件事。
2. **步骤**：起一个本地服务（`python -m sglang.launch_server --model <小模型> --port 30000`）；用 `curl` 手动模仿本脚本的请求体：`curl -X POST http://127.0.0.1:30000/generate -H 'Content-Type: application/json' -d '{"text":["hello","world"],"sampling_params":{"max_new_tokens":4,"temperature":0.7}}'`。
3. **需要观察的现象**：返回的 JSON 里每条 prompt 都有一段生成文本（`text` 字段是列表，对应两条输入）。
4. **预期结果**：服务对一个含 2 条 `text` 的请求返回 2 段输出，证明 `/generate` 原生支持批。
5. **标注**：待本地验证（需先起服务）。

#### 4.3.5 小练习与答案

**Q1**：为什么把计时点设在 `requests.post` **之前**，而不是 `RuntimeEndpoint` 构造时？
**答**：`RuntimeEndpoint` 构造只连一次服务、做健康检查，不属于「单次请求」耗时；吞吐基准要测的是「每批请求的往返时间」，所以计时必须紧贴 `post` 前后。

**Q2**：把 `text` 从列表改成单条字符串会发生什么？
**答**：服务端 `/generate` 既接受单字符串也接受列表；改成单字符串后每次只送 1 条 prompt，`BATCH_SIZE` 就失去意义，压不出「批并行」的吞吐。

### 4.4 延迟与吞吐统计

#### 4.4.1 概念说明

跑完所有批后，脚本统计四类数字：

- **平均每批请求延迟**（`avg_request_latency`）：所有成功批的 `elapsed_time` 取平均。
- **平均每条 prompt 延迟**（`avg_per_prompt_latency`）：所有成功批的「本批耗时 / 批大小」取平均。
- **总墙钟延迟**（`total_latency`）：`run_benchmark` 用一个 `perf_counter` 包住整个循环得到，包含顺序执行的所有批。
- **吞吐**（`throughput`）：成功 prompt 总数 ÷ 所有成功批耗时之和，单位 `prompts/s`。

吞吐公式：

\[
\text{throughput} \;=\; \frac{\text{total\_prompts}}{\sum_{i\in\text{成功批}} \text{latency}_i / 1000} \quad [\text{prompts/s}]
\]

因为请求是**顺序**发的，分母里「各批耗时之和」≈ 总墙钟，所以这里的吞吐其实就是「连续发批」的稳态吞吐。

#### 4.4.2 核心流程

```
遍历 results:
  成功批 → 累加 total_prompts、收集 request_latencies / per_prompt_latencies、total_time += elapsed/1000
  失败批 → failed_requests += 1
avg_request_latency  = mean(request_latencies)
avg_per_prompt_latency = mean(per_prompt_latencies)
throughput = total_prompts / total_time
打印汇总表
```

#### 4.4.3 源码精读

`run_benchmark` 的顺序循环与总延迟：

[benchmark_batch/benchmark_batch.py:L101-L124](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py#L101-L124) —— 第 109 行的 `for i, batch_prompts in enumerate(batched_prompts)` 是**顺序**遍历（没有线程/进程并发），第 118 行 `send_batch_request(...)` 同步等待返回才进下一批；`benchmark_start_time`（第 107 行）和 `total_latency`（第 122 行）用一对 `perf_counter` 包住整个循环。

`process_results` 的汇总与吞吐计算：

[benchmark_batch/benchmark_batch.py:L130-L161](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py#L130-L161) —— 第 139 行解包每批的五元组 `(request_id, elapsed_time, avg_per_prompt, success, batch_size)`；成功批累加 `total_prompts`（第 142 行）和 `total_time`（第 145 行，把毫秒换算回秒）；第 149-150 行用 `statistics.mean` 求平均延迟；第 151 行 `throughput = total_prompts / total_time` 算吞吐；第 153-161 行打印汇总表，含 `Throughput: ... prompts/second`。

> 注意：第 151 行的吞吐用 `total_time`（各成功批耗时之和）做分母，**不是** `total_latency`。在顺序执行下两者近似相等，但语义不同：`total_time` 只统计成功批，`total_latency` 是包含任何间隙的总墙钟。

#### 4.4.4 代码实践

1. **目标**：亲手算一遍吞吐，验证对公式的理解。
2. **步骤**：运行一次基准后，从汇总表读出 `Total prompts sent`、`Avg per request latency`、`Successful requests`；用 `throughput = total_prompts / (avg_request_latency × successful_requests / 1000)` 手算，再与脚本打印的 `Throughput` 对照。
3. **需要观察的现象**：手算值与打印值基本一致（顺序执行下 `Σlatency ≈ avg × count`）。
4. **预期结果**：两者误差应在舍入范围内。
5. **标注**：待本地验证。

#### 4.4.5 小练习与答案

**Q1**：若有几批请求失败，吞吐公式会不会被拉高（虚高）？
**答**：不会虚高。失败批的 `elapsed_time` 记为 0 且不计入 `total_time`/`total_prompts`（第 96-98 行返回 0、第 140-145 行只统计成功批），所以失败批既不贡献分子也不贡献分母，吞吐反映的是「成功批」的稳态速率。但要注意：失败批的墙钟间隙不在 `total_time` 里，所以吞吐对失败是「宽容」的。

**Q2**：为什么说本脚本的吞吐是「连续批吞吐」而非「并发饱和吞吐」？
**答**：因为请求顺序发送，任一时刻服务里最多只有 1 个在途 batch，无法把服务「填满」；要测饱和吞吐，需要像 u5-l1 的 `bench_serving` 那样并发发请求。

### 4.5 分词预处理的并行与批量：benchmark_tokenizer.py

#### 4.5.1 概念说明

吞吐基准里，「分词」是常被忽略却很贵的一环：把 prompt 文本变成 token id（`encode`）或反过来（`decode`）。`benchmark_batch.py` 把这步**提前**到多进程里做，避免它污染计时。`benchmark_tokenizer.py` 则专门用来量化：**逐条处理 vs 批量处理**到底差多少。

HuggingFace 分词器支持两种调用：逐条 `tokenizer.encode(p)`（循环 N 次）和批量 `tokenizer(batch)`（一次传一个列表）。批量版本底层用 Rust 实现、可并行，通常快很多。脚本还对比了 `tokenizer.decode` 逐条 vs `tokenizer.batch_decode`，并模拟了 SGLang `DetokenizerManager` 的真实参数（`skip_special_tokens=True, spaces_between_special_tokens=True`）。

#### 4.5.2 核心流程

```
随机生成 max_batch_size 条、每条 num_tokens 个 token id
对每个 batch_size in [1,2,4,8]:
  对每个 function in [encode, decode]:
    single: 逐条调用 sequential_fn，跑 num_runs 次取平均
    batch:  一次调用 batch_fn，    跑 num_runs 次取平均
    speedup = avg_sequential_ms / avg_batch_ms
打印对比表
```

#### 4.5.3 源码精读

入口与逐条/批量两种函数：

[benchmark_batch/benchmark_tokenizer.py:L31-L62](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_tokenizer.py#L31-L62) —— `encode` 的 `sequential_fn` 是列表推导 `[tokenizer.encode(p) for p in batch]`（第 39 行），`batch_fn` 是 `tokenizer(batch)`（第 40 行）；`decode` 分支（第 46-62 行）的 `sequential_fn` 逐条 `tokenizer.decode`，`batch_fn` 用 `tokenizer.batch_decode`，并用 `decode_kwargs`（第 48-51 行）复刻 DetokenizerManager 的真实去分词参数。

计时与加速比：

[benchmark_batch/benchmark_tokenizer.py:L86-L116](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_tokenizer.py#L86-L116) —— `measure_times` 包跑 `num_runs` 次；第 109-114 行 `speedup_factor = avg_sequential_ms / avg_batch_ms` 算出「批量相对逐条」的加速倍数。

计时原语 `measure_times`：

[benchmark_batch/benchmark_tokenizer.py:L170-L176](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_tokenizer.py#L170-L176) —— 同样用 `time.perf_counter()`，跑 `num_runs` 次取每次毫秒耗时，最后取平均（第 175 行换算成 ms）。注意它把每次结果都存下来（第 174 行），`benchmark` 再用 `mean` 聚合，这是「多次采样取平均」的标准做法。

命令行参数：

[benchmark_batch/benchmark_tokenizer.py:L188-L232](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_tokenizer.py#L188-L232) —— `--tokenizer`（必填，分词器名/路径）、`--function`（`encode`/`decode`，默认都跑）、`--num-tokens`（默认 20000）、`--batch-sizes`（默认 `1 2 4 8`）、`--batch-mode`（`single`/`batch`）、`--num-runs`（默认 5）。

#### 4.5.4 代码实践

1. **目标**：量化「批量 vs 逐条」的加速比，理解为什么 `benchmark_batch.py` 要把分词并行化。
2. **步骤**：运行 `python benchmark_tokenizer.py --tokenizer <本地分词器> --num-tokens 20000 --batch-sizes 1 4 8 --num-runs 5`（本机有分词器即可，**不需要起服务**）。
3. **需要观察的现象**：输出的 SUMMARY 表里，`Batch (ms)` 明显小于 `Sequential (ms)`，`Speedup` 列通常 >1（常达数倍）。
4. **预期结果**：随着 `batch_size` 增大，批量路径的 Rust 并行优势更明显，加速比升高。
5. **标注**：待本地验证（加速比取决于分词器实现与 CPU）。

#### 4.5.5 小练习与答案

**Q1**：为什么 `decode` 分支要专门设 `skip_special_tokens=True, spaces_between_special_tokens=True`？
**答**：这两个参数是 SGLang `DetokenizerManager` 在真实推理时去分词的默认设置（脚本注释第 47 行写明「mimic DetokenizerManager's usual case」）；用真实参数测，得到的耗时才有代表性。

**Q2**：`num_runs=5` 为什么要跑 5 次取平均，而不是跑 1 次？
**答**：单次计时受系统抖动（GC、调度、缓存）影响大；多次采样取平均能压低噪声，得到更稳的延迟估计。这是所有微基准的通用范式（u7-l1 的内核微基准会进一步加 warmup/iters）。

## 5. 综合实践

把 4.1 的「配置旋钮」与 4.4 的「吞吐统计」串起来，亲手做一次 prefill vs decode 的对比压测。

**任务**：修改 `benchmark_batch.py` 顶部的 `CONFIG`，分别跑两种负载，记录并解释吞吐差异。

1. **准备**：起一个本地 SGLang 服务（`python -m sglang.launch_server --model <模型> --port 30000`），把 `TOKENIZER_DIR` 指向该模型对应的本地分词器路径。
2. **负载 A（长 prompt、零生成，纯 prefill）**：设 `NUM_TOKENS=32000, BATCH_SIZE=8, GEN_TOKENS=0, NUM_REQUESTS=10`，运行脚本，记录 `Avg per request latency` 与 `Throughput`。
3. **负载 B（短 prompt、多生成，重 decode）**：设 `NUM_TOKENS=64, BATCH_SIZE=8, GEN_TOKENS=512, NUM_REQUESTS=10`，再次运行，记录同样两项。
4. **对比与分析**：
   - 画出一张两行的小表：`(Avg per request latency, Throughput)` × `(负载A, 负载B)`。
   - 用 4.1 的 work 公式解释：为什么 A 的单批延迟主要由 prefill 决定、B 的主要由 decode 决定。
   - 解释为什么两种负载的 `Throughput(prompts/s)` 数值差异巨大（提示：A 每条 prompt 要算 3.2 万 token 的 prefill，B 每条只要算 64 token prefill + 512 步串行 decode；分母是「每批总耗时」）。
5. **进阶（可选）**：把 `BATCH_SIZE` 在负载 A 下分别设为 1、4、8，观察「平均每条 prompt 延迟」是否随批变大而下降（验证 4.1.5 Q1 的结论）。
6. **标注**：具体数值**待本地验证**，取决于模型大小、GPU、是否命中缓存；重点是**相对趋势**与**解释**，而非绝对数字。

## 6. 本讲小结

- `benchmark_batch.py` 是一份**最小吞吐基准**：用随机 token 当 prompt，纯靠四个常量（`NUM_REQUESTS / NUM_TOKENS / BATCH_SIZE / GEN_TOKENS`）控制负载形状。
- 数据准备用 `ProcessPoolExecutor` **多进程并行**预生成随机 prompt，把昂贵的 `tokenizer.decode` 排除在计时之外（4.2）。
- 客户端经 `RuntimeEndpoint`（构造时打 `/get_model_info` 做健康检查）连服务，再用 `requests.post` 直接打 `/generate`，`text` 字段一次送一整批（4.3）。
- 计时紧贴 `requests.post` 前后，用 `time.perf_counter()`；吞吐 = 成功 prompt 数 ÷ 各成功批耗时之和（4.4）。
- 关键区别：本脚本是**顺序**压测（一个 batch 接一个 batch），衡量「连续批」的延迟/吞吐，**不是**并发饱和吞吐——后者是 u5-l1 `bench_serving` 的主题。
- `benchmark_tokenizer.py` 量化分词器「逐条 vs 批量」的开销与加速比，解释了为什么吞吐基准要把分词并行化/批量化（4.5）。

## 7. 下一步学习建议

- **本讲只压了「顺序连续批」**。若想知道服务在**并发**请求下的 TTFT/TPOT/ITL 与饱和吞吐，请进入 **u5-l1「bench_serving 框架与核心服务指标」**——它用 `get_request` 按 Poisson/常数速率并发发请求，并用 `calculate_metrics` 算出工业级的服务指标。
- 若对**精度评测**（答对率）那条线感兴趣，回顾 u1-l2，并继续 u2「精度评测基准集」（hellaswag/gsm8k/multimodal 等）。
- 若想看 SGLang 服务本身怎么起、`/generate` 端点背后做了什么，可阅读 `python/sglang/launch_server.py` 与 `tokenizer_manager`。
- 本讲的「随机文本压测」思路在内核层也会复现：u7-l1 起的内核微基准同样用合成输入，但计时范式更严格（warmup/iters/barrier/`cuda.synchronize`），值得对照学习。

# executor RPC 服务

## 1. 本讲目标

前几讲我们接触的 BabelDOC 入口有两种：终端用的 `babeldoc` 命令（`main.cli`），以及给下游程序直接调用的库 API `high_level.async_translate`。两者有一个共同前提——**调用方和翻译代码跑在同一个 Python 进程里**。但真实部署里，翻译一份 PDF 往往要拉起 ONNX 模型、反复请求 LLM、占用上 GB 内存，一旦崩溃会把宿主进程一起带走。于是 BabelDOC 提供了第三个、也是隔离级别最高的入口：`babeldoc/tools/executor`，一个**独立的翻译任务 RPC 服务**。

本讲带你钻进这个 sidecar（边车）服务的四个源码文件，学完后你应该能够：

1. 说清 `ExecutorServer` 暴露的 HTTP 端点（`/v1/executions`、`/v1/executions/{id}/events`、`/v1/abort`、`/healthz` 以及水印端点），以及它们各自的方法与状态码。
2. 解释 `ExecutionStore` 的「**单活跃任务**」语义：为什么同一时刻只允许一个任务在跑、`busy` 时返回 `409 CONFLICT`，以及任务如何提交与中止。
3. 读懂 **NDJSON（换行分隔 JSON）事件流**协议：`EventEnvelope` 的四个字段、单调递增的 `sequence`、`after_sequence` 断点续传与回放（replay）。
4. 理解 `MultiprocessExecutionRunner` 如何用 `forkserver` 在**独立进程**里跑翻译、用两根 `Pipe` 做父子进程双向通信、并在缺失终态事件时兜底。

本讲的实践任务是：启动 executor 服务、用一个最小请求提交一个任务、观察返回的 NDJSON 事件流，并说明它与 u8-l3 讲过的 `high_level.async_translate` 事件协议的**异同**。

---

## 2. 前置知识

本讲承接 u8-l3 建立的「同步内核 + 异步外壳」心智模型。为照顾从零开始的读者，先补四个概念：

- **RPC（Remote Procedure Call，远程过程调用）**：让一个程序能像调本地函数一样，调用「跑在另一个进程/另一台机器上」的服务。本讲的 RPC 走的是最朴素的 **HTTP**：客户端发 `POST`/`GET` 请求，服务端回 JSON 或流。不依赖任何 RPC 框架，只用 Python 标准库的 `http.server`。
- **sidecar（边车）**：与主程序「并排」运行的辅助进程。主程序（比如一个 Web 后端）把耗资源、易崩溃的「翻译 PDF」工作外包给 executor 这个 sidecar，主程序自己只负责调度与展示。两者通过 HTTP 通信，互不拖垮。
- **NDJSON（Newline-Delimited JSON）**：一种流式文本格式，**每行一个独立的 JSON 对象，行与行之间用换行符 `\n` 分隔**。和「一个大 JSON 数组」相比，它的好处是**边产生边发送**——服务端不必等全部事件凑齐就能写出第一行，客户端也能逐行解析。HTTP 响应头里用 `Content-Type: application/x-ndjson` 标识。
- **多进程隔离**：Python 的 `multiprocessing` 可以创建一个**全新的操作系统进程**来跑任务。子进程崩溃（段错误、OOM、死循环）只会死自己，父进程还活着并能善后。这比「开一个线程」要安全得多——线程崩了往往连累整个解释器。

还需要记住两个 BabelDOC 事实（来自 u8-l3）：

- `high_level.async_translate` 是一个 `async` 生成器，它把同步的 `do_translate` 丢进线程池，把进度回调桥接成事件流，`yield` 出 `progress_start` / `progress_update` / `progress_end` / `finish` / `error` 五种字典事件。它是**同进程**的。
- `TranslationConfig.cancel_translation()` 是翻译主链路响应取消的统一入口。

本讲的核心问题就是：**如何把「同进程的 async 事件流」升级成「跨进程、跨网络、可中断、可续传的 RPC 事件流」，同时用独立进程把翻译的崩溃风险隔离掉？**

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `babeldoc/tools/executor/server.py` | HTTP 服务本体。`ExecutorServer`/`ExecutorHandler` 定义全部端点，`serve()`/`main()` 负责启动。是 RPC 的「前台」。 |
| `babeldoc/tools/executor/state.py` | `ExecutionStore`：任务的「账本与调度器」。负责创建/中止任务、单活跃约束、事件序号分配、事件回放与流式输出。是连接前台与执行器的中枢。 |
| `babeldoc/tools/executor/runner.py` | 执行器抽象。`MultiprocessExecutionRunner` 在独立进程里跑任务并桥接父子进程通信；`FakeExecutionRunner` 供无 LLM/模型环境下的测试。 |
| `babeldoc/tools/executor/protocol.py` | 协议数据结构。`WorkerEvent`（执行器发出的事件）与 `EventEnvelope`（带上 `execution_id`/`sequence` 后写给客户端的信封），以及序号常量。 |
| `babeldoc/tools/executor/babeldoc_adapter.py` | 把「执行器协议」与「BabelDOC 翻译」粘合起来的适配层。`run_babeldoc_request` 在子进程内调用 `high_level.async_translate` 并把它的字典事件转写成 `WorkerEvent`。 |
| `babeldoc/tools/executor/workroot.py` | 「工作根目录」沙箱。所有输入/输出文件路径都被强制解析到该目录内，防止越界读写。 |

> 真正的「翻译」并不在这几个文件里发生，而是由适配层 `run_babeldoc_request` 转交给 `high_level.async_translate`（见 4.4 节）。executor 自己只管「**怎么把翻译过程包装成一个健壮的 RPC 服务**」。

---

## 4. 核心概念与源码讲解

### 4.1 ExecutorServer 端点：HTTP 表面

#### 4.1.1 概念说明

executor 对外是一个极简的 HTTP 服务，用标准库 `http.server.ThreadingHTTPServer` 实现，**没有引入 FastAPI / Flask 等框架**——因为端点很少、逻辑很专一，标准库足够，且少一个依赖就少一份供应链风险。它把 URL 当作「路由」，把 HTTP 方法（`GET`/`POST`）当作「动作」，把请求/响应体统一约定为 JSON。

它的核心端点可以归为四类：

| 方法 | 路径 | 作用 | 成功状态码 |
| --- | --- | --- | --- |
| `GET` | `/healthz` | 健康检查，顺带验证工作目录可读写 | `200` |
| `POST` | `/v1/executions` | 提交一个翻译任务 | `201`（忙时 `409`） |
| `GET` | `/v1/executions/{id}/events?after_sequence=N` | 流式拉取某任务从序号 N 之后的事件（NDJSON） | `200` |
| `POST` | `/v1/abort` | 中止当前正在跑的任务 | `202` |
| `POST` | `/v1/pdf/watermark1`、`/v1/pdf/watermark2` | 给已有 PDF 叠加水印（两种样式） | `200` |

#### 4.1.2 核心流程

请求进来后的分派逻辑分两条路：

```
do_GET(path):
    if path == "/healthz":              → 写一个临时文件验证 workroot 可写，返回 {"status":"ok"}
    elif path 匹配 /v1/executions/{id}/events:
        解析 after_sequence 查询参数     → _stream_events(id, after_seq)  # NDJSON 流
    else:                               → 404 not_found

do_POST(path):
    if path == "/v1/executions":        → _create_execution()            # 提交任务
    elif path == "/v1/abort":           → _abort_current()               # 中止
    elif path in {watermark1, watermark2}: → _run_watermark(...)          # 水印
    else:                               → 404 not_found
```

注意：**事件流是 `GET`，提交任务是 `POST`**。这是一个刻意的设计——提交是「改变状态」的动作用 `POST`，而拉取事件是「只读」的流式查询用 `GET`，这样浏览器、`curl`、各种 HTTP 客户端都能无障碍地消费事件流。

#### 4.1.3 源码精读

服务类本身非常薄，只是把 `ThreadingHTTPServer` 与一个共享的 `store` 绑在一起，并指定请求处理器为 `ExecutorHandler`：

[`babeldoc/tools/executor/server.py:32-35`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/server.py#L32-L35) — `ExecutorServer` 持有全局唯一的 `ExecutionStore`，每个请求线程通过 `self.server.store` 访问它。

`GET` 路由先把路径切成段，再用「段数 + 前缀 + 后缀」的模式匹配事件流端点；`after_sequence` 是必填的整数查询参数，缺失或非整数都返回 `400`：

[`babeldoc/tools/executor/server.py:58-85`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/server.py#L58-L85) — 路径形如 `v1/executions/{id}/events`，共 4 段；解析出 `id` 与 `after_sequence` 后交给 `_stream_events`。

`POST` 路由同理分派到提交、中止、水印三类：

[`babeldoc/tools/executor/server.py:87-102`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/server.py#L87-L102) — 注意水印端点用 `rsplit("/", 1)[-1]` 取出 `watermark1`/`watermark2` 作为 `operation` 参数。

两个写响应的工具方法体现了「JSON 优先」的约定：

[`babeldoc/tools/executor/server.py:408-419`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/server.py#L408-L419) — `_write_json` 统一序列化、设置 `Content-Type: application/json` 与 `Content-Length`；`_write_error` 把错误包装成 `{"code":..., "message":...}`，错误用稳定的 `code` 字符串而非 HTTP 状态码本身来表达，方便客户端编程式判断。

最后，`serve()` 是启动入口，它在绑定非环回地址（non-loopback）时会打一条**安全告警**——因为这个 HTTP 服务**没有任何鉴权**，默认只应绑在 `127.0.0.1`：

[`babeldoc/tools/executor/server.py:436-457`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/server.py#L436-L457) — 默认 `--host 127.0.0.1 --port 7860`；`_is_loopback_host` 判定环回；用 `babeldoc` runner 时先 `runner.warmup()` 预热 forkserver。

#### 4.1.4 代码实践

**实践目标**：把服务跑起来，亲手走一遍 `/healthz`。

**操作步骤**：

1. executor 需要一个「工作根目录」作为文件沙箱，先准备它并设为环境变量：

   ```bash
   export WR=$(mktemp -d)
   export BABELDOC_EXECUTOR_WORKROOT=$WR
   ```

2. 用 `fake` runner（无需 LLM、无需模型，详见 4.4 节）后台启动服务：

   ```bash
   python -m babeldoc.tools.executor --host 127.0.0.1 --port 7860 --runner fake &
   ```

   > 说明：executor 没有注册成 `babeldoc` 的子命令，只能用 `python -m babeldoc.tools.executor` 启动（入口见 [`babeldoc/tools/executor/__main__.py:1-4`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/__main__.py#L1-L4)）。

3. 健康检查：

   ```bash
   curl -s http://127.0.0.1:7860/healthz
   ```

**需要观察的现象**：服务日志打印 `starting executor on 127.0.0.1 7860`；`curl` 返回 `{"status":"ok"}`。

**预期结果 / 待本地验证**：返回 `{"status":"ok"}`。若忘记设 `BABELDOC_EXECUTOR_WORKROOT`，`/healthz` 会返回 `503` 与 `{"status":"error","code":"workroot_unavailable"}`（对应 [`server.py:42-56`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/server.py#L42-L56)）。请以本地实际运行为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么事件流端点用 `GET` 而不是 `POST`？如果改成 `POST` 会带来什么不便？

> **参考答案**：事件流是「只读取已产生的事件」的查询，语义上是幂等只读，适合 `GET`；用 `GET` 后，浏览器、`curl`、各种监控脚本都能直接消费这条流，不必构造请求体。若改成 `POST`，则每次拉取都要带 body，且许多 HTTP 中间件（缓存、代理）对 `POST` 流的处理不如 `GET` 友好。

**练习 2**：服务为什么默认绑 `127.0.0.1` 而不是 `0.0.0.0`？

> **参考答案**：executor 的 HTTP 协议**没有任何鉴权**（见 `serve()` 的告警）。绑 `127.0.0.1`（环回）意味着只有本机进程能访问，把它当作一个「信任边界」。绑 `0.0.0.0` 等于向全网开放一个能提交翻译任务、能读写工作目录的服务，极不安全。

---

### 4.2 任务提交与中止：ExecutionStore 的单活跃语义

#### 4.2.1 概念说明

`ExecutorHandler` 只是前台，真正「管事」的是 [`state.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/state.py) 里的 `ExecutionStore`。它有两条核心职责：

1. **记录每一次执行（execution）**：每个任务分配一个 `execution_id`（UUID）、一个单调递增的事件序号起点 `initial_sequence`，并把沿途产生的事件存进一个有界环形缓冲。
2. **强制「单活跃任务」**：同一时刻**只允许一个任务处于 `active` 状态**。这是 executor 有意为之的设计——翻译是重资源操作，并发跑多份会击穿内存与 QPS 限流；与其用复杂的排队，不如直接拒绝，让上游自己排队。

「单活跃」用状态机表达：每个 `ExecutionRecord` 有 `status` 字段，取值为 `active`（运行中）→ `terminal`（正常结束）/ `aborted`（被中止）/ `completed`（重操作完成）。只要当前 `status == "active"`，再提交新任务就被拒。

#### 4.2.2 核心流程

```
create(request):                         # POST /v1/executions
    校验 request["task_id"] 非空
    加锁：
        if 当前有 active 任务:
            raise ExecutionBusyError(快照)   # → 前台返回 409 {"code":"busy","snapshot":...}
        生成 initial_sequence（随机大整数，留出序号余量）
        造 ExecutionRecord，置为 _current，status="active"
    起一个守护线程跑 _run(record)            # 真正驱动 runner
    return {execution_id, status:"started", initial_sequence}   # → 前台返回 201

abort_current():                          # POST /v1/abort
    加锁：置 record.abort_event，status="aborted"，唤醒等待者
    return {"status":"aborting"}            # → 前台返回 202
```

`abort_current` **只是「举旗」**——它置位 `record.abort_event`（一个 `threading.Event`），并不直接杀死任务。真正响应取消的是执行器（4.4 节）和翻译主链路（`TranslationConfig.cancel_translation()`）。这种「协作式取消」避免了强行终止带来的资源泄漏。

#### 4.2.3 源码精读

`create` 的关键在于：先在锁内完成「占位」（判定 busy + 建 record），再**锁外**起线程跑任务，避免长时间持锁：

[`babeldoc/tools/executor/state.py:68-111`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/state.py#L68-L111) — 注意 `initial_sequence` 用 `secrets.randbelow(MAX_INITIAL_SEQUENCE) + 1` 生成一个随机起点，`MAX_INITIAL_SEQUENCE = MAX_SEQUENCE - SEQUENCE_HEADROOM`（见 [`protocol.py:7-10`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/protocol.py#L7-L10)），给单调递增的序号预留了 1 亿的余量。

前台把 `ExecutionBusyError` 翻译成 `409 CONFLICT`，并附上当前活跃任务的**快照**，让客户端知道「谁占着」：

[`babeldoc/tools/executor/server.py:121-136`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/server.py#L121-L136) — 快照含 `execution_id`/`task_id`/`status`/`last_sequence`（见 [`state.py:305-311`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/state.py#L305-L311)），便于客户端决定是等待还是改投。

`abort_current` 极简，核心就是 `record.abort_event.set()`：

[`babeldoc/tools/executor/state.py:113-128`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/state.py#L113-L128) — 置位后 `notify_all` 唤醒可能在 `stream()` 里等待新事件的消费者，避免它们傻等。

> 顺带一提：水印端点复用了同一套「单活跃」机制——它通过 `begin_heavy_operation` / `finish_heavy_operation` 把自己也注册成一个临时的 `ExecutionRecord`，从而保证水印子过程（4.1 表里的 `watermark1/2`）也不会和翻译任务并发（[`state.py:130-164`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/state.py#L130-L164)）。

#### 4.2.4 代码实践

**实践目标**：提交一个任务，并体验「单活跃」约束。

**操作步骤**（接 4.1.4 已启动的服务）：

1. 提交一个会「占着」的任务（fake runner 的 `block` 模式会空等 30 秒，模拟长任务）：

   ```bash
   curl -s -X POST http://127.0.0.1:7860/v1/executions \
        -H 'Content-Type: application/json' \
        -d '{"task_id":"long-1","mode":"block"}'
   ```

   记下返回里的 `execution_id` 与 `initial_sequence`。

2. **趁它还没结束**，立刻再提交第二个任务：

   ```bash
   curl -s -X POST http://127.0.0.1:7860/v1/executions \
        -H 'Content-Type: application/json' \
        -d '{"task_id":"long-2","mode":"block"}' -w '\n%{http_code}\n'
   ```

3. 中止当前任务：

   ```bash
   curl -s -X POST http://127.0.0.1:7860/v1/abort -w '\n%{http_code}\n'
   ```

**需要观察的现象**：第 2 步返回 HTTP `409`，body 是 `{"code":"busy",...,"snapshot":{"task_id":"long-1",...}}`；第 3 步返回 `202` 与 `{"status":"aborting"}`，被占住的任务随后释放，此时再提交新任务就能成功。

**预期结果 / 待本地验证**：并发提交被 `409 busy` 拒绝；中止后槽位释放。若动作不够快、`block` 任务已超时结束，第二个请求可能成功——请以本地实际现象为准。

#### 4.2.5 小练习与答案

**练习 1**：`create` 为什么要在锁内「占位」、锁外「起线程」？

> **参考答案**：占位（判定 busy + 建 record）是临界区操作，必须持锁以保证「单活跃」判定的原子性；但真正跑翻译的 `_run` 很慢（几分钟），若在锁内启动会阻塞所有其他请求（包括事件流拉取）。所以锁只保护账本更新，耗时执行放锁外。

**练习 2**：`abort_current` 为什么不直接 `kill` 掉任务线程/进程？

> **参考答案**：强杀会留下半开的文件、未释放的模型句柄、未清理的临时目录，长期会泄漏资源。`abort_current` 只置 `abort_event`，由执行器和翻译链路**协作式**地检查并优雅退出，保证善后（关 PDF、删临时文件）能跑完。

---

### 4.3 NDJSON 事件流：EventEnvelope、序号与续传

#### 4.3.1 概念说明

executor 最有意思的部分是它的事件流协议。回顾 u8-l3：`high_level.async_translate` 把事件 `yield` 给**同一个进程**里的调用方，事件是「即生即灭」的字典，**错过了就没有了**。RPC 场景下这远远不够——客户端可能晚到、可能断线重连、可能想回放。于是 executor 在「裸事件」之上加了两层：

1. **信封（EventEnvelope）**：给每个事件套上 `type`、`execution_id`、`sequence`、`payload` 四个字段，让它在网络里自描述「我属于哪个任务、我是第几条」。
2. **持久化 + 续传**：`ExecutionStore` 把事件按 `sequence` 存进一个有界环形缓冲（`deque`，默认上限 1000 条），客户端可以用 `after_sequence=N` 请求「N 之后的所有事件」，从而支持**断点续传**与**回放**。

协议定义在 [`protocol.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/protocol.py)，只有两个 frozen dataclass：

- `WorkerEvent`：执行器内部产生的事件，只有 `type` 与 `payload`。`type` 取值为 `progress`（进度）、`result`（成功终态）、`error`（失败终态）。
- `EventEnvelope`：写给客户端的信封，多了 `execution_id` 与 `sequence`，并自带 `to_json_line()` 序列化成「一行 JSON + `\n`」。

#### 4.3.2 核心流程

事件从产生到送达客户端的全链路：

```
子进程内 runner 产生 WorkerEvent(type, payload)
        │  progress_send.send({"type","payload"})
        ▼
父进程 MultiprocessExecutionRunner.run 收到 → emit(event)
        │
        ▼
ExecutionStore._append_event:                # state.py
    加锁：last_seq += 1
    包成 EventEnvelope(type, execution_id, sequence=last_seq, payload)
    追加进 record.events（环形，超 1000 条丢最旧的）
    若 type ∈ {result,error}: status="terminal"
    notify_all（唤醒 stream 中等待的消费者）
        │
        ▼
客户端 GET .../events?after_sequence=N
ExecutionStore.stream(id, N):                # 生成器
    cursor = N
    循环：
        取出 sequence > cursor 的事件，逐个 yield（每个写成 to_json_line() 一行）
        若没新事件且 status=="active": wait 等待被唤醒
        若遇到终态事件或任务结束: return
```

**序号（sequence）是续传的关键**：它单调递增、由 `ExecutionStore` 在锁内分配，客户端每次记住「我读到第几条」，下次用 `after_sequence=最后读到的序号` 续上。环形缓冲丢弃最旧事件后，若客户端请求的 `after_sequence` 已落在缓冲之外，会抛 `ReplayGapError`，前台翻译成 `410 Gone`（[`server.py:348-374`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/server.py#L348-L374)）。

#### 4.3.3 源码精读

协议常量与两个数据类：

[`babeldoc/tools/executor/protocol.py:7-35`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/protocol.py#L7-L35) — `MAX_EVENT_LOG_SIZE=1000` 是环形缓冲容量；`to_json_line` 用 `separators=(",", ":")` 去掉空白、`ensure_ascii=False` 保留中文，输出紧凑的一行。

序号分配与环形裁剪、终态判定都在 `_append_event_locked`：

[`babeldoc/tools/executor/state.py:255-298`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/state.py#L255-L298) — `last_seq += 1` 后构造信封；`while len(events) > max: popleft()` 实现环形；`TERMINAL_EVENT_TYPES = {"result","error"}`（[`state.py:38`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/state.py#L38)）命中时把 `status` 置 `terminal`。

流式输出 `stream` 是一个**阻塞生成器**：没新事件就在 `Condition` 上 `wait`，被 `_append_event` 的 `notify_all` 唤醒后继续 `yield`，直到吐出终态事件或任务结束：

[`babeldoc/tools/executor/state.py:172-196`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/state.py#L172-L196) — 这是「长连接」式流：HTTP 响应不结束，服务端持续 `write + flush` 每一行 NDJSON。

前台把每行写出去，并处理客户端断连（`BrokenPipeError`）：

[`babeldoc/tools/executor/server.py:376-399`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/server.py#L376-L399) — `Content-Type: application/x-ndjson`；逐行 `wfile.write(event.to_json_line())` + `flush()`，客户端可即时读到。

> **与 u8-l3 `async_translate` 的对照**：两者都把翻译过程表达成「进度事件 + 终态事件」的流。区别在于：
>
> | 维度 | `high_level.async_translate` | executor NDJSON 流 |
> | --- | --- | --- |
> | 传输 | 同进程 `async for` 迭代器 | 跨网络 HTTP NDJSON |
> | 事件形态 | 裸字典，含内层 `type` | `EventEnvelope` 信封（外加 `execution_id`/`sequence`） |
> | 终态事件 | `finish`（带 `TranslateResult`）/ `error` | `result`（带 `files/metrics/pages`）/ `error` |
> | 进度事件 | `progress_start/update/end` | `progress`，其 `payload` 内层**复用**了 `progress_start/update/end` 字典 |
> | 续传/回放 | 无，错过即丢 | 有，靠 `sequence` + `after_sequence` 断点续传 |
> | 并发 | 由调用方决定 | 强制单活跃，忙时 `409` |
> | 取消 | `cancel_event`（线程） | `/v1/abort` → `abort_event` → 父子进程 cancel 管道 → 同一个 `config.cancel_translation()` |
>
> 简言之：executor 的 NDJSON 流是「把 `async_translate` 的事件流**信封化、持久化、网络化、进程隔离化**」后的产物，内层进度事件的语义是一脉相承的。

#### 4.3.4 代码实践

**实践目标**：提交一个能快速完成的小任务，亲眼看 NDJSON 流一行行吐出来。

**操作步骤**（接 4.1.4 已启动的 `fake` 服务，确保当前无活跃任务）：

1. 提交一个 `burst` 任务（fake runner 会立刻发 2 条 progress + 1 条 result）：

   ```bash
   curl -s -X POST http://127.0.0.1:7860/v1/executions \
        -H 'Content-Type: application/json' \
        -d '{"task_id":"demo-1","mode":"burst"}'
   ```

   返回形如 `{"execution_id":"...","status":"started","initial_sequence":12345}`。

2. 用返回的 `execution_id` 和 `initial_sequence` 拉事件流（`-N` 关闭 curl 缓冲，逐行显示）：

   ```bash
   curl -s -N "http://127.0.0.1:7860/v1/executions/<execution_id>/events?after_sequence=<initial_sequence>"
   ```

**需要观察的现象**：终端逐行打印三行 JSON，每行都有 `type`、`execution_id`、`sequence`、`payload` 四个字段；`sequence` 单调递增；前两行 `type` 为 `progress`，最后一行 `type` 为 `result`。`result` 之后连接关闭（因为它是终态事件）。

预期看到的 NDJSON（字段顺序、`sequence`/`execution_id` 以本地为准）：

```json
{"type":"progress","execution_id":"...","sequence":12346,"payload":{"index":1}}
{"type":"progress","execution_id":"...","sequence":12347,"payload":{"index":2}}
{"type":"result","execution_id":"...","sequence":12348,"payload":{"files":{"dual_pdf":"dual.pdf"},"metrics":{},"usage":null}}
```

**预期结果 / 待本地验证**：应观察到 3 行 NDJSON，末行为 `result`。`sequence` 从 `initial_sequence + 1` 开始——因为 [`create`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/state.py#L68-L111) 把 `last_seq` 初始化为 `initial_sequence`，而 `_append_event_locked` 是先 `last_seq += 1` 再写信封。请以本地实际输出为准。

**进阶**：把第 2 步的 `after_sequence` 改成比 `initial_sequence` 更大的某个**中间** `sequence` 值重跑，验证「续传」——只会拿到该序号之后的事件；若把 `after_sequence` 设得过大（超过已产生的最大序号），由于任务已 `terminal`，流会立即结束而不报错。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `sequence` 要用一个**随机**的 `initial_sequence` 起点，而不是从 0 或 1 开始？

> **参考答案**：从安全角度，随机大起点避免客户端猜测其他任务的序号；从工程角度，`MAX_INITIAL_SEQUENCE = MAX_SEQUENCE - SEQUENCE_HEADROOM`（1 亿余量）保证即便起点很「靠后」，单调递增也几乎不可能撞到 `MAX_SEQUENCE = 2^63 - 1` 上限。撞顶会触发 `OverflowError` 并中止任务（[`state.py:262-264`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/state.py#L262-L264)）。

**练习 2**：环形缓冲容量是 1000 条，如果一个长任务产生了 1500 条事件，客户端断线后想从头拉会怎样？

> **参考答案**：最旧的 500 条已被 `popleft()` 丢弃，`first_available_seq` 会前移。客户端若请求的 `after_sequence` 落在已丢弃区间，`_raise_if_gap_locked` 抛 `ReplayGapError`，前台返回 `410 Gone` 与 `{"code":"replay_gap",...}`（[`server.py:351-362`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/server.py#L351-L362)），提示「这些事件已不可用」。

---

### 4.4 多进程执行器：MultiprocessExecutionRunner

#### 4.4.1 概念说明

前台（HTTP）和账本（ExecutionStore）都跑在**主进程**里，但真正的翻译绝不能在主进程里跑——ONNX 推理、字体子集化、第三方原生库都可能段错误，一旦崩了整个服务就没了。`MultiprocessExecutionRunner`（[`runner.py`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py)）负责把任务丢进一个**独立子进程**，并在父子进程间用两根 `multiprocessing.Pipe` 双向通信：

- **progress 管道**（子→父）：子进程把 `WorkerEvent` 顺着管道发上来，父进程收到的就是 4.3 节里 `emit` 的事件。
- **cancel 管道**（父→子）：父进程在收到 `/v1/abort` 时往这根管道塞一个信号，子进程据此取消翻译。

它用 `forkserver` 启动方式（而不是默认的 `fork`/`spawn`）：forkserver 会在服务启动时预先 fork 出一个「已经 import 好重模块」的服务进程，之后每个任务从这个预热好的进程 fork，**省掉反复 import `high_level`、ONNX 运行时等大模块的几秒钟启动开销**。预热时还可以用 `preload_modules` 提前加载指定模块（[`server.py:460-474`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/server.py#L460-L474) 里预载了 `high_level`、`rpc_doclayout8` 等）。

> **`FakeExecutionRunner` 的角色**：它和 `MultiprocessExecutionRunner` 实现同一个抽象基类 `ExecutionRunner.run`，但**不开子进程、不调 LLM**，只在主进程里按 `mode` 发几条假事件或拷贝文件（[`runner.py:87-195`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py#L87-L195)）。本讲前面几节用 `--runner fake` 就是靠它跑通的。真实的 `--runner babeldoc` 才会走多进程路径。

#### 4.4.2 核心流程

```
MultiprocessExecutionRunner.run(request, emit, abort_event):
    建 progress 管道 (recv, send) 与 cancel 管道 (recv, send)
    Process(target=_run_process_target, args=(target, request, progress_send, cancel_recv)).start()
    父进程关闭自己这头的 progress_send / cancel_recv
    循环：
        if abort_event.is_set():          # 收到 /v1/abort
            cancel_send.send(True)         # 通知子进程取消
            return
        if progress_recv.poll(0.05):       # 0.05s 探一次
            item = progress_recv.recv()
            if item is None:               # 子进程主动结束的哨兵
                兜底发 missing_terminal_event error；return
            event = _coerce_event(item)    # dict/WorkerEvent → WorkerEvent
            emit(event)                    # → ExecutionStore 记账
            if event.type ∈ {result,error}: terminal_seen=True; return
        elif not process.is_alive():       # 子进程先死了
            排空残余事件；若没见过终态 → 兜底 error；return
    finally:
        cancel_send.send(True); 关闭管道；_stop_process（terminate→kill 兜底）
```

子进程那头（`_run_process_target` → `run_babeldoc_request`）则反过来：用 `progress_send` 往上发事件，用 `cancel_recv` 接收取消。`run_babeldoc_request` 的 `finally` 里会发一个 `None` 作为「我结束了」的哨兵（[`babeldoc_adapter.py:78-83`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/babeldoc_adapter.py#L78-L83)）。

#### 4.4.3 源码精读

构造器把 `preload_modules` 注册给 forkserver，并按 `start_method` 取一个独立的 `multiprocessing` 上下文：

[`babeldoc/tools/executor/runner.py:198-215`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py#L198-L215) — `multiprocessing.set_forkserver_preload(...)` 让预热进程提前 import 这些模块。

`warmup` 在服务启动前先 fork 一个空任务，确保 forkserver 与预载模块就绪，失败则直接报错而不带病上岗：

[`babeldoc/tools/executor/runner.py:217-234`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py#L217-L234) — 这就是 `serve()` 里 `runner.warmup()`（[`server.py:450-451`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/server.py#L450-L451)）的来源。

`run` 的主循环是「**轮询管道 + 探活子进程**」的双重检查，保证无论子进程是「正常发完终态事件退出」还是「中途崩了」，父进程都能正确收尾：

[`babeldoc/tools/executor/runner.py:236-337`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py#L236-L337) — 注意三处「没见到终态事件」的兜底：管道提前关闭、收到 `None` 哨兵、子进程先死，都会调 `_emit_missing_terminal_error` 补发一条 `error`（[`runner.py:375-393`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py#L375-L393)），**保证每个任务一定有一个终态事件**——这是 RPC 协议可靠性的基石。

`_coerce_event` 容错地把管道里发上来的 `dict` 或 `WorkerEvent` 统一成 `WorkerEvent`：

[`babeldoc/tools/executor/runner.py:339-348`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py#L339-L348) — 因为 `multiprocessing.Pipe` 传对象时会按值序列化，子进程里的 `WorkerEvent` 到父进程可能变成普通结构，故需归一化。

**取消的两段旅程**（与 u8-l3 的 `cancel_event` 形成呼应）：父进程 `_send_cancel` 往 cancel 管道写 `True`（[`runner.py:368-373`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py#L368-L373)）；子进程里的 `_watch_cancel_pipe` 守护线程收到后调用 `_CancelState.request_cancel` → `config.cancel_translation()`（[`babeldoc_adapter.py:92-104`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/babeldoc_adapter.py#L92-L104)、[`babeldoc_adapter.py:107-115`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/babeldoc_adapter.py#L107-L115)）。也就是说：**executor 的 `/v1/abort` 最终和 `async_translate` 的 `cancel_event` 殊途同归，都落在同一个 `TranslationConfig.cancel_translation()` 上**。

最后，适配层 `_run_async_translate` 把 `high_level.async_translate` 的字典事件**逐个转写**成 `WorkerEvent("progress", ...)` 上发，并在 `finish` 事件里抽取 `TranslateResult` 转成标准化的 `result` 信封：

[`babeldoc/tools/executor/babeldoc_adapter.py:309-336`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/babeldoc_adapter.py#L309-L336) — 这段就是 4.3.3 对照表里「progress 信封内层复用 high_level 事件字典」的具体来源：`emit("progress", dict(event))` 把 `progress_start/update/end` 原样塞进 `payload`。

#### 4.4.4 代码实践

**实践目标**：用源码阅读理解「为什么 executor 必须开子进程」，并定位取消信号的两段旅程。本节为**源码阅读型实践**（真实 `babeldoc` runner 还需要外部 LLM 网关与 RPC 版面服务，本地不一定具备，故以阅读为主）。

**操作步骤**：

1. 打开 [`babeldoc/tools/executor/runner.py:236-337`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py#L236-L337)，在主循环里找出三处「子进程异常退出」的分支，记下它们各自调用 `_emit_missing_terminal_error` 的行号。

2. 打开 [`babeldoc/tools/executor/babeldoc_adapter.py:27-83`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/babeldoc_adapter.py#L27-L83)，画出取消信号的传递链：`/v1/abort` → `store.abort_current` → `record.abort_event` → `MultiprocessExecutionRunner.run` 的 `abort_event.is_set()` → `cancel_send.send(True)` → 子进程 `_watch_cancel_pipe` → `config.cancel_translation()`。

3. 想清楚：如果翻译子进程在调 LLM 时发生段错误（`exit_code = -11`）直接死掉，主循环的哪个分支会触发？客户端最终会收到什么？

**需要观察的现象（推理）**：`progress_recv.poll` 返回假且 `not process.is_alive()` 为真，进入 [`runner.py:319-332`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py#L319-L332) 分支；`_drain_progress` 排空残余事件后，因没见过终态，调用 `_emit_missing_terminal_error(-11)`，客户端最终收到一条 `{"type":"error",...,"payload":{"code":"missing_terminal_event","message":"...exit_code=-11"}}`。

**预期结果 / 待本地验证**：上述推理应与源码一致。可选用 4.3.4 的 `fake` 服务结合一个能主动 `os._exit(139)` 的小脚本替换 `target` 验证（需改代码，**不要改本项目源码**，建议在副本里做）。若不做实验，结论标注「待本地验证」即可。

#### 4.4.5 小练习与答案

**练习 1**：为什么 runner 默认用 `forkserver` 而不是 `spawn`？

> **参考答案**：`spawn` 每次都要重新启动 Python 解释器并 import 全部模块（`high_level`、ONNX 运行时等），单次任务启动开销几秒；`forkserver` 在服务启动时预热好一个已 import 完重模块的进程，之后每个任务只需 `fork` 它，启动开销降到毫秒级。`serve()` 里 `runner.warmup()` 就是触发这个预热的。

**练习 2**：父进程在 `finally` 里为什么一定要 `_send_cancel` + `_stop_process`，即使任务已正常结束？

> **参考答案**：这是防御性收尾。即使子进程已正常退出，`_stop_process` 也会 `join` 回收僵尸进程；若子进程因异常**还活着**（比如卡在原生调用里），则 `terminate` → 等 `join_timeout` → 仍不死就 `kill`，确保不会留下泄漏的子进程占用资源（[`runner.py:395-412`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py#L395-L412)）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「**端到端的事件流追踪**」。

**任务**：用 `fake` runner 提交一个**默认模式**的任务（会发 progress + result 并拷贝文件），用一条命令同时完成「提交并立即拉流」，然后对照源码解释你看到的每一行 NDJSON 是从哪段代码发出来的。

**建议步骤**：

1. 确保已设 `BABELDOC_EXECUTOR_WORKROOT` 并启动 `--runner fake` 服务（4.1.4）。

2. 准备一个假的输入 PDF（任意小文件即可，fake runner 只 `copyfile`），放入 workroot：

   ```bash
   head -c 1024 /dev/urandom > "$WR/sample.pdf"
   ```

3. 提交默认模式任务（需要 `paths.input_file` 与 `paths.output_dir`，见 [`runner.py:174-195`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py#L174-L195)）：

   ```bash
   curl -s -X POST http://127.0.0.1:7860/v1/executions \
        -H 'Content-Type: application/json' \
        -d "{\"task_id\":\"e2e-1\",\"paths\":{\"input_file\":\"sample.pdf\",\"output_dir\":\"out\"}}"
   ```

   记下 `execution_id` 与 `initial_sequence`。

4. 拉流：

   ```bash
   curl -s -N "http://127.0.0.1:7860/v1/executions/<execution_id>/events?after_sequence=<initial_sequence>"
   ```

5. **对照源码逐行解释**（写成你的笔记）：
   - 每行的 `sequence` 由 [`state.py:255-298`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/state.py#L255-L298) 的 `last_seq += 1` 分配；
   - `type:progress` 的两条来自 [`runner.py:114-123`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py#L114-L123) 与 [`runner.py:139-148`](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py#L139-L148)；
   - `type:result` 的那条来自 [`runner.py:152-172`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py#L152-L172)，其 `payload.files` 列出了拷贝出来的 mono/dual 等文件路径；
   - 它们都经 `_coerce_event`（[`runner.py:339-348`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py#L339-L348)）归一化、`ExecutionStore._append_event` 信封化、`to_json_line()`（[`protocol.py:26-35`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/protocol.py#L26-L35)）序列化成一行。

6. **写一段对比**：用你观察到的 NDJSON，对照 4.3.3 的表格，说明它与 `high_level.async_translate` 事件协议的三点相同（都有进度+终态、都有取消、进度都来自翻译主链路）与三点不同（信封化 vs 裸字典、可续传 vs 不可续传、跨进程隔离 vs 同进程）。

**预期结果 / 待本地验证**：应看到两行 `progress`（`overall_progress` 分别为 10 和 90）与一行 `result`（`files` 含 `dual_pdf`/`mono_pdf` 等路径，路径相对于 workroot 的 `out/` 目录）。文件被实际拷贝到 `$WR/out/`。请以本地实际输出为准。

---

## 6. 本讲小结

- executor 是 BabelDOC 的**第三个入口**：一个独立的、基于标准库 `http.server` 的翻译任务 RPC 服务，定位是 sidecar，默认只绑 `127.0.0.1`、**无鉴权**，把翻译的崩溃风险隔离在主程序之外。
- 端点极简：`/healthz` 健康检查、`POST /v1/executions` 提交、`GET /v1/executions/{id}/events?after_sequence=N` 流式拉取、`POST /v1/abort` 中止，外加两个水印端点；错误统一用 `{"code","message"}` 表达。
- `ExecutionStore` 强制**单活跃任务**语义：同一时刻只允许一个 `active` 任务，忙时提交返回 `409 busy` 并附当前任务快照；中止只是置 `abort_event`，由执行器协作式退出。
- 事件流是 **NDJSON**：每个事件被包成 `EventEnvelope`（`type`/`execution_id`/`sequence`/`payload`），`sequence` 单调递增，支持 `after_sequence` **断点续传**；环形缓冲丢弃旧事件后请求越界返回 `410 Gone`。
- `MultiprocessExecutionRunner` 用 `forkserver` 在**独立子进程**里跑翻译，靠 progress/cancel 两根 `Pipe` 与父进程通信；无论子进程正常结束还是崩溃，父进程都保证补发一个**终态事件**（`result`/`error`），让协议可靠收敛。
- 取消信号 `/v1/abort` 穿过 `abort_event` → cancel 管道 → `config.cancel_translation()`，最终与 `high_level.async_translate` 的 `cancel_event` **殊途同归**；NDJSON 流内层的进度事件字典也直接复用自 `async_translate`——executor 是把后者「信封化、持久化、网络化、进程隔离化」的升级版。

---

## 7. 下一步学习建议

- **回到适配层细节**：精读 [`babeldoc_adapter.py`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/babeldoc_adapter.py) 的 `build_translation_config`，看 RPC 请求 JSON 是如何严格校验并组装成 `TranslationConfig` 的（它复用了 u1-l4 的配置中心），以及 `ExecutorTranslator`（[`translator.py`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/translator.py)）如何实现 u6-l1 的 `BaseTranslator` 接口。
- **水印后处理**：阅读 [`watermark_transform.py`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/watermark_transform.py) 与 [`server.py:151-346`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/server.py#L151-L346)，理解 `watermark1`（平铺）/`watermark2`（四角）两种水印为何要放进**子进程 + 超时**保护（`WATERMARK_TIMEOUT_SECONDS=600`），这与 u7-l3 讲过的「子进程 + 超时 + 回退」健壮性模式一脉相承。
- **结合 u8-l1（资源管理）**：executor 默认 `babeldoc` runner 在 `preload_modules` 里预载 `rpc_doclayout8`，并要求 `gateways.layout.adapter == "rpc_doclayout8"`——即版面分析走 RPC 服务而非本地 ONNX。可对照 u5-l2 与 u8-l1 理解这种「模型外置」部署形态。
- **若要二次开发一个执行器**：实现 `ExecutionRunner.run(request, emit, abort_event)` 接口（参考 [`runner.py:58-65`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/runner.py#L58-L65)），在 `_create_runner` 里登记一个新名字（[`server.py:460-477`](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/server.py#L460-L477)），即可在不改动前台与账本的前提下替换执行后端。

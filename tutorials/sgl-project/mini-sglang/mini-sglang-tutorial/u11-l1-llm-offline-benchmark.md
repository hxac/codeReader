# LLM 离线推理接口与基准

## 1. 本讲目标

前面十几讲里，我们一直在拆解 Mini-SGLang 的「在线服务」形态：FastAPI 前端、tokenizer/detokenizer 进程、Scheduler 多 rank 广播、Engine 前向……它们由 ZMQ 串成一圈消息环。本讲换一个视角：**把这些进程间管道全部拆掉，把 Scheduler 直接当成一个进程内的 Python 对象来用**。

学完本讲，你应当能够：

1. 说清 `LLM` 类如何继承 `Scheduler`、靠 `offline_mode=True` 让主循环代码「一字不改」地在进程内自闭环。
2. 复述 `offline_receive_msg` / `offline_send_result` 如何用一个异常 `RequestAllFinished` 终结本应「永不返回」的 `run_forever`。
3. 跟踪 `LLM.generate` 从 `pending_requests` 到 `status_map` 再到结果文本的完整数据流。
4. 区分「离线基准」与「在线基准」度量的东西不同：前者算聚合吞吐，后者算 TTFT/TPOT/E2E 延迟分位。
5. 亲手跑一次离线 bench，并在 `page_size` 与 overlap 开关下解释吞吐差异。

## 2. 前置知识

本讲默认你已经读过以下讲义（术语不再重新定义）：

- **u4-l1 Scheduler 主循环与 Overlap Scheduling**：`run_forever` 是 `@torch.inference_mode` 装饰的死循环，`overlap_loop` 用两条 CUDA stream 把「处理上一批结果」与「算当前批」重叠；`ForwardInput`/`ForwardData` 是 overlap 下跨 stream 的保活容器。
- **u2-l3 进程间消息与序列化**：`UserMsg(uid, input_ids, sampling_params)` 是进 scheduler 的入向消息，`DetokenizeMsg(uid, next_token, finished)` 是出向消息，`uid` 是贯穿全环的请求身份。
- **u2-l2 配置体系**：`SchedulerConfig` 继承 `EngineConfig`，本讲会反复用到它的 `offline_mode`、`max_extend_tokens`、`page_size`、`cuda_graph_max_bs`、`max_seq_len_override` 等字段。

两个通俗概念先讲清：

- **离线推理（offline inference）**：你手里已经有一批 prompt，不关心每个请求什么时候开始、什么时候返回第一个字，只关心「把这批全部算完一共花了多少时间、平均每秒吐多少 token」。它对应「填满 GPU 再算」的吞吐极限场景。
- **在线推理（online inference）**：请求按真实用户的到达节奏（trace）陆续到来，你关心的是每个请求的「首字延迟 TTFT」「逐字延迟 TPOT」「端到端 E2E」。它对应「用户在等」的延迟敏感场景。

Mini-SGLang 用**同一个推理内核**服务这两种场景：离线用 `LLM` 类直接调用，在线用 HTTP 服务 + 异步客户端回放 trace。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/minisgl/llm/llm.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/llm/llm.py) | `LLM` 类本体，继承 `Scheduler`，定义 `offline_receive_msg`/`offline_send_result`/`generate` |
| [python/minisgl/scheduler/scheduler.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py) | `Scheduler` 主循环 `run_forever`/`overlap_loop`，`LLM` 完全复用 |
| [python/minisgl/scheduler/io.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py) | `SchedulerIOMixin`：按 `offline_mode` 把 `receive_msg`/`send_result` 动态绑到不同实现 |
| [benchmark/offline/bench.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/benchmark/offline/bench.py) | 离线基准主脚本（随机 token prompt，256 序列） |
| [benchmark/offline/bench_wildchat.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/benchmark/offline/bench_wildchat.py) | 离线基准的「真实数据」版（WildChat 对话 prompt + chat template） |
| [benchmark/online/bench_qwen.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/benchmark/online/bench_qwen.py) | 在线基准主脚本（下载 Qwen trace、按时间戳回放） |
| [benchmark/online/bench_simple.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/benchmark/online/bench_simple.py) | 在线基准的简化版（固定 batch size） |
| [python/minisgl/benchmark/client.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/benchmark/client.py) | 在线基准的客户端工具库：trace 回放、TTFT/TPOT/E2E 统计、吞吐计算 |

## 4. 核心概念与源码讲解

### 4.1 LLM 类：让 Scheduler 在进程内自闭环

#### 4.1.1 概念说明

`LLM` 是一个面向「离线批处理」用户的极简入口：你给它一组 prompt 和采样参数，它返回每组 prompt 的生成文本。但它的实现方式很特别——**它不是一个新写的推理引擎，而是直接继承 `Scheduler`**。

回顾 u4-l1：`Scheduler` 的全部推理能力（overlap 调度、prefill/decode 分批、Engine 前向、采样、KV cache 管理）都封装在 `run_forever` 这个死循环里。`LLM` 没有重写任何调度或前向逻辑，它只做了两件事：

1. 构造一个 `offline_mode=True` 的 `SchedulerConfig`，让 Scheduler 的 I/O 层「换轨」到进程内消息源；
2. 重写两个 I/O 钩子 `offline_receive_msg` / `offline_send_result`，分别取代「从 ZMQ 收消息」和「往 ZMQ 发结果」。

于是同一个 `run_forever`，在在线模式下从 ZMQ 读消息，在离线模式下从 `LLM` 实例的 `pending_requests` 列表读消息——**主循环代码一行都没改**。

#### 4.1.2 核心流程

```
用户调用 LLM.generate(prompts, sampling_params)
        │
        ▼
  填充 self.pending_requests
        │
        ▼
  调用 self.run_forever()   ← 完全复用 Scheduler 的 overlap_loop
        │
        │  每轮循环里:
        │   receive_msg()  → offline_receive_msg()  从 pending_requests 取 UserMsg
        │   _schedule_next_batch() / _forward()      原封不动
        │   send_result()  → offline_send_result()  把 DetokenizeMsg 写回 status_map
        │
        │  当 pending 空且无在途请求时:
        │   offline_receive_msg(blocking=True) 抛 RequestAllFinished
        │
        ▼
  generate 捕获异常, 从 status_map 聚合结果文本返回
```

关键点：`run_forever` 的类型签名是 `-> NoReturn`（永不返回），但 `LLM` 通过「在 receive 路径抛异常」让它以受控方式退出。这是一个非常干净的「复用 + 终结」设计。

#### 4.1.3 源码精读

`LLM.__init__` 构造一个单卡、离线模式的配置并交给父类 `Scheduler`：

[python/minisgl/llm/llm.py:L28-L40](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/llm/llm.py#L28-L40) —— `LLM(Scheduler)` 的构造函数。注意三个要点：`tp_info=DistributedInfo(0, 1)` 表示单卡（rank 0、size 1）；`offline_mode=True` 是本讲一切的开关；`**kwargs` 把 `page_size`、`max_extend_tokens`、`cuda_graph_max_bs`、`max_seq_len_override` 等透传给 `SchedulerConfig`。末尾初始化三个实例状态：`pending_requests`（待处理队列）、`status_map`（uid → 结果状态）、`counter`（uid 发号器）。

真正「换轨」的代码在 `SchedulerIOMixin.__init__` 里，靠 `offline_mode` 提前 return：

[python/minisgl/scheduler/io.py:L30-L33](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L30-L33) —— 当 `config.offline_mode` 为真时，把 `self.receive_msg` 绑到 `offline_receive_msg`、`self.send_result` 绑到 `offline_send_result`，然后 **`return` 提前退出**，跳过下面所有 ZMQ 队列（`ZmqPullQueue`/`ZmqPushQueue`/`ZmqPubQueue`/`ZmqSubQueue`）的创建。这就是「拆掉进程间管道」的精确位置：离线模式下根本不创建任何 ZMQ socket。

而 `Scheduler.run_forever` 完全不知道这一切，它只是调用 `self.receive_msg` 与 `self.send_result` 这两个名字：

[python/minisgl/scheduler/scheduler.py:L120-L131](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L120-L131) —— `run_forever` 的类型注解是 `-> NoReturn`。它根据 `ENV.DISABLE_OVERLAP_SCHEDULING` 选择 `normal_loop` 或 `overlap_loop`。`LLM` 没有改写它，所以离线推理默认也走 overlap 分支（除非用环境变量关闭）。

> 对比在线模式：在线时这同一份 `run_forever` 跑在每个 Scheduler 进程里，`receive_msg` 绑到 ZMQ 拉取 + 多 rank 广播（见 u4-l2）。**主循环对所有拓扑保持不变**，是这套设计最优雅的地方。

#### 4.1.4 代码实践

1. **目标**：验证「离线模式下不创建任何 ZMQ socket」。
2. **步骤**：在 `LLM.__init__` 调用 `super().__init__(config)` 之后，打印 `self.__dict__` 的键，检查是否存在以 `_recv` / `_send` / Zmq 开头的属性。
3. **示例代码**（这是示例代码，不是项目原有文件）：
   ```python
   from minisgl.llm import LLM
   llm = LLM("Qwen/Qwen3-0.6B")
   zmq_like = [k for k in llm.__dict__ if "recv" in k or "send" in k or "zmq" in k.lower()]
   print("ZMQ-related attrs:", zmq_like)   # 预期为空列表 []
   ```
4. **观察现象**：输出应为空列表，证明 `offline_mode` 让 I/O 层提前 return，没有创建任何 ZMQ 队列。
5. **预期结果**：`ZMQ-related attrs: []`。若在无 GPU 环境下无法实例化 `LLM`，可改为阅读 [io.py:L30-L33](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L30-L33) 的 `return` 语句，结论一致——标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `offline_mode` 设为 `False` 但仍用 `LLM` 调用 `generate`，会发生什么？

**答案**：`SchedulerIOMixin.__init__` 不会提前 return，会去创建 `ZmqPullQueue(config.zmq_backend_addr, ...)` 等真实 ZMQ 队列；由于 `tp_info=DistributedInfo(0,1)` 是单 rank 且 `is_primary()` 为真，它会绑定 `zmq_backend_addr`（`ipc:///tmp/minisgl_0.pid=...`）。随后 `run_forever` 里 `receive_msg` 绑到 `_recv_msg_single_rank`，它会**阻塞等待 ZMQ 消息**——而离线场景下没有任何进程往这个地址发消息，于是 `generate` 会永远挂起。`offline_mode=True` 是离线通路能工作的前提。

**练习 2**：`LLM` 为什么强制 `tp_info=DistributedInfo(0, 1)`（单卡）？

**答案**：离线 `LLM` 是进程内单对象，没有多进程组、没有 NCCL 通信器、没有 rank 1+ 来广播。多卡张量并行需要 `launch_server` 起多个 Scheduler 进程（见 u1-l4），那是**在线服务**的形态。所以 `LLM` 只支持单卡；若要测多卡吞吐，应使用在线服务 + 在线基准。

---

### 4.2 offline_receive_msg / offline_send_result：用异常终结死循环

#### 4.2.1 概念说明

`Scheduler` 的主循环依赖两个 I/O 钩子：`receive_msg(blocking)`「拿一批入向消息」、`send_result(reply)`「把一批出向结果发走」。在线模式下它们对接 ZMQ；离线模式下，`LLM` 用两个方法把它们对接到进程内的 Python 列表与字典。

这里有一个绕不开的难题：**`run_forever` 是死循环，离线批处理算完后怎么让它停下来？** `LLM` 的答案是——在「该收消息却没消息可收」时抛一个自定义异常 `RequestAllFinished`，让 `generate` 在外层 `try/except` 捕获。于是「永不返回」的循环以受控异常的方式「返回」了。

#### 4.2.2 核心流程

**入向 `offline_receive_msg(blocking)`**：

```
若 blocking 且 pending_requests 为空:
    抛 RequestAllFinished    ← 唯一的退出信号
否则:
    遍历 pending_requests, 受 prefill_budget 约束攒一批
    对每个 prompt:
        _tokenize_one → input_ids
        发 uid, 构造 UserMsg(uid, input_ids, sampling_params)
        在 status_map 注册 RequestStatus(uid, input_ids, output_ids=[])
    counter += 本批量; pending_requests 弹出已处理项
    返回 UserMsg 列表
```

**出向 `offline_send_result(reply)`**：

```
对每条 DetokenizeMsg(uid, next_token, finished):
    若 不是(finished 且 next_token==eos):
        status.output_ids.append(next_token)
```

**终止条件的正确性**：`blocking=True` 只在 Scheduler 真正空闲时出现——没有 `last_data`（上一批已处理完）、`prefill_manager` 不可运行、`decode_manager` 不可运行（见 [scheduler.py:L90-L94](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L90-L94) 的 `blocking` 计算）。此时若 `pending_requests` 也空，意味着「没有待处理的、没有在途的、没有在算的」——三空，确实该结束。

#### 4.2.3 源码精读

异常与状态类的定义：

[python/minisgl/llm/llm.py:L17-L25](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/llm/llm.py#L17-L25) —— `RequestAllFinished(Exception)` 是空的标记异常；`RequestStatus` 是一个 dataclass，保存每个请求的 `uid`、原始 `input_ids`、以及逐步累积的 `output_ids`。它就是离线模式下的「结果收件箱」。

入向钩子，承担「发号 + 终止」双重职责：

[python/minisgl/llm/llm.py:L48-L69](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/llm/llm.py#L48-L69) —— `offline_receive_msg`。逐行看四个关键点：
- 第 49-50 行：`blocking and len(self.pending_requests) == 0` 时抛 `RequestAllFinished`，这是循环唯一的正常出口。
- 第 54-55 行：`if sum_input_len >= self.prefill_budget: break`，用 `prefill_budget`（= `max_extend_tokens`）限制单批输入 token 总量，所以 256 个请求不会一次性灌入，而是**按 prefill 预算分波**喂给 Scheduler。这正是 u4-l3 Chunked Prefill 在离线场景的自然体现。
- 第 58 行：`uid, added = self.counter + added, added + 1`，uid 从 0 起单调递增、无空洞（详见 4.3）。
- 第 60-66 行：每个新请求在 `status_map` 里登记一个空 `output_ids` 的 `RequestStatus`。

出向钩子，把每个生成 token 累积进收件箱：

[python/minisgl/llm/llm.py:L71-L75](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/llm/llm.py#L71-L75) —— `offline_send_result`。条件 `not (msg.finished and msg.next_token == self.eos_token_id)` 的含义：若该 token 同时是「请求已结束」且「正好是 eos」，则不追加（eos 本身不计入输出文本）；其余情况（还在生成中、或因 `max_tokens` 到顶而结束的非 eos 尾 token）都追加进 `output_ids`。

> 与在线模式对比：在线时 `send_result` 会把 `DetokenizeMsg` 经 ZMQ 推给 detokenizer 进程做流式 decode（见 u3-l2），那里维护着 `DecodeStatus` 的三偏移量状态机解决「半个字」问题。**离线模式跳过流式 detokenize**，直接攒 `output_ids`，最后在 `generate` 里一次性 `tokenizer.decode`。

#### 4.2.4 代码实践

1. **目标**：理解终止条件，验证「三空才退出」。
2. **步骤**：阅读 [scheduler.py:L90-L96](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L90-L96) 的 `overlap_loop`，找到 `blocking` 的计算与 `receive_msg` 的调用。
3. **观察现象**：回答——假设还有 1 个请求在 decode（`decode_manager.runnable` 为真），但 `pending_requests` 已空，此时 `blocking` 是 True 还是 False？会不会误抛 `RequestAllFinished`？
4. **预期结果**：`blocking = not(last_data or prefill_runnable or decode_runnable)`，由于 `decode_runnable` 为真，`blocking=False`，不会进入抛异常的分支。循环会继续算完这个 decode 批，直到某轮「三空」才退出。所以即使 pending 提前空了，在途请求也会被安全算完——**不会丢请求**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `offline_receive_msg` 用「抛异常」而不是返回一个特殊的「结束」消息（比如 `ExitMsg`）来终止？

**答案**：`ExitMsg` 在 `_process_one_msg` 里被处理成 `raise KeyboardInterrupt`（见 [scheduler.py:L173-L174](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L173-L174)），那语义是「外部要求关闭」，会打断当前批处理。而 `RequestAllFinished` 只在 `blocking=True`（即已经空闲、无在途批次）时从 `receive_msg` 抛出，时机安全——此时没有需要 flush 的中间状态。用独立异常也让 `generate` 能精确 `except RequestAllFinished` 而不误捕其他错误。

**练习 2**：`offline_send_result` 里为什么不是「finished 就停止追加」，而是「finished 且是 eos 才不追加」？

**答案**：请求结束有两种原因——生成了 eos，或达到了 `max_tokens` 上限（见 [scheduler.py:L153-L155](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L153-L155) 的 `finished` 计算）。后者最后一个 token 不是 eos 但仍是有效输出，应当保留。所以只在「确实命中 eos」时才丢弃该 token，其余结束场景都保留尾 token。

---

### 4.3 generate：从 pending_requests 到结果聚合

#### 4.3.1 概念说明

`generate` 是 `LLM` 暴露给用户的唯一推理入口。它把「一组 prompt + 采样参数」翻译成 `pending_requests`，启动主循环，等循环自行结束后，从 `status_map` 里把每个请求累积的 `output_ids` decode 成文本返回。

这里有一个容易被忽略的**隐形契约**：`generate` 最后用 `for i in range(len(prompts)): status = self.status_map[i]` 按下标取结果。这意味着 `status_map` 的 key 必须正好是 `0, 1, 2, ..., len(prompts)-1`。这个契约由 `offline_receive_msg` 的发号逻辑保证。

#### 4.3.2 核心流程

```
generate(prompts, sampling_params):
    重置 pending_requests=[], status_map={}, counter=0
    若 sampling_params 是单个对象 → 广播成 len(prompts) 份
    把每个 (prompt, sp) 塞进 pending_requests
    try:
        run_forever()              ← 阻塞, 直到 RequestAllFinished
    except RequestAllFinished:
        pass
    for i in range(len(prompts)):
        status = status_map[i]     ← 依赖 uid == 下标 的契约
        text = tokenizer.decode(status.output_ids)
        results.append({"text": text, "token_ids": status.output_ids})
    return results
```

**uid == 下标的契约怎么来的**：`generate` 一开始把 `counter` 重置为 0；`offline_receive_msg` 发号 `uid = self.counter + added`，每批后 `self.counter += added`。由于请求严格按 `pending_requests` 的顺序出队、且每批连续发号，最终第 k 个 prompt（k 从 0 计）拿到的 uid 就是 k。于是 `status_map[i]` 正好对应第 i 个 prompt。

#### 4.3.3 源码精读

`generate` 全文：

[python/minisgl/llm/llm.py:L77-L98](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/llm/llm.py#L77-L98) —— 注意四个细节：
- 第 82-84 行：每次 `generate` 都重置三个状态，保证多次调用互不污染（`LLM` 实例可复用，正是离线 bench 能先 warmup 再正式测的前提）。
- 第 85-86 行：单个 `SamplingParams` 自动广播成与 `prompts` 等长的列表。
- 第 89-92 行：`try/except RequestAllFinished: pass`——循环被异常终结后，平静地继续往下聚合结果。
- 第 94-97 行：按下标取结果并 decode。返回的字典同时含 `text`（decode 后文本）和 `token_ids`（原始 id 列表），后者对计算「实际输出 token 数」很关键（见 4.4）。

辅助方法 `_tokenize_one` 支持两种 prompt 形式：

[python/minisgl/llm/llm.py:L42-L46](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/llm/llm.py#L42-L46) —— 若 prompt 是字符串，走 `self.tokenizer.encode`（注意：`Scheduler` 已经在 [scheduler.py:L69](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L69) 加载了 `self.tokenizer`，离线模式直接复用，省掉独立 tokenizer 进程）；若是 token id 列表，直接包成 int32 张量。离线 bench 大量用第二种形式以精确控制输入长度。

#### 4.3.4 代码实践

1. **目标**：亲手用 `LLM.generate` 跑一次最小推理，验证返回结构。
2. **步骤**：写一个脚本（示例代码）：
   ```python
   from minisgl.core import SamplingParams
   from minisgl.llm import LLM

   llm = LLM("Qwen/Qwen3-0.6B")
   out = llm.generate(["你好，请用一句话介绍你自己。"], SamplingParams(temperature=0.6, max_tokens=64))
   print(out[0]["text"])
   print("token count:", len(out[0]["token_ids"]))
   ```
3. **观察现象**：第一行打印生成文本，第二行打印 token 数（应 ≤ 64，取决于是否命中 eos）。
4. **预期结果**：得到一段中文文本与一个 ≤ 64 的 token 计数。无 GPU 环境下无法运行，标注「待本地验证」；可改为阅读 [llm.py:L93-L98](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/llm/llm.py#L93-L98) 确认返回结构。

#### 4.3.5 小练习与答案

**练习 1**：如果用户传入的 `prompts` 列表里有重复的相同字符串，`status_map[i]` 还能正确对应吗？

**答案**：能。`status_map` 的 key 是 uid（按出队顺序的整数下标），与 prompt 内容无关。即使两个 prompt 字符串完全相同，它们也会拿到不同的 uid（比如 i 和 i+1），`status_map[i]` 与 `status_map[i+1]` 是两条独立结果。uid 是身份，内容可以重复。

**练习 2**：为什么 `generate` 要返回 `token_ids` 而不只返回 `text`？

**答案**：因为文本 decode 后无法精确还原 token 数量（一个字可能对应多个 token，反之亦然）。基准测试需要精确的「实际输出 token 数」来算吞吐（见 [bench_wildchat.py:L128-L132](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/benchmark/offline/bench_wildchat.py#L128-L132)），所以直接暴露 `token_ids` 让 `len(token_ids)` 作为 ground truth。

---

### 4.4 离线基准与吞吐统计

#### 4.4.1 概念说明

离线基准回答一个问题：**「把一批请求一次性灌满，GPU 每秒能吐多少 token？」** 它不关心到达节奏，把所有 prompt 同时提交（`generate` 一次性传入全部），让 Scheduler 自由地拼成最大 batch，从而逼近吞吐上限。

吞吐的数学定义很简单——总输出 token 数除以墙钟时间：

\[
\text{Throughput} = \frac{\sum_{i} \text{output\_tokens}_i}{T_{\text{wall}}}
\]

但「总输出 token 数」有两种算法，对应两种 bench 脚本，这点务必分清。

#### 4.4.2 核心流程

`benchmark/offline/bench.py` 的流程：

```
固定随机种子; 设 num_seqs=256, 输入/输出长度均 100~1024 随机
构造 LLM(page_size=256, max_extend_tokens=16384, cuda_graph_max_bs=256, ...)
生成 256 个随机 token id prompt + 各自的 SamplingParams(ignore_eos=True, max_tokens=随机)
warmup: llm.generate(["Benchmark: "], ...)     ← 触发 flashinfer/kernel/graph 的懒初始化
t0 = time.time()
llm.generate(prompt_token_ids, sampling_params) ← 正式计时
t = time.time() - t0
total_tokens = sum(sp.max_tokens)               ← 用预算算(因 ignore_eos=True, 实际==预算)
throughput = total_tokens / t
打印 Total / Time / Throughput
```

两个脚本的关键差异：

| 维度 | bench.py | bench_wildchat.py |
| --- | --- | --- |
| prompt 来源 | 随机 token id | 真实 WildChat 对话，套 `apply_chat_template` |
| `ignore_eos` | `True` | `False` |
| 输出 token 计数 | `sum(sp.max_tokens)`（预算） | `sum(len(token_ids))`（实际） |
| 目的 | 测吞吐极限（固定长度） | 测真实分布下的吞吐 |

#### 4.4.3 源码精读

`bench.py` 的核心，构造 `LLM` 与 warmup：

[benchmark/offline/bench.py:L17-L32](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/benchmark/offline/bench.py#L17-L32) —— 注意构造参数：`page_size=256`（大页，配合 H200/flashinfer 的 paged attention，见 u6-l1）、`max_extend_tokens=16384`（单批 prefill token 预算）、`cuda_graph_max_bs=256`（CUDA Graph 捕获到 256，见 u5-l3）。第 32 行的 warmup `llm.generate(["Benchmark: "], ...)` 注释明说「to warm up flashinfer」——第一次前向会触发 flashinfer 的 `plan`、kernel JIT 编译、CUDA Graph 捕获等懒初始化，若不 warmup 这些开销会被算进正式计时，拉低吞吐。

计时与吞吐公式：

[benchmark/offline/bench.py:L33-L38](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/benchmark/offline/bench.py#L33-L38) —— `total_tokens = sum(sp.max_tokens for sp in sampling_params)`。**这里用 `max_tokens` 而非实际输出**，是因为所有请求都 `ignore_eos=True`（第 29 行），每个请求一定会生成满 `max_tokens` 个 token，所以预算即实际。throughput = total_tokens / t。

对照真实数据版 `bench_wildchat.py`，它 `IGNORE_EOS=False`，所以必须数实际 token：

[benchmark/offline/bench_wildchat.py:L123-L139](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/benchmark/offline/bench_wildchat.py#L123-L139) —— 这里同时打印了「Output budget」（= `sum(sp.max_tokens)`，预算）与「Actual output」（= `sum(len(token_ids))`，实际），两者差异体现了 eos 提前停止的效果。throughput 用实际值 `total_output_tokens / t`。

> README 给出的离线基准配置（[README.md:L145-L152](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/README.md#L145-L152)）：1×H200、Qwen3-0.6B / Qwen3-14B、256 序列、输入与输出长度均 100~1024 随机。并且明确提示：「Set `MINISGL_DISABLE_OVERLAP_SCHEDULING=1` for ablation study on overlap scheduling」——这正是本讲综合实践的依据。

#### 4.4.4 代码实践

1. **目标**：跑一次离线 bench 并读懂输出。
2. **步骤**：在有 GPU 的机器上 `cd` 到仓库根目录，执行 `python benchmark/offline/bench.py`（需先按 u1-l2 安装好依赖与模型）。
3. **观察现象**：脚本先做一次 warmup（会有首次加载模型、编译 kernel 的等待），随后打印一行 `Total: ...tok, Time: ...s, Throughput: ...tok/s`。
4. **预期结果**：在 H200 + Qwen3-0.6B 上应得到数万 tok/s 量级的吞吐（具体数值「待本地验证」，依赖硬件）。若内存不足可调小 `num_seqs`。
5. **若无法运行**：改为阅读 [bench.py:L33-L38](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/benchmark/offline/bench.py#L33-L38)，手算：若 256 序列、平均 max_tokens≈560、总时间 8 秒，则 throughput ≈ 256×560/8 ≈ 17920 tok/s。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `bench.py` 敢用 `sum(sp.max_tokens)` 当总 token 数，而 `bench_wildchat.py` 不敢？

**答案**：`bench.py` 设了 `ignore_eos=True`，请求不会因命中 eos 提前结束，必定生成满 `max_tokens`，所以预算==实际。`bench_wildchat.py` 用 `IGNORE_EOS=False`，真实 prompt 会在语义完整时命中 eos 提前结束，实际 token 数 < 预算，必须用 `len(token_ids)` 数实际值。

**练习 2**：去掉 warmup 那一行（第 32 行）会让 throughput 变高还是变低？

**答案**：变低。第一次 `generate` 会触发 flashinfer `plan`、kernel JIT、CUDA Graph 捕获等一次性开销，这些会被计入正式计时的时间 `t`，使分母变大、throughput 变小。warmup 的作用就是把这些懒初始化开销「隔离」在计时窗口之外。

---

### 4.5 在线基准与延迟统计

#### 4.5.1 概念说明

在线基准回答另一个问题：**「当用户按真实节奏陆续发来请求时，每个用户感受到的延迟如何？」** 它需要先把服务跑起来（`python -m minisgl`），再用一个异步 HTTP 客户端按 trace 里的时间戳「回放」请求，记录每个请求首字、逐字、结束的时间点，最后算出 TTFT、TPOT、E2E 的分位数。

三个核心延迟指标：

- **TTFT（Time To First Token，首字延迟）**：从发出请求到收到第一个 token 的时间。包含排队 + prefill。
- **TPOT（Time Per Output Token，逐字延迟）**：收到第一个 token 之后，平均每多一个 token 的时间。反映 decode 速度。
- **E2E（端到端延迟）**：从发请求到收完最后一个 token 的总时间。

在线基准还能通过 `scale_traces` 压缩或拉伸请求到达时间，模拟「高负载/低负载」，观察延迟随并发变化的曲线。

#### 4.5.2 核心流程

`bench_qwen.py` 的流程：

```
下载 Qwen trace 文件(若本地不存在)
read_qwen_trace → N=1000 条 BenchmarkTrace(timestamp, message, output_length, ...)
连上已运行服务(127.0.0.1:1919)的 OpenAI 异步客户端
for scale in [0.4, 0.5, 0.6, 0.7, 0.8, 1.6]:     ← 从快(高负载)到慢(低负载)
    traces = scale_traces(TRACES, scale)          ← 把到达时间戳 × scale
    results = benchmark_trace(client, traces, MODEL)  ← 按时间戳回放, 并发收
    process_benchmark_results(results)            ← 打印 TTFT/TPOT/E2E/吞吐
```

`benchmark_trace` 内部：把所有 trace 的第一个时间戳对齐到「现在」，为每个请求算出「该发的目标时刻」，到点前 `asyncio.sleep`，到点即并发 `await benchmark_one(...)` 流式收 token，逐块记 `time.perf_counter()` 时间戳。

#### 4.5.3 源码精读

在线主脚本：

[benchmark/online/bench_qwen.py:L37-L51](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/benchmark/online/bench_qwen.py#L37-L51) —— 第 41 行 `SCALES = [0.4, 0.5, 0.6, 0.7, 0.8, 1.6]` 注释「from fast to slow」。`scale` 乘在到达时间间隔上：`scale` 越小，请求挤得越紧、并发越高、负载越大；`1.6` 则把请求拉开、负载最低。所以数组从 0.4（最快/最高负载）排到 1.6（最慢/最低负载）。

trace 回放——按时间戳调度并发请求：

[python/minisgl/benchmark/client.py:L287-L309](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/benchmark/client.py#L287-L309) —— `benchmark_trace`。第 298 行 `offset = min(timestamp) - 1` 把所有时间戳对齐到「现在」为起点；第 300-305 行的 `benchmark_timed` 先 `await asyncio.sleep(max(0, target - now))` 等到目标时刻再发，再用 `asyncio.gather` 让所有请求并发跑完。每个请求内部（[client.py:L236-L248](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/benchmark/client.py#L236-L248)）边流式收边把每个 chunk 的 `time.perf_counter()` 追加进 `tics`，这就是后续所有统计的原始时间序列。

到达节奏缩放：

[python/minisgl/benchmark/client.py:L479-L495](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/benchmark/client.py#L479-L495) —— `scale_traces` 把每个 `(timestamp - min_tic) * scale` 重新作为到达时刻，并按时间排序。一行公式就把同一份 trace 变成不同负载强度。

统计与吞吐——把 tics 序列翻译成 TTFT/TPOT/E2E：

[python/minisgl/benchmark/client.py:L324-L384](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/benchmark/client.py#L324-L384) —— `process_benchmark_results`。逐行看：
- 第 327-333 行：对每个请求的 `tics` 求相邻差 `deltas`；`deltas[0]`（首字间隔）进 `first_times`（→ TTFT）；`deltas[1:]`（后续逐字间隔）展平进 `accum_times`（→ TPOT 样本）。
- 第 335 行：`e2e_times = tics[-1] - tics[0]`（每请求端到端）。
- 第 358-360 行：分别对三类时间算 avg / p50 / p90 / p99 / max，TTFT 与 TPOT 乘 1000 转毫秒。
- 第 362-364 行：`dur = max(所有 tics) - min(所有 tics)`，是整批回放的总跨度。
- 第 367-384 行：`num_tokens = sum(len(tic))`，`throughput = num_tokens / dur`（tok/s）和 `num_requests / dur`（req/s）。

> 注意在线吞吐分母是 `dur`（trace 回放的总时长，含请求间的等待间隙），而离线吞吐分母是纯计算时间 `t`。所以同样硬件下，在线吞吐数字通常远低于离线——它们度量的是不同东西，**不能直接横比**。

#### 4.5.4 代码实践

1. **目标**：理解 trace 回放如何制造不同负载。
2. **步骤**：阅读 [client.py:L479-L495](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/benchmark/client.py#L479-L495) 的 `scale_traces`，假设原始 trace 有两个请求、到达时刻分别为 `t=0` 和 `t=10` 秒。
3. **观察现象**：分别代入 `scale=0.4` 和 `scale=1.6`，算出第二个请求的新到达时刻。
4. **预期结果**：`min_tic=0`。`scale=0.4` 时第二个请求到 `(10-0)*0.4=4` 秒（更密集、更高负载）；`scale=1.6` 时到 `16` 秒（更稀疏、更低负载）。这解释了为何 `SCALES` 从 0.4 到 1.6 代表「from fast to slow」。

#### 4.5.5 小练习与答案

**练习 1**：TTFT 的样本为什么取 `deltas[0]`，TPOT 的样本为什么取 `deltas[1:]`？

**答案**：`tics` 的第一个点是「发出请求时刻」，第二个点是「收到第一个 token 时刻」，故 `deltas[0] = tics[1] - tics[0]` 正是首字延迟 TTFT。从第二个点起，相邻差就是「每多收一个 token 的时间」，即 TPOT 样本，所以是 `deltas[1:]`。

**练习 2**：在线基准为什么必须先把服务用 `python -m minisgl` 跑起来，而离线基准不用？

**答案**：在线基准通过 OpenAI HTTP 客户端访问 `http://127.0.0.1:1919/v1`（[bench_qwen.py:L42](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/benchmark/online/bench_qwen.py#L42)），需要一个运行中的 FastAPI 服务进程。离线基准直接 `import` 并实例化 `LLM`，在脚本进程内完成推理，不需要任何独立服务。这也决定了在线基准天然支持 `--tp` 多卡（服务侧），而离线基准受限于单卡 `LLM`。

---

## 5. 综合实践

把本讲的核心串起来：用 `LLM.generate` 跑离线 bench，在 `page_size` 与 overlap 两个维度上做对照实验，解释吞吐差异。这是 README 明确建议的 ablation（[README.md:L143](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/README.md#L143)）。

### 实践目标

量化两个工程参数对离线吞吐的影响，并用本讲学到的源码知识解释「为什么」。

### 操作步骤

1. **准备一个可复用的小脚本** `my_bench.py`（示例代码，放在仓库任意位置，不要放进 `benchmark/` 以免污染）：
   ```python
   import time
   from random import randint, seed
   from minisgl.core import SamplingParams
   from minisgl.llm import LLM

   def bench(page_size, num_seqs=64):
       seed(0)
       llm = LLM(
           "Qwen/Qwen3-0.6B",
           page_size=page_size,
           max_extend_tokens=16384,
           cuda_graph_max_bs=256,
       )
       prompts = [[randint(0, 10000) for _ in range(randint(100, 1024))] for _ in range(num_seqs)]
       sps = [SamplingParams(temperature=0.6, ignore_eos=True, max_tokens=randint(100, 1024)) for _ in range(num_seqs)]
       llm.generate(["warmup"], SamplingParams(temperature=0.1))  # warmup
       t = time.time()
       llm.generate(prompts, sps)
       t = time.time() - t
       total = sum(sp.max_tokens for sp in sps)
       print(f"page_size={page_size}: {total/t:.2f} tok/s  ({t:.2f}s)")
       del llm

   bench(page_size=1)
   bench(page_size=256)
   ```
2. **对照 overlap 开/关**：分别用两种方式跑上面的脚本——
   - overlap 开（默认）：`python my_bench.py`
   - overlap 关：`MINISGL_DISABLE_OVERLAP_SCHEDULING=1 python my_bench.py`
   （环境变量名见 [env.py:L69](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/env.py#L69) 与 [env.py:L50](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/env.py#L50)：前缀 `MINISGL_` + `DISABLE_OVERLAP_SCHEDULING`。）
3. **记录四组数据**：`{page_size=1, page_size=256} × {overlap on, overlap off}` 的吞吐。

### 需要观察的现象与预期解释

- **`page_size=256` 通常快于 `page_size=1`**：大页让 paged attention 的页表更紧凑、kernel 访问更连续，且 flashinfer 后端在 Hopper/Blackwell 上对大页有优化（见 u7-l1 的 `auto` 后端选择与 u6-l1 的池布局）。但注意：`page_size` 必须与注意力后端兼容——FlashInfer 后端硬约束 `page_size == 1`（见 u7-l2），所以 `page_size=256` 实际会走 trtllm 或 fa 后端，这点要结合日志确认（「待本地验证」具体后端）。
- **overlap on 快于 overlap off**：overlap 把 CPU 调度与 GPU 计算重叠，每轮耗时从约 `T_cpu + T_gpu` 降到约 `max(T_cpu, T_gpu)`（见 u4-l1）。关闭后（`normal_loop`）变回串行，吞吐下降。这正是 `bench.py` warmup 与正式计时间隔的设计动机。

### 预期结果

得到一张 2×2 的吞吐对照表。若硬件受限无法跑满 64 序列，可把 `num_seqs` 调小到 8~16，仍能观察到方向性趋势。具体数值「待本地验证」。

> 如果完全没有 GPU：改为「源码阅读型实践」——在 [scheduler.py:L120-L131](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L120-L131) 处对照 `overlap_loop`（L83-L106）与 `normal_loop`（L108-L118），指出后者少了「engine stream 与 self.stream 的交错」，从而在概念上解释 overlap 为何更快。

## 6. 本讲小结

- `LLM` 直接继承 `Scheduler`，靠 `SchedulerConfig(offline_mode=True)` 让 I/O 层在 [io.py:L30-L33](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L30-L33) 提前 return、不创建任何 ZMQ 队列；主循环 `run_forever` 一字不改地复用。
- 离线通路的两个 I/O 钩子是 `offline_receive_msg`（从 `pending_requests` 取 `UserMsg`，受 `prefill_budget` 分波，并在「空闲且无待处理」时抛 `RequestAllFinished`）与 `offline_send_result`（把每个 token 累积进 `status_map`）。
- `generate` 把异常驱动的循环封装成同步调用：填 `pending_requests` → `run_forever` → 捕获 `RequestAllFinished` → 按 uid（== 下标）从 `status_map` 聚合文本；返回同时含 `text` 与 `token_ids`。
- 离线基准度量**聚合吞吐** = 总输出 token / 纯计算墙钟时间；`ignore_eos=True` 时可用 `sum(max_tokens)` 当总 token，否则须数实际 `len(token_ids)`。warmup 用于隔离懒初始化开销。
- 在线基准通过 HTTP 客户端按 trace 时间戳回放请求，度量 **TTFT/TPOT/E2E 分位**与吞吐；`scale_traces` 调节到达节奏模拟不同负载。在线吞吐分母含等待间隙，与离线不可直接横比。
- 关键对照：`page_size`（页大小，影响后端选择与访存连续性）与 `MINISGL_DISABLE_OVERLAP_SCHEDULING`（overlap 开关，影响 CPU/GPU 重叠）是离线吞吐的两个主要可调维度。

## 7. 下一步学习建议

- 想理解在线基准背后那个「被回放」的服务？回到 **u3-l1 API Server 与 OpenAI 兼容接口**，看 `/v1/chat/completions` 如何把流式 `UserReply` 包成 SSE。
- 想理解离线 `LLM` 之所以能省掉 tokenizer 进程，是因为它复用了 `Scheduler` 内置的 `self.tokenizer`——可回顾 **u4-l1 Scheduler 主循环**。
- 想深入「`page_size` 为何影响后端」？继续读 **u7-l1 注意力后端抽象与 Hybrid** 与 **u7-l2 FlashInfer 后端实现**（其中 `page_size == 1` 的硬约束）。
- 想验证调度正确性？下一讲 **u11-l2 测试体系与质量保证** 会讲 `tests/` 下的 `test_scheduler`、`test_cache_allocate` 如何在无完整服务的情况下用 ZMQ 直接驱动 scheduler，与本讲的「进程内 LLM」是两种互补的测试入口。

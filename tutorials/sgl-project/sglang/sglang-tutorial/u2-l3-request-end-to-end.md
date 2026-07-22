# 请求端到端流转

## 1. 本讲目标

本讲把前几讲（u2-l1 多进程拓扑、u2-l2 启动流程、u2-l5 RuntimeContext）建立的三进程「环」真正跑通：跟踪**一条请求**从 HTTP 进入，到被 `TokenizerManager` 分词转发、`Scheduler` 调度执行、`DetokenizerManager` 解码回写，最终流式返回给客户端的完整生命周期。

学完本讲你应该能够：

- 说出 `TokenizerManager` 的两个职责（**入向**：收请求、分词、转发；**出向**：事件循环收结果、按 `rid` 分发），并定位 `generate_request` 与 `handle_loop` 的源码位置。
- 说出 `DetokenizerManager` 的「收 → 解 → 发」三步循环，以及它如何用 `DecodeStatus` 做**增量解码**（incremental coding）。
- 对照 `io_struct.py` 说出一条请求在进程间演化时携带的关键消息结构体：`TokenizedGenerateReqInput` → `BatchTokenIDOutput` → `BatchStrOutput`，以及取消用的 `AbortReq`。
- 为一条流式 chat 请求，标注 `rid`、`input_ids`、`output_ids`、`decoded_text` 在每个进程的读写操作。

> 承接：u2-l1 已建立 `TokenizerManager → Scheduler → DetokenizerManager` 的 ZMQ 环拓扑与三条 PUSH/PULL 边；u2-l2 已讲清这三进程由 `Engine._launch_subprocesses` 拉起并套上 FastAPI 外壳；u2-l5 已讲清运行期配置由 `RuntimeContext` 的命名空间袋（`get_serving()`/`get_observability()` 等）提供。本讲不再重复启动与拓扑，只聚焦**请求在环上流动时每一步发生了什么**，并在途中指出各进程通过哪些访问器读取运行期配置。`Scheduler` 内部的调度细节（prefill/decode 批构造、策略）留到 u3，本讲只把它当作环上的「黑盒计算节点」。

## 2. 前置知识

- **进程与 IPC**：SGLang 用多个操作系统进程而非线程来组织运行时。进程间不共享内存，靠 ZeroMQ（ZMQ）的 socket 传消息。回顾 u2-l1：每个 socket 有一个「地址名」（如 `scheduler_input_ipc_name`），PUSH 端发、PULL 端收。
- **异步事件循环（asyncio）**：`TokenizerManager` 跑在主进程，用 `asyncio` 协程并发处理成千上万个请求。每个请求被 `await` 挂起，等结果回来再被「唤醒」继续。
- **msgspec 与 msgpack**：跨进程消息用 `msgspec.Struct` 定义，序列化默认走 msgpack（比 pickle 快且可校验类型）。`tag=True` 让接收端能根据消息自带标签还原成确切的子类。
- **rid（request id）**：每条请求的唯一标识（一个 `uuid4().hex` 字符串）。它是把「散落在三个进程里的同一请求」串起来的唯一线索——所有跨进程消息都带 `rid`。
- **增量解码**：模型每个 decode step 只新增少量 token。如果把「到目前为止的全部 token」每次都整体解码成文本再整体发送，会重复解码旧 token、产生 O(n²) 开销。增量解码只解码「新增片段」，靠两个偏移量（`surr_offset`/`read_offset`）记住进度。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `python/sglang/srt/managers/io_struct.py` | 跨进程消息结构体字典 | 输入消息 `TokenizedGenerateReqInput`、输出消息 `BatchTokenIDOutput`/`BatchStrOutput`、取消 `AbortReq`、序列化工具 `sock_send/recv` |
| `python/sglang/srt/managers/tokenizer_manager.py` | 主进程：分词 + 事件循环 | `generate_request`、`handle_loop`、`_handle_batch_output`、`_wait_one_response`、`ReqState` |
| `python/sglang/srt/managers/detokenizer_manager.py` | 子进程：token id → 文本 | `event_loop`、`DecodeStatus`、`_decode_batch_token_id_output`、`handle_batch_token_id_out` |
| `python/sglang/srt/entrypoints/http_server.py` | HTTP 入口（辅助引用） | `/generate` 端点如何调用 `generate_request` |
| `python/sglang/srt/managers/scheduler_components/ipc_channels.py` | Scheduler 的 IPC socket（辅助引用） | 确认环上两条边的 socket 名 |

---

## 4. 核心概念与源码讲解

### 4.1 io_struct：跨进程消息字典

#### 4.1.1 概念说明

`io_struct.py` 是「环」上所有消息的**唯一定义处**。文件顶部明确写道：

> The definition of objects transferred between different processes (TokenizerManager, DetokenizerManager, Scheduler).

它解决两个问题：

1. **进程间要传什么**：把每条请求在生命周期各阶段的「快照」定义成结构体，发送方填字段、接收方读字段，双方对字段含义有共识。
2. **怎么传**：用 `msgspec.Struct + tag=True` 让消息可被 msgpack 高效序列化，接收端还能自动还原成确切子类。

理解本讲的关键是区分两类「请求对象」：

- `GenerateReqInput`：**进程内**的 Python `@dataclass`，由 HTTP 层或 `Engine` 构造，**不上 ZMQ**。它字段最全（文本、图片、采样参数、流式开关……），是「原始请求」。
- `TokenizedGenerateReqInput`：**分词后**的 msgspec 结构体，**才是真正进线（on the wire）的消息**。它把文本换成了 `input_ids`（token id 数组），并剔除了 tokenizer 进程不需要的多模态原始数据。

#### 4.1.2 核心流程

一条语言模型请求在环上演化的消息链：

```
HTTP 层构造
  GenerateReqInput (进程内, 不上线)
     │  TokenizerManager 分词
     ▼
  TokenizedGenerateReqInput ──(scheduler_input_ipc_name)──► Scheduler
     │  Scheduler 调度 + 前向 (u3 详解, 本讲当黑盒)
     ▼
  BatchTokenIDOutput ──(detokenizer_ipc_name)──► DetokenizerManager
     │  Detokenizer 增量解码 token id → 文本
     ▼
  BatchStrOutput ──(tokenizer_ipc_name)──► TokenizerManager → 客户端
```

注意输入侧是「单条」结构体（`...ReqInput`），输出侧是「批次」结构体（`Batch...Output`）——因为 Scheduler 把多条请求合并成一个 batch 做前向，回吐时也是整批回。

取消（abort）走独立的 `AbortReq`，由 TokenizerManager 单独发给 Scheduler。

#### 4.1.3 源码精读

**消息基类与标签**：所有单条级消息继承 `BaseReq`，批次级继承 `BaseBatchReq`，二者都用 `tag=True`：

- [`io_struct.py:74-82`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/io_struct.py#L74-L82) — `BaseReq`，带 `rid` 与 `http_worker_ipc` 两个路由字段。`tag=True` 让 msgpack 编码时自动写入类型标签，接收端 `msgpack_decode` 能据此还原成 `TokenizedGenerateReqInput` 等确切子类。
- [`io_struct.py:85-96`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/io_struct.py#L85-L96) — `BaseBatchReq`，用 `rids`（复数）和 `http_worker_ipcs` 承载整批路由信息。

**输入消息**：

- [`io_struct.py:154-158`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/io_struct.py#L154-L158) — `GenerateReqInput` 是 `@dataclass`（注意：这是历史遗留，新代码应按 `no-dataclasses` 规范用 `msgspec.Struct`）。字段含 `text`/`input_ids`/`sampling_params`/`stream` 等，是「原始请求」，进程内使用，不上 ZMQ。
- [`io_struct.py:788-798`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/io_struct.py#L788-L798) — `TokenizedGenerateReqInput(BaseReq)`：**真正上线**的输入消息。`input_text` 保留原文、`input_ids` 是分词后的 token 数组（`array` 类型，比 list 更紧凑）、`sampling_params` 是已构造好的 `SamplingParams` 对象。`mm_inputs` 等不透明字段经 `PickleWrapper` 包装。
- [`io_struct.py:895-907`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/io_struct.py#L895-L907) — `BatchTokenizedGenerateReqInput(BaseBatchReq)`：把多条 `TokenizedGenerateReqInput` 包成一批一次发送，减少 ZMQ 往返。

**输出消息**：

- [`io_struct.py:1209-1221`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/io_struct.py#L1209-L1221) — `BatchTokenIDOutput`：Scheduler→Detokenizer 的批次输出。核心增量解码字段：`decode_ids`（每请求的 token id 数组，每步追加）、`read_offsets`（已读取到哪）、`finished_reasons`（`None` 表示仍在流式生成）、`decoded_texts`（初始化用）。
- [`io_struct.py:1300-1312`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/io_struct.py#L1300-L1312) — `BatchStrOutput`：Detokenizer→Tokenizer 的批次输出。把 `decode_ids` 解码成 `output_strs`（文本），并透传 `output_ids` 与各类统计字段。

**取消消息**：

- [`io_struct.py:1795-1806`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/io_struct.py#L1795-L1806) — `AbortReq`，可单条（带 `rid`）或全量（`abort_all=True`）取消。

**序列化工具**：

- [`io_struct.py:2253-2266`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/io_struct.py#L2253-L2266) — `sock_send/sock_recv`：ZMQ 收发的统一封装。默认走 `msgpack_encode/decode`，设了 `SGLANG_USE_PICKLE_IPC` 时退回 `socket.send_pyobj`。
- [`io_struct.py:2153-2173`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/io_struct.py#L2153-L2173) — `enc_hook`：把 `array`/`torch.Tensor`/`np.ndarray` 这类 msgpack 不原生支持的类型转成 `(元信息, 原始字节)` 元组。`dec_hook` 是它的逆操作。不透明对象（如多模态输入）则经 [`wrap_as_pickle`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/io_struct.py#L2136-L2141) 包成 `PickleWrapper`。

#### 4.1.4 代码实践

**实践目标**：在 `io_struct.py` 里把「输入消息、批次输出、取消」三类结构体找全，并验证「`GenerateReqInput` 不上线」这一关键区分。

**操作步骤**：

1. 打开 `python/sglang/srt/managers/io_struct.py`。
2. 用编辑器搜索 `class GenerateReqInput`、`class TokenizedGenerateReqInput`、`class BatchTokenIDOutput`、`class BatchStrOutput`、`class AbortReq`。
3. 对每个类，记录它的父类（`BaseReq` 还是 `BaseBatchReq`，或都不是）和它携带的 `rid`/`rids` 字段。

**需要观察的现象 / 预期结果**：

- `GenerateReqInput` 与 `EmbeddingReqInput` 是普通 `@dataclass`，**不**继承 `BaseReq`——这正是它们不上 ZMQ 的体现。文件末尾 [`_check_all_req_types`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/io_struct.py#L2102-L2105) 把这两个名字显式列进 `_IGNORE_REQ_TYPES_CHECK`，跳过「必须是 BaseReq 子类」的校验。
- 其余以 `Req`/`Input`/`Output` 结尾的类都继承 `BaseReq` 或 `BaseBatchReq`。

> 本步骤为源码阅读型实践，无需运行服务。

#### 4.1.5 小练习与答案

**练习 1**：为什么输入侧用 `TokenizedGenerateReqInput` 而不直接把 `GenerateReqInput` 发给 Scheduler？

**参考答案**：`GenerateReqInput` 里的 `text` 是字符串，Scheduler 需要 token id 才能做前向；多模态原始图片数据体积大、Scheduler 也用不上。TokenizerManager 在主进程完成分词与多模态预处理后，把结果压成更紧凑、更贴近计算需求的 `TokenizedGenerateReqInput` 再上线，既减少传输量，也让 Scheduler 专注调度。

**练习 2**：`BatchTokenIDOutput` 里 `finished_reasons[i]` 为 `None` 代表什么？

**参考答案**：代表第 `i` 条请求尚未结束，仍在流式生成中。Detokenizer 据此走增量分支（只发新增文本），TokenizerManager 也据此判断是否继续等待下一批。

---

### 4.2 TokenizerManager：分词、转发与事件循环

#### 4.2.1 概念说明

`TokenizerManager` 跑在**主进程**（u1-l4 已确认），是整个环的「咽喉」，同时承担**入向**和**出向**两条数据流：

- **入向（请求方向）**：接收 HTTP/`Engine` 发来的 `GenerateReqInput`，分词成 `TokenizedGenerateReqInput`，通过 `send_to_scheduler` 推给 Scheduler。
- **出向（响应方向）**：一个独立的事件循环 `handle_loop` 持续从 `recv_from_detokenizer` 拉取 `BatchStrOutput`，按 `rid` 找到对应请求的 `ReqState`，把结果塞进它的 `out_list` 并置位 `event`，唤醒正在 `await` 的请求协程。

这种「生产者（`handle_loop`）写 + 消费者（`generate_request`）等事件」的模式，是单进程内用 asyncio 实现高并发的标准做法——成千上万条请求协程同时 `await state.event.wait()`，谁的结果先到就先唤醒谁。

#### 4.2.2 核心流程

入向（`generate_request`，被 HTTP 端点调用）：

```
generate_request(obj)
  ├─ normalize_batch_and_arguments()   # 补默认值、展开并行采样、生成 rid
  ├─ _init_req_state(obj)              # 为每个 rid 建 ReqState 并存入 rid_to_state
  ├─ _tokenize_one_request(obj)        # 文本/图片 → TokenizedGenerateReqInput
  ├─ _send_one_request(tokenized_obj)  # _dispatch_to_scheduler → ZMQ PUSH
  └─ async for response in _wait_one_response(obj):  # 挂起等 event
        yield response                 # 流式逐块 yield 给 HTTP 层
```

出向（`handle_loop`，后台常驻协程）：

```
while True:
  recv_obj = await async_sock_recv(recv_from_detokenizer)   # 拉 BatchStrOutput
  if isinstance(recv_obj, (BatchStrOutput, BatchEmbeddingOutput, BatchTokenIDOutput)):
      await _handle_batch_output(recv_obj)   # 按 rid 写 ReqState、置 event
  else:
      _result_dispatcher(recv_obj)            # AbortReq/权重更新等控制类
```

#### 4.2.3 源码精读

**HTTP 入口**：[`http_server.py:825-841`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/http_server.py#L825-L841) — `/generate` 端点把 `GenerateReqInput` 交给 `tokenizer_manager.generate_request(obj, request)`，流式 `async for` 每一块返回。这就是请求进入环的第一跳。

**请求状态容器**：[`tokenizer_manager.py:181-201`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L181-L201) — `ReqState` 是「请求在 TokenizerManager 内的驻留状态」：`event`（唤醒信号）、`out_list`（待消费的输出块队列）、`finished`、累计的 `output_ids` 与 `text_chunks`、各种 logprob 累加器。`rid_to_state` 这个字典把 `rid → ReqState` 映射保存起来，是入向与出向两条流的**汇合点**。

**入向主流程**：[`tokenizer_manager.py:631-688`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L631-L688) — `generate_request`。关键步骤：先 [`normalize_batch_and_arguments`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/io_struct.py#L334-L358)（补默认值、生成 `rid`），再 `_init_req_state`，然后在 `model_update_lock.reader_lock` 下分词并发送，最后 `async for` 等响应。`try/except` 末尾的 `_discard_pending_req_states`（[L679-L688](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L679-L688)，调用点在 L687）负责清理「分词阶段就失败、还没到 Scheduler」的残留 `ReqState`，避免内存泄漏。

**建立状态**：[`tokenizer_manager.py:2992-3035`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L2992-L3035) — `_init_req_state` 把单条/批次统一成 `(rid, sub_obj)` 列表，为每个 `rid` 新建 `ReqState` 并写入 `rid_to_state`；若 `rid` 已存在则抛 `Duplicate request ID`（L3029）。

**转发到 Scheduler**：[`tokenizer_manager.py:421-442`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L421-L442) — `init_ipc_channels` 建两个 socket：`send_to_scheduler`（PUSH 到 `scheduler_input_ipc_name`）和 `recv_from_detokenizer`（PULL 自 `tokenizer_ipc_name`）。[`_dispatch_to_scheduler`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L444-L447) 是实际发送点（多 tokenizer 模式下还会盖 `http_worker_ipc` 戳）。[`_send_one_request`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L1374-L1384) 在发送前后打点 `time_stats` 并 `wrap_pickle_fields`。

**出向事件循环**：[`tokenizer_manager.py:1893-1906`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L1893-L1906) — `handle_loop`。它由 [`auto_create_handle_loop`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L1868-L1892) 在首个请求到来时懒启动（`loop.create_task(self.handle_loop)`）。循环体极简：收一条 → 是输出类就 `_handle_batch_output`，否则走控制类分发器。

**按 rid 分发结果**：[`tokenizer_manager.py:1908-2199`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L1908-L2199) — `_handle_batch_output`。遍历 `recv_obj.rids`，对每个 `rid`：用 `self.rid_to_state.get(rid)` 取出 `ReqState`（取不到就记错误并跳过）；组装 `meta_info`；根据是否流式、是否增量，把 `output_strs[i]` 与 `output_ids[i]` 累加进 `state`，构造 `out_dict` 追加到 `state.out_list`；最后 `state.event.set()` 唤醒等待方。请求 `finished` 时从 `rid_to_state` 删除（[L2174](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L2174)），并触发指标采集与 crash dump。

**消费方等待**：[`tokenizer_manager.py:1489-1596`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L1489-L1596) — `_wait_one_response`。`await state.event.wait()` 挂起，被唤醒后原子地把 `state.out_list` 取空（[L1517-L1521](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L1517-L1521)），增量流式时还会用 [`_coalesce_streaming_chunks`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L1408-L1443) 把积压的多块合并成一块再 `yield`，避免漏 token。它还会检测客户端断连并 `abort_request`。

**配置读取的命名空间化（承接 u2-l5）**：以上讲的是请求在 `TokenizerManager` 内的流转，但要注意，本进程读取运行期配置已从 `self.server_args.x` 改为走 `runtime_context` 的命名空间访问器。例如 `_handle_batch_output` 组装 `meta_info` 时读的是 [`get_serving().weight_version`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L1939)（[tokenizer_manager.py:1939](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L1939)），而 `__init__` 里日志开关走 `get_observability().log_requests`、LoRA 路径走 `get_lora().lora_paths`、分离模式走 `get_disagg().disaggregation_mode`、DP 开关走 `get_parallel().enable_dp_attention`。这与 u2-l5 的「四层结构 + 命名空间袋」完全一致：业务代码读袋，运行期改写（如权重热更新改 `model_path`/`load_format`）走 `get_context().override()`。对请求生命周期本身没有影响——`rid`、消息结构体、事件循环与汇合点机制都保持原样。

#### 4.2.4 代码实践

**实践目标**：在源码里确认「入向发、出向收」共用同一个 `rid_to_state` 字典作为汇合点。

**操作步骤**：

1. 在 `tokenizer_manager.py` 中搜索 `rid_to_state`，列出所有读/写位置。
2. 分类：哪些发生在 `generate_request`/`_init_req_state`（入向建状态）、哪些发生在 `_handle_batch_output`（出向写状态/删状态）、哪些发生在 `_wait_one_response`（消费状态）。

**需要观察的现象 / 预期结果**：

- `_init_req_state` 写入 `rid_to_state[rid] = state`。
- `_handle_batch_output` 用 `.get(rid)` 读、`finished` 时 `del`。
- `_wait_one_response` 用 `self.rid_to_state[obj.rid]` 读。
- 三处指向同一个字典，证明入向协程与出向 `handle_loop` 通过它解耦通信。

> 本步骤为源码阅读型实践，无需运行服务。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `handle_loop` 要用 `auto_create_handle_loop` 懒启动，而不是在 `__init__` 里直接启动？

**参考答案**：`TokenizerManager` 可能在没有 asyncio 事件循环的上下文里被构造（例如某些测试或离线 `Engine`），而 `handle_loop` 是协程、必须挂到事件循环上。懒启动保证「直到第一个请求真正到来、事件循环一定就绪」时才创建任务，避免「构造时尚无运行中的 loop」的错误。

**练习 2**：如果一条请求的 `rid` 在 `rid_to_state` 里找不到（`_handle_batch_output` 中 `state is None`），会发生什么？

**参考答案**：会记一条 error 日志并 `continue` 跳过这条结果（见 [L1924-L1932](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/tokenizer_manager.py#L1924-L1932)）。常见原因是该请求已被取消或异常清理（`_discard_pending_req_states` 删掉了状态），Scheduler 却仍回吐了它的结果。`health_generate` 这种以 `HEALTH_CHECK_RID_PREFIX` 开头的 rid 会静默跳过。

---

### 4.3 DetokenizerManager：处理循环与增量解码

#### 4.3.1 概念说明

`DetokenizerManager` 是一个**独立子进程**（u1-l4），唯一职责：把 Scheduler 回吐的 token id 解码成人类可读文本。它不需要 asyncio——单进程内一个朴素 `while True` 循环就够了，因为解码是 CPU 密集且顺序的。

它的核心难点是**增量解码**：模型每步只新增几个 token，但 BPE 分词器存在「token 边界 ≠ 字符边界」的问题——某些 token 单独解码会得到不完整的 UTF-8（结尾出现 `�`）。例如中文「你好」可能被切成两个 token，单独解码第一个会得到半个字节序列。DetokenizerManager 必须缓存已读 token、在能确定输出安全文本时才提交，靠 `DecodeStatus` 维护进度。

#### 4.3.2 核心流程

主循环 `event_loop`（三步：收 → 解 → 发）：

```
while True:
    recv_obj = sock_recv(recv_from_scheduler)        # 收 BatchTokenIDOutput
    output   = _request_dispatcher(recv_obj)          # 按 type 分发
    if output is not None:
        sock_send(send_to_tokenizer, output)          # 发 BatchStrOutput
```

`handle_batch_token_id_out` 内部的增量解码逻辑：

```
对批次里每条请求 rid:
    若 decode_status 里没有 rid → 新建 DecodeStatus（记初始 read_offset）
    否则 → 把本批 decode_ids 追加到 s.decode_ids
    read_ids = decode_ids[surr_offset:]            # 待解码片段
    surr_ids  = decode_ids[surr_offset:read_offset] # 上一轮的"参照"片段
    # 关键：分别解码 read_ids 与 surr_ids，用差值得到"纯新增文本"
    new_text = read_text[len(surr_text):]

    若尚未 finished（流式中）:
        若 new_text 不以 "�" 结尾 → 安全，提交并推进 offset
        否则 → 只输出可打印前缀，offset 不动，等下轮重试
    若 finished → 合并最终文本、裁剪 stop 串、发尾段
```

新增文本的计算本质上是一个集合差：

\[
\text{new\_text} = \text{decode}(\text{read\_ids})[\,\text{len}(\text{decode}(\text{surr\_ids})):\,]
\]

即「多读几个 token 后整体解码的结果」减去「上一轮已提交前缀」。

#### 4.3.3 源码精读

**进程入口**：[`detokenizer_manager.py:512-534`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/detokenizer_manager.py#L512-L534) — `run_detokenizer_process`。`setproctitle("sglang::detokenizer")`、`kill_itself_when_parent_died()` 保证父进程崩溃时自我了断，然后 `manager.event_loop()`。

**IPC socket**：[`detokenizer_manager.py:111-122`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/detokenizer_manager.py#L111-L122) — `recv_from_scheduler`（PULL 自 `detokenizer_ipc_name`）、`send_to_tokenizer`（PUSH 到 `tokenizer_ipc_name`）。这正是环上后两条边。

**主循环与分发器**：[`detokenizer_manager.py:166-174`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/detokenizer_manager.py#L166-L174) — `event_loop`。配合 [`init_request_dispatcher`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/detokenizer_manager.py#L156-L164) 用 `TypeBasedDispatcher` 按 `BatchTokenIDOutput`/`BatchEmbeddingOutput` 等类型分发。

**增量解码状态**：[`detokenizer_manager.py:63-88`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/detokenizer_manager.py#L63-L88) — `DecodeStatus`。字段：`decode_ids`（累计 token）、`surr_offset`（surrogate 起点指针）、`read_offset`（已读取指针）、`sent_offset`（已发给 tokenizer 的文本长度）、`decoded_text`/`decoded_text_chunks`（懒累加文本）。它存在一个有界字典 `LimitedCapacityDict`（[L499-L509](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/detokenizer_manager.py#L499-L509)，容量 `SGLANG_DETOKENIZER_MAX_STATES`，默认 65536），并发太高时最老的请求状态会被驱逐。

**增量解码核心**：[`detokenizer_manager.py:290-409`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/detokenizer_manager.py#L290-L409) — `_decode_batch_token_id_output`。[L296-L321](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/detokenizer_manager.py#L296-L321) 维护 `read_ids`/`surr_ids`；[L373-L394](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/detokenizer_manager.py#L373-L394) 处理不完整 UTF-8：若 `new_text` 以 `�` 结尾则只发可打印前缀、不推进 offset，等下轮补齐。`find_printable_text` 就是做这件事的工具。

**组装修出消息**：[`detokenizer_manager.py:430-484`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/detokenizer_manager.py#L430-L484) — `handle_batch_token_id_out`。把解码出的 `output_strs` 连同透传字段（`output_ids`、各类 logprob、token 计数）打包成 `BatchStrOutput`。注意 [`trim_matched_stop`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/detokenizer_manager.py#L176-L206) 会按停止串/停止 token 裁剪输出。

#### 4.3.4 代码实践

**实践目标**：用纸笔模拟一次增量解码，理解 `read_offset`/`surr_offset` 如何避免重复解码与乱码。

**操作步骤**：

1. 假设某中文词被切成 3 个 token：`t1`（半字节）、`t2`（半字节，与 t1 合成一个字）、`t3`（独立一个字）。模型分 3 步逐个产出。
2. 模拟每一步 Detokenizer 收到 `decode_ids` 与 `finished_reason`（前两步为 `None`，第三步非 `None`）。
3. 对照 [`_decode_batch_token_id_output`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/detokenizer_manager.py#L290-L409)，写出每步 `read_ids`、`surr_ids`、`new_text`、是否推进 offset、最终 `output_strs`。

**需要观察的现象 / 预期结果**：

- 第 1 步：`decode([t1])` 得到含 `�` 的串，只发可打印前缀（可能为空），`read_offset` 不推进。
- 第 2 步：`decode([t1,t2])` 得到完整第一个字，`new_text = 第一个字`，提交并推进 offset。
- 第 3 步 finished：合并全部文本，发尾段。
- 全程旧 token 只解码了一次（在每步整体解码 `read_ids` 时），没有「每次从头解码全部 token」的 O(n²) 行为——增量靠的是「减去 `surr_text` 前缀」而非「只解码最后一个 token」。

> 本步骤为「阅读 + 手工模拟」型实践，无需运行服务。如要运行验证，可对照 `test/srt` 下与 detokenizer/detokenization 相关的测试断言。

#### 4.3.5 小练习与答案

**练习 1**：`DecodeStatus` 为什么用 `LimitedCapacityDict` 限容，而不是无限增长？

**参考答案**：每个进行中的流式请求都占一条 `DecodeStatus`。若客户端断连或 Scheduler 异常导致请求没正常 `finished`，状态会残留。限容（默认 65536）是兜底：超限时驱逐最老的状态，防内存无限膨胀。代价是若某请求的状态被驱逐后又来结果，会抛 `Decode status not found`（见 [L364-L372](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/detokenizer_manager.py#L364-L372)），此时应调大 `SGLANG_DETOKENIZER_MAX_STATES`。

**练习 2**：`BatchEmbeddingOutput` 到达 Detokenizer 时会发生什么？

**参考答案**：[`handle_batch_embedding_out`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/detokenizer_manager.py#L208-L210) 直接原样返回——嵌入模型不需要解码文本，Detokenizer 只做透传中转。

---

## 5. 综合实践

**任务**：为一条流式 chat 请求，画出它在三个进程间流转时关键字段的读写表，把本讲三个模块串成一张完整时序图。

**背景设定**：客户端发起一次 `stream=True` 的 chat 请求，prompt = "你好"，模型分两步生成回复（第一步 1 个 token、第二步 1 个 token 后结束）。

**步骤**：

1. **画环**：画出 `TokenizerManager → Scheduler → DetokenizerManager → TokenizerManager` 的环，标注三条 ZMQ 边的地址名（`scheduler_input_ipc_name`、`detokenizer_ipc_name`、`tokenizer_ipc_name`）和每条边上的消息类型。参考 [`ipc_channels.py:37-68`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/scheduler_components/ipc_channels.py#L37-L68)。

2. **填字段表**：按下表，对每个进程标出它对 `rid`、`input_ids`、`output_ids`、`decoded_text` 四个关键字段的「读 / 写」操作与所在源码位置：

   | 字段 | TokenizerManager | Scheduler（黑盒） | DetokenizerManager |
   | --- | --- | --- | --- |
   | `rid` | 写（`normalize_batch_and_arguments` 调 `_normalize_rid`/`regenerate_rid` 生成）；读（`rid_to_state` 查） | 读（路由） | 读（`decode_status[rid]` 索引） |
   | `input_ids` | 写（`_tokenize_one_request` 分词产出，装入 `TokenizedGenerateReqInput.input_ids`） | 读（前向输入） | —（不关心） |
   | `output_ids` | 读（`_handle_batch_output` 取 `recv_obj.output_ids[i]` 累加进 `state.output_ids`） | 写（采样产出，装入 `BatchTokenIDOutput.output_ids`） | 透传（`BatchStrOutput.output_ids=recv_obj.output_ids`） |
   | `decoded_text` | 读（`recv_obj.output_strs[i]` → `state.append_text`） | — | 写（`_decode_batch_token_id_output` 解码产出 `output_strs`） |

3. **补时序**：在环上标出两次 decode step 各自的消息往返（两次 `BatchTokenIDOutput` → `BatchStrOutput`），第二次带非 `None` 的 `finished_reason`。指出 TokenizerManager 在 `finished` 时执行 `del self.rid_to_state[rid]`、Detokenizer 执行 `del self.decode_status[rid]`。

**预期产出**：一张图 + 一张字段读写表。完成后你应能解释：为什么 `rid` 必须全程携带（它是三个进程的唯一汇合键），以及为什么 `input_ids` 只在前半段、`decoded_text` 只在后半段出现。

**可选运行验证**（待本地验证，需 GPU 环境）：用 `sglang serve` 启动一个小模型，参考 `examples/frontend_language/quick_start/local_example_chat.py` 发一条 `stream=True` 请求，在服务端日志里观察请求被各进程处理的顺序，与你的时序图对照。

## 6. 本讲小结

- 一条请求在环上演化四种消息：`GenerateReqInput`（进程内，不上线）→ `TokenizedGenerateReqInput`（上线输入）→ `BatchTokenIDOutput`（Scheduler 回吐的 token id 批）→ `BatchStrOutput`（Detokenizer 回吐的文本批），取消另走 `AbortReq`。
- `TokenizerManager` 同时跑两条流：入向 `generate_request` 负责分词、建 `ReqState`、转发；出向 `handle_loop` 负责 `recv_from_detokenizer` 并按 `rid` 写 `ReqState.event` 唤醒等待方。`rid_to_state` 是两条流的汇合点。
- `DetokenizerManager` 是朴素 `while True` 三步循环（收 → 解 → 发），用 `DecodeStatus` 的 `surr_offset`/`read_offset` 做增量解码，靠「减去上一轮前缀」避免重复解码、靠检测 `�` 避免半字节乱码。
- `rid` 是贯穿三个进程的唯一标识；`input_ids` 在请求前半段流转，`decoded_text`/`output_strs` 在后半段产生；`finished_reasons[i] is None` 是「仍在流式生成」的统一信号。
- 整套机制用 msgpack + `tag=True` 序列化、`array`/`Tensor` 经 `enc_hook`、不透明对象经 `PickleWrapper` 兜底，兼顾性能与类型安全。
- 配置读取已命名空间化：各进程通过 `runtime_context` 访问器（如 `get_serving().weight_version`）读运行期配置，改写走 `get_context().override()`；但这只影响「配置怎么读」，请求流转的机制本身不变。

## 7. 下一步学习建议

- **u3-l1 Scheduler 核心与事件循环**：本讲把 Scheduler 当黑盒，下一单元正式打开它，看 `run_event_loop` 如何接收 `TokenizedGenerateReqInput`、构造 prefill/decode 批、把采样结果打包成 `BatchTokenIDOutput` 回吐——补齐环上中间那一跳。
- **u3-l2 请求与批数据模型**：深入 `schedule_batch.py` 的 `Req`/`ScheduleBatch`，理解 `input_ids` 进入 Scheduler 后如何演化成 `origin_input_ids`/`output_ids`/`fill_ids`，与本讲的字段表呼应。
- **u2-l5 RuntimeContext 与配置命名空间**（若想深挖配置读取）：本讲提到各进程通过 `get_serving()`/`get_observability()` 等访问器读配置，其背后的四层结构与命名空间袋在 u2-l5 有完整讲解。
- **u2-l4 OpenAI 兼容 API 层**（若尚未学）：看 `/v1/chat/completions` 如何转成本讲的 `GenerateReqInput`，补齐「HTTP 请求到 `generate_request` 之前」的那一段。
- 想验证增量解码行为，可阅读 `test/srt` 下 detokenization 相关测试，对照断言理解 `read_offset` 的边界条件。

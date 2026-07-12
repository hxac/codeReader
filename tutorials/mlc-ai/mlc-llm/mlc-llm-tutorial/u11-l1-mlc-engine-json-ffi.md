# MLCEngine 与 JSON FFI 桥接

## 1. 本讲目标

在前面几讲里，我们已经知道 MLC LLM 的「大脑」是一个用 C++ 写的 `ThreadedEngine`（见 u9-l1），它在一个后台线程里反复执行 `Step()`，完成 prefill、decode、采样等所有重活。可是用户写的是 Python，请求是 OpenAI 风格的 JSON，模型产物是磁盘上的文件——**Python 世界和 C++ 引擎之间，到底是怎么衔接的？**本讲就专门拆解这条「跨界」通道。

学完本讲，你应当能够：

- 说清 MLC LLM 里**两套**不同的 Python↔C++ 桥接方案——`JSONFFIEngine`（字符串桥）与 `MLCEngine`/`AsyncMLCEngine`（类型化对象桥）——各自的工作方式与适用场景。
- 画出一次 chat completion 请求从 Python 函数出发、跨进 C++ `ThreadedEngine`、再被两个后台线程「流」回来的完整往返时序。
- 理解为什么引擎需要**两个**后台循环（`run_background_loop` 与 `run_background_stream_back_loop`），以及它们和 Python 侧队列/事件循环的配合。
- 区分同步 `MLCEngine` 与异步 `AsyncMLCEngine` 在回调投递上的关键差别（`queue.Queue` vs `call_soon_threadsafe`）。

## 2. 前置知识

- **FFI（Foreign Function Interface，外部函数接口）**：让一种语言调用另一种语言编译产物的机制。MLC LLM 用 Python 做上层编排，用 C++ 做高性能推理，二者之间靠 TVM runtime 的 PackedFunc 机制互通。
- **PackedFunc**：TVM 里的统一函数抽象。任何注册到 TVM registry 的函数，无论用 C++ 还是 Python 写，都能被对方「按名字符串」取到并调用，参数以类型化对象（`DLTensor`、`Array`、`String` 等）传递。
- **JSON FFI**：上面 PackedFunc 的一种特例用法——干脆只用「字符串」当唯一的参数和返回值。好处是跨语言零对象耦合、好测试；坏处是每次都要序列化/反序列化。本讲的 `JSONFFIEngine` 就是这种「纯字符串接口」。
- **生产者-消费者队列**：一个线程往队列里塞数据（生产者），另一个线程从队列里取数据（消费者），用互斥锁 + 条件变量协调。本讲里 C++ 引擎「生产」生成结果，Python「消费」它们，中间就是这种队列。
- **`asyncio` 与线程安全**：Python 的 `asyncio` 事件循环默认不是线程安全的。从别的线程往事件循环里投递任务，必须用 `call_soon_threadsafe`。这一点会在异步引擎里看到。
- 建议先读过 **u9-l1**（Engine / ThreadedEngine / EngineState 的 C++ 抽象）与 **u6-l3**（OpenAI 兼容协议与 `GenerationConfig`），本讲会直接使用那里的概念。

## 3. 本讲源码地图

本讲涉及的关键文件与职责：

| 文件 | 语言 | 职责 |
| --- | --- | --- |
| `python/mlc_llm/json_ffi/engine.py` | Python | **JSON 字符串桥**的 Python 侧：`EngineState`、`BackgroundLoops`、`Completions`、`JSONFFIEngine`。chat CLI 默认走这条路。 |
| `cpp/json_ffi/json_ffi_engine.h` | C++ | JSON FFI 引擎的头文件：`JSONFFIEngine` 类的接口与内部状态。 |
| `cpp/json_ffi/json_ffi_engine.cc` | C++ | JSON FFI 引擎的实现：`ChatCompletion`/`AddRequest`/`Reload`、把 token 流反序列化成 JSON 响应的 `GetResponseFromStreamOutput`，以及 `CreateJSONFFIEngine` 的注册。 |
| `python/mlc_llm/serve/engine_base.py` | Python | **类型化对象桥**的公共基座：`MLCEngineBase.__init__`（创建 `ThreadedEngine`、装配 FFI、启动后台线程）、`EngineState`（同步/异步回调投递）。 |
| `python/mlc_llm/serve/engine.py` | Python | `MLCEngine`（同步）与 `AsyncMLCEngine`（异步）及其 `chat.completions.create` 代理、`_generate` 主循环。 |
| `cpp/serve/threaded_engine.cc` | C++ | `ThreadedEngine` 的两个后台循环 `RunBackgroundLoop` / `RunBackgroundStreamBackLoop`——两条桥共同的底层引擎心跳。 |

记忆口诀：**两条桥，同一个心脏**。`JSONFFIEngine` 和 `MLCEngine`/`AsyncMLCEngine` 是两条不同的 Python↔C++ 桥，但它们最终都创建并驱动同一个 C++ `ThreadedEngine`。

## 4. 核心概念与源码讲解

### 4.1 JSON FFI 引擎：用字符串跨越语言边界

#### 4.1.1 概念说明

`JSONFFIEngine` 是 MLC LLM 提供的一种「纯字符串」引擎接口。它在自己的模块 docstring 里说得很直白：

> JSON FFI is a pure string based interface of MLC LLM Engine. … For most python API usage, please use MLCEngine and MLCAsyncEngine.
>
> 见 [python/mlc_llm/json_ffi/__init__.py:L1-L8](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/json_ffi/__init__.py#L1-L8)

也就是说，它**不是**给最终用户日常用的主推 API（那是 `MLCEngine`），而是一个「薄」接口，主要服务于两件事：

1. **chat CLI 的默认后端**：终端交互场景，命令简单、单并发，不需要 Python 侧的复杂编排。
2. **测试与跨语言对接**：因为请求和响应都只是 JSON 字符串，任何能产出 JSON 的客户端都能直接驱动它，方便写测试或用别的语言对接。

为什么用「字符串」而不是直接传对象？因为字符串是**最小公约数**：Python 侧只需 `json.dumps` / `json.loads`，C++ 侧只需 `tvm::ffi::json::Parse` / `Stringify`，两边都不必关心对方定义了哪些类型对象，耦合最低。代价是**所有跨界数据都要序列化一遍**，并把本可以在 Python 做的事（拼 prompt、分词、去分词）挪到 C++ 侧完成。在 chat CLI 这种轻量场景，这个代价完全可以接受。

于是有了两条桥最本质的差别：

| 维度 | JSON FFI 桥（`JSONFFIEngine`） | 类型化对象桥（`MLCEngine`/`AsyncMLCEngine`） |
| --- | --- | --- |
| 跨界载荷 | JSON 字符串 | TVM 类型化对象（`Data`、`RequestStreamOutput` 等） |
| 拼 prompt / 分词 | **C++ 侧**完成（`CreatePrompt` + `Tokenizer`） | **Python 侧**完成（`engine_base.process_chat_completion_request`） |
| 去分词 / 序列化 | **C++ 侧**完成（`TextStreamer` + JSON） | **Python 侧**完成（`TextStreamer` + Pydantic） |
| 主用途 | chat CLI、跨语言测试 | Python 库、REST 服务器 |
| 入口 C++ 模块 | `mlc.json_ffi.CreateJSONFFIEngine` | `mlc.serve.create_threaded_engine` |

#### 4.1.2 核心流程

`JSONFFIEngine` 的**构造阶段**做三件事：拿 C++ 模块、装配 FFI 字典、启动后台线程并加载模型。

```text
Python JSONFFIEngine.__init__
  ├─ tvm.get_global_func("mlc.json_ffi.CreateJSONFFIEngine")()  # 取回 C++ 模块
  ├─ self._ffi = { 模块里按名字取出 9 个函数 }                  # 装配 FFI 字典
  ├─ BackgroundLoops(self._ffi)                                  # 启动两个后台线程
  ├─ ffi["init_background_engine"](device, callback)             # 把回调登记进 C++
  └─ ffi["reload"](engine_config_json)                           # 加载模型权重 / 库
```

**请求阶段**（一次 `chat.completions.create`）的往返时序：

```text
[Python 调用线程]                                   [C++ 后台线程]
Completions.create(messages=..., stream=True)
  │ 构造 ChatCompletionRequest（Pydantic 校验）
  │ request.model_dump_json(by_alias=True)  ──┐
  │                                            │ JSON 字符串
  ├─ ffi["chat_completion"](json, request_id) ─┘
  │                                            ▼
  │                                   JSONFFIEngine::ChatCompletion
  │                                     └─ AddRequest
  │                                          ├─ ChatCompletionRequest::FromJSON
  │                                          ├─ CreatePrompt(conv_template, ...)   拼 prompt
  │                                          ├─ 组装 GenerationConfig
  │                                          ├─ new Request(id, inputs, gen_cfg)
  │                                          ├─ 每个 n 建一个 TextStreamer        去分词器就位
  │                                          └─ engine_->AddRequest(request) ──┐
  │                                                                   进入 ThreadedEngine │
  │                                                                   （waiting_queue） │
  │ handle_chat_completion 在 sync_queue.get() 上阻塞 ◀──────────── 生成结果经回调推回
  │ 循环 yield ChatCompletionStreamResponse
  ▼
返回给用户
```

关键点：Python 侧的 `Completions.create` 把请求序列化成 JSON 后就「撒手」交给 FFI，然后**阻塞在 `sync_queue` 上**等结果；真正干活的是 C++ 后台线程（见 4.2）。`JSONFFIEngine` 只支持 `stream=True`，因为它本质上就是按流式分块设计的。

#### 4.1.3 源码精读

先看 Python 侧的构造。`JSONFFIEngine.__init__` 通过 TVM registry 取出 C++ 模块，并按一组**固定的函数名**把方法抓进 `_ffi` 字典——这组名字就是 Python 与 C++ 之间的「契约字符串」：

[python/mlc_llm/json_ffi/engine.py:L238-L268](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/json_ffi/engine.py#L238-L268) —— 取出 C++ `CreateJSONFFIEngine` 模块，按名字装配 9 个 FFI 函数（`init_background_engine`/`reload`/`chat_completion`/`abort`/两个后台循环/`exit_background_loop` 等），随后调用 `init_background_engine` 登记回调、`reload` 加载模型。

`init_background_engine` 把设备类型/序号和**一个 Python 回调函数**传给 C++。这个回调后面会被 C++ 用来把生成结果「送回」Python：

[python/mlc_llm/json_ffi/engine.py:L263-L267](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/json_ffi/engine.py#L263-L267) —— 把设备信息和 `self._state.get_request_stream_callback()` 一起传进 C++。

再看请求入口 `Completions.create`。它只接受 `stream=True`，否则直接报错；构造完 `ChatCompletionRequest` 后，用 `model_dump_json(by_alias=True)` 把请求压成 JSON 字符串，交给 `handle_chat_completion`：

[python/mlc_llm/json_ffi/engine.py:L144-L195](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/json_ffi/engine.py#L144-L195) —— `stream=False` 时抛 `ValueError`；构造请求后调用 `handle_chat_completion`，传入序列化后的 JSON 与 `include_usage` 标志。

接下来是真正承担「投递请求 + 收割结果」的 `EngineState.handle_chat_completion`。它每次为本次请求新建一个 `sync_queue`，调用 `ffi["chat_completion"]` 投递请求（非阻塞，立即返回），然后在 `sync_queue.get()` 上**阻塞等待** C++ 回推的 JSON 字符串，逐块反序列化成 `ChatCompletionStreamResponse` 并 `yield`。约定：**带 `usage` 的那块永远是最后一块**，收到它就结束循环：

[python/mlc_llm/json_ffi/engine.py:L38-L72](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/json_ffi/engine.py#L38-L72) —— 新建队列、投递请求、阻塞读队列并把 JSON 字符串还原成流式响应；遇到 `usage is not None` 的块即判定为最后一块，异常时调用 `ffi["abort"]`。

现在跨到 C++ 侧。`ChatCompletion` 极其简短：调用 `AddRequest`，失败就走 `StreamBackError` 把错误以流式块的形式回传（注意：**错误也走流式通道**，因为「最后一块带 usage」是系统不变量）：

[cpp/json_ffi/json_ffi_engine.cc:L26-L32](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/json_ffi/json_ffi_engine.cc#L26-L32) —— `ChatCompletion` 只是 `AddRequest` 的薄壳，失败时 `StreamBackError`。

`AddRequest` 是 JSON FFI 桥「替 Python 干活」的核心：解析 JSON 请求 → 用对话模板拼出 prompt（`CreatePrompt`）→ 合并 stop 字符串 → 按请求字段填出 `GenerationConfig` 并校验 → 构造引擎层 `Request` → 为每个并行生成分支建一个 `TextStreamer` 存进 `request_map_` → 调用 `engine_->AddRequest` 把请求送进 `ThreadedEngine`：

[cpp/json_ffi/json_ffi_engine.cc:L68-L140](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/json_ffi/json_ffi_engine.cc#L68-L140) —— `AddRequest` 把 OpenAI 风格的 JSON 请求翻译成引擎能消费的 `Request`，并在 `request_map_` 里登记本请求的去分词器（`TextStreamer`）。

注意 L130-L136：每个 `n`（并行生成数）对应一个独立的 `TextStreamer`，它们被存进 `RequestState.streamer`，留到回送阶段做去分词。这正是「C++ 侧负责去分词」的体现。

C++ 模块怎么被 Python 取到？靠一个 TVM 静态初始化块注册的全局函数 `mlc.json_ffi.CreateJSONFFIEngine`，它返回一个 `JSONFFIEngineImpl` 模块对象；而 `JSONFFIEngineImpl` 用 `TVM_MODULE_VTABLE` 把一组 C++ 方法按名字暴露出去——这些名字和 Python `_ffi` 字典里的 key **一一对应**：

[cpp/json_ffi/json_ffi_engine.cc:L157-L171](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/json_ffi/json_ffi_engine.cc#L157-L171) —— `JSONFFIEngineImpl` 的 VTABLE，把 `"chat_completion"`、`"reload"`、`"run_background_loop"` 等名字绑定到对应 C++ 方法。

[cpp/json_ffi/json_ffi_engine.cc:L302-L306](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/json_ffi/json_ffi_engine.cc#L302-L306) —— 注册 `mlc.json_ffi.CreateJSONFFIEngine` 工厂，Python 侧 `tvm.get_global_func(...)` 取到的就是它。

类成员方面，`JSONFFIEngine` 内部持有一个真正的 `ThreadedEngine`（`engine_`）、一个 Python 注入的回调（`request_stream_callback_`），以及一个按 `request_id` 索引的本地状态表 `request_map_`（装去分词器等）：

[cpp/json_ffi/json_ffi_engine.h:L53-L67](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/json_ffi/json_ffi_engine.h#L53-L67) —— `engine_` 是被包裹的 `ThreadedEngine`；`request_map_` 每项是 `{model, streamer[]}`，对应一条回复流。

小结：JSON FFI 桥的设计哲学是**「让 C++ 多干活，让 Python 当传话筒」**——拼 prompt、分词、去分词、序列化响应全在 C++ 完成，Python 侧只负责「收 JSON、发 JSON」。

#### 4.1.4 代码实践

> 本实践需要本地能跑起一个 MLC 模型（CPU/GPU 均可）。若暂无环境，可按「源码阅读型」部分完成。

**实践目标**：亲手用 `JSONFFIEngine` 发一次流式请求，并对照源码看清「JSON 串进、JSON 串出」的边界。

**操作步骤（可运行型）**：

1. 写一个最小脚本（示例代码，非项目原有文件）：

   ```python
   # demo_json_ffi.py —— 示例代码
   from mlc_llm.json_ffi import JSONFFIEngine

   model = "HF://mlc-ai/Llama-3-8B-Instruct-q4f16_1-MLC"
   engine = JSONFFIEngine(model, mode="interactive")

   for chunk in engine.chat.completions.create(
       messages=[{"role": "user", "content": "用一句话介绍你自己"}],
       model=model,
       stream=True,
       stream_options={"include_usage": True},
   ):
       for choice in chunk.choices:
           print(choice.delta.content, end="", flush=True)
   print()  # 最后一块带 usage
   engine.terminate()
   ```

2. 运行：`python demo_json_ffi.py`。

**需要观察的现象**：

- 文本逐字（或逐块）打印，符合「流式」预期。
- 最后会收到一块 `chunk.usage is not None` 的数据，其中 `extra` 字段里通常含 `prefill_tokens_per_s` / `decode_tokens_per_s` 等速度统计（与 u1-l3 提到的 `/stats` 同源）。

**预期结果**：模型正常输出一段回答，并在末尾给出 usage 块；这正对应源码里 `handle_chat_completion` 收到「带 usage 的最后一块」即结束循环的约定。

**源码阅读型补充**（无需运行）：在 `Completions.create` 里把 `stream=True` 改成 `stream=False` 再读源码，确认它会在 [engine.py:L145-L146](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/json_ffi/engine.py#L145-L146) 抛出 `ValueError("JSONFFIEngine only support stream=True")`。这说明 JSON FFI 桥是「纯流式」设计——非流式语义要由上层自己聚合多个流式块。

> 若无法本地运行，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`JSONFFIEngine` 的 `_ffi` 字典里共有哪些 key？它们分别对应 C++ 里的哪个方法？

**参考答案**：共 9 个（见 [engine.py:L240-L253](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/json_ffi/engine.py#L240-L253)）：`init_background_engine`、`reload`、`unload`、`reset`、`chat_completion`、`abort`、`run_background_loop`、`run_background_stream_back_loop`、`exit_background_loop`。它们与 [json_ffi_engine.cc:L159-L171](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/json_ffi/json_ffi_engine.cc#L159-L171) 的 VTABLE 条目一一对应，比如 `"chat_completion"` → `JSONFFIEngineImpl::ChatCompletion`。

**练习 2**：为什么 `handle_chat_completion` 里判定「最后一块」用的是 `response.usage is not None`，而不是某个独立的结束标志？

**参考答案**：因为「带 usage 的块永远最后发」是引擎的系统不变量（C++ 侧 `StreamBackError` 即便出错也会补发一个 usage 块，见 [json_ffi_engine.cc:L53-L62](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/json_ffi/json_ffi_engine.cc#L53-L62)）。用 usage 当哨兵，可以让 Python 侧不用额外维护「是否结束」的状态，只要看到 usage 就知道流终止。

---

### 4.2 后台循环与流式回调队列

#### 4.2.1 概念说明

无论走哪条桥，真正驱动模型跑起来的都是 C++ 的 `ThreadedEngine`。它有一个关键设计（详见 u9-l1）：**对调用方不线程安全**，所以用「单消费者后台线程」包了一层。本讲要回答的是：引擎跑起来后，**生成结果怎么流回 Python？**

答案是 `ThreadedEngine` 跑着**两个**后台循环，各司其职：

- **`RunBackgroundLoop`（引擎心跳循环）**：从指令队列里取出 `AddRequest`/`AbortRequest`/`Reload`/`Reset` 等操作并执行，然后调用 `background_engine_->Step()`——也就是 u9-l2 里讲的「事件-动作循环」的一次心跳。生成结果就在这一步产生。
- **`RunBackgroundStreamBackLoop`（回送循环）**：专门负责把引擎产出的增量结果，通过 `request_stream_callback_` 投递回 Python。

为什么要把「跑引擎」和「回送结果」拆成两个循环？因为它们对延迟的敏感度不同：

- `Step()` 是 GPU 密集的重活，必须尽快让出，好让下一批请求被调度进来。
- 回送（尤其经过 JSON 序列化、Python 回调、Pydantic 校验）相对慢且会抢 GIL。如果把回送塞进 `Step()` 所在线程，就会拖慢引擎心跳。

用两个线程 + 一个有锁队列把它们解耦，引擎就能在「回送还没做完」时继续跑下一个 `Step()`，**生成与回送并行推进**。这是一个典型的多生产者-单消费者结构，其吞吐可以用 Little 定律粗略刻画：当回送速率 \( \mu \) 低于生成速率 \( \lambda \) 时，队列会堆积；引擎用条件变量做背压，让生产侧在队列过载时自然放缓。

#### 4.2.2 核心流程

两个循环都遵循「等通知 → 批量取走 → 释放锁 → 在锁外处理」的同一套骨架，差别只在「处理」的是什么：

```text
RunBackgroundLoop（跑引擎）              RunBackgroundStreamBackLoop（回送）
  while !exit_now_:                       while !exit_now_:
    wait(指令队列非空 或 exit)               wait(回送队列非空 或 exit)
    一次性取走全部指令（锁内）                一次性取走全部 delta（锁内）
    for 每条指令: 执行它                    把多批 delta 拍平成一批（锁外）
    background_engine_->Step()             request_stream_callback_(flat)   ← 调回 Python
```

引擎内部产生的结果并不是直接调 Python，而是先塞进一个 `request_stream_callback_inputs_` 队列（`engine_->Step()` 内部触发），再由回送循环批量取走调用。这就形成了一条完整的「单向带缓冲」链：

```text
engine_->Step() 产生 delta
   └─[内部回调，加锁]─▶ request_stream_callback_inputs_（C++ 队列）
                              │ notify_one()
                              ▼
                    RunBackgroundStreamBackLoop（C++ 线程）
                              │ 批量拍平 + 调用 request_stream_callback_
                              ▼
              【桥的选择在此分叉】
        ┌─────────────────────┴──────────────────────┐
        ▼ JSON FFI 桥                                 ▼ 类型化对象桥
 GetResponseFromStreamOutput（去分词+序列化成 JSON）   直接把 delta 对象交给 Python
 → 调 Python 的 _callback(json_str)                  → 调 Python 的 _callback(delta_objs)
        │                                              │
        ▼                                              ▼
 sync_queue.put_nowait(json_str)                     sync: sync_output_queue.put_nowait(...)
                                                     async: event_loop.call_soon_threadsafe(...)
        │                                              │
        ▼                                              ▼
 handle_chat_completion 阻塞 get()                   _generate 循环消费
```

注意 4.2.2 这张图最上面的「内部回调」是 C++ `Engine` 在 `Step()` 里产 token 时调用的——它把原始 delta 对象塞队列；而 `request_stream_callback_`（回送循环调用它）则是「桥的入口」：JSON FFI 桥在这里把对象转成 JSON，类型化对象桥则原样透传。

#### 4.2.3 源码精读

先看 C++ 引擎心跳循环。它在锁内批量取走指令并清空队列（避免长时间持锁），然后在**锁外**逐条执行指令、最后跑一次 `Step()`：

[cpp/serve/threaded_engine.cc:L136-L189](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L136-L189) —— `RunBackgroundLoop`：条件变量等待 → 取走指令 → 处理 `kAddRequest`/`kAbortRequest`/`kReloadEngine` 等 → `background_engine_->Step()`。

回送循环结构几乎一样，只是处理对象换成「delta 列表」，处理动作换成「拍平 + 调用 `request_stream_callback_`」：

[cpp/serve/threaded_engine.cc:L191-L221](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L191-L221) —— `RunBackgroundStreamBackLoop`：等待 → 取走 `request_stream_callback_inputs_` → 把多批 delta 拍平成一批 → 调 `request_stream_callback_(flat)`。

那么 `request_stream_callback_inputs_` 是谁在往里塞？是构造 `ThreadedEngine` 时包的一层 wrapper——`Engine::Step()` 每产出 delta，就通过它把数据推进队列并 `notify_one()` 唤醒回送循环：

[cpp/serve/threaded_engine.cc:L272-L285](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L272-L285) —— 引擎内部回调：加锁把 delta `push_back` 进队列、计数 +1、通知回送循环。

两个循环都靠同一个 `exit_now_` 标志退出，`ExitBackgroundLoop` 会同时唤醒两者（各自的条件变量都 `notify_one`），保证 `terminate()` 能干净收尾：

[cpp/serve/threaded_engine.cc:L223-L230](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L223-L230) —— `ExitBackgroundLoop` 同时通知两个循环退出。

现在回到 Python 侧的 `JSONFFIEngine`。它的 `BackgroundLoops` 就是把 C++ 这两个循环函数各包成一个 `threading.Thread` 并启动；`terminate` 调用 `exit_background_loop` 再 `join` 两个线程：

[python/mlc_llm/json_ffi/engine.py:L75-L102](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/json_ffi/engine.py#L75-L102) —— `BackgroundLoops`：把 `run_background_loop` 和 `run_background_stream_back_loop` 各起一个线程；注释特意提醒「避免在闭包里 self 引用」以防循环引用。

C++ 侧的回送「出口」是 `InitBackgroundEngine` 里包的那层 wrapper：它把回送循环传来的 `Array<RequestStreamOutput>` 交给 `GetResponseFromStreamOutput`（去分词 + 组 JSON 响应），再用 Python 注入的回调把 JSON 字符串送回 Python：

[cpp/json_ffi/json_ffi_engine.cc:L181-L189](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/json_ffi/json_ffi_engine.cc#L181-L189) —— wrapper 把 delta 对象转成 JSON 字符串后，才调用 Python 的 `request_stream_callback_`，并用它初始化 `ThreadedEngine`。

`GetResponseFromStreamOutput` 是「去分词 + 序列化」的所在：对每条 delta，用之前存在 `request_map_` 里的 `TextStreamer` 把 token id 转成文本，按 OpenAI 流式格式组装 `choices`，最后 `Stringify` 成 JSON 字符串。带 `request_final_usage_json_str` 的 delta 被当作最后一块处理并清掉 `request_map_` 里的项：

[cpp/json_ffi/json_ffi_engine.cc:L220-L298](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/json_ffi/json_ffi_engine.cc#L220-L298) —— 把 delta token 经 `TextStreamer::Put` 去分词、组装 `ChatCompletionStreamResponse`、序列化为 JSON；usage 块单独处理并清理本地状态。

最后看 Python 侧的「最后一站」。`EngineState.get_request_stream_callback` 返回的回调 `_callback`，正是被 `init_background_engine` 注入 C++ 的那个。它收到的已经是 JSON 字符串，于是只做一件事——`put_nowait` 进 `sync_queue`：

[python/mlc_llm/json_ffi/engine.py:L26-L36](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/json_ffi/engine.py#L26-L36) —— 回调把 C++ 送来的 JSON 字符串以非阻塞方式塞进 `sync_queue`，供 `handle_chat_completion` 取走。

把 4.2.3 串起来就是一条完整往返：`Step()` 产 delta → 内部回调塞 C++ 队列 → 回送循环取走 → JSON FFI wrapper 去分词+序列化 → Python `_callback` 塞 `sync_queue` → `handle_chat_completion` 阻塞 `get()` 拿到并 `yield`。整条链路有**两个**缓冲点（C++ 队列、Python `sync_queue`）和**两个**线程边界（C++ 回送线程↔Python 调用线程），解耦得非常彻底。

#### 4.2.4 代码实践

**实践目标**：在不改动源码的前提下，通过「打点观察」验证两个后台线程确实存在，并感受回送是异步发生的。

**操作步骤（源码阅读 + 轻量验证）**：

1. 阅读 [engine.py:L75-L102](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/json_ffi/engine.py#L75-L102)，确认 `JSONFFIEngine` 构造后会多出两个线程。可在 4.1.4 的脚本里加上：

   ```python
   # 示例代码
   import threading
   engine = JSONFFIEngine(model, mode="interactive")
   print("当前线程数:", len(threading.enumerate()), "线程名:", [t.name for t in threading.enumerate()])
   ```

2. 阅读 [threaded_engine.cc:L191-L221](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L191-L221)，理解回送循环为何要「批量拍平」多批 delta 再调用回调——这能减少跨 FFI 的调用次数。

**需要观察的现象**：构造完引擎后，进程里至少多了 2 个非主线程（两个后台循环）。流式输出期间主线程大部分时间阻塞在 `sync_queue.get()`，说明生成与回送发生在别的线程。

**预期结果**：线程计数 ≥ 3（主线程 + 两个后台线程）；流式块能稳定回送。若你在无 GPU 环境跑，只能确认线程被创建，模型加载与生成「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：假设把「回送」合并进 `RunBackgroundLoop`（在 `Step()` 之后直接同步调 Python 回调），会出现什么问题？

**参考答案**：Python 回调涉及 JSON 序列化、GIL、Pydantic 校验，耗时不可控；若同步执行，会阻塞 `Step()`，导致引擎心跳变慢、吞吐下降，尤其当多个请求并发回送时，新请求的 prefill/decode 会被推迟。拆成独立循环让生成与回送**并行**，引擎心跳不被拖累。

**练习 2**：回送循环为什么在调用 `request_stream_callback_` 之前要把多批 delta 「拍平（flatten）」成一批？

**参考答案**：减少跨 FFI 的回调次数。多次 `Step()` 可能各自产生一批 delta，回送循环一次性取走后拍平成一个大 `Array`，只触发一次 Python 回调，降低 FFI 跨界与 GIL 切换的频率（见 [threaded_engine.cc:L210-L218](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L210-L218)）。

---

### 4.3 MLCEngine / AsyncMLCEngine：富 Python API

#### 4.3.1 概念说明

`MLCEngine` 与 `AsyncMLCEngine` 是 MLC LLM **主推**的 Python 接口，也是 REST 服务器（u11-l2）的底层引擎。和 JSON FFI 桥相反，它们走的是「**类型化对象桥**」：

- Python 直接把 `Data`、`Request`、`GenerationConfig` 等结构化对象（经 TVM runtime 包装）传进 C++，**不走 JSON**。
- 拼 prompt、分词、去分词、组装 OpenAI 响应，全部在 **Python** 侧完成（复用 `engine_base` 里的 `process_chat_completion_request` 等工具）。
- C++ 引擎被当成一台「纯粹的 token 生成器」：给它 `Request`（含已分词的 prompt 和生成配置），它吐回 `delta_token_ids`。

这样做的好处是：Python 侧能灵活定制（多模态、function calling、自定义流处理），且不必为每次调用付出 JSON 序列化代价；代价是 Python 与 C++ 共享更多类型定义，耦合更高。`json_ffi/__init__.py` 的注释也明说「多数 Python 用法请用 MLCEngine/AsyncMLCEngine」。

二者提供完全相同的 OpenAI 风格 API（`engine.chat.completions.create(...)`、`engine.completions.create(...)`），区别只在**调用模型**：

- `MLCEngine`：**同步**接口。`_generate` 在一个普通 `for` 循环里反复 `sync_output_queue.get()`，阻塞当前线程直到拿到下一块——适合脚本、批处理。
- `AsyncMLCEngine`：**异步**接口（`async def` / `async for`）。底层用一个 `AsyncRequestStream`，C++ 回调通过 `asyncio` 事件循环的 `call_soon_threadsafe` 把结果「线程安全地」投回协程——适合 FastAPI 这类异步服务端，能在一个事件循环里并发处理成百上千个请求。

#### 4.3.2 核心流程

构造阶段与 JSON FFI 桥高度同构，只是入口函数和 FFI 字典不同：

```text
Python MLCEngineBase.__init__
  ├─ tvm.get_global_func("mlc.serve.create_threaded_engine")()
  ├─ self._ffi = { 11 个函数：add_request/abort_request/create_request/... }
  ├─ 启动两个后台线程（run_background_loop / run_background_stream_back_loop）
  ├─ ffi["init_threaded_engine"](device, state.get_request_stream_callback(kind), trace_recorder)
  └─ ffi["reload"](engine_config.asjson())
```

`get_request_stream_callback(kind)` 是个分叉点：`kind="sync"` 走 `_sync_request_stream_callback`（塞队列），`kind="async"` 走 `_async_request_stream_callback`（投递到事件循环）。这正决定了 `MLCEngine` 与 `AsyncMLCEngine` 的差异。

请求阶段（同步 `MLCEngine`）：

```text
chat.completions.create(...)            # ChatCompletion 代理
  └─ engine._chat_completion(...)       # 组 ChatCompletionRequest
       └─ _handle_chat_completion       # process_chat_completion_request：拼 prompt + 分词 + GenerationConfig
            └─ _generate(prompt, gen_cfg, request_id):
                 ├─ ffi["create_request"](id, input_data, gen_cfg_json)   # 建引擎 Request
                 ├─ state.sync_output_queue = queue.Queue()               # 每请求一个新队列
                 ├─ ffi["add_request"](request)                           # 入引擎
                 └─ while True:
                      delta = state.sync_output_queue.get()              # 阻塞等回送
                      _request_stream_callback_impl(delta)              # 去分词、组 CallbackStreamOutput
                      yield ...
                      if final_usage: break
```

异步 `AsyncMLCEngine` 的 `_generate` 则把「阻塞 `get()`」换成「`async for request_output in stream`」：引擎回调经 `call_soon_threadsafe` 把结果推进 `AsyncRequestStream`，协程再异步消费。

注意两处「拼 prompt + 分词」的位置：在 JSON FFI 桥里是 C++ 的 `CreatePrompt`，在这里是 Python 的 `process_chat_completion_request`——这就是两条桥分工差别的根源。

#### 4.3.3 源码精读

先看公共基座 `MLCEngineBase.__init__`。它取 `mlc.serve.create_threaded_engine` 模块，按名字装配 11 个 FFI 函数（比 JSON FFI 桥多了 `create_request`、`get_complete_engine_config`、`debug_call_func_on_all_worker` 等管理接口），起两个后台线程，再 `reload`：

[python/mlc_llm/serve/engine_base.py:L610-L653](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L610-L653) —— 取 `create_threaded_engine` 模块、装配 `_ffi`、用 `state.get_request_stream_callback(kind)` 登记回调、启动两个后台线程、`reload` 加载模型并取回完整配置。

回调分叉就在 `EngineState.get_request_stream_callback`：它根据 `kind` 选择同步或异步的实现，再包一层 `_callback`：

[python/mlc_llm/serve/engine_base.py:L443-L464](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L443-L464) —— 按 `kind` 选 `_async_request_stream_callback` 或 `_sync_request_stream_callback`。

**同步版**回调把 delta 塞进 `sync_output_queue`（与 JSON FFI 桥的 `sync_queue` 思路一致，只是这里装的是结构化对象而非 JSON 字符串）：

[python/mlc_llm/serve/engine_base.py:L548-L553](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L548-L553) —— `_sync_request_stream_callback`：`put_nowait` 进 `sync_output_queue`。

**异步版**回调的关键是 `call_soon_threadsafe`——因为 C++ 回送线程不是事件循环线程，必须用线程安全方式把任务「预约」到事件循环里，否则会破坏 `asyncio` 的单线程模型：

[python/mlc_llm/serve/engine_base.py:L473-L489](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L473-L489) —— `_async_request_stream_callback`：用 `async_event_loop.call_soon_threadsafe` 把真正处理逻辑 `_async_request_stream_callback_impl` 调度到事件循环线程异步执行。

异步版在事件循环里的真正处理逻辑：去分词、组装 `CallbackStreamOutput`、推进对应 `request_id` 的 `AsyncRequestStream`，遇到 usage 块就收尾并移除 streamer：

[python/mlc_llm/serve/engine_base.py:L491-L546](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L491-L546) —— `_async_request_stream_callback_impl`：按 `request_id` 找到 stream + streamers，去分词后 `stream.push(outputs)`，usage 块则 `stream.finish()`。

再看同步 `MLCEngine._generate`：它先 `create_request`（建引擎 Request）、把当前请求的队列和 streamers 记进 `state`、`add_request` 入引擎，然后在 `while True` 里 `sync_output_queue.get()` 阻塞等回送，调 `_request_stream_callback_impl` 去分词并 `yield`，遇到 `final_usage` 就 `break`：

[python/mlc_llm/serve/engine.py:L1834-L1904](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine.py#L1834-L1904) —— 同步 `_generate`：建请求 → 记录队列/streamers → `add_request` → 阻塞循环消费 `sync_output_queue`，最后补发一个只含 usage 的块。

异步 `AsyncMLCEngine._generate` 的差别：用 `AsyncRequestStream` 代替 `queue.Queue`，`add_request` 后用 `async for request_output in stream` 异步消费，并支持取消（`asyncio.CancelledError`）时清理：

[python/mlc_llm/serve/engine.py:L1309-L1383](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine.py#L1309-L1383) —— 异步 `_generate`：建请求 → 登记 `AsyncRequestStream`（重复 `request_id` 会推错误）→ `add_request` → `async for ... in stream: yield`。

最后看用户最常调的 `engine.chat.completions.create`。它其实是个**代理**（`Chat`/`ChatCompletion` 用 `weakref.ref` 持有引擎，避免循环引用），最终转发到 `engine._chat_completion`：

[python/mlc_llm/serve/engine.py:L359-L432](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine.py#L359-L432) —— 同步 `ChatCompletion.create`：解析参数后转发给 `engine()._chat_completion`。

`MLCEngine` 与 `AsyncMLCEngine` 的构造分别传 `"sync"` / `"async"` 给基类，并挂上各自的 `chat`/`completions` 代理：

[python/mlc_llm/serve/engine.py:L869-L889](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine.py#L869-L889) —— `AsyncMLCEngine.__init__` 传 `"async"`，挂 `AsyncChat`/`AsyncCompletion`。

[python/mlc_llm/serve/engine.py:L1441-L1461](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine.py#L1441-L1461) —— `MLCEngine.__init__` 传 `"sync"`，挂 `Chat`/`Completion`。

> 说明：本节聚焦 chat completion 路径。`completions.create`（纯文本补全）走 `_completion`，结构与 chat 完全平行，不再赘述。

#### 4.3.4 代码实践

**实践目标**：用 `MLCEngine` 跑一次流式 chat，对比它与 `JSONFFIEngine` 的用法差异，验证「拼 prompt / 分词在 Python 侧」。

**操作步骤（可运行型）**：直接用仓库自带的示例（**这是项目原有文件**，非示例代码）：

[examples/python/sample_mlc_engine.py:L1-L19](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/examples/python/sample_mlc_engine.py#L1-L19) —— 官方示例：`MLCEngine(model)` + `engine.chat.completions.create(..., stream=True)`。

1. 运行：`python examples/python/sample_mlc_engine.py`（按需把 `model` 改成本地能拉取的 MLC 模型）。
2. 跑通后，对照本节源码，追踪一次 `create` 调用的链路：`ChatCompletion.create` → `_chat_completion` → `_handle_chat_completion`（这里调用 `process_chat_completion_request` 完成**分词**）→ `_generate` → `create_request`/`add_request` → `sync_output_queue.get()`。

**需要观察的现象**：流式输出正常；与 4.1.4 的 JSON FFI 版相比，**最终用户代码几乎一样**（都是 `engine.chat.completions.create(...)`），但底层走的桥不同。

**预期结果**：模型正常回答；你能在源码里指认出「分词发生在 `_handle_chat_completion` 调用的 `process_chat_completion_request`」，而 JSON FFI 版的分词发生在 C++ 的 `CreatePrompt`。

**源码阅读型补充**：对比 [engine_base.py:L548-L553](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L548-L553)（同步 `put_nowait`）与 [engine_base.py:L473-L489](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L473-L489)（异步 `call_soon_threadsafe`），说清同步/异步回送的差别。若本地无模型环境，模型加载与生成「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`AsyncMLCEngine` 的回送回调为什么必须用 `call_soon_threadsafe`，而 `MLCEngine` 直接 `put_nowait` 就行？

**参考答案**：`AsyncMLCEngine` 跑在 `asyncio` 事件循环里，而 C++ 回送循环是**另一个线程**；`asyncio` 默认非线程安全，从别的线程碰它会出问题，必须用 `call_soon_threadsafe` 把任务安全地排进事件循环。`MLCEngine` 是同步阻塞模型，主线程自己 `get()` 队列，`put_nowait` 本身是线程安全的，无需额外协调。

**练习 2**：两条桥在「分词 / 去分词」职责上的分工分别是什么？为什么 REST 服务器（u11-l2）会选择 `AsyncMLCEngine` 而非 `JSONFFIEngine`？

**参考答案**：JSON FFI 桥在 **C++** 侧分词/去分词（`CreatePrompt` + `TextStreamer`）；类型化对象桥在 **Python** 侧分词/去分词（`process_chat_completion_request` + Python 端 `TextStreamer`）。REST 服务器基于 FastAPI（异步），需要在一个事件循环里并发处理大量请求，`AsyncMLCEngine` 的异步接口天然契合；且 Python 侧分词便于做多模态、function calling 等上层定制。故 REST 选 `AsyncMLCEngine`。

## 5. 综合实践

把本讲三条线索（两条桥 + 两个后台循环）串起来，完成一次「全链路追踪」。

**任务**：画一张完整的时序图，描述下面这段用户代码的一次完整调用（从 `create` 到收到最后一块）：

```python
# 示例代码
from mlc_llm import MLCEngine
engine = MLCEngine("HF://mlc-ai/Llama-3-8B-Instruct-q4f16_1-MLC")
for chunk in engine.chat.completions.create(
    messages=[{"role": "user", "content": "Hi"}],
    model="...", stream=True,
):
    print(chunk.choices[0].delta.content, end="")
engine.terminate()
```

要求在图上标注：

1. **Python 调用线程**上的：`ChatCompletion.create` → `_chat_completion` → `_handle_chat_completion`（在此分词）→ `_generate`（`create_request`/`add_request`）→ `sync_output_queue.get()` 阻塞。
2. **C++ 引擎线程**上的：`RunBackgroundLoop` 取指令 → `background_engine_->Step()` 产 delta → 内部回调塞 C++ 队列。
3. **C++ 回送线程**上的：`RunBackgroundStreamBackLoop` 取 delta → 调 `request_stream_callback_`（注意类型化对象桥**不**做 JSON 序列化，直接把对象交给 Python）。
4. **回到 Python**：`_sync_request_stream_callback` → `sync_output_queue` → `_generate` 的 `get()` 返回 → `_request_stream_callback_impl` 去分词 → `yield`。
5. 用虚线标出**两个线程边界**与**两个缓冲队列**（C++ `request_stream_callback_inputs_`、Python `sync_output_queue`）。

**验证方式**：画完后，对照本讲 4.2.2 的流程图与 4.3.3 的源码链接逐条核对；再思考：若把 `MLCEngine` 换成 `JSONFFIEngine`，图里哪几步会变？（提示：分词/去分词从 Python 侧挪到 C++ 侧；回送链上多一步「`GetResponseFromStreamOutput` 把对象序列化成 JSON」；Python 侧 `sync_queue` 里装的是字符串而非对象。）

> 若本地有 GPU/CPU 可跑模型，建议实际运行上面两段代码（`MLCEngine` 与 `JSONFFIEngine` 各一次），在关键函数（`_generate`、`handle_chat_completion`、`GetResponseFromStreamOutput`）临时加 `print` 或用 `logging` 打点，亲眼验证时序。运行结果「待本地验证」。

## 6. 本讲小结

- MLC LLM 有**两条** Python↔C++ 桥：`JSONFFIEngine`（字符串桥，chat CLI 默认）与 `MLCEngine`/`AsyncMLCEngine`（类型化对象桥，Python 主推 API）。二者最终都创建同一个 C++ `ThreadedEngine`。
- JSON FFI 桥以 JSON 字符串为唯一跨界载荷，**C++ 侧**完成拼 prompt、分词、去分词、序列化；Python 侧只发 JSON、收 JSON，是「薄」接口，只支持 `stream=True`。
- 类型化对象桥直接传 TVM 对象，**Python 侧**完成分词/去分词与 OpenAI 响应组装，C++ 引擎当「纯 token 生成器」用。
- 两条桥共享 `ThreadedEngine` 的**两个后台循环**：`RunBackgroundLoop` 跑引擎心跳（`Step()`），`RunBackgroundStreamBackLoop` 把 delta 经回调送回 Python；二者解耦，使生成与回送并行。
- 回送链有**两个缓冲队列**（C++ `request_stream_callback_inputs_`、Python 侧队列），跨越两个线程边界；JSON FFI 桥在 C++↔Python 之间多一步「对象→JSON」序列化。
- `MLCEngine`（同步）用 `queue.Queue` + 阻塞 `get()`；`AsyncMLCEngine`（异步）用 `AsyncRequestStream` + `call_soon_threadsafe` 把回调安全投回事件循环，适配 FastAPI 等异步服务。

## 7. 下一步学习建议

- **继续往下读服务端**：本讲的 `AsyncMLCEngine` 是 u11-l2「REST 服务器与 OpenAI 端点」的底层引擎。下一步去看 `interface/serve.py` 如何把 `AsyncMLCEngine` 装进 FastAPI、用 uvicorn 跑起来，以及 `/v1/chat/completions` 路由如何把 HTTP 请求转成 `chat.completions.create` 调用。
- **深入引擎心跳**：本讲只把 `Step()` 当成黑盒「产 delta」。若想看清 `Step()` 内部如何 prefill/decode/采样，回到 u9-l2（事件-动作循环）与 u10 系列（KV 缓存、采样、推测解码）。
- **看清「契约字符串」**：可以扫一遍 `mlc.json_ffi.CreateJSONFFIEngine` 与 `mlc.serve.create_threaded_engine` 这两个注册点，体会「Python 按名字符串取 C++ 函数」的 PackedFunc 模式——这是整个 TVM 生态跨语言互通的基石。
- **异步引擎与服务治理**：u11-l3 会讲 `EngineConfig` 校验、同步引擎封装与 `ServerContext` 的多模型生命周期管理，承接本讲对 `MLCEngineBase` 的初步认识。

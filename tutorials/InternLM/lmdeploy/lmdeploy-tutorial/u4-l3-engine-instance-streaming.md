# 引擎实例与流式推理 engine_instance

## 1. 本讲目标

上一讲（u4-l1）我们看清了 `Engine` 这个「大管家」：它管理 session/message、绑定 `RequestManager`、启动主循环。但**用户并不直接调用 `Engine`**——用户拿到的是一个轻量的「句柄（handle）」，叫 `EngineInstance`。本讲就回答下面这些问题：

- 用户调一次 `stream_infer`，请求是怎么一步步送到引擎、token 又是怎么一步步流回来的？
- `EngineInstance` 和 `Engine` 到底是什么关系？为什么需要这一层？
- 流式输出的「增量」是怎么算出来的？什么时候停？
- 用户传进来的 prompt（尤其是带图片的多模态输入）在进入调度器之前，被谁预处理过？
- 引擎主循环那一侧，又是谁把调度器的「决定」翻译成 GPU 能吃的张量？

学完本讲，你应该能：

1. 说清 `EngineInstance` 作为「用户面句柄」的职责与构造方式。
2. 画出一次 `stream_infer` 从输入到 `yield` token 的完整数据流。
3. 解释「累计 token → 增量切片」与「请求—回应乒乓」两个关键机制。
4. 区分 `input_process`（消息准入预处理）与 `inputs_maker`（调度结果转张量）这两个完全不同的阶段。

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下概念（来自 u2-l1、u3-l1、u4-l1、u4-l2）：

- **两类同名 Response**：用户面的 `lmdeploy.messages.Response`（含 `text`/`generate_token_len`/`finish_reason`）与引擎面的 `engine/request.py:Response`（带 `asyncio.Event`）。本讲大量出现的是后者。
- **EngineOutput**：引擎每一步产出的「裸输出」，字段有 `status`（`ResponseType`）、`token_ids`、`logprobs`、`cache_block_ids` 等（见 [lmdeploy/messages.py:L685-L707](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L685-L707)）。
- **ResponseType 状态枚举**：`SUCCESS`（又来了一批 token）、`FINISH`（正常结束）、`CANCEL`（被取消）、`INPUT_LENGTH_ERROR`（输入超长）等（见 [lmdeploy/messages.py:L520-L533](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L520-L533)）。
- **Actor 模型的 Engine**：`Engine` 在构造期装配好 `executor/scheduler/req_manager` 但不启动循环；`RequestManager` 是跨执行流的「信箱」，请求按 `request_priority` 分桶派发（u4-l1）。
- **EngineLoop 的多协程结构**：`preprocess_loop` 消费请求喂给调度器，`main_loop` 跑 forward，`send_response_loop` 把产出送回客户端；「派发即计步」，`scheduler.tick()` 在 `inputs_maker._send_next_inputs_impl` 里被调用（u4-l2）。

一句话回顾：`Engine` 是「跑模型的人」，但它只认引擎面的 `Request`；用户面对的是「一问一答」的接口。`EngineInstance` 就是中间的翻译层和信箱。

## 3. 本讲源码地图

本讲涉及三个核心源码文件，外加两个关联文件用于串通调用链：

| 文件 | 作用 | 本讲定位 |
|------|------|---------|
| [lmdeploy/pytorch/engine/engine_instance.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py) | `EngineInstance` 句柄：把一次推理请求翻译成对主循环的请求，并把引擎产出翻译成流式 token。 | **主角**，4.1–4.2 节。 |
| [lmdeploy/pytorch/engine/input_process.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/input_process.py) | 多模态输入预处理钩子：定义 `BaseModelInputProcessor` 抽象与 `DefaultModelInputProcessor` 直通实现。 | 4.3 节，消息准入阶段的扩展点。 |
| [lmdeploy/pytorch/engine/inputs_maker.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py) | `InputsMakerAsync`：把调度器决策翻译成 GPU 张量（prefill/decode/长上下文分块），并经 `executor` 派发 forward。 | 4.4 节，引擎主循环一侧。 |
| [lmdeploy/pytorch/engine/request.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py) | `RequestSender`/`RequestManager`：信箱与请求/响应对象、`async_recv` 等待机制。 | 4.1–4.2 节引用。 |
| [lmdeploy/pytorch/engine/engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py) | `Engine.create_instance` 创建句柄；`_on_add_message` 在准入时调 `input_processor`。 | 串联 4.3 节。 |

全局数据流（**这张图是本讲的总纲**）：

```
用户
 │  stream_infer(session_id, input_ids, gen_config)
 ▼
EngineInstance ──send_async(ADD_MESSAGE)──► RequestManager(信箱) ──► Engine._on_add_message
(async 生成器)                                            │ 调 input_processor.preprocess_input (input_process.py)
 ◲ ping-pong ◣                                            ▼ 建立 SchedulerSequence
 ◲  await async_recv(resp) ◣                        Scheduler(准入/分桶)  ← u4-l4
 ◲        ▲                                         │
 ◲  resp.event.set() 唤醒                           ▼
 send_response_loop ◄──forward 产出── executor ◄── InputsMakerAsync (inputs_maker.py)
 (engine_loop.py)                                 把调度结果→张量, do_prefill 决定 prefill/decode
```

注意上下两端：**`EngineInstance` 在「用户侧」逐请求工作，`InputsMakerAsync` 在「引擎侧」逐 batch 工作**——它们从不直接对话，唯一的桥梁是 `RequestManager` 的请求/响应事件。

## 4. 核心概念与源码讲解

### 4.1 EngineInstance：从 Engine 到用户句柄

#### 4.1.1 概念说明

为什么需要 `EngineInstance` 这一层？因为 `Engine` 是一个**共享的单例**：整个进程里只有一个引擎、一套 GPU、一个调度器、一个主循环。但服务端常常要同时服务成百上千个用户请求，每个用户都需要：

- 自己的 session 上下文（多轮对话）；
- 自己的流式输出通道（互不干扰）；
- 自己的「取消」「结束」控制。

如果把这套状态直接塞进 `Engine`，多用户就会互相串台。于是 LMDeploy 引入**句柄模式（Handle Pattern）**：

> `Engine` 是共享的服务器；`EngineInstance` 是每个客户端拿到的一条「专线」。多条专线共享同一个服务器，但各自有独立的信箱地址（`sender_id`）。

这层对应到代码就是 [lmdeploy/pytorch/engine/engine_instance.py:L119-L138](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L119-L138)。`EngineInstance` 本身**不含任何模型权重、不跑 forward**，它只持有两样东西：

1. 一个指向 `Engine` 的引用（`self.engine`）；
2. 一个属于自己的 `RequestSender`（`self.req_sender`），即那条「专线」。

#### 4.1.2 核心流程

`EngineInstance` 的生命周期：

```
Engine.create_instance()
   └─► EngineInstance(engine)
         │ self.engine      = engine              # 共享引擎
         │ self.req_sender  = engine.req_manager.build_sender()  # 新建一条专线（拿到唯一 sender_id）
         │ self.max_input_len = engine.max_session_len           # 输入长度上限
         └─ 用户调用 stream_infer / infer / cancel / end
              └─► __del__(): 从 req_manager.senders 里把自己摘掉，释放专线
```

关键点：`build_sender()` 每调一次就 `sender_id` 自增，并把新建的 `RequestSender` 登记进 `RequestManager.senders` 字典。主循环在回送响应时，靠 `sender_id` 找到正确的专线（见 [lmdeploy/pytorch/engine/request.py:L292-L298](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L292-L298)）。

#### 4.1.3 源码精读

`Engine.create_instance` 非常薄，只是**延迟导入**并 new 一个句柄（延迟导入是为了避免循环依赖）：

[lmdeploy/pytorch/engine/engine.py:L661-L670](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L661-L670) —— `Engine` 创建句柄：参数 `cuda_stream_id` 仅为与 TurboMind 后端 API 对齐而保留的空形参。

`EngineInstance.__init__` 才是关键：

[lmdeploy/pytorch/engine/engine_instance.py:L126-L138](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L126-L138) —— 构造句柄：建立专线、记录输入长度上限；析构时把专线从管理器里移除。

注意 `max_input_len` 取的是 `engine.max_session_len`（见 [lmdeploy/pytorch/engine/engine.py:L193](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L193)），它就是「一个会话最多能塞多少 token」的上限。这个值会在 4.2 节的流式入口里做第一道长度校验。

#### 4.1.4 代码实践

**实践目标**：确认「多个句柄共享同一个引擎，但各自有独立 sender_id」。

**操作步骤**（源码阅读型）：

1. 打开 [engine_instance.py:L126-L128](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L126-L128)，确认 `__init__` 只做 `build_sender()`。
2. 打开 [request.py:L292-L298](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L292-L298)，看 `build_sender` 如何 `self._next_sender_id += 1`。
3. 在脑海里模拟：对同一个 `engine` 连调两次 `engine.create_instance()`，得到 `inst1`、`inst2`，它们的 `req_sender.sender_id` 分别是 `0` 和 `1`，但 `inst1.engine is inst2.engine is engine` 为真。

**需要观察的现象**：两个句柄的 `sender_id` 不同；两个句柄的 `.engine` 是同一个对象。

**预期结果**：句柄是「便宜的」——创建它不分配显存、不复制权重，只是登记一个 sender。这正是服务端能为每个请求新建句柄而不会爆显存的原因。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `EngineInstance.__del__` 删掉（即不执行 [engine_instance.py:L136-L138](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L136-L138) 的 `senders.pop`），长时间运行的服务端会出现什么问题？

> **答案**：`RequestManager.senders` 字典会无限增长，泄漏已结束请求的 `RequestSender` 对象（以及它们持有的 `resp_dict`），最终造成内存缓慢上涨。

**练习 2**：`cuda_stream_id` 这个参数在 PyTorch 后端里真的控制了 CUDA 流吗？

> **答案**：没有。它只是为了和 TurboMind 后端的 `create_instance(cuda_stream_id)` 接口签名对齐而保留的形参，PyTorch 实现里完全没用到（见 u4-l1 提到的「空形参」）。

### 4.2 stream_infer：流式推理的请求—回应乒乓

#### 4.2.1 概念说明

`stream_infer` 是用户最常用的接口，它是一个**生成器（generator）**：每 `yield` 一次，就吐出「这一步新生成的 token」。要理解它，必须先理解 LMDeploy 推理的**乒乓（ping-pong）模型**：

> 用户把请求送进信箱 → 引擎主循环跑一步 forward → 把这一步的新 token 写回信箱、点亮一个事件 → 用户被唤醒、取走增量 token → 再次进入信箱等待下一步 …… 如此往复，直到引擎返回 `FINISH`/`CANCEL`。

这里的关键是「**事件（asyncio.Event）**」：用户侧的 `async_recv` 会阻塞在 `resp.event` 上；引擎侧每完成一步 forward，就 `resp.event.set()` 点亮它，唤醒用户。这就是 [lmdeploy/pytorch/engine/request.py:L139-L154](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L139-L154) 的 `async_recv` 干的事——它甚至带 `wait_main=True`，先调 `prepare_send()` 做发送端限速，再死等事件点亮。

另一个关键概念是**累计 vs 增量**：引擎主循环为了方便内部统计，每步回送的 `token_ids` 是「**到目前为止生成的全部 token**」（累计值）；而用户要的是「**这一步新增的 token**」（增量值）。`EngineInstance` 负责做这道减法（4.2.3 详述）。

#### 4.2.2 核心流程

一次 `stream_infer` 的完整步骤（对应 `async_stream_infer`）：

```
1. 输入长度校验：len(input_ids) > max_input_len ?  → yield INPUT_LENGTH_ERROR 并 return
2. 采样参数：SamplingParam.from_gen_config(gen_config)
3. send_async(ADD_SESSION)            # 登记会话（异步、不等回应）
4. 组装 message 字典（token_ids/sampling_param/multimodal/...）
5. resp = send_async(ADD_MESSAGE, msg) # 投递真正的推理请求，拿到一个带 Event 的 Response
6. notify_add_msg_func()              # 可选：通知上层「消息已投递」
7. while True:                         # —— 乒乓循环 ——
     resp = await async_recv(resp, wait_main=True)   # 阻塞等引擎点亮事件
     if SUCCESS:    yield 增量 token; output_offset 前移;  继续
     if FINISH/CANCEL: yield 最终输出(含 logits/ce_loss/routed_experts); break
     else (其它错误):  yield 错误输出; break
```

#### 4.2.3 源码精读

**第一段：输入校验与请求投递**。

[lmdeploy/pytorch/engine/engine_instance.py:L196-L217](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L196-L217) —— 长度守卫、采样参数、登记会话、投递 `ADD_MESSAGE`。

注意几个细节：

- 第 196-198 行的长度校验是**用户侧第一道防线**，超长直接返回，不进引擎，省一次往返。
- `send_async`（[request.py:L135-L137](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/request.py#L135-L137)）是「投了就走」，立即返回一个挂着 `asyncio.Event` 的 `Response`，**不等结果**。
- 第 203-212 行组装的 `msg` 字典把 `gen_config` 里的 `migration_request`/`with_cache`/`preserve_cache` 一并塞进去——这些是 PD 分离（u9-l5）和前缀缓存（u9-l3）用的开关。
- 第 216-217 行 `notify_add_msg_func`：这是一个回调钩子，serve 层在多请求调度时会用它来唤醒分发协程，告诉它「这条消息已经进队了」。

**第二段：乒乓循环与增量切片**。

[lmdeploy/pytorch/engine/engine_instance.py:L222-L270](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L222-L270) —— `async_recv` 等待、按 `resp.type` 三分支处理、增量切片、停止条件。

把增量切片的数学关系写成公式。设第 \(k\) 步引擎累计生成的 token 序列为 \(T_k\)（长度 \(|T_k|\)），`output_offset` 记录到上一步为止已经 `yield` 出去的数量 \(o_{k-1}\)，则本步增量长度为：

\[
\Delta_k = |T_k| - o_{k-1}, \qquad \text{yield} = T_k[\,o_{k-1} : |T_k|\,], \qquad o_k = |T_k|
\]

对应代码就是第 231-238 行的 `num_ids = len(token_ids) - output_offset` → `yield ... token_ids[output_offset:]` → `output_offset = len(token_ids)`。引擎给的是累计 \(T_k\)，句柄算出增量切片再吐给用户。

**停止条件**在第 239-270 行，分两种「正常结束」：

- `FINISH`：模型自然结束（命中 EOS 或 stop words 或达到 `max_new_tokens`）。这时还会再 `yield` 一次最终增量（如果有），并附带 `logits`、`ce_loss`（`return_ppl` 时）、`routed_experts`（MoE 路由统计）等「收尾数据」，然后 `break`。
- `CANCEL`：被用户主动取消（见 4.2.4）。处理路径与 FINISH 几乎一致，也是收尾后 `break`。
- 其它任何 `resp.type`（如 `ENGINE_STOP_ERROR`）走 `else` 分支：`yield EngineOutput(resp.type, [])` 后 `break`，表示异常终止。

**第三段：同步包装**。`stream_infer`（[engine_instance.py:L301-L336](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L301-L336)）是个**同步生成器**，它内部把异步的 `async_stream_infer` 用 `req_sender.run_until_complete(coro_gen.__anext__())` 一步步驱动。这正是 u3-l1 讲过的「同步外观 + 异步内核 + 事件循环线程桥接」模式在句柄层的体现：`__anext__()` 取下一个异步 yield，`run_until_complete` 把它跑完并阻塞当前（用户）线程，直到拿到结果。

> **呼应 u4-l2**：乒乓的「另一端」是 [engine_loop.py:L213-L231](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L213-L231) 的 `_send_resp` → `response_reqs` → `req_manager.response(resp)`，最终就是 `resp.event.set()`。点灯的那行在 [engine.py:L78-L89](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L78-L89)，其中第 84-85 行有个关键守卫：**一旦 `resp.type == FINISH` 就直接 return，不再覆盖**——保证「FINISH 是终点」这一语义不会被后续误改。

#### 4.2.4 代码实践

**实践目标**：跟踪一次 `stream_infer` 从输入到 `yield` 的完整路径，并验证「增量切片」逻辑。

**操作步骤**（源码阅读 + 本地可选运行）：

1. **静态追踪**：从 [engine_instance.py:L196](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L196) 出发，依次标注：① 长度校验行；② `ADD_MESSAGE` 投递行；③ `async_recv` 等待行；④ SUCCESS 分支的增量切片行；⑤ FINISH 分支的 break 行。
2. **动态验证（可选，待本地验证）**：写一个最小脚本，用 `pipeline(...)` 拿到底层引擎实例（或直接 `engine.create_instance()`），对一个短 prompt 调 `stream_infer`，把每次 `yield` 的 `outputs.status` 和 `len(outputs.token_ids)` 打印出来。预期你会看到一连串 `SUCCESS`（每段 1 个或几个 token），最后是一个 `FINISH`。

   ```python
   # 示例代码：仅演示流式消费的写法，实际运行需已构建好的引擎实例 engine
   inst = engine.create_instance()
   for outputs in inst.stream_infer(session_id=0,
                                    input_ids=[1, 2, 3],          # 实际应传 tokenizer 编码后的 id
                                    gen_config=None):
       print(outputs.status, len(outputs.token_ids))
   ```

3. **观察增量**：在 SUCCESS 分支里，`outputs.token_ids` 的长度通常很小（decode 阶段多为 1）；如果你改成打印「累计」概念，会发现引擎内部其实是累计的——句柄层已经替你切好了。

**需要观察的现象**：流式输出由若干 `SUCCESS` 段 + 1 个 `FINISH` 段组成；取消时最后一段是 `CANCEL`。

**预期结果**：每段 `SUCCESS` 携带本步新增 token，`FINISH` 段携带收尾字段。若无法本地运行，记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `async_stream_infer` 里投递 `ADD_SESSION` 用的是 `send_async`（不等回应），而不是 `async_send`（等回应）？

> **答案**：因为会话登记是「尽力而为」的预处理步骤，引擎侧的 `_on_add_message` 会再次校验 session 是否存在（见 [engine.py:L394-L396](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L394-L396)），重复登记只会得到 `SESSION_REPEAT`（见 [engine_instance.py:L39-L40](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L39-L40) 的容忍处理）。用 `send_async` 避免一次往返延迟，让消息尽快进队。

**练习 2**：如果用户在第 7 步「乒乓循环」里长时间不消费生成器（比如 `async for` 之间 sleep 10 秒），引擎会怎样？

> **答案**：引擎主循环并不阻塞等待用户消费——它照常跑 forward 并把结果存进 `resp.data`、点亮事件。用户侧的 `async_recv` 会一直阻塞在事件上；当用户最终醒来取走结果，事件被 `clear()`，进入下一轮乒乓。也就是说，慢消费不会拖慢引擎，但用户侧会「积压」一步（下一次 `async_recv` 几乎立刻返回，因为事件早已被点亮）。代价是用户感知到的 token 流会成块到达，而非平滑流出。

**练习 3**：`async_infer`（[engine_instance.py:L272-L299](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L272-L299)）和 `stream_infer` 是什么关系？

> **答案**：`async_infer` 是「非流式」接口，但它**内部完全复用** `async_stream_infer`：用一个 `async for` 把生成器消费到底，期间丢弃中间 `SUCCESS` 段，只在状态不属于 `SUCCESS/FINISH` 时提前返回，否则返回最后那个 `outputs`（即 `FINISH` 段）。所以非流式 = 流式 + 全量消费。

### 4.3 input_process：多模态输入预处理钩子

#### 4.3.1 概念说明

`input_process.py` 是一个非常小（仅 ~45 行）但**很关键**的扩展点文件。它解决的问题是这样的：

用户的 prompt 里可能混着图片、视频、音频等多模态数据。但调度器、KV cache、模型 forward 只认 **token id**。把「一张图 + 一段文字」变成「模型能吃的 token 序列 + 视觉 embedding」这件脏活，每个模型族（InternVL、Qwen-VL、LLaVA……）做法都不同。

LMDeploy 没有把这堆「模型特有的预处理逻辑」写死在引擎里，而是定义了一个**抽象基类** `BaseModelInputProcessor`，由各模型自己提供实现，引擎在**消息准入时**统一调用它。`input_process.py` 只提供「接口契约」和「默认直通实现」。

#### 4.3.2 核心流程

`input_process` 发生在数据流的**早期**，在调度器之前：

```
EngineInstance 投递 ADD_MESSAGE
   └─► Engine._on_add_message(reqs)          # engine.py:388
         ├─ for req in reqs:
         │    if 有 input_multimodals 且 input_processor 存在:
         │        result = input_processor.preprocess_input(input_ids, input_multimodals)  # ← 本节主角
         │        req_data['token_ids']         = result.input_ids
         │        req_data['input_multimodals'] = result.input_multimodals
         └─ self._add_message(valid_reqs)     # 真正进调度器
```

注意：`preprocess_input` 返回的是 `PreprocessInputResult`——一个只有三个字段的轻量数据类。

#### 4.3.3 源码精读

**结果类型**：

[lmdeploy/pytorch/engine/input_process.py:L13-L18](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/input_process.py#L13-L18) —— `PreprocessInputResult`：`input_ids`（token 序列）、`input_multimodals`（规整后的多模态数据，可能含视觉 embedding）、`model_metas`（模型需要的元信息）。

**抽象基类**：

[lmdeploy/pytorch/engine/input_process.py:L21-L30](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/input_process.py#L21-L30) —— `BaseModelInputProcessor.preprocess_input` 是抽象方法，由具体模型实现。

**默认直通实现**：

[lmdeploy/pytorch/engine/input_process.py:L33-L44](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/input_process.py#L33-L44) —— `DefaultModelInputProcessor`：什么也不改，原样返回。纯文本模型用它即可。

**调用点**（在 Engine 里）：

[lmdeploy/pytorch/engine/engine.py:L398-L423](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L398-L423) —— `_on_add_message` 里：只有当请求带 `input_multimodals` 且引擎持有 `input_processor` 时才调预处理；否则发出警告。第 415 行是真正的调用，第 419-420 行在前缀缓存开启时还会给多模态内容算 hash 以便缓存命中。

> **真实实现的来源**：`self.input_processor = self.executor.get_input_processor()`（[engine.py:L176](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L176)），它一路委托到 `patched_model.get_input_processor()`（见 [lmdeploy/pytorch/engine/model_agent/agent.py:L1196-L1198](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py#L1196-L1198)）。也就是说，**处理器是「模型自己提供的」**，纯文本模型没有 `get_input_processor`，executor 会回退到 `DefaultModelInputProcessor`。具体的 VLM 处理器散布在 `models/` 下，例如 `internvl.py` 的 `InternVLInputProcessor`、`qwen2_vl.py` 的 `Qwen2VLInputProcessor`、`llava.py` 的 `LLavaInputProcessor` 等，它们都继承自本文件的 `BaseModelInputProcessor`。

#### 4.3.4 代码实践

**实践目标**：确认「input_process 是消息准入阶段的模型级钩子」，并找到一个真实处理器。

**操作步骤**：

1. 在仓库内搜索继承 `BaseModelInputProcessor` 的真实实现（例如 [lmdeploy/pytorch/models/internvl.py:L915-L952](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/internvl.py#L915-L952) 的 `InternVLInputProcessor`）。
2. 对比 `InternVLInputProcessor.preprocess_input` 与本讲的 `DefaultModelInputProcessor.preprocess_input`：前者会真正调用视觉编码器、算 image token 数、扩展 `input_ids`；后者直接 `return`。
3. 回到 [engine.py:L415](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L415)，确认这一步发生在 `_add_message`（进调度器）**之前**。

**需要观察的现象**：所有 VLM 模型都把「图片怎么变成 token/embedding」的逻辑放在自己的 `InputProcessor` 子类里，引擎本体完全不知道这些细节。

**预期结果**：你能在 `models/` 下找到至少 5 个 `XxxInputProcessor`，它们都 `from lmdeploy.pytorch.engine.input_process import BaseModelInputProcessor`，且都在模型类里通过 `get_input_processor()` 暴露。

#### 4.3.5 小练习与答案

**练习 1**：如果 `language_model_only=True`（见 [engine.py:L409-L413](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L409-L413)），传了图片会怎样？

> **答案**：引擎会把 `input_multimodals` 置为 `None` 并打印警告，然后**不调用** `input_processor`，图片被丢弃，请求按纯文本处理。这正好对应 u2-l5 提到的 `language_model_only` 可强制走纯文本。

**练习 2**：`input_process` 阶段和 `inputs_maker` 阶段都会处理多模态数据，它们会重复劳动吗？

> **答案**：不会，职责不同。`input_process` 在**消息准入时**（每条请求一次）把原始多模态数据规整成 token id + 结构化多模态描述；`inputs_maker`（4.4 节）在**每次 forward 前**（每个 batch 多次）把这些结构化描述拼成 GPU 张量（`VisionModelInputs`）。前者是「逻辑层规整」，后者是「张量层打包」。

### 4.4 inputs_maker：从调度结果到 GPU 张量

#### 4.4.1 概念说明

到目前为止，用户侧的故事讲完了：请求进了信箱，被 `_on_add_message` 预处理并建成 `SchedulerSequence`，进了调度器。但调度器只会输出一个**决策**——「这一步该 prefill 哪些序列、decode 哪些序列、哪些块映射给谁」。GPU 不能直接吃「决策」，它要的是**张量**：`input_ids`、`seq_length`、`block_offsets`、`history_lengths`……

`InputsMakerAsync` 就是「决策 → 张量」的翻译官。它运行在引擎主循环里（只有一个实例，由 `build_inputs_maker(engine)` 创建，见 [inputs_maker.py:L1178-L1189](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L1178-L1189)），与 `EngineInstance` 形成鲜明对比：

| 维度 | `EngineInstance` | `InputsMakerAsync` |
|------|------------------|--------------------|
| 所在侧 | 用户侧 | 引擎主循环侧 |
| 数量 | 每请求/每客户端一个 | 全引擎唯一 |
| 工作粒度 | 单条请求、逐 token | 整个 batch、每次 forward |
| 输入 | `session_id`/`input_ids` | 调度器输出的 `running` 序列 |
| 输出 | 流式 `EngineOutput` | `ModelInputs`/`ModelInputsDelta` 张量 |

它的核心职责可以归纳为三句话（也写在它的类文档里）：

1. **决定本轮是 prefill 还是 decode**（`do_prefill`）。
2. **把调度结果构造成张量**（`create_model_inputs` / `create_model_inputs_delta`）。
3. **挂上采样、停止、LoRA 等元信息后派发 forward**（`_send_next_inputs_impl`）。

#### 4.4.2 核心流程

一次「构造输入 + 派发」的流程（对应 `_make_forward_inputs` + `_send_next_inputs_impl`）：

```
EngineLoop.main_loop 调用
   └─► InputsMakerAsync.send_next_inputs()
         ├─ prefill = do_prefill()              # 决定 prefill/decode（do_prefill_default/chunked/pnode）
         └─► _send_next_inputs_impl(prefill)
               ├─ forward_inputs = _make_forward_inputs(prefill)
               │     ├─ 若 prefill: scheduler.schedule(is_prefill=True) → create_model_inputs (full)
               │     │     ├─ 若是长上下文: LongContextChunker 切成 model-safe 的块
               │     ├─ 若 decode : create_model_inputs_delta() (基于 running_seqs)
               │     ├─ 挂 sampling_inputs / stopping_criteria / extra_inputs
               │     └─ 返回 dict{running, inputs, delta, ...}
               ├─ await executor.forward_async(forward_inputs)   # 真正把张量送进 GPU
               ├─ scheduler.tick()             # ← 呼应 u4-l2「派发即计步」
               └─ 缓存 forward_inputs 供下一轮 prefetch
```

#### 4.4.3 源码精读

**决定 prefill/decode**。三种策略由 `_init_do_prefill` 在构造时按角色/配置选定（[inputs_maker.py:L344-L350](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L344-L350)）：

- PD 分离的 prefill 节点 → `do_prefill_pnode`（恒 prefill）；
- 开启 chunked prefill → `do_prefill_chunked`；
- 默认 → `do_prefill_default`。

默认策略 [inputs_maker.py:L1108-L1141](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L1108-L1141) 是一组启发式：没有 waiting 且没有挂起的长块就 decode；连续 decode 轮数超过 `prefill_interval` 就强制 prefill；waiting 中 token 太多就 prefill；running + ready 少于 `max_batches` 的一半就 prefill；否则继续 decode。这套规则保证了 prefill（吃显存多、吞吐高）和 decode（逐 token、延迟敏感）之间的平衡。

**构造 prefill 张量**。[inputs_maker.py:L497-L591](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L497-L591) 的 `create_model_inputs` 把一批序列拼成 `ModelInputs`。核心字段对应的数学关系：

\[
\text{kv\_seqlen}_i = \text{seq\_length}_i + \text{history\_length}_i
\]

即第 \(i\) 条序列的 KV 总长度 = 本轮新增长度 + 历史长度（见 [inputs_maker.py:L523](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L523)）。`block_offsets` 是 Paged Attention 的核心——每条序列映射到的物理 KV 块号表，由调度器 `get_block_tables` 给出，再经 `_map_to_kernel_block_offsets` 把「管理块」映射成「kernel 块」（见 [inputs_maker.py:L474-L495](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L474-L495) 的示例注释）。视觉输入由 `_create_vision_model_inputs` 单独打包成 `VisionModelInputs`（[inputs_maker.py:L394-L456](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L394-L456)）。

**构造 decode 张量**。[inputs_maker.py:L672-L736](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L672-L736) 的 `create_model_inputs_delta` 不再重建整批，而是基于上一轮的 `running_seqs` 生成轻量 `ModelInputsDelta`。这里有个关键细节（代码注释里点名的 #4024 bug）：`kv_seqlens` 用 `seq.num_all_ids + max_q_seqlen`（[inputs_maker.py:L710](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L710)），因为 `num_all_ids` 可能滞后一步——`EngineLoop` 会在 forward 前就预取下一批输入，所以补一个 `max_q_seqlen` 才能恢复本轮真实的 KV 长度。

**长上下文分块**。[inputs_maker.py:L119-L254](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L119-L254) 的 `LongContextChunker` 处理「单条 prompt 超过 `max_prefill_token_num`」的情况：把它切成多个 model-safe 的块，每块单独 forward；**多模态 span 不可分割**，所以遇到一个比块上限还大的图片 span，会临时抬高块上限（见类文档说明）。

**派发与计步**。[inputs_maker.py:L1151-L1165](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L1151-L1165) 的 `_send_next_inputs_impl` 是「派发即计步」的落点：构造完 `forward_inputs` 就 `await self.executor.forward_async(forward_inputs)` 把张量送进 GPU，紧接着 `self.scheduler.tick()` 让调度器步进。`send_next_inputs` 与 `prefetch_next_inputs` 都走这条路，区别在于后者 `enable_empty=True`——允许「空 forward」，这就是 u4-l2 讲的「CPU 准备输入与 GPU 计算 forward 重叠」的预取机制。

#### 4.4.4 代码实践

**实践目标**：理清 `InputsMakerAsync` 在主循环中的位置，并验证「派发即计步」。

**操作步骤**（源码阅读型）：

1. 从 [engine_loop.py:L472](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L472) 的 `main_loop` 入手，找到 `_main_loop_try_send_next_inputs`（[engine_loop.py:L399-L407](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L399-L407)），确认它调用的是 `self.inputs_maker.send_next_inputs()`。
2. 顺着 `send_next_inputs` → `_send_next_inputs_impl`（[inputs_maker.py:L1167-L1169](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L1167-L1169) 与 [L1151-L1165](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L1151-L1165)），标出 `forward_async` 与 `scheduler.tick()` 两行。
3. 估算 KV 占用（结合 u3-l2 的公式）：若一个模型 `block_size=16`、单层 KV 每 token 占 \(b\) 字节、共 \(L\) 层，则一条长度 \(N\) 的序列需要 \(\lceil N/16\rceil\) 个块。

**需要观察的现象**：`scheduler.tick()` 紧跟在每次 `forward_async`（含预取）之后，不在 `engine_loop.py` 里。

**预期结果**：确认 u4-l2 的结论——「每次 forward 派发（含预取）都让 `scheduler_tick` 自增」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 decode 用「delta（增量）」而不是像 prefill 那样重建整批张量？

> **答案**：decode 阶段每条序列只新增 1 个 token，整批结构（哪些序列、它们的 block 表、model_agent 的持久 `StepInputs`）基本不变。重建整批张量开销大且会破坏 CUDA Graph 的静态结构；用轻量 `ModelInputsDelta` 只更新变化的部分（block_offsets、indices），才能复用上一轮的持久状态，这也是 decode 路径能上 CUDA Graph 的前提。

**练习 2**：`do_prefill_default` 在什么条件下会**强制**切回 prefill？

> **答案**：三种情况（见 [inputs_maker.py:L1121-L1137](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L1121-L1137)）：① 连续 decode 轮数达到 `prefill_interval`；② waiting 中的 token 总数达到 `max_prefill_token_num`；③ running + ready 序列数少于 `max_batches` 的一半。本质都是「该补新请求进来了」。

## 5. 综合实践

把本讲四个模块串起来，做一次**端到端调用链标注**。任务是：给一条带图片的多模态 prompt，画出它从「用户调 `stream_infer`」到「第一个 token 流回用户」的完整路径，并在每个关键节点标注「谁、在哪个文件、做了什么」。

建议步骤：

1. **起点**：用户调 `inst.stream_infer(session_id, input_ids, multimodal=[...])`（[engine_instance.py:L301](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L301)）。
2. **翻译**：`stream_infer` 内部驱动 `async_stream_infer`，经长度校验（L196）、采样参数（L200）、投递 `ADD_MESSAGE`（L214）。
3. **预处理**：`Engine._on_add_message` 收到请求，调 `input_processor.preprocess_input`（[engine.py:L415](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L415)），把图片变成 token + 视觉数据。
4. **准入**：`_add_message` → 调度器建 `SchedulerSequence`（u4-l4）。
5. **构造张量**：`EngineLoop.main_loop` → `inputs_maker.send_next_inputs` → `_make_forward_inputs`（prefill 分支）→ `create_model_inputs` + `_create_vision_model_inputs`（[inputs_maker.py:L497](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L497)、[L394](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L394)）。
6. **派发**：`executor.forward_async` + `scheduler.tick()`（[inputs_maker.py:L1161-L1163](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L1161-L1163)）。
7. **回送**：`send_response_loop` → `_send_resp` → `response_reqs` → `resp.event.set()`（[engine_loop.py:L213-L231](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L213-L231)、[engine.py:L89](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L89)）。
8. **收尾**：`EngineInstance.async_recv` 唤醒 → SUCCESS 分支增量切片 → `yield EngineOutput`（[engine_instance.py:L229-L238](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_instance.py#L229-L238)）。

**交付物**：一张包含 8 个节点、每个节点带 `文件:行号` 的流程图（手绘或文字版均可）。**待本地验证**：若本地有可运行的小模型，可在每个节点加日志（`LMDEPLOY_LOG_LEVEL=DEBUG`）核对调用顺序。

## 6. 本讲小结

- `EngineInstance` 是**轻量用户句柄**：它不持有权重、不跑 forward，只持有一个共享 `Engine` 引用和一条独占的 `RequestSender`「专线」（`sender_id`），使多用户互不串台。
- `stream_infer` 基于**请求—回应乒乓**：用户 `send_async` 投递 `ADD_MESSAGE` 后，在 `async_recv` 里阻塞等 `resp.event.set()`；引擎每跑一步 forward 就点亮一次事件，用户醒来取走增量。
- **增量切片**：引擎回送的是累计 token，句柄用 `output_offset` 做减法 \(\Delta_k = |T_k| - o_{k-1}\)，只把新增部分 `yield` 给用户；`FINISH`/`CANCEL` 是终止信号，`INPUT_LENGTH_ERROR` 是用户侧第一道长度防线。
- `input_process.py` 是**消息准入阶段**的模型级预处理钩子：`BaseModelInputProcessor` 是抽象接口，`DefaultModelInputProcessor` 是纯文本直通，真实多模态处理器由各 VLM 模型自带；它把原始图片/视频规整成 token + 结构化数据，发生在调度器之前。
- `InputsMakerAsync` 是**引擎主循环侧**的唯一翻译官：把调度器决策翻成 GPU 张量（prefill 用 `create_model_inputs`、decode 用 `create_model_inputs_delta`），决定 prefill/decode（`do_prefill*`），处理长上下文分块（`LongContextChunker`），并在 `forward_async` 后调用 `scheduler.tick()`——「派发即计步」。
- `EngineInstance`（用户侧、逐请求）与 `InputsMakerAsync`（引擎侧、逐 batch）是数据流的两端，**唯一的桥梁是 `RequestManager` 的请求/响应事件**。

## 7. 下一步学习建议

本讲把「用户接口 → 引擎内部」的衔接讲完了。接下来：

- **u4-l4 调度器 Scheduler**：本讲反复提到「调度器输出决策」，但没讲决策怎么做的。下一讲深入 `_schedule_prefill` / `_schedule_decoding`，看清持续批处理的准入与抢占。
- **u4-l5 分块 KV 缓存与 BlockManager**：本讲提到 `block_offsets`、`get_block_tables`、`create_model_inputs_delta` 里的块映射，这些都依赖物理块管理器。下一讲讲清块的分配/释放/驱逐。
- **u9-l1 VLM 处理**：本讲的 `input_process` 只是 VLM 链路的入口。若想完整理解「图片如何变成 embedding」，请读 u9-l1 与 `vl/model/qwen3.py` 等参考实现。
- **源码延伸**：想看 serve 层如何消费 `EngineInstance`，可读 `lmdeploy/serve/core/async_engine.py` 的 `safe_run`（[L427-L429](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L427-L429)），它正是用 `handle.async_stream_infer` 驱动整个 HTTP 流式响应。

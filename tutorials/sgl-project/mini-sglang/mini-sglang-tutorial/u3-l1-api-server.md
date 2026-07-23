# API Server 与 OpenAI 兼容接口

## 1. 本讲目标

本讲聚焦 Mini-SGLang 的「门面」——跑在主进程里的 FastAPI 前端 `api_server.py`。它是用户唯一直接打交道的入口：接收 HTTP 请求，把它翻译成内部消息送进 tokenizer/scheduler 流水线，再把生成结果以流式或一次性 JSON 返回。

读完本讲，你应该能够：

- 说清 `/generate`、`/v1/chat/completions`、`/v1/models` 这几条路由分别做什么、走不走模型推理。
- 解释 `FrontendManager` 如何用 `uid` + `asyncio.Event` 把「一个 HTTP 请求」和「异步到达的多条增量回复」精准配对。
- 区分流式 SSE 与非流式聚合两条返回路径的实现差异。
- 理解 `stream_with_cancellation` 如何在客户端断连时中止请求，并向后台发出 `AbortMsg`。
- 独立给这个前端新增一个 `/health` 端点，并说清它为何不必经过 tokenizer/scheduler。

## 2. 前置知识

本讲默认你已读过前置讲义 **u1-l4（进程架构）** 与 **u2-l3（消息与序列化）**，已经知道：

- 系统是「API Server / Tokenizer / Detokenizer / 每个 GPU 一个 Scheduler」的多进程架构，前端跑在主进程。
- 请求身份靠一个全局自增的 `uid` 串起 `TokenizeMsg → UserMsg → DetokenizeMsg → UserReply` 的整条环（见 u1-l4 的 8 步生命周期）。
- 进程间用 ZMQ 传轻量控制消息（含 1D tensor），重型张量走 NCCL；`serialize_type`/`deserialize_type` 负责消息编解码。

下面几个概念本讲会用到，先做个最简解释：

- **FastAPI**：一个基于 Python 类型注解的现代 Web 框架。你把一个函数用 `@app.post("/path")` 装饰，并把参数声明成某个 pydantic 模型，FastAPI 就会自动解析请求体、做校验，并把这个函数注册成一条 HTTP 路由。
- **pydantic `BaseModel`**：一个数据校验模型。声明字段类型后，传入的 JSON 会被自动解析、类型转换、越界报错。
- **Server-Sent Events（SSE）**：一种让服务器「持续向浏览器/客户端推数据」的 HTTP 流式协议，正文是一行行 `data: <内容>\n\n`。OpenAI 的流式接口用的就是它。
- **`asyncio.Event`**：Python 异步里的「一次性门铃」。`set()` 按响、`wait()` 等响、`clear()` 复位。本讲用它做「来了一条回复就唤醒等待者」的信号。

一句话定位：`api_server.py` 既是 HTTP 适配层（把 OpenAI 协议翻成内部消息），又是**前端的异步事件调度器**（靠 `uid` 把请求和回复缝合起来）。

## 3. 本讲源码地图

本讲的主战场只有一个文件，其余是理解它所需的依赖：

| 文件 | 作用 |
| --- | --- |
| [python/minisgl/server/api_server.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py) | **本讲主角**：FastAPI app、所有路由、`FrontendManager`、shell 交互、`run_api_server` 启动入口。 |
| [python/minisgl/server/launch.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py) | `launch_server` 在此调用 `run_api_server`，并 spawn 出 scheduler/tokenizer 子进程。说明前端「何时被创建」。 |
| [python/minisgl/server/args.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py) | `ServerArgs` 提供 `zmq_frontend_addr`、`zmq_tokenizer_addr` 等地址，决定前端连到哪条 ZMQ 通道。 |
| [python/minisgl/message/frontend.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/frontend.py) | `UserReply`（增量回复）的定义，是前端从 detokenizer 收到的消息。 |
| [python/minisgl/message/tokenizer.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/tokenizer.py) | `TokenizeMsg`（送进 tokenizer 的请求）与 `AbortMsg`（中止信号）的定义。 |
| [python/minisgl/utils/mp.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/mp.py) | `ZmqAsyncPushQueue` / `ZmqAsyncPullQueue`，前端用来收发消息的异步 ZMQ 队列。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**FastAPI 路由** → **FrontendManager 的 uid/Event 配对** → **流式与非流式两条返回路径** → **客户端断连中止**。顺序就是一次请求真正走过的路径。

### 4.1 FastAPI 路由与请求模型

#### 4.1.1 概念说明

用户的请求首先撞上的是 FastAPI 路由层。这一层只做两件事：

1. **协议适配**：把外界五花八门的请求格式（OpenAI chat、OpenAI completion、裸 `/generate`）统一翻译成内部唯一一种「送进 tokenizer」的消息 `TokenizeMsg`。
2. **响应包装**：把内部异步产出的增量文本包装成 OpenAI 兼容的 JSON 或 SSE 流。

也就是说，路由层**本身不做任何模型推理**，它只是个翻译官 + 包装工。真正的推理在 scheduler 子进程里。理解这一点，就能回答实践任务里「`/health` 为什么不必经过 tokenizer/scheduler」——因为它根本不需要翻译任何推理请求。

#### 4.1.2 核心流程

一个 HTTP 请求在前端的处理骨架是固定的：

```
收到请求(某个 pydantic 模型)
   │
   ├─ state = get_global_state()          # 取全局唯一的 FrontendManager
   ├─ uid = state.new_user()              # 申请一个新身份 + 配对用的 Event
   ├─ 组装 TokenizeMsg(uid, text, sampling_params)
   ├─ await state.send_one(msg)           # 经 ZMQ 送给 tokenizer
   │
   └─ 选择返回路径：
        ├─ 流式  → StreamingResponse(stream_with_cancellation(...))
        └─ 非流式 → 等所有 ack 拼成 full_content，返回单个 JSON
```

#### 4.1.3 源码精读

先看请求模型。`OpenAICompletionRequest` 用一个类同时兼容「chat（带 messages）」和「completion（带 prompt）」两种用法，并集中收纳采样参数：

请求模型与消息定义 —— [python/minisgl/server/api_server.py:59-83](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L59-L83)

```python
class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

class OpenAICompletionRequest(BaseModel):
    model: str
    prompt: str | None = None
    messages: List[Message] | None = None
    max_tokens: int = 16
    temperature: float = 1.0
    top_k: int = -1
    top_p: float = 1.0
    n: int = 1
    stream: bool = False
    ...
```

> 说明：`messages` 和 `prompt` 二选一；`stream` 决定走哪条返回路径。注意 `n` 字段虽然声明了，但当前实现固定按 `n=1` 处理。

核心路由 `/v1/chat/completions` —— [python/minisgl/server/api_server.py:255-310](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L255-L310)

```python
@app.post("/v1/chat/completions")
async def v1_completions(req: OpenAICompletionRequest, request: Request):
    state = get_global_state()
    if req.messages:
        prompt = [msg.model_dump() for msg in req.messages]
    else:
        assert req.prompt is not None, "Either 'messages' or 'prompt' must be provided"
        prompt = req.prompt

    uid = state.new_user()
    await state.send_one(
        TokenizeMsg(uid=uid, text=prompt,
                    sampling_params=SamplingParams(ignore_eos=..., max_tokens=..., ...))
    )

    if req.stream:
        return StreamingResponse(
            state.stream_with_cancellation(state.stream_chat_completions(uid), request, uid),
            media_type="text/event-stream",
        )

    # Non-streaming: 收集全部 chunk，返回单个 JSON
    full_content = ""
    async for ack in state.wait_for_ack(uid):
        full_content += ack.incremental_output
        if ack.finished:
            break
    return { ... "object": "chat.completion", "choices": [...] ... }
```

> 说明：注意它把 FastAPI 注入的 `request: Request` 一路传下去——这是后续断连检测的关键（见 4.4）。`messages` 被 `model_dump()` 成普通 dict 列表后塞进 `TokenizeMsg.text`，tokenizer 那边再用 chat 模板渲染成 prompt（详见 u3-l2）。

其余两条路由更轻量，体现「翻译官」的边界：

- `/generate`（裸文本生成，SSE 流）—— [python/minisgl/server/api_server.py:228-247](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L228-L247)
- `/v1/models`（返回模型卡片，**完全不触发推理**）—— [python/minisgl/server/api_server.py:313-316](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L313-L316)

```python
@app.get("/v1/models")
async def available_models():
    state = get_global_state()
    return ModelList(data=[ModelCard(id=state.config.model_path, root=state.config.model_path)])
```

> 说明：它只是把 `config.model_path` 包成一个 `ModelCard` 返回，连 `new_user()` 都没调用，自然不进流水线。`/health` 实践就是仿照它写一个「只读本地状态、直接返回」的路由。

#### 4.1.4 代码实践

**实践目标**：亲手给前端加一个 `/health` 健康检查端点，验证「不进流水线」的路由长什么样。

**操作步骤**（这是示例代码，项目原本没有此端点）：

```python
# 在 api_server.py 里 /v1/models 路由附近新增
@app.get("/health")
async def health():
    return {"status": "ok"}
```

**需要观察的现象**：用 `curl http://127.0.0.1:1919/health` 应立即返回 `{"status":"ok"}`，且服务端日志**不会**出现「Received generate request」之类的推理记录。

**预期结果**：响应延迟在毫秒级（一次本地 HTTP 往返），与模型大小、是否加载完权重无关。

**为什么不必经过 tokenizer/scheduler**：健康检查只关心「前端进程活着」，它既不需要 `uid`（不追踪某次具体生成），也不调用 `send_one()` 把 `TokenizeMsg` 推进 ZMQ 环。tokenizer/scheduler 是为「把文本变成 token、再算出 token」而存在的；`/health` 没有任何文本要算，因此可以直接返回。这与 `/v1/models` 同理。

> 待本地验证：在真实启动的服务上 `curl` 该端点并记录耗时。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `/v1/chat/completions` 路由函数删掉 `await state.send_one(...)` 这一行，会发生什么？
**答案**：请求会被登记一个 `uid`（`new_user()` 已建好 `ack_map`/`event_map`），但没有任何 `TokenizeMsg` 送进 tokenizer，scheduler 永远不会产生对应的 `UserReply`。于是流式路径会永远 `await event.wait()` 卡住，非流式路径也会卡在 `wait_for_ack` 循环里，直到客户端超时。

**练习 2**：`/v1/models` 返回的 `id` 是从哪里来的？为什么它和「实际能推理的模型」可能不一致？
**答案**：来自 `state.config.model_path`（用户启动时传的 `--model-path`）。它只是个字符串标识，框架并不校验该路径对应的权重是否已成功加载或是否可推理——这是个「配置即真相」的朴素实现。

---

### 4.2 FrontendManager：uid + asyncio.Event 关联请求与增量回复

#### 4.2.1 概念说明

这是本讲最核心的设计。难点在于：**生成是异步、分批、增量到达的**。一个请求要生成几百个 token，scheduler 每个 decode 步骤只算出一个，detokenizer 把它解码成一段文字后，会以一条独立的 `UserReply(uid, incremental_output, finished)` 消息推回前端。这些消息：

- **乱序风险**：多个用户的回复会交错到达同一个 ZMQ 拉取队列。
- **多片拼装**：一次请求对应**多条**回复，最后一条 `finished=True`。

`FrontendManager` 用两把钥匙解决：用 `uid` 把回复**路由**回正确的请求，用 `asyncio.Event` 把「回复到达」**通知**给正在等待的那条协程。

#### 4.2.2 核心流程

`FrontendManager` 维护两张以 `uid` 为键的表：

```
ack_map : Dict[int, List[UserReply]]   # 收件箱：uid -> 还没被消费的增量回复
event_map: Dict[int, asyncio.Event]    # 门铃：  uid -> 是否有新回复到达
```

配对的三方协作时序如下：

```
路由协程(生产请求)            listen 协程(消费回复)          等待协程(wait_for_ack)
─────────────────────         ──────────────────            ──────────────────────
uid = new_user()
  建空 ack_map[uid]
  建门铃 event_map[uid]
send_one(TokenizeMsg)
                              ......(经过 tokenizer/scheduler/detokenizer)
                              recv 一条 UserReply
                              ack_map[uid].append(reply)     await event.wait()  # 阻塞
                              event_map[uid].set()  ────────── 被唤醒
                                                             event.clear()
                                                             取走并清空 ack_map[uid]
                                                             yield 每一条 reply
                                                             若 finished 则结束
```

关键直觉：**`Event` 只是「唤醒信号」，`ack_map` 的列表才是真正的缓冲区**。哪怕多条回复在协程醒来之前就堆进列表，下一次唤醒会一次性全部取走（批量 drain），不会丢失。

#### 4.2.3 源码精读

数据结构与三张表 —— [python/minisgl/server/api_server.py:99-114](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L99-L114)

```python
@dataclass
class FrontendManager:
    config: ServerArgs
    send_tokenizer: ZmqAsyncPushQueue[BaseTokenizerMsg]
    recv_tokenizer: ZmqAsyncPullQueue[BaseFrontendMsg]
    uid_counter: int = 0
    initialized: bool = False
    ack_map: Dict[int, List[UserReply]] = field(default_factory=dict)
    event_map: Dict[int, asyncio.Event] = field(default_factory=dict)

    def new_user(self) -> int:
        uid = self.uid_counter
        self.uid_counter += 1
        self.ack_map[uid] = []
        self.event_map[uid] = asyncio.Event()
        return uid
```

> 说明：`new_user()` 是「请求登记处」——发号 + 建空收件箱 + 装门铃，三者原子完成，保证后续 `listen` 和 `wait_for_ack` 都能查到这个 uid。

后台监听循环：把回复按 uid 分发 —— [python/minisgl/server/api_server.py:116-132](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L116-L132)

```python
async def listen(self):
    while True:
        msg = await self.recv_tokenizer.get()
        for msg in _unwrap_msg(msg):
            if msg.uid not in self.ack_map:
                continue            # 已被中止/清理的 uid，直接丢弃
            self.ack_map[msg.uid].append(msg)
            self.event_map[msg.uid].set()

def _create_listener_once(self):
    if not self.initialized:
        asyncio.create_task(self.listen())
        self.initialized = True

async def send_one(self, msg: BaseTokenizerMsg):
    self._create_listener_once()
    await self.send_tokenizer.put(msg)
```

> 说明：`listen` 是整个前端的「邮差」——死循环从 ZMQ 拉消息，按 `uid` 分拣进收件箱并按响门铃。注意先 `append` 再 `set` 的顺序：保证等待者醒来时数据一定已在列表里。`if msg.uid not in self.ack_map: continue` 这一行很关键——它吞掉「迟到」的回复（比如请求已被 abort 清理后才到的残留 token），防止 KeyError。
>
> `_unwrap_msg` 负责把单条 `UserReply` 和批量 `BatchFrontendMsg` 统一成列表，见 [api_server.py:42-50](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L42-L50)。`listen` 采用**懒启动**：直到第一次 `send_one` 才被 `create_task` 拉起，避免在 uvicorn 多 worker 等场景下空转。

等待端：消费收件箱直到 finished —— [python/minisgl/server/api_server.py:134-150](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L134-L150)

```python
async def wait_for_ack(self, uid: int):
    event = self.event_map[uid]
    while True:
        await event.wait()
        event.clear()
        pending = self.ack_map[uid]
        self.ack_map[uid] = []          # 一次性取走并清空收件箱
        ack = None
        for ack in pending:
            yield ack
        if ack and ack.finished:
            break
    del self.ack_map[uid]
    del self.event_map[uid]
```

> 说明：这是本讲的「微缩状态机」。每轮：等门铃 → 复位 → 把当前所有回复作为一批 yield 出去 → 看最后一条是否 `finished` 决定是否收尾。结束最后 `del` 掉两张表里的条目，防止内存随请求数无限增长。这里用「先 clear 再 drain」而非「先 drain 再 clear」是有意为之——和 `listen` 的「先 append 再 set」配合，确保不会丢消息（见练习）。

#### 4.2.4 代码实践

**实践目标**：用源码阅读法验证「Event 只是信号、列表才是缓冲」这条不变量。

**操作步骤**：

1. 打开 [api_server.py:116-150](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L116-L150)。
2. 在脑中模拟：假设 `listen` 在 `wait_for_ack` 醒来之前，连续收到同一个 uid 的 3 条 `UserReply`（前两条 `finished=False`，第三条 `finished=True`），且这期间 `wait_for_ack` 一直没被调度。
3. 推演 `ack_map[uid]`、`event_map[uid]` 的状态变化。

**需要观察的现象（在代码逻辑层面）**：3 条都进了 `ack_map[uid]` 列表，`event` 被连续 `set()`（幂等，仍是「已响」状态）。

**预期结果**：`wait_for_ack` 一旦被调度，一次 `await event.wait()` 立即返回，随后一次 drain 把 3 条全部 yield，并在第 3 条 `finished` 后跳出循环。即「多次 set 合并成一次唤醒、但一条回复都不丢」。

**如果无法运行**：这是源码阅读型实践，无需 GPU；可直接对照代码逐步推演，标注为「待本地验证」的是真实运行时的调度顺序。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `listen` 里的两行顺序对调成「先 `set()` 再 `append`」，会引入什么 bug？
**答案**：存在丢消息的竞态。若 `wait_for_ack` 恰好在 `set()` 之后、`append()` 之前被调度唤醒，它会 `clear()` 复位门铃、drain 一个**还不含本条回复**的列表，然后继续 `await event.wait()`——而此时门铃已被清掉，本条回复既没进这批、也没有新的唤醒信号，最终丢失。正确顺序必须是「先 append 再 set」。

**练习 2**：`uid_counter` 是进程内自增整数。前端重启后 uid 从 0 重新开始，这会出问题吗？
**答案**：不会。uid 只在**单次前端进程的生命周期内**用于配对请求与回复，所有相关消息（`TokenizeMsg`/`UserReply`/`AbortMsg`）都只在当前运行的进程组之间流转。重启后是新的一组进程，旧的 uid 不会有残留消息来混淆。

---

### 4.3 流式 SSE 与非流式聚合两条返回路径

#### 4.3.1 概念说明

同一个 `/v1/chat/completions` 路由，根据 `stream` 字段走两条完全不同的返回路径：

- **流式（SSE）**：每收到一条增量 `UserReply`，立刻包成一个 OpenAI chunk 推给客户端，让用户看到「打字机」效果。延迟低，但响应是多次小包。
- **非流式**：等服务端把所有 token 都算完、拼成完整字符串后，再返回**一个**大 JSON。首字延迟高，但客户端只收一次。

两条路径的「数据来源」是同一个 `wait_for_ack(uid)`，区别只在**如何消费**它吐出的 ack。

#### 4.3.2 核心流程

```
wait_for_ack(uid)  ──yield 一串 UserReply──┐
                                           ├──> 流式：每个 ack → 一个 SSE chunk，边到边发
                                           └──> 非流式：把 incremental_output 累加进 full_content，
                                                         最后一次性返回 chat.completion JSON
```

两种 SSE 帧格式略有不同（注意换行个数）：

- `/generate` 的 `stream_generate`：`data: <文本>\n`（单换行），结尾 `data: [DONE]\n`。
- `/v1/chat/completions` 的 `stream_chat_completions`：标准 SSE `data: {json}\n\n`（双换行），首个 chunk 带 `role`，末尾追加 `finish_reason: "stop"` 与 `data: [DONE]`。

#### 4.3.3 源码精读

`/generate` 的裸流式 —— [python/minisgl/server/api_server.py:152-158](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L152-L158)

```python
async def stream_generate(self, uid: int):
    async for ack in self.wait_for_ack(uid):
        yield f"data: {ack.incremental_output}\n".encode()
        if ack.finished:
            break
    yield "data: [DONE]\n".encode()
```

> 说明：注意 finished 的那条 ack 的 `incremental_output` 也会被先 yield 出去，再 break，再补 `[DONE]`。`/generate` 和交互式 shell（见 4.3.5 末尾）共用这条函数。

OpenAI chat 流式 —— [python/minisgl/server/api_server.py:160-188](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L160-L188)

```python
async def stream_chat_completions(self, uid: int):
    first_chunk = True
    async for ack in self.wait_for_ack(uid):
        delta = {}
        if first_chunk:
            delta["role"] = "assistant"      # 仅首个 chunk 带 role
            first_chunk = False
        if ack.incremental_output:
            delta["content"] = ack.incremental_output
        chunk = {
            "id": f"cmpl-{uid}",
            "object": "text_completion.chunk",
            "choices": [{"delta": delta, "index": 0, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        if ack.finished:
            break
    # 收尾：补一个 finish_reason="stop" 的 chunk
    end_chunk = {... "finish_reason": "stop" ...}
    yield f"data: {json.dumps(end_chunk)}\n\n".encode()
    yield b"data: [DONE]\n\n"
```

> 说明：符合 OpenAI 惯例——首个 chunk 的 `delta` 里放 `role`，后续 chunk 的 `delta` 里放 `content` 片段，最后单独发一个 `finish_reason="stop"` 的收尾 chunk 与 `[DONE]` 哨兵。`if ack.incremental_output:` 跳过空片段，避免发无意义的空 content。

非流式聚合 —— [python/minisgl/server/api_server.py:286-310](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L286-L310)

```python
full_content = ""
async for ack in state.wait_for_ack(uid):
    full_content += ack.incremental_output
    if ack.finished:
        break
return {
    "id": f"chatcmpl-{uid}",
    "object": "chat.completion",
    "created": int(time.time()),
    "model": req.model,
    "choices": [{"index": 0,
                 "message": {"role": "assistant", "content": full_content},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
}
```

> 说明：非流式没有 `StreamingResponse`，直接 `return` 一个普通 dict，FastAPI 会把它序列化成单次 JSON 响应。注意 `usage` 里 token 计数固定为 0——这是个已知简化，前端并不掌握真实的 token 计数（那需要 tokenizer/scheduler 回传，当前协议没带）。

#### 4.3.4 代码实践

**实践目标**：亲手对比两条路径的 HTTP 表现，直观感受「首字延迟 vs 总延迟」。

**操作步骤**：

1. 启动服务（参照 u1-l2，例如 `python -m minisgl --model-path Qwen/Qwen3-0.6B --port 1919`）。
2. **非流式**：
   ```bash
   curl http://127.0.0.1:1919/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -d '{"model":"x","messages":[{"role":"user","content":"讲个长一点的故事"}],"max_tokens":128,"stream":false}'
   ```
3. **流式**：把上面 JSON 里的 `"stream":false` 改成 `"stream":true`，加 `-N`（禁用 curl 缓冲）：
   ```bash
   curl -N http://127.0.0.1:1919/v1/chat/completions -H 'Content-Type: application/json' \
     -d '{"model":"x","messages":[{"role":"user","content":"讲个长一点的故事"}],"max_tokens":128,"stream":true}'
   ```

**需要观察的现象**：非流式要等整段生成结束才一次性输出 JSON；流式会逐块刷出 `data: {...}` 行，文字像被实时打出来。

**预期结果**：流式的「首字时间」明显小于非流式的「总时间」；但两者「总时间」接近（同样多的 token 要算）。流式输出末尾应出现 `finish_reason":"stop"` 与 `data: [DONE]`。

> 待本地验证：在没有 GPU 的环境无法真实启动，可改为阅读 `stream_chat_completions` 的代码，逐行标注每个 chunk 的 `delta` 内容来模拟输出序列。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `/generate` 用 `data: <文本>\n`（单换行），而 `/v1/chat/completions` 用 `data: {json}\n\n`（双换行）？
**答案**：标准 SSE 规定一个事件以「空行」（两个换行）作为分隔。`/v1/chat/completions` 严格遵循 SSE，便于通用 SSE 客户端解析；`/generate` 是个更朴素的「逐行推文本」约定（交互式 shell 直接按行拼接，见 `shell()` 里对 `data: ...\n` 的解析 [api_server.py:384-393](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L384-L393)），不追求通用 SSE 兼容。

**练习 2**：非流式路径里，若客户端在生成中途断开连接，会发生什么？
**答案**：当前非流式分支**没有**包 `stream_with_cancellation`，也没有检查 `is_disconnected()`，所以服务端会继续把整段生成算完、拼出 `full_content` 才返回——只是返回时客户端已经走了，这次计算白做。要避免这种浪费，应像流式分支那样接入断连检测（见 4.4）。这是一个值得改进的点。

---

### 4.4 客户端断连中止与 AbortMsg

#### 4.4.1 概念说明

LLM 生成很贵。如果用户中途关掉页面或取消请求，服务端理应尽快**停止为这个请求继续算 token**，否则 scheduler 会一直 decode 到 `max_tokens`，白白烧 GPU。

Mini-SGLang 的做法：在流式输出链路里包一层 `stream_with_cancellation`，**每吐一个 chunk 前都探一下客户端是否还在**；一旦发现断连，就抛 `CancelledError` 终止输出，并异步发出一条 `AbortMsg(uid)`。这条消息经 tokenizer 一路传到 scheduler，让它把这个 uid 的请求从 running 队列里剔除。

#### 4.4.2 核心流程

```
stream_with_cancellation(inner_generator, request, uid):
    for chunk in inner_generator:
        if request.is_disconnected():     # 客户端走了？
            raise CancelledError          # 立刻停掉流
        yield chunk
    ── 捕获 CancelledError ──> create_task(abort_user(uid))   # 异步善后
                                 ├─ sleep 0.1s（让在途回复先到）
                                 ├─ 清理 ack_map/event_map[uid]
                                 └─ send_one(AbortMsg(uid))    # 通知 scheduler 停
```

#### 4.4.3 源码精读

断连检测与中止 —— [python/minisgl/server/api_server.py:190-209](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L190-L209)

```python
async def stream_with_cancellation(self, generator, request: Request, uid: int):
    try:
        async for chunk in generator:
            if await request.is_disconnected():       # 每块前探活
                logger.info("Client disconnected for user %s", uid)
                raise asyncio.CancelledError
            yield chunk
    except asyncio.CancelledError:
        asyncio.create_task(self.abort_user(uid))     # 异步触发善后
        raise

async def abort_user(self, uid: int):
    await asyncio.sleep(0.1)            # 稍等，让可能在途的最后几条回复到达
    if uid in self.ack_map:
        del self.ack_map[uid]
    if uid in self.event_map:
        del self.event_map[uid]
    logger.warning("Aborting request for user %s", uid)
    await self.send_one(AbortMsg(uid=uid))   # 通知 tokenizer/scheduler 停止该 uid
```

> 说明：三个细节值得注意。① 检测靠 Starlette/FastAPI 注入的 `request.is_disconnected()`，所以路由必须把 `request` 一路传进来（4.1 已见）。② `abort_user` 用 `create_task` **异步**执行，`stream_with_cancellation` 自己立刻 `raise` 把当前流尽快结束，不等善后完成。③ `await asyncio.sleep(0.1)` 是个让步——给可能在 ZMQ 管道里「在飞」的最后几条 `UserReply` 一个到达的机会，随后再删表，避免 `listen` 里对已删 uid 的回复报错（配合 4.2 里 `if msg.uid not in self.ack_map: continue` 的兜底）。

`AbortMsg` 的定义 —— [python/minisgl/message/tokenizer.py:41-43](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/tokenizer.py#L41-L43)

```python
@dataclass
class AbortMsg(BaseTokenizerMsg):
    uid: int
```

> 说明：`AbortMsg` 是个极简消息——只带 `uid`。它和 `TokenizeMsg` 一样继承自 `BaseTokenizerMsg`，所以走同一条「前端 → tokenizer」的 ZMQ 通道（`send_tokenizer` 队列）。tokenizer 收到后会把它转发给 scheduler，scheduler 据此把对应请求从 decode 队列移除（具体处理见 u4 调度器讲义）。

接线点：流式路由才会包这一层 —— [python/minisgl/server/api_server.py:280-284](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L280-L284)

```python
if req.stream:
    return StreamingResponse(
        state.stream_with_cancellation(state.stream_chat_completions(uid), request, uid),
        media_type="text/event-stream",
    )
```

> 说明：注意嵌套顺序——最外层是 `stream_with_cancellation`，它包裹 `stream_chat_completions`（真正的 SSE 生成器）。`request` 与 `uid` 同时传入，正是为了「边发边探活」。

#### 4.4.4 代码实践

**实践目标**：观察断连中止的真实效果（或通过源码推演它为何能省算力）。

**操作步骤**：

1. 启动服务，发起一个会生成较长的流式请求，但**不等它结束**就主动断开：
   ```bash
   # 后台发起，2 秒后 kill 客户端
   ( curl -N http://127.0.0.1:1919/v1/chat/completions \
       -H 'Content-Type: application/json' \
       -d '{"model":"x","messages":[{"role":"user","content":"写一篇很长的文章"}],"max_tokens":1024,"stream":true}' \
       > /tmp/stream.out 2>&1 & ) ; sleep 2 ; pkill -f 'curl -N'
   ```
2. 观察服务端日志。

**需要观察的现象**：服务端日志应出现 `Client disconnected for user <uid>` 与 `Aborting request for user <uid>`，且之后**不再**为该请求继续打印 decode 相关输出。

**预期结果**：相比让请求跑到 `max_tokens=1024`，断连后 scheduler 收到 `AbortMsg` 并提前停止，GPU 不再为这个已离开的客户端付费。

> 待本地验证：断连是否被及时探测，取决于 Starlette `is_disconnected()` 的轮询频率与网络环境；在无 GPU 环境可改为阅读 `abort_user` 与 scheduler 端对 `AbortMsg` 的处理来推演链路。

#### 4.4.5 小练习与答案

**练习 1**：`abort_user` 里的 `await asyncio.sleep(0.1)` 去掉行不行？
**答案**：功能上多数时候仍能工作，因为 `listen` 里有 `if msg.uid not in self.ack_map: continue` 兜底。但去掉后，若 detokenizer 在断连瞬间正好发出最后几条 `UserReply`，它们到达时 `ack_map[uid]` 已被删除，会被静默丢弃——虽不报错，但属于「竞态依赖兜底」的脆弱写法。保留 sleep 是一种显式的让步，让在途回复有机会先落进列表再被清理，逻辑更稳健。

**练习 2**：`stream_with_cancellation` 为什么用 `asyncio.create_task(self.abort_user(uid))` 而不是 `await self.abort_user(uid)`？
**答案**：因为外层 `except` 块最后 `raise` 重新抛出了 `CancelledError`，目的是让当前流式协程**尽快**终止（把控制权交还给 `StreamingResponse` 的清理逻辑）。若改成 `await`，会先阻塞等待 `abort_user`（含 0.1s sleep + ZMQ 发送）完成，反而拖延了终止；用 `create_task` 把善后挂到后台，当前协程立即结束，两者解耦。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「前端可观测性」小任务：

**任务**：给 `FrontendManager` 加一个**轻量统计**能力，并暴露成一个**不进流水线**的 HTTP 端点 `/stats`，体现你对「路由层 vs 推理流水线」边界的理解。

**建议步骤**（以下为示例代码，项目原本没有）：

1. 在 `FrontendManager` 里加两个计数器字段，并在 `new_user` / `abort_user` 里维护：
   ```python
   # 示例代码
   total_requests: int = 0
   aborted_requests: int = 0

   def new_user(self) -> int:
       self.total_requests += 1
       ...  # 其余不变

   async def abort_user(self, uid: int):
       self.aborted_requests += 1
       ...  # 其余不变
   ```
2. 新增路由：
   ```python
   # 示例代码
   @app.get("/stats")
   async def stats():
       s = get_global_state()
       return {"total_requests": s.total_requests,
               "aborted_requests": s.aborted_requests}
   ```
3. 跑几个请求（含一个中途断连的流式请求），再 `curl http://127.0.0.1:1919/stats`。

**验收点（串联本讲知识）**：

- `/stats` 像练习里的 `/health` 一样**不调用** `new_user`/`send_one`，因此不进 tokenizer/scheduler——它只读前端进程自己的内存计数器。（对应 4.1）
- 计数发生在 `new_user`（4.2 的「请求登记处」）和 `abort_user`（4.4 的断连善后），说明你准确找到了「请求生命周期」的两个关键事件点。
- 你应该能解释：为什么 `total_requests` 只增不减、而 `ack_map` 的大小会随请求完成而回落（因为 `wait_for_ack` 与 `abort_user` 都会 `del` 掉对应条目，见 4.2/4.4）。

> 待本地验证：在有 GPU 的环境真实跑通并核对计数；无 GPU 环境可通过阅读 `new_user`/`wait_for_ack`/`abort_user` 三处，论证计数何时增减。

## 6. 本讲小结

- `api_server.py` 是 FastAPI 前端，充当**协议翻译官**：把 OpenAI/chat/`/generate` 请求统一翻成 `TokenizeMsg`，把内部增量 `UserReply` 包回 OpenAI 格式；路由层本身不做推理。
- `FrontendManager` 用 `uid` + 两张表（`ack_map` 收件箱、`event_map` 门铃）解决「异步、分批、多用户交错到达」的回复配对难题；`listen` 是按 uid 分发的邮差，`wait_for_ack` 是批量 drain 的消费者。
- `Event` 只是唤醒信号、列表才是缓冲：`listen`「先 append 再 set」、`wait_for_ack`「先 clear 再 drain」配合，保证多次 set 合并一次唤醒但一条回复都不丢。
- 两条返回路径共享 `wait_for_ack`：流式用 `stream_chat_completions`/`stream_generate` 边到边发 SSE，非流式用 `full_content` 累加后返回单个 JSON（注意 `usage` 固定为 0）。
- 客户端断连时，流式路径经 `stream_with_cancellation` 探活 → 抛 `CancelledError` → 异步 `abort_user` 发 `AbortMsg(uid)` 通知 scheduler 停算；非流式路径当前**未**接入断连检测。
- `run_api_server` 是前端启动入口：建好 `FrontendManager`（接两条 ZMQ 通道）→ `start_backend()` 拉起子进程 → `uvicorn.run(app, ...)`，或 shell 模式下 `asyncio.run(shell())`。

## 7. 下一步学习建议

本讲只讲了「前端发出 `TokenizeMsg`、收回 `UserReply`」的两端，中间那一站——**tokenizer/detokenizer 进程怎么把文本变 token、把 token 变文本、并路由消息**——是下一讲 **u3-l2（Tokenizer / Detokenizer Worker）** 的内容，建议紧接着读。

横向延伸：

- 想理解 `AbortMsg` 被 scheduler 收到后**到底怎么停止 decode**，请读 **u4-l1（Scheduler 主循环）** 与 **u4-l4（Decode 调度）**。
- 想搞清 `ack_map`/`event_map` 这种「uid 配对」之外，多 rank 之间如何用 ZMQ pub/sub + broadcast 同步消息，请读 **u4-l2（Scheduler I/O 与多 rank 广播）**。
- 想看「不进流水线、纯本地路由」的更多例子，可重读本讲 `/v1/models` 与 `/v1` 根路由，并对照 `run_api_server` 的接线方式。

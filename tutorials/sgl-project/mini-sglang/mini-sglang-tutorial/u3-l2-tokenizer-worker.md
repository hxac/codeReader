# Tokenizer / Detokenizer Worker

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `tokenize_worker` 这个进程**为什么只用一个函数就能同时做 tokenize（文本转 token）和 detokenize（token 转文本）**这两件方向相反的事。
- 描述 `tokenize_worker` 的事件循环：它如何收消息、如何用 `local_bs` 做批处理、如何按消息类型把消息分流到 `TokenizeManager` 或 `DetokenizeManager`。
- 区分两条消息转换链路：
  - 入向：`TokenizeMsg` → tokenize → `UserMsg`（送给 backend / scheduler）。
  - 出向：`DetokenizeMsg` → detokenize → `UserReply`（回给 frontend / api_server）。
- 理解 `num_tokenizer` 配置如何决定「共享一个 worker」还是「拆成多个独立 tokenizer + 一个 detokenizer」，以及对应的 ZMQ `bind`/`connect` 拓扑。

本讲是 [u3-l1 API Server](u3-l1-api-server.md) 的下游：API Server 把用户文本打包成 `TokenizeMsg` 推出去之后，接住它的就是这个 worker；它产出的 `UserReply` 也是 API Server 流式返回给用户的最终内容。本讲依赖 [u2-l3 进程间消息与序列化](u2-l3-message-serialization.md) 里讲过的消息族与序列化机制。

## 2. 前置知识

在进入源码前，先用大白话建立三点直觉。

**第一，为什么需要单独的 tokenizer / detokenizer 进程？**

大模型内部只认数字（token id），不认文字。于是请求进系统时要先把用户文字切成一串数字（**tokenize**），模型算完输出的也是一串数字，要再拼回人类可读的文字（**detokenize**）。这两件事都用 HuggingFace 的 `transformers` 库做，属于 **CPU 密集**而非 GPU 工作。Mini-SGLang 把它们单独放到 CPU 进程里，让 GPU 进程（Scheduler/Engine）专心做张量计算，互不阻塞。这与 [u1-l4 进程架构](u1-l4-process-architecture.md) 讲的「CPU 与 GPU 工作分离」一脉相承。

**第二，什么是「流式 detokenize」？**

模型生成时是一个 token 一个 token 吐出来的（decode 阶段每轮产出 1 个 token）。如果每来一个 token 就把「到目前为止的所有 token」整体 decode 一次再发给用户，会有两个问题：一是重复 decode 浪费算力；二是会出现「半个字」。很多 tokenizer（尤其是中文、emoji）一个字由多个 token 组成，单独 decode 某些中间 token 会得到乱码（替换字符 `�`）。所以 detokenizer 要维护一个**游标状态机**，只把「已经稳定、可安全打印」的那部分文本作为增量发出去。本讲会精读这个状态机。

**第三，消息是怎么在进程间流动的？**

复习 [u2-l3](u2-l3-message-serialization.md)：进程间用 ZMQ 队列传消息，消息是带 `__type__` 标记的 dataclass，靠 `serialize_type`/`deserialize_type` 自动编解码。本讲的 worker 收到的是 `BaseTokenizerMsg` 族的若干消息，发出的是 `BaseBackendMsg` 族（去 scheduler）或 `BaseFrontendMsg` 族（回 api_server）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [python/minisgl/tokenizer/server.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py) | 定义 `tokenize_worker` 进程入口函数：事件循环、批处理、按消息类型分流。是本讲的主角。 |
| [python/minisgl/tokenizer/tokenize.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/tokenize.py) | `TokenizeManager`：把 `TokenizeMsg`（用户文本）转成 `input_ids` 张量。 |
| [python/minisgl/tokenizer/detokenize.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/detokenize.py) | `DetokenizeManager`：把 `DetokenizeMsg`（模型产出的 token）转成增量文本，含流式游标状态机。 |
| [python/minisgl/server/launch.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py) | `launch_server` 用 `mp.Process` 启动 worker 进程，决定共享/独立拓扑。 |
| [python/minisgl/server/args.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py) | `ServerArgs` 提供 `num_tokenizer`、`share_tokenizer` 及各 ZMQ 地址与 `create` 标志。 |
| [python/minisgl/utils/mp.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/mp.py) | `ZmqPushQueue` / `ZmqPullQueue`：底层 ZMQ 收发封装。 |

另外会引用到消息定义文件 [python/minisgl/message/tokenizer.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/tokenizer.py)、[backend.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/backend.py)、[frontend.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/frontend.py)，它们在 u2-l3 已详细讲过。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 `tokenize_worker` 循环**：一个函数两种身份、事件循环与 `local_bs` 批处理。
- **4.2 `TokenizeManager`**：文本 → `input_ids`。
- **4.3 `DetokenizeManager`**：token → 增量文本，流式游标状态机。
- **4.4 消息分流与拓扑配置**：按类型分流的三条支路，以及 `num_tokenizer` 决定的共享/独立 ZMQ 拓扑。

### 4.1 tokenize_worker 循环

#### 4.1.1 概念说明

`tokenize_worker` 是 Mini-SGLang 里一个很巧妙的设计：**同一个函数既能当 tokenizer 用，又能当 detokenizer 用**。它不关心「自己是哪种角色」，只关心「收到的是什么类型的消息」——收到 `TokenizeMsg` 就做 tokenize 并把结果送给 backend，收到 `DetokenizeMsg` 就做 detokenize 并把结果回给 frontend。角色是由「它监听哪个 ZMQ 地址、谁往那个地址推消息」决定的，而不是由代码分支决定的。

这种设计的好处是：代码只有一份，逻辑不重复；拓扑变了（共享一个进程 vs 拆成多个进程），函数不用改。

#### 4.1.2 核心流程

`tokenize_worker` 的执行过程可以概括为「初始化 → 就绪握手 → 永久循环」三段：

```text
初始化阶段
  ├─ 建三条 ZMQ 通道：send_backend、send_frontend、recv_listener
  ├─ load_tokenizer(model_path)  加载 HuggingFace tokenizer
  ├─ new TokenizeManager(tokenizer)   处理入向
  ├─ new DetokenizeManager(tokenizer) 处理出向
  └─ ack_queue.put("...is ready")     告诉主进程「我准备好了」

永久循环（每轮）
  ├─ recv_listener.get()               阻塞拿一批消息（至少 1 条）
  ├─ while 还没凑够 local_bs 且队列非空:  继续攒消息（批处理）
  ├─ 按类型把 pending_msg 分成三堆:
  │     detokenize_msg / tokenize_msg / abort_msg
  ├─ 若有 detokenize_msg → detokenize → UserReply → send_frontend
  ├─ 若有 tokenize_msg    → tokenize   → UserMsg   → send_backend
  └─ 若有 abort_msg       → AbortBackendMsg        → send_backend
```

关于 `local_bs`：它是「每轮至少攒够多少条消息才处理」的阈值。但要注意，[launch.py 里启动 worker 时 `local_bs` 写死为 `1`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L80)，所以 `while len(pending_msg) < local_bs` 这一条件第一步就不成立——**当前实现实际上是来一条处理一条，并不真正批处理**。`tokenize.py` 里也留着 `# TODO: batch tokenization` 注释，说明批量 tokenize 是预留的优化点。

关于就绪握手：worker 在进入循环前会往 `ack_queue` 放一条就绪消息，主进程 `launch_server` 会阻塞等待收齐 `num_tokenizers + 2` 条（详见 4.4），保证所有 worker 都就绪后才开始接请求。这点在 [u1-l2](u1-l2-install-and-run.md) 已提过。

#### 4.1.3 源码精读

函数签名与初始化。注意三处细节：被 `@torch.inference_mode()` 装饰（tokenizer 虽不跑模型，但关掉 autograd 引擎更省内存）；`send_backend`/`send_frontend` 都是 `create=False`（worker 只 connect 不 bind）；`recv_listener` 的 `create` 由参数 `create` 决定（共享模式下由 worker 自己 bind）。

[python/minisgl/tokenizer/server.py:30-54](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py#L30-L54) —— `tokenize_worker` 的签名与初始化：建通道、加载 tokenizer、构造两个 manager。

```python
@torch.inference_mode()
def tokenize_worker(
    *,
    tokenizer_path: str,
    addr: str,
    create: bool,
    backend_addr: str,
    frontend_addr: str,
    local_bs: int,
    tokenizer_id: int = -1,
    ...
) -> None:
    send_backend = ZmqPushQueue(backend_addr, create=False, encoder=BaseBackendMsg.encoder)
    send_frontend = ZmqPushQueue(frontend_addr, create=False, encoder=BaseFrontendMsg.encoder)
    recv_listener = ZmqPullQueue(addr, create=create, decoder=BatchTokenizerMsg.decoder)
    ...
    tokenizer = load_tokenizer(tokenizer_path)
    tokenize_manager = TokenizeManager(tokenizer)
    detokenize_manager = DetokenizeManager(tokenizer)
```

主循环：先阻塞 `get()` 拿第一批，再尝试用 `empty()`（非阻塞轮询）继续攒到 `local_bs`。

[python/minisgl/tokenizer/server.py:59-70](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py#L59-L70) —— 收消息、攒批、按类型分三堆。最后那行 `assert` 是个不变量检查：每条消息必须属于三类之一，不能有「漏网之鱼」。

```python
while True:
    pending_msg = _unwrap_msg(recv_listener.get())
    while len(pending_msg) < local_bs and not recv_listener.empty():
        pending_msg.extend(_unwrap_msg(recv_listener.get()))
    ...
    detokenize_msg = [m for m in pending_msg if isinstance(m, DetokenizeMsg)]
    tokenize_msg = [m for m in pending_msg if isinstance(m, TokenizeMsg)]
    abort_msg = [m for m in pending_msg if isinstance(m, AbortMsg)]
    assert len(detokenize_msg) + len(tokenize_msg) + len(abort_msg) == len(pending_msg)
```

`_unwrap_msg` 是个小工具：因为消息可能被打包成 `BatchTokenizerMsg`（一个信封装多条），也可能裸着来一条，这里统一拆成列表。

[python/minisgl/tokenizer/server.py:24-27](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py#L24-L27) —— 把 `BatchTokenizerMsg` 拆平，普通消息包一层列表。

```python
def _unwrap_msg(msg: BaseTokenizerMsg) -> List[BaseTokenizerMsg]:
    if isinstance(msg, BatchTokenizerMsg):
        return msg.data
    return [msg]
```

#### 4.1.4 代码实践

**实践目标**：亲眼看到「同一个函数处理两类相反的消息」。

**操作步骤**（源码阅读型 + 可选运行）：

1. 在 [server.py 第 65 行的 `logger.debug` 处](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py#L65)，把它临时改成 `logger.info`，并补一行打印每类消息数量，例如：
   ```python
   logger.info(f"[tokenizer_{tokenizer_id}] recv={len(pending_msg)} "
               f"tok={len(tokenize_msg)} detok={len(detokenize_msg)} abort={len(abort_msg)}")
   ```
   （这是**示例代码**，仅用于观察，勿提交。）
2. 若本机有 GPU 与已下载的模型（如 `Qwen/Qwen3-0.6B`），按 [u1-l2](u1-l2-install-and-run.md) 的方式启动服务，并用 `curl` 发一次 `/v1/chat/completions` 请求。
3. 观察日志。

**需要观察的现象**：

- 默认配置（`num_tokenizer=0`，共享模式）下，**同一个 worker 进程**的日志里会交替出现 `tok>0`（处理用户输入）和 `detok>0`（处理模型输出）两类记录，印证「一函数两身份」。
- 由于 `local_bs=1`，每次 `recv=` 应该是 1（或一个 batch 信封里的条数），看不到真正的跨请求攒批。

**预期结果**：一次请求会在日志里留下「先一条 tok、随后若干条 detok（每个生成 token 一条或一信封）」的轨迹。若无法运行（无 GPU），则改为纯阅读：在脑中走一遍「`TokenizeMsg` 与 `DetokenizeMsg` 都会进入同一个 `pending_msg` 列表」这一事实，并确认 `assert` 不会因混入第三种类型而失败。**待本地验证**（运行部分）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `local_bs` 从 1 调到一个较大的值（比如 8），会带来什么好处和风险？

> **答案**：好处是能把多个 tokenize 请求攒成一批，未来配合 `TokenizeManager` 的批量编码（目前是逐条 `for` 循环 + `# TODO: batch tokenization`）可以摊薄 Python 调用开销。风险是会引入**等待延迟**——`recv_listener.get()` 拿到第一条后，要继续 `empty()` 轮询凑够 8 条才处理，低并发时第一条请求会被拖着白白等待，首 token 延迟变高。所以批处理阈值要在「吞吐」与「延迟」间权衡。

**练习 2**：为什么 `send_backend` 和 `send_frontend` 都硬编码 `create=False`？

> **答案**：因为 backend 地址（`zmq_backend_addr`，由 scheduler 的 `_recv_from_tokenizer` 以 `create=True` 绑定）和 frontend 地址（`zmq_frontend_addr`，由 api_server 的 `recv_tokenizer` 以 `create=True` 绑定）都已有「宿主」在 bind。ZMQ 里一个 PUSH/PULL 对只能有一端 `bind`、另一端 `connect`，worker 作为生产者只需 connect 过去即可。唯一由 worker bind 的是 `recv_listener`，且仅在共享模式下（见 4.4）。

### 4.2 TokenizeManager

#### 4.2.1 概念说明

`TokenizeManager` 负责**入向转换**：把用户给的一段文字（或一段 chat 消息列表）变成模型能吃的 `input_ids`（一维 `int32` 张量）。它的输入是若干 `TokenizeMsg`，输出是与之一一对应的 `input_ids` 列表。

这里有个细节值得注意：用户传进来的可能是「裸字符串」（`/generate` 风格），也可能是「对话消息列表」（`/v1/chat/completions` 风格，形如 `[{"role":"user","content":"你好"}]`）。后者需要先套上模型的 **chat template**（把 role/content 拼成模型训练时见过的那种带特殊标记的文本），再编码。`TokenizeMsg.text` 字段的联合类型 `str | List[Dict[str, str]]` 正好体现这两种输入。

#### 4.2.2 核心流程

```text
对每条 TokenizeMsg：
  ├─ text 是 list？ → apply_chat_template(tokenize=False, add_generation_prompt=True)
  │                   得到拼好模板的纯字符串 prompt
  ├─ text 是 str？  → 直接当作 prompt
  └─ tokenizer.encode(prompt, return_tensors="pt")
        → view(-1).to(torch.int32)   拍平成一维 int32 张量
```

`apply_chat_template(tokenize=False, ...)` 表示「只做文本拼接、不做编码」，因为编码统一交给下一步的 `encode`。`add_generation_prompt=True` 会在末尾补上「助手发言」的开头标记，让模型知道该它输出了。

#### 4.2.3 源码精读

[python/minisgl/tokenizer/tokenize.py:14-31](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/tokenize.py#L14-L31) —— `TokenizeManager.tokenize`：逐条处理，区分 chat 模板与裸字符串，最后统一 encode 成一维 int32。

```python
def tokenize(self, msgs: List[TokenizeMsg]) -> List[torch.Tensor]:
    results: List[torch.Tensor] = []
    # TODO: batch tokenization
    for msg in msgs:
        if isinstance(msg.text, list):
            prompt = self.tokenizer.apply_chat_template(
                msg.text, tokenize=False, add_generation_prompt=True,
            )
            assert isinstance(prompt, str)
        else:
            prompt = msg.text
        input_ids: torch.Tensor = (
            self.tokenizer.encode(prompt, return_tensors="pt")
        )
        results.append(input_ids.view(-1).to(torch.int32))
    return results
```

两个关键点：

- `view(-1).to(torch.int32)`：`encode` 可能返回形状为 `(1, L)` 的二维张量，`view(-1)` 拍平成一维，`to(torch.int32)` 对齐到系统内部统一的 token id 类型（[u2-l1 核心数据结构](u2-l1-core-data-structures.md) 里 `Req` 持有的就是这种一维 id 张量）。
- `# TODO: batch tokenization`：当前是 `for` 循环逐条编码，是预留的批量优化点（与 4.1 的 `local_bs` 配套）。

产出会交给 [server.py 的 tokenize 分支](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py#L87-L101)，逐个 `input_ids` 包成 `UserMsg`，再打成 `BatchBackendMsg` 送给 backend。

#### 4.2.4 代码实践

**实践目标**：理解 `text` 字段的两种形态如何走不同分支，最终汇成同一种 `input_ids`。

**操作步骤**（源码阅读 + 本地小脚本）：

1. 阅读 [api_server.py 里构造 `TokenizeMsg` 的三处调用](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L234)（`/generate` 与 `/v1/chat/completions` 路由），确认哪些传的是 `str`、哪些传的是 `List[Dict]`。
2. 在项目 venv 里跑一个独立小脚本（**示例代码**，不写入项目）：
   ```python
   from transformers import AutoTokenizer
   tok = AutoTokenizer.from_pretrained("Qwen/Qwen3-0.6B")
   msgs = [{"role": "user", "content": "你好"}]
   prompt = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
   print(repr(prompt))                       # 带特殊标记的字符串
   print(tok.encode(prompt, return_tensors="pt").view(-1).to(torch.int32))
   ```

**需要观察的现象**：`apply_chat_template` 产出的 `prompt` 里会出现 `<|im_start|>user\n你好<|im_end|>\n<|im_start|>assistant\n` 这类模型专有标记（不同模型标记不同）。

**预期结果**：无论输入是裸字符串还是消息列表，最终都得到一个一维 int32 张量，可直接塞进 `UserMsg.input_ids`。**待本地验证**（脚本运行需能下载模型）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `apply_chat_template` 要传 `add_generation_prompt=True`？

> **答案**：因为这是「让模型生成回复」的请求。`add_generation_prompt=True` 会在拼好的对话历史末尾追加「助手回合」的起始标记（如 `<|im_start|>assistant\n`），模型看到这个标记才知道「现在轮到我输出了」，从而自然地接续生成。如果是做纯文本补全（continuation）则不需要。

**练习 2**：`view(-1)` 这一步在解决什么问题？

> **答案**：`tokenizer.encode(..., return_tensors="pt")` 通常返回形状 `(1, L)`（带 batch 维）的二维张量，而系统下游（`Req.input_ids`、注意力后端的 `cu_seqlens` 等）期望的是**一维**序列。`view(-1)` 把它拍平成 `(L,)`，去掉多余的 batch 维，使所有请求的 id 序列形状统一。

### 4.3 DetokenizeManager

#### 4.3.1 概念说明

`DetokenizeManager` 负责**出向转换**：把模型一个个吐出来的 token 变回人类可读文字，并且要以**增量**形式输出（只发本次新增的那段文字，而非全量重发）。它是本讲里状态最复杂的一块，因为它要在流式场景下解决「半个字」问题。

核心难点：很多字符（尤其中文、emoji）在 tokenizer 里被切成多个 token。如果模型刚吐出「半個字」对应的 token 就立刻 decode，会得到替换字符 `�`，此时不能把这段发给用户，得**攒着**等后续 token 凑齐了再发。`DetokenizeManager` 用一个 `DecodeStatus` 状态机记录每个请求「已 decode 到哪、已安全发送到哪」。

#### 4.3.2 核心流程

每个请求（按 `uid`）在 `decode_map` 里有一个 `DecodeStatus`，含四个偏移量。一次 `detokenize` 调用（一批 `DetokenizeMsg`）的过程：

```text
第一遍（按 msg 累积 token）：
  对每条 DetokenizeMsg(uid, next_token, finished):
    ├─ 若是新 uid → 建空的 DecodeStatus
    ├─ 除非 (finished 且 next_token==eos)：把 next_token 追加进 decoded_ids
    └─ 切出两段 id 列表：
         read_ids = decoded_ids[surr_offset:]   # 含「未定」尾部，用于探测
         surr_ids = decoded_ids[surr_offset:read_offset]  # 已稳定部分

第二遍（批量 decode + 算增量）：
  read_texts = batch_decode(read_ids)   # decode 含尾部的版本
  surr_texts = batch_decode(surr_ids)   # decode 已稳定版本
  对每条 msg：
    new_text = read_str[len(surr_str):]   # 尾部多 decode 出来的那段
    if new_text 非空 且 不以 "�" 结尾:
        # 这段是安全、完整的 → 提交：更新 decoded_str，推进 surr_offset/read_offset
    else:
        # 可能是半个字 → 用 find_printable_text 截到「最后一个完整词/字」再发
    incremental_output = output_str[sent_offset:]   # 只取本次新增
    推进 sent_offset
    if finished: del decode_map[uid]   # 请求结束，清理状态
```

三个偏移量的关系可以理解为「三个游标，越靠右越保守」：

| 偏移量 | 含义 | 保守程度 |
| --- | --- | --- |
| `read_offset` | 已读取的 token 数（含正在探测的尾部） | 最激进 |
| `surr_offset` | 已确认安全、其 decode 结果可作为对照基准的 token 数 | 中间 |
| `sent_offset` | 已发送给用户的**字符**数（注意是字符串长度，不是 token 数） | 最保守 |

`new_text = read_str[len(surr_str):]` 的巧妙之处：用「含尾部 decode」减去「不含尾部 decode」的差，精确地把「尾部那个 token 带来的新增文字」隔离出来，再判断它完不完整。

#### 4.3.3 源码精读

`DecodeStatus` 是每个请求的流式状态：

[python/minisgl/tokenizer/detokenize.py:54-61](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/detokenize.py#L54-L61) —— 四个偏移量记录「已 decode / 已稳定 / 已发送」的进度。

```python
@dataclass
class DecodeStatus:
    decoded_ids: List[int]
    decoded_str: str
    read_offset: int  # length of read ids
    surr_offset: int  # length of surr ids
    sent_offset: int  # length of sent out string
```

第一遍循环：累积 token、切出 `read_ids` / `surr_ids`。注意那个 `finished and next_token == eos` 的特判——当请求以 eos 收尾时，**不要把 eos 这个 token 放进 `decoded_ids`**，否则 decode 出来会多一个结束符。

[python/minisgl/tokenizer/detokenize.py:71-86](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/detokenize.py#L71-L86) —— 累积 token 并切出待 decode 的两段 id 列表。

```python
for msg in msgs:
    if msg.uid not in self.decode_map:
        self.decode_map[msg.uid] = DecodeStatus(...)
    s = self.decode_map[msg.uid]
    if not (msg.finished and msg.next_token == self.eos_token_id):
        s.decoded_ids.append(msg.next_token)
    read_ids.append(s.decoded_ids[s.surr_offset :])
    surr_ids.append(s.decoded_ids[s.surr_offset : s.read_offset])
```

第二遍：批量 decode、判断是否安全、算增量、推进游标。

[python/minisgl/tokenizer/detokenize.py:88-109](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/detokenize.py#L88-L109) —— 核心增量逻辑：靠 `endswith("�")` 判断是否半个字，安全则提交并推进 `surr/read_offset`，否则用 `find_printable_text` 保守截断。

```python
read_texts = self.tokenizer.batch_decode(read_ids)
surr_texts = self.tokenizer.batch_decode(surr_ids)
incremental_strs: List[str] = []
for msg, read_str, surr_str in zip(msgs, read_texts, surr_texts, strict=True):
    s = self.decode_map[msg.uid]
    new_text = read_str[len(surr_str) :]
    if len(new_text) > 0 and not new_text.endswith("�"):
        output_str = s.decoded_str + new_text
        s.decoded_str = output_str
        s.surr_offset = s.read_offset
        s.read_offset = len(s.decoded_ids)
    else:
        new_text = find_printable_text(new_text)
        output_str = s.decoded_str + new_text
    incremental_output = output_str[s.sent_offset :]
    s.sent_offset = len(output_str)
    incremental_strs.append(incremental_output)
    if msg.finished:
        del self.decode_map[msg.uid]
```

`find_printable_text` 是「半个字」的最后一道防线——当 decode 出来的尾部以 `�` 结尾（可能是多字节字符没凑齐），它按一套启发式规则只保留到「最后一个完整词边界」：遇到换行直接全发、末尾是 CJK 字符就发、倒数第二个是 CJK 就少发一个、否则截到最后一个空格。

[python/minisgl/tokenizer/detokenize.py:35-51](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/detokenize.py#L35-L51) —— `find_printable_text`：避免把不完整单词发给前端的启发式截断。

产出的 `incremental_output` 字符串会回到 [server.py 的 detokenize 分支](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py#L71-L85)，包成 `UserReply(uid, incremental_output, finished)` 回给 frontend，最终成为用户看到的流式文字。

#### 4.3.4 代码实践

**实践目标**：用一个最小输入走通 `DetokenizeManager`，观察「半个字」被截住、凑齐后才发送的现象。

**操作步骤**（源码阅读 + 本地小脚本）：

1. 先阅读上面的两段循环，在纸上为一个**中文 token 化为多 token** 的例子标注 `read_offset`/`surr_offset`/`sent_offset` 的变化。
2. 在项目 venv 里跑下面这个独立脚本（**示例代码**，不写入项目）。它的关键是：把同一段 `decoded_ids` **一个一个**喂给 manager，模拟流式 decode，观察每次返回的增量：
   ```python
   from transformers import AutoTokenizer
   from minisgl.tokenizer.detokenize import DetokenizeManager
   from minisgl.message import DetokenizeMsg
   tok = AutoTokenizer.from_pretrained("Qwen/Qwen3-0.6B")
   mgr = DetokenizeManager(tok)
   ids = tok.encode("你好，世界", return_tensors="pt").view(-1).tolist()
   for i, tid in enumerate(ids):
       finished = (i == len(ids) - 1)
       out = mgr.detokenize([DetokenizeMsg(uid=1, next_token=tid, finished=finished)])
       print(i, repr(out[0]))
   ```

**需要观察的现象**：某些步返回空串 `''`（那个 token 还凑不成完整字符，被 `endswith("�")` 拦住或被 `find_printable_text` 截断），随后某一步会一次性返回较长的字符串（凑齐后补发）。

**预期结果**：所有增量拼起来等于 `"你好，世界"`，且没有任何一步返回包含 `�` 的串。**待本地验证**（脚本运行需能下载模型）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `finished and next_token == eos` 时要把这个 token **排除**在 `decoded_ids` 之外？

> **答案**：eos（end-of-sequence）是「句子结束」的控制标记，不是用户要看的文字。如果把它一起 decode，输出末尾会多出一个 `</s>` / `<|im_end|>` 之类的可见或不可见符号，污染最终回复。所以在「这是最后一个 token 且它就是 eos」时跳过追加，保证 `decoded_ids` 里只有真正的内容 token。

**练习 2**：`incremental_output = output_str[s.sent_offset:]` 这行在保证什么不变量？

> **答案**：保证「流式发送的不重不漏」。`sent_offset` 记录已经发给前端的字符数，`output_str[s.sent_offset:]` 严格只取这次新长出来的部分。即使内部 `decoded_str` 因为「半个字」被反复重算，只要 `sent_offset` 单调推进，前端把所有 `incremental_output` 顺序拼接，就一定得到完整且无重复的文本。

**练习 3**：如果一个用户请求生成到一半被中断（abort），`decode_map` 里它的 `DecodeStatus` 会被清理吗？

> **答案**：不会自动清理。`DetokenizeManager.detokenize` 只在收到 `finished=True` 的 `DetokenizeMsg` 时才 `del self.decode_map[uid]`（见 [detokenize.py:108-109](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/detokenize.py#L108-L109)）。abort 走的是另一条路（`AbortMsg` → `AbortBackendMsg`，见 4.4），它**不经过 detokenize**，所以 detokenizer 侧不会收到该 uid 的 `finished` 消息，那条 `DecodeStatus` 会残留。实践中这通常无害（uid 全局唯一不会复用，进程退出时整体回收），但要知道这个状态机是「靠 finished 驱动清理」的。

### 4.4 消息分流与拓扑配置

#### 4.4.1 概念说明

4.1 讲了循环骨架，这里把循环里的「三条分流支路」和「worker 到底连到哪儿」讲透。本模块回答两个问题：

1. 收到的消息按类型分成三堆后，各自走哪条转换 + 哪条出口？
2. `num_tokenizer` 这个参数如何改变 worker 的数量和 ZMQ 拓扑（谁 bind、谁 connect）？

第二个问题尤其关键，它是理解「一个函数两种身份」落到工程上怎么布置的钥匙。

#### 4.4.2 核心流程

**三条分流支路**（在主循环里依次判断、互不干扰）：

| 收到的消息 | 处理 | 产出 | 出口 |
| --- | --- | --- | --- |
| `TokenizeMsg` | `TokenizeManager.tokenize` | `UserMsg(uid, input_ids, sampling_params)` | `send_backend`（去 scheduler） |
| `DetokenizeMsg` | `DetokenizeManager.detokenize` | `UserReply(uid, incremental_output, finished)` | `send_frontend`（回 api_server） |
| `AbortMsg` | 直接转包 | `AbortBackendMsg(uid)` | `send_backend`（去 scheduler） |

注意一个小优化：当一批里只有 1 条结果时，代码会把 `BatchXxxMsg(data=[...])` 拆成单条 `XxxMsg` 再发（`if len(batch_output.data) == 1: batch_output = batch_output.data[0]`），省一层信封。

`AbortMsg` 这条支路值得单独说明：客户端断连时（见 [u3-l1](u3-l1-api-server.md) 的 `stream_with_cancellation`），api_server 发出 `AbortMsg(uid)`。worker 收到后**不调用任何 manager**，只是把它「翻译」成 backend 能认的 `AbortBackendMsg(uid)` 转发给 scheduler，让 scheduler 停止为该 uid 继续算。这是 worker 兼任的第三个职责——**消息协议转换器**。

**两种拓扑**（由 `num_tokenizer` 决定）：

- **共享模式（默认，`num_tokenizer == 0`，即 `share_tokenizer == True`）**：只启动 **1 个** worker（名字叫 `minisgl-detokenizer-0`，`tokenizer_id=0`）。它的 `recv_listener` 以 `create=True` **绑定** `zmq_detokenizer_addr`（此时 `zmq_tokenizer_addr` 与之相同）。api_server（发 `TokenizeMsg`）和 scheduler（发 `DetokenizeMsg`）都 connect 到这个地址。于是**同一个进程的同一个收信 socket 上同时收到两种消息**，靠类型分流。
- **独立模式（`num_tokenizer > 0`）**：启动 **1 个 detokenizer + N 个 tokenizer**，共 N+1 个 worker。tokenizer 进程 connect 到 `zmq_tokenizer_addr`（由 api_server bind），只收 `TokenizeMsg`；detokenizer 进程 connect 到 `zmq_detokenizer_addr`（由 scheduler bind），只收 `DetokenizeMsg`。两条链路物理分离，可并行、可水平扩展 tokenizer 数量来分摊 CPU 编码压力。

#### 4.4.3 源码精读

主循环里的三条分流支路：

[python/minisgl/tokenizer/server.py:71-108](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py#L71-L108) —— detokenize → frontend、tokenize → backend、abort → backend 三条支路。下面是 detokenize 与 abort 两段（tokenize 段见 4.2.3 引用）：

```python
if len(detokenize_msg) > 0:
    replies = detokenize_manager.detokenize(detokenize_msg)
    batch_output = BatchFrontendMsg(
        data=[UserReply(uid=msg.uid, incremental_output=reply, finished=msg.finished)
              for msg, reply in zip(detokenize_msg, replies, strict=True)])
    if len(batch_output.data) == 1:
        batch_output = batch_output.data[0]
    send_frontend.put(batch_output)
...
if len(abort_msg) > 0:
    batch_output = BatchBackendMsg(data=[AbortBackendMsg(uid=msg.uid) for msg in abort_msg])
    if len(batch_output.data) == 1:
        batch_output = batch_output.data[0]
    send_backend.put(batch_output)
```

启动拓扑：`launch_server` 里先起 1 个 detokenizer，再起 N 个 tokenizer。

[python/minisgl/server/launch.py:73-103](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L73-L103) —— 用同一个 `tokenize_worker` 目标函数起两类进程；detokenizer 的 `tokenizer_id=num_tokenizers`，tokenizers 的 `tokenizer_id=i`。

```python
num_tokenizers = server_args.num_tokenizer
# DeTokenizer, only 1
mp.Process(target=tokenize_worker, kwargs={
    "addr": server_args.zmq_detokenizer_addr,
    "create": server_args.tokenizer_create_addr,
    "tokenizer_id": num_tokenizers, ...}, name="minisgl-detokenizer-0").start()
for i in range(num_tokenizers):
    mp.Process(target=tokenize_worker, kwargs={
        "addr": server_args.zmq_tokenizer_addr,
        "create": server_args.tokenizer_create_addr,
        "tokenizer_id": i, ...}, name=f"minisgl-tokenizer-{i}").start()
```

决定拓扑的几个 `ServerArgs` 派生属性：

[python/minisgl/server/args.py:18-47](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L18-L47) —— `share_tokenizer` 派生出各地址与 `create` 标志，决定谁 bind 谁 connect。

```python
num_tokenizer: int = 0
@property
def share_tokenizer(self) -> bool:
    return self.num_tokenizer == 0
@property
def zmq_tokenizer_addr(self) -> str:
    if self.share_tokenizer:
        return self.zmq_detokenizer_addr   # 共享：两地址合并
    result = "ipc:///tmp/minisgl_4" + self._unique_suffix
    ...
@property
def tokenizer_create_addr(self) -> bool:
    return self.share_tokenizer            # 共享：worker 自己 bind
@property
def backend_create_detokenizer_link(self) -> bool:
    return not self.share_tokenizer        # 独立：scheduler bind detokenizer 地址
@property
def frontend_create_tokenizer_link(self) -> bool:
    return not self.share_tokenizer        # 独立：api_server bind tokenizer 地址
```

把这三处连起来读，就能补全「谁 bind」的全景（独立模式下）：

- `zmq_backend_addr`：scheduler 的 `_recv_from_tokenizer` 以 `create=True` 绑定（见 [scheduler/io.py:36-43](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L36-L43)），所有 worker connect 过去推 `UserMsg`/`AbortBackendMsg`。
- `zmq_frontend_addr`：api_server 的 `recv_tokenizer` 以 `create=True` 绑定（见 [api_server.py:433-442](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L433-L442)），detokenizer worker connect 过去推 `UserReply`。
- `zmq_tokenizer_addr` / `zmq_detokenizer_addr`：独立模式下分别由 api_server、scheduler 绑定；共享模式下合并为一个、由唯一的 worker 绑定。

就绪握手计数：主进程等待 `num_tokenizers + 2` 条 ack（1 个 scheduler 主 rank + N 个 tokenizer + 1 个 detokenizer）。

[python/minisgl/server/launch.py:110-111](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L110-L111) —— 阻塞收齐 ack 才返回，保证开服前所有 worker 就绪。

#### 4.4.4 代码实践

**实践目标**：把「消息分流」与「拓扑配置」串起来，画出完整的请求转换图与进程连接图。这是本讲的主实践。

**操作步骤**（源码阅读 + 画图）：

1. **入向追踪**：从 [api_server.py 构造 `TokenizeMsg`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L234) 开始 → `send_tokenizer` 推到 `zmq_tokenizer_addr` → worker `recv_listener` 收到 → 命中 [server.py 的 tokenize 分支](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py#L87-L101) → `TokenizeManager.tokenize` → `UserMsg` → `send_backend` → scheduler。
2. **出向追踪**：scheduler 把生成结果包成 `DetokenizeMsg` 推到 `zmq_detokenizer_addr`（见 [scheduler/io.py 的 `_reply_tokenizer_rank0`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L124-L143)）→ worker 收到 → 命中 [server.py 的 detokenize 分支](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py#L71-L85) → `DetokenizeManager.detokenize` → `UserReply` → `send_frontend` → api_server。
3. 画两张图：
   - **消息转换图**：标出每一步的消息类型（`TokenizeMsg`/`UserMsg`/`DetokenizeMsg`/`UserReply`）、字段变化（`text`→`input_ids`、`next_token`→`incremental_output`）。
   - **进程连接图**：分别画 `num_tokenizer=0` 与 `num_tokenizer=2` 两种拓扑，标出每条 ZMQ 通道的地址、谁 `bind`、谁 `connect`、上面跑的是哪种消息。

**需要观察的现象**：

- 在消息转换图上，worker 是**两种方向的交汇点**：入向把 `str|List[Dict]` 压成 `int32` 张量，出向把 `int` 还原成增量 `str`。
- 在进程连接图上，共享模式只有 1 个 worker 节点，独立模式有 N+1 个；两种模式下 `zmq_backend_addr` 与 `zmq_frontend_addr` 的 bind 方（scheduler / api_server）不变。

**预期结果**：得到两张自洽的图，且图中每条边都能在源码里找到对应的 `ZmqPushQueue`/`ZmqPullQueue` 与 `create=` 取值。运行验证（启动 `--num-tokenizer 2` 看进程数与日志）**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：默认配置下（`num_tokenizer=0`），系统一共启动了几个 worker 进程？分别叫什么？

> **答案**：1 个。在 [launch.py:73-87](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/launch.py#L73-L87) 以 `name="minisgl-detokenizer-0"`、`tokenizer_id=num_tokenizers=0` 启动；后面的 `for i in range(0)` 循环不执行。这唯一一个进程同时承担 tokenize 与 detokenize。

**练习 2**：什么场景下应该调大 `num_tokenizer`？

> **答案**：当**入向 tokenize 成为瓶颈**时——例如大量并发短请求、且 prompt 需要走 `apply_chat_template`（CPU 开销较大）时，单个 worker 的 CPU 会打满，编码排队会拖慢首 token 延迟。调大 `num_tokenizer` 能水平分担编码压力。代价是进程变多、独立模式下 detokenizer 仍只有 1 个（出向不能水平扩展），且拓扑更复杂。注意 detokenize 因为带流式状态机（`decode_map` 按 uid 存状态），天然只能集中在单一进程里，不能像 tokenize 那样随便拆。

**练习 3**：为什么 `AbortMsg` 在 worker 里不经过任何 manager，直接转成 `AbortBackendMsg`？

> **答案**：因为 abort 是**控制信号**，不涉及任何文本/token 转换。worker 在这里只做「协议适配」：把 frontend/tokenizer 族的消息（`AbortMsg` 属于 `BaseTokenizerMsg`）翻成 backend 族能认的 `AbortBackendMsg`，让 scheduler 去停止该 uid 的计算。这也说明 worker 的职责其实是「面向 tokenizer 侧消息的通用入口」，tokenize/detokenize 只是其中两种需要实际计算的消息类型。

## 5. 综合实践

把本讲四个模块串起来，完成一次**完整的两端追踪 + 拓扑对比**。

**任务**：选定一个模型（如 `Qwen/Qwen3-0.6B`），分别用默认（`num_tokenizer=0`）与 `--num-tokenizer 2` 两种配置，追踪同一条用户输入 `"用一句话解释 KV cache"` 的完整往返，并产出一份报告。

**步骤**：

1. **入向链路**：api_server 收到 `/v1/chat/completions` → 构造 `TokenizeMsg(uid, text=[{...}], sampling_params)` → 推 `zmq_tokenizer_addr`。在 [tokenize.py:18-26](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/tokenize.py#L18-L26) 确认 `text` 是 list 走 chat template 分支，得到带 `<|im_start|>...` 标记的 prompt，再 encode 成 `input_ids`，包成 `UserMsg` 推给 backend。
2. **出向链路**：模型逐 token 生成，scheduler 把每个 token 包成 `DetokenizeMsg(uid, next_token, finished=False)` 推 `zmq_detokenizer_addr`。在 [detokenize.py:88-107](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/detokenize.py#L88-L107) 追踪游标推进，最后一条 `finished=True` 时 `del decode_map[uid]`。增量包成 `UserReply` 推 `zmq_frontend_addr`，api_server 转 SSE 发给用户。
3. **拓扑对比**：在报告里画出两种配置的进程连接图，重点标出：
   - `num_tokenizer=0`：1 个 worker，bind 一个合并地址，同 socket 收两种消息。
   - `num_tokenizer=2`：3 个 worker（1 detokenizer + 2 tokenizer），api_server bind tokenizer 地址做负载分发，scheduler bind detokenizer 地址。
4. **断连场景**（阅读型）：说明用户中途断开时，`AbortMsg` 如何在 [server.py:102-108](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py#L102-L108) 被翻译成 `AbortBackendMsg`，并思考：此时该 uid 的 `DecodeStatus` 会被清理吗？（参考 4.3.5 练习 3）

**预期产出**：一张消息转换图 + 两张拓扑图 + 一段对断连副作用的说明。运行部分（启动服务、curl 验证）**待本地验证**；纯源码追踪部分现在即可完成。

## 6. 本讲小结

- `tokenize_worker` 是**一个函数两种身份**：不区分自己是 tokenizer 还是 detokenizer，只按收到的消息类型（`TokenizeMsg` / `DetokenizeMsg` / `AbortMsg`）分流处理。
- 主循环用 `local_bs` 做「攒批」阈值，但当前 `local_bs` 写死为 1，实际是来一条处理一条；真正的批量 tokenize 是 `tokenize.py` 里的 TODO。
- `TokenizeManager` 做入向：区分 chat 消息列表（套 chat template）与裸字符串，统一 encode 成一维 int32 张量，包成 `UserMsg` 送给 backend。
- `DetokenizeManager` 做出向：用 `DecodeStatus` 的三个偏移量（`read_offset`/`surr_offset`/`sent_offset`）实现流式增量 decode，靠 `endswith("�")` 与 `find_printable_text` 解决「半个字」问题，按 `uid` 维护状态、`finished` 时清理。
- 三条分流支路分别去 `send_backend`（tokenize、abort）和 `send_frontend`（detokenize）；`AbortMsg` 只做协议翻译，不调任何 manager。
- `num_tokenizer` 决定拓扑：默认 0 为共享模式（1 个 worker，自己 bind 合并地址）；大于 0 为独立模式（1 detokenizer + N tokenizer，地址由 scheduler/api_server 分别 bind）。detokenizer 因带流式状态而不可水平扩展。

## 7. 下一步学习建议

本讲把请求在「CPU 侧」的最后一站讲完了。接下来：

- **顺着入向走**：`UserMsg` 被推进 `zmq_backend_addr` 后，是谁在收？进入 [u4 调度器 Scheduler](u4-l1-scheduler-main-loop.md)，看 Scheduler 的 I/O 如何收消息、rank0 如何广播给其他 rank（[u4-l2 Scheduler I/O 与多 rank 广播](u4-l2-scheduler-io.md)）。
- **顺着出向走**：`DetokenizeMsg` 是 scheduler 在每轮 decode 后构造的，可结合 [u4-l1 Scheduler 主循环](u4-l1-scheduler-main-loop.md) 与 [u5-l2 Engine forward 与采样](u5-l2-engine-forward-sampling.md) 理解 `next_token` 从哪来。
- **补全前端**：`UserReply` 回到 api_server 后如何变成 SSE 流，详见 [u3-l1 API Server](u3-l1-api-server.md) 的 `FrontendManager` 与 `stream_with_cancellation`。
- **序列化细节**：若对 `UserMsg.input_ids` 这个一维张量如何跨进程传输感兴趣，回看 [u2-l3 进程间消息与序列化](u2-l3-message-serialization.md) 的 tensor 序列化部分。

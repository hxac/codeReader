# 内部 Client 客户端层

> 本讲对应仓库讲义 `u2-l3`，承接 [u2-l1 请求主链路总览](u2-l1-request-main-chain.md) 与 [u2-l2 OpenAI 兼容 API 服务层](u2-l2-openai-api-layer.md)。
> 上一讲我们看到 HTTP 层调用了 `client.completion(...)`、`client.completion_stream(...)`、`client.speech(...)` 这些方法，
> 却刻意没有展开它们内部干了什么。本讲就下钻到这一层——**内部 Client**——看看一份 `GenerateRequest` 是怎么变成管线能懂的 `OmniRequest`、怎么提交给 Coordinator、
> 管线吐回来的一串碎片又是怎么被拼成一条完整回复的。

## 1. 本讲目标

读完本讲，你应当能够：

- 说出 `Client` 类在整个分层里「只做什么、不做什么」，并能默写它的三个对外接口表面：`generate`（低级）、`completion` / `completion_stream` / `speech`（高级）。
- 解释 `request_id` 是如何生成的（`uuid.uuid4()`），以及它为什么必须由 Client 而非 HTTP 层负责。
- 跟踪一次 `GenerateRequest` 是如何被 `_build_omni_request`（`_extract_inputs` + `_build_params`）翻译成 `OmniRequest` 的，并指出多模态字段（`images/audios/videos`）去了哪里。
- 讲清楚非流式 `completion` 如何把一串 `GenerateChunk` 片段聚合（文本 `join`、音频 `np.concatenate`）、并把音频做 base64 编码，最终产出 `CompletionResult`。

## 2. 前置知识

进入源码前，先用几段大白话建立直觉。

**为什么需要一个「内部 Client」层？**
在 u2-l1 我们画过分层链路：HTTP API → Client → Coordinator → Stage → Scheduler → ModelRunner。HTTP 层只懂「OpenAI 格式的 JSON」和「SSE 流」，Coordinator 只懂「控制平面消息」和「`OmniRequest`」。这两者之间隔着一道鸿沟：HTTP 层产出的 `GenerateRequest` 带着采样参数、消息列表、多模态引用，而 Coordinator 想要的是一个带 `inputs/params/metadata` 三段式、且和具体 API 协议无关的 `OmniRequest`。Client 层就是这道鸿沟上的桥——它负责**协议无关的请求翻译**和**结果聚合**。理解它的关键是：Client 自己**也不碰 GPU**，它只是把请求翻译好、递给 Coordinator，再把 Coordinator 流式吐回的碎片拼起来。

**`request_id` 是什么、为什么重要？**
一次推理请求在管线里要穿过好几个 stage、可能跨多个进程和 GPU。要让 Coordinator 能追踪它、让用户能 `abort` 它、让多个 terminal（比如文本收口 `decode` 和语音收口 `code2wav`）的结果能被正确合并，就必须有一个**全局唯一**的标识。Client 用 `uuid.uuid4()` 生成这个 id，并把它一路带到 Coordinator。注意：HTTP 层也可能传入自己生成的 id（u2-l2 里 OpenAI 兼容端点会生成），Client 的 `generate` 接受可选的 `request_id` 参数——传了就用，没传就自己造一个。

**流式（stream）与非流式的区别。**
一个 AR（自回归）模型生成回复时，是一个 token 一个 token 往外吐的。流式就是「吐一个给一个」，非流式就是「全部吐完再一次性给」。Client 的 `generate` 是一个**异步生成器**（`AsyncIterator`），无论上层要流式还是非流式，底层都通过迭代它来拿数据；`completion` 是非流式的——它把 `generate` 迭代到底、在内存里把片段拼好再返回一个 `CompletionResult`；`completion_stream` 则是流式的——它把每个片段包成 `CompletionStreamChunk` 原样向上吐。这就是「同一个底层迭代器，两种消费姿势」。

**多模态（multimodal）与 terminal（终态）两个术语。**
- 多模态：指请求里除了纯文本，还可能带图像（`images`）、音频（`audios`）、视频（`videos`）等。Client 在翻译请求时要把这些引用塞进 `OmniRequest.metadata`，让下游 stage 能拿到。
- terminal：一条管线可以有多个「终态阶段」（u1-l1、u2-l1 提过）。例如 Qwen3-Omni 同时有文本收口（`decode`）和语音收口（`code2wav`）。Coordinator 必须等所有期望终态都完成才算这次请求结束；而 Client 在 `_default_result_builder` 里要处理「`decode` + `code2wav` 合并成一条回复」的情况。

> 一个贯穿全讲的关键认知（承接 u2-l1、u2-l2）：**Client 层是翻译+聚合层，不碰 GPU**。真正算东西的是更内层的 Scheduler/ModelRunner。Client 只是 Coordinator 上面一层很薄的「门面」（facade），把请求整理好递下去、把碎片拼好递上来。

## 3. 本讲源码地图

本讲聚焦两个核心文件，必要时旁征两个邻居文件：

| 文件 | 作用 |
| --- | --- |
| [sglang_omni/client/client.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py) | **主文件**。`Client` 类的全部接口（`generate/completion/completion_stream/speech/abort/admin` 系列）、请求翻译（`_build_omni_request/_extract_inputs/_build_params`）、结果与流式 builder 都在这里。 |
| [sglang_omni/client/types.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/types.py) | **类型定义**。`GenerateRequest`（HTTP 层交给 Client 的请求形状）、`GenerateChunk`（流式片段）、`CompletionResult`/`CompletionStreamChunk`/`SpeechResult`（高级返回类型）、`ClientError`。 |
| [sglang_omni/client/audio.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/audio.py) | 音频编码工具：`to_numpy`（统一转 float32 数组）、`encode_audio`（wav/mp3/flac/opus/aac/pcm，带格式回退）、`audio_to_base64`（编码进 base64 字符串，便于嵌进 JSON）。 |
| [sglang_omni/proto/request.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/request.py) | 定义 `OmniRequest`（`inputs/params/metadata` 三段式）与 `RequestState`/`RequestInfo`，是 Client 提交给 Coordinator 的目标类型。 |

记忆口诀：**「类型在 `types.py`，行为在 `client.py`，编码在 `audio.py`」**。`types.py` 只有数据形状（dataclass），`client.py` 才有翻译与聚合逻辑，`audio.py` 专管原始音频张量如何变成可传输的字节/字符串。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 Client 类与接口表面** —— 这层对外暴露了哪些方法、各自定位（低级 `generate` vs 高级 `completion/speech`）。
- **4.2 OmniRequest 转换** —— `GenerateRequest` 如何变成管线能懂的 `OmniRequest`。
- **4.3 结果聚合** —— 一串 `GenerateChunk` 片段如何被拼成 `CompletionResult`。
- **4.4 音频编码** —— numpy/张量音频如何变成 base64 字符串或编码字节。

### 4.1 Client 类与接口表面

#### 4.1.1 概念说明

`Client` 是一个很薄的**门面**（facade）：它内部只持有一个 `Coordinator` 引用，再加两个可选的「builder」回调。它的全部价值在于把「面向协议的请求」整理成「Coordinator 能消费的请求」，并把「Coordinator 流式吐回的碎片」整理成「上层好用的结果对象」。

它的对外接口可以分成三档：

| 档位 | 方法 | 定位 |
| --- | --- | --- |
| 低级 | `generate` | 异步生成器，原样转发 Coordinator 的流/终态消息，返回 `GenerateChunk`。是其它高级方法的公共底座。 |
| 高级·聚合 | `completion` | 非流式，迭代 `generate` 到底，在内存里拼好文本+音频，返回 `CompletionResult`。 |
| 高级·流式 | `completion_stream` | 流式，把每个 `GenerateChunk` 包成 `CompletionStreamChunk` 原样吐。 |
| 高级·TTS | `speech` | 专为 `/v1/audio/speech`，强制非流式，只要音频，返回 `SpeechResult`（编码字节+MIME）。 |
| 控制 | `abort` / `get_status` / `health` | 中止请求、查状态、健康检查。 |
| Admin | `admin` / `model_info` / `update_weights_*` / `pause_generation` / ... | 一组对 Coordinator admin 通道的透传包装（详见 u6-l4 RL 控制）。 |

理解这层的关键不变量：**所有「真正算东西」的调用最终都汇到 `generate`，而 `generate` 最终都汇到 `coordinator.submit` 或 `coordinator.stream`**。Client 自己不调度、不前向。

#### 4.1.2 核心流程

一次高级非流式调用的总流程（自外向内再向外）：

```
HTTP 层: await client.completion(gen_req, request_id=rid, audio_format="wav")
   │
   ▼
completion() 内部：
   async for chunk in self.generate(request, request_id=request_id):   # 复用低级迭代器
       累积 text_parts / audio_chunks / sample_rate / finish_reason / usage ...
   拼接 full_text, np.concatenate(audio), audio_to_base64(...)
   return CompletionResult(...)
   │
   ▼
generate() 内部：
   req_id = request_id or str(uuid.uuid4())            # ① 生成/复用 request_id
   omni_request = self._build_omni_request(request)    # ② 翻译成 OmniRequest
   if request.stream:
       async for msg in coordinator.stream(req_id, omni_request):   # ③ 流式提交
           StreamMessage  -> _stream_builder -> GenerateChunk
           终态消息        -> _result_builder -> GenerateChunk
   else:
       result = await coordinator.submit(req_id, omni_request)       # ④ 非流式提交
       yield _result_builder(req_id, result)
   │
   ▼
Coordinator（下一讲 u2-l4 详讲）: submit/stream/abort ...
```

注意一个细节：**`generate` 永远是「流式迭代器」**。即使是非流式请求，它也只 `yield` 一次（一个终态结果）；高级方法通过「迭代它到底」或「边迭代边吐」来区分行为。这是把流式与非流式统一在一个底座上的关键设计。

#### 4.1.3 源码精读

先看构造与低级 `generate`：

[sglang_omni/client/client.py:39-47](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L39-L47) —— 构造函数只存 `Coordinator` 引用，并允许调用方注入自定义的 `result_builder`/`stream_builder`（用于模型特定的结果形状，不传则用默认实现）。

[sglang_omni/client/client.py:53-69](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L53-L69) —— 低级 `generate`。三步走：① `req_id = request_id or str(uuid.uuid4())` 决定 request_id；② `_build_omni_request` 翻译请求；③ 按是否流式选 `coordinator.stream`（逐条消息）或 `coordinator.submit`（一次终态）。注意 `coordinator.stream` 迭代到的消息可能是 `StreamMessage`（中间流片段，走 `_stream_builder`）也可能是终态结果（走 `_result_builder`）。

再看三档高级方法的「骨架」差异（聚合 vs 流式 vs TTS）：

[sglang_omni/client/client.py:75-153](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L75-L153) —— 非流式 `completion`：遍历 `generate`，把每个 chunk 的 `text/audio_data/sample_rate/finish_reason/usage/output_token_logprobs/omni_rollout/weight_version` 分别累加进各自的容器；若一个 chunk 都没收到就抛 `ClientError("No response from pipeline")`；最后把文本 `"".join`、音频 `np.concatenate` 并 base64 编码，返回 `CompletionResult`。聚合细节在 4.3 详讲。

[sglang_omni/client/client.py:159-188](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L159-L188) —— 流式 `completion_stream`：同样遍历 `generate`，但**不累积**，每个 chunk 直接包成 `CompletionStreamChunk` 吐出；唯一处理是对音频做 base64（注释强调「让调用方永远不用碰 numpy/原始字节」）。这与 u2-l2 讲的 SSE 流式直接对应。

[sglang_omni/client/client.py:194-258](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L194-L258) —— TTS 专用 `speech`：先用 `dataclasses.replace` 把请求**强制改成非流式**（`stream=False`，并清掉 `extra_params` 里的 `stream`），因为 `/v1/audio/speech` 只要最终音频；只收 `audio_data`，没有音频就抛 `ClientError("No audio output generated from the pipeline.")`；最后用 `encode_audio` 编码（放线程里跑 `asyncio.to_thread` 避免阻塞事件循环），并从返回的 MIME 反推实际格式（编码器可能回退到 WAV）。

最后是一组「控制 / Admin」的薄包装：

[sglang_omni/client/client.py:264-279](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L264-L279) —— `abort` 透传 `coordinator.abort`，`get_status` 取 `coordinator.get_request_info` 的 `.state`，`health` 直接返回 `coordinator.health()` 的字典（这正是 u1-l4 里 `/health` 端点判断 `running` 字段的来源）。

[sglang_omni/client/client.py:296-305](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L296-L305) —— `model_info` 及其后的 `update_weights_from_disk` / `init_weights_update_group` / `pause_generation` 等都是同构的薄包装：把 `payload/stages/timeout_s` 原样转给 Coordinator 的同名方法。这些是 u6-l4「RL 权重热更新与 Admin 控制」的入口，本讲只需记住「Client 不实现它们，只转发」。

#### 4.1.4 代码实践

**实践目标**：不启动真模型，仅凭 Client 的接口形状，验证「`generate` 是流式迭代器底座、高级方法只是它的两种消费姿势」。

**操作步骤**：

1. 打开 [sglang_omni/client/client.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py)，在 `generate`（53 行）、`completion`（75 行）、`completion_stream`（159 行）三个方法各贴一个心智断点。
2. 用文字（伪代码）追踪：当 HTTP 层调 `completion` 时，控制流是 `completion → generate → coordinator.submit`；当调 `completion_stream` 时是 `completion_stream → generate → coordinator.stream`。
3. 在仓库里搜索 `client.generate(`、`client.completion(`、`client.speech(` 的所有调用点，确认它们都来自 `serve/` 层。

**需要观察的现象**：你会看到三个高级方法**都**调用 `self.generate(...)`，没有任何一个高级方法直接碰 `coordinator.submit/stream`——证明「`generate` 是唯一底座」这条不变量成立。

**预期结果**：在 `sglang_omni/serve/openai_api.py` 中能找到形如 `async for chunk in client.generate(gen_req, request_id=request_id)`（如 PCM 直出端点）和 `await client.completion(...)`、`client.completion_stream(...)`、`client.speech(...)` 的调用，但**找不到**任何 `client._coordinator.submit(...)` 的直接调用——Coordinator 始终藏在 Client 后面。

#### 4.1.5 小练习与答案

**练习 1**：`completion` 和 `completion_stream` 都迭代 `generate`，为什么前者能返回「一条完整回复」而后者是「一串片段」？

> **参考答案**：`completion` 在循环体里把每个 chunk 的文本/音频累加进容器，循环结束后才拼装成一个 `CompletionResult` 返回（**累积**）；`completion_stream` 在循环体里对每个 chunk 直接 `yield` 一个 `CompletionStreamChunk`，不累积（**透传**）。底层 `generate` 完全一样，区别只在「怎么消费」。

**练习 2**：`speech` 方法第一行就 `replace(request, stream=False, ...)`，为什么要强制改成非流式？

> **参考答案**：`/v1/audio/speech` 语义是「给我一段完整的合成语音」，不需要边生成边吐。强制 `stream=False` 让 `generate` 走 `coordinator.submit`（一次性终态），从而 `speech` 只需收集所有音频块再统一编码，逻辑更简单、也避免流式 SSE 包装。同时清掉 `extra_params["stream"]` 防止该标志被 `_build_params` 重新写回 params。

---

### 4.2 OmniRequest：从 GenerateRequest 到管线请求的转换

#### 4.2.1 概念说明

`GenerateRequest`（[types.py:81](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/types.py#L81)）是**协议无关**的请求对象，但它仍然是「面向调用者」的形状：字段叫 `prompt/messages/sampling/stream/max_tokens`，多模态藏在 `metadata` 里。而 Coordinator 和管线内部各 stage 想要的是 `OmniRequest`——一个只有三段的结构：

- `inputs`：模型真正要「吃」的东西（一段文本、一串 token id、或一个带媒体的消息字典）。
- `params`：生成参数（采样、`max_new_tokens`、`stream`、阶段级参数、extra params）。
- `metadata`：旁路信息（`model`、`output_modalities`、各种媒体引用、显式参数标志等）。

这道转换由静态方法 `_build_omni_request` 完成，它把「翻译职责」集中在一处：上层不管来自 chat、speech、generate 哪个端点，只要产出标准 `GenerateRequest`，Client 就能统一翻成 `OmniRequest`。这正是 u2-l2 把多模态塞进 `metadata` 的转换放在 HTTP 层、而**最终落地成 `OmniRequest` 放在 Client 层**的原因——HTTP 层负责「把外部协议的脏活封死在边界」，Client 层负责「把协议无关请求变成管线请求」。

#### 4.2.2 核心流程

`_build_omni_request(request)` 的三步翻译：

```
GenerateRequest
   │
   ├─ inputs = _extract_inputs(request)        # 选出唯一的「输入主体」
   │      规则：prompt / prompt_token_ids / messages 三选一，否则报错
   │            若有 multimodal_train_inputs，要求必须配 prompt_token_ids
   │            若 messages 且 metadata 里有 images/audios/videos，
   │            返回 {"messages":..., "images":..., "audios":..., "videos":...}
   │
   ├─ params = _build_params(request)          # 打包生成参数
   │      规则：sampling.to_dict() 为基底；
   │            max_tokens 覆盖 sampling.max_new_tokens（都可能为 None→删除键）；
   │            追加 stream、stage_sampling、stage_params、extra_params
   │
   ├─ metadata = dict(request.metadata)        # 复制旁路信息
   │      追加 setdefault("model", request.model)
   │      追加 metadata["output_modalities"] = request.output_modalities
   │
   ▼
OmniRequest(inputs=inputs, params=params, metadata=metadata)
```

两个关键纪律：
- **输入唯一性**：`_extract_inputs` 强制 `prompt`/`prompt_token_ids`/`messages` 恰好有一个非空，否则抛 `ValueError`。这避免「既给了 prompt 又给了 messages」的歧义请求悄悄进入管线。
- **参数来源优先级**：`max_tokens`（OpenAI 风格）若存在则覆盖 `sampling.max_new_tokens`（SGLang 风格）；两者都没有时从 params 里**删掉** `max_new_tokens` 键（而不是传 `None`），让下游能用模型默认值。

#### 4.2.3 源码精读

[sglang_omni/client/client.py:442-450](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L442-L450) —— `_build_omni_request` 主体：调 `_extract_inputs` 拿 `inputs`，`_build_params` 拿 `params`，复制 `metadata` 并补 `model`（用 `setdefault` 不覆盖已有）与 `output_modalities`（直接覆盖），最后组装 `OmniRequest`。

[sglang_omni/client/client.py:590-645](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L590-L645) —— `_extract_inputs`：先 `sum(choices) != 1` 校验三选一；再处理 `multimodal_train_inputs`（要求配 `prompt_token_ids`，返回带 `input_ids` 的字典）；再按 `prompt`→`prompt_token_ids`→`messages` 顺序取值；`messages` 分支里还会把 `metadata` 里的 `images/audios/videos` 以及 `video_fps/video_max_frames/...` 等视频参数一并塞进返回字典——**这就是多模态字段从 HTTP 层 `metadata` 最终进入 `OmniRequest.inputs` 的落点**。

[sglang_omni/client/client.py:648-666](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L648-L666) —— `_build_params`：以 `sampling.to_dict()` 为基底；`max_tokens` 与 `sampling.max_new_tokens` 协商出最终的 `max_new_tokens`（为 `None` 则 `pop` 掉键）；追加 `stream`、`stage_sampling`（每个 stage 的采样参数）、`stage_params`、`extra_params`（直接 `update` 进去）。

再看翻译的「目标类型」长什么样：

[sglang_omni/proto/request.py:34-48](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/request.py#L34-L48) —— `OmniRequest` 定义：只有 `inputs: Any`、`params: dict`、`metadata: dict` 三个字段，外加 `to_dict/from_dict`（序列化时打上 `"_type": "OmniRequest"` 标记，方便跨进程/msgpack 重建）。对比 [types.py:81-123](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/types.py#L81-L123) 的 `GenerateRequest`（十几个具名字段），就能直观感受「面向调用者的富结构」到「面向管线的三段式」的收敛。

#### 4.2.4 代码实践

**实践目标**：用一个纯文本请求和一个带图像的请求，手工推演 `_extract_inputs` 的返回值，确认多模态字段去了哪里。

**操作步骤**：

1. 阅读上面引用的 `_extract_inputs` 源码，画出它的判定分支树。
2. **场景 A**（纯文本）：构造 `GenerateRequest(prompt="你好")`，推演：`choices` 只有 `prompt` 非空 → `sum==1` 通过 → 走 `if request.prompt is not None` → 返回字符串 `"你好"`。
3. **场景 B**（带图消息）：构造 `GenerateRequest(messages=[Message("user","看图")], metadata={"images":["http://.../a.png"]})`，推演：`messages` 非空且 `metadata` 有 `images` → 返回 `{"messages":[...], "images":["http://.../a.png"]}`。
4. **场景 C**（非法）：构造 `GenerateRequest(prompt="x", messages=[...])`，推演：`sum(choices)==2` → 抛 `ValueError("...requires exactly one input...")`。

**需要观察的现象**：场景 B 里图像 URL **没有**出现在 `params` 或顶层，而是嵌在 `inputs` 字典里与 `messages` 并列；这意味着下游 stage 拿到 `OmniRequest.inputs` 时，能同时看到「对话文本」和「媒体引用」。

**预期结果**：三种场景的推演结果与源码分支一一对应；你应能解释「为什么图像 URL 必须先被 HTTP 层放进 `metadata`（u2-l2），再由 Client 的 `_extract_inputs` 提到 `inputs` 里」——这是两步接力，HTTP 层管协议映射，Client 层管管线打包。

#### 4.2.5 小练习与答案

**练习 1**：`_build_params` 里 `max_tokens` 和 `sampling.max_new_tokens` 同时存在时谁优先？都没有时参数字典里会有 `max_new_tokens` 键吗？

> **参考答案**：`max_tokens`（外层）优先，会覆盖 `sampling.max_new_tokens`。两者都没有时，`_build_params` 会 `params.pop("max_new_tokens", None)` **删掉**这个键（而不是留一个 `None`），让下游 stage 回退到模型默认上限。

**练习 2**：为什么 `metadata["model"]` 用 `setdefault`，而 `metadata["output_modalities"]` 用直接赋值？

> **参考答案**：`model` 用 `setdefault` 是因为调用方可能已经在 `metadata` 里放了 `model`（比如上游已注入），不应被 `request.model` 覆盖；`output_modalities` 直接赋值是因为它是 `GenerateRequest` 的显式字段、代表本次请求的最新意图，应盖过 metadata 里可能残留的旧值。

---

### 4.3 结果聚合：把流式片段拼成完整回复

#### 4.3.1 概念说明

管线吐回的数据有两类来源（对应 `generate` 里的两个 builder）：

- **流式片段**（`StreamMessage`）：AR 生成过程中逐 token/逐音频块吐出的中间结果，经 `_default_stream_builder` 变成 `GenerateChunk`。
- **终态结果**（终态消息的 `.result`）：一个 stage 跑完后给出的「完整产物」，经 `_default_result_builder` 变成 `GenerateChunk`。终态结果可能是单字典，也可能是**多 terminal 合并字典**（如 `{"decode": {...}, "code2wav": {...}}`）。

`GenerateChunk`（[types.py:126](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/types.py#L126)）是这两类来源的**统一容器**：它同时装着 `text`、`token_ids`、`audio_data`、`sample_rate`、`finish_reason`、`usage`、`modality` 等字段。聚合的本质就是：把一串 `GenerateChunk` 里「同质」的字段分别合并——文本拼接、音频按轴拼接、`finish_reason`/`usage` 取最后一个非空值。

聚合只发生在**非流式**路径（`completion`/`speech`）。流式路径（`completion_stream`）不聚合，逐片透传。

#### 4.3.2 核心流程

`completion` 的聚合循环（伪代码）：

```
text_parts, audio_chunks = [], []
sample_rate = finish_reason = usage = ... = None
last_chunk = None

async for chunk in generate(request, request_id=request_id):
    last_chunk = chunk
    if chunk.text:            text_parts.append(chunk.text)        # 文本：追加
    if chunk.audio_data is not None:
        audio_chunks.append(chunk.audio_data)                     # 音频：收集
    if chunk.sample_rate:     sample_rate   = chunk.sample_rate   # 标量：取最后非空
    if chunk.finish_reason:   finish_reason = chunk.finish_reason
    if chunk.usage:           usage         = chunk.usage
    ...

if last_chunk is None:
    raise ClientError("No response from pipeline")                # 一个都没有→错误

full_text = "".join(text_parts)                                   # 文本拼成一句

if audio_chunks:
    if len(audio_chunks) == 1:
        combined = audio_chunks[0]
    else:
        arrays = [to_numpy(c) for c in audio_chunks]              # 统一转 float32 数组
        axis = -1 if arrays[0].ndim > 1 else 0                    # 多通道沿 -1，单通道沿 0
        combined = np.concatenate(arrays, axis=axis)
    audio_b64 = audio_to_base64(combined, sample_rate=..., output_format=audio_format)
    audio = CompletionAudio(id=f"audio-{request_id}", data=audio_b64, transcript=full_text or None)

return CompletionResult(text=full_text, audio=audio, finish_reason=finish_reason or "stop", ...)
```

三个要点：
- **文本用 `str` 拼接**（`"".join`），保留生成顺序。
- **音频用 `np.concatenate` 沿时间轴拼接**，且先经 `to_numpy` 统一成 float32——因为不同 stage 吐的可能是 numpy 数组、torch 张量或原始字节。
- **标量字段（`sample_rate`/`finish_reason`/`usage`）取「最后一个非空」**，符合「终态覆盖中间态」的直觉。

#### 4.3.3 源码精读

[sglang_omni/client/client.py:90-119](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L90-L119) —— `completion` 的聚合循环主体：声明各容器，遍历 `generate`，按字段类型分别累积；`last_chunk is None` 时抛 `ClientError("No response from pipeline")`。注意 `output_token_logprobs` 用了 `saw_output_token_logprobs` 标志位配合 `extend`——只有真的出现过 logprobs 才会在结果里带上（区分「没有」和「空列表」）。

[sglang_omni/client/client.py:121-153](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L121-L153) —— 聚合收尾：`full_text = "".join(text_parts)`；音频按 chunk 数量分单/多处理，多块走 `np.concatenate`（轴选择 `-1 if ndim>1 else 0`）；`audio_to_base64` 编码后包成 `CompletionAudio`（`id` 用 `f"audio-{request_id}"`，`transcript` 复用 `full_text`）；最终组装 `CompletionResult`，`finish_reason` 兜底为 `"stop"`。

再看「终态结果如何变成 `GenerateChunk`」——这是聚合的输入端：

[sglang_omni/client/client.py:452-524](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L452-L524) —— `_default_result_builder`：处理三种结果形态——已是 `GenerateChunk`（直接复用）、`dict`、`str`。`dict` 分支里最关键的是**多 terminal 合并**逻辑（[L461-L488](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L461-L488)）：当结果同时含 `decode`（文本终态）和 `code2wav`/`talker`/`talker_stream`（音频终态）时，从 `decode` 取文本/`finish_reason`/`usage`，从音频终态取音频数据，合成一条既有文本又有音频的 `GenerateChunk`。这正是 u2-l1 说的「一条管线多个 terminal，由上层合并」在 Client 层的具体落点。

[sglang_omni/client/client.py:526-587](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L526-L587) —— `_default_stream_builder`：把 `StreamMessage` 翻译成 `GenerateChunk`，逻辑与 result_builder 类似但更简单（流片段通常只带增量文本或单个音频块），并会从消息头补 `stage_name/stage_id/modality`。

最后看聚合产出的两个返回类型：

[sglang_omni/client/types.py:196-220](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/types.py#L196-L220) —— `CompletionResult`（非流式返回：`text` + 可选 `audio`(CompletionAudio) + `finish_reason` + `usage` + `output_token_logprobs` + `omni_rollout` + `weight_version`）与 `CompletionStreamChunk`（流式返回：单片段，`text` + 可选 `audio_b64` + `modality` + `finish_reason` + `usage` + `stage_name`）。注意非流式的音频字段叫 `audio.data`（base64 字符串），流式的叫 `audio_b64`——形状不同，因为 HTTP 层会把它们塞进不同的 OpenAI 响应结构。

#### 4.3.4 代码实践

**实践目标**：用一个**假 Coordinator**（不连真模型）驱动 `Client.completion`，观察聚合如何把多个伪造片段拼成一条结果。

**操作步骤**：

1. 阅读上面引用的 `completion` 与 `_default_result_builder` 源码。
2. 构造一个最小 fake coordinator：它实现 `async submit(self, request_id, request)`，返回一个多 terminal 合并字典 `{"decode": {"text": "你好", "finish_reason": "stop", "usage": {"prompt_tokens": 3, "completion_tokens": 2}}, "code2wav": {"audio_data": np.zeros(1000, dtype="float32"), "sample_rate": 24000}}`。
3. 写一段示例代码（**示例代码，非项目原有**）：

   ```python
   import asyncio, numpy as np
   from sglang_omni.client import Client, GenerateRequest
   from dataclasses import dataclass

   @dataclass
   class FakeCoord:
       async def submit(self, request_id, request):
           return {"decode": {"text": "你好", "finish_reason": "stop"},
                   "code2wav": {"audio_data": np.zeros(1000, dtype="float32"),
                                "sample_rate": 24000}}

   async def main():
       c = Client(FakeCoord())
       req = GenerateRequest(prompt="hi", stream=False)
       res = await c.completion(req, request_id="r1")
       print(res.text, res.audio is not None, res.finish_reason)

   asyncio.run(main())
   ```

4. 追踪控制流：`completion → generate(stream=False) → coordinator.submit → _default_result_builder`（走多 terminal 合并分支）→ 回到 `completion` 聚合（只有 1 个 chunk）。

**需要观察的现象**：`res.text == "你好"`、`res.audio is not None`（`data` 是一段 base64 WAV）、`res.finish_reason == "stop"`。这证明 `_default_result_builder` 把 `decode`+`code2wav` 两个 terminal 合成了一条带文本+音频的 chunk，`completion` 再把它聚合成 `CompletionResult`。

**预期结果**：打印 `你好 True stop`。若你的环境尚未装好 `sglang_omni`（见 u1-l2），可改为纯纸面推演——按 4.3.2 的伪代码逐步代入上面的字典，结论一致。**待本地验证**：实际 `np`/`Client` 的 import 与 `asyncio.run` 在你的 venv 里是否可用。

#### 4.3.5 小练习与答案

**练习 1**：如果管线一个 chunk 都没吐（比如入口 stage 直接失败），`completion` 会怎样？

> **参考答案**：循环结束时 `last_chunk is None`，`completion` 抛 `ClientError("No response from pipeline")`。注意这是「完全无输出」才抛；只要吐了哪怕一个空 chunk，`last_chunk` 就非 None，会正常返回（文本可能为空、`finish_reason` 兜底 `"stop"`）。

**练习 2**：多块音频为什么用 `np.concatenate` 而不是直接 `bytes` 拼接？轴 `axis = -1 if ndim>1 else 0` 在解决什么问题？

> **参考答案**：因为不同 stage 吐的音频可能是 numpy 数组、torch 张量或字节，必须先 `to_numpy` 统一成 float32 数组才能拼接。`-1 if ndim>1 else 0` 是为了兼容单声道（1 维，沿轴 0 即时间轴）和多声道（2 维，`[channel, time]`，沿时间轴 -1）两种布局——单声道沿 0、多声道沿 -1 都正好是「时间方向」。

---

### 4.4 音频编码：numpy 张量变 base64/bytes

#### 4.4.1 概念说明

管线内部到处用 numpy 数组或 torch 张量表示音频（一段浮点波形），但 HTTP 响应是 JSON/SSE 文本流——不能直接塞原始浮点数组。Client 层的最后一公里就是**音频编码**：把 `audio_data`（可能是 ndarray/Tensor/list/bytes）转成上层能用的形态：

- 对**聊天/非流式**（`completion`）：要嵌进 JSON，所以编码成 **base64 字符串**（`audio_to_base64`），默认 WAV。
- 对**流式**（`completion_stream`）：每个音频片段也 base64，但逐片编码逐片吐。
- 对**TTS**（`speech`）：要直接当 HTTP body 返回，所以编码成**原始字节**（`encode_audio`），格式可由 `response_format` 指定（wav/mp3/flac/opus/aac/pcm）。

这套编码全部住在 `sglang_omni/client/audio.py`，被 `client.py` 复用。它的两个设计要点：
- **格式回退（fallback）**：若请求 mp3 但环境没装 PyAV/pydub，**自动回退到 WAV**（`allow_format_fallback=True`），保证「总能吐出能播的音频」而不是报错。
- **统一入口 `to_numpy`**：不管上游给的是 ndarray、Tensor、list 还是 16-bit PCM 字节，都先归一成 float32 数组再编码。

#### 4.4.2 核心流程

音频编码的三条路径与共享前置：

```
audio_data (ndarray / Tensor / list / bytes)
   │
   ▼
to_numpy(audio)                       # 统一成 float32 ndarray
   │
   ├─ completion  → audio_to_base64(arr, sample_rate, output_format="wav")
   │                   └─ encode_audio(...) → bytes → base64.b64encode → str
   │
   ├─ completion_stream → 同上，但逐片段编码
   │
   └─ speech → encode_audio(arr, response_format, speed, allow_format_fallback)
                   ├─ wav  → encode_wav (16-bit PCM + RIFF 头)
                   ├─ pcm  → encode_pcm (裸 16-bit)
                   ├─ mp3/aac/opus → _encode_with_pyav (PyAV) → 失败回退 pydub → 再失败回退 WAV
                   └─ flac → soundfile → 失败回退 WAV
                   返回 (bytes, mime_type)
```

WAV 编码的数学很简单：浮点波形 `x ∈ [-1,1]` 量化成 16-bit 整数 `q = round(x * 32767)`，再按 RIFF/WAVE 容器封装。采样率 `sample_rate` 决定字节率：

\[
\text{byte\_rate} = \text{sample\_rate} \times \text{num\_channels} \times \text{bits\_per\_sample} / 8
\]

默认采样率 `DEFAULT_SAMPLE_RATE = 24000`（24 kHz，多数 TTS/omni 模型的输出率）。

#### 4.4.3 源码精读

[sglang_omni/client/audio.py:110-135](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/audio.py#L110-L135) —— `to_numpy`：按类型分派——ndarray 直接 `astype(float32)`；有 `cpu/numpy` 属性的当 torch Tensor（`detach().cpu().float().numpy()`）；list/tuple 转 `np.array(float32)`；bytes 当 16-bit PCM（`np.frombuffer("<i2")` 再除以 32768 归一化）。**这是 Client 能兼容多种音频表示的根基**。

[sglang_omni/client/audio.py:162-203](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/audio.py#L162-L203) —— `encode_wav`：`np.clip(-1,1)` 防越界 → `(x*32767).astype(int16)` 量化 → 按 RIFF/fmt/data chunk 手写二进制头（`struct.pack`）。这是默认且最稳的编码路径。

[sglang_omni/client/audio.py:305-417](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/audio.py#L305-L417) —— `encode_audio`：分发总入口。先 `to_numpy` + 维度规整（多通道降混/转置），可选 `apply_speed` 变速；再按 `fmt` 分派 wav/pcm/mp3/aac/opus/flac；mp3/aac/opus 走 PyAV，**任何异常都按 `allow_format_fallback` 决定回退到 WAV 还是抛错**（[L364-L395](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/audio.py#L364-L395)）。返回 `(bytes, mime_type)`。

[sglang_omni/client/audio.py:420-434](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/audio.py#L420-L434) —— `audio_to_base64`：`encode_audio` 拿到 bytes 后 `base64.b64encode(...).decode("ascii")`，返回可嵌 JSON 的字符串。

回到 `client.py` 看它如何调这些工具：

[sglang_omni/client/client.py:131-140](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L131-L140) —— `completion` 里音频编码：拼接好的 `combined` 经 `audio_to_base64`（默认 WAV）后包成 `CompletionAudio`。

[sglang_omni/client/client.py:172-178](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L172-L178) —— `completion_stream` 里音频编码：仅当 `chunk.modality == "audio"` 时对该片段 base64。

[sglang_omni/client/client.py:232-258](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L232-L258) —— `speech` 里音频编码：用 `asyncio.to_thread(encode_audio, ...)` 在线程池跑编码（避免阻塞事件循环），再用返回的 MIME 反查 `FORMAT_MIME_TYPES` 得到实际格式（`encode_audio` 可能回退到 WAV，所以不能直接信 `response_format`）。

#### 4.4.4 代码实践

**实践目标**：脱离管线，直接用 `audio.py` 验证「同一小段波形在不同格式下的输出差异」，建立对编码与回退的直觉。

**操作步骤**：

1. 阅读上面引用的 `to_numpy`、`encode_wav`、`encode_audio`、`audio_to_base64`。
2. 写一段示例代码（**示例代码，非项目原有**）：

   ```python
   import numpy as np
   from sglang_omni.client.audio import encode_audio, audio_to_base64, FORMAT_MIME_TYPES

   sr = 24000
   t = np.linspace(0, 0.05, int(sr * 0.05), dtype=np.float32)  # 50ms
   wave = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

   wav_bytes, wav_mime = encode_audio(wave, response_format="wav", sample_rate=sr)
   b64 = audio_to_base64(wave, sample_rate=sr, output_format="wav")
   print("wav bytes:", len(wav_bytes), wav_mime)
   print("b64 head:", b64[:16], "len:", len(b64))
   ```

3. 把 `response_format` 改成 `"mp3"`，观察在没装 PyAV/pydub 时是否回退到 WAV（看返回的 mime 是不是 `audio/wav`）。

**需要观察的现象**：WAV 字节长度应略大于 `50ms * 24000 * 2字节 ≈ 2400` 字节（多出 44 字节 RIFF 头）；base64 字符串长度约为字节数的 4/3；mp3 在无编码器时 mime 回退为 `audio/wav`。

**预期结果**：`wav bytes` 长度在 2400 附近、mime 为 `audio/wav`；`b64` 是合法 base64（只含 `A-Za-z0-9+/=`）。若环境无 PyAV，mp3 请求会回退。**待本地验证**：你的 venv 是否装了 `av`/`soundfile`，决定 mp3/flac 是否真能编码。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `speech` 用 `asyncio.to_thread(encode_audio, ...)` 而 `completion` 直接调 `audio_to_base64`？

> **参考答案**：mp3/aac/opus/flac 等压缩编码是 CPU 密集且可能阻塞的（要调 PyAV/ffmpeg），`speech` 支持这些格式，所以放线程池避免阻塞事件循环；`completion` 默认只走 WAV（`audio_to_base64` 内部 `encode_audio(response_format="wav")` → `encode_wav`，纯 numpy/struct，很快），无需切线程。

**练习 2**：`speech` 最后要从 MIME 反推 `actual_format`，为什么不直接用调用者传的 `response_format`？

> **参考答案**：因为 `encode_audio` 在编码器不可用或失败时会按 `allow_format_fallback` **回退到 WAV**，实际产出的 MIME 可能是 `audio/wav` 而非请求的 `audio/mpeg`。若不反推，响应头里声明的格式会与真实字节不符，客户端解码会失败。所以用 `FORMAT_MIME_TYPES` 反查 MIME 得到「真实格式」。

---

## 5. 综合实践

**任务**：用纸面推演 +（可选）fake coordinator，完整复现一次「带文本+语音双 terminal 的非流式 chat 请求」在 Client 层的全程，把四个最小模块串起来。

**背景**：Qwen3-Omni 这类 omni 模型，一条请求会同时产生文本（`decode` terminal）和语音（`code2wav` terminal）。你要追踪从 `GenerateRequest` 进来、到 `CompletionResult` 出去的全链路。

**步骤**：

1. **构造请求**（模块 4.2）：写一个 `GenerateRequest(prompt="用一句话介绍自己", output_modalities=["text","audio"], stream=False)`。推演 `_build_omni_request` 产出的 `OmniRequest` 三段：`inputs`（字符串）、`params`（含 `stream=False`、`max_new_tokens` 或缺省）、`metadata`（含 `output_modalities`）。
2. **提交并收终态**（模块 4.1）：fake coordinator 的 `submit` 返回多 terminal 字典 `{"decode": {"text":"我是Qwen","finish_reason":"stop","usage":{"completion_tokens":4}}, "code2wav": {"audio_data": np.zeros(2000,dtype="float32"), "sample_rate":24000}}`。推演 `generate(stream=False)` 调 `coordinator.submit` 得到该 dict。
3. **builder 合并 terminal**（模块 4.3）：推演 `_default_result_builder` 走「`decode` + `code2wav` 合并」分支，产出一个**既有 `text="我是Qwen"` 又有 `audio_data`** 的 `GenerateChunk`。
4. **聚合**（模块 4.3）：推演 `completion` 循环只有 1 个 chunk，`full_text="我是Qwen"`，`audio_chunks` 有 1 块。
5. **音频编码**（模块 4.4）：推演该音频块经 `audio_to_base64`（默认 WAV）变成 base64 字符串，包进 `CompletionAudio(id="audio-<rid>")`。
6. **产出**：最终 `CompletionResult(text="我是Qwen", audio=CompletionAudio(...), finish_reason="stop", usage=UsageInfo(completion_tokens=4))`。

**验收**：你能用一句话说清「为什么文本来自 `decode`、音频来自 `code2wav`，却能在 Client 层合成同一条 `CompletionResult`」——答案就在 `_default_result_builder` 的多 terminal 合并分支（[client.py:461-488](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/client/client.py#L461-L488)）。

**进阶（可选）**：把 fake coordinator 改成 `stream` 版——让它先 `yield` 两个 `StreamMessage`（一个文本增量、一个音频增量），再 `yield` 一个终态 `CompleteMessage`。推演 `completion_stream` 如何把这两个增量分别包成 `CompletionStreamChunk`（一个 `modality="text"`、一个 `modality="audio"`）逐片吐出，对照 u2-l2 的 SSE 四语义。

---

## 6. 本讲小结

- **Client 是 Coordinator 上的薄门面**：自己不碰 GPU，只做「请求翻译 + 结果聚合」，所有真正算东西的调用都汇到底层 `generate` → `coordinator.submit/stream`。
- **`request_id` 由 `generate` 兜底生成**（`uuid.uuid4()`），但允许 HTTP 层传入，是贯穿 Coordinator/Stage 的全局请求标识。
- **请求翻译走三段式**：`_build_omni_request` = `_extract_inputs`（三选一 + 多模态落 `inputs`）+ `_build_params`（`max_tokens` 覆盖 `max_new_tokens`、追加 stream/stage/extra）+ `metadata`（补 `model`/`output_modalities`），产出协议无关的 `OmniRequest`。
- **聚合只发生在非流式路径**：文本 `"".join`、音频 `np.concatenate` 沿时间轴、标量取最后非空；多 terminal（`decode`+`code2wav`）由 `_default_result_builder` 合并成单条 chunk。
- **音频编码住在 `audio.py`**：`to_numpy` 统一表示、`encode_audio` 按 wav/pcm/mp3/aac/opus/flac 分派并带 WAV 回退、`audio_to_base64` 嵌 JSON；`speech` 用 `asyncio.to_thread` 跑压缩编码、并从 MIME 反推真实格式。
- **流式与非流式共用 `generate` 底座**：`completion` 累积、`completion_stream` 透传、`speech` 强制非流式——同一个异步生成器，三种消费姿势。

## 7. 下一步学习建议

Client 把请求交给了 Coordinator，并把 Coordinator 的碎片拼成了结果——但「Coordinator 如何把请求路由进入口 stage、如何等齐多个 terminal、如何广播 abort」我们只用了它的接口，还没看内部。下一篇 [u2-l4 Coordinator 协调器](u2-l4-coordinator.md) 就下钻 Coordinator：

- 看 `submit`/`stream` 如何把 `OmniRequest` 经控制平面送进入口 stage，以及 `_completion_futures`/`_stream_queues` 两个字典如何让「提交」与「终态回填」异步对接。
- 理解多 terminal 的 `_expected_terminal_stages` 机制——它正是本讲 `_default_result_builder` 处理「`decode`+`code2wav` 合并」的上游原因。
- 看 `abort` 如何经 PUB/SUB 广播到所有 stage（预告 u3-l2 控制平面）。

如果更想先横向把「请求翻译」这条线补全，可回头对照 [u2-l2](u2-l2-openai-api-layer.md) 的 `_build_chat_generate_request`（HTTP 层翻译）与本讲的 `_build_omni_request`（Client 层翻译），体会「两步接力」的设计。后续 u3 单元会进入 Stage/Scheduler，看 `OmniRequest` 被 Coordinator 派发后，stage 内部如何真正消费它。

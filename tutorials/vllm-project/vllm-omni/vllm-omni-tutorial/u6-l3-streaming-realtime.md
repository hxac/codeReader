# 流式输出与实时/全双工

## 1. 本讲目标

上一讲（u6-l2）我们看了「请求—响应」式的多模态端点：客户端发一个完整的 `chat/completions` 请求，服务端算完后一次性（或 token 级流式）回文本/图像/语音。但现实里很多模态是**连续不断**的——摄像头一帧帧推视频、麦克风一段段收音频、全双工对话里说话和听话同时进行。这类场景下，「请求—响应」模型不够用，需要**长连接 WebSocket + 流式投递**。

本讲聚焦 vLLM-Omni 在线服务层的三条 WebSocket 流式通道。学完后你应当能：

1. 说清「视频流式理解 `/v1/video/chat/stream`」「实时音频 `/v1/realtime`」「全双工 `/v1/duplex`」三条通道各自解决什么问题、它们的协议长什么样。
2. 读懂 `OmniStreamingVideoHandler` 的会话循环：客户端视频帧如何被缓冲、过滤（EVS）、预热，又如何在收到 `video.query` 时被拼成一次多阶段推理请求。
3. 读懂 `RealtimeConnection` 如何把模型逐步产出的音频张量**转成增量 PCM 片段**回传客户端，并能画出「张量 → numpy → PCM16 base64」的转换点。
4. 理解 `should_enable_duplex_endpoint` 这个**纯函数开关**如何依据 `session_mode == "duplex"` 决定是否装配全双工端点。

## 2. 前置知识

- **WebSocket**：一种在单条 TCP 连接上进行「全双工」双向通信的协议。HTTP 是「一问一答」，WebSocket 是「连上之后双方都能随时主动推消息」，非常适合流式音视频。
- **base64**：把二进制字节（如 JPEG 帧、PCM 音频）编码成 ASCII 字符串，方便塞进 JSON 文本消息里通过 WebSocket 传输。
- **PCM16 音频**：每个采样点用一个 16 位有符号整数（int16）表示、范围 `[-32768, 32767]` 的原始波形格式；深度学习模型内部则常用 float32、范围 `[-1.0, 1.0]` 的波形，两者之间需要做缩放转换。
- **增量（delta）输出**：流式场景下，服务端每次只发「自上次以来新增的一小段」，而不是每次都重发全部已生成内容。这是降低延迟的关键，但需要正确追踪「上次发到哪里」。
- **多阶段流水线**：回顾 u3-l1，一次 omni 请求会被拆成 Thinker → Talker → Code2wav 等多个 stage 串联执行。本讲看到的「文本先出、音频后出」正是不同 stage 产出的结果。
- **EVS（Efficient Video Sampling）**：一种在帧到达视觉编码器**之前**做轻量像素相似度过滤、丢弃近似重复帧的技术，能成倍减少视觉编码算力。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [`vllm_omni/entrypoints/openai/video_stream_base.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/video_stream_base.py) | 视频流式会话的**通用基类** `OmniStreamingVideoHandler`：会话循环、帧/音频缓冲、EVS 过滤、预热、中断、引擎 `generate()` 流式消费。本讲大部分逻辑都在这里。 |
| [`vllm_omni/entrypoints/openai/serving_video_stream.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video_stream.py) | Qwen3-Omni 专用子类 `QwenOmniStreamingVideoHandler`：决定何时触发一轮对话、如何把帧/音频拼成 OpenAI 风格 messages。 |
| [`vllm_omni/entrypoints/openai/realtime_connection.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/realtime_connection.py) | 实时音频连接 `RealtimeConnection`：继承上游 vLLM 的 speech-to-text 连接，**自定义生成输出处理**，把模型音频张量编成增量 PCM 回传。 |
| [`vllm_omni/entrypoints/openai/duplex_capability.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/duplex_capability.py) | 全双工端点**开关判定函数** `should_enable_duplex_endpoint`：只有显式声明 `session_mode == "duplex"` 的部署才装配全双工路由。 |
| [`vllm_omni/entrypoints/openai/api_server.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/api_server.py) | 把上述处理器挂到三条 WebSocket 路由，并在初始化阶段依据开关决定是否创建全双工处理器。 |
| [`docs/serving/video_stream_api.md`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/serving/video_stream_api.md) | 视频流式输入 API 的官方协议说明与字段表。 |

**三条 WebSocket 通道总览**（先建立全局，下面三节分别精读）：

| 通道 | 路由 | 处理器 | 数据流向 | 适用场景 |
|------|------|--------|----------|----------|
| 视频流式理解 | `/v1/video/chat/stream` | `QwenOmniStreamingVideoHandler` | 视频帧 + 可选音频 **进** → 文本 + 音频 **出** | 边推视频边提问（Qwen3-Omni） |
| 实时音频 | `/v1/realtime` | `RealtimeConnection` | 音频 **进**（上游 ASR）→ 文本增量 + 音频增量 **出** | 实时语音对话（半双工：说完再回） |
| 全双工 | `/v1/duplex`、`/v1/realtime?duplex=1` | `OmniDuplexSessionHandler`（实验性） | 听与说**可重叠** | 全双工语音（打断、叠音） |

> 注意：本讲只精读前两条通道的实现与第三条的**开关判定**。`OmniDuplexSessionHandler` 本身位于 `experimental/fullduplex/`，属于实验特性，其完整设计留给 u9-l4（实验特性）讲义展开。

## 4. 核心概念与源码讲解

### 4.1 视频流式理解：StreamingVideoHandler 的会话与帧处理

#### 4.1.1 概念说明

「视频流式理解」要解决的问题是：客户端有一路**持续到达**的视频帧（如摄像头、屏幕、视频文件逐帧），希望服务端**边收边缓冲**，等用户提问时，把缓冲里的若干帧连同问题一起送进多阶段模型（Thinker → Talker → Code2wav），再**流式**地把回答文本和语音回传。

这和「请求—响应」的根本区别有三点：

1. **连接是长期的**：一条 WebSocket 连接对应一个**会话（session）**，会话内可以有多次「提问—回答」轮次。
2. **输入是增量到达的**：视频帧不是一次性给齐，而是一帧帧推；服务端要在内存里维护一个**帧缓冲区（frame buffer）**。
3. **触发是显式的**：帧到达不等于要推理。Qwen3-Omni 子类采用「手动触发」——只有收到 `video.query` 消息才真正发起一轮推理。

vLLM-Omni 把「与具体模型无关的会话/缓冲/中断/引擎对接」逻辑抽到基类 `OmniStreamingVideoHandler`，把「何时触发、prompt 长什么样」留给子类用钩子（hooks）定制。这是一种典型的**模板方法（Template Method）+ 钩子**设计。

#### 4.1.2 核心流程

一个视频流式会话的生命周期：

```
客户端                          服务端 (OmniStreamingVideoHandler)
  │
  │── session.config ──────────► _receive_config: 校验出 StreamingVideoSessionConfig
  │                               (num_frames / max_frames / EVS 阈值 / modalities ...)
  │
  │── video.frame (b64) ───────► _processor[video.frame]:
  │      ├ base64 解码 → PIL
  │      ├ EVS 过滤 (FrameSimilarityFilter.should_retain) ── 近似重复则丢弃
  │      ├ 缓冲超限 → 淘汰最旧帧 (max_frames)
  │      ├ 入 frame_buffer + frame_metadata
  │      ├ 发 video.frame.ack
  │      └ 后台 prewarm: asyncio.to_thread 解码 PIL + md5 uuid (供 mm_cache 去重)
  │      ... (重复多帧) ...
  │── audio.chunk (b64 PCM) ──► 累积进 audio_buffer (溢出则清空)
  │
  │── video.query {text} ─────► _start_query_turn:
  │                               ├ 取消正在进行的上一轮 (软中断)
  │                               ├ 快照 frame_buffer / audio_buffer / pil_cache
  │                               └ _process_query_engine:
  │                                   ├ build_engine_prompt → OpenAI messages
  │                                   ├ ChatCompletionRequest(stream=True, modalities)
  │                                   ├ _preprocess_to_engine_prompt
  │                                   ├ 发 response.start
  │                                   └ engine.generate(...) 流式迭代:
  │                                       text 输出 → response.text.delta
  │                                       audio 输出 → response.audio.delta (增量)
  │                                   ├ 发 response.text.done / response.audio.done
  │                                   └ on_turn_complete: 把这轮写进 message_history
  │── video.done ─────────────► 发 session.done，结束会话
```

几个关键设计：

- **读/处理分离**：一个 `_reader` 协程专门收消息塞进 `msg_queue`，一个 `_processor` 协程串行处理。这样收消息的 I/O 不会阻塞在耗时的推理上，同时保证**单会话内推理串行**（一次只跑一轮）。
- **帧采样**：缓冲里可能攒了几十帧，但模型一次只取 `num_frames` 帧（默认 4）。超过时按**等距步长**采样（保留首帧与末帧），避免上下文过长。
- **预热（prewarm）**：收到帧后立刻在后台线程把 base64 解码成 PIL 对象并算 md5 uuid，这样真正提问时 `chat_template` 渲染可以跳过昂贵的 `Image.open`，并让多模态缓存按 uuid 命中。

#### 4.1.3 源码精读

**会话主循环**定义在基类，子类只覆盖钩子。会话入口 [`handle_session`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/video_stream_base.py#L210-L235) 负责 accept、收 config、初始化各缓冲区：

```python
async def handle_session(self, websocket: WebSocket) -> None:
    await websocket.accept()
    config = await self._receive_config(websocket)
    ...
    frame_buffer: list[str] = []          # base64 JPEG 帧
    frame_pil_cache: dict[...] = {}       # b64 -> (PIL.Image, uuid) 预热缓存
    frame_filter = FrameSimilarityFilter(...) if config.enable_frame_filter else None
    audio_buffer = bytearray()            # 原始 PCM16 16kHz mono
    message_history: Any = self.create_message_history(config)
    ...
    msg_queue: asyncio.Queue[...] = asyncio.Queue(maxsize=_MAX_MSG_QUEUE)
```

`_reader` 把 WebSocket 消息搬进 `msg_queue`，并打上接收时间戳（`video.frame` 单独记 `_receiver_received_ts_ms`，用于端到端时延统计）。

**`video.frame` 处理**（节选自 [`video_stream_base.py:L372-L445`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/video_stream_base.py#L372-L445)）展示了「过滤 → 淘汰 → 入缓冲 → 确认 → 预热」五步：

```python
elif msg_type == "video.frame":
    frame_data = msg.get("data", "")
    ...
    raw_bytes = base64.b64decode(frame_data, validate=True)
    if frame_filter is not None and not frame_filter.should_retain(raw_bytes):
        await self._send_frame_ack(websocket, msg, accepted=False, reason="filtered")
        continue                      # EVS 判定近似重复，丢弃
    if len(frame_buffer) >= config.max_frames:
        dropped = frame_buffer.pop(0) # 淘汰最旧
        frame_pil_cache.pop(dropped, None)
    frame_buffer.append(frame_data)
    ...
    # 后台预热：解码 PIL + md5 uuid，供查询时复用与 mm_cache 去重
    if frame_data not in frame_pil_cache:
        mm_uuid = hashlib.md5(raw_bytes, usedforsecurity=False).hexdigest()
        task = asyncio.create_task(_prewarm(frame_data, raw_bytes, mm_uuid))
```

**触发与推理**：收到 `video.query` 调 [`_start_query_turn`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/video_stream_base.py#L294-L344)，它会先取消上一轮、快照缓冲、生成 `request_id = f"video-{uuid...}"`，然后调度 `_process_query`。真正的引擎流式消费在 [`_process_query_engine`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/video_stream_base.py#L609-L695)：

```python
result_gen = self._engine_client.generate(
    prompt=engine_prompt,
    request_id=request_id,
    output_modalities=config.modalities,
)
async for output in result_gen:
    if interrupt_event.is_set():      # 软中断：排空但不发送
        interrupted = True; continue
    ...
    out_type = getattr(output, "final_output_type", "text")
    if out_type == "audio":
        ...                           # 取音频增量，发 response.audio.delta
    else:
        delta_text, previous_text = self._extract_text_delta(output, previous_text)
        if streaming:
            await websocket.send_json({"type": "response.text.delta", "delta": delta_text})
```

注意 `final_output_type` 字段：同一轮迭代里，**文本 stage（Talker）的输出**与**音频 stage（Code2wav）的输出**会先后到达，由这个字段区分路由——这与 u3-l1 讲的多阶段「文本先出、音频后出」一致。

**Qwen3-Omni 子类的两个关键定制**。第一，手动触发（ [`should_trigger_turn` 始终返回 False](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video_stream.py#L52-L53)），即收到帧不自动提问，必须等 `video.query`。第二，`build_engine_prompt` 把帧/音频/文本拼成 messages（ [`serving_video_stream.py:L55-L122`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video_stream.py#L55-L122)）：

```python
def build_engine_prompt(self, config, frame_buffer, audio_buffer, ...):
    n_buf = len(frame_buffer)
    if n_buf <= config.num_frames:
        frames = list(frame_buffer)
    else:                             # 等距采样，保留首末帧
        stride = max(1, n_buf // config.num_frames)
        idx = [i * stride for i in range(config.num_frames - 1)] + [n_buf - 1]
        frames = [frame_buffer[i] for i in idx]
    ...
    for frame_b64 in frames:
        cached = prewarmed.get(frame_b64)
        if cached is not None:        # 命中预热缓存：用 image_pil + uuid
            user_content.append({"type": "image_pil", "image_pil": pil, "uuid": pil_uuid})
        else:                         # 未命中：回退到 data URI
            user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"}})
    if len(audio_buffer) > 0:
        wav_b64 = self._pcm_to_wav_b64(bytes(audio_buffer))
        user_content.append({"type": "input_audio", "input_audio": {"data": wav_b64, "format": "wav"}})
    ...
```

工厂 [`create_streaming_video_handler`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video_stream.py#L136-L152) 今天固定返回 `QwenOmniStreamingVideoHandler`，注释里点明「未来可在此选择其他 pipeline」——为新增流式 pipeline 预留了扩展点。

#### 4.1.4 代码实践

**实践目标**：用官方示例客户端跑通一次「推帧 → 提问 → 收流式文本」，亲历会话协议。

**操作步骤**：

1. 启动 Qwen3-Omni 服务（命令见 [`docs/serving/video_stream_api.md` Quick Start](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/serving/video_stream_api.md#L9-L26)）：

   ```bash
   vllm serve Qwen/Qwen3-Omni-30B-A3B-Instruct \
       --deploy-config vllm_omni/deploy/qwen3_omni.yaml \
       --omni --port 8000 --trust-remote-code
   ```

2. 用合成帧（无需真实视频与 opencv）运行客户端：

   ```bash
   pip install websockets pillow
   python examples/online_serving/qwen3_omni/streaming_video_client.py \
       --url ws://localhost:8000/v1/video/chat/stream \
       --synthetic-frames 10 \
       --query "Describe what you see."
   ```

**需要观察的现象**：客户端先发 `session.config`，再连发 10 个 `video.frame`，然后发一个 `video.query`。服务端依次回 `response.start` → 多条 `response.text.delta` → `response.text.done`（`modalities` 含 `audio` 时还会有 `response.audio.delta`/`response.audio.done`）。

**预期结果**：终端逐字打印出模型的文本描述，最后打印完整文本。

> 若无可用 GPU/模型权重，此为「待本地验证」；可改为**源码阅读型实践**：在 `_process_query_engine` 的 `async for output in result_gen` 处加一行 `print(out_type)`，对照日志验证「text 输出先于 audio 输出」的多阶段顺序。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `session.config` 里的 `enable_frame_filter` 设为 `false`，会跳过哪一步？后果是什么？

> **答案**：跳过 [`frame_filter.should_retain`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/video_stream_base.py#L384-L394) 的 EVS 近似重复过滤。后果是静态/缓慢场景下的近似重复帧也会全部进入缓冲，导致视觉编码算力与 KV-cache 占用升高（ [`video_frame_filter.py` 文档注释](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/video_frame_filter.py#L5-L8) 称可减少 2–5 倍帧数）。

**练习 2**：为什么 `build_engine_prompt` 要区分「命中预热的 `image_pil`」和「未命中的 `image_url` data URI」两种写法？

> **答案**：命中预热时已经有解码好的 PIL 对象和 md5 uuid，用 `image_pil` 可以让 `chat_template` 渲染跳过昂贵的 `base64 + Image.open`，并让多模态缓存（mm_cache）按 uuid 命中相同帧；只有预热失败或未预热时才回退到重新传 base64 data URI。

### 4.2 实时音频：RealtimeConnection 的音频流编解码

#### 4.2.1 概念说明

`/v1/realtime` 是一条**实时音频**通道。它继承自上游 vLLM 的 speech-to-text 实时连接（`vllm.entrypoints.speech_to_text.realtime.connection.RealtimeConnection`），上游负责 WebSocket 生命周期、把客户端音频帧做 ASR（语音转 token）喂给引擎；omni 的子类 `RealtimeConnection` **只自定义「生成输出」的处理**——在文本转写增量之外，额外把模型产出的**音频张量**编成增量 PCM 回传客户端。

这里的核心工程难题是：**模型每一步产出的音频，到底是「累积的完整波形」还是「本步新增的小片段」？两种引擎路径都存在**。如果简单地把每步结果都发出去，客户端会听到重复/叠加的音频。`RealtimeConnection` 用一个「上次发到哪里」的参考数组 `_realtime_audio_ref`，**同时兼容两种语义**而不产生重复。这是本节最值得品味的设计。

> 概念澄清：「全双工」严格指「听与说同时进行、可打断、可叠音」，那是 4.3 节开关背后的实验性 `OmniDuplexSessionHandler`。`RealtimeConnection` 本身是「实时音频进、文本+音频增量出」的流式通道，方向是双向的，但收与发在时序上并非真正重叠。本讲的实践任务正是围绕它的双向数据流展开。

#### 4.2.2 核心流程

一次实时音频生成的数据流（聚焦 omni 自定义的输出侧）：

```
客户端音频帧 ──► (上游 ASR) ──► streaming_input_gen (prompt 异步生成器)
                                        │
                                        ▼
                          engine.generate(prompt=streaming_input_gen,
                                          request_id="rt-...",
                                          sampling_params_list=...)
                                        │  流式产出 OmniRequestOutput
                                        ▼
              ┌─────────────────────────┴──────────────────────────┐
              ▼ (stage_id==0, 文本)                        ▼ (音频输出)
   first_output.token_ids → input_stream           multimodal_output["audio"]
   first_output.text → TranscriptionDelta                   │ (torch 张量, GPU)
                                                            ▼
                                              _extract_audio_chunks:
                                                _tensor_to_numpy   ◄── 张量→numpy 转换点
                                                            │ (np.float32, 1D)
                                                            ▼
                                              _raw_waveform_to_deltas:
                                                用 _realtime_audio_ref 判定
                                                累积型 → 截取新尾巴
                                                增量型 → 整段发出
                                                            │ (list[np.float32])
                                                            ▼
                                              _pcm16_b64:
                                                clip(-1,1) → ×32767 → int16
                                                            │ ◄── numpy→PCM16 转换点
                                                            ▼
                                          response.audio.delta {audio:b64, format:"pcm16",
                                                                 sample_rate_hz}
                                                            │
                                                            ▼
                                          TranscriptionDone(usage) / response.audio.done
```

**张量与 numpy 的两个关键转换点**（实践任务要标注的）：

1. **张量 → numpy**：`_tensor_to_numpy` 把 GPU 上的 float32 张量 `detach().float().cpu().numpy()` 拉回 CPU 并展平。
2. **numpy → PCM16 字节**：`_pcm16_b64` 把 `[-1,1]` 的 float32 波形 clip 后乘以 32767 转成 int16，再 base64。

#### 4.2.3 源码精读

`RealtimeConnection` 是个轻量子类，[类定义](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/realtime_connection.py#L22-L32) 只是把 `engine` 强制成 `AsyncOmni` 并初始化音频参考数组：

```python
class RealtimeConnection(VllmRealtimeConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.engine = cast(AsyncOmni, self.serving.engine_client)
        self._realtime_audio_ref: np.ndarray | None = None
```

**转换点 1：张量 → numpy**。 [`_tensor_to_numpy`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/realtime_connection.py#L37-L52) 兼容三种输入（已是 ndarray、torch 张量、其他可转对象），统一成 1D float32：

```python
@staticmethod
def _tensor_to_numpy(value) -> np.ndarray | None:
    if isinstance(value, np.ndarray):
        arr = value
    elif hasattr(value, "detach"):
        arr = value.detach().float().cpu().numpy()   # ← GPU 张量拉回 CPU
    else:
        arr = np.asarray(value)
    if arr.ndim > 1:
        arr = arr.reshape(-1)                        # 展平成 1D
    return arr.astype(np.float32, copy=False)
```

**兼容两种音频语义的核心**。 [`_raw_waveform_to_deltas`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/realtime_connection.py#L63-L81) 用「前缀匹配」判定当前波形是历史波形的扩展（累积型）还是独立的新片段（增量型）：

```python
def _raw_waveform_to_deltas(self, arr: np.ndarray) -> list[np.ndarray]:
    if arr.size == 0:
        return []
    ref = self._realtime_audio_ref
    if ref is None:                       # 第一次：整段发出，记下参考
        self._realtime_audio_ref = arr.copy()
        return [arr]
    if self._numpy_audio_prefix_match(ref, arr):   # 累积型：当前是历史的扩展
        delta = arr[ref.shape[0]:]                 #   只取新增的尾巴
        self._realtime_audio_ref = arr.copy()
        return [delta] if delta.size > 0 else []
    # 增量型：当前是独立的新片段，拼接后整段发出
    self._realtime_audio_ref = np.concatenate([ref, arr])
    return [arr]
```

前缀匹配 [`_numpy_audio_prefix_match`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/realtime_connection.py#L54-L61) 用 `np.allclose`（`rtol=1e-3, atol=2e-4`）做**近似**比较，容忍浮点误差。

**抽取音频块**。 [`_extract_audio_chunks`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/realtime_connection.py#L83-L112) 从 `output.multimodal_output` 里导航到音频（键优先级 `audio` > `model_outputs`），解析采样率，对单个张量或张量列表的**最后一个**做转换：

```python
sr = mm.get("sr") or mm.get("sample_rate") or mm.get("audio_sample_rate") or 24000
key = "audio" if "audio" in mm else ("model_outputs" if "model_outputs" in mm else None)
raw_audio = mm.get(key)
if isinstance(raw_audio, (list, tuple)):
    arr = self._tensor_to_numpy(raw_audio[-1])     # 列表取最后一个（最新一步）
else:
    arr = self._tensor_to_numpy(raw_audio)
chunks.extend(self._raw_waveform_to_deltas(arr))
```

**转换点 2：numpy → PCM16**。 [`_pcm16_b64`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/realtime_connection.py#L114-L118)：

```python
@staticmethod
def _pcm16_b64(audio_f32: np.ndarray) -> str:
    clipped = np.clip(audio_f32, -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype(np.int16)
    return base64.b64encode(pcm16.tobytes()).decode("utf-8")
```

缩放公式即：

\[
\text{pcm16}[i] = \bigl\lfloor \operatorname{clip}(x_i,\,-1,\,1)\times 32767 \bigr\rfloor
\]

**生成主循环**。 [`_run_generation`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/realtime_connection.py#L120-L181) 把上述片段串起来。它把上游喂进来的 `streaming_input_gen`（一个异步生成器，作为可变长 prompt）直接传给 `engine.generate`，再边迭代边分拣文本与音频：

```python
result_gen = self.engine.generate(
    prompt=streaming_input_gen,
    request_id=request_id,
    sampling_params_list=sampling_params_list,
)
async for output in result_gen:
    stage_id = getattr(output, "stage_id", None)
    if stage_id == 0 and output.outputs:          # stage-0：文本/转写
        first_output = output.outputs[0]
        ...
        if delta_text:
            await self.send(TranscriptionDelta(delta=delta_text))
    audio_chunks, sample_rate = self._extract_audio_chunks(output)   # 音频
    for chunk in audio_chunks:
        sent_audio = True
        await self.send_json({"type": "response.audio.delta",
                              "audio": self._pcm16_b64(chunk),
                              "format": "pcm16", "sample_rate_hz": sample_rate})
```

循环结束后发 `TranscriptionDone`（含 token 用量）与 `response.audio.done`。注意 [`finally` 块](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/realtime_connection.py#L199-L217) 有一处重要的健壮性处理：**显式 `await result_gen.aclose()`**。注释引用 issue #4271 指出，若不主动关闭生成器，它的清理逻辑（取消 input-pump、引擎侧 abort）会延迟到事件循环 GC 时才跑，这段空窗期会让已断开的会话仍在各 stage 间空转。

#### 4.2.4 代码实践

**实践目标**（本讲指定实践）：阅读 `realtime_connection.py`，画出全双工/实时音频会话中「客户端音频帧 → 模型 → 服务端音频帧」的双向数据流，标注张量与 numpy 的转换点。

**操作步骤**（源码阅读型，无需运行）：

1. 打开 [`realtime_connection.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/realtime_connection.py)，沿 `_run_generation` 的调用链标注每一处数据形态变化。
2. 用如下表格记录（参考答案见下）：

   | 步骤 | 位置（行号） | 数据形态 | 方向 |
   |------|--------------|----------|------|
   | 客户端音频 | 上游 base | base64 PCM16 | 进 |
   | 喂给引擎 | L143 `engine.generate` | prompt 异步生成器 | 进 |
   | 模型产出音频 | L170 `_extract_audio_chunks` 入口 | `multimodal_output["audio"]` torch 张量（GPU） | 内部 |
   | **转换点①** | L44/L109 `_tensor_to_numpy` | torch → np.float32 1D | 内部 |
   | 去重/取增量 | L75-81 `_raw_waveform_to_deltas` | list[np.float32] | 内部 |
   | **转换点②** | L117 `_pcm16_b64` | np.float32 → int16 → base64 | 出 |
   | 回传客户端 | L174 `send_json response.audio.delta` | JSON `{audio:b64, format:pcm16}` | 出 |

**需要观察的现象**：在脑中/纸上走一遍，确认「张量只在 `_tensor_to_numpy` 处离开 GPU，PCM 字节只在 `_pcm16_b64` 处产生」——即**两个转换点是唯一的形态边界**。

**预期结果**：得到一张清晰标注两个转换点的双向数据流图（如 4.2.2 的流程图所示）。

> 若想动态验证（待本地验证）：在 `_tensor_to_numpy` 的 return 前打印 `arr.shape`，在 `_pcm16_b64` 入口打印 `audio_f32.shape`，对一段真实音频会话观察「每次 delta 的样本数随步数增长（累积型）或基本恒定（增量型）」，从而判断当前引擎走的是哪条路径。

#### 4.2.5 小练习与答案

**练习 1**：`_raw_waveform_to_deltas` 为什么要区分「累积型」和「增量型」两种音频？如果不区分、每次都直接发 `arr`，会出什么问题？

> **答案**：因为不同引擎路径语义不同——有的每步产出**到目前为止的完整波形**（累积型），有的产出**本步新增片段**（增量型）。若不区分都直接发：累积型会导致客户端把前缀重复播放（声音叠加/卡顿）；增量型若误判为累积型又会截断。用 `_realtime_audio_ref` 做前缀匹配，能自动识别并只发真正的新增部分。

**练习 2**：`_extract_audio_chunks` 里解析采样率时为何有 `sr = mm.get("sr") or mm.get("sample_rate") or ... or 24000` 这样一长串回退？

> **答案**：不同模型/路径把采样率放在 `multimodal_output` 字典里的键名不统一（`sr`/`sample_rate`/`audio_sample_rate`），有的甚至不提供。这串 `or` 是按优先级逐个尝试、最后兜底 24kHz（omni 音频输出的常见采样率），保证总能给出一个合理的整数值发给客户端。

### 4.3 全双工开关：should_enable_duplex_endpoint 判定

#### 4.3.1 概念说明

全双工语音（听与说重叠、可打断）是个**重特性**：它需要专门的会话状态机、专门的运行时适配器（见 u9-l4 的 `experimental/fullduplex`）。把它默认开给所有部署既不安全也不必要——绝大多数用户只想用普通的 chat/realtime。因此 vLLM-Omni 用一个**纯函数** `should_enable_duplex_endpoint` 当作「总开关」：只有部署**显式声明** `session_mode == "duplex"` 时，才在 `omni_init_app_state` 阶段创建全双工处理器 `OmniDuplexSessionHandler` 并挂到 `/v1/duplex`、`/v1/realtime?duplex=1`。

这是个很小的函数，但它体现了一个重要的工程原则：**危险/重特性的装配必须显式 opt-in，且判定逻辑要集中、可测、无副作用**。

#### 4.3.2 核心流程

判定流程：

```
should_enable_duplex_endpoint(stage_configs, config_path)
        │
        ├─ 遍历 stage_configs（list，每项是 dict 或对象）
        │     取 session_mode（dict.get / getattr 双适配）
        │     任一 stage 的 session_mode == "duplex"  →  return True
        │
        ├─ 若给了 config_path（YAML 文件）
        │     OmegaConf.load(config_path)
        │     顶层 session_mode == "duplex"           →  return True
        │     （失败只 warning，不抛异常）
        │
        └─ 其余                                            →  return False
```

调用点在 [`api_server.py` 的状态初始化](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/api_server.py#L1117-L1128)：

```python
state.openai_serving_duplex = None
if state.openai_serving_chat is not None and should_enable_duplex_endpoint(
    state.stage_configs,
    config_path=getattr(args, "stage_configs_path", None) or getattr(args, "deploy_config", None),
):
    from vllm_omni.experimental.fullduplex.openai.serving import OmniDuplexSessionHandler
    state.openai_serving_duplex = OmniDuplexSessionHandler(
        chat_service=state.openai_serving_chat,
        duplex_session_config=getattr(engine_client, "duplex_session_config", None),
        serving_runtime_adapter_path=getattr(engine_client, "duplex_serving_adapter_path", None),
    )
```

注意两个前提：① 必须先有 `openai_serving_chat`（全双工复用 chat 服务）；② 配置路径同时兼容旧 `stage_configs_path` 与新 `deploy_config`（与 u1-l5 讲过的「移除 legacy `--stage-configs-path`」演进一致，这里做了双兜底）。判定为真时才**延迟导入**实验性的 `OmniDuplexSessionHandler`，避免把实验代码拉进普通部署的 import 路径。

#### 4.3.3 源码精读

完整函数只有 20 余行（ [`duplex_capability.py:L9-L32`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/duplex_capability.py#L9-L32)）：

```python
def should_enable_duplex_endpoint(
    stage_configs: list | None,
    *,
    config_path: str | None = None,
) -> bool:
    """Enable duplex routes only for deployments that explicitly opt in."""
    if stage_configs:                       # 优先看内存中的结构化配置
        for stage in stage_configs:
            session_mode = (
                stage.get("session_mode") if isinstance(stage, dict)
                else getattr(stage, "session_mode", None)
            )
            if session_mode == "duplex":
                return True
    if config_path:                         # 再看 YAML 文件
        try:
            from omegaconf import OmegaConf
            raw_config = OmegaConf.load(config_path)
            session_mode = raw_config.get("session_mode") if hasattr(raw_config, "get") else None
            if session_mode == "duplex":
                return True
        except Exception as exc:
            logger.warning("Failed to inspect duplex session_mode from %s: %s", config_path, exc)
    return False
```

三个细节值得注意：

1. **dict/对象双适配**：`stage` 既可能是 dict（来自 YAML/JSON）也可能是 dataclass/对象，用 `isinstance(stage, dict)` 分流取 `session_mode`，体现配置来源多样化的现实。
2. **OmegaConf 惰性导入**：`from omegaconf import OmegaConf` 写在函数内部而非模块顶部，避免给不需要全双工的部署强加 omegaconf 导入开销（与 u3-l4 讲过的「懒导入隔离重依赖」同思路）。
3. **失败降级而非崩溃**：YAML 解析失败只 `logger.warning` 然后当作「未声明」，不会让整个服务起不来。

**端点挂载**与判定结果的关系（ [`api_server.py:L1627-L1675`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/api_server.py#L1627-L1675)）：

- `/v1/realtime`：先看查询参数 `?duplex=1`，若 `openai_serving_duplex` 存在且开了 `duplex` 查询参数 → 走 `OmniDuplexSessionHandler.handle_realtime_session`；否则走普通 `RealtimeConnection`。
- `/v1/duplex`：直接取 `openai_serving_duplex`，为 `None` 时返回 `unsupported` 错误。

即「路由永远存在，但处理器可能为 `None`」——客户端会拿到明确的「不可用」而非连接被拒，这是更友好的降级。

#### 4.3.4 代码实践

**实践目标**：验证「默认不装配全双工、显式声明后才装配」的行为。

**操作步骤**（源码阅读 + 推理型）：

1. 在 [`api_server.py:L1118`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/api_server.py#L1118) 处确认：`state.openai_serving_duplex` 的初值是 `None`，只有 `should_enable_duplex_endpoint(...)` 返回 `True` 才被赋值。
2. 假设有两份部署配置：
   - **A**（普通）：`vllm_omni/deploy/qwen3_omni.yaml`，顶层与各 stage 都**没有** `session_mode: duplex`。
   - **B**（全双工）：某 stage 配置里写了 `session_mode: duplex`。
3. 推理：A 部署下连 `/v1/duplex` 会得到什么？B 部署下呢？

**需要观察的现象 / 预期结果**：

- A 部署：`should_enable_duplex_endpoint` 遍历所有 stage 都不命中、YAML 顶层也无 `session_mode` → 返回 `False` → `openai_serving_duplex` 保持 `None` → 客户端连 `/v1/duplex` 会收到 `{"type":"error","error":"Duplex API is not available","code":"unsupported"}`。
- B 部署：某个 stage 命中 → 返回 `True` → 创建 `OmniDuplexSessionHandler` → `/v1/duplex` 与 `/v1/realtime?duplex=1` 可用。

> 「待本地验证」：若你有 B 类配置，启动后用 `websocat` 或 Python `websockets` 连 `ws://localhost:8000/v1/duplex` 确认握手成功；切回 A 配置重启，确认收到 `unsupported`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `from omegaconf import OmegaConf` 写在函数体内，而不是模块顶部？

> **答案**：惰性导入。绝大多数部署不会触发 `config_path` 分支（`stage_configs` 已在内存里），把它放函数内可避免给所有调用方强加 omegaconf 的导入开销与潜在依赖问题；只有真正需要读 YAML 时才付这个成本。

**练习 2**：如果 `OmegaConf.load(config_path)` 抛异常（比如文件不存在），函数会返回什么？这个选择合理吗？

> **答案**：被 `try/except` 捕获，记一条 warning 后继续，最终 `return False`（即不启用全双工）。合理——配置读不出来时「安全降级为不开」比「让服务启动失败」更友好，全双工是可选增强特性而非核心功能。

## 5. 综合实践

把三条通道串起来，设计一个**对照实验**，加深对「何时用哪条通道」的理解：

1. **场景选定**：你想做一个「看着摄像头画面、能用语音问答」的助手。请判断该用哪条通道，并说明理由。
   - 提示：视频是主模态 → `/v1/video/chat/stream`；若要叠音/打断 → 还需 `/v1/duplex`（需 `session_mode: duplex`）。

2. **协议走查**：对照 [`docs/serving/video_stream_api.md` 的协议表](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/serving/video_stream_api.md#L36-L51)，写出一次完整视频会话的「客户端→服务端」「服务端→客户端」消息序列（含 `session.config` / `video.frame`×N / `video.query` / `response.start` / `response.text.delta`×M / `response.text.done` / `video.done` / `session.done`）。

3. **音频链路标注**：在序列里标出「音频输出」分支，并指出它复用了 4.2 节的哪段编码逻辑（`_extract_audio_delta_b64` 的 fast/slow 路径 vs RealtimeConnection 的 `_raw_waveform_to_deltas`）。注意：视频通道的音频增量用的是**基类** `video_stream_base.py` 的 `_delta_fast`（按 `chunks_drained` 下标取新增），而 realtime 通道用的是**前缀匹配**——体会两条通道应对「累积 vs 增量」用了不同但等价目标的策略。

4. **开关验证**：把 `should_enable_duplex_endpoint` 的两个入参（`stage_configs`、`config_path`）分别构造一个返回 `True` 的最小输入，写成 2 个 Python 断言（无需运行，写出预期 `assert` 即可）。

   - 参考答案：
     ```python
     # 示例代码（非项目原有代码）
     assert should_enable_duplex_endpoint([{"session_mode": "duplex"}]) is True
     assert should_enable_duplex_endpoint(None, config_path="/path/to/duplex.yaml") is True
     # 其中 duplex.yaml 顶层含 session_mode: duplex
     assert should_enable_duplex_endpoint([{"session_mode": "monologue"}]) is False
     ```

## 6. 本讲小结

- vLLM-Omni 在线服务有三条 WebSocket 流式通道：视频流式理解（`/v1/video/chat/stream`）、实时音频（`/v1/realtime`）、全双工（`/v1/duplex`、`/v1/realtime?duplex=1`），分别服务「视频帧→问答」「音频→文本+音频」「听可叠说」三类连续模态场景。
- `OmniStreamingVideoHandler`（基类）用「读/处理分离 + 模板方法钩子」组织会话：`_reader` 收消息入队、`_processor` 串行处理，`video.frame` 经 EVS 过滤/淘汰/入缓冲/预热，`video.query` 触发一次多阶段推理并流式回 text/audio；Qwen3-Omni 子类只定制「手动触发」与「prompt 拼装」。
- `RealtimeConnection` 继承上游 speech-to-text 连接、只自定义输出侧：用 `_tensor_to_numpy`（张量→numpy，转换点①）和 `_pcm16_b64`（numpy→PCM16，转换点②）把模型音频张量编成增量 PCM，并用 `_realtime_audio_ref` 前缀匹配**同时兼容累积型与增量型**两种引擎音频语义而不重复。
- 视频通道的音频增量用基类 `_delta_fast`（按下标 `chunks_drained` 取新增 tail，并剥离 CausalConv 首帧伪影），与 realtime 通道的前缀匹配是两套等价目标的策略。
- `should_enable_duplex_endpoint` 是个纯函数 opt-in 开关：只有 `session_mode == "duplex"`（在 `stage_configs` 或 YAML `config_path` 中声明）才在初始化时创建实验性 `OmniDuplexSessionHandler`；判定为否时路由仍在但处理器为 `None`，客户端收到 `unsupported` 而非连接被拒。
- 健壮性细节：`_run_generation` 的 `finally` 显式 `aclose()` 生成器（issue #4271，避免断连后空转）、OmegaConf 惰性导入、解析失败降级为 warning，都是可复用的工程范式。

## 7. 下一步学习建议

- **深入全双工实现**：本讲只讲了全双工的「开关」，真正的会话状态机与运行时适配器在 `vllm_omni/experimental/fullduplex/`，建议结合 u9-l4（实验特性）阅读 `experimental/fullduplex/DESIGN.md` 与 `OmniDuplexSessionHandler`，理解它相对普通请求/响应新增了哪些组件与状态、与 `realtime_connection` 的关系。
- **回顾多阶段编排**：本讲反复出现「stage-0 出文本、后续 stage 出音频」，其底层串联机制是 u3-l1（AsyncOmni/Orchestrator）与 u3-l2（Orchestrator 跨阶段前推），可对照阅读加深「为什么文本先于音频」的理解。
- **音频输出处理的另一面**：`OmniRequestOutput` 里 `multimodal_output["audio"]` 是怎么从 AR stage 的逐步张量累积拼出来的，见 u4-l3（MultimodalOutputProcessor 与张量累积），与本讲的「增量编码」形成「累积 → 增量」的闭环。
- **端点装配全貌**：本讲的三条路由都挂在 `api_server.py`，其「删上游路由 + 并入 omni router」的装配哲学见 u6-l1（API Server），可顺带理解 `omni_init_app_state` 的两条初始化分支。

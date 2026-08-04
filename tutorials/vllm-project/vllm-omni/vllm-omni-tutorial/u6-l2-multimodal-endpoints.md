# 多模态端点：Chat / 图像 / 视频 / 语音

## 1. 本讲目标

本讲聚焦 vLLM-Omni 在线服务的「多模态端点层」——也就是介于 HTTP 请求与底层引擎之间的那组 `serving_*` 模块。学完本讲，你应该能够：

1. 说出 `/v1/chat/completions`、`/v1/videos`、`/v1/audio/speech`、`/v1/audio/generate` 四类端点分别由哪个 serving 模块处理，以及它们最终落到哪一类 stage。
2. 理解 `OmniOpenAIServingChat` 如何把一条 OpenAI chat 请求里的多模态 `content`（text/image/video/audio）解析成引擎能消费的 prompt，并能区分「扩散模式的 chat」与「多阶段 LLM 的 chat」两条路径。
3. 读懂 `OmniOpenAIServingVideo` 如何用 `ReferenceImage / ReferenceVideo / ReferenceAudio` 三类参考输入支撑 text-to-video / image-to-video / speech-to-video。
4. 区分 TTS 端点（`/v1/audio/speech`，AR 后端的语音合成）与扩散音频端点（`/v1/audio/generate`，DiT 后端的声音生成），并理解 `tts_adapters` 如何把不同 TTS 模型的请求「归一化」成统一的引擎调用。

本讲是 [u6-l1（API Server：FastAPI 应用构建）](u6-l1-api-server.md) 的直接续篇：u6-l1 讲的是 FastAPI 外壳如何删上游路由、挂 omni 路由并把引擎钉进 `app.state`；本讲拆开这些路由背后的四个 serving 模块。

## 2. 前置知识

- **OpenAI 兼容 API**：vLLM-Omni 尽量复用 OpenAI 的请求/响应 schema（`/v1/chat/completions`、`/v1/audio/speech` 等），但在标准字段之外通过 `extra_body` 透传扩散专用参数（如 `num_inference_steps`、`height`）。详见 [docs/serving/chat_completions_api.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/serving/chat_completions_api.md)。
- **stage（阶段）**：一个请求在 vLLM-Omni 里可能被拆成多个顺序执行的子任务（如 Thinker → Talker → Code2wav），每个子任务是一个独立 stage（见 [u3-l1](u3-l1-async-omni-architecture.md)）。本讲要反复判断「这个端点落到的是 diffusion stage、AR（LLM）stage 还是 TTS stage」。
- **`stage_type` 与 `model_stage`**：`stage_type`（`llm` / `diffusion`）决定一条配置属于哪类执行路径；`model_stage`（如 `qwen3_tts`、`cosyvoice3_talker`、`code2wav`）是更细的模型角色标签，TTS 模块就是靠它识别具体 TTS 模型的。
- **serving 模块与引擎**：每个 serving 模块都持有全局单例引擎 `AsyncOmni`（见 u6-l1），调用其 `generate(...)` 把请求下沉到 stage 进程。本讲的 serving 模块本身不跑模型，只做「请求翻译 + 输出打包」。
- **TTS vs diffusion 音频**：这是本讲的一个关键区分点。TTS（text-to-speech）是用 AR（自回归）模型把文本变成语音，走 `engine_client`（LLM 后端）；而「扩散音频」（如 Stable Audio 的音效/音乐生成）用 DiT 扩散模型生成音频，走 diffusion 后端，二者由不同的 serving 模块和不同端点承载。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [vllm_omni/entrypoints/openai/api_server.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/api_server.py) | 注册 HTTP 路由，定义 `Omnichat/Omnispeech/Omnivideo` 依赖提供者，把请求转交给对应 serving 模块 |
| [vllm_omni/entrypoints/openai/serving_chat.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_chat.py) | `OmniOpenAIServingChat`：同时承载 LLM chat 与扩散 chat，解析多模态 content |
| [vllm_omni/entrypoints/openai/serving_video.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video.py) | `OmniOpenAIServingVideo`：视频生成，处理图片/视频/音频参考输入 |
| [vllm_omni/entrypoints/openai/serving_speech.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_speech.py) | `OmniOpenAIServingSpeech`：TTS 语音合成（AR 后端）编排 |
| [vllm_omni/entrypoints/openai/serving_audio_generate.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_audio_generate.py) | `OmniOpenAIServingAudioGenerate`：扩散音频生成（如 Stable Audio） |
| [vllm_omni/entrypoints/openai/tts_adapters/base.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/tts_adapters/base.py) | TTS 模型适配器基类：`TTSModelAdapter` / `PreparedRequest` / `SpeechServingContext` |
| [vllm_omni/entrypoints/openai/utils.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/utils.py) | `get_stage_type` / `is_single_stage_diffusion` 等跨模块判定函数 |
| [docs/serving/chat_completions_api.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/serving/chat_completions_api.md) | chat 端点如何通过 `extra_body` 传递扩散参数的官方说明 |

## 4. 核心概念与源码讲解

### 4.1 端点到 serving 模块的路由总览

#### 4.1.1 概念说明

u6-l1 讲过：omni 不重写 FastAPI 应用，而是「借上游 + 改两处」。在端点层，这套做法的体现是：保留 OpenAI 风格的 URL 与 schema，但每个路由的处理函数不再调用上游 vLLM 的 serving，而是从 `app.state` 取出 omni 自己的 serving 单例。

关键在于一组**依赖提供者**（dependency provider）——它们是把「HTTP 请求」与「serving 单例」粘合起来的薄函数：

- [api_server.py:1146-1159](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/api_server.py#L1146-L1159) 定义了 `Omnivideo` / `Omnichat` / `Omnispeech` / `OmniAudioGenerate` 四个函数，每个都只是从 `request.app.state` 取出对应的 serving 单例返回（可能为 `None`）。

每个路由处理函数用 `handler = Omnichat(raw_request)` 取到单例后，调用其入口方法（如 `create_chat_completion` / `create_speech`）。

#### 4.1.2 核心流程

一条多模态请求的生命周期如下（请求 → serving 模块 → 引擎 → stage）：

```text
HTTP POST /v1/chat/completions          （或 /v1/videos, /v1/audio/speech, /v1/audio/generate）
   │
   ▼
api_server 的路由处理函数
   │  handler = Omnichat(raw_request)    # 依赖提供者取 serving 单例
   ▼
serving 模块入口方法 (create_chat_completion / generate_videos / create_speech)
   │  1. 解析/校验请求，把多模态 content 翻译成引擎 prompt (OmniTextPrompt)
   │  2. 组装 sampling_params_list（按 stage）
   │  3. 调用 self.engine_client.generate(prompt=..., sampling_params_list=..., output_modalities=...)
   ▼
AsyncOmni / AsyncOmniEngine  （薄代理，下沉到 stage 进程）
   │
   ▼
Orchestrator → 各 stage（diffusion / AR / TTS）   （见 u3-l1 ~ u3-l3）
   │
   ▼
serving 模块把 stage 输出打包成 OpenAI 响应（base64 图/视频/音频）
```

四种端点对应四类 stage：

| 端点 | serving 模块 | 典型 stage 类型 |
|------|--------------|-----------------|
| `/v1/chat/completions` | `OmniOpenAIServingChat` | LLM（AR）/ diffusion / 多阶段 |
| `/v1/videos`、`/v1/videos/sync` | `OmniOpenAIServingVideo` | diffusion（视频） |
| `/v1/audio/speech` | `OmniOpenAIServingSpeech` | TTS（AR 后端） |
| `/v1/audio/generate` | `OmniOpenAIServingAudioGenerate` | diffusion（音频） |

#### 4.1.3 源码精读

以 chat 端点为例，路由与转交逻辑在 [api_server.py:1162-1234](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/api_server.py#L1162-L1234)：路由声明 `@router.post("/v1/chat/completions", ...)`，处理函数 `create_chat_completion` 取到 `Omnichat(raw_request)` 后调用 `handler.create_chat_completion(request, raw_request)`。返回值有三种形态：`ErrorResponse`（转 JSON 错误体）、`ChatCompletionResponse`（一次性 JSON）、或一个异步生成器（SSE 流式，包成 `StreamingResponse`）。注意它对多模态内容刻意绕开 Pydantic 的类型校验（`ChatMessage.content` 标准只接受 `str`，但图像响应需要 list），用 `serialize_as_any=True` 抑制告警——这与 serving 模块里 `model_construct` + `object.__setattr__` 的手法呼应（见 4.2.4）。

speech 端点与之同构，见 [api_server.py:1240-1289](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/api_server.py#L1240-L1289)：`handler = Omnispeech(raw_request)`，调用 `handler.create_speech(request, raw_request)`，返回 `bytes`（音频）、`ErrorResponse` 或 `StreamingResponse`（PCM/SSE 流）。

video 端点稍有不同——它走 multipart 表单而非 JSON，且分异步与同步两条路由：异步 `POST /v1/videos` 见 [api_server.py:3146-3186](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/api_server.py#L3146-L3186)，通过 `_parse_video_form` 依赖解析表单后，用 `asyncio.create_task` 在后台跑生成任务；同步 `POST /v1/videos/sync` 见 [api_server.py:3189-3225](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/api_server.py#L3189-L3225)，直接 `await` 拿到 MP4 字节。

> **判别小工具**：serving 模块反复用 `get_stage_type(stage_cfg)` 判断某 stage 是 `llm` 还是 `diffusion`（[utils.py:11-20](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/utils.py#L11-L20)），用 `is_single_stage_diffusion(engine_client)` 判断是否「单 stage 纯扩散」（[utils.py:86-91](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/utils.py#L86-L91)）。后者决定了 chat 端点走「扩散快路径」还是「多阶段 LLM 路径」。

#### 4.1.4 代码实践

1. **实践目标**：建立「端点 → serving 模块 → stage」的全局映射。
2. **操作步骤**：
   - 打开 `api_server.py`，依次定位 `@router.post("/v1/chat/completions"...)`、`@router.post("/v1/audio/speech"...)`、`@router.post("/v1/videos"...)`、`@router.post("/v1/audio/generate"...)` 四处。
   - 对每处，确认它调用的 `handler.<方法>` 名字，以及 handler 来自哪个依赖提供者。
3. **需要观察的现象**：四个端点的处理函数结构高度相似（取 handler → 调方法 → 处理三种返回形态），这是 omni 故意保持的对称性。
4. **预期结果**：你能用一张表把「端点 / handler 提供者 / 入口方法 / 返回类型」四列填满。

#### 4.1.5 小练习与答案

**练习 1**：为什么 chat 路由处理函数里要用 `isinstance(generator, ChatCompletionResponse)` 和异步生成器两种分支分别处理？
**参考答案**：因为 `create_chat_completion` 既可能返回一次性 JSON（非流式 `ChatCompletionResponse`），也可能返回 SSE 流（`stream=true` 时）。前者包成 `JSONResponse`，后者包成 `media_type="text/event-stream"` 的 `StreamingResponse`。

---

### 4.2 Chat 端点的多模态 content 解析

#### 4.2.1 概念说明

`/v1/chat/completions` 是 omni 最「万能」的端点：它既能跑普通 LLM 对话，也能跑扩散文生图/图生图，还能跑 Qwen3-Omni 这类多阶段全模态模型。这一切都由同一个类 `OmniOpenAIServingChat` 承担，它继承自上游 vLLM 的 `OpenAIServingChat` 并混入 `AudioMixin`（见 [serving_chat.py:148](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_chat.py#L148)）。

它的难点在于：OpenAI 的 chat 请求里，多模态信息藏在 `messages[*].content` 里，可能是纯字符串，也可能是 `[{type:"text"...}, {type:"image_url"...}, {type:"audio_url"...}]` 这样的结构化列表。serving 模块要做的就是把这份异构 content「摊平」成引擎能理解的 prompt + 参考媒体。

`OmniOpenAIServingChat` 有两种构造方式：完整的多阶段实例（持有 `engine_client`）与扩散专用实例（由 `for_diffusion` 工厂创建，仅持有 `_diffusion_engine`）。后者用于「单 stage 纯扩散」部署。

#### 4.2.2 核心流程

一条 chat 请求进入 `create_chat_completion` 后，主入口 `_create_chat_completion` 按以下顺序分流（见 [serving_chat.py:445-469](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_chat.py#L445-L469)）：

```text
serves_diffusion = self._diffusion_mode 或 任一 stage 是 diffusion ?
   │
   ├─ 若 serves_diffusion: 先 _normalize_diffusion_request_args 把 extra_body 归一化
   │
   ├─ 分支 A: self._diffusion_mode == True
   │     → _create_diffusion_chat_completion （纯扩散 chat 快路径）
   │
   └─ 分支 B: 多阶段 LLM 模式
         1. 用请求 modalities（如 ["image"]）决定如何重写 engine_prompt
         2. 按 stage 组装 sampling_params_list
         3. engine_client.generate(...) → 全量/流式生成
```

关键变量是 **`request.modalities`**（输出模态列表，如 `["image"]`、`["text","audio"]`）。它决定后续走文生图、文生视频还是语音输出。

#### 4.2.3 源码精读

**（a）多模态 content 解析器**。无论走哪条路径，都靠 `_extract_diffusion_prompt_and_media` 把 messages 解析成 `(prompt_text, images, videos, audios)` 四元组，见 [serving_chat.py:3821-3888](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_chat.py#L3821-L3888)。它遍历 `role=="user"` 的消息，识别三类 content part：

- `{"type":"text",...}` 或纯字符串 → 拼进 `prompt_parts`；
- `{"type":"image_url",...}` → 抽出 base64 进 `images`；
- `{"type":"video_url",...}` → 进 `videos`；`{"type":"audio_url",...}` → 进 `audios`。

最后 `prompt = " ".join(prompt_parts).strip()`。

**（b）扩散专用 chat 的快路径**。当部署是单 stage 纯扩散（`_diffusion_mode=True`）时，走 `_create_diffusion_chat_completion`（[serving_chat.py:3444-3458](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_chat.py#L3444-L3458) 的方法签名段）。它直接构造一个扩散 prompt：

```python
gen_prompt: OmniTextPrompt = {"prompt": prompt, "negative_prompt": ..., "modalities": ["image"]}
```

再包一层 `OmniDiffusionSamplingParams`（`height/width/num_inference_steps/seed/...`），调用 `diffusion_engine.generate(prompt=..., sampling_params_list=...)`，最后把图片转 base64 塞进 `message.content`（详见 4.2.4）。注意 `extra_body` 里若带 `modalities: ["text"]`，会把 `gen_prompt["modalities"]` 改成 `["text"]`，走 img2text/text2text 路径——这是扩散模型也能输出文本的入口。

**（c）多阶段 LLM 的 image 模态重写**。当部署是多阶段（如 GLM-Image 这类「AR 理解 + DiT 生成」），且 `modalities` 含 `"image"` 时，[serving_chat.py:617-688](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_chat.py#L617-L688) 会**覆盖** chat-template 预处理产生的 `engine_prompts`，改用一个干净的 `OmniTextPrompt`：

```python
tprompt: OmniTextPrompt = {"prompt": extracted_prompt}
tprompt["modalities"] = ["img2img"] if is_img2img else ["image"]
tprompt["mm_processor_kwargs"] = {"target_h": height, "target_w": width, "modalities": [...]}
if engine_prompt_image is not None:
    tprompt["multi_modal_data"] = {"img2img": img}
```

注释点明了原因（[serving_chat.py:613-617](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_chat.py#L613-L617)）：多阶段文生图时，Stage-0（AR）需要**干净的文本 prompt**，若传 chat-template 渲染过的 token id，GLM-Image 会变成「无条件生成」产出乱图。

**（d）extra_body 归一化**。扩散参数（`height`、`num_inference_steps` 等）在 OpenAI schema 里不存在，靠 `extra_body` 透传。`_normalize_diffusion_request_args`（[serving_chat.py:331-353](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_chat.py#L331-L353)）处理 `extra_body` 的两种来源：SDK 的关键字参数（被展平进顶层）与 curl 的嵌套 `extra_body` 键（见 [chat_completions_api.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/serving/chat_completions_api.md) 的「SDK extra_body vs JSON extra_body」说明）。它区分「公共根字段」（`_diffusion_common_root_fields`，如 `height/width/seed`）与「模型专属字段」（从 `model_extras` 注册表查），保证两者不冲突。

#### 4.2.4 代码实践

1. **实践目标**：发起一次「文生图」chat 请求，并对照源码说明它落到哪个 serving 模块、哪个 stage。
2. **操作步骤**：
   - 启动单 stage 扩散服务：`vllm serve Qwen/Qwen-Image --omni --port 8091`。
   - 用 quickstart 里的 curl 发一次请求（来自 [text_to_image.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/user_guide/examples/online_serving/text_to_image.md)）：

     ```bash
     curl -s http://localhost:8091/v1/chat/completions \
       -H "Content-Type: application/json" \
       -d '{"messages":[{"role":"user","content":"A beautiful landscape painting"}],
            "extra_body":{"height":1024,"width":1024,"num_inference_steps":50,"seed":42}}' \
       | jq -r '.choices[0].message.content[0].image_url.url' | cut -d',' -f2- | base64 -d > output.png
     ```
3. **需要观察的现象**：服务日志里应出现 `Diffusion chat request ...` 与 `Diffusion chat completed ... output_type=image`（对应 `_create_diffusion_chat_completion` 里的 `logger.info`）。返回的图片以 `data:image/png;base64,...` 形式藏在 `choices[0].message.content[0].image_url.url`。
4. **预期结果**：得到一张 1024×1024 的风景画。**待本地验证**（若无 GPU，可只读源码理解流程）。
5. **对照源码**：该请求因 `is_single_stage_diffusion` 为真，走 4.2.3 (b) 的扩散快路径；`extra_body` 里的 `height/width/num_inference_steps` 经 4.2.3 (d) 归一化进 `OmniDiffusionSamplingParams`；图片由 `_create_image_choice`（[serving_chat.py:2805-2917](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_chat.py#L2805-L2917)）转成 base64，其中用 `ChatMessage.model_construct` + `object.__setattr__(message, "content", ...)` 绕过 Pydantic 的 `str` 类型校验，让 `content` 能装下 list 形式的图像内容。

#### 4.2.5 小练习与答案

**练习 1**：如果同一个 `/v1/chat/completions` 请求里 `modalities` 同时含 `"text"` 和 `"image"`（如多阶段 Qwen3-Omni 的图文混合输出），serving 模块如何保证流式响应里 `finish_reason="stop"` 只出现一次？
**参考答案**：`chat_completion_stream_generator` 用 `modality_seen`/`modality_finished` 两个集合跟踪「真正出现过的模态」与「已完成的模态」，只有当所有已产出模态都完成时，才在某条 chunk 上写 `finish_reason="stop"`，其余 chunk 的 `finish_reason` 被置为 `None`（见 [serving_chat.py:2003-2012](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_chat.py#L2003-L2012) 附近逻辑），以符合 OpenAI「每个 choice 恰好一个 stop」的规范。

**练习 2**：为什么多阶段文生图要「覆盖」chat-template 渲染出的 prompt，而单阶段扩散不用？
**参考答案**：多阶段模型的 Stage-0 是 AR 模型，需要干净文本让模型自己的 processor 构造正确输入；若喂入 chat-template 渲染过的 token id，AR 阶段会误判为无图像生成意图（[serving_chat.py:613-617](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_chat.py#L613-L617)）。单阶段扩散只有一个 diffusion stage，prompt 直接喂给扩散 pipeline，无需 AR 预处理。

---

### 4.3 Video 端点的参考音视频处理

#### 4.3.1 概念说明

视频生成端点 `/v1/videos` 与 chat 端点的设计哲学不同：它**只服务 diffusion stage**（见 `_run_generation` 里的校验，[serving_video.py:412-419](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video.py#L412-L419)：任一非 diffusion stage 直接报 `SERVICE_UNAVAILABLE`），且接受 multipart 表单而非 JSON。

`OmniOpenAIServingVideo`（[serving_video.py:76](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video.py#L76)）的核心是三类「参考输入」dataclass，它们决定了视频生成是 text-to-video、image-to-video 还是 speech-to-video：

- `ReferenceImage`（[serving_video.py:41-45](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video.py#L41-L45)）：单张参考图，用于 image-to-video。
- `ReferenceVideo`（[serving_video.py:48-53](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video.py#L48-L53)）：参考视频帧列表，用于 video-to-video。
- `ReferenceAudio`（[serving_video.py:56-59](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video.py#L56-L59)）：参考音频路径，用于 speech-to-video（音频驱动口型/动作，如 Wan2.2-S2V）。

这三类参考输入会被汇入 prompt 的 `multi_modal_data` 字典，键名分别是 `image` / `video` / `audio`。

#### 4.3.2 核心流程

视频请求的核心是 `_run_and_extract`（[serving_video.py:114-253](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video.py#L114-L253)），它把表单参数翻译成引擎调用并抽取结果：

```text
_run_and_extract(request, reference_id, reference_image=?, reference_video=?, reference_audio=?)
   │
   ├─ 1. prompt = OmniTextPrompt(prompt=..., modalities=["video"])     # 视频 modality 固定
   ├─ 2. gen_params = 深拷贝 stage 默认 OmniDiffusionSamplingParams     # 避免请求间状态泄漏
   ├─ 3. 互斥校验：reference_image 与 reference_video 不能同时给
   ├─ 4. 组装 multi_modal_data = {"image"|"video"|"audio": ...}（若有参考）
   ├─ 5. 把 vp.width/height/num_frames/fps/num_inference_steps/guidance_scale... 写进 gen_params
   ├─ 6. _run_generation → engine_client.generate(prompt, sampling_params_list)
   └─ 7. 从结果抽取 videos / audios / actions / sample_rate / fps → VideoGenerationArtifacts
```

`_run_generation`（[serving_video.py:398-443](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video.py#L398-L443)）用 `build_stage_sampling_params_list(..., replace_diffusion_params=True)` 把 `gen_params` 注入到 diffusion stage 的采样参数，再调 `engine_client.generate(...)`。

#### 4.3.3 源码精读

**（a）参考输入如何汇入 prompt**。[serving_video.py:144-152](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video.py#L144-L152) 展示了三类参考的归一化：

```python
multi_modal_data: dict[str, Any] = {}
if input_image is not None:
    multi_modal_data["image"] = input_image
elif input_video is not None:
    multi_modal_data["video"] = input_video
if reference_audio is not None:
    multi_modal_data["audio"] = reference_audio.path
if multi_modal_data:
    prompt["multi_modal_data"] = multi_modal_data
```

注意 image 与 video 互斥（前面 [serving_video.py:132-136](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video.py#L132-L136) 已校验），但 audio 可以与 image 共存——这正是 speech-to-video（音频驱动 + 人脸参考图）的组合。若给了参考图且指定了目标尺寸，还会先把图按 LANCZOS 重采样到目标尺寸（[serving_video.py:140-143](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video.py#L140-L143)）。

**（b）「只覆盖用户显式提供的字段」原则**。`_run_and_extract` 大量使用 `if "xxx" in request.model_fields_set and request.xxx is not None` 模式（如 [serving_video.py:171-194](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video.py#L171-L194)）。`model_fields_set` 是 Pydantic 记录的「用户显式传入的字段集合」，这样默认值不会覆盖 stage/deploy YAML 的内置默认，只有用户真正写了 `num_inference_steps=40` 才覆盖。

**（c）深拷贝防泄漏**。`_resolve_default_sampling_params`（[serving_video.py:370-379](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video.py#L370-L379)）对 stage 默认采样参数做 `copy.deepcopy`，注释点明：请求会原地改采样参数（含 `extra_args` 这类嵌套 dict），不深拷贝会让一个请求的改动泄漏到下一个请求。

**（d）统一产出 `VideoGenerationArtifacts`**。结果抽取汇聚成这个 dataclass（[serving_video.py:63-73](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video.py#L63-L73)），含 `videos/audios/actions/audio_sample_rate/output_fps/stage_durations/peak_memory_mb`。其中 `actions` 是面向「世界模型/VLA」的动作输出（机器人动作策略），`output_fps` 会乘以模型报告的 `video_fps_multiplier`（支持插帧后的 fps 翻倍）。最终 `generate_videos`（[serving_video.py:255-302](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_video.py#L255-L302)）把视频帧编码成 MP4 + base64（若有音频则混流），包进 `VideoGenerationResponse`。

#### 4.3.4 代码实践

1. **实践目标**：发起一次「文生视频」请求，并说明它落到哪个 serving 模块与 stage。
2. **操作步骤**：
   - 启动视频模型：`vllm serve Wan-AI/Wan2.2-T2V-A14B-Diffusers --omni --port 8091`。
   - 用同步端点（来自 [videos_api.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/serving/videos_api.md)）：

     ```bash
     curl -X POST http://localhost:8091/v1/videos/sync \
       -F "prompt=A small robot walking through a neon city" \
       -F "width=854" -F "height=480" -F "num_frames=81" -F "fps=16" \
       -F "num_inference_steps=30" -F "seed=42" \
       -o output.mp4
     ```
3. **需要观察的现象**：响应头 `X-Request-Id` / `X-Model` / `X-Inference-Time-S` 体现同步端点的元数据返回方式；服务日志应出现 `Video sampling params: steps=... guidance=...`（来自 `_run_and_extract` 的 `logger.info`）。
4. **预期结果**：得到一个 `video/mp4` 文件。**待本地验证**（视频模型显存需求高，无 GPU 时可只读源码）。
5. **对照源码**：表单经 `_parse_video_form` 解析成 `VideoGenerationRequest` + 参考 dataclass；`generate_video_bytes` → `_run_and_extract` → `_run_generation`；落到唯一的 diffusion stage；产出经 `_encode_video_bytes` 编码返回原始字节。

#### 4.3.5 小练习与答案

**练习 1**：`ReferenceImage`、`ReferenceVideo`、`ReferenceAudio` 在 `multi_modal_data` 里的键分别是什么？哪些可以共存？
**参考答案**：键分别是 `image`、`video`、`audio`。`image` 与 `video` 互斥（一个请求只能有一种视觉参考），但 `audio` 可以与 `image` 共存——这正是 speech-to-video（音频驱动 + 参考人脸图）的组合。

**练习 2**：为什么 `_resolve_default_sampling_params` 要对默认采样参数做 `copy.deepcopy`？
**参考答案**：因为请求处理会原地修改采样参数（包括 `extra_args` 这种嵌套 dict）。引擎持有的默认参数是跨请求共享的对象，若不深拷贝，一个请求的修改会污染后续所有请求。

---

### 4.4 Speech 端点：TTS 与 diffusion 音频的区分

#### 4.4.1 概念说明

「语音」相关的端点在 omni 里被拆成两个完全不同的 serving 模块，这是本讲最容易混淆的点：

| 端点 | serving 模块 | 后端 | 模型举例 | 用途 |
|------|--------------|------|----------|------|
| `/v1/audio/speech` | `OmniOpenAIServingSpeech` | AR（`engine_client`） | Qwen3-TTS、CosyVoice3、Fish Speech、VoxCPM2 | 文本→**语音**（TTS） |
| `/v1/audio/generate` | `OmniOpenAIServingAudioGenerate` | diffusion | Stable Audio | 文本→**声音/音乐**（扩散音频） |

二者的本质区别：TTS 是「自回归地预测语音 token 再解码成波形」，扩散音频是「用 DiT 在音频 latent 空间去噪」。所以 TTS 用 `SamplingParams`（token 级），扩散音频用 `OmniDiffusionSamplingParams`（步数/CFG 级）。

`OmniOpenAIServingSpeech` 还能在「纯扩散 TTS」（如 OmniVoice）部署下用 `for_diffusion` 工厂创建（`backend="diffusion"`），此时它走 `_create_diffusion_speech`（[serving_speech.py:3611](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_speech.py#L3611)），但绝大多数 TTS 模型走 AR 后端。

#### 4.4.2 核心流程

TTS 请求的主编排是 `_prepare_speech_generation`（[serving_speech.py:3211-3458](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_speech.py#L3211-L3458)），它被 `create_speech`（[serving_speech.py:3761-3902](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_speech.py#L3761-L3902)）在多种流式/非流式分支中复用：

```text
create_speech(request)
   │
   ├─ 若 _diffusion_mode: → _create_diffusion_speech
   ├─ 若 is_raw_audio_stream:  → _prepare → _generate_audio_chunks（PCM/WAV 流）
   ├─ 若 is_sse_stream:        → _prepare → _generate_audio_sse_events（SSE 流）
   └─ 否则（同步）:            → _generate_audio_bytes → Response(bytes)

_prepare_speech_generation(request)
   ├─ 1. 深拷贝 default_sampling_params_list，按是否流式 coerce 输出类型（DELTA/FINAL_ONLY）
   ├─ 2. 通过 _get_tts_adapter() 取该模型的 adapter：
   │      adapter.validate(request) → adapter.build(...) → PreparedRequest(prompt, tts_params, ...)
   ├─ 3. 注入模型特定采样覆盖（max_new_tokens / seed / extra_params / CFG pair 等）
   └─ 4. generator = engine_client.generate(prompt, sampling_params_list, output_modalities=["audio"])
```

注意第 4 步固定 `output_modalities=["audio"]`（[serving_speech.py:3447-3452](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_speech.py#L3447-L3452)）——TTS 永远声明音频输出。

#### 4.4.3 源码精读

**（a）如何识别「这是哪款 TTS 模型」**。`_find_tts_stage`（[serving_speech.py:688-708](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_speech.py#L688-L708)）遍历所有 stage，按 `engine_args.model_stage` 是否落入 `_TTS_MODEL_STAGES`（[serving_speech.py:107-126](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_speech.py#L107-L126)）来判定；这个集合枚举了所有支持的 TTS `model_stage`（`qwen3_tts`、`cosyvoice3_talker`、`fish_speech_slow_ar`、`latent_generator` 等）。找到后，`_detect_tts_model_type`（[serving_speech.py:710-757](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_speech.py#L710-L757)）进一步把它映射成一个短字符串（如 `"qwen3_tts"`、`"voxcpm2"`、`"cosyvoice3"`），这个字符串就是 adapter 注册表的 key。

**（b）Omni 模型被显式拒绝**。若 stage 名是 `talker`（Qwen3-Omni 这类全模态模型的 talker），但没有专用 TTS adapter，`_prepare_speech_generation` 会抛出明确错误（[serving_speech.py:3280-3287](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_speech.py#L3280-L3287)）：`/v1/audio/speech` 只服务专用 TTS 模型，Qwen3-Omni 这类要用 `/v1/chat/completions` + `"modalities":["audio"]`。

**（c）扩散音频端点的对照**。`OmniOpenAIServingAudioGenerate`（[serving_audio_generate.py:22](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_audio_generate.py#L22)）的 `create_audio_generate`（[serving_audio_generate.py:40-167](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_audio_generate.py#L40-L167)）是 TTS 的「扩散镜像」：它构造 `OmniDiffusionSamplingParams`（[serving_audio_generate.py:73](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_audio_generate.py#L73)），把 `audio_length`/`audio_start` 换算成 `audio_start_in_s`/`audio_end_in_s` 塞进 `extra_args`，然后调 `engine_client.generate(..., output_modalities=["audio"])`（[serving_audio_generate.py:104-109](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_audio_generate.py#L104-L109)）。它专门处理 Stable Audio 这类「按时长生成音效/音乐」的模型，从 `multimodal_output["audio"]` 或 `["model_outputs"]` 取音频张量。

#### 4.4.4 代码实践

1. **实践目标**：发起一次「文生语音」请求，对照源码确认它走的是 TTS（AR）而非扩散音频。
2. **操作步骤**：
   - 启动 TTS 模型：`vllm serve Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice --omni --port 8091`。
   - 发请求（来自 [text_to_speech.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/user_guide/examples/online_serving/text_to_speech.md)）：

     ```bash
     curl -X POST http://localhost:8091/v1/audio/speech \
       -H "Content-Type: application/json" \
       -d '{"input":"今天天气真好","voice":"ryan","response_format":"wav"}' \
       --output output.wav
     ```
3. **需要观察的现象**：服务启动日志应有 `Resolved TTS serving adapter: ...`（`__init__` 里 `resolve_adapter`）；请求日志 `TTS speech request ...: text=... model=qwen3_tts`（`_prepare_speech_generation` 的 `logger.info`）。
4. **预期结果**：得到一个 24 kHz 的 WAV 文件。**待本地验证**。
5. **对照源码**：请求进入 `create_speech`（非 `_diffusion_mode`、非流式）→ `_generate_audio_bytes` → `_prepare_speech_generation` → `adapter.build`（Qwen3-TTS 的 adapter）→ `engine_client.generate(output_modalities=["audio"])`，落 Qwen3-TTS 的 talker stage。

#### 4.4.5 小练习与答案

**练习 1**：用户把一个 Qwen3-Omni（全模态）模型用 `vllm serve` 起来后，调 `/v1/audio/speech` 会发生什么？正确做法是什么？
**参考答案**：会被拒绝并返回错误信息「`/v1/audio/speech` 只支持专用 TTS 模型，Qwen3-Omni 这类要用 `/v1/chat/completions` 加 `"modalities":["audio"]`」（[serving_speech.py:3280-3287](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_speech.py#L3280-L3287)）。因为全模态模型的 talker stage 需要完整 chat 上下文，不能裸跑 TTS。

**练习 2**：`/v1/audio/speech`（TTS）与 `/v1/audio/generate`（扩散音频）在采样参数类型上有何不同？
**参考答案**：TTS 用 vLLM 的 `SamplingParams`（AR token 级，关心 `max_tokens`/`temperature`）；扩散音频用 `OmniDiffusionSamplingParams`（步数级，关心 `num_inference_steps`/`guidance_scale` 与 `audio_start_in_s`/`audio_end_in_s` 时长参数，见 [serving_audio_generate.py:89-96](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/serving_audio_generate.py#L89-L96)）。

---

### 4.5 TTS adapter 的请求/参数归一化

#### 4.5.1 概念说明

TTS 是 omni 里「模型种类最多」的模态（Qwen3-TTS、CosyVoice3、Fish Speech、VoxCPM2、MOSS-TTS、Higgs-Audio……），每款的 prompt 构造、参数语义、参考音频处理都不一样。如果把这一切都堆进 `serving_speech.py` 的 `if self._tts_model_type == ...` 分支，文件会膨胀到无法维护。

`tts_adapters` 框架（RFC #4327）就是为了解这个问题：**一个 TTS 模型 = 一个 adapter 类**。`serving_speech.py` 只保留通用编排（流式分流、采样覆盖、引擎调用），把「请求校验 + prompt/参数构造」下放给每个模型自己的 adapter。本模块对应规格里的「TTS adapter 的请求/参数归一化」。

#### 4.5.2 核心流程

adapter 的生命周期由 `serving_speech.py` 在两处驱动：

```text
启动时（OmniOpenAIServingSpeech.__init__）:
   _detect_tts_model_type() → "qwen3_tts" / "cosyvoice3" / ...
   resolve_adapter(model_type) → adapter_cls
   adapter = adapter_cls(SpeechServingContext(server=self, engine_client=...))

请求时（_prepare_speech_generation）:
   has_inline_ref_audio = (request.ref_audio is not None)   # 在 validate 前抓取
   adapter.validate(request)        # 校验/normalize 请求
   prepared = adapter.build(request, sampling_params_list, has_inline_ref_audio)
        → PreparedRequest(prompt, tts_params, model_type, output_policy, warmup_artifact_key)
   adapter.apply_sampling_overrides(sampling_params_list, request)   # 可选
   engine.generate(prompt=prepared.prompt, sampling_params_list=..., output_modalities=["audio"])
```

#### 4.5.3 源码精读

**（a）三个核心数据结构**。

- `TTSModelAdapter`（[tts_adapters/base.py:91-154](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/tts_adapters/base.py#L91-L154)）：抽象基类，定义 `normalize` / `validate` / `build`（抽象）/ `apply_sampling_overrides` 四个钩子。类属性 `name`（注册 key）、`backend`（`"ar"` 或 `"diffusion"`）、`stage_keys`（该模型用的 `model_stage`）。
- `PreparedRequest`（[tts_adapters/base.py:55-70](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/tts_adapters/base.py#L55-L70)）：`build` 的返回值，装着引擎调用所需的一切——`prompt`（引擎 prompt dict）、`tts_params`（每模型参数 dict）、`model_type`（日志用判别串）、`output_policy`（控制非流式聚合）、`warmup_artifact_key`（Qwen3-TTS 参考音频预热产物 key）。
- `SpeechServingContext`（[tts_adapters/base.py:74-88](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/tts_adapters/base.py#L74-L88)）：传给 adapter 的「上下文」，持有 owner `server`（复用 serving_speech 已有的参考音频解析、上传音色处理等成熟工具方法）与 `engine_client`/`diffusion_engine`（二选一，匹配部署后端）。

**（b）两类后端的 adapter 基类**。

- `ARTTSAdapter`（[tts_adapters/base.py:157-165](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/tts_adapters/base.py#L157-L165)：`backend="ar"`）：覆盖纯 AR codec 模型，以及「AR-LM + 模型内部扩散」的混合模型（如 VoxCPM2、Ming），其内部扩散对 serving 层不可见。
- `DiffusionTTSAdapter`（[tts_adapters/base.py:168-189](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/tts_adapters/base.py#L168-L189)：`backend="diffusion"`）：纯扩散 TTS pipeline（如 OmniVoice），通过 `extra_body_params()` 桥接到扩散 pipeline 的参数契约。

**（c）「先抓 inline ref audio 再 validate」的顺序细节**。[tts_adapters/base.py:125-142](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/tts_adapters/base.py#L125-L142) 的 `build` 文档点明：`has_inline_ref_audio` 必须在 `validate()` **之前**由编排者抓取，因为若干 adapter 的 `validate` 内部会调 `_apply_uploaded_speaker` 原地设置 `request.ref_audio`，若在 validate 之后再算，会把「上传音色」误判成「内联参考音频」，丢掉 `voice_name`/`voice_created_at` 元数据。这种顺序约束体现了 adapter 框架对细节的精确控制。

**（d）归一化的实际例子**。例如 Qwen3-TTS 的 adapter 会把 OpenAI 请求的 `voice`（如 `"Vivian"`）、`task_type`（`"CustomVoice"`/`"VoiceDesign"`/`"Base"`）、`language`、`instructions`、`ref_audio`/`ref_text` 归一化成 talker stage 能消费的 `additional_information`（speaker、task_type、ref_code 等），并估算 placeholder prompt 长度（见 serving_speech.py 的 `_estimate_prompt_len`）。而 CosyVoice3 的 adapter 则会把参考音频编解码成 `audio_input_ids` 张量塞进 `additional_information`。不同模型，统一接口。

#### 4.5.4 代码实践

1. **实践目标**：为一个假想 TTS 模型设计 adapter 骨架，理解请求/参数归一化的职责边界。
2. **操作步骤**（源码阅读型实践）：
   - 读 [tts_adapters/base.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/tts_adapters/base.py)，列出 `TTSModelAdapter` 的四个钩子方法及其职责。
   - 在 `vllm_omni/entrypoints/openai/tts_adapters/` 目录下找一个具体实现（如 qwen3_tts 的 adapter），对比它的 `validate` 与 `build` 如何把 OpenAI 字段映射成引擎字段。
3. **需要观察的现象**：具体 adapter 的 `build` 返回的 `PreparedRequest.prompt` 形态各异（有的是 `{"prompt": text}`，有的是带 `prompt_token_ids` 的 `tokens_input`，有的带 `additional_information` 张量），但都被编排者用同一行 `engine.generate(prompt=prepared.prompt, ...)` 消费。
4. **预期结果**：你能写出一份「字段映射表」，左列是 OpenAI 请求字段（`voice`/`speed`/`ref_audio`/`instructions`…），右列是它最终落到引擎/stage 的什么参数。
5. **示例代码**（假想 adapter 骨架，非项目原有代码）：

   ```python
   # 示例代码：假想 TTS adapter，仅演示归一化职责
   class MyTTSAdapter(ARTTSAdapter):
       name = "my_tts"
       stage_keys = frozenset({"my_tts_talker"})
       backend = "ar"

       def validate(self, request):
           if not request.input.strip():
               return "input cannot be empty"
           return None

       async def build(self, request, sampling_params_list, has_inline_ref_audio):
           # 把 voice/speed 归一化成 stage 采样参数
           return PreparedRequest(
               prompt={"prompt": request.input},
               tts_params={"speaker": [request.voice], "speed": [request.speed or 1.0]},
               model_type=self.name,
           )
   ```

#### 4.5.5 小练习与答案

**练习 1**：为什么 `has_inline_ref_audio` 必须在 `adapter.validate()` **之前**抓取，而不能在 `build()` 内部重新计算？
**参考答案**：因为部分 adapter 的 `validate` 会调 `_apply_uploaded_speaker`，它会把上传音色的数据原地写进 `request.ref_audio`。若在 validate 之后才算 `has_inline_ref_audio`，上传音色会被误判成「用户内联提供了参考音频」，导致 `voice_name`/`voice_created_at` 等元数据丢失、缓存 key 错乱（[tts_adapters/base.py:125-142](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/tts_adapters/base.py#L125-L142)）。

**练习 2**：`ARTTSAdapter` 与 `DiffusionTTSAdapter` 的 `backend` 分别是什么？这决定了 serving 层走哪个引擎入口？
**参考答案**：分别是 `"ar"` 与 `"diffusion"`。`"ar"` 的 adapter 走 `engine_client`（LLM 后端，`SpeechServingContext.engine_client`），`"diffusion"` 的走扩散引擎（`SpeechServingContext.diffusion_engine`）。`DiffusionTTSAdapter` 还多一个 `pipeline_cls` 与 `extra_body_params()`，用于桥接扩散 pipeline 的参数契约（[tts_adapters/base.py:168-189](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/openai/tts_adapters/base.py#L168-L189)）。

---

## 5. 综合实践

**任务**：用 `chat_completions_api` 文档中的请求格式，分别构造「文生图」「文生视频」「文生语音」三种请求，对照源码填写每个请求落到哪个 serving 模块、哪个 stage、用哪种采样参数类型。

**步骤**：

1. 准备三份请求模板（均来自官方文档）：

   ```bash
   # (1) 文生图 — /v1/chat/completions
   curl -s http://localhost:8091/v1/chat/completions -H "Content-Type: application/json" \
     -d '{"messages":[{"role":"user","content":"A futuristic city at night"}],
          "extra_body":{"height":1024,"width":1024,"num_inference_steps":50,"seed":42}}'

   # (2) 文生视频 — /v1/videos/sync
   curl -X POST http://localhost:8091/v1/videos/sync \
     -F "prompt=A small robot walking through a neon city" \
     -F "width=854" -F "height=480" -F "num_frames=81" -F "fps=16" -o out.mp4

   # (3) 文生语音 — /v1/audio/speech
   curl -X POST http://localhost:8091/v1/audio/speech -H "Content-Type: application/json" \
     -d '{"input":"Hello, how are you?","voice":"default","response_format":"wav"}' -o out.wav
   ```

2. 针对每种请求，对照本讲源码填写下表（**待本地验证**列：若未实际运行，标注「待本地验证」）：

   | 请求 | HTTP 端点 | api_server 路由函数 | serving 模块 | 入口方法 | 落到的 stage 类型 | 采样参数类型 |
   |------|-----------|---------------------|--------------|----------|-------------------|--------------|
   | 文生图 | `/v1/chat/completions` | `create_chat_completion` | `OmniOpenAIServingChat` | `_create_diffusion_chat_completion`（单 stage 扩散） | diffusion | `OmniDiffusionSamplingParams` |
   | 文生视频 | `/v1/videos/sync` | `create_video_sync` | `OmniOpenAIServingVideo` | `generate_video_bytes`→`_run_and_extract` | diffusion（视频） | `OmniDiffusionSamplingParams` |
   | 文生语音 | `/v1/audio/speech` | `create_speech` | `OmniOpenAIServingSpeech` | `create_speech`→`_generate_audio_bytes`→`_prepare_speech_generation` | TTS（AR） | `SamplingParams`（token 级） |

3. **进阶**：把文生图请求的 `extra_body` 改成 `{"modalities":["text"]}` 并发给一个支持 img2text 的扩散模型，观察它是否走 `_create_diffusion_chat_completion` 里的文本输出分支（`is_text_request`），返回 `text_body`。

**预期结果**：你将得到一张图、一段视频、一段音频，并能完整解释每个请求从 HTTP 到 stage 的流转路径。

## 6. 本讲小结

- 四类多模态端点由四个 serving 模块承载：chat（`OmniOpenAIServingChat`）、video（`OmniOpenAIServingVideo`）、speech（`OmniOpenAIServingSpeech`，TTS/AR）、audio_generate（`OmniOpenAIServingAudioGenerate`，扩散音频）；api_server 用 `Omnichat/Omnispeech/Omnivideo/OmniAudioGenerate` 依赖提供者把它们钉进路由。
- `OmniOpenAIServingChat` 是最万能的端点：靠 `request.modalities` + `is_single_stage_diffusion` 在「扩散快路径」与「多阶段 LLM 路径」间分流；多模态 content 由 `_extract_diffusion_prompt_and_media` 摊平成 prompt + 参考媒体；扩散参数靠 `extra_body` 透传并经 `_normalize_diffusion_request_args` 归一化。
- `OmniOpenAIServingVideo` 只服务 diffusion stage，用 `ReferenceImage/ReferenceVideo/ReferenceAudio` 三类参考输入支撑 text/image/speech-to-video；image 与 video 互斥但 audio 可与 image 共存；坚持「只覆盖用户显式提供的字段」+「深拷贝默认参数防泄漏」。
- `/v1/audio/speech`（TTS，AR 后端，`SamplingParams`）与 `/v1/audio/generate`（扩散音频，`OmniDiffusionSamplingParams`）是两个本质不同的端点；TTS 靠 `_find_tts_stage`/`_detect_tts_model_type` 按 `model_stage` 识别具体模型，并显式拒绝 Qwen3-Omni 这类全模态模型（应用 chat + `modalities:["audio"]`）。
- `tts_adapters` 框架（`TTSModelAdapter`/`PreparedRequest`/`SpeechServingContext`）用「一模型一 adapter」把 TTS 的请求校验与 prompt/参数构造从共享 serving 文件中剥离，分 `ARTTSAdapter`/`DiffusionTTSAdapter` 两个后端基类；`has_inline_ref_audio` 必须在 `validate` 前抓取是该框架的关键顺序约束。

## 7. 下一步学习建议

- **流式与实时能力**：本讲的端点大多是一次性或简单流式。下一步读 [u6-l3（流式输出与实时/全双工）](u6-l3-streaming-realtime.md)，了解 `serving_video_stream`、`realtime_connection`（WebSocket 全双工音频）、`duplex_capability` 如何在端点层支撑实时音视频交互。
- **TTS 的工程细节**：若你对 TTS 感兴趣，建议读 [u9-l3（添加 TTS 模型）](u9-l3-add-tts-model.md) 与 `docs/contributing/model/adding_tts_model.md`，看如何为一个新 TTS 模型编写完整的 adapter 并接入流式/CUDA graph 加速。
- **多阶段模型的端点**：本讲的 chat 端点多阶段分支只是入口，完整的「请求如何被拆成 Thinker/Talker/Code2wav 三个 stage」在 [u3-l1 ~ u3-l3](u3-l1-async-omni-architecture.md) 已讲透，可回头对照 Qwen3-Omni 的 `examples/offline_inference/qwen3_omni/end2end.py` 理解端点与 stage 的对应。
- **图像/视频专属端点**：除了 chat，omni 还有 `/v1/images/generations`、`/v1/images/edits` 等图像专属端点（接受 top-level 生成参数而非 `extra_body`），可读 `docs/serving/image_generation_api.md` 与 `image_edit_api.md` 补全图像端点全景。

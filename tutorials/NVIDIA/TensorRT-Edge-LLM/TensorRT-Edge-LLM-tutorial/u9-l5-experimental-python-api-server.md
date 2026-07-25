# 实验性 Python API 与 OpenAI 服务端

## 1. 本讲目标

前几讲我们一直在用「命令行 + C++」的方式驱动 EdgeLLM：`tensorrt-edgellm-export` 导出 ONNX，`llm_build` 编译 engine，`llm_inference` 跑推理。这条链路完整、生产可用，但对应用开发者而言门槛偏高——每次都要写 JSON、编译 C++ 二进制。

本讲打开 `experimental/` 目录，讲清楚 NVIDIA 在「纯 C++ 流水线」之外**额外提供的一层 Python 高级封装**：它模仿 vLLM 的「一行 `LLM(model=...)`」体验，把「导出 → 构建 → 加载 → 生成 → 流式 → OpenAI 服务」串成一条 Python 可调用的链，底层仍落到我们 u5-l1 学过的 `LLMInferenceRuntime::handleRequest`。

读完本讲，你应该能够：

- 理解 `LLM` 类如何用 `model` / `onnx_dir` / `engine_dir` 三种来源初始化，以及它如何按需触发 export 与 build。
- 掌握 `SamplingParams` 的字段，会写出 `generate`（同步批量）与 `generate_stream`（流式）的最小调用。
- 看懂 pybind 绑定 `_edgellm_runtime` 如何把 C++ 的 runtime / request / response / streaming / builder 暴露成 Python 可用对象，并理解 GIL 释放与 CUDA 流管理。
- 说明 `api_server.py` 如何把一个 OpenAI 风格的 `/v1/chat/completions` 请求，逐步翻译到底层 `handleRequest`，并标注它为什么被标记为 `experimental`。

## 2. 前置知识

本讲默认你已经读过以下讲义，下面只做最小回顾：

- **u5-l1（LLMInferenceRuntime 与 handleRequest）**：所有 LLM 推理（普通自回归、EAGLE/MTP/DFlash 投机解码、多模态）的统一入口是 C++ 类 `LLMInferenceRuntime`，它的心脏是 `handleRequest(request, response, stream)`，分「准备 → prefill → 解码循环」三阶段；运行时不自建 CUDA 流，须由调用方传入 non-blocking 流。
- **u5-l2（请求/响应数据模型与流式）**：请求分三层 `Message → Request → LLMGenerationRequest`，响应是 `LLMGenerationResponse`；流式有轻量 `TokenCallback` 与完整 `StreamChannel`（MPSC 队列）两条通道，终止靠 `FinishReason` 枚举。
- **u1-l5（端到端流水线实战）**：三段式流水线 `检查点 → ONNX → engine → 推理`，`llm_inference` 用 non-blocking stream 构造 runtime、捕获 CUDA graph、逐请求 `handleRequest`。

三个贯穿本讲的关键直觉：

1. **薄封装而非重写**：`experimental/server` 几乎不重新实现任何推理逻辑，它只是把 C++ 运行时「包」进 Python，把 OpenAI/ vLLM 的字段「翻译」成 `LLMGenerationRequest`。真正的活仍由 C++ 干。
2. **数据来源决定阶段数**：传 `engine_dir` 只加载（一步）；传 `onnx_dir` 会构建（两步）；传 `model`（HF 检查点）会先导出再构建再加载（三步）。同一个 `LLM` 类，靠初始化路径自动补齐缺的阶段。
3. **Python 只做编排与翻译**：采样参数、chat 模板、消息结构、logprobs 格式这些「面向人类/HTTP 的东西」在 Python 里处理；分词、KV cache、采样 kernel、投机解码这些「面向性能的东西」全在 C++ 里。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| `experimental/server/engine.py` | vLLM 风格高级 API | `LLM` 类、三种初始化路径、`generate` / `generate_stream`、`_make_generation_request` 的字段翻译 |
| `experimental/server/__init__.py` | 包导出 | `LLM` / `SamplingParams` / `CompletionOutput` / `StreamDelta` 四个公共符号 |
| `experimental/server/engine_layout.py` | 目录布局约定 | `detect_engine_type`、各 `validate_*`、ONNX/engine 目录契约 |
| `experimental/server/api_server.py` | OpenAI 兼容服务端 | FastAPI app、`/v1/chat/completions`、SSE 流式、`finish_reason` 映射 |
| `experimental/server/__main__.py` | `python -m` 入口 | 转发到 `api_server.main` |
| `experimental/pybind/edgellm_pybind.cpp` | pybind11 绑定 | `PyLLMRuntime` 封装、request/response/streaming/builder 绑定、GIL 释放 |
| `experimental/pybind/CMakeLists.txt` | 绑定构建 | 两种构建模式、`--whole-archive` 链接 `edgellmCore` |
| `experimental/server/setup_pybind.py` | setuptools 构建脚本 | 用 `CMakeExtension` 驱动 CMake 出 `.so` |
| `docs/source/user_guide/examples/experimental-server.md` | 官方使用文档 | API 用法、curl 示例、参数表 |

## 4. 核心概念与源码讲解

### 4.1 LLM 类与三种初始化来源（engine.py）

#### 4.1.1 概念说明

`experimental/server/engine.py` 的设计目标是「模仿 vLLM 的一行 API」——用一个 `LLM` 类把 EdgeLLM 三段式流水线藏起来。它的核心是一句约定：**调用者只需提供「数据从哪一阶段开始」**，剩下的阶段由 `LLM` 自动补齐：

- 给一个 HuggingFace 检查点（`model=`）→ 先导出 ONNX、再构建 engine、最后加载。
- 给一个已存在的 ONNX 目录（`onnx_dir=`）→ 跳过导出，只构建 + 加载。
- 给一个已构建好的 engine 目录（`engine_dir=`）→ 直接加载，不导出不构建。

这正是把 u1-l5 里「手动跑 export → llm_build → llm_inference 三条命令」收敛成「构造一个 Python 对象」。

#### 4.1.2 核心流程

`LLM.__init__` 的执行流程可以用下面这段伪代码概括：

```
LLM(model | onnx_dir | engine_dir):   # 三选一，互斥
  校验: 三者恰好传一个，否则 ValueError
  记录投机解码参数 (eagle_engine_dir, draft_top_k, ...)
  if engine_dir:   _init_from_engine(...)      # 只加载
  elif onnx_dir:   _init_from_onnx(...)         # 构建 + 加载
  else:            _init_from_model(...)         # 导出 + 构建 + 加载
  _load_runtime()                                  # 拿到 self._rt (pybind 模块)
```

关键点是「**已存在则复用**」：导出与构建都先检查目标产物（`model.onnx` / `llm.engine`）是否已存在，存在就跳过，避免重复劳动。构建产物被缓存在检查点目录下的 `.edgellm/` 子目录里，并以配置参数打标签（如 `i4096_b1_kv8192`），换一组 `max_input_len/max_batch_size/max_kv_cache_capacity` 就换一个标签目录、各自独立缓存。

`_init_from_onnx` 还会做一件聪明事：它**委托回 `_init_from_onnx`** 复用构建逻辑，避免导出路径与构建路径写两份代码。`_init_from_model` 先导出，再调用 `_init_from_onnx`——这是把「三步」拆成「一步 + 复用两步」。

#### 4.1.3 源码精读

先看 `__init__` 的「三选一互斥」校验与三分支派发：

[experimental/server/engine.py:392-425](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/engine.py#L392-L425) —— 用 `sum(bool(s) for s in (model, onnx_dir, engine_dir))` 一行算出传了几个来源，必须恰好为 1；随后按 `engine_dir → onnx_dir → model` 的顺序选中对应初始化路径，最后统一调用 `self._load_runtime()`。

再看「已存在则复用」的缓存逻辑（以 ONNX 来源为例）：

[experimental/server/engine.py:499-522](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/engine.py#L499-L522) —— `_engine_dir` 由「artifacts 目录 + `engine/` + 配置标签 + `llm`」拼出；只有当 `llm.engine` 不存在时才调用 `self._build_engine()`，否则打日志复用。`_engine_config_tag` 用 `i{max_input_len}_b{max_batch_size}_kv{max_kv_cache_capacity}` 当目录名，正是缓存键。

而「真正的加载」发生在 `_load_runtime`——这是 Python 与 C++ 的接缝：

[experimental/server/engine.py:570-600](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/engine.py#L570-L600) —— 先 `_import_runtime()` 拿到 pybind 模块 `self._rt`；再据 `eagle_engine_dir` 是否为空，选择「投机解码五参数构造」还是「vanilla 三参数构造」`self._rt.LLMRuntime(...)`；最后调 `capture_decoding_cuda_graph()` 预录 CUDA graph（对应 u5-l1 讲过的可选优化）。

「engine 来源」路径还会自动探测视觉引擎——这是 `engine_layout.py` 的职责：

[experimental/server/engine.py:431-479](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/engine.py#L431-L479) —— `detect_engine_type` 先判定目录是普通 LLM、VLM 还是 spec-decode（靠里面有没有 `spec_base.engine` / `llm.engine` / `visual.engine`）；若是 spec-decode，就把 `engine_dir` 同时当成 `eagle_engine_dir` 透传，走投机解码路径；若用户没显式给 `visual_engine_dir`，就用 `find_visual_engine_dir` 在同级目录里自动找。

> 一句话：`LLM` 的「智能」全在初始化分发——它替你判断该跑哪几个流水线阶段、产物缓存在哪、要不要启用投机解码，而真正干活的 C++ runtime 只在最后一步 `_load_runtime` 才被请出来。

#### 4.1.4 代码实践

**实践目标**：用 `engine_dir` 来源加载一个已构建好的引擎（最轻量路径），并验证三种来源的互斥校验。

**操作步骤**：

1. 先用 u1-l5 / u4-l3 的 `llm_build` 产出一个 `engine_dir`（内含 `llm.engine` + `config.json` + tokenizer + `processed_chat_template.json`）。
2. 写一段最简加载脚本（**示例代码**）：

```python
# 示例代码：用 engine_dir 直接加载，不触发任何 export/build
from experimental.server import LLM
llm = LLM(engine_dir="/path/to/llm_engine")
print("model_dir =", llm.model_dir)
print("engine_dir =", llm.engine_dir)
print("has_draft_model =", llm.has_draft_model)   # vanilla 引擎应为 False
```

3. 故意同时传两个来源，观察报错：

```python
# 示例代码：触发互斥校验
LLM(model="Qwen/Qwen3-1.7B", engine_dir="/path/to/engine")
```

**需要观察的现象**：
- 步骤 2：日志应只出现 `Loading TensorRT engine from ...` 与 `Engine loaded and ready.`，**没有** `Exporting ONNX` 或 `Building TensorRT engine`——说明走了「只加载」分支。
- `has_draft_model` 是个 property，它转发到底层 `self._runtime.has_draft_model()`（见 4.3）。

**预期结果**：步骤 2 正常加载；步骤 3 抛出 `ValueError: Exactly one of 'model', 'onnx_dir', or 'engine_dir' must be provided.`

#### 4.1.5 小练习与答案

**练习 1**：传 `model="Qwen/Qwen3-1.7B"` 时，如果之前已经跑过一次导出，第二次构造会重新导出 ONNX 吗？

**参考答案**：不会。`_init_from_model` 在导出前检查 `os.path.join(self._onnx_dir, "model.onnx")` 是否存在（[engine.py:546-549](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/engine.py#L546-L549)），存在就打 `Using cached ONNX` 并跳过。engine 同理走缓存。

**练习 2**：为什么 `_init_from_model` 末尾要再调用一次 `_init_from_onnx`？

**参考答案**：为了复用构建逻辑。导出只产出 ONNX，之后「构建 + 加载」与「`onnx_dir` 来源」完全相同，所以委托给 `_init_from_onnx`，避免把 `_build_engine` 与缓存判断写两遍。

### 4.2 SamplingParams 与 generate / generate_stream

#### 4.2.1 概念说明

`LLM` 类提供两种推理入口：

- `generate(prompts, sampling_params)`：**同步**批量生成，返回 `List[CompletionOutput]`，一次返回整段文本。
- `generate_stream(messages, sampling_params)`：**流式**生成，返回一个 Python 生成器，逐 token `yield StreamDelta`。

二者底层最终都构造一个 `LLMGenerationRequest` 并调用 `self._runtime.handle_request(request)`——区别只在「怎么取结果」：`generate` 等它跑完读 response；`generate_stream` 在后台线程跑、用一个 `StreamChannel` 边跑边取（对应 u5-l2 讲过的 MPSC 队列）。

`sampling_params` 由 `SamplingParams` 数据类表达，字段刻意对齐 vLLM 与 OpenAI，方便迁移：

| 字段 | 默认值 | 映射到的 C++ 字段 |
|------|--------|------------------|
| `temperature` | 0.7 | `request.temperature` |
| `top_p` | 0.9 | `request.top_p` |
| `top_k` | 50 | `request.top_k` |
| `max_tokens` | 2048 | `request.max_generate_length` |
| `stop` | `[]` | `req.stop_strings` |
| `logit_bias` | `{}` | `req.logit_bias`（与投机解码互斥） |
| `num_logprobs` | 0 | `request.num_logprobs` |
| `enable_thinking` | False | `request.enable_thinking` |
| `disable_spec_decode` | False | `request.disable_spec_decode` |

#### 4.2.2 核心流程

`generate` 的流程是「逐 prompt 构造请求 → handleRequest → 解析响应」：

```
generate(prompts, params):
  把每个 prompt 归一成 messages 列表
  for messages in 批次:
      request = _make_generation_request(messages, params, ...)
      response = self._runtime.handle_request(request)   # 同步阻塞
      从 response.output_texts / output_ids / finish_reasons 取结果
      包成 CompletionOutput
```

`generate_stream` 的流程则引入「生产者-消费者」：

```
generate_stream(messages, params):
  channel = self._rt.StreamChannel.create()
  request = _make_generation_request(..., stream_channel=channel)
  def _run():  self._runtime.handle_request(request)   # 后台线程（生产者）
  start worker thread
  while True:
      chunk = channel.wait_pop(timeout_ms=200)          # 主线程（消费者）
      if chunk is None: 继续等 / 若 finished 或 cancelled 则退出
      yield StreamDelta(text, token_ids, finished, ...)
      if chunk.finished: break
  worker.join()
```

注意一个关键细节：`handleRequest` 被包进后台线程，主线程则在 `channel.wait_pop` 上**阻塞等待**，靠 200ms 超时轮询 channel 的 finished/cancelled 状态。这与 u5-l1 强调的「运行时不自建流、由调用方传入 non-blocking 流」配合——non-blocking 流让 C++ 推理与 Python 取数可以真正并发。

字段翻译的核心在 `_make_generation_request`——它把 Python 的 messages（OpenAI 风格 dict）转成 C++ 的 `Message`/`Request` 对象，把 `SamplingParams` 各字段贴到 `LLMGenerationRequest` 上。

#### 4.2.3 源码精读

先看 `SamplingParams` 数据类本身：

[experimental/server/engine.py:82-94](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/engine.py#L82-L94) —— 九个字段，`stop` 与 `logit_bias` 用 `field(default_factory=...)` 避免可变默认值陷阱；`disable_spec_decode` 让你**逐请求**关掉投机解码（不必重建引擎）。

再看字段翻译的「主战场」`_make_generation_request`：

[experimental/server/engine.py:794-842](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/engine.py#L794-L842) —— 先做 `logit_bias` 校验（调 `_normalize_logit_bias` 限范围、再调 `_validate_logit_bias_spec_decode` 禁止在投机解码开启时用）；接着 `_prepare_messages_for_runtime` 把 OpenAI messages 转成 C++ `Message` 列表并加载图片/音频 buffer；最后逐字段把 `params.xxx` 写进 `request.xxx`，把 `stream_channel` 挂到 `request.stream_channels`。

其中「messages dict → C++ Message」的转换逻辑在 `_convert_messages_to_cpp`：

[experimental/server/engine.py:1074-1117](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/engine.py#L1074-L1117) —— 文本 content 造 `MessageContent("text", ...)`，图片造 `("image", path)`，音频造 `("audio", "")`（音频字节由 `_load_audio_buffers` 在带外单独解码）。这正好对应 u5-l2 讲过的 `Message(role, contents[])` 多模态结构。

同步入口 `generate` 取结果的方式：

[experimental/server/engine.py:904-926](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/engine.py#L904-L926) —— `response = self._runtime.handle_request(request)` 一行拿到完整响应；`finish_reason_name` 把 C++ 的 `FinishReason` 枚举（END_ID/LENGTH/STOP_WORDS/…）翻译成 OpenAI 字符串（`"stop"`/`"length"`/…）；logprobs 经 `_convert_logprobs` 转成 Python 数据类。

流式入口 `generate_stream` 的生产者-消费者骨架：

[experimental/server/engine.py:952-1013](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/engine.py#L952-L1013) —— `channel = self._rt.StreamChannel.create()` 建队列并 `set_skip_special_tokens(True)`；`_run` 闭包在守护线程里调 `handleRequest`，异常时记进 `error_holder` 并 `channel.cancel()`；主线程 `wait_pop(timeout_ms=200)` 轮询，`yield StreamDelta`，遇到 `chunk.finished` 退出；`finally` 里 `worker.join(timeout=5.0)` 确保线程回收，最后若有异常则重新抛出。

`FinishReason` 枚举到 OpenAI 字符串的映射表也很值得看，它体现了「C++ 枚举 ↔ HTTP 字符串」的契约：

[experimental/server/engine.py:1055-1071](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/engine.py#L1055-L1071) —— `NOT_FINISHED → None`（非终止态本不该到这，显式返回 None 让 bug 可见）；`END_ID` 与 `STOP_WORDS` 都映射 `"stop"`（OpenAI 不区分两者）；兜底 `"stop"` 兜住未来新增的未知枚举值。

> 一句话：`generate` 与 `generate_stream` 共用同一套「构造请求 → handleRequest」逻辑，差异只在结果回收方式——同步读 response，或异步从 `StreamChannel` 拽 chunk。

#### 4.2.4 代码实践

**实践目标**：用 `LLM(engine_dir=...)` 写一段流式生成代码，逐 token 打印。

**操作步骤**：

1. 准备一个已构建好的 `engine_dir`。
2. 编写并运行下面的脚本（**示例代码**）：

```python
# 示例代码：流式生成
from experimental.server import LLM, SamplingParams

llm = LLM(engine_dir="/path/to/llm_engine")

for delta in llm.generate_stream(
    [{"role": "user", "content": "用三句话介绍 TensorRT Edge-LLM。"}],
    SamplingParams(max_tokens=128, temperature=0.7),
):
    print(delta.text, end="", flush=True)
    if delta.finished:
        print("\n[finish_reason =", delta.finish_reason, "]")
```

3. 对照 `generate_stream` 源码，跟踪一个 token 从 C++ 产出（`StreamChannel::push`）到 Python 打印（`print(delta.text)`）的路径。

**需要观察的现象**：
- 文本应**逐段**出现（不是一次性吐出），说明流式管道生效。
- 最后一个 `delta` 的 `finished=True`、`finish_reason` 为 `"stop"` 或 `"length"`。
- 若中途 `Ctrl+C`：观察 `finally` 块是否把 worker 线程 join 回来（守护线程会被强杀，但正常退出路径走 `worker.join`）。

**预期结果**：逐 token 打印，末尾打印 `finish_reason`。**待本地验证**（需要本机有对应 GPU 与已构建引擎）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `generate_stream` 要用「后台线程跑 handleRequest、主线程 wait_pop」这种结构，而不是直接在主线程跑？

**参考答案**：因为 `handleRequest` 是同步阻塞调用（它要把整段生成跑完才返回）。若在主线程跑，主线程就没机会 `yield`——流式就退化成同步。放进后台线程后，C++ 边生成边往 `StreamChannel` push chunk，主线程才能边 `wait_pop` 边 `yield`，实现真正的增量输出。

**练习 2**：`SamplingParams(disable_spec_decode=True)` 在 vanilla（无 draft）引擎上会有什么效果？

**参考答案**：无效果但无害。它最终把 `request.disable_spec_decode` 设为 True，传给 u5-l4 讲过的 `DecoderRegistry::select`——vanilla 引擎本就没有投机解码器，`disableSpecDecode` 的回退路径就是 vanilla，所以行为不变。它真正的价值在「同引擎逐请求切换」：投机解码引擎上，某条请求需要 logit_bias 时用它临时关掉投机解码（见 `_LOGIT_BIAS_SPEC_DECODE_ERROR`）。

### 4.3 pybind 绑定：把 C++ 运行时暴露给 Python

#### 4.3.1 概念说明

4.1 / 4.2 里反复出现的 `self._rt.LLMRuntime(...)`、`self._rt.LLMGenerationRequest()`、`self._rt.StreamChannel`、`self._rt.LLMBuilder(...)`——这些「Python 里能直接 new 的 C++ 对象」全来自一个 pybind11 扩展模块 `_edgellm_runtime`。

这个模块由 `experimental/pybind/edgellm_pybind.cpp` 定义，它的职责是把 EdgeLLM 的 C++ 类「翻译」成 Python 类：

- **运行时**：`LLMRuntime`（对应 C++ `LLMInferenceRuntime`）、`handleRequest`、`capture_decoding_cuda_graph`、`save_system_prompt_kv_cache`、`has_draft_model`、各 `get_*_metrics`。
- **请求/响应/流式**：`Message` / `MessageContent` / `Request` / `LLMGenerationRequest` / `LLMGenerationResponse` / `LogprobEntry` / `FinishReason`（枚举）/ `StreamChunk` / `StreamChannel`。
- **构建器**：`LLMBuilderConfig` / `LLMBuilder` / `VisualBuilderConfig` / `VisualBuilder`（这样 Python 也能驱动 build 阶段）。
- **多模态工具**：`load_image_from_path/bytes`、`load_audio_buffer_from_bytes`、`load_video_from_array`、`extract_mel_to_numpy`。

注意 pybind 不是简单「逐字段透传」，它做了一层 **`PyLLMRuntime` 封装类**，目的是替 C++ runtime 管理「CUDA 流」与「插件库句柄」这两个 Python 不该直接碰的资源。

#### 4.3.2 核心流程

`PyLLMRuntime` 的职责边界可以用它的成员来表达：

```
PyLLMRuntime:
  mStream          : CudaStreamWrapper   # RAII 的 non-blocking CUDA 流
  mPluginHandle    : dlopen 句柄          # 持有 NvInfer_edgellm_plugin.so
  mRuntime         : unique_ptr<LLMInferenceRuntime>   # 真正的 C++ runtime

构造(两套重载):
  vanilla(engineDir, mmEngineDir, loraMap)         → LLMInferenceRuntime(4 参数)
  eagle(engineDir, mmEngineDir, loraMap, topK, step, tree)  → LLMInferenceRuntime(5 参数 + draftingConfig)

handleRequest(request):
  释放 GIL
  bool ok = mRuntime->handleRequest(request, response, mStream.get())
  ok ? 返回 response : 抛 ELLM_CHECK 异常
```

这与 u5-l1 讲的「运行时有两个构造函数、vanilla 与 speculative 都转发给 `initializeCommon`」完全对齐——pybind 这层只是把 C++ 的构造重载原样镜像过来。

两个对正确性至关重要的细节：

1. **CUDA 流由 pybind 自己建**：`CudaStreamWrapper` 在构造时 `cudaStreamCreateWithFlags(..., cudaStreamNonBlocking)`，正好满足 u5-l1 强调的「必须 non-blocking」。
2. **`handleRequest` 释放 GIL**：`py::call_guard<py::gil_scoped_release>()` 让 C++ 推理期间 Python 其他线程能跑（这正是 4.2 里后台线程能并发的前提）。

#### 4.3.3 源码精读

先看 CUDA 流的 RAII 封装——这是「为什么 pybind 要包一层」的第一个原因：

[experimental/pybind/edgellm_pybind.cpp:54-100](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/pybind/edgellm_pybind.cpp#L54-L100) —— 构造即 `cudaStreamCreateWithFlags(NonBlocking)`，析构即 `cudaStreamDestroy`；删除拷贝、只允许移动（与 u5-l6 讲过的 `Tensor` RAII 哲学一致），保证流有唯一所有者、不重复释放。

再看 `PyLLMRuntime` 的两个构造重载与 `handleRequest`：

[experimental/pybind/edgellm_pybind.cpp:105-133](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/pybind/edgellm_pybind.cpp#L105-L133) —— 两个构造函数都先 `loadEdgellmPluginLib()`（对应 u8-l1 讲过的 `RTLD_NODELETE` 加载插件共享库），再用 `draftingConfig` 是否传入分流到 C++ 的五参数或四参数构造；`handleRequest` 把 C++ 的「成功与否 + 输出参数 response」签名（见 [cpp/runtime/llmInferenceRuntime.h:112](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.h#L112)）翻译成 Python 期望的「直接返回 response、失败抛异常」。

请求/响应/流式结构的绑定集中在一处：

[experimental/pybind/edgellm_pybind.cpp:449-471](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/pybind/edgellm_pybind.cpp#L449-L471) —— `LLMGenerationRequest` 用一串 `.def_readwrite(...)` 把 C++ 结构体的每个字段（`requests/temperature/top_p/top_k/max_generate_length/lora_weights_name/...`）暴露成可读可写的 Python 属性——这正是 4.2 里 `_make_generation_request` 能逐字段赋值的依据；`LLMGenerationResponse` 则把 `output_ids/output_texts/logprobs/finish_reasons` 暴露出来供 `generate` 读取。

`FinishReason` 枚举与 `StreamChannel` 的绑定也很关键：

[experimental/pybind/edgellm_pybind.cpp:414-444](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/pybind/edgellm_pybind.cpp#L414-L444) —— `FinishReason` 用 `py::enum_` 暴露成 `rt_module.FinishReason.END_ID` 等；`StreamChannel` 暴露 `create/try_pop/wait_pop/is_finished/cancel/set_skip_special_tokens` 等，`wait_pop` 还带 `py::call_guard<py::gil_scoped_release>()`——阻塞取数时释放 GIL，让生产者线程能继续 push（这是 4.2 流式能并发的基础）。

最后看 `LLMRuntime` 类本身的绑定声明，注意 `handle_request` 上的 GIL 释放：

[experimental/pybind/edgellm_pybind.cpp:476-500](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/pybind/edgellm_pybind.cpp#L476-L500) —— `py::init<...>` 镜像两个构造重载；`.def("handle_request", ..., py::call_guard<py::gil_scoped_release>())` 是性能关键——C++ 推理可能耗时数秒，期间不持有 GIL，后台线程与主线程才能协作。

> 一句话：pybind 的本质是「翻译层 + 资源管家」——把 C++ 类翻成 Python 类，把 CUDA 流与插件库句柄这两个资源用 RAII 藏好，并在所有耗时调用上释放 GIL。

#### 4.3.4 代码实践

**实践目标**：绕开 `LLM` 高级类，直接用 pybind 模块 `_edgellm_runtime` 构造 runtime 与请求，理解「LLM 类只是薄封装」。

**操作步骤**：

1. 读 `experimental/server/engine.py` 顶部的 `_import_runtime()`（[engine.py:244-275](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/engine.py#L244-L275)），理解它如何按优先级在多个路径里搜索 `_edgellm_runtime*.so`。
2. 写一段「裸 pybind」调用（**示例代码**）：

```python
# 示例代码：直接用 pybind 模块，复刻 LLM.generate 的最小子集
from experimental.server.engine import _import_runtime, finish_reason_name

rt = _import_runtime()
runtime = rt.LLMRuntime("/path/to/llm_engine", "", {})   # vanilla 三参数
runtime.capture_decoding_cuda_graph()

msg = rt.Message(role="user", contents=[rt.MessageContent("text", "你好")])
req = rt.Request(messages=[msg])
request = rt.LLMGenerationRequest()
request.requests = [req]
request.temperature = 0.7
request.top_p = 0.9
request.top_k = 50
request.max_generate_length = 64

response = runtime.handle_request(request)    # 同步，已释放 GIL
print(response.output_texts[0])
print("finish_reason =", finish_reason_name(rt, response.finish_reasons[0]))
```

**需要观察的现象**：
- 这段代码与 `LLM(engine_dir=...).generate(...)` 的行为应几乎一致——差别只在 `LLM` 类额外做了 messages 归一化、logprobs 转换、tool 解析等。
- 确认 `rt.Message`、`rt.Request`、`rt.LLMGenerationRequest` 这些类型确实存在（它们正是 pybind 暴露的）。

**预期结果**：打印出模型回复与 `finish_reason`。**待本地验证**（需本机 GPU + 已构建引擎 + 已编译 `_edgellm_runtime.so`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `handle_request` 与 `wait_pop` 都加了 `py::call_guard<py::gil_scoped_release>()`，而 `Message` 的属性读写（`.def_readwrite`）没有？

**参考答案**：`handle_request` 与 `wait_pop` 是**耗时阻塞**调用（推理数秒、取数阻塞），释放 GIL 能让其他 Python 线程并发工作，这是流式生产者-消费者模型能成立的前提。而 `Message` 属性读写是极快的内存访问，持有 GIL 反而更安全（避免数据竞争），且开销可忽略，无需释放。

**练习 2**：`_edgellm_runtime` 这个 `.so` 是怎么被 Python 找到的？

**参考答案**：`_import_runtime` 先尝试 `from tensorrt_edgellm import _edgellm_runtime`（pip 安装场景）；失败则在 `BUILD_DIR/pybind`、`experimental/pybind/build`、`build/pybind`、`build/lib.*` 等路径里 `glob("*_edgellm_runtime*.so")`，找到后用 `importlib.util.spec_from_file_location` 手动加载并注册进 `sys.modules`（[engine.py:261-271](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/engine.py#L261-L271)）。

### 4.4 api_server.py：OpenAI 兼容服务端

#### 4.4.1 概念说明

`api_server.py` 在 `LLM` 类之上再叠一层 HTTP，暴露三个端点（与官方文档的 Endpoints 表一致）：

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/health` | 健康检查（返回模型路径、是否投机解码） |
| GET | `/v1/models` | 列出模型（OpenAI 风格） |
| POST | `/v1/chat/completions` | 聊天补全，支持 SSE 流式 |

它的核心是 `chat_completions` 这个路由处理函数：接收一个 OpenAI 风格的 JSON body，把它**逐步翻译**成我们在 4.2 见过的 `SamplingParams` 与 messages，最终调用同一个 `self._runtime.handle_request(request)`。换句话说，HTTP 层只是又一层「翻译 + 编排」，真正的推理入口和 Python API 完全相同。

启动方式有两种：要么 `python -m experimental.server --model ...`（`__main__.py` 转发到 `api_server.main`），要么在已有 `LLM` 对象上调 `llm.serve(port=8000)`（内部 `from .api_server import run_server`）。

#### 4.4.2 核心流程

一个非流式 `/v1/chat/completions` 请求的完整路径：

```
POST /v1/chat/completions  (OpenAI JSON body)
  ↓
chat_completions(body):
  1. 解析 temperature/top_p/top_k/max_tokens/stream/enable_thinking/disable_spec_decode
  2. 校验 logit_bias (_normalize_logit_bias + _validate_logit_bias_spec_decode)
  3. 解析 OpenAI logprobs 语义 (logprobs:bool + top_logprobs:int 0-50)
  4. 解析 stop (null | str | list[str])
  5. validate_tool_request 校验 tools/tool_choice
  6. 构造 SamplingParams(...)
  7. 调 llm_instance._make_generation_request(...)   ← 复用 engine.py 的翻译
  8. response = llm_instance._runtime.handle_request(request)
  9. 把 response 拼成 OpenAI 响应 JSON (choices/usage/finish_reason)
```

流式（`stream: true`）则走 `_generate_stream_sse`：它调用 `llm.generate_stream(...)`（4.2 那个生产者-消费者），把每个 `StreamDelta` 包成 OpenAI SSE 格式（`data: {json}\n\n`）逐块 yield，最后发 `data: [DONE]\n\n`。

这里有个**重要的设计选择**值得专门点出：`chat_completions` 没有自己重写「构造请求 → handleRequest」的逻辑，而是直接复用 `llm_instance._make_generation_request` 与 `llm_instance._runtime`。这意味着 API 层、Python 层、HTTP 层共用同一条到 C++ 的通道——`api_server.py` 真的只是「OpenAI 协议适配器」。

#### 4.4.3 源码精读

先看路由处理函数怎么把 body 字段翻译成 `SamplingParams`：

[experimental/server/api_server.py:102-185](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/api_server.py#L102-L185) —— 逐字段 `body.get("temperature", 0.7)` 取默认值；`logit_bias` 走与 engine.py **同一个** `_normalize_logit_bias` / `_validate_logit_bias_spec_decode`（注意它是从 `.engine` import 进来的，保证两层校验一致）；OpenAI 的 `logprobs`(bool) + `top_logprobs`(int) 语义被翻译成单一 `num_logprobs`（`true` 但不给 `top_logprobs` 时 `num_logprobs=1`）；`stop` 接受 null/str/list 三种形态。

关键的一步——复用 `LLM` 的请求构造与推理：

[experimental/server/api_server.py:209-237](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/api_server.py#L209-L237) —— `request = llm_instance._make_generation_request(...)` 然后 `response = llm_instance._runtime.handle_request(request)`。这就是「HTTP 请求映射到底层 handleRequest」的全部真相——没有第二条推理路径，只有这一句。注意它被 `try/except (ValueError, KeyError)` 与 `except Exception` 两层包住，分别返回 400 与 500。

响应拼装成 OpenAI 形态：

[experimental/server/api_server.py:249-269](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/api_server.py#L249-L269) —— 注意 `prompt_tokens` 被硬编码为 0（代码注释解释：运行时 response 不暴露已分词的 prompt ids），`total_tokens` 因此等于 `completion_tokens`；`finish_reason` 经 `finish_reason_name` 翻译，若有 tool_calls 则改写为 `"tool_calls"`。

流式分支的入口与 SSE 包装：

[experimental/server/api_server.py:191-207](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/api_server.py#L191-L207) —— `stream=True` 时返回 `StreamingResponse(_generate_stream_sse(...), media_type="text/event-stream")`，正是 OpenAI 流式客户端期望的 `text/event-stream`。

`_generate_stream_sse` 怎么把 `StreamDelta` 转成 SSE chunk：

[experimental/server/api_server.py:331-401](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/api_server.py#L331-L401) —— 先发一个 `{"role": "assistant"}` 的起始 chunk；遍历 `llm_instance.generate_stream(...)`，对每个 delta 用 `_ThinkingStateMachine` 把文本按 `<think>...</think>` 边界拆成 `content` / `reasoning` 两路字段（这是 Qwen 风格思维链的协议适配）；末尾发带 `finish_reason` 的终止 chunk 与 `[DONE]`。

最后，CLI 入口 `main` 暴露了哪些命令行参数：

[experimental/server/api_server.py:567-635](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/api_server.py#L567-L635) —— `--model/--host/--port/--max-input-len/--max-batch-size/--max-kv-cache-capacity` 与投机解码三参数 `--spec-decode-engine-dir/--draft-top-k/--draft-step/--verify-tree-size`；构造 `LLM(...)` 后调 `llm.serve(...)`。

> 一句话：`api_server.py` 是个纯粹的「OpenAI 协议适配器」——它把 HTTP body 翻译成 `SamplingParams`，把 SSE 翻译成 `StreamDelta`，但真正调用 `handleRequest` 的那一句与 Python API 完全相同。

#### 4.4.4 代码实践

**实践目标**：启动服务端，用 curl 触发 `/v1/chat/completions`，并对照源码追踪「请求 → handleRequest」的映射。

**操作步骤**：

1. 先构建好引擎（或用 `model=` 让 `LLM` 自动导出+构建），再启动服务（**示例命令**）：

```bash
# 示例命令：从检查点一键起服务（会自动 export + build + load + serve）
python -m experimental.server --model Qwen/Qwen3-1.7B --port 8000
```

2. 另开终端发请求（**示例命令**，来自官方文档）：

```bash
curl -sN http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello!"}], "max_tokens": 64}'
```

3. 对照源码画一张映射表：curl body 的 `temperature` → `body.get("temperature")` → `SamplingParams.temperature` → `request.temperature` → C++ `LLMGenerationRequest::temperature` → 采样 kernel。

**需要观察的现象**：
- `/health` 返回的 `speculative_decoding` 字段，与 `llm.has_draft_model` 一致（它就是读这个 property）。
- 非流式响应里 `usage.prompt_tokens` 为 0（源码注释说明的原因）。
- 加 `"stream": true` 后，curl 会逐块收到 `data: {...}\n\n`。

**预期结果**：返回 OpenAI 风格 JSON，`choices[0].message.content` 为模型回复。**待本地验证**（需本机 GPU + 已构建引擎）。

**为什么是 experimental？** 请在阅读源码后自行归纳，参考答案见下。

#### 4.4.5 小练习与答案

**练习 1**：`api_server.py` 里「请求映射到底层 handleRequest」的核心是哪一行？为什么说它没有「第二条推理路径」？

**参考答案**：核心是 [api_server.py:217](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/api_server.py#L217) 的 `response = llm_instance._runtime.handle_request(request)`——其中 `request` 由 `llm_instance._make_generation_request(...)` 构造（[api_server.py:210](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/api_server.py#L210)）。这与 `LLM.generate` 内部用的是**完全相同的两步**，所以 HTTP 层只是协议适配，推理仍走唯一一条 C++ 通道。

**练习 2（综合）**：综合本讲全部源码，列出「experimental」的至少三条具体理由。

**参考答案**：
1. **API 不稳定**：官方文档首句即标 `Status: Experimental. API may change between releases.`，公共符号（`LLM`/`SamplingParams`）字段随时可能调整。
2. **依赖未默认安装**：FastAPI/uvicorn/pybind11 属于 `pyproject.toml` 的 `server` 可选依赖（需 `pip install -r requirements-server.txt`），不在基础导出依赖里。
3. **能力尚有缺口**：如非流式响应 `prompt_tokens` 硬编码为 0（注释明说运行时暂不暴露已分词 prompt ids），消费方按此算成本会失真；这类「待补全」语义正是 experimental 的典型特征。

## 5. 综合实践

把本讲四个模块串起来，完成一个「从零到 HTTP 服务」的最小闭环，并回答本讲的核心问题。

**任务**：选一个你本机能跑的小模型（如 `Qwen/Qwen3-1.7B`），完成以下步骤，**每一步都对照源码说清楚「调到了哪个函数」**：

1. **构建阶段**：用 `LLM(model=...)` 触发 export + build，观察日志确认走了 `_init_from_model → _export_onnx → _init_from_onnx → _build_engine → _load_runtime`。对照 [engine.py:524-600](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/engine.py#L524-L600)。
2. **同步推理**：调 `llm.generate(["你好"], SamplingParams(max_tokens=32))`，在 `_make_generation_request`（[engine.py:794](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/engine.py#L794)）处确认 `SamplingParams` 字段被逐个贴到 `request`。
3. **流式推理**：改用 `llm.generate_stream(...)`，在 pybind 的 `wait_pop`（[edgellm_pybind.cpp:435](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/pybind/edgellm_pybind.cpp#L435)）处理解「为什么阻塞取数要释放 GIL」。
4. **HTTP 服务**：调 `llm.serve(port=8000)`，用 curl 发 `/v1/chat/completions`，在 `chat_completions`（[api_server.py:217](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/experimental/server/api_server.py#L217)）确认它最终调的是与第 2 步**同一个** `handleRequest`。
5. **总结**：用一句话回答「Python API、流式 API、HTTP 服务三者共用什么？」——答案是「同一个 pybind 暴露的 `LLMRuntime.handle_request`」。

若本机无 GPU：把每一步的**命令/代码组装完整**，并写出「预期日志关键字」（如 `Exporting ONNX`、`Building TensorRT engine`、`Engine loaded and ready.`、`Starting server on ...`），作为静态验证。

## 6. 本讲小结

- `experimental/server` 是叠在 C++ 流水线之上的**薄封装**：`LLM` 类用 `model` / `onnx_dir` / `engine_dir` 三种来源（互斥）决定跑几个流水线阶段，已存在产物会缓存复用，配置参数打标签区分缓存目录。
- `generate` 是同步批量入口（直接读 `LLMGenerationResponse`），`generate_stream` 是流式入口（后台线程跑 `handleRequest` + `StreamChannel` MPSC 队列 + 主线程 `wait_pop` 轮询）；二者共用 `_make_generation_request` 做字段翻译。
- `SamplingParams` 字段刻意对齐 vLLM/OpenAI，逐字段映射到 C++ `LLMGenerationRequest`；`finish_reason_name` 把 C++ `FinishReason` 枚举翻译成 OpenAI 字符串。
- pybind 模块 `_edgellm_runtime`（`edgellm_pybind.cpp`）把 C++ runtime/request/response/streaming/builder 暴露成 Python 类；`PyLLMRuntime` 封装类用 RAII 管 CUDA 流（non-blocking）与插件库句柄，并在 `handle_request`/`wait_pop` 上释放 GIL。
- `api_server.py` 是 OpenAI 协议适配器：`/v1/chat/completions` 把 body 翻译成 `SamplingParams`，**复用** `llm._make_generation_request` 与 `llm._runtime.handle_request`，没有第二条推理路径；流式用 `text/event-stream` + `StreamDelta`。
- 标记 experimental 的依据：API 不稳定、依赖未默认安装（`server` 可选依赖）、能力有缺口（如 `prompt_tokens=0`）。

## 7. 下一步学习建议

- **回到 C++ 深处**：若想理解 `handleRequest` 内部到底怎么 prefill + 解码循环，重温 **u5-l1**；想理解 `StreamChannel` 的 MPSC 实现细节，看 **u5-l2** 与 `cpp/runtime/streaming.cpp`。
- **多模态与音频服务化**：本讲的 `_load_image_buffers` / `_load_audio_buffers` 已支持图文与音频输入，结合 **u6-l1（多模态运行器）** 与 **u6-l2（音频与 Omni）**，可尝试用 `LLM(visual_engine_dir=...)` 起一个能看图、能听音的 OpenAI 服务。
- **投机解码服务化**：结合 **u7-l1**，尝试 `LLM(engine_dir=..., eagle_engine_dir=...)` 或 `--spec-decode-engine-dir`，对比开关投机解码时的吞吐与延迟，并用 `get_spec_decode_generation_metrics()` 量化接受率。
- **构建与定制**：pybind 还暴露了 `LLMBuilder`/`VisualBuilder`，结合 **u4-l1/u4-l3**，可在 Python 里直接驱动 build，写出自定义的「导出→构建→部署」脚本。
- **阅读官方文档**：`docs/source/user_guide/examples/experimental-server.md` 给出了完整的 curl 示例（含 tool calls、音频 base64 inline），是本讲最好的补充材料。

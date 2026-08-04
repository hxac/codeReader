# 输入处理与 Tokenization

> 本讲承接 **u3-l4（AsyncLLM 在线引擎客户端）**。在那一讲里我们已经知道：`AsyncLLM` 是 API Server 眼中的「引擎客户端」，请求在进入后台 `EngineCore` 进程之前，会先在前端进程里被预处理。本讲就下钻到这层「预处理」——它把人类可读的文本 prompt 变成模型能吃的 `prompt_token_ids`，又把模型吐出的 token 流式还原成人类可读的文本。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚一条文本 prompt 从进入系统到变成 `prompt_token_ids` 的完整路径，以及这段处理发生在**哪个进程**里。
- 理解 `InputProcessor` 如何把原始输入校验、分词、组装成跨进程可序列化的 `EngineCoreRequest`，并认识 `session_id` 等请求级元数据如何被一路透传。
- 解释 `Detokenizer` 为什么必须**增量**解码、它是如何处理 BPE 合并与 stop 字符串的。
- 掌握 tokenizer 注册表（`vllm/tokenizers/registry.py`）如何把 `tokenizer_mode` 字符串映射到具体的 tokenizer 实现类。

## 2. 前置知识

在阅读本讲前，建议你已经具备以下认知（均在前面讲义中建立）：

- **进程边界**：V1 把 HTTP 接入、tokenize、多模态预处理等放在 **API Server（前端）进程**，把调度与 GPU 前向放在 **EngineCore / Worker（后台）进程**（见 u3-l1）。本讲的 `InputProcessor` 与 `Detokenizer` **都住在前端进程**，这是理解它们位置的关键。
- **EngineCoreRequest**：跨进程传递的请求数据结构，用 `msgspec.Struct` 定义、可序列化。前端把原始输入加工成它，再经 ZMQ 发给后台（见 u3-l4、u4-l1）。
- **SamplingParams**：承载用户采样意图的参数容器（见 u2-l4），其中的 `stop`、`skip_special_tokens`、`spaces_between_special_tokens`、`min_tokens` 等字段会直接影响 detokenizer 的行为。
- **分词（tokenization）**：把字符串切成整数 token id；**解码（detokenization）**：把整数 token id 还原成字符串。这两者互为逆操作，但因为 BPE 等子词算法的存在，二者都不能「逐 token 独立」地做。

> 术语提示：本讲中 **renderer**（渲染器）负责把 chat template / 文本渲染并分词，**InputPreprocessor** 是更底层的、对原始 prompt 做分词与多模态处理的组件，**InputProcessor** 则是它们的上层封装与引擎入口。三者层层包裹，初学时只需先记住「InputProcessor 是总入口」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vllm/v1/engine/input_processor.py](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/input_processor.py) | `InputProcessor` 类：前端进程的输入总入口，负责校验、分词委托、参数收尾，产出 `EngineCoreRequest`。 |
| [vllm/v1/engine/detokenizer.py](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/detokenizer.py) | `IncrementalDetokenizer` 及其 Fast / Slow 子类：把生成的 token 流式、增量地还原成文本，并处理 stop 字符串。 |
| [vllm/tokenizers/registry.py](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/tokenizers/registry.py) | tokenizer 注册表：把 `tokenizer_mode` 映射到具体 tokenizer 类，并提供 `get_tokenizer` 加载入口。 |
| [vllm/v1/engine/__init__.py](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/__init__.py) | 定义 `EngineCoreRequest`（跨进程请求结构），含本版本新增的 `session_id` 字段。 |
| [vllm/v1/engine/async_llm.py](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py) | `AsyncLLM` 在 `add_request` 中调用 `InputProcessor.process_inputs`，是输入处理的实际触发点。 |

## 4. 核心概念与源码讲解

本讲覆盖三个最小模块：**InputProcessor（输入总入口）**、**Detokenizer（增量还原文本）**、**tokenizer registry（分词器注册表）**。

### 4.1 InputProcessor：输入总入口

#### 4.1.1 概念说明

`InputProcessor` 是 API Server（前端）进程里的「输入翻译官」。它的输入是用户给的原始 prompt——可能是纯文本字符串、已经是 token id 的列表、或者是带图像的多模态输入；它的输出是一个干净、校验过、可跨进程序列化的 `EngineCoreRequest`，交给后台 `EngineCore` 去调度执行。

为什么需要这一层？

1. **分词是 CPU 密集且可能阻塞的**：把它放在前端进程、用独立线程池跑，可以避免卡住 asyncio 事件循环，也让前端的输入处理与后台 GPU 计算并行（呼应 u3-l1 的多进程动机）。
2. **跨进程需要扁平数据**：后台进程不持有 Python 对象引用，所有信息必须打包进可序列化的 `EngineCoreRequest`。
3. **集中校验**：长度越界、token 越词表、参数不合法等错误应当在前端就拦下，而不是把坏请求送进后台再失败。

本版本（`f0de1a6 → c2881ce`）的一个增量改动是：`process_inputs` 新增了 `session_id` 参数并透传进 `EngineCoreRequest`（见 PR #48048）。它是一个「请求级元数据」——前端只搬运、不消费，最终落到 V1 的 `Request` 对象上（见 u4-l1）。

#### 4.1.2 核心流程

`InputProcessor.process_inputs(...)` 的处理大致是六步：

```text
1. 校验  _validate_params / _validate_lora / data_parallel_rank 范围
2. 取得 processed_inputs
     - 若 prompt 已被 renderer 渲染成 dict（含 "type"）：直接用
     - 否则（原始文本/token）：调用 input_preprocessor.preprocess() 做分词
3. 校验模型输入  _validate_model_inputs（长度、词表、多模态嵌入数）
4. 收尾采样参数
     - clone() 后，若 max_tokens 未设，按 max_model_len - seq_len 推算
     - 用 generation_config / tokenizer 补全 stop_token 等默认
5. 组装多模态特征（若有 mm_kwargs → MultiModalFeatureSpec 列表）
6. 构造 EngineCoreRequest（含 session_id、cache_salt、priority 等）
```

注意第 2 步里有一个「快慢」分流：如果上层（API Server 的 chat/completion 路由）**已经**调用了 `Renderer.render_chat()` / `render_cmpl()`，那么传进来的 `prompt` 是一个已经分好词的 dict（带 `"type"` 键），`process_inputs` 走**同步**路径直接用；如果传进来的是**原始文本**（还没分词），就必须做阻塞的分词，这时 `AsyncLLM` 会改用异步包装 `process_inputs_async`，把工作丢到 renderer 的线程池上执行。

#### 4.1.3 源码精读

**构造与异步包装**：`InputProcessor.__init__` 持有一个 `renderer` 与一个底层的 `InputPreprocessor`，并在线程池上预包装出异步版本（input_processor.py:39-82）：

[InputProcessor.__init__ 包装出 process_inputs_async（input_processor.py:80-82）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/input_processor.py#L80-L82) —— `make_async(self.process_inputs, executor=self.renderer._executor)` 把阻塞的分词调用变成可在事件循环里 `await` 的协程，这是「分词不卡 asyncio」的关键。

**签名与 session_id**：`process_inputs` 的参数列表末尾新增了 `session_id: str | None = None`（input_processor.py:264）：

[process_inputs 新增 session_id 形参（input_processor.py:251-265）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/input_processor.py#L251-L265) —— 形参只用于向下透传，`process_inputs` 内部任何调度/采样逻辑都不读取它。

**分词委托**：当 prompt 是原始文本（非已渲染 dict）时，真正分词发生在底层预处理器的 `preprocess`（input_processor.py:301-304）：

[委托 InputPreprocessor.preprocess 做分词（input_processor.py:301-304）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/input_processor.py#L301-L304) —— 该方法最终调用 renderer 的 `_tokenize_singleton_prompt`，把字符串变成 `prompt_token_ids`。

**采样参数收尾**（input_processor.py:321-340）：对 `SamplingParams` 先 `clone()`，再在 `max_tokens is None` 时按 `max_model_len - seq_len` 推算默认上限，随后用 generation config 与 tokenizer 补全 EOS 等信息：

[max_tokens 默认值推算与 generation_config 收尾（input_processor.py:326-338）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/input_processor.py#L326-L338) —— 这解释了「为什么用户不填 max_tokens 也能跑」：`InputProcessor` 在前端就补上了。

**构造 EngineCoreRequest**：所有字段最终在这里打包，`session_id` 作为最后一项被透传（input_processor.py:380-396）：

[打包成 EngineCoreRequest，含 session_id（input_processor.py:380-396）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/input_processor.py#L380-L396) —— 注意 `session_id=session_id` 出现在构造参数里，意味着它进入 `EngineCoreRequest` 字段，从而能跨进程序列化送达后台。

**真实调用点**：`AsyncLLM.add_request` 根据 prompt 是否已渲染，二选一调用同步 / 异步版本，二者都把 `session_id` 一路传下去（async_llm.py:356-384）：

[add_request 中的同步与异步两条预处理路径（async_llm.py:356-384）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L356-L384) —— 已渲染 dict 走 `process_inputs`（同步），原始文本走 `await process_inputs_async`（线程池）。

**EngineCoreRequest 上的 session_id 字段**（engine/__init__.py:148）：作为 `msgspec.Struct` 的可选字段存在，`omit_defaults=True` 意味着不填时不会被序列化发送，避免无谓开销。

#### 4.1.4 代码实践

**实践目标**：确认 `session_id` 的透传链路，并理解分词发生的位置。

**操作步骤**（源码阅读型）：

1. 打开 [input_processor.py:251-265](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/input_processor.py#L251-L265)，确认 `session_id` 是 `process_inputs` 的形参。
2. 跳到 [input_processor.py:380-396](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/input_processor.py#L380-L396)，确认它被原样写入 `EngineCoreRequest`。
3. 在 [async_llm.py:356-384](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/async_llm.py#L356-L384) 中找到两处 `session_id=session_id`，确认上层传入。
4. 想要观察运行时行为，可在 `process_inputs` 的 `return EngineCoreRequest(...)` 前临时加一行 `logger.info("session_id=%s tokens=%d", session_id, len(prompt_token_ids or []))`（**示例代码，勿提交**），发一个带 `extra_body={"session_id": "abc"}` 的请求观察日志。

**需要观察的现象**：`session_id` 在 `InputProcessor` 内部从不参与分支判断或长度计算，纯粹是「搬运」。

**预期结果**：你能画出 `AsyncLLM.add_request(session_id=...) → InputProcessor.process_inputs(session_id=...) → EngineCoreRequest.session_id` 的三跳链路，且确认分词（`prompt_token_ids` 的产生）只发生在原始文本分支的 `preprocess()` 调用里。

> 若无法实际启动服务，本实践为「待本地验证」的部分仅限加日志观察；静态阅读部分可直接完成。

#### 4.1.5 小练习与答案

**练习 1**：如果用户传入的 prompt 已经是 token id 列表（`{"prompt_token_ids": [...]}`），`process_inputs` 还会做分词吗？

**参考答案**：不会。已渲染的 dict 含 `"type"` 键，会进入「直接使用」分支，`preprocess()` 不会被调用。它只做校验、参数收尾与打包。

**练习 2**：为什么 `max_tokens` 可以在用户不填的情况下正常工作？

**参考答案**：因为 `InputProcessor` 在第 4 步（[input_processor.py:326-331](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/input_processor.py#L326-L331)）用 `max_model_len - seq_len` 给出了默认值，在前端就补全了。

---

### 4.2 Detokenizer：把 token 流式还原成文本

#### 4.2.1 概念说明

模型每一步吐出的是整数 token id（例如 `1234`），而用户要看到的是文本（例如 `"Hello"`）。把 id 变回文本这件事看似简单——查表即可——但有两个关键难点，决定了 vLLM 必须用**增量（incremental）**解码：

1. **不能逐 token 独立解码**。BPE 等子词分词会把一个词切成多片，单独解码每片会得到不完整甚至错误的字符（涉及 UTF-8 字节跨 token、前导空格等）。正确做法是「在已有上下文的基础上，只解码新增的那一片」，并维护若干偏移指针避免重复解码整段。
2. **流式输出要求每来一个 token 就能给出新文本**。客户端不等整句生成完才看到内容，所以 detokenizer 要能在 token 流的任意断点产出「目前为止的增量文本」。

此外，detokenizer 还兼任**stop 字符串检测**：当生成内容里出现用户指定的停止串（如 `"</answer>"`）时，要在前端就识别出来并截断输出。

`Detokenizer` 住在前端进程，由 `OutputProcessor` 持有（每个请求一个 `RequestState`，里面挂一个 detokenizer 实例）。当后台 `EngineCore` 把新 token 经 ZMQ 送回前端时，`OutputProcessor` 调用 detokenizer 增量解码并拼出可发给客户端的文本。

#### 4.2.2 核心流程

detokenizer 的生命周期与每步动作如下：

```text
请求到达时（前端）：
  IncrementalDetokenizer.from_new_request(tokenizer, request)
    ├─ tokenizer 为 None             → IncrementalDetokenizer（空实现，跳过解码）
    ├─ 是 TokenizersBackend（fast）   → FastIncrementalDetokenizer（用原生 DecodeStream）
    └─ 其它（slow）                   → SlowIncrementalDetokenizer（纯 Python 增量算法）

每收到一批新 token（OutputProcessor 驱动）：
  update(new_token_ids, stop_terminated) -> stop_string | None
    1. 按需剔除终止 token（当 exclude stop 时）
    2. 对每个新 id 调 decode_next(id) 增量解码，拼到 output_text
    3. 维护 min_tokens / stop_buffer 偏移
    4. check_stop_strings(...) 扫描停止串，命中则截断并返回

需要取文本时：
  get_next_output_text(finished, delta)
    ├─ delta=True  → 仅返回「自上次调用以来」的新文本（流式）
    └─ delta=False → 返回（受 stop_buffer 约束的）完整文本
```

三种实现的取舍：`Fast` 版用 tokenizers 库原生的 `DecodeStream`（Rust 实现，快，且用 prompt token 做原生 prefill 预热），仅在 `tokenizers >= 0.22.0` 且拿到 `TokenizersBackend` 时启用；否则退到 `Slow` 版的纯 Python `detokenize_incrementally` 算法。

#### 4.2.3 源码精读

**工厂方法**（detokenizer.py:49-66）：根据 tokenizer 类型三选一，没有 tokenizer 时返回空实现：

[from_new_request 工厂方法三选一（detokenizer.py:49-66）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/detokenizer.py#L49-L66) —— `USE_FAST_DETOKENIZER` 在模块顶部依 `tokenizers` 版本判定，`TokenizersBackend` 是 transformers 的新 fast 后端。

**增量更新主逻辑**（detokenizer.py:96-143）：`BaseIncrementalDetokenizer.update` 是核心，逐个 id 调 `decode_next` 拼接，并在 `min_tokens` 约束下调整 stop 检查偏移，最后扫描停止串：

[update：逐 token 增量解码 + 停止串扫描（detokenizer.py:96-143）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/detokenizer.py#L96-L143) —— 注意 `min_tokens`：在尚未达到最小长度前，会把 `stop_check_offset` 持续后移，从而即便文本里碰巧出现停止串也不会提前停（对应 u2-l4 里 `min_tokens` 屏蔽 EOS 的语义）。

**取增量/完整文本**（detokenizer.py:149-165）：`get_next_output_text` 用 `_last_output_text_offset` 记忆上次发到哪儿，实现「只返回新增」；`stop_buffer_length` 用来在排除停止串时扣留末尾若干字符，等确认不是停止串的一部分再放出：

[get_next_output_text：delta 与 stop_buffer（detokenizer.py:149-165）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/detokenizer.py#L149-L165) —— 这就是流式输出能在「停止串尚未确定」时也不漏发、不误发的机制。

**Fast 版的原生 prefill**（detokenizer.py:184-187）：用 prompt token id 初始化 `DecodeStream`，把 prompt 上下文一次性喂给解码器，之后只需 `stream.step(...)` 解码新 token：

[FastIncrementalDetokenizer 用 DecodeStream 预热（detokenizer.py:184-187）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/detokenizer.py#L184-L187) —— 原生 prefill 让 fast 版天然知道前缀上下文，无需自己维护偏移指针。

**Slow 版的增量算法**（detokenizer.py:292-307）：`decode_next` 委托给 `detokenize_incrementally`，维护 `prefix_offset` / `read_offset` 两个指针，避免重复解码整段：

[SlowIncrementalDetokenizer.decode_next（detokenizer.py:292-307）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/detokenizer.py#L292-L307) —— 这正是「不能逐 token 独立解码」的工程化解决：靠偏移指针只重算末尾少数几个 token。

**停止串扫描**（detokenizer.py:310-362）：`check_stop_strings` 在「新增字符窗口」内查找停止串，并在多个停止串同时命中时选**结束最早**的那个，保证与逐 token 追加的结果一致（对推测解码一步多 token 尤为重要）：

[check_stop_strings：选最早结束的停止串（detokenizer.py:310-362）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/detokenizer.py#L310-L362)

**实际驱动点**：`OutputProcessor` 在收到后台输出后调用 detokenizer（output_processor.py:656 附近）：

[OutputProcessor 调用 detokenizer.update（output_processor.py:653-656）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/output_processor.py#L653-L656) —— 这里把「后台来的 token」与「前端 detokenizer」接起来。

#### 4.2.4 代码实践

**实践目标**：用最小例子体会「逐 token 独立解码为何出错，增量解码为何正确」。

**操作步骤**（源码阅读 + 本地验证）：

1. 阅读 [detokenizer.py:292-307](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/detokenizer.py#L292-L307)，确认 Slow 版每次只解码「新 token + 回看前缀」，而非整段。
2. 若本地装好 vLLM，写一段示例代码（**示例代码，非项目原有**）：

   ```python
   from transformers import AutoTokenizer
   tok = AutoTokenizer.from_pretrained("facebook/opt-125m")
   ids = tok("hello world", return_tensors=None)["input_ids"]

   # 错误示范：逐 token 独立解码
   wrong = "".join(tok.decode([i]) for i in ids)
   # 正确：整段解码
   right = tok.decode(ids)
   print("wrong=", repr(wrong), "right=", repr(right))
   ```

**需要观察的现象**：`wrong` 与 `right` 通常**不一致**（前导空格、BPE 片断会导致差异），从而直观说明为何 detokenizer 必须增量。

**预期结果**：你会看到逐 token 拼接丢失或多余空格，证明「增量 + 偏移指针」的必要性。

> 若本地无环境，本实践标记为「待本地验证」；纯阅读部分可直接完成。

#### 4.2.5 小练习与答案

**练习 1**：`stop_buffer_length` 是干什么用的？为什么只在 `stop` 非空且 `include_stop_str_in_output=False` 时才非零？

**参考答案**：当用户要求**不**把停止串包含在输出里时，detokenizer 无法在第一时间确定末尾的若干字符是否属于某个停止串的开头。于是它扣留「最长停止串长度 - 1」个字符不立即发出（[detokenizer.py:87-90](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/detokenizer.py#L87-L90)），等确认后再放行，避免把停止串的一部分误发给客户端。

**练习 2**：为什么 fast 版要拿 prompt token 做「原生 prefill」？

**参考答案**：因为增量解码需要前缀上下文（决定前导空格、合并等）。fast 版用 prompt id 初始化 `DecodeStream`（[detokenizer.py:184-187](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/engine/detokenizer.py#L184-L187)），让解码器一开始就「知道」整个 prompt，之后每个新 token 才能被正确解码。

---

### 4.3 tokenizer registry：分词器注册表

#### 4.3.1 概念说明

vLLM 支持很多模型，它们的 tokenizer 各不相同：大多数用 HuggingFace 的标准 fast tokenizer，但 Mistral 用自己的 tekken tokenizer、DeepSeek 自家格式、Kimi 有音频 tokenizer……为了把这些差异屏蔽掉，vLLM 用一个**注册表**把字符串形式的 `tokenizer_mode` 映射到具体的 tokenizer 类。

注册表的核心思想是「**用名字解耦**」：上层只说「我要 `mistral` 模式的 tokenizer」，注册表负责找到对应类并加载。这与 vLLM 的模型注册表（u6-l1）思路一致——都是用字符串映射到实现类。

`tokenizer_mode` 的取值里，最常用的是 `"auto"`（默认，自动探测后落到 `"hf"`）和 `"slow"`（强制用慢速 Python tokenizer）。此外还有为特定模型族预留的 `mistral` / `cohere` / `deepseek_v32` / `kimi_audio` 等。

#### 4.3.2 核心流程

从 `tokenizer_mode` 到一个可用 tokenizer 实例的流程：

```text
1. resolve_tokenizer_args(tokenizer_name, tokenizer_mode="auto", ...)
     ├─ "slow" → 改写为 mode="hf", use_fast=False
     ├─ 自动探测 Mistral 仓库（tekken.json / tokenizer.model.v*）→ mode="mistral"
     └─ "auto" 兜底 → mode="hf"
     （结果用 lru_cache 缓存，避免重复解析）

2. get_tokenizer(...)
     ├─ 可选：apply_fastokens_patch() 打 BPE 后端补丁
     ├─ TokenizerRegistry.load_tokenizer_cls(mode) → 用 resolve_obj_by_qualname 解析 "module.Class"
     ├─ 处理仓库里 tokenizer_class 错误的模型（改用 TokenizersBackend）
     └─ tokenizer_cls.from_pretrained(...)  真正加载

3. 返回 TokenizerLike 实例（交给 renderer / InputProcessor / Detokenizer 使用）
```

`_TokenizerRegistry` 是注册表本体（`register` / `load_tokenizer_cls` / `load_tokenizer`），`TokenizerRegistry` 是它的全局单例，初始化时把 `_VLLM_TOKENIZERS` 表灌进去。

#### 4.3.3 源码精读

**模式 → 类的映射表**（registry.py:42-56）：每个模式对应「(模块相对名, 类名)」二元组：

[_VLLM_TOKENIZERS 映射表（registry.py:42-56）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/tokenizers/registry.py#L42-L56) —— 例如 `"hf"` → `("hf", "CachedHfTokenizer")`、`"mistral"` → `("mistral", "MistralTokenizer")`。注意 `cohere` / `kimi_k3` / `inkling` 仍复用 HF tokenizer 类，只是 renderer 不同。

**注册表类与按名解析**（registry.py:59-89）：`load_tokenizer_cls` 用 `resolve_obj_by_qualname(f"{module}.{class_name}")` 把字符串解析成真实类对象，这是「字符串映射到实现」的落点：

[_TokenizerRegistry.load_tokenizer_cls 按全限定名解析类（registry.py:78-89）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/tokenizers/registry.py#L78-L89)

**全局单例**（registry.py:92-97）：把映射表里的模块相对名补全为 `vllm.tokenizers.<mod>`，构造出 `TokenizerRegistry` 单例供全局使用：

[TokenizerRegistry 全局单例（registry.py:92-97）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/tokenizers/registry.py#L92-L97)

**模式归一化**（registry.py:100-166）：`resolve_tokenizer_args` 处理 ModelScope 下载、`truncation_side`（generate 左截断、pooling 右截断）、`"slow"` 改写、Mistral 自动探测、`"auto"` 兜底：

[resolve_tokenizer_args 的归一化分支（registry.py:141-166）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/tokenizers/registry.py#L141-L166) —— 这里能看到 `slow → hf`、Mistral 探测、`auto → hf` 的三段处理；结果被 `lru_cache` 包裹（registry.py:169），同一组参数只解析一次。

**加载入口**（registry.py:186-264）：`get_tokenizer` 是面向使用者的总入口，串联 fastokens 补丁、类解析、错误 `tokenizer_class` 覆盖（针对 `_MODEL_TYPES_WITH_INCORRECT_TOKENIZER_CLASS`）、`from_pretrained`：

[get_tokenizer 总加载入口（registry.py:186-264）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/tokenizers/registry.py#L186-L264) —— 注意 registry.py:237-245 对 `internlm2`、`step3_vl` 等 model_type 的特殊处理：当仓库的 `tokenizer_class` 不正确时，绕过 AutoTokenizer 直接用 `TokenizersBackend`。

**配置驱动加载**（registry.py:270-283）：`cached_tokenizer_from_config` 从 `ModelConfig` 读出 tokenizer、mode、revision 等字段，喂给被缓存的 `get_tokenizer`：

[cached_tokenizer_from_config（registry.py:270-283）](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/tokenizers/registry.py#L270-L283) —— 这是模型初始化时「拿到 tokenizer」的标准路径。

#### 4.3.4 代码实践

**实践目标**：搞清 `tokenizer_mode` 字符串如何被映射到一个具体 tokenizer 类。

**操作步骤**（源码阅读型 + 本地验证）：

1. 打开 [registry.py:42-56](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/tokenizers/registry.py#L42-L56)，列出所有内置模式及其对应的类。
2. 在解释器里（若已安装 vLLM，**示例代码**）打印注册表内容：

   ```python
   from vllm.tokenizers.registry import TokenizerRegistry
   print(TokenizerRegistry.tokenizers)
   # 预期看到 {'cohere': (...), 'hf': (...), 'mistral': (...), ...}
   ```

3. 跟踪 `resolve_tokenizer_args("facebook/opt-125m", tokenizer_mode="auto")` 会落到哪个模式（应是 `"hf"`）。

**需要观察的现象**：`"auto"` 被归一化为 `"hf"`；`"slow"` 被改写并设 `use_fast=False`。

**预期结果**：你能复述「`tokenizer_mode` 字符串 → `load_tokenizer_cls` 解析类 → `from_pretrained` 加载」的三步链路。

> 若本地无环境，第 2 步为「待本地验证」；第 1、3 步纯阅读可直接完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cohere` / `kimi_k3` / `inkling` 在表里都映射到 `CachedHfTokenizer`？

**参考答案**：因为它们**做 token 操作时用的就是标准 HF tokenizer**，差异只在 chat 模板渲染（renderer）层。注册表注释里写明：`cohere` 模式只替换 renderer 的模板阶段，tokenize 仍走 HF（见 registry.py:43-45）。

**练习 2**：`resolve_tokenizer_args` 的结果为什么用 `lru_cache`（registry.py:169）？

**参考答案**：因为它涉及读仓库文件判断（Mistral 探测）、参数归一化等较重且确定性的计算；同一组 `(tokenizer_name, mode, revision, ...)` 反复解析是浪费，缓存后只算一次。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「文本 prompt 的一生」追踪任务。

**任务**：给定一句文本 prompt `"你好，vLLM"`，画出它从进入系统到最终以流式文本回到客户端的全链路，并标注每一步发生在哪个组件、哪个进程。

**建议产出**：一张时序图（文字版即可），至少包含以下节点：

1. 客户端发请求 → API Server（前端进程）。
2. `AsyncLLM.add_request` 判定 prompt 是原始文本 → `await input_processor.process_inputs_async(...)`（线程池）。
3. `InputProcessor` → `InputPreprocessor.preprocess` → renderer 分词 → 得到 `prompt_token_ids`。
4. `InputProcessor` 收尾 `SamplingParams`、打包 `EngineCoreRequest`（含 `session_id`），经 ZMQ 发给后台 `EngineCore`。
5. 后台 `EngineCore` 调度、Worker 前向、采样，把新 token 经 ZMQ 送回前端。
6. 前端 `OutputProcessor` 调用该请求的 `IncrementalDetokenizer.update(new_token_ids, ...)` 增量解码。
7. `get_next_output_text(finished=True, delta=True)` 取出新增文本，流式发回客户端。

**验收点**：

- 你能指出**分词**发生在第 3 步（前端进程、线程池）、**解码**发生在第 6 步（前端进程、OutputProcessor），二者都在前端，都不碰 GPU。
- 你能解释为什么分词与解码必须在前端：因为它们是 CPU 密集且与 HTTP 流式输出耦合的工作，放在前端能让后台专心做调度与 GPU 前向（呼应 u3-l1 的进程分工）。
- 你能说出 `session_id` 在这条链路里「只被搬运、不被消费」的性质（它最终落到后台的 `Request` 对象，见 u4-l1）。

## 6. 本讲小结

- `InputProcessor` 是前端进程的输入总入口：校验 → 委托分词 → 收尾采样参数 → 打包 `EngineCoreRequest`；本版本新增 `session_id` 形参并透传进 `EngineCoreRequest`（PR #48048）。
- 真正的分词只发生在「原始文本」分支（`InputPreprocessor.preprocess`），已是 dict 的渲染结果不再分词；阻塞分词通过 `process_inputs_async` 丢到线程池，避免卡住事件循环。
- `Detokenizer` 住在前端、由 `OutputProcessor` 驱动，负责把生成的 token **增量**还原成文本；由于 BPE 等子词算法，不能逐 token 独立解码，需靠偏移指针或原生 `DecodeStream` 维护上下文。
- detokenizer 还兼任 stop 字符串检测，靠 `stop_buffer_length` 扣留末尾字符，避免把停止串的一部分误发给客户端；`min_tokens` 会延迟停止检查。
- tokenizer 注册表用 `tokenizer_mode` 字符串映射到具体 tokenizer 类（`"auto"`→`"hf"`，`"slow"`→`"hf"`+`use_fast=False`，另有 mistral/cohere/deepseek 等专用模式），`get_tokenizer` 是加载总入口。
- 分词（tokenize）与解码（detokenize）都发生在**前端进程**、都不碰 GPU，这正是 V1 把 CPU 输入处理与 GPU 计算分到不同进程的体现。

## 7. 下一步学习建议

- **沿数据流向后**：本讲处理的是「输入进入」与「输出文本化」，下一步可读 **u5-l4（模型加载与权重加载器）** 了解 `tokenizer` 是如何在模型/worker 初始化阶段被加载并分发到各组件的。
- **沿数据流向前**：`EngineCoreRequest` 打包好后交给后台，建议接着读 **u7-l2（输出处理与解码结果）**，深入 `OutputProcessor` 如何聚合 detokenizer 的结果、处理 logprobs 与流式完成判定。
- **多模态延伸**：若你对 `process_inputs` 里第 5 步的 `MultiModalFeatureSpec` 组装感兴趣，可直接进入 **u7-l3（多模态输入处理）** 与 **u7-l4（多模态缓存与预算）**，那里会展开图像/音频如何替代占位符 token 并受 encoder budget 约束。
- **源码延伸阅读**：可顺带读 `vllm/tokenizers/detokenizer_utils.py` 中的 `detokenize_incrementally`，理解 Slow 版「前缀/读偏移」增量算法的细节；以及 `vllm/inputs/preprocess.py` 的 `InputPreprocessor`，看分词与多模态处理在更底层的实现。

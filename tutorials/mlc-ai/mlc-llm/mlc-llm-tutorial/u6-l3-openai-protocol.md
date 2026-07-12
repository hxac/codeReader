# OpenAI 兼容协议与生成配置

## 1. 本讲目标

上一讲（u6-l2）我们搞清楚了「对话模板名如何变成 `Conversation` 对象」。本讲继续沿着协议链条往下走，回答两个问题：

1. 当一个用户用 OpenAI 风格的 JSON 向 `/v1/chat/completions` 发请求时，MLC LLM 用什么数据结构来**接收**和**返回**它？
2. 用户传的 `temperature`、`max_tokens`、`stop` 这些采样参数，是如何从「OpenAI 协议字段」变成「引擎内部使用的 `GenerationConfig`」的？

学完本讲，你应当能够：

- 读懂 `openai_api_protocol.py` 中 `ChatCompletionRequest`、`ChatCompletionResponse`、`ChatCompletionStreamResponse` 这三组 Pydantic 模型，并知道它们各自的字段含义。
- 说出 OpenAI 协议字段与引擎内部 `GenerationConfig` 字段之间的映射关系（包括 `stop` → `stop_strs`、`max_tokens=None` → `-1` 这类转换）。
- 理解 `check_function_call_usage` 如何把 `tools`/`tool_choice` 翻译成对话模板里的 `function_string`，以及 `DebugConfig` 这种「非 OpenAI 标准字段」存在的意义。

## 2. 前置知识

本讲假设你已经：

- 熟悉 u6-l1 中 `Conversation` 模板协议的字段结构（`messages`、`seps`、`stop_str`、`as_prompt`）。
- 知道 u6-l2 中「注册表 + 导入即注册」的模式，以及模板最终会被序列化进 `mlc-chat-config.json`。
- 了解 Pydantic 的基本用法：`BaseModel` 定义字段、`@field_validator`（单字段校验）、`@model_validator(mode="after")`（整对象校验）、`model_dump_json(by_alias=True)`（按别名序列化）。
- 大致清楚 MLC LLM 的分层：Python `MLCEngine` / FastAPI serve 只是薄封装，真正干活的是 C++ `ThreadedEngine`（见 u1-l3 的 JSON FFI 桥）。

> 术语提示：**OpenAI 兼容协议**指的是一组与 OpenAI 官方 API 字段名、结构一致的 JSON 约定（如 `messages`、`temperature`、`stream`、`usage`）。MLC LLM 选择兼容它，这样任何用 OpenAI SDK 写的客户端都能直接连 MLC 的服务。

## 3. 本讲源码地图

本讲主要涉及三个协议文件，以及两处「消费」它们的地方：

| 文件 | 作用 |
| --- | --- |
| `python/mlc_llm/protocol/openai_api_protocol.py` | 定义所有 OpenAI 兼容的请求/响应 Pydantic 模型（chat、completion、embedding、models），以及字段级与对象级校验、function calling 的预处理。 |
| `python/mlc_llm/protocol/generation_config.py` | 定义 `GenerationConfig`——引擎**内部**使用的、与 OpenAI 字段解耦的采样配置。 |
| `python/mlc_llm/protocol/debug_protocol.py` | 定义 `DebugConfig` 与 `DisaggConfig`，这是 MLC 在 OpenAI 协议之外**额外**加的扩展字段。 |
| `python/mlc_llm/protocol/conversation_protocol.py` | （承接 u6-l1）`Conversation` 模板，本讲关注其中的 `function_string`/`use_function_calling` 字段与 `as_prompt` 中的 `{function_string}` 替换。 |
| `python/mlc_llm/serve/engine_utils.py` | `openai_api_get_generation_config` / `get_generation_config`：把 OpenAI 请求转换成 `GenerationConfig`。 |
| `python/mlc_llm/serve/engine_base.py` | `_handle_chat_completion` 中调用 `check_message_validity` 与 `check_function_call_usage` 的位置；以及 `process_function_call_output` 把模型输出还原成 `ChatToolCall`。 |

贯穿本讲的一个核心设计直觉是：**协议层（OpenAI 字段）和引擎层（GenerationConfig）是故意解耦的**。OpenAI 协议面向外部兼容、字段多且带有语义约束；`GenerationConfig` 面向引擎内部、字段精简且贴近采样算法。两者之间靠一个转换函数衔接。

## 4. 核心概念与源码讲解

### 4.1 请求/响应 Pydantic 模型

#### 4.1.1 概念说明

`openai_api_protocol.py` 是 MLC LLM 与外界对话的「合同模板」。它把 OpenAI 官方 API 文档里描述的请求体和响应体，逐字段翻译成 Python 的 Pydantic 模型。这样带来三个好处：

1. **自动校验**：非法请求（如 `frequency_penalty=10`）在进引擎前就被 Pydantic 拦下，返回 400 错误。
2. **自动序列化**：响应对象调用 `model_dump_json()` 即可变成符合 OpenAI 格式的 JSON 字符串。
3. **类型安全**：C++ 引擎收到的不是裸 dict，而是经过类型检查的结构化对象。

文件按 OpenAI 的端点分了四段：`v1/embeddings`、`v1/models`、`v1/completions`（旧式纯文本补全）、`v1/chat/completions`（对话补全，本讲重点）。每段都成对地定义了 `XxxRequest` 与 `XxxResponse`。

#### 4.1.2 核心流程

一个 chat 请求从 HTTP 进入到变成结构化对象，再吐出响应的过程：

```text
POST /v1/chat/completions  (JSON body)
        │
        ▼  FastAPI 自动解析
ChatCompletionRequest  ← 字段校验（penalty 范围、logit_bias 范围、logprobs 一致性、stream_options、debug_config）
        │
        ▼  check_message_validity / check_function_call_usage
   合法性 + function calling 预处理
        │
        ▼  转 GenerationConfig，进 C++ 引擎
   采样、生成 token
        │
        ▼  封装
ChatCompletionStreamResponse (每个 chunk)  或  ChatCompletionResponse (整段)
        │
        ▼  model_dump_json(by_alias=True)
   返回给客户端的 JSON
```

流式与非流式的区别在于返回容器：流式用 `ChatCompletionStreamResponse`（每个 chunk 的 choice 里是 `delta`），非流式用 `ChatCompletionResponse`（choice 里是完整的 `message`）。

#### 4.1.3 源码精读

**请求模型 `ChatCompletionRequest`** 是本节的绝对主角。它的字段几乎与 OpenAI 文档一一对应：

[python/mlc_llm/protocol/openai_api_protocol.py:258-284](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/openai_api_protocol.py#L258-L284) —— 定义 `ChatCompletionRequest`，包含 `messages`、采样参数、`tools`/`tool_choice`、`response_format` 以及 MLC 扩展的 `debug_config`。

注意几个设计细节：

- 大部分采样字段（`temperature`、`top_p`、`max_tokens`、`seed`）都是 `Optional`，默认 `None`。`None` 的语义是「不覆盖，用模型默认」，这与 `GenerationConfig` 里的处理方式不同（见 4.2）。
- `messages` 的元素是 `ChatCompletionMessage`，`role` 被限制为四个字面量之一：

[python/mlc_llm/protocol/openai_api_protocol.py:250-255](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/openai_api_protocol.py#L250-L255) —— `role: Literal["system", "user", "assistant", "tool"]`，非法 role 会被 Pydantic 直接拒绝。

- `debug_config` 是 MLC 自己加的字段，文件里明确注释它**不属于** OpenAI 协议（见 4.3.3）。

**字段级校验**用 `@field_validator`，针对单个字段。例如 `frequency_penalty` 必须落在 `[-2, 2]`：

[python/mlc_llm/protocol/openai_api_protocol.py:286-292](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/openai_api_protocol.py#L286-L292) —— `check_penalty_range`，超出 `[-2, 2]` 抛 `ValueError`。

注意 `if penalty_value and (...)` 的写法：当 `penalty_value` 为 `0.0` 或 `None` 时短路跳过。`0.0` 在这里是「不施加惩罚」的有效值，不需要校验范围。

**对象级校验**用 `@model_validator(mode="after")`，需要多个字段联合判断。最典型的是 `check_logprobs`，它要求「想看 top_logprobs 就必须先打开 logprobs」：

[python/mlc_llm/protocol/openai_api_protocol.py:311-320](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/openai_api_protocol.py#L311-L320) —— `top_logprobs` 必须在 `[0, 20]`（上限来自常量 `CHAT_COMPLETION_MAX_TOP_LOGPROBS = 20`，[第 21 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/openai_api_protocol.py#L21)），且 `logprobs=False` 时 `top_logprobs` 必须为 0。

`check_stream_options`（[第 322-329 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/openai_api_protocol.py#L322-L329)）和 `check_debug_config`（[第 331-346 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/openai_api_protocol.py#L331-L346)）也是同款模式：它们检查「带 `stream_options` 就必须 `stream=True`」「带 `special_request` 就必须 stream + include_usage」。

**响应模型**有两套，分别对应非流式和流式：

[python/mlc_llm/protocol/openai_api_protocol.py:408-419](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/openai_api_protocol.py#L408-L419) —— `ChatCompletionResponseChoice`（含完整 `message`）与 `ChatCompletionStreamResponseChoice`（含增量 `delta`），`finish_reason` 都是 `Literal["stop", "length", "tool_calls", "error"]`。

两者的顶层容器分别是 `ChatCompletionResponse`（[第 422-433 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/openai_api_protocol.py#L422-L433)，`object="chat.completion"`）和 `ChatCompletionStreamResponse`（[第 436-447 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/openai_api_protocol.py#L436-L447)，`object="chat.completion.chunk"`）。两者的 `created` 都用 `Field(default_factory=lambda: int(time.time()))` 自动填时间戳，`usage` 都是可选的——因为流式场景下 `usage` 只在最后一个 chunk 出现。

> 小知识：`usage.extra` 字段（[第 60 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/openai_api_protocol.py#L60)）就是 u1-l3 提到的 `prefill/decode_tokens_per_s` 速度统计的承载位置，仅在开启 debug 时返回。

#### 4.1.4 代码实践

**实践目标**：亲手构造一个 `ChatCompletionRequest`，触发它的字段校验和对象校验，并把它序列化成 JSON，直观感受「Pydantic 模型 = JSON ↔ Python 对象」的双向桥梁。

**操作步骤**：

1. 在已安装 `mlc_llm` 的环境里启动 `python`（若未安装，可只 `pip install pydantic shortuuid` 后把下面的 import 改成本地路径导入，但行为一致）。
2. 依次执行：

   ```python
   from mlc_llm.protocol.openai_api_protocol import ChatCompletionRequest, ChatCompletionMessage

   # 1) 构造一个合法请求
   req = ChatCompletionRequest(
       model="Llama-3-8B-Instruct-q4f16_1-MLC",
       messages=[
           ChatCompletionMessage(role="user", content="用一句话介绍 TVM"),
       ],
       temperature=0.7,
       max_tokens=100,
       stream=True,
   )
   print(req.model_dump_json(by_alias=True, exclude_none=True))

   # 2) 触发对象级校验：logprobs=False 却给了 top_logprobs=3
   try:
       ChatCompletionRequest(
           messages=[ChatCompletionMessage(role="user", content="hi")],
           logprobs=False,
           top_logprobs=3,
       )
   except Exception as e:
       print("校验失败：", type(e).__name__)
   ```

**需要观察的现象**：

- 第 1 步打印出一个 JSON 字符串，其中 `temperature=0.7`、`max_tokens=100`、`stream=true` 都在；`exclude_none=True` 让默认 `None` 的字段不出现在 JSON 里，这正是 OpenAI 客户端期望的「只发必要字段」。
- 第 2 步抛出 Pydantic 的 `ValidationError`（`model_validator` 把它包成校验错误），原因正是 `check_logprobs` 里 `"logprobs" must be True to support "top_logprobs"`。

**预期结果**：合法请求序列化成功；非法的 logprobs 组合在**构造对象时**就被拒绝，根本走不到引擎。这验证了「校验前置」的设计。

**若未安装 mlc_llm**：可仅安装 `pydantic`，把 `temperature/max_tokens` 字段照抄到一个本地最小 `BaseModel` 里复现校验行为；JSON 序列化部分完全一致。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ChatCompletionMessage.role` 用 `Literal[...]` 而不是 `str`？

**参考答案**：用 `Literal["system", "user", "assistant", "tool"]` 后，Pydantic 会在解析 JSON 时拒绝任何不在这四个值里的 role，把「拼写错误 / 非法 role」挡在协议层，而不是让它在引擎里引发莫名其妙的 KeyError。这是类型即文档、类型即校验的典型用法。

**练习 2**：`ChatCompletionResponse` 和 `ChatCompletionStreamResponse` 各自的 `object` 字段默认值是什么？客户端如何据此区分？

**参考答案**：分别是 `"chat.completion"` 与 `"chat.completion.chunk"`。OpenAI 客户端依据这个字段判断收到的是一次性的完整响应还是流式的一个分片（chunk）。

---

### 4.2 GenerationConfig：引擎内部的采样参数

#### 4.2.1 概念说明

`ChatCompletionRequest` 里有很多与生成**无关**的字段（`messages`、`tools`、`stream`、`user`……）。把这些都塞给引擎内部的采样循环显然不合适。于是 MLC 设计了 `GenerationConfig`——一个**只包含采样相关字段**的精简模型，它是 `Request` 对象的一部分，会被序列化成 JSON 经 FFI 传进 C++ 引擎（见 u6-l3 依赖链下游的 u9/u11）。

关键区别：

- `ChatCompletionRequest.temperature` 是 `Optional[float]`，`None` 表示「不覆盖」。
- `GenerationConfig.temperature` 也是 `Optional[float]`，但 `max_tokens` 有个特殊约定：`-1` 表示「不限长，直到撞上停止条件或模型能力上限」。

#### 4.2.2 核心流程

```text
ChatCompletionRequest / CompletionRequest
        │
        ▼  openai_api_get_generation_config(request)
   抽取 10 个采样字段，转换 stop → stop_strs，max_tokens=None → -1
        │
        ▼  get_generation_config(request, extra_stop_*)
   叠加模板/EOS 带来的额外停止串与停止 token
        │
        ▼  GenerationConfig(**kwargs)
   引擎内部统一的采样配置
        │
        ▼  generation_config.model_dump_json(by_alias=True)
   作为 Request 的一部分经 FFI 传进 C++ ThreadedEngine
```

#### 4.2.3 源码精读

**`GenerationConfig` 的字段**比 `ChatCompletionRequest` 干净得多：

[python/mlc_llm/protocol/generation_config.py:11-32](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/generation_config.py#L11-L32) —— 引擎内部用的采样配置，含 `n`、`temperature`、`top_p`、各类 `penalty`、`logprobs`、`max_tokens=-1`、`seed`、`stop_strs`/`stop_token_ids`、`response_format`、`debug_config`。

注意三个与请求模型不同的地方：

1. 多了 `repetition_penalty`、`stop_strs`、`stop_token_ids`（复数），这些是引擎实际需要的形态。
2. `max_tokens: int = -1`，不是 Optional。引擎用 `-1` 这个哨兵值表示「无限」。
3. 命名变了：OpenAI 的单数 `stop` 在这里变成了复数 `stop_strs`（因为引擎需要把模板 stop、用户 stop、function calling stop 合并成一个列表）。

**转换函数** `openai_api_get_generation_config` 负责这道翻译：

[python/mlc_llm/serve/engine_utils.py:30-60](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_utils.py#L30-L60) —— 显式列出要拷贝的 10 个字段名，逐个 `getattr`；`max_tokens` 为 `None` 时改写为 `-1`；`stop` 为字符串时包成单元素列表赋给 `stop_strs`。

这里有两处值得品味的设计：

- **白名单拷贝**：转换函数用一个写死的 `arg_names` 列表（第 33-44 行）逐个 `getattr`，而不是 `request.model_dump()`。这等于声明「我只认这 10 个字段，其余 OpenAI 字段一律丢弃」，避免把 `messages`/`tools` 这类非采样字段误传进引擎。
- **形态归一**：`stop` 字段在 OpenAI 协议里可以是 `str` 或 `List[str]`，这里统一拍平成 `List[str]` 赋给 `stop_strs`。
- **分支差异**：chat 请求直接用布尔 `logprobs`；旧式 completion 请求的 `logprobs` 是 `Optional[int]`（要返回几个），所以第 57-59 行做了 `request.logprobs is not None` 的转换。

外层 `get_generation_config` 再叠加上下文带来的额外停止条件：

[python/mlc_llm/serve/engine_utils.py:63-93](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_utils.py#L63-L93) —— 在 `openai_api_get_generation_config` 结果之上，把 `extra_stop_token_ids`（通常是 EOS token）和 `extra_stop_str`（通常是模板的 `stop_str`）追加进去，最后 `GenerationConfig(**kwargs)` 构造引擎配置。

这解释了 u6-l1 里那句「停止条件不在 `as_prompt` 内消费，而是被引擎读出作额外停止条件注入采样配置」：模板的 `stop_str` 正是在这里通过 `extra_stop_str` 进入了 `GenerationConfig.stop_strs`。

#### 4.2.4 代码实践

**实践目标**：手工模拟 `openai_api_get_generation_config` 的转换，体会 `stop` 与 `max_tokens` 这两个字段从「OpenAI 形态」到「引擎形态」的变化。

**操作步骤**：

```python
from mlc_llm.protocol.openai_api_protocol import ChatCompletionRequest, ChatCompletionMessage
from mlc_llm.serve.engine_utils import get_generation_config

req = ChatCompletionRequest(
    messages=[ChatCompletionMessage(role="user", content="hi")],
    temperature=0.7,
    max_tokens=None,          # 故意不传，看它如何变成 -1
    stop="</s>",              # 字符串形态
)

gc = get_generation_config(req, extra_stop_str=["[INST]"])
print(gc.model_dump_json(exclude_none=True))
```

**需要观察的现象**：

- 输出 JSON 里 `max_tokens` 为 `-1`（来自第 47-50 行的 `None` → `-1` 改写）。
- `stop_strs` 是 `["</s>", "[INST]"]`：前者来自用户的 `stop`（被包成列表），后者来自 `extra_stop_str`。
- `messages`/`model` 等**非采样字段完全不在** `GenerationConfig` 里。

**预期结果**：确认「OpenAI 协议字段 ⊃ GenerationConfig 字段」，且转换过程做了形态归一与哨兵值替换。运行结果待本地验证（需安装 mlc_llm）。

#### 4.2.5 小练习与答案

**练习 1**：为什么转换函数用写死的 `arg_names` 列表去 `getattr`，而不是 `request.model_dump()` 后直接 `**kwargs`？

**参考答案**：白名单方式能精确控制「哪些字段允许进入引擎」，把 `messages`、`tools`、`stream` 这些非采样字段排除在外，避免引擎拿到无关数据。同时它让 OpenAI 协议字段的变化（新增字段）不会意外影响引擎行为。

**练习 2**：`GenerationConfig.max_tokens = -1` 与 `ChatCompletionRequest.max_tokens = None` 都表示「不限制」，为什么要用两个不同的值？

**参考答案**：`None` 是「用户没指定、由系统决定」的协议层语义；`-1` 是引擎层的哨兵值，`max_tokens` 在引擎里是 `int` 类型（不可为 `None`），需要一个具体整数表示「无限」，这样采样循环只需判断 `generated >= max_tokens` 即可（`-1` 永远不会先于自然停止条件触发）。

---

### 4.3 function calling 与 debug_config 扩展

#### 4.3.1 概念说明

OpenAI 协议有两类扩展在本讲需要厘清：

1. **function calling（工具调用）**：这是 OpenAI 协议**标准内**的能力——请求里带 `tools`（声明有哪些函数）和 `tool_choice`（要不要用、用哪个），模型可以选择输出一个函数调用而非普通文本。MLC 支持它，但实现方式是「把工具描述拼进 prompt」，所以需要在请求阶段预处理。
2. **debug_config**：这是 MLC **自己加的**、不在 OpenAI 协议里的字段，用来给引擎开「后门」做调试、结构化约束、分离式推理等高级控制。

#### 4.3.2 核心流程

function calling 的两段式处理：

```text
请求阶段（check_function_call_usage）：
  tools/tool_choice  ──►  conv_template.use_function_calling = True
                          conv_template.function_string = "<工具 JSON 描述>"
            │
            ▼  as_prompt() 把 {function_string} 占位符替换成上面的描述
          拼出最终 prompt，进引擎生成

响应阶段（process_function_call_output）：
  模型输出的文本  ──►  解析成 ChatToolCall 列表  ──►  finish_reason="tool_calls"
```

#### 4.3.3 源码精读

先看 function calling 在**请求侧**的预处理。`check_function_call_usage` 做的事是：判断用户到底要不要用工具，如果要，就把工具描述序列化成字符串塞进对话模板：

[python/mlc_llm/protocol/openai_api_protocol.py:366-405](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/openai_api_protocol.py#L366-L405) —— `check_function_call_usage(conv_template)`：没有 `tools` 或 `tool_choice=="none"` 时关闭 function calling；`tool_choice` 是 dict 时强制选指定工具；`tool_choice=="auto"` 时把所有工具描述拼成 JSON 数组赋给 `function_string`。

这个函数有四个分支值得理解：

1. **无工具**（第 372-374 行）：`tools is None` 或 `tool_choice == "none"` → 关闭，直接返回。
2. **指定工具**（第 377-393 行）：`tool_choice` 是 dict（形如 `{"type": "function", "function": {"name": "get_weather"}}`）→ 在 `tools` 里找到它，只把这个工具的描述写进 `function_string`；找不到就报错。
3. **非法值**（第 395-396 行）：`tool_choice` 是字符串但不是 `"auto"` 也不是 `"none"` → 报错。
4. **自动模式**（第 398-405 行）：`tool_choice=="auto"` → 把所有工具的 `function` 描述 `model_dump(by_alias=True)` 收集成列表，`json.dumps` 后赋给 `function_string`，并打开 `use_function_calling` 开关。

注意它修改的是传入的 `conv_template`，设置两个字段：

[python/mlc_llm/protocol/conversation_protocol.py:88-90](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L88-L90) —— `function_string` 与 `use_function_calling` 两个字段，默认空串 / `False`。

这两个字段在 `as_prompt` 里被消费——把模板里的 `{function_string}` 占位符替换成实际的工具描述：

[python/mlc_llm/protocol/conversation_protocol.py:200-206](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L200-L206) —— 用 `function_string` 替换最后一个 `{function_string}` 占位符（`MessagePlaceholders.FUNCTION`），其余的占位符清空。

占位符本身定义在：

[python/mlc_llm/protocol/conversation_protocol.py:10-17](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/conversation_protocol.py#L10-L17) —— `MessagePlaceholders` 枚举，`FUNCTION = "{function_string}"` 是 function calling 的注入点。

**调用位置**在 `engine_base.py`，紧挨着 `check_message_validity`：

[python/mlc_llm/serve/engine_base.py:745-747](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L745-L747) —— 先校验消息合法性，再做 function calling 预处理，两者都发生在「消息进入对话模板之前」。

再看**响应侧**：模型生成完后，输出文本可能是一段工具调用 JSON，需要还原成结构化的 `ChatToolCall`：

[python/mlc_llm/serve/engine_base.py:1179-1213](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L1179-L1213) —— `process_function_call_output`：只要任一 `finish_reason=="tool_calls"`，就把每条输出文本用 `convert_function_str_to_json` 解析成 `ChatToolCall` 列表；解析失败则把 `finish_reason` 改成 `"error"`。

注意第 1189 行用 `any(...)` 判断是否进入 function calling 分支——只要 n 个候选里有一个判定为工具调用，全部候选都按工具调用来解析。

最后看 **`DebugConfig`**，它是 MLC 在 OpenAI 协议之外加的扩展字段：

[python/mlc_llm/protocol/debug_protocol.py:29-41](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/debug_protocol.py#L29-L41) —— `ignore_eos`（忽略 EOS 继续生成）、`pinned_system_prompt`（钉住系统提示）、`special_request`（如 `query_engine_metrics` 走特殊引擎路径）、`grammar_execution_mode`（结构化解码的执行模式）、`disagg_config`（分离式推理元数据，见 u12-l3）。

它的设计哲学写在类注释里：**这些选项对引擎可见，但默认不暴露给服务端点**，只有启动时显式传 `--enable-debug` 才会放开（参见 `openai_entrypoints.py` 中 `request.debug_config = None` 的清空逻辑）。这样既能在调试/内部使用时拥有后门，又不会让普通用户绕过限制。

`DebugConfig` 里的 `special_request` 受到严格约束——前面 4.1.3 提到的 `check_debug_config` 就要求它必须配合 `stream=True` 且 `stream_options.include_usage=True`，因为它要把特殊结果塞在 `usage` 字段里返回。

#### 4.3.4 代码实践

**实践目标**：构造一个带 `tools` 的请求，调用 `check_function_call_usage`，观察它如何修改对话模板，从而把「OpenAI 的 tools 字段」变成「prompt 里的一段工具描述文本」。

**操作步骤**：

```python
import json
from mlc_llm.protocol.openai_api_protocol import (
    ChatCompletionRequest, ChatCompletionMessage, ChatTool, ChatFunction,
)
from mlc_llm.conversation_template.registry import get_conv_template

# 1) 取一个支持 function calling 的模板（llama-3_1 带 tool 角色）
conv = get_conv_template("llama-3_1")

# 2) 构造带工具的请求
req = ChatCompletionRequest(
    messages=[ChatCompletionMessage(role="user", content="北京天气如何？")],
    tools=[ChatTool(type="function", function=ChatFunction(
        name="get_weather",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}},
    ))],
    tool_choice="auto",
)

# 3) 预处理 —— 这一步会修改 conv
req.check_function_call_usage(conv)
print("use_function_calling =", conv.use_function_calling)
print("function_string =")
print(json.dumps(json.loads(conv.function_string), indent=2, ensure_ascii=False))
```

**需要观察的现象**：

- `conv.use_function_calling` 从默认的 `False` 变成 `True`。
- `conv.function_string` 变成一段 JSON 数组字符串，里面是 `get_weather` 工具的描述（带 `name`、`parameters`）。
- 这段字符串随后会被 `as_prompt` 拼进最终 prompt 的 `{function_string}` 位置——也就是说，模型其实是通过「读到一段工具说明书」来决定要不要调用工具的。

**说明 `check_function_call_usage` 在启用 function calling 时做了什么**（本讲实践任务的第二问）：

1. **判定开关**：确认请求带了 `tools` 且 `tool_choice` 不是 `"none"`，把对话模板的 `use_function_calling` 置为 `True`；否则置 `False` 并返回。
2. **选择工具**：若 `tool_choice` 是 dict，只选其中指定的那个工具（找不到或多个都报错）；若是 `"auto"`，则收集全部工具。
3. **序列化注入**：把选中的工具描述用 `model_dump(by_alias=True)` 序列化后 `json.dumps` 成字符串，写入模板的 `function_string` 字段。
4. **下游消费**：随后 `as_prompt` 把模板里的 `{function_string}` 占位符替换成这段字符串，最终拼进 prompt 喂给模型。模型若输出工具调用文本，`process_function_call_output` 再把它解析回 `ChatToolCall`。

**预期结果**：能清晰看到「tools 字段 → function_string → prompt 占位符」的完整数据流。运行结果待本地验证（需安装 mlc_llm，且模板名以本地 `registry` 实际注册为准；若 `llama-3_1` 不可用，改用任意带 `{function_string}` 占位符的模板）。

#### 4.3.5 小练习与答案

**练习 1**：`tool_choice="auto"` 和 `tool_choice={"type":"function","function":{"name":"get_weather"}}` 在 `check_function_call_usage` 里的行为有何不同？

**参考答案**：`"auto"` 会把 `tools` 里**所有**工具的描述都收集起来拼成 `function_string`，让模型自由选；dict 形式则**只**把指定的那一个工具写进 `function_string`（强制模型只能调它），且如果指定的名字不在 `tools` 列表里会直接抛 `BadRequestError`。

**练习 2**：为什么 `DebugConfig` 不直接做成 OpenAI 协议的标准字段，而是要靠 `--enable-debug` 才放开？

**参考答案**：`DebugConfig` 里的 `ignore_eos`、`special_request`、`disagg_config` 等都是「引擎后门」，会让请求走非常规路径（比如不正常停止、返回 metrics 而非文本、跨实例传 KV）。把它们默认对外关闭，可以防止普通用户绕过服务的正常约束（如生成长度上限、安全停止），同时又方便开发者与内部场景（调试、分离式推理）按需开启。`check_debug_config` 还对 `special_request` 追加了 stream + include_usage 的硬约束，确保结果能经 `usage.extra` 回传。

## 5. 综合实践

把本讲三个最小模块串起来，模拟「一个请求从 JSON 进、到 GenerationConfig 出、到 function calling 预处理完成」的完整协议侧链路。

**任务**：写一个 Python 脚本，完成以下步骤并打印每一步的中间结果。

1. 构造一个 `ChatCompletionRequest`：`temperature=0.7`、`max_tokens=100`、`stream=True`、带一个 `get_weather` 工具、`tool_choice="auto"`，并故意把 `frequency_penalty` 设为 `5`，先观察它在校验阶段是否被拦下。
2. 把 `frequency_penalty` 改回合法值（如 `0.5`）重新构造；调用 `get_generation_config(req, extra_stop_str=["<end>"])` 打印出生成的 `GenerationConfig`，确认 `max_tokens=100`、`stop_strs` 包含 `"<end>"`、且不含 `messages`/`tools`。
3. 取一个对话模板，调用 `req.check_function_call_usage(conv)`，打印 `conv.function_string`，确认工具描述已被注入模板。

**验收要点**：

- 步骤 1 应抛出 `frequency_penalty` 超范围的校验错误（来自 [第 286-292 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/openai_api_protocol.py#L286-L292)）。
- 步骤 2 的 `GenerationConfig` 应当是「干净」的采样配置，证明协议层与引擎层的解耦。
- 步骤 3 的 `function_string` 应包含 `get_weather` 的 JSON 描述，证明 OpenAI 的 `tools` 字段最终被翻译成了 prompt 文本。

> 若本地未安装 `mlc_llm`，可只 `pip install pydantic shortuuid`，把上述模型与函数照抄到本地最小复现脚本，行为一致。完整运行结果待本地验证。

## 6. 本讲小结

- `openai_api_protocol.py` 用一组 Pydantic 模型定义了 OpenAI 兼容的请求/响应契约；`ChatCompletionRequest`、`ChatCompletionResponse`、`ChatCompletionStreamResponse` 分别对应对话请求、整段响应、流式 chunk。
- 校验分两层：`@field_validator` 校验单字段（如 penalty 范围 `[-2, 2]`、logit_bias 范围 `[-100, 100]`），`@model_validator(mode="after")` 校验多字段联合约束（如 `top_logprobs` 要 `logprobs=True`、`stream_options` 要 `stream=True`）。
- `GenerationConfig` 是引擎**内部**的精简采样配置，与 OpenAI 协议字段解耦；二者靠 `openai_api_get_generation_config` / `get_generation_config` 用**白名单**方式转换，期间 `stop`→`stop_strs`、`max_tokens=None`→`-1`。
- function calling 在请求侧由 `check_function_call_usage` 把 `tools`/`tool_choice` 翻译成模板的 `function_string`，由 `as_prompt` 注入 `{function_string}` 占位符；在响应侧由 `process_function_call_output` 把模型输出还原成 `ChatToolCall`。
- `DebugConfig` 是 MLC 在 OpenAI 协议之外**额外**加的扩展字段（`ignore_eos`、`special_request`、`disagg_config` 等），默认不对外暴露，需 `--enable-debug` 开启，用于调试与高级场景。

## 7. 下一步学习建议

本讲把「协议层」讲透了：请求怎么进来、怎么校验、怎么转成引擎配置。接下来：

- **协议如何进引擎**：建议阅读 u11-l1（MLCEngine 与 JSON FFI 桥接），看 `GenerationConfig` 如何被 `model_dump_json` 序列化后经 FFI 传进 C++ `ThreadedEngine`。
- **采样参数如何被消费**：建议进入 u10-l3（采样器：CPU 与 GPU），看 `temperature`、`top_p` 在采样循环里到底怎么作用到 logits 上。
- **function calling 的下游**：建议读 `python/mlc_llm/serve/engine_base.py` 中 `convert_function_str_to_json` 与 `wrap_chat_completion_response` 的实现，理解工具调用输出的完整还原链路。
- **REST 端点全景**：建议进入 u11-l2（REST 服务器与 OpenAI 端点），看 `/v1/chat/completions` 如何把本讲这些协议模型与 FastAPI 路由、流式响应缝合起来。

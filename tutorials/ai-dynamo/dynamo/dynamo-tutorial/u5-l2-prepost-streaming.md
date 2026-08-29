# 请求整形：prepost.py 与流式后处理

## 1. 本讲目标

上一讲（u5-l1）我们跟完了 `dynamo.frontend` 从 CLI 到 `make_engine` 的启动主流程，知道 `chat_engine_factory` 会让 Rust 在发现模型时"反向回调" Python 来构造处理器。本讲就钻进这个处理器的核心文件 `prepost.py`（约 1300 行，是前端最厚的文件），学完后你应该能：

1. 说清一条 chat 请求在 Python 侧被"整形"成了什么：`PreprocessResult` 的七个字段各自是什么、谁消费它们。
2. 追踪 `preprocess_chat_request` 的完整链路：请求校验 → 工具解析器装配 → 引导解码（guided decoding）裁决 → 模板渲染 → 分词。
3. 理解 `StreamingPostProcessor` 这个**有状态机**的流式后处理器：它如何把一串 token 增量切成 `reasoning_content` / `content` / `tool_calls` 三种 delta，并处理"标签跨 chunk 断裂"这类工程难题。
4. 亲手扩展它：实现一个识别 `<answer>...</answer>` 自定义标签的子类，并用仓库现成的"固定 token 回放 worker"验证。

## 2. 前置知识

### 2.1 前后处理发生在哪一层

回顾 u4-l1 的分层：`frontend` 进程里的 Python 处理器（`vllm_processor.py` / `sglang_processor.py`）负责**引擎无关**的请求整形与响应整形，真正的推理在远端 worker。一条 chat 请求的旅程是：

```
HTTP 请求(dict)
  → preprocess_chat_request()          # 本讲 M1/M2：OpenAI 格式 → token ids + 解析器
  → routed_engine.generate()           # 跨进程发给 worker
  ← token 增量流
  → StreamingPostProcessor.process_output()  # 本讲 M4：token → OpenAI delta
  → SSE 流式响应
```

前后处理都在 frontend 一侧完成，worker 只见 token。这就是为什么"换个后端（vLLM/SGLang/TRT-LLM）不用换前端协议"。

### 2.2 三种 delta 字段

OpenAI 的流式 chat 响应里，每个 SSE chunk 的 `choices[i].delta` 可以带：

- `content`：普通回答文本；
- `reasoning_content`：思考型模型（Qwen3、DeepSeek-R1 等）的推理文本，这是社区事实标准扩展字段；
- `tool_calls`：工具调用片段（函数名 + 增量 JSON 参数）。

模型的原始输出其实是一锅混杂的字符串，例如：

```
<think>用户要查天气，我该调用工具</think><tool_call>{"name":"get_weather","arguments":{"city":"NYC"}}</tool_call>
```

后处理器的职责就是把这锅字符串**流式地、增量地**拆成上面三个字段，同时保证 `<think>`、`<tool_call>` 这类标记本身永远不泄漏进 `content`。

### 2.3 为什么"流式"很难

非流式解析很简单：等全部文本到齐，一次性正则切分。但流式场景下，Dynamo 内部即使客户端不要流式也是逐 chunk 处理的（后面 M4 会看到），于是你必须面对：

- **标签断裂**：`<an` 和 `swer>` 分属两个 chunk，不能见 `<an` 就当成正文吐出去；
- **状态延续**：这个 chunk 处于"思考中"还是"思考已结束"，取决于之前的 chunk；
- **一次成型**：delta 一旦发出去就收不回来，错拆一个标签就是错误的响应。

这就是 `StreamingPostProcessor` 存在的意义——它是一个跨 chunk 的状态机。

### 2.4 Dynamo 大量复用 vLLM 的解析器

`prepost.py` 顶层 import 了一批 vLLM 符号（`ToolParser`、`ReasoningParser`、`ChatCompletionRequest`、`SamplingParams`……）。Dynamo 不重造工具调用/思考解析器，而是把 vLLM 的解析器族当作库来用，自己只做"编排 + 状态机 + 边界裁决"。读懂这一点，文件长度就不可怕了。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲涉及 |
|------|------|----------|
| `components/src/dynamo/frontend/prepost.py` | 前端核心：请求预处理 + 流式后处理状态机 | M1/M2/M4 |
| `components/src/dynamo/frontend/thinking.py` | 部署级"思考模式默认值"的合并规则 | M3 |
| `components/src/dynamo/frontend/utils.py` | 前后处理公共工具：模板解析、媒体 URL 提取、错误信封 | 源码地图级 |
| `components/src/dynamo/frontend/vllm_processor.py` | 消费者：调用 pre/post 处理并组装 SSE | 串联验证 |
| `tests/frontend/test_prepost.py` | `StreamingPostProcessor` 的单元测试（2078 行，最佳教材） | 实践依据 |
| `tests/frontend/vllm_prepost_worker.py` | 回放固定 token 序列的测试 worker | 实践依据 |
| `tests/frontend/test_vllm_prepost_integration.py` | 端到端集成测试（起 frontend + 固定 token worker） | 实践依据 |
| `lib/llm/src/protocols/openai/chat_completions.rs` | Rust 侧流式 delta 的**类型化**使用点（结构定义在外部 `dynamo-protocols` crate，重要边界！） | M4 边界警示 |

## 4. 核心概念与源码讲解

### 4.1 模块一：PreprocessResult 与请求校验

#### 4.1.1 概念说明

预处理要回答一个问题：**"这条 OpenAI 格式的请求，要变成哪些东西才能交给引擎？"** 答案被装进一个 dataclass：

- `request_for_sampling`：校验后的 `ChatCompletionRequest`（vLLM 类型），后续所有采样参数从它取；
- `tool_parser`：**已实例化、且已调用过 `adjust_request()` 的**工具解析器（没有工具就是 `None`）；
- `chat_template_kwargs`：渲染聊天模板时要传的额外键（思考模式开关就在这里）；
- `engine_prompt`：模板渲染的产物（字符串 prompt 或现成的 token ids）；
- `prompt_token_ids`：最终 token id 列表——这是真正发给 worker 的"提示词"；
- `guided_decoding`：引导解码约束（JSON schema / 正则 / structural_tag），可为 `None`；
- `uses_dynamo_json_tool_call_fallback`：一个隐蔽但关键的标志位，后文 M4 会用到。

#### 4.1.2 核心流程

请求校验有一条"快路径"：

```
收到请求(dict 或 ChatCompletionRequest)
  ├─ 已是 ChatCompletionRequest？→ 直接返回
  ├─ DYN_VLLM_SKIP_REQUEST_VALIDATION != "1"？→ Pydantic 全量校验
  └─ 快路径（默认）：model_construct() 跳过 Pydantic
       ├─ tools 里有裸 dict / response_format 是 dict / structured_outputs 是 dict
       │    → 这些字段必须深度校验，退回全量校验
       └─ 否则保留半校验对象，但把裸 dict 形态的 tool_choice 规范成类型化对象
```

`model_construct()` 是 Pydantic 的"不校验构造"，跳过了整棵嵌套模型的递归验证——这是热路径上的性能取舍。但代价是 `tool_choice` 可能还是客户端发来的裸 dict，而下游 vLLM 工具函数分支判断依赖类型化对象，裸 dict 会让 `get_json_schema_from_tools` **静默返回 None**（约束悄悄丢失）。所以快路径上必须补一次规范化。

#### 4.1.3 源码精读

先看结果容器（注意 `guided_decoding` 和 fallback 标志有默认值，前五个字段必填）：

[`prepost.py:54-62`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L54-L62) —— `PreprocessResult`：预处理阶段的全部产物，字段含义见上一节。

[`prepost.py:66`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L66) —— 快路径开关：`DYN_VLLM_SKIP_REQUEST_VALIDATION` 默认为 `"1"`，即**默认走跳过校验的快路径**。

[`prepost.py:386-414`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L386-L414) —— `_validate_chat_completion_request`：先判断是否已是类型化对象；快路径用 `model_construct` 构造后，检测三种"必须深度校验"的形态（裸 dict tools、dict `response_format`、dict `structured_outputs`）；最后把裸 dict 的具名 tool_choice 规范成 `ChatCompletionNamedToolChoiceParam`。

[`prepost.py:73-91`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L73-L91) —— `_is_named_tool_choice`：兼容"类型化对象"与"裸 dict"两种形态判断是否为具名强制工具选择。注释明确说明了为什么不能只写 `not isinstance(tool_choice, str)`——那会把 `{}`、`{"type": "function"}` 这类畸形值也当成强制工具选择，进而触发下面的冲突检查。

#### 4.1.4 代码实践

**实践目标**：观察快路径与全量校验的分界。

1. 读 [`prepost.py:386-414`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L386-L414)，列出三种会"退回全量校验"的请求形态；
2. 构造三个请求 dict（裸 dict tools / dict response_format / 干净请求），在你的 Python 环境里分别调用 `_validate_chat_completion_request`，用 `type(result.tools[0])` 观察产物差异。

**观察现象**：干净请求在快路径下 `tools` 里的元素可能还是 dict（未深度校验），而带裸 dict tools 的请求会退回全量校验、得到 Pydantic 模型。

**预期结果**：能说清"为什么裸 dict 的 tools 必须全量校验，而其他字段可以放过"（提示：tools 会被传给 `ToolParser` 构造函数和模板渲染，两者都要 `.model_dump()`）。

> 本实践需要安装 vLLM（`uv pip install -e '.[vllm]'`）。若本地没有 vLLM 环境，改为纯源码阅读并写出三种形态的判断分支即可。

#### 4.1.5 小练习与答案

**练习 1**：`SKIP_REQUEST_VALIDATION = True` 时，一条带 `tools`（已类型化）和 `response_format=None` 的请求会走哪条路？
**答案**：走 `model_construct` 快路径，且因为 `has_unvalidated_tools` 为 False、`response_format`/`structured_outputs` 都不是 dict，不再退回全量校验；若 `tool_choice` 是裸 dict 具名选择，会被就地规范化成类型化对象。

**练习 2**：为什么 `PreprocessResult` 里要单独存 `chat_template_kwargs`，而不是塞进 `engine_prompt`？
**答案**：`engine_prompt` 是渲染**结果**（字符串或 token ids），而 `chat_template_kwargs` 是渲染**输入参数**。后处理阶段还要用它：`StreamingPostProcessor.__init__` 会读 `chat_template_kwargs.get("enable_thinking")` 判断思考模式是否被关闭（见 M4），所以这两个产物必须分开传递。

**练习 3**：`uses_dynamo_json_tool_call_fallback` 这个字段名里的 "fallback" 指什么？
**答案**：强制工具选择（`tool_choice="required"` 或具名）但没有解析器提供的语法时，Dynamo 会退而给引擎一个"裸 JSON schema"约束，模型输出的是 Dynamo 自己定义的 JSON 线格式而非模型原生工具语法；该标志告诉后处理器**不要**用解析器的原生语法解码器去解析这段输出（见 M4 的 `_process_dynamo_json_fallback_tool_calls`）。

### 4.2 模块二：preprocess_chat_request 主链路

#### 4.2.1 概念说明

这是预处理的**总调度函数**。它协调四件事，其中"引导解码裁决"是最容易出错的部分：

1. **工具解析器装配**：有 tools（或开启了 auto tool choice）就实例化 `tool_parser_class`，并立刻调用 `adjust_request()` 让解析器改写请求（例如剥掉它不支持的 `response_format`）；
2. **引导解码裁决**：解码器只有**一个**语法槽。请求自带的约束（`guided_json` 等）、工具调用需要的约束、解析器生成的约束，三者竞争这个槽，冲突要报 400；
3. **模板渲染**：把 messages + 工具定义 + 模板参数交给 vLLM 的 renderer，得到 prompt；
4. **分词**：prompt → token ids（若模板已直接产出 token ids 则跳过）。

#### 4.2.2 核心流程

```
preprocess_chat_request(request, ...)
  1 校验请求 → validated_request
  2 算 assistant_guided_decoding（请求自带约束，按优先级取一个）
  3 算 has_explicit_output_constraint   ← 必须在 _prepare_request 之前！
  4 _prepare_request():
      a. 实例化 tool_parser + adjust_request()（原地改写请求）
      b. 决定 tool_dicts（tool_choice=none 时是否把工具从模板里剥掉）
      c. 合并 chat_template_kwargs（默认值 ← 请求值 ← reasoning_effort ← 思考模式默认）
      d. 组装 ChatParams
  5 算 parser_guided_decoding（解析器改写后新产生的约束）
  6 算 tool_guided_decoding（工具调用需要的约束）
  7 冲突检查：强制工具选择 × 显式输出约束 → raise InvalidArgument(400)
  8 最终 guided_decoding = 请求自带约束（非强制工具时）否则 工具约束
  9 render_messages_async() → engine_prompt
 10 分词（或直取 prompt_token_ids）
 11 返回 PreprocessResult
```

#### 4.2.3 源码精读

[`prepost.py:417-530`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L417-L530) —— `_prepare_request`：五元组返回（请求、解析器、模板参数、待渲染消息、ChatParams）。

[`prepost.py:450-458`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L450-L458) —— 工具解析器装配：`enable_auto_tool_choice` 打开时即使客户端没传 `tools` 也要激活解析器（模型可能自发调用工具）；实例化后**立刻** `adjust_request()` 原地改写请求。

[`prepost.py:462-470`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L462-L470) —— `tool_dicts`：当 `tool_choice="none"` 且配置了 `exclude_tools_when_tool_choice_none` 时把工具列表置 `None`，不让模型在模板里看见工具定义。

[`prepost.py:471-475`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L471-L475) —— 一个跨层细节：Rust 侧 serde 的 `alias` 是**只反序列化**的，所以 Python 收到的键名是 Rust 字段名 `chat_template_args`，必须额外读它，否则客户端传的模板参数会被静默丢弃。

[`prepost.py:479-496`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L479-L496) —— 模板参数合并：用 vLLM 的 `merge_kwargs` 让未设置的请求值保留服务端默认；`reasoning_effort` 显式写入；最后套用部署级思考模式默认（M3 详述）。

[`prepost.py:533-560`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L533-L560) —— 主函数开头。注意第 555-559 行的注释：`has_explicit_output_constraint` **必须在 `_prepare_request` 之前计算**，因为 `adjust_request()` 会原地改写同一个请求对象、可能自己设置 `structured_outputs`；放在后面就会把"解析器生成的约束"误判为"调用方发来的约束"而错误拒绝请求。这是一个典型的"顺序即语义"陷阱。

[`prepost.py:577-615`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L577-L615) —— 引导解码三方裁决 + 冲突拒绝。第 606-610 行：强制工具选择已经占用了解码器唯一的语法槽，再叠加调用方的 `guided_*`/`structured_outputs` 约束必然无法同时满足，于是抛 `InvalidArgument`（映射为 HTTP 400）而不是静默丢弃。注释同时说明了为什么 `response_format` **不参与**这个冲突判断——OpenAI 规范把它限定在"返回给用户的消息"范围内，所以对强制工具选择它是"丢弃"而非"拒绝"。

[`prepost.py:630-650`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L630-L650) —— 渲染与分词的分叉：若模板（Mistral 路径 `tokenize=True`）直接产出 `prompt_token_ids` 就直取；否则用缓存的异步分词器（`_get_async_tokenizer`，按 `id(tokenizer)` 池化、单线程池包裹）编码 prompt。

再看消费端，确认这些产物去了哪：

[`vllm_processor.py:540-555`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L540-L555) —— `VllmProcessor._generator_inner` 调用 `preprocess_chat_request`，随后逐个取出 `request_for_sampling` / `tool_parser` / `chat_template_kwargs` / `engine_prompt` / `prompt_token_ids` / `guided_decoding`。

[`prepost.py:216-271`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L216-L271) —— `build_tool_call_guided_decoding`：优先尝试解析器的 structural_tag（vLLM 0.27 的结构化标签机制），否则回退到"强制选择 → JSON schema"的兜底路径。第 257-260 行注释解释了为什么某些引擎级解析器（如 Gemma4Engine）要**故意**跳过 JSON 兜底：强推 JSON schema 与它们的线格式冲突，甚至会让引擎在投机解码下崩溃。

#### 4.2.4 代码实践

**实践目标**：把"参数 → 约束"的裁决逻辑整理成一张可查的表。

1. 通读 [`prepost.py:117-146`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L117-L146)（`_has_explicit_output_constraint`）和 [`prepost.py:153-193`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L153-L193)（`_should_build_tool_call_guidance`）；
2. 手工推导下面 5 种请求各自得到的 `guided_decoding`（结构：`tools` 有无 / `tool_choice` 取值 / 有无 `guided_json`）：
   - 无 tools，无约束；
   - 有 tools，`tool_choice="auto"`；
   - 有 tools，`tool_choice="required"`；
   - 有 tools，具名 `tool_choice={"type":"function","function":{"name":"f"}}`；
   - 有 tools，具名选择 + `guided_json`。

**预期结果**：一张五行的表。第 5 行应推导出"抛 `InvalidArgument` → HTTP 400"。源码里两处 TODO 注释（第 160-166 行、第 170-176 行）描述了 Python 路径与 Rust 路径（`preprocessor/tool_choice.rs`）的已知行为差 gap，把它们也记进表的备注列。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_ASYNC_TOKENIZER_POOL` 按 `id(tokenizer)` 做键，且给每个分词器配 `max_workers=1` 的线程池？
**答案**：分词器对象本身不是线程安全的；单 worker 线程池把对同一分词器的所有调用**串行化**，同时不同分词器（不同模型）之间仍可并行。按 `id()` 池化则避免为同一分词器重复建池。

**练习 2**：Mistral 分词器路径有什么特殊处理？
**答案**：见 [`prepost.py:498-508`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L498-L508)：Mistral 要求模板内 `tokenize=True`（其官方模板认为 tokenize=False 不安全），且 assistant 消息的 `tool_calls` 需先物化成具体 dict 列表（`_materialize_assistant_tool_calls`），否则模板内 tokenize 会崩。

**练习 3**：`parser_guided_decoding` 和 `client_structured_guidance` 的**不相等比较**（第 577-585 行）在检测什么？
**答案**：`_prepare_request` 调用 `adjust_request()` 前后各算一次结构化约束，不相等说明**解析器自己**在 adjust 时生成/修改了约束——这份"新出现的约束"要传给 `build_tool_call_guided_decoding` 作为 `parser_guided_decoding` 优先采用，而不是 Dynamo 再自己造一个 JSON schema。

### 4.3 模块三：thinking.py——部署级思考模式默认值

#### 4.3.1 概念说明

思考型模型（Qwen3 系列）支持"开/关思考"两种模式。控制信号有**两个来源**，需要一套合并规则：

- **请求级**：客户端在请求里带 `thinking` / `enable_thinking` / `thinking_mode` / `reasoning_effort` 任一键；
- **部署级**：运维在模型的 `runtime_data` 元数据里写 `default_thinking_mode: enabled|disabled`，作为该部署的默认。

规则很朴素：**请求已经控制了就不动；请求没控制才套部署默认**。

#### 4.3.2 核心流程

```
chat_template_kwargs（已合并请求值）
  ├─ default_thinking_mode 为 None？→ 原样返回
  ├─ 请求带了任一思考控制键？→ 原样返回（请求优先）
  ├─ 部署默认不是 enabled/disabled？→ 告警并原样返回
  └─ 否则同时写入三个键：thinking / enable_thinking / thinking_mode
```

同时写入三个键是为了兼容不同模型家族的模板约定（有的模板读 `enable_thinking`，有的读 `thinking`）。

#### 4.3.3 源码精读

[`thinking.py:11-17`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/thinking.py#L11-L17) —— 两个常量：runtime 元数据里的键名，以及被视为"请求已控制思考"的四个键。

[`thinking.py:20-30`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/thinking.py#L20-L30) —— `runtime_default_thinking_mode`：从模型的 runtime 配置（`runtime_data` 字典）里读部署级默认值，类型不对就返回 `None`。

[`thinking.py:33-60`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/thinking.py#L33-L60) —— `apply_default_thinking_mode_to_template_kwargs`：完整的合并规则实现，注意 `request_has_root_thinking` 参数——它检查的是**原始请求 dict 根级**的 `thinking` 键（Rust 侧协议的字段），这与模板 kwargs 里的键是两个世界，所以单独传布尔进来。

来源链路：[`vllm_processor.py:1143`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L1143) 从 `ModelDeploymentCard.runtime_config()` 读出默认值，经构造函数存为 `self.default_thinking_mode`，最终在 [`vllm_processor.py:548`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L548) 传入 `preprocess_chat_request`。

这个模块还有一个**下游消费者**在 M4：`StreamingPostProcessor.__init__` 用 `chat_template_kwargs.get("enable_thinking") is False` 判断"模板关闭了思考"，此时**跳过推理解析器**——因为模板关闭思考时 `<think>...</think>` 标签出现在 prompt 里而不在生成输出里，解析器永远等不到结束标记，反而会把工具调用标记误判成推理文本泄漏进 `reasoning_content`（注释里链接了 issue #8636）。

#### 4.3.4 代码实践

**实践目标**：验证"请求优先于部署默认"的合并表。

`thinking.py` 不 import 任何 vLLM 符号，是本讲唯一**零依赖可运行**的模块。在你的 Python 环境里：

```python
# 示例代码：直接对 thinking.py 做表驱动实验
from dynamo.frontend.thinking import apply_default_thinking_mode_to_template_kwargs as apply

cases = [
    ({}, "disabled", False),                       # 空参数 + 部署默认关闭
    ({"enable_thinking": True}, "disabled", False),# 请求已控制 → 不覆盖
    ({}, "weird-value", False),                    # 非法部署默认 → 忽略
    ({}, None, False),                             # 无部署默认
    ({}, "enabled", True),                         # 根级 thinking 优先
]
for kwargs, mode, has_root in cases:
    print(mode, "->", apply(dict(kwargs), mode, request_has_root_thinking=has_root))
```

**预期结果**：只有第 1 行会产出 `{'thinking': False, 'enable_thinking': False, 'thinking_mode': 'disabled'}`；第 2 行原样返回；第 3 行触发一条 `logger.warning`；第 4、5 行原样返回。

**需要观察的现象**：第 5 行即使部署默认是 `enabled`，因为 `request_has_root_thinking=True` 也不写入任何键——这正是"请求优先"的体现。

#### 4.3.5 小练习与答案

**练习 1**：为什么"部署默认"要同时写三个键，而请求级控制只写一个？
**答案**：部署默认是 Dynamo **主动注入**的，它不知道这个模型的模板读哪个键，所以三个都写保证生效；请求级控制是客户端自己写的键，客户端知道自己模型的约定，Dynamo 不应替它扩展（参见 [`prepost.py:488-489`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L488-L489) 对 `reasoning_effort` 的透传）。

**练习 2**：`runtime_default_thinking_mode` 为什么对返回值做 `isinstance(value, str) and value` 双重检查？
**答案**：runtime 元数据来自模型卡片里的任意 JSON，运维可能写成数字、布尔或空串；双重检查把所有非"非空字符串"的值统一归一为 `None`（即无默认），避免下游 `not in ("enabled", "disabled")` 分支处理一堆畸形类型。

### 4.4 模块四：StreamingPostProcessor——token 流状态机

#### 4.4.1 概念说明

这是本讲的主角，也是前端最精巧的部分。它是一个**每请求（甚至每 choice）一个实例**的有状态对象，输入是引擎吐出的 `CompletionOutput` 增量（含 `text`、`token_ids`、`finish_reason`），输出是一个 OpenAI choice dict（或 `None` 表示这个 chunk 不产生可见增量）。

它同时驱动三个"解析器视图"：

- **reasoning 解析器**（vLLM `ReasoningParser`）：把 `<think>...</think>` 里的文本送进 `reasoning_content`；
- **tool 解析器**（vLLM `ToolParser`）：把 `<tool_call>...</tool_call>` 解析成 `tool_calls` 增量；
- **直通视图**：没有以上任何解析器时（`_fast_plain_text`），原文进 `content`。

核心难点是**跨 chunk 状态**：`previous_text` / `previous_token_ids` 记住"到目前为止的全部输出"，`reasoning_is_done` 记住思考是否已结束，`_tool_text_buffer` 处理"`</think>` 和 `<tool_call>` 挤在同一个 chunk"的边角情况。

#### 4.4.2 核心流程

`process_output(output)` 的决策树（简化伪代码）：

```
若 uses_dynamo_json_fallback → 收集全文，finish 时一次性解析 JSON 工具调用
若 非流式 且 有工具解析器   → 缓冲全文，finish 时用非流式 extract_tool_calls
若 _fast_plain_text          → {"role","content": text} 直通返回

current_text = previous_text + delta_text          # 增量累积

若 _tool_text_buffer 非空：                          # 正在缓冲 tool 文本
    追加 delta；出现 tool 结束标记或 finish → 非流式解析缓冲文本
    否则返回 None（这个 chunk 什么都不发）

否则若 reasoning 未结束 且 有 reasoning 解析器：
    delta_message = extract_reasoning_streaming(...)
    若本 chunk 检测到 reasoning 结束：
        重置累积状态；若结束标记后还跟着 tool 开始标记 → 进 _tool_text_buffer
否则（reasoning 已结束或无 reasoning）：
    若需要解析工具 → extract_tool_calls_streaming(...)

若 finish_reason：
    冲刷引擎级解析器的残留状态（finish_streaming）

组装 choice：
    content / reasoning_content / tool_calls 三选多
    finish_reason == "stop" 且本 choice 发过 tool_calls → 重映射为 "tool_calls"
previous_text = current_text                        # 状态前移
```

三条不变式值得记住：

1. **解析器消耗的文本不泄漏**：`<think>`、`<tool_call>` 等标记永远不会出现在 `content` 里（测试用 `assert "<tool_call>" not in content` 钉死）；
2. **每 choice 独立状态**：`n > 1` 时多个 choice 的 chunk 交错到达，必须各配一个实例（消费端 `post_processors` 字典按 `output.index` 分发）；
3. **`reasoning_content` 一旦停止就不再出现**：开始出 `content` 后不得再冒出思考文本。

#### 4.4.3 源码精读

**构造与解析器装配**

[`prepost.py:653-734`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L653-L734) —— `__init__` 全貌。

[`prepost.py:684-694`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L684-L694) —— reasoning 解析器的三个跳过条件：模板 `enable_thinking=False`（M3 讲过的 #8636 问题）、`response_reasoning_ended is True`（Rust 侧已确认思考结束）、未配置解析器类。第 696-701 行：若 prompt 本身已经包含推理结束标记（`is_reasoning_end(prompt_token_ids)`），直接置 `reasoning_is_done=True` 并用 `adjust_initial_state_from_prompt` 校准解析器初始状态。

[`prepost.py:709-713`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L709-L713) —— `_fast_plain_text` 快路径：没有任何解析器且不走 JSON 兜底时，`process_output` 直接透传文本，跳过全部状态机。

[`prepost.py:717-734`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L717-L734) —— 全部跨 chunk 状态：`previous_text` / `previous_token_ids`（累积输出）、`in_progress_tool_calls`（半成品工具调用，按 index 合并）、`_tool_call_choices_emitted`（**按 choice** 记录是否发过工具调用，`n>1` 时防止 choice 0 的工具调用改写 choice 1 的 finish_reason）、`_tool_text_buffer`（边角缓冲）。

**process_output 主干**

[`prepost.py:1099-1122`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L1099-L1122) —— 入口三分：JSON 兜底 → 非流式工具缓冲 → 快路径直通。第 1108-1110 行注释解释了为什么用 `output.text` 而不从 token_ids 重新解码：vLLM 的 output_processor 已对文本做过停止词裁剪，重新解码会把停止标记带回来。

[`prepost.py:1124-1127`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L1124-L1127) —— 增量累积：`current_text = previous_text + delta_text`。这就是流式解析的物理基础——vLLM 的流式解析器接口需要 previous/current/delta 三组文本与 token ids。

[`prepost.py:1135-1151`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L1135-L1151) —— **工具文本缓冲的排空**。当缓冲未完成时直接返回 `None`（这个 chunk 对客户端不可见），直到结束标记或 `finish_reason` 出现才一次性做非流式解析。

[`prepost.py:1153-1237`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L1153-L1237) —— **reasoning 分支**，状态机的核心。第 1169-1176 行区分引擎级流式解析器（用 `has_engine_confirmed_reasoning_end()`）与传统解析器（用 `is_reasoning_end_streaming(current_token_ids, delta_token_ids)`）。第 1178-1221 行处理"思考在本 chunk 结束"：重置全部累积状态（第 1188-1191 行把 `previous_text` 清空——因为工具解析要从干净文本开始），然后判断结束标记之后的内容里有没有工具开始标记，有则进缓冲，无则作为普通 content 输出。第 1222-1237 行处理"模型跳过了思考直接调工具"的形态（如 Mistral 无 `[THINK]` 标记）。

[`prepost.py:1238-1251`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L1238-L1251) —— reasoning 结束（或从未开始）后的工具流式解析。

[`prepost.py:1253-1265`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L1253-L1265) —— `finish_reason` 到达时冲刷引擎级解析器的残留状态（`finish_streaming()`），把最后的增量合并进来。

**choice 组装与 finish_reason 重映射**

[`prepost.py:1045-1055`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L1045-L1055) —— `_build_choice`：产出标准 choice dict；`logprobs` 在抑制推理输出时置 `None`。

[`prepost.py:1016-1027`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L1016-L1027) —— `_remap_finish_reason`：OpenAI 规范要求模型调了工具时 `finish_reason` 必须是 `"tool_calls"`，而 vLLM 停在 `<|im_end|>` 上报 `"stop"`；只在该 choice 确实发过工具调用增量时重映射一次。

[`prepost.py:866-878`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L866-L878) —— `_is_control_only_content` / `_should_parse_tools`：前者判断某段 content 是否只由特殊标记（`all_special_tokens`）组成，配合第 1283-1286 行使用——当有半成品工具调用时，纯标记文本直接丢弃不进 `content`。

**消费端：每 choice 一个实例**

[`vllm_processor.py:731-755`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L731-L755) —— `new_post_processor()` 工厂：注意它**重新实例化**了一份工具解析器给每个 choice，而不是复用预处理阶段那份——因为 vLLM 工具解析器自带可变流式状态，多 choice 共享会互相污染。

[`vllm_processor.py:757-786`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L757-L786) —— 注释直接点明设计约束："StreamingPostProcessor keeps delta/tool/reasoning parser state, so parallel choices must not share one instance"，于是按 `range(sp.n)` 建字典。

[`vllm_processor.py:929-933`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L929-L933) —— 真正的调用点：引擎响应经 vLLM `OutputProcessor` 去词化后，按 `output.index` 找到对应的 post processor，`choice = post.process_output(output)`。

**一条重要边界：delta 是类型化的，不是自由 dict**

Python 侧产出的 choice 会随整个 chunk dict 跨过 PyO3 边界交给 Rust HTTP 层做 SSE 序列化，而 Rust 侧是**强类型结构**：

[`lib/llm/src/protocols/openai/chat_completions.rs:307-313`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/protocols/openai/chat_completions.rs#L307-L313) —— 构造 `ChatCompletionStreamResponseDelta` 的字段就这几个：`role / content / tool_calls / function_call / refusal / reasoning_content`。该结构本身定义在外部 crate `dynamo-protocols`（见根 `Cargo.toml` 第 71 行的 workspace 依赖 `=5.3.1`），本仓库只有使用点。serde 反序列化默认忽略未知键，所以你在 Python 侧往 `delta` 里塞一个新键（比如 `answer`），**到了 HTTP 响应里会无声消失**。想真正新增一个对客户端可见的 delta 字段，必须改 `dynamo-protocols` 的结构定义并升级依赖——这是一个跨仓库变更。综合实践会利用这一点做一个诚实的实验。

#### 4.4.4 代码实践：扩展一个 `<answer>` 标签解析器

这是本讲的主实践，分三层，由易到难。所有新增文件都放在仓库外的临时目录（例如 `/tmp/answer-practice/`），**不要改动仓库源码**。

**实践目标**：写一个 `AnswerTagStreamingPostProcessor` 子类，把模型输出中的 `<answer>...</answer>` 内容作为单独字段流式返回，并验证标签跨 chunk 断裂时也能正确切分。

**第一层（必做，离线可验证）：子类 + 状态机**

设计要点（先想清楚再动手）：

- 用 `previous_text` 之后的**全量累积文本**判断标签是否完整，绝不要只用 `delta_text` 判断；
- 维护三态：`None`（还没见到 `<answer>`）→ `"inside"`（在标签内）→ `"done"`（见到 `</answer>`）；
- `<answer>` 之前的文本进 `content`，标签内的进新字段，`</answer>` 之后的回 `content`；
- 标签本身（`<answer>`、`</answer>`）永远不能出现在任何输出字段里。

示例代码（骨架，放 `/tmp/answer-practice/answer_post.py`）：

```python
# 示例代码：StreamingPostProcessor 的 <answer> 标签扩展骨架
from dynamo.frontend.prepost import StreamingPostProcessor

ANSWER_OPEN, ANSWER_CLOSE = "<answer>", "</answer>"

class AnswerTagStreamingPostProcessor(StreamingPostProcessor):
    """把 <answer>...</answer> 内的文本路由到单独的 delta 字段。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._answer_state = "outside"   # outside | inside | done

    def process_output(self, output):
        # 最简做法：先把本 chunk 的文本按状态机切分，再交给父类处理 content 部分，
        # 标签内文本放入 delta["answer"]。
        # 提示：用 self.previous_text + (output.text or "") 得到全量文本，
        #       用 str.find() 定位标签边界，注意标签可能跨 chunk 断裂。
        raise NotImplementedError
```

写完后，模仿仓库现成的单测来验证。测试的黄金模板是：

[`tests/frontend/test_prepost.py:1424-1432`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/tests/frontend/test_prepost.py#L1424-L1432) —— `_collect_results`：把一组 `CompletionOutput` 逐个喂给 `process_output`，收集非 `None` 的结果。这正是"模拟流式到达"的方法——**你构造 outputs 列表的方式就是 token 的到达方式**。

[`tests/frontend/test_prepost.py:1407-1422`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/tests/frontend/test_prepost.py#L1407-L1422) —— `processor` fixture：构造一个真实处理器的全部参数（tokenizer、请求、采样参数、prompt token ids、Hermes 工具解析器、Qwen3 reasoning 解析器类）。照抄这个 fixture，把 `tool_parser=None`、`reasoning_parser_class=None`（走 `_fast_plain_text` 的父类路径，让子类逻辑成为唯一变量）。

你的测试用例应当覆盖（每个用例构造不同的 outputs 列表）：

| 用例 | outputs 切分方式 | 断言要点 |
|------|------------------|----------|
| A 标签完整在一个 chunk | `["前言<answer>42</answer>"]` | content=="前言"，answer=="42"，无标签泄漏 |
| B 标签跨 chunk 断裂 | `["前言<an","swer>4","2</ans","wer>"]` | 拼接后各字段与用例 A 完全一致 |
| C 标签未闭合 | `["<answer>还没有结束"]` | 累积中，answer 字段按你的设计决定是流式吐还是缓冲 |
| D 标签后还有正文 | `["<answer>42</answer>再见"]` | "再见"进 content，answer 不再增长 |

运行方式（需要 vLLM 环境）：

```bash
uv pip install -e '.[vllm]'
pytest /tmp/answer-practice/test_answer_post.py -v
```

**预期结果**：用例 B 与用例 A 的拼接结果完全一致——这是流式解析器正确性的金标准（仓库测试 `test_stream_interval_1` 与 `test_stream_interval_20` 互相印证的正是这一点，见 [`tests/frontend/test_prepost.py:1713-1755`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/tests/frontend/test_prepost.py#L1713-L1755)）。

**第二层（推荐）：真实 token 流端到端验证**

仓库自带一个"回放固定 token 序列"的 worker，专门为这类实验设计：

[`tests/frontend/vllm_prepost_worker.py:30-70`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/tests/frontend/vllm_prepost_worker.py#L30-L70) —— `VllmPrepostTestHandler.generate`：把 `OUTPUTS_INTERVAL_20` 这组**预录的 token 序列**原样按 chunk 回放。把这份文件复制到 `/tmp/answer-practice/answer_worker.py`，把回放源换成你自己编码的文本：

```python
# 示例代码：替换 worker 的回放源（节选）
text = "思考过程<answer>巴黎</answer>完毕"
ids = self.tokenizer.encode(text)          # 与 frontend 用同一个分词器
# 每 4 个 token 一组切成 chunk，刻意制造标签断裂
```

然后让 frontend 用你的子类。`vllm_processor.py` 在模块顶层 `from .prepost import StreamingPostProcessor`（[`vllm_processor.py:44`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/vllm_processor.py#L44)），而 `new_post_processor()` 在**调用时**才解析这个模块全局名（第 740 行），所以启动前打模块补丁即可生效。示例代码（`/tmp/answer-practice/answer_frontend.py`）：

```python
# 示例代码：打补丁后启动 frontend，其余 CLI 参数照常从 sys.argv 读取
import dynamo.frontend.vllm_processor as vp
from answer_post import AnswerTagStreamingPostProcessor
vp.StreamingPostProcessor = AnswerTagStreamingPostProcessor
from dynamo.frontend.main import main
main()
```

启动拓扑参照集成测试的现成配方（本地把 etcd 换成 file 后端可免依赖，见 u1-l2/u2-l1 的结论）：

[`tests/frontend/test_vllm_prepost_integration.py:177-216`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/tests/frontend/test_vllm_prepost_integration.py#L177-L216) —— frontend 参数：`--dyn-chat-processor vllm --enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3`，外加环境变量 `DYN_VLLM_STREAM_INTERVAL=20`（控制 chunk 聚合粒度，调小它能让标签断裂更容易复现）。

curl 验证：

```bash
curl -N http://localhost:8000/v1/chat/completions -d '{
  "model": "<你的模型名>", "stream": true, "max_tokens": 64,
  "messages": [{"role":"user","content":"测试"}]
}'
```

**需要观察的现象**：

1. SSE 流里 `<answer>` 前的文本落在 `delta.content`；
2. 标签内的文本落在你选的字段；
3. 标签标记本身在任何 delta 里都找不到。

**一个必须预知的诚实结论**：如果你把新字段命名为 `delta["answer"]`，你会在第一层单测里看到它，但**端到端 SSE 里看不到**——因为 Rust 侧的 `ChatCompletionStreamResponseDelta` 是类型化结构（见 4.4.3 末尾的边界警示），未知键在 PyO3 边界被丢弃。第二层想看到效果，把标签内文本暂时路由到现成的 `reasoning_content` 字段即可端到端可见；想让 `answer` 成为真正的新协议字段，就得修改外部 crate `dynamo-protocols` 里 `ChatCompletionStreamResponseDelta` 的定义并升级依赖版本——那是一次跨仓库变更，超出本讲范围，但这个实验让你亲眼确认了边界在哪。

**第三层（可选）：改参数观察行为**

把 `DYN_VLLM_STREAM_INTERVAL` 在 1 / 20 / 64 之间切换重跑第二层，观察同一个标签在不同 chunk 粒度下的切分结果是否仍然一致。若不一致，说明你的状态机漏了某种断裂方式。

> 端到端部分（第二、三层）需要本地 etcd（或改用 `--discovery-backend file`）与可下载的分词器模型；若环境不可用，本层标「待本地验证」，第一层的单元测试已足以验证状态机正确性。另外说明：`examples/backends/sample` 的假后端产出的是轮转合成 token（u1-l2 已确认 `(i+1) % 32000` 轮转、无语义文本），`dynamo.mocker` 也是性能模型驱动的模拟引擎，两者都**无法**回放指定字符串，所以本实践选用 `vllm_prepost_worker.py` 这种"预录 token 回放"方式——这也是仓库自己的集成测试选它的原因。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `process_output` 在 reasoning 结束时要把 `previous_text` 清空（第 1188-1191 行），而不是继续累积？
**答案**：推理结束后文本要交给**工具解析器**重新从头解析。如果保留 `previous_text`，工具解析器看到的"当前文本"会包含整段推理文本，而它是按模型原生工具语法从头匹配的；清空等于告诉解析器"从现在开始才是有效输入"。

**练习 2**：`_remap_finish_reason` 为什么要按 choice 记录 `_tool_call_choices_emitted`，而不是用一个实例级布尔？
**答案**：`n > 1` 的请求会交错流出多个 choice 的 chunk，共用一个实例级布尔的话，choice 0 发过工具调用会把 choice 1 的 `"stop"` 也错误重映射成 `"tool_calls"`。按 `output.index` 记录集合才能保证重映射只作用于真正发过工具调用的那个 choice。

**练习 3**：`stream_response=False`（客户端要非流式响应）时，Dynamo 是不是就一次性解析了？
**答案**：不是。内部仍然逐 chunk 调 `process_output`，只是 `_should_buffer_for_non_streaming_tool_parse()`（[`prepost.py:833-838`](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/prepost.py#L833-L838)）为真时走 `_process_non_streaming_tool_output`：缓冲全文，直到 `finish_reason` 出现才做一次**非流式**工具解析。设计动机见测试注释"non-streaming clients should match vLLM batch parsing"——保证流式与非流式客户端看到一致的工具调用结果。

## 5. 综合实践

把本讲三个模块串起来，做一次"给 Dynamo 加一种新的输出标签"的完整演练：

**任务**：假设你们的模型会在回答里输出 `<answer>...</answer>` 标签（例如内部评测用的标准答案标注），需要前端把它从正文里剥离、作为独立字段返回，且不影响现有的 reasoning 与 tool call 解析。

1. **预处理侧**（M2/M3）：确认这类请求**不需要**任何 guided decoding 约束——沿着 `preprocess_chat_request` 推一遍，确认无 tools、无 `guided_*` 时 `guided_decoding` 为 `None`、`tool_parser` 为 `None`，即预处理对这种请求是"透明"的。把推导过程写成一页笔记。
2. **思考模式侧**（M3）：如果该模型同时是思考型模型，验证部署级 `default_thinking_mode=disabled` 时，`chat_template_kwargs` 会带上 `enable_thinking=False`，而这会让你的后处理器**继承**一个重要行为：父类构造时跳过 reasoning 解析器。确认你的子类在这一路径下依然工作。
3. **后处理侧**（M4）：完成 4.4.4 的 `AnswerTagStreamingPostProcessor`，并保证三条不变式不被破坏（标记不泄漏、`reasoning_content` 先于 `content` 结束、`n>1` 时各 choice 互不污染——最后一条要求你的新状态也按 choice 隔离，而子类实例本身已经是每 choice 一个，所以只需说明为什么这样就够了）。
4. **验收**：用 4.4.4 的四张用例表跑单测；有环境的话再跑端到端。最后写一段 200 字的总结，回答：如果这个字段要对客户端正式开放，需要动哪几处代码（提示：至少包括外部 `dynamo-protocols` crate 的 delta 结构定义、本仓库的依赖版本，以及 OpenAI 兼容性文档）。

完成这个任务后，你就走通了一条"从请求进来到响应出去"的完整 Python 侧链路。

## 6. 本讲小结

- `prepost.py` 是前端的"整形车间"：`preprocess_chat_request` 把 OpenAI 请求变成 `PreprocessResult`（核心是 `prompt_token_ids` + 两个解析器 + 引导解码约束），`StreamingPostProcessor` 把 token 增量流变回 OpenAI delta 流。
- 预处理最大的暗礁是**引导解码的单槽裁决**：请求自带约束、工具约束、解析器生成的约束三方竞争，顺序错了会把解析器的约束误判为客户端的（第 555-559 行的注释是不可多得的"顺序即语义"教材）。
- `thinking.py` 用 60 行讲清了"请求级优先于部署级默认"的合并规则，其产物 `enable_thinking=False` 还会反向影响后处理器的解析器装配。
- `StreamingPostProcessor` 是一个跨 chunk 状态机：累积文本 + 三态 reasoning + 工具文本缓冲 + 按 choice 隔离，专门解决"标签跨 chunk 断裂"这一类流式难题；`n>1` 时每 choice 一个实例是硬约束。
- Python 侧的 delta 只是看起来自由：跨过 PyO3 边界后由 Rust 的类型化结构 `ChatCompletionStreamResponseDelta` 序列化，新增字段必须两侧同步改。
- 验证流式解析器的金标准是"改变 chunk 切分方式，拼接结果不变"——仓库的 `test_stream_interval_1` / `test_stream_interval_20` 与 `vllm_prepost_worker.py` 提供了现成的实验装置。

## 7. 下一步学习建议

- **下一讲（u5-l3）**：`vllm_processor.py` / `sglang_processor.py` 的后端差异化 Processor——本讲只看了它们的两个调用点，下一讲讲它们如何挂进 `EngineFactory`、以及 SGLang 侧为什么需要一份独立的 `sglang_prepost.py`（对照本讲的 `preprocess_chat_request`，找出两份实现的取舍差异会是很好的练习）。
- **顺着边界往下读**：`lib/llm/src/protocols/openai/chat_completions.rs` 与 `delta_common.rs`——u4-l2 已建立 `DeltaGeneratorState` 的概念，现在你有了 Python 侧视角，可以完整拼出"一个流式 chunk 的诞生"。
- **顺着测试往下读**：`tests/frontend/test_prepost.py` 剩余部分（`test_stream_terminal_single_chunk`、`test_streaming_parallel_tool_calls_no_think` 等覆盖了更多边角），以及 `tests/frontend/FRONTEND_CASES.md` 的用例索引。
- **回望依赖**：若对 `ModelDeploymentCard` 如何把 `runtime_data.default_thinking_mode` 带到 frontend 还不清楚，回看 u4-l4 的 discovery 模块。

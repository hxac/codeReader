# 约束解码、推理解析与函数调用

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 LightLLM 的「结构化输出」有三套彼此正交的机制：**约束解码**（采样前在 GPU 上改 logits）、**推理解析**（把思考链从答案里分离）、**函数调用解析**（把模型生成的工具调用文本解析成结构化对象）。
- 指出这三套机制分别住在请求链路的哪一段：约束解码住在 ModelBackend（GPU，采样之前），而两类 parser 住在 HttpServer 的 OpenAI 适配层（CPU，反 token 化之后）。
- 读懂 `output_constraint_mode` 如何选出一个专用推理后端，并用「token 掩码 + FSM 推进」把输出强制约束到正则 / JSON Schema / 文法上。
- 读懂 `reasoning_parser.py` 的策略/探测器模式，理解 `<think>...</think>` 这类思考标记如何被流式切分。
- 读懂 `function_call_parser.py` 的 `FunctionCallParser` 如何依据 `tool_call_parser` 把不同模型族（Qwen / Llama3.2 / Mistral / DeepSeek / GLM …）的工具调用文本统一解析为 `ToolCallItem`。

## 2. 前置知识

本讲默认你已经读过：

- **u2-l2（HTTP API 服务与请求分发）**：知道一次请求如何进入 HttpServer、被反 token 化、再被组装成 OpenAI 风格响应回流。
- **u3-l6（采样与后处理）**：知道采样发生在 ModelBackend 内部，顺序是「惩罚 → 屏蔽 → 温度 → softmax → top-k/top-p」，logits 在 softmax 之前可以被任意改写。

本讲要解决的，是一个横跨「采样之前」与「采样之后」的共同问题：**原始的 token 流往往不是业务想要的形状**。

- 模型采样出的下一个 token 可能破坏 JSON 结构（多了一个 `{`、少了一个 `}`），于是业务方拿到的文本无法 `json.loads`。
- 推理模型（DeepSeek-R1、Qwen3、GPT-OSS …）会把「思考过程」和「最终答案」连在一起输出，客户端只想看答案，或者想把思考过程单独存档。
- 工具调用模型会用各自专属的标记（`<tool_call>`、`[TOOL_CALLS]`、`<|python_tag|>` …）包裹一段 JSON，客户端希望直接拿到结构化的 `tool_calls` 数组而不是自己去正则匹配。

LightLLM 用三类组件分别处理这三种「形状不对」的输出。理解它们的关键，是先想清楚**改写的时机**：

| 机制 | 作用阶段 | 所在进程/层 | 改的是 logits 还是文本 |
| --- | --- | --- | --- |
| 约束解码（`output_constraint_mode`） | 采样**之前** | ModelBackend（GPU） | logits（掩码） |
| 推理解析（`reasoning_parser`） | 反 token 化**之后** | HttpServer / `api_openai.py` | 已解码文本 |
| 函数调用解析（`tool_call_parser`） | 反 token 化**之后** | HttpServer / `api_openai.py` | 已解码文本 |

一条贯穿全讲的直觉：**约束解码是「让模型说对话」，两类 parser 是「把模型说的话翻译成结构」**。前者侵入采样、成本高但保证合法；后者纯后处理、零推理开销但救不了已经生成错的 token。

下面几个术语会反复出现：

- **logits 掩码（masking）**：把不该出现的 token 的 logit 压成一个极大的负值（代码里是 `-1000000.0`），使其 softmax 后概率趋近 0。
- **FSM / 文法匹配器（GrammarMatcher、RegexGuide）**：一个有限状态机，给定「当前已生成内容」，告诉你「下一个合法 token 集合」。
- **思考标记（think token）**：如 `<think>` / `</think>`，模型用它把推理过程包起来。
- **工具调用标记（tool call tag）**：各模型族自定义的、用来包裹工具调用的特殊字符串。

## 3. 本讲源码地图

本讲涉及的关键文件与各自职责：

| 文件 | 职责 |
| --- | --- |
| [lightllm/server/api_cli.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py) | 注册 `--output_constraint_mode`、`--tool_call_parser`、`--reasoning_parser` 三个启动参数及其取值范围。 |
| [lightllm/server/core/objs/start_args_type.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/start_args_type.py) | 启动参数的数据类，记录 `output_constraint_mode` 等字段的默认值。 |
| [lightllm/server/core/objs/sampling_params.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/sampling_params.py) | 采样参数，承载每请求的约束字段（`guided_json`/`guided_grammar`/`regular_constraint`/`allowed_token_ids`）。 |
| [lightllm/server/router/model_infer/model_rpc.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py) | 按 `output_constraint_mode` 选出专用推理后端。 |
| [lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl_for_xgrammar_mode.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl_for_xgrammar_mode.py) | xgrammar 约束后端：用文法匹配器生成 token 掩码。 |
| [lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl_for_outlines_constraint_mode.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl_for_outlines_constraint_mode.py) | outlines 约束后端：用正则引导的 FSM 生成 token 掩码。 |
| [lightllm/server/router/model_infer/infer_batch.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py) | `SamplingParam.has_constraint_setting()`：判断一个请求是否带约束。 |
| [lightllm/server/reasoning_parser.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/reasoning_parser.py) | 推理内容解析器：策略 + 探测器，切分思考链。 |
| [lightllm/server/function_call_parser.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/function_call_parser.py) | 函数调用解析器：探测器集合，把各模型族工具调用文本解析成 `ToolCallItem`。 |
| [lightllm/server/build_prompt.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/build_prompt.py) | 用 chat template 构造 prompt，并探测 tokenizer 是否支持强制思考。 |
| [lightllm/server/api_openai.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_openai.py) | OpenAI 适配层，把两类 parser 接进流式 / 非流式响应。 |

## 4. 核心概念与源码讲解

### 4.1 约束解码（output_constraint_mode）

#### 4.1.1 概念说明

约束解码要解决的问题是：**保证模型输出永远满足某种语法约束**——比如「必须是合法 JSON」「必须匹配某个正则」「必须符合某条文法」。

它的实现思路是「**采样前掩码**」。在 u3-l6 里我们知道，采样是 `softmax(logits) → 概率分布 → 选 token`。如果在 softmax 之前，把当前状态下**不合法**的 token 的 logit 改成负无穷，那么这些 token 被采到的概率就严格为 0，模型只能从合法集合里挑。这样从第一个 token 起每一步都合法，整条序列自然合法。

这要求每一步都知道「给定已经生成的前缀，下一个合法 token 集合是什么」。这正是一个有限状态机（FSM）或文法匹配器能回答的问题：

- **正则约束**（`regular_constraint`）：outlines 库把正则编译成 FSM，`get_next_instruction(state)` 返回当前状态下允许的 token id 列表。
- **JSON Schema / 文法约束**（`guided_json` / `guided_grammar`）：xgrammar 库把 Schema 或文法编译成匹配器，`fill_next_token_bitmask()` 产出一个覆盖整个词表的比特掩码。

注意约束解码与两类 parser 的本质差别：约束解码**侵入采样**，保证生成结果合法；parser **不干预生成**，只在事后切分已有文本。所以「要保证输出一定是合法 JSON」必须用约束解码，parser 救不回一个已经被模型写坏的序列。

#### 4.1.2 核心流程

约束解码的整体流程可以画成一条「每步采样」的增强链路：

```text
请求携带 guided_json / guided_grammar / regular_constraint / allowed_token_ids
        │
        ▼  （这些字段落到 sampling_param 上）
ModelBackend 用专用后端（XgrammarBackend / OutlinesConstraintBackend）
        │
        ▼  （后端注册了两个回调）
每步前向得到 logits
        │
        ├──► prefill_mask_func / decode_mask_func
        │       1. 对每个带约束的请求，查 FSM/匹配器
        │       2. 把不合法 token 的 logit 置 -1000000.0
        │       3. （xgrammar 还会把 padding 维度也掩掉）
        ▼
正常采样（top-k/top-p，见 u3-l6）→ next_token_id
        │
        ▼
extra_post_req_handle_func：用 next_token_id 推进 FSM/匹配器状态
        │
        ▼
若 FSM 进入终止态（matcher.is_terminated() 或 fsm_current_state == -1）
        → 标记请求 FINISHED_STOP
```

用一个最朴素的公式描述掩码（行内）：

\[ \mathrm{logits}'[t] = \begin{cases} \mathrm{logits}[t] & t \in \text{allowed(state)} \\ -\infty & t \notin \text{allowed(state)} \end{cases} \]

代码里 \(-\infty\) 被替换成一个具体大负数 `-1000000.0`（避免下游 `-inf` 参与运算出 NaN）。

#### 4.1.3 源码精读

**① 启动参数注册。** `--output_constraint_mode` 在 CLI 里的合法取值是 `outlines / xgrammar / none`：

[lightllm/server/api_cli.py:325-331](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L325-L331) — 注册 `--output_constraint_mode`，`choices=["outlines", "xgrammar", "none"]`，默认 `none`。

> 注意一个容易踩的坑：数据类 [lightllm/server/core/objs/start_args_type.py:83](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/start_args_type.py#L83) 里这个字段的 `metadata` 写的是 `["none", "simple", "xgrammar"]`，与真实 CLI 的 `outlines` 不一致。**真实启动校验以 `api_cli.py` 为准**，`start_args_type.py` 里的 `"simple"` 是遗留的过时声明，运行时不会被用来分支。

**② 后端选择。** `ModelRpcServer.init_model` 依据该参数挑后端：

[lightllm/server/router/model_infer/model_rpc.py:63-98](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/model_rpc.py#L63-L98) — 把 `output_constraint_mode` 映射到 `OutlinesConstraintBackend()` 或 `XgrammarBackend()`，二者互斥；其余情况落回普通 `ChunkedPrefillBackend`。

这两个约束后端都继承自 `ChunkedPrefillBackend`，只是**换掉了三个钩子**（prefill 掩码、decode 掩码、采样后状态推进），其余 prefill/decode 主循环完全复用 u3-l2 讲过的流程。

**③ 请求侧的约束开关。** 一个请求到底有没有约束，由 `has_constraint_setting()` 判定：

[lightllm/server/router/model_infer/infer_batch.py:482-488](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py#L482-L488) — 只要 `regular_constraint / allowed_token_ids / guided_grammar / guided_json` 任一非空即视为带约束。

这四个字段定义在采样参数里，并做了互斥校验（同一请求只能指定一种约束）：

[lightllm/server/core/objs/sampling_params.py:444-459](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/sampling_params.py#L444-L459) — `guided_grammar` 与 `regular_constraint`、`guided_json` 互斥；`allowed_token_ids` 与其余三者都互斥。

**④ xgrammar 掩码实现。** 这是「采样前改 logits」最核心的一段：

[lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl_for_xgrammar_mode.py:54-97](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl_for_xgrammar_mode.py#L54-L97) — `_decode_mask_callback` / `_prefill_mask_callback` 调用 `_mask_req_out_token`：对带 `guided_grammar` 或 `guided_json` 的请求，让 `xgrammar_matcher.fill_next_token_bitmask` 产出整张词表的比特掩码，再用 `xgr.apply_token_bitmask_inplace` 把 logits 里不合法位置直接压成大负值。

匹配器的编译被 `lru_cache(maxsize=200)` 缓存，同一个 schema/grammar 只编译一次：

[lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl_for_xgrammar_mode.py:33-51](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl_for_xgrammar_mode.py#L33-L51) — `get_cached_grammar` 按 `(type, schema)` 缓存编译结果，`type` 为 `grammar`（含内置 `json`）/`schema`。

采样之后，状态推进在 `_update_xgrammer_fsm`：

[lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl_for_xgrammar_mode.py:77-87](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl_for_xgrammar_mode.py#L77-L87) — `matcher.accept_token(next_token_id)` 推进匹配器；若 `is_terminated()` 则把请求标记为 `FINISHED_STOP`，主动结束生成。

**⑤ outlines 掩码实现。** 思路与 xgrammar 对称，但产物是「token id 允许列表」而非比特掩码：

[lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl_for_outlines_constraint_mode.py:58-105](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl_for_outlines_constraint_mode.py#L58-L105) — 先建一个全 `True`（意为「待掩码」）的 bool 张量，再用 `regex_guide.get_next_instruction(state).tokens` 把合法 token 位置置 `False`，最后 `logits[mask] = -1000000.0`。

它还支持 `allowed_token_ids`（一个固定白名单，不走 FSM）。状态推进走 `regex_guide.get_next_state`，状态变 `-1` 同样触发结束。

#### 4.1.4 代码实践

**实践目标**：用源码阅读 + 一次真实启动，验证「约束解码确实在采样前掩码」。

**操作步骤**：

1. 阅读上面的源码精读片段，确认约束后端只是替换了三个钩子，主循环仍是 `ChunkedPrefillBackend`（参见 u3-l2）。
2. 用 xgrammar 模式启动一个支持函数调用的小模型（示例命令，路径按本地替换）：

   ```bash
   python -m lightllm.server.api_server \
       --model_dir /path/to/qwen2.5 \
       --output_constraint_mode xgrammar \
       --tp 1 --port 8088
   ```

3. 发一个带 `guided_json` 的请求，强制返回固定 schema（示例代码）：

   ```python
   # 示例代码：仅演示请求体，不依赖 lightllm 内部 API
   import requests, json
   schema = {
       "type": "object",
       "properties": {
           "name": {"type": "string"},
           "age": {"type": "integer"}
       },
       "required": ["name", "age"]
   }
   data = {
       "model": "qwen2.5",
       "messages": [{"role": "user", "content": "介绍一下张三，他 30 岁。"}],
       "guided_json": schema,
       "max_new_tokens": 64,
   }
   r = requests.post("http://127.0.0.1:8088/generate", json=data).json()
   print(r["generated_text"])
   print(json.loads(r["generated_text"]))  # 一定能 loads 成功
   ```

**需要观察的现象**：返回文本严格是一个 JSON 对象，只含 `name`/`age`，且 `age` 是整数而非字符串。

**预期结果**：`json.loads` 不会抛异常；若把 schema 的 `age` 改成 `{"type": "string"}`，重新请求应得到字符串型的 `age`。

**若无法本地运行**：明确「待本地验证」。退而求其次的源码阅读型实践——在 `impl_for_xgrammar_mode.py` 的 `_mask_req_out_token` 里临时加一行 `print(logits.shape)`，启动后发一次带 `guided_json` 的请求，确认掩码回调被触发、且 logits 形状是 `[batch, vocab_size]`。

#### 4.1.5 小练习与答案

**练习 1**：为什么约束后端把 `-inf` 统一替换成 `-1000000.0`？

> 参考答案：下游采样里有 softmax、top-k 累加等运算，`-inf` 参与减法/加法容易产生 `NaN`，用一个「足够大但仍有限」的负值能保证概率被压到 ~0 的同时不破坏数值稳定性。

**练习 2**：约束后端为什么必须继承 `ChunkedPrefillBackend`、且在 `__init__` 里把 `support_overlap = False`？

> 参考答案：约束解码要在每一步前向后「推进 FSM 并据此决定下一步掩码」，这是一种强顺序依赖，无法与 u6-l2 讲的 microbatch overlap（把计算与通信重叠）兼容；后者要求两个 microbatch 能交错执行，会打乱 FSM 的单调推进，因此显式禁用 overlap。

---

### 4.2 推理解析（reasoning_parser）

#### 4.2.1 概念说明

推理模型在给出最终答案前，会先输出一段「内心独白」（思考链）。不同模型用不同标记把它包起来：

- DeepSeek-R1 / Qwen3：`<think>思考内容</think>最终答案`。
- Kimi 思考模型：用全角符号 `◁think▷ ... ◁/think▷`。
- GPT-OSS：用一套「频道」结构 `<|channel|>analysis<|message|> ... <|end|>`。
- Gemma-4：`<|channel>thought\n ... <channel|>`。

对客户端而言，这段思考过程有时是负担（占用 token、干扰阅读），有时是资产（可解释、可存档）。`reasoning_parser` 的职责就是：**把同一段生成文本，按模型各自的标记，拆成「思考内容（reasoning）」与「正常内容（normal）」两路**，让 OpenAI 响应可以分别放进 `reasoning_content` 与 `content` 字段。

它**不参与采样**，纯 CPU 后处理，发生在反 token 化之后、组装响应之前（详见 4.2.3 的 api_openai 集成）。

#### 4.2.2 核心流程

`reasoning_parser.py` 采用「**外观 + 策略 + 探测器**」三层：

```text
ReasoningParser（外观）
   │  按 model_type 查 DetectorMap 选探测器
   ▼
BaseReasoningFormatDetector（抽象基类）
   │  提供 detect_and_parse（一次性）与 parse_streaming_increment（流式）
   │  维护 _buffer / _in_reasoning / stripped_think_start 等流式状态
   ▼
具体探测器：DeepSeekR1Detector / Qwen3Detector / KimiDetector /
            GptOssDetector / Gemma4Detector / ...
```

流式解析的关键难点是**标记被切断**：`</think>` 可能分两次 chunk 到达（先是 `</thi`，再是 `nk>`）。基类用一个简单但有效的策略处理它：

- 把新 chunk 追加到 `_buffer`；
- 若当前 `_buffer` 恰好是某个标记（`<think>`/`</think>`）的**前缀**，就先 hold 住、不输出，等下一个 chunk 来确认；
- 否则按「是否在思考块内」决定送往 `reasoning_text` 还是 `normal_text`。

`force_reasoning` 是另一个要点：DeepSeek-R1 总是先思考（即便模型没显式输出 `<think>` 开标记），所以它的探测器把 `force_reasoning=True`，解析器一启动就处在「思考中」状态。

#### 4.2.3 源码精读

**① 探测器注册表。** `model_type` 到探测器的映射：

[lightllm/server/reasoning_parser.py:903-918](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/reasoning_parser.py#L903-L918) — `DetectorMap` 把 `deepseek-r1`、`qwen3`、`gpt-oss`、`kimi`、`gemma4` 等 14 个键映射到对应探测器类；注意 `deepseek-v3`/`glm45`/`qwen3-thinking`/`minimax`/`interns1` 都复用 `Qwen3Detector`。

启动参数 `--reasoning_parser` 的合法取值正是这些键：

[lightllm/server/api_cli.py:171-192](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L171-L192) — 列出全部 14 个可选值。

**② 基类的流式切分。** 这是「思考/正常」分流的核心：

[lightllm/server/reasoning_parser.py:641-692](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/reasoning_parser.py#L641-L692) — `parse_streaming_increment`：先 hold 住可能是标记前缀的 buffer；遇到 `<think>` 进入思考态；遇到 `</think>` 把之前的内容作 `reasoning_text`、之后作 `normal_text` 一次性返回。

**③ force_reasoning 的差异。** 两个最常用的探测器只差一个参数：

[lightllm/server/reasoning_parser.py:695-749](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/reasoning_parser.py#L695-L749) — `DeepSeekR1Detector` 构造时 `force_reasoning=True`（默认就在思考），`Qwen3Detector` 则 `force_reasoning=False`，需由 `<think>` 标记或外部开关决定。

**④ 非流式与 flush。** 一次性解析较简单（按 `</think>` 切分）；`flush` 处理「生成被 `max_tokens` 截断、还没看到 `</think>`」的边界：

[lightllm/server/reasoning_parser.py:625-639](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/reasoning_parser.py#L625-L639) — `flush` 根据「当前是否还在思考块」把残余 buffer 归入 `reasoning_text` 或 `normal_text`，避免丢失尾巴。

**⑤ Harmony（GPT-OSS）—— 更复杂的状态机。** GPT-OSS 不用 `<think>`，而用频道结构。`GptOssDetector` 委托给 `HarmonyParser`，后者用 `CanonicalStrategy`（带 `<|channel|>` 标记）或 `TextStrategy`（纯文本回退）两种策略解析，把 `analysis` 频道识别为 reasoning、`final`/`commentary` 识别为 normal、`<|call|>` 识别为 tool_call：

[lightllm/server/reasoning_parser.py:770-821](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/reasoning_parser.py#L770-L821) — `GptOssDetector` 的两个方法都把事件按 `event_type` 归类：`reasoning` 进 `reasoning_text`，`normal`/`tool_call` 进 `normal_text`（tool_call 保留 `raw_text` 以便交给函数调用解析器继续处理）。

**⑥ 与 OpenAI 层的集成。** 流式与非流式两条入口：

[lightllm/server/api_openai.py:176-192](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_openai.py#L176-L192) — `_process_reasoning_stream`：每个 choice 一个 `ReasoningParser` 实例（按 `index` 缓存），调用 `parse_stream_chunk(delta)` 得到 `(reasoning_text, normal_text)`。

[lightllm/server/api_openai.py:510-530](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_openai.py#L510-L530) — 流式响应里，若 `separate_reasoning=True`，reasoning 单独成块塞进 `delta.reasoning`；否则把它拼回 `delta` 当普通 content 输出。

「是否强制思考」由 `_is_force_thinking_mode` 决定，它综合了 tokenizer 是否支持思考、用的是哪个 parser、以及请求里的 `chat_template_kwargs`：

[lightllm/server/api_openai.py:156-173](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_openai.py#L156-L173) — 例如 `qwen3-thinking/gpt-oss/minimax` 一律强制思考；`qwen3/glm45/nano_v3/interns1/gemma4` 默认思考但可被 `enable_thinking=False` 关掉；`deepseek-v3` 需显式 `thinking=True`。

`build_prompt.py` 里的 `tokenizer_supports_force_thinking` 是这一切的前置探测——只有 chat template 里含 `thinking` 或 `enable_thinking` 的 tokenizer 才允许强制思考：

[lightllm/server/build_prompt.py:50-73](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/build_prompt.py#L50-L73) — 用 `@lru_cache` 缓存探测结果，避免每个请求都扫一遍 template。

#### 4.2.4 代码实践

**实践目标**：脱离服务进程，直接用 `ReasoningParser` 验证「思考/答案」的切分逻辑。

**操作步骤**：写一个最小脚本（示例代码），分别构造「带开标记」「不带开标记（R1 风格）」「流式分片」三种输入：

```python
# 示例代码：直接调用 lightllm 的推理解析器，无需启动服务
from lightllm.server.reasoning_parser import ReasoningParser

# 1) Qwen3 风格：有 <think> 开标记
p = ReasoningParser(model_type="qwen3")
print(p.parse_non_stream("<think>先想一下</think>答案是 42"))
# 预期: ('先想一下', '答案是 42')

# 2) DeepSeek-R1 风格：无 <think> 开标记，强制思考
p2 = ReasoningParser(model_type="deepseek-r1")
print(p2.parse_non_stream("先想一下</think>答案是 42"))
# 预期: ('先想一下', '答案是 42')，因为 force_reasoning=True

# 3) 流式分片：把 </think> 拆到两个 chunk
p3 = ReasoningParser(model_type="qwen3")
print(p3.parse_stream_chunk("正在思考"))            # 进入思考态，输出 reasoning
print(p3.parse_stream_chunk("完毕</"))              # hold 住，可能还没成 </think>
print(p3.parse_stream_chunk("think>答案是 42"))      # 确认结束标记，切出 normal
```

**需要观察的现象**：第 3 步中，`完毕</` 不应被立刻当作 reasoning 输出（因为 `</` 可能是 `</think>` 的前缀），要等第三个 chunk 拼成完整 `</think>` 后才把「完毕」归入 reasoning、「答案是 42」归入 normal。

**预期结果**：reasoning 与 normal 不重叠、不丢失，且 `</` 这种半截标记被正确 hold。

**若无法本地运行**：标注「待本地验证」。退而求其次的源码阅读实践——在 `BaseReasoningFormatDetector.parse_streaming_increment` 的「hold 前缀」分支（约 L657-L661）加日志，复现上面的分片输入，观察 buffer 在何时被 hold、何时被释放。

#### 4.2.5 小练习与答案

**练习 1**：`DeepSeekR1Detector` 与 `Qwen3Detector` 几乎一样，唯一差别是 `force_reasoning`。这个差别在解析「`先想一下</think>答案是 42`」时会如何体现？

> 参考答案：Qwen3（`force_reasoning=False`）因看不到 `<think>` 开标记，会把整段当 normal 文本、`reasoning_text` 为空；DeepSeek-R1（`force_reasoning=True`）一启动就处在思考态，能正确把「先想一下」识别为 reasoning。

**练习 2**：为什么 `GptOssDetector` 的 `normal_text` 里要保留 `tool_call` 事件的 `raw_text`？

> 参考答案：GPT-OSS 的工具调用与思考共享同一套频道结构，`reasoning_parser` 只负责把频道归好类，但**不解析工具调用的具体内容**；保留带结构标记的 `raw_text` 是为了让下游的 `function_call_parser`（见 4.3）接力把工具调用解析出来。这体现了两类 parser 的分工与衔接。

---

### 4.3 函数调用解析（function_call_parser）

#### 4.3.1 概念说明

函数调用（function calling / tool calling）让模型能「调用外部工具」。模型并不会真的执行函数，而是按自己的格式输出一段「我想调用哪个函数、参数是什么」的文本，由服务端把这段文本解析成结构化的 `tool_calls`，再由客户端去真正执行。

麻烦在于**每个模型族的格式都不一样**：

| parser 名 | 模型族 | 典型格式 |
| --- | --- | --- |
| `qwen25` / `qwen` | Qwen2.5 / Qwen3 | `<tool_call>{"name":..,"arguments":{..}}</tool_call>` |
| `llama3` | Llama 3.2 | `<|python_tag|>{"name":..,"arguments":{..}}` |
| `mistral` | Mistral | `[TOOL_CALLS] [{..}, ..]` |
| `deepseekv3` | DeepSeek-V3 | `<｜tool▁calls▁begin｜>...<｜tool▁sep｜>...\n```json\n{..}\n```...` |
| `deepseekv31` | DeepSeek-V3.1 | 简化版 V3，参数直接内联 |
| `deepseekv32` | DeepSeek-V3.2 | DSML 标记语言 `<｜DSML｜function_calls>...` |
| `glm47` | GLM-4.7 | XML 风格 `<tool_call>fn<arg_key>k</arg_key><arg_value>v</arg_value></tool_call>` |
| `qwen3_coder` | Qwen3-Coder | XML 风格 `<function=name><parameter=p>v</parameter></function>` |
| `kimi_k2` | Kimi-K2 | `<|tool_call_begin|>functions.name:0<|tool_call_argument_begin|>{..}<|tool_call_end|>` |

`function_call_parser.py` 的职责就是**用一个统一接口，把这些五花八门的格式都解析成同一种 `ToolCallItem`**。它同样**不参与采样**，纯 CPU 后处理。

#### 4.3.2 核心流程

和推理解析一样，函数调用解析也是「**外观 + 探测器**」结构：

```text
FunctionCallParser（外观）
   │  按 tool_call_parser 查 ToolCallParserEnum 选探测器
   ▼
BaseFormatDetector（抽象基类）
   │  提供 has_tool_call / detect_and_parse（一次性）/ parse_streaming_increment（流式）
   │  维护 _buffer / current_tool_id / current_tool_name_sent / streamed_args_for_tool
   ▼
具体探测器：Qwen25Detector / Llama32Detector / MistralDetector /
            DeepSeekV3/V31/V32Detector / KimiK2Detector / Glm47Detector / Qwen3CoderDetector
```

流式解析有两个通用难点，基类都处理了：

1. **工具调用标记也可能被分片到达**（如 `<tool_c` → `all>`）——用 `_ends_with_partial_token` 判断 buffer 尾巴是否是某标记的前缀，是就继续 hold。
2. **参数 JSON 是逐渐长大的**——用 `partial_json_parser` 库解析「半个 JSON」（如 `{"city":"北` 也能取出已写完的部分），从而实现参数的增量推送。

一个关键设计是「**先发名字、再流参数**」：探测到工具调用后，第一拍先用空参数把函数名发出去（让客户端尽早知道要调谁），后续每拍把参数的增量（`argument_diff`）补上。

#### 4.3.3 源码精读

**① 统一产物与流式结果。** 无论哪种格式，最终都产出 `ToolCallItem`：

[lightllm/server/function_call_parser.py:46-60](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/function_call_parser.py#L46-L60) — `ToolCallItem`（含 `tool_index`/`name`/`parameters`）与 `StreamingParseResult`（含 `normal_text` 与 `calls`），是所有探测器对外的一致接口。

**② 半 JSON 解析工具。** 这是流式参数推送的基础：

[lightllm/server/function_call_parser.py:73-99](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/function_call_parser.py#L73-L99) — `_partial_json_loads` 借助 `partial_json_parser.loads` 解析残缺 JSON，`Allow.ALL` 允许字符串/对象/数组都不完整。

**③ 基类的流式主循环。** 这是「先发名字、再流参数」的通用实现，适用于「bot_token 紧跟 JSON 数组」的格式：

[lightllm/server/function_call_parser.py:199-376](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/function_call_parser.py#L199-L376) — `parse_streaming_increment`：先判断是否在工具调用段；解析出当前 `obj` 后，若尚未发过名字（`current_tool_name_sent=False`）就先发名字，否则计算 `argument_diff` 增量推送参数。

**④ 探测器注册表。** `tool_call_parser` 到探测器类的映射：

[lightllm/server/function_call_parser.py:1974-1985](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/function_call_parser.py#L1974-L1985) — `ToolCallParserEnum`，注意 `qwen` 与 `qwen25` 都指向 `Qwen25Detector`。

启动参数合法取值与之一致：

[lightllm/server/api_cli.py:153-170](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L153-L170) — 9 个 `--tool_call_parser` 可选值。

**⑤ Qwen25 探测器（最常见格式）。** 它在基类基础上处理「每个调用单独包一对 `<tool_call>...</tool_call>`」：

[lightllm/server/function_call_parser.py:379-466](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/function_call_parser.py#L379-L466) — `detect_and_parse` 用正则抠出所有 `<tool_call>...</tool_call>` 块再逐个 `json.loads`；`parse_streaming_increment` 调基类实现，并额外缓冲可能被分片的 `</tool_call>` 结束标记。

**⑥ GLM-4.7 / Qwen3-Coder（非 JSON 格式）。** 这类格式参数不是 JSON 而是 XML 键值对，探测器要自己做类型转换：

[lightllm/server/function_call_parser.py:1247-1272](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/function_call_parser.py#L1247-L1272) — `Glm47Detector._parse_xml_arguments` 把 `<arg_key>k</arg_key><arg_value>v</arg_value>` 解析成 dict，并尝试把 value 当 JSON 解析以支持复杂类型。

[lightllm/server/function_call_parser.py:1777-1811](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/function_call_parser.py#L1777-L1811) — `Qwen3CoderDetector._convert_param_value` 依据工具 schema 里声明的 `type`（string/integer/number/boolean/object/array）做安全类型转换，**显式拒绝 `eval()`**（用 `ast.literal_eval` 兜底）。

**⑦ 外观类的两个入口。**

[lightllm/server/function_call_parser.py:2013-2058](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/function_call_parser.py#L2013-L2058) — `parse_non_stream`（一次性，调 `detect_and_parse`）与 `parse_stream_chunk`（流式，调 `parse_streaming_increment`）。

**⑧ 与 OpenAI 层的集成。** 先看非流式：先用一个**快速门槛**判断是否值得解析，再真正调用 parser：

[lightllm/server/api_openai.py:414-437](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_openai.py#L414-L437) — `if any([i in text for i in TOOLS_TAG_LIST])` 先扫一遍所有已知工具调用标记，命中才构造 `FunctionCallParser` 并 `parse_non_stream`，避免对普通文本白白构造解析器。

`TOOLS_TAG_LIST` 是所有支持格式的开始标记汇总，作为「这个请求可能含工具调用」的廉价探测：

[lightllm/server/function_call_parser.py:35-43](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/function_call_parser.py#L35-L43) — 汇总 `<|plugin|>`/`<function=`/`<tool_call>`/`<|python_tag|>`/`[TOOL_CALLS]`/`<｜tool▁calls▁begin｜>`/`<｜DSML｜function_calls>` 等。

流式入口与非流式对称：

[lightllm/server/api_openai.py:195-209](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_openai.py#L195-L209) — `_process_tools_stream` 每个 choice 一个 `FunctionCallParser`（按 `index` 缓存），返回 `(normal_text, calls)`。

**⑨ 与推理解析的衔接顺序。** 在组装响应时，**先跑 reasoning_parser，再跑 function_call_parser**：因为思考内容里不该再去找工具调用，只有「正常内容」段才可能包含工具调用文本。这一点在流式与非流式两条路径里都体现为「先调 `_process_reasoning_stream`/`parse_non_stream` 拿到 normal，再把 normal 喂给 `_process_tools_stream`/`FunctionCallParser`」：

[lightllm/server/api_openai.py:510-536](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_openai.py#L510-L536) — 流式路径里，先用 `_process_reasoning_stream` 切出 reasoning/normal，再把 `delta`（即 normal 部分）交给 `_process_tools_stream`。

#### 4.3.4 代码实践

**实践目标**：直接用 `FunctionCallParser` 验证「不同 `tool_call_parser` 把不同格式解析成同一个 `ToolCallItem`」。

**操作步骤**：写一个最小脚本（示例代码），分别用 `qwen25` 与 `glm47` 两种 parser 解析各自的格式：

```python
# 示例代码：直接调用 lightllm 的函数调用解析器
from lightllm.server.function_call_parser import FunctionCallParser
from lightllm.server.api_models import Tool, Function

# 定义一个工具，Function 是 pydantic 模型
tools = [Tool(function=Function(name="get_weather", description="查天气"))]

# 1) Qwen 风格：JSON 参数
text_qwen = '<tool_call>\n{"name":"get_weather","arguments":{"city":"北京"}}\n</tool_call>'
p = FunctionCallParser(tools=tools, tool_call_parser="qwen25")
normal, calls = p.parse_non_stream(text_qwen)
print(calls[0].name, calls[0].parameters)
# 预期: get_weather {"city": "北京"}

# 2) GLM-4.7 风格：XML 参数
text_glm = (
    "<tool_call>get_weather\n"
    "<arg_key>city</arg_key><arg_value>北京</arg_value>\n"
    "</tool_call>"
)
p2 = FunctionCallParser(tools=tools, tool_call_parser="glm47")
normal2, calls2 = p2.parse_non_stream(text_glm)
print(calls2[0].name, calls2[0].parameters)
# 预期: get_weather {"city": "北京"}

# 3) 流式：把 Qwen 文本切成几段喂进去
p3 = FunctionCallParser(tools=tools, tool_call_parser="qwen25")
for chunk in ["<tool_call>\n", '{"name":"get', '_weather","arg', 'uments":{"city":"北京"}}\n</tool_call>']:
    normal, calls = p3.parse_stream_chunk(chunk)
    for c in calls:
        print("call:", c.name, repr(c.parameters))
```

**需要观察的现象**：两种完全不同的文本格式（JSON vs XML），最终都得到 `name="get_weather"`、`parameters='{"city": "北京"}'`，证明 parser 把异构格式归一化了。流式那段里，函数名应先于完整参数被发出。

**预期结果**：`calls` 非空且字段正确；流式时第一拍能看到只有 `name`、`parameters` 为空或半截。

**若无法本地运行**：标注「待本地验证」。退而求其次的源码阅读实践——在 `parse_streaming_increment` 的「Case 1：发名字」与「Case 2：流参数」两个分支（约 L285-L363）各加一行日志，复现上面的分片输入，观察名字与参数增量分别在哪一拍被发出。

#### 4.3.5 小练习与答案

**练习 1**：`api_openai.py` 在调用 `FunctionCallParser` 前先用 `TOOLS_TAG_LIST` 做了一次 `any(... in text ...)` 扫描。为什么需要这道「廉价门槛」？

> 参考答案：构造 `FunctionCallParser` 与逐字符正则/JSON 解析都有成本，而绝大多数请求（普通问答）根本不含工具调用。先用一个 `O(len(text)·len(tags))` 的子串扫描过滤掉这些请求，可以避免对每条普通回复都白白构造解析器，显著降低 CPU 开销。

**练习 2**：`Qwen3CoderDetector` 在转换参数值时，为什么用 `ast.literal_eval` 兜底而绝不用 `eval()`？

> 参考答案：模型生成的文本是不可信输入，`eval()` 会执行任意 Python 代码，有代码注入风险；`ast.literal_eval` 只解析字面量（数字、列表、字典等），是安全的。这是处理「把模型输出当代码」时的标准安全姿态。

**练习 3**：为什么函数调用解析必须排在推理解析**之后**，而不能反过来？

> 参考答案：思考内容（`<think>...</think>` 之间或 analysis 频道内）可能也含有形如工具调用的字符串，但那不是真正的工具调用，只是模型的自言自语。必须先把思考段切走，只在「正常内容」段里找工具调用标记，才能避免把思考过程中的样例文本误解析成 `tool_calls`。

## 5. 综合实践

把三块知识串起来，完成一次「**约束解码 + 推理解析 + 函数调用**」联合的端到端验证。

**任务**：启动一个同时开启三类能力的服务，发送一个带 JSON 约束的函数调用请求，观察三者在响应里各自留下的痕迹。

**步骤**：

1. 选一个同时支持思考与工具调用的模型（如 Qwen3）。用以下参数启动（示例命令，路径按本地替换）：

   ```bash
   python -m lightllm.server.api_server \
       --model_dir /path/to/qwen3 \
       --tool_call_parser qwen25 \
       --reasoning_parser qwen3 \
       --output_constraint_mode xgrammar \
       --tp 1 --port 8088
   ```

2. 发一个 OpenAI 风格的 chat 请求，定义 `get_weather` 工具，并要求把思考单独分离（示例代码）：

   ```python
   # 示例代码
   import requests, json
   tools = [{"type": "function", "function": {
       "name": "get_weather",
       "description": "查询城市天气",
       "parameters": {
           "type": "object",
           "properties": {"city": {"type": "string"}},
           "required": ["city"],
       },
   }}]
   data = {
       "model": "qwen3",
       "messages": [{"role": "user", "content": "北京天气怎么样？请调用工具查询。"}],
       "tools": tools,
       "tool_choice": "auto",
       "separate_reasoning": True,
       "chat_template_kwargs": {"enable_thinking": True},
   }
   r = requests.post("http://127.0.0.1:8088/v1/chat/completions", json=data).json()
   msg = r["choices"][0]["message"]
   print("reasoning:", msg.get("reasoning_content"))  # 来自 reasoning_parser
   print("tool_calls:", msg.get("tool_calls"))         # 来自 function_call_parser
   ```

3. **追踪三处痕迹**，把结果与本讲源码对应起来：
   - `reasoning_content` 非空 → 印证 `ReasoningParser.parse_non_stream` 把 `<think>...</think>` 切了出去（4.2）。
   - `tool_calls[0].function.arguments` 是合法 JSON 字符串 → 印证 `FunctionCallParser` 解析了 `<tool_call>...</tool_call>`（4.3）。
   - （进阶）再用 `guided_json` 限制工具参数的 schema，验证即便模型「想」乱写，xgrammar 掩码也会逼它合规（4.1）。

4. **画一张时序图**，标注：请求字段落在 `sampling_param`（约束）还是 request 体（parser）、每类机制发生在哪个进程、reasoning 与 function_call 的执行先后。

**预期结果**：能清楚说出「约束解码在 ModelBackend 改 logits、两类 parser 在 HttpServer 改文本、reasoning 先于 function_call」这条主线。**若本地无 GPU 或模型**，把第 2 步降级为「阅读 `api_openai.py` 的 `chat_completions_impl` 非流式分支，在源码上标注三类机制各自的调用点」，并标注「待本地验证」。

## 6. 本讲小结

- LightLLM 的「结构化输出」由三套**正交**机制组成：约束解码侵入采样、两类 parser 纯后处理。
- **约束解码**（`output_constraint_mode`）在采样前用 FSM/文法匹配器把不合法 token 的 logit 压成 `-1000000.0`，实现「正则/JSON Schema/文法」级保证；由 `XgrammarBackend`/`OutlinesConstraintBackend` 两个专用后端承载，靠替换 prefill/decode/post 三个钩子完成。
- CLI 真实合法值是 `outlines/xgrammar/none`（以 `api_cli.py` 为准），`start_args_type.py` 里的 `"simple"` 是过时声明。
- **推理解析**（`reasoning_parser`）用「外观 + 探测器」把思考链与答案分开，靠 `force_reasoning` 与「hold 住标记前缀」两个机制应对「R1 无开标记」与「标记被流式分片」两种边界。
- **函数调用解析**（`tool_call_parser`）同样用探测器模式，把 9 种模型族格式归一为 `ToolCallItem`；流式时遵循「先发名字、再流参数」，靠 `partial_json_parser` 解析半截 JSON。
- 两类 parser 都住在 HttpServer/OpenAI 适配层（CPU，反 token 化之后），且**先 reasoning、后 function_call**；`TOOLS_TAG_LIST` 提供廉价预筛避免对普通文本白跑解析。

## 7. 下一步学习建议

- 若你想知道「约束后端替换的那三个钩子，究竟是怎么挂进 prefill/decode 主循环的」，回到 **u3-l2（prefill 与 decode 推理主流程）** 对照 `ChunkedPrefillBackend` 的 `prefill_mask_func`/`extra_post_req_handle_func` 调用点细读。
- 若你对采样本身（top-k/top-p 如何作用于被掩码后的 logits）还想加深，回看 **u3-l6（采样与后处理）**。
- 若你想了解约束解码之外、面向「首 token」的更轻量约束（`first_token_constraint_mode`，用环境变量 `FIRST_ALLOWED_TOKENS` 限定第一步可选范围），可阅读 [lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl_for_first_token_constraint_mode.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl_for_first_token_constraint_mode.py)。
- 下一讲 **u7-l7（指标监控与健康检查）** 将离开「请求内容」层面，转向「系统运行状态」的采集与暴露，与本讲的业务语义解析形成互补。

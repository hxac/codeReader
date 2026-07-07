# OpenAI/Anthropic/Responses 端点

## 1. 本讲目标

本讲承接 u7-l1 建立的「请求解析并发、推理串行」的服务器骨架，专门拆解 **HTTP 端点这一层**：客户端发来的 JSON 请求，是怎么按 URL 路由到不同的解析函数、又被翻译成引擎能理解的统一 `request` 结构体的。

学完本讲，你应当能够：

1. 说出 `ds4-server` 暴露的全部端点（GET 与 POST），并指出每个 POST 端点对应的解析函数。
2. 给定一个客户端请求 JSON，判断它属于 OpenAI Chat、OpenAI Responses、Anthropic、还是传统 Completions 风格，并说出每个字段被映射到 `request` 的哪个成员。
3. 理解 `model` 字段的「别名」语义：为什么 `deepseek-v4-flash` 与 `deepseek-v4-pro` 都不会真的去换一个模型，以及 `deepseek-chat` / `deepseek-reasoner` 如何隐式控制思考模式。
4. 知道哪些参数会被服务器接受、哪些会被显式拒绝（例如 Responses 的 `tool_choice=required`）。

本讲**只讲请求解析与参数映射**，不展开 SSE 流式输出（u7-l3）、工具调用 DSML 回放（u7-l4）与前缀复用（u7-l5）。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **服务器线程模型**（u7-l1）：每条 HTTP 连接由一个客户端线程（`client_main`）处理，它负责读 HTTP、解析 JSON、把作业塞进共享 FIFO 队列；全局唯一的 graph worker 串行执行推理。本讲的「端点分发」就发生在 `client_main` 里、入队之前。
- **`request` 是统一中间表示**：无论客户端用的是哪种 API 方言，解析完都会产出同一个 `request` 结构体，graph worker 只认 `request`，不关心它来自哪个端点。
- **采样默认值**（u4-l3）：`temperature=1`、`top_p=1`、`min_p=0.05` 是默认采样过滤器；思考模式下服务器固定使用这套默认值，忽略客户端旋钮。
- **JSON 是手写递归下降解析**：`ds4_server.c` 没有引入第三方 JSON 库，而是用 `json_string` / `json_int` / `json_number` / `json_bool` / `json_skip_value` 等小函数逐 token 推进一个 `const char *p` 游标。理解本讲的参数映射，关键就是理解「读到某个 key，就调用对应的 json 函数把值塞进 `request`」这个重复模式。

一个需要提前澄清的概念：**API 方言（api_style）** 与 **请求种类（req_kind）** 是两个正交的标签：

```c
typedef enum { REQ_CHAT, REQ_COMPLETION } req_kind;        // 请求种类
typedef enum { API_OPENAI, API_ANTHROPIC, API_RESPONSES } api_style; // API 方言
```

它们定义在 [ds4_server.c:488-497](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L488-L497)。`req_kind` 区分「聊天补全」还是「原始文本补全」，`api_style` 区分「OpenAI / Anthropic / Responses」三种响应格式与协议细节。后文会看到，四个 POST 端点就是通过给同一个 `request` 打上不同的 `(kind, api)` 组合来区格的。

## 3. 本讲源码地图

本讲几乎全部源码都集中在一个文件里：

| 文件 | 作用 |
| --- | --- |
| `ds4_server.c` | 服务器全部实现。本讲涉及端点路由（`client_main`）、四个解析函数、`request` 结构体定义、模型别名工具函数、默认配置。 |
| `README.md` | 「Supported endpoints」「Tool call handling」段落，是端点行为的权威文档，也是 curl 示例的来源。 |

`ds4_server.c` 内本讲涉及的关键锚点（行号基于当前 HEAD `80ebbc3`）：

| 锚点 | 行号 | 作用 |
| --- | --- | --- |
| `req_kind` / `api_style` 枚举 | 488–497 | 两个正交标签 |
| `request` 结构体 | 597–647 | 四类端点共享的统一中间表示 |
| `request_init` 默认值 | 760–771 | 所有请求的采样/思考默认值 |
| 模型别名工具函数 | 901–918 | `deepseek-chat`/`deepseek-reasoner`/flash/pro 的别名判定 |
| `parse_chat_request` | 2635–2804 | OpenAI Chat 端点解析 |
| `parse_anthropic_request` | 2806–3006 | Anthropic `/v1/messages` 解析 |
| `parse_responses_request` | 3696–3959 | OpenAI Responses `/v1/responses` 解析 |
| `parse_completion_request` | 3991–4131 | 传统文本补全 `/v1/completions` 解析 |
| `send_models` / `send_model` | 11217–11236 | GET `/v1/models` 列表与单个模型 |
| 端点路由分发 | 11265–11301 | `client_main` 里的 if/else 路由 |
| 入队前的收尾 | 11302–11316 | model 兜底、上下文长度校验 |
| 默认 server 配置 | 11515–11526 | 默认 host/port/ctx |

## 4. 核心概念与源码讲解

本讲按规格拆成三个最小模块：**端点路由**、**参数映射**、**模型别名**。

### 4.1 端点路由：HTTP 方法与路径分发

#### 4.1.1 概念说明

`ds4-server` 对外暴露的是一组**与主流 AI 服务兼容**的 HTTP 端点。所谓「兼容」，是指客户端（OpenAI SDK、Anthropic SDK、Codex CLI、Claude Code 等）可以几乎不改代码地把 base URL 指向本地 `ds4-server`，就当作是在调 OpenAI 或 Anthropic 的官方 API。

这种兼容靠两件事实现：

1. **URL 路径**完全照搬官方：`/v1/chat/completions`、`/v1/responses`、`/v1/messages`、`/v1/completions`、`/v1/models`。
2. 每个 URL 在服务器内部对应一个**专门的解析函数**，它把该 API 方言的请求字段翻译成统一的 `request`。换句话说，URL 决定了「用哪种方言去读 JSON」。

README 把端点清单列得很清楚（[README.md:717-725](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L717-L725)）：

```
GET  /v1/models
GET  /v1/models/deepseek-v4-flash
GET  /v1/models/deepseek-v4-pro
POST /v1/chat/completions
POST /v1/responses
POST /v1/completions
POST /v1/messages
```

#### 4.1.2 核心流程

路由发生在客户端线程 `client_main` 的开头。处理顺序是：

1. 读 HTTP 请求行与头（`read_http_request`），失败回 400。
2. `OPTIONS` 方法直接回 204（CORS 预检）。
3. `GET /v1/models` → `send_models`（返回 flash 与 pro 两个模型对象）。
4. `GET /v1/models/<alias>` → 若别名已知则 `send_model`，否则继续往下走（最终 404）。
5. 四个 `POST` 端点 → 分别调用四个 `parse_*_request`，把 JSON body 解析进 `request`。
6. 解析成功后做收尾（model 兜底、上下文长度校验），构造 `job` 入队。

整个分发是一个**线性的 if/else if 链**，按 `method + path` 精确字符串匹配，没有任何框架式的路由表。

#### 4.1.3 源码精读

GET 端点（[ds4_server.c:11265-11279](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11265-L11279)）：`/v1/models` 直接列出两个模型；`/v1/models/` 前缀的路由会先用 `server_model_alias_known` 校验别名，避免任意路径都被当成模型名。

```c
if (!strcmp(hr.method, "GET") && !strcmp(hr.path, "/v1/models")) {
    send_models(s, fd);
    ...
}
const char *model_path_prefix = "/v1/models/";
...
if (!strcmp(hr.method, "GET") &&
    !strncmp(hr.path, model_path_prefix, model_path_prefix_len) &&
    server_model_alias_known(hr.path + model_path_prefix_len))
{
    send_model(s, fd, hr.path + model_path_prefix_len);
    ...
}
```

POST 端点分发（[ds4_server.c:11285-11301](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11285-L11301)）：注意每个分支调用的是**不同的解析函数**，但它们都向同一个 `request req` 写入，并共享 `ctx_size`（当前 session 的上下文容量，用于稍后的思考模式降级与长度校验）：

```c
request req;
char err[160];
bool ok = false;
const int ctx_size = ds4_session_ctx(s->session);
if (!strcmp(hr.method, "POST") && !strcmp(hr.path, "/v1/messages")) {
    ok = parse_anthropic_request(s->engine, s, hr.body, s->default_tokens, ctx_size, &req, err, sizeof(err));
} else if (!strcmp(hr.method, "POST") && !strcmp(hr.path, "/v1/chat/completions")) {
    ok = parse_chat_request(s->engine, s, hr.body, s->default_tokens, ctx_size, &req, err, sizeof(err));
} else if (!strcmp(hr.method, "POST") && !strcmp(hr.path, "/v1/responses")) {
    ok = parse_responses_request(s->engine, s, hr.body, s->default_tokens, ctx_size, &req, err, sizeof(err));
} else if (!strcmp(hr.method, "POST") && !strcmp(hr.path, "/v1/completions")) {
    ok = parse_completion_request(s->engine, hr.body, s->default_tokens, ctx_size, &req, err, sizeof(err));
} else {
    http_error(fd, s->enable_cors, 404, "unknown endpoint");
    ...
}
```

任何不匹配的路径都落到 `else`，返回 404 `unknown endpoint`。这就是端点路由的全部：**字符串精确匹配，一一对应到一个解析函数**。

入队前还有两步收尾（[ds4_server.c:11308-11316](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11308-L11316)）：如果请求体里没带 `model` 字段，就用引擎实际加载的模型 id 兜底；随后用 `request_exceeds_context` 检查 prompt token 数是否超过了 `ctx_size`，超了就回一个协议化的「context length exceeded」错误（错误体格式还会按 `api_style` 区分，Anthropic 用 `{"type":"error",...}` 形态）。

#### 4.1.4 代码实践

**实践目标**：在不运行服务器的前提下，画出完整的端点路由表。

**操作步骤**：

1. 打开 [ds4_server.c:11247](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11247) 起的 `client_main`。
2. 从上到下，把每个 `if (!strcmp(hr.method, ...) && !strcmp(hr.path, ...))` 分支抄下来。
3. 记录每个分支调用的处理函数与最终给 `request` 打上的 `(kind, api)` 标签（解析函数开头第一行的 `request_init` 与紧随其后的 `r->api = ...` 会告诉你答案）。

**需要观察的现象**：你会得到一张 7 行的表（2 个 GET + 4 个 POST + 1 个 else 404），且能看出「GET 不构造 `request`、直接回 JSON；POST 才构造 `request` 入队」。

**预期结果**：表格形如「`POST /v1/completions` → `parse_completion_request` → `kind=REQ_COMPLETION, api=API_OPENAI`」。其余三行同理可填。

> 待本地验证：若你已构建好 `ds4-server` 并加载了模型，可用 `curl -i http://127.0.0.1:8000/v1/models` 验证 GET 端点，用 `curl -i -X POST http://127.0.0.1:8000/v1/nope` 验证 404 分支。

#### 4.1.5 小练习与答案

**练习 1**：如果客户端发来 `GET /v1/chat/completions`（方法错了），服务器会怎么响应？

**答案**：所有 chat/responses/messages/completions 分支都同时检查 `hr.method=="POST"`，方法不匹配会逐个 fall through，最终命中 `else` 返回 404 `unknown endpoint`。

**练习 2**：为什么 `GET /v1/models/anything-else`（别名未知）不会返回一个模型对象？

**答案**：路由分支用 `server_model_alias_known(...)` 做了门禁（[ds4_server.c:11274](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11274)），该函数只接受 `deepseek-v4-flash` 与 `deepseek-v4-pro`，其余路径继续往下走最终 404。

---

### 4.2 参数映射：四类端点 → request 结构体

#### 4.2.1 概念说明

四个 POST 端点虽然方言不同，但解析后的归宿是同一个 `request` 结构体（[ds4_server.c:597-647](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L597-L647)）。它的核心成员包括：

- `kind` / `api`：两个正交标签；
- `prompt`（`ds4_tokens`）：最终送进引擎的 token 序列；
- `prompt_text`：渲染后的纯文本提示（供 `--trace` 调试用）；
- 采样旋钮：`temperature` / `top_p` / `min_p` / `top_k` / `seed`；
- `max_tokens`、`stream`、`stream_include_usage`、`stops`（停止序列）；
- `think_mode`（思考档位）、`has_tools`、`tool_orders`、`model`、`model_from_request` 等。

所有请求一开始都由 `request_init` 灌入同一套默认值（[ds4_server.c:760-771](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L760-L771)）：

```c
r->kind = kind;
r->api = API_OPENAI;                 // 默认 OpenAI 方言，Anthropic/Responses 会覆盖
r->model = xstrdup("deepseek-v4-flash");
r->max_tokens = max_tokens;
r->top_k = 0;
r->temperature = DS4_DEFAULT_TEMPERATURE;   // 1.0
r->top_p = DS4_DEFAULT_TOP_P;               // 1.0
r->min_p = DS4_DEFAULT_MIN_P;               // 0.05
r->think_mode = DS4_THINK_HIGH;
```

每个解析函数都是同一个套路：手写一个 JSON 对象遍历循环——读 key 字符串、按 key 名把值塞进 `request` 的对应字段、未知 key 用 `json_skip_value` 跳过。下面把四个端点识别的字段做成一张对照表。

#### 4.2.2 核心流程：字段对照表

下表汇总四个端点各自识别的请求字段（来自四个解析函数的 `if (!strcmp(key, ...))` 分支）。「→」表示映射到的 `request` 字段或行为。

| 字段 | `/v1/chat/completions` | `/v1/responses` | `/v1/messages` | `/v1/completions` |
| --- | --- | --- | --- | --- |
| 对话主体 | `messages`（必填）→ `parse_messages` | `input`（必填，可为字符串或数组）→ `parse_responses_input` | `messages`（必填）→ `parse_anthropic_messages` | `prompt`（必填）→ `parse_prompt` |
| 系统提示 | （写在 messages 里 role=system） | `instructions`（替换任何 system，前置到头部） | `system`（顶层字段，解析后追加为 system 消息） | （无，硬编码 "You are a helpful assistant"） |
| 模型 | `model` | `model` | `model` | `model` |
| 最大 token | `max_tokens` / `max_completion_tokens` | `max_output_tokens` / `max_tokens` | `max_tokens` | `max_tokens` |
| 采样 | `temperature` / `top_p` / `min_p` / `top_k` / `seed` | `temperature` / `top_p` | `temperature` / `top_p` / `top_k` | `temperature` / `top_p` / `min_p` / `top_k` / `seed` |
| 流式 | `stream` / `stream_options.include_usage` | `stream` | `stream` | `stream` / `stream_options.include_usage` |
| 停止序列 | `stop` | （无原生字段） | `stop_sequences` | `stop` |
| 工具 | `tools` / `tool_choice`（字符串 `"none"`） | `tools` / `tool_choice`（`"none"`/`"auto"`，其余拒绝） | `tools` / `tool_choice`（对象 `{type:"none"}`） | （不支持工具） |
| 思考控制 | `thinking` / `reasoning_effort` / `think` | `reasoning`（含 effort + summary） | `thinking` / `output_config` / `reasoning_effort` | `thinking` / `reasoning_effort` / `think` |
| 协议特有 | — | `previous_response_id` / `conversation`（须为 null） | — | — |

这张表是本讲最重要的产出。几个关键差异值得记住：

- **必填字段不同**：chat/anthropic 要 `messages`，responses 要 `input`，completions 要 `prompt`。缺了对应字段会返回 `missing messages` / `missing input` / `missing prompt`。
- **系统提示的位置不同**：OpenAI chat 把 system 放在 `messages` 数组里；Anthropic 把 `system` 放在顶层；Responses 用 `instructions` 并且会**替换**任何 system 消息。
- **Responses 显式拒绝若干字段**：`tool_choice` 只接受 `"none"` 与 `"auto"`，`"required"` 或对象形式的强制工具会被拒绝（因为那需要服务器未实现的受限解码）；`previous_response_id` / `conversation` 必须为 `null`（服务器尚未实现持久化状态存储）。

#### 4.2.3 源码精读

**OpenAI Chat**（[ds4_server.c:2660-2768](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L2660-L2768)）展示了标准的「读 key → 填字段」循环。摘录几条典型分支：

```c
if (!strcmp(key, "messages")) { ... parse_messages(&p, &msgs); got_messages = true; }
else if (!strcmp(key, "tools")) { ... parse_tools_value(&p, &tool_schemas, &r->tool_orders); }
else if (!strcmp(key, "model")) { free(r->model); json_string(&p, &r->model); r->model_from_request = true; }
else if (!strcmp(key, "max_tokens") || !strcmp(key, "max_completion_tokens")) { json_int(&p, &r->max_tokens); }
else if (!strcmp(key, "temperature")) { double v; json_number(&p, &v); r->temperature = (float)v; }
...
else if (!json_skip_value(&p)) { goto bad; }   // 未知 key：跳过其值
```

注意最后那条 `else`：**未识别的字段不会被当成错误**，而是用 `json_skip_value` 整体跳过。这是兼容性的关键——客户端发的额外字段（如 `frequency_penalty`、`user`、`n` 等）不会让请求失败，只是被忽略。

**Anthropic `/v1/messages`**（[ds4_server.c:2806-2965](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L2806-L2965)）：开头先把方言标记改成 Anthropic（`r->api = API_ANTHROPIC`），然后处理 `system`（顶层字符串）与 Anthropic 风格的 `tool_choice`（对象，取其 `type` 字段）。它的 `system` 在循环结束后才被追加为一条 role=system 消息（[ds4_server.c:2974-2980](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L2974-L2980)）：

```c
if (system && system[0]) {
    chat_msg msg = {0};
    msg.role = xstrdup("system");
    msg.content = system;       // 转移所有权
    chat_msgs_push(&msgs, msg);
}
```

**Responses `/v1/responses`**（[ds4_server.c:3696-3883](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L3696-L3883)）：方言标记为 `API_RESPONSES`。它的 `input` 字段既容忍裸字符串（包成一条 user 消息），也容忍标准数组（`parse_responses_input`）。最值得看的是它对不支持字段的**显式拒绝**逻辑。`tool_choice`（[ds4_server.c:3762-3800](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L3762-L3800)）：

```c
if (!strcmp(choice, "none")) { tool_choice_none = true; }
else if (strcmp(choice, "auto") != 0) {
    snprintf(err, errlen, "tool_choice=%s not supported", choice);
    ... return false;   // 直接报错，而不是静默降级
}
```

注释里把设计意图说得很直白：宁可让客户端看到「不支持」，也不要静默地把 `required` 降级成 `auto`。同理，`previous_response_id` / `conversation`（[ds4_server.c:3849-3874](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L3849-L3874)）若非 `null` 就报错 `<key> is not supported; replay full input instead`——因为接受一个无法解析的持久化引用会**悄悄截断 prompt**。

Responses 的思考控制也更复杂，走 `reasoning` 对象（[ds4_server.c:3832-3848](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L3832-L3848)），同时解析 effort 档位与是否输出 `reasoning_summary`，且只有显式给了 effort 才算客户端「介入」思考控制。

**传统 Completions `/v1/completions`**（[ds4_server.c:3991-4131](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L3991-L4131)）：它是四个里最「特殊」的一个——`kind` 是 `REQ_COMPLETION` 而非 `REQ_CHAT`，且它**不渲染完整聊天模板**，而是自己手拼一个最小提示（[ds4_server.c:4115-4124](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L4115-L4124)）：

```c
buf rendered = {0};
buf_puts(&rendered, "<｜begin▁of▁sentence｜>");
if (r->think_mode == DS4_THINK_MAX) buf_puts(&rendered, ds4_think_max_prefix());
buf_puts(&rendered, "You are a helpful assistant<｜User｜>");
buf_puts(&rendered, prompt);
buf_puts(&rendered, "<｜Assistant｜>");
buf_puts(&rendered, ds4_think_mode_enabled(r->think_mode) ? "<think>" : "</think>");
```

这是「文本补全」API 的语义：客户端给一段裸 prompt，服务器替它套上 DeepSeek 的对话外壳。它也不支持 `tools`。

最后，三个聊天类端点（chat/anthropic/responses）在解析完消息后都会走同一段收尾：调用 `render_chat_prompt_text` 渲染统一聊天文本、`ds4_tokenize_rendered_chat` 分词、把工具回放信息挂到消息上（这部分细节属于 u7-l4）。也就是说，**四种方言最终都汇流到同一份 `prompt` token 序列**，graph worker 完全不感知客户端用了哪种 API。

#### 4.2.4 代码实践

**实践目标**：用一条真实的 curl 请求，对照源码列出 `parse_chat_request` 识别的每一个字段。

**操作步骤**：

1. 启动服务器（需要已构建且加载了模型；若不具备，跳到步骤 3 的源码阅读部分）：
   ```sh
   ./ds4-server --ctx 32768
   ```
2. 发送一个流式 chat 请求（来自 [README.md:805-813](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L805-L813) 的最小示例）：
   ```sh
   curl http://127.0.0.1:8000/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -d '{
       "model":"deepseek-v4-flash",
       "messages":[{"role":"user","content":"List three Redis design principles."}],
       "stream":true
     }'
   ```
3. 对照 [ds4_server.c:2660-2768](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L2660-L2768)，逐个 `if (!strcmp(key, ...))` 分支打勾：`model` ✓（→ `r->model`）、`messages` ✓（→ `parse_messages`）、`stream` ✓（→ `r->stream`）。

**需要观察的现象**：流式响应是一系列 `data: {...}\n\n` 的 SSE 事件（u7-l3 详述）；非流式则是一个完整 JSON。若你故意加一个不存在的字段（如 `"frequency_penalty": 0.5`），请求仍应成功——因为它会命中 `json_skip_value` 分支被忽略。

**预期结果**：你能写出一份「curl 用到的字段 → 源码分支 → `request` 成员」的三列对照清单，例如：

| curl 字段 | 源码分支 | request 成员 |
| --- | --- | --- |
| `model` | `!strcmp(key,"model")` | `r->model` + `r->model_from_request=true` |
| `messages` | `!strcmp(key,"messages")` | 经 `parse_messages` → 最终 `r->prompt` |
| `stream` | `!strcmp(key,"stream")` | `r->stream` |

> 待本地验证：步骤 1–2 需要可运行的 `ds4-server` 与已下载的 GGUF 模型；若环境不具备，步骤 3 的源码阅读型实践可独立完成。

#### 4.2.5 小练习与答案

**练习 1**：客户端给 `/v1/responses` 发了 `"tool_choice": "required"`，会发生什么？为什么这样设计？

**答案**：`parse_responses_request` 会返回错误 `tool_choice=required not supported`，请求以 400 失败（[ds4_server.c:3774-3786](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L3774-L3786)）。设计理由（见注释）：`required` 与显式函数目标需要服务器未实现的受限解码，显式报错比静默降级成 `auto` 更诚实。

**练习 2**：为什么 `/v1/completions` 没有 `tools` 字段，但它和 `/v1/chat/completions` 共享同一套采样字段？

**答案**：`/v1/completions` 是「原始文本补全」语义（`kind=REQ_COMPLETION`），它把整段 prompt 当作裸文本，没有对话角色与工具调用概念，因此解析函数里根本没有 `tools` 分支；但采样（temperature/top_p/...）是所有生成任务共有的，所以沿用 `request` 的同一组采样成员。

---

### 4.3 模型别名：model 字段的语义

#### 4.3.1 概念说明

许多客户端 SDK 要求请求体里带一个 `model` 字段，否则报错。但 `ds4-server` 一次只加载一个 GGUF（由启动时的 `-m` / 默认 `ds4flash.gguf` 决定），不存在「按 model 字段切换模型」的能力。于是服务器采用了一套**别名**策略，README 把它说得很清楚（[README.md:727-729](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L727-L729)）：

> The Flash and PRO model endpoints are compatibility aliases. They both report the model currently loaded from the GGUF passed with `-m`; the endpoint name does not select a different model.

换句话说，`model` 字段在 ds4 里**几乎是纯装饰**——它只用来：(a) 让客户端 SDK 满意；(b) 在响应里原样回显；(c) 个别别名会隐式影响思考模式。

除了 `deepseek-v4-flash` / `deepseek-v4-pro`，还有两个会**改变行为**的别名：

- `deepseek-chat`：等价于「关闭思考」（对齐 DeepSeek 官方 chat 模型语义）。
- `deepseek-reasoner`：等价于「开启思考」（对齐 DeepSeek 官方 reasoner 模型语义）。

#### 4.3.2 核心流程

`model` 字段的处理分四步：

1. **默认值**：`request_init` 把 `r->model` 初始化为 `"deepseek-v4-flash"`，`r->model_from_request = false`。
2. **请求覆盖**：解析函数遇到 `model` key 时，用请求里的字符串覆盖 `r->model`，并置 `model_from_request = true`。
3. **入队前兜底**（`client_main`）：如果请求里**没带** `model`（`!model_from_request`），就用引擎实际加载的模型 id 覆盖，保证响应里回显的是真实模型。
4. **思考模式联动**：若客户端没有显式控制思考（没发 `thinking`/`reasoning`/`reasoning_effort` 等字段），则根据 `model` 别名隐式决定开/关思考。

引擎实际加载的是 flash 还是 pro，由 `ds4_engine_model_id(engine)` 返回值决定（`1` = PRO，其余 = Flash）。

#### 4.3.3 源码精读

引擎 → 别名字符串的映射（[ds4_server.c:909-912](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L909-L912)）：

```c
static const char *server_model_id_from_engine(ds4_engine *engine) {
    return ds4_engine_model_id(engine) == 1 ?
           "deepseek-v4-pro" : "deepseek-v4-flash";
}
```

GET 路由用来判断别名是否「已知」（[ds4_server.c:914-918](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L914-L918)），只认 flash 与 pro：

```c
static bool server_model_alias_known(const char *id) {
    return id &&
           (!strcmp(id, "deepseek-v4-flash") ||
            !strcmp(id, "deepseek-v4-pro"));
}
```

两个会改变思考行为的别名（[ds4_server.c:901-907](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L901-L907)）：

```c
static bool model_alias_disables_thinking(const char *model) {
    return model && !strcmp(model, "deepseek-chat");
}
static bool model_alias_enables_thinking(const char *model) {
    return model && !strcmp(model, "deepseek-reasoner");
}
```

这俩别名在四个解析函数里都以**完全相同**的两行被消费——只有当客户端没有显式控制思考（`!got_thinking`）时才生效。以 chat 端点为例（[ds4_server.c:2783-2784](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L2783-L2784)）：

```c
if (!got_thinking && model_alias_disables_thinking(r->model)) thinking_enabled = false;
if (!got_thinking && model_alias_enables_thinking(r->model)) thinking_enabled = true;
```

入队前的 model 兜底（[ds4_server.c:11308-11311](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11308-L11311)）：

```c
if (!req.model_from_request) {
    free(req.model);
    req.model = xstrdup(server_model_id_from_engine(s->engine));
}
```

GET `/v1/models` 列表则无脑同时列出 flash 与 pro 两个对象（[ds4_server.c:11226-11236](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11226-L11236)），让客户端以为有两个模型可选，但二者背后是同一个 GGUF。

一个重要后果：**`model` 字段不会触发任何校验失败**。客户端发 `"model": "gpt-4o"` 也不会被拒绝——它只是被存进 `r->model` 并在响应里原样回显，引擎照样用启动时加载的那个 GGUF 推理。这是刻意的兼容性设计：让任意 OpenAI 客户端都能无修改地连上来。

#### 4.3.4 代码实践

**实践目标**：验证 `model` 字段是「装饰性」的，且 `deepseek-chat` 会隐式关闭思考。

**操作步骤**：

1. 阅读四个解析函数，确认它们都包含相同的两行 `model_alias_disables/enables_thinking` 判定（chat: [ds4_server.c:2783-2784](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L2783-L2784)；anthropic: [ds4_server.c:2982-2983](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L2982-L2983)；responses: [ds4_server.c:3921-3922](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L3921-L3922)；completions: [ds4_server.c:4111-4112](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L4111-L4112)）。
2. 推演：若客户端发 `"model": "deepseek-chat"` 且不带任何思考控制字段，`thinking_enabled` 会被置为什么？`think_mode` 最终档位如何？

**需要观察的现象**：`deepseek-chat` 命中 `model_alias_disables_thinking`，`thinking_enabled = false`；再经 `think_mode_from_enabled(false, ...)` 与 `ds4_think_mode_for_context(...)` 后，思考被关闭。

**预期结果**：得到结论「`model` 字段对选哪个 GGUF 毫无影响，但 `deepseek-chat`/`deepseek-reasoner` 会在客户端未显式控制时隐式切换思考开关」。

> 待本地验证：在运行中的服务器上，分别用 `"model":"deepseek-chat"` 与 `"model":"deepseek-reasoner"` 发同一句提问，观察响应里是否包含 `<think>...</think>` 推理段（思考模式相关细节见 u7-l3）。

#### 4.3.5 小练习与答案

**练习 1**：客户端没发 `model` 字段，响应里的 `model` 会是什么？

**答案**：`request_init` 先给默认值 `deepseek-v4-flash`，但 `model_from_request` 为 false，于是 `client_main` 在入队前用 `server_model_id_from_engine(s->engine)` 覆盖（[ds4_server.c:11308-11311](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11308-L11311)）。若加载的是 Flash GGUF，响应里就是 `deepseek-v4-flash`。

**练习 2**：为什么发 `"model":"gpt-4o"` 不会报错？

**答案**：解析函数只把 `model` 当字符串存进 `r->model`，没有任何「已知模型」校验；未知 key 才会被 `json_skip_value` 跳过，而 `model` 是已知 key，必定被接受。这保证任意 OpenAI 兼容客户端都能连上。

---

## 5. 综合实践

把三个模块串起来：**为四种 API 方言各写一条最小的 curl 请求，并标注它会命中哪个解析函数、最终给 `request` 打上什么 `(kind, api)` 标签、`model` 字段会如何被处理。**

建议步骤：

1. 准备四条请求体（不必都发出去，重点是写对字段名）：
   - **OpenAI Chat**：POST `/v1/chat/completions`，body 含 `messages`、`model`、`stream`。（参考 [README.md:805-813](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L805-L813)）
   - **Responses**：POST `/v1/responses`，body 含 `input`（数组）、`instructions`、`reasoning`。
   - **Anthropic**：POST `/v1/messages`，body 含 `system`（顶层）、`messages`、`max_tokens`。
   - **Completions**：POST `/v1/completions`，body 含 `prompt`、`stream`。
2. 对每条请求，在源码里定位它命中的解析函数（路由见 [ds4_server.c:11285-11301](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11285-L11301)），写出 `(kind, api)`：
   - chat → `(REQ_CHAT, API_OPENAI)`
   - responses → `(REQ_CHAT, API_RESPONSES)`
   - messages → `(REQ_CHAT, API_ANTHROPIC)`
   - completions → `(REQ_COMPLETION, API_OPENAI)`
3. 对每条请求的每个字段，在 4.2.2 的对照表里找到它映射到 `request` 的哪个成员。
4. 进阶：在 Anthropic 请求里把 `tool_choice` 写成 `{"type":"none"}`，在 Responses 请求里把 `tool_choice` 写成 `"required"`，预测分别会发生什么（前者禁用工具，后者 400 报错）。

完成后，你应当能用一张表回答：「给定任意一份客户端请求 JSON，它走哪条路、每个字段去哪儿、会不会被拒绝」。这张表就是本讲的全部收获。

## 6. 本讲小结

- `ds4-server` 用 `client_main` 里一条线性的 `method + path` 字符串匹配链做端点路由：2 个 GET（`/v1/models`、`/v1/models/<alias>`）直接回 JSON，4 个 POST 各自调用一个 `parse_*_request` 解析进统一的 `request`。
- 四类 POST 端点对应四种 API 方言，由 `(req_kind, api_style)` 两个正交标签区分：chat=`(REQ_CHAT, API_OPENAI)`、responses=`(REQ_CHAT, API_RESPONSES)`、messages=`(REQ_CHAT, API_ANTHROPIC)`、completions=`(REQ_COMPLETION, API_OPENAI)`。
- 参数映射遵循统一套路：手写 JSON 对象遍历，读 key → 填 `request` 成员，未知 key 用 `json_skip_value` 跳过（所以额外字段不会报错）。
- 四种方言在「必填字段」「系统提示位置」「工具字段形态」上差异最大；Responses 还会显式拒绝 `tool_choice=required` 与 `previous_response_id`/`conversation` 非空等不支持的能力。
- `model` 字段是装饰性的：`deepseek-v4-flash`/`deepseek-v4-pro` 是兼容别名（不切换 GGUF），`deepseek-chat`/`deepseek-reasoner` 会在客户端未显式控制时隐式关/开思考；任意 `model` 字符串都不会触发校验失败。
- 四种方言最终都汇流到同一份 `prompt` token 序列，graph worker 完全不感知客户端用了哪种 API——这是「窄引擎、宽兼容」的关键。

## 7. 下一步学习建议

本讲只覆盖了「请求进得来」这一段。请求被解析成 `request`、入队之后，还有三块重要机制建议依次深入：

1. **u7-l3 SSE 流式输出与 thinking 模式**：本讲多次提到 `stream` 字段，但流式事件的生命周期（OpenAI 的 `data:` 分块、Responses 的 `response.output_text.delta`、Anthropic 的 `tool_use` 块）在那里详述。
2. **u7-l4 工具调用：DSML、精确回放、规范化**：本讲的 `tools`/`tool_choice` 字段只是入口；工具调用如何被渲染成 DSML、如何用 tool id 做精确回放、回退路径如何改写 KV checkpoint，是服务器最精巧的部分。
3. **u7-l5 实时 KV 前缀复用与检查点改写**：本讲提到「四种方言最终汇流到同一份 prompt」，而服务器正是靠这份 prompt 的 token 前缀去复用单一活 KV checkpoint——这条复用链是 `ds4_session` 层面的，在那里展开。

如果对客户端侧集成感兴趣，可顺带阅读 README 的「Agent Client Usage」段（[README.md:815-830](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L815-L830)），看 Codex CLI 与 Claude Code 如何通过这些端点连上 ds4-server。

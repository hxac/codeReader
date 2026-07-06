# SSE 流式输出与 thinking 模式

## 1. 本讲目标

本讲承接 u7-l2（端点路由与参数映射），往下钻一层到「请求一旦决定 `stream:true`，服务器到底往那条 HTTP 连接上写什么」。

学完本讲，你应当能够：

1. 说出 ds4-server 在三种主流 API 方言（OpenAI Chat、OpenAI Responses、Anthropic）下各自采用的 **SSE 事件生命周期**——连接先发什么、中间增量是什么、结束发什么。
2. 解释 **thinking 模式下推理内容（`<think>...</think>`）如何被翻译成各 API 的「原生形态」**：OpenAI Chat 的 `reasoning_content`、Anthropic 的 `thinking` 内容块、Responses 的 `reasoning` 输出项。
3. 讲清 `stream_options.include_usage` 的作用：它如何让 OpenAI 风格的流在结尾额外吐一个带 `usage` 的 chunk，以及它和 Anthropic/Responses 的「使用量」语义有什么不同。
4. 理解流式输出的几个工程细节：为什么要在 prefill 期间发心跳、为什么要在 UTF-8 字符边界处「扣住」一段不发、以及「推理被截断」时为什么要标成 `incomplete`。

本讲**只讲流式输出协议本身**，不展开工具调用的 DSML 回放（u7-l4）与前缀复用（u7-l5），也不重述 thinking 模式在分词阶段的实现（见 u3-l3）。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **HTTP 服务器线程模型**（u7-l1）：客户端线程解析请求、graph worker 串行推理。本讲的流式事件，是 graph worker 在「每生成一个 token」之后，沿着同一条已经发出了 SSE 头的 socket 把增量写回去。所以「流式」本质上是 worker 主循环里的副作用。
- **端点与 `request` 中间表示**（u7-l2）：无论客户端用哪种 API 方言，解析后都产出同一个 `request`，其中 `api_style`（`API_OPENAI`/`API_ANTHROPIC`/`API_RESPONSES`）和 `req_kind`（`REQ_CHAT`/`REQ_COMPLETION`）两个正交标签决定走哪条流式分支。
- **thinking 模式与分词**（u3-l3）：开启 thinking 时，聊天模板会把提示以 `<think>` 结尾，于是模型生成从「已经在推理块内部」开始，直到产出 `</think>` 才切换到正文。这是为什么流式状态机要从 `THINKING` 态起步。
- **采样与 thinking 的固定参数**（u4-l3）：thinking 模式下服务器固定 `temperature=1/top_p=1/min_p=0.05`、忽略客户端旋钮，以对齐 DeepSeek 官方的 fixed-thinking 行为。流式只是「把同样的 token 序列边生成边吐」，不改采样。

先澄清一个总容易被忽略的事实：**ds4 的「流式」与「非流式」跑的是同一条生成主循环**，区别只在每生成一个 token 后要不要立刻把这段文本写成 SSE 事件发给客户端。非流式是「攒完一次性发 JSON」，流式是「边攒边发增量」。所以本讲你会反复看到同一份 `text` 缓冲区被两类代码读取。

一个最朴素的 SSE 回顾（RFC 无关，只讲本仓库实际用法）：

- 响应头是 `Content-Type: text/event-stream`。
- 每个事件由若干行组成，最常见的是 `data: <一行 JSON>\n\n`（两个换行结束一个事件）。
- 还可以带 `event: <名字>` 行，Anthropic 端点大量使用命名事件（`message_start`、`content_block_delta`…）。
- 以 `:` 开头的行是注释（心跳），客户端应忽略。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ds4_server.c` | 流式全部实现：SSE 头/事件/usage 工具函数、三种方言各自的「流式状态机」、生成主循环里的流式分派与收尾。 |
| `README.md` | 「SSE streaming」段落（约 753–764 行）与端点参数清单（含 `stream_options.include_usage`）是协议行为的权威说明。 |

`ds4_server.c` 内本讲涉及的关键锚点（行号基于当前 HEAD `80ebbc3`）：

| 锚点 | 行号 | 作用 |
| --- | --- | --- |
| `request` 的流式字段 | 613–623 | `stream`、`stream_include_usage`、`reasoning_summary_emit` |
| `utf8_stream_safe_len` | 1008–1035 | 在 UTF-8 字符边界处扣住不完整的尾字节 |
| `parse_stream_options` | 1037–1068 | 解析 `stream_options.include_usage` |
| `sse_headers` / `sse_error_event` | 4881–4909 | 发 SSE 响应头 / 发错误事件 |
| `sse_chunk` | 4911–4940 | OpenAI 风格增量 chunk（chat/text_completion 两种 object） |
| `append_openai_usage_json` / `sse_usage_chunk` / `sse_done` | 4948–4992 | 使用量 JSON、可选 usage chunk、`[DONE]` |
| `sse_chat_finish` | 4994–5032 | 非实时回放路径的一次性收尾 |
| OpenAI Chat 流式状态机 | 5034–5967 | `openai_stream_mode`、`openai_sse_stream_update`、`openai_sse_finish_live` |
| `request_uses_*_stream` | 5969–5981 | 三类「结构化流」的判定 |
| Responses 流式状态机 | 6005–6716 | `responses_stream`、`*_sse_*` 事件构造、`responses_sse_stream_update`、`finish_live` |
| `sse_event` | 6924–6934 | Anthropic 命名事件通用 helper |
| Anthropic 流式状态机 | 6936–7601 | `anthropic_stream`、`open_block`/`delta_live`/`close_block_live`、`stream_update`、`stop_live`、`finish_live` |
| prefill 心跳回调 | 9626–9655 | prefill 期间先发 SSE 头、每 5s 发 `: prefill` 注释保活 |
| 流式初始化（worker） | 10293–10353 | 在 decode 前发头、发 message_start / role chunk / response.created |
| 生成循环里的流式分派 | 10477–10519 | 每个 token 后按 api 分派到三套 `*_sse_stream_update` |
| 流式收尾分派 | 10909–10942 | 按 api 分派到三套 `*_sse_finish_live` |

## 4. 核心概念与源码讲解

本讲按规格拆成三个最小模块：**SSE 事件生命周期**、**thinking 流式形态**、**include_usage**。其中 thinking 形态是穿插在三种生命周期里的「同一件事的三种方言翻译」，所以 4.2 会反复回引 4.1 里建立的函数。

### 4.1 SSE 事件生命周期

#### 4.1.1 概念说明

「生命周期」要回答的问题是：**从客户端发来一个 `stream:true` 的请求，到连接关闭，这条 socket 上先后会出现哪些 SSE 事件、它们的相对顺序是什么、谁是「开场」谁是「收尾」。**

ds4-server 一共维护着**两套半**流式实现，对应不同的 API 方言：

1. **OpenAI Chat 实时流**（`request_uses_openai_live_stream`）：`/v1/chat/completions` 且 `stream:true`。事件是连续的 `data: {…chat.completion.chunk…}`，没有命名事件，靠 `delta` 字段区分 reasoning / content / tool_calls。
2. **Responses 实时流**（`request_uses_responses_live_stream`）：`/v1/responses` 且 `stream:true`。事件全部带 `{"type":"response.xxx"}`，是 Codex CLI 期望的事件生命周期，有明确的 created/进行中/done/terminal 多层结构。
3. **Anthropic 实时流**：`/v1/messages` 且 `stream:true`。大量使用 `event:` 命名事件（`message_start`、`content_block_start`、`content_block_delta`、`content_block_stop`、`message_delta`、`message_stop`）。

剩下的「传统补全」`/v1/completions` 以及**没有走实时路径的 chat**，落到最朴素的 `sse_chunk`（按 token 增量直接吐 `text`），以及一个回放式收尾 `sse_chat_finish`。判定逻辑集中在这里：

```c
static bool request_uses_openai_live_stream(const request *r) {
    return r->stream && r->api == API_OPENAI && r->kind == REQ_CHAT;
}
static bool request_uses_responses_live_stream(const request *r) {
    return r->stream && r->api == API_RESPONSES && r->kind == REQ_CHAT;
}
static bool request_uses_structured_stream(const request *r) {
    return r->stream && (r->api == API_ANTHROPIC ||
                         r->api == API_RESPONSES ||
                         request_uses_openai_live_stream(r));
}
```

这三段在 [ds4_server.c:5969-5981](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L5969-L5981)。「结构化流」（`structured_stream`）这个词是相对「朴素增量流」而言的：朴素流只发文本片段，结构化流要把同一段原始文本**翻译**成该 API 的 reasoning/text/tool 三类语义事件。传统补全 `/v1/completions` 不在结构化流里，因为它压根没有 reasoning / tool 概念。

#### 4.1.2 核心流程

三类流式的生命周期都可以抽象成同一个三段式骨架：

```
1. 开场（head）：发 SSE 响应头，发一个「会话开始」事件
   - OpenAI Chat: 头 + 一个空 delta（带 role:assistant）
   - Responses  : 头 + response.created
   - Anthropic  : 头 + message_start（带 usage）
2. 中间（body）：decode 循环里，每个 token 后按状态机发增量事件
3. 收尾（tail）：发「结束」事件 + 使用量 + 关闭
```

**开场**集中在 worker 主循环 `generate_job` 的开头（[ds4_server.c:10300-10353](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10300-L10353)）。它先发 SSE 头，再按方言发各自的「开场事件」：

```c
if (!progress.headers_sent && !sse_headers(j->fd, s->enable_cors)) { … return; }
progress.headers_sent = true;
if (j->req.api == API_ANTHROPIC &&
    !anthropic_sse_start_live(j->fd, &j->req, id, prompt_tokens, &anthropic_live)) { … }
if (j->req.api == API_OPENAI && j->req.kind == REQ_CHAT &&
    !sse_chunk(j->fd, &j->req, id, NULL, NULL)) { … }   // role:assistant chunk
if (openai_live_chat) openai_stream_start(&j->req, &openai_live);
if (responses_live_chat) {
    responses_stream_init(&j->req, &responses_live);
    responses_live.active = true;
    responses_sse_created(j->fd, &j->req, &responses_live, responses_created_at);
}
```

SSE 头本身极简，注意 `Connection: close`——ds4 的流式连接是一次性的，发完即关，不做长连接复用（[ds4_server.c:4881-4892](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L4881-L4892)）：

```c
"HTTP/1.1 200 OK\r\n"
"Content-Type: text/event-stream\r\n"
"Cache-Control: no-cache\r\n"
…
"Connection: close\r\n\r\n"
```

> **细节：prefill 期间就发头。** 上面这段代码里有个 `progress.headers_sent` 的判断，是因为**长 prefill** 可能持续几十秒，HTTP 客户端若迟迟收不到响应头会超时断开。所以 ds4 在 prefill 进度回调里就提前把 SSE 头发出去了，并每隔约 5 秒发一行 `: prefill\n\n` 注释做心跳（SSE 客户端会忽略以 `:` 开头的行）。这段逻辑在 [ds4_server.c:9626-9655](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9626-L9655)，注释写得很直白：「Keep the HTTP/SSE connection alive while prefill runs」。这也是为什么 decode 前的代码要判 `if (!progress.headers_sent)`：prefill 可能已经发过头了，不能重复发。

**中间**是生成主循环里、每个 token 之后的流式分派（[ds4_server.c:10477-10519](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10477-L10519)）。核心是四个互斥的 if：

```c
if (j->req.stream && !structured_stream && stream_len > plain_stream_pos) {
    … sse_chunk(j->fd, &j->req, id, delta, NULL);          // 朴素增量
}
if (j->req.stream && j->req.api == API_ANTHROPIC &&
    !anthropic_sse_stream_update(j->fd, s, &j->req, id, &anthropic_live, text.ptr, stream_len, false)) { … }
if (openai_live_chat &&
    !openai_sse_stream_update(j->fd, s, &j->req, id, &openai_live, text.ptr, stream_len, false)) { … }
if (responses_live_chat &&
    !responses_sse_stream_update(j->fd, &j->req, &responses_live, text.ptr, stream_len, false)) { … }
```

几个要点：

- 传给状态机的不是「本次新 token」，而是**整段已生成文本 `text.ptr` 和「现在可以安全发到的长度 `stream_len`」**。状态机内部用一个游标 `emit_pos` 记录「上次发到哪里」，本次只发 `[emit_pos, stream_len)` 这一段。这样状态机可以自由地「扣住」末尾（例如半个 `</think>` 标签、或半个多字节字符）不发出。
- `stream_len` 已经被 `utf8_stream_safe_len` 和 `stop_list_stream_safe_len` 处理过，保证不会在 UTF-8 字符中间切断（见 4.1.4）。
- 四个 if 之所以是「互斥」的，是因为一个请求只属于一种 API 方言，`structured_stream` 已经把朴素流和结构化流分开。

**收尾**在生成循环结束后（[ds4_server.c:10909-10942](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10909-L10942)），按 api 分派到各自的 `*_finish_live`：

```c
if (j->req.stream) {
    if (j->req.api == API_ANTHROPIC)        response_ok = anthropic_sse_finish_live(…);
    else if (openai_live_chat)              response_ok = openai_sse_finish_live(…);
    else if (responses_live_chat)           response_ok = responses_sse_finish_live(…);
    else if (structured_stream)             response_ok = sse_chat_finish(…);     // 回放式
    else                                    response_ok = sse_chunk(…) && sse_done(…); // 朴素补全
}
```

每种 `*_finish_live` 都会做同一件事：以 `final=true` 再调一次 `*_stream_update` 把状态机里「扣住没发」的尾巴冲干净，然后发该方言的「终止事件」和 usage。

#### 4.1.3 源码精读：朴素增量与通用工具函数

先看最基础的 `sse_chunk`，它是朴素流和 OpenAI Chat 开场 role chunk 的共同底座（[ds4_server.c:4911-4940](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L4911-L4940)）。它按 `req_kind` 产两种 `object`：

```c
if (r->kind == REQ_CHAT) {
    … "object":"chat.completion.chunk" …
    "delta": text ? {"content": text} : (finish ? {} : {"role":"assistant"})
    "finish_reason": finish ? finish : null
} else {
    … "object":"text_completion" …
    "choices":[{"text": text ? text : "", "finish_reason": …}]
}
```

注意那个三元嵌套：`text==NULL` 时，若有 `finish` 就发空 `{}`（收尾帧），否则发 `{"role":"assistant"}`（开场帧）。这就是 OpenAI Chat 流式的「第一帧带 role、中间帧带 content、末帧带 finish_reason」约定，用同一个函数复用。

收尾的尽头是 `sse_done`（[ds4_server.c:4988-4992](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L4988-L4992)），它先发（可选的）usage chunk，再发标志结束的 `data: [DONE]`：

```c
static bool sse_done(int fd, const request *r, const char *id,
                     int prompt_tokens, int completion_tokens) {
    return sse_usage_chunk(fd, r, id, prompt_tokens, completion_tokens) &&
           send_all(fd, "data: [DONE]\n\n", 14);
}
```

Anthropic 的命名事件则用通用 helper `sse_event`（[ds4_server.c:6924-6934](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L6924-L6934)），把 `event:` 行和 `data:` 行拼成一个事件：

```c
event: message_start
data: {"type":"message_start", …}

```

Responses 端点没有 `event:` 行，但它的事件体里都带 `"type":"response.xxx"`，并且 `responses_sse_emit_event` 会往每个事件体里**自动注入一个 `sequence_number`** 字段（[ds4_server.c:6056-6087](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L6056-L6087)）。注释解释这是「Codex parses an explicit sequence_number on every Responses event for ordering and reconnect resilience」——也就是给客户端一个单调递增的序号，方便断线重连时定位位置。注入手法很轻量：识别事件体开头的 `{"type":"…"}`，在 type 字符串的闭引号后插一段 `,"sequence_number":N`。

#### 4.1.4 一个贯穿全局的细节：UTF-8 边界与「扣住」

分词器可能把一个多字节 UTF-8 字符切成两个 token。如果 SSE 增量正好切在这个字符中间，某些客户端会把不完整的字节序列替换成 U+FFFD，再把这段被污染的文本作为「助手历史」发回来——这会**摧毁下一轮的 KV 前缀复用**（u7-l5）。`utf8_stream_safe_len` 就是用来防这个的（[ds4_server.c:1008-1035](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L1008-L1035)）：

```c
/* … Hold only the trailing incomplete character; the next
 * generated token will complete it. */
static size_t utf8_stream_safe_len(const char *s, size_t start, size_t limit, bool final) {
    if (final || !s || limit <= start) return limit;   // 收尾时不扣，一次性冲完
    …从 limit 往回数连续的 0x80 起首字节（UTF-8 续字节）…
    …若末尾是某个多字节字符的不完整前缀，就把 limit 退回到该字符首字节之前…
}
```

它的契约很清晰：**非 final 调用时，宁可少发一个字符，也绝不在多字节字符中间切断；final 调用（收尾冲刷）时直接返回 `limit` 把剩下的全发掉。** 所有三套状态机在计算「这次发到哪」时都会调它。这个函数是「流式不影响正确性」这条底线的一部分——它保证客户端累积回放的文本和服务器内部的 token 序列逐字节一致。

#### 4.1.5 代码实践

**实践目标**：用「源码阅读 + 本地抓包」两种方式，确认三类结构化流的「开场第一帧」分别长什么样。

**操作步骤**：

1. 在 [ds4_server.c:10300-10353](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10300-L10353) 找到流式初始化段，对照三类 `if`，写出 OpenAI Chat、Responses、Anthropic 各自的「开场事件」由哪个函数发出、发出的 JSON 顶层 `type`/`object` 字段是什么。
2.（可选，待本地验证）启动一个本地 ds4-server（需有可用 GGUF，详见 u1-l5/u7-l1），用 `curl -N` 抓三种端点的流式原始字节：
   ```sh
   curl -N http://127.0.0.1:8000/v1/chat/completions -H 'Content-Type: application/json' \
     -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"说一个字"}],"stream":true}'
   curl -N http://127.0.0.1:8000/v1/messages -H 'Content-Type: application/json' \
     -d '{"model":"deepseek-v4-flash","max_tokens":16,"messages":[{"role":"user","content":"说一个字"}],"stream":true}'
   curl -N http://127.0.0.1:8000/v1/responses -H 'Content-Type: application/json' \
     -d '{"model":"deepseek-v4-flash","input":"说一个字","stream":true}'
   ```

**需要观察的现象**：

- OpenAI Chat 的第一帧 `delta` 应为 `{"role":"assistant"}`，没有 `content`。
- Anthropic 的第一个事件应是 `event: message_start`，且 `data` 里的 `message.usage` 已带 `input_tokens`。
- Responses 的第一个事件应是 `data: {"type":"response.created", …}`，且每个事件体里都能找到 `sequence_number` 字段。

**预期结果**：你能在原始字节里直接看到这三类「开场」的差异，与源码分派一一对应。若本地没有模型可跑，仅完成步骤 1 的源码对照也算达成（明确标注「待本地验证」）。

#### 4.1.6 小练习与答案

**练习 1**：为什么 `generate_job` 在发 SSE 头之前要先判 `if (!progress.headers_sent)`？如果不判，最坏会发生什么？

**参考答案**：因为 prefill 进度回调可能在长 prefill 期间已经提前发过 SSE 头（并持续发心跳保活，见 [ds4_server.c:9626-9655](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9626-L9655)）。若不判而重复发头，第二个 `HTTP/1.1 200 OK\r\n…` 会被 SSE 客户端当成事件数据解析，协议立刻错乱。`progress.headers_sent` 是「头是否已发」的事实记录，decode 段据此决定补发还是跳过。

**练习 2**：`utf8_stream_safe_len` 的 `final` 参数什么时候为 true？为什么为 true 时就「不再扣住」？

**参考答案**：`final` 为 true 发生在收尾冲刷（各 `*_finish_live` 以 `final=true` 调一次 `*_stream_update`）。此时生成已经结束，不会再有「下一个 token 来补全这个字符」，所以必须把剩余字节一次发完，否则客户端永远收不到最后那半个字符。非 final 时才需要扣住，因为下个 token 大概率会补全它。

### 4.2 thinking 流式形态

#### 4.2.1 概念说明

DeepSeek V4 在 thinking 模式下，模型实际产出的 token 序列形如：

```
<think>
这里是链式推理过程……
</think>
这里是给用户看的最终回答。
```

`<think>…</think>` 这段是**推理内容（reasoning）**，`</think>` 之后是**正文（content）**。问题是：OpenAI Chat、Anthropic、Responses 三家 API 对「推理内容」各有各的「原生形态」，客户端 SDK 只认自家的形态。ds4-server 必须把**同一段 `<think>` 文本翻译成三种不同的 SSE 事件**，且翻译要逐 token 实时进行（边生成边发）。

这就引出本模块的核心抽象——**三套同构的流式状态机**。它们都长一个样：

```c
typedef enum { …_THINKING, …_TEXT, …_TOOL, …_SUPPRESS } …_stream_mode;
```

- `THINKING`：当前在 `<think>` 内部，发的增量是「推理」语义。
- `TEXT`：已过 `</think>`，发的增量是「正文」语义。
- `TOOL`：进入 DSML 工具调用块，发的增量是「工具调用」语义（u7-l4 详讲）。
- `SUPPRESS`：静默——后面这段不发给客户端（例如已被工具投影接管，或要等收尾一次性发）。

初始模式由是否开启 thinking 决定。以 OpenAI 为例（[ds4_server.c:5080-5084](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L5080-L5084)）：

```c
static void openai_stream_start(const request *r, openai_stream *st) {
    memset(st, 0, sizeof(*st));
    st->active = true;
    st->mode = ds4_think_mode_enabled(r->think_mode) ? OPENAI_STREAM_THINKING : OPENAI_STREAM_TEXT;
}
```

Anthropic（[ds4_server.c:6999](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L6999)）和 Responses（[ds4_server.c:6038](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L6038)）是同样的判定：开了 thinking 就从 `THINKING` 起，否则直接 `TEXT`。

#### 4.2.2 核心流程

THINKING 态的迁移逻辑，三套状态机几乎是逐行复制的。以 OpenAI 版（[ds4_server.c:5865-5907](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L5865-L5907)）为例，伪代码如下：

```
state THINKING:
  # 1) 处理可能重复的 <think> 前缀（只判一次）
  if 未检查过前缀:
      if 文本目前只是 "<think>" 的前缀且非 final: 直接返回（等下个 token 再判）
      if 文本以 "<think>" 开头: 把 emit_pos 跳过这 7 个字节
      标记已检查

  # 2) 在剩余文本里找 "</think>"
  if 找到 </think>:  发到闭合标签前为止
  elif final:        发到末尾为止（推理被截断）
  else:              扣住末尾 7 字节不发（可能是半个 </think>），等下个 token

  # 3) 把 [emit_pos, limit) 这段当 reasoning 发出去
  if limit > emit_pos: 发一个 reasoning 增量

  # 4) 状态迁移
  if 找到 </think>:  emit_pos 跳过闭合标签，切到 TEXT 态
  elif final:        切到 SUPPRESS（推理没收尾，直接结束）
  else:              保持 THINKING，返回等下一 token
```

三个关键设计：

1. **`checked_think_prefix` 只判一次**：因为聊天模板已经把提示以 `<think>` 结尾（u3-l3），模型生成时通常**已经在推理块内部**，第一段 token 本来就是推理内容、不会带 `<think>` 开头。这个判断只是兜底「万一模型重复输出了 `<think>`，就把它跳过」。Responses 版本的注释把这个坑讲得很清楚（[ds4_server.c:6554-6563](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L6554-L6563)）：早期版本曾用「没看到 `<think>` 前缀就直接切 TEXT」的捷径，结果把推理内容当成正文漏给了客户端。
2. **扣住末尾 7 字节**：`hold = strlen("</think>") - 1 = 7`。在非 final 情况下，文本末尾可能是 `"</thin"`、`"</think"` 这样半个闭合标签，如果当成 reasoning 发出去，客户端就会看到错误的 `</thin` 文本。扣住 7 字节，等下个 token 到了再判断是不是真的闭合标签。
3. **`final` 时推理没收尾 = 截断**：如果生成因为 EOS / max_tokens / stop / error 在 `</think>` **之前**就停了，状态机切到 `SUPPRESS` 而不是 TEXT——意思是「这段未闭合的推理不当作正文」。Responses/Anthropic 还会据此把 reasoning 项标成 `incomplete`（见 4.2.4）。

TEXT 态的逻辑类似：把 `</think>` 之后的字节当正文发，遇到 DSML 工具起始标记就切 TOOL（或 SUPPRESS）。

#### 4.2.3 源码精读：三种「推理」原生形态

同样是「发一段 reasoning 增量」，三种 API 发出的事件天差地别。我们逐个对照源码。

**OpenAI Chat：`reasoning_content` 字段。** OpenAI 官方 Chat Completions 没有 reasoning 字段，这是 DeepSeek 扩展并被多家客户端接受的 `delta.reasoning_content`。ds4 用一个通用函数 `sse_chat_delta_n`，把字段名作为参数传入（[ds4_server.c:5891-5893](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L5891-L5893) 调用，函数定义在 [ds4_server.c:5136-5151](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L5136-L5151)）：

```c
// 推理增量：字段名 "reasoning_content"
sse_chat_delta_n(fd, r, id, "reasoning_content", raw + emit_pos, limit - emit_pos);
…
// 正文增量：同一个函数，字段名换成 "content"
sse_chat_delta_n(fd, r, id, "content", raw + emit_pos, limit - emit_pos);
```

所以 OpenAI Chat 流里，同一个 `chat.completion.chunk` 对象，靠 `delta` 里是 `reasoning_content` 还是 `content` 区分推理与正文。客户端累积时分别接到两个字段。

**Anthropic：`thinking` 内容块。** Anthropic 协议用「内容块（content block）」来表达一段同类型内容。推理是一个独立的 `thinking` 块，正文是 `text` 块，工具是 `tool_use` 块。每开一个块要发 `content_block_start`，每段增量发 `content_block_delta`，结束发 `content_block_stop`。推理块还有个特殊的 `signature_delta`（[ds4_server.c:7141-7168](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7141-L7168)）：

```c
// 开 thinking 块：content_block.type = "thinking"，带空 signature
content_block_start: {"index":N, "content_block":{"type":"thinking","thinking":"","signature":""}}
// 增量：delta.type = "thinking_delta"
content_block_delta: {"index":N, "delta":{"type":"thinking_delta","thinking":"…"}}
// 收尾 thinking 块：先发 signature_delta，再 content_block_stop
content_block_delta: {"index":N, "delta":{"type":"signature_delta","signature":<id>}}
content_block_stop : {"index":N}
```

`index` 是块序号，由 `next_index` 单调递增（[ds4_server.c:7163-7165](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7163-L7165)）。正文块用 `text_delta`（[ds4_server.c:7110-7115](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7110-L7115)）。所以 Anthropic 客户端看到的是「先来一个 thinking 块、再来一个 text 块」的结构化序列，而非混在一个字段里。

**Responses：`reasoning` 输出项 + summary。** Responses API 的「输出」是一组 item（reasoning item、message item、function_call item）。推理被表达成一个 `reasoning` 类型的 item，其 `summary` 是一组 `summary_text` part。事件生命周期更长（[ds4_server.c:6103-6205](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L6103-L6205)）：

```
response.output_item.added          → 通知「新增了一个 reasoning item」（status: in_progress）
response.reasoning_summary_part.added → 通知「这个 reasoning item 加了一个 summary part」
response.reasoning_summary_text.delta → 推理增量（可多次）
response.reasoning_summary_text.done  → summary part 完成
response.reasoning_summary_part.done  → part 关闭
response.output_item.done           → reasoning item 关闭
```

推理增量本身的事件体是 `{"type":"response.reasoning_summary_text.delta","delta":"…"}`（[ds4_server.c:6127-6140](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L6127-L6140)）。注意 Responses 的推理输出**默认不发给客户端**，只有客户端在请求里显式带了 `reasoning.summary` 才发（见 4.2.4）。

把三种形态横向对比：

| 维度 | OpenAI Chat | Anthropic | Responses |
| --- | --- | --- | --- |
| 推理承载 | `delta.reasoning_content` 字段 | 独立 `thinking` 内容块 | `reasoning` 输出项 + summary part |
| 推理增量事件 | `chat.completion.chunk`（同正文对象） | `content_block_delta` / `thinking_delta` | `response.reasoning_summary_text.delta` |
| 正文承载 | `delta.content` | 独立 `text` 内容块（`text_delta`） | `message` 输出项 / `response.output_text.delta` |
| 默认是否吐推理 | 是（DeepSeek 客户端期望） | 是（thinking 块） | **否**，需 `reasoning.summary` 显式开启 |

#### 4.2.4 源码精读：推理截断与「opt-in」

两个容易被忽略的工程约束。

**约束一：Responses 端点推理默认隐藏，需 opt-in。** 看 `responses_sse_stream_update` 开头（[ds4_server.c:6548-6551](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L6548-L6551)）：

```c
/* The client only sees reasoning if it explicitly opted in via
 * reasoning.summary. Otherwise we still need to walk past <think>...</think>
 * to find the user-visible text, but we suppress the per-chunk emission. */
const bool emit_reasoning = r->reasoning_summary_emit;
```

也就是说，即使客户端没要推理，状态机**仍然要走完 THINKING 态**——目的是把游标 `emit_pos` 推过 `<think>...</think>` 这段，否则会把推理文本当成正文吐出来。区别只在于「走到 `limit > emit_pos` 时，发不发 reasoning 事件」。`r->reasoning_summary_emit` 这个字段（[ds4_server.c:623](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L620-L623)）只在解析 Responses 的 `reasoning` 参数时被置位（[ds4_server.c:3832-3836](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L3832-L3836)）。OpenAI Chat 和 Anthropic 没有这个开关，开了 thinking 就直接吐推理。

**约束二：推理未闭合 = 标记 incomplete。** 如果生成在 `</think>` 之前就停了（EOS、达到 max_tokens、命中 stop、出错），那段推理是「残缺的」。直接把它当完整推理交还客户端很危险：客户端可能把这段半截推理作为「助手历史」发回来，等于把模型的未完成隐藏状态当成既定事实喂回去。`reasoning_closed_naturally` 这个标志记录「是否真的观测到了 `</think>`」（[ds4_server.c:6610](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L6610)），收尾时据此决定 item 状态（[ds4_server.c:6147-6156](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L6147-L6156)）：

```c
/* If the stream terminates before `</think>` was actually observed the
 * reasoning item is partial … Force the item to incomplete so a client
 * replay rejects it instead of feeding unfinished hidden state back as
 * completed history. */
const char *item_status =
    st->reasoning_closed_naturally ? "completed" : "incomplete";
```

终端的 `response.completed` 事件里也复用同一逻辑（[ds4_server.c:6492-6507](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L6492-L6507)），保证 item 级和 response 级一致。这是「流式翻译不能改变模型语义」这条原则的体现：状态机只决定「怎么把字节翻译成事件」，但翻译结果必须忠实反映「这段推理到底有没有写完」。

#### 4.2.5 代码实践

**实践目标**：对比 OpenAI Chat 与 Anthropic 在 thinking 模式下输出同一段推理时的**事件结构差异**——这是本讲规格里指定的实践任务。

**操作步骤**：

1. 在源码里定位两条「发推理增量」的代码路径：
   - OpenAI Chat：[ds4_server.c:5891-5893](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L5891-L5893)（`sse_chat_delta_n(…, "reasoning_content", …)`）。
   - Anthropic：开块 [ds4_server.c:7465](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7465)（`anthropic_sse_open_block(…, ANTH_BLOCK_THINKING)`）+ 增量 [ds4_server.c:7466-7468](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7466-L7468)（`anthropic_sse_delta_live(…, ANTH_BLOCK_THINKING, …)`）。
2. 假设模型生成的推理是 `仔细想想…`、正文是 `答案是 42。`，**手写**出两种 API 各自会发出的 SSE 事件序列（参照 4.2.3 的格式）。
3.（可选，待本地验证）用 `curl -N` 对本地 server 发两个 thinking 请求（Chat 与 Messages），抓原始字节，核对你手写的序列。

**需要观察的现象 / 预期结果**：

- OpenAI Chat：你应该写出两段 `data: {…chat.completion.chunk…}`，一段 `delta` 是 `{"reasoning_content":"仔细想想…"}`，另一段是 `{"content":"答案是 42。"}`，最后 `data: [DONE]`。
- Anthropic：你应该写出一条 `event: content_block_start`（thinking 块）、一条 `content_block_delta`（thinking_delta）、一条带 `signature_delta` 的 delta、一条 `content_block_stop`，再开一个 text 块重复 start/delta/stop，最后 `message_delta` + `message_stop`。
- 结论一句话：**Chat 把推理和正文塞进同一个 chunk 对象的不同字段；Anthropic 把它们拆成两个独立的内容块，各有完整的 start/delta/stop 生命周期。**

#### 4.2.6 小练习与答案

**练习 1**：为什么三套状态机在 THINKING 态都要「扣住末尾 7 字节」（`hold = strlen("</think>") - 1`）？这个 7 是怎么来的？

**参考答案**：闭合标签 `</think>` 长 8 字节。在非 final 的增量里，文本末尾可能是这个标签的不完整前缀（如 `</thin`，7 字节）。如果把这种前缀当成推理内容发出去，客户端会看到错误的 `</thin` 字样。扣住 `8-1=7` 字节，意味着只要末尾不足 8 字节就先不发，等下一个 token 到了，要么补成完整 `</think>`（识别为闭合、切 TEXT），要么确认不是闭合标签（放开这些字节当推理发）。`final` 时不再扣，因为没有「下一个 token」了。

**练习 2**： Responses 端点里，如果客户端没带 `reasoning.summary`，`responses_sse_stream_update` 还会进入 THINKING 态吗？为什么？

**参考答案**：会进入。因为模型生成的 token 序列在 thinking 模式下仍然以 `<think>` 开头，状态机**必须**走 THINKING 态把游标推过 `</think>`，才能找到正文起点；否则推理字节会被当成正文。区别只在于 `emit_reasoning = r->reasoning_summary_emit` 为 false 时，走到「该发 reasoning 增量」的分支会跳过实际的事件发送（[ds4_server.c:6588-6603](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L6588-L6603)），但 `emit_pos` 照常推进。即「走过去，但不说话」。

### 4.3 include_usage

#### 4.3.1 概念说明

`stream_options.include_usage` 是 OpenAI Chat Completions / Completions API 的一个开关：默认情况下，流式响应**只在每个 chunk 里带文本增量、不带 token 使用量**；客户端若想在流结束时拿到 `prompt_tokens` / `completion_tokens`，就得在请求里加：

```json
{ "stream": true, "stream_options": { "include_usage": true } }
```

ds4 把这个开关解析后存进 `request.stream_include_usage`（[ds4_server.c:614](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L613-L614)）。注意这是 **OpenAI 风格专属**的概念——Anthropic 的使用量随 `message_start`（input）和 `message_delta`（output）自然流出，Responses 的使用量随终端 `response.completed` 流出，都不需要这个开关。README 也只在 chat/completions 的参数清单里列了它（[README.md:733](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L733)），并在 Codex 风格的模型能力声明里写了 `supportsUsageInStreaming: true`（[README.md:887](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L887)）。

#### 4.3.2 核心流程

`include_usage` 的完整数据流是：

```
请求 JSON: "stream_options":{"include_usage":true}
   ──parse_stream_options──▶ request.stream_include_usage = true
   ──sse_done──▶ sse_usage_chunk(若开关开)──▶ append_openai_usage_json
   ──▶ 一个 choices:[] + usage:{…} 的 chunk
   ──▶ data: [DONE]
```

关键点：usage chunk 是**整个流的倒数第二个事件**，紧跟在最后一个带 `finish_reason` 的 chunk 之后、`[DONE]` 之前。它的 `choices` 是空数组 `[]`——这是 OpenAI 的约定：usage chunk 不携带文本增量，只携带使用量。客户端据此知道「生成已结束，这是统计帧」。

#### 4.3.3 源码精读

**解析**：`parse_stream_options` 是个标准的手写 JSON 对象遍历器（[ds4_server.c:1037-1068](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L1037-L1068)）。它只认 `include_usage` 这一个 key，其它 key 用 `json_skip_value` 跳过（和 u7-l2 讲过的「未知字段静默跳过」原则一致）：

```c
if (!strcmp(key, "include_usage")) {
    if (!json_bool(p, include_usage)) { … return false; }
} else if (!json_skip_value(p)) { … }
```

它被 chat 解析和 completion 解析各自调用一次（[ds4_server.c:2738-2739](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L2738-L2739) 与 [ds4_server.c:4069-4070](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L4069-L4070)），都把结果写进 `r->stream_include_usage`。

**发送**：`sse_usage_chunk` 第一行就是这个开关的守卫（[ds4_server.c:4965-4986](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L4965-L4986)）：

```c
static bool sse_usage_chunk(int fd, const request *r, const char *id,
                            int prompt_tokens, int completion_tokens) {
    if (!r->stream_include_usage) return true;   // 开关没开，直接跳过
    …
    ",\"choices\":[],\"usage\":"           // 空 choices + usage
    append_openai_usage_json(&b, r, prompt_tokens, completion_tokens);
}
```

`return true` 表示「什么都没发，但不算失败」——这样调用方 `sse_done` 可以无条件调用它，由它自己决定发不发。

**使用量 JSON 的内容**：`append_openai_usage_json`（[ds4_server.c:4948-4963](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L4948-L4963)）发的不只是 OpenAI 标准的三件套，还有 ds4 的扩展：

```json
{
  "prompt_tokens": N,
  "completion_tokens": M,
  "total_tokens": N+M,
  "prompt_tokens_details": {
    "cached_tokens": K,        // 命中磁盘 KV 缓存的 prompt token 数
    "cache_write_tokens": W    // 本次新写入缓存的 token 数（DS4 扩展）
  }
}
```

注释特别强调（[ds4_server.c:4954-4957](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L4954-L4957)）：`cached_tokens` 严格按 OpenAI 语义（从缓存读取的 prompt token），而「本次新 prefill 的 token」是 DS4 扩展，必须放在单独的 `cache_write_tokens` 字段里，不能混进 `cached_tokens`，否则兼容客户端会把缓存命中数算重。`clamp_usage_tokens` 还做了钳位，保证这两个数不超过 `prompt_tokens`，避免统计溢出误导客户端。这两个字段把 u7-l1 讲过的「单活 KV session + 磁盘快照」带来的缓存命中信息，透传给了 API 调用方。

#### 4.3.4 代码实践

**实践目标**：观察 `include_usage` 开与关时，流末尾事件序列的差异。

**操作步骤**：

1. 读 [ds4_server.c:4988-4992](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L4988-L4992) 的 `sse_done`，确认它的发送顺序是「usage chunk（可选）→ `[DONE]`」。
2.（可选，待本地验证）对本地 server 发两个对比请求，唯一区别是 `stream_options`：
   ```sh
   # 不带 include_usage
   curl -N http://127.0.0.1:8000/v1/chat/completions … -d '{"…","stream":true}'
   # 带 include_usage
   curl -N http://127.0.0.1:8000/v1/chat/completions … -d '{"…","stream":true,"stream_options":{"include_usage":true}}'
   ```

**需要观察的现象 / 预期结果**：

- 不带时：流末尾是 `…finish_reason:"stop"…` 的 chunk，紧接着 `data: [DONE]`，**中间没有 usage chunk**。
- 带时：`finish_reason` chunk 之后、`[DONE]` 之前，**多出一个 `choices:[]` 且带 `usage` 的 chunk**，其中 `prompt_tokens_details.cached_tokens` 在第二次发同样 prompt 时会变成非零（命中了磁盘 KV）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 usage chunk 的 `choices` 是空数组 `[]`，而不是带一个空 delta 的 choice？

**参考答案**：因为 usage chunk 的语义是「统计帧，不携带任何文本增量」。OpenAI 协议约定此时 `choices` 为空数组，客户端据此区分「这是使用量统计」与「这是一个 (即便 delta 为空的) 文本帧」。如果塞一个空 delta 的 choice，有的客户端会把它当成一次空的文本增量去累积，可能触发空字符串回调。

**练习 2**：Anthropic 流式需要 `include_usage` 吗？它的使用量从哪里来？

**参考答案**：不需要，`stream_options.include_usage` 是 OpenAI 风格专属，Anthropic 解析路径根本不读它。Anthropic 的使用量天然分两处流出：输入 token 数随开场的 `message_start` 事件里的 `message.usage` 发出（[ds4_server.c:6991](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L6991)），输出 token 数随收尾的 `message_delta` 事件里的 `usage.output_tokens` 发出（[ds4_server.c:7578-7581](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7578-L7581)）。所以 Anthropic 客户端总能拿到使用量，无需额外开关。

## 5. 综合实践

把三个模块串起来：**手动追踪一个「thinking 模式 + 带工具 + 流式 + include_usage」的 OpenAI Chat 请求，画出它从请求到 `[DONE]` 的完整事件时间线。**

具体步骤：

1. **请求侧**：写出一个合理的 `/v1/chat/completions` 请求 JSON，包含 `stream:true`、`stream_options:{include_usage:true}`、`tools:[…]`、并隐式开启 thinking（用模型别名 `deepseek-reasoner`，见 u7-l2）。
2. **生命周期**：参照 [ds4_server.c:10300-10353](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10300-L10353)，写出 worker 在 decode 前发出的开场事件（SSE 头 + role chunk）。
3. **THINKING 段**：假设模型先输出一段推理再调用工具，参照 [ds4_server.c:5865-5907](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L5865-L5907)，写出若干个 `reasoning_content` 增量 chunk，并标注状态机何时因为观测到 `</think>` 而从 THINKING 切到 TEXT。
4. **TEXT/TOOL 段**：参照 [ds4_server.c:5909-5937](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L5909-L5937)，写出正文增量 chunk，以及当检测到 DSML 工具起始标记时切到 TOOL 态、改发 `tool_calls` 增量（工具调用的 DSML 细节留待 u7-l4，这里只需标注「状态切换点」）。
5. **收尾**：参照 [ds4_server.c:5941-5967](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L5941-L5967) 与 [ds4_server.c:4988-4992](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L4988-L4992)，写出末尾的 `finish_reason:"tool_calls"` chunk、usage chunk（带 `cached_tokens` / `cache_write_tokens`）、`[DONE]`。

**验收标准**：你能指着时间线上每一个事件，说出它由哪个函数、在哪一行发出、为什么这个时间点发。如果某一处你只能写出「待确认」，就老实标注，不要编造。本实践无需真实模型即可完成（纯源码追踪），若想在本地 server 上核对，明确标注为「待本地验证」。

## 6. 本讲小结

- ds4-server 维护**两套半**流式实现：OpenAI Chat 实时流（连续 `chat.completion.chunk`）、Responses 实时流（带 `type` 与 `sequence_number` 的生命周期事件）、Anthropic 实时流（`event:` 命名事件 + content block 生命周期）；传统补全与未走实时的 chat 退化为朴素 `sse_chunk` 增量。判定集中在 `request_uses_*_stream`。
- 三类结构化流共享同构的 **`THINKING/TEXT/TOOL/SUPPRESS` 状态机**，靠一个 `emit_pos` 游标在「整段已生成文本」上推进，本次只发 `[emit_pos, safe_limit)`，从而能扣住半个 `</think>` 或半个多字节字符不发。
- **thinking 流式形态**是把同一段 `<think>` 文本翻译成三种「原生 API 形态」：OpenAI Chat 的 `reasoning_content` 字段、Anthropic 的 `thinking` 内容块（带 `signature_delta`）、Responses 的 `reasoning` 输出项 + summary part；Responses 默认隐藏推理、需 `reasoning.summary` opt-in，且推理未闭合时标 `incomplete` 防止脏历史回灌。
- `stream_options.include_usage` 是 **OpenAI 风格专属**开关，由 `parse_stream_options` 解析、`sse_usage_chunk` 在 `[DONE]` 前发一个 `choices:[]` 的统计帧，其中 `cached_tokens`/`cache_write_tokens` 把磁盘 KV 命中信息透传给客户端；Anthropic/Responses 各有自带的使用量出口，不需要此开关。
- 两条贯穿全局的工程底线：prefill 期间提前发 SSE 头并每 5s 发 `: prefill` 心跳防客户端超时；`utf8_stream_safe_len` 在非 final 时绝不切断多字节字符，保证客户端回放文本与服务器 token 序列逐字节一致，进而保住下一轮 KV 前缀复用。

## 7. 下一步学习建议

本讲讲清了「事件怎么发」，但刻意回避了两件事：

1. **工具调用的 DSML 投影**：4.2 里 TEXT→TOOL 的状态切换只点了到为止。工具块的 DSML 字节如何被实时解析成 `tool_calls` 增量、`tool_use` 块、function_call item，是 u7-l4（工具调用：DSML、精确回放、规范化）的主题。建议紧接着读 u7-l4，重点看 `openai_tool_stream_update` / `anthropic_tool_stream_update` 与本讲 `*_stream_update` 的 TOOL 分支如何衔接。
2. **前缀复用与回放**：本讲反复强调「流式不能改模型语义、客户端回放文本必须逐字节一致」，但「为什么一致这么重要」「不一致会怎样」属于 u7-l5（实时 KV 前缀复用与检查点改写）。读完 u7-l5 你会理解 `utf8_stream_safe_len` 和 `reasoning_closed_naturally` 这些「底线」其实是在保护 KV checkpoint。

若你想从源码层面再加深本讲，推荐带着这两个问题回读 `generate_job`（[ds4_server.c:9991](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9991) 起）：一是 `stream_len` 这个「安全长度」从 stop list、UTF-8、工具标记三道关卡是怎么一路算出来的；二是同一个 `text` 缓冲区如何同时被朴素流、三套状态机、stop 检测、DSML 跟踪五处读取而互不干扰——这是 ds4-server 把「流式」作为生成主循环副作用的工程集中体现。

# OpenAIUser 流式与非流式响应解析

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `OpenAIUser` 如何为 chat / embeddings / rerank / images / speech 五类任务**构造请求**并选择**不同的 endpoint**。
- 理解统一发送入口 `send_request` 如何用一个 `parse_strategy` 回调同时驾驭**流式**和**非流式**两种响应。
- 逐行读懂 `parse_chat_response` 对 **SSE（Server-Sent Events）流式 chunk** 的解析，尤其是 **TTFT（首 token 时间）** 与 **token 计数** 是怎么提取出来的。
- 掌握当服务端**没有返回 usage 信息**时，如何用 **tokenizer 回退估算** token 数。
- 看懂 reasoning 模型（如 gpt-oss、DeepSeek-R1 类）流式输出中 `reasoning` / `reasoning_content` 字段的处理。

## 2. 前置知识

本讲承接 [u3-l1](u3-l1-base-user-and-locust.md)，默认你已经知道：

- `BaseUser` 继承自 Locust 的 `HttpUser`，`sample()` 从 `environment.sampler` 取到一个 `UserRequest`，最后调用 `collect_metrics()` 把指标上报。
- 一次请求的生命周期是：**取请求 → 发请求 → 解析为 `UserResponse` → 上报指标**。本讲聚焦中间两步（发请求 + 解析）。

还需要两个基础概念：

- **流式（streaming）响应**：LLM 生成是一个 token 一个 token 吐出来的。服务端不会等全部生成完再返回，而是用一个长连接，边生成边把已经算出来的 token 推给客户端。客户端要边读边算指标。OpenAI 兼容服务用 **SSE** 协议：每条消息是一行 `data: {JSON}\n\n`，最后用一行 `data: [DONE]` 结束。
- **非流式（non-streaming）响应**：服务端算完全部结果后，一次性返回一个完整 JSON。embeddings / rerank / 文生图这类任务没有“逐 token”的概念，走非流式。

> 为什么 chat 必须用流式？因为基准测试要测 **TTFT（Time To First Token）**——模型“多久才开口说第一个字”。这只有边读边计时才测得到。两个核心时间量的定义：

\[
\text{TTFT} = t_{\text{first\_token}} - t_{\text{start}}
\]

\[
\text{e2e\_latency} = t_{\text{end}} - t_{\text{start}}
\]

`parse_chat_response` 的全部工作，就是把上面公式里需要的三个时间戳（\(t_{\text{start}}\)、\(t_{\text{first\_token}}\)、\(t_{\text{end}}\)）和 token 数准确地从字节流里抠出来。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [genai_bench/user/openai_user.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py) | 本讲主角。定义 `OpenAIUser`，包含五类任务的请求构造、统一发送 `send_request`、以及四个 `parse_*_response` 解析器。 |
| [genai_bench/protocol.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py) | 数据契约。`UserChatResponse` / `UserResponse` 等响应模型，是解析器的产出物。 |
| [tests/user/test_openai_user.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_openai_user.py) | 真实测试。提供了大量“模拟 SSE 字节流”的样本，是理解解析逻辑的最佳素材。 |

---

## 4. 核心概念与源码讲解

### 4.1 请求构造与 endpoint

#### 4.1.1 概念说明

`OpenAIUser` 是所有“OpenAI 兼容协议”后端的实现类。它用一个类级字典 `supported_tasks` 声明自己能干哪些活，每个任务对应一个方法名，每个方法又对应一个固定的 HTTP endpoint：

| 任务字符串 | 方法名 | endpoint | 流式？ |
| --- | --- | --- | --- |
| `text-to-text` / `image-text-to-text` | `chat` | `/v1/chat/completions` | 是 |
| `text-to-embeddings` | `embeddings` | `/v1/embeddings` | 否 |
| `text-to-rerank` | `rerank` | `/v1/rerank` | 否 |
| `text-to-image` | `images_generations` | `/v1/images/generations` | 否 |
| `text-to-speech` | `speech` | `/v1/audio/speech` | 是（字节流） |

关键设计：每个 `@task` 方法只做两件事——**把 `UserRequest` 翻译成 HTTP payload**，然后**委托给统一的 `send_request`** 去发请求。真正“怎么发、怎么解析”的复杂逻辑全部收口在 `send_request` 里，任务方法本身保持轻薄。

#### 4.1.2 核心流程

一个任务方法的执行流程：

```
1. endpoint = "/v1/xxx"                # 选定端点
2. user_request = self.sample()         # 从 sampler 取请求对象
3. isinstance 校验请求类型             # 防止拿错类型的请求
4. 组装 payload 字典                    # model / messages / max_tokens ...
5. self.send_request(stream, endpoint, payload, parse_strategy, num_prefill_tokens)
```

`send_request` 的统一调度流程：

```
start_time = now()
response = requests.post(url, json=payload, stream=stream, headers)   # 发请求
post_end  = now()                                                     # POST 返回时刻

if status == 200:
    metrics = parse_strategy(response, start_time, num_prefill_tokens, post_end)
else:
    metrics = UserResponse(status_code, error_message=response.text)

# 异常兜底：ConnectionError→503, Timeout→408, RequestException→500
finally: response.close()
self.collect_metrics(metrics, endpoint)   # 上报（见 u3-l1）
```

注意第四个参数 `post_end`（代码里叫 `non_stream_post_end_time`）：它是 `requests.post` **返回的那个瞬间**。对非流式响应，此时整个响应体已经下载完毕，所以这个时刻就等于 `end_time`；对流式响应，真正的 `end_time` 要等解析器把流读完才知道，所以流式解析器会忽略这个参数、自己在循环末尾重新打时间戳。

#### 4.1.3 源码精读

`supported_tasks` 字典把任务映射到方法名（[genai_bench/user/openai_user.py:32-41](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L32-L41)）——这就是 u3-l1 讲过的“后端能力声明表”：

```python
class OpenAIUser(BaseUser):
    BACKEND_NAME = "openai"
    supported_tasks = {
        "text-to-text": "chat",
        "image-text-to-text": "chat",
        "text-to-embeddings": "embeddings",
        "text-to-rerank": "rerank",
        "text-to-image": "images_generations",
        "text-to-speech": "speech",
    }
```

`chat()` 是典型的任务方法（[genai_bench/user/openai_user.py:58-123](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L58-L123)）。它构造 payload 的关键部分——强制开启流式并要求返回 usage：

```python
payload = {
    "model": user_request.model,
    "messages": messages,
    "max_tokens": user_request.max_tokens,
    "temperature": ...,
    "stream": True,                       # 关键：chat 永远流式
    "stream_options": {"include_usage": True},  # 关键：要求最后一个 chunk 带 usage
    **filtered_params,
}
# vllm / sglang 才允许 ignore_eos（强制生成满 max_tokens）；OpenAI 官方不支持，要删掉
if self.api_backend in ["vllm", "sglang"]:
    payload.setdefault("ignore_eos", bool(user_request.max_tokens))
else:
    payload.pop("ignore_eos", None)

self.send_request(True, endpoint, payload, self.parse_chat_response,
                  user_request.num_prefill_tokens)
```

`stream_options.include_usage=True` 很重要——它要求服务端在流的**最后一个 chunk** 里附上 `usage`（含 `prompt_tokens` / `completion_tokens`），这样客户端才能拿到准确的 token 数。否则解析器只能走 tokenizer 回退估算（见 4.3）。

`send_request` 是全类的发送中枢（[genai_bench/user/openai_user.py:311-377](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L311-L377)）。它的精髓在于**用 `parse_strategy` 回调把“发”和“解析”解耦**，并用统一的异常兜底把网络错误翻译成带状态码的 `UserResponse`：

```python
response = requests.post(url=f"{self.host}{endpoint}", json=payload,
                         stream=stream, headers=self.headers)
non_stream_post_end_time = time.monotonic()

if response.status_code == 200:
    metrics_response = parse_strategy(response, start_time,
                                      num_prefill_tokens, non_stream_post_end_time)
else:
    metrics_response = UserResponse(status_code=response.status_code,
                                    error_message=response.text)
# ConnectionError→503, Timeout→408, RequestException→500
finally:
    if response is not None:
        response.close()        # 流式响应必须显式关闭，释放连接
```

为了能让 `send_request` 用同一种方式调用所有解析器，四个 `parse_*_response` 被设计成**完全相同的函数签名**（[genai_bench/user/openai_user.py:379-385](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L379-L385) 等处）：

```python
def parse_chat_response(self, response, start_time, num_prefill_tokens, _: float):
def parse_embedding_response(response, start_time, _: Optional[int], end_time):
def parse_rerank_response(response, start_time, num_prefill_tokens, end_time):
def parse_images_generations_response(response, start_time, _: Optional[int], end_time):
```

那些 `_` 和 `__` 是**故意留的占位参数**，目的就是保持接口一致——这样 `send_request` 不用关心对方是哪种解析器，统一传四个参数即可。这种“用占位参数对齐签名”是 Python 里实现策略模式的一种轻量写法。

#### 4.1.4 代码实践

**实践目标**：通过真实测试，验证 chat 请求确实发了正确的 payload 和 endpoint。

**操作步骤**：

1. 打开 [tests/user/test_openai_user.py:72](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_openai_user.py#L72) 的 `test_chat`。
2. 阅读它如何用 `mock_post` 拦截 `requests.post`，并用 `response_mock.iter_lines` 喂回一段模拟 SSE 字节流。
3. 看末尾 `mock_post.assert_called_once_with(...)` 断言的 url 是 `http://example.com/v1/chat/completions`，payload 里 `stream=True`、`stream_options={"include_usage": True}`。

**需要观察的现象**：测试通过即证明 `chat()` 组装的 payload 与本讲描述一致。

**预期结果**：`pytest tests/user/test_openai_user.py::test_chat -v` 通过。

> 待本地验证：实际运行结果以你本地环境为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `embeddings` 任务的 `send_request` 第一个参数是 `False`，而 `chat` 是 `True`？

> **答案**：第一个参数是 `stream`。embeddings 是非流式任务（一次性返回完整 JSON），所以 `False`；chat 要逐 token 测 TTFT，必须流式，所以 `True`。

**练习 2**：`send_request` 捕获到 `requests.exceptions.Timeout` 时，会构造一个什么状态码的 `UserResponse`？

> **答案**：408（见 [openai_user.py:363-366](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L363-L366)）。这样上层指标系统能把超时当作一次“失败请求”统计。

---

### 4.2 流式 chunk 解析

#### 4.2.1 概念说明

`parse_chat_response` 是本讲最核心、也最复杂的方法。它要从一段**逐行到达的字节流**里，重建出“模型说了什么”以及“每个关键时刻”。

SSE 流的每一行长这样（真实样本来自测试）：

```
data: {"choices":[{"delta":{"content":"R"},"finish_reason":null}]}
data: {"choices":[{"delta":{"content":"AG"},"finish_reason":null}]}
...
data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":10}}
data: [DONE]
```

解析器要在遍历这些行的过程中维护几个累加器：`generated_text`（已生成文本）、`tokens_received`（收到的 token 数）、`time_at_first_token`（首个 token 到达时刻）、`finish_reason`（结束原因）。

#### 4.2.2 核心流程

```
for chunk in response.iter_lines(chunk_size=None):     # 逐行读字节流
    chunk = chunk.strip()
    if 空: continue
    去掉 "data: " 或 "data:" 前缀          # 兼容两种写法
    if chunk == b"[DONE]": break            # 流结束标志

    data = json.loads(chunk)

    if data 里有 "error":                   # 流中途出错（见下）
        return UserResponse(error...)

    if 没有 choices 且 有 usage 且 之前见过 finish_reason:   # 标准 OpenAI 末尾 usage chunk
        提取 usage → break

    delta = data["choices"][0]["delta"]
    content = delta["content"] 或 reasoning_content
    if usage 在 delta 里: tokens_received = usage["completion_tokens"]
    if content 非空:
        if 还没记录首个 token 时刻:
            if tokens_received > 1: 打警告（首 chunk 就多个 token 会污染 TTFT）
            time_at_first_token = now()      # ★ 记录 TTFT 时间点
        generated_text += content
    finish_reason = choices[0].get("finish_reason")

    if finish_reason 且 同一 chunk 里就有 usage:   # SGLang v0.4.3~0.4.7 末尾格式
        提取 usage → break

end_time = now()                             # ★ 记录结束时刻
```

这里有一个**容易踩的坑**：有些服务端（比如早期 vLLM）会把多个 token 打包进**第一个** chunk。如果第一个 chunk 就带了 `completion_tokens > 1`，那么此时记录的 `time_at_first_token` 其实是“第 N 个 token 的时刻”而非“第 1 个”，TTFT 会被低估。代码因此特意检测这种情况并打 `🚨🚨🚨` 警告（[openai_user.py:459-465](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L459-L465) 与 [openai_user.py:492-498](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L492-L498)），提醒你这次 TTFT 不准。

另一个坑：**流中途出错**。有的服务端先返回 HTTP 200（让连接建立），然后在流里才吐一个 `{"error": {...}}`。代码在 [openai_user.py:431-437](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L431-L437) 专门处理这种情况，把它翻译成带错误码的 `UserResponse`。

#### 4.2.3 源码精读

主循环开头处理 SSE 的行格式与结束标志（[genai_bench/user/openai_user.py:399-426](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L399-L426)）：

```python
end_chunk = b"[DONE]"
...
for chunk in response.iter_lines(chunk_size=None):
    chunk = chunk.strip()
    if not chunk:
        continue
    if chunk.startswith(b"data: "):
        chunk = chunk[6:]          # 去掉 "data: "（带空格）
    elif chunk.startswith(b"data:"):
        chunk = chunk[5:]          # 去掉 "data:"（不带空格）
    else:
        continue                  # 既不是 data 行也不是空行，跳过
    if chunk == end_chunk:
        break                     # [DONE] → 流结束
    data = json.loads(chunk)
```

注意 `iter_lines(chunk_size=None)`：传入 `None` 表示**按行**而不是按固定字节数迭代，这正好契合 SSE“一行一条消息”的格式。

TTFT 的捕获在 content 分支里（[genai_bench/user/openai_user.py:481-499](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L481-L499)）——这是整个方法最关键的一段：

```python
delta = data["choices"][0]["delta"]
content = delta.get("content")
reasoning_content_chunk = self._get_reasoning_content_chunk(delta)
content = content or reasoning_content_chunk   # 普通内容优先，否则用推理内容
usage = delta.get("usage")
if usage:
    tokens_received = usage["completion_tokens"]
if content:
    if not time_at_first_token:                 # ★ 还没记录过首个 token
        if tokens_received > 1:
            logger.warning("...first chunk has >1 tokens...")   # TTFT 会不准
        time_at_first_token = time.monotonic()  # ★ 记录 TTFT 时刻
    generated_text += content
```

代码同时兼容两种“末尾 usage”格式。**标准 OpenAI**：最后一个 chunk 里 `choices` 为空、只含 `usage`（[openai_user.py:443-479](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L443-L479)）；**SGLang v0.4.3~v0.4.7**：最后一个 chunk 同时带 `finish_reason` 和 `usage`（[openai_user.py:507-514](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L507-L514)）。两处都会调用 `_get_usage_info` 提取 token 数后 `break`。

循环结束后构造返回值（[openai_bench/user/openai_user.py:525-568](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L525-L568)），把三个时间戳和 token 数装进 `UserChatResponse`：

```python
end_time = time.monotonic()
...
return UserChatResponse(
    status_code=200,
    generated_text=generated_text,
    tokens_received=tokens_received,
    time_at_first_token=time_at_first_token,
    num_prefill_tokens=num_prefill_tokens,
    start_time=start_time,
    end_time=end_time,
    reasoning_tokens=reasoning_tokens,
)
```

> 顺带一提 `speech` 任务虽然也“流式”，但流的是**二进制音频字节**而非 SSE 文本，所以它用 `response.iter_content(chunk_size=1024)` 按 1KB 块读，首个非空块的到达时刻即 `time_at_first_token`（见 [openai_user.py:277-309](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L277-L309)）。思路和 chat 一致：**首个有效数据块的到达时刻 = TTFT**。

#### 4.2.4 代码实践

**实践目标**：写一个最小函数，给定一段模拟的 SSE `data` 行列表，复用本讲的解析思路统计 `tokens_received` 与 `time_at_first_token`。这是脱离真实 HTTP、纯逻辑地理解解析器的好办法。

**操作步骤**：新建一个本地脚本（示例代码，非项目源码）：

```python
# 示例代码：模拟 OpenAI SSE 流式响应解析（非项目源码）
import json
import time

def parse_sse_lines(data_lines):
    """给定 SSE data 行列表，复用 parse_chat_response 思路统计关键指标。"""
    end_chunk = "[DONE]"
    generated_text = ""
    tokens_received = 0
    time_at_first_token = None
    finish_reason = None
    start_time = time.monotonic()

    for line in data_lines:
        line = line.strip()
        if not line:
            continue
        # 兼容 "data: " 和 "data:" 两种前缀
        if line.startswith("data: "):
            line = line[6:]
        elif line.startswith("data:"):
            line = line[5:]
        else:
            continue

        if line == end_chunk:
            break

        data = json.loads(line)

        # 末尾 usage chunk：choices 为空、只含 usage
        if not data.get("choices"):
            if data.get("usage"):
                tokens_received = data["usage"].get("completion_tokens", 0)
            break

        delta = data["choices"][0].get("delta", {})
        content = delta.get("content")
        if content:
            if time_at_first_token is None:   # ★ 记录 TTFT
                time_at_first_token = time.monotonic()
            generated_text += content
        finish_reason = data["choices"][0].get("finish_reason")

    end_time = time.monotonic()
    return {
        "generated_text": generated_text,
        "tokens_received": tokens_received,
        "time_at_first_token": time_at_first_token,
        "finish_reason": finish_reason,
        "ttft": (time_at_first_token - start_time) if time_at_first_token else None,
        "e2e_latency": end_time - start_time,
    }


if __name__ == "__main__":
    # 一段标准 OpenAI 流式响应（样本取自 tests/user/test_openai_user.py::test_chat）
    sse_lines = [
        'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"content":" world"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}',
        'data: [DONE]',
    ]
    print(parse_sse_lines(sse_lines))
```

**需要观察的现象**：`generated_text` 应为 `"Hello world"`；`tokens_received` 应为 `2`（来自末尾 usage chunk）；`time_at_first_token` 是个非 None 的浮点数；`ttft` 是个很小的正数。

**预期结果**：输出大致为

```python
{'generated_text': 'Hello world', 'tokens_received': 2, 'time_at_first_token': 1.2e-05,
 'finish_reason': 'stop', 'ttft': 1.5e-05, 'e2e_latency': 3.0e-05}
```

> 待本地验证：`time_at_first_token` / `ttft` 的具体数值取决于本机时钟，重点是结构正确。

**进阶**：把 `Hello` 那一行删掉再跑，观察 `time_at_first_token` 会推迟到 `world` 行；再删掉末尾 usage chunk，观察 `tokens_received` 退化为 `0`（这正是 4.3 回退估算要解决的场景）。

#### 4.2.5 小练习与答案

**练习 1**：为什么解析循环里要同时处理 `data: `（带空格）和 `data:`（不带空格）两种前缀？

> **答案**：不同服务端对 SSE 前缀的写法不一致，有的严格按规范 `data: `（带一个空格），有的省略空格。两种都剥掉才能兼容多家后端（见 [openai_user.py:416-419](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L416-L419)）。

**练习 2**：如果服务端在流的**第一个** chunk 里就塞了 3 个 token 的内容，会发生什么？

> **答案**：代码检测到首 chunk `tokens_received > 1`，会打 `🚨🚨🚨` 警告，说明这次 TTFT 不准确（因为记录的时刻对应的是第 3 个 token 而非第 1 个）。TTFT 值仍会记录，但可信度下降。

---

### 4.3 usage 提取与回退估算

#### 4.3.1 概念说明

token 数有两个来源：

1. **服务端权威报告**：流末尾的 `usage` 字段，含 `prompt_tokens`、`completion_tokens`，以及 `completion_tokens_details.reasoning_tokens`（推理 token）。这是首选。
2. **本地 tokenizer 估算**：当服务端不返回 usage 时，用模型自带的 tokenizer 对生成的文本重新编码计数。这是兜底。

基准测试追求**可重复、可比较**，所以优先用服务端数字；只有拿不到时才退而求其次用估算，并打警告提醒“这批数据是估的，横向比较要当心”。

`reasoning_tokens` 是给推理类模型（如 gpt-oss、DeepSeek-R1）准备的：这类模型在给出最终答案前，会先输出一段“思考过程”。genai-bench 把它单独统计，避免它和正常输出 token 混在一起影响吞吐指标（近期提交 `7fc8483 Fix gpt-oss and SMG reasoning token parsing` 正是修这块）。

#### 4.3.2 核心流程

**usage 提取**（`_get_usage_info`）：

```
num_prompt_tokens   = usage.prompt_tokens
tokens_received     = usage.completion_tokens
reasoning_tokens    = usage.completion_tokens_details.reasoning_tokens
if 视觉任务且本地 num_prefill_tokens 为空:
    num_prefill_tokens = num_prompt_tokens      # 用 prompt token 数覆盖图像 token
effective_prefill = num_prompt_tokens 优先，否则用本地 num_prefill_tokens
```

**回退估算**（在 `parse_chat_response` 末尾）：

```
if tokens_received 为 0（即服务端完全没给 usage）:
    tokens_received = sampler.get_token_length(generated_text)   # 用 tokenizer 数
    warning_once("tokens_received_estimated", ...)               # 只警告一次

if reasoning_tokens 为空 且 累积了 reasoning_text:
    reasoning_tokens = sampler.get_token_length(reasoning_text)  # 推理 token 也估
    warning_once("reasoning_tokens_estimated", ...)
```

`warning_once` 的作用是**去重**：这种“没有 usage”的情况可能在一个 run 里发生成千上万次，如果每次都打警告会刷爆日志。它用一个进程级集合记住“这个 key 已经警告过”，之后只打一条 debug。见 [genai_bench/logging.py:297-309](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/logging.py#L297-L309)。

#### 4.3.3 源码精读

usage 提取的统一实现（[genai_bench/user/openai_user.py:570-584](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L570-L584)）：

```python
@staticmethod
def _get_usage_info(data, num_prefill_tokens):
    num_prompt_tokens = data["usage"].get("prompt_tokens")
    tokens_received = data["usage"].get("completion_tokens", 0)
    details = data["usage"].get("completion_tokens_details") or {}
    reasoning_tokens = details.get("reasoning_tokens")
    # 视觉任务：用 prompt token 数覆盖 prefill，把图像 token 也算进去
    if num_prefill_tokens is None:
        num_prefill_tokens = num_prompt_tokens
    # 优先用服务端报的 prompt 数，否则回退到本地估算的 prefill
    effective_prefill = (num_prompt_tokens if num_prompt_tokens is not None
                         else num_prefill_tokens)
    return effective_prefill, num_prompt_tokens, tokens_received, reasoning_tokens
```

回退估算（[genai_bench/user/openai_user.py:536-558](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L536-L558)）：

```python
if not tokens_received:
    tokens_received = self.environment.sampler.get_token_length(
        generated_text, add_special_tokens=False)
    warning_once(logger, "tokens_received_estimated",
                 "🚨🚨🚨 There is no usage info returned ...")
if (not reasoning_tokens) and len(reasoning_text) > 0:
    reasoning_tokens = self.environment.sampler.get_token_length(
        reasoning_text, add_special_tokens=False)
    warning_once(logger, "reasoning_tokens_estimated", ...)
```

这里 `environment.sampler.get_token_length` 就是 u3-l1 里挂在 sampler 上的 token 计数函数，它本质上是 `len(tokenizer.encode(text))`（见 [genai_bench/sampling/base.py:53-55](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/base.py#L53-L55)）。注意传了 `add_special_tokens=False`——因为流里 `generated_text` 已经是去掉了特殊 token的纯文本，重新编码时不该再加回来，否则会多算。

推理字段的歧义处理（[genai_bench/user/openai_user.py:149-163](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L149-L163)）：不同后端把“思考内容”放在 `reasoning` 或 `reasoning_content` 两个字段之一。如果两个都出现且值不同，会警告“冲突，采用 `reasoning` 以免重复计数”：

```python
@staticmethod
def _get_reasoning_content_chunk(delta):
    reasoning = delta.get("reasoning")
    reasoning_content = delta.get("reasoning_content")
    if reasoning and reasoning_content and reasoning != reasoning_content:
        warning_once(logger, "conflicting_reasoning_fields",
                     "...Using reasoning and ignoring reasoning_content...")
    return reasoning or reasoning_content
```

#### 4.3.4 代码实践

**实践目标**：用真实测试验证“无 usage 时走 tokenizer 回退估算”。

**操作步骤**：

1. 打开 [tests/user/test_openai_user.py:332](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_openai_user.py#L332) 的 `test_chat_no_usage_info`。
2. 阅读它如何构造一段**没有 usage chunk** 的 SSE 流，并把 `sampler.get_token_length` mock 成固定返回值。
3. 观察断言：`tokens_received` 等于 mock 的返回值，并且 `caplog` 里捕获到了 `tokens_received_estimated` 警告。

**需要观察的现象**：缺少 usage 时，`tokens_received` 不再是 0，而是 tokenizer 估出来的值；同时日志里出现一次（且仅一次）估算警告。

**预期结果**：`pytest tests/user/test_openai_user.py::test_chat_no_usage_info -v` 通过。

> 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么回退估算时要传 `add_special_tokens=False`？

> **答案**：`generated_text` 是流里拼出来的纯文本，本身不含特殊 token（如 `<bos>`）。重新编码时若再加特殊 token，会凭空多算几个，导致 token 数偏大、吞吐指标失真。

**练习 2**：`warning_once` 为什么用 `(logger.name, key)` 作为去重键，而不是全局只警告一次？

> **答案**：不同模块、不同原因的警告互不干扰——比如 `tokens_received_estimated` 和 `reasoning_tokens_estimated` 是两件事，应该各自只警告一次；用 key 区分既避免刷屏，又不至于漏掉不同类型的提示。

---

## 5. 综合实践

把三个模块串起来，做一次“端到端解析追踪”：

1. **造数据**：仿照 [tests/user/test_openai_user.py:72](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_openai_user.py#L72) 的 `test_chat`，自己手写一段包含 5~8 个 chunk 的 SSE 字节流列表，要求覆盖：一个 `role` chunk、若干 `content` chunk、一个带 `finish_reason` 的 chunk、一个末尾 `usage` chunk、一个 `[DONE]`。
2. **解析**：用 4.2.4 写的 `parse_sse_lines` 函数跑这段数据，记录 `generated_text` / `tokens_received` / `time_at_first_token`。
3. **制造回退场景**：删掉末尾 usage chunk，把 `environment.sampler.get_token_length` 想象成 `lambda text: len(text.split())`（按空格数 token），重新计算 `tokens_received`，体会“无 usage 时数字从哪来”。
4. **画时序图**：在一张图上标出 `start_time`（POST 发起）、`time_at_first_token`（首个 content chunk）、`end_time`（循环结束）三个点，并标出 TTFT 和 e2e_latency 的区间。
5. **对照源码**：把你手算的 TTFT 区间，对应回 [openai_user.py:490-498](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L490-L498) 那段“首次 content 即记录时刻”的逻辑，确认理解一致。

完成后再回答：如果服务端把所有 content 合并进**一个** chunk 一次性返回（`completion_tokens=8`），你的 TTFT 还准吗？为什么 genai-bench 要对此打警告？

## 6. 本讲小结

- `OpenAIUser` 用 `supported_tasks` 字典把任务映射到方法，每个方法只负责**组装 payload + 选 endpoint**，然后统一委托 `send_request`。
- `send_request` 是发送中枢：用 `parse_strategy` 回调解耦“发”与“解析”，四个解析器靠**占位参数对齐签名**，并用统一的异常兜底把网络错误翻译成带状态码的 `UserResponse`。
- `parse_chat_response` 按 SSE 协议逐行解析：剥 `data:` 前缀、遇 `[DONE]` 结束、从 `delta.content` 累积文本，并在**首个 content chunk** 记录 `time_at_first_token`（TTFT 的来源）。
- 它兼容两种末尾 usage 格式（标准 OpenAI 的空 choices + usage，以及 SGLang 的 finish_reason + usage 同 chunk），并专门处理“流中途出错”和“首 chunk 多 token 污染 TTFT”两个坑。
- token 数优先取服务端 `usage`；拿不到时用 `sampler.get_token_length` 做 tokenizer 回退估算，并用 `warning_once` 去重警告。
- reasoning 模型的 `reasoning` / `reasoning_content` 字段单独累积为 `reasoning_tokens`，冲突时优先取 `reasoning` 以免重复计数。

## 7. 下一步学习建议

- 本讲的产出是 `UserChatResponse` / `UserResponse`（含三个时间戳和 token 数）。这些字段如何被换算成 TTFT / TPOT / 吞吐等最终指标，请进入 [u4-l1 单请求指标计算 RequestMetricsCollector](u4-l1-request-metrics-collector.md)。
- 想了解 `OpenAIUser` 之外的其他后端（OCI / AWS Bedrock / Azure / GCP / Together）如何复用或覆写这些解析方法，见 [u3-l3 多后端 User 体系](u3-l3-multi-backend-users.md)。
- 想从全局看一次请求如何从 CLI 跑到这里，可预习 [u8-l1 benchmark 主流程编排](u8-l1-benchmark-main-flow-capstone.md)。

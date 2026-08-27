# LLM 运行时：请求、重试与缓存

## 1. 本讲目标

在 [u2-l3](u2-l3-llm-config.md) 里我们认识了 `LLM` 这个**声明式配置类**——它只描述"连谁、用什么模型、重试几次"，自己不发任何请求。本讲打开它的执行侧：`LLMRuntime` 与 `LLMContext`。读完本讲，你应该能够：

1. 说清 `LLM`（配置）与 `LLMRuntime`（执行）分离的设计意图，以及 `runtime_for` 工厂的意义。
2. 精确描述重试语义：`retry_times` 是"额外重试次数"，`retry_interval_seconds` 只对**可重试异常**生效，空响应走的是另一条不等待的重试路径。
3. 解释 `is_retry_error` 如何把 openai / httpx / requests 三族异常分类成"值得重试"与"立即失败"。
4. 说出缓存键里 `cache_seed_content`（缓存种子）与 `protocol_version`（协议版本号）各自隔离的是什么，以及"上下文退出才提交缓存"的两阶段提交设计。
5. 动手搭一个本地 mock 服务，亲眼看到 500 → 500 → 成功的重试过程与日志。

## 2. 前置知识

### 2.1 声明式配置与运行时执行分离

pdf-craft 的 LLM 相关代码分两层：

- **`LLM` 类**（`core.py`）：一个普通类，字段就是参数，构造时只做两件有副作用的事——创建缓存/日志目录、用 tiktoken 加载编码器。它是"说明书"。
- **`LLMRuntime` 类**（`runtime.py`）：真正持有 openai 客户端、发请求、管重试、读写缓存。它是"发动机"。

分离的好处：同一个 `LLM` 配置可以按需创建多个运行时（后面会看到 XMLTranslator 用**同一个配置**建了两个运行时，仅靠协议版本号区分缓存），而配置对象本身可以安全地到处传递、比较、打印。

### 2.2 OpenAI 兼容 API 与流式补全

pdf-craft 不直连某家厂商，而是使用 openai 官方 Python SDK 的 `chat.completions.create(stream=True)`——只要服务端实现 OpenAI 兼容协议（DeepSeek、各类本地网关都兼容），换 `url` 即可换服务商。**流式（stream）** 指服务端一块一块地推回生成内容，客户端在每个 `chunk.choices[0].delta.content` 里收到增量文本，最后拼接成完整回复。

### 2.3 哪些错误值得重试

调用远程服务失败大致分三类：

| 类别 | 例子 | 值得重试吗 |
| --- | --- | --- |
| 传输层抖动 | 连接超时、网络中断、流中断 | 值得，通常下次就好了 |
| 服务端临时故障 | HTTP 500 / 502 / 503 / 429 限流 | 值得，稍等再试 |
| 请求本身有错 | 401 密钥错误、400 参数错误 | 不值得，重试一万次也一样 |

`error.py` 整个文件就是在做这张表的落地：`is_retry_error` 函数把异常映射成布尔值。

### 2.4 为什么 LLM 请求可以缓存

LLM 补全请求默认是**确定性输入**（模型、消息、采样参数相同）→ 输出可复用。翻译一本书动辄几百次请求，一旦中途失败重跑，若每次都重新请求会浪费大量 token。所以 pdf-craft 把"请求指纹 → 响应文本"落成磁盘文件缓存；而凡是会改变输出语义的因素（目标语言、提示词协议、采样参数）都必须参与指纹计算，否则会读到"错版本"的缓存——这正是本讲第三模块"缓存种子"的主题。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [pdf_craft/llm/core.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/core.py) | `LLM` 声明式配置类：字段定义、目录创建、tiktoken 编码器、jinja 模板宿主 |
| [pdf_craft/llm/runtime.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py) | 本讲主角：`LLMRuntime`（客户端与并发）、`LLMContext`（请求循环、重试、缓存两阶段提交）、两个运输层异常 |
| [pdf_craft/llm/error.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/error.py) | `is_retry_error`：openai / httpx / requests 三族异常的可重试判定 |
| [pdf_craft/llm/increasable.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/increasable.py) | `Increasable` / `Increaser`：temperature、top_p 的"区间爬升"调度 |
| [tests/test_llm_runtime.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_llm_runtime.py) | 运行时单元测试：缓存提交、空响应、运输失败，本讲实践的模板 |
| [pdf_craft/transformer/xml_translator/xml_translator/translator.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py) | 调用方示例：双运行时 + 双协议版本号的实战用法 |

## 4. 核心概念与源码讲解

### 4.1 运行时封装：从 LLM 配置到 LLMRuntime

#### 4.1.1 概念说明

`LLMRuntime` 是由一个 `LLM` 配置构造出来的执行器。它聚合了发请求需要的全部**有状态**资源：

- 一个 openai 客户端（连接池、超时）；
- 一个并发信号量（同一运行时最多 6 个在途请求）；
- 两个采样参数调度器（temperature / top_p 的 `Increasable`）；
- 一个日志器（可选落盘）。

工厂函数 `runtime_for(config, protocol_version=...)` 是全库创建运行时的唯一入口（`llm/__init__.py` 将其与 `LLMRuntime`、`LLMContext` 一并导出为公开 API）。

#### 4.1.2 核心流程

```text
LLM 配置 ──runtime_for(config, protocol_version)──▶ LLMRuntime
                                                        │
                              context(cache_seed_content)│  ──▶ LLMContext（上下文管理器）
                                                        │        │
                                                        │     request(input, ...)
                                                        │        │  查缓存 → 未命中 → 重试循环 → 成功后写临时缓存
                                                        │     with 块正常退出 → 临时缓存转正
```

- `LLMRuntime.request(...)` 是便捷入口：自己开一个上下文、发一次请求、提交缓存、返回结果（见 [pdf_craft/llm/runtime.py:L53-L60](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L53-L60)）。
- 需要批量发请求（如逐组翻译一整章）时，调用方自己 `with runtime.context(...) as ctx:` 长持上下文——这样"整批成功"才提交缓存（4.3 详解）。

#### 4.1.3 源码精读

**构造：关掉 SDK 自带重试，一切都自己来。**

[pdf_craft/llm/runtime.py:L41-L48](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L41-L48) —— 构造函数做了四件事：

```python
def __init__(self, config: LLM, *, protocol_version: str = "1") -> None:
    self.config = config
    self.protocol_version = protocol_version
    self._client = openai.OpenAI(api_key=config.key, base_url=config.url,
                                 timeout=config.timeout, max_retries=0)
    self._top_p, self._temperature = Increasable(config.top_p), Increasable(config.temperature)
    self._limiter = threading.BoundedSemaphore(6)
    self._logger = _create_logger(config.log_dir_path)
```

三个关键决策：

1. **`max_retries=0`**：openai SDK 自带一套指数退避重试，这里被显式关闭。为什么？因为 pdf-craft 要用**自己的** `retry_times` / `retry_interval_seconds` 语义（配置里可见、日志里可数），两层重试叠在一起会让"到底试了几次"变成黑盒。
2. **`timeout=config.timeout`**：超时交给 SDK 的 httpx 传输层执行，超时后抛 `openai.Timeout`——它在可重试名单里（4.2 详解）。
3. **`BoundedSemaphore(6)`**：进程内限流阀。多线程并发翻译时（见 u7-l4 的 `run_concurrency`），每个请求进入 `_invoke` 都要先拿到信号量，保证同一运行时最多 6 个在途请求，避免打爆服务商限流。

**协议版本号是构造参数而非配置字段**——这一点刻意为之：协议版本属于"调用协议"，同一份 `LLM` 配置可以在不同管线里以不同协议各建一个运行时：

[pdf_craft/transformer/xml_translator/xml_translator/translator.py:L41-L42](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/transformer/xml_translator/xml_translator/translator.py#L41-L42) —— XMLTranslator 用**可能完全相同的** LLM 配置建了两个运行时：

```python
self._translation_runtime = runtime_for(translation_llm, protocol_version="xml-translation-v1")
self._fill_runtime = runtime_for(fill_llm, protocol_version="xml-fill-v1")
```

翻译请求与回填请求的提示词语义不同，即使消息恰好相同，也不允许共用缓存条目——协议版本号参与缓存键（4.3 详解），天然隔离。

**发请求：流式拼接。**

[pdf_craft/llm/runtime.py:L72-L81](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L72-L81) —— `_invoke` 是唯一触碰网络的函数：

```python
def _invoke(self, messages: list[Message], max_tokens, temperature, top_p) -> str:
    converted = cast(list[ChatCompletionMessageParam], [
        {"role": message.role.name.lower(), "content": message.message} for message in messages
    ])
    with self._limiter:
        stream = self._client.chat.completions.create(model=self.config.model,
            messages=converted, stream=True, top_p=top_p, temperature=temperature,
            max_tokens=max_tokens)
        return "".join(chunk.choices[0].delta.content for chunk in stream
                       if chunk.choices and chunk.choices[0].delta.content)
```

- `Message` 是库自己的消息类型（`role` 为枚举 `MessageRole`，见 [pdf_craft/llm/types.py:L5-L15](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/types.py#L5-L15)），这里转成 openai 的字典格式。
- 生成器推导式里的 `if chunk.choices and chunk.choices[0].delta.content` 过滤了空块（如仅含 `role` 的首块、finish 块），只拼接真实增量文本。
- 注意 `_invoke` **不含任何重试**——重试在上一层 `LLMContext.request` 里，这让单元测试可以像 [tests/test_llm_runtime.py:L28](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_llm_runtime.py#L28) 那样直接替换 `runtime._invoke` 来模拟网络行为。

**采样参数调度：`_scheduled` 的三级优先级。**

[pdf_craft/llm/runtime.py:L62-L70](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L62-L70):

```python
@staticmethod
def _scheduled(value, source: Increasable, index, maximum):
    if value is not None:
        return value
    value_range = source._value_range
    if index is not None and maximum and value_range is not None:
        start, end = value_range
        return start + (end - start) * min(max(index, 0), maximum) / maximum
    return source.context().current
```

取值优先级为：**显式传参 > 按 `retry_index` 线性插值 > 区间起点**。当 `LLM(temperature=(0.1, 0.9))` 配置成区间、且外层修复循环传入 `retry_index / retry_max`（u8-l2 的主题）时，第 \( i \) 轮重试的采样参数按

\[ t_i = t_{start} + (t_{end} - t_{start}) \cdot \frac{\min(\max(i,0),\ m)}{m} \]

从起点线性爬到终点——直觉是：一轮轮修不好，就逐步"放开"采样随机性，鼓励模型跳出重复输出。`Increasable` 的归一化逻辑（单值 `(v, v)`、区间校验）见 [pdf_craft/llm/increasable.py:L18-L37](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/increasable.py#L18-L37)。

#### 4.1.4 代码实践：三个运行时，一个配置

**实践目标**：验证"配置可以复用，运行时各自独立"，并确认 `runtime_for` 不发任何网络请求。

**操作步骤**（以下为示例代码）：

```python
# lab_runtimes.py —— 示例代码
from pdf_craft.llm import LLM, runtime_for

config = LLM(key="fake", url="https://example.invalid/v1",
             model="demo", token_encoding="o200k_base")

r1 = runtime_for(config, protocol_version="a-v1")
r2 = runtime_for(config, protocol_version="b-v1")
print(r1.protocol_version, r2.protocol_version)
print(r1.config is config, r2.config is config)  # 两个运行时共享同一配置对象
```

**需要观察的现象**：脚本瞬间结束（`example.invalid` 是不可解析域名，若构造期发请求必然报错）；输出 `a-v1 b-v1` 与 `True True`。

**预期结果**：构造 `LLM` 与 `runtime_for` 全程无网络 IO；协议版本号存在运行时对象上而非配置对象上。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把 `protocol_version` 放进 `LLM` 配置类？
**答案**：协议版本描述的是"这次调用遵循哪套提示词/解析协议"，属于**调用方式**而非**服务凭据**。同一配置（同一个服务商账号）会被多条管线复用，各管线的协议演进节奏不同——放在 `runtime_for` 的参数上，才能一份配置派生多个互不干扰的运行时。

**练习 2**：`openai.OpenAI(..., max_retries=0)` 若改成默认值（2），`retry_times=5` 的语义会发生什么变化？
**答案**：SDK 会先在自己的传输层对可重试错误做最多 2 次内部重试，然后才把异常抛给 `LLMContext`——一次"逻辑尝试"实际可能发出 3 个 HTTP 请求，`retry_times=5` 最坏情况变成 15 个请求，且日志中的 attempt 计数与真实请求数对不上。显式关掉才能让重试语义完全由本层掌控。

### 4.2 重试与超时：错误分类与重试循环

#### 4.2.1 概念说明

`LLMContext.request` 是一个最多尝试 `retry_times + 1` 次的循环（首次请求之外最多再试 `retry_times` 次，默认 5 次）。每种失败有明确归宿：

| 失败情形 | 处理 | 最终异常 |
| --- | --- | --- |
| 可重试异常（超时/连接/5xx/429…） | 等待 `retry_interval_seconds` 后重试 | 耗尽后抛 `LLMTransportError`（带 `attempts` 与 `__cause__`） |
| 不可重试异常（401 等） | 立即停止 | 同样包装成 `LLMTransportError`，但 `attempts` 为当前次数 |
| 空响应（`response.strip()` 为空） | **不等待**，直接重试 | 耗尽后抛 `LLMEmptyResponseError`（带空响应次数） |

注意区分两个"重试"：这里是**运输层重试**（同一次语义请求的重发）；u7-l5 / u8-l2 讲的修复循环是**语义层重试**（带着错误反馈重新组织请求），后者会通过 `retry_index / retry_max` 影响前者的采样参数调度。

#### 4.2.2 核心流程

`LLMContext.request`（[pdf_craft/llm/runtime.py:L105-L150](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L105-L150)）的循环骨架：

```text
计算 temperature / top_p（三级优先级）
计算缓存键 key（use_cache=False 时 key=None）
若 key 命中缓存 → 记日志 cache-hit，直接返回
记日志 cache-miss
for attempt in 0..retry_times:            # 共 retry_times+1 次尝试
    记日志 request(attempt+1)
    response = _invoke(...)               # 唯一的网络调用
    若 response 为空白:
        empty_attempts += 1，记日志 empty-response
        若已是最后一次 → 抛 LLMEmptyResponseError
        continue                           # 注意：不 sleep
    若启用缓存 → 写临时缓存文件，登记到 _pending
    记日志 success，返回 response
    异常 error:
        retryable = is_retry_error(error)
        记日志 transport-error 或 non-retryable-error
        若不可重试 或 已是最后一次 → 抛 LLMTransportError(from error)
        sleep(retry_interval_seconds)      # 仅可重试且还有余量时
finally:
    temperature/top_p 的上下文调度器各 increase() 一次
```

#### 4.2.3 源码精读

**判定"值得重试"：三族异常与状态码白名单。**

[pdf_craft/llm/error.py:L6-L26](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/error.py#L6-L26) —— `is_retry_error` 依次探测三个异常族：

```python
def is_retry_error(err: Exception) -> bool:
    if _is_openai_retry_error(err):
        return True
    if _is_httpx_retry_error(err):
        return True
    if _is_request_retry_error(err):
        return True
    return False

def _is_openai_retry_error(err: Exception) -> bool:
    if isinstance(err, openai.Timeout):
        return True
    if isinstance(err, openai.APIConnectionError):
        return True
    if isinstance(err, openai.InternalServerError):
        return err.status_code in (500, 502, 503, 504, 520, 522, 524, 529)
    if isinstance(err, openai.APIStatusError):
        return err.status_code in (408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529)
    return False
```

三个要点：

1. **状态码白名单**：`APIStatusError` 分支放行 408（请求超时）、409（冲突）、425（过早）、429（限流）及 5xx 系列；401 / 403 / 400 等鉴权与参数错误不在名单 → 立即失败，不浪费重试。`InternalServerError` 是 `APIStatusError` 的子类，单独一行是为了可读性。
2. **为什么要查 httpx 和 requests 两族**（[pdf_craft/llm/error.py:L30-L54](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/error.py#L30-L54)）？openai SDK 底层用 httpx，但**流式迭代阶段**的断流可能以 httpx 原生异常（`RemoteProtocolError`、`StreamError`…）穿透出来；requests 则是防御其他代码路径。异常族会随 SDK 版本漂移，多查一族就少一类"明明该重试却直接崩"的事故。
3. 函数签名只依赖 `isinstance`，因此测试与 mock 都很容易构造。

**重试循环本体。**

[pdf_craft/llm/runtime.py:L121-L146](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L121-L146) —— 摘出异常处理分支：

```python
for attempt in range(self.runtime.config.retry_times + 1):
    try:
        ...
    except Exception as error:
        last_error = error
        retryable = is_retry_error(error)
        self._log("transport-error" if retryable else "non-retryable-error", attempt + 1, key=key, error=error)
        if isinstance(error, LLMEmptyResponseError):
            raise
        if not retryable or attempt >= self.runtime.config.retry_times:
            raise LLMTransportError("LLM transport request failed", attempts=attempt + 1, cause=error) from error
        if self.runtime.config.retry_interval_seconds > 0:
            time.sleep(self.runtime.config.retry_interval_seconds)
```

- `LLMTransportError`（定义在 [pdf_craft/llm/runtime.py:L25-L29](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L25-L29)）携带 `attempts`（总尝试次数）与 `__cause__`（原始异常），调用方既能量化"试了几次"，又能溯源根因。
- 空响应分支（[pdf_craft/llm/runtime.py:L125-L130](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L125-L130)）抛出的 `LLMEmptyResponseError` 在 except 里被原样上抛（`isinstance` 检查），不会再包一层；其 `attempts` 字段记录的是**空响应次数**而非总尝试次数（[pdf_craft/llm/runtime.py:L32-L35](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L32-L35)）。tests 里 `retry_times=1` 时断言 `attempts == 2` 正对应"首次 + 1 次重试全空"（[tests/test_llm_runtime.py:L47-L53](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_llm_runtime.py#L47-L53)）。

**日志：每个动作一行 JSON。**

[pdf_craft/llm/runtime.py:L160-L166](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L160-L166) —— `_log` 输出含 `session`（上下文 id）、`category`、`attempt`、`model`、`cache_key`、可选 `error` 的 JSON 行；类别共七种：`cache-hit` / `cache-miss` / `request` / `empty-response` / `success` / `transport-error` / `non-retryable-error`。日志器由 `_create_logger`（[pdf_craft/llm/runtime.py:L173-L182](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L173-L182)）创建：`log_dir_path` 为空时挂 `NullHandler`（零开销），否则每个运行时写一个 `request-<uuid>.log`。

#### 4.2.4 代码实践：mock 服务 500 → 500 → 成功

**实践目标**：用本地 mock 服务复现"前两次 500、第三次成功"，验证重试循环、间隔等待与日志记录。

**操作步骤**：

第一步，编写 mock 服务器（示例代码，依赖仅标准库 + 已安装的 pdf-craft 环境）：

```python
# mock_server.py —— 示例代码：OpenAI 兼容的最小 mock
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"count": 0}

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_response(404); self.end_headers(); return
        STATE["count"] += 1
        if STATE["count"] <= 2:                       # 前两次：服务端故障
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"mock internal error")
            return
        chunk = json.dumps({                          # 第三次起：合法 SSE 流
            "id": "mock", "object": "chat.completion.chunk", "model": "mock",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "delta": {"role": "assistant", "content": "你好，世界"}}],
        }, ensure_ascii=False)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(f"data: {chunk}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *args):                     # 静默默认访问日志
        pass

ThreadingHTTPServer(("127.0.0.1", 8977), Handler).serve_forever()
```

第二步，编写客户端脚本（示例代码）：

```python
# lab_retry.py —— 示例代码
from pathlib import Path
from pdf_craft.llm import LLM, runtime_for

log_dir = Path("llm-lab/logs")
config = LLM(key="mock-key", url="http://127.0.0.1:8977/v1",
             model="mock", token_encoding="o200k_base",
             retry_times=3, retry_interval_seconds=1,
             log_dir_path=log_dir)
runtime = runtime_for(config)
print("result:", runtime.request("hello", use_cache=True))
```

第三步，两个终端分别运行 `python mock_server.py` 与 `python lab_retry.py`，然后查看 `llm-lab/logs/` 下最新的 `request-*.log`。

**需要观察的现象**：客户端约 2 秒后返回（两次失败各等待 1 秒间隔）；日志文件应呈现这样的类别序列（示意，字段以实际为准）：

```text
cache-miss → request(1) → transport-error(1, error=InternalServerError)
           → request(2) → transport-error(2, error=InternalServerError)
           → request(3) → success(3)
```

**预期结果**：`result: 你好，世界`；日志里 `transport-error` 恰好 2 条、`request` 恰好 3 条，证明 `retry_times=3` 的余量足够吞掉两次瞬时故障。把 `retry_times` 改为 1 再跑（先重启服务器重置计数），应捕获 `LLMTransportError`，其 `attempts == 2`、`__cause__` 是 `openai.InternalServerError`。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：空响应路径为什么不做 `retry_interval_seconds` 等待，而异常路径要等？
**答案**：空响应说明服务端**正常应答**了（连接、鉴权、流式协议都通），只是生成内容为空——多半是模型采样问题，立即换一次采样是合理赌注；而异常路径的典型成因（限流、服务过载）恰恰需要时间恢复，不等只会在错误状态下连撞南墙。

**练习 2**：`LLMContext.request` 末尾 `finally` 里的 `self._temperature.increase()`（[pdf_craft/llm/runtime.py:L147-L149](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L147-L149)）到底影响谁？读 `_scheduled` 的 fallback 分支时注意 `source.context()` 每次新建 `Increaser`。
**答案**：容易误以为它让"同一上下文里的后续请求"逐步升温，但 `_scheduled` 的 fallback 用的是 `runtime._temperature.context()`——每次新建的 `Increaser`，其 `current` 恒为区间起点。因此当前实现中该 `increase()` 只推进了上下文对象自己的调度器状态（`self._temperature`），并未被后续 `request` 的取值路径读到；真正影响重试轮次采样的是 `retry_index / retry_max` 的线性插值分支。这是个值得在代码里跟一遍的细节。

**练习 3**：服务端返回 401 时会重试几次？
**答案**：0 次重试，即只请求 1 次。`openai.AuthenticationError` 是 `APIStatusError` 子类，401 不在状态码白名单 → `is_retry_error` 为 False → 走 `non-retryable-error` 日志并立即抛 `LLMTransportError(attempts=1)`。

### 4.3 缓存种子：cache_seed_content 与 protocol_version

#### 4.3.1 概念说明

缓存的本质是一个字典：**键 = 请求指纹的 sha256，值 = 响应文本**。指纹算错一个维度，就会出现两类事故：

- **该隔离没隔离**：换了目标语言却读到旧语言缓存；回填请求复用了翻译请求的缓存——结构对不上，回填直接失败。
- **该命中没命中**：无关字段混入键，缓存命中率暴跌，token 白烧。

pdf-craft 的键由八个维度构成（见下面源码），其中两个是本模块主角：

- **`cache_seed_content`（缓存种子）**：调用方注入的"业务语境"字符串。EPUB 翻译管线传入 `库版本:目标语言`——升级或换语言都换键，旧缓存自动整体失效。
- **`protocol_version`（协议版本号）**：提示词/解析协议的版本。它隔离的不是业务，而是**管线身份**。

#### 4.3.2 核心流程

```text
请求到达
  │ use_cache=False? ──是──▶ key=None（修复类请求绕过缓存，直接走网络）
  ▼
key = sha256(url, model, messages, seed, temperature, top_p, max_tokens, protocol)
  │ key 命中 {cache_path}/{key}.txt ──▶ 日志 cache-hit，返回文件内容（零网络）
  ▼ 未命中
请求成功 → 写临时文件 {key}.{context_id}.txt，登记进 _pending
  │
with 块退出
  ├─ 正常退出：临时文件改名 {key}.txt（若已存在则丢弃临时文件）——缓存提交
  └─ 异常退出：删除全部临时文件——缓存不提交
```

两阶段提交的意义：`LLMContext` 通常包住"一批"请求（例如一个章节的所有翻译组）。只要批内任何一步带着异常冲出 `with` 块，**整批新增缓存全部作废**——磁盘上的 `.txt` 永远只来自完整成功的批次，断点续跑时不会漏掉半成品。

#### 4.3.3 源码精读

**指纹的八个维度。**

[pdf_craft/llm/runtime.py:L152-L158](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L152-L158):

```python
def _cache_key(self, messages, max_tokens, temperature, top_p) -> str:
    payload = {"url": self.runtime.config.url, "model": self.runtime.config.model,
               "messages": [(m.role.name, m.message) for m in messages],
               "seed": self.cache_seed_content, "temperature": temperature,
               "top_p": top_p, "max_tokens": max_tokens,
               "protocol": self.runtime.protocol_version}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
```

逐维检视：`url + model` 定位服务与模型；`messages` 是输入本体；`temperature / top_p / max_tokens` 是采样与长度参数；`seed` 就是 `cache_seed_content`；`protocol` 来自运行时。`sort_keys=True` 保证字典序稳定、`ensure_ascii=False` 保留非 ASCII 原文——同一输入在任何机器上算出同一键。

**命中与落盘。**

[pdf_craft/llm/runtime.py:L110-L117](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L110-L117) —— 先查后发：

```python
key = self._cache_key(messages, max_tokens, temperature, top_p) if use_cache else None
cache_path = self.runtime.config.cache_path
if key and cache_path:
    cached = cache_path / f"{key}.txt"
    if cached.exists():
        self._log("cache-hit", 0, key=key)
        return cached.read_text(encoding="utf-8")
self._log("cache-miss", 0, key=key)
```

[pdf_craft/llm/runtime.py:L131-L134](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L131-L134) —— 成功后写**临时**文件并登记：

```python
if key and cache_path:
    temporary = cache_path / f"{key}.{self.context_id}.txt"
    temporary.write_text(response, encoding="utf-8")
    self._pending.add(temporary)
```

**提交/回滚在 `__exit__`。**

[pdf_craft/llm/runtime.py:L93-L103](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L93-L103) —— 上下文管理器的退出协议：

```python
def __exit__(self, exc_type, exc_val, exc_tb) -> None:
    for temporary in sorted(self._pending):
        if exc_type is None:
            permanent = temporary.with_name(temporary.name.rsplit(".", 2)[0] + ".txt")
            with _CACHE_LOCK:
                if permanent.exists():
                    temporary.unlink(missing_ok=True)
                else:
                    temporary.rename(permanent)
        else:
            temporary.unlink(missing_ok=True)
```

四个细节：文件名 `{key}.{context_id}.txt` 经 `rsplit(".", 2)` 剥掉 id 与后缀还原 `{key}.txt`；`_CACHE_LOCK`（[pdf_craft/llm/runtime.py:L22](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L22)）保证多线程并发提交时"检查-改名"原子；同名永久文件已存在则丢弃新临时文件——**第一个成功者赢**，缓存条目一经提交不再更新；异常路径一律 `unlink` 回滚。注意 `runtime.request` 便捷入口（[pdf_craft/llm/runtime.py:L57](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L57)）也用 `with` 包住，所以单次调用成功同样会提交缓存——tests 中先 `context` 后 `runtime.request` 共只触发一次 `_invoke`，正是对这条路径的验证（[tests/test_llm_runtime.py:L29-L32](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_llm_runtime.py#L29-L32)）。

**调用方一：EPUB 翻译的种子。**

[pdf_craft/pipeline/epub/translation/translator.py:L62-L72](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L62-L72) —— 种子串由**上游包版本 + 目标语言**拼成：

```python
translator = XMLTranslator(
    ...,
    cache_seed_content=f"{_get_version()}:{target_language}",
)
```

效果：pdf-craft 或 epub-translator 升级（`_get_version()` 取自包元数据，见 [pdf_craft/pipeline/epub/translation/translator.py:L190-L193](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/pipeline/epub/translation/translator.py#L190-L193)）、或用户改译日语为译英语，全部键值整体换代——旧缓存不删也不会被误用，只是自然失活。

**调用方二：目录分析的协议版本。**

[pdf_craft/extractor/toc/llm_analyser.py:L568](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/extractor/toc/llm_analyser.py#L568) —— `runtime_for(llm, protocol_version="toc-json-v1")`：目录层级分析与 XML 翻译即使共用一个 LLM 配置，缓存也互不相通。

#### 4.3.4 代码实践：让缓存"离线命中"

**实践目标**：亲手验证三件事——同键命中缓存可完全离线；换种子即换键；协议版本号独立隔离。

**操作步骤**（示例代码，复用 4.2.4 的 mock 服务器）：

```python
# lab_cache.py —— 示例代码
from pathlib import Path
import openai
from pdf_craft.llm import LLM, runtime_for

cache_dir = Path("llm-lab/cache")
config = LLM(key="k", url="http://127.0.0.1:8977/v1", model="mock",
             token_encoding="o200k_base", cache_path=cache_dir)

rt_a = runtime_for(config, protocol_version="p1")
rt_b = runtime_for(config, protocol_version="p2")

print("1:", rt_a.request("hello", cache_seed_content="v1:zh"))   # 真实请求，落缓存
print("2:", rt_a.request("hello", cache_seed_content="v1:zh"))   # 预期 cache-hit
print("3:", rt_a.request("hello", cache_seed_content="v1:en"))   # 换语言 → 换键 → 真实请求
print("4:", rt_b.request("hello", cache_seed_content="v1:zh"))   # 换协议 → 换键 → 真实请求
```

跑完第 1 步后**关掉 mock 服务器**再执行第 2 步对应的单独脚本（把其余行注释掉）。

**需要观察的现象**：第 2 步在服务器已停的情况下仍立即返回 `你好，世界`——cache-hit 路径发生在 `_invoke` 之前，根本不碰网络；`llm-lab/cache/` 下应出现 3 个不同文件名的 `.txt`（键 1、键 3、键 4 各一个）；日志中第 2 步记录 `cache-hit`、`attempt` 为 0。

**预期结果**：种子或协议任一变化都产生新缓存文件；同键请求只落一个文件。对照 tests 的写法（[tests/test_llm_runtime.py:L35-L45](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_llm_runtime.py#L35-L45) 断言缓存文件名 stem ≤ 64 字符——正是 sha256 十六进制的长度），确认文件名长度恰为 64。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 XML 回填请求要传 `use_cache=False`（u7-l2 已见）？结合键里的 `temperature` 维度解释。
**答案**：回填发生在修复循环中，每轮重试的采样参数随 `retry_index` 线性爬升，消息里还附加了逐轮不同的错误反馈——键几乎轮轮不同，缓存命中率趋近于零；更糟的是这些"中间态"结果一旦提交，只会污染缓存目录。`use_cache=False` 让 `key=None`：既不读也不写，是最诚实的选择。

**练习 2**：假设你把提示词模板 `translate.jinja` 改得面目全非，但忘了改协议版本号，会发生什么？
**答案**：消息内容变了，键里的 `messages` 维度随之变化，所以不会读到**旧消息**的缓存——协议版本号在多数场景是双保险。它真正不可替代的场景是：消息完全相同、但**解析/校验规则**变了（比如对同一份译文，旧协议接受、新协议拒绝）。此时若不换版本号，旧缓存里"按旧规则算合格"的条目会被新协议直接复用。升级解析逻辑时递增版本号（`xml-fill-v1` → `xml-fill-v2`）是标准动作。

**练习 3**：两个线程同时以同键请求（都未命中），各自成功后会发生什么？
**答案**：两个上下文各写自己的临时文件（`context_id` 不同不会互相覆盖）；退出时先到者在 `_CACHE_LOCK` 保护下把临时文件改名为永久文件，后到者发现永久文件已存在，直接删除自己的临时文件——缓存内容定格为第一个成功者，写缓存是幂等的。

## 5. 综合实践

把三个模块串成一场"故障演练"。延续 4.2.4 的 mock 服务器与日志目录，完成一份补强测试（示例代码，文件名建议 `tests/test_llm_timeout_retry.py`，写在前缀 `test_` 的临时脚本里运行亦可，不要提交到仓库）：

**任务 A（超时用例）**：参考 [tests/test_llm_runtime.py:L10-L13](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_llm_runtime.py#L10-L13) 的 `_config` 与该文件替换 `runtime._invoke` 的手法，构造一个"每次调用都抛 `openai.Timeout`"的桩，断言：`retry_times=2, retry_interval_seconds=0` 时共调用 3 次，最终抛 `LLMTransportError` 且 `attempts == 3`、`__cause__` 是 `openai.Timeout`：

```python
# —— 示例代码 ——
import tempfile, unittest, httpx, openai
from pathlib import Path
from pdf_craft.llm import LLM, runtime_for
from pdf_craft.llm.runtime import LLMTransportError

class TestTimeoutRetry(unittest.TestCase):
    def test_timeout_retries_then_wraps_transport_error(self):
        with tempfile.TemporaryDirectory() as directory:
            config = LLM("k", "https://example.invalid/v1", "m", "o200k_base",
                         retry_times=2, retry_interval_seconds=0)
            runtime = runtime_for(config)
            calls = []
            def fake_invoke(*_args):
                calls.append(1)
                raise openai.Timeout(httpx.Request("POST", "https://example.invalid/v1/chat/completions"))
            runtime._invoke = fake_invoke
            with self.assertRaises(LLMTransportError) as raised:
                runtime.request("hello", use_cache=False)
            self.assertEqual(len(calls), 3)
            self.assertEqual(raised.exception.attempts, 3)
            self.assertIsInstance(raised.exception.__cause__, openai.Timeout)

if __name__ == "__main__":
    unittest.main()
```

要点：`openai.Timeout` 只需一个 `httpx.Request` 即可构造；`retry_interval_seconds=0` 让测试免于真实等待（循环里 `> 0` 才 sleep，见 [pdf_craft/llm/runtime.py:L145-L146](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/runtime.py#L145-L146)）。

**任务 B（真实超时观察，可选）**：给 mock 服务器加一个分支——请求体包含 `"timeout-lab"` 时 `time.sleep(10)` 再响应；客户端配 `timeout=0.5, retry_times=1, retry_interval_seconds=0`，确认抛出 `LLMTransportError` 且日志出现两条 `transport-error`、`error=Timeout`。

**验收标准**：任务 A 测试通过；能口头回答——超时为什么属于可重试错误（`error.py` 白名单）、3 次调用对应"首次 + 2 次重试"的语义、`attempts` 与 `__cause__` 各自的来源。

## 6. 本讲小结

- **配置与执行分离**：`LLM` 是无网络的说明书；`runtime_for(config, protocol_version=...)` 造发动机，显式关闭 openai SDK 的内部重试（`max_retries=0`），把重试语义完全收归本层，并用 `BoundedSemaphore(6)` 做进程内在途请求限流。
- **重试语义**：`retry_times` 是首次之外的额外重试次数（默认 5），`retry_interval_seconds`（默认 6 秒）只对 `is_retry_error` 判定为可重试的异常生效；空响应重试**不等待**；不可重试错误（如 401）一次即败。
- **错误归宿**：可重试/不可重试异常最终都包装为 `LLMTransportError`（携带 `attempts` 与 `__cause__`），空响应耗尽抛 `LLMEmptyResponseError`（`attempts` 计空响应次数）；`is_retry_error` 用状态码白名单覆盖 openai/httpx/requests 三族异常。
- **采样调度**：`_scheduled` 三级优先级——显式传参 > 修复循环 `retry_index` 的线性插值（区间起点向终点爬升）> 区间起点；`_invoke` 以流式方式拼接增量文本。
- **缓存指纹**：键是八个维度（url、model、messages、seed、temperature、top_p、max_tokens、protocol）的 sha256；`cache_seed_content` 由调用方注入业务语境（EPUB 翻译传 `版本:目标语言`），`protocol_version` 标识管线协议——两者任一变化即整体换键。
- **两阶段提交**：成功先写 `{key}.{context_id}.txt` 临时文件，上下文**正常退出**才改名转正（首个成功者赢、之后不再更新），异常退出全部回滚——磁盘缓存永远只含完整成功批次的产物。

## 7. 下一步学习建议

本讲讲完了"一次请求怎么发、怎么重试、怎么缓存"。下一讲 [u8-l2 修复循环：协议驱动的重试](u8-l2-repair-loop-protocol.md) 将进入语义层：`run_repair_loop` 与 `ResponseProtocol` 如何用 Success/Retry/Partial/Failure 四种判定把"结构不合格的回复"一轮轮修好，其中传入 `request` 的 `retry_index / retry_max` 正是本讲 `_scheduled` 线性插值的驱动来源。建议先行阅读 [pdf_craft/llm/loop.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/pdf_craft/llm/loop.py) 与 [tests/test_llm_loop.py](https://github.com/oomol-lab/pdf-craft/blob/bbb2d20a93178f9bc3b7be6b23e8f5b23f551c50/tests/test_llm_loop.py)，并带着一个问题去读：修复循环的重试与运输层的重试在同一参数体系里如何各司其职？

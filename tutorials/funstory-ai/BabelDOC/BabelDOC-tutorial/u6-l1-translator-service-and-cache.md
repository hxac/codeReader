# 翻译器服务与缓存：OpenAITranslator

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `BaseTranslator` 与 `OpenAITranslator` 各自的职责，理解「模板方法」式的调用约定（外部只调 `translate`，子类只实现 `do_translate`）。
- 掌握 BabelDOC 的翻译缓存机制：用 peewee + SQLite 落盘，以 `(engine, params, text)` 作为唯一键，并在写入时按概率触发自动清理。
- 认识漏桶（leaky bucket）限流 `RateLimiter` 如何用 `time.monotonic()` 保证线程安全、平滑请求速率，以及全局 `_translate_rate_limiter` 与 `set_translate_rate_limiter` 的关系。
- 理解 token 统计如何用 `AtomicInteger` 在多线程下累加，以及它在「自动术语抽取」等下游模块里被读取的方式。

本讲只聚焦**翻译器服务本身**（一次句子的翻译、缓存、限流、计数）。至于「如何把整篇 PDF 的段落批量、并发、带占位符地送进翻译器」，是下一讲 u6-l2 `ILTranslator` 编排的主题，本讲不展开。

## 2. 前置知识

在进入源码前，先用通俗语言铺垫几个概念。

- **翻译服务（Translation Service）**：一个能接收原文、返回译文的外部接口。BabelDOC 默认对接 **OpenAI 兼容 API**（官方 OpenAI、DeepSeek、GLM、本地 Ollama 等都遵循这套 Chat Completions 协议），所以它只内置了 `OpenAITranslator` 一个实现。
- **缓存（Cache）**：同一个句子在「同样的引擎、同样的参数」下，译文是确定的（BabelDOC 固定 `temperature=0`）。把译文存下来，下次遇到同样的句子直接返回，既省钱又省时间。BabelDOC 把缓存放在本地 SQLite 文件里，跨进程、跨次运行都能复用。
- **QPS 限流（Rate Limiting）**：QPS = Queries Per Second（每秒请求数）。翻译服务通常对调用频率有上限，超过会被拒（429）或封禁。BabelDOC 在**本地**用一个「漏桶」算法主动控制发送节奏，避免触发上游限制。
- **漏桶算法（Leaky Bucket）**：想象一个底部有小孔的水桶，水（请求）以恒定速率漏出。无论你瞬时倒多少水进来，出口都是匀速的。代码里体现为「相邻两次请求之间，至少要间隔 `min_interval = 1 / QPS` 秒」。
- **peewee**：一个轻量级的 Python ORM（对象关系映射），让你用 Python 类操作数据库表，而不必手写 SQL。BabelDOC 用它定义 `_TranslationCache` 这张表。
- **SQLite + WAL**：SQLite 是一个单文件嵌入式数据库；WAL（Write-Ahead Logging）是它的一种日志模式，允许「读」和「写」并发，适合多线程翻译场景。

> 名词速查：`peewee`、`SqliteDatabase`、`httpx`、`openai`（官方 Python SDK）、`tenacity`（重试库）、`AtomicInteger`（线程安全计数器）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [babeldoc/translator/translator.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py) | 翻译器主体：`RateLimiter` 漏桶限流、`BaseTranslator` 抽象基类（含缓存读写骨架）、`OpenAITranslator` OpenAI 兼容实现、token 统计。 |
| [babeldoc/translator/cache.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/cache.py) | 翻译缓存：peewee 模型 `_TranslationCache`、外层封装 `TranslationCache`、数据库初始化 `init_db` 与自动清理逻辑。 |
| [babeldoc/const.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/const.py) | 全局常量，定义缓存目录 `CACHE_FOLDER = ~/.cache/babeldoc`。 |
| [babeldoc/utils/atomic_integer.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/utils/atomic_integer.py) | 线程安全整数 `AtomicInteger`，供 token 计数使用。 |
| [babeldoc/main.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py) | CLI 入口，把 `--qps`、`--openai-*` 参数装配成 `OpenAITranslator` 并调用 `set_translate_rate_limiter`。 |

一句话串联：`main.py` 实例化 `OpenAITranslator`（内部建缓存、连 OpenAI、初始化 token 计数）→ 调 `set_translate_rate_limiter(qps)` 设置全局限流 → 下游 `ILTranslator` 拿到这个翻译器后逐句调用 `translator.translate(text)`。

## 4. 核心概念与源码讲解

### 4.1 翻译器服务：BaseTranslator 与 OpenAITranslator

#### 4.1.1 概念说明

翻译器是 BabelDOC 唯一直接对话 LLM 的地方。设计上分两层：

- `BaseTranslator`：抽象基类，**不关心具体用哪家 LLM**，只负责「缓存查 → 限流 → 真正翻译 → 缓存写」这套**对所有引擎都一样**的骨架流程。它用「模板方法」模式：对外暴露 `translate()`，对内要求子类实现 `do_translate()`。
- `OpenAITranslator`：`BaseTranslator` 的唯一内置子类，把 `do_translate()` 落实为「调用 OpenAI 兼容的 Chat Completions」。

这样设计的好处：要接别的引擎（比如 Google、Anthropic），只需新写一个子类实现 `do_translate`，缓存和限流「骨架」自动复用。

#### 4.1.2 核心流程

`translate(text)` 的执行过程（见下方伪代码，对应 [translator.py:120-139](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L120-L139)）：

```
translate(text):
    translate_call_count += 1                      # 统计调用次数
    if 缓存可用:
        cached = cache.get(text)
        if cached 命中:
            translate_cache_call_count += 1
            return cached                          # 直接返回，不调 LLM、不限流
    rate_limiter.wait()                            # 漏桶等待，控制 QPS
    translation = do_translate(text)               # 子类实现：真正调 LLM
    if 缓存可用:
        cache.set(text, translation)               # 写回缓存
    return translation
```

关键点：**缓存命中时既不调 LLM 也不消耗限流配额**，这是省钱的根本来源。注意 `do_translate` 被一个重试装饰器包裹，遇到上游 429（`openai.RateLimitError`）会指数退避重试。

#### 4.1.3 源码精读

**BaseTranslator 的缓存骨架** —— 在构造时为每个翻译器建一个绑定语言对的 `TranslationCache`：

```python
# babeldoc/translator/translator.py:85-98
def __init__(self, lang_in, lang_out, ignore_cache):
    self.ignore_cache = ignore_cache
    lang_in = self.lang_map.get(lang_in.lower(), lang_in)   # 语言名归一化
    lang_out = self.lang_map.get(lang_out.lower(), lang_out)
    self.lang_in = lang_in
    self.lang_out = lang_out
    self.cache = TranslationCache(
        self.name,                                            # 引擎名作缓存键的一部分
        {"lang_in": lang_in, "lang_out": lang_out},           # 语言对作缓存参数
    )
```

语言对（`lang_in`/`lang_out`）被放进缓存参数，意味着「英→中」和「英→日」的同一句话不会串缓存。`lang_map` 是子类可覆盖的「语言别名映射表」，用于把 `english`/`en` 等别名统一。

参考链接：[BaseTranslator.__init__（构造与缓存绑定）：translator.py:85-98](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L85-L98)

**translate 方法本身**（就是上面伪代码的对应实现）：

```python
# babeldoc/translator/translator.py:120-139
def translate(self, text, ignore_cache=False, rate_limit_params: dict = None):
    self.translate_call_count += 1
    if not (self.ignore_cache or ignore_cache):
        try:
            cache = self.cache.get(text)
            if cache is not None:
                self.translate_cache_call_count += 1
                return cache
        except Exception as e:
            logger.debug(f"try get cache failed, ignore it: {e}")
    _translate_rate_limiter.wait()
    translation = self.do_translate(text, rate_limit_params)
    if not (self.ignore_cache or ignore_cache):
        self.cache.set(text, translation)
    return translation
```

注意 `rate_limit_params` 参数：它是从外部传进来的「限流提示」（例如 JSON 模式请求），透传给 `do_translate`，基类自己的 `RateLimiter.wait()` 并不读取它。参考链接：[translate 方法：translator.py:120-139](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L120-L139)。

**OpenAITranslator 的构造** —— 重点看它如何「连上服务」并把影响翻译质量的参数登记进缓存键：

```python
# babeldoc/translator/translator.py:229-256（节选）
self.client = openai.OpenAI(
    base_url=base_url, api_key=api_key,
    http_client=httpx.Client(
        limits=httpx.Limits(max_connections=None, max_keepalive_connections=None),
        timeout=600,                                        # 单请求 10 分钟超时
    ),
)
...
self.add_cache_impact_parameters("model", self.model)        # 不同模型 → 不同缓存
self.add_cache_impact_parameters("prompt", self.prompt(""))  # 提示词变 → 缓存失效
...
self.token_count = AtomicInteger()
self.prompt_token_count = AtomicInteger()
self.completion_token_count = AtomicInteger()
self.cache_hit_prompt_token_count = AtomicInteger()
```

`add_cache_impact_parameters(k, v)` 会把这个键值对追加进缓存参数（详见 4.2.3）。这里把 `model`、`prompt`、`temperature`、`reasoning` 等都登记进去——意思是：**换模型或改提示词，旧缓存自动失效**，避免用旧模型的译文冒充新模型的结果。参考链接：[OpenAITranslator.__init__（连接与缓存参数登记）：translator.py:229-256](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L229-L256)。

**真正调用 LLM 的 do_translate** —— 注意它被 `tenacity` 重试装饰器包裹，且有固定的 `temperature=0`：

```python
# babeldoc/translator/translator.py:259-277
@retry(
    retry=retry_if_exception_type(openai.RateLimitError),    # 仅对 429 重试
    stop=stop_after_attempt(100),                            # 最多 100 次
    wait=wait_exponential(multiplier=1, min=1, max=15),      # 指数退避 1~15s
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def do_translate(self, text, rate_limit_params: dict = None) -> str:
    options = {}
    if self.send_temperature:
        options.update(self.options)                         # self.options = {"temperature": 0}
    response = self.client.chat.completions.create(
        model=self.model, **options,
        messages=self.prompt(text), extra_body=self.extra_body,
    )
    self.update_token_count(response)
    return response.choices[0].message.content.strip()
```

注释里写明了为什么固定温度为 0：「随机采样可能会打断公式标记」。`self.options` 在构造时设为 `{"temperature": 0}`（见 [translator.py:221](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L221)）。参考链接：[do_translate（带重试的真实调用）：translator.py:259-277](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L259-L277)。

**提示词模板**（系统提示 + 用户提示，译文语言由 `lang_out` 决定）：

```python
# babeldoc/translator/translator.py:279-289
def prompt(self, text):
    return [
        {"role": "system",
         "content": "You are a professional,authentic machine translation engine."},
        {"role": "user",
         "content": f";; Treat next line as plain text input and translate it into {self.lang_out}, "
                    f"output translation ONLY. ... Input:\n\n{text}"},
    ]
```

提示词要求「只输出译文、不要解释」，并且对专有名词/代码/公式占位符原样返回——这正是 `prompt` 被登记进缓存键的原因：改提示词就是改翻译行为。参考链接：[prompt 模板：translator.py:279-289](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L279-L289)。

> 另有 `llm_translate / do_llm_translate`（[translator.py:141-165](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L141-L165) 与 [291-337](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L291-L337)）是「裸 LLM 调用」通道：它**不带提示词模板、用户消息就是原文**，支持 JSON 输出模式与 DashScope 内容安全头，被 `ILTranslatorLLMOnly`（u6-l3）用于整段上下文翻译。其缓存骨架与 `translate` 完全一致。

#### 4.1.4 代码实践

**实践目标**：阅读 `translate` 的缓存骨架，跟踪一次「缓存未命中→调用→写入」与一次「缓存命中→直接返回」的差异。

**操作步骤**：

1. 打开 [translator.py:120-139](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L120-L139)。
2. 在脑中（或在本地 fork 上）对同一句 `"Hello"` 模拟两次 `translate("Hello")` 调用。
3. 第一次：`cache.get("Hello")` 返回 `None` → `_translate_rate_limiter.wait()` → `do_translate` 调 LLM → `cache.set("Hello", "你好")`。
4. 第二次：`cache.get("Hello")` 命中 `"你好"` → `translate_cache_call_count += 1` → 直接 return，**完全不经过限流与 LLM**。

**需要观察的现象**：第二次调用应当几乎瞬时返回，且 `translate_cache_call_count` 比 `translate_call_count` 少 1（因为第一次未命中）。这两次计数在翻译器析构时由 `__del__` 打印（[translator.py:103-110](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L103-L110)）。

**预期结果**：能复述「缓存命中走捷径、未命中才限流+调 LLM」这条分支。运行现象待本地验证（需一个 OpenAI 兼容服务）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 BabelDOC 把 `temperature` 固定为 0，而不是让用户自由调？

**参考答案**：因为缓存假设「同样的输入产生同样的输出」。若 `temperature > 0`，同一句话每次调用 LLM 可能得到不同译文，缓存里存的旧译文就不再可信，会污染翻译质量。固定温度为 0 才能保证缓存命中的译文是确定的、可复用的。

**练习 2**：`do_translate` 的重试只对 `openai.RateLimitError` 生效。如果上游返回 `BadRequestError`（比如内容违规），会发生什么？

**参考答案**：`BadRequestError` 不在 `retry_if_exception_type` 列表里，`tenacity` 不会重试，异常会向上抛出。在 `do_llm_translate` 中，代码额外捕获了 `openai.BadRequestError` 并把阿里云 DashScope 的「内容敏感」中文消息转成 `ContentFilterError`（[translator.py:330-337](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L330-L337)），但 `do_translate` 不做这层转换。

---

### 4.2 翻译缓存：TranslationCache（SQLite + peewee）

#### 4.2.1 概念说明

翻译缓存是 BabelDOC 省钱省时的核心。它把「原文 → 译文」的映射持久化到一个本地 SQLite 文件里（`~/.cache/babeldoc/cache.v1.db`），跨进程、跨次运行复用。它的设计目标有三个：

1. **正确区分**：不同引擎、不同语言对、不同模型/提示词的译文不能串。
2. **并发安全**：翻译是多线程的（u6-l2 会看到线程池），读写不能互相破坏。
3. **不无限膨胀**：缓存行数有上限，超过自动清理旧记录。

#### 4.2.2 核心流程

缓存的唯一键由三部分组成：`(translate_engine, translate_engine_params, original_text)`。

- `translate_engine`：引擎名，如 `"openai"`。
- `translate_engine_params`：一个**递归排序后**的 JSON 字符串，内容是 `{lang_in, lang_out, model, prompt, temperature, ...}`。
- `original_text`：原文。

为什么要把 params 递归排序再 `json.dumps`？因为字典的键顺序在 Python 里不一定稳定（虽然 3.7+ 保序，但不同代码路径可能以不同顺序构建），排序后才能保证「内容相同的参数」生成完全相同的字符串，从而正确匹配缓存。

数据表 `_TranslationCache` 在数据库层用 `UNIQUE (...) ON CONFLICT REPLACE` 约束：插入重复键时直接替换，保证一行只对应一份译文。

```
get(text):  按 (engine, params, text) 查；命中→返回 translation，未命中→返回 None
            命中时有 0.1% 概率顺手触发清理
set(text, translation): 插入一行；ON CONFLICT REPLACE 自动覆盖旧值
            写入时有 0.1% 概率顺手触发清理
```

#### 4.2.3 源码精读

**数据模型 `_TranslationCache`** —— 一张表四个字段加唯一约束：

```python
# babeldoc/translator/cache.py:31-51
class _TranslationCache(Model):
    id = AutoField()
    translate_engine = CharField(max_length=20)        # 引擎名，最长 20 字符
    translate_engine_params = TextField()              # 排序后的 JSON
    original_text = TextField()
    translation = TextField()

    class Meta:
        database = db
        constraints = [
            SQL("UNIQUE (translate_engine, translate_engine_params, original_text) "
                "ON CONFLICT REPLACE"),
        ]
```

注意 `translate_engine = CharField(max_length=20)` 这条约束，它正是 `BaseTranslator` 里 `name = "base"` 旁边那条注释「Due to cache limitations, name should be within 20 characters」（[translator.py:80-82](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L80-L82)）的来源。参考链接：[缓存表定义：cache.py:31-51](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/cache.py#L31-L51)。

**外层封装 TranslationCache 的参数管理** —— `_sort_dict_recursively` + `json.dumps` 生成稳定键：

```python
# babeldoc/translator/cache.py:55-89（节选）
@staticmethod
def _sort_dict_recursively(obj):
    if isinstance(obj, dict):
        return {k: TranslationCache._sort_dict_recursively(v)
                for k in sorted(obj.keys()) for v in [obj[k]]}
    elif isinstance(obj, list):
        return [TranslationCache._sort_dict_recursively(item) for item in obj]
    return obj

def add_params(self, k: str, v):
    self.params[k] = v
    self.replace_params(self.params)        # 每次都重排序 + 重新 dumps
```

`add_params` 被 `BaseTranslator.add_cache_impact_parameters`（[translator.py:112-118](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L112-L118)）调用——这就是 4.1 里 `OpenAITranslator` 登记 `model`/`prompt` 的落点。参考链接：[递归排序与参数管理：cache.py:55-89](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/cache.py#L55-L89)。

**get / set 的读写与「database is locked」容错**：

```python
# babeldoc/translator/cache.py:93-126（节选）
def get(self, original_text: str) -> str | None:
    try:
        result = _TranslationCache.get_or_none(
            translate_engine=self.translate_engine,
            translate_engine_params=self.translate_engine_params,
            original_text=original_text,
        )
        if result and random.random() < CLEAN_PROBABILITY:    # 0.1% 概率清理
            self._cleanup()
        return result.translation if result else None
    except peewee.OperationalError as e:
        if "database is locked" in str(e):
            logger.debug("Cache is locked")
            return None                                       # 锁冲突→当作未命中
        else:
            raise
```

注意两处容错思想：一是「锁冲突时 `get` 返回 None、`set` 静默跳过」，宁可丢失一次缓存也不要让翻译线程崩掉；二是「按概率触发清理」，避免每次写都清理造成的开销。参考链接：[get/set 与锁容错：cache.py:93-126](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/cache.py#L93-L126)。

**自动清理 `_cleanup`** —— 用线程级锁保证同一进程内只有一个清理在跑：

```python
# babeldoc/translator/cache.py:128-145
def _cleanup(self) -> None:
    if not _cleanup_lock.acquire(blocking=False):     # 拿不到锁就直接返回
        return
    try:
        max_id = _TranslationCache.select(fn.MAX(_TranslationCache.id)).scalar()
        if not max_id or max_id <= MAX_CACHE_ROWS:    # 不足 5 万行不清理
            return
        threshold = max_id - MAX_CACHE_ROWS
        _TranslationCache.delete().where(_TranslationCache.id <= threshold).execute()
    finally:
        _cleanup_lock.release()
```

`MAX_CACHE_ROWS = 50_000`（[cache.py:25](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/cache.py#L25)）：始终只保留最新的 5 万条。参考链接：[自动清理：cache.py:128-145](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/cache.py#L128-L145)。

**数据库初始化 init_db** —— 文件名带版本号，开启 WAL 与忙等待：

```python
# babeldoc/translator/cache.py:148-162（节选）
def init_db(remove_exists=False):
    CACHE_FOLDER.mkdir(parents=True, exist_ok=True)
    cache_db_path = CACHE_FOLDER / "cache.v1.db"          # 版本号 v1 写进文件名
    ...
    db.init(cache_db_path, pragmas={
        "journal_mode": "wal",                            # WAL：读写并发
        "busy_timeout": 1000,                             # 锁等待 1 秒
    })
    db.create_tables([_TranslationCache], safe=True)
```

缓存目录 `CACHE_FOLDER` 来自 [const.py:11](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/const.py#L11) 的 `Path.home() / ".cache" / "babeldoc"`。关键细节：模块末尾有一行 `init_db()`（[cache.py:199](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/cache.py#L199)），即**导入 `babeldoc.translator.cache` 这个模块时，数据库就被自动初始化了**——这也是为什么你只要用过一次 babeldoc，`~/.cache/babeldoc/cache.v1.db` 就会出现。参考链接：[init_db：cache.py:148-162](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/cache.py#L148-L162)。

#### 4.2.4 代码实践

**实践目标**：在不调任何 LLM 的前提下，验证缓存的「写后命中」与「唯一键」行为。下面这段**示例代码**直接操作缓存，可作为独立脚本运行。

```python
# 示例代码：仅用于演示缓存行为，非项目原有代码
from babeldoc.translator.cache import TranslationCache

# 引擎名 = "openai"，参数 = 语言对 + 模型
cache = TranslationCache("openai", {"lang_in": "en", "lang_out": "zh", "model": "gpt-4o"})

print("第一次 get:", cache.get("Hello"))      # None（未命中）
cache.set("Hello", "你好")
print("第二次 get:", cache.get("Hello"))      # 你好（命中）

# 换一个模型 → params 变 → 视作不同缓存
cache2 = TranslationCache("openai", {"lang_in": "en", "lang_out": "zh", "model": "gpt-4o-mini"})
print("换模型后 get:", cache2.get("Hello"))   # None（参数不同，未命中）
```

**操作步骤**：

1. 把上面的示例代码存为 `cache_demo.py`，在装好 BabelDOC 的环境里运行 `python cache_demo.py`。
2. 运行后用 sqlite3 查看 `~/.cache/babeldoc/cache.v1.db`：
   ```bash
   sqlite3 ~/.cache/babeldoc/cache.v1.db "SELECT translate_engine, translate_engine_params, original_text, translation FROM _translation_cache;"
   ```

**需要观察的现象**：第二次 `get` 返回 `"你好"`；`cache2` 因模型不同而未命中。数据库里能看到一行，`translate_engine_params` 是排序后的 JSON（键按字母序排列）。

**预期结果**：确认「相同 `(engine, params, text)` 命中、任一不同即未命中」。运行现象待本地验证（取决于本地 SQLite 是否可用，通常可用）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `add_params("model", "gpt-4o")` 改成 `add_params("model", "gpt-4o-mini")`，旧的译文还能被命中吗？为什么？

**参考答案**：不能。`model` 是 `translate_engine_params` 的一部分，模型变了，序列化后的 JSON 字符串就不同，唯一键 `(engine, params, text)` 也不同，于是查不到旧行，视为未命中。这正是「换模型自动失效旧缓存」的设计意图。

**练习 2**：为什么清理逻辑用「按概率 0.1% 触发」而不是「每次写入都检查」？

**参考答案**：每次写入都跑 `SELECT MAX(id)` + `DELETE` 会拖慢高频翻译。0.1% 的概率意味着平均每 1000 次操作才检查一次，既能让缓存大小收敛到 5 万行上限附近，又把清理开销摊薄到几乎可忽略；同时 `_cleanup_lock.acquire(blocking=False)` 保证同一进程不会有多个清理并发执行。

---

### 4.3 漏桶限流：RateLimiter 与 set_translate_rate_limiter

#### 4.3.1 概念说明

`RateLimiter` 用**漏桶算法**控制请求发送速率。它的目标不是「桶满就丢」，而是「强制相邻请求间隔不小于 `1/QPS` 秒」，从而把突发的并发请求**平滑成匀速**。这是保护上游 API、避免触发 429 的本地手段。

BabelDOC 里有一个**全局唯一的**限流器 `_translate_rate_limiter`，所有翻译器实例共享它。CLI 通过 `--qps` 设置它。

#### 4.3.2 核心流程

漏桶的核心数学很简单。设 QPS 为 \(q\)，则相邻请求的最小间隔为：

\[
\Delta t_{\min} = \frac{1}{q}
\]

每次调用 `wait()`：

```
wait():
    加锁
    now = monotonic()                         # 单调时钟，不受系统时间回拨影响
    if next_request_time > now:
        sleep(next_request_time - now)        # 等到允许的时刻
    now = monotonic()                         # 重新取「现在」
    next_request_time = max(next_request_time, now) + min_interval   # 预约下一次
    解锁
```

两个关键设计：

1. **`time.monotonic()` 而非 `time.time()`**：单调时钟不会因为系统校时而回退或跳变，保证等待逻辑稳定。
2. **`max(next_request_time, now)`**：如果限流器空闲了一阵（`next_request_time` 已经过期），下一次请求从「现在」开始算，而不是从「过期的预约时刻」累加——避免空闲后第一次请求被无谓拖延。

`next_request_time` 在 `wait()` 里被「滚动预约」：每次都把下一次允许的时刻往后推一个 `min_interval`，形成匀速节拍。

#### 4.3.3 源码精读

**RateLimiter 类** —— 完整的漏桶实现：

```python
# babeldoc/translator/translator.py:28-69
class RateLimiter:
    def __init__(self, max_qps: int):
        if max_qps <= 0:
            raise ValueError("max_qps must be a positive number")
        self.max_qps = max_qps
        self.min_interval = 1.0 / max_qps
        self.lock = threading.Lock()
        self.next_request_time = time.monotonic()

    def wait(self, _rate_limit_params: dict = None):
        with self.lock:
            now = time.monotonic()
            wait_duration = self.next_request_time - now
            if wait_duration > 0:
                time.sleep(wait_duration)
            now = time.monotonic()
            self.next_request_time = max(self.next_request_time, now) + self.min_interval
```

注意 `wait()` 在**持锁期间**调用 `time.sleep`——这意味着所有线程的请求被串行化在锁上，严格保证间隔。`min_interval = 1.0 / max_qps`，例如 QPS=4 时每次至少间隔 0.25 秒。参考链接：[RateLimiter 漏桶实现：translator.py:28-69](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L28-L69)。

**全局限流器与设置函数**：

```python
# babeldoc/translator/translator.py:72-76
_translate_rate_limiter = RateLimiter(5)              # 模块加载时默认 QPS=5

def set_translate_rate_limiter(max_qps):
    _translate_rate_limiter.set_max_qps(max_qps)
```

`RateLimiter(5)` 是模块级的默认值，但 CLI 启动时会被 `set_translate_rate_limiter(args.qps)` 覆盖（`args.qps` 默认 4，见 [main.py:148-154](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L148-L154)）。`BaseTranslator.translate` 里调用的 `_translate_rate_limiter.wait()`（[translator.py:135](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L135)）引用的就是这个全局对象。参考链接：[全局限流器：translator.py:72-76](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L72-L76)。

**CLI 装配** —— `main.py` 在实例化翻译器后设置限流：

```python
# babeldoc/main.py:541-542
# 设置翻译速率限制
set_translate_rate_limiter(args.qps)
```

这行紧跟在 `OpenAITranslator(...)` 构造之后（[main.py:503-514](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L503-L514)）。注意限流器是**全局单例**，所以即便有「正文翻译器」和「术语抽取翻译器」两个 `OpenAITranslator` 实例（[main.py:503](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L503) 与 [526](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L526)），它们共享同一个 QPS 配额。参考链接：[CLI 设置 QPS：main.py:541-542](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L541-L542)。

#### 4.3.4 代码实践

**实践目标**：直接验证漏桶的匀速效果，**无需任何 LLM**。下面这段**示例代码**只测限流器本身。

```python
# 示例代码：演示漏桶匀速，非项目原有代码
import time
from babeldoc.translator.translator import RateLimiter

rl = RateLimiter(4)            # QPS=4 → min_interval = 0.25s
start = time.monotonic()
for i in range(5):
    rl.wait()
    print(f"第 {i+1} 次 wait 通过，已耗时 {time.monotonic() - start:.3f}s")
```

**操作步骤**：

1. 把示例代码存为 `ratelimit_demo.py` 运行。
2. 修改 `RateLimiter(4)` 为 `RateLimiter(1)`（每秒 1 次），再运行一次。

**需要观察的现象**：

- QPS=4 时，5 次 `wait` 的总耗时约 \(4 \times 0.25 = 1.0\) 秒（第一次立即通过，后续每次间隔 0.25s）。
- QPS=1 时，总耗时约 4 秒。

**预期结果**：相邻两次通过的时间差稳定接近 `min_interval = 1/qps`，验证漏桶把突发请求平滑成匀速。运行现象待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果 `--qps` 设得过大（比如 1000），漏桶还能起保护作用吗？还有什么机制在兜底？

**参考答案**：QPS 过大时 `min_interval` 极小，本地漏桶几乎不阻塞，保护作用减弱。兜底机制是 `do_translate` 上的 `tenacity` 重试装饰器：一旦上游真的返回 `openai.RateLimitError`（429），它会指数退避（1~15s）重试，最多 100 次（[translator.py:259-264](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L259-L264)）。本地限流是「主动节流」，重试是「被动补救」。

**练习 2**：为什么 `wait()` 用 `time.monotonic()` 而不是 `time.time()`？

**参考答案**：`time.time()` 返回的是墙上时钟，可能因为 NTP 校时、夏令时或手动改时间而回退或跳变；若回退，`wait_duration` 会算出负值之外的异常行为，破坏节拍。`time.monotonic()` 单调递增、不受校时影响，是做速率计时的正确选择。

---

### 4.4 token 统计与原子计数

#### 4.4.1 概念说明

翻译是要按 token 计费的，所以 BabelDOC 统计四类 token 计数：

| 字段 | 含义 |
| --- | --- |
| `token_count` | 累计 `total_tokens`（prompt + completion） |
| `prompt_token_count` | 累计输入 prompt token |
| `completion_token_count` | 累计输出 completion token |
| `cache_hit_prompt_token_count` | 累计**上游 prompt 缓存命中**的 token（如 DeepSeek 的上下文缓存） |

注意区分两种「缓存」：本讲的**翻译缓存**（本地 SQLite，整句命中就不调 LLM）与这里的 **prompt 缓存命中**（仍调了 LLM，但上游服务对重复 prompt 给了折扣）。后者通过 OpenAI 响应里的 `prompt_cache_hit_tokens` / `cached_tokens` 字段读取。

由于翻译是多线程并发的，计数必须线程安全，所以用 `AtomicInteger`。

#### 4.4.2 核心流程

```
do_translate / do_llm_translate 调 LLM 得到 response
    ↓
update_token_count(response):
    从 response.usage 读取 total/prompt/completion tokens → AtomicInteger.inc()
    从 response 读取 prompt 缓存命中 token → cache_hit_prompt_token_count.inc()
    （任何异常都吞掉，只 log，不影响翻译主流程）
    ↓
下游模块（如 automatic_term_extractor）通过 getattr(translate_engine, "token_count") 读取 .value
```

#### 4.4.3 源码精读

**AtomicInteger** —— 用锁保护的自增：

```python
# babeldoc/utils/atomic_integer.py:4-26
class AtomicInteger:
    def __init__(self, value=0):
        self._value = int(value)
        self._lock = threading.Lock()

    def inc(self, d=1):
        with self._lock:
            self._value += int(d)
            return self._value
```

`inc` 在锁内完成「读-改-写」，保证多线程下不会丢失更新。参考链接：[AtomicInteger：atomic_integer.py:4-26](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/utils/atomic_integer.py#L4-L26)。

**update_token_count** —— 从 OpenAI 响应抽取四类计数，兼容多种字段名：

```python
# babeldoc/translator/translator.py:339-358
def update_token_count(self, response):
    try:
        if response.usage and response.usage.total_tokens:
            self.token_count.inc(response.usage.total_tokens)
        if response.usage and response.usage.prompt_tokens:
            self.prompt_token_count.inc(response.usage.prompt_tokens)
        if response.usage and response.usage.completion_tokens:
            self.completion_token_count.inc(response.usage.completion_tokens)
        # 兼容 prompt_cache_hit_tokens 与 cached_tokens 两种字段
        hit_count = 0
        if response.usage and hasattr(response.usage, "prompt_cache_hit_tokens"):
            hit_count = getattr(response.usage, "prompt_cache_hit_tokens", 0)
        if hasattr(response, "prompt_tokens_details") and getattr(
            response.prompt_tokens_details, "cached_tokens", 0
        ):
            hit_count += getattr(response.prompt_tokens_details, "cached_tokens", 0)
        if hit_count:
            self.cache_hit_prompt_token_count.inc(hit_count)
    except Exception as e:
        logger.exception("Error updating token count")
```

关键点：整个统计被 `try/except` 包住，**统计失败绝不影响翻译结果**（最坏只是少统计，不能让翻译崩）。它同时兼容了 DeepSeek 风格的 `prompt_cache_hit_tokens` 和 OpenAI 风格的 `prompt_tokens_details.cached_tokens`。参考链接：[update_token_count：translator.py:339-358](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L339-L358)。

**下游如何读取** —— 自动术语抽取模块用 `getattr` 安全地读取计数：

```python
# babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py:163-171（节选）
token_counter = getattr(self.translate_engine, "token_count", None)
prompt_counter = getattr(self.translate_engine, "prompt_token_count", None)
...
total_tokens = token_counter.value if token_counter else 0
```

用 `getattr(..., None)` 是因为 `translate_engine` 可能是任意 `BaseTranslator` 子类，未必都有这些计数器；`.value` 属性（[atomic_integer.py:17-20](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/utils/atomic_integer.py#L17-L20)）在锁内读取。这些计数最终汇入 `TranslationConfig.total_valid_text_token_count` 并在翻译结束时打印（[translation_config.py:590-595](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/translation_config.py#L590-L595)）。参考链接：[下游读取计数：automatic_term_extractor.py:163-171](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py#L163-L171)。

#### 4.4.4 代码实践

**实践目标**：验证 `AtomicInteger` 在多线程下的累加正确性。下面这段**示例代码**对照「普通 int」与「AtomicInteger」。

```python
# 示例代码：演示原子计数，非项目原有代码
import threading
from babeldoc.utils.atomic_integer import AtomicInteger

N_THREADS, N_EACH = 100, 1000

# 1) 普通 int：会丢更新
plain = [0]
def bump_plain():
    for _ in range(N_EACH):
        plain[0] += 1

# 2) AtomicInteger：线程安全
ai = AtomicInteger()
def bump_ai():
    for _ in range(N_EACH):
        ai.inc()

ts = [threading.Thread(target=bump_plain) for _ in range(N_THREADS)]
[t.start() for t in ts]; [t.join() for t in ts]
ts2 = [threading.Thread(target=bump_ai) for _ in range(N_THREADS)]
[t.start() for t in ts2]; [t.join() for t in ts2]

print("plain int:", plain[0], "(期望", N_THREADS * N_EACH, "，通常更小)")
print("AtomicInteger:", ai.value, "(期望", N_THREADS * N_EACH, ")")
```

**操作步骤**：

1. 存为 `atomic_demo.py` 运行（可能需多次运行以观察到 plain int 的丢更新）。

**需要观察的现象**：`AtomicInteger` 的值始终精确等于 `N_THREADS * N_EACH`；普通 int 通常小于该值（因为有竞态）。

**预期结果**：直观感受「为什么 token 计数必须用原子操作」。运行现象待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`update_token_count` 用 `try/except` 包住整个统计逻辑，这样做到底好不好？

**参考答案**：在「统计」这种旁路逻辑上是合理的：统计的目的是记账，绝不能因为某个响应字段缺失或格式异常而让一次成功的翻译抛错、让整篇 PDF 翻译失败。代价是统计可能偶尔不准，但相比「翻译崩溃」这个代价小得多。这是「核心路径 vs 旁路路径」容错策略的典型取舍。

**练习 2**：`cache_hit_prompt_token_count` 统计的「prompt 缓存命中」和本讲的「翻译缓存」是同一回事吗？

**参考答案**：不是。翻译缓存是 BabelDOC **本地**的 SQLite 缓存，命中后**完全不调 LLM**；而 prompt 缓存命中指的是**上游 LLM 服务**（如 DeepSeek、OpenAI）对重复的前缀 prompt 给的计费折扣，此时**仍然调用了 LLM**，只是便宜。前者省掉整次调用，后者只省钱不省调用。

---

## 5. 综合实践

把本讲四个模块串起来：写一个脚本，**实例化一个真正的 `OpenAITranslator`**，翻译同一句话两次，观察缓存命中与限流节流，并读取 token 计数。

```python
# 示例代码：综合实践，需 OpenAI 兼容服务，非项目原有代码
from babeldoc.translator.translator import OpenAITranslator, set_translate_rate_limiter

# 1) 准备一个 OpenAI 兼容服务（替换为你的 base_url / api_key / model）
t = OpenAITranslator(
    lang_in="English", lang_out="Chinese",
    model="gpt-4o-mini",                       # 换成你可用的模型
    base_url="https://api.openai.com/v1",      # 换成你的服务地址
    api_key="sk-xxxx",                         # 换成你的 key
)

# 2) 设置一个较小的 QPS，便于观察节流
set_translate_rate_limiter(2)                  # 每秒 2 次

# 3) 第一次翻译（未命中缓存 → 调 LLM → 写缓存）
print("第一次:", t.translate("The quick brown fox jumps over the lazy dog."))
print("  token_count =", t.token_count.value)

# 4) 第二次翻译（命中缓存 → 直接返回，不调 LLM、不耗 token）
print("第二次:", t.translate("The quick brown fox jumps over the lazy dog."))
print("  token_count =", t.token_count.value, "（应与第一次相同，未新增）")

# 5) 析构时会打印 translate call count 与 cache call count
```

**操作步骤**：

1. 准备一个可用的 OpenAI 兼容服务（官方 OpenAI、DeepSeek 或本地 Ollama 均可）。
2. 替换脚本里的 `base_url` / `api_key` / `model`。
3. 运行脚本，记录两次翻译的耗时与 `token_count`。
4. 翻译完成后查看本地缓存库：
   ```bash
   sqlite3 ~/.cache/babeldoc/cache.v1.db \
     "SELECT original_text, translation FROM _translation_cache WHERE original_text LIKE '%quick brown%';"
   ```
5. 重新运行脚本（不删缓存库），观察第一次翻译是否**因为命中旧缓存而不再调用 LLM**（`token_count` 不增长）。

**需要观察的现象**：

- 首次运行：第一次翻译有网络往返（耗时长、`token_count` 增长），第二次几乎瞬时（命中缓存、`token_count` 不变）。
- 二次运行：连第一次翻译都命中了上次写入的缓存，全程不调 LLM。
- 数据库里能查到那条「原文 → 译文」记录，`translate_engine_params` 是排序后的 JSON。

**预期结果**：贯通「服务调用 → 缓存命中 → 限流节流 → token 计数」四件事，并亲眼看到 `cache.v1.db` 里的内容。运行现象待本地验证（需可用 API key）。

> 若暂时没有可用的 LLM 服务，可退化为「源码阅读型实践」：对照 [translator.py:120-139](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/translator/translator.py#L120-L139)，在纸上推导两次 `translate("Hello")` 分别走过哪些函数、`token_count` 与 `translate_cache_call_count` 各如何变化。

## 6. 本讲小结

- `BaseTranslator` 是「模板方法」基类：对外 `translate()` 统一负责「查缓存 → 限流 → 翻译 → 写缓存」，子类（`OpenAITranslator`）只需实现 `do_translate()` 调真实 LLM；缓存命中时**既不调 LLM 也不耗限流配额**。
- `OpenAITranslator` 固定 `temperature=0`（避免随机采样破坏公式标记与缓存一致性），用 `tenacity` 对 `openai.RateLimitError` 指数退避重试，并把 `model`/`prompt`/`temperature` 等登记进缓存键以实现「换配置自动失效旧缓存」。
- 翻译缓存用 peewee + SQLite 落盘到 `~/.cache/babeldoc/cache.v1.db`，唯一键为 `(engine, params, text)` 且 `ON CONFLICT REPLACE`；参数经 `_sort_dict_recursively` 递归排序保证键稳定；按 0.1% 概率触发清理、始终保留最新 5 万行；「database is locked」时静默降级为未命中。
- 漏桶限流 `RateLimiter` 用 `time.monotonic()` 与 `max(next, now) + min_interval` 把并发请求平滑成匀速，全局单例 `_translate_rate_limiter` 被 `set_translate_rate_limiter(--qps)` 设置，所有翻译器实例共享同一 QPS 配额。
- token 统计用 `AtomicInteger` 在多线程下安全累加四类计数（total/prompt/completion/prompt 缓存命中），`update_token_count` 整体被 `try/except` 包住以保证「统计失败不影响翻译」，下游（如自动术语抽取）用 `getattr` 安全读取。

## 7. 下一步学习建议

本讲只解决了「**一句话**怎么翻译、缓存、限流、计数」。下一篇 **u6-l2《IL 翻译编排：占位符、批处理与线程池》** 将进入**整篇 PDF** 的层面：

- `ILTranslator` 如何用 `PriorityThreadPoolExecutor` 并发调用本讲的 `translate()`，并优先翻译短段落。
- 公式占位符 `{v1}` 与富文本占位符 `<style id='1'>`（即本讲 `get_formular_placeholder` / `get_rich_text_left_placeholder` 的产物）如何在 pre/post translate 中构造与还原。
- 如何按 token 数与段落数切批，把成百上千段塞进本讲的限流与缓存机制。

建议在进入 u6-l2 前，先动手跑一遍本讲的「综合实践」，亲眼看到 `cache.v1.db` 里出现一行记录、第二次翻译命中缓存——这会让后续「并发批量翻译如何复用同一份缓存」变得直观。

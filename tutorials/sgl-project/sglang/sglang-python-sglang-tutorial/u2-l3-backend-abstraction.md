# 后端抽象：RuntimeEndpoint 与第三方后端

> 本讲是第 2 单元（前端语言层 `lang/`）第 3 篇，承接 u2-l2。
> u2-l2 讲清了「解释器 `StreamExecutor` 执行到 `SglGen` / `SglSelect` 时，会调用 `self.backend.generate(...)` / `self.backend.select(...)`」。
> 本讲要回答下一个自然问题：**`self.backend` 到底是什么对象？这一层统一接口长什么样？自研运行时和 OpenAI / Anthropic 等第三方 API 是如何被同一套前端复用的？**

## 1. 本讲目标

学完本讲，你应当能够：

- 用一句话说清 `BaseBackend` 这个抽象类为前端定义了哪些「必须实现」与「可选实现」的方法，以及为什么前端只认接口、不关心底层是 HTTP 服务还是第三方 SDK。
- 区分 `RuntimeEndpoint`（一个 **`BaseBackend` 子类**，用 REST 调用正在运行的 sglang HTTP 服务）与 `Runtime`（一个**服务启动器**，内部 spawn 一个 HTTP 子进程并包住一个 `RuntimeEndpoint`）。
- 看懂 `RuntimeEndpoint.generate` 如何把 `s.text_` + 采样参数组装成 `/generate` 请求体并解析返回；以及 `OpenAI.generate` 如何走 OpenAI SDK，并理解两者在前端层的「行为差异」。
- 解释 `set_default_backend` 的作用、`global_config.default_backend` 的兜底语义，以及为什么 `run / run_batch / trace` 在没传 `backend` 时都回退到它。
- 亲手写一段 `@function`，先用 `RuntimeEndpoint` 连本地 sglang 服务跑一遍，再用 `OpenAI` 后端连同一个服务跑一遍，对比差异。

## 2. 前置知识

本讲假设你已经了解（来自 u2-l1 / u2-l2 / u1-l4）：

- **SglExpr 与惰性求值**：`gen` / `select` 返回的是表达式对象，真正执行发生在解释器阶段（u2-l1）。
- **解释器 `StreamExecutor`**：它持有一个 `backend`，执行到 `SglGen` 调 `backend.generate`、执行到 `SglSelect` 调 `backend.select`，结果写回 `variables`（u2-l2）。
- **`StreamExecutor` 与 `s` 的关系**：执行期的 `s`（`ProgramState`）包住 `StreamExecutor`，`backend.generate(s, ...)` 里的 `s` 就是这个执行器，后端通过 `s.text_` 读到当前已拼接的文本。
- **`sglang.Engine` 与 HTTP 服务**：运行时引擎既可作为同进程入口（`sglang.Engine`），也可起一个 HTTP 服务（`sglang serve`）对外暴露 `/generate` 等接口（u1-l2 / u1-l4）。

下面用三个类比建立直觉：

| 概念 | 直觉类比 | 关键点 |
| --- | --- | --- |
| `BaseBackend` | 一份「**插座标准**」 | 定义了插孔形状（方法签名），不关心背后是哪家发电厂 |
| `RuntimeEndpoint` | 一根「**接到自家发电厂的电线**」 | 走 REST，把请求送到正在运行的 sglang HTTP 服务 |
| `OpenAI` / `Anthropic` / `LiteLLM` | 「**接外网的适配器**」 | 走各家 SDK，把前端的统一调用翻译成各家 API |

核心设计思想是**依赖倒置**：前端的追踪器、解释器只依赖 `BaseBackend` 这个抽象接口，而不依赖任何具体后端。于是「换一个推理提供方」只需要换一个实现了 `BaseBackend` 的类，前端代码一行都不用改。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `python/sglang/lang/` 下：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [lang/backend/base_backend.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/base_backend.py) | **后端抽象基类** | `BaseBackend`、`generate` / `generate_stream` / `select` 等接口签名与默认实现 |
| [lang/backend/runtime_endpoint.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py) | **自研运行时后端** | `RuntimeEndpoint`（`BaseBackend` 子类）、`Runtime`（HTTP 服务启动器） |
| [lang/backend/openai.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/openai.py) | **第三方后端示例** | `OpenAI`（`BaseBackend` 子类）、`openai_completion` 重试包装 |
| [lang/backend/anthropic.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/anthropic.py) | **第三方后端示例** | `Anthropic`，结构对照 `OpenAI` |
| [lang/api.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py) | **公共 API** | `set_default_backend`、`Runtime` / `Engine` 工厂 |
| [lang/ir.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py) | **采样参数翻译** | `SglSamplingParams.to_srt_kwargs` / `to_openai_kwargs` / `to_anthropic_kwargs` |
| [global_config.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/global_config.py) | **全局配置** | `default_backend` 字段 |

一句话总览：`base_backend.py` 定义插座标准，`runtime_endpoint.py` / `openai.py` / `anthropic.py` 是不同插头，`api.py::set_default_backend` 决定当前用哪个插头，`ir.py` 负责把统一的采样参数翻译成各插头听得懂的方言。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 `BaseBackend` 接口**——统一后端契约，前端只认这一层。
- **4.2 `RuntimeEndpoint`**——把前端请求转发到自研运行时的 REST 后端。
- **4.3 `OpenAI` 后端**——对接第三方 API 的代表，对照理解「行为差异」。
- **4.4 `set_default_backend`**——切换默认后端的开关与兜底语义。

### 4.1 BaseBackend 接口：统一后端契约

#### 4.1.1 概念说明

`BaseBackend` 是所有后端的抽象基类。它定义了一套方法签名，约定「一个合格的后端必须能做什么」。前端解释器（`StreamExecutor`）在运行时只持有某个 `BaseBackend` 子类的实例，调用它的 `generate` / `select`，从不关心底层是 HTTP REST、OpenAI SDK 还是 Anthropic SDK。

这套抽象解决的核心问题是：**让同一份前端 DSL 程序，能在不同推理提供方之间无缝切换**。你今天用自研 sglang 服务，明天想对比 OpenAI 的 GPT，只需要 `set_default_backend` 换一个对象，`@function` 里写的 `gen` / `select` 一行都不用动。

`BaseBackend` 里的方法分两类：

- **必须实现的核心方法**：`generate` / `generate_stream` / `select`——基类里直接 `raise NotImplementedError()`，逼着子类去实现。
- **可选钩子方法**：`cache_prefix` / `begin_program` / `end_program` / `fork_program` / `fill_image` / `shutdown` / `flush_cache` / `get_server_info` 等——基类里给空实现（`pass`），子类按需覆写。

#### 4.1.2 核心流程

一个后端对象在前端里的生命周期：

```
1. 构造：RuntimeEndpoint(url) / OpenAI(model_name) / ...
   - super().__init__() 设置 support_concate_and_append、默认 chat_template
2. 注册：set_default_backend(backend) → 写进 global_config.default_backend
3. 运行：SglFunction.run() → run_program(...) → StreamExecutor(backend, ...)
4. 执行期被解释器反复调用：
   - 遇 SglGen    → backend.generate(s, sampling_params)        返回 (comp, meta_info)
   - 遇流式 SglGen → backend.generate_stream(s, sampling_params)  返回生成器
   - 遇 SglSelect → backend.select(s, choices, temperature, method) 返回 ChoicesDecision
   - 程序开始/结束 → backend.begin_program(s) / backend.end_program(s)
   - 提前缓存前缀 → backend.cache_prefix(prefix_str)
5. 收尾：backend.shutdown()
```

三个核心方法的返回值约定很关键，前端解释器依赖它们：

| 方法 | 入参 | 返回值 | 前端如何使用 |
| --- | --- | --- | --- |
| `generate` | `(s, sampling_params)` | `(comp, meta_info)`：comp 是字符串（或字符串列表，多路 `n`） | 写进 `variables[name]`，拼到 `s.text_` |
| `generate_stream` | `(s, sampling_params)` | 一个生成器，逐个 `yield (chunk, meta_info)` | 解释器循环 `for comp, meta_info in generator` 边收边拼 |
| `select` | `(s, choices, temperature, choices_method)` | `ChoicesDecision`（含 `.decision` 与 `.meta_info`） | `.decision` 写进 `variables[name]` |

#### 4.1.3 源码精读

**`BaseBackend` 类与构造**（[lang/backend/base_backend.py:L9-L13](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/base_backend.py#L9-L13)）：

```python
class BaseBackend:
    def __init__(self) -> None:
        self.support_concate_and_append = False
        self.chat_template = get_chat_template("default")
```

构造里设了两个默认属性。`support_concate_and_append` 是个能力开关：默认 `False`，只有 `RuntimeEndpoint` 把它设成 `True`（见 4.2），表示「这个后端支持把多个已完成请求的 KV 拼接到另一个请求上」——这是自研运行时才有的能力，第三方 API 做不到。`chat_template` 默认用 `"default"` 模板，子类通常按模型名重新解析（见 4.2/4.3）。

**三个必须实现的核心方法**（[lang/backend/base_backend.py:L49-L70](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/base_backend.py#L49-L70)）：

```python
def generate(self, s: StreamExecutor, sampling_params: SglSamplingParams):
    raise NotImplementedError()

def generate_stream(self, s: StreamExecutor, sampling_params: SglSamplingParams):
    raise NotImplementedError()

def select(self, s, choices, temperature, choices_method=None) -> ChoicesDecision:
    raise NotImplementedError()
```

注意三个签名都把 `s: StreamExecutor` 作为第一个参数——后端通过它读到当前已累积的文本 `s.text_`、多模态输入 `s.images_`、对话历史 `s.messages_` 等。这套「把整个执行器传进来」的约定，是前端与后端之间的边界。

**可选钩子默认空实现**（[lang/backend/base_backend.py:L20-L36](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/base_backend.py#L20-L36)）：`cache_prefix` / `uncache_prefix` / `end_request` / `begin_program` / `end_program` / `commit_lazy_operations` / `fork_program` / `fill_image` 全是 `pass`。这意味着第三方后端（如 `OpenAI`）可以「不实现」前缀缓存、程序分叉等高级特性，基类的空实现保证前端调用它们时不会报错——只是什么都不做。这是一个典型的「**接口提供默认退化行为**」设计：能力弱的后端不会被高级特性卡住。

**与解释器的对接点**（[lang/interpreter.py:L598-L601](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L598-L601) 与 [lang/interpreter.py:L648-L650](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L648-L650)）：

```python
# _execute_gen 里：
comp, meta_info = self.backend.generate(self, sampling_params=sampling_params)
# _execute_select 里：
choices_decision = self.backend.select(self, expr.choices, expr.temperature, expr.choices_method)
```

这就是 u2-l2 里反复出现的 `backend.generate` / `backend.select`——本讲正是在解释这行代码背后的对象。注意 `self.backend` 的类型在前端代码里是 `BaseBackend`（鸭子类型），实际运行时是它的某个子类实例。

#### 4.1.4 代码实践（源码阅读型，无需 GPU）

**目标**：用一张表把 `BaseBackend` 的方法分成「必须实现」与「可选钩子」两类，加深对契约的理解。

**步骤**：

1. 打开 [lang/backend/base_backend.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/base_backend.py)，逐个方法看函数体。
2. 函数体是 `raise NotImplementedError()` 的归入「必须实现」，是 `pass` 或 `return self.xxx` 的归入「可选钩子」。

**需要观察的现象**：你会得到一张约十几个方法的分类表。

**预期结果**：`get_model_name` / `generate` / `generate_stream` / `select` / `concatenate_and_append` 是「抛异常」的必须项；其余（`cache_prefix` / `begin_program` / `end_program` / `fork_program` / `fill_image` / `shutdown` / `flush_cache` / `get_server_info` 等）是「空实现」的可选项。这张表就是判断「新写一个后端最少要实现什么」的依据。

#### 4.1.5 小练习与答案

**练习 1**：基类为什么把 `cache_prefix` 写成 `pass` 而不是 `raise NotImplementedError()`？

> **答案**：因为前缀缓存是**自研运行时才有的能力**（依赖底层 RadixCache，见第 6 单元），第三方 API（OpenAI 等）根本没有这个能力。如果基类强行 `raise NotImplementedError()`，那么任何第三方后端都会在前端批处理触发 `cache_prefix` 时崩溃。写成 `pass` 等于「我尽力了，做不到就算了」，让弱后端能优雅退化。`begin_program` / `end_program` / `fork_program` 同理。

**练习 2**：`BaseBackend.__init__` 里的 `self.support_concate_and_append = False` 这个默认值，说明了什么设计意图？

> **答案**：它把 `concatenate_and_append`（把多个请求的 KV 拼到另一个请求）默认标记为「不支持」。只有自研的 `RuntimeEndpoint` 把它改成 `True`（[lang/backend/runtime_endpoint.py:L35](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L35)）。前端在用到这个能力前会先检查该标志，从而对「能力不足」的后端跳过相关优化。这是「用属性声明能力、由调用方按能力分支」的常见模式。

### 4.2 RuntimeEndpoint：把请求转发到自研运行时

#### 4.2.1 概念说明

`RuntimeEndpoint` 是 SGLang 自带的、最重要的后端实现。它是一个 `BaseBackend` 子类，其本质是**一个 REST 客户端**：它持有某个 sglang HTTP 服务的 `base_url`，每次 `generate` / `select` 都是对该服务的 `/generate` 等 HTTP 接口发请求、解析 JSON 返回。

务必区分两个名字相近的类（这是本讲最容易混淆的点）：

| 类 | 是否 `BaseBackend` 子类 | 职责 | 典型用法 |
| --- | --- | --- | --- |
| `RuntimeEndpoint` | **是** | 对**已经在运行**的 sglang HTTP 服务发 REST 请求 | 先 `sglang serve`，再 `set_default_backend(RuntimeEndpoint(url))` |
| `Runtime` | **否**（独立类） | **启动**一个 HTTP 子进程，并内部包一个 `RuntimeEndpoint` | 在 Python 程序里一键起服务：`Runtime(model_path=...)` |

换句话说：`RuntimeEndpoint` 是「插头」，`Runtime` 是「插头 + 自带发电厂」。`Runtime` 不是后端，它是一个服务启动器，有自己的 `generate` / `async_generate`（直接 REST 调用，签名与 `BaseBackend` 不同），用于不走 DSL 的简单场景；而真正作为前端后端使用的，是它内部那个 `RuntimeEndpoint`。

#### 4.2.2 核心流程

`RuntimeEndpoint.generate` 的一次调用流程：

```
generate(s, sampling_params):
  1. _handle_dtype_to_regex(sampling_params)   # 若设了 dtype(int/float/str/bool)，翻译成正则+stop
  2. 组装 data = {
       "text": s.text_,                         # 当前已累积的完整 prompt
       "sampling_params": {
         skip_special_tokens / spaces_between_special_tokens (来自 global_config),
         **sampling_params.to_srt_kwargs(),     # 统一采样参数翻译成运行时格式
       },
       # 可选：return_logprob / logprob_start_len / top_logprobs_num / return_text_in_logprobs
     }
  3. _add_images(s, data)   # 若有图像，塞 image_data（当前仅支持单图）
  4. http_request(POST base_url + "/generate", json=data)
  5. _assert_success(res)   # 非 200 抛 RuntimeError
  6. return res.json()["text"], res.json()["meta_info"]
```

而 `Runtime`（启动器）的构造流程是另一条线：

```
Runtime(model_path=..., **kwargs):
  1. ServerArgs(*args, log_level=..., **kwargs)        # 解析服务参数
  2. 预分配一个可用端口
  3. multiprocessing.spawn → 子进程跑 launch_server    # 拉起 HTTP 服务
  4. 轮询 GET /health_generate 直到 200（或超时）
  5. self.endpoint = RuntimeEndpoint(self.url)         # 包一个后端插头
  6. atexit.register(self.shutdown)                    # 程序退出时自动关子进程
```

#### 4.2.3 源码精读

**`RuntimeEndpoint.__init__`：连服务、取模型信息、定 chat 模板**（[lang/backend/runtime_endpoint.py:L26-L54](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L26-L54)）：

```python
class RuntimeEndpoint(BaseBackend):
    def __init__(self, base_url, api_key=None, verify=None, chat_template_name=None):
        super().__init__()
        self.support_concate_and_append = True              # 自研后端才有的能力

        self.base_url = base_url
        self.api_key = api_key
        self.verify = verify

        res = http_request(self.base_url + "/get_model_info", ...)  # 构造时就探活
        self._assert_success(res)
        self.model_info = res.json()

        if chat_template_name:
            self.chat_template = get_chat_template(chat_template_name)
        else:
            self.chat_template = get_chat_template_by_model_path(self.model_info["model_path"])
```

三个关键点：(1) 构造时**立刻**调 `/get_model_info` 探活——服务没起或地址错，会在 `RuntimeEndpoint(...)` 这一步就抛错，而不是等到第一次 `generate` 才失败；(2) 把 `support_concate_and_append` 置 `True`，声明自研后端的额外能力；(3) chat 模板优先用显式给的 `chat_template_name`，否则按服务返回的 `model_path` 自动推断，保证前端渲染对话时用的是正确的模板。

**`generate`：组装请求体并解析返回**（[lang/backend/runtime_endpoint.py:L159-L196](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L159-L196)）：

```python
def generate(self, s, sampling_params):
    self._handle_dtype_to_regex(sampling_params)
    data = {
        "text": s.text_,
        "sampling_params": {
            "skip_special_tokens": global_config.skip_special_tokens_in_output,
            "spaces_between_special_tokens": global_config.spaces_between_special_tokens_in_out,
            **sampling_params.to_srt_kwargs(),
        },
    }
    # 可选 logprob 相关字段……
    self._add_images(s, data)
    res = http_request(self.base_url + "/generate", json=data, ...)
    self._assert_success(res)
    obj = res.json()
    return obj["text"], obj["meta_info"]
```

注意 `sampling_params.to_srt_kwargs()`（[lang/ir.py:L121](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L121)）：前端统一的 `SglSamplingParams` 在这里被翻译成运行时 `/generate` 接口认识的字段名。这正是「统一接口 + 各后端方言翻译」的落点。返回的 `obj["text"]` 是补全文本、`obj["meta_info"]` 携带 prompt_tokens 等元信息，前者写进 `variables`、后者写进 `meta_info`。

**`_assert_success`：统一错误处理**（[lang/backend/runtime_endpoint.py:L342-L348](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L342-L348)）：状态码非 200 时，尝试把响应体当 JSON 解析（失败则取文本），再包成 `RuntimeError` 抛出。这样服务端报的错能比较可读地冒到前端。

**`select`：用 logprob 给候选项打分**（[lang/backend/runtime_endpoint.py:L248-L315](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L248-L315)）：这是 `RuntimeEndpoint` 相对第三方后端最大的优势——它能拿到**真正的 token 对数概率**。流程是：先发一次 `max_new_tokens=0` 的请求缓存公共前缀并拿到 `prompt_tokens`，再对每个候选 `s.text_ + choice` 请求 `return_logprob=True`，取 `input_token_logprobs` 算归一化平均对数概率：

\[
\bar{\ell}(c) \;=\; \frac{1}{|c|}\sum_{t} \log p\!\left(\text{token}_{t}^{(c)} \,\big|\, \text{context}\right)
\]

其计算函数见 `compute_normalized_prompt_logprobs`（[lang/backend/runtime_endpoint.py:L351-L353](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L351-L353)），就是「取所有非空 logprob 的平均」。最后把这套数交给 `choices_method`（如 u2-l4 会讲的 `token_length_normalized`）裁定胜者。代码里还处理了 token healing（去掉多算的一个 token）和无条件概率（某些 `choices_method` 需要）。

**`Runtime`：启动器而非后端**（[lang/backend/runtime_endpoint.py:L356-L434](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L356-L434)）：注意类定义 `class Runtime:` **没有继承** `BaseBackend`。它在构造里 spawn 子进程跑 `launch_server`（[L401-L407](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L401-L407)），轮询 `/health_generate` 等服务就绪（[L413-L432](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L413-L432)），就绪后 `self.endpoint = RuntimeEndpoint(self.url)`（[L434](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L434)）。`Runtime` 自己的 `generate`（[L503-L527](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L503-L527)）签名是 `(prompt, sampling_params, ...)`，与 `BaseBackend.generate(s, ...)` 不同——它是给「不走 DSL、直接发 prompt」的用户用的便捷方法。如果你想把 DSL 后端指向它，应该取 `runtime.endpoint`（即那个 `RuntimeEndpoint`），而不是 `runtime` 本身。

#### 4.2.4 代码实践（本地起服务型）

**目标**：确认「先起服务、再 `RuntimeEndpoint` 连接」的典型用法，并理解 `RuntimeEndpoint` 构造即探活。

**步骤**：

1. 参考真实用法（[test/few_shot_gsm8k.py:L66](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/test/few_shot_gsm8k.py#L66)）：先用一个小模型起服务（具体启动方式见 u1-l2），例如 `sglang serve --model-path Qwen/Qwen2.5-0.5B --port 30000`。
2. 编写如下示例（本地示例文件）：

```python
import sglang as sgl
from sglang.lang.backend.runtime_endpoint import RuntimeEndpoint

# 构造时就会 GET /get_model_info 探活；服务没起会在这里报错
sgl.set_default_backend(RuntimeEndpoint("http://localhost:30000"))

@sgl.function
def hello(s):
    s += "Say hello in one short sentence:" + sgl.gen("greeting", max_new_tokens=32, stop="\n")

ret = hello.run()
print(ret["greeting"])
```

3. 运行前先确认服务已就绪（`curl http://localhost:30000/get_model_info` 应返回 JSON）。

**需要观察的现象**：若服务未启动，`RuntimeEndpoint(...)` 这一行（而非 `hello.run()`）就会抛 `RuntimeError`，提示连接失败——印证「构造即探活」。

**预期结果**：服务在线时，`ret["greeting"]` 打印出一句模型生成的招呼。

**若本地无 GPU / 无法起服务**：则把启动与运行标注为「待本地验证」，但你可以只阅读 4.2.3 的源码，理解 `/get_model_info`、`/generate`、`/health_generate` 这几个 HTTP 端点在前端后端里的角色。

#### 4.2.5 小练习与答案

**练习 1**：`RuntimeEndpoint` 和 `Runtime` 哪个是 `BaseBackend` 的子类？为什么这样设计？

> **答案**：`RuntimeEndpoint` 是，`Runtime` 不是。`RuntimeEndpoint` 代表「一个可被解释器调用的后端」（实现了 `generate` / `select` 等同构签名），所以必须实现 `BaseBackend` 契约；`Runtime` 的职责是「启动并管理一个 HTTP 子进程」，它有自己的、签名不同的 `generate`（直接吃 `prompt` 字符串），不属于后端契约。把两者拆开，避免了「服务启动逻辑」与「后端调用逻辑」耦合在一个类里。

**练习 2**：为什么 `RuntimeEndpoint.select` 能做候选项打分，而 `OpenAI.select` 做得很勉强（见 4.3）？

> **答案**：因为 `RuntimeEndpoint` 调的是自研运行时，可以请求 `return_logprob=True` 直接拿到每个 token 的真实对数概率（[lang/backend/runtime_endpoint.py:L276-L281](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L276-L281)），从而精确算归一化似然。而 OpenAI 等第三方 API 不暴露每个 token 的 logprob，只能用 `logit_bias` 逐 token 逼模型选（见 4.3），既慢又不精确。这是自研后端相对第三方的核心能力差距。

### 4.3 OpenAI 后端：对接第三方 API

#### 4.3.1 概念说明

`OpenAI` 后端（`BaseBackend` 子类）让前端的 `@function` 可以直接调 OpenAI 的 GPT 系列模型。它的实现思路与 `RuntimeEndpoint` 形成鲜明对照：

- `RuntimeEndpoint` 走 REST，调自研 `/generate`，拿得到 token 级 logprob，能力完整。
- `OpenAI` 走 OpenAI Python SDK（`client.chat.completions.create` / `client.completions.create`），拿不到 token 级 logprob，`select` 只能用 `logit_bias` 逐 token 逼答案。

`OpenAI` 后端还引入了几个第三方特有问题：(1) **chat 模型 vs completion 模型** 的区分（`is_chat_model`）；(2) **API 投机执行**（`num_api_spec_tokens`）——为 chat 模型模拟「一次生成多 token 再切片」的行为；(3) **token 用量统计**（`TokenUsage`）；(4) **重试**（`openai_completion` 里的限流/连接错误重试）。

`Anthropic`（[lang/backend/anthropic.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/anthropic.py)）和 `LiteLLM`（[lang/backend/litellm.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/litellm.py)）结构与之同构，只是换成各家 SDK 与各自的参数翻译方法（`to_anthropic_kwargs` / litellm 自身的参数）。

#### 4.3.2 核心流程

`OpenAI.generate` 的简化决策流程：

```
generate(s, sampling_params):
  if sampling_params.dtype is None:          # 普通生成
      if is_chat_model:
          if 未开 api 投机 且 s.text_ 不以 assistant 前缀结尾:
              raise RuntimeError("sgl.gen must be right after sgl.assistant")
          prompt = s.messages_                # chat 模型用消息列表
      else:
          prompt = s.text_                    # completion 模型用纯文本
      kwargs = sampling_params.to_openai_kwargs()
      comp = openai_completion(client, prompt=prompt, **kwargs)   # 带重试
  elif dtype in [str/int/...]:                # 约束类型（仅 completion 模型）
      用 logit_bias / stop 强约束
  return comp, {}
```

注意 `generate` 返回的 `meta_info` 是空字典 `{}`——因为 OpenAI 不像运行时那样返回丰富的 `meta_info`，只单独累计进 `self.token_usage`。

#### 4.3.3 源码精读

**`OpenAI.__init__`：建客户端、定 tokenizer、判 chat/completion**（[lang/backend/openai.py:L56-L98](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/openai.py#L56-L98)）：

```python
class OpenAI(BaseBackend):
    def __init__(self, model_name, is_chat_model=None, chat_template=None, is_azure=False, *args, **kwargs):
        super().__init__()
        if isinstance(openai, Exception):
            raise openai                                # openai 包没装时抛 ImportError
        if is_azure:
            self.client = openai.AzureOpenAI(*args, **kwargs)
        else:
            self.client = openai.OpenAI(*args, **kwargs)
        self.model_name = model_name
        self.tokenizer = tiktoken.encoding_for_model(model_name)  # 或回退 cl100k_base
        self.logit_bias_int = create_logit_bias_int(self.tokenizer)
        ...
        if is_chat_model is not None:
            self.is_chat_model = is_chat_model
        else:
            self.is_chat_model = model_name not in INSTRUCT_MODEL_NAMES   # 默认当 chat 模型
```

三个要点：(1) `openai` / `tiktoken` 是**可选依赖**，用 `try/except ImportError` 包住（[L15-L19](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/openai.py#L15-L19)），没装时 `openai` 被赋成那个异常对象，构造时再 `raise` 出来——这样不装 OpenAI 包也能用自研后端；(2) 用 `tiktoken` 在本地做 tokenizer，是为了算 `logit_bias_int`（约束整数生成）和 `select` 的逐 token 比较；(3) `is_chat_model` 默认为 `True`，只有少数 instruct 模型（如 `gpt-3.5-turbo-instruct`，[L42-L44](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/openai.py#L42-L44)）走 completion 接口。

**`generate`：chat/completion 分流与 `to_openai_kwargs`**（[lang/backend/openai.py:L140-L180](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/openai.py#L140-L180)）：核心是把统一的 `SglSamplingParams` 用 `to_openai_kwargs()`（[lang/ir.py:L64](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L64)）翻成 OpenAI 认识的 `max_tokens` / `temperature` / `top_p` / `stop` 等，再交给 `openai_completion`。注意 chat 模型要求 `sgl.gen` 必须紧跟 `sgl.assistant`（否则抛错，[L150-L154](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/openai.py#L150-L154)），这是因为 chat 模型只能「在 assistant 回复位」上生成，不能在任意文本中续写——这是第三方 API 相对自研运行时的一个硬约束。

**`select`：chat 模型直接不支持，completion 模型逐 token 逼**（[lang/backend/openai.py:L312-L380](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/openai.py#L312-L380)）：

```python
def select(self, s, choices, temperature, choices_method):
    """Note: choices_method is not used by the OpenAI backend."""
    if self.is_chat_model:
        raise NotImplementedError("select/choices is not supported for chat models. ...")
    # 逐 token：对每个候选的当前 token 设 logit_bias=100，调一次 max_tokens=1
    ...
```

这段清楚体现了能力差距：chat 模型直接抛 `NotImplementedError`；completion 模型则**每个 token 都要发一次 API 请求**（`max_tokens=1`，用 `logit_bias` 把候选 token 的 logit 抬到 100），按命中数累加 `scores` 取 argmax。相比 `RuntimeEndpoint.select` 的一批 logprob 请求，这里既慢又贵。注释里还列了 TODO（返回 logits、算完整候选似然、chunk 解码），说明这是个权宜实现。

**`openai_completion`：统一的重试包装**（[lang/backend/openai.py:L383-L422](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/openai.py#L383-L422)）：对 `openai.APIError` / `APIConnectionError` / `RateLimitError` 等（限流、连接抖动）做最多 3 次重试，每次间隔 5 秒，并累加 `token_usage`。这是第三方 API 后端必须有的健壮性处理（自研 `RuntimeEndpoint` 因为是内网调用，没做这层重试）。

> 对照表（自研 vs 第三方）：

| 维度 | `RuntimeEndpoint` | `OpenAI` |
| --- | --- | --- |
| 通信 | REST `/generate`（内网） | OpenAI SDK（公网） |
| 采样参数翻译 | `to_srt_kwargs` | `to_openai_kwargs` |
| `select` 实现 | 请求 logprob，精确 | `logit_bias` 逐 token 逼，chat 模型不支持 |
| `meta_info` | 丰富（prompt_tokens 等） | 空字典 `{}`，仅累计 `token_usage` |
| 重试 | 无 | 3 次重试（限流/连接） |
| `concatenate_and_append` | 支持（`support_concate_and_append=True`） | 不支持 |

#### 4.3.4 代码实践（源码阅读型，无需 OpenAI key）

**目标**：对照 `RuntimeEndpoint` 与 `OpenAI` 的 `generate`，理解「同一接口签名、截然不同实现」。

**步骤**：

1. 并排打开 [lang/backend/runtime_endpoint.py:L159-L196](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L159-L196) 与 [lang/backend/openai.py:L140-L180](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/openai.py#L140-L180)。
2. 回答：两者都从哪里读 prompt？各自如何翻译采样参数？返回的 `meta_info` 有何不同？

**需要观察的现象**：两者签名相同 `generate(self, s, sampling_params)`，但 `RuntimeEndpoint` 读 `s.text_` 发 REST、返回真实 `meta_info`；`OpenAI` 按 `is_chat_model` 分流到 `s.messages_` 或 `s.text_`、调 SDK、返回 `{}`。

**预期结果**：你能用一句话总结「`BaseBackend` 的抽象让两种完全不同的底层（REST vs SDK）呈现成同一个方法」。

> 说明：若想真正跑 `OpenAI` 后端，需要 `pip install openai tiktoken` 并配置 API key；本实践不要求联网，重在阅读对照。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `OpenAI.generate` 对 chat 模型要求 `sgl.gen` 必须紧跟 `sgl.assistant`？

> **答案**：因为 OpenAI 的 chat 接口（`client.chat.completions.create`）只接受「消息列表」，模型只能在 `assistant` 角色的回复位上生成新内容，不能在任意拼接的文本中续写。所以前端要求用户先用 `sgl.assistant()` 切到 assistant 角色，再 `sgl.gen(...)`，这样 `s.messages_` 里正好留出 assistant 回复的空位。自研 `RuntimeEndpoint` 没这个限制，因为它调的是 `/generate`，可在任意 prompt 后续写。

**练习 2**：`OpenAI` 后端的 `generate` 返回的 `meta_info` 为什么是空字典？

> **答案**：因为 OpenAI 的响应不像自研运行时那样返回每 token 的 logprob、prompt_tokens 明细等结构化 `meta_info`，它只在 `ret.usage` 里给 token 用量统计。`OpenAI` 把用量单独累加进 `self.token_usage`（[lang/backend/openai.py:L410-L411](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/openai.py#L410-L411)），而前端约定的 `meta_info` 字段没东西可填，就返回 `{}`。这体现了「同一抽象接口，弱后端只能返回较少信息」。

### 4.4 set_default_backend：切换默认后端

#### 4.4.1 概念说明

`set_default_backend` 是前端「切换推理提供方」的总开关。它做的事情极其简单：把传入的后端对象写进 `global_config.default_backend`。之后，所有 `SglFunction.run()` / `run_batch()` / `trace()` 在没有显式传 `backend=` 参数时，都会**回退**到这个全局默认值。

这个设计的好处是：你只需在程序开头设一次默认后端，后面所有 `@function` 都用它；少数需要「临时换后端」的场景，可以单独给某次调用传 `backend=` 覆盖。

`global_config.default_backend` 默认是 `None`（[global_config.py:L17-L18](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/global_config.py#L17-L18)）。注意它只是历史全局常量（文件顶部 FIXME 标记计划废弃，见 u1-l1），但前端后端的「默认值回退」目前仍依赖它。

#### 4.4.2 核心流程

默认后端的回退链（在 `ir.py` 里反复出现）：

```
SglFunction.run(..., backend=None):
    backend = backend or global_config.default_backend     # 未传则回退全局默认
    run_program(self, backend, ...)

SglFunction.run_batch(..., backend=None):
    backend = backend or global_config.default_backend     # 同上
    run_program_batch(...)

SglFunction.trace(..., backend=None):
    backend = backend or global_config.default_backend     # 同上
    trace_program(...)
```

三处回退点分别在 [lang/ir.py:L212](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L212)、[lang/ir.py:L293](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L293)、[lang/ir.py:L307](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L307)。

#### 4.4.3 源码精读

**`set_default_backend`**（[lang/api.py:L49-L50](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L49-L50)）：

```python
def set_default_backend(backend: BaseBackend):
    global_config.default_backend = backend
```

就这一行。注意类型标注是 `BaseBackend`，但实际由于 Python 鸭子类型，传一个 `Runtime`（启动器，非 `BaseBackend` 子类）也不会立刻报错——只是后续 `backend.generate(s, ...)` 会因签名不匹配而出问题。因此推荐传 `RuntimeEndpoint` / `OpenAI` 等真正的 `BaseBackend` 子类实例。

**`Runtime` / `Engine` 工厂的懒导入**（[lang/api.py:L35-L46](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L35-L46)）：

```python
def Runtime(*args, **kwargs):
    from sglang.lang.backend.runtime_endpoint import Runtime   # 延迟到调用时再 import
    return Runtime(*args, **kwargs)

def Engine(*args, **kwargs):
    from sglang.srt.entrypoints.engine import Engine
    return Engine(*args, **kwargs)
```

这里的「懒导入」很关键：前端的 `lang/` 包不希望在 `import sglang` 时就把重量级的 `srt`（运行时）拖进来——这样纯前端用户（比如只用 OpenAI 后端）不必安装运行时及其依赖。所以 `Engine` / `Runtime` 都包成函数，到真正调用时才 import。这与 u1-l1 讲过的 `LazyImport` 思路一致。

**`flush_cache` / `get_server_info` 对 `Runtime` 的兼容**（[lang/api.py:L53-L72](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L53-L72)）：

```python
def flush_cache(backend=None):
    backend = backend or global_config.default_backend
    if backend is None:
        return False
    if hasattr(backend, "endpoint"):     # 若是 Runtime（启动器），取它内部的 RuntimeEndpoint
        backend = backend.endpoint
    return backend.flush_cache()
```

这段印证了 4.2 的区分：如果默认后端恰好是一个 `Runtime`（它有 `.endpoint` 属性），这里会自动「拆包」取出真正的 `RuntimeEndpoint` 再调 `flush_cache`。这是一个对 `Runtime` 的兼容补丁——但也侧面说明，把 `Runtime` 当后端用属于「半官方」用法，主流用法仍是 `RuntimeEndpoint`。

**真实使用样例**（[test/few_shot_gsm8k.py:L66](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/test/few_shot_gsm8k.py#L66) 与 [test/kits/hellaswag_kit.py:L20](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/test/kits/hellaswag_kit.py#L20)）：

```python
set_default_backend(RuntimeEndpoint(normalize_base_url(args.host, args.port)))
# 或
sgl.set_default_backend(sgl.RuntimeEndpoint(self.base_url))
```

这就是「先 `sglang serve` 起服务，再 `set_default_backend(RuntimeEndpoint(url))`」的标准范式。`normalize_base_url` 把 host/port 拼成 `http://host:port`（见 [test/test_utils.py:L463-L468](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/test/test_utils.py#L463-L468)）。

#### 4.4.4 代码实践（本地起服务型）

**目标**：体验「设默认后端 → 不传 backend 也能跑」与「临时传 backend= 覆盖」两种用法。

**步骤**：

1. 起一个本地 sglang 服务（见 u1-l2），设默认后端为 `RuntimeEndpoint`：

```python
import sglang as sgl
from sglang.lang.backend.runtime_endpoint import RuntimeEndpoint

sgl.set_default_backend(RuntimeEndpoint("http://localhost:30000"))

@sgl.function
def qa(s, question):
    s += "Q: " + question + "\nA:" + sgl.gen("answer", max_new_tokens=32, stop="\n")

# 用法一：用默认后端
ret1 = qa.run(question="What is 2+2?")
print("default backend:", ret1["answer"])
```

2. （可选）临时换后端，验证 `backend=` 参数可覆盖默认：

```python
# 用法二：给单次调用临时传另一个 backend（此处仍指向同一服务，仅演示覆盖语法）
another = RuntimeEndpoint("http://localhost:30000")
ret2 = qa.run(question="What is 3+3?", backend=another)
print("override backend:", ret2["answer"])
```

**需要观察的现象**：用法一无需传 `backend=` 即可工作（走了 `global_config.default_backend` 回退）；用法二显式传的 `backend=` 生效。

**预期结果**：两次都能返回答案。若想确认回退确实发生，可在 [lang/ir.py:L212](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L212) 处加一行 `print("resolved backend:", type(backend).__name__)` 观察。

**若本地无法起服务**：标注为「待本地验证」；可只做源码阅读，理解 `backend = backend or global_config.default_backend` 的回退语义。

#### 4.4.5 小练习与答案

**练习 1**：如果不调用 `set_default_backend`，直接 `qa.run(...)` 会怎样？

> **答案**：`global_config.default_backend` 是 `None`，于是 `run` 里 `backend = backend or None` 仍是 `None`，传给 `StreamExecutor(None, ...)`。随后 `_execute_gen` 调 `None.generate(...)` 会抛 `AttributeError`。所以用 DSL 前必须先设默认后端（或每次调用显式传 `backend=`）。`trace` 是例外——它会在 `backend is None` 时造一个空壳 `BaseBackend()`（见 u2-l2），所以纯追踪不需要真后端。

**练习 2**：`set_default_backend` 接收的是对象还是类？为什么？

> **答案**：接收**对象**（实例），不是类。因为后端是有状态的（`RuntimeEndpoint` 持有 `base_url`、连接信息；`OpenAI` 持有 client、tokenizer、token_usage 累计值）。同一个后端实例被多次调用时会累积状态（如 OpenAI 的 token 用量统计），所以必须是实例而非类。

## 5. 综合实践

把本讲四个模块串起来，完成「**同一份 `@function`，两种后端**」的对比任务：

1. **起服务**：用一个小模型起本地 sglang 服务（`sglang serve --model-path <small> --port 30000`，见 u1-l2）。

2. **写一份 `@function`**（本地示例文件）：

```python
import sglang as sgl
from sglang.lang.backend.runtime_endpoint import RuntimeEndpoint

@sgl.function
def plan(s, topic):
    s += "Topic: " + topic + "\n"
    s += "Step 1:" + sgl.gen("s1", max_new_tokens=16, stop="\n")
```

3. **用 `RuntimeEndpoint` 跑一遍**：

```python
sgl.set_default_backend(RuntimeEndpoint("http://localhost:30000"))
r1 = plan.run(topic="bake a cake")
print("[RuntimeEndpoint] s1 =", r1["s1"])
print("[RuntimeEndpoint] meta_info keys =", list(r1.get_meta_info("s1").keys()) if hasattr(r1, "get_meta_info") else "n/a")
```

4. **用 `OpenAI` 后端连同一个 sglang 服务**（sglang 的 `/v1/chat/completions` 是 OpenAI 兼容接口，见 u1-l4）：

```python
from sglang.lang.backend.openai import OpenAI
# 注意：sglang 服务的 OpenAI 兼容端点在 /v1，model 名以 /get_model_info 返回为准
sgl.set_default_backend(OpenAI(model_name="<见 /get_model_info>", base_url="http://localhost:30000/v1", api_key="None"))
r2 = plan.run(topic="bake a cake")
print("[OpenAI backend] s1 =", r2["s1"])
```

   **预期会踩一个坑**：`OpenAI` 后端默认把模型当 chat 模型（`is_chat_model=True`，见 4.3.3），而 chat 模型要求 `sgl.gen` 必须紧跟 `sgl.assistant()`。`plan` 里写的是 `s += "Step 1:" + sgl.gen(...)`，没有 `sgl.assistant()`，因此这里大概率会抛 `RuntimeError("...sgl.gen must be right after sgl.assistant...")`。这**正是两种后端的行为差异**——观察并记录这个报错。若想让 OpenAI 路径也跑通，需把程序改成 `s += sgl.assistant(); s += sgl.gen("s1", ...)`（chat 模板与角色原语见 u2-l4）。

5. **对照并写一段总结**，回答：
   - 两者返回的 `meta_info` 丰富度有何不同？（提示：`RuntimeEndpoint` 返回真实运行时 `meta_info`，`OpenAI` 返回 `{}`）
   - 同一份 `@function` 在两种后端下是否都能直接跑？为什么 OpenAI chat 后端会多出「必须紧跟 `sgl.assistant`」的约束？（提示：chat 接口只能在 assistant 回复位生成）
   - 若把 `gen` 换成 `select(["A","B"])`，`RuntimeEndpoint` 能正常打分，`OpenAI` 在 chat 模型下会怎样？（提示：抛 `NotImplementedError`，见 4.3）
   - 为什么说「`RuntimeEndpoint` 能力最完整，第三方后端是退化版」？

> 若本地无 GPU 或无法起服务，第 1、3、4 步标注为「待本地验证」，但第 2、5 步（写程序 + 阅读源码对照）可独立完成。

## 6. 本讲小结

- `BaseBackend`（[lang/backend/base_backend.py:L9-L83](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/base_backend.py#L9-L83)）是前端与底层推理提供方之间的统一契约：`generate` / `generate_stream` / `select` 是必须实现的核心方法，其余是可选钩子（默认空实现，让弱后端优雅退化）。
- `RuntimeEndpoint`（`BaseBackend` 子类）是对**正在运行**的 sglang HTTP 服务的 REST 客户端，构造即探活（`/get_model_info`），`generate` 组装 `/generate` 请求体并用 `to_srt_kwargs` 翻译采样参数；它 `support_concate_and_append=True` 且 `select` 能拿真实 logprob，能力最完整。
- `Runtime` **不是**后端，而是服务启动器（spawn 子进程跑 `launch_server`，轮询 `/health_generate`），内部包一个 `RuntimeEndpoint`；它自己的 `generate` 签名与后端契约不同。
- `OpenAI` / `Anthropic` / `LiteLLM` 是第三方后端，走各家 SDK，用各自的 `to_*_kwargs` 翻译参数；它们拿不到 token 级 logprob，`select` 只能退化（chat 模型直接不支持），并需做限流重试——是「能力退化的后端」。
- `set_default_backend(backend)`（[lang/api.py:L49-L50](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L49-L50)）把后端写进 `global_config.default_backend`；`run` / `run_batch` / `trace` 在未传 `backend=` 时都回退到它（[lang/ir.py:L212](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L212) 等）。
- 典型用法是「先 `sglang serve`，再 `set_default_backend(RuntimeEndpoint(url))`」（[test/few_shot_gsm8k.py:L66](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/test/few_shot_gsm8k.py#L66)）；`Runtime` / `Engine` 在 `api.py` 里用懒导入，避免纯前端用户被迫安装运行时依赖。

## 7. 下一步学习建议

- **本单元收尾**：本讲讲完了前端「后端如何对接不同提供方」。下一讲 **u2-l4（Chat 模板与 choices 选择策略）** 会回到 `select` 的评分细节，讲 `chat_template` 如何渲染对话、`token_length_normalized` 等 `choices_method` 如何决定候选项胜出——它正是本讲 `RuntimeEndpoint.select` 里那个 `choices_method(...)` 参数的来处。
- **横向对照**：建议并排读 [lang/backend/runtime_endpoint.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py) 与 [lang/backend/openai.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/openai.py) 的 `generate` / `select`，体会「同一接口签名、截然不同实现」的抽象价值；`Anthropic` / `LiteLLM` 可作延伸阅读。
- **回到运行时主线**：`RuntimeEndpoint` 调的 `/generate`、`/get_model_info`、`/health_generate`、`/flush_cache` 等端点都定义在运行时层 `srt/entrypoints/http_server.py`。学完本单元后，第 3 单元将正式进入 `srt/` 服务端架构，届时你会看到这些 HTTP 端点背后是如何接收请求、做 token 化并交给调度器的——也就是 `RuntimeEndpoint.generate` 这一行 REST 调用在服务端的完整落地。

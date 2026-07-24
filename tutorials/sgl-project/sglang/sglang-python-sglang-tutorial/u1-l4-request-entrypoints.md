# 发送请求：OpenAI 兼容 API 与 Engine 嵌入式 API

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 OpenAI Python SDK 或 `curl` 调用一个正在运行的 SGLang 服务，命中 `/v1/chat/completions`、`/v1/completions`、`/v1/embeddings` 三个端点。
- 用 `sglang.Engine(...)` 在「同一个 Python 进程里」直接发起推理，无需启动 HTTP 服务。
- 看清这两条请求路径在源码里如何**汇聚到同一个函数** `tokenizer_manager.generate_request(...)`，从而进入运行时调度核心。
- 理解 `sglang.Engine` 这个名字背后的「前端工厂」与命名空间解析机制：为什么 `import sglang` 之后 `sglang.Engine` 指向的是运行时引擎而不是前端 DSL 里的同名函数。
- 掌握新增的 `return_token_ids` 参数：它如何在 OpenAI 兼容响应里带回**输出** token id 与 **prompt** token id，以及 chat 与 completions 两个端点在「流式」下的不同行为。

本讲是 u1-l2（启动服务）的延续。上一讲你学会了「把服务跑起来」，本讲教你「把请求发进去」，并打通从入口到调度核心的源码链路。

## 2. 前置知识

在进入源码前，先建立几个直觉性的概念。

**推理请求的本质**。一条推理请求可以抽象成一句话：给定一段输入 token 序列（prompt），请模型接着生成若干个输出 token。无论是聊天、补全还是生成 embedding，底层都是「输入 token → 模型前向 → 输出」的过程。

**「入口」和「运行时」的分界**。SGLang 把「如何接收请求」和「如何执行推理」解耦了：

- **入口（entrypoint）**：负责把外部世界（HTTP 客户端、Python 函数调用）的请求翻译成运行时能理解的数据结构 `GenerateReqInput`。
- **运行时（runtime）**：拿到 `GenerateReqInput` 之后，做 token 化、组 batch、GPU 前向、采样、detokenize，最后把结果送回来。

本讲只聚焦「入口」这一侧，「运行时」内部机制（Scheduler、ModelRunner、KV 缓存）留到第 4–6 单元细讲。

**两种典型入口**：

1. **HTTP + OpenAI 兼容协议**：起一个 HTTP 服务（`sglang serve`），客户端用 OpenAI 官方 SDK 或 `curl` 发 JSON 请求。适合多语言客户端、跨机器、生产部署。
2. **同进程嵌入式 API（`sglang.Engine`）**：在你的 Python 脚本里直接构造 `sglang.Engine(...)`，调用 `engine.generate(...)`。没有 HTTP、没有序列化开销，适合单机应用、测试、RL rollout。

**进程拓扑速览**（详见 u3-l2）。无论哪种入口，运行时通常由三部分组成：主进程里的 `TokenizerManager`、子进程里的 `Scheduler`、子进程里的 `DetokenizerManager`，它们之间用 ZMQ 通信。本讲最关键的结论是：**两条入口最终都汇聚到 `TokenizerManager.generate_request(...)` 这一个方法**。

**token id 是什么**。文本进入模型前会被分词器（tokenizer）切成一串整数 id；模型吐出的也是一串整数 id，再由分词器还原成文本。多数接口只把「文本」返回给你，但有时（RL 训练、分词调试、跨引擎对齐）你需要直接拿到这串整数 id，这正是本讲第 4.4 节 `return_token_ids` 要解决的问题。

## 3. 本讲源码地图

本讲涉及的关键文件，按「请求流向」排列：

| 文件 | 作用 |
| --- | --- |
| [srt/entrypoints/engine.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/engine.py) | 嵌入式入口。`Engine` 类在主进程里拉起 Scheduler/Detokenizer 子进程，并暴露 `generate/encode` 等方法。 |
| [srt/entrypoints/EngineBase.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/EngineBase.py) | 抽象基类，统一「HTTP 引擎」和「嵌入式引擎」的接口形状（`generate/flush_cache/update_weights...`）。 |
| [srt/entrypoints/openai/protocol.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/protocol.py) | OpenAI 兼容协议的 Pydantic 模型（`ChatCompletionRequest` / `CompletionRequest` / `EmbeddingRequest` 等），含 `return_token_ids` 开关与 `token_ids`/`prompt_token_ids` 响应字段。 |
| [srt/entrypoints/openai/serving_chat.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/serving_chat.py) | 处理 `/v1/chat/completions` 的 handler：把聊天请求渲染成 prompt、转成内部 `GenerateReqInput`，并实现 `return_token_ids` 的非流式回填与流式拒绝。 |
| [srt/entrypoints/openai/serving_completions.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/serving_completions.py) | 处理 `/v1/completions` 的 handler：把补全请求转成 `GenerateReqInput`，并在非流式与流式两种路径下都支持 `return_token_ids`。 |
| [srt/entrypoints/openai/serving_base.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/serving_base.py) | 各 OpenAI handler 的公共基类，定义 `handle_request` 的统一流程（校验→转换→分流流式/非流式）。 |
| [srt/entrypoints/http_server.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/http_server.py) | FastAPI 路由表，把 `/v1/chat/completions`、`/generate` 等 URL 绑到对应 handler。 |
| [lang/api.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/lang/api.py) | 前端 DSL 的公共 API，提供 `function/gen/select` 等原语，也提供 `Engine`、`Runtime` 两个「前端工厂」。 |
| [__init__.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/__init__.py) | `import sglang` 执行的装配文件，决定 `sglang.Engine` 最终指向谁。 |

> 提示：表格里的链接已固定到本讲使用的 HEAD `59ef3b15cc`，可直接点击阅读。下文正文中的「永久链接」均采用相同的 `#L起始-L结束` 行号格式。

---

## 4. 核心概念与源码讲解

### 4.1 两条请求路径与它们的汇聚点

#### 4.1.1 概念说明

很多人第一次接触 SGLang 时会困惑：「为什么有 `sglang.Engine`，又有 HTTP 服务，还有前端 `@function`？它们到底什么关系？」

答案是：**它们是同一套运行时的不同入口**。运行时核心只有一份，入口可以是：

- **嵌入式入口**：在你的进程里 `import sglang; engine = sglang.Engine(...)`，直接函数调用。
- **HTTP 入口**：服务以独立进程跑着，客户端通过 `/v1/chat/completions`、`/generate` 等 URL 发请求。
- **前端 DSL 入口**：用 `@function` + `gen/select` 写复杂生成流程，由前端解释器驱动上面任意一种后端（这部分第 2 单元细讲）。

之所以要区分入口，是因为不同使用场景对「序列化开销」「跨进程/跨机器」「客户端语言」的要求不同。但只要请求进入运行时，它们就共用同一套调度与执行逻辑。

#### 4.1.2 核心流程

两条路径的「形状」可以用下面的伪代码对比：

```
# 路径 A：嵌入式
obj = GenerateReqInput(text=prompt, sampling_params={...})      # 在 Engine.generate 里构造
generator = engine.tokenizer_manager.generate_request(obj, None)
result = await generator.__anext__()                            # 拿到结果

# 路径 B：HTTP /v1/chat/completions
chat_req = ChatCompletionRequest(messages=..., temperature=...) # Pydantic 解析
adapted = serving_chat._convert_to_internal_request(chat_req)  # 渲染 prompt → GenerateReqInput
result = await tokenizer_manager.generate_request(adapted, raw_request).__anext__()
```

两条路径唯一相同的、也是最关键的一行，都是：

```
tokenizer_manager.generate_request(<GenerateReqInput>, <raw_request or None>)
```

`GenerateReqInput` 是运行时统一理解的「请求对象」，无论它来自 `Engine.generate` 的直接构造，还是来自 OpenAI 协议的转换。这就是「汇聚点」的含义——**入口的多样性在 `GenerateReqInput` 这一层数据结构上被抹平了**。

#### 4.1.3 源码精读

`Engine` 类的文档注释把这套拓扑讲得很清楚，它明确指出三件事：HTTP server、Engine、TokenizerManager 都在主进程；Scheduler 和 DetokenizerManager 是子进程；进程间用 ZMQ 通信。

[engine.py:L183-L195](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/engine.py#L183-L195) —— `Engine` 类的文档字符串说明了三组件的职责与进程归属。

为了让「HTTP 引擎」和「嵌入式引擎」能互换使用（例如 RL 框架里同一段代码既能连 HTTP 也能用嵌入式），SGLang 抽象出了 `EngineBase`，规定一个引擎必须实现 `generate / flush_cache / update_weights_from_tensor / release_memory_occupation / resume_memory_occupation / shutdown` 等方法。

[EngineBase.py:L7-L39](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/EngineBase.py#L7-L39) —— 抽象方法 `generate` 的签名，是「入口→运行时」的统一契约。

> 本讲我们集中看嵌入式 `Engine` 和 HTTP 入口；HTTP 那一侧也有符合该接口形状的实现，这让你可以在不改业务代码的前提下切换部署形态。

#### 4.1.4 代码实践

**实践目标**：不写代码，只在源码里「走一遍」两条路径，确认它们都汇聚到 `tokenizer_manager.generate_request`。

**操作步骤**：

1. 打开 [engine.py:L400](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/engine.py#L400)，这是 `Engine.generate` 里把请求交给 TokenizerManager 的那一行。
2. 打开 [serving_chat.py:L1452-L1454](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/serving_chat.py#L1452-L1454)，这是 HTTP 聊天请求非流式分支里把请求交给 TokenizerManager 的那一行。
3. 对比这两处调用：参数 1 都是 `GenerateReqInput`（或其适配对象），参数 2 一个是 `None`、一个是 `raw_request`（FastAPI 的 `Request`，用于检测客户端断连）。

**需要观察的现象**：两边调用的方法名、第一个参数类型完全相同；唯一的差别是第二个参数——HTTP 路径多传了一个 `raw_request`，仅用于断连检测等 HTTP 特有能力。

**预期结果**：你能用一句话总结——「嵌入式入口和 HTTP 入口的差别只在『如何拿到 `GenerateReqInput`』，进入运行时的那一行是同一个方法」。

#### 4.1.5 小练习与答案

**练习 1**：如果客户端通过 `/v1/chat/completions` 发请求，运行时最终调用的是 `TokenizerManager.generate_request` 还是 `Scheduler.run_batch`？

> **答案**：先调用 `TokenizerManager.generate_request`（入口汇聚点），由 TokenizerManager 再通过 ZMQ 把请求转发给 Scheduler 子进程，Scheduler 内部才会执行 `run_batch`。本讲聚焦入口那一层。

**练习 2**：为什么 `Engine.generate` 传给 `generate_request` 的第二个参数是 `None`，而 HTTP 路径传的是 `raw_request`？

> **答案**：嵌入式入口没有 HTTP 连接，不存在「客户端断连」的概念，所以不需要 `raw_request`；HTTP 路径需要它来在中途检测客户端是否已经断开，以便及时中止无用的生成。

---

### 4.2 Engine 类：同进程嵌入式入口

#### 4.2.1 概念说明

`sglang.Engine` 是「把整个运行时塞进你的 Python 进程」的入口。它的最大优点是**零网络开销**：请求不需要走 HTTP 序列化、不需要 JSON 编解码、结果直接以 Python 字典/张量形式返回。这对以下场景特别友好：

- 本地脚本、Notebook 实验。
- RL 框架的 rollout：训练循环和推理引擎在同一个或相邻进程里，频繁短交互。
- 需要返回 `hidden_states`、`logprobs` 张量等不便 JSON 序列化的大对象。

代价是：`Engine` 会占用当前进程的 GPU，且它本身要在构造时拉起子进程、加载模型权重，初始化较重。

#### 4.2.2 核心流程

`Engine` 的生命周期分三步：

1. **构造** `Engine(**kwargs)`：解析 `ServerArgs` → 拉起 Scheduler/Detokenizer 子进程 → 在主进程创建 `TokenizerManager` → 等待模型加载完成。
2. **调用** `engine.generate(prompt=..., sampling_params=...)`：构造 `GenerateReqInput` → 调 `tokenizer_manager.generate_request` → 在事件循环里 `run_until_complete` 取出结果 → 返回字典。
3. **销毁**：`engine.shutdown()` 或用 `with sglang.Engine(...) as engine:` 上下文管理器自动清理子进程。

用伪代码概括 `generate` 的内部实现：

```
def generate(self, prompt=None, sampling_params=None, ..., stream=False):
    obj = GenerateReqInput(text=prompt, sampling_params=sampling_params, stream=stream, ...)
    generator = self.tokenizer_manager.generate_request(obj, None)
    if stream:
        # 把异步生成器包成同步生成器，逐块 yield
        return generator_wrapper()
    else:
        return self.loop.run_until_complete(generator.__anext__())
```

注意 `Engine.generate` 是**同步**方法（内部用 `loop.run_until_complete` 驱动异步循环），而 `Engine.async_generate` 是**异步**方法，适合你已经在一个 asyncio 程序里。

#### 4.2.3 源码精读

先看构造函数。`Engine.__init__` 接收的 `**kwargs` 与 `ServerArgs` 完全一致（`--tp`、`--mem-fraction-static` 等命令行参数都可以作为关键字参数传入）。构造时会调用 `_launch_subprocesses`，在主进程保留 `tokenizer_manager`。

[engine.py:L234-L252](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/engine.py#L234-L252) —— `__init__` 拉起子进程并把 `tokenizer_manager` 存为属性，这是后续 `generate` 能调用的基础。

再看核心的 `generate` 方法。它的参数列表非常长，但结构很清晰：所有参数都原样塞进 `GenerateReqInput`，然后交给 `tokenizer_manager`。

[engine.py:L370-L399](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/engine.py#L370-L399) —— 把传入参数打包成 `GenerateReqInput` 对象。

紧接着就是本讲的「汇聚点」那一行：

[engine.py:L400-L415](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/engine.py#L400-L415) —— 调用 `tokenizer_manager.generate_request` 并区分流式/非流式返回。注意 `generator = self.tokenizer_manager.generate_request(obj, None)` 这一行——它和 HTTP 路径用的是**同一个方法**。

生成 embedding 用的是 `encode`，结构几乎一样，只是构造的是 `EmbeddingReqInput`：

[engine.py:L528-L542](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/engine.py#L528-L542) —— `encode` 方法同样汇聚到 `tokenizer_manager.generate_request`。

> 说明：源码里 `Engine` 还继承了 `EngineScoreMixin`，因此额外提供 `score()`/`async_score()`（打分/重排）能力，但本讲不展开，留到第 8 单元。

#### 4.2.4 代码实践

**实践目标**：用 `sglang.Engine` 在同进程里跑一句生成，确认嵌入式入口可用。

**操作步骤**：

1. 准备一个本地小模型路径（例如 `Qwen/Qwen2.5-0.5B`，或你本地已有的路径）。
2. 写一段最小脚本（**示例代码，非项目原有文件**）：

   ```python
   # example_engine.py （示例代码）
   import sglang

   # 关键字参数等价于 sglang serve 的命令行参数
   with sglang.Engine(model_path="Qwen/Qwen2.5-0.5B") as engine:
       out = engine.generate(
           prompt="The capital of France is",
           sampling_params={"temperature": 0.0, "max_new_tokens": 16},
       )
       print(type(out))            # <class 'dict'>
       print(out["text"])          # 模型生成的文本
       print(out["meta_info"])     # 含 prompt_tokens / completion_tokens / finish_reason 等
   ```

3. 运行 `python example_engine.py`。

**需要观察的现象**：

- 脚本启动时会打印模型加载日志（首次较慢），随后直接输出文本，**全程没有起 HTTP 服务**。
- `out` 是一个普通 Python 字典，键包含 `text` 和 `meta_info`。

**预期结果**：看到一段补全文本（如 `Paris`），以及 `meta_info` 里 `completion_tokens` 等统计字段。若你的环境无 GPU 或模型下载失败，请**待本地验证**后记录实际输出。

#### 4.2.5 小练习与答案

**练习 1**：`engine.generate(...)` 是同步函数，但它内部驱动的是异步生成器。如果我想在一个已有的 `async def main()` 协程里发请求，应该用哪个方法？

> **答案**：用 `await engine.async_generate(...)`。`async_generate` 直接返回异步迭代器/协程，不会像 `generate` 那样用 `loop.run_until_complete` 阻塞当前线程，避免在已有事件循环里嵌套阻塞调用。

**练习 2**：把 `stream=True` 传给 `engine.generate`，返回值的类型会变成什么？

> **答案**：变成一个同步生成器（`Iterator[Dict]`）。源码里的 `generator_wrapper()` 会把异步块逐个 `yield` 出来，你可以用 `for chunk in engine.generate(..., stream=True)` 拿到逐步增长的文本。

---

### 4.3 OpenAI 协议与 serving_chat：HTTP 入口

#### 4.3.1 概念说明

为了让任何用惯了 OpenAI API 的客户端（官方 Python/Node SDK、LangChain、curl 等）能**零改动**地连上 SGLang，SGLang 实现了一套 OpenAI 兼容的 HTTP 协议。核心端点有三个：

- `POST /v1/chat/completions`：聊天补全，输入是 `messages`（多轮对话），最常用。
- `POST /v1/completions`：文本补全，输入是单个 `prompt` 字符串。
- `POST /v1/embeddings`：生成向量表示。

这套协议的「形态」由 [protocol.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/protocol.py) 里的 Pydantic 模型定义；「行为」（如何把 OpenAI 请求转成内部请求）由 `serving_chat.py`（聊天）与 `serving_completions.py`（补全）等 handler 实现。本节先建立整体框架，第 4.4 节再用 `return_token_ids` 这个具体特性把两个 handler 串起来对比。

#### 4.3.2 核心流程

一个 `/v1/chat/completions` 请求的生命周期：

1. FastAPI 收到请求，按路由表把它分给 `openai_v1_chat_completions` 处理函数。
2. FastAPI 用 Pydantic 把 JSON body 解析成 `ChatCompletionRequest`。
3. 该函数调用 `app.state.openai_serving_chat.handle_request(request, raw_request)`。
4. `handle_request`（在基类 `serving_base.py` 里）做三件事：**校验** → **转换成内部 `GenerateReqInput`** → 按 `stream` 标志分流到流式/非流式分支。
5. 转换过程（`_convert_to_internal_request`）会用 chat template 把 `messages` 渲染成 prompt、把采样参数归并好、抽取多模态数据，最终构造 `GenerateReqInput`。
6. 流式/非流式分支最终都调 `tokenizer_manager.generate_request(adapted_request, raw_request)`——再次回到那个汇聚点。

用伪代码概括基类的统一流程：

```
async def handle_request(self, request, raw_request):
    if (err := self._validate_request(request)): return error(err)
    adapted, processed = self._convert_to_internal_request(request, raw_request)
    if request.stream:
        return await self._handle_streaming_request(adapted, processed, raw_request)
    else:
        return await self._handle_non_streaming_request(adapted, processed, raw_request)
```

#### 4.3.3 源码精读

先看协议侧。`ChatCompletionRequest` 把 OpenAI 的字段原样建模，同时塞进了一批「SRT backend only」的扩展字段（如 `top_k`、`regex`、`json_schema`、`lora_path`）。这些扩展字段是 SGLang 比标准 OpenAI 多出来的能力，标准 OpenAI 模型会忽略它们。

[protocol.py:L720-L729](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/protocol.py#L720-L729) —— `ChatCompletionRequest` 的头部字段：`messages`、`model`、采样参数等。

补全和嵌入的请求结构类似，分别对应 `/v1/completions` 与 `/v1/embeddings`：

[protocol.py:L317-L324](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/protocol.py#L317-L324) —— `CompletionRequest` 的 `prompt` 字段，支持字符串、token id 列表等多种形态。

[protocol.py:L1163-L1170](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/protocol.py#L1163-L1170) —— `EmbeddingRequest` 的 `input` / `model` / `dimensions` 字段。

协议还负责把 OpenAI 风格的采样参数翻译成运行时风格，优先级是「用户显式值 > 模型 generation-config > OpenAI 默认值」：

[protocol.py:L966-L1015](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/protocol.py#L966-L1015) —— `to_sampling_params` 方法，把请求里的 `temperature/top_p/...` 归并成内部采样参数字典。

再看路由侧。FastAPI 把三个 OpenAI 端点绑到对应的 handler：

[http_server.py:L1651-L1678](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/http_server.py#L1651-L1678) —— `/v1/completions`、`/v1/chat/completions`、`/v1/embeddings` 三个路由及其 handler 函数。

这些 handler 在服务启动时被实例化，并持有同一个 `tokenizer_manager`：

[http_server.py:L302-L308](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/http_server.py#L302-L308) —— 服务启动时创建 `OpenAIServingChat` / `OpenAIServingCompletion` / `OpenAIServingEmbedding`，把 `tokenizer_manager` 注入进去。

基类 `handle_request` 提供了统一的「校验→转换→分流」骨架：

[serving_base.py:L73-L109](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/serving_base.py#L73-L109) —— `handle_request` 的主流程，注意第 102–109 行按 `request.stream` 分流。

聊天 handler 的核心是 `_convert_to_internal_request`：它渲染 chat template、提取多模态数据、计算 LoRA 路径、解析工具调用约束，最终构造出 `GenerateReqInput`：

[serving_chat.py:L746-L782](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/serving_chat.py#L746-L782) —— `OpenAIServingChat` 把请求组装成内部 `GenerateReqInput` 的关键片段。

非流式分支拿到这个 `GenerateReqInput` 后，调用的就是那个汇聚点方法：

[serving_chat.py:L1452-L1454](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/serving_chat.py#L1452-L1454) —— `_handle_non_streaming_request` 调用 `tokenizer_manager.generate_request`，与嵌入式入口完全相同。

> 顺带一提：除了 OpenAI 兼容端点，还有一个更「裸」的端点 `/generate`，它直接接收 `GenerateReqInput`，不做 chat template 渲染。对前端 DSL（`RuntimeEndpoint`）和一些内部工具来说，这个端点更轻量。

[http_server.py:L828-L875](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/http_server.py#L828-L875) —— `/generate` 端点直接把 `GenerateReqInput` 交给 `tokenizer_manager.generate_request`，是 HTTP 侧「最薄」的入口。

#### 4.3.4 代码实践

**实践目标**：用 OpenAI Python SDK 连上本地 `sglang serve`，发一个聊天请求，确认 OpenAI 兼容入口可用。

**操作步骤**：

1. 在一个终端启动服务（沿用 u1-l2）：

   ```bash
   python -m sglang.launch_server --model-path Qwen/Qwen2.5-0.5B --port 30000
   ```

2. 在另一个终端运行（**示例代码**）：

   ```python
   # example_openai.py （示例代码）
   from openai import OpenAI

   client = OpenAI(base_url="http://127.0.0.1:30000/v1", api_key="EMPTY")
   resp = client.chat.completions.create(
       model="default",
       messages=[{"role": "user", "content": "用一句话介绍 SGLang。"}],
       temperature=0.0,
   )
   print(type(resp))                              # ChatCompletion 对象
   print(resp.choices[0].message.content)         # 模型回复
   print(resp.usage.prompt_tokens, resp.usage.completion_tokens)
   ```

   或用 `curl` 等价调用：

   ```bash
   curl http://127.0.0.1:30000/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"default","messages":[{"role":"user","content":"hi"}]}'
   ```

**需要观察的现象**：返回结构与官方 OpenAI API 完全一致（`choices[0].message.content`、`usage` 字段），无需为 SGLang 改写客户端代码。

**预期结果**：打印出一句中文回复，以及非零的 token 统计。若未安装 `openai` 包，可改用 `curl`，或**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`ChatCompletionRequest` 里有一批注释写着「Extra parameters for SRT backend only and will be ignored by OpenAI models」，举两个这样的字段并说明用途。

> **答案**：例如 `regex`（用正则约束输出）、`json_schema`（强制输出合法 JSON）、`top_k`、`lora_path`。它们是 SGLang 相对标准 OpenAI 的扩展能力；标准 OpenAI 模型不识别这些字段。

**练习 2**：如果我只想做「文本补全」（没有多轮对话），该用哪个端点？它和 `/v1/chat/completions` 在协议层的最大区别是什么？

> **答案**：用 `POST /v1/completions`。区别在于输入字段：completions 用单个 `prompt`，chat 用 `messages` 列表，且 chat 会经过 chat template 渲染。

---

### 4.4 return_token_ids：在响应中回传 token id 序列

#### 4.4.1 概念说明

到此为止，我们看到的响应都只返回「文本」。但在很多场景下，你需要直接拿到模型实际吐出的**整数 token id**：

- **RL 训练 / rollout**：训练侧需要对齐推理引擎的 token 序列与 logprobs，拿到精确的 token id 才能复算 loss，不能只依赖文本（重新分词可能引入边界误差）。
- **调试分词**：确认「这段文本到底被切成哪些 token」，排查 tokenizer 与生成不一致。
- **跨引擎对齐**：把 SGLang 的输出 token id 序列喂给另一个引擎做校验，省去重新分词。

为此，SGLang 在 OpenAI 兼容协议里新增了一个请求开关 **`return_token_ids`**（[PR #30917](https://github.com/sgl-project/sglang/pull/30917)）。打开后，响应的每个 `choice` 会多出两个字段：

- `token_ids: List[int]` —— 模型本次生成的**输出** token id 序列（回答「模型说了什么」）。
- `prompt_token_ids: List[int]` —— 输入 prompt 被分词后的 token id 序列（回答「我们喂了什么」）。

两个易混点：

1. **输出 vs 输入**：`token_ids` 是「生成的输出」，`prompt_token_ids` 是「输入的 prompt」，别弄反。
2. **`return_token_ids` vs `return_prompt_token_ids`**：chat 接口历史上早就有 `return_prompt_token_ids`（只回传 prompt 的 token id）。新增的 `return_token_ids` 是「同时回传输出 + prompt」的统一开关；completions 接口则只有 `return_token_ids` 这一个开关（见下方对比表）。

为了不破坏与官方 OpenAI 客户端的兼容性，这两个字段默认都是 `None`，并在序列化时被**剔除**（`pop`），只有显式请求时才出现在 JSON 里。

#### 4.4.2 核心流程

`return_token_ids` 涉及「请求字段 → 内部转换 → 响应填充」三步，且 **chat 与 completions 两个端点的实现并不相同**。先用一张表说清差异：

| 维度 | `/v1/completions` | `/v1/chat/completions` |
| --- | --- | --- |
| 请求字段 | 只有 `return_token_ids` | `return_token_ids` 与 `return_prompt_token_ids` 两个 |
| 非流式响应 | 回填 `token_ids` + `prompt_token_ids` | 回填 `token_ids` + `prompt_token_ids` |
| 流式响应 | **支持**，每个 chunk 带「增量」token id | **不支持**，抛 `ValueError` |

两个端点「流式行为不一样」的根本原因：completions 的流式 choice 直接有 `text`/`token_ids` 字段，可以逐 chunk 切出增量 token id；而 chat 的流式响应用的是 OpenAI 的 `delta` 增量消息结构，没有承载 token id 序列的合适位置，强行支持会破坏协议形状，因此 chat 在转换阶段直接拒绝。

两端非流式填充逻辑的伪代码对比：

```
# completions（serving_completions.py）
token_ids        = ret_item["output_ids"]                if request.return_token_ids else None
prompt_token_ids = ret_item.get("prompt_token_ids")      if request.return_token_ids else None

# chat（serving_chat.py）
choice_token_ids        = ret_item["output_ids"]          if request.return_token_ids else None
choice_prompt_token_ids = ret_item.get("prompt_token_ids")
                          if (request.return_prompt_token_ids or request.return_token_ids) else None
```

两个端点都把「要不要回传 prompt token id」映射到运行时内部的 `return_prompt_token_ids` 机制：completions 直接复用 `return_token_ids` 的值（`return_prompt_token_ids=request.return_token_ids`），chat 用 `or` 把两个开关合并（`return_prompt_token_ids=request.return_prompt_token_ids or request.return_token_ids`）。

#### 4.4.3 源码精读

**① 请求字段：两端都新增 `return_token_ids` 开关。**

[protocol.py:L345](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/protocol.py#L345) —— `CompletionRequest` 新增 `return_token_ids: bool = False`（completions 端点唯一的 token id 开关，completions 没有 `return_prompt_token_ids` 请求字段）。

[protocol.py:L761-L762](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/protocol.py#L761-L762) —— `ChatCompletionRequest` 在既有的 `return_prompt_token_ids` 旁边新增 `return_token_ids`（chat 端点两个开关并存）。

**② 响应字段：每个 choice 多出 `token_ids` / `prompt_token_ids`，且 `None` 时被剔除。**

[protocol.py:L430-L441](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/protocol.py#L430-L441) —— `CompletionResponseChoice` 的 `token_ids` / `prompt_token_ids` 字段及其 `_serialize`：当为 `None` 时 `pop` 掉，保证默认 JSON 与 OpenAI 一致。

[protocol.py:L1072-L1084](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/protocol.py#L1072-L1084) —— `ChatCompletionResponseChoice` 同样新增 `token_ids` 并在 `None` 时 `pop`（其中 `prompt_token_ids` 是既有字段）。

> 设计要点：用 Pydantic 的 `@model_serializer(mode="wrap")` 包装默认序列化、再做条件 `pop`，是 SGLang 让「可选扩展字段」默认不污染响应的通用手法（`hidden_states`、`meta_info` 都沿用同一模式）。

**③ completions 端点：转换 + 非流式 + 流式。**

[serving_completions.py:L127](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/serving_completions.py#L127) —— 转换时把 `return_token_ids` 直接当成内部的 `return_prompt_token_ids` 传入，复用既有「回传 prompt token id」的链路（这就是 completions 没有独立 `return_prompt_token_ids` 字段也能回传 prompt token id 的原因）。

[serving_completions.py:L572-L579](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/serving_completions.py#L572-L579) —— 非流式响应里，从 `ret_item["output_ids"]` 取输出 token id、从 `ret_item["prompt_token_ids"]` 取 prompt token id 填入 choice。

[serving_completions.py:L318-L331](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/serving_completions.py#L318-L331) —— 流式响应里，按 `incremental_streaming_output` 开关决定每个 chunk 带「增量」还是「全量」token id，并在首个 chunk 附带 `prompt_token_ids`。

**④ chat 端点：转换 + 非流式回填 + 流式拒绝。**

[serving_chat.py:L780-L781](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/serving_chat.py#L780-L781) —— 转换时用 `request.return_prompt_token_ids or request.return_token_ids` 合并两个开关。

[serving_chat.py:L1547-L1553](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/serving_chat.py#L1547-L1553) —— 非流式响应里取出 `choice_token_ids = ret_item["output_ids"]`，随后作为 `token_ids=` 填入 choice。

[serving_chat.py:L685-L690](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/serving_chat.py#L685-L690) —— 流式分支直接 `raise ValueError`，拒绝 `return_token_ids` + stream 的组合。这正是 chat 与 completions 流式行为不同的来源。

#### 4.4.4 代码实践

**实践目标**：验证 `return_token_ids` 在两个端点的非流式回传，并亲测两端流式行为**不同**（chat 报错、completions 正常）。

**操作步骤**（**示例代码**，需先按 4.3.4 启动 `sglang serve`）：

```python
# return_token_ids.py （示例代码）
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:30000/v1", api_key="EMPTY")

# ---- (a) /v1/completions 非流式 ----
c = client.completions.create(
    model="default",
    prompt="The capital of France is",
    max_tokens=8,
    temperature=0.0,
    extra_body={"return_token_ids": True},   # SGLang 扩展字段
)
ch = c.choices[0]
print("[comp] token_ids        =", ch.token_ids)          # 期望: 非空 list[int]
print("[comp] prompt_token_ids =", ch.prompt_token_ids)   # 期望: 非空 list[int]

# ---- (b) /v1/chat/completions 非流式 ----
r = client.chat.completions.create(
    model="default",
    messages=[{"role": "user", "content": "What is 2+2? Answer with a number."}],
    max_tokens=8,
    temperature=0.0,
    extra_body={"return_token_ids": True},
)
print("[chat] token_ids        =", r.choices[0].token_ids)
print("[chat] prompt_token_ids =", r.choices[0].prompt_token_ids)

# ---- (c) 流式：chat 报错 vs completions 正常 ----
try:
    list(client.chat.completions.create(
        model="default",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        extra_body={"return_token_ids": True},
    ))
    print("[chat stream] 未报错（与预期不符）")
except Exception as e:
    print("[chat stream] 报错（符合预期）:", type(e).__name__)
```

**需要观察的现象**：

- (a)(b) 非流式响应里出现 `token_ids`（输出 token）与 `prompt_token_ids`（输入 token），且二者长度分别与 `usage.completion_tokens` / `usage.prompt_tokens` 数量级一致。
- (c) **chat 流式 + `return_token_ids` 会报错**（`ValueError` 经 HTTP 返回错误体）；而把同样请求换成 `client.completions.create(..., stream=True, extra_body={"return_token_ids": True})` 则**不会报错**，每个 chunk 里 `token_ids` 是增量片段，逐块拼起来等于完整输出序列。

**预期结果**：确认「非流式两端都回传、流式 chat 拒绝而 completions 支持」这一**不对称**行为。注意：有的 sglang 版本/客户端支持把扩展字段放在顶层（直接 `return_token_ids=True`）而非 `extra_body`，具体传参方式请以**本地验证**为准；核心是能观察到 token id 列表被回传。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `token_ids` / `prompt_token_ids` 默认是 `None` 还要在序列化时 `pop` 掉，而不是干脆不定义这两个字段？

> **答案**：为了保持与官方 OpenAI 协议的**字节级兼容**。官方客户端不认识 `token_ids`；若默认就输出它，可能让某些严格校验的客户端报错。用「字段存在但 `None` 时 `pop`」的模式，默认响应里完全看不到它，只有显式 `return_token_ids=true` 才出现——既支持了扩展，又不破坏兼容。

**练习 2**：同样是流式，为什么 completions 支持 `return_token_ids` 而 chat 不支持？

> **答案**：两者的流式响应结构不同。completions 的流式 choice 直接有 `text`/`token_ids` 字段，可以逐 chunk 切增量 token id；chat 的流式用的是 OpenAI 的 `delta` 增量消息结构，没有合适的位置承载 token id 序列，强行支持会破坏协议形状，因此 chat 在转换阶段直接 `raise ValueError` 拒绝。

**练习 3**：completions 端点没有 `return_prompt_token_ids` 请求字段，为什么 `return_token_ids=true` 时也能回传 `prompt_token_ids`？

> **答案**：在 `serving_completions.py` 的转换里，直接把 `return_token_ids` 的值赋给内部的 `return_prompt_token_ids`（`return_prompt_token_ids=request.return_token_ids`），复用了「回传 prompt token id」的既有链路；运行时会据此一并算出 `prompt_token_ids`，再在响应里同时填入 `token_ids` 与 `prompt_token_ids`。

---

### 4.5 Runtime/Engine 前端工厂与 sglang.Engine 的命名空间解析

#### 4.5.1 概念说明

最后一个容易混淆的点：`sglang.Engine` 这个名字。在 [lang/api.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/lang/api.py) 里有一个 `Engine` 函数，在 `srt/entrypoints/engine.py` 里又有一个 `Engine` 类，它们是什么关系？`import sglang` 之后 `sglang.Engine` 到底指向谁？

这其实是 SGLang 的「前端工厂」设计：

- 前端 `lang/api.py` 面向 DSL 用户，提供 `function/gen/select` 等原语，也提供 `Engine`（构造同进程运行时）和 `Runtime`/`RuntimeEndpoint`（连接到一个已运行的 HTTP 服务作为后端）这两个工厂。
- 运行时 `srt/entrypoints/engine.py` 才是真正的 `Engine` 类实现。
- `__init__.py` 在装配公共 API 时，**先**把前端那套全部导入，**再**用运行时的 `Engine` 覆盖掉 `Engine` 这个名字，使得 `sglang.Engine` 默认指向运行时引擎。

#### 4.5.2 核心流程

`import sglang` 时发生的事（按 `__init__.py` 的顺序）：

1. 导入前端 API（含前端的 `Engine`、`Runtime`、`RuntimeEndpoint`、`gen`、`select`、`function` 等）。
2. 用懒导入 `Engine = LazyImport("sglang.srt.entrypoints.engine", "Engine")` **重新绑定** `Engine`。
3. 结果：`sglang.Engine` 是一个懒导入对象，首次调用时才真正 import 并实例化运行时 `Engine` 类；`sglang.Runtime`、`sglang.RuntimeEndpoint`、`sglang.gen` 等仍是前端那套。

为什么前端 `lang/api.py` 里也保留一个 `Engine` 函数？因为它要让纯前端用户（只用 `@function` + `gen`）也能方便地拿到一个运行时实例作为默认后端，所以做了一个转发壳。

#### 4.5.3 源码精读

先看前端的两个工厂函数，它们都做了「懒导入 + 转发」，避免在不使用时强制 import 重型运行时依赖：

[lang/api.py:L35-L46](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/lang/api.py#L35-L46) —— `Runtime` 与 `Engine` 两个前端工厂，函数体内才 import 真正的类。

`Runtime`/`RuntimeEndpoint` 用于「连接到一个已经跑起来的 HTTP 服务」，它通过 HTTP 调用 `/get_model_info`、`/generate` 等端点，是前端 DSL 用来驱动远程后端的方式：

[runtime_endpoint.py:L26-L54](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/lang/backend/runtime_endpoint.py#L26-L54) —— `RuntimeEndpoint` 在构造时通过 HTTP 拉取 `/get_model_info`，本质是一个「指向远程服务」的后端。

前端 DSL 的核心原语也都来自这个文件：

[lang/api.py:L23-L32](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/lang/api.py#L23-L32) —— `@function` 装饰器，把普通函数包成 `SglFunction`（可追踪、可解释执行）。

[lang/api.py:L75-L139](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/lang/api.py#L75-L139) —— `gen(...)` 原语，返回一个 `SglGen` 表达式（注意：是「表达式」而非立即执行）。

`set_default_backend` 决定前端 DSL 默认用哪个后端（嵌入式 Engine 还是远程 RuntimeEndpoint）：

[lang/api.py:L49-L51](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/lang/api.py#L49-L51) —— `set_default_backend` 把后端写入 `global_config.default_backend`。

现在看关键的命名空间解析。`__init__.py` 先导入前端那套（第 36–59 行，其中第 37 行就导入了前端的 `Engine`），然后在第 79 行用运行时 `Engine` 覆盖：

[__init__.py:L34-L60](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/__init__.py#L34-L60) —— 导入前端 API（含前端 `Engine`）与 `RuntimeEndpoint`。

[__init__.py:L77-L79](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/__init__.py#L77-L79) —— 用 `LazyImport` 把 `Engine` 和 `ServerArgs` 重新绑定到运行时实现。这一行是「`sglang.Engine` 指向运行时引擎」的决定性步骤。

> 结论：对绝大多数用户而言，`sglang.Engine` 就是运行时引擎 `sglang.srt.entrypoints.engine.Engine`；只有在显式 `from sglang.lang.api import Engine` 时，拿到的才是前端那个转发壳。两者的最终效果一致（都实例化运行时 Engine），但默认入口是运行时版本。

#### 4.5.4 代码实践

**实践目标**：验证 `sglang.Engine` 的命名空间解析结果，理解前端工厂与运行时引擎的关系。

**操作步骤**（**示例代码**，可在不加载模型的情况下运行）：

```python
# example_namespace.py （示例代码）
import sglang

# 1) sglang.Engine 是什么？
print(type(sglang.Engine))            # 期望：LazyImport 相关对象
print(sglang.Engine.__name__ if hasattr(sglang.Engine, "__name__") else "n/a")

# 2) 显式导入前端壳对比
from sglang.lang.api import Engine as FrontendEngine
print("sglang.Engine is lang.api.Engine ?", sglang.Engine is FrontendEngine)  # 期望 False

# 3) RuntimeEndpoint 是前端连 HTTP 的后端（看类来源即可，不必真正连接）
print(sglang.RuntimeEndpoint.__module__)
```

**需要观察的现象**：

- `sglang.Engine` 不是前端的 `Engine` 函数（两者 `is` 比较为 `False`），而是指向运行时类。
- `sglang.RuntimeEndpoint` 来自 `sglang.lang.backend.runtime_endpoint`，即它属于前端。

**预期结果**：打印出表明 `sglang.Engine` → 运行时引擎、`RuntimeEndpoint` → 前端后端的结论。不同 sglang 版本下 `type(sglang.Engine)` 的具体 repr 可能略有差异，以**本地验证**为准；关键是「二者不是同一个对象」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `__init__.py` 要「先导入前端 Engine，再用运行时 Engine 覆盖」，而不是直接只导入运行时 Engine？

> **答案**：因为前端 API（`gen/select/function/assistant/...`）是公共 API 的一部分，必须导出；而 `Engine` 这个名字前端和运行时都想用。SGLang 的取舍是：前端整套原语照常导出，但 `Engine` 这个高频名字默认让位给运行时引擎（多数用户要的是运行时），靠后的 `LazyImport` 覆盖了靠前的前端版本。

**练习 2**：前端 DSL 想驱动「另一个机器上正在运行的 sglang serve」，应该用 `RuntimeEndpoint` 还是直接 `sglang.Engine`？

> **答案**：用 `RuntimeEndpoint`（或 `Runtime`）。`sglang.Engine` 会在当前进程里拉起整套运行时并占用本地 GPU，不适合连远程；`RuntimeEndpoint` 通过 HTTP 连到远程服务的 `/generate` 等端点，是「远程后端」的正确选择。

---

## 5. 综合实践

把本讲的两条入口串起来对比。**目标**：用同一段聊天 prompt，分别走 (a) OpenAI SDK 连 `sglang serve`、(b) 直接 `sglang.Engine`，对比返回结构，并粗略比较两者的吞吐差异；最后顺带用 `return_token_ids` 验证「HTTP 路径也能回传 token id」。

**步骤**：

1. **启动 HTTP 服务**（终端 A）：

   ```bash
   python -m sglang.launch_server --model-path Qwen/Qwen2.5-0.5B --port 30000
   ```

2. **编写对比脚本**（**示例代码**）：

   ```python
   # compare_entrypoints.py （示例代码）
   import time
   from openai import OpenAI
   import sglang

   PROMPT = "请用三句话解释什么是 KV 缓存。"
   N = 5  # 重复次数，用于估算吞吐

   # ---- 路径 A：HTTP + OpenAI 兼容（附带 return_token_ids）----
   client = OpenAI(base_url="http://127.0.0.1:30000/v1", api_key="EMPTY")
   t0 = time.perf_counter()
   for _ in range(N):
       r = client.chat.completions.create(
           model="default",
           messages=[{"role": "user", "content": PROMPT}],
           temperature=0.0,
           max_tokens=64,
       )
   dt_http = time.perf_counter() - t0
   print("[HTTP ]", r.choices[0].message.content[:40], "...")
   print("[HTTP ] 结构类型:", type(r).__name__)
   # 单独发一次带 return_token_ids 的请求，验证 token id 回传
   r_ids = client.chat.completions.create(
       model="default",
       messages=[{"role": "user", "content": "Say hello."}],
       max_tokens=8,
       extra_body={"return_token_ids": True},
   )
   print("[HTTP ] token_ids =", r_ids.choices[0].token_ids)

   # ---- 路径 B：同进程 Engine ----
   with sglang.Engine(model_path="Qwen/Qwen2.5-0.5B") as engine:
       t0 = time.perf_counter()
       last = None
       for _ in range(N):
           last = engine.generate(
               prompt=PROMPT,
               sampling_params={"temperature": 0.0, "max_new_tokens": 64},
           )
       dt_engine = time.perf_counter() - t0
       print("[Engine]", last["text"][:40], "...")
       print("[Engine] 结构类型:", type(last).__name__)

   print(f"总耗时  HTTP={dt_http:.2f}s  Engine={dt_engine:.2f}s")
   ```

3. **观察与思考**：

   - **返回结构差异**：路径 A 拿到的是 OpenAI 的 `ChatCompletion` 对象（`.choices[0].message.content`）；路径 B 拿到的是普通 `dict`（`out["text"]`、`out["meta_info"]`）。两者携带的核心信息（文本、token 数）一致，但访问方式不同。
   - **token id 回传**：路径 A 打开 `return_token_ids` 后，`choices[0].token_ids` 直接给出整数序列；路径 B（嵌入式 `Engine`）默认就在 `meta_info` 里携带 token 统计，但具体字段形态以本地版本为准。
   - **吞吐差异**：理论上同进程 `Engine` 省去了 HTTP 序列化与网络往返，单次小请求的固定开销更低。用 N 次请求的总耗时可以粗略对比。

吞吐可以用「每秒生成 token 数」来量化：

\[
\text{throughput} = \frac{N \times \text{completion\_tokens}}{\text{总耗时（秒）}}
\]

> 注意：本实践的精确数字依赖你的硬件、模型大小、是否命中 CUDA graph 等，**请以本地实测为准**。本实践的重点是「结构对比」与「理解两种入口的差异」，而非追求绝对数字。

**验收标准**：

- 两条路径都成功返回非空文本。
- 你能说清楚：HTTP 路径多走了 `OpenAIServingChat.handle_request → _convert_to_internal_request`（渲染 chat template），而 Engine 路径直接构造 `GenerateReqInput`；但二者都汇聚到 `tokenizer_manager.generate_request`。
- 你能说清楚 `return_token_ids` 在非流式下回传 `token_ids` + `prompt_token_ids`，且 chat 流式会拒绝而 completions 流式会逐块回传增量。

## 6. 本讲小结

- SGLang 有两类请求入口：**HTTP + OpenAI 兼容协议**（`/v1/chat/completions` 等）与**同进程嵌入式 API**（`sglang.Engine`），分别面向跨机器/多语言客户端与单机低开销场景。
- 两条路径在源码层**汇聚于同一个方法** `tokenizer_manager.generate_request(GenerateReqInput, ...)`；入口的多样性在 `GenerateReqInput` 这一层数据结构上被抹平。
- `Engine` 类在主进程拉起 Scheduler/Detokenizer 子进程，`generate/encode` 等方法把参数打包成 `GenerateReqInput` 后交给 `tokenizer_manager`，无需起 HTTP 服务。
- OpenAI 兼容协议由 `protocol.py` 的 Pydantic 模型定义形态、由 `serving_chat.py`/`serving_completions.py` 等 handler 实现行为；`serving_base.handle_request` 提供统一的「校验→转换→分流」骨架。
- `return_token_ids` 是新增的请求开关，打开后响应 choice 会多出 `token_ids`（输出）与 `prompt_token_ids`（输入）两个整数序列；默认为 `None` 并在序列化时被 `pop`，保持与官方 OpenAI 协议兼容。
- 该开关两端流式行为**不对称**：`/v1/completions` 流式支持（逐块回传增量 token id），`/v1/chat/completions` 流式直接 `raise ValueError` 拒绝。
- `EngineBase` 抽象让 HTTP 引擎与嵌入式引擎接口形状一致，可在不改业务代码的前提下切换部署形态。
- `sglang.Engine` 这个名字经过 `__init__.py` 的命名空间解析，默认指向运行时引擎 `sglang.srt.entrypoints.engine.Engine`；前端 `lang/api.py` 还提供 `Runtime`/`RuntimeEndpoint` 用于连接远程 HTTP 服务。

## 7. 下一步学习建议

- **进入运行时内部**：本讲止步于「入口汇聚点」`tokenizer_manager.generate_request`。下一单元（u3，服务端架构）会带你走进 `TokenizerManager → Scheduler → DetokenizerManager` 的进程拓扑与请求生命周期，建议接着读 [tokenizer_manager.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/managers/tokenizer_manager.py)。
- **理解请求对象**：通读 [io_struct.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/managers/io_struct.py) 中 `GenerateReqInput` 的字段定义（注意其中的 `return_prompt_token_ids` / `output_ids` 等字段，正是 `return_token_ids` 能回传 token id 的底层来源），它是贯穿整个运行时的「通用语言」。
- **前端 DSL**：如果你对 `@function/gen/select` 这种声明式写法感兴趣，第 2 单元（u2）会讲前端如何把程序追踪成 IR 再解释执行。
- **想动手扩展入口**：阅读 [serving_base.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/entrypoints/openai/serving_base.py) 的 `handle_request`，理解新增一个 OpenAI 兼容端点需要实现哪些抽象方法（`_convert_to_internal_request` / `_handle_streaming_request` / `_handle_non_streaming_request`）——`return_token_ids` 正是顺着这套骨架在两个 handler 里落地的，可作为学习范例。

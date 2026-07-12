# 推理引擎抽象与协议

## 1. 本讲目标

本讲是「推理引擎」单元的第一篇。学完本讲，你应当能够：

- 说清 ms-swift 的推理引擎为什么需要一个统一抽象 `BaseInferEngine`，以及它定义了哪两个核心接口。
- 理解「两层继承」结构：纯抽象 `BaseInferEngine` → 带共享实现的 `InferEngine` → 具体后端引擎（Transformers/Vllm/…）与远端客户端 `InferClient`。
- 掌握推理输入的两件套：`InferRequest`（问什么）与 `RequestConfig`（怎么生成），并能区分它们与 OpenAI 线上协议的关系。
- 看懂输出 `ChatCompletionResponse` 的字段结构，并能逐字段对上 OpenAI Chat Completions API。
- 自己动手用 `TransformersEngine` 跑一次推理，打印并解读返回结构。

## 2. 前置知识

本讲建立在 u3-l3「Template 体系与对话格式」之上，复用以下概念（不重复展开）：

- **Template（对话模板）**：把 `messages` 对话翻译成 token 序列的层。推理引擎**内部**仍然依赖 template 来编码输入、解码输出，本讲的 `InferEngine.__init__` 第一个参数就是 `template`。
- **messages 格式**：`[{role, content}, ...]` 的对话列表，是 `InferRequest` 的核心字段。
- **processor / model_meta / model_info**：模型加载产物（u3-l1），推理引擎在初始化时从中取出 `max_model_len`、`task_type` 等信息。

另外需要一点背景：**OpenAI Chat Completions API** 是当今大模型服务的事实标准（`POST /v1/chat/completions`，请求含 `messages/temperature/max_tokens`，响应含 `choices/usage`）。ms-swift 的推理协议基本是对齐它的，本讲会反复做字段对照。

一句话直觉：**推理引擎 = 把「一段 messages 对话」变成「一段模型生成的回答」的可替换黑盒**。不同的后端（原生 transformers、vllm、sglang、lmdeploy，甚至远端 HTTP 服务）都长得一样，调用方式完全相同——这就是抽象的价值。

## 3. 本讲源码地图

本讲涉及的核心文件都在 `swift/infer_engine/` 目录下：

| 文件 | 作用 |
| --- | --- |
| `swift/infer_engine/base.py` | 定义纯抽象基类 `BaseInferEngine`，只声明 `infer` 与 `infer_async` 两个抽象方法。 |
| `swift/infer_engine/infer_engine.py` | 定义中间基类 `InferEngine`，提供共享实现：构造（吃 template）、默认 `infer()` 批处理、tqdm、metrics、max_tokens 默认值、logprobs、finish_reason 等。 |
| `swift/infer_engine/protocol.py` | 推理协议数据类全家桶：`InferRequest`、`RequestConfig`、`ChatCompletionResponse` 及其嵌套类型，还有面向 OpenAI 线上协议的 `ChatCompletionRequest` 等。 |
| `swift/infer_engine/infer_client.py` | `InferClient`：继承 `InferEngine`，但不在本地跑模型，而是通过 HTTP 调用一个已部署的 OpenAI 兼容服务。 |
| `swift/infer_engine/transformers_engine.py` | `TransformersEngine`：基于原生 transformers 的具体引擎，本讲综合实践的落地实现。 |
| `examples/infer/demo.py` / `tests/infer/test_transformers_engine.py` | 真实使用样例，是综合实践的模板。 |

## 4. 核心概念与源码讲解

### 4.1 BaseInferEngine 抽象与两层继承

#### 4.1.1 概念说明

为什么需要抽象？ms-swift 支持四种推理后端（transformers / vllm / sglang / lmdeploy），它们的底层实现天差地别（transformers 是逐 token 自回归；vllm 是 PagedAttention 引擎；sglang/lmdeploy 各有自家 runtime）。此外，用户也可能不本地跑模型，而是调用一个已经部署好的 HTTP 服务。

如果上层代码（pipeline、CLI、评测、GRPO rollout）要直接面对这五种差异，会写满 `if backend == 'vllm': ...`。于是 ms-swift 把「输入对话 → 输出回答」这件事抽象成一个统一接口 `BaseInferEngine`，所有后端都实现它。上层只认这个接口，后端可自由替换。

这个抽象的设计要点是**两个正交的接口维度**：

- `infer()`：**同步、批量**。一次吃一组请求 `List[InferRequest]`，返回一组响应。适合「跑完一个数据集」「批量评测」。
- `infer_async()`：**异步、单条**。一次吃一个请求，返回一个 `await`-able 结果或异步流。适合「流式输出」「高并发服务」。

这二者并非独立：默认实现里，批量的 `infer()` 就是把每条请求各起一个 `infer_async()` 协程再并发收集（见 4.1.2）。

#### 4.1.2 核心流程

推理引擎采用「两层继承 + 具体实现」的三层结构：

```
BaseInferEngine (ABC, base.py)        ← 纯接口：infer / infer_async
        ↑
InferEngine (infer_engine.py)        ← 共享实现：构造、批处理、tqdm、metrics、默认值
        ↑
┌───────┴──────────────────────────────────────────┐
TransformersEngine  VllmEngine  SglangEngine  LmdeployEngine   InferClient
(本地 transformers)  (本地 vllm)  ...                                  (远端 HTTP)
```

- **第一层 `BaseInferEngine`**：只声明两个抽象方法，不含任何实现，是「契约」。
- **第二层 `InferEngine`**：把公共逻辑集中起来。它的 `infer()` 默认实现是一条「批处理流水线」：
  1. 为每条请求构造一个 `infer_async(...)` 协程任务；
  2. 用 `_batch_infer_stream` 并发执行这些协程（`asyncio.gather`）；
  3. 期间用 `tqdm` 显示进度、用 `metrics` 收集统计；
  4. 流式模式下把每个异步迭代器桥接成同步迭代器（`async_iter_to_iter`）。

  这意味着**子类通常只需实现 `infer_async()`**，`infer()` 白捡。
- **第三层具体引擎**：
  - 本地引擎（如 `TransformersEngine`）实现 `infer_async()`，内部调用模型 forward。
  - `InferClient` 比较特殊——它**不本地跑模型**，而是把请求序列化成 JSON，POST 给一个 OpenAI 兼容服务，再把返回 JSON 反序列化回 `ChatCompletionResponse`。调用方完全无感。

#### 4.1.3 源码精读

**纯抽象基类 `BaseInferEngine`**——整个抽象的契约就这几十行：

[swift/infer_engine/base.py:9-18](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/base.py#L9-L18) 定义类与同步批量接口 `infer()`，返回 `List[ChatCompletionResponse]`（或流迭代器）：

```python
class BaseInferEngine(ABC):

    @abstractmethod
    def infer(self,
              infer_requests: List[InferRequest],
              request_config: Optional[RequestConfig] = None,
              metrics: Optional[List[Metric]] = None,
              *,
              use_tqdm: Optional[bool] = None,
              **kwargs) -> List[Union[ChatCompletionResponse, Iterator[ChatCompletionStreamResponse]]]:
```

[swift/infer_engine/base.py:38-42](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/base.py#L38-L42) 声明异步单条接口 `infer_async()`（注意是 `async def`）：

```python
    @abstractmethod
    async def infer_async(self,
                          infer_request: InferRequest,
                          request_config: Optional[RequestConfig] = None,
                          **kwargs) -> Union[ChatCompletionResponse, AsyncIterator[ChatCompletionStreamResponse]]:
```

两者都用 `@abstractmethod` 标注，`BaseInferEngine` 继承 `ABC`，所以它不能直接实例化——必须由子类实现。

**第二层 `InferEngine` 的构造与共享逻辑**——注意构造函数**只收一个 template**，并从中「榨取」出模型信息：

[swift/infer_engine/infer_engine.py:21-35](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/infer_engine.py#L21-L35) 展示了引擎与 template 的强绑定（`model_info`/`max_model_len`/`task_type` 都来自 template 背后的 processor）：

```python
class InferEngine(BaseInferEngine, ProcessorMixin):

    def __init__(self, template: Template):
        processor = template.processor
        self.template = template
        ...
        self.max_model_len = self.model_info.max_model_len
        self.task_type = self.model_info.task_type
```

[swift/infer_engine/infer_engine.py:176-188](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/infer_engine.py#L176-L188) 是默认 `infer()` 实现——它把批量拆成若干 `infer_async` 协程并发跑：

```python
    def infer(self, infer_requests, request_config=None, metrics=None, *, use_tqdm=None, **kwargs):
        if request_config is None:
            request_config = RequestConfig()
        tasks = [self.infer_async(infer_request, request_config, **kwargs) for infer_request in infer_requests]
        if use_tqdm is None:
            use_tqdm = not request_config.stream and len(infer_requests) > 1
        return self._batch_infer_stream(tasks, request_config.stream, use_tqdm, metrics)
```

注意第三行：批量 `infer` 的本质就是「对每条请求调用子类的 `infer_async`，再收集」。所以子类实现 `infer_async` 即可获得批量能力。

**第三层 `TransformersEngine`** 的 `infer_async` 用「生产者线程 + asyncio.Queue」把同步的 `model.generate` 包装成异步接口：

[swift/infer_engine/transformers_engine.py:467-498](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/transformers_engine.py#L467-L498) 把请求丢进 `self._queue`，由后台 `_infer_worker` 线程消费，结果通过 `asyncio.Queue` 回传：

```python
    async def infer_async(self, infer_request, request_config=None, *, adapter_request=None, pre_infer_hook=None):
        if request_config is None:
            request_config = RequestConfig()
        queue = asyncio.Queue()
        self._queue.put((infer_request, {...}, (queue, asyncio.get_event_loop())))
        await asyncio.sleep(0)
        if self._task_thread is None:
            self._start_infer_worker()
        ...
        return await queue.get()   # 非流式：直接等单个结果
```

**`InferClient` 是「反向」实现**——它继承 `InferEngine`，但 `infer_async` 不跑模型，而是发 HTTP：

[swift/infer_engine/infer_client.py:120-158](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/infer_client.py#L120-L158) 向 `{base_url}/chat/completions` POST，再用 `from_dict` 把 JSON 还原成 `ChatCompletionResponse`：

```python
    async def infer_async(self, infer_request, request_config=None, *, model=None):
        ...
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        request_data = self._prepare_request_data(model, infer_request, request_config)
        ...
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=request_data, **self._get_request_kwargs()) as resp:
                resp_obj = await resp.json()
                ...
                return from_dict(ChatCompletionResponse, resp_obj)
```

正因为 `InferClient` 与本地引擎实现的是**同一个接口**，所以「本地推理」与「调远端服务」对上层是透明的——这也正是 u8-l2「部署与服务化」能把同一套推理代码同时用于本地和线上服务的根基。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：通过「类型与调用链」确认所有后端确实实现同一接口，而不依赖 GPU。
2. **操作步骤**：
   - 在 `swift/infer_engine/__init__.py` 的 `_import_structure`（[swift/infer_engine/__init__.py:18-29](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/__init__.py#L18-L29)）中数一下导出了哪几个引擎类。
   - 在仓库内确认继承关系：`TransformersEngine(InferEngine)`、`VllmEngine(InferEngine)`、`InferClient(InferEngine)`、`InferEngine(BaseInferEngine)`。
3. **需要观察的现象**：所有具体类最终都汇聚到 `BaseInferEngine`；`InferClient` 与 `TransformersEngine` 是「兄弟」，都继承 `InferEngine`。
4. **预期结果**：得到一张「`BaseInferEngine` → `InferEngine` → {Transformers, Vllm, Sglang, Lmdeploy, InferClient}」的继承树。
5. 待本地验证：可用 `python -c "from swift.infer_engine import TransformersEngine, InferClient, InferEngine, BaseInferEngine; print(TransformersEngine.__mro__)"` 打印方法解析顺序确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BaseInferEngine` 要把 `infer`（同步批量）和 `infer_async`（异步单条）拆成两个抽象方法，而不是只留一个？

> **参考答案**：因为这两个接口服务于不同场景且实现路径不同。批量同步适合离线评测/数据集推理（要 tqdm、要聚合 metrics）；异步单条适合在线服务/流式输出（要 `async for`、要高并发）。把它们都提到抽象层，让上层既能批量调用、也能逐条异步调用，而后端只需各实现一次。

**练习 2**：`InferClient` 继承 `InferEngine` 却不本地加载模型，它如何满足 `__init__(self, template)` 的构造契约？

> **参考答案**：`InferClient` 实际上重写了 `__init__`，接收的是 `host/port/api_key` 等连接参数（见 [swift/infer_engine/infer_client.py:16-41](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/infer_client.py#L16-L41)），并不调用 `InferEngine.__init__`。它复用的是 `InferEngine` 的 `infer()` 批处理逻辑与协议类型，而非「本地有模型」的前提——这是接口复用、实现替换的典型。

### 4.2 InferRequest / RequestConfig 协议

#### 4.2.1 概念说明

引擎的输入被有意拆成两个独立对象，对应「问什么」和「怎么答」两个正交维度：

- **`InferRequest`（问什么）**：承载对话内容本身。核心字段是 `messages`（对话列表），外加多模态资产 `images`/`audios`/`videos`、工具声明 `tools`、以及透传给模板的 `chat_template_kwargs`。它**不包含**任何「如何采样」的信息。
- **`RequestConfig`（怎么答）**：承载生成策略。`max_tokens`、`temperature`、`top_p`、`top_k`、`n`（生成几条）、`stream`（是否流式）、`stop`、`seed`、`logprobs` 等。它**不关心**对话内容。

这种拆分的好处：同一批对话可以用不同的采样参数反复跑；同一组采样参数也能套在不同对话上。它与 OpenAI API 的请求体结构同构（OpenAI 把它们合在一个 JSON 里，ms-swift 在 Python 层拆成两个 dataclass）。

注意：`protocol.py` 里还有一组「面向 HTTP 线上协议」的请求类 `ChatCompletionRequest` / `CompletionRequest` / `EmbeddingRequest`。它们 = `RequestConfig` + 多模态 mixin + 各自的语义 mixin（如 `model`/`messages`）。它们带一个 `.parse()` 方法，把自己**拆解回** `(InferRequest, RequestConfig)` 这对内部对象。也就是说，内部引擎只认 `InferRequest + RequestConfig`，HTTP 层负责翻译。

#### 4.2.2 核心流程

一次推理请求的输入组装流程：

```
HTTP 层:  ChatCompletionRequest(model, messages, temperature, ...)
                                  │  .parse()
                                  ▼
内部层:   InferRequest(messages, images, tools, ...)   +   RequestConfig(max_tokens, temperature, stream, ...)
                                  │
                                  ▼
                       engine.infer([infer_request], request_config)
```

- 若是程序直接调用引擎（本讲综合实践），用户**直接构造** `InferRequest` 与 `RequestConfig`，跳过 HTTP 层。
- 若是经部署服务（u8-l2），请求走 `ChatCompletionRequest` → `.parse()` 拆解 → 引擎。

`InferRequest.__post_init__` 还做一件贴心事：把 `images='http://...'` 这种「单个字符串」自动包成 `['http://...']` 列表，统一后续处理；并断言 `messages` 必须是 list。

#### 4.2.3 源码精读

**`InferRequest`** 是个普通 dataclass，字段很直观：

[swift/infer_engine/protocol.py:44-101](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/protocol.py#L44-L101) 定义请求体与 `__post_init__` 的归一化：

```python
@dataclass
class InferRequest:
    messages: Messages
    images: List[Union[str, Image.Image]] = field(default_factory=list)
    audios: List[str] = field(default_factory=list)
    videos: List[str] = field(default_factory=list)
    tools: Optional[List[Tool]] = None
    objects: Dict[str, Any] = field(default_factory=dict)
    chat_template_kwargs: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        for key in ['images', 'audios', 'videos']:
            val = getattr(self, key)
            if isinstance(val, str):
                setattr(self, key, [val])          # 单字符串 → 列表
        assert isinstance(self.messages, list), ...
```

注意 `messages` 是唯一必填、无默认值的字段，所以 `InferRequest([{'role':'user','content':'hi'}])` 这种位置参数写法可行（见 `examples/infer/demo.py`）。

**`RequestConfig`** 的字段几乎逐个对应 OpenAI 采样参数，且文档字符串明确指出默认值与 OpenAI **不一致**：

[swift/infer_engine/protocol.py:180-216](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/protocol.py#L180-L216) 列出生成参数：

```python
@dataclass
class RequestConfig:
    """NOTE: The following behavior is inconsistent with the OpenAI API.
    Default values for OpenAI: temperature=1., top_k=-1, top_p=1., repetition_penalty=1."""
    max_tokens: Optional[int] = None        # None: max_model_len - num_tokens
    temperature: Optional[float] = None     # None: use deploy_args
    top_k: Optional[int] = None
    top_p: Optional[float] = None
    repetition_penalty: Optional[float] = None
    num_beams: int = 1
    stop: Optional[List[str]] = field(default_factory=list)
    seed: Optional[int] = None
    stream: bool = False
    logprobs: bool = False
    top_logprobs: Optional[int] = None
    n: int = 1
    ...
    return_details: bool = False            # 额外返回 token_ids（非流式）
    structured_outputs_regex: Optional[str] = None   # vLLM 引导解码
```

关键差异：ms-swift 的 `temperature/top_p/top_k` 默认是 `None`（表示「用部署参数/模型默认」），而 OpenAI 默认是具体数值；`top_k` 和 `repetition_penalty` 是 HuggingFace 概念，OpenAI 原生 API 没有；`return_details`、`structured_outputs_regex` 则是 ms-swift 自有扩展。

**HTTP 请求类到内部对象的拆解**——`ChatCompletionRequest.parse()`：

[swift/infer_engine/protocol.py:361-368](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/protocol.py#L361-L368) 按字段名筛分成两个内部对象：

```python
    def parse(self) -> Tuple['InferRequest', 'RequestConfig']:
        data = asdict(self)
        res = []
        for cls_type in [InferRequest, RequestConfig]:
            parameters = set(f.name for f in fields(cls_type))
            _data = {k: v for k, v in data.items() if k in parameters}
            res.append(cls_type(**_data))
        return tuple(res)
```

这段正好印证 4.2.1 说的「HTTP 层翻译」：一个线上请求被拆成内部两件套。

#### 4.2.4 代码实践（无需 GPU，纯数据类）

1. **实践目标**：亲手构造 `InferRequest` 与 `RequestConfig`，观察字段与归一化行为。本实践**不需要加载模型**，纯 dataclass 操作，本地即可跑。
2. **操作步骤**：新建 `play_request.py`：

   ```python
   # 示例代码：本讲自编，用于观察协议数据类
   from swift.infer_engine import InferRequest, RequestConfig

   # 1) 构造纯文本请求：messages 是唯一必填项
   req = InferRequest(messages=[{'role': 'user', 'content': '你好'}])
   print('messages:', req.messages)
   print('images:', req.images)

   # 2) 构造带图像的请求：观察单字符串被自动包成列表
   req_mm = InferRequest(
       messages=[{'role': 'user', 'content': '<image>这是什么？'}],
       images='https://example.com/a.jpg')
   print('mm images(应已是列表):', req_mm.images)

   # 3) 构造采样配置
   cfg = RequestConfig(max_tokens=64, temperature=0, stream=False, n=1)
   print('config:', cfg)
   ```
3. **需要观察的现象**：`req_mm.images` 由传入的字符串变成了单元素列表；`cfg` 各字段值与构造参数一致。
4. **预期结果**：打印 `mm images(应已是列表): ['https://example.com/a.jpg']`，验证 `__post_init__` 的归一化。
5. 待本地验证：实际运行输出可能与模型/版本无关，但请在本地确认 `InferRequest`/`RequestConfig` 的导入路径与字段名未变。

#### 4.2.5 小练习与答案

**练习 1**：`InferRequest` 为什么不把 `temperature`、`max_tokens` 这些也做成字段？

> **参考答案**：因为「问什么」和「怎么生成」是正交的两个维度，混在一起会让「同一对话用不同采样反复跑」这种需求难以表达。拆开后，`InferRequest` 只描述内容、`RequestConfig` 只描述策略，二者可独立复用与组合。

**练习 2**：`RequestConfig.temperature` 默认是 `None` 而非 `1.0`，这有什么含义？

> **参考答案**：`None` 表示「不覆盖、回退到部署参数/模型 generation_config 的默认值」。这是 ms-swift 与 OpenAI 的一个有意差异（OpenAI 默认 `1.0`）。在部署场景，`None` 让服务端用 `deploy_args` 决定温度，避免客户端强制覆盖。

### 4.3 ChatCompletion 响应结构与 OpenAI 对齐

#### 4.3.1 概念说明

引擎的输出统一是 `ChatCompletionResponse`（流式时是 `ChatCompletionStreamResponse` 的迭代）。它是一个嵌套 dataclass，**结构刻意对齐 OpenAI Chat Completions 响应**，目的是：

1. 上层（评测、GRPO rollout、用户代码）可以用同一套取值方式读结果，例如永远 `resp.choices[0].message.content`。
2. `InferClient` 能直接用 `from_dict` 把远端服务的 JSON 响应「灌」进同一个 dataclass，本地引擎与远端服务输出形态完全一致。

核心嵌套关系（自顶向下）：

```
ChatCompletionResponse
├── model: str
├── choices: List[ChatCompletionResponseChoice]
│   ├── index: int
│   ├── message: ChatMessage
│   │   ├── role: 'assistant'
│   │   ├── content: str
│   │   ├── tool_calls: Optional[List[...]]
│   │   └── reasoning_content: Optional[str]   # 思维链（推理模型）
│   ├── finish_reason: 'stop' | 'length' | None
│   └── logprobs: Optional[...]
├── usage: UsageInfo
│   ├── prompt_tokens: int
│   ├── completion_tokens: int
│   └── total_tokens: int
├── id: 'chatcmpl-...'
├── object: 'chat.completion'
└── created: int
```

流式版本把 `choices[].message` 换成 `choices[].delta`（`DeltaMessage`，增量片段），`object` 变成 `'chat.completion.chunk'`——与 OpenAI SSE 流的字段一一对应。

#### 4.3.2 核心流程

本地引擎装配一个响应的过程（以非流式 `_infer_full` 为例）：

```
model.generate(...)  →  generate_ids [batch, seq]
                          │  解码 + 去 pad + 计 logprobs
                          ▼
              逐样本构造 ChatCompletionResponseChoice
                          │  打包 choices + usage
                          ▼
              ChatCompletionResponse(model, choices, usage, ...)
```

- `usage` 由 `_get_usage_info(prompt_tokens, completion_tokens)` 算出。
- `finish_reason` 由 `_get_finish_reason(max_tokens, completion_tokens, is_finished)` 判定：已结束且达到上限为 `'length'`，正常结束为 `'stop'`，未结束为 `None`。
- 多模态额外信息（图像尺寸 `images_size`、`prompt_token_ids`）只在 `request_config.return_details=True` 时才填。

#### 4.3.3 源码精读

**顶层响应 `ChatCompletionResponse`**——字段名与 OpenAI 完全一致：

[swift/infer_engine/protocol.py:457-467](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/protocol.py#L457-L467) 定义顶层响应：

```python
@dataclass
class ChatCompletionResponse:
    model: str
    choices: List[ChatCompletionResponseChoice]
    usage: UsageInfo
    id: str = field(default_factory=lambda: f'chatcmpl-{random_uuid()}')
    object: str = 'chat.completion'
    created: int = field(default_factory=lambda: int(time.time()))
    prompt_token_ids: Optional[List[int]] = None
    prompt_logprobs: Optional[List] = None
    images_size: Optional[List[Tuple[int, int]]] = None
```

`object='chat.completion'`、`id='chatcmpl-...'` 都是 OpenAI 原汁原味的取值；`prompt_token_ids`/`images_size` 是 ms-swift 扩展字段。

**选项 `ChatCompletionResponseChoice` 与消息 `ChatMessage`**：

[swift/infer_engine/protocol.py:417-424](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/protocol.py#L417-L424) 定义单个选项：

```python
@dataclass
class ChatCompletionResponseChoice:
    index: int
    message: ChatMessage
    finish_reason: Literal['stop', 'length', None]
    logprobs: Optional[Dict[str, List[Dict[str, Any]]]] = None
    token_ids: Optional[List[int]] = None
    routed_experts: Optional[NumpyArray] = None
```

[swift/infer_engine/protocol.py:409-414](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/protocol.py#L409-L414) 定义消息体（含思维链字段）：

```python
@dataclass
class ChatMessage:
    role: Literal['system', 'user', 'assistant']
    content: Union[str, List[Dict[str, Any]], int, float, List[float]]
    tool_calls: Optional[List[ChatCompletionMessageToolCall]] = None
    reasoning_content: Optional[str] = None
```

`reasoning_content` 用于承载 DeepSeek-R1 这类推理模型的「思考过程」，是 OpenAI 后期也采用的概念；`tool_calls` 承载函数调用结果。

**用量 `UsageInfo`**：

[swift/infer_engine/protocol.py:383-387](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/protocol.py#L383-L387) 三项 token 计数：

```python
@dataclass
class UsageInfo:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
```

**流式响应**结构对称：

[swift/infer_engine/protocol.py:587-594](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/protocol.py#L587-L594) 流式顶层用 `'chat.completion.chunk'`：

```python
@dataclass
class ChatCompletionStreamResponse:
    model: str
    choices: List[ChatCompletionResponseStreamChoice]
    usage: Optional[UsageInfo] = None
    id: str = field(default_factory=lambda: f'chatcmpl-{random_uuid()}')
    object: str = 'chat.completion.chunk'
    created: int = field(default_factory=lambda: int(time.time()))
```

**本地引擎装配响应的现场**——`TransformersEngine._infer_full` 收尾处把生成结果打包：

[swift/infer_engine/transformers_engine.py:441-464](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/transformers_engine.py#L441-L464) 逐选项构造并组装：

```python
                choices.append(
                    ChatCompletionResponseChoice(
                        index=j,
                        message=ChatMessage(role='assistant', content=response, tool_calls=toolcall),
                        finish_reason=finish_reason,
                        logprobs=logprobs,
                        token_ids=token_ids))
        ...
        res.append(
            ChatCompletionResponse(
                model=self.model_name,
                choices=choices,
                usage=usage_info,
                prompt_token_ids=prompt_token_ids,
                images_size=images_size))
```

`finish_reason` 由 `_get_finish_reason` 给出，`response` 由 `template.decode_generate_ids` 解码——再次体现引擎与 template 的协作。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：跟踪响应装配链路，确认本地引擎产出的结构与 OpenAI 一致。
2. **操作步骤**：
   - 阅读 [swift/infer_engine/transformers_engine.py:399-465](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/transformers_engine.py#L399-L465)（`_infer_full`）。
   - 找到三处关键调用：`_get_usage_info`、`_get_finish_reason`、`template.decode_generate_ids`，分别说明它们贡献了响应的哪个字段。
3. **需要观察的现象**：`usage_info` 来自 prompt/completion token 计数；`finish_reason` 来自「是否结束 + 是否触顶」；`message.content` 来自 template 解码生成 token。
4. **预期结果**：能画出 `generate_ids → (decode, count, judge) → ChatCompletionResponse` 的字段映射表。
5. 待本地验证：无需运行，纯阅读即可。

#### 4.3.5 小练习与答案

**练习 1**：把 `ChatCompletionResponse` 与 OpenAI 官方 `/v1/chat/completions` 响应做对照，哪些字段是 ms-swift 额外加的？

> **参考答案**：与 OpenAI 一致的有 `id`、`object`、`created`、`model`、`choices`（含 `index/message/finish_reason/logprobs`）、`usage`（含 `prompt_tokens/completion_tokens/total_tokens`）。ms-swift 额外的有顶层 `prompt_token_ids`、`prompt_logprobs`、`images_size`，以及选项里的 `token_ids`、`routed_experts`（MoE 路由统计）。`message.reasoning_content` 是二者后期共同演进的概念。

**练习 2**：`finish_reason` 在什么情况下是 `'length'`、什么情况下是 `'stop'`、什么情况下是 `None`？

> **参考答案**：看 [swift/infer_engine/infer_engine.py:252-261](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/infer_engine.py#L252-L261) 的 `_get_finish_reason`：未结束时返回 `None`（流式中间块）；已结束且 `completion_tokens >= max_tokens` 返回 `'length'`（被截断）；已结束但未触顶返回 `'stop'`（命中停止符或 EOS）。

## 5. 综合实践

**任务**：用 `TransformersEngine` 端到端跑一次推理——构造 `InferRequest`、配置 `RequestConfig`、调用 `infer`、打印 `ChatCompletionResponse`，并把关键字段对上 OpenAI API。

本实践改编自官方样例 [examples/infer/demo.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/infer/demo.py) 与测试 [tests/infer/test_transformers_engine.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/tests/infer/test_transformers_engine.py)。

**步骤**：

1. 新建 `play_engine.py`（示例代码：本讲自编，整合自上述官方样例）：

   ```python
   import os
   os.environ['CUDA_VISIBLE_DEVICES'] = '0'

   from swift import TransformersEngine
   from swift.infer_engine import InferRequest, RequestConfig

   # ① 构造引擎：传入模型 id（字符串），引擎内部完成「加载模型 + 选 template」
   engine = TransformersEngine('Qwen/Qwen2.5-0.5B-Instruct', max_batch_size=4)

   # ② 构造输入：InferRequest（问什么）+ RequestConfig（怎么答）
   infer_requests = [
       InferRequest([{'role': 'user', 'content': 'hello, who are you?'}])
       for _ in range(2)
   ]
   request_config = RequestConfig(max_tokens=32, temperature=0)

   # ③ 调用统一的 infer 接口（与 vllm/sglang/lmdeploy 写法完全相同）
   resp_list = engine.infer(infer_requests, request_config)

   # ④ 解析 ChatCompletionResponse，逐字段对照 OpenAI
   resp = resp_list[0]
   print('id        :', resp.id)                 # 对应 OpenAI id
   print('object    :', resp.object)             # 'chat.completion'
   print('model     :', resp.model)
   print('content   :', resp.choices[0].message.content)   # 主要回答
   print('role      :', resp.choices[0].message.role)
   print('finish    :', resp.choices[0].finish_reason)     # 'stop' / 'length'
   print('usage     :', resp.usage)              # prompt/completion/total tokens
   print('n_choices :', len(resp.choices))
   ```

2. 运行（需 GPU 与可联网下载模型；模型较大时首次会下载权重）：

   ```bash
   python play_engine.py
   ```

3. **观察重点**：
   - `engine.infer(...)` 的调用签名与换用 `VllmEngine`/`SglangEngine` 时**完全一致**——这就是抽象的威力（结合 4.1）。
   - `resp.choices[0].message.content` 是最终回答；`resp.usage` 给出 token 计数；`resp.object == 'chat.completion'`。
   - 把打印结果与 OpenAI 官方 `/v1/chat/completions` 响应样例逐字段比对，应能一一对应（结合 4.3.5 练习 1）。

4. **进阶**：把 `request_config` 改成 `RequestConfig(max_tokens=32, temperature=0, stream=True)`，并把 `engine.infer(...)` 的返回当作迭代器 `for chunk in resp_list[0]: print(chunk.choices[0].delta.content, end='')`，观察流式 `ChatCompletionStreamResponse`（`object='chat.completion.chunk'`、`delta` 替代 `message`）。

5. **预期结果**：非流式打印出一段模型自我介绍，`finish_reason` 多为 `'stop'`，`usage` 三项 token 数为正。**待本地验证**：实际文本与 token 数取决于模型与硬件，但结构字段必须与上述一致——这正是本讲要建立的认知。

## 6. 本讲小结

- ms-swift 用 `BaseInferEngine` 把「对话 → 回答」抽象成两个正交接口：同步批量的 `infer()` 与异步单条的 `infer_async()`，所有后端都实现它。
- 继承是三层：纯抽象 `BaseInferEngine` → 共享实现 `InferEngine`（吃 template、批处理、tqdm、metrics、默认值）→ 具体引擎（Transformers/Vllm/Sglang/Lmdeploy）与远端 `InferClient`。
- `InferClient` 与本地引擎是「兄弟」，实现同一接口但走 HTTP；这让「本地推理」与「调远端服务」对上层透明。
- 输入拆成正交两件套：`InferRequest`（messages + 多模态资产）描述「问什么」，`RequestConfig`（max_tokens/temperature/stream…）描述「怎么答」；HTTP 层的 `ChatCompletionRequest` 经 `.parse()` 拆解回这对内部对象。
- 输出 `ChatCompletionResponse` 结构刻意对齐 OpenAI Chat Completions（`choices[].message`、`usage`、`object='chat.completion'`、`id='chatcmpl-...'`），并扩展了 `prompt_token_ids`/`images_size`/`token_ids` 等字段。
- `RequestConfig` 的采样参数默认值（`None`）与 OpenAI 不同，`top_k`/`repetition_penalty` 是 HF 概念——这是协议对齐中的有意差异。

## 7. 下一步学习建议

- **本单元下一篇 u6-l2「多后端推理引擎」**：进入 `VllmEngine`/`SglangEngine`/`LmdeployEngine`/`GRPOVllmEngine` 的具体实现差异，看它们如何在同一个 `BaseInferEngine` 契约下各自接入高性能 runtime。
- **u8-l2「部署与服务化」**：看 `swift deploy` 如何把引擎包成 OpenAI 兼容 HTTP 服务，与本讲的 `InferClient` 互为「服务端 / 客户端」两面。
- **u7-2「GRPO 算法核心」**：GRPO 的 rollout 会大量调用推理引擎批量采样，本讲的 `infer()` 批处理与 `RequestConfig.n`（一次生成多条）是那里的基石。
- 继续阅读 [swift/infer_engine/infer_engine.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/infer_engine/infer_engine.py) 中的 `_batch_infer_stream`、`async_iter_to_iter`、`set_default_max_tokens` 等方法，能更扎实地理解默认实现细节。

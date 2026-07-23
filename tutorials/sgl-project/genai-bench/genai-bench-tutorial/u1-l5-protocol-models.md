# 协议数据模型（protocol.py）

## 1. 本讲目标

本讲是入门单元的最后一篇，聚焦 genai-bench 中**最关键的一个文件**：`genai_bench/protocol.py`。

学完本讲后，你应该能够：

- 说出 `UserRequest` 及其子类（`UserChatRequest`、`UserEmbeddingRequest` 等）各自代表什么请求、有哪些关键字段；
- 说出 `UserResponse` 及其子类（`UserChatResponse` 等）如何承载一次请求的时序与计数结果；
- 看懂 `ExperimentMetadata` 的字段结构，理解它如何作为「一次实验的身份证」贯穿始终；
- 理解 **Pydantic** 在 genai-bench 中扮演的「统一数据契约」角色——为什么采样器、User、指标、分析这些互不相干的子系统，能通过同一组模型顺畅对话。

本讲只讲「数据长什么样」，不讲「数据怎么算」（指标计算留给 U4）。

## 2. 前置知识

### 2.1 为什么需要「协议」

genai-bench 内部有很多子系统：采样器负责「造请求」，User 负责「发请求、收响应」，指标模块负责「算结果」，分析模块负责「出报告」。这些模块由不同的人在不同时间编写，如果大家对「一个请求里到底有哪些字段」「一个响应里时间戳记在哪」各执一词，代码就会乱套。

`protocol.py` 的作用就是**把这些约定集中写在一个文件里**，所有子系统都 `from genai_bench.protocol import ...`，用它定义的类来传递数据。这样，一个模块的输出天然就是另一个模块能识别的输入——这就是「数据契约（data contract）」的含义。

### 2.2 Pydantic 基础（最小必要量）

genai-bench 用 **Pydantic v2**（`pydantic>=2.8.2`，见 [pyproject.toml:28](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L28)）来定义这些模型。你只需要掌握下面几个概念：

- **`BaseModel`**：继承它的类就是一个数据模型。你用类型注解声明字段，Pydantic 自动帮你做「校验 + 序列化」。

  ```python
  from pydantic import BaseModel

  class UserRequest(BaseModel):
      model: str          # 字段：必须是字符串
  ```

- **`Field(...)`**：给字段加描述、默认值等元信息。`...`（三个点，即 `Ellipsis`）表示**必填**；写成 `default=0` 表示默认值为 0。

- **类型校验**：构造对象时，Pydantic 会按注解检查类型，不合规就抛 `ValidationError`。例如把字符串传给 `int` 字段会报错。

- **序列化**：`model_dump()` 把对象转成 `dict`；`model_dump_json()` 把对象转成 JSON 字符串。后者是 genai-bench 把实验结果落盘的核心手段。

- **继承**：子类继承父类的全部字段，再追加自己的字段——`protocol.py` 大量使用这种「基类 + 子类」的家族结构。

- **几个类型提示**：
  - `Optional[int]` 与 `int | None`：等价，都表示「可以为 `None` 的整数」。
  - `Literal["a", "b"]`：取值只能是字符串列表里的某一个（枚举效果）。
  - `conint(ge=1)`：受约束的整数（constrained int），`ge=1` 表示「大于等于 1」。

有了这些，下面的源码你就能轻松读懂。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但会引用它被「使用」的几个真实位置，帮助你理解契约如何流动。

| 文件 | 作用 |
|------|------|
| [genai_bench/protocol.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py) | **本讲主角**。定义全部请求/响应/认证/实验元数据模型，是全局数据契约。 |
| [genai_bench/sampling/text.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py) | **请求模型的生产者**。`TextSampler` 在这里构造各种 `UserXxxRequest`。 |
| [genai_bench/user/base_user.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py) | **契约的消费者**。`import UserRequest, UserResponse`，`sample()` 取请求、`collect_metrics()` 收响应。 |
| [genai_bench/cli/cli.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py) | **`ExperimentMetadata` 的生产者**。benchmark 命令在这里组装并把它写成 JSON。 |
| [genai_bench/analysis/experiment_loader.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/experiment_loader.py) | **`ExperimentMetadata` 的消费者**。读回 JSON 重建对象。 |

文件最顶部还有一个类型别名：

```python
LiveMetricsData = Dict[str, List[float] | Dict[str, float]]
```

见 [protocol.py:5](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L5)。它不是 Pydantic 模型，只是一个普通类型注解，供实时仪表盘（`ui/dashboard.py`）和聚合指标（`metrics/aggregated_metrics_collector.py`）描述「指标名 → 数值序列」的字典结构，本讲不做深入。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**请求模型族**、**响应模型族**、**ExperimentMetadata**。三者覆盖了 `protocol.py` 的主体内容（认证配置类 `APIAuthConfig` 等较薄，留到 U5 详讲）。

### 4.1 请求模型族（UserRequest 及其子类）

#### 4.1.1 概念说明

「请求模型」描述的是**一次发往模型服务的任务输入**长什么样。genai-bench 支持多种任务（聊天、嵌入、重排、语音、画图……），每种任务的输入字段不同。项目用一个**继承家族**来组织它们：

- `UserRequest`：所有请求的**公共基类**，只放每个任务都有的字段（模型名、附加参数）。
- 一批子类：在基类基础上，各自追加自己独有的字段。

这样设计的两个好处：

1. **公共逻辑只写一遍**：任何子类都自带 `model`、`additional_request_params`，下游代码拿到一个请求时，总能安全地访问这两个公共字段。
2. **任务专属字段强约束**：构造 `UserChatRequest` 必须给 `prompt`，构造 `UserEmbeddingRequest` 必须给 `documents`——少给就报错。字段即文档。

#### 4.1.2 核心流程

请求模型在数据流中的生命周期是：

```text
Sampler（采样器）
   │  按任务类型，调用对应的 _sample_xxx_request()
   │  构造出 UserXxxRequest 对象
   ▼
User.sample()
   │  从 environment.sampler 取到一个 UserRequest
   │  交给具体后端（OpenAI/OCI/...）翻译成 HTTP 请求发出
   ▼
（请求发出后，进入「响应模型」流程，见 4.2）
```

关键点：**采样器只产协议对象，不直接产 HTTP 请求**。它产的是「语言中立的请求描述」，由各后端 User 负责翻译成具体的 API 调用。这正是数据契约带来的解耦。

#### 4.1.3 源码精读

**基类 `UserRequest`** —— 整个家族的根，只有两个公共字段：

见 [protocol.py:8-17](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L8-L17)。

```python
class UserRequest(BaseModel):
    model: str = Field(..., description="Model Name")
    additional_request_params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional parameters for the request.",
    )
```

- `model` 用 `Field(...)` 表示**必填**——没有模型名就没法发请求。
- `additional_request_params` 用 `default_factory=dict`，意思是「默认建一个空 dict」（注意：可变默认值必须用 `default_factory`，不能直接写 `default={}`，这是 Python 的通用规则，Pydantic 亦然）。它用来承载 `temperature`、`top_p` 之类因后端而异的「额外参数」。

**子类 `UserChatRequest`** —— 聊天/补全任务，最常用：

见 [protocol.py:20-31](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L20-L31)。

```python
class UserChatRequest(UserRequest):
    prompt: str = Field(..., description="Prompt to send to the LLM API server.")
    num_prefill_tokens: int | None = Field(..., description="Number of tokens in the prompt.")
    max_tokens: int | None = Field(..., description="Number of maximum tokens expected in the generation.")
```

- 它**继承了** `model` 和 `additional_request_params`，自己再加 `prompt`（提示词）、`num_prefill_tokens`（输入侧 token 数）、`max_tokens`（期望生成的最大 token 数）。
- 注意 `num_prefill_tokens` 和 `max_tokens` 虽然允许 `None`，但仍标了 `...`（必填）——意思是「这个键必须给，值可以是 `None`」。这是 Pydantic 里一个容易踩坑的细节。

**其余请求子类一览**（字段差异即可说明用途）：

| 类名 | 位置 | 关键独有字段 | 代表的任务 |
|------|------|--------------|-----------|
| `UserImageChatRequest` | [protocol.py:34-49](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L34-L49) | `image_content`、`num_images` | 视觉问答（图+文聊天） |
| `UserEmbeddingRequest` | [protocol.py:52-64](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L52-L64) | `documents` | 文本向量化 |
| `UserReRankRequest` | [protocol.py:67-80](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L67-L80) | `documents`、`query` | 按查询重排文档 |
| `UserTextToSpeechRequest` | [protocol.py:83-89](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L83-L89) | `input_text`、`voice`（默认 `"alloy"`） | 文本转语音 |
| `UserImageEmbeddingRequest` | [protocol.py:92-106](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L92-L106) | 继承嵌入，加 `image_content`、`num_images` | 图像向量化 |
| `UserImageGenerationRequest` | [protocol.py:109-124](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L109-L124) | `prompt`、`size`、`quality`、`num_images`（默认 1） | 文生图 |

注意两个「多级继承」关系：`UserImageChatRequest` 继承自 `UserChatRequest`（视觉问答复用聊天通道），`UserImageEmbeddingRequest` 继承自 `UserEmbeddingRequest`。这反映出项目的任务观：**图像类任务往往是文本类任务的「多模态扩展」**。

**真实生产者**：以聊天为例，`TextSampler._sample_chat_request` 的结尾就是直接 `return UserChatRequest(...)`，见 [sampling/text.py:127-133](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L127-L133)：

```python
return UserChatRequest(
    model=self.model,
    prompt=prompt,
    num_prefill_tokens=num_prefill_tokens,
    max_tokens=num_output_tokens,
    additional_request_params=self.additional_request_params,
)
```

同文件里还能看到嵌入、重排、文生图、语音的构造点：[sampling/text.py:154-159](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L154-L159)、[sampling/text.py:178-184](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L178-L184)、[sampling/text.py:214-221](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L214-L221)、[sampling/text.py:236-241](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L236-L241)。每一个都是「按场景采样 → 填进对应请求模型」的统一写法。

#### 4.1.4 代码实践

**实践目标**：亲手构造请求模型，验证「必填字段缺失会报错」以及「序列化结果」。

**操作步骤**（这是一段**示例代码**，不是项目原有代码；可在已 `pip install genai-bench` 的环境里运行）：

```python
# 示例代码：实践请求模型族
from genai_bench.protocol import UserChatRequest, UserImageGenerationRequest

# 1) 正常构造一个聊天请求
req = UserChatRequest(
    model="my-model",
    prompt="你好",
    num_prefill_tokens=2,
    max_tokens=16,
    additional_request_params={"temperature": 0.7},
)
print(req.model_dump_json(indent=2))

# 2) 故意漏掉必填的 prompt，观察校验失败
try:
    UserChatRequest(model="my-model", num_prefill_tokens=2, max_tokens=16)
except Exception as e:
    print("校验失败类型：", type(e).__name__)
```

**需要观察的现象**：

1. 第 1 步打印出一段 JSON，应包含 `model`、`prompt`、`num_prefill_tokens`、`max_tokens`、`additional_request_params` 五个键。
2. 第 2 步抛出异常。

**预期结果**：

- 第 2 步的异常类型为 `pydantic.ValidationError`（Pydantic v2 的校验失败异常）。
- 第 1 步的 JSON 中，`additional_request_params` 的值为 `{"temperature": 0.7}`，验证了公共字段被正确继承。

> 若你本地安装的是更老/更新的 Pydantic，异常类型名或错误文案可能略有差异，具体以本地运行为准（**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：`UserTextToSpeechRequest` 的 `voice` 字段没有用 `...`，而是 `default="alloy"`。这意味着什么？如果不传 `voice` 会怎样？

**参考答案**：`voice` 是**可选字段**，默认值是字符串 `"alloy"`。构造时不传 `voice`，对象里 `voice` 会等于 `"alloy"`；传了就用传入值。它和 `Field(...)` 的必填字段相反。

**练习 2**：为什么 `UserImageChatRequest` 要继承 `UserChatRequest`，而不是直接继承 `UserRequest`？

**参考答案**：因为视觉问答（图+文）在协议层面就是「聊天请求 + 一组图片」，复用 `UserChatRequest` 的 `prompt`、`num_prefill_tokens`、`max_tokens` 字段，避免重复定义；再追加 `image_content`、`num_images` 即可。继承准确表达了「图像聊天是一种聊天」的语义。

---

### 4.2 响应模型族（UserResponse 及其子类）

#### 4.2.1 概念说明

「响应模型」描述的是**一次请求完成后，回收到的结果**长什么样。注意：这里存的不是 HTTP 原文，而是 genai-bench **从响应里提炼出来的、与指标计算相关的标准化结果**——比如首 token 的时间戳、收到了几个 token、生成的文本、错误信息等。

之所以要单独建一族响应模型，是因为不同任务的「产出物」形态差异很大：聊天产出文本与 token 数，文生图产出图片列表，TTS 产出音频字节数。用继承家族分门别类，下游指标模块就能按类型走不同计算分支。

#### 4.2.2 核心流程

响应模型的生命周期，是请求流程的后半段：

```text
后端 User 发出 HTTP 请求
   │  解析响应（流式 chunk / 一次性 JSON）
   │  把提炼出的数值填进 UserXxxResponse
   ▼
User.collect_metrics(response, endpoint)
   │  把 response 交给 RequestMetricsCollector
   │  计算出 ttft / 吞吐等指标（U4 详讲）
   ▼
聚合 → 落盘 JSON
```

这里有一个贯穿全项目的关键三元时间戳：`start_time`、`time_at_first_token`、`end_time`。它们三个相减就能得到 TTFT（首 token 延迟）和端到端延迟——这是 token 级基准测试的核心，全部由响应模型统一承载。

#### 4.2.3 源码精读

**基类 `UserResponse`** —— 所有响应的公共字段，重点是时序与计数：

见 [protocol.py:127-155](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L127-L155)。

```python
class UserResponse(BaseModel):
    status_code: int = Field(..., description="The HTTP status code of the response.")
    time_at_first_token: Optional[float] = Field(default=None, ...)
    start_time: Optional[float] = Field(default=None, ...)
    end_time: Optional[float] = Field(default=None, ...)
    error_message: Optional[str] = Field(default=None, ...)
    num_prefill_tokens: Optional[int] = Field(default=None, ...)
```

逐字段理解：

- `status_code`：HTTP 状态码，**必填**。哪怕请求失败，也要记下状态码（如 500），方便后续区分成功/失败请求。
- `start_time` / `time_at_first_token` / `end_time`：请求发起、首 token 到达、请求结束的三个时间戳（单位：秒，来自 `time.time()` 之类的绝对时刻）。三者默认 `None`——因为对非流式任务，可能没有「首 token」概念。
- `error_message`：失败时的错误信息，默认 `None`。
- `num_prefill_tokens`：输入侧 token 数。它的 docstring 很值得读（见链接），因为它对不同任务含义不同：聊天是 prompt token 数，视觉只算文本 token，嵌入是全部文档的 token 总数。

**子类 `UserChatResponse`** —— 聊天任务追加「生成物」字段：

见 [protocol.py:168-183](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L168-L183)。

```python
class UserChatResponse(UserResponse):
    generated_text: Optional[str] = Field(default="", ...)
    tokens_received: Optional[int] = Field(default=0, ...)
    reasoning_tokens: Optional[int] = Field(default=0, ...)
```

- `tokens_received`：本次响应实际收到的输出 token 数（吞吐量计算的分子）。
- `reasoning_tokens`：推理 token 数（针对带思维链的模型，如 gpt-oss 等）。

**其余响应子类**：

| 类名 | 位置 | 关键独有字段 | 代表的任务 |
|------|------|--------------|-----------|
| `UserTextToSpeechResponse` | [protocol.py:158-165](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L158-L165) | `audio_bytes`（默认 0） | 文本转语音 |
| `UserImageGenerationResponse` | [protocol.py:186-202](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L186-L202) | `generated_images`、`revised_prompt`、`images_generated` | 文生图 |

注意一个设计细节：聊天和文生图都有「产出内容」要记（文本 / 图片），所以各自有专门子类；而**嵌入、重排**任务没有追加字段——它们复用基类 `UserResponse` 即可，因为指标只关心时序与 `num_prefill_tokens`。

**真实消费者**：测试里直接构造 `UserChatResponse` 来验证指标上报逻辑，见 [tests/user/test_base_user.py:66-74](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_base_user.py#L66-L74)：

```python
user_response = UserChatResponse(
    status_code=200,
    generated_text="random",
    tokens_received=2,
    time_at_first_token=2,
    num_prefill_tokens=1,
    start_time=0,
    end_time=3,
)
```

同文件 [tests/user/test_base_user.py:87-93](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_base_user.py#L87-L93) 则用裸 `UserResponse`（无 `tokens_received`）来表示嵌入类响应——印证了「嵌入用基类即可」的设计。

#### 4.2.4 代码实践

**实践目标**：用响应模型里的三个时间戳，手算一次聊天的 TTFT 与端到端延迟，体会「数据契约如何支撑指标」。

**操作步骤**（**示例代码**）：

```python
# 示例代码：实践响应模型族
from genai_bench.protocol import UserChatResponse

resp = UserChatResponse(
    status_code=200,
    generated_text="hello",
    tokens_received=5,
    start_time=10.0,
    time_at_first_token=10.8,
    end_time=12.0,
    num_prefill_tokens=3,
)

# 指标计算的核心思想（U4 会用专门 collector 实现，这里手算体会含义）
ttft = resp.time_at_first_token - resp.start_time          # 首 token 延迟
e2e  = resp.end_time - resp.start_time                      # 端到端延迟
print(f"TTFT = {ttft:.2f} s, 端到端 = {e2e:.2f} s")
print("可序列化为：", resp.model_dump_json())
```

**需要观察的现象**：

1. TTFT 与端到端延迟的数值。
2. `model_dump_json()` 输出里，未显式赋值的字段（如 `reasoning_tokens`、`error_message`）如何出现。

**预期结果**：

- `TTFT = 0.80 s, 端到端 = 2.00 s`。
- JSON 中 `reasoning_tokens` 为 `0`、`error_message` 为 `null`——即填入了各字段的默认值。这说明 Pydantic 序列化时会带上带默认值的字段。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `start_time` / `time_at_first_token` / `end_time` 都设计成 `Optional[float]`（允许 `None`）？

**参考答案**：不是所有任务都有完整的三元时间戳。例如非流式的嵌入请求没有「首 token」概念，`time_at_first_token` 就是 `None`；请求失败时 `end_time` 可能缺失。允许 `None` 让模型能统一描述成功/失败、流式/非流式各种情况，下游再按「是否为 `None`」分支处理。

**练习 2**：嵌入（embedding）任务的响应为什么没有专门的 `UserEmbeddingResponse` 子类？

**参考答案**：因为嵌入任务的指标只需要基类 `UserResponse` 已有的字段（状态码、三元时间戳、`num_prefill_tokens`、错误信息），没有额外的「生成内容」要记录。没有必要为它造一个空子类，直接复用基类即可——这体现了「按需继承」的克制。

---

### 4.3 ExperimentMetadata（实验元数据）

#### 4.3.1 概念说明

前两族模型描述的是「单次请求/响应」，属于**微观**层面。`ExperimentMetadata` 描述的则是**一整次基准实验**的「身份证」——属于**宏观**层面。

一次 genai-bench 实验，往往要跑「多个场景 × 多个并发档位」共几十轮 run（回顾 u1-l2 提到的 `total_runs = 场景数 × 并发档位数`）。这几十轮 run 共享同一组「实验配置」：用的哪个后端、哪个模型、哪个任务、跑多久、结果存哪个文件夹……这些不随单次请求变化的配置，就是 `ExperimentMetadata`。

它的用途有两面：

1. **实验开始时**：benchmark 命令把它序列化成 `experiment_metadata.json` 写入实验目录，作为这次实验的「出生证明」。
2. **实验结束后**：分析模块（excel/plot）读回这个 JSON，重建 `ExperimentMetadata`，据此知道这批结果对应什么配置，从而正确地分组、命名、出报告。

#### 4.3.2 核心流程

`ExperimentMetadata` 的「写出 → 读回」闭环，是数据契约价值的集中体现：

```text
cli.py: benchmark 命令
   │  收集全部配置（后端/模型/任务/并发/场景/时长……）
   │  ExperimentMetadata(...)
   │  model_dump_json(indent=4)  ──写──▶  experiment_metadata.json
   ▼
（跑完所有 run，各自再写 run 的 JSON）
   ▼
analysis/experiment_loader.py
   │  读 experiment_metadata.json → dict
   │  ExperimentMetadata(**data)  ◀──读──  重建对象
   │  据此解析 run 数据、出 excel/plot 报告
```

这里能看到 Pydantic 模型「双向」的好处：`model_dump_json` 把对象无损变 JSON，`ExperimentMetadata(**data)` 又能把 JSON 无损变回对象。**写出端和读入端共用同一份类定义**，配置项不可能对不上——这就是「契约」二字的力量。

#### 4.3.3 源码精读

`ExperimentMetadata` 字段较多，按功能分组讲解。类定义见 [protocol.py:222-277](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L222-L277)。

**① 可追溯性字段（这条命令、这次实验的来源）**

```python
cmd: str = Field(..., description="Exact command for the current experiment.")
benchmark_version: str = Field(..., description="The current version of genai-bench.")
```

见 [protocol.py:225-228](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L225-L228)。`cmd` 记录完整命令行，方便日后**复现**这次实验；`benchmark_version` 记录当时 genai-bench 的版本。

**② 后端与认证字段**

```python
api_backend: str = Field(..., description="The API backend to use.")
auth_config: Dict[str, Any] = Field(default={}, ...)
api_model_name: str = Field(..., ...)
server_model_tokenizer: Optional[str] = Field(None, ...)
model: str = Field(..., ...)
task: str = Field(..., ...)
```

见 [protocol.py:229-243](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L229-L243)。注意区分三个「模型」相关字段：`api_backend`（如 `openai`、`aws-bedrock`）、`model`（被测模型名）、`api_model_name`（请求体里实际填的模型名，常与 `model` 相同，但允许不同）、`server_model_tokenizer`（用于 token 采样的分词器）。`auth_config` 存认证配置（敏感信息通常不入这里，详见 U5）。

**③ 运行规模字段（带校验！）**

```python
num_concurrency: List[conint(ge=1)] = Field(..., description="The number of concurrent requests.")
batch_size: Optional[List[int]] = Field(None, ...)
iteration_type: Literal["num_concurrency", "batch_size"] = Field("num_concurrency", ...)
traffic_scenario: List[str] = Field(default_factory=list)
```

见 [protocol.py:245-254](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L245-L254)。这里出现了本讲最值得注意的两个**约束类型**：

- `List[conint(ge=1)]`：并发数列表，**每个元素都必须 ≥ 1**。传 `[0]` 或 `[-1]` 会触发 `ValidationError`。这从源头杜绝了「并发 0」这种无意义配置。
- `Literal["num_concurrency", "batch_size"]`：迭代类型只能是这两个字符串之一，默认 `"num_concurrency"`。它决定实验是按「并发档位」还是「batch size」遍历——这是 u1-l2 提到「text-to-text 默认跑 5×9=45 次」背后的开关之一。

`traffic_scenario` 是场景字符串列表（如 `["N(480,240)/(300,150)"]`，场景语法本身在 U2 详讲）。

**④ 时长与产出字段**

```python
max_time_per_run_s: int = Field(..., ...)
max_requests_per_run: int = Field(..., ...)
experiment_folder_name: str = Field(..., ...)
metrics_time_unit: str = Field(default="s", ...)
```

见 [protocol.py:260-276](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L260-L276)。`max_time_per_run_s` 是**每轮 run 的最长秒数**（回顾 u1-l2：CLI 上的 `--max-time-per-run` 单位是分钟，在这里已被换算成秒 `_s`）。`experiment_folder_name` 存实验目录绝对路径。`metrics_time_unit` 控制延迟指标显示单位（`s` 或 `ms`），贯穿保存/UI/报告（U4 详讲）。

**⑤ 服务端环境信息（可选）**

```python
server_engine: Optional[str] = None
server_version: Optional[str] = None
server_gpu_type: Optional[str] = None
server_gpu_count: Optional[str] = None
dataset_path: Optional[str] = None
```

见 [protocol.py:256-259](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L256-L259) 与 [protocol.py:277](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L277)。这组字段记录被测服务用什么引擎、什么 GPU——把「压测结果」和「被测环境」绑定，便于横向对比不同部署。

**真实生产者**：`cli.py` 在 benchmark 函数里组装并落盘，见 [cli.py:350-377](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L350-L377)：

```python
experiment_metadata = ExperimentMetadata(
    cmd=cmd_line,
    benchmark_version=GENAI_BENCH_VERSION,
    api_backend=api_backend,
    auth_config=auth_provider.get_config(),
    ...
    num_concurrency=num_concurrency,
    iteration_type=iteration_type,
    ...
)
experiment_metadata_file.write_text(experiment_metadata.model_dump_json(indent=4))
```

**真实消费者**：分析模块把 JSON 读回成对象，见 [experiment_loader.py:139](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/experiment_loader.py#L139)：

```python
experiment_metadata = ExperimentMetadata(**data)
```

一写一读，完美闭环。

#### 4.3.4 代码实践

**实践目标**：亲手完成 `ExperimentMetadata` 的「构造 → 序列化 → 重建」闭环，并验证 `conint` 约束生效。

**操作步骤**（**示例代码**）：

```python
# 示例代码：实践 ExperimentMetadata 的写出与读回
import json
from genai_bench.protocol import ExperimentMetadata

# 1) 构造一个最小合法的实验元数据
meta = ExperimentMetadata(
    cmd="genai-bench benchmark --api-backend openai ...",
    benchmark_version="0.0.5",
    api_backend="openai",
    api_model_name="my-model",
    model="my-model",
    task="text-to-text",
    num_concurrency=[1, 2, 4],          # 三个并发档位
    max_time_per_run_s=60,
    max_requests_per_run=1000,
    experiment_folder_name="/tmp/exp1",
)
text = meta.model_dump_json(indent=2)
print(text)

# 2) 模拟「分析模块读回」
meta2 = ExperimentMetadata(**json.loads(text))
print("读回后 task =", meta2.task, "| 并发档位数 =", len(meta2.num_concurrency))

# 3) 触发 conint 约束：并发里放一个 0
try:
    ExperimentMetadata(
        cmd="x", benchmark_version="0.0.5", api_backend="openai",
        api_model_name="m", model="m", task="text-to-text",
        num_concurrency=[1, 0],          # 0 非法
        max_time_per_run_s=60, max_requests_per_run=1000,
        experiment_folder_name="/tmp/exp1",
    )
except Exception as e:
    print("约束触发，异常类型：", type(e).__name__)
```

**需要观察的现象**：

1. 第 1 步 JSON 中，未显式传入的字段（`iteration_type`、`metrics_time_unit`、`traffic_scenario` 等）以默认值出现。
2. 第 2 步读回的对象字段与原始一致。
3. 第 3 步抛异常。

**预期结果**：

- JSON 里 `iteration_type` 为 `"num_concurrency"`，`metrics_time_unit` 为 `"s"`，`traffic_scenario` 为 `[]`，`auth_config` 为 `{}`——都是字段默认值。
- 读回后 `task = text-to-text`，并发档位数 = 3。
- 第 3 步异常类型为 `pydantic.ValidationError`，错误信息会指向 `num_concurrency` 里的 `0` 不满足 `ge=1`。

#### 4.3.5 小练习与答案

**练习 1**：`num_concurrency` 用的是 `List[conint(ge=1)]`。如果把类型改成普通 `List[int]`，会丢失什么能力？

**参考答案**：会丢失「每个并发数必须 ≥ 1」的**自动校验**。改成普通 `List[int]` 后，传 `[0]` 或负数都不会被拦截，错误会推迟到运行期（比如 Locust 启动 0 个虚拟用户）才暴露，排查更困难。`conint(ge=1)` 把约束前移到「构造对象时」，符合「快速失败」原则。

**练习 2**：为什么 `ExperimentMetadata` 要同时保存 `model` 和 `api_model_name` 两个看起来都是「模型名」的字段？

**参考答案**：它们语义不同。`model` 是「被基准测试的模型」标识（用于报告、分组、命名），`api_model_name` 是「请求体里实际填给后端的模型名」。多数情况下两者相同，但某些后端/代理场景下，对外暴露的名字和实际请求的名字可能不同（比如经过一层路由）。分开存放能准确描述这种差异，避免歧义。

---

## 5. 综合实践

把本讲三族模型串起来，模拟一段「采样 → 请求 → 响应 → 实验元数据」的迷你数据流（**示例代码**，无需真实服务）：

```python
# 示例代码：综合实践 —— 串起三族模型
from genai_bench.protocol import (
    UserChatRequest, UserChatResponse, ExperimentMetadata,
)

# ① 采样器产出：一个聊天请求
req = UserChatRequest(
    model="my-model", prompt="讲个笑话", num_prefill_tokens=4, max_tokens=32,
)

# ② 后端 User 提炼：对应的响应（假设成功）
resp = UserChatResponse(
    status_code=200, generated_text="…", tokens_received=32,
    start_time=100.0, time_at_first_token=100.6, end_time=102.0,
    num_prefill_tokens=4,
)

# ③ 这次实验的「身份证」
meta = ExperimentMetadata(
    cmd="genai-bench benchmark ...", benchmark_version="0.0.5",
    api_backend="openai", api_model_name="my-model", model="my-model",
    task="text-to-text", num_concurrency=[1],
    max_time_per_run_s=60, max_requests_per_run=100,
    experiment_folder_name="/tmp/exp",
)

# ④ 体会：三类对象都能被同一套 Pydantic 机制序列化
for name, obj in [("请求", req), ("响应", resp), ("实验元数据", meta)]:
    print(f"--- {name} ---")
    print(obj.model_dump_json(indent=2))
```

**实践要点**：

1. 体会三类模型各自描述的「粒度」：请求/响应是单次的，实验元数据是整次的。
2. 体会它们都来自同一个 `protocol.py`，都被同一套 `model_dump_json` 序列化——这就是「统一数据契约」。
3. 进阶：把第 ② 步的三个时间戳相减，写出 TTFT 与端到端延迟的计算（参考 4.2.4），为 U4 的指标计算做铺垫。

## 6. 本讲小结

- `protocol.py` 是 genai-bench 的**全局数据契约**，所有子系统都通过它定义的 Pydantic 模型交换数据。
- **请求模型族**以 `UserRequest` 为基类，按任务衍生出聊天、嵌入、重排、语音、文生图等子类；采样器是这些请求的**生产者**。
- **响应模型族**以 `UserResponse` 为基类，承载 `status_code` 与 `start_time`/`time_at_first_token`/`end_time` 三元时间戳；不同任务按需追加产出字段，是指标计算的**输入**。
- **`ExperimentMetadata`** 描述一整次实验的配置「身份证」，用 `conint(ge=1)`、`Literal` 等做配置约束，经 `model_dump_json` 写出、`ExperimentMetadata(**data)` 读回，形成闭环。
- Pydantic 在项目里的核心价值：**字段即文档、构造即校验、对象可无损序列化**，让互不相干的模块靠同一份类定义协同。
- 本讲只看「数据形状」，指标如何从响应里算出来（TTFT、吞吐）留给 U4。

## 7. 下一步学习建议

- **横向**：进入 U2，看请求模型是如何被**生产**出来的——`Sampler` 与 `Scenario` 如何按场景采样、填进 `UserChatRequest` 等模型（u2-l2、u2-l4）。
- **纵向**：进入 U4，看响应模型如何被**消费**——`RequestMetricsCollector` 如何从 `UserResponse` 的时间戳与 token 数算出 TTFT、吞吐等指标（u4-l1）。
- **闭环**：进入 U6，看 `ExperimentMetadata` 如何被分析模块读回、驱动 excel/plot 报告（u6-l1）。
- 如果你想立刻验证本讲的代码实践，可在已安装 genai-bench 的环境运行第 4 节与第 5 节的示例脚本，对照「预期结果」逐条确认。

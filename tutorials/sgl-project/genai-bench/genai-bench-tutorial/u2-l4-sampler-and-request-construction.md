# 采样器 Sampler 与请求构造

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `Sampler` 抽象基类在 genai-bench 中扮演的角色，以及它如何用 `modality_registry` + `create()` 工厂把「任务字符串」翻译成一个具体的采样器实例。
- 复述 `TextSampler.sample()` 按 `output_modality` 分发的完整流程，并能区分「场景模式」与「数据集模式」两条分支的差异。
- 解释 `--prefix-len` / `--prefix-ratio` 两种共享前缀（prefix）缓存机制的实现原理，以及为什么要在前缀和后缀之间插入一个随机分隔符。
- 自己动手构造一个 `TextSampler`，分别用 `D(100,100)` 和 `dataset` 模式调用 `sample()`，并对比返回的 `UserChatRequest` 字段差异。

## 2. 前置知识

本讲是把前几讲「散落的零件」组装成「一个完整请求」的关键一步。开始前，请先确认你已经理解下面三件事：

- **任务字符串**（u2-l1）：形如 `<input>-to-<output>`，例如 `text-to-text`、`image-text-to-text`。输入模态决定用哪个采样器，输出模态决定生成哪种请求。
- **场景**（u2-l2）：用 `D(100,100)`、`N(480,240)/(300,150)`、`I(1024,1024)` 这类微型语言描述「每个请求的输入输出规模」，`Scenario.sample()` 会返回具体的 token 数或尺寸。还有一类特殊的 `dataset` 场景（`DatasetScenario`），它本身不携带任何采样参数，只是用来发信号说「直接用原始数据，不要做 token 塑形」。
- **数据集**（u2-l3）：`DataLoaderFactory.load_data_for_task(...)` 会把语料读成 `List[str]`（文本）或图像结构，交给采样器使用。

一个关键直觉：**场景负责「定量」，数据集负责「提供素材」，而采样器负责把这两者揉成一个可发送的请求对象。** 三者关系如下：

```text
任务字符串 ──► Sampler.create() ──► 选出采样器类（TextSampler / ImageSampler）
                                          │
   数据集 data (List[str]) ─────────────►│
   场景 scenario (D/N/U/I/...) ─────────►│
   tokenizer ───────────────────────────►│
                                          ▼
                                   sampler.sample(scenario)
                                          │
                                          ▼
                                  UserRequest（协议模型，见 u1-l5）
```

`UserRequest` 是 u1-l5 讲过的全局数据契约，本讲产出的 `UserChatRequest`、`UserEmbeddingRequest` 等都是它的子类。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [genai_bench/sampling/base.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/base.py) | `Sampler` 抽象基类：定义接口、维护 `modality_registry` 注册表、提供 `create()` 工厂。 |
| [genai_bench/sampling/text.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py) | `TextSampler`：文本类任务的统一采样器，覆盖 chat/embeddings/rerank/image/speech 五种输出，并实现 prefix 缓存。 |
| [genai_bench/sampling/image.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/image.py) | `ImageSampler`：图像类任务的采样器（`image-text-to-text`、`image-to-embeddings`）。 |
| [genai_bench/protocol.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py) | `UserRequest` 及其子类，采样器最终产出这些对象。 |
| [genai_bench/scenarios/base.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/scenarios/base.py) | `Scenario` 抽象基类与 `DatasetScenario`，是 `sample()` 的输入。 |

调用方入口（仅作背景了解）：CLI 在 `cli.py` 里用 `Sampler.create(...)` 构造采样器（[cli.py:314-323](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L314-L323)），随后 Locust 虚拟用户每次发请求时通过 `BaseUser.sample()` 调用 `environment.sampler.sample(scenario)`（[base_user.py:27-44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L27-L44)）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **Sampler 基类与注册**：接口 + `modality_registry` 自动注册 + `create()` 工厂。
2. **TextSampler 采样流程**：`sample()` 按 `output_modality` 分发，以及场景模式 vs 数据集模式。
3. **prefix 缓存机制**：`--prefix-len` 与 `--prefix-ratio` 的实现。

---

### 4.1 Sampler 基类与注册

#### 4.1.1 概念说明

采样器的职责非常专一：**给定场景和数据，产出一个 `UserRequest`。** 源码注释明确提醒「不要在这里塞解析、编码等额外逻辑」——它是一个纯粹的「装配器」（[base.py:11-17](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/base.py#L11-L17)）。

和 u2-l2 讲过的 `Scenario` 一样，`Sampler` 也用一个**类级注册表**来实现「定义即注册、工厂按名查找」。这样上层（CLI）只需要传入任务字符串，就能自动拿到正确的采样器子类，无需写一长串 `if task == ...`。

#### 4.1.2 核心流程

注册与查找的关键是两个类属性和一个魔术方法：

```text
modality_registry : Dict[str, Type[Sampler]]   # 输入模态 → 采样器类
input_modality    : str                          # 子类声明自己处理哪种输入
supported_tasks   : Set[str]                     # 子类声明支持哪些任务
```

每当定义一个新的 `Sampler` 子类，Python 会自动调用 `__init_subclass__`，把它登记进 `modality_registry`，键就是子类的 `input_modality`（[base.py:23-26](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/base.py#L23-L26)）。这与 `Scenario._registry`（u2-l2）是同一种「插件式注册」思想。

工厂 `create(task, ...)` 的查找流程如下：

```text
1. task.split("-to-")  →  (input_modality, output_modality)
2. modality_registry[input_modality]  →  找到采样器类
   （特例：复合输入 "image-text" 含 "image"，回退用 ImageSampler）
3. supports_task(input, output)  →  确认该类确实支持这个任务
4. 实例化 sampler_cls(*args, output_modality=output_modality, **kwargs)
```

#### 4.1.3 源码精读

注册表的声明与自动注册（[base.py:19-26](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/base.py#L19-L26)）：

```python
modality_registry: Dict[str, Type["Sampler"]] = {}
input_modality: str
supported_tasks: Set[str]

def __init_subclass__(cls, **kwargs):
    """Automatically registers subclasses in the task registry."""
    super().__init_subclass__(**kwargs)
    cls.modality_registry[cls.input_modality] = cls
```

> 这段是说：任何声明了 `input_modality` 的子类，一被定义就会被加进 `modality_registry`。所以 `TextSampler`（`input_modality = "text"`）和 `ImageSampler`（`input_modality = "image"`）定义的瞬间就已注册。

构造函数保存公共依赖，并构造一个 `get_token_length` 工具（[base.py:28-57](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/base.py#L28-L57)），其中 `tokenizer` 用来把文本「数 token」，是后面塑形与偏差检查的基础：

```python
self.tokenizer = tokenizer
self.model = model
self.output_modality = output_modality
self.additional_request_params = additional_request_params or {}
self.get_token_length = lambda text, add_special_tokens=False: len(
    tokenizer.encode(text, add_special_tokens=add_special_tokens)
)
```

`create()` 工厂把任务字符串拆分、查表、校验、实例化四步串起来（[base.py:82-123](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/base.py#L82-L123)）。其中两段值得细看。

拆分任务字符串并对格式错误给出友好报错（[base.py:97-102](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/base.py#L97-L102)）：

```python
try:
    input_modality, output_modality = task.split("-to-")
except ValueError as err:
    raise ValueError(
        f"Invalid task format: {task}. Expected '<input>-to-<output>'."
    ) from err
```

复合输入模态的回退处理：`image-text-to-text` 的输入是 `image-text`，注册表里没有这个键，但因为其中包含 `image`，就回退到 `ImageSampler`（[base.py:107-116](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/base.py#L107-L116)）：

```python
if input_modality not in cls.modality_registry:
    if "image" in input_modality and "image" in cls.modality_registry:
        sampler_cls = cls.modality_registry["image"]
    else:
        raise ValueError(f"No sampler supports input modality: {input_modality}")
else:
    sampler_cls = cls.modality_registry[input_modality]
```

最后用 `supports_task` 二次确认，再把 `output_modality` 传给构造函数（[base.py:117-123](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/base.py#L117-L123)）：

```python
if not sampler_cls.supports_task(input_modality, output_modality):
    raise ValueError(...)
return sampler_cls(*args, output_modality=output_modality, **kwargs)
```

> 注意：`output_modality` 不是注册表的键，而是**运行时参数**。同一个 `TextSampler` 类，根据传入的 `output_modality` 不同，会走完全不同的 `sample()` 分支（见 4.2）。

#### 4.1.4 代码实践

**目标**：验证 `create()` 工厂的路由行为，不实际发请求。

**步骤**：

1. 在项目根目录启动 Python（确保已 `pip install -e .` 安装好依赖）。
2. 执行下面的脚本，观察不同的 `task` 会得到哪个采样器类。

```python
# 示例代码：仅演示工厂路由，不需要真实 tokenizer
from genai_bench.sampling.base import Sampler
# 触发子类定义，使其自动注册到 modality_registry
from genai_bench.sampling.text import TextSampler   # noqa: F401
from genai_bench.sampling.image import ImageSampler  # noqa: F401

print(Sampler.modality_registry)
# 期望：{'text': <class 'TextSampler'>, 'image': <class 'ImageSampler'>}

# 用一个极简的假 tokenizer，避免下载真实模型
class FakeTokenizer:
    def encode(self, text, add_special_tokens=False):
        return text.split()

for task in ["text-to-text", "image-text-to-text", "text-to-embeddings"]:
    s = Sampler.create(task=task, tokenizer=FakeTokenizer(),
                       model="m", data=["a b c"])
    print(task, "->", type(s).__name__, "output_modality=", s.output_modality)
```

**需要观察的现象**：

- `modality_registry` 里只有 `text` 和 `image` 两个键。
- `image-text-to-text` 路由到 `ImageSampler`（复合模态回退）。

**预期结果**（输出形如）：

```text
{'text': <class 'TextSampler'>, 'image': <class 'ImageSampler'>}
text-to-text -> TextSampler output_modality= text
image-text-to-text -> ImageSampler output_modality= text
text-to-embeddings -> TextSampler output_modality= embeddings
```

> 注意：`FakeTokenizer` 仅供演示路由；后续 4.2/4.3 的实践需要能真正数 token 的 tokenizer。`data` 是位置参数，本例用 `["a b c"]` 占位。

#### 4.1.5 小练习与答案

**练习 1**：调用 `Sampler.create("text-to-xyz", ...)` 会发生什么？为什么？

**参考答案**：`split("-to-")` 得到 `("text", "xyz")`，`modality_registry["text"]` 找到 `TextSampler`；但 `supports_task("text", "xyz")` 会检查 `"text-to-xyz" in TextSampler.supported_tasks`，由于不在集合内，抛出 `ValueError: Sampler for text does not support output modality: xyz`。这说明注册表只解决「输入模态→类」，输出模态的合法性由 `supports_task` 二次把关。

**练习 2**：为什么不把 `output_modality` 也做成注册表的键？

**参考答案**：因为同一个输入模态（如 `text`）要支持多种输出（text/embeddings/rerank/image/speech），这些输出共享大量采样逻辑（都从同一份文本数据取材、都用同一个 tokenizer 数 token）。把它们合并到一个 `TextSampler` 类里、用 `output_modality` 在运行时分发，比拆成五个类更能复用代码。

---

### 4.2 TextSampler 采样流程

#### 4.2.1 概念说明

`TextSampler` 是文本类任务的「多面手」：一个类覆盖五种输出（[text.py:32-39](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L32-L39)）。它的核心设计是**按 `output_modality` 分发**到不同的私有方法，每个方法负责构造对应类型的 `UserRequest`。

另一个贯穿所有分支的二分法是**场景模式 vs 数据集模式**：

- **场景模式**：场景是 `D/N/U/E/R/I/A` 等真实分布，`scenario.sample()` 返回目标 token 数 / 尺寸，采样器据此**生成**一段精确长度的文本（或图像规格）。
- **数据集模式**：场景为 `None` 或 `DatasetScenario`，采样器**不做任何塑形**，直接从原始数据里随机挑一条原文发给服务。

判断函数在基类里（[base.py:59-68](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/base.py#L59-L68)）：

```python
def _is_dataset_mode(self, scenario: Optional[Scenario]) -> bool:
    if scenario is None:
        return True
    return isinstance(scenario, DatasetScenario)
```

#### 4.2.2 核心流程

`sample()` 的分发逻辑是一组 `if/elif`（源码里有一句 TODO 说未来想换成「委托式请求构造器」替换这串 if-else，但目前仍是直接分发）：

```text
sample(scenario)
  ├── output_modality == "text"       → _sample_chat_request     → UserChatRequest
  ├── output_modality == "embeddings" → _sample_embedding_request → UserEmbeddingRequest
  ├── output_modality == "rerank"     → _sample_rerank_request    → UserReRankRequest
  ├── output_modality == "image"      → _sample_image_generation_request → UserImageGenerationRequest
  ├── output_modality == "speech"     → _sample_tts_request       → UserTextToSpeechRequest
  └── else                            → raise ValueError
```

以最常用的 chat 分支 `_sample_chat_request` 为例，它的内部流程是：

```text
_sample_chat_request(scenario):
  if 数据集模式:
      num_input_tokens = num_output_tokens = None
      effective_prefix_len = None
      additional_request_params["ignore_eos"] = False      # 让模型自然停止
  else (场景模式):
      校验场景类型必须是 TextDistribution
      (num_input_tokens, num_output_tokens) = scenario.sample()
      additional_request_params["ignore_eos"] = True       # 强制生成到 max_tokens
      计算 effective_prefix_len（见 4.3，若无前缀则为 None）

  prompt = _sample_text(num_input_tokens, effective_prefix_len)
  num_prefill_tokens = get_token_length(prompt)            # 实际 token 数
  若有目标值：_check_discrepancy(...)                       # 偏差超 10% 则告警
  返回 UserChatRequest(model, prompt, num_prefill_tokens, max_tokens=num_output_tokens, ...)
```

> 关于 `ignore_eos`：`eos`（end-of-sequence）是模型表示「回答结束」的停止符。场景模式下设 `ignore_eos=True`，是为了让模型**忽略停止符、一直生成到 `max_tokens`**，从而精确控制输出长度，便于测吞吐；数据集模式下设 `False`，让模型像真实业务那样自然停止。

#### 4.2.3 源码精读

`sample()` 的分发主体（[text.py:65-87](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L65-L87)）：

```python
def sample(self, scenario: Optional[Scenario]) -> UserRequest:
    if self.output_modality == "text":
        return self._sample_chat_request(scenario)
    elif self.output_modality == "embeddings":
        return self._sample_embedding_request(scenario)
    elif self.output_modality == "rerank":
        return self._sample_rerank_request(scenario)
    elif self.output_modality == "image":
        return self._sample_image_generation_request(scenario)
    elif self.output_modality == "speech":
        return self._sample_tts_request(scenario)
    else:
        raise ValueError(f"Unsupported output modality: {self.output_modality}")
```

`_sample_chat_request` 里场景模式与数据集模式的分叉（[text.py:89-100](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L89-L100)）：

```python
if self._is_dataset_mode(scenario):
    num_input_tokens, num_output_tokens = None, None
    effective_prefix_len = None
    self.additional_request_params["ignore_eos"] = False
else:
    self._validate_scenario(scenario)
    num_input_tokens, num_output_tokens = scenario.sample()
    self.additional_request_params["ignore_eos"] = True
```

构造请求的最后一步：用 `_sample_text` 生成 prompt，数出真实 token 数，再装配成 `UserChatRequest`（[text.py:122-133](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L122-L133)）：

```python
prompt = self._sample_text(num_input_tokens, effective_prefix_len)
num_prefill_tokens = self.get_token_length(prompt)
if num_input_tokens is not None:
    self._check_discrepancy(num_input_tokens, num_prefill_tokens, threshold=0.1)

return UserChatRequest(
    model=self.model,
    prompt=prompt,
    num_prefill_tokens=num_prefill_tokens,
    max_tokens=num_output_tokens,
    additional_request_params=self.additional_request_params,
)
```

> 注意 `max_tokens` 直接取自 `num_output_tokens`：场景模式下它是 `scenario.sample()` 的第二个值（如 `D(100,100)` 的 `100`），数据集模式下是 `None`。这就是两种模式下 `UserChatRequest.max_tokens` 字段差异的根因。

塑形偏差检查 `_check_discrepancy`：由于从数据集拼文本难以精确命中目标 token 数，当实际与目标偏差超过 10% 时只发一次告警（用 `warning_once` 避免日志刷屏），不中断运行（[text.py:434-457](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L434-L457)）。

#### 4.2.4 代码实践

见本讲 **第 5 节 综合实践**，它会让 `D(100,100)` 和 `dataset` 两种模式并排对比，直接观察 `UserChatRequest` 字段差异。

#### 4.2.5 小练习与答案

**练习 1**：`_sample_embedding_request` 里 `scenario.sample()` 返回的是一个整数（`tokens_per_document`）而不是元组，为什么这里能直接用？

**参考答案**：因为对应场景是 `E(1024)` 这类 `EmbeddingScenario`，它的 `sample()` 返回的就是单个整数（u2-l2 已说明每种场景的 `sample()` 返回结构不同）。采样器据此为 batch 里每一条文档生成一段约 `tokens_per_document` 长度的文本，再把所有文档的 token 数累加成 `num_prefill_tokens`（[text.py:135-159](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L135-L159)）。这也呼应了「采样器必须信任场景返回的结构」这一约定。

**练习 2**：如果调用 `TextSampler(output_modality="text").sample(some_E_scenario)` 会怎样？

**参考答案**：会在 `_validate_scenario` 里抛错——`output_modality == "text"` 要求场景类型是 `TextDistribution`，而 `E` 属于 `EmbeddingDistribution`，于是抛 `Expected TextDistribution for text output`（[text.py:272-278](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L272-L278)）。这保证「输出模态」与「场景类型」必须配对。

---

### 4.3 prefix 缓存机制

#### 4.3.1 概念说明

很多 LLM 推理服务都有 **prefix caching（前缀缓存）**：如果多个请求共享同一段开头（prefix），服务端可以复用这段开头已算好的 KV-cache，从而显著降低 TTFT。genai-bench 提供两个选项来构造这种「共享前缀」流量：

- `--prefix-len N`：每个请求都带一段**固定长度 N** 的共享前缀（全局只生成一次，所有请求复用）。
- `--prefix-ratio R`：每个请求的前缀长度 = `该请求输入 token 数 × R`（按请求动态变化，不共享）。

两者都是为了测「前缀命中」场景下的性能，但实现策略不同。

还有一个关键细节：**前缀和后缀之间会插入一个随机分隔符（separator）**。为什么？因为如果不加分隔符，`prefix + suffix` 拼接处的 token 边界可能与真实 prefix 不一致，导致服务端的 KV-cache 命中失效。插入一个随机串，能强制「prefix 部分」与「suffix 部分」在 token 层面清晰断开。

#### 4.3.2 核心流程

前缀长度的计算发生在 `_sample_chat_request` 里（[text.py:102-120](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L102-L120)）：

```text
if prefix_ratio is not None:
    effective_prefix_len = int(num_input_tokens * prefix_ratio)   # 按请求动态算
elif prefix_len is not None:
    effective_prefix_len = prefix_len                              # 固定值
    # 对非确定性场景(N/U)，分布尾部可能采样出 < prefix_len 的输入，
    # 此时重采样最多 10 次，直到 num_input_tokens >= prefix_len
    （重采样 10 次仍不够则抛错）
else:
    effective_prefix_len = None                                    # 无前缀
```

真正的文本生成与缓存策略在 `_sample_text` 里（[text.py:302-382](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L302-L382)）：

```text
_sample_text(num_input_tokens, effective_prefix_len):
  if 无目标 token 数:  return random.choice(self.data)   # 数据集模式，直接挑一条

  if 有前缀:
    prefix_rng = random.Random(hash((42, effective_prefix_len)))   # 确定性种子
    if prefix_len 模式:
        if self._shared_prefix is None:                            # 全局只生成一次
            self._shared_prefix = _generate_text_from_dataset(len, rng=prefix_rng)
        prefix = self._shared_prefix                               # 复用缓存
    else (prefix_ratio 模式):
        prefix = _generate_text_from_dataset(len, rng=prefix_rng)  # 每次重新生成

    suffix_len = num_input_tokens - effective_prefix_len
    separator = 随机 4 字符 hex（装不下则按 token 截断）
    suffix    = _generate_text_from_dataset(suffix_len - separator_len)
    return f"{prefix}{separator}{suffix}"
  else:
    return _generate_text_from_dataset(num_input_tokens)           # 无前缀，整段生成
```

#### 4.3.3 源码精读

构造函数接收并保存两个前缀参数，并初始化共享前缀缓存为 `None`（[text.py:49-63](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L49-L63)）：

```python
self.prefix_len = prefix_len
self.prefix_ratio = prefix_ratio
# Globally shared prefix (generated once for --prefix-len,
# per-request for --prefix-ratio)
self._shared_prefix: Optional[str] = None
```

`prefix_len` 模式的「生成一次并缓存」核心（[text.py:325-338](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L325-L338)）：

```python
if effective_prefix_len is not None:
    prefix_rng = random.Random(hash((42, effective_prefix_len)))
    if self.prefix_len is not None:
        # --prefix-len mode: generate once and cache
        if self._shared_prefix is None:
            self._shared_prefix = self._generate_text_from_dataset(
                effective_prefix_len, rng=prefix_rng
            )
            logger.info("Generated shared prefix ... reused across all requests.")
        prefix = self._shared_prefix
    else:
        # --prefix-ratio mode: generate fresh prefix per-request
        prefix = self._generate_text_from_dataset(effective_prefix_len, rng=prefix_rng)
```

> 两点设计：(1) `random.Random(hash((42, effective_prefix_len)))` 用确定种子，保证多 worker 进程生成**完全相同**的前缀，否则分布式压测时各 worker 前缀不同就达不到「共享」效果；(2) 把 `effective_prefix_len` 放进种子，是为了「不同前缀长度的场景之间不会意外撞上同一段缓存」（源码注释原话）。

分隔符的构造：生成一个 4 字符随机 hex 串作为前缀与后缀之间的「断点」，若剩余空间不够还会按 token 截断（[text.py:348-379](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L348-L379)），最终拼接成 `f"{prefix}{separator}{suffix}"`：

```python
if effective_prefix_len is not None and effective_prefix_len > 0:
    suffix_len = num_input_tokens - effective_prefix_len
    separator = random.randbytes(2).hex()              # 随机 4 字符 hex
    separator_len = self.get_token_length(separator)
    ...                                               # 装不下则截断 separator
    adjusted_suffix_len = suffix_len - separator_len
    suffix = self._generate_text_from_dataset(adjusted_suffix_len) if adjusted_suffix_len > 0 else ""
    return f"{prefix}{separator}{suffix}"
else:
    # No prefix caching - just return the full prompt
    return self._generate_text_from_dataset(num_input_tokens)
```

`_generate_text_from_dataset` 是底层「凑 token」工具：把数据集行打乱后逐行拼接，直到接近目标 token 数，最后一行按 token 级别截断（[text.py:384-428](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L384-L428)）。它接受一个可选的 `rng`，传入带种子的 `random.Random` 即可得到确定性输出。

最后，`reset_prefix_cache` 在切换场景时清空缓存，避免上一个场景的共享前缀串到下一个场景（[text.py:430-432](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/text.py#L430-L432)）：

```python
def reset_prefix_cache(self):
    """Reset the prefix cache when switching to a new scenario."""
    self._shared_prefix = None
```

#### 4.3.4 代码实践

**目标**：观察 `prefix_len` 模式下「前缀只生成一次、被多次复用」。

**步骤**：在综合实践脚本基础上，给 `TextSampler` 传 `prefix_len=20`，连续调用两次 `sample(Scenario.from_string("D(100,100)"))`，打印两个 prompt 的前 20 个 token 是否相同。

```python
# 示例代码片段
sampler = TextSampler(
    tokenizer=tokenizer, model="m", output_modality="text",
    data=data, prefix_len=20,
)
scn = Scenario.from_string("D(100,100)")
r1 = sampler.sample(scn)
r2 = sampler.sample(scn)
print(tokenizer.encode(r1.prompt, add_special_tokens=False)[:20]
      == tokenizer.encode(r2.prompt, add_special_tokens=False)[:20])
# 期望：True（前 20 个 token 完全一致，因为共享了 _shared_prefix）
```

**需要观察的现象**：两次请求 prompt 的前缀部分完全相同，但后缀不同（后缀是随机生成的）。

**预期结果**：前 20 个 token 相等返回 `True`；同时日志里应只出现一次 `Generated shared prefix ...`。具体前缀内容取决于 tokenizer 与数据，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `prefix_len` 用全局缓存而 `prefix_ratio` 每请求重新生成？

**参考答案**：`prefix_len` 是固定长度，所有请求共享同一段前缀才能最大化服务端 KV-cache 命中率，生成一次并缓存既保证一致性又省算力。`prefix_ratio` 的前缀长度随每个请求的 `num_input_tokens` 变化，长度都不一样，没法共享，只能按请求重新生成。

**练习 2**：前缀和后缀之间的随机 `separator` 起什么作用？

**参考答案**：它强制 prefix 与 suffix 在 token 边界上断开。若直接拼接，`prefix` 末尾与 `suffix` 开头可能被 tokenizer 合并成新 token，导致实际 prompt 的「前缀部分」与缓存里的 prefix 不一致，前缀缓存就会失效。随机分隔符让前缀段落保持稳定可命中。

**练习 3**：`reset_prefix_cache` 什么时候应该被调用？

**参考答案**：在切换到一个新场景（新的前缀长度）时调用，清掉上一个场景缓存的 `_shared_prefix`，否则新场景会误用旧前缀，既不符合预期长度，也可能跨场景「撞缓存」。

---

## 5. 综合实践

**任务**：构造一个 `TextSampler`，分别用 `D(100,100)`（场景模式）和 `dataset`（数据集模式）调用 `sample()`，对比返回的 `UserChatRequest` 字段差异，亲手验证 4.2 讲的两条分支。

**实践目标**：直观看到两种模式下 `max_tokens`、`num_prefill_tokens`、`ignore_eos`、`prompt` 内容的区别。

**操作步骤**：

1. 确保已 `pip install -e .`，并安装一个可用的 tokenizer（首次运行会从 HuggingFace 下载，约几十 MB）。
2. 在项目根目录保存并运行下面的脚本。

```python
# 示例代码
from transformers import AutoTokenizer
from genai_bench.sampling.text import TextSampler
from genai_bench.scenarios.base import Scenario, DatasetScenario

# 任选一个支持 .encode 的 tokenizer
tokenizer = AutoTokenizer.from_pretrained("HuggingFaceTB/SmolLM2-135M")

# 准备一份小数据集（List[str]），真实使用时由 DataLoaderFactory 提供
data = [
    "The quick brown fox jumps over the lazy dog.",
    "Artificial intelligence is transforming how we build software.",
    "A benchmark measures the performance of a system under load.",
] * 20

sampler = TextSampler(
    tokenizer=tokenizer,
    model="my-model",
    output_modality="text",
    data=data,
)

# === 场景模式：D(100,100) → 精确 100 输入 / 100 输出 ===
scenario = Scenario.from_string("D(100,100)")
req_scenario = sampler.sample(scenario)
print("=== 场景模式 D(100,100) ===")
print(req_scenario.model_dump_json(indent=2))

# === 数据集模式：传 None 或 DatasetScenario() ===
req_dataset = sampler.sample(DatasetScenario())   # 等价于 sampler.sample(None)
print("=== 数据集模式 ===")
print(req_dataset.model_dump_json(indent=2))
```

**需要观察的现象与预期结果**：

| 字段 | 场景模式 `D(100,100)` | 数据集模式 |
| --- | --- | --- |
| `prompt` | 由数据集拼接生成的、约 100 token 的文本 | `data` 中随机一整行原文 |
| `num_prefill_tokens` | 接近 100（实际 token 数，可能有轻微偏差） | `None` |
| `max_tokens` | `100` | `None` |
| `additional_request_params["ignore_eos"]` | `True` | `False` |
| `model` | `"my-model"` | `"my-model"` |

**预期结果说明**：场景模式下 `max_tokens=100` 且 `ignore_eos=True`，目的是让模型强制生成 100 个输出 token 以测吞吐；数据集模式下两者为 `None`/`False`，让模型按原文自然处理。`num_prefill_tokens` 的精确数值取决于所选 tokenizer 与数据内容，**待本地验证**。

> 若想进一步练习：把 `output_modality` 改成 `"embeddings"`，场景换成 `Scenario.from_string("E(64)")`，观察返回对象类型变成 `UserEmbeddingRequest`，且 `documents` 是一个列表。

## 6. 本讲小结

- `Sampler` 是纯粹的「装配器」：输入场景 + 数据 + tokenizer，输出一个 `UserRequest`；它用 `modality_registry` + `__init_subclass__` 实现「定义即注册」，用 `create(task)` 工厂按任务字符串路由到 `TextSampler` / `ImageSampler`。
- `create()` 先按 `-to-` 拆分任务得到输入/输出模态，查表选类（复合输入 `image-text` 回退到 `ImageSampler`），再用 `supports_task` 二次校验输出模态合法性。
- `TextSampler.sample()` 按 `output_modality` 分发到 chat/embeddings/rerank/image/speech 五个私有方法；chat 分支内部又分「场景模式」（`scenario.sample()` 取目标 token 数、`ignore_eos=True`）与「数据集模式」（直接取原文、`ignore_eos=False`）。
- 两种模式下 `UserChatRequest` 的 `max_tokens`、`num_prefill_tokens`、`ignore_eos` 字段差异，直接源自 `_sample_chat_request` 里 `None` 与 `scenario.sample()` 的分叉。
- prefix 缓存分两档：`--prefix-len` 全局生成一次并缓存复用、`--prefix-ratio` 按请求动态生成；两者都用确定种子保证多 worker 一致，并在前缀与后缀间插入随机分隔符以稳定 token 边界。
- 切换场景时要调用 `reset_prefix_cache()` 清空 `_shared_prefix`，避免跨场景误用旧前缀。

## 7. 下一步学习建议

本讲产出的 `UserRequest`（如 `UserChatRequest`）会被交给 Locust 虚拟用户去真正发送。下一步建议进入 **u3-l1（User 基类与 Locust 集成）**，阅读 [base_user.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py) 的 `sample()`，看它如何从 `environment.sampler` 取到本讲生成的请求，再把响应交给指标采集。如果想横向了解图像采样，可对照阅读 [image.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/image.py) 的 `ImageSampler.sample()`，它的分发结构与 `TextSampler` 完全同构。

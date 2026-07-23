# Task 任务定义体系

## 1. 本讲目标

本讲解决一个贯穿 `genai-bench` 全程的核心问题:**一次基准测试到底在测什么类型的请求?**

`genai-bench` 既能测聊天生成,也能测图像生成、向量化(embeddings)、重排(rerank)、语音合成。这些能力差异巨大,如果只用一个开关表示,代码会非常混乱。项目用一个统一的字符串——**任务(task)**——来表达「输入是什么模态、输出是什么模态」,并让这个字符串一路驱动采样器、请求类型、默认场景、迭代方式。

学完本讲,你应当能够:

- 说清 `<input>-to-<output>` 任务命名约定的含义,并能拆解任意一个任务字符串。
- 看懂每个后端(OpenAI / OCI / AWS Bedrock / Azure / GCP / Together / Cohere …)各自支持哪些任务,以及为什么不同后端支持的任务集合不一样。
- 读懂 `validate_task` 的三重校验链路(后端先就位 → 任务在后端支持范围内 → 后端细粒度限制),并理解任务如何决定默认场景、是否要数据集、迭代方式。

本讲承接 [u1-l5 协议数据模型](u1-l5-protocol-models.md):任务字符串最终决定了采样器会构造出哪种 `UserRequest`(如 `UserChatRequest` / `UserEmbeddingRequest`),而那是协议层的产物。

## 2. 前置知识

在进入源码前,先用大白话建立三个直觉。

**模态(modality)** 指数据的呈现形式,比如「文本」「图像」「音频」。一个 LLM 服务的请求,可以粗略拆成「喂进去的是什么」(输入模态)和「吐出来的是什么」(输出模态)。

**token 级基准** 是 `genai-bench` 的核心追求(见 [u1-l1](u1-l1-project-overview.md))。不同任务的「产出单位」不同:文本任务是 token,图像任务是像素张量,语音任务是音频帧。任务字符串正是为了让框架知道该用哪一套指标和哪一种采样方式。

**回调式校验(callback validation)** 是 `click` 的一种用法:每个命令行选项可以挂一个回调函数,在选项被解析后立即执行。`genai-bench` 把「任务合法性」的全部检查都写成了这类回调,而且依赖选项之间的先后顺序(`--api-backend` 必须先于 `--task` 校验)。这点在本讲的校验章节会反复出现。

## 3. 本讲源码地图

本讲涉及的关键文件如下:

| 文件 | 作用 |
|------|------|
| [docs/getting-started/task-definition.md](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/getting-started/task-definition.md) | 官方任务定义文档:命名约定与任务对照表 |
| [genai_bench/cli/option_groups.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py) | 用 `click` 声明 `--task` 选项及其枚举值 |
| [genai_bench/cli/validation.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py) | 任务校验函数、后端→User 映射、各任务的默认场景表 |
| [genai_bench/user/base_user.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py) | `BaseUser.supported_tasks` 与 `is_task_supported` |
| [genai_bench/user/openai_user.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py) | `OpenAIUser` 的 `supported_tasks` 声明(最全的后端样本) |
| [genai_bench/sampling/image.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/image.py) | 图像采样器,用来佐证「图像类任务为何需要数据集」 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块:**任务命名约定**、**后端任务支持表**、**校验与默认场景**。

### 4.1 任务命名约定

#### 4.1.1 概念说明

`genai-bench` 用一个字符串表达任务,统一格式是:

```
<input_modality>-to-<output_modality>
```

也就是「输入模态」加上 `-to-` 再加上「输出模态」。`-to-` 是固定分隔符,代码会拿它来拆字符串。

目前支持的任务有 7 种:

| 任务名 | 输入 | 输出 | 典型场景 |
|--------|------|------|----------|
| `text-to-text` | 文本 | 文本 | 聊天 / 问答(chat completions) |
| `text-to-image` | 文本 | 图像 | 文生图 |
| `text-to-embeddings` | 文本 | 向量 | 语义检索 |
| `text-to-rerank` | 文本 | 重排序号 | 文档重排 |
| `text-to-speech` | 文本 | 音频 | 语音合成 |
| `image-text-to-text` | 图像+文本 | 文本 | 视觉问答(VQA) |
| `image-to-embeddings` | 图像 | 向量 | 图像相似度 |

> 注意:官方文档 `task-definition.md` 的对照表里**没有列出 `text-to-rerank`**,但它在 CLI 枚举、`OpenAIUser.supported_tasks` 和默认场景表里都真实存在(见 4.2、4.3)。文档与代码存在轻微出入,以代码为准。

任务字符串的真正价值在于它**驱动后续选择**。正如官方文档所说:指定任务后,框架会依据输入/输出模态**自动**选定对应的采样器(`TextSampler` 或 `ImageSampler`)和请求类型(`UserChatRequest`、`UserEmbeddingRequest` 等)。任务字符串是这条数据流的「总开关」。

#### 4.1.2 核心流程

从命令行到采样器的「任务解析」过程可以概括为:

1. `click` 解析 `--task`,只接受枚举内的合法字符串(大小写不敏感)。
2. 校验回调拿到任务字符串,用 `-to-` 拆出输入/输出模态。
3. 输入模态决定采样器:`text` 走 `TextSampler`,`image` 走 `ImageSampler`。
4. 输出模态决定请求类型:`text`→`UserChatRequest`、`embeddings`→`UserEmbeddingRequest`、`rerank`→`UserReRankRequest`、`image`→`UserImageGenerationRequest`、`speech`→`UserTextToSpeechRequest`(这些都是 [u1-l5](u1-l5-protocol-models.md) 讲过的协议模型)。

关键在于第 2 步的拆分。Python 的 `str.split("-to-")` 会按**所有**非重叠的 `-to-` 切分,但由于任务命名里 `-to-` 只出现一次,拆出来一定是两段。

#### 4.1.3 源码精读

先看官方文档对命名约定的定义:

[docs/getting-started/task-definition.md:L5-L9](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/getting-started/task-definition.md#L5-L9) — 明确写出「每个任务遵循 `<input_modality>-to-<output_modality>` 模式」。

[docs/getting-started/task-definition.md:L24-L30](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/getting-started/task-definition.md#L24-L30) — 说明输入模态、输出模态的含义,以及任务会自动决定采样器和请求类型。

`--task` 选项在 CLI 层用 `click.Choice` 锁定枚举,大小写不敏感:

```python
# genai_bench/cli/option_groups.py
func = click.option(
    "--task",
    type=click.Choice(
        [
            "text-to-text", "text-to-image", "text-to-embeddings",
            "text-to-rerank", "text-to-speech",
            "image-text-to-text", "image-to-embeddings",
        ],
        case_sensitive=False,
    ),
    required=True,
    prompt=True,
    callback=validate_task,
    ...
)
```

完整定义见 [option_groups.py:L41-L63](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py#L41-L63)。注意它挂了 `callback=validate_task`,这是 4.3 要展开的校验入口。

真正用 `-to-` 拆字符串的代码在校验文件里:

```python
# genai_bench/cli/validation.py (validate_dataset_path_callback 内)
input_modality, output_modality = task.split("-to-")
if "image" in input_modality and value is None:
    ...
```

[validation.py:L113-L114](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L113-L114) — 这一行就是命名约定的「执行点」:拆出输入模态后,只要它含 `image`,就强制要求提供数据集。注意判断用的是子串 `"image" in input_modality`,所以 `image-text-to-text` 拆出的 `image-text` 也会命中。

#### 4.1.4 代码实践

**实践目标:**亲手验证 `split("-to-")` 的拆分语义,理解 `image-text-to-text` 这类多段任务名如何被解析。

**操作步骤:**

1. 在项目根目录(已安装 `genai-bench` 的虚拟环境里)新建脚本 `explore_task.py`:

```python
# 示例代码:不依赖项目运行时,只演示字符串拆分
def decompose_task(task: str):
    input_modality, output_modality = task.split("-to-")
    return input_modality, output_modality

for t in [
    "text-to-text",
    "image-text-to-text",
    "text-to-image",
    "image-to-embeddings",
]:
    inp, out = decompose_task(t)
    needs_dataset = "image" in inp
    print(f"{t:22s} -> input={inp!r:14s} output={out!r:12s} needs_image_dataset={needs_dataset}")
```

2. 运行:`python explore_task.py`

**需要观察的现象:**

- `image-text-to-text` 被拆成 `input='image-text'`、`output='text'`,且命中 `needs_image_dataset=True`。
- `text-to-image` 被拆成 `input='text'`、`output='image'`,**不**命中数据集要求——因为它输入是文本(文生图),不需要喂图片。

**预期结果:**

```
text-to-text           -> input='text'        output='text'        needs_image_dataset=False
image-text-to-text     -> input='image-text'  output='text'        needs_image_dataset=True
text-to-image          -> input='text'        output='image'       needs_image_dataset=False
image-to-embeddings    -> input='image'       output='embeddings'  needs_image_dataset=True
```

这个结果与 [validation.py:L113-L114](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L113-L114) 的判断完全一致。把脚本删掉即可,不要留在仓库里。

#### 4.1.5 小练习与答案

**练习 1:**给定 `task="image-to-embeddings"`,写出 `split("-to-")` 后的输入、输出模态。

> **答:**输入模态 `image`,输出模态 `embeddings`。由于输入含 `image`,该任务必须提供数据集。

**练习 2:**为什么 `text-to-image` 不需要数据集,而 `image-text-to-text` 需要?

> **答:**`text-to-image` 的输入是文本,框架可以用 tokenizer 合成任意长度的文本;它的「图像」是**输出**,由模型生成。而 `image-text-to-text` 的输入里**含图像**,必须有真实图片字节作为输入数据,所以需要数据集。关键看的是**输入**模态是否含 `image`,与输出无关。

---

### 4.2 后端任务支持表

#### 4.2.1 概念说明

「任务」是全局概念,但**不是每个后端都支持全部任务**。OpenAI API 能聊天、能文生图;AWS Bedrock 的不同模型接口能力各异;OCI 上的 Cohere 又是另一套。如果让用户给一个后端传一个它根本不支持的任务,框架应当在发请求之前就拦下,而不是把错误请求打到线上。

为此,`genai-bench` 在每个 `User` 子类上声明一个 `supported_tasks` 字典:

```python
supported_tasks = {
    "text-to-text": "chat",
    ...
}
```

这个字典有双重含义——**键**表示「这个后端支持哪个任务」,**值**表示「这个任务由 `User` 类里的哪个方法去执行」。比如 `"text-to-text": "chat"` 表示测聊天时,真正发请求的是 `OpenAIUser.chat` 方法。

#### 4.2.2 核心流程

后端与任务的匹配,由两个层次共同把关:

1. **后端选择层**:`--api-backend` 经 `validate_api_backend` 把字符串(如 `openai`)映射到对应的 `User` 类,存进 `ctx.obj["user_class"]`。
2. **任务匹配层**:`validate_task` 调用 `user_class.is_task_supported(task)`,本质就是查 `task in cls.supported_tasks`。不在字典里就报错,并打印该后端**实际**支持的任务清单,方便用户纠正。
3. **细粒度限制层**:某些任务即使在 `supported_tasks` 里,也只对部分后端开放(典型是 `text-to-rerank`)。这由一张额外的 `backend_task_restrictions` 表把关。

`is_task_supported` 的实现非常朴素:

```python
# genai_bench/user/base_user.py
@classmethod
def is_task_supported(cls, task: str) -> bool:
    return task in cls.supported_tasks
```

#### 4.2.3 源码精读

后端到 `User` 类的映射表如下(注意 `vllm` / `sglang` 复用 `OpenAIUser`,因为它们都实现了 OpenAI 兼容 API):

[validation.py:L25-L38](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L25-L38) — `API_BACKEND_USER_MAP`,把每个后端名映射到 `User` 类。

`OpenAIUser` 是支持任务最广的后端,它的 `supported_tasks`:

```python
# genai_bench/user/openai_user.py
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

[openai_user.py:L32-L41](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L32-L41) — 注意 `image-text-to-text` 和 `text-to-text` 都映射到同一个 `chat` 方法(注释里 `# Same method handles both text and image` 的思路在其他后端里也能看到),值是方法名字符串。

[base_user.py:L12-L25](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L12-L25) — `BaseUser` 定义了空的 `supported_tasks: Dict[str, str] = {}` 和 `is_task_supported` 类方法,子类只需覆写字典即可。

把各后端的 `supported_tasks` 汇总,就得到下面这张**后端任务支持表**:

| 后端名(`BACKEND_NAME`) | 支持的任务 |
|---|---|
| `openai`(及 `vllm`、`sglang`) | text-to-text、image-text-to-text、text-to-embeddings、text-to-rerank*、text-to-image、text-to-speech |
| `oci-openai` | text-to-image、text-to-speech |
| `oci-genai` | text-to-text |
| `cohere` | text-to-text、text-to-embeddings、image-to-embeddings |
| `oci-cohere` | text-to-text、text-to-rerank、text-to-embeddings、image-to-embeddings |
| `oci-cohere-v2` | text-to-text、image-text-to-text |
| `aws-bedrock` | text-to-text、text-to-embeddings、image-text-to-text |
| `azure-openai` | text-to-text、text-to-embeddings、image-text-to-text |
| `gcp-vertex` | text-to-text、text-to-embeddings、image-text-to-text |
| `together` | text-to-text、image-text-to-text、text-to-embeddings |

> 表格依据各 `User` 文件中 `supported_tasks = {...}` 的实际声明整理。`*` 标记的 `text-to-rerank` 有附加限制,见 4.3。

**关于 `text-to-rerank` 的特殊限制**:虽然 `OpenAIUser.supported_tasks` 里有 `text-to-rerank`,但标准的 OpenAI API 并没有 `/rerank` 端点,只有 vLLM / SGLang 这类自建推理引擎才有。所以项目又加了一张细粒度限制表:

[validation.py:L338-L340](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L338-L340) — `backend_task_restrictions = {"text-to-rerank": {"vllm", "sglang"}}`,把 `text-to-rerank` 限定在这两个后端。

也就是说,`text-to-rerank` 要通过两道关卡:先过 `is_task_supported`(类级,通过),再过 `backend_task_restrictions`(后端级,只有 vllm/sglang 通过)。用 `--api-backend openai --task text-to-rerank` 会在第二道关卡被拒。

#### 4.2.4 代码实践

**实践目标:**用源码本身验证上表,并理解 `is_task_supported` 的两道关卡。

**操作步骤:**

1. 在已安装 `genai-bench` 的环境里运行(导入 `genai_bench` 会触发 `gevent` monkey patch,属正常现象):

```python
# 示例代码
from genai_bench.cli.validation import API_BACKEND_USER_MAP

for name, cls in API_BACKEND_USER_MAP.items():
    tasks = ", ".join(sorted(cls.supported_tasks.keys()))
    print(f"{name:16s} -> {tasks}")
```

> 说明:`backend_task_restrictions` 是 `validate_task` 函数内的**局部变量**,不是模块级常量,无法直接 import。要观察它,请直接阅读 [validation.py:L338-L340](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L338-L340)。

2. 修正后运行脚本,打印每个后端的任务集合。

**需要观察的现象:**

- `openai` / `vllm` / `sglang` 三行打印的任务集合**完全相同**(都指向 `OpenAIUser.supported_tasks`)。
- 不同后端的任务集合确实不一样,例如 `oci-genai` 只有 `text-to-text`。

**预期结果:**脚本输出应与 4.2.3 的支持表一致。若发现某后端的实际 `supported_tasks` 与表格不符,以你本地的源码输出为准(并欢迎核对对应 `User` 文件)。`vllm`/`sglang` 是否真能跑 `text-to-rerank` 还需后端服务本身提供 `/rerank` 端点,框架只负责放行。

#### 4.2.5 小练习与答案

**练习 1:**哪个后端支持的任务最多?为什么 `vllm`、`sglang` 与它的任务集完全一样?

> **答:**`openai` 最多(6 个)。因为 `API_BACKEND_USER_MAP` 里 `"vllm": OpenAIUser`、`"sglang": OpenAIUser`,三者用的是**同一个** `User` 类,自然 `supported_tasks` 完全一致。

**练习 2:**`text-to-rerank` 已经在 `OpenAIUser.supported_tasks` 里了,为什么用 `--api-backend openai --task text-to-rerank` 仍会报错?

> **答:**因为它卡在第二道关卡 `backend_task_restrictions`:该表规定 `text-to-rerank` 只允许 `vllm` / `sglang`,标准 `openai` 后端的 API 没有 `/rerank` 端点,所以被拒。

---

### 4.3 校验与默认场景

#### 4.3.1 概念说明

任务字符串不止「合法与否」一个问题。一旦任务确定,还会连带决定三件事:

1. **默认场景**:`--traffic-scenario` 不传时,用哪一组默认流量场景?
2. **是否要数据集**:输入含图像时,必须提供 `--dataset-path` 或 `--dataset-config`。
3. **迭代方式**:是用并发数(`num_concurrency`)遍历,还是用批大小(`batch_size`)遍历?

这三件事都写在 `validation.py` 里,各自是一个 `click` 回调。它们共同构成「任务」的完整校验与派发语义。

`DEFAULT_SCENARIOS_BY_TASK` 是任务到默认场景的映射,它是本讲的实践重点。每个任务都有一组精心设计的默认场景(场景字符串本身的语法是下一讲 [u2-l2](u2-l2-scenario-definition.md) 的主题,这里只需把它们当成「一组预设参数」)。

#### 4.3.2 核心流程

`validate_task` 是任务校验的总入口,流程如下:

1. `task = value.lower()`,统一小写。
2. 从 `ctx.obj` 取出 `user_class`;若没有,说明 `--api-backend` 没先校验,直接报错(强调顺序)。
3. `user_class.is_task_supported(task)` → 不支持就报错并列出该后端支持的任务。
4. `backend_task_restrictions` 细粒度检查(目前只针对 `text-to-rerank`)。
5. 把任务对应的方法引用存进 `ctx.obj["user_task"]`,供后续 `benchmark` 函数体使用。

任务确定后,其他回调据此派发:

- `validate_traffic_scenario_callback`:用户没传场景时,若提供了数据集则用 `["dataset"]`,否则取 `DEFAULT_SCENARIOS_BY_TASK[task]`;若任务不在默认表里就报错。
- `validate_dataset_path_callback`:输入含 `image` 且无数据集 → 报错。
- `validate_iteration_params`:`text-to-embeddings` / `text-to-rerank` 强制用 `batch_size` 迭代,其余用 `num_concurrency`。

任务与并发遍历次数的关系(回顾 [u1-l2](u1-l2-install-and-first-run.md) 提到的总运行次数):

\[
\text{total\_runs} = |\text{场景数}| \times |\text{并发档位数}|
\]

对 `text-to-embeddings` 这类任务,「并发档位」被换成「批大小档位」(`DEFAULT_BATCH_SIZES`),其余任务用 `DEFAULT_NUM_CONCURRENCIES`。

#### 4.3.3 源码精读

先看默认场景表。`validation.py` 先为每类任务定义一组场景列表,再汇总成 `DEFAULT_SCENARIOS_BY_TASK`:

[validation.py:L43-L49](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L43-L49) — `DEFAULT_SCENARIOS_FOR_CHAT`,文本聊天的默认场景,混用了正态分布 `N(...)` 和确定性分布 `D(...)`。

[validation.py:L52-L64](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L52-L64) — 视觉 `I(...)` 与向量 `E(...)` 的默认场景。

[validation.py:L66-L88](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L66-L88) — 重排 `R(...)`、图像生成 `I(...)`、语音 `A(...)` 的默认场景。

最终汇总:

```python
# genai_bench/cli/validation.py
DEFAULT_SCENARIOS_BY_TASK = {
    "text-to-text": DEFAULT_SCENARIOS_FOR_CHAT,
    "text-to-rerank": DEFAULT_SCENARIOS_FOR_RERANK,
    "text-to-image": DEFAULT_SCENARIOS_FOR_IMAGE_GENERATION,
    "text-to-speech": DEFAULT_SCENARIOS_FOR_SPEECH,
    "image-text-to-text": DEFAULT_SCENARIOS_FOR_VISION,
    "text-to-embeddings": DEFAULT_SCENARIOS_FOR_EMBEDDING,
    "image-to-embeddings": DEFAULT_SCENARIOS_FOR_VISION,
    # add other tasks and default scenarios as needed
}
```

[validation.py:L90-L99](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L90-L99) — 任务→默认场景的总映射。注意 `image-text-to-text` 和 `image-to-embeddings` 共用 `DEFAULT_SCENARIOS_FOR_VISION`,末尾注释提示可按需扩展。

接着看三个派发回调。场景回退逻辑:

```python
# validate_traffic_scenario_callback
if value:
    return [validate_scenario_callback(v) for v in value]
if ctx.params.get("dataset_path") or ctx.params.get("dataset_config"):
    return ["dataset"]
if task not in DEFAULT_SCENARIOS_BY_TASK:
    raise click.BadParameter(f"No default traffic scenarios defined for task '{task}'")
return DEFAULT_SCENARIOS_BY_TASK[task]
```

[validation.py:L151-L169](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L151-L169) — 优先级:用户显式场景 > 数据集模式 > 任务默认场景 > 报错。

数据集强制要求:

[validation.py:L105-L127](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L105-L127) — `validate_dataset_path_callback`:输入含 `image` 且 `--dataset-path` 与 `--dataset-config` 都没给,直接抛 `BadParameter`。

迭代方式选择:

[validation.py:L208-L249](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L208-L249) — `validate_iteration_params`:`text-to-embeddings` / `text-to-rerank` 用 `batch_size`,其余用 `num_concurrency`,并把决定写回 `ctx.params`。

最后是任务校验总入口:

[validation.py:L313-L352](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L313-L352) — `validate_task` 完整实现。第 320–325 行强制 `--api-backend` 先于 `--task` 校验;第 328–333 行做类级支持检查;第 338–347 行做 `backend_task_restrictions` 细粒度检查;第 350 行把方法引用 `getattr(user_class, supported_tasks[task])` 存进 `ctx.obj["user_task"]`——这正是「任务字符串→执行方法」的最终落点。

#### 4.3.4 代码实践

**实践目标(本讲指定实践):**阅读 `DEFAULT_SCENARIOS_BY_TASK`,为某个任务设想一个合理的自定义场景字符串,并解释为何图像类任务需要数据集。

**操作步骤:**

1. 在已安装 `genai-bench` 的环境里运行:

```python
# 示例代码
from genai_bench.cli.validation import DEFAULT_SCENARIOS_BY_TASK

for task, scenarios in DEFAULT_SCENARIOS_BY_TASK.items():
    print(f"{task:22s} -> {scenarios}")
```

2. **为 `text-to-image` 设想一个自定义场景。**图像生成场景语法是 `I(width,height,num_images)`(见 [validation.py:L384-L388](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L384-L388) 的提示:`Set image count via --traffic-scenario I(width,height,num_images)`)。默认值 `I(1024,1024)` 是单张 1024×1024;一个合理自定义是 `I(768,768,4)`——一次请求生成 4 张 768×768 的图,用来压测批量出图吞吐。把它传给 `--traffic-scenario`:

```bash
genai-bench benchmark --api-backend openai --task text-to-image \
    --traffic-scenario "I(768,768,4)" ...
```

3. **回答「为何图像类任务需要数据集」。**阅读图像采样器 [sampling/image.py:L148-L188](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/sampling/image.py#L148-L188) 的 `_sample_image_and_text` 方法:它从 `self.data`(加载好的数据集)里 `random.choices` 取出真实图片条目,再经 `process_image` 编码成 base64 或校验 URL。

**需要观察的现象:**

- 步骤 1 打印出 7 个任务的默认场景,其中 `image-text-to-text` 与 `image-to-embeddings` 共用同一组 `I(...)` 视觉场景。
- 步骤 3 可见:文本任务的输入可以用 tokenizer 随机合成任意长度(所以 `D(100,100)` 能凭空造出 100 个输入 token);但**图像内容无法凭空合成**——场景 `I(512,512)` 只规定了「缩放到 512×512」这个目标尺寸,真正的图片像素必须来自数据集。没有数据集,就没有可发送的图片字节。

**预期结果:**步骤 1 输出与 4.3.3 列出的映射一致;自定义场景 `I(768,768,4)` 语义成立(需在下一讲 [u2-l2](u2-l2-scenario-definition.md) 学完场景正则后,可用 `Scenario.validate("I(768,768,4)")` 验证其合法性)。图像任务强依赖数据集的根本原因:文本可合成、图像不可合成,场景只描述尺寸不提供内容。若本地无图像数据集可加载,步骤 3 的运行验证标注为「待本地验证」,但源码层面的结论已确定。

#### 4.3.5 小练习与答案

**练习 1:**如果 `--task text-to-text` 且既不传 `--traffic-scenario` 也不传数据集,框架会用哪组场景?一共默认几个场景?

> **答:**走 `validate_traffic_scenario_callback` 的最后分支,取 `DEFAULT_SCENARIOS_BY_TASK["text-to-text"]`,即 `DEFAULT_SCENARIOS_FOR_CHAT`,共 5 个场景(见 [validation.py:L43-L49](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L43-L49))。

**练习 2:**`text-to-embeddings` 默认用 `num_concurrency` 还是 `batch_size` 迭代?为什么和聊天任务不同?

> **答:**用 `batch_size`。因为向量/embedding 服务通常按批处理(batch)而非按并发连接来衡量吞吐,`validate_iteration_params` 会把这类任务(含 `text-to-rerank`)强制切成 `batch_size` 迭代,并把并发数置为 `[1]`(见 [validation.py:L225-L230](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L225-L230))。

**练习 3:**`validate_task` 为什么要先检查 `ctx.obj["user_class"]` 是否存在,而不是直接查 `supported_tasks`?

> **答:**因为任务合法性是**相对于后端**的——同一个任务,A 后端支持、B 后端未必支持。必须先由 `--api-backend` 确定好 `user_class`,才能用 `user_class.is_task_supported(task)` 判断。这也强制了 CLI 选项的校验顺序:`--api-backend` 必须先于 `--task`。

---

## 5. 综合实践

把三个最小模块串起来,完成一次「任务定义全链路」的纸面推演。

**任务:**假设你要为一个部署在 vLLM 上的多模态模型设计一次基准测试,目标是比较「纯文本聊天」和「图文问答」两种负载。请完成下列各步:

1. **确定任务字符串**:纯文本聊天用 `text-to-text`;图文问答用 `image-text-to-text`。
2. **核对后端支持**:查 4.2 的支持表,确认 `vllm`(= `OpenAIUser`)同时支持这两个任务。
3. **检查数据集要求**:对 `image-text-to-text`,因输入含 `image`,必须准备一份图像数据集(本地文件、HuggingFace 或 `--dataset-config`),否则 `validate_dataset_path_callback` 会报错。
4. **预测默认场景**:若都不传 `--traffic-scenario`,`text-to-text` 会用 `DEFAULT_SCENARIOS_FOR_CHAT`(5 个),`image-text-to-text` 会用 `DEFAULT_SCENARIOS_FOR_VISION`(3 个 `I(...)` 场景)。
5. **预测迭代方式**:两者都不是 embeddings/rerank,故都用 `num_concurrency` 迭代,默认并发档位 `DEFAULT_NUM_CONCURRENCIES`(9 档)。
6. **估算总运行次数**:纯文本约 \(5 \times 9 = 45 \) 次 run;图文约 \(3 \times 9 = 27 \) 次 run(回顾 [u1-l2](u1-l2-install-and-first-run.md) 的 `total_runs` 公式)。

**交付物:**一张表,列出两种任务的「后端支持 / 是否需数据集 / 默认场景 / 迭代方式 / 估计 run 数」,并用一句话解释 `image-text-to-text` 为何 run 数更少(默认场景更少)。

完成后再回到 [validation.py:L313-L352](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L313-L352) 通读一遍 `validate_task`,你会发现整条校验链路已经能从头讲到尾。

## 6. 本讲小结

- 任务用 `<input>-to-<output>` 字符串表达,代码用 `task.split("-to-")` 拆出输入/输出模态,`-to-` 是固定分隔符。
- 任务字符串是总开关:输入模态决定采样器(`TextSampler` / `ImageSampler`),输出模态决定请求类型(对应 [u1-l5](u1-l5-protocol-models.md) 的 `UserRequest` 子类)。
- 不同后端支持的任务集合不同,由每个 `User` 子类的 `supported_tasks` 字典声明;`is_task_supported` 就是查字典成员。
- `text-to-rerank` 是特例:类级支持但受 `backend_task_restrictions` 限定,只有 `vllm` / `sglang` 能真正使用。
- 任务还连带决定默认场景(`DEFAULT_SCENARIOS_BY_TASK`)、是否强制数据集(输入含 `image`)、迭代方式(embeddings/rerank 用 `batch_size`,其余用 `num_concurrency`)。
- 校验依赖选项顺序:`--api-backend` 必须先于 `--task`,因为任务合法性是相对后端而言的。

## 7. 下一步学习建议

本讲把「任务是什么、谁支持、怎么校验」讲透了,但场景字符串(如 `N(480,240)/(300,150)`、`D(100,100)`、`I(512,512)`、`E(64)`、`R(64,100)`、`A(100)`)的**语法与解析**还没展开。下一讲 [u2-l2 场景定义与解析](u2-l2-scenario-definition.md) 会进入 `genai_bench/scenarios/`,讲清楚 `Scenario` 基类的注册表机制和 `from_string` 工厂。

如果你更关心「任务如何变成真正的 HTTP 请求」,可以先跳到 [u3-l1 User 基类与 Locust 集成](u3-l1-base-user-and-locust.md),看 `supported_tasks` 的值(方法名字符串)是如何被 `ctx.obj["user_task"]` 调用的。建议按 U2 的顺序学完场景与采样,再进 U3。

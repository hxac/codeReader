# 五分钟上手 pipeline API

## 1. 本讲目标

本讲是「让模型真正跑起来」的第一站。学完后你应该能够：

- 用 `pipeline()` 一行代码完成一个推理任务（文本分类、文本生成、图像分类等）。
- 说清楚 `pipeline()` 这一行代码内部到底做了哪三件事：**预处理 → 模型推理 → 后处理**。
- 知道如何通过 `task`、`model`、`device` 三个参数控制 pipeline 装哪个任务、用哪个模型、跑在哪个设备上。
- 能在源码里定位 `pipeline()` 工厂函数与 `Pipeline` 基类，并理解它们各自的职责。

本讲只解决「pipeline 是什么、怎么用、内部怎么串起来的」这个问题，不深入 tokenizer、model、generation 的内部细节——那些是后续讲义（u3 分词器、u5 模型基类、u8 生成）的主题。

## 2. 前置知识

读本讲前，建议你已经具备以下认知（来自 u1-l1 ~ u1-l4）：

- **三大预训练对象**：transformers 里每个模型都由 Configuration（配置）、Model（模型）、Preprocessing（预处理）三类对象构成，三者共享 `from_pretrained` / `save_pretrained` 统一接口。pipeline 就是把这三类对象「粘」成一条端到端流水线的胶水。
- **模型默认从 Hub 下载并缓存**：pipeline 背后会自动调用 `from_pretrained` 去下载权重、配置与预处理对象，并命中本地缓存（详见 u1-l2）。
- **惰性导入**：`import transformers` 不会立刻加载重量级后端，`pipeline` 也是在你真正调用时才加载需要的模型与 tokenizer。

此外你需要一点点 PyTorch 直觉：

- **张量（Tensor）**：可以理解成多维数组，模型只认张量。tokenizer 和 image_processor 的工作，就是把「人类可读的输入」变成张量。
- **设备（device）**：张量要么在 CPU 上、要么在 GPU（`cuda`）上。模型推理时，输入张量和模型必须在同一个设备上。

> 不熟悉也没关系，本讲会用具体的输入输出把这些概念讲透。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/transformers/pipelines/__init__.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/__init__.py) | pipeline 子系统的入口。定义 `pipeline()` 工厂函数、`SUPPORTED_TASKS` 任务注册表、任务别名，以及 `PipelineRegistry`。 |
| [src/transformers/pipelines/base.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/base.py) | 定义 `Pipeline` 基类及其三段式骨架（`preprocess` / `_forward` / `postprocess`），还有 `PipelineRegistry`、`load_model` 等。 |
| [src/transformers/pipelines/text_classification.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/text_classification.py) | `TextClassificationPipeline`：文本分类/情感分析的具体实现，是观察三段式的最佳样本。 |
| [src/transformers/pipelines/image_classification.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/image_classification.py) | `ImageClassificationPipeline`：图像分类的具体实现，用图像处理器替代 tokenizer，对照理解。 |
| [docs/source/en/quicktour.md](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/quicktour.md) | 官方快速上手文档，给出 pipeline / Trainer 的最小示例，是本讲的官方对照。 |

一句话记忆：**`__init__.py` 负责「按 task 装配出一条 pipeline」，`base.py` 负责「这条 pipeline 跑起来时分三步走」**。

## 4. 核心概念与源码讲解

### 4.1 pipeline() 工厂函数：一行代码背后的任务分发

#### 4.1.1 概念说明

新手最常写的第一行 transformers 代码大概是：

```python
from transformers import pipeline
classifier = pipeline("sentiment-analysis")
classifier("I love transformers!")
# [{'label': 'POSITIVE', 'score': 0.99...}]
```

问题来了：`pipeline("sentiment-analysis")` 这一行，凭什么就能「自动」下载好模型、加载好 tokenizer、并组装成一个能直接 `__call__` 的对象？

答案是：`pipeline()` 是一个**工厂函数（factory）**。它的职责不是自己去做推理，而是像一名调度员，根据你给的 `task`，从一张**任务注册表**里查出「该用哪个 Pipeline 类、该用哪类模型、默认用哪个 checkpoint」，然后把模型、tokenizer/processor 等零件逐一加载、组装，最后返回一个组装好的 Pipeline 实例。

之所以叫「工厂」，是因为它**生产对象**而不是执行业务逻辑——真正的业务逻辑（推理）在它返回的 `Pipeline` 实例里（见 4.2）。

#### 4.1.2 核心流程

`pipeline()` 的执行流程可以概括为五步：

```text
1. 确定 task
   - 显式传了 task?  -> 用它（并解析别名，如 sentiment-analysis -> text-classification）
   - 没传 task 但传了 model? -> 联网查该模型的 pipeline_tag 自动推断 task
2. 查注册表 SUPPORTED_TASKS
   - 拿到这一项的: impl(具体 Pipeline 类)、pt(模型 Auto 类)、default(默认 checkpoint)
3. 加载 config 与 model
   - 没指定 model -> 用 default checkpoint（并打印一条警告）
   - 调用 AutoConfig / AutoModelForXxx.from_pretrained 下载权重
4. 加载预处理组件
   - 根据 Pipeline 类的 _load_* 标志，决定要不要加载 tokenizer / image_processor / feature_extractor / processor
5. 组装并返回
   - return pipeline_class(model=..., tokenizer=..., task=task, ...)
```

第 2 步依赖的关键数据结构是任务注册表，它是一张 `task 字符串 -> 配置字典` 的映射，每个任务登记了「实现类、可选模型类、默认模型、输入类型」四样东西。

#### 4.1.3 源码精读

**① 任务注册表与别名** —— `SUPPORTED_TASKS` 是整张表的真身，`TASK_ALIASES` 是快捷别名。

[src/transformers/pipelines/__init__.py:136-140](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/__init__.py#L136-L140) 给出三个别名，注意 `sentiment-analysis` 就是 `text-classification` 的别名：

```python
TASK_ALIASES = {
    "sentiment-analysis": "text-classification",
    "ner": "token-classification",
    "text-to-speech": "text-to-audio",
}
```

注册表里每一项的结构（以文本分类为例）见 [src/transformers/pipelines/__init__.py:166-171](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/__init__.py#L166-L171)：

```python
"text-classification": {
    "impl": TextClassificationPipeline,        # 用哪个 Pipeline 类
    "pt": (AutoModelForSequenceClassification,),  # 用哪类模型
    "default": {"model": ("distilbert/distilbert-base-uncased-finetuned-sst-2-english", "714eb0f")},  # 默认 checkpoint + revision
    "type": "text",
},
```

这就是「不传 model 时，pipeline 凭什么知道下哪个模型」的答案——表里写死了默认 checkpoint。整张表用 `PipelineRegistry` 包起来，见 [src/transformers/pipelines/__init__.py:296](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/__init__.py#L296)。

**② 工厂函数签名** —— [src/transformers/pipelines/__init__.py:671-690](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/__init__.py#L671-L690) 列出全部参数。最常用的三个就是 `task`、`model`、`device`，其余如 `tokenizer`/`image_processor`/`processor` 允许你显式覆盖预处理组件，`dtype` 控制权重精度，`device_map` 配合 accelerate 做大模型分片。

它的 docstring 一语道破 pipeline 由三部分构成，见 [src/transformers/pipelines/__init__.py:694-699](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/__init__.py#L694-L699)：

```python
# A pipeline consists of:
#   - One or more components for pre-processing model inputs (tokenizer / image_processor / feature_extractor / processor)
#   - A model that generates predictions from the inputs.
#   - Optional post-processing steps to refine the model's output.
```

这恰好对应本讲反复强调的三段式。

**③ 校验 task 并解析别名** —— 工厂内部把校验委托给 `check_task`（见 [src/transformers/pipelines/__init__.py:323-359](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/__init__.py#L323-L359)），最终落到 `PipelineRegistry.check_task`，逻辑在 [src/transformers/pipelines/base.py:1352-1359](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/base.py#L1352-L1359)：先把别名换成正式名，再在表里查；查不到就抛 `KeyError`。

**④ 推断默认模型** —— 当用户没传 `model` 时，工厂会查表取默认 checkpoint，并打印一条「建议在产品环境显式指定 model」的警告，见 [src/transformers/pipelines/__init__.py:979-989](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/__init__.py#L979-L989)。`get_default_model_and_revision` 的实现见 [src/transformers/pipelines/base.py:284-311](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/base.py#L284-L311)。

**⑤ 装配返回** —— 加载好 model、tokenizer、image_processor 等零件后，最后一行直接实例化目标 Pipeline 类，见 [src/transformers/pipelines/__init__.py:1124](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/__init__.py#L1124)：

```python
return pipeline_class(model=model, task=task, **kwargs)
```

> 小贴士：是否加载某个预处理组件，取决于目标 Pipeline 类身上的 `_load_*` 标志（见 4.2.3）。工厂在 [src/transformers/pipelines/__init__.py:1047-1101](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/__init__.py#L1047-L1101) 读取这些标志，决定要不要去加载 tokenizer / image_processor / feature_extractor / processor / video_processor。所以文本任务会加载 tokenizer、视觉任务会加载 image_processor，互不干扰。

#### 4.1.4 代码实践

**实践目标**：亲手验证「task → 具体 Pipeline 类」的分发关系，并观察默认模型警告。

**操作步骤**（确保已按 u1-l2 安装好 `torch` 与 `transformers`）：

1. 新建 `pipe_factory.py`：

   ```python
   from transformers import pipeline, pipelines

   # 不传 model，触发「默认模型」警告
   classifier = pipeline("sentiment-analysis")

   # 打印工厂实际装配出的类
   print("实际类型:", type(classifier).__name__)
   print("默认模型:", classifier.model.config._name_or_path)

   # 用别名和正式名各试一次，确认它们是同一个类
   a = pipeline("ner")
   b = pipeline("token-classification")
   print(type(a).__name__, type(b).__name__)
   ```

2. 运行：`python pipe_factory.py`

**需要观察的现象**：
- 终端先打印一条 `No model was supplied, defaulted to ...` 的警告（来自 4.1.3 第④点的源码）。
- `实际类型` 应为 `TextClassificationPipeline`（因为 `sentiment-analysis` 是 `text-classification` 的别名）。
- `ner` 与 `token-classification` 装配出的类**相同**（都是 `TokenClassificationPipeline`）——验证别名解析。

**预期结果**：三个类名分别落到 `TextClassificationPipeline` 与 `TokenClassificationPipeline`，证明工厂确实按 task 查表分发。

**如果无法确定运行结果**：在没有网络/未配置缓存时下载默认 checkpoint 会失败，可改为显式传一个本地或已缓存模型 `pipeline("text-classification", model="distilbert/distilbert-base-uncased-finetuned-sst-2-english")` 再观察——「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：调用 `pipeline()` 时既不传 `task` 也不传 `model` 会发生什么？

**参考答案**：会直接报错。源码 [src/transformers/pipelines/__init__.py:856-861](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/__init__.py#L856-L861) 明确写了 `Impossible to instantiate a pipeline without either a task or a model`。两者至少给一个；若只给 `model`（字符串），则联网查它的 `pipeline_tag` 来推断 task。

**练习 2**：`SUPPORTED_TASKS` 里每个任务有 `impl`、`pt`、`default`、`type` 四个键，它们分别用来干什么？

**参考答案**：`impl` 决定装配哪个 Pipeline 类（业务逻辑）；`pt` 给出可用的模型 Auto 类元组，供 `load_model` 加载权重；`default` 存默认 checkpoint 与 revision，用于用户没指定 model 时；`type` 标注输入模态（text/image/audio/multimodal/video）。

---

### 4.2 Pipeline 基类：preprocess → forward → postprocess 三段式

#### 4.2.1 概念说明

工厂函数装配好的对象，几乎都是 `Pipeline` 基类的子类。`Pipeline` 把「一次推理」抽象成固定三段式骨架：

> **输入 → 预处理（Tokenization）→ 模型推理（Model Inference）→ 后处理（Post-Processing）→ 输出**

这个公式在基类 docstring 里白纸黑字写着，见 [src/transformers/pipelines/base.py:762](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/base.py#L762)。

为什么非要拆成三段？因为不同任务（分类、生成、分割、问答……）**前后处理完全不同，但中间那段「喂给模型」高度一致**。把它拆开、中间那段尽量保持「热路径」最快，就能用同一套骨架支撑几十种任务。每个子类只需要实现三个方法，不必重复造轮子。

`Pipeline` 是抽象基类：`preprocess`、`_forward`、`postprocess`、`_sanitize_parameters` 都是 `@abstractmethod`（见 4.2.3），子类不实现就无法实例化。

#### 4.2.2 核心流程

单条输入推理时，骨架用一个极简的 `run_single` 串起三段（见 [src/transformers/pipelines/base.py:1296-1300](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/base.py#L1296-L1300)）：

```text
run_single(inputs):
    model_inputs  = preprocess(inputs)        # ① 预处理：原始输入 -> 张量字典
    model_outputs = forward(model_inputs)     # ② 推理：  张量 -> 模型原始输出
    outputs       = postprocess(model_outputs)# ③ 后处理：原始输出 -> 友好结果
    return outputs
```

三段的职责分工：

| 阶段 | 输入 | 输出 | 做什么 |
|---|---|---|---|
| `preprocess` | 原始输入（字符串/图片/音频） | 张量字典（如 `input_ids`、`attention_mask`、`pixel_values`） | 调用 tokenizer / image_processor / feature_extractor，把人读得懂的输入变成模型认识的张量 |
| `forward`（内部 `_forward`） | 张量字典 | 模型原始输出（`ModelOutput`，含 logits 等） | 把张量搬到模型所在设备，关掉梯度，跑一次模型 |
| `postprocess` | 模型原始输出 | 友好结果（标签+分数、生成文本、边界框……） | 把数值化的张量翻译成人话（如 softmax + `id2label`） |

注意中间的 `forward` 是 `Pipeline` 提供的**公共方法**，它在 `_forward` 外面套了两层：设备搬运（`ensure_tensor_on_device`）和推理上下文（`torch.no_grad`），见 [src/transformers/pipelines/base.py:1178-1185](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/base.py#L1178-L1185)。所以子类只需实现纯推理的 `_forward`，设备和梯度的事基类包了。

还有一个第四个抽象方法 `_sanitize_parameters`，它负责把调用时随手传的 kwargs（如 `top_k=5`、`function_to_apply="softmax"`）**分流**到三段各自的参数字典里，是 pipeline「参数路由」的关键（见 4.2.4）。

#### 4.2.3 源码精读

**① 四个抽象方法** —— [src/transformers/pipelines/base.py:1132-1173](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/base.py#L1132-L1173) 定义了 `_sanitize_parameters` / `preprocess` / `_forward` / `postprocess` 四个抽象方法。`run_single` 正是调用前三者，见 [src/transformers/pipelines/base.py:1296-1300](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/base.py#L1296-L1300)：

```python
def run_single(self, inputs, preprocess_params, forward_params, postprocess_params):
    model_inputs = self.preprocess(inputs, **preprocess_params)
    model_outputs = self.forward(model_inputs, **forward_params)
    outputs = self.postprocess(model_outputs, **postprocess_params)
    return outputs
```

**② `_load_*` 标志** —— 基类用一组类属性声明「我这个 pipeline 需要哪些预处理组件」，见 [src/transformers/pipelines/base.py:776-780](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/base.py#L776-L780)：

```python
_load_processor = None
_load_image_processor = None
_load_video_processor = None
_load_feature_extractor = None
_load_tokenizer = None
```

取值含义：`True`（必需，缺失就报错）、`None`（可选，有就加载）、`False`（永不加载）。例如 `TextClassificationPipeline` 把 `_load_tokenizer = True`、其余设 `False`（见 [src/transformers/pipelines/text_classification.py:75-78](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/text_classification.py#L75-L78)）；`ImageClassificationPipeline` 则 `_load_image_processor = True`、其余 `False`（见 [src/transformers/pipelines/image_classification.py:98-101](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/image_classification.py#L98-L101)）。这就是工厂「按需加载预处理组件」的依据。

**③ device 解析** —— 基类 `__init__` 负责把用户给的 `device` 解析成真正的 `torch.device`，并把模型 `.to(device)`，见 [src/transformers/pipelines/base.py:820-874](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/base.py#L820-L874)。要点：
- 不传 `device` 时，若模型用 accelerate 加载（有 `hf_device_map`）则取其首个设备，否则默认 `device=0`；
- `device=0` 会按可用硬件依次尝试 `cuda:0` → `npu` → `hpu` → `xpu` → `mps`，都没有则落到 `cpu`；
- 传 `device=-1` 或 `"cpu"` 强制走 CPU；
- `device` 与 `device_map` 不能同时给（会冲突，工厂里也会警告）。

**④ 文本生成的特殊性** —— 文本生成（`text-generation`）不是「一次 forward 出结果」，而是要**自回归地反复调用模型**。基类用 `_pipeline_calls_generate` 标志区分这类 pipeline，见 [src/transformers/pipelines/base.py:783](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/base.py#L783)（默认 `False`）。`TextGenerationPipeline` 把它设为 `True`（[src/transformers/pipelines/text_generation.py:86](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/text_generation.py#L86)），于是基类 `__init__` 会额外准备 `generation_config`（[src/transformers/pipelines/base.py:880-898](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/base.py#L880-L898)），其 `_forward` 内部走的是 `model.generate(...)` 而非单次 `model(...)`。生成的默认参数（`max_new_tokens=256, do_sample=True, temperature=0.7`）见 [src/transformers/pipelines/text_generation.py:93-97](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/text_generation.py#L93-L97)。生成的完整机制留待 u8 专题讲解。

#### 4.2.4 代码实践

**实践目标**：手动复刻一次 `run_single`，亲眼看到三段的中间产物，建立「pipeline 内部到底转了几手」的直觉。

**操作步骤**：

1. 新建 `pipe_stages.py`（用情感分析做样本，因为它最轻）：

   ```python
   from transformers import pipeline

   pipe = pipeline("sentiment-analysis",
                   model="distilbert/distilbert-base-uncased-finetuned-sst-2-english")
   text = "I love transformers!"

   # ① 预处理：手动调用 preprocess，看 tokenizer 把文本变成了什么
   model_inputs = pipe.preprocess(text)
   print("【预处理产物 keys】", list(model_inputs.keys()))
   print("【input_ids】", model_inputs["input_ids"])

   # ② 推理：调用公共 forward（基类会自动搬设备 + no_grad）
   model_outputs = pipe.forward(model_inputs)
   print("【模型输出 keys】", list(model_outputs.keys()))
   print("【logits shape】", model_outputs["logits"].shape)

   # ③ 后处理：调用 postprocess，把 logits 翻译成 label + score
   result = pipe.postprocess(model_outputs)
   print("【最终结果】", result)

   # 对照：直接 __call__，结果应一致
   print("【__call__ 结果】", pipe(text))
   ```

2. 运行：`python pipe_stages.py`

**需要观察的现象**：
- ① 的 `input_ids` 是一串整数（token id），开头通常有特殊 token（如 `101` = `[CLS]`），结尾有 `102` = `[SEP]`。这就是 tokenizer 把文本「切碎编号」的结果。
- ② 的 `logits` 形状类似 `torch.Size([1, 2])`（batch=1，2 个类别）。
- ③ 把 logits 经 softmax 后，挑出最大者，用 `id2label` 翻译成 `POSITIVE`/`NEGATIVE` 并附上分数。
- 手动三段的结果与 `pipe(text)` 完全一致——证明 `__call__` 内部走的就是这条 `run_single`。

**预期结果**：最终打印类似 `[{'label': 'POSITIVE', 'score': 0.999...}]`。

> 说明：`pipe.preprocess` / `pipe.forward` / `pipe.postprocess` 是公开方法，完全可以单独调用。它们正是 `run_single`（[src/transformers/pipelines/base.py:1296-1300](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/base.py#L1296-L1300)）里那三步。如果你对某些输出含义不确定，标注「待本地验证」即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Pipeline` 把推理拆成 `_forward`（私有）和 `forward`（公共）两层，而不是直接让子类实现 `forward`？

**参考答案**：为了把「与任务无关的通用杂事」（把张量搬到正确设备、用 `torch.no_grad` 关掉梯度训练部分、把输出搬回 CPU）集中在公共 `forward` 里（见 [src/transformers/pipelines/base.py:1178-1185](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/base.py#L1178-L1185)），让子类的 `_forward` 只关心「喂给模型」这条最热路径，跑得尽量快。

**练习 2**：如果调用 `pipe(text, top_k=3)`，这个 `top_k` 是怎么传到 `postprocess` 手里的？

**参考答案**：经过 `_sanitize_parameters` 分流。`__call__` 会先调用 `_sanitize_parameters(**kwargs)`，把 `top_k` 放进「后处理参数字典」（见 [src/transformers/pipelines/text_classification.py:87-103](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/text_classification.py#L87-L103)），最后由 `run_single` 以 `postprocess(model_outputs, **postprocess_params)` 传进去。

---

### 4.3 对比两条真实流水线：文本分类 vs 图像分类

#### 4.3.1 概念说明

4.2 讲的是骨架，本节用两个最直观的具体子类把骨架「填上肉」。文本分类（`TextClassificationPipeline`）和图像分类（`ImageClassificationPipeline`）的**后处理几乎一样**（都是 logits → softmax/sigmoid → `id2label`），但**预处理完全不同**：前者用 tokenizer 处理字符串，后者用 image_processor 处理图片。放在一起对比，你能一眼看出 pipeline 框架「哪里变了、哪里没变」。

#### 4.3.2 核心流程

两条流水线在三段上的差异：

| 阶段 | 文本分类 | 图像分类 |
|---|---|---|
| 预处理 | `tokenizer(text)` → `input_ids`/`attention_mask` | `load_image(url)` → `image_processor(image)` → `pixel_values` |
| 推理 | `model(**inputs)` → `logits` | `model(**inputs)` → `logits`（**相同**） |
| 后处理 | softmax/sigmoid + `id2label` → `[{label, score}]` | softmax/sigmoid + `id2label` → `[{label, score}]`（**几乎相同**） |

结论：**任务差异主要落在预处理，模型推理与后处理高度一致**——这正是把推理拆成统一骨架的价值。

#### 4.3.3 源码精读

**① 文本分类的三段**（[src/transformers/pipelines/text_classification.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/text_classification.py)）：

- 预处理（[:154-169](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/text_classification.py#L154-L169)）：调 `self.tokenizer(inputs, return_tensors="pt", ...)`，把字符串变成 PyTorch 张量。
- 推理（[:171-176](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/text_classification.py#L171-L176)）：`self.model(**model_inputs)`，并显式关掉 `use_cache`。
- 后处理（[:178-219](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/text_classification.py#L178-L219)）：取 `logits[0]`，按 `problem_type`/`num_labels` 选 sigmoid 或 softmax，再用 `self.model.config.id2label` 把下标翻译成标签名。

**② 图像分类的三段**（[src/transformers/pipelines/image_classification.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/image_classification.py)）：

- 预处理（[:183-188](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/image_classification.py#L183-L188)）：先用 `load_image(image, timeout=timeout)` 把 URL/路径变成 PIL 图像，再 `self.image_processor(images=image, return_tensors="pt")` 得到 `pixel_values`，并 `.to(self.dtype)` 对齐精度。
- 推理（[:190-192](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/image_classification.py#L190-L192)）：`self.model(**model_inputs)`，与文本分类几乎一字不差。
- 后处理（[:194-230](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/image_classification.py#L194-L230)）：同样 softmax/sigmoid + `id2label`，只是默认 `top_k=5`，并对 fp16/bf16 做了升精度处理。

注意两个类各自的 `_load_*` 标志（文本：[:75-78](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/text_classification.py#L75-L78)；图像：[:98-101](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/image_classification.py#L98-L101)）正好决定了工厂会为它们加载 tokenizer 还是 image_processor。

> 这也呼应了 u1-l1 的核心理念：**每种输入模态都有一个 Preprocessing 对象**（文本→tokenizer、图像→image_processor），pipeline 用 `_load_*` 标志把它们对称地接入同一条骨架。

#### 4.3.4 代码实践

**实践目标**：跑通图像分类，打印 image_processor 的中间张量与最终后处理结果，与 4.2.4 的文本流水线形成对照。

**操作步骤**（图像分类需要 `torchvision`/PIL，可用 `pip install "transformers[vision]"` 补齐）：

1. 新建 `pipe_image.py`：

   ```python
   from transformers import pipeline

   pipe = pipeline("image-classification",
                   model="google/vit-base-patch16-224")
   url = "https://huggingface.co/datasets/Narsil/image_dummy/raw/main/parrots.png"

   # ① 预处理：看 image_processor 把图片变成了什么形状
   inputs = pipe.preprocess(url)
   print("【预处理 keys】", list(inputs.keys()))
   print("【pixel_values shape】", inputs["pixel_values"].shape)
   # 形如 torch.Size([1, 3, 224, 224])：batch=1, 通道=3, 高宽=224

   # ② 推理
   outputs = pipe.forward(inputs)
   print("【logits shape】", outputs.logits.shape)

   # ③ 后处理
   print("【Top-3 结果】", pipe.postprocess(outputs, top_k=3))

   # 对照直接调用
   print("【__call__ 结果】", pipe(url, top_k=3))
   ```

2. 运行：`python pipe_image.py`

**需要观察的现象**：
- ① 的 `pixel_values` 形状为 `[1, 3, 224, 224]`——图像被 resize/normalize 成模型期望的尺寸。
- ③ 返回形如 `[{'score': 0.44, 'label': 'macaw'}, ...]` 的列表，与官方 [quicktour](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/quicktour.md) 中 image-classification 示例一致。
- 与 4.2.4 对比：文本任务预处理产物是 `input_ids`，图像任务是 `pixel_values`；但 `_forward` 都是 `model(**inputs)`，后处理都是 logits→softmax→标签。

**预期结果**：打印出 Top-3 鸟类标签及分数。

**如果无法确定运行结果**：联网下载图片或模型可能失败，可改用本地图片路径 `pipe.preprocess("/path/to/cat.jpg")` 验证流程——「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果把图像分类的 `preprocess` 第一步 `load_image` 去掉、直接把 URL 字符串喂给 `image_processor`，会发生什么？

**参考答案**：会出错。`image_processor` 只认 PIL 图像（或 numpy/tensor 数组），不认 URL 字符串。`load_image`（见 [src/transformers/pipelines/image_classification.py:185](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/image_classification.py#L185)）负责「URL/路径 → PIL 图像」这一步归一化。这就是预处理阶段存在的意义之一：把异构的原始输入统一成处理器能吃的形态。

**练习 2**：文本分类与图像分类的 `postprocess` 都用到 `self.model.config.id2label`。这个 `id2label` 从哪来？

**参考答案**：它来自模型的 config（`PretrainedConfig`），是 checkpoint 的 `config.json` 里写好的「类别下标 → 人类可读标签」映射。这也是 u1-l1 强调的「Configuration 描述模型属性」在 pipeline 后处理里的直接体现。深入阅读 config 见 u5-l1。

---

## 5. 综合实践

把本讲的知识串起来，完成一个小任务：**用一条统一的「手动三段式」脚本，同时处理一段文本和一张图片，并对比二者的中间产物**。

要求：

1. 分别创建文本分类 pipeline 与图像分类 pipeline（各指定一个 `model`，避免触发默认下载警告）。
2. 对**两条**流水线都手动执行 `preprocess → forward → postprocess`，分别打印：
   - 预处理产物的 keys 与核心张量的 shape（文本看 `input_ids`，图像看 `pixel_values`）；
   - 模型输出 logits 的 shape；
   - 最终后处理结果。
3. 在脚本最后用一段注释写出你的观察：两条流水线**哪一段几乎相同、哪一段差异最大**，并用自己的话解释原因（提示：对应 4.3.2 的表格）。
4.（可选）给两个 pipeline 传 `device=0`（若有 GPU）或 `device=-1`（CPU），观察基类 `__init__` 里 device 解析（4.2.3 第③点）的效果。

**验收标准**：
- 脚本能分别输出文本与图像的预测结果。
- 注释里能正确指出「预处理差异最大、推理与后处理高度一致」。
- 能在源码中指认 `run_single`（[base.py:1296-1300](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/pipelines/base.py#L1296-L1300)）是这条手动链路的官方实现。

> 这个综合实践直接对应本讲规格里的代码实践任务：用 pipeline 分别跑通 text-generation 与 image-classification，并打印中间 tokenizer/processor 输出与最终后处理结果。若想替换成 `text-generation`，注意它的 `_forward` 走的是 `generate`（4.2.3 第④点），`preprocess` 仍可单独调用以观察 tokenizer 输出，完整生成机制留待 u8。

## 6. 本讲小结

- `pipeline()` 是**工厂函数**，职责是按 `task` 查 `SUPPORTED_TASKS` 注册表，装配出「模型 + 预处理组件 + 具体 Pipeline 类」并返回实例；它本身不做推理。
- 任务有**别名**（如 `sentiment-analysis` → `text-classification`），不传 `model` 时会用表里的默认 checkpoint 并打印警告。
- `Pipeline` 基类把一次推理抽象成**三段式骨架**：`preprocess`（输入→张量）→ `forward`/`_forward`（张量→模型输出）→ `postprocess`（模型输出→友好结果），由 `run_single` 串联。
- 不同任务**预处理不同、推理与后处理高度一致**；预处理组件的加载由各 Pipeline 类的 `_load_*` 标志决定（文本用 tokenizer、图像用 image_processor）。
- 公共 `forward` 统一处理「设备搬运 + `no_grad`」，子类的 `_forward` 只关心热路径推理。
- 文本生成类 pipeline 把 `_pipeline_calls_generate` 设为 `True`，其 `_forward` 走 `model.generate(...)`，因此是自回归多步推理，区别于分类任务的单次 forward。

## 7. 下一步学习建议

- **想搞懂 tokenizer 内部**：阅读 u3（分词器），看 `tokenizer(...)` 到底如何切词、填充、返回 `BatchEncoding`。
- **想搞懂模型加载与 `from_pretrained`**：阅读 u2（Auto 类与 from_pretrained 范式）与 u5（PreTrainedModel 基类），理解 pipeline 第③④步背后的权重与配置加载。
- **想搞懂文本生成的多步推理**：阅读 u8（Generation），重点看 `GenerationMixin.generate()` 主循环、`GenerationConfig` 与解码策略——这会解释本节提到的 `_pipeline_calls_generate` 与 `_default_generation_config`。
- **想自定义一条 pipeline**：直接阅读 `src/transformers/pipelines/base.py` 的 `Pipeline` 抽象方法，并参考 `text_classification.py` 实现四个方法；进阶的自定义流程见 u11-l5（Pipeline 进阶与自定义）。
- **想用更可控的方式做推理**：跳出 pipeline，直接组合 `AutoTokenizer` + `AutoModelForXxx` + `model.generate()`，这正是 quicktour 里「Pretrained models」一节展示的做法（[quicktour.md](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/quicktour.md)）。

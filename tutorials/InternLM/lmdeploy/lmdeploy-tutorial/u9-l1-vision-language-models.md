# 视觉语言模型 VLM 处理

## 1. 本讲目标

本讲讲解 lmdeploy 中**视觉语言模型（Vision-Language Model, VLM）**的完整处理链路。学完后你应当能够：

- 说清一条「图片 + 文字」请求从用户输入到 GPU forward 之间，到底经过了哪几个组件、各自做了什么。
- 区分 `vl/`（视觉编码基础设施）与引擎（PyTorch / TurboMind）的职责边界：图像在哪里被预处理、在哪里被编码成 embedding、又是如何被「塞进」语言模型的输入里的。
- 看懂 `VisionModel` 基类提供的两套 `preprocess`（新式 / 旧式）与两套后端包装（`to_pytorch` / `to_turbomind`）的差异。
- 理解引擎侧的统一多模态数据载体 `MultiModalData`，以及 PyTorch 模型在 `forward` 中如何用 `masked_scatter` 把图像 embedding 替换到占位 token 上。

本讲依赖 u4-l3（EngineInstance 与流式推理）。强烈建议先建立「AsyncEngine 是引擎的异步外壳」的认知，再来看 VLM 如何在这层外壳之上挂载一个独立的视觉编码器。

## 2. 前置知识

阅读本讲前，先用通俗语言建立几个直觉：

**1）VLM 是「两个模型」粘在一起。** 一个 VLM（如 Qwen3-VL、InternVL、LLaVA）内部其实有两部分：一个**视觉编码器**（Vision Encoder，把图片变成一串向量）和一个**语言模型**（把图像向量和文字 token 向量拼到一起，继续做自回归生成）。lmdeploy 的核心是把语言模型推理做到极致（见 U3–U5），而视觉编码器相对独立、变化更快，于是被单独抽到 `lmdeploy/vl/` 子包里，作为「与后端无关的公共基础设施」。

**2）「占位 token」思想。** 语言模型只认 token。但一张图片对应几百到几千个连续向量，没法直接用 tokenizer 编出来。通行做法是：在 prompt 里插入一长串**占位 token**（如 Qwen 系的 `<|image_pad|>`），先让语言模型给这些占位位置算出默认 embedding，**然后在 forward 内部把这些位置的 embedding 替换成真正的图像 embedding**。本讲你会看到 lmdeploy 如何在引擎侧完成这次「替换」。

**3）预处理（preprocess）≠ 前向（forward）。** 这是本讲最容易混淆的一点：

- **preprocess**：把图片读进来、resize、归一化、切成 patch，产出 `pixel_values` 张量 + 一堆形状信息（如 `image_grid_thw`），并算出占位 token 应该重复多少次。这一步**不碰 GPU 上的视觉编码器权重**。
- **forward**（视觉编码器的前向）：把 `pixel_values` 喂进视觉编码器，真正算出图像 embedding。

本讲的一个核心结论是：**这两个步骤的归属，在 PyTorch 与 TurboMind 后端下不一样**——这正是理解 VLM 全链路的钥匙。

**4）多模态（multimodal）= 图片 + 视频 + 音频 + 时序。** lmdeploy 的 VLM 框架不仅支持图片，`Modality` 枚举里有 `IMAGE / VIDEO / AUDIO / TIME_SERIES` 四种模态。本讲以图像为主线讲解，其他模态走同一套管线。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [lmdeploy/vl/engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/engine.py) | `ImageEncoder`：把 `VisionModel` 包成异步可调用的视觉编码器，提供 `preprocess / async_infer / wrap_for_pytorch / wrap_for_turbomind` 四个阶段。 |
| [lmdeploy/vl/model/base.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/base.py) | `VisionModel` 抽象基类：定义 preprocess/forward/to_pytorch/to_turbomind 协议，并内置「新式 preprocess」与两种后端包装的默认实现。 |
| [lmdeploy/vl/model/qwen3.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/qwen3.py) | `Qwen3VLModel`：Qwen3-VL 的预处理实现，是「新式 preprocess」的典型样本。 |
| [lmdeploy/vl/model/qwen2.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/qwen2.py) | `Qwen2VLModel`：Qwen2-VL 的「旧式 preprocess」样本，对比阅读帮助理解新旧两套机制。 |
| [lmdeploy/vl/model/builder.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/builder.py) | `load_vl_model`：按 HF 配置的 arch 名匹配并实例化 `VisionModel` 子类。 |
| [lmdeploy/pytorch/multimodal/data_type.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/multimodal/data_type.py) | `MultiModalData`：引擎内部统一的多模态数据载体（含范围、元数据、内容哈希）。 |
| [lmdeploy/pytorch/models/qwen3_vl.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/qwen3_vl.py) | PyTorch 后端里被 patch 进来的 Qwen3-VL 重写类：`forward` 与 `prepare_inputs_for_generation` 展示引擎侧如何消费多模态输入。 |
| [lmdeploy/serve/processors/multimodal.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/processors/multimodal.py) | `MultimodalProcessor`：服务层的提示词处理器，串起「加载图片 → vl 编码 → 引擎输入」全过程。 |
| [lmdeploy/serve/core/vl_async_engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/vl_async_engine.py) | `VLAsyncEngine`：在 `AsyncEngine` 之上挂载 `ImageEncoder`，是 VLM 的异步引擎外壳。 |

## 4. 核心概念与源码讲解

### 4.1 vl engine：ImageEncoder 与 VLAsyncEngine 的分工

#### 4.1.1 概念说明

回忆 u4-l3 与 u8-l3：`AsyncEngine` 是「服务层 ↔ 引擎层」之间的后端无关异步封装。对纯文本模型，`AsyncEngine` 把 prompt tokenize 后丢给引擎即可。但 VLM 多了一件事：**在 tokenize 之前，要先把图片编码成引擎能吃的格式**。这件事不能塞进引擎主循环（会阻塞 GPU 推理），于是 lmdeploy 用一个独立的组件 `ImageEncoder` 来做，再用 `VLAsyncEngine` 把它和 `AsyncEngine` 组合起来。

这里有个命名容易误导的地方：**`ImageEncoder` 这个「Encoder」其实是「视觉预处理 + 视觉前向的总调度器」**，它本身不实现任何编码数学，真正的视觉编码由它持有的 `VisionModel` 子类完成（详见 4.2）。

#### 4.1.2 核心流程

一条多模态请求在 vl engine 这一层的处理分为四个阶段，对应 `ImageEncoder` 的四个方法：

```text
messages(含图片)
   │
   │ ① preprocess()    读图、resize、归一化、切 patch、算占位 token 数
   ▼
pixel_values + grid_thw + input_ids(含占位 token)
   │
   │ ② async_infer()   （仅 TurboMind 旧式需要）把 pixel_values 喂进视觉编码器，
   ▼                     真正算出 image embeddings
image embeddings（TurboMind 旧式）/ pixel_values（PyTorch、新式）
   │
   │ ③ wrap_for_pytorch()  或  ④ wrap_for_turbomind()
   ▼
打包成引擎 forward 所需的 dict（prompt / input_ids / multimodal 或 input_embeddings+ranges）
```

注意 ② `async_infer`（调用 `VisionModel.forward`）**只有 TurboMind 旧式模型才需要**；PyTorch 后端与新式模型会把视觉前向推迟到引擎的 `forward` 内部完成（见 4.3）。这正是 `ImageEncoder` 要同时提供 `wrap_for_pytorch` 与 `wrap_for_turbomind` 两个分支的原因。

#### 4.1.3 源码精读

`ImageEncoder` 在构造时加载视觉模型，并解析多模态特征的数据类型：

- [lmdeploy/vl/engine.py:73-98](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/engine.py#L73-L98)：`ImageEncoder.__init__`。它调用 `load_vl_model` 拿到一个 `VisionModel` 实例，解析 `mm_feature_dtype`（图像特征要用 fp16 还是 bf16），并用一个 **`ThreadPoolExecutor(max_workers=1)`** 来跑视觉任务——`max_workers=1` 是关键：视觉编码被串行化，避免与引擎主循环争抢 GPU、也保证预处理顺序确定。

- [lmdeploy/vl/engine.py:100-107](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/engine.py#L100-L107)：`_is_new_preprocess_api`。它用 `inspect.signature` 探测 `model.preprocess` 的形参，若同时含 `input_prompt` 与 `mm_processor_kwargs` 则判为「新式」预处理。这个布尔标志 `_uses_new_preprocess` 会在后端包装分支里反复出现，是判断走哪条管线的关键开关。

- [lmdeploy/vl/engine.py:109-122](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/engine.py#L109-L122)：`preprocess`。这是个 `async` 方法，但内部用 `run_in_executor` 把**同步的** `model.preprocess` 丢到线程池里跑，再用 `await future` 等结果——这是「异步外壳 + 同步实现」的典型桥接，避免阻塞 asyncio 事件循环（见 u4-l2 的 EngineLoop）。

- [lmdeploy/vl/engine.py:137-178](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/engine.py#L137-L178)：`wrap_for_pytorch`。它的产物结构在 docstring 里写得很清楚——一个 list，每项含 `prompt`、`input_ids` 与一个 `multimodal` 字典（内含 `pixel_values` 等**原始张量**，不是 embedding）。也就是说，**PyTorch 路径把「算 embedding」推迟到引擎 forward 里**。

- [lmdeploy/vl/engine.py:180-217](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/engine.py#L180-L217)：`wrap_for_turbomind`。产物含 `input_embeddings`（已经算好的图像 embedding 列表）与 `input_embedding_ranges`（每个 embedding 在 input_ids 中的 `[begin, end)` 区间）。也就是说，**TurboMind 旧式路径在 Python 侧就把 embedding 算好**，引擎只负责按区间替换。两个 `wrap_for_*` 方法的产物结构差异，是 4.3 节「两种注入方式」的根源。

`VLAsyncEngine` 把这个 `ImageEncoder` 挂到 `AsyncEngine` 上：

- [lmdeploy/serve/core/vl_async_engine.py:12-58](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/vl_async_engine.py#L12-L58)：构造期创建 `self.vl_encoder = ImageEncoder(...)`，再把提示词处理器换成 `MultimodalProcessor`（注入 `vl_encoder`）。注意它**没有重写 `generate()`**（见 u8-l3 的依赖注入结论），多模态能力完全靠换掉 `prompt_processor` 获得。这里还有一个有意思的细节：当 `enable_prefix_caching=True` 时，只有「PyTorch 后端 + 新式 preprocess」才真正支持多模态前缀缓存，否则会被强制关闭并打印警告——因为只有新式 preprocess 才能产出稳定的内容哈希（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：在不实际加载大模型的前提下，确认 `ImageEncoder` 的四个阶段方法都存在，并理解它们的产物结构差异。

**操作步骤**：

1. 打开 [lmdeploy/vl/engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/engine.py)，定位 `ImageEncoder` 类（第 73 行）。
2. 在类体内依次找到 `preprocess`、`async_infer`、`wrap_for_pytorch`、`wrap_for_turbomind` 四个方法。
3. 对比 `wrap_for_pytorch`（第 137 行）与 `wrap_for_turbomind`（第 180 行）的 docstring 中给出的产物结构。

**需要观察的现象**：

- `wrap_for_pytorch` 的产物里有 `multimodal` 字段（装的是 `pixel_values` 等原始张量），**没有** `input_embeddings`。
- `wrap_for_turbomind` 的产物里有 `input_embeddings` 与 `input_embedding_ranges`，**没有** `multimodal` 字段。

**预期结果**：你能用一句话总结：「PyTorch 让引擎自己算 embedding，TurboMind 旧式在 Python 侧算好 embedding 再按区间交给引擎」。这就是本讲最核心的一条结论之一。

**待本地验证**：若你本地有 GPU 且已下载某个 Qwen3-VL 权重，可额外尝试 `from lmdeploy.serve.core.vl_async_engine import VLAsyncEngine`，打印 `engine.vl_encoder._uses_new_preprocess` 的值（应为 `True`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ImageEncoder` 的线程池要设 `max_workers=1`？改成更大的值会有什么风险？

> **参考答案**：视觉编码是 GPU 密集任务，多个视觉任务并发跑会与引擎主循环的 LLM forward 抢占同一块 GPU，导致推理延迟抖动、显存峰值不可控。`max_workers=1` 把视觉任务串行化，保证「一次只编码一个请求的图片」，让 GPU 资源在「视觉编码」与「LLM 推理」之间有确定性的分配。同时串行化也保证预处理顺序与请求顺序一致。

**练习 2**：`VLAsyncEngine` 为什么不重写父类 `AsyncEngine.generate()`？

> **参考答案**：因为多模态处理的差异只发生在「tokenize 之前的提示词准备阶段」。`VLAsyncEngine` 通过把 `prompt_processor` 换成 `MultimodalProcessor`、并注入 `vl_encoder`，就让 `generate()` 复用父类的「请求转发 → 句柄流式」逻辑（见 u8-l3 的 `generate → safe_run → handle.async_stream_infer`）。这是依赖注入优于继承的典型用法。

---

### 4.2 vl/model 预处理：VisionModel 基类与 Qwen3VLModel

#### 4.2.1 概念说明

`ImageEncoder` 只是个调度器，真正的「怎么读图、怎么切 patch、占位 token 用哪个」全由它持有的 `VisionModel` 子类决定。lmdeploy 在 [lmdeploy/vl/model/](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/) 下为 20 多个模型族各写了一个子类（`internvl.py`、`qwen2.py`、`qwen3.py`、`llava.py`、`gemma3_vl.py` …）。

这些子类通过 mmengine 的 `VISION_MODELS` 注册表登记，并用类属性 `_arch`（HF `config.json` 里 `architectures` 字段的值）声明自己支持哪个模型。加载时 `load_vl_model` 遍历注册表，用 `match(hf_config)` 比对 arch 名来选中实现，这与 u2-l5 讲的「arch 名是模型身份证」、u3-l3 讲的 PyTorch patch 查表是同一套思路。

预处理存在**新旧两套 API**，这是历史演进的结果：

- **旧式**（qwen2/internvl/llava 等）：子类自己重写 `def preprocess(self, messages)`，每个模型各写一套「读图 → 切 patch → 算占位 token 数」的逻辑，高度定制、互相重复。
- **新式**（qwen3/gemma3_vl/glm4_1v 等）：子类**只负责建 HF 的 `AutoProcessor`**，`preprocess` 的主体逻辑由基类 `VisionModel.preprocess` 统一实现——直接调用 HF processor，再把产物按模态分类打包。这大幅减少了重复代码，也是支持前缀缓存的前提。

#### 4.2.2 核心流程

以新式 `Qwen3VLModel` 为例，预处理在基类里的执行过程：

```text
messages(含图片 item)
   │
   │ collect_multimodal_items()   按模态分桶，收集 (modality, data, params)
   ▼
raw_images / raw_videos / ...
   │
   │ self.processor(text, images=..., return_tensors='pt')   调 HF processor
   ▼
processor_outputs: input_ids, pixel_values, image_grid_thw, ...
   │
   │ 按 ATTR_NAME_TO_MODALITY 把每个输出属性归到对应模态；FEATURE_NAMES 里的
   │ 主特征张量被改名为 'feature' 并按 mm_feature_dtype 转精度
   ▼
collected_mm_items: {IMAGE: {feature: pixel_values, image_grid_thw: ...}, ...}
   │
   │ get_expanded_mm_items()  按 token 展开成逐图条目，并算好每张图在 input_ids 中的 offset
   ▼
result = {input_ids: [...], multimodal: [每张图的 {feature, image_grid_thw, offset}, ...]}
```

随后这个 `result` 会被 `wrap_for_pytorch`（PyTorch）或 `forward + wrap_for_turbomind`（TurboMind 旧式）进一步打包。

#### 4.2.3 源码精读

先看新式样本 `Qwen3VLModel`，它非常短，因为脏活都交给基类了：

- [lmdeploy/vl/model/qwen3.py:19-47](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/qwen3.py#L19-L47)：`Qwen3VLModel`。`_arch` 声明支持 `Qwen3VLForConditionalGeneration` 与 `Qwen3VLMoeForConditionalGeneration` 两个 arch（普通版与 MoE 版共用一个视觉处理器）。`build_preprocessor` 用 `AutoProcessor.from_pretrained` 建 HF processor，取出图像/视频的占位 token（`image_token`/`video_token`）及其 id，组装成 `MultimodalSpecialTokens`。注意它**没有重写 `preprocess`**——直接复用基类的统一实现。

再看基类 `VisionModel` 的关键方法：

- [lmdeploy/vl/model/base.py:29-53](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/base.py#L29-L53)：两张映射表是「按模态分类」的核心词汇。`ATTR_NAME_TO_MODALITY` 把 HF processor 输出的属性名（如 `pixel_values`、`image_grid_thw`、`pixel_values_videos`、`input_features`）映射到模态枚举；`FEATURE_NAMES` 列出承载主特征张量的属性（会被改名成统一的 `feature`）。这两张表让基类能通吃图/视频/音频/时序四种模态。

- [lmdeploy/vl/model/base.py:123-238](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/base.py#L123-L238)：基类的 `preprocess`。第 133 行 `collect_multimodal_items` 把消息里的多模态 item 按 `(modality, data, params)` 三元组收集；第 158-198 行按模态把原始数据填进 HF processor 的 kwargs；第 201 行真正调 `self.processor(...)` 拿到 HF 输出；第 209-235 行把输出按模态归类、算 offset、展开成逐条目，最后产出 `dict(input_ids=..., multimodal=...)`。这是一段「数据搬运 + 分桶」的编排代码，不含任何视觉数学。

- [lmdeploy/vl/model/base.py:334-361](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/base.py#L334-L361)：`collect_multimodal_items`。遍历消息，把每个 `type` 不是 `text` 的 item 收集起来。这是「messages 里的图片 → 可处理数据」的入口。

- [lmdeploy/vl/model/base.py:286-299](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/base.py#L286-L299)：`forward`。基类默认实现会在 `backend == 'turbomind'` 时抛 `NotImplementedError`，提示子类去实现「真正跑视觉编码器」。注意：**PyTorch 后端根本不会调用这个 `forward`**（它由引擎内的 patched 模型完成），所以新式模型通常只在 TurboMind 旧式路径下才实现它。

- [lmdeploy/vl/model/base.py:421-454](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/base.py#L421-L454)：`to_pytorch_aux`。它把 prompt 按 `<IMAGE_TOKEN>` 占位符切成段，在占位位置插入 `image_token_id`（重复 `image_tokens` 次），并记录每张图的 `offset`。最终产出 `dict(prompt, input_ids, multimodal=preps)`，其中 `preps` 每项含 `pixel_values`、`image_grid_thw`、`offset` 等。

- [lmdeploy/vl/model/base.py:456-490](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/base.py#L456-L490)：`to_turbomind_aux`。与上面类似，但它处理的是**已经算好的图像 embedding**（来自 `forward`），产出 `input_embeddings`（embedding 列表）与 `input_embedding_ranges`（`[begin, end)` 区间）。这就是 TurboMind 旧式路径的产物结构。

- [lmdeploy/vl/model/base.py:492-498](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/base.py#L492-L498)：`match`。比对 `config.architectures[0]` 与子类的 `_arch`，是注册表选中的依据。

- [lmdeploy/vl/model/builder.py:35-84](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/builder.py#L35-L84)：`load_vl_model`。遍历 `VISION_MODELS.module_dict`，用 `module.match(hf_config)` 找到匹配的子类并实例化，再调 `build_preprocessor`。注意第 76-78 行：当后端是 TurboMind 且模型非「原生 C++ 视觉」（`_turbomind_native_vision=False`），或 `with_llm=True`（量化场景）时，才调 `build_model` 真正加载视觉编码器权重；PyTorch 后端不在这里加载视觉权重（交给 patched 模型）。

为对比新旧两套机制，可以读旧式样本：

- [lmdeploy/vl/model/qwen2.py:36-53](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/qwen2.py#L36-L53)：`Qwen2VLModel.preprocess` 自己重写了预处理，用 `qwen_vl_utils.process_vision_info` + `self.processor.image_processor` 逐张处理图片，并把 `image_tokens`（占位 token 数）算出来挂在结果上。这与新式「交给基类 + HF processor 统一处理」形成鲜明对比。
- [lmdeploy/vl/model/qwen2.py:92-123](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/qwen2.py#L92-L123)：`Qwen2VLModel.forward` 在 Python 侧用 `self.model.visual` 真正算 image embedding，并按 `merge_length` 切分。这就是 TurboMind 旧式路径会调用的 `async_infer` 背后的实现。

#### 4.2.4 代码实践

**实践目标**：通过对比 `Qwen3VLModel`（新式）与 `Qwen2VLModel`（旧式），体会新式 API 如何把重复逻辑收敛到基类。

**操作步骤**：

1. 打开 [lmdeploy/vl/model/qwen3.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/qwen3.py)，确认 `Qwen3VLModel` 类体里**只有** `build_preprocessor`，没有 `preprocess` / `forward` / `to_pytorch` / `to_turbomind`。
2. 打开 [lmdeploy/vl/model/qwen2.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/qwen2.py)，确认 `Qwen2VLModel` 重写了 `preprocess`（第 36 行）、`forward`（第 92 行）、`build_model`（第 55 行）、`to_pytorch`（第 186 行）、`to_turbomind`（第 191 行）。
3. 在 [lmdeploy/vl/model/base.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/base.py) 第 29-53 行找到 `ATTR_NAME_TO_MODALITY` 与 `FEATURE_NAMES` 两张表。

**需要观察的现象**：

- 新式 `Qwen3VLModel` 文件只有约 48 行，几乎全是「建 processor + 取 token」；旧式 `Qwen2VLModel` 文件超过 200 行，含大量定制逻辑。
- 两个模型都靠 `_arch` 在注册表里登记，被 `load_vl_model` 选中。

**预期结果**：你能总结出新式 API 的设计动机——「把 per-model 的差异压到最小（只留 build_preprocessor），把通用流程上提到基类」。这也解释了为什么只有新式模型能稳定支持前缀缓存（基类产出的结构可被稳定哈希）。

**待本地验证**：可选地用 `python -c "from lmdeploy.vl.model.base import VISION_MODELS; print(list(VISION_MODELS.module_dict.keys()))"` 打印所有已注册的视觉模型名。

#### 4.2.5 小练习与答案

**练习 1**：`Qwen3VLModel` 没有重写 `preprocess`，那它的图像预处理逻辑来自哪里？

> **参考答案**：来自基类 `VisionModel.preprocess`（base.py 第 123 行）。基类直接调用子类在 `build_preprocessor` 里建好的 `self.processor`（HF `AutoProcessor`）来处理图像，子类只需提供 processor 与占位 token 配置，不必重写 `preprocess` 主体。

**练习 2**：`ATTR_NAME_TO_MODALITY` 与 `FEATURE_NAMES` 这两张表分别解决什么问题？

> **参考答案**：`ATTR_NAME_TO_MODALITY` 把 HF processor 输出的各种属性名（`pixel_values`、`image_grid_thw`、`pixel_values_videos`、`input_features` …）归到 IMAGE/VIDEO/AUDIO/TIME_SERIES 四种模态，让基类能按模态分桶。`FEATURE_NAMES` 标记哪些属性是「主特征张量」（如 `pixel_values`），这些会被统一改名为 `feature` 并按 `mm_feature_dtype` 转精度，从而让下游消费方不必关心具体属性名。两表配合，让基类通吃四种模态。

---

### 4.3 multimodal 输入注入：MultiModalData 与引擎侧消费

#### 4.3.1 概念说明

前两节讲的都是 `vl/` 侧（与后端无关的视觉基础设施）。这一节回答最后一个问题：**这些打包好的多模态数据，是如何「注入」到 PyTorch 引擎的 forward 里的？**

这里要区分两条注入路径，它们对应 4.1 节两个 `wrap_for_*` 的产物结构：

| 路径 | 传入引擎的数据 | 谁来跑视觉编码器 | embedding 如何进入 LLM |
| --- | --- | --- | --- |
| **PyTorch 后端**（新式/旧式） | `pixel_values`（原始图像张量）+ 形状元数据 | 引擎内 patched 模型的 `forward` | `forward` 内 `masked_scatter` 把占位 token 的 embedding 换成图像 embedding |
| **TurboMind 旧式** | `input_embeddings`（已算好）+ `input_embedding_ranges` | Python 侧 `VisionModel.forward` | 引擎按 `[begin, end)` 区间替换 embedding |

引擎内部用一个统一的数据载体 `MultiModalData` 来承载这些信息，无论哪条路径，最终都被翻译成 `StepContext.input_multimodals` 或 `StepContext.input_embeddings`，由 patched 模型的 `prepare_inputs_for_generation` 读取。

> 关于前缀缓存的数学：多模态前缀缓存要求「相同图片 → 相同缓存键」。lmdeploy 对每条 `MultiModalData` 算一个 `content_hash`（对像素张量的字节做 SHA-256）。两张图内容相同即哈希相同，从而命中缓存。这正是 4.1 节「只有 PyTorch + 新式 preprocess 才支持多模态前缀缓存」的原因——只有这条路径能稳定产出可哈希的 `MultiModalData`。

#### 4.3.2 核心流程

以 PyTorch 后端的 Qwen3-VL 为例，从 `wrap_for_pytorch` 产物到 GPU forward 的注入过程：

```text
wrap_for_pytorch 产物：{prompt, input_ids, multimodal:[{feature:pixel_values, image_grid_thw, offset}]}
   │
   │ MultimodalProcessor 把每张图包成 MultiModalData(data=pixel_values, start=offset,
   │   meta={grid_thw,...}, modality=IMAGE, content_hash=...)
   ▼
引擎把 input_multimodals 挂到 SchedulerSequence，经 InputsMaker 翻译进 StepContext
   │
   │ Qwen3VLForConditionalGeneration.prepare_inputs_for_generation 读 context.input_multimodals：
   │   - cat 出 pixel_values
   │   - 算 multimodal_mask（标出哪些位置是占位 token）
   │   - 算 grid_thw / 视觉位置编码 vis_pos_emb
   ▼
forward：
   inputs_embeds = embedding(input_ids)            # 占位 token 先得默认 embedding
   image_embeds  = self.visual(pixel_values, ...)  # 真正跑视觉编码器
   inputs_embeds.masked_scatter(multimodal_mask, image_embeds)  # 替换占位位置
   hidden = self.language_model(inputs_embeds, ...)             # 进 LLM 主干
```

关键一步是 `masked_scatter`：它把 `image_embeds` 的元素按顺序填进 `inputs_embeds` 中 `multimodal_mask` 为 True 的位置——这就是「占位 token embedding 被替换成真实图像 embedding」的落地。

#### 4.3.3 源码精读

先看引擎内部的多模态数据载体：

- [lmdeploy/pytorch/multimodal/data_type.py:56-100](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/multimodal/data_type.py#L56-L100)：`MultiModalData`。字段含义：
  - `data`：主张量（PyTorch 路径下是 `pixel_values`；TurboMind 旧式下是已算好的 embedding），可以是单张量或张量列表（`NestedTensor`）。
  - `start` / `end`：这条多模态数据在 `input_ids` 中的区间（`[start, end)`），告诉引擎 embedding 该填到哪些 token 位置。
  - `meta`：形状与位置元数据（如 `grid_thw`、`mrope_position_ids`）。
  - `modality`：`IMAGE / VIDEO / AUDIO / TIME_SERIES`。
  - `mrope_pos_ids`：Qwen-VL 系特有的多维度旋转位置编码（MRoPE），下文单独说明。
  - `content_hash`：用于前缀缓存的内容哈希。
  - `to_device`：把 `data` 与 `meta` 里的张量整体搬到指定设备。

- [lmdeploy/pytorch/multimodal/data_type.py:46-53](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/multimodal/data_type.py#L46-L53)：`make_multimodal_content_hash`。对 `data`/`meta`/`mrope_pos_ids` 做确定性哈希。注意第 16-43 行的 `_hash_multimodal_value` 会把张量 `detach().cpu().contiguous()` 后按 `view(uint8)` 取原始字节来哈希——所以内容相同即哈希相同，是前缀缓存匹配的依据。

再看引擎 patched 模型如何消费这些数据（以 Qwen3-VL 为例）：

- [lmdeploy/pytorch/models/qwen3_vl.py:530-590](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/qwen3_vl.py#L530-L590)：`prepare_inputs_for_generation`。第 549-564 行从 `context.input_multimodals` 读出每条数据的 `mm_data`，`cat` 出 `pixel_values`，用 `get_multimodal_mask` 算出占位位置掩码，再用 `grid_thw` 算视觉位置编码 `vis_pos_emb` 与累计序列长度 `vis_cu_seqlens`。第 568-574 行是另一条注入路径（`context.input_embeddings`，对应 TurboMind 风格的预算 embedding）：若存在，直接按 `vision_embedding_indexing` 写进 `inputs_embeds`。

- [lmdeploy/pytorch/models/qwen3_vl.py:469-524](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/qwen3_vl.py#L469-L524)：`forward`。第 490 行先用 `get_input_embeddings()(input_ids)` 给所有 token（含占位 token）算默认 embedding；第 492-501 行若有 `pixel_values`，调 `self.visual(...)` 真正跑视觉编码器拿到 `image_embeds`；第 504-511 行按 `grid_thw` 切分 `image_embeds`，并用 `masked_scatter` 把占位位置的 embedding 替换成 `image_embeds`。**这一句 `inputs_embeds.masked_scatter(multimodal_mask, image_embeds)` 就是「图像 embedding 注入 LLM」的最终落地点**。最后第 513 行把替换好的 `inputs_embeds` 喂进 `language_model` 主干。

最后看服务层如何把这一切串起来：

- [lmdeploy/serve/processors/multimodal.py:390-442](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/processors/multimodal.py#L390-L442)：`_get_multimodal_prompt_input`。这是「图片 + 文字 → 引擎输入」的总编排。先 `async_parse_multimodal_item` 把消息里的图片 URL/path 加载成 PIL 图像（见 [lmdeploy/serve/processors/multimodal.py:329](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/processors/multimodal.py#L329) 的 `load_image`），然后按后端分支：TurboMind 走 `preprocess → async_infer → wrap_for_turbomind`（旧式）或 `preprocess`（新式，视觉在 C++ 内）；PyTorch 走 `preprocess → wrap_for_pytorch`（旧式）或 `preprocess`（新式）。

**关于 MRoPE 的补充**：普通语言模型用一维位置编码（token 0,1,2,…），但图像 token 在二维空间上有结构。Qwen-VL 系用 MRoPE（Multi-dimensional Rotary Position Embedding），对时间、高度、宽度三个维度分别编位置。`MultiModalData.mrope_pos_ids` 承载这个三维位置编码，最终经 TurboMind 的 `input_meta`（见 u6-l2）或 PyTorch 的 `mrope_position_ids` 传进 forward（[qwen3_vl.py:476](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/qwen3_vl.py#L476) 形参 `mrope_position_ids`）。其计算可参考 [lmdeploy/vl/model/qwen2.py:161-184](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/qwen2.py#L161-L184) 的 `get_mrope_info`。

#### 4.3.4 代码实践

**实践目标**：定位「图像 embedding 注入 LLM」的最终代码行，并理解 `masked_scatter` 的作用。

**操作步骤**：

1. 打开 [lmdeploy/pytorch/models/qwen3_vl.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/qwen3_vl.py)，定位 `forward` 方法（第 469 行）。
2. 找到第 510 行 `inputs_embeds = inputs_embeds.masked_scatter(multimodal_mask, image_embeds)`。
3. 向上看第 492-506 行：`pixel_values` 如何经 `self.visual(...)` 变成 `image_embeds`，再按 `grid_thw` 切分。
4. 打开 [lmdeploy/pytorch/multimodal/data_type.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/multimodal/data_type.py)，对照 `MultiModalData` 的 `data` / `start` / `end` / `meta` 字段，理解它们如何对应到 `prepare_inputs_for_generation` 里的 `pixel_values`、`offset`、`grid_thw`。

**需要观察的现象**：

- `masked_scatter(mask, source)` 的语义：把 `source` 的元素按 `True` 位置逐个填进 `inputs_embeds`。`mask` 由 `multimodal_mask.unsqueeze(-1).expand_as(inputs_embeds)` 扩展到与 embedding 同形。
- 如果 `pixel_values is None`（纯文本请求），第 492 行的 `if` 分支被跳过，`inputs_embeds` 保持 embedding 层的默认输出，退化为普通文本推理。

**预期结果**：你能用一句话说清「PyTorch 后端把视觉前向后移到 LLM forward 内、用 masked_scatter 替换占位 embedding」这一设计，并解释它为何比 TurboMind 旧式「Python 侧预算 embedding」更高效（少一次 embedding 的 CPU↔GPU 往返、可与 LLM 前向融合）。

**待本地验证**：若你有 GPU 与 Qwen3-VL 权重，可设 `LMDEPLOY_LOG_LEVEL=DEBUG` 跑一次图文推理，观察日志中 `pixel_values` 的 shape 与 `image_grid_thw` 的值，验证占位 token 数 = `grid_thw.prod() // merge_size**2`（merge_size 通常为 2）。

#### 4.3.5 小练习与答案

**练习 1**：`MultiModalData.start` 与 `end` 字段在 PyTorch 后端下主要用于什么？为什么 PyTorch 路径似乎更依赖 `multimodal_mask` 而不是 `[start, end)` 区间？

> **参考答案**：`start`/`end` 描述这条多模态数据在 `input_ids` 中的区间，在 TurboMind 旧式路径下直接用于 `input_embedding_ranges` 的区间替换。PyTorch 路径下，`start`（即 offset）用于在 `prepare_inputs_for_generation` 里构造 `multimodal_mask`（标出占位 token 位置），真正的替换由 `masked_scatter` 按 mask 完成，所以看起来更依赖 mask。本质都是「定位占位位置」，只是表达形式不同（区间 vs 掩码）。

**练习 2**：为什么多模态前缀缓存只支持「PyTorch 后端 + 新式 preprocess」？

> **参考答案**：前缀缓存要求相同输入产生相同缓存键。新式 preprocess 走基类统一逻辑，能把每张图稳定地包成带 `content_hash`（对像素字节做 SHA-256）的 `MultiModalData`；而旧式 preprocess 各模型自定义、且 TurboMind 旧式还要在 Python 侧算 embedding（浮点运算结果不稳定、不可哈希）。只有 PyTorch + 新式这条路径能产出稳定可哈希的键，所以 `VLAsyncEngine` 在不满足时会强制关闭前缀缓存（见 4.1.3 引用的 vl_async_engine.py 第 33-38 行）。

## 5. 综合实践

**任务**：用 `pipeline` 跑一次完整的图文推理，并把本讲学到的三个阶段（vl engine 调度、vl/model 预处理、multimodal 注入）逐一对应到源码行。

**操作步骤**（需要 GPU 与一个已下载的 Qwen3-VL 权重，例如 `Qwen/Qwen3-VL-2B-Instruct`）：

1. 编写脚本：

   ```python
   # 示例代码
   from lmdeploy import pipeline, PytorchEngineConfig, GenerationConfig
   from lmdeploy.vl import load_image

   pipe = pipeline('Qwen/Qwen3-VL-2B-Instruct',
                   backend_config=PytorchEngineConfig(cache_max_entry_count=0.4))
   image = load_image('https://huggingface.co/datasets/huggingface/documentation-images/resolve/main/transformers/tasks/ai2n1.png')
   response = pipe(('describe this image', image))
   print(response.text)
   ```

   > 标注：以上为示例代码，模型名与图片 URL 需按你本地可用资源替换。

2. 在运行前设置 `export LMDEPLOY_LOG_LEVEL=DEBUG`，观察日志中：
   - `matching vision model: Qwen3VLModel`（`load_vl_model` 选中子类，对应 4.2.3）。
   - `preprocess` 阶段产出的 `image_grid_thw` 值。
3. 运行后，对照源码复盘三个阶段：
   - **vl engine**：请求进入 `VLAsyncEngine` → `MultimodalProcessor._get_multimodal_prompt_input`（multimodal.py 第 390 行）→ `ImageEncoder.preprocess`（vl/engine.py 第 109 行）。
   - **vl/model 预处理**：实际执行的是基类 `VisionModel.preprocess`（base.py 第 123 行），因为 `Qwen3VLModel` 未重写它。
   - **multimodal 注入**：`pixel_values` 经 `MultiModalData` 进入引擎，在 `Qwen3VLForConditionalGeneration.forward`（qwen3_vl.py 第 469 行）被 `self.visual` 编码、再被 `masked_scatter`（第 510 行）注入 `inputs_embeds`。

**需要观察的现象与预期结果**：

- 模型能正确描述图片内容，返回一段文本。
- 日志里能看到视觉模型被匹配、预处理被调用、且只发生一次（`max_workers=1` 串行）。
- 你能在源码中为「读图 → 切 patch → 算 embedding → 注入 LLM」每一步指出具体的文件与行号。

**待本地验证**：若本地无 GPU，则改为「源码阅读型实践」——按上述第 3 步的对照表，逐行在源码中定位三个阶段，写下每个文件名:行号与一句话说明，作为本讲的阅读笔记。

## 6. 本讲小结

- VLM = 独立的视觉编码基础设施（`lmdeploy/vl/`）+ 语言模型引擎（PyTorch / TurboMind）。`vl/` 是与后端无关的公共组件，由 `ImageEncoder` 调度。
- `ImageEncoder` 把视觉任务分为 `preprocess`（读图/切 patch/算占位数）、`async_infer`（仅 TurboMind 旧式需要，真正算 embedding）、`wrap_for_pytorch` / `wrap_for_turbomind`（按后端打包）四个阶段，并用 `max_workers=1` 的线程池串行执行以免抢占 GPU。
- `VisionModel` 子类通过 `VISION_MODELS` 注册表 + `_arch` 匹配被选中。存在新旧两套 preprocess：新式（qwen3）只建 HF processor、复用基类统一逻辑；旧式（qwen2）每模型自己重写。
- 两条注入路径：PyTorch 后端传 `pixel_values`，视觉前向在 patched 模型 `forward` 内完成，用 `masked_scatter` 把占位 token 的 embedding 替换成图像 embedding；TurboMind 旧式在 Python 侧预算 `input_embeddings` + `input_embedding_ranges`，引擎按区间替换。
- `MultiModalData` 是引擎内部统一载体，含 `data`/`start`/`end`/`meta`/`modality`/`mrope_pos_ids`/`content_hash`；其中 `content_hash`（对像素字节 SHA-256）是多模态前缀缓存的匹配键，故前缀缓存只支持「PyTorch + 新式 preprocess」。
- MRoPE（多维度旋转位置编码）为图像 token 提供时间/高/宽三维位置，经 `mrope_position_ids`（PyTorch）或 `input_meta`（TurboMind）传入 forward。

## 7. 下一步学习建议

- **u9-l3（Prefix 缓存与 BlockTrie）**：本讲多次提到 `content_hash`，下一步可深入看 `BlockTrie` 如何在 Paged Attention 的块层级上利用这个哈希实现多模态前缀命中与 LRU 驱逐。
- **u9-l4（张量并行与分布式）**：视觉编码器在多卡下的切分（`max_memory`、`device_map='auto'`）与 LLM 的 tp 是两套并行策略，建议对照阅读。
- **新增 VLM 适配**：若你想支持一个新 VLM，重点参考 [lmdeploy/vl/model/qwen3.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/qwen3.py)（新式样本）与基类 [lmdeploy/vl/model/base.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/base.py)，并在 [lmdeploy/vl/model/builder.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/model/builder.py) 的 import 列表里登记；同时还要在 PyTorch 侧写对应的 `models/xxx_vl.py`（参考 [qwen3_vl.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/qwen3_vl.py)）并在 `module_map` 注册（见 u10-l1）。
- **视频/音频/时序模态**：本讲以图像为主线，其他模态走同一套 `ATTR_NAME_TO_MODALITY` 分桶管线，可阅读 [lmdeploy/vl/media/](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/vl/media/) 下的 `video.py`、`audio.py`、`time_series.py` 了解各模态的加载器。

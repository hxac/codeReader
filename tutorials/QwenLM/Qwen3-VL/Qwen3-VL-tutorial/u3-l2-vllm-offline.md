# vLLM 离线推理与 prepare_inputs_for_vllm 模式

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立实现 `prepare_inputs_for_vllm` 函数——这是本仓库中反复出现的一段「样板代码」，出现在 web 演示、README 示例和全部五个评测脚本中。
2. 准确说出 vLLM 离线推理输入的三个字段 `prompt`、`multi_modal_data`、`mm_processor_kwargs` 各自承担什么职责。
3. 掌握视频输入的特殊处理：`return_video_metadata=True` 时返回的 `(视频张量, video_metadata)` 元组列表如何传给 vLLM，以及 `do_sample_frames=False` 这个开关为什么必须传。
4. 会配置 vLLM 的 `LLM` 引擎参数与 `SamplingParams` 采样参数，并用 `llm.generate` 完成一次批量离线推理。

## 2. 前置知识

### 2.1 vLLM 是什么

vLLM 是一个高性能推理引擎。相比直接用 transformers 的 `model.generate`，它通过两項核心技术大幅提升吞吐：

- **PagedAttention**：像操作系统管理内存页一样管理 KV 缓存，避免为每条请求预留整块显存。
- **连续批处理（continuous batching）**：新请求随时插入、完成的请求随时退出，GPU 始终满载。

本仓库 README 明确推荐用 vLLM 部署 Qwen3-VL，并要求 `vllm>=0.11.0`（低于该版本不支持 Qwen3-VL）。

### 2.2 离线推理 vs 在线服务

vLLM 有两种用法，本讲只讲前者：

| 方式 | 入口 | 适用场景 | 对应讲义 |
| --- | --- | --- | --- |
| **离线推理（offline）** | Python 里 `from vllm import LLM`，直接调用 `llm.generate(...)` | 批量跑数据、评测、一次性任务 | 本讲 |
| **在线服务（serving）** | 命令行 `vllm serve ...` 起一个 OpenAI 兼容 HTTP 服务 | 产品化 API 服务 | 下一讲 u3-l3 |

「离线」不是指断网，而是指**不起网络服务、在脚本进程内直接推理**。单元六的全部评测脚本（MMMU、VideoMME、ODinW-13 等）用的都是离线方式。

### 2.3 为什么 vLLM 需要手动准备多模态输入

回顾上一讲（u3-l1）的 transformers 路径：`processor.apply_chat_template(messages, tokenize=True, ...)` 一步就把 messages 变成了带 `pixel_values`、`image_grid_thw` 的张量字典。**processor 帮你做了所有事**。

vLLM 路径则把职责拆开：

- 你的脚本负责：把视觉内容从 messages 里**真正解码出来**（下载图片、解码视频帧、按像素预算缩放）；
- vLLM 引擎负责：接收文本 prompt 和现成的图像/视频张量，在引擎内部调用模型自带的多模态预处理器，把占位符展开成真正的视觉 token，再完成前向与解码。

于是就有了那个问题：谁来「真正解码」？答案就是 `qwen-vl-utils` 的 `process_vision_info`——这正是单元二花了四讲精读的库在此处派上用场。README 中也专门引用了 vLLM 官方多模态输入文档（见 [README.md:856-868](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L856-L868)）。

### 2.4 三个字段的契约预告

`llm.generate` 接收一个字典列表，每个字典描述一条请求：

```text
{
  'prompt':               渲染好的文本提示串（视觉位置只留占位符）
  'multi_modal_data':     {'image': [PIL图...], 'video': [视频...]} 现成的视觉数据
  'mm_processor_kwargs':  传给引擎内部处理器的关键字参数（如 do_sample_frames）
}
```

记住这张「三件套」卡片，下面逐个拆解。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [web_demo_mm.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py) | Gradio 网页演示，其中的 `_prepare_inputs_for_vllm` 是该模式最紧凑的实现，vLLM 双后端加载逻辑也在其中 |
| [README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md) | 「Offline Inference」一节给出完整的可运行 vLLM 示例脚本，是本讲的权威参照 |
| [qwen-vl-utils/src/qwen_vl_utils/vision_process.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py) | `process_vision_info` 的实现，决定了三个返回值（尤其 `video_kwargs`）的确切内容 |
| [evaluation/mmmu/run_mmmu.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py) | MMMU 评测脚本，逐行复用了同一模式，证明它是仓库级的「标准写法」 |

## 4. 核心概念与源码讲解

### 4.1 输入准备函数：prepare_inputs_for_vllm

#### 4.1.1 概念说明

`prepare_inputs_for_vllm` 不是 vLLM 库的 API，而是**本仓库自己约定的一个辅助函数名**：输入标准 messages 和 processor，输出 vLLM `llm.generate` 需要的那条请求字典。它解决的问题是：**把「对话 + 视觉素材」翻译成「文本 prompt + 现成张量 + 处理器开关」三部分**。

这个函数在仓库里出现了至少 7 次，写法几乎逐行相同：

- [web_demo_mm.py:178-198](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L178-L198)（网页演示）
- [README.md:997-1018](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L997-L1018)（官方离线推理示例）
- [evaluation/mmmu/run_mmmu.py:76-107](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L76-L107)（MMMU 评测，函数内注释直接写明 "following the examples in README.md"）

此外 RealWorldQA、MathVision、VideoMME、ODinW-13 四个评测目录也各有一份同名同构实现。学会这一个函数，就看懂了仓库里所有 vLLM 推理代码的入口。

#### 4.1.2 核心流程

函数只有三步：

```text
输入: messages（含 image/video 元素的对话）, processor

第 1 步  渲染文本
    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    # tokenize=False：只产出字符串，视觉位置仅保留一个 <|image_pad|>/<|video_pad|> 占位符

第 2 步  解码视觉素材
    image_inputs, video_inputs, video_kwargs = process_vision_info(
        messages,
        image_patch_size=processor.image_processor.patch_size,   # Qwen3-VL 为 16
        return_video_kwargs=True,                                # 额外返回 video_kwargs
        return_video_metadata=True                               # 视频变为 (张量, metadata) 元组
    )

第 3 步  组装三件套字典并返回
    {'prompt': text, 'multi_modal_data': mm_data, 'mm_processor_kwargs': video_kwargs}
```

注意一个关键细节：第 1 步与第 2 步**遍历的是同一份 messages**。`apply_chat_template` 数出「有几个视觉元素、各在哪个位置」来放置占位符；`process_vision_info` 按 messages 中出现的顺序产出视觉数据。两边顺序天然对齐，vLLM 才能把第 i 个占位符对应到第 i 份视觉数据。

#### 4.1.3 源码精读

先看最紧凑的 web 演示版本：

[web_demo_mm.py:178-198](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L178-L198) —— `_prepare_inputs_for_vllm` 完整实现：

```python
def _prepare_inputs_for_vllm(messages, processor):
    """Prepare inputs for vLLM inference"""
    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    image_inputs, video_inputs, video_kwargs = process_vision_info(
        messages,
        image_patch_size=processor.image_processor.patch_size,
        return_video_kwargs=True,
        return_video_metadata=True
    )

    mm_data = {}
    if image_inputs is not None:
        mm_data['image'] = image_inputs
    if video_inputs is not None:
        mm_data['video'] = video_inputs

    return {
        'prompt': text,
        'multi_modal_data': mm_data,
        'mm_processor_kwargs': video_kwargs
    }
```

逐段说明：

- `tokenize=False`：只渲染出字符串。上一讲（u3-l1）讲过，此时每张图在串里只留一个 `<|image_pad|>` 占位 token；真正的视觉 token 数量由 vLLM 引擎内部按图像实际宽高展开。
- `image_patch_size=processor.image_processor.patch_size`：从 processor 动态读取 patch 边长（Qwen3-VL 为 16）。这是单元二强调过的最佳实践——**不要硬编码 16**，这样同一段代码也能服务 patch 为 14 的 Qwen2.5-VL。
- `return_video_kwargs=True` 与 `return_video_metadata=True`：两个开关同时打开，含义见 4.3 节。
- `mm_data` 只在对应输入非 `None` 时才填键：纯图请求就不会有 `'video'` 键，纯文本请求 `mm_data` 是空字典。

再看 README 的离线示例版本，它多了一行版本提示：

[README.md:1000-1005](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1000-L1005) —— 官方示例的 `process_vision_info` 调用，其上一行注释标注 `qwen_vl_utils 0.0.14+ reqired`（原文如此）：`return_video_metadata` 是 0.0.14 才加入的参数，旧版本会因不认识该关键字而报错。安装命令见 [README.md:864-867](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L864-L867)（`pip install qwen-vl-utils==0.0.14`）。

最后看评测目录的复用，确认「标准写法」的稳定性：

[evaluation/mmmu/run_mmmu.py:87-95](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L87-L95) —— MMMU 评测中的同款调用：先 `apply_chat_template(tokenize=False)`，再以完全相同的三个开关调用 `process_vision_info`。函数结尾同样返回三字段字典（[evaluation/mmmu/run_mmmu.py:103-107](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L103-L107)）。

#### 4.1.4 代码实践

1. **实践目标**：不加载模型，单独验证 `_prepare_inputs_for_vllm` 的三步产出，建立对三个字段的直观认识。
2. **操作步骤**（示例代码，仅需 CPU 与网络）：

```python
# inspect_prepare.py（示例代码）
from transformers import AutoProcessor
from qwen_vl_utils import process_vision_info

ckpt = "Qwen/Qwen3-VL-2B-Instruct"
processor = AutoProcessor.from_pretrained(ckpt)

messages = [{
    "role": "user",
    "content": [
        {"type": "image",
         "image": "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen-VL/assets/demo.jpeg"},
        {"type": "text", "text": "Describe this image."},
    ],
}]

text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
image_inputs, video_inputs, video_kwargs = process_vision_info(
    messages,
    image_patch_size=processor.image_processor.patch_size,
    return_video_kwargs=True,
    return_video_metadata=True,
)
print(repr(text))                 # 观察 prompt 串
print(type(image_inputs), image_inputs[0].size)  # PIL 图像及其缩放后宽高
print(video_inputs, video_kwargs) # 本例应为 None 和 {'do_sample_frames': False}
```

3. **需要观察的现象**：prompt 串中出现 `<|vision_start|><|image_pad|><|vision_end|>`，且 `<|image_pad|>` **只有一个**；图像宽高都是 32 的倍数（像素预算在 utils 内已完成）。
4. **预期结果**：`video_inputs` 为 `None`、`video_kwargs` 为 `{'do_sample_frames': False}`。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `apply_chat_template` 必须传 `tokenize=False`，而不能像 transformers 路径那样传 `tokenize=True`？

**答案**：`tokenize=True` 会直接产出 `input_ids` 和 `pixel_values` 等**已经预处理完**的张量，那是给 `model.generate` 用的；而 vLLM 引擎要求输入是「文本串 + 原始视觉数据」，它要在引擎内部自行完成 tokenization 与视觉展开。若传 `tokenize=True`，产出对 vLLM 无从递交给引擎，且 `tokenize=False` 时每图仅留一个占位符，正适合让 vLLM 按实际视觉 token 数展开。

**练习 2**：如果把 `image_patch_size=14`（Qwen2.5-VL 的值）误传给 Qwen3-VL，会发生什么？

**答案**：不会报错，但 `smart_resize` 会按 patch=14 的对齐因子（28）而非 32 来缩放图像，导致图像宽高不再是 32 的倍数，视觉 token 网格被算错，模型可能表现出细微的性能下降。这就是要从 `processor.image_processor.patch_size` 动态读取、而不是硬编码的原因（见单元二 u2-l1）。

### 4.2 多模态数据字典 multi_modal_data

#### 4.2.1 概念说明

`multi_modal_data` 是 vLLM 定义的多模态输入挂载点：一个字典，键是模态名（`'image'`、`'video'`），值是**已经解码并缩放好的视觉数据列表**。vLLM 收到后，会在引擎内部把它们与 prompt 里的占位符一一对应，展开成真正的视觉 token 送入视觉编码器。

它与 `prompt` 的分工是：**prompt 描述「视觉内容出现在对话的什么位置、有几份」，multi_modal_data 提供「视觉内容本身」**。

图像侧的值是 PIL `Image.Image` 对象列表（`process_vision_info` 的 `fetch_image` 产出），视频侧的值则可能是张量或「张量 + 元数据」元组（见 4.3 节）。

#### 4.2.2 核心流程

一条含 1 图 1 视频的请求，数据流如下：

```text
messages
  ├── apply_chat_template(tokenize=False) ──► prompt 串: "...<|image_pad|>...<|video_pad|>..."
  └── process_vision_info(...)
        ├── image_inputs = [PIL 图]        ──► mm_data['image'] = [PIL 图]
        └── video_inputs = [(张量, 元数据)] ──► mm_data['video'] = [(张量, 元数据)]

vLLM 引擎内部: 按占位符顺序取 mm_data 中的数据 → 视觉编码 → 展开进序列 → 解码生成
```

视觉 token 数量与图像宽高的关系（承接 u3-l1）：图像被 32×32 空间压缩，视频再叠加 2× 时间压缩。对缩放后为 \(H' \times W'\) 的单帧，\(T\) 帧视频的视觉 token 数为

\[
N_{\text{video}} = \frac{H'}{32} \times \frac{W'}{32} \times \frac{T}{2}
\]

这个数量不由你的脚本计算，而由 vLLM 引擎按传入张量的实际尺寸展开——你的脚本只负责把尺寸控制好（元素级 `min_pixels`/`max_pixels` 或 processor 的 `size` 预算）。

#### 4.2.3 源码精读

[web_demo_mm.py:188-192](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L188-L192) —— 组装 `mm_data`：只有 `image_inputs is not None` 时才填 `'image'` 键，视频同理。`process_vision_info` 在没有任何图像元素时会返回 `None` 而非空列表（见 [qwen-vl-utils/src/qwen_vl_utils/vision_process.py:523-526](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L523-L526)），所以这个 `if` 判断既避免把 `None` 塞给 vLLM，也避免无意义的空键。

[README.md:1008-1018](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1008-L1018) —— README 离线示例中的同一段逻辑与最终返回的三件套字典：`mm_data` 的组装方式与 web 演示完全一致，最后返回 `{'prompt', 'multi_modal_data', 'mm_processor_kwargs'}` 三键字典，这就是单条 vLLM 请求的标准形态。

值得对比的是 [README.md:428-436](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L428-L436)：README 在介绍 `qwen-vl-utils` 用法时给出了 Qwen3-VL 系列的标准调用姿势——`process_vision_info(messages, image_patch_size=16, return_video_kwargs=True, return_video_metadata=True)`，并提醒由于 utils 已经完成了缩放，transformers 手动调用 processor 时要传 `do_resize=False` 避免二次缩放。这条提醒针对的是 transformers 路径；vLLM 路径则通过 4.3 节的 `mm_processor_kwargs` 来传达同类信息。

#### 4.2.4 代码实践

1. **实践目标**：观察一条「双图 + 文本」请求的 `mm_data` 结构，并验证多图顺序与 messages 中元素顺序一致。
2. **操作步骤**（示例代码）：在 4.1.4 的脚本基础上，把 messages 改成两张图（可用 `file:///path/to/a.jpg` 与 `file:///path/to/b.jpg`），再打印 `mm_data`。
3. **需要观察的现象**：`mm_data == {'image': [<PIL.Image ...>, <PIL.Image ...>]}`，列表长度为 2，且没有 `'video'` 键；prompt 串中出现两个 `<|vision_start|><|image_pad|><|vision_end|>` 块，先后顺序与 content 中两个 image 元素的顺序一致。
4. **预期结果**：两份 PIL 图像的宽高分别都是 32 的倍数。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mm_data` 要用 `if xxx is not None` 条件填键，而不是直接 `mm_data = {'image': image_inputs, 'video': video_inputs}`？

**答案**：因为 `process_vision_info` 对「不存在的模态」返回 `None`。若把 `None` 或空列表作为 `'image'` 键的值传给 vLLM，引擎会认为这条请求携带图像数据并尝试处理它，轻则报错、重则行为未定义。条件填键保证字典里只出现真实存在的模态。

**练习 2**：一条 messages 里有 1 张图和 1 个视频，`mm_data` 会有几个键？prompt 里会有几个视觉占位块？

**答案**：两个键：`'image'`（1 个 PIL 对象的列表）与 `'video'`（1 个视频条目的列表）；prompt 里有两个视觉占位块——`<|vision_start|><|image_pad|><|vision_end|>` 与 `<|vision_start|><|video_pad|><|vision_end|>`，顺序跟随 content 中元素的出现顺序。

### 4.3 video_metadata 解包与 mm_processor_kwargs 传参

#### 4.3.1 概念说明

这是本讲最微妙也最容易踩坑的模块，涉及三件事：

1. **`return_video_metadata=True` 改变了 `video_inputs` 的形态**。开关关闭时，`video_inputs` 是 `[视频张量, ...]`；开关打开时，每个视频变成 `(视频张量, video_metadata)` **元组**，整个 `video_inputs` 变成元组列表。`video_metadata` 记录了原视频的 `fps`、`frames_indices`（采样帧在原视频中的帧号）、`total_num_frames` 等时间档案（详见 u2-l4）——因为视频张量经过抽帧与缩放后，时间信息全部丢失，必须由 metadata 随帧携带，才能支撑 Qwen3-VL 的「文本-时间戳对齐」。
2. **`mm_processor_kwargs` 的核心内容是 `do_sample_frames=False`**。`qwen-vl-utils` 在库内**已经完成了帧采样**（按 `fps` 或 `nframes` 抽帧），如果引擎内部的处理器再采样一次，帧数就会错一倍。这个开关告诉处理器「帧已采好，别再动」。
3. **vLLM 路径与 transformers 路径的解包方式不同**。vLLM 直接收元组列表；transformers 手动调用 processor 时必须先 `zip(*)` 拆开再分别传参。这是两条路径最大的写法差异。

#### 4.3.2 核心流程

`process_vision_info` 内部如何构造 `video_kwargs`（这是 `mm_processor_kwargs` 的直接来源）：

```text
video_kwargs = {'do_sample_frames': False}          # 恒有：帧已在库内采样完
if not return_video_metadata:                       # 仅 Qwen2.5-VL 兼容分支
    video_kwargs.update({'fps': video_sample_fps_list})
```

于是形成清晰的版本分工：

- **Qwen3-VL**（`return_video_metadata=True`）：`video_kwargs = {'do_sample_frames': False}`，时间信息走 metadata；
- **Qwen2.5-VL 兼容模式**（`return_video_metadata=False`）：`video_kwargs` 额外带 `fps` 列表，时间信息靠 T-RoPE 的 fps 重建。

两条路径的视频传参对比：

```text
vLLM 路径（本讲）:
    mm_data['video'] = [(张量1, 元数据1), (张量2, 元数据2), ...]   # 元组列表整体直传
    mm_processor_kwargs = {'do_sample_frames': False}

transformers 路径（对比, README "Process Videos" 示例）:
    videos, video_metadatas = zip(*videos)   # 手动拆元组
    videos, video_metadatas = list(videos), list(video_metadatas)
    processor(text=text, videos=videos, video_metadata=video_metadatas,
              do_resize=False, **video_kwargs)
```

#### 4.3.3 源码精读

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:528-530](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L528-L530) —— `video_kwargs` 的构造现场：第 528 行先放入 `do_sample_frames: False`；第 529-530 行仅在 `return_video_metadata=False`（Qwen2.5-VL 向后兼容分支）时追加 `fps` 列表。这解释了为什么 Qwen3-VL 模式下打印 `video_kwargs` 只会看到一个键。

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:517-520](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L517-L520) —— 视频元素的解码入口：对每个 video 元素调用 `fetch_video(..., return_video_metadata=return_video_metadata)`，返回值直接 `append` 进 `video_inputs`。当开关为 True 时，`fetch_video` 返回的就是 `(视频输入, 实际采样率)` 二元组、且其中的视频输入本身又是 `(张量, metadata)`——所以 `video_inputs` 最终是元组的列表，这正是 4.2 节 `mm_data['video']` 直传的形态。

[README.md:643-651](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L643-L651) —— transformers 路径的「手动解包」对照组：第 643-645 行用 `zip(*videos)` 把元组列表拆成 `videos` 与 `video_metadatas` 两个列表，第 651 行分别以 `videos=` 与 `video_metadata=` 传给 processor，同时传 `do_resize=False` 和 `**video_kwargs`。**注意 README 在这段代码注释里明确写了 "split the videos and according metadatas"**——这一步在 transformers 路径必须手动做，而 vLLM 路径（web_demo 与离线示例）直接把 `video_inputs` 塞进 `mm_data['video']`，解包由 vLLM 引擎内部完成。

[README.md:965-975](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L965-L975) —— 在线服务场景下的同类机制佐证：官方注释说明默认 `fps=2`、`do_sample_frames=True`（由服务端采样），可通过请求体 `extra_body={"mm_processor_kwargs": {"fps": 2, "do_sample_frames": True}}` 覆盖。可见 `do_sample_frames` 是贯穿离线与在线的同一个处理器开关：**谁来采样，谁就把开关置 False**——离线路径采样发生在 `qwen-vl-utils`，故传 False。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到 `return_video_metadata` 开关对 `video_inputs` 形态与 `video_kwargs` 内容的影响。
2. **操作步骤**（示例代码，仅需 CPU）：

```python
# inspect_video.py（示例代码）
from qwen_vl_utils import process_vision_info

messages = [{
    "role": "user",
    "content": [
        {"type": "video",
         "video": "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen2-VL/space_woaudio.mp4"},
        {"type": "text", "text": "Describe this video."},
    ],
}]

# Qwen3-VL 模式
imgs, vids, kwargs = process_vision_info(
    messages, image_patch_size=16,
    return_video_kwargs=True, return_video_metadata=True)
print(type(vids[0]), len(vids[0]))        # 期望: <class 'tuple'> 2
print(vids[0][1])                          # metadata: fps / frames_indices / ...
print(kwargs)                              # 期望: {'do_sample_frames': False}

# Qwen2.5-VL 兼容模式（对照组）
imgs2, vids2, kwargs2 = process_vision_info(
    messages, image_patch_size=14, return_video_kwargs=True)
print(type(vids2[0]))                      # 期望: torch.Tensor（非元组）
print(kwargs2)                             # 期望: {'do_sample_frames': False, 'fps': [...]}
```

3. **需要观察的现象**：第一次调用中 `vids[0]` 是长度为 2 的 tuple，第二项是含 `fps`、`frames_indices`、`total_num_frames` 等字段的 metadata；第二次调用中 `vids2[0]` 直接是张量，`kwargs2` 多出 `fps` 键。
4. **预期结果**：两种模式的差异与上文注释一致。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：vLLM 路径里，视频的 `(张量, metadata)` 元组需要像 transformers 路径那样先 `zip(*)` 拆开吗？

**答案**：不需要。按官方示例（web_demo_mm.py 与 README 离线示例），`mm_data['video']` 直接接收元组列表，vLLM 引擎内部会处理元数据。`zip(*)` 拆包只发生在 transformers 手动调用 `processor(text=..., videos=..., video_metadata=...)` 的路径（README "Process Videos" 示例），因为那里 `videos` 与 `video_metadata` 是两个独立参数。

**练习 2**：如果忘记把 `video_kwargs`（含 `do_sample_frames=False`）传给 `mm_processor_kwargs`，最可能发生什么？

**答案**：处理器会按默认行为（`do_sample_frames=True`，默认 `fps=2`）对已采样好的帧序列**再抽一次帧**，输入视频的帧数可能减半，时间轴信息随之错乱，模型的视频理解与时间定位会明显劣化，且不一定会抛出异常——属于典型的「静默出错」。

**练习 3**：为什么 Qwen3-VL 模式下 `video_kwargs` 里不再携带 `fps` 列表？

**答案**：因为时间信息已由 `video_metadata`（含原视频 `fps` 与每帧 `frames_indices`）显式携带，时间戳 = 帧号 ÷ fps 可以直接算出；而 Qwen2.5-VL 的 T-RoPE 需要靠 `fps` 列表在位置编码里隐式恢复时间。前者以 `return_video_metadata=True` 为前提，后者是 `return_video_metadata=False` 的兼容分支（见 vision_process.py 第 529-530 行的条件）。

### 4.4 引擎初始化与 SamplingParams

#### 4.4.1 概念说明

准备好请求字典后，还差两块拼图：

- **`LLM(...)`**：离线推理引擎对象。构造时完成权重加载、KV 缓存池分配，关键参数围绕「并行度」与「显存」。
- **`SamplingParams`**：采样参数。决定「贪心还是随机」「最多生成几个 token」等解码行为，与视觉输入无关，但多模态长输入下 `max_tokens` 要给足。

最后 `llm.generate(inputs, sampling_params=...)` 把请求列表整批送入引擎，返回与输入等长的输出列表，每项的 `output.outputs[0].text` 就是生成文本。注意与 transformers 的一个差异：**vLLM 返回的只有新生成部分，不需要像 transformers 那样裁剪输入前缀**。

#### 4.4.2 核心流程

```text
引擎初始化
  os.environ['VLLM_WORKER_MULTIPROC_METHOD'] = 'spawn'   # 多卡 worker 用 spawn 启动
  tensor_parallel_size = torch.cuda.device_count()        # 未指定时默认用满全部 GPU
  llm = LLM(model=..., tensor_parallel_size=..., gpu_memory_utilization=..., seed=0)

批量推理
  inputs = [prepare_inputs_for_vllm(m, processor) for m in all_messages]  # 每条请求一个字典
  outputs = llm.generate(inputs, sampling_params=SamplingParams(...))
  for output in outputs:
      text = output.outputs[0].text        # 直接是新生成文本，无需裁剪
```

其中「张量并行」指把单层权重切到多卡上并行计算，是比数据并行更细粒度的多卡协作方式，适合单卡放不下的大模型。

#### 4.4.3 源码精读

**引擎初始化——web 演示版**：

[web_demo_mm.py:71-88](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L71-L88) —— vLLM 后端的完整加载逻辑：第 71 行设置 `VLLM_WORKER_MULTIPROC_METHOD='spawn'`（避免 fork 方式与 CUDA 初始化冲突）；第 72-74 行在用户未指定 `--tensor-parallel-size` 时默认取 `torch.cuda.device_count()`（用满所有可见 GPU）；第 77-84 行构造 `LLM`，传入 `trust_remote_code=True`、`gpu_memory_utilization`（默认 0.70，即 vLLM 最多可占用该比例显存）、`enforce_eager=False`（允许 CUDA graph 加速）与 `seed=0`（固定随机种子保证可复现）；第 87 行单独加载 `AutoProcessor`——**vLLM 引擎负责生成，processor 在你手里负责准备输入**，这正是 `_prepare_inputs_for_vllm(messages, processor)` 需要 processor 参数的原因。

**引擎初始化——README 离线示例版（MoE 大模型姿势）**：

[README.md:994-995](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L994-L995) —— 脚本开头同样设置 `spawn` 环境变量，说明这是离线推理的标准前奏。

[README.md:1053-1059](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1053-L1059) —— 针对 235B MoE 模型的引擎参数：`mm_encoder_tp_mode="data"`（视觉编码器按数据切分而非按张量切分，降低多卡下的显存与通信开销）、`enable_expert_parallel=True`（MoE 专家并行）、`tensor_parallel_size=torch.cuda.device_count()`。小模型（如 2B）不需要这两个 MoE/编码器参数，只留 `model`、`tensor_parallel_size`、`seed` 即可。

**请求构造与批量推理**：

[README.md:1048-1051](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1048-L1051) —— 批量的本质就是一个列表推导：对每份 messages 各调用一次 `prepare_inputs_for_vllm`，得到请求字典列表。想跑多少条就放多少份 messages。

[README.md:1061-1066](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1061-L1066) —— 官方示例的 `SamplingParams`：`temperature=0`（贪心解码，评测首选，保证可复现）、`max_tokens=1024`、`top_k=-1`（不截断候选集）、`stop_token_ids=[]`（不额外设停止 token）。对照 [README.md:1142-1164](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1142-L1164) 的官方评测超参可知产品级配置更细：Instruct 档用 `temperature=0.7, top_p=0.8, top_k=20, presence_penalty=1.5`，Thinking 档用 `temperature=0.6, top_p=0.95, presence_penalty=0` 且 `out_seq_length=40960`——思考型模型要留足输出长度。

[web_demo_mm.py:206-217](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L206-L217) —— 网页演示中的最小用法：`inputs = _prepare_inputs_for_vllm(messages, processor)` 后直接 `SamplingParams(max_tokens=1024)`，随后 `for output in model.generate(inputs, sampling_params=...)` 遍历输出、取 `completion.text` 累积为回复文本。注意这里传给 `generate` 的是**单个字典而非列表**——vLLM 会自动把单条请求包装成批量，两种写法都合法。

#### 4.4.4 代码实践

1. **实践目标**：对比贪心与采样两种 `SamplingParams` 对同一输入的输出稳定性。
2. **操作步骤**：在综合实践（第 5 节）的脚本中准备两组参数——`SamplingParams(temperature=0, max_tokens=256)` 与 `SamplingParams(temperature=0.7, top_p=0.8, top_k=20, max_tokens=256, seed=1234)`——对同一张图各跑两次推理。
3. **需要观察的现象**：贪心组两次输出完全相同；采样组两次输出通常不同（若设了 seed 则同 seed 下可复现）。
4. **预期结果**：贪心 = 确定性；采样 = 多样性。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：web 演示中 `--tensor-parallel-size` 不传时的默认行为是什么？为什么这个默认值是合理的？

**答案**：默认取 `torch.cuda.device_count()`，即用满当前进程可见的全部 GPU 作为张量并行组（web_demo_mm.py 第 57-60 行定义参数、第 72-74 行实现默认值）。对演示场景而言，单卡能放下就用 1 卡，多卡则自动分摊权重，用户无需关心机器拓扑。

**练习 2**：transformers 推理要用 `out_ids[len(in_ids):]` 裁剪输出，vLLM 需要吗？

**答案**：不需要。transformers 的 `model.generate` 返回「输入 + 新生成」拼接后的完整 id 序列，所以要裁剪前缀；vLLM 的 `output.outputs[0].text` 只包含新生成文本（web_demo_mm.py 第 213-217 行直接取 `completion.text` 使用即是佐证）。

**练习 3**：为什么 235B-A22B 示例要加 `mm_encoder_tp_mode="data"`，而 2B 示例不需要？

**答案**：该参数控制视觉编码器在张量并行下的切分方式：大模型并行度高时，按数据切分（每卡各处理一部分图像）比按张量切分更省显存与通信。2B 模型通常单卡即可放下（`tensor_parallel_size=1`），不存在编码器切分问题，无需该参数。详见 README 部署一节引用的 vLLM 社区指南。

## 5. 综合实践：批量离线推理脚本（2 张图 + 1 个视频）

把本讲四个模块串成一个可运行的脚本：对 2 张图片请求和 1 个视频请求共 3 条输入做**一次批量**离线推理，并正确处理 `do_sample_frames` 与 `video_metadata`。

**实践目标**：独立写出自己的第一个 Qwen3-VL + vLLM 离线推理脚本。

**操作步骤**：

1. 安装依赖（版本要求来自 [README.md:856-868](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L856-L868)）：

```bash
pip install "transformers>=4.57.0" "vllm>=0.11.0" qwen-vl-utils==0.0.14 accelerate
```

2. 编写 `offline_infer.py`（示例代码，参照 [README.md:986-1080](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L986-L1080) 官方示例改写）：

```python
# -*- coding: utf-8 -*-
import os
import torch
from qwen_vl_utils import process_vision_info
from transformers import AutoProcessor
from vllm import LLM, SamplingParams

os.environ['VLLM_WORKER_MULTIPROC_METHOD'] = 'spawn'

IMG1 = "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen-VL/assets/demo.jpeg"
IMG2 = "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen2-VL/assets/demo.jpeg"  # 换成你自己的第二张图
VID  = "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen2-VL/space_woaudio.mp4"


def prepare_inputs_for_vllm(messages, processor):
    """三件套: prompt + multi_modal_data + mm_processor_kwargs"""
    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    image_inputs, video_inputs, video_kwargs = process_vision_info(
        messages,
        image_patch_size=processor.image_processor.patch_size,  # 动态读取, Qwen3-VL 为 16
        return_video_kwargs=True,
        return_video_metadata=True,   # 视频 -> (张量, metadata) 元组列表
    )
    print(f"video_kwargs: {video_kwargs}")   # 预期 {'do_sample_frames': False}

    mm_data = {}
    if image_inputs is not None:
        mm_data['image'] = image_inputs      # PIL 列表直传
    if video_inputs is not None:
        mm_data['video'] = video_inputs      # 元组列表直传, 由 vLLM 解包
    return {
        'prompt': text,
        'multi_modal_data': mm_data,
        'mm_processor_kwargs': video_kwargs, # 防止引擎内二次抽帧
    }


if __name__ == '__main__':
    ckpt = "Qwen/Qwen3-VL-2B-Instruct"
    processor = AutoProcessor.from_pretrained(ckpt)

    all_messages = [
        [  # 请求 1: 单图
            {"role": "user", "content": [
                {"type": "image", "image": IMG1},
                {"type": "text", "text": "Describe this image."},
            ]},
        ],
        [  # 请求 2: 单图
            {"role": "user", "content": [
                {"type": "image", "image": IMG2},
                {"type": "text", "text": "What is in this image?"},
            ]},
        ],
        [  # 请求 3: 视频
            {"role": "user", "content": [
                {"type": "video", "video": VID},
                {"type": "text", "text": "Describe this video."},
            ]},
        ],
    ]

    inputs = [prepare_inputs_for_vllm(m, processor) for m in all_messages]

    llm = LLM(model=ckpt, tensor_parallel_size=min(1, torch.cuda.device_count()), seed=0)
    sampling_params = SamplingParams(temperature=0, max_tokens=512)

    outputs = llm.generate(inputs, sampling_params=sampling_params)
    for i, output in enumerate(outputs):
        print("=" * 40)
        print(f"[{i}] prompt 长度: {len(output.prompt_token_ids)} tokens")
        print(f"[{i}] 生成文本: {output.outputs[0].text}")
```

3. 运行：`python offline_infer.py`。

**需要观察的现象**：

1. 三条请求的 `video_kwargs` 打印——前两条（纯图）与第三条（视频）都应输出 `{'do_sample_frames': False}`；
2. 请求 3 的 `mm_data` 含 `'video'` 键且值为长度 1 的元组列表，元组第二项是含 `fps`、`frames_indices`、`total_num_frames` 的 metadata；
3. `output.outputs[0].text` 直接就是生成文本，无需任何前缀裁剪；
4. vLLM 的连续批处理日志中三条请求在同一次 `generate` 调用内完成调度。

**预期结果**：三段与各图/视频内容相符的描述文本；图像请求的 `mm_data` 只有 `'image'` 键。全流程显存不足以运行 2B 模型时，可改用官方 Docker 镜像（见 [README.md:1167-1173](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1167-L1173)）。**本实践未在编写本讲义时实际运行，待本地验证。**

## 6. 本讲小结

- `prepare_inputs_for_vllm` 是仓库级标准模式，在 web 演示、README 示例与全部五个评测目录中逐行同构地出现，核心是三步：`apply_chat_template(tokenize=False)` 渲染文本 → `process_vision_info` 解码视觉素材 → 组装三件套字典。
- 三字段契约：`prompt` 只含占位符的文本串；`multi_modal_data` 按 `'image'`/`'video'` 键挂载现成 PIL/张量数据，且只在模态真实存在时填键；`mm_processor_kwargs` 传递处理器开关。
- Qwen3-VL 视频必须 `return_video_metadata=True`：视频变为 `(张量, metadata)` 元组列表，vLLM 路径直传由引擎解包，transformers 路径才需手动 `zip(*)`；`video_kwargs` 恒含 `do_sample_frames=False`，防止引擎对已采样的帧二次抽帧。
- 引擎与采样：`LLM(...)` 关注张量并行与显存比例，MoE 大模型加 `mm_encoder_tp_mode="data"` 与专家并行；`SamplingParams` 中 `temperature=0` 是评测贪心档，官方 Instruct/Thinking 产品档超参不同。
- vLLM 输出只含新生成部分，直接取 `output.outputs[0].text`，无需 transformers 式的前缀裁剪。

## 7. 下一步学习建议

- **下一讲 u3-l3（在线服务部署）**：把本讲的离线引擎换成 `vllm serve` 常驻服务，用 OpenAI SDK 发 `image_url`/`video_url` 请求，并理解 `extra_body.mm_processor_kwargs` 与本讲 `mm_processor_kwargs` 的对应关系。
- **单元六（评测体系）**：`evaluation/mmmu/run_mmmu.py` 的 `prepare_inputs_for_vllm`（第 76-107 行）与本讲逐行一致，学完本讲可直接读懂评测主循环，届时重点看它如何按数据集设定 `MIN_PIXELS/MAX_PIXELS` 并批量组装 messages。
- **延伸阅读**：README 部署一节引用的 [vLLM 多模态输入官方文档](https://docs.vllm.ai/en/latest/serving/multimodal_inputs.html)，可进一步了解 `multi_modal_data` 支持的全部模态与格式。

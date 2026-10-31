# 第一次推理：单图、多图与视频

> 本讲属单元一（初识 Qwen3-VL）。前置：u1-l1 项目总览（你已经知道模型家族、仓库四大子项目的分工）。

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立写出 Qwen3-VL 的 `messages` 数据结构，正确使用 `image` / `text` / `video` 三类内容元素。
2. 说清从 `processor.apply_chat_template(...)` 到 `model.generate(...)` 再到 `processor.batch_decode(...)` 的完整推理调用链，以及每一步的输入输出是什么。
3. 理解为什么要用 `out_ids[len(in_ids):]` 裁剪生成结果，并会自己实现这一步。
4. 跑通单图、多图、视频三种输入的推理，并统计每种输入的 token 数。

本讲所有代码实践统一使用最小的 `Qwen/Qwen3-VL-2B-Instruct` 模型，方便在消费级显卡上复现。

## 2. 前置知识

- **多模态大模型（VLM）**：能同时"看图/看视频"和"读文字"的模型。对它来说，一张图不是像素原样进模型，而是先被视觉编码器压缩成一串"视觉 token"，和文字 token 拼在同一条序列里送入语言模型。Qwen3-VL 中一张 H×W 的图约被压缩为 \( \frac{H}{32} \times \frac{W}{32} \) 个视觉 token（32 倍空间压缩）。
- **transformers 的 Auto 类**：`AutoProcessor` 和 `AutoModelForImageTextToText` 会根据模型目录里的配置文件自动选择正确的处理器类和模型类，你不需要手工 import `Qwen3VLForConditionalGeneration` 这样的具体类。
- **chat template（对话模板）**：每个对话模型都定义了自己的提示词格式（角色标记、特殊 token 等）。`apply_chat_template` 负责把你写的结构化 `messages` 渲染成模型实际看到的 token 序列。
- **token id 与解码**：模型的输入输出都是 token id（整数序列），`batch_decode` 把 id 序列转回人类可读文本。
- **本讲与 qwen-vl-utils 的关系**：transformers 路径下，图像/视频的读取和缩放由 processor 在内部完成，**不需要**显式调用 `qwen-vl-utils`；后者是仓库里独立的预处理库，用在 vLLM 等需要"手动传视觉张量"的场景（单元二、三会精读）。本讲只带你看它的公共 API 面，建立"输入预处理在哪里发生"的地图。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md) | 项目门面文档，含 Quickstart 四段官方示例 | 单图/多图/视频/batch 四段推理代码，是本讲的主线 |
| [qwen-vl-utils/src/qwen_vl_utils/\_\_init\_\_.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/__init__.py) | 预处理库的公共出口，只导出 5 个函数 | 确认库的 API 边界，理解它在本讲调用链中的位置 |
| [qwen-vl-utils/src/qwen_vl_utils/vision_process.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py) | 视觉输入预处理实现 | 仅浏览 `extract_vision_info` / `process_vision_info`，看它如何识别 messages 里的视觉元素 |

提醒：模型结构代码（`Qwen3VLForConditionalGeneration` 等）在 transformers ≥ 4.57.0 里，不在本仓库，本讲通过 README 的用法侧源码来理解调用链。

## 4. 核心概念与源码讲解

### 4.1 messages 结构

#### 4.1.1 概念说明

`messages` 是 transformers 生态通用的"对话数据格式"：一个列表，每个元素是一条消息（dict），含 `role`（`system` / `user` / `assistant`）和 `content`。Qwen3-VL 的特殊之处在于 **`content` 是一个列表**，列表里可以混排多种元素：

- `{"type": "text", "text": "..."}` —— 文字
- `{"type": "image", "image": <路径或URL>}` —— 图像
- `{"type": "video", "video": <路径或URL>}` —— 视频

图像和文字的先后顺序就是模型"看到"它们的顺序，可以图在前、文字在后，也可以反过来。

#### 4.1.2 核心流程

```text
messages（Python 结构）
  ├── role: "user"
  └── content: [ {image 元素}, {image 元素}, ..., {text 元素} ]
                        │
                        ▼
processor.apply_chat_template(messages, ...)
  → 把每个视觉元素渲染成一段占位特殊 token（如 <|vision_start|>...<|vision_end|>）
  → 加载并缩放图像/视频为视觉张量（pixel_values / pixel_values_videos / video_grid_thw 等）
  → 输出 BatchEncoding：input_ids + attention_mask + 视觉张量
```

#### 4.1.3 源码精读

官方单图示例的 messages 写法：

```python
messages = [
    {
        "role": "user",
        "content": [
            {
                "type": "image",
                "image": "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen-VL/assets/demo.jpeg",
            },
            {"type": "text", "text": "Describe this image."},
        ],
    }
]
```

这段来自 [README.md:L162-L173](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L162-L173)：一个 user 角色，content 列表里先放一个 image 元素（值为远程 URL），再放一个 text 元素作为问题。

`"image"` 字段的取值支持本地路径、`file://` 前缀路径、http(s) URL 和 data URI 的 base64——多图示例里用的就是 `file:///path/to/image1.jpg` 写法，见 [README.md:L215-L222](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L215-L222)：同一个 content 里放两个 image 元素再加一句 `Identify the similarities between these images.`，模型会把两张图当作一个比较任务来回答。

再看 qwen-vl-utils 这边"如何从 messages 里找出视觉元素"。虽然 transformers 路径不经过它，但这个识别逻辑能帮你理解 messages 的结构约定：

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L483-L498](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L483-L498)：`extract_vision_info` 遍历（可能嵌套的）对话列表，只要某个 content 元素含有 `image` / `image_url` / `video` 键，或 `type` 字段是这三类之一，就收集为视觉信息。这印证了 messages 里视觉元素的两种合法写法：**带 `type` 的规范写法**和**直接以 `image`/`video` 为键的简写**。

[qwen-vl-utils/src/qwen_vl_utils/\_\_init\_\_.py:L1-L7](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/__init__.py#L1-L7)：整个库的公共出口只有 5 个函数——`extract_vision_info`、`fetch_image`、`fetch_video`、`process_vision_info`、`smart_resize`。记住这个 API 面，单元二会逐个精读。

#### 4.1.4 代码实践

1. **实践目标**：直观感受 messages 是纯 Python 数据，不需要模型就能检查。
2. **操作步骤**：把上面单图 messages 存成变量，调用 `qwen_vl_utils.extract_vision_info(messages)`（需 `pip install qwen-vl-utils`），打印返回值。
3. **需要观察的现象**：返回的是一个列表，长度为 1，元素就是那个 image dict。
4. **预期结果**：`[{'type': 'image', 'image': 'https://...demo.jpeg'}]`。再把 `type` 字段删掉只留 `image` 键，重跑一次仍能识别——这正是 L491-L496 那个条件表达式支持的两种写法。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果想让模型先读问题再看图，messages 应该怎么写？
答案：把 text 元素放在 content 列表的第 0 位、image 元素放在后面。content 列表的顺序即模型接收顺序，Qwen3-VL 对"图在前/文在前"都兼容。

**练习 2**：`extract_vision_info` 为什么在 L485-L486 要先判断 `isinstance(conversations[0], dict)`？
答案：该函数同时接受"单轮对话"（列表的元素是 dict，即一条消息）和"批量对话"（列表的元素还是列表）两种输入；判断第一个元素是 dict 还是 list，可以把单轮包装成 `[conversations]`，让后续统一按批量处理。

### 4.2 apply_chat_template：从 messages 到模型输入

#### 4.2.1 概念说明

`processor.apply_chat_template(...)` 是整条链路的"总装车间"。它做三件事：

1. 用模型自带的 Jinja 模板把 messages 渲染成提示串（插入 `<|im_start|>` 等角色标记）；
2. `add_generation_prompt=True` 会在末尾追加 assistant 起始标记，"引导"模型开始回答；
3. `tokenize=True` 时进一步把提示串转成 `input_ids`，同时**加载 messages 里的图像/视频**、缩放并转成视觉张量，与 `input_ids` 一起以张量字典返回。

#### 4.2.2 核心流程

```text
messages
   │ apply_chat_template(tokenize=True, add_generation_prompt=True,
   │                     return_dict=True, return_tensors="pt")
   ▼
BatchEncoding:
   input_ids            # 形状 [1, L]，L = 文本 token + 视觉占位 token
   attention_mask       # [1, L]
   pixel_values         # 图像像素（已按 32 对齐的尺寸缩放）
   image_grid_thw       # 每张图的 (t, h, w) 网格，用于还原 token 排布
   （视频输入时为 pixel_values_videos / video_grid_thw / second_per_grid_ts 等）
   │  inputs.to(model.device)
   ▼
送入 model.generate
```

关键参数含义：

| 参数 | 作用 |
| --- | --- |
| `tokenize=True` | 直接返回 token id 张量（`False` 则只返回渲染后的字符串，调试模板时用） |
| `add_generation_prompt=True` | 追加 assistant 开头标记，是"对话续写"所必需 |
| `return_dict=True` | 返回字典而不是并列参数，方便 `**inputs` 解包 |
| `return_tensors="pt"` | 返回 PyTorch 张量 |

#### 4.2.3 源码精读

[README.md:L176-L183](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L176-L183)：官方推荐的预处理调用，四个参数如上表；随后 `inputs = inputs.to(model.device)` 把所有张量搬到模型所在设备——多卡 `device_map="auto"` 时模型被切到不同卡上，这一步不能省。

[README.md:L145-L160](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L145-L160)：模型与处理器的加载。`AutoModelForImageTextToText.from_pretrained(..., dtype="auto", device_map="auto")` 按仓库配置的精度（通常 bf16）加载；L152-L158 的注释给出加速选项 `attn_implementation="flash_attention_2"`（需额外安装 flash-attn，多图/视频场景收益明显，细节在 u3-l4）。注意 L133 的提示：**Qwen3-VL 要求 transformers ≥ 4.57.0**（[README.md:L132-L135](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L132-L135)），低版本里没有 Qwen3-VL 的模型与处理器实现。

若想看模板渲染的"原始文本"，把同一段 messages 以 `tokenize=False` 再跑一遍，会看到 `<|vision_start|>`、`<|image_pad|>`、`<|vision_end|>` 这类特殊 token 包裹着视觉占位（u3-l1 会专门做这个对照实验）。

#### 4.2.4 代码实践

1. **实践目标**：确认 `tokenize` 两种模式的输出差异。
2. **操作步骤**：对同一份单图 messages 分别执行 `processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)` 与 `tokenize=True, return_dict=True, return_tensors="pt"`。
3. **需要观察的现象**：前者是 `str`，里面能看到 `<|vision_start|>` 与成串的 `<|image_pad|>`；后者是 `BatchEncoding`，`inputs.keys()` 里除了 `input_ids` 还有 `pixel_values`、`image_grid_thw`。
4. **预期结果**：`input_ids` 长度 ≈ 文本 token 数 + `<|image_pad|>` 个数（每 \( \frac{H}{32} \times \frac{W}{32} \) 个对应一个图像 patch）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：不加 `add_generation_prompt=True` 会发生什么？
答案：模板渲染出的串在 user 轮结束后没有 assistant 起始标记，模型可能把输入当作"未完成的对话"继续补 user 内容或直接停住，输出通常不符合"回答问题"的预期。

**练习 2**：为什么官方示例要写 `return_tensors="pt"`？
答案：processor 是多框架设计（还可返回 numpy 等），而下一步 `model.generate(**inputs)` 需要 PyTorch 张量；显式指定避免拿到 list 版本的 token id。

### 4.3 generate 与输出裁剪

#### 4.3.1 概念说明

`model.generate(**inputs, max_new_tokens=128)` 是自回归解码循环：每步预测一个 token，直到生成结束符或达到 `max_new_tokens`。**关键坑点**：返回的 `generated_ids` 是"输入 + 新生成"拼接而成的完整序列，直接 `batch_decode` 会把你的问题也打印一遍。所以需要裁剪：只保留每个样本中"超出输入长度"的那一段。

#### 4.3.2 核心流程

```text
generated_ids        形状 [batch, L_in + L_new]
        │
        │  对每个样本 i：out_ids[len(in_ids):]
        ▼
generated_ids_trimmed  每个样本只保留 L_new 个新生成 token
        │
        │  processor.batch_decode(..., skip_special_tokens=True)
        ▼
output_text           人类可读的回答文本列表
```

#### 4.3.3 源码精读

[README.md:L186-L193](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L186-L193)：推理与裁剪三连。

- L186：`generated_ids = model.generate(**inputs, max_new_tokens=128)`——`**inputs` 把 `input_ids`、`attention_mask`、`pixel_values` 等一次性透传给模型。
- L187-L189：列表推导逐样本裁剪，`zip(inputs.input_ids, generated_ids)` 把"第 i 个样本的输入"与"第 i 个样本的完整输出"配对，`out_ids[len(in_ids):]` 丢掉前缀。
- L190-L193：`processor.batch_decode(generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False)`。`skip_special_tokens=True` 去掉 `<|im_end|>` 等结束标记；`clean_up_tokenization_spaces=False` 保留原文空格，对代码类输出尤其重要。

这个逐样本 zip 的写法天然兼容 batch 推理。batch 时要注意两件事，见 [README.md:L288-L333](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L288-L333)：L292-L293 注释明确要求 `processor.tokenizer.padding_side = 'left'`（生成模型必须左填充，否则右侧的 pad token 会被当成"待续写"的内容）；L311 把 `messages` 组成"列表的列表"，L320 在调用时加 `padding=True`。左填充时每个样本的 `len(in_ids)` 恰好都等于 batch 的最大长度，裁剪逻辑依然正确。batch 细节在 u3-l1 展开。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到"不裁剪会发生什么"。
2. **操作步骤**：跑通单图推理后，把 L190-L193 改成先 `batch_decode(generated_ids, skip_special_tokens=True)` 打印一次，再按官方裁剪写法打印一次，对比两条输出。
3. **需要观察的现象**：未裁剪的输出以 `<|im_start|>user\n...Describe this image.` 开头，把你的问题原样复述了一遍；裁剪后的输出直接是模型的回答。
4. **预期结果**：两条文本前者是后者的超集（前缀多出整个提示词）。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么裁剪要用 `len(in_ids)` 而不是 `len(inputs.input_ids[0]) - something` 之类的固定偏移？
答案：不同样本输入长度不同（batch 内左填充后表面等长，但视觉 token 数、问题长度都随样本变化），逐样本用各自的真实输入长度切片才是通用写法；固定偏移只在对齐过的 batch 里"碰巧"成立。

**练习 2**：`skip_special_tokens=False` 时输出会多出什么？
答案：会保留 `<|im_end|>`（对话结束标记）等特殊 token。调试时保留它们有助于确认模型是否正常停止；面向用户展示时通常设为 `True`。

### 4.4 多图与视频输入

#### 4.4.1 概念说明

多图输入只是"content 里多放几个 image 元素"，没有额外 API。视频输入则是 `{"type": "video", "video": <URL或本地路径>}`：processor 会解码视频、按默认 `fps=2.0` 采样帧、把每帧当图像处理并附加时间信息。视频的视觉 token 数同时受帧数和单帧分辨率约束，预算控制与图像分开配置（这是 u2-l3、u2-l4 的主题）。

#### 4.4.2 核心流程

```text
多图:  content = [image, image, ..., text]      → 每张图独立缩放，占位 token 依次拼接
视频:  content = [video, text]
         → 解码 → 按 fps 采样 n 帧（FRAME_FACTOR=2 对齐）
         → 每帧缩放 → 时间维 + 空间维组成 video_grid_thw
         → 默认 fps=2.0，可在 apply_chat_template 时用 fps= / num_frames= 覆盖
```

#### 4.4.3 源码精读

[README.md:L251-L285](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L251-L285)：视频推理示例。content 里 `type: "video"`，值可以是 URL 或本地路径；其余调用与单图完全一致——这就是好的 API 封装：**输入类型变了，调用链一行不改**。

[README.md:L374-L397](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L374-L397)：视频采样控制。默认 `fps=2`；传 `fps=4` 提高采样密度（帧更多、更细腻、更贵）；或者改用 `num_frames=128, fps=None` 直接锁定帧数。注意注释里的提醒：设置 `num_frames` 时要把 `fps` 覆盖为 `None`，两者是互斥的采样策略。

[README.md:L348-L356](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L348-L356)：processor 侧的像素预算。Qwen3-VL 的 processor 把图像和视频处理拆成两个子处理器：`processor.image_processor.size` 中 `longest_edge`/`shortest_edge` 分别对应单图的最大/最小像素数（除以 \( 32^2 \) 即视觉 token 数上限/下限）；`processor.video_processor.size` 的语义则是**全部帧的总像素预算**（L344-L346 的说明），因视频还有 2 倍时间压缩，换算系数多乘一个 2。

对应地，qwen-vl-utils 里的采样与预算实现（本讲只指路）：[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L31-L34](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L31-L34) 定义了 `FPS=2.0`、`FRAME_FACTOR=2`、`FPS_MIN_FRAMES=4`、`FPS_MAX_FRAMES=768` 这些默认采样常量，与 processor 侧默认值一致；`process_vision_info` 的完整签名与 `image_patch_size`（14 对应 Qwen2.5-VL、16 对应 Qwen3-VL）见 [qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L501-L506](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L501-L506)，其返回 `(image_inputs, video_inputs)` 或带 `video_kwargs` 的三元组（L532-L534）。

#### 4.4.4 代码实践

见下方第 5 节综合实践的 `infer.py`，其中包含视频分支。

#### 4.4.5 小练习与答案

**练习 1**：同一张图分别放进"单图 messages"和"多图 messages"（图 + 同一张图），输入 token 数会怎么变？
答案：大约翻倍（多出一份图像占位 token 与少量模板 token）。视觉 token 数只由每张图自己的分辨率决定，与第几张无关。

**练习 2**：`fps=2` 时一个 10 秒的视频大约采多少帧？`FRAME_FACTOR=2` 起什么作用？
答案：\( 10 \times 2 = 20 \) 帧，再对齐到 2 的倍数（20 已满足）。`FRAME_FACTOR=2` 保证帧数取偶，与视觉编码器 2 倍时间压缩对齐。

**练习 3**：为什么视频的 `longest_edge` 预算要比图像多乘一个 2？
答案：视频 token = 帧数 × 每帧 token，其中帧方向有 2 倍时间压缩（相邻 2 帧合并），所以同样的"目标 token 数"换算成像素时要额外乘 2（见 README L354-L355 的 `16384*32*32*2`）。

## 5. 综合实践

编写一个 `infer.py`，对**单图、两张图、一个视频**三种输入各做一次推理，统一使用 `Qwen/Qwen3-VL-2B-Instruct` 与 `dtype="auto"`，打印每种输入的 token 数与输出文本（即本讲的 `practice_task`）。

下面的完整脚本为**示例代码**（基于 README 四段官方示例改写整合）：

```python
# infer.py  —— 示例代码
import torch
from transformers import AutoModelForImageTextToText, AutoProcessor

MODEL_ID = "Qwen/Qwen3-VL-2B-Instruct"

model = AutoModelForImageTextToText.from_pretrained(
    MODEL_ID, dtype="auto", device_map="auto"
)
processor = AutoProcessor.from_pretrained(MODEL_ID)

def chat(messages, max_new_tokens=128):
    """三步调用链：模板 → 生成 → 裁剪解码"""
    inputs = processor.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
    )
    inputs = inputs.to(model.device)
    n_in = inputs.input_ids.shape[-1]                 # 输入 token 数
    generated_ids = model.generate(**inputs, max_new_tokens=max_new_tokens)
    trimmed = [out[len(inp):] for inp, out in zip(inputs.input_ids, generated_ids)]
    text = processor.batch_decode(
        trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
    )
    return n_in, text[0]

IMG1 = "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen-VL/assets/demo.jpeg"
IMG2 = "file:///path/to/image2.jpg"   # 换成本地第二张图
VIDEO = "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen2-VL/space_woaudio.mp4"

cases = {
    "单图": [{
        "role": "user",
        "content": [
            {"type": "image", "image": IMG1},
            {"type": "text", "text": "Describe this image."},
        ],
    }],
    "两图": [{
        "role": "user",
        "content": [
            {"type": "image", "image": IMG1},
            {"type": "image", "image": IMG2},
            {"type": "text", "text": "Identify the similarities between these images."},
        ],
    }],
    "视频": [{
        "role": "user",
        "content": [
            {"type": "video", "video": VIDEO},
            {"type": "text", "text": "Describe this video."},
        ],
    }],
}

for name, messages in cases.items():
    n_in, out = chat(messages)
    print(f"[{name}] 输入 token 数: {n_in}\n输出: {out}\n{'='*60}")
```

操作步骤与观察要点：

1. **环境**：`pip install "transformers>=4.57.0" torch torchvision`；视频走 processor 内置解码（依赖 torchvision/decord 之一，网络示例视频需要能访问外网）。显存估算：2B 参数 × 2 字节（bf16）≈ 4 GB 权重，加上激活与视觉编码缓存，准备 8 GB 以上较稳妥。
2. **运行**：`python infer.py`，依次观察三个 case 打印的输入 token 数与回答文本。
3. **核对 token 数**：单图 case 的 `n_in` 应显著大于纯文字提问（图像占位 token 占大头）；把 `IMG1` 的实际分辨率代入 \( \frac{H}{32} \times \frac{W}{32} \) 粗算，与 `n_in` 的量级对照（processor 有默认 min/max 像素预算，会先缩放，所以是量级对照而非精确相等）。
4. **对比实验**：把视频 case 的调用改为 `fps=4`（对照 README L377-L385），重跑一次，观察输入 token 数上升——帧数翻倍，视觉 token 近似翻倍。
5. **预期结果**：三种输入均能得到通顺的中文/英文描述；若视频解码失败，优先检查 torchvision 版本或改用本地 mp4 路径。具体数值**待本地验证**（本讲义撰写环境无 GPU，未实际执行）。

## 6. 本讲小结

- Qwen3-VL 的输入统一用 `messages` 描述：`content` 是列表，可混排 `text` / `image` / `video` 三种元素，顺序即模型接收顺序。
- 推理调用链三步走：`processor.apply_chat_template(tokenize=True, add_generation_prompt=True, return_dict=True, return_tensors="pt")` → `inputs.to(model.device)` → `model.generate(**inputs, max_new_tokens=...)`。
- `generate` 返回的是"输入 + 新生成"的完整序列，必须用 `out_ids[len(in_ids):]` 逐样本裁剪后再 `batch_decode(skip_special_tokens=True)`。
- 多图输入只是 content 里多放几个 image 元素；视频输入换成 `type: "video"`，默认 `fps=2` 采样，可用 `fps=` / `num_frames=` 覆盖。
- transformers 路径的视觉预处理由 processor 内部完成；仓库里的 `qwen-vl-utils`（公共 API 仅 5 个函数）是独立的预处理库，主要服务 vLLM 等手动传张量的场景，是单元二的主角。
- batch 推理两个必备设置：`processor.tokenizer.padding_side = 'left'` 与 `apply_chat_template(..., padding=True)`。

## 7. 下一步学习建议

- **下一讲 u1-l4**：把本讲的调用链搬进 Gradio，读 `web_demo_mm.py` 的 hf/vllm 双后端与流式输出，并用 Docker 起官方 demo。
- **单元二**：如果想知道"图像到底被缩放成了什么尺寸、视频到底抽了几帧"，进入 `qwen-vl-utils` 的 `vision_process.py`，精读 `smart_resize`（u2-l2）与 `smart_nframes`（u2-l3）。
- **延伸阅读**：transformers 中 `ProcessingMixin.apply_chat_template` 的官方文档，以及 Qwen3-VL 处理器的 chat template 文件（模型仓库内的 `chat_template.json`），对照本讲 4.2.4 的 `tokenize=False` 实验观察渲染结果。

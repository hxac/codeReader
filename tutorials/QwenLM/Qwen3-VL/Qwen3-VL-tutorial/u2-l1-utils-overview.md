# qwen-vl-utils 总览与 process_vision_info 主流程

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `qwen-vl-utils` 这个库在整个 Qwen3-VL 工程体系中的位置：它是「messages 里的视觉元素」到「PIL 图像列表 / 视频张量」的转换器，主要服务 vLLM 等**需要手动传视觉张量**的场景。
2. 独立跟踪 `process_vision_info` 的完整数据流：`extract_vision_info` 收集视觉元素 → 逐个分发给 `fetch_image` / `fetch_video` → 按开关组装返回值与 `video_kwargs`。
3. 掌握 `image_patch_size` 从 14（Qwen2-VL/2.5-VL）到 16（Qwen3-VL）的代际差异，理解它如何通过 `patch_factor = image_patch_size × 2` 决定输出图像的对齐倍数与视觉 token 数。
4. 理解 `return_video_kwargs` 与 `return_video_metadata` 两个开关的作用，以及它们背后「Qwen2.5-VL 用 fps、Qwen3-VL 用时间戳元数据」的版本兼容策略。

本讲是单元二的第一讲：只讲**总览与主流程**，`smart_resize` 像素预算细节留给下一讲（u2-l2），视频帧采样与解码后端留给 u2-l3，时间戳机制留给 u2-l4。

## 2. 前置知识

### 2.1 messages 结构回顾（承接 u1-l3）

Qwen3-VL 的对话以 `messages` 列表表示，`content` 可以是字符串，也可以是元素列表。视觉信息以独立元素出现在列表中：

```python
{"role": "user", "content": [
    {"type": "image", "image": "file:///path/to/img.jpg"},
    {"type": "video", "video": "file:///path/to/v.mp4"},
    {"type": "text",  "text": "描述一下这两个内容"},
]}
```

`type` 为 `image` / `video` 的元素就是本讲要处理的「视觉元素」。

### 2.2 patch、merge 与视觉 token

视觉编码器（ViT）不是逐像素看图的，而是把图切成小方块：

- **patch（图块）**：ViT 的最小处理单元，边长记为 \(p\)。Qwen2-VL/2.5-VL 的 \(p=14\)，Qwen3-VL 的 \(p=16\)。
- **spatial merge（空间合并）**：ViT 输出后每 \(2 \times 2\) 个 patch 的特征被合并成 1 个送入语言模型的 token（源码中 `SPATIAL_MERGE_SIZE = 2`）。

因此 1 个视觉 token 实际覆盖 \(2p \times 2p\) 的像素区域：Qwen2.5-VL 是 \(28 \times 28\)，Qwen3-VL 是 \(32 \times 32\)。这解释了 u1-l1 提到的「Qwen3-VL 一张图约为 \(\frac{H}{32} \times \frac{W}{32}\) 个视觉 token」。

### 2.3 谁需要 qwen-vl-utils？

u1-l3 已经展示：transformers 路径下 `processor.apply_chat_template(..., tokenize=True)` 会**在 processor 内部**完成视觉预处理，不需要这个库。而 vLLM 离线推理（u1-l4 的 `_prepare_inputs_for_vllm`）、评测脚本（`evaluation/` 目录）都需要先把 messages 变成现成的张量再喂给引擎——这正是 `process_vision_info` 的职责。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [qwen-vl-utils/src/qwen_vl_utils/\_\_init\_\_.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/__init__.py) | 包入口，仅导出 5 个公共函数，是库的公共 API 清单 |
| [qwen-vl-utils/src/qwen_vl_utils/vision_process.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py) | 全部核心逻辑：常量、取整函数、`smart_resize`、`fetch_image`、`fetch_video`、`extract_vision_info`、`process_vision_info` |
| [qwen-vl-utils/README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/README.md) | 官方用法示例，Qwen2VL/2.5VL/3VL 三节的调用差异是本讲「版本兼容」的权威参照 |
| [web_demo_mm.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py) | 仓库内的真实调用方，其 `_prepare_inputs_for_vllm` 展示了生产代码如何传参 |
| [qwen-vl-utils/pyproject.toml](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/pyproject.toml) | 包元信息（版本 0.0.14）与最小依赖清单 |

公共 API 一览（[\_\_init\_\_.py:L1-L7](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/__init__.py#L1-L7) 导出，共 5 个函数）：

| 函数 | 职责 | 本讲地位 |
| --- | --- | --- |
| `process_vision_info` | 主入口，messages → 图像列表 + 视频列表 + kwargs | 4.2 精读 |
| `extract_vision_info` | 从 messages 中收集视觉元素 | 4.1 精读 |
| `fetch_image` | 单个图像元素 → 调整尺寸后的 PIL Image | 仅涉及其接口，u2-l2 精读 |
| `fetch_video` | 单个视频元素 → 张量/帧列表（可附 metadata） | 仅涉及其接口，u2-l3 精读 |
| `smart_resize` | 保持长宽比 + 对齐取整的尺寸计算 | 仅提及其作用，u2-l2 精读 |

## 4. 核心概念与源码讲解

### 4.1 extract_vision_info：从对话中「捞出」视觉元素

#### 4.1.1 概念说明

`process_vision_info` 的第一步是搞清楚「这轮对话里到底有哪些视觉输入、按什么顺序」。`extract_vision_info` 做的就是这件事：遍历 messages，把所有图像/视频元素抽出来， flattening 成一个列表。

它要解决两个问题：

1. **形态多样**：content 里的视觉元素可能带 `"image"`/`"image_url"`/`"video"` 数据键，也可能只有 `"type"` 字段声明类型——两种写法都要识别。
2. **输入形态多样**：调用方可能传单条对话（`List[Dict]`），也可能传一个 batch（`List[List[Dict]]`）——两种都要兼容。

#### 4.1.2 核心流程

```text
输入 conversations
  ├─ conversations[0] 是 dict？ → 视为单条对话，包一层变成 [conversations]
  └─ 否则 → 已是 batch 形态
对每条对话的每条 message：
  ├─ content 不是 list（纯文本）？ → 跳过
  └─ 对 content 中每个元素 ele：
       满足以下任一条件即收集：
         - 含 "image" 键
         - 含 "image_url" 键
         - 含 "video" 键
         - ele.get("type") ∈ {"image", "image_url", "video"}
输出：按对话顺序排列的视觉元素列表
```

注意：输出把图像和视频混在同一个列表里，**不区分类型**；分类是下一步 `process_vision_info` 通过检查元素自身的键完成的。

#### 4.1.3 源码精读

[vision_process.py:L483-L498](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L483-L498) 是完整实现，这段代码从 messages 中筛出所有视觉元素：

```python
def extract_vision_info(conversations):
    vision_infos = []
    if isinstance(conversations[0], dict):      # 单条对话 → 包装成 batch
        conversations = [conversations]
    for conversation in conversations:
        for message in conversation:
            if isinstance(message["content"], list):   # 纯文本 content 直接跳过
                for ele in message["content"]:
                    if (
                        "image" in ele
                        or "image_url" in ele
                        or "video" in ele
                        or ele.get("type", "text") in ("image", "image_url", "video")
                    ):
                        vision_infos.append(ele)
    return vision_infos
```

三个细节值得注意：

- 判断条件是 `or` 连接的四种情况，所以 `{"type": "image", "image": ...}` 和裸的 `{"image": ...}` 都能被收集；`{"type": "text", "text": ...}` 因不含数据键且 type 不在元组中而被排除。
- 只靠 `"type"` 命中、但没有数据键的元素会在下一步分发时触发报错（见 4.2.3 的 `ValueError`），即「收集宽进、使用严出」。
- batch 输入时结果被展平：图像间/视频间的相对顺序保留，但图像与视频的交错顺序信息丢失——这份信息由 chat template 渲染出的 `<|vision_start|>` 占位符序列承载，不在这个列表里。

#### 4.1.4 代码实践

**实践目标**：验证 `extract_vision_info` 对单条对话与 batch 输入、以及不同元素写法的收集行为。

**操作步骤**（示例代码）：

```python
from qwen_vl_utils import extract_vision_info

single = [
    {"role": "user", "content": [
        {"type": "image", "image": "file:///a.jpg"},          # 标准写法
        {"image": "file:///b.jpg"},                            # 简写：只有数据键
        {"type": "video", "video": "file:///c.mp4"},
        {"type": "text", "text": "看看这些"},
    ]},
]
print(len(extract_vision_info(single)))          # 期望 3
print(len(extract_vision_info([single, single])))  # batch：两条对话，期望 6
```

**需要观察的现象**：文本元素被跳过；简写元素同样被收集；batch 输入返回的是展平后的总数。

**预期结果**：依次打印 `3` 和 `6`。（待本地验证）

#### 4.1.5 小练习与答案

**练习 1**：如果一条 message 的 `content` 是纯字符串 `"描述这张图"`，会发生什么？
**答案**：`isinstance(message["content"], list)` 为 False，整条 message 被跳过，不会报错——纯文本轮次天然没有视觉输入。

**练习 2**：为什么判断条件里既查数据键又查 `type` 字段？
**答案**：兼容两种历史写法。老写法（README 的示例）元素带 `type`；新写法/部分调用方只放数据键。查 `type` 是为了「宽进」，但只带 `type` 不带数据的元素会在 `process_vision_info` 分发时抛 `ValueError`，不会静默通过。

---

### 4.2 process_vision_info：主入口的分发与组装

#### 4.2.1 概念说明

`process_vision_info` 是这个库存在的理由：给定 messages，返回模型/推理引擎直接可用的视觉数据。它是一个纯粹的「分发 + 组装」函数，不做任何像素级工作——具体劳动全部委托给 `fetch_image`（单个图像 → 对齐尺寸的 PIL Image）和 `fetch_video`（视频 → 张量或帧列表）。

它的完整签名（[vision_process.py:L501-L506](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L501-L506)）：

```python
def process_vision_info(
    conversations,
    return_video_kwargs: bool = False,
    return_video_metadata: bool = False,
    image_patch_size: int = 14,
):
```

三个开关参数的含义：

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `return_video_kwargs` | `False` | 为 True 时额外返回第三个值 `video_kwargs`（喂给 processor 的关键字参数） |
| `return_video_metadata` | `False` | 为 True 时每个视频的返回值变成 `(video, metadata)` 元组，供 Qwen3-VL 时间戳机制使用 |
| `image_patch_size` | `14` | 视觉 patch 边长，决定输出尺寸的对齐倍数；**Qwen3-VL 必须传 16** |

#### 4.2.2 核心流程

```text
process_vision_info(messages, ...)
  1. vision_infos = extract_vision_info(messages)        # 收集视觉元素
  2. 逐个分发：
       ├─ 含 "image"/"image_url" → fetch_image(ele, image_patch_size=...)
       │     追加到 image_inputs（List[PIL.Image]）
       ├─ 含 "video" → fetch_video(ele, return_video_sample_fps=True,
       │                image_patch_size=..., return_video_metadata=...)
       │     追加到 video_inputs，同时记录 sample_fps
       └─ 都不含 → raise ValueError
  3. 空列表归一化为 None
  4. 组装 video_kwargs：
       始终含 {'do_sample_frames': False}
       若 not return_video_metadata：追加 {'fps': [每个视频的采样帧率]}
  5. 返回 (image_inputs, video_inputs[, video_kwargs])
```

`do_sample_frames=False` 的含义：**帧已经在 `fetch_video` 内部采样完了**，processor 不应再次采样。`fps` 列表则是给 processor 侧使用的版本兼容参数（见 4.4）。

#### 4.2.3 源码精读

主流程在 [vision_process.py:L501-L534](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L501-L534)，这段代码完成收集、分发与返回值组装：

```python
def process_vision_info(conversations, return_video_kwargs=False,
                        return_video_metadata=False, image_patch_size=14):
    vision_infos = extract_vision_info(conversations)   # 第一步：收集
    image_inputs, video_inputs, video_sample_fps_list = [], [], []
    for vision_info in vision_infos:                    # 第二步：分发
        if "image" in vision_info or "image_url" in vision_info:
            image_inputs.append(fetch_image(vision_info, image_patch_size=image_patch_size))
        elif "video" in vision_info:
            video_input, video_sample_fps = fetch_video(vision_info, return_video_sample_fps=True,
                        image_patch_size=image_patch_size, return_video_metadata=return_video_metadata)
            video_sample_fps_list.append(video_sample_fps)
            video_inputs.append(video_input)
        else:
            raise ValueError("image, image_url or video should in content.")
    if len(image_inputs) == 0: image_inputs = None      # 第三步：None 归一化
    if len(video_inputs) == 0: video_inputs = None
    video_kwargs = {'do_sample_frames': False}          # 第四步：kwargs 组装
    if not return_video_metadata:  # BC for qwen2.5vl
        video_kwargs.update({'fps': video_sample_fps_list})
    if return_video_kwargs:
        return image_inputs, video_inputs, video_kwargs # 三元组
    return image_inputs, video_inputs                   # 二元组
```

`image_patch_size` 如何流向下游（以图像为例）：[vision_process.py:L100](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L100)（`fetch_image` 内部）把 patch 边长换算成尺寸对齐因子：

```python
patch_factor = int(image_patch_size * SPATIAL_MERGE_SIZE)   # 14→28，16→32
```

后续 `smart_resize` 保证输出宽高都是 `patch_factor` 的整数倍，于是视觉 token 数为：

\[
N_{\text{token}} = \frac{H}{2p} \times \frac{W}{2p}
\]

例如 Qwen3-VL（\(p=16\)）下 \(1600 \times 992\) 的图恰为 \(50 \times 31 = 1550\) 个视觉 token。

而仓库内真实调用方 web_demo 的写法（[web_demo_mm.py:L181-L186](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L181-L186)），这段代码展示生产环境如何传三个开关：

```python
image_inputs, video_inputs, video_kwargs = process_vision_info(
    messages,
    image_patch_size=processor.image_processor.patch_size,  # 从 processor 读取，而非硬编码
    return_video_kwargs=True,
    return_video_metadata=True
)
```

#### 4.2.4 代码实践

**实践目标**：亲手触发主流程的「分发」与「报错」两条路径，确认返回类型。

**操作步骤**（示例代码，使用 PIL 合成图与帧列表，无需下载任何文件）：

```python
from PIL import Image
from qwen_vl_utils import process_vision_info

img = Image.new("RGB", (1600, 1000), color=(30, 144, 255))
messages = [{"role": "user", "content": [
    {"type": "image", "image": img},
    {"type": "video", "video": [img, img, img]},   # 3 帧列表也合法
    {"type": "text", "text": "hi"},
]}]

images, videos = process_vision_info(messages, image_patch_size=16)
print(type(images), type(images[0]), images[0].size)
print(type(videos), type(videos[0]), videos[0].shape)

bad = [{"role": "user", "content": [{"type": "image", "text": "缺数据键"}]}]
try:
    process_vision_info(bad)
except ValueError as e:
    print("ValueError:", e)
```

**需要观察的现象**：

1. `images[0]` 是 PIL Image，`size`（宽, 高）的宽高都是 32 的倍数。
2. `videos[0]` 是 `torch.Tensor`，形状为 `(T, C, H, W)`；传入 3 帧却得到 `T=4`（帧数被补齐到 `FRAME_FACTOR=2` 的倍数）。
3. 只有 `type` 没有数据键的元素触发 `ValueError: image, image_url or video should in content.`

**预期结果**：`size` 为 `(1600, 992)`（见 4.4.4 的手算推导）；`videos[0].shape` 形如 `(4, 3, 672, 1120)`。（待本地验证）

#### 4.2.5 小练习与答案

**练习 1**：messages 里没有任何视觉元素时返回什么？
**答案**：`(None, None)`（`return_video_kwargs=False` 时是二元组）。两个空列表都被归一化为 `None`，调用方用 `if images is not None:` 判断即可。

**练习 2**：为什么 `process_vision_info` 内部调用 `fetch_video` 时固定传 `return_video_sample_fps=True`？
**答案**：因为无论调用方是否要 metadata，`video_kwargs` 的组装都可能需要每个视频的实际采样帧率（`fps` 列表，见 [L517-L519](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L517-L519)），所以采样帧率必须始终取回。

**练习 3**：`process_vision_info` 自己会修改像素吗？
**答案**：不会。它只做分发与组装；像素级操作（下载/解码、`smart_resize` 缩放）全部在 `fetch_image`/`fetch_video` 内完成。

---

### 4.3 返回值与 kwargs：一份精确的输入输出契约

#### 4.3.1 概念说明

把 `process_vision_info` 当作一个黑盒，它的契约可以完整写成一张表。理解这张表的关键是：**返回形态由两个开关的四种组合决定，而视频元素的形态又额外受 `return_video_metadata` 影响**。

#### 4.3.2 核心流程

```text
return_video_kwargs=False, return_video_metadata=False（默认，Qwen2.5-VL 风格）
  → (images, videos)
    images: List[PIL.Image] | None
    videos: List[Tensor(T,C,H,W)] | None

return_video_kwargs=True, return_video_metadata=False
  → (images, videos, {'do_sample_frames': False, 'fps': [f1, f2, ...]})

return_video_kwargs=True, return_video_metadata=True（Qwen3-VL 风格）
  → (images, videos, {'do_sample_frames': False})
    videos: List[(Tensor, metadata_dict)] | None     ← 每个视频变成二元组
```

`metadata_dict` 含 `fps`（原视频帧率）、`frames_indices`（采样到的帧序号）、`total_num_frames`（总帧数）、`video_backend`（解码后端名）。

#### 4.3.3 源码精读

开关的最终生效点在 `fetch_video` 的返回打包处，[vision_process.py:L477-L480](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L477-L480)，这段代码决定视频以张量还是「张量+元数据」元组返回：

```python
final_video = (video, video_metadata) if return_video_metadata else video
if return_video_sample_fps:
    return final_video, sample_fps
return final_video
```

`video_kwargs` 的组装在 [vision_process.py:L528-L530](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L528-L530)，注意源码注释明确标注了这是为 Qwen2.5-VL 保留的向后兼容分支：

```python
video_kwargs = {'do_sample_frames': False}
if not return_video_metadata:  # BC for qwen2.5vl
    video_kwargs.update({'fps': video_sample_fps_list})
```

拿到元组形态的视频后如何交给 processor？官方示例（[qwen-vl-utils/README.md:L128-L136](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/README.md#L128-L136)）用 `zip(*videos)` 解包：

```python
images, videos, video_kwargs = process_vision_info(
    messages, image_patch_size=16, return_video_kwargs=True, return_video_metadata=True)

if videos is not None:
    videos, video_metadatas = zip(*videos)      # [(v1, m1), (v2, m2)] → (v1, v2), (m1, m2)
    videos, video_metadatas = list(videos), list(video_metadatas)
else:
    video_metadatas = None

inputs = processor(text=text, images=images, videos=videos,
                   video_metadata=video_metadatas, return_tensors="pt",
                   do_resize=False, **video_kwargs)
```

两个配套细节：`video_metadata=` 是 Qwen3-VL processor 的新参数；`do_resize=False` 是因为 `fetch_video` 已经完成缩放，processor 不必重复。

#### 4.3.4 代码实践

**实践目标**：用同一份 messages 对比四种开关组合的返回值形态。

**操作步骤**（示例代码）：

```python
from PIL import Image
from qwen_vl_utils import process_vision_info

img = Image.new("RGB", (640, 480), color=(200, 30, 30))
messages = [{"role": "user", "content": [
    {"type": "image", "image": img},
    {"type": "video", "video": [img] * 4},
    {"type": "text", "text": "hi"},
]}]

r1 = process_vision_info(messages)                                   # 默认
r2 = process_vision_info(messages, return_video_kwargs=True)         # +kwargs
r3 = process_vision_info(messages, return_video_kwargs=True,
                         return_video_metadata=True, image_patch_size=16)
print(len(r1), len(r2), len(r3))
print(r2[2])                    # {'do_sample_frames': False, 'fps': [2.0]}
print(r3[2])                    # {'do_sample_frames': False}
print(type(r3[1][0]))           # tuple：(Tensor, metadata)
print(r3[1][0][1])              # metadata 字典
```

**需要观察的现象**：三元组/二元组长度切换；`fps` 键随 `return_video_metadata` 出现/消失；`r3` 中视频元素变成 tuple，且 `r2[2]['fps'][0]` 恰为帧列表路径默认的 `sample_fps=2.0`。

**预期结果**：依次打印 `2 3 3`、含 `fps: [2.0]` 的字典、只含 `do_sample_frames` 的字典、`tuple`、含 `fps/frames_indices/total_num_frames` 的 metadata。（待本地验证）

#### 4.3.5 小练习与答案

**练习 1**：`return_video_kwargs=True` 但 `return_video_metadata=False` 时，`kwargs` 里的 `fps` 是「原视频帧率」还是「采样帧率」？
**答案**：是**采样帧率**（`sample_fps`），即「本视频平均每秒取了多少帧」，由 `fetch_video` 按 `nframes / total_frames * video_fps` 算出；metadata 里的 `fps` 才是原始视频帧率。两者语义不同。

**练习 2**：为什么 Qwen3-VL 路径的 `video_kwargs` 里没有 `fps` 了？
**答案**：Qwen3-VL 的时间信息改由 `video_metadata`（原帧率 + 帧序号 + 总帧数）承载，processor 用它计算时间戳；`fps` 列表是 Qwen2.5-VL 处理器侧兼容参数，故仅在 `not return_video_metadata` 分支加入。

---

### 4.4 版本兼容策略：一个函数服务三代模型

#### 4.4.1 概念说明

`qwen-vl-utils` 同时服务 Qwen2-VL、Qwen2.5-VL、Qwen3-VL 三代模型，但三代的视觉 patch 尺寸与时间位置机制不同。库的策略是：**函数签名保持不变，用默认值兼容旧代、用开关参数启用新代**：

| 差异点 | Qwen2-VL / 2.5-VL | Qwen3-VL | 兼容手段 |
| --- | --- | --- | --- |
| patch 边长 \(p\) | 14 | 16 | `image_patch_size` 参数，默认 14 |
| 对齐倍数 \(2p\) | 28 | 32 | 同上，流向 `smart_resize` 的 `factor` |
| 视频时间信息 | 处理器用 `fps` 计算时间位置（T-RoPE） | 用 `video_metadata` 构造时间戳 | `return_video_metadata` 开关 + kwargs 分支 |

如果不传 `image_patch_size=16` 就给 Qwen3-VL 处理数据会发生什么？输出的宽高会被对齐到 28 的倍数——与 Qwen3-VL 处理器期望的 32 倍数网格不匹配，轻则视觉 token 数计算错乱，重则张量形状对不上。**这是初学者最常踩的坑。**

#### 4.4.2 核心流程

patch 尺寸影响输出尺寸的传导链：

\[
\text{patch\_factor} = p \times 2 \quad\Rightarrow\quad H' \equiv 0 \pmod{2p},\; W' \equiv 0 \pmod{2p} \quad\Rightarrow\quad N_{\text{token}} = \frac{H'}{2p} \times \frac{W'}{2p}
\]

默认像素预算也随 \(p\) 缩放（[vision_process.py:L26-L27](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L26-L27) 的 `IMAGE_MIN_TOKEN_NUM=4`、`IMAGE_MAX_TOKEN_NUM=16384` 是以 **token 数**定义的）：

\[
\text{min\_pixels} = 4 \times (2p)^2, \qquad \text{max\_pixels} = 16384 \times (2p)^2
\]

\(p=14\) 时约 3136 ~ 12,845,056 像素；\(p=16\) 时 4096 ~ 16,777,216 像素（见 [fetch_image 的 L131-L132](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L131-L132)）。

#### 4.4.3 源码精读

代际差异的「单点收口」在签名默认值：[vision_process.py:L501-L506](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L501-L506) 中 `image_patch_size: int = 14`——默认按最老的 Qwen2-VL 代际取值，保证旧调用方零改动升级库版本。

参数在 `fetch_image` 内换算为对齐因子（[vision_process.py:L100](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L100)，此行把 patch 边长乘上空间合并系数得到 28/32）：

```python
patch_factor = int(image_patch_size * SPATIAL_MERGE_SIZE)
```

生产代码的最佳实践来自 web_demo（[web_demo_mm.py:L183](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L183)）：`image_patch_size=processor.image_processor.patch_size`——**从模型自带的 processor 读取 patch 尺寸，而不是在业务代码里硬编码 14 或 16**，换模型代际时无需改代码。

官方 README 的三代调用对照也印证了这一策略：Qwen2.5-VL 一节用 `process_vision_info(messages, return_video_kwargs=True)`（[README.md:L89-L90](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/README.md#L89-L90)，返回的 kwargs 直接解包给 processor），Qwen3-VL 一节则显式传 `image_patch_size=16, return_video_metadata=True`（[README.md:L128](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/README.md#L128)）。

另两个工程层面的兼容事实：包版本为 0.0.14、运行时依赖仅 `requests/pillow/av/packaging` 四项（[pyproject.toml:L1-L13](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/pyproject.toml#L1-L13)），torch/decord 等重依赖都是可选或运行时探测——这让库可以装在极简环境里，视频解码后端在运行时按 torchcodec → decord → torchvision 顺序自动回退（[vision_process.py:L389-L400](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L389-L400) 的 `get_video_reader_backend`，细节留待 u2-l3）。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：构造包含 1 张图和 1 个视频的 messages，分别以 `image_patch_size=14` 和 `16` 调用 `process_vision_info`，打印两次返回的图像尺寸并解释差异。

**操作步骤**（示例代码，全部用合成数据，离线可跑）：

```python
# patch_compare.py
from PIL import Image
from qwen_vl_utils import process_vision_info

img = Image.new("RGB", (1600, 1000), color=(30, 144, 255))   # 宽1600 × 高1000
messages = [{"role": "user", "content": [
    {"type": "image", "image": img},
    {"type": "video", "video": [img, img, img]},             # 3 帧组成的"视频"
    {"type": "text", "text": "compare patch size"},
]}]

for p in (14, 16):
    images, videos = process_vision_info(messages, image_patch_size=p)
    w, h = images[0].size
    tokens = (h // (2 * p)) * (w // (2 * p))
    print(f"patch={p:>2}  factor={2*p:>2}  image={w}x{h}  "
          f"整除性: w%{2*p}={w%(2*p)}, h%{2*p}={h%(2*p)}  视觉token={tokens}")
    print(f"          video shape={tuple(videos[0].shape)}  "
          f"frame_h%{2*p}={videos[0].shape[2]%(2*p)}, frame_w%{2*p}={videos[0].shape[3]%(2*p)}")
```

**需要观察的现象**：

1. 两次输出的图像宽高都保持了约 16:10 的原始长宽比，但数值不同。
2. `patch=14` 时宽高是 **28 的倍数**；`patch=16` 时宽高是 **32 的倍数**——这是 `patch_factor` 直接决定的。
3. 视频张量的帧高/帧宽同样满足对应倍数；且 `T=4`（3 帧被补到 `FRAME_FACTOR=2` 的倍数）。

**预期结果**（按源码公式手算，输入为 1600×1000 时）：

| `image_patch_size` | 对齐倍数 | 图像输出 (宽×高) | 视觉 token 数 | 视频张量形状 |
| --- | --- | --- | --- | --- |
| 14 | 28 | 1596 × 1008 | 57 × 36 = 2052 | (4, 3, 588, 980) |
| 16 | 32 | 1600 × 992 | 50 × 31 = 1550 | (4, 3, 672, 1120) |

手算过程（图像分支，以 `patch=14` 为例）：`round_by_factor(1000, 28) = round(35.71) × 28 = 1008`，`round_by_factor(1600, 28) = 57 × 28 = 1596`，乘积 1,608,768 落在预算 [3136, 12,845,056] 内，无需二次缩放；`patch=16` 时同理得 992 × 1600。视频分支因单帧预算（`VIDEO_MAX_TOKEN_NUM=768` token/帧）远小于图像预算，触发了按 \(\beta=\sqrt{HW/\text{max\_pixels}}\) 的等比缩小，再向下对齐取整，故帧分辨率更低。（数值待本地验证）

**差异解释**：同一个库、同一份输入，唯一的变量是 `patch_factor`——它同时改变了「对齐网格的粒度」（28 vs 32）与「以 token 数定义的像素预算的绝对值」，因此两代模型拿到的是**尺寸不同但都合规**的输入。这也说明给 Qwen3-VL 传错 patch size 时程序往往不报错、只是悄悄算错 token 网格，必须显式传 16 或从 processor 读取。

#### 4.4.5 小练习与答案

**练习 1**：为什么库作者把默认值设为 14 而不是 16？
**答案**：向后兼容。Qwen2-VL/2.5-VL 时代的存量调用代码不传该参数，默认 14 保证它们升级库后行为不变；新代模型（Qwen3-VL）由调用方显式 opt-in，符合「新功能显式启用」的 API 演进惯例。

**练习 2**：如果不给 Qwen3-VL 传 `image_patch_size=16`，最直接的后果是什么？
**答案**：输出宽高被对齐到 28 的倍数而非 32 的倍数，视觉 token 网格与 Qwen3-VL 处理器的预期不匹配；同时像素预算按 28² 换算也偏小。这类错误不会抛异常，属于「静默出错」，排查时可直接检查输出尺寸能否被 32 整除。

**练习 3**：如何写出对三代模型都正确的调用代码？
**答案**：模仿 [web_demo_mm.py:L183](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L183)，用 `image_patch_size=processor.image_processor.patch_size` 从 processor 动态读取；`return_video_metadata` 也可按模型代际（是否有 video_metadata 支持）决定。

## 5. 综合实践

设计一个「**输入契约探针**」小工具，把本讲的收集、分发、返回值、版本兼容四个模块串起来。

任务：编写 `probe.py`（示例代码），实现函数 `describe(messages, patch_size, need_metadata)`，它调用 `process_vision_info(messages, image_patch_size=patch_size, return_video_kwargs=True, return_video_metadata=need_metadata)`，然后打印：

1. 视觉元素总数（自己先调用 `extract_vision_info` 数一遍，与实际返回的图像/视频数对照）；
2. 每张图像的 `size`、能否被 `2*patch_size` 整除、按公式 \(\frac{H}{2p} \times \frac{W}{2p}\) 估算的视觉 token 数；
3. 每个视频的形状（`need_metadata=True` 时先解包元组）、帧数、`video_kwargs` 的完整内容；
4. 若 `need_metadata=True`，额外打印 metadata 的 `fps`、`total_num_frames`。

然后构建一组 messages（1 张 PIL 合成图 + 1 个 3 帧列表视频 + 文本），跑出 2×2 的开关矩阵（patch 14/16 × metadata True/False），把结果整理成一张 Markdown 表格。验收标准：

- 表格能直观呈现「图像尺寸随 patch 变化、视频形态随 metadata 变化、kwargs 的 fps 键随 metadata 出现/消失」三条规律；
- 表格下方用自己的话写 3 句结论，分别对应本讲的三个学习目标。

本实践完全离线可跑（合成数据），运行结果中的具体数值标记为「待本地验证」。

## 6. 本讲小结

- `qwen-vl-utils` 的公共 API 只有 5 个函数（[\_\_init\_\_.py:L1-L7](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/__init__.py#L1-L7)），核心入口 `process_vision_info` 是「messages → 视觉张量 + kwargs」的分发组装器，服务 vLLM 等手动传张量的场景。
- 数据流三步：`extract_vision_info` 收集视觉元素（宽进）→ 按数据键分发给 `fetch_image`/`fetch_video`（严出，缺键即 `ValueError`）→ 组装返回值与 `video_kwargs`。
- 返回契约由两个开关决定：`return_video_kwargs=True` 得到三元组；`return_video_metadata=True` 时每个视频变成 `(tensor, metadata)` 元组，且 kwargs 不再含 `fps`。
- `video_kwargs` 恒含 `do_sample_frames: False`（帧已采样完，processor 不必再做）；`fps` 列表仅存在于 Qwen2.5-VL 兼容分支（源码注释 `# BC for qwen2.5vl`）。
- `image_patch_size` 默认 14 对应 Qwen2/2.5-VL 的对齐倍数 28；Qwen3-VL 必须传 16（对齐倍数 32），视觉 token 数为 \(\frac{H}{2p} \times \frac{W}{2p}\)。传错不报错、只静默算错网格，最佳实践是从 `processor.image_processor.patch_size` 动态读取。

## 7. 下一步学习建议

- **u2-l2（图像链路）**：精读 `fetch_image` 与 `smart_resize`，弄清本讲「预算区间」「按 \(\beta\) 等比缩放」「向下/向上取整对齐」的完整算法，以及 `resized_height/resized_width`、`min_pixels/max_pixels` 的精确控制方式。
- **u2-l3（视频链路）**：精读 `fetch_video` 与 `smart_nframes` 的帧数采样公式、torchvision/decord/torchcodec 三种解码后端的探测与回退（本讲 4.4.3 提到的 `get_video_reader_backend`）。
- **u2-l4（video metadata）**：深入本讲埋下的伏笔——metadata 各字段如何支撑 Qwen3-VL 的文本-时间戳对齐，帧列表输入时 `total_num_frames = (nframes / sample_fps) * raw_fps` 的合成逻辑。
- 顺带对照 [qwen-vl-utils/README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/README.md) 三代示例的差异，自测是否真正理解了本讲的版本兼容策略。

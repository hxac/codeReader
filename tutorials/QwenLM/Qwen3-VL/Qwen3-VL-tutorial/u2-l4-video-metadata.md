# u2-l4 video metadata 与 Qwen3-VL 时间戳对齐机制

> 讲义 ID：u2-l4 ｜ 层级：intermediate ｜ 依赖：u2-l3（视频链路与三种解码后端）
> 关键源码：`qwen-vl-utils/src/qwen_vl_utils/vision_process.py`、`README.md`

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `video_metadata` 字典中 `fps`、`frames_indices`、`total_num_frames`（以及 `video_backend`）各字段时间语义，并手算「第 i 个采样帧对应视频第几秒」。
2. 解释 Qwen3-VL 为什么放弃 Qwen2.5-VL 的 T-RoPE 绝对时间位置编码，改为「文本-时间戳对齐」，以及 `return_video_metadata` 这个开关在其中扮演的角色。
3. 在没有视频文件、只有一组图片帧时，用 `sample_fps`（必要时加 `raw_fps`）构造出带时间信息的视频输入，并读懂合成的 `video_metadata`。
4. 理清 `video_kwargs` 在「Qwen2.5 兼容模式」与「Qwen3 模式」两条分支下的不同内容，以及它一路传到 processor / vLLM 的完整链路。

## 2. 前置知识

在进入源码之前，先统一几个本讲反复使用的基础概念：

- **帧率（fps, frames per second）**：视频每秒包含多少帧。一个 `25fps、250 帧` 的视频时长就是 \( 250/25 = 10 \) 秒。反过来，**帧号 ÷ 帧率 = 该帧出现的秒数**，这是本讲最重要的一把换算尺。
- **原始帧率 vs 采样率**：u2-l3 讲过 `smart_nframes` 会按你指定的 `fps`（默认 2.0）从原视频里均匀抽帧。抽完之后「实际每秒保留了几个帧」叫**实际采样率**（源码里的 `sample_fps`），它不一定等于你请求的值——因为帧数会被夹进 `[4, 768]` 区间并向下取偶。
- **T-RoPE 是什么**：Qwen2.5-VL 的多模态旋转位置编码把位置 id 排成时间/高/宽三行，其中时间行按「第几个采样帧」递增。也就是说，时间信息被**隐式地编码进位置编码**里，模型只能「感觉到」先后顺序，不能直接知道「这是第 3.5 秒」。
- **时间戳对齐（Text–Timestamp Alignment）**：Qwen3-VL 的做法是把每一帧的**绝对时间（第几秒）**作为显式信息随视觉输入一起交给模型，让画面内容与文本里的「00:05」这类时间表述直接对齐。本讲的主角 `video_metadata` 就是这些时间信息的载体。
- **字典即数据契约**：`video_metadata` 不是什么特殊类，就是一个普通 Python `dict`，由 `qwen-vl-utils` 生产、交给 processor（transformers 侧）消费。理解了字段语义，你就理解了整条链路。

另外回顾 u2-l3 的两个结论，本讲直接使用：帧数恒为偶数（`FRAME_FACTOR=2`，时间维 2× 压缩）；解码后端优先级 torchcodec > decord > torchvision，任一后端失败自动回退 torchvision。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [qwen-vl-utils/src/qwen_vl_utils/vision_process.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py) | 主战场：三个解码后端如何构造 `video_metadata`；`fetch_video` 帧列表分支如何合成 metadata；`process_vision_info` 如何组装 `video_kwargs` |
| [README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md) | 官方对「文本-时间戳对齐」的定位说明、`return_video_metadata` 用法、帧列表 + `sample_fps` 示例、processor / vLLM 的传参写法 |

一张速览图（本讲数据流）：

```text
messages 里的 video 元素
        │
        ▼
process_vision_info(..., return_video_metadata=True)
        │  内部对每个视频调 fetch_video(return_video_sample_fps=True)
        ▼
fetch_video
        ├─ video 是字符串 ──► 解码后端(torchcodec/decord/torchvision)
        │                        └─ 产出真实 video_metadata（fps/frames_indices/total_num_frames/video_backend）
        └─ video 是图片列表 ──► 线程池 fetch_image + 补偶帧
                                 └─ 合成 video_metadata（时长守恒公式）
        │
        ▼
返回 (video, video_metadata) 元组 + sample_fps
        │
        ▼
video_kwargs = {'do_sample_frames': False}   # Qwen3 模式；Qwen2.5 兼容模式额外带 fps 列表
        │
        ▼
processor(..., video_metadata=[...], **video_kwargs)   # 或 vLLM 的 mm_processor_kwargs
```

## 4. 核心概念与源码讲解

### 4.1 video_metadata 的结构与各字段的时间语义

#### 4.1.1 概念说明

当你用 `return_video_metadata=True` 调用 `process_vision_info`（或直接调 `fetch_video`）时，每个视频的返回值从「裸张量」变成「(张量, metadata) 元组」。metadata 记录的是**采样过程的时间档案**：这段张量里的每一帧，是从原视频的哪个位置抽出来的、原视频有多长、以什么帧率播放。

它解决的问题是：模型拿到的视频张量经过抽帧与缩放后，**时间信息全部丢失**——张量只是一个 `(T, C, H, W)` 的数组，谁也不知道第 0 帧和第 1 帧之间隔了 0.5 秒还是 3 秒。metadata 就是把这些丢掉的时间信息打包随身携带。

#### 4.1.2 核心流程

三个解码后端构造 metadata 的步骤完全同构：

1. 解码器探测原视频的 `total_frames`（总帧数）与 `video_fps`（原始帧率）。
2. `smart_nframes`（u2-l3 精读过）算出要抽的帧数 `nframes`。
3. 用 `linspace(起点帧, 终点帧, nframes)` **均匀**选出帧号列表 `frames_indices`。
4. 计算**实际采样率**：

\[ \text{sample\_fps} = \frac{\text{nframes}}{\text{total\_frames}} \times \text{video\_fps} = \frac{\text{nframes}}{\text{视频时长（秒）}} \]

5. 打包成字典：`fps`（原帧率）、`frames_indices`（采样帧号）、`total_num_frames`（总帧数）、`video_backend`（后端名）。

有了它，两个关键时间量都能手算：

\[ t_i = \frac{\text{frames\_indices}[i]}{\text{fps}} \quad\text{（第 } i \text{ 个采样帧出现的秒数）} \]

\[ T_{\text{total}} = \frac{\text{total\_num\_frames}}{\text{fps}} \quad\text{（视频总时长，单位秒）} \]

#### 4.1.3 源码精读

先看 torchvision 后端（另外两个后端逻辑相同）：

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L214-L225](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L214-L225) —— `smart_nframes` 定帧数后，`linspace` 在 `[0, total_frames-1]` 上均匀取 `nframes` 个帧号；`sample_fps` 按「抽出的帧数 ÷ 原时长」计算；最后把 `fps / frames_indices / total_num_frames / video_backend` 四个字段打包返回：

```python
nframes = smart_nframes(ele, total_frames=total_frames, video_fps=video_fps)
idx = torch.linspace(0, total_frames - 1, nframes).round().long()
sample_fps = nframes / max(total_frames, 1e-6) * video_fps
video = video[idx]

video_metadata = dict(
    fps=video_fps,
    frames_indices=idx,
    total_num_frames=total_frames,
    video_backend="torchvision",
)
```

decord 与 torchcodec 后端产出的字段一致：

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L321-L329](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L321-L329) —— decord 后端同样按公式算 `sample_fps`，并把后端名写进 metadata：

```python
sample_fps = nframes / max(total_frames, 1e-6) * video_fps
video_metadata = dict(
    fps=video_fps,
    frames_indices=idx,
    total_num_frames=total_frames,
    video_backend="decord",
)
```

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L371-L377](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L371-L377) —— torchcodec 后端的 metadata 结构与上面完全相同，只是 `video_backend="torchcodec"`。

字段语义总结表：

| 字段 | 含义 | 时间换算 |
| --- | --- | --- |
| `fps` | 原视频帧率（解码器报告值） | 换算尺的分母 |
| `frames_indices` | 采样帧在**原视频**中的帧号 | 第 i 帧时间 = `frames_indices[i] / fps` |
| `total_num_frames` | 原视频总帧数（设 `video_start/video_end` 时为区间内帧数，见下） | 总时长 = `total_num_frames / fps` |
| `video_backend` | 解码后端名（仅文件路径有此字段） | 无时间语义，用于溯源 |

两个容易踩的细节，均直接来自源码：

- **类型差异**：torchvision 后端的 `frames_indices` 是 `torch.Tensor`（L215 没有 `.tolist()`），decord / torchcodec 是 Python `list`（L317、L366 调了 `.tolist()`）。打印时形态不同，做时间戳计算时注意统一转 `int`。
- **截取区间会改写 `total_num_frames` 的含义**：[vision_process.py:L310-L317](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L310-L317) 中，decord 后端先用 `calculate_video_frame_range` 把 `total_frames` **覆盖**为区间内帧数，但 `idx = linspace(start_frame, end_frame, nframes)` 里的帧号仍是**原视频的绝对帧号**。因此设了 `video_start/video_end` 后：`total_num_frames / fps` 是**区间时长**，而 `frames_indices[i] / fps` 仍是**相对整个视频的绝对时间戳**。该函数的定义在 [vision_process.py:L234-L289](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L234-L289)：未设区间时直接返回 `(0, total_frames-1, total_frames)`（L262-L263），设了则按秒换算成起止帧并校验顺序（L267-L289）。

#### 4.1.4 代码实践

**实践目标**：对一段真实视频拿到 metadata，手算每帧时间戳，与库返回的 `sample_fps` 互相印证。

**操作步骤**（示例代码，可直接保存为 `inspect_metadata.py` 运行）：

```python
from qwen_vl_utils.vision_process import fetch_video

# 注意：process_vision_info 内部恒以 return_video_sample_fps=True 调 fetch_video，
# 直接调 fetch_video 时需要显式传这两个开关。
ele = {"video": "file:///path/to/demo.mp4", "fps": 2.0}

(video, metadata), sample_fps = fetch_video(
    ele,
    image_patch_size=16,              # Qwen3-VL 必须传 16（见 u2-l1）
    return_video_metadata=True,
    return_video_sample_fps=True,
)

print("video.shape =", tuple(video.shape))       # (T, C, H, W)
print("sample_fps  =", sample_fps)
print("fps               =", metadata["fps"])
print("frames_indices    =", metadata["frames_indices"])
print("total_num_frames  =", metadata["total_num_frames"])
print("video_backend     =", metadata.get("video_backend"))

# 手算时间戳：帧号 / 原帧率
idx = [int(i) for i in metadata["frames_indices"]]
timestamps = [round(i / metadata["fps"], 3) for i in idx]
print("每帧时间戳(秒) =", timestamps)
print("总时长(秒)     =", metadata["total_num_frames"] / metadata["fps"])

# 印证：sample_fps == nframes / 总时长
print("nframes / T =", video.shape[0] / (metadata["total_num_frames"] / metadata["fps"]))
```

**需要观察的现象**：

- `video.shape[0]`（帧数 T）是偶数（`FRAME_FACTOR=2`，u2-l3 讲过）。
- `frames_indices` 是从 0 到 `total_num_frames-1` 的**近似等差**序列（`linspace` 均匀采样）。
- 最后一个打印值应与 `sample_fps` 相等。

**预期结果**：以一个 `25fps、10 秒、250 帧` 的视频、`fps=2.0` 请求为例，按源码公式推导：`nframes = 10 × 2 = 20`，`frames_indices ≈ [0, 13, 26, ...]`（250 帧里均匀取 20 个），`sample_fps = 20/10 = 2.0`，`总时长 = 250/25 = 10.0` 秒。若视频短于 2 秒，帧数会被 `FPS_MIN_FRAMES=4` 抬底，此时 `sample_fps = 4 / 时长 > 2`，不再等于请求值——这正是「实际采样率」要单独返回的原因。以上数值由源码公式推导，具体打印值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：一个 `30fps、总 303 帧` 的视频，`frames_indices = [0, 30, 60, 90, 120, 150]`。求每个采样帧的时间戳与实际采样率。

**答案**：时间戳 = 帧号 ÷ 30，即 `[0, 1, 2, 3, 4, 5]` 秒；实际采样率 = 6 帧 ÷ (303/30 ≈ 10.1 秒) ≈ 0.593 fps。注意它小于常见的请求值 2.0——因为帧数被 `floor_by_factor` 取偶及 `max_frames` 上限约束后，实际抽到的密度由公式 \( \text{nframes}/T \) 决定，而非请求值。

**练习 2**：为什么 metadata 里 `total_num_frames` 是「原视频总帧数」而不是「抽出来的帧数」？后者从哪能拿到？

**答案**：抽出来的帧数就是返回张量的 `video.shape[0]`，调用方天然可见，无需重复；而原视频总帧数在抽样后彻底丢失，它是计算**视频总时长**（`total_num_frames / fps`）的唯一依据，必须随张量一并带出。

**练习 3**：设了 `video_start=10, video_end=20`（decord 后端）后，`total_num_frames / fps` 与 `frames_indices[i] / fps` 分别表示什么？

**答案**：前者是**截取区间**的时长（约 10 秒），因为 [L311-L315](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L310-L317) 用区间帧数覆盖了 `total_frames`；后者仍是**相对原视频的绝对时间戳**（例如第 12.4 秒），因为 `idx` 里存的是原视频绝对帧号。

### 4.2 时间戳编码机制：从 T-RoPE 到文本-时间戳对齐

#### 4.2.1 概念说明

Qwen2.5-VL 用 T-RoPE 处理视频时间：位置 id 的时间行随采样帧序号递增。这有两个先天限制：

1. **只有序，没有量**：时间行编码的是「第 0 帧、第 2 帧、第 4 帧……」这样的相对次序，模型要靠训练隐式地猜「两帧之间隔了多久」，无法精确换算成秒。
2. **占用位置编码预算**：时间维挤占了多模态旋转位置编码的频段分配（u1-l1 提过 Interleaved-MRoPE 的动机之一就是把全频段留给更需要的位置维度）。

Qwen3-VL 的官方表述（README「Model Architecture Updates」第 3 条）：

[README.md:L54](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L54) —— 「Text–Timestamp Alignment: Moves beyond T-RoPE to precise, timestamp-grounded event localization for stronger video temporal modeling」，即超越 T-RoPE，实现以时间戳为锚的精确事件定位。

落点就是本讲的 metadata：**采样时把每一帧的绝对秒数（`frames_indices[i] / fps`）记录下来，随视觉输入显式交给模型**。这样「第 3 秒画面里有人举手」与文本里的「03:00」处在同一坐标系统下，模型可以 grounding 式地回答「事件发生在第几秒」。README 对这个开关的说明：

[README.md:L422-L433](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L422-L433) —— 官方列出 `qwen-vl-utils` 的两处新变化：`image_patch_size`（14 对应 Qwen2.5-VL、16 对应 Qwen3-VL），以及 `return_video_metadata`（仅 Qwen3-VL，原因写着「Due to the new video processor」）；并给出两种调用对照——Qwen2.5-VL 直接 `process_vision_info(messages, return_video_kwargs=True)`，Qwen3-VL 需要 `image_patch_size=16, return_video_kwargs=True, return_video_metadata=True`。

需要划清边界的是：metadata 是 `qwen-vl-utils`（本仓库）这一侧的**生产端**；它被消费成什么（processor 如何把帧级时间戳换算到视觉网格、位置 id 的 t 维度在新模型里如何退化），代码在 transformers 与微调框架里。本仓库 `qwen-vl-finetune/qwenvl/data/rope2d.py` 中三代 `get_rope_index_*` 的对比（Qwen3-VL 版本里 t 维不再吃时间步进）会在 **u5-l4** 精读，这里先建立「时间信息从位置编码搬家到显式时间戳」的整体图景即可。

#### 4.2.2 核心流程

时间信息在两种代际协议下的运输路线对比：

```text
Qwen2.5-VL（T-RoPE 协议）
  fetch_video 返回 (video, sample_fps)
      └─ sample_fps 汇入 video_kwargs['fps'] 列表
            └─ processor 由 fps 推时间步 → 写进 position_ids 的 t 行（隐式）

Qwen3-VL（时间戳对齐协议）
  fetch_video 返回 (video, video_metadata)
      └─ metadata 里带 frames_indices/fps/total_num_frames（绝对时间可换算）
            └─ processor(text, videos, video_metadata=[...], ...)（显式）
```

关键差异一句话：**旧协议把时间压进位置编码，新协议把时间作为数据随帧同行**。这也是为什么 4.4 节你会看到 `return_video_metadata=True` 时 `video_kwargs` 反而**不再**携带 `fps`——时间信息已经换了交通工具。

#### 4.2.3 源码精读

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L477-L480](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L477-L480) —— `fetch_video` 的收尾：把 metadata 与视频张量**绑成一个元组**作为最终返回，这正是「时间信息随帧同行」在代码层面的形态：

```python
final_video = (video, video_metadata) if return_video_metadata else video
if return_video_sample_fps:
    return final_video, sample_fps
return final_video
```

注意返回结构随开关变化的规则（后面拆包时要用）：

| 开关组合 | 返回值 |
| --- | --- |
| 两个都 False | `video` |
| 仅 `return_video_sample_fps=True` | `(video, sample_fps)` |
| 仅 `return_video_metadata=True` | `(video, metadata)` |
| 两个都 True | `((video, metadata), sample_fps)` |

而 `process_vision_info` 层面永远以 `return_video_sample_fps=True` 调用（见 4.4.3），所以从 `process_vision_info` 出来的每个视频元素是 `(video, metadata)` 元组（Qwen3 模式）或 `(video, sample_fps)` 中被剥掉 fps 后的 `video`（Qwen2.5 模式）。

#### 4.2.4 代码实践

**实践目标**：用同一组 messages 分别走两种协议，观察时间信息「装在哪」。

**操作步骤**：

```python
from qwen_vl_utils import process_vision_info

messages = [{
    "role": "user",
    "content": [
        {"type": "video", "video": "file:///path/to/demo.mp4"},
        {"type": "text", "text": "Describe this video."},
    ],
}]

# 协议 A：Qwen2.5-VL 兼容（时间信息 → video_kwargs['fps']）
imgs, vids, kwargs_a = process_vision_info(
    messages, image_patch_size=14, return_video_kwargs=True)
print("A: video_kwargs =", kwargs_a, "| videos[0].shape =", vids[0].shape)

# 协议 B：Qwen3-VL（时间信息 → metadata 元组）
imgs, vids, kwargs_b = process_vision_info(
    messages, image_patch_size=16, return_video_kwargs=True, return_video_metadata=True)
print("B: video_kwargs =", kwargs_b)
print("B: videos[0] 是元组:", isinstance(vids[0], tuple))
v, meta = vids[0]
print("B: video.shape =", tuple(v.shape), "| metadata =", {k: meta[k] for k in ("fps", "total_num_frames")})
```

**需要观察的现象**：协议 A 的 `video_kwargs` 里多出一个 `fps` 列表、`videos[0]` 是裸张量；协议 B 的 `video_kwargs` 只有 `do_sample_frames`、`videos[0]` 变成 `(张量, metadata)` 元组。

**预期结果**：A 打印 `{'do_sample_frames': False, 'fps': [x.xx]}`；B 打印 `{'do_sample_frames': False}` 且 `isinstance(vids[0], tuple) == True`。此结论由 [vision_process.py:L528-L530](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L528-L530) 直接保证；具体 fps 数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：用一句话向同事解释「Qwen3-VL 的时间戳对齐比 T-RoPE 强在哪」。

**答案**：T-RoPE 只把「帧的先后次序」编码进位置 id，时间是被隐式猜测的；时间戳对齐把每帧的**绝对秒数**（由 `frames_indices/fps` 换算）作为显式数据随视觉输入提供给模型，因此能做「事件发生在第几秒」的精确定位，而不只是顺序感知。

**练习 2**：如果调用方在 Qwen3 模式下忘了传 `return_video_metadata=True`，会发生什么？

**答案**：不会报错，但时间信息会退化成 Qwen2.5 协议——`video_kwargs` 里带 `fps` 列表、视频是裸张量、没有 metadata。processor 拿不到 `video_metadata`，时间戳对齐无从谈起；且若 processor 按新协议期望 metadata，行为不可预期。这属于 u2-l1 提过的「静默错配」家族：类型/开关传错不抛异常，只是结果悄悄变差。

**练习 3**：`sample_fps`（实际采样率）在 Qwen3 模式下还需要吗？它去哪了？

**答案**：metadata 路径下它不再进 `video_kwargs`（L529-L530 只在 `not return_video_metadata` 时写入 `fps` 列表）。它仍由 `fetch_video` 计算并返回，但只在 Qwen2.5 兼容分支被消费；Qwen3 的时间换算完全基于 metadata 的 `frames_indices` 与 `fps`。

### 4.3 帧列表输入的合成 metadata

#### 4.3.1 概念说明

u2-l3 提过 `fetch_video` 支持把**一组图片**当视频输入（`video` 字段传 list）。这条路径没有解码器，因此没有「原视频帧率」「原视频帧号」可言——时间档案的原料缺失。解决办法是让用户**声明**采样参数，再据此**合成**一份 metadata：

- `sample_fps`：这些图片是按每秒几帧的速率采出来的（README 示例注释写明「used to determine timestamps for each frame」）。
- `raw_fps`（可选）：原始视频的帧率，默认取 `sample_fps`。

合成的目标只有一个：让下游以为它收到的是一段「时长 \( N/\text{sample\_fps} \) 秒」的普通视频。

#### 4.3.2 核心流程

设用户给了 \( N' \) 张图（补偶帧后为 \( N \) 帧，\( N = \lceil N'/2 \rceil \times 2 \)），声明的采样率为 \( f_s \)、原始帧率为 \( f_r \)（默认 \( f_r = f_s \)），则合成 metadata 为：

\[ \text{fps} = f_r,\qquad \text{frames\_indices} = [0, 1, \dots, N-1],\qquad \text{total\_num\_frames} = \frac{N}{f_s} \times f_r \]

它有一条**时长守恒**性质——不管 `raw_fps` 声明成多少，总时长不变：

\[ \frac{\text{total\_num\_frames}}{\text{fps}} = \frac{(N/f_s)\times f_r}{f_r} = \frac{N}{f_s} \quad\text{（秒）} \]

直觉解读：4 帧图片、`sample_fps=1`，就是宣称「这是一个 4 秒的视频，每秒一帧」。若再声明 `raw_fps=25`，则 `total_num_frames = 4 × 25 = 100`，意思是「等价的原始视频是 25fps、100 帧」，时长依然 4 秒——`raw_fps` 只改变「原视频多大多密」的观感标尺，不改变时间轴本身。

#### 4.3.3 源码精读

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L415-L446](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L415-L446) —— `fetch_video` 的帧列表分支。先用 `ThreadPoolExecutor` 并发调用 `fetch_image`（每帧复用图像链路的像素预算逻辑），再补偶帧、取 `sample_fps`、合成 metadata：

```python
# The input is a list of frames
assert isinstance(ele["video"], (list, tuple))
process_info = ele.copy()
process_info.pop("type", None)
process_info.pop("video", None)
# use ThreadPoolExecutor to parallel process frames
max_workers = min(MAX_NUM_WORKERS_FETCH_VIDEO, len(ele["video"]))
with ThreadPoolExecutor(max_workers=max_workers) as executor:
    futures = [
        executor.submit(fetch_image, {"image": video_element, **process_info}, image_patch_size)
        for video_element in ele["video"]
    ]
    image_list = [future.result() for future in futures]

nframes = ceil_by_factor(len(image_list), FRAME_FACTOR)
if len(image_list) < nframes:
    image_list.extend([image_list[-1]] * (nframes - len(image_list)))   # 复制末帧补偶

sample_fps = ele.get("sample_fps", 2.0)
...
# fake video metadata
raw_fps = process_info.pop("raw_fps", sample_fps)
video_metadata = dict(
    fps=raw_fps,
    frames_indices=[i for i in range(len(video))],
    total_num_frames=(nframes / sample_fps) * raw_fps,
)
```

逐点说明：

- **补偶帧**（L430-L432）：`ceil_by_factor(len, 2)` 保证帧数为偶数，不足就复制最后一帧——与 u2-l3 视频路径 `floor_by_factor` 的动机相同（时间维 2× 压缩，`FRAME_FACTOR=2`，见 [L31-L35](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L31-L35) 的常量定义）。
- **`sample_fps` 默认 2.0**（L434）：与视频路径的默认采样 `FPS = 2.0` 保持一致的时间假设。
- **源码自己承认这是合成品**：注释「fake video metadata」。注意它与真实 metadata 的两处差别：没有 `video_backend` 字段；`total_num_frames` 是**浮点数**（除法所致），而文件路径产出的是整数。打印时看到 `4.0` 不要惊讶。
- **`frames_indices` 是 0..N-1 的连续序号**：即「相对于这组帧自身的序号」。默认 `raw_fps = sample_fps` 时，帧 i 的时间戳 \( i / f_r = i / f_s \)（每帧间隔 \( 1/f_s \) 秒），时间轴自洽。

官方用法示例（注意 `sample_fps` 写在视频元素里，值是字符串 `'1'`）：

[README.md:L553-L572](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L553-L572) —— messages 中的 `video` 字段传 4 个图片路径的列表，并附 `'sample_fps':'1'`，注释说明 sample_fps 是「帧采样率，用于确定每一帧的时间戳」：

```python
{
    "type": "video",
    "video": [
        "file:///path/to/frame1.jpg",
        "file:///path/to/frame2.jpg",
        "file:///path/to/frame3.jpg",
        "file:///path/to/frame4.jpg",
    ],
    'sample_fps':'1',
},
```

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手触发 metadata 的两种生产方式——真实解码与帧列表合成，重点解释合成 `total_num_frames` 的含义。

**操作步骤**：

```python
import torch
from PIL import Image
from qwen_vl_utils.vision_process import fetch_video

# ── Part 1：真实视频文件（如无本地视频，可沿用 u2-l3 实践下载的那个）──
ele = {"video": "file:///path/to/demo.mp4"}
(video, metadata), sample_fps = fetch_video(
    ele, image_patch_size=16,
    return_video_metadata=True, return_video_sample_fps=True)
print("[文件] shape =", tuple(video.shape))
print("[文件] metadata =", metadata)
print("[文件] 总时长 =", metadata["total_num_frames"] / metadata["fps"], "秒")

# ── Part 2：4 帧图片列表 + sample_fps=1 ──
frames = [Image.new("RGB", (64, 64), c) for c in
          ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0))]
ele2 = {"video": frames, "sample_fps": 1.0}
(video2, meta2), sample_fps2 = fetch_video(
    ele2, image_patch_size=16,
    return_video_metadata=True, return_video_sample_fps=True)

print("[帧列表] shape =", tuple(video2.shape))
print("[帧列表] sample_fps =", sample_fps2)
print("[帧列表] fps              =", meta2["fps"])
print("[帧列表] frames_indices   =", meta2["frames_indices"])
print("[帧列表] total_num_frames =", meta2["total_num_frames"])
print("[帧列表] 总时长 =", meta2["total_num_frames"] / meta2["fps"], "秒")
print("[帧列表] 有 video_backend 字段吗：", "video_backend" in meta2)
```

**需要观察的现象**：Part 2 中 `shape` 的帧维为 4、`frames_indices = [0, 1, 2, 3]`、`fps = 1.0`、`total_num_frames = 4.0`（注意是 float）、无 `video_backend` 键；`total_num_frames / fps = 4.0` 秒。

**预期结果**（由源码公式确定性推导，具体打印待本地验证）：
`nframes = ceil_by_factor(4, 2) = 4`，无需补帧；`fps = raw_fps = sample_fps = 1.0`；`total_num_frames = (4/1) × 1 = 4.0`。

**`total_num_frames = 4.0` 的含义**：这 4 张图宣称以每秒 1 帧的速率采得，因此等价于一个「1fps、4 帧、4 秒」的完整视频；`total_num_frames ÷ fps = 4` 秒即其总时长。若把 `raw_fps` 改成 25（`{"video": frames, "sample_fps": 1.0, "raw_fps": 25.0}`），`total_num_frames` 变为 `(4/1)×25 = 100.0`，总时长依旧 `100/25 = 4` 秒——这就是 4.3.2 的时长守恒：`raw_fps` 只改「原视频标尺」，不改时间轴。

**进阶观察**（可选）：只给 3 张图，看补偶帧——`nframes = ceil_by_factor(3, 2) = 4`，`video2.shape[0]` 仍是 4，第 4 帧是第 3 帧的复制；`frames_indices = [0, 1, 2, 3]` 与图片内容无关，只数张数。

#### 4.3.5 小练习与答案

**练习 1**：6 张图、`sample_fps=2`，合成 metadata 是什么？总时长多少？

**答案**：`nframes = 6`（已偶）；`fps = 2.0`；`frames_indices = [0..5]`；`total_num_frames = (6/2)×2 = 6.0`；总时长 = 6/2 = **3 秒**，即每帧间隔 0.5 秒。

**练习 2**：用户传 `sample_fps=1, raw_fps=24`，共 5 张图。求补帧后的张量帧数、`total_num_frames` 与总时长，并说明 `raw_fps` 改变了什么、没改变什么。

**答案**：`nframes = ceil_by_factor(5,2) = 6`（末帧复制 1 次）；`total_num_frames = (6/1)×24 = 144.0`；总时长 = 144/24 = **6 秒**（= 6 帧 ÷ 1fps）。`raw_fps` 改变的是「等价原视频的帧数标尺」（6 → 144），没改变时间轴（仍 6 秒）与帧间隔（1 秒）。

**练习 3**：为什么帧列表分支的 `frames_indices` 是 `[0,1,...,N-1]` 这种「假帧号」，而不尝试还原原视频里的真实帧号？

**答案**：因为这条路径根本没有原视频——图片列表可能来自任何地方（截图、抽帧后丢掉源文件、甚至纯合成序列），库无从得知也无法验证真实帧号。它只能以「子序列序号 + 用户声明的时间参数」构造一个自洽的时间档案，把「这组帧等价于多长的视频」这一最小必要信息交给下游；源码注释「fake video metadata」正是在坦诚这一点。

### 4.4 video_kwargs 的传递链路与版本兼容策略

#### 4.4.1 概念说明

`video_kwargs` 是 `process_vision_info(..., return_video_kwargs=True)` 的第三个返回值，它的职责是**告诉 processor 采样这件事已经做完了、以及该按哪代协议理解视频输入**。它很小，却是新旧两代模型共用同一个库的关键兼容层：

- 恒有 `do_sample_frames: False`——帧已在 `qwen-vl-utils` 内按 `smart_nframes` 采完，processor 不要再按 fps 二次抽帧（否则时间戳与画面全部错位）。
- 仅在 **Qwen2.5 兼容模式**（`return_video_metadata=False`）下额外带 `fps: [每个视频的实际采样率]`，供旧 processor 构造 T-RoPE 时间步。
- Qwen3 模式下时间信息走 metadata 元组，`fps` 列表不再出现。

#### 4.4.2 核心流程

```text
process_vision_info(messages, return_video_kwargs=True, return_video_metadata=?)
        │ 对每个 video 元素：
        │   fetch_video(vision_info, return_video_sample_fps=True,   ← 恒开
        │               image_patch_size=..., return_video_metadata=?)
        │   sample_fps 收进 video_sample_fps_list
        ▼
video_kwargs = {'do_sample_frames': False}
        │
        ├── return_video_metadata=False（Qwen2.5 兼容）
        │       └─ video_kwargs['fps'] = video_sample_fps_list
        │
        └── return_video_metadata=True（Qwen3）
                └─ 时间信息在 (video, metadata) 元组里，kwargs 不含 fps
        ▼
transformers: processor(text, videos=videos, video_metadata=[...], **video_kwargs)
vLLM:         {'prompt', 'multi_modal_data': {'video': [(v, meta), ...]},
               'mm_processor_kwargs': video_kwargs}
```

#### 4.4.3 源码精读

[qwen-vl-utils/src/qwen_vl_utils/vision_process.py:L513-L534](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L513-L534) —— `process_vision_info` 的分发与 kwargs 组装。视频元素**恒以** `return_video_sample_fps=True` 调用 `fetch_video`（所以 `sample_fps` 总会被算出来），但它是否对外可见取决于 `return_video_metadata`：

```python
elif "video" in vision_info:
    video_input, video_sample_fps = fetch_video(vision_info, return_video_sample_fps=True,
                image_patch_size=image_patch_size, return_video_metadata=return_video_metadata)
    video_sample_fps_list.append(video_sample_fps)
    video_inputs.append(video_input)
...
video_kwargs = {'do_sample_frames': False}
if not return_video_metadata: # BC for qwen2.5vl
    video_kwargs.update({'fps': video_sample_fps_list})
```

那句 `# BC for qwen2.5vl`（backward compatibility）就是两代协议的分岔口。下游两种接法：

**transformers 直用**（官方示例）：

[README.md:L639-L651](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L639-L651) —— 先用 `zip(*videos)` 把「(video, metadata) 元组的列表」拆成「张量列表 + metadata 列表」两个平行列表，再连同 `do_resize=False`（u2-l2 讲过：库内已缩放，防重复）一起喂给 processor：

```python
images, videos, video_kwargs = process_vision_info(
    messages, image_patch_size=16, return_video_kwargs=True, return_video_metadata=True)

# split the videos and according metadatas
if videos is not None:
    videos, video_metadatas = zip(*videos)
    videos, video_metadatas = list(videos), list(video_metadatas)
else:
    video_metadatas = None

inputs = processor(text=text, images=images, videos=videos,
                   video_metadata=video_metadatas, return_tensors="pt",
                   do_resize=False, **video_kwargs)
```

**vLLM 路径**：

[README.md:L997-L1018](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L997-L1018) —— `prepare_inputs_for_vllm` 模式（u1-l4、u3-l2 都见过它）：这里**不拆包**，`video_inputs` 里的 `(video, metadata)` 元组原样放进 `multi_modal_data['video']`，`video_kwargs` 整体作为 `mm_processor_kwargs` 透传，由 vLLM 内部的 Qwen3-VL processor 解包消费：

```python
image_inputs, video_inputs, video_kwargs = process_vision_info(
    messages,
    image_patch_size=processor.image_processor.patch_size,
    return_video_kwargs=True,
    return_video_metadata=True
)
...
return {
    'prompt': text,
    'multi_modal_data': mm_data,          # video 是 (张量, metadata) 元组列表
    'mm_processor_kwargs': video_kwargs   # {'do_sample_frames': False}
}
```

顺带注意这里的最佳实践：`image_patch_size=processor.image_processor.patch_size` 动态取自模型配置（u2-l1 推荐过），避免硬编码 14/16 造成静默错配。

#### 4.4.4 代码实践

**实践目标**：走通「拆包 → processor」完整一跳，确认 metadata 与 `do_sample_frames` 都正确到达 processor。

**操作步骤**：

```python
from transformers import AutoProcessor
from qwen_vl_utils import process_vision_info

MODEL = "Qwen/Qwen3-VL-2B-Instruct"
processor = AutoProcessor.from_pretrained(MODEL)

messages = [{
    "role": "user",
    "content": [
        {"type": "video", "video": "file:///path/to/demo.mp4"},
        {"type": "text", "text": "Describe this video."},
    ],
}]

text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
images, videos, video_kwargs = process_vision_info(
    messages, image_patch_size=16, return_video_kwargs=True, return_video_metadata=True)

videos, video_metadatas = zip(*videos)
videos, video_metadatas = list(videos), list(video_metadatas)
print("video_kwargs      =", video_kwargs)          # 期望只有 do_sample_frames
print("video_metadatas[0]的键 =", list(video_metadatas[0].keys()))

inputs = processor(text=text, images=images, videos=videos,
                   video_metadata=video_metadatas,
                   return_tensors="pt", do_resize=False, **video_kwargs)
print({k: tuple(v.shape) for k, v in inputs.items() if hasattr(v, "shape")})
```

**需要观察的现象**：`video_kwargs` 只含 `{'do_sample_frames': False}`；metadata 键为 `fps / frames_indices / total_num_frames / video_backend` 四项；inputs 中出现 `pixel_values_videos` 等视觉张量。

**预期结果**：processor 正常产出张量；若漏传 `video_metadata` 或忘开 `return_video_metadata`，Qwen3-VL 的 processor 可能报缺参错误或静默走旧逻辑（取决于 transformers 版本）——待本地验证你所用版本的具体行为。

#### 4.4.5 小练习与答案

**练习 1**：`do_sample_frames: False` 为什么必须恒为 False？什么场景下会出问题？

**答案**：因为 `qwen-vl-utils` 已在库内完成 `smart_nframes` 抽帧并**基于抽出的帧号**生成了 metadata 时间戳；若 processor 再按 fps 二次抽帧，画面帧数变了而 metadata 还是旧帧号，时间戳与画面将整体错位。只要走 `process_vision_info` 预处理路径，这个键就必须带上且为 False。

**练习 2**：`video_kwargs['fps']` 是列表而不是单个数，为什么？

**答案**：因为一条 messages 里可以有多个视频（u1-l3 的多视频输入），`video_sample_fps_list` 按顺序记录每个视频的实际采样率，processor 需要按同样顺序为各视频构造时间步；列表长度即视频个数。

**练习 3**：transformers 路径要 `zip(*videos)` 拆包，vLLM 路径却不拆，为什么？

**答案**：接口约定不同。transformers 的 `processor(...)` 参数要求 `videos`（张量列表）与 `video_metadata`（dict 列表）是两个平行参数，所以必须拆；vLLM 的 `multi_modal_data['video']` 约定每项就是 `(video, metadata)` 元组，由 vLLM 内部再转交给 processor，因此原样放入即可。两种接法运输的是同一份时间档案，只是封装不同。

## 5. 综合实践

**任务：给一段视频建立「采样方案 → 时间轴」对照表，并验证帧列表合成 metadata 的时长等价性。**

1. 准备一段 10~30 秒的本地 mp4。
2. 对它分别用 `fps=1.0` 与 `fps=4.0` 两次调用 `fetch_video(ele, image_patch_size=16, return_video_metadata=True, return_video_sample_fps=True)`。
3. 对每次结果，用 `frames_indices / fps` 手算全部采样帧的时间戳，填入对照表：采样方案 ｜ 抽出帧数 ｜ 实际 sample_fps ｜ 前 5 帧时间戳 ｜ 总时长。验证：帧数符合 u2-l3 的 `smart_nframes` 公式；两种方案的总时长一致（同一视频）。
4. 把 `fps=1.0` 方案抽出的前 4 帧用 `torchvision.utils.save_image` 存成 jpg，再作为图片列表 + `sample_fps=1.0` 重新喂给 `fetch_video`；比较合成 metadata 的总时长与第 3 步的真实总时长（应等于 4 帧 × 1 秒 = 4 秒），并用一段话解释「fake metadata」为何仍然自洽（提示：时长守恒公式）。
5. 把以上结果整理进你的 `notes.md`。全程不需要 GPU；运行数值待本地验证。

这个任务把本讲四个模块串成一条线：真实 metadata 的生产（4.1）→ 时间戳换算（4.2）→ 帧列表合成与守恒（4.3）→ 采样完成标记与传参（4.4）。

## 6. 本讲小结

- `video_metadata` 是采样过程的时间档案：`fps`（原帧率）、`frames_indices`（采样帧的原视频帧号）、`total_num_frames`（原视频/区间总帧数）、`video_backend`（溯源）；帧时间戳 = `frames_indices[i] / fps`，总时长 = `total_num_frames / fps`。
- Qwen3-VL 用「文本-时间戳对齐」取代 T-RoPE：时间不再隐式藏进位置编码，而是由 metadata 显式随帧同行，支撑秒级事件定位；这是 `return_video_metadata` 开关存在的根本原因。
- `sample_fps`（实际采样率 = nframes ÷ 视频时长）只在 Qwen2.5 兼容分支进入 `video_kwargs['fps']`；Qwen3 模式下时间信息全部由 metadata 承载。
- 帧列表输入时库会「fake」一份 metadata：`fps = raw_fps`、`frames_indices = [0..N-1]`、`total_num_frames = (N/sample_fps)×raw_fps`，满足时长守恒 \( \text{total\_num\_frames}/\text{fps} = N/\text{sample\_fps} \)；不足偶数帧会复制末帧补齐。
- `video_kwargs` 恒带 `do_sample_frames: False`（帧已在库内采样，禁止 processor 二次抽帧）；transformers 路径要 `zip(*videos)` 拆出 `video_metadata` 平行列表，vLLM 路径则把 `(video, metadata)` 元组直接放进 `multi_modal_data`。
- 两个易踩的坑：torchvision 与 decord/torchcodec 的 `frames_indices` 类型不同（Tensor vs list）；设 `video_start/video_end` 后 `total_num_frames` 变区间帧数而帧号仍是绝对值。

## 7. 下一步学习建议

- 下一讲 **u3-l1（深入 processor）**：metadata 交给 processor 之后发生什么——chat template 渲染出 `<|video_pad|>`、像素预算如何从 processor 侧配置，是本讲 4.4 传参链路的直接下游。
- 若你想立刻追进「模型内部」，可提前阅读 **u5-l4（rope2d.py：三代 MRoPE 位置编码）**：那里对比 `get_rope_index_2/25/3`，你会看到 Qwen2.5-VL 的 t 维时间步进如何被 Qwen3-VL 淘汰（t_index 恒 0），从位置编码侧印证本讲的时间戳对齐设计。
- 建议顺手源码回看两处：[vision_process.py:L528-L530](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L528-L530)（`# BC for qwen2.5vl` 分岔口）与 [README.md:L54](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L54)（官方对 Text–Timestamp Alignment 的一句话定位），它们是理解整条视频时间链路的两枚路标。

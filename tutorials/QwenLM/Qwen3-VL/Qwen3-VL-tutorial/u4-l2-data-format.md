# 训练数据格式精读：conversations 与 \<image\>/\<video\> 占位符

## 1. 本讲目标

学完本讲，你应该能够：

1. **独立编写**完全符合 qwen-vl-finetune 规范的训练样本 JSON——包括单图、多图、视频、grounding 四类任务。
2. **说清 `image`/`video` 字段的两种写法**（单值字符串与列表）在源码里如何被统一处理，以及 `<image>`/`<video>` 占位符按什么规则与媒体文件一一配对。
3. **掌握 grounding 样本的 bbox JSON 标注格式**，知道绝对坐标与相对坐标两套体系的区别。
4. **看懂 packed（打包）样本的两层列表结构**，理解它为什么长这样。
5. 会用 `tools/check_image.py` 在训练前校验数据完整性，避免坏样本在训练中被静默跳过。

## 2. 前置知识

**SFT 数据是什么。** u4-l1 讲过，qwen-vl-finetune 做的是有监督微调（SFT）：给模型看大量「输入 → 期望输出」的样本对，只对助手回答部分计算损失。因此每条训练标注必须包含两样东西：**媒体文件在哪里**（`image`/`video` 字段），以及**问了什么、答了什么**（`conversations` 字段）。

**标注格式与推理 messages 的关系。** 你在 u1-l3 已经见过推理用的 `messages` 结构：content 是一个列表，元素带 `type` 字段（text/image/video）。训练标注其实是一个「扁平化前身」：把媒体路径直接用 `<image>`/`<video>` 这两个**纯文本占位符**写在问句里。训练框架在读取样本时，会把占位符重新展开成带 `type` 的结构化元素——本讲 4.1 会精读这个转换函数 `_build_messages`，你会看到它输出的正是你在 u1-l3 熟悉的 messages 形态。

**JSON 与 JSONL。** 标注文件是一个 JSON 数组（每行一个样本对象），或是 JSONL（每行一个 JSON 对象）。加载逻辑按文件扩展名区分，两者都支持。

**占位符为什么只能出现在提问里。** `<image>` 在提问里代表「这里插入一张图」；模型的任务是**看着图生成回答**，回答里再出现 `<image>` 没有任何图像可对应，只会污染监督信号。所以官方规则明确：占位符不得出现在答案文本中。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [qwen-vl-finetune/README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md) | 数据格式官方规范：五类示例 + 三条硬性约束（L44-L215） |
| [qwen-vl-finetune/demo/single_images.json](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/single_images.json) | 可直接训练的单图标注示例（3 条样本，图表问答） |
| [qwen-vl-finetune/demo/video.json](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/video.json) | 可直接训练的视频标注示例（3 条样本） |
| `qwen-vl-finetune/demo/images/`、`demo/videos/` | demo 标注引用的真实媒体文件（含 grounding 用的 COCO 图） |
| [qwen-vl-finetune/qwenvl/data/data_processor.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py) | 标注的消费方：`_build_messages` 占位符展开、数据集加载、packed 样本递归处理 |
| [qwen-vl-finetune/qwenvl/data/__init__.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py) | 数据集注册表：`annotation_path` 与 `data_path` 的分工（下一讲主角，本讲只涉及路径注入） |
| [qwen-vl-finetune/tools/check_image.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py) | 训练前数据校验：媒体存在性 + 占位符数量一致性 + 答案带占位符三类检查 |
| [qwen-vl-finetune/tools/pack_data.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py) | 数据打包：预估每条样本 token 数并装箱，产出 packed 标注 |
| [qwen-vl-finetune/tools/process_bbox.ipynb](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/process_bbox.ipynb) | grounding 标注转换：绝对/相对坐标两套体系的换算 |

## 4. 核心概念与源码讲解

### 4.1 单图样本：一条标注的解剖与占位符匹配机制

#### 4.1.1 概念说明

单图样本是最小、最基础的标注单元。官方规范在 README 的 JSON Data Structure 一节给出：顶层只有两个必填键——`image`（媒体文件路径）和 `conversations`（问答对话列表），问句中用 `<image>` 占位符标记图像的插入位置。

先看官方示例（[qwen-vl-finetune/README.md:L55-L70](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L55-L70)）：

```json
{
    "image": "images/001.jpg",
    "conversations": [
        {
            "from": "human",
            "value": "<image>\nWhat's the main object in this picture?"
        },
        {
            "from": "gpt",
            "value": "A red apple on a wooden table"
        }
    ]
}
```

逐字段解读：

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `image` | 字符串或字符串列表 | 媒体文件路径（相对于 `data_path`，见 4.2.3） |
| `conversations` | 列表 | 每个元素是一轮对话，从 `human` 开始，`human`/`gpt` 交替 |
| `from` | `"human"` 或 `"gpt"` | 训练时分别映射为 `user`/`assistant` 角色 |
| `value` | 字符串 | 该轮的文本；`<image>`/`<video>` 占位符只允许出现在 `human` 轮 |

仓库里真实可训练的例子是 [qwen-vl-finetune/demo/single_images.json:L2-L14](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/single_images.json#L2-L14)，它把 `<image>` 放在了问句**末尾**：

```json
{
  "image": "demo/images/10095.png",
  "conversations": [
    {
      "from": "human",
      "value": "Is the value of Favorable 38 in 2015?\n<image>"
    },
    {
      "from": "gpt",
      "value": "Yes"
    }
  ]
}
```

对比两例可以得出一个重要结论：**占位符在问句中的位置是自由的**——放开头、末尾、甚至句子中间都行，它的位置就是图像在多模态输入序列中的实际插入位置。

#### 4.1.2 核心流程

标注在训练侧被 `_build_messages` 函数消费，它把扁平标注展开成结构化 messages。伪代码：

```text
输入: item(一条标注), base_path(数据集的 data_path)
1  images ← item["image"]，若是字符串则包成 [字符串]     # 单值/列表两种写法在此统一
2  videos ← item["video"]，同样归一成列表
3  image_pool ← [ {type:image, image: 绝对路径} ... ]    # 建立待消费的媒体池
4  video_pool ← 同上
5  for 每轮对话:
6      human → user 角色:
7          用正则按 (<image>|<video>) 切分文本，切分结果保留分隔符
8          遍历切片:
9              "<image>" → 从 image_pool 头部弹出一张，作为 image 元素加入 content
10             "<video>" → 从 video_pool 头部弹出一条，作为 video 元素加入 content
11             其他文本  → 去首尾空白后作为 text 元素加入（纯空白切片直接丢弃）
12     gpt → assistant 角色:
13         整段 value 原样作为唯一 text 元素
14 若 image_pool 或 video_pool 还有剩余 → 报错（媒体未被占位符消费）
15 返回 messages
```

两条硬性校验由这个流程自然推出：

- **占位符多于媒体**（第 9/10 步弹出空池）→ 抛 `ValueError`；
- **媒体多于占位符**（第 14 步有剩余）→ 抛 `ValueError`。

这正是 README「Notes」一节三条规则中前两条的源码出处——[qwen-vl-finetune/README.md:L211-L215](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L211-L215)：一个 `<image>` 必须对应恰好一个图像文件，`<video>` 同理，且这些特殊 token 不得出现在答案文本中。

#### 4.1.3 源码精读

**① 占位符常量定义。** [qwen-vl-finetune/qwenvl/data/data_processor.py:L21-L25](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L21-L25) 把标注中的字符串占位符与词表中的图像/视频填充 token id（即 u1-l3 在 `input_ids` 里见到的 `<|image_pad|>`/`<|video_pad|>`）登记为常量：

```python
IGNORE_INDEX = -100
IMAGE_TOKEN_INDEX = 151655
VIDEO_TOKEN_INDEX = 151656
DEFAULT_IMAGE_TOKEN = "<image>"
DEFAULT_VIDEO_TOKEN = "<video>"
```

**② `_build_messages` 全函数。** [qwen-vl-finetune/qwenvl/data/data_processor.py:L140-L199](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L140-L199) 是本讲最核心的一段。先看单值/列表两种写法的归一化（L141-L148）——这就是「`image` 字段既可以是字符串也可以是列表」的源码依据：

```python
images = item.get("image") or []
if isinstance(images, str):
    images = [images]
```

注意 `item.get("image") or []`：字段缺失或为空时得到空列表，后续任何 `<image>` 占位符都会因空池而报错。也就是说，**没有媒体字段的纯文本样本会直接失败**，这个框架不接受纯文本 SFT 样本。

再看占位符切分与消费（L163-L182）。正则带捕获组，`re.split` 会把分隔符保留在结果里：

```python
text_parts = re.split(r"(<image>|<video>)", text)

for seg in text_parts:
    if seg == "<image>":
        if not image_pool:
            raise ValueError(
                "Number of <image> placeholders exceeds the number of provided images"
            )
        content.append(image_pool.pop(0))
    elif seg == "<video>":
        ...
        content.append(video_pool.pop(0))
    elif seg.strip():
        content.append({"type": "text", "text": seg.strip()})
```

三个细节值得注意：

- `pop(0)` 是**先进先出**——第 k 个出现的 `<image>` 占位符对应路径列表中的第 k 张图；
- 文本切片执行 `seg.strip()`（L181-L182），所以 `<image>\nWhat's...` 里的换行会被去掉，纯空白的切片（比如两个连续占位符之间的空白）整个被丢弃；
- `human` 轮以外的轮次走 L185-L187 的 else 分支——**assistant 轮永远只构造纯文本元素**，即使答案里写了 `<image>` 也不会被消费，只会以字面文本进入训练序列。这解释了为什么答案带占位符是必须禁止的脏数据。

最后是剩余媒体检查（L189-L197）：池子非空即抛错。它与上面的空池检查一起，把「占位符数量 == 媒体数量」变成强制约束。

**③ 转换结果与 u1-l3 的衔接。** `_build_messages` 返回的 messages 随后被送进 `processor.apply_chat_template(messages, tokenize=True, ...)`（[qwen-vl-finetune/qwenvl/data/data_processor.py:L213-L215](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L213-L215)）——与推理时完全相同的入口。区别只在训练额外构造了 `labels`（哪些 token 计算损失），那是 u5-l3 的内容。

**④ 坏样本的下场：静默跳过。** 如果一条样本违反了上述任何约束，`_build_messages` 抛出的 `ValueError` 会被 `__getitem__` 的重试机制捕获（[qwen-vl-finetune/qwenvl/data/data_processor.py:L345-L387](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L345-L387)）：先对同一样本重试 3 次（每次睡 1 秒，防云端磁盘抖动），仍失败就取下标 `i+1` 的样本顶替，再不行才最终抛出。对确定性的格式错误来说，重试注定无效，**坏样本最终会被邻居样本悄悄替换掉**，训练不崩、日志只留一行打印。这就是为什么必须在训练前用 `check_image.py` 主动校验（见 4.3）——否则数据规模可能在不知不觉中缩水。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `re.split` 如何切分占位符，并理解空切片被丢弃的规则。

1. 操作步骤：在任何有 Python 3 的机器上运行（无需安装任何第三方库，`re` 是标准库）：

```python
import re

text = "<image>\nIs the value of Favorable 38 in 2015?\n<image>"
parts = re.split(r"(<image>|<video>)", text)
for i, seg in enumerate(parts):
    print(i, repr(seg))
```

2. 需要观察的现象：输出有几段？哪几段是占位符？哪几段是空字符串？
3. 预期结果：切出 5 段——`''`、`'<image>'`、`'\nIs the value of Favorable 38 in 2015?\n'`、`'<image>'`、`''`。首尾两个空串在 `_build_messages` 里因 `seg.strip()` 为空被丢弃，中间的文本段被 strip。
4. 进阶（需在装好 `torch`/`transformers` 的环境中运行，结果**待本地验证**）：在 `qwen-vl-finetune` 目录下直接调用真实函数，验证 4.1.2 的两条报错：

```bash
python -c "
from pathlib import Path
from qwenvl.data.data_processor import _build_messages
# 样本 A：占位符比图多 → 应抛 'placeholders exceeds'
print(_build_messages({'image': [], 'conversations': [
    {'from': 'human', 'value': '<image>\nhi'}, {'from': 'gpt', 'value': 'yo'}]}, Path('')))
# 样本 B：图比占位符多 → 应抛 'remain unused'
print(_build_messages({'image': 'a.jpg', 'conversations': [
    {'from': 'human', 'value': 'hi'}, {'from': 'gpt', 'value': 'yo'}]}, Path('')))
"
```

#### 4.1.5 小练习与答案

**练习 1**：把 `<image>` 写进 `gpt` 轮的 `value` 里，`_build_messages` 会报错吗？会发生什么？
**答案**：不会报错。assistant 轮走 else 分支，整段文本原样作为 text 元素；占位符不被消费。但此时 `human` 轮里若也有占位符，媒体池的配对仍在 human 轮内完成；真正的危害是答案文本中混入了字面 `<image>` 字符串，污染监督信号——`check_image.py` 会把它单独标记为一类问题（见 4.3.3）。

**练习 2**：一条样本的 `image` 字段漏写了，`conversations` 却有 2 个 `<image>`，训练时会发生什么？
**答案**：`item.get("image") or []` 得到空列表，第一个 `<image>` 弹空池抛 `ValueError`；`__getitem__` 重试 3 次均失败后，取下一条样本顶替，坏样本被静默跳过。

**练习 3**：占位符 `<image>` 与路径列表第 2 张图的对应关系由什么决定？
**答案**：由**文本中的出现顺序**决定。`pop(0)` 先进先出，第 k 个 `<image>` 占位符消费列表中的第 k 个路径，与路径的绝对位置、文件名都无关。

### 4.2 多图样本：列表写法、多轮对话与路径解析

#### 4.2.1 概念说明

多图样本只需两处变化：`image` 字段写成**字符串列表**，问句里放**同等数量**的 `<image>` 占位符。官方示例见 [qwen-vl-finetune/README.md:L72-L87](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L72-L87)：

```json
{
    "image": ["cats/001.jpg", "cats/002.jpg"],
    "conversations": [
        {
            "from": "human",
            "value": "<image>\n<image>\nWhat are the differences between these two cats?"
        },
        {
            "from": "gpt",
            "value": "The first cat is an orange tabby ..."
        }
    ]
}
```

两个占位符之间只隔一个换行——切分后中间的空白切片被丢弃，两张图在多模态序列中**紧挨着**排列。哪个占位符对应哪张图？按 4.1 的规则：第 1 个占位符 ↔ `cats/001.jpg`，第 2 个 ↔ `cats/002.jpg`。

`conversations` 也支持**多轮**（多对 human/gpt）。多轮时占位符可以分布在不同的 human 轮里，媒体池是整条样本全局共享的——按全部 human 轮中占位符的出现顺序依次消费，末尾统一检查剩余。

#### 4.2.2 核心流程

多图样本从标注到 messages 的完整链路：

```text
标注: image = ["A.jpg", "B.jpg"]，human 轮文本含 2 个 <image>
 ↓ _build_messages: 单值/列表归一化 → 建池
 ↓ 第 1 个 <image> 弹出 A.jpg → content 追加 {type:image, image:绝对路径(A)}
 ↓ 第 2 个 <image> 弹出 B.jpg → content 追加 {type:image, image:绝对路径(B)}
 ↓ 文本片段(去空白) → {type:text, ...}
 ↓ 池空、无剩余 → 返回 messages
 ↓ apply_chat_template(tokenize=True) → input_ids + pixel_values + image_grid_thw
```

注意 `image_grid_thw` 此时是**两行**的张量（每图一行 t/h/w 网格），u5-l4 讲 MRoPE 时会用到它。

#### 4.2.3 源码精读

**① 路径如何变成绝对路径。** [qwen-vl-finetune/qwenvl/data/data_processor.py:L40-L41](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L40-L41) 定义了唯一的路径拼接函数：

```python
def _make_abs_paths(base: Path, files: str) -> str:
    return f"{(base / files).resolve()}"
```

`base` 来自数据集注册表里的 `data_path`（下一讲 u4-l3 详讲），在加载标注时被逐条注入样本（[qwen-vl-finetune/qwenvl/data/data_processor.py:L285-L291](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L285-L291)）。所以标注里的路径**永远相对于 `data_path` 解释**；若 `data_path` 为空字符串，`Path("") / "a.jpg"` 会相对于**当前工作目录**解析——启动训练时的 cwd 不同，结果就不同，这是新人最常见的路径坑。

对照两个 demo 文件能直观看到这种「基准自选」的灵活性：

- [demo/single_images.json:L3](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/single_images.json#L3) 写的是 `"demo/images/10095.png"`（带 `demo/images/` 前缀）→ `data_path` 应设为 `qwen-vl-finetune` 仓库根目录；
- [demo/video.json:L3](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/video.json#L3) 写的是裸文件名 `"v_7bUu05RIksU.mp4"` → `data_path` 应设为 `demo/videos/`。

两份标注引用的媒体都实际存在于 `demo/images/`、`demo/videos/` 目录下，可作为你自制数据集的模板。

**② 多轮与全局消费。** [qwen-vl-finetune/qwenvl/data/data_processor.py:L158-L184](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L158-L184) 的 for 循环遍历**所有**轮次，`role = "user" if turn["from"] == "human" else "assistant"` 完成角色映射。媒体池在循环外建立、循环内消费、循环后检查——三段式保证了「跨轮全局配对 + 末尾总量守恒」。

**③ 同一张图可以复用。** [demo/single_images.json:L3-L27](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/single_images.json#L3-L27) 的前两条样本引用了**同一张** `demo/images/10095.png`，问的是不同问题（一条问 Favorable 是否为 38，一条问 Unfavorable 低于 40 的值有几个）。这说明标注层面媒体可以任意复用，一条样本的配对关系与其他样本无关。

#### 4.2.4 代码实践

**实践目标**：验证 `data_path` 与标注路径的拼接规则，以及图序对调后配对关系如何变化。

1. 操作步骤（无第三方依赖）：

```python
from pathlib import Path

# 模拟 _make_abs_paths：demo 标注的两种基准
print(Path("") / "demo/images/10095.png")       # data_path 为空 → 相对 cwd
print(Path("/abs/qwen-vl-finetune") / "demo/images/10095.png")
print(Path("/abs/qwen-vl-finetune/demo/videos") / "v_7bUu05RIksU.mp4")
```

2. 需要观察的现象：第一种拼接得到什么？它取决于什么？
3. 预期结果：第一种得到 `demo/images/10095.png`（相对路径，`.resolve()` 后锚定到运行目录的 cwd）；后两种得到确定的绝对路径。结论：`data_path` 留空时媒体路径实际相对于**启动训练的目录**解析，建议总是显式配置 `data_path`。
4. 思考题（源码阅读型）：把 4.2.1 示例中的列表改成 `["cats/002.jpg", "cats/001.jpg"]` 但问句不变，模型看到的「第一只猫」是哪张图？
5. 预期答案：是 `cats/002.jpg`。第 1 个 `<image>` 占位符弹出列表头部即 `002.jpg`，配对只看出现在序与列表序，不看语义。

#### 4.2.5 小练习与答案

**练习 1**：`"image": "a.jpg"` 与 `"image": ["a.jpg"]` 在源码看来有区别吗？
**答案**：没有。L141-L144 的归一化会把单值字符串包成单元素列表，两条路径殊途同归。单图用字符串、多图用列表，只是书写习惯。

**练习 2**：一个多轮样本有 2 个 human 轮，各含 1 个 `<image>`，`image` 列表给了 2 张图——合法吗？若只给 1 张呢？
**答案**：合法。占位符按跨轮出现顺序消费两张图，末尾池空通过检查。只给 1 张时，第 2 个占位符弹空池抛 `ValueError`，样本训练时被静默跳过。

**练习 3**：README 说 `data_path`「可以在标注里已是绝对路径时留空」。结合 `_make_abs_paths` 说明为什么绝对路径不受空 `data_path` 影响。
**答案**：`Path("") / "/data/img.jpg"` 的结果就是 `/data/img.jpg`——`pathlib` 中与绝对路径拼接会丢弃左侧操作数，因此绝对路径原样保留，与基准无关。

### 4.3 视频样本：`<video>` 占位符与三类一致性校验

#### 4.3.1 概念说明

视频样本与图像样本**结构完全同构**，只是把键名换成 `video`、占位符换成 `<video>`。官方示例见 [qwen-vl-finetune/README.md:L89-L104](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L89-L104)：

```json
{
    "video": "videos/005.mp4",
    "conversations": [
        {
            "from": "human",
            "value": "<video>\nWhat caused the blue object to move?\nOptions:\n(A) Gravity\n(B) Collision\n(C) Magnetic force"
        },
        {
            "from": "gpt",
            "value": "Answer: (B) Collision"
        }
    ]
}
```

一个关键认知：**标注里只有路径和问答，没有任何采样参数**。帧率、帧数上下限、像素预算这些都是训练启动参数（`--video_fps`、`--video_max_frames` 等，见 u5-l2），作用在 processor 上（u4-l1 读过的 `update_processor_pixels` 负责注入）。换句话说，**「视频怎么被抽帧缩放」是训练配置的事，「视频问什么答什么」才是标注的事**——两者解耦。抽帧细节你在 u2-l3 已经学过，这里不重复。

#### 4.3.2 核心流程

`check_image.py` 的 `validate_data` 对每条样本做三类检查，任何一类失败都把样本归入问题文件：

```text
对每条样本:
  检查① 媒体存在性: image(/images)、video(/videos) 字段归一成列表后,
          逐个 join(media_folder, 路径) 检查 os.path.exists
  检查② 占位符计数: 统计所有 human 轮 value 中 <image>/<video> 的出现次数,
          必须分别等于图像/视频文件数
  检查③ 答案纯净性: 任一 gpt 轮 value 含 <image> 或 <video> 即标记
全部通过 → 写入 *_valid.json; 否则附带 validation_problems 写入 *_problems.json
```

#### 4.3.3 源码精读

**① 真实视频标注。** [qwen-vl-finetune/demo/video.json:L2-L14](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/video.json#L2-L14) 是仓库自带的可训练视频样本：`video` 字段单值字符串、`<video>` 占位符置于问句开头、`gpt` 轮是较长的视频内容描述（洗车场景）。这正是「视频描述」类 SFT 数据的典型形态。

**② 媒体字段的宽容读取。** [qwen-vl-finetune/tools/check_image.py:L52-L63](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L52-L63) 用 `item.get("image", item.get("images", []))` 同时容忍单数/复数两种键名，再把字符串包成列表：

```python
media_info = {
    'image': item.get("image", item.get("images", [])),
    'video': item.get("video", item.get("videos", []))
}
```

**但要注意一个不对称**：训练加载器 `_build_messages` 只认单数键 `image`/`video`（L142、L146）。复数键 `images` 在校验器眼里是合法媒体，在训练器眼里却等于「没有媒体」——占位符一弹空池就失败。**写训练标注请一律用单数键**；`check_image.py` 的宽容只服务于外来开源数据的第一轮筛查。

**③ 三类检查的判定逻辑。** 占位符计数在 [qwen-vl-finetune/tools/check_image.py:L104-L123](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L104-L123)：human 轮用 `str.count` 累计 `<image>`/`<video>` 个数，与媒体数逐一比对，不一致记 `token_mismatches` 并附上期望/实际值。答案纯净性在 [L125-L130](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L125-L130)：gpt 轮含任一占位符记 `gpt_media_tokens`。媒体存在性在 [L74-L89](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L74-L89)，缺失文件的完整路径会被收进统计并抽样打印。

**④ 输出与汇总。** [qwen-vl-finetune/tools/check_image.py:L141-L163](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L141-L163) 把通过/失败样本分别写入 `<原名>_valid.json` 与 `<原名>_problems.json`（问题样本带 `validation_problems` 字段说明原因），并打印总量、媒体类型分布（image/video/mixed）、三类问题计数。**`*_valid.json` 就是你可以直接拿去注册训练的干净标注**——这是「校验即清洗」的工作流。

#### 4.3.4 代码实践

**实践目标**：对仓库自带的 demo 标注跑一次完整校验，读懂汇总报告。

1. 操作步骤（在 `qwen-vl-finetune` 目录下执行；依赖 `datasets` 与 `tqdm`，未安装请先 `pip install datasets tqdm`；运行结果**待本地验证**）：

```bash
python -c "
import sys; sys.path.insert(0, 'tools')
from check_image import validate_data
validate_data('demo/single_images.json', '.')          # 图像标注，媒体基准是仓库根
validate_data('demo/video.json', 'demo/videos')        # 视频标注，媒体基准是 demo/videos
"
```

2. 需要观察的现象：两条汇总报告的 Total/Valid 计数、Media type distribution、三类问题计数各是多少？
3. 预期结果：两份标注各 3 条样本，demo 媒体文件齐全、占位符数量正确，应得到 `Valid entries: 3 (100.0%)`，三类问题全为 0，并在 demo 目录下生成 `single_images_valid.json` 等输出文件。
4. 动手制造一个坏样本再跑一次：把 `video.json` 复制一份，删掉其中一条的 `<video>` 前缀，重新校验应看到 `token_mismatches: 1`，且 `_problems.json` 里该样本的 `validation_problems` 明确写着 `Expected 1 <video> tokens, found 0`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `check_image.py` 要单独统计 `media_types` 里的 `mixed`？
**答案**：一条样本同时带 `image` 和 `video` 字段时计入 mixed。框架允许图文混排（两个池独立消费、独立检查剩余），mixed 计数帮助你在拥抱开源多模态数据时快速了解混排样本的规模。

**练习 2**：`check_image.py` 的入口写死了 `example.json` 与 `media` 两个路径（[L165-L169](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L165-L169)）。不改源码的前提下怎么用它校验自己的文件？
**答案**：像 4.3.4 那样 `import validate_data` 直接调用函数，把路径作为参数传入——这正是它被写成独立函数而非脚本主逻辑的好处。

**练习 3**：视频标注里可以写 `"fps": 2` 之类的采样参数吗？
**答案**：不可以（写了也会被无视，消费方只读取 `image`/`video`/`conversations` 三个键）。采样行为由训练参数与 processor 决定（`--video_fps` 等），与标注解耦。

### 4.4 grounding 样本：bbox_2d JSON 与坐标体系

#### 4.4.1 概念说明

grounding（定位）样本教模型「把目标物体框出来」。它的标注结构仍是标准的单图样本，特殊之处全在 **gpt 轮的答案是 JSON 形式的坐标**。官方示例见 [qwen-vl-finetune/README.md:L106-L121](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L106-L121)：

```json
{
    "image": "demo/COCO_train2014_000000580957.jpg",
    "conversations": [
        {
            "from": "human",
            "value": "<image>\nLocate house in this image and output the bbox coordinates in JSON format."
        },
        {
            "from": "gpt",
            "value": "{\"bbox_2d\": [135, 114, 1016, 672]}"
        }
    ]
}
```

（注：README 原文 L117 里 `bbox_2d` 两侧的引号**未加转义**，直接照抄会得到非法 JSON；上文已替你补上 `\"`。自制数据时用 `json.dumps` 生成 value 可彻底避免这类转义错误。）

`bbox_2d` 是四元数组 `[x1, y1, x2, y2]`，即左上角与右下角坐标。坐标体系有历史演进，这也是 `tools/process_bbox.ipynb` 存在的原因：

| 代际 | 坐标体系 | 换算 |
| --- | --- | --- |
| Qwen2-VL | 0-1000 **相对坐标**（与原图分辨率无关） | `round(x / w * 1000)`，再夹到 [0,1000] |
| Qwen2.5-VL 起 | **绝对坐标**，基于 smart_resize 缩放后的图 | 先算缩放后宽高，再按比例缩放坐标 |

Qwen3-VL 的训练标注沿用绝对坐标的 `bbox_2d` JSON 输出（README 示例即此格式）。两套体系的换算函数就在 process_bbox 笔记本里，下面精读。

#### 4.4.2 核心流程

把外部检测标注（如 COCO 的绝对像素坐标）转成训练答案的流程：

```text
外部标注: bbox = [x1, y1, x2, y2]（原图绝对像素），原图高 h 宽 w
 ├─ 若目标是 Qwen2-VL 相对坐标:
 │    new = round(coord / 对应边长 * 1000)，再裁剪到 [0, 1000]
 ├─ 若目标是绝对坐标(Qwen2.5-VL 起):
 │    先用 smart_resize(h, w) 算出缩放后 (new_h, new_w)
 │    再按 new_w/w、new_h/h 分别缩放 x、y，裁剪到边界内
 └─ 拼成 gpt 轮 value: {"bbox_2d": [x1', y1', x2', y2']}
```

#### 4.4.3 源码精读

**① 相对坐标换算。** [qwen-vl-finetune/tools/process_bbox.ipynb](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/process_bbox.ipynb) 的第 3 个代码单元给出 Qwen2-VL 格式转换，笔记本自带的运行输出显示 `convert_to_qwen2vl_format([146, 137, 615, 415], 428, 640)` 得 `[228, 320, 961, 970]`——x 除以宽 640、y 除以高 428，各自乘 1000 取整并夹取。

**② 绝对坐标换算。** 同笔记本第 5 个代码单元复刻了 Qwen2.5-VL 的 `smart_resize`（与 u2-l2 精读的同名函数同源，默认 `factor=28`），先得到缩放后尺寸再等比缩放坐标，示例输出 `[147, 134, 619, 407]`。注意这里坐标随**训练时的像素预算**变化——`max_pixels`/`min_pixels` 不同，缩放后尺寸就不同，绝对坐标也应随之重算；这正是 grounding 数据制作比普通 QA 多一道工序的原因。

**③ demo 里的 grounding 素材。** `demo/images/COCO_train2014_000000580957.jpg` 真实存在，笔记本第 1 个代码单元正是用它演示「画框可视化」——用 cv2 把 `{"bbox_2d": [146, 137, 615, 415], "label": "bowl of carrots"}` 画回原图，先做校验再转格式，是处理自有检测数据的标准三步：**可视化抽查 → 选定坐标体系 → 批量转换**。

#### 4.4.4 代码实践

**实践目标**：用 `json.dumps` 生成一条完全合法的 grounding 样本，并手算一次坐标换算。

1. 操作步骤（无第三方依赖）：

```python
import json

# 用 dumps 生成 value，彻底规避 README 示例的转义问题
value = json.dumps({"bbox_2d": [135, 114, 1016, 672]})
sample = {
    "image": "demo/images/COCO_train2014_000000580957.jpg",
    "conversations": [
        {"from": "human", "value": "<image>\nLocate the bowl of carrots in this image and output the bbox coordinates in JSON format."},
        {"from": "gpt", "value": value},
    ],
}
print(json.dumps([sample], indent=2))   # 得到可直接保存训练的标注数组
```

2. 手算验证：一张 640×428 的图上有框 `[146, 137, 615, 415]`，按 Qwen2-VL 相对坐标公式算 `x1'`。
3. 需要观察的现象：`json.dumps` 的输出里内层引号是否已正确转义？
4. 预期结果：`x1' = round(146 / 640 * 1000) = 228`，与笔记本运行输出 `[228, 320, 961, 970]` 的首位一致；`json.dumps` 会输出 `"{\"bbox_2d\": [135, 114, 1016, 672]}"`，内层引号自动带 `\"` 转义。

#### 4.4.5 小练习与答案

**练习 1**：gpt 轮答案里的 `{"bbox_2d": ...}` 会被 `_build_messages` 特殊处理吗？
**答案**：不会。它就是普通文本，走 assistant 纯文本分支，模型学到的是「输出 JSON 形状的字符串」。解析这个 JSON 是推理后处理的事（u7-l3 会练）。

**练习 2**：同一张图、同一个框，`max_pixels` 从 A 档调到 B 档后，绝对坐标标注还能原样用吗？
**答案**：严格说不能。绝对坐标基于 smart_resize 缩放后的尺寸，预算变了缩放后尺寸就变，坐标需按新比例重算（process_bbox 笔记本第 5 单元的逻辑）。相对坐标（0-1000）则与预算无关。制作 grounding 数据前应先固定训练像素预算。

**练习 3**：为什么模型答案用 JSON 而不是自然语言（如「左上角大概三分之一处」）？
**答案**：JSON 结构化输出便于程序解析与自动评测（u6-l4 的 ODinW-13 评测就是解析 bbox JSON 算指标）；SFT 数据里统一用 JSON 答案，正是把这种输出格式「训」进模型的方式。

### 4.5 packed 样本：num_tokens、装箱与两层列表结构

#### 4.5.1 概念说明

packed（打包）样本不是新的任务类型，而是**把多条普通样本装进一个桶、当作一条样本来训练**的形态，目的是抹平长短样本的长度差异、提高 GPU 利用率（配合 u5-l5 的 flatten collator 与 u5-l6 的 varlen 注意力）。使用前提在 README 里一句话带过——[qwen-vl-finetune/README.md:L312](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L312)：`data_packing` 需要先用 `tools/pack_data.py` 预处理。

官方格式示例见 [qwen-vl-finetune/README.md:L123-L153](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L123-L153)：**顶层列表的每个元素本身又是一个列表**，内层列表里的每个对象都是一条你在 4.1 学过的普通样本：

```json
[
    [
        { "image": "images/001.jpg", "conversations": [ ... ] },
        { "image": "images/002.jpg", "conversations": [ ... ] }
    ]
]
```

即「桶的数组 → 样本的数组 → 样本对象」三层嵌套。

#### 4.5.2 核心流程

`pack_data.py` 的主流程：

```text
对每个数据集:
  1 若存在 *_count.json 缓存 → 直接读(跳过昂贵的解码)
  2 否则逐条计算 num_tokens:
      21(基础开销) + 每轮对话 apply_chat_template 的 token 数
                  + 每张图 image_grid_thw.prod() // 4
                  + 每条视频 video_grid_thw.prod() // 4
      结果写回 *_count.json 缓存
  3 按 256 条一批做恒定体积装箱(装到不超过 pack_length=4096 token)
  4 弹出临时的 num_tokens 字段, 把每个桶存成内层列表
  5 全部桶写入 *_pack.json
```

第 2 步里的 `prod() // 4` 就是 u2-l1 讲过的 **2×2 空间合并**：patch 数除以 4 才是进入 LLM 的视觉 token 数。\[ \text{num\_tokens} \approx 21 + \sum_{\text{轮}} \text{len(template(轮))} + \sum_{\text{图}} \frac{t \cdot h \cdot w}{4} + \sum_{\text{视频}} \frac{t \cdot h \cdot w}{4} \]

#### 4.5.3 源码精读

**① token 数预估。** [qwen-vl-finetune/tools/pack_data.py:L86-L103](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L86-L103)：基础值取 21（聊天模板的控制 token 开销），每轮对话单独过一遍 `apply_chat_template` 计数；图像走 `process_image`——真实打开图片、跑一遍 image processor、用 `image_grid_thw.prod() // 4` 得视觉 token（[L59-L67](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L59-L67)）；视频走 `process_video`，用 torchcodec 均匀抽帧后同样计数（[L69-L83](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L69-L83)）。**这是「用 processor 真实估算」而非按字符数瞎猜**，所以代价是要解码媒体——也因此第 2 步的结果必须缓存到 `*_count.json`（[L153-L167](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L153-L167)），避免重复解码。

**② 装箱与输出。** [qwen-vl-finetune/tools/pack_data.py:L106-L122](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L106-L122)：用 `binpacking.to_constant_volume` 按 `pack_length` 装箱，输入是 `(原下标, num_tokens)` 对；装完后 `new_data.pop("num_tokens", None)` 把临时字段弹掉，**每个分组 collect 成 `group_data` 列表再 append**——这就是两层列表结构的诞生处。主循环（[L169-L191](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L169-L191)）固定 `pack_length = 4096`、批大小 256，最终写出 `*_pack.json`。

**③ 训练侧如何识别 packed 样本。** 加载标注时，[qwen-vl-finetune/qwenvl/data/data_processor.py:L285-L291](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L285-L291) 用 `isinstance(ann, list)` 区分两种形态：普通样本（dict）直接注入 `data_path`；packed 样本（list）则**遍历内层逐条注入**。训练时 `_get_packed_item`（[L450-L459](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L450-L459)）对桶内每条样本递归调用单样本处理，再把 `input_ids`/`labels`/`position_ids`/`pixel_values` 沿序列维拼接成一个长样本（[L461-L472](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L461-L472)），`attention_mask` 变成各段长度的列表——这正是 u5-l5/u5-l6 的伏笔。

**④ 一个必须避开的坑。** 开启 `--data_packing True` 后，`item_fn` 切换为 `_get_packed_item`（[L304-L307](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L304-L307)）。按源码（L444-L448），此时若标注里混入**未打包的 dict 样本**，会走进引用未定义变量的分支抛出异常，随后被 `__getitem__` 的重试机制当坏样本跳过。所以 packed 标注应当**整份**都是 `pack_data.py` 产出的两层列表结构，不要与普通标注混用同一份数据集。

#### 4.5.4 代码实践

**实践目标**：确认 packed 标注的两层结构，并理解 `num_tokens` 的角色。

1. 操作步骤（源码阅读 + 结构检查，无需 GPU）：先精读 [pack_data.py:L106-L122](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L106-L122)，然后对任意一份打包产物（若无，可手工构造一个假想文件）验证结构：

```python
import json

# 示例代码：手工构造一个最小的 packed 标注并验证结构
packed = [
    [
        {"image": "demo/images/10095.png",
         "conversations": [{"from": "human", "value": "Q1\n<image>"},
                           {"from": "gpt", "value": "A1"}]},
        {"image": "demo/images/10149.png",
         "conversations": [{"from": "human", "value": "Q2\n<image>"},
                           {"from": "gpt", "value": "A2"}]},
    ]
]
json.dump(packed, open("fake_pack.json", "w"))

data = json.load(open("fake_pack.json"))
assert isinstance(data, list) and isinstance(data[0], list)   # 两层结构
assert all(isinstance(s, dict) for s in data[0])              # 桶内是普通样本
assert not any("num_tokens" in s for s in data[0])            # 临时字段已弹出
print("结构合法, 第一个桶含", len(data[0]), "条样本")
```

2. 需要观察的现象：三层断言是否全部通过？
3. 预期结果：全部通过。第一个桶含 2 条样本，且样本内没有 `num_tokens` 字段（装箱后被 pop 掉）。
4. 思考并回答：为什么 `num_tokens` 要先写进 `*_count.json` 缓存、装箱前又从样本里 pop 掉？
5. 预期答案：写入缓存是因为计算 num_tokens 需要真实解码图像/视频跑 processor，代价高，缓存后重跑打包不必重算；pop 掉是因为它只是装箱用的临时元数据，训练消费方（`_build_messages`）只认 `image`/`video`/`conversations` 三个键，留着无害但污染标注。

#### 4.5.5 小练习与答案

**练习 1**：为什么图像 token 数是 `image_grid_thw.prod() // 4` 而不是 `prod()`？
**答案**：`grid_thw` 数的是 ViT 的 patch 数；进入 LLM 前要做 2×2 空间合并（u2-l1），4 个 patch 合成 1 个视觉 token，所以除以 4。

**练习 2**：`pack_length` 固定 4096，训练脚本的 `--model_max_length` 设成 2048 会怎样？
**答案**：桶内总 token 可能超过模型最大长度，超出部分在训练中会被截断，桶尾样本的监督信号不完整。装箱长度应与 `model_max_length`（以及显存预算）对齐。

**练习 3**：packed 样本里的每条子样本还各自遵守占位符配对规则吗？
**答案**：遵守。`_get_packed_item` 对桶内每条样本**递归调用单样本处理**，配对、报错、路径解析规则与 4.1/4.2 完全一致；打包只是外层套桶，不改变样本内部语义。

## 5. 综合实践

**任务**：亲手制作一份属于你自己的单图 SFT 数据集并完成校验，走通「写标注 → 校验 → 产出干净标注」的完整链路。

1. **准备素材**：挑 5 张你自己的图片（截图、照片皆可）放入某个目录，例如 `~/my_data/images/`。

2. **编写标注**：仿照 [demo/single_images.json](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/single_images.json) 的格式，每张图写一句问答，保存为 `~/my_data/my_dataset.json`。注意三个规范点：`image` 用单数键；每条 human 轮恰好 1 个 `<image>`；gpt 轮答案里不出现任何占位符。示例结构：

```json
[
  {
    "image": "images/cat.jpg",
    "conversations": [
      {"from": "human", "value": "<image>\n这只动物在做什么？"},
      {"from": "gpt", "value": "一只橘猫正趴在窗台上晒太阳。"}
    ]
  }
]
```

（其余 4 条照此扩展；中文问答完全可用。）

3. **校验（两种途径任选）**：

   - **途径 A（推荐，需 `pip install datasets tqdm`，结果待本地验证）**：用官方校验器——

   ```bash
   python -c "
   import sys; sys.path.insert(0, 'tools')
   from check_image import validate_data
   validate_data('/home/你/my_data/my_dataset.json', '/home/你/my_data')
   "
   ```

   - **途径 B（零依赖，示例代码）**：手写最小校验脚本，覆盖与官方一致的三类检查——

   ```python
   import json, os

   ANNO, MEDIA = "/home/你/my_data/my_dataset.json", "/home/你/my_data"
   for i, s in enumerate(json.load(open(ANNO))):
       imgs = s.get("image", [])
       imgs = [imgs] if isinstance(imgs, str) else imgs
       n_ph = sum(c["value"].count("<image>") for c in s["conversations"]
                  if c["from"] == "human")
       bad_ans = any("<image>" in c["value"] or "<video>" in c["value"]
                     for c in s["conversations"] if c["from"] == "gpt")
       assert all(os.path.exists(os.path.join(MEDIA, p)) for p in imgs), f"样本{i}: 文件缺失"
       assert n_ph == len(imgs), f"样本{i}: 占位符 {n_ph} != 图像数 {len(imgs)}"
       assert not bad_ans, f"样本{i}: 答案含占位符"
   print("5 条样本全部合法")
   ```

4. **观察与预期**：官方校验器应报告 `Valid entries: 5 (100.0%)`、三类问题全 0，并在标注同目录生成 `my_dataset_valid.json`；自写脚本应打印全部合法。若失败，按 `_problems.json` 里的 `validation_problems` 或断言信息逐条修复。

5. **收尾**：把 `my_dataset_valid.json` 和 `data_path`（你的 `my_data` 目录）记下来——下一讲 u4-l3 就把它注册进 `data_dict`，变成训练可用的数据集；本讲的成果是 u7-l4 端到端微调项目的第一步。

## 6. 本讲小结

- 训练标注 = `image`/`video`（路径，**单值或列表皆可**，源码在 `_build_messages` 里归一化）+ `conversations`（`human`/`gpt` 交替的问答），占位符 `<image>`/`<video>` 只允许出现在 human 轮，位置自由。
- 占位符匹配是**先进先出的全局配对**：第 k 个占位符消费路径列表的第 k 个媒体；数量不配对（多或少）都会抛 `ValueError`，坏样本在训练中被 `__getitem__` 重试机制**静默跳过**——所以训练前必须校验。
- 标注路径**相对于数据集的 `data_path` 解析**（`Path(data_path) / 路径` 后 `resolve()`），`data_path` 留空则相对启动目录；demo 的两份标注恰好演示了两种基准写法。
- 视频标注与图像标注完全同构，采样参数不在标注里，与训练配置解耦；`check_image.py` 做媒体存在性、占位符计数、答案纯净性三类检查，产出可直接训练的 `*_valid.json`。
- grounding 样本的答案是 `{"bbox_2d": [x1, y1, x2, y2]}` JSON 文本（绝对坐标体系，Qwen2-VL 时代是 0-1000 相对坐标，`process_bbox.ipynb` 负责换算）；packed 样本是「桶数组 → 样本数组 → 样本」两层列表，由 `pack_data.py` 按 `num_tokens` 装箱产出，训练侧递归处理后再拼接。

## 7. 下一步学习建议

本讲解决了「数据长什么样」，下一讲 **u4-l3 数据集注册表与采样率控制** 解决「数据怎么被找到」：把你在综合实践里做好的 `my_dataset.json` 注册进 `qwenvl/data/__init__.py` 的 `data_dict`，理解 `annotation_path`/`data_path` 的分工（本讲 4.2.3 已埋下伏笔）与 `数据集名%X` 采样率语法。之后再进入 u4-l4 深挖 `pack_data.py` 的装箱细节。若你想先弄清「占位符最终如何变成 `input_ids` 里的 `<|image_pad|>`、哪些 token 参与损失计算」，可以直接跳读 u5-l3 的 `preprocess_qwen_visual` 标签掩码部分，本讲 4.1.3 的第 ③ 步是它的入口。

# 图像链路：fetch_image 与 smart_resize 像素预算

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立实现与 `round_by_factor` / `ceil_by_factor` / `floor_by_factor` 等价的对齐取整函数，并能说清「缩小取 floor、放大取 ceil」背后的方向性设计。
2. 逐行读懂 `smart_resize` 的三重约束——宽高被 `factor` 整除、总像素落在 `[min_pixels, max_pixels]` 区间、尽可能保持长宽比——并能手算任意输入的输出尺寸。
3. 掌握像素预算与视觉 token 数的换算：`min_pixels = T × 32 × 32` 的含义是「这张图最多（或至少）占 T 个视觉 token」，理解 32 倍空间压缩的来源。
4. 会用 `resized_height` / `resized_width` 绕过预算精确直控输出尺寸，并知道这条路径的边界与陷阱。

本讲只讲**图像**：从 `fetch_image` 的多来源加载，到 `smart_resize` 的预算算法。视频帧链路（`fetch_video` / `smart_nframes`）留给 u2-l3。

## 2. 前置知识

### 2.1 承接 u2-l1：patch、merge 与对齐因子

u2-l1 已建立的核心事实，本讲直接使用：

- ViT 把图切成边长为 \(p\) 的 patch（Qwen3-VL \(p=16\)），再经 \(2\times2\) 空间合并，1 个视觉 token 覆盖 \(2p \times 2p\) 的像素区域。
- 因此 `fetch_image` 内部换算出**对齐因子** \(\text{factor} = 2p\)：Qwen2-VL/2.5-VL 为 28，Qwen3-VL 为 32。
- 输出图像的宽高都必须是 factor 的整数倍，视觉 token 数为：

\[
N_{\text{token}} = \frac{H}{\text{factor}} \times \frac{W}{\text{factor}}
\]

### 2.2 PIL Image 的宽高顺序陷阱

本讲会反复在两种「宽高顺序」之间切换，读源码前必须先建立这个意识：

- `PIL.Image.size` 返回 **(宽, 高)**；`image.resize((宽, 高))` 也以宽为先。
- `smart_resize(height, width, ...)` 的参数与返回值都是 **(高, 宽)** 顺序（高在前）。

`fetch_image` 内部正是做了这次翻译（[vision_process.py:L130](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L130) 解包为 `width, height`，[L140](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L140) 又以 `(resized_width, resized_height)` 传回 resize）。自己写实验代码打印 `img.size` 时务必记住：**第一个数是宽**。

### 2.3 为什么需要「像素预算」

直接把原图喂给模型有三个问题，`smart_resize` 的三重约束逐一回应：

| 问题 | 来源 | smart_resize 的应对 |
| --- | --- | --- |
| 宽高不是 factor 整数倍，patch 网格切不齐 | ViT 按固定 patch 切图 | 宽高对齐到 factor 的倍数 |
| 大图产生海量视觉 token，撑爆上下文 | 上下文长度是稀缺资源 | 总像素 ≤ max_pixels |
| 小图 token 太少，细节信息不足 | 视觉表征过于粗糙 | 总像素 ≥ min_pixels |

同时，为了让模型看到的画面「形状不变形」，缩放过程尽量等比。**预算以像素为单位定义，但真正的稀缺资源是 token**——这就是为什么预算常写成 `T × 32 × 32` 的形式（见 4.4）。

## 3. 本讲源码地图

| 文件 | 本讲关注的部分 | 作用 |
| --- | --- | --- |
| [qwen-vl-utils/src/qwen_vl_utils/vision_process.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py) | L24-L27 常量、L41-L53 取整函数、L56-L81 smart_resize、L84-L90 to_rgb、L93-L141 fetch_image | 本讲全部核心逻辑 |
| [README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md) | L477-L532 图像尺寸控制、L348-L356 processor 侧预算 | 官方文档对 min_pixels / max_pixels / resized_* 的说明 |
| [qwen-vl-utils/src/qwen_vl_utils/\_\_init\_\_.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/__init__.py) | L1-L7 | 包级导出：`fetch_image` 与 `smart_resize` 可直接 `from qwen_vl_utils import` 使用 |

注意：包级只导出 5 个函数（[\_\_init\_\_.py:L1-L7](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/__init__.py#L1-L7)）。三个取整函数**不在**其中，实验时需要 `from qwen_vl_utils.vision_process import round_by_factor, ...` 从子模块导入。

## 4. 核心概念与源码讲解

### 4.1 fetch_image：四种图像来源的统一加载

#### 4.1.1 概念说明

`fetch_image` 是「一个视觉元素字典 → 一张尺寸合规的 RGB PIL 图像」的完整流水线，分两段：

1. **加载段**：把五花八门的图像来源（PIL 对象、本地路径、`file://`、HTTP/HTTPS URL、base64 数据 URL）统一变成一个 PIL Image 对象。
2. **缩放段**：按预算（或直控尺寸）调用 `smart_resize` 并执行 `resize`（缩放段见 4.3 与 4.5，本节专注加载段）。

为什么要支持这么多来源？因为真实业务里图像可能躺在磁盘、对象存储（URL）、前端上传的 base64 里，或是上游算法直接产出的内存对象。统一入口让上层 `process_vision_info` 无需关心来源差异。

#### 4.1.2 核心流程

```text
fetch_image(ele, image_patch_size)
  1. 取数据：ele["image"]，旧写法回退到 ele["image_url"]
  2. patch_factor = image_patch_size × SPATIAL_MERGE_SIZE   # 16→32
  3. 按类型加载（先判断 PIL，再按字符串前缀分发）：
       PIL.Image 实例      → 直接使用
       http:// / https://  → requests 流式下载 → BytesIO → Image.open → deepcopy
       file://             → 去掉前 7 个字符后按本地路径打开
       data:image…base64,  → 切出 base64 段 → b64decode → BytesIO → deepcopy
       其他字符串          → 视为本地路径 Image.open
     加载失败/类型无法识别 → ValueError
  4. to_rgb：RGBA 贴白底合成，其余统一 convert("RGB")
  5. （缩放段，见 4.3 / 4.5）
```

#### 4.1.3 源码精读

数据键兼容与对齐因子在 [vision_process.py:L93-L100](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L93-L100)，这段代码先取图像数据并把 patch 边长换算成对齐因子：

```python
def fetch_image(ele, image_patch_size: int = 14):
    if "image" in ele:
        image = ele["image"]
    else:
        image = ele["image_url"]        # 兼容旧版 OpenAI 风格的 image_url 键
    image_obj = None
    patch_factor = int(image_patch_size * SPATIAL_MERGE_SIZE)   # 14→28，16→32
```

四路来源的分发逻辑在 [vision_process.py:L101-L119](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L101-L119)，这段代码把不同来源统一成 PIL 对象：

```python
if isinstance(image, Image.Image):                  # ① 内存中的 PIL 对象
    image_obj = image
elif image.startswith("http://") or image.startswith("https://"):
    with requests.get(image, stream=True) as response:
        response.raise_for_status()
        with BytesIO(response.content) as bio:
            image_obj = copy.deepcopy(Image.open(bio))   # ② 网络图：下载后立即深拷贝
elif image.startswith("file://"):
    image_obj = Image.open(image[7:])               # ③ 去掉 "file://" 7 个字符
elif image.startswith("data:image"):
    if "base64," in image:
        _, base64_data = image.split("base64,", 1)
        data = base64.b64decode(base64_data)
        with BytesIO(data) as bio:
            image_obj = copy.deepcopy(Image.open(bio))   # ④ base64 数据 URL
else:
    image_obj = Image.open(image)                   # ⑤ 兜底：本地路径
if image_obj is None:
    raise ValueError(f"Unrecognized image input, ...")
```

三个容易被忽略的细节：

- **②④ 为什么 `deepcopy`，③⑤ 却不用**：PIL 是惰性解码的，`Image.open` 并不立即读像素。②④ 的图像数据挂在 `BytesIO` 上，`with` 块退出后流就关了；`deepcopy` 强制在流关闭前把像素读进内存。③⑤ 打开的是文件，文件句柄由 PIL 持有，惰性读取仍然安全。
- **④ 的 `split("base64,", 1)`**：data URL 形如 `data:image/jpeg;base64,xxxx`，按第一个 `base64,` 切开，逗号后才是真正的 base64 载荷，且 `maxsplit=1` 保证载荷内部再出现该子串也不受影响。
- **判断顺序**：先 `isinstance` 再前缀匹配，所以传入 PIL 对象时后续 `startswith` 分支自然跳过。

颜色统一在 [vision_process.py:L84-L90](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L84-L90)（`fetch_image` 在 [L120](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L120) 调用它），这段代码把任意模式的图统一成三通道 RGB：

```python
def to_rgb(pil_image):
    if pil_image.mode == 'RGBA':
        white_background = Image.new("RGB", pil_image.size, (255, 255, 255))
        white_background.paste(pil_image, mask=pil_image.split()[3])  # alpha 通道当掩码
        return white_background          # 透明区域落在白底上
    else:
        return pil_image.convert("RGB")
```

RGBA 不是简单丢掉 alpha 通道，而是**贴白底合成**——否则透明 PNG 直接转 RGB 会把透明区域变成黑色，语义就错了。

#### 4.1.4 代码实践

**实践目标**：用同一张图构造四种来源，验证加载段的输出完全一致。

**操作步骤**（示例代码，除可选的 URL 外完全离线）：

```python
# sources_test.py
import base64, io
from PIL import Image
from qwen_vl_utils import fetch_image          # fetch_image 在包级导出中

img = Image.new("RGB", (1600, 1000), color=(30, 144, 255))
img.save("/tmp/demo.jpg")                      # 顺便落盘，供路径类来源使用

buf = io.BytesIO()
img.save(buf, format="JPEG")
b64_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

variants = {
    "PIL 对象":    {"image": img},
    "本地路径":    {"image": "/tmp/demo.jpg"},
    "file:// 前缀": {"image": "file:///tmp/demo.jpg"},
    "base64":     {"image": b64_url},
    "image_url 键": {"image_url": "/tmp/demo.jpg"},   # 旧写法同样有效
}
for name, ele in variants.items():
    out = fetch_image(ele, image_patch_size=16)
    print(f"{name:12s} -> size={out.size}（宽,高）, mode={out.mode}")
```

**需要观察的现象**：五行输出的 `size` 与 `mode` 完全相同；`mode` 恒为 `RGB`。

**预期结果**：全部输出 `size=(1600, 992), mode=RGB`（尺寸推导见 4.3.4 的手算；JPEG 保存不改变宽高）。（数值待本地验证）

**可选扩展**：把其中一个来源换成 README 官方示例图 `https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen-VL/assets/demo.jpeg`（[README.md:L500](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L500)），验证 URL 分支（需网络）。再给 `img` 换成 `Image.new("RGBA", (1600, 1000), (30,144,255,128))` 存成 PNG 走一遍，观察输出 `mode` 仍是 `RGB`——白底合成生效。

#### 4.1.5 小练习与答案

**练习 1**：`{"type": "image", "image_url": "/tmp/demo.jpg"}`（只有 `image_url` 键没有 `image` 键）能被 `fetch_image` 处理吗？
**答案**：能。`if "image" in ele: ... else: image = ele["image_url"]`（[L94-L97](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L94-L97)）——`image_url` 是历史遗留的兼容键，两者取其一即可。

**练习 2**：为什么 URL 分支要先 `requests.get(..., stream=True)` 拿到 `response.content` 再 `BytesIO`，而不是直接写临时文件？
**答案**：图像本体已在响应字节里，`BytesIO` 把它包装成内存中的类文件对象供 `Image.open` 使用，避免磁盘 IO；`stream=True` 让 requests 按流接收响应体，配合 `with` 及时释放连接。（深层原因：PIL 惰性解码，所以还必须 `deepcopy` 强制在 `with` 退出前读完像素——见 4.1.3。）

**练习 3**：一张带 alpha 通道的 PNG 图标，如果不做白底合成而直接 `convert("RGB")`，画面会变成什么样？
**答案**：PIL 的 `convert("RGB")` 会直接丢弃 alpha 通道，原本透明的区域通常呈现黑色（RGB 全 0）；`to_rgb` 的白底合成让它呈现白色，更符合「图显示在白纸上」的直觉，与 ViT 预训练时看到的数据分布也更一致。

---

### 4.2 对齐取整函数：round_by_factor / ceil_by_factor / floor_by_factor

#### 4.2.1 概念说明

「宽高必须是 factor 的整数倍」这条约束需要三个基本工具：把任意数变成 factor 倍数的三种取整方式——就近、向上、向下。它们是 `smart_resize` 的原子操作，也是 u2-l3 中视频帧数对齐（`FRAME_FACTOR`）的同一套工具。

三个函数的语义：

| 函数 | 语义 | 数学表达 |
| --- | --- | --- |
| `round_by_factor(x, f)` | 最接近 x 的 f 的倍数 | \(\mathrm{round}(x/f)\cdot f\) |
| `ceil_by_factor(x, f)` | ≥ x 的最小 f 倍数 | \(\lceil x/f\rceil\cdot f\) |
| `floor_by_factor(x, f)` | ≤ x 的最大 f 倍数 | \(\lfloor x/f\rfloor\cdot f\) |

一个关键的设计意识：**取整方向是有语义的**。在预算约束里，「缩小」时用 floor（宁可略小于预算，绝不超出上限），「放大」时用 ceil（务必达到下限）。这个配对会在 4.3 反复出现。

#### 4.2.2 核心流程

```text
输入 number（可以是浮点数，smart_resize 会传入 h/β 这类浮点值）
  round 路径：round(number / factor) * factor
  ceil  路径：math.ceil(number / factor) * factor
  floor 路径：math.floor(number / factor) * factor
输出 factor 的整数倍
```

注意：类型注解写的是 `int`，但 Python 鸭子类型下传浮点数完全合法——`smart_resize` 的缩放分支正是传浮点进来（见 4.3.3）。

#### 4.2.3 源码精读

三个函数在 [vision_process.py:L41-L53](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L41-L53)，这段代码是本讲算法的全部数学原语：

```python
def round_by_factor(number: int, factor: int) -> int:
    """Returns the closest integer to 'number' that is divisible by 'factor'."""
    return round(number / factor) * factor

def ceil_by_factor(number: int, factor: int) -> int:
    """Returns the smallest integer >= 'number' that is divisible by 'factor'."""
    return math.ceil(number / factor) * factor

def floor_by_factor(number: int, factor: int) -> int:
    """Returns the largest integer <= 'number' that is divisible by 'factor'."""
    return math.floor(number / factor) * factor
```

一个隐藏知识点：Python 3 的 `round` 是**银行家舍入**（round half to even），恰好在 `.5` 处不进位到「更大的数」，而是取偶数。所以 `round_by_factor(16, 32) = round(0.5)*32 = 0*32 = 0`（0 是偶数），而 `round_by_factor(48, 32) = round(1.5)*32 = 2*32 = 64`（2 是偶数）。返回 0 的情形由 `smart_resize` 里的 `max(factor, ...)` 兜底（见 4.3.3），但自己复用这三个函数时要当心。

#### 4.2.4 代码实践

**实践目标**：建立三个函数的手算直觉，并亲眼看到银行家舍入行为。

**操作步骤**（示例代码）：

```python
# rounding_test.py
from qwen_vl_utils.vision_process import (
    round_by_factor, ceil_by_factor, floor_by_factor,
)

F = 32
for x in (1000, 420, 48, 16):
    print(f"x={x:>5}  round={round_by_factor(x, F):>5}  "
          f"ceil={ceil_by_factor(x, F):>5}  floor={floor_by_factor(x, F):>5}")

# 浮点输入：smart_resize 内部的真实用法
print(floor_by_factor(404.77, F), floor_by_factor(647.64, F))
print(round(0.5), round(1.5), round(2.5))   # 银行家舍入：0 2 2
```

**需要观察的现象**：

1. `1000 → round/floor 均为 992、ceil 为 1024`（1000/32 = 31.25）。
2. `16 → round/floor 均为 0`、`48 → round/ceil 均为 64`——`.5` 恰好落在边界时，round 的方向由「取偶」决定。
3. 浮点输入正常工作，输出仍是 32 的倍数。

**预期结果**：

| x | x/32 | round | ceil | floor |
| --- | --- | --- | --- | --- |
| 1000 | 31.25 | 992 | 1024 | 992 |
| 420 | 13.125 | 416 | 448 | 416 |
| 48 | 1.5 | 64 | 64 | 32 |
| 16 | 0.5 | 0 | 32 | 0 |

随后打印 `384 640` 与 `0 2 2`。（待本地验证）

#### 4.2.5 小练习与答案

**练习 1**：`round_by_factor(1000, 28)` 等于多少？（Qwen2.5-VL，factor=28）
**答案**：1000/28 ≈ 35.714，round = 36 → 36×28 = 1008。这正是 u2-l1 里 patch=14 时输出高为 1008 的出处。

**练习 2**：为什么「缩小预算」配 floor、「放大预算」配 ceil，而不是反过来？
**答案**：约束的方向性决定取整方向。max_pixels 是**不得超出的上限**，若用 ceil 可能在取整后反超预算；min_pixels 是**必须达到的下限**，若用 floor 可能仍然不达标。floor 保守地留在预算内、ceil 保守地越过下限，各取其「不会违反约束」的方向。

**练习 3**：`floor_by_factor(-5, 32)` 会返回什么？这会造成问题吗？
**答案**：`math.floor(-5/32) = -1`，返回 -32。负数倍数在数学上成立但语义荒谬；`smart_resize` 的输入来自真实图像宽高（恒正）且 `max(factor, ...)` 兜底，所以库内不会触发，但自己复用时别喂负数。

---

### 4.3 smart_resize：三重约束与长宽比保持算法

#### 4.3.1 概念说明

`smart_resize` 是整条图像链路的大脑：给定原始 (高, 宽) 和预算，输出一个**合规**的新尺寸。它要在三个互相牵制的目标间求平衡：

1. **整除性**：新宽高都能被 factor 整除（patch 网格切得齐）。
2. **预算性**：总像素落在 `[min_pixels, max_pixels]` 内（token 数受控）。
3. **保形性**：长宽比尽量不变（画面不变形）。

算法策略可以概括为「**先就近对齐，越界再等比缩放**」：先把宽高各自就近取整到 factor 倍数（此时长宽比几乎不变）；如果取整后的总像素超出预算，再用一个等比缩放系数 β 把尺寸整体拉回预算区间，并朝安全方向取整。

等比缩放系数 β 的推导：设原图面积 \(S = h \times w\)，目标是把面积变成 \(T\)（预算值），等比缩放系数 β 满足：

\[
\frac{h}{\beta} \times \frac{w}{\beta} = \frac{S}{\beta^2} = T
\quad\Longrightarrow\quad
\beta = \sqrt{\frac{S}{T}} \;\;(\text{缩小时})，\qquad
\beta = \sqrt{\frac{T}{S}} \;\;(\text{放大时})
\]

缩小（\(S > T\)）时 β > 1，放大（\(S < T\)）时 β < 1——源码里两个分支的 β 表达式正是这样写的。

#### 4.3.2 核心流程

```text
smart_resize(h, w, factor, min_pixels=None, max_pixels=None)
  0. 预算缺省：max ← 16384·factor²，min ← 4·factor²（以 token 数定义的常量 × factor²）
     断言 max_pixels ≥ min_pixels，否则 AssertionError
  1. 长宽比闸门：max(h,w)/min(h,w) > 200 → ValueError（拒绝极端长条图）
  2. 就近对齐：h_bar ← max(factor, round_by_factor(h, factor))，w_bar 同理
     （max(factor, ·) 保证最小尺寸为一个 factor，避免 round 出 0）
  3. 三分支判断（注意 if/elif 的优先级：超上限优先于低下限）：
     ① h_bar·w_bar > max_pixels（超上限）：
          β ← sqrt(h·w / max_pixels)
          h_bar ← floor_by_factor(h/β, factor)   # floor：不超上限
          w_bar ← floor_by_factor(w/β, factor)
     ② h_bar·w_bar < min_pixels（低于下限）：
          β ← sqrt(min_pixels / (h·w))
          h_bar ← ceil_by_factor(h·β, factor)    # ceil：达到下限
          w_bar ← ceil_by_factor(w·β, factor)
     ③ 预算内：保持第 2 步的就近对齐结果
  4. 返回 (h_bar, w_bar)   # 高在前！单遍执行，不再迭代复查
```

**重要**：这是一次性变换，不循环。分支执行完后即使结果略越界（例如 floor 之后略低于 min_pixels）也不会再修正——4.4.4 的实践会亲眼看到这一点。

#### 4.3.3 源码精读

完整实现在 [vision_process.py:L56-L81](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L56-L81)，docstring 明确写着三条约束，这段代码是本讲的 centerpiece：

```python
def smart_resize(height, width, factor, min_pixels=None, max_pixels=None):
    """Rescales the image so that the following conditions are met:
    1. Both dimensions are divisible by 'factor'.
    2. The total number of pixels is within ['min_pixels', 'max_pixels'].
    3. The aspect ratio is maintained as closely as possible."""
    max_pixels = max_pixels if max_pixels is not None else (IMAGE_MAX_TOKEN_NUM * factor ** 2)
    min_pixels = min_pixels if min_pixels is not None else (IMAGE_MIN_TOKEN_NUM * factor ** 2)
    assert max_pixels >= min_pixels, "The max_pixels of image must be >= min_pixels."
    if max(height, width) / min(height, width) > MAX_RATIO:        # 200:1 闸门
        raise ValueError(f"absolute aspect ratio must be smaller than {MAX_RATIO}, ...")
    h_bar = max(factor, round_by_factor(height, factor))           # 就近对齐 + 最小尺寸兜底
    w_bar = max(factor, round_by_factor(width, factor))
    if h_bar * w_bar > max_pixels:                                 # ① 超上限 → 等比缩小
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = floor_by_factor(height / beta, factor)             # floor 保守
        w_bar = floor_by_factor(width / beta, factor)
    elif h_bar * w_bar < min_pixels:                               # ② 低于下限 → 等比放大
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = ceil_by_factor(height * beta, factor)              # ceil 保守
        w_bar = ceil_by_factor(width * beta, factor)
    return h_bar, w_bar
```

五个值得咀嚼的细节：

- **预算缺省值以 token 定义**（[L26-L27](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L26-L27) 的 `IMAGE_MIN_TOKEN_NUM = 4`、`IMAGE_MAX_TOKEN_NUM = 16384`）：缺省 min/max 像素 = 4/16384 个 token × factor²，体现「token 才是稀缺资源」的设计观（详见 4.4）。
- **`assert max_pixels >= min_pixels`**：调用方把两个预算写反不会得到静默的错误行为，而是直接 AssertionError——快速失败。
- **β 用原始 h·w 计算，比较却用取整后的 h_bar·w_bar**：分支判断看「对齐后是否越界」，缩放基准却是原始尺寸，两者之间允许有细微不一致——工程上够用即可，不追求严格最优。
- **缩小分支查的是 `> max_pixels` 且优先于 `< min_pixels`**：当 min == max（锁定 token 数）时，几乎必然走缩小分支，最终结果略低于目标值（floor 所致）。
- **`max(factor, ...)` 的兜底**：极小图（如 10×10）经 `round_by_factor` 可能得到 0，`max(factor, 0) = factor` 保证最小输出为一个 factor 见方（1 个 patch-merge 单位）。

`fetch_image` 对 `smart_resize` 的两处调用点：预算路径在 [vision_process.py:L129-L139](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L129-L139)（默认预算从 `ele` 的 `min_pixels`/`max_pixels` 键读取），直控路径在 [L123-L128](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L123-L128)（见 4.5）。最终执行缩放与返回在 [L140-L141](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L140-L141)，这一行把 (高, 宽) 结果翻译回 PIL 的 (宽, 高) 顺序：

```python
image = image.resize((resized_width, resized_height))
return image
```

#### 4.3.4 代码实践

**实践目标**：把三个分支（预算内 / 超上限 / 低于下限）与长宽比闸门各触发一次，手算与程序对照。

**操作步骤**（示例代码，直接调用导出的 `smart_resize`，完全离线）：

```python
# smart_resize_test.py
from qwen_vl_utils import smart_resize

# ① 预算内：只做就近对齐（1600×1000，缺省预算 4096~16,777,216 px）
print(smart_resize(1000, 1600, factor=32))                        # 期望 (992, 1600)

# ② 超上限：等比缩小 + floor
print(smart_resize(1000, 1600, factor=32, max_pixels=1280*32*32)) # 期望 (896, 1440)

# ③ 低于下限：等比放大 + ceil（10×10 小图，缺省下限 4 token = 4096 px）
print(smart_resize(10, 10, factor=32))                            # 期望 (64, 64)

# ④ 极端长宽比：报错
try:
    smart_resize(10, 10000, factor=32)                            # 1000:1 > 200
except ValueError as e:
    print("ValueError:", e)

# ⑤ 预算写反：断言失败
try:
    smart_resize(1000, 1600, factor=32, min_pixels=1280*32*32, max_pixels=256*32*32)
except AssertionError as e:
    print("AssertionError:", e)
```

**手算过程**（输入高 1000、宽 1600，factor = 32）：

- **① 预算内**：`round_by_factor(1000, 32) = round(31.25)×32 = 31×32 = 992`；`round_by_factor(1600, 32) = round(50)×32 = 1600`；乘积 \(992 \times 1600 = 1{,}587{,}200\)，落在 \([4096,\; 16{,}777{,}216]\) 内 → 直接返回 (992, 1600)，对应 \(31 \times 50 = 1550\) 个视觉 token。
- **② 超上限**（max_pixels = 1,310,720）：\(992 \times 1600 = 1{,}587{,}200 > 1{,}310{,}720\)；\(\beta = \sqrt{1{,}600{,}000 / 1{,}310{,}720} = \sqrt{1.2207} \approx 1.1049\)；\(1000/\beta \approx 905.1 \to \lfloor 905.1/32 \rfloor \times 32 = 28 \times 32 = 896\)；\(1600/\beta \approx 1448.2 \to 45 \times 32 = 1440\) → (896, 1440)，对应 \(28 \times 45 = 1260\) 个 token（≤ 1280 ✓）。
- **③ 低于下限**：`round_by_factor(10, 32) = 0`，被 `max(32, 0)` 兜成 32，乘积 1024 < 4096；\(\beta = \sqrt{4096/100} = 6.4\)；\(10 \times 6.4 = 64\)，`ceil_by_factor(64, 32) = 64` → (64, 64)，恰好 \(2 \times 2 = 4\) 个 token，正好踩在下限上。

**需要观察的现象**：前三个调用分别命中分支 ③/①、分支 ①、分支 ②；④ 抛 ValueError；⑤ 抛 AssertionError。所有输出的宽高都能被 32 整除。

**预期结果**：依次打印 `(992, 1600)`、`(896, 1440)`、`(64, 64)`，随后是两条错误信息。（数值待本地验证）

#### 4.3.5 小练习与答案

**练习 1**：一张 640×480 的图（factor=32、缺省预算）输出什么？
**答案**：`round_by_factor(480, 32) = round(15)×32 = 480`；`round_by_factor(640, 32) = 640`；乘积 307,200 在预算内 → (480, 640)，\(15 \times 20 = 300\) 个 token。

**练习 2**：为什么不直接把 β 取为 `sqrt(h_bar*w_bar/max_pixels)`（用取整后的尺寸算 β），而是用原始 h·w？
**答案**：两者只差取整带来的微小面积差。用原始尺寸意味着「从真实画面出发等比缩放」，概念上更干净；且 h_bar·w_bar 只用于**判断**是否越界。这是工程近似——smart_resize 本身就声明「as closely as possible」，不追求像素级最优。

**练习 3**：把 min_pixels 和 max_pixels 设成同一个值（例如 256×32×32）会发生什么？
**答案**：就近对齐后的面积只要大于该值就进入缩小分支（`if > max` 优先于 `elif < min`），floor 取整后结果通常**略低于**目标（如 4.4.4 的 240 < 256）；只有原图本身就极小（对齐后低于该值）才走放大分支。所以 `min == max` 的实际语义是「目标 token 数，允许因取整略小」，而非「精确等于」。

---

### 4.4 像素预算与视觉 token 数的换算

#### 4.4.1 概念说明

预算参数虽然叫 `min_pixels` / `max_pixels`（单位是像素），但**它真正控制的量是视觉 token 数**。换算关系只依赖一个事实——factor=32 时，每 32×32 的像素块恰好产出 1 个视觉 token：

\[
N_{\text{token}} = \frac{H \times W}{32 \times 32}
\quad\Longleftrightarrow\quad
\text{pixels} = N_{\text{token}} \times 32 \times 32
\]

于是「把这张图的视觉 token 上限设为 T」就直接写成：

```python
{"image": path, "max_pixels": T * 32 * 32}
```

这套「token 数 × factor²」的记法贯穿整个生态：本库的缺省预算常量（4 / 16384 token）、README 的推荐区间（图像 256~1280 token）、评测脚本里的分辨率设定、微调参数里的 `max_pixels`，全都用同一公式。掌握它就能在任何地方读懂数字背后的 token 含义。

两代模型的换算对照：

| | factor | 1 token 覆盖 | min/max 缺省（token） | 缺省像素预算 |
| --- | --- | --- | --- | --- |
| Qwen2-VL / 2.5-VL | 28 | 28×28 = 784 px | 4 ~ 16384 | 3,136 ~ 12,845,056 px |
| Qwen3-VL | 32 | 32×32 = 1024 px | 4 ~ 16384 | 4,096 ~ 16,777,216 px |

#### 4.4.2 核心流程

换算链条全景（以设置「单图最多 T 个 token」为例）：

```text
意图：单图视觉 token ≤ T
  ↓ 乘 factor²（像素 = token × 32×32）
max_pixels = T * 32 * 32
  ↓ fetch_image 读取 ele["max_pixels"]（vision_process.py L132）
smart_resize(h, w, factor=32, min_pixels=…, max_pixels=T*1024)
  ↓ 缩小分支：floor 取整
输出 (H, W)，两者均为 32 的倍数
  ↓ 除 factor
实际 token 数 = (H/32) × (W/32) ≤ T（因 floor，可能略小）
```

预算信息也可以**不加在元素上**而通过 processor 统一配置：`processor.image_processor.size = {"longest_edge": T_max*32*32, "shortest_edge": T_min*32*32}`（[README.md:L348-L356](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L348-L356)），其中 `longest_edge` 对应 max_pixels、`shortest_edge` 对应 min_pixels——两条路径语义等价，前者逐元素覆盖、后者全局默认（processor 侧细节在 u3-l1 展开）。

#### 4.4.3 源码精读

以 token 数定义的常量在 [vision_process.py:L24-L29](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L24-L29)，这段代码同时给出图像与视频两套预算（视频的供 u2-l3 使用）：

```python
MAX_RATIO = 200
SPATIAL_MERGE_SIZE = 2
IMAGE_MIN_TOKEN_NUM = 4        # 图像最小 4 个视觉 token
IMAGE_MAX_TOKEN_NUM = 16384    # 图像最大 16384 个视觉 token
VIDEO_MIN_TOKEN_NUM = 128      # 视频单帧另有预算，见 fetch_video
VIDEO_MAX_TOKEN_NUM = 768
```

`fetch_image` 的预算读取与传递在 [vision_process.py:L129-L139](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L129-L139)，这段代码把元素级预算键翻译成 `smart_resize` 参数：

```python
else:
    width, height = image.size                       # 注意 PIL 顺序：(宽, 高)
    min_pixels = ele.get("min_pixels", IMAGE_MIN_TOKEN_NUM * patch_factor ** 2)
    max_pixels = ele.get("max_pixels", IMAGE_MAX_TOKEN_NUM * patch_factor ** 2)
    resized_height, resized_width = smart_resize(
        height, width,                               # 翻译回 (高, 宽)
        factor=patch_factor,
        min_pixels=min_pixels,
        max_pixels=max_pixels,
    )
```

对照 `smart_resize` 内部的缺省逻辑（[L64-L65](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L64-L65)，None 时用 `IMAGE_*_TOKEN_NUM × factor²`），会发现**两层都写了同样的缺省**——`fetch_image` 显式算出缺省值传入，`smart_resize` 又兜了一次底。效果相同，但显式传参让调用点（如直控路径不给预算）仍能获得统一缺省。

官方文档对这套预算的说明见 [README.md:L477-L481](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L477-L481)，官方推荐的图像 token 区间（256~1280）出现在 [README.md:L351-L352](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L351-L352)：

```python
# budget for image processor, since the compression ratio is 32 for Qwen3-VL,
# we can set the number of visual tokens of a single image to 256-1280
processor.image_processor.size = {"longest_edge": 1280*32*32, "shortest_edge": 256*32*32}
```

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：对同一张图设置两档 token 预算（256 与 1280），验证①输出宽高都是 32 的倍数；②手算 token 数与程序输出一致；③体会 floor 取整让实际 token 略低于预算值。

**操作步骤**（示例代码，合成图完全离线）：

```python
# budget_tiers.py
from PIL import Image
from qwen_vl_utils import process_vision_info

img = Image.new("RGB", (1600, 1000), color=(30, 144, 255))   # 宽1600 × 高1000
F = 32

print(f"原始尺寸（宽×高）: {img.size}")
for tier in (256, 1280):
    budget = tier * F * F                                    # token 数 → 像素预算
    messages = [{"role": "user", "content": [
        {"type": "image", "image": img,
         "min_pixels": budget, "max_pixels": budget},        # min==max：锁定该档
        {"type": "text", "text": "describe"},
    ]}]
    images, _ = process_vision_info(messages, image_patch_size=16)
    w, h = images[0].size                                    # PIL 顺序：(宽, 高)
    tokens = (h // F) * (w // F)
    print(f"预算 {tier:>4} token ({budget:>8} px) -> 输出 {w}×{h} | "
          f"w%32={w % F}, h%32={h % F} | 实际 token = ({h}//32)×({w}//32) = {tokens}")
```

**手算推导**（沿用 4.3.4 ②的中间结果）：

- **256 档**：max_pixels = 262,144；对齐后 \(992 \times 1600 = 1{,}587{,}200 > 262{,}144\) → 缩小，\(\beta = \sqrt{1{,}600{,}000/262{,}144} = \sqrt{6.1035} \approx 2.4705\)；\(1000/\beta \approx 404.8 \to \lfloor 404.8/32 \rfloor \times 32 = 12 \times 32 = 384\)；\(1600/\beta \approx 647.6 \to 20 \times 32 = 640\) → 输出 高 384 × 宽 640，token = \(12 \times 20 = 240\)。
- **1280 档**：见 4.3.4 ② → 输出 高 896 × 宽 1440，token = \(28 \times 45 = 1260\)。

**需要观察的现象**：

1. 两档输出的宽、高对 32 取余都为 0（整除性约束生效）。
2. 实际 token 数 240 / 1260 都**略低于**预算 256 / 1280——缩小分支 floor 取整 + 单遍执行的结果，不是 bug。
3. 两档输出都保持了 640:384 = 5:3、1440:896 = 45:28 ≈ 1.607，与原图 1600:1000 = 1.6 几乎一致（长宽比保持）。

**预期结果**：

| 预算（token） | 预算（px） | 输出（宽×高） | 视觉 token |
| --- | --- | --- | --- |
| 256 | 262,144 | 640 × 384 | 12 × 20 = 240 |
| 1280 | 1,310,720 | 1440 × 896 | 28 × 45 = 1260 |

（数值待本地验证）

**可选扩展（需要联网下载模型文件，仅 processor 不需 GPU）**：把 `images` 交给 processor tokenize，统计展开后的视觉占位 token 数量是否等于上表手算值：

```python
from transformers import AutoProcessor
processor = AutoProcessor.from_pretrained("Qwen/Qwen3-VL-2B-Instruct")
text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
inputs = processor(text=text, images=images, do_resize=False, return_tensors="pt")
# 视觉占位符形如 <|image_pad|>（u1-l3/u3-l1 均使用该记法），统计其在 input_ids 中的出现次数
pad_id = processor.tokenizer.convert_tokens_to_ids("<|image_pad|>")
print("占位 token 数：", int((inputs.input_ids[0] == pad_id).sum()))
```

注意 `do_resize=False`——u2-l1 已强调：`process_vision_info` 缩放过的图不能再让 processor 缩一遍（[README.md:L526-L532](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L526-L532) 同样注明）。占位 token 的确切名称与计数方式以本地 tokenizer 实际输出为准（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`{"image": p, "max_pixels": 50176}`（[README.md:L517-L518](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L517-L518) 官方示例的写法）在 Qwen3-VL 下大约允许几个视觉 token？
**答案**：\(50176 / 1024 = 49\)，即约 49 个 token（README 该示例若按 factor=28 理解则是 64 个 token；同一个像素值在两代模型下对应的 token 数不同，这正是必须乘对 factor² 的原因）。

**练习 2**：为什么缺省上限高达 16384 个 token（4096×4096 像素），README 却推荐图像只用 256~1280？
**答案**：缺省值是「能力上限」（保证任意合法图片都能被处理，不因库的缺省配置失败）；推荐值是「性价比区间」——绝大多数任务的细节 256~1280 token 已足够，token 越多推理越慢、上下文占用越大。工程上应按任务实测选档，而不是依赖缺省。

**练习 3**：如果不传 `image_patch_size=16`（保持缺省 14），同样的 `max_pixels=256*32*32` 会怎样？
**答案**：factor 变成 28，像素预算不变但换算基准错了：输出的宽高对齐到 28 的倍数，token 数按 784 px/token 计（\(262144/784 \approx 334\) 个 token），与 Qwen3-VL 处理器期望的 32 网格不匹配——u2-l1 强调过的「静默出错」：不报异常，只是网格全错。

---

### 4.5 resized_height / resized_width：绕过预算的精确直控

#### 4.5.1 概念说明

预算控制（min/max_pixels）回答的是「**最多/最少**占多少 token」，尺寸由算法按长宽比自行决定。但有些场景需要**指定输出尺寸本身**——例如多张图必须统一到同一分辨率、复现某个实验的输入规格、或已知目标检测任务里小目标密集需要精确的网格数。这时在元素里直接写：

```python
{"type": "image", "image": p, "resized_height": 280, "resized_width": 420}
```

语义是「**把这张图缩放到约 280×420**」。注意三点：

1. 给出的尺寸仍会被 `round_by_factor` **就近对齐**到 factor 的倍数（官方文档原话：rounded to the nearest multiple of 32，见 [README.md:L477-L479](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L477-L479)）。
2. `ele` 里的 `min_pixels`/`max_pixels` 在这条路径上**不参与**计算；预算回退到缺省的 4~16384 token。
3. 必须两个键**同时**给出，只给一个会被静默忽略、走回预算路径。

#### 4.5.2 核心流程

```text
fetch_image 的缩放段（vision_process.py L122-L139）
  if "resized_height" in ele and "resized_width" in ele:     # 双键齐备才走直控
      smart_resize(ele["resized_height"], ele["resized_width"],
                   factor=patch_factor)                       # 不传预算 → 缺省 4~16384 token
  else:                                                       # 否则走预算路径（4.4）
      smart_resize(原图 h, 原图 w, factor, ele 的 min/max_pixels)
  image.resize((resized_width, resized_height))               # 统一执行缩放
```

直控路径内部仍会经过 `smart_resize` 的完整流程：就近对齐 → 长宽比闸门 → 缺省预算检查。所以「直控」并不等于「无约束」，只是把预算换成缺省值、把「原始尺寸」换成「你指定的尺寸」。

#### 4.5.3 源码精读

直控分支在 [vision_process.py:L122-L128](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L122-L128)，这段代码只做对齐、不传预算：

```python
## resize
if "resized_height" in ele and "resized_width" in ele:
    resized_height, resized_width = smart_resize(
        ele["resized_height"],
        ele["resized_width"],
        factor=patch_factor,          # 没有 min_pixels/max_pixels
    )
```

对照官方文档对两种控制方式的定义（[README.md:L477-L481](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L477-L481)）：

> - Specify exact dimensions: Directly set resized_height and resized_width. These values will be rounded to the nearest multiple of 32.
> - Define min_pixels and max_pixels: Images will be resized to maintain their aspect ratio within the range of min_pixels and max_pixels.

README 的直控示例（[README.md:L493-L507](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L493-L507)）用的正是 280×420 这组值，手算其对齐结果：`round_by_factor(280, 32) = round(8.75)×32 = 9×32 = 288`；`round_by_factor(420, 32) = round(13.125)×32 = 13×32 = 416`；乘积 \(288 \times 416 = 119{,}808\) 在缺省预算内 → 输出 高 288 × 宽 416，\(9 \times 13 = 117\) 个 token。

两个边界值得知道：

- **直控仍有上限**：指定 20000×20000 时，对齐后 \(20000^2 = 4 \times 10^8\) 远超缺省上限 \(16384 \times 1024 = 16{,}777{,}216\)，照样触发缩小分支：\(\beta = \sqrt{4\times10^8 / 16{,}777{,}216} \approx 4.883\)，\(20000/\beta \approx 4095.9 \to \lfloor 127.99 \rfloor \times 32 = 4064\) → 输出 4064×4064（\(127^2 = 16129\) 个 token）。想突破 16384 token 得改源码常量，不是传参能绕过的。
- **就近对齐可能轻微改变长宽比**：280:420 = 2:3 ≈ 0.667，对齐后 288:416 ≈ 0.692——两个维度**各自独立**取整，比率无法严格保持。这是直控与预算路径（等比缩放）的重要差异。

#### 4.5.4 代码实践

**实践目标**：验证直控的「就近对齐」行为，以及「只给一个键会被忽略」的陷阱。

**操作步骤**（示例代码，完全离线）：

```python
# direct_size_test.py
from PIL import Image
from qwen_vl_utils import fetch_image

img = Image.new("RGB", (1600, 1000), color=(30, 144, 255))

# ① 双键直控（README 官方示例的数值）
out = fetch_image({"image": img, "resized_height": 280, "resized_width": 420},
                  image_patch_size=16)
print("① 直控 280×420 ->", out.size, "token =", (out.size[1]//32) * (out.size[0]//32))

# ② 超大直控：缺省 16384-token 上限仍生效
out2 = fetch_image({"image": img, "resized_height": 20000, "resized_width": 20000},
                   image_patch_size=16)
print("② 直控 20000×20000 ->", out2.size, "token =", (out2.size[1]//32) * (out2.size[0]//32))

# ③ 只给一个键：静默回退到预算路径（输出由原图 1600×1000 决定，与 ① 不同）
out3 = fetch_image({"image": img, "resized_height": 280}, image_patch_size=16)
print("③ 只有 resized_height ->", out3.size)

# ④ 直控 + 预算键并存：预算键被忽略（对比 ① 输出相同）
out4 = fetch_image({"image": img, "resized_height": 280, "resized_width": 420,
                    "min_pixels": 256*32*32, "max_pixels": 256*32*32},
                   image_patch_size=16)
print("④ 直控+预算并存 ->", out4.size)
```

**需要观察的现象**：①输出宽高都是 32 的倍数且不等于请求值（280→288、420→416）；②被压回 4064×4064；③输出是按原图算出的 (1600, 992) 而非 280 相关的值；④与①相同，min/max_pixels 未生效。

**预期结果**：①`(416, 288)`，token = 117；②`(4064, 4064)`，token = 16129；③`(1600, 992)`；④`(416, 288)`。（数值待本地验证）

#### 4.5.5 小练习与答案

**练习 1**：想让输出**精确等于** 448×320（都是 32 的倍数），直控能达到吗？
**答案**：能。`round_by_factor(448, 32) = 448`、`round_by_factor(320, 32) = 320`，已是 factor 倍数的值取整后不变；乘积 143,360 也在缺省预算内 → 原样输出。**结论：直控值本身取 32 的倍数即可获得精确尺寸**，这也是工程上推荐的做法。

**练习 2**：直控路径会读取 `ele["min_pixels"]` 吗？
**答案**：不会。[L123-L128](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L123-L128) 的调用只传了 `factor`，min/max 走 `smart_resize` 的 None 缺省（4~16384 token）。元素里的预算键只在 else 分支（[L129-L139](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L129-L139)）被读取。

**练习 3**：为什么源码要求两个 resized 键**同时**存在（`and`）而不是缺一个用另一个补？
**答案**：宽高二者只要有一个走「按原图比例推算」，就退化为预算路径的等比逻辑，与调用方「明确指定两个维度」的意图不符；`and` 让语义清晰——要么完整直控，要么完全不直控。副作用是「只写一个键」被静默忽略（4.5.4 ③），排查问题时可以先检查这对键是否成对出现。

## 5. 综合实践

设计一个「**smart_resize 复刻 + 随机对拍**」小项目，把本讲的取整函数、约束算法、预算换算、直控路径全部串起来。

任务分四步：

1. **复刻**：不看源码，凭 4.3.2 的伪代码独立实现 `my_round_by_factor` / `my_ceil_by_factor` / `my_floor_by_factor` / `my_smart_resize`（示例代码，放入 `my_smart_resize.py`），要求函数签名与库一致（`smart_resize(height, width, factor, min_pixels=None, max_pixels=None)`）。
2. **手算锚点**：用三个锚点用例验证你的实现——`(1000, 1600, 32)` → `(992, 1600)`；`(1000, 1600, 32, None, 1280*32*32)` → `(896, 1440)`；`(10, 10, 32)` → `(64, 64)`（推导过程见 4.3.4）。
3. **随机对拍**：写 `diff_test.py`（示例代码），固定随机种子，对 200 组随机输入 \((h, w) \in [8, 4000]^2\)、factor ∈ {28, 32}、随机预算（min 取 \([4, 2000]\times \text{factor}^2\)、max 取 \([\text{min}, 4000]\times \text{factor}^2\)），逐组断言 `my_smart_resize(...) == smart_resize(...)`，统计并打印不一致数。特别注意：因为复刻版与原版用的是同一套 Python 内置 `round`/`ceil`/`floor`，**银行家舍入行为也应一致**——如果你的实现对 `.5` 情况做了「四舍五入」，对拍会立刻抓出来。
4. **端到端抽查**：随机挑 5 组参数，用 `fetch_image`（元素上带 `min_pixels`/`max_pixels` 或 `resized_height`/`resized_width`）跑真实链路，验证返回的 `size`（记得 (宽, 高) 顺序）与你的复刻输出一致。

验收标准：

- 对拍不一致数为 0；
- 能用一句话向同伴解释清楚「为什么 min == max 时实际 token 数总是略低于目标值」（答案在 4.3.5 练习 3）；
- 把第 4 步的 5 组结果整理成表格：元素配置 → 复刻输出 → `fetch_image` 实际输出 → token 数。

本实践完全离线可跑（合成图 + 直接函数调用），所有数值可精确预判，无需 GPU。

## 6. 本讲小结

- `fetch_image` 加载段把 PIL 对象 / 本地路径 / `file://` / HTTP(S) URL / base64 数据 URL 五种来源统一成 RGB PIL 图（[vision_process.py:L93-L120](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L93-L120)）；URL/base64 路径必须 `deepcopy` 强制在 BytesIO 关闭前读完像素；RGBA 走白底合成而非直接丢 alpha。
- 三个对齐取整函数（[L41-L53](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L41-L53)）是所有尺寸/帧数对齐的原子操作；取整方向带语义——缩小配 floor（不超上限）、放大配 ceil（达到下限）；Python `round` 是银行家舍入，`.5` 边界取偶。
- `smart_resize`（[L56-L81](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-utils/src/qwen_vl_utils/vision_process.py#L56-L81)）= 长宽比闸门（200:1）→ 就近对齐（`max(factor, ·)` 兜底）→ 三分支（超上限按 \(\beta=\sqrt{hw/\text{max}}\) 等比缩小、低于下限按 \(\beta=\sqrt{\text{min}/hw}\) 等比放大、预算内不动），单遍执行不迭代。
- 像素预算的真正单位是 token：\(\text{pixels} = N_{\text{token}} \times 32 \times 32\)（Qwen3-VL），缺省 4~16384 个 token；`min == max` 是「锁定目标 token 数」的惯用写法，因 floor 取整实际值常略低（256 档实得 240）。
- `resized_height`/`resized_width` 直控绕过元素预算（缺省 4~16384 token 仍生效），值会被就近对齐到 32 的倍数——想让输出精确等于某尺寸，直接给 32 的倍数即可；两键必须成对出现，否则静默走回预算路径。

## 7. 下一步学习建议

- **u2-l3（视频链路）**：`fetch_video` 把本讲的 `smart_resize` 用到**每一帧**上，并叠加 `smart_nframes` 帧数采样、`total_pixels` 总预算与 torchvision/decord/torchcodec 三种解码后端——本讲的取整函数会在帧数对齐（`FRAME_FACTOR=2`）处再次登场。
- **u2-l4（video metadata）**：u2-l1 埋下的时间戳伏笔将在那里展开；可对照本讲理解「图像只有空间预算、视频还有时间预算」的差异。
- **u3-l1（processor 深入）**：本讲 4.4 提到的 `processor.image_processor.size`（longest_edge/shortest_edge）将在 processor 侧系统展开，两套预算机制（元素级 vs processor 级）在那里汇合。
- **源码延伸阅读**：`evaluation/` 目录下各基准的 `run_*.py` 里大量出现 `MIN_PIXELS`/`MAX_PIXELS` 常量设定，读完本讲你可以直接读懂它们的 token 含义；微调侧 `qwen-vl-finetune` 的 `DataArguments` 也有同名的 `min_pixels`/`max_pixels` 参数（u5-l2），同一公式贯穿训练与推理。

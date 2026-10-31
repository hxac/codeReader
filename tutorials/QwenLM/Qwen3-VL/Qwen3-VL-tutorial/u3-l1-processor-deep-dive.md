# 深入 processor：chat template、像素预算与 add_vision_id

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `apply_chat_template` 两种模式（`tokenize=False` / `tokenize=True`）各自的输出形态与内部流水线，说清 `<|image_pad|>` 从「一个占位符」到「N 个 token」的展开发生在哪一步、N 由什么决定。
2. 掌握 `processor.image_processor.size` 与 `processor.video_processor.size` 两套像素预算的**语义差异**（单帧预算 vs 全帧总预算），能按目标视觉 token 数反推出 size 取值，并说清它与 u2-l2 元素级预算的分层关系。
3. 会用 `add_vision_id=True` 为多图/多视频对话自动加上「Picture N: / Video N:」编号，并知道编号文本在提示串里的确切位置。
4. 掌握 batch 推理的两个必备设置——`processor.tokenizer.padding_side = 'left'` 与 `padding=True`——并能解释左填充与输出裁剪 `out_ids[len(in_ids):]` 为什么能配合工作。

u1-l3 已经建立了「apply_chat_template → generate → 裁剪解码」的调用链骨架，并在实践中粗看了一眼两种模式的类型差异；本讲把 processor 这一环拆开细讲，所有实践**只需 processor、不需加载模型权重**，笔记本电脑 CPU 即可完成。

## 2. 前置知识

### 2.1 承接前讲：两条已有结论

- **u1-l3**：`processor.apply_chat_template(messages, tokenize=True, add_generation_prompt=True, return_dict=True, return_tensors="pt")` 一步返回 `BatchEncoding`，其中除了 `input_ids` / `attention_mask`，还有 `pixel_values`、`image_grid_thw`（视频输入时是 `pixel_values_videos` / `video_grid_thw` 等时间戳张量）。
- **u2-l2**：qwen-vl-utils 在 **messages 元素级** 提供 `min_pixels` / `max_pixels` 预算，图像宽高被对齐到 32 的倍数，视觉 token 数 \( N = \frac{H}{32} \times \frac{W}{32} \)。

本讲要回答的追问是：**transformers 的 processor 内部还有一套预算（`size` 字典），它和 qwen-vl-utils 那套是什么关系？谁在什么时候生效？**

### 2.2 AutoProcessor：三合一的预处理入口

`AutoProcessor.from_pretrained(...)` 加载的不是单一对象，而是三个组件的组合：

| 组件 | 访问方式 | 职责 |
| --- | --- | --- |
| tokenizer | `processor.tokenizer` | 文本 ↔ token id，负责 padding |
| image_processor | `processor.image_processor` | 图像加载、缩放（执行 size 预算）、patch 化，持有 `patch_size` |
| video_processor | `processor.video_processor` | 视频帧采样与缩放（执行视频 size 预算） |

README 明确说明 Qwen3-VL 的 processor **把图像与视频处理拆成了两个独立组件**，因此两种模态可以各配一套预算（[README.md:L339](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L339)）。另外，`processor.image_processor.patch_size` 是 patch 边长的**权威来源**——u2-l1 说过「Qwen3-VL 必须传 `image_patch_size=16`，最佳实践是动态读取」，本讲 4.2.3 会看到 web_demo_mm.py 里那句动态读取的真实出处。

### 2.3 Qwen3-VL 提示串中的特殊 token 速查

以下是 README 官方给出的渲染结果中出现的全部特殊 token（出处见 4.3.3 的 [README.md:L729](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L729)）：

| token | 含义 |
| --- | --- |
| `<\|im_start\|>` / `<\|im_end\|>` | 对话轮次的开始 / 结束包裹符，后跟 `system` / `user` / `assistant` 角色名 |
| `<\|vision_start\|>` / `<\|vision_end\|>` | 一个视觉输入块的包裹符（图像与视频通用） |
| `<\|image_pad\|>` | 图像占位 token（编码阶段按视觉 token 数展开） |
| `<\|video_pad\|>` | 视频占位 token（同样按视觉 token 数展开） |

### 2.4 实验环境

- transformers ≥ 4.57.0（[README.md:L132-L135](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L132-L135)），建议用 `Qwen/Qwen3-VL-2B-Instruct` 做实验（processor 与大模型同构，但更省流量）。
- 本讲实践中 `apply_chat_template` 只用 processor 做预处理，**无需 `AutoModelForImageTextToText`**；只有 4.4 的最后一步「跑一次 batch 生成」可选地需要模型（也可只检查张量形状跳过生成）。

## 3. 本讲源码地图

| 文件 | 本讲关注的行段 | 作用 |
| --- | --- | --- |
| [README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md) | L177-L183 模式 A 调用；L288-L334 batch 推理；L336-L356 像素预算语义与代码；L358-L397 fps/num_frames；L436 与 L527-L532 模式 B 组合调用；L688-L738 add_vision_id | 官方对 processor 全部用法约定的「规范文本」 |
| [web_demo_mm.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py) | L180 与 L220-L226 两种 tokenize 模式的真实分工；L183 patch_size 动态读取；L178-L198 `_prepare_inputs_for_vllm` | 仓库中唯一同时用上两种模式的可运行工程代码 |

说明：processor 的**实现代码**（chat template Jinja 模板、`Qwen3VLImageProcessor` 的 resize 逻辑）位于 transformers 库与模型仓库中，不在本仓库——本讲的源码精读以本仓库的官方文档与工程用法为锚点，行为断言均可被 4.x 的实践在本地验证。

## 4. 核心概念与源码讲解

### 4.1 apply_chat_template 的两种模式

#### 4.1.1 概念说明

`apply_chat_template` 有两种工作模式，区别在于「模板渲染」与「视觉预处理 + 分词编码」是否一步完成：

- **模式 A：一步到位（`tokenize=True`）**。渲染对话模板的同时，processor 自动完成图像/视频加载、按预算缩放、patch 化，并把每个视觉占位符展开成与视觉 token 数一致的 `<|image_pad|>` / `<|video_pad|>`，最终返回 `input_ids` + `pixel_values` 等张量。这是 **transformers 推理的默认路径**。
- **模式 B：渲染与编码分离（`tokenize=False`）**。只做模板渲染，返回**纯字符串**：每个图像位置只留**一个** `<|image_pad|>` 占位符。视觉部分交给外部工具（qwen-vl-utils）处理，再由 `processor(text=..., images=..., videos=..., do_resize=False)` 显式编码。这是 **vLLM / 自定义预处理场景的路径**。

为什么需要模式 B？因为 qwen-vl-utils 已经按元素级预算完成缩放与帧采样（u2-l2 / u2-l3），若让 processor 再 resize 一次，既浪费算力又可能破坏已对齐的尺寸，所以要传 `do_resize=False`；而 vLLM 引擎接收的是「原始 prompt 字符串 + 张量字典」，编码由引擎内部完成（这正是 u3-l2 的主角）。

#### 4.1.2 核心流程

```text
模式 A（tokenize=True，一步到位）
  messages ──渲染模板──▶ 含 <|image_pad|> 占位的提示串
          ──processor 加载图像并按 size 预算缩放（H',W' 对齐 32）──▶ pixel_values + 网格
          ──把占位符展开为 (H'/32)×(W'/32) 个 <|image_pad|>，分词──▶ input_ids
          ──返回──▶ BatchEncoding{input_ids, attention_mask, pixel_values, image_grid_thw, ...}

模式 B（tokenize=False，渲染/编码分离）
  messages ──渲染模板──▶ 纯字符串 text（每个图像仅 1 个 <|image_pad|>）
          ──qwen_vl_utils.process_vision_info──▶ 已缩放的图像/视频张量 + video_kwargs
          ──processor(text=text, images=..., videos=..., do_resize=False)──▶ 同样的 BatchEncoding
          （或把 text + 张量直接交给 vLLM 引擎）
```

两条路殊途同归，但有一条**因果链**值得背下来：

\[ \text{size 预算} \;\Rightarrow\; \text{缩放后尺寸 } H' \times W' \;\Rightarrow\; \text{视觉 token 数 } \tfrac{H'}{32} \times \tfrac{W'}{32} \;\Rightarrow\; \text{input\_ids 长度} \]

这就是为什么 4.2 的预算实验直接表现为「`input_ids` 变长/变短」。

#### 4.1.3 源码精读

**模式 A 的标准调用**（[README.md:L177-L183](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L177-L183)）：这段是官方 Quickstart 的预处理调用，`tokenize=True` + `return_dict=True` + `return_tensors="pt"` 三件套，messages 里的图像 URL 由 processor 自行下载。

**模式 B 的标准组合拳**（[README.md:L527-L532](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L527-L532)）：L527 用 `tokenize=False` 拿到字符串，L528 用 `process_vision_info(messages, image_patch_size=16)` 得到已缩放的图像，L532 把两者交给 `processor(text=..., images=..., videos=..., do_resize=False, return_tensors="pt")` 完成**编码**。注意 L530-L531 的注释与 [README.md:L436](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L436) 的📌提示：**utils 已缩放过，processor 必须 `do_resize=False` 以避免二次缩放**——这句话就是两套预算的「交接协议」。

**两种模式在一个真实工程文件里的分工**（[web_demo_mm.py:L180](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L180) 与 [web_demo_mm.py:L220-L226](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L220-L226)）：

- vLLM 分支的 `_prepare_inputs_for_vllm` 用 `tokenize=False` 只取提示串（L180），因为张量要原样交给 vLLM 引擎；
- HF 分支的 `call_local_model` 用 `tokenize=True` 一步拿到全部输入张量直接 `model.generate`（L220-L226，u1-l4 已讲过该分支的流式线程）。

同一个函数族、同一个 messages，两种模式的选择完全由**后端吃什么样的输入**决定——这是本讲最重要的工程直觉。

**`add_generation_prompt=True` 渲染出了什么**：对照 [README.md:L729](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L729) 给出的预期串，末尾是 `<|im_start|>assistant\n`——即「助手轮的开头」。模型从这里续写回答，`batch_decode(skip_special_tokens=True)` 时最终又把它过滤掉。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认两种模式的输出形态，并数出 `<|image_pad|>` 的个数与其决定因素。
2. **操作步骤**（示例代码，只需 processor）：

```python
# 示例代码：u3_l1_practice_a.py
from transformers import AutoProcessor

ckpt = "Qwen/Qwen3-VL-2B-Instruct"
processor = AutoProcessor.from_pretrained(ckpt)

messages = [{
    "role": "user",
    "content": [
        {"type": "image", "image": "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen-VL/assets/demo.jpeg"},
        {"type": "text", "text": "Describe this image."},
    ],
}]

# 模式 B：只渲染
text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
print(repr(text))                      # 观察 <|vision_start|><|image_pad|><|vision_end|> 的个数
print("字符串中 image_pad 个数:", text.count("<|image_pad|>"))

# 模式 A：一步到位
inputs = processor.apply_chat_template(
    messages, tokenize=True, add_generation_prompt=True,
    return_dict=True, return_tensors="pt",
)
print(inputs.keys())
print("input_ids 形状:", inputs["input_ids"].shape)

pid = processor.tokenizer.convert_tokens_to_ids("<|image_pad|>")
n_pad = (inputs["input_ids"] == pid).sum().item()
grid = inputs["image_grid_thw"][0]     # (t, h, w)
print("input_ids 中 image_pad 个数:", n_pad)
print("image_grid_thw:", grid.tolist(), "乘积:", grid.prod().item())
```

3. **需要观察的现象**：字符串里每个图像只有 **1 个** `<|image_pad|>`；而 `input_ids` 里它是成片出现的，个数恰好等于 `image_grid_thw` 的三元素乘积 \( t \times h \times w \)（图像 \( t=1 \)，即 \( \frac{H'}{32} \times \frac{W'}{32} \)）。
4. **预期结果**：`n_pad == grid.prod().item()` 成立；把 `inputs["input_ids"]` 用 `processor.tokenizer.decode` 解码后与 `text` 逐字符对齐（唯一差异是占位符个数）。具体的 token 数取决于 demo.jpeg 在默认预算下的缩放结果，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：模式 B 里，如果把 `do_resize=False` 忘了传，会发生什么？
答案：processor 的 image_processor 会对 utils 已缩放好的图像**再执行一次** size 预算缩放。轻则浪费算力，重则当 processor 预算与 utils 预算不一致时图像被二次缩放，`image_grid_thw` 与 utils 意图不符（官方在 [README.md:L436](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L436) 专门加了📌提示）。

**练习 2**：为什么 vLLM 分支用 `tokenize=False` 而不是 `tokenize=True` 后再把张量拆出来？
答案：vLLM 的输入协议是「prompt 字符串 + `multi_modal_data` 张量字典 + `mm_processor_kwargs`」（见 [web_demo_mm.py:L194-L198](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L194-L198)），编码由引擎内部的 processor 完成；自己先编码反而会丢失「按引擎侧预算展开占位符」的机会。

**练习 3**：`add_generation_prompt=True` 渲染出的最后一个 token 是什么？为什么 `batch_decode` 看不到它？
答案：`<|im_start|>assistant\n` 对应的 token；`batch_decode(..., skip_special_tokens=True)` 会过滤特殊 token，所以最终输出文本里不出现。

### 4.2 size 预算参数：image_processor 与 video_processor 的分治

#### 4.2.1 概念说明

processor 级预算写在两个 `size` 字典里，**图像与视频语义不同**是本节最大的坑：

- `processor.image_processor.size = {"longest_edge": ..., "shortest_edge": ...}`
  - `longest_edge` 对应 `max_pixels`：**单张图**的 \( H \times W \) 上限；
  - `shortest_edge` 对应 `min_pixels`：单张图像素下限（[README.md:L341-L342](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L341-L342)）。
- `processor.video_processor.size = {"longest_edge": ..., "shortest_edge": ...}`
  - `longest_edge` 是**全部帧的像素总和**上限：视频形状为 \( T \times H \times W \) 时，\( T \times H \times W \) 不得超过它；`shortest_edge` 是总预算下限（[README.md:L345-L346](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L345-L346)）。

它与 u2-l2 元素级预算构成**两层旋钮**：

| 层次 | 写在哪 | 作用域 | 生效路径 |
| --- | --- | --- | --- |
| 元素级 `min_pixels` / `max_pixels` / `total_pixels` | messages 的 image/video 元素里 | 单个输入 | qwen-vl-utils（模式 B） |
| processor 级 `size` 字典 | `processor.image_processor.size` 等 | 该 processor 处理的所有输入 | processor 内部（模式 A，或模式 B 未关 resize 时） |

两层不要同时「较劲」：走模式 B 时预算交给 utils、processor 关掉 resize；走模式 A 时统一由 `size` 说了算。

#### 4.2.2 核心流程

图像（32× 空间压缩）：

\[
N_{\text{img}} = \frac{H' \times W'}{32 \times 32}, \qquad
\text{shortest\_edge} \le H' \times W' \le \text{longest\_edge}
\]

视频（32× 空间 + 2× 时间压缩，u2-l3 讲过帧数取偶正源于此）：

\[
N_{\text{vid}} = \frac{T' \times H' \times W'}{32 \times 32 \times 2}
= \frac{T'}{2} \times \frac{H'}{32} \times \frac{W'}{32}, \qquad
\text{shortest\_edge} \le T' \times H' \times W' \le \text{longest\_edge}
\]

反过来，**按目标 token 数配置预算**就用乘法（这就是官方示例数字的由来）：

\[
\text{image size} = N_{\text{img}} \times 32 \times 32, \qquad
\text{video size} = N_{\text{vid}} \times 32 \times 32 \times 2
\]

操作流程：

```text
1. AutoProcessor.from_pretrained(ckpt)
2. processor.image_processor.size = {"longest_edge": N_max*32*32, "shortest_edge": N_min*32*32}
   processor.video_processor.size = {"longest_edge": N_max*32*32*2, "shortest_edge": N_min*32*32*2}
3. （可选）apply_chat_template(..., fps=...) 或 (..., num_frames=..., fps=None) 控制视频采样
4. 之后所有 tokenize=True 的调用都按新预算执行
```

#### 4.2.3 源码精读

**官方预算配置代码**（[README.md:L349-L356](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L349-L356)）：L352 给 image_processor 设 `{"longest_edge": 1280*32*32, "shortest_edge": 256*32*32}`——L351 注释点破换算：32 倍压缩下，这等于**单张图 256～1280 个视觉 token**；L355 给 video_processor 设 `{"longest_edge": 16384*32*32*2, "shortest_edge": 256*32*32*2}`——L354 注释说明是「32× 空间 + 2× 时间压缩」，即**单视频 256～16384 个 token**。注意赋值是**整体替换** size 字典，两个键都要写全。

**视频采样参数直传 processor**（[README.md:L358-L397](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L358-L397)）：L374-L375 注释说明默认 `fps=2`；L378-L385 示范在 `apply_chat_template` 调用里直接传 `fps=4`；L388-L397 示范 `num_frames=128` 并**同时显式传 `fps=None`** 覆盖之（帧数采样公式本身在 u2-l3 的 `smart_nframes` 已拆解，processor 内部是同一套逻辑的 processor 侧入口）。这组参数只在模式 A / processor 路径下生效；模式 B 的采样由 utils 的 `video_kwargs`（`do_sample_frames=False`）接管。

**patch_size 的权威来源**（[web_demo_mm.py:L181-L186](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L181-L186)）：`process_vision_info(messages, image_patch_size=processor.image_processor.patch_size, ...)`——不硬编码 16，而是从当前 processor 动态读取。这样 demo 代码对 Qwen2.5-VL（patch 14）与 Qwen3-VL（patch 16）都天然正确，u2-l1 说的「最佳实践」在仓库里的落地就是这一行。

#### 4.2.4 代码实践

1. **实践目标**：验证「size 预算 → input_ids 长度」的因果链，并把结果写成笔记。
2. **操作步骤**（示例代码；选一张**高分辨率**图片，太小的图会一直贴着下限、看不出差异）：

```python
# 示例代码：u3_l1_practice_b.py
from transformers import AutoProcessor

ckpt = "Qwen/Qwen3-VL-2B-Instruct"
processor = AutoProcessor.from_pretrained(ckpt)
print("默认 image size:", processor.image_processor.size)   # 先看默认预算

messages = [{
    "role": "user",
    "content": [
        {"type": "image", "image": "/path/to/huge_photo.jpg"},   # 换成本地大图
        {"type": "text", "text": "Describe this image."},
    ],
}]

def probe(tag):
    inputs = processor.apply_chat_template(
        messages, tokenize=True, add_generation_prompt=True,
        return_dict=True, return_tensors="pt")
    L = inputs["input_ids"].shape[1]
    t, h, w = inputs["image_grid_thw"][0].tolist()
    print(f"[{tag}] input_ids 长度={L}, grid=({t},{h},{w}), 视觉 token={t*h*w}")
    return L

L_default = probe("默认预算")

processor.image_processor.size = {"longest_edge": 1280*32*32, "shortest_edge": 256*32*32}
L_custom = probe("1280/256 token 预算")

with open("notes.md", "a", encoding="utf-8") as f:
    f.write(f"| 默认 | {L_default} |\n| 1280/256 | {L_custom} |\n")
```

3. **需要观察的现象**：两次调用的 `input_ids` 长度差应约等于两次视觉 token 数之差（文本部分不变）；`h × w × 1024`（即像素数）落在 `[256*32*32, 1280*32*32]` 区间内。
4. **预期结果**：自定义预算下视觉 token ≤ 1280；长度差 = 两次 `t*h*w` 之差。默认预算的具体数值以打印的 `processor.image_processor.size` 为准，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 video_processor 的预算数字要额外乘 2？
答案：视频在时间维还有 2× 压缩（相邻两帧合并，u2-l3 的 `FRAME_FACTOR=2`），1 个视频 token 平均消耗 \( 32 \times 32 \times 2 \) 个像素，所以按目标 token 数反推 size 时要乘 2（[README.md:L354-L355](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L354-L355)）。

**练习 2**：把 `processor.image_processor.size` 设成 `{"longest_edge": 256*32*32, "shortest_edge": 256*32*32}`（上下限相等）意味着什么？
答案：锁定单张图恰好 256 个视觉 token（u2-l2 讲过因取整方向实际可能略低）——评测或对齐实验中「固定视觉 token 数」的常用手法，代价是长宽比不同的图会被不同程度地拉伸或裁剪策略处理。

**练习 3**：同一个脚本里先走模式 A（processor resize），又给元素加了 `max_pixels`，两个预算谁生效？
答案：元素级 `max_pixels` 是 qwen-vl-utils 的参数，模式 A 根本不经过 utils，所以 processor 的 `size` 生效；元素级预算只在模式 B（`process_vision_info`）里被读取。两层旋钮各管各的路径。

### 4.3 add_vision_id：多模态输入自动编号

#### 4.3.1 概念说明

默认渲染下，图像/视频内容是「裸」地嵌进对话的。多图多轮对话中，用户说「第二张图里的狗」，模型只能靠位置隐式推断「第二张」是哪个。`add_vision_id=True` 让模板在每个视觉块前自动插入一段**普通文本编号**（`Picture N: ` / `Video N: `），把隐式的位置信息显式化——用户与模型从此有了一个共同的指代锚点。

#### 4.3.2 核心流程

编号规则（由官方示例的预期输出可直接读出）：

- 图像序列独立编号：`Picture 1`、`Picture 2`、`Picture 3`……跨轮次**持续累加**，不因新开一轮而重新计数；
- 视频序列独立编号：`Video 1`、`Video 2`……与图像计数互不干扰；
- 编号文本插在 `<|vision_start|>` **之前**、属于提示串的普通文本 token（不是特殊 token）。

```text
content: [text, image, image, video, text]
默认:    text <vis_start><image_pad><vis_end> <vis_start><image_pad><vis_end> <vis_start><video_pad><vis_end> text
加编号:  text Picture 1: <vis_start>... Picture 2: <vis_start>... Video 1: <vis_start>... text
```

#### 4.3.3 源码精读

**功能说明**（[README.md:L690-L691](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L690-L691)）：官方定位——默认图像与视频内容直接嵌入对话，处理多图时加标签便于指代，行为由用户开关控制。

**示例对话**（[README.md:L696-L723](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L696-L723)）：五轮对话含 1 图（首轮）+ 第二个 user 轮里 2 图 1 视频混排，是观察「跨轮累加」与「图/视频独立计数」的最佳样本。注意该示例的 `{"type": "image"}` 元素**不带图像数据**，因此它展示的是模板渲染层面的结果——自己复现时要显式传 `tokenize=False` 拿字符串（带真实图像时可继续 `tokenize=True`）。

**两种渲染的预期输出对照**（[README.md:L725-L729](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L725-L729) 默认 vs [README.md:L732-L736](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L732-L736) `add_vision_id=True`）：L736 的串里可以看到 `Picture 1: <|vision_start|><|image_pad|><|vision_end|>`、`Picture 2: ... Picture 3: ... Video 1: <|vision_start|><|video_pad|><|vision_end|>`——首个 user 轮的图是 Picture 1，第二个 user 轮的两张图接着是 Picture 2、3，视频单独从 Video 1 起数。

#### 4.3.4 代码实践

1. **实践目标**：亲手渲染出与 README 预期一致的编号串。
2. **操作步骤**（示例代码；无图像数据也能跑，与官方示例同构）：

```python
# 示例代码：u3_l1_practice_c.py
from transformers import AutoProcessor

processor = AutoProcessor.from_pretrained("Qwen/Qwen3-VL-2B-Instruct")
conversation = [
    {"role": "user", "content": [{"type": "image"},
                                  {"type": "text", "text": "Hello, how are you?"}]},
    {"role": "assistant", "content": "I'm doing well, thank you."},
    {"role": "user", "content": [
        {"type": "text", "text": "Describe these images and video?"},
        {"type": "image"}, {"type": "image"}, {"type": "video"},
        {"type": "text", "text": "These are from my vacation."}]},
]

s_default = processor.apply_chat_template(
    conversation, tokenize=False, add_generation_prompt=True)
s_with_id = processor.apply_chat_template(
    conversation, tokenize=False, add_generation_prompt=True, add_vision_id=True)
print(s_default)
print("=" * 40)
print(s_with_id)
```

3. **需要观察的现象**：`s_default` 里三个视觉块前没有任何文字标签；`s_with_id` 里依次出现 `Picture 1:`、`Picture 2:`、`Picture 3:`、`Video 1:`，且都位于 `<|vision_start|>` 之前。
4. **预期结果**：与 [README.md:L736](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L736) 的预期串结构一致（assistant 回复内容因 messages 简写而略有出入属正常）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`Picture N` 的 N 会不会因为换了一轮对话而重新从 1 开始？
答案：不会。README 示例中第一个 user 轮的图是 `Picture 1`，隔了一个 assistant 轮之后，第二个 user 轮的图接着编号 `Picture 2`、`Picture 3`——编号沿**整段对话**累加。

**练习 2**：`add_vision_id` 加进来的「Picture 1:」会被 `skip_special_tokens=True` 过滤掉吗？
答案：不会。它是普通文本 token，不是特殊 token；而 `<|vision_start|>` 等会被过滤。

**练习 3**：既然编号只是普通文本，为什么不手工在 text 元素里写「第一张图：」达到同样效果？
答案：可以，且效果等价——`add_vision_id` 的价值在于**自动**保证编号与视觉元素的实际顺序一致、免维护；手工编号在插入/删除图像时容易漏改错位。

### 4.4 batch 推理与左填充

#### 4.4.1 概念说明

batch 推理把多条样本的 `input_ids` 叠成一个矩形张量 \([B, L_{\max}]\)，但各样本长度天然不同，必须补齐（padding）。**关键约定：生成式模型必须左填充**——新生成 token 是接在每条序列**末尾**续写的，若把 pad 补在右侧，pad 会插在「提示结束」与「新生成」之间，模型把无意义的 pad 当成待续写内容；补在左侧则所有样本的真实内容都在序列右端对齐，`generate` 从同一位置起续写。

#### 4.4.2 核心流程

```text
两条样本（长度 12 与 35，pad_token 记为 PAD）
右填充（错误示范）:  [c₁..c₁₂  PAD PAD ... PAD]   ← 新 token 会生成在 PAD 之后
左填充（正确）:      [PAD PAD ... PAD c₁..c₁₂]   ← 真实内容右端对齐
                     [c₂₁ ... c₂₃₅            ]

attention_mask 与 input_ids 同形：pad 位置为 0，真实 token 为 1（左填充 ⇒ 每行 0 集中在开头）
裁剪依然成立: generated_ids[i] 的前 L_max 个是输入（含 pad），
             out_ids[len(in_ids):] 依旧正确裁掉整个输入前缀
```

注意一个容易忽略的事实：**batch 里可以混合多模态样本与纯文本样本**——视觉张量沿 batch 维独立拼接，文本侧的 padding 完全不干扰 `pixel_values` / `image_grid_thw`。

#### 4.4.3 源码精读

**左填充的两行设置**（[README.md:L292-L293](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L292-L293)）：L292 注释直言「batch generation 必须 left」，L293 `processor.tokenizer.padding_side = 'left'`——注意改的是 **tokenizer** 的属性（padding 由 tokenizer 执行，见 [web_demo_mm.py:L228](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L228) 中 processor 持有 tokenizer 的三合一结构）。

**混合模态 batch 的样本构造**（[README.md:L296-L311](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L296-L311)）：`messages1` 含两张图，`messages2` 是带 system 轮的**纯文本**对话；L311 `messages = [messages1, messages2]` 组成「列表的列表」——外层是 batch 维，内层才是一条对话。

**调用时的 `padding=True`**（[README.md:L314-L321](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L314-L321)）：L320 在 `apply_chat_template` 里显式传 `padding=True`（L320 行内注释：batch generation 必须设置）。**设了 `padding_side` 却忘传 `padding=True`（或反之）都会让 batch 失效**，两者是一对。

**裁剪与解码对 batch 的兼容**（[README.md:L325-L332](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L325-L332)）：与单样本完全同款的 `zip(inputs.input_ids, generated_ids)` + `out_ids[len(in_ids):]`——左填充下每条 `len(in_ids)` 都等于 \( L_{\max} \)，逐样本裁剪天然正确，无需任何改动。

#### 4.4.4 代码实践

1. **实践目标**：观察左填充在张量上的真实形态，验证裁剪逻辑不受 padding 影响。
2. **操作步骤**（示例代码；最后一步生成可选，只看张量可跳过加载模型）：

```python
# 示例代码：u3_l1_practice_d.py
from transformers import AutoProcessor

processor = AutoProcessor.from_pretrained("Qwen/Qwen3-VL-2B-Instruct")
processor.tokenizer.padding_side = "left"          # 设置一：方向

messages1 = [{"role": "user", "content": [
    {"type": "image", "image": "/path/to/a.jpg"},
    {"type": "text", "text": "What is in this picture?"}]}]
messages2 = [{"role": "user", "content": [
    {"type": "text", "text": "Who are you?"}]}]

inputs = processor.apply_chat_template(
    [messages1, messages2],                        # 外层 = batch 维
    tokenize=True, add_generation_prompt=True,
    return_dict=True, return_tensors="pt",
    padding=True,                                  # 设置二：开关（与设置一成对）
)
print("input_ids:", inputs["input_ids"].shape)
print("attention_mask 每行前 8 个:", inputs["attention_mask"][:, :8])
print("每行 padding 个数:", (inputs["attention_mask"] == 0).sum(dim=1).tolist())
pad_id = processor.tokenizer.pad_token_id
print("pad_token:", processor.tokenizer.pad_token, "开头是否为 pad:",
      (inputs["input_ids"][:, 0] == pad_id).tolist())
```

3. **需要观察的现象**：`attention_mask` 的 0 **全部集中在每行开头**；较短样本（纯文本那条）所在行的 0 更多；`input_ids` 每行长度相同（等于 batch 内最长序列）。
4. **预期结果**：若再加载模型执行 `model.generate(**inputs, max_new_tokens=32)` 并用 `out_ids[len(in_ids):]` 裁剪，两条输出都应是干净的新生成文本、不含 pad 与提示词。具体 pad 数量取决于两条样本的长度差，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：只用 `padding=True` 而不改 `padding_side`，最可能看到什么现象？
答案：tokenizer 默认右填充，pad 补在序列右侧。生成会把 pad 当成提示的一部分续写下去，输出常以无意义内容开头，甚至永不命中结束符；同时「真实提示右端对齐」被破坏。

**练习 2**：左填充后，`inputs["input_ids"].shape[1]` 对两条样本分别意味着什么？
答案：都等于 batch 内最长序列的长度 \( L_{\max} \)——含 pad。因此逐样本 `len(in_ids)` 相同，`out_ids[len(in_ids):]` 恰好裁掉「pad + 提示」的完整输入前缀。

**练习 3**：batch 中一条含 2 张图、一条纯文本，`pixel_values` 会为纯文本样本补空张量吗？
答案：不会。视觉张量只按实际存在的图像拼接（`image_grid_thw` 逐图记录网格），文本侧 padding 与视觉侧互不相干——[README.md:L296-L311](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L296-L311) 的官方 batch 示例正是这种混合模态组合。

## 5. 综合实践

**任务：写一个 `processor_probe.py` 体检脚本，产出一份《processor 行为笔记》。** 把本讲四个模块串成一个可复跑的脚本，对**同一组 messages**（1 张高分辨率图片 + 1 段提问）依次完成：

1. **渲染检查**（4.1）：`tokenize=False` 打印提示串，人工标注 `<|im_start|>` / `<|vision_start|>` / `<|image_pad|>` / `<|im_end|>` 各出现几次。
2. **展开验证**（4.1 + 4.2）：`tokenize=True` 下统计 `input_ids` 中 `<|image_pad|>` 的个数，与 `image_grid_thw` 三元素乘积对照，再验证乘积 × 1024（像素数）落在当前预算区间内。
3. **预算对照**（4.2）：分别在「默认预算」与 `{"longest_edge": 1280*32*32, "shortest_edge": 256*32*32}` 两档下记录 `input_ids` 长度与视觉 token 数，写进 `notes.md` 的表格，并用一句话解释长度差来源。
4. **编号对照**（4.3）：把 messages 扩成「2 图 + 1 视频占位」的多轮对话，`add_vision_id` 两种取值各渲染一次，把 diff 行摘进笔记。
5. **batch 检查**（4.4）：把（多模态样本，纯文本样本）组成 batch，`padding_side='left'` + `padding=True`，记录 `input_ids` 形状与每行 padding 数。

**验收标准**：`notes.md` 中应能回答——这张图在你的默认预算下占多少视觉 token？改预算后 `input_ids` 长度变化了多少、差值是否恰等于视觉 token 数之差？左填充的 0 在张量的哪一侧？全部结论都来自你自己的运行输出（本环境无 GPU/未装 transformers，结果**待本地验证**）。

## 6. 本讲小结

- `apply_chat_template` 有两种模式：`tokenize=True` 一步返回含 `pixel_values` / `image_grid_thw` 的张量（transformers 路径）；`tokenize=False` 只返回字符串、每个图像仅 1 个 `<|image_pad|>` 占位（配合 qwen-vl-utils + `do_resize=False`，或交给 vLLM）。web_demo_mm.py 的两个后端分支就是两种模式的活样本。
- `<|image_pad|>` 在编码阶段展开为 \( \frac{H'}{32} \times \frac{W'}{32} \) 个，因此 **size 预算 → 图像实际尺寸 → input_ids 长度**是一条因果链。
- processor 级预算分治：`image_processor.size` 的 longest/shortest_edge 约束**单图** \( H \times W \)；`video_processor.size` 约束**全帧总和** \( T \times H \times W \)；按目标 token 数配置时分别乘 \( 32 \times 32 \) 与 \( 32 \times 32 \times 2 \)。
- `add_vision_id=True` 在每个视觉块前插入普通文本编号（图像 `Picture N`、视频 `Video N` 独立累加，跨轮不断档），为多图对话提供显式指代锚点。
- batch 推理两件套缺一不可：`processor.tokenizer.padding_side = 'left'` + `padding=True`；左填充让真实内容右端对齐，pad 位置 `attention_mask=0`，逐样本 `out_ids[len(in_ids):]` 裁剪无需任何修改，且 batch 可混排多模态与纯文本样本。

## 7. 下一步学习建议

- **下一讲 u3-l2（vLLM 离线推理与 prepare_inputs_for_vllm 模式）**：本讲模式 B 的终点正是它的起点——`prompt` / `multi_modal_data` / `mm_processor_kwargs` 三件套如何在 vLLM 引擎里落地，建议先精读 [web_demo_mm.py:L178-L198](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L178-L198) 带着问题过去。
- 想看 processor 的**实现**（chat template 的 Jinja 模板、`Qwen3VLImageProcessor` 的 resize 细节）：读本机安装的 transformers ≥ 4.57.0 源码中 `models/qwen3_vl/` 目录与模型仓库里的 `chat_template.json`、`preprocessor_config.json`，对照本讲的行为断言逐条验证。
- 想理解 `image_grid_thw` 里的网格如何变成 3D 位置 id：预习单元五的 rope2d.py（u5-l4），那是本仓库中最接近模型内部机制的源码。

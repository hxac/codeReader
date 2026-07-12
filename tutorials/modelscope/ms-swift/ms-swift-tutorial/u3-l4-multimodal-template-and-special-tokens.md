# 多模态 Template 与特殊 Token

## 1. 本讲目标

本讲承接 u3-l3 的纯文本 Template 体系，把视角扩展到「多模态」。学完后你应该能够：

- 说清 `<image>` / `<video>` / `<audio>` 这类占位符是如何从对话里被识别、被替换、再被扩展成真实 token 的。
- 理解一张图片的「像素」与一串「占位 token」是如何在数量上对齐的。
- 掌握 `vision_utils.py` 提供的图片/视频/音频加载与预处理工具，以及它们在不同后端下的差异。
- 判断一个多模态模板是否支持 `padding_free` / `packing`，并能解释为什么多模态默认不支持。

## 2. 前置知识

阅读本讲前，你需要先具备 u3-l3 建立的以下认知：

- **Template 的 encode 主链路**：`encode → _encode_truncated → _encode → _swift_encode → _encode_context_list`，把对话切成「上下文段」，再逐段 tokenize。
- **ContextType 与 LossScale**：每一段上下文会被打上类型标签（RESPONSE/SUFFIX/OTHER），据此决定是否计入 loss。
- **TEMPLATE_MAPPING 与 `register_template`**：以「导入即注册」方式收录各模型对话格式。

本讲在此基础上回答一个新问题：**当对话里出现一张图、一段视频或一段音频时，Template 怎么把它「塞进」token 序列？** 多模态模型无法直接吃像素，它只能吃 token embedding。所以核心矛盾是——

> 文本可以逐字 tokenize，但图像是一块连续的像素张量。我们需要在文本序列里「挖一个洞」，把这个洞的大小标出来，再把视觉编码器算出的图像 embedding 「填」进去。

ms-swift 用「**占位符 → 替换 → 扩展 → 填充 embedding**」四步解决了这个矛盾。本讲三个最小模块分别对应：占位符机制（挖洞）、`vision_utils`（把像素读进来）、`support_padding_free`（多模态对训练效率优化的特殊约束）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [swift/template/base.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py) | Template 基类。定义 `special_tokens`、占位符常量、`replace_tag`、`_pre_tokenize_images`、`_extend_tokens`、`_init_placeholder_tokens`、`_truncate`、`_data_collator`、`_get_inputs_embeds_hf` 等多模态核心逻辑。 |
| [swift/template/template_inputs.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/template_inputs.py) | `StdTemplateInputs` 数据类。持有 `images/videos/audios/objects` 字段，并提供 `remove_messages_media` 把 OpenAI 风格的 content list 拆成「占位符 + 媒体列表」。 |
| [swift/template/vision_utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/vision_utils.py) | 媒体加载工具箱。`load_image`/`load_file`/`load_video_hf`/`load_audio`/`rescale_image` 等，统一处理本地路径、HTTP、base64 三种来源。 |
| [swift/template/grounding.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/grounding.py) | 检测/定位（grounding）任务的画框工具 `draw_bbox`，配合 `norm_bbox='norm1000'` 把 0–1000 归一化坐标还原到像素坐标。 |
| [swift/template/templates/qwen.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py) | `Qwen2VLTemplate` 是最典型的多模态模板实现，覆盖了 `replace_tag` 与 `_encode`，演示占位符如何按图像分辨率被扩展。 |
| [swift/pipelines/train/sft.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py) | SFT 主流程里 `_prepare_template` 一段，落地 `support_padding_free` 的运行时判定与报错。 |

## 4. 核心概念与源码讲解

### 4.1 特殊 token 与占位符机制

#### 4.1.1 概念说明

ms-swift 定义了一组**特殊 token**，它们在对话文本里以普通字符串形式出现，但在 tokenize 之前/之后会被特殊处理：

```
special_tokens = ['<image>', '<video>', '<audio>', '<bbox>', '<ref-object>', '<cot-process>', '<start-image>']
```

这些 token 扮演两类角色：

1. **媒体占位符**：`<image>` / `<video>` / `<audio>`。它们出现在对话内容里，代表「这里有一张图/一段视频/一段音频」。
2. **结构化标注**：`<bbox>`（检测框）、`<ref-object>`（被引用的目标）、`<cot-process>`（过程奖励标签）、`<start-image>`（图像生成模式标记）。

注意区分两个容易混淆的概念：

- **`special_tokens`**：框架内部用于「拆分文本」的标记集合，是一个固定的字符串列表。
- **`placeholder_tokens`**：具体某个模型在词表里**真实的占位 token**（如 Qwen2-VL 的 `<|image_pad|>`、`<|video_pad|>`）。它一开始可能是字符串，`_init_placeholder_tokens` 会把它转成 token id，供 truncate、collator、embedding 填充时识别。

> 直觉：`<image>` 是「读者写在对话里的标记」，`<|image_pad|>` 是「模型词表里真正会被图像 embedding 替换的那个 token」。Template 的工作就是从前者的位置，导出后者的数量。

#### 4.1.2 核心流程

一条多模态样本从原始对话到最终 `input_ids`，占位符要经历四个阶段：

```text
① 解析对话 content list
   OpenAI 风格 [{'type':'image_url','image_url':{'url':...}}]
        │  remove_messages_media
        ▼
② 占位符 + 媒体分离
   content 文本里的图被替换成 '<image>'，
   媒体本身被收集进 inputs.images 列表
        │  _pre_tokenize_images → replace_tag
        ▼
③ 替换为模型占位 token
   '<image>' → '<|vision_start|><|image_pad|><|vision_end|>'
   （此时只有一个 <｜image_pad｜>）
        │  _encode（VL 子类）→ _extend_tokens
        ▼
④ 按分辨率扩展
   一个 <｜image_pad｜> 被复制成 N 个，
   N 由图像网格 image_grid_thw 决定
        │  前向时 _get_inputs_embeds_hf
        ▼
   N 个 token 的位置被视觉 embedding 填充
```

关键设计点：

- **顺序很重要**。第 ② 步发生在 tokenize 之前（`_pre_tokenize`），所以 `<image>` 这种字符串标记必须先被「切开」（`_split_special_tokens`），才能在普通文本段里精确替换。
- **占位符的 loss 被置零**。图像/视频/音频占位段不参与语言建模 loss（`loss_scale = 0.`），因为模型不该「学会生成」这些占位 token——它们只在前向时被 embedding 覆盖。
- **截断时占位符受保护**。当序列超长需要截断时，占位 token 会被优先保留，避免「图被截掉了但 inputs 里还留着这张图的像素」这种错位。

#### 4.1.3 源码精读

**(1) 特殊 token 与占位符常量** —— [swift/template/base.py:56-63](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L56-L63)

```python
special_tokens = ['<image>', '<video>', '<audio>', '<bbox>', '<ref-object>', '<cot-process>', '<start-image>']
special_keys = ['images', 'videos', 'audios', 'objects']

image_placeholder = ['<image>']
video_placeholder = ['<video>']
audio_placeholder = ['<audio>']
placeholder_tokens = []  # For clearer printing
```

`special_tokens` 是「切分文本用的标记」，`*_placeholder` 是基类对各模态占位符的默认产物。`placeholder_tokens` 初始为空，会在初始化时按具体模型填充。

**(2) 把占位 token 解析为 id** —— [swift/template/base.py:247-257](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L247-L257)

```python
def _init_placeholder_tokens(self):
    for mm_type in ['image', 'video', 'audio']:
        mm_token = getattr(self.processor, f'{mm_type}_token', None)
        mm_token_id = getattr(self.processor, f'{mm_type}_token_id', None)
        if mm_token_id is not None and mm_token_id not in self.placeholder_tokens:
            self.placeholder_tokens.append(mm_token_id)
        elif mm_token is not None and mm_token not in self.placeholder_tokens:
            self.placeholder_tokens.append(mm_token)
    for i, token in enumerate(self.placeholder_tokens):
        if isinstance(token, str):
            self.placeholder_tokens[i] = self.tokenizer.convert_tokens_to_ids(token)
```

这段在 `init_processor` 时执行：从 HF `processor` 上读 `image_token` / `image_token_id`（优先用 id，避免分词歧义），把模型真实的占位 token 收集进 `placeholder_tokens`，并保证最终存的都是 id。子类（如 `Qwen2VLTemplate`）也可以直接用类属性 `placeholder_tokens = ['<|image_pad|>', '<|video_pad|>']` 覆盖声明。

**(3) 用 special_tokens 切分文本** —— [swift/template/base.py:887-904](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L887-L904)

```python
@staticmethod
def _split_special_tokens(context_list, loss_scale_list):
    """Split special tokens, for example `<image>`, `<video>`, this will help the replace_tag operation"""
    res, loss_scale_res = [], []
    for context, loss_scale in zip(context_list, loss_scale_list):
        contexts = []
        if isinstance(fetch_one(context), str):
            for d in split_str_parts_by(context, Template.special_tokens):
                contexts.extend([d['key'], d['content']])
            contexts = [c for c in contexts if c]
            res.extend(contexts)
            loss_scale_res.extend([loss_scale] * len(contexts))
        else:
            res.append(context)
            loss_scale_res.append(loss_scale)
    return res, loss_scale_res
```

一段像 `"看这张<image>图"` 的文本会被切成 `['看这张', '<image>', '图']`，使 `<image>` 成为独立元素，便于后续逐段处理。

**(4) 基类的 `replace_tag`：默认行为** —— [swift/template/base.py:909-937](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L909-L937)

```python
def replace_tag(self, media_type, index, inputs):
    if media_type == 'image':
        if self.mode == 'lmdeploy':
            return [[-100]]
        return self.image_placeholder          # ['<image>']
    elif media_type == 'video':
        if self.mode == 'vllm':
            ...load_vllm_video(...)
            return self.video_placeholder
        else:
            return self.video_placeholder      # ['<video>']
    elif media_type == 'audio':
        return self.audio_placeholder          # ['<audio>']
```

`replace_tag` 是「把标准标记 `<image>` 翻译成模型需要的占位形式」的钩子，**子类通常覆盖它**。基类默认返回的就是 `<image>` 字符串本身（一些老模型直接把 `<image>` 当词表里的特殊 token 用）。

**(5) 触发替换：`_pre_tokenize_images`** —— [swift/template/base.py:986-1003](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L986-L1003)

```python
def _pre_tokenize_images(self, context_list, loss_scale_list, inputs):
    res, res_loss_scale = [], []
    inputs.image_idx = 0
    for context, loss_scale in zip(context_list, loss_scale_list):
        if context == '<image>' and inputs.is_multimodal and inputs.image_idx < len(inputs.images):
            c_list = self.replace_tag('image', inputs.image_idx, inputs)  # 调子类
            inputs.image_idx += 1
            loss_scale = 0. if self.template_backend == 'swift' else 1.   # 占位符不计 loss
        else:
            c_list = [context]
        res += c_list
        res_loss_scale += [loss_scale] * len(c_list)
    return res, res_loss_scale
```

注意两点：每遇到一个 `<image>` 就消费 `inputs.images` 里的下一张图（用 `image_idx` 计数），且占位段的 `loss_scale` 被设成 0（swift 后端下），这样回答段才不会去「学习预测」图像 token。

**(6) VL 子类的 `replace_tag`：以 Qwen2-VL 为例** —— [swift/template/templates/qwen.py:329-343](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py#L329-L343)

```python
def replace_tag(self, media_type, index, inputs):
    from qwen_vl_utils import fetch_image, fetch_video
    ...
    if media_type == 'image':
        inputs.images[index] = fetch_image({'image': inputs.images[index], **inputs.chat_template_kwargs}, **kwargs)
        if self.mode == 'lmdeploy':
            return ['<|vision_start|>', [-100], '<|vision_end|>']
        else:
            return ['<|vision_start|><|image_pad|><|vision_end|>']
```

注意：这里返回的只有一个 `<|image_pad|>`！真正的「按分辨率扩展」发生在后面的 `_encode` 里。`[-100]` 是给 lmdeploy 后端用的「占位洞」（prepare 阶段再填）。

**(7) 按分辨率扩展占位符：`_extend_tokens`** —— [swift/template/base.py:443-470](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L443-L470) 与 [swift/template/templates/qwen.py:408-417](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py#L408-L417)

Qwen2-VL 的 `_encode` 在拿到 `image_grid_thw` 后调用：

```python
idx_list = findall(input_ids, media_token)         # 找到每个 <|image_pad|> 的位置
merge_length = processor.image_processor.merge_size**2

def _get_new_tokens(i):
    token_len = (media_grid_thw[i].prod() // merge_length)   # 该图要展开成多少个 token
    return [media_token] * token_len

input_ids, labels, loss_scale, mm_mask = self._extend_tokens(
    input_ids, labels, loss_scale, idx_list, _get_new_tokens, mm_mask=mm_mask)
```

`_extend_tokens` 的核心是「原地用 N 个新 token 替换位置上的 1 个 token」：

```python
input_ids = input_ids[:idx + added_tokens_len] + new_tokens + input_ids[added_tokens_len + idx + 1:]
```

并同步把 `labels` 填 `-100`、`mm_mask` 标 `True`。这样一来，一张图对应多少个 `<|image_pad|>` 完全由它的网格尺寸决定（见 4.1.2 末的公式）。

**(8) 截断保护占位符** —— [swift/template/base.py:1388-1402](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1388-L1402)

```python
placeholder_tokens = torch.tensor(self.placeholder_tokens)
input_ids_tensor = torch.tensor(input_ids)
protected = (input_ids_tensor[:, None] == placeholder_tokens).any(dim=-1)
n_protected = protected.sum().item()
if n_protected < self.max_length:
    non_protected = (~protected).nonzero(as_tuple=True)[0]
    ...  # 只从「非占位」token 里挑出要保留/截断的部分
    protected[idx] = True
```

含义：超长截断时，占位 token 视为「不可丢弃」，只在普通文本 token 的额度内做 left/right 截断。否则就会出现「文本没了但图像 embedding 还在」的错位。

**(9) 前向时用图像 embedding 填洞** —— [swift/template/base.py:2278-2288](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L2278-L2288)

```python
if image_embeds is not None:
    image_mask = (input_ids == config.image_token_id).unsqueeze(-1).expand_as(inputs_embeds)
    inputs_embeds = inputs_embeds.masked_scatter(image_mask, image_embeds)
```

`masked_scatter` 把视觉编码器算出的 `image_embeds` 按顺序「灌」进所有 `image_token_id` 位置——这就是「像素与占位 token 对齐」的最终落地：**N 个占位 token 严格等于 N 个图像 patch embedding**。

#### 4.1.4 代码实践

**实践目标**：观察一条带图对话里，`<image>` 如何先被替换成单个 `<|image_pad|>`，再被按图像分辨率扩展成多个，并理解像素与 token 的对齐关系。

**操作步骤**：

1. 准备一张本地小图（例如 `cat.jpg`，分辨率越小越省显存）。
2. 用纯文本模型做对照，理解「占位符替换前」是什么样：阅读 [swift/template/base.py:986-1003](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L986-L1003)（`_pre_tokenize_images`），确认 `<image>` 在这一步会被 `replace_tag` 的返回值替换。
3. 用 Qwen2-VL 跑一次推理，开启 debug 打印 input_ids：

```bash
# 示例命令（实际执行需本地具备 GPU 与模型权重，部分行为待本地验证）
SWIFT_DEBUG=1 swift infer \
  --model Qwen/Qwen2.5-VL-7B-Instruct \
  --infer_backend pt \
  --messages '[{"role":"user","content":[{"type":"image_url","image_url":{"url":"cat.jpg"}},{"type":"text","text":"这是什么？"}]}]'
```

4. 在日志里找到 `[INPUT_IDS]` 一行，会看到形如 `... <|vision_start|> [<|image_pad|> * N] <|vision_end|> ...` 的展开结果（`safe_decode` 会把连续占位 token 折叠成 `[id * 数量]`，见 [swift/template/base.py:2200-2204](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L2200-L2204)）。记录这个 `N`。
5. 用对齐公式手算验证。对 Qwen2-VL，单张图的 token 数为：

\[
N_{\text{img}} = \frac{T \cdot H \cdot W}{\text{merge\_size}^2}
\]

其中 \((T,H,W)\) 是 `image_grid_thw`（时间/高/宽三个维度的 patch 数，单图 \(T=1\)），`merge_size` 一般为 2。也就是说图像被切成 \(\frac{H \cdot W}{4}\) 个视觉 token。

**需要观察的现象**：

- 同一张图，分辨率越大，`N` 越大（占位 token 越多），`input_ids` 越长。
- 不同分辨率的两张图，`N` 不同，但每张图各自「占位 token 数 == 视觉 embedding 数」严格相等。

**预期结果**：日志里 `[INPUT_IDS]` 的占位段长度等于 `image_grid_thw.prod() // 4`，且与 `pixel_values` 经视觉编码器后产出的 embedding 行数一致。

**待本地验证**：具体 `N` 取决于模型的 `patch_size`、`merge_size` 与图像实际分辨率；上述命令在无 GPU/无权重环境下无法运行，可改为纯源码阅读型实践（跟踪 4.1.3 的 7 个代码点）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_pre_tokenize_images` 要把占位段的 `loss_scale` 设成 0？

**参考答案**：占位 token（如 `<|image_pad|>`）在前向时会被图像 embedding 覆盖，它本身不是模型应当「生成」的目标。如果让它计入 loss，模型会被鼓励去预测这些占位 token，反而干扰正常的语言建模。

**练习 2**：如果一张图在 tokenize 后只剩 1 个 `<|image_pad|>`，但视觉编码器算出了 256 个 embedding，前向时会发生什么？

**参考答案**：会出错。`masked_scatter` 要求被填入的张量数等于掩码中 `True` 的数量。正因为如此，`_encode` 必须先用 `_extend_tokens` 把 1 个占位 token 扩展成正好 256 个，二者数量严格对齐才能正确填充。

**练习 3**：`special_tokens` 与 `placeholder_tokens` 有何区别？

**参考答案**：`special_tokens` 是框架用于「切分文本」的固定字符串标记集合（如 `<image>`），作用于 tokenize 之前；`placeholder_tokens` 是具体模型词表里真实的占位 token id（如 `<|image_pad|>` 的 id 151655），作用于 tokenize 之后——truncate 保护、collator 拼接、embedding 填充都靠它识别「哪些位置是媒体」。

---

### 4.2 vision_utils 多模态媒体加载

#### 4.2.1 概念说明

`vision_utils.py` 是 Template 的「媒体入库口」。无论图片来自本地路径、HTTP 链接还是 base64 字符串，框架都希望用统一接口把它变成 `PIL.Image.Image`（或 numpy 帧、音频波形）。这层抽象的意义在于：

- **数据格式多样**：用户数据集里的图片字段可能是文件路径、URL、`data:image/...;base64,...`、甚至原始 bytes。
- **后端差异**：transformers/vllm/lmdeploy/sglang 对媒体的期望格式不同，加载策略需要分支。
- **资源约束**：图片太大直接撑爆显存，需要 `max_pixels` 做等比缩放。

#### 4.2.2 核心流程

媒体加载在 Template 主链路里的位置（`_preprocess_inputs`）：

```text
inputs.images = ['http://.../a.jpg', '/data/b.png', '<base64>']
        │  _load_image → load_file
        ▼
统一变成 PIL.Image（RGB）
        │  若设了 max_pixels：rescale_image 等比缩小
        ▼
交给具体模板的 replace_tag / _encode，
由 image_processor 算出 pixel_values 与 image_grid_thw
```

路径解析的三道判断：

1. 以 `http` 开头 → 用带重试的 `requests` 下载。
2. 看起来像路径且文件存在 → 直接读。
3. 否则尝试按 base64 解码 → 当成内嵌图片。

#### 4.2.3 源码精读

**(1) 路径/来源识别** —— [swift/template/vision_utils.py:104-125](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/vision_utils.py#L104-L125)

```python
def _check_path(path):
    """If it is a path, return the string; if it is base64, return None."""
    MAX_PATH_HEURISTIC = 2000
    if len(path) > MAX_PATH_HEURISTIC:
        return
    if os.path.exists(path):
        return os.path.abspath(path)
    ...  # ROOT_IMAGE_DIR 前缀拼接
    if data.startswith('data:'):
        return
    try:
        base64.b64decode(data)
        return                # base64，返回 None 表示「不是文件路径」
    except Exception:
        pass
    return data
```

启发式判断：超长字符串多半是 base64；`data:` 前缀是标准 base64 URL scheme；其余尝试 `os.path.exists`。还支持环境变量 `ROOT_IMAGE_DIR` 给相对路径加前缀（容器化部署常用）。

**(2) 统一读取入口 `load_file`** —— [swift/template/vision_utils.py:128-161](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/vision_utils.py#L128-L161)

```python
def load_file(path):
    res = path
    if isinstance(path, str):
        path = path.strip()
        if path.startswith('http'):
            retries = Retry(total=3, backoff_factor=1, allowed_methods=['GET'])
            with requests.Session() as session:
                ...  # 带重试下载，超时由 SWIFT_TIMEOUT 控制
                res = BytesIO(content)
        else:
            data = path
            path = _check_path(path)
            if path is None:           # base64
                ...  # 去掉 'data:...;base64,' 前缀后 b64decode
                res = BytesIO(data)
            else:
                with open(path, 'rb') as f:
                    res = BytesIO(f.read())
    elif isinstance(path, bytes):
        res = BytesIO(path)
    return res
```

最终都归一成 `BytesIO`，屏蔽了来源差异。下载超时受 `SWIFT_TIMEOUT` 环境变量控制（默认 20 秒，`<=0` 表示不限）。

**(3) `load_image`：变成 RGB 的 PIL 图** —— [swift/template/vision_utils.py:164-170](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/vision_utils.py#L164-L170)

```python
def load_image(image):
    image = load_file(image)
    if isinstance(image, BytesIO):
        image = Image.open(image)
    if image.mode != 'RGB':
        image = image.convert('RGB')
    return image
```

强制转 RGB——很多视觉模型预处理假设三通道。

**(4) 等比缩放 `rescale_image`** —— [swift/template/vision_utils.py:88-98](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/vision_utils.py#L88-L98)

```python
def rescale_image(img, max_pixels):
    width, height = img.width, img.height
    if max_pixels is None or max_pixels <= 0 or width * height <= max_pixels:
        return img
    ratio = width / height
    height_scaled = math.sqrt(max_pixels / ratio)
    width_scaled = height_scaled * ratio
    return T.Resize((int(height_scaled), int(width_scaled)))(img)
```

保持宽高比，把像素总数压到 `max_pixels`（H·W）以内。这个 `max_pixels` 来自 Template 构造参数（见 [swift/template/base.py:88](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L88)），是控制显存的关键旋钮。

**(5) `_preprocess_inputs` 把加载串进主链路** —— [swift/template/base.py:375-393](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L375-L393)

```python
images = inputs.images
load_images = self.load_images or self.mode in {'vllm', 'lmdeploy'}
...
max_pixels = self._get_max_pixels(inputs)
if max_pixels is not None or inputs.objects:
    load_images = True
if images:
    for i, image in enumerate(images):
        images[i] = self._load_image(images[i], load_images)
...
if max_pixels is not None:
    images = [rescale_image(img, max_pixels) for img in images]
```

注意 `load_images` 的条件：vllm/lmdeploy 后端会自己处理图片，这里就不提前 PIL 化；带 `objects`（grounding）时必须加载以算框的像素坐标；设了 `max_pixels` 也必须加载才能缩放。

**(6) 视频与音频** —— [swift/template/vision_utils.py:184-203](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/vision_utils.py#L184-L203)（视频）、[swift/template/vision_utils.py:397-410](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/vision_utils.py#L397-L410)（音频）

视频统一走 `load_video_hf`（底层用 transformers 的 `video_utils.load_video`，后端 `pyav` 可配），音频走 `load_audio`（默认 `librosa`，可切 `soundfile_pyav`）。它们的共同点是：把异构输入归一成「帧序列 / 波形数组 + 采样率」，再交给具体模板。

**(7) grounding 与 `norm_bbox`** —— [swift/template/grounding.py:45-58](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/grounding.py#L45-L58)

```python
def draw_bbox(image, ref, bbox, norm_bbox='norm1000'):
    for box in bbox:
        if norm_bbox == 'norm1000':
            box[0] = box[0] / 1000 * image.width
            box[2] = box[2] / 1000 * image.width
            box[1] = box[1] / 1000 * image.height
            box[3] = box[3] / 1000 * image.height
    ...
```

`norm_bbox='norm1000'` 是 ms-swift 的默认约定：检测框坐标用 0–1000 的归一化值存储（与分辨率无关），画框/计算时再按当前图像宽高还原成像素。这也是 Template 基类属性 `norm_bbox = 'norm1000'` 的来历（见 [swift/template/base.py:67](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L67)）。

#### 4.2.4 代码实践

**实践目标**：体验 `vision_utils` 对三种图片来源的统一处理，并观察 `max_pixels` 缩放效果。

**操作步骤**（纯 Python，无需 GPU）：

```python
# 示例代码：直接调用 vision_utils，不依赖完整 Template
from PIL import Image
from swift.template.vision_utils import load_image, rescale_image

# 1) 三种来源都能被 load_image 接受
img_local = load_image('/path/to/cat.jpg')          # 本地路径
# img_http  = load_image('https://example.com/cat.jpg')  # HTTP（需联网）
# img_b64   = load_image('data:image/png;base64,iVBORw0KGgo...')  # base64

print(type(img_local), img_local.size, img_local.mode)   # <PIL.Image> (W,H) RGB

# 2) 等比缩放到 512*512 像素以内
small = rescale_image(img_local, max_pixels=512*512)
print('before:', img_local.size, 'after:', small.size)
assert small.size[0] * small.size[1] <= 512*512 + 1     # 允许取整误差
```

**需要观察的现象**：

- 三种来源返回的都是 `PIL.Image.Image`，`mode` 为 `RGB`。
- `rescale_image` 后宽高比不变，像素总数下降到阈值以内。

**预期结果**：`after` 的 `W*H` 不超过 `max_pixels`，且 `after_W/after_H == before_W/before_H`。

**待本地验证**：换一张本身就小于阈值的图，确认它原样返回（不放大）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 vllm 后端下 `_preprocess_inputs` 不提前把图片 PIL 化？

**参考答案**：vllm（以及 lmdeploy）有自己的多模态预处理流水线，会在引擎内部按需读取、缩放图片。提前 PIL 化既浪费又可能与引擎内部的缩放重复甚至冲突（参考 [swift/template/templates/qwen.py:334-337](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py#L334-L337) 里 vllm 下设置 `do_resize=False` 的注释）。所以框架只在「需要」（设了 max_pixels、带 objects、非 vllm/lmdeploy）时才加载。

**练习 2**：`ROOT_IMAGE_DIR` 环境变量解决什么问题？

**参考答案**：在容器/分布式部署中，数据集里的图片路径常是相对路径，而实际运行目录不同。`ROOT_IMAGE_DIR` 给这些相对路径统一加前缀，避免「找不到文件」。见 [swift/template/vision_utils.py:112-114](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/vision_utils.py#L112-L114)。

**练习 3**：`norm_bbox='norm1000'` 为什么比直接存像素坐标更好？

**参考答案**：归一化到 0–1000 后，标注与图像分辨率解耦——同一张图缩放或换了版本，框的归一化值不变；只在画框/计算时按当前图像宽高还原（见 grounding.py 的 `box[0]/1000*image.width`）。这让数据集可以跨分辨率复用。

---

### 4.3 support_padding_free 约束

#### 4.3.1 概念说明

`padding_free` 与 `packing` 是提升训练效率的两项技术（详见 u4-l3）：

- **padding_free**：同 batch 内不同样本拼接成一条长序列，用 `position_ids` 区分边界，免去 padding 浪费。
- **packing**：把多条短样本拼进一个 `max_length` 桶，提高 token 利用率。

对纯文本模型，这两项「几乎总是可用」。但**多模态模型默认不支持**，原因在于：

> padding_free/packing 需要在拼好的长序列上重算 `position_ids`（如 Qwen2-VL 的 3D M-RoPE 位置编码）。而多模态的位置编码依赖 `image_grid_thw`、`attention_mask_2d` 等媒体形状信息，跨样本拼接后这些信息要重新对齐，实现复杂且容易出错。

因此 ms-swift 用一个类属性 `support_padding_free` 做显式声明：默认 `None`（按是否多模态推断），多模态模板若已适配则显式置 `True`，未适配的置 `False`。

#### 4.3.2 核心流程

```text
SwiftSft._prepare_template()
   │  template.support_padding_free
   ▼
是 None？── 是 ──▶ support_padding_free = not is_multimodal
   │                       （纯文本 True，多模态 False）
   否
   ▼
用户开了 --padding_free 或 --packing 且 support_padding_free 为 False？
   │
   是 ──▶ raise ValueError('Template `xxx` does not support padding free or packing.')
```

三种取值含义：

| `support_padding_free` | 含义 |
| --- | --- |
| `None`（基类默认） | 自动推断：纯文本→支持；多模态→不支持 |
| `True` | 该模板已适配，可开 padding_free/packing |
| `False` | 显式禁用，即便用户传了开关也报错 |

#### 4.3.3 源码精读

**(1) 基类默认值** —— [swift/template/base.py:68-69](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L68-L69)

```python
# For pure text models, the default is True; for multimodal models, the default is False.
support_padding_free = None
```

注释把推断规则讲得很清楚。

**(2) 运行时判定与报错** —— [swift/pipelines/train/sft.py:71-75](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L71-L75)

```python
support_padding_free = template.support_padding_free
if support_padding_free is None:
    support_padding_free = not args.model_meta.is_multimodal
if (args.padding_free or args.packing) and not support_padding_free:
    raise ValueError(f'Template `{args.template}` does not support padding free or packing.')
```

这段把「None → 按多模态推断」落地，并在用户强行开启时给出明确报错（而不是默默走错路径）。

**(3) 多模态模板显式声明** —— [swift/template/templates/qwen.py:312](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py#L312)

```python
class Qwen2VLTemplate(Template):
    ...
    support_padding_free = True
```

Qwen2-VL 通过覆盖 `packing_row` / `_get_position_ids` / `_data_collator`（见 [swift/template/templates/qwen.py:457-510](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py#L457-L510)）实现了多模态下的位置编码重算，所以敢声明 `True`。InternVL、Kimi-VL、LLaVA-OneVision、MiniCPM-V4.6 等同样声明 `True`；而 Step3-VL、Qwen3-TTS、Gpt-OSS 等显式 `False`。

**(4) data_collator 里 padding_free 的实际行为** —— [swift/template/base.py:1869-1886](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1869-L1886)

```python
if self.padding_free:
    batch[:] = [self.packing_row(batch)]           # 整个 batch 拼成一条
    assert 'position_ids' in batch[0], f'batch[0]: {batch[0]}'
...
if self.padding_free:
    assert len(batch) == 1, f'batch: {batch}'
    for k in ['input_ids', 'channel'] + gather_keys:
        v = batch[0].get(k)
        if v is not None:
            res[k] = v if k == 'channel' else [v]
```

padding_free 时整个 batch 被 `packing_row` 拼成单条序列，靠 `position_ids` 区分样本边界，因此**不再 padding**。这要求模板能正确产出拼接后的 `position_ids`——多模态模板必须自行覆盖 `packing_row` 才能做到（这就是 4.3.1 说的「适配」工作量）。

#### 4.3.4 代码实践

**实践目标**：验证 `support_padding_free` 的判定逻辑，并观察一个多模态模板开/关 packing 的差异。

**操作步骤（源码阅读型）**：

1. 在 [swift/template/templates/qwen.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py) 里用搜索定位 `class Qwen2VLTemplate`，确认其 `support_padding_free = True`（L312）。
2. 在同文件找 `class Qwen3TTSTemplate`（[swift/template/templates/qwen.py:1140-1142](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py#L1140-L1142)），确认它是 `support_padding_free = False`。
3. 阅读基类 `_data_collator` 在 `padding_free=True` 时的拼接逻辑（[swift/template/base.py:1869-1886](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L1869-L1886)），理解它为什么要求模板自己重算 `position_ids`。
4. （可选，需 GPU）分别尝试：

```bash
# Qwen2-VL 支持 packing（示例，待本地验证）
swift sft --model Qwen/Qwen2.5-VL-7B-Instruct --dataset <mm-dataset> --packing true ...

# Qwen3-TTS 不支持 packing，预期报错（示例，待本地验证）
swift sft --model <qwen3-tts> --dataset <tts-dataset> --packing true ...
# 预期：ValueError: Template `qwen3_tts` does not support padding free or packing.
```

**需要观察的现象**：

- Qwen2-VL 的 packing 能正常启动，且 `position_ids` 是 3D 的 M-RoPE（见 `_concat_text_position_ids`，[swift/template/base.py:2291-2295](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L2291-L2295)）。
- Qwen3-TTS 强行开 packing 会被 [sft.py:74-75](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L74-L75) 拦截报错。

**预期结果**：`support_padding_free` 为 `False` 的模板，开 `--packing/--padding_free` 时在训练启动前就报错，不会静默走错路径。

**待本地验证**：上述 `swift sft` 命令需真实环境与数据集；可仅完成步骤 1–3 的源码阅读部分。

#### 4.3.5 小练习与答案

**练习 1**：为什么基类把多模态的 `support_padding_free` 默认推断为 `False`（而不是 `True`）？

**参考答案**：多模态的位置编码（如 M-RoPE）依赖媒体形状（`image_grid_thw`）和 2D attention mask，跨样本拼接后必须重算这些信息才能正确生成 `position_ids`。这套重算逻辑没有通用实现，只有逐个模板手工适配（覆盖 `packing_row`）才安全。默认 `False` 是「安全优先」——宁可不能用，也不要静默算错。

**练习 2**：若你为新多模态模型写了模板并覆盖了 `packing_row` 正确产出 3D `position_ids`，应该同时做什么声明？

**参考答案**：在模板类里显式设置 `support_padding_free = True`，否则 [sft.py:72-73](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L72-L73) 会因 `is_multimodal=True` 把它推断为 `False`，用户开 packing 时仍会报错。

**练习 3**：`padding_free` 与普通训练在 `data_collator` 输出上最大的区别是什么？

**参考答案**：普通训练把 batch 内多条样本 pad 到等长，产出 `[B, L]` 的 `input_ids` 和 `attention_mask`；padding_free 则把整个 batch 拼成单条 `[1, sum_len]`，靠 `position_ids` 区分样本边界，**不产生 padding**，从而省掉无效计算。

---

## 5. 综合实践

把三个最小模块串起来，完成一次「**占位符全链路追踪**」：

**任务**：选取一个多模态模型（推荐 Qwen2.5-VL），手工追踪一条带图对话从原始 messages 到最终 `inputs_embeds` 的完整过程，标注每一步发生在哪个函数、`<image>` 变成了什么。

**建议步骤**：

1. **输入构造**：写一条 OpenAI 风格的 messages（含 `image_url`）。跟踪 [template_inputs.py:107-135](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/template_inputs.py#L107-L135) 的 `remove_messages_media`，确认 `image_url` 被拆成 `content` 里的 `<image>` 字符串 + `inputs.images` 列表里的一项。
2. **媒体加载**：跟踪 [base.py:375-393](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L375-L393)，说明图片如何经 `load_image` → 可选 `rescale_image` 变成 PIL。
3. **占位符替换**：跟踪 [base.py:986-1003](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L986-L1003) → [qwen.py:329-343](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py#L329-L343)，确认 `<image>` 被换成单个 `<|vision_start|><|image_pad|><|vision_end|>`，且该段 `loss_scale=0`。
4. **占位符扩展**：跟踪 [qwen.py:408-417](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py#L408-L417) → [base.py:443-470](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L443-L470)，用 \(N_{\text{img}}=\frac{T\cdot H\cdot W}{4}\) 算出该图展开后的 token 数。
5. **像素对齐**：跟踪 [base.py:2278-2288](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/base.py#L2278-L2288)，确认视觉 embedding 数量与第 4 步的 N 严格相等，`masked_scatter` 完成填充。
6. **效率约束**：确认该模板 `support_padding_free = True`（[qwen.py:312](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py#L312)），并解释它为何能开 packing（覆盖了 `packing_row` 重算 3D 位置编码）。

**交付物**：一张标注了「函数名 + `<image>` 当前形态 + loss_scale」的流程图或表格，能向他人讲清「像素如何变成 token」。

## 6. 本讲小结

- ms-swift 用一组 `special_tokens`（`<image>`/`<video>`/`<audio>` 等）在文本里「挖洞」，再用具体模型的 `placeholder_tokens`（如 `<|image_pad|>`）填充，最后用视觉 embedding 覆盖。
- 占位符四阶段：**解析 content → 替换为模型占位 token → 按分辨率扩展（`_extend_tokens`）→ 前向填充 embedding（`masked_scatter`）**，其中「扩展后的占位 token 数」必须严格等于「视觉 embedding 数」。
- 占位段 `loss_scale=0`（不参与 loss），且截断时受 `_truncate` 的 `protected` 保护，避免图被截掉而 embedding 残留。
- `vision_utils.py` 用 `load_file`/`load_image` 把本地/HTTP/base64 三种来源统一成 `PIL.Image`，用 `rescale_image` 配合 `max_pixels` 控制显存；视频/音频有各自的 `load_video_*`/`load_audio`。
- 多模态模板默认 **不支持** padding_free/packing（位置编码重算复杂），靠类属性 `support_padding_free` 显式声明；`None` 时按 `not is_multimodal` 推断，强行开启会在 `_prepare_template` 报错。
- `norm_bbox='norm1000'` 让检测框坐标与分辨率解耦，是 grounding 任务的默认约定。

## 7. 下一步学习建议

- **u4-l1 / u4-l3（数据集加载与编码）**：去看 `EncodePreprocessor` 是如何调用本讲的 Template.encode，把整个数据集批量编码、并配合 `PackingDataset` 拼接的——本讲的 `support_padding_free` 在那里会被真正消费。
- **u6-l1 / u6-l2（推理引擎）**：对比 transformers / vllm / lmdeploy / sglang 四种后端在多模态上的差异（本讲已多次提到 `self.mode` 分支），理解为何 vllm/lmdeploy 不提前 PIL 化。
- **u10-l3（自定义模型、模板与 Agent 注册）**：当你需要为新多模态模型写模板时，本讲的 `replace_tag` / `_encode` / `_extend_tokens` / `support_padding_free` 就是需要覆盖的关键钩子。
- 继续阅读源码：[swift/template/templates/qwen.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/qwen.py) 里的 `Qwen2_5VLTemplate` / `Qwen3VLTemplate`、[swift/template/templates/internvl.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/template/templates/internvl.py) 的 InternVL（动态分块预处理 `_dynamic_preprocess`），体会不同视觉后端在占位符扩展上的实现差异。

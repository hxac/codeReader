# 多图输入与 Thinking 模型：Kimi-VL-A3B-Thinking-2506 推理实践

## 1. 本讲目标

上一讲（u2-l1）我们用 Transformers 跑通了 Kimi-VL-A3B-Instruct 的**单图**问答。本讲往前走两步：

1. 学会用**列表**方式构造多张图片的 `messages` 输入，并正确地把图片列表传给 processor。
2. 理解 Thinking 模型的推荐生成参数：为什么 `max_new_tokens=32768`、为什么 `temperature=0.8`。
3. 学会**识别**长思维链输出的结构，并从中提取最终答案。

学完本讲，你应该能独立写出「多图 + 深度推理」的推理脚本，并知道输出被截断时该调哪个参数。

## 2. 前置知识

- **多图理解（multi-image understanding）**：模型同时看多张图片，综合它们的信息回答一个问题。README 在介绍模型能力时明确列出了这项能力（[README.md:L19](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L19)）。典型任务：比较两页手稿、对比前后截图、看多张图表找规律。
- **思维链（Chain-of-Thought，CoT）**：让模型「先写推理过程，再给答案」。Thinking 变体经过长思维链监督微调（SFT）与强化学习（RL）训练，会自发输出很长的推理文本（[README.md:L25](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L25)）。
- **温度（temperature）**：采样时控制随机性的参数。温度越高，输出越多样、越有「探索性」；越低越保守确定。官方推荐值出自 README 的 Note（见 4.2 节）。
- **`max_new_tokens`**：限制**新生成** token 的上限（不含输入）。generate 返回的序列 = 输入 token + 新生成 token，这也是上一讲「裁剪再解码」写法成立的原因。
- **双通道输入**（上一讲已建立）：`messages` 里的图片分片只是**占位符**（里面放的是路径字符串），真实像素通过 `processor(images=...)` 传入。本讲的多图写法完全建立在这个机制上。

## 3. 本讲源码地图

本仓库是发布型仓库（不含 Python 源码），本讲的「源码」就是 README 中的官方示例代码，素材是 figures 下的两张手稿图：

| 文件 | 作用 |
| --- | --- |
| [README.md:L156-L201](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L156-L201) | 本讲主线：Thinking-2506 的官方多图推理示例 |
| [README.md:L113-L154](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L113-L154) | 对照组：Instruct 单图示例（上一讲已精读） |
| [README.md:L65-L68](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L65-L68) | 官方推荐温度：Thinking 用 0.8，Instruct 用 0.2 |
| [README.md:L28-L33](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L28-L33) | Thinking-2506 相对旧版的改进（含思维长度平均下降 20%） |
| `figures/demo1.png`、`figures/demo2.png` | 推理输入素材：两页德文科学演算手稿（内容为张量/偏微分方程计算，疑似爱因斯坦相对论笔记——这正是示例 prompt 让模型推断的问题） |

> 提示：想直接看这两张图，可在仓库中打开 `figures/demo1.png` 与 `figures/demo2.png`。示例的问题 *"Please infer step by step who this manuscript belongs to and what it records"* 就是一个典型的**跨图推理**任务：模型需要分别辨认两页手稿的语言、符号风格和内容主题，再综合判断归属。

## 4. 核心概念与源码讲解

### 4.1 多图输入构造

#### 4.1.1 概念说明

多图输入要解决的问题是：**如何让模型知道「这里有 N 张图，按这个顺序看」**。

回顾上一讲的「双通道」机制：

- **占位通道**：`messages` 中放 `{"type": "image", "image": <路径>}` 分片，聊天模板会为每个图片分片渲染一个图像占位符；分片的**顺序和数量**决定占位符在文本中的位置。
- **像素通道**：`processor(images=...)` 传入真实图片，processor 按顺序把像素编码成 `pixel_values`，与文本中的占位符一一对应。

因此多图构造的关键就是：**占位通道给 N 个图片分片，像素通道给 N 个 PIL 对象，两边数量一致、顺序对应**。

#### 4.1.2 核心流程

官方多图示例的输入组装流程（对照单图写法）：

```text
单图（Instruct 示例）                    多图（Thinking-2506 示例）
─────────────────────                   ─────────────────────
image_path = "一个路径"                  image_paths = ["路径1", "路径2"]
image = Image.open(image_path)          images = [Image.open(p) for p in image_paths]
content = [ {image 分片}, {text 分片} ]   content = [ {image 分片}×2 ] + [ {text 分片} ]
processor(images=image, ...)            processor(images=images, ...)
```

要点：

1. 用**列表推导** `image_paths` 得到 PIL 对象列表 `images`。
2. `content` 用列表推导生成 N 个图片分片，再用 `+` 拼接文本分片——文本永远放在图片之后（这是官方示例的写法，也可按需调整分片顺序）。
3. processor 的 `images` 参数从「单个对象」变成「对象列表」。

#### 4.1.3 源码精读

多图路径与 PIL 对象列表的定义：

> 这两行先声明两个图片路径，再用列表推导打开成 PIL 图像列表。`images` 的顺序将对应文本中占位符出现的顺序。

```python
image_paths = ["./figures/demo1.png", "./figures/demo2.png"]
images = [Image.open(path) for path in image_paths]
```

出处：[README.md:L181-L182](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L181-L182)

多图 `messages` 的构造：

> `content` 由两部分用 `+` 拼接：前半是列表推导生成的**两个图片分片**（每个分片的 `image` 字段只放路径字符串，作占位用）；后半是文本分片，要求模型逐步推断手稿的归属与内容。

```python
messages = [
    {
        "role": "user",
        "content": [
            {"type": "image", "image": image_path} for image_path in image_paths
        ] + [{"type": "text", "text": "Please infer step by step who this manuscript belongs to and what it records"}],
    },
]
```

出处：[README.md:L183-L190](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L183-L190)

模板渲染与多图编码：

> `apply_chat_template` 把两个图片分片渲染成带占位符的输入文本；随后 `processor(images=images, ...)` 传入的是**列表**而非单个对象，产出的 `pixel_values` 将包含两份图像特征。`padding=True, truncation=True` 的含义与上一讲相同。

```python
text = processor.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")
inputs = processor(images=images, text=text, return_tensors="pt", padding=True, truncation=True).to(model.device)
```

出处：[README.md:L191-L192](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L191-L192)

对照单图写法（上一讲已精读，此处只列差异点）：

> 单图示例只打开一个 PIL 对象、content 只有一个图片分片、`processor(images=image, ...)` 传单个对象。三处差异完全同构：**单个 → 列表**。

出处：[README.md:L139-L145](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L139-L145)（单图），[README.md:L181-L192](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L181-L192)（多图）

> 关于「第 i 个图片分片对应 `images` 列表第 i 个元素」这一对应关系，是 transformers 多模态 processor 的通用契约；具体占位符 token 的形式由 HuggingFace 模型仓库中的聊天模板决定（本仓库不含该文件，具体 token 待确认）。可靠的验证方法是打印渲染文本观察占位符数量，见下面的实践。

#### 4.1.4 代码实践

**实践目标**：验证「图片分片数量 → 占位符数量 → 编码张量规模」的对应关系。这个实践**不需要加载 16B 模型**，只加载 processor，CPU 即可运行（只需下载几 MB 的配置文件）。

1. 操作步骤（示例代码，基于官方示例改写）：

   ```python
   # 示例代码：仅加载 processor，验证多图输入组装
   from PIL import Image
   from transformers import AutoProcessor

   processor = AutoProcessor.from_pretrained(
       "moonshotai/Kimi-VL-A3B-Thinking-2506", trust_remote_code=True
   )

   image_paths = ["./figures/demo1.png", "./figures/demo2.png"]
   images = [Image.open(p) for p in image_paths]
   messages = [
       {"role": "user", "content": [
           {"type": "image", "image": p} for p in image_paths
       ] + [{"type": "text", "text": "What do these two pages record?"}]},
   ]
   text = processor.apply_chat_template(messages, add_generation_prompt=True)
   print(text)  # 观察占位符：两图应出现两个图像占位符
   inputs = processor(images=images, text=text, return_tensors="pt", padding=True, truncation=True)
   print({k: tuple(v.shape) for k, v in inputs.items()})
   ```

2. 把 `image_paths` 换成三张图（可加入 `./figures/demo.png`），重复运行。
3. 需要观察的现象：
   - 渲染文本 `text` 中图像占位符的**个数**随图片数增加；
   - `inputs` 字典中 `pixel_values`（或类似键）的形状随图片数变化；
   - `input_ids` 的长度也随之增加（每张图贡献一段占位 token）。
4. 预期结果：两图时占位符 2 个，三图时 3 个；图像相关张量的第一维（或总 token 数）按图片数成比例增长。**具体张量名称与形状待本地验证**（取决于 HF 模型仓库中 processor 的实现）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `messages` 里写 2 个图片分片，但 `processor(images=...)` 只传 1 张图，会发生什么？

**参考答案**：两边数量不匹配，违反了「占位通道与像素通道一一对应」的契约。通常会抛出校验错误（占位符数量与图像数量不一致）；即便某些实现不报错，图像与占位符的对应关系也会错乱，模型看到的图片内容与提问位置对不上。因此两边的数量与顺序必须严格一致。

**练习 2**：想让模型「先看手稿、再看提示文字、最后再看一张补充截图」，`content` 该怎么写？

**参考答案**：把分片按期望顺序排列即可，例如：

```python
content = [
    {"type": "image", "image": "demo1.png"},
    {"type": "image", "image": "demo2.png"},
    {"type": "text", "text": "以上是两页手稿"},
    {"type": "image", "image": "demo.png"},
    {"type": "text", "text": "这是补充截图，请综合分析"},
]
```

同时 `images` 列表按相同顺序放 3 个 PIL 对象。`content` 是分片列表，顺序就是模型「读到」的顺序。

### 4.2 Thinking 模型生成参数

#### 4.2.1 概念说明

生成环节有两处与单图示例明显不同：

```python
generated_ids = model.generate(**inputs, max_new_tokens=32768, temperature=0.8)
```

- `max_new_tokens=32768`：Instruct 示例是 512，这里放大了 **64 倍**。
- `temperature=0.8`：Instruct 官方推荐 0.2，Thinking 官方推荐 0.8。

这两个数字都不是拍脑袋，而是「长思维链模型」的输出特性决定的。

#### 4.2.2 核心流程

**（1）为什么需要 32768 的生成预算**

Thinking 模型的一次完整输出由两部分组成：

\[ L_{\text{output}} = L_{\text{think}} + L_{\text{answer}} \le \text{max\_new\_tokens} \]

- \( L_{\text{think}} \)：思维链长度，复杂推理任务动辄数千 token；
- \( L_{\text{answer}} \)：最终答案，通常只占很小一段。

如果预算太小，生成会在**思维链中途**被截断——模型「想了一半」就被掐断，最终答案 \( L_{\text{answer}} \) 根本没机会输出，你拿到的将是一段没有结论的半截推理。这就是官方给 Thinking 模型配 32768 预算的原因：**宁可给足，也不能让答案被截掉**。

值得注意的是，2506 版在提升精度的同时把**平均思维长度缩短了约 20%**（[README.md:L29](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L29)），所以实际任务往往用不满 32768——它是「安全上限」，不是「期望长度」。

**（2）为什么 Thinking 推荐 0.8、Instruct 推荐 0.2**

官方推荐来自 README 的 Note：

> Thinking 模型推荐 `Temperature = 0.8`；Instruct 模型推荐 `Temperature = 0.2`。

出处：[README.md:L65-L68](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L65-L68)

直觉解释：

- **Instruct 是「执行型」模型**：看图描述、OCR、信息提取，答案基本由图片决定，不需要发散——低温（0.2）让输出稳定、确定性强。
- **Thinking 是「探索型」模型**：长思维链本质是在解空间里试错、回溯、验证。适当高温（0.8）让模型在推理时保留多样性，不容易在一条错误的思路上「锁死」；而且它的 RL 训练就是在这样的温度下优化过的。

**（3）一个必须说清的细节：温度何时真正生效**

在 transformers 的 `generate` 中，`temperature` 属于**采样参数**，只有在启用随机采样（`do_sample=True`）时才实际起作用；在贪心解码下传入 `temperature` 通常会被忽略（部分版本会打印警告）。官方示例没有显式写 `do_sample=True`，能否生效取决于模型仓库 `generation_config.json` 中的默认设置（该文件在 HuggingFace 侧，本仓库看不到，**待确认**）。上一讲 Instruct 示例也遇到过同样的问题。

最稳妥的写法是显式声明：

```python
# 示例代码：显式启用采样，确保温度生效
generated_ids = model.generate(
    **inputs, max_new_tokens=32768, do_sample=True, temperature=0.8
)
```

#### 4.2.3 源码精读

Thinking-2506 的生成调用：

> `generate` 一次性给出两处关键参数：生成预算放大到 32768（容纳长思维链），采样温度 0.8（官方对 Thinking 模型的推荐值）。返回值仍由后面的裁剪逻辑处理。

```python
generated_ids = model.generate(**inputs, max_new_tokens=32768, temperature=0.8)
```

出处：[README.md:L193](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L193)

对照 Instruct 示例的生成调用（`max_new_tokens=512`、未传温度）：

> 单图 Instruct 示例只限制 512 个新 token，且未设置采样参数——短答案 + 低推荐温度（0.2）的组合。

出处：[README.md:L146](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L146)

模型变体与推荐温度的官方出处：

> 变体表中三个模型规格完全相同（16B 总参 / 3B 激活 / 128K 上下文），差异全部来自后训练；Note 部分给出两条温度推荐——Thinking 用 0.8，Instruct 用 0.2。

出处：[README.md:L57-L68](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L57-L68)

#### 4.2.4 代码实践

**实践目标**：体会「生成预算 vs 思维链长度」的关系，理解截断风险。需要能容纳 16B 模型的 GPU 环境（bfloat16 约 32GB 显存，参考上一讲）；若无 GPU，可改用 README 提供的 [HuggingFace 在线 Demo](https://huggingface.co/spaces/moonshotai/Kimi-VL-A3B-Thinking/) 做定性对照（链接见 [README.md:L71-L74](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L71-L74)）。

1. 操作步骤：运行 4.3 节的完整示例脚本，只修改 `max_new_tokens`，分别取 `2048` 和 `32768`，各运行一次。
2. 需要观察的现象：
   - 记录两次输出的**字符/token 长度**（可用 `len(response)` 与 `len(generated_ids_trimmed[0])`）；
   - 记录两次输出中**是否出现最终结论**（对「手稿属于谁」的明确回答）；
   - 记录两次运行耗时。
3. 预期结果（待本地验证）：
   - `2048` 时很可能思维链被截断，输出结尾停在推理中途，没有结论；
   - `32768` 时思维链完整、末尾给出明确答案，但实际生成量通常远小于 32768；
   - 耗时与实际生成 token 数近似成正比，而不是与 `max_new_tokens` 上限成正比（预算只是上限，不是配额）。

#### 4.2.5 小练习与答案

**练习 1**：同事抱怨「Kimi-VL-Thinking 跑得好慢，能不能把 `max_new_tokens` 改成 512 提速」？怎么回答？

**参考答案**：不能简单这么改。`max_new_tokens` 是上限，实际生成到结束符就停止，因此把它设大本身不拖慢短任务；反过来设小会把长思维链拦腰截断，导致拿不到最终答案，白白浪费已消耗的算力。正确做法是：按任务复杂度给足预算（推理题给数千到数万），若确知任务是简单感知类，考虑直接换 Instruct 变体，而不是压缩 Thinking 的预算。

**练习 2**：为什么 Instruct 推荐 0.2、Thinking 推荐 0.8，而不是两者都用同一个「最优温度」？

**参考答案**：因为两类模型的输出性质不同。Instruct 做感知/执行类任务，答案由输入图片基本决定，低温保证稳定与可复现；Thinking 做长链推理，需要在思路间探索、回溯，适当高温保持思路多样性，且模型的后训练（RL）就是在该温度区间下优化的。不存在跨任务通用的「最优温度」，推荐值与模型训练口径是对齐的。

### 4.3 思维链输出口径

#### 4.3.1 概念说明

拿到 Thinking 模型的输出后，你面对的是一整段「推理过程 + 最终答案」的混合文本。所谓「口径」，就是回答三个问题：

1. 输出的**结构**长什么样？（哪里是思考，哪里是答案）
2. `skip_special_tokens=True` 解码后**丢掉了什么**？
3. 怎么**稳定地提取**最终答案？

一条实用的经验：思维链文本通常较长，直接 `print(response)` 不便阅读，建议分段打印或写进文件再查看。

#### 4.3.2 核心流程

处理 Thinking 输出的推荐流程：

```text
generate 返回完整序列（输入 + 新生成）
   │
   ├─ 按输入长度裁剪 → 只留新生成部分
   │
   ├─ batch_decode(skip_special_tokens=True) → 干净文本
   │
   ├─ 【定位分界】：观察输出，找到「思考结束 / 答案开始」的标记
   │      （若模型用特殊 token 包裹思维链，需用 skip_special_tokens=False 观察）
   │
   └─ 提取最终答案 → 结构化使用（如存入 answer 字段）
```

关于分界标记：不同模型有不同约定（常见做法是用特殊 token 或固定标签包裹思维链）。Kimi-VL-Thinking-2506 的具体标记由 HuggingFace 模型仓库中的聊天模板/词表决定，**本仓库无法确认**——下面实践的第一步就是把它实测出来。

#### 4.3.3 源码精读

生成后的裁剪与解码（与单图示例完全一致）：

> 先按每个样本的输入长度裁掉 prompt 部分（`out_ids[len(in_ids):]`），再 `batch_decode` 且 `skip_special_tokens=True`——把控制类特殊 token 滤掉，得到可读文本。这套「裁剪→解码」写法是 Kimi-VL 所有 Transformers 示例的统一口径。

```python
generated_ids_trimmed = [
    out_ids[len(in_ids) :] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
]
response = processor.batch_decode(
    generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
)[0]
print(response)
```

出处：[README.md:L194-L200](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L194-L200)

输出长度的官方背景：

> 2506 版在 MathVision(+20.1)、MathVista(+8.4) 等推理基准上提升明显，同时平均思维长度**减少 20%**——「想得更短但更准」是这一代 Thinking 模型的特点，也是解读其输出长度时的官方参照。

出处：[README.md:L28-L33](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L28-L33)

#### 4.3.4 代码实践

**实践目标**：实测思维链输出的结构，并写一个「答案提取」函数。以下为示例代码（基于官方示例 [README.md:L156-L201](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L156-L201) 扩展）。

1. 操作步骤：

   ```python
   # 示例代码：在官方示例的 decode 之后追加三步
   # ① 用 skip_special_tokens=False 看原始 token，确认思维链的包裹标记
   raw = processor.batch_decode(
       generated_ids_trimmed, skip_special_tokens=False,
       clean_up_tokenization_spaces=False,
   )[0]
   print(raw[:2000])   # 看开头：思维链从什么标记开始
   print(raw[-2000:])  # 看结尾：答案前有什么分界标记

   # ② 按实测标记改写 split_token，提取最终答案
   split_token = "</think>"  # 占位：以 ① 中实测结果为准（待本地验证）
   def extract_answer(full_text: str) -> str:
       if split_token in full_text:
           return full_text.split(split_token, 1)[1].strip()
       return full_text[-500:].strip()  # 无标记时的兜底：取结尾一段

   # ③ 结构化结果
   print({"thinking_len": len(response), "answer": extract_answer(response)})
   ```

2. 需要观察的现象：
   - `raw` 的开头与结尾处，是否出现包裹思维链的特殊 token 或标签；
   - `response`（过滤特殊 token 后）中该标记是否仍然保留——若标记本身是特殊 token，过滤后会消失，此时应改为在 `raw` 上切分；
   - 结尾处的最终答案是否明确回答了「手稿属于谁、记录了什么」。
3. 预期结果（待本地验证）：输出呈现「长推理 → 分界 → 简短结论」的结构；提取函数能稳定拿到结论部分。若发现输出根本没有分界标记（答案直接跟在推理后），就改用「取末尾 N 字符」或「用第二次提问让模型自述结论」的兜底策略。

#### 4.3.5 小练习与答案

**练习 1**：为什么提取答案前要先跑一遍 `skip_special_tokens=False` 的解码？

**参考答案**：因为 `skip_special_tokens=True` 会把特殊 token 全部滤掉。如果模型用特殊 token 包裹思维链（例如某种 `<think>` 类标记），过滤后分界就消失了，你无法判断哪段是思考、哪段是答案。先看「未过滤」的原始输出，才能确定分界的真实形式，再决定在哪个版本上切分。

**练习 2**：多图手稿任务的输出里，思维链通常包含哪些可辨认的「步骤感」内容？

**参考答案**：典型步骤包括：逐页辨认文字语言与书写风格 → 识别数学符号体系（张量记号、偏微分方程等）→ 结合历史背景列出候选作者 → 逐个排除 → 综合两页证据给出结论。观察思维链时可以刻意找这些「环节」，这也是判断模型是否真正做了**跨图综合**（而非只看其中一页）的方法。

## 5. 综合实践

把本讲三个模块串起来，完成官方示例的「预算对照实验」（需要 GPU 环境；无 GPU 时用 [在线 Demo](https://huggingface.co/spaces/moonshotai/Kimi-VL-A3B-Thinking/) 定性完成）：

1. **跑通基线**：完整运行 [README.md:L156-L201](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L156-L201) 的 Thinking-2506 示例，对 `figures/demo1.png` 与 `figures/demo2.png` 做「手稿归属与内容」推断，确认能拿到完整输出。
2. **对照预算**：把 `max_new_tokens` 分别设为 `2048` 与 `32768` 各跑一次，记录成表格：

   | max_new_tokens | 输出 token 数 | 是否给出明确结论 | 耗时 |
   | --- | --- | --- | --- |
   | 2048 | ？ | ？ | ？ |
   | 32768 | ？ | ？ | ？ |

3. **提取答案**：用 4.3.4 实践中的方法实测分界标记，把两次输出切分为 `thinking` 与 `answer` 两部分。
4. **回答思考题**（写进你的笔记）：为什么长思维链模型需要更大的生成预算？结合 \( L_{\text{output}} = L_{\text{think}} + L_{\text{answer}} \le \text{max\_new\_tokens} \) 与你的实验数据说明：预算压缩伤害的是答案还是推理？实际生成量与预算上限是什么关系？

预期结论（待本地验证）：2048 很可能截断在推理中途而无结论；32768 给出完整「思维链 + 结论」，实际生成量远小于上限——预算是保险丝，不是燃料。

## 6. 本讲小结

- 多图输入 = 占位通道给 N 个图片分片（列表推导）+ 像素通道给 N 个 PIL 对象（`images=images`），两边数量与顺序严格一致；与单图写法的差异是「单个 → 列表」。
- Thinking-2506 官方推荐 `max_new_tokens=32768`：思维链 \( L_{\text{think}} \) 可达数千 token，预算不足会截断在推理中途，最终答案拿不到。
- 官方温度口径：Thinking 用 0.8（推理需要探索性），Instruct 用 0.2（感知任务求稳定）；`temperature` 需配合采样（`do_sample=True`）才确定生效。
- 解读输出的统一口径：裁剪输入 → `skip_special_tokens=True` 解码 → 定位「思考/答案」分界 → 提取结论；分界标记要用 `skip_special_tokens=False` 实测。
- 2506 版特点：推理基准大幅提升的同时平均思维长度减少 20%——「想得更短但更准」。

## 7. 下一步学习建议

- 下一讲（u2-l3）深入输入组装内部：`apply_chat_template` 渲染出的文本到底长什么样、`processor` 如何同时产出 `input_ids` 与 `pixel_values`，把本讲「占位通道」的细节彻底打通。
- 若你更关心部署，可以提前浏览 README 的 vLLM 离线推理示例（[README.md:L215-L246](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L215-L246)），注意那里多图/图像改用 `multi_modal_data` 传递，与本讲写法形成对照。
- 动手型读者建议：把本讲的提取函数封装成一个小工具，下一讲综合实践会直接复用。

# 第一次推理：用 Transformers 跑通 Kimi-VL-A3B-Instruct 单图问答

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立运行 README 中的 Kimi-VL-A3B-Instruct 单图推理示例，得到模型回答。
2. 逐行说出示例代码的五个阶段：**加载模型与 Processor → 构造 messages → 渲染模板并编码 → 生成 → 裁剪解码**。
3. 解释 `torch_dtype`、`device_map`、`trust_remote_code` 三个加载参数各自的含义。
4. 掌握「先裁掉输入 token、再解码」的标准写法（`generated_ids_trimmed`），并明白为什么必须这么做。
5. 看懂 README 中被注释掉的 flash-attn 推荐配置，知道它为什么能省显存、提速。

本讲是整个学习手册里第一次真正「让模型跑起来」的讲义。代码全部来自 README 的官方示例，我们不发明任何新写法，只把它拆开讲透。

## 2. 前置知识

在动手之前，请确认你已完成 [u1-l2 环境搭建](u1-l2-environment-setup.md)：conda 环境 `kimi-vl`（python=3.10、torch=2.5.1、transformers=4.51.3）已就绪。同时回顾 [u1-l1](u1-l1-project-overview.md) 中的两个关键事实：

- Kimi-VL 总参数 16B，语言解码器每 token 只激活约 2.8B；
- 本仓库不含模型实现代码，实现与权重都在 HuggingFace 模型仓库 `moonshotai/Kimi-VL-A3B-Instruct` 中，通过 `trust_remote_code=True` 在加载时动态下载执行。

本讲还需要几个基础概念，用大白话解释：

| 概念 | 通俗解释 |
| --- | --- |
| 自回归生成 | 模型一次只预测「下一个 token」，把它拼回输入，再预测下一个，循环往复直到遇到结束符或达到长度上限。`model.generate` 就是这个循环的封装。 |
| token / input_ids | 模型不直接读文字，而是读「词元编号」。把文字切成词元、再查表变成整数编号，得到的就是 `input_ids` 张量。 |
| 张量（tensor） | PyTorch 里的多维数组。`input_ids` 形如 `[1, 序列长度]`，`pixel_values` 是图片被视觉编码器预处理后的像素张量。 |
| 聊天模板（chat template） | 模型在训练时见过一种固定格式的对话文本（带角色标记、图像占位符）。推理时必须把你的问题拼成同样的格式，模型才「认得」。 |
| 贪心解码 vs 采样 | 贪心解码每步都选概率最大的 token（`generate` 的默认行为）；采样则按概率分布随机抽取，`temperature` 参数只在采样模式下生效。 |

另外一条显存账（承接 u1-l2）：16B 权重以 bfloat16 加载约占 \( 16 \times 10^9 \times 2 \text{字节} \approx 32\text{GB} \) 显存，运行时还需额外的激活与 KV 缓存开销。请准备一张 40GB 以上的 GPU（如 A100 40G/80G），或依赖 `device_map="auto"` 把放不下的部分卸载到 CPU 内存（会明显变慢）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [README.md:L109-L154](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L109-L154) | 本讲唯一精读对象：Transformers 推理章节的环境说明与 Instruct 完整示例代码 |
| [README.md:L65-L68](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L65-L68) | 官方推荐采样参数：Instruct 用 Temperature=0.2，Thinking 用 0.8 |
| [README.md:L106-L107](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L106-L107) | flash-attn 的安装提示与用途说明 |
| [figures/demo.png](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/figures/demo.png) | 推理输入图：官方测试素材，Transformers、vLLM、OpenAI API 三段示例共用（见 u1-l3） |
| requirements.txt | 环境依据，u1-l2 已逐行解读，本讲不再展开 |

提醒：模型内部实现（MoonViT、MoE 解码器的 `.py` 文件）不在本仓库，本讲的「源码精读」读的是官方示例代码本身；想要看模型实现，需要去 HuggingFace 模型仓库的 Files 页面，这恰好是本讲实践的作业之一。

## 4. 核心概念与源码讲解

先给一张全景图。README 的 Instruct 示例可以拆成三个最小模块，数据流如下：

```
┌─ 模块一：加载 ──────────────────────────────┐
│ model_path                                      │
│   ├─ AutoModelForCausalLM.from_pretrained ──► model（16B 权重 + 远程模型代码）
│   └─ AutoProcessor.from_pretrained ────────► processor（分词器 + 图像预处理器）
└────────────────────────────────────────────────┘
┌─ 模块二：消息构造与模板渲染 ────────────────┐
│ messages（结构化对话）                          │
│   ── apply_chat_template ──► text（模板渲染后的文本，含图像占位符）
│   ── processor(images=图, text=text) ──► inputs（input_ids / attention_mask / pixel_values）
└────────────────────────────────────────────────┘
┌─ 模块三：生成与输出解码 ────────────────────┐
│ model.generate(**inputs) ──► generated_ids（= 输入 token + 新生成 token）    │
│   ── 裁掉输入部分 ──► generated_ids_trimmed                                   │
│   ── batch_decode ──► response（纯文本回答）                                  │
└────────────────────────────────────────────────┘
```

下面逐模块精读。

### 4.1 模型与 Processor 加载

#### 4.1.1 概念说明

多模态推理需要两样东西，它们来自**两条平行的加载通道**：

- **model**：16B 的模型权重与网络结构。它负责「思考」，即根据 `input_ids` 和 `pixel_values` 预测下一个 token。
- **processor**：输入组装器。它内部打包了分词器（tokenizer，文字 → token 编号）和图像预处理器（image processor，图片 → 像素张量），负责在推理前把原始的「一张图 + 一段话」翻译成模型吃的张量。

为什么一个「视觉语言模型」用的是 `AutoModelForCausalLM`（因果语言模型的通用入口）？因为 Kimi-VL 在 HuggingFace 模型仓库的 `config.json` 里通过 `auto_map` 机制把自己的模型类注册在了这个入口下。`trust_remote_code=True` 的作用（见 u1-l1）就是允许 transformers 下载并执行模型仓库里那些 `.py` 实现文件，注册的模型类才能被找到。你可以到模型页 `config.json` 的 `auto_map` 字段里亲自确认这一点——这是本模块的实践任务。

#### 4.1.2 核心流程

`from_pretrained` 一次调用背后发生的事，按顺序是：

1. 根据 `model_path`（模型名或本地路径）定位 HuggingFace 仓库。
2. 读取 `config.json`；因为 `trust_remote_code=True`，允许下载并加载仓库中的自定义 `.py` 模型代码。
3. 按 `torch_dtype` 决定权重精度：`"auto"` 表示沿用模型配置中声明的精度；示例注释里则显式指定 `torch.bfloat16`。
4. 由 `device_map="auto"`（依赖 accelerate 库）根据当前机器的 GPU/CPU 显存情况，自动把模型各层分配到可用设备上。
5. 若指定 `attn_implementation="flash_attention_2"`，则注意力算子切换为 flash-attn 实现（要求已安装 flash-attn，且通常与半精度配套使用）。

一句话区分三个参数：`torch_dtype` 管「权重用什么精度存」，`device_map` 管「权重放在哪块设备」，`trust_remote_code` 管「允不允许运行远端自定义代码」。三者互不替代。

#### 4.1.3 源码精读

模型加载的主体（示例代码，来自 README）：

> [README.md:L120-L126](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L120-L126)：指定模型为 Instruct 变体，用 `AutoModelForCausalLM` 加载权重，`torch_dtype="auto"` 按模型配置精度加载，`device_map="auto"` 自动分配设备，`trust_remote_code=True` 允许执行 HuggingFace 仓库中的远程模型代码。

```python
model_path = "moonshotai/Kimi-VL-A3B-Instruct"
model = AutoModelForCausalLM.from_pretrained(
    model_path,
    torch_dtype="auto",
    device_map="auto",
    trust_remote_code=True,
)
```

被注释掉的 flash-attn 推荐配置：

> [README.md:L127-L135](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L127-L135)：官方给出的「进阶版」加载写法——把 `torch_dtype` 从 `"auto"` 换成显式的 `torch.bfloat16`，并新增 `attn_implementation="flash_attention_2"`，注释说明这两项配合使用可以省显存、加速推理。

```python
# model = AutoModelForCausalLM.from_pretrained(
#     model_path,
#     torch_dtype=torch.bfloat16,
#     device_map="auto",
#     trust_remote_code=True,
#     attn_implementation="flash_attention_2"
# )
```

为什么这两个参数成对出现？flash-attn 的 CUDA 核心面向半精度（bf16/fp16）设计，不与 fp32 权重搭配使用；同时如 u1-l2 分析的，它通过分块计算避免物化完整注意力矩阵，把注意力部分的显存从 \( O(n^2) \) 降到 \( O(n) \)。官方在安装说明里也专门提示了这一条：

> [README.md:L106-L107](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L106-L107)：如果遇到显存不足（Out-of-Memory）或想加速推理，用 `pip install flash-attn --no-build-isolation` 安装 flash-attn。

Processor 加载这一行容易被忽略，但没有它后面所有输入组装都无法进行：

> [README.md:L137](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L137)：用 `AutoProcessor` 从同一个模型仓库加载输入处理器（含分词器与图像预处理器），同样需要 `trust_remote_code=True`，因为多模态预处理逻辑也是模型仓库自定义的。

```python
processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)
```

注意两个通道用的是**同一个 `model_path`**：权重与预处理器必须来自同一模型仓库，保证「翻译规则」和「大脑」配套。

#### 4.1.4 代码实践

**实践目标**：不下载 16B 权重，仅通过「读配置」把加载参数与模型仓库的对应关系搞清楚。

**操作步骤**：

1. 打开浏览器访问 `https://huggingface.co/moonshotai/Kimi-VL-A3B-Instruct`，进入 **Files and versions** 页。
2. 点开 `config.json`，查找并记录三个字段：
   - `torch_dtype`（或新版字段名 `dtype`）：`torch_dtype="auto"` 时权重将按这个精度加载；
   - `auto_map`：确认 Kimi-VL 把模型类注册在 `AutoModelForCausalLM` 入口下；
   - `architectures`：模型结构类的名字。
3. 再看 Files 页里有哪些 `.py` 文件——它们就是 `trust_remote_code=True` 实际执行的代码（与 u1-l1 的清单核对）。
4. 写一份两栏对照表（本机可完成）：`torch_dtype="auto"` + 默认注意力 vs. `torch_dtype=torch.bfloat16` + `attn_implementation="flash_attention_2"`，各自影响加载的哪一步。

**需要观察的现象**：`auto_map` 中 `AutoModelForCausalLM` 键指向的远程类名；`torch_dtype` 字段的实际取值。

**预期结果**：能看到 `auto_map` 存在且指向模型仓库内的 Python 类路径；`torch_dtype` 字段为某个半精度取值（具体值以页面实际内容为准，待本地验证）。

**进阶（需 GPU，待本地验证）**：真正加载模型后执行 `print(model.hf_device_map)`，观察 `device_map="auto"` 把各层分配到了哪些设备（单卡时会全部落在 `cuda:0` 或显示 `DISK`/`cpu` 卸载）。

#### 4.1.5 小练习与答案

**练习 1**：把 `trust_remote_code=True` 去掉会发生什么？为什么？

<details>（先想再看）</details>

参考答案：加载会失败。因为 Kimi-VL 的模型类不在 transformers 库内建类型中，而在 HuggingFace 模型仓库的自定义代码里；关掉这个开关，transformers 出于安全考虑拒绝下载执行远程代码，也就找不到对应的模型实现。这也是 u1-l1 强调的「发布型仓库 + trust_remote_code」形态的直接体现。

**练习 2**：`torch_dtype="auto"` 和 `torch_dtype=torch.bfloat16` 有什么区别？为什么 flash-attn 版配置要显式指定后者？

参考答案：`"auto"` 表示「模型配置里声明什么精度就用什么精度」，跟随模型仓库走；显式传 `torch.bfloat16` 则强制以 bfloat16 加载。flash-attn 的注意力实现面向 bf16/fp16 设计，官方为确保精度与算子匹配、避免歧义，在推荐配置中显式写死了 bfloat16。

**练习 3**：如果只有一张 24GB 显存的卡，直接跑这个示例可能遇到什么问题？`device_map="auto"` 能帮上什么忙？

参考答案：bfloat16 权重约 32GB，24GB 放不下，会报 Out-of-Memory。`device_map="auto"` 会自动把装不下的层卸载到 CPU 内存（甚至磁盘），让程序仍能运行，但层间数据搬运会显著拖慢推理速度；更实际的办法是换更大显存的卡、多卡分摊，或等到 u3 单元学习 vLLM 部署。

### 4.2 消息构造与模板渲染

#### 4.2.1 概念说明

模型在训练时看到的是固定格式的对话文本。推理时我们发给模型的不能是裸字符串，而是一个**结构化的 messages 列表**——这与 OpenAI Chat API 的 messages 结构同构：每条消息有 `role`（谁说的）和 `content`（说了什么）。

Kimi-VL 是多模态模型，所以 `content` 不是纯字符串，而是**分片（parts）列表**，每个分片带 `type`：

- `{"type": "image", "image": <图片路径或对象>}`：这里有一张图；
- `{"type": "text", "text": "..."}`：这里有一段话。

这里有个初学者最容易困惑的细节，值得单独画重点：**示例中「消息里的图」和「喂给 processor 的图」是两条平行的线**。

- 消息里的 `{"type": "image", "image": image_path}` 只带路径字符串，作用是告诉模板「这个位置要放一张图」；
- 真正的像素由 `processor(images=image, ...)` 里的 PIL 图片对象单独传入。

模板渲染只需要知道「图的占位顺序」，不需要真实像素；像素在 processor 编码阶段才被消费。理解了这种分工，你就不会误以为示例里 `Image.open` 那一行是多余的。

#### 4.2.2 核心流程

从 messages 到模型输入，共三步：

```
1. Image.open(image_path)
       └─► image：PIL 图片对象（真实像素，供 processor 用）

2. processor.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")
       └─► text：按 Kimi-VL 的聊天模板渲染出的文本，
            包含角色标记、特殊控制 token、图像占位符，
            末尾因 add_generation_prompt=True 追加了「助手开始作答」的引导标记

3. processor(images=image, text=text, return_tensors="pt", padding=True, truncation=True)
       └─► inputs：类字典对象，至少包含
            input_ids      文本（含占位符）切分编号后的整数张量
            attention_mask 标记哪些位置是真实 token（Padding 位置为 0）
            pixel_values   图片经图像预处理器处理后的像素张量
            随后 .to(model.device) 把这些张量搬到模型所在设备
```

伪代码概括三步的职责边界：

```
读图        → 提供像素
模板渲染    → 提供格式（谁说的、图在哪、轮到助手了）
processor   → 像素 + 格式 一起编码成张量
```

#### 4.2.3 源码精读

消息构造（示例代码，来自 README）：

> [README.md:L139-L143](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L139-L143)：打开示例图得到 PIL 对象；构造一条 `user` 角色的消息，`content` 列表里先放图片分片（只带路径），再放文字分片——官方预设的问题「图中的穹顶建筑是什么？请一步步思考」。

```python
image_path = "./figures/demo.png"
image = Image.open(image_path)
messages = [
    {"role": "user", "content": [{"type": "image", "image": image_path}, {"type": "text", "text": "What is the dome building in the picture? Think step by step."}]}
]
```

模板渲染与编码，一行一个职责：

> [README.md:L144](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L144)：把结构化 messages 按 Kimi-VL 聊天模板渲染为模型输入文本；`add_generation_prompt=True` 在末尾补上「该助手回复了」的引导段，模型由此知道要从「作答」开始生成。

```python
text = processor.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")
```

> [README.md:L145](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L145)：processor 同时接收图像（PIL 对象，真实像素）与渲染文本，输出 `return_tensors="pt"` 的 PyTorch 张量；`padding=True` 在批量输入时补齐到最长序列，`truncation=True` 在超长时截断；最后 `.to(model.device)` 与模型同设备。

```python
inputs = processor(images=image, text=text, return_tensors="pt", padding=True, truncation=True).to(model.device)
```

两个布尔参数单例时看似「没用」，实为批量场景的保险：单图单问时没有 Padding 需求、也不会超长，但保留它们是好习惯——这段代码无需改动就能直接用于批量。

#### 4.2.4 代码实践

**实践目标**：不加载 16B 模型，只加载 processor（只需下载分词器与预处理配置，体量很小），亲眼看到「模板渲染」和「张量编码」两个阶段的中间产物。

**操作步骤**（示例代码，可直接保存为 `inspect_processor.py` 在仓库根目录运行，注意 `image_path` 要指向仓库内的 `figures/demo.png`）：

```python
# 示例代码：仅加载 processor，观察多模态输入组装过程
from PIL import Image
from transformers import AutoProcessor

model_path = "moonshotai/Kimi-VL-A3B-Instruct"
processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)

image_path = "./figures/demo.png"
image = Image.open(image_path)
messages = [
    {"role": "user", "content": [{"type": "image", "image": image_path}, {"type": "text", "text": "What is the dome building in the picture? Think step by step."}]}
]

text = processor.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")
print("=== 模板渲染结果 ===")
print(type(text))   # 先确认返回类型（字符串还是张量，随 transformers 版本而定）
print(text)

inputs = processor(images=image, text=text, return_tensors="pt", padding=True, truncation=True)
print("=== processor 输出的键与形状 ===")
for k, v in inputs.items():
    print(k, getattr(v, "shape", type(v)))
```

**需要观察的现象**：

1. `type(text)` 的实际类型（不同 transformers 版本行为可能不同，以打印结果为准）；
2. 渲染文本中的特殊控制标记与图像占位符长什么样、出现在哪个位置；
3. `inputs` 里有哪些键，`input_ids` 和 `pixel_values` 的形状。

**预期结果**：渲染文本包含角色/边界类的特殊 token 以及图像占位符，末尾出现引导助手作答的标记；`inputs` 至少含 `input_ids`、`attention_mask`、`pixel_values` 三个张量。占位符的具体样式、`pixel_values` 的具体形状（与 MoonViT 原生分辨率 patch 化方式有关）**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把消息里的文字分片放到图片分片**之前**，即先 `text` 后 `image`，会发生什么？

参考答案：模板渲染出的文本中图像占位符与文字的相对位置随之改变，模型「看到」的输入顺序变为「先读问题再看图」。对这张示例图影响不大，但涉及「先给条件再看图」或图表配文顺序的任务时，顺序错误可能导致理解偏差。messages 里分片的书写顺序就是模型感知的顺序。

**练习 2**：`add_generation_prompt=True` 去掉会怎样？

参考答案：模板末尾不会补「助手开始作答」的引导段，模型可能不知道此刻该以助手身份接话，表现为不回答、续写用户的话或输出格式错乱。多轮对话补全（模仿历史对话继续写）时才需要去掉它，生成场景必须保留。

**练习 3**：为什么消息里图片分片只放路径字符串，还要额外 `Image.open` 再把对象传给 `images=`？

参考答案：两条线职责不同——模板渲染只需要「此处有图」的位置信息；真实像素只在 processor 编码时才需要。路径进消息、PIL 对象进 `images=`，是这套官方示例采用的分工写法。也正因如此，换图时两处要同步改（本讲综合实践会踩到这个点）。

### 4.3 生成与输出解码

#### 4.3.1 概念说明

`model.generate(**inputs, max_new_tokens=512)` 启动自回归循环：模型基于 `input_ids` + `pixel_values` 预测下一个 token，拼回序列再预测下一个，直到生成结束符（EOS）或新 token 数达到 `max_new_tokens` 上限（Instruct 示例设为 512；对比 Thinking 变体的 32768，留到 u2-l2 讨论）。

关键认知：**`generate` 返回的不是回答，而是「你的输入 + 模型的回答」拼接在一起的完整 token 序列**。如果直接把返回值整个解码，你会看到自己的问题被原样复述一遍，然后才是回答。所以官方示例有一段「裁剪」代码：对每个批次样本，用输入序列长度 `len(in_ids)` 把开头的输入部分切掉，只留新生成的 `out_ids[len(in_ids):]`。

裁剪之后用 `batch_decode` 把 token 编号还原成文本。两个参数的含义：

- `skip_special_tokens=True`：解码时丢弃特殊 token（角色标记、结束符等），否则回答里会混进 `<...>` 形式的控制符号；
- `clean_up_tokenization_spaces=False`：保留分词产生的空格原样，不做美化清理——对中文与代码类输出更保真。

最后 `[0]` 取批次里的第一条（本例只有一条）。

#### 4.3.2 核心流程

```
generated_ids = model.generate(**inputs, max_new_tokens=512)
        │
        ▼  generated_ids 形如 [1, 输入长度 + 新增长度]
        │
逐批次样本配对：zip(inputs.input_ids, generated_ids)
        │
        ▼  对每对 (in_ids, out_ids)：
裁剪：out_ids[len(in_ids):]        ← 切掉开头与输入等长的部分
        │
        ▼
解码：batch_decode(..., skip_special_tokens=True, clean_up_tokenization_spaces=False)
        │
        ▼
取 [0] → response 字符串 → print(response)
```

一个值得注意的细节：官方 Instruct 示例的 `generate` 调用**没有传 temperature**，即走 transformers 默认的贪心解码（`do_sample=False`）。README 单独在参数建议区给出了推荐值：

> [README.md:L65-L68](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L65-L68)：官方推荐——Thinking 类模型用 `Temperature = 0.8`，Instruct 类模型用 `Temperature = 0.2`。

若想让温度真正生效，需要同时开启采样，例如（示例代码）：

```python
generated_ids = model.generate(**inputs, max_new_tokens=512, do_sample=True, temperature=0.2)
```

只传 `temperature` 而不开 `do_sample` 时，温度不会起作用——这是 transformers 的通用行为，不是 Kimi-VL 特有的。

#### 4.3.3 源码精读

生成与三行「裁剪-解码」标准写法（示例代码，来自 README）：

> [README.md:L146-L149](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L146-L149)：`generate` 以自回归方式最多生成 512 个新 token，返回值是「输入 + 新生成」的完整序列；随后用 `zip` 把每个批次样本的输入序列与输出序列配对，`out_ids[len(in_ids):]` 切掉与输入等长的前缀，只保留模型新生成的部分。

```python
generated_ids = model.generate(**inputs, max_new_tokens=512)
generated_ids_trimmed = [
    out_ids[len(in_ids) :] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
]
```

> [README.md:L150-L153](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L150-L153)：把裁剪后的 token 序列解码为文本——`skip_special_tokens=True` 去掉控制类特殊 token，`clean_up_tokenization_spaces=False` 保留原始分词空格；`batch_decode` 返回列表，`[0]` 取批次中第一条并打印。

```python
response = processor.batch_decode(
    generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
)[0]
print(response)
```

`generated_ids_trimmed` 用列表推导而不是直接对整个张量切片，正是为了**逐批次样本**处理：每个样本的输入长度可能不同（尤其打开 `padding=True` 之后），按各自 `len(in_ids)` 裁剪才不会切错位置。

#### 4.3.4 代码实践

**实践目标**：体会「不裁剪会怎样」，把裁剪写法从「背下来」变成「理解了」。

**操作步骤**（示例代码，在成功运行官方示例之后追加试验；模型输出**待本地验证**）：

1. 运行官方示例，记录正常输出。
2. 把 `batch_decode` 的输入从 `generated_ids_trimmed` 换成 `generated_ids`（即不裁剪），再跑一次：

```python
# 示例代码：对照组——不裁剪直接解码
response_raw = processor.batch_decode(
    generated_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False
)[0]
print(response_raw)
```

3. 再做一次「不丢特殊 token」的对照：保留 `generated_ids_trimmed`，但把 `skip_special_tokens` 改为 `False`，观察输出里多出了什么。

**需要观察的现象**：第 2 步的开头是否复述了你的问题（以及图像占位符相关的处理痕迹）；第 3 步输出中是否出现 `<...>` 形式的控制标记（如助手结束标记）。

**预期结果**：不裁剪时回答前会拼着完整的输入 prompt；不丢特殊 token 时输出末尾应能看到生成结束的控制标记（具体 token 样式**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `zip(inputs.input_ids, generated_ids)` 用 `inputs.input_ids` 而不是重新解码一遍 `text` 来确定输入长度？

参考答案：`len(in_ids)` 需要的是** token 个数**，`inputs.input_ids` 就是编码后的 token 序列，取长度精确且零开销；而把 `text` 再解码/再分词一遍既多余，还可能因模板与处理器的差异对不上号。以 processor 的产物为唯一基准，是这类代码的通用原则。

**练习 2**：`max_new_tokens=512` 意味着什么？如果回答被截断了，改哪里？

参考答案：它限制**新生成**的 token 数上限（不含输入）。回答没说完就被切断时，调大这个值即可；它不会影响输入长度，输入侧的约束是 processor 的 `truncation` 与模型 128K 上下文窗口。

**练习 3**：`skip_special_tokens=True` 丢掉的特殊 token 里，哪一类其实承担着「停止生成」的功能？

参考答案：结束符（EOS 类）token。它在前向循环里触发停止条件；解码阶段则作为控制符号被过滤掉，不应出现在给用户看的文本里。这也解释了为什么必须保留这个参数——否则回答里会混入人眼不该看到的控制符号。

## 5. 综合实践

把三个模块串成一次完整的实验。本实践对应大纲任务：跑通官方示例 → 换 prompt → 对比 flash-attn 配置的显存。

**任务**：对 `figures/demo.png` 完成一次问答，并做两组对照实验，最终产出一份三行记录表。

**步骤**：

1. **跑通基线**。激活 `kimi-vl` 环境，在仓库根目录新建 `run_instruct.py`，粘贴 [README.md:L115-L154](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L115-L154) 的完整示例代码并运行（首次运行会下载约 32GB 权重，需要耐心与磁盘空间）。记录：模型对「穹顶建筑是什么」的回答。
2. **换任务**。只修改 messages 中的文字分片，换成图片描述任务，例如 `"Describe this image in detail."`；注意 `image` 分片路径与 `Image.open` 的路径保持同步（呼应 4.2 的「两条平行线」）。再跑一次，记录输出。
3. **显存对照**。用下面的骨架（示例代码）分别以「默认配置」与「flash-attn 配置」各启动一次进程，测量峰值显存：

```python
# 示例代码：显存测量骨架——两种配置各跑一个独立进程
import torch
from PIL import Image
from transformers import AutoModelForCausalLM, AutoProcessor

use_flash = False  # 第二次实验改为 True，并确认已安装 flash-attn

model_path = "moonshotai/Kimi-VL-A3B-Instruct"
kwargs = dict(torch_dtype=torch.bfloat16 if use_flash else "auto",
              device_map="auto", trust_remote_code=True)
if use_flash:
    kwargs["attn_implementation"] = "flash_attention_2"
model = AutoModelForCausalLM.from_pretrained(model_path, **kwargs)

torch.cuda.reset_peak_memory_stats()   # 加载后清零，只统计推理阶段的峰值
processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)
image = Image.open("./figures/demo.png")
messages = [{"role": "user", "content": [
    {"type": "image", "image": "./figures/demo.png"},
    {"type": "text", "text": "What is the dome building in the picture? Think step by step."}]}]
text = processor.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")
inputs = processor(images=image, text=text, return_tensors="pt", padding=True, truncation=True).to(model.device)
generated_ids = model.generate(**inputs, max_new_tokens=512)
trimmed = [o[len(i):] for i, o in zip(inputs.input_ids, generated_ids)]
print(processor.batch_decode(trimmed, skip_special_tokens=True)[0])
print(f"峰值显存: {torch.cuda.max_memory_allocated()/1024**3:.2f} GiB")
```

4. **记录表**（填写你的实测值）：

| 实验 | 配置 | 回答要点 | 峰值显存 (GiB) | 耗时 |
| --- | --- | --- | --- | --- |
| 基线问答 | `torch_dtype="auto"` | 待本地验证 | 待本地验证 | 待本地验证 |
| 图片描述 | 同上，改 prompt | 待本地验证 | 待本地验证 | 待本地验证 |
| flash-attn | `bfloat16` + `flash_attention_2` | 待本地验证 | 待本地验证 | 待本地验证 |

**预期结果**：三种配置都能得到通顺回答；flash-attn 配置的峰值显存应低于或不高于基线、生成速度更快（若基线 `auto` 已解析为 bfloat16，差距主要来自注意力实现而非权重精度，具体差值与显卡型号有关，**待本地验证**）。若第 3 步报 flash-attn 相关导入错误，回到 [u1-l2](u1-l2-environment-setup.md) 检查安装顺序（`pip install flash-attn --no-build-isolation`，装在 torch 之后）。

## 6. 本讲小结

- 多模态推理 = **两条加载通道**（`AutoModelForCausalLM` 出模型、`AutoProcessor` 出输入组装器），二者必须来自同一个 `model_path`，且都要 `trust_remote_code=True`。
- 输入组装走「**消息 → 模板文本 → 张量**」流水线：messages 里的图片分片只占位，真实像素由 `images=` 单独传入；`add_generation_prompt=True` 负责补「该助手作答」的引导段。
- `generate` 返回「输入 + 新生成」的完整序列，所以必须按各样本 `len(in_ids)` 裁剪后再 `batch_decode`，并用 `skip_special_tokens=True` 滤掉控制符号。
- 官方 Instruct 示例未开采样（默认贪心）；README 推荐 Instruct 用 `temperature=0.2`，要生效需 `do_sample=True`。
- flash-attn 推荐配置是 `torch_dtype=torch.bfloat16` 与 `attn_implementation="flash_attention_2"` 成对出现，收益是显存从 \( O(n^2) \) 降为 \( O(n) \) 量级、推理提速。

## 7. 下一步学习建议

- 下一讲 [u2-l2 多图输入与 Thinking 模型](u2-l2-multi-image-thinking-inference.md)：在本讲单图骨架上扩展为 `images` 列表与多图片分片，并见识 Thinking-2506 的 `max_new_tokens=32768` 长思维链——届时你会明白 512 对思考型模型有多不够用。
- 若想深挖 `apply_chat_template` 渲染出了什么、`pixel_values` 形状怎么来的，[u2-l3 Processor 与聊天模板](u2-l3-processor-and-chat-template.md) 会把本讲 4.2 的黑盒彻底打开。
- 想看模型内部实现？带着本讲 4.1 实践记录的 `.py` 文件清单，去 HuggingFace 模型仓库的 Files 页按图索骥——那也是 u3-l1 架构解析的前置作业。

# vLLM 离线批量推理：从 Transformers 到 vLLM 的迁移

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「Transformers 推理」和「vLLM 离线推理」这两条路径各自的定位与分工：谁负责模板渲染、谁负责生成。
2. 用 `vllm.LLM` 加载 Kimi-VL，用 `SamplingParams` 控制生成长度等参数。
3. 理解 `multi_modal_data` 传入图像的方式，以及它与 Transformers 写法（`processor(images=..., text=...)` 产出张量）的本质差异。
4. 把单条请求改造成「一次传入一批图片」的离线批量推理脚本，这是 vLLM 相对 Transformers 最直观的收益点。

本讲所有代码均以 README 中的 vLLM 离线推理示例为锚点，该示例位于仓库 README 的「Deployment → Using vLLM → Offline Inference」小节。

## 2. 前置知识

### 2.1 什么是 vLLM，为什么要用它

前几讲我们一直用 Hugging Face Transformers 做推理：`model.generate()` 一次跑一个（或一小批）请求，简单直接，适合调试和交互式使用。但当你有几百上千张图片要批量打标、批量问答时，Transformers 这种「自己拼 batch、自己搬数据」的方式吞吐量有限。

vLLM 是一个专为高吞吐 LLM 推理设计的服务引擎。对本讲而言，你只需要记住它的两个外在特征：

1. **批量是引擎内部调度的**：你把一批 prompt 全部丢给 `llm.generate()`，引擎自己决定怎么把它们塞进 GPU、怎么排队，你不用手工拼 batch 张量。
2. **接口更「高层」**：它接收的是「文本 + 原始图像」这样的数据，而不是 `input_ids`、`pixel_values` 这类已经编码好的张量；返回的也直接是生成文本字符串，而不是 token id 序列。

vLLM 对 Kimi-VL 的支持是官方确认的：README 的 News 小节记录了 vLLM 主干分支在 2025.04.15 起支持 Kimi-VL 部署（见 [README.md:L48](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L48)，对应 vLLM 仓库 PR #16387），部署小节也明确写着「The vLLM main branch has supported Kimi-VL deployment」（见 [README.md:L211-L213](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L211-L213)）。

### 2.2 「离线推理」是什么意思

vLLM 有两种使用形态：

| 形态 | 入口 | 适用场景 | 对应讲义 |
| :--- | :--- | :--- | :--- |
| 离线推理（Offline Inference） | Python 代码里 `llm = LLM(...)` 然后 `llm.generate(...)` | 写脚本批量处理数据，不需要起服务 | 本讲 |
| OpenAI 兼容服务 | 命令行 `vllm serve ...` 起一个 HTTP 服务 | 对外提供 API、多客户端并发调用 | 下一讲 u3-l3 |

「离线」不是指断网，而是指**不起网络服务、在 Python 进程内直接调用引擎**。README 在离线推理小节上方专门给了提示：更多用法可查 vLLM 官方文档的 Offline Inference 页面（见 [README.md:L217-L218](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L217-L218)）。

### 2.3 你需要复习的前置认知

本讲高度依赖 u2-l3 建立的「输入组装三道工序」认知：

- `apply_chat_template` 把结构化 `messages` 渲染成带特殊 token 与图像占位符的扁平文本——**这一步在 vLLM 路径里原样保留**；
- `processor(images=..., text=...)` 把文本和像素编码成张量——**这一步在 vLLM 路径里被移交给 vLLM 引擎内部完成**；
- `generate` 之后要按输入长度裁剪再解码——**这一步在 vLLM 路径里直接消失**，引擎只把新生成的文本给你。

另外回顾 u1-l2 的结论：requirements.txt 锁定的是 torch 2.5.1 + transformers 4.51.3 这套「Transformers 推理」组合，里面**没有 vllm**。所以走 vLLM 路径需要额外安装，README 未指定 vllm 的具体版本号，安装方式以 vLLM 官方文档为准（待本地验证你环境下的可用版本）。

## 3. 本讲源码地图

本仓库是发布型仓库（回顾 u1-l3），没有 Python 源码，本讲的「源码」就是 README 中的官方示例代码与依赖清单：

| 文件 | 本讲涉及内容 | 作用 |
| :--- | :--- | :--- |
| `README.md` L220-L246 | vLLM 离线推理完整示例 | 本讲的精读对象，约 25 行官方代码 |
| `README.md` L115-L154 | Transformers Instruct 推理示例 | 迁移对照的「旧写法」 |
| `README.md` L211-L218 | vLLM 部署说明与文档指引 | 确认官方支持口径 |
| `requirements.txt` L1-L8 | 依赖清单 | 证明 vllm 需另行安装 |
| `figures/`（7 张图） | 实践任务的素材 | demo.png / demo1.png / demo2.png 为推理输入图，其余 4 张为文档插图 |

永久链接 base 为当前 HEAD `41d5ef0`，下文所有链接均可直接点击跳转。

## 4. 核心概念与源码讲解

先看官方示例全貌（即本讲全篇的精读锚点），再拆成三个最小模块：

```python
# 节选自 README vLLM Offline Inference 示例（L220-L246）
from PIL import Image
from transformers import AutoProcessor
from vllm import LLM, SamplingParams

model_path = "moonshotai/Kimi-VL-A3B-Instruct"
llm = LLM(model_path, trust_remote_code=True)
processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)
text = processor.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")
outputs = llm.generate(
    [{"prompt": text, "multi_modal_data": {"image": image}}],
    sampling_params=SamplingParams(max_tokens=512),
)
```

### 4.1 LLM 对象初始化：模板留在 transformers，生成交给 vLLM

#### 4.1.1 概念说明

这个模块回答一个问题：**从 Transformers 迁移到 vLLM，模型加载这一步到底换了什么？**

答案有点反直觉：**只换了一半**。

- 换掉的是「生成引擎」：`AutoModelForCausalLM` + `model.generate()` → `vllm.LLM` + `llm.generate()`。vLLM 引擎自己完成权重加载、显存管理与调度，所以不再需要 `device_map` 这类手工设备分配参数。
- 保留的是「输入组装器」：`AutoProcessor` 依然从 transformers 导入、依然用 `trust_remote_code=True` 加载。因为聊天模板（chat template）定义在 HuggingFace 模型仓库的配置里，vLLM 路径同样需要它把 `messages` 渲染成模型认识的文本。

一句话总结这个分工：**apply_chat_template 负责「说什么」，vLLM 负责「怎么算」**。

#### 4.1.2 核心流程

```text
pip install vllm（requirements.txt 之外的额外安装）
        │
        ▼
LLM(model_path, trust_remote_code=True)
        │  vLLM 引擎：下载/加载 16B 权重、预分配 KV cache
        ▼
AutoProcessor.from_pretrained(model_path, trust_remote_code=True)
        │  transformers：加载聊天模板 + 图像预处理配置
        ▼
（进入 4.2：渲染文本、组装多模态输入）
```

注意两个加载调用必须指向**同一个** `model_path`——模板和权重来自同一个模型仓库，混用 Instruct 和 Thinking-2506 的路径会导致模板与权重不匹配。

#### 4.1.3 源码精读

导入区三行，分别来自三个包，恰好对应本讲的分工：

> [README.md:L221-L223](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L221-L223) 从 PIL 导入 Image（打开图片文件）、从 transformers 导入 AutoProcessor（渲染聊天模板）、从 vllm 导入 LLM 与 SamplingParams（推理引擎与采样参数）。注意这里没有导入任何 `AutoModel*`——模型本体由 vLLM 引擎接管。

初始化 LLM 只传了两个参数：

> [README.md:L225-L229](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L225-L229) 指定 model_path（注释说明同一份代码换路径即可跑 Thinking-2506），并创建 `llm = LLM(model_path, trust_remote_code=True)`。对比 Transformers 示例里的 `torch_dtype="auto"` 和 `device_map="auto"`（[README.md:L121-L126](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L121-L126)），这两个参数在这里都不需要了——精度与设备分配由 vLLM 引擎自行管理。

processor 紧随其后、写法与 Transformers 路径完全一致：

> [README.md:L231](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L231) `AutoProcessor.from_pretrained(model_path, trust_remote_code=True)`。这一行与 Transformers 示例的 [README.md:L137](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L137) 一字不差——它是两条路径共用的「输入组装器」。

而 requirements.txt 里确实没有 vllm 这一项：

> [requirements.txt:L1-L8](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/requirements.txt#L1-L8) 依赖清单只有 torch、torchvision、transformers、pillow、tiktoken、accelerate、blobfile、openai 八项。vLLM 是独立的推理引擎，需按其官方文档单独安装（具体可用的 vllm 版本：待本地验证）。

#### 4.1.4 代码实践

**实践目标**：完成 vLLM 环境准备，并观察 LLM 初始化时引擎做了哪些事。

**操作步骤**：

1. 在 u1-l2 建好的 conda 环境基础上另建一个环境（vLLM 对 torch 版本有自己的要求，避免污染原环境）：

   ```bash
   conda create -n kimi-vl-vllm python=3.10 -y
   conda activate kimi-vl-vllm
   pip install vllm   # 版本以 vLLM 官方文档为准
   ```

2. 验证安装：

   ```bash
   python -c "import vllm; print(vllm.__version__)"
   ```

3. 在脚本里只保留初始化两行，打印确认对象创建成功：

   ```python
   # 示例代码：仅验证初始化，不生成
   from vllm import LLM
   from transformers import AutoProcessor

   model_path = "moonshotai/Kimi-VL-A3B-Instruct"
   llm = LLM(model_path, trust_remote_code=True)
   processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)
   print(type(llm), type(processor))
   ```

**需要观察的现象**：`LLM(...)` 执行时会打印大量引擎日志（权重下载进度、权重加载、KV cache 显存预分配等），耗时明显长于 `AutoProcessor` 的加载。

**预期结果**：末尾打印出 `llm` 与 `processor` 的类型名，进程正常退出。首次运行需要下载约 16B 参数的权重，请确保磁盘与显存充足（待本地验证：你的 GPU 显存是否足够单卡装载；不足时下一讲的 `--tensor-parallel-size` 是出路之一）。

#### 4.1.5 小练习与答案

**练习 1**：vLLM 路径为什么仍然要 `AutoProcessor.from_pretrained(..., trust_remote_code=True)`？它和 `LLM(...)` 各自负责什么？

<details>
<summary>参考答案</summary>

`AutoProcessor` 负责输入侧：加载模型仓库里的聊天模板与图像预处理配置，用来执行 `apply_chat_template`（后续还会用它管理图像预处理）。`LLM` 负责计算侧：加载权重、管理显存、执行生成。两者共用同一个 model_path，缺一不可——没有 processor 就渲染不出模型认识的输入文本，没有 LLM 就没有生成引擎。
</details>

**练习 2**：README 的 Transformers 示例加载模型时传了 `torch_dtype="auto"` 和 `device_map="auto"`（L123-L124），vLLM 示例的 `LLM(...)` 却只传了 `trust_remote_code`，为什么参数变少了？

<details>
<summary>参考答案</summary>

因为职责转移了：Transformers 路径下这两项是让用户手工指定「权重精度」和「放哪块卡」；vLLM 是完整的推理引擎，权重精度与设备分配由引擎内部按默认策略和硬件情况自行管理，示例因此只需要 `trust_remote_code=True` 这一个必要开关（模型实现代码托管在 HuggingFace 模型仓库，回顾 u1-l1）。
</details>

### 4.2 多模态提示构造：从「张量对」到「prompt + multi_modal_data」

#### 4.2.1 概念说明

这是迁移中**变化最大**的一环。u2-l3 讲过，Transformers 路径的输入组装是：

```python
inputs = processor(images=image, text=text, return_tensors="pt", ...)
# 产出 input_ids / attention_mask / pixel_values 等张量
generated_ids = model.generate(**inputs, ...)
```

你亲手把「文本 + 像素」编码成张量，再喂给模型。而 vLLM 路径把这一步**外包给了引擎**：

```python
outputs = llm.generate(
    [{"prompt": text, "multi_modal_data": {"image": image}}],
    sampling_params=...,
)
```

你交出去的是「渲染好的文本字符串 + 原始 PIL 图像对象」，编码成张量的工作在 vLLM 内部完成。数据结构上有三个要点：

1. **`generate` 的第一个参数是「字典的列表」**——列表里有几个字典，就是几条请求。示例只放了一个元素，把它扩成 N 个就是批量推理（4.3 详解）。
2. **每个字典有两个键**：`prompt` 放 `apply_chat_template` 渲染出的文本；`multi_modal_data` 放多模态原始数据，`{"image": image}` 里的 image 是 PIL 图像对象。
3. **两通道对齐的硬约束依然成立**（承接 u2-l2）：`prompt` 文本里的图像占位符数量/顺序，必须与 `multi_modal_data` 里实际传入的图像严格一致，否则引擎无法把像素对齐到占位符位置。

#### 4.2.2 核心流程

两条路径的对照流程：

```text
【Transformers 路径】                       【vLLM 路径】
messages                                    messages
  │ apply_chat_template                       │ apply_chat_template（完全相同）
  ▼                                           ▼
text（含图像占位符的文本）                    text（含图像占位符的文本）
  │ processor(images=..., text=...)           │
  ▼                                           ▼
input_ids + pixel_values 等张量              {"prompt": text,
  │                                             "multi_modal_data": {"image": PIL对象}}
  ▼                                           │
model.generate(**inputs)                     llm.generate([字典, 字典, ...])
  │                                           │ 编码与占位符对齐在 vLLM 引擎内完成
  ▼                                           ▼
generated_ids（输入+生成，需手工裁剪）        outputs[i].outputs[0].text（纯生成文本）
```

关键认知：**`apply_chat_template` 这一步原封不动地留在你的代码里**，所以 u2-l3 学到的 messages 结构、`add_generation_prompt=True`、图像占位符等知识全部平移适用。

#### 4.2.3 源码精读

官方示例的输入构造四步：

> [README.md:L233-L234](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L233-L234) 打开图像文件得到 PIL 对象。注意：这里图像必须真的被 `Image.open` 打开传入 `multi_modal_data`，而 `messages` 里的 `image_path` 字符串只是占位描述，两通道各司其职（与 u2-l1 的双通道认知一致）。

> [README.md:L235-L237](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L235-L237) 构造 messages：content 是分片列表，一个 `image` 分片加一个 `text` 分片。这段与 Transformers 示例的 [README.md:L141-L143](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L141-L143) 逐字相同——messages 的写法在两条路径间完全通用。

> [README.md:L238](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L238) `text = processor.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")` 渲染出带特殊 token 与图像占位符的文本。注意与 Transformers 路径（L144）相比一字未改——这就是 4.1 说的「模板留在 transformers 侧」。

> [README.md:L239](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L239) 调用 `llm.generate([{"prompt": text, "multi_modal_data": {"image": image}}], ...)`。对照 Transformers 路径的 [README.md:L145](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L145)：`processor(images=image, text=text, ...)` 的「两个并行输入」在这里变成了一个字典的两个键，且传入的不再是编码后的张量而是原始文本与 PIL 图像。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「交给 vLLM 的到底是什么」，把两条路径的输入形态差异落到可打印的证据上。

**操作步骤**：

```python
# 示例代码：只做输入构造，不加载 vLLM 引擎，CPU 即可运行
from PIL import Image
from transformers import AutoProcessor

model_path = "moonshotai/Kimi-VL-A3B-Instruct"
processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)

image_path = "./figures/demo.png"
image = Image.open(image_path)
messages = [
    {"role": "user", "content": [
        {"type": "image", "image": image_path},
        {"type": "text", "text": "What is the dome building in the picture? Think step by step."},
    ]}
]
text = processor.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")

print("=== 交给 vLLM 的 prompt ===")
print(text)
print("=== 交给 vLLM 的图像 ===")
print(type(image), image.size)

# 对照：Transformers 路径在同一行 text 之后会做什么
inputs = processor(images=image, text=text, return_tensors="pt", padding=True, truncation=True)
print("=== Transformers 路径产出的张量 ===")
for k, v in inputs.items():
    print(k, v.shape)

# vLLM 真正接收的请求字典长这样：
request = {"prompt": text, "multi_modal_data": {"image": image}}
print("=== vLLM 请求字典的键 ===")
print(request.keys(), type(request["multi_modal_data"]["image"]))
```

**需要观察的现象**：

1. `text` 里能看到图像占位符和 `<|im_start|>` 一类特殊 token（占位符的具体形态以实测输出为准）；
2. `inputs` 字典里有 `input_ids`、`attention_mask`、`pixel_values` 等键及各自形状——这些在 vLLM 路径里**不会出现在你的代码中**；
3. 请求字典里只有 `prompt`（str）和 `multi_modal_data.image`（PIL 对象）。

**预期结果**：三段打印依次呈现「文本 + 原始图像 → 张量」与「文本 + 原始图像」两种输入形态，直观验证 4.2.2 的对照流程图。（本实践只需下载 processor 配置文件，无需下载完整权重，待本地验证具体输出。）

#### 4.2.5 小练习与答案

**练习 1**：把官方示例改成两张图（比如 demo1.png 和 demo2.png），请求字典应该怎么写？

<details>
<summary>参考答案</summary>

messages 里放两个 `image` 分片（参照 [README.md:L183-L190](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L183-L190) Thinking-2506 的多图写法），`apply_chat_template` 照常渲染；请求字典改为 `{"prompt": text, "multi_modal_data": {"image": [image1, image2]}}`——图像变成列表，且数量、顺序与 prompt 里的占位符一致。（vLLM 中多图传列表的具体键写法以官方文档为准，待本地验证。）
</details>

**练习 2**：为什么 vLLM 路径的代码里看不到 `pixel_values`，但模型依然「看得见」图？

<details>
<summary>参考答案</summary>

因为「PIL 图像 → pixel_values 张量 → 与占位符对齐」这整套编码工作被移进了 vLLM 引擎内部：你通过 `multi_modal_data` 交出原始图像，引擎内部完成与 Transformers 路径等价的预处理和拼接。分工从「用户编码、模型计算」变成了「用户给数据、引擎全包」。
</details>

### 4.3 SamplingParams 与批处理：参数搬家、输出瘦身

#### 4.3.1 概念说明

第三个模块覆盖两件事：**采样参数怎么传** 和 **一批请求怎么发**。

**参数搬家**：Transformers 路径里，生成长度、温度等都作为 `model.generate()` 的关键字参数（如 `max_new_tokens=512, temperature=0.8`）；vLLM 把它们抽成了一个独立对象 `SamplingParams`，在调用 `generate` 时整体传入。注意一个易踩的命名差异：

- Transformers：`max_new_tokens`
- vLLM：`max_tokens`

两者语义相同——都是「新生成 token 数的上限」。官方示例里 `SamplingParams(max_tokens=512)` 与 Transformers 示例里 `max_new_tokens=512`（[README.md:L146](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L146)）是同一件事的两种写法。另外承接 u2-l4：Instruct 推荐温度 0.2、Thinking 推荐 0.8，本示例未传温度（走 vLLM 默认值），需要时在 `SamplingParams` 里加即可。

**输出瘦身**：u2-l1/u2-l3 反复强调 Transformers 路径必须「按输入长度裁剪再解码」：

```python
generated_ids_trimmed = [out_ids[len(in_ids):] for ...]   # 手工裁掉输入部分
response = processor.batch_decode(generated_ids_trimmed, ...)
```

vLLM 路径里这三行全部消失——`o.outputs[0].text` 直接就是**只含新生成内容**的文本字符串。这是迁移中最能提升代码简洁度的一点。

**批处理**：`llm.generate` 第一个参数是列表。示例里列表只有一个元素；当你放进去 N 个请求字典，引擎会在内部持续调度这批请求（而不是等最长的那个生成完才返回下一批），这正是 vLLM 高吞吐的来源，也是「离线批量推理」的价值所在。

#### 4.3.2 核心流程

```text
构造 N 个请求字典：
  requests = [
      {"prompt": text_1, "multi_modal_data": {"image": img_1}},
      {"prompt": text_2, "multi_modal_data": {"image": img_2}},
      ...
  ]
        │
        ▼
一次调用：
  outputs = llm.generate(requests, sampling_params=SamplingParams(max_tokens=512))
        │  引擎内部：编码 → 排队调度 → 逐条生成
        ▼
outputs 是与 requests 等长的列表：
  outputs[i].outputs[0].text  ← 第 i 条请求的新生成文本（无需裁剪、无需解码）
```

补充说明输出对象的层级：`outputs` 的每个元素对应一条请求；每个元素的 `.outputs` 是该请求的候选回答列表（默认采样一份，所以取 `[0]`），其 `.text` 是生成文本。

#### 4.3.3 源码精读

生成调用与参数对象：

> [README.md:L239](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L239) `outputs = llm.generate([...], sampling_params = SamplingParams(max_tokens=512))`。对照 Transformers 路径 [README.md:L146](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L146) 的 `model.generate(**inputs, max_new_tokens=512)`：生成长度从 generate 的关键字参数变成了独立的 SamplingParams 对象，参数名从 max_new_tokens 变成 max_tokens。

输出读取与打印：

> [README.md:L241-L245](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L241-L245) 遍历 `outputs`，直接取 `o.outputs[0].text` 打印。对照 Transformers 路径的裁剪加解码三件套（[README.md:L147-L152](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L147-L152)）：vLLM 已把「输入 + 生成」的完整序列替你处理成纯生成文本，`generated_ids_trimmed` 与 `batch_decode` 都不再需要。

一个隐含的批量线索：官方示例虽然只传了一条请求，但 `for o in outputs` 的循环写法天然为多条输出预留了结构——把输入列表扩长，这段输出代码一行都不用改。

#### 4.3.4 代码实践

**实践目标**：亲手把「单条请求」变成「两条请求的批」，验证 `outputs` 列表与输入一一对应。

**操作步骤**：

```python
# 示例代码：在官方示例（README L220-L246）基础上做最小改动
from PIL import Image
from transformers import AutoProcessor
from vllm import LLM, SamplingParams

model_path = "moonshotai/Kimi-VL-A3B-Instruct"
llm = LLM(model_path, trust_remote_code=True)
processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)

# 两条请求：同一张图 + 两个不同问题
image_path = "./figures/demo.png"
image = Image.open(image_path)

def build_request(question):
    messages = [
        {"role": "user", "content": [
            {"type": "image", "image": image_path},
            {"type": "text", "text": question},
        ]}
    ]
    text = processor.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")
    return {"prompt": text, "multi_modal_data": {"image": image}}

requests = [
    build_request("What is the dome building in the picture? Think step by step."),
    build_request("Describe the overall style of this photo in one sentence."),
]

outputs = llm.generate(requests, sampling_params=SamplingParams(max_tokens=512))

print("-" * 50)
print("本批输出条数:", len(outputs))          # 预期：2，与 requests 等长
for i, o in enumerate(outputs):
    print(f"[请求 {i}] prompt 长度: {len(o.prompt)} 字符")
    print(o.outputs[0].text)
    print("-" * 50)
```

**需要观察的现象**：

1. `len(outputs)` 等于 2，与输入列表长度一致；
2. 两条回答内容分别对应两个问题，没有串位；
3. 引擎日志中能看到请求被处理的过程（打印格式与进度条样式以实际运行输出为准）。

**预期结果**：两段与各自问题匹配的回答依次打印。（待本地验证：批处理耗时通常小于把两条请求各调一次 `generate` 的总耗时，可以顺手计时对比。）

#### 4.3.5 小练习与答案

**练习 1**：把官方 Transformers 示例的 `model.generate(**inputs, max_new_tokens=512)` 迁移成 vLLM 写法，参数应该放到哪里、名字叫什么？

<details>
<summary>参考答案</summary>

放进 `SamplingParams`，参数名从 `max_new_tokens` 改为 `max_tokens`：`llm.generate(requests, sampling_params=SamplingParams(max_tokens=512))`。温度同理：Transformers 的 `temperature=0.8` 迁移为 `SamplingParams(max_tokens=..., temperature=0.8)`。
</details>

**练习 2**：vLLM 路径为什么不需要 `generated_ids_trimmed` 那段裁剪代码？

<details>
<summary>参考答案</summary>

因为两条路径返回的东西不同。Transformers 的 `model.generate` 返回「输入 token + 新生成 token」拼在一起的 id 序列，所以必须先按各样本输入长度裁掉前缀、再 `batch_decode` 成文字；vLLM 的 `outputs[i].outputs[0].text` 是引擎处理好的纯生成文本，裁剪与解码都已在引擎侧完成。
</details>

**练习 3**：同一批 10 条请求，两种做法——(a) 循环 10 次每次 `llm.generate([一条])`；(b) 一次 `llm.generate(十条列表)`。哪种通常更快？为什么？

<details>
<summary>参考答案</summary>

通常 (b) 更快。(a) 每次调用引擎只能看到一条请求，前后两批之间硬件可能闲置；(b) 把全部请求一次交给引擎，它可以在内部持续把多条请求排进同一轮计算、动态填充空位，减少硬件空闲时间。这也正是 4.3.1 说的「离线批量推理」价值所在（具体加速比取决于硬件与负载，待本地验证）。
</details>

## 5. 综合实践：figures 目录批量问答脚本

**任务**：把官方单图示例改造成批量脚本——对 `figures/` 下所有图片提出同一个问题，用 `SamplingParams(max_tokens=512)` 一次生成，并打印每张图的回答。这个任务综合了本讲全部三个模块：LLM 初始化（4.1）、批量请求字典构造（4.2）、SamplingParams 与输出遍历（4.3）。

**参考实现**（示例代码）：

```python
# batch_infer.py —— Kimi-VL vLLM 离线批量推理（基于 README L220-L246 官方示例改造）
from pathlib import Path

from PIL import Image
from transformers import AutoProcessor
from vllm import LLM, SamplingParams

model_path = "moonshotai/Kimi-VL-A3B-Instruct"  # or "moonshotai/Kimi-VL-A3B-Thinking-2506"
llm = LLM(model_path, trust_remote_code=True)
processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)

QUESTION = "What is shown in this picture? Answer in one or two sentences."

# 1. 收集 figures 下所有图片（7 张：demo 系列 3 张 + 文档插图 4 张）
image_dir = Path("./figures")
image_paths = sorted(p for p in image_dir.iterdir() if p.suffix == ".png")

# 2. 逐张构造请求字典：模板渲染（transformers 侧）+ 原始图像（vLLM 侧）
requests = []
for path in image_paths:
    image = Image.open(path)
    messages = [
        {"role": "user", "content": [
            {"type": "image", "image": str(path)},
            {"type": "text", "text": QUESTION},
        ]}
    ]
    text = processor.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")
    requests.append({"prompt": text, "multi_modal_data": {"image": image}})

print(f"共 {len(requests)} 条请求，开始批量生成……")

# 3. 一次调用，批量生成
outputs = llm.generate(requests, sampling_params=SamplingParams(max_tokens=512))

# 4. 遍历输出（outputs 与 requests 等长，.text 已是纯生成文本）
for path, o in zip(image_paths, outputs):
    print("=" * 60)
    print(f"图片: {path}")
    print("-" * 60)
    print(o.outputs[0].text)
```

**运行方式**（在有 vllm 环境与 GPU 的机器上，从仓库根目录执行）：

```bash
python batch_infer.py
```

**验收要点**：

1. 输出条数 = 图片数（7），每条回答与图片名对应；
2. demo.png 的回答应能识别出穹顶建筑（与官方示例同一张图）；arch.png、instruct_perf.png 等文档插图得到的是「对图内容的一般描述」——它们本来就是给人看的图表，模型答不出「正确标答」是正常现象，重点在于验证批处理链路；
3. 观察 vLLM 的进度条：一批请求整体推进，而不是一张图跑完再跑下一张。

**延伸思考**：如果想把文档插图排除、只跑 demo 系列，只需改 `image_paths` 的过滤条件（如 `if p.name.startswith("demo")`）；如果想换 Thinking-2506，改 `model_path` 的同时应把 `max_tokens` 调大并加 `temperature=0.8`（承接 u2-l2/u2-l4 的参数口径）。（本综合实践全程待本地验证。）

## 6. 本讲小结

- **分工模式**：vLLM 路径 = 「transformers 出模板 + vLLM 出算力」。`AutoProcessor` 与 `apply_chat_template` 原样保留，`AutoModelForCausalLM` + `model.generate` 换成 `vllm.LLM` + `llm.generate`。
- **输入形态变化**：`processor(images=..., text=...)` 产出的张量对，变成 `{"prompt": 渲染文本, "multi_modal_data": {"image": PIL图像}}` 请求字典；编码与占位符对齐移入引擎内部，但「占位符与图像数量、顺序一致」的硬约束不变。
- **参数与输出**：采样参数搬进独立的 `SamplingParams` 对象（`max_new_tokens` 改名 `max_tokens`）；输出侧的「裁剪 + 解码」三行代码消失，`o.outputs[0].text` 直接是纯生成文本。
- **批量是第一公民**：`llm.generate` 收字典列表，一次调用处理一批请求，引擎内部统一调度，这是离线批量推理相对逐条 Transformers 推理的核心收益。
- **环境提示**：vllm 不在 requirements.txt 中，需按 vLLM 官方文档单独安装；README 同时指明该支持自 2025.04.15 起进入 vLLM 主干分支。

## 7. 下一步学习建议

本讲跑通的是「进程内离线推理」。下一讲 **u3-l3（vLLM OpenAI 兼容服务）** 将进入服务化形态：用 `vllm serve` 把 Kimi-VL 起成一个 HTTP 服务，理解 `--max-model-len`、`--limit-mm-per-prompt`、`--tensor-parallel-size` 等启动参数（它们与本讲提到的 128K 上下文、多图上限、多卡显存问题一一对应），并用 openai SDK 以 base64 图片的形式调用。

建议的衔接练习：把第 5 节的批量脚本改造成「计时版」，记录本机批量推理的吞吐，等学完 u3-l3 后用同样的 7 张图对比「服务化接口」的延迟与吞吐，你会对两种部署形态的取舍有第一手体感。

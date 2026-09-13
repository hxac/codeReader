# 综合实战：基于 Kimi-VL 构建多模态问答小应用

## 1. 本讲目标

这是学习手册的收官之讲。前面 13 讲我们分别掌握了模型加载、消息构造、模板渲染、多图输入、Thinking 参数、vLLM 离线推理与服务化部署，但它们都是「孤立片段」。本讲要把这些片段组装成一条完整的应用流水线，读完本讲你应该能够：

1. 把「图像输入 → 消息组装 → 推理后端 → 输出解析 → 结构化展示」五段链路设计成一个可扩展的命令行程序。
2. 用工程化方式封装两类图像输入：给本地 Transformers / vLLM 的 PIL 对象通道，给 OpenAI 兼容服务的 base64 data URL 通道。
3. 编写一个兼容双后端（本地 Transformers 与 vLLM 服务）的 CLI 小工具，把 Thinking 模型的原始输出解析为 `thinking` 与 `answer` 两个字段。
4. 依据延迟、token 成本、并发需求，在「进程内推理」与「常驻服务」两种部署形态之间做出有依据的选型。

## 2. 前置知识

本讲是综合实战，默认你已完成 u2 与 u3 单元。用到的核心结论只做一句话回顾，不再展开：

- **messages 与占位通道**：`messages` 里 `type: "image"` 的分片只负责占位与定序，真实像素通过 `processor(images=...)` 独立传入，两边数量相等、顺序一致（u2-l1、u2-l3）。
- **Thinking 输出口径**：Thinking 模型输出 = 思维链（CoT）+ 最终答案，官方推荐 `max_new_tokens=32768`、`temperature=0.8`；温度需配合 `do_sample=True` 才生效；思考/答案的分界标记要用 `skip_special_tokens=False` 解码实测确认（u2-l2）。
- **vLLM 服务形态**：`vllm serve` 把模型变成常驻 HTTP 服务，默认 `8000` 端口，暴露 OpenAI 兼容接口；openai SDK 只需改 `base_url` 即可接入（u3-l3）。
- **视觉 token 估算**：MoonViT 原生分辨率下，视觉 token 数 ≈ 像素总数 ÷ 784（14×14 的 patch 再经 2×2 pixel shuffle 压缩，u3-l1、u3-l3）。

本讲新引入的工程概念：

| 术语 | 含义 |
| :--- | :--- |
| 后端抽象（Backend） | 一个约定了 `ask(image_paths, question)` 签名的接口，本地推理与服务调用分别实现它，上层代码不感知差异 |
| data URL | `data:image/jpeg;base64,<编码串>` 形式的内嵌资源地址，OpenAI 兼容接口用它把图片「塞进」JSON 请求 |
| usage 统计 | OpenAI 兼容接口返回的 `prompt_tokens` / `completion_tokens` 计数，是核算 token 成本的官方口径 |
| 冷启动 / 热服务 | 进程内推理每次启动都要加载权重；常驻服务只加载一次，之后每次请求都是「热」的 |

## 3. 本讲源码地图

本仓库是发布型仓库（u1-l3 已确认），不含 Python 源码，**README 中的官方代码块就是我们唯一可引用的「源码」**。本讲涉及：

| 文件 | 作用 |
| :--- | :--- |
| [README.md](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md) | 官方示例的唯一来源：Transformers 双变体推理（第 6 节）、vllm serve 命令与 OpenAI API 调用（第 8 节）、温度推荐（第 4 节注释） |
| [figures/demo.png](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/figures/demo.png) | 官方测试图，被 Transformers、vLLM 离线、OpenAI API 三段示例共用，也是本讲实践的验证素材 |
| [requirements.txt](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/requirements.txt) | 本地后端依赖清单（注意：vllm 与 openai 服务调用所需依赖不在其中，需另行安装） |

> 行号说明：本讲所有行号基于当前 HEAD `41d5ef0`。本讲自己编写的程序代码均标注「示例代码」，与官方示例严格区分。

## 4. 核心概念与源码讲解

### 4.1 应用链路设计

#### 4.1.1 概念说明

跑通一次推理和做成一个应用，差距在于**链路的稳定性与可替换性**。README 的示例代码是「脚本式」的：从上到下一口气执行，换一个后端就要整段重写。应用化设计的第一步，是把这条链路切成五个职责单一的环节：

1. **输入解析**：从命令行拿到图片路径列表与问题文本。
2. **图像入口**：把磁盘文件变成后端需要的形态（PIL 对象或 base64 字符串）。
3. **消息组装**：构造 `messages`（占位通道）并渲染成后端需要的输入。
4. **推理后端**：真正调用模型，得到原始文本输出。
5. **输出解析与展示**：切分思维链与答案，连同耗时、token 统计一起结构化输出。

关键设计决策是**后端抽象**：定义统一接口 `ask(image_paths, question) -> dict`，让本地 Transformers 与 vLLM 服务各自实现。这样上层 CLI 完全不感知后端差异，将来要加 vLLM 离线后端（u3-l2 的形态）也只是新增一个类。

#### 4.1.2 核心流程

```text
命令行参数 (--backend local|service, --image ..., --question ...)
        │
        ▼
┌─────────────────┐     ┌──────────────────────┐
│  输入解析 (CLI)  │────▶│  图像入口 load_image  │
└─────────────────┘     │  /encode_image_base64 │
                        └──────────┬───────────┘
                                   │ PIL 对象 / data URL
                                   ▼
                        ┌──────────────────────┐
                        │  消息组装 build_messages│  ← 占位通道 + 问题文本
                        └──────────┬───────────┘
              ┌─────────────────────┴─────────────────────┐
              ▼                                           ▼
   ┌─────────────────────┐                    ┌─────────────────────┐
   │ TransformersBackend │                    │ VLLMServiceBackend  │
   │ apply_chat_template │                    │ base64 + image_url  │
   │ processor → 张量     │                    │ openai SDK → HTTP   │
   │ model.generate      │                    │ chat.completions    │
   └──────────┬──────────┘                    └──────────┬──────────┘
              │ 原始文本 raw                              │ 原始文本 raw + usage
              └─────────────────────┬─────────────────────┘
                                   ▼
                        ┌──────────────────────┐
                        │ split_thinking(raw)  │  → thinking / answer
                        └──────────┬───────────┘
                                   ▼
                        JSON 结构化输出（含 elapsed、usage）
```

两条后端分支的差异被收敛在各自的 `ask()` 里；分叉点之前的图像入口与消息组装、汇合点之后的输出解析，全部复用。

#### 4.1.3 源码精读

官方 Thinking-2506 的 Transformers 示例是本地后端的骨架，逐段对应到我们的应用环节：

[README.md:L156-L201](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L156-L201)——Kimi-VL-A3B-Thinking-2506 的完整推理示例，本讲应用链路的官方蓝本。

- [README.md:L163-L169](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L163-L169)：加载 `moonshotai/Kimi-VL-A3B-Thinking-2506`，`torch_dtype="auto"`、`device_map="auto"`、`trust_remote_code=True` 三件套——对应应用中 `TransformersBackend.__init__`（只做一次，构造期完成）。
- [README.md:L181-L190](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L181-L190)：多图输入的两条通道——`image_paths` 列表喂给 messages 做占位（L187），`images = [Image.open(path) ...]`（L182）是真实像素通道——对应应用的 `build_messages` 与 `load_image`。
- [README.md:L191-L192](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L191-L192)：`apply_chat_template` 渲染出带图像占位符的文本，再由 `processor(images=..., text=...)` 编码为张量——u2-l3 精讲过的两道工序，在应用里原样保留。
- [README.md:L193](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L193)：`model.generate(**inputs, max_new_tokens=32768, temperature=0.8)`——Thinking 的官方推荐参数。注意官方示例未写 `do_sample=True`，u2-l1 已确认温度只有配合采样才生效，应用代码中我们会补上。
- [README.md:L194-L200](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L194-L200)：按输入长度裁剪再 `batch_decode`——这是 u2-l1 讲过的「生成后处理」标准写法，对应应用中的输出环节，但我们要在解码后多加一步 `split_thinking`。

服务端的官方蓝本则是：

[README.md:L268-L297](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L268-L297)——OpenAI 兼容服务的完整调用代码，`VLLMServiceBackend` 的骨架（图像编码部分下一模块精读）。

对照两段蓝本可以发现：**两条链路中「消息组装」之前的语义完全一致（用户 + 若干图片 + 一个问题），差异只在占位分片的类型（`image` vs `image_url`）与像素的载体（PIL 对象 vs base64 字符串）**。这正是后端抽象成立的根本原因。

#### 4.1.4 代码实践：调用链映射表

这是一个源码阅读型实践，目的是让你在写代码前先想清楚每一环节的官方出处。

1. **实践目标**：建立「应用函数 → README 官方代码行」的映射表。
2. **操作步骤**：
   - 在纸上或文档里画出 4.1.2 的流程图；
   - 对每个环节（加载 / 图像入口 / 消息组装 / 编码 / 生成 / 裁剪解码）写出它对应 README 的哪几行（本地链路查 L158-L201，服务链路查 L268-L297）；
   - 标出两条链路「语义相同但写法不同」的环节（提示：占位分片类型、像素载体、输出是否需要手工解码）。
3. **需要观察的现象**：两条链路在哪个环节彻底分叉、在哪个环节重新汇合。
4. **预期结果**：得到一张 6~8 行的映射表；分叉点在「消息组装」（`image` vs `image_url`），汇合点在「拿到原始文本 raw 之后」。
5. 映射表内容无法在本地「运行」，但它决定了 5 节代码的结构，属设计验证，无需标注待验证。

#### 4.1.5 小练习与答案

**练习 1**：如果要把 u3-l2 的 vLLM 离线推理也接入本应用，需要新增什么？要改动现有后端吗？

<details>
<summary>参考答案</summary>

新增一个 `VLLMOfflineBackend` 类，实现同样的 `ask(image_paths, question)` 接口：内部沿用 `AutoProcessor` + `apply_chat_template` 渲染文本（[README.md:L238](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L238)），把 `{"prompt": text, "multi_modal_data": {"image": image}}` 交给 `llm.generate`（[README.md:L239](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L239)）。现有两个后端与 CLI 主流程无需改动——这正是后端抽象的价值：新增形态 = 新增实现，而不是修改既有代码。
</details>

**练习 2**：为什么把 `model.generate` 的调用放在 `ask()` 里、而把 `from_pretrained` 放在 `__init__` 里？

<details>
<summary>参考答案</summary>

`from_pretrained` 要加载约 32GB 的 bf16 权重（u3-l4 的显存账本），耗时以分钟计，只应发生一次；`generate` 是每次问答都要执行的轻入口。把重资源初始化放进构造器，`ask()` 的多次调用就能复用已加载的模型。这也让耗时统计（4.3 节）更公平：`ask()` 内计时不含加载开销。
</details>

### 4.2 图像输入封装

#### 4.2.1 概念说明

图像是双后端差异最大的一环。工程上把它封装成两个纯函数：

- **`load_image(path)`**：统一的 PIL 入口，`Image.open(path).convert("RGB")`，供本地后端使用。
- **`encode_image_base64(path)`**：把图片在内存中编码为 JPEG、base64 化，拼成 data URL，供服务后端使用。

为什么服务端要 `convert("RGB")`？PIL 打开 PNG 得到的可能是 RGBA（带透明通道）或调色板模式，而 JPEG 只支持三通道——不转换会在 `save(format="JPEG")` 时直接报错。官方示例同样在编码前做了转换（见下文 L280），这不是可选的风格问题，而是必要步骤。

base64 把每 3 字节编成 4 个 ASCII 字符，因此传输体积会膨胀为原始字节的

\[
\frac{4}{3} \approx 1.33 \text{ 倍}
\]

这对 HTTP 请求体是个必须计入的成本：一张 2MB 的 JPEG 走 base64 通道就变成约 2.7MB 的请求体。

#### 4.2.2 核心流程

```text
load_image(path)                     encode_image_base64(path)
  Image.open(path)                     load_image(path)          ← 复用统一入口
  .convert("RGB")                      image.save(buffer, "JPEG") ← 内存编码，不落盘
  → PIL.Image                          base64.b64encode(...)
  → 本地后端像素通道                     .decode("utf-8")
                                       f"data:image/jpeg;base64,{...}"
                                       → messages 里的 image_url 分片
```

两个函数有依赖关系：`encode_image_base64` 内部调用 `load_image`，保证「转 RGB」这条规则只写一处。

#### 4.2.3 源码精读

[README.md:L279-L285](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L279-L285)——官方的图像 base64 编码五步：L280 打开并转 RGB；L282 建内存缓冲区；L283 存为 JPEG；L284 `b64encode` 后解码为字符串；L285 拼 data URL 前缀。我们的 `encode_image_base64` 就是这五行的事先约定好的函数化。

[README.md:L287-L289](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L287-L289)——服务链路的占位分片：`{"type": "image_url", "image_url": {"url": base64_image_url}}`。与本地链路的 `{"type": "image", "image": path}`（[README.md:L142](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L142)）对照：**服务链路没有独立的像素通道，base64 字符串本身就放在占位分片里**，服务端代劳解码（u3-l3 的结论）。

[README.md:L139-L140](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L139-L140) 与 [README.md:L181-L182](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L181-L182)——本地链路的像素通道：单图 `Image.open(image_path)`、多图列表推导式。注意官方在 Transformers 路径**没有**转 RGB，PIL 对象原样传给 processor（MoonViT 原生分辨率预处理在 processor 内部完成）；转 RGB 只在 JPEG 编码前是硬要求。因此我们的 `load_image` 统一转 RGB 对两条链路都安全。

#### 4.2.4 代码实践：量化 base64 膨胀与 token 估算

1. **实践目标**：用真实数据验证 base64 的 \(4/3\) 膨胀，并建立「图片体积 → 视觉 token」的直觉。
2. **操作步骤**（示例代码，仅需 pillow，无需模型与 GPU）：

```python
# 示例代码：measure_image.py —— 量化图像两条通道的成本
import base64
from io import BytesIO
from PIL import Image

path = "figures/demo.png"
raw_png = open(path, "rb").read()                      # 原始文件字节
image = Image.open(path).convert("RGB")

buf = BytesIO()
image.save(buf, format="JPEG")                          # 内存 JPEG 编码
jpeg_bytes = buf.getvalue()
b64_len = len(base64.b64encode(jpeg_bytes).decode())    # base64 字符数

w, h = image.size
print(f"尺寸: {w}x{h} = {w*h} 像素")
print(f"PNG 原始: {len(raw_png)/1024:.0f} KB")
print(f"JPEG 编码后: {len(jpeg_bytes)/1024:.0f} KB")
print(f"base64 字符数: {b64_len/1024:.0f} KB, 膨胀比: {b64_len/len(jpeg_bytes):.3f}")
print(f"估算视觉 token: ~{w*h // 784}")                 # u3-l1: 像素÷784

# 再把图片缩小到 50%，观察体积与 token 估算如何变化
image.resize((w//2, h//2)).save("half.jpg")             # 示例输出文件，可随手删除
```

3. **需要观察的现象**：膨胀比是否落在 1.333 附近；图片线性缩小一半（面积 1/4）后，token 估算是否也约降为 1/4。
4. **预期结果**：膨胀比 ≈ 1.33；半尺寸图的估算 token 约为原图的 1/4。demo.png 的具体 KB 数与 token 数**待本地验证**（与图片实际内容有关）。
5. 此实践不调用模型，任何装了 pillow 的环境（含 u1-l2 的 kimi-vl 环境）都可运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接把 PNG 原始字节 base64 编码，而要先转 JPEG？

<details>
<summary>参考答案</summary>

两个原因：一是兼容性，PNG 常见 RGBA 模式，而官方链路 `convert("RGB")` + `save(format="JPEG")`（L280、L283）明确选择了三通道有损格式，跟随官方口径最稳；二是体积，截图/照片类内容 JPEG 通常显著小于 PNG，而 base64 还要再乘 \(4/3\)，输入越大请求体越大、服务端解码与视觉 token 越多，成本层层放大。
</details>

**练习 2**：用户传入 3 张图，服务后端的请求体大约是什么规模？如何提前估算？

<details>
<summary>参考答案</summary>

请求体 ≈ 3 × 单图 JPEG 字节数 × \(4/3\)（再加分片 JSON 的少量开销）。先用 4.2.4 的脚本算出单图 base64 字符数，乘以图片数即可。同时用「像素 ÷ 784 × 张数」估算视觉 token，对照服务端 `--max-model-len`（[README.md:L260](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L260)）确认不会超限。
</details>

### 4.3 两种部署形态对比

#### 4.3.1 概念说明

同一个模型、同一个问题，有两条投递路径，它们不是「谁更好」，而是**各自命中不同的使用场景**：

- **本地 Transformers（进程内）**：模型作为 Python 对象活在你的进程里。启动即加载权重（约 32GB bf16），之后每次 `generate` 直接调 GPU；没有网络、没有序列化开销；但一个进程同时基本只能服务一个请求序列，批处理要自己写。
- **vLLM 服务（常驻 HTTP）**：`vllm serve` 独立进程加载一次权重，之后常驻；客户端经 HTTP 调用，服务端用连续批处理（continuous batching）把并发请求拼批，吞吐高；还免费带来 usage 统计、部署名解耦等工程能力。

选型三问：

1. **并发**：只有自己用（调试、实验、单机脚本）→ 本地；有多个消费者 → 服务。
2. **生命周期**：跑一次就退 → 本地（免部署）；全天候被调用 → 服务。
3. **计量**：需要按 token 计费/核算 → 服务（usage 是官方口径）；本地要自己数张量长度。

#### 4.3.2 核心流程

两条路径一次问答的时序对比：

```text
本地 Transformers（每次进程启动）:
  python 启动 → from_pretrained(加载权重, 分钟级) → [ask: 编码 → generate → 裁剪解码] → 退出
                              ↑ 冷启动，每次进程重启都要付

vLLM 服务（服务与客户端分离）:
  vllm serve(加载权重, 一次) → 常驻: 8000 端口监听
  客户端(随时): build base64 → HTTP POST /v1/chat/completions → 拿文本 + usage
                              ↑ 客户端零加载成本，服务端多请求拼批
```

注意计时的公平性：本地后端若把「加载权重」算进耗时，对服务后端（加载发生在启动阶段）极不公平。我们的工具把加载放在构造器、只在 `ask()` 内计时，两边口径一致。

#### 4.3.3 源码精读

[README.md:L255-L264](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L255-L264)——官方 serve 命令。L260 启动 Thinking-2506：`--served-model-name kimi-vl-thinking-2506` 把部署名与仓库长名解耦，`--max-model-len 32768` 限定单请求总 token，`--limit-mm-per-prompt image=64` 限定单请求图片数（三道闸门详见 u3-l3）；L256-L257 的注释给出了长上下文（131072）与多图（256/512）的调优方向。

[README.md:L291-L294](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L291-L294)——服务端调用：`client.chat.completions.create(model="kimi-vl-thinking-2506", messages=messages)`。`model` 填的正是 serve 命令里 `--served-model-name` 定义的部署名——这是两端对接的暗号，改名要两端同步。

[README.md:L296](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L296)——`print(completion.choices[0].message)` 打印的是完整响应对象，其中除 `content` 外还携带 `usage`（`prompt_tokens` / `completion_tokens`，OpenAI 兼容接口的标准字段）。对比本地链路：`generate` 只返回 token id 序列（[README.md:L193](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L193)），token 数要自己用张量长度相减来算。**输出形态的差异本身就是两种部署形态的差异**：服务返回结构化对象，本地返回裸张量。

[README.md:L65-L68](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L65-L68)——官方温度推荐（Thinking 0.8 / Instruct 0.2）。本讲工具默认走 Thinking-2506，故两个后端统一 `temperature=0.8`。

#### 4.3.4 代码实践：双后端同题计时对比

1. **实践目标**：亲测两种形态在「同一问题」上的延迟与 token 计量差异。
2. **操作步骤**：
   - 按 [README.md:L260](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L260) 启动服务：`vllm serve moonshotai/Kimi-VL-A3B-Thinking-2506 --served-model-name kimi-vl-thinking-2506 --trust-remote-code --tensor-parallel-size 1 --max-num-batched-tokens 32768 --max-model-len 32768 --limit-mm-per-prompt image=64`；
   - 用第 5 节的工具分别执行：`--backend local` 与 `--backend service`，同一张 `figures/demo.png`、同一个推理型问题（如「图中的穹顶建筑是什么？请一步步推理」）；
   - 记录两者的 `elapsed_seconds` 与 `usage`。
3. **需要观察的现象**：两边 `answer` 是否基本一致（温度 0.8 有随机性，允许措辞差异）；`elapsed_seconds` 是否同量级；`usage` 口径是否一致。
4. **预期结果**：答案语义一致；本地首问耗时 ≈ 服务首问耗时（都含预热），后续请求服务端明显更快且稳定；`prompt_tokens` 两边接近（视觉 token + 文本 token），服务端为官方计数。具体数值与硬件强相关，**待本地验证**。
5. 额外观察点：本地进程重启要重新加载权重，而服务端只要不关就一直热着——把同一问题连续问 3 次对比方差。

#### 4.3.5 小练习与答案

**练习 1**：同事反馈「你的问答服务对 1792×1792 的大图经常报长度超限」，先查哪里？

<details>
<summary>参考答案</summary>

先查两道闸门：单张 1792×1792 图的视觉 token ≈ 1792×1792÷784 ≈ 4096（u3-l1 的上限口径），多张图则成倍增加；若 `--max-model-len` 仍是 32768（[README.md:L260](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L260)），输入加 32768 的生成预算必然超限。对策按官方注释（L256-L257）：调大 `--max-model-len` 到 131072，或减小请求的 `max_tokens` / 图片数。
</details>

**练习 2**：为什么本地后端的 `usage` 要用「张量长度相减」来算，而服务后端直接读字段？

<details>
<summary>参考答案</summary>

`model.generate` 返回「输入 + 新生成」的完整 token id 序列（u2-l1），没有附带计数对象，所以 `completion_tokens = generated_ids.shape[-1] - input_ids.shape[-1]` 是唯一口径；而 OpenAI 兼容接口的响应自带 `usage.prompt_tokens / completion_tokens`（服务端统计），直接读取即可，也避免了客户端复算口径不一致的问题。
</details>

## 5. 综合实践：双后端多模态问答 CLI 工具

现在把三个模块合体：写一个 `kimi_vl_qa.py`，输入图片路径与问题，输出含 `thinking`、`answer`、`usage`、耗时与后端信息的 JSON。它复用 README 的全部官方写法，只做「组装与封装」。

**完整代码（示例代码，保存为 `kimi_vl_qa.py`）**：

```python
"""示例代码：基于 Kimi-VL 的双后端多模态问答 CLI（本讲综合实践）

用法：
  python kimi_vl_qa.py --backend local   --image figures/demo.png --question "图中的穹顶建筑是什么？"
  python kimi_vl_qa.py --backend service --image figures/demo.png --question "图中的穹顶建筑是什么？"
"""
import argparse
import base64
import json
import time
from io import BytesIO

from PIL import Image

MODEL_REPO = "moonshotai/Kimi-VL-A3B-Thinking-2506"   # 官方仓库名（本地后端用）
DEPLOY_NAME = "kimi-vl-thinking-2506"                  # serve 的 --served-model-name（服务后端用）
# 思维链/答案分界标记候选：确切标记请用 skip_special_tokens=False 解码一次确认（承接 u2-l2）
THINK_SEPARATORS = ["</think>", "<|/think|>", "<|/think>"]


# ---------- 4.2 图像输入封装 ----------
def load_image(path):
    """统一 PIL 入口：转 RGB，避免 JPEG 编码时 RGBA 报错。"""
    return Image.open(path).convert("RGB")


def encode_image_base64(path):
    """官方 L279-L285 五行的函数化：内存 JPEG → base64 → data URL。"""
    buffered = BytesIO()
    load_image(path).save(buffered, format="JPEG")
    b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


# ---------- 4.1 消息组装（占位通道） ----------
def build_messages_local(image_paths, question):
    """本地链路：type=image 占位，像素走 processor 的 images= 通道（L183-L190）。"""
    content = [{"type": "image", "image": p} for p in image_paths]
    content.append({"type": "text", "text": question})
    return [{"role": "user", "content": content}]


def build_messages_service(image_paths, question):
    """服务链路：type=image_url 分片自带 base64 像素（L287-L289）。"""
    content = [{"type": "image_url", "image_url": {"url": encode_image_base64(p)}}
               for p in image_paths]
    content.append({"type": "text", "text": question})
    return [{"role": "user", "content": content}]


# ---------- 输出解析 ----------
def split_thinking(raw):
    """把 Thinking 原始输出切为 (thinking, answer)。"""
    for sep in THINK_SEPARATORS:
        if sep in raw:
            head, _, tail = raw.partition(sep)
            return head.strip(), tail.strip()
    return "", raw.strip()          # 未找到分界标记：视为整段都是答案


# ---------- 4.1 后端抽象 ----------
class TransformersBackend:
    """本地进程内推理，蓝本：README L158-L201。"""

    def __init__(self):
        from transformers import AutoModelForCausalLM, AutoProcessor  # 延迟导入，加快 --help
        # 已装 flash-attn 时可换 torch_dtype=torch.bfloat16 + attn_implementation="flash_attention_2"
        self.model = AutoModelForCausalLM.from_pretrained(
            MODEL_REPO, torch_dtype="auto", device_map="auto", trust_remote_code=True)
        self.processor = AutoProcessor.from_pretrained(MODEL_REPO, trust_remote_code=True)

    def ask(self, image_paths, question):
        images = [load_image(p) for p in image_paths]          # 像素通道（L182）
        text = self.processor.apply_chat_template(             # 渲染（L191）
            build_messages_local(image_paths, question),
            add_generation_prompt=True, return_tensors="pt")
        inputs = self.processor(images=images, text=text,      # 编码（L192）
                                return_tensors="pt", padding=True, truncation=True).to(self.model.device)
        t0 = time.perf_counter()
        generated = self.model.generate(**inputs, max_new_tokens=32768,
                                        do_sample=True, temperature=0.8)   # L193 + 采样开关
        elapsed = time.perf_counter() - t0
        trimmed = [out[len(inp):] for inp, out in zip(inputs.input_ids, generated)]  # L194-L196
        raw = self.processor.batch_decode(
            trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]  # L197-L199
        thinking, answer = split_thinking(raw)
        n_in = int(inputs.input_ids.shape[-1])
        n_out = int(generated.shape[-1]) - n_in                # 本地口径：张量长度相减
        return {"thinking": thinking, "answer": answer, "elapsed_seconds": elapsed,
                "usage": {"prompt_tokens": n_in, "completion_tokens": n_out}}


class VLLMServiceBackend:
    """vLLM OpenAI 兼容服务客户端，蓝本：README L268-L297。"""

    def __init__(self, base_url, deploy_name):
        from openai import OpenAI
        self.client = OpenAI(base_url=base_url, api_key="token-abc123")   # L274-L277
        self.deploy_name = deploy_name

    def ask(self, image_paths, question):
        messages = build_messages_service(image_paths, question)
        t0 = time.perf_counter()
        completion = self.client.chat.completions.create(     # L291-L294
            model=self.deploy_name, messages=messages,
            temperature=0.8, max_tokens=32768)
        elapsed = time.perf_counter() - t0
        thinking, answer = split_thinking(completion.choices[0].message.content)
        u = completion.usage                                   # 官方计量口径
        return {"thinking": thinking, "answer": answer, "elapsed_seconds": elapsed,
                "usage": {"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens}}


def main():
    ap = argparse.ArgumentParser(description="Kimi-VL 双后端多模态问答")
    ap.add_argument("--backend", choices=["local", "service"], default="local")
    ap.add_argument("--image", nargs="+", required=True, help="图片路径，可多张")
    ap.add_argument("--question", required=True)
    ap.add_argument("--base-url", default="http://localhost:8000/v1")
    ap.add_argument("--deploy-name", default=DEPLOY_NAME)
    args = ap.parse_args()

    if args.backend == "local":
        backend, model = TransformersBackend(), MODEL_REPO
    else:
        backend, model = VLLMServiceBackend(args.base_url, args.deploy_name), args.deploy_name

    result = backend.ask(args.image, args.question)
    result.update(backend=args.backend, model=model, images=args.image, question=args.question)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
```

**运行与验证步骤**：

1. 环境准备：u1-l2 的 `kimi-vl` conda 环境为基础；服务后端另需 `pip install vllm openai`（vllm 不在 requirements.txt 中，u3-l2 已说明）。
2. 先跑感知题（快速冒烟）：

   ```bash
   python kimi_vl_qa.py --backend service --image figures/demo.png \
     --question "What is the dome building in the picture? Think step by step."
   ```

3. 再跑推理题（本讲的正式验证，与官方示例同图不同问法）：

   ```bash
   Q="这张图里最能证明建筑年代与风格的特征是什么？请一步步推理后给结论。"
   python kimi_vl_qa.py --backend local   --image figures/demo.png --question "$Q"
   python kimi_vl_qa.py --backend service --image figures/demo.png --question "$Q"
   ```

4. 把两次运行输出的 `elapsed_seconds`、`usage`、`answer` 摘录进对比表：

   | 指标 | local | service |
   | :--- | :--- | :--- |
   | elapsed_seconds（首问 / 后续） | 待本地验证 | 待本地验证 |
   | prompt_tokens | 待本地验证 | 待本地验证 |
   | completion_tokens（思维链长度） | 待本地验证 | 待本地验证 |
   | answer 语义一致性 | — | 与 local 对比 |

5. 多图回归验证（可选）：`--image figures/demo1.png figures/demo2.png --question "请一步步推断这份手稿属于谁、记录了什么"`，复现官方多图场景（[README.md:L181-L188](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L181-L188)）。

**预期观察**：

- 两个后端的 `thinking` 非空（推理题会产出长思维链），`answer` 语义一致但措辞不同（温度 0.8 采样所致）；
- `completion_tokens` 达到数千量级——这正是 u2-l4 讲过的 512 vs 32768 生成预算差异的直观体现；
- 服务后端把同一问题连问 3 次，耗时方差明显小于本地单问（常驻 + 拼批）；
- 若 `THINK_SEPARATORS` 三个候选都未命中（`thinking` 恒为空），按 u2-l2 的方法用 `skip_special_tokens=False` 解码一次原始输出，把真实分界标记加进列表——该标记的确切写法**待本地验证**。

**工程注意**：本地后端首次运行会从 HuggingFace 下载约 32GB 权重并执行远程代码（`trust_remote_code=True`，u1-l1）；服务后端的 `max_tokens=32768` 受 serve 时 `--max-model-len 32768` 约束，输入视觉 token 较大时需按 4.3.5 练习 1 的思路调整。

## 6. 本讲小结

- 应用链路设计：把官方脚本切为「输入解析 → 图像入口 → 消息组装 → 推理后端 → 输出解析」五段，用 `ask(image_paths, question)` 后端抽象隔离本地与服务两条实现，上层 CLI 与后端解耦。
- 图像输入封装：`load_image`（统一 PIL 入口、强制 RGB）与 `encode_image_base64`（JPEG → base64 → data URL）覆盖两类后端的像素投递方式；base64 通道有 \(4/3\) 体积膨胀，视觉 token 可用「像素 ÷ 784」提前估算。
- 两种部署形态对比：本地 Transformers 无网络开销但冷启动分钟级、单序列、token 数要自己算；vLLM 服务常驻高吞吐、自带 usage 官方计量与部署名解耦，选型看并发、生命周期与计量三问。
- 输出解析是双后端共同的汇合点：按输入长度裁剪 → `skip_special_tokens` 解码 → `split_thinking` 切分思维链与答案，分界标记以本地解码实测为准。
- 计时公平性：把模型加载放构造器、只在 `ask()` 内用 `time.perf_counter()` 计时，两种形态的延迟才可比。

## 7. 下一步学习建议

至此 14 讲全部完成，你已具备「读懂发布仓库 → 跑通推理 → 部署服务 → 构建应用」的完整能力。三个继续深入的方向：

1. **给工具加上流式输出**：研究 openai SDK 的 `stream=True`，让长思维链边生成边展示（Thinking 模型动辄数千 token，流式对体验提升最大）；对比本地 `generate` 的 `Streamer` 参数。
2. **接入 vLLM 离线后端**：按 4.1.5 练习 1 实现 `VLLMOfflineBackend`，把三种后端放进同一工具做吞吐横评（结合 u3-l2 的批量能力，对 figures 下全部图片做一次批量问答）。
3. **走到权重与实现层**：带着 u4-l1 的报告结论，去 HuggingFace 模型仓库精读 `trust_remote_code` 实际加载的实现文件（u1-l1 记录过的那份清单），验证 MoonViT 打包、MLP 投影与 MoE 路由在代码中的真实形态——这是从「使用者」迈向「二次开发者」的最后一步。

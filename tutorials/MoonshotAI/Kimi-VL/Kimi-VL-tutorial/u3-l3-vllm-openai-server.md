# vLLM OpenAI 兼容服务：启动参数与 API 调用全流程

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `vllm serve` 命令把 Kimi-VL 启动为一个 OpenAI 兼容的 HTTP 服务，并逐个说清 `--served-model-name`、`--trust-remote-code`、`--tensor-parallel-size`、`--max-num-batched-tokens`、`--max-model-len`、`--limit-mm-per-prompt` 这六个关键参数的作用。
2. 用 `openai` Python SDK 向该服务发起多模态请求：把本地图片编码为 base64 data URL，组装 `messages`，调用 `chat.completions.create` 拿到回答。
3. 根据业务需求（长文档、多图、高并发）调整 `--max-model-len` 与 `--limit-mm-per-prompt`，并理解调整背后「显存换容量」的权衡逻辑。

本讲是第 3 单元「架构解析与生产部署」的第三讲，承接 [u3-l2] 的 vLLM 离线推理：上一讲模型住在你的 Python 进程里，本讲把它搬进一个独立的长驻服务进程，供任意语言的客户端通过网络调用——这是从「跑通 demo」到「对外提供服务」的关键一步。

## 2. 前置知识

本讲假设你已完成 u3-l2（vLLM 离线推理），并补充四个新概念：

**服务化部署 vs 离线推理。**
u3-l2 的写法是 `llm = LLM(...)` 之后在同一个 Python 进程里直接调 `llm.generate(...)`——模型和你的业务代码住在一个进程里，脚本结束模型就消失。服务化部署则是让 vLLM 作为一个独立进程常驻内存，对外暴露 HTTP 接口；业务代码（哪怕运行在另一台机器上）通过发 HTTP 请求来使用模型。服务进程只需加载一次权重，之后所有请求共享这份权重。

**OpenAI 兼容 API。**
OpenAI 的 Chat Completions 接口（`POST /v1/chat/completions`）事实上是行业通用协议：请求体里有 `model`、`messages`，响应里有 `choices[0].message.content`。vLLM 内置实现了这套协议的 HTTP 服务，所以任何会调 OpenAI API 的代码，只要改一下 `base_url` 指向本地 vLLM 服务，就能无缝切换到 Kimi-VL——这就是「OpenAI 兼容」的含义。请求路径里的 `/v1` 不是版本号装饰，而是协议约定的路径前缀。

**base64 编码与 data URL。**
HTTP 请求体（JSON）是纯文本，而图片是二进制数据。要把二进制塞进文本协议，标准做法是 base64 编码：把每 3 个字节映射成 4 个可打印字符，体积膨胀约 \( 4/3 \approx 1.33 \) 倍。编码后的字符串再拼上前缀 `data:image/jpeg;base64,` 就构成一个 **data URL**——一个「自带数据 的伪网址」，浏览器和各大模型 API 都认这种格式。openai SDK 的多模态消息正是通过 `image_url` 字段接收它。

**openai SDK 是通用客户端。**
[requirements.txt:L8](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/requirements.txt#L8) 里列出的 `openai` 依赖，就是官方 Python SDK。它默认连 OpenAI 官方服务，但构造 `OpenAI(base_url=..., api_key=...)` 时可以指向任何兼容服务——本讲就指向本地 vLLM。

另外回顾两个旧知识点（u2-l4、u3-l1 已讲）：Kimi-VL 的上下文窗口是 128K = 131,072 个 token，是「视觉 token + 文本 + 生成输出」共享的总预算；MoonViT 原生分辨率编码使图片像素数与视觉 token 数近似成正比。这两点是理解本讲调优参数的钥匙。

## 3. 本讲源码地图

本仓库是发布型仓库（回顾 u1-l3），不含模型实现代码，本讲的「源码」是 README 中的官方部署示例与依赖清单：

| 文件 | 本讲用到的部分 | 作用 |
| --- | --- | --- |
| [README.md](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md) | 第 8 节 Deployment（L209–L297） | `vllm serve` 启动命令（L255–L264）与 openai SDK 调用代码（L268–L297），本讲全部内容的出处 |
| [requirements.txt](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/requirements.txt) | L8 的 `openai` | 说明调用服务的 SDK 已在官方依赖清单中，但 `vllm` 本身不在其中，需单独安装 |
| [README.md:L48](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L48) | News 一行 | vLLM 官方于 2025.04.15 合入 Kimi-VL 支持（vllm-project/vllm#16387），说明需要较新版本的 vLLM |
| figures/demo.png | 实践素材 | 官方测试图片，与 README 示例代码共用 |

> 注意：`vllm` 不在 requirements.txt 中（u1-l2、u3-l2 均已确认），实践前需按 vLLM 官方文档单独安装；README L213 说明支持来自 vLLM 主分支，具体可用版本以 vLLM 文档为准（待确认）。

## 4. 核心概念与源码讲解

### 4.1 vllm serve 启动参数

#### 4.1.1 概念说明

`vllm serve` 是 vLLM 提供的命令行入口：它把「加载模型权重 → 初始化推理引擎 → 启动 HTTP 服务」三件事打包成一条命令。命令跑起来后，服务默认监听 `8000` 端口（README 调用代码中的 `http://localhost:8000/v1` 印证了这一点），你就可以用任何 HTTP 客户端调用了。

与 u3-l2 的离线推理对比，最大的结构差异是：离线模式下 `LLM(...)` 只是个 Python 对象；服务模式下同一个引擎被包进常驻进程，多了「服务名」「端口」「并发调度」这些服务化概念。参数虽多，但每个都在回答一个具体问题：

| 参数 | 回答的问题 | 取值与含义 |
| --- | --- | --- |
| `moonshotai/Kimi-VL-A3B-Instruct`（位置参数） | 加载哪个模型 | HuggingFace 仓库名，权重与实现代码从这里下载 |
| `--served-model-name kimi-vl` | 客户端请求里 `model` 字段填什么 | 给服务起个别名，让 API 侧的模型名与 HF 仓库名解耦 |
| `--trust-remote-code` | 允许执行模型仓库里的自定义代码吗 | Kimi-VL 的实现代码托管在 HF 仓库（u1-l1），必须开启，作用等同于 Transformers 侧的 `trust_remote_code=True` |
| `--tensor-parallel-size 1` | 用几张 GPU 切分模型张量 | `1` 表示单卡；16B 权重按 u1-l2 的估算（bfloat16 约 32GB）需单卡显存充裕，多卡时可调大 |
| `--max-num-batched-tokens 32768` | 引擎每一步最多打包处理多少 token | 吞吐与显存的平衡旋钮，影响预填充（prefill）阶段的分块批量大小 |
| `--max-model-len 32768` | 单个请求最多多少 token | 上下文预算上限，小于模型 capability 的 128K，详见 4.3 |
| `--limit-mm-per-prompt image=64` | 单个请求最多带几张图 | 多模态输入的防滥用护栏，详见 4.3 |

其中 `--served-model-name` 特别值得一提：它实现的是「部署名 ≠ 仓库名」的解耦。客户端请求里的 `model="kimi-vl"` 是你自起的名字，将来换模型、换版本，客户端代码一行都不用改——只改服务端这一行。

#### 4.1.2 核心流程

`vllm serve` 一条命令背后经过三个阶段：

```text
阶段一：解析命令行参数
  vllm serve <model_path> [flags]
        │
阶段二：初始化引擎（耗时主要在这里）
  ├─ 从 HuggingFace 下载/读取权重（首次需 --trust-remote-code 拉取自定义代码）
  ├─ 按 --tensor-parallel-size 划分 GPU、加载权重
  ├─ 按 --max-model-len 确定单请求序列长度上限
  └─ 用剩余显存预分配 KV cache（连续分页，vLLM 高吞吐的根基）
        │
阶段三：启动 HTTP 服务
  ├─ 监听端口（默认 8000）
  ├─ 暴露 OpenAI 兼容路由：/v1/chat/completions、/v1/models 等
  └─ 等待请求 → 进入引擎调度 → 流式/非流式返回
```

要观察的阶段三是启动日志：服务就绪前会打印引擎配置（模型路径、最大长度、KV cache 用量等），日志里看到类似 `Uvicorn running on http://0.0.0.0:8000` 的行即可开始调用（具体日志文案随 vLLM 版本可能不同，待本地验证）。

#### 4.1.3 源码精读

服务启动命令出自 README Deployment 一节。先看整体入口：

[README.md:L209-L215](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L209-L215) — Deployment 一节的标题与引言：说明 vLLM 主分支已支持 Kimi-VL 部署，下面分「Offline Inference」（u3-l2 已讲）与「OpenAI-Compatible Server」（本讲）两条路线。

具体的 serve 命令（含两行注释、两个变体各一条）：

[README.md:L255-L264](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L255-L264) — 官方给出的两条完整启动命令。以 Thinking-2506 那条（L260）为例逐段拆解：

```bash
vllm serve moonshotai/Kimi-VL-A3B-Thinking-2506 \
  --served-model-name kimi-vl-thinking-2506 \
  --trust-remote-code \
  --tensor-parallel-size 1 \
  --max-num-batched-tokens 32768 \
  --max-model-len 32768 \
  --limit-mm-per-prompt image=64
```

- 位置参数 `moonshotai/Kimi-VL-A3B-Thinking-2506`：模型来源，HuggingFace 仓库名；
- `--served-model-name kimi-vl-thinking-2506`：服务别名，客户端 `model` 字段必须填这个名字（见 4.2 的调用代码 L292）；
- `--trust-remote-code`：放行 HF 仓库里的自定义模型代码，没有它加载会直接报错；
- `--tensor-parallel-size 1`：单卡张量并行；多卡时改此值，权重矩阵按张量维度切到多卡；
- `--max-num-batched-tokens 32768`：引擎单步批处理的 token 预算；
- `--max-model-len 32768`：单请求 token 上限，注意只有模型 capability（128K）的四分之一；
- `--limit-mm-per-prompt image=64`：每个请求最多 64 张图。

[README.md:L262-L263](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L262-L263) — Instruct 变体的命令结构完全相同，只有两处不同：模型仓库换成 `Kimi-VL-A3B-Instruct`，别名换成 `kimi-vl`。这正是 `--served-model-name` 解耦价值的体现——客户端只需把 `model` 字段从 `"kimi-vl-thinking-2506"` 改成 `"kimi-vl"` 即可切换模型。

再看两行注释，它们是官方给出的调优线索（本讲 4.3 的主线）：

[README.md:L256-L257](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L256-L257) — 官方注释：需要更长上下文时，把 `--max-model-len` 和 `--max-num-batched-tokens` 提到 131072（即 128K）；需要更多输入图片时，把 `--limit-mm-per-prompt` 提到 `image=256` 或 `512`。这两行注释说明默认命令里的 32768/64 是「够用且省显存」的保守值，不是能力上限。

最后是版本线索：

[README.md:L48](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L48) — News 记录：2025.04.15 vLLM 通过 PR #16387 合入 Kimi-VL 支持。这提示你需要使用该时间点之后的 vLLM 版本。

#### 4.1.4 代码实践

**实践目标**：亲手启动 Kimi-VL 服务，认全每个参数，并验证服务真的在监听。

**操作步骤**：

1. 单独安装 vLLM（它不在 requirements.txt 中），建议参考 [vLLM 官方安装文档](https://docs.vllm.ai/en/latest/getting_started/installation.html)；
2. 在仓库根目录执行 README 官方命令（Instruct 变体）：

   ```bash
   vllm serve moonshotai/Kimi-VL-A3B-Instruct --served-model-name kimi-vl --trust-remote-code --tensor-parallel-size 1 --max-num-batched-tokens 32768 --max-model-len 32768 --limit-mm-per-prompt image=64
   ```

3. 观察启动日志，重点找三类信息：模型加载进度、`max_model_len` 等引擎配置的回显、端口监听就绪的提示；
4. 另开一个终端，用 curl 查看服务注册了哪些模型：

   ```bash
   curl http://localhost:8000/v1/models
   ```

**需要观察的现象**：启动日志中回显的 `max_model_len=32768` 与命令行传入值一致；`/v1/models` 返回的 JSON 中出现 `kimi-vl` 这个别名（而不是 HF 仓库名）。

**预期结果**：服务常驻运行，`curl` 能拿到包含模型列表的 JSON 响应。（本讲义在无 GPU 环境下编写，以上现象待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：如果把 `--served-model-name kimi-vl` 这个参数删掉，客户端代码需要怎么改？
**答案**：vLLM 会默认用模型的真实路径（即 `moonshotai/Kimi-VL-A3B-Instruct`）作为服务名，客户端 `chat.completions.create` 的 `model` 字段必须填这个完整仓库名。可见 `--served-model-name` 不影响功能，只影响 API 侧的命名与后续换模型的解耦便利性。

**练习 2**：`--tensor-parallel-size 1` 改成 `2` 需要什么硬件条件？它解决的是算力问题还是显存问题？
**答案**：需要同一机器上至少 2 张 GPU（张量并行是卡间切分权重矩阵，依赖高速卡间互联）。它两者都缓解：16B 权重被切到两张卡，单卡显存压力减半，同时矩阵乘法可并行执行；但对小模型而言通常显存收益是主要动机。

**练习 3**：为什么 README 两条命令除了模型名和别名，其余参数一字不差？
**答案**：因为三个变体规格完全相同（u1-l1 讲过的 16B 总参 / 128K 上下文），部署层（显存、上下文、图片数）的约束只取决于模型规模而不取决于后训练差异，所以部署命令可以模板化，仅需替换模型与别名。

### 4.2 base64 图像编码调用

#### 4.2.1 概念说明

服务跑起来后，客户端要解决的问题是：**怎么把一张本地图片塞进一个 JSON 请求里？**

对比三条路线就明白 base64 的位置了：

| 场景 | 图像的传递方式 | 出处 |
| --- | --- | --- |
| Transformers 离线推理（u2-l1） | PIL 对象直接走进程内参数 `processor(images=image, ...)` | README L145 |
| vLLM 离线推理（u3-l2） | PIL 对象放进请求字典 `multi_modal_data: {"image": image}` | README L239 |
| OpenAI 兼容服务（本讲） | 图片编码为 base64 data URL，作为**文本字符串**放进 JSON 消息 | README L282-L289 |

前两条路线图像始终留在 Python 进程内，以对象形式传递；服务化后，客户端与服务之间只剩 HTTP 一条文本通道，图像必须被编码成文本。README 的做法分四步：PIL 打开图片并 `convert("RGB")` → 存进内存缓冲区（JPEG 格式）→ `base64.b64encode` 编码 → 拼 data URL 前缀。

两个容易忽略的细节：

- **`convert("RGB")` 不是多余的**：PNG 图片常带 alpha 透明通道（RGBA 四通道），而 JPEG 只支持三通道；统一转 RGB 再存 JPEG，既压缩了体积又避免了格式报错。
- **`api_key="token-abc123"` 是占位符**：本地 vLLM 服务默认不校验密钥，但 openai SDK 要求 `api_key` 非空，否则构造客户端就抛异常。这个值随便填，只是让 SDK「闭嘴」。

#### 4.2.2 核心流程

调用链共六步，前三步准备图片，后三步发起请求：

```text
① Image.open(path).convert("RGB")     打开图片，丢弃 alpha 通道
② image.save(BytesIO(), "JPEG")        编码为 JPEG 字节流，存内存缓冲区
③ b64encode(...).decode("utf-8")       字节流 → base64 字符串
④ 拼 f"data:image/jpeg;base64,{...}"    构造 data URL
⑤ messages 里放 image_url 分片 + text 分片   组装多模态消息
⑥ client.chat.completions.create(...)   POST /v1/chat/completions，取回答案
```

注意消息结构与 u2-l1 的微妙差异：Transformers 写法里图片分片是 `{"type": "image", "image": path}`（仅占位）；OpenAI 协议里是 `{"type": "image_url", "image_url": {"url": ...}}`——这里不再是占位符，而是**真正承载数据**的字段。占位符与像素对齐的工作（u2-l3 讲过的那套机制）全部移交给了服务端。

体积账也值得算一下：base64 编码使数据膨胀 \( \frac{4}{3} \) 倍，一张 1MB 的 JPEG 编码后约 1.33MB 的请求体。图片很多、很大时，请求体本身会成为网络负担——这也是 4.3 中限制每请求图片数的现实原因之一。

#### 4.2.3 源码精读

调用代码出自 README L268–L297，逐段拆解：

[README.md:L269-L272](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L269-L272) — 四个导入：`base64`（编码）、`PIL.Image`（读图）、`io.BytesIO`（内存缓冲区）、`openai.OpenAI`（HTTP 客户端）。`openai` 正是 [requirements.txt:L8](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/requirements.txt#L8) 声明的依赖，说明官方预期这条调用路线开箱即用。

[README.md:L274-L277](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L274-L277) — 构造客户端：`base_url` 指向本地 vLLM 服务的 `/v1` 路由前缀（印证默认端口 8000）；`api_key` 是占位字符串，本地服务不校验。

[README.md:L279-L285](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L279-L285) — 图像编码五连，对应核心流程的①–④：打开 `figures/demo.png` 并转 RGB、存入 `BytesIO` 缓冲区为 JPEG、`b64encode` 后 `decode("utf-8")` 得到纯文本、用 f 字符串拼出 data URL。注意 `format="JPEG"` 与前缀 `data:image/jpeg` 必须一致。

[README.md:L287-L289](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L287-L289) — 组装消息：`content` 是分片列表（与 u2-l1 同构），图片分片类型为 `image_url`，`url` 字段直接放 data URL；文本分片就是问题本身。对比 Transformers 版（L142）：分片类型从 `image` 变成 `image_url`，且从「占位」变成「载货」。

[README.md:L291-L296](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L291-L296) — 发起请求并取回结果：`model="kimi-vl-thinking-2506"` 必须与服务端 `--served-model-name` 起的别名一致（L292 注释说明 Instruct 服务则填 `kimi-vl`）；返回的 `completion` 对象里，答案在 `choices[0].message` 中。与离线推理对比：没有 `generated_ids_trimmed` 裁剪、没有 `batch_decode`——服务端已把 token 解码成文本，客户端拿到的直接就是字符串。

#### 4.2.4 代码实践

**实践目标**：跑通「编码图片 → 发请求 → 收答案」全链路，并验证 data URL 确实是纯文本。

**操作步骤**：

1. 保持 4.1 实践启动的服务运行；
2. 把 README L268–L297 的代码原样存为 `ask_kimi.py`（示例代码来自 README，可在仓库根目录执行）；
3. 先做一个小实验，只运行编码部分并检查：

   ```python
   import base64
   from PIL import Image
   from io import BytesIO

   image = Image.open("./figures/demo.png").convert("RGB")
   buffered = BytesIO()
   image.save(buffered, format="JPEG")
   img_b64_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
   print(f"data:image/jpeg;base64,{img_b64_str}"[:60], "...")
   print("原始字节:", len(buffered.getvalue()), "编码字符:", len(img_b64_str))
   ```

4. 运行完整脚本：`python ask_kimi.py`；
5. 改成多图测试：在 `messages` 的 `content` 里再插一个 `image_url` 分片（编码 `figures/demo1.png`），复用 u2-l2 的多图问题「请逐步推断这份手稿属于谁、记录了什么」。

**需要观察的现象**：第 3 步打印出的 data URL 以 `data:image/jpeg;base64,/9j/` 开头（`/9j/` 是 JPEG 文件的 base64 特征头）；「编码字符数 ÷ 原始字节数」约为 1.33；第 4 步输出对穹顶建筑（demo.png 拍摄的是宗教建筑场景）的描述。

**预期结果**：单图请求返回对图片的逐步描述；多图请求返回手稿推断。若服务端别名对不上，会收到「model not found」类错误。（待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `b64encode` 之后还要 `.decode("utf-8")`？
**答案**：`base64.b64encode` 返回的是 `bytes`（字节串），而 f 字符串拼接和 JSON 序列化需要 `str`（字符串）。`.decode("utf-8")` 把 base64 字节串（内容全是 ASCII 字符）转成 Python 字符串，二者内容相同、类型不同。

**练习 2**：能否跳过 PIL，直接对 PNG 文件二进制内容做 base64？
**答案**：可以，协议上行得通（把前缀改成 `data:image/png;base64,`）。README 选择「PIL 打开 → 转 RGB → 存 JPEG」的路线，好处是统一格式、丢弃 alpha 通道、JPEG 压缩缩小请求体。但对调色板图、CMYK 图等异常格式，PIL 的 `convert("RGB")` 更稳健，所以官方写法值得照抄。

**练习 3**：客户端不传 `api_key` 会发生什么？传错的密钥呢？
**答案**：不传时 openai SDK 在构造 `OpenAI()` 时就会抛出要求提供 api_key 的异常（本地服务根本收不到请求）；传任意非空错误密钥（如 `token-abc123` 本身就是占位假值）则本地 vLLM 默认不校验、请求照常通过。若部署在公网，需要另在 vLLM 侧配置鉴权（待确认具体配置方式，见 vLLM 文档）。

### 4.3 多模态并发与限制调优

#### 4.3.1 概念说明

这一模块回答部署时最实际的问题：**`--max-model-len 32768` 和 `--limit-mm-per-prompt image=64` 这两个数字是怎么定下来的，什么时候该调大？**

三个参数其实管着三件不同的事：

- `--max-model-len`：**单请求**的 token 总预算上限（视觉 token + 文本 + 生成输出）。它必须 ≤ 模型的 capability（128K = 131,072）。超过它的请求会被服务直接拒绝。
- `--limit-mm-per-prompt`：**单请求**的图片数量上限，防止单个请求塞爆预算的多模态护栏。
- `--max-num-batched-tokens`：**引擎单步**批处理的 token 预算，管的是吞吐与预填充调度，与单请求上限是两个维度。

为什么单请求上限重要？因为 KV cache 的容量账是「每序列都要按最大长度预留管理」。`max-model-len` 越大，可并发的序列越少、或显存占用越高——这是典型的「显存换容量」权衡，README L256–L257 的两行注释正是官方给出的权衡建议。

图片数与 token 数之间还有一个换算关系，把 u3-l1 的结论串起来：MoonViT 按 14×14 像素切 patch、再经 2×2 pixel shuffle 把视觉 token 压到四分之一，所以一张 \( w \times h \) 的图大约产生：

\[
\text{视觉 token 数} \approx \frac{w \times h}{14 \times 14} \times \frac{1}{4}
\]

代入 2506 版单图上限 1792×1792（[README.md:L32](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L32)，共约 3.2M 像素）：\( \frac{1792 \times 1792}{196} \div 4 = 16384 \div 4 = 4096 \) 个视觉 token。原生分辨率意味着小图产生的 token 按比例减少——这是估算，精确值以实际 tokenizer 输出为准。

#### 4.3.2 核心流程

三个参数如何互相牵制，用一条「请求进入服务」的流程串起来：

```text
请求到达 /v1/chat/completions
  │
  ├─ 校验①：图片数 ≤ limit-mm-per-prompt（64）？
  │     否 → 400 错误拒绝
  ├─ 校验②：编码后总 token ≤ max-model-len（32768）？
  │     否 → 请求被拒（提示超过最大长度）
  │        其中图片贡献 ≈ Σ 每张图(像素数/196/4) 个视觉 token
  ├─ 通过 → 进入引擎排队
  │     引擎按 max-num-batched-tokens（32768）分步打包预填充
  └─ KV cache 有空闲块 → 开始生成；无空闲 → 继续排队
```

由此推出两条调优不等式：

1. **图片预算约束**：若一个请求要带 \( n \) 张满分辨率图，需要 \( n \times 4096 + \text{text} \le \text{max-model-len} \)。取 README 注释里的 `image=256`：\( 256 \times 4096 = 1{,}048{,}576 \) 个视觉 token，远超 128K 能力——所以「放开图片数」必然同时要求「图片本身不大」或「max-model-len 顶到 131072 且图片数适度」。官方把两者写在同一行注释里，就是这个联动关系。
2. **吞吐/延迟取向**：`max-num-batched-tokens` 大 → 单步打包更多 token → 吞吐高但单步显存峰值大；小 → 峰值低但预填充变慢。README 对 128K 长上下文场景建议同步提到 131072，即「要吃满长上下文，预填充批量也要跟上」。

（具体的排队与拒绝行为细节以所用 vLLM 版本的实际实现为准，待本地验证。）

#### 4.3.3 源码精读

[README.md:L256-L257](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L256-L257) — 官方调优注释原文，两条建议对应两个维度：`--max-model-len` 与 `--max-num-batched-tokens` 同步提到 131072 服务长上下文；`--limit-mm-per-prompt` 提到 `image=256` 或 512 服务多图。这两行是本模块全部推理的官方依据。

[README.md:L260](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L260) — 默认值组合 `--max-num-batched-tokens 32768 --max-model-len 32768 --limit-mm-per-prompt image=64`：单请求最多 32K token、64 张图。按 4.3.1 的估算，64 张满分辨率图约需 26 万视觉 token，远超 32K——说明默认组合的含义是「允许 64 张**中小图**（例如 64 张约 448×448 的图各约 1K token），或少量大图」，图片数上限与 token 上限是两道独立的闸门。

[README.md:L32](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L32) — 2506 版支持单图 3.2M 总像素（1792×1792），是初代 4 倍。这条规格决定了「单张图最多吃掉约 4096 视觉 token」，是所有图片预算估算的基数。

[README.md:L23](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L23) — 128K 扩展上下文的官方表述（64.5 LongVideoBench、35.1 MMLongBench-Doc），说明 `--max-model-len 131072` 这个建议值就是把模型的 128K capability 完整开放出来。

#### 4.3.4 代码实践

**实践目标**：亲手调整两个限制参数各一次，从启动日志与请求行为两方面验证参数生效。

**操作步骤**：

1. **基线**：按 4.1 启动服务（`--max-model-len 32768 --limit-mm-per-prompt image=64`），记录启动日志中回显的配置；
2. **调整图片上限**：停掉服务，把 `--limit-mm-per-prompt image=64` 改为 `image=2` 重启；
3. 用 4.2 的脚本发一个带 3 张图（demo.png、demo1.png、demo2.png）的请求，观察返回；
4. **调整长度上限**：再把 `--max-model-len 32768` 改为 `16384` 重启，发一个带一张大图（可用 PIL 把 demo.png 放大到 1792×1792 后转存）加长文本的请求；
5. 每次重启都对照启动日志里 `max_model_len` 等回显值的变化。

**需要观察的现象**：第 3 步应收到图片数超限的错误（因为 3 > 2）；第 4 步长请求应收到超过最大长度的错误或被截断（取决于 vLLM 版本行为，待本地验证）；启动日志中回显的限制值随命令行同步变化。

**预期结果**：两个参数分别像两道独立的闸门——`image=N` 卡图片张数，`max-model-len=M` 卡总 token；都调大则服务可接收更大的请求，但日志中 KV cache 可用容量/并发能力会相应变化。（待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：业务方要求「单请求最多传 100 张 448×448 的截图」，参数该怎么配？
**答案**：先算 token：每张 \( 448 \times 448 / 196 / 4 = 1024 \div 4 = 256 \) 个视觉 token，100 张约 25,600，加文本与输出余量，`--max-model-len 32768` 恰好够用（或稳妥起见调到 49152）；`--limit-mm-per-prompt image=100`（≥100 即可）。若图片尺寸不可控，需按可能的最大分辨率重算并把 max-model-len 提到 131072。

**练习 2**：`--max-model-len 131072` 会有什么代价？为什么 README 默认只用 32768？
**答案**：代价在 KV cache 与并发：更长的序列上限意味着每条序列的缓存管理开销更大、同等显存下可并发的请求数更少，同时更长的预填充批量推高峰值显存。默认 32768 是「多数单图/少量多图场景够用」的保守值，能换更高的并发吞吐；只有长文档、长视频场景才值得按注释调到 131072。

**练习 3**：`--max-model-len` 和 `--max-num-batched-tokens` 都设成 32768，这两个 32768 是一回事吗？
**答案**：不是。前者是**单请求**的序列长度上限（能不能进门的门槛）；后者是**引擎单步**打包处理的总 token 预算（一次能同时处理多少活的调度参数），多个在途请求的预填充分块会共享这个预算。两者数值相同只是 README 选择的巧合式配平，语义完全不同。

## 5. 综合实践

把本讲三个模块串成一个「Kimi-VL 问答命令行小工具」，作为综合验收：

**任务**：编写 `kimi_ask.py`，功能为 `python kimi_ask.py --image figures/demo.png --question "穹顶建筑是什么？"`，要求：

1. 封装 `encode_image(path) -> str` 函数：完成 RGB 转换、JPEG 内存编码、base64、data URL 拼接（对应 4.2 的①–④，代码以 README L279–L285 为蓝本）；
2. 封装 `ask(image_urls: list[str], question: str) -> str` 函数：支持任意张图片（自动组装 `image_url` 分片列表），调用 `chat.completions.create`，返回 `choices[0].message.content`；
3. 从环境变量读取 `KIMI_BASE_URL`（默认 `http://localhost:8000/v1`）与 `KIMI_MODEL`（默认 `kimi-vl`），体会 `--served-model-name` 带来的解耦：换模型只改环境变量，不改代码；
4. 验证三组调用：
   - 单图 + 感知型问题（demo.png）；
   - 双图 + 推理型问题（demo1.png、demo2.png，沿用 u2-l2 的手稿推断问题）；
   - 故意传 3 张图但把服务端 `--limit-mm-per-prompt` 设为 `image=2`，观察第 4.3.4 步见过的拒绝行为；
5. 记录每组调用的耗时与回答，写 100 字总结：服务化调用与 u3-l2 离线推理在「输入构造、输出处理、权重生命周期」三点上的差异。

**验收标准**：前两组拿到合理回答；第三组复现图片数超限错误；总结能说清「离线是进程内对象调用、服务是 HTTP 文本协议」这一根本区别。（本实践需要 GPU 与已安装的 vLLM 环境，运行结果待本地验证。）

## 6. 本讲小结

- `vllm serve` 把 Kimi-VL 从「进程内对象」变成「常驻 HTTP 服务」，默认监听 8000 端口，暴露 OpenAI 兼容的 `/v1/chat/completions` 路由，六个关键参数各管一件事：模型来源、服务别名、远程代码授权、张量并行度、单步批量预算、单请求 token 上限与图片数上限。
- `--served-model-name` 实现「部署名 ≠ 仓库名」的解耦：客户端 `model` 字段填别名，换模型只改服务端一行命令。
- 服务化后图像只能走文本通道：PIL 转 RGB → JPEG 内存编码 → base64（体积膨胀约 4/3）→ `data:image/jpeg;base64,...` data URL → `image_url` 消息分片；本地服务的 `api_key` 是让 SDK 满意的占位符。
- 服务端已代劳解码：客户端拿到的直接是文本，离线推理里的裁剪（`generated_ids_trimmed`）与 `batch_decode` 两步在服务路线上完全消失。
- 三道闸门各司其职：`--limit-mm-per-prompt` 卡图片张数，`--max-model-len` 卡单请求总 token（视觉 token ≈ 像素数 ÷ 196 ÷ 4），`--max-num-batched-tokens` 卡引擎单步批量；调大前两者的代价是 KV cache 与并发能力。
- README L256–L257 的两行官方注释给出调优方向：长上下文场景把 `--max-model-len` 与 `--max-num-batched-tokens` 提到 131072，多图场景把 `--limit-mm-per-prompt` 提到 image=256 或 512。

## 7. 下一步学习建议

- **下一讲 [u3-l4]**：转向训练侧——通过 LLaMA-Factory 对 Kimi-VL 做 LoRA / 全量微调，理解发布型仓库如何借助社区框架补齐训练生态。
- **服务化深挖**：阅读 [vLLM OpenAI-Compatible Server 官方文档](https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html)（README L251 给出的链接），本讲只覆盖了 `chat.completions`，文档中还有流式输出（`stream=True`）、`max_tokens`/`temperature` 请求级参数等内容值得展开。
- **对照阅读**：回到 [README.md:L215-L246](https://github.com/MoonshotAI/Kimi-VL/blob/41d5ef072bc52a04524f94ab736ff9c29f125fda/README.md#L215-L246)（离线推理）与本讲 L268–L297（服务调用）并排对照，巩固「同一模型、两种交付形态」的认知。
- **收尾预告**：第 4 单元的 [u4-l3] 综合实战会把本讲的服务调用封装成完整的多模态问答应用，届时将复用本讲 5 节的综合实践成果。

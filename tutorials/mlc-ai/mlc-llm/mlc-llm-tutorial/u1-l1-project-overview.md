# 项目总览：ML 编译驱动的通用 LLM 部署引擎

## 1. 本讲目标

本讲是整本学习手册的第一篇。读完本讲，你应当能够：

- 用一句话说清楚 **MLC LLM 是什么**：它同时是一个「机器学习编译器」和一个「高性能 LLM 部署引擎」，两重身份缺一不可。
- 说出 MLC LLM **支持哪些硬件平台与后端**（NVIDIA/AMD/Apple/Intel GPU、Web 浏览器、iOS、Android），并理解「同一套引擎、同一份模型、跨后端部署」这一核心承诺。
- 认识 **MLCEngine** 这个统一抽象：它对外提供 **OpenAI 兼容 API**，可以通过 REST 服务器、Python、JavaScript、iOS、Android 等多种方式访问，而底层是同一套引擎和编译器。
- 学会**验证 mlc_llm 是否安装成功**，并跑通官方文档里的三种最小示例（chat CLI / Python API / REST 服务器）。

本讲只读不写代码，重点是建立「全局心智模型」。后续每一篇讲义都会在这个全局图里找到自己的位置。

## 2. 前置知识

在开始前，建议你大致了解下面几个名词。即便不完全理解也没关系，本讲会用通俗的方式再解释一遍。

- **大语言模型（LLM, Large Language Model）**：像 Llama、GPT 这样的模型，输入一段文字（prompt），输出一段续写的文字（completion）。
- **推理（Inference）**：模型训练好之后，用它来「生成回答」的过程。MLC LLM 关注的就是推理，而不是训练。
- **量化（Quantization）**：把模型权重从高精度（如 float16）压缩成低精度（如 4-bit整数），从而省显存、跑得更快。本讲的示例模型 `Llama-3-8B-Instruct-q4f16_1-MLC` 名字里的 `q4f16_1` 就是量化方案的名字（后面专门有一单元讲）。
- **编译（Compilation）**：把「模型的数学描述」翻译成「某块硬件上能高效执行的代码」。这是 MLC LLM 的看家本领。
- **OpenAI API**：OpenAI 公司为其 GPT 模型定义的一套 HTTP/Python 调用接口（如 `chat.completions.create`）。它已经成为业界事实标准，很多项目都「兼容」这套接口，MLC LLM 也是如此。

如果你对「编译」这个词还比较陌生，可以这样类比：模型权重是一份菜谱（数据），而编译器是把它改写成「针对你这口锅（GPU/CPU/Web）的最优火候步骤」的师傅。同一份菜谱，换一口锅就要重新改写步骤——这就是「跨平台编译」要解决的事。

## 3. 本讲源码地图

本讲引用的关键文件如下。它们都是「文档与入口层」的文件，用来建立全局认知，不涉及复杂代码：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目的门面。一句话定位、平台/后端支持矩阵、MLCEngine 的总体介绍都在这里。 |
| `docs/get_started/introduction.rst` | 官方入门教程。完整工作流（chat CLI → Python API → REST → 部署自己的模型）的最权威说明。 |
| `docs/index.rst` | 文档站点首页。用 toctree 列出了整个文档的章节结构，是「手册地图」。 |
| `python/mlc_llm/__init__.py` | Python 包的入口。`MLCEngine` 等对外 API 从这里导出，验证安装时也会用到。 |
| `python/mlc_llm/serve/engine.py` | `MLCEngine` 类的真实定义所在（本讲只看它的文档字符串，理解它「对齐 OpenAI API」）。 |
| `examples/python/sample_mlc_engine.py` | 官方最小可运行示例：用 `MLCEngine` 跑一次流式 chat completion。 |

> 提示：本讲是「总览」，所以引用以文档为主、代码为辅。从下一篇 `u1-l2` 开始，我们会真正深入目录结构与源码。

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **项目使命与定位** —— MLC LLM 到底是什么、解决什么问题。
2. **支持的平台/后端矩阵** —— 它能跑在哪些硬件上，靠什么后端驱动。
3. **MLCEngine 与 OpenAI 兼容 API** —— 对外统一抽象长什么样。

### 4.1 项目使命与定位

#### 4.1.1 概念说明

打开 `README.md`，最顶端的一行标语就是项目最精确的自我定位：

> **Universal LLM Deployment Engine with ML Compilation**
> （基于 ML 编译的通用 LLM 部署引擎）

这里有两个关键词，理解了它们就理解了 MLC LLM：

- **Deployment Engine（部署引擎）**：它负责让 LLM「真正跑起来」并对外提供服务，关注的是推理性能、显存占用、并发处理。
- **ML Compilation（机器学习编译）**：它不是一个只跑固定算子的运行库，而是会**针对你手里的硬件，把模型重新编译优化一遍**，榨干硬件性能。

把这两点合起来，README 给出的官方使命是：

> The mission of this project is to enable everyone to develop, optimize, and deploy AI models natively on everyone's platforms.
> （让每个人都能在**自己的**平台上，原生地开发、优化和部署 AI 模型。）

注意「natively（原生地）」和「everyone's platforms（每个人的平台）」——这是 MLC LLM 区别于很多推理框架的核心愿景：**不挑硬件、不绑云**。无论你手里是 NVIDIA 显卡、苹果 M 芯片、Android 手机还是浏览器，都希望同一套模型能原生跑起来。

一句话定位（建议你背下来）：

> **MLC LLM = 一个「会编译」的 LLM 推理引擎：把任意开源 LLM，自动编译优化后，部署到从手机到服务器再到浏览器的任意平台。**

#### 4.1.2 核心流程

「编译器 + 引擎」这两重身份，最直观的体现是官方入门文档里描述的 **chat CLI 背后三阶段**。第一次运行 `mlc_llm chat HF://mlc-ai/Llama-3-8B-Instruct-q4f16_1-MLC` 时，后台会依次做三件事：

```text
阶段1  下载预量化权重   ──┐
                          ├──► 这两步只在「首次」执行，之后会被本地缓存
阶段2  编译模型库     ──┘
阶段3  启动对话运行时 ─────► 消费阶段1的权重 + 阶段2的库，真正生成回答
```

- **阶段 1（下载预量化权重）**：从 Hugging Face 拉取已经量化好的模型权重，缓存到本地。
- **阶段 2（编译模型）**：用 Apache TVM 编译器把模型优化成「针对你本机 GPU」的二进制模型库（model library）。这一步体现了「ML 编译」身份。
- **阶段 3（对话运行时）**：加载阶段 2 的库 + 阶段 1 的权重，启动引擎驱动模型推理。这一步体现了「部署引擎」身份。

这个三阶段模型非常重要，它是后续 U1–U2 单元「工作流与产物」「CLI 子命令」两篇讲义的认知基础。本讲你只需要记住：**MLC LLM 的工作 = 先（编译）造出能跑的产物，再（引擎）跑它**。

#### 4.1.3 源码精读

标语与使命在 README 顶部：

- [README.md:L10-L10](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/README.md#L10-L10) —— 这行加粗标语 `Universal LLM Deployment Engine with ML Compilation` 是全项目最精炼的定位。
- [README.md:L18-L18](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/README.md#L18-L18) —— 官方使命原文：让每个人都能在自己的平台上原生地开发、优化、部署 AI 模型。

入门教程的开篇重复并展开了同样的定位：

- [docs/get_started/introduction.rst:L10-L12](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/get_started/introduction.rst#L10-L12) —— 「machine learning compiler and high-performance deployment engine」再次点明双重身份。

「三阶段」描述在入门教程的 Chat CLI 一节：

- [docs/get_started/introduction.rst:L64-L72](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/get_started/introduction.rst#L64-L72) —— Phase 1 下载权重、Phase 2 编译、Phase 3 运行时；并说明阶段 1、2 只在首次执行，之后缓存复用。

文档首页同样以使命开场，可作交叉印证：

- [docs/index.rst:L9-L12](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/index.rst#L9-L12) —— 文档站首页开门见山复述使命。

#### 4.1.4 代码实践

**实践目标**：亲手验证 MLC LLM 的 Python 包是否安装成功，并用一句话写下你对定位的理解。

**操作步骤**：

1. 打开终端，激活你安装 `mlc_llm` 时所用的虚拟环境（官方推荐用独立的 conda 环境）。
2. 运行官方文档给出的验证命令：
   ```bash
   python -c "import mlc_llm; print(mlc_llm.__path__)"
   ```
3. 如果安装成功，会打印出 `mlc_llm` 这个 Python 包在本地的安装目录（一个类似 `.../site-packages/mlc_llm` 的路径）。

**需要观察的现象**：

- 命令**没有报错**（若报 `ModuleNotFoundError`，说明环境未装好或未激活）。
- 输出的是**一个真实存在的目录路径**，而不是空字符串。

**预期结果**：

- 成功输出安装路径，例如（你的具体路径会不同）：
  ```text
  ['/home/you/miniconda3/envs/mlc/lib/python3.11/site-packages/mlc_llm']
  ```
- 该命令来自官方文档 [docs/get_started/introduction.rst:L25-L29](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/get_started/introduction.rst#L25-L29)。

> 如果在你的机器上无法安装（例如没有 GPU、网络受限），请把这一步标注为「待本地验证」，并在能联网的环境补做。本讲后续的实践都遵循同一原则。

最后，请用自己的话写下**一句话定位**，并和本讲 4.1.1 的一句话定位对照。

#### 4.1.5 小练习与答案

**练习 1**：MLC LLM 的「编译器」身份和「引擎」身份，分别对应三阶段流程中的哪个阶段？

> **答案**：编译器身份对应**阶段 2（编译模型库）**——把模型优化成针对本机硬件的二进制库；引擎身份对应**阶段 3（对话运行时）**——加载库与权重并驱动推理。阶段 1 只是下载权重，不属于这两者。

**练习 2**：为什么官方强调「natively（原生地）」部署？这与「调用云端 API」有什么不同？

> **答案**：原生部署指模型直接在你本地的硬件（手机/PC/浏览器）上执行，数据不出本机、不依赖云服务，也没有网络延迟与按量计费。这跟调用云端 API（模型跑在别人服务器上、你只发请求）是两种完全不同的部署形态。MLC LLM 的愿景就是让任意设备都能「原生」跑 LLM。

### 4.2 支持的平台/后端矩阵

#### 4.2.1 概念说明

要理解 MLC LLM 的「跨平台」，需要区分两个容易混淆的概念：

- **平台（Platform）**：操作系统/运行环境，例如 Linux、Windows、macOS、Web 浏览器、iOS、Android。
- **后端（Backend）**：真正执行计算的底层 API/驱动，例如 CUDA（NVIDIA）、ROCm（AMD）、Metal（Apple）、Vulkan（跨厂商）、OpenCL（移动端）、WebGPU/WASM（浏览器）。

一个平台可以有多种后端可选。例如在同一台 Linux 机器上，NVIDIA 显卡既可以走 **CUDA**（性能最好），也可以走 **Vulkan**（跨厂商、可移植）。MLC LLM 的强大之处在于：**同一份模型，只要改一个 `--device` 参数，就能切换后端**。

README 用一张表把「平台 × GPU 厂商 × 后端」的支持矩阵列得清清楚楚，这是判断「我的设备能不能跑」的最快参考。

#### 4.2.2 核心流程

「同一模型跨后端部署」在文档里被反复强调，其核心逻辑可以这样表达：

> The same core LLM runtime engine powers all the backends, enabling the same model to be deployed across backends **as long as they fit within the memory and computing budget** of the corresponding hardware backend.

也就是说，跨后端的**唯一硬约束是「装得下、算得动」**。我们可以用一个简单的预算不等式来理解这个约束：

\[
M_{\text{free}} \;\geq\; M_{\text{weight}} \;+\; M_{\text{kv}}(L) \;+\; M_{\text{act}}
\]

其中 \(M_{\text{free}}\) 是设备可用显存，\(M_{\text{weight}}\) 是模型权重占用（量化能显著降低它），\(M_{\text{kv}}(L)\) 是上下文长度 \(L\) 对应的 KV 缓存占用，\(M_{\text{act}}\) 是推理时的中间激活显存。只要右边之和不超过左边，模型就能在该后端跑起来——这也是为什么 4-bit 量化（缩小 \(M_{\text{weight}}\)）对「在小设备上跑大模型」如此关键。

切换后端的操作非常简单，文档给出了直接示例：用 `--device vulkan` 强制走 Vulkan 后端：

```bash
mlc_llm chat HF://mlc-ai/Llama-3-8B-Instruct-q4f16_1-MLC --device vulkan
```

#### 4.2.3 源码精读

支持矩阵的整张表在 README 中部：

- [README.md:L20-L61](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/README.md#L20-L61) —— 平台/后端支持矩阵表。为方便阅读，把表内容整理成中文版如下：

  | 平台 | AMD GPU | NVIDIA GPU | Apple GPU | Intel GPU |
  | --- | --- | --- | --- | --- |
  | Linux / Win | Vulkan, ROCm | Vulkan, CUDA | 不适用 | Vulkan |
  | macOS | Metal（独显） | 不适用 | Metal | Metal（核显） |
  | Web 浏览器 | WebGPU + WASM（所有厂商通用） | | | |
  | iOS / iPadOS | Metal（Apple A 系列 GPU） | | | |
  | Android | OpenCL（Adreno GPU）/ OpenCL（Mali GPU） | | | |

  从中可以总结：桌面 GPU 主要用 CUDA/ROCm/Metal/Vulkan；浏览器统一走 WebGPU+WASM；苹果移动端走 Metal；安卓走 OpenCL。

「跨后端只要装得下、算得动」的承诺，以及 `--device` 切换示例，在入门教程的「Universal Deployment」一节：

- [docs/get_started/introduction.rst:L307-L331](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/get_started/introduction.rst#L307-L331) —— 强调同一套核心运行时驱动所有后端，并用 `--device vulkan` 演示后端切换。

#### 4.2.4 代码实践

**实践目标**：读懂支持矩阵表，并确认「至少三种目标后端」这个事实。

**操作步骤**：

1. 打开 [README.md:L20-L61](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/README.md#L20-L61) 的支持矩阵表。
2. 在表中数一数一共出现了多少种**后端**（不是平台，是 CUDA/ROCm/Metal/Vulkan/WebGPU/WASM/OpenCL 这一类）。
3. 写下你自己的设备对应「哪个平台 + 哪个后端」，例如「macOS + Apple GPU + Metal」。

**需要观察的现象**：

- 你会发现后端种类远不止三种（CUDA、ROCm、Metal、Vulkan、WebGPU、WASM、OpenCL 至少七种）。
- 同一个平台常常有多种后端可选（如 Linux + NVIDIA 既支持 CUDA 也支持 Vulkan）。

**预期结果**：

- 至少列出三种目标后端，例如：**CUDA（NVIDIA）、Metal（Apple）、WebGPU（浏览器）**。这三者正好覆盖「服务器 / Mac / Web」三大典型场景。

> 本实践是纯阅读型，不依赖 GPU，任何机器都能完成。

#### 4.2.5 小练习与答案

**练习 1**：一位用户手上是 Apple M2 MacBook，另一位用户手上是 Android 手机。他们分别应该用哪个后端？

> **答案**：Apple M2 走 **Metal** 后端（Apple GPU）；Android 手机走 **OpenCL** 后端（Adreno 或 Mali GPU）。两者都可在支持矩阵表里查到。

**练习 2**：为什么 MLC LLM 在 Linux + NVIDIA 上同时提供 CUDA 和 Vulkan 两个后端？只用 CUDA 不就够了么？

> **答案**：CUDA 性能最好但只适用于 NVIDIA；Vulkan 是跨厂商的，能在「不太典型的环境」（官方举例 SteamDeck 这类掌机）上运行。保留 Vulkan 后端正是为了兑现「跨平台」承诺，让同一模型不被锁死在单一厂商的硬件上。

### 4.3 MLCEngine 与 OpenAI 兼容 API

#### 4.3.1 概念说明

如果说前两模块讲的是「MLC LLM 在底层是什么、能跑在哪」，那么本模块讲的是「**它对外长什么样**」。答案是：它对外伪装成了一个 **OpenAI**。

README 对此有一段纲领性描述：

> MLC LLM compiles and runs code on **MLCEngine** — a unified high-performance LLM inference engine across the above platforms. MLCEngine provides **OpenAI-compatible API** available through REST server, python, javascript, iOS, Android, all backed by the same engine and compiler.

理解这段话要抓住三点：

1. **MLCEngine 是统一引擎**：前面提到的所有平台、所有后端，最终都被这一套引擎驱动。你换硬件不用换 API。
2. **API 兼容 OpenAI**：调用方式刻意做得跟 OpenAI 官方 Python 包几乎一样。你以前用 `openai.ChatCompletion.create(...)`，现在换成 `engine.chat.completions.create(...)`，参数几乎可以照搬。
3. **多端入口、同一引擎**：REST 服务器、Python、JavaScript、iOS、Android 都是「入口」，背后是同一套 MLCEngine 和同一套编译器。这就保证了「在哪调用，行为都一致」。

为什么要兼容 OpenAI？因为 OpenAI 的 API 已经是业界事实标准。兼容它意味着：**已有的应用、SDK、评测脚本几乎可以零成本从 OpenAI 迁移到本地部署的 MLC LLM**。

#### 4.3.2 核心流程

官方入门教程给出了三种使用 MLCEngine 的入口，对应三种典型场景：

```text
                 ┌──► chat CLI      （mlc_llm chat ...）        交互式终端对话
同一份 MLC 模型 ──┼──► Python API   （MLCEngine / AsyncMLCEngine） 嵌入到 Python 程序
                 └──► REST 服务器   （mlc_llm serve ...）         对外提供 HTTP 服务
```

三者的关系是：

- **chat CLI**：最简单的人机交互入口，直接在终端里跟模型聊天，适合尝鲜。
- **Python API**：把引擎当成一个 Python 对象来调用，适合集成进你自己的程序。`MLCEngine` 是同步接口，`AsyncMLCEngine` 是异步接口（适合并发）。
- **REST 服务器**：用 `mlc_llm serve` 启动一个 HTTP 服务，任何语言都能通过 `curl` 或 HTTP 客户端访问，适合给团队/线上提供服务。

这三者底层都是 MLCEngine，区别只在「怎么调用」。下面是入门教程里的 Python API 最小示例（流式输出）：

```python
from mlc_llm import MLCEngine

model = "HF://mlc-ai/Llama-3-8B-Instruct-q4f16_1-MLC"
engine = MLCEngine(model)

for response in engine.chat.completions.create(
    messages=[{"role": "user", "content": "What is the meaning of life?"}],
    model=model,
    stream=True,
):
    for choice in response.choices:
        print(choice.delta.content, end="", flush=True)
engine.terminate()
```

对照 OpenAI 官方写法，你会发现除了把 `openai` 换成 `MLCEngine`，几乎一模一样——这就是「兼容」的含义。

#### 4.3.3 源码精读

MLCEngine 的纲领性描述在 README：

- [README.md:L63-L63](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/README.md#L63-L63) —— MLCEngine 是跨平台统一推理引擎，提供 OpenAI 兼容 API，可通过 REST/Python/JS/iOS/Android 访问。

入门教程里 Python API 与 REST 两节的具体用法：

- [docs/get_started/introduction.rst:L93-L117](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/get_started/introduction.rst#L93-L117) —— Python API 示例，演示 `MLCEngine` 流式 `chat.completions.create`。
- [docs/get_started/introduction.rst:L125-L129](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/get_started/introduction.rst#L125-L129) —— 明确说明：`MLCEngine` 的设计目标就是**对齐 OpenAI API**，可像用 OpenAI Python 包一样同步/异步使用。
- [docs/get_started/introduction.rst:L165-L190](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/get_started/introduction.rst#L165-L190) —— REST 服务器：`mlc_llm serve` 启动后默认监听 `http://127.0.0.1:8000`，并给出 `curl` 调用 `/v1/chat/completions` 的示例（注意端点路径正是 OpenAI 风格的 `/v1/...`）。

落回到真实代码，`MLCEngine` 类的定义与文档字符串把「对齐 OpenAI」写得很清楚：

- [python/mlc_llm/serve/engine.py:L1391-L1393](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine.py#L1391-L1393) —— `class MLCEngine(engine_base.MLCEngineBase)`，其文档字符串写明：它「provides the synchronous interfaces with regard to OpenAI API」（提供对齐 OpenAI API 的同步接口）。

`MLCEngine` 通过 Python 包顶层导出，所以才能 `from mlc_llm import MLCEngine`：

- [python/mlc_llm/__init__.py:L10-L10](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__init__.py#L10-L10) —— `from .serve import AsyncMLCEngine, MLCEngine`，把同步、异步两个引擎都从顶层导出。

官方最小可运行示例（与本讲综合实践直接相关）：

- [examples/python/sample_mlc_engine.py:L1-L19](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/examples/python/sample_mlc_engine.py#L1-L19) —— 完整的 `MLCEngine` 流式 chat completion 示例，可作为本讲实践的参考代码。

#### 4.3.4 代码实践

**实践目标**：跑通官方最小示例，亲眼看一次「兼容 OpenAI 风格」的流式输出。

**操作步骤**：

1. 确认 4.1.4 的安装验证已通过。
2. 把 [examples/python/sample_mlc_engine.py:L1-L19](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/examples/python/sample_mlc_engine.py#L1-L19) 的内容保存为本地 `sample.py`。
3. 运行：
   ```bash
   python sample.py
   ```

**需要观察的现象**：

- 首次运行会触发**阶段 1（下载权重）和阶段 2（JIT 编译）**，可能需要几分钟，期间会看到下载与编译日志。
- 随后模型逐字打印回答（`stream=True` 的效果），像打字机一样。
- 结束时调用 `engine.terminate()` 释放引擎资源。

**预期结果**：

- 模型对 "What is the meaning of life?" 给出一段连贯的回答，并逐 token 流式打印。
- 第二次运行会跳过下载和编译（命中本地缓存），启动明显变快。

> 若本机没有合适的 GPU 或无法联网下载模型，请标注为「待本地验证」，改为阅读型实践：把示例代码与 OpenAI 官方 chat completion 写法逐行对比，圈出「哪些行一字不差、哪些行只是把 `openai` 换成了 `MLCEngine`」。

#### 4.3.5 小练习与答案

**练习 1**：`MLCEngine` 和 `AsyncMLCEngine` 有什么区别？分别适合什么场景？

> **答案**：`MLCEngine` 是**同步**接口，调用会阻塞直到拿到结果，适合简单脚本和单请求场景；`AsyncMLCEngine` 是**异步**接口（基于协程），适合需要**并发处理多个请求**的场景（例如自己写一个高并发服务）。两者都来自 `mlc_llm` 顶层导出（见 `__init__.py:L10`）。

**练习 2**：REST 服务器默认监听的地址和端点路径是什么？为什么端点路径以 `/v1/` 开头？

> **答案**：默认监听 `http://127.0.0.1:8000`，chat completion 的端点是 `/v1/chat/completions`。`/v1/` 前缀是**对 OpenAI API URL 约定的刻意模仿**，目的是让已有的 OpenAI 客户端只要改个 base URL 就能直接访问 MLC LLM，体现「兼容 OpenAI」的设计。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿性小任务。

**任务**：为一个「想在自己的设备上跑 LLM」的新同学，写一份不超过 200 字的「MLC LLM 极速入门卡」，要求包含：

1. **定位**：用一句话写出 MLC LLM 是什么（参考 4.1.1 的一句话定位）。
2. **平台/后端**：列出至少三种 MLC LLM 支持的目标后端，并标注你自己的设备该用哪个（参考 4.2 与支持矩阵表 [README.md:L20-L61](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/README.md#L20-L61)）。
3. **三种入口**：写出 chat CLI / Python API / REST 三种使用方式各一条最小命令（参考 4.3 与 [docs/get_started/introduction.rst:L38-L43](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/get_started/introduction.rst#L38-L43)、[L99-L117](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/get_started/introduction.rst#L99-L117)、[L165-L190](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/get_started/introduction.rst#L165-L190)）。
4. **验证**：附上 `python -c "import mlc_llm; print(mlc_llm.__path__)"`（参考 4.1.4）作为「检查是否装好」的一行命令。

**参考答案骨架**（请你用自己的设备和语言补全）：

```text
定位：MLC LLM 是一个会编译的 LLM 推理引擎，把任意开源 LLM 编译优化后部署到手机/PC/服务器/浏览器。
后端：CUDA（NVIDIA）、Metal（Apple）、WebGPU（浏览器）等；我的设备 = macOS + Apple GPU + Metal。
三入口：
  - chat CLI : mlc_llm chat HF://mlc-ai/Llama-3-8B-Instruct-q4f16_1-MLC
  - Python   : from mlc_llm import MLCEngine; engine = MLCEngine(model); engine.chat.completions.create(...)
  - REST     : mlc_llm serve HF://mlc-ai/Llama-3-8B-Instruct-q4f16_1-MLC  → curl /v1/chat/completions
验证：python -c "import mlc_llm; print(mlc_llm.__path__)"
```

> 如果条件允许，在写完入门卡后，挑一种入口在本机真正跑一次（推荐先跑 chat CLI，最简单）。无法运行的部分请明确标注「待本地验证」，不要伪造运行结果。

## 6. 本讲小结

- MLC LLM 的定位是 **「基于 ML 编译的通用 LLM 部署引擎」**，同时具备**编译器**（把模型优化成硬件专用代码）和**部署引擎**（驱动推理并对外服务）两重身份。
- 它的使命是让每个人都能在**自己的**平台上**原生地**部署 AI 模型，不挑硬件、不绑云。
- 首次运行 chat CLI 会经历**下载权重 → 编译模型 → 启动运行时**三阶段，前两阶段结果会被缓存复用。
- 支持矩阵覆盖 **NVIDIA(CUDA/Vulkan)、AMD(ROCm/Vulkan)、Apple(Metal)、Intel(Vulkan/Metal)、Web(WebGPU/WASM)、iOS(Metal)、Android(OpenCL)**；切换后端只需 `--device`，唯一硬约束是「装得下、算得动」。
- **MLCEngine** 是跨平台/跨后端的统一引擎，对外提供 **OpenAI 兼容 API**，可通过 chat CLI / Python(`MLCEngine`) / REST 等多种入口访问，背后是同一套引擎与编译器。
- 用 `python -c "import mlc_llm; print(mlc_llm.__path__)"` 即可一键验证安装是否成功。

## 7. 下一步学习建议

本讲建立的是「全局心智模型」，还没有真正进入仓库内部。建议按手册顺序继续：

- **下一篇 `u1-l2`（仓库目录结构与多语言布局）**：带你走进仓库顶层目录，看清 `python/`（编译器与 CLI）、`cpp/`（推理引擎）、`android/`、`ios/`、`docs/`、`3rdparty/`（tvm 等）各自负责什么，区分「编译期代码」和「运行期代码」。这是从「读文档」过渡到「读源码」的关键一步。
- **再往后 `u1-l3`/`u1-l4`**：分别讲解安装构建与端到端工作流（convert_weight → gen_config → compile → serve）及三类模型产物，把本讲提到的「三阶段」细化成可操作的命令链。
- 如果你已经迫不及待想看代码：可以先把本讲引用的 [examples/python/sample_mlc_engine.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/examples/python/sample_mlc_engine.py) 跑通，再去 `u1-l2` 对照目录结构，理解这段示例代码背后的引擎究竟住在仓库的哪里。

# SGLang 是什么：项目定位与架构总览

> 本讲是整个学习手册的第一篇，面向「完全没接触过 SGLang」的读者。
> 读完后你不需要理解任何内部机制，但你能说清楚：SGLang 是什么、它由哪两大部分组成、它的公共 API 长什么样、怎么确认自己的环境能跑起来。

---

## 1. 本讲目标

读完本讲，你应该能够：

1. 用一句话说清楚 SGLang 解决什么问题，以及它和 vLLM、TensorRT-LLM 这类项目的关系。
2. 记住 SGLang 的「两层架构」：前端语言层（`lang`）与运行时层（`srt`），并能说清各自职责。
3. 打开 `python/sglang/__init__.py` 后，知道哪些符号是给前端用的、哪些是给运行时用的。
4. 知道版本号 `sglang.__version__` 是怎么来的、`global_config` 里默认存了哪些全局常量。
5. 在自己机器上跑通一次「安装 → 打印版本 → `sglang.Engine` 生成一句话」的最小验证流程。

---

## 2. 前置知识

本讲不假设你读过 SGLang 的任何代码，但下面几个名词最好有个印象：

- **LLM（大语言模型）推理**：把一段文字（prompt）喂给模型，让它一个 token 一个 token 地「生成」后续文字。token 可以粗略理解为「一个词或词的一部分」。
- **服务框架（serving framework）**：把「单次调用模型」包装成一个能同时服务成千上万并发请求、还能控制显存与吞吐的系统。vLLM、TensorRT-LLM、SGLang 都属于这一类。
- **多模态模型（multimodal）**：除了文字，还能接收图像、视频、音频等输入的模型。
- **KV cache / 前缀缓存（prefix caching）**：模型在生成时会把前面 token 的中间结果（key/value）缓存下来，遇到相同前缀的新请求就能直接复用，不必重算。SGLang 的招牌特性 **RadixAttention** 就是干这件事的，本讲只做概念介绍，细节留到第 6 单元。
- **Python 包**：`import sglang` 时，Python 实际执行的是 `sglang` 包目录下的 `__init__.py`。本讲会反复读这个文件。

如果你对以上概念陌生也没关系，本讲会结合源码逐步解释。

---

## 3. 本讲源码地图

本讲只读 4 个文件，都是「最顶层」的入口级文件，不需要进入子系统：

| 文件 | 作用 | 在本讲的角色 |
| --- | --- | --- |
| `README.md`（仓库根目录） | 项目自我介绍：定位、核心特性、支持的模型与硬件、应用场景 | 帮你建立「SGLang 是什么」的直觉 |
| `python/sglang/__init__.py` | `import sglang` 时执行的包入口，导出所有公共 API | 帮你看清「前端 `lang` + 运行时 `srt`」两层公共接口 |
| `python/sglang/version.py` | 决定 `sglang.__version__` 的取值 | 一个最小、可验证的源码阅读样例 |
| `python/sglang/global_config.py` | 存放少量「全局常量」的默认配置 | 理解全局配置的来历与它正在被迁移的现状 |

> 说明：本仓库的代码永久链接 base 是
> `https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/`。
> 凡是 `python/sglang/` 下的文件，链接都形如 `.../python/sglang/<相对路径>#L起始-L结束`。
> 仓库根目录的 `README.md` 不在这个 base 下，会单独给出完整链接。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 SGLang 的定位与核心特性** —— 先建立直觉，知道它「是什么、能干什么」。
2. **4.2 sglang 顶层包的公共 API（lang 与 srt 的分界）** —— 读 `__init__.py`，看清两层架构。
3. **4.3 global_config 默认配置** —— 读 `global_config.py` 和 `version.py`，理解全局常量。

---

### 4.1 SGLang 的定位与核心特性

#### 4.1.1 概念说明

SGLang（发音类似「S-G-Lang」）是一个**面向大语言模型与多模态模型的高性能推理服务框架（serving framework）**。

一句话定位（直接来自 README）：

> SGLang is a high-performance serving framework for large language models and multimodal models.

它要解决的核心问题是：**如何让一个几十亿到上万亿参数的模型，在一台甚至成百上千台 GPU 上，又快又稳地服务海量并发请求。**

它和同类项目的关系可以这样理解：

- **vLLM**：业界最早的「分页注意力 + 连续批处理」开源推理框架，SGLang 的运行时在工程上借鉴了它的很多思路（README 的致谢里明确提到）。
- **TensorRT-LLM**：NVIDIA 官方的推理引擎，性能极高，但偏「封闭、绑定 NVIDIA、配置繁琐」；SGLang 是开源的，且覆盖 NVIDIA / AMD / Intel / TPU / Ascend 等多种硬件。
- **SGLang 的差异化卖点**：**RadixAttention**（用基数树做前缀缓存，命中即复用 KV）、**零开销 CPU 调度器**、**Prefill-Decode 分离部署**、**投机解码**、**结构化输出**、以及对**强化学习（RL）rollout**场景的一等支持。

#### 4.1.2 核心流程（两层架构鸟瞰）

SGLang 的代码在物理上分成两大块，这也是贯穿整本学习手册的主线：

```text
┌──────────────────────────────────────────────────────────────────┐
│  python/sglang/                                                  │
│                                                                  │
│   ┌──────────────────────┐        ┌───────────────────────────┐  │
│   │   lang/  前端语言层   │  调用  │   srt/  运行时层（SGLang  │  │
│   │  - @function/gen/    │ ────▶ │         Runtime）          │  │
│   │    select/image DSL  │        │  - 调度、KV 缓存、GPU 执行 │  │
│   │  - tracer/interpreter│        │  - 分布式、PD 分离、投机…  │  │
│   └──────────────────────┘        └───────────────────────────┘  │
│         「写程序」                      「跑模型」                 │
│                                                                  │
│   cli/   命令行入口（sglang serve/generate/version）             │
│   kernels/  自定义高性能算子（CUDA/JIT）                          │
│   test/ benchmark/ eval/  测试与基准                              │
└──────────────────────────────────────────────────────────────────┘
```

- **前端 `lang/`**：一套「领域特定语言（DSL）」，让你用 `@function`、`gen`、`select` 这类原语**像写程序一样描述一次复杂的 LLM 调用流程**（例如「生成→分支选择→再生成」）。它本身**不直接跑模型**，而是把你的程序编译成一棵中间表示（IR），再交给后端执行。
- **运行时 `srt/`**：真正负责「加载模型、调度 batch、在 GPU 上 forward、管理 KV cache」的引擎。HTTP 服务、`sglang.Engine`、三大管理器（TokenizerManager / Scheduler / DetokenizerManager）都住在这里。

> 一个关键区别要记住：**前端 `lang` 是「可选的」**。很多用户从来不写前端 DSL，而是直接 `sglang.Engine(...)` 或起一个 HTTP 服务，调 OpenAI 兼容接口——这些都只用到运行时 `srt`。前端 DSL 主要面向需要复杂生成控制流的场景。

#### 4.1.3 源码精读

定位和核心特性最权威的来源就是 README 的 **About** 段落。下面这一句给出了最准确的「是什么」：

- [README.md:L62-L63](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/README.md#L62-L63) —— 原文定义「SGLang is a high-performance serving framework for large language models and multimodal models」，并说明它的目标是从单卡到大规模集群的低延迟、高吞吐推理。

紧跟着的 5 条特性 bullet 是 SGLang 的「能力清单」，建议逐条对照记一下：

- [README.md:L66-L70](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/README.md#L66-L70) —— 列出核心特性与适用场景：
  - **Fast Runtime**：RadixAttention、零开销调度器、Prefill-Decode 分离、投机解码、连续批处理、分页注意力、张量/流水线/专家/数据并行、结构化输出、chunked prefill、量化（FP4/FP8/INT4/AWQ/GPTQ）、多 LoRA 批处理。
  - **Broad Model Support**：Llama / Qwen / DeepSeek / Kimi / GLM / GPT / Gemma / Mistral 等语言模型，外加 embedding 模型、reward 模型、扩散模型。
  - **Extensive Hardware Support**：NVIDIA（GB200/B300/H100/A100 等）、AMD（MI355/MI300）、Intel CPU、Google TPU、华为 Ascend NPU。
  - **Active Community**：开源、社区活跃、据称全球支撑超 40 万张 GPU。
  - **RL & Post-Training Backbone**：SGLang 是许多前沿模型训练时的 **rollout 后端**（生成回放），原生对接 AReaL、Miles、slime、Tunix、verl 等强化学习/后训练框架。

最后，README 的致谢段落透露了 SGLang 的「技术血统」，对理解它的设计很有帮助：

- [README.md:L95](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/README.md#L95) —— 明确说明 SGLang 借鉴了 Guidance（前端 DSL 思路）、vLLM（运行时）、LightLLM、FlashInfer（注意力算子）、Outlines（结构化输出）、LMQL 的设计与代码。这解释了为什么它的前端像 Guidance、运行时像 vLLM。

#### 4.1.4 代码实践

**实践目标**：用 README 给出的「能力清单」做一次有针对性的阅读，建立一张特性速查表，为后续每一讲对应到具体特性做准备。

**操作步骤**：

1. 打开上面 [README.md:L66-L70](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/README.md#L66-L70) 的 5 条 bullet。
2. 建一张两列表格：左列「特性名」，右列「一句话解释 + 我猜测它对应哪个子系统目录（如 `mem_cache/`、`speculative/`、`disaggregation/`、`distributed/`、`constrained/`、`lora/`）」。
   - 例如：`RadixAttention` → 前缀缓存，对应 `srt/mem_cache/`；`speculative decoding` → 投机解码，对应 `srt/speculative/`。
3. **不需要现在去读这些子系统**，只是先把「特性 ↔ 目录」的对应关系猜出来。后续每一讲会逐一验证。

**需要观察的现象**：你会发现自己能凭目录名猜对大部分特性的归属——这正说明 SGLang 的目录划分是「按特性」组织的。

**预期结果**：得到一张 10 行左右的速查表，覆盖 RadixAttention、零开销调度、PD 分离、投机解码、连续批处理、张量/流水线/数据/专家并行、结构化输出、量化、多 LoRA 等。这张表会成为你后续阅读的索引。

#### 4.1.5 小练习与答案

**练习 1**：SGLang 同时支持「服务」和「RL rollout」两种场景。请根据 README 判断：这两种场景用的是同一套运行时，还是两套独立实现？

> **参考答案**：用的是**同一套运行时**。README 的 `RL & Post-Training Backbone` 明确把 SGLang 定位为「被 RL 框架使用的 rollout 后端」，而这些 RL 框架（verl、AReaL 等）是通过 SGLang 的运行时 API（如 `sglang.Engine` 或 HTTP）来发起生成的，并非另一套实现。

**练习 2**：SGLang 的「Fast Runtime」bullet 里出现了 `paged attention` 和 `RadixAttention` 两个词。粗略说说它们各自管什么。

> **参考答案**：`paged attention`（分页注意力）解决「如何把一整段序列的 KV 切成固定大小的 page、像操作系统管理虚拟内存一样管理显存」，避免显存碎片；`RadixAttention` 解决「如何让**不同请求之间相同前缀**的 KV 被复用」，通过基数树做前缀缓存。前者偏「单请求内的内存布局」，后者偏「多请求间的缓存共享」。

---

### 4.2 sglang 顶层包的公共 API（lang 与 srt 的分界）

#### 4.2.1 概念说明

当你 `import sglang` 时，Python 会执行 `python/sglang/__init__.py`。这个文件是 SGLang 对外暴露的**公共 API 清单**。读懂它，就等于拿到了一张「SGLang 能给你什么」的总目录。

这个文件透露的第一件大事，就是 4.1 节说的**两层架构**：它分别从前端 `lang` 和运行时 `srt` 引入符号，再把它们合并成一个统一的顶层命名空间。

#### 4.2.2 核心流程

`__init__.py` 的执行可以分成几步：

1. **环境兜底**：在某些平台（如 Apple Silicon / MPS）上，提前安装 triton、MPS 的 stub（占位实现），让缺少某些依赖的机器也能 import。
2. **打 HuggingFace 补丁**：在导入任何下游模块前，先给 `transformers` 打补丁，保证兼容性。
3. **导入前端 API**：从 `sglang.lang.api` 引入 `function / gen / select / image / video` 等 DSL 原语，以及 `global_config`。
4. **惰性导入第三方后端**：`Anthropic / OpenAI / LiteLLM` 等用 `LazyImport` 包裹，用到才真正 import，避免启动变慢。
5. **覆盖式导入运行时引擎**：最后用 `LazyImport` 把 `Engine`、`ServerArgs` 指向运行时 `srt` 的实现。

> 注意第 5 步的关键细节：`Engine` 这个名字**先**被前端 `lang.api` 赋值，**后**被运行时 `srt.entrypoints.engine` 覆盖。所以你写 `sglang.Engine`，拿到的是**运行时引擎**，不是前端的那个同名类。这是一个容易踩坑的点，记住它。

#### 4.2.3 源码精读

**（1）环境兜底与 HF 补丁**——文件开头先处理平台差异，再打补丁：

- [`__init__.py:L9-L27`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/__init__.py#L9-L27) —— 在 Apple Silicon（`darwin` + `arm64`）且有 MPS 时，安装 triton stub 和 MPS stub，因为 macOS 上没有 triton、`torch.mps` 也缺一些 API。
- [`__init__.py:L29-L32`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/__init__.py#L29-L32) —— `apply_all` 给 HuggingFace `transformers` 打补丁，必须在下游 import 之前完成。

**（2）前端语言层 API**——这是「写程序」的那一层：

- [`__init__.py:L34-L59`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/__init__.py#L34-L59) —— 从 `sglang.lang.api` 导入前端 DSL 原语：`function`（定义可复用生成程序）、`gen / gen_int / gen_string`（生成）、`select`（在候选项里挑）、`image / video`（多模态输入）、`system / user / assistant` 等角色原语、`set_default_backend`、`flush_cache`、`get_server_info` 等。注意这里也导入了一个叫 `Engine` 和 `Runtime` 的前端符号（但 `Engine` 稍后会被覆盖）。
- [`__init__.py:L60`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/__init__.py#L60) —— `RuntimeEndpoint` 来自 `sglang.lang.backend.runtime_endpoint`，是前端把请求转发到 SGLang 运行时的「后端适配器」。
- [`__init__.py:L61-L65`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/__init__.py#L61-L65) —— 从 `sglang.lang.choices` 导入三种「select 候选项打分方法」，例如 `token_length_normalized`（按 token 长度归一化）。这些决定了 `select` 时哪个候选项胜出。

**（3）惰性导入**——`LazyImport` 让重型后端只在真正用到时才加载：

- [`__init__.py:L67-L75`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/__init__.py#L67-L75) —— 先导入 `LazyImport` 工具和 `__version__`，再用 `LazyImport` 包裹 `Anthropic / Crusoe / LiteLLM / OpenAI / VertexAI` 五个第三方后端。这样 `import sglang` 不会因为装了某个 SDK 没装另一个而变慢或报错。

**（4）运行时引擎 API（覆盖前端同名符号）**——这是「跑模型」的那一层：

- [`__init__.py:L77-L79`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/__init__.py#L77-L79) —— `ServerArgs` 和 `Engine` 都用 `LazyImport` 指向运行时：`sglang.srt.server_args.ServerArgs` 和 `sglang.srt.entrypoints.engine.Engine`。由于这两行在第 36–59 行之后执行，顶层 `sglang.Engine` 最终就是**运行时引擎**。

**（5）公共 API 总表**——`__all__` 列出了对外承诺的所有符号：

- [`__init__.py:L81-L116`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/__init__.py#L81-L116) —— `__all__` 就是 SGLang 的「公共 API 契约」。可以看到它混合了前端符号（`function/gen/select/...`）、后端适配（`RuntimeEndpoint`、`OpenAI` 等）、运行时入口（`ServerArgs`、`Engine`）、全局对象（`global_config`）和版本号（`__version__`）。

> 关于 `sglang.Engine` 到底是什么：它是运行时引擎的入口。它的类文档把整个运行时的进程结构讲得很清楚，虽然细节属于第 3 单元，但值得现在看一眼——
> [`srt/entrypoints/engine.py:L183-L195`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L183-L195) —— `class Engine` 的 docstring 说明运行时由 **TokenizerManager（主进程）+ Scheduler（子进程）+ DetokenizerManager（子进程）** 三部分组成，进程间通过 ZMQ 做 IPC。这正是后续第 3、4 单元要深入的内容。
> [`srt/entrypoints/engine.py:L318`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L318) —— `Engine.generate(...)` 是最常用的入口方法，本讲的综合实践会用到它。

#### 4.2.4 代码实践

**实践目标**：亲手验证「`sglang.Engine` 指向运行时而非前端」，并看清公共 API 清单。

**操作步骤**（纯 Python，无需 GPU）：

1. 在已安装 sglang 的环境里，打开 Python REPL，执行：

   ```python
   import sglang
   # 1. 看公共 API 清单
   print([n for n in sglang.__all__])
   # 2. 看 Engine 来自哪个模块
   print(sglang.Engine.__module__ if hasattr(sglang.Engine, "__module__") else sglang.Engine)
   # 3. 看前端 function 来自哪里
   print(sglang.function)
   ```

2. 对照 [`__init__.py:L77-L79`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/__init__.py#L77-L79) 解释你看到的 `Engine` 来源。

**需要观察的现象**：`sglang.all` 里同时有 `Engine` 和前端 DSL 原语；`Engine` 最终指向 `sglang.srt.entrypoints.engine`（运行时），而不是 `sglang.lang.api`。

**预期结果**：你能用一句话说清「为什么 `__init__.py` 里 `Engine` 出现了两次，而最终生效的是运行时那一个」。如果 `sglang.Engine.__module__` 因为 `LazyImport` 不能直接显示模块名，可改为 `print(type(sglang.Engine))` 或阅读 `__init__.py` 第 36 行与第 79 行确认覆盖关系。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Anthropic / OpenAI / LiteLLM` 这些第三方后端要用 `LazyImport`，而 `function / gen / select` 不用？

> **参考答案**：`function / gen / select` 是纯 Python 的前端原语，加载它们代价很小，且是 SGLang 的核心 API，应当立即可用；而 `Anthropic / OpenAI / LiteLLM` 依赖各自的第三方 SDK，并非每个用户都装了，且体积大。`LazyImport` 让它们「用到才加载」，避免 `import sglang` 时被迫加载一堆可能缺失的重型依赖。

**练习 2**：在 `__init__.py` 里，`Engine` 这个名字被赋值了两次。请说出这两次分别来自哪里，以及最终 `sglang.Engine` 是哪一个。

> **参考答案**：第一次来自第 36–59 行的 `from sglang.lang.api import (..., Engine, ...)`（前端）；第二次来自第 79 行 `Engine = LazyImport("sglang.srt.entrypoints.engine", "Engine")`（运行时）。因为第 79 行在后，所以最终 `sglang.Engine` 是**运行时引擎** `sglang.srt.entrypoints.engine.Engine`。

---

### 4.3 global_config 默认配置

#### 4.3.1 概念说明

`global_config` 是 SGLang 历史最久的「全局配置」对象：一个进程内唯一的 `GlobalConfig` 实例，存了几个会影响前端行为的全局常量（日志详细度、默认后端、输出 token 化选项、前端解释器优化开关等）。

> ⚠️ 注意一个现状：[`global_config.py:L3`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/global_config.py#L3) 有一句 FIXME——**这个文件计划被废弃**，相关用法将迁移到 `sglang.srt.environ`（运行时环境变量）或 `sglang.__init__.py`。所以本节你只需要「知道它是什么、它现在存了什么」，不必把它的字段当成稳定 API。环境变量的体系会在第 3 单元（u3-l5）专门讲。

#### 4.3.2 核心流程

`global_config` 的使用方式非常直接：

1. 模块加载时，`global_config.py` 末尾创建一个全局单例 `global_config = GlobalConfig()`。
2. `__init__.py` 把这个单例 re-export 出去，于是 `sglang.global_config` 就是它。
3. 任何代码都可以读写 `sglang.global_config.verbosity` 等字段来改变全局行为（例如把详细度从 0 调到 2，让前端每次运行后打印最终文本）。

它和 `version.py` 一起，是本讲里「最小、最容易读懂」的两个文件，很适合作为源码阅读的起点。

#### 4.3.3 源码精读

**（1）GlobalConfig 的默认值**：

- [`global_config.py:L6-L27`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/global_config.py#L6-L27) —— `GlobalConfig.__init__` 设定默认值，重点字段：
  - `verbosity`（L15）：日志详细度，`0` 不输出，`2` 每次运行后输出最终文本。
  - `default_backend`（L18）：前端默认后端，初始为 `None`，可用 `set_default_backend` 设置。
  - `skip_special_tokens_in_output` / `spaces_between_special_tokens_in_out`（L21-L22）：输出 token 化（反序列化成文本）时是否跳过/间隔特殊 token。
  - `enable_precache_with_tracing` / `enable_parallel_encoding`（L25-L26）：前端解释器的优化开关（预缓存追踪、并行编码），属于第 2 单元的前端执行机制。

**（2）全局单例**：

- [`global_config.py:L29`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/global_config.py#L29) —— `global_config = GlobalConfig()` 创建进程级唯一实例；`__init__.py` 第 35 行把它导出为 `sglang.global_config`。

**（3）版本号是怎么来的（顺带读 `version.py`）**：

`sglang.__version__` 并非硬编码，而是按一套「回退链」动态确定，这正好是个练手的好例子：

- [`version.py:L1-L9`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/version.py#L1-L9) —— 首选：从构建时生成的 `sglang._version` 读取（正式发布包走这条）；读不到则回退到 `importlib.metadata.version("sglang")`（从已安装的包元数据读）。
- [`version.py:L13-L20`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/version.py#L13-L20) —— 再读不到则用 `setuptools_scm`，从 git 标签动态算版本；注意 `project_root` 取的是 `__file__` 往上三级，即包含 `pyproject.toml` 的仓库根目录。
- [`version.py:L21-L24`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/version.py#L21-L24) —— 最终兜底：连 setuptools_scm 都不可用（例如完全没构建过的裸开发环境），返回 `"0.0.0.dev0"`。所以**你在开发环境下看到 `0.0.0.dev0` 是正常的**，不代表安装坏了。

#### 4.3.4 代码实践

**实践目标**：直接读取 `global_config` 的默认值，验证它与源码一致；并解释版本号字符串的来源。

**操作步骤**（纯 Python，无需 GPU）：

```python
import sglang
cfg = sglang.global_config
print("verbosity =", cfg.verbosity)
print("default_backend =", cfg.default_backend)
print("skip_special_tokens_in_output =", cfg.skip_special_tokens_in_output)
print("enable_parallel_encoding =", cfg.enable_parallel_encoding)
print("__version__ =", sglang.__version__)
```

**需要观察的现象**：打印出的字段值应与 [`global_config.py:L15-L26`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/global_config.py#L15-L26) 的默认值逐一吻合；`__version__` 则取决于你是 pip 安装的正式版（形如 `0.4.x`）还是开发环境（可能是 `0.0.0.dev0` 或带 `+git` 后缀的 scm 版本）。

**预期结果**：你能根据 `__version__` 的实际取值，反推出它走了 `version.py` 哪一条回退分支（`_version` / metadata / scm / 兜底）。这是「读源码理解运行时行为」的一次最小练习。

#### 4.3.5 小练习与答案

**练习 1**：如果你的 `sglang.__version__` 打印出 `0.0.0.dev0`，是不是说明安装出错了？

> **参考答案**：**不一定**。根据 [`version.py:L21-L24`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/version.py#L21-L24)，这只是一个兜底值，表示前三条来源（`_version`、包元数据、setuptools_scm）都不可用，常见于「直接 clone 源码、既没装发布包、也没装 setuptools_scm」的裸开发环境。功能本身不受影响，只是版本号无法精确确定。

**练习 2**：`global_config.py` 顶部的 FIXME 说这个文件要被废弃，迁移目标是哪里？这对你写代码有什么提示？

> **参考答案**：迁移目标是 `sglang.srt.environ`（运行时环境变量集中定义）或 `sglang.__init__.py`。提示是：**不要在新代码里新增对 `global_config` 字段的依赖**，尤其是运行时相关的开关；运行时的可调参数应优先走环境变量体系（第 3 单元 u3-l5 会讲 `srt/environ.py` 的约定）。

---

## 5. 综合实践

本讲的综合实践是**一次端到端的环境验证**：从安装到让 `sglang.Engine` 真正生成一句话。这也是本讲规格里指定的实践任务。

**实践目标**：确认你的环境能跑起 SGLang 运行时，并把本讲学到的「公共 API、版本号、两层架构」串起来。

**操作步骤**：

1. **安装**（任选其一；`[all]` 会装上运行时、服务端、常见后端等全部可选依赖）：

   ```bash
   pip install "sglang[all]"
   ```

   > 如果只想跑最小验证、不装全套，也可先 `pip install sglang` 再按报错补依赖。注意：真正在 GPU 上跑模型还需要对应的 torch + CUDA 环境。

2. **打印版本号**，并解释它来自 `version.py` 的哪条分支：

   ```python
   import sglang
   print(sglang.__version__)
   ```

3. **用 `sglang.Engine` 跑一句 "Hello" 生成**（示例代码，请替换成你本地可用的模型路径）：

   ```python
   # 示例代码（非项目自带脚本）：用 sglang.Engine 做一次最小生成验证
   import sglang

   # model_path 换成你本地可用的 HF 模型路径，例如 "Qwen/Qwen2.5-0.5B"
   engine = sglang.Engine(model_path="Qwen/Qwen2.5-0.5B")

   out = engine.generate(
       prompt="Hello",
       sampling_params={"max_new_tokens": 16, "temperature": 0.0},
   )
   print(out["text"])
   engine.shutdown()
   ```

   - 这里用到的 `sglang.Engine` 就是 4.2 节确认过的**运行时引擎**（`sglang.srt.entrypoints.engine.Engine`），它的 `generate` 方法签名见 [`srt/entrypoints/engine.py:L318`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L318)。
   - `sampling_params` 里 `max_new_tokens` 控制最多生成多少个新 token，`temperature=0.0` 表示贪心采样（确定性输出）。

**需要观察的现象**：

- 安装后 `sglang.__version__` 能正常打印（即使是 `0.0.0.dev0` 也不影响）。
- `Engine(...)` 启动时，日志里会出现 TokenizerManager / Scheduler / DetokenizerManager 三个组件的初始化信息——这正好印证 [`engine.py:L183-L195`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L183-L195) 描述的进程结构。
- `generate` 返回一个 dict，其中 `text` 字段是模型在 "Hello" 之后续写的内容。

**预期结果**：程序成功打印一段以 "Hello" 开头的续写文本，且无报错退出。说明环境可用。

> **待本地验证**：实际输出文本内容取决于你选的模型与权重，本讲无法预先给出确切字符串。如果你**没有 GPU 或没装好模型权重**，可把综合实践降级为「源码阅读型实践」：只完成步骤 1、2，然后对照 [`engine.py:L183-L195`](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/srt/entrypoints/engine.py#L183-L195) 写一段话，描述一次 `generate` 请求会依次穿过哪三个组件、它们分别在哪个进程里。这同样能达成本讲的认知目标。

---

## 6. 本讲小结

- SGLang 是一个**面向大语言模型/多模态模型的高性能推理服务框架**，目标是单卡到大规模集群的低延迟、高吞吐推理，同时也是主流 RL 框架的 rollout 后端。
- 代码在物理上分为**两层**：前端语言层 `lang/`（用 `@function/gen/select` 等 DSL 写生成程序）和运行时层 `srt/`（加载模型、调度、GPU 执行、KV 缓存、分布式等）。
- `import sglang` 执行的 `__init__.py` 就是公共 API 清单；其中 `Engine` 这个名字被前端和运行时各赋值一次，**最终生效的是运行时引擎** `sglang.srt.entrypoints.engine.Engine`。
- 运行时引擎由 **TokenizerManager（主进程）+ Scheduler（子进程）+ DetokenizerManager（子进程）** 三部分组成，进程间用 ZMQ 通信——这是第 3、4 单元的主线。
- `global_config` 存放了少量历史全局常量（verbosity、默认后端、前端解释器优化开关等），但**该文件已标记为计划废弃**，运行时配置正迁移到 `srt/environ.py` 的环境变量体系。
- `sglang.__version__` 由 `version.py` 的四级回退链动态决定，开发环境下出现 `0.0.0.dev0` 是正常的兜底行为，不代表安装损坏。

---

## 7. 下一步学习建议

本讲只建立了宏观认识，还没有动任何子系统的代码。建议按这个顺序继续：

1. **如果你想先把服务跑起来**：直接进入 **u1-l2（从零启动：安装与运行第一个推理服务）**，学习 `sglang serve` 命令行与 `launch_server.run_server` 的模式分发，把一个可接收 HTTP 请求的服务跑起来。
2. **如果你想先了解怎么发请求**：进入 **u1-l4（发送请求：OpenAI 兼容 API 与 Engine 嵌入式 API）**，对比 HTTP 与 `sglang.Engine` 两种入口。
3. **如果你对前端 DSL 感兴趣**：进入第 2 单元，从 **u2-l1（前端 DSL 基础）** 开始学 `@function/gen/select`。
4. **想立刻建立「目录地图」**：进入 **u1-l3（目录结构与代码地图）**，把 `srt/` 下 40 多个子系统目录的职责一次性梳理清楚，为后续每一讲定位「该去哪个目录读代码」打好基础。

> 推荐的最短路径：**u1-l1（本讲）→ u1-l3（目录地图）→ u1-l2（启动服务）→ u1-l4（发请求）**，先具备「能跑、能找」的能力，再进入第 3 单元的服务端架构与第 4 单元的调度核心。

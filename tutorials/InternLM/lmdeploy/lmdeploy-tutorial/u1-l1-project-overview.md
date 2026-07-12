# 项目总览与核心定位

## 1. 本讲目标

本讲是 LMDeploy 学习手册的**第一篇**，面向完全没接触过这个项目的读者。读完本讲，你应当能够：

- 用一句话说清楚 **lmdeploy 是什么、解决什么问题**；
- 列举 lmdeploy 的**四大核心能力**（高效推理、量化、服务、VLM/兼容）；
- 区分 **TurboMind 引擎**与 **PyTorch 引擎**这两条推理路线的定位差异，并知道它们各自大约何时诞生；
- 看懂仓库根目录 `README.md` 的结构，能独立从「Latest News / 最新进展」里读出项目特性演进的时间线。

本讲**不涉及任何源码细节**，只建立全局认知。从下一篇开始，我们才会逐步进入目录结构、配置、引擎源码。这样的顺序，是为了让你先有「地图」，再钻进每一条「街道」。

## 2. 前置知识

在开始前，最好对下面几个概念有最基本的了解（不知道也没关系，本讲会用通俗语言再解释一次）：

- **大语言模型（LLM）**：像 ChatGPT、Qwen、InternLM、Llama 这样的模型，输入一段文字，输出一段文字。它们体量很大（几十亿到几千亿参数），单靠「直接跑 PyTorch」往往又慢又费显存。
- **推理（Inference）**：模型训练好之后，拿去「使用」、给用户吐字的过程。本讲关心的是「如何把推理跑得又快又省」。
- **部署 / 服务（Serving）**：把推理能力包装成一个长期运行的服务（比如 HTTP API），让很多用户同时访问。
- **量化（Quantization）**：用更低精度（如 4bit）表示模型权重，换取更小体积、更快速度、更低显存，代价通常是极轻微的精度损失。
- **吞吐（Throughput）**：单位时间能处理的请求数或 token 数，是衡量推理效率的核心指标。

如果你已经知道「LLM 推理很吃资源、需要专门的优化工具」，那就足够开始本讲了。

## 3. 本讲源码地图

本讲只读「项目门面」类文件，它们位于仓库根目录：

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `README.md` | 英文版项目说明书：定位、特性、模型清单、安装、快速上手 | 提取项目定位与四大核心特性 |
| `README_zh-CN.md` | 中文版项目说明书，内容与英文版对应 | 提供中文表述，方便对照 |
| `CLAUDE.md` | 给开发者/AI 助手的项目架构速览（`Architecture` 章节） | 印证「两条后端、一个 Pipeline」这一架构主线 |

> 提示：本讲的「源码精读」引用的是这三份**说明性文档**。它们不是 `.py` 代码，但作为理解项目的「第一手事实来源」，比任何二手教程都权威——遇到分歧，以 `README.md` 为准。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. **4.1** lmdeploy 是什么——定位与职责范围
2. **4.2** 四大核心特性
3. **4.3** 两条推理引擎：TurboMind 与 PyTorch
4. **4.4** Latest News：特性演进时间线

### 4.1 lmdeploy 是什么——定位与职责范围

#### 4.1.1 概念说明

很多初学者会问：「我已经会用 HuggingFace `transformers` 跑模型了，为什么还要 lmdeploy？」

一句话回答：**`transformers` 负责「能跑」，lmdeploy 负责「跑得快、跑得省、能上线」。**

`README.md` 给出的官方定位是：lmdeploy 是一个面向 **LLM 的「压缩（compressing）、部署（deploying）、服务（serving）」一体化工具包**（toolkit），由 OpenMMLab 体系下的 [MMRazor](https://github.com/open-mmlab/mmrazor) 与 [MMDeploy](https://github.com/open-mmlab/mmdeploy) 团队联合开发。

注意三个关键词：**压缩、部署、服务**。它们恰好对应 LLM 从「训练好的原始模型」到「线上可用的服务」之间的完整链路：

- **压缩**：把动辄几十 GB 的浮点模型，量化成 4bit/8bit，体积更小、推理更快；
- **部署**：在一台或多台 GPU 机器上，把模型高效地「跑起来」（含多卡并行、显存管理）；
- **服务**：把推理能力暴露成 HTTP API（OpenAI 兼容），让前端/客户端能调用。

理解了这条链路，你就能把后续每一篇讲义对号入座：看到 `lmdeploy/lite/` 就知道是「压缩」，看到 `lmdeploy/turbomind/`、`lmdeploy/pytorch/` 就知道是「部署」，看到 `lmdeploy/serve/` 就知道是「服务」。

#### 4.1.2 核心流程

从用户视角，lmdeploy 把一条「原始模型 → 线上服务」的链路收敛成一个工具：

```text
HF 原始模型（FP16/BF16，又大又慢）
        │
        │  （可选）lmdeploy lite 量化：AWQ / GPTQ / KV Cache 量化
        ▼
量化/优化后的模型
        │
        │  lmdeploy 引擎：TurboMind 或 PyTorch
        │  - 持续批处理、Paged Attention、张量并行
        ▼
高效推理能力（单机/多卡）
        │
        │  lmdeploy serve：OpenAI 兼容 API / 多机代理
        ▼
对外的 HTTP 服务（多用户并发访问）
```

这条流程**并不强制每一步都走**。你完全可以跳过量化，直接用引擎跑原始模型；也可以只用 `pipeline` 做离线批处理，而不起服务。lmdeploy 的模块化设计让你按需取用。

#### 4.1.3 源码精读

`README.md` 开头的 `# Introduction` 一节，用四句加粗的要点概括了项目能力。我们先看它的总起句：

> LMDeploy is a toolkit for compressing, deploying, and serving LLM ...
> （LMDeploy 是一个用于压缩、部署和服务 LLM 的工具包）

这段话锁定了项目的三个职责维度，参见 [README.md:L96-L107](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README.md#L96-L107)（这一段同时包含下面 4.2 要讲的四条特性）。

中文表述在 `README_zh-CN.md` 的「简介」一节，措辞略有不同但含义一致：见 [README_zh-CN.md:L95-L107](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README_zh-CN.md#L95-L107)。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目的是让你亲手翻一遍 `README.md`，建立对项目定位的第一手印象。

1. **实践目标**：在仓库里定位「Introduction / 简介」段落，并用自己的话复述 lmdeploy 的职责。
2. **操作步骤**：
   - 打开仓库根目录的 `README.md`，找到第 96 行起的 `# Introduction` 段落。
   - 对比中文版 `README_zh-CN.md` 第 95 行起的 `# 简介` 段落。
   - 找到「压缩、部署、服务」三个词在两份文档中的英文/中文对应。
3. **需要观察的现象**：两份文档结构几乎一致，只是语言不同；英文版是「事实标准」，中文版用于辅助理解。
4. **预期结果**：你能合上文档，向同事用一句话讲清楚 lmdeploy 做什么。
5. 结果是否可运行：本步无需运行代码，属于阅读任务，无需「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：lmdeploy 官方定位里的三个职责词是哪三个？它们分别对应仓库里的哪个子包？

**参考答案**：压缩（compressing）→ `lmdeploy/lite/`；部署/推理（deploying）→ `lmdeploy/turbomind/` 与 `lmdeploy/pytorch/`；服务（serving）→ `lmdeploy/serve/`。

**练习 2**：如果只想「跑一次推理看看效果」，是否一定要走完压缩→部署→服务三步？

**参考答案**：不需要。压缩是可选的，服务也是可选的。最简方式是直接用 `lmdeploy.pipeline(...)` 跑离线推理，跳过量化与起服务这两步。

---

### 4.2 四大核心特性

#### 4.2.1 概念说明

`README.md` 在 Introduction 中用四条加粗要点列出了 lmdeploy 的核心能力。我们逐条翻译并解释：

1. **高效推理（Efficient Inference）**：通过持续批处理（continuous batching）、分块 KV 缓存（blocked KV cache）、动态拆分与融合（dynamic split & fuse）、张量并行（tensor parallelism）以及高性能 CUDA kernel，把吞吐做到 vLLM 的约 1.8 倍。
2. **可靠量化（Effective Quantization）**：支持权重量化（weight-only）与 K/V 缓存量化；4bit 推理速度约为 FP16 的 2.4 倍，且精度损失已通过 OpenCompass 评测验证可接受。
3. **便捷服务（Effortless Distribution Server）**：内置请求分发服务，支持在多机多卡上部署**多模型**推理服务。
4. **卓越兼容（Excellent Compatibility）**：KV Cache 量化、AWQ 量化、自动前缀缓存（Automatic Prefix Caching）这三者可以**同时启用**，不必二选一。

这些术语现在看着陌生没关系，后面每个特性都有专门的讲义深入。这里只要建立一个**关键词清单**即可。

#### 4.2.2 核心流程

理解这四条特性的一个方式，是看它们分别解决了 LLM 落地时的哪类「痛点」：

| 痛点 | 对应特性 | 一句话效果 |
| --- | --- | --- |
| 推理慢、并发低 | 高效推理 | 同样的卡，吞吐翻倍 |
| 模型太大、显存不够 | 可靠量化 | 4bit 后体积/显存大降，速度反升 |
| 要上线、要多模型 | 便捷服务 | 一套服务管多机多卡多模型 |
| 量化与缓存「打架」 | 卓越兼容 | 三大优化可同时用，不互相排斥 |

至于「1.8 倍」「2.4 倍」这类数字，可以用一个简单的比值表达。以吞吐为例，若 vLLM 在相同硬件下的吞吐为 \(T_{\text{vLLM}}\)，则 lmdeploy 声称

\[
T_{\text{lmdeploy}} \approx 1.8 \times T_{\text{vLLM}}
\]

注意：这是**官方在特定模型/硬件上的基准结果**，不是普适常数，实际数字随模型、batch、显卡而变。

#### 4.2.3 源码精读

四条特性原文见 [README.md:L100-L106](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README.md#L100-L106)。对应中文版见 [README_zh-CN.md:L100-L106](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README_zh-CN.md#L100-L106)。

举一行来对照阅读——高效推理这条，英文原文是：

```text
- **Efficient Inference**: LMDeploy delivers up to 1.8x higher request throughput than vLLM,
  by introducing key features like persistent batch(a.k.a. continuous batching),
  blocked KV cache, dynamic split&fuse, tensor parallelism, high-performance CUDA kernels and so on.
```

这里出现了后面会反复出现的术语 **persistent batch（即 continuous batching，持续批处理）** 与 **blocked KV cache（分块 KV 缓存，对应 Paged Attention 思想）**。先记住这两个词，本手册 U4「调度器」与「BlockManager」会专门讲它们。

中文版同义表述见 [README_zh-CN.md:L100](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README_zh-CN.md#L100)。

#### 4.2.4 代码实践

1. **实践目标**：把四条特性从 README「抠」出来，整理成自己的速查表。
2. **操作步骤**：
   - 打开 [README.md:L96-L107](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README.md#L96-L107)。
   - 新建一个本地笔记文件（任意位置，不影响项目），用一张四行表格记下：特性名、解决什么痛点、关键术语、官方声称的数字。
3. **需要观察的现象**：你会发现第四条「卓越兼容」列出了 KV Cache Quant、AWQ、Automatic Prefix Caching 三个可同时启用的能力——这是 lmdeploy 区别于很多「二选一」框架的卖点。
4. **预期结果**：笔记里有一张 4 行 × 4 列的表，能脱离文档复述。
5. 结果是否可运行：纯阅读与记录，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：「持续批处理」和「blocked KV cache」分别对应英文里的哪两个词？它们大致解决什么问题？

**参考答案**：分别对应 *persistent batch (continuous batching)* 与 *blocked KV cache*。前者解决「请求来了就立刻并入当前批次、请求结束就立刻让出资源」的动态调度问题，提升并发；后者解决「KV 缓存按固定大小分块存储/按需分配」的显存碎片问题，是 Paged Attention 的核心思想。

**练习 2**：lmdeploy 说 4bit 推理比 FP16 快多少？这个数字需要无条件相信吗？

**参考答案**：官方声称约 **2.4 倍**（见 README 高效量化一条）。这是特定基准下的结果，实际取决于模型、显卡、batch size 等，不应当成普适常数。

---

### 4.3 两条推理引擎：TurboMind 与 PyTorch

#### 4.3.1 概念说明

lmdeploy 内部**不是一套推理实现，而是两套**，这是初学者最容易混淆的点。`README.md` 明确写道：

> LMDeploy has developed two inference engines - TurboMind and PyTorch, each with a different focus.
> The former strives for ultimate optimization of inference performance,
> while the latter, developed purely in Python, aims to decrease the barriers for developers.

| 引擎 | 实现语言 | 定位 | 适合谁 |
| --- | --- | --- | --- |
| **TurboMind** | C++（通过 pybind 暴露给 Python） | 追求**极致推理性能** | 生产上线、追求吞吐 |
| **PyTorch** | 纯 Python | **降低开发门槛**，便于快速实验新特性 | 开发者、研究者、二次开发 |

两套引擎不是「新旧替代」关系，而是**并存互补**。它们在支持的模型类别、推理数据类型（精度）上有差别，选哪个要看你的模型和需求。

> 名词解释：**pybind** 是一种让 C++ 代码可以被 Python 调用的桥接技术。TurboMind 的核心是 C++，但你仍能用 Python 调用它，靠的就是 pybind（仓库里对应 `lmdeploy/lib/_turbomind`）。这部分细节留到 U6 TurboMind 单元讲。

#### 4.3.2 核心流程

从架构上看，两套引擎共用一个**统一的对外入口 `pipeline`**。`CLAUDE.md` 的架构章节用一句话点破了主线——「Two Backends, One Pipeline（两条后端，一个 Pipeline）」：

```text
用户调用 pipeline(...)  ──┐
                          │  根据「模型架构 + 配置」自动判断
                          ▼
              ┌──────────────────────────┐
              │  走 TurboMind ？ 走 PyTorch？│
              └──────────────────────────┘
                  │                  │
                  ▼                  ▼
        lmdeploy/turbomind/   lmdeploy/pytorch/
        （C++ 高性能）        （纯 Python，易扩展）
```

也就是说，对普通用户而言，调用方式是**统一的**（都从 `pipeline` 进），差异被封装在内部。具体怎么自动判断后端，是 U2「架构注册 archs.py」、U3「Pipeline 如何选择后端」的内容，这里只需知道「有两套引擎、入口统一」。

#### 4.3.3 源码精读

两套引擎的官方对比说明，见 [README.md:L203-L205](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README.md#L203-L205)，中文版见 [README_zh-CN.md:L205-L207](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README_zh-CN.md#L205-L207)。

「两条后端、一个 Pipeline」这条架构主线，在 `CLAUDE.md` 里被点明：见 [CLAUDE.md:L37-L39](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/CLAUDE.md#L37-L39)，其中第 39 行写道：

```text
`lmdeploy/pipeline.py` is the main user-facing entry point (`pipeline()` in `api.py`).
It instantiates either the PyTorch engine (`lmdeploy/pytorch/`)
or the TurboMind engine (`lmdeploy/turbomind/`) based on config.
```

这段话定位了三个关键文件，记下来对后续阅读很有用：

- `lmdeploy/api.py` —— 提供 `pipeline()` 工厂函数；
- `lmdeploy/pipeline.py` —— 用户侧统一入口，决定走哪个引擎；
- `lmdeploy/pytorch/` 与 `lmdeploy/turbomind/` —— 两条引擎实现。

#### 4.3.4 代码实践

这是本讲的**主实践任务**之一：找出两条引擎各自的「首发时间」与「一句话定位」，并整理成笔记。

1. **实践目标**：从 README 的 Latest News 里定位 TurboMind 与 PyTorch 引擎各自首次出现的条目。
2. **操作步骤**：
   - 在 `README.md` 中展开 `## Latest News 🎉` 区块，按年份从下往上（由早到晚）阅读。
   - 找到 **TurboMind 首次出现**的条目：[README.md:L90](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README.md#L90) ——「[2023/07] TurboMind supports tensor-parallel inference of InternLM」。
   - 找到 **PyTorch 引擎首次出现**的条目：[README.md:L68](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README.md#L68) ——「[2024/01] Support PyTorch inference engine, developed entirely in Python」。
   - 中文版对应：[README_zh-CN.md:L90](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README_zh-CN.md#L90) 与 [README_zh-CN.md:L68](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README_zh-CN.md#L68)。
3. **需要观察的现象**：TurboMind 比 PyTorch 引擎早出现了约半年，TurboMind 是项目最早的引擎，PyTorch 引擎是「作为补充」后加进来的（中文版原话：「作为 TurboMind 引擎的补充」）。
4. **预期结果**：得到一张这样的小笔记：

   | 引擎 | 首发时间 | 一句话定位 |
   | --- | --- | --- |
   | TurboMind | 2023/07（先） | C++ 高性能后端，追求极致推理性能 |
   | PyTorch | 2024/01（后） | 纯 Python 引擎，降低开发门槛，便于实验新特性 |

5. 结果是否可运行：纯阅读任务，无需运行代码。

#### 4.3.5 小练习与答案

**练习 1**：两条引擎里，哪一条是「纯 Python」？为什么要保留另一条 C++ 引擎？

**参考答案**：**PyTorch** 引擎是纯 Python。保留 C++ 的 TurboMind 是为了在**生产场景下追求极致推理性能**（吞吐、延迟、显存效率通常优于纯 Python）。两者互补：PyTorch 易扩展、易上手，TurboMind 性能强。

**练习 2**：用户调用 `lmdeploy.pipeline(...)` 时，需要自己手动指定走哪个引擎吗？

**参考答案**：可以指定，也可以不指定。`pipeline.py` 会根据「模型架构 + 引擎配置」自动判断后端（详见后续 U2 archs.py、U3 后端选择）。所以对外是「一个入口」，对内是「两套引擎」。

---

### 4.4 Latest News：特性演进时间线

#### 4.4.1 概念说明

`README.md` 顶部有一个 `## Latest News 🎉`（中文版 `## 最新进展 🎉`）区块，按时间倒序列出项目的重要特性更新。对一个初学者来说，这条时间线是**最高效的「项目全景速览」**——花两分钟扫一遍，就能知道 lmdeploy 这两年到底加了多少东西。

读时间线有一个小技巧：**从最老的年份往新读**（也就是网页上从下往上），你会清楚地看到项目「从单引擎到双引擎、从纯文本到多模态、从单机到多机分离」的演化脉络。

#### 4.4.2 核心流程

下面是从 README Latest News 中提炼的关键节点（年份/月份尽量保留原始表述）：

```text
2023/07  TurboMind 首发：支持 InternLM 的张量并行推理
2023/08  TurboMind 支持 4bit 推理（声称比 FP16 快 2.4x）、引入 AWQ 量化
2023/11  TurboMind 重磅升级：Paged Attention、Flash Decoding(Split-K)、KV8 kernel
2024/01  ★ PyTorch 推理引擎加入（纯 Python，作为 TurboMind 的补充）
2024/01  支持多模型/多机/多卡推理服务（proxy）
2024/03  支持 VLM（视觉语言模型）的离线推理 pipeline 与服务
2024/09  PyTorch 引擎支持华为 Ascend 平台；引入 CUDA Graph 加速 Llama3-8B
2025/01  支持 DeepSeek V3 / R1
2025/04  集成 deepseek-ai 组件（FlashMLA、DeepGemm、DeepEP、MicroBatch、eplb）
2025/06  DeepSeek PD 分离部署（集成 DLSlime + Mooncake）
2025/09  TurboMind 支持 MXFP4（NVIDIA V100 起）
2026/02  支持 Qwen3.5
2026/04  v0.12.3 重新上架 PyPI，pip install lmdeploy 恢复
```

这条脉络对应了本手册后续多个单元：

- 「Paged Attention」→ U4 调度器、U9 前缀缓存；
- 「多机多卡 / 张量并行」→ U9 张量并行；
- 「VLM」→ U9 视觉语言模型；
- 「PD 分离」→ U9 PD 分离部署；
- 「量化」→ U7 Lite 量化压缩。

也就是说，Latest News 不只是「新闻」，它还是**学习路线的导航图**。

#### 4.4.3 源码精读

Latest News 区块入口见 [README.md:L24-L33](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README.md#L24-L33)（2026 年区块默认展开）。历史年份（2025 / 2024 / 2023）放在可折叠的 `<details>` 区块里：

- 2024 年区块：[README.md:L46-L70](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README.md#L46-L70)，其中第 68 行是 PyTorch 引擎首发条目。
- 2023 年区块：[README.md:L72-L92](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README.md#L72-L92)，其中：
  - 第 77 行是 TurboMind 重磅升级（Paged Attention）条目；
  - 第 90 行是 TurboMind 首发条目。

中文版「最新进展」对应入口见 [README_zh-CN.md:L24-L33](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README_zh-CN.md#L24-L33)。

#### 4.4.4 代码实践

1. **实践目标**：亲手从 Latest News 里抽出「双引擎诞生」与「多模态 / PD 分离 / 量化」三类关键节点。
2. **操作步骤**：
   - 打开 [README.md:L72-L92](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README.md#L72-L92)，确认 TurboMind 在 2023 年的关键里程碑。
   - 打开 [README.md:L46-L70](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README.md#L46-L70)，找到 PyTorch 引擎、VLM、多模型服务这三类条目。
   - 把三类节点按时间填进 4.4.2 的时间线里。
3. **需要观察的现象**：多模态（VLM）相关特性主要集中在 2024 年之后；2025 年的重点明显转向 DeepSeek 系大模型与 PD 分离。
4. **预期结果**：得到一条带年份标注的演进时间线笔记。
5. 结果是否可运行：纯阅读任务，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：TurboMind 引入 Paged Attention 是在什么时候？这条特性在本手册后续哪一单元深入？

**参考答案**：2023/11（见 [README.md:L77](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README.md#L77)）。在本手册 U4「分块 KV 缓存与 BlockManager」、U9「Prefix 缓存与 BlockTrie」会深入。

**练习 2**：从时间线看，lmdeploy 在 2025 年的重点方向是什么？

**参考答案**：以 DeepSeek V3/R1 为代表的大模型优化——包括集成 FlashMLA/DeepGemm/DeepEP/MicroBatch/eplb 等组件提升性能（2025/04）、FP8 MoE 深度优化（2025/06）、以及通过 DLSlime + Mooncake 实现 PD 分离部署（2025/06）。

---

## 5. 综合实践

把本讲学到的定位、特性、双引擎、时间线串起来，完成下面这个综合小任务——**写一份「lmdeploy 一页速览」笔记**。

**任务要求**：

1. 用一句话写出 lmdeploy 的定位（参考 4.1）。
2. 列出四大核心特性，并各配一个关键词（参考 4.2）。
3. 用一张表对比 TurboMind 与 PyTorch 两条引擎，包含：实现语言、定位、首发时间（参考 4.3）。
4. 从 Latest News 里挑出**你认为最影响后续学习的 3 个里程碑**，写出时间和理由（参考 4.4）。
5. 在本地（如条件允许）执行安装：

   ```shell
   conda create -n lmdeploy python=3.12 -y
   conda activate lmdeploy
   pip install lmdeploy
   ```

   安装命令出自 [README.md:L211-L219](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README.md#L211-L219)、中文版 [README_zh-CN.md:L211-L219](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/README_zh-CN.md#L211-L219)。官方说明：自 v0.13.0 起，PyPI 默认预编译 wheel 基于 CUDA 12.8 构建，一般用户（含 RTX 50 系列）直接 `pip install lmdeploy` 即可。

**验收标准**：

- 笔记 1～4 条都能脱离 README 独立完成；
- 第 5 条：如果你的机器有 NVIDIA GPU 且网络通畅，安装后应能 `python -c "import lmdeploy; print(lmdeploy.__version__)"` 成功打印版本号；若你的环境无 GPU 或无法联网，请**如实标注「待本地验证」**，不要假装已安装成功。

> 提示：本讲不要求你真正跑推理（那是下一篇 `u1-l4 pipeline 快速上手` 的任务）。本讲的安装只是为后续做铺垫。

## 6. 本讲小结

- lmdeploy 是一个 **LLM 压缩—部署—服务一体化**工具包，三个职责分别对应 `lite/`、`turbomind/`+`pytorch/`、`serve/` 子包。
- 四大核心特性：**高效推理、可靠量化、便捷服务、卓越兼容**；其中持续批处理与分块 KV 缓存是后续会反复出现的两个关键词。
- 项目有**两套并存互补的引擎**：TurboMind（C++，追求极致性能，2023/07 首发）与 PyTorch（纯 Python，降低开发门槛，2024/01 首发）。
- 对外入口统一：用户调 `lmdeploy.pipeline(...)`，内部再决定走哪个引擎，这就是「**两条后端，一个 Pipeline**」。
- `README.md` 的 **Latest News** 不只是新闻，更是项目演进导航图：Paged Attention（U4/U9）、VLM（U9）、张量并行（U9）、PD 分离（U9）、量化（U7）都能在时间线上找到起点。
- 遇到表述分歧，以英文版 `README.md` 为事实标准，中文版用于辅助理解。

## 7. 下一步学习建议

本讲建立了「全局认知」，下一步建议：

1. **紧接着读 `u1-l2 目录结构与架构全景`**：把 `lmdeploy` 包内子目录的职责、`api.py`/`pipeline.py`/`__init__.py` 的入口关系搞清楚，把本讲讲的「两条后端一个 Pipeline」落到具体目录上。
2. 然后 `u1-l3 安装与构建方式`：理解 `setup.py` + CMake 如何构建 TurboMind C++ 扩展，以及 `requirements_cuda.txt` 等按设备拆分的依赖。
3. 之后 `u1-l4 pipeline 推理快速上手`：亲手用 `pipeline()` 跑通第一次推理，把本讲「装好了」变成「跑起来了」。
4. 想提前感受双引擎差异的，可以在读 U3（PyTorch 后端）和 U6（TurboMind 后端）前，先去 `docs/en/inference/` 下读官方的 `pytorch.md` 与 `turbomind.md` 两篇文档对照。

> 本讲的全部结论都来自 `README.md` / `README_zh-CN.md` / `CLAUDE.md`，没有引入任何源码逻辑。从下一篇起，我们才真正开始读 `.py` 代码。

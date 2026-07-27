# 讲义标题：项目总览与定位 —— ncnn_llm 是什么

> 本讲是《ncnn_llm 学习手册》的第一篇（u1-l1），属于**入门层（beginner）**。
> 本讲不要求你已经读过任何一行项目源码，也不要求你会构建运行。
> 学完本讲，你将能向别人用三句话讲清楚「ncnn_llm 是什么、解决什么问题、为什么建立在学习路线的最前面」。

---

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚 **ncnn_llm 的定位**：它是一个建立在 [ncnn](https://github.com/Tencent/ncnn) 之上的轻量级 C++ 推理运行时，统一支持文本对话、图像理解、OCR、语音识别、翻译和向量嵌入等多种 AI 能力。
2. 说出项目的 **起源**（源自 nihui 对 ncnn `kvcache` 的实验性工作）与 **目标场景**（边缘设备 / 桌面 CPU / 支持 Vulkan 的 GPU）。
3. 列出项目 **支持的全部模型类别与代表模型**，并能把每一类对应到「输入 → 输出」的高层管线。
4. 用一句话理清 **ncnn_llm 与底层 ncnn 的关系**，明白「谁是底座、谁做上层调度」。

本讲只读、不写、不跑模型。后续讲义（u1-l2）才会带你真正构建并运行项目。

---

## 2. 前置知识

为了让你从零开始也能读懂，下面几个名词在本讲里反复出现，先给一个通俗解释：

| 名词 | 通俗解释 |
| --- | --- |
| **推理（inference）** | 用已经训练好的模型去「算出结果」。训练是「学习」，推理是「考试答题」。ncnn_llm 只做推理，不做训练。 |
| **LLM**（大语言模型） | 输入一段文字，输出一段文字，例如聊天机器人。 |
| **VLM**（视觉语言模型） | 既能看图又能读字，输入「图片 + 文字提问」，输出文字回答。 |
| **OCR** | 把图片里的文字「读」成可编辑的文本。 |
| **ASR** | 把声音（语音）转成文字。 |
| **嵌入（embedding）** | 把一段文字或一张图片变成一串数字（向量），用来度量「相似度」。比如搜索、去重、图文匹配。 |
| **ncnn** | 腾讯开源的**神经网络推理框架**，专注于在手机、PC、嵌入式等「算力有限」的设备上又快又小地跑神经网络。它是 ncnn_llm 的「底座」。 |
| **Vulkan** | 一个跨平台的 GPU 计算接口。ncnn 可以用它让模型在 GPU 上跑得更快。 |
| **KV cache** | 自回归模型（如 LLM）逐字生成时，把每一步的 Key/Value 中间结果「记」下来，下一步直接复用，避免重复计算。这是 ncnn_llm 性能的关键。 |
| **xmake** | 一个国产的构建工具，类似 CMake/Meson，本项目用它来管理编译。 |

如果上面有些名词现在还不够清楚也没关系——本讲的重点是「看清项目全貌」，细节会在后续讲义里逐层展开。

---

## 3. 本讲源码地图

本讲是「总览」，主要阅读**文档与配置类文件**，不深入 C++ 实现。涉及的关键文件如下：

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `readme.md` | 项目英文说明：定位、特性、支持模型、快速开始、项目结构 | 提取项目定位、特性清单、模型表、与 ncnn 的关系 |
| `README_CN.md` | 项目中文说明，内容与英文版对应 | 对照中文表述，方便初学者准确理解 |
| `LICENSE` | 开源许可证（Apache-2.0） | 确认项目可自由使用、修改、分发的边界 |
| `xmake.lua`（补充） | 构建配置 | 直观看到 `add_requires("ncnn master")` 这一依赖声明，佐证「ncnn 是底座」 |

> 说明：本讲把 `xmake.lua` 作为**补充引用**（不在讲义规格的关键源码列表里，但与「ncnn 后端关系」直接相关，且确实存在于仓库），目的是让你在源码层面、而不只是文档层面，亲眼看到这种依赖关系。后续讲义（u1-l2）会专门逐行讲解它。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 项目定位与特性清单** —— ncnn_llm 是什么、解决什么问题。
- **4.2 支持模型表** —— 它到底能跑哪些模型，每一类的输入输出是什么。
- **4.3 ncnn 后端关系** —— 它和底层 ncnn 是什么关系，谁是地基、谁是楼。

---

### 4.1 项目定位与特性清单

#### 4.1.1 概念说明

先回答最根本的问题：**ncnn_llm 是什么？**

一句话：**ncnn_llm 是一个用 C++ 写的「模型推理运行时（runtime）」，它把已经训练好的大模型，在 ncnn 这个推理引擎上跑起来，让你能在普通电脑、甚至边缘设备上本地完成对话、看图、OCR、语音识别、翻译、算向量等工作。**

这里有几个关键词，逐个解释：

- **「运行时」而不是「框架」**：ncnn_llm 自己不去定义「什么是神经网络层」，这件事交给底层的 ncnn。ncnn_llm 做的是「把一类模型的完整推理流程组织起来」——怎么分词、怎么准备位置编码、怎么一步步生成下一个词、怎么管理 KV cache。它更像一个**针对这些模型家族的上层调度程序**。
- **「本地（local）」**：模型就在你自己的机器上跑，不需要联网，不需要调用云端 API。这对隐私、离线、边缘部署很重要。
- **「轻量级」**：目标是能跑在算力和内存受限的设备上（手机、嵌入式、低配 PC），而不是依赖数据中心的大显卡。

为什么要造它？因为现在主流的大模型推理框架（如 vLLM、transformers）大多面向服务器、面向 GPU，又重又大；而 ncnn 长期在「端侧轻量推理」上做得很好，但缺一套「专门把现代 LLM/VLM 这一类自回归模型跑起来、并管理好 KV cache」的上层封装。**ncnn_llm 就是来补这一层的。**

它的官方定位原文如下（英文版）：

> `ncnn_llm` provides a lightweight C++ runtime for running language models and embedding models with ncnn. It focuses on practical local inference for edge devices, desktop CPU, and Vulkan-capable GPUs.

中文版（README_CN）对应表述：

> `ncnn_llm` 提供一个轻量级 C++ 运行时，用于在 ncnn 上运行语言模型和嵌入模型。项目关注可落地的本地推理场景，包括边缘设备、桌面 CPU 和支持 Vulkan 的 GPU。

#### 4.1.2 核心流程

从「使用者」的视角，ncnn_llm 把一切推理都抽象成同一条高层管线：

```text
   输入 (Input)                    处理 (ncnn_llm 运行时)                  输出 (Output)
┌───────────────────┐         ┌──────────────────────────────┐         ┌──────────────────┐
│ 文本 prompt        │         │  分词 tokenizer               │         │ 文本回答 (对话)   │
│ 图像 image         │ ──────▶ │  ↓                            │ ──────▶ │ OCR 文本         │
│ 音频 audio (wav)   │         │  位置编码 / KV cache 调度      │         │ ASR 转写文本     │
└───────────────────┘         │  ↓                            │         │ 翻译文本         │
                              │  ncnn 神经网络前向计算          │         │ 向量 embedding   │
                              │  ↓                            │         └──────────────────┘
                              │  采样 / 后处理                 │
                              └──────────────────────────────┘
```

要点：

1. **输入**只有三类：文本（prompt）、图像（image）、音频（wav）。
2. **输出**只有两类：文本（对话/OCR/ASR/翻译）、向量（嵌入）。
3. 中间的「运行时」是统一的一套，靠 `model.json` 配置去切换到底层跑哪种模型——这就是后续你会反复看到的「**一套共享运行时驱动多种模型家族**」的设计主线。

#### 4.1.3 源码精读

定位说明在 README 的开头，紧随标题和徽章之后：

[readme.md:L30-L32](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L30-L32) —— 项目自我介绍：lightweight C++ runtime，基于 ncnn，面向边缘设备/桌面 CPU/Vulkan GPU。

[README_CN.md:L30-L32](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/README_CN.md#L30-L32) —— 中文版的同一段定位，并写明了**项目起源**：源自 nihui 对 ncnn `kvcache` 的实验性工作，并扩展出可复用的示例、模型加载、分词器、视觉预处理、OCR 推理和嵌入 API。

特性清单（Highlights）一节列出了项目的九条能力，这是你理解「这个项目能干什么」最快的入口：

[readme.md:L34-L44](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L34-L44) —— 九条特性：统一 CLI、KV cache 自回归解码（CPU/Vulkan）、Qwen/MiniCPM 风格 LLM、Qwen VL 图文、GLM-OCR、NLLB 翻译、文本与多模态嵌入、BPE/Unigram 分词器、xmake 构建。

中文对应在 [README_CN.md:L34-L44](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/README_CN.md#L34-L44)。

> 关键提炼：这九条特性其实已经在预告整本学习手册的结构——「统一 CLI / 共享解码」是主干（U2 单元），「Qwen VL / GLM-OCR / NLLB / 嵌入」是各模态分支（U5、U6 单元），「BPE/Unigram 分词器」是子系统（U3 单元）。**读懂这九条，等于读到了后续整本手册的目录。**

许可证方面，项目采用 Apache License 2.0：

[LICENSE:L1-L4](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/LICENSE#L1-L4) —— 开头即声明 Apache License 2.0。这意味着你可以自由使用、修改、分发（含商用），但需保留版权与许可证声明。对学习者而言：可以放心读源码、改源码做实验。

#### 4.1.4 代码实践

这是一个**阅读 + 画图型实践**，目的是把「定位」内化成你自己的理解。

1. **实践目标**：用自己的话写出 ncnn_llm 的定位，并画出高层管线图。
2. **操作步骤**：
   - 打开 [readme.md](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md) 和 [README_CN.md](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/README_CN.md)。
   - 阅读 `Highlights`（特性）一节（readme.md 第 34–44 行）。
   - 在你的笔记里，用**不超过三句话**写出「ncnn_llm 是什么、面向什么场景、和 ncnn 什么关系」。
3. **需要观察的现象**：注意英文版和中文版的特性条目是**一一对应**的，只是语言不同。
4. **预期结果**：你应当能写出类似这样的一句话定位——「ncnn_llm 是一个基于 ncnn 的轻量级 C++ 推理运行时，把对话、看图、OCR、ASR、翻译、嵌入等能力在端侧本地跑起来」。
5. 画图：照着本讲 4.1.2 的高层管线，自己手画一遍「输入(prompt/图像/音频) → 运行时 → 输出(文本/嵌入)」，加深印象。

> 本步骤无需运行任何命令，纯阅读即可完成。

#### 4.1.5 小练习与答案

**练习 1**：ncnn_llm 是「训练框架」还是「推理运行时」？为什么？

> **参考答案**：是**推理运行时**。它不负责训练模型，只负责把已经训练好、并转换成 ncnn 格式的模型在本地上跑出结果。文档里反复出现 "runtime" 和 "inference"，而不是 "training"。

**练习 2**：列出 Highlights 里至少三条「面向开发者的能力」和一条「面向最终用户的能力」。

> **参考答案**：面向开发者的能力如——统一 CLI runner、KV cache 自回归解码、文本/多模态嵌入 API、BPE/Unigram 分词器支持、xmake 构建；面向最终用户的能力是——可以直接用 `llm_ncnn_run` 这个交互式命令行进行聊天对话。

---

### 4.2 支持模型表

#### 4.2.1 概念说明

ncnn_llm 的一大卖点是「**统一**」：用一个项目跑多种模型家族。理解「支持哪些模型」，本质上是在理解「这个项目覆盖了哪些 AI 能力」。

README 用一张表把支持的模型按**类别（Category）**组织。先理解每个类别的含义（与第 2 节的前置知识呼应）：

| 类别 | 全称 | 输入 | 输出 | 通俗场景 |
| --- | --- | --- | --- | --- |
| **LLM** | 大语言模型 | 文本 | 文本 | 聊天、问答、续写 |
| **VLM** | 视觉语言模型 | 图像 + 文本 | 文本 | 看图回答问题 |
| **OCR** | 光学字符识别 | 图像 | 文本 | 把图片里的字读出来 |
| **ASR** | 自动语音识别 | 音频 | 文本 | 语音转文字 |
| **Translation** | 机器翻译 | 文本 | 文本 | 中英互译等 |
| **Embedding** | 向量嵌入 | 文本 / 图像 | 向量 | 语义搜索、图文检索、相似度计算 |

注意两个容易混的点：

- **LLM 和 Translation 都输入文本、输出文本**，区别在于翻译是 encoder-decoder 架构、面向「把一种语言翻成另一种」，而 LLM 通常是 decoder-only、面向开放式对话生成。
- **VLM 和 OCR 都吃图像**，区别在于 VLM 是「理解图像并自由对话」，OCR 是「专门把图像里的文字提取成纯文本」。

#### 4.2.2 核心流程

把上一节的「支持模型表」与第 4.1.2 的高层管线对应起来，就能看清「每种模型走管线的哪一段」：

```text
类别            输入                  共享运行时（按 model.json 切换）        输出
─────────────────────────────────────────────────────────────────────────────
LLM             文本 prompt    ──▶    分词 + RoPE + decoder(KV) + 采样  ──▶  文本
VLM             图像 + 文本    ──▶    图像预处理 + 视觉编码 + 注入 + decoder ──▶ 文本
OCR             图像           ──▶    图像 prefill + 共享文本解码        ──▶  文本（纯文字）
ASR             音频 wav       ──▶    mel 频谱 + 音频编码 + 共享 decoder ──▶  文本（转写）
Translation     文本           ──▶    encoder + cross-attention decoder ──▶  文本（译文）
Embedding       文本 / 图像    ──▶    encoder + mean pool + normalize   ──▶  向量
```

关键设计点（也是后续整本手册的主线）：**OCR / ASR / VLM 都「复用」同一套共享文本解码运行时**，只是在前端（图像/音频如何变成 token 嵌入）各做各的处理。这就是 README 路线图第一条所说的「Keep decoder and KV-cache runtime shared across model families」（在不同模型族之间共享 decoder 与 KV cache 运行时）。

#### 4.2.3 源码精读

支持模型表是 README 的核心内容之一：

[readme.md:L46-L60](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L46-L60) —— `## Supported Models` 表格，按类别（LLM / VLM / OCR / ASR / Translation / Embedding）列出每个模型及其状态与说明。

中文对应在 [README_CN.md:L46-L60](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/README_CN.md#L46-L60)。

把表里读到的模型，按类别整理如下（共 **6 大类、11 个模型**）：

| 类别 | 模型 | 维度/说明 |
| --- | --- | --- |
| LLM | YoutuLLM / MiniCPM4 / Qwen3 | 聊天 / 文本生成 |
| VLM | Qwen3.5 / Qwen2.5-VL | 图像 + 文本输入 |
| OCR | GLM-OCR / HunyuanOCR | 图像转文字 |
| ASR | Qwen3 ASR | 语音转文字 |
| Translation | NLLB | 翻译 |
| Embedding | Jina-Embeddings-v5-Text-Nano（768 维文本）/ Jina-CLIP-v2（1024 维文本+图像） | 向量嵌入 |

「路线图（Roadmap）」一节进一步印证了「共享运行时」这条主线：

[readme.md:L286-L292](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L286-L292) —— Roadmap：保持 decoder/KV cache 跨模型族共享、扩展更多架构与分词器、提升 Vulkan/CPU 性能、增加 INT8 量化、完善导出文档。

此外，README 的「Other Examples」表把每个示例程序（target）和它的用途对应起来，能帮你把「模型类别」与「可执行程序」挂钩：

[readme.md:L196-L205](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L196-L205) —— 各 target 用途：`llm_ncnn_run`（统一聊天/VL CLI）、`ocr_main`（GLM-OCR）、`embedding_main`（文本嵌入）、`clip_main`（CLIP 图文嵌入）、`nllb_main`（翻译）、`unigram_main`（分词器示例）、`benchllm`（基准测试）、`test_llm`（单元测试）。

#### 4.2.4 代码实践

这是一个**对照阅读型实践**，目的是让你把「模型类别」与「示例程序」对上号。

1. **实践目标**：列出项目支持的**全部模型类别**，并为每个类别指出它对应的示例 target。
2. **操作步骤**：
   - 读 [readme.md:L46-L60](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L46-L60) 的支持模型表。
   - 再读 [readme.md:L196-L205](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L196-L205) 的 Other Examples 表。
   - 在笔记里画一张两列对照表：左列写「模型类别」，右列写「用来跑它的示例 target」。
3. **需要观察的现象**：注意 LLM 和 VLM 共用同一个 `llm_ncnn_run`（因为它们都是「对话型」，只是 VLM 多了 `--image`），而 OCR/嵌入/翻译/分词器各有独立的 `*_main` 程序。
4. **预期结果**：得到类似下面的对照表。

| 模型类别 | 示例 target |
| --- | --- |
| LLM / VLM | `llm_ncnn_run`（VLM 多加 `--image`） |
| OCR | `ocr_main` |
| ASR | `asr_main` |
| Translation | `nllb_main` |
| Embedding（文本） | `embedding_main` |
| Embedding（CLIP 图文） | `clip_main` |
| 分词器示例 | `unigram_main` |

> 注意：`asr_main` 这个 target 出现在 `xmake.lua`（见 4.3.3），但 README 的 Other Examples 表里**尚未列出**。这属于文档与构建配置的细微出入，真实运行行为以本地构建为准（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：支持模型表里共有几个「类别」？分别是什么？

> **参考答案**：共 **6 个类别**：LLM、VLM、OCR、ASR、Translation、Embedding。（不是 6 个模型——模型有 11 个，类别是 6 个。）

**练习 2**：VLM 和 OCR 都吃图像，它们的本质区别是什么？

> **参考答案**：VLM（如 Qwen2.5-VL）是「看图自由对话」，输入图像+问题、输出开放式文本回答；OCR（如 GLM-OCR）是「专门提取图像中的文字」，输出是相对纯净的文字内容。两者复用同一套文本解码运行时，但前端处理和目标不同。

**练习 3**：从 Roadmap 看，ncnn_llm 最坚持的一条设计原则是什么？

> **参考答案**：**保持 decoder 和 KV cache 运行时在不同模型族之间共享**（Keep decoder and KV-cache runtime shared across model families）。这是整个项目架构的核心理念。

---

### 4.3 ncnn 后端关系

#### 4.3.1 概念说明

理解 ncnn_llm，必须先理解它和 **ncnn** 的关系。打个比方：

- **ncnn 是「发动机」**：它是一个成熟的神经网络推理引擎，知道怎么加载网络、怎么在 CPU/GPU 上高效计算每一层（卷积、矩阵乘、注意力……）。
- **ncnn_llm 是「整车与驾驶系统」**：它不重新发明发动机，而是在 ncnn 之上，针对「大模型这一类自回归模型」组织出完整的推理流程——怎么分词、怎么算位置编码、怎么一步步生成下一个 token、怎么管理 KV cache 让生成又快又不重复计算。

所以一句话总结它们的关系：

> **ncnn 是底层的通用神经网络推理引擎；ncnn_llm 是构建在 ncnn 之上、专门面向 LLM/VLM/OCR/ASR/翻译/嵌入的「模型推理运行时」。ncnn_llm 离不开 ncnn，但 ncnn 并不专门懂「大模型生成」。**

为什么这个区分重要？因为它决定了你以后读源码时的「分工」：

- 遇到「为什么某层算得慢、为什么显存占用大」——更多要去理解 **ncnn**（Net、Extractor、Vulkan、bf16/fp16）。
- 遇到「为什么生成的文本不对、为什么多轮对话记不住、为什么采样参数没生效」——更多要去读 **ncnn_llm** 的上层逻辑（prefill、generate、采样、ctx）。

#### 4.3.2 核心流程

从「依赖与调用」的角度，两者的协作流程如下：

```text
┌─────────────────────────────────────────────────────────┐
│  你的应用 / 示例 (examples/llm_ncnn_run/main.cpp 等)      │
└───────────────────────────┬─────────────────────────────┘
                            │ 调用 ncnn_llm 的高层 API
                            ▼
┌─────────────────────────────────────────────────────────┐
│  ncnn_llm 运行时 (src/ncnn_llm_gpt.* 等)                  │
│  - 读 model.json 配置                                     │
│  - 分词 / 位置编码(RoPE) / KV cache 调度                  │
│  - prefill → generate 自回归循环                          │
│  - 采样选词                                                │
└───────────────────────────┬─────────────────────────────┘
                            │ 通过 ncnn::Net / ncnn::Extractor 调用
                            ▼
┌─────────────────────────────────────────────────────────┐
│  ncnn 推理引擎 (外部依赖)                                 │
│  - 加载 .ncnn.param / .ncnn.bin                          │
│  - CPU 多线程 / Vulkan GPU 计算                          │
│  - 真正执行每一层神经网络运算                             │
└─────────────────────────────────────────────────────────┘
```

记住两个事实：

1. ncnn_llm 在构建时**显式依赖 ncnn**（见下文源码 `add_requires("ncnn master")`）。
2. ncnn_llm 把模型权重以 `.ncnn.param` / `.ncnn.bin` 格式存放——这本身就是 ncnn 的原生格式，再次说明 ncnn 是底座。

#### 4.3.3 源码精读

**文档层面**，README 在开头就点明了这种依赖与起源：

[readme.md:L30-L32](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L30-L32) —— 明确写 "running language models and embedding models **with ncnn**"，并说明项目 "started from **nihui's** experimental ncnn `kvcache` work"。（nihui 同时也是 ncnn 的主要作者，所以这条传承关系是顺理成章的。）

**构建配置层面**，`xmake.lua` 直接声明了对 ncnn 的依赖，这是「ncnn 是底座」最硬的源码证据：

[xmake.lua:L50-L54](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L50-L54) —— `add_requires("ncnn master", { configs = { vulkan=true } })`：把 ncnn 的 `master` 分支作为依赖引入，并开启 Vulkan 能力。注意它要求 ncnn 的 **master** 分支（而不是某个发布版本），因为 ncnn_llm 用到了 ncnn 较新的能力。

[xmake.lua:L56-L57](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L56-L57) —— 另一个依赖 `nlohmann_json`，这正是用来解析 `model.json` 的 JSON 库（在 u1-l5 会详讲）。

再看核心运行时 target 是怎么把 ncnn「链接」进来的：

[xmake.lua:L65-L72](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L65-L72) —— `target("ncnn_llm")` 是一个静态库，编译 `src/*.cpp`，并通过 `add_packages("ncnn", "nlohmann_json")` 把 ncnn 与 json 库引入。所有示例 target（`llm_ncnn_run`、`ocr_main` 等）都 `add_deps("ncnn_llm")` 依赖它，从而间接依赖到 ncnn。

> 提炼：`add_requires`（声明外部包）→ `add_packages`（把包接到某个 target）→ `add_deps`（target 之间互相依赖）。这条链路把「ncnn 引擎 → ncnn_llm 运行时 → 各示例程序」串了起来，正是「地基—楼—房间」的关系。

#### 4.3.4 代码实践

这是一个**源码阅读型实践**，目的是让你亲手在构建配置里确认这种依赖。

1. **实践目标**：在源码层面验证「ncnn_llm 依赖 ncnn」，并能用一句话描述二者关系。
2. **操作步骤**：
   - 打开 [xmake.lua](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua)。
   - 找到第 50–54 行的 `add_requires("ncnn master", ...)`，记下它要求 ncnn 的哪个分支、是否开启 Vulkan。
   - 找到第 65–72 行的 `target("ncnn_llm")`，确认它通过 `add_packages` 引入了 ncnn。
   - 浏览后续 target（`llm_ncnn_run`、`ocr_main`、`asr_main` 等），确认它们都通过 `add_deps("ncnn_llm")` 间接用上 ncnn。
3. **需要观察的现象**：每个可执行 target 都没有直接 `add_requires("ncnn")`，而是依赖 `ncnn_llm` 这个静态库——ncnn 的能力是被 `ncnn_llm` 封装后再透出来的。
4. **预期结果**：你能写出这样一句话——「ncnn 是底层神经网络推理引擎（外部依赖，要求 master 分支并开 Vulkan），ncnn_llm 是构建在它之上的静态库运行时，所有示例程序都通过依赖 ncnn_llm 来间接使用 ncnn」。
5. 如果无法在本地打开文件查看，标注「待本地验证」。

> 进阶观察（可选）：注意 [xmake.lua:L50-L54](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L50-L54) 要求 ncnn 来自 `master`，这意味着你不能随便装一个旧的 ncnn 就用，构建时需要较新的 ncnn。这是 u1-l2（构建与运行）会再次遇到的关键约束。

#### 4.3.5 小练习与答案

**练习 1**：用「地基与楼」的比喻，描述 ncnn 与 ncnn_llm 的关系。

> **参考答案**：ncnn 是「地基/发动机」，提供通用的神经网络层计算（CPU 多线程、Vulkan GPU）；ncnn_llm 是「楼/整车」，在 ncnn 之上专门组织大模型的自回归推理流程（分词、RoPE、KV cache、采样）。ncnn_llm 依赖 ncnn，ncnn 不专门懂大模型生成。

**练习 2**：在 `xmake.lua` 里，ncnn 是通过哪两个 xmake 关键字被引入到 `ncnn_llm` 这个 target 的？

> **参考答案**：先用 `add_requires("ncnn master", ...)`（第 50 行）声明外部依赖，再用 `add_packages("ncnn", "nlohmann_json")`（第 71 行）把 ncnn 这个包接到 `ncnn_llm` target 上。

**练习 3**：为什么遇到「采样温度没生效」要读 ncnn_llm，而遇到「GPU 上某层算得慢」要关注 ncnn？

> **参考答案**：采样逻辑属于「大模型生成流程」的上层调度，由 ncnn_llm 实现（后续 U3 单元）；而单层神经网络在 GPU 上的计算效率，属于 ncnn 引擎本身的执行能力（Vulkan、bf16/fp16 等）。这是「楼」和「地基」的分工。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个**总览级小任务**（无需运行任何模型，纯阅读 + 整理）：

**任务：为 ncnn_llm 建立一张「一页纸项目画像」**

请在你自己的笔记里，产出以下四样东西：

1. **一句话定位**：用一句话说清 ncnn_llm 是什么（参考 4.1 的练习结果）。
2. **能力清单**：把 [readme.md:L34-L44](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L34-L44) 的九条 Highlights，按「对话类 / 模态扩展类 / 工程基础设施类」自己归一下类。
3. **支持模型对照表**：把 4.2.4 里那张「模型类别 → 示例 target」表抄一遍，并补上每个类别的「输入 → 输出」（参考 4.2.1）。
4. **依赖关系图**：照着 4.3.2，画一张「应用 → ncnn_llm → ncnn」的三层依赖图，并在 ncnn_llm 那一层标注它做了哪些事（分词、RoPE、KV cache、prefill/generate、采样）。

完成后，你应该能不查文档、向同事讲清楚 ncnn_llm 是什么。这张「一页纸画像」也会成为你后续学习每个具体模块时回头参照的「地图」。

> 关于「能不能真正跑起来」：本讲刻意不要求运行模型。真正构建并运行 `llm_ncnn_run` 的实践，留给下一篇讲义 **u1-l2（构建系统与运行方式）**。本讲的实践产物是**笔记和图**，而不是命令行输出。

---

## 6. 本讲小结

- **ncnn_llm 是什么**：基于 ncnn 的轻量级 C++ 推理运行时，统一支持 LLM、VLM、OCR、ASR、翻译、嵌入六类能力，面向边缘设备 / 桌面 CPU / Vulkan GPU 的本地推理。
- **起源**：源自 nihui（ncnn 主要作者）对 ncnn `kvcache` 的实验性工作，扩展出可复用的示例、加载器、分词器、视觉预处理、OCR 与嵌入 API。
- **统一管线**：输入只有文本/图像/音频三类，输出只有文本/向量两类，中间是「按 model.json 切换」的共享运行时。
- **支持模型**：共 6 大类、11 个模型（LLM×3、VLM×2、OCR×2、ASR×1、翻译×1、嵌入×2）。
- **核心设计主线**：在不同模型族之间**共享 decoder 与 KV cache 运行时**，这是 Roadmap 的第一条，也是整本学习手册的主线。
- **与 ncnn 的关系**：ncnn 是底层通用神经网络推理引擎（地基/发动机），ncnn_llm 是它之上专门面向大模型生成的运行时（楼/整车）；在 `xmake.lua` 里通过 `add_requires("ncnn master")` 显式声明这种依赖。

---

## 7. 下一步学习建议

本讲只看了「门牌号」。建议按学习手册的顺序继续：

1. **下一篇 u1-l2（构建系统与运行方式）**：动手用 xmake 构建项目，真正跑通 `llm_ncnn_run`，理解 `add_requires("ncnn master")` 在实战中意味着什么。
2. **u1-l3（目录结构与源码地图）**：对照 README 的 Project Layout，建立完整的源码地图，知道每个功能大概在哪个目录。
3. **u1-l4 / u1-l5**：从 CLI 入口 `main.cpp` 读起，再理解每个模型目录的 `model.json` 配置体系。

继续阅读建议：在进入 u1-l2 之前，可以把 [readme.md](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md) 的 Quick Start、Project Layout、Configuration 三节通读一遍，它们是后续讲义的「索引页」。**不要急着读 `src/` 下的 C++ 实现**——那是 U2 单元以后的事，现在只要记住「有一套共享运行时在调度各种模型」就够了。

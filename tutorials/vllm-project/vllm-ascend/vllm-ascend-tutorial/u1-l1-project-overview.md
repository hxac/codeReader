# 项目定位：vLLM Ascend 是什么

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标只有一个：**让你在不动任何 NPU、不跑任何命令的前提下，搞清楚 `vllm-ascend` 到底是什么、它和上游 `vLLM` 是什么关系、它需要什么样的软硬件环境、它的代码是怎么组织的。**

学完本讲，你应该能够：

- 用一句话向别人解释 `vllm-ascend` 解决了什么问题；
- 说出 `vllm-ascend` 遵循的设计思想是「可插拔硬件（Hardware Pluggable）」，并理解它为什么能让 Ascend 与 vLLM **解耦**；
- 列出它支持的硬件型号与关键软件依赖（CANN / torch-npu / vLLM / Python 版本）；
- 说清 `main` 分支与 `releases/vX.Y.Z` 分支的分工，以及它们如何与上游 vLLM 版本对应。

本讲不深入源码逻辑（那是后续讲义的任务），只建立**正确的项目认知**。认知错了，后面读再多代码都会跑偏。

## 2. 前置知识

阅读本讲前，你只需要具备以下常识即可：

- **大语言模型（LLM）推理**：把一个训练好的模型部署起来，输入一段文本（prompt），让它输出续写文本。`vLLM` 就是干这件事的高性能推理引擎。
- **GPU / NPU**：GPU 是英伟达的通用计算卡（用 CUDA 编程）；**NPU** 是华为昇腾（Ascend）系列的神经网络处理器（用 CANN + AscendC 编程）。两者是不同的硬件生态。
- **插件（Plugin）**：就像浏览器可以装扩展一样，主程序预留好接口，第三方按接口实现就能"接入"而不必改主程序代码。本项目的"硬件插件"就是这种思路。

如果你还不知道 `vLLM` 是什么，只需记住：它是当前最流行的开源 LLM 推理框架之一，原生主要面向 GPU。本讲会解释 `vllm-ascend` 是如何把它"搬"到昇腾 NPU 上的。

## 3. 本讲源码地图

本讲几乎不涉及 Python 源码，而是以**项目说明文档**为主要"源码"。这些文档本身就是理解项目定位的最权威材料。

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md) | 项目英文主页：定位、前置条件、分支策略、社区入口。本讲最重要的材料。 |
| [README.zh.md](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.zh.md) | 上面这份主页的中文版，内容对应，可作为对照阅读。 |
| [AGENTS.md](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/AGENTS.md) | 贡献者开发规范。其中"vLLM Ascend Plugin Architecture"一节用最简练的语言点明了项目的架构哲学，本讲会引用。 |

补充说明：真正让 NPU 跑起来的 Python 代码都在 `vllm_ascend/` 目录下，C++ 算子内核在 `csrc/` 下。这两个目录的详细地图是下一篇讲义（u1-l2「源码地图」）的主题，本讲只在需要时点到为止。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 项目说明** —— `vllm-ascend` 是什么、解决什么问题、如何与 vLLM 解耦；
- **4.2 依赖与分支** —— 运行它需要什么软硬件，以及 `main` / `releases` 分支策略。

### 4.1 项目说明：vllm-ascend 是什么

#### 4.1.1 概念说明

一句话定位（也是 README 的原文）：

> vLLM Ascend (`vllm-ascend`) is a community maintained hardware plugin for running vLLM seamlessly on the Ascend NPU.
>
> ——vLLM Ascend 是一个由社区维护的、让 vLLM 在昇腾 NPU 上无缝运行的**硬件插件**。

要理解这句话，需要拆开三个关键词：

1. **vLLM**：上游的推理框架，原生以 GPU/CUDA 为主战场。它本身并不"认识"昇腾 NPU。
2. **Ascend NPU**：华为昇腾系列神经网络处理器，是一套与 CUDA 不同的软硬件栈（CANN 驱动 + torch-npu 适配层 + AscendC 算子）。
3. **硬件插件（Hardware Plugin）**：一种**解耦**的集成方式——vLLM 不必把昇腾的代码合并进自己的主干，而是预留一套"可插拔硬件接口"；`vllm-ascend` 作为独立项目实现这套接口，从而把 vLLM 的推理能力延伸到 NPU 上。

这种"解耦"带来的好处是双向的：

- 对 **vLLM 上游**：不必为每一种新硬件污染主干代码，主干保持干净、专注于 GPU；
- 对 **昇腾生态**：可以有自己的发布节奏、CI 看护和版本策略，不被上游发版绑架；
- 对 **用户**：装了 vLLM 再装一个 `vllm-ascend` 插件，就能在 NPU 上用几乎一致的 API 跑推理。

这个设计思想来自一份公开的提案：vLLM 社区的 `[[RFC]: Hardware pluggable]`。README 明确写道，本项目"adheres to the principles outlined in the RFC"（遵循该 RFC 所述原则）。`RFC`（Request For Comments）在开源社区里通常指一份"提议/征求意见"文档，这里特指 vLLM 那条关于"硬件可插拔"的设计讨论。本讲最后会带你点开它的链接看一眼。

一句话总结这层关系：**`vllm-ascend` 不重写模型，也不 fork vLLM；它通过"插件接口"站在 vLLM 身旁，把 vLLM 指向 CUDA 的那些路径，改道指向昇腾 NPU。** 至于具体怎么"改道"（Patch / 继承 / 自定义算子），是后续单元（u3、u4、u6）的主题，本讲先记住这个"定位"即可。

#### 4.1.2 核心流程

从用户视角看，"用上 vllm-ascend" 的整体流程是：

```text
安装上游 vLLM  ──▶  安装 vllm-ascend 插件  ──▶  vLLM 启动时通过"插件入口点(entry points)"发现并加载插件
                                                          │
                                                          ▼
                              插件把"硬件平台"注册为 NPUPlatform（昇腾专用平台）
                                                          │
                                                          ▼
                       vLLM 后续的 Worker / ModelRunner / 注意力后端 / 算子，统统走昇腾 NPU 路径
```

这张图里有几个概念，本讲只需建立直觉，不必深究：

- **插件入口点（entry points）**：Python 打包机制，让 vLLM 能"按名字"找到 `vllm-ascend`。细节见 u1-l5「插件入口」。
- **NPUPlatform**：插件向 vLLM 注册的"昇腾平台"对象，描述了这块硬件能干什么。细节见 u2-l1。
- **Worker / ModelRunner / 注意力后端 / 算子**：vLLM 内部把"执行推理"拆成的几个层级，插件在每一层都做了昇腾适配。细节见 u4、u5、u6。

本讲的重点是**第①步的认知**：理解插件是一种"解耦"的集成方式，而不是一个"魔改的 vLLM 分支"。

#### 4.1.3 源码精读

下面引用 README 的关键原文（注意每条都附永久链接与行号，方便你点开核对）。

**① 一句话定位——它是什么**

README 第 52 行直接给出定义：

[README.md:52-52](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md#L52-L52) —— "vLLM Ascend (`vllm-ascend`) is a community maintained hardware plugin for running vLLM seamlessly on the Ascend NPU."

中文版对应同义表述：

[README.zh.md:45-45](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.zh.md#L45-L45) —— "vLLM 昇腾插件 (`vllm-ascend`) 是一个由社区维护的让 vLLM 在 Ascend NPU 无缝运行的后端插件。"

> 说明：英文用 "hardware plugin"（硬件插件），中文版译为"后端插件"，含义一致——它是一个**独立于 vLLM 主干的、针对某类硬件的接入层**。

**② 设计思想——它为什么能解耦**

README 第 54 行点明了它遵循的设计原则：

[README.md:54-54](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md#L54-L54) —— "It is the recommended approach for supporting the Ascend backend within the vLLM community. It adheres to the principles outlined in the `[[RFC]: Hardware pluggable]`, providing a hardware-pluggable interface that decouples the integration of the Ascend NPU with vLLM."

这句话信息量很大，拆成三点：

- **"recommended approach"**：它是 vLLM 社区**官方推荐**的昇腾支持方式（不是某个团队的私有 fork）；
- **"adheres to the RFC"**：它遵循 vLLM 的"可插拔硬件"设计提案；
- **"decouples ... Ascend NPU with vLLM"**：核心价值是**解耦**——昇腾的集成代码不进 vLLM 主干，vLLM 主干也不必为昇腾特判。

**③ 能力边界——它能让什么模型跑起来**

[README.md:56-56](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md#L56-L56) —— "By using vLLM Ascend plugin, popular open-source models, including Transformer-like, Mixture-of-Experts (MoE), Embedding, Multi-modal LLMs can run seamlessly on the Ascend NPU."

这说明插件支持四大类模型：

- **Transformer-like**：常见的稠密 Transformer 大模型（如 Llama、Qwen 类）；
- **MoE（Mixture-of-Experts，混合专家）**：每个 token 只激活部分专家网络的大模型（如 DeepSeek 系列）；
- **Embedding**：输出向量嵌入、用于检索/分类的模型；
- **Multi-modal**：多模态模型（图文等）。

具体的支持清单（哪些模型、哪些特性、哪张卡）在官方「Support Matrix（支持矩阵）」里维护，本讲 4.2 节和综合实践都会用到它。

**④ 架构哲学——AGENTS.md 的精炼总结**

除了 README，贡献规范 AGENTS.md 里有一段对架构最直接的描述，值得记住：

[AGENTS.md:210-211](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/AGENTS.md#L210-L211) —— "vLLM Ascend is a **hardware plugin** that integrates with upstream vLLM via the pluggable hardware interface. It does not add new model files directly."

后半句"It does not add new model files directly"（不直接添加新的模型文件）很关键：**插件的本职不是"造模型"，而是"让上游已有的模型能在 NPU 上正确、高效地跑"**。AGENTS.md 接着指出，模型相关的功能主要通过三种手段实现——**Patching（打补丁）/ Inheritance（继承重写）/ 向上游贡献**。这三种手段是后续单元（u3、u4、u11）的核心，本讲只需知道有这三条路。

#### 4.1.4 代码实践

> 本实践为"源码阅读型"，无需 NPU，也无需安装任何东西。

**实践目标**：通过官方"支持矩阵"和 RFC 提案，验证你对项目定位的理解。

**操作步骤**：

1. 打开支持矩阵官方页面（README 里给出的链接）：
   <https://docs.vllm.ai/projects/ascend/en/latest/user_guide/support_matrix/>
2. 在其中找到「Supported Models（支持的模型）」一节，记录 3 个你认识的模型名字。
3. 打开 RFC 提案（README 第 44/54 行提到的链接）：
   <https://github.com/vllm-project/vllm/issues/11162>
   只读它的标题与首段描述即可。

**需要观察的现象**：

- 支持矩阵里既有模型清单，也有硬件/特性清单——这说明"插件支持什么"是**有明确矩阵、持续维护**的，而不是口头承诺。
- RFC 的标题里就写着 "Hardware pluggable"——这正是本项目设计思想的源头。

**预期结果**：你能用自己的话回答："支持矩阵证明 vllm-ascend 是一个有明确能力边界的、持续维护的项目；RFC 证明它的'解耦'设计是 vLLM 社区层面的共识，而非 vllm-ascend 自说自话。"

> 说明：支持矩阵是官方文档站点上的页面，不在本仓库内，因此不附仓库内永久链接。若页面内容有调整，以你打开时看到的实际内容为准。

#### 4.1.5 小练习与答案

**练习 1**：有人说"`vllm-ascend` 就是 vLLM 的一个 fork（分叉）"，这种说法对吗？为什么？

**参考答案**：不对。fork 意味着把 vLLM 整套代码复制一份并自行维护；而 `vllm-ascend` 是一个**独立的项目**，通过 vLLM 的"可插拔硬件接口"与上游对接，上游 vLLM 仍然是独立安装、独立升级的。README 第 54 行明确指出它"decouples the integration of the Ascend NPU with vLLM"（解耦）。

**练习 2**：`vllm-ascend` 不"直接添加新的模型文件"（AGENTS.md:211），那它是怎么让上游模型在 NPU 上跑起来的？请列举它采用的几种手段（名称即可）。

**参考答案**：主要三种手段（见 AGENTS.md「Model and Plugin Architecture」）：① **Patching（打补丁）**——在 `vllm_ascend/patch/` 下替换上游方法；② **Inheritance（继承重写）**——如 `NPUModelRunner(GPUModelRunner)`、`AscendSampler` 等，在上游类基础上扩展 NPU 行为；③ **向 vLLM 上游直接贡献**。此外还包括为 NPU 编写**自定义算子**。

### 4.2 依赖与分支：运行环境与版本策略

#### 4.2.1 概念说明

要真正跑起 `vllm-ascend`，需要一套**软硬协同**的栈。理解这套栈的层次，有助于你在后续读源码时分清"哪些是插件代码、哪些是底层 SDK 提供"。

整个运行栈从下到上大致是：

```text
┌─────────────────────────────────────────────┐
│  你的应用 / examples 里的推理脚本             │  ← 用户层
├─────────────────────────────────────────────┤
│  vLLM（上游推理框架，与 vllm-ascend 同版本）  │  ← 推理引擎
├─────────────────────────────────────────────┤
│  vllm-ascend（本插件：NPU 平台/算子/补丁）    │  ← 硬件适配层（本项目）
├─────────────────────────────────────────────┤
│  torch-npu（让 PyTorch 能调用 NPU 的适配层） │  ← PyTorch↔NPU 桥
├─────────────────────────────────────────────┤
│  PyTorch（深度学习框架）                      │  ← 框架
├─────────────────────────────────────────────┤
│  CANN（昇腾异构计算架构，NPU 的"驱动+运行时"）│  ← NPU 系统软件
├─────────────────────────────────────────────┤
│  Ascend NPU 硬件（Atlas 800I A2 / A3 …）     │  ← 硬件
└─────────────────────────────────────────────┘
```

几个名词解释（初学者最容易混淆）：

- **CANN**（Compute Architecture for Neural Networks）：华为昇腾的异构计算架构，相当于 NPU 这一侧的"驱动 + 算子库 + 运行时"。没有它，NPU 就是一块无法编程的卡。可类比为英伟达侧的"驱动 + CUDA runtime"。
- **torch-npu**：华为提供的 PyTorch 适配插件，让 `torch` 的张量和算子能跑到 NPU 上（类似把 `cuda` 后端换成 `npu` 后端）。`vllm-ascend` 的 Python 代码大多最终通过 torch-npu 调用到 NPU。
- **vLLM 版本对齐**：README 明确要求 vLLM 要"the same version as vllm-ascend"（与 vllm-ascend 同版本）。这是因为插件深度依赖上游内部接口，版本错配会直接报错。

#### 4.2.2 核心流程

版本与分支策略可以总结成一张"对应关系图"：

```text
vLLM 上游                  vllm-ascend
─────────                  ───────────
vLLM main 分支   ◀──对应──▶  vllm-ascend main 分支        （持续 CI 看护，跟随上游最新）
vLLM vX.Y.Z tag  ◀──对应──▶  vllm-ascend releases/vX.Y.Z  （随上游发版而创建的稳定分支）
（实验特性）      ◀──对应──▶  rfc/<feature-name>           （协作用的特性分支）
```

要点：

1. **main 分支**：与 vLLM 的 main 分支对应，并由昇腾 CI **持续看护质量**。它是"追新"用的——能用到最新的上游能力，但也可能跟随上游发生接口变动。
2. **releases/vX.Y.Z 分支**：每当 vLLM 发布一个新版本，vllm-ascend 就创建一条对应的 `releases/vX.Y.Z` 开发分支，做该版本的 CI 看护。它是"求稳"用的——比如 `releases/v0.18.0` 对应 vLLM 的 `v0.18.0`。
3. **rfc/<feature-name>**：用于在社区里协作开发某项实验特性，合入主干前先在这里讨论。

理解这条对应关系后，你就能回答两个常见问题：

- "我应该用哪个分支？" —— 想稳定生产用 `releases/*`；想跟进最新能力用 `main`。
- "为什么我装了 vLLM 却跑不起来 vllm-ascend？" —— 大概率是**版本没对齐**（README 要求同版本）。

#### 4.2.3 源码精读

下面引用 README 中「Prerequisites（前置条件）」与「Branch（分支）」两节的原文。

**① 支持的硬件型号**

[README.md:62-62](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md#L62-L62) —— "Hardware: Atlas 800I A2 Inference series, Atlas A2 Training series, Atlas 800I A3 Inference series, Atlas A3 Training series, Atlas 300I Duo (Experimental)"

可见主力支持的硬件是 **A2 / A3** 两大系列的训练与推理卡，而 **300I Duo 标注为 Experimental（实验性支持）**。这个"实验性"标注很重要——后续在 u11-l2 你会看到代码里有 `is_310p()` 之类的硬件分支判断，正是因为不同型号能力不同、需要走不同代码路径。

**② 软件依赖版本（关键）**

[README.md:64-68](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md#L64-L68) ——

```
Software:
    - Python >= 3.10, < 3.13
    - CANN == 9.0.1 (...)
    - PyTorch == 2.10.0, TorchNPU == 2.10.0.post2
    - vLLM (the same version as vllm-ascend)
```

逐条解读：

| 依赖 | 要求版本 | 含义 |
| --- | --- | --- |
| Python | `>= 3.10, < 3.13` | 支持 3.10 / 3.11 / 3.12，不支持 3.13 及以上 |
| CANN | `== 9.0.1` | **精确版本**，必须匹配，否则算子/运行时不兼容 |
| PyTorch | `== 2.10.0` | 精确版本 |
| TorchNPU | `== 2.10.0.post2` | 与上面 PyTorch 配套的 NPU 适配层，精确版本 |
| vLLM | 与 vllm-ascend 同版本 | 版本必须对齐 |

注意 `==`（精确等于）和 `>=`（范围）的区别：CANN / PyTorch / TorchNPU 都是**精确版本**要求，差一个小版本号都可能出问题。这是 NPU 软件栈的常态——它比 GPU 生态更"脆"，所以版本纪律很重要。

**③ 分支策略原文**

[README.md:85-88](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md#L85-L88) ——

```
vllm-ascend has a main branch and a dev branch.
- main: ... corresponds to the vLLM main branch, and is continuously monitored for quality through Ascend CI.
- releases/vX.Y.Z: development branch, created alongside new releases of vLLM.
```

**④ 当前维护中的分支（带具体版本对应）**

[README.md:90-98](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md#L90-L98) 给出了一张维护表，提炼如下：

| 分支 | 含义 / 对应的 vLLM 版本 |
| --- | --- |
| `main` | 对应 vLLM main 分支，并 CI 看护到 vLLM `v0.26.0` tag |
| `releases/v0.13.0` | 只允许 Bug 修复，不再发新版本 tag |
| `releases/v0.18.0` | 对 vLLM `0.18.0` 做 CI 看护 |
| `releases/v0.23.0` | 对 vLLM `0.23.0` 做 CI 看护 |
| `rfc/<feature-name>` | 协作用的特性分支 |

> 注意：上表是 README 在当前 HEAD（`646684f43`）记录的状态，属于**会随时间变化**的信息。如果你在较晚的时间阅读，请以当时 README 的实际内容为准。

#### 4.2.4 代码实践

> 本实践为"对照阅读型"，无需 NPU。

**实践目标**：把"硬件型号 / 软件版本 / 分支策略"三件事和你自己的实际情况对上号。

**操作步骤**：

1. 假设你（或你的团队）手头有一张昇腾卡，对照 [README.md:62-62](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md#L62-L62) 判断它是否在支持矩阵内，属于哪一系列（A2 / A3 / 300I Duo）。
2. 如果你想用一个**稳定**的部署，对照 [README.md:90-98](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md#L90-L98) 选一条 `releases/vX.Y.Z` 分支，并写出它对应的 vLLM 版本。
3. 检查你计划使用的 CANN / PyTorch / TorchNPU 版本是否满足 [README.md:64-68](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md#L64-L68) 的精确版本要求。

**需要观察的现象**：

- 同样是"昇腾卡"，300I Duo 是**实验性**支持，主力是 A2/A3——这说明硬件型号直接决定了你能不能用、以及用哪条代码路径。
- `releases/*` 分支是"版本对齐"的载体：选了 `releases/v0.18.0`，就要配 vLLM `0.18.0`。

**预期结果**：你能写出一条形如"我用 ___ 系列卡，选择 `releases/___` 分支，需要 CANN ___ / PyTorch ___ / TorchNPU ___ / vLLM ___"的完整配置清单。若你目前没有卡或不确定型号，请明确写「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 README 对 CANN、PyTorch、TorchNPU 都用 `==`（精确版本），而不是 `>=`（最低版本）？

**参考答案**：因为 NPU 软件栈（CANN / torch-npu）和 PyTorch 之间是**强耦合**的：不同小版本的算子行为、接口、运行时可能不兼容，`vllm-ascend` 的代码是针对**特定组合**调试通过的。用 `==` 能避免用户装到未经测试的组合上而出诡异错误。这是 NPU 生态相比 GPU 生态更"脆"、版本纪律更严的体现。

**练习 2**：`main` 分支和 `releases/v0.18.0` 分支，哪个更适合"生产环境部署"，哪个更适合"尝鲜最新功能"？为什么？

**参考答案**：生产环境选 `releases/v0.18.0`——它对应一个固定的 vLLM 版本，有 CI 看护，行为更稳定可预期；尝鲜最新功能选 `main`——它跟随 vLLM main 分支，能拿到最新能力，但可能随上游接口变动而不够稳定。

**练习 3**：README 在硬件列表里给 `Atlas 300I Duo` 标了 `(Experimental)`。这个标注对源码阅读有什么提示？

**参考答案**：它提示源码里很可能存在**针对该型号的分支判断**（如 `is_310p()` 之类的条件），让它在能力较弱的卡上走简化/降级路径。后续 u11-l2「Ascend 310P 适配」会专门讲这类硬件分支。本讲只需记住：不同型号 = 不同代码路径。

## 5. 综合实践

这是本讲的"收口"任务，把 4.1 和 4.2 串起来。

**任务**：阅读 [README.md](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md)（或 [README.zh.md](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.zh.md)）和官方支持矩阵，**用自己的话写一段约 200 字的中文说明**，回答两个问题：

1. `vllm-ascend` 解决了什么问题？
2. 它和上游 vLLM 是什么关系？

**写作要求**：

- 不要照抄 README 原文，要用"如果你向一个完全没接触过这个项目的同事解释"的口吻来写；
- 必须包含以下要点中的至少 3 个：① 它是硬件插件；② 它遵循"可插拔硬件"RFC、与 vLLM 解耦；③ 它不重写模型，而是通过 Patch/继承/自定义算子适配 NPU；④ 它支持的硬件系列与关键依赖（CANN/torch-npu）；⑤ main 与 releases 的版本对应关系。

**参考写法（仅供对照思路，请自己重写）**：

> `vllm-ascend` 解决的是"如何让主流开源大模型在华为昇腾 NPU 上高效推理"的问题。它不是 vLLM 的分叉，而是一个独立的**硬件插件**：遵循 vLLM 的"可插拔硬件"RFC，通过预留接口与 vLLM 解耦——上游 vLLM 仍独立安装升级，插件则负责把原本指向 CUDA 的执行路径改道指向 NPU。它不重写模型，而是用打补丁、继承重写和自定义算子三种方式做适配，支持 A2/A3 等系列的训练与推理卡，依赖 CANN、torch-npu 等配套软件栈，并通过 `main`（追新）与 `releases/vX.Y.Z`（求稳）两类分支与上游 vLLM 版本一一对应。

完成后，把这段话保存下来——在后续每一篇讲义里，你都可以用它来快速回忆"我现在在读的这个项目，到底是干什么的"。

## 6. 本讲小结

- `vllm-ascend` 是一个**社区维护的硬件插件**，让 vLLM 在昇腾 NPU 上无缝运行（[README.md:52](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md#L52-L52)）。
- 它遵循 vLLM 的 **"可插拔硬件" RFC**，核心价值是**解耦**：昇腾的集成代码不进 vLLM 主干（[README.md:54](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md#L54-L54)）。
- 它**不直接添加模型文件**，而是通过 Patch / 继承 / 自定义算子让上游模型在 NPU 上跑起来（[AGENTS.md:210-211](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/AGENTS.md#L210-L211)）。
- 它支持 **A2 / A3 系列**主力卡，300I Duo 为实验性支持（[README.md:62](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md#L62-L62)）。
- 它要求 **CANN / PyTorch / TorchNPU 精确版本**，且 vLLM 与插件同版本（[README.md:64-68](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md#L64-L68)）。
- 分支上 `main` 追新、`releases/vX.Y.Z` 求稳，二者与上游 vLLM 版本一一对应（[README.md:85-98](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/README.md#L85-L98)）。

## 7. 下一步学习建议

本讲建立了"项目是什么"的认知，但还没看到一行真正的 Python 代码。建议按下面的顺序继续：

1. **u1-l2「源码地图：目录结构与模块总览」**：进入 `vllm_ascend/` 目录，建立对 platform / patch / worker / attention / ops 等子目录的整体认知。这是读所有后续源码的基础。
2. **u1-l3「环境准备与安装构建」**：了解 `setup.py` + `CMakeLists.txt` 如何编译 C++ 自定义算子，以及 `envs.py` 里的构建相关环境变量。
3. 如果你想先建立"插件是怎么被发现的"的直觉，可以跳读 **u1-l5「插件入口：注册机制与发现流程」**，看 `vllm_ascend/__init__.py` 里的 `register()` 是如何把 NPUPlatform 注册给 vLLM 的。

一个提醒：本讲反复强调的"解耦"和"Patch/继承/算子"三种手段，会在 u3（Patch 机制）、u4（Worker/ModelRunner）、u6（自定义算子）里逐层展开，届时你再回看本讲的定位描述，会有更深的体会。

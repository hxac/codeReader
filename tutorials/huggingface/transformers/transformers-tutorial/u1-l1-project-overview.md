# 项目定位与设计哲学

## 1. 本讲目标

本讲是整本《transformers 源码学习手册》的第一篇。学完本讲，你应当能够：

- 用一句话说清楚 **transformers 到底是什么**：它不是「训练框架」，也不是「推理引擎」，而是连接两者的 **「模型定义框架（model-definition framework）」**。
- 理解为什么「**把模型定义集中化**」是整个机器学习生态得以协作的关键理念。
- 说出 transformers 与 PyTorch、vLLM、DeepSpeed 等周边工具的 **上下游关系**。
- 掌握 transformers 的 **三条核心准则（core tenets）**，知道它们如何决定维护者评审代码的方式。

本篇只读两个文件：`README.md` 和 `docs/source/en/philosophy.md`。不写任何可运行代码、不需要安装环境——这是一次纯粹的「**读懂项目自述**」练习，但它会为你后续阅读全部源码奠定正确的认知坐标。

---

## 2. 前置知识

在开始前，请确认你对以下概念有最基本的印象（不必深入）：

- **深度学习模型（model）**：一段把输入（如文本、图像）映射为输出的神经网络。在本项目中，模型由若干层（layer）、注意力（attention）、归一化（norm）等组件拼装而成。
- **训练（training）vs 推理（inference）**：训练是用数据调整模型参数的过程；推理是用训练好的参数做预测的过程。
- **PyTorch**：一个深度学习计算框架，`torch.nn.Module` 是它的模型基类。transformers 的所有模型都是 `nn.Module` 的子类。
- **checkpoint（检查点 / 预训练权重）**：训练后保存下来的模型参数文件。Hugging Face Hub 上托管了 100 万+ 个这样的 checkpoint。

> 一个常见的误区：很多人以为 transformers「就是用来训练模型的」或「就是用来跑推理的」。本讲最重要的任务，就是纠正这个认知——它定义模型，然后把这份定义共享给所有训练和推理工具。

---

## 3. 本讲源码地图

本讲只涉及两个非代码文档文件，但它们是理解整个仓库的「宪法」：

| 文件 | 作用 | 在本讲中的角色 |
| --- | --- | --- |
| `README.md` | 项目的「门面」，定义项目定位、安装方式、快速上手示例 | 提供 transformers 的**自我定位**与**生态枢纽**描述 |
| `docs/source/en/philosophy.md` | 项目的设计哲学与核心准则 | 提供维护者做技术取舍的**指导思想** |

> 提示：这两个文件是文档而非源码。但 transformers 把「项目定位」和「设计哲学」明文写进仓库，这本身就是一种工程纪律——它会反映在后面每一讲读到的真实代码里（比如「One Model, One File」准则解释了为什么每个模型目录的 `modeling_*.py` 都很长但很少被拆分）。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 transformers 是什么：模型定义框架**（对应 README 概述）
- **4.2 设计哲学与核心准则**（对应 philosophy 文档）
- **4.3 生态枢纽：上下游工具与「集中化的模型定义」**（贯穿两个文件）

### 4.1 transformers 是什么：模型定义框架

#### 4.1.1 概念说明

先看 README 副标题给自己下的定义：

> State-of-the-art pretrained models for inference and training（用于推理与训练的、最先进的预训练模型）

这句话容易被误读。它并不是说 transformers「既会训练又会推理」，而是说：**transformers 提供的模型定义，既能被训练框架使用，也能被推理引擎使用**。

那么「**模型定义（model definition）**」具体指什么？它包括：

1. **模型的结构**：有多少层、注意力怎么实现、用什么归一化、位置编码怎么做。在代码里体现为 `modeling_*.py` 中那一个个 `nn.Module` 子类。
2. **模型的超参数**：隐藏层大小、层数、词表大小等。在代码里体现为 `configuration_*.py` 中的 `Config` 类。
3. **预处理器**：把原始文本/图像/音频转成模型能吃的张量。在代码里体现为分词器（tokenizer）、图像处理器（image processor）等。

关键洞见是：**只要这三样东西被「定义清楚并固定下来」，训练框架和推理引擎就能各自去实现「如何高效地训练它」或「如何高效地推理它」，而不必重复造轮子**。transformers 的角色，就是做那个「定义清楚并固定下来」的人。

这就像 USB 接口标准：制定标准的人（transformers）不生产 U 盘（训练框架），也不生产电脑（推理引擎），但只要大家都遵守这个接口定义，U 盘和电脑就能互相兼容。

#### 4.1.2 核心流程

transformers 在生态中的位置，可以用下面这张「枢纽图」来理解：

```
                       ┌──────────────────────────┐
                       │   transformers           │
                       │  (模型定义的「单一事实源」) │
                       │  config / modeling / 预处理 │
                       └────────────┬─────────────┘
                                    │ 共享同一份模型定义
            ┌───────────────────────┼───────────────────────┐
            ▼                       ▼                       ▼
   ┌─────────────────┐    ┌──────────────────┐    ┌──────────────────┐
   │   训练框架       │    │    推理引擎       │    │  相邻建模库        │
   │ Axolotl/Unsloth  │    │  vLLM/SGLang/TGI │    │ llama.cpp / mlx  │
   │ DeepSpeed/FSDP   │    │                  │    │                  │
   │ PyTorch-Lightning│    │                  │    │                  │
   └─────────────────┘    └──────────────────┘    └──────────────────┘
```

核心机制可以概括为三点：

1. **定义集中化**：一个模型的结构只在 transformers 里被「权威地」定义一次。
2. **生态约定一致**：因为定义集中，所有依赖它的工具对该模型的理解一致（同一份 config、同一份权重键名）。
3. **一次支持，处处兼容**：README 原话——「if a model definition is supported, it will be compatible with the majority of ...」（一旦某个模型定义被支持，它就能与主流的训练/推理工具兼容）。

#### 4.1.3 源码精读

README 用一整段话给出了 transformers 最权威的自我定位。这是全项目最重要的一段文字，务必逐句读懂：

[README.md:69-75](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/README.md#L69-L75) — 这是 transformers 对自身定位的「官方陈述」，逐句解读如下：

- **第 69-70 行**「Transformers acts as the model-definition framework ... for both inference and training」：明确点出它是「模型定义框架」，且同时服务于训练和推理。
- **第 72 行**「It centralizes the model definition so that this definition is agreed upon across the ecosystem」：点出核心理念——**集中化模型定义**，使整个生态对模型的理解一致。
- **第 72-75 行**「`transformers` is the pivot across frameworks」：用 **pivot（枢纽 / 支点）** 这个词，把自己比作连接各类工具的「转轴」，并列出了三类依赖者（训练框架、推理引擎、相邻库）。

紧接着，README 给出了它的承诺与规模：

[README.md:77-78](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/README.md#L77-L78) — 承诺让新模型的定义保持「简单（simple）、可定制（customizable）、高效（efficient）」。

[README.md:80](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/README.md#L80) — 说明 Hub 上已有 **1M+（100 万以上）** 个可直接使用的 checkpoint。规模本身印证了「集中化定义」的价值。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把 4.1.3 中那段「官方陈述」真正读进脑子里，而不是一眼带过。
2. **操作步骤**：
   - 打开本仓库根目录的 `README.md`，定位到第 69–75 行（用编辑器的「跳转行号」功能）。
   - 用荧光笔/笔记，把这段话里出现的所有「角色词」圈出来：`model-definition framework`、`centralizes`、`pivot`、`training frameworks`、`inference engines`、`adjacent modeling libraries`。
3. **需要观察的现象**：你会注意到整段话**没有出现**「we train models」或「we run fast inference」这样的表述——它在反复强调「定义」和「连接」。
4. **预期结果**：你能用自己的话复述「transformers 是枢纽，而不是终点」这句话的含义。
5. 本实践为纯阅读，无需运行任何命令。

#### 4.1.5 小练习与答案

**练习 1**：如果 transformers 不存在，vLLM 想支持一个新模型 Llama-X，它需要自己做什么？
> **参考答案**：它需要自己重新实现 Llama-X 的网络结构、定义权重键名约定、编写 config 解析——而且很可能和 DeepSpeed、TGI 等工具的实现不一致，导致同一个 checkpoint 在不同工具里行为不同。有了 transformers，大家共用一份定义，就避免了这种碎片化。

**练习 2**：README 说 transformers 服务于「inference and training」，这与「它不是训练框架」矛盾吗？
> **参考答案**：不矛盾。它的意思是「它定义的模型既能用于训练也能用于推理」，而不是「它本身是训练引擎」。transformers 内部确实有一个 `Trainer`（见后续 u9 单元），但它优化的是「配合 PyTorch 模型」的训练循环，而不是通用机器学习循环（通用场景 README 建议用 Accelerate）。

---

### 4.2 设计哲学与核心准则

#### 4.2.1 概念说明

如果说 4.1 讲的是 transformers「是什么」，那么 `philosophy.md` 讲的就是「它**为什么这么做**」。开篇一句话定调：

> Transformers is a PyTorch-first library. It provides models that are faithful to their papers, easy to use, and easy to hack.

三个关键词：

- **PyTorch-first（以 PyTorch 为先）**：所有模型首先且主要作为 PyTorch 的 `nn.Module` 实现。
- **faithful to their papers（忠实于论文）**：实现要和原始论文/官方结果一致，不擅自改动。
- **easy to use, and easy to hack（好用且好改）**：不仅要能直接用，还要方便研究者动手改。

理解这三点，能解释你以后在源码里看到的很多「反直觉」设计。比如：为什么 transformers 的模型文件**故意不把代码拆成很多小文件、不引入大量抽象**？因为「easy to hack」要求研究者打开一个文件就能从头读到尾，而不需要在十几个抽象层之间跳来跳去。

#### 4.2.2 核心流程

transformers 的设计哲学通过两层机制落地：

**第一层：三类核心对象 + 三种统一方法。**

philosophy 指出，每个模型都由三类对象构成，并且这三类对象共享同一套加载/保存方法：

| 核心对象 | 职责 | 典型代码（后续讲义精读） |
| --- | --- | --- |
| **Configuration（配置）** | 存储构建模型所需的超参（层数、隐藏大小等） | `configuration_*.py` |
| **Model（模型）** | `nn.Module` 子类，被 `PreTrainedModel` 包裹 | `modeling_*.py` |
| **Preprocessing（预处理）** | 把原始数据转成模型输入：tokenizer / image processor / video processor / feature extractor / processor | `tokenization_*.py` 等 |

这三类对象都共享三个方法：

```
from_pretrained()   # 从 Hub 或本地 checkpoint 下载并加载
save_pretrained()   # 保存到本地，之后可用 from_pretrained 重新加载
push_to_hub()       # 上传到 Hub 共享给所有人
```

> 这套 `from_pretrained / save_pretrained / push_to_hub` 范式是后续 u2 单元的主题。这里你只需记住：**它统一了配置、模型、预处理三大对象的「读/写」接口**——这正是「集中化定义」在 API 层面的体现。

**第二层：八条核心准则（core tenets）**，指导维护者在评审 PR 时如何取舍。

#### 4.2.3 源码精读

[docs/source/en/philosophy.md:19](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/philosophy.md#L19) — 开篇定调句：「PyTorch-first、忠实论文、好用好改」。

[docs/source/en/philosophy.md:34-42](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/philosophy.md#L34-L42) — 「What you can expect」一节：点明每个模型只需三类对象（config / model / 预处理），它们都能用统一的 `from_pretrained()` 加载；在此之上提供两个高层 API——`pipeline`（快速推理）与 `Trainer`（快速训练）。

[docs/source/en/philosophy.md:44-55](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/philosophy.md#L44-L55) — 这是本节最关键的一段：**八条核心准则**。逐条解读（中英对照）：

| 准则 | 含义 | 对源码的影响 |
| --- | --- | --- |
| **Source of Truth（事实之源）** | 实现必须忠实于官方结果与预期行为 | 不能为了「优雅」而改变模型的数值行为 |
| **One Model, One File（一模型一文件）** | 核心训练/推理逻辑要在用户读的那一个模型文件里从头到尾可见 | `modeling_*.py` 故意写得很长、很少外拆 |
| **Code is the Product（代码即产品）** | 为「可读、可 diff」优化，宁可显式命名也不要花哨的间接层 | 少用元编程，命名直白 |
| **Standardize, Don't Abstract（标准化而非抽象化）** | 模型特有行为留在模型里，共享接口只用于通用基础设施 | 通用机制（如 KV Cache）抽公共，模型细节不抽 |
| **DRY\*（必要时允许重复）** | 面向用户的建模文件保持自洽，基础设施则抽离 | 模型间可以有重复代码，靠 Modular 系统管理（见 u7-l3） |
| **Minimal User API（极简用户 API）** | 代码路径少、kwargs 可预测、方法稳定 | 用户接口刻意保持简单 |
| **Backwards Compatibility（向后兼容）** | 公共表面不应破坏，旧的 Hub 产物必须继续可用 | 改 API 极其谨慎 |
| **Consistent Public Surface（一致的公共表面）** | 命名、输出、可选诊断信息保持一致并测试 | 所有模型输出都用统一的 `ModelOutput`（见 u5-l4） |

[docs/source/en/philosophy.md:57-64](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/philosophy.md#L57-L64) — 「Main classes」一节：进一步解释 Configuration / Model / Modular transformers / Preprocessing 各自的定义。其中 **Modular transformers** 提到：贡献者写一个小小的 `modular_*.py` 片段声明复用关系，库自动展开成用户阅读的 `modeling_*.py`——这既保证「一模型一文件」又避免样板代码漂移（详见 u7-l3）。

[docs/source/en/philosophy.md:66-73](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/philosophy.md#L66-L73) — 总结三大方法 `from_pretrained / save_pretrained / push_to_hub`。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：用「三类核心对象」的框架，去对照一个真实模型目录，建立直觉。
2. **操作步骤**：
   - 在仓库里浏览 `src/transformers/models/llama/` 目录（只看文件名，不读内容）。
   - 找出与「三类对象」对应的文件：配置类（`configuration_llama.py`）、模型类（`modeling_llama.py`）、预处理类（`tokenization_llama.py`）。
3. **需要观察的现象**：你会看到几乎每个模型目录都遵循同样的「三件套」命名规律。
4. **预期结果**：你能说出「每个模型 = 配置 + 模型 + 预处理」这句话在文件层面长什么样。
5. 本实践为目录浏览，无需运行命令；模型文件的具体精读留到 u7 单元。

#### 4.2.5 小练习与答案

**练习 1**：准则「One Model, One File」和「Code is the Product」如何共同解释 `modeling_llama.py` 文件很长这一现象？
> **参考答案**：「One Model, One File」要求核心逻辑集中在一个文件里可见；「Code is the Product」要求代码为可读性优化、不引入花哨的抽象拆分。两者叠加的结果就是：宁可让单个模型文件变长，也要让用户打开一个文件就能从头读到尾，而不必在多个抽象层之间跳转。

**练习 2**：准则「DRY\*」带了一个星号，注释是「Repeat when it helps users」。这和传统的「DRY（不要重复自己）」有什么不同？
> **参考答案**：传统 DRY 追求「消除一切重复」；而 transformers 认为如果消除重复会损害用户的可读性/自洽性，就**允许重复**。面向用户的建模文件可以重复一些代码以保持自洽，重复代码的同步则交给 Modular 系统（`modular_*.py`）和 `# Copied from` 机制来管理。

---

### 4.3 生态枢纽：上下游工具与「集中化的模型定义」

#### 4.3.1 概念说明

4.1 讲了 transformers 是「枢纽」，本模块把枢纽两端的「上下游」具体化。理解这一点的价值在于：**当你在 transformers 里读懂一个模型，你其实同时读懂了它能跑在哪些工具上**。

把生态分成三类依赖者：

1. **训练框架（training frameworks）**：负责高效地训练/微调模型。它们消费 transformers 的模型定义，再加上自己的训练加速（如 ZeRO 分片、算子融合）。
2. **推理引擎（inference engines）**：负责把训练好的模型高效地部署成服务。它们消费同一份模型定义，再加上自己的推理优化（如 PagedAttention、连续批处理）。
3. **相邻建模库（adjacent modeling libraries）**：在其他运行时/硬件上重实现同一份模型定义，使其能在不同环境（如 CPU 量化推理、Apple Silicon）运行。

#### 4.3.2 核心流程

README 在第 72–75 行明确列出了这三类工具的代表。下面是一张「枢纽 × 工具」对照表，方便你建立全景：

| 类别 | 代表工具（README 列举） | 它们如何依赖 transformers |
| --- | --- | --- |
| 训练框架 | Axolotl、Unsloth、DeepSpeed、FSDP、PyTorch-Lightning | 复用 transformers 的模型结构定义，叠加各自的分布式/加速策略 |
| 推理引擎 | vLLM、SGLang、TGI | 复用 transformers 的模型结构与权重格式，叠加各自的推理优化 |
| 相邻建模库 | llama.cpp、mlx | 依据 transformers 的模型定义，在其他运行时上重实现 |

同时，README 也诚实地划出了 transformers **不该**扮演的角色，这对形成正确预期同样重要。这些「边界」会帮助你避免把 transformers 用在不合适的场景。

#### 4.3.3 源码精读

[README.md:72-75](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/README.md#L72-L75) — 列出三类依赖者（训练框架 / 推理引擎 / 相邻建模库），并点明它们都「leverage the model definition from `transformers`」（利用来自 transformers 的模型定义）。

[README.md:216-237](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/README.md#L216-L237) — 「Why should I use Transformers?」一节，从用户视角总结了四大价值：① 易用的最先进模型（统一 API，只需学三个类）；② 更低的算力成本与碳足迹（共享预训练模型而非从头训练）；③ 为模型生命周期的每个环节选对框架；④ 易于定制。

[README.md:243-247](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/README.md#L243-L247) — 「When shouldn't I use Transformers?」一节，划出三条边界（务必记住，这是最容易被误解的部分）：

- **它不是「神经网络积木工具箱」**：模型文件里的代码**故意**不做额外抽象重构，目的是让研究者能快速迭代单个模型，而不必钻进额外的抽象层。
- **它的训练 API 只为「transformers 提供的 PyTorch 模型」优化**：如果你要的是通用的机器学习训练循环，README 明确建议改用 [Accelerate](https://huggingface.co/docs/accelerate)。
- **示例脚本只是「示例」**：`examples/` 下的脚本不一定能开箱即用，需根据自己的场景改造。

> 这三条边界和 4.2 的核心准则一脉相承：「One Model, One File」解释了为什么不做成积木箱；「Minimal User API」解释了为什么训练 API 只覆盖 PyTorch 模型。**哲学和定位是自洽的。**

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：建立「一个模型定义 → 多个下游工具」的具体映射感。
2. **操作步骤**：
   - 从 README 第 72–75 行给出的工具清单里，**各选一个**训练框架、推理引擎、相邻库（共 3 个）。
   - 针对你选的工具，用一句话推测它「复用了 transformers 模型定义的哪个方面」（结构？权重格式？预处理？）。
3. **需要观察的现象**：你会发现这三个工具虽然职责不同，但都建立在「同一份模型定义」之上。
4. **预期结果**：你能说出例如「vLLM 复用 transformers 的模型结构和权重键名约定，再加上 PagedAttention 做推理加速」这样的判断。
5. 本实践为推理型阅读，无需运行命令；具体验证留待你实际接触这些工具时。

#### 4.3.5 小练习与答案

**练习 1**：为什么 README 要专门写一节「When shouldn't I use Transformers?」？
> **参考答案**：为了管理用户预期、避免误用。transformers 的定位很明确（模型定义框架 + 针对其 PyTorch 模型的训练 API），把它当成通用积木箱或通用训练循环都会失望。提前划清边界，反而凸显了它在「模型定义集中化」这一本职上的不可替代性。

**练习 2**：判断对错：「transformers 的模型定义被 vLLM 用了，所以 vLLM 是 transformers 的一部分。」
> **参考答案**：错。vLLM 是**独立**的推理引擎，它**依赖/复用** transformers 的模型定义，但并不属于 transformers。关系是「上下游依赖」，不是「包含」。这也正是 4.1 里「pivot（枢纽）」一词的精确含义——枢纽连接各方，但各方各自独立。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿性小任务（对应规格中要求的核心实践）：

**任务：用自己的话，写出 transformers 解决的三个核心问题，并列出两个依赖它的上下游工具。**

操作步骤：

1. 重新通读 [README.md:69-80](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/README.md#L69-L80) 与 [docs/source/en/philosophy.md:19-55](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/philosophy.md#L19-L55)。
2. 在笔记里用**自己的话**（不要照抄英文原文）回答：
   - **问题一**：transformers 用「集中化模型定义」解决了什么原本存在的痛点？（提示：碎片化 / 重复实现 / 不一致）
   - **问题二**：它如何降低使用最先进模型的门槛？（提示：三类对象 + 统一的 `from_pretrained`）
   - **问题三**：它如何让一个模型「一次定义，处处可用」？（提示：枢纽连接训练框架与推理引擎）
3. 从 README 第 72–75 行的工具清单里，挑 **两个** 上下游工具（例如一个训练框架 + 一个推理引擎），各用一句话说明它们与 transformers 的依赖关系。
4. **预期结果**：得到一份不超过 200 字的中文小结，包含三个核心问题 + 两个工具及其依赖关系。

> 参考思路（请先自己写，再对照）：核心痛点是「模型定义碎片化」——每个训练/推理工具若各自实现模型，会导致同一 checkpoint 行为不一致。transformers 把定义集中起来，使生态约定一致；它用「config + model + 预处理」三类对象和统一的 `from_pretrained` 降低门槛；它作为枢纽，让 vLLM（推理引擎）和 DeepSpeed（训练框架）等工具复用同一份定义。

---

## 6. 本讲小结

- transformers 的精准定位是 **「模型定义框架（model-definition framework）」**，它集中定义模型结构/超参/预处理，同时服务于训练和推理。
- 它的核心理念是 **「集中化模型定义」**：一份定义被整个生态共同接受，从而成为连接各类工具的 **pivot（枢纽）**。
- 上游下游关系：训练框架（Axolotl/Unsloth/DeepSpeed/FSDP/PyTorch-Lightning）、推理引擎（vLLM/SGLang/TGI）、相邻库（llama.cpp/mlx）都**复用** transformers 的模型定义。
- 每个模型由 **三类核心对象** 构成：Configuration、Model、Preprocessing，它们共享 `from_pretrained / save_pretrained / push_to_hub` 三个统一方法。
- 八条 **核心准则**（如 One Model, One File、Code is the Product、Standardize Don't Abstract）解释了源码中那些「反直觉」的设计选择。
- transformers 有明确的 **边界**：不是积木箱、训练 API 只为其 PyTorch 模型优化、示例只是示例。

---

## 7. 下一步学习建议

本讲建立了「transformers 是什么、为什么这么做」的认知坐标。接下来建议：

1. **下一讲 u1-l2「环境安装与首次运行」**：亲手把环境搭起来，跑通第一段推理代码，把抽象定位变成可触摸的体验。
2. **之后 u1-l3「源码目录结构地图」**：把本讲提到的「三类对象」「枢纽角色」落实到 `src/transformers/` 的真实目录上。
3. **延展阅读**：philosophy.md 里提到的 [Transformers-tenets](https://huggingface.co/spaces/transformers-community/Transformers-tenets) 长文（带示例与时间线），是核心准则的权威参考，学有余力时可深入。
4. **先别急着读模型源码**：在还没建立目录地图（u1-l3）和 `from_pretrained` 范式（u2）之前，直接钻进 `modeling_llama.py` 容易迷失。按手册顺序推进会更稳。

# 讲义 u1-l1：项目定位与生态

## 1. 本讲目标

本讲是整本 SpecForge 学习手册的第一篇，目标是帮你建立「SpecForge 到底是什么」的第一印象。读完本讲，你应当能够：

- 说清楚 SpecForge 在「大模型推理加速」这件事里扮演的角色，以及它和 SGLang 服务框架的关系。
- 列出 SpecForge 当前支持的全部草稿方法（draft methods），并能看懂 README 里的方法矩阵表。
- 理解 SpecBundle 是什么、它解决了什么问题，以及它如何体现 SpecForge 的工程价值。

本讲**不要求你写过任何训练代码**，也不要求你懂投机解码的数学推导。我们只建立直觉和全局认知——更深的原理会在后续讲义（u1-l3、u1-l4）展开。

---

## 2. 前置知识

在开始前，先用最朴素的语言把几个词讲清楚。如果你已经熟悉，可以跳过本节。

- **大语言模型（LLM）推理**：把一个训练好的模型部署成服务，用户发一句 prompt，模型一个一个地「吐」出 token（词片段）作为回答。这个逐 token 生成的过程叫**自回归生成（autoregressive generation）**。
- **目标模型（target model）**：用户真正想用的那个又大又准的模型，比如 Qwen3-8B、Llama-3.1-8B。它很准但生成慢。
- **草稿模型（draft model）**：一个体积小、生成快的小模型。它先「猜」几个 token，再交给目标模型一次性验证。猜对的就直接用，猜错的就纠正。
- **投机解码（speculative decoding）**：上面这套「小模型先猜 + 大模型验证」的技术总称。它能在**不改变最终输出**的前提下让推理变快。
- **SGLang**：一个高性能的大模型服务框架（serving framework），负责把模型真正跑起来对外提供服务。SpecForge 和 SGLang 是同一个团队（sgl-project）维护的姊妹项目。

一句话概括它们的关系：**SpecForge 负责「训练草稿模型」，SGLang 负责「用草稿模型加速服务」**。本讲后面会把这句话拆开讲透。

> 术语提示：本手册里「草稿方法（draft method / algorithm）」指一种训练草稿模型的算法（如 EAGLE3、DFlash）；「草稿模型（draft model）」指训练产出的那个具体小模型。两者是「方法」和「产物」的关系。

---

## 3. 本讲源码地图

本讲是定位性讲义，引用的不是 Python 源码，而是项目的「门面文档」。这些文档是了解 SpecForge 最权威、最新的入口：

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| [README.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/README.md) | 项目首页，给出定位、支持的草稿方法矩阵、SpecBundle 入口 | 建立「SpecForge 是什么」的整体认知 |
| [docs/get_started/about.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/get_started/about.md) | 项目动机与「SGLang-ready」说明 | 理解为什么会有 SpecForge、它的三个承诺 |
| [docs/community_resources/specbundle.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/community_resources/specbundle.md) | SpecBundle 社区资源与已发布模型清单 | 了解 SpecForge 训练出的草稿模型如何形成生态 |

后续讲义（如 u1-l5「目录结构与源码地图」）才会进入 `specforge/` 这个 Python 包的内部源码。本讲只需读懂上面三份文档。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**README 概述**、**支持方法矩阵**、**SpecBundle 资源**。

---

### 4.1 README 概述：SpecForge 是什么

#### 4.1.1 概念说明

很多同学第一次听到「投机解码训练框架」会有点懵：训练框架我知道（比如 PyTorch、HuggingFace Trainer），但为什么要专门为「投机解码」做一个训练框架？

原因在于：**草稿模型不是随便拿个小模型就能用的**。草稿模型必须和目标模型「配合默契」——它要能猜中目标模型会输出什么。这种配合关系需要专门的训练流程（比如要从目标模型里抽取隐藏状态来当训练信号）。市面上的开源投机解码项目大多是研究原型，能跑但不稳定，而且训练出来的草稿模型往往**无法直接导入 SGLang 服务**，还要自己写转换脚本。

SpecForge 就是为了填补这个空缺而生的。它是 **SGLang 团队官方出品的生态项目**，定位非常明确：**一个用来训练投机解码草稿模型的框架，训练产物可以平滑地导入 SGLang 来加速推理。**

#### 4.1.2 核心流程

理解 SpecForge 的定位，最简单的方式是看它在「模型从训练到上线」整条链路里的位置：

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐     ┌──────────────────┐
│  目标模型 + 数据  │ ──▶ │   SpecForge 训练   │ ──▶ │  specforge export │ ──▶ │  SGLang 服务加速   │
│  (大模型已就绪)   │     │  (产出草稿模型)    │     │  (物化为服务目录)  │     │  (用草稿模型提速)  │
└─────────────────┘     └──────────────────┘     └─────────────────┘     └──────────────────┘
```

四个关键点：

1. **输入**：一个已经训好的目标模型（你最终要服务的大模型）+ 训练数据。
2. **训练**：SpecForge 用目标模型当「老师」，训练出一个配合度高的草稿模型。
3. **导出**：用 `specforge export` 把训练检查点物化成 SGLang 能直接加载的服务目录。
4. **服务**：SGLang 加载目标模型 + 草稿模型，用投机解码对外提供更快的推理服务。

SpecForge 的「定位」就卡在第 2、3 步：**专注训练 + 导出，不负责推理服务本身**（那是 SGLang 的事）。这种清晰分工是它「直接兼容 SGLang」承诺的基础。

#### 4.1.3 源码精读

我们直接读 README 的 Overview 段落。下面是 [README.md:14-26](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/README.md#L14-L26) 中关于定位的核心描述：

> SpecForge is an ecosystem project developed by the SGLang team. It is a framework for training speculative decoding models so that you can smoothly port them over to the SGLang serving framework to speed up your inference.

这句话点明了三件事：作者是 SGLang 团队、用途是训练投机解码模型、目的是导入 SGLang 加速推理。

紧接着，README 用一个无序列表给出了 SpecForge 相对其它开源项目的**三个核心承诺**（[README.md:18-23](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/README.md#L18-L23)）：

- **regularly maintained**：由 SpecForge 团队持续维护，代码开箱即用（runnable out-of-the-box）。
- **directly compatible with SGLang**：直接兼容 SGLang，无需额外的移植工作。
- **统一运行时**：通过**同一个运行时**提供本地离线（local offline）和服务端在线分离（server-only online-disaggregated）两种训练，并支持相应的数据并行、张量并行、序列并行拓扑。

第三条是技术含量最高的一条，也是 SpecForge 区别于「单脚本研究项目」的关键——它不是把 online 和 offline 拼成两套代码，而是用**一套运行时**统一支撑。这个点会在后续进阶讲义（u3「入口与启动链路」、u7「DataFlow 运行时」）深入展开，本讲先记住结论。

`docs/get_started/about.md` 用几乎相同的措辞再次确认了这三个承诺（[about.md:5-11](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/get_started/about.md#L5-L11)），并额外强调了对 **CUDA / ROCm / Ascend** 三类硬件的可移植性。这说明 SpecForge 不绑死 NVIDIA，也支持 AMD GPU 和华为昇腾 NPU。

#### 4.1.4 代码实践

这是一个纯阅读型实践，目标是让你亲手从源码里「挖」出定位信息。

1. **实践目标**：用一句话总结 SpecForge 是什么，并指出它的三个核心承诺。
2. **操作步骤**：
   - 打开项目根目录的 [README.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/README.md)，阅读 `## 📍 Overview` 段落。
   - 再打开 [docs/get_started/about.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/get_started/about.md)，阅读 `## 💡 Motivation` 段落。
3. **需要观察的现象**：两份文档对「三个承诺」的描述高度一致，但 about.md 多了一句关于硬件可移植性（CUDA/ROCm/Ascend）和评测、检查点选择的补充。
4. **预期结果**：你能写出类似下面这样的总结——
   > SpecForge 是 SGLang 团队出品的投机解码草稿模型训练框架，三个核心承诺是：① 持续维护、开箱即用；② 训练产物直接兼容 SGLang、无需移植；③ 用一套运行时统一支撑在线/离线训练及多种并行拓扑。
5. 本实践无需运行任何命令，属于「文档阅读型」任务。

#### 4.1.5 小练习与答案

**练习 1**：SpecForge 和 SGLang 各自负责什么？它们为什么是两个项目而不是一个？

> **参考答案**：SpecForge 负责「训练草稿模型」和「把检查点导出成服务目录」，SGLang 负责「加载模型对外提供推理服务」。分开是因为训练和服务是两套截然不同的工程问题（训练关心梯度、优化器、分布式数据并行；服务关心吞吐、延迟、批处理），独立演进更清晰，但同一团队保证了它们的对接零成本。

**练习 2**：README 说 SpecForge 提供「local offline」和「server-only online-disaggregated」两种训练。请猜测「online」相比「offline」可能多了什么组件？

> **参考答案**：online 模式大概率需要在训练过程中**实时启动一个推理服务**（即 SGLang）来动态产生训练数据/特征，而 offline 模式则是预先把特征算好存成文件再训练。所以 online 多了「在线推理 + 特征流式传输」这一层（这与后续 u5-l3「离线特征生成」、u7「DataFlow 运行时」的内容呼应）。

---

### 4.2 支持方法矩阵：SpecForge 能训练哪些草稿

#### 4.2.1 概念说明

「草稿方法（draft method）」决定了草稿模型**如何猜测下一个 token**。不同方法的猜测策略不同，训练流程也不同。SpecForge 的一大设计亮点是：**所有方法共用同一个类型化训练入口**。

这意味着无论你用哪种方法，启动训练的命令长得几乎一样——区别只在于你给的配置文件（YAML）不同。这是 SpecForge「开箱即用」承诺在方法层面的体现：你不需要为每种方法学一套新的启动脚本。

#### 4.2.2 核心流程

SpecForge 当前支持 **6 种草稿方法**，可以用一张矩阵表来理解。每行是一种方法，关键信息包括：方法名、原理一句话、示例配置、可选的优化损失。

| 方法 | 一句话原理 | 示例配置 | 优化 |
| --- | --- | --- | --- |
| **EAGLE3** | 基于目标模型特征的自回归草拟（主力方法） | Online / Offline / Disaggregated offline | LK loss |
| **P-EAGLE** | 并行版 EAGLE | Online | — |
| **EAGLE3.1** | 带注意力漂移（attention drift）的 EAGLE3 | Online | — |
| **DFlash** | 块并行草拟（block-parallel drafting） | Online / Disaggregated | D-PACE |
| **Domino** | DFlash + GRU logit 修正 | Online / Disaggregated | — |
| **DSpark** | 置信度调度的半自回归生成 | Disaggregated | — |

从这张表能读出几个重要事实：

1. **EAGLE3 是绝对主力**——它是唯一同时提供 online、offline、disaggregated offline 三种示例配置的方法，也是 SpecBundle 已发布模型采用的方法（见 4.3 节）。
2. **方法之间存在演进关系**：Domino 是「DFlash + GRU 修正」，EAGLE3.1 是「EAGLE3 + 注意力漂移」。理解 EAGLE3 和 DFlash 两条主线，就能快速理解其它方法。
3. **不是所有组合都支持**。比如 DSpark 目前只有 disaggregated 配置；某些方法只支持 online。SpecForge 的处理方式很硬核：**不支持的组合会在配置校验或装配阶段直接报错拒绝，而不是悄悄回退到旧 trainer**。这避免了「你以为在用 A 方法，其实在用 B」的隐蔽 bug。

#### 4.2.3 源码精读

我们先看「统一入口」这句话在 README 里的原文（[README.md:29-41](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/README.md#L29-L41)）：

> Every method uses the same typed training entry point:
> ```bash
> specforge train --config examples/configs/qwen3-8b-eagle3-disaggregated.yaml
> ```
>
> The typed `deployment.trainer` topology self-launches trainer DP and EAGLE3 offline USP process groups. ... There are no method-specific Python training entry points.

最后一句话至关重要：**没有针对单个方法的 Python 训练入口**。无论 EAGLE3 还是 DSpark，都走 `specforge train` 这一个命令，靠配置文件里的 `strategy` 字段来区分（这一点会在 u4「算法注册与契约」、u6「训练主链路」深入）。

接着是完整的方法矩阵表（[README.md:43-50](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/README.md#L43-L50)），表里为每种方法都附了真实的示例配置链接，例如 EAGLE3 的离线配置是 [examples/configs/qwen3-8b-eagle3-offline.yaml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3-8b-eagle3-offline.yaml)。这些配置文件在仓库里**真实存在**（你可以在 `examples/configs/` 目录下找到 60+ 个 `.yaml`），所以矩阵表不是空头承诺。

README 还特别提醒（[README.md:52-56](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/README.md#L52-L56)）：不支持的「方法 × 拓扑」组合会在**配置校验或装配阶段（config validation or run assembly）**被拒绝，而不是回退。这是一种「快速失败（fail fast）」的工程哲学——越早暴露错误，越不容易在生产中踩坑。

#### 4.2.4 代码实践

1. **实践目标**：从仓库里亲手清点 SpecForge 支持的草稿方法，并验证示例配置真实存在。
2. **操作步骤**：
   - 在项目根目录列出所有示例配置（在终端执行，属于只读操作）：
     ```bash
     ls examples/configs/*.yaml
     ```
   - 对照 README 的方法矩阵表（[README.md:43-50](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/README.md#L43-L50)），把每个方法对应的配置文件名记下来。
3. **需要观察的现象**：你会看到大量以 `eagle3`、`dflash`、`domino`、`dspark`、`peagle` 命名的 yaml 文件，覆盖了从 0.5B 小模型到 480B 大模型（如 `qwen3-coder-480b-a35b-eagle3`）的各种目标模型。
4. **预期结果**：你能列出 6 个方法名（EAGLE3、P-EAGLE、EAGLE3.1、DFlash、Domino、DSpark），并为每个方法至少找到一个真实存在的示例配置文件。
5. 注意：本实践只做文件清点，**不要**修改任何 yaml，也**不要**尝试真正运行 `specforge train`（那需要 GPU 环境，留到 u2-l1「五分钟跑通一次训练」）。

#### 4.2.5 小练习与答案

**练习 1**：README 说「There are no method-specific Python training entry points」。如果 EAGLE3 和 DFlash 共用同一个 `specforge train` 命令，那它们是靠什么区分的？

> **参考答案**：靠配置文件（YAML）里的字段来区分——最关键的是 `training.strategy` 字段（选择算法）以及 `model` 段里草稿架构的声明。同一个入口会根据配置解析出不同的算法契约（AlgorithmSpec）和训练策略（DraftTrainStrategy）。详见后续 u4-l1、u6-l2。

**练习 2**：为什么 SpecForge 选择「不支持就报错」而不是「不支持就回退到一个通用 trainer」？

> **参考答案**：因为回退会造成**静默的行为偏差**——用户以为在用某种高性能方法，实际跑的是降级版本，结果接受率低、加速不明显，却很难定位原因。显式报错（fail fast）能让用户立刻知道这个「方法 × 拓扑」组合不可用，避免隐蔽的生产事故。这是对工程可靠性的取舍。

---

### 4.3 SpecBundle 资源：训练产物如何形成生态

#### 4.3.1 概念说明

光有一个训练框架还不够。即使 SpecForge 再好用，如果社区里没有**高质量的草稿模型权重**可以直接下载使用，大多数用户还是会望而却步——毕竟训练一个草稿模型需要 GPU、数据、调参经验。

**SpecBundle** 就是补上这一环的生态项目。它是 SpecForge 团队联合产业伙伴发布的**一批生产级（production-grade）草稿模型集合**。简单说：**SpecForge 是「造草稿模型的工厂」，SpecBundle 是「工厂产出的成品货架」**。

SpecBundle 的核心价值主张是：相比已有的开源检查点，这些模型在**更广的领域范围内有更高的接受率（acceptance rate）**，配合 SGLang 可以获得**最高约 4 倍的推理加速**（见 [README.md:59-61](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/README.md#L59-L61)）。

> 术语提示：**接受率（acceptance rate / token acceptance rate）**指草稿模型猜的 token 中被目标模型接受的比例。接受率越高，省下的计算越多，加速越明显。

#### 4.3.2 核心流程

要理解 SpecBundle 为什么重要，得先看它要解决的**三个痛点**。`docs/community_resources/specbundle.md` 在开头把它们讲得很清楚（[specbundle.md:10-14](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/community_resources/specbundle.md#L10-L14)）：

1. **缺少生产级训练基础设施**：现有投机解码工具链大多是研究原型，系统级优化不足，对多样化架构和大规模模型支持差。
2. **缺少高质量草稿模型**：公开可用的 EAGLE3 兼容检查点极少，基本都来自原论文作者。
3. **已有草稿模型训练规模不足**：大多在小规模或精挑数据上训练，难以泛化到现代 LLM 训练用的大规模、多样化语料，导致接受率低、实际加速有限。

SpecBundle 的应对策略是双轮驱动（[specbundle.md:16](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/community_resources/specbundle.md#L16)）：开源社区 + 产业伙伴（包括 **Ant Group 蚂蚁、Meituan 美团、Nex-AGI、EigenAI**）。这同时解决了「基础设施」（由 SpecForge 框架本身承担）和「模型规模/质量」（由多方贡献大规模训练的检查点）两个问题。

用一张图概括 SpecForge 与 SpecBundle 的协作：

```
        SpecForge (框架/工厂)                    SpecBundle (成品/货架)
  ┌────────────────────────────┐         ┌────────────────────────────┐
  │  训练草稿模型的工程能力        │  产出 ▶  │  生产级草稿模型权重集合        │
  │  - 统一入口 / 多方法 / 多拓扑  │ ──────▶ │  - 高接受率 / 跨领域泛化       │
  │  - 直接导出 SGLang            │         │  - 多伙伴联合发布             │
  └────────────────────────────┘         └────────────────────────────┘
                    │                                  │
                    └──────────── 用户直接下载使用 ──────┘
```

值得注意的是，SpecBundle **也充当了 SpecForge 框架本身的「验证场」**——通过在多种规模和架构上发布模型，反过来证明 SpecForge 框架的健壮性（[specbundle.md:16](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/community_resources/specbundle.md#L16)）。

#### 4.3.3 源码精读

SpecBundle 的「使用入口」非常简单——它直接以 SGLang 服务参数的形式提供。下面是 [specbundle.md:24-36](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/community_resources/specbundle.md#L24-L36) 给出的启动命令（**示例代码，摘自官方文档**）：

```bash
python3 -m sglang.launch_server \
    --model <target-model-path> \
    --speculative-algorithm EAGLE3 \
    --speculative-draft-model-path <draft-model-path> \
    --speculative-num-steps 3 \
    --speculative-eagle-topk 1 \
    --speculative-num-draft-tokens 4
```

从这条命令能看出 SpecForge 生态的「无缝衔接」设计：

- `--model` 指向**目标模型**（大模型）。
- `--speculative-draft-model-path` 指向**草稿模型**——这正是 SpecForge 训练、SpecBundle 发布的产物。
- `--speculative-algorithm EAGLE3` 指定草稿方法，和 SpecForge 训练时用的方法一一对应。
- 其余参数（`num-steps`、`topk`、`num-draft-tokens`）是投机解码的树/链结构控制参数。

也就是说，SpecBundle 模型本质上就是「目标模型 + 草稿模型 + 一组 SGLang 参数」的组合包。

SpecBundle 已经覆盖了主流开源 LLM 家族（[specbundle.md:38-94](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/community_resources/specbundle.md#L38-L94)），包括：

| 系列 | 代表目标模型 |
| --- | --- |
| Llama | Llama-3.1-8B、Llama-3.3-70B、Llama-4-Scout/Maverick |
| Qwen | Qwen3-30B-A3B、Qwen3-235B-A22B、Qwen3-Next-80B |
| Qwen Coder | Qwen3-Coder-30B-A3B、Qwen3-Coder-480B-A35B |
| Ling / Kimi / GPT-OSS / Nex | Ling-flash-2.0、Kimi-K2、gpt-oss-20b/120b 等 |

每个模型都附了 Hugging Face 权重链接和「再生数据集（Regenerated Dataset）」链接。这个「再生数据集」其实就是 SpecForge 训练时用的特征数据，后续 u5-l1「数据集准备」会专门讲它为什么能提升接受率。

最后，README 的 SpecBundle 段还汇总了三个权威入口（[README.md:63-68](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/README.md#L63-L68)）：文档、性能看板（Performance Dashboard）、Hugging Face 合集。其中性能看板可以查到每个 SpecBundle 模型在各 benchmark 上的实测加速数据。

#### 4.3.4 代码实践

1. **实践目标**：理解 SpecBundle 模型如何被消费，并找到一个可以直接用的草稿模型权重。
2. **操作步骤**：
   - 打开 [docs/community_resources/specbundle.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/community_resources/specbundle.md)，滚动到 `## Released Models` 段。
   - 在「Llama Series」表里找到 `meta-llama/Llama-3.1-8B-Instruct` 这一行，点击它的「🤗 Model」链接（指向 `lmsys/SGLang-EAGLE3-Llama-3.1-8B-Instruct-SpecForge`）。
   - 打开 README 的 SpecBundle 资源表（[README.md:63-68](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/README.md#L63-L68)），点击「Performance Dashboard」链接查看实测加速比。
3. **需要观察的现象**：Hugging Face 上的草稿模型仓库通常包含 `config.json`、草稿权重文件，以及说明它配合哪个目标模型使用的 README。
4. **预期结果**：你能说出「一个 SpecBundle 模型 = 目标模型路径 + 草稿模型路径 + 一组 SGLang speculative 参数」，并且知道去哪里下载现成的草稿权重而不必自己训练。
5. 本实践**不需要 GPU**，纯网页浏览。如果你想在本地真正启动服务验证加速，需要先安装 SGLang 并准备目标模型权重——这超出了本讲范围，可记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：SpecBundle 解决的三个痛点里，哪一个是由 SpecForge 框架本身直接解决的？

> **参考答案**：第一个痛点「缺少生产级训练基础设施」由 SpecForge 框架本身解决（提供统一入口、多方法、多拓扑、导出能力）。第二、三个痛点（高质量模型、训练规模）则由 SpecBundle 通过联合产业伙伴发布大规模训练的检查点来解决。

**练习 2**：SpecBundle 的启动命令里 `--speculative-algorithm EAGLE3` 和 SpecForge 训练时的什么概念对应？

> **参考答案**：对应 SpecForge 训练配置里的草稿方法（`training.strategy` 选定的算法）。训练时用 EAGLE3 训出的草稿模型，服务时也必须声明 `--speculative-algorithm EAGLE3`，方法必须前后一致，否则草稿模型和目标模型的「协作协议」对不上。

**练习 3**：为什么 SpecBundle 既是「成品货架」，又能反过来验证 SpecForge 框架？

> **参考答案**：因为 SpecBundle 的每一个模型都是用 SpecForge 框架训练出来的。在从 0.5B 到 480B、从 Llama 到 Qwen 的多种规模和架构上都能稳定产出高质量草稿模型，本身就证明了 SpecForge 框架的健壮性和可扩展性。这是「吃自己的狗粮（dogfooding）」的工程实践。

---

## 5. 综合实践

本讲的综合实践把三个模块串起来，完成一次完整的「认知闭环」。

**任务：为 SpecForge 写一份一页纸的「项目速览」**

请结合本讲读过的三份文档（README、about.md、specbundle.md），产出一份包含以下四节的小结文档（可以写在笔记里，不必提交到仓库）：

1. **一句话定位**：用一句话说清 SpecForge 是什么、给谁用、解决什么问题。
2. **三个核心承诺**：逐条列出 SpecForge 相对其它开源投机解码项目的差异化优势（提示：见 [README.md:18-23](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/README.md#L18-L23)）。
3. **方法矩阵**：列出 6 种草稿方法名称，并标注哪种是「主力方法」（提示：看哪种方法示例配置最多）。
4. **生态闭环**：画一张从「目标模型」到「SGLang 加速服务」的流程图，标出 SpecForge（训练/导出）和 SpecBundle（现成权重）分别出现在哪个环节。

**验收标准**：

- 定位句必须同时提到「训练草稿模型」和「SGLang」两个关键词。
- 三个承诺必须包含「开箱即用 / 直接兼容 SGLang / 统一运行时」这三层意思。
- 方法矩阵能正确识别 EAGLE3 为主力方法。
- 流程图能体现「SpecForge 产出的草稿模型 = SpecBundle 货架上的商品」这层关系。

完成后，你就建立了阅读后续所有讲义所需的全局认知。

---

## 6. 本讲小结

- **SpecForge 是 SGLang 团队出品的投机解码草稿模型训练框架**，定位是「训练 + 导出」，加速推理交给 SGLang。
- 三个核心承诺：**持续维护开箱即用**、**直接兼容 SGLang 无需移植**、**一套运行时统一支撑在线/离线训练与多种并行拓扑**（见 [README.md:18-23](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/README.md#L18-L23)）。
- 所有草稿方法共用**同一个类型化训练入口** `specforge train --config ...`，没有方法专属的 Python 入口（见 [README.md:29-41](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/README.md#L29-L41)）。
- 当前支持 **6 种草稿方法**：EAGLE3（主力）、P-EAGLE、EAGLE3.1、DFlash、Domino、DSpark；不支持的「方法 × 拓扑」组合会在校验/装配阶段直接报错（见 [README.md:43-56](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/README.md#L43-L56)）。
- **SpecBundle** 是 SpecForge 团队联合产业伙伴发布的生产级草稿模型集合，解决「缺基础设施 / 缺高质量模型 / 训练规模不足」三大痛点，配合 SGLang 可获最高约 4x 加速（见 [specbundle.md:10-16](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/community_resources/specbundle.md#L10-L16)）。
- SpecForge 与 SpecBundle 是「工厂」与「货架」的关系：框架产出模型，模型反过来验证框架。

---

## 7. 下一步学习建议

本讲建立了全局认知，接下来建议按以下顺序继续：

1. **先动手装好环境**：进入 [u1-l2 安装与环境准备](./u1-l2-install-and-env.md)，学会用 `uv`/`pip` 安装 SpecForge，并确认 `specforge --help` 的三个子命令可用。这是后续所有实践的前提。
2. **补齐原理直觉**：如果你对投机解码还只停留在「小模型猜 + 大模型验证」的模糊印象，强烈建议先读 [u1-l3 投机解码原理](./u1-l3-speculative-decoding.md)，理解 prefill/草拟/验证三阶段和「为何能保证输出不变」。
3. **深入主力方法**：原理清楚后，读 [u1-l4 EAGLE3 特征式草稿原理](./u1-l4-eagle3-concepts.md)，理解为什么 EAGLE3 是 SpecForge 的主力方法。
4. **建立源码地图**：当你准备好进入代码，[u1-l5 目录结构与源码地图](./u1-l5-source-map.md) 会带你通览 `specforge/` 包的目录划分，为进阶层（u2 之后）做铺垫。

如果时间有限，**最低限度**也请先完成 u1-l2（装好能跑）和 u1-l5（知道代码在哪），这样后续讲义里的源码引用你都能跟得上。

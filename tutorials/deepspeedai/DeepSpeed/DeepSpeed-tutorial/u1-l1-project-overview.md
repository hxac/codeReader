# DeepSpeed 项目全景：大模型训练为什么需要它

> 本讲是整本学习手册的第一讲。你不写一行 DeepSpeed 代码，只需要建立三样东西：对项目定位的认知、对文档地图的熟悉、对版本机制的把握。它们是后续所有源码阅读的坐标系。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 DeepSpeed 解决的三大核心痛点：**显存墙**（模型放不进显存）、**通信墙**（多卡之间数据搬运太慢）、**训练吞吐**（整体训练太慢）。
2. 列举 DeepSpeed 的代表性系统创新：**ZeRO**、**ZeRO-Infinity / ZeRO-Offload**、**3D-Parallelism**、**Ulysses 序列并行**、**DeepSpeed-MoE** 等。
3. 知道仓库当前版本（0.19.9）、文档入口（deepspeed.ai 与 `docs/` 目录），以及它与 HuggingFace Transformers / Accelerate / Lightning 等框架的集成关系。
4. 学会用 `git log` 观察一个开源项目「最近在忙什么」，把 README 的新闻列表和实际代码提交互相印证。

## 2. 前置知识

本讲几乎不需要分布式训练背景，但以下概念会反复出现，先用大白话解释：

- **参数量 Ψ（psi）**：模型里所有可训练权重标量的总数。一个 7B 模型有约 70 亿个参数。显存讨论中常用 Ψ 代指「参数个数」。
- **数据并行（Data Parallelism, DP）**：每张卡都持有**完整的**模型副本，各自吃不同批次的数据，反向传播后同步梯度。最易用，但每张卡都重复存一份模型状态——这就是显存浪费的根源。
- **显存墙（memory wall）**：以混合精度 + Adam 优化器为例，除了参数本身，还要存梯度和优化器状态（Adam 的一阶/二阶动量、fp32 主权重）。粗略估算，训练状态约占

  \[ 2\Psi(\text{fp16 参数}) + 2\Psi(\text{fp16 梯度}) + 12\Psi(\text{fp32 优化器状态}) = 16\Psi \text{ 字节} \]

  一个 7B 模型仅模型状态就需要约 112 GB，而单卡显存通常只有 40~96 GB——模型还没跑起来就「撞墙」了。
- **通信墙（communication wall）**：多卡训练要在卡间搬运梯度/参数。当通信时间接近甚至超过计算时间，加卡不再提速。
- **ZeRO（Zero Redundancy Optimizer）**：DeepSpeed 的招牌技术。核心想法一句话：数据并行下每张卡都存了**完整副本**，这是冗余的；ZeRO 把优化器状态、梯度、参数逐级**切分**到各卡上，用时再临时聚合，从而用「通信换显存」。细节在单元 5 展开，本讲只需记住它是 DeepSpeed 的立身之本。

## 3. 本讲源码地图

本讲涉及的文件很少，都是「项目门面」级别的文件：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目第一入口：新闻、定位、集成生态、硬件支持表、安装方式、论文列表 |
| `docs/_pages/training.md` | 官网「训练总览」页面的源文件，相当于官方版能力矩阵与特性清单 |
| `version.txt` | 单行文件，记录当前版本号（0.19.9），是发布线的源头 |
| `setup.py` | 安装脚本，从这里消费 `version.txt` |
| `deepspeed/__init__.py` | 主包入口，把版本号暴露为 `deepspeed.__version__` |

## 4. 核心概念与源码讲解

### 4.1 README 的能力矩阵

#### 4.1.1 概念说明

读一个大型开源项目的第一步不是看代码，而是读它的 README，并从中提炼出一张「能力矩阵」：**它解决什么问题、靠什么技术解决、和谁协作、支持什么硬件**。

DeepSpeed 的 README 把这些信息分散在几个板块里：

| README 板块 | 回答的问题 |
| --- | --- |
| Latest News（L27 起） | 项目最近在往哪个方向演进 |
| Extreme Speed and Scale（L71 起） | 项目一句话定位与核心创新点 |
| DeepSpeed Adoption（L79 起） | 哪些著名大模型用它训出来、与哪些框架集成 |
| Build Pipeline Status（L110 起） | 在哪些硬件平台上跑 CI（即官方背书的平台） |
| Installation / HW support（L123 起） | 怎么装、支持哪些加速器 |
| Publications（L223 起） | 每项技术对应的论文 |

#### 4.1.2 核心流程

把 README 当地图来「测绘」的流程：

1. 先看定位句（Extreme Speed and Scale 段），抓住核心创新关键词。
2. 再看 Latest News，按时间倒序扫一遍，找出反复出现的主题（这些就是活跃战线）。
3. 然后看 Adoption 与集成表，确认生态位：它是「库」，被上层框架调用，而不是取代它们。
4. 最后看 Build Pipeline / HW support 表，确认硬件矩阵，这决定了后续读源码时会遇到的多后端抽象（单元 3 会讲）。

#### 4.1.3 源码精读

**① 一句话定位与五大创新点。** README 在标题段直接列出了 DeepSpeed 的系统创新清单：

> 这些创新包括 ZeRO、ZeRO-Infinity、3D-Parallelism、Ulysses Sequence Parallelism、DeepSpeed-MoE 等。

见 [README.md:L71-L73](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/README.md#L71-L73)。这段话就是整本学习手册的「课程大纲」：单元 5 讲 ZeRO 系列，单元 6 讲 3D 并行 / Ulysses / MoE。README 还点出 DeepSpeed 训练过 MT-530B、BLOOM 等当时的最大模型，见 [README.md:L84-L95](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/README.md#L84-L95)。

**② Latest News：项目的活跃战线。** 新闻列表从 2026/05 一直排到 2025/06，见 [README.md:L27-L51](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/README.md#L27-L51)。几个值得记住的条目：

- Muon 优化器支持与 ZeRO-3 的 SDMA 卸载（2026/05，L29-L31）；
- SuperOffload 获 ASPLOS 2026 最佳论文提名（2026/03，L35）；
- Core API 更新：PyTorch 风格 backward 与低精度主权重（2025/12，L37）；
- ZenFlow 无停顿卸载引擎（2025/08，L47）。

折叠区「More news」里还有三条：DeepCompile（编译优化）、AutoTP（自动张量并行）、Ulysses-Offload（长上下文），见 [README.md:L55-L67](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/README.md#L55-L67)。这些名字后续单元都会对应到具体源码目录（如 `deepspeed/compile/`、`deepspeed/module_inject/auto_tp.py`、`deepspeed/runtime/zenflow/`）。

**③ 集成生态：DeepSpeed 是「被集成」的库。** README 用一张表列出与主流框架的集成文档，见 [README.md:L97-L106](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/README.md#L97-L106)：

- HuggingFace **Transformers**（L101）——最常见用法：`TrainingArguments` 里打开 `deepspeed` 即可；
- HuggingFace **Accelerate**（L102）；
- PyTorch **Lightning**、**MosaicML**、**Determined**、**MMEngine**（L103-L106）。

这说明 DeepSpeed 的生态位是**底层训练加速库**：用户通常不直接面对它，而是通过这些上层框架间接使用。但要看懂它为什么快、为什么省显存，必须回到源码——这正是本手册的目的。

**④ 硬件能力矩阵。** CI 表见 [README.md:L110-L121](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/README.md#L110-L121)：NVIDIA、AMD、CPU、Intel Gaudi（hpu）、Intel XPU、华为昇腾 NPU 都有独立 CI。社区贡献的硬件表见 [README.md:L141-L151](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/README.md#L141-L151)，列出各加速器的短名（`npu`、`hpu`、`cpu`、`xpu`、`sdaa`、`klxpu`）。这张表解释了为什么源码里会有一整套「加速器抽象层」（`deepspeed/accelerator/`，单元 3 精读）：一套代码要同时跑在这么多硬件上。

**⑤ 论文索引。** [README.md:L223-L262](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/README.md#L223-L262) 的 Publications 列表是按技术查论文的入口：ZeRO 原论文（L224）、ZeRO-Infinity（L229）、DeepSpeed-MoE（L233）、ZeRO++（L245）、ZenFlow（L260）、SuperOffload（L262）等。读源码卡住时，回这里找对应论文往往能豁然开朗。

#### 4.1.4 代码实践

**实践目标**：亲手从 README 提炼出一张属于自己的能力矩阵表。

**操作步骤**：

1. 打开仓库根目录的 `README.md`（本讲用 HEAD `dc046b15d` 的版本）。
2. 用编辑器搜索以下小标题并记下其起始行号：`Latest News`、`Extreme Speed`、`DeepSpeed Adoption`、`Build Pipeline Status`、`Installation`、`Contributed HW support`、`Publications`。
3. 为每个板块用一句话总结它回答的问题，填进 4.1.1 的表格里替换掉我们的答案。
4. 统计 Latest News 中 2025-2026 年条目里出现次数最多的技术关键词（提示：offload/ZenFlow/SuperOffload 相关条目出现了 4 次以上）。

**需要观察的现象**：你会发现 README 的新闻列表不是随机罗列——offload（卸载）方向（ZeRO-Offload → ZeRO-Infinity → ZenFlow → SuperOffload）和并行方向（3D → AutoTP → AutoEP）各自成线，每条线都能在 Publications 里找到对应论文、在源码目录里找到对应模块。

**预期结果**：得到一张 7 行左右的能力矩阵表，并且能回答「DeepSpeed 最近一年最重要的三个新方向是什么」。如果答案里包含「卸载引擎（ZenFlow/SuperOffload）」「自动并行注入（AutoTP/AutoEP）」「编译优化（DeepCompile）」之一，说明读对了。

#### 4.1.5 小练习与答案

**练习 1**：README 说 DeepSpeed 的核心创新包括哪五项？分别对应手册的哪个单元？

答案：ZeRO、ZeRO-Infinity、3D-Parallelism、Ulysses 序列并行、DeepSpeed-MoE（见 [README.md:L73](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/README.md#L73)）。对应关系：ZeRO → 单元 5；3D 并行与 Ulysses → 单元 6；MoE → 单元 6；ZeRO-Infinity（NVMe 交换）→ 单元 5 第 7 讲。

**练习 2**：如果你在用 HuggingFace Transformers 训练模型，README 里哪个链接告诉你如何配合 DeepSpeed 使用？

答案：集成表第一行的 [Transformers with DeepSpeed](https://huggingface.co/docs/transformers/deepspeed) 文档链接，见 [README.md:L101](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/README.md#L101)。

**练习 3**：README 的 CI 表为什么要为 AMD、CPU、Gaudi、XPU、昇腾分别设置流水线？这对源码结构有什么暗示？

答案：因为 DeepSpeed 明确支持多硬件平台（每个平台一个 CI workflow 保证不回归），见 [README.md:L110-L121](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/README.md#L110-L121)。源码结构上的暗示是：一定存在一层设备无关抽象把硬件差异挡住——即 `deepspeed/accelerator/`（单元 3 会精读），项目规则也禁止直接 `import torch.distributed` 而要求走统一通信层。

### 4.2 训练文档结构

#### 4.2.1 概念说明

`docs/` 目录是 DeepSpeed 官网（deepspeed.ai）的源文件，用 Jekyll 构建。其中 `docs/_pages/training.md` 是官网「Training Overview and Features」页面的源文件——可以把它理解为**官方版能力矩阵**：README 用一段话告诉你「有什么」，training.md 用整篇文档告诉你「每项特性具体是什么、怎么开」。

这个文件有个有趣的细节：它实际上包含**两个 Jekyll 页面**（两段 front matter），前半部分是 `/training/`（训练总览），后半部分是 `/features/`（特性详解）。读它时要意识到自己在「两页文档」之间穿越。

#### 4.2.2 核心流程

training.md 的叙事结构本身就是一条问题-方案链，推荐按这个顺序读：

```
训练大模型难（Overview, L9-17）
  ├─ 要自己搭分布式/混合精度/梯度累积/检查点 → DeepSpeed API 轻量包装 PyTorch（L19-27）
  ├─ 太慢 → Speed：BERT-large 44 分钟、GPT-2 3.75x（L29-52）
  ├─ 显存不够 → Memory efficiency：ZeRO、ZeRO-Offload（L56-77）
  ├─ 规模上天 → Scalability：3D 并行、万亿参数（L79-96）
  ├─ 网络带宽差 → Communication efficiency：流水线并行降通信（L98-100）
  ├─ 数据太多 → Data efficiency：课程学习、token dropping（L102-103）
  ├─ 收敛太慢 → LAMB 等大 batch 优化器（L105-111）
  └─ 都想要但怕难用 → Good usability：几行代码接入（L114-115）
```

前半部分以「叙事」收尾于一张特性清单（L118-163）；后半部分（L165 起）逐项展开每个特性的细节与配置片段。

#### 4.2.3 源码精读

**① 问题描述：为什么需要 DeepSpeed。** 文档开篇直接点出双重痛点——既要搭好分布式/混合精度这些「样板代码」，又可能得不到理想的性能；大模型在纯数据并行下显存直接爆掉，而模型并行又难用：

见 [training.md:L9-L17](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/docs/_pages/training.md#L9-L17)。这句「a large model easily runs out of memory with pure data parallelism」就是第 2 节讲的显存墙的官方表述。

**② 产品哲学：轻量包装，不另起炉灶。** DeepSpeed API 被定位为「PyTorch 之上的轻量包装」——你仍用 PyTorch 的一切，DeepSpeed 接管分布式训练、混合精度、梯度累积、检查点这些样板技术，见 [training.md:L19-L27](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/docs/_pages/training.md#L19-L27)。这解释了为什么手册单元 2 讲 `initialize()` 时你会看到：模型、优化器、数据加载器都还是 PyTorch 对象，DeepSpeed 只是「包」了一层引擎。

**③ 显存效率：ZeRO 的官方定义。** 文档给出了 ZeRO 的一句话定义——与普通数据并行「复制」模型状态不同，ZeRO 把模型状态和梯度「切分」到各数据并行进程上，见 [training.md:L62-L69](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/docs/_pages/training.md#L62-L69)。同一节还给出量化对比：单卡 13B 参数 vs 传统 DDP 约 1.4B 就 OOM（L57-L60），ZeRO-2 最多省 8 倍内存（L66-L67）；ZeRO-Offload 进一步用「单张 V100 训 13B」打开局面，见 [training.md:L77](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/docs/_pages/training.md#L77)。

**④ 可扩展性与 3D 并行。** 「3D 并行」= 数据并行 + 张量并行 + 流水线并行三者组合，官方文档说明它支撑过万亿参数模型，见 [training.md:L79-L88](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/docs/_pages/training.md#L79-L88)。这三个维度分别对应单元 5（ZeRO 数据并行）、单元 6（张量/序列并行、流水线并行）。

**⑤ 核心 API 清单。** 文档后半部分总结了 DeepSpeed 的核心 API，只有寥寥几个方法：`initialize`、`backward`、`step`、`add_config_arguments`、`load_checkpoint` / `store_checkpoint`，见 [training.md:L305-L314](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/docs/_pages/training.md#L305-L314)。这是单元 2 的预习提纲：整个训练引擎对用户暴露的入口就这么几个。

**⑥ 特性深读区（后半页）。** 从 [training.md:L233-L259](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/docs/_pages/training.md#L233-L259) 开始逐项解释 ZeRO 的子优化（优化器状态与梯度切分、激活切分、恒定缓冲区优化 CBO、连续内存优化 CMO）——这四个名词在单元 5 读 `stage_1_and_2.py` / `stage3.py` 时会一一对应到具体代码。ZeRO-Offload 的展开说明见 [training.md:L279-L283](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/docs/_pages/training.md#L279-L283)。

#### 4.2.4 代码实践

**实践目标**：熟悉 `docs/` 目录与官网页面的对应关系，学会「从文档反查源码目录」。

**操作步骤**：

1. 列出 `docs/_pages/` 下的所有文件（本讲实测共 7 个：`config-json.md`、`training.md`、`inference.md`、`deepspeed4science.md`、`posts-landing.md`、`posts_list_landing.md`、`tutorials-landing.md`）。
2. 打开 `docs/_pages/training.md`，观察第 1-7 行和第 165-171 行的两段 front matter，确认它们分别是 `/training/` 与 `/features/` 两个页面。
3. 在 training.md 中搜索 `zero`（不区分大小写），记下每个 ZeRO 相关小节的行号。
4. 在仓库根目录执行 `ls deepspeed/runtime/zero/`，把源码目录名（如 `stage_1_and_2.py`、`stage3.py`）与文档小节一一配对。
5. 打开 [config-json.md](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/docs/_pages/config-json.md)，找到 `train_batch_size` 的说明位置并加书签——它是单元 2 第 2 讲的主战场。

**需要观察的现象**：文档里的几乎每个特性小节，都能在 `deepspeed/` 包里找到一个同名或近名的模块（monitor → `deepspeed/monitor/`，flops profiler → `deepspeed/profiling/flops_profiler/`，MoE → `deepspeed/moe/`）。

**预期结果**：得到一张「文档小节 ↔ 源码目录」对照表（不少于 5 行）。此后遇到任何 DeepSpeed 功能问题，你知道先查哪个文档页、再进哪个源码目录。

#### 4.2.5 小练习与答案

**练习 1**：training.md 中 ZeRO 的官方一句话定义是什么？它和数据并行的本质区别在哪里？

答案：ZeRO 与「在数据并行进程间复制内存状态」的基本数据并行不同，它把模型状态（model states）和梯度**切分（partition）**到各进程以节省内存（见 [training.md:L62-L65](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/docs/_pages/training.md#L62-L65)）。本质区别：复制 vs 切分——切分换来的是显存节省，付出的是用时需临时通信聚合。

**练习 2**：文档说 DeepSpeed 的核心 API「只有一把手能数得过来的几个方法」，请列举它们。

答案：`initialize`（初始化）、`backward` 和 `step`（训练）、`add_config_arguments`（参数解析）、`load_checkpoint` 和 `store_checkpoint`（检查点），见 [training.md:L305-L310](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/docs/_pages/training.md#L305-L310)。

**练习 3**：「3D 并行」中的三个维度是什么？ZeRO 在其中扮演哪个维度？

答案：数据并行、张量/模型并行、流水线并行（见 [training.md:L80-L82](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/docs/_pages/training.md#L80-L82)）。ZeRO 强化的是数据并行这一维（内存高效的数据并行），且可与另外两类并行组合。

### 4.3 版本与发布线

#### 4.3.1 概念说明

「这个仓库现在是哪个版本？」看似简单，但在 DeepSpeed 里版本信息有一条清晰的**传递链**：

```
version.txt（唯一事实来源，当前 0.19.9）
   ├─→ setup.py 打包时读取 → PyPI 发布的包版本
   └─→ (构建期写入) deepspeed/git_version_info.py
          └─→ deepspeed/__init__.py 导出 __version__ / __git_hash__ / __git_branch__
```

理解这条链的意义在于：**用户看到的 `deepspeed.__version__` 与仓库里的 `version.txt` 是同源的**；而且 DeepSpeed 还把 git 提交哈希打进包里，运行时日志会打印 version + git-hash + git-branch，这对你排查「线上到底跑的是哪份代码」极其有用（阅读源码时也能用它确认代码与讲义引用的 HEAD 一致）。

另一个要点是**发布节奏**：DeepSpeed 是「rolling master」模式——master 分支持续合入，定期打 tag 发 PyPI。所以 `git log` 里的最新提交就是最真实的项目动态，比新闻列表更细粒度。

#### 4.3.2 核心流程

版本信息的生命周期：

1. 开发期：`version.txt` 由维护者更新，`0.19.9` 表示主版本 0、次版本 19、补丁版本 9。
2. 构建期：`setup.py` 读取 `version.txt`；如果设置了 `DS_BUILD_STRING` 环境变量则附加 `.devX` 后缀，否则把 git 哈希写入版本串，并生成 `deepspeed/git_version_info.py`。
3. 运行期：`import deepspeed` 时从 `git_version_info` 导入 version / git_hash / git_branch，暴露为 `deepspeed.__version__`、`deepspeed.__git_hash__`。
4. 日志期：每次 `initialize()` 都会打印 `DeepSpeed info: version=..., git-hash=..., git-branch=...`。

#### 4.3.3 源码精读

**① 唯一事实来源：version.txt。** 整个文件只有一行：

见 [version.txt:L1](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/version.txt#L1)——内容就是 `0.19.9`。这就是本手册撰写时仓库的版本号，也就是大纲中所说的 v0.19.9。

**② setup.py 消费版本号。** 安装脚本在打包时读取该文件：

见 [setup.py:L228-L229](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L228-L229)，注释明确写着「Parse the DeepSpeed version string from version.txt」。紧随其后（L231 起）的逻辑决定是否附加 `.dev` 后缀或 git 哈希。

**③ 包入口导出版本三元组与 git 信息。** `import deepspeed` 时：

见 [deepspeed/__init__.py:L70](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/__init__.py#L70)，从 `git_version_info`（构建时生成）导入 version、git_hash、git_branch；随后在 [deepspeed/__init__.py:L75-L85](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/__init__.py#L75-L85) 解析出主/次/补丁三元组并导出 `__version__`、`__git_hash__`、`__git_branch__`。同一个文件里，[deepspeed/__init__.py:L88](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/__init__.py#L88) 附近的 `dist = None` 是个伏笔：分布式通信句柄被设计为延迟绑定（引擎初始化时才指向 `deepspeed.comm`），单元 3 会展开。

顺带一提，`deepspeed/__init__.py:498` 与 `721` 附近各有一行 `log_dist("DeepSpeed info: version={}, git-hash={}, git-branch={}"...)`——这就是运行时日志里版本行的出处。

#### 4.3.4 代码实践

**实践目标**：验证版本传递链的三环都指向同一个版本号。

**操作步骤**：

1. `cat version.txt` → 记下输出（预期 `0.19.9`）。
2. `grep -n "version.txt" setup.py` → 确认 setup.py 第 229 行在读取它。
3. 若本地已安装 deepspeed：`python -c "import deepspeed; print(deepspeed.__version__, deepspeed.__git_hash__)"` → 对比输出与第 1 步结果，并核对 `__git_hash__` 的前 9 位是否等于 `git rev-parse --short=9 HEAD`（本仓库 HEAD 为 `dc046b15d`）。
4. `pip index versions deepspeed 2>/dev/null || pip install deepspeed==` → 观察 PyPI 上可用的版本列表，看看 0.19.9 处在发布线的什么位置。

**需要观察的现象**：第 3 步中 `__version__` 与 `version.txt` 一致；若你是 `pip install deepspeed` 装的正式发行版，`__git_hash__` 是打包那次提交的哈希；若是源码 editable 安装（`pip install -e .`），哈希应与你本地 HEAD 一致。

**预期结果**：三个环节读出的版本号完全一致。第 3、4 步依赖本地 Python 环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：DeepSpeed 的版本号「唯一事实来源」是哪个文件？当前值是多少？

答案：仓库根目录的 `version.txt`，当前值为 `0.19.9`（见 [version.txt:L1](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/version.txt#L1)）。

**练习 2**：为什么 `deepspeed.__version__` 之外还要导出 `__git_hash__`？举一个实际用途。

答案：因为 PyPI 版本号更新有间隔，而 rolling master 每天都有合入，仅凭 `0.19.9` 无法区分「同一版本号下不同的代码状态」。`__git_hash__`（见 [deepspeed/__init__.py:L84](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/__init__.py#L84)）能精确定位到具体提交。实际用途：报 bug 时把 `initialize()` 日志里的 version/git-hash 行贴出来，维护者即可复现你的代码版本；本手册也用它保证所有永久链接指向同一提交。

**练习 3**：如果不小心删除了 `version.txt`，安装（`pip install -e .`）会在哪一步失败？

答案：在 `setup.py` 执行到 [setup.py:L229](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L229) 的 `open('version.txt', 'r')` 时直接抛 `FileNotFoundError`，打包元数据无法生成。这体现了该文件作为唯一事实来源的地位。

## 5. 综合实践

**任务：写一份 1 页的《DeepSpeed 活跃方向调研笔记》——用 git 历史与 README 新闻互相印证。**

一份开源项目的「自我介绍」（README News）和它的「实际行为」（git log）对照着读，是判断项目健康度和方向的最快方法。本实践把前面三个模块串起来。

**实践目标**：从真实提交记录中识别出 3 个正在积极演进的子系统，并与 README 的 Latest News 互相印证。

**操作步骤**：

1. 进入仓库根目录，执行：

   ```bash
   git log --oneline -20
   ```

2. 把输出按主题分组。分组线索：提交信息开头的方括号标签（`[Hybrid Engine]`、`[AutoEP]`、`[AffineIR]`、`[VocabTP]`）和前缀（`ZeRO-1/2:`、`ZeRO-3:`、`ci:`）。
3. 打开 [README.md:L27-L67](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/README.md#L27-L67) 的 Latest News（含 More news），为每个分组找一条时间上最近的新闻佐证。
4. 用 `git log --oneline -20 -- deepspeed/runtime/zero/` 这样的路径过滤命令，分别统计各主题目录的提交热度。
5. 写成 1 页笔记，每个方向包含：方向名、证据提交（哈希 + 一句话）、README 佐证新闻（日期 + 标题）、一句话判断它解决的是显存墙 / 通信墙 / 吞吐中的哪一个。

**参考范例**（以下为撰写本讲时对 HEAD `dc046b15d` 的真实观察，可作为你的笔记样例）：

- **ZeRO 卸载与显存优化**：`dc046b15d`「ZeRO-1/2: preserve gradients across request boundaries until step」、`c6291b7e1`「ZeRO-3: copy offloaded master weights back to the device through pinned buffer」——佐证新闻：SuperOffload 获 ASPLOS 2026 提名（2026/03）、ZenFlow（2025/08）。主攻**显存墙**。
- **专家并行 AutoEP / AffineIR**：`66fef47b2`、`f81850499`、`12ffb9903`、`7501610ee` 四条 AutoEP/AffineIR 相关提交——佐证新闻：Ray x DeepSpeed Meetup 中提到的专家并行与 fused kernel 工作（2025/10）。主攻**吞吐与显存**。
- **混合引擎与张量并行**：`ab7fd98fa`「[Hybrid Engine] Enable graph capture ... on non-CUDA accelerators」、`1a956270f`「[VocabTP] vocab_parallel_ce support fused LM-head projection + loss」——佐证新闻：AutoTP（2025/03，More news）。主攻**推理/训练一体化与通信墙**。

**需要观察的现象**：git log 的主题分布与 README 新闻的优先级高度吻合——新闻是「宣言」，提交是「行动」；两者不一致的地方（例如某特性上了新闻但近期无提交）通常意味着该方向已进入稳定维护期。

**预期结果**：一份含 3 个方向、每个方向有提交证据 + 新闻佐证 + 一句话定位的 1 页笔记。完成后建议存档在 `DeepSpeed-tutorial/` 之外的自有笔记目录里，学习单元 5 和单元 6 时回头验证你的判断。

## 6. 本讲小结

- DeepSpeed 是面向超大模型训练与推理的**底层优化库**，三大痛点是**显存墙、通信墙、训练吞吐**；核心创新包括 ZeRO、ZeRO-Infinity、3D 并行、Ulysses 序列并行与 DeepSpeed-MoE（[README.md:L71-L73](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/README.md#L71-L73)）。
- README 的板块结构本身就是一张能力矩阵：News 看方向、Adoption 看生态（Transformers/Accelerate/Lightning 都在集成它）、CI 与 HW 表看硬件版图（NVIDIA/AMD/CPU/Gaudi/XPU/昇腾等）。
- `docs/_pages/training.md` 是官方能力矩阵的展开版：按「难 → 快 → 省 → 大 → 通 → 效 → 易」的问题链组织；其中 ZeRO 的定义（切分而非复制）与核心 API 清单（`initialize`/`backward`/`step`/checkpoint）是单元 2 的直接预习材料。
- 版本传递链：`version.txt`（0.19.9）→ `setup.py` 打包 → `deepspeed.__version__` / `__git_hash__`；运行时日志会打印 version + git-hash + git-branch，用于精确定位代码版本。
- 阅读 »README News + git log« 双轨对照是判断开源项目活跃方向的标准手法；当前最活跃的三条线是 ZeRO 卸载（ZenFlow/SuperOffload 系）、专家并行（AutoEP/AffineIR）、混合引擎/张量并行。

## 7. 下一步学习建议

下一讲（u1-l2「代码地图：仓库目录结构与模块导览」）将钻进 `deepspeed/` 主包，逐一认识 runtime、ops、module_inject、csrc 等目录的职责分工，并精读 `deepspeed/__init__.py` 的导出列表。在进入下一讲之前，建议你：

1. 把本讲的「文档小节 ↔ 源码目录」对照表再过一遍，下一讲会把每个目录展开讲。
2. 通读一遍 [README.md:L123-L169](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/README.md#L123-L169) 的安装章节，留意「JIT 编译」与「预编译」两种算子构建方式的区别——这是 u1-l3 的主题。
3. 如果想提前建立 ZeRO 直觉，可泛读 [training.md:L233-L283](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/docs/_pages/training.md#L233-L283) 关于 ZeRO 各子优化的说明，不求全懂。

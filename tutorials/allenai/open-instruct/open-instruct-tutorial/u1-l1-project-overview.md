# u1-l1 open-instruct 是什么：开放后训练与 Tülu/OLMo 全景

## 1. 本讲目标

读完本讲，你应该能够：

1. 用一两句话说出 open-instruct 的定位：它是 Allen AI 维护的**开放后训练（post-training）研究代码库**，覆盖 SFT、DPO、RLVR 全链路。
2. 讲清它和 Tülu 1/2/2.5/3、OLMo 2、Olmo 3 这些模型系列的关系：**模型是这套代码训练出来的产物**，代码库本身就是「配方」的公开实现。
3. 在仓库里准确指出 SFT、DPO、RLVR 三个后训练阶段各自的训练入口文件（`finetune.py` / `olmo_core_finetune.py`、`dpo.py`、`grpo.py` / `grpo_fast.py` / `python -m open_instruct.miles`）。
4. 理解这是一个**研究代码库**：不保证向后兼容、多套并行技术路线共存是它的常态，而不是「历史包袱」。

本讲是整本手册的第一讲，不要求你读懂任何训练代码，只要求建立正确的「地图感」——知道这个仓库是什么、产出过什么、入口在哪。

## 2. 前置知识

本讲几乎不需要编程基础，但下面几个概念会反复出现，先用通俗语言解释一遍：

- **大语言模型（LLM）**：如 Llama、OLMo，是通过预测下一个词训练出来的神经网络。只做过预训练的模型叫**基座模型（Base Model）**，它会「接话」但不一定会「听话」。
- **后训练（post-training）**：把基座模型变成好用助手的全过程。open-instruct 的全部工作都围绕它展开，典型流水线是三步：
  - **SFT（Supervised Fine-Tuning，监督微调）**：拿「问题 + 标准回答」的数据集直接教模型模仿，相当于「抄范例学写作」。
  - **DPO（Direct Preference Optimization，直接偏好优化）**：拿「同一个问题的两个回答，人工标注哪个更好」的数据，教模型偏向好的那个，相当于「对比范文改作文」。
  - **RLVR（Reinforcement Learning with Verifiable Rewards，可验证奖励强化学习）**：对数学、代码这类**答案可以对错自动判定**的任务，让模型自己生成答案、程序自动打分（对=1，错=0），再用强化学习（本仓库主要用 GRPO 算法）朝高分方向优化。
- **奖励模型（RM，Reward Model）**：一个专门学会给回答打分的模型，用于制造偏好数据。它是 DPO 的「上游供应商」，本仓库也能训练它。
- **HuggingFace**：模型和数据集的「GitHub」。本仓库训练的模型都发布在 HuggingFace Hub 上，形如 `allenai/Llama-3.1-Tulu-3-8B`。
- **OLMo-core**：Allen AI 自研的高效训练库，是 `transformers` 之外的另一条训练路线，部分入口文件基于它构建（后面单元会专门讲）。

> 命名小提示（本手册遵循仓库约定）：OLMo 1/2 代写法为「OLMo」；从 Olmo 3 起改用普通专有名词写法「Olmo」。

## 3. 本讲源码地图

本讲涉及的关键文件（本讲只精读前两个，其余只定位入口）：

| 文件 | 作用 |
|---|---|
| [README.md](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/README.md) | 项目门面：定位声明、安装、三类训练的启动示例、仓库结构图 |
| [docs/index.md](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/docs/index.md) | 文档站首页：项目介绍、模型发布表格（Base/SFT/DPO/Final/RM 各阶段产物）、新闻时间线 |
| `open_instruct/finetune.py` | SFT 入口之一：基于 transformers + accelerate/DeepSpeed 的经典路线 |
| `open_instruct/olmo_core_finetune.py` | SFT 入口之二：基于 OLMo-core 的高效路线（对支持的模型推荐） |
| `open_instruct/dpo.py` | DPO 入口（OLMo-core 路线）；旧路线在 `dpo_tune_cache.py` |
| `open_instruct/grpo.py` | RLVR 入口之一：Core/vLLM 的 GRPO 工作流 |
| `open_instruct/grpo_fast.py` | RLVR 入口之二：Ray + DeepSpeed/vLLM 的 GRPO 工作流 |
| `open_instruct/miles/__main__.py` | RLVR 入口之三：MILES 系统的 CLI（`python -m open_instruct.miles`） |

一个先记住的事实：`open_instruct/` 是全部核心源码所在目录，仓库里**没有** `src/` 或 `lib/`，训练脚本就是这个目录下的顶层 `.py` 文件。

## 4. 核心概念与源码讲解

### 4.1 模块一：README 项目介绍——open-instruct 的定位

#### 4.1.1 概念说明

打开任何开源项目，第一个该读的都是 README 的第一段。open-instruct 的自我介绍是：**「对流行的预训练语言模型、基于公开数据集做指令微调和后训练的开放努力（open effort）」**。

拆开看，这句话包含三个承诺，正好对应 README 开头列出的三条发布内容：

1. 用统一格式微调语言模型的代码（对应 SFT）；
2. DPO、偏好微调和 RLVR 的代码（对应偏好优化与强化学习）；
3. 探索过程中产出的 checkpoint 等工件（对应 HuggingFace 上的模型）。

「开放」是关键词：数据配方、训练超参、代码、模型权重全部公开，目标是让任何人都能复现甚至改进 Allen AI 的后训练结果。Tülu 系列模型就是这套开放配方的产物。

#### 4.1.2 核心流程

open-instruct 世界的中心是一条**后训练流水线**，理解了它就理解了整个仓库的代码组织：

```
基座模型（Base，只会接话）
   │  输入公开的指令/对话数据
   ▼
① SFT 监督微调        ——「照范例学」
   ▼
② DPO 偏好优化        ——「对比着学」
   ▼
③ RLVR 可验证奖励 RL  ——「做题对答案学」
   ▼
最终模型（Final，会听话、会推理）
```

仓库里的训练入口文件基本可以一一挂到这条流水线上；此外还有一个「支线」入口——奖励模型（RM）训练，它为阶段②提供打分能力。后续所有单元讲的具体机制（数据混合、损失函数、权重同步……）都是为了让这条流水线跑得更快、更好。

#### 4.1.3 源码精读

**定位声明**。README 用三行列表说清了仓库会持续发布什么：

> [README.md:L5-L11](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/README.md#L5-L11)
>
> 这段是仓库的「宪法」：标题表明主题是训练开放指令遵循语言模型；编号列表的三条分别承诺 SFT 代码、DPO/偏好/RLVR 代码、以及训练产物（checkpoint 与工件）。本手册后面所有单元都在这三条的范围内。

**研究代码库声明**。README 在 Setup 一节里有一句非常关键的提醒：

> [README.md:L23](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/README.md#L23)
>
> 原文明确写道：*「Open Instruct is a research codebase and does not guarantee backward compatibility」*——这是研究代码库，**不保证向后兼容**。这意味着：接口可能随论文实验随时变化；新旧多套实现并存是常态（例如 DPO 就同时存在 `dpo.py` 和 `dpo_tune_cache.py` 两条路线）；读代码时要先确认「哪条是当前主推路线」，方法就是看 README 的 Training 一节（本讲 4.3.3 会精读）。

**代码的血统**。README 的致谢部分透露了这个仓库的技术来源，对理解代码风格很有帮助：

> [README.md:L145-L152](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/README.md#L145-L152)
>
> 这里说明了：微调脚本改编自 HuggingFace Transformers 的 Trainer；偏好微调改编自 TRL 和 Eric Mitchell 的 DPO 实现；RL 部分源自 OpenAI 经典 RLHF 代码；而 70B 规模的 Ray + vLLM 分布式代码改编自 OpenRLHF。所以你在本仓库会看到这四个项目的影子，并不奇怪。

#### 4.1.4 代码实践

**实践：给 open-instruct 写一句「电梯介绍」并找到三条承诺的证据**

1. **实践目标**：不看任何二手资料，只凭 README 第一屏，能向别人准确介绍这个仓库。
2. **操作步骤**：
   - 打开 [README.md](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/README.md)，只读第 1 行到第 45 行（定位、Setup、Training 开头）。
   - 用一句话写下 open-instruct 是什么，必须包含「后训练」「开放」「SFT/DPO/RLVR」中的至少两个词。
   - 再打开 [docs/index.md](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/docs/index.md) 第 1–7 行，对比两处介绍是否一致。
3. **需要观察的现象**：两个文件的介绍段落几乎逐字相同——这告诉你 `docs/index.md` 是文档站（MkDocs）首页，与 README 共享同一份「自我介绍」，以后检索信息时两边都可以查。
4. **预期结果**：你写出的介绍与下面这句大意相近即算通过——「open-instruct 是 Allen AI 的开放后训练代码库，用公开数据集对流行的预训练模型做 SFT、DPO 偏好微调和 RLVR 强化学习，并发布由此产出的 Tülu、OLMo 系列模型。」

#### 4.1.5 小练习与答案

**练习 1**：README 说仓库「不保证向后兼容」，这对使用者意味着什么？举一个仓库中的实际例子。

**参考答案**：意味着依赖本仓库的代码或脚本可能在某次更新后失效，升级需重读文档和 diff。实际例子：DPO 训练同时存在 `open_instruct/dpo.py`（OLMo-core 路线，当前 README 推荐）和 `open_instruct/dpo_tune_cache.py`（旧版 DeepSpeed 路线），入口会随研究进展切换；又如 RLVR 从 `grpo.py`/`grpo_fast.py` 发展出了新的 MILES 路线，但旧入口仍保留给稠密模型。

**练习 2**：README 承诺发布的三类东西分别对应仓库/HuggingFace 上的什么？

**参考答案**：① SFT 微调代码 → `open_instruct/finetune.py`、`open_instruct/olmo_core_finetune.py` 等训练脚本；② DPO/偏好/RLVR 代码 → `open_instruct/dpo.py`、`open_instruct/grpo*.py`、`open_instruct/miles/` 包；③ checkpoint 等工件 → HuggingFace 上的 `allenai/Llama-3.1-Tulu-3-8B-SFT`、`allenai/OLMo-2-1124-7B-Instruct` 等模型（见 4.2 的表格）。

### 4.2 模块二：模型发布表格——Tülu/OLMo 系列与后训练阶段

#### 4.2.1 概念说明

「这个仓库能训练出什么？」最直观的答案在 `docs/index.md` 的模型发布表格里。表格按**基座模型**分列（Llama 3.1 8B / 70B、OLMo-2 7B / 13B），按**后训练阶段**分行（Base / SFT / DPO / Final / RM），每一格是一个可下载的 HuggingFace 模型。

这张表能帮你建立两个关键认知：

1. **阶段即产物**：后训练不是黑箱，每个中间阶段都会发布一个模型。例如 `allenai/Llama-3.1-Tulu-3-8B-SFT` 是 SFT 完成的快照，`...-DPO` 是偏好优化后的快照，不带后缀的 `allenai/Llama-3.1-Tulu-3-8B` 才是走完 RLVR 的最终模型。
2. **系列演进**：Tülu 1（2023，纯 SFT 探索）→ Tülu 2（2023，引入 DPO）→ Tülu 2.5（2024，DPO/PPO 对比）→ Tülu 3（2024，完整 SFT+DPO+RLVR 配方）→ Tülu 3.1（2025，GRPO 配方重训）→ Olmo 3（2025，新一代模型）。**仓库代码的每次大扩充都对应一代模型的发布**，读 News 时间线就是在读这个仓库的进化史。

#### 4.2.2 核心流程

从表格读出的「一条模型生产线」（以 Llama 3.1 8B 列为例）：

```
meta-llama/Llama-3.1-8B                （Base，第三方基座）
   ▼ SFT          产物：allenai/Llama-3.1-Tulu-3-8B-SFT
   ▼ DPO          产物：allenai/Llama-3.1-Tulu-3-8B-DPO
   ▼ RLVR (GRPO)  产物：allenai/Llama-3.1-Tulu-3-8B（最终）
                     2025 年又用 GRPO 配方重训出
                     allenai/Llama-3.1-Tulu-3.1-8B
```

支线上还有 `allenai/Llama-3.1-Tulu-3-8B-RM`（奖励模型），它不在这条主线上，而是为偏好数据生产服务的。注意各列进度不一致属于正常现象——研究以实际需要为准，不是每个基座都必须走完全部阶段。

#### 4.2.3 源码精读

**模型发布表格**。docs 首页的这张表是全仓库对「后训练流水线」最直观的可视化：

> [docs/index.md:L34-L41](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/docs/index.md#L34-L41)
>
> 表格行名依次是 **Base Model → SFT → DPO → Final Models (RLVR) → Reward Model (RM)**，共四列基座模型。其中第 40 行单独标注了 2025 年新发布的 `allenai/Llama-3.1-Tulu-3.1-8B`，注明 *「trained with GRPO」*——它是用更新后的 GRPO 配方训练的，在几乎所有评测上超过了旧的 Tulu-3-8B。把这张表的每一行想象成流水线上的一个工位，训练代码就是让工件在工位间流转的传送带。

**新闻时间线**。表格下方按时间倒序记录了每代模型的发布：

> [docs/index.md:L43-L47](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/docs/index.md#L43-L47)
>
> 三条关键新闻：2025-11-20 发布 **Olmo 3**；2025-02-12 发布用 GRPO 训练的 Tulu 3.1 8B；2024-11-22 发布 TÜLU 3 论文并更新了 Llama 3.1 与 OLMo 2 的完整后训练配方。对照 README 引用区可以找到各代对应的论文。

**配套论文**。README 的 Citation 区按代收录了 bibtex：

> [README.md:L154-L217](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/README.md#L154-L217)
>
> 从上到下依次是 Tulu 1（《How Far Can Camels Go?》）、Tulu 2、Tulu 2.5（《Unpacking DPO and PPO》）、Tülu 3（《Pushing Frontiers in Open Language Model Post-Training》）和 Olmo 3 的引用条目。想深入某一代配方时，论文 + `docs/` 下对应文档（如 `docs/tulu3.md`）是最权威的组合。

#### 4.2.4 代码实践

**实践：从表格反推「一个模型的身世」**

1. **实践目标**：学会用模型命名规则判断「它属于哪个基座、走到了哪个阶段」。
2. **操作步骤**：
   - 阅读 [docs/index.md:L34-L41](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/docs/index.md#L34-L41) 的表格。
   - 对下面 4 个模型 ID，分别写出：基座是谁、对应表格哪一行、它训练自哪个模型——
     `allenai/OLMo-2-1124-7B-SFT`、`allenai/Llama-3.1-Tulu-3-8B-DPO`、`allenai/Llama-3.1-Tulu-3-8B`、`allenai/OLMo-2-1124-7B-RM`。
   - 在 HuggingFace 上打开其中一个（如 [allenai/Llama-3.1-Tulu-3-8B](https://huggingface.co/allenai/Llama-3.1-Tulu-3-8B)），看模型卡片如何描述训练过程。
3. **需要观察的现象**：命名中带 `-SFT`/`-DPO`/`-RM` 后缀的是**中间产物**，带 `Instruct` 或无后缀的 Tulu 主名（如 `Tulu-3-8B`、`OLMo-2-1124-7B-Instruct`）是**走完全部阶段的最终模型**。
4. **预期结果**：`OLMo-2-1124-7B-SFT` 的基座是 `allenai/OLMo2-7B-1124`（表格 Base 行），训练自 SFT 阶段产物；`Llama-3.1-Tulu-3-8B-DPO` 由 `...-8B-SFT` 训练而来；`Llama-3.1-Tulu-3-8B` 由 `...-8B-DPO` 经 RLVR 得到，是最终模型；`OLMo-2-1124-7B-RM` 是奖励模型支线产物，不在主流水线上。HuggingFace 页面访问需要网络，若无法访问则以仓库内表格为准。

#### 4.2.5 小练习与答案

**练习 1**：Tülu 3 和 Tülu 3.1 的 8B 最终模型有什么区别？

**参考答案**：`allenai/Llama-3.1-Tulu-3-8B` 是 Tülu 3 配方的最终模型（SFT→DPO→RLVR）；`allenai/Llama-3.1-Tulu-3.1-8B` 是 2025-02 用更新的 **GRPO 配方**重新训练的版本，按 docs/index.md 第 46 行的说法，它在几乎所有评测上超过旧版。

**练习 2**：为什么表格里 Reward Model 一行 70B 列写的是「(Same as 8B)」？

**参考答案**：奖励模型只为生产偏好数据服务，不必为每个规模各训一个；70B 的偏好数据可以直接用 8B 训练的 RM 来打分/排序，所以 70B 复用 8B 的 RM（`allenai/Llama-3.1-Tulu-3-8B-RM`），OLMo-2 13B 同理复用 7B 的。

### 4.3 模块三：仓库结构图与训练入口文件

#### 4.3.1 概念说明

最后一个模块解决「入口在哪」：给定一个后训练阶段，哪个文件是它的启动脚本？这需要把 README 的两段内容拼起来——**Repo structure**（目录职责）和 **Training**（每个阶段的启动命令）。

先记目录口诀：

- `open_instruct/` —— 源码主体，所有训练脚本和工具库都在这（**本手册的主战场**）；
- `scripts/` —— 启动脚本，`train/`（训练，含 `debug/` 冒烟测试）、`eval/`（评测）、`data/`(数据制备)；
- `configs/` —— 三套配置：`ds_configs/`（DeepSpeed）、`train_configs/`（训练超参）、`beaker_configs/`（AI2 Beaker 集群）；
- `docs/` —— MkDocs 文档站，本手册大量引用它；
- `decontamination/` —— 训练/评测数据去污染工具；
- `mason.py` —— 向 AI2 Beaker 集群提交任务的启动器。

「入口文件」指**可以被直接运行、负责组装整个训练的顶层脚本**，它们都定义了 `def main(...)`，这是你在仓库里导航的锚点。

#### 4.3.2 核心流程

阶段 → 入口的完整映射（★ 为当前 README 主推）：

```
阶段            入口文件                                 技术路线
────────────────────────────────────────────────────────────────
SFT        ★   open_instruct/olmo_core_finetune.py      OLMo-core（支持的模型推荐）
               open_instruct/finetune.py                transformers + accelerate/DeepSpeed
DPO        ★   open_instruct/dpo.py                     OLMo-core
               open_instruct/dpo_tune_cache.py          DeepSpeed（旧路线，支持 LoRA）
RLVR       ★   python -m open_instruct.miles            MILES（支持的 Olmo MoE 模型）
               open_instruct/grpo.py                    Core/vLLM（Ray + OLMo-core）
               open_instruct/grpo_fast.py               Ray + DeepSpeed/vLLM
（支线）RM      open_instruct/reward_modeling.py         transformers Trainer
```

实际启动不是直接 `python` 这些文件，而是经由 `scripts/train/` 下的脚本 + `./scripts/train/build_image_and_launch.sh`（构建 Docker 镜像并提交到 Beaker 集群）完成——这属于第 4 讲（u1-l4）的内容，本讲只需记住映射表本身。

#### 4.3.3 源码精读

**仓库结构图**。README 用一棵 ASCII 树画出了目录职责：

> [README.md:L115-L133](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/README.md#L115-L133)
>
> 从上到下：`assets/`（图片与许可证）、`configs/`（beaker/ds/train 三套配置）、`decontamination/`（污染检查）、`docs/`（MkDocs 文档）、`human_eval/`（人工评测界面，已不维护）、`open_instruct/`（**源码主体**）、`scripts/`（train/eval/data 三类启动脚本，`train/debug/` 是冒烟测试）、`tests/`（多数测试与代码同目录）、`mason.py`（Beaker 启动器）、`Dockerfile`。

**Training 一节：三个阶段的官方入口**。README 的训练示例按三小节组织，正好对应流水线三阶段：

> [README.md:L47-L57](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/README.md#L47-L57)
>
> **Finetuning（SFT）**：对支持的模型（OLMo、OLMoE、Qwen3）推荐 GPU 效率更高的 OLMo-core 实现，入口是 `open_instruct/olmo_core_finetune.py`，并给出单卡冒烟命令 `./scripts/train/build_image_and_launch.sh scripts/train/debug/oc_sft.sh`。

> [README.md:L60-L70](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/README.md#L60-L70)
>
> **Preference Tuning（DPO）**：明确写着 *「We train DPO with `open_instruct/dpo.py`」*，配 `scripts/train/debug/dpo/single_gpu.sh` 冒烟脚本。

> [README.md:L73-L87](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/README.md#L73-L87)
>
> **RLVR**：对支持的 Olmo MoE 模型用 MILES + OLMo-core 集成（见 `docs/miles/grpo.md`）；现有 Core/vLLM 与 DeepSpeed/vLLM GRPO 工作流**仍可用于稠密模型**，示例命令用 `open_instruct/grpo_fast.py`。注意「MILES 不废弃旧 GRPO 训练器」——新旧并存是刻意设计，按模型选路线即可。

**用 `def main` 验证入口**。五个入口在源码中的锚点（可直接点击跳转）：

> [open_instruct/finetune.py:L377](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/open_instruct/finetune.py#L377)
>
> `def main(args: FlatArguments, tc: TokenizerConfig):` —— 经典 SFT 路线的主函数，文件开头 import 了 `deepspeed`、`accelerate`、`transformers`，说明它走 HuggingFace 生态。

> [open_instruct/olmo_core_finetune.py:L538](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/open_instruct/olmo_core_finetune.py#L538)
>
> `def main(args: SFTArguments, tc: dataset_transformation.TokenizerConfig) -> None:` —— OLMo-core SFT 路线主函数（当前推荐）。

> [open_instruct/dpo.py:L141-L144](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/open_instruct/dpo.py#L141-L144)
>
> docstring 直言 *「Main entry point for DPO training with OLMo-core」*；第 144 行还提示 LoRA 用户改用 `dpo_tune_cache.py`——新旧路线的分工在代码里就有说明。

> [open_instruct/grpo.py:L103](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/open_instruct/grpo.py#L103) 与 [open_instruct/grpo_fast.py:L2190](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/open_instruct/grpo_fast.py#L2190)
>
> 两个 GRPO 主函数：前者编排 OLMo-core 模型组与 vLLM（Core/vLLM），后者是 Ray + DeepSpeed/vLLM 的大规模实现（`grpo_fast.py` 超过十万字符，是仓库里最大的训练脚本）。

> [open_instruct/miles/__main__.py:L45-L47](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/open_instruct/miles/__main__.py#L45-L47)
>
> MILES 的 CLI 入口：`python -m open_instruct.miles {plan,validate,train,run,status} CONFIG.toml`——注意它与前几个入口形态不同，是一个带五个子命令的命令行工具，接收 TOML 配置文件。文件开头的 docstring（L1-L12）值得通读，它解释了为何把运行时 import 延迟到需要的子命令，使 `plan` 无需 GPU 环境即可运行。

#### 4.3.4 代码实践

**实践：亲手验证「阶段 → 入口」映射表**

1. **实践目标**：不借助记忆，用一条命令在仓库里重新找到所有训练入口，建立「随时能定位入口」的能力。
2. **操作步骤**：
   - 在仓库根目录执行：
     ```bash
     ls open_instruct/*.py | head -20          # 感受源码目录的规模
     grep -n "^def main" open_instruct/finetune.py \
        open_instruct/olmo_core_finetune.py \
        open_instruct/dpo.py \
        open_instruct/grpo.py \
        open_instruct/grpo_fast.py
     grep -n "choices=" open_instruct/miles/__main__.py
     ```
   - 把每条输出行号与本讲 4.3.3 的五个链接对照，确认行号一致。
   - 再执行 `grep -rn "We train" README.md`，找到 README 对 DPO/RLVR 入口的原文表述。
3. **需要观察的现象**：`grep` 输出的行号应与 4.3.3 中给出的完全相同（同一 commit 下）；README 中 DPO 一节出现 *「We train DPO with `open_instruct/dpo.py`」*、RLVR 一节出现 *「We train with `open_instruct/grpo_fast.py`」*。
4. **预期结果**：你得到一张自己验证过的「阶段 → 入口 → main 函数行号」对照表。这个 `grep "^def main"` 技巧以后面对任何陌生 Python 项目都能用。

#### 4.3.5 小练习与答案

**练习 1**：朋友告诉你「open-instruct 只能训 Llama」。请用 docs/index.md 的表格反驳。

**参考答案**：表格（[docs/index.md:L34-L41](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/docs/index.md#L34-L41)）同时列有 Llama 3.1 8B/70B 与 **OLMo-2 7B/13B** 两家基座的完整阶段产物；此外 README L49 提到 OLMo-core 路线还支持 OLMoE、Qwen3 等，RLVR 的 MILES 路线面向 Olmo MoE 模型。

**练习 2**：`scripts/train/debug/` 下的脚本（如 `oc_sft.sh`、`single_gpu_on_beaker.sh`）和 `open_instruct/` 下的 `.py` 入口是什么关系？

**参考答案**：`.py` 文件是真正的训练程序；`scripts/train/debug/` 下的 shell 脚本是**冒烟测试启动器**，用小模型、小步数把对应入口快速跑一遍以验证环境，通常经由 `./scripts/train/build_image_and_launch.sh` 构建镜像后提交到 Beaker 集群运行。脚本内部最终调用的就是这些 `.py` 入口。

**练习 3**：想给一个稠密模型（非 MoE）做 RLVR，应该选哪个入口？依据是什么？

**参考答案**：选 `open_instruct/grpo.py`（Core/vLLM）或 `open_instruct/grpo_fast.py`（DeepSpeed/vLLM）。依据是 [README.md:L75-L77](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/README.md#L75-L77)：MILES 面向「支持的 Olmo MoE 模型」，而现有两个 GRPO 工作流「remain available for dense-model training」。

## 5. 综合实践

**任务：绘制一张「open-instruct 后训练全景图」，把本讲三个模块串起来。**

1. **实践目标**：产出一张自己画的流程图 + 一张入口对照表，作为整本手册的「总导航页」。
2. **操作步骤**：
   - 准备一张纸或任意画图工具，画五个竖排节点：`Base` → `SFT` → `DPO` → `RLVR` → `Final`。
   - **给节点标注产物**（依据 [docs/index.md:L34-L41](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/docs/index.md#L34-L41)）：以 Llama 3.1 8B 列为例，在 Base 节点写 `meta-llama/Llama-3.1-8B`，SFT 节点写 `allenai/Llama-3.1-Tulu-3-8B-SFT`，依次类推，Final 节点写 `allenai/Llama-3.1-Tulu-3-8B`。
   - **给箭头标注入口**（依据 [README.md:L47-L87](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/README.md#L47-L87)）：
     - Base→SFT：`open_instruct/finetune.py`（HF 路线）/ `open_instruct/olmo_core_finetune.py`（推荐）
     - SFT→DPO：`open_instruct/dpo.py`（旧路线 `dpo_tune_cache.py`）
     - DPO→RLVR：`open_instruct/grpo.py` / `open_instruct/grpo_fast.py` / `python -m open_instruct.miles`
   - **画支线**：从 SFT 产物旁引出一条线到 `RM` 节点，标注入口 `open_instruct/reward_modeling.py`，并注明「RM 生产的偏好数据供 DPO 使用」。
   - 最后在图角落写一行小字：*「研究代码库，不保证向后兼容（README L23）」*，提醒未来的自己。
3. **需要观察的现象**：画完后你会发现仓库目录结构（`open_instruct/` 放程序、`scripts/train/` 放启动器、`configs/` 放配置）能和图上的每个箭头对上——每个阶段箭头都同时需要这三样东西。
4. **预期结果**：一张类似下面骨架的图（箭头上为入口）：

```
              allenai/Llama-3.1-Tulu-3-8B-SFT      allenai/Llama-3.1-Tulu-3-8B-DPO
meta-llama/          │  finetune.py /                 │  dpo.py          RM（支线）
Llama-3.1-8B ────────┘  olmo_core_finetune.py ────────┘  ── grpo.py /     ▲
（Base）                                              grpo_fast.py       │ reward_modeling.py
                                                   / python -m          │（偏好数据供 DPO）
                                                 open_instruct.miles    │
                                                       ▼                │
                                        allenai/Llama-3.1-Tulu-3-8B（Final）
```

这张图建议保留，后续单元每学完一个模块（数据加载、损失函数、权重同步……），就回来把它标注到对应箭头上。

## 6. 本讲小结

- open-instruct 是 Allen AI 的**开放后训练研究代码库**：用公开数据集对流行基座模型做 SFT、DPO/偏好微调和 RLVR，并发布全部代码、配方与模型（README L5-L11）。
- 它产出了 **Tülu 1→2→2.5→3→3.1** 和 **OLMo 2 / Olmo 3** 系列模型；`docs/index.md` 的发布表格把后训练拆成 Base → SFT → DPO → Final(RLVR) + RM 支线，每个阶段都有可下载的中间产物。
- 三大阶段的训练入口：SFT 用 `finetune.py` / `olmo_core_finetune.py`；DPO 用 `dpo.py`（旧路线 `dpo_tune_cache.py`）；RLVR 用 `grpo.py` / `grpo_fast.py` / `python -m open_instruct.miles`，按模型类型选路线。
- 仓库结构口诀：`open_instruct/` 源码、`scripts/`（train/eval/data）启动器、`configs/` 三套配置、`docs/` 文档、`mason.py` 集群提交器。
- 这是**研究代码库，不保证向后兼容**（README L23）：多套路线并存、入口随研究演进切换是常态，判断「当前主推」要看 README 的 Training 一节。
- 定位入口的通用技巧：`grep -n "^def main" open_instruct/*.py`。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：动手搭建本地开发环境——`uv sync` 安装、Python/CUDA 版本约束、`make style` / `pytest` 工具链。从下一讲起，你将开始真正运行这个仓库的代码。
- **按需跳读**：如果你急于看训练代码长什么样，可以先跳到单元 2 的 `finetune.py` 精读（u2-l1），但建议先完成 u1 全部四讲，跑通一次冒烟训练再深入。
- **推荐延伸阅读**（本仓库内）：
  - [docs/tulu3.md](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/docs/tulu3.md) —— Tülu 3 完整复现指南，是第 35 讲（u9-l5）的主角；
  - [docs/algorithms/grpo.md](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/docs/algorithms/grpo.md) —— GRPO 工作流总览；
  - [docs/miles/index.md](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/docs/miles/index.md) —— MILES 系统文档入口。
- **论文**：Tülu 3 论文（arXiv:2411.15124）与本手册单元 4-6 的内容一一对应，读论文时手边开着本仓库源码效果最佳。

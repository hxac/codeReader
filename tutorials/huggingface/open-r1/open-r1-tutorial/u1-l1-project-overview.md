# Open-R1 是什么：R1 复现计划全景

## 1. 本讲目标

读完本讲后，你应该能够：

- 说清楚 **open-r1** 这个项目到底想做什么、为什么要做。
- 用自己的话描述 DeepSeek-R1 流水线的 **三步走计划**（蒸馏、纯 RL、多阶段训练），以及它们之间的依赖关系。
- 在仓库里 **准确定位** 三大核心入口脚本 `sft.py`、`grpo.py`、`generate.py`，并理解 `Makefile` 如何把它们串成一条「命令即流水线」的开发体验。
- 知道后续每一篇讲义会深入到哪个脚本，建立全局地图。

本讲是整个学习手册的「第 0 步」，不写一行训练代码，只帮你建立全局观。

---

## 2. 前置知识

本讲面向零基础读者，但下面几个名词会反复出现，先建立直觉：

- **大语言模型（LLM）**：一种能「续写文本」的概率模型。给它一段开头，它能生成后续内容。
- **推理（reasoning）**：模型在给出最终答案前，先输出一段「思考过程」。例如解数学题时先写推导，再给答案。DeepSeek-R1 正是以「会一步步推理」闻名。
- **监督微调（SFT, Supervised Fine-Tuning）**：拿一批「问题 + 高质量答案」做样本，让模型模仿这些答案的写法。相当于「老师改作文」。
- **强化学习（RL, Reinforcement Learning）**：不给标准答案，而是给模型生成的答案打分（奖励 reward），让它自己往高分方向调整。相当于「做题对答案、自己总结套路」。
- **蒸馏（distillation）**：用一个强模型（老师）生成大量高质量「思考过程」，再用这些数据 SFT 训练一个小模型（学生），让学生学会老师的推理风格。
- **GRPO（Group Relative Policy Optimization）**：DeepSeek 提出的一种强化学习算法，是本项目的核心训练方法之一。本讲只需记住「它是一种打分驱动的 RL」，细节会在第三单元细讲。

如果你对其中几个词还很陌生，没关系——本讲只要求你理解「大方向」，名词的精确定义会在后续讲义里逐一展开。

---

## 3. 本讲源码地图

本讲涉及的关键文件很少，都是项目「门面」级别的文件：

| 文件 | 作用 |
|------|------|
| `README.md` | 项目说明书。包含项目定位、三步走计划、安装、训练、评估、数据生成等全部入口说明。**本讲主要依据它**。 |
| `assets/plan-of-attack.png` | 一张示意图，直观展示 DeepSeek-R1 的多阶段训练流水线，对应 README 里的「Plan of attack」。 |
| `src/open_r1/sft.py` | 监督微调（SFT）入口脚本，负责「蒸馏」阶段的核心训练。 |
| `src/open_r1/grpo.py` | GRPO 强化学习入口脚本，负责「纯 RL」阶段的核心训练。 |
| `src/open_r1/generate.py` | 数据生成入口，用 Distilabel 流水线批量产出推理数据。 |
| `Makefile` | 把上面的脚本封装成 `make install`、`make evaluate` 等一条命令可调用的快捷方式。 |

> 提示：本讲只读这些文件的「头部说明」和 README，不深入实现细节。源码精读从第二单元开始。

---

## 4. 核心概念与源码讲解

### 4.1 项目背景：为什么要复现 DeepSeek-R1

#### 4.1.1 概念说明

2025 年初，DeepSeek 发布了 **DeepSeek-R1**，一个在数学、代码、科学推理上表现极强、并且会「长链思考」的开源模型。但 DeepSeek 只公开了**模型权重**和一份**技术报告（tech report）**，并没有公开训练用的完整代码、数据和配置。

这带来一个痛点：全世界的研究者都知道「R1 很强」，却**无法亲手复现、也无法在它的基础上改进**。

**open-r1** 要解决的就是这个问题。它的一句话定位写在 README 第一行：

> *A fully open reproduction of DeepSeek-R1. This repo is a work in progress, let's build it together!*
> （对 DeepSeek-R1 的完全开源复现。本仓库仍在建设中，一起来搭吧！）

关键词是 **reproduction（复现）** 和 **fully open（完全开源）**——不只是放权重，而是把「数据怎么造、模型怎么训、效果怎么测」整套流水线都开源出来，让任何人都能跑通、能改造。

#### 4.1.2 核心流程

open-r1 的整体策略可以抽象成一句话：

```
把 DeepSeek-R1 的技术报告当成「施工图纸」，
逐步把图纸里描述的每一段流水线，用开源代码补齐。
```

具体来说，项目把 R1 的完整能力拆成 **四条主链**，本手册的八个单元正是围绕这四条链展开：

1. **数据生成（Data Generation）**：用强模型批量产出带「思考过程」的训练数据。
2. **SFT 蒸馏（Distillation）**：用这些数据做监督微调，让小模型学会推理。
3. **GRPO 强化学习（Pure RL）**：不给标准答案，靠「打分」让模型自我进化。
4. **评估（Evaluation）**：用 AIME、MATH-500、GPQA、LiveCodeBench 等基准量化效果。

#### 4.1.3 源码精读

项目的自我介绍集中在 README 的 **Overview** 小节。先看这句点题的定位：

[README.md:3](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L3) —— 仓库副标题，明确写出「fully open reproduction of DeepSeek-R1」，这是理解整个项目动机的钥匙。

紧接着 Overview 说明了项目的**设计哲学**——「刻意保持简单（simple by design）」，核心代码其实只有几个脚本：

[README.md:19-28](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L19-L28) —— 这段用 bullet 列出了 `src/open_r1` 下的三大脚本和 `Makefile`，并说「The project is simple by design」。

> 这一点很重要：open-r1 **不是**一个庞大臃肿的框架，它的「主程序」非常薄，复杂度都委托给了成熟的第三方库（TRL、transformers、vLLM、Distilabel）。所以从源码学习的角度，它非常适合作为「读懂一个真实 LLM 训练系统」的入门样本。

#### 4.1.4 代码实践（源码阅读型）

> **实践目标**：亲手确认项目的「门面」信息，而不是依赖别人转述。

1. **操作步骤**：
   - 用编辑器或 `cat` 打开仓库根目录的 `README.md`，只读第 1–48 行（标题、Overview、Plan of attack、News）。
   - 找到 `assets/plan-of-attack.png`，打开这张图，先**不要**急着分析细节，只感受它大致画了几段流程。
2. **需要观察的现象**：
   - README 是否明确写了「work in progress」（仍在进行中）？
   - 是否有一行说明项目依赖 [DeepSeek-R1 技术报告](https://github.com/deepseek-ai/DeepSeek-R1) 作为指南？
3. **预期结果**：你会确认 open-r1 = 「以 DeepSeek-R1 技术报告为蓝图的开源复现工程」。
4. 关于运行：本实践纯阅读，**无需 GPU、无需安装**，可立即完成。

#### 4.1.5 小练习与答案

**练习 1**：open-r1 和 DeepSeek-R1 各自公开了什么？为什么还需要 open-r1？

> **参考答案**：DeepSeek-R1 公开了**模型权重 + 技术报告**，但没公开完整的训练代码/数据/配置；open-r1 的目标是补齐「完全开源」的这部分，让人能复现并二次开发。

**练习 2**：README 说项目「simple by design」，这对你阅读源码意味着什么？

> **参考答案**：意味着核心入口脚本很少（主要是 `sft.py`/`grpo.py`/`generate.py`），大部分底层能力（训练循环、分布式、生成加速）都委托给了 TRL/transformers/vLLM 等成熟库，所以源码学习门槛相对可控。

---

### 4.2 三步走计划：Distill → R1-Zero 纯 RL → 多阶段训练

#### 4.2.1 概念说明

DeepSeek 在 R1 的技术报告里，描述了他们如何一步步造出最终模型。open-r1 把这条路线概括成**三步走（three main steps）**，这也是本手册学习顺序的总纲：

- **Step 1：蒸馏复现（Distill）**。用 DeepSeek-R1（强模型）生成大量高质量「推理轨迹」，再拿这些数据去 SFT 训练小模型，复现出类似 `DeepSeek-R1-Distill-Qwen-7B` 的蒸馏模型。
- **Step 2：纯 RL 复现（R1-Zero 路线）**。复现 DeepSeek 用来造 **R1-Zero** 的纯强化学习流水线——不经过 SFT，直接从基础模型用 GRPO 打分训练。这一步通常需要为数学、推理、代码整理大规模数据集。
- **Step 3：多阶段训练（从 base 到 RL-tuned）**。把前两步串起来，演示如何从一个**基础模型（base model）**，经过多阶段训练，最终得到 RL 调优过的完整模型。

#### 4.2.2 核心流程

三步的依赖关系如下图（文字版流程）：

```
        ┌─────────────────────────────────────────────────────────┐
Step 1  │  DeepSeek-R1(老师) ──生成推理数据──▶ SFT ──▶ 蒸馏小模型    │  (先有"会推理"的学生)
        └─────────────────────────────────────────────────────────┘
                                  │ 数据与方法可复用
                                  ▼
        ┌─────────────────────────────────────────────────────────┐
Step 2  │  Base 模型 ──GRPO 纯 RL(打分)──▶ R1-Zero 风格模型          │  (证明"纯 RL 也能涌现推理")
        └─────────────────────────────────────────────────────────┘
                                  │ 把蒸馏与 RL 组合
                                  ▼
        ┌─────────────────────────────────────────────────────────┐
Step 3  │  Base ──▶ (冷启动/SFT) ──▶ RL ──▶ 拒绝采样+SFT ──▶ RL ──▶ 最终模型 │  (多阶段叠加)
        └─────────────────────────────────────────────────────────┘
```

读这张图的关键直觉：

- **Step 1 解决「数据从哪来」**：没有老师的优质推理样本，学生模型学不会思考。
- **Step 2 解决「能不能不靠老师、自己练出来」**：这是 R1-Zero 最惊艳的发现——纯 RL 也能让推理能力涌现。
- **Step 3 解决「如何把两者融合成最强形态」**：现实里最强的模型往往是「先 SFT 再 RL」的多阶段产物。

> 数学直觉（非公式，仅比喻）：强化学习的奖励信号可以抽象成在策略空间里「爬山」。Step 1 给你一个好的出发点（蒸馏初始化），Step 2 证明即使从随机出发点，靠奖励也能爬到不错的位置，Step 3 则是「先放到好位置、再爬山、再换起点、再爬山」的迭代策略。

#### 4.2.3 源码精读

三步走的官方表述在 README 的 **Plan of attack** 小节：

[README.md:30-36](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L30-L36) —— 这三行 bullet 就是 Step 1 / 2 / 3 的权威定义，并且明确说「以 DeepSeek-R1 技术报告为指南（guide）」。这是本讲最重要的一段引用。

配图在这里：

[README.md:38-40](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L38-L40) —— README 用 `<img>` 标签嵌入 `assets/plan-of-attack.png`，把上面三步画成了一张流程示意图。图片本体见 [assets/plan-of-attack.png](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/assets/plan-of-attack.png)。图中可以辨认出 DeepSeek-R1 流水线的典型阶段：从基础模型出发，经过冷启动（Cold Start）、推理导向的 RL（Reasoning RL）、拒绝采样与 SFT（Rejection Sampling & SFT）、再到多任务 RL，最终得到 R1；同时画出了一条「基础模型 → 纯 RL → R1-Zero」的对照支线。这张图正是 Step 3「多阶段」与 Step 2「纯 RL」的视觉化。

进展状态（哪些步已完成）记录在 News 区：

[README.md:44](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L44) —— 这条 News 标注 **Step 1 已完成（Step 1 completed）**：发布了 `Mixture-of-Thoughts` 数据集与 `OpenR1-Distill-7B` 模型，复现了 `DeepSeek-R1-Distill-Qwen-7B` 的推理能力。

> 含义：截至本讲，Step 1 已经有可跑通的端到端配方（recipe），而 Step 2、Step 3 仍在推进中。这也是为什么本手册「SFT 单元」讲得最完整、最贴近可复现实践。

#### 4.2.4 代码实践（源码阅读型）

> **实践目标**：把抽象的「三步」落回到仓库里的具体脚本/目录，建立「计划 ↔ 代码」的映射。

1. **操作步骤**：
   - 打开 README 的 Plan of attack（第 30–40 行）和 News（第 44 行附近）。
   - 对照下面这张「计划 ↔ 代码」映射表，在仓库里逐个找到对应文件：

   | 计划步骤 | 要解决的问题 | 对应仓库位置（本讲先定位，不细读） |
   |----------|--------------|--------------------------------------|
   | Step 1 蒸馏 | 造数据 + SFT | `src/open_r1/generate.py`（造数据）、`src/open_r1/sft.py`（训练）、`recipes/OpenR1-Distill-7B/`（配方） |
   | Step 2 纯 RL | 不靠老师、用 GRPO 自练 | `src/open_r1/grpo.py`、`src/open_r1/rewards.py`（打分函数）、`recipes/Qwen2.5-1.5B-Instruct/grpo/` |
   | Step 3 多阶段 | 组合 SFT+RL | 上述脚本的串联使用 + `slurm/train.slurm`（多机多阶段编排） |

2. **需要观察的现象**：
   - 你能否在仓库根目录看到 `src/open_r1/`、`recipes/`、`slurm/` 这几个目录？
   - News 里 Step 1 完成时提到的两个产物（数据集名、模型名）分别是什么？
3. **预期结果**：能说出「Step 1 = generate.py + sft.py」「Step 2 = grpo.py + rewards.py」这样的映射。
4. 关于运行：纯阅读定位，**无需安装**。

#### 4.2.5 小练习与答案

**练习 1**：Step 1（蒸馏）和 Step 2（纯 RL）最本质的区别是什么？

> **参考答案**：Step 1 依赖一个强模型（DeepSeek-R1）先**生成标准答案式的推理数据**再做 SFT；Step 2 **不依赖老师的标准答案**，而是靠奖励函数对模型自己生成的输出打分、用 GRPO 自我进化。前者是「模仿」，后者是「试错+奖励」。

**练习 2**：为什么 README 要强调 Step 2「will likely involve curating new, large-scale datasets」？

> **参考答案**：纯 RL 需要大量**可验证对错**的题目（数学题有标准答案、代码题有测试用例）才能算奖励，所以必须先整理大规模、可判分的数据集，RL 才有信号可学。

**练习 3**：Step 1 已完成，那 Step 2/3 在本仓库里是否已有代码？

> **参考答案**：已有起步代码——`grpo.py` 和一整套奖励函数（`rewards.py`）就是为 Step 2/3 准备的，但 README 的 News 未宣布 Step 2/3 完成，说明它们仍在迭代。

---

### 4.3 三大脚本与 Makefile 的角色

#### 4.3.1 概念说明

了解了「为什么做」和「分几步做」，最后要看「代码长什么样」。open-r1 的可执行核心只有三个 Python 脚本，加上一个 `Makefile` 当「总调度」：

- **`sft.py`**：监督微调入口。读数据、加载模型和分词器、用 TRL 的 `SFTTrainer` 训练、保存并推送到 Hugging Face Hub。对应 Step 1。
- **`grpo.py`**：GRPO 强化学习入口。结构和 `sft.py` 类似，但训练器换成 `GRPOTrainer`，并注入一组**奖励函数**。对应 Step 2/3。
- **`generate.py`**：数据生成入口。用 Distilabel 搭一条流水线，批量让模型产出推理数据。对应 Step 1 的「造数据」。
- **`Makefile`**：把常用命令封装成 `make xxx`，比如 `make install`（一键装环境）、`make evaluate`（一键跑评估）、`make test`（跑测试）。

#### 4.3.2 核心流程

三个脚本 + Makefile 的协作关系：

```
            ┌─────────────┐
            │  Makefile   │  make install / make evaluate / make test
            └──────┬──────┘
       （封装快捷命令）│
    ┌───────────────┼────────────────────┐
    ▼               ▼                    ▼
┌────────┐     ┌────────┐          ┌──────────────┐
│sft.py  │     │grpo.py │          │ generate.py  │
│ SFT训练 │     │ RL训练  │          │  数据生成     │
│(Step 1)│     │(Step2/3)│         │ (Step1 数据) │
└───┬────┘     └───┬────┘          └──────┬───────┘
    │              │                      │
    └──────► 都依赖 TRL / transformers ◄──┘
                 （底层训练与生成能力）
```

要点：

- 三个脚本都**很薄**：真正的训练循环来自 TRL（`SFTTrainer` / `GRPOTrainer`），脚本只负责「组装配置 + 调用训练器」。
- `Makefile` 不是必需品，但它是「降低记忆负担」的工具——你不用记住一长串 `accelerate launch ...` 命令。

#### 4.3.3 源码精读

README 对三大脚本的职责定义只有三行，但句句是权威：

[README.md:24-27](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L24-L27) —— 逐行说明 `grpo.py`（GRPO 训练）、`sft.py`（简单 SFT）、`generate.py`（用 Distilabel 生成合成数据）。

[README.md:28](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L28) —— 一句话定义 `Makefile`：为流水线每一步提供「易运行的命令」。

再看三个脚本各自的开头自述（验证 README 说的没错）：

[src/open_r1/sft.py:15-34](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L15-L34) —— 模块 docstring 写明「Supervised fine-tuning script for decoder language models」（解码器语言模型的监督微调脚本），并给出一段 `accelerate launch ... sft.py` 的用法示例。

[src/open_r1/grpo.py:35](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L35) —— `grpo.py` 的核心入口 `def main(script_args, training_args, model_args)`，结构与 `sft.py` 一致：接收「脚本参数 + 训练参数 + 模型参数」三元组。这个三元组是后续讲义（配置系统）的重点。

[src/open_r1/generate.py:23-36](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/generate.py#L23-L36) —— `build_distilabel_pipeline(...)` 函数签名，参数包括 `model`、`base_url`（vLLM/OpenAI 服务地址）、`prompt_template`、`num_generations`、`client_replicas` 等。这就是「数据生成流水线」的组装函数。

最后看 Makefile 提供了哪些快捷命令：

[Makefile:10-16](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L10-L16) —— `install` 目标：一行命令创建 `uv` 虚拟环境、装 vLLM、装 flash-attn、再以可编辑模式安装 `.[dev]` 开发依赖。

[Makefile:18-31](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L18-L31) —— `style` / `quality` / `test` / `slow_test` 四个代码质量与测试目标（分别用 ruff、isort、flake8、pytest）。

[Makefile:35-53](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L35-L53) —— `evaluate` 目标：把 `lighteval vllm ...` 这条长命令封装成 `make evaluate MODEL=... TASK=...`，并支持 `data`/`tensor` 两种并行。

#### 4.3.4 代码实践（命令体验型）

> **实践目标**：用 `make` 命令「触摸」一下项目，建立「Makefile 就是快捷方式」的肌肉记忆。本实践不训练模型，零算力可完成。

1. **实践目标**：列出 Makefile 提供的所有目标，并解读其中两个。
2. **操作步骤**：
   - 在仓库根目录执行：`make help`（若无 `help` 目标则直接 `cat Makefile`）。
   - 找到 `install` 和 `evaluate` 两个目标，阅读它们展开后的真实命令。
3. **需要观察的现象**：
   - `make install` 实际上调用了哪些 `uv pip install ...`？
   - `make evaluate` 内部用什么变量（`MODEL`、`TASK`、`PARALLEL`、`NUM_GPUS`）来拼 `lighteval` 命令？
4. **预期结果**：你能向别人解释「`make evaluate MODEL=X TASK=aime24` 会被翻译成一条 `lighteval vllm ...` 命令」。
5. 关于运行：仅阅读 Makefile **无需安装**；若想真正执行 `make install`，需要 Linux + CUDA 12.4 环境，否则**待本地验证**（README 明确提示库依赖 CUDA 12.4）。

#### 4.3.5 小练习与答案

**练习 1**：`sft.py` 和 `grpo.py` 的 `main` 函数签名几乎一样（都接收三个参数），为什么？

> **参考答案**：因为它们用同一套配置解析机制（`TrlParser` 解析 `ScriptArguments`、`TrainingConfig`、`ModelConfig` 三元组），共用 README 描述的「YAML + 命令行」配置系统。这让你切换 SFT/GRPO 时只需换脚本和配置，参数风格保持一致。

**练习 2**：`generate.py` 里的 `base_url` 参数（默认 `http://localhost:8000/v1`）暗示了什么？

> **参考答案**：数据生成不是「在脚本里直接加载模型」，而是**通过一个 OpenAI 兼容的 HTTP 服务**（通常是 vLLM 起的服务）来批量生成。所以生成前要先起一个推理服务，`generate.py` 只是把请求发给它。

**练习 3**：既然三个脚本已经能直接 `python src/open_r1/xxx.py` 运行，为什么还要 Makefile？

> **参考答案**：真实训练命令往往很长（`accelerate launch --config_file ... --一长串参数`），Makefile 把高频命令封装成 `make xxx`，降低记忆成本、减少手误，也让 CI 和文档里的命令更简洁。

---

## 5. 综合实践

**任务：画一张属于你自己的「open-r1 全景图」。**

把本讲三个模块的知识串起来，完成下面这张表（建议用纸笔或 Markdown 表格）：

| 维度 | 你的回答 |
|------|----------|
| 项目一句话定位 | ______ |
| 复现的蓝本是什么 | ______ |
| Step 1 要解决什么问题，用到哪些脚本/目录 | ______ |
| Step 2 要解决什么问题，用到哪些脚本/目录 | ______ |
| Step 3 要解决什么问题，用到哪些脚本/目录 | ______ |
| 三大脚本各自的一句话职责 | sft.py：______；grpo.py：______；generate.py：______ |
| Makefile 的作用 | ______ |
| 目前 News 里哪一步已完成 | ______ |

完成步骤：

1. 重读 [README.md:19-44](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L19-L44)。
2. 打开 [assets/plan-of-attack.png](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/assets/plan-of-attack.png)，尝试把图里的每个方框对应到上表的某一行。
3. 在仓库里**实际打开** `src/open_r1/sft.py`、`src/open_r1/grpo.py`、`src/open_r1/generate.py` 各看前 40 行，确认它们的开头描述和你在表里写的一致。

**验收标准**：你能不看任何资料，用 3 分钟向一个没接触过本项目的人讲清楚「open-r1 是什么、分几步、代码在哪」。做到这一点，本讲就过关了。

---

## 6. 本讲小结

- **open-r1** 是对 **DeepSeek-R1** 的「完全开源复现」：DeepSeek 只放了权重和技术报告，open-r1 补齐了完整的数据/训练/评估代码。
- 项目以 DeepSeek-R1 **技术报告**为施工图纸，路线概括为**三步走**：蒸馏（Step 1）、纯 RL 即 R1-Zero 路线（Step 2）、多阶段训练（Step 3）。
- 三步之间的依赖是：Step 1 提供「会推理的初始化与数据」，Step 2 证明「纯 RL 也能涌现推理」，Step 3 把两者融合成最强形态。
- 可执行核心只有三个薄脚本：`sft.py`（SFT 蒸馏）、`grpo.py`（GRPO 强化学习）、`generate.py`（Distilabel 数据生成），复杂度都委托给 TRL/transformers/vLLM。
- `Makefile` 是「快捷方式集合」，把装环境、跑评估、跑测试封装成 `make install` / `make evaluate` / `make test`。
- 截至当前 HEAD，News 显示 **Step 1 已完成**（`Mixture-of-Thoughts` 数据集 + `OpenR1-Distill-7B` 模型），Step 2/3 仍在推进。

---

## 7. 下一步学习建议

本讲建立了全局观，接下来建议**先把「地形」看清，再深入任何一条链**：

1. **下一讲（u1-l2 仓库目录结构与核心入口）**：系统走一遍 `src/`、`scripts/`、`recipes/`、`slurm/`、`tests/` 的目录职责，把本讲点到的文件放进完整目录树里。
2. **然后（u1-l3 安装与环境搭建、u1-l4 配置系统）**：学会用 `make install` 搭环境，并理解贯穿三大脚本的「YAML + 命令行」配置系统——这是读懂后续所有脚本的前提。
3. **进入主链**：配置系统搞懂后，第二单元从 `sft.py` 开始逐行精读（最完整、最可复现的 Step 1），第三单元再攻 `grpo.py` 与奖励函数（本项目最有特色的部分）。

> 建议的延伸阅读（仓库外）：DeepSeek-R1 官方 [技术报告仓库](https://github.com/deepseek-ai/DeepSeek-R1)，对照本讲的「三步走」会更有体感。

# RTL-Coder 项目总览与定位

## 1. 本讲目标

本讲是整本学习手册的第一篇，面向「完全没接触过 RTL-Coder」的读者。读完本讲，你应当能够：

- 说清楚 RTL-Coder 要解决什么问题（Verilog/RTL 代码生成中的数据稀缺痛点）。
- 用一句话讲明白它的两大核心贡献：**自动化数据集生成** 与 **基于代码质量评分的训练**。
- 区分项目在 HuggingFace 上发布的四个模型（Deepseek / Mistral / GPTQ / GGUF）各自的特点与适用场景。
- 认识项目用到的两个评测基准：**VerilogEval** 与 **RTLLM**。
- 在仓库里把「数据生成」和「训练」两个阶段分别对应到正确的文件夹。

本讲只读一个源码文件——`README.md`，但它信息密度很高，是理解整个项目的「地图」。后续每一篇讲义都会回到这张地图上的某个点深入展开。

## 2. 前置知识

在开始前，建议你大致了解以下几个概念（不懂也没关系，本讲会用通俗语言再解释一次）：

- **Verilog**：一种硬件描述语言（HDL），工程师用它写出数字电路的「代码」，再由综合工具转成真实的电路。可以把它类比成「给硬件写代码的编程语言」。
- **RTL（Register Transfer Level，寄存器传输级）**：Verilog 常见的一种抽象层级，描述数据在寄存器之间如何流动和运算。在本项目语境里，「RTL 代码」基本等同于「Verilog 代码」。
- **LLM（大语言模型）**：像 GPT、Mistral、DeepSeek 这类模型，本来的强项是生成自然语言文本；RTL-Coder 的思路是「让它学会生成 Verilog」。
- **微调（Fine-tuning）**：在一个已经预训练好的通用模型基础上，用领域数据再训练，让它专门擅长某项任务。
- **HuggingFace**：一个托管模型与数据集的平台，RTL-Coder 把训练好的模型发布在这里，任何人都能下载使用。

> 一句话定位：**RTL-Coder 是一个把通用大语言模型「改造」成 Verilog 代码生成器的开源项目，并且整套数据生成 + 训练流程都公开。**

## 3. 本讲源码地图

本讲只涉及一个文件，但它是全项目的「总入口」：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md) | 项目主页说明：论文、四大模型、两大贡献、数据生成、评测与训练命令的集合地。 |

为了让你对项目有「全局感」，这里先给一张仓库目录速览（**本讲不深入这些目录，只做定位**，后续讲义会逐一精读）：

```
RTL-Coder/
├── README.md                  ← 本讲主角：项目总说明
├── requirements.txt           ← 依赖列表
├── data_generation/           ← 【贡献一】自动化数据集生成的脚本与样本
│   ├── instruction_gen.py
│   ├── utils.py
│   ├── p_example.txt
│   └── data_sample.json
├── dataset/
│   └── Resyn27k.json          ← 生成的 2.7 万条「指令-代码」数据集
├── train/                     ← 【贡献二】三种训练方案 + DeepSpeed 配置
│   ├── mle.py
│   ├── mle_scoring.py
│   ├── mle_scoring_grad_split.py
│   ├── ds_stage_2.json
│   └── scoring_data_sample.json
└── benchmark_inference/       ← 两个评测基准的推理脚本
    ├── test_on_verilog-eval.py
    ├── test_on_rtllm.py
    ├── rtllm-1.1.json
    └── rtllm.json
```

记住两个关键对应关系：**数据生成 → `data_generation/` 文件夹**；**训练 → `train/` 文件夹**。这是本讲综合实践要标注的重点。

---

## 4. 核心概念与源码讲解

### 4.1 背景：RTL 代码生成与数据稀缺痛点

#### 4.1.1 概念说明

要理解 RTL-Coder 为什么有价值，先要理解它所处的「困境」。

芯片设计（IC design）领域严重依赖 Verilog 代码，但相比于自然语言（互联网上有无穷无尽的文本），**高质量的「Verilog 设计问题描述 + 对应参考代码」数据极度稀缺**。原因很现实：

- 写 Verilog 需要专业硬件知识，能写的人少。
- 公开的、带清晰需求描述的 Verilog 代码库很少。
- 人工标注一条「需求 → 代码」样本成本很高。

而大语言模型又是个「吃数据」的东西——没有足够多的领域训练数据，再强的通用模型也学不会写好 Verilog。这就是 RTL-Coder 要解决的核心痛点：**用自动化方法，大规模制造 RTL 训练数据**。

> **关键直觉**：项目不是「再训一个更强的通用模型」，而是「用巧妙的自动化流程，造出一份数据集，让现成的开源模型学会写 Verilog，并达到甚至超过 GPT-3.5 的水平」。

#### 4.1.2 核心流程

从「痛点」到「方案」的逻辑链可以概括为：

1. **痛点**：Verilog「需求-代码」训练数据稀缺。
2. **观察**：商用大模型（GPT）有极强的通用文本生成能力。
3. **思路**：能否让 GPT 帮我们「批量造」这种训练数据？
4. **约束**：GPT 只用于造数据，不参与最终的 RTL-Coder 模型本身。
5. **结果**：得到一份 2.7 万条样本的开源数据集，用来训练开源模型。

#### 4.1.3 源码精读

README 在「Repo-intro」一节直接点明了这个定位：

[README.md:L36-L39](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L36-L39) —— 这一段说明项目面向 Verilog 代码生成，提出了一条自动化流程来生成大规模带标签数据集，**正是为了应对 IC 设计任务中严重的数据可得性挑战**；直接用这份数据训练出的 LLM，已经能达到与 GPT-3.5 相当的精度。

紧接着：

[README.md:L50-L50](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L50) —— 明确指出项目在 HuggingFace 上提供了**四个** RTL 代码生成模型（下一节 4.3 详解）。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，帮你确认对痛点的理解。

1. **实践目标**：在 README 中找到描述「数据稀缺痛点」的原文。
2. **操作步骤**：
   - 打开 `README.md`，定位到第 36–39 行（Repo-intro 段落）。
   - 把其中描述「为什么需要自动化生成数据集」的那句话摘抄下来。
3. **需要观察的现象**：注意作者用了哪个关键词来形容数据问题（提示：与「可获得性 / availability」相关）。
4. **预期结果**：你应该能找到类似 "serious data availability challenge" 的表述，对应中文即「严重的数据可得性挑战」。
5. **待本地验证**：若你的 README 行号与本讲不一致，用搜索功能查找关键词 `availability` 定位即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能直接拿通用大模型生成 Verilog，而要先造一份数据集？
**参考答案**：通用模型预训练时见过的 Verilog 太少，缺乏「需求 → 代码」的成对样本；没有足够的领域数据做微调，模型无法稳定产出高质量 Verilog。RTL-Coder 的核心就是先用自动化流程补上这份数据。

**练习 2**：项目明确说 GPT 在流程中只扮演什么角色？为什么有这个限制？
**参考答案**：GPT 只用于**数据集生成**，不参与最终的 RTL-Coder 模型。README 也强调遵守 OpenAI 的服务条款，且与 OpenAI 模型不存在商业竞争——最终发布的 RTL-Coder 是基于开源底座（Mistral / DeepSeek）训练的独立模型。

---

### 4.2 两大核心贡献：自动化数据集生成 + 质量评分训练

#### 4.2.1 概念说明

README 的「RTLCoder-flow」一节开宗明义：取得 RTLCoder 有**两大贡献**。这是整个项目的「骨架」，后续几乎所有讲义都围绕这两条线展开。

- **贡献一：自动化数据集生成流程**。
  解决「数据从哪来」的问题。它用 GPT 自动造出了超过 2.7 万条样本，每条样本是一对「设计描述指令 + 对应参考代码」。这条线对应仓库的 `data_generation/` 和 `dataset/` 文件夹。

- **贡献二：基于代码质量评分的新训练方案**。
  解决「怎么训得更好」的问题。它在普通微调基础上引入「代码质量分数」作为反馈，进一步提升模型性能，甚至超过 GPT-3.5；同时作者还从算法角度改造训练过程，**降低显存消耗**，让普通硬件也能跑。这条线对应仓库的 `train/` 文件夹。

一句话区分：**贡献一管「造数据」，贡献二管「用数据训出更好的模型」**。

#### 4.2.2 核心流程

贡献一的数据生成流程分**三个阶段**（对应 README 的 Figure 1）：

```
阶段 1: RTL 领域关键词准备   ──┐
阶段 2: 指令生成 (instruction) ─┤── 由 GPT 在提示词模板驱动下完成
阶段 3: 参考代码生成          ──┘
        ↓
   得到「指令-代码」成对样本（最终汇聚成 Resyn27k.json）
```

贡献二的训练流程（对应 README 的 Figure 2）：在普通监督微调（MLE）之上，加入「质量评分」信号，并通过算法改造降低显存，使评分训练能在有限硬件上实现。

#### 4.2.3 源码精读

README 第 62 行起明确点出「two main contributions」：

[README.md:L62-L64](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L62-L64) —— 贡献一：自动化数据集生成流程，生成超过 2.7 万条样本，每条是「设计描述指令 + 对应参考代码」一对；并强调 GPT 仅用于数据生成，遵守 OpenAI 服务条款。流程含三个阶段（关键词准备 / 指令生成 / 参考代码生成）。

[README.md:L67-L69](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L67-L69) —— Figure 1：自动化数据集生成流程示意图（图片资源 `_pic/data_gen_flow.jpg`）。

[README.md:L71-L71](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L71-L71) —— 贡献二：提出融入代码质量评分的新训练方案，显著提升 RTL 生成性能；并从算法角度降低该训练方法的显存消耗，使其能在有限硬件上实现。

[README.md:L74-L76](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L74-L76) —— Figure 2：基于 RTL 质量评分的训练方案示意图（图片资源 `_pic/training_flow.jpg`）。

数据生成流程对应的代码位置，README 在「Dataset」一节给出：

[README.md:L81-L87](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L81-L87) —— 数据生成脚本与样本放在 `data_generation` 文件夹；通过 `python instruction_gen.py` 可扩展数据集；2.7 万条的 `Resyn-27k.json` 放在 `dataset` 文件夹，由 GPT-3.5-turbo 生成（注意：不能保证所有样本都完全正确）。

训练流程对应的代码位置，README 在「Training」一节给出：

[README.md:L236-L236](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L236-L236) —— 提供三种指令微调方案（MLE 直接训练 / 评分训练 / 带梯度切分的评分训练），详见 `train` 文件夹与论文。

#### 4.2.4 代码实践

1. **实践目标**：把两大贡献各自对应到仓库的具体文件夹。
2. **操作步骤**：
   - 在纸上画两列：左列写「贡献一 / 贡献二」，右列写对应文件夹。
   - 对照本讲「3. 本讲源码地图」的目录树填写。
3. **需要观察的现象**：注意贡献一占用了**两个**文件夹（脚本 + 数据），而贡献二主要集中在**一个**文件夹。
4. **预期结果**：
   - 贡献一（数据集生成）→ `data_generation/`（脚本）+ `dataset/`（生成的数据 `Resyn27k.json`）。
   - 贡献二（训练）→ `train/`（`mle.py` / `mle_scoring.py` / `mle_scoring_grad_split.py`）。
5. **待本地验证**：可用 `ls data_generation dataset train` 在本地确认这三个文件夹确实存在。

#### 4.2.5 小练习与答案

**练习 1**：贡献一的三个阶段分别是什么？哪个阶段产生「设计描述指令」？
**参考答案**：三个阶段依次是 ①RTL 领域关键词准备、②指令生成、③参考代码生成。产生「设计描述指令」的是**阶段 2（指令生成）**；阶段 3 则为该指令配上参考代码。

**练习 2**：贡献二除了「引入质量评分」，还做了什么额外工作？为什么必要？
**参考答案**：还从**算法角度降低了该训练方法的 GPU 显存消耗**。因为引入质量评分后训练更「重」（要处理多个候选代码），普通硬件可能跑不动；作者因此改造训练过程（后续 `mle_scoring_grad_split.py` 讲义会深入），让有限显存也能训练。

---

### 4.3 四个 HuggingFace 模型定位

#### 4.3.1 概念说明

项目不止提供「方法和数据」，还直接发布了**四个开箱即用的模型**。它们都托管在 HuggingFace 的 `ishorn5` 名字空间下。理解它们的差异，能帮你快速选型：

| 模型 | 底座 | 精度 | 主要特点 | 适用场景 |
| --- | --- | --- | --- | --- |
| **RTLCoder-Deepseek-v1.1** | DeepSeek-coder-6.7b | fp16 | 评测精度最高，但推理速度较慢；输出可能不停，需特殊后处理 | 追求最高精度、有 GPU |
| **RTLCoder-v1.1** | Mistral-v0.1 | fp16 | 默认推荐；输出会自然停止 | 通用 GPU 推理 |
| **RTLCoder-v1.1-gptq-4bit** | Mistral-v0.1 | 4bit(GPU) | GPTQ 4 比特量化，显存占用更小 | 显存吃紧的 GPU |
| **RTLCoder-v1.1-gguf-4bit** | Mistral-v0.1 | 4bit(CPU) | GGUF 量化，可在 CPU 上运行 | 没有 GPU / 想在 CPU 跑 |

> **选型直觉**：要精度选 Deepseek；要通用选 Mistral；显存紧选 GPTQ；没 GPU 选 GGUF。

#### 4.3.2 核心流程

四个模型可按两个维度分类：

- **底座不同**：Deepseek 版基于 DeepSeek-coder-6.7b；其余三个都基于 Mistral-v0.1。
- **精度/部署不同**：Deepseek 与 v1.1 是全精度（fp16）；gptq-4bit 是 GPU 量化；gguf-4bit 是 CPU 量化。

值得特别注意的是 **Deepseek 版的后处理差异**：它「输出可能不会自动停止」，所以需要用关键字 `endmodulemodule` 截取代码部分并补一个 `endmodule`。这是后续「输出抽取」讲义（u1-l5）的重点，这里先建立印象。

#### 4.3.3 源码精读

README 用一个列表给出四个模型：

[README.md:L52-L56](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L52-L56) —— 四个模型清单：
- 第 52–53 行：`RTLCoder-Deepseek-v1.1`，基于 DeepSeek-coder-6.7b 微调，在 VerilogEval 与 RTLLM 上精度最好但速度较慢；需要用 `endmodulemodule` 关键字抽取代码。
- 第 54 行：`RTLCoder-v1.1`，基于 Mistral-v0.1 微调。
- 第 55 行：`RTLCoder-v1.1-gptq-4bit`，v1.1 的 GPTQ 版本。
- 第 56 行：`RTLCoder-v1.1-gguf-4bit`，量化版可在 CPU 上运行。

README 顶部的「Important」提示也强调了 Deepseek 版的特殊性：

[README.md:L33-L33](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L33-L33) —— 提醒：测试 RTLCoder-Deepseek 时，要看 `benchmark_inference/test_on_verilog-eval.py` 里的注释，那里有为 Deepseek 设计的响应后处理方法；默认推理脚本是为 RTLCoder-Mistral 准备的。

#### 4.3.4 代码实践

1. **实践目标**：根据使用场景，选出最合适的模型。
2. **操作步骤**：
   - 假设有三种需求：(a) 实验室有一张 24GB GPU，追求最高精度；(b) 只有一台笔记本、无 GPU；(c) 有一张 8GB 显存的小显卡。
   - 对照上面的模型对比表，为每种需求挑选一个模型。
3. **需要观察的现象**：思考为什么 (a) 不选 Mistral、(b) 只能选 GGUF。
4. **预期结果**：(a) → RTLCoder-Deepseek-v1.1；(b) → RTLCoder-v1.1-gguf-4bit；(c) → RTLCoder-v1.1-gptq-4bit。
5. **待本地验证**：可访问对应 HuggingFace 页面确认模型卡是否存在（`ishorn5/RTLCoder-Deepseek-v1.1` 等）。

#### 4.3.5 小练习与答案

**练习 1**：四个模型里，哪几个是基于 Mistral-v0.1 的？哪一个是唯一基于 DeepSeek 的？
**参考答案**：基于 Mistral-v0.1 的有 `RTLCoder-v1.1`、`RTLCoder-v1.1-gptq-4bit`、`RTLCoder-v1.1-gguf-4bit` 三个；唯一基于 DeepSeek-coder-6.7b 的是 `RTLCoder-Deepseek-v1.1`。

**练习 2**：为什么 Deepseek 版需要「特殊后处理」，而 Mistral 版默认不需要？
**参考答案**：Deepseek 版即使所需输出已经完成，也可能继续生成 token 不停止；因此需要用关键字 `endmodulemodule` 把代码部分截取出来并补上 `endmodule`。Mistral 版完成任务后会自动停止，所以默认不需要这套截取（但仍可用 `endmodule` 做兜底抽取）。

---

### 4.4 两个评测基准：VerilogEval 与 RTLLM

#### 4.4.1 概念说明

「模型好不好」不能凭感觉，要用标准化的**基准（benchmark）**来测。RTL-Coder 主要用两个公开基准：

- **VerilogEval**（来自 NVIDIA 的 NVlabs）：一套机器可评测的 Verilog 代码生成基准，分为 `EvalMachine`（Machine 类题目）和 `EvalHuman`（Human 类题目）两种子集。
- **RTLLM**（来自 hkust-zhiyao）：另一套 RTL 生成评测基准，项目把它的题目描述整理成了 `rtllm-1.1.json`。

项目为这两个基准各提供了一个推理脚本，放在 `benchmark_inference/` 文件夹。注意：这些脚本只负责**让模型生成代码并保存**，真正「判分」（代码能不能通过测试）要交给基准仓库自己的评测工具。

> **关键区分**：推理脚本 ≠ 评测工具。脚本负责「生成」，基准仓库负责「打分」。

#### 4.4.2 核心流程

两个基准的使用流程类似：

```
1. 准备基准数据（VerilogEval 需另外 clone；RTLLM 项目已提供 rtllm-1.1.json）
2. 运行对应的推理脚本，传入模型 / 温度 / 候选数 n 等参数
3. 脚本让模型对每道题生成 n 个候选代码，保存成 JSON / .v 文件
4. 用基准仓库的工具对生成结果判分，得到通过率
```

两个脚本的「落盘方式」略有不同（一个按 JSON、一个按 design 关键字落 `.v` 文件），这些细节会在 u1-l5 与 u2-l8 讲义展开。

#### 4.4.3 源码精读

README 的「Benchmarking」一节分别给出两个基准的用法：

[README.md:L206-L206](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L206-L206) —— VerilogEval 的推理脚本 `test_on_verilog-eval.py`，位于 `benchmark_inference` 文件夹；需先 `git clone` NVlabs 的 verilog-eval 基准。

[README.md:L214-L216](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L214-L216) —— VerilogEval 在 EvalMachine 上的推理命令示例，关键参数：`--model`（模型路径或名）、`--n 20`（候选数）、`--temperature=0.2`、`--gpu_name 0`、`--bench_type Machine`。换 `--bench_type Human` 即可测 EvalHuman。

[README.md:L225-L225](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L225-L225) —— RTLLM 的题目描述整理在 `rtllm-1.1.json`，位于 `benchmark_inference` 文件夹。

[README.md:L228-L229](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L228-L229) —— RTLLM 推理命令示例：`test_on_rtllm.py`，参数含 `--model`、`--n 5`、`--temperature=0.5`、`--gpu_name 0`、`--output_dir`。

> 说明：README 在 VerilogEval 命令里写的是 `test_on_nvbench.py`（第 211、215、219 行），而「实际提供的脚本文件名」是 `test_on_verilog-eval.py`（第 206 行）。这是 README 里的一处命名不一致，以仓库里实际存在的文件 `test_on_verilog-eval.py` 为准。

#### 4.4.4 代码实践

1. **实践目标**：识别两个基准推理脚本的关键参数差异。
2. **操作步骤**：
   - 阅读上面的命令示例。
   - 找出 VerilogEval 示例与 RTLLM 示例在 `--n` 和 `--temperature` 上的默认取值差异。
3. **需要观察的现象**：思考「候选数 n」和「温度 temperature」分别控制什么（提示：n 控制生成几份、temperature 控制随机性）。
4. **预期结果**：VerilogEval 示例为 `--n 20 --temperature=0.2`；RTLLM 示例为 `--n 5 --temperature=0.5`。
5. **待本地验证**：若想真正运行，需要先准备好 GPU 与对应模型权重；本讲不必运行，留到 u2-l8 讲义实操。

#### 4.4.5 小练习与答案

**练习 1**：项目为 VerilogEval 和 RTLLM 分别提供了哪个脚本文件？
**参考答案**：VerilogEval 对应 `benchmark_inference/test_on_verilog-eval.py`；RTLLM 对应 `benchmark_inference/test_on_rtllm.py`。

**练习 2**：为什么说「推理脚本」本身并不完成最终打分？
**参考答案**：推理脚本只负责让模型对每道题生成候选代码并保存；代码是否正确（能否通过测试台）需要交给 VerilogEval / RTLLM 各自基准仓库的评测工具去判分。所以脚本的产出是「生成结果」，基准工具的产出才是「通过率」。

---

## 5. 综合实践

**任务：画出从「原始需求」到「可推理模型」的整体流程图，并标注两个阶段对应的代码文件夹。**

这是本讲的收尾实践，目的是把四大模块串成一张完整地图。请按以下步骤完成：

1. **实践目标**：用一张流程图，把「数据生成」和「训练」两大阶段、它们的子步骤、以及对应的仓库文件夹串起来。
2. **操作步骤**：
   - 准备一张纸或任意画图工具。
   - 画出如下主干（你可以自行美化）：

   ```
   ┌───────────────────────── 数据生成阶段（贡献一）─────────────────────────┐
   │                                                                         │
   │  RTL 关键词准备 → 指令生成 → 参考代码生成  ⇒  「指令-代码」样本对          │
   │      (GPT 在提示词模板驱动下完成)                       ↓                │
   │                                            汇聚成 Resyn27k.json          │
   │   对应文件夹：data_generation/  +  dataset/                              │
   └─────────────────────────────────────────────────────────────────────────┘
                                      ↓
   ┌───────────────────────── 训练阶段（贡献二）─────────────────────────────┐
   │                                                                         │
   │  MLE 直接训练   /   评分训练   /   带梯度切分的评分训练                     │
   │      （引入代码质量评分 + 算法层降低显存）                                  │
   │   对应文件夹：train/  (mle.py / mle_scoring.py / mle_scoring_grad_split.py)│
   └─────────────────────────────────────────────────────────────────────────┘
                                      ↓
                        得到可推理的 RTL-Coder 模型
                                      ↓
   ┌───────────────────────── 评测阶段 ──────────────────────────────────────┐
   │  VerilogEval  /  RTLLM   （benchmark_inference/ 里的推理脚本生成代码）     │
   └─────────────────────────────────────────────────────────────────────────┘
   ```
   - 在图上**用不同颜色**标出「数据生成阶段」和「训练阶段」。
   - 在每个阶段旁边**标注对应的仓库文件夹**。
3. **需要观察的现象**：注意 GPT 只出现在数据生成阶段，训练阶段用的是开源底座（Mistral / DeepSeek）；评测阶段只读模型、不改模型。
4. **预期结果**：得到一张清晰的四阶段图（数据生成 → 训练 → 模型 → 评测），且两个核心阶段正确对应到 `data_generation/+dataset/` 与 `train/`。
5. **待本地验证**：对照本讲「3. 本讲源码地图」的目录树，检查你标注的文件夹是否都真实存在（可用 `ls` 确认）。

> 这个流程图建议保留下来——后续每一篇讲义，都是这张图上某个方框的「放大版」。

## 6. 本讲小结

- RTL-Coder 的核心痛点是 **Verilog/RTL 训练数据稀缺**，它用「自动化造数据」破局。
- 项目有**两大贡献**：①自动化数据集生成流程（造数据）；②基于代码质量评分的训练方案（训得更好，还省显存）。
- 数据生成对应 `data_generation/` + `dataset/`；训练对应 `train/`；评测对应 `benchmark_inference/`。
- 项目发布**四个 HuggingFace 模型**：Deepseek（精度最高、需后处理）、Mistral（默认推荐）、GPTQ-4bit（省显存）、GGUF-4bit（可跑 CPU）。
- 评测用**两个基准**：VerilogEval（Machine/Human）与 RTLLM；推理脚本只负责生成，打分交给基准工具。
- GPT 仅用于数据生成，最终模型基于开源底座，与 OpenAI 无商业竞争。

## 7. 下一步学习建议

本讲建立了「全局地图」，下一步建议沿着地图逐层下钻：

- **紧接着读 u1-l2（仓库结构与依赖）**：搞清每个文件夹、每个文件、以及 `requirements.txt` 里 `torch / transformers / deepspeed / openai` 等依赖的作用，为后续动手做准备。
- **想立刻体验模型**：可跳到 u1-l3（快速上手推理），用 README 的代码片段加载模型生成一段 Verilog。
- **想理解数据**：读 u1-l4（数据格式详解），看懂三种 JSON 数据结构。
- **长期路线**：入门层（u1）打基础 → 进阶层（u2）精读数据生成与训练主链路 → 专家层（u3）攻质量评分损失、显存切分与 DeepSpeed。

> 建议按讲义顺序学习：先 u1-l2 把结构与依赖理清，再进入具体脚本。每篇讲义开头都会说明它对应地图上的哪个方框。

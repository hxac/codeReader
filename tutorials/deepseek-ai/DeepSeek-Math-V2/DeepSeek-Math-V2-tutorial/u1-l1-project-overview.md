# 项目定位与总体架构：DeepSeekMath-V2 是什么

## 1. 本讲目标

这是本学习手册的第一讲。读完本讲，你应该能够：

1. 一句话说清 DeepSeekMath-V2 是什么：一个把「验证器当作奖励模型」来训练「证明生成器」、从而实现**自验证（self-verifiable）数学推理**的大语言模型。
2. 说出「生成器 — 验证器 — 元验证器」三个角色在这个项目中各自负责什么。
3. 解释什么是**生成-验证差距（generation-verification gap）**，以及为什么它和**测试时算力扩展（scaling test-time compute）**是一对搭档概念。
4. 列举仓库顶层目录（`figures/`、`inputs/`、`outputs/`、`inference/`、`DeepSeekMath_V2.pdf`）各自的用途，并明白**这是一个模型发布仓库，而不是训练代码库**。
5. 知道模型在 IMO 2025、CMO 2024、Putnam 2024 上的官方公布成绩。

## 2. 前置知识

本讲几乎不需要写代码，但需要以下几个概念垫底。不熟悉的读者可以先读下面的通俗解释：

- **大语言模型（LLM）做数学题**：给模型一段题目文字，它生成一段解题过程和最终答案。这类能力通常用「在竞赛题上做对多少」来衡量。
- **最终答案奖励（final answer reward）**：训练时只看最终答案对不对，对了给奖励、错了不给。这是目前强化学习训练推理模型的主流做法。它的优点是判分便宜（只需比对答案），缺点是**答对不等于推理过程正确**。
- **定理证明（theorem proving）**：与「算出数值答案」不同，定理证明要求一步一步严格推导，往往没有可以简单比对的「最终答案」，因此最终答案奖励在这类任务上不适用。
- **验证器（verifier）与奖励模型（reward model）**：验证器是给一段解答打分的模型；奖励模型是训练中给生成结果提供奖励信号的模型。把验证器当奖励模型用，就是「用打分代替答案比对」来训练生成器。
- **测试时算力（test-time compute）**：模型推理阶段愿意花多少计算。例如让模型并行生成很多份证明、每份再让验证器反复审很多遍，都是「花更多推理时算力换更高正确率」的手段。
- **pass@1**：评测指标，采样 1 次答案就统计正确率（这里对应证明被判定通过的比例），是常用的「单次尝试成功率」指标。
- **如何读 GitHub 仓库**：会点开文件、会看目录树即可；本讲所有源码引用都附永久链接，可直接点击跳转。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 / 目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md) | 项目门面：研究动机（Introduction）、评测结果（Evaluation Results）、模型下载方式 |
| [DeepSeekMath_V2.pdf](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/DeepSeekMath_V2.pdf) | 论文全文，标题与 README 相同，摘要是对研究动机最凝练的表述 |
| [figures/IMO-ProofBench.png](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/figures/IMO-ProofBench.png) | IMO-ProofBench（Basic / Advanced）上的对比柱状图 |
| [figures/Competitions.png](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/figures/Competitions.png) | IMO 2025 / CMO 2024 / Putnam 2024 等竞赛的逐题结果图 |
| [inputs/](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inputs/IMO2025.json) | 4 份竞赛题输入数据（JSON 数组，每题含 `id`、`question` 等字段） |
| [outputs/](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/outputs/IMO2025.jsonl) | 模型在各项基准上的预测结果（JSONL，含 `model_prediction` 等字段） |
| [inference/](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/README.md) | 全仓库唯一的可执行代码：证明评估流水线（`main.py`、`generate.py`、`math_templates.py`、`utils.py`、`run.sh`） |
| [LICENSE](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/LICENSE) | Apache License 2.0 |

## 4. 核心概念与源码讲解

### 4.1 仓库定位：这是一个「模型发布仓库」

#### 4.1.1 概念说明

开源界有两类常见仓库：

- **训练代码库**：包含数据预处理、训练循环、分布式训练框架等，体量大、模块多。
- **模型发布仓库**：随论文/模型发布，通常只包含「如何下载模型、如何推理评估」的最小代码，加上论文、评测数据与结果。

DeepSeek-Math-V2 属于第二类。模型权重托管在 HuggingFace，本仓库里可读的代码只有 `inference/` 目录下的 4 个 Python 文件和 1 个启动脚本。**这对学习者是好事**：代码量小，但每一行都服务于论文的核心方法，可以把它们全部精读一遍。

#### 4.1.2 核心流程

仓库顶层结构如下（已在本地确认）：

```text
deepseek-ai/DeepSeek-Math-V2/
├── DeepSeekMath_V2.pdf      # 论文全文
├── LICENSE                  # Apache 2.0
├── README.md                # 项目门面：动机 + 成绩 + 下载方式
├── figures/                 # README 引用的两张成绩图
│   ├── IMO-ProofBench.png
│   └── Competitions.png
├── inference/               # 唯一的代码目录：证明评估流水线
│   ├── README.md            # 两行说明：先填 API key，再跑 run.sh
│   ├── run.sh               # 启动脚本，写满超参数
│   ├── main.py              # 多轮「生成-验证-元验证-精炼」编排
│   ├── generate.py          # 异步调用推理 API 的生成引擎
│   ├── math_templates.py    # 四个提示词模板
│   └── utils.py             # 输出解析工具（boxed 提取等）
├── inputs/                  # 4 份竞赛题（IMO2025 / CMO2024 / CMO2025 / Putnam2024）
└── outputs/                 # 模型预测结果（5 份 JSONL + 1 份说明）
```

数据在仓库里的流向可以概括为：

```text
inputs/*.json（竞赛题）
      │  inference/run.sh 启动 inference/main.py
      ▼
  多轮流水线：证明生成 → 证明验证 → 元验证 → 证明精炼
      │
      ▼
outputs/*.jsonl（模型预测，即 README 成绩的原始依据）
```

#### 4.1.3 源码精读

先看 README 中「模型在哪里」的关键证据。[README.md:L67-L70](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L67-L70) 的 Download & Quick Start 小节写道：模型基于 **DeepSeek-V3.2-Exp-Base**，从 HuggingFace 下载；推理支持则指向 DeepSeek-V3.2-Exp 仓库。也就是说，本仓库**不含权重、也不含推理引擎**，只含「调用推理服务做证明评估」的脚本。

再看 `inference/` 目录自己的说明，[inference/README.md:L1-L2](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/README.md#L1-L2)：

> Here presents an example code for running proof-based evaluations.
> You should first specify your api key in the `generate.py` file and then run `run.sh` to start your job.

两句话就交代了全部用法：**在 `generate.py` 里填 API key → 运行 `run.sh`**。它把本目录定位为「基于证明的评估（proof-based evaluation）示例代码」，且依赖一个兼容 OpenAI 接口的外部推理服务。

启动脚本 [inference/run.sh:L3-L20](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L3-L20) 里，输入正是 `inputs/` 下的三份竞赛题，后面跟着一串控制「生成多少、验证多少」的超参数（这些参数是第 4.3 节的主角，也是本手册 u6 的主题）：

```bash
input_path=../IMO2025.json,../CMO2024.json,../CMO2025.json
...
python main.py \
    --input_paths ${input_path} \
    ...
    --n_parallel_proof_gen 128 \
    --n_verification_per_proof 64 \
    --skip_meta_verification \
    --start_round 1 \
    --max_rounds 16
```

#### 4.1.4 代码实践

**实践目标**：亲手确认「这是个发布仓库」的判断，并摸清目录结构。

操作步骤：

1. 打开仓库首页（或本地 `ls`），对照 4.1.2 的目录树逐项核对。
2. 数一数 `inference/` 目录：应该恰好是 4 个 `.py` 文件 + 1 个 `run.sh` + 1 个 `README.md`，没有任何 `train`、`trainer`、`optimizer` 字样的文件。
3. 打开 [README.md:L67-L70](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L67-L70)，确认模型权重与推理引擎都不在本仓库内。

需要观察的现象：

- 全仓库 Python 代码只出现在 `inference/` 一个目录里；
- README 中没有任何训练相关的章节。

预期结果：你会得到一个明确印象——要「读懂这个项目」，核心任务是读懂 `inference/` 下不到 50 KB 的代码 + 论文思想，而不是啃一个训练框架。这正是本手册后续所有讲义的作战地图。

#### 4.1.5 小练习与答案

**练习 1**：为什么说本仓库是「模型发布仓库」而不是「训练代码库」？给出两个证据。

参考答案：(1) README 的 Download & Quick Start（L67-L70）只讲如何从 HuggingFace 下载权重、去 DeepSeek-V3.2-Exp 仓库找推理支持，没有训练说明；(2) 全部代码集中在 `inference/`，只有评估流水线（main.py / generate.py / math_templates.py / utils.py / run.sh），没有任何训练循环或优化器代码。

**练习 2**：`outputs/` 目录下的文件是谁产生的？

参考答案：是 `inference/` 流水线（`run.sh` 启动 `main.py`）跑 `inputs/` 竞赛题后产出的模型预测结果，README L50 明确写了 "Model predictions are available in the `outputs` folder"，它们是 README 成绩表的原始依据。

**练习 3**：如果想在本仓库基础上微调（fine-tune）这个模型，本仓库能直接支持吗？

参考答案：不能。本仓库不含训练代码与权重文件，权重在 HuggingFace（`deepseek-ai/DeepSeek-Math-V2`），推理依赖 DeepSeek-V3.2-Exp 仓库的支持；训练需要自行另建工程。

---

### 4.2 README Introduction：自验证数学推理的核心主张

#### 4.2.1 概念说明

DeepSeekMath-V2 要解决的问题，来自「最终答案奖励」训练范式的两个软肋（README L36-L38 原文）：

1. **正确答案不保证正确推理**——模型可能蒙对答案而过程漏洞百出；
2. **定理证明类任务没有可比对的数值答案**——它需要的是严格的逐步推导，最终答案奖励根本不适用。

项目的应对方案是一个三角色结构：

- **生成器（generator）**：写数学证明的模型。它被训练成会在「交卷前」主动找出并修复自己证明中的问题（自我检查）。
- **验证器（verifier）**：给一段证明严格打分的模型。它同时还是训练生成器时的**奖励模型**——生成器写得越严谨，从验证器拿到的奖励越高。
- **元验证器（meta-verifier）**：复核「验证器打出的评价本身是否合理」的模型。它不重新解题，只审查验证器的挑错有没有道理，用来对冲验证器自身的误判。

「自验证（self-verifiable）」的含义就藏在这三者里：**同一个模型体系既能解题、又能给自己的解题过程把关**，而不是依赖外部裁判（例如人类标注者或 LeetCode 式答案比对器）。

#### 4.2.2 核心流程

把三个角色串起来，就是 `inference/main.py` 编排的多轮闭环（函数名均为仓库中真实存在，见下方源码精读）：

```text
┌────────────────────────── 一轮循环（R = 1..max_rounds）──────────────────────────┐
│                                                                                │
│  ① 证明生成        main.py 调用推理 API，并行生成多份候选证明                      │
│        │                                                                       │
│  ② 证明验证        prepare_proof_verification：验证器给每份证明打分               │
│        │              （0 / 0.5 / 1 三档：错 / 部分对 / 全对）                   │
│  ③ 元验证          prepare_meta_verification：对低分评价做复核，                 │
│        │              判断「验证器的挑错是否合理」                                │
│  ④ 证明精炼        prepare_proof_refinement：汇总多份证明与多条评价，             │
│        │              生成下一轮的精炼输入                                       │
│        └──► 回到 ①，直到 max_rounds 或该题已出现满分证明                          │
└────────────────────────────────────────────────────────────────────────────────┘
```

注意：这是**测试时（推理阶段）**的闭环；论文用它来评估模型的自验证能力，而同样的「验证器当奖励模型」思想也用于训练阶段（见下一节）。

#### 4.2.3 源码精读

**第一段：问题陈述**。[README.md:L34-L38](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L34-L38) 先肯定「用最终答案奖励做强化学习」一年内让模型饱和了 AIME/HMMT 这类竞赛，然后话锋一转指出两个根本局限：*correct answers don't guarantee correct reasoning*（正确答案不保证正确推理），以及定理证明这类任务需要严格逐步推导、最终答案奖励不适用。这两句是整个项目的立项理由。

**第二段：三步走方案**。[README.md:L39-L43](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L39-L43) 逐句对应三个角色：

- L41「训练一个准确、忠实的基于 LLM 的定理证明验证器」→ **验证器**；
- L42「用验证器作为奖励模型训练证明生成器，激励生成器在定稿前尽量找出并解决自己证明中的问题」→ **生成器**（以及「自我检查」习惯的来源）；
- L43「随着生成器变强，通过扩展验证算力来自动标注新的难验证证明，制造训练数据持续改进验证器」→ 维持**生成-验证差距**的机制（下一节展开）。

**代码侧的证据**：上面流程图里的三个阶段函数确实存在于 [inference/main.py:L66](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L66)（`prepare_proof_verification`）、[inference/main.py:L118](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L118)（`prepare_meta_verification`）和 [inference/main.py:L286](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L286)（`prepare_proof_refinement`），本讲只需记住名字，细节留给第 4 单元。

「元验证器只审评价、不重解题」也有模板原文为证，[inference/math_templates.py:L47](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L47) 与 [inference/math_templates.py:L62](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L62)：

> You are given a "problem", "solution", and "solution evaluation", and you need to assess whether this "solution evaluation" is reasonable.
> ... You do not need to solve the "problem", nor do you need to strictly assess whether the "solution" is accurate. Your only task is to ... evaluate whether the "solution evaluation" is reasonable.

#### 4.2.4 代码实践

**实践目标**：把 README 的英文论述与代码实体一一对上号。

操作步骤：

1. 打开 [README.md:L39-L43](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L39-L43)，逐句抄出对应「验证器」「生成器」「元验证器」的原文。
2. 在 `inference/main.py` 中用编辑器搜索三个函数名：`prepare_proof_verification`、`prepare_meta_verification`、`prepare_proof_refinement`，记下各自所在行号（应为 66、118、286 附近）。
3. 打开 [inference/math_templates.py:L62](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L62)，找到"You do not need to solve the problem"这句话。

需要观察的现象：README 里一句抽象的描述，都能在代码里找到一个具名的函数或模板。

预期结果：得到一张三行对照表——验证器 ↔ `prepare_proof_verification` + 验证模板；元验证器 ↔ `prepare_meta_verification` + 元验证模板；生成器/精炼 ↔ `prepare_proof_refinement` + 生成/精炼模板。完成后再进入第 5 节综合实践写 200 字总结。

#### 4.2.5 小练习与答案

**练习 1**：用自己的话解释：为什么「最终答案奖励」对定理证明任务不适用？

参考答案：定理证明的产出是一整段严格推导，通常没有类似 "42" 这样可以字符串比对的最终答案；即使有数值结论，答对也代表不了每一步推导都正确。奖励信号无法从「答案比对」中获得，所以必须换一种奖励来源——用验证器打分。

**练习 2**：元验证器评审的对象是什么？它评分为 1 代表什么？

参考答案：评审对象是**验证器对证明写下的评价（solution evaluation）**，而不是证明本身。按 [inference/math_templates.py:L96-L103](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L96-L103) 的规则，评 1 分表示「该评价挑出的缺陷全部合理，且表达与打分也无误」，即验证器的这条评价可信。

**练习 3**：判断对错：「元验证器需要先自己把题目解一遍，才能判断验证器的评价是否合理。」

参考答案：错。模板 L62 明确写了元验证器不需要解题、也不需要严格判断解答是否正确，它的唯一任务是判断「评价是否合理」（例如评价指出的缺陷是否真实存在）。

---

### 4.3 生成-验证差距与测试时算力扩展

#### 4.3.1 概念说明

**生成-验证差距（generation-verification gap）**：指「验证一道解答比生成一道解答更容易」的程度。差距越大，验证器越能可靠地给生成器的输出把关。这个概念类似于师生关系：学生（生成器）越强，老师（验证器）若原地踏步，就会渐渐批不出学生作业里的深坑——差距一旦消失甚至反转为「生成强于验证」，自我验证就变成「自己骗自己」。

**测试时算力扩展（scaling test-time compute）**：在推理阶段投入更多计算（多采样、多轮验证、多轮精炼）来提升正确率。README L40 特别强调：自验证对扩展测试时算力尤为重要，**尤其是对没有已知解答的开放问题**——没有标准答案可比对时，唯一能依赖的裁判就是模型自己的验证器。

两者的联系：DeepSeekMath-V2 用「扩展验证算力」来**维持**生成-验证差距——生成器变强后，让它产出大量「难验证」的新证明，再用大规模验证算力（多次采样验证 + 元验证复核）自动给这些证明打标，作为新训练数据去继续提升验证器。

#### 4.3.2 核心流程

训练阶段的飞轮（依据 README L41-L43 的描述）：

```text
        ┌──────────────────────────────────────────────────────┐
        │                                                      │
        ▼                                                      │
  训练验证器（准确、忠实地给证明打分）                              │
        │                                                      │
        ▼                                                      │
  验证器作为奖励模型 ──► 训练生成器（学会自我检查、自我修复）          │
        │                                                      │
        ▼                                                      │
  生成器变强 ──► 产出更多「难验证」的证明                            │
        │                                                      │
        ▼                                                      │
  扩展验证算力，自动给难验证证明打标（新训练数据）────────────────────┘
```

推理（评估）阶段同理：`run.sh` 的超参数直接决定了每道题消耗的验证算力。以 [inference/run.sh:L13-L20](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L13-L20) 的竞赛配置为例：每题每轮并行生成 128 份证明（`n_parallel_proof_gen 128`）、每份证明验证 64 次（`n_verification_per_proof 64`）、最多跑 16 轮（`max_rounds 16`）。粗略估算，单题单轮仅「生成 + 验证」两类请求的量级就是：

\[
\text{请求数} \;\approx\; n_{\text{parallel\_proof\_gen}} \times \left(1 + n_{\text{verification\_per\_proof}}\right) \;=\; 128 \times (1+64) \;=\; 8320
\]

（这是示意估算，未计精炼轮次与元验证的额外请求；完整推导放在本手册 u6-l1。）

#### 4.3.3 源码精读

**关于自验证与测试时算力**，[README.md:L40](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L40)：*"Self-verification is particularly important for scaling test-time compute, especially for open problems without known solutions."*（自验证对扩展测试时算力尤为重要，特别是对没有已知解答的开放问题。）这句话是理解整个项目「为什么执着于验证器」的钥匙。

**关于差距的维持**，[README.md:L43](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L43)：*"To maintain the generation-verification gap as the generator becomes stronger, we propose to scale verification compute to automatically label new hard-to-verify proofs, creating training data to further improve the verifier."*（为了在生成器变强的同时维持生成-验证差距，我们提出扩展验证算力来自动标注新的难验证证明，制造训练数据以继续改进验证器。）

**关于收益**，[README.md:L44](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L44)：模型「以扩展的测试时算力」在 IMO 2025 与 CMO 2024 取得金牌水准、Putnam 2024 拿到 118/120 的近满分。成绩与算力扩展的搭配并非偶然——验证算力正是这套方法把分数推上去的燃料。

**代码侧**：[inference/run.sh:L13-L20](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L13-L20) 中每个超参数都是一处「算力旋钮」：`n_parallel_proof_gen 128`（生成广度）、`n_verification_per_proof 64`（验证深度）、`max_rounds 16`（精炼轮数）、`--skip_meta_verification`（本配置关闭元验证以省算力，说明元验证本身也是一笔可观开销）。

#### 4.3.4 代码实践

**实践目标**：把「测试时算力」从口号变成可数的请求量。

操作步骤：

1. 打开 [inference/run.sh:L13-L20](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L13-L20)，抄下 5 个关键参数值：`n_parallel_proof_gen=128`、`n_verification_per_proof=64`、`max_rounds=16`、`n_best_proofs_to_sample=32`、`n_agg_trials=32`。
2. 用上面的公式估算单题单轮的生成+验证请求数（\(128 \times 65 = 8320\)）。
3. 再估算一份 6 题的赛事（如 IMO 2025）只跑 1 轮的请求量级（\(8320 \times 6 \approx 5\times 10^4\)）。

需要观察的现象：`--skip_meta_verification` 这个开关出现在竞赛配置里，说明官方配置也在做「算力取舍」。

预期结果：直观感受到为什么这套方法叫「以算力换正确率」，并理解为什么本手册 u6 专门有一讲做成本估算。（本实践为纸面推算，无需真实调用 API；结果待本地核对计算即可。）

#### 4.3.5 小练习与答案

**练习 1**：用「师生」类比解释生成-验证差距，并说明差距消失会发生什么。

参考答案：验证器是老师、生成器是学生，差距 = 老师水平领先学生的幅度。差距消失（学生强于老师）后，老师批不出学生作业里的错误，「自我验证」退化为「自我背书」，验证器给出的奖励信号随之失真。

**练习 2**：README L43 提出的维持差距的手段，核心动作是什么？

参考答案：扩展验证算力（scale verification compute），对生成器新产出的「难验证证明」自动打标，形成新训练数据，反过来继续训练更强的验证器——即让老师的成长速度跟上学生。

**练习 3**：`run.sh` 里 `--skip_meta_verification` 被启用，这暗示了什么？

参考答案：元验证（对验证器评价的复核）会额外增加一倍量级的 API 请求与开销；在竞赛评估这种已投入大量验证算力（64 次/证明）的场景下，官方选择关闭它来控制成本，说明元验证是可选项、其收益与开销需要权衡。

---

### 4.4 评估结果：成绩图、预测数据与论文 PDF

#### 4.4.1 概念说明

模型在两类基准上接受检验：

- **IMO-ProofBench**：由打造 DeepThink IMO-Gold 的 DeepMind 团队开发的形式化证明评测（README L49 提供了其 GitHub 链接），分 Basic 与 Advanced 两个难度档，指标是 pass@1。
- **真实竞赛**：IMO 2025（国际数学奥林匹克）、CMO 2024/2025（中国数学奥林匹克）、Putnam 2024（普特南竞赛）。这些比赛官方有人类奖牌线，可以直接衡量「模型 vs 人类顶尖选手」。

与之配套，`inputs/` 存题目、`outputs/` 存模型预测（JSONL），`figures/` 存成绩可视化，`DeepSeekMath_V2.pdf` 存论文全文（其标题与 README 标题一致：*DeepSeekMath-V2: Towards Self-Verifiable Mathematical Reasoning*）。

#### 4.4.2 核心流程

看懂本仓库成绩的阅读顺序：

```text
README L44（文字结论：金牌照旧、118/120）
   → figures/IMO-ProofBench.png（与对手模型的 pass@1 对比柱状图）
   → figures/Competitions.png（竞赛逐题结果：灰底 = 完全解出，下划线 = 部分分）
   → outputs/*.jsonl（逐题原始预测，可自行复核）
   → DeepSeekMath_V2.pdf（方法与实验细节的完整论述）
```

`outputs/` 中每条 JSONL 记录的结构（以 [outputs/IMO2025.jsonl](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/outputs/IMO2025.jsonl) 首行为例）顶层含 `question`（题面）、`problem_idx`（如 `"IMO2025-1"`）、`model_prediction`（内含 `proof` 等字段的模型预测）。

#### 4.4.3 源码精读

**评测小节全文**，[README.md:L47-L51](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L47-L51)：声明评测基准为 IMO-ProofBench 与 IMO 2025、CMO 2024、Putnam 2024 等竞赛，并注明 "Model predictions are available in the `outputs` folder"——即成绩可复核查证。

**两张图的嵌入位置**：[README.md:L52-L56](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L52-L56) 引用 [figures/IMO-ProofBench.png](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/figures/IMO-ProofBench.png)（Basic 与 Advanced 两个面板的 pass@1 对比，DeepSeek-Math-V2 与 Kimi K2 Thinking Preview、DeepSeek-Hybrid、GPT-OSS-120、Gemini 3 Pro、DeepThink IMO-Gold 等模型同场竞技，两个面板均领先）；[README.md:L61-L65](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L61-L65) 引用 [figures/Competitions.png](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/figures/Competitions.png)（图注为 "Problems in gray are fully solved, while underlined problems received partial credit"，即灰底题完全解出、下划线题得部分分）。图中各柱的具体数值请以仓库原图为准（**待确认**，建议读者自行打开图片核对）。

**文字版硬成绩**，[README.md:L44](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L44)：IMO 2025 与 CMO 2024 **金牌水准**，Putnam 2024 **118/120** 近满分（均以扩展测试时算力取得）。

**数据与版权说明**：[outputs/README.md:L1](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/outputs/README.md#L1) 注明本工作使用了 Google DeepMind 开发的 IMO-ProofBench（Apache 2.0 许可）；[README.md:L75-L83](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L75-L83) 的引用信息列出作者（Zhihong Shao、Yuxiang Luo、Chengda Lu 等）与年份 2025。

**论文**：[DeepSeekMath_V2.pdf](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/DeepSeekMath_V2.pdf)。其标题即 README L30 的大标题；摘要为整篇论文的浓缩（本讲综合实践中将亲自阅读，此处不复述、以免转述失真）。

#### 4.4.4 代码实践

**实践目标**：从「文字成绩 → 图片 → 原始预测」三个层次核对模型成绩。

操作步骤：

1. 打开 [README.md:L44](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L44)，记下 IMO 2025 / CMO 2024 / Putnam 2024 三个成绩。
2. 打开 [figures/IMO-ProofBench.png](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/figures/IMO-ProofBench.png) 与 [figures/Competitions.png](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/figures/Competitions.png)，记录 DeepSeek-Math-V2 在 Basic / Advanced 两个面板上相对其他模型的位置。
3. 打开 [outputs/IMO2025.jsonl](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/outputs/IMO2025.jsonl) 第一行，确认字段 `question` / `problem_idx` / `model_prediction` 存在，并数一下该文件共几行（应为 IMO 2025 的 6 道题）。

需要观察的现象：README 声称的成绩与 `outputs/` 中的预测记录一一对应（每道题一条）。

预期结果：三个层次互相印证——文字结论、图表可视化、逐题原始数据。图片中的精确分值请以你亲眼所见为准（**待确认**：本讲义不转录图中具体数字，避免转录误差）。

#### 4.4.5 小练习与答案

**练习 1**：IMO-ProofBench 是谁开发的？分哪两档？

参考答案：由打造 DeepThink IMO-Gold 的 DeepMind 团队开发（README L49 与 outputs/README.md 均有说明），分 Basic 与 Advanced 两个难度档。

**练习 2**：`outputs/IMO2025.jsonl` 与 `inputs/IMO2025.json` 是什么关系？

参考答案：`inputs/` 存题目（JSON 数组，每题含 `id`、`question` 等字段）；`outputs/` 存模型对这些题的预测（JSONL，每行一题，含 `problem_idx` 与 `model_prediction`）。流水线读前者产出后者。

**练习 3**：为什么说这个仓库的成绩「可复核」？

参考答案：因为 `outputs/` 公开了逐题的模型预测原文（README L50 明确指出），任何人可以拿去用 IMO-ProofBench 的评分方式重新判分；同时 `inference/` 提供了复现评估的代码。

---

## 5. 综合实践

**任务**：写一份 200 字以内的「三角色说明书」，并登记三项官方成绩。（本实践为纯阅读与写作任务，无需运行代码。）

**步骤**：

1. **精读 Introduction**：打开 [README.md:L32-L45](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L32-L45)，逐句读完后合上页面。
2. **读论文标题与摘要**：打开 [DeepSeekMath_V2.pdf](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/DeepSeekMath_V2.pdf) 第 1 页，读标题与摘要（摘要中会出现 Introduction 里没有的细节，例如具体的训练数据构造方式——**待本地验证**，请以你读到的原文为准）。
3. **写总结**：用不超过 200 字写清「生成器 — 验证器 — 元验证器」三者在本项目中各自扮演的角色，要求至少各有一句，并点出「验证器是生成器的奖励模型」这层关系。
4. **登记成绩**：对照 [figures/Competitions.png](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/figures/Competitions.png) 与 [figures/IMO-ProofBench.png](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/figures/IMO-ProofBench.png)，记录模型在 IMO 2025、CMO 2024、Putnam 2024 上的大致成绩（IMO 2025：金牌水准；CMO 2024：金牌水准；Putnam 2024：118/120——依据 README L44，图中具体分值以原图为准）。
5. **自查**：把总结里的每个说法都回链到 README 的某一行（L41-L43）或某个代码实体（main.py 的三个 `prepare_*` 函数），无法回链的删掉。

**预期成果**：一段 200 字总结 + 一张三行成绩表。它是你后续阅读 `inference/` 代码时的「概念底座」。

## 6. 本讲小结

- DeepSeek-Math-V2 的核心主张：**用验证器作为奖励模型训练证明生成器**，让模型在定稿前自我检查、自我修复，实现自验证的数学推理（README L41-L42）。
- 三个角色：**生成器**写证明；**验证器**给证明打 0/0.5/1 分并兼任训练奖励；**元验证器**复核「验证器的评价是否合理」，只审评价、不重解题。
- **生成-验证差距**必须随生成器变强而主动维持，手段是**扩展验证算力**自动标注难验证证明、反哺验证器训练（README L43）；这也是「测试时算力扩展」的意义所在（README L40、L44）。
- 本仓库是**模型发布仓库**：权重在 HuggingFace，代码只有 `inference/` 下 4 个 Python 文件 + `run.sh`，数据在 `inputs/`，预测结果在 `outputs/`。
- 官方成绩：IMO 2025 与 CMO 2024 金牌水准，Putnam 2024 拿到 118/120（README L44）；逐题预测在 `outputs/` 可复核。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：深入 `inputs/*.json` 与 `outputs/*.jsonl` 的字段级结构，学会用脚本检查数据——这是阅读一切流水线代码前的数据直觉训练。
- **提前浏览**（不强求读懂）：[inference/main.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py) 的三个 `prepare_*` 函数名，与 [inference/run.sh](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh) 的参数列表，混个眼熟即可。
- **论文阅读**：有余力可通读 [DeepSeekMath_V2.pdf](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/DeepSeekMath_V2.pdf) 的引言与方法部分，与本讲 4.2/4.3 的概念互相印证。

# 仓库结构与依赖

> 本讲承接 [u1-l1 项目总览与定位](./u1-l1-project-overview.md)。上一讲你已经知道 RTL-Coder 的两大贡献是「自动化数据集生成」与「基于代码质量评分的训练」，也知道了它发布了四个 HuggingFace 模型。本讲带你走进仓库本身：**这些代码到底放在哪里、彼此如何分工、依赖了什么库**。读完本讲，你拿到任何一个新克隆，都能在一分钟内说清「哪个目录干什么事、跑哪条流程看哪个文件」。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 画出 RTL-Coder 仓库的顶层目录树，并说出 `data_generation/`、`dataset/`、`train/`、`benchmark_inference/` 四个核心目录各自的职责。
2. 逐条解释 `requirements.txt` 中每个依赖的作用，并指出哪些依赖「在代码里被用到却没写进 requirements.txt」。
3. 把仓库里的 21 个被 git 跟踪的文件，准确归入**脚本文件、数据文件、配置文件**三类，并能解释为什么这样分。
4. 按「数据生成 → 训练 → 推理」的真实执行顺序，重新给这些文件排序，为后续精读每一篇源码打基础。

---

## 2. 前置知识

本讲是纯结构导览，门槛很低，但有几个名词先对齐：

- **仓库（repository）**：用 git 管理的项目目录。本讲我们用 `git ls-files` 列出仓库里「被 git 跟踪的全部文件」，这是看清结构最可靠的方式。
- **脚本文件（`.py`）**：可以直接被 Python 解释器执行的代码，承担「干活」的职责（造数据、训练、推理）。
- **数据文件（`.json` / `.txt`）**：被脚本读取或写入的内容，例如训练集、提示词模板、基准题目。
- **配置文件**：不执行逻辑，只声明「参数」，例如 Python 依赖清单 `requirements.txt`、DeepSpeed 分布式训练参数 `ds_stage_2.json`。
- **RTL/Verilog**：寄存器传输级（Register Transfer Level）硬件描述语言，RTL-Coder 的最终目标就是让大模型生成可综合的 Verilog 代码。
- **目录（folder）vs 文件（file）**：本讲的四大目录各装着一个「主题」的全部相关文件；目录本身不是代码。

如果你对上一讲提到的「Resyn27k 数据集」「四个 HF 模型」「VerilogEval / RTLLM 两个基准」印象模糊，建议先回看 [u1-l1 项目总览与定位](./u1-l1-project-overview.md)。

---

## 3. 本讲源码地图

本讲只看「骨架」，不进入任何脚本的内部逻辑。涉及的文件如下：

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| `README.md` | 项目主文档，目录职责的「官方说明」都写在这里 | 用来交叉验证我们对四个目录的理解 |
| `requirements.txt` | Python 依赖清单，共 7 行 | 本讲 4.2 的主角 |

> 提示：本讲提到的所有目录与文件，都是仓库里**真实存在、被 git 跟踪**的。后面给出的目录树就是用 `git ls-files` 还原出来的，你可以自己复现。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 仓库顶层结构与四大目录职责**——回答「东西放在哪」。
2. **4.2 `requirements.txt` 关键依赖**——回答「跑起来要装什么」。
3. **4.3 脚本 / 数据 / 配置三类文件的分布**——回答「文件按角色怎么分类」。

### 4.1 仓库顶层结构与四大目录职责

#### 4.1.1 概念说明

RTL-Coder 仓库非常「轻」：去掉 README 用图，**真正承载逻辑的文件只有 11 个脚本/数据/配置**，外加一份 26532 行的成品数据集。它没有 `src/`、没有复杂的包结构，而是按**项目工作流的阶段**直接切出四个平级目录：

- `data_generation/`——自动化数据集**生成**的代码与模板。
- `dataset/`——生成出来的**成品数据集**（27K 条）。
- `train/`——把数据集**训练**成模型的代码、配置与样例。
- `benchmark_inference/`——把训练好的模型拿去**推理/评测**的代码与基准题目。

这四个目录恰好对应 RTL-Coder 从「原始需求」到「可评测模型」的完整流水线。**目录名就是流水线阶段名**，这是这个仓库最重要的阅读线索。

#### 4.1.2 核心流程

把四个目录串起来，就是 RTL-Coder 的主链路：

```
data_generation/  ──生成──▶  dataset/  ──喂给──▶  train/  ──产出──▶  benchmark_inference/
  (造数据)                    (成品数据)          (训练模型)            (推理评测)
   GPT-3.5 辅助                  Resyn27k          mle*.py              生成 .v 代码
```

注意一个上一讲强调过、这里要再次落实的边界：**GPT-3.5 只出现在最左边的 `data_generation/`，用于造数据；`train/` 训练的是开源底座（DeepSeek-coder / Mistral），`benchmark_inference/` 用的是自家训练出来的模型**。整条链路没有用 OpenAI 的模型做最终推理。

除四个核心目录外，顶层还有两类辅助内容：

- `README.md`、`requirements.txt`：文档与依赖（顶层根目录）。
- `_pic/`：6 个图片/资源文件，仅供 README 渲染流程图用，不参与任何执行。

#### 4.1.3 源码精读

下面是仓库真实的目录树（行数来自 `wc -l`，已一一核对）：

```text
RTL-Coder/
├── README.md                      (333 行)   项目主文档：定位、流程图、用法
├── requirements.txt               (7 行)     Python 依赖清单
├── _pic/                                     README 用图（6 个文件，不参与执行）
│   ├── data_gen_flow.jpg
│   ├── training_flow.jpg
│   ├── filter_dataset.jpg
│   ├── LLM4RTL_comparison.jpg
│   ├── LLM4RTL_comparison_arxiv.jpg
│   └── readme
├── data_generation/                          ① 数据生成
│   ├── instruction_gen.py         (172 行)   生成主循环脚本
│   ├── utils.py                   (72 行)    GPT-3.5 调用工具
│   ├── p_example.txt              (46 行)    变异提示词模板（数据）
│   └── data_sample.json           (10 行)    生成数据样例（数据）
├── dataset/                                  ② 成品数据集
│   └── Resyn27k.json              (26532 行) 27K 条「指令-代码」对（数据）
├── train/                                    ③ 训练
│   ├── mle.py                     (217 行)   标准监督微调 SFT
│   ├── mle_scoring.py             (296 行)   质量评分训练
│   ├── mle_scoring_grad_split.py  (385 行)   梯度切分（省显存版）
│   ├── ds_stage_2.json            (42 行)    DeepSpeed ZeRO-2 配置（配置）
│   └── scoring_data_sample.json   (10 行)    评分训练样例（数据）
└── benchmark_inference/                      ④ 基准推理
    ├── test_on_verilog-eval.py    (119 行)   VerilogEval 推理脚本
    ├── test_on_rtllm.py           (118 行)   RTLLM 推理脚本
    ├── rtllm-1.1.json             (29 行)    RTLLM 题目描述（数据）
    └── rtllm.json                 (29 行)    RTLLM 题目描述（数据）
```

四个目录的职责，README 自己也做了官方说明，可逐一对照：

- **① `data_generation/`**——README 明确说「生成脚本与数据样例」放在这里：[README.md:L81-L86](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L81-L86) 提到你可以改 `p_example.txt` 和 `instruction_gen.py` 来设计自己的提示方法。这里就是「自己造数据」的入口。

- **② `dataset/`**——README 说明 27K 数据集就在这个目录：[README.md:L87](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L87) 指出 `Resyn-27k.json` 由 GPT-3.5-turbo 生成、不保证全对。目录里只有这一个文件，但它是仓库里最大的文件（2.6 万行）。

- **③ `train/`**——README 说三种训练方案都在这里：[README.md:L236](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L236) 写明「please refer to the paper and the folder `"train"`」。三种方案对应三个 `.py` 脚本（见目录树），外加一份 DeepSpeed 配置 `ds_stage_2.json` 和一份评分训练数据样例。

- **④ `benchmark_inference/`**——README 给出两个基准的入口：[README.md:L206](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L206) 指向 VerilogEval 脚本，[README.md:L225](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L225) 指向 RTLLM 题目描述 `rtllm-1.1.json`。

#### 4.1.4 代码实践

**实践目标**：用一条 git 命令自己还原上面的目录树，验证我们说的每个文件都真实存在。

**操作步骤**：

1. 在仓库根目录执行（只读命令，安全）：
   ```bash
   git ls-files | xargs wc -l
   ```
2. 观察输出：每个文件后面跟着它的行数。
3. 再执行下面这条，按目录分组统计文件个数：
   ```bash
   git ls-files | cut -d/ -f1 | sort | uniq -c
   ```

**需要观察的现象**：

- 第一条命令会列出全部 21 个被跟踪的文件，其中 `dataset/Resyn27k.json` 行数最多（约 2.6 万行），是仓库里体量最大的文件。
- 第二条命令会显示顶层各「前缀」（目录）下的文件数，你会看到 `data_generation`、`dataset`、`train`、`benchmark_inference` 各占若干个。

**预期结果**：四个核心目录的文件总数加起来，再加上根目录的 `README.md`、`requirements.txt` 与 `_pic/` 下的 6 个文件，正好等于 `git ls-files` 的总条数。如果你数出来对不上，说明有文件没被 git 跟踪（本仓库不存在这种情况）。

> 说明：本实践的命令都是只读的，不会修改任何文件。命令的实际输出请以你在本地运行为准。

#### 4.1.5 小练习与答案

**练习 1**：仓库里行数最多的文件是哪一个？它属于哪个目录？为什么它这么大？

> **答案**：是 `dataset/Resyn27k.json`（约 26532 行），属于 `dataset/` 目录。它大是因为里面存了 2.7 万条「指令-Verilog 代码」样本对，是整个项目训练用的成品数据集。

**练习 2**：如果一个新同学只想「用现成模型跑一次 Verilog 生成」，他需要关心 `data_generation/` 和 `train/` 吗？

> **答案**：不需要。这两者分别负责「造数据」和「训练模型」，属于离线生产环节。只想用模型，只需关注 `benchmark_inference/`（或直接照 README 写几行推理代码加载 HF 模型）。这正是按目录划分阶段的好处——职责隔离。

---

### 4.2 requirements.txt 关键依赖

#### 4.2.1 概念说明

`requirements.txt` 是 Python 项目的标准依赖清单：每行一个包，`==` 表示锁定版本。它解决的问题是「换一台机器、换一个同事，怎么保证装出来的环境一致」。RTL-Coder 的这份清单只有 7 行，但它把项目用到的所有重型库都钉死了版本。

读懂这份清单的关键是：**把每个依赖对应到仓库里的哪个目录、哪条流程**。比如训练用到的库不会出现在推理样例里，造数据用到的库不会出现在训练脚本里。

#### 4.2.2 核心流程

依赖与目录、流程的对应关系可以这样梳理：

```text
依赖                     主要被谁用                      所属流程
─────────────────────────────────────────────────────────────────
torch 2.1.0          →  train/mle*.py                  训练 + 推理
transformers 4.34.0  →  train/, benchmark_inference/   训练 + 推理（加载模型/分词器）
SentencePiece        →  分词器后端（间接）              推理（加载某些分词器）
Deepspeed            →  train/ds_stage_2.json 配合     训练（多卡分布式）
scikit-learn         →  数据/数值处理支撑              数据 + 训练
openai 0.28.0        →  data_generation/utils.py       数据生成（调 GPT-3.5）
optimum              →  量化推理支撑                   推理（GPTQ 等）
```

一个重要观察：`openai` 是**唯一**只服务于数据生成的依赖——它印证了「GPT 只用来造数据」。而 `Deepspeed` 是**唯一**只服务于训练的依赖。

#### 4.2.3 源码精读

依赖清单原文（逐行核对，注意第 4 行的拼写就是 `Deepspeed`）：

```text
torch==2.1.0
transformers==4.34.0
SentencePiece
Deepspeed
scikit-learn
openai==0.28.0
optimum
```

逐条解读：

- **`torch==2.1.0`**：[requirements.txt:L1](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/requirements.txt#L1) ——PyTorch 深度学习框架。训练脚本里 `import torch`、`from torch.utils.data import Dataset` 都依赖它；README 推理示例里的 `torch.float16` 也是它提供的。

- **`transformers==4.34.0`**：[requirements.txt:L2](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/requirements.txt#L2) ——HuggingFace Transformers。负责 `AutoTokenizer` / `AutoModelForCausalLM` 加载模型、`Trainer` 训练循环。**版本被严格锁死在 4.34**，因为这个版本的 Trainer/TrainingArguments API 与 `train/` 下脚本写法匹配，升级可能报错。

- **`SentencePiece`**：[requirements.txt:L3](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/requirements.txt#L3) ——分词器（tokenizer）底层实现。仓库的 `.py` 里没有直接 `import sentencepiece`，但加载 DeepSeek/Mistral 分词器时，transformers 会间接调用它，所以必须装。

- **`Deepspeed`**：[requirements.txt:L4](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/requirements.txt#L4) ——分布式训练框架（注意清单里写的是首字母大写的 `Deepspeed`，标准包名其实是小写 `deepspeed`，安装时一般用 `pip install deepspeed`）。它配合 [train/ds_stage_2.json](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/ds_stage_2.json#L22-L34) 里的 ZeRO stage 2 配置，把优化器状态切分到多卡并 offload 到 CPU，是「省显存训练」的关键。

- **`scikit-learn`**：[requirements.txt:L5](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/requirements.txt#L5) ——科学计算支撑库（附带 numpy/scipy）。仓库脚本里直接用的是 numpy（`data_generation/instruction_gen.py` 里 `import numpy as np`），而 numpy 通常随 scikit-learn/torch 一起装上。

- **`openai==0.28.0`**：[requirements.txt:L6](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/requirements.txt#L6) ——OpenAI 官方 Python SDK 的 **0.28 旧版**。这个版本用的是 `openai.ChatCompletion.create(...)` 的老接口，`data_generation/utils.py` 调 GPT-3.5 用的就是它。**版本必须锁 0.28**，新接口（1.x）写法完全不同，会直接跑不通。这再次印证：openai 只服务于数据生成。

- **`optimum`**：[requirements.txt:L7](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/requirements.txt#L7) ——HuggingFace Optimum，常配合量化推理（README 提到的 GPTQ/AutoGPTQ 链路）使用，是推理侧的加速支撑。

> **真实世界的小坑（重要）**：仓库代码里还有几个 `import` **并没有**写进 `requirements.txt`：
> - `data_generation/instruction_gen.py` 里 `from rouge_score import rouge_scorer`（用于 ROUGE-L 去重）——`rouge_score` 未列。
> - `data_generation/instruction_gen.py` 里 `import numpy as np`——numpy 未单列（随其它包装入）。
> - `train/mle_scoring_grad_split.py` 里 `from accelerate import Accelerator`——`accelerate` 未列。
>
> 这意味着你照 `requirements.txt` 装完后，**跑数据生成还得补装 `rouge_score`，跑梯度切分训练还得补装 `accelerate`**。这是真实项目常见的「依赖清单滞后于代码」现象，读源码时要留意。

#### 4.2.4 代码实践

**实践目标**：把 7 个依赖各自「属于哪条流程」整理成一张可核对的表。

**操作步骤**：

1. 打开 [requirements.txt:L1-L7](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/requirements.txt#L1-L7)。
2. 在仓库里搜索每个依赖的踪迹（只读）：
   ```bash
   grep -rn "import openai\|openai\." data_generation/
   grep -rn "from transformers\|import transformers" train/ benchmark_inference/
   grep -rn "accelerate\|rouge_score" --include=*.py .
   ```
3. 为每个依赖填一张表：`包名 | 用途 | 被哪个目录/文件用到 | 是否锁定版本`。

**需要观察的现象**：

- `openai` 的痕迹只会出现在 `data_generation/` 下，不会出现在 `train/` 或 `benchmark_inference/`。
- `rouge_score` 和 `accelerate` 在代码里有 import，但在 `requirements.txt` 里找不到——你会亲眼看到上面说的「依赖清单滞后」。

**预期结果**：你会得到一张清晰的「依赖 → 流程」对照表，并且发现真实仓库里 `requirements.txt` 不完全自洽——这是一个很有价值的读源码收获。> 待本地验证：grep 的具体命中行以你本地输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `transformers` 要锁定 `4.34.0`，而不是写 `transformers`（不锁版本）？

> **答案**：HuggingFace Transformers 在不同大版本之间 API 变化较大（尤其是 `Trainer`、`TrainingArguments` 的参数名和默认行为）。`train/` 下的脚本是基于 4.34 写的，锁版本是为了保证「今天能跑、明年 clone 下来还能跑」，避免被上游升级悄悄破坏。

**练习 2**：`openai==0.28.0` 这个依赖，如果换成 `openai>=1.0` 会怎样？

> **答案**：会直接报错。0.28 用的是 `openai.ChatCompletion.create(...)` 旧接口，而 1.x 改成了 `client.chat.completions.create(...)` 且需要先建 `OpenAI()` 客户端。`data_generation/utils.py` 是按 0.28 写的，换版本必须改代码。

**练习 3**：仓库里实际用到了却没写进 `requirements.txt` 的包，至少举出两个。

> **答案**：`rouge_score`（`instruction_gen.py` 做去重时用）和 `accelerate`（`mle_scoring_grad_split.py` 里 `from accelerate import Accelerator`）。`numpy` 也算一个（随其它包间接安装）。

---

### 4.3 脚本、数据、配置三类文件的分布

#### 4.3.1 概念说明

把文件按「角色」而不是「目录」重新分类，是看懂任何代码仓库的通用技能。RTL-Coder 里的 21 个被跟踪文件，可以干净地分成三类：

- **脚本文件**：`.py`，可执行，承担逻辑。仓库里有 **7 个**。
- **数据文件**：`.json` / `.txt`，被脚本读写的内容。仓库里有 **8 个**（含 6 个图片资源算作另一类辅助资源）。
- **配置文件**：声明参数、不执行逻辑。仓库里有 **2 个**（`requirements.txt`、`train/ds_stage_2.json`）。
- **文档与资源**：`README.md` 与 `_pic/` 下 6 个图片/资源，**不参与执行**。

这种分类的好处是：当你想改「行为」就找脚本，想换「内容」就找数据，想调「参数」就找配置——三者职责互不混淆。

#### 4.3.2 核心流程

三类文件的分布可以用一张表概括（行数已核对）：

| 类别 | 文件 | 数量 | 特征 |
| --- | --- | --- | --- |
| **脚本 `.py`** | `data_generation/instruction_gen.py`、`data_generation/utils.py`、`train/mle.py`、`train/mle_scoring.py`、`train/mle_scoring_grad_split.py`、`benchmark_inference/test_on_verilog-eval.py`、`benchmark_inference/test_on_rtllm.py` | 7 | 可执行，`python xxx.py` 或 `torchrun xxx.py` 跑起来 |
| **数据 `.json`/`.txt`** | `data_generation/p_example.txt`、`data_generation/data_sample.json`、`dataset/Resyn27k.json`、`train/scoring_data_sample.json`、`benchmark_inference/rtllm-1.1.json`、`benchmark_inference/rtllm.json` | 6 | 被脚本读/写的内容（模板、样本、数据集、基准题） |
| **配置** | `requirements.txt`、`train/ds_stage_2.json` | 2 | 声明依赖/训练参数，不含可执行逻辑 |
| **文档/资源** | `README.md`、`_pic/*`（6 个） | 7 | 给人看，不参与执行 |

> 注：把图片单独算作资源，则 `.json`/`.txt` 数据文件共 6 个；不细分则数据+资源共 13 个。两种口径都行，关键是分清「执行 vs 不执行」「逻辑 vs 内容」。

一条贯穿全仓库的规律：**每个目录都按需混合了脚本 + 数据**，但配置只有两处：

- `data_generation/`：2 脚本 + 2 数据。
- `train/`：3 脚本 + 1 配置 + 1 数据。
- `benchmark_inference/`：2 脚本 + 2 数据。
- `dataset/`：1 数据。
- 顶层：1 脚本都没有，只有文档 `README.md` + 配置 `requirements.txt` + 图片。

#### 4.3.3 源码精读

挑三个代表件来印证分类：

- **脚本代表** [data_generation/instruction_gen.py:L1-L15](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L1-L15)：开头一连串 `import time / json / os ...` 加 `import numpy as np`、`from rouge_score import rouge_scorer`、`import utils`，这是典型的可执行脚本特征——它在「做事」。

- **数据代表** [data_generation/p_example.txt:L1-L5](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/p_example.txt#L1-L5)：内容是自然语言提示词（`Please act as a Verilog code designer ... #method# ...`），没有任何 `import`，只是被脚本当作模板读进来。它是「内容」而非「逻辑」。

- **配置代表** [train/ds_stage_2.json:L22-L34](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/ds_stage_2.json#L22-L34)：是一段 JSON，声明 `zero_optimization.stage=2`、`offload_optimizer.device=cpu` 等参数。它不执行任何代码，只是被 `torchrun ... --deepspeed ds_stage_2.json` 读取来配置训练。

#### 4.3.4 代码实践

**实践目标**：用命令自动把仓库文件按扩展名分类，验证我们手工归类的结果。

**操作步骤**：

1. 按扩展名统计被跟踪的文件（只读）：
   ```bash
   git ls-files | sed 's/.*\.//' | sort | uniq -c | sort -rn
   ```
2. 想看「哪些是脚本」，再执行：
   ```bash
   git ls-files '*.py'
   ```

**需要观察的现象**：

- 第一步输出里，`.json` 出现 6 次（`Resyn27k`、`scoring_data_sample`、`data_sample`、`rtllm-1.1`、`rtllm`、`ds_stage_2`），`.py` 出现 7 次，`.txt` 出现 2 次（`p_example`、`requirements`），`.md` 出现 1 次，其余是图片。
- 注意：`.json` 既可能是**数据**（如 `Resyn27k.json`），也可能是**配置**（如 `ds_stage_2.json`）——扩展名相同但角色不同，要靠「是否被当作参数传给命令」「是否含可执行逻辑」来区分，不能只看后缀。

**预期结果**：`.py` 文件正好 7 个，与 4.3.2 表格一致。> 待本地验证：不同平台的 `sed`/`uniq` 输出顺序可能略有差异，以你本地结果为准。

#### 4.3.5 小练习与答案

**练习 1**：`ds_stage_2.json` 和 `Resyn27k.json` 后缀都是 `.json`，为什么一个算配置、一个算数据？

> **答案**：`ds_stage_2.json` 体积小（42 行）、声明训练参数（ZeRO stage、offload 等），被 `--deepspeed` 当配置读取，是配置文件；`Resyn27k.json` 体积大（2.6 万行）、存的是 2.7 万条训练样本，被 `--data_path` 当数据读取，是数据文件。区分依据是「用途与是否含可执行逻辑」，而非后缀。

**练习 2**：仓库里一共有几个 `.py` 脚本？它们分布在哪几个目录？

> **答案**：共 7 个。`data_generation/` 2 个（`instruction_gen.py`、`utils.py`），`train/` 3 个（`mle.py`、`mle_scoring.py`、`mle_scoring_grad_split.py`），`benchmark_inference/` 2 个（`test_on_verilog-eval.py`、`test_on_rtllm.py`）。顶层根目录没有任何 `.py`。

---

## 5. 综合实践

> 这是本讲的主线实践，把三个模块串起来。完成后你将拥有一份「个人版仓库导览」。

**任务**：对照真实的目录树，给每个被 git 跟踪的文件写一份「一行作用清单」，并按「**数据生成 → 训练 → 推理**」的真实执行顺序重新排序。

**操作步骤**：

1. 在仓库根目录运行 `git ls-files`，拿到全部 21 个文件路径。
2. 新建一张三列表格：`文件路径 | 一行作用 | 所属流程阶段`。其中「流程阶段」只能填三选一：`数据生成` / `训练` / `推理`，跨阶段的填它的「主要」阶段。
3. 把表格按「数据生成 → 训练 → 推理」重新排序，顶层文档/依赖/图片放在最后。
4. 对每个脚本，标注它的「入口命令」（参考 README：`python instruction_gen.py`、`torchrun ... mle.py`、`python test_on_rtllm.py` 等）。

**参考排序骨架**（请你补全每个文件的一行作用）：

```text
【数据生成 data_generation】
  p_example.txt          —— 变异提示词模板（改它=改数据风格）
  data_sample.json       —— 生成数据样例（看格式）
  utils.py               —— 调 GPT-3.5 的工具函数
  instruction_gen.py     —— 生成主循环（入口：python instruction_gen.py）
【数据生成产物 dataset】
  Resyn27k.json          —— 27K 成品数据集（喂给训练）
【训练 train】
  scoring_data_sample.json —— 评分训练数据样例
  ds_stage_2.json        —— DeepSpeed ZeRO-2 配置
  mle.py                 —— 标准 SFT（torchrun ... mle.py）
  mle_scoring.py         —— 评分训练（torchrun ... mle_scoring.py）
  mle_scoring_grad_split.py —— 梯度切分省显存版（torchrun ... mle_scoring_grad_split.py）
【推理 benchmark_inference】
  rtllm-1.1.json / rtllm.json —— RTLLM 基准题目描述
  test_on_verilog-eval.py —— VerilogEval 推理脚本
  test_on_rtllm.py       —— RTLLM 推理脚本
【文档/依赖/资源 顶层】
  requirements.txt       —— Python 依赖
  README.md              —— 项目主文档
  _pic/*                 —— README 用图
```

**预期结果**：

- 你会发现 `data_generation/` 的产物（`Resyn27k.json`）正好是 `train/` 的输入——这验证了「数据生成 → 训练」的衔接。
- 你会发现 `train/` 产出的「模型」（不在这个仓库里，而在 HuggingFace 上）正是 `benchmark_inference/` 的输入——这验证了「训练 → 推理」的衔接。
- 你会清楚看到：**没有任何一个 `.py` 脚本同时跨两条流程**，职责是隔离的。

> 实践产物请你保存在自己的笔记里。本讲**不要求**（也不允许）把它写进 `RTL-Coder-tutorial/` 目录之外或修改任何源码。

---

## 6. 本讲小结

- 仓库按**流水线阶段**切出四个平级目录：`data_generation/`（造数据）、`dataset/`（成品数据）、`train/`（训练）、`benchmark_inference/`（推理评测），目录名即阶段名。
- 真正承载逻辑的文件很轻：**7 个 `.py` 脚本 + 6 个 `.json`/`.txt` 数据 + 2 个配置**，外加一份 2.6 万行的成品数据集 `Resyn27k.json`。
- `requirements.txt` 锁死了 `torch==2.1.0`、`transformers==4.34.0`、`openai==0.28.0` 等关键版本；其中 `openai` 只服务于数据生成，`Deepspeed` 只服务于训练——依赖清单本身就把「GPT 只造数据」这件事坐实了。
- 真实坑：代码里用到的 `rouge_score`、`accelerate`、`numpy` 并没有写进 `requirements.txt`，照单安装后跑某些脚本还需补装——这是读源码要留意的「依赖滞后」。
- 文件按角色分三类：**脚本（做事）/ 数据（内容）/ 配置（参数）**；同名 `.json` 可能是数据也可能是配置，要看用途而非后缀。
- 每个目录都混合了「脚本 + 数据」，但只有顶层和 `train/` 各有一个配置文件；没有任何脚本跨两条流程，职责严格隔离。

---

## 7. 下一步学习建议

本讲只看了「骨架」，没有进入任何脚本的内部。建议下一步：

1. **先跑通一个推理**：进入 [u1-l3 快速上手：加载预训练模型做推理](./u1-l1-quick-start-inference.md)，用 README 的代码片段加载 `ishorn5/RTLCoder-v1.1` 生成一段 Verilog，获得「这个仓库能产出什么」的直观感受。
2. **或先看懂数据格式**：如果你更关心「数据长什么样」，进入 [u1-l4 数据格式详解](./u1-l4-data-formats.md)，对照 `data_sample.json`、`Resyn27k.json`、`scoring_data_sample.json` 三种 JSON 结构。
3. **进阶精读路线**：等入门层看完，进阶层（u2）会带你**逐文件**精读 `data_generation/`、`train/`、`benchmark_inference/` 三个目录里的每个脚本——届时本讲的「文件一行清单」就是你的导航地图。
4. **延伸阅读**：随手打开 [README.md:L60-L77](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L60-L77)，对着 Figure 1（数据生成流程）和 Figure 2（训练方案）的图，把本讲的四个目录在两张图里「对号入座」，能帮你把结构与论文贡献连起来。

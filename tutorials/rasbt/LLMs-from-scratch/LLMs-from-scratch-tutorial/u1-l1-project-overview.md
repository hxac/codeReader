# 项目定位与整体结构

> 适用阶段：入门（beginner）
> 本讲是整个学习手册的**第一篇**，不预设你已读过任何后续讲义。
> 关键源码：`README.md`、`pyproject.toml`、`requirements.txt`

---

## 1. 本讲目标

读完本讲，你应当能够：

- 说清楚**这个仓库是什么**：它是《Build a Large Language Model (From Scratch)》一书的官方代码库，目标是带你「从零」用 PyTorch 搭出一个类 ChatGPT 的 GPT 模型。
- 看懂**顶层目录是怎么组织的**：`ch01`–`ch07` 七个正文章节目录、`appendix-A/B/C/D/E` 五个附录目录、`setup`、`pkg` 等辅助目录各自承担什么职责。
- 掌握**技术栈与依赖清单**：核心依赖是 PyTorch、tiktoken、（用于加载 OpenAI GPT-2 权重的）TensorFlow，以及 JupyterLab、matplotlib 等；知道去哪里安装、各章分别用到哪些库。

本讲不要求你写代码，但会带你读项目最关键的三个文件，建立一张「全局地图」。有了这张地图，后续每一篇讲义你都能随时定位到自己关心的章节。

---

## 2. 前置知识

本讲几乎不需要任何机器学习背景。下面几个概念理解到「能读下去」的程度即可：

- **大语言模型（LLM）**：一类用海量文本训练出来的神经网络，能根据上下文预测下一个词，从而「续写」文本。ChatGPT 就是一种 LLM。
- **GPT**：Generative Pre-trained Transformer 的缩写，是一类「解码器型」Transformer，本仓库要亲手实现的就是它的精简教学版。
- **PyTorch**：一个主流的深度学习框架，用「张量（tensor）」做计算，并能自动求导。本仓库全部用 PyTorch「从零」实现，**不依赖任何现成的 LLM 库**（如 transformers）。
- **Jupyter Notebook（.ipynb）**：一种可以逐段运行代码、并把代码和输出混排在一起的文档。本书的章节正文都以 notebook 形式呈现。
- **从零（from scratch）**：指尽量不调用高层封装，而是用最基础的张量运算一行行把算法写出来，目的是让你**理解原理**，而不是追求工程性能。

> 小提示：如果你完全没用过 PyTorch，先不用慌——本书附录 A 是一份 PyTorch 速成，本手册也会在 `u8-l1` 专门讲解。

---

## 3. 本讲源码地图

本讲只读三个最顶层的「元数据」文件，它们决定了整个仓库的定位、结构和依赖：

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| [`README.md`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/README.md) | 项目主页说明：定位、章节目录表、安装提示、硬件要求、附加材料索引。 | 理解项目定位、章节组织与学习顺序。 |
| [`pyproject.toml`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/pyproject.toml) | 现代化的 Python 项目配置：包名、版本、Python 版本要求、依赖清单、可选依赖组（bonus）。 | 了解技术栈、Python 版本与正式依赖。 |
| [`requirements.txt`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/requirements.txt) | pip 直接安装用的依赖清单，并标注了**每个库被哪些章节使用**。 | 快速安装 + 看清「哪章用到哪个库」。 |

> 后续讲义会逐层进入 `ch02`–`ch07` 的真实代码；本讲先把「地图」画好。

---

## 4. 核心概念与源码讲解

本讲包含三个最小模块：

1. **4.1 项目定位与书籍关系** —— 这个仓库到底要构建什么。
2. **4.2 顶层目录与章节结构** —— 仓库是怎么按章节组织的。
3. **4.3 技术栈与依赖清单** —— 用了哪些库、怎么装。

---

### 4.1 项目定位与书籍关系

#### 4.1.1 概念说明

很多开源 LLM 项目的目标是「跑通一个可用的大模型」，而这个仓库的目标很不一样：**它的首要目标是「教学」**。它把构建一个 GPT 类模型的过程拆成了一条由浅入深、可逐步验证的流水线：

> 文本数据处理 → 注意力机制 → GPT 模型组装 → 预训练 → 分类微调 → 指令微调

作者强调，这条流水线**镜像了真实大规模基础模型（如 ChatGPT 背后的模型）的训练方法**，只是规模小到可以在普通笔记本上跑通；同时它也提供了「加载更大的预训练权重来做微调」的代码。

因此阅读本仓库时，心态要调整为：**理解原理 > 复现 SOTA**。每一章都在上一章基础上只新增一小块，验证一块再前进一块。

#### 4.1.2 核心流程

仓库的定位决定了它的几条「设计原则」，你可以把它们理解为项目自上而下的流程约束：

1. **以章节为骨架**：学习路径由书决定，目录严格对应章节。
2. **不引入黑盒**：所有 LLM 相关实现都用 PyTorch 基础算子手写，唯一例外是分词器用 `tiktoken`（因为分词不是本书重点）。
3. **代码可在普通笔记本运行**：正文代码不强依赖 GPU；有 GPU 会自动加速。
4. **可加载真实权重**：提供脚本把 OpenAI 官方 GPT-2 权重加载进自建模型，用于验证「我们手写的结构和真实模型一致」。

#### 4.1.3 源码精读

仓库首页开篇就点明了它的定位——它是书的官方代码库，目标是带你**由内而外**地理解 LLM，并一步步亲手造一个：

- [README.md:L1-L3](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/README.md#L1-L3)：说明本仓库是「developing, pretraining, and finetuning a GPT-like LLM」的代码，且是书的官方代码库。
- [README.md:L12-L14](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/README.md#L12-L14)：点明教学思路是「from the ground up, step by step」，并说明该方法**镜像了真实大规模基础模型的训练方式**，同时包含加载预训练权重做微调的代码。

作者还在前置知识里点明：本书**全程用 PyTorch 从零实现，不使用任何外部 LLM 库**：

- [README.md:L91-L99](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/README.md#L91-L99)：前置知识说明，强调「PyTorch basics is useful」，并推荐不熟悉 PyTorch 的读者先看附录 A。

硬件要求同样是「教学优先」的体现——正文代码为普通笔记本设计，有 GPU 自动用：

- [README.md:L106-L108](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/README.md#L106-L108)：说明正文代码可在普通笔记本上合理时间内跑完，无需专用硬件，并自动利用 GPU。

#### 4.1.4 代码实践

> 这是一条**源码阅读型实践**，目的是确认你抓准了项目的教育定位。

1. **实践目标**：能用自己的话回答「这个仓库为什么存在、它和书是什么关系」。
2. **操作步骤**：
   - 打开 [`README.md`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/README.md)，只读第 1–18 行和「Prerequisites」「Hardware Requirements」两节。
   - 用一句话写下：本仓库要构建的目标模型是什么？（提示：一个 __ GPT-like LLM，用 __ 框架从零实现）
3. **需要观察的现象**：注意作者反复强调的两个关键词「from scratch」「step by step」。
4. **预期结果**：你会意识到「从零」指的是不依赖 transformers 之类的库，而是用 PyTorch 张量运算手写；「step by step」对应后面按章节组织的学习路径。
5. 待本地验证：无（纯阅读）。

#### 4.1.5 小练习与答案

**练习 1**：本仓库是「工程优先」还是「教学优先」？给出一条来自 README 的证据。
> **参考答案**：教学优先。证据如 [README.md:L12-L14](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/README.md#L12-L14) 强调「understand how LLMs work from the inside out by coding them from the ground up」，以及硬件要求声明正文代码可在普通笔记本运行、面向「a wide audience」。

**练习 2**：本书是否使用外部 LLM 库（如 HuggingFace transformers）来实现正文模型？
> **参考答案**：正文不使用。README 的 Prerequisites 明确「uses PyTorch to implement the code from scratch without using any external LLM libraries」。

---

### 4.2 顶层目录与章节结构

#### 4.2.1 概念说明

这个仓库**天然以章节组织**：顶层目录就是 `ch01`–`ch07` 加上若干 `appendix-*`。这是它和一般项目最大的不同——目录结构本身就是一张学习路线图。

每个正文章节目录内部还有一个高度一致的**子目录约定**：

- `01_main-chapter-code/`：**正文代码**，是学习的核心。里面通常有：
  - `<章>.ipynb`：与书中代码逐行对应的主 notebook（最重要）。
  - `previous_chapters.py`：把前几章已经实现好的代码「汇总复用」成一个模块，供本章 import。
  - 一个或多个 `summary` 形式的 `.py` 文件：把 notebook 里的关键代码整理成可直接运行的脚本。
  - `exercise-solutions.ipynb`：本章习题答案。
  - `the-verdict.txt`（仅 ch02）等数据文件。
- `02_*`、`03_*` …：该章的**附加（bonus）材料**，是可选的进阶内容。

掌握这套约定后，你打开任何一章都能立刻找到「该从哪个文件读起」。

#### 4.2.2 核心流程

把七正章 + 三主要附录串起来，就是一条完整的「从零构建并训练 GPT」流水线，**章节之间是严格自底向上的依赖关系**：

```text
ch02 文本数据处理 (分词/词表/嵌入)
        │  产出: token id 与嵌入向量
        ▼
ch03 注意力机制 (自注意力/因果掩码/多头)
        │  产出: MultiHeadAttention 模块
        ▼
ch04 GPT 模型组装 (LayerNorm/FFN/TransformerBlock/GPTModel)
        │  产出: 完整 GPT 模型 + 文本生成函数
        ▼
ch05 预训练 (损失/训练循环/解码策略/加载 OpenAI 权重)
        │  产出: 可用的预训练权重
        ▼
ch06 分类微调 (替换输出头, 冻结主干, 垃圾短信分类)
        ▼
ch07 指令微调 (指令数据, 自定义 collate, 跟随指令)
        ▼
appendix-D 训练增强 (warmup/余弦衰减/梯度裁剪)
appendix-E 参数高效微调 (LoRA)
```

依赖关系的代码落地方式就是每章的 `previous_chapters.py`：比如 `ch04` 要用它从 `ch03` 继承 `MultiHeadAttention`，`ch05` 要用它从 `ch04` 继承 `GPTModel`。**所以阅读顺序强烈建议从 ch02 开始一路向前**，跳着读会因为缺前置模块而报错。

#### 4.2.3 源码精读

README 中的**章节目录表**是整张学习地图的权威出处（下面是节选，完整表见原文件）：

- [README.md:L64-L79](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/README.md#L64-L79)：章节目录表，列出 Ch1–Ch7 与各附录对应的主代码文件。例如第 4 行对应「Ch 4: Implementing a GPT Model from Scratch」，主代码是 `ch04.ipynb`（正文）和 `gpt.py`（summary 脚本）。

注意 ch01 是「Understanding Large Language Models」，属于概念章，**没有代码**：

- 仓库中 `ch01/` 目录只含 `README.md` 和 `reading-recommendations.md`，印证了「No code」。

每章 `01_main-chapter-code` 内部「正文 notebook + 汇总脚本」的约定，在 ch04 的 README 里有明确说明：

- [ch04/01_main-chapter-code/README.md:L5-L6](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/README.md#L5-L6)：说明 `ch04.ipynb` 是本章全部代码，`previous_chapters.py` 提供上一章的 `MultiHeadAttention` 供本章 import。
- [ch04/01_main-chapter-code/README.md:L10](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/README.md#L10)：说明 `gpt.py` 是把「到目前为止实现的全部代码」整理成的独立可运行脚本。

附加材料（bonus）的索引集中在 README 末尾，数量庞大且可选：

- [README.md:L150-L209](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/README.md#L150-L209)：按章节列出所有 bonus 材料（如 ch04 的 KV cache、GQA/MLA/SWA、MoE；ch05 的 GPT→Llama、Qwen3、Gemma3 等）。

下面是「主章节 → 主 notebook 文件」对照表（基于上述章节目录表与目录结构整理）：

| 章节 | 主题 | 主 notebook（正文） | summary 脚本（示例） |
| --- | --- | --- | --- |
| Ch 2 | 处理文本数据 | `ch02/01_main-chapter-code/ch02.ipynb` | `dataloader.ipynb` |
| Ch 3 | 实现注意力 | `ch03/01_main-chapter-code/ch03.ipynb` | `multihead-attention.ipynb` |
| Ch 4 | 从零搭 GPT | `ch04/01_main-chapter-code/ch04.ipynb` | `gpt.py` |
| Ch 5 | 预训练 | `ch05/01_main-chapter-code/ch05.ipynb` | `gpt_train.py`、`gpt_generate.py` |
| Ch 6 | 分类微调 | `ch06/01_main-chapter-code/ch06.ipynb` | `gpt_class_finetune.py` |
| Ch 7 | 指令微调 | `ch07/01_main-chapter-code/ch07.ipynb` | `gpt_instruction_finetuning.py` |
| 附录 A | PyTorch 入门 | `appendix-A/01_main-chapter-code/code-part1.ipynb` 等 | `DDP-script.py` |
| 附录 D | 训练增强 | `appendix-D/01_main-chapter-code/appendix-D.ipynb` | — |
| 附录 E | LoRA | `appendix-E/01_main-chapter-code/appendix-E.ipynb` | — |

> 除了章节目录，顶层还有几个辅助目录值得一提：`setup/`（环境安装指南）、`pkg/`（发布到 PyPI 的可复用包 `llms_from_scratch`）、`reasoning-from-scratch/`（姊妹项目，推理模型）、`troubleshooting.md`（排错指南）。这些都不是学习主线，但在你需要时会很有用。

#### 4.2.4 代码实践

> 这是本讲的**主任务**（也是大纲指定的实践）。它帮你把「章节 ↔ 文件」的映射牢牢建立起来。

1. **实践目标**：为每个主章节写出「主 notebook 文件路径」，并说明它依赖哪些前置章节。
2. **操作步骤**：
   - 先克隆仓库（如果你还没克隆）：
     ```bash
     git clone --depth 1 https://github.com/rasbt/LLMs-from-scratch.git
     ```
     该命令取自 [README.md:L29-L31](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/README.md#L29-L31)。
   - 打开 [README.md:L64-L79](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/README.md#L64-L79) 的章节目录表。
   - 在表格里填写：每章的「Main Code」第一项（通常就是 `chXX.ipynb`）的完整相对路径。
   - 用一句话说明每章依赖的前置章节（提示：参考 4.2.2 的依赖流程图）。
3. **需要观察的现象**：注意「Main Code」列里通常有多个条目——带 `(summary)` 标记的是把 notebook 关键代码整理成的独立脚本，不是正文。
4. **预期结果**：你会得到一张与上面「主章节 → 主 notebook」表类似的映射，并能解释为什么 ch05 的脚本 `gpt_train.py` 必须能 import 到 ch04 的 `GPTModel`（通过 `previous_chapters.py`）。
5. 待本地验证：无（纯阅读 + 整理）。

#### 4.2.5 小练习与答案

**练习 1**：你想学习「注意力机制」，应该打开哪个文件作为入口？
> **参考答案**：`ch03/01_main-chapter-code/ch03.ipynb`（正文）；想看整理好的独立实现可看 `ch03/01_main-chapter-code/multihead-attention.ipynb`。

**练习 2**：ch04 目录里的 `previous_chapters.py` 起什么作用？为什么需要它？
> **参考答案**：它把第 3 章实现的 `MultiHeadAttention`（以及更早章节的代码）汇总成一个可 import 的模块，让第 4 章的 `ch04.ipynb` 能直接复用而无需重复贴代码。需要它是因为本项目是「自底向上」的——每一章都建立在前几章的实现之上。

**练习 3**：ch01 目录下为什么没有 `01_main-chapter-code`？
> **参考答案**：因为第 1 章「Understanding Large Language Models」是概念介绍章，没有代码，README 的目录表也标注了「No code」。

---

### 4.3 技术栈与依赖清单

#### 4.3.1 概念说明

「技术栈」就是构建这个项目用到的所有库。这个仓库的技术栈相当精简，核心只有三块：

- **PyTorch（torch）**：唯一的深度学习框架，承载全部模型实现与训练。
- **tiktoken**：OpenAI 开源的 BPE 分词器，用于把文本切成 token。这是项目里少数「不自己从零写」的部分（分词不是本书重点）。
- **TensorFlow / tensorflow-cpu**：**仅用于把 OpenAI 发布的 GPT-2 权重（TensorFlow checkpoint 格式）解析并加载**到我们的 PyTorch 模型里。本书并不用 TF 训练。

其余都是数据/可视化/工程类辅助库：JupyterLab（跑 notebook）、matplotlib（画损失曲线）、pandas/numpy（数据处理）、tqdm（进度条）、pytest（测试）。

> 名词解释：
> - **BPE（Byte Pair Encoding，字节对编码）**：一种子词分词算法，把罕见词拆成更小的「子词」单元。GPT-2 系列用的就是 BPE，本仓库通过 `tiktoken` 直接加载 GPT-2 的 BPE 词表。
> - **checkpoint**：训练过程中保存下来的模型权重文件。OpenAI 当年用 TensorFlow 存的 GPT-2 权重就是 TF 格式的 checkpoint。

#### 4.3.2 核心流程

依赖管理的两条路径，对应两类读者：

1. **快速上手（pip）**：从仓库根目录执行 `pip install -r requirements.txt`。`requirements.txt` 还贴心地标注了**每个库被哪些章节用到**，方便你「用到哪章装哪个」。
2. **正式安装（pyproject.toml）**：`pyproject.toml` 是更规范的现代化配置，定义了 PyPI 包 `llms-from-scratch`、精确的版本约束与平台分支（macOS/Windows/Linux、x86/ARM），以及可选依赖组 `bonus`（附加材料用到）。

由于 PyTorch 和 TensorFlow 在不同操作系统/芯片架构上的可用版本不同，配置里用「环境标记（marker）」做了平台分支，例如 `tensorflow-cpu` 在 Linux x86 上用、`tensorflow` 在 macOS ARM 上用。

安装流程图：

```text
选择安装方式
   ├── 只要跑正文  → pip install -r requirements.txt （最简）
   └── 要跑 bonus   → uv pip install --group bonus   （附加依赖）
```

#### 4.3.3 源码精读

`requirements.txt` 是最实用的安装清单，且**逐行标注了库与章节的对应关系**：

- [requirements.txt:L1-L11](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/requirements.txt#L1-L11)：例如 `tiktoken >= 0.5.1  # ch02; ch04; ch05`、`tensorflow >= 2.18.0 ... # ch05; ch06; ch07`、`pandas >= 2.2.1  # ch06`，能直接看出「哪章用到哪个库」。

`pyproject.toml` 给出更正式的依赖定义与平台分支：

- [pyproject.toml:L5-L10](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/pyproject.toml#L5-L10)：包名 `llms-from-scratch`、版本、描述，以及 Python 版本要求 `>=3.10,<3.13`。
- [pyproject.toml:L11-L29](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/pyproject.toml#L11-L29)：核心依赖数组，含按平台分支的 `torch` 与 `tensorflow`（注意 Linux x86 用 `tensorflow-cpu`）、`tiktoken`、`jupyterlab`、`matplotlib`、`pandas`、`pytest` 等。

附加材料依赖被单独放在 `bonus` 可选组，避免正文读者装一堆用不上的库：

- [pyproject.toml:L41-L56](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/pyproject.toml#L41-L56)：`bonus` 依赖组，包含 `transformers`、`safetensors`、`openai`、`chainlit`、`sentencepiece` 等（这些是 bonus 材料才需要的）。

`pkg/` 目录下的代码被组织成可发布的 PyPI 包，`pyproject.toml` 指定了它的包目录：

- [pyproject.toml:L69-L73](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/pyproject.toml#L69-L73)：`[tool.setuptools]` 段说明 `package-dir = {"" = "pkg"}`，即顶层 `pkg/llms_from_scratch/` 就是发布包的源码目录。

最权威的安装指引其实在 `setup/README.md`：

- [setup/README.md:L8-L20](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/setup/README.md#L8-L20)：Quickstart，给出 `pip install -r requirements.txt`，以及 Colab 的安装单行命令和 `uv pip install --group bonus`。

#### 4.3.4 代码实践

> 这是一条**环境检查型实践**。它确认你装好了正文所需的最小依赖。

1. **实践目标**：确认 PyTorch 与 tiktoken 可正常导入，并验证版本满足 `requirements.txt` 的下限。
2. **操作步骤**：
   - 按上一模块的方式安装依赖（`pip install -r requirements.txt`）。
   - 在仓库根目录新建一个临时 Python 文件或在 REPL 中运行（**示例代码**，非项目原有代码）：
     ```python
     # 示例代码：检查正文最小依赖是否就绪
     import torch, tiktoken, jupyterlab
     print("torch", torch.__version__)
     print("tiktoken", tiktoken.__version__)
     print("cuda available:", torch.cuda.is_available())
     enc = tiktoken.get_encoding("gpt2")
     print("gpt2 vocab size:", enc.n_vocab)
     ```
3. **需要观察的现象**：torch 与 tiktoken 版本号是否分别 `>= 2.2.2` 与 `>= 0.5.1`；`cuda available` 在没有 GPU 时应为 `False`（正常，正文不强依赖 GPU）。
4. **预期结果**：能成功打印版本号，且 `enc.n_vocab` 约为 50257（GPT-2 的 BPE 词表大小）。如果 `tiktoken.get_encoding` 首次运行需要联网下载词表，属正常现象。
5. 待本地验证：请在你本机实际运行确认上述数值；不同平台下 `torch` 具体小版本号可能略有差异。

#### 4.3.5 小练习与答案

**练习 1**：为什么本项目要引入 TensorFlow？它用在训练阶段吗？
> **参考答案**：TensorFlow **不用于训练**。它只在 ch05/ch06/ch07 中用于把 OpenAI 发布的 GPT-2 权重（TensorFlow checkpoint 格式）读出来、再映射进我们的 PyTorch 模型，目的是验证我们手写的结构与真实模型一致，并复用真实预训练权重。证据见 [requirements.txt:L7](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/requirements.txt#L7) 的注释 `# ch05; ch06; ch07`。

**练习 2**：`requirements.txt` 与 `pyproject.toml` 都列了依赖，二者关系是什么？普通读者该用哪个？
> **参考答案**：`pyproject.toml` 是规范的现代项目配置（定义 PyPI 包、精确版本与平台分支、可选组 bonus）；`requirements.txt` 是面向「直接 pip 安装」的精简清单，并标注了库与章节的对应。普通读者跟随 `setup/README.md` 的 Quickstart 用 `pip install -r requirements.txt` 最省事。

**练习 3**：如果只想跑 ch02 的正文，最小依赖是什么？
> **参考答案**：至少需要 `torch`、`tiktoken`（`requirements.txt` 标注 tiktoken 用于 ch02/ch04/ch05）和 `jupyterlab`（跑 notebook）。具体以 [requirements.txt:L1-L11](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/requirements.txt#L1-L11) 的注释为准。

---

## 5. 综合实践

把三个模块串起来，完成一份**「项目入职手册」**，作为你后续学习所有讲义的常备参考：

1. **画目录地图**：把顶层目录分成三类——学习主线（`ch02`–`ch07`、`appendix-A/D/E`）、环境与工具（`setup`、`pkg`、`troubleshooting.md`）、姊妹/附加项目（`reasoning-from-scratch`、各章 bonus 子目录）。
2. **填章节文件表**：按 4.2.4 的步骤，写出每个主章节的主 notebook 路径。
3. **标注依赖链**：在每章旁注明「依赖哪几章」，并解释 `previous_chapters.py` 如何承载这条依赖（提示：ch04 用它继承 ch03 的 `MultiHeadAttention`，ch05 用它继承 ch04 的 `GPTModel`）。
4. **标注技术栈职责**：在一个小表里写清 `torch`、`tiktoken`、`tensorflow`、`matplotlib`、`pandas` 各自「在哪个章节、干什么」。
5. **确认环境**：按 4.3.4 运行示例代码，确认最小依赖就绪（待本地验证）。

完成后，你应该能回答三个问题：**（a）这仓库是什么；（b）从哪个文件开始读；（c）要装哪些库。** 这三问答上来，本讲就达标了。

---

## 6. 本讲小结

- 本仓库是《Build a Large Language Model (From Scratch)》的官方代码库，定位**教学**：带你用 PyTorch「从零」造一个类 ChatGPT 的 GPT 模型，并镜像真实大规模模型的训练流程。
- 顶层目录**以章节组织**，`ch02`–`ch07` 构成「数据处理 → 注意力 → GPT 模型 → 预训练 → 分类微调 → 指令微调」的自底向上主线，章节间通过每章的 `previous_chapters.py` 汇总复用前置代码。
- 每章 `01_main-chapter-code/` 是学习核心，约定是「正文 `<章>.ipynb` + summary `.py` 脚本 + `exercise-solutions.ipynb`」；`02_*` 及以后是可选 bonus。
- 技术栈精简：核心是 **PyTorch（实现/训练）+ tiktoken（分词）+ TensorFlow（仅用于加载 OpenAI GPT-2 权重）**，外加 JupyterLab、matplotlib、pandas、tqdm 等辅助库。
- 普通读者用 `pip install -r requirements.txt` 即可跑通正文；`requirements.txt` 还逐行标注了「库 ↔ 章节」对应关系。
- 正文代码为普通笔记本设计，**不强依赖 GPU**，有 GPU 会自动加速。

---

## 7. 下一步学习建议

- 下一篇讲义是 **`u1-l2` 环境搭建与运行第一个模型**：动手装好依赖、运行 `ch04/01_main-chapter-code/gpt.py` 跑通第一次文本生成，把「读」变成「跑」。
- 如果你完全没用过 PyTorch，可以先跳到附录 A（对应 `u8-l1` PyTorch 核心基础）扫一眼，再回来；不过本手册前几章不需要深厚的 PyTorch 功底。
- 想立刻看到成果：直接读 `ch04/01_main-chapter-code/gpt.py` 里的 `main()`，那是「一次完整自回归生成」的最短入口。
- 想理解「每章如何复用前几章代码」：读下一篇 **`u1-l3` 仓库阅读地图**，它会专门讲 `previous_chapters.py` 这一汇总机制。

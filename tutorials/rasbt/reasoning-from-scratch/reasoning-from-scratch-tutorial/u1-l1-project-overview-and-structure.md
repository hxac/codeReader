# 第 1 讲　项目定位与仓库结构

## 1. 本讲目标

本讲是整本学习手册的起点。学完本讲，你应该能够：

1. 用自己的话说出这个仓库是干什么的：它是《Build a Reasoning Model (From Scratch)》一书的配套代码库，目标是在一个预训练好的 Qwen3 0.6B 基座模型之上，**亲手从零实现推理（reasoning）能力**。
2. 说出本书三大方法的名字和一句话含义：推理时扩展（inference-time scaling）、强化学习（reinforcement learning）、蒸馏（distillation）。
3. 对照目录树，说出 `ch01`–`ch08` 章节文件夹、`chC`–`chG` 附录文件夹、`reasoning_from_scratch` Python 包和 `tests` 测试目录各自的分工。
4. 能独立找到任何一章的入口 notebook（`chXX_main.ipynb`），并知道 `pyproject.toml` 里声明了哪些依赖、要求什么 Python 版本。

本讲不涉及任何模型运行，只做「看懂地图」这一件事。

## 2. 前置知识

本讲几乎不需要机器学习背景，但下面几个名词最好先有个印象：

- **LLM（大语言模型）**：输入一段 token 序列、输出下一个 token 概率分布的神经网络。本仓库用的是阿里开源的 **Qwen3** 系列（0.6B 参数起步）。
- **基座模型（base model）与推理模型（reasoning model）**：基座模型只会"接着往下写"；推理模型（如 DeepSeek R1、OpenAI 的 o 系列）在给出最终答案前会先生成一段较长的"思考过程"再总结答案。本书要做的，就是把一个现成的基座模型改造成推理模型。
- **Notebook（.ipynb）**：Jupyter 笔记本文件，可以逐格运行代码并保留输出，是这本书每一章的"教学现场"。
- **`pyproject.toml`**：Python 项目的"身份证 + 购物清单"，声明项目名、支持的 Python 版本、依赖哪些第三方库。`pip` 和 `uv` 等安装工具都读它。
- **Python 包（package）**：一个含 `__init__.py` 的文件夹，可以被 `import`。本仓库的核心包叫 `reasoning_from_scratch`。
- **pytest / CI**：`tests/` 目录下的自动化测试脚本；`.github/workflows/` 下的配置让 GitHub 在每次提交时自动跑这些测试（持续集成）。

术语先混个脸熟即可，后面每一讲都会反复见到它们。

## 3. 本讲源码地图

先看仓库整体布局（为节省篇幅做了裁剪，只保留与本讲相关的部分）：

```
reasoning-from-scratch/
├── README.md                 # 项目总说明 + 章节目录表（本讲核心）
├── pyproject.toml            # 包元数据、依赖声明、构建配置（本讲核心）
├── requirements.txt          # 依赖清单（内容与 pyproject 基本对应）
├── troubleshooting.md        # 常见问题排查手册
├── ch01/                     # 第 1 章：无代码，只有 README
├── ch02/ ~ ch08/             # 第 2~8 章：每章含 01_main-chapter-code/（notebook 入口）+ 若干附加脚本子目录
├── chC/ chD/ chE/ chF/ chG/  # 附录 C~G：模型源码、大模型、批处理、评测、聊天界面
├── reasoning_from_scratch/   # 核心 Python 包（章节 notebook 复用的实现代码）
│   ├── __init__.py           # 包入口，仅含版本号（本讲核心）
│   ├── qwen3.py              # Qwen3 模型架构 + 分词器 + 权重加载
│   ├── qwen3_optimized.py    # GPU 优化版模型（SDPA、预分配 KV cache）
│   ├── qwen3_batched.py      # 批量推理版模型
│   ├── ch02.py ~ ch08.py     # 与第 2~8 章一一对应的工具模块
│   ├── appendix_c.py         # 1.7B~32B 更大模型的配置与加载
│   ├── appendix_f.py         # 附录 F 的评测工具（Elo 等）
│   ├── bonus/parser.py       # 高级混合 LaTeX 解析器
│   └── utils.py              # 文件下载工具
├── tests/                    # pytest 测试套件（每章/每模块一个 test_*.py）
└── .github/workflows/        # CI 配置（Linux/macOS/Windows 测试矩阵等）
```

本讲精读的关键文件：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目定位说明、章节目录表（哪章有什么代码一目了然） |
| `pyproject.toml` | 项目名、Python 版本要求、依赖列表、打包配置 |
| `ch01/README.md` | 第 1 章目录，用来印证"有些章节没有代码" |
| `reasoning_from_scratch/__init__.py` | 包入口文件，定义 `__version__` |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**README 目录表**、**pyproject.toml**、**reasoning_from_scratch 包结构**。

### 4.1 项目定位与 README 目录表

#### 4.1.1 概念说明

打开任何开源项目，第一步都应该是读 README。这个仓库的 README 告诉我们三件事：

1. **它是什么**：《Build a Reasoning Model (From Scratch)》一书的官方代码库。这本书的思路是"动手做"——不满足于在理论上知道"推理模型"是什么，而是从一个预训练基座模型出发，用代码一步步把推理能力加上去，让你亲眼看到每一步发生了什么。
2. **它解决什么问题**：推理（reasoning）是最近提升 LLM 能力最显著的进展之一，但也最容易被"只听名词"式地误解。本书通过可运行、可修改的小规模复现（对标 DeepSeek R1、GPT-5 Thinking 等大规模推理模型的做法），把这件事讲透。
3. **它用哪三条路**：README 明确列出了三大方法——**推理时扩展**（不训练权重，靠采样策略、思维链、多次投票等在推理阶段提升效果）、**强化学习**（用 GRPO 等算法在可验证奖励上微调权重）、**蒸馏**（让更强的教师模型生成带思考过程的答案，再监督微调小模型）。这三条路正好对应本书第 4~5 章、第 6~7 章、第 8 章。

#### 4.1.2 核心流程

整个仓库（以及整本书）的方法论主线可以用一句话概括：

```
预训练 Qwen3 0.6B 基座模型（下载权重，不自己预训练）
        │
        ├── 路线一：推理时扩展（第 4~5 章）
        │     温度/top-p 采样 → 思维链提示 → 自一致性投票 → Best-of-N → 自我精炼
        │     （不改权重，只改"怎么问"和"怎么采样"）
        │
        ├── 路线二：强化学习 RLVR/GRPO（第 6~7 章）
        │     数学题 + 可验证奖励（答案对=1，错=0）→ GRPO 训练 → 裁剪/KL/格式奖励等改进
        │     （改权重）
        │
        └── 路线三：知识蒸馏（第 8 章）
              大模型生成 <think>…</think> 教师答案 → 监督微调 0.6B 模型
              （改权重）
```

配套的仓库组织方式是：**每个 chXX 文件夹对应书的一章**，章内 `01_main-chapter-code/` 放主 notebook，编号更大的子目录（如 `02_`、`03_`）放附加脚本、日志或 checkpoint 下载工具。

#### 4.1.3 源码精读

**（1）项目定位与三大方法。** README 在介绍姊妹书时点明了技术路线：

> 在本书中，我们使用一个预训练的开源基座 LLM（Qwen3），在其之上从零编写推理方法。这包括推理时扩展、强化学习和蒸馏。

对应源码（这段还说明了本书与作者前一本书《Build a Large Language Model (From Scratch)》的分工：前一本书讲怎么造基座模型，本书讲怎么加推理能力）：[README.md:88-92](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/README.md#L88-L92)

README 还强调本书代码面向消费级硬件：第 2~4 章在 CPU 和 GPU 上都能跑，第 5、6 章若要复现书中结果建议用 GPU：[README.md:104-106](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/README.md#L104-L106)

**（2）章节目录表——仓库的"总目录"。** README 中间这张表是本讲最重要的地图，它逐行列出了每一章/每个附录的入口代码：[README.md:54-70](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/README.md#L54-L70)

| 行 | 章节 | 主代码位置 | 主题 |
| --- | --- | --- | --- |
| L56 | 第 1 章 | 无代码 | 理解推理模型（概念章） |
| L57 | 第 2 章 | `ch02/01_main-chapter-code/ch02_main.ipynb` | 用预训练 LLM 生成文本 |
| L58 | 第 3 章 | `ch03/01_main-chapter-code/ch03_main.ipynb` | 评测推理模型（MATH-500） |
| L59 | 第 4 章 | `ch04/01_main-chapter-code/ch04_main.ipynb` | 推理时扩展 |
| L60 | 第 5 章 | `ch05/01_main-chapter-code/ch05_main.ipynb` | 自我精炼 |
| L61 | 第 6 章 | `ch06/01_main-chapter-code/ch06_main.ipynb` | 强化学习训练 |
| L62 | 第 7 章 | `ch07/01_main-chapter-code/ch07_main.ipynb` | 改进 GRPO |
| L63 | 第 8 章 | `ch08/01_main-chapter-code/ch08_main.ipynb` | 蒸馏 |
| L66-70 | 附录 C~G | `chC`~`chG` 各自的 notebook 或脚本 | 模型源码/大模型/批处理/评测/聊天界面 |

注意两个细节：

- 每章除了 `chXX_main.ipynb`，还有一个 `chXX_exercise-solutions.ipynb`（习题解答），同样在 `01_main-chapter-code/` 下。
- 第 1 章确实没有代码。ch01 目录里只有一个 README，里面明说"本章没有代码"：[ch01/README.md:L1-L7](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch01/README.md#L1-L7)

**（3）附加材料（Bonus）。** README 后半段列出各章的编号子目录里都装了什么（例如 `ch02/03_optimized-LLM` 是 GPU 优化版模型、`ch07/04_download_trainining_checkpoints` 是训练 checkpoint 下载工具、`ch08/06_use_via_huggingface` 是与 Hugging Face 生态互通的两种方案）：[README.md:118-149](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/README.md#L118-L149)。这些目录会在后续单元的讲义中逐一展开，本讲只需知道"编号大于 01 的子目录 = 附加材料"。

#### 4.1.4 代码实践

**实践：验证 README 目录表与磁盘上的真实文件一致。**

1. **实践目标**：确认 README 表格不是"空头支票"——每个 `_main.ipynb` 入口都真实存在，并观察哪些章节没有 notebook。
2. **操作步骤**：在仓库根目录执行（只读命令，不改动任何文件）：

   ```bash
   git ls-files | grep -E "_main\.ipynb$"
   ```

3. **需要观察的现象**：输出应包含 `ch02` 到 `ch08` 的 7 个 `chXX_main.ipynb`，以及 `chC`、`chD`、`chE`、`chF` 的 4 个附录 notebook（`chD`/`chE` 的 notebook 直接放在章节根目录，不在 `01_main-chapter-code` 里）；`ch01` 和 `chG` 不应出现在结果中。
4. **预期结果**：共 11 个 `_main.ipynb` 文件；`chG`（聊天界面）只有 `.py` 脚本，`ch01` 无代码。以上判断基于对当前仓库目录的实际核对，具体输出格式待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：本书三大方法分别对应哪几章？哪条路线不修改模型权重？

答案：推理时扩展对应第 4~5 章，强化学习对应第 6~7 章，蒸馏对应第 8 章。推理时扩展不修改权重，只在推理阶段改变提示方式和采样策略；强化学习和蒸馏都会更新模型参数。

**练习 2**：如果你想看第 7 章（改进 GRPO）的主代码，应该打开哪个文件？

答案：`ch07/01_main-chapter-code/ch07_main.ipynb`（习题解答在同目录的 `ch07_exercise-solutions.ipynb`）。

**练习 3**：`ch07/02_logs/`、`ch08/03_logs/` 这类编号 `02_`、`03_` 的子目录是什么性质的内容？

答案：它们是章节的附加材料（Bonus），不是主章节代码。`02_logs` 存放作者训练时记录的指标 CSV 日志，供读者不跑训练也能分析和画图。

### 4.2 pyproject.toml：依赖与包元数据

#### 4.2.1 概念说明

`pyproject.toml` 是现代 Python 项目的标准配置文件。当你在仓库根目录执行 `pip install -e .` 或 `uv sync` 时，安装工具读的就是它。它回答三个问题：

1. **这是谁**：项目名 `reasoning_from_scratch`、描述、作者、许可证、版本号。
2. **需要什么环境**：`requires-python` 限定 Python 版本区间；`dependencies` 列出运行所需的第三方库。
3. **打包哪些代码**：`[tool.setuptools.packages.find]` 告诉构建后端只把 `reasoning_from_scratch` 包（而不是 `ch02/` 这些章节目录）打进安装包。

一个值得注意的设计：版本号是**动态的**（`dynamic = ["version"]`），真正的版本字符串写在包的 `__init__.py` 里，打包时由 setuptools 去读取。这保证了"包的版本"只有一个唯一出处。

#### 4.2.2 核心流程

安装这个项目时的信息流：

```
pip install -e . / uv sync
        │
        ▼
读取 pyproject.toml
        │
        ├─ [build-system]           → 知道用 setuptools 构建
        ├─ [project] name           → 包名叫 reasoning_from_scratch
        ├─ [project] requires-python → 检查你的 Python 是否 >=3.10 且 <3.15
        ├─ [project] dependencies   → 安装 torch、sympy、matplotlib 等运行依赖
        ├─ [dependency-groups] dev  → （可选）安装 pytest、ruff 等开发工具
        ├─ [project.optional-dependencies] extra → （可选）datasets、chainlit
        │
        ▼
从 reasoning_from_scratch/__init__.py 读取 __version__ 作为包版本
        │
        ▼
之后任何目录下都可以 import reasoning_from_scratch
```

#### 4.2.3 源码精读

**（1）动态版本号。** 版本号不在 toml 里写死，而是从包内读取：[pyproject.toml:L5-L6](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L5-L6) 声明"版本来自 `reasoning_from_scratch.__version__` 属性"，[pyproject.toml:L12-L16](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L12-L16) 则是项目名与 `dynamic = ["version"]` 的声明。

**（2）Python 版本要求。** [pyproject.toml:L23](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L23) 要求 `>=3.10,<3.15`，即 Python 3.10 到 3.14 都可以用（下方的 classifiers 也逐个列出了这五个版本）。

**（3）运行依赖。** [pyproject.toml:L25-L33](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L25-L33)：

- `torch>=2.10,<3` —— 深度学习框架，注释标明 2.10.0 是写书时的原始版本；
- `tokenizers>=0.21.2` —— 分词器（Qwen3 分词基于它）；
- `sympy>=1.14.0` —— 符号数学库，第 3 章的数学答案验证器靠它判断 `\frac{1}{2}` 和 `0.5` 相等；
- `matplotlib>=3.10.7` —— 画训练曲线等图表；
- `jupyterlab`、`nbformat`、`setuptools` —— notebook 环境与构建工具。

**（4）开发依赖与可选依赖。** [pyproject.toml:L46-L56](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L46-L56) 的 `dev` 组包含 pytest、ruff、transformers 等，只有跑测试/开发时才需要；[pyproject.toml:L58-L62](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L58-L62) 的 `extra` 组包含 `datasets`（附录 F 的 MMLU 评测用）和 `chainlit`（附录 G 聊天界面用，且标记了仅在 Python < 3.14 时可装）。

**（5）打包范围。** [pyproject.toml:L70-L72](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L70-L72) 明确只打包 `reasoning_from_scratch` 及其子包——这就是为什么章节文件夹（`ch02/` 等）不会被安装，它们只是"教学材料"，而包才是"可复用的实现"。

#### 4.2.4 代码实践

**实践：用 Python 解析 pyproject.toml 并打印关键信息。**

1. **实践目标**：不靠肉眼，用程序读出项目名、Python 版本要求和依赖列表。
2. **操作步骤**：在仓库根目录创建临时脚本 `peek_toml.py`（**示例代码**，非项目原有文件）：

   ```python
   # peek_toml.py（示例代码）
   import tomllib  # Python 3.11+ 自带；3.10 需先 pip install tomli，然后 import tomli as tomllib

   with open("pyproject.toml", "rb") as f:
       data = tomllib.load(f)

   project = data["project"]
   print("项目名:", project["name"])
   print("Python 版本要求:", project["requires-python"])
   print("运行依赖:")
   for dep in project["dependencies"]:
       print("  -", dep)
   ```

   运行 `python peek_toml.py`。
3. **需要观察的现象**：打印出的依赖共 7 项，其中 `torch` 的版本约束是 `>=2.10,<3`。
4. **预期结果**：项目名为 `reasoning_from_scratch`，`requires-python` 为 `>=3.10,<3.15`。若你在 Python 3.10 上运行，`import tomllib` 会报 `ModuleNotFoundError`，此时按注释改用 `tomli`。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sympy` 是运行依赖而不是可选依赖？

答案：因为第 3 章的数学验证器（判断模型给出的数学答案是否等价于标准答案）依赖 SymPy 做符号等价判断，这是贯穿全书评测和强化学习奖励计算的基础能力，属于核心运行路径。

**练习 2**：包的版本号 `0.2.0` 写在哪里？pyproject.toml 里为什么找不到？

答案：写在 `reasoning_from_scratch/__init__.py` 的 `__version__` 变量里；pyproject.toml 通过 `[tool.setuptools.dynamic]` 的 `version = { attr = "reasoning_from_scratch.__version__" }` 在构建时动态读取它。

**练习 3**：如果你只想跑附录 G 的聊天界面，需要预先装好哪些"额外"依赖？装了会拖累只跑主章节的用户吗？

答案：需要 `chainlit`（以及界面本身用到的模型依赖）。不会拖累——它放在 `[project.optional-dependencies] extra` 里，只有显式选择安装 extra 组时才会装，默认安装不包含它。

### 4.3 reasoning_from_scratch 包结构与章节目录的对应

#### 4.3.1 概念说明

这个仓库有两条并行的"代码线"，初学者最容易混淆：

1. **章节文件夹（`chXX/`）**：书的"教学现场"。里面是 notebook（带讲解、带图表输出）和独立的实验脚本。它们是**写给你读和跑的**。
2. **`reasoning_from_scratch` 包**：可 `pip install` 的正式 Python 模块。每章 notebook 里反复用到的核心函数（生成、打分、训练循环）被沉淀到这里，避免在多个 notebook 间复制粘贴。它们是**写给代码复用的**。

两者大体上**一章对一模块**：`ch02.py` 对应第 2 章的文本生成函数，`ch03.py` 对应第 3 章的评测与验证器……以此类推。例外是模型本身：`qwen3.py`（模型架构 + 分词器 + 权重加载）被所有章节共用，它是整个仓库的地基；`qwen3_optimized.py` 和 `qwen3_batched.py` 是它的两个工程化变体。

`tests/` 目录则是第三条线：每个包模块、每个章节脚本都有对应的 `test_*.py`（例如 `test_qwen3.py`、`test_ch04.py`），由 CI 在三个操作系统上自动运行，保证书里的代码随仓库演进始终可复现。

#### 4.3.2 核心流程

章节 notebook 与包模块的协作方式：

```
ch04/01_main-chapter-code/ch04_main.ipynb     （教学：讲解 + 演示）
        │  from reasoning_from_scratch.ch04 import ...
        ▼
reasoning_from_scratch/ch04.py                （实现：温度采样、top-p 过滤、自一致性投票）
        │  from reasoning_from_scratch import qwen3
        ▼
reasoning_from_scratch/qwen3.py               （地基：模型架构、分词器、权重下载与加载）
        ▲
        │  对应测试
tests/test_qwen3.py、tests/test_ch04.py        （保障：pytest 回归测试）
```

也就是说：**notebook 负责"讲"，包负责"干"，测试负责"守"**。你以后调试任何一章，都应先去包里找实现，再回 notebook 看讲解。

#### 4.3.3 源码精读

**（1）包入口与版本号。** `__init__.py` 整个文件除版权声明外只有一行有效代码——定义版本号：[reasoning_from_scratch/__init__.py:L25](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/__init__.py#L25)。当前版本为 `0.2.0`，这正是 4.2 节 pyproject.toml 动态读取的那个值。

**（2）包内文件与章节的对应关系**（以下文件均已核实存在）：

| 包内文件 | 行数 | 对应章节 | 内容 |
| --- | --- | --- | --- |
| `qwen3.py` | 662 | 全书共用 / 附录 C | Qwen3 模型、分词器、权重下载加载 |
| `ch02.py` / `ch02_ex.py` | 199 / 14 | 第 2 章 | 文本生成函数族 / 习题辅助 |
| `ch03.py` | 574 | 第 3 章 | MATH-500 加载、答案提取、数学验证器 |
| `ch04.py` | 319 | 第 4 章 | 温度采样、top-p、自一致性 |
| `ch05.py` | 268 | 第 5 章 | log-prob 打分、Best-of-N、自我精炼 |
| `ch06.py` | 338 | 第 6 章 | RLVR 奖励、GRPO 损失与训练循环 |
| `ch07.py` | 155 | 第 7 章 | 训练指标可视化、熵统计、格式奖励 |
| `ch08.py` | 362 | 第 8 章 | 蒸馏数据管线与训练循环 |
| `qwen3_optimized.py` | 677 | `ch02/03_optimized-LLM` | SDPA + 预分配 KV cache 的优化版 |
| `qwen3_batched.py` | 684 | 附录 E | 批量推理版模型 |
| `appendix_c.py` | 201 | 附录 C/D | 1.7B~32B 配置与 HF 权重映射 |
| `appendix_f.py` | 55 | 附录 F | Elo 排名等评测工具 |
| `bonus/parser.py` | — | `ch03/03_advanced-parser` | 高级混合 LaTeX 解析器 |
| `utils.py` | 113 | 全书共用 | 断点续传的文件下载工具 |

**（3）测试目录的组织。** `tests/` 下每个包模块都有同名测试文件（`test_ch02.py` 对 `ch02.py`……），公共夹具放在 `conftest.py`，README 说明了如何跑测试（推荐 `SKIP_EXPENSIVE=1 RUN_REAL_DOWNLOAD_TESTS=0 uv run pytest tests` 跳过耗时用例）：[tests/README.md:L13-L24](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/README.md#L13-L24)。测试体系的细节在第 10 单元专门讲。

#### 4.3.4 代码实践

**实践：确认包已可导入，并核对你的 Python 版本。**

1. **实践目标**：验证"包"这条线在你的环境里是通的。
2. **操作步骤**：

   ```bash
   python --version
   python -c "import reasoning_from_scratch as rfs; print(rfs.__version__)"
   ```

   如果你还没安装这个包（下一讲才讲安装），第二条命令会失败，这是**预期行为**；此时可以先退而求其次，直接查看版本定义那一行：

   ```bash
   grep __version__ reasoning_from_scratch/__init__.py
   ```

3. **需要观察的现象**：`python --version` 输出应落在 3.10~3.14 区间；grep 应输出 `__version__ = "0.2.0"`。
4. **预期结果**：安装前用 grep 能看到版本号；安装后（下一讲）import 命令应打印 `0.2.0`。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`ch05/01_main-chapter-code/ch05_main.ipynb 里调用的自我精炼循环函数，实现代码去哪里找？

答案：去 `reasoning_from_scratch/ch05.py` 找（函数名 `self_refinement_loop`，第 6 单元会精读）。notebook 里通常只有 `from reasoning_from_scratch.ch05 import ...` 这样的导入。

**练习 2**：为什么模型架构要单独放在 `qwen3.py`，而不是塞进某一章的 `chXX.py`？

答案：因为模型是所有章节的公共地基：第 2 章用它生成文本，第 4 章用它采样，第 6/7 章在它上面做强化学习，第 8 章蒸馏也基于它。放进任何一章都会造成别的章"反向依赖某一章"，单独成模块才符合依赖关系。

**练习 3**：`tests/test_ch07_scripts.py` 这类带 `_scripts` 后缀的测试文件，和 `test_ch07.py` 测的对象有什么不同？

答案：从命名可以看出，`test_ch07.py` 测的是包内 `ch07.py` 模块的函数；`test_ch07_scripts.py` 测的是 `ch07/03_rlvr_grpo_scripts_advanced/` 下的独立训练脚本（如脚本参数解析、可运行性），保证这些不在包里的脚本也被回归覆盖。

## 5. 综合实践

**任务：写一个 `explore_repo.py` 脚本，自动生成"项目元信息 + 章节-入口文件对照表"。** 这个小工具综合了本讲的三个模块：读 README 学到的章节布局、解析 pyproject.toml、遍历目录理解包结构。

1. **实践目标**：写一个脚本，一次输出：(a) 项目名、Python 版本要求、依赖列表；(b) ch01~ch08 每章的主 notebook 路径（或"无代码"）。

2. **操作步骤**：在仓库根目录创建 `explore_repo.py`（**示例代码**，非项目原有文件；建议用完删除或加入 `.gitignore`，避免混入书籍官方仓库）：

   ```python
   # explore_repo.py（示例代码）
   import os
   import tomllib  # Python 3.11+；3.10 需 pip install tomli 后 import tomli as tomllib

   REPO_ROOT = os.path.dirname(os.path.abspath(__file__))  # 脚本放在仓库根目录

   # --- 第 1 部分：解析 pyproject.toml ---
   with open(os.path.join(REPO_ROOT, "pyproject.toml"), "rb") as f:
       data = tomllib.load(f)

   project = data["project"]
   print("=" * 50)
   print("项目名          :", project["name"])
   print("Python 版本要求 :", project["requires-python"])
   print("版本号来源      : dynamic ->", end=" ")
   init = open(os.path.join(REPO_ROOT, "reasoning_from_scratch", "__init__.py")).read()
   for line in init.splitlines():
       if line.startswith("__version__"):
           print(line.strip())
   print("运行依赖:")
   for dep in project["dependencies"]:
       print("   -", dep)

   # --- 第 2 部分：os.walk 遍历 ch01~ch08，找主 notebook ---
   print("=" * 50)
   print("章节-入口文件对照表:")
   for i in range(1, 9):
       ch = f"ch{i:02d}"
       entries = []
       for dirpath, _dirnames, filenames in os.walk(os.path.join(REPO_ROOT, ch)):
           for fn in filenames:
               if fn.endswith("_main.ipynb"):
                   rel = os.path.relpath(os.path.join(dirpath, fn), REPO_ROOT)
                   entries.append(rel)
       if entries:
           for e in sorted(entries):
               print(f"  {ch}: {e}")
       else:
           print(f"  {ch}: 无代码（仅 README）")
   ```

   运行 `python explore_repo.py`。

3. **需要观察的现象**：第一部分应打印 7 条依赖（其中 torch 约束为 `>=2.10,<3`）和从 `__init__.py` 读到的 `__version__ = "0.2.0"`；第二部分中 `ch01` 一行应显示"无代码（仅 README）"，`ch02`~`ch08` 各打印一个 `chXX/01_main-chapter-code/chXX_main.ipynb` 路径。

4. **预期结果**：基于当前 HEAD（b864602）的目录核对，ch02~ch08 的主 notebook 都存在于 `01_main-chapter-code/` 子目录下，共 7 条记录；脚本的具体输出格式待本地验证。

5. **延伸**：把遍历范围从 `range(1, 9)` 改成 `["chC", "chD", "chE", "chF", "chG"]` 再跑一次，观察附录的特殊布局——`chD`、`chE` 的 notebook 直接放在章节根目录（不在 `01_main-chapter-code` 里），`chG` 则完全没有 notebook，只有 `.py` 脚本。这正好和 README 目录表（L66-L70）互相印证。

## 6. 本讲小结

- 本仓库是《Build a Reasoning Model (From Scratch)》的官方代码库：在预训练 Qwen3 0.6B 基座上，用**推理时扩展（第 4~5 章）、强化学习 RLVR/GRPO（第 6~7 章）、蒸馏（第 8 章）**三条路线从零构建推理模型。
- 仓库按"**章节文件夹（notebook 教学现场）+ `reasoning_from_scratch` 包（可复用实现）+ `tests`（pytest 回归）**"三条线组织，章节与包模块大体一章对一文件（`ch04.py` ↔ `ch04/`）。
- 每章入口在 `chXX/01_main-chapter-code/chXX_main.ipynb`，习题解答在同目录；第 1 章无代码；附录 C~G 布局略有例外（chD/chE 的 notebook 在章节根目录，chG 只有脚本）。
- `pyproject.toml` 要求 Python `>=3.10,<3.15`，核心依赖是 `torch>=2.10`、`tokenizers`、`sympy`（验证器用）、`matplotlib`；版本号动态取自 `reasoning_from_scratch/__init__.py` 的 `__version__ = "0.2.0"`。
- `qwen3.py` 是全书公共地基（模型 + 分词器 + 权重加载），另有两个工程变体 `qwen3_optimized.py`（SDPA 优化）和 `qwen3_batched.py`（批量推理）。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：环境搭建与首次运行——安装依赖（pip/uv）、理解 CPU/GPU 硬件要求，并跑通第一个推理脚本 `ch02/05_use_model/generate_simple.py`，亲眼看到 0.6B 模型生成文字。
- **提前预习**：如果想先睹模型真容，可以浏览 [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) 开头的 `QWEN_CONFIG_06_B` 配置字典——第 2 单元将从它开始逐层拆解模型架构。
- **通读材料**：README 的 Bonus Material 清单（L118-L149）列出了全部附加目录，建议花五分钟扫一遍，建立"以后去哪找东西"的印象。

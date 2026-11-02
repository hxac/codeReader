# 第 2 讲　环境搭建与首次运行

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `pip` 或 `uv` 两条官方推荐路线之一，为本项目搭好一个可用的 Python 环境，并确认安装的 PyTorch 版本满足项目约束（`torch>=2.10,<3`）。
2. 说出项目的 Python 版本要求（`>=3.10,<3.15`）、CPU / MPS / CUDA / XPU 四类设备的含义，以及「第 2~4 章 CPU 即可、第 5~6 章建议 GPU」这一章节级硬件建议。
3. 独立运行仓库自带的第一个推理脚本 `ch02/05_use_model/generate_simple.py`，读懂它打印的环境信息横幅和流式生成文本。
4. 当下载模型权重或编译加速出错时，知道去 `troubleshooting.md` 的哪个章节、按什么顺序排查。

本讲是全手册第一次「让代码真的跑起来」的一讲，不深入模型内部——模型架构是单元二的主题，生成函数细节是单元三的主题。

## 2. 前置知识

本讲需要的概念都很基础，逐一用大白话解释：

- **Python 版本**：项目用 `python --version` 查看。本项目要求 3.10（含）到 3.15（不含）之间，作者写书时用的是 3.12。
- **虚拟环境（virtual environment）**：一个隔离的「Python 包安装空间」。不同项目依赖不同版本的包，装在各自的虚拟环境里互不干扰。`pip` 路线需要你手动创建（如 `python -m venv .venv`）；`uv` 路线会在首次运行时自动创建一个 `.venv` 目录。
- **pip 与 uv**：`pip` 是 Python 自带的包管理器；`uv` 是近年流行的更快替代品，能自动管理虚拟环境和锁文件（`uv.lock`），本项目作者本人推荐 `uv`。
- **PyTorch（import 名为 `torch`）**：深度学习框架，本项目的模型前向计算、张量操作全部基于它。它既能在 CPU 上跑，也能在 NVIDIA CUDA GPU、Apple 芯片的 MPS、Intel GPU 的 XPU 上跑。
- **版本约束符号**：`torch>=2.10,<3` 表示「大于等于 2.10.0 且小于 3.0」，即允许 2.10、2.11……但不允许 3.0。
- **token 与 tokenizer**：模型不直接读文字，而是读「token id」整数序列；tokenizer 负责在文本和 id 之间互转。本讲只需知道脚本里 `tokenizer.encode(prompt)` 把提示词变成 id 序列即可，细节在 u2-l6。
- **权重文件（.pth）**：把训练好的模型参数（一堆数字张量）存成的文件。本项目用的 0.6B 参数 Qwen3 权重需要先下载到本地，脚本才能加载。

如果你还没读过 u1-l1（项目定位与仓库结构），建议先读：本讲假设你知道仓库分为 `chXX` 章节目录、`reasoning_from_scratch` 包和 `tests` 三部分。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pyproject.toml](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml) | 项目元数据：Python 版本要求、依赖列表、可选依赖、uv 配置 |
| [requirements.txt](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/requirements.txt) | 极简依赖清单，供 `pip install -r` 一键安装 |
| [ch02/02_setup-tips/python-instructions.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/02_setup-tips/python-instructions.md) | 作者写的官方安装指南：pip 与 uv 两种路线的逐步命令 |
| [README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/README.md) | 仓库主页，本讲用到其中的 Hardware Requirements 一节 |
| [ch02/05_use_model/generate_simple.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/generate_simple.py) | 本讲主角：约 130 行的独立推理脚本 |
| [ch02/05_use_model/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/README.md) | 该目录三个脚本（generate_simple / chat / chat_multiturn）的用法说明与示例输出 |
| [reasoning_from_scratch/ch02.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py) | 第 2 章配套模块：`get_device`、生成函数、统计函数（本讲只用其中三个） |
| [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) | 模型与分词器实现；本讲只涉及 `download_qwen3_small` 这一个下载入口 |
| [troubleshooting.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/troubleshooting.md) | 仓库根目录的故障排查指南 |

## 4. 核心概念与源码讲解

### 4.1 依赖安装与环境要求

#### 4.1.1 概念说明

一个 Python 项目的「环境」由三件事构成：**哪个 Python 解释器**、**装了哪些包及版本**、**跑在哪类硬件上**。本项目对这三件事的官方答案分散在三个文件里：

- `pyproject.toml`：权威声明（Python 版本区间、7 个核心依赖、可选依赖、uv 配置）。
- `requirements.txt`：给 `pip` 用户的极简清单（只有 5 行）。
- `ch02/02_setup-tips/python-instructions.md`：手把手的安装命令。

为什么有两份依赖清单？因为服务两类读者：`uv` 用户直接读 `pyproject.toml`（配合 `uv.lock` 锁文件自动解析）；`pip` 用户用一条 `pip install -r` 命令拉起最小可运行集合。`requirements.txt` 比 `pyproject.toml` 的依赖更少——它不含 `jupyterlab`、`matplotlib`、`nbformat`，只保证「能跑 Python 脚本」；要跑教学 notebook 还需另装 jupyterlab。

硬件方面，README 明确承诺：主线章节代码面向消费级硬件设计，**第 2~4 章 CPU 和 GPU 都能良好运行，第 5、6 章若想复现书中结果建议使用 GPU**。也就是说，本讲的首次运行在任何现代电脑上都可行，只是速度不同。

#### 4.1.2 核心流程

两条安装路线的决策与步骤：

```text
选择路线
├── 路线 A：pip（处处可用，经典）
│   1. python --version           # 确认 3.10 ~ 3.14
│   2. python -m venv .venv       # 创建虚拟环境
│   3. 激活虚拟环境（source .venv/bin/activate 或 .venv\Scripts\activate）
│   4. pip install -r requirements.txt   # 或指南中的 GitHub raw URL 版本
│   5. （可选）pip install jupyterlab    # 要跑 notebook 才需要
│
└── 路线 B：uv（作者推荐，更快）
    1. 安装 uv（macOS/Linux 用 curl 脚本，Windows 用 PowerShell 脚本）
    2. git clone 本仓库并 cd 进去
    3. uv python pin 3.13 && uv sync   # 固定 Python 版本并按锁文件安装
    4. 之后所有命令用 uv run 前缀：uv run python script.py
```

安装完成后统一做一步**验收**：

```bash
python -c "import torch; print(torch.__version__)"   # 期望输出 ≥ 2.10.0 且 < 3
```

#### 4.1.3 源码精读

**① 权威约束：pyproject.toml**

[pyproject.toml:L23-L33](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L23-L33) 声明了 Python 版本区间和 7 个核心依赖。注意第 27 行的注释直接告诉我们：`2.10.0` 是作者写书时使用的版本，这就是学习目标里「确认 torch 版本满足要求」的判断基准。

```toml
requires-python = ">=3.10,<3.15"

dependencies = [
    "jupyterlab>=4.4.7",
    "torch>=2.10,<3",  # 2.10.0 is the version originally used in the book
    "tokenizers>=0.21.2",
    ...
]
```

此外还有两组「非核心」依赖，本讲不需要安装，但要知道它们的存在，免得以后跑附录代码时报 ImportError：

- [pyproject.toml:L46-L56](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L46-L56)：`dev` 依赖组（pytest、ruff、transformers 等），跑测试和二次开发才需要。
- [pyproject.toml:L58-L62](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L58-L62)：`extra` 可选依赖（`datasets` 供附录 F 的 MMLU 用；`chainlit` 供附录 G 聊天界面用，且标记了 `python_version < '3.14'`，因为 Chainlit 尚不支持 3.14）。

**② pip 路线的最小清单：requirements.txt**

[requirements.txt:L1-L5](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/requirements.txt#L1-L5) 只有 5 行：

```text
reasoning-from-scratch >= 0.1.2
setuptools>=61.0
sympy>=1.14.0  # For verifier in ch03
torch >= 2.10, < 3
tokenizers >= 0.21.2
```

关键点在第 1 行：pip 路线安装的是 **PyPI 上发布的 `reasoning-from-scratch` 包**（版本 ≥ 0.1.2），不是你本地检出的这份代码。跑仓库脚本时 `import reasoning_from_scratch` 能成功，靠的是这个已发布包。如果你希望直接使用本地仓库里的最新代码（例如你打算改源码做实验），可以在仓库根目录执行 `pip install -e .`（可编辑安装）——这依托 [pyproject.toml:L70-L72](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L70-L72) 的打包配置（`include = ["reasoning_from_scratch", ...]`）。（此命令属于通用 setuptools 用法，非仓库文档原文，属示例命令。）

**③ 官方安装指南：python-instructions.md**

Python 版本建议在 [ch02/02_setup-tips/python-instructions.md:L16-L24](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/02_setup-tips/python-instructions.md#L16-L24)：作者用 3.12 写书，3.10~3.14 都可以，前提是该版本被 PyTorch 支持；PyTorch 通常落后最新 Python 版本几个月，别抢鲜用刚发布的 Python。

pip 一键安装命令在 [ch02/02_setup-tips/python-instructions.md:L32-L36](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/02_setup-tips/python-instructions.md#L32-L36)：

```bash
pip install -r https://raw.githubusercontent.com/rasbt/reasoning-from-scratch/refs/heads/main/requirements.txt
```

（在本地仓库里直接 `pip install -r requirements.txt` 效果相同。）

uv 路线的核心步骤在 [ch02/02_setup-tips/python-instructions.md:L81-L102](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/02_setup-tips/python-instructions.md#L81-L102)：clone 仓库、`cd` 进入后，`uv` 会在首次运行脚本或打开 Jupyter 时**自动**创建 `.venv` 并安装 `pyproject.toml` 里声明的依赖；仓库没有提交 `.python-version` 文件，想固定版本就 `uv python pin 3.13` 再 `uv sync`。

运行命令统一加 `uv run` 前缀（[ch02/02_setup-tips/python-instructions.md:L121-L127](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/02_setup-tips/python-instructions.md#L121-L127)）：`uv run jupyter lab`、`uv run python script.py`。

锁文件细节值得知道：[ch02/02_setup-tips/python-instructions.md:L130-L143](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/02_setup-tips/python-instructions.md#L130-L143) 说明仓库提交的 `uv.lock` 固定用 PyTorch 2.10.0（跨操作系统通用）；想升级本地锁文件里的 torch 用 `uv lock --upgrade-package torch`，想还原用 `git restore uv.lock && uv sync`。

**④ 章节级硬件建议：README.md**

[README.md:L104-L109](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/README.md#L104-L109) 是硬件问题的权威答案：代码会**自动利用可用 GPU**；第 2~4 章（生成、评测、推理时扩展）CPU 和 GPU 都运行良好；第 5、6 章（自我精炼、GRPO 训练）想复现书中结果建议 GPU。

#### 4.1.4 代码实践

**实践：搭好环境并通过 torch 版本验收**

1. **实践目标**：建立本项目专属虚拟环境，安装依赖，验证 torch 版本落在 `[2.10, 3)` 区间。
2. **操作步骤**（pip 路线，在仓库根目录执行）：
   ```bash
   python --version                       # 确认 3.10 ~ 3.14
   python -m venv .venv
   source .venv/bin/activate              # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   python -c "import torch; print(torch.__version__)"
   ```
   uv 路线等价操作：`uv sync` 之后 `uv run python -c "import torch; print(torch.__version__)"`。
3. **需要观察的现象**：安装过程会下载 torch 等包（体积较大，耐心等待）；最后一行应打印一个 `2.10.x`（或更高 2.x）的版本号。
4. **预期结果**：打印值满足 `>=2.10` 且 `<3`。如果你的输出低于 2.10（常见于复用了装过旧 torch 的环境），说明虚拟环境未生效或需要升级 torch。
5. 本讲义无法替你执行安装，以上为**待本地验证**步骤。

#### 4.1.5 小练习与答案

**练习 1**：`torch>=2.10,<3` 允许安装 torch 3.1 吗？允许 2.10.0 吗？
**答案**：不允许 3.1（违反 `<3`）；允许 2.10.0（`>=` 含等号）。

**练习 2**：为什么附录 G（Chainlit 聊天界面）作者建议用 Python 3.13 而不是 3.14？
**答案**：Chainlit 目前不支持 Python 3.14，`pyproject.toml` 的 extra 依赖里也显式标记了 `chainlit>=2.8.3; python_version < '3.14'`（[pyproject.toml:L58-L62](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L58-L62)）。

**练习 3**：`pip install -r requirements.txt` 之后，`import reasoning_from_scratch` 用的是你本地仓库的代码吗？
**答案**：不是。requirements.txt 第 1 行安装的是 PyPI 发布版 `reasoning-from-scratch >= 0.1.2`。想用本地检出的源码需 `pip install -e .`，或改用 uv 路线（`uv run` 安装的是当前工作区项目本身）。

### 4.2 第一个推理脚本：generate_simple.py

#### 4.2.1 概念说明

`ch02/05_use_model/generate_simple.py` 是仓库提供的「最少装饰」独立推理脚本：**一次命令行调用，完成下载权重 → 加载模型 → 流式生成 → 打印统计**的全流程。它存在的意义是让你不打开 notebook 也能快速试模型——换 prompt、换设备、切换 base/reasoning 两种权重变体，都只是换个命令行参数。

脚本用到三个来自 `reasoning_from_scratch` 包的函数，本讲先认识签名，深读留到单元三：

- `get_device()`：自动探测可用计算设备；
- `generate_text_basic_stream_cache()`：带 KV 缓存的流式贪心生成器；
- `generate_stats()`：打印耗时、吞吐（tokens/sec）和显存峰值。

还有一个来自 `qwen3.py` 的 `download_qwen3_small()`：首次运行时自动从 Hugging Face 下载权重和分词器到本地 `qwen3/` 目录（深入讲解在 u1-l3）。

#### 4.2.2 核心流程

脚本的执行流程可以画成一条直线：

```text
解析命令行参数 (argparse)
        │
        ▼
确定 device：--device 显式指定，否则 get_device() 自动探测
        │
        ▼
按 --reasoning 分流：
  ├─ base 变体      → 下载 qwen3-0.6B-base.pth + tokenizer-base.json
  │                    分词器：不套聊天模板
  └─ reasoning 变体 → 下载 qwen3-0.6B-reasoning.pth + tokenizer-reasoning.json
                       分词器：套聊天模板 + thinking 标签
        │
        ▼
构造 Qwen3Model → torch.load 权重 → load_state_dict → model.eval()
（可选 --compile：torch.compile 包装）
        │
        ▼
确定 prompt（--prompt 或默认值）→ tokenizer.encode → 张量化
        │
        ▼
打印环境信息横幅（torch 版本 / device / cache / compile / reasoning）
        │
        ▼
for token in generate_text_basic_stream_cache(...):
        逐 token decode 并实时 print（流式输出）
        │
        ▼
generate_stats：总耗时 + tokens/sec +（GPU 时）峰值显存
```

#### 4.2.3 源码精读

**① 命令行参数：五个旋钮**

[generate_simple.py:L27-L58](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/generate_simple.py#L27-L58) 用 `argparse` 定义了 5 个参数：`--device`（不传则自动探测）、`--max_new_tokens`（默认 2048）、`--compile`（开关型，默认关闭）、`--reasoning`（开关型，默认用 base 变体）、`--prompt`（不传则按变体给默认题目）。

```python
parser.add_argument(
    "--prompt",
    type=str,
    default=None,
    help=("Use a custom prompt. If not explicitly provided, uses the following defaults: "
          "'Explain large language models in a single sentence.' for the base model, and "
          "'Find all c in Z_3 such that Z_3[x]/(x^2 + c) is a field.' for the reasoning model.")
)
```

**② 设备选择与自动探测**

[generate_simple.py:L60-L61](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/generate_simple.py#L60-L61)：显式传了 `--device` 就用 `torch.device(args.device)`，否则交给 `get_device()`。

`get_device` 的实现在 [reasoning_from_scratch/ch02.py:L10-L39](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L10-L39)，它按固定优先级探测并打印一句人话：

```python
def get_device(enable_tensor_cores=True):
    if torch.cuda.is_available():
        device = torch.device("cuda")
        print("Using NVIDIA CUDA GPU")
        ...
    elif torch.backends.mps.is_available():
        device = torch.device("mps")
        print("Using Apple Silicon GPU (MPS)")
    elif torch.xpu.is_available():
        device = torch.device("xpu")
        print("Using Intel GPU")
    else:
        device = torch.device("cpu")
        print("Using CPU")
```

四类设备按优先级排列：NVIDIA CUDA GPU → Apple 芯片 MPS → Intel GPU XPU → CPU 兜底。你在控制台看到的第一行输出（如 `Using CPU`）就是它打印的。CUDA 分支里还有一段按 PyTorch 版本开启 TF32 加速的代码（`>= (2, 11)` 用新 API，否则用旧 API），那是性能优化话题，u3-l1 再细讲。

**③ base 与 reasoning 两条加载路径**

[generate_simple.py:L63-L82](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/generate_simple.py#L63-L82) 是一个 if/else：两条路径都是「下载文件 → 定位文件路径 → 构造分词器」，差别在文件名和分词器三个布尔参数。reasoning 路径设置了 `apply_chat_template=True, add_generation_prompt=True, add_thinking=True`，base 路径全部为 `False`——这呼应 u1-l1 讲过的「基座模型 vs 推理模型」：同一架构，不同权重、不同对话包装。

`download_qwen3_small` 的真身在 [reasoning_from_scratch/qwen3.py:L440-L457](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L440-L457)：它维护一个「变体 → 文件名」映射，主 URL 指向 Hugging Face 的 `rasbt/qwen3-from-scratch` 仓库，另有 Backblaze 备用 URL，逐个调用 `download_file` 下载。脚本传入的 `out_dir="qwen3"` 是相对路径——**所以务必在仓库根目录运行脚本**，权重才会落在根目录的 `qwen3/` 文件夹里。

**④ 模型加载三件套**

[generate_simple.py:L84-L91](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/generate_simple.py#L84-L91)：

```python
model = Qwen3Model(QWEN_CONFIG_06_B)
state = torch.load(model_path, map_location=device)
model.load_state_dict(state)
model.to(device)
model.eval()
```

四步是 PyTorch 标准套路：按配置实例化空模型 → 从 `.pth` 读出参数字典 → 灌入模型 → 搬到目标设备并切到推理模式（`eval()` 关闭 dropout 等）。`QWEN_CONFIG_06_B` 就是 0.6B 模型的配置字典，u2-l1 会逐字段拆解。

**⑤ 环境信息横幅**

[generate_simple.py:L106-L114](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/generate_simple.py#L106-L114) 打印五行分隔线包围的信息：torch 版本、设备、是否用缓存（固定 True）、是否 compile、是否 reasoning 变体。**实践任务要求记录的「设备类型」就在这里**。`ch02/05_use_model/README.md` 记录了一份真实示例输出（[ch02/05_use_model/README.md:L25-L41](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/README.md#L25-L41)），其中显示 `torch: 2.7.1`——那是作者早期记录的截图，当前锁文件已固定 2.10.0，你跑出来的版本号不同是正常的。

**⑥ 流式生成循环与统计**

[generate_simple.py:L116-L131](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/generate_simple.py#L116-L131)：对生成器函数 `generate_text_basic_stream_cache` 逐 token 迭代，每个 token 立刻 `decode` 并 `print(..., end="", flush=True)` 实时上屏——这就是「流式」的含义。结束后调用 `generate_stats` 输出统计。

生成器本体在 [reasoning_from_scratch/ch02.py:L116-L141](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L116-L141)：每步取 logits 的 `argmax`（贪心，永远选概率最大的 token），遇到 EOS 提前停止，并用 KV 缓存避免重复计算前文（`model(next_token, cache=cache)` 每步只喂新 token）。

[reasoning_from_scratch/ch02.py:L173-L199](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L173-L199) 的 `generate_stats` 打印总耗时和吞吐：

\[ \text{吞吐（tokens/sec）} = \frac{\text{生成的 token 总数}}{\text{end\_time} - \text{start\_time}} \]

并在 CUDA/XPU 后端可用时额外打印峰值显存；若张量不在该后端上还会发出警告，提醒统计可能为 0。

#### 4.2.4 代码实践

**实践：跑通第一次生成，并换一个自己的 prompt**

1. **实践目标**：成功运行 `generate_simple.py`，记录设备类型、生成文本和吞吐；再用自己的 prompt 复跑一次。
2. **操作步骤**（在仓库根目录，环境已按 4.1 搭好）：
   ```bash
   # pip 路线
   python ch02/05_use_model/generate_simple.py --max_new_tokens 128
   # uv 路线（等价）
   uv run ch02/05_use_model/generate_simple.py --max_new_tokens 128

   # 换自己的 prompt（推荐用命令行参数，不改源码）
   python ch02/05_use_model/generate_simple.py \
       --prompt "Explain what a token is in one sentence." \
       --max_new_tokens 128
   ```
   首次运行会先自动下载分词器和约 0.6B 参数的权重文件到 `qwen3/` 目录，请耐心等待；再次运行会直接复用本地文件。
3. **需要观察的现象**：
   - 第一行设备提示（`Using CPU` / `Using Apple Silicon GPU (MPS)` / `Using NVIDIA CUDA GPU` 之一）；
   - 首次运行时的文件下载进度提示，二次运行时的 `✓ ... already up-to-date` 提示；
   - 五行环境横幅中的 `device`、`compile`、`reasoning` 值；
   - 文本逐字「打字机式」出现在终端（流式输出）；
   - 末尾的 `Time: X sec` 与 `tokens/sec`。
4. **预期结果**：base 变体默认 prompt（"Explain large language models in a single sentence."）应生成一句关于大语言模型的英文描述，风格可对照 `ch02/05_use_model/README.md` 记录的示例。CPU 上吞吐明显低于 GPU 属正常现象。具体耗时与文本内容因设备与版本而异，**待本地验证**。
5. 若下载失败：直接进入 4.3 的排查流程，不要盲目重试。

#### 4.2.5 小练习与答案

**练习 1**：你的机器有 NVIDIA GPU，但想测试纯 CPU 性能，怎么跑？
**答案**：加 `--device cpu`。脚本第 60~61 行会优先采用显式指定的设备，跳过 `get_device()` 的自动探测。

**练习 2**：`--reasoning` 与默认 base 变体相比，脚本的哪几处行为不同？
**答案**：三处——(1) 下载的文件不同（`qwen3-0.6B-reasoning.pth` + `tokenizer-reasoning.json` vs base 版）；(2) 分词器参数不同（reasoning 开启 `apply_chat_template`、`add_generation_prompt`、`add_thinking`）；(3) 默认 prompt 不同（reasoning 默认是一道抽象代数题，base 默认让模型用一句话解释 LLM）。见 [generate_simple.py:L63-L100](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/generate_simple.py#L63-L100)。

**练习 3**：为什么建议在仓库根目录运行这个脚本？
**答案**：脚本调用 `download_qwen3_small(..., out_dir="qwen3")`，`out_dir` 是相对当前工作目录的路径。在根目录运行，权重和分词器会统一落在根目录的 `qwen3/` 下，供本讲及后续章节脚本复用；在其他目录运行则会在那里另建 `qwen3/`，造成重复下载。

### 4.3 常见问题排查：troubleshooting.md

#### 4.3.1 概念说明

仓库根目录的 `troubleshooting.md` 是作者维护的「急救手册」，汇总了读者最常踩的坑。它按章节组织：JupyterLab 滚动 bug、第 2 章的文件下载问题与 `torch.compile` 编译错误、第 6 章的 checkpoint 损坏问题。本讲运行 `generate_simple.py` 可能撞上的正是前两类：**下载失败**和（若开了 `--compile` 的）**编译失败**。

排查的总原则：先判断问题属于「环境问题」还是「代码问题」。作者在文档里反复强调一个事实——本书代码经过大量读者验证，报错 mentioning SSL、证书、代理、C++ 头文件时，几乎总是本机环境（网络、代理、编译器）的问题，而不是仓库代码的问题。

#### 4.3.2 核心流程

首次运行出问题时的排查决策树：

```text
运行 generate_simple.py 出错
│
├─ 卡在/失败于「下载权重」
│   ├─ 第 1 步：用浏览器手动打开 HF 文件页（troubleshooting.md 列出了全部 URL）
│   │    └─ 浏览器也打不开 → 网络/防火墙问题，非代码问题
│   ├─ 第 2 步：看报错关键字
│   │    ├─ SSL / CERTIFICATE_VERIFY_FAILED / ProxyError
│   │    │    → 公司/校园网代理拦截 HTTPS：找 IT 放行，
│   │    │      或 pip install pip-system-certs 用系统证书
│   │    └─ 分词器能下、.pth 下不了 → 代理可能拦截大文件或 .pth 后缀
│   └─ 第 3 步：仍无解 → 到文档指定的 Discussion #145 发帖
│
├─ 加了 --compile 后编译报错
│   ├─ Linux: InductorError: CppCompileError（缺 Python.h）
│   │    → 装 python3.x-dev + build-essential，或关闭 cpp_wrapper
│   ├─ Windows: fatal error C1083（缺 algorithm / omp.h）
│   │    → 升级 PyTorch，或退而求其次 backend="eager"/"aot_eager"
│   └─ 重要：torch.compile 非必需，可直接去掉 --compile 跳过整个问题
│
└─ JupyterLab 笔记本滚动异常
    → Settings → Settings Editor → Windowing mode 改为 "none"
```

#### 4.3.3 源码精读

**① File Download Issues：先核对 URL**

[troubleshooting.md:L22-L34](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/troubleshooting.md#L22-L34) 列出了所有被下载文件的 Hugging Face 地址：第 2 章的 base/reasoning 权重与分词器（`rasbt/qwen3-from-scratch`）、第 7 章 GRPO checkpoint、第 8 章蒸馏 checkpoint。排查第一步永远是**把 URL 贴进浏览器**，区分「我下不了」和「文件不存在」。

**② SSL / 代理 / 证书错误**

[troubleshooting.md:L37-L46](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/troubleshooting.md#L37-L46) 针对报错含 `SSL`、`CERTIFICATE_VERIFY_FAILED`、`ProxyError` 的场景：常见于公司/学校机器上 VPN、代理、防火墙或杀毒软件拦截 HTTPS 流量。文中给出四条对策：浏览器验证 URL；注意「分词器能下载而 `.pth` 不能」往往意味着代理拦截大文件或 `.pth` 扩展名；请 IT 放行或信任证书；有读者用 `pip install pip-system-certs`（让 Python 使用操作系统证书库）成功解决。

**③ torch.compile 编译错误（仅当你加 --compile）**

Linux 下若见 `InductorError: CppCompileError` 且日志含 `Python.h: No such file or directory`（[troubleshooting.md:L49-L80](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/troubleshooting.md#L49-L80)），说明缺 Python C++ 头文件：可 `sudo apt-get install -y python3.12-dev build-essential`，或在编译前设置 `torch._inductor.config.cpp_wrapper = False`。

Windows CPU 下若见 `fatal error C1083` 缺 `algorithm` 或 `omp.h`（[troubleshooting.md:L84-L140](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/troubleshooting.md#L84-L140)）：升级 PyTorch 或改用 `torch.compile(model, backend="eager")` 这类回退后端。文档同时强调：**torch.compile 对本书不是必需的，跳过该节完全不影响学习**——对应到本脚本，就是别加 `--compile` 即可。

**④ 面向未来的预告**

[troubleshooting.md:L145-L152](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/troubleshooting.md#L145-L152) 讲第 6 章训练时 `Ctrl+C` 打断 checkpoint 写入会导致文件损坏（比正常的约 1.5 GB 明显偏小）——现在只需留有印象，学到 u7-l4 时会回来对照。

#### 4.3.4 代码实践

**实践：验证下载缓存行为，并演练一次排查路径**

1. **实践目标**：亲眼确认「首次下载 / 二次复用」两种行为，并掌握用浏览器核对下载源的方法。
2. **操作步骤**：
   - 按上一节的实践跑两次 `generate_simple.py`，分别记录两次运行中与下载相关的输出行；
   - 打开文件管理器查看 `qwen3/` 目录中生成了哪些文件；
   - 用浏览器访问 [https://huggingface.co/rasbt/qwen3-from-scratch/tree/main](https://huggingface.co/rasbt/qwen3-from-scratch/tree/main)，核对目录中的文件名与本地一致。
3. **需要观察的现象**：第一次运行出现下载进度；第二次运行出现 `✓ qwen3/qwen3-0.6B-base.pth already up-to-date` 字样（该样式见 `ch02/05_use_model/README.md` 的示例输出）；浏览器能正常列出 4 个第 2 章文件。
4. **预期结果**：`qwen3/` 目录含 `qwen3-0.6B-base.pth` 与 `tokenizer-base.json` 两个文件；二次运行不再发生网络下载。若你的网络环境连 HF 主站失败，可观察到脚本回退到备用源下载（对应 `download_qwen3_small` 的 backup_url 机制，u1-l3 详述）。**待本地验证**。
5. 若你恰好遇到下载失败：按 4.3.2 的决策树逐步执行，并把每一步的现象记进实验记录——这个记录本身就是本讲综合实践的一部分。

#### 4.3.5 小练习与答案

**练习 1**：tokenizer 下载成功，但 `.pth` 权重文件下载失败，最可能的原因是什么？
**答案**：本地网络/代理对大文件或 `.pth` 扩展名做了拦截。[troubleshooting.md:L44](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/troubleshooting.md#L44) 明确列了这一现象，应请 IT 放行或检查代理对文件大小/后缀的策略。

**练习 2**：Linux 上加 `--compile` 报 `CppCompileError: Python.h: No such file or directory`，给出两种解法。
**答案**：(1) 安装对应版本的头文件与工具链：`sudo apt-get install -y python3.12-dev build-essential`；(2) 编译前设 `torch._inductor.config.cpp_wrapper = False` 再 `torch.compile(model)`。最省事的第三种：不加 `--compile`，编译加速非必需。

**练习 3**：为什么第二次运行脚本会显示 `already up-to-date` 而不是重新下载？
**答案**：`download_file`（被 `download_qwen3_small` 调用）在下载前会检查目标路径：文件已存在且完整就跳过下载、打印 up-to-date 提示。这是一种本地缓存策略，避免每次运行重复下载上百 MB 的权重。实现细节在 `reasoning_from_scratch/utils.py`，u1-l3 精读。

## 5. 综合实践

**任务：建立你的「环境档案」，并完成首次生成的完整记录。**

把本讲三个模块串成一份可留存的实验档案（建议放在自己的笔记里，不要写进仓库）：

1. **环境部分**（对应 4.1）：
   - 记录你选择的路线（pip / uv）、Python 版本、`python -c "import torch; print(torch.__version__)"` 的输出；
   - 对照 [README.md:L104-L109](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/README.md#L104-L109)，写下你的设备属于哪一档（CPU / MPS / CUDA / XPU），并判断它足以跑哪些章节。
2. **首次运行部分**（对应 4.2）：
   - 运行 `python ch02/05_use_model/generate_simple.py --max_new_tokens 128`（或 `uv run` 版本），抄录：设备提示行、五项环境横幅、生成文本、`Time` 与 `tokens/sec`；
   - 换一个自己拟定的 prompt 再跑一次，记录两次输出差异（提示：base 模型没有经过对话微调，输出风格可能不稳定，这本身就是有效观察）。
3. **排查部分**（对应 4.3）：
   - 若一切顺利：二次运行验证 `already up-to-date` 缓存行为，并用浏览器核对 HF 文件页；
   - 若遇下载失败：按 4.3.2 决策树逐步执行，把「现象 → 判断 → 动作 → 结果」四列记入档案。

**验收标准**：档案里能回答三个问题——「我的 torch 版本合规吗」「我的脚本跑在哪类设备上、吞吐多少」「出下载问题时我的第一步动作是什么」。这份档案在单元三对比性能时会再次派上用场。

## 6. 本讲小结

- 项目环境约束：Python `>=3.10,<3.15`（写书用 3.12），核心依赖 7 个，其中 `torch>=2.10,<3` 是最关键的版本约束；pip 路线装的是 PyPI 发布包，uv 路线装的是本地工作区项目。
- 硬件要求分层：第 2~4 章 CPU 可跑，第 5~6 章建议 GPU；`get_device()` 按 CUDA → MPS → XPU → CPU 的优先级自动探测并打印结果。
- `generate_simple.py` 是一条完整的最小推理流水线：argparse 参数 → 设备选择 → 按 `--reasoning` 分流下载/加载 → 环境横幅 → 流式贪心生成（KV 缓存）→ 吞吐统计。
- 流式生成的体感：token 逐个打印上屏，`generate_stats` 用 \( \text{tokens/sec} = \frac{\text{token 数}}{\text{耗时}} \) 报告吞吐。
- 排查心法：下载问题先拿浏览器核对 Hugging Face URL；SSL/代理错误是环境问题不是代码问题；`torch.compile` 出错可以直接跳过，不影响任何主线内容。

## 7. 下一步学习建议

下一讲 **u1-l3（权重与数据下载机制）** 将钻进本讲只「用了」没「读」的部分：`utils.py` 的 `download_file` 如何实现分块下载、进度显示、主备 URL 切换与缓存跳过，以及 `qwen3.py` 里三个下载入口函数的分工。学完它，你对「第一次运行时终端里都发生了什么」就有完整答案了。

如果你已迫不及待想理解模型本身，也可以跳到 **u2-l1（Qwen3 模型总体架构）**，但建议按路线先完成单元一。后续 **u3-l1** 会深读 `get_device` 的 TF32 分支，**u3-l2** 会逐行对比四代生成函数——本讲跑过的 `generate_text_basic_stream_cache` 到那时会彻底拆开。

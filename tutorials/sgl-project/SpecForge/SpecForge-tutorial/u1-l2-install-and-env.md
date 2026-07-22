# 讲义 u1-l2：安装与环境准备

## 1. 本讲目标

上一篇（[u1-l1](./u1-l1-project-overview.md)）我们建立了全局认知：SpecForge 是 SGLang 团队的投机解码草稿模型训练框架，所有方法共用一个类型化入口 `specforge train`，并且不绑死 NVIDIA——同时覆盖 CUDA、ROCm、Ascend 三类加速器。本讲就把「认知」落到「能跑」。

读完本讲，你应当能够：

- 用 `uv` 或 `pip` 两种方式从零安装 SpecForge，并知道为什么官方更推荐从源码安装。
- 看懂 [pyproject.toml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/pyproject.toml) 里声明的依赖、控制台脚本入口（`specforge` 命令）以及「可选扩展（extras）」。
- 说明 CUDA / ROCm / Ascend NPU 三类加速器在安装步骤上的差异，并能为自己的硬件选择正确的安装路径。
- 安装完成后运行 `specforge --help`，确认 `train` / `export` / `benchmark` 三个子命令可用。

本讲**几乎不需要你懂深度学习的数学**，但需要你对「Python 虚拟环境」和「pip 安装包」有最基本的动手能力。我们会一边讲原理，一边对照真实文件给出可操作的步骤。

---

## 2. 前置知识

承接 [u1-l1](./u1-l1-project-overview.md) 已经建立的几个结论（这里不再重复论证，只点明与安装相关的部分）：

- SpecForge 是一个**Python 包**，包名叫 `specforge`，安装后会注册一个同名命令行工具 `specforge`。
- 它**强依赖 PyTorch（torch）和 SGLang（sglang）**——前者负责训练计算，后者负责在线捕获与服务对接。这两个库本身很大，而且会根据你的硬件（NVIDIA / AMD / 昇腾）选择不同的安装版本。这是本讲「环境差异」一节的根源。
- 它支持三类加速器，所以安装路径不止一条，需要你先判断自己手上的硬件。

如果你对下面几个 Python 工程术语不熟，先花一分钟看懂：

- **虚拟环境（virtual environment）**：一个隔离的 Python 目录，里面装的包不会污染系统 Python。SpecForge 依赖版本很新（如 `torch==2.11.0`），强烈建议在虚拟环境里安装，避免和别的项目打架。
- **PEP 517 / `pyproject.toml`**：现代 Python 项目的「元信息配置文件」。`pip install .` 会读这个文件，知道项目叫什么、依赖什么、怎么构建、注册哪些命令。
- **控制台脚本（console script / entry point）**：安装时自动生成一个命令（比如 `specforge`），它实际指向包里某个 Python 函数（这里是 `specforge.cli:main`）。所以装完之后你直接敲 `specforge` 就能用。
- **wheel index（轮子索引）**：PyTorch 这类含编译产物的库，会为不同操作系统/显卡/CUDA 版本发布不同的「wheel」安装包，存放在不同的下载地址（index）里。`--extra-index-url` 就是告诉 pip「再去这个地址找一找」。
- **可选依赖（extras）**：`pip install 'specforge[fa]'` 这种方括号语法，用来按需安装额外功能（如 flash-attention），不装也不影响核心训练。

> 术语提示：本讲里 **PyPI** 指 Python 官方包仓库（pypi.org），`pip install specforge` 默认从它下载；**源码安装**指先把仓库 `git clone` 下来、再 `pip install .`，能拿到最新代码且便于二次开发。

---

## 3. 本讲源码地图

本讲主要围绕「安装相关」的工程文件展开，不涉及训练逻辑源码：

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| [pyproject.toml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/pyproject.toml) | 项目元信息：包名、Python 版本要求、依赖、控制台入口、可选扩展、动态版本 | 搞清楚「装了什么、装出哪个命令、有哪些可选项」 |
| [version.txt](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/version.txt) | 单独存放版本号，被 `pyproject.toml` 动态读取 | 理解 `dynamic = ["version"]` 的来源 |
| [docs/get_started/installation.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/get_started/installation.md) | 官方安装指南：源码 / PyPI 两种装法 + 三类加速器差异 | 作为动手安装的权威步骤依据 |
| [requirements-rocm.txt](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/requirements-rocm.txt) | AMD ROCm 环境的钉版依赖清单（含 ROCm 专属 wheel index） | 理解 ROCm 为什么不能只用 `pyproject.toml` |
| [docs/basic_usage/training.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md) | 训练指南里的「CUDA, ROCm, and Ascend NPU」一节 | 补充三类硬件运行时的细节（NCCL / HCCL） |
| [specforge/cli.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py) | 控制台入口指向的 `main()` 函数，定义三个子命令 | 验证 `specforge --help` 应该出现的三个子命令 |

记住一条主线：**`pyproject.toml` 声明依赖 → `installation.md` 告诉你怎么装 → 装完 `specforge` 命令由 `cli.py:main` 提供**。下面按这条线拆成三个最小模块。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**pyproject 依赖与 scripts 入口**、**安装指南（源码 / PyPI）**、**三类加速器环境差异（CUDA / ROCm / NPU）**。

---

### 4.1 pyproject 依赖与 scripts 入口

#### 4.1.1 概念说明

安装一个 Python 包，本质上是回答四个问题：

1. **叫什么名字？**（包名，决定 `pip install <名字>`）
2. **需要什么环境？**（Python 版本、操作系统、硬件）
3. **依赖哪些别的库？**（装它时会顺带装什么）
4. **装完能怎么用？**（提供命令行命令，还是只能 `import`）

SpecForge 用 [pyproject.toml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/pyproject.toml) 一次性回答了这四个问题。它是「单一事实来源（single source of truth）」：你只要读懂这一个文件，就知道装 SpecForge 会牵动哪些东西。

这里有一个关键设计：**SpecForge 提供的是一个命令行工具（CLI），而不是一个只能被 `import` 的库**。安装后你会得到一个名为 `specforge` 的命令，它指向 `specforge.cli` 模块里的 `main` 函数。这就是 [u1-l1](./u1-l1-project-overview.md) 里说的「所有方法共用同一个类型化训练入口」在工程上的落点。

#### 4.1.2 核心流程

`pip install .`（或 `uv pip install .`）读到 `pyproject.toml` 后，大致经历这样几步：

```
┌──────────────────────┐
│  读 pyproject.toml    │  ① build-system 用 setuptools 构建
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│  解析 dependencies    │  ② 下载并安装 torch/transformers/sglang 等 18 个依赖
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│  注册 console script  │  ③ 在虚拟环境的 bin/ 下生成 specforge 命令
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│  写入动态版本号        │  ④ 从 version.txt 读出 "0.2.0" 作为包版本
└──────────────────────┘
```

四个要点：

1. **构建后端是 setuptools**，这是最主流的 Python 构建工具，意味着你不需要额外装 Poetry 等工具。
2. **依赖会被自动拉齐到钉死版本**——比如 `torch==2.11.0`、`transformers==5.8.1`、`sglang==0.5.14` 都带 `==`，pip 会精确安装这些版本。这保证了「所有人装出来的环境一致」（可复现性），代价是和你现有项目里的版本可能冲突——**这正是要用虚拟环境的理由**。
3. **`specforge` 命令是免费送你的**：`[project.scripts]` 声明后，pip 会自动生成一个可执行入口，指向 `specforge.cli:main`。
4. **版本号不写死在 pyproject.toml 里**，而是用 `dynamic` 从 `version.txt` 读，方便单独改版本。

#### 4.1.3 源码精读

先看项目元信息与 Python 版本要求（[pyproject.toml:5-12](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/pyproject.toml#L5-L12)）：包名是 `specforge`，**要求 Python ≥ 3.11**（`requires-python = ">=3.11"`），版本号是动态的（`dynamic = ["version"]`）。这意味着你必须用 Python 3.11 或更高版本——这也是官方安装指南里固定写 `uv venv -p 3.11` 的原因。

接着是核心依赖列表（[pyproject.toml:13-32](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/pyproject.toml#L13-L32)）。挑几个关键的看：

| 依赖 | 钉版 | 它在 SpecForge 里干什么 |
| --- | --- | --- |
| `torch` | `==2.11.0` | 训练计算（前向/反向/FSDP/分布式） |
| `transformers` | `==5.8.1` | 加载目标模型与 tokenizer、chat_template |
| `sglang` | `==0.5.14` | 在线捕获（online）时启动推理服务、捕获隐藏状态 |
| `accelerate` | 不钉版 | 分布式启动工具（`set_seed` 等） |
| `pydantic` | 不钉版 | 类型化配置（typed run config）校验 |
| `wandb` / `tensorboard` | 不钉版 | 实验跟踪后端 |
| `yunchang` | 不钉版 | 序列并行（USP / ring attention）实现 |
| `safetensors` | 不钉版 | 检查点读写 |

> 注意三个钉死的「铁三角」`torch==2.11.0` / `transformers==5.8.1` / `sglang==0.5.14`。这三者的版本必须严格匹配，因为 SpecForge 的捕获逻辑会「打补丁」进入 SGLang 内部、读取 transformers 的隐藏状态，版本不一致就会对不上接口。安装时如果 pip 报版本冲突，**不要随意改这三个版本**，而应检查你的虚拟环境是否干净。

再看控制台入口（[pyproject.toml:34-35](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/pyproject.toml#L34-L35)）：

```toml
[project.scripts]
specforge = "specforge.cli:main"
```

这两行就是「`specforge` 命令从哪来」的全部秘密：安装后，pip 会在虚拟环境的 `bin/specforge`（Windows 下是 `Scripts\specforge.exe`）生成一个入口，调用 `specforge.cli` 模块的 `main` 函数。

`main` 函数确实定义在 [cli.py:169](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L169)：

```python
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="specforge")
    sub = parser.add_subparsers(dest="command", required=True)
    train = sub.add_parser("train", help="train a draft model from a typed config")
    ...
    export = sub.add_parser("export", help="materialize a runtime checkpoint as a model directory")
    ...
    benchmark = sub.add_parser("benchmark", help="benchmark a running SGLang server", ...)
```

这里用 `argparse` 注册了**三个子命令**：`train`（训练）、`export`（把检查点物化成模型目录）、`benchmark`（对一个运行中的 SGLang 服务做基准测试）。本讲的最终实践就是验证这三个子命令是否可用。

> 小细节：`add_subparsers(..., required=True)` 意味着**直接敲 `specforge`（不带任何子命令）会报错**，提示 `the following arguments are required: command`。但 `specforge --help` 是有效的——它会打印帮助并列出三个子命令。所以验证安装时要用 `specforge --help`，而不是光敲 `specforge`。

最后看可选扩展与动态版本（[pyproject.toml:40-49](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/pyproject.toml#L40-L49)）：

```toml
[project.optional-dependencies]
data = ["openai"]
dev = ["pre-commit"]
fa = ["flash-attn", "ninja", "packaging"]
liger = ["liger-kernel"]

[tool.setuptools.dynamic]
version = {file = "version.txt"}
```

这四组 extras 的含义：

| extra | 安装语法 | 用途 |
| --- | --- | --- |
| `data` | `pip install 'specforge[data]'` | 装上 `openai`，数据准备脚本可能用到 |
| `dev` | `pip install 'specforge[dev]'` | 开发用，装 `pre-commit`（代码提交前检查） |
| `fa` | `pip install 'specforge[fa]'` | 装 flash-attention（更快注意力，需编译，可选） |
| `liger` | `pip install 'specforge[liger]'` | 装 liger-kernel（融合算子优化，可选） |

而 `version = {file = "version.txt"}` 表示版本号从 [version.txt](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/version.txt) 读取，该文件内容是 `0.2.0`（[version.txt:1](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/version.txt#L1)），所以 `pip show specforge` 会显示 `Version: 0.2.0`。

#### 4.1.4 代码实践

这是一个纯阅读型实践，目标是让你亲手读懂「装了什么」。

1. **实践目标**：从 `pyproject.toml` 里提取 SpecForge 的包名、Python 版本要求、控制台入口和可选扩展。
2. **操作步骤**：
   - 打开项目根目录的 [pyproject.toml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/pyproject.toml)。
   - 找到 `requires-python` 这一行，记下最低 Python 版本。
   - 找到 `[project.scripts]` 段，记下命令名和它指向的函数。
   - 找到 `[project.optional-dependencies]` 段，列出四个 extra 的名字。
3. **需要观察的现象**：你会发现控制台入口 `specforge = "specforge.cli:main"` 正好对应本讲后面要验证的那个命令；可选扩展里 `fa` 和 `liger` 是性能优化项，不装也能训练。
4. **预期结果**：你能写出——包名 `specforge`，Python ≥ 3.11，命令入口 `specforge.cli:main`，四个 extras 为 `data` / `dev` / `fa` / `liger`，版本来自 `version.txt`（当前 `0.2.0`）。
5. 本实践无需运行任何命令，属于「配置阅读型」任务。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `torch`、`transformers`、`sglang` 都用 `==` 钉死版本，而 `pydantic`、`accelerate` 没钉？

> **参考答案**：前三者是 SpecForge「打补丁进 SGLang、读 transformers 隐藏状态」的核心依赖，接口必须严格对齐，任何小版本漂移都可能导致捕获逻辑失效，所以钉死。后者（pydantic/accelerate）是相对通用的工具库，API 稳定，允许 pip 在合理范围内选版本，便于和用户其它依赖共存。

**练习 2**：如果你在 Python 3.10 的环境里执行 `pip install specforge`，会发生什么？

> **参考答案**：因为 `requires-python = ">=3.11"`，pip 会在解析阶段直接拒绝安装，并提示 Python 版本不满足要求。你必须先升级到 Python 3.11 或更高，再创建虚拟环境安装。

---

### 4.2 安装指南：从源码安装与 PyPI 安装

#### 4.2.1 概念说明

SpecForge 提供两种安装方式，对应两类读者：

- **从源码安装（官方推荐）**：先把仓库克隆到本地，再 `pip install .`。好处是拿到最新代码、方便看源码和二次开发，也是本学习手册「结合真实源码」的前提。坏处是要先装 `git` 和构建工具。
- **从 PyPI 安装**：直接 `pip install specforge`。好处是一行命令、省去克隆步骤，适合只想用、不想看源码的用户。PyPI 上的版本可能略落后于仓库主干。

官方在 [installation.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/get_started/installation.md) 里明确把「从源码安装」标为 **recommended**。对学习手册的读者来说，**请选择从源码安装**——后续讲义会大量引用仓库里的真实源码行号，本地有一份代码才能边读边对照。

工具链上，官方推荐用 **`uv`**（一个用 Rust 写的极快 Python 包管理器）而不是传统 `pip`。两者都行，但 `uv` 装依赖（尤其是 torch 这种大包）明显更快。

#### 4.2.2 核心流程

从源码安装的标准五步（依据 [installation.md:9-20](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/get_started/installation.md#L9-L20)）：

```
1. git clone 仓库 + cd 进目录
2. uv venv -p 3.11              ← 用 Python 3.11 建虚拟环境
3. source .venv/bin/activate     ← 激活虚拟环境（Windows 用 .venv\Scripts\activate）
4. uv pip install -v . --prerelease=allow   ← 安装（允许预发布版本）
5. specforge --help              ← 验证命令可用
```

几个关键细节：

- **第 2 步 `uv venv -p 3.11`**：明确指定 Python 3.11，呼应 `requires-python = ">=3.11"`。如果用 `pip` 等价做法是 `python3.11 -m venv .venv`。
- **第 4 步的 `-v`**：verbose，打印详细安装日志，便于排查（torch 等大包下载慢，有日志心里有底）。
- **第 4 步的 `--prerelease=allow`**：允许安装预发布（prerelease）版本。SpecForge 的依赖里可能有尚未正式发布的版本，不加这个 flag，`uv` 默认会跳过预发布包导致装不上。传统 `pip` 默认就允许预发布，所以如果用 `pip install .` 不需要这个 flag。

从 PyPI 安装则只有一行（[installation.md:24-26](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/get_started/installation.md#L24-L26)）：

```bash
pip install specforge
```

#### 4.2.3 源码精读

[installation.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/get_started/installation.md) 把两种装法并列给出。源码安装（推荐）这块（[installation.md:7-20](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/get_started/installation.md#L7-L20)）原文是：

```bash
# git clone the source code
git clone https://github.com/sgl-project/SpecForge.git
cd SpecForge

# create a new virtual environment
uv venv -p 3.11
source .venv/bin/activate

# install specforge
uv pip install -v . --prerelease=allow
```

PyPI 安装（[installation.md:22-26](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/get_started/installation.md#L22-L26)）则是：

```bash
pip install specforge
```

对比两种方式的适用场景：

| 维度 | 从源码安装（推荐） | 从 PyPI 安装 |
| --- | --- | --- |
| 命令 | `uv pip install -v . --prerelease=allow` | `pip install specforge` |
| 代码版本 | 仓库主干最新 | PyPI 上发布的快照（可能略旧） |
| 是否便于读源码 / 二次开发 | 是（本地有完整代码） | 否（装到 site-packages，不易改） |
| 适合人群 | 学习者、开发者、本手册读者 | 只想用、不看源码的用户 |

> 提示：从源码安装时，`pip install .`（注意是一个点）和 `pip install -e .`（editable，「可编辑模式」）有区别。后者把你对源码的修改立即生效，非常适合边改边测；前者是普通安装，改代码后要重装。本手册后续若涉及「改一个参数观察行为」的实践，建议用 `-e .`。官方 ROCm/NPU 文档里就用了 `pip install -e .`（见 4.3 节）。

#### 4.2.4 代码实践

这是本讲的核心动手实践之一：在你的机器上从源码装好 SpecForge。

1. **实践目标**：完成源码安装，让 `specforge` 命令出现在虚拟环境中。
2. **操作步骤**：
   - 确认已安装 `git` 和 `uv`（`uv` 可用 `curl -LsSf https://astral.sh/uv/install.sh | sh` 安装，或 `pip install uv`）。
   - 执行官方五步（见 4.2.2 的代码块）。
   - 安装耗时较长（主要是 torch/sglang 这两个大包），请耐心等待。
3. **需要观察的现象**：安装过程中会看到大量依赖被逐个解析、下载；最后会在 `.venv/bin/` 下生成 `specforge` 这个可执行文件。
4. **预期结果**：执行 `which specforge`（Windows 用 `where specforge`）应指向你的 `.venv/bin/specforge`；执行 `pip show specforge` 应显示 `Version: 0.2.0`。
5. **待本地验证**：实际下载耗时和是否遇到网络问题取决于你的网络与镜像源配置；若下载过慢，可配置国内 PyPI 镜像，但 **torch/sglang 的 ROCm 专属 wheel 必须用指定 index（见 4.3 节），不要随意换源**。本讲作者无法预知你的网络情况，具体耗时待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：官方命令里 `--prerelease=allow` 是给谁用的？如果删掉会怎样？

> **参考答案**：这是给 `uv pip install` 的 flag，允许安装预发布（alpha/beta/rc）版本的依赖包。SpecForge 的某些依赖可能尚未发布正式版，删掉这个 flag 后 `uv` 会拒绝安装预发布包，导致解析失败。如果改用传统 `pip install .`，则 `pip` 默认就允许预发布，无需该 flag。

**练习 2**：学习手册的读者为什么应该选「从源码安装」而不是 `pip install specforge`？

> **参考答案**：因为后续讲义会大量引用仓库里的真实源码行号（永久链接）。从源码安装后本地有一份完整代码，可以边读讲义边打开对应文件、跳到对应行号对照，甚至用可编辑模式（`-e .`）改参数观察行为。PyPI 安装只把包放进 site-packages，不方便阅读和修改。

---

### 4.3 三类加速器环境差异：CUDA / ROCm / Ascend NPU

#### 4.3.1 概念说明

SpecForge 的一个重要承诺是**不绑死 NVIDIA**（见 [u1-l1](./u1-l1-project-overview.md)）。它支持三类加速器：

- **NVIDIA CUDA**：最常见的 NVIDIA GPU。PyTorch 官方 wheel 默认就是 CUDA 版，安装最省心。
- **AMD ROCm**：AMD GPU 的计算平台。PyTorch 为 ROCm 发布**单独的 wheel**，存放在单独的下载地址，所以需要一份专门的依赖清单（`requirements-rocm.txt`）。
- **Ascend NPU**：华为昇腾加速器。需要安装厂商配套的 PyTorch 和 `torch_npu` 包，分布式通信用 HCCL 而非 NCCL。

为什么会有差异？因为 PyTorch 这类库是**带编译产物**的：针对不同硬件编译出的二进制不同，必须从对应的「wheel index」下载。`pyproject.toml` 里写的 `torch==2.11.0` 是通用版本号，pip 会根据你的平台去默认 index 找——在 NVIDIA 上能找到 CUDA 版，但在 AMD/昇腾上就得另外指定 index 或额外包。这就是 ROCm 需要 `requirements-rocm.txt`、NPU 需要 `torch_npu` 的根本原因。

好消息是：**装好之后，三类硬件用的是同一个 `specforge train` 入口和同一份 YAML**（[training.md:370](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L370) 明确说「CUDA and ROCm runs use the same YAML and entry point」）。差异只在「怎么装环境」，不在「怎么用」。

#### 4.3.2 核心流程

三类加速器的安装路径对比：

```
┌─────────────┐   直接 pip install .          ┌──────────────┐
│ NVIDIA CUDA  │ ───────────────────────────▶ │ torch 走默认   │
│ (最省心)      │   pyproject.toml 通用即可      │ CUDA wheel     │
└─────────────┘                                └──────────────┘

┌─────────────┐   ① pip install -r              ┌──────────────────┐
│  AMD ROCm    │      requirements-rocm.txt      │ torch==2.11.0      │
│              │ ─────────────────────────────▶ │ +rocm7.2 (ROCm 版) │
│              │   ② pip install -e .            │ 走 ROCm wheel index│
└─────────────┘                                └──────────────────┘

┌─────────────┐   ① 装厂商版 PyTorch + torch_npu  ┌──────────────────┐
│ Ascend NPU   │ ─────────────────────────────▶ │ torch_npu + HCCL   │
│              │   ② pip install -e .            │ 运行时自动选 NPU    │
└─────────────┘                                └──────────────────┘
```

三类硬件在「通信后端」「设备 API」「在线是否需要兼容服务」上也有差异，一张表概括：

| 维度 | NVIDIA CUDA | AMD ROCm | Ascend NPU |
| --- | --- | --- | --- |
| 安装 | 标准 `pip install .` | 先装 `requirements-rocm.txt` | 先装厂商 PyTorch + `torch_npu` |
| torch wheel | 默认 index | `torch==2.11.0+rocm7.2`（ROCm index） | 厂商配套 torch |
| 设备 API | `torch.cuda` | `torch.cuda`（ROCm 复用 CUDA API） | `torch.npu`（由 `torch_npu` 提供） |
| 分布式通信 | NCCL | NCCL | HCCL |
| 在线捕获服务 | 标准 SGLang | 需 ROCm 兼容的 SGLang | 需 NPU 兼容的 SGLang（外部服务） |
| 离线 consumer | 可独立起步 | 可独立起步（无需目标推理） | 用 SDPA consumer |

#### 4.3.3 源码精读

**NVIDIA CUDA** 的说明最简短（[installation.md:30-34](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/get_started/installation.md#L30-L34)）：标准安装即可，PyTorch 会自己选 CUDA 版本，装好后所有命令都走同一个 `specforge train` 入口。你只需要保证宿主机驱动和装上的 CUDA 版本兼容。

**AMD ROCm** 是本节重点，因为它有一份专属依赖清单。官方步骤（[installation.md:38-44](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/get_started/installation.md#L38-L44)）：

```bash
python -m pip install -r requirements-rocm.txt
python -m pip install -e .
```

注意**顺序**：先装 ROCm 清单（它会把 torch 钉成 ROCm 版），再装 SpecForge 本体。如果反过来，`pyproject.toml` 里的通用 `torch==2.11.0` 可能先被解析成 CUDA 版，后面就乱了。

打开 [requirements-rocm.txt](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/requirements-rocm.txt) 看它和 `pyproject.toml` 有什么不同（[requirements-rocm.txt:1-22](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/requirements-rocm.txt#L1-L22)）：

- 第 2 行 `--extra-index-url https://download.pytorch.org/whl/rocm7.2`：告诉 pip 去 PyTorch 的 **ROCm 7.2** 专属 index 下载 wheel。
- 第 5 行 `torch==2.11.0+rocm7.2`：注意后缀 `+rocm7.2`，这就是 ROCm 版 torch 的标记，和 `pyproject.toml` 里通用的 `torch==2.11.0` 区分开。
- 第 18 行 `sglang[all]==0.5.14`：带了 `[all]` extra，比 `pyproject.toml` 里的 `sglang==0.5.14` 多拉一批 SGLang 附加依赖。
- 此外还显式列了 `pre-commit`、`setuptools` 等，比 `pyproject.toml` 更「全」。

[installation.md:46-50](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/get_started/installation.md#L46-L50) 还点出一个重要运行时事实：ROCm 上 PyTorch **通过 `torch.cuda` 这个 API** 暴露 AMD 显卡（即代码里写的是 `torch.cuda`，底层却跑在 ROCm 上），分布式用 NCCL。这意味着 SpecForge 的源码不需要为 ROCm 单独写一套设备分支——它复用 CUDA 的 API 抽象。

**Ascend NPU**（[installation.md:52-62](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/get_started/installation.md#L52-L62)）：先装「厂商匹配的 PyTorch 和 `torch_npu` 包」，再装 SpecForge。NPU 的两个 checked-in 配方 [`qwen3.5-4b-dflash-online-npu.yaml`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3.5-4b-dflash-online-npu.yaml) 和 [`qwen3.5-4b-domino-online-npu.yaml`](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/examples/configs/qwen3.5-4b-domino-online-npu.yaml) 用「外部 SGLang server 捕获 + SDPA consumer」的方式跑。

NPU 的运行命令和环境变量在训练指南里（[training.md:386-393](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L386-L393)）：

```bash
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3
export HCCL_CONNECT_TIMEOUT=7200
export HCCL_EXEC_TIMEOUT=7200
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True

specforge train -c examples/configs/qwen3.5-4b-dflash-online-npu.yaml
```

这几个环境变量的含义：

- `ASCEND_RT_VISIBLE_DEVICES`：指定用哪几张 NPU（类似 CUDA 的 `CUDA_VISIBLE_DEVICES`）。
- `HCCL_CONNECT_TIMEOUT` / `HCCL_EXEC_TIMEOUT`：HCCL 通信建链和执行的超时（秒），NPU 大模型分布式建链慢，所以要放宽到 7200 秒。
- `PYTORCH_NPU_ALLOC_CONF=expandable_segments:True`：NPU 显存分配策略，减少碎片。

最后一句很关键（[training.md:395-397](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L395-L397)）：**统一启动器会自动补齐 rank / world-size / rendezvous 变量，运行时会动态选择当前设备，并在检测到 `torch_npu` 活跃时自动用 HCCL**。这就是 SpecForge「一套运行时统一支撑三类硬件」的工程体现——你不用为不同硬件改训练代码或 YAML，差异都被启动器和运行时吸收了。

#### 4.3.4 代码实践

这是一个阅读对比型实践，目标是让你看懂 ROCm 清单与通用依赖的差异。

1. **实践目标**：对比 [requirements-rocm.txt](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/requirements-rocm.txt) 与 [pyproject.toml](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/pyproject.toml)，找出 ROCm 版本独有的三处改动，并为「ROCm」和「NPU」各写出正确的安装命令顺序。
2. **操作步骤**：
   - 同时打开两个文件，逐行对照 `torch`、`sglang` 两行。
   - 找出 ROCm 清单里多出的 `--extra-index-url`。
   - 写出 ROCm 的两步安装命令（先 requirements，再本体）。
   - 写出 NPU 的安装思路（先厂商 torch + torch_npu，再本体）。
3. **需要观察的现象**：你会发现 ROCm 清单里 `torch` 带 `+rocm7.2` 后缀、`sglang` 带 `[all]`，且开头有一行额外的 index URL——这三处都是 `pyproject.toml` 里没有的。
4. **预期结果**：
   - ROCm 安装：`pip install -r requirements-rocm.txt` → `pip install -e .`。
   - NPU 安装：先按昇腾官方文档装好 PyTorch 与 `torch_npu`，再 `pip install -e .`，运行前导出 `ASCEND_RT_VISIBLE_DEVICES` / `HCCL_*` 等环境变量。
5. 本实践无需 GPU，属于「文档与配置对照型」任务。真正的 ROCm/NPU 安装需要对应硬件，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 ROCm 必须先装 `requirements-rocm.txt`，而不能直接 `pip install -e .`？

> **参考答案**：因为 `pyproject.toml` 里写的是通用 `torch==2.11.0`，pip 会去默认 index 找，默认 index 上没有 ROCm 版（ROCm wheel 在 PyTorch 的 `rocm7.2` 专属 index 上）。先装 `requirements-rocm.txt` 可以通过其中的 `--extra-index-url` 把 torch 钉成 `torch==2.11.0+rocm7.2`，确保拿到 ROCm 版二进制；之后再装 SpecForge 本体时，pip 看到 torch 已满足，就不会再用通用版本覆盖。

**练习 2**：SpecForge 源码里需要为 ROCm 和 NPU 单独写「设备分支」吗？为什么？

> **参考答案**：基本不需要。ROCm 复用 `torch.cuda` 这个 API（底层是 ROCm），所以 CUDA 代码路径直接适用。NPU 由统一启动器和运行时在检测到 `torch_npu` 活跃时自动切到 NPU 设备和 HCCL 通信（见 [training.md:395-397](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L395-L397)）。差异被「启动器 + 运行时」吸收了，这正是 SpecForge「一套运行时统一支撑多硬件」的设计目标。具体设备选择与通信后端如何在源码里实现的细节，留到 u8「分布式与并行」单元展开。

**练习 3**：如果你手上是 NVIDIA GPU，想用 flash-attention 加速，应该怎么装？

> **参考答案**：先正常从源码安装 SpecForge（`uv pip install -v . --prerelease=allow`），再单独装可选扩展：`pip install 'specforge[fa]'`（等价于装 `flash-attn`、`ninja`、`packaging`）。注意 flash-attn 需要编译，耗时较长，且对 CUDA 版本有要求；装不上也不影响核心训练，只是注意力算子会退回 SDPA。

---

## 5. 综合实践

把三个模块串起来，完成本讲的「毕业任务」：**从零搭好环境并验证三个子命令**。

**任务：创建 Python 3.11 虚拟环境，安装 SpecForge，运行 `specforge --help` 确认 `train` / `export` / `benchmark` 三个子命令可用。**

操作步骤（假设 NVIDIA GPU，从源码安装）：

```bash
# 1. 克隆并进入目录
git clone https://github.com/sgl-project/SpecForge.git
cd SpecForge

# 2. 创建 Python 3.11 虚拟环境并激活
uv venv -p 3.11
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. 安装（耗时较长，主要是 torch/sglang）
uv pip install -v . --prerelease=allow

# 4. 验证安装
specforge --help                  # 应列出 train / export / benchmark 三个子命令
specforge train --help            # 查看 train 子命令的参数（--config / --role / --plan 等）
pip show specforge                # 应显示 Version: 0.2.0
```

**需要观察的现象与预期结果**：

- `specforge --help` 的输出里应当能看到三个子命令及它们的 help 文案：
  - `train` —— 「train a draft model from a typed config」（对应 [cli.py:172](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L172)）。
  - `export` —— 「materialize a runtime checkpoint as a model directory」（对应 [cli.py:199-201](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L199-L201)）。
  - `benchmark` —— 「benchmark a running SGLang server」（对应 [cli.py:213-219](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L213-L219)）。
- `specforge train --help` 应能看到 `-c/--config`、`--role`、`--node-rank`、`--plan` 以及可变长 `overrides` 参数。
- `pip show specforge` 的 `Version` 应为 `0.2.0`（来自 [version.txt](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/version.txt)）。

**验收标准**：

- `which specforge` 指向你的虚拟环境目录，说明命令入口注册成功（对应 [pyproject.toml:34-35](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/pyproject.toml#L34-L35) 的 `[project.scripts]`）。
- `specforge --help` 能列出三个子命令，且文案与 [cli.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py) 里的 `help=` 字符串一致。
- 能用一句话说出自己机器属于 CUDA / ROCm / NPU 哪一类，以及对应走哪条安装路径。

**待本地验证**：本讲作者未在你的机器上实际执行安装，以上命令的真实耗时、是否需要配镜像源、是否遇到编译问题（如 `fa` 扩展的 flash-attn）均取决于你的硬件与网络，请以本地实际结果为准。若安装失败，先检查：① Python 是否 ≥ 3.11；② 虚拟环境是否激活；③ 是否混装了旧版 torch（建议在干净虚拟环境里装）。

---

## 6. 本讲小结

- SpecForge 是一个 **Python ≥ 3.11** 的包，包名 `specforge`，版本号从 [version.txt](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/version.txt)（当前 `0.2.0`）动态读取（见 [pyproject.toml:5-12](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/pyproject.toml#L5-L12)、[pyproject.toml:48-49](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/pyproject.toml#L48-L49)）。
- 核心依赖里 `torch==2.11.0` / `transformers==5.8.1` / `sglang==0.5.14` 三个**钉死版本**是接口对齐的「铁三角」；另有 `data` / `dev` / `fa` / `liger` 四个可选扩展（见 [pyproject.toml:13-46](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/pyproject.toml#L13-L46)）。
- 安装后自动注册 `specforge` 命令，指向 `specforge.cli:main`（见 [pyproject.toml:34-35](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/pyproject.toml#L34-L35)），`main` 里定义了 `train` / `export` / `benchmark` **三个子命令**（见 [cli.py:169-220](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/cli.py#L169-L220)）。
- 官方**推荐从源码安装**：`git clone` → `uv venv -p 3.11` → `uv pip install -v . --prerelease=allow`；也可 `pip install specforge` 从 PyPI 装（见 [installation.md:7-26](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/get_started/installation.md#L7-L26)）。
- 三类加速器差异只在「装环境」：CUDA 标准 `pip install .`；ROCm 先装 [requirements-rocm.txt](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/requirements-rocm.txt)（含 `torch==2.11.0+rocm7.2` 与 ROCm 专属 index）再装本体；NPU 先装厂商 PyTorch + `torch_npu` 再装本体（见 [installation.md:28-62](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/get_started/installation.md#L28-L62)）。
- 装好之后，三类硬件**共用同一个 `specforge train` 入口和同一份 YAML**，ROCm 复用 `torch.cuda` API、NPU 由运行时自动切到 HCCL（见 [training.md:370](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L370)、[training.md:395-397](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/training.md#L395-L397)）。

---

## 7. 下一步学习建议

环境装好后，建议按以下顺序继续：

1. **补齐原理直觉（可选但推荐）**：如果你对投机解码还只有模糊印象，先读 [u1-l3 投机解码原理](./u1-l3-speculative-decoding.md)，理解 prefill/草拟/验证三阶段。理解原理后再看 `specforge train` 在做什么会更踏实。
2. **建立源码地图**：读 [u1-l5 目录结构与源码地图](./u1-l5-source-map.md)，通览 `specforge/` 包从 `cli` 到 `algorithms/training/runtime/inference/modeling` 的目录划分——这是进入进阶层的前提。
3. **第一次跑通训练**：直接进入 [u2-l1 五分钟跑通一次训练](./u2-l1-first-run.md)，用一个 checked-in 示例配置走通 `specforge train`，亲眼看到训练启动。需要 GPU 环境。
4. **读懂配置结构**：跑通后读 [u2-l2 配置文件七段结构](./u2-l2-config-sections.md)，理解 YAML 的 `model/data/training/...` 七大段，为后续自定义做准备。

如果暂时没有 GPU，可以先把 [u1-l5](./u1-l5-source-map.md) 和 [u2-l2](./u2-l2-config-sections.md) 这类偏阅读的讲义过一遍，等有硬件了再回头做 [u2-l1](./u2-l1-first-run.md) 的实战。无论如何，本讲建立的「三类硬件装法 + 三个子命令」是后续所有动手实践的地基。

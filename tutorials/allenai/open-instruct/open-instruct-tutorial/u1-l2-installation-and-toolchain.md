# 环境搭建：uv、Docker 与开发工具链

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `uv sync` 在本地装好 open-instruct 的完整开发环境，并说清楚它到底装了什么、为什么这么装。
2. 读懂 `pyproject.toml` 中的依赖分组（`dev` / `cuda12` / `cuda13`）与 `tool.uv.sources` 的 CUDA 版本切换机制，理解同一份依赖声明如何支撑不同 GPU 平台。
3. 读懂 `Dockerfile` 的三个基础阶段（`cuda12` / `cuda13` / `cuda13-moe`）与 `IMAGE_VARIANT` 构建参数，理解训练镜像为什么长这样。
4. 说清楚 `-moe` 变体额外引入了什么（transformer-engine 2.16.1、`DS_SKIP_CUDA_CHECK=1`、cuBLAS 指向系统副本与构建期符号检查）、它专供哪些模型（MoE v2，即 `OLMoDDPModel`，如 Olmo 3.5 Small），以及两个默认镜像为什么完全不受影响。
5. 掌握日常开发三板斧：`uv run pytest` 跑测试、`make style` 格式化、`make quality` 过质量门禁，并知道它们背后分别是 ruff、ty、compileall 等哪些工具。

本讲不涉及任何模型训练，只解决一件事：**把环境搭对，把工具链用起来**。这是后面所有讲义的前置条件。

## 2. 前置知识

### 2.1 依赖管理器：从 pip 到 uv

- **pip**：Python 最经典的包管理器，按 `requirements.txt` 逐个安装，但不负责「锁版本」和「建虚拟环境」。
- **虚拟环境（venv）**：一个隔离的 Python 解释器 + 依赖目录，避免不同项目之间依赖互相污染。传统流程是 `python -m venv .venv && source .venv/bin/activate`。
- **uv**：Astral 公司用 Rust 写的下一代 Python 包管理器，一条 `uv sync` 命令可以同时完成「下载指定版本的 Python 解释器 + 创建 `.venv` + 按 lock 文件精确安装所有依赖」，速度比 pip 快一个数量级。open-instruct 已经把 uv 作为主力安装方式（安装文档中标注为 preview），Docker 镜像里也用它装依赖。
- **lock 文件（`uv.lock`）**：把「我想要 torch>=2.10」这种模糊声明解析成「装 torch 2.10.x 的某个具体 wheel 及其哈希」的精确快照。CI 和 Docker 构建都用 `--frozen` 模式，即「只按 lock 装，不许重新解析」，保证所有人环境一致。

### 2.2 Python、PyTorch 与 CUDA 的三角关系

深度学习环境最常见的坑：PyTorch 的 wheel（预编译包）是**按 CUDA 版本分别编译**的。同样是 `torch==2.10.0`，CUDA 12.8 的 Linux 版和 CUDA 13.0 的 Linux 版是两个不同的文件，来自不同的下载索引。所以「装哪个 torch」取决于「你的机器驱动支持哪代 CUDA」。open-instruct 用 uv 的**依赖分组（dependency groups）**把这两套 wheel 隔开，这是本讲的重点之一。

同一份 torch wheel 里还会**自带**一批 NVIDIA 运行库（cuBLAS、cuDNN 等，以 `nvidia-*` pip 包的形式躺在 venv 里），torch 是按 venv 内的绝对路径加载它们的——这个细节是理解本讲 4.3 节 MoE 镜像变体的关键。

### 2.3 工具链里的每个工具是干什么的

| 工具 | 角色 | 一句话说明 |
|---|---|---|
| ruff | 格式化 + Lint | 用 Rust 写的超快工具，`ruff format` 管代码排版，`ruff check` 找坏味道（未用 import、可疑写法等） |
| ty | 类型检查 | Astral 的 Python 类型检查器，静态发现类型错误 |
| compileall | 语法检查 | 把所有 `.py` 编译成字节码，能抓到语法错误 |
| pytest | 测试框架 | 自动发现并运行 `test_*.py` 里的用例 |
| pre-commit | Git 钩子管理 | 在 `git commit` 前自动跑格式化和检查 |
| mkdocs | 文档站 | 把 `docs/` 渲染成可搜索的文档网站 |
| Docker / Beaker | 容器 / AI2 集群 | 把环境打包成镜像，在 GPU 集群上复现 |

### 2.4 一条重要提醒：文档会滞后，源码是真相

安装文档 `docs/get_started/installation.md` 开头写的是「Dockerfile 使用 Python 3.10」，还提到了一个 `Dockerfile.uv` 文件。但你核对当前仓库会发现：`pyproject.toml` 钉死的是 **Python 3.12**，仓库里也**只有 `Dockerfile` 一个 Dockerfile**（它本身就内置了 uv）。这是研究代码库的常态——文档更新赶不上代码。本讲会同时告诉你「文档怎么说」和「源码实际是什么」，并教你在两者冲突时以 `pyproject.toml` + `uv.lock` + `Dockerfile` 为准。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [docs/get_started/installation.md](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/docs/get_started/installation.md) | 官方安装指南：pip / uv / Docker 三条路线 |
| [pyproject.toml](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/pyproject.toml) | 项目元数据、依赖声明、uv 分组与索引、ruff/ty/pytest 配置，**本讲最核心的文件** |
| [uv.lock](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/uv.lock) | uv 生成的精确锁文件，Docker 与 CI 用 `--frozen` 模式消费它 |
| [requirements.txt](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/requirements.txt) | 由 `uv export` 自动导出，供 pip 路线使用，**不是手写的** |
| [Dockerfile](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile) | 训练镜像入口：cuda12/cuda13/cuda13-moe 三个基础阶段 + 变体选择 + uv 安装依赖 |
| [Makefile](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Makefile) | `make style` / `make quality` 等质量门禁目标 |
| [.pre-commit-config.yaml](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/.pre-commit-config.yaml) | git 提交前的自动检查钩子 |
| [conftest.py](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/conftest.py) | pytest 全局配置：没有 vllm/GPU 时自动跳过相关测试 |
| [open_instruct/test_math_utils.py](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/test_math_utils.py) | 本讲实践用的最小测试文件 |
| [scripts/train/build_image_and_launch.sh](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh) | AI2 内部用户「构建镜像 + 提交 Beaker」的一键脚本，含 `--cuda-version` 与 `--moe` 参数 |
| [open_instruct/olmo_core_utils.py](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_utils.py) | `require_transformer_engine`：MoE v2 训练启动期的 transformer-engine 检查与报错指引 |
| [.github/workflows/tests.yml](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/.github/workflows/tests.yml) | CI 单测流程，展示官方推荐的环境装配顺序 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **依赖声明与 uv 分组**（`pyproject.toml` 为主）
2. **Dockerfile 构建入口**（镜像里如何复用同一套依赖声明、如何选择 CUDA 阶段与变体）
3. **CUDA 13 MoE 镜像变体**（`cuda13-moe`：transformer-engine、`DS_SKIP_CUDA_CHECK` 与 cuBLAS 手术）
4. **Makefile 质量工具链**（ruff / ty / pytest / pre-commit 如何串成门禁）

### 4.1 依赖声明与 uv 分组

#### 4.1.1 概念说明

`pyproject.toml` 是现代 Python 项目的「身份证 + 装备清单」。open-instruct 在这一个文件里同时声明了：

- 项目叫什么、支持哪个 Python 版本；
- 运行训练需要哪些库（torch、transformers、vllm、deepspeed、ray……）；
- 这些库在不同平台（Linux/macOS、x86_64/arm64）上分别怎么装；
- 开发工具（pytest、ruff、ty）单独归入 `dev` 组，不污染运行时；
- CUDA 12 与 CUDA 13 两套互斥的 GPU 栈分别归入 `cuda12` / `cuda13` 组。

「分组」的好处：一台 CUDA 12.8 驱动的机器和一台 CUDA 13 的机器，用**同一份声明**、**不同的 `--group` 参数**就能装出各自可用的环境，且两组永远不会被同时装进一个环境（uv 的 `conflicts` 声明强制互斥）。

#### 4.1.2 核心流程

`uv sync` 的决策链可以概括为：

```text
读取 pyproject.toml（声明）
    ↓
解析规则：
    - Python 版本必须满足 requires-python（==3.12.*）
    - 默认安装 default-groups = ["dev", "cuda12"]
    - cuda12 与 cuda13 互斥（conflicts）
    - torch/torchvision/torchaudio 的下载源按所在组切换：
        cuda12 组 → pytorch-cu128 索引
        cuda13 组 → pytorch-cu130 索引
    - 平台标记（marker）过滤：
        vllm、bitsandbytes、flash-attn 等不装到 macOS（Darwin）
    ↓
对照 uv.lock（精确版本与哈希）
    ↓
创建/更新 .venv，uv 自管 Python 3.12（python-preference = "only-managed"）
    ↓
安装 open-instruct 本身（可编辑安装）+ 所选组的全部依赖
```

#### 4.1.3 源码精读

**① Python 版本与核心依赖**

[pyproject.toml:L1-L6](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/pyproject.toml#L1-L6) 声明项目名并钉死 Python 版本为 3.12：

```toml
[project]
name = "open-instruct"
dynamic = ["version"]
description = "Train open, instruction-following language models"
readme = "README.md"
requires-python = "==3.12.*"
```

注意 `dynamic = ["version"]`：版本号不写死在文件里，而是由 git 标签动态生成（见后文 setuptools_scm）。这就是为什么 Docker 构建时要设置 `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_OPEN_INSTRUCT` 环境变量兜底。

[pyproject.toml:L7-L55](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/pyproject.toml#L7-L55) 是运行时依赖清单，几个值得留意的条目：

- [pyproject.toml:L32](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/pyproject.toml#L32)：`"torch>=2.10.0"` —— 只写版本下限，具体装哪个 CUDA 变体由后面的 `tool.uv.sources` 决定。
- [pyproject.toml:L38](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/pyproject.toml#L38)：`"vllm>=0.19.1; platform_system != 'Darwin'"` —— vllm 没有 macOS 版本，用平台标记排除。
- [pyproject.toml:L43-L45](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/pyproject.toml#L43-L45)：`flash-attn`、`liger-kernel` 等同样带平台标记；`flash-attn` 在 Apple Silicon（aarch64）上也不装。

**② 版本动态生成**

[pyproject.toml:L57-L65](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/pyproject.toml#L57-L65) 声明构建系统，并用 setuptools_scm 从 git 状态生成版本号写入 `open_instruct/_version.py`：

```toml
[build-system]
requires = ["setuptools", "setuptools_scm>=8"]
build-backend = "setuptools.build_meta"

[tool.setuptools_scm]
write_to = "open_instruct/_version.py"
```

**③ CUDA 分组切换的核心：tool.uv.sources 与索引**

这是本模块最关键的一段。[pyproject.toml:L83-L97](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/pyproject.toml#L83-L97) 为 torch 三件套按「组」指定下载索引：

```toml
torch = [
  { index = "pytorch-cu128", group = "cuda12" },
  { index = "pytorch-cu130", group = "cuda13" },
  { index = "pytorch-cu130", marker = "platform_system == 'Linux' and platform_machine == 'aarch64'" },
]
```

读法：当解析 `cuda12` 组时，torch 一律从 `pytorch-cu128` 索引（即 CUDA 12.8 wheel）获取；解析 `cuda13` 组时改从 `pytorch-cu130` 获取。这些索引定义在 [pyproject.toml:L105-L118](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/pyproject.toml#L105-L118)，都是 `explicit = true` 的专用索引（不会用于解析其他包）。[pyproject.toml:L78-L81](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/pyproject.toml#L78-L81) 对 `flash-attn` 用的是另一种写法——直接按组指向不同的预编译 wheel URL。[pyproject.toml:L99-L103](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/pyproject.toml#L99-L103) 则说明 vllm 的 PyPI 官方 wheel 基于 CUDA 12.8，`+cu130` 构建只发布在专用 wheel 索引上，所以只有 cuda13 组需要换源。

**④ uv 全局行为**

[pyproject.toml:L136-L154](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/pyproject.toml#L136-L154) 定义 uv 的全局行为，四行最有信息量：

```toml
[tool.uv]
preview = true
python-preference = "only-managed"   # 只用 uv 自己下载的 Python，不用系统 Python
default-groups = ["dev", "cuda12"]   # uv sync 默认装 dev + cuda12
conflicts = [
    [ { group = "cuda12" }, { group = "cuda13" } ],   # 两组互斥
]
```

也就是说，在一台普通 Linux 机器上裸跑 `uv sync`，你得到的是 **dev 工具 + CUDA 12.8 训练栈**；要装 CUDA 13 栈需要显式 `uv sync --group cuda13`（此时 uv 会拒绝同时带上 cuda12）。

**⑤ 三个依赖组的内容**

[pyproject.toml:L156-L184](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/pyproject.toml#L156-L184)：

- `cuda12` 组（L157-L165）与 `cuda13` 组（L166-L173）：内容几乎相同（torch 三件套 + vllm + flash-attn 系列），区别只在 flash-attn-3 的 wheel 是 cu128 还是 cu130 构建。真正的版本切换靠前述 `tool.uv.sources` 完成。
- `dev` 组（L174-L184）：`pytest`、`ruff`、`ty`、`beaker-py`、`mkdocs-material`、`pre-commit` 等，全是开发工具，不进运行时环境也能跑训练。

注意：transformer-engine **不在**任何依赖组里——它只存在于 `-moe` Docker 镜像变体中（见 4.3 节），本地 `uv sync` 装不到它，这是有意为之的设计。

**⑥ requirements.txt 从哪来**

[requirements.txt:L1-L2](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/requirements.txt#L1-L2) 的第一行就自曝了身份：

```text
# This file was autogenerated by uv via the following command:
#    uv export --format requirements-txt --no-hashes --all-extras --group cuda12 --no-emit-project --output-file requirements.txt
```

它是从 `uv.lock` 导出的 cuda12 视图，供 pip 安装路线（[installation.md:L6-L16](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/docs/get_started/installation.md#L6-L16)）使用。pre-commit 里还有个钩子保证 `uv.lock` 变化时它会被重新导出（见 4.4.3）。**不要手改这个文件。**

#### 4.1.4 代码实践

**实践目标**：用 `uv sync` 装好环境，并验证你装到的正是声明所描述的那套依赖。

**操作步骤**：

1. 确认已安装 uv（未安装可 `curl -LsSf https://astral.sh/uv/install.sh | sh`，这也是 [build_image_and_launch.sh:L114-L118](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L114-L118) 使用的安装方式）。
2. 在仓库根目录执行：

   ```bash
   uv sync
   ```

3. 验证解释器与关键库版本：

   ```bash
   uv run python -c "import sys; print(sys.version)"
   uv run python -c "import torch; print(torch.__version__)"
   ```

4. 验证平台差异（在 Linux 上执行应成功；在 macOS 上执行应报 `ModuleNotFoundError: No module named 'vllm'`，因为 L38 的标记把它排除了）：

   ```bash
   uv run python -c "import vllm; print(vllm.__version__)"
   ```

5. 验证 transformer-engine 确实不在本地环境（应报 `ModuleNotFoundError: No module named 'transformer_engine'`）：

   ```bash
   uv run python -c "import transformer_engine; print(transformer_engine.__version__)"
   ```

**需要观察的现象**：

- `uv sync` 首次运行会下载一个 uv 托管的 Python 3.12 并创建 `.venv/` 目录；
- `sys.version` 输出 3.12.x；
- `torch.__version__` 在 cuda12 默认组下应带 `cu128` 字样（具体小版本以 lock 为准，待本地验证）。

**预期结果**：所有命令退出码为 0（macOS 上第 4 步、所有平台上第 5 步的报错本身也是「符合预期」的结果，说明平台标记与「TE 只随镜像分发」的设计生效）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 torch 只写 `>=2.10.0`，而不直接在依赖里写死 CUDA 版本？
**答案**：因为同一个 torch 版本号对应多个按 CUDA 分别编译的 wheel。CUDA 变体的选择放在 [tool.uv.sources](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/pyproject.toml#L83-L97) 里按依赖组切换：cuda12 组走 `pytorch-cu128` 索引，cuda13 组走 `pytorch-cu130` 索引。版本约束管「哪个版本」，source 管「哪个构建」。

**练习 2**：裸跑 `uv sync`（不带任何参数）会安装哪些组？
**答案**：`dev` 和 `cuda12`，因为 [pyproject.toml:L140](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/pyproject.toml#L140) 设置了 `default-groups = ["dev", "cuda12"]`。所以本地开发开箱即得 pytest/ruff/ty 和 CUDA 12.8 栈。

**练习 3**：在一台 CUDA 13 集群的机器上应该怎么装环境？
**答案**：`uv sync --group cuda13`（dev 组仍是默认组之一会一起装上）。uv 的 `conflicts` 声明（[pyproject.toml:L141-L146](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/pyproject.toml#L141-L146)）保证 cuda12 与 cuda13 不会同时进入一个环境。

### 4.2 Dockerfile 构建入口

#### 4.2.1 概念说明

训练大模型通常不在笔记本上跑，而是提交到 GPU 集群（open-instruct 默认对接 Allen AI 的 Beaker）。集群环境不可控，所以要把「操作系统 + CUDA 工具链 + Python 依赖 + 仓库代码」整体打包成 Docker 镜像。open-instruct 的 `Dockerfile` 有三个鲜明特点：

1. **一个文件支持两代 CUDA × 两个变体**：用多阶段构建 + 构建参数，`--build-arg CUDA_VERSION=12|13` 选 CUDA 代际，`--build-arg IMAGE_VARIANT=""|"-moe"` 选是否叠加 transformer-engine（仅 CUDA 13 有 `-moe` 变体），组合出 cuda12 / cuda13 / cuda13-moe 三种镜像。
2. **镜像内用 uv 装依赖**：镜像里不手写 pip 命令，而是复用 4.1 节的同一套 `pyproject.toml` + `uv.lock`，做到「本地环境和镜像环境来自同一份声明」。
3. **变体差异收拢在一个 `if` 里**：MoE 专用组件全部集中在一条带条件守卫的 `RUN` 指令中，默认镜像执行到它时是空操作（详见 4.3 节）。

#### 4.2.2 核心流程

镜像构建流程：

```text
docker build --build-arg CUDA_VERSION=12|13 --build-arg IMAGE_VARIANT=""|"-moe" .
    ↓
选择基础镜像：
    cuda12     → nvidia/cuda:12.8.1-devel-ubuntu22.04
    cuda13     → nvidia/cuda:13.0.3-devel-ubuntu22.04
    cuda13-moe → nvidia/cuda:13.3.1-cudnn-devel-ubuntu22.04
    ↓
apt 安装基础工具（git/make/curl/nginx 等）
    ↓
安装 DOCA OFED 用户态驱动、Google Cloud CLI、Beaker CLI
    ↓
拷入 uv 可执行文件（官方镜像 ghcr.io/astral-sh/uv）
    ↓
uv run --frozen --no-default-groups --group dev --group cuda${CUDA_VERSION}
    → 按 uv.lock 精确安装依赖 + 下载 nltk 数据
    ↓
【仅 -moe 变体】安装 transformer-engine、替换 cuBLAS、符号自检（见 4.3 节）
    ↓
COPY 仓库代码（configs/scripts/mason.py/open_instruct/oe-eval-internal）
    ↓
设置 PATH 指向 /stage/.venv/bin，容器内命令直接可用
```

#### 4.2.3 源码精读

**① 三个基础阶段与变体选择**

[Dockerfile:L1-L12](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L1-L12)：

```dockerfile
ARG CUDA_VERSION=12
# "" or "-moe" (CUDA 13 only): the -moe image adds transformer-engine for MoE v2 models; see below.
ARG IMAGE_VARIANT=""
FROM nvidia/cuda:12.8.1-devel-ubuntu22.04 AS cuda12
FROM nvidia/cuda:13.0.3-devel-ubuntu22.04 AS cuda13
FROM nvidia/cuda:13.3.1-cudnn-devel-ubuntu22.04 AS cuda13-moe
# torch is built with CUDA 13.0; without this DeepSpeed refuses to JIT-build its ops (CPU Adam) against 13.3.
ENV DS_SKIP_CUDA_CHECK=1
FROM cuda${CUDA_VERSION}${IMAGE_VARIANT}

ARG CUDA_VERSION
ARG IMAGE_VARIANT
```

要点逐条拆解：

- 前两个 `ARG` 是全局默认值：CUDA 代际默认 12，变体默认空串。`IMAGE_VARIANT` 只接受 `""` 或 `"-moe"`，且注释写明 **-moe 仅限 CUDA 13**。
- 三个 `FROM ... AS` 各定义一个基础阶段，第四个 `FROM cuda${CUDA_VERSION}${IMAGE_VARIANT}` 用字符串拼接选择从哪个阶段继续——`12`+`""` 得 cuda12 镜像，`13`+`""` 得 cuda13 镜像，`13`+`"-moe"` 得 cuda13-moe 镜像。这是 Docker 的「条件基础镜像」惯用法。
- `ENV DS_SKIP_CUDA_CHECK=1` 位于 `FROM ... AS cuda13-moe` 之后、最终 `FROM` 之前，属于 **cuda13-moe 阶段内部**的指令——只有以它为基座的 `-moe` 镜像才继承这个变量，默认镜像完全不受影响（动机见 4.3 节）。
- 最终阶段里重新 `ARG CUDA_VERSION` / `ARG IMAGE_VARIANT`：Docker 的 ARG 在进入新阶段后失效，重新声明才能在后续指令（L85 的 `--group cuda${CUDA_VERSION}`、L98 的 if 守卫）里继续使用。

默认值是 12，AI2 的 Jupiter/Saturn 集群（CUDA 12.8 宿主驱动）就用默认；Holmes 等 CUDA 13 集群用 `--build-arg CUDA_VERSION=13`；MoE v2 模型再叠加 `--build-arg IMAGE_VARIANT="-moe"`。

**② 基础工具与集群设施**

[Dockerfile:L18-L28](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L18-L28) 安装 build-essential、git、make、nginx 等基础工具；[Dockerfile:L59-L70](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L59-L70) 安装固定版本（v1.5.235）的 Beaker CLI，供集群上提交任务使用。这些与 Python 无关，却是集群可运行性的保障。

**③ 拷入 uv 并装配依赖**

[Dockerfile:L72](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L72) 从官方镜像拷入 uv 0.8.6（CI 的 [tests.yml:L24-L27](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/.github/workflows/tests.yml#L24-L27) 也用同一版本，保持一致）。随后是整个镜像最核心的一步：

[Dockerfile:L82-L86](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L82-L86)：

```dockerfile
RUN --mount=type=cache,target=${UV_CACHE_DIR} \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv run --frozen --no-default-groups --group dev --group cuda${CUDA_VERSION} \
        python -m nltk.downloader punkt punkt_tab words
```

这段做了三件事：

- 用 `--frozen` 严格按 `uv.lock` 安装（代码改动不会引起依赖漂移）；
- 用 `--no-default-groups` **关掉**默认组，再显式指定 `--group dev --group cuda${CUDA_VERSION}`——因为默认组固定是 cuda12，而镜像需要按构建参数在 cuda12/cuda13 之间切换；
- 顺手下载 nltk 的分词数据（`punkt punkt_tab words`），数学判分等模块要用。

注意这里只 bind-mount 了 `uv.lock` 和 `pyproject.toml`，**还没拷贝任何源码**——依赖层和代码层分离，代码改动时依赖层缓存依然有效，这是镜像构建加速的关键技巧。

**④ 代码层与环境变量**

[Dockerfile:L115-L119](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L115-L119) 拷贝仓库代码。其中 `COPY oe-eval-interna[l] oe-eval-internal/` 的方括号是 Docker 的 glob 技巧：目录存在时正常拷贝，不存在时也不让构建失败（CLAUDE.md 说明该目录是 `--try_launch_beaker_eval_jobs_on_weka` 实验所必需，缺失时需自行 clone）。[Dockerfile:L121-L126](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L121-L126) 把 git 提交号写进环境变量，并把 `/stage/.venv/bin` 放进 PATH——容器里直接敲 `python` 就是刚装好的环境。

**⑤ 文档中已失效的引用**

[installation.md:L41-L49](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/docs/get_started/installation.md#L41-L49) 提到「用 `Dockerfile.uv` 构建带 uv 的镜像」，但当前仓库已经没有这个文件——因为主 `Dockerfile` 本身就内置了 uv（见上文 L72、L82-L86）。同样，[installation.md:L4](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/docs/get_started/installation.md#L4) 说「使用 Python 3.10」，而 `pyproject.toml` 已钉到 3.12。读文档时请以源码为准，这是研究仓库的生存技能。

#### 4.2.4 代码实践

**实践目标**：不真正构建镜像（耗时且需要网络），通过读脚本掌握 CUDA 版本与变体如何一路传递；有 Docker 环境的读者可实际构建。

**操作步骤（阅读路线）**：

1. 打开 [scripts/train/build_image_and_launch.sh](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh)，先看 [L13-L34](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L13-L34)：解析 `--cuda-version 12|13` 参数（默认 12），非法值直接报错退出。
2. 再看 [L86-L94](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L86-L94)：`docker buildx build --build-arg CUDA_VERSION="$cuda_version" --build-arg IMAGE_VARIANT="$image_variant" ...` 把两个参数都传给 Dockerfile 的 L1/L3。
3. 串起两条调用链：
   - `--cuda-version 13`（脚本参数）→ `--build-arg CUDA_VERSION=13`（构建参数）→ `FROM cuda13`（基础镜像）→ `--group cuda13`（依赖组）→ torch 走 `pytorch-cu130` 索引；
   - `--cuda-version 13 --moe`（脚本参数）→ `--build-arg IMAGE_VARIANT="-moe"` → `FROM cuda13-moe`（基础镜像换成 13.3.1-cudnn）→ MoE 专属 RUN 块生效（见 4.3）。

   **同一组数字与字符串贯穿脚本、构建参数、基础镜像、依赖组四处**，这就是「CUDA 版本一致性」的工程实现。

**操作步骤（可选，实机构建）**：

```bash
docker build --build-arg CUDA_VERSION=12 -t open_instruct_dev .
```

**需要观察的现象**：构建日志中依赖安装步骤出现 `uv run --frozen --no-default-groups --group dev --group cuda12`；由于依赖层有 BuildKit 缓存挂载，重复构建时该步骤明显加速。

**预期结果**：镜像构建成功，`docker run --rm open_instruct_dev python -V` 输出 Python 3.12.x。（构建耗时与网络相关，待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么镜像里安装依赖要用 `--no-default-groups` 再显式 `--group cuda${CUDA_VERSION}`，而不是直接依赖默认组？
**答案**：`default-groups = ["dev", "cuda12"]` 是写死的，无法随构建参数变化。镜像需要按 `CUDA_VERSION` 在 cuda12/cuda13 之间切换，所以关掉默认组、显式指定，同时保留 dev 组（[Dockerfile:L85](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L85)）。

**练习 2**：为什么依赖安装层（L82-L86）在 COPY 代码层（L115-L119）之前？
**答案**：Docker 按层缓存。把「改动频繁的代码」放在「几乎不变的依赖」之后，改代码重建镜像时可以直接复用依赖层缓存，只需重跑 COPY，大幅加速迭代。

**练习 3**：`COPY oe-eval-interna[l] oe-eval-internal/` 里方括号的作用是什么？
**答案**：`oe-eval-interna[l]` 是一个能匹配 `oe-eval-internal` 的 glob 模式；当源路径是字面量且不存在时 COPY 会报错，而是 glob 模式且无匹配时则静默通过。这样没 clone 该内部仓库的构建也不会失败（[Dockerfile:L119](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L119)）。

### 4.3 CUDA 13 MoE 镜像变体：transformer-engine、DS_SKIP_CUDA_CHECK 与 cuBLAS 手术

#### 4.3.1 概念说明

先补几个初学者可能陌生的名词：

- **MoE（Mixture of Experts，混合专家）**：一种模型结构。每层有一个路由器（router），把每个 token 分发给少数几个「专家」前馈网络计算，而不是让所有 token 过同一个巨大的 FFN。**MoE v2** 指 OLMo-core 的第二代 MoE 实现，载体是 `OLMoDDPModel`（走 OLMo-core 的 DDP 训练模块而不是 FSDP），代表模型是 **Olmo 3.5 Small（A4B）**。
- **token permutation（token 重排）**：MoE 高效批处理的关键操作——把路由到同一专家的 token 在内存里排到一起，算完再排回去。OLMo-core 的 MoE v2 把这个重排算子委托给 transformer-engine。
- **transformer-engine（TE）**：NVIDIA 的高性能算子库。open-instruct 的默认镜像**不装**它；只有 `-moe` 变体镜像装（版本 2.16.1）。
- **cuBLAS / cuBLASLt**：NVIDIA 的矩阵乘法运行库。torch 的 wheel 自带一份（venv 里的 `nvidia/cu13/lib` 下，随 `nvidia-cublas` pip 包分发），CUDA 基础镜像的工具链目录（`/usr/local/cuda/lib64`）里也有系统副本。两份版本不同，**导出的符号（可调用函数名）集合可能不同**。
- **DeepSpeed 的 CUDA 版本检查**：DeepSpeed JIT 编译自家算子（如 CPU Adam）时会校验「本机 CUDA 工具链版本」与「torch 编译时用的 CUDA 版本」是否一致，不一致就直接拒绝构建。`DS_SKIP_CUDA_CHECK=1` 让它跳过这道校验。

这个模块解决的问题是：**MoE v2 模型需要 TE，但把 TE 装进镜像会连带引出一串 CUDA 库兼容性手术，影响面大到不能动默认镜像**。于是一条独立的 `-moe` 变体诞生了。整个来龙去脉写在 Dockerfile 的注释块里（[Dockerfile:L88-L95](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L88-L95)），按因果链捋一遍：

1. MoE v2（`OLMoDDPModel`）的 token permutation 依赖 TE 的算子（OLMo-core 的 `olmo_core/nn/moe/utils.py`），默认镜像没有 TE → 需要一个装了 TE 的镜像。
2. TE 2.16.1 的预编译 cu13 库会调用 `cublasLtGroupedMatrixLayoutInit_internal` 这个符号，而 torch 自带的 cuBLAS（nvidia-cublas 13.1.0.3）**不导出**它 → 直接装 TE，一运行就找不到符号。
3. 解法：把 venv 里 torch 自带的 `libcublas` / `libcublasLt` **替换成（符号链接到）基础镜像里更新的系统副本**（CUDA 13.3 工具链的 `/usr/local/cuda/lib64`）。
4. 为什么不用 `LD_LIBRARY_PATH` 指过去？因为 torch 是**按 venv 内的绝对路径**加载自带库的，动态链接器的搜索路径对它无效——只能替换文件本身。
5. 为什么不直接改默认 cuda13 镜像？因为这一替换**改变了镜像里所有程序用的 cuBLAS**，影响面太大，所以隔离成独立变体，两个默认镜像保持原样。
6. `-moe` 的基座是 CUDA **13.3.1**（比默认 cuda13 的 13.0.3 新），正是为了拿到带那个符号的系统 cuBLAS；但 torch 是按 CUDA 13.0 编译的，于是有了 `DS_SKIP_CUDA_CHECK=1`（让 DeepSpeed 仍然用 13.3 工具链 JIT 构建算子）。
7. 最后加一道**构建期自检**：用 `nm` 列出替换后 `libcublasLt` 导出的符号、用 `grep` 确认目标符号在——缺了就让整个构建失败，把问题从「运行时神秘崩溃」提前到「构建时大声报错」。

#### 4.3.2 核心流程

`-moe` 变体的装配流程（发生在依赖安装之后、代码 COPY 之前）：

```text
IMAGE_VARIANT == "-moe" ？
    ├─ 否（默认 cuda12/cuda13）→ 整个 RUN 块为空操作，什么都不装、什么都不改
    └─ 是 →
        ① uv pip install cmake ninja pybind11 setuptools wheel   （TE 源码编译需要的构建工具）
        ② 从 venv 的 nvidia.cudnn 包定位 cuDNN 目录，
           设 CPLUS_INCLUDE_PATH / C_INCLUDE_PATH / LIBRARY_PATH （让 TE 构建找得到 cuDNN 头文件与库）
        ③ MAX_JOBS=$TE_MAX_JOBS uv pip install --no-build-isolation
           "transformer-engine[pytorch]==2.16.1"                  （绑定本 venv 的 torch 编译 Python 绑定）
        ④ 对 libcublasLt、libcublas：
           把 venv 的 nvidia/cu13/lib/*.so.13 符号链接到
           /usr/local/cuda/lib64 下更新的系统副本                （补齐 TE 需要的符号）
        ⑤ nm -D 列出 libcublasLt 导出符号
           | grep cublasLtGroupedMatrixLayoutInit_internal        （构建期符号自检，缺了即失败）
```

配套的运行期防线：训练脚本启动时若发现模型是 `OLMoDDPModel`，会调用 `require_transformer_engine()` 检查 TE 算子可用，不可用立即报错并给出正确启动命令——镜像里没 TE 时不会等到第一次 forward 才崩。

#### 4.3.3 源码精读

**① 为什么需要独立变体：注释块与并行度参数**

[Dockerfile:L88-L96](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L88-L96) 是整个变体的「设计文档」，说明了上面 4.3.1 的完整因果链（TE 的 cu13 库需要 torch 自带 cuBLAS 没有的符号、替换会波及全镜像所以独立成变体、TE 的 Python 绑定按 venv 的 torch 与 cuDNN 头编译）。末尾一行构建参数：

```dockerfile
ARG TE_MAX_JOBS=2
```

`TE_MAX_JOBS` 控制 TE 源码编译的并行任务数；默认 2 是为了塞进 Docker Desktop 8 GiB 内存的虚拟机，在内存充裕的 Linux 构建机上可以调大加速编译。

**② 条件装配的 RUN 块**

[Dockerfile:L97-L105](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L97-L105) 是守卫与 TE 安装：

```dockerfile
RUN --mount=type=cache,target=${UV_CACHE_DIR} \
    if [ "$IMAGE_VARIANT" = "-moe" ]; then \
        uv pip install cmake ninja pybind11 setuptools wheel && \
        CUDNN_DIR=$(/stage/.venv/bin/python -c "import nvidia.cudnn; print(list(nvidia.cudnn.__path__)[0])") && \
        CUDNN_PATH="$CUDNN_DIR" \
        CPLUS_INCLUDE_PATH="$CUDNN_DIR/include" \
        C_INCLUDE_PATH="$CUDNN_DIR/include" \
        LIBRARY_PATH="$CUDNN_DIR/lib" \
        MAX_JOBS="$TE_MAX_JOBS" uv pip install --no-build-isolation "transformer-engine[pytorch]==2.16.1" && \
```

- 整块包在 `if [ "$IMAGE_VARIANT" = "-moe" ]` 里：默认变体时条件不成立，`if ... fi` 直接空转结束——**这就是两个默认镜像完全不受影响的第一道保障**（Shell 里条件为假的 `if` 退出码为 0，不会让构建失败）。
- `--no-build-isolation` 意味着用 venv 里已装好的构建工具（所以先装 cmake/ninja/pybind11/setuptools/wheel），并且 TE 的 Python 绑定**针对本 venv 的 torch 头文件**编译，而不是隔离环境里的另一套依赖。
- cuDNN 的头文件与库不是来自系统，而是来自 venv 的 `nvidia.cudnn` pip 包（用 `import nvidia.cudnn; print(...path...)` 动态定位），保证与所装 torch 严格配套。

[Dockerfile:L106-L112](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L106-L112) 是 cuBLAS 替换与符号自检：

```dockerfile
        for lib in libcublasLt libcublas; do \
            target=$(ls /usr/local/cuda/lib64/${lib}.so.13.* | head -1) && [ -n "$target" ] && \
            ln -sf "$target" /stage/.venv/lib/python3.12/site-packages/nvidia/cu13/lib/${lib}.so.13 || exit 1; \
        done && \
        nm -D --defined-only /stage/.venv/lib/python3.12/site-packages/nvidia/cu13/lib/libcublasLt.so.13 \
            | grep -q cublasLtGroupedMatrixLayoutInit_internal; \
    fi
```

- 对 `libcublasLt` 和 `libcublas` 各做一次：找到基础镜像 CUDA 13.3 工具链里的系统副本（`/usr/local/cuda/lib64/lib*.so.13.*`），用 `ln -sf` 把 venv 里 torch 自带的同名文件**覆盖成指向系统副本的软链接**；找不到目标文件就 `exit 1` 让构建失败。
- 最后一行 `nm -D --defined-only`（列出动态导出符号）接 `grep -q cublasLtGroupedMatrixLayoutInit_internal`：确认替换后的 `libcublasLt` 真的导出了 TE 需要的符号，缺了同样让构建失败。

**③ 启动脚本的 --moe 守卫**

[build_image_and_launch.sh:L36-L45](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L36-L45) 解析 `--moe` 参数，并强制它搭配 CUDA 13：

```bash
# --moe: the CUDA 13 image plus transformer-engine, which MoE v2 (OLMoDDPModel) models need (see Dockerfile).
image_variant=""
if [[ "${1:-}" == "--moe" ]]; then
  if [[ "$cuda_version" != "13" ]]; then
    echo "Error: --moe needs --cuda-version 13 (only the CUDA 13 image has a transformer-engine variant)." >&2
    exit 1
  fi
  image_variant="-moe"
  shift
fi
```

用法串见 [build_image_and_launch.sh:L47-L51](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L47-L51)：`Usage: $0 [--cuda-version 12|13 [--moe]] SCRIPT [SCRIPT_ARGS...]`。变体会体现在镜像名后缀（[L73](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L73) 的 `...-cuda${cuda_version}${image_variant}`）、传给 Docker 的 `--build-arg IMAGE_VARIANT`（[L89-L90](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L89-L90)），以及 Beaker 镜像描述里的 `variant: moe` 标记（[L110-L111](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L110-L111)）。

**④ 运行期防线：require_transformer_engine**

[olmo_core_utils.py:L575-L587](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_utils.py#L575-L587)：

```python
def require_transformer_engine() -> None:
    """Fail at startup when OLMo-core's MoE v2 token permutation has no kernels to call. ..."""
    if oc_moe_utils.moe_permute is None:
        raise RuntimeError(
            "MoE v2 (OLMoDDPModel) training needs transformer-engine: OLMo-core's MoE token permutation "
            "calls transformer_engine.pytorch.permutation, which is not importable here. Only open-instruct's "
            "MoE image installs it: launch with `scripts/train/build_image_and_launch.sh --cuda-version 13 "
            "--moe SCRIPT`."
        )
```

OLMo-core 在 import 不到 TE 时会把 MoE 重排算子置为 `None`，模型要等到第一次 forward 才会挂。这个函数把失败提前到启动期，且报错文本直接告诉用户正确的启动命令。它的调用点在 [olmo_core_finetune.py:L651-L658](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_finetune.py#L651-L658)：模型是 `OLMoDDPModel`（MoE v2）时才检查——dense 模型不需要 TE，不受影响。

#### 4.3.4 代码实践

**实践目标**：对照 Dockerfile，亲手列清 `-moe` 变体额外引入的每个组件，并论证两个默认镜像为何分毫未动。

**操作步骤**：

1. 通读 [Dockerfile:L1-L12](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L1-L12) 与 [L88-L112](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L88-L112)，把下表填完（答案在本节末尾，先自己填）：

   | 组件 | 默认 cuda12 / cuda13 镜像 | `-moe` 变体 | Dockerfile 出处 |
   |---|---|---|---|
   | 基础镜像 | 12.8.1-devel / 13.0.3-devel | ？ | ？ |
   | `DS_SKIP_CUDA_CHECK` | ？ | ？ | ？ |
   | transformer-engine 2.16.1 | ？ | ？ | ？ |
   | cmake / ninja / pybind11 等构建工具 | ？ | ？ | ？ |
   | venv 里的 cuBLAS（libcublas/libcublasLt） | ？ | ？ | ？ |
   | 构建期符号自检 | ？ | ？ | ？ |

2. 写出「为什么两个默认镜像完全不受影响」的三条理由（提示：if 守卫、ENV 所在阶段、其余指令是否共享）。
3. 在仓库根目录模拟一次错误调用（只是让脚本报错退出，不会真的构建，前提是当前目录是干净的 git 仓库；否则直接阅读 L36-L45 推演）：

   ```bash
   ./scripts/train/build_image_and_launch.sh --moe scripts/train/debug/oc_sft.sh
   ```

4. 阅读 [olmo_core_utils.py:L575-L587](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_utils.py#L575-L587) 的报错文本，抄下它推荐的正确命令。

**需要观察的现象**：

- 第 3 步脚本立即打印 `Error: --moe needs --cuda-version 13 (only the CUDA 13 image has a transformer-engine variant).` 并以非零退出码结束（报错文本见 [L40](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L40)，实际运行待本地验证）。

**预期结果**（第 1 步表格参考答案）：

| 组件 | 默认 cuda12 / cuda13 镜像 | `-moe` 变体 | 出处 |
|---|---|---|---|
| 基础镜像 | 12.8.1-devel / 13.0.3-devel | **13.3.1-cudnn-devel** | L4-L6 |
| `DS_SKIP_CUDA_CHECK` | 未设置 | **=1**（写在 cuda13-moe 阶段内） | L7-L8 |
| transformer-engine 2.16.1 | 无 | **有**（`--no-build-isolation` 绑定 venv 的 torch 编译） | L105 |
| 构建工具 | 无 | **有**（cmake/ninja/pybind11/setuptools/wheel，仅构建期） | L99 |
| venv 里的 cuBLAS | torch 自带版本（nvidia-cublas 13.1.0.3，cuda12 为对应 cu12 版本） | **符号链接到 /usr/local/cuda/lib64 的 13.3 系统副本** | L106-L109 |
| 构建期符号自检 | 无 | **有**（`nm -D` + `grep cublasLtGroupedMatrixLayoutInit_internal`） | L110-L111 |

第 2 步参考答案：① MoE 装配整块包在 `if [ "$IMAGE_VARIANT" = "-moe" ]` 中，默认变体时空转（L97-L98）；② `ENV DS_SKIP_CUDA_CHECK=1` 是 cuda13-moe 阶段的指令（L6 之后、L9 之前），只有以该阶段为基座的构建继承（L8）；③ 除此之外两个默认镜像与 `-moe` 走完全相同的指令序列（apt、Beaker CLI、uv 依赖、COPY 代码）。

#### 4.3.5 小练习与答案

**练习 1**：修复 cuBLAS 符号问题为什么必须替换 venv 里的文件，而不能设置 `LD_LIBRARY_PATH` 指向系统副本？
**答案**：torch 的 wheel 自带 NVIDIA 运行库，并且按 **venv 内的绝对路径**加载它们（`site-packages/nvidia/cu13/lib/...`）；动态链接器的搜索路径（`LD_LIBRARY_PATH`）管不到按绝对路径的加载。所以只能用 `ln -sf` 把 venv 里的文件本身覆盖成指向系统副本的软链接。[Dockerfile:L90-L93](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L90-L93) 的注释写明了这一点。

**练习 2**：`--build-arg CUDA_VERSION=13`（不带 `--moe`）构建出的默认 cuda13 镜像里，`DS_SKIP_CUDA_CHECK` 是什么值？
**答案**：未设置。该 ENV 位于 `FROM ... AS cuda13-moe`（L6）之后、最终 `FROM`（L9）之前，属于 cuda13-moe 阶段；只有 `IMAGE_VARIANT="-moe"` 时最终阶段才以它为基座并继承该变量。注释（L7）给出动机：torch 按 CUDA 13.0 编译，不设它 DeepSpeed 会拒绝用 13.3 工具链 JIT 构建算子（CPU Adam）。

**练习 3**：如果同事在默认 cuda13 镜像里直接训练 Olmo 3.5 Small（MoE v2），会在什么时刻、以什么方式失败？open-instruct 用什么手段把失败提前？
**答案**：OLMo-core 在 import 不到 TE 时把 MoE 重排算子置为 `None`，若没有额外检查，模型要等到第一次 forward 才崩。open-instruct 在 [olmo_core_finetune.py:L656-L658](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_finetune.py#L656-L658) 发现模型是 `OLMoDDPModel` 时调用 `require_transformer_engine()`，启动期即抛 `RuntimeError`，报错文本直接给出正确命令 `--cuda-version 13 --moe`。镜像构建期另有 `nm` + `grep` 符号自检（L110-L111），把「TE 与 cuBLAS 不匹配」进一步提前到构建期。

### 4.4 Makefile 质量工具链

#### 4.4.1 概念说明

研究代码库最怕「每个人一套风格」。open-instruct 用一个 28 行的 Makefile 把格式化、静态检查、测试统一成两条命令：

- `make style`：**格式化**（自动改写你的代码排版）；
- `make quality`：**质量检查**（lint 自动修复 + 语法编译检查 + 类型检查）。

配套还有 `-check` 变体（`style-check` / `quality-check`）：不改文件、只报告问题并以非零退出码失败，供 CI 使用。此外仓库用 pre-commit 把同样的检查挂到 `git commit` 上，用根目录 `conftest.py` 让 pytest 在缺少 vllm/GPU 的机器上自动跳过相关测试。

#### 4.4.2 核心流程

```text
开发者改完代码
    ↓
make style            → ruff format 改写排版（可能修改文件）
make quality          → ① check-keywords：确认没有违禁关键字 nonlocal
                        ② ruff check --fix：lint 并自动修复
                        ③ python -m compileall：语法编译检查
                        ④ ty check：类型检查
    ↓
uv run pytest         → conftest.py 先探测 vllm / CUDA 可用性
                        → 不可用的测试文件加入 collect_ignore，自动跳过
    ↓
git commit            → pre-commit 钩子再跑一遍 ruff + 关键字检查
                        + uv.lock 变化时重新导出 requirements.txt
    ↓
CI（.github/workflows）→ uv sync --frozen 装环境 → uv run --frozen pytest
```

#### 4.4.3 源码精读

**① Makefile 的目标定义**

[Makefile:L1-L9](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Makefile#L1-L9)：

```makefile
.PHONY: style quality check-keywords

# make sure to test the local checkout in scripts and not the pre-installed one (don't use quotes!)
export PYTHONPATH = open_instruct

check_dirs := open_instruct *mason.py

style:
	uv run ruff format $(check_dirs)
```

- `check_dirs` 限定检查范围：`open_instruct/` 目录和根目录的 `mason.py`（`*mason.py` 同样匹配 `test_mason.py`）。
- `export PYTHONPATH = open_instruct` 确保测试跑的是本地检出代码而非 pip 装的旧版。

[Makefile:L11-L14](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Makefile#L11-L14) 定义 quality 门禁：

```makefile
quality: check-keywords
	uv run ruff check -q --fix $(check_dirs)
	uv run python -m compileall -qq $(check_dirs)
	uv run ty check
```

注意 `quality` 依赖 `check-keywords`，会先跑它。[Makefile:L24-L28](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Makefile#L24-L28) 是这个特殊检查：

```makefile
# TYPE_CHECKING is allowed so optional dependencies stay out of runtime imports.
# Share the remaining keyword policy between pre-commit and CI.
check-keywords:
	@test -d open_instruct/
	@grep -rn --include='*.py' -E 'nonlocal[[:space:]]' open_instruct/; test $$? -eq 1
```

逻辑是：grep 查找 `nonlocal ` 关键字，要求其退出码为 1（即「什么都没找到」）——找到了反而算失败。`nonlocal` 在多进程/分布式训练代码里是雷区，所以干脆全面禁用。

**② 提交前钩子与 CI 保持同一套规则**

[.pre-commit-config.yaml:L20-L28](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/.pre-commit-config.yaml#L20-L28) 挂载 ruff 与 ruff-format 钩子（作用域同样是 `open_instruct/` 和 `*mason.py`）；[.pre-commit-config.yaml:L30-L37](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/.pre-commit-config.yaml#L30-L37) 的 `ban-keywords` 钩子直接复用 `make check-keywords`；[.pre-commit-config.yaml:L38-L43](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/.pre-commit-config.yaml#L38-L43) 的 `export-requirements` 钩子在 `uv.lock` 变化时自动重新导出 `requirements.txt`——这就是 4.1.3 里「该文件是自动生成的」的机制保障。

CI 侧，[.github/workflows/tests.yml:L28-L35](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/.github/workflows/tests.yml#L28-L35) 展示了官方装配顺序：`uv sync --frozen` → 下载 nltk `punkt_tab` → `uv run --frozen pytest`。你在本地照着做，就能最大程度复现 CI 行为。

**③ pytest 的环境自适应**

[conftest.py:L5-L20](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/conftest.py#L5-L20)：根目录的 conftest 先试探 vllm 是否可导入、CUDA 是否可用，然后把依赖它们的测试文件追加进 `collect_ignore`。效果是：在没有 GPU 的笔记本上 `uv run pytest` 也不会因 vllm/GPU 用例报错，它们被静默跳过。

**④ 实践要用的测试文件**

[open_instruct/test_math_utils.py:L8-L24](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/test_math_utils.py#L8-L24) 是仓库里最小的测试之一：用 `parameterized` 把 9 组「LaTeX 字符串归一化」输入塞进同一个断言，检验 `math_utils.strip_string` 能否把 `\frac 1 2`、`\left(...\right)`、`50\%` 等写法归一成统一形式（这是数学答案判分的基础，u6-l3 会展开）。它只依赖 CPU 和纯 Python，是验证环境的首选用例。

#### 4.4.4 代码实践

**实践目标**：跑通「测试 + 格式化 + 质量门禁」完整闭环，亲眼看到门禁拦截坏代码。

**操作步骤**：

1. 跑指定测试文件：

   ```bash
   uv run pytest open_instruct/test_math_utils.py -v
   ```

2. 跑格式化（注意：这一步**可能改写文件**，这是正常的）：

   ```bash
   make style
   ```

3. 跑质量门禁：

   ```bash
   make quality
   ```

4. 主动制造一个违规，观察门禁工作：新建 `open_instruct/_style_probe.py`，内容只有一行 `import os`（一个未使用的 import），再执行 `make quality`，然后删除该文件并重跑确认恢复绿色。

**需要观察的现象**：

- 第 1 步：`test_latex_normalization` 被 parameterized 展开成 **9 个**子用例（对应 [L11-L19](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/test_math_utils.py#L11-L19) 的 9 行参数），全部 PASSED（待本地验证具体输出）；
- 第 2 步：ruff format 对干净仓库通常是 no-op，输出 `X files left unchanged` 之类；
- 第 3 步：四道检查依次通过，退出码 0；
- 第 4 步：ruff check 报出 `F401 unused-import`（或自动 fix 删掉这行），quality 以非零退出码失败——门禁确实在拦人。

**预期结果**：1-3 步全绿；第 4 步演示了「坏代码进不了仓库」的原因。

#### 4.4.5 小练习与答案

**练习 1**：`make style` 和 `make style-check` 有什么区别？分别什么时候用？
**答案**：[Makefile:L8-L9](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Makefile#L8-L9) 的 `style` 会直接改写文件排版；[Makefile:L16-L17](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Makefile#L16-L17) 的 `style-check` 用 `--check --diff` 只报告不改写、有问题就失败退出。本地开发用前者，CI 用后者。

**练习 2**：为什么在没有 GPU 的机器上 `uv run pytest` 不会因为 GPU 测试失败？
**答案**：根目录 [conftest.py:L12-L20](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/conftest.py#L12-L20) 在收集阶段检测 vllm 可用性与 `torch.cuda.is_available()`，把不满足条件的测试文件加入 `collect_ignore`，pytest 直接跳过收集。

**练习 3**：如果你改了 `pyproject.toml` 里的依赖并 `uv lock`，`requirements.txt` 会自动更新吗？
**答案**：会（前提是你装了 pre-commit 并执行了提交）。[.pre-commit-config.yaml:L38-L43](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/.pre-commit-config.yaml#L38-L43) 的 `export-requirements` 钩子监听 `uv.lock` 变化并自动运行导出命令。不走 pre-commit 时需要手动执行该导出命令。

## 5. 综合实践

**任务：产出一份你自己的《open-instruct 环境体检报告》。**

在你刚装好的环境里依次完成，并把结果填进表格：

| 检查项 | 命令 / 阅读位置 | 你的记录 |
|---|---|---|
| Python 版本 | `uv run python -c "import sys; print(sys.version)"` | 应为 3.12.x |
| torch 及 CUDA 变体 | `uv run python -c "import torch; print(torch.__version__)"` | 应含 cu128（默认 cuda12 组） |
| uv 装了哪些组 | 查看 `pyproject.toml` 的 `default-groups` 并对比 `.venv` 中是否有 pytest/ruff | dev + cuda12 |
| transformer-engine 不在本地 | `uv run python -c "import transformer_engine"` | 应报 ModuleNotFoundError（TE 只随 -moe 镜像分发） |
| 单测结果 | `uv run pytest open_instruct/test_math_utils.py -v` | 9 passed（待本地验证） |
| 全量 CPU 测试（可选） | `uv run pytest`（约数分钟） | 记录 passed/skipped 数量 |
| 格式化 | `make style` | 记录改写了几个文件 |
| 质量门禁 | `make quality` | 退出码应为 0 |
| 门禁有效性验证 | 新增含未使用 import 的探针文件再跑 `make quality` | 应失败并报 F401 |
| 镜像变体对照 | 阅读 `Dockerfile` L1-L12 与 L88-L112 | 填出 4.3.4 的组件对照表 |
| 文档站（可选） | `uv run mkdocs serve` 后浏览器打开 http://127.0.0.1:8000/ | 能看到 Open Instruct 文档站 |

做完后回答三个思考题（答案都在本讲源码里）：

1. 如果明天要在 CUDA 13 的集群上跑实验，你的安装命令和 Docker 构建命令分别要怎么改？如果跑的还是 MoE v2 模型（如 Olmo 3.5 Small）呢？
2. `make quality` 里四道检查（关键字、lint、编译、类型）各自能拦截哪类错误？举一个其他三道拦不住、只有某一道能拦的例子。
3. `-moe` 变体把 venv 里的 cuBLAS 换成了系统副本，为什么这必须做成独立镜像变体，而不是默认 cuda13 镜像的一个补丁？

（参考答案：1. `uv sync --group cuda13`，`./scripts/train/build_image_and_launch.sh --cuda-version 13 <script>`；MoE v2 模型再追加 `--moe`，即 `--cuda-version 13 --moe`。2. 例如「调用了不存在的属性」只有 ty 能在运行前发现，ruff/compileall 都不管类型。3. 替换改变了镜像内**所有**程序加载的 cuBLAS，影响面波及全镜像，所以隔离成独立变体、让默认镜像保持原样（[Dockerfile:L88-L95](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L88-L95) 注释）。）

## 6. 本讲小结

- open-instruct 用 **uv + `pyproject.toml` + `uv.lock`** 管理依赖：Python 钉死 3.12，默认装 `dev + cuda12` 两组，`cuda12`/`cuda13` 通过 `conflicts` 互斥、通过 `tool.uv.sources` 切换 torch/vllm 的 wheel 索引。
- **平台差异用 marker 声明**：vllm、bitsandbytes、flash-attn 等不进 macOS 环境，一份声明适配多平台。
- **`requirements.txt` 是 `uv.lock` 的自动导出物**（pip 路线用），由 pre-commit 钩子保持同步，不要手改。
- **Dockerfile 一个文件出三种镜像**：`ARG CUDA_VERSION`（12/13）选 CUDA 代际与依赖组，`ARG IMAGE_VARIANT`（空/`-moe`）叠加 MoE 变体；依赖层与代码层分离以利用缓存；镜像内同样用 uv + `--frozen`，与本地同源。
- **`-moe` 变体专供 MoE v2（`OLMoDDPModel`，如 Olmo 3.5 Small）**：13.3.1-cudnn 基座 + transformer-engine 2.16.1（绑定 venv 的 torch 编译）+ `DS_SKIP_CUDA_CHECK=1` + cuBLAS 指向系统副本 + `nm` 构建期符号自检；全部收在一条 `if` 守卫的 RUN 里，`ENV` 也只属于 cuda13-moe 阶段，**两个默认镜像完全不受影响**；运行期另有 `require_transformer_engine` 启动检查兜底。
- **质量工具链三条命令**：`uv run pytest`（conftest 自动跳过缺 vllm/GPU 的用例）、`make style`（ruff format 改写排版）、`make quality`（关键字检查 + ruff lint + compileall + ty）。
- **文档可能滞后**（Python 3.10 vs 3.12、不存在的 `Dockerfile.uv`），冲突时以 `pyproject.toml`、`uv.lock`、`Dockerfile` 为准。

## 7. 下一步学习建议

环境就绪后，建议：

1. 下一讲 **u1-l3 仓库目录结构导览**：搞清楚 `open_instruct/`、`scripts/`、`configs/`、`docs/`、`tests/` 各放什么，为后续源码精读建立地图。
2. 再下一讲 **u1-l4 跑通第一个训练**：用本讲装好的环境 + `build_image_and_launch.sh`（含 `--cuda-version 13 --moe` 变体的使用场景），跑一次 debug 冒烟脚本，第一次见到真实的训练日志。
3. 想深挖 `-moe` 镜像的第一个消费者，看 **u7-l5（SFT 配方启动器）**：`scripts/train/sft.py` 的 `a4b` 配方固定使用该镜像训练 Olmo 3.5 Small。
4. 提前浏览 [docs/get_started/installation.md](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/docs/get_started/installation.md) 与 [CONTRIBUTING.md](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/CONTRIBUTING.md) 的测试一节，本讲的 pytest/质量门禁结论在那里有官方口径。
5. 如果你在 macOS 上开发，读一下 [CLAUDE.md](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/CLAUDE.md) 中关于 Darwin 平台 vllm 导入限制与 `--moe` 启动约定的说明——它解释了本讲 4.1.4 第 4 步的现象和 4.3 节的启动命令。

# 环境搭建：uv、Docker 与开发工具链

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `uv sync` 在本地装好 open-instruct 的完整开发环境，并说清楚它到底装了什么、为什么这么装。
2. 读懂 `pyproject.toml` 中的依赖分组（`dev` / `cuda12` / `cuda13`）与 `tool.uv.sources` 的 CUDA 版本切换机制，理解同一份依赖声明如何支撑不同 GPU 平台。
3. 读懂 `Dockerfile` 的双 CUDA 阶段构建，理解训练镜像为什么长这样。
4. 掌握日常开发三板斧：`uv run pytest` 跑测试、`make style` 格式化、`make quality` 过质量门禁，并知道它们背后分别是 ruff、ty、compileall 等哪些工具。

本讲不涉及任何模型训练，只解决一件事：**把环境搭对，把工具链用起来**。这是后面所有讲义的前置条件。

## 2. 前置知识

### 2.1 依赖管理器：从 pip 到 uv

- **pip**：Python 最经典的包管理器，按 `requirements.txt` 逐个安装，但不负责「锁版本」和「建虚拟环境」。
- **虚拟环境（venv）**：一个隔离的 Python 解释器 + 依赖目录，避免不同项目之间依赖互相污染。传统流程是 `python -m venv .venv && source .venv/bin/activate`。
- **uv**：Astral 公司用 Rust 写的下一代 Python 包管理器，一条 `uv sync` 命令可以同时完成「下载指定版本的 Python 解释器 + 创建 `.venv` + 按 lock 文件精确安装所有依赖」，速度比 pip 快一个数量级。open-instruct 已经把 uv 作为主力安装方式（安装文档中标注为 preview）。
- **lock 文件（`uv.lock`）**：把「我想要 torch>=2.10」这种模糊声明解析成「装 torch 2.10.x 的某个具体 wheel 及其哈希」的精确快照。CI 和 Docker 构建都用 `--frozen` 模式，即「只按 lock 装，不许重新解析」，保证所有人环境一致。

### 2.2 Python、PyTorch 与 CUDA 的三角关系

深度学习环境最常见的坑：PyTorch 的 wheel（预编译包）是**按 CUDA 版本分别编译**的。同样是 `torch==2.10.0`，CUDA 12.8 的 Linux 版和 CUDA 13.0 的 Linux 版是两个不同的文件，来自不同的下载索引。所以「装哪个 torch」取决于「你的机器驱动支持哪代 CUDA」。open-instruct 用 uv 的**依赖分组（dependency groups）**把这两套 wheel 隔开，这是本讲的重点之一。

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
| [docs/get_started/installation.md](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/docs/get_started/installation.md) | 官方安装指南：pip / uv / Docker 三条路线 |
| [pyproject.toml](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/pyproject.toml) | 项目元数据、依赖声明、uv 分组与索引、ruff/ty/pytest 配置，**本讲最核心的文件** |
| [uv.lock](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/uv.lock) | uv 生成的精确锁文件，Docker 与 CI 用 `--frozen` 模式消费它 |
| [requirements.txt](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/requirements.txt) | 由 `uv export` 自动导出，供 pip 路线使用，**不是手写的** |
| [Dockerfile](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/Dockerfile) | 训练镜像入口：双 CUDA 阶段 + uv 安装依赖 |
| [Makefile](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/Makefile) | `make style` / `make quality` 等质量门禁目标 |
| [.pre-commit-config.yaml](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/.pre-commit-config.yaml) | git 提交前的自动检查钩子 |
| [conftest.py](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/conftest.py) | pytest 全局配置：没有 vllm/GPU 时自动跳过相关测试 |
| [open_instruct/test_math_utils.py](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/open_instruct/test_math_utils.py) | 本讲实践用的最小测试文件 |
| [scripts/train/build_image_and_launch.sh](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/scripts/train/build_image_and_launch.sh) | AI2 内部用户「构建镜像 + 提交 Beaker」的一键脚本 |
| [.github/workflows/tests.yml](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/.github/workflows/tests.yml) | CI 单测流程，展示官方推荐的环境装配顺序 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **依赖声明与 uv 分组**（`pyproject.toml` 为主）
2. **Dockerfile 构建入口**（镜像里如何复用同一套依赖声明）
3. **Makefile 质量工具链**（ruff / ty / pytest / pre-commit 如何串成门禁）

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

[pyproject.toml:L1-L6](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/pyproject.toml#L1-L6) 声明项目名并钉死 Python 版本为 3.12：

```toml
[project]
name = "open-instruct"
dynamic = ["version"]
description = "Train open, instruction-following language models"
readme = "README.md"
requires-python = "==3.12.*"
```

注意 `dynamic = ["version"]`：版本号不写死在文件里，而是由 git 标签动态生成（见后文 setuptools_scm）。这就是为什么 Docker 构建时要设置 `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_OPEN_INSTRUCT` 环境变量兜底。

[pyproject.toml:L7-L55](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/pyproject.toml#L7-L55) 是运行时依赖清单，几个值得留意的条目：

- [pyproject.toml:L32](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/pyproject.toml#L32)：`"torch>=2.10.0"` —— 只写版本下限，具体装哪个 CUDA 变体由后面的 `tool.uv.sources` 决定。
- [pyproject.toml:L38](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/pyproject.toml#L38)：`"vllm>=0.19.1; platform_system != 'Darwin'"` —— vllm 没有 macOS 版本，用平台标记排除。
- [pyproject.toml:L43-L45](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/pyproject.toml#L43-L45)：`flash-attn`、`liger-kernel` 等同样带平台标记；`flash-attn` 在 Apple Silicon（aarch64）上也不装。

**② 版本动态生成**

[pyproject.toml:L57-L65](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/pyproject.toml#L57-L65) 声明构建系统，并用 setuptools_scm 从 git 状态生成版本号写入 `open_instruct/_version.py`：

```toml
[build-system]
requires = ["setuptools", "setuptools_scm>=8"]
build-backend = "setuptools.build_meta"

[tool.setuptools_scm]
write_to = "open_instruct/_version.py"
```

**③ CUDA 分组切换的核心：tool.uv.sources 与索引**

这是本模块最关键的一段。[pyproject.toml:L83-L97](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/pyproject.toml#L83-L97) 为 torch 三件套按「组」指定下载索引：

```toml
torch = [
  { index = "pytorch-cu128", group = "cuda12" },
  { index = "pytorch-cu130", group = "cuda13" },
  { index = "pytorch-cu130", marker = "platform_system == 'Linux' and platform_machine == 'aarch64'" },
]
```

读法：当解析 `cuda12` 组时，torch 一律从 `pytorch-cu128` 索引（即 CUDA 12.8 wheel）获取；解析 `cuda13` 组时改从 `pytorch-cu130` 获取。这些索引定义在 [pyproject.toml:L105-L118](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/pyproject.toml#L105-L118)，都是 `explicit = true` 的专用索引（不会用于解析其他包）。[pyproject.toml:L78-L81](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/pyproject.toml#L78-L81) 对 `flash-attn` 用的是另一种写法——直接按组指向不同的预编译 wheel URL。[pyproject.toml:L99-L103](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/pyproject.toml#L99-L103) 则说明 vllm 的 PyPI 官方 wheel 基于 CUDA 12.8，`+cu130` 构建只发布在专用 wheel 索引上，所以只有 cuda13 组需要换源。

**④ uv 全局行为**

[pyproject.toml:L136-L154](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/pyproject.toml#L136-L154) 定义 uv 的全局行为，四行最有信息量：

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

[pyproject.toml:L156-L184](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/pyproject.toml#L156-L184)：

- `cuda12` 组（L157-L165）与 `cuda13` 组（L166-L173）：内容几乎相同（torch 三件套 + vllm + flash-attn 系列），区别只在 flash-attn-3 的 wheel 是 cu128 还是 cu130 构建。真正的版本切换靠前述 `tool.uv.sources` 完成。
- `dev` 组（L174-L184）：`pytest`、`ruff`、`ty`、`beaker-py`、`mkdocs-material`、`pre-commit` 等，全是开发工具，不进运行时环境也能跑训练。

**⑥ requirements.txt 从哪来**

[requirements.txt:L1-L2](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/requirements.txt#L1-L2) 的第一行就自曝了身份：

```text
# This file was autogenerated by uv via the following command:
#    uv export --format requirements-txt --no-hashes --all-extras --group cuda12 --no-emit-project --output-file requirements.txt
```

它是从 `uv.lock` 导出的 cuda12 视图，供 pip 安装路线（[installation.md:L6-L16](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/docs/get_started/installation.md#L6-L16)）使用。pre-commit 里还有个钩子保证 `uv.lock` 变化时它会被重新导出（见 4.3.3）。**不要手改这个文件。**

#### 4.1.4 代码实践

**实践目标**：用 `uv sync` 装好环境，并验证你装到的正是声明所描述的那套依赖。

**操作步骤**：

1. 确认已安装 uv（未安装可 `curl -LsSf https://astral.sh/uv/install.sh | sh`，这也是 [build_image_and_launch.sh:L102-L105](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/scripts/train/build_image_and_launch.sh#L102-L105) 使用的安装方式）。
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

**需要观察的现象**：

- `uv sync` 首次运行会下载一个 uv 托管的 Python 3.12 并创建 `.venv/` 目录；
- `sys.version` 输出 3.12.x；
- `torch.__version__` 在 cuda12 默认组下应带 `cu128` 字样（具体小版本以 lock 为准，待本地验证）。

**预期结果**：所有命令退出码为 0（macOS 上第 4 步的报错本身也是「符合预期」的结果，说明平台标记生效）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 torch 只写 `>=2.10.0`，而不直接在依赖里写死 CUDA 版本？
**答案**：因为同一个 torch 版本号对应多个按 CUDA 分别编译的 wheel。CUDA 变体的选择放在 [tool.uv.sources](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/pyproject.toml#L83-L97) 里按依赖组切换：cuda12 组走 `pytorch-cu128` 索引，cuda13 组走 `pytorch-cu130` 索引。版本约束管「哪个版本」，source 管「哪个构建」。

**练习 2**：裸跑 `uv sync`（不带任何参数）会安装哪些组？
**答案**：`dev` 和 `cuda12`，因为 [pyproject.toml:L140](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/pyproject.toml#L140) 设置了 `default-groups = ["dev", "cuda12"]`。所以本地开发开箱即得 pytest/ruff/ty 和 CUDA 12.8 栈。

**练习 3**：在一台 CUDA 13 集群的机器上应该怎么装环境？
**答案**：`uv sync --group cuda13`（dev 组仍是默认组之一会一起装上）。uv 的 `conflicts` 声明（[pyproject.toml:L141-L146](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/pyproject.toml#L141-L146)）保证 cuda12 与 cuda13 不会同时进入一个环境。

### 4.2 Dockerfile 构建入口

#### 4.2.1 概念说明

训练大模型通常不在笔记本上跑，而是提交到 GPU 集群（open-instruct 默认对接 Allen AI 的 Beaker）。集群环境不可控，所以要把「操作系统 + CUDA 工具链 + Python 依赖 + 仓库代码」整体打包成 Docker 镜像。open-instruct 的 `Dockerfile` 有两个鲜明特点：

1. **一个文件支持两代 CUDA**：用多阶段构建 + 构建参数，`docker build --build-arg CUDA_VERSION=12` 或 `13` 出不同的镜像。
2. **镜像内用 uv 装依赖**：镜像里不手写 pip 命令，而是复用 4.1 节的同一套 `pyproject.toml` + `uv.lock`，做到「本地环境和镜像环境来自同一份声明」。

#### 4.2.2 核心流程

镜像构建流程：

```text
docker build --build-arg CUDA_VERSION=12|13 .
    ↓
选择基础镜像：nvidia/cuda:12.8.1-devel-ubuntu22.04 或 13.0.3-devel-ubuntu22.04
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
COPY 仓库代码（configs/scripts/mason.py/open_instruct/oe-eval-internal）
    ↓
设置 PATH 指向 /stage/.venv/bin，容器内命令直接可用
```

#### 4.2.3 源码精读

**① 双 CUDA 阶段选择**

[Dockerfile:L1-L4](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/Dockerfile#L1-L4)：

```dockerfile
ARG CUDA_VERSION=12
FROM nvidia/cuda:12.8.1-devel-ubuntu22.04 AS cuda12
FROM nvidia/cuda:13.0.3-devel-ubuntu22.04 AS cuda13
FROM cuda${CUDA_VERSION}
```

前三行各定义一个阶段，最后一行用变量选择从哪个阶段继续构建——这是 Docker 的「条件基础镜像」惯用法。默认值是 12，AI2 的 Jupiter/Saturn 集群（CUDA 12.8 宿主驱动）就用默认；Holmes 等新集群用 `--build-arg CUDA_VERSION=13`。

**② 基础工具与集群设施**

[Dockerfile:L12-L22](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/Dockerfile#L12-L22) 安装 build-essential、git、make、nginx 等基础工具；[Dockerfile:L54-L64](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/Dockerfile#L54-L64) 安装固定版本（v1.5.235）的 Beaker CLI，供集群上提交任务使用。这些与 Python 无关，却是集群可运行性的保障。

**③ 拷入 uv 并装配依赖**

[Dockerfile:L66](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/Dockerfile#L66) 从官方镜像拷入 uv 0.8.6（CI 的 [tests.yml:L25-L27](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/.github/workflows/tests.yml#L25-L27) 也用同一版本，保持一致）。随后是整个镜像最核心的一步：

[Dockerfile:L76-L80](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/Dockerfile#L76-L80)：

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

[Dockerfile:L83-L87](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/Dockerfile#L83-L87) 拷贝仓库代码。其中 `COPY oe-eval-interna[l] oe-eval-internal/` 的方括号是 Docker 的 glob 技巧：目录存在时正常拷贝，不存在时也不让构建失败（CLAUDE.md 说明该目录是 `--try_launch_beaker_eval_jobs_on_weka` 实验所必需，缺失时需自行 clone）。[Dockerfile:L89-L94](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/Dockerfile#L89-L94) 把 git 提交号写进环境变量，并把 `/stage/.venv/bin` 放进 PATH——容器里直接敲 `python` 就是刚装好的环境。

**⑤ 文档中已失效的引用**

[installation.md:L41-L49](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/docs/get_started/installation.md#L41-L49) 提到「用 `Dockerfile.uv` 构建带 uv 的镜像」，但当前仓库已经没有这个文件——因为主 `Dockerfile` 本身就内置了 uv（见上文 L66、L76-L80）。同样，[installation.md:L4](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/docs/get_started/installation.md#L4) 说「使用 Python 3.10」，而 `pyproject.toml` 已钉到 3.12。读文档时请以源码为准，这是研究仓库的生存技能。

#### 4.2.4 代码实践

**实践目标**：不真正构建镜像（耗时且需要网络），通过读脚本掌握 CUDA 版本如何一路传递；有 Docker 环境的读者可实际构建。

**操作步骤（阅读路线）**：

1. 打开 [scripts/train/build_image_and_launch.sh](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/scripts/train/build_image_and_launch.sh)，先看 [L13-L34](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/scripts/train/build_image_and_launch.sh#L13-L34)：解析 `--cuda-version 12|13` 参数（默认 12），非法值直接报错退出。
2. 再看 [L75-L93](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/scripts/train/build_image_and_launch.sh#L75-L93)：`docker buildx build --build-arg CUDA_VERSION="$cuda_version" ...` 把版本传给 Dockerfile 的 L1。
3. 串起调用链：`--cuda-version 13`（脚本参数）→ `--build-arg CUDA_VERSION=13`（构建参数）→ `FROM cuda13`（基础镜像）→ `--group cuda13`（依赖组）→ torch 走 `pytorch-cu130` 索引。**同一个数字贯穿四处**，这就是「CUDA 版本一致性」的工程实现。

**操作步骤（可选，实机构建）**：

```bash
docker build --build-arg CUDA_VERSION=12 -t open_instruct_dev .
```

**需要观察的现象**：构建日志中依赖安装步骤出现 `uv run --frozen --no-default-groups --group dev --group cuda12`；由于依赖层有 BuildKit 缓存挂载，重复构建时该步骤明显加速。

**预期结果**：镜像构建成功，`docker run --rm open_instruct_dev python -V` 输出 Python 3.12.x。（构建耗时与网络相关，待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么镜像里安装依赖要用 `--no-default-groups` 再显式 `--group cuda${CUDA_VERSION}`，而不是直接依赖默认组？
**答案**：`default-groups = ["dev", "cuda12"]` 是写死的，无法随构建参数变化。镜像需要按 `CUDA_VERSION` 在 cuda12/cuda13 之间切换，所以关掉默认组、显式指定，同时保留 dev 组（[Dockerfile:L79](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/Dockerfile#L79)）。

**练习 2**：为什么依赖安装层（L76-L80）在 COPY 代码层（L83-L87）之前？
**答案**：Docker 按层缓存。把「改动频繁的代码」放在「几乎不变的依赖」之后，改代码重建镜像时可以直接复用依赖层缓存，只需重跑 COPY，大幅加速迭代。

**练习 3**：`COPY oe-eval-interna[l] oe-eval-internal/` 里方括号的作用是什么？
**答案**：`oe-eval-interna[l]` 是一个能匹配 `oe-eval-internal` 的 glob 模式；当源路径是字面量且不存在时 COPY 会报错，而是 glob 模式且无匹配时则静默通过。这样没 clone 该内部仓库的构建也不会失败（[Dockerfile:L87](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/Dockerfile#L87)）。

### 4.3 Makefile 质量工具链

#### 4.3.1 概念说明

研究代码库最怕「每个人一套风格」。open-instruct 用一个 28 行的 Makefile 把格式化、静态检查、测试统一成两条命令：

- `make style`：**格式化**（自动改写你的代码排版）；
- `make quality`：**质量检查**（lint 自动修复 + 语法编译检查 + 类型检查）。

配套还有 `-check` 变体（`style-check` / `quality-check`）：不改文件、只报告问题并以非零退出码失败，供 CI 使用。此外仓库用 pre-commit 把同样的检查挂到 `git commit` 上，用根目录 `conftest.py` 让 pytest 在缺少 vllm/GPU 的机器上自动跳过相关测试。

#### 4.3.2 核心流程

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

#### 4.3.3 源码精读

**① Makefile 的目标定义**

[Makefile:L1-L9](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/Makefile#L1-L9)：

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

[Makefile:L11-L14](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/Makefile#L11-L14) 定义 quality 门禁：

```makefile
quality: check-keywords
	uv run ruff check -q --fix $(check_dirs)
	uv run python -m compileall -qq $(check_dirs)
	uv run ty check
```

注意 `quality` 依赖 `check-keywords`，会先跑它。[Makefile:L26-L28](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/Makefile#L26-L28) 是这个特殊检查：

```makefile
check-keywords:
	@test -d open_instruct/
	@grep -rn --include='*.py' -E 'nonlocal[[:space:]]' open_instruct/; test $$? -eq 1
```

逻辑是：grep 查找 `nonlocal ` 关键字，要求其退出码为 1（即「什么都没找到」）——找到了反而算失败。`nonlocal` 在多进程/分布式训练代码里是雷区，所以干脆全面禁用。

**② 提交前钩子与 CI 保持同一套规则**

[.pre-commit-config.yaml:L20-L28](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/.pre-commit-config.yaml#L20-L28) 挂载 ruff 与 ruff-format 钩子（作用域同样是 `open_instruct/` 和 `*mason.py`）；[.pre-commit-config.yaml:L32-L37](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/.pre-commit-config.yaml#L32-L37) 的 `ban-keywords` 钩子直接复用 `make check-keywords`；[.pre-commit-config.yaml:L38-L43](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/.pre-commit-config.yaml#L38-L43) 的 `export-requirements` 钩子在 `uv.lock` 变化时自动重新导出 `requirements.txt`——这就是 4.1.3 里「该文件是自动生成的」的机制保障。

CI 侧，[.github/workflows/tests.yml:L28-L35](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/.github/workflows/tests.yml#L28-L35) 展示了官方装配顺序：`uv sync --frozen` → 下载 nltk `punkt_tab` → `uv run --frozen pytest`。你在本地照着做，就能最大程度复现 CI 行为。

**③ pytest 的环境自适应**

[conftest.py:L5-L22](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/conftest.py#L5-L22)：根目录的 conftest 先试探 vllm 是否可导入、CUDA 是否可用，然后把依赖它们的测试文件追加进 `collect_ignore`。效果是：在 没有 GPU 的笔记本上 `uv run pytest` 也不会因 vllm/GPU 用例报错，它们被静默跳过。

**④ 实践要用的测试文件**

[open_instruct/test_math_utils.py:L8-L24](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/open_instruct/test_math_utils.py#L8-L24) 是仓库里最小的测试之一：用 `parameterized` 把 9 组「LaTeX 字符串归一化」输入塞进同一个断言，检验 `math_utils.strip_string` 能否把 `\frac 1 2`、`\left(...\right)`、`50\%` 等写法归一成统一形式（这是数学答案判分的基础，u6-l3 会展开）。它只依赖 CPU 和纯 Python，是验证环境的首选用例。

#### 4.3.4 代码实践

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

- 第 1 步：`test_latex_normalization` 被 parameterized 展开成 **9 个**子用例（对应 L10-L20 的 9 行参数），全部 PASSED（待本地验证具体输出）；
- 第 2 步：ruff format 对干净仓库通常是 no-op，输出 `X files left unchanged` 之类；
- 第 3 步：四道检查依次通过，退出码 0；
- 第 4 步：ruff check 报出 `F401 unused-import`（或自动 fix 删掉这行），quality 以非零退出码失败——门禁确实在拦人。

**预期结果**：1-3 步全绿；第 4 步演示了「坏代码进不了仓库」的原因。

#### 4.3.5 小练习与答案

**练习 1**：`make style` 和 `make style-check` 有什么区别？分别什么时候用？
**答案**：[Makefile:L8-L9](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/Makefile#L8-L9) 的 `style` 会直接改写文件排版；[Makefile:L16-L17](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/Makefile#L16-L17) 的 `style-check` 用 `--check --diff` 只报告不改写、有问题就失败退出。本地开发用前者，CI 用后者。

**练习 2**：为什么在没有 GPU 的机器上 `uv run pytest` 不会因为 GPU 测试失败？
**答案**：根目录 [conftest.py:L13-L22](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/conftest.py#L13-L22) 在收集阶段检测 vllm 可用性与 `torch.cuda.is_available()`，把不满足条件的测试文件加入 `collect_ignore`，pytest 直接跳过收集。

**练习 3**：如果你改了 `pyproject.toml` 里的依赖并 `uv lock`，`requirements.txt` 会自动更新吗？
**答案**：会（前提是你装了 pre-commit 并执行了提交）。[.pre-commit-config.yaml:L38-L43](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/.pre-commit-config.yaml#L38-L43) 的 `export-requirements` 钩子监听 `uv.lock` 变化并自动运行导出命令。不走 pre-commit 时需要手动执行该导出命令。

## 5. 综合实践

**任务：产出一份你自己的《open-instruct 环境体检报告》。**

在你刚装好的环境里依次完成，并把结果填进表格：

| 检查项 | 命令 | 你的记录 |
|---|---|---|
| Python 版本 | `uv run python -c "import sys; print(sys.version)"` | 应为 3.12.x |
| torch 及 CUDA 变体 | `uv run python -c "import torch; print(torch.__version__)"` | 应含 cu128（默认 cuda12 组） |
| uv 装了哪些组 | 查看 `pyproject.toml` 的 `default-groups` 并对比 `.venv` 中是否有 pytest/ruff | dev + cuda12 |
| 单测结果 | `uv run pytest open_instruct/test_math_utils.py -v` | 9 passed（待本地验证） |
| 全量 CPU 测试（可选） | `uv run pytest`（约数分钟） | 记录 passed/skipped 数量 |
| 格式化 | `make style` | 记录改写了几个文件 |
| 质量门禁 | `make quality` | 退出码应为 0 |
| 门禁有效性验证 | 新增含未使用 import 的探针文件再跑 `make quality` | 应失败并报 F401 |
| 文档站（可选） | `uv run mkdocs serve` 后浏览器打开 http://127.0.0.1:8000/ | 能看到 Open Instruct 文档站 |

做完后回答两个思考题（答案都在本讲源码里）：

1. 如果明天要在 CUDA 13 的集群上跑实验，你的安装命令和 Docker 构建命令分别要怎么改？
2. `make quality` 里四道检查（关键字、lint、编译、类型）各自能拦截哪类错误？举一个其他三道拦不住、只有某一道能拦的例子。

（参考答案：1. `uv sync --group cuda13`，`./scripts/train/build_image_and_launch.sh --cuda-version 13 <script>`；2. 例如「调用了不存在的属性」只有 ty 能在运行前发现，ruff/compileall 都不管类型。）

## 6. 本讲小结

- open-instruct 用 **uv + `pyproject.toml` + `uv.lock`** 管理依赖：Python 钉死 3.12，默认装 `dev + cuda12` 两组，`cuda12`/`cuda13` 通过 `conflicts` 互斥、通过 `tool.uv.sources` 切换 torch/vllm 的 wheel 索引。
- **平台差异用 marker 声明**：vllm、bitsandbytes、flash-attn 等不进 macOS 环境，一份声明适配多平台。
- **`requirements.txt` 是 `uv.lock` 的自动导出物**（pip 路线用），由 pre-commit 钩子保持同步，不要手改。
- **Dockerfile 一个文件出两代 CUDA 镜像**：`ARG CUDA_VERSION` 选基础镜像、选依赖组，依赖层与代码层分离以利用缓存；镜像内同样用 uv + `--frozen`，与本地同源。
- **质量工具链三条命令**：`uv run pytest`（conftest 自动跳过缺 vllm/GPU 的用例）、`make style`（ruff format 改写排版）、`make quality`（关键字检查 + ruff lint + compileall + ty）。
- **文档可能滞后**（Python 3.10 vs 3.12、不存在的 `Dockerfile.uv`），冲突时以 `pyproject.toml`、`uv.lock`、`Dockerfile` 为准。

## 7. 下一步学习建议

环境就绪后，建议：

1. 下一讲 **u1-l3 仓库目录结构导览**：搞清楚 `open_instruct/`、`scripts/`、`configs/`、`docs/`、`tests/` 各放什么，为后续源码精读建立地图。
2. 再下一讲 **u1-l4 跑通第一个训练**：用本讲装好的环境 + `build_image_and_launch.sh`，跑一次 debug 冒烟脚本，第一次见到真实的训练日志。
3. 提前浏览 [docs/get_started/installation.md](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/docs/get_started/installation.md) 与 [CONTRIBUTING.md](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/CONTRIBUTING.md) 的测试一节，本讲的 pytest/质量门禁结论在那里有官方口径。
4. 如果你在 macOS 上开发，读一下 [CLAUDE.md](https://github.com/allenai/open-instruct/blob/896a734f5152f6092a1ae2c2bccb8195a70abc42/CLAUDE.md) 中关于 Darwin 平台 vllm 导入限制的说明——它解释了本讲 4.1.4 第 4 步的现象。

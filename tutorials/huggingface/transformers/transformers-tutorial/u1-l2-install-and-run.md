# 环境安装与首次运行

## 1. 本讲目标

上一讲（u1-l1）我们已经建立了认知坐标：transformers 是一个「模型定义框架」，而不是训练框架或推理引擎。本讲把这套认知落到**可运行的环境**上。读完本讲，你应当能够：

1. 说出 transformers 对 Python、PyTorch 的最低版本要求，并解释为什么。
2. 用 pip / uv / 源码 / 可编辑（editable）四种方式分别装好 transformers，并知道每种方式的适用场景。
3. 读懂 `setup.py` 中的依赖声明——区分「硬依赖」与「可选依赖（extras）」，理解 `transformers[torch]`、`transformers[audio]` 这类写法背后的 `extras` 字典。
4. 运行一段最小脚本验证安装成功，并理解模型下载缓存与离线模式。

本讲不涉及任何模型内部原理，只解决「把项目跑起来」这一件事。

## 2. 前置知识

在动手之前，先澄清几个初学者常混淆的概念：

- **Python 包管理器**：`pip` 是 Python 官方的包管理器；`uv` 是一个用 Rust 写的、更快的现代替代品，命令几乎兼容（`uv pip install ...`）。transformers 官方文档现在以 `uv` 为主示例，但 `pip` 同样完全可用。
- **虚拟环境（virtual environment）**：一个隔离的 Python 目录，里面的依赖不会污染系统 Python。强烈建议在虚拟环境里装 transformers，避免和别的项目冲突。
- **硬依赖 vs 可选依赖（extras）**：硬依赖是 transformers 运行**必需**的包（如 `tokenizers`、`safetensors`）；可选依赖是按需安装的（如 `torch`、`torchvision`），用 `transformers[torch]` 这种方括号语法触发。transformers 把大量重量级后端设计为「可选」，这正是 u1-l1 讲到的「集中化模型定义、轻量核心」理念在打包层面的体现。
- **stable 版本 vs 源码版本**：PyPI 上的 `transformers` 是稳定发布版；从 GitHub 源码安装的是最新开发版，功能最新但可能不稳定。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [docs/source/en/installation.md](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/installation.md) | 官方安装文档，给出版本要求、各安装命令、缓存与离线配置 |
| [setup.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py) | 打包脚本，声明全部依赖（`_deps`）、可选依赖组（`extras`）、硬依赖（`install_requires`）、Python 版本范围与 CLI 入口 |
| [pyproject.toml](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/pyproject.toml) | 工具配置（ruff 代码风格、pytest、ty 类型检查），不直接管安装依赖，但定义了 `target-version = "py310"` |
| [docs/source/en/quicktour.md](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/quicktour.md) | 五分钟上手文档，演示 pipeline 推理与生态库安装 |
| [README.md](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/README.md) | 项目首页，含精简版安装命令与 quickstart 示例 |
| [src/transformers/cli/transformers.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/cli/transformers.py) | `transformers` 命令行入口，注册 `chat`/`serve`/`download` 等子命令 |

## 4. 核心概念与源码讲解

### 4.1 安装方式全览（installation 文档）

#### 4.1.1 概念说明

transformers 的安装方式并不唯一。官方文档把「装好」拆成了几条路径，每条路径对应一种使用场景：

1. **基础安装**：只装核心库，最轻量。
2. **带 PyTorch 的安装**（`transformers[torch]`）：绝大多数用户的首选，因为模型推理/训练依赖 PyTorch。
3. **CPU-only 安装**：在没有 GPU 的机器上，避免下载巨大的 CUDA 版 PyTorch。
4. **源码安装**：需要最新、尚未发布的功能或修 bug。
5. **可编辑安装（editable）**：本地开发 transformers 自身时使用，修改源码即时生效。
6. **conda 安装**：习惯 conda 生态的用户。

理解这些方式的差异，比死记命令更重要——它们对应不同的「你今天到底要做什么」。

#### 4.1.2 核心流程

一条最小化的安装与验证流程如下：

```text
创建虚拟环境
   └─> 选 pip 或 uv 安装（基础 或 [torch]）
         └─> (可选) 装 CPU-only 版 torch
               └─> 运行 sentiment-analysis 验证命令
                     └─> 成功打印 label + score，即安装完成
```

版本要求是流程的起点。官方文档第一句话就钉死了底线：

> Transformers works with PyTorch. It has been tested on Python 3.10+ and PyTorch 2.4+.

也就是说：**Python ≥ 3.10**、**PyTorch ≥ 2.4**。这两个数字是硬门槛，下面 4.2 节会从 `setup.py` 里再次确认。

#### 4.1.3 源码精读

**版本要求**。installation.md 的首行声明了测试通过的版本组合（[docs/source/en/installation.md:23](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/installation.md#L23)），这句对应 setup.py 里的 `python_requires` 与 `"torch>=2.4"`。

**虚拟环境**。官方推荐用 `uv` 创建虚拟环境（[docs/source/en/installation.md:36-39](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/installation.md#L36-L39)）：

```bash
uv venv .env
source .env/bin/activate
```

**基础安装命令**（[docs/source/en/installation.md:48](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/installation.md#L48)）：

```bash
uv pip install transformers
```

注意：这里只装了核心库，**不会**自动装 PyTorch。如果你只跑纯文本的小模型又想省事，可以用 README 给的 `pip install "transformers[torch]"`（见 4.1 节末）一次性带上 PyTorch。

**CPU-only 安装**（[docs/source/en/installation.md:62-64](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/installation.md#L62-L64)）：先从 PyTorch 的 CPU 索引装 torch，再装 transformers：

```bash
uv pip install torch --index-url https://download.pytorch.org/whl/cpu
uv pip install transformers
```

**验证命令**（[docs/source/en/installation.md:69](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/installation.md#L69)）——这是判断「装没装好」的官方一句话：

```bash
python -c "from transformers import pipeline; print(pipeline('sentiment-analysis')('hugging face is the best'))"
# 期望输出: [{'label': 'POSITIVE', 'score': 0.9998...}]
```

它做了三件事：从 transformers 导入 `pipeline`；构造一个情感分析流水线（首次会从 Hub 下载默认小模型）；对一句话做推理并打印结果。能打印出 `label` 和 `score`，就说明核心库 + 后端都通了。

**源码安装**（[docs/source/en/installation.md:82](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/installation.md#L82)）：

```bash
uv pip install git+https://github.com/huggingface/transformers
```

这种方式拿到的是 main 分支最新代码，而非 PyPI 上的稳定版。

**可编辑安装**（[docs/source/en/installation.md:97-99](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/installation.md#L97-L99)）：

```bash
git clone https://github.com/huggingface/transformers.git
cd transformers
uv pip install -e .
```

`-e`（editable）不会把文件复制到 site-packages，而是把本地目录链接进 Python 的 import 路径——你改源码，下一次 `import transformers` 立刻反映出来。官方同时警告：**必须保留本地这个 transformers 文件夹**，删了就不能用了。

**conda 安装**（[docs/source/en/installation.md:117](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/installation.md#L117)）：

```bash
conda install conda-forge::transformers
```

**README 的精简版命令**。README 给出的「带 PyTorch」一键安装（[README.md:103](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/README.md#L103)）：

```bash
pip install "transformers[torch]"
```

以及对应的源码可编辑安装（[README.md:116](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/README.md#L116)）：`pip install '.[torch]'`（注意 `.[torch]` 表示「当前目录 + torch extras」）。

> 说明：installation.md 默认用 `uv pip`，README 与 quicktour 用 `pip`，二者只是包管理器不同，命令含义一致。

#### 4.1.4 代码实践

**实践目标**：在虚拟环境里完成一次基础安装并用官方命令验证。

**操作步骤**：

1. 在任意目录创建并激活虚拟环境：
   ```bash
   uv venv .env && source .env/bin/activate
   # 或纯 pip: python -m venv .env && source .env/bin/activate
   ```
2. 安装带 PyTorch 的 transformers：
   ```bash
   pip install "transformers[torch]"
   ```
3. 运行官方验证命令：
   ```bash
   python -c "from transformers import pipeline; print(pipeline('sentiment-analysis')('hugging face is the best'))"
   ```

**需要观察的现象**：首次运行会有一段下载进度条（在拉取默认情感分析小模型与分词器），随后打印结果。

**预期结果**：返回一个列表，形如 `[{'label': 'POSITIVE', 'score': 0.9998...}]`。看到 `label` 和 `score` 即说明安装成功。

**待本地验证**：具体的 `score` 数值会随默认模型版本略有浮动；若网络不通则需先配置镜像或离线缓存（见 4.3 节）。

#### 4.1.5 小练习与答案

**练习 1**：`uv pip install transformers` 和 `pip install "transformers[torch]"` 装出来的环境，最大区别是什么？

> **答案**：前者只装核心库（不包含 PyTorch）；后者额外装上 `torch` 与 `accelerate`（对应 `extras["torch"]`）。没有 PyTorch，绝大多数模型无法实际跑 forward。

**练习 2**：你在改 transformers 的源码，希望改完立刻生效，应该用哪种安装方式？

> **答案**：可编辑安装 `pip install -e .`（或 `uv pip install -e .`）。它把本地源码目录链接进 import 路径，修改即时生效；而普通安装会把文件复制到 site-packages，改源码不生效。

---

### 4.2 setup.py 依赖声明（extras 与硬依赖）

#### 4.2.1 概念说明

为什么 `pip install "transformers[torch]"` 能自动带上 PyTorch？答案藏在 `setup.py` 的 `extras` 字典里。`setup.py` 是 transformers 的打包脚本，它做了三件事：

1. 声明**全部依赖**及其版本约束（`_deps` 列表）。
2. 把这些依赖分组成若干**可选依赖组**（`extras`），如 `torch`、`vision`、`audio`、`dev`。
3. 指定少数**硬依赖**（`install_requires`），即哪怕不装任何 extras 也必须有的包。

这种设计让核心库保持轻量——`import transformers` 本身不强制拉 PyTorch、torchvision 这些大块头，只在用户明确需要时才安装。这与 u1-l1 讲的「集中化模型定义、把重量级后端留作可选」完全一致。

#### 4.2.2 核心流程

`setup.py` 中依赖从「声明」到「生效」的流程：

```text
_deps（原始字符串列表，含版本约束）
   └─> deps（字典：包名 -> 带版本的字符串）
         └─> extras（按用途分组：torch / vision / audio / ...）
               └─> install_requires（少数硬依赖，无 extras 也必装）
                     └─> setup(...) 把 extras_require / install_requires 交给 pip
```

另外，`_deps` 还会被一个自定义命令 `deps_table_update` 反向写回 `src/transformers/dependency_versions_table.py`，供运行时做依赖版本校验。

#### 4.2.3 源码精读

**Python 版本范围**。`setup.py` 用一个元组定义支持的 Python 版本（[setup.py:51](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L51)）：

```python
SUPPORTED_PYTHON_VERSIONS = (10, 14)  # 3.10 to 3.14
```

它会被转换成 `python_requires = ">=3.10.0"`（[setup.py:317-318](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L317-L318)），并传给 `setup()`（[setup.py:343](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L343)）。这就从源头拒绝了 Python 3.9 及更早版本——与 installation.md 的「Python 3.10+」相互印证。pyproject.toml 里 ruff 的 `target-version = "py310"`（[pyproject.toml:17](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/pyproject.toml#L17)）也再次确认了 3.10 这个基线。

**原始依赖列表 `_deps`**。所有依赖连同版本约束集中在此（[setup.py:72-164](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L72-L164)），其中关键的几条：

```python
"numpy>=1.17",                 # L98
"huggingface-hub>=1.5.0,<2.0", # L89
"safetensors>=0.8.0",          # L135
"tokenizers>=0.22.0,<=0.23.0", # L149
"torch>=2.4",                  # L150
```

`"torch>=2.4"` 正是 installation.md「PyTorch 2.4+」的来源。

**可选依赖分组 `extras`**（[setup.py:175-209](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L175-L209)）。核心几组：

```python
extras["torch"]  = deps_list("torch", "accelerate")                              # L177
extras["vision"] = deps_list("torchvision", "Pillow")                            # L178
extras["audio"]  = deps_list("torchaudio", "librosa", "pyctcdecode", "phonemizer") # L179
extras["video"]  = deps_list("av")                                               # L182
```

于是 `transformers[torch]` ≈ 装 torch + accelerate；`transformers[audio]` ≈ 装 torchaudio + librosa 等。还有一个聚合组 `all`（[setup.py:245-257](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L245-L257)）一次性包含 torch/vision/audio/video/timm/sentencepiece/tiktoken/chat_template 等，以及面向贡献者的 `dev`（[setup.py:259](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L259)）= `all` + testing + ja + sklearn。

**硬依赖 `install_requires`**（[setup.py:262-272](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L262-L272)）：

```python
install_requires = [
    deps["huggingface-hub"],
    deps["numpy"],
    deps["packaging"],
    deps["pyyaml"],
    deps["regex"],
    deps["tokenizers"],
    deps["typer"],
    deps["safetensors"],
    deps["tqdm"],
]
```

这 9 个包是「无论装不装 extras 都会被强制安装」的底线依赖。注意 `torch` **不**在其中——它是可选的。

**依赖表自动生成**。`DepsTableUpdateCommand` 会把 `_deps` 写成 `src/transformers/dependency_versions_table.py`（[setup.py:293-312](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L293-L312)，目标文件见 [setup.py:310](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L310)）。注释里强调：改了 `_deps` 之后要跑 `make fix-repo` 来刷新这张表（[setup.py:70-71](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L70-L71)）。这张表在运行时被 `dependency_versions_check.py` 用来校验实际安装的依赖版本是否满足要求。

**版本号与 CLI 入口**。在 `setup()` 调用中（[setup.py:325-357](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L325-L357)），几个关键字段值得记住：

- 版本：`version="5.15.0.dev0"`（[setup.py:327](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L327)）——`.dev0` 表示这是开发版。
- 包描述里直接写明它是「the model-definition framework」（[setup.py:330](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L330)），与 u1-l1 的定位完全呼应。
- CLI 入口：`entry_points={"console_scripts": ["transformers=transformers.cli.transformers:main"]}`（[setup.py:342](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L342)）——安装后你会得到一个 `transformers` 命令。

**CLI 入口实现**。这个 `transformers` 命令指向 `cli/transformers.py` 的 `main()`，它用 typer 注册了若干子命令（[src/transformers/cli/transformers.py:25-32](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/cli/transformers.py#L25-L32)）：

```python
app.command(name="chat")(Chat)
app.command(name="serve")(Serve)
app.command()(download)
app.command()(env)
app.command()(version)
```

也就是说，安装完成后你可以直接运行 `transformers env`（打印环境信息，常用于排查安装问题）、`transformers serve`（启动推理服务）、`transformers chat`（命令行聊天）等。

#### 4.2.4 代码实践

**实践目标**：用源码方式安装，并用 `transformers` CLI 验证安装信息。

**操作步骤**：

1. 克隆并做可编辑安装：
   ```bash
   git clone https://github.com/huggingface/transformers.git
   cd transformers
   pip install -e ".[torch]"
   ```
   注意 `.[torch]`：`.` 表示当前目录（源码），`[torch]` 表示同时装 torch 这一组 extras。
2. 查看 CLI 是否就绪：
   ```bash
   transformers version
   transformers env
   ```
3. （阅读型）打开 `setup.py`，定位 `extras["audio"]`（[setup.py:179](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L179)），对照 `extras["all"]`（[setup.py:245](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L245)），列出 `transformers[audio]` 与 `transformers[all]` 各会多装哪些包。

**需要观察的现象**：`transformers env` 会打印 Python、PyTorch、transformers 自身及一系列可选库的版本与是否可用（`is_*_available` 的运行时结果）。

**预期结果**：`transformers version` 打印 `5.15.0.dev0`（源码版）；`transformers env` 中 `torch` 一栏显示为已安装版本号。

**待本地验证**：`transformers env` 的具体输出行随安装的 extras 不同而不同。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `torch` 不在 `install_requires` 里，而在 `extras["torch"]` 里？

> **答案**：`install_requires` 是无条件的硬依赖。如果把 `torch` 放进去，任何 `pip install transformers`（哪怕只想用纯文本处理或 CPU 推理）都会被强制拉一个巨大的 PyTorch。放在 extras 里，让用户按需选择（CPU/CUDA 版、是否要 accelerate），保持了核心库的轻量。

**练习 2**：`transformers[all]` 和 `transformers[dev]` 的区别是什么？普通用户该选哪个？

> **答案**：`all` 聚合了 torch/vision/audio/video/timm/sentencepiece/tiktoken/chat_template 等运行所需的可选后端（[setup.py:245-257](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L245-L257)）；`dev` = `all` + testing + ja + sklearn（[setup.py:259](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L259)），面向贡献者，额外包含测试与代码质量工具。普通应用选 `all` 或更细的 `[torch]`；参与开发才选 `dev`。

---

### 4.3 quicktour 与环境验证（首次推理、缓存与离线）

#### 4.3.1 概念说明

装好之后，「首次运行」要解决两件事：跑通一条推理；理解模型从哪来、缓存在哪。

- **pipeline**：transformers 的高层推理入口，把「分词 → 模型 forward → 后处理」串成一步（下一讲 u1-l5 会深入）。本讲只用它来验证环境。
- **Hub 与缓存**：你 `pipeline(...)` 时用到的模型，默认从 Hugging Face Hub 下载，并缓存到本地某个目录。第二次加载就直接用缓存。
- **离线模式**：在内网/断网环境下，可以预先下载好模型，再设置环境变量强制只读缓存。

#### 4.3.2 核心流程

首次推理与缓存的生命周期：

```text
pipeline(task, model="...")
   └─> 首次：从 Hub 下载 model + tokenizer 到 HF_HUB_CACHE
         └─> 之后：命中本地缓存，不再联网
               └─> 设置 HF_HUB_OFFLINE=1 可强制只读缓存（断网可用）
```

quicktour 还建议安装一套 HF 生态库来覆盖更完整的工作流（数据集、评估、加速、视觉模型）。

#### 4.3.3 源码精读

**quicktour 的安装建议**。在装好 PyTorch 之后，quicktour 推荐一并安装生态库（[docs/source/en/quicktour.md:69](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/quicktour.md#L69)）：

```bash
pip install -U transformers datasets evaluate accelerate timm
```

这些是常用 companion 库：`datasets`（数据）、`evaluate`（指标）、`accelerate`（分布式/加速）、`timm`（视觉模型）。注意它们是独立项目，不是 transformers 的 extras，而是生态协作。

**quicktour 的核心抽象**。文档开篇点明 transformers 只暴露极少的用户抽象（[docs/source/en/quicktour.md:23](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/quicktour.md#L23)）：三类用于实例化模型的对象，以及推理/训练各一套 API。本讲只需记住其中一个——`Pipeline`。

**README 的 quickstart 示例**（[README.md:128-133](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/README.md#L128-L133)）：

```python
from transformers import pipeline

pipeline = pipeline(task="text-generation", model="Qwen/Qwen2.5-1.5B")
pipeline("the secret to baking a really good cake is ")
```

能跑出这段文本生成，就说明 torch + transformers + Hub 下载整条链路都正常。

**缓存目录**。installation.md 说明：模型默认缓存到 `HF_HUB_CACHE`，默认值 `~/.cache/huggingface/hub`（Windows 为 `C:\Users\username\.cache\huggingface\hub`），见 [docs/source/en/installation.md:130](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/installation.md#L130)。每次加载会检查缓存是否最新，相同就复用本地，不同就重新下载。

改变缓存位置的环境变量按优先级为（[docs/source/en/installation.md:134-136](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/installation.md#L134-L136)）：

1. `HF_HUB_CACHE`（默认）
2. `HF_HOME`
3. `XDG_CACHE_HOME` + `/huggingface`（仅当 `HF_HOME` 未设置时）

**离线模式**。设置 `HF_HUB_OFFLINE=1` 可阻止任何对 Hub 的 HTTP 请求（[docs/source/en/installation.md:151](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/installation.md#L151)）。也可以在 `from_pretrained` 里传 `local_files_only=True`，只读本地目录（[docs/source/en/installation.md:160-164](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/installation.md#L160-L164)）。提前下载可用 `huggingface_hub.snapshot_download`（[docs/source/en/installation.md:145-149](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/installation.md#L145-L149)）。

#### 4.3.4 代码实践

**实践目标**：跑通一次真实模型推理，观察缓存命中行为。

**操作步骤**：

1. 写一个最小脚本 `hello.py`（**示例代码**，非项目原有文件）：
   ```python
   from transformers import pipeline

   gen = pipeline(task="text-generation", model="Qwen/Qwen2.5-1.5B")
   print(gen("the secret to baking a really good cake is "))
   ```
2. 设置自定义缓存目录并运行：
   ```bash
   export HF_HOME=/tmp/hf_cache
   python hello.py
   ```
3. 查看缓存目录结构：
   ```bash
   ls -R /tmp/hf_cache
   ```
4. 再次运行同一脚本（不走离线），观察是否还有下载进度条。
5. 强制离线再跑一次：
   ```bash
   HF_HUB_OFFLINE=1 python hello.py
   ```

**需要观察的现象**：第 2 步首次运行有模型下载进度条；第 4 步几乎不再下载（命中缓存）；第 5 步即使断网也能正常生成。

**预期结果**：三次都能打印出一段 `generated_text`；缓存目录 `models--Qwen--Qwen2.5-1.5B` 下能看到 snapshots/blobs 等子目录。

**待本地验证**：生成内容是随机的，重点验证「第二次与离线模式能复现成功运行」这一行为，而非具体文本。

#### 4.3.5 小练习与答案

**练习 1**：你把 `HF_HOME` 指向了一个空目录，然后断网运行 `pipeline(...)`，会发生什么？怎么提前避免报错？

> **答案**：空目录意味着缓存里没有模型，断网又无法下载，会抛出连接/找不到文件的错误。避免方法是**先在联网时运行一次**（或用 `snapshot_download` 预下载），让模型进入缓存，之后再断网配合 `HF_HUB_OFFLINE=1` 运行。

**练习 2**：`pip install transformers datasets evaluate accelerate timm`（quicktour 写法）和 `pip install "transformers[torch]"`（README 写法）覆盖范围有何不同？

> **答案**：前者装 transformers 核心 + 四个**独立的生态库**（它们不是 transformers 的 extras）；后者装 transformers 核心 + torch/accelerate 这一组 **extras**。前者偏「工作流配套」（数据/评估/视觉），后者偏「保证 PyTorch 后端可用」。二者可以叠加使用。

---

## 5. 综合实践

把本讲三块知识串起来，完成一次「从零到可运行」的环境搭建：

1. **规划依赖**：打开 [setup.py:175-209](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/setup.py#L175-L209)，根据你接下来想做的事（纯文本推理 / 图像分类 / 开发 transformers 自身）选择一个 extras 组合（如 `[torch]`、`[torch,vision]` 或 `dev`），并写下你的选择理由。
2. **建虚拟环境并安装**：用 `uv venv` 或 `python -m venv` 建环境，用 `pip install "transformers[...]"` 安装；如果想体验最新功能，改用源码可编辑安装 `pip install -e ".[...]"`。
3. **验证三件套**：
   - 跑官方一句话验证命令（[installation.md:69](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/docs/source/en/installation.md#L69)）。
   - 运行 `transformers env`，确认 Python ≥ 3.10、PyTorch ≥ 2.4、transformers 为预期版本。
   - 跑一段 README 的 text-generation 示例（[README.md:128-133](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/README.md#L128-L133)），观察 Hub 下载与缓存。
4. **记录一份「环境说明书」**：写下你的 Python/PyTorch/transformers 版本、所选 extras、缓存目录（`HF_HOME`），以及离线运行的命令。这份文档会在后续每一讲复用。

完成本实践后，你就拥有了一个稳定、可复现、可离线的 transformers 运行环境。

## 6. 本讲小结

- transformers 要求 **Python ≥ 3.10**、**PyTorch ≥ 2.4**，这分别由 `setup.py` 的 `SUPPORTED_PYTHON_VERSIONS`/`python_requires` 和 `"torch>=2.4"` 把关。
- 安装方式有四种主流路径：基础安装、`[torch]` extras、源码安装、可编辑安装（`-e`）；CPU-only 与 conda 是两种特殊情形。
- `setup.py` 用 `_deps` 集中声明依赖，用 `extras` 分组（`torch`/`vision`/`audio`/`video`/`all`/`dev`），用 `install_requires` 列出 9 个硬依赖——核心库刻意保持轻量。
- 官方一句话验证命令 `pipeline('sentiment-analysis')(...)` 是判断「装好没」的最快手段。
- 安装后即获得 `transformers` CLI（指向 `cli/transformers.py:main`），含 `env`/`version`/`serve`/`chat` 等子命令。
- 模型默认从 Hub 下载并缓存到 `HF_HUB_CACHE`（默认 `~/.cache/huggingface/hub`），可用 `HF_HUB_OFFLINE=1` 或 `local_files_only=True` 走纯离线。

## 7. 下一步学习建议

环境就绪后，建议按以下顺序继续：

- **u1-l3 源码目录结构地图**：先建立对 `src/transformers/` 下各子系统的全局认知，再读代码就不会迷路。
- **u1-l4 库入口与惰性导入机制**：理解 `import transformers` 背后的 `_LazyModule`，弄清为什么核心库能这么轻。
- **u1-l5 五分钟上手 pipeline API**：本讲只把 pipeline 当验证工具，下一讲会拆解它内部 tokenization → model → postprocess 的三段式结构。

如果想立刻动手，可以在本讲环境里直接尝试 quicktour 的 `Trainer` 微调示例，但更系统的训练讲解在 u9 单元。

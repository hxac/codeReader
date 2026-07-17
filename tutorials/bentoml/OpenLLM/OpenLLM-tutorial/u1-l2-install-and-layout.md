# 安装、目录结构与运行方式

## 1. 本讲目标

学完本讲，你应当能够：

- 用两种方式把 `openllm` 装到机器上：从 PyPI 直接安装、从源码做开发安装（editable）。
- 读懂 `pyproject.toml`，说清楚 `[project.scripts]` 与 hatch 构建配置是如何「凭空」生成 `openllm` 这条命令的。
- 说清楚 `src/openllm/` 下每个文件的职责，画出「一条命令 → 哪个模块」的映射。
- 理解 `OPENLLM_HOME`（默认 `~/.openllm`）里 `repos`、`temp`、`venv`、`config.json` 各自是什么、何时被创建。

本讲承接 [u1-l1 项目定位](./u1-l1-project-overview.md)：上一讲明确了「OpenLLM 是编排者，自身不存权重」，这一讲就走进它的**安装入口、源码布局和运行时落盘位置**。

## 2. 前置知识

- **Python 包与入口脚本**：用 `pip install` 装一个工具时，pip 会根据包元数据里的 `[project.scripts]` 在 `bin/`（Windows 是 `Scripts/`）下生成一个可执行的小包装脚本，让你能像运行系统命令一样运行它。本讲要看 OpenLLM 是怎么声明这个入口的。
- **构建后端（build backend）**：`pyproject.toml` 里的 `[build-system]` 决定了一个 Python 项目「怎么被打成 wheel」。OpenLLM 用的是 [hatch](https://hatch.pypa.io/) 系列（hatchling + hatch-vcs），其中 hatch-vcs 会**从 git 标签推导版本号**。
- **src layout**：把真正的代码放在 `src/包名/` 下（而不是仓库根目录），可以避免「测试时误把当前目录当代码」的常见坑。OpenLLM 用的就是 src layout。
- **运行时目录（runtime home）**：很多 CLI 工具会在用户主目录下建一个文件夹存缓存、配置、临时文件（如 `~/.cache`、`~/.config`）。OpenLLM 的这个目录叫 `OPENLLM_HOME`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pyproject.toml` | 项目元数据：依赖、Python 版本要求、**控制台入口脚本**、构建配置。 |
| `README.md` | 面向用户的安装与上手说明（`pip install openllm`）。 |
| `DEVELOPMENT.md` | 面向贡献者的开发环境搭建步骤。 |
| `src/openllm/__init__.py` | 包标记文件（空文件），让 Python 把 `src/openllm` 识别为 `openllm` 包。 |
| `src/openllm/__main__.py` | CLI 总入口：定义 `app`、注册子命令、`--version` 回调。 |
| `src/openllm/common.py` | 公共基础设施：定义 `OPENLLM_HOME` 及各运行时子目录常量、`config.json` 读写。 |
| `src/openllm/repo.py` | 模型仓库子命令，是少数会**写** `config.json` 的地方。 |

## 4. 核心概念与源码讲解

### 4.1 安装与开发环境搭建

#### 4.1.1 概念说明

拿到 OpenLLM 有两条路：

1. **作为用户安装**：从 PyPI 装，得到一条可全局调用的 `openllm` 命令。README 的「Get Started」就是这条路。
2. **作为贡献者安装**：把仓库 clone 下来，做「可编辑安装」（editable install），改源码立即生效，用于二次开发和调试。

两条路最终都落到同一个入口脚本 `openllm` 上，区别只是包从哪来、版本号怎么定。

#### 4.1.2 核心流程

```text
用户路线：
  pip install openllm
    └─ pip 从 PyPI 拉 wheel
       └─ 安装时读取 [project.scripts]
          └─ 在 bin/ 生成 openllm 包装脚本 → 调用 openllm.__main__:app

贡献者路线：
  git clone … && cd OpenLLM
    └─ pip install -e .          # 可编辑安装
       └─ hatch-vcs 从 git 标签算出版本号
          └─ 生成 src/openllm/_version.py 并写入 wheel 元数据
             └─ 同样生成 openllm 入口脚本
```

注意一个**容易踩坑的细节**：`--version` 在运行时并不是去读 `_version.py`，而是通过 Python 标准库 `importlib.metadata` 读取**已安装包的元数据**。这意味着你必须把项目「装」上（哪怕是 editable 安装），`openllm --version` 才能拿到版本号；只 clone 不安装、直接 `python src/...` 是拿不到的。

#### 4.1.3 源码精读

README 给出的最短上手路径就两行——先装、再跑 `hello`：[README.md:21-24](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md#L21-L24)（中文说明：用户路线的官方入口，`pip install openllm` 之后直接 `openllm hello`）。

「`openllm` 这条命令从哪来」由 `pyproject.toml` 的这一段决定：[pyproject.toml:73-74](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/pyproject.toml#L73-L74)（中文说明：声明控制台脚本 `openllm`，它指向 `openllm.__main__` 模块里的 `app` 对象）。

```toml
[project.scripts]
openllm = "openllm.__main__:app"
```

而 Python 版本门槛写在这里：[pyproject.toml:71](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/pyproject.toml#L71)（中文说明：要求 `requires-python >= 3.9`）。

> [!NOTE]
> 版本要求要特别留意：`pyproject.toml` 写的是 `>=3.9`，而 `DEVELOPMENT.md` 文字里写的是「Python 3.8+」。以 `pyproject.toml` 为准——它才是实际安装时 pip 校验的依据。

依赖清单在这里（注意 `bentoml`、`openai` 被**锁死版本**，呼应上一讲说的强依赖）：[pyproject.toml:32-48](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/pyproject.toml#L32-L48)。

贡献者路线的步骤在开发指南里：[DEVELOPMENT.md:20-51](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/DEVELOPMENT.md#L20-L51)（中文说明：从 fork、clone、添加 upstream remote 到（可选）链接 `.python-version` 的完整流程）。其中一步是：

```bash
git clone git@github.com:username/OpenLLM.git && cd openllm
```

版本号的来源（hatch-vcs 从 git 推导）写在这里：[pyproject.toml:94-98](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/pyproject.toml#L94-L98)（中文说明：`source = "vcs"` 表示版本来自 git，`version-file` 指定构建期生成 `src/openllm/_version.py`）。注意：在原始仓库里**看不到** `_version.py`，它是构建时才生成的。

`--version` 运行时实际读的是已安装包元数据，不是那个文件：[src/openllm/__main__.py:362-366](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L362-L366)（中文说明：`--version`/`-v` 触发时，用 `importlib.metadata.version("openllm")` 取版本并退出）。

#### 4.1.4 代码实践

1. **实践目标**：亲手把 `openllm` 装上，验证入口脚本可用。
2. **操作步骤**（二选一）：
   - 用户路线：`pip install openllm`（或用 `uv tool install openllm`）。
   - 贡献者路线：按 [DEVELOPMENT.md:20-51](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/DEVELOPMENT.md#L20-L51) clone 后，在仓库根目录执行 `pip install -e .`。
3. **运行**：`openllm --version` 和 `openllm --help`。
4. **预期结果**：
   - `--version` 输出形如 `openllm, <版本号>` 加一行 Python 实现与版本（格式见 [__main__.py:364](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L364)）。**具体版本号待本地验证**（取决于当前发布版本）。
   - `--help` 列出顶层命令。
5. 若直接 `python src/openllm/__main__.py --version` 报取不到版本，正是上面说的「没装就读不到元数据」——这本身就是一个验证点。

#### 4.1.5 小练习与答案

**Q1**：为什么 `openllm` 命令能在终端直接敲，而不需要写 `python -m openllm`？
**A**：因为 `pyproject.toml` 的 `[project.scripts]` 声明了 `openllm = "openllm.__main__:app"`，pip 安装时会在可执行路径下生成同名的包装脚本，它内部去调用 `openllm.__main__` 里的 `app`。

**Q2**：仓库里搜不到 `_version.py`，但 `openllm --version` 能输出版本，为什么？
**A**：`_version.py` 由 hatch-vcs 在**构建期**根据 git 标签生成，原始仓库里本就没有；而 CLI 的 `--version` 走 `importlib.metadata.version("openllm")`，读的是已安装包的元数据，只要把项目安装过就能取到。

### 4.2 src/openllm 模块布局

#### 4.2.1 概念说明

OpenLLM 采用 **src layout**：真正的包是 `src/openllm/`，安装后才叫 `openllm`。`src/openllm/__init__.py` 是一个**空文件**（0 字节），它唯一的作用是告诉 Python「这个目录是一个包」。包里一共 9 个 `.py` 模块，**几乎一个文件一个职责**，这正是后续讲义能「一篇对应一个文件」的根本原因。

#### 4.2.2 核心流程

```text
入口 openllm (来自 [project.scripts])
   └─ openllm.__main__.app   ← 总装车间：定义 app、注册子命令
        ├─ repo_app   (来自 repo.py)   → openllm repo …
        ├─ model_app  (来自 model.py)  → openllm model …
        ├─ clean_app  (来自 clean.py)  → openllm clean …
        └─ 顶层命令 hello/serve/run/deploy 直接写在 __main__.py
```

模块职责一览：

| 文件 | 职责（一句话） |
| --- | --- |
| `__init__.py` | 空的包标记。 |
| `__main__.py` | CLI 总入口、顶层命令、全局回调。 |
| `common.py` | 公共基础设施：配置、输出、子进程、运行时目录。 |
| `analytic.py` | 自定义 `OpenLLMTyper`，给每条命令加使用埋点。 |
| `repo.py` | 模型仓库（git 形式）的增删改查。 |
| `model.py` | 把模型名解析为可运行的 Bento。 |
| `accelerator_spec.py` | GPU 探测与「能否跑得动」打分。 |
| `venv.py` | 为每个 Bento 准备独立虚拟环境。 |
| `local.py` | 本地 `serve`/`run` 全链路。 |
| `cloud.py` | 部署到 BentoCloud。 |
| `clean.py` | 清理缓存与配置。 |

（其中 `py.typed` 是 [PEP 561](https://peps.python.org/pep-0561/) 标记，表示本包带类型标注，可空。）

#### 4.2.3 源码精读

「src layout 如何映射成安装后的包名」由 wheel 构建目标决定：[pyproject.toml:112-114](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/pyproject.toml#L112-L114)（中文说明：只打包 `src/openllm`，并把源路径 `src` 映射为安装后的顶层包 `openllm`）。

```toml
[tool.hatch.build.targets.wheel]
only-include = ["src/openllm"]
sources = ["src"]
```

入口脚本指向的 `app` 是一个 `OpenLLMTyper` 实例（它是 `typer.Typer` 的子类，详见 u3-l3）：[src/openllm/__main__.py:19-23](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L19-L23)（中文说明：创建顶层 `app`，帮助文本提示用户 `openllm hello` 上手）。

三个子命令组在这里被挂载上去：[src/openllm/__main__.py:25-27](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L25-L27)（中文说明：用 `add_typer` 把 `repo`/`model`/`clean` 三个子命令组注册到主 app，名字分别是 `repo`、`model`、`clean`）。

```python
app.add_typer(repo_app, name='repo')
app.add_typer(model_app, name='model')
app.add_typer(clean_app, name='clean')
```

#### 4.2.4 代码实践

1. **实践目标**：建立「命令 ↔ 模块」的映射直觉。
2. **操作步骤**：
   - 运行 `openllm --help`，记下列出的顶层命令与子命令组。
   - 打开 [src/openllm/__main__.py:1-27](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L1-L27)，对照 import 语句把每个子命令组对应到文件（`repo_app`→`repo.py`、`model_app`→`model.py`、`clean_app`→`clean.py`）。
3. **需要观察的现象**：`--help` 输出的命令树与源码里 `add_typer` 的注册完全一一对应。
4. **预期结果**：你能画出一张命令树（根 `openllm` 下有 `hello`/`serve`/`run`/`deploy` 四个顶层命令，外加 `repo`/`model`/`clean` 三个子命令组）。

#### 4.2.5 小练习与答案

**Q1**：`src/openllm/__init__.py` 是空文件，删掉会怎样？
**A**：在 src layout 下，删掉它后 `src/openllm` 不再被识别为（常规）包，构建与 `import openllm` 都会出问题。它的存在本身就是包标记。

**Q2**：为什么安装后包叫 `openllm`，而源码在 `src/openllm/`？
**A**：因为 `pyproject.toml` 的 wheel 目标里写了 `sources = ["src"]`，构建时把 `src/` 剥掉，于是 `src/openllm` 变成安装后的顶层包 `openllm`。

### 4.3 OPENLLM_HOME 运行时目录

#### 4.3.1 概念说明

上一讲强调「OpenLLM 不存模型权重」，但它**确实**会在本地维护一个工作目录，用来放：仓库缓存（`repos`）、为每个 Bento 准备的虚拟环境（`venv`）、临时文件（`temp`）、配置（`config.json`）。这个目录叫 `OPENLLM_HOME`，默认是 `~/.openllm`，也可以用同名环境变量覆盖。

#### 4.3.2 核心流程

```text
import openllm.common（任意 openllm 命令都会触发）
  ├─ OPENLLM_HOME = $OPENLLM_HOME or ~/.openllm
  ├─ REPO_DIR = OPENLLM_HOME/repos   ┐
  ├─ TEMP_DIR = OPENLLM_HOME/temp    ├─ 立即 mkdir（exist_ok）
  ├─ VENV_DIR = OPENLLM_HOME/venv    ┘
  └─ CONFIG_FILE = OPENLLM_HOME/config.json   ← 仅「读」，不主动创建

config.json 何时出现？
  仅当 repo add / repo remove 调用 save_config() 时才落盘
```

各目录「何时被创建」是本讲最容易考的细节，整理成表：

| 路径 | 何时被创建 | 由谁创建 |
| --- | --- | --- |
| `OPENLLM_HOME`（`~/.openllm`） | 首次运行任意 `openllm` 命令 | `common.py` 的 `mkdir` |
| `repos/`、`temp/`、`venv/` | 首次运行任意命令（导入 `common` 时立即建） | `common.py:22-24` |
| `repos/<server>/<owner>/<repo>/<branch>/` | 执行 `repo update` 或首次列出/拉取模型时克隆 | `repo.py` 的 `_clone_repo` |
| `venv/<hash>/` | `serve`/`run` 某个 Bento、为其准备环境时 | `venv.py` |
| `config.json` | 第一次执行会**修改**配置的命令（`repo add`/`repo remove`） | `repo.py` 调用 `save_config` |

> [!IMPORTANT]
> `config.json` **不会**在你第一次敲 `openllm` 或 `openllm model list` 时出现。`load_config()` 在文件不存在时只返回内存里的默认 `Config()`（默认含 `default`、`nightly` 两个仓库），并不会写盘；只有真正改动配置时才落盘。所以「刚装完看不到 config.json」是正常的。

#### 4.3.3 源码精读

运行时根目录与三个子目录常量的定义：[src/openllm/common.py:16-24](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L16-L24)（中文说明：用环境变量 `OPENLLM_HOME` 覆盖默认 `~/.openllm`，并定义 `repos`/`temp`/`venv` 三个子目录常量，紧接着用 `mkdir(exist_ok=True)` 在**导入时就创建**它们）。

```python
OPENLLM_HOME = pathlib.Path(os.getenv('OPENLLM_HOME', pathlib.Path.home() / '.openllm'))
REPO_DIR = OPENLLM_HOME / 'repos'
TEMP_DIR = OPENLLM_HOME / 'temp'
VENV_DIR = OPENLLM_HOME / 'venv'

REPO_DIR.mkdir(exist_ok=True, parents=True)
TEMP_DIR.mkdir(exist_ok=True, parents=True)
VENV_DIR.mkdir(exist_ok=True, parents=True)
```

配置文件路径与默认配置结构：[src/openllm/common.py:26](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L26) 与 [src/openllm/common.py:74-81](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L74-L81)（中文说明：`CONFIG_FILE` 指向 `config.json`；`Config` 默认带 `default`（main 分支）和 `nightly` 两个仓库，`default_repo` 为 `default`）。

「文件不存在就返回默认、不报错」的读取逻辑：[src/openllm/common.py:87-94](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L87-L94)（中文说明：`load_config` 在 `config.json` 缺失或 JSON 损坏时返回默认 `Config()`，因此首次运行不会创建该文件）。

真正会落盘的写入逻辑：[src/openllm/common.py:97-99](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L97-L99)（中文说明：`save_config` 把配置以 JSON 写入 `config.json`）。

那 `save_config` 又被谁调用？仓库管理里的两个命令：[src/openllm/repo.py:31-42](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L31-L42)（`repo remove` 删除仓库后保存）与 [src/openllm/repo.py:82-110](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L82-L110)（`repo add` 新增仓库后保存）。所以 **`config.json` 首次出现**通常是在你第一次 `openllm repo add …` 之后。

仓库内容在 `repos/` 下的目录约定（`server/owner/repo/branch` 四级）：[src/openllm/repo.py:257](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L257)（中文说明：克隆目标路径按 git URL 的 server/owner/repo/branch 拼接，这也是 `list_repo` 用 `glob('*/*/*/*')` 扫描的原因）。

虚拟环境目录的命名（预告 u2-l6）：[src/openllm/venv.py:40](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L40)（中文说明：每个 Bento 的 venv 放在 `VENV_DIR / str(hash(venv_spec))`，用依赖规格的哈希当目录名以实现复用）。

#### 4.3.4 代码实践

1. **实践目标**：亲眼确认各运行时目录/文件「何时出现」。
2. **操作步骤**（找一个干净环境，逐步观察）：
   - 先删除工作目录：`rm -rf ~/.openllm`（确认无重要数据后再删）。
   - 第 1 步：`openllm --version`，然后 `ls ~/.openllm`。
   - 第 2 步：`openllm model list`，再次 `ls ~/.openllm` 和 `ls ~/.openllm/repos`。
   - 第 3 步：`openllm repo add mytmp https://github.com/bentoml/openllm-models@main`（用公开 URL，会提示 `mytmp` 已存在则选择不覆盖也无妨；若 `mytmp` 不存在则直接添加成功），再看 `cat ~/.openllm/config.json`。
3. **需要观察的现象**：
   - 第 1 步后：`~/.openllm` 已存在，里面有 `repos`、`temp`、`venv`，但**没有** `config.json`。
   - 第 2 步后：`repos/` 下出现克隆出来的仓库目录（四级路径）。
   - 第 3 步后：`config.json` 才首次出现，内容含 `repos`（含你新加的 `mytmp`）与 `default_repo`。
4. **预期结果**：与你从 `common.py:22-24` 和 `repo.py` 推断的「创建时机」完全吻合。
5. 若你机器上环境受限，无法实际克隆仓库，可只做第 1 步，并把第 2、3 步标注为「待本地验证」——仅靠源码也能得出上表的结论。

#### 4.3.5 小练习与答案

**Q1**：刚装好 OpenLLM，第一次运行 `openllm hello`，`~/.openllm` 里能看到 `config.json` 吗？为什么？
**A**：看不到。`config.json` 只在 `save_config` 被调用时才写盘，而 `save_config` 只在 `repo add`/`repo remove` 里调用；`hello` 只读取默认配置，不写盘。

**Q2**：想换一个目录存 OpenLLM 的缓存，怎么做？
**A**：设置环境变量 `OPENLLM_HOME` 指向目标目录即可，见 `common.py:16`——它优先读 `os.getenv('OPENLLM_HOME')`，读不到才用 `~/.openllm`。

**Q3**：`repos` 子目录里为什么是 `server/owner/repo/branch` 这样的多级路径？
**A**：因为模型仓库就是 git 仓库，OpenLLM 把它克隆到 `REPO_DIR/<server>/<owner>/<repo>/<branch>`（`repo.py:257`），多级路径天然避免不同来源/分支的仓库互相覆盖。

## 5. 综合实践

把本讲三块内容串起来，完成规格里要求的端到端任务：

1. 按 [DEVELOPMENT.md:20-51](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/DEVELOPMENT.md#L20-L51) 克隆仓库并做开发安装（`pip install -e .`）。
2. 运行 `openllm --version`，对照 [__main__.py:362-366](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L362-L366) 解释它为什么能输出版本（`importlib.metadata` 读已安装包元数据）。
3. 从干净状态出发（`rm -rf ~/.openllm`），按下表逐行验证，记录每一步 `~/.openllm` 的变化：

   | 操作 | 预期新增的目录/文件 | 依据 |
   | --- | --- | --- |
   | `openllm --version` | `~/.openllm/{repos,temp,venv}` | [common.py:22-24](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L22-L24) |
   | `openllm model list` | `repos/` 下出现克隆仓库 | [repo.py:257](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L257) |
   | `openllm repo add …` | `config.json` 首次出现 | [repo.py:109](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L109) |
   | `openllm serve <小模型>` | `venv/<hash>/` 出现 | [venv.py:40](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L40) |

4. 把观察结果与上表对照；不一致的地方记录下来，作为后续读 `common.py`/`repo.py`/`venv.py` 源码的切入点。无 GPU 环境下，第 4 行可只验证到「会去建 venv 目录」这一步，实际安装依赖可能失败——属正常现象，记为「待本地验证」即可。

## 6. 本讲小结

- 装好 `openllm` 有两条路：用户走 `pip install openllm`，贡献者按 `DEVELOPMENT.md` 克隆后 `pip install -e .`，两者都靠 `[project.scripts]` 的 `openllm = "openllm.__main__:app"` 生成入口命令。
- 项目用 **src layout** + hatch/hatch-vcs 构建；`--version` 走 `importlib.metadata` 读已安装包元数据，而非 `_version.py`（后者由构建期生成）。
- `src/openllm/` 下 9 个 `.py` 文件几乎「一文件一职责」，`__main__.py` 是总装车间，通过 `add_typer` 挂载 `repo`/`model`/`clean` 子命令组。
- 运行时工作目录是 `OPENLLM_HOME`（默认 `~/.openllm`），`repos`/`temp`/`venv` 在首次运行时就创建，而 `config.json` 只在 `repo add`/`repo remove` 改动配置时才落盘。
- 「刚装完看不到 config.json」「权重不在本地而在 Hugging Face」都印证了上一讲的结论：OpenLLM 是**编排者**，自己只管缓存目录与配置。

## 7. 下一步学习建议

- 想真正会用 CLI，下一讲 [u1-l3 CLI 入口与命令体系](./u1-l3-cli-entry-and-commands.md) 会逐条拆解 `hello`/`serve`/`run`/`deploy` 与全局回调。
- 想搞懂 `config.json` 和输出细节，可先跳到 [u2-l1 公共基础设施（一）](./u2-l1-common-config-output.md) 读 `common.py` 的 `Config` 与 `ContextVar`。
- 想深入 `repos/` 的克隆逻辑，直接读 `src/openllm/repo.py`，对应 [u2-l3 模型仓库管理](./u2-l3-repo-management.md)。

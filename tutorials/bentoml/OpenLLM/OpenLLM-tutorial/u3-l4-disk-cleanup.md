# 磁盘清理与缓存管理 clean.py

> 阶段：advanced · 依赖：u2-l1（公共基础设施：配置、输出与上下文变量）

## 1. 本讲目标

OpenLLM 在运行过程中会在磁盘上留下三类缓存与一份配置：HuggingFace 的模型权重缓存、为每个 Bento 用 uv 创建的虚拟环境、克隆下来的模型仓库，以及 `config.json`。本讲聚焦 `src/openllm/clean.py` 这个仅约 80 行的小模块，回答三个问题：

1. 这几类东西分别落在磁盘的哪里？
2. 模块里的 `_du` 函数为什么不能简单地对每个文件累加 `st_size`，而要用 inode 去重？
3. 五个 `clean` 子命令分别清什么、是否交互确认、删除失败会怎样？

学完后你应该能够：准确说出四类缓存/配置的路径来源；看懂 `_du` 跨平台统计的原理与硬链接去重的必要性；理解 `clean` 各子命令的「确认 + 安全删除」策略，并发现其中一个值得本地验证的细节。

## 2. 前置知识

本讲需要你先掌握 u2-l1 中建立的几个概念，这里只做要点回顾，不重复展开：

- **`OPENLLM_HOME` 与派生路径常量**：`OPENLLM_HOME` 默认为 `~/.openllm`（可被同名环境变量覆盖），它派生出 `REPO_DIR`（`repos`）、`TEMP_DIR`（`temp`）、`VENV_DIR`（`venv`）三个目录常量，以及 `CONFIG_FILE`（`config.json`）。前三个目录在 `import openllm.common` 时就会因模块顶层的 `mkdir` 调用而被创建，`config.json` 则只在 `save_config` 时落盘。
- **`VERBOSE_LEVEL` 上下文变量**：自制的栈式 `ContextVar`，默认 `0`，由全局 `--verbose` 或本模块子命令自己的 `--verbose` 调高到 `20`。`output()` 仅在消息的 `level` 不大于 `VERBOSE_LEVEL.get()` 时才打印。
- **`questionary`**：用于交互式确认（`questionary.confirm(...).ask()`）与彩色打印（`questionary.print(..., style=...)`）。

两个本讲要用到但属于操作系统基础的概念：

- **inode（索引节点）**：Unix 系文件系统里，每个文件内容由一个 inode 描述，目录条目（文件名）只是指向 inode 的「硬链接」。多个文件名可以指向同一个 inode——这就是**硬链接（hard link）**。`stat.st_ino` 就是文件的 inode 号。
- **硬链接与「真实占用」**：当两个文件名指向同一个 inode 时，磁盘上只存了一份内容。如果统计目录占用时把每个文件名的 `st_size` 都加一遍，就会把同一份内容重复计费。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/openllm/clean.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/clean.py) | 本讲主角。定义 `clean` 子命令组（一个 `OpenLLMTyper` 应用），含 `model_cache`/`venvs`/`repos`/`configs`/`all` 五个命令，以及真实占用统计函数 `_du`。 |
| [src/openllm/common.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py) | 提供 `OPENLLM_HOME`、`REPO_DIR`、`VENV_DIR`、`CONFIG_FILE` 四个路径常量，以及 `VERBOSE_LEVEL` 与 `output`。本讲只引用其中路径定义部分。 |
| [src/openllm/__main__.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py) | 通过 `add_typer(clean_app, name='clean')` 把 clean 应用挂载成 `openllm clean` 子命令组。 |

一句话关系：`clean.py` 从 `common.py`「借」路径常量，再把自己作为子命令挂到 `__main__.py` 的顶层 `app` 上。

## 4. 核心概念与源码讲解

### 4.1 三类缓存与配置定位

#### 4.1.1 概念说明

OpenLLM 自己并不训练模型，也不长期持有权重，但它运行一次 `serve`/`run` 会在磁盘上留下三类「大块头」产物，外加一份小配置：

| 类别 | 来源 | 路径 | 谁创建 |
| --- | --- | --- | --- |
| HF 模型缓存 | 推理运行时从 HuggingFace 下载的权重 | `~/.cache/huggingface/hub` | HuggingFace 库自身（OpenLLM 不设 `HF_HOME`，用默认位置） |
| 虚拟环境 | OpenLLM 为每个 Bento 用 `uv venv` + `uv pip install` 创建 | `~/.openllm/venv` | `venv.py` 的 `_ensure_venv`（见 u2-l6） |
| 仓库缓存 | `repo add` / `repo update` 克隆下来的模型仓库 | `~/.openllm/repos` | `repo.py` 的 `_clone_repo`（见 u2-l3） |
| 配置 | 登记的仓库列表与默认仓库 | `~/.openllm/config.json` | `common.py` 的 `save_config`（见 u2-l1） |

理解这张表的关键在于区分「OpenLLM 管的」和「OpenLLM 借的」：venv、repos、config.json 三者都落在 `OPENLLM_HOME` 下，由 OpenLLM 自己创建、自己清理；而 HF 模型缓存位于用户主目录下的标准 `.cache/huggingface/hub`，OpenLLM 只是**借用它的默认位置**，并不设置 `HF_HOME` 去重定向。这就是为什么 `clean model_cache` 要单独定位一个**不在 `OPENLLM_HOME` 下**的路径。

#### 4.1.2 核心流程

clean.py 顶部的导入与常量定义把这张表落地为代码：

1. 从 `common` 导入四个路径符号：`CONFIG_FILE`、`REPO_DIR`、`VENV_DIR`、`VERBOSE_LEVEL`、`output`。
2. 自行定义 HF 缓存路径常量 `HUGGINGFACE_CACHE`（因为 common.py 里没有它）。
3. 之后五个命令各自引用这些常量去删除，做到「路径只有一个真相来源」。

#### 4.1.3 源码精读

clean.py 顶部导入并定义常量：

[src/openllm/clean.py:1-11](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/clean.py#L1-L11) —— 从 common 借路径，自己补 `HUGGINGFACE_CACHE`。

```python
from openllm.common import CONFIG_FILE, REPO_DIR, VENV_DIR, VERBOSE_LEVEL, output

app = OpenLLMTyper(help='clean up and release disk space used by OpenLLM')

HUGGINGFACE_CACHE = pathlib.Path.home() / '.cache' / 'huggingface' / 'hub'
```

注意三点：① `clean.py` 没有导入 `OPENLLM_HOME` 本身，而是直接导入它派生出的三个常量，说明 clean 只关心最终路径、不关心 `OPENLLM_HOME` 是否被环境变量改写（路径常量已经在 common 模块加载时定型）。② `HUGGINGFACE_CACHE` 用 `pathlib.Path.home()` 拼装，是本模块独有的常量。③ `app` 是 `OpenLLMTyper` 的实例——这继承了 u1-l3 讲过的埋点与「按定义顺序展示命令」能力，本讲把它当作普通 Typer 应用看待即可。

这些常量的「老家」在 common.py：

[src/openllm/common.py:16-26](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L16-L26) —— `OPENLLM_HOME` 派生出 `REPO_DIR/TEMP_DIR/VENV_DIR`，前三个目录在导入即建，`CONFIG_FILE` 仅落盘不预建。

```python
OPENLLM_HOME = pathlib.Path(os.getenv('OPENLLM_HOME', pathlib.Path.home() / '.openllm'))
REPO_DIR = OPENLLM_HOME / 'repos'
TEMP_DIR = OPENLLM_HOME / 'temp'
VENV_DIR = OPENLLM_HOME / 'venv'

REPO_DIR.mkdir(exist_ok=True, parents=True)
TEMP_DIR.mkdir(exist_ok=True, parents=True)
VENV_DIR.mkdir(exist_ok=True, parents=True)

CONFIG_FILE = OPENLLM_HOME / 'config.json'
```

挂载到主应用只有两行，决定了用户最终用到的命令名 `openllm clean ...`：

[src/openllm/__main__.py:9](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L9) 与 [src/openllm/__main__.py:27](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L27) —— 导入并挂载。

```python
from openllm.clean import app as clean_app
...
app.add_typer(clean_app, name='clean')
```

#### 4.1.4 代码实践

1. **实践目标**：把四个磁盘路径在源码中的「真相来源」找全。
2. **操作步骤**：
   - 打开 `src/openllm/common.py`，确认 `OPENLLM_HOME` 的默认值与 `os.getenv` 覆盖逻辑。
   - 打开 `src/openllm/clean.py`，确认 `HUGGINGFACE_CACHE` 不来自 common。
   - 在终端执行 `openllm clean --help`，确认它列出 `model-cache`、`venvs`、`repos`、`configs`、`all` 五个子命令。
3. **需要观察的现象**：`openllm clean --help` 中各子命令的 help 文案与源码里每个函数的 `@app.command(help=...)` 字符串一一对应。
4. **预期结果**：五个子命令名与函数名/`name=` 参数对应（注意 `model_cache` 在 CLI 上会被 Typer 转成 `model-cache`，`all_cache` 用了显式 `name='all'`）。
5. 待本地验证：Typer 把下划线命令名转连字符的实际展示效果。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `clean.py` 里需要单独定义 `HUGGINGFACE_CACHE`，而 `VENV_DIR`/`REPO_DIR` 都是从 common 导入的？

> **答案**：HF 模型缓存位于 `~/.cache/huggingface/hub`，不属于 `OPENLLM_HOME` 体系，common.py 里没有对应常量；而 venv、repos 是 OpenLLM 自己在 `OPENLLM_HOME` 下创建的，common.py 已经把它们定义好，clean 直接复用即可，避免路径重复定义导致不一致。

**练习 2**：如果用户设置了 `OPENLLM_HOME=/data/openllm`，`clean venvs` 会删哪个目录？

> **答案**：会删 `/data/openllm/venv`。因为 `VENV_DIR` 在 common 模块加载时就已经按新的 `OPENLLM_HOME` 拼好，clean 导入的就是这个已定型的值。但 `HUGGINGFACE_CACHE` 不受影响，仍是 `~/.cache/huggingface/hub`。

---

### 4.2 _du 真实占用统计

#### 4.2.1 概念说明

`_du` 是 clean.py 里唯一带「算法味」的函数，职责是：**算出一个目录在磁盘上真正占用了多少字节**。

为什么要单独造一个 `_du` 而不是直接调用系统命令 `du`？两个原因：① 跨平台——Windows 没有 `du`；② 要**去掉硬链接导致的重复计数**，得到「真实占用」而非「名义占用」。

关键直觉：在 Unix 文件系统里，同一个 inode（同一份磁盘内容）可能被多个路径引用。OpenLLM 的场景里这种现象很常见——`uv` 创建虚拟环境时会大量使用硬链接（甚至 reflink/clone）让多个 venv **共享**同一份已安装的包文件。如果统计时按「每个文件名都加一遍 `st_size`」，那么两个 venv 共享的一份 100MB 的包，会被计成 200MB，严重高估。`_du` 用 `st_ino` 去重，确保「同一份物理内容只计一次」。

#### 4.2.2 核心流程

`_du` 的伪代码：

```
输入: path
seen_paths = 空集合
used_space = 0
遍历 path.rglob('*') 得到的每一个条目 f:
    若是 Windows:
        used_space += f.stat().st_size          # 直接累加，不去重
    否则(Unix-like):
        stat = f.stat()
        若 stat.st_ino 不在 seen_paths:
            把 stat.st_ino 加入 seen_paths
            used_space += stat.st_size          # 只对「没见过」的 inode 计一次
返回 used_space
```

数学上，设目录下所有条目的 inode 集合为 \(I\)，每个 inode \(i\) 对应的文件大小为 \(s_i\)，则 Unix 分支返回：

\[
\mathrm{used\_space} = \sum_{i \in I} s_i
\]

而「不去重」的朴素做法返回的是 \(\sum_{f \in \text{paths}} s_{\mathrm{ino}(f)}\)，对每个硬链接重复计费。

> 小注：`rglob('*')` 会同时遍历文件与目录本身。目录条目的 `st_size` 是目录元数据大小（ext4 上常见 4096 字节），所以 `_du` 会把目录元数据也算进去，与 `du` 的语义略有差异；但在「评估能否清理、清完能省多少」的语境下，这种小偏差可接受，硬链接去重才是关键。

#### 4.2.3 源码精读

[src/openllm/clean.py:14-28](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/clean.py#L14-L28) —— `_du` 跨平台真实占用统计。

```python
def _du(path: pathlib.Path) -> int:
  seen_paths = set()
  used_space = 0

  for f in path.rglob('*'):
    if os.name == 'nt':  # Windows system
      # On Windows, directly add file sizes without considering hard links
      used_space += f.stat().st_size
    else:
      # On non-Windows systems, use inodes to avoid double counting
      stat = f.stat()
      if stat.st_ino not in seen_paths:
        seen_paths.add(stat.st_ino)
        used_space += stat.st_size
  return used_space
```

逐行读：

- `seen_paths = set()`：用集合记录「已经计过费的 inode 号」，去重的核心数据结构。
- `path.rglob('*')`：递归穷举 `path` 下所有层级的条目（含文件与目录）。`rglob('*')` 等价于 `glob('**/*')`。
- `os.name == 'nt'`：Windows 分支。NTFS 的硬链接语义与 Unix 不同，且 `uv` 在 Windows 上通常改用拷贝而非硬链接，所以这里直接累加、不做 inode 去重。
- `stat = f.stat()`：Unix 分支取一次 stat（一次系统调用拿到 size、inode 等）。
- `if stat.st_ino not in seen_paths:`：只有没计过费的 inode 才累加，并登记。这正是「同一份内容只计一次」的实现。

调用方把字节数转成 MB 展示，例如 [src/openllm/clean.py:35-38](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/clean.py#L35-L38)：

```python
used_space = _du(HUGGINGFACE_CACHE)
sure = questionary.confirm(
  f'This will remove all models cached by Huggingface (~{used_space / 1024 / 1024:.2f}MB), are you sure?'
).ask()
```

`used_space / 1024 / 1024` 把字节除两次 1024 得到 MB，`:.2f` 保留两位小数——这是 `model_cache` 和 `venvs` 在确认提示里展示的「占用预估」数字。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到「不去重」与「inode 去重」在硬链接场景下的差异，理解为什么 `clean.py` 在 Linux 上要走非 Windows 分支。
2. **操作步骤**（纯文件系统实验，不涉及 OpenLLM 运行）：
   - 建一个临时目录 `t`，写入一个大文件 `t/a.bin`（例如随机写入 50MB）。
   - 用 `ln t/a.bin t/b.bin` 创建一个**硬链接**，此时 `a.bin` 与 `b.bin` 指向同一个 inode。
   - 复制本讲 `_du` 的逻辑写一小段脚本，分别打印「朴素累加」与「inode 去重」两个结果。
   - 用 `stat -c '%i %s' t/a.bin t/b.bin` 观察两者的 inode 号是否相同、大小是否相同。
3. **需要观察的现象**：朴素累加约 100MB（把同一份内容计了两遍），inode 去重约 50MB（与磁盘真实占用一致）；`a.bin` 与 `b.bin` 的 inode 号相同。
4. **预期结果**：inode 去重值 = 单个文件大小，朴素累加值 = 单个文件大小 × 硬链接数。这正是 OpenLLM 在 `uv` 共享包文件的多 venv 场景下必须去重的理由。
5. 待本地验证：在你的机器上实际跑一次 `openllm clean venvs --verbose`（在确认提示处选 No，不真删），观察提示里的 `~XX.XXMB` 数字，再对照系统 `du -sh ~/.openllm/venv` 思考两者差异的来源（`du` 默认也会去重重链接，但口径与 `_du` 不完全相同）。

> 说明：本讲作者未在本环境运行上述命令（运行环境受限），上述行为基于源码与 Unix 硬链接语义得出，请你本地验证后记录真实数值。

#### 4.2.5 小练习与答案

**练习 1**：把 `_du` 里 `if stat.st_ino not in seen_paths:` 这层判断删掉会怎样？

> **答案**：Unix 分支退化为「每个条目都加一遍 `st_size`」，对硬链接场景会把同一份内容重复计费，`_du(VENV_DIR)` 在多个 venv 共享包文件时会显著高估占用，导致确认提示里的 `~XXMB` 偏大。功能不会报错，但数字失真。

**练习 2**：`_du` 在 Windows 上为什么不去重？

> **答案**：代码注释写明 Windows 分支直接累加、不考虑硬链接。一方面 NTFS 的硬链接模型与 Unix 不同，另一方面 `uv` 在 Windows 上一般用拷贝而非硬链接来填充 venv，共享少、去重收益小，所以选择更简单的直接累加。

---

### 4.3 清理子命令与一键重置

#### 4.3.1 概念说明

五个命令按「是否交互确认」分为两类，这是本模块最重要的设计取舍：

- **带 `_du` 统计 + `questionary.confirm` 二次确认**：`model_cache`、`venvs`。这两类体积可能很大、删了可能要重新下载/重装，成本高，所以先估占用、再问用户「are you sure?」，用户拒绝就直接 `return`。
- **无确认、直接删**：`repos`、`configs`。仓库克隆可一键重新拉取、配置可由默认值重建，删除成本低，所以直接 `shutil.rmtree` 不问。其中 `configs` 是一个**值得单独警惕**的特例（见 4.3.3 的细节）。
- **一键重置**：`all`（函数名 `all_cache`），依次调用上面四个命令，复用各自的确认逻辑。

所有删除调用都带 `ignore_errors=True`——这是**安全删除策略**：路径不存在（比如你从没跑过 `serve`，`venv` 目录里是空的、或 HF 缓存根本没建）时不会抛异常崩溃，命令始终能「优雅地」报告「已清理」。

#### 4.3.2 核心流程

每个命令的统一骨架：

```
def 某命令(verbose=False):
    if verbose: VERBOSE_LEVEL.set(20)      # 打开详细输出
    [可选] used_space = _du(目标路径)        # 估占用（仅 model_cache/venvs）
    [可选] sure = questionary.confirm(提示).ask()
    if not sure: return                     # 用户拒绝则不动手
    shutil.rmtree(目标路径, ignore_errors=True)   # 安全删除
    output('清理完成', style='green')        # 绿色成功提示
```

`all` 的流程则是「串联调用」：

```
def all_cache(verbose=False):
    if verbose: VERBOSE_LEVEL.set(20)
    repos()          # 无确认
    venvs()          # 会确认
    model_cache()    # 会确认
    configs()        # 无确认
```

注意 `all` 是直接以 Python 函数调用形式串起四个命令，而不是重新拼装命令行——因此每个被调用命令内部的 `verbose` 形参都不会被传入（都用默认 `False`），只有 `all` 自己开头设的那次 `VERBOSE_LEVEL.set(20)` 生效。同时，`venvs()` 与 `model_cache()` 各自仍会弹一次确认提示，所以 `openllm clean all` 实际会问两次「are you sure?」。

#### 4.3.3 源码精读

**`model_cache` 与 `venvs`**：典型的「估占用 + 确认 + 删」。

[src/openllm/clean.py:31-42](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/clean.py#L31-L42) —— 清 HF 模型缓存，带统计与确认。

```python
@app.command(help='Clean up all the cached models from huggingface')
def model_cache(verbose: bool = False) -> None:
  if verbose:
    VERBOSE_LEVEL.set(20)
  used_space = _du(HUGGINGFACE_CACHE)
  sure = questionary.confirm(
    f'This will remove all models cached by Huggingface (~{used_space / 1024 / 1024:.2f}MB), are you sure?'
  ).ask()
  if not sure:
    return
  shutil.rmtree(HUGGINGFACE_CACHE, ignore_errors=True)
  output('All models cached by Huggingface have been removed', style='green')
```

[src/openllm/clean.py:45-57](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/clean.py#L45-L57) —— 清 OpenLLM 创建的虚拟环境，结构同上，目标改为 `VENV_DIR`。

`venvs` 与 `model_cache` 是镜像关系，唯一差别是目标路径与提示文案。

**`repos` 与 `configs`**：无确认、直接删。

[src/openllm/clean.py:60-65](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/clean.py#L60-L65) —— 清仓库缓存，不确认。

```python
@app.command(help='Clean up all the repositories cloned by OpenLLM')
def repos(verbose: bool = False) -> None:
  if verbose:
    VERBOSE_LEVEL.set(20)
  shutil.rmtree(REPO_DIR, ignore_errors=True)
  output('All repositories have been removed', style='green')
```

[src/openllm/clean.py:68-73](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/clean.py#L68-L73) —— 重置配置，不确认。

```python
@app.command(help='Reset configurations to default')
def configs(verbose: bool = False) -> None:
  if verbose:
    VERBOSE_LEVEL.set(20)
  shutil.rmtree(CONFIG_FILE, ignore_errors=True)
  output('All configurations have been reset', style='green')
```

> **细节与待验证点**：`shutil.rmtree` 的语义是「递归删除一个**目录**」。但 `CONFIG_FILE` 是一个**文件**（`config.json`），不是目录。按 CPython 的实现，对普通文件调用 `rmtree` 会经由 `onerror` 抛出错误（如 `NotADirectoryError`）；而这里传了 `ignore_errors=True`，该错误会被**静默吞掉**。结果是：这条命令会照常打印绿色的「All configurations have been reset」，但 `config.json` **很可能并未被真正删除**。对比 `model_cache`/`venvs`/`repos` 删的都是目录、`rmtree` 能正常工作。本讲作者受运行环境限制未实跑验证，请你按下方 4.3.4 的步骤本地确认 `configs` 是否真能清掉 `config.json`（这同时是一个很好的「读源码发现潜在缺陷」练习）。

**`all`**：一键重置，串联四个。

[src/openllm/clean.py:76-83](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/clean.py#L76-L83) —— 一键清理，调用顺序为 repos → venvs → model_cache → configs。

```python
@app.command(name='all', help='Clean up all above and bring OpenLLM to a fresh start')
def all_cache(verbose: bool = False) -> None:
  if verbose:
    VERBOSE_LEVEL.set(20)
  repos()
  venvs()
  model_cache()
  configs()
```

注意 `@app.command(name='all', ...)` 用了显式 `name='all'`，因为函数名是 `all_cache`，不指定的话命令名会变成 `all-cache`。`all` 是 Python 内置函数名，这里用 `all_cache` 避免遮蔽内置，再借 `name=` 把对外命令名改回 `all`，是一个常见的命名小技巧。

#### 4.3.4 代码实践

1. **实践目标**：验证各子命令的「确认行为」差异，并重点核验 `configs` 是否真能删除 `config.json`。
2. **操作步骤**：
   - 先制造「有东西可清」的状态：执行一次 `openllm repo add myrepo https://github.com/bentoml/openllm-models@main`（或任意公开 git URL），让 `config.json` 落盘、`repos` 下出现克隆。
   - 跑 `openllm clean repos --verbose`：观察它**不问**就直接删，并打印绿色成功。
   - 跑 `openllm clean venvs --verbose`：观察它先弹 `~XX.XXMB, are you sure?`，在提示处选 No，确认它不动手。
   - 重点：跑 `openllm clean configs --verbose` 前后，分别用 `cat ~/.openllm/config.json` 查看文件是否存在。
   - （可选）在 Python 里对 `config.json` 这样一个普通文件单独调用 `shutil.rmtree(path, ignore_errors=True)`，看返回后文件是否还在。
3. **需要观察的现象**：
   - `repos` 无确认即删、目录消失。
   - `venvs`/`model_cache` 有确认提示，选 No 时目录保留。
   - `configs` 打印了绿色「已重置」，但 `config.json` 是否真的被删除——这是关键观察点。
4. **预期结果**：基于 4.3.3 的分析，`configs` 很可能**删不掉** `config.json`（`rmtree` 对文件报错被 `ignore_errors` 吞掉）。若验证属实，这等价于一个「命令声称成功但实际未生效」的小缺陷，正确做法应是 `CONFIG_FILE.unlink(missing_ok=True)`。
5. 待本地验证：`shutil.rmtree` 对普通文件在你的 Python 版本上的精确表现（不同 CPython 版本错误类型可能略有差异，但「文件未被删除」的结论在高版本上一致）。

> 说明：本讲作者未在本环境实跑上述命令，以上预期基于源码与 CPython `shutil.rmtree` 语义推断，请你本地验证后记录真实结果。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `repos` 和 `configs` 不像 `venvs`、`model_cache` 那样做二次确认？

> **答案**：删除成本不同。仓库克隆可以重新 `repo update` 拉回、配置可由 `Config` 的默认值重建，损失小且可快速恢复；而 HF 权重缓存和 venv 体积大、重建慢（要重新下载数 GB 权重或重装依赖），所以后者需要 `_du` 估占用 + `confirm` 确认，前者直接删。

**练习 2**：所有 `shutil.rmtree(...)` 都带了 `ignore_errors=True`，去掉它会怎样？

> **答案**：当目标路径不存在（例如用户从未运行过任何会创建该路径的命令）时，`rmtree` 会抛 `FileNotFoundError`，命令直接崩溃报错。加上 `ignore_errors=True` 后，路径不存在被视为「无需清理」，命令仍能正常打印绿色成功，体验更稳健。

**练习 3**：`openllm clean all` 会弹出几次确认提示？

> **答案**：两次。`all_cache` 依次调用 `repos()`（无确认）、`venvs()`（确认）、`model_cache()`（确认）、`configs()`（无确认），其中 `venvs` 与 `model_cache` 各弹一次。

## 5. 综合实践

把本讲三块内容串成一个「磁盘体检 + 安全清理」小任务：

1. **体检**：写一段不超过 20 行的 Python 脚本，从 `openllm.common` 导入 `REPO_DIR`、`VENV_DIR`、`CONFIG_FILE`，再从 `openllm.clean` 导入 `HUGGINGFACE_CACHE` 和 `_du`，打印这四个路径「各自的真实占用」（用 `_du`，注意对不存在的目录要 `try/except` 兜底，因为 `_du` 内部对不存在路径的 `rglob` 不会有条目、返回 0，但调用方最好显式判空以便区分「不存在」与「空」）。
2. **对照**：把脚本的输出与系统命令 `du -sh ~/.openllm/*` `du -sh ~/.cache/huggingface/hub` 比较，思考两者在硬链接场景下的差异原因，写下一句话结论。
3. **清理（可选，谨慎）**：在确认不需要后，分别用 `openllm clean repos`、`openllm clean venvs` 体验「无确认」与「有确认」两种交互；对 `openllm clean configs` 重点记录 `config.json` 是否真被删除，作为你对 4.3.3 那个待验证点的结论。
4. **产出**：一张表，列出四类缓存/配置的「路径来源（哪个常量）」「是否交互确认」「删除用的函数」「实测占用」「清理后是否真消失」。

这个任务让你同时用到「路径定位（4.1）」「`_du` 统计（4.2）」「子命令策略（4.3）」三块知识，并亲手验证一个读源码时发现的可疑点。

## 6. 本讲小结

- OpenLLM 的磁盘产物分四类：HF 模型缓存（`~/.cache/huggingface/hub`，借默认位置）、venv（`~/.openllm/venv`）、repos（`~/.openllm/repos`）、config（`~/.openllm/config.json`）。前三者中 venv/repos/config 的路径都来自 `common.py` 的 `OPENLLM_HOME` 派生常量，HF 缓存是 `clean.py` 自定义的。
- `_du` 用 `path.rglob('*')` 遍历、用 `stat.st_ino` 在 Unix 上去重硬链接，得到「真实占用」；Windows 分支（`os.name == 'nt'`）直接累加。去重的必要性来自 `uv` 用硬链接在多个 venv 间共享包文件。
- 五个命令分两类：`model_cache`/`venvs` 走「`_du` 估占用 + `questionary.confirm` 确认 + 删」；`repos`/`configs` 无确认直接删；`all` 串联调用这四个。
- 所有删除都带 `ignore_errors=True`，构成「路径不存在也不崩」的安全删除策略。
- 一个待本地验证的细节：`configs` 对文件 `CONFIG_FILE` 调用面向目录的 `shutil.rmtree(..., ignore_errors=True)`，错误被静默吞掉，`config.json` 很可能并未被真正删除——这是「读源码发现潜在缺陷」的典型练习。

## 7. 下一步学习建议

本讲是专家层的「运维与清理」收尾之一。建议接下来：

- **横向对照「创建侧」**：本讲只讲「删」，可以回头读 u2-l6（`venv.py` 的 `_ensure_venv`）与 u2-l3（`repo.py` 的 `_clone_repo`），看清 venv 与 repos 是如何被**创建**与**缓存复用**的，从而完整理解「创建—复用—清理」的生命周期。
- **关注清理正确性**：把本讲发现的 `configs` 疑点当成一个小型贡献机会——若本地验证确认它确实删不掉 `config.json`，可以尝试向仓库提交一个改用 `CONFIG_FILE.unlink(missing_ok=True)` 的小修复（注意先阅读 `DEVELOPMENT.md` 的贡献流程）。
- **回到全景**：至此你已读完 `__main__.py` 的全部子命令组（`repo`/`model`/`clean`）与执行层（`local.py`/`cloud.py`）。下一讲 u3-l5（自定义模型仓库与 Bento 实践）会把 `repo.py`/`model.py`/`common.py` 串成一次端到端的二次开发实践，建议带着「缓存与配置都落在哪」的清晰认知进入那一讲。

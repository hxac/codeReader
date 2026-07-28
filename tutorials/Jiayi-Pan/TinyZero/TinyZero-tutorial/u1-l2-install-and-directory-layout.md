# 环境安装与项目目录结构

## 1. 本讲目标

上一篇（u1-l1）我们已经建立了一个关键认知：**TinyZero 仓库的本质是「veRL 训练框架 + 任务数据 + 规则奖励函数」**，真正属于 TinyZero 自己的代码只集中在数据预处理和奖励打分两处，其余绝大部分是 vendored（直接拷贝进来）的 veRL 框架。

本讲承接这个认知，学完后你应当能够：

1. 说出 TinyZero「安装」到底是在装什么——它装的不是某个叫 `tinyzero` 的包，而是仓库里的 `verl` 包。
2. 理解 Python 打包「三件套」`pyproject.toml` / `setup.py` / `requirements.txt` 各自的角色，以及它们之间的关系与主次。
3. 看懂 `pip install -e .`（可编辑安装）背后发生的事情，包括版本号、包数据（yaml 配置）是怎么被打进包里的。
4. 对照真实仓库，理清顶层目录（`scripts`、`examples`、`verl`、`tests`、`docs` 等）的职责划分，为后续逐层进入源码打下地图。

## 2. 前置知识

阅读本讲前，建议你已经读过 u1-l1，知道 TinyZero 是对 DeepSeek R1 Zero 的轻量化复现、基于 veRL 框架。此外需要几个最基础的背景：

- **Python 包（package）**：一个可以被 `import` 的目录，目录里有 `__init__.py`。本仓库里 `verl/` 就是一个包，里面还有子包如 `verl/trainer/`、`verl/workers/`。
- **依赖（dependency）**：项目运行需要用到的其他库，例如 `torch`、`vllm`、`ray`、`transformers`。
- **打包/安装**：把一个 Python 项目「注册」到当前 Python 环境里，让解释器能找到它。传统用 `setup.py`，现代推荐用 `pyproject.toml`。
- **可编辑安装（editable install）**：`pip install -e .`，不是把代码复制到 `site-packages`，而是建立一个「链接」指回你的源码目录，于是你改源码立即生效。这对读源码、改源码学习非常关键。

如果你对这些概念还模糊，不用担心，本讲会结合真实文件一步步讲清楚。

## 3. 本讲源码地图

本讲涉及的文件都属于「项目构建与安装」层面，不涉及训练逻辑本身：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md) | 项目说明与官方安装步骤（依赖版本、安装顺序）。 |
| [pyproject.toml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/pyproject.toml) | 现代 Python 打包的「主」配置：构建后端、元数据、依赖、版本来源、包数据。 |
| [setup.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/setup.py) | 传统打包脚本，在注释里明确写着它是 `pyproject.toml` 失效时的「兜底（fallback）」方案。 |
| [requirements.txt](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/requirements.txt) | 依赖清单。`setup.py` 直接读它来填充 `install_requires`。 |
| [verl/\_\_init\_\_.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/__init__.py) | 包的入口，定义 `__version__` 并导出核心数据类型 `DataProto`。 |
| [verl/version/version](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/version/version) | 一个只写了 `0.1` 的纯文本文件，是版本号的唯一真实来源。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1** Python 打包三件套：`pyproject.toml` / `setup.py` / `requirements.txt` 的角色与关系。
- **4.2** verl 包与可编辑安装：`pip install -e .` 背后发生了什么。
- **4.3** 仓库目录结构与各目录职责划分。

### 4.1 Python 打包三件套：pyproject.toml / setup.py / requirements.txt

#### 4.1.1 概念说明

一个 Python 项目要能被 `pip install`，需要告诉 pip 三件事：

1. **怎么构建**（build system）：用 `setuptools` 还是别的后端？
2. **项目是谁、版本号多少、依赖哪些库**（metadata）。
3. **哪些文件要打进包里**（packages / package_data），尤其是非 `.py` 的资源文件（本项目的 yaml 配置就属于这类）。

历史上这三件事全写在 `setup.py` 一个脚本里。现代 Python（PEP 517/518/621）推荐把这些信息放进声明式的 `pyproject.toml`，而 `requirements.txt` 则是「依赖清单」的常见载体。

TinyZero（其实是上游 veRL）同时保留了这三者，并在文件里**写明了主次关系**：`pyproject.toml` 是主，`setup.py` 是兜底。这是一个值得注意的细节——它解释了为什么你改依赖时，可能要同时留意两个地方。

#### 4.1.2 核心流程

当你执行 `pip install -e .` 时，pip 的大致流程是：

```
1. 读取 pyproject.toml 的 [build-system] → 决定用 setuptools 构建
2. 读取 [project] 元数据 → 包名 verl、版本（dynamic，去读 verl/version/version）
3. 读取 dependencies → 安装运行期依赖（torch/vllm/ray/transformers…）
4. 读取 [tool.setuptools.package-data] → 把 verl/trainer/config/*.yaml 打进包
5. 建立「可编辑链接」指回当前源码目录 → import verl 立即生效
```

> 说明：`requirements.txt` 并不会被 pip 自动当作依赖来源。它在本项目里被 `setup.py` 读取（见 4.1.3），所以只有当走 `setup.py` 路径时，`requirements.txt` 里的依赖才会进入 `install_requires`。

#### 4.1.3 源码精读

**① pyproject.toml —— 主配置（构建后端 + 元数据 + 依赖）**

构建后端声明，告诉 pip 用 `setuptools` 来构建（[pyproject.toml:4-9](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/pyproject.toml#L4-L9)）：

```toml
[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"
```

项目元数据与依赖列表（[pyproject.toml:32-44](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/pyproject.toml#L32-L44)）。注意几个带版本上限的「夹紧」约束，它们是为了和 veRL 当时的实现兼容：

```toml
dependencies = [
    "accelerate", "codetiming", "datasets", "dill",
    "hydra-core", "numpy", "pybind11", "ray",
    "tensordict",
    "transformers<4.48",
    "vllm<=0.6.3",
]
```

版本号是「动态」的——不写在 toml 里，而是去读 `verl/version/version` 文件（[pyproject.toml:65-66](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/pyproject.toml#L65-L66)）：

```toml
[tool.setuptools.dynamic]
version = {file = "verl/version/version"}
```

包数据声明（[pyproject.toml:74-77](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/pyproject.toml#L74-L77)）。这一段非常关键：训练用的 Hydra 配置 `ppo_trainer.yaml` 不是 `.py` 文件，必须在这里显式声明，否则 `pip install` 后 `import verl` 时会找不到配置文件：

```toml
[tool.setuptools.package-data]
verl = [
  "version/*",
  "trainer/config/*.yaml"
]
```

**② setup.py —— 兜底脚本**

文件第 15 行的注释一语道破它的定位（[setup.py:15](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/setup.py#L15)）：

```python
# setup.py is the fallback installation script when pyproject.toml does not work
```

`setup.py` 自己会去读 `requirements.txt` 来填充 `install_requires`（[setup.py:25-27](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/setup.py#L25-L27)），并跳过以 `#` 开头的注释行：

```python
with open('requirements.txt') as f:
    required = f.read().splitlines()
    install_requires = [item.strip() for item in required if item.strip()[0] != '#']
```

最终的 `setup(...)` 调用（[setup.py:37-54](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/setup.py#L37-L54)）同样声明了 yaml 包数据，和 `pyproject.toml` 里的设置保持一致：

```python
setup(
    name='verl',
    version=__version__,
    packages=find_packages(where='.'),
    package_data={'': ['version/*'],
                  'verl': ['trainer/config/*.yaml']},
    ...
)
```

> 注意：包名是 `verl`，不是 `tinyzero`。这正是 u1-l1 结论的佐证——你安装的就是 vendored 的 veRL。

**③ requirements.txt —— 依赖清单**

完整清单只有 14 行（[requirements.txt:1-14](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/requirements.txt#L1-L14)）：

```
accelerate
codetiming
datasets
dill
flash-attn
hydra-core
numpy
pandas
pybind11
ray
tensordict<0.6
transformers<4.48
vllm<=0.6.3
wandb
```

> **对比要点**：`requirements.txt` 比 `pyproject.toml` 的 `dependencies` 多了 `flash-attn`、`pandas`、`wandb`，并且给 `tensordict` 加了 `<0.6` 的上限。这说明两份依赖列表并不完全一致——学习时若要改依赖，需要确认你走的是哪条安装路径。

#### 4.1.4 代码实践

**实践目标**：用肉眼比对两份依赖列表，理解它们的差异，并定位「夹紧版本」的约束。

**操作步骤**：

1. 打开 [requirements.txt](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/requirements.txt) 与 [pyproject.toml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/pyproject.toml#L32-L44) 两个文件。
2. 列出 `requirements.txt` 里有、但 `pyproject.toml` 的 `dependencies` 里没有的库。
3. 找出所有带 `<` 或 `<=` 的版本约束。

**需要观察的现象**：

- `requirements.txt` 多出 `flash-attn`、`pandas`、`wandb` 三个。
- 带版本上限的有：`tensordict<0.6`（仅 requirements.txt）、`transformers<4.48`、`vllm<=0.6.3`。

**预期结果**：你会清楚看到，如果走 `setup.py` 路径，会安装到更全的依赖（含 flash-attn）；而走 `pyproject.toml` 路径则更精简。这也解释了为什么 README 的安装步骤里要**单独**再 `pip3 install flash-attn --no-build-isolation`——因为它不在 `pyproject.toml` 的核心依赖里，且它有特殊的编译要求（必须加 `--no-build-isolation`）。

> 待本地验证：若你真的执行 `pip install -e .`，可用 `pip show verl` 看到包名是 `verl`、版本是 `0.1`，并可用 `pip list | grep -E "tensordict|vllm|transformers"` 复核夹紧的版本是否生效。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tensordict` 在 `requirements.txt` 里被限制为 `<0.6`，而 `pyproject.toml` 里没有这个限制？

**参考答案**：`requirements.txt` 是 `setup.py`（兜底路径）读取的、更贴近实际运行验证过的依赖快照，作者发现 `tensordict>=0.6` 会有破坏性改动，于是夹紧版本；而 `pyproject.toml` 的 `dependencies` 是更「宽松/精简」的声明，没有同步这个约束。这正是两份依赖列表维护不同步带来的典型坑。

**练习 2**：如果要新增一个运行依赖（比如 `scipy`），同时走 `pyproject.toml` 和 `setup.py` 两条路径都能生效，至少要改哪两个文件？

**参考答案**：要在 [pyproject.toml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/pyproject.toml#L32-L44) 的 `dependencies` 列表里加一行 `scipy`，并在 [requirements.txt](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/requirements.txt) 里加一行 `scipy`（因为 `setup.py` 从这里读依赖）。

---

### 4.2 verl 包与可编辑安装：pip install -e . 背后发生了什么

#### 4.2.1 概念说明

README 的安装步骤里有一行 `pip install -e .`，这里的 `.` 指当前目录（仓库根目录）。这一行做的事情是：把仓库里的 `verl` 包以「可编辑」方式装进当前 Python 环境。

可编辑安装（`-e`）的意义在于：它不复制源码到 `site-packages`，而是放一个「指回源码目录」的链接（`.pth` / `__editable__` 机制）。这样你在 `verl/` 下改任何 `.py`，下次 `import verl` 立刻就是新代码——这对「边读源码、边改、边调试」的学习场景几乎是必需的。

由于包名是 `verl`，安装完之后你 `import verl` 拿到的，就是这个仓库里 `verl/` 目录的代码。

#### 4.2.2 核心流程

```
pip install -e .
  → 读 pyproject.toml（主）/ setup.py（兜底）
  → 解析 name=verl, version=动态读取 verl/version/version (=0.1)
  → 安装 dependencies（torch 已单独装过则跳过）
  → 把 verl/trainer/config/*.yaml 作为 package_data 纳入
  → 写入「可编辑链接」指向当前源码目录
  → 之后 import verl 即从源码目录加载
```

成功后，`verl` 包入口 [verl/\_\_init\_\_.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/__init__.py) 会做三件事：读版本号、导出核心数据类型 `DataProto`、配置日志级别。

#### 4.2.3 源码精读

**① 版本号的真实来源**

[verl/\_\_init\_\_.py:20](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/__init__.py#L20) 读取同目录下 `version/version` 文件，得到 `__version__`：

```python
with open(os.path.join(version_folder, 'version/version')) as f:
    __version__ = f.read().strip()
```

而 [verl/version/version](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/version/version) 这个文件里只有一个字符串 `0.1`。这就是为什么 `pyproject.toml` 把 version 标记成 `dynamic` 并指向它——版本号只有这一个真实来源，`setup.py` 和 `pyproject.toml` 都从这里取值，避免多处维护。

**② 导出核心数据类型 DataProto**

[verl/\_\_init\_\_.py:22](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/__init__.py#L22)：

```python
from .protocol import DataProto
```

`DataProto` 是贯穿整个训练流程的统一数据容器（我们会在 u3-l1 专门讲它）。这里只要知道：`import verl` 之后就能用 `verl.DataProto`，这是 verl 包对外暴露的「门面」之一。这也提示我们，[verl/protocol.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py) 是后续必读的核心文件。

**③ 为什么 yaml 必须进 package_data**

训练入口用 Hydra 加载 `verl/trainer/config/ppo_trainer.yaml`。它是数据文件，不是 `.py`，默认不会被打包。所以 [pyproject.toml:74-77](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/pyproject.toml#L74-L77) 和 [setup.py:49-50](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/setup.py#L49-L50) 都显式声明了 `'verl': ['trainer/config/*.yaml']`。如果是可编辑安装，源码就在原地，这个声明主要影响「非可编辑」的打包分发，但理解它有助于你在 u1-l4 找到配置文件。

#### 4.2.4 代码实践

**实践目标**：验证「安装的就是 verl 包」，并确认版本号来源。

**操作步骤**：

1. 在装好依赖的 conda 环境里执行（待本地验证）：
   ```bash
   pip install -e .
   python -c "import verl; print(verl.__version__)"
   ```
2. 用 `pip show verl` 查看包名与版本。
3. 临时改一下 `verl/version/version` 的内容（比如改成 `0.1-test`），再跑一遍上面的 `python -c`，观察版本号变化，**验证后改回 `0.1`**。

**需要观察的现象**：

- `import verl` 成功，打印 `0.1`。
- `pip show verl` 里 Name 是 `verl`、Version 是 `0.1`。
- 改了 version 文件后，`__version__` 立刻跟着变（可编辑安装下，version 文件就在源码目录里）。

**预期结果**：你会直观体会到「包名是 verl、版本来自单个文本文件」这两件事，从而完全确认 u1-l1 的结论——TinyZero 在安装层面就是 veRL。

> 待本地验证：若没装 GPU/torch/vllm，`import verl` 可能因为 `verl/__init__.py` 链路上的间接依赖报错；这不影响你阅读 `__init__.py` 本身。

#### 4.2.5 小练习与答案

**练习 1**：为什么不把版本号直接写死在 `pyproject.toml` 里，而要单独放一个 `verl/version/version` 文件？

**参考答案**：为了让 `setup.py`、`pyproject.toml`、运行时 `verl/__init__.py` 三处都引用同一个真实来源，避免版本号在多个文件里各写一份、改一处忘改另一处导致不一致。这是单点真相（single source of truth）的做法。

**练习 2**：可编辑安装（`-e`）相对普通安装，对「读源码学习」最大的好处是什么？

**参考答案**：源码不会被复制走，你在 `verl/` 目录里的任何修改（加打印、改参数）在下次 `import` 时立即生效，不必反复 `pip install`。非常适合边读边改边验证。

---

### 4.3 仓库目录结构与各目录职责划分

#### 4.3.1 概念说明

仓库根目录下既有「学习 TinyZero 时会反复打开」的目录（`examples`、`scripts`），也有「构成 veRL 框架本体」的目录（`verl`），还有辅助目录（`tests`、`docs`、`docker`、`patches`）。理清它们的职责，能帮你快速定位「想看某个功能时该去哪个目录」。

需要再次强调：`verl/` 这个目录占据了仓库 90% 以上的代码量，它就是 veRL 框架；而 TinyZero 自己的贡献主要在 `examples/data_preprocess/`（任务数据生成）和 `verl/utils/reward_score/`（规则奖励函数）这两处。

#### 4.3.2 目录地图（核心流程：一个真实文件清单）

下面这张表，把仓库顶层与 `verl/` 下的关键目录对应到「你在学习手册里会接触到的后续讲义」，帮你建立目录 ↔ 主题的映射：

| 目录 | 职责（一句话） | 对应后续讲义 |
| --- | --- | --- |
| `scripts/` | 训练/格式化脚本，含核心入口 `train_tiny_zero.sh` | u1-l3、u1-l4 |
| `examples/` | 各任务数据预处理脚本与各 trainer 启动脚本（`data_preprocess`、`ppo_trainer`、`grpo_trainer`、`sft`、`generation`、`ray`、`split_placement`） | u2-l1、u2-l2、u7-l3 |
| `verl/` | veRL 框架本体（被 vendored 进来），几乎所有训练逻辑都在这里 | 贯穿全手册 |
| `verl/trainer/` | 训练入口与 PPO 训练循环：`main_ppo.py`（主入口）、`ppo/`（`ray_trainer.py` 主循环 + `core_algos.py` 算法）、`config/`（yaml 配置） | u1-l4、u4-*、u5-* |
| `verl/workers/` | 各角色 Worker 实现：`actor/`、`critic/`、`rollout/`、`reward_model/`、`sharding_manager/`，以及 `fsdp_workers.py`、`megatron_workers.py` 两种后端 | u6-*、u7-l4 |
| `verl/utils/` | 工具集：`dataset/`（数据加载）、`reward_score/`（规则奖励）、`seqlen_balancing.py`（序列均衡）、`tracking.py`（实验跟踪）、`torch_functional.py`（数学函数）等 | u2-l3、u2-l4、u5-*、u7-l2 |
| `verl/protocol.py` | 统一数据容器 `DataProto`（在 `verl/` 根下，非子目录） | u3-l1 |
| `verl/single_controller/` | 「单控制器」+ Ray 资源池调度机制 | u3-l2、u3-l3 |
| `verl/third_party/vllm/` | 对 vLLM 的封装与适配 | u6-l4、u6-l5 |
| `tests/` | 端到端（e2e）最小化训练测试，含 `arithmetic_sequence`、`digit_completion` 等可跑样例 | u7-l5 |
| `docs/` | 文档（`.rst`），含安装、奖励函数、实验说明等 | u7-l6 |
| `docker/` | 两种预置环境的 Dockerfile（`ngc.vllm`、`vemlp.vllm.te`） | — |
| `patches/` | Megatron 后端所需的补丁（`megatron_v4.patch`） | u7-l4 |

#### 4.3.3 源码精读

下面用仓库里真实存在的文件，佐证上表对 `examples/`、`verl/trainer/`、`verl/workers/`、`verl/utils/` 四个目录的职责描述。

**① examples/ —— 数据预处理与示例脚本**

`examples/data_preprocess/` 下有 `countdown.py`、`multiply.py`、`arth.py` 等（[examples/data_preprocess/countdown.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/data_preprocess/countdown.py)）。这些是 TinyZero 自己写的任务数据生成脚本，README 的「Data Preparation」步骤正是调用它们。`examples/ppo_trainer/`、`examples/grpo_trainer/` 下则是各模型的启动脚本（如 `run_qwen2-7b.sh`）。

**② verl/trainer/ —— 训练入口与 PPO 循环**

这个目录里最关键的是 [verl/trainer/main_ppo.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py)，它是 `scripts/train_tiny_zero.sh` 最终 `python3 -m verl.trainer.main_ppo` 调用的入口。PPO 训练主循环在 [verl/trainer/ppo/ray_trainer.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py)，算法实现在 [verl/trainer/ppo/core_algos.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py)，配置在 [verl/trainer/config/ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml)。

**③ verl/workers/ —— 各角色 Worker**

FSDP 后端的混合引擎 Worker 在 [verl/workers/fsdp_workers.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py)（`ActorRolloutRefWorker`），Megatron 后端在 [verl/workers/megatron_workers.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/megatron_workers.py)。子目录 `actor/`、`critic/`、`rollout/`、`reward_model/`、`sharding_manager/` 分别对应策略、价值、生成、奖励模型、权重分片管理。

**④ verl/utils/ —— 工具集**

规则奖励函数在 [verl/utils/reward_score/countdown.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py)（TinyZero 自己的贡献），序列长度均衡在 [verl/utils/seqlen_balancing.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py)，数学工具在 [verl/utils/torch_functional.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py)，数据加载在 [verl/utils/dataset/rl_dataset.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/dataset/rl_dataset.py)。

#### 4.3.4 代码实践（本讲主实践任务）

**实践目标**：亲手把 `verl/` 的子目录列出来，并为 `examples/`、`verl/trainer/`、`verl/workers/`、`verl/utils/` 四个目录各写一句职责说明，建立「目录 ↔ 职责」的肌肉记忆。

**操作步骤**：

1. 在仓库根目录执行（只读操作）：
   ```bash
   ls verl/
   ```
   你应当看到：`__init__.py models protocol.py single_controller third_party trainer utils version workers`。
2. 按 README 完成本讲的依赖安装（conda 环境 → torch → vllm → ray → `pip install -e .` → flash-attn → wandb 等），命令见 [README.md:20-37](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L20-L37)。
3. 为下列四个目录各写一句话的职责说明（可参考 4.3.2 的表格，但请用自己的话写）：
   - `examples/`
   - `verl/trainer/`
   - `verl/workers/`
   - `verl/utils/`

**需要观察的现象**：

- `verl/` 下确实有 `trainer`、`workers`、`utils` 等子目录，且 `protocol.py` 是一个单独的文件（不是目录）。
- 安装步骤里 `pip install -e .` 成功后，`pip show verl` 显示包名 `verl`。

**预期结果（示例答案，供你对照）**：

- `examples/`：放各任务的数据预处理脚本与各 trainer 的示例启动脚本，是 TinyZero 自己贡献最集中的地方之一。
- `verl/trainer/`：训练入口与 PPO 训练循环，`main_ppo.py` 是总入口，`ppo/` 下是主循环与算法实现，`config/` 下是 Hydra 配置。
- `verl/workers/`：各角色 Worker（actor/critic/rollout/reward_model/sharding_manager）以及 FSDP、Megatron 两种后端的 Worker 实现。
- `verl/utils/`：工具函数集合，含数据加载、规则奖励、序列均衡、实验跟踪、数学函数等，是被各 Worker/Trainer 反复调用的「底座」。

> 待本地验证：安装过程是否顺利完成取决于本地是否有兼容的 GPU 与 CUDA；若仅做源码阅读，可跳过实际安装，直接用 `ls` 完成目录梳理部分。

#### 4.3.5 小练习与答案

**练习 1**：如果你想看「PPO 一步训练里到底先做什么、后做什么」，应该去哪个目录的哪个文件？

**参考答案**：去 [verl/trainer/](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py) 下的 `ppo/ray_trainer.py`，找 `RayPPOTrainer.fit()` 方法。这正是 u4-l3 的主题。

**练习 2**：仓库里哪个目录最能体现「TinyZero 区别于 veRL 的自己贡献」？

**参考答案**：`examples/data_preprocess/`（任务数据生成，如 `countdown.py`）和 `verl/utils/reward_score/`（规则奖励函数，如 `countdown.py`）。这两处是 TinyZero 真正「自己写的」逻辑，也是 u2 单元的重点。

**练习 3**：`tests/e2e/` 下的样例对学习有什么用？

**参考答案**：它们是「最小可跑」的端到端训练样例（如 `arithmetic_sequence`、`digit_completion`），不需要大模型和大显存即可验证训练管线是否通畅，非常适合学习与调试，是 u7-l5 的主题。

## 5. 综合实践

把本讲的三个模块串起来，完成一次「安装 + 认图 + 定位」的小任务：

1. **安装**：按 [README.md:20-37](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L20-L37) 在新 conda 环境里完成依赖安装，包括最后的 `pip install -e .`。
2. **认图**：执行 `ls verl/` 与 `ls verl/trainer verl/workers verl/utils`，在纸上画一张三层的「目录树」，标注每个子目录的职责。
3. **定位**：不打开文件内容，仅凭目录结构回答三个问题：
   - 训练总入口在哪个文件？（答：`verl/trainer/main_ppo.py`）
   - 统一数据容器 `DataProto` 在哪个文件？（答：`verl/protocol.py`）
   - countdown 任务的规则奖励函数在哪个文件？（答：`verl/utils/reward_score/countdown.py`）
4. **验证版本来源**：用 `python -c "import verl; print(verl.__version__)"` 确认打印 `0.1`，并指出这个值来自哪个文件（答：`verl/version/version`）。

> 这个任务把「会装、会看目录、会定位关键文件」三件事一次性走通，是进入后续源码精读前的最佳热身。若本地无 GPU，步骤 1 的实际安装可标记为「待本地验证」，重点完成 2、3、4 的源码阅读型任务。

## 6. 本讲小结

- TinyZero 的「安装」本质是 `pip install -e .` 把仓库里的 **`verl` 包**以可编辑方式装进环境，包名是 `verl` 不是 `tinyzero`——再次印证 u1-l1「TinyZero = veRL + 任务 + 奖励」的结论。
- 打包「三件套」有主次：[pyproject.toml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/pyproject.toml) 是主配置，[setup.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/setup.py) 是兜底，[requirements.txt](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/requirements.txt) 是被 `setup.py` 读取的依赖清单；两份依赖列表并不完全一致（如 `flash-attn`、`pandas`、`tensordict<0.6` 仅在 requirements.txt）。
- 版本号遵循「单点真相」：只有 [verl/version/version](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/version/version) 一处写 `0.1`，`pyproject.toml`、`setup.py`、`verl/__init__.py` 都引用它。
- 可编辑安装（`-e`）让你改源码立即生效，是读源码学习的关键；安装后 `import verl` 即拿到本仓库代码，并自动暴露核心数据类型 `DataProto`。
- 仓库目录分工清晰：`scripts/`（脚本）、`examples/`（数据 + 示例，TinyZero 贡献集中地）、`verl/trainer/`（训练入口与循环）、`verl/workers/`（各角色 Worker）、`verl/utils/`（工具底座）、`tests/`（最小可跑 e2e）、`docs/`（文档）。
- 记住一张「定位地图」：训练入口 `main_ppo.py`、主循环 `ppo/ray_trainer.py`、算法 `ppo/core_algos.py`、数据容器 `protocol.py`、规则奖励 `utils/reward_score/`——后续讲义会逐个深入。

## 7. 下一步学习建议

本讲解决了「怎么装、目录怎么分布」的问题。下一讲 **u1-l3「跑通第一次训练：Countdown 任务」** 会带你用 `scripts/train_tiny_zero.sh` 端到端跑通一次训练，把本讲看到的目录（`examples/data_preprocess/`、`scripts/`）和训练入口 `verl.trainer.main_ppo` 串起来。

在进入 u1-l3 前，建议你：

- 自己用 `ls` / `tree` 再走一遍 `verl/` 的子目录，确保能脱口说出 `trainer/workers/utils` 的职责。
- 顺手打开 [verl/trainer/main_ppo.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py) 扫一眼，不用看懂，只感受一下「入口文件长什么样」，为 u1-l3/ u1-l4 做心理预热。
- 如果你对配置体系好奇，可以直接跳到 **u1-l4「配置系统：Hydra 与 ppo_trainer.yaml」**，但要先对 `train_tiny_zero.sh` 有印象，所以推荐顺序仍是 u1-l3 → u1-l4。

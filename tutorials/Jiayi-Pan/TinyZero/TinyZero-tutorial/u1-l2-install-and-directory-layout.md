# 环境安装与项目目录结构

> 本讲是学习手册第 2 篇（u1-l2），承接 u1-l1 建立的项目心智模型。上一篇你已经知道：TinyZero 是 DeepSeek R1 Zero 的轻量复现，本质是「veRL 框架 + 任务数据 + 规则奖励」。本篇我们离开「读文档」，正式进入「看仓库」——把它装起来，并把目录结构理清楚。

## 1. 本讲目标

学完本讲，你应该能够：

- 说出从「新建 conda 环境」到「`pip install -e .` 可编辑安装 verl」的完整步骤，并理解每一步装的是什么。
- 看懂 `setup.py`、`pyproject.toml`、`requirements.txt` 三个构建文件各自的作用，以及它们之间「谁能替代谁」的关系。
- 在不看资料的情况下，画出仓库顶层目录（`scripts`、`examples`、`verl`、`tests`、`docs`）的职责划分，并能进一步说出 `verl/` 内部 `trainer`、`workers`、`utils` 等子目录是干什么的。
- 判断「一个新依赖该写在哪、一个配置文件该放哪、一个新脚本该建在哪」。

本篇**只讲环境与目录结构**，不深入任何一行训练算法代码。这是后续所有源码阅读篇（从 u2 开始）的「地图基础」。

## 2. 前置知识

本篇假设你已具备：

- **Python 与 pip 的基础**：知道 `pip install` 装包、`python -m xxx` 运行模块。本讲会解释更细的部分。
- **命令行基础**：能在终端里 `cd`、`ls`、设置环境变量（`export VAR=value`）。
- **conda 的最基本概念**：conda 是一个「环境管理器」，可以把不同项目的 Python 版本和依赖隔离开，互不污染。你可以把一个 conda 环境理解成一个「一次性的、可删除的 Python 沙箱」。
- **什么是「包（package）」**：一个 Python 包就是一个可以被 `import` 的目录（里面通常有 `__init__.py`）。`verl` 本身就是一个大包，里面又套着 `verl.trainer`、`verl.workers` 等子包。

如果上面这些对你来说很陌生也没关系，本讲会用通俗语言一步步带。

> 名词速查
> - **veRL**：Volcano Engine Reinforcement Learning，字节跳动开源的 LLM 强化学习训练框架，TinyZero 直接基于它。仓库里的 `verl/` 目录就是它的源码（注意大小写：仓库目录小写，框架名写作 veRL）。
> - **可编辑安装（editable install）**：`pip install -e .` 会把你当前目录的代码「链接」进 Python 环境，而不是复制一份。这样你**改了源码，立刻生效**，不用反复重装——这正是我们要读源码、做实验所需要的。
> - **vendored**：把第三方框架的源码直接拷贝进自己仓库里维护。`verl/` 目录就是把 veRL vendored 进来的结果。

## 3. 本讲源码地图

本讲涉及的「源码」其实主要是**构建与说明类文件**，它们决定了「怎么装」和「装进来的是什么」：

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| [README.md](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md) | 项目首页说明，含官方安装步骤 | 提供「正确答案」式的安装顺序 |
| [setup.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/setup.py) | 传统的 Python 安装脚本（备用） | 拆解 `name`、`install_requires`、`package_data` |
| [pyproject.toml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/pyproject.toml) | 现代化的项目元数据文件（主入口） | 拆解 `[build-system]`、`[project]`、动态版本 |
| [requirements.txt](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/requirements.txt) | 运行期依赖清单 | 逐行解读每个依赖及其版本约束 |

此外，我们会用 `git ls-files` / `ls` 去看清整个仓库目录长什么样，这是「目录结构」模块的素材。

---

## 4. 核心概念与源码讲解

本讲先把「安装流程」串起来（4.1），再分别精读三个最小模块——`requirements.txt`（4.2）、`setup.py`（4.3）、`pyproject.toml`（4.4），最后讲目录结构（4.5）。

### 4.1 安装流程总览：从 conda 到 verl

#### 4.1.1 概念说明

很多人照着 README 把命令敲一遍，跑起来了就不再深究。但要长期读源码、改实验，你必须理解每一步装的是什么、为什么是这个顺序。TinyZero 的依赖栈是「分层」的：

1. **Python 解释器层**：conda 提供 `python=3.9`。
2. **深度学习运行时层**：PyTorch（`torch==2.4.0`）+ CUDA 12.1。
3. **推理加速层**：`vllm==0.6.3`，它对 torch 版本有严格要求，所以 README 说「你也可以跳过装 torch，让 vllm 帮你装对的版本」。
4. **分布式调度层**：`ray`，veRL 用它来跨 GPU 编排 worker（这是 u3-l2 的内容）。
5. **训练框架层**：`verl` 本身，用「可编辑安装」装进来。
6. **可选增强层**：`flash-attn`（注意力加速）、`wandb`（实验记录）等。

这个顺序很重要：**vllm 必须在 verl 之前装**，因为 verl 的依赖声明里写了 `vllm<=0.6.3`，如果先装 verl，pip 可能拉一个不符合 vllm 自身要求的 torch 版本，造成版本打架。

#### 4.1.2 核心流程

官方安装步骤可以画成这样的流水线：

```
conda create -n zero python=3.9          # ① 建沙箱
        │
        ▼
pip install torch==2.4.0 (+cu121)        # ② 装 GPU 版 torch（可省，vllm 会兜底）
        │
        ▼
pip3 install vllm==0.6.3                 # ③ 推理引擎（顺带锁 torch 版本）
        │
        ▼
pip3 install ray                         # ④ 分布式编排
        │
        ▼
pip install -e .                         # ⑤ 可编辑安装 verl（本仓库的核心）
        │
        ▼
pip3 install flash-attn --no-build-isolation   # ⑥ 注意力加速（编译型，要单独装）
pip install wandb IPython matplotlib           # ⑦ 实验记录与画图
```

#### 4.1.3 源码精读

这些步骤全部写在 README 的 Installation 小节里，我们逐句对一下：

[README.md:22-37](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L22-L37) 给出了完整的安装命令块。下面是其中最关键的几行（注意 README 给的 `python=3.9` 和 `torch==2.4.0` 都是有意固定的版本）：

```bash
conda create -n zero python=3.9
pip install torch==2.4.0 --index-url https://download.pytorch.org/whl/cu121
pip3 install vllm==0.6.3
pip3 install ray
pip install -e .                  # 这一行就是「安装 verl 自己」
pip3 install flash-attn --no-build-isolation
```

要点拆解：

- `--index-url https://download.pytorch.org/whl/cu121`：从 PyTorch 官方的 CUDA 12.1 通道拉取带 GPU 支持的 torch；不加这个会装到 CPU 版，训练就跑不动了。
- `pip install -e .` 里的 `-e` 就是 editable，`.` 表示「当前目录」。它要能在当前目录找到构建配置——也就是我们 4.3、4.4 要讲的 `pyproject.toml`（主）和 `setup.py`（备用）。
- `flash-attn --no-build-isolation`：`flash-attn` 是要从源码编译的 C++/CUDA 扩展，`--no-build-isolation` 让它在**当前已装好 torch 的环境里**编译，否则它会在一个隔离的、没有 torch 的环境里编译失败。

> 顺手记一个「弃用提示」：README 顶部（[README.md:1-5](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L1-L5)）写明本仓库已停止维护，生产环境请直接用上游 [veRL](https://github.com/volcengine/verl)。这提醒我们：本系列的学习目标是**读懂实现与思路**，而不是拿它做生产训练。

#### 4.1.4 代码实践

**实践目标**：在你自己的机器上把 conda 环境建出来，至少完成到 `pip install -e .`，验证 `import verl` 能成功。

**操作步骤**：

1. 确认你装了 conda（`conda --version` 有输出即可）。
2. 执行 `conda create -n zero python=3.9 -y`，再 `conda activate zero`。
3. 按 4.1.2 的流水线依次执行 torch → vllm → ray → `pip install -e .`。
   - 如果没有 GPU，`vllm` 这一步很可能装不上或跑不动；这种情况下，本步可作为「待本地验证」，你只需在仓库根目录执行 `pip install -e .` 并跳过后续训练相关导入即可。
4. 验证安装：进入仓库根目录，运行 `python -c "import verl; print(verl.__version__)"`。

**需要观察的现象**：

- `conda activate zero` 后，命令行提示符前出现 `(zero)`，说明进入了隔离环境。
- `pip install -e .` 输出里能看到 `Successfully installed verl-0.1 ...`（注意版本号 0.1，来自 4.3 会讲到的版本文件）。

**预期结果**：`python -c "import verl; print(verl.__version__)"` 打印出 `0.1`。

> 待本地验证：如果你在无 GPU 的环境里，`import verl` 可能因为缺 vllm/torch 而报错。这是正常的——本仓库是「面向 GPU 训练」的，没有 GPU 时，你可以只验证「目录与构建文件存在、`pip install -e .` 能解析依赖」这一层。

#### 4.1.5 小练习与答案

**练习 1**：README 说「装 torch 这一步可以跳过，让 vllm 帮你装」。请解释为什么把 torch 交给 vllm 来装是合理的，而不是自己随便装一个？

**答案**：vllm 对 torch、CUDA 的版本组合有严格要求，它自己的元数据里声明了兼容的 torch 版本区间。让 vllm 来装 torch，能保证「推理引擎 ↔ torch」这一对组合是经过验证的，避免后面 `import vllm` 时出现 ABI 不兼容（典型报错如 `undefined symbol`）。

**练习 2**：`pip install -e .` 里的 `.` 为什么必须在你**执行命令时所在的目录**里运行？换个目录会怎样？

**答案**：`.` 表示「当前目录」，pip 会在当前目录寻找构建配置文件（`pyproject.toml` 或 `setup.py`）来知道「这个包叫什么、包含哪些子包、依赖什么」。如果你在别的目录执行，pip 找不到这些文件，会报 `Directory '.' is not installable. Neither 'setup.py' nor 'pyproject.toml' found.`，所以必须 `cd` 到仓库根目录再执行。

---

### 4.2 requirements.txt：依赖清单与版本约束

#### 4.2.1 概念说明

`requirements.txt` 是 Python 生态里最朴素的依赖清单：一行一个包，可选地带上版本约束。它解决的问题是——**别人（或未来的你）拿到这个项目，怎么复现出一样的依赖环境**。

需要先建立两个概念：

- **版本约束符号**：`==`（精确等于）、`>=`（至少）、`<` / `<=`（不超过）、无符号（任意版本）。
- **为什么要有上限约束**：很多库的新版本会改 API（比如 `transformers<4.48` 是因为 4.48 之后某些接口变了，veRL 还没适配）。写 `<4.48` 就是在「锁住一个已知可用的天花板」。

#### 4.2.2 核心流程

`requirements.txt` 的使用方式有两条路径，两条在 TinyZero 里**同时存在**：

1. **直接给人用**：开发者执行 `pip install -r requirements.txt`，pip 会按文件装。
2. **给 `setup.py` 用**：`setup.py` 在构建时读取这个文件，把它转成 `install_requires`，于是「安装 verl」会顺带把这些依赖都装上。

这就是为什么你会看到：requirements.txt 里列的包，和「装完 verl 后看到的一堆依赖」高度重合。

#### 4.2.3 源码精读

[requirements.txt:1-14](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/requirements.txt#L1-L14) 全文如下：

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

逐个分组解读（不必背，理解「每类解决什么问题」即可）：

| 依赖 | 解决什么问题 | 版本约束含义 |
| --- | --- | --- |
| `transformers<4.48` | 加载 Qwen2.5 等 HuggingFace 模型 | 上限锁定，避开未适配的新接口 |
| `vllm<=0.6.3` | 高速推理 / rollout 生成 | 上限锁定，与仓库 third_party 的 vLLM 桥接对齐 |
| `tensordict<0.6` | DataProto 使用的张量字典容器（u3-l1 会讲） | 上限锁定 |
| `ray` | 跨 GPU 分布式编排 worker（u3-l2 会讲） | 无约束 |
| `hydra-core` | 配置系统（u1-l4 会讲 PPO yaml） | 无约束 |
| `accelerate` / `datasets` | 模型/数据的常用工具 | 无约束 |
| `flash-attn` | 注意力加速 | 注意：它其实**编译型**，requirements.txt 里写了但 README 让你单独装 |
| `wandb` | 实验指标记录 | 无约束 |
| `codetiming` / `dill` / `pybind11` / `numpy` / `pandas` | 计时、序列化、C++ 绑定、数值计算等基础设施 | 无约束 |

> 注意一个「细节坑」：`flash-attn` 出现在 `requirements.txt` 里，但 README 又单独让你用 `--no-build-isolation` 装。这是因为普通的 `pip install -r requirements.txt` 装不好它。所以**清单声明 ≠ 安装顺序**，这是读构建文件时要留意的。

#### 4.2.4 代码实践

**实践目标**：体会「版本上限约束」的作用，不实际破坏环境。

**操作步骤**：

1. 打开 `requirements.txt`，找出所有「带 `<` 或 `<=`」的行。
2. 想象一个场景：上游 `transformers` 升到了 4.55。问自己——如果不写 `<4.48`，本仓库最可能在哪一步出问题？
3. 用 pip 的「干跑」查看依赖解析（不会真的装，需要网络）：
   ```bash
   pip install --dry-run -r requirements.txt
   ```
   如果你没有网络或环境不全，此步标注「待本地验证」。

**需要观察的现象**：`--dry-run` 会打印出 pip 打算安装/跳过的每个包及解析出的版本，你能看到 `transformers` 被钉在 4.48 以下。

**预期结果**：能口头说出「三个带上限约束的包是 `tensordict<0.6`、`transformers<4.48`、`vllm<=0.6.3`，它们都是 API 变化敏感、必须锁天花板的包」。

#### 4.2.5 小练习与答案

**练习 1**：`ray` 这一行没有任何版本约束，这样安全吗？什么情况下你应该给它加约束？

**答案**：不写约束意味着「装最新版」。在「能跑就行」的实验阶段可以接受；但一旦你要把环境**固定下来给团队复现**，就应该改成 `ray==<某个验证过的版本>`，否则某天 ray 发了大版本、API 改了，你的代码会莫名其妙报错却查不到原因。

**练习 2**：`flash-attn` 既然在 `requirements.txt` 里，为什么 README 还要单独装？

**答案**：因为 `flash-attn` 是带 C++/CUDA 内核的编译型扩展，`--no-build-isolation` 表示「在已有 torch 的环境里就地编译」。直接 `pip install -r requirements.txt` 时，pip 会在一个隔离环境里编译，那里没有正确版本的 torch/CUDA，几乎必然失败。所以它虽然在清单里，实际仍需按 README 的特殊方式单独安装。

---

### 4.3 setup.py：传统的安装脚本（备用入口）

#### 4.3.1 概念说明

`setup.py` 是 Python「老一代」的打包方式：用一段 Python 脚本调用 `setuptools.setup(...)`，向打包工具描述「这个包叫什么、版本多少、包含哪些子包、依赖什么、附带哪些非代码文件」。

它现在多半是**备用方案**——文件里第一行注释就直说了：

[setup.py:15-16](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/setup.py#L15-L16) 注释写明：`setup.py is the fallback installation script when pyproject.toml does not work`（当 pyproject.toml 不工作时，setup.py 作为兜底安装脚本）。所以它的存在是为了「兼容性兜底」，主入口其实是 4.4 的 `pyproject.toml`。

> 一个 `setup.py`（或 `pyproject.toml`）最核心要回答的五个问题：
> 1. 包叫什么名字？（`name`）
> 2. 版本号？（`version`）
> 3. 哪些目录算作包？（`packages`）
> 4. 依赖什么？（`install_requires`）
> 5. 要不要附带非 Python 文件，比如 yaml 配置？（`package_data`）

#### 4.3.2 核心流程

`setup.py` 在被 `pip install -e .` 调用时，会做这几件事：

```
1. 读取 verl/version/version 文件      → 得到版本号字符串
2. 打开 requirements.txt 并逐行解析     → 得到 install_requires 列表
3. 调用 find_packages(where='.')        → 自动发现所有可导入的子包
4. 声明附带数据（version/*、trainer/config/*.yaml）
5. 调用 setup(name=..., version=..., ...) → 把以上信息交给 setuptools 完成安装
```

#### 4.3.3 源码精读

先看版本号怎么来的。[setup.py:19-22](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/setup.py#L19-L22) 直接读取 `verl/version/version` 文件的内容当版本号：

```python
with open(os.path.join(version_folder, 'verl/version/version')) as f:
    __version__ = f.read().strip()
```

这个文件的内容就一行 `0.1`（[verl/version/version](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/version/version)），所以装出来的包就是 `verl-0.1`。这正是 4.1.4 里 `import verl; print(verl.__version__)` 打印 `0.1` 的根源——`verl/__init__.py`（[verl/__init__.py:17-20](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/__init__.py#L17-L20)）用几乎一样的逻辑读同一个文件。

接着看依赖怎么从 `requirements.txt` 转过来。[setup.py:25-27](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/setup.py#L25-L27) 读文件、去空行、去注释行：

```python
with open('requirements.txt') as f:
    required = f.read().splitlines()
    install_requires = [item.strip() for item in required if item.strip()[0] != '#']
```

这一段就是 4.2 说的「requirements.txt 给 setup.py 用」的具体写法：把文本清单转成 Python 列表，过滤掉 `#` 开头的注释。

最后看 `setup(...)` 的核心字段。[setup.py:37-54](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/setup.py#L37-L54) 集中回答了上面那五个问题：

```python
setup(
    name='verl',                 # ① 包名
    version=__version__,         # ② 版本，来自 version 文件
    package_dir={'': '.'},
    packages=find_packages(where='.'),   # ③ 自动发现所有子包
    ...
    install_requires=install_requires,   # ④ 依赖，来自 requirements.txt
    extras_require={'test': ['pytest', 'yapf']},  # 额外：测试用依赖
    package_data={'': ['version/*'],                # ⑤ 附带非代码文件
                  'verl': ['trainer/config/*.yaml'],},
    ...
)
```

两个要点：

- `find_packages(where='.')`：自动把仓库里所有含 `__init__.py` 的目录识别为包，所以 `verl/`、`verl/trainer/`、`verl/workers/` 等都会被打包进去——这就是「目录即包」。
- `package_data`：默认情况下 setuptools **只打包 `.py` 文件**。但 verl 的 PPO 配置是 `yaml` 文件，版本是纯文本文件，必须在这里显式声明 `package_data` 才会被装进去。这一行非常重要，它保证了 `import` 之后能找到 [verl/trainer/config/ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml)（u1-l4 要精读它）。
- `extras_require={'test': ['pytest', 'yapf']}`：声明「可选附加依赖」，用 `pip install -e .[test]` 才会装上，用来跑测试。

#### 4.3.4 代码实践

**实践目标**：验证「版本号来自 version 文件」「依赖来自 requirements.txt」这两条事实。

**操作步骤**：

1. 不修改任何文件，仅阅读：打开 `verl/version/version`，确认内容是 `0.1`。
2. 在仓库根目录运行：
   ```bash
   python -c "import verl; print('verl version =', verl.__version__)"
   ```
3. 在仓库根目录运行一段「复刻 setup.py 依赖解析逻辑」的小脚本（**示例代码**，仅演示逻辑，不写入仓库）：
   ```python
   # 示例代码：模拟 setup.py 解析 requirements.txt
   with open('requirements.txt') as f:
       reqs = [x.strip() for x in f.read().splitlines() if x.strip() and x.strip()[0] != '#']
   print(reqs[:5])   # 打印前 5 个依赖
   ```

**需要观察的现象**：步骤 2 打印 `verl version = 0.1`；步骤 3 打印出一个列表，前几项形如 `['accelerate', 'codetiming', 'datasets', 'dill', 'flash-attn']`。

**预期结果**：你能复述「version 文件里写什么，`verl.__version__` 就是多少；requirements.txt 里去掉注释后的每一行，就是安装 verl 时会被装上的依赖」。

#### 4.3.5 小练习与答案

**练习 1**：如果你把 `verl/version/version` 的内容从 `0.1` 改成 `0.2`（重新 `pip install -e .` 后），`import verl; verl.__version__` 会变成什么？为什么？

**答案**：会变成 `0.2`。因为 `setup.py` 和 `verl/__init__.py` 都是**运行时读取这个文件**得到版本字符串，没有任何地方硬编码 `0.1`。改文件即改版本——这是「单一数据源（single source of truth）」的简单实现。

**练习 2**：`package_data` 里写了 `'verl': ['trainer/config/*.yaml']`。如果删掉这一行重新安装，会出什么问题？

**答案**：安装出来的 `verl` 包里将**不包含 yaml 配置文件**。于是当代码（用 Hydra）去加载 `verl/trainer/config/ppo_trainer.yaml` 时会找不到文件而报错。这一行就是为了把「非 `.py` 的资源文件」一起打进包里。

---

### 4.4 pyproject.toml：现代化的主入口

#### 4.4.1 概念说明

`pyproject.toml` 是 Python 打包的「新标准」（PEP 517/518/621）。它用一种叫 **TOML** 的结构化配置格式，把 `setup.py` 里那段 Python 代码要做的事，全部用「键值对」声明出来。它的好处是：**不依赖运行 Python 代码就能解析**，更安全、更规范。

一个 `pyproject.toml` 通常分三块：

- `[build-system]`：用什么工具来构建这个包（构建后端）。
- `[project]`：包的元数据（名字、版本、依赖、作者……），对应 `setup.py` 里的 `setup(...)` 参数。
- `[tool.*]`：各种工具（setuptools、pytest、yapf……）的额外配置。

在本仓库里，`pyproject.toml` 和 `setup.py` **描述的是同一个包**（都叫 `verl`，版本都来自同一个文件），只是一个为主、一个为备用。

#### 4.4.2 核心流程

当你在仓库根目录执行 `pip install -e .`，pip 的决策顺序大致是：

```
1. 找 pyproject.toml → 找到了
2. 读 [build-system] → 用 setuptools.build_meta 作为构建后端
3. 读 [project] 元数据 → 包名 verl，动态版本（从文件读）
4. 执行可编辑安装
（如果 pyproject.toml 缺失或损坏，才会退回去找 setup.py）
```

#### 4.4.3 源码精读

**构建后端声明**。[pyproject.toml:4-9](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/pyproject.toml#L4-L9) 说明用 setuptools 来构建：

```toml
[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"
```

**包名与动态版本**。[pyproject.toml:14-19](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/pyproject.toml#L14-L19) 声明包名是 `verl`，版本是「动态」的（不从 toml 里写死，而是从文件读）：

```toml
[project]
name = "verl"
dynamic = ["version"]
```

动态版本具体从哪读？看 [pyproject.toml:64-66](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/pyproject.toml#L64-L66)：

```toml
[tool.setuptools.dynamic]
version = {file = "verl/version/version"}
```

这和 `setup.py` 读同一个 `verl/version/version` 文件——再次印证「两个构建入口，单一数据源」。

**依赖列表**。[pyproject.toml:32-44](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/pyproject.toml#L32-L44) 列出依赖，对应 `setup.py` 的 `install_requires`：

```toml
dependencies = [
    "accelerate", "codetiming", "datasets", "dill", "hydra-core",
    "numpy", "pybind11", "ray", "tensordict", "transformers<4.48",
    "vllm<=0.6.3",
]
```

> 对比一下：`pyproject.toml` 的 dependencies 和 `requirements.txt` 高度重合，但**不完全一致**。比如 `pyproject.toml` 这里没列 `flash-attn`、`wandb`、`pandas`，而 `requirements.txt` 里列了；而且 `pyproject.toml` 对 `tensordict` **没有** `<0.6` 的上限，`requirements.txt` 却有。这是因为 toml 描述的是「打包发布的最小依赖」，requirements.txt 描述的是「完整运行/开发环境」——两者用途不同，差别是正常的，但也意味着改依赖时要注意改的是哪一份。

**附带数据文件**。[pyproject.toml:74-78](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/pyproject.toml#L74-L78) 对应 `setup.py` 的 `package_data`，声明 yaml 与版本文件要打包：

```toml
[tool.setuptools.package-data]
verl = [
  "version/*",
  "trainer/config/*.yaml"
]
```

#### 4.4.4 代码实践

**实践目标**：建立「pyproject.toml 是主入口、setup.py 是备用」的直观认识。

**操作步骤**：

1. 用 Python 自带（3.11+）或 `pip install tomli` 解析 `pyproject.toml`，打印包名和依赖（**示例代码**）：
   ```python
   # 示例代码：读取 pyproject.toml 元数据
   try:
       import tomllib          # Python 3.11+
   except ModuleNotFoundError:
       import tomli as tomllib
   with open('pyproject.toml', 'rb') as f:
       data = tomllib.load(f)
   print('name =', data['project']['name'])
   print('version is dynamic =', 'version' in data['project']['dynamic'])
   print('num deps =', len(data['project']['dependencies']))
   ```
2. 思考题（不用动手）：如果 `pyproject.toml` 被误删，`pip install -e .` 还能成功吗？

**需要观察的现象**：步骤 1 打印 `name = verl`、`version is dynamic = True`、依赖条数约为 11。

**预期结果**：你能口头回答「删掉 pyproject.toml 后，pip 会回退到 setup.py，所以仍能安装；这就是 setup.py 作为 fallback 的意义」。

#### 4.4.5 小练习与答案

**练习 1**：`pyproject.toml` 里 `[project]` 的 `version` 没有直接写数字，而是写 `dynamic = ["version"]`。为什么不直接写 `version = "0.1"`？

**答案**：为了让版本号「只在 `verl/version/version` 一个地方维护」。如果 toml 里也写、version 文件也写，就出现了两处来源，更新时容易忘记同步。用 `dynamic` + `file = "verl/version/version"` 让两边读同一个文件，避免不一致。

**练习 2**：对比 `pyproject.toml` 的 `dependencies` 和 `requirements.txt`，哪个范围更大？为什么？

**答案**：`requirements.txt` 范围更大（多了 `flash-attn`、`wandb`、`pandas`，且 `tensordict` 多了 `<0.6` 约束）。因为 `pyproject.toml` 的 dependencies 是「别人 `pip install verl` 时必须满足的最小核心依赖」，应尽量精简；而 `requirements.txt` 面向「在本仓库做开发/复现实验的人」，需要包含记录、画图、注意力加速等完整工具链。

---

### 4.5 仓库目录结构与各目录职责

#### 4.5.1 概念说明

装好之后，更重要的是知道「东西都放在哪」。一个训练框架的目录通常遵循这样的分工：

- **入口/脚本**放最外层（`scripts/`），让人一眼能找到「怎么跑」。
- **示例与数据预处理**放 `examples/`，是「改实验」的入口。
- **框架核心源码**放 `verl/`，是「读实现」的入口。
- **测试**放 `tests/`，是「验证正确性」的地方。
- **文档**放 `docs/`。

TinyZero 把这些分得很清楚。理解了这张地图，后面每一篇讲义你都能快速定位「它在讲哪个目录」。

需要再次强调：`verl/` 占据了仓库绝大多数代码量，它就是 vendored 进来的 veRL 框架；而 TinyZero 自己的贡献主要集中在 `examples/data_preprocess/`（任务数据生成）和 `verl/utils/reward_score/`（规则奖励函数）这两处。

#### 4.5.2 核心流程

下面是仓库顶层目录与各自职责的一览（基于 `git ls-files` 的真实结果整理）：

```
TinyZero/
├── README.md            # 项目首页（安装、快速开始）
├── OLD_README.md        # 归档的旧版说明
├── setup.py             # 备用安装脚本（4.3）
├── pyproject.toml       # 主安装元数据（4.4）
├── requirements.txt     # 依赖清单（4.2）
├── scripts/             # 训练/格式化脚本（入口）
│   ├── train_tiny_zero.sh   # ★ TinyZero 的主训练入口
│   └── format.sh            # 代码格式化
├── examples/            # 数据预处理 + 各类训练脚本示例
│   ├── data_preprocess/     # ★ countdown/multiply 等数据生成（u2）
│   ├── ppo_trainer/         # PPO 训练示例脚本
│   ├── grpo_trainer/        # GRPO 训练示例脚本（u5-l5）
│   └── sft/  generation/  ray/  split_placement/
├── verl/                # ★ 框架核心源码（本系列主战场）
│   ├── trainer/             # 训练入口与主循环（u4）
│   ├── workers/             # Actor/Critic/Rollout 等 worker（u6）
│   ├── single_controller/   # Ray 单控制器调度（u3）
│   ├── utils/               # 数据/奖励/并行等工具（u2、u7）
│   ├── protocol.py          # DataProto 数据协议（u3-l1）
│   ├── models/  third_party/  version/
├── tests/               # 单元/端到端测试（u7-l5）
├── docs/                # Sphinx 文档
├── docker/              # 可选的镜像构建文件
└── patches/             # Megatron 等第三方补丁
```

打 ★ 的是 TinyZero 学习路线里**最重要**的几个目录。

#### 4.5.3 源码精读

我们重点看 `verl/` 内部，因为它会是后续讲义的主战场。基于 `git ls-files` 的真实结构，`verl/` 的二级目录如下：

| `verl/` 子目录 / 文件 | 职责一句话 | 后续讲义 |
| --- | --- | --- |
| `verl/trainer/` | 训练入口 `main_ppo.py` 与 PPO 主循环 `ppo/ray_trainer.py`、算法 `ppo/core_algos.py`、配置 `config/*.yaml` | u1-l4、u4、u5 |
| `verl/workers/` | 各角色 worker：`actor/`（策略）、`critic/`（价值）、`rollout/`（生成）、`reward_model/`、`sharding_manager/`，以及聚合入口 `fsdp_workers.py`、`megatron_workers.py` | u6、u7 |
| `verl/utils/` | 工具集：`dataset/`（数据加载）、`reward_score/`（规则奖励）、`seqlen_balancing.py`（序列均衡）、`torch_functional.py`（masked_mean 等）、`tracking.py`（wandb 日志） | u2、u5、u7 |
| `verl/single_controller/` | Ray 单控制器：`base/`（装饰器、worker 抽象）、`ray/`（RayWorkerGroup、资源池） | u3 |
| `verl/protocol.py` | 统一数据容器 `DataProto` | u3-l1 |
| `verl/models/` | 模型相关封装 | — |
| `verl/third_party/vllm/` | 与 vLLM 推理引擎的桥接 | u6-l4、u6-l5 |
| `verl/version/` | 版本号文件（`0.1`） | 本讲 4.3 |

再看另外几个顶层目录的真实内容，印证上面的「一句话职责」：

- **`scripts/`** 真实只有两个文件：`train_tiny_zero.sh`（TinyZero 的训练总入口，u1-l3 会逐行读）和 `format.sh`（代码格式化）。
- **`examples/data_preprocess/`** 真实包含 `countdown.py`、`multiply.py`、`arth.py`、`gsm8k.py` 等——这些就是 u2 要讲的「任务数据生成」。
- **`tests/`** 真实包含 `e2e/`（端到端训练测试，u7-l5 会用）、`rollout/`、`model/`、`sanity/` 等子目录。
- **`docker/`** 真实有 `Dockerfile.ngc.vllm` 和 `Dockerfile.vemlp.vllm.te` 两个镜像文件——如果你嫌手动装依赖麻烦，也可以用它们起一个现成环境（属于可选方式，README 的主流程没提）。
- **`patches/`** 真实有 `megatron_v4.patch`，是 Megatron 后端（u7-l4）所需的第三方补丁。

> 读源码的「定位技巧」：当你后续看到讲义里提到某个文件，比如 `verl/workers/fsdp_workers.py`，你可以立刻根据这张表知道「它在 workers 下，是 FSDP 后端的 worker 聚合入口」——这就是目录地图的价值。

#### 4.5.4 代码实践

**实践目标**：亲手把仓库目录结构打印出来，并为四个关键目录各写一句职责说明。

**操作步骤**：

1. 进入仓库根目录，用 `ls` 查看顶层与 `verl/` 下面的子目录：
   ```bash
   ls -1                          # 顶层
   ls -1 verl                     # verl/ 二级
   ```
   如果你装了 `tree`，可以 `tree verl -L 2 -d` 只看目录（看不到也没关系，`ls` 足够）。
2. 用 `git ls-files` 统计每个顶层目录有多少个文件，确认你看到的结构是「仓库实际跟踪的」：
   ```bash
   git ls-files verl/trainer | wc -l
   git ls-files verl/workers  | wc -l
   git ls-files verl/utils     | wc -l
   ```
3. 在你的笔记里，为下面四个目录各写一句不超过 20 字的中文职责说明（参考 4.5.3 的表格，但**用自己的话**）：
   - `examples/`
   - `verl/trainer/`
   - `verl/workers/`
   - `verl/utils/`

**需要观察的现象**：

- `ls -1 verl` 能列出 `trainer workers utils single_controller protocol.py models third_party version __init__.py` 等条目。
- `git ls-files verl/trainer | wc -l` 等命令各返回一个正整数（比如 trainer 几十个文件）。

**预期结果（示例答案，供你对照）**：你产出四句职责说明，例如「`examples/`：数据预处理脚本与各类训练示例；`verl/trainer/`：训练入口与 PPO 主循环；`verl/workers/`：Actor/Critic/Rollout 等计算角色；`verl/utils/`：数据、奖励、并行等通用工具」。

#### 4.5.5 小练习与答案

**练习 1**：你想找「countdown 任务的奖励函数」应该去哪个目录？为什么？

**答案**：去 `verl/utils/reward_score/`。因为根据 4.5.3 的表格，`verl/utils/` 下的 `reward_score/` 专门放「规则奖励函数」，`countdown.py` 就在那里（u2-l4 会精读）。这也体现了「按职责分目录」的好处：找东西有规律可循。

**练习 2**：`scripts/train_tiny_zero.sh` 和 `examples/ppo_trainer/run_qwen2-7b.sh` 都是 shell 脚本，为什么一个放在 `scripts/`、一个放在 `examples/`？

**答案**：`scripts/` 放的是「本项目自己的、面向用户的总入口」，`train_tiny_zero.sh` 是 TinyZero 的官方训练脚本；`examples/` 放的是「框架能力演示/参考用法」，`examples/ppo_trainer/*.sh` 是 veRL 上游带来的、面向各种模型/场景的示例脚本，供你参考改写。前者是「直接拿来跑」，后者是「拿来学/改」。

---

## 5. 综合实践

把本讲学的「安装流程 + 构建文件 + 目录结构」串成一个综合任务：

**任务：为 TinyZero 画一张「环境与代码地图」**

1. **复现安装**（条件允许时）：新建 `conda` 环境 `zero`，按 4.1.2 流水线装到 `pip install -e .`，用 `python -c "import verl; print(verl.__version__)"` 验证得到 `0.1`。无 GPU 环境可跳过 vllm/torch，只验证构建可解析。
2. **追溯版本号**：用编辑器打开 `setup.py`、`pyproject.toml`、`verl/__init__.py`、`verl/version/version` 四个文件，把「版本号 0.1 是怎么一步步被读到的」画成一条数据流图（谁读了哪个文件、传给谁）。
3. **画目录树**：用 `ls` / `tree` 生成 `verl/` 的二级目录树，标注每个子目录将在「哪一篇后续讲义」里被精读（参考 4.5.3 的表格）。
4. **写一段复盘**（不超过 200 字）：解释「为什么 TinyZero 既能用 `pip install -e .` 装成一个叫 verl 的包，又能直接在仓库里读改源码」——把可编辑安装、`find_packages`、`package_data` 这几个概念串起来。

**预期产出**：一张数据流图 + 一张目录树 + 一段复盘文字。这个任务的目的是让你从「会敲安装命令」升级到「理解安装背后发生了什么、代码是如何组织的」，为从 u2 开始的源码精读打好地图基础。

## 6. 本讲小结

- TinyZero 的安装是**分层**的：conda 提供 Python → torch 提供 GPU 运行时 → vllm 提供推理 → ray 提供分布式 → `pip install -e .` 把 verl 以**可编辑**方式装进来。注意 vllm 要在 verl 之前装。
- `requirements.txt` 是朴素依赖清单，既供人 `pip install -r` 用，也被 `setup.py` 读取转成 `install_requires`；其中 `tensordict<0.6`、`transformers<4.48`、`vllm<=0.6.3` 三个带上限约束（前一个仅在 requirements.txt），是为了锁住 API 敏感包的版本天花板。
- `setup.py` 是**备用**安装脚本，`pyproject.toml` 是**主**入口，两者描述同一个包 `verl`、都从 `verl/version/version` 读版本号（`0.1`），构成「单一数据源」；`pyproject.toml` 被误删时才会回退到 `setup.py`。
- `package_data` / `package-data` 的作用是把 yaml 配置、版本文件等**非 `.py` 资源**打进包里，否则 Hydra 加载 `ppo_trainer.yaml` 会找不到文件。
- 仓库目录分工清晰：`scripts/` 是入口脚本、`examples/` 是数据预处理与示例（TinyZero 贡献集中地）、`verl/` 是框架核心（`trainer` 训练、`workers` 角色、`utils` 工具、`single_controller` 调度、`protocol.py` 数据协议）、`tests/` 测试、`docs/` 文档。

## 7. 下一步学习建议

你已经把环境装好、把目录地图建好，下一步建议：

- **u1-l3「跑通第一次训练：Countdown 任务」**：把本讲的 `scripts/train_tiny_zero.sh` 真正逐行读懂，并跑通端到端训练。这是从「看目录」到「跑流程」的跨越。
- 想提前理解「配置是怎么被读进来的」，可以先把 `verl/trainer/config/ppo_trainer.yaml` 扫一遍，它会在 **u1-l4「配置系统：Hydra 与 ppo_trainer.yaml」** 里被精读。
- 想验证自己环境是否健康，可以翻一眼 `tests/e2e/`（**u7-l5** 会讲），那里有一个最小化的端到端训练测试，适合用来检查安装。

> 本系列从下一篇起正式进入源码：u2 讲「数据与任务定义」（`examples/data_preprocess/` 与 `verl/utils/reward_score/`），u3 讲「数据协议与单控制器」（`verl/protocol.py` 与 `verl/single_controller/`）。你今天建立的目录地图，就是那时候的导航。

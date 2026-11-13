# 安装与运行：setup.py 打包方式和第一个可运行示例

> 本讲是学习手册第 1 单元第 2 讲（u1-l2），承接 u1-l1 对 Muon 定位的介绍。上一讲我们已经知道：Muon 是只用于隐藏层二维权重矩阵的优化器，整个项目的核心实现集中在约 290 行的 `muon.py` 中。本讲解决的问题是——**把这个项目装进你的 Python 环境，并跑通第一个最小训练循环**。

## 1. 本讲目标

学完本讲，你应该能够：

1. 掌握 `pip install git+https://github.com/KellerJordan/Muon` 这种「从 git 仓库直接安装」的方式，并能在本地验证安装是否成功。
2. 读懂 `setup.py`，理解 `py_modules=['muon']` 如何把**单个文件**打包成发行包（distribution），以及为什么 PyPI 包名叫 `muon-optimizer` 而导入名却叫 `muon`。
3. 知道 `import muon` 之后能拿到哪些对象（3 个函数 + 4 个优化器类），以及为什么**必须先装好 PyTorch**。
4. 确认本机 torch 环境可用，并用 `SingleDeviceMuon` 在一个 CPU 就能跑的 toy 回归任务上完成 50 步训练，为后续所有实践打底。

## 2. 前置知识

### 2.1 pip、PyPI 与「从 git 安装」

- **pip**：Python 的包管理器，负责把别人写好的代码装进你的 Python 环境（准确说是 `site-packages` 目录）。
- **PyPI**：Python 官方的包仓库，`pip install numpy` 就是从这里下载。
- **从 git 安装**：`pip install git+https://github.com/xxx/yyy` 表示让 pip 现场**克隆这个 git 仓库**，在临时目录里执行打包流程，再把产物安装进环境。这种方式适合那些你只想「直接用最新代码」的小型研究项目，Muon 就是典型例子。

### 2.2 setuptools 与 setup.py：包有两层名字

`setup.py` 是老牌打包工具 setuptools 的配置脚本。这里有一个初学者最容易混淆的点——一个 Python 包有**两个名字**：

| 概念 | 本项目的取值 | 在哪里定义 | 什么时候用 |
|---|---|---|---|
| 发行包名（distribution name） | `muon-optimizer` | `setup.py` 的 `name=` 参数 | `pip install` / `pip show` / `pip uninstall` 时 |
| 导入名（import name） | `muon` | `py_modules=['muon']` 指向的文件名 `muon.py` | 代码里 `from muon import ...` 时 |

两者**可以不一样**，本项目就是故意设计成不一样的。所以你用 `pip uninstall muon` 会失败，必须写 `pip uninstall muon-optimizer`。

### 2.3 模块（module）vs 包（package）

- **模块**：一个单独的 `.py` 文件就是一个模块，文件名（去掉 `.py`）就是模块名。
- **包**：一个含 `__init__.py` 的**目录**，用来组织多个模块。

setuptools 打包时二选一：`packages=[...]` 打包目录形式的包，`py_modules=[...]` 打包根目录下的单个模块文件。Muon 整个项目就一个 `muon.py`，所以用的是后者。

### 2.4 虚拟环境（建议）

不同项目对包版本的要求可能冲突，建议每个学习项目用一个独立虚拟环境：

```bash
python -m venv .venv
source .venv/bin/activate    # Windows 用 .venv\Scripts\activate
```

### 2.5 torch.distributed：import 不等于初始化

上一讲提过 `muon.py` 里有分布式版本（`Muon`、`MuonWithAuxAdam`）和单机版本（`SingleDeviceMuon`、`SingleDeviceMuonWithAuxAdam`）。文件顶部导入了 `torch.distributed`（分布式训练通信库），这里你只需要记住一条：**`import torch.distributed` 这个动作本身不会建立任何进程组**。只有在调用 `dist.init_process_group(...)` 之后，`dist.get_world_size()` 这类函数才能正常工作。本讲稍后会用源码证明：单机版的两个类从头到尾不碰任何 `dist.*` 调用，所以**没有 GPU、没有多卡，照样能学习和验证 Muon**。

## 3. 本讲源码地图

本讲涉及的文件极少，正好体现这个项目「麻雀虽小」的特点：

| 文件 | 行数范围 | 在本讲中的角色 |
|---|---|---|
| `setup.py` | 全文 1-28 行 | 打包配置：决定包名、版本、包含哪些模块 |
| `README.md` | 第 5-9 行 | 安装命令 |
| `README.md` | 第 11-47 行 | 用法示例与调参建议 |
| `muon.py` | 第 1-2 行 | 模块导入部分（本讲主角之一） |
| `muon.py` | 全文 | 安装后 `import muon` 能拿到的全部对象 |

整个仓库只有 5 个被 git 跟踪的文件（`README.md`、`muon.py`、`setup.py`、`LICENSE`、`.gitignore`），没有任何测试目录、示例目录或 CI 配置——所以本讲的实践属于「依据 README 用法 + 源码自行构造最小示例」型。

## 4. 核心概念与源码讲解

### 4.1 setup.py：29 行把一个文件变成一个包

#### 4.1.1 概念说明

`setup.py` 回答三个问题：这个包**叫什么**（name）、**版本是多少**（version）、**里面装了什么代码**（packages 或 py_modules）。读完这 29 行，你就能明白 `pip install git+...` 之后你的环境里多了什么。

#### 4.1.2 核心流程

pip 从 git 安装本项目时的流程：

1. pip 克隆 `https://github.com/KellerJordan/Muon` 到临时目录。
2. pip 发现仓库根目录有 `setup.py`，于是用 setuptools 执行它。
3. setuptools 读取 `py_modules=['muon']`，把 `muon.py` 这一个文件复制进 wheel（Python 的标准安装包格式）。
4. wheel 被安装进当前环境的 `site-packages/`，此后任何目录下的 Python 进程都能 `import muon`。

#### 4.1.3 源码精读

先看文件开头和打包方式的选择：

```python
from setuptools import setup, find_packages

setup(
    name='muon-optimizer',
    version='0.1.0',
```

- [setup.py:L1-L5](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/setup.py#L1-L5)：第 1 行导入了 `setup` 和 `find_packages`；第 4-5 行声明发行包名 `muon-optimizer`、版本 `0.1.0`。注意描述文字里有个原文拼写小瑕疵 `'Muon opimizer'`（少了一个 t），这无伤大雅，但提醒我们：元数据是手写的，别把它当成文档权威。

接着是本讲最关键的两行：

```python
    #packages=find_packages(),
    py_modules=['muon'],
```

- [setup.py:L12-L13](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/setup.py#L12-L13)：第 12 行把 `packages=find_packages()` **注释掉了**（`find_packages` 因此成了第 1 行那个「导入了却没用上」的遗留导入），第 13 行改用 `py_modules=['muon']`。这就是「单文件打包」的核心：告诉 setuptools 这个发行包里只有一个模块——仓库根目录的 `muon.py`。也正是文件名 `muon.py` 决定了导入名是 `muon`。

再看元数据部分：

```python
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    url='https://github.com/KellerJordan/Muon',
```

- [setup.py:L9-L11](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/setup.py#L9-L11)：把 README 全文作为包的「长描述」（会显示在 PyPI 项目页上），并声明它是 Markdown 格式；`url` 是项目主页。

最后注意两个**没有出现**的东西：

- **没有 `install_requires`**：即 `setup.py` 没有声明依赖 `torch`。这意味着 pip 装 `muon-optimizer` 时**不会**自动帮你装 PyTorch——如果你在裸环境里 `import muon`，会得到 `ModuleNotFoundError: No module named 'torch'`。正确顺序是**先装 torch，再装 muon**。
- **没有 `python_requires`**：没有硬性 Python 版本约束（classifiers 里罗列了 3.7-3.11，见 [setup.py:L14-L27](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/setup.py#L14-L27)，但那只是展示性标签）。

#### 4.1.4 代码实践

1. **实践目标**：确认包的「两个名字」确实不同，并亲眼看到安装产物。
2. **操作步骤**（示例命令，请在自己的环境中执行）：
   ```bash
   pip install git+https://github.com/KellerJordan/Muon
   pip show muon-optimizer        # 用发行包名查询
   pip uninstall muon             # 故意用错名字
   pip uninstall muon-optimizer   # 再用正确名字（先回答 n 别真卸载）
   ```
   然后在 Python 里：
   ```python
   import muon
   print(muon.__file__)   # 打印该模块安装到了 site-packages 的哪个位置
   ```
3. **需要观察的现象**：`pip show muon-optimizer` 能列出 Name/Version（应为 0.1.0）；`pip uninstall muon` 提示 WARNING: Skipping muon as it is not installed；`muon.__file__` 指向当前环境 `site-packages/muon.py`。
4. **预期结果**：三项现象如上。具体的 `__file__` 路径因环境而异，**待本地验证**。

> 提示：因为我们是从 git 安装，pip 只取仓库快照，不会在本地留下 git 仓库；想边读源码边实验，可以 `git clone https://github.com/KellerJordan/Muon` 后在克隆目录里 `pip install .`，效果等价。另外还有一个适合本手册后续讲义的「土办法」：整个包就是一个文件，直接把 `muon.py` 复制进你的练习目录也能 `import muon`——第二单元的改造实验会用到这个技巧。

#### 4.1.5 小练习与答案

**练习 1**：为什么安装后代码里写的是 `import muon`，而不是 `import muon_optimizer`？

**答案**：导入名由被打包的模块文件名决定（`py_modules=['muon']` 对应 `muon.py`），而 `name='muon-optimizer'` 只是 pip 层面的发行包名，两者互不相干。

**练习 2**：第 1 行导入了 `find_packages`，第 12 行却把 `packages=find_packages()` 注释掉。`packages` 和 `py_modules` 分别在什么场景下使用？

**答案**：`packages` 用于打包目录形式的包（带 `__init__.py` 的目录，比如 `mylib/core/`）；`py_modules` 用于打包位于仓库根目录的**单个模块文件**。本仓库没有包目录，全部实现就在一个 `muon.py` 里，所以用 `py_modules`。注释痕迹说明作者从「目录打包」方案改成了「单文件」方案。

**练习 3**：在一个从未装过 PyTorch 的虚拟环境里直接 `pip install git+https://github.com/KellerJordan/Muon` 然后 `import muon`，会发生什么？为什么？

**答案**：会抛 `ModuleNotFoundError: No module named 'torch'`。因为 `setup.py` 没有 `install_requires=['torch']`，pip 不会自动安装 PyTorch；而 `muon.py` 第 1 行就是 `import torch`。正确做法是先 `pip install torch`。

### 4.2 README 的安装与用法章节：官方接入配方

#### 4.2.1 概念说明

README 是这个项目唯一的「文档」，它的安装章节只有一行命令，用法章节给出了一段可以直接抄的参数组（param_groups）配方。本节把这两段读透，因为综合实践要完全照着它来做。

#### 4.2.2 核心流程

按 README 接入 Muon 的标准流程：

1. 安装（一行 pip 命令）。
2. 在训练脚本里把模型参数**手工拆成两组**：隐藏层二维权重（走 Muon）与其余参数（走内部 AdamW）。
3. 用两组 dict 构造 `MuonWithAuxAdam`，替换原来的 `torch.optim.AdamW`。
4. 训练循环里只 step 这一个优化器。

#### 4.2.3 源码精读

安装命令：

````markdown
## Installation

```
pip install git+https://github.com/KellerJordan/Muon
```
````

- [README.md:L5-L9](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L5-L9)：官方唯一给出的安装方式，直接从 GitHub 主分支安装最新代码。

用法配方（替换 AdamW）：

```python
from muon import MuonWithAuxAdam
hidden_weights = [p for p in model.body.parameters() if p.ndim >= 2]
hidden_gains_biases = [p for p in model.body.parameters() if p.ndim < 2]
nonhidden_params = [*model.head.parameters(), *model.embed.parameters()]
param_groups = [
    dict(params=hidden_weights, use_muon=True,
         lr=0.02, weight_decay=0.01),
    dict(params=hidden_gains_biases+nonhidden_params, use_muon=False,
         lr=3e-4, betas=(0.9, 0.95), weight_decay=0.01),
]
optimizer = MuonWithAuxAdam(param_groups)
```

- [README.md:L17-L33](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L17-L33)：第 18 行是被注释掉的「改造前」写法（一个 AdamW 管所有参数）；第 23-25 行按 `ndim >= 2` 与名称把参数拆成隐藏权重 / 增益偏置 / 非隐藏（头+embedding）三类；第 26-31 行的关键是每组都带一个 `use_muon` 标志——`True` 的组走 Muon（注意其 `lr=0.02`，比 AdamW 组的 `3e-4` 大两个数量级，呼应上一讲「Muon 的学习率单位是每次更新的谱范数」）；第 32 行用参数组列表构造**单个**优化器。这段代码对应 [muon.py:L138-L164](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L138-L164) 中 `MuonWithAuxAdam` 的 docstring 示例，第三单元（u3-l2）会逐行精读它的构造函数。

- [README.md:L35-L36](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L35-L36)：提醒你要把 `model.body` / `model.head` / `model.embed` 换成自己模型里对应的部分；对卷积网络，除第一层外的卷积核都可以交给 Muon。

调参建议：

- [README.md:L44-L47](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L44-L47)：momentum（0.95）、nesterov（True）、ns_steps（5）用默认值即可，**只有学习率和权重衰减需要调**；且学习率自带 muP 缩放性质（模型变大也不需要重调）。这解释了为什么 `muon.py` 里几乎看不到可调旋钮。

#### 4.2.4 代码实践

1. **实践目标**：验证「torch 必须先装」这一从 `setup.py` 缺失 `install_requires` 推出的结论，并跑通官方安装命令。
2. **操作步骤**（示例命令）：
   ```bash
   python -c "import torch; print(torch.__version__)"   # 第 0 步：确认 torch 已就绪
   pip install git+https://github.com/KellerJordan/Muon
   pip list | grep -i muon
   ```
3. **需要观察的现象**：第一条命令打印出 torch 版本号（如 2.x.x，具体**待本地验证**）；安装日志里能看到 `Building wheel for muon-optimizer` 字样；`pip list` 里出现 `muon-optimizer 0.1.0`。
4. **预期结果**：安装完成后 `python -c "from muon import MuonWithAuxAdam"` 不报错。

#### 4.2.5 小练习与答案

**练习 1**：README 用法示例中，`use_muon=True` 组和 `use_muon=False` 组的默认学习率相差多少倍？为什么可以差这么多？

**答案**：0.02 对 3e-4，约 67 倍。因为两者单位不同：Muon 的 lr 以「每次更新的谱范数」为单位（正交化后更新各方向能量被拉平），AdamW 的 lr 作用于逐元素归一化的更新量。这正是 u1-l1 强调过的「lr 量纲不同」。

**练习 2**：README 只给了 `pip install git+...` 一种安装方式。如果你想把本手册讲解的特定 commit（f98f1ca）装进环境，pip 命令该怎么写？

**答案**：pip 支持在 git URL 后用 `@` 指定 ref，例如 `pip install git+https://github.com/KellerJordan/Muon@f98f1ca`。这样能保证你的运行环境与本手册的永久链接行号完全对应（`@` 语法是 pip 的通用功能，非本仓库代码）。

### 4.3 muon.py 的导入部分与可导入对象清单

#### 4.3.1 概念说明

`import muon` 会执行 `muon.py` 的顶层代码。顶层代码只有两行 import，但它们决定了两件重要的事：这个模块的**依赖**（torch），以及它在什么条件下**不会**出问题（不需要分布式环境）。同时，由于 Python 模块没有 `__all__` 声明，文件里所有顶层名字都是公开可导入的。

#### 4.3.2 核心流程

`from muon import X` 时发生的事：

1. Python 在 `site-packages` 找到 `muon.py` 并开始执行。
2. 第 1 行 `import torch` → 若环境无 torch，立刻失败（这是 4.1 练习 3 的根因）。
3. 第 2 行 `import torch.distributed as dist` → 只是绑定了名字 `dist`，**没有**建立进程组，也不需要多卡。
4. 函数与类的定义语句依次执行（只定义、不运行），随后 `muon` 模块对象就绪，其下挂载了 3 个函数和 4 个类。

#### 4.3.3 源码精读

```python
import torch
import torch.distributed as dist
```

- [muon.py:L1-L2](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L1-L2)：整个模块的全部顶层导入。注意：文件里**没有**任何 `dist.init_process_group` 调用——初始化进程组是使用者的启动脚本（如 torchrun）的职责。`dist.*` 函数只在 `Muon.step`（[muon.py:L81-L94](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L81-L94)）和 `MuonWithAuxAdam.step` 的 muon 分支（[muon.py:L195-L208](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L195-L208)）内部被调用；`SingleDeviceMuon`（L99-L127）和 `SingleDeviceMuonWithAuxAdam`（L228-L287）**从头到尾没有一处 `dist.` 调用**。这就是「单机版」的全部含义，也是本手册大量实践可以用一台笔记本 CPU 完成的原因。

安装后你能导入的全部对象（本表同时是 u1-l3 代码地图的预习）：

| 对象 | 类型 | 位置 | 一句话用途 |
|---|---|---|---|
| `zeropower_via_newtonschulz5` | 函数 | [muon.py:L5-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L5-L31) | Newton-Schulz 迭代做正交化 |
| `muon_update` | 函数 | [muon.py:L34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41) | 动量 + 正交化，算出一次参数更新 |
| `Muon` | 类 | [muon.py:L44-L96](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L44-L96) | 分布式纯 Muon 优化器 |
| `SingleDeviceMuon` | 类 | [muon.py:L99-L127](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L99-L127) | 单机纯 Muon 优化器（本讲实践主角） |
| `adam_update` | 函数 | [muon.py:L130-L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130-L135) | 30 行手写 AdamW 单步更新 |
| `MuonWithAuxAdam` | 类 | [muon.py:L138-L225](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L138-L225) | 分布式 Muon+AdamW 单优化器 |
| `SingleDeviceMuonWithAuxAdam` | 类 | [muon.py:L228-L287](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L228-L287) | 单机 Muon+AdamW 单优化器 |

本讲综合实践要用到的 `SingleDeviceMuon` 构造函数：

```python
def __init__(self, params, lr=0.02, weight_decay=0, momentum=0.95):
    defaults = dict(lr=lr, weight_decay=weight_decay, momentum=momentum)
    super().__init__(params, defaults)
```

- [muon.py:L103-L105](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L103-L105)：三个默认超参（lr=0.02、weight_decay=0、momentum=0.95）直接交给 `torch.optim.Optimizer` 父类。对比分布式版 `Muon.__init__` 在 [muon.py:L67-L68](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L67-L68) 有 `assert isinstance(params, list)...` 和按 size 排序两步额外操作（那是为 all_gather 分片服务的，u3-l3 再讲）；单机版**没有**这个断言，所以直接传 `model.parameters()` 生成器也没问题。

还有一个会在实践中「坑」到你的断言：

```python
assert G.ndim >= 2
```

- [muon.py:L15](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L15)：正交化函数要求输入至少是二维矩阵。一维参数（bias、LayerNorm 的 gain）如果被喂进 `SingleDeviceMuon`，会在第一次 `step()` 时触发这里的 `AssertionError`。这就是为什么 README 配方里用 `p.ndim >= 2` 筛参数——也是下面综合实践中模型全部用 `bias=False` 的原因。

#### 4.3.4 代码实践

1. **实践目标**：确认 `import muon` 拿到的对象与上表一致，并验证「单机版不碰 dist」。
2. **操作步骤**（示例代码，非项目原有代码）：
   ```python
   from muon import Muon, SingleDeviceMuon, MuonWithAuxAdam, SingleDeviceMuonWithAuxAdam
   for cls in (Muon, SingleDeviceMuon, MuonWithAuxAdam, SingleDeviceMuonWithAuxAdam):
       print(cls.__name__, cls.__module__)

   import inspect, muon
   src = inspect.getsource(SingleDeviceMuon)   # 只取单机类自己的源码
   print("dist." in src)                        # 期望 False
   ```
3. **需要观察的现象**：四行打印的 `__module__` 都是 `muon`；最后一行打印 `False`。
4. **预期结果**：如上；若 `dist.` 检查返回 `True`，说明你取错了类或环境里装的 muon.py 版本不同（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：`import muon` 成功后，紧接着调用 `torch.distributed.get_world_size()` 会怎样？这说明什么？

**答案**：会抛 RuntimeError（默认后端未初始化，进程组不存在）。说明 `import torch.distributed` 只是导入模块，不产生任何分布式状态；进程组必须由使用者显式调用 `dist.init_process_group` 建立，而 `muon.py` 自己从不调用它。

**练习 2**：如果你把一个带 bias 的 `nn.Linear` 的所有参数都传给 `SingleDeviceMuon` 并执行 `step()`，会在哪一行、以什么方式失败？

**答案**：bias 是一维张量，进入 `muon_update` 后传给 `zeropower_via_newtonschulz5`，在 [muon.py:L15](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L15) 的 `assert G.ndim >= 2` 处抛出 `AssertionError`。正确做法是把一维参数过滤掉交给 AdamW（这正是 `MuonWithAuxAdam` 存在的意义）。

**练习 3**：`SingleDeviceMuon(model.parameters(), lr=0.02)` 与 `Muon(model.parameters(), lr=0.02)` 的构造过程差在哪两步？

**答案**：`Muon.__init__`（[muon.py:L67-L68](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L67-L68)）多做了：断言 params 是非空 Parameter 列表；按参数 size 降序排序。这两步都服务于分布式 all_gather 的负载均衡，单机版不需要，所以 `SingleDeviceMuon` 可以直接接受 `model.parameters()` 迭代器。

## 5. 综合实践

把本讲三块内容串成一个任务：**安装 → 验证导入 → 用 SingleDeviceMuon 跑通第一个 50 步训练循环**。

**实践目标**：得到一个可复用的 Muon 实验环境，并亲眼看到 loss 在 Muon 优化下下降。

**操作步骤**：

1. 准备环境（示例命令）：
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install torch            # 必须先装 torch（setup.py 未声明依赖）
   pip install git+https://github.com/KellerJordan/Muon
   ```
2. 验证导入（示例代码）：
   ```python
   from muon import Muon, SingleDeviceMuon, MuonWithAuxAdam, SingleDeviceMuonWithAuxAdam
   for cls in (Muon, SingleDeviceMuon, MuonWithAuxAdam, SingleDeviceMuonWithAuxAdam):
       print(cls.__name__, "->", cls.__module__)
   ```
3. 编写训练脚本 `practice_u1l2.py`（示例代码，非项目原有代码）：
   ```python
   import torch
   from muon import SingleDeviceMuon

   torch.manual_seed(0)

   # toy 回归任务：Y = X @ W_true，数据用 torch.randn 合成，CPU 即可运行
   N, D_IN, D_OUT = 1024, 64, 8
   W_true = torch.randn(D_IN, D_OUT)
   X = torch.randn(N, D_IN)
   Y = X @ W_true

   # 两层无 bias 的 MLP：所有参数都是二维矩阵，符合 Muon 的适用范围
   # （若加 bias，一维参数会触发 muon.py:L15 的 ndim 断言）
   model = torch.nn.Sequential(
       torch.nn.Linear(D_IN, 32, bias=False),
       torch.nn.ReLU(),
       torch.nn.Linear(32, D_OUT, bias=False),
   )

   optimizer = SingleDeviceMuon(model.parameters(), lr=0.02)
   print(optimizer)  # 观察参数组结构与默认超参（momentum=0.95 等）

   mse = torch.nn.functional.mse_loss
   print(f"step   0  loss = {mse(model(X), Y).item():.4f}")

   for step in range(1, 51):
       loss = mse(model(X), Y)
       optimizer.zero_grad()
       loss.backward()
       optimizer.step()
       if step % 10 == 0:
           print(f"step {step:3d}  loss = {loss.item():.4f}")
   ```
4. 运行脚本，观察输出。

**需要观察的现象**：

- 第 2 步四行打印的 `__module__` 均为 `muon`。
- `print(optimizer)` 展示的 param_groups 中带有 `lr: 0.02`、`momentum: 0.95`、`weight_decay: 0` 等默认超参。
- 训练日志中 loss 从初始值持续下降；每步都会执行 [muon.py:L123-L125](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L123-L125) 的「算更新 → 乘衰减 → 加更新」三行。

**预期结果**：loss 在 50 步内明显下降（这类随机矩阵回归任务通常能下降一个数量级左右）。具体下降到多少与你机器上的随机种子实现、torch 版本有关，**待本地验证**。若一开始就报 `AssertionError`，几乎一定是模型里混入了一维参数——回到 4.3.5 练习 2 检查。

## 6. 本讲小结

- `setup.py` 用 `py_modules=['muon']` 把仓库根目录的单个 `muon.py` 打包成发行包 `muon-optimizer 0.1.0`；发行包名（pip 用）与导入名（import 用）是两个独立的名字。
- 官方安装方式是 `pip install git+https://github.com/KellerJordan/Muon`；`setup.py` 未声明 `install_requires`，所以必须**先装 torch 再装 muon**。
- `import muon` 能拿到 3 个函数（`zeropower_via_newtonschulz5`、`muon_update`、`adam_update`）和 4 个优化器类（`Muon`、`SingleDeviceMuon`、`MuonWithAuxAdam`、`SingleDeviceMuonWithAuxAdam`）。
- `muon.py` 顶部只导入 `torch` 和 `torch.distributed`，且从不初始化进程组；`dist.*` 调用只出现在两个分布式类的 `step()` 里，`SingleDevice*` 类完全不用分布式，单机 CPU 即可学习。
- Muon 只接受二维及以上参数（正交化函数在 [muon.py:L15](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L15) 有 `ndim >= 2` 断言），toy 实验模型要用 `bias=False` 规避一维参数。

## 7. 下一步学习建议

- 下一讲 **u1-l3「muon.py 代码地图」**：为整个文件画出行号级地图，并给出按「单机/分布式 × 纯 Muon/带辅助 AdamW」两个维度选择优化器类的决策表——本讲的 7 对象表格是它的雏形。
- 建议现在打开 [muon.py:L99-L127](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L99-L127)，对照综合实践刚刚执行过的 `SingleDeviceMuon.step`，先凭直觉读一遍主循环（你现在已经能认出 `muon_update`、`momentum_buffer`、`p.add_` 这些角色），第二单元（u2-l5）会逐行精读它。
- 如果你有余力，可以把综合实践的 `lr` 改成 `0.002` 和 `0.2` 各跑一遍，直观感受「lr 以谱范数为单位」时过大/过小的表现，为 u3-l4 的调参实战积累手感。

# 搭建 Python 环境并验证可运行性

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解「虚拟环境」是什么、为什么本书必须使用独立的虚拟环境（答案：代码依赖一批 2021 年前后的固定版本包）。
2. 用 conda 或 pip + virtualenv 两条路线之一，创建名为 `pyml-book` 的环境并安装指定版本的依赖。
3. 看懂仓库根目录的 `environment.yml` 依赖清单和 `Makefile`，并能用 `make -f Makefile` 一键创建环境。
4. 运行 `python_environment_check.py` 验证环境，读懂它的检查逻辑（Python 版本门槛、逐包 import、版本比较）。
5. 知道在没有本地 GPU 或不想装环境时，可以用仓库自带的 Google Colab 指南运行全部代码。

## 2. 前置知识

- **虚拟环境（virtual environment）**：Python 的包都装在同一个"全局仓库"里，如果不加隔离，A 项目要 `numpy 1.21`、B 项目要 `numpy 2.0`，就会互相覆盖。虚拟环境给每个项目划出一套独立的 Python 解释器和包目录，互不干扰。本书代码固定使用 2021 年前后的包版本，与你自己机器上的新版包大概率冲突，所以独立环境不是可选项，而是必需品。
- **conda 与 pip 的区别**：两者都是包管理器。`pip` 只管 Python 包（从 PyPI 下载）；`conda` 除了 Python 包还能管 Python 解释器本身、C/C++ 依赖库等（从"频道/channel"下载）。conda 的环境还能指定 Python 版本，这是 ch01 推荐用 conda 的原因。
- **conda channel（频道）**：conda 包的下载源。本书使用 `conda-forge`，这是社区维护的频道，包比官方 defaults 频道更全、更新。
- **语义化版本**：`numpy=1.21.2` 里 `1` 是主版本（大改动）、`21` 是次版本（新功能）、`2` 是修订号（修 bug）。`=` 表示"就要这个精确版本"，`>=` 表示"最低不低于"。
- **YAML**：一种用缩进表示层级的配置文件格式，`environment.yml` 用它描述 conda 环境的名字、频道和依赖。
- **Makefile**：`make` 是一个老牌构建工具，Makefile 里定义若干"目标（target）"，每个目标下写若干 shell 命令；在终端敲 `make 目标名` 就会依次执行这些命令。它在这里被用来把两条 conda 命令封装成一条快捷指令。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch01/README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch01/README.md) | 第 1 章的官方环境搭建说明，是全仓库唯一的安装文档入口，包含 conda、pip、make 三条路线和检查脚本用法 |
| [environment.yml](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/environment.yml) | conda 环境描述文件（依赖清单），列出环境名、频道和 7 个固定版本的依赖 |
| [Makefile](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/Makefile) | 定义 `install` 和 `create` 两个目标，用一条 `make` 命令完成环境创建 |
| [python_environment_check.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/python_environment_check.py) | 环境自检脚本：检查 Python 版本与 5 个核心包的版本，逐项打印 `[OK]` 或 `[FAIL]` |
| [README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/README.md) | 根 README，第 27 行把安装指引指向 ch01/README.md，第 29 行给出 Google Colab 备选方案 |
| [ch02/ch02.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py) | 第 2 章脚本。它的开头会导入并调用根目录的环境检查脚本，说明"先过环境检查、再学代码"是本书的工作惯例 |
| supplementary/running-on-colab.pdf | 社区贡献的 Google Colab 运行指南（PDF，在仓库根 README 第 29 行被引用） |

## 4. 核心概念与源码讲解

### 4.1 conda/pip 环境管理

#### 4.1.1 概念说明

搭建环境有两条主流路线，本书两种都支持：

- **conda 路线**：用 `conda create` 一条命令同时指定 Python 版本和全部包版本，最省事，是书中首选（书中还建议通过 Miniforge 安装 conda）。
- **pip + virtualenv 路线**：`virtualenv` 只负责创建隔离目录，装包交给 `pip`。适合不想装 conda 的读者。

无论哪条路线，目标都一样：得到一个名为 `pyml-book` 的环境，里面有 `python 3.9`、`numpy 1.21.2`、`scipy 1.7.0`、`scikit-learn 1.0`、`matplotlib 3.4.3`、`pandas 1.3.2`。

为什么把这些版本记得这么死？因为书中的代码、图示、默认参数都是在这套版本上写出来并验证过的。版本漂移可能带来 API 变化（例如 matplotlib 后续版本改过一些绘图行为），届时报错会和"算法理解"混在一起，干扰学习。

#### 4.1.2 核心流程

conda 路线的完整流程：

```text
安装 Miniforge(含 conda)
    ↓
conda create -n pyml-book python=3.9 numpy=1.21.2 ...   # 创建环境并装包
    ↓
conda activate pyml-book                                # 激活(进入)环境
    ↓
python python_environment_check.py                      # 自检(见 4.3)
    ↓
conda install -c conda-forge jupyterlab                 # 按需补装 notebook 工具
```

pip + virtualenv 路线：

```text
pip install virtualenv
    ↓
virtualenv pyml-book            # 在当前目录创建环境文件夹
    ↓
source pyml-book/bin/activate   # 激活(Windows 下是 pyml-book\Scripts\activate)
    ↓
pip install numpy==1.21.2 scipy==1.7.0 scikit-learn==1.0 matplotlib==3.4.3 pandas==1.3.2
```

注意一个语法差异：conda 用单个 `=`（`numpy=1.21.2`），pip 用双等号（`numpy==1.21.2`）。

#### 4.1.3 源码精读

ch01/README.md 给出的 conda 创建命令（建议先装 [Miniforge](https://github.com/conda-forge/miniforge)）：

[ch01/README.md:L34-L46](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch01/README.md#L34-L46) —— 这段是 conda 路线：`conda create -n "pyml-book"` 创建名为 pyml-book 的新环境，后面 `python=3.9 numpy=1.21.2 scipy=1.7.0 scikit-learn=1.0 matplotlib=3.4.3 pandas=1.3.2` 把解释器版本和 5 个核心包一次性钉死；`conda activate "pyml-book"` 则进入这个环境（命令行提示符前会出现环境名）。

[ch01/README.md:L50-L72](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch01/README.md#L50-L72) —— 这段是 pip + virtualenv 路线：`pip install virtualenv` 装工具，`virtualenv pyml-book` 在你选定的目录下创建环境文件夹，`source pyml-book/bin/activate` 激活，最后 `pip install numpy==1.21.2 ...` 安装与 conda 路线完全相同的版本组合。

[ch01/README.md:L102-L128](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch01/README.md#L102-L128) —— 说明仓库的 `.ipynb` 是 Jupyter notebook（代码、运行结果、图、文档合在一起），并给出两种安装 JupyterLab 的命令：`conda install -c conda-forge jupyterlab` 或 `pip install jupyterlab`。这为下一讲（u1-l3）的 notebook 工作流做准备。

一个值得注意的实用细节（属于经验提示，非仓库原文）：`numpy 1.21.2`、`scikit-learn 1.0` 这批包发布于 2021 年，官方只提供到 Python 3.10 左右的预编译包。如果你在 Python 3.11/3.12 上安装，pip 可能会尝试源码编译并失败。所以请尽量按书中指定的 `python=3.9` 来建环境。

#### 4.1.4 代码实践

1. **实践目标**：创建 `pyml-book` 环境并确认能激活。
2. **操作步骤**：
   - conda 用户：执行 [ch01/README.md:L39](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch01/README.md#L39) 中的 `conda create` 命令，然后 `conda activate pyml-book`。
   - pip 用户：按 [ch01/README.md:L62-L66](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch01/README.md#L62-L66) 用 virtualenv 创建并激活环境，再执行 [ch01/README.md:L71](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch01/README.md#L71) 的 `pip install` 命令。
   - 激活后运行 `python --version` 与 `conda env list`（或 `which python`）确认当前解释器属于 pyml-book 环境。
3. **需要观察的现象**：激活成功后终端提示符前出现 `(pyml-book)`；`python --version` 输出 3.9.x。
4. **预期结果**：环境可激活，`python --version` 显示 Python 3.9 系列。安装耗时取决于网络（conda 解析依赖可能需要几分钟），属正常现象。
5. 具体安装输出因机器而异，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么强烈不建议直接在系统的 base 环境（或全局 Python）里装 `numpy 1.21.2`？

**答案**：base 环境是全局共享的。本书要求一组 2021 年的旧版本包，直接装会覆盖系统里其他项目（甚至系统工具）依赖的新版本包，造成冲突。虚拟环境让这组旧版本只对本书生效，删除环境即可完全还原。

**练习 2**：conda 路线里 `numpy=1.21.2` 和 pip 路线里 `numpy==1.21.2` 写法为什么不同？

**答案**：这是两个工具各自的命令行语法约定——conda 用单 `=` 指定包版本，pip 用 `==` 表示精确版本。语义相同：都要求安装精确的 1.21.2 版本。

**练习 3**：如果想确认当前终端里到底激活的是哪个环境，可以用什么命令？

**答案**：conda 用户用 `conda env list`（当前环境带 `*` 标记）；通用方法是 `which python`（Windows 下 `where python`）查看当前 `python` 命令的实际路径，路径中含 `pyml-book` 即说明在正确环境里。

### 4.2 environment.yml 依赖清单与 Makefile

#### 4.2.1 概念说明

`environment.yml` 是 conda 的"环境配方文件"：把环境名、下载频道、依赖及版本写在一个 YAML 文件里，任何人拿到它都能用一条命令复现出一模一样的环境。这比把一长串包名贴在 README 里更可靠，也是科研与工程界做"环境可复现"的标准做法。

`Makefile` 则是对 conda 命令的封装。ch01/README.md 里给老 conda 用户的建议就是一句 `make -f Makefile`（在仓库根目录执行），它会执行 Makefile 中的第一个目标 `install`。

#### 4.2.2 核心流程

`make -f Makefile` 的执行路径：

```text
make -f Makefile            # 未指定目标名 → 执行第一个目标 install
    ↓
conda config --append channels conda-forge     # 把 conda-forge 追加到频道列表
    ↓
conda env update -n pyml-book \
      --file environment.yml --prune           # 按 yml 创建/更新名为 pyml-book 的环境
```

`make create` 的执行路径（用于彻底重建）：

```text
make create
    ↓
conda env create environment.yml --force       # 按 yml 强制新建环境
                                             # 环境名取自 yml: machine-learning-book
```

注意两个目标产出的环境名不同：`install` 用 `-n pyml-book` 覆盖了 yml 里的名字，产出 **pyml-book**；`create` 直接采用 yml 第 1 行的 `name: machine-learning-book`，产出 **machine-learning-book**。

#### 4.2.3 源码精读

[environment.yml:L1-L13](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/environment.yml#L1-L13) —— 完整的依赖清单：第 1 行 `name: machine-learning-book` 声明环境名；第 2-4 行声明从 `conda-forge` 和 `defaults` 两个频道找包；第 5-12 行是依赖列表——`python=3.9`、`numpy=1.21.2`、`pandas=1.3.2`、`scipy=1.7.0`、`matplotlib=3.4.3`、`scikit-learn=1.0` 六项精确钉死版本，而 `jupyterlab>=3.4.4` 只设下限（notebook 工具对代码结果影响小，允许更新版本）；第 13 行 `prefix` 只是导出此文件时的本机路径，可忽略。

[Makefile:L1-L4](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/Makefile#L1-L4) —— `install` 目标：先 `conda config --append channels conda-forge` 确保 conda-forge 频道可用，再 `conda env update -n pyml-book --file environment.yml --prune` 按 yml 创建（不存在时）或更新（已存在时）名为 pyml-book 的环境；`--prune` 表示顺带删掉环境中不在 yml 清单里的旧依赖。注释标明该写法在 conda 4.13.0 上验证过。

[Makefile:L5-L8](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/Makefile#L5-L8) —— `create` 目标：`conda env create environment.yml --force` 强制按配方重建环境。注释解释了为什么还需要它：conda 4.4 之后 `--prune` 行为损坏（注释中链接了 conda 官方 issue #7279），依赖删不干净时只能推倒重来。

[ch01/README.md:L11-L22](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch01/README.md#L11-L22) —— 面向已有 conda 用户的快捷通道：在仓库主目录执行 `make -f Makefile` 即可获得一个包含全部推荐版本、名为 "pyml-book" 的新环境。

顺带一提 Makefile 的语法细节：每个目标下的命令行必须以 **Tab**（不是空格）开头，这是 make 的硬性规定，也是初学者手改 Makefile 时最常见的报错来源（`Makefile:5: *** missing separator`）。

#### 4.2.4 代码实践

1. **实践目标**：用 `make` 一键创建环境，并观察它等价于哪两条 conda 命令。
2. **操作步骤**：
   - 在仓库根目录执行 `make -f Makefile`（需已安装 conda）。
   - 分别执行 `make -n -f Makefile` 和 `make -n create -f Makefile`（`-n` 表示 dry-run：只打印将执行的命令、不真正执行）。
   - 执行 `conda env list`，找到新建的环境名。
3. **需要观察的现象**：`-n` 模式打印出的命令应与 Makefile 中 `install` 目标的两行完全一致；环境列表里出现 `pyml-book`。
4. **预期结果**：环境创建成功且名为 `pyml-book`（而不是 yml 里的 machine-learning-book）；随后 `conda activate pyml-book` 即可使用。
5. 如果你没有安装 make 或 conda，此实践无法执行，可改走 4.1 的手动路线；实际安装输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`environment.yml` 中 `jupyterlab>=3.4.4` 与 `numpy=1.21.2` 的版本约束有何不同？为什么对 jupyterlab 可以宽松？

**答案**：`=`（配合 conda 语义）钉死精确版本，`>=` 只要求不低于该版本。numpy 等计算库的版本直接影响数组行为与代码兼容性，必须精确；jupyterlab 只是运行 notebook 的界面工具，装新一点不影响书中的计算结果，所以只设下限。

**练习 2**：`make install`（即默认目标）与 `make create` 最终创建的环境名分别是什么？为什么不同？

**答案**：`install` 创建 `pyml-book`，因为 `conda env update` 显式给了 `-n pyml-book`，命令行参数优先于 yml 文件中的 `name`；`create` 创建 `machine-learning-book`，因为 `conda env create` 没有给 `-n`，直接采用 yml 第 1 行的名字。用哪个目标都可以，但后续 `conda activate` 时要激活对应的名字。

**练习 3**：如果 `make install` 之后想彻底删掉环境重来，应该执行什么？

**答案**：先 `conda env remove -n pyml-book` 删除环境，再执行 `make -f Makefile`（或 `conda env update`）重建；也可以直接 `make create` 用 `--force` 强制重建（但注意它产出的环境名是 machine-learning-book）。

### 4.3 版本检查脚本 python_environment_check.py

#### 4.3.1 概念说明

环境装好后如何确认"版本都对"？逐个打开 Python 去 `import xxx; print(xxx.__version__)` 太繁琐。仓库根目录提供了 [python_environment_check.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/python_environment_check.py)，一个 57 行的自检脚本，它做三件事：

1. 检查 Python 解释器版本不低于 3.8；
2. 逐个尝试 import 五个核心包（numpy、scipy、matplotlib、sklearn、pandas），读取它们自报的版本号；
3. 把实际版本与"建议最低版本"比较，逐项打印 `[OK]` 或 `[FAIL]`。

这个脚本的地位比看起来重要：它不只装完环境跑一次，而是被**每一章代码开头复用**——后面每一章的 `.py`/`.ipynb` 第一个代码单元格都是导入它的 `check_packages` 函数做版本自检（第 5 讲会再遇到它）。可以说"环境检查"是本书所有代码的第一道门。

#### 4.3.2 核心流程

脚本的执行流程：

```text
python python_environment_check.py
    ↓
① 比较解释器版本: sys.version < 3.8 ?
       是 → [FAIL] 建议升级;  否 → [OK] 打印当前版本
    ↓
② get_packages(包名列表):
       对每个包 p:
           __import__(p) 动态导入
           成功 → 依序尝试 imported.__version__ / .version / .version_info
                  (三级回退取版本号)
           失败 → 打印 [FAIL]: p is not installed... 并记 'N/A'
    ↓
③ check_packages(字典 {包名: 建议版本}):
       对每个包:
           实际版本 == 'N/A' → 跳过(②里已报错)
           特判: matplotlib 且版本恰为 3.8 → [FAIL] 提示装 >3.8 的新版
           实际 < 建议 → [FAIL] 提示 upgrade to >= 建议版本
           其余(≥建议) → [OK]
```

版本比较用的是 `distutils.version.LooseVersion`——它按"点分数字逐段比较"的方式比较版本字符串，因此 `1.21.2 > 1.21.10` 这类字符串陷阱（字符串比较会认为 "10" < "2"）能被正确处理。

#### 4.3.3 源码精读

[python_environment_check.py:L1-L8](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/python_environment_check.py#L1-L8) —— 脚本入口的第一步：用 `sys.version` 拿到解释器版本字符串，与 `'3.8'` 做 `LooseVersion` 比较，低于 3.8 打 `[FAIL]`，否则打印 `[OK]` 与完整版本信息。`sys.version` 同时包含编译器等细节，所以示例输出里会看到 `[Clang 11.1.0]` 之类的第二行。

[python_environment_check.py:L11-L29](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/python_environment_check.py#L11-L29) —— `get_packages` 函数：核心是 `__import__(p)` 按字符串动态导入包（等价于写了 `import numpy`，但包名可以放在变量里循环处理）。导入成功后按 `__version__` → `version` → `version_info` 三级回退读版本号（不同包暴露版本号的属性名不统一，所以逐级 try/except，全失败则记 `'0.0'`）；导入抛 `ImportError` 说明包没装，打印 `[FAIL]` 并记 `'N/A'`。

[python_environment_check.py:L32-L45](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/python_environment_check.py#L32-L45) —— `check_packages` 函数：先调 `get_packages` 拿全部实际版本，再用 `zip(d.items(), versions)` 把"（包名, 建议版本）"与实际版本配对逐一判断。规则是：`N/A` 跳过（前面已报错）；matplotlib 恰为 3.8 时单独提示升级（该版本与书中代码不兼容）；实际低于建议则 `[FAIL]`；**大于等于建议版本都算 `[OK]`**——即脚本表达的是"最低版本要求"，装了更新的版本并不会被判失败。

[python_environment_check.py:L49-L57](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/python_environment_check.py#L49-L57) —— `__main__` 入口：定义字典 `d`，把五个核心包与建议版本一一对应（`'sklearn'` 对应导入名而非 pip 包名 `scikit-learn`），然后调用 `check_packages(d)`。这也是各章代码复用它的方式——只是各章传入的字典更小。

[ch02/ch02.py:L4-L5](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L4-L5) 与 [ch02/ch02.py:L21-L35](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L21-L35) —— 第 2 章脚本的开头：`from python_environment_check import check_packages` 导入根目录的检查函数；因为 ch02 脚本在子目录里运行，需要 `sys.path.insert(0, '..')` 把仓库根目录临时加进模块搜索路径才能找到它；随后构造只含 3 个包的小字典并调用 `check_packages(d)`。这印证了"每章开头先自检环境"是贯穿全书的惯例。

一个重要的现代环境提醒（经验知识，非仓库原文）：脚本第 2 行 `from distutils.version import LooseVersion` 依赖的 `distutils` 模块在 Python 3.12 中已被标准库移除（PEP 632）。用 Python 3.12+ 运行这个脚本会直接 `ModuleNotFoundError`。只要按 `environment.yml` 用 Python 3.9 建环境就不会踩到这个坑；若你用自己的新版 Python 运行检查脚本而报此错，应意识到这是解释器版本问题，而不是书的问题。

ch01/README.md 还展示了脚本的真实输出样例（作者机器上的记录），可作为比对自己结果的参照：

[ch01/README.md:L84-L99](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch01/README.md#L84-L99) —— 运行方式是 `python python_environment_check.py`，示例输出依次为：Python 3.9.6 `[OK]`、numpy 1.21.2 `[OK]`、scipy 1.7.0 `[OK]`、matplotlib 3.4.3 `[OK]`、sklearn 1.0 `[OK]`、pandas 1.3.2 `[OK]`。

#### 4.3.4 代码实践

1. **实践目标**：运行环境检查脚本，记录每个包的实际版本，确认全部 `[OK]`。
2. **操作步骤**：
   - 激活 4.1/4.2 中创建的环境（`conda activate pyml-book` 或 `source pyml-book/bin/activate`）。
   - 在仓库根目录执行：

     ```bash
     python python_environment_check.py
     ```

   - 把输出逐行抄录到自己的笔记里，格式如"numpy 1.21.2 → [OK]"。
   - 对照 [ch01/README.md:L90-L99](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch01/README.md#L90-L99) 的示例输出，逐项核对。
3. **需要观察的现象**：第一行是 Python 版本；随后五行按 numpy → scipy → matplotlib → sklearn → pandas 顺序输出，每行以 `[OK]` 或 `[FAIL]` 开头。
4. **预期结果**：在按本书版本安装的 pyml-book 环境中，六项应全部 `[OK]`（Python ≥ 3.8，其余包版本 ≥ 建议值）。若某行出现 `[FAIL]`，按提示 `pip install 包名>=建议版本` 或重装对应精确版本即可。没有 GPU 不影响本检查——脚本只查这 5 个 CPU 包，PyTorch 相关环境留到第 12 章再装。
5. 各机器输出细节（Python 小版本号、编译器行）会有差异，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`check_packages` 的判断规则是 `actual < suggested → FAIL`，那么装了 `numpy 1.24`（高于建议的 1.21.2）会输出什么？这个设计传达了什么意图？

**答案**：会输出 `[OK] numpy 1.24`（除非另有特判）。脚本表达的是"最低可用版本"而非"精确锁定"——它只保证代码所需的基本 API 存在，不阻止你用更新的版本。真正钉死版本的是 `environment.yml` 和安装命令。

**练习 2**：`get_packages` 里为什么要写 `__version__` → `version` → `version_info` 的三级 try/except 回退？

**答案**：Python 包暴露版本号的属性名没有统一标准：多数用 `__version__`，少数旧包用 `version` 模块级变量，还有些只提供 `version_info` 元组。脚本为了对各种包都能取到版本号，逐级尝试；全部失败时退而记 `'0.0'`，保证脚本不会因某个包的属性缺失而崩溃。

**练习 3**：如果不装 scikit-learn，运行检查脚本会看到什么？程序会崩溃吗？

**答案**：不会崩溃。`get_packages` 中 `__import__('sklearn')` 抛出 `ImportError`，被 L26-L28 捕获后打印 `[FAIL]: sklearn is not installed and/or cannot be imported.` 并记版本为 `'N/A'`；`check_packages` 遇到 `N/A` 直接 `continue`，继续检查剩余包。这是"逐项容错、给出完整体检报告"的设计。

## 5. 综合实践

**任务：从零走完"环境搭建 → make 复现 → 自检 → 记录报告"全流程。**

1. **准备**：任选一条路线创建环境——
   - 路线 A（conda 手动）：按 [ch01/README.md:L39](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch01/README.md#L39) 创建 `pyml-book`；
   - 路线 B（make 一键）：在仓库根目录执行 `make -f Makefile`（见 [ch01/README.md:L18-L20](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch01/README.md#L18-L20) 与 [Makefile:L1-L4](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/Makefile#L1-L4)）；
   - 路线 C（pip 用户）：按 [ch01/README.md:L62-L71](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch01/README.md#L62-L71)。
2. **自检**：激活环境后，在仓库根目录运行 `python python_environment_check.py`，抄录完整输出。
3. **交叉验证**：打开 [ch02/ch02.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L30-L35)，把 L30-L34 的字典内容改成你想额外确认的包（示例代码，例如加上 `'sklearn': '1.0'`），在环境里用 `python -c "from python_environment_check import check_packages; ..."` 的方式调用一次（或者干脆直接跑原脚本即可）。
4. **整理报告**：用一张表记录"包名 | 建议版本 | 我的实际版本 | OK/FAIL"，并注明你所选的路线（A/B/C）和 Python 版本。
5. **备选方案（无 GPU / 不装环境）**：阅读根 [README.md:L29](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/README.md#L29) 提到的 [supplementary/running-on-colab.pdf](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/supplementary/running-on-colab.pdf) 指南，在 Google Colab 中打开仓库任一 notebook（如 `ch02/ch02.ipynb`）验证可运行。Colab 免装环境、自带 GPU，但包版本由 Colab 决定，不保证与书中版本一致——这也正好印证了本讲的中心思想：**可复现的环境是学习本书代码的第一块地基**。

## 6. 本讲小结

- 本书代码依赖一批 2021 年前后的固定版本包（Python 3.9、numpy 1.21.2、scikit-learn 1.0 等），必须用独立的虚拟环境隔离，官方支持 conda 与 pip+virtualenv 两条路线，环境惯例名为 `pyml-book`。
- `environment.yml` 是 conda 的环境配方文件（环境名 + conda-forge 频道 + 7 项依赖），`make -f Makefile` 通过 `install` 目标把它一键落地为 `pyml-book` 环境；`make create` 则按 yml 原名 `machine-learning-book` 强制重建。
- `python_environment_check.py` 用动态 import 与 `LooseVersion` 实现"Python ≥ 3.8 + 5 个包 ≥ 建议版本"的最低版本体检，逐行输出 `[OK]/[FAIL]`；各章代码开头都会导入它的 `check_packages` 复用该检查。
- 检查脚本依赖的 `distutils` 在 Python 3.12 中已被移除——按 yml 用 Python 3.9 建环境即可避开；遇到该报错时先怀疑解释器版本，而不是书中的代码。
- 没有 GPU 或不想装环境时，可用仓库自带的 Google Colab 指南（supplementary/running-on-colab.pdf）直接运行各章 notebook。

## 7. 下一步学习建议

环境就绪后，下一讲（u1-l3「Notebook 与脚本双轨工作流」）将正式打开第一份代码：用 Jupyter 打开 `ch02/ch02.ipynb`，跑通"读入鸢尾花数据 → 可视化"的完整流程，并了解仓库如何用 `.convert_notebook_to_script.py` 和 `update_python_from_notebook.sh` 维护 `.ipynb` 与 `.py` 的同步。如果你本讲选择了 Colab 路线，建议提前在 Colab 中挂载本仓库，下一讲可以直接接着用。

# scipy.misc 的历史职能与退役原因

## 1. 本讲目标

前两讲我们只盯着「现状」：`scipy.misc` 现在是三个只会喊弃用的桩文件（见 [u1-l1](u1-l1-project-overview-and-directory.md)），导入即触发 `DeprecationWarning`（见 [u1-l2](u1-l2-running-and-observing-warnings.md)）。但你一定会问：**它以前到底是干什么的？那些 `face`、`ascent`、`derivative` 之类的名字都去哪了？** 本讲就来回答这个问题。

学完本讲后，你应当能够：

- 说出 `scipy.misc` **历史上承担的四类职能**：示例数据集、数值工具、通用工具、文档辅助。
- 用 `git show <commit>:<path>` **亲手还原被移除的源码**，并整理出一张「名字 → 当前去向」的映射表。
- 理解什么叫 **「杂物箱（catch-all）模块」**，以及 SciPy 为什么要把这样一个模块拆掉。
- 知道这四类内容各自的最终归宿：迁往 `scipy.datasets`、被完全移除、或（连同测试）迁往 `scipy/_lib`。

> 本讲几乎不涉及数学，只把版本号当作一条「从弃用到移除」的时间轴来看（`1.10.0 → 2.0.0`）。

## 2. 前置知识

进入源码考古之前，先建立四个直觉。前两个承接 [u1-l1](u1-l1-project-overview-and-directory.md)，这里只做一句话回顾。

### 2.1 回顾：桩文件与弃用

`scipy/misc/` 下现在的三个 `.py` 文件都是**桩文件（stub）**：它们不含任何函数或类，唯一作用是保留旧的 `import` 路径，并在被导入时发出 `DeprecationWarning`，给使用者一个迁移缓冲期。详情见 [u1-l1](u1-l1-project-overview-and-directory.md)。

### 2.2 回顾：导入即警告

`import scipy.misc` 时会执行 `scipy/misc/__init__.py` 的顶层语句，从而触发那条 `warnings.warn(..., DeprecationWarning, stacklevel=2)`。详情见 [u1-l2](u1-l2-running-and-observing-warnings.md)。

### 2.3 新工具：用 `git show <commit>:<path>` 读取历史版本的文件

本讲的核心实践是「源码考古」。Git 不仅能看现在的代码，还能看**任意历史版本**的代码。语法是：

```bash
git show <提交号>:<文件路径>
```

它的含义是：「把某个提交里、某个文件的内容打印出来」。有两个常用变体：

- `git show <提交号>:scipy/misc/__init__.py` —— 打印该提交时这个文件的内容。
- `git show <提交号>^:scipy/misc/__init__.py` —— 注意那个 `^`，它表示「**这个提交的父提交**」，也就是它**上一步**的状态。本讲会频繁用 `^` 来取「移除之前」的版本。

> 为什么用 `^`？因为我们要看的是「东西还在」的那一版，而不是「东西刚被删」的那一版。`<移除提交>^` 正好指向删除前的最后一次完好状态。

### 2.4 新概念：什么是「杂物箱（catch-all）模块」

一个软件库里，每个模块 ideally 应该有**单一、清晰的职责**——比如 `scipy.fft` 只管傅里叶变换，`scipy.stats` 只管统计。但实际演进中，往往会冒出一个「**什么不太重要、不好归类的东西都往里塞**」的模块，它没有一个统一的主题，只是一个大杂烩。这种模块在英文里叫 **catch-all**（杂物箱、兜底模块）。`scipy.misc` 的 `misc` 正是 *miscellaneous*（杂项）的缩写——名字本身就暴露了它的「杂物箱」本质。

杂物箱模块用着方便（写代码时随手 `from scipy.misc import xxx`），但长期看是大问题：职责不清、依赖膨胀、难以单独测试。本讲后半段会讲 SciPy 为什么痛下决心拆掉它。

## 3. 本讲源码地图

本讲会穿梭在「现在」和「过去」两个时间点，涉及以下文件：

| 文件 | 时间点 | 作用 |
| --- | --- | --- |
| `scipy/misc/__init__.py` | 现在 / 过去 | 现在是 6 行的弃用桩；过去则是这个杂物箱的「总入口」，挂着四类职能。 |
| `scipy/misc/_common.py` | **过去**（已被删除） | 过去放数值工具 `derivative`、`central_diff_weights` 的私有实现文件。用 `git show` 还原。 |
| `scipy/datasets/__init__.py` | 现在 | 示例数据集的**新家**：`face`/`ascent`/`electrocardiogram` 迁到了这里。 |
| `scipy/datasets/_fetchers.py` | 现在 | 数据集的实现：用 `pooch` 按需下载、SHA256 校验、本地缓存。 |
| `scipy/_lib/doccer.py` | 现在 | 文档辅助模块 `doccer` 的**新家**（从 `scipy/misc/doccer.py` 迁来）。 |

> 关键方法论：我们用**现在的源码**来证明「东西搬到了哪里」，用**`git show` 还原的旧源码**来证明「东西以前长什么样」。两者对照，迁移路径就一目了然。

## 4. 核心概念与源码讲解

本讲围绕 `__init__.py`（以及它背后那个曾经的杂物箱）展开，拆成三个模块：先总览四类历史职能与去向（4.1），再用 `git show` 还原被删除的源码（4.2），最后讲清拆分这个杂物箱的动机（4.3）。

### 4.1 scipy.misc 曾经是什么：四类历史职能

#### 4.1.1 概念说明

在它还是「功能模块」的年代，`scipy.misc` 一共承担了**四类**互不相干的职能。把这四类摆在一起，你立刻就能感受到「杂物箱」的味道——它们之间几乎没有内在联系：

1. **示例数据集**：`ascent`（一张 512×512 灰度图）、`face`（一张 1024×768 浣熊脸彩图）、`electrocardiogram`（一段心电图信号）。它们纯粹是给教程、示例、测试用的「样例图片/信号」。
2. **数值工具**：`derivative`（数值求导）、`central_diff_weights`（中心差分权重系数）。这是两个数值计算小工具。
3. **通用工具 `common`**：一个历史悠久的子模块，里面曾经有过 `factorial`（阶乘）、`comb`（组合数）等「不知道该放哪」的通用函数。
4. **文档辅助 `doccer`**：一个帮开发者往函数 docstring 里批量插入「通用参数说明片段」的工具子模块。

把这四类放一起看：样例图、求导公式、阶乘函数、docstring 工具——它们唯一的共同点就是「不知道该放哪个正经模块」。这正是 catch-all 模块的典型特征。

#### 4.1.2 核心流程：四类职能的「去向」决策

退役不是一删了之，而是给每类内容**安排一个更合适的归宿**。SciPy 的决策大致是这样分流：

```text
scipy.misc 里的四类内容
        │
        ├── 示例数据集 (ascent/face/electrocardiogram)
        │       └── 迁往新建的 scipy.datasets（按需下载，不再打进安装包）
        │
        ├── 数值工具 (derivative/central_diff_weights)
        │       └── 彻底移除（无内置替代，改用外部库或手写有限差分）
        │
        ├── 通用工具 common
        │       └── 早就只剩残余；factorial/comb 很早迁往 scipy.special；
        │           剩下的 common 名字最终被完全移除
        │
        └── 文档辅助 doccer
                └── 迁往私有目录 scipy/_lib/doccer.py（连同其测试）
```

把这张图浓缩成一张「名字 → 去向」表，就是本讲最核心的产出：

| 历史名字 | 类别 | 当前去向 |
| --- | --- | --- |
| `face` | 示例数据集 | `scipy.datasets.face` |
| `ascent` | 示例数据集 | `scipy.datasets.ascent` |
| `electrocardiogram` | 示例数据集 | `scipy.datasets.electrocardiogram` |
| `derivative` | 数值工具 | **已移除**（无内置替代） |
| `central_diff_weights` | 数值工具 | **已移除**（无内置替代） |
| `common`（子模块） | 通用工具 | **已移除**（`factorial`/`comb` 早年已迁往 `scipy.special`） |
| `doccer`（子模块） | 文档辅助 | `scipy._lib.doccer`（私有） |

> 注意 `scipy._lib` 这个前缀：带下划线的 `_lib` 是 SciPy 的**私有**内部库，不保证对外稳定。`doccer` 本来就是给 SciPy 自己开发者用的工具，迁到私有目录正合适，不必再占着公开的 `scipy.misc` 名额。

#### 4.1.3 源码精读

我们用**现在的源码**来证明表里的「去向」是真的。先看数据集的新家 `scipy/datasets/__init__.py`——它公开导出的正是那三个搬过来的名字：

[`scipy/datasets/__init__.py:80-85`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/__init__.py#L80-L85) —— 数据集新家的公开接口，三个名字赫然在列：

```python
from ._fetchers import face, ascent, electrocardiogram
from ._download_all import download_all
from ._utils import clear_cache

__all__ = ['ascent', 'electrocardiogram', 'face',
           'download_all', 'clear_cache']
```

这一行 `from ._fetchers import face, ascent, electrocardiogram` 就是迁移的「铁证」：`face`/`ascent`/`electrocardiogram` 现在从 `scipy.datasets` 对外提供。

再看 `doccer` 的新家 `scipy/_lib/doccer.py`——它确实是一个**有实际函数**的模块（而不是空桩）：

[`scipy/_lib/doccer.py:1-18`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/doccer.py#L1-L18) —— 文档辅助工具的新家，定义了一批真实可用的函数：

```python
"""Utilities to allow inserting docstring fragments for common
parameters into function and method docstrings."""
...
__all__ = [
    "docformat",
    "inherit_docstring_from",
    "indentcount_lines",
    "filldoc",
    "unindent_dict",
    "unindent_string",
    "extend_notes_in_docstring",
    "replace_notes_in_docstring",
    "doc_replace",
]
```

对比一下 [`scipy/misc/doccer.py:1-6`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/doccer.py#L1-L6)——它只剩 6 行、一句警告、零个函数。这就构成了清晰的对照：**真身已搬到 `scipy/_lib/doccer.py`，原地只留下一个喊「我弃用了」的桩**。`doccer` 的测试也跟着搬到了 `scipy/_lib/tests/test_doccer.py`。

> 至于 `derivative`/`central_diff_weights` 的「去向」是「**已移除**」——这种「没有新家」的去向没法用一行 `import` 来证明。最可靠的证据就是用 `git show` 还原旧实现、再在现在的代码里全局搜索确认它已不存在。这正是 4.2 节要做的考古实践。

#### 4.1.4 代码实践

**实践目标**：用现在的 Python 环境验证「数据集确实在 `scipy.datasets`」，并确认 `derivative` 在 `scipy.misc` 里已经找不到。

**操作步骤**：

1. 在已安装好该版本 scipy 的环境里运行（首次调用 `face()` 会联网下载数据，需联网；若无网络可只做第 2 步的属性检查）：
   ```python
   import scipy.datasets
   print(hasattr(scipy.datasets, "face"))          # 预期 True
   print(hasattr(scipy.datasets, "ascent"))        # 预期 True
   print(hasattr(scipy.datasets, "electrocardiogram"))  # 预期 True
   ```
2. 再确认这些名字在 `scipy.misc` 里已经**不存在**（注意：`import scipy.misc` 会触发 `DeprecationWarning`，这是正常的）：
   ```python
   import warnings
   with warnings.catch_warnings():
       warnings.simplefilter("ignore")          # 临时屏蔽弃用警告，便于观察属性
       import scipy.misc
       print(hasattr(scipy.misc, "face"))        # 预期 False
       print(hasattr(scipy.misc, "derivative"))  # 预期 False
   ```

**需要观察的现象**：`scipy.datasets` 三个名字都返回 `True`；`scipy.misc` 里这几个名字都返回 `False`。

**预期结果**：这从运行时印证了映射表——数据集在 `scipy.datasets`，「数值工具」在 `scipy.misc` 已无影无踪。

> 待本地验证：第 1 步中 `face()` 真正下载数据需要网络与可选依赖 `pooch`；若仅检查 `hasattr` 则不需要联网。关于 `pooch` 的下载机制，会在 4.3.3 与 [u3-l1](u3-l1-datasets-migration.md) 详讲。

#### 4.1.5 小练习与答案

**练习 1**：`scipy.misc` 历史上的四类职能里，哪一类「最不像」其余三类、最不该和它们待在同一个模块？

**参考答案**：**示例数据集**（样例图片/信号）和其余三类差别最大。`derivative`、`common`、`doccer` 至少都是「代码工具」，而数据集本质是「**数据文件**」，它们的获取、缓存、校验机制（联网下载、SHA256、本地缓存）和纯代码工具完全不同。把它们塞进同一个 `misc`，正是职责不清的体现。

**练习 2**：为什么 `doccer` 迁到的是 `scipy._lib/doccer.py`（带下划线），而不是某个公开模块？

**参考答案**：`doccer` 是给 SciPy **自己开发者**用的「docstring 片段拼接工具」，不是面向最终用户的科学计算 API。放在私有的 `scipy/_lib/` 下，表明它是内部实现细节、不保证对外稳定，避免它继续占用公开命名空间。

**练习 3**：`derivative` 和 `central_diff_weights` 的「去向」写的是「已移除」，而不是「迁往某处」。这意味着什么？

**参考答案**：意味着 SciPy **没有为它们提供内置替代**。如果你的旧代码用了 `scipy.misc.derivative`，迁移时不能简单地换个 `import`，而要改用外部库（如 `numdifftools`、`findiff`）或自己手写有限差分。这种「无替代移除」是弃用里对用户冲击最大的一种。

### 4.2 用 git 历史还原被移除的源码

#### 4.2.1 概念说明

上一节我们用「现在的源码」证明了东西搬到了哪里。但「东西以前长什么样」在当前仓库里已经**看不到了**——因为它们已经被删掉。要看到被删除的内容，唯一的办法是翻 Git 历史。

Git 把每一次提交都当作一个完整快照保存下来。即便一个文件后来被删除，它在历史提交里的版本依然可以被取回。`git show <commit>:<path>` 就是取回历史文件内容的命令（语法见 2.3 节）。

本节要还原的关键历史提交是 `43fc97efa8`——这是「移除 scipy.misc 大部分内容」的那次提交（对应 PR #21864）。我们用 `43fc97efa8^`（它的父提交，即「移除之前」的状态）来取回还完好时的源码。

#### 4.2.2 核心流程：考古的标准三步

```text
1. 定位「移除提交」：43fc97efa8（PR #21864，移除 scipy.misc 大部分内容）
2. 取「移除之前」的版本：在该提交号后加 ^，得到 43fc97efa8^
3. 用 git show <commit>:<path> 打印旧文件内容：
     git show 43fc97efa8^:scipy/misc/__init__.py     ← 旧总入口
     git show 43fc97efa8^:scipy/misc/_common.py      ← 数值工具的私有实现
```

> `^` 是 Git 的「父提交」运算符。`A^` 读作「A 的父亲」，也就是 A 之前的那一步。所以 `43fc97efa8^` 是「移除动作发生前的最后一次完好状态」——我们要的正是这个状态。

#### 4.2.3 源码精读（历史版本，行号以 git show 输出为准）

由于这些文件在当前 HEAD 已经被删除，**下面是它们在历史版本中的概念性描述**，精确行号请以你本地 `git show` 的实际输出为准（故不附固定行号的永久链接，避免编造）。

**旧 `scipy/misc/__init__.py`（`43fc97efa8^` 版本）**——它远不像现在只剩一句警告。概念上它包含：

- 一段说明「`scipy.misc` 已弃用、将在 2.0.0 移除」的模块 docstring。
- 一个**模块级 `__getattr__`**（PEP 562，Python 3.7+ 特性）：当用户访问 `scipy.misc.face` 这类「历史名字」时，Python 会拦截这次访问并交给 `__getattr__` 处理。`__getattr__` 根据被访问的名字分流——比如数据集名字转告「请用 `scipy.datasets`」，数值工具名字给出对应的弃用提示。
- 一个模块级 `__dir__`，让 `dir(scipy.misc)` 仍能列出这些历史名字（便于旧代码与补全工具发现它们）。

> 这种「用 `__getattr__` 按名字拦截访问、给出不同弃用提示」的机制，是 scipy.misc 弃用期的关键设计。它的逐行精读（含 `dataset_methods` 名单、分流逻辑）留给 [u3-l2](u3-l2-pep562-getattr-evolution.md)；本讲你只需知道「旧入口里曾用这种方式保留了所有历史名字的访问入口」。

**旧 `scipy/misc/_common.py`（`43fc97efa8^` 版本）**——这是数值工具的**私有实现文件**（注意文件名前的下划线，表示私有）。它真正定义了两个函数：

- `derivative(func, x0, x0 + h, ...)`: 用有限差分对 `func` 在 `x0` 处求数值导数。
- `central_diff_weights(Np, ndiv=1)`: 计算 `Np` 点中心差分的权重系数。

概念上 `derivative` 的核心是：先调用 `central_diff_weights` 算出一组差分权重 \(w_k\)，再把它们与函数在若干采样点处的取值做加权求和：

\[
f'(x_0) \approx \frac{1}{h} \sum_{k} w_k \, f(x_0 + (k - \text{center})\,h)
\]

其中 \(h\) 是步长，\(w_k\) 是中心差分权重。这正是「数值微分」最朴素的有限差分公式。具体函数签名与默认参数请以 `git show 43fc97efa8^:scipy/misc/_common.py` 的输出为准。

> 为什么强调是 `_common.py`（带下划线）而不是 `common.py`？因为在弃用期，SciPy 把真正的实现藏进了下划线私有文件，公开的 `scipy/misc/common.py` 则退化为桩。这与我们在 [u1-l1](u1-l1-project-overview-and-directory.md) 看到的「`common.py` 现在只剩一句警告」是一脉相承的。

#### 4.2.4 代码实践（本讲核心实践）

**实践目标**：用 `git show` 亲手还原被移除的源码，并整理出一张完整的「名字 → 当前去向」映射表。

**操作步骤**：

1. 确保你在本仓库根目录、已切到本讲对应的 HEAD：
   ```bash
   git checkout de190e7fde9d3d34400dbfe1eeacc9fc6d29cede
   ```
2. 取回移除前的总入口，通读它，记下它处理了哪些历史名字：
   ```bash
   git show 43fc97efa8^:scipy/misc/__init__.py
   ```
3. 取回数值工具的私有实现，确认 `derivative`、`central_diff_weights` 曾定义在这里：
   ```bash
   git show 43fc97efa8^:scipy/misc/_common.py
   ```
4. 在**当前 HEAD** 里搜索这些名字，确认它们已经不存在（例如）：
   ```bash
   git grep -n "def derivative" -- 'scipy/misc'
   git grep -n "central_diff_weights" -- 'scipy/misc'
   ```
   预期：当前 HEAD 的 `scipy/misc` 下搜不到这些定义（桩文件里没有 `def`）。
5. 把你从第 2、3 步看到的历史名字，填进一张「名字 → 当前去向」表（可参考 4.1.2 的表，但请你用自己的 `git show` 输出来佐证每一行）。

**需要观察的现象**：

- 第 2 步的旧 `__init__.py` 里能看到对 `ascent`/`face`/`electrocardiogram`/`derivative`/`central_diff_weights` 等名字的处理（而不是现在的一句 `warnings.warn`）。
- 第 3 步的旧 `_common.py` 里能看到 `def derivative(...)` 和 `def central_diff_weights(...)` 两个函数定义。
- 第 4 步在当前 HEAD 搜不到这些定义，证明它们已被移除。

**预期结果**：你得到一张和 4.1.2 节一致的映射表，并且**每一行都能用 `git show` 或当前源码亲自验证**——而不是仅凭本讲的叙述。

> 待本地验证：旧文件的具体行数、函数签名的默认参数值，以你本地 `git show` 的实际输出为准；本讲不为其编造固定行号。

#### 4.2.5 小练习与答案

**练习 1**：`git show 43fc97efa8:scipy/misc/_common.py`（**不带** `^`）和 `git show 43fc97efa8^:scipy/misc/_common.py`（**带** `^`）输出会有什么不同？

**参考答案**：`43fc97efa8` 是「移除」那次提交本身。在那个提交里 `_common.py` 已经被删除，所以不带 `^` 的命令多半会报「path does not exist in this commit」之类的错误；带 `^` 取的是它的父提交（移除前），才能看到完整的函数定义。这正是用 `^` 的意义——取「还在」的那一版。

**练习 2**：为什么要到 `_common.py`（带下划线）里去找 `derivative`，而不是到 `common.py`？

**参考答案**：弃用期 SciPy 把数值工具的真正实现藏进了下划线前缀的私有文件 `_common.py`，而公开的 `common.py` 退化成了只发警告的桩（见 [u1-l1](u1-l1-project-overview-and-directory.md)）。所以要找「真身」就得看私有文件。

**练习 3**：用 `git show` 看历史文件，和直接在 GitHub 上点开某个旧版本的文件，本质上是同一回事吗？

**参考答案**：是的，本质上都是「读取某个历史提交里某个文件的内容」。GitHub 网页上把 commit 号填进 URL（如 `.../blob/<commit号>/scipy/misc/_common.py`）看到的，和本地 `git show <commit号>:scipy/misc/_common.py` 打印的，是同一份快照。区别只在入口：一个是网页 UI，一个是命令行。

### 4.3 为什么退役：拆分「杂物箱」的动机

#### 4.3.1 概念说明

知道「搬去了哪」之后，还要回答一个更深层的问题：**SciPy 为什么非要拆掉这个用了十几年的模块？** 答案就在「杂物箱」这三个字里。

catch-all 模块（见 2.4 节）有三个难以根治的 architectural 毛病：

1. **职责不清**：里面装的是「不知道该放哪」的东西，没有一个统一的主题，新人读代码时无从建立心智模型——「`scipy.misc.derivative` 是干嘛的？它和 `scipy.misc.face` 有什么关系？」答案是「没有关系」。
2. **依赖膨胀**：不同类别的依赖会被强行捆在一起。比如数据集要依赖 `pooch`、要打包几兆字节的 `.dat` 图片文件；而 `derivative` 只需要纯数学。把它们放一个模块，意味着只想用 `derivative` 的用户也不得不背上数据集的依赖和体积。
3. **难以独立演进**：当数据集需要引入「按需下载 + 缓存」这种全新机制时，塞在 `misc` 里很难大刀阔斧地改；单独拆出 `scipy.datasets` 后，才能放手设计 pooch/registry 体系。

退役 `scipy.misc`，本质上就是「**给每个被错放的东西，找到它真正属于的那个抽屉**」。

#### 4.3.2 核心流程：拆分的决策树

```text
对 misc 里的每样东西问：「它真正的主题是什么？」
        │
        ├── 是「数据文件」？ ── 是 ──→ 新建 scipy.datasets（数据归数据）
        │                              （按需下载，不随安装包分发）
        │
        ├── 是「SciPy 内部开发工具」？ ── 是 ──→ 迁往 scipy/_lib（私有）
        │
        ├── 是「早就该进专门模块的通用函数」？
        │       └── 早年的 factorial/comb ──→ scipy.special
        │
        └── 是「没有核心价值、外部早有更好实现」？
                └── derivative/central_diff_weights ──→ 直接移除
```

这张决策树的精神是：**让模块按「主题」而不是按「放不下」来组织**。拆完后，`scipy.datasets` 专管样例数据，`scipy.special` 专管特殊数学函数，`scipy._lib` 收纳内部工具，而「无主题的杂物」要么归位、要么淘汰。

#### 4.3.3 源码精读

我们用 `scipy.datasets` 的实现来印证「数据归数据」的合理性——它有一整套为「数据文件」量身打造的机制，这套机制放在旧的 `misc` 里是施展不开的：

[`scipy/datasets/_fetchers.py:14-26`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/_fetchers.py#L14-L26) —— 用 `pooch` 建立一个「带缓存目录、带哈希校验、带远程 URL」的数据获取器：

```python
    data_fetcher = pooch.create(  # type:ignore[union-attr]
        # Use the default cache folder for the operating system
        path=pooch.os_cache("scipy-data"),  # type:ignore[union-attr]
        base_url="https://github.com/scipy/",
        registry=registry,
        urls=registry_urls
    )
```

几个关键点：

- `path=pooch.os_cache("scipy-data")`：下载的数据放进**操作系统级缓存目录**（不是 scipy 安装目录）。这意味着——**图片不再随 scipy 安装包分发**，谁需要谁才下载。
- `registry=registry`：一份「文件名 → SHA256 哈希」的登记表，下载后用哈希校验完整性，防止文件损坏或被篡改。
- `base_url` / `urls`：数据文件托管在 GitHub 上的独立仓库（如 `scipy/dataset-face`）。

再看具体的数据集函数，比如 `face`：

[`scipy/datasets/_fetchers.py:184-225`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/_fetchers.py#L184-L225) —— `face()` 先按需下载 `face.dat`，再解压成数组返回：

```python
def face(gray=False):
    ...
    fname = fetch_data("face.dat")        # 按需下载（带缓存）
    with open(fname, 'rb') as f:
        rawdata = f.read()
    face_data = bz2.decompress(rawdata)   # 解压
    face = frombuffer(face_data, dtype='uint8').reshape((768, 1024, 3))
    ...
    return face
```

对比一下旧的 `scipy.misc.face`：它直接从**打进 scipy 安装包里**的本地 `.dat` 文件读取。新旧两种方式的差别一目了然：

| 维度 | 旧 `scipy.misc.face` | 新 `scipy.datasets.face` |
| --- | --- | --- |
| 数据存放 | 打进 scipy 安装包（本地 `.dat`） | 远程仓库，按需下载到系统缓存 |
| 安装体积 | 每个用户都背上这些图片 | 不下载就不占空间 |
| 完整性校验 | 无 | SHA256 哈希校验 |
| 缓存机制 | 无（每次从安装目录读） | `pooch.os_cache`，跨平台 |

这张表恰好印证了 4.3.1 的第 2 条动机：**数据集有自己独特的需求（缓存、校验、按需下载），把它和纯数值工具捆在 `misc` 里，反而限制了这套机制的引入**。拆出 `scipy.datasets` 后，这些设计才得以落地。

#### 4.3.4 代码实践

**实践目标**：把「为什么要拆」用自己的话讲清楚，写一段约 200 字的中文说明。

**操作步骤**：

1. 重读 4.1.2 的「名字 → 去向」表，注意每类内容被分到的「新抽屉」各不相同（`datasets` / `_lib` / `special` / 直接删除）。
2. 重读 4.3.3 的新旧 `face` 对比表，体会「数据文件」与「数值工具」需求的不同。
3. 写一段约 200 字的说明，回答：**SciPy 为什么要拆分并废弃 `scipy.misc` 这个「杂物箱」模块？** 至少覆盖以下三点中的两点：
   - 职责不清（catch-all 没有统一主题）；
   - 依赖/体积膨胀（数据集把 `.dat` 图片塞进安装包）；
   - 各类内容需要不同的归宿（数据要下载缓存、工具要进专门模块或私有库）。

**需要观察的现象**：写完后自查——你的说明里是否出现了「具体例子」（如 `face` 的体积、`derivative` 的无替代移除），而不是空泛的「为了更好」。

**预期结果**：一段有理有据、举了具体例子的 200 字说明，能说服一个没读过本讲的人「拆分是合理的」。

**参考范文（约 200 字，仅供参考，请用自己的话重写）**：

> `scipy.misc` 的 `misc`（miscellaneous，杂项）本就是个「杂物箱」——样例图片、求导工具、docstring 助手全堆在一起，彼此毫无主题关联。这带来三个问题：职责不清，新人无从建立心智模型；依赖与体积膨胀，旧版把 `face.dat`、`ascent.dat` 等几兆图片打进每个用户的安装包，只想用 `derivative` 的人也被迫背上；各类内容需求不同，数据集需要按需下载、SHA256 校验和系统缓存，纯数值工具则需要进入专门模块。于是 SciPy 按主题分流：数据集新建 `scipy.datasets`（用 pooch 按需下载），文档工具迁入私有 `scipy._lib/doccer`，`derivative` 等无核心价值者直接移除。拆分让每个抽屉各司其职。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `scipy.misc` 留着不拆，只是「往里面继续加新功能」，长期会出现什么问题？

**参考答案**：它会越长越大、越来越没有主题，最终变成一个「什么都有一点、什么都不专」的黑洞。新人难以理解，依赖关系越缠越乱，单独测试和演进都变得困难。这正是 catch-all 模块的典型衰败轨迹，也是 SciPy 决定拆掉它的根本原因。

**练习 2**：新的 `scipy.datasets.face` 用 `pooch.os_cache` 把数据放在系统缓存目录，而不是打进 scipy 安装包。对一个「只想用 `derivative` 求数值导数、根本不需要样例图片」的用户，这种拆分带来了什么直接好处？

**参考答案**：该用户安装 scipy 时不再被迫下载/存储那些样例图片，安装体积更小、安装更快；也不会因为缺少图片下载的网络/依赖（`pooch`）而影响数值计算功能。这就是「按主题拆分」让「依赖与体积」对号入座的好处。

**练习 3**：`derivative` 被直接移除而非迁往某模块，这和「杂物箱」问题有什么关系？

**参考答案**：`derivative` 本质是一个「没有核心价值、外部早有更好实现」的数值小工具——它既不属于某个明确的科学计算主题，SciPy 也不想再为它维护一个公开入口。与其给它硬找个抽屉（继续制造 catch-all），不如直接淘汰、让用户改用专门库（`numdifftools` 等）。这是「杂物箱清理」中最果断的一种处置。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「考古 + 复盘」任务。

**任务**：用 `git show` 还原 `scipy.misc` 的历史面貌，产出一份**完整的迁移档案**，并对这次拆分做一个整体评价。

**操作步骤**：

1. **考古**：依次运行下面两条命令，把移除前的源码取回，仔细阅读：
   ```bash
   git show 43fc97efa8^:scipy/misc/__init__.py
   git show 43fc97efa8^:scipy/misc/_common.py
   ```
2. **整理映射表**：基于第 1 步看到的所有历史名字，填一张「名字 → 类别 → 当前去向」三列表（类别分：示例数据集 / 数值工具 / 通用工具 / 文档辅助）。要求每一行的「去向」都能被验证——数据集去 `scipy.datasets`（见 [`scipy/datasets/__init__.py:80`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/__init__.py#L80-L80)）、`doccer` 去 `scipy._lib/doccer`（见 [`scipy/_lib/doccer.py:8-18`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/doccer.py#L8-L18)）、数值工具与 `common` 已移除（用 `git grep` 在当前 HEAD 下搜不到）。
3. **写动机说明**：写约 200 字，说明拆分这个「杂物箱」模块的动机（可参考 4.3.4 的范文，但用自己的话）。
4. **复盘**：用一句话回答——如果今天你要在一个新项目里避免重蹈 `scipy.misc` 的覆辙，最该坚守的一条原则是什么？

**预期结果**：一份包含「考古命令 → 映射表 → 200 字动机 → 一句话原则」的完整档案。它既能证明你读懂了 `scipy.misc` 的历史，也能指导你今后避免造出新的杂物箱模块。

> 建议原则参考：**「按主题组织模块，不要用 misc 给放不下的东西兜底。」**

## 6. 本讲小结

- `scipy.misc` 历史上承担**四类互不相干**的职能：示例数据集（`face`/`ascent`/`electrocardiogram`）、数值工具（`derivative`/`central_diff_weights`）、通用工具（`common`）、文档辅助（`doccer`）。
- 各类的最终去向不同：数据集迁往 `scipy.datasets`；`doccer` 迁往私有 `scipy/_lib/doccer.py`（连同测试）；`common` 的 `factorial`/`comb` 早年已迁往 `scipy.special`，残余被移除；`derivative`/`central_diff_weights` 被直接移除、无内置替代。
- 用 `git show <commit>:<path>`（尤其 `<移除提交>^` 取「移除之前」的版本）可以还原被删除的源码；移除 scipy.misc 大部分内容的提交是 `43fc97efa8`（PR #21864）。
- 旧 `__init__.py` 通过模块级 `__getattr__`（PEP 562）保留历史名字的访问入口并按名字给出弃用提示，旧 `_common.py`（带下划线的私有文件）才是 `derivative`/`central_diff_weights` 的真身。
- 退役的根本动机是 `scipy.misc` 是个**杂物箱（catch-all）模块**：职责不清、依赖与体积膨胀（旧版把图片打进安装包）、各类内容需要不同归宿；拆分让模块按主题各司其职。
- `scipy.datasets` 用 `pooch`（`os_cache` + SHA256 registry + 按需下载）取代了旧的「打包 `.dat`」方式，正是拆分后才能放手引入的机制。

## 7. 下一步学习建议

本讲让你看清了「过去」和「为什么退役」。接下来建议按以下顺序继续：

1. **`u2-l1` 三个桩文件的实现：warnings.warn 与 stacklevel**——从历史回到现实，逐行精读现在这三个桩文件的 `warnings.warn`，彻底弄懂 `message`/`category`/`stacklevel` 三个参数。
2. **`u2-l2` SciPy 的弃用约定与版本时间线**——读 `scipy/_lib/deprecation.py`，把本讲提到的「`1.10.0` 弃用 → PR #21864 移除 → `2.0.0` 完全移除」这条时间线套进 SciPy 的通用弃用政策里。
3. **`u3-l1` 数据集迁移路径：scipy.misc → scipy.datasets**——深入 `pooch`/registry/缓存机制，学会把旧 `from scipy.misc import face` 的代码实际迁过去。
4. **`u3-l2` 访问控制演进：模块级 `__getattr__` 与 PEP 562**——本讲只是点到为止的「旧 `__init__.py` 用 `__getattr__` 拦截访问」，将在这一讲得到逐行精读。

> 建议你把本讲产出的「名字 → 去向」映射表保存好——它是后续所有迁移讲义（`u3` 单元）的索引底图。

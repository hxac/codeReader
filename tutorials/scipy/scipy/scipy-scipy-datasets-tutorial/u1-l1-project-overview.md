# 项目定位与对外 API

## 1. 本讲目标

本讲是「scipy.datasets 学习手册」的第一篇。读完本讲，你应当能够：

- 说清楚 `scipy.datasets` 这个子模块在整个 SciPy 项目里扮演什么角色、解决什么问题。
- 记住它对外暴露的 **五个公开函数**，并把它们分成「数据集方法」与「工具方法」两类。
- 看懂 `__init__.py` 里「导入组织 + `__all__` 公开声明 + 模块文档字符串」这三段代码是如何共同决定一个子模块对外面貌的。
- 理解数据集文件存放在独立的 `dataset-<name>` 仓库、由第三方库 `pooch` 负责下载与 SHA256 校验、并缓存到本地 `scipy-data` 目录的整体工作方式。

本讲只聚焦 `__init__.py` 这一个文件，目的是先建立「整体地图」；后续讲义再逐层进入各个私有模块的内部细节。

---

## 2. 前置知识

在开始之前，先了解几个对理解本讲有帮助的概念。如果你已经熟悉，可以跳过。

- **子模块（submodule）与包（package）**：在 Python 里，一个目录只要包含 `__init__.py`，就被视为一个「包」。`scipy/datasets/` 目录下的 `__init__.py` 让 `scipy.datasets` 成为 `scipy` 大包里的一个子模块。外部使用者写 `import scipy.datasets` 时，Python 实际执行的就是这个 `__init__.py`。
- **`__all__` 是什么**：`__all__` 是一个字符串列表，它声明「当别人用 `from scipy.datasets import *` 时，哪些名字会被导出」。它同时也充当一份「公开 API 清单」，告诉使用者哪些函数是稳定对外提供的。
- **示例数据集（example datasets）**：很多科学计算库都会内置一些「小数据」（比如一张测试图像、一段心电信号），方便做演示、写教程、跑测试。`scipy.datasets` 就是 SciPy 提供这类示例数据集的统一入口。
- **可选依赖（optional dependency）**：SciPy 本身不强依赖 `pooch`，但 `scipy.datasets` 下载数据时需要它。这种「不是必须安装、用到时才需要」的第三方库，叫可选依赖。后续进阶讲义会专门讲它的降级处理。
- **SHA256 哈希**：一种把任意文件「压缩」成一串定长字符（指纹）的算法。下载完文件后，把本地文件算一遍哈希，和预先记录的哈希比对，就能判断文件是否被篡改或下载损坏。

---

## 3. 本讲源码地图

本讲只涉及一个源文件，但它是整个子模块的「门面」：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [scipy/datasets/__init__.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py) | 子模块入口与门面，决定对外暴露哪些名字、文档怎么写 | 公开 API 声明、导入组织、模块文档字符串 |

作为「门面」，`__init__.py` 自己几乎不实现业务逻辑，它做的是三件事：

1. 用 `from ._xxx import ...` 把定义在私有模块里的函数「引进来」。
2. 用 `__all__` 声明哪些是公开 API。
3. 用一段很长的文档字符串向使用者和文档生成工具说明这个子模块是什么、怎么用。

它引用的三个私有模块（本讲只需知道它们各自负责什么，不需要深入）：

| 私有模块 | 职责 |
|----------|------|
| `_fetchers.py` | 真正去下载并加载三个数据集（`ascent` / `face` / `electrocardiogram`）的函数都在这里 |
| `_download_all.py` | 提供 `download_all`，批量下载所有数据文件，也能当命令行脚本运行 |
| `_utils.py` | 提供 `clear_cache`，清理本地缓存目录 |

下划线前缀（`_fetchers` 等）是 Python 社区的约定，表示「这是内部实现细节」。`__init__.py` 把它们重新导出为没有下划线的公开名字，正是这种「内部私有 + 门面公开」的组织方式。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块拆解：

- **4.1** `__all__` 公开 API 声明
- **4.2** 模块文档字符串中的工作原理说明
- **4.3** 导入组织（`from ._fetchers / ._download_all / ._utils`）

### 4.1 `__all__` 公开 API 声明

#### 4.1.1 概念说明

一个子模块对外暴露什么，本质上有两层控制：

- **名字是否被 `import` 进当前命名空间**（由 `from ._xxx import ...` 决定）。
- **名字是否被视为「公开 API」**（由 `__all__` 决定）。

`__all__` 是一份显式的「公开合约」。它有两个实际作用：

1. 当使用者写 `from scipy.datasets import *` 时，只有 `__all__` 列表里的名字会被导入。
2. 它告诉文档工具（如 Sphinx 的 `autosummary`）和使用者：这些是稳定、对外支持的接口。

`scipy.datasets` 的 `__all__` 把五个函数分成两类：三个「数据集方法」和两个「工具方法」。

#### 4.1.2 核心流程

公开 API 的声明流程可以概括为：

```text
私有模块里定义函数
        │
        ▼
__init__.py 用 from ._xxx import 把函数名引入
        │
        ▼
__all__ 列表显式登记这些名字为「公开 API」
        │
        ▼
外部使用者通过 scipy.datasets.<name>() 调用
```

五个公开函数按职责分类如下：

| 分类 | 函数 | 作用 |
|------|------|------|
| 数据集方法 | `ascent` | 返回一张 512×512 的 8 位灰度测试图像 |
| 数据集方法 | `face` | 返回一张浣熊彩色测试图像（ndarray） |
| 数据集方法 | `electrocardiogram` | 返回一段心电图信号（一维 ndarray，单位 mV） |
| 工具方法 | `download_all` | 把所有数据集文件批量下载到指定目录 |
| 工具方法 | `clear_cache` | 清理本地缓存目录 |

注意：`__all__` 里只是字符串名字的列表；这些名字能被找到，靠的是上面那条 `import` 语句先把它们引入到 `__init__.py` 的命名空间里。两者缺一不可。

#### 4.1.3 源码精读

公开 API 的最终声明只有两行：

[__init__.py:L84-L85](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L84-L85) —— `__all__` 把五个名字登记为公开 API，前三个是数据集方法，后两个是工具方法。

```python
__all__ = ['ascent', 'electrocardiogram', 'face',
           'download_all', 'clear_cache']
```

> 说明：注意列表里的顺序是 `ascent, electrocardiogram, face`（字母序），而后两个工具方法 `download_all, clear_cache` 跟在后面，恰好与文档里「Dataset Methods / Utility Methods」两节对应。

文档字符串里也用 `autosummary` 分两组列出了同样的五个函数，可与 `__all__` 对照阅读：

- [__init__.py:L8-L16](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L8-L16) —— 文档的 `Dataset Methods` 小节，列出 `ascent / face / electrocardiogram`。
- [__init__.py:L18-L25](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L18-L25) —— 文档的 `Utility Methods` 小节，列出 `download_all / clear_cache`，并各带一句说明。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证「`__all__` 决定 `import *` 行为」这件事。
2. **操作步骤**：
   ```python
   import scipy.datasets as d
   # 1. 直接看 __all__
   print(d.__all__)
   # 2. 模拟「from scipy.datasets import *」会拿到哪些名字
   public = [n for n in dir(d) if not n.startswith('_')]
   print(public)
   ```
3. **需要观察的现象**：`d.__all__` 打印出的正是上面那五个名字；`dir(d)` 过滤掉下划线开头的私有名字后，结果应当 **包含** 这五个公开函数（可能还包含 `test` 这种辅助对象）。
4. **预期结果**：`__all__` 输出 `['ascent', 'electrocardiogram', 'face', 'download_all', 'clear_cache']`。若输出里多了或少了名字，说明你对公开 API 的理解有偏差，可回到源码对照。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `__all__` 里 `'face'` 这一项删掉（但 `from ._fetchers import face` 仍在），`scipy.datasets.face` 还能调用吗？`from scipy.datasets import *` 还会导入 `face` 吗？

> **答案**：能调用——因为名字已经被 `from ._fetchers import face` 引入到了 `__init__.py` 的命名空间，`scipy.datasets.face` 依然可访问。但 `from scipy.datasets import *` **不会**再导入 `face`，因为 `__all__` 才是控制 `import *` 行为的依据。

**练习 2**：为什么 `ascent / face / electrocardiogram` 被归为「数据集方法」，而 `download_all / clear_cache` 被归为「工具方法」？

> **答案**：前者调用后会「返回一段数据」（图像或信号），是真正「取数据」的入口；后者不返回数据本身，而是对数据文件做「管理」——批量下载到指定目录、或清理本地缓存，属于辅助性质，因此归为工具方法。

---

### 4.2 模块文档字符串中的工作原理说明

#### 4.2.1 概念说明

`__init__.py` 最开头那段用三引号包起来的大段文字，叫做**模块文档字符串（module docstring）**。它有双重身份：

- 给人类阅读：解释这个子模块是什么、怎么用。
- 给工具消费：SciPy 用 Sphinx 自动生成官方文档，这段 docstring 会被直接渲染成 `scipy.datasets` 的文档页面。

`scipy.datasets` 的这段 docstring 特别值得读，因为它把「数据集是怎么获取和存储的」整套机制讲清楚了——这正是初学者最容易困惑的地方。

#### 4.2.2 核心流程

docstring 描述的整体工作流程如下：

```text
调用 <dataset-name>()，例如 face()
        │
        ▼ （首次调用，联网）
pooch 根据 registry（文件名 → SHA256 + 远程 URL）去 GitHub 上的 dataset-<name> 仓库下载
        │
        ▼
下载完成后用 SHA256 校验文件完整性
        │
        ▼
把文件保存到系统缓存目录下的 'scipy-data' 子目录
        │
        ▼ （之后再次调用）
直接从本地缓存命中，不再联网，返回 numpy.ndarray
```

几个关键事实（全部来自 docstring 原文）：

- 数据集文件存放在 SciPy 组织下、遵循 `dataset-<name>` 命名的独立 GitHub 仓库，例如 `face` 的文件在 `https://github.com/scipy/dataset-face`。
- 下载与校验依赖第三方库 **Pooch**。
- 维护着一张「注册表」：文件名 → SHA256 哈希 + 仓库 URL，Pooch 用它来处理和校验下载。
- 首次下载后，文件被缓存到系统缓存目录下的 `scipy-data` 子目录；之后再调用就直接用缓存。
- 缓存目录因平台而异（见下方源码精读的具体路径）。

#### 4.2.3 源码精读

整段 docstring 位于 [__init__.py:L1-L77](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L1-L77)。其中几个关键片段：

- [__init__.py:L28-L37](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L28-L37) —— `Usage of Datasets`：说明调用方式就是 `'<dataset-name>()'`，首次会联网下载并缓存，之后返回一个 `numpy.ndarray`；不同数据集的返回结构与 dtype 可能不同。
- [__init__.py:L40-L55](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L40-L55) —— `How dataset retrieval and storage works`：核心机制说明，包含 `dataset-<name>` 仓库约定、Pooch 依赖、registry（SHA256 + url）、`scipy-data` 缓存目录等关键信息。
- [__init__.py:L56-L69](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L56-L69) —— `Dataset cache locations`：给出三大平台的缓存目录绝对路径：
  - macOS：`~/Library/Caches/scipy-data`
  - Linux / Unix：`~/.cache/scipy-data`（或 `XDG_CACHE_HOME` 环境变量指向的目录）
  - Windows：`C:\Users\<user>\AppData\Local\<AppAuthor>\scipy-data\Cache`
- [__init__.py:L71-L75](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L71-L75) —— 离线/受限网络环境说明：可以手动把数据集仓库内容放进上面的缓存目录，从而在无网络时也能使用。

> 提示：`face` 的远程仓库地址是 `https://github.com/scipy/dataset-face`，这正是 `dataset-<name>` 命名约定的一个实例。其它数据集同理，例如 `ascent` 对应 `dataset-ascent`。

#### 4.2.4 代码实践

1. **实践目标**：在 Python 里直接读到这段 docstring，确认它就是官方文档页面的来源。
2. **操作步骤**：
   ```python
   import scipy.datasets as d
   doc = d.__doc__ or ""
   print(doc[:200])        # 打印开头 200 个字符
   print("dataset-face" in doc, "scipy-data" in doc, "Pooch" in doc)
   ```
3. **需要观察的现象**：开头应是 `Datasets (:mod:`scipy.datasets`)`；三个关键字 `dataset-face / scipy-data / Pooch` 的判断应都为 `True`。
4. **预期结果**：三个布尔值均为 `True`，证明 docstring 里确实记载了「仓库命名约定 / 缓存目录名 / 下载依赖库」这三件事。若网络受限拿不到在线文档，这段 docstring 本身就是离线可读的权威说明。

#### 4.2.5 小练习与答案

**练习 1**：根据 docstring，`scipy.datasets.electrocardiogram` 的数据文件最可能存放在哪个 GitHub 仓库？

> **答案**：遵循 `dataset-<name>` 命名约定，应在 `https://github.com/scipy/dataset-electrocardiogram`。

**练习 2**：在断网环境下调用 `face()` 报错，但 docstring 提到一种「手动加载缓存」的办法，具体怎么做？

> **答案**：把 `dataset-face` 仓库的文件内容手动放到本平台对应的 `scipy-data` 缓存目录（Linux 上是 `~/.cache/scipy-data`），这样即使无网络，`face()` 也能从本地缓存命中文件而不报错。

---

### 4.3 导入组织（`from ._fetchers / ._download_all / ._utils`）

#### 4.3.1 概念说明

`__init__.py` 之所以能成为「门面」，关键在于它用 `from ._xxx import ...` 把分散在各个私有模块里的函数汇集到自己名下。理解这三行 import，就理解了「为什么函数定义在带下划线的模块里、却被当作 `scipy.datasets` 的公开 API」。

注意相对导入的写法：`from ._fetchers import ...` 里的那个点 `.`，表示「从当前包（也就是 `scipy.datasets` 自己）里导入」。这是一种包内模块互相引用的简洁写法。

#### 4.3.2 核心流程

门面汇集名字的过程：

```text
_fetchers.py        定义 ascent / face / electrocardiogram
_download_all.py    定义 download_all
_utils.py           定义 clear_cache
        │
        │  __init__.py 用三条 from ._xxx import 把它们引入
        ▼
scipy.datasets 命名空间里就有了这五个名字
        │
        ▼
__all__ 把它们登记为公开 API（见 4.1）
```

补充一个 SciPy 通用的小细节：文件末尾还出现了一段 `PytestTester`，它不属于本讲的五个公开 API，但属于「几乎所有 SciPy 子模块 `__init__.py` 都会有的标准结尾」，作用是把 `scipy.datasets.test()` 挂上去，方便只跑这一个子模块的测试。

#### 4.3.3 源码精读

三条导入语句是整个门面的核心：

[__init__.py:L80-L82](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L80-L82) —— 用相对导入把三个私有模块里的函数汇集到当前命名空间。

```python
from ._fetchers import face, ascent, electrocardiogram
from ._download_all import download_all
from ._utils import clear_cache
```

> 说明：每一行都从 `._` 开头的私有模块里「挑出」特定函数。这正是「内部私有 + 门面公开」模式——实现细节藏在带下划线的模块里，门面只暴露干净的公开名字。

文件末尾的标准测试入口（非本讲重点，但值得认识）：

[__init__.py:L88-L90](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L88-L90) —— 这是 SciPy 各子模块通用的写法，挂上 `test = PytestTester(__name__)` 后，使用者可以执行 `scipy.datasets.test()` 来只跑本子模块的测试，随后用 `del PytestTester` 把这个名字从命名空间删掉，避免它被当成公开 API 暴露。

```python
from scipy._lib._testutils import PytestTester
test = PytestTester(__name__)
del PytestTester
```

#### 4.3.4 代码实践

1. **实践目标**：验证三个公开函数确实「来自」带下划线的私有模块，而不是定义在 `__init__.py` 里。
2. **操作步骤**：
   ```python
   import scipy.datasets as d
   for name in ['face', 'ascent', 'electrocardiogram', 'download_all', 'clear_cache']:
       fn = getattr(d, name)
       print(f"{name:20s} -> 定义在模块: {fn.__module__}")
   ```
3. **需要观察的现象**：每个函数的 `__module__` 属性会指出它真正定义在哪个模块。前三个应是 `scipy.datasets._fetchers`，`download_all` 是 `scipy.datasets._download_all`，`clear_cache` 是 `scipy.datasets._utils`。
4. **预期结果**：输出形如
   ```
   face                 -> 定义在模块: scipy.datasets._fetchers
   ascent               -> 定义在模块: scipy.datasets._fetchers
   electrocardiogram    -> 定义在模块: scipy.datasets._fetchers
   download_all         -> 定义在模块: scipy.datasets._download_all
   clear_cache          -> 定义在模块: scipy.datasets._utils
   ```
   这直接证明了「函数定义在私有模块、由 `__init__.py` 导出」的组织方式。若某个名字找不到或模块名不符，回头对照 L80-L82 的三条 import。

#### 4.3.5 小练习与答案

**练习 1**：`from ._fetchers import face` 里的 `.` 能省略吗？省略后会怎样？

> **答案**：不能省略。这里的 `.` 表示「相对当前包导入」。若写成 `from _fetchers import face`，Python 会去 `sys.path` 里找一个叫 `_fetchers` 的顶层模块，而不是包内的 `scipy.datasets._fetchers`，通常会抛出 `ModuleNotFoundError`。

**练习 2**：为什么 `__init__.py` 末尾要写 `del PytestTester`？

> **答案**：`PytestTester` 只是被「借用」一下，用来构造 `test` 对象；它本身不是 `scipy.datasets` 想对外暴露的公开 API。写 `del PytestTester` 是把这个临时名字清理掉，避免它污染命名空间或被误当成公开接口。同时 `test` 也没出现在 `__all__` 里，进一步说明它不是数据集相关 API。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个贯穿性小任务：**「读懂门面，再动手取一个数据」**。

**任务目标**：通过一次完整的「阅读源码 → 解释机制 → 运行取数」流程，验证你对公开 API、docstring 机制和导入组织的理解。

**操作步骤**：

1. 打开 [scipy/datasets/__init__.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py)，找到 L80-L82 的三条 import 和 L84-L85 的 `__all__`，确认五个公开函数的来源与登记。
2. 安装可选依赖 pooch（在终端执行）：`pip install pooch`。
3. 运行下面这段脚本（首次会联网下载，请保持网络通畅）：
   ```python
   import scipy.datasets

   arr = scipy.datasets.face()          # 调用一个数据集方法
   print("shape:", arr.shape)           # 观察数组形状
   print("dtype:", arr.dtype)           # 观察数据类型
   print("public API:", scipy.datasets.__all__)   # 对照公开 API 分类
   ```
4. 对照 `__all__` 体会：`face` 属于「数据集方法」（返回 ndarray），而 `download_all / clear_cache` 属于「工具方法」（不返回数据本身）。
5. （进阶观察）在平台对应的缓存目录里找到刚下载的文件：
   - Linux：查看 `~/.cache/scipy-data/` 下是否多出了 face 的数据文件。

**需要观察的现象与预期结果**：

- `face()` 返回一个三维 ndarray，`shape` 为 `(768, 1024, 3)`（一张 768×1024 的彩色 RGB 图像），`dtype` 为 `uint8`。
- `__all__` 输出 `['ascent', 'electrocardiogram', 'face', 'download_all', 'clear_cache']`，前三个是数据集方法、后两个是工具方法。
- 缓存目录下能看到对应的数据文件。

> 如果首次下载因网络受限失败，可参考 docstring 的「手动加载缓存」说明（L71-L75）把数据文件放进缓存目录后重试；或在能联网的环境完成下载、再把缓存目录整体拷贝过去。若你无法确认本机能否下载成功，请将本步骤标注为「待本地验证」。

---

## 6. 本讲小结

- `scipy.datasets` 是 SciPy 中负责提供「示例数据集」的子模块，`__init__.py` 是它的门面。
- 对外暴露 **五个公开函数**：数据集方法 `ascent / face / electrocardiogram`（返回 ndarray），工具方法 `download_all / clear_cache`（批量下载与清理缓存）。
- `__all__` 是公开 API 的显式合约，它和 L80-L82 的三条 `from ._xxx import ...` 共同决定了「哪些名字对外可见」。
- 数据集文件存放在 SciPy 组织下、遵循 `dataset-<name>` 命名的独立 GitHub 仓库，由第三方库 **Pooch** 负责下载与 SHA256 校验，并缓存到本地 `scipy-data` 目录。
- 缓存目录因平台而异（macOS / Linux / Windows 路径不同）；断网时可手动把文件放进缓存目录。
- 函数定义在带下划线的私有模块里（`_fetchers / _download_all / _utils`），由 `__init__.py` 重新导出为公开 API，这是「内部私有 + 门面公开」的组织方式。

---

## 7. 下一步学习建议

本讲只读了「门面」，还没有进入任何私有模块的内部。建议下一步：

- **阅读 [u1-l2 目录结构与各文件职责](u1-l2-module-structure.md)**：逐一认识 `_fetchers.py / _registry.py / _utils.py / _download_all.py` 各自承担的职责，以及 `meson.build` 如何声明需要安装的源文件。
- **阅读 [u1-l3 第一次运行与缓存初探](u1-l3-first-run-and-cache.md)**：动手运行三个数据集函数，亲历「首次下载 → SHA256 校验 → 之后缓存命中」的全过程。
- 想直接看代码细节的读者，可以提前浏览 [_fetchers.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py) 与 [_registry.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py)，但建议先跟完入门单元再深入。

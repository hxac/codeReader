# 目录结构、构建方式与 Python+Cython 双层架构

## 1. 本讲目标

本讲是阅读 `scipy.cluster` 源码的「地图课」。读完本讲，你应该能够：

1. 看懂 `cluster/` 及其两个子模块 `vq/`、`hierarchy/` 的目录布局，知道哪类文件放在哪里。
2. 读懂 `meson.build` 是如何用 `subdir(...)` 递归编译、并把 Cython 源码 `.pyx` 编译成可被 `import` 的扩展模块的。
3. 理解每个子模块的「双层结构」：纯 Python 的封装/实现层 `_*_impl.py` 负责 docstring、输入校验与分发；Cython 的性能层 `_*.pyx` 负责热点循环。
4. 明白 `from . import _vq`、`from . import _hierarchy` 这类语句为什么能拿到「编译后的 C 扩展模块」，以及它们在运行时是如何被调用的。

本讲只讲「结构与构建」，不深入任何具体算法——那是后续讲义的内容。但你会建立起一张准确的「导入路径地图」，以后读任何函数都能快速定位它真正实现在哪里。

## 2. 前置知识

### 2.1 什么是包（package）与 `__init__.py`

在 Python 里，一个目录只要含有 `__init__.py`，就被当作一个**包**（package），可以用 `import a.b.c` 的方式导入。`__init__.py` 就是这个包的「门面」：当解释器执行 `import scipy.cluster` 时，实际运行的就是 `scipy/cluster/__init__.py` 里的代码。

`scipy.cluster` 的 `__init__.py` 几乎不写算法，它只做两件事：声明对外暴露的名字（`__all__`），并把子模块「摆上台面」（`from . import vq, hierarchy`）。

### 2.2 什么是 Cython 与扩展模块

Python 解释器逐行解释执行，对于「几万次循环」的数值计算来说太慢。**Cython** 是一种语言：它长得像 Python，但允许你写静态类型（比如 `cdef double`），然后用编译器把 `.pyx` 源码翻译成 C 代码，再编译成一个**扩展模块**（extension module，本质是一个 `.so` / `.pyd` 共享库）。

这个编译后的模块对外看起来就是一个普通的 Python 模块，可以 `import`、可以调用其中的函数——但它的内部跑的是编译后的 C 速度。所以你会在 `scipy.cluster` 里看到 `_vq.pyx`、`_hierarchy.pyx` 这样的文件：它们就是「为了速度」而存在的。

> 名字前缀的下划线 `_` 是 Python 的约定：表示「这是私有的，外部不应直接导入」。所以用户只应该用 `scipy.cluster.vq.kmeans`，而不应该直接 `import scipy.cluster.vq._vq`。

### 2.3 什么是 Meson

**Meson** 是 SciPy 用来管理编译的构建系统（取代了早期的 `setup.py` / `distutils`）。构建规则写在名为 `meson.build` 的文本文件里，每个目录一个。Meson 的两个关键概念：

- **`py3.install_sources([...], subdir: '...')`**：把纯 Python 源码安装到目标目录。
- **`py3.extension_module('名字', ...)`**：把一段 C/Cython 源码**编译**成一个扩展模块（`.so`）。
- **`subdir('子目录')`**：跳进子目录，去执行那个子目录里的 `meson.build`。这就是「递归构建」的机制。

理解了 `subdir`，你就能顺着 `meson.build` 把整个包的构建树走一遍。

## 3. 本讲源码地图

下表列出本讲涉及的关键文件（相对于 `scipy/cluster/`），以及它们在「双层架构」中的角色：

| 文件 | 角色 | 作用 |
| --- | --- | --- |
| [`__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/__init__.py) | 容器包门面 | 声明 `__all__`，重新导出 `vq`、`hierarchy`，提供 `test()` |
| [`meson.build`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/meson.build) | 顶层构建脚本 | 安装 `__init__.py`，并用 `subdir` 递归进入两个子模块 |
| [`vq/__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/__init__.py) | vq 子模块门面 | `from ._vq_impl import ...`，对外暴露 `kmeans` 等 |
| [`vq/_vq_impl.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py) | **Python 实现层** | 纯 Python 的 `kmeans`/`vq`/`whiten`，含 docstring、校验、分发 |
| [`vq/_vq.pyx`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq.pyx) | **Cython 性能层** | 编译为 `_vq` 扩展模块，提供 `vq`、`update_cluster_means` |
| [`vq/meson.build`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/meson.build) | vq 构建脚本 | 编译 `_vq` 扩展、安装 Python 源 |
| [`hierarchy/__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/__init__.py) | hierarchy 子模块门面 | `from ._hierarchy_impl import (...)`，暴露约 30 个名字 |
| [`hierarchy/_hierarchy_impl.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py) | **Python 实现层** | 纯 Python 的 `linkage`/`fcluster`/`dendrogram` 等 |
| [`hierarchy/_hierarchy.pyx`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx) | **Cython 性能层** | 编译为 `_hierarchy`，含聚类核心算法 |
| [`hierarchy/_optimal_leaf_ordering.pyx`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_optimal_leaf_ordering.pyx) | **Cython 性能层** | 编译为 `_optimal_leaf_ordering`，最优叶序算法 |
| [`hierarchy/meson.build`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/meson.build) | hierarchy 构建脚本 | 编译两个扩展、安装 Python 源 |

> 一句话规律：**用户可见的公共名字永远来自 `_*_impl.py`，而 `_*_impl.py` 内部再调用编译后的 `_*.pyx` 扩展。** `__init__.py` 只是「转发行」。

## 4. 核心概念与源码讲解

### 4.1 Meson 构建：subdir 递归与 Cython 扩展编译

#### 4.1.1 概念说明

SciPy 的源码树里有上百个目录，每个目录里都可能既有纯 Python 文件、又有需要编译的 Cython/C 文件。Meson 用一套「每个目录一个 `meson.build`、用 `subdir` 把它们串起来」的方式管理这一切。

`scipy.cluster` 的构建从 `scipy/meson.build` 里的 `subdir('cluster')`（位于 [scipy/meson.build:730](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/meson.build#L730)）开始，跳进 `scipy/cluster/meson.build`，再由后者继续 `subdir` 进入 `vq/` 和 `hierarchy/`。构建树和目录树是同构的——你顺着 `subdir` 走，就能复刻整个包的编译流程。

注意：`meson.build` 里出现的一些变量（如 `py3`、`linalg_cython_gen`、`cython_c_args`、`np_dep`、`version_link_args`）并不是在本目录定义的，而是由上层 Meson 配置在进入 `subdir` 之前就定义好的，沿作用域向下传递。所以子目录的 `meson.build` 可以直接使用它们。

#### 4.1.2 核心流程

`scipy.cluster` 的构建流程可以用下面的伪代码描述：

```
# 入口（在 scipy/meson.build 中）
subdir('cluster')
      │
      ▼
# scipy/cluster/meson.build
1. py3.install_sources(['__init__.py'], subdir:'scipy/cluster')   # 安装容器门面
2. subdir('hierarchy')                                              # 进入 hierarchy 子树
3. subdir('vq')                                                     # 进入 vq 子树
      │
      ├──► hierarchy/meson.build
      │      1. extension_module('_hierarchy', 编译 _hierarchy.pyx)   # 编译扩展1
      │      2. extension_module('_optimal_leaf_ordering', 编译 .pyx) # 编译扩展2
      │      3. install_sources(['__init__.py','_hierarchy_impl.py']) # 安装 Python 源
      │      4. subdir('tests')
      │
      └──► vq/meson.build
             1. extension_module('_vq', 编译 _vq.pyx)                 # 编译扩展
             2. install_sources(['__init__.py','_vq_impl.py'])        # 安装 Python 源
             3. subdir('tests')
```

要点：
- **`install_sources` 只搬运**：把 `.py` 文件原样放到安装目录，不编译。
- **`extension_module` 才编译**：把 `.pyx`（经 Cython 处理）翻译成 C、再编译成 `.so`，并安装为一个可 `import` 的模块。
- **编译产物与 Python 源同目录**：安装后 `_vq.so`、`_vq_impl.py`、`__init__.py` 会出现在同一个 `scipy/cluster/vq/` 目录下，所以 `from . import _vq` 能直接找到那个 `.so`。

#### 4.1.3 源码精读

顶层构建脚本非常简短，先安装门面，再递归进两个子模块：

```python
# scipy/cluster/meson.build
py3.install_sources([
    '__init__.py',
  ],
  subdir: 'scipy/cluster'
)

subdir('hierarchy')
subdir('vq')
```

这段代码（[cluster/meson.build:1-8](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/meson.build#L1-L8)）做了两件事：把容器包的 `__init__.py` 安装到 `scipy/cluster/`；然后分别进入 `hierarchy/` 和 `vq/` 子目录执行它们各自的 `meson.build`。

vq 子模块的构建脚本展示了「编译扩展 + 安装源码」的标准两段式：

```python
# scipy/cluster/vq/meson.build
py3.extension_module('_vq',                                 # 编译成名为 _vq 的扩展模块
  linalg_cython_gen.process('_vq.pyx'),                     # 先用 Cython 处理 .pyx
  c_args: cython_c_args,
  dependencies: np_dep,
  link_args: version_link_args,
  install: true,
  subdir: 'scipy/cluster/vq'
)

py3.install_sources([                                       # 纯 Python 源只安装不编译
    '__init__.py',
    '_vq_impl.py'
  ],
  subdir: 'scipy/cluster/vq'
)

subdir('tests')
```

见 [vq/meson.build:1-17](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/meson.build#L1-L17)。其中 `py3.extension_module('_vq', ...)`（[vq/meson.build:1-8](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/meson.build#L1-L8)）是关键：它声明「请把 `_vq.pyx` 编译成一个名字叫 `_vq` 的扩展模块」。`linalg_cython_gen.process('_vq.pyx')` 这一步会先用 Cython 把 `.pyx` 转成 C 源码，再交给 C 编译器。后面 `py3.install_sources(...)`（[vq/meson.build:10-15](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/meson.build#L10-L15)）则把 `__init__.py` 和 `_vq_impl.py` 这两个纯 Python 文件直接安装。

hierarchy 子模块结构完全对称，只是它要编译**两个**扩展（`_hierarchy` 和 `_optimal_leaf_ordering`）：

```python
# scipy/cluster/hierarchy/meson.build
py3.extension_module('_hierarchy', ...)                     # 编译 _hierarchy.pyx → _hierarchy
py3.extension_module('_optimal_leaf_ordering', ...)         # 编译 _optimal_leaf_ordering.pyx
py3.install_sources(['__init__.py', '_hierarchy_impl.py'], subdir: 'scipy/cluster/hierarchy')
subdir('tests')
```

见 [hierarchy/meson.build:1-26](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/meson.build#L1-L26)，其中两个 `extension_module` 在 [hierarchy/meson.build:1-17](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/meson.build#L1-L17)，Python 源安装在 [hierarchy/meson.build:19-24](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/meson.build#L19-L24)。

#### 4.1.4 代码实践

**实践目标**：亲手沿着 `meson.build` 的 `subdir` 链把构建树画出来，验证「编译产物」与「Python 源」是分开声明的。

**操作步骤**：

1. 打开 [scipy/meson.build:730](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/meson.build#L730)，确认入口 `subdir('cluster')`。
2. 打开 [cluster/meson.build](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/meson.build)，数出它有几次 `subdir(...)` 调用（应为 2 次：`hierarchy`、`vq`）。
3. 打开 [vq/meson.build](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/meson.build)，记录：哪个 `.pyx` 被编译、编译后的模块叫什么名字、哪些 `.py` 被安装。
4. 对 [hierarchy/meson.build](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/meson.build) 做同样的事，注意它编译了**两个**扩展模块。

**需要观察的现象**：每个子模块的 `meson.build` 都遵循「`extension_module`（编译）+ `install_sources`（搬运）」的固定模式；扩展模块的名字（`_vq`、`_hierarchy`、`_optimal_leaf_ordering`）恰好就是后面 `from . import _xxx` 里要导入的名字。

**预期结果**：你会得到一张「目录 → 编译产物 → Python 源」的对照表，例如 `vq/` 目录产出 `_vq`（扩展）和 `_vq_impl.py`（源），`hierarchy/` 目录产出两个扩展和 `_hierarchy_impl.py`。

#### 4.1.5 小练习与答案

**练习 1**：如果想在 `vq/` 下新增一个 Cython 文件 `_fast.pyx` 并把它编译成扩展模块，应该在哪个文件、加哪一行？  
**答案**：在 [vq/meson.build](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/meson.build) 里新增一个 `py3.extension_module('_fast', linalg_cython_gen.process('_fast.pyx'), ..., subdir: 'scipy/cluster/vq')`。它会被 `cluster/meson.build` 里的 `subdir('vq')` 自动纳入构建。

**练习 2**：为什么 `_vq_impl.py` 用的是 `install_sources` 而不是 `extension_module`？  
**答案**：因为 `_vq_impl.py` 是纯 Python（封装层），不需要编译，只需原样安装；`extension_module` 只用于需要编译成 `.so` 的 `.pyx` / C 源。

---

### 4.2 Python 封装层：`__init__.py` 到 `_*_impl.py` 的重导出

#### 4.2.1 概念说明

「双层架构」的第一层是**Python 封装/实现层**。它的设计意图是：把所有「面向用户的、需要漂亮 docstring、需要输入校验、需要处理各种数组类型」的逻辑放在纯 Python 里写，保持可读性和可维护性；而把「跑得越快越好」的循环交给 Cython。

这一层的入口是各包的 `__init__.py`。它的唯一职责是**重导出（re-export）**：把私有的 `_*_impl` 模块里的名字，以「干净、无下划线」的公开名字暴露给用户。用户写 `scipy.cluster.vq.kmeans`，实际上拿到的是 `scipy.cluster.vq._vq_impl.kmeans`——同一个函数对象，只是换了个「门牌号」。

为什么不让 `__init__.py` 直接写算法？因为这样可以让 `_*_impl.py` 文件专注于实现、拥有清晰的模块结构，而 `__init__.py` 只负责「对外契约」（docstring 汇总、`__all__` 列表）。SciPy 几乎所有子包都采用这种 `__init__.py → _xxx_impl.py` 的模式。

#### 4.2.2 核心流程

公共 API 的暴露路径如下：

```
用户代码:  from scipy.cluster.vq import kmeans
                │  触发执行 vq/__init__.py
                ▼
vq/__init__.py:  from ._vq_impl import kmeans, vq, whiten, kmeans2, ClusterError
                │  触发执行 _vq_impl.py（纯 Python）
                ▼
_vq_impl.py:     def kmeans(...): ...   ← 真正的函数定义在这里
                     内部: from . import _vq   ← 再调用 Cython 后端（见 4.3）
```

hierarchy 完全对称，只是导出的名字多得多（约 30 个），所以用括号换行的 `from ... import (...)` 形式。

#### 4.2.3 源码精读

先看容器包门面 [`__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/__init__.py)。它不含任何算法函数，只声明对外暴露两个子模块，并附带一个全包通用的 `test()` 入口：

```python
# scipy/cluster/__init__.py
__all__ = ['vq', 'hierarchy']

from . import vq, hierarchy

from scipy._lib._testutils import PytestTester
test = PytestTester(__name__)
del PytestTester
```

见 [cluster/__init__.py:25-31](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/__init__.py#L25-L31)。`__all__` 只列了 `vq` 和 `hierarchy` 两个子包——这意味着 `from scipy.cluster import *` 只会拿到这两个子包。`test = PytestTester(__name__)` 提供了 `scipy.cluster.test()` 这个便捷方法，可以一键跑本包的测试。

再看 vq 子模块的门面，关键是这一行重导出：

```python
# scipy/cluster/vq/__init__.py
from ._vq_impl import ClusterError, kmeans, kmeans2, vq, whiten
from ._vq_impl import py_vq

__all__ = ["ClusterError", "kmeans", "kmeans2", "vq", "whiten"]
__all__ += ["py_vq"]   # py_vq 是已弃用属性
```

见 [vq/__init__.py:74-80](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/__init__.py#L74-L80)。注意：所有公开函数（`kmeans`、`vq`、`whiten`、`kmeans2`）都来自 `._vq_impl`，即同目录下的 [`_vq_impl.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py)。`py_vq` 单独从 `__all__` 里追加并标注为「deprecated」（已弃用，曾意外地变成公开接口）。

hierarchy 的门面也是同样的套路，只是名字多，用了多行括号导入：

```python
# scipy/cluster/hierarchy/__init__.py
from ._hierarchy_impl import (
    ClusterNode, ClusterWarning, DisjointSet, average, centroid, complete, cophenet,
    correspond, cut_tree, dendrogram, fcluster, fclusterdata, from_mlab_linkage,
    inconsistent, is_isomorphic, is_monotonic, is_valid_im, is_valid_linkage, leaders,
    leaves_list, linkage, maxRstat, maxdists, maxinconsts, median, num_obs_linkage,
    optimal_leaf_ordering, set_link_color_palette, single, to_mlab_linkage, to_tree,
    ward, weighted
)
```

见 [hierarchy/__init__.py:100-107](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/__init__.py#L100-L107)。这一长串名字全部来自 `._hierarchy_impl`（即 [`_hierarchy_impl.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py)），随后的 `__all__`（[hierarchy/__init__.py:109-116](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/__init__.py#L109-L116)）只是把它们再次列出来，作为「公开 API 契约」。

#### 4.2.4 代码实践

**实践目标**：验证「公开函数的真身确实住在 `_*_impl` 模块里」，用 Python 自省（introspection）亲眼看到这一点。

**操作步骤**（需要已安装 SciPy；若未安装则改为纯源码阅读，见下方说明）：

1. 在能 `import scipy` 的环境里运行：

   ```python
   # 示例代码：用 __module__ 自省追踪函数真身
   from scipy.cluster.vq import kmeans
   print(kmeans.__module__)
   # 预期输出形如: scipy.cluster.vq._vq_impl

   from scipy.cluster.hierarchy import linkage
   print(linkage.__module__)
   # 预期输出形如: scipy.cluster.hierarchy._hierarchy_impl
   ```

2. 再确认 `__init__.py` 只是「转发行」：对比 `scipy.cluster.vq.kmeans` 和 `scipy.cluster.vq._vq_impl.kmeans` 是否为同一个对象：

   ```python
   # 示例代码：验证重导出是同一个函数对象
   from scipy.cluster.vq import kmeans, _vq_impl
   print(kmeans is _vq_impl.kmeans)   # 预期: True
   ```

**需要观察的现象**：`kmeans.__module__` 指向的是带下划线的 `scipy.cluster.vq._vq_impl`，而不是 `scipy.cluster.vq`；两个引用指向同一个函数对象（`is` 返回 `True`）。

**预期结果**：这证明了 `__init__.py` 没有定义任何函数，它只是把 `_*_impl` 里的名字重新挂到了公开命名空间下。

> **无法运行时的替代（源码阅读型实践）**：直接打开 [vq/__init__.py:74](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/__init__.py#L74) 和 [hierarchy/__init__.py:100-107](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/__init__.py#L100-L107)，确认每一行 `import` 的来源都是 `._*_impl`。把每个公开名字和它「真正来自哪个 impl 模块」做成一张对照表即可。运行结果：待本地验证（取决于环境是否装好 SciPy）。

#### 4.2.5 小练习与答案

**练习 1**：`from scipy.cluster import *` 会导入 `kmeans` 吗？为什么？  
**答案**：不会。因为容器包的 `__all__ = ['vq', 'hierarchy']`（[cluster/__init__.py:25](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/__init__.py#L25)）只暴露两个子包，`*` 只会拿到 `vq` 和 `hierarchy`。要拿 `kmeans` 必须写 `from scipy.cluster.vq import kmeans`。

**练习 2**：为什么 `_*_impl.py` 的名字要带下划线前缀？  
**答案**：下划线是 Python「私有」约定，表示这是内部实现细节，不属于稳定公开 API。SciPy 保留随时重构它的自由；用户只应通过 `__init__.py` 暴露的无下划线名字来使用。

---

### 4.3 双层架构的运行时协作：编译后端如何被调用

#### 4.3.1 概念说明

「双层架构」的第二层是 **Cython 性能层**。它解决的问题是：聚类算法的核心（比如「对每个观测找出最近码字」「更新簇心」）是计算热点，纯 Python 循环太慢，必须编译。

但编译后的扩展模块（`.so`）不包含 docstring、不处理输入校验、不关心「数组是 numpy 还是别的数组库」——它只接受最朴素的连续内存数组、跑得飞快。所以分工是：

- **Python 层 `_*_impl.py`**：写 docstring、做参数校验、把各种数组类型「归一化」成后端能吃的格式、决定何时调用后端、何时回退到纯 Python。
- **Cython 层 `_*.pyx`**：只负责「拿到干净的数组 → 算 → 返回结果」，全速运行。

这种「外层 Python 包一层、内层 Cython 跑循环」的模式，正是 SciPy 性能与可维护性兼顾的关键。

#### 4.3.2 核心流程

以 `vq` 编码函数为例，运行时的分发逻辑：

```
用户调用 vq(obs, code_book)
        │
        ▼  （在 _vq_impl.py 的 vq() 内）
1. xp = array_namespace(obs, code_book)        # 识别数组类型（numpy/dask/...）
2. 校验 + 转成数组 _asarray(...)
3. ct = xp.result_type(obs, code_book)         # 推断结果类型
4. 判断 ct 是否为「实数浮点」:
        ├── 是 → 转成 numpy 连续数组 → result = _vq.vq(c_obs, c_code_book)   # ★ 调用 Cython
        └── 否 → return _py_vq(obs, code_book)                                # 回退纯 Python
```

hierarchy 一侧的导入则更直接：`_hierarchy_impl.py` 在模块顶部一次性导入两个编译后端，后续函数体里直接调用它们。

#### 4.3.3 源码精读

vq 的 Python 实现层在模块顶部导入编译后端：

```python
# scipy/cluster/vq/_vq_impl.py（顶部）
from . import _vq  # type:ignore[attr-defined]
```

见 [_vq_impl.py:12](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L12)。这一句 `from . import _vq` 拿到的 `_vq`，正是 [vq/meson.build](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/meson.build) 里 `extension_module('_vq', ...)` 编译出来的那个 `.so` 模块。`# type:ignore[attr-defined]` 是给静态类型检查器看的：因为 `_vq` 是运行时才存在、源码里看不到定义，类型检查器会报「找不到属性」，所以显式忽略。

接着看 `vq()` 函数体里如何分发到后端：

```python
# scipy/cluster/vq/_vq_impl.py 中 vq() 的尾部
    xp = array_namespace(obs, code_book)
    ...
    ct = xp.result_type(obs, code_book)

    if xp.isdtype(ct, kind='real floating'):
        c_obs = xp.astype(obs, ct, copy=False)
        c_code_book = xp.astype(code_book, ct, copy=False)
        c_obs = np.asarray(c_obs)
        c_code_book = np.asarray(c_code_book)
        result = _vq.vq(c_obs, c_code_book)          # ★ 调用 Cython 后端 _vq.vq
        return xp.asarray(result[0]), xp.asarray(result[1])
    return _py_vq(obs, code_book, check_finite=False)  # 非浮点 → 回退纯 Python
```

见 [_vq_impl.py:150-157](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L150-L157)。这里清晰地展示了双层协作：当输入是「实数浮点」时，先把它转成 numpy 连续数组，再交给编译后端 `_vq.vq(...)`（[第 155 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L155)）；否则回退到纯 Python 的 `_py_vq`（[第 157 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L157)）。`_vq` 模块除了 `vq` 函数，还提供 `update_cluster_means`，它被 `kmeans`/`kmeans2` 反复调用（例如 [_vq_impl.py:266](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L266)、[_vq_impl.py:770](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L770)）。

hierarchy 的实现层则在顶部一次性导入两个后端：

```python
# scipy/cluster/hierarchy/_hierarchy_impl.py（顶部）
from . import _hierarchy, _optimal_leaf_ordering  # type:ignore[attr-defined]
```

见 [_hierarchy_impl.py:43](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L43)。这两个名字分别对应 [hierarchy/meson.build](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/meson.build) 里编译出的 `_hierarchy` 和 `_optimal_leaf_ordering` 扩展模块。后续 `linkage`、`fcluster` 等函数会在内部把数据准备好后，调用 `_hierarchy.mst_single_linkage(...)` 之类的编译函数。

> 小结这三层关系（以 vq 为例）：
> `__init__.py`（门面）→ `_vq_impl.py`（Python 实现层，`from . import _vq`）→ `_vq.pyx`（Cython 性能层，被编译为 `_vq` 扩展）。

#### 4.3.4 代码实践

**实践目标**：完整追踪一次 `from scipy.cluster.vq import kmeans` 的导入与调用路径，定位它最终来自哪个 `_*_impl` 模块，并指出其中调用 Cython 后端 `_vq` 的那一步。

**操作步骤**：

1. **导入路径追踪**（源码阅读）：从用户语句出发，逐层打开文件：
   - `from scipy.cluster.vq import kmeans` → 触发 [vq/__init__.py:74](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/__init__.py#L74) 的 `from ._vq_impl import ... kmeans ...`。
   - 所以 `kmeans` 真正定义在 [`_vq_impl.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py)（Python 实现层）。
   - `_vq_impl.py` 顶部 [_vq_impl.py:12](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L12) 执行 `from . import _vq`，引入 Cython 后端。

2. **定位 Cython 调用点**：在 `_vq_impl.py` 里搜索 `_vq.`，至少能找到三处后端调用：
   - `vq()` 函数里的 `_vq.vq(c_obs, c_code_book)`（[_vq_impl.py:155](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L155)）——把观测编码到最近码字；
   - `_kmeans` 里的 `_vq.update_cluster_means(...)`（[_vq_impl.py:266](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L266)）——更新簇心；
   - `kmeans2` 里的 `_vq.update_cluster_means(...)`（[_vq_impl.py:770](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L770)）。

3. **运行时验证（可选）**：如果环境已装好 SciPy，用下面的「示例代码」确认 `_vq` 是一个编译扩展模块：

   ```python
   # 示例代码：确认 _vq 是编译后端（.so）
   from scipy.cluster.vq import _vq
   print(type(_vq).__name__)        # 预期: module
   print(getattr(_vq, "__file__", None))  # 预期: 指向一个 .so 文件
   print([n for n in dir(_vq) if not n.startswith('__')])
   # 预期: 包含 'vq'、'update_cluster_means' 等
   ```

**需要观察的现象**：`kmeans` 的 `__module__` 指向 `_vq_impl`；`_vq.__file__` 指向一个 `.so` 共享库而非 `.py`；`kmeans` 的内部迭代每一步都通过 `_vq.update_cluster_means` 触达编译后端。

**预期结果**：你应能画出完整链路  
`from scipy.cluster.vq import kmeans` →（`vq/__init__.py`）→ `_vq_impl.kmeans` →（`_vq_impl.py` 内）→ `_vq.update_cluster_means` / `_vq.vq`（Cython 后端）。运行结果：待本地验证（依赖 SciPy 是否已编译安装）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `vq()` 在输入是「实数浮点」时才调用 Cython 后端 `_vq.vq`，其它情况回退 `_py_vq`？  
**答案**：因为 `_vq.pyx` 这个编译后端是针对浮点连续数组特化的（性能最优路径）；当输入类型不匹配（如整数）时，没有对应的优化路径，于是回退到通用、较慢但万能的纯 Python 实现 `_py_vq`，保证正确性。见 [_vq_impl.py:150-157](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L150-L157)。

**练习 2**：如果有人误删了 [vq/meson.build](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/meson.build) 里的 `extension_module('_vq', ...)`，`import scipy.cluster.vq` 会发生什么？  
**答案**：`_vq_impl.py` 顶部的 `from . import _vq`（[_vq_impl.py:12](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L12)）会失败，抛出 `ModuleNotFoundError: No module named 'scipy.cluster.vq._vq'`，进而整个 `scipy.cluster.vq` 无法导入——因为门面 `__init__.py` 在 `from ._vq_impl import ...` 时连带触发了 `_vq_impl.py` 的执行。

## 5. 综合实践

把本讲的三块知识串起来，完成一个「全链路追踪」任务。

**任务**：选取 `scipy.cluster.hierarchy.linkage`，写出它从「用户 import」到「最终触达 Cython 后端」的完整路径，并对照 `meson.build` 解释后端模块是怎么被编译出来的。

**要求**：

1. **导入链**：从 `from scipy.cluster.hierarchy import linkage` 出发，指出它经由 [hierarchy/__init__.py:100-107](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/__init__.py#L100-L107) 的哪一行来到 `_hierarchy_impl.py`，再确认 [hierarchy/_hierarchy_impl.py:43](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L43) 导入了哪些后端。
2. **构建链**：在 [hierarchy/meson.build](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/meson.build) 里找出 `_hierarchy` 这个扩展模块是怎么从 `_hierarchy.pyx` 编译来的，并说明这条 `subdir` 链是从 [cluster/meson.build](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/meson.build) 的哪一行进入的。
3. **画图**：把上面两条链合成一张图，标注「门面层 / Python 实现层 / Cython 性能层」三层，以及每层之间的 `import` 语句行号。
4. **运行验证（可选）**：用 `linkage.__module__` 确认它的真身在 `_hierarchy_impl`，用 `from scipy.cluster.hierarchy import _hierarchy; _hierarchy.__file__` 确认后端是 `.so`。

**预期产出**：一张清晰的「`linkage` 三层调用与构建图」加一段文字说明。运行验证部分若环境不具备，标注「待本地验证」即可，但源码追踪部分必须基于本讲引用的真实文件与行号完成。

## 6. 本讲小结

- `scipy.cluster` 是一个**容器包**：顶层 [`__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/__init__.py) 不含算法，只通过 `__all__ = ['vq', 'hierarchy']` 暴露两个相对独立的子模块。
- 构建由 **Meson** 驱动，靠 `subdir(...)` 递归：[`cluster/meson.build`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/meson.build) 进入 `vq/` 与 `hierarchy/`，每个子目录的 `meson.build` 各自编译扩展并安装 Python 源。
- 每个子模块都是**双层架构**：纯 Python 的 `_*_impl.py`（封装/实现层，含 docstring、校验、分发）+ Cython 的 `_*.pyx`（性能层，编译为扩展模块）。
- 公共 API 通过 `__init__.py` **重导出**：用户拿到的 `kmeans`/`linkage` 等其实定义在 `_*_impl` 里，`__init__.py` 只是「转发行」。
- 编译后端在运行时由 `from . import _vq` / `from . import _hierarchy, _optimal_leaf_ordering` 引入；`vq()` 等函数会在条件满足时把数据喂给 `_vq.vq(...)` 这样的 Cython 函数，否则回退纯 Python。
- 想找任何 `scipy.cluster` 函数的「真身」，记住口诀：**顺着 `__init__.py` 的 `from ._xxx_impl import` 找实现层，再从实现层的 `from . import _xxx` 找 Cython 后端。**

## 7. 下一步学习建议

本讲建立了「结构与构建」的全局地图。接下来建议：

1. **先把示例跑起来**：进入 [u1-l3 快速上手](u1-l3-quickstart-end-to-end.md)，用 `whiten + kmeans` 与 `linkage + fcluster` 跑两个端到端小例子，建立「手感」。
2. **再读 vq 主线**：从 [u2 向量量化与 k-means](u2-l1-whiten-preprocessing.md) 开始，深入 `_vq_impl.py` 的 `whiten` → `vq` → `kmeans` → `kmeans2`，你会反复用到本讲学到的「Python 层调 Cython 后端」的追踪方法。
3. **或读 hierarchy 主线**：从 [u3 层次聚类基础](u3-l1-linkage-matrix.md) 开始，理解 linkage matrix 数据结构与 `linkage()` 总入口。
4. **对构建感兴趣**：可对比阅读 [`scipy/meson.build`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/meson.build) 顶层脚本，看 `py3`、`linalg_cython_gen` 等变量是如何定义并沿 `subdir` 作用域传递到本模块的。

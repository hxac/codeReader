# 顶层目录结构与模块导出

## 1. 本讲目标

学完本讲，你应该能够：

- 画出 `numpy` 顶层目录里各个子包（`_core`、`lib`、`linalg`、`fft`、`random`、`ma`、`polynomial`、`f2py` 等）的组织结构与依赖关系。
- 说清楚 `_core`（底层核心）与 `lib`（上层工具）之间的职责分工。
- 看懂 `numpy/__init__.py` 是如何把几百个名字「再导出」（re-export）到 `np.` 命名空间的。
- 理解「懒加载」（lazy import）机制：为什么 `import numpy` 不会立刻加载 `numpy.linalg`、`numpy.fft`，而访问 `np.linalg` 时才会真正加载。
- 能够回答一个具体问题：**为什么 `np.array` 来自 `_core` 而不是 `lib`？**

本讲只读三个文件：`numpy/__init__.py`、`numpy/_core/__init__.py`、`numpy/lib/__init__.py`。它们是整个 NumPy 命名空间的「装配车间」。

## 2. 前置知识

在开始之前，你需要理解几个 Python 包机制的基础概念。如果已经熟悉，可以跳过本节。

- **包（package）与 `__init__.py`**：Python 里一个「目录 + `__init__.py`」就是一个包。`__init__.py` 在 `import 包名` 时自动执行，相当于这个包的「初始化脚本」。NumPy 的 `numpy/__init__.py` 长达数百行，因为它要在导入时把分散在各个子模块里的对象「汇聚」到顶层。
- **再导出（re-export）**：一个模块用 `from .sub import name` 把子模块里的名字拿到自己身上，再让用户直接 `from numpy import name` 使用。用户不需要关心 `name` 真正定义在哪个子模块。
- **`__all__`**：一个列表/集合，声明 `from 包 import *` 时会导出哪些名字，也常被文档工具和 IDE 当作「公开 API」的清单。
- **模块级 `__getattr__`（PEP 562）**：Python 3.7+ 允许在模块里定义一个 `__getattr__(name)` 函数。当访问一个模块上**不存在**的属性时，Python 会调用它。NumPy 用它实现「用到时才导入」。
- **C 扩展模块**：NumPy 的大量底层功能是用 C 写的，编译后得到 `_multiarray_umath` 这样的 `.so`/`.pyd` 文件。Python 层只是对它的薄封装。

> 关键直觉：把 `numpy/__init__.py` 想象成一个「展销大厅」。真正的货物（`ndarray`、`array`、`ufunc`）生产在工厂（`_core` 的 C 代码）里，工具（`histogram`、`pad`）生产在作坊（`lib`）里。大厅的作用只是把所有商品摆到 `np.` 这个货架上，让顾客（用户）一站式购买。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `numpy/__init__.py` | 顶层「装配车间」。从 `_core`、`lib`、`matrixlib` 汇聚出 `np.` 命名空间，定义懒加载、过期属性警告、导入时自检。 |
| `numpy/_core/__init__.py` | 核心子包的入口。负责加载 C 扩展 `multiarray`（即 `_multiarray_umath`）和 `umath`，并把 `numeric`、`shape_base` 等模块的名字汇聚到 `_core` 命名空间。 |
| `numpy/lib/__init__.py` | 工具子包的入口。导入一批私有的 `_*_impl` 模块，声明 `lib` 自己的公开名字。 |
| `numpy/_core/multiarray.py` | （辅助理解）对 C 扩展 `_multiarray_umath` 的 Python 封装，`array`、`ndarray`、`zeros` 等真正「露面」的地方。 |

## 4. 核心概念与源码讲解

### 4.1 顶层子包一览

#### 4.1.1 概念说明

`import numpy as np` 之后，`np.` 下面能点出很多东西：`np.array`、`np.linalg`、`np.fft`、`np.random`…… 这些东西并不是平铺在一个文件里的，而是分布在一堆**子包**中。

NumPy 把子包分成两类：

1. **底层核心 `_core`**：NumPy 的「发动机舱」。`ndarray`（N 维数组对象）、`ufunc`（通用函数）、`dtype`（数据类型）这些最底层、性能最关键的对象，都由 C 语言实现，住在 `_core` 里。注意名字前面有下划线，表示它**是私有的**——官方不鼓励用户直接 `import numpy._core`，所有公开对象都应从 `np.` 取。
2. **功能子包**：在 `_core` 之上构建的、面向具体领域的能力，例如线性代数 `linalg`、傅里叶变换 `fft`、随机数 `random`、遮罩数组 `ma`、多项式 `polynomial`。它们大多是纯 Python（部分会回调 C），可以理解为「使用 ndarray 的高级工具箱」。
3. **工具子包 `lib`**：一个「杂物间」。`lib/__init__.py` 自己的文档字符串说得很直白：它存放那些「既不属于 core、也不属于其他有明确用途的子包」的通用函数。

#### 4.1.2 核心流程

子包之间的依赖是**严格分层、单向依赖**的：

```text
              用户代码
                 |
        import numpy as np
                 |
        +----------------+
        | numpy/__init__ |   ← 顶层装配（再导出 + 懒加载）
        +----------------+
           /     |     \
         /       |       \
+--------+  +---------+  +----------+   +---------+  +--------+
| _core  |  |   lib   |  | linalg   |   | random  |  |  fft   |
| (C层)  |  |(纯Py工具)|  |(BLAS封装)|   |(BitGen) |  |(pocket)|
+--------+  +---------+  +----------+   +---------+  +--------+
     ↑          |
     |          +----- lib 依赖 _core（在 core 之上构建）
     |
  linalg / fft / random / ma / polynomial 都依赖 _core
```

要点：

- `_core` 是地基，**所有**功能子包都依赖它，它不依赖其他子包。
- `lib` 依赖 `_core`，提供构建在数组之上的通用工具。
- `linalg`/`fft`/`random` 等依赖 `_core`，彼此之间基本独立。

#### 4.1.3 源码精读

顶层 `__init__.py` 的文档字符串直接列出了「可用子包」，这是官方对结构的说明：

[numpy/__init__.py:L41-L55](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L41-L55) —— 顶层文档字符串里枚举的「Available subpackages」（lib、random、linalg、fft、polynomial、testing）与「Utilities」（test、show_config、`__version__`）。这是子包划分的最权威清单。

`lib` 子包的自我定位，在它自己的入口文件里说得很清楚：

[numpy/lib/__init__.py:L1-L9](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/__init__.py#L1-L9) —— `numpy.lib` 的定位：存放「既不属于 core，也不属于其他有明确用途子包」的通用函数。这段话是理解 `_core` 与 `lib` 分工的钥匙。

实际的子包目录可以通过 `git ls-files` 等命令核对。本仓库 `numpy/` 下存在 `__init__.py` 的子包包括：`_core`、`lib`、`linalg`、`fft`、`random`、`ma`、`polynomial`、`f2py`、`matrixlib`、`char`、`strings`、`rec`、`ctypeslib`、`testing`、`typing`、`core`（兼容垫片）等。

#### 4.1.4 代码实践

**实践目标**：用一段脚本，把 `np.` 下「公开的子包」逐一列出来，并分类。

**操作步骤**：

```python
# 文件名：list_subpackages.py
import numpy as np

# 这些是 numpy/__init__.py 中 __numpy_submodules__ 声明的公开子包名
declared = {
    "linalg", "fft", "dtypes", "random", "polynomial", "ma",
    "exceptions", "lib", "ctypeslib", "testing", "typing",
    "f2py", "test", "rec", "char", "core", "strings",
}

for name in sorted(declared):
    obj = getattr(np, name)      # 访问 np.<name>，触发（或命中）懒加载
    print(f"{name:12s} -> {type(obj).__name__:10s} {obj.__name__}")
```

**需要观察的现象**：每访问一个 `np.<name>`，都会得到一个 module 对象；类型是 `module`。

**预期结果**：会打印出 17 行，每一行形如 `linalg -> module numpy.linalg`。

**待本地验证**：实际是否全部为 `module`，以及是否有访问会触发额外的警告（取决于你的 NumPy 版本）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_core` 的名字前面有一个下划线，而 `lib` 没有？

> **参考答案**：下划线前缀是 Python 的「私有」约定。`_core` 是内部实现细节，API 可能在版本间变化，官方不希望用户直接依赖 `numpy._core.xxx`；`lib` 虽然也以内部为主，但它是历史悠久的公开子包，部分名字（如 `numpy.lib.NumpyVersion`）是公开的。NumPy 2.x 进一步把 `lib` 下的子模块也改成私有 `_*_impl` 命名，公开内容统一从 `np.` 取。

**练习 2**：`np.linalg` 和 `np.fft` 这两个子包，哪一个会被「立即加载」？

> **参考答案**：都不会立即加载。它们属于懒加载子包，只有真正访问 `np.linalg` 时才会触发 `import numpy.linalg`。唯一会被立即加载的是 `lib`（因为顶层 `__init__` 需要从它那里取工具函数，详见 4.3）。

---

### 4.2 `_core` 模块的导入与 `__all__` 汇聚

#### 4.2.1 概念说明

`_core` 是 NumPy 的地基。它要做两件事：

1. **加载 C 扩展**：把编译好的 `_multiarray_umath`（包含 `ndarray`、`array`、`ufunc`、`dtype` 等 C 对象）挂进来。如果这一步失败，整个 NumPy 都用不了。
2. **汇聚 Python 层封装**：C 扩展只提供「裸」对象，很多便捷函数（`linspace`、`reshape`、`einsum`）是用 Python 写的，分散在 `_core/numeric.py`、`_core/shape_base.py`、`_core/einsumfunc.py` 等文件里。`_core/__init__.py` 把它们汇总到 `numpy._core.` 这个命名空间。

「汇聚」的机制就是经典的 `from .module import *`：把子模块里 `__all__` 列出的名字，全部搬到 `_core` 自己的名下。

#### 4.2.2 核心流程

`_core/__init__.py` 的执行流程：

```text
1. 设置 OPENBLAS_MAIN_FREE 环境变量（避免 BLAS 占用主线程）
2. try: from . import multiarray        ← 加载 C 扩展（最关键、最易失败的一步）
   except ImportError: 给出详尽的排错信息
3. from . import umath                  ← 加载 ufunc 的 C 扩展
4. 校验 multiarray/umath 确实是新版封装（防止装了旧版 numpy）
5. from .numerictypes import ...        ← 标量类型表
6. multiarray.set_typeDict(...)         ← 把类型表回填给 C 层
7. from .numeric import *  等若干个星号导入  ← 汇聚 Python 封装
8. __all__ = [...] + numeric.__all__ + shape_base.__all__ + ...
9. 注册 PytestTester（让 numpy._core.test() 可用）
```

其中第 2 步是「命门」：一旦 C 扩展加载失败（比如没编译成功、Python 版本不对、平台不匹配），`_core/__init__.py` 会拼一段非常详细的错误信息，引导用户排查。

#### 4.2.3 源码精读

加载 C 扩展 `multiarray` 的核心代码：

[numpy/_core/__init__.py:L23-L91](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L23-L91) —— `try: from . import multiarray`，并在失败时检查 `_multiarray_umath` 编译产物是否存在、是否与当前 Python/平台匹配，最后拼出带排查链接的 `ImportError`。这段是 NumPy 「导入失败时为什么报错信息这么详细」的根源。

加载 `umath` 并校验两个模块都是新版封装：

[numpy/_core/__init__.py:L93-L105](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L93-L105) —— `from . import umath`，并校验 `multiarray` 和 `umath` 都带有 `_multiarray_umath` 属性，确保它们是 1.16 之后合并的封装、而非旧的独立 C 扩展。

把 Python 封装汇聚到 `_core` 命名空间：

[numpy/_core/__init__.py:L107-L123](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L107-L123) —— 从 `numerictypes` 取标量类型表，调用 `multiarray.set_typeDict(...)` 回填给 C 层；随后 `from .numeric import *`、`from .shape_base import *` 等星号导入，把 Python 封装函数汇聚到 `_core`。

`__all__` 的拼装方式——一个列表加上多个子模块的 `__all__`：

[numpy/_core/__init__.py:L152-L161](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L152-L161) —— `_core` 的 `__all__` 由一个手工列表（别名如 `acos`/`concat`/`permute_dims`，以及 `memmap`/`sctypeDict` 等）加上 `numeric.__all__`、`function_base.__all__`、`getlimits.__all__`、`shape_base.__all__`、`einsumfunc.__all__` 拼成。这是「子模块各自维护自己的公开清单、父包做并集」的典型写法。

#### 4.2.4 代码实践

**实践目标**：追踪 `np.array` 这个名字的「出生地」，验证它确实来自 `_core` 的 C 扩展。

**操作步骤**：

```python
# 文件名：trace_array.py
import numpy as np

print("array 的 __module__ 是：", np.array.__module__)
print("array 的类型是：", type(np.array))

# _core/multiarray.py 第 59 行显式把 array.__module__ 改成了 'numpy'
# 但它真正的定义在 C 扩展 _multiarray_umath 里：
from numpy._core import multiarray
print("multiarray.array 就是 np.array 吗：", multiarray.array is np.array)
```

**需要观察的现象**：`np.array` 是 `builtin_function_or_method` 类型（C 函数），而不是 Python 函数；它和 `multiarray.array` 是同一个对象。

**预期结果**：

```text
array 的 __module__ 是：numpy
array 的类型是：<class 'builtin_function_or_method'>
multiarray.array 就是 np.array 吗：True
```

**源码依据**：`_core/multiarray.py` 通过 `from ._multiarray_umath import *` 把 C 扩展里的 `array` 拿出来，并在 [numpy/_core/multiarray.py:L30-L50](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L30-L50) 的 `__all__` 里登记了 `'array'`，最后在 [numpy/_core/multiarray.py:L58-L60](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L58-L60) 把 `array.__module__` 改写为 `'numpy'`，让用户看到的归属地是顶层 `numpy`。

#### 4.2.5 小练习与答案

**练习 1**：`_core/__init__.py` 为什么要 `multiarray.set_typeDict(nt.sctypeDict)` 把类型表「回填」给 C 层？

> **参考答案**：标量类型表（`sctypeDict`，记录类型名到标量类的映射）是在 Python 层（`numerictypes.py`）维护的，但 C 层的 `multiarray` 在很多地方（比如解析 dtype 字符串）需要查这张表。所以在 Python 层把表建好后，调用 `set_typeDict` 把它交给 C 层持有，避免重复维护、也避免循环依赖。

**练习 2**：如果有人不小心装了两个 NumPy（一个是旧的 1.x），`_core/__init__.py` 是怎么发现的？

> **参考答案**：见 [numpy/_core/__init__.py:L97-L105](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L97-L105)。它检查 `multiarray` 和 `umath` 是否都带有 `_multiarray_umath` 属性——这是 1.16 合并 C 扩展后的特征。旧的独立 C 扩展没有这个属性，于是会抛出「检测到旧版 numpy」的错误，建议反复卸载再重装。

---

### 4.3 懒加载子模块（`numpy/__init__.py` 的 `__getattr__`）

#### 4.3.1 概念说明

如果 `numpy/__init__.py` 在导入时就把 `linalg`、`fft`、`random`、`ma`、`polynomial`、`f2py`…… 全部加载进来，那么 `import numpy as np` 会变得很慢，而且很多用户根本用不到 FFT 或随机数。

NumPy 的做法是**懒加载**（lazy import）：

- 在 `numpy/__init__.py` 里定义一个模块级 `__getattr__(attr)`。
- 把 `linalg`/`fft`/`random` 等子包的名字登记到一个集合 `__numpy_submodules__`，并放进 `__all__`（这样 `dir(np)` 和 `from numpy import *` 都知道它们存在）。
- 但**不**在导入时真正 `import` 它们。只有当用户第一次写 `np.linalg` 时，Python 发现 `np` 没有 `linalg` 属性，转而调用 `__getattr__("linalg")`，此时才执行 `import numpy.linalg`。

这就实现了「声明归声明，加载看需要」。

#### 4.3.2 核心流程

```text
import numpy as np
   └─ 只加载 _core / lib / matrixlib，把几百个名字摆到 np.
   └─ 登记公开子包名到 __numpy_submodules__，但不 import 它们

np.linalg            ← 用户第一次访问
   └─ Python 在 np 的命名空间里找不到 'linalg'
   └─ 调用 __getattr__('linalg')
   └─ 执行 import numpy.linalg as linalg；return linalg
   └─ （注意：return 不会缓存到 np.__dict__，所以每次访问都可能重入 __getattr__,
       但 Python 的 sys.modules 会缓存已加载的模块，真正的加载只发生一次）
```

此外，`__getattr__` 还承担了**两件副业**：

1. **过期属性警告**：对于 2.0 移除的旧名字（如 `np.int`、`np.float`），抛出带迁移指引的 `AttributeError`。
2. **未来标量警告**：对 `np.str`/`np.bytes`/`np.object` 给出 `FutureWarning`。

#### 4.3.3 源码精读

公开子包名的登记：

[numpy/__init__.py:L622-L630](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L622-L630) —— `__numpy_submodules__` 集合，列出全部 17 个公开子包名（`linalg`/`fft`/`dtypes`/`random`/`polynomial`/`ma`/`exceptions`/`lib`/`ctypeslib`/`testing`/`typing`/`f2py`/`test`/`rec`/`char`/`core`/`strings`）。注释说明：这些名字「声明可见，但通过 `__getattr__` 访问」。

`__all__` 如何把子包名和再导出名字合并：

[numpy/__init__.py:L674-L693](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L674-L693) —— `__all__` 是「子包名集合」∪「`_core.__all__`」∪「`_mat.__all__`」∪「各 `lib._*_impl.__all__`」∪ 几个手工名字（`emath`/`show_config`/`__version__`/`__array_namespace_info__`）的并集。这正是顶层「展销大厅」的完整货架清单。

`__getattr__` 的核心——逐个懒加载子包：

[numpy/__init__.py:L700-L751](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L700-L751) —— 模块级 `__getattr__`。前半段是一长串 `if attr == "linalg": import numpy.linalg ...` 分支，命中哪个子包就 `import` 并 `return` 它。这就是「用到才加载」的全部魔法。

`__getattr__` 的后半段——过期/废弃属性的处理：

[numpy/__init__.py:L759-L769](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L759-L769) —— 对 `__former_attrs__`（如 `np.int` 这类已弃用别名）和 `__expired_attributes__`（2.0 彻底移除的名字）抛出带说明的 `AttributeError`，否则抛标准的 `AttributeError`。这是 NumPy 在大版本升级时给用户的「迁移向导」。

> 旁注：顶层入口还有一道「构建期跳过」的保护——[numpy/__init__.py:L95-L104](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L95-L104)。当 `__NUMPY_SETUP__` 为真（即构建系统正在收集包元信息）时，整个 `_core` 加载与命名空间装配都会被跳过，避免在没有编译产物时 `import numpy` 崩溃。u1-l2 讲过构建链路，这里能看到它在源码层面的对应实现。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「懒加载」——证明 `import numpy` 后 `numpy.linalg` 还没被加载，访问 `np.linalg` 后才被加载。

**操作步骤**：

```python
# 文件名：prove_lazy.py
import sys
import numpy as np

# 1) 刚 import numpy，检查 linalg/fft/random 是否已在 sys.modules
before = {k for k in sys.modules if k.startswith("numpy.")}
print("import numpy 后，已加载的 numpy.* 子模块数量：", len(before))
print("numpy.linalg 已加载？", "numpy.linalg" in sys.modules)
print("numpy.fft    已加载？", "numpy.fft" in sys.modules)

# 2) 故意访问 np.linalg，触发懒加载
_ = np.linalg

after = {k for k in sys.modules if k.startswith("numpy.")}
print("\n访问 np.linalg 后，新增的子模块：")
print(sorted(after - before))
```

**需要观察的现象**：第一步里 `numpy.linalg` / `numpy.fft` 应该**不在** `sys.modules` 里（或至少尚未完整加载）；访问 `np.linalg` 之后，会新增 `numpy.linalg` 及其依赖。

**预期结果**（具体子模块列表以本地为准）：

```text
import numpy 后，已加载的 numpy.* 子模块数量：约几十个（含 _core / lib / matrixlib）
numpy.linalg 已加载？ False
numpy.fft    已加载？ False

访问 np.linalg 后，新增的子模块：
['numpy.linalg', ...]
```

**待本地验证**：「已加载的 numpy.* 子模块数量」在不同平台/版本会有差异；重点是观察「访问前 False、访问后新增」这一对比。

#### 4.3.5 小练习与答案

**练习 1**：`__numpy_submodules__` 里有 `lib`，但 `lib` 又是在 `numpy/__init__.py` 里被 `from . import lib` 立即导入的。这不矛盾吗？

> **参考答案**：不矛盾。`__numpy_submodules__` 的作用是声明「哪些子包名是公开的」，它被用于 `__all__` 和 `__dir__`，让 `dir(np)`、`from numpy import *` 能看到 `lib`。而 `lib` 确实被立即导入了（因为顶层 `__init__` 要从 `lib._function_base_impl` 等模块取工具函数，见 [numpy/__init__.py:L454-L620](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L454-L620)）。所以访问 `np.lib` 时直接命中已存在的属性，根本不会走到 `__getattr__`。集合里的名字「大部分」走懒加载，但 `lib` 是个例外。

**练习 2**：如果用户写 `np.something_that_does_not_exist`，会发生什么？

> **参考答案**：`__getattr__` 走完所有分支都不命中，最后落到 [numpy/__init__.py:L769](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L769)，抛出标准的 `AttributeError: module 'numpy' has no attribute 'something_that_does_not_exist'`。如果这个名字恰好在 `__former_attrs__` 或 `__expired_attributes__` 里，则会抛出带迁移指引的更友好的错误。

---

## 5. 综合实践

把本讲的三个最小模块串起来，完成下面这个「绘制依赖关系图 + 追根溯源」的任务。

### 任务

**目标**：用一份可运行的小脚本，验证「`np.` 命名空间 = `_core` 再导出 + `lib` 再导出 + 懒加载子包」，并解释 `np.array` 为什么来自 `_core` 而非 `lib`。

**操作步骤**：

1. **画依赖图**：根据本讲 4.1.2 的分层关系，手绘或用文字画出下面三层结构，并标注每条依赖方向：

   - 地基层：`_core`（C 扩展 `_multiarray_umath` + Python 封装）
   - 工具层：`lib`（依赖 `_core`）
   - 领域层：`linalg` / `fft` / `random` / `ma` / `polynomial` / `f2py`（都依赖 `_core`）

2. **运行下面这段验证脚本**，对照你画的图：

   ```python
   # 文件名：dependency_audit.py
   import sys
   import numpy as np

   # (A) _core 提供了哪些核心对象？
   from numpy import _core
   print("array  在 _core 里？", hasattr(_core, "array"))
   print("ndarray 在 _core 里？", hasattr(_core, "ndarray"))
   print("zeros  在 _core 里？", hasattr(_core, "zeros"))

   # (B) lib 提供的是「上层工具」，而不是 array 这种原语
   from numpy import lib
   print("\nlib 里有 array？", hasattr(lib, "array"))     # 预期 False
   print("lib 里有 histogram？", hasattr(lib, "histogram"))  # 预期 True（经再导出可见）

   # (C) 懒加载：访问前后对比
   print("\nnumpy.fft 已加载？", "numpy.fft" in sys.modules)
   _ = np.fft
   print("访问 np.fft 后已加载？", "numpy.fft" in sys.modules)

   # (D) 追根溯源 np.array
   print("\nnp.array 的类型：", type(np.array))
   print("np.array 与 _core.multiarray.array 同一对象？",
         np.array is _core.multiarray.array)
   ```

3. **回答两个问题**（写进你的学习笔记）：
   - 为什么 `np.array` 来自 `_core` 而非 `lib`？
   - `np.histogram` 又是哪一层提供的，为什么？

### 需要观察的现象

- (A) 全部为 `True`：核心原语住在 `_core`。
- (B) `lib` 里**没有** `array`，但有 `histogram`。
- (C) 访问 `np.fft` 前后，`sys.modules` 里 `numpy.fft` 从无到有。
- (D) `np.array` 是 C 函数（`builtin_function_or_method`），与 `_core.multiarray.array` 是同一对象。

### 预期结果（要点）

- `array` 是直接构造 C 层 `PyArrayObject` 的底层原语，性能敏感、与内存布局强耦合，所以必须用 C 实现，住在 `_core`（经 `_multiarray_umath` → `_core/multiarray.py` → `_core/__init__.py` → `numpy/__init__.py` 四级再导出）。
- `lib` 的自我定位是「不属于 core 也不属于其他子包的通用工具」，它在 `_core` 之上构建，本身并不提供 `array` 这种原语；`histogram` 这类纯 Python 实现的统计工具才归 `lib`。

> 待本地验证：脚本中 `hasattr` 的结果在不同次版本间高度稳定，但「`import numpy` 后 `sys.modules` 里到底有哪些 `numpy.*`」会随版本变化，重点关注对比关系而非绝对数量。

## 6. 本讲小结

- NumPy 顶层由若干**子包**组成：`_core` 是 C 实现的底层核心（`ndarray`/`ufunc`/`dtype`），`lib` 是构建在 `_core` 之上的纯 Python 工具集，`linalg`/`fft`/`random`/`ma`/`polynomial`/`f2py` 是面向领域的功能子包。
- 依赖是**单向分层**的：所有功能子包都依赖 `_core`，`_core` 不依赖它们；`lib` 依赖 `_core`。
- `numpy/__init__.py` 是「装配车间」：从 `_core` 星号导入约 320 个核心名字，从 `lib` 的各 `_*_impl` 模块导入工具函数，汇聚到 `np.` 命名空间。
- `_core/__init__.py` 的命门是加载 C 扩展 `multiarray`/`umath`，失败时会给出详尽的排查信息；它再用 `from .numeric import *` 等方式汇聚 Python 封装。
- **懒加载**：`linalg`/`fft`/`random` 等子包通过模块级 `__getattr__` 在首次访问时才 `import`，既加快启动、又按需付费；`__all__` 通过并集构造，把再导出名与子包名统一管理。
- `np.array` 来自 `_core` 而非 `lib`，因为它是直接构造 C 层数组的底层原语；`lib` 只提供上层工具。

## 7. 下一步学习建议

- **下一讲 u1-l4（ndarray 初体验与核心属性）**：会正式打开 `np.ndarray`，讲解 `shape`/`strides`/`dtype`/`flags` 的物理含义——这些都是 `_core` 里 C 对象的直接反映。学完本讲你已经知道 `ndarray` 住在 `_core`，下一讲就深入它的内部。
- **可选阅读**：在源码里浏览 [numpy/__init__.py:L120-L443](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L120-L443) 这段长长的 `from ._core import (...)`，感受一下「顶层货架」到底从 `_core` 取了多少名字——这会强化你对「再导出」机制的直觉。
- **后续衔接**：u2-l1 会讲数组创建函数，届时你会再次回到 `_core`，看 `array`/`zeros`/`arange`/`linspace` 的具体实现差异；u5-l1 会专门讲 `multiarray` 模块全貌，把本讲提到的 C 扩展桥接讲透。

# 入口与导入链路

## 1. 本讲目标

上一篇（u1-l2）我们已经把 `scipy/interpolate/` 目录分成了「公共私有模块 `_xxx.py`」「原生扩展」「弃用垫片」三类，并知道扁平公共命名空间是由 `__init__.py` 顶部的 `from ._xxx import *` 拼出来的。本讲要在这张地图上把「链路」走通，学完后你应当能够：

1. 逐行说清 `__init__.py` 的导入顺序，并解释 `__all__` 是如何用 `dir()` 在某一刻「拍快照」生成的。
2. 解释为什么写 `from scipy.interpolate.interpolate import interp1d` 这种旧式「子模块路径」会触发一条 `DeprecationWarning`，而写 `from scipy.interpolate import interp1d` 却不会。
3. 读懂垫片模块里 `__getattr__` 的「延迟弃用」机制，包括它最终委托给 `_sub_module_deprecation` 的哪一行代码真正发出警告。
4. 理解 SciPy v2.0.0 计划删除这些旧命名空间的治理意图，以及它对「该写哪种 import」的现实影响。

本讲只解决两件事：**导入链**与**垫片弃用机制**，不展开任何具体插值算法。

## 2. 前置知识

阅读本讲前，请先具备以下概念（不熟悉也没关系，下面会就地解释）：

- **命名空间（namespace）**：一个模块里「名字 → 对象」的映射。`scipy.interpolate` 这个包本身就是一个命名空间，里面装着 `interp1d`、`BSpline`、`make_interp_spline` 等名字。
- **`from module import *`**：把目标模块里「公开的」名字（由它的 `__all__` 决定，没有 `__all__` 时就是所有不以 `_` 开头的名字）批量搬进当前命名空间。
- **`__all__`**：一个字符串列表，声明「`import *` 会搬走哪些名字」。它只影响 `import *` 和文档工具，不影响你显式写 `from m import x`。
- **`__getattr__`（模块级）**：Python 3.7+ 允许在模块对象上定义一个 `__getattr__(name)` 函数。当你访问 `m.xxx` 而 `xxx` 不是模块里真实存在的属性时，Python 会转而调用 `m.__getattr__('xxx')`。这就是「访问时才算账」的钩子。
- **`DeprecationWarning`**：Python 标准的「弃用警告」类别。默认情况下它只在「被 `__main__`（即你直接运行的脚本）里的代码触发」时才显示，所以测试或第三方库里触发时常被静默——这也是为什么我们在实践里要主动捕获它。
- **`stacklevel`**：`warnings.warn` 的一个参数，决定警告「算在谁的头上」。`stacklevel=1` 算 `warn` 自己，`stacklevel=2` 算调用 `warn` 的那一层，依此类推。本讲会看到 `stacklevel=3` 这个精心选择的值。

上一讲（u1-l2）建立的认知我们会直接承接：私有模块（`_xxx.py`）是真正的实现，垫片（不带下划线的 `fitpack.py`、`interpolate.py` 等）几乎不含实现，只负责转发旧导入路径并发出弃用警告。本讲不再重复这一分类，而是把其中的「拼装」与「警告触发」两条机制讲透。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `__init__.py` | 子包入口，拼装扁平公共命名空间 | L192–L221 的导入顺序、`__all__` 的 `dir()` 快照、L219 引入垫片、L228 的 `pchip` 别名 |
| `interpolate.py` | 弃用垫片（旧路径 `scipy.interpolate.interpolate`） | `__all__`、`__dir__`、`__getattr__` 三件套 |
| `fitpack.py` | 弃用垫片（旧路径 `scipy.interpolate.fitpack`） | 同上三件套，对照不同的 `private_modules` |
| `dfitpack.py` | 弃用垫片（旧路径 `scipy.interpolate.dfitpack`） | 同上三件套，注意它指向真正私有的 `_fitpack` 扩展 |
| `scipy/_lib/deprecation.py` | 全 SciPy 共用的弃用工具 | `_sub_module_deprecation` 函数，真正发出 `DeprecationWarning` 的那一行 |

> 说明：`scipy/_lib/deprecation.py` 不在 `interpolate/` 目录下，但它是所有垫片共同委托的「警告引擎」，本讲必须读到它才能讲清警告从何而来。链接里会给出它在仓库中的完整路径。

## 4. 核心概念与源码讲解

### 4.1 import * 链路：从十几个私有模块拼出扁平公共命名空间

#### 4.1.1 概念说明

很多大型库的公共 API 是「分散实现、集中暴露」的：每个功能模块（`_bsplines.py`、`_cubic.py`、`_rgi.py`……）各自定义自己那部分类和函数，再由一个 `__init__.py` 把它们的公开名字汇聚到一个扁平的包命名空间里，让用户只写 `scipy.interpolate.XXX` 而不用关心 `X` 到底住在哪个私有文件里。

`scipy.interpolate` 就是这种模式的典型例子。它的 `__init__.py` 用一连串 `from ._xxx import *` 把十几个私有模块的公开名字「堆叠」进包命名空间。这种堆叠有两个关键性质，也是本节要讲清的：

1. **顺序敏感**：`import *` 是「搬名字进当前空间」的命令，后搬的会覆盖同名先搬的。因此导入顺序是设计决策，不能随意调换。
2. **`__all__` 是某一刻的快照**：它不是手写的清单，而是在所有 `import *` 执行完之后、用内置 `dir()` 把当前命名空间里「所有不以 `_` 开头的名字」一次性收集出来的。这意味着——快照之后才定义的名字，进不了 `__all__`。

#### 4.1.2 核心流程

`__init__.py` 的执行流程可以概括为三段：

```text
第 1 段：L192 ~ L216  连续 from ._xxx import *
        每一句把一个私有模块的公开名字搬进 scipy.interpolate 命名空间
        （后搬者覆盖同名先搬者）

第 2 段：L218 ~ L221   显式 import 7 个垫片 + 用 dir() 拍快照得到 __all__
        L219  from . import fitpack, fitpack2, interpolate, ndgriddata,
                                    polyint, rbf, interpnd
        L221  __all__ = [s for s in dir() if not s.startswith('_')]

第 3 段：L223 ~ L228   挂上 test 入口 + 补一个向后兼容别名
        L228  pchip = PchipInterpolator   ← 注意：写在 L221 之后
```

用伪代码描述 `dir()` 快照这一步：

```python
# 到执行 L221 时，命名空间里已经装满了 interp1d、BSpline、make_interp_spline ...
# dir() 返回当前所有已绑定的名字（含 __init__、__name__ 等双下划线名）
__all__ = [s for s in dir() if not s.startswith('_')]
# 于是 __all__ 自动收集到所有「公开名」，无需手写维护
```

这里有一个常被忽略但很重要的细节：`pchip = PchipInterpolator` 写在 L228，**晚于** L221 的快照。所以 `pchip` 这个名字：

- 作为属性是可以访问的：`scipy.interpolate.pchip` 能拿到 `PchipInterpolator`；
- 但**不在** `__all__` 里：`from scipy.interpolate import *` 不会搬走它。

这就是「`__all__` 是某一刻快照」的直接后果——一个真实的、可在源码里验证的例子。

#### 4.1.3 源码精读

下面这十几行就是把整个子包「拼」出来的核心：

[scipy/interpolate/__init__.py:192-216](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py#L192-L216) —— 逐个把私有模块的公开名字搬进包命名空间。每一句都对应一个实现模块，例如 `from ._interpolate import *` 带来 `interp1d`/`PPoly`/`BPoly`/`NdPPoly` 等，`from ._bsplines import *` 带来 `BSpline`/`make_interp_spline` 等。

[scipy/interpolate/__init__.py:218-219](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py#L218-L219) —— 注释明确写了「Deprecated namespaces, to be removed in v2.0.0」，随后 `from . import fitpack, fitpack2, interpolate, ndgriddata, polyint, rbf, interpnd` 把 7 个垫片模块作为「子模块名」挂到包上。注意：这一句只是**导入模块对象本身**，并不访问它们内部的名字，所以**不会**在此触发任何弃用警告（详见 4.2）。

[scipy/interpolate/__init__.py:221](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py#L221) —— `__all__ = [s for s in dir() if not s.startswith('_')]`：用 `dir()` 在此刻拍快照，自动收集所有公开名。这是「不用手写维护 `__all__`」的关键。

[scipy/interpolate/__init__.py:228](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py#L228) —— `pchip = PchipInterpolator`：向后兼容别名，但因写在 L221 之后而不在 `__all__` 内。

关于「顺序敏感」，可以验证一个具体来源：`RegularGridInterpolator` 与 `interpn` 当前定义在 `_rgi.py` 里。

[scipy/interpolate/_rgi.py:1](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/_rgi.py#L1) —— `__all__ = ['RegularGridInterpolator', 'interpn']`，而类定义在 [scipy/interpolate/_rgi.py:66](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/_rgi.py#L66)。

而 `_interpolate.py` 的 `__all__` 完全不同：

[scipy/interpolate/_interpolate.py:1](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/_interpolate.py#L1) —— `__all__ = ['interp1d', 'interp2d', 'lagrange', 'PPoly', 'BPoly', 'NdPPoly']`，里面没有 `RegularGridInterpolator`。

由于 `__init__.py` 在 L192 先 `from ._interpolate import *`、后在 L212 才 `from ._rgi import *`，公共命名空间里的 `RegularGridInterpolator` 自然来自 `_rgi`。推广而言：**只要两个模块都导出同名符号，后执行的 `import *` 决定最终归属**——这正是导入顺序被当作设计决策对待的原因。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`__all__` 是快照」这一论断——确认 `pchip` 虽然能用，却不在 `__all__` 中。

**操作步骤**：

```python
import scipy.interpolate as si

# (1) pchip 作为属性能否访问？
print("pchip 是不是 PchipInterpolator：", si.pchip is si.PchipInterpolator)

# (2) pchip 是否在 __all__ 里？
print("pchip 在 __all__ 中：", "pchip" in si.__all__)
print("PchipInterpolator 在 __all__ 中：", "PchipInterpolator" in si.__all__)

# (3) 对照：from ... import * 会不会带进 pchip？
ns = {}
exec("from scipy.interpolate import *", ns)
print("import * 之后 ns 里有 pchip：", "pchip" in ns)
print("import * 之后 ns 里有 PchipInterpolator：", "PchipInterpolator" in ns)
```

**需要观察的现象**：

- `si.pchip is si.PchipInterpolator` 应为 `True`（别名确实生效）。
- `"pchip" in si.__all__` 应为 `False`，而 `"PchipInterpolator" in si.__all__` 应为 `True`。
- `import *` 之后命名空间里有 `PchipInterpolator`、没有 `pchip`。

**预期结果**：以上三组观察都与源码逻辑一致，说明 `__all__` 在 L221 拍快照时 `pchip` 尚未定义（它在 L228 才赋值），因此被排除在 `__all__` 之外，但仍是可访问的模块属性。若你的环境结果不同，请先确认 SciPy 版本对应的源码是否仍如此排布。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `__init__.py` 第 221 行的 `__all__` 赋值**移到**第 228 行（`pchip = PchipInterpolator`）之后，`__all__` 的内容会发生什么变化？

> **参考答案**：`pchip` 会进入 `__all__`，因为此时 `dir()` 已经能看到它。这反过来说明 `__all__` 反映的是「赋值那一刻」命名空间的状态，而非「整个文件执行完」的状态。

**练习 2**：为什么 `from . import fitpack, ..., interpolate`（L219）这一句不会触发弃用警告？

> **参考答案**：这一句只把 7 个垫片**模块对象**绑定到包命名空间，并没有访问模块内部的任何属性（如 `interp1d`）。垫片的弃用警告是写在 `__getattr__` 里的，只有「属性访问」才会触发它，单纯「拿到模块对象」不会。

---

### 4.2 垫片模块与延迟弃用机制（__getattr__）

#### 4.2.1 概念说明

早期的 SciPy 用户习惯了从「子模块路径」导入，例如：

```python
from scipy.interpolate.interpolate import interp1d   # 旧式
from scipy.interpolate.fitpack import splrep          # 旧式
```

但随着子包内部重构，这些「不带下划线」的模块名（`interpolate`、`fitpack`、`dfitpack`……）其实是**本应私有**的内部细节。SciPy 决定在 v2.0.0 删除它们，但为了不立刻破坏海量存量代码，保留了一层**垫片（shim）**：模块文件还在，但里面几乎不剩实现，只剩一个 `__getattr__` 钩子。当你照旧路径去取里面的名字时，这个钩子会：

1. 发出一条 `DeprecationWarning`，告诉你「请改从 `scipy.interpolate` 这个扁平命名空间导入」；
2. 仍然把真正的对象返回给你，让你的旧代码继续能跑。

这叫**延迟弃用（lazy deprecation）**——「惰性」体现在：警告只在「你真的去取这个名字」时才发出，导入子包、甚至导入垫片模块本身都不会报警。这样既给了迁移窗口，又不会在每次 `import scipy` 时刷屏。

#### 4.2.2 核心流程

一个垫片模块（以 `interpolate.py` 为例）的结构是固定的三件套：

```text
1. __all__     : 一个字符串列表，声明「本旧模块过去对外暴露过哪些名字」
                 （这些名字在垫片里其实并没有真正定义，所以标了 # noqa: F822）

2. __dir__()   : 返回 __all__，让 dir(scipy.interpolate.interpolate)
                 仍然列出那些旧名字（兼容 IDE 自动补全 / 文档工具）

3. __getattr__(name) :
        当用户访问 旧模块.name  时被调用
        → 委托给 _sub_module_deprecation(...)
        → 它内部 warnings.warn(..., DeprecationWarning, stacklevel=3)  # 真正报警
        → 然后从真正的私有模块（如 _interpolate）里取回对象并返回
```

`_sub_module_deprecation` 内部做四件事：

```text
(a) 若 name 不在 __all__ 里 → 直接 raise AttributeError（说明根本没这个名字）
(b) 构造一段中文/英文的提示消息（"请改从 scipy.interpolate 导入 …"）
(c) warnings.warn(message, category=DeprecationWarning, stacklevel=3)  ← 警告在此发出
(d) 遍历 private_modules，从第一个能找到该名字的私有模块里取回真实对象并 return
```

为什么是 `stacklevel=3`？因为调用栈是：

```text
[用户代码] from scipy.interpolate.interpolate import interp1d      ← stacklevel 3 指向这里
   ↓ 触发属性访问
[垫片] __getattr__('interp1d')                                      ← stacklevel 1
   ↓ 调用
[_sub_module_deprecation] warnings.warn(..., stacklevel=3)          ← stacklevel 2
```

`stacklevel=3` 让警告的「来源」显示为**用户自己的那一行 import**，而不是垫片或工具函数内部——这正是迁移提示该出现的地方。

#### 4.2.3 源码精读

先看垫片本身。`interpolate.py` 全文就是一个标准垫片：

[scipy/interpolate/interpolate.py:1-3](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/interpolate.py#L1-L3) —— 顶部注释直白声明「This file is not meant for public use and will be removed in SciPy v2.0.0」。

[scipy/interpolate/interpolate.py:8-20](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/interpolate.py#L8-L20) —— `__all__` 列出旧路径下曾暴露的名字（`interp1d`、`PPoly`、`RegularGridInterpolator` 等），并标 `# noqa: F822`：因为这些名字在本文件里**根本没有被定义**，flake8 默认会报「`__all__` 里有未定义名字」，这里显式抑制。

[scipy/interpolate/interpolate.py:27-30](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/interpolate.py#L27-L30) —— `__getattr__` 把所有属性访问统一委托给 `_sub_module_deprecation`，并告诉它「真正的实现散落在 `_interpolate`、`fitpack2`、`_rgi` 这几个私有模块里，请去那里取」。

另外两个垫片结构完全一致，只是 `__all__` 和 `private_modules` 不同，可以对照阅读：

[scipy/interpolate/fitpack.py:28-31](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/fitpack.py#L28-L31) —— `fitpack` 垫片的 `__getattr__`，`private_modules=["_fitpack_py"]`，对应 `splrep`/`splev`/`splint` 等函数式 FITPACK 接口。

[scipy/interpolate/dfitpack.py:21-24](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/dfitpack.py#L21-L24) —— `dfitpack` 垫片的 `__getattr__`，`private_modules=["_fitpack"]`。注意它指向的是带下划线的 `_fitpack`（一个原生 C 扩展模块），这正是上一讲提到的「两套 FITPACK 后端」之一。

真正发出警告的代码在共用工具里：

[scipy/_lib/deprecation.py:15-16](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/deprecation.py#L15-L16) —— `_sub_module_deprecation` 的签名，注意默认参数 `dep_version="1.16.0"`（个别垫片如 `interpnd` 会显式传 `dep_version="1.17.0"` 覆盖它）。

[scipy/_lib/deprecation.py:44-49](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/deprecation.py#L44-L49) —— 第 (a) 步：若被访问的 `attribute` 不在 `all` 列表里，直接抛 `AttributeError`，并附上「该命名空间已弃用、将在 2.0.0 移除」的说明。

[scipy/_lib/deprecation.py:51-66](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/deprecation.py#L51-L66) —— 第 (b) 步：构造消息。如果能从正确的扁平命名空间取到该对象，消息是「请改从 `scipy.interpolate` 导入」；否则是「该属性将随命名空间一起在指定版本移除」。

[scipy/_lib/deprecation.py:68](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/deprecation.py#L68) —— 第 (c) 步，**全机制的核心一行**：`warnings.warn(message, category=DeprecationWarning, stacklevel=3)`。这就是你看到的那条 `DeprecationWarning` 的真正源头。

[scipy/_lib/deprecation.py:70-78](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/deprecation.py#L70-L78) —— 第 (d) 步：遍历 `private_modules`，从第一个能取到该名字的私有模块里 `getattr` 出真实对象并 `return`。所以即使发了警告，旧代码依然能拿到能用的对象，不会立刻崩。

补充一点关于 `interp1d` 本身：它当前在文档里被标注为 `.. legacy:: class`（见 [scipy/interpolate/_interpolate.py:189-196](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/_interpolate.py#L189-L196)），这是一个文档标记，并不在构造时发运行时警告。因此本讲实践里观察到的警告，是**单纯由垫片命名空间**触发的，不会和「类自身弃用」混淆。（顺带一提：同文件里的 `lagrange` 才是真正在运行时 `warnings.warn` 的弃用函数，见 [scipy/interpolate/_interpolate.py:105-107](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/_interpolate.py#L105-L107)，计划在 1.20.0 移除——那是另一条独立的弃用线，本讲不展开。）

#### 4.2.4 代码实践

**实践目标**：用旧命名空间路径导入 `interp1d`，捕获并解析它触发的 `DeprecationWarning`，验证「警告来源指向用户自己的代码」「拿到的对象是真的」「正确路径不会报警」三件事。

**操作步骤**：

```python
import warnings

# === A. 旧路径：会触发垫片的 __getattr__ → _sub_module_deprecation ===
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")          # 确保 DeprecationWarning 一定被记录
    from scipy.interpolate.interpolate import interp1d   # 旧式子模块路径

print(f"捕获到 {len(caught)} 条警告：")
for w in caught:
    print(f"  类别   : {w.category.__name__}")   # 预期: DeprecationWarning
    print(f"  消息   : {w.message}")
    print(f"  来源文件: {w.filename}")             # 预期: 指向你正在运行的这个脚本
    print(f"  来源行号: {w.lineno}")               # 预期: 上面那行 from ... import interp1d
    print("  " + "-" * 50)

print("拿到的对象:", interp1d)                     # 预期: 仍是真实的 interp1d 类

# === B. 新路径：作为对照，不应触发命名空间弃用警告 ===
with warnings.catch_warnings(record=True) as caught2:
    warnings.simplefilter("always")
    from scipy.interpolate import interp1d as interp1d_new   # 推荐的扁平路径

print(f"\n新路径捕获到 {len(caught2)} 条命名空间弃用警告（预期为 0）。")
print("两个对象是同一个:", interp1d is interp1d_new)
```

**需要观察的现象**：

1. A 段捕获到 **1 条** `DeprecationWarning`，消息形如：
   `Please import 'interp1d' from the 'scipy.interpolate' namespace; the 'scipy.interpolate.interpolate' namespace is deprecated and will be removed in SciPy 2.0.0.`
2. 该警告的 `filename`/`lineno` 指向**你自己脚本里** `from scipy.interpolate.interpolate import interp1d` 那一行——这正是 `stacklevel=3` 的效果。
3. `interp1d` 仍然拿到了真实可用的类对象（第 (d) 步的兜底返回）。
4. B 段（扁平路径）**不**触发命名空间弃用警告，且 `interp1d is interp1d_new` 为 `True`，说明两条路径最终拿到同一个对象，只是旧路径多了一条警告。

**预期结果**：与上述四点一致。如果你在 A 段额外看到关于 `interp1d` 类自身弃用的警告，请核对 SciPy 版本——本讲撰写时所读源码里 `interp1d` 仅有 `.. legacy::` 文档标记、不发运行时警告（见 4.2.3 末尾）。若运行环境与预期不符，标注「待本地验证」并记录实际 SciPy 版本。

> 提示：捕获到的警告数量、确切措辞可能随 SciPy 版本微调；核心判据是「旧路径报警且 `stacklevel` 指向用户代码、新路径不报警、两者对象同一」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 A 段的 `warnings.simplefilter("always")` 去掉，在很多环境下你会**看不到**这条警告。为什么？

> **参考答案**：Python 默认会把 `DeprecationWarning` 过滤掉，除非它是由 `__main__`（顶层脚本）里的代码触发的。在交互环境、测试框架或被导入的库里触发时常被静默。`simplefilter("always")` 强制「所有警告一律记录」，配合 `catch_warnings(record=True)` 才能可靠捕获，这也是写迁移检查脚本时的标准做法。

**练习 2**：访问一个**不在**垫片 `__all__` 里的名字（例如 `scipy.interpolate.interpolate.this_does_not_exist`）会发生什么？请对照 [deprecation.py:44-49](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/deprecation.py#L44-L49) 解释。

> **参考答案**：会抛 `AttributeError`，且消息里同时说明「该命名空间已弃用、将在 2.0.0 移除」。因为 `_sub_module_deprecation` 的第 (a) 步先判断 `attribute not in all`，不在就 raise，而不会去发 `DeprecationWarning` 再返回——这避免了「拿一个根本不存在的名字」被误当成正常的弃用访问。

**练习 3**：垫片里的 `__dir__()` 起什么作用？删掉它会怎样？

> **参考答案**：`__dir__()` 让 `dir(scipy.interpolate.interpolate)` 仍能列出旧名字（返回 `__all__`），从而兼容 IDE 自动补全、`tab` 补全和文档生成工具。删掉它，`dir()` 就只能看到模块里真实定义的名字（基本只有 `__getattr__`、`__all__` 等），旧名字在补全里「消失」，但 `__getattr__` 仍能让显式访问生效——只是体验变差，不影响功能。

## 5. 综合实践

把本讲两块内容串起来，做一次「命名空间侦探」任务：写一个脚本，对 `scipy.interpolate` 的公共命名空间做一次体检，并对照垫片机制得出结论。

**任务要求**：

1. 列出 `scipy.interpolate.__all__` 的前 10 个名字，并标注每个名字「来自哪个私有模块」（提示：用 `interp1d.__module__`、`BSpline.__module__` 等读取对象的真实定义模块）。
2. 验证 4.1 的「快照」结论：检查 `pchip` 是否在 `__all__` 中、是否可作为属性访问。
3. 用 `warnings.catch_warnings(record=True)` 分别测试三条导入路径各捕获到几条 `DeprecationWarning`：
   - `from scipy.interpolate import interp1d`（扁平，推荐）
   - `from scipy.interpolate.interpolate import interp1d`（旧垫片路径）
   - `from scipy.interpolate.fitpack import splrep`（另一个旧垫片路径）
4. 对第 3 步每条警告，打印 `w.category.__name__`、`w.message` 与 `w.filename`，验证 `stacklevel=3` 让来源指向你自己的脚本。
5. 把结论整理成一张表：「导入路径 → 警告数 → 是否推荐 → 迁移建议」。

**参考实现骨架**（需你补全观察与结论）：

```python
import warnings
import scipy.interpolate as si

# 1. 追溯每个公开名的真实来源模块
for name in si.__all__[:10]:
    obj = getattr(si, name)
    mod = getattr(obj, "__module__", "(无 __module__)")
    print(f"{name:30s} <- {mod}")

# 2. 快照结论
print("pchip 在 __all__:", "pchip" in si.__all__,
      "| 可访问:", hasattr(si, "pchip"))

# 3 & 4. 三条路径的警告对比
def try_path(label, stmt):
    ns = {}
    with warnings.catch_warnings(record=True) as cw:
        warnings.simplefilter("always")
        exec(stmt, ns)
    print(f"\n[{label}] {stmt}")
    print(f"  警告数: {len(cw)}")
    for w in cw:
        print(f"    {w.category.__name__}: {w.message}")
        print(f"      @ {w.filename}:{w.lineno}")

try_path("扁平(推荐)", "from scipy.interpolate import interp1d")
try_path("旧垫片", "from scipy.interpolate.interpolate import interp1d")
try_path("另一垫片", "from scipy.interpolate.fitpack import splrep")
```

**预期结果**：扁平路径警告数为 0；两条垫片路径各报 1 条 `DeprecationWarning`，消息都包含「will be removed in SciPy 2.0.0」，且来源行指向 `exec` 所在行（因 `exec` 内的 import 在该栈帧触发）。通过这张表，你能直观看到「为什么要迁移到扁平命名空间」。

## 6. 本讲小结

- `scipy.interpolate` 的扁平公共命名空间，是由 `__init__.py` L192–L216 的连续 `from ._xxx import *` 把十几个私有模块的公开名字「堆叠」出来的，导入顺序是设计决策（后执行者决定同名符号归属）。
- `__all__` 不是手写清单，而是 L221 用 `dir()` 在那一刻拍的快照；写在快照之后的 `pchip = PchipInterpolator`（L228）因此进不了 `__all__`，但仍可作为属性访问。
- L219 把 7 个垫片模块挂到包上，但这一步只导入模块对象、不访问其属性，因此**不触发**任何弃用警告。
- 旧式 `from scipy.interpolate.interpolate import interp1d` 之所以报警，是因为垫片的 `__getattr__` 被触发，最终走到 `_sub_module_deprecation` 的 [deprecation.py:68](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/deprecation.py#L68) `warnings.warn(..., stacklevel=3)`。
- 这是「延迟弃用」：警告只在真正访问属性时发出，发完警告后仍从私有模块（如 `_interpolate`）取回真实对象并返回，旧代码不会立刻崩。
- `stacklevel=3` 让警告来源指向**用户自己的 import 行**；SciPy 计划在 v2.0.0 删除整组旧命名空间垫片，因此新代码应一律使用扁平的 `scipy.interpolate` 路径。

## 7. 下一步学习建议

本讲讲清了「公共 API 是怎么拼出来的、旧路径为何被弃用」。接下来建议：

- **横向对照**：用本讲的方法去读 `scipy/stats/__init__.py`、`scipy/linalg/__init__.py` 等其它子包，你会发现它们也用了「`from ._xxx import *` + 垫片 `__getattr__` + `_sub_module_deprecation`」的同一套治理模式，理解一个子包就理解了 SciPy 的命名空间治理全局。
- **进入算法**：从下一单元（u2 一维插值快速上手）开始，我们将离开「工程结构」主题，正式进入插值算法本身——先从 `CubicSpline`/`PchipInterpolator`/`make_interp_spline` 这些最常用的一维插值器用起。
- **延展阅读弃用治理**：如果你对 v2.0.0 的迁移计划感兴趣，可以先读 `__init__.py` 顶部 docstring 里对各功能的分类，并在后续 u17-l2「弃用治理与命名空间演进」里系统学习 `_sub_module_deprecation`、`interp2d` 已移除案例、以及 `lagrange`/`pade` 的弃用时间线。

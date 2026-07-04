# 公共命名空间与 API 导出链路

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `import scipy.signal` 之后，名字 `scipy.signal.butter` 是怎么一步步「长出来」的；
- 解释四层接力结构：**实现模块 → `_signal_api` 聚合 → `_support_alternative_backends` 装饰 → `__init__` 暴露**，以及每一层各自承担什么职责；
- 理解 `_signal_api` 为什么叫「裸 API（bare API）」、它的 `__all__` 是如何用 `dir()` 自动生成的；
- 理解 `_support_alternative_backends` 如何在函数上叠加「多后端委托（CuPy/JAX）」与「能力标注（`xp_capabilities`）」两道装饰，并据此再次导出；
- 解释为什么旧路径 `scipy.signal.filter_design.butter` 仍然可用、却会抛出 `DeprecationWarning`，以及它背后的 `_sub_module_deprecation` 延迟重定向机制。

本讲承接 [u1-l2 源码目录结构与模块组织](u1-l2-directory-structure.md)：你已经知道 `signal/` 里有「`_` 前缀私有实现模块」「弃用 stub 模块」「编译扩展」三类文件，本讲要回答的是——**这些零散的私有模块，如何被「编织」成一个干净的公共命名空间 `scipy.signal`**。

## 2. 前置知识

本讲几乎不涉及数字信号处理算法本身，只涉及 Python 的**模块导入与命名空间**机制。需要先理解几个概念：

- **命名空间（namespace）**：一个模块（`.py` 文件）就是一个命名空间，里面用 `=` 绑定的名字（函数、类、变量）就是它的属性。`import` 的本质是「把别的命名空间里的名字搬进当前命名空间」。
- **`__all__`**：一个模块里若定义了列表 `__all__`，它声明「这是本模块的公共 API」。`from module import *` 只会搬入 `__all__` 里列出的名字（没有 `__all__` 时则搬入所有不以 `_` 开头的名字）。
- **`__getattr__`（模块级）**：Python 3.7+ 允许在模块里定义一个模块级函数 `__getattr__(name)`。当访问 `module.某名字` 而该名字又不存在时，Python 会改去调用 `__getattr__(name)`。这是实现「延迟导入 / 重定向」的关键钩子，本讲弃用 stub 就靠它。
- **子模块导入的副作用**：当某个包内的模块被首次导入时，Python 会自动把它作为属性挂到所属**包对象**上。例如导入 `scipy.signal._filter_design` 会使得 `scipy.signal` 这个包对象上多出一个 `_filter_design` 属性。这一点在本讲末尾的 `del` 清理里很关键。
- **装饰器（decorator）**：`@deco def f` 等价于 `f = deco(f)`，即用一个「包装函数」替换原函数。本讲中 `delegate_xp` 和 `xp_capabilities` 都是装饰器，但行为不同：前者会真正包一层 wrapper，后者只贴元数据、返回原对象。

## 3. 本讲源码地图

本讲围绕「导出链路」这一条线，涉及以下文件，按在链路中的位置排列：

| 文件 | 在链路中的角色 | 本讲关注点 |
| --- | --- | --- |
| [_signaltools.py] / [_filter_design.py] 等私有实现模块 | **第 0 层：真正的算法实现** | 以 `butter` 为例，定位它的定义位置 |
| [_signal_api.py] | **第 1 层：裸 API 聚合** | 顶部 import 序列、`dir()` 自动生成 `__all__` |
| [_delegators.py] | （第 2 层的辅助）每个函数的「签名委托器」 | `butter_signature` 如何判定哪些参数是数组 |
| [_support_alternative_backends.py] | **第 2 层：装饰 + 多后端委托再导出** | `delegate_xp`、装饰循环、`xp_capabilities`、`capabilities_overrides` |
| [__init__.py] | **第 3 层：对外暴露 + 清理脚手架** | 尾部 `from ... import *`、`__all__`、`del`、弃用 stub 导入 |
| [filter_design.py] | **第 4 层：弃用 stub** | `__getattr__` 调用 `_sub_module_deprecation` |
| [_lib/deprecation.py] | （第 4 层的辅助）弃用工具 | `_sub_module_deprecation` 的判空 + 警告 + 取值逻辑 |
| [_lib/_array_api.py] | （第 2 层的辅助）能力标注装饰器 | `xp_capabilities` 只贴元数据、不改变行为 |

> 链接均为本仓库当前 HEAD（`ce1f6477`）的永久链接，下文代码精读处会再给出带行号的精确片段。

## 4. 核心概念与源码讲解

### 4.1 命名空间导出全景：四层接力与 `__init__` 的最终暴露

#### 4.1.1 概念说明

一个大型子包要把几百个函数暴露成一个干净的 `scipy.signal.*`，而又想同时满足几个相互拉扯的目标：

1. **实现要分散**在不同模块里（`_filter_design.py` 放滤波器设计、`_signaltools.py` 放滤波……），否则单文件会大到无法维护；
2. 这些实现模块**不应被用户直接 import**（私有，随时可重构），所以要加 `_` 前缀；
3. 但子包要支持 **CuPy/JAX 等数组后端**，需要在「实现函数」外面包一层「按后端分发」的逻辑；
4. 还要为每个函数**标注它在各后端上的能力**（能不能在 GPU 跑、能不能 JIT……），供文档和测试使用；
5. 最后，对外只暴露**一个**扁平的 `scipy.signal.butter`，而把上面那些「脚手架」藏起来。

scipy.signal 的解法是把这件事拆成一条**四层接力流水线**，每一层只做一件事，层层转发：

#### 4.1.2 核心流程

以 `butter` 为例，从「函数定义」到「用户调用」，数据（名字 `butter`）流经四层：

```
第 0 层  实现模块 _filter_design.py
            def butter(...)              ← 真正的算法
              │  （被聚合层收集）
              ▼
第 1 层  _signal_api.py   （"bare API" 裸聚合层）
            from ._filter_design import *
            __all__ = [s for s in dir() if not s.startswith('_')]   ← 自动收集
              │  （被装饰层导入）
              ▼
第 2 层  _support_alternative_backends.py   （装饰 + 委托层）
            from ._signal_api import *
            for name in _signal_api.__all__:
                f = delegate_xp(...)(bare)   ← 可选：CuPy/JAX 委托包装
                f = capabilities(f)          ← 总是：贴后端能力元数据
                vars()[name] = f
              │  （被 __init__ 导入）
              ▼
第 3 层  __init__.py   （对外暴露 + 清理）
            from ._support_alternative_backends import *
            __all__ = _support_alternative_backends.__all__
            del _support_alternative_backends, _signal_api, _delegators  ← 抹掉脚手架

第 4 层  弃用 stub（并行机制）
            filter_design.py 等：__getattr__ → _sub_module_deprecation
            让旧路径 scipy.signal.filter_design.butter 仍能用、但发警告
```

要点：**前三层是「同一条主链」的串行接力，第 4 层是一条并行的「向后兼容」侧链**，专门照顾还在用老 import 路径的代码。

#### 4.1.3 源码精读：`__init__` 尾部的「三行收尾」

整条主链的「终点」就在 [`__init__.py`] 末尾，只有寥寥几行：

[\_\_init\_\_.py:L312-L319](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L312-L319) —— 注释 `bring in the public functionality from private namespaces` 直白说明意图：把私有命名空间里的公共功能「搬」过来：

```python
# bring in the public functionality from private namespaces
from ._support_alternative_backends import *
from . import _support_alternative_backends
__all__ = _support_alternative_backends.__all__
del _support_alternative_backends, _signal_api, _delegators  # noqa: F821  # pyrefly:ignore[unbound-name]
```

逐行解读：

- `from ._support_alternative_backends import *`：把第 2 层导出的所有公共函数（含已装饰的 `butter`）搬入 `scipy.signal` 命名空间。因为第 2 层定义了 `__all__`，所以只有公共名字会被搬入。
- `from . import _support_alternative_backends`：单独再 import 一次模块对象本身（注意不是 `*`），这样下一行才能用 `_support_alternative_backends.__all__`。
- `__all__ = _support_alternative_backends.__all__`：让 `scipy.signal` 的公共 API 列表与第 2 层完全一致。
- `del _support_alternative_backends, _signal_api, _delegators`：把三个**中间脚手架模块**从 `scipy.signal` 命名空间里抹掉，使用户无法（轻易）经 `scipy.signal._signal_api` 这种路径触达内部实现。

关于这行 `del` 的小细节：`_signal_api` 和 `_delegators` 在 `__init__.py` 里并没有被显式 `import`，那为什么 `del` 不会报 `NameError`？因为它们是作为**子模块**被间接导入的（第 2 层里有 `from . import _signal_api` / `from . import _delegators`），而 Python 在导入子模块时会把子模块作为属性挂到所属包对象上——于是 `scipy.signal` 包对象上就**自动有了** `_signal_api`、`_delegators` 属性，`del` 才能生效。末尾的 `# noqa: F821` 和 `# pyrefly:ignore[unbound-name]` 正是为了让静态检查器（它不懂这种导入副作用）忽略「该名字未在当前作用域显式定义」的告警。

紧接着，[`__init__.py:L322-L326](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L322-L326) 导入了所有弃用 stub 模块，激活第 4 层侧链：

```python
# Deprecated namespaces, to be removed in v2.0.0
from . import (
    bsplines, filter_design, fir_filter_design, lti_conversion, ltisys,
    spectral, signaltools, waveforms, wavelets, spline
)
```

这一步只是「注册」这些 stub 模块为 `scipy.signal` 的属性，使 `scipy.signal.filter_design` 这个路径可达；真正发生什么，要等到用户去访问里面的属性（见 4.4 节）。

#### 4.1.4 代码实践：追踪 `butter` 的完整链路

1. **实践目标**：亲手验证 `scipy.signal.butter` 是同一条流水线的产物，并理解它指向的真实定义。
2. **操作步骤**：在已安装 SciPy 的环境中运行下面这段内省脚本。
3. **观察现象**：关注函数的 `__module__`、文档里的「array backend」注释、以及两个名字是否指向**同一个对象**。
4. **预期结果**：

```python
# 示例代码：追踪 scipy.signal.butter 的导出链路
import scipy.signal as sig

# (1) butter 来自哪个实现模块？
print(sig.butter.__module__)
# 预期：scipy.signal._filter_design  ← 第 0 层的真正定义处

# (2) 公共命名空间里的 butter，与私有模块里的 butter，是否同一个对象？
import scipy.signal._filter_design as fd
print(sig.butter is fd.butter)
# 预期（默认 SCIPY_ARRAY_API 关闭时）：True
#   —— 因为 xp_capabilities 装饰器只贴元数据、返回原对象，
#      且 delegate_xp 在 SCIPY_ARRAY_API 关闭时根本不包装。
#   （若你设置了环境变量 SCIPY_ARRAY_API=1，则可能为 False，见 4.3 节。）

# (3) 文档末尾是否被 xp_capabilities 注入了后端能力说明？
print(sig.butter.__doc__[-200:])
# 预期：能看到一段关于该函数在 CuPy 等后端上支持情况的表格/说明。
```

5. **画链路图**：对照上面三段输出，把 `butter` 经历的四层画成箭头图，并标注每一层做了什么（聚合 / 装饰 / 暴露）。如果你设置了 `SCIPY_ARRAY_API=1` 重跑，注意第 (2) 步结果的变化，并在图上标注「第 2 层多包了一层 delegate wrapper」。

> 说明：以上 `__module__` 与 `is` 判定的结果已在源码层面核实（见 4.3.3 对 `xp_capabilities` 返回原对象的分析）；不同 SciPy 版本若改动了装饰策略，结果可能不同，请以本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果删掉 `__init__.py` 里的 `del _support_alternative_backends, _signal_api, _delegators` 这一行，用户能多访问到哪些路径？会带来什么问题？

> **答案**：用户将能直接 `scipy.signal._signal_api`、`scipy.signal._delegators` 访问内部脚手架。这些模块是私有的、不保证稳定，暴露它们会让用户写出依赖内部实现的脆弱代码，也模糊了「公共 API」的边界。`del` 的作用就是把内部实现藏好。

**练习 2**：为什么 `__init__.py` 要写 `from . import _support_alternative_backends` 这一整句，而不是只写 `from ._support_alternative_backends import *`？

> **答案**：因为下一行要用 `_support_alternative_backends.__all__`。`import *` 只搬入 `__all__` 里的公共名字，并不会把模块对象本身绑定到 `_support_alternative_backends` 这个名字上；所以需要单独再 `from . import` 一次模块对象，才能访问它的 `.__all__` 属性。

---

### 4.2 第 1 层聚合：`_signal_api` 裸 API

#### 4.2.1 概念说明

实现散落在十几个 `_*` 模块里，第 2 层（装饰层）需要把它们「当成一个整体」来遍历。如果让第 2 层自己去 `from ._filter_design import *`、`from ._signaltools import *`……就会把「收集实现」和「装饰实现」两件事搅在一起。

scipy.signal 的做法是插入一个**专职收集层** [`_signal_api.py`]，它的模块文档字符串把意图说得非常直白：

> This --- private! --- module only collects implementations of public API for `_support_alternative_backends`. The latter --- also private! --- module adds delegation to CuPy etc and re-exports decorated names to `__init__.py`

也就是说，`_signal_api` 只做一件事：**把各实现模块的公共函数聚拢到一个命名空间里，形成一个「裸的、未装饰的」API 集合**，方便第 2 层统一处理。「裸（bare）」是相对于第 2 层之后的「装饰过」而言——这里还没有任何后端委托或能力标注。

#### 4.2.2 核心流程

`_signal_api` 的流程极简：

1. 用一连串 `from ._xxx import *` 把每个实现模块的公共函数搬进来；
2. 用一行 `__all__ = [s for s in dir() if not s.startswith('_')]` **自动**生成公共 API 列表——凡是搬进来的、且不以 `_` 开头的名字，都算公共 API。

这种「用 `dir()` 自动生成 `__all__`」的写法意味着：**新增一个实现函数时，只要它所在的实现模块被 `import *` 进来、且名字不以 `_` 开头，就会自动出现在 `_signal_api.__all__` 里，进而自动出现在 `scipy.signal.*` 里**——无需手动维护任何注册表。这是该层设计的核心便利。

#### 4.2.3 源码精读

[_signal_api.py:L1-L7](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signal_api.py#L1-L7) 的文档字符串点明它是「私有聚合层」：

```python
"""This is the 'bare' scipy.signal API.

This --- private! --- module only collects implementations of public  API
for _support_alternative_backends.
...
"""
```

[_signal_api.py:L9-L28](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signal_api.py#L9-L28) 是收集动作本身——注意有 `import *`（搬入整个模块的公共函数）和具名 import（只搬入个别函数）两种用法：

```python
from . import _sigtools, windows         # noqa: F401
from ._waveforms import *        # noqa: F403
from ._max_len_seq import max_len_seq       # noqa: F401
...
from ._filter_design import *         # noqa: F403   ← butter 就从这里进来
...
from ._signaltools import *         # noqa: F403
from ._savitzky_golay import savgol_coeffs, savgol_filter  # noqa: F401
...
from .windows import get_window  # keep this one in signal namespace  # noqa: F401
```

几个要点：

- `# noqa: F403`：抑制「`import *` 不利于静态分析」的告警——这里正是故意用 `*` 来批量收集。
- `# noqa: F401`：抑制「导入的名字未被本模块使用」的告警——因为本模块的职责就是「转手」，本身不调用这些函数。
- 末行 `from .windows import get_window` 旁的注释 `keep this one in signal namespace`：`get_window` 本属于 `windows` 子包，但这里特意把它也提到顶层 `scipy.signal.get_window`，方便用户。
- 其中 [_signal_api.py:L17](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signal_api.py#L17) `from ._filter_design import *` 正是 `butter` 进入聚合层的入口（`butter` 在 [`_filter_design.py:L3383](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_filter_design.py#L3383) 定义）。

最后，[_signal_api.py:L31](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signal_api.py#L31) 用 `dir()` 自动生成清单：

```python
__all__: list[str] = [s for s in dir() if not s.startswith('_')]
```

`dir()` 在模块顶层调用时，返回当前模块命名空间里所有已绑定的名字；过滤掉 `_` 前缀后，剩下的就是「刚刚被 `import *` 搬进来的公共函数」，正好作为本层的 `__all__` 交给第 2 层。

#### 4.2.4 代码实践：验证「自动收集」与来源模块

1. **实践目标**：确认 `_signal_api` 的 `__all__` 是自动生成的、且 `butter` 的来源模块确实是 `_filter_design`。
2. **操作步骤**：

```python
# 示例代码：内省聚合层与来源
import scipy.signal as sig

# (1) butter 的实现来自哪个模块？
print(sig.butter.__module__)   # 预期：scipy.signal._filter_design

# (2) 统计公共 API 的来源模块分布，体会 _signal_api 的"自动收集"
from collections import Counter
public_names = [n for n in dir(sig) if not n.startswith('_')]
src_modules = Counter(getattr(sig, n).__module__ for n in public_names
                      if hasattr(getattr(sig, n), '__module__'))
print(src_modules.most_common(5))
# 预期：能看到 scipy.signal._filter_design、._signaltools、._spectral_py 等多个来源，
#       印证"_signal_api 把多个模块聚拢成一个命名空间"。
```

3. **需要观察的现象**：`__module__` 指向私有模块（而非 `scipy.signal`），说明聚合层只是「搬运」而非「重定义」。
4. **预期结果**：`butter.__module__ == 'scipy.signal._filter_design'`。
5. 若无法运行，记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果在 `_filter_design.py` 里新增一个公共函数 `def my_new_filter(...)`（不以 `_` 开头），它会不会自动出现在 `scipy.signal` 里？为什么？

> **答案**：会。因为 `_signal_api` 用 `from ._filter_design import *` 搬入所有公共名字，再用 `dir()` 自动生成 `__all__`，第 2 层又遍历这个 `__all__` 做装饰并 `vars()[name] = f`，最终 `__init__` 用 `import *` 暴露。整条链上没有任何手动注册表，新增函数自动「流」到顶层。

**练习 2**：为什么 `_signal_api` 对 `max_len_seq`、`savgol_filter` 等用具名 import（`from ._xxx import foo`），而对 `_filter_design`、`_signaltools` 用 `import *`？

> **答案**：具名 import 用于「只想搬入其中个别函数」的模块（例如 `_savitzky_golay` 里只需 `savgol_coeffs` 和 `savgol_filter` 两个）；`import *` 用于「整个模块的公共函数都要搬入」的情形。两种写法都是为聚合服务，选择取决于「要搬多少」。

---

### 4.3 第 2 层装饰：`_support_alternative_backends` 与多后端委托

#### 4.3.1 概念说明

第 1 层产出的是「裸 API」。第 2 层 [`_support_alternative_backends.py`] 要在这批裸函数上做**两件事**，然后把装饰后的结果再次导出：

1. **多后端委托（仅当开启时）**：若用户设置了环境变量 `SCIPY_ARRAY_API=1`（开启 Array API 多后端），并且该函数有一个对应的「签名委托器」（在 [`_delegators.py`] 里），就给函数包一层 `delegate_xp` wrapper——调用时，若输入数组是 CuPy/JAX 数组，就转发到 `cupyx.scipy.signal` / `jax.scipy.signal` 的同名函数。
2. **能力标注（总是做）**：无论是否开启多后端，都给函数贴上 `xp_capabilities` 元数据——声明它在各后端上的支持情况（如 `butter` 是 `cpu_only`、CuPy 除外、不能 JAX JIT 等），并据此在文档里注入一张后端支持表。

这一层也是私有的，但它产出的函数就是最终暴露给用户的版本。

#### 4.3.2 核心流程

装饰层的核心是一个 `for` 循环，遍历 `_signal_api.__all__` 里的每个名字：

```
for name in _signal_api.__all__:
    bare = getattr(_signal_api, name)              # 取出裸函数
    delegator = getattr(_delegators, name + "_signature", None)
    if SCIPY_ARRAY_API and delegator is not None:
        f = delegate_xp(delegator, "signal")(bare)  # 包一层后端委托 wrapper
    else:
        f = bare                                    # 不包装，直接用原函数
    if 不是模块:
        caps = capabilities_overrides.get(name, 默认能力)
        f = caps(f)                                 # 贴能力元数据（返回原对象）
    vars()[name] = f                                # 绑定到本层命名空间
```

两条决策线很关键：

- **委托是否生效**取决于 `SCIPY_ARRAY_API`（全局开关）**且**该函数有委托器；
- **能力标注恒定生效**，但 `capabilities(f)` 这个装饰器**不改变函数行为**（只贴元数据、返回原对象），所以默认情况下 `scipy.signal.butter is _filter_design.butter` 为真。

#### 4.3.3 源码精读

[_support_alternative_backends.py:L7-L10](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_support_alternative_backends.py#L7-L10) 是本层的入口与 `__all__` 透传：

```python
from ._signal_api import *   # noqa: F403
from . import _signal_api
from . import _delegators
__all__ = _signal_api.__all__
```

装饰循环在文件末尾 [_support_alternative_backends.py:L376-L392](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_support_alternative_backends.py#L376-L392)：

```python
for obj_name in _signal_api.__all__:
    bare_obj = getattr(_signal_api, obj_name)
    delegator = getattr(_delegators, obj_name + "_signature", None)

    if SCIPY_ARRAY_API and delegator is not None:
        f = delegate_xp(delegator, MODULE_NAME)(bare_obj)
    else:
        f = bare_obj

    if not isinstance(f, types.ModuleType):
        capabilities = capabilities_overrides.get(
            obj_name, get_default_capabilities(obj_name, delegator)
        )
        f = capabilities(f)  # pyrefly:ignore[not-callable]

    # add the decorated function to the namespace, to be imported in __init__.py
    vars()[obj_name] = f
```

逐段解读：

- `delegator = getattr(_delegators, obj_name + "_signature", None)`：按命名约定 `<函数名>_signature` 去查委托器；查不到（返回 `None`）说明该函数不做后端委托。
- `if SCIPY_ARRAY_API and delegator is not None`：两个条件**同时**满足才包 `delegate_xp`。`SCIPY_ARRAY_API` 是从 `scipy._lib._array_api` 导入的布尔开关，默认关闭，所以默认安装下绝大多数函数走 `else` 分支、**不被包装**。
- `capabilities_overrides.get(...)`：先查 `capabilities_overrides` 字典里有没有为该函数**专门**写的能力声明；没有就用 `get_default_capabilities` 给默认值（无委托器或在 `untested` 集合里的函数默认 `np_only`）。
- `f = capabilities(f)`：`capabilities` 是 `xp_capabilities(...)` 返回的装饰器。关键在 [_lib/_array_api.py:L922-L938](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/_array_api.py#L922-L938) 的 `decorator`：

```python
def decorator(f):
    capabilities_table[f] = capabilities       # 仅登记元数据
    doc = FunctionDoc(f)
    ...
    f.__doc__ = doc                            # 仅改文档
    return f                                   # 返回原对象，不包装！
```

它**不创建 wrapper**，只把能力信息登记到 `capabilities_table`、改写 `__doc__`，然后 `return f`（原对象）。这就是为什么默认情况下「装饰前后的 `butter` 是同一个对象」。

`delegate_xp` 则是真正会包一层的装饰器，[_support_alternative_backends.py:L33-L67](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_support_alternative_backends.py#L33-L67)：

```python
def delegate_xp(delegator, module_name):
    def inner(func):
        @functools.wraps(func)
        def wrapper(*args, **kwds):
            xp = delegator(*args, **kwds)          # 调签名委托器，判定数组命名空间
            if is_cupy(xp) and func.__name__ not in CUPY_BLACKLIST:
                ...                                # 转发到 cupyx.scipy.signal.同名函数
                return cupyx_func(*args, **kwds)
            elif is_jax(xp) and func.__name__ in JAX_SIGNAL_FUNCS:
                ...                                # 转发到 jax.scipy.signal.同名函数
                return jax_func(*args, **kwds)
            else:
                return func(*args, **kwds)         # 否则用原函数
        return wrapper
    return inner
```

它的核心是先调用 `delegator(*args, **kwds)` 拿到「输入数组属于哪个命名空间（NumPy / CuPy / JAX）」，再据此决定是否转发。而**签名委托器**就是那个「判定命名空间」的小函数，例如 [`_delegators.py:L75-L78](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_delegators.py#L75-L78)：

```python
def bessel_signature(N, Wn, *args, **kwds):
    return array_namespace(Wn)

butter_signature = bessel_signature
```

`butter_signature` 与 `bessel_signature` 共用：它们都只看 `Wn`（截止频率）参数是不是数组、属于哪个命名空间——因为对这两个设计函数而言，`Wn` 是唯一的「数组类」入参（`N` 阶数是标量）。

最后，每个函数的「能力」声明写在 [_support_alternative_backends.py:L174-L372](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_support_alternative_backends.py#L174-L372) 的 `capabilities_overrides` 字典里，例如 `butter`：

```python
"butter": xp_capabilities(cpu_only=True, exceptions=["cupy"], jax_jit=False,
                          allow_dask_compute=True),
```

含义：`butter` 仅在 CPU 上运行、CuPy 后端是例外（不支持）、不能被 JAX JIT 编译、允许 Dask 延迟计算。

#### 4.3.4 代码实践：观察能力标注与委托开关

1. **实践目标**：直观看到第 2 层给函数贴上的「能力标注」，并理解 `SCIPY_ARRAY_API` 开关的作用。
2. **操作步骤**：

```python
# 示例代码：观察第 2 层的装饰产物
import scipy.signal as sig

# (1) 文档里应有 xp_capabilities 注入的后端支持表
doc = sig.butter.__doc__
print("含 backend 说明：", "CuPy" in doc or "backend" in doc.lower())

# (2) 默认 SCIPY_ARRAY_API 关闭：butter 与裸实现是同一对象
import scipy.signal._filter_design as fd
print("默认 is 同一对象：", sig.butter is fd.butter)
```

3. **进阶（可选）**：在**另一个**shell 里设置环境变量再启动 Python，对比委托是否生效——

```bash
SCIPY_ARRAY_API=1 python -c "import scipy.signal as s; \
import scipy.signal._filter_design as f; print(s.butter is f.butter)"
```

4. **需要观察的现象**：
   - 第 (1) 步应能看到文档里被注入了后端能力说明（这是 `xp_capabilities` 的副作用）；
   - 第 (2) 步默认为 `True`（因为 `capabilities` 只贴元数据、`delegate_xp` 未启用）；
   - 第 (3) 步开启 `SCIPY_ARRAY_API=1` 后，由于 `butter` 有委托器 `butter_signature`，`delegate_xp` 会包一层 wrapper，`is` 判定可能变为 `False`。
5. **预期结果**：默认 `True`；`SCIPY_ARRAY_API=1` 下可能为 `False`。若你本地未安装 CuPy/JAX，wrapper 仍会存在（开关只决定是否包装，不决定是否真的能转发），故 `is` 结果以「是否包装」为准。

> 说明：`is` 判定的依据是 `xp_capabilities` 装饰器 `return f`（不改对象）与 `SCIPY_ARRAY_API` 的默认值，已在源码核实。具体取值请以本地输出为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `butter` 在 `capabilities_overrides` 里被标成 `exceptions=["cupy"]`，而 `delegate_xp` 里又有 `CUPY_BLACKLIST`？这两个机制有什么不同？

> **答案**：`CUPY_BLACKLIST`（[_support_alternative_backends.py:L24-L27](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_support_alternative_backends.py#L24-L27)）控制「**是否转发**」——名单里的函数即使在 CuPy 后端下也直接用 SciPy 原实现（因为 `cupyx` 版本签名不兼容）；`capabilities_overrides` 里的 `exceptions=["cupy"]` 控制「**测试与文档如何标注**」。两者目标不同：一个是运行时转发策略，一个是能力声明/测试标记。`butter` 不在 `CUPY_BLACKLIST` 里（理论上可转发），但在能力表里标注 CuPy 为例外（实际未在 CuPy 上验证/支持）。

**练习 2**：`get_default_capabilities` 在什么情况下返回 `np_only`？

> **答案**：当函数没有委托器（`delegator is None`），或函数名在 `untested` 集合里时（[_support_alternative_backends.py:L116-L119](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_support_alternative_backends.py#L116-L119)）。前者表示该函数压根没接入多后端；后者表示虽接入但缺测试，保守地标成「仅 NumPy」。

---

### 4.4 弃用 stub 模块与 `_sub_module_deprecation` 重定向

#### 4.4.1 概念说明

历史上，用户是这样使用滤波器设计的：

```python
from scipy.signal.filter_design import butter   # 旧路径
```

后来 scipy.signal 把所有实现收进了 `_` 前缀的私有模块，并希望用户改用扁平路径 `scipy.signal.butter`。但直接删掉 `filter_design` 模块会立刻破坏大量现存代码。

折中方案是保留一个**「壳」模块** [`filter_design.py`]（以及 `signaltools.py`、`bsplines.py` 等共 10 个），它**不包含任何真实实现**，只是：

- 维护一份 `__all__`（声明「我看起来还有这些名字」）；
- 定义一个模块级 `__getattr__`：当用户访问 `scipy.signal.filter_design.butter` 时，Python 找不到 `butter`，就改调 `__getattr__('butter')`；
- `__getattr__` 调用 `_sub_module_deprecation`，发出 `DeprecationWarning`，然后**从私有实现模块取回真实函数返回**。

这样旧代码「还能跑、但会收到警告」，给用户迁移时间，计划在 v2.0.0 彻底移除这些 stub（见 [`__init__.py:L322](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L322) 的注释 `to be removed in v2.0.0`）。

#### 4.4.2 核心流程

```
用户代码：scipy.signal.filter_design.butter
        │
        ▼  Python 在 filter_design 模块里找不到 butter
filter_design.__getattr__('butter')
        │
        ▼
_sub_module_deprecation(
    sub_package="signal", module="filter_design",
    private_modules=["_filter_design"], all=__all__, attribute="butter"
)
        │
        ├─ butter in __all__?  是 → 继续；否则抛 AttributeError
        ├─ warnings.warn(..., DeprecationWarning)   ← 发警告
        └─ return getattr(scipy.signal._filter_design, "butter")  ← 取回真实函数
```

注意一个细节：`_sub_module_deprecation` 最终从**私有模块** `_filter_design` 取值，返回的是**裸实现**；而经主链暴露的 `scipy.signal.butter` 是第 2 层装饰后的版本。对 `butter` 而言两者默认是同一对象（4.3 已分析），但在开启 `SCIPY_ARRAY_API` 时，两者可能不同——这就是为什么官方推荐用扁平路径：它能拿到带后端委托的「正确装饰版本」。

#### 4.4.3 源码精读

壳模块 [`filter_design.py:L1-L4](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/filter_design.py#L1-L4) 开头就声明了「不打算被公开使用」：

```python
# This file is not meant for public use and will be removed in SciPy v2.0.0.
# Use the `scipy.signal` namespace for importing the functions
# included below.
```

[filter_design.py:L7-L18](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/filter_design.py#L7-L18) 是一份「看起来还存在」的名字清单：

```python
__all__ = [  # noqa: F822
    'findfreqs', 'freqs', 'freqz', 'tf2zpk', 'zpk2tf', 'normalize',
    ...
    'butter', 'cheby1', 'cheby2', 'ellip', 'bessel',
    ...
]
```

注意 `# noqa: F822`：它抑制「`__all__` 里有名字、但模块中未定义」的告警——因为这些名字**确实**没有在壳里定义，全靠下面的 `__getattr__` 动态提供。

核心是 [filter_design.py:L25-L28](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/filter_design.py#L25-L28) 的模块级 `__getattr__`：

```python
def __getattr__(name):
    return _sub_module_deprecation(sub_package="signal", module="filter_design",
                                   private_modules=["_filter_design"], all=__all__,
                                   attribute=name)
```

它的全部行为委托给 [`_lib/deprecation.py`] 里的 `_sub_module_deprecation`，见 [_lib/deprecation.py:L15-L78](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/deprecation.py#L15-L78)。关键三段：

```python
# (1) 名字是否合法？
if attribute not in all:
    raise AttributeError(
        f"`scipy.{sub_package}.{module}` has no attribute `{attribute}`; ...")

# (2) 计算正确导入路径，发警告
correct_import = f"scipy.{sub_package}.{correct_module}" if correct_module \
                 else f"scipy.{sub_package}"      # 默认即 scipy.signal
attr = getattr(import_module(correct_import), attribute, None)
... message = f"Please import `{attribute}` from the `{correct_import}` namespace; ..."
warnings.warn(message, category=DeprecationWarning, stacklevel=3)

# (3) 从私有模块取回真实函数
for module in private_modules:
    try:
        return getattr(import_module(f"scipy.{sub_package}.{module}"), attribute)
    except AttributeError as e:
        ...
```

要点：

- `private_modules=["_filter_design"]`：声明「本壳的内容实际来自 `_filter_design`」。一个壳可以对应多个私有模块（列表），逐个尝试取值。
- `stacklevel=3`：让警告指向「用户调用处」而非库内部，方便用户定位。
- 默认 `correct_module=None`，所以警告建议的「正确路径」就是 `scipy.signal`（顶层扁平命名空间）。

#### 4.4.4 代码实践：触发并捕获弃用警告

1. **实践目标**：亲手走一遍旧路径，确认它「能用但报警告」，并理解警告内容。
2. **操作步骤**：

```python
# 示例代码：观察弃用 stub 的重定向
import warnings
import scipy.signal as sig

# 默认 DeprecationWarning 不显示，需显式打开
warnings.simplefilter('always')

# 用旧路径访问 butter
fd = sig.filter_design                # 这是弃用壳模块
b = fd.butter                         # 触发 __getattr__('butter') → 发 DeprecationWarning
print(type(b))                        # 仍是一个可调用的 butter 函数

# 对比：扁平路径不发警告、且默认指向同一实现
print("两者默认同一对象：", b is sig.butter)
```

3. **需要观察的现象**：访问 `fd.butter` 时控制台应打印一条 `DeprecationWarning`，提示「请从 `scipy.signal` 命名空间导入 `butter`」。
4. **预期结果**：`b` 可正常调用（如 `b(4, 0.2)` 能返回滤波器系数），证明旧路径仍可用；`b is sig.butter` 默认为 `True`（同 4.3 分析）。
5. 也可以用命令行开关代替 `simplefilter`：`python -W default::DeprecationWarning your_script.py`。

> 说明：`DeprecationWarning` 默认被 Python 过滤，必须用 `simplefilter('always')` 或 `-W default` 才能看到，这是 Python 自身行为，非 SciPy 特例。

#### 4.4.5 小练习与答案

**练习 1**：若用户访问 `scipy.signal.filter_design.no_such_func`（不在 `__all__` 里），会发生什么？

> **答案**：`__getattr__('no_such_func')` 仍被调用，进入 `_sub_module_deprecation`；因 `attribute not in all`，函数抛出 `AttributeError`，提示「该模块没有这个属性，且本模块已弃用、将在 SciPy 2.0.0 移除」。即非法名字不会静默成功。

**练习 2**：为什么 stub 的 `__getattr__` 最终从 `private_modules=["_filter_design"]` 取值，而不是直接从 `scipy.signal`（公共命名空间）取值？

> **答案**：从私有实现模块取值能拿到「最原始、未装饰」的函数对象，避免在「壳」与「主链」之间形成循环依赖或拿到已包装版本带来的副作用。对 `butter` 这类（默认装饰不改对象）两者等价；但设计上「壳 → 私有实现」是更直接、更少耦合的路径，也符合「壳只是私有实现的兼容门面」这一定位。

---

## 5. 综合实践

把本讲四层串起来，完成下面这个「链路审计」小任务：

1. 任选一个公共函数（建议 `butter`，也可换 `welch`、`find_peaks` 等）；
2. 画出它的**完整四层链路图**，标注：
   - 第 0 层：它在哪个私有模块的哪一行定义？（用永久链接给出，例如 [`_filter_design.py:L3383`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_filter_design.py#L3383)）；
   - 第 1 层：它是经 [`_signal_api.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signal_api.py) 哪一行 `import` 进入聚合层的？是否有对应的 `*_signature` 委托器（查 [`_delegators.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_delegators.py)）？
   - 第 2 层：它在 [`capabilities_overrides`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_support_alternative_backends.py#L174-L372) 里的能力声明是什么（`cpu_only` / `exceptions` / `jax_jit`）？是否在 `CUPY_BLACKLIST` 或 `untested` 里？
   - 第 3 层：它如何经 [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py) 暴露？
   - 第 4 层：它出现在哪些弃用 stub 的 `__all__` 里？（例如 `butter` 既在 `filter_design.py` 也在……请用 Grep 在各 stub 里搜确认。）
3. 用一小段 Python（综合 4.1.4 与 4.4.4 的脚本）验证：扁平路径不发警告、旧路径发 `DeprecationWarning`、且两者默认指向同一实现对象。
4. 写一句结论：**为什么官方推荐扁平路径 `scipy.signal.butter` 而非 `scipy.signal.filter_design.butter`？**（提示：从「拿到正确装饰版本」「不依赖将移除的私有模块」「无警告」三个角度作答。）

## 6. 本讲小结

- scipy.signal 的公共 API 由一条**四层接力**流水线产生：实现模块 → `_signal_api` 聚合 → `_support_alternative_backends` 装饰 → `__init__` 暴露。
- [`_signal_api.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signal_api.py) 是私有的「裸 API 聚合层」，用 `from ._xxx import *` 收集各实现模块的公共函数，并用 `dir()` **自动生成** `__all__`，新增函数无需手动注册。
- [`_support_alternative_backends.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_support_alternative_backends.py) 给每个函数叠加两道处理：可选的 `delegate_xp`（仅 `SCIPY_ARRAY_API` 开启且有委托器时包装，负责 CuPy/JAX 转发）与恒定的 `xp_capabilities`（贴后端能力元数据、改文档，**返回原对象、不改行为**）。
- [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py) 用 `from ._support_alternative_backends import *` 暴露公共函数，再用 `del` 抹掉 `_signal_api`/`_delegators` 等脚手架（这些名字因「子模块导入副作用」而存在于包对象上）。
- [`filter_design.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/filter_design.py) 等 10 个**弃用 stub** 是「壳模块」：无实现，靠模块级 `__getattr__` 调用 [`_sub_module_deprecation`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/deprecation.py#L15-L78) 发 `DeprecationWarning` 并从私有实现取回函数，计划 v2.0.0 移除。
- 因此默认情况下 `scipy.signal.butter is scipy.signal._filter_design.butter` 为真；但官方推荐用扁平路径，以获得正确装饰版本、避免依赖将移除的私有模块、且不触发警告。

## 7. 下一步学习建议

- 本讲只解剖了「导出链路」这条骨架，尚未进入任何具体算法。下一站建议进入 **u2（信号生成与窗函数）**，从最容易上手的 [`_waveforms.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py) 与 `windows` 子包开始，结合本讲学到的「按命名空间定位真实实现」的方法去读源码。
- 若你对「多后端委托」这条线更感兴趣，可跳读 **u10-l1 / u10-l2**，深入 [`_delegators.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_delegators.py) 的签名委托器与 `xp_capabilities` 能力矩阵。
- 想立刻动手验证本讲结论，可重做 4.1.4 与 4.4.4 的内省脚本，并尝试设置 `SCIPY_ARRAY_API=1` 观察第 2 层包装行为的变化。

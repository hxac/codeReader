# 四层架构与公开 API 装配链

## 1. 本讲目标

前两讲你已经知道了「`scipy.ndimage` 能做什么」（u1-l1）以及「它的源码在硬盘上如何分层摆放」（u1-l2）。本讲要把这两讲缝合起来，回答一个更本质的工程问题：

> 当你写下 `from scipy import ndimage; ndimage.gaussian_filter(a, 1)` 时，这个名字 `gaussian_filter` 究竟是从哪里「长」出来的？它穿过了哪几层代码才最终落到 C 内核上？

读完本讲，你应该能够：

- 画出 `scipy.ndimage` 自底向上的**四层架构**：C 核心 → Python 实现（`_*.py`）→ 后端委托（`_support_alternative_backends.py` / `_delegators.py`）→ 公开命名空间（`__init__.py`）。
- 说清楚 [`_ndimage_api.py`](_ndimage_api.py) 是如何用 5 行 `from ._xxx import *` 把「裸 API」聚合到一处的。
- 说清楚 [`_support_alternative_backends.py`](_support_alternative_backends.py) 里的 `delegate_xp` 装饰循环，是如何给每一个函数套上「CuPy/JAX 后端委托」外壳的。
- 理解 [`__init__.py`](__init__.py) 如何用一行 `from ._support_alternative_backends import *` 完成最终导出，以及为什么还要保留 `filters`/`morphology` 等「弃用子模块」。
- 判断当环境变量 `SCIPY_ARRAY_API` 关闭时，调用链里**跳过了哪一层**。

本讲不展开任何具体算法（高斯核怎么算、连通标记怎么做），只讲「装配」这件事本身。

## 2. 前置知识

- **Python 的 `from package import *` 与 `__all__`**：当一个模块定义了 `__all__`，`import *` 就只会导入 `__all__` 里列出的名字；若没有 `__all__`，则导入所有不以下划线开头的名字。本讲的整个装配链都建立在这个机制上。
- **「装饰器」是干什么的**：装饰器本质是「接收一个函数、返回一个新函数」的函数。本讲里 `delegate_xp` 就是一个装饰器工厂，它把原始函数包一层「先判断数组来自哪个后端」的逻辑。
- **数组 API（Array API）后端的直觉**：NumPy、CuPy、JAX 都能造多维数组，但它们是不同的类。`scipy.ndimage` 默认处理 NumPy 数组；当传入 CuPy 数组时，理想做法是把调用「转交」给 `cupyx.scipy.ndimage` 里同名函数（在 GPU 上跑），而不是强转回 CPU 上的 NumPy。这一层「转交」逻辑就是本讲的第三层。
- **环境变量作为开关**：SciPy 用一个名为 `SCIPY_ARRAY_API` 的环境变量（设为 `1` 即开启）来控制是否启用上面这套「数组 API 后端委托」。默认是关闭的。

如果某个点不熟也没关系，本讲会边讲源码边解释。

## 3. 本讲源码地图

本讲沿着「装配链」自底向上精读 4 个核心文件，并顺带引用 2 个佐证文件：

| 文件 | 所属层 | 作用 | 本讲如何使用 |
| --- | --- | --- | --- |
| [`__init__.py`](__init__.py) | 第 4 层·公开命名空间 | 顶部 docstring 分组列 API；底部 import 完成最终导出，并保留弃用子模块。 | 精读 L153–L174。 |
| [`_support_alternative_backends.py`](_support_alternative_backends.py) | 第 3 层·后端委托 | 用 `delegate_xp` 给每个裸函数套上 CuPy/JAX 委托外壳，导出 `__all__`。 | 本讲最核心的一节，精读 `delegate_xp` 与装饰循环。 |
| [`_delegators.py`](_delegators.py) | 第 3 层·后端委托（辅助） | 为每个函数写一个 `*_signature` 函数，声明「哪些参数是数组」。 | 解释委托层如何判断后端。 |
| [`_ndimage_api.py`](_ndimage_api.py) | 第 2 层·裸 API 聚合 | 用 5 行 `import *` 把五个实现模块的公开函数汇到一处。 | 精读全文（仅 17 行）。 |
| [`_filters.py`](_filters.py) | 第 1 层·Python 实现（佐证） | 五个实现模块之一，定义 `gaussian_filter` 等，并调用 C 内核 `_nd_image`。 | 用 `gaussian_filter` 调用链作为贯穿示例。 |
| `src/_nd_image`（C 扩展） | 第 0 层·C 核心（佐证） | 真正的数值内核，编译成 `_nd_image.*.so`。 | 仅作为调用链终点提及，详见 u6。 |

> 提示：`src/` 下的 C 内核属于第 0 层，本讲只把它当作「终点」提及，不深入；它的内部机制留给单元 6 讲。

## 4. 核心概念与源码讲解

### 4.1 四层架构总览：从 C 内核到公开命名空间

#### 4.1.1 概念说明

`scipy.ndimage` 的公开 API 不是在一个文件里写死的，而是被刻意拆成**四层**，自底向上逐层「装配」。这是一种典型的「关注点分离（separation of concerns）」设计：

| 层 | 代表文件 | 关注点 | 是否私有 |
| --- | --- | --- | --- |
| 第 0 层·C 核心 | `src/ni_*.c`、编译产物 `_nd_image` | 数值性能（迭代器、行缓冲、样条） | 私有（C 扩展） |
| 第 1 层·Python 实现 | `_filters.py` 等 5 个 `_*.py` | 参数校验、边界模式编码、调度 C 内核 | 私有 |
| 第 2 层·裸 API 聚合 | `_ndimage_api.py` | 把 5 个实现模块的公开函数汇成一张名单 | 私有 |
| 第 3 层·后端委托 | `_support_alternative_backends.py` + `_delegators.py` | 给每个函数套 CuPy/JAX 委托外壳 | 私有 |
| 第 4 层·公开命名空间 | `__init__.py` | 对外暴露 `scipy.ndimage.xxx` | **公开** |

为什么要有第 2、3 层这种「看似多余」的中间层？

- **第 2 层（`_ndimage_api.py`）** 的存在，是因为第 3 层需要「一个干净、不带委托外壳的函数清单」作为输入。如果直接在 5 个实现模块上装饰，就得在 5 个文件里各写一遍装饰逻辑；现在把它集中到 `_ndimage_api` 这个「名单」上，装饰逻辑只写一遍。
- **第 3 层** 的存在，是因为 CuPy/JAX 后端支持是「可选能力」，不应耦合到算法实现里。把它抽成一层后，关闭 `SCIPY_ARRAY_API` 时可以整层跳过，回到纯 NumPy 行为。

#### 4.1.2 核心流程：一次调用穿过四层

以 `ndimage.gaussian_filter(a, 1)`（`a` 是 NumPy 数组）为例，调用自顶向下穿过四层：

```text
你的代码:  ndimage.gaussian_filter(a, 1)
              │  名字来自 __init__.py 的 from ._support_alternative_backends import *
              ▼
第 4 层  __init__.py            只负责「重新导出」，本身无逻辑
              │  实际对象是第 3 层装饰后的函数
              ▼
第 3 层  _support_alternative_backends.py
              │  delegate_xp 包装：先用 *_signature 判定 a 的数组命名空间
              │  NumPy → 不委托，直接调用「裸函数」
              ▼
第 1 层  _filters.gaussian_filter   (经第 2 层 _ndimage_api 透传)
              │  参数校验 → 生成核 → 沿各轴调用 correlate1d
              ▼
第 0 层  _nd_image.correlate1d(...)  C 内核完成真正的滑动求和
              │
              ▼
           返回 NumPy 数组
```

注意：**第 2 层 `_ndimage_api` 在运行时几乎是「透明」的**——它只是把名字集中，真正的函数对象仍指向第 1 层的实现。所以图里把它画成「透传」。

#### 4.1.3 源码精读

入口文件 [`__init__.py:153-162`](__init__.py#L153-L162) 用几行就把上面整条链拉了起来：

```python
# bring in the public functionality from private namespaces

# mypy: ignore-errors

from ._support_alternative_backends import *    # L157：从第 3 层一把导入全部公开函数

# adjust __all__ and do not leak implementation details
from . import _support_alternative_backends      # L160
__all__ = _support_alternative_backends.__all__  # L161：__all__ 也直接复用第 3 层的
del _support_alternative_backends, _ndimage_api, _delegators  # L162：删掉模块对象，防止 scipy.ndimage._xxx 泄漏
```

这段代码说明三件事：

1. **L157**：公开 API 的唯一来源是 `_support_alternative_backends`（第 3 层）。`__init__.py` 自己不实现任何函数。
2. **L161**：连 `scipy.ndimage.__all__` 都是「借来的」，直接等于第 3 层的 `__all__`，而后者又等于第 2 层的 `__all__`（见 4.3.3）。可见这份「名单」是单一事实来源（single source of truth）。
3. **L162**：导入完后立刻 `del` 掉 `_support_alternative_backends` 等模块对象，这是为了**不把私有模块名泄漏到公开命名空间**（否则用户就能看到 `scipy.ndimage._filters`，破坏封装）。注释里的 `# noqa: F821` 是因为被 `del` 的 `_ndimage_api`、`_delegators` 并未在本文件显式 `import`（它们是经 `import *` 顺带进来的），静态检查器会误报「未定义名字」。

#### 4.1.4 代码实践

**实践目标**：用 Python 内省验证「公开 API 的函数对象确实来自私有模块」。

**操作步骤**：

```python
# 示例代码
import scipy.ndimage as ndi

print(type(ndi.gaussian_filter))                 # 应为 function
print(ndi.gaussian_filter.__module__)            # 看它声明自己属于哪个模块
print(ndi.__all__[:5], "...", "(共", len(ndi.__all__), "个)")
print(hasattr(ndi, "_filters"))                  # 预期 False：被 del 了
print(hasattr(ndi, "filters"))                   # 预期 True：弃用桩仍保留
```

**需要观察的现象**：

- `__module__` 应指向类似 `scipy.ndimage._filters`，证明函数体在第 1 层实现（即使你通过第 4 层访问它）。
- `_filters`（带下划线）应不可见，`filters`（不带下划线）可见。

**预期结果**：`hasattr(ndi, "_filters")` 为 `False`，呼应 L162 的 `del`；`hasattr(ndi, "filters")` 为 `True`，呼应 L166–L170 保留的弃用桩。具体 `__module__` 字符串：待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `__init__.py` 里 L157 的 `from ._support_alternative_backends import *` 删掉，`scipy.ndimage.gaussian_filter` 还能访问吗？

**答案**：不能。`__init__.py` 自身不定义任何函数，公开名字全部来自这一行导入；删掉后 `gaussian_filter` 不会出现在 `scipy.ndimage` 命名空间（但 `filters`/`morphology` 等弃用子模块仍能通过 `__getattr__` 懒加载，见 4.4）。

**练习 2**：为什么 L162 要 `del _support_alternative_backends`，却不 `del` 掉 `filters`、`morphology`？

**答案**：前者是实现细节，泄漏它会破坏封装；后者（弃用桩）是**有意保留**的兼容入口，目的是让老代码 `scipy.ndimage.filters.gaussian_filter` 仍能跑（并触发弃用警告），所以不能删。

---

### 4.2 第 2 层：_ndimage_api.py 如何聚合「裸 API」

#### 4.2.1 概念说明

第 1 层有 5 个实现模块（`_filters.py`、`_fourier.py`、`_interpolation.py`、`_measurements.py`、`_morphology.py`），各自定义了一堆公开函数、各自维护自己的 `__all__`。第 3 层在装饰时，希望面对的是**一张统一的函数清单**，而不是去翻 5 个文件。

[`_ndimage_api.py`](_ndimage_api.py) 就是这个「清单收集器」。它的全部职责只有两个：

1. 用 5 行 `from ._xxx import *` 把 5 个模块的公开函数「抄」到一个命名空间里。
2. 用一行列表推导生成「合并后的 `__all__`」。

它本身**不含任何算法逻辑**，所以文件只有 17 行。模块 docstring 也明确强调了它的「私有不公开」定位。

#### 4.2.2 核心流程：用 dir() 自动汇总名单

「抄函数」用 `import *`，但要生成合并 `__all__` 时有个小技巧：不需要手工把 5 个模块的 `__all__` 拼起来，而是利用一个事实——执行完 5 行 `import *` 后，本模块的命名空间里**刚好**就是所有被导入的公开名字。于是用内置 `dir()` 把当前模块的所有名字列出来，过滤掉以下划线开头的私有名（以及一个带 `@` 的特殊情况），剩下的就是公开 API。

```text
_filters.__all__  +  _fourier.__all__  + ... + _morphology.__all__
        │  via 5× `from ._xxx import *`
        ▼
_ndimage_api 的局部命名空间 = 全部公开函数
        │  dir() → 过滤掉 _ / @ 开头
        ▼
_ndimage_api.__all__  （单一事实来源的「裸 API 名单」）
```

#### 4.2.3 源码精读

整个 [`_ndimage_api.py:1-17`](_ndimage_api.py#L1-L17) 值得完整看一遍：

```python
"""This is the 'bare' ndimage API.

This --- private! --- module only collects implementations of public ndimage API
for _support_alternative_backends.
The latter --- also private! --- module adds delegation to CuPy etc and
re-exports decorated names to __init__.py
"""

from ._filters import *    # noqa: F403   L9
from ._fourier import *   # noqa: F403    L10
from ._interpolation import *   # noqa: F403   L11
from ._measurements import *   # noqa: F403    L12
from ._morphology import *   # noqa: F403      L13

# '@' due to pytest bug, scipy/scipy#22236
__all__: list[str] = [s for s in dir() if not s.startswith(('_', '@'))]   # L16
```

要点：

- **L9–L13**：5 行 `import *`。每个实现模块都定义了自己的 `__all__`（例如 [`_filters.py:45-47`](_filters.py#L45-L47) 里 `__all__ = ['correlate1d', 'convolve1d', 'gaussian_filter1d', 'gaussian_filter', ...]`），所以 `import *` 只会把这些列出的名字「抄」过来，不会误带进私有辅助函数。
- **L16**：`dir()` 返回当前模块命名空间里所有名字（字符串列表），列表推导式筛掉以 `_` 或 `@` 开头的，剩下的就是合并后的 `__all__`。
- **L15 的 `@` 注释**：之所以额外排除以 `@` 开头的名字，是为了规避一个 pytest 的 bug（SciPy issue #22236），属于防御性写法，与正常 API 无关。
- docstring 把本模块（bare API 收集器）和下游的 `_support_alternative_backends`（加委托、再导出给 `__init__`）的分工讲得很清楚，是理解整条链的最佳注释。

#### 4.2.4 代码实践

**实践目标**：直接观察「裸 API 名单」的来源与构成。

**操作步骤**：

```python
# 示例代码
from scipy.ndimage import _ndimage_api   # 私有模块，仅用于学习观察

print("裸 API 函数总数：", len(_ndimage_api.__all__))
print("是否含 gaussian_filter：", "gaussian_filter" in _ndimage_api.__all__)
print("是否含 _get_output（私有）：", any(s.startswith("_") for s in _ndimage_api.__all__))

# 对照：_filters 模块自己的 __all__ 应是 _ndimage_api.__all__ 的子集
from scipy.ndimage import _filters
print("_filters.__all__ 是否全部在裸 API 中：",
      set(_filters.__all__).issubset(set(_ndimage_api.__all__)))
```

**需要观察的现象**：`_ndimage_api.__all__` 应有约 80+ 个名字（五个模块合并）；不含任何下划线开头的私有名；`_filters.__all__` 是它的子集。

**预期结果**：最后一个断言应为 `True`，证明 `_ndimage_api.__all__` 确实是各模块 `__all__` 的并集。精确函数总数：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接在 `_support_alternative_backends.py` 里写 `from ._filters import *` 等 5 行，而要单独搞一个 `_ndimage_api.py`？

**答案**：为了「职责分离」与「避免循环」。`_support_alternative_backends` 需要 (a) 一份干净的函数清单用来装饰，(b) 一个稳定的 `__all__` 来源。把这个「清单」固化成独立模块后，第 3 层只需 `from ._ndimage_api import *` 一行就能拿到全部函数，逻辑更清晰；同时其它工具（如文档生成、类型检查）也能单独引用这份「裸 API」。

**练习 2**：如果有人往 `_filters.py` 里新增了一个公开函数 `foo` 并加进 `_filters.__all__`，但忘了在 `__init__.py` 做任何事，`ndimage.foo` 会自动可用吗？

**答案**：会。因为整条链是自动的：`_filters.__all__` 多了 `foo` → `import *` 把它带进 `_ndimage_api` → `_ndimage_api.__all__` 经 `dir()` 自动包含 `foo` → 第 3 层装饰循环遍历 `__all__` 时自动装饰并 `vars()[func_name] = f` → `__init__` 的 `import *` 自动导出。这正是「单一事实来源」设计带来的好处：只需在最底层登记一次。

---

### 4.3 第 3 层核心：delegate_xp 装饰循环

#### 4.3.1 概念说明

第 3 层是整条装配链里**逻辑最重**的一层，由两个文件配合完成：

- [`_support_alternative_backends.py`](_support_alternative_backends.py)：主文件。定义装饰器 `delegate_xp`、能力表 `capabilities_dict`、黑名单 `CUPY_BLOCKLIST`，并用一个 `for` 循环把「裸 API」里的每个函数都装饰一遍。
- [`_delegators.py`](_delegators.py)：辅助文件。为每个公开函数写一个 `<func>_signature` 函数，唯一职责是「告诉我调用时哪些参数是数组」。

**「委托（delegation）」要解决的问题**：当用户传进来一个 CuPy 数组（住在 GPU 显存），如果还走 SciPy 默认的 NumPy 实现，就得把数据从 GPU 搬到 CPU、算完再搬回去，既慢又费显存。更好的做法是：识别出「这是 CuPy 数组」，然后把整个调用**转交**给 `cupyx.scipy.ndimage` 里同名函数（它原生支持 GPU）。`delegate_xp` 就是做这件事的「前台接待员」。

**`<func>_signature` 的作用**：要判断「这次调用用了什么后端」，就得知道哪些参数是数组（标量参数如 `sigma=1` 不携带后端信息）。每个 `*_signature` 函数的形参与对应公开函数**完全一致**，函数体只是把「数组类的参数」收集起来交给 `array_namespace(...)`，由后者判定命名空间。

#### 4.3.2 核心流程：装饰循环与运行时分派

装配时（模块导入时执行一次）：

```text
for func_name in _ndimage_api.__all__:           # 遍历裸 API 名单
    bare_func = getattr(_ndimage_api, func_name)  # 取出第 1 层的原始函数
    delegator = getattr(_delegators, func_name + "_signature")  # 取对应的 signature
    capabilities = capabilities_dict.get(func_name, default)     # 查能力表
    f = capabilities(
        delegate_xp(delegator, MODULE_NAME)(bare_func)   # 装饰（套委托外壳）
        if SCIPY_ARRAY_API else bare_func                # 开关：关闭则不套外壳
    )
    vars()[func_name] = f                        # 注入到本模块命名空间
```

运行时（每次调用 `ndimage.gaussian_filter(a, 1)`）：

```text
wrapper(*args, **kwds)
   │
   │  xp = delegator(*args, **kwds)        # 用 *_signature 判定数组命名空间
   ▼
   is_cupy(xp)?  且不在 CUPY_BLOCKLIST?
       ├─ 是 → import cupyx.scipy.ndimage，取同名函数，直接调用并返回（GPU 上算）
       │
   is_jax(xp)?  且函数名 == "map_coordinates"?
       ├─ 是 → 转交给 jax.scipy 的同名函数
       │
   否则（NumPy，或被 blocklist / 能力限制）
       └─ 调用原始 bare_func（走第 1 层 + 第 0 层），结果按需转回 xp 数组
```

关键判断点：是否真的「转交」，取决于传入数组的命名空间 + 当前函数是否在黑名单/能力表里被特别处理。

#### 4.3.3 源码精读

**(a) 文件头：把裸 API 与名单接进来**

[`_support_alternative_backends.py:1-13`](_support_alternative_backends.py#L1-L13)：

```python
import functools
from scipy._lib._array_api import (
    is_cupy, is_jax, scipy_namespace_for, SCIPY_ARRAY_API, xp_capabilities
)
import numpy as np
from ._ndimage_api import *   # noqa: F403    L7：把裸函数抄进来
from . import _ndimage_api                                  # L8：同时保留模块对象供循环用
from . import _delegators                                    # L9：取 *_signature
__all__ = _ndimage_api.__all__                               # L10：名单直接复用第 2 层
MODULE_NAME = 'ndimage'                                      # L13：委托目标子包名
```

注意 L10：第 3 层的 `__all__` 直接等于第 2 层的 `__all__`，名字清单在两层间原样传递，没有增删。

**(b) CuPy 黑名单**

[`_support_alternative_backends.py:28-34`](_support_alternative_backends.py#L28-L34)：

```python
# Some cupyx.scipy.ndimage functions don't exist or are incompatible with
# their SciPy counterparts
CUPY_BLOCKLIST = [
    'distance_transform_bf',
    'distance_transform_cdt',
    'find_objects',
    'geometric_transform',
    'vectorized_filter',
]
```

这 5 个函数即便传入 CuPy 数组也**不转交**给 cupyx（要么 cupyx 没有，要么行为不兼容），只能回退到 NumPy 实现。

**(c) 装饰器 delegate_xp**

[`_support_alternative_backends.py:37-79`](_support_alternative_backends.py#L37-L79)，核心分派逻辑在 L40–L58：

```python
def delegate_xp(delegator, module_name):
    def inner(func):
        @functools.wraps(func)
        def wrapper(*args, **kwds):
            xp = delegator(*args, **kwds)                       # 判定数组命名空间

            # try delegating to a cupyx/jax namesake
            if is_cupy(xp) and func.__name__ not in CUPY_BLOCKLIST:        # L44
                import importlib
                cupyx_module = importlib.import_module(f"cupyx.scipy.{module_name}")
                cupyx_func = getattr(cupyx_module, func.__name__)
                return cupyx_func(*args, **kwds)                # 转交 GPU 实现
            elif is_jax(xp) and func.__name__ == "map_coordinates":        # L50
                spx = scipy_namespace_for(xp)
                jax_module = getattr(spx, module_name)
                jax_func = getattr(jax_module, func.__name__)
                return jax_func(*args, **kwds)
            else:                                               # L55：回退到裸函数
                result = func(*args, **kwds)                    # 第 1 层 + 第 0 层
                if isinstance(result, np.ndarray | np.generic):
                    return xp.asarray(result)                   # 把结果转回 xp 命名空间
                ...
        return wrapper
    return inner
```

要点：

- **`@functools.wraps(func)`**：让包装后的 `wrapper` 仍保留原始函数的 `__name__`、`__doc__`，对用户透明。
- **L44**：只有「是 CuPy 数组」**且**「函数不在黑名单」才转交 cupyx。
- **L50**：JAX 路径目前**只对 `map_coordinates` 一个函数**开通（其余 JAX 数组回退走 NumPy 实现后转回）。
- **L55–L58**：回退分支里，原始函数返回 NumPy 数组后，再用 `xp.asarray(result)` 转回调用方的命名空间（保证「进 CuPy 出 CuPy」的契约）。

**(d) 能力表 capabilities_dict**

[`_support_alternative_backends.py:81-107`](_support_alternative_backends.py#L81-L107) 声明了每个函数对「非 NumPy 后端」的支持能力，例如 `map_coordinates` 标注了 `exceptions=["cupy", "jax.numpy"]`、`generate_binary_structure` 标注了 `out_of_scope=True`（永远只返回 NumPy）。这张表主要供测试框架（`xp_capabilities` 装饰器会据此生成 `pytest.mark.xfail_xp_backends` 标记）和能力文档使用，详见 u7-l1。

**(e) 装饰循环**

[`_support_alternative_backends.py:109-122`](_support_alternative_backends.py#L109-L122)：

```python
# ### decorate ###
for func_name in _ndimage_api.__all__:                         # L110
    bare_func = getattr(_ndimage_api, func_name)               # L111
    delegator = getattr(_delegators, func_name + "_signature") # L112
    capabilities = capabilities_dict.get(func_name, default_capabilities)  # L114
    f = capabilities(
        delegate_xp(delegator, MODULE_NAME)(bare_func)         # L118：套委托外壳
        if SCIPY_ARRAY_API else bare_func                      # L119：开关
    )
    vars()[func_name] = f                                      # L122：注入命名空间
```

**最关键的一行是 L119**：`if SCIPY_ARRAY_API else bare_func`。这决定了「后端委托层是否生效」：

- `SCIPY_ARRAY_API` 为真（环境变量 `SCIPY_ARRAY_API=1`）：函数被 `delegate_xp` 外壳包住，运行时会按数组后端分派。
- `SCIPY_ARRAY_API` 为假（**默认**）：直接用 `bare_func`，**完全跳过委托外壳**，调用直达第 1 层 NumPy 实现。这就是 4.1.2 里说的「关闭时跳过了哪一层」——跳过的正是 `delegate_xp` 这一层委托/分派逻辑。

> 注：无论开关如何，函数对象都仍从 `_support_alternative_backends` 命名空间导出（L122 的 `vars()[func_name] = f`），`__init__.py` 也照常导入它。开关影响的只是「这个对象有没有被套委托外壳」。

**(f) delegator signature 举例**

`_delegators.py` 里每个 `*_signature` 函数形参与公开函数一致，函数体只做一件事：把数组类参数喂给 `array_namespace`。例如 [`_delegators.py:125-126`](_delegators.py#L125-L126)：

```python
def gaussian_filter_signature(input, sigma, order=0, output=None, *args, **kwds):
    return array_namespace(input, _skip_if_dtype(output))
```

它声明：`gaussian_filter` 调用里，`input` 是数组、`output` 也可能是数组（用 `_skip_if_dtype` 排除「`output` 被传成 dtype」的情况），而 `sigma`、`order` 是标量、不参与后端判定。特殊地，[`_delegators.py:141-143`](_delegators.py#L141-L143) 的 `generate_binary_structure_signature` 因为**没有数组参数**，直接返回 `np`：

```python
def generate_binary_structure_signature(rank, connectivity):
    # XXX: no input arrays; always return numpy
    return np
```

`_delegators.py` 顶部 docstring（[`_delegators.py:1-25`](_delegators.py#L1-L25)）还说明了这些 signature 是怎么来的：先用 `inspect.signature` 自动生成骨架，再**人工**对照文档标注「哪些参数是数组」。

#### 4.3.4 代码实践

**实践目标**：体会「同一个名字，在 `SCIPY_ARRAY_API` 开关不同时，是不同的函数对象」。

**操作步骤**：

```python
# 示例代码（在一个普通 Python 会话里，默认 SCIPY_ARRAY_API 关闭）
import os
import scipy.ndimage as ndi

# 默认：SCIPY_ARRAY_API 未设
gf = ndi.gaussian_filter
print("默认 wrapped?", hasattr(gf, "__wrapped__"))   # 是否被 functools.wraps 包装过

# 看 _support_alternative_backends 里的「装饰后」对象是否带了委托外壳
from scipy.ndimage import _support_alternative_backends as sab
gf2 = getattr(sab, "gaussian_filter", None)
print("sab.gaussian_filter 是否有 __wrapped__:", getattr(gf2, "__wrapped__", None) is not None)
```

**需要观察的现象**：在默认（关闭）状态下，`gaussian_filter` **不应**有 `__wrapped__` 属性——因为 L119 走的是 `bare_func` 分支，没套 `delegate_xp`。

**进阶（可选）**：在 shell 里设 `SCIPY_ARRAY_API=1` 再启动一个新 Python 进程重复上述检查，此时 `__wrapped__` 应为真（函数被套了委托外壳）。

**预期结果**：默认状态下 `hasattr(gf, "__wrapped__")` 为 `False`；开启后为 `True`。完整对照：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：假设你给 `gaussian_filter` 传一个 CuPy 数组，但 `SCIPY_ARRAY_API` 没开，会发生什么？

**答案**：因为 L119 走 `bare_func` 分支，函数没有委托外壳，不会转交给 `cupyx`。调用会进入第 1 层 `gaussian_filter`，其第一行 `input = np.asarray(input)`（见 [`_filters.py:843`](_filters.py#L843)）会把 CuPy 数组搬到 CPU 变成 NumPy 数组再计算——也就是「能算出结果，但丧失了 GPU 加速」。

**练习 2**：`map_coordinates` 在 JAX 数组下的转交，与 `gaussian_filter` 在 CuPy 数组下的转交，为什么代码里写得不一样（一个是 `is_jax and func.__name__ == "map_coordinates"`，一个是 `is_cupy and not in BLOCKLIST`）？

**答案**：两条后端的覆盖范围不同。CuPy 路径覆盖了**几乎所有** ndimage 函数（仅排除黑名单 5 个），所以用「黑名单」写法；JAX 路径目前**只**对 `map_coordinates` 开通（其余 JAX 函数回退走 NumPy 再转回），所以用「白名单（精确匹配单个函数名）」写法。两种写法分别匹配各自后端的实际支持现状。

---

### 4.4 顶层：__all__ 导出与弃用子模块

#### 4.4.1 概念说明

第 4 层 [`__init__.py`](__init__.py) 的职责很轻，但有两个细节容易踩坑，单独成节讲清楚：

1. **`__all__` 的导出与私有名的清理**：公开 API 的名单层层透传，最终在 `__init__` 落地；同时要 `del` 掉私有模块对象，避免泄漏。
2. **弃用子模块（deprecated namespaces）**：历史上 `scipy.ndimage` 下有 `filters`、`fourier`、`interpolation`、`measurements`、`morphology` 这些**不带下划线**的同名子模块（例如老代码常用 `scipy.ndimage.filters.gaussian_filter`）。它们已弃用、计划在 SciPy v2.0.0 移除，但目前仍以「桩文件（stub）」形式保留，靠 `__getattr__` 懒加载并发出弃用警告。

理解这两点，你就能解释「为什么 `scipy.ndimage.filters` 能访问，但 `scipy.ndimage._filters` 不该用」。

#### 4.4.2 核心流程：导出 + 清理 + 保留弃用桩

```text
__init__.py 执行顺序：
  ① from ._support_alternative_backends import *   → 公开函数进入命名空间
  ② __all__ = _support_alternative_backends.__all__ → 名单透传
  ③ del _support_alternative_backends, _ndimage_api, _delegators → 清理私有模块对象
  ④ from . import filters / fourier / interpolation / measurements / morphology
                                                    → 显式保留弃用桩子模块（触发警告）
  ⑤ test = PytestTester(__name__)                  → 提供 ndimage.test() 入口
```

其中 ④ 的「弃用桩」机制：每个不带下划线的桩文件（如 [`filters.py`](filters.py)）本身不实现任何函数，而是在被访问属性时（通过 `__getattr__`）调用 SciPy 的 `_sub_module_deprecation` 辅助函数，发出 `DeprecationWarning` 并把请求转发到真正的私有实现 `_filters`。

#### 4.4.3 源码精读

**(a) 导出与清理**（已在 4.1.3 精读过 [`__init__.py:157-162`](__init__.py#L157-L162)，此处补充 ④⑤）。

[`__init__.py:165-174`](__init__.py#L165-L174)：

```python
# Deprecated namespaces, to be removed in v2.0.0
from . import filters           # L166
from . import fourier           # L167
from . import interpolation     # L168
from . import measurements      # L169
from . import morphology        # L170

from scipy._lib._testutils import PytestTester   # L172
test = PytestTester(__name__)                    # L173
del PytestTester                                 # L174
```

- L165 注释明确写了「v2.0.0 移除」，呼应 u1-l2 里讲过的弃用桩移除计划。
- L172–L174 是 SciPy 各子包的通用约定：给 `scipy.ndimage.test()` 提供一个跑本子包测试的快捷入口，然后 `del` 掉 `PytestTester` 类本身避免泄漏。

**(b) 弃用桩的工作方式**

以 [`filters.py:1-27`](filters.py#L1-L27) 为例（其它四个桩文件结构完全一致）：

```python
# This file is not meant for public use and will be removed in SciPy v2.0.0.
# Use the `scipy.ndimage` namespace for importing the functions
# included below.

from scipy._lib.deprecation import _sub_module_deprecation

__all__ = [  # noqa: F822
    'correlate1d', 'convolve1d', 'gaussian_filter1d',
    'gaussian_filter', ...                       # 仅列名字，供 IDE/文档参考
]

def __dir__():
    return __all__

def __getattr__(name):                           # 懒加载：访问任意属性时触发
    return _sub_module_deprecation(
        sub_package='ndimage', module='filters',
        private_modules=['_filters'], all=__all__,
        attribute=name,
    )
```

要点：

- 桩文件**不 import 任何实现**，只在被访问属性时（`__getattr__`）才动态解析，因此不会拖慢 `import scipy.ndimage`。
- `_sub_module_deprecation` 会发出弃用警告，并把 `name`（如 `gaussian_filter`）从私有实现 `_filters` 取出返回。所以 `scipy.ndimage.filters.gaussian_filter` 仍能跑，但会警告你改用 `scipy.ndimage.gaussian_filter`。
- 同理 [`morphology.py:1-27`](morphology.py#L1-L27) 是 morphology 域的弃用桩。

#### 4.4.4 代码实践

**实践目标**：亲见「弃用桩」发出警告，并确认它最终取到的函数与公开命名空间是同一个。

**操作步骤**：

```python
# 示例代码
import warnings
import scipy.ndimage as ndi

# 1) 触发弃用桩
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    f = ndi.filters.gaussian_filter      # 走弃用桩 __getattr__
    print("收到警告数：", len(w))
    print("是否 DeprecationWarning：",
          any(issubclass(x.category, DeprecationWarning) for x in w))

# 2) 桩取到的函数 vs 公开命名空间的函数
print("是同一个对象吗：", f is ndi.gaussian_filter)
```

**需要观察的现象**：访问 `ndi.filters.gaussian_filter` 应触发至少一条 `DeprecationWarning`；且取到的函数对象应与 `ndi.gaussian_filter` 是同一个。

**预期结果**：`f is ndi.gaussian_filter` 为 `True`，证明弃用桩只是「带警告的转发」，最终拿到的还是第 1 层（经第 3 层装饰）的同一个函数。具体警告文案：待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：既然 `__init__.py` L162 已经把 `_support_alternative_backends` 等 `del` 掉了，为什么 `import scipy.ndimage` 不报错说 `filters` 找不到（L166 的 `from . import filters`）？

**答案**：`del` 的是 `_support_alternative_backends`、`_ndimage_api`、`_delegators` 这三个**带下划线的私有模块对象**，目的是不泄漏私有实现；而 `filters`、`morphology` 等**不带下划线的桩文件**是真实存在的 `.py` 文件（见 u1-l2 目录结构），`from . import filters` 是正常的子模块导入，不受前面 `del` 影响。

**练习 2**：用户代码里 `from scipy.ndimage.filters import gaussian_filter` 还能用多久？

**答案**：在 SciPy v2.0.0 之前都能用（会发 `DeprecationWarning`），v2.0.0 会随 [`__init__.py:165-170`](__init__.py#L165-L170) 和桩文件一起移除。新代码应一律写 `from scipy.ndimage import gaussian_filter`。

---

## 5. 综合实践

把本讲四层架构串起来，完成下面这个**调用链追踪**任务（本讲的主实践，对应规格里的 practice_task）。

**任务**：以 `ndimage.gaussian_filter(a, 1)`（`a` 为 NumPy 数组）为例，画出从 `__init__` 到 C 内核 `_nd_image` 的完整调用链，标注**每一层所在的文件与关键行号**，并指出 `SCIPY_ARRAY_API` 关闭时跳过了哪一层。

**操作步骤**：

1. **准备输入**：

   ```python
   # 示例代码
   import numpy as np
   import scipy.ndimage as ndi
   a = np.arange(50, step=2).reshape((5, 5))
   ```

2. **逐层标注调用链**（请你自己填入对应文件/行号，参考答案见下）：

   | 层 | 文件 | 关键代码（行号） | 发生了什么 |
   | --- | --- | --- | --- |
   | 第 4 层 | [`__init__.py:157`](__init__.py#L157) | `from ._support_alternative_backends import *` | 名字 `gaussian_filter` 来自这里 |
   | 第 3 层 | [`_support_alternative_backends.py:109-122`](_support_alternative_backends.py#L109-L122) | 装饰循环 / `delegate_xp` | **SCIPY_ARRAY_API 关闭时跳过此前缀委托外壳** |
   | 第 1 层 | [`_filters.py:758-861`](_filters.py#L758-L861) | `def gaussian_filter(...)` | 参数校验、生成核、沿各轴循环 |
   | 第 1 层（内层） | [`_filters.py:855-857`](_filters.py#L855-L857) | `gaussian_filter1d(input, ...)` | N-D 分离为多次 1-D 卷积 |
   | 第 1 层（1-D） | [`_filters.py:610-611`](_filters.py#L610-L611) | `_nd_image.correlate1d(...)` | 进入 C 内核 |
   | 第 0 层 | `src/_nd_image`（C 扩展） | `Py_Correlate1D` | 真正的滑动加权求和 |

3. **验证关键跳过点**：

   ```python
   # 示例代码
   import scipy.ndimage as ndi
   print("SCIPY_ARRAY_API 关闭时，gaussian_filter 是否被委托外壳包裹：",
         hasattr(ndi.gaussian_filter, "__wrapped__"))   # 预期 False
   ```

**需要观察的现象与预期结果**：

- 第 1 层 `gaussian_filter` 的入口在 [`_filters.py:843-844`](_filters.py#L843-L844)：`input = np.asarray(input)` + `_get_output(...)`。
- 多维高斯被「分离」为沿每个轴各调一次 `gaussian_filter1d`，循环见 [`_filters.py:854-858`](_filters.py#L854-L858)，每次把上一次的 `output` 当作下一次的 `input`（`input = output`）。
- 1-D 路径最终在 [`_filters.py:610-611`](_filters.py#L610-L611) 落到 `_nd_image.correlate1d`，这是进入 C 内核的「最后一跳」。
- **跳过层答案**：当 `SCIPY_ARRAY_API` 关闭（默认）时，第 3 层的 `delegate_xp` 委托外壳被跳过（[`_support_alternative_backends.py:119`](_support_alternative_backends.py#L119) 的 `else bare_func` 分支），调用直接从 `__init__` 进入第 1 层 NumPy 实现，不经任何后端分派。

> 待本地验证：上述所有 `__wrapped__` 判断与「不触发 cupy 转交」的行为，需在本地默认环境（未设 `SCIPY_ARRAY_API`）下确认。

## 6. 本讲小结

- `scipy.ndimage` 的公开 API 由**四层**自底向上装配而成：C 核心（`_nd_image` 等）→ Python 实现（5 个 `_*.py`）→ 后端委托（`_support_alternative_backends.py` + `_delegators.py`）→ 公开命名空间（`__init__.py`）。
- `_ndimage_api.py` 是一个 17 行的「裸 API 聚合器」：用 5 行 `from ._xxx import *` 抄来函数，用 `dir()` 自动汇总出 `__all__`，作为下游装饰的单一事实来源。
- `_support_alternative_backends.py` 用一个 `for` 循环遍历 `__all__`，给每个函数套上 `delegate_xp` 委托外壳（运行时按数组命名空间分派给 CuPy/JAX），并用 `capabilities_dict` 声明每函数的后端能力。
- `delegate_xp` 的运行时分派有三条分支：CuPy（除黑名单 5 个外全部转交）、JAX（仅 `map_coordinates`）、其余回退到裸 NumPy 实现并把结果转回调用方命名空间。
- `__init__.py` 只做「重新导出 + 清理私有名」：`__all__` 直接复用第 3 层，再用 `del` 隐藏私有模块；同时保留 `filters`/`morphology` 等「不带下划线」的弃用桩子模块（v2.0.0 移除）。
- 环境变量 `SCIPY_ARRAY_API` 是委托层的总开关：关闭（默认）时整层 `delegate_xp` 外壳被跳过，函数走纯 NumPy 路径。

## 7. 下一步学习建议

本讲把「装配链」讲完了，接下来的学习可以按两条线推进：

- **横向进入各功能域（推荐先走这条）**：现在你已经知道 `gaussian_filter` 最终会调到 `_nd_image.correlate1d`。单元 2（滤波与傅里叶）会从 [`correlate1d` / `convolve1d`](_filters.py) 讲起，把「第 1 层参数校验 → 第 0 层 C 内核」这条最短链路彻底讲透；随后是单元 3（插值）、单元 4（测量）、单元 5（形态学）。
- **纵向下探 C 内核**：如果你更关心「第 0 层 `_nd_image` 内部怎么迭代、怎么做边界扩展」，可以直接跳到单元 6 的 u6-l1（`_nd_image` 扩展模块与方法分发表）和 u6-l2（C 端迭代器与行缓冲）。
- **后端委托的完整图景**：本讲只讲了第 3 层的「装配」部分，关于 `capabilities_dict` 如何驱动测试矩阵、`SCIPY_ARRAY_API=1` 时 CuPy/JAX 的端到端行为，留到单元 7 的 u7-l1（数组 API / CuPy / JAX 后端委托）展开。

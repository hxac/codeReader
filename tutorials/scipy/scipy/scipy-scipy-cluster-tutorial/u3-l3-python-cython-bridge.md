# Python 与 Cython 的桥接：lazy_cython 与 array_api

## 1. 本讲目标

u3-l2 讲清了 `linkage()` 如何「按方法分派到三个 Cython 后端」，但当时刻意把分派外面那层 `lazy_cython` 装饰器和 `xpx.lazy_apply(...)` 调用当成黑盒，只点到为止。本讲就把这个黑盒打开。学完本讲你应当能够：

- 说清 `lazy_cython` 是什么——它只是 `xp_capabilities(cpu_only=True, reason="Cython code", warnings=[("dask.array", "merges chunks")])` 的一个**复用别名**，并解释它声明的能力约束（CPU-only、对 dask 告警）。
- 解释 `cy_linkage` 这个**内嵌闭包**为什么必须存在：它把「方法分派」封装成一个「接受数据 + `validate` 标志」的标准形态函数，供桥接层统一调用。
- 读懂 `xpx.lazy_apply(cy_linkage, y, validate=lazy, ...)` 的每一个参数，并解释它如何让**同一个 Cython 后端**既能吃普通 NumPy 数组、又能吃 dask 惰性数组。
- 画出 `linkage → lazy_apply → cy_linkage → _hierarchy.*` 的完整调用链，并说清「当输入是普通 NumPy 数组时这条链如何短路直接计算」。

本讲只讲「桥接（bridge）」这一层：装饰器如何声明能力、闭包如何被桥接层调用、惰性输入如何被拆块喂给 Cython。**不**展开三种 Cython 算法的内部实现（见 u4），也**不**展开 Lance-Williams 公式（见 u3-l4）。`xp_capabilities` / `lazy_apply` 的**底层实现**位于 `scipy/_lib/_array_api.py` 与 `array_api_extra`，超出本模块范围，留待 u7-l3 专题；本讲只讲它们的**可观察契约**与本模块的**使用方式**。

## 2. 前置知识

阅读本讲前，你需要已经建立以下认知（均来自前置讲义）：

- **双层架构**：`_hierarchy_impl.py`（纯 Python 封装/校验层）通过 `from . import _hierarchy` 调用 `_hierarchy.pyx` 编译出的 Cython 性能层（见 u1-l2）。
- **`linkage()` 的分派**：`single→mst_single_linkage`、`complete/average/weighted/ward→nn_chain`、`centroid/median→fast_linkage`（见 u3-l2）。
- **xp / array_namespace**：`array_namespace(obs)` 默认（未开 `SCIPY_ARRAY_API` 环境变量时）返回 `numpy`，借此用 `xp.std`/`xp.any` 等跨 NumPy/JAX/Dask 通用（见 u2-l1）。
- **惰性数组**：Dask 等数组把计算组织成一张「任务图」，只在必要时才求值；对惰性数组调用 `isfinite` 会**强制触发整图求值**，违背惰性初衷（见 u2-l1）。
- **`@xp_capabilities(...)` 装饰器**：声明函数对各数组后端的支持情况，并自动往 docstring 追加一张能力表（见 u2-l1、u2-l2）。

本讲会用到的三个新术语：

- **桥接（bridge）**：把「Python 层的、支持多种数组后端的接口」与「Cython 层的、只认连续 NumPy 数组的内核」衔接起来的那一层胶水代码（装饰器 + 闭包 + `lazy_apply`）。
- **eager（即时）数组 / lazy（惰性）数组**：前者（如 `numpy.ndarray`）创建时数据已在内存里；后者（如 `dask.array`）只记录「怎么算」，数据要到 `.compute()` 时才落地。本讲用 `is_lazy_array(y)` 判别。
- **`validate` 标志**：闭包的最后一个参数，由桥接层注入，控制「是否在真正调用 Cython 前补做一次有限性校验」。它是桥接层处理惰性输入的关键旋钮。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，仅在确认后端签名时瞥一眼 `.pyx`：

| 文件 | 作用 |
|------|------|
| `hierarchy/_hierarchy_impl.py` | hierarchy 子模块的纯 Python 实现层。本讲的全部桥接代码都在这里：模块级别名 `lazy_cython`、`linkage()` 及其内部闭包 `cy_linkage`、以及 `xpx.lazy_apply(...)` 调用。 |
| `hierarchy/_hierarchy.pyx` | Cython 性能层。本讲只看三个后端函数 `mst_single_linkage` / `nn_chain` / `fast_linkage` 的**签名**（它们对桥接层暴露的接口），不进入算法实现。 |

涉及的关键符号：

- `lazy_cython`：模块级常量，等价于一组固定的 `xp_capabilities(...)` 能力声明，作为装饰器复用。
- `cy_linkage(y, validate)`：`linkage` 内部的闭包，真正调用 Cython 后端。
- `xpx.lazy_apply`：来自 `array_api_extra` 的桥接函数，让任意「吃 NumPy 数组的函数」也能吃惰性数组。
- `_hierarchy.mst_single_linkage` / `_hierarchy.nn_chain` / `_hierarchy.fast_linkage`：三个 Cython 后端。

## 4. 核心概念与源码讲解

### 4.1 `lazy_cython` 装饰器：`xp_capabilities` 的复用别名

#### 4.1.1 概念说明

`scipy.cluster.hierarchy` 里有十几个函数（`linkage`、七个 `single/complete/...` 包装、`cophenet`、`inconsistent`、`leaves_list`、`maxdists`、`maxinconsts`、`maxRstat`、`optimal_leaf_ordering`）有一个共同特征：它们的热点循环最终都落到 `_hierarchy.pyx` 编译出的 Cython 内核。这意味着它们对数组后端的**能力约束完全一致**：

1. **只能跑在 CPU 上**（`cpu_only=True`）：Cython 代码直接操作内存里的 C 数组，没有 GPU 实现。
2. **遇到 dask 输入要告警**：Cython 内核需要**完整、连续**的距离矩阵（成对距离无法分块独立计算），所以一个分块的 dask 数组必须先「合并成单块」才能喂给内核；这一步代价不小，故发一条告警提醒用户。

既然这十几个函数的能力声明一字不差，重复写十几遍 `@xp_capabilities(cpu_only=True, reason="Cython code", warnings=[("dask.array", "merges chunks")])` 既啰嗦又易写错。于是实现层把它们**抽成一个模块级别名** `lazy_cython`，装饰时只写 `@lazy_cython` 即可。这就是「DRY（Don't Repeat Yourself）」原则在装饰器层面的体现——名字 `lazy_cython` 本身也透露了用途：「走惰性桥接的 Cython 函数」。

#### 4.1.2 核心流程

```
模块加载时（一次性）：
   lazy_cython = xp_capabilities(
       cpu_only=True,                      # 声明：只支持 CPU 数组
       reason="Cython code",               # 能力表里显示给用户的理由
       warnings=[("dask.array", "merges chunks")]   # 收到 dask 数组时发此告警
   )

定义每个 Cython 后端函数时：
   @lazy_cython          # 等价于上面那一长串 xp_capabilities(...)
   def linkage(...): ...
```

#### 4.1.3 源码精读

导入语句把桥接所需的工具一次性拉进来：

```python
from scipy._lib._array_api import (_asarray, array_namespace, is_dask,
                                   is_lazy_array, xp_capabilities, xp_copy)
import scipy._external.array_api_extra as xpx
```

参见 [_hierarchy_impl.py:L45-L48](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L45-L48)：`xp_capabilities` 来自 SciPy 自家的 array API 基础设施；`is_lazy_array` / `is_dask` 用来识别惰性（dask）输入；`xpx` 是 `array_api_extra` 的别名，`lazy_apply` 就挂在它名下。

紧接着就是别名定义本体：

```python
lazy_cython = xp_capabilities(
    cpu_only=True, reason="Cython code",
    warnings=[("dask.array", "merges chunks")])
```

参见 [_hierarchy_impl.py:L83-L85](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L83-L85)。三个参数的含义：

- `cpu_only=True` + `reason="Cython code"`：函数不支持 GPU 数组（如 `cupy.ndarray`、`jax.numpy` 的 GPU 后端），理由是「内核是 Cython（CPU 代码）」。
- `warnings=[("dask.array", "merges chunks")]`：当传入 `dask.array` 时，发一条告警，文案是「merges chunks」（合并分块）。这条告警由 `xp_capabilities` 在探测到 dask 输入时自动发出，提醒用户：**你的分块 dask 数组会被合并成单块再交给 Cython**，可能带来较大的内存/计算开销。

这个别名随后被当作装饰器复用在全部 15 个「Cython 后端函数」上。以 `linkage` 为例：

```python
@lazy_cython
def linkage(y, method='single', metric='euclidean', optimal_ordering=False):
    """ ... """
```

参见 [_hierarchy_impl.py:L722-L723](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L722-L723)。其它复用点：七个包装函数 `single`(L88)、`complete`(L167)、`average`(L250)、`weighted`(L333)、`centroid`(L419)、`median`(L522)、`ward`(L622)，以及 `optimal_leaf_ordering`(L1402)、`cophenet`(L1479)、`inconsistent`(L1622)、`leaves_list`(L2705)、`maxdists`(L3797)、`maxinconsts`(L3887)、`maxRstat`(L3987)——清一色 `@lazy_cython`。

> 对比：模块里**不**走 Cython 的函数用的是别的声明，例如 `@xp_capabilities()`（无约束，如 `fcluster`）、`@xp_capabilities(np_only=True, reason="non-standard indexing")`（只支持 NumPy，因为用了非标准索引，见 [L1210](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1210)）、`@xp_capabilities(out_of_scope=True)`（明确不纳入 array API 计划）。可见 `lazy_cython` 只是众多能力声明中「Cython 后端」那一类的命名快捷方式。

#### 4.1.4 代码实践

**实践目标**：验证 `lazy_cython` 确实等于那一长串 `xp_capabilities(...)`，并且能像普通装饰器一样工作。

**操作步骤**：

```python
# 示例代码
from scipy.cluster.hierarchy._hierarchy_impl import lazy_cython
import scipy._lib._array_api as _aapi

# 1) 看清 lazy_cython 的真身
print("lazy_cython 就是:", lazy_cython)

# 2) linkage 的 __wrapped__（装饰器通常用 functools.wraps 保留原函数）
from scipy.cluster.hierarchy import linkage
print("linkage 是否被 lazy_cython 装饰:", hasattr(linkage, "__wrapped__"))
```

**需要观察的现象**：`lazy_cython` 打印出来是 `xp_capabilities` 所返回的装饰器对象（其内部携带 `cpu_only=True` 等参数）；`linkage` 上能看到装饰器留下的痕迹（如 `__wrapped__` 指向原始函数、docstring 末尾被追加能力表）。

**预期结果**：`lazy_cython` 与你手写的 `xp_capabilities(cpu_only=True, reason="Cython code", warnings=[("dask.array", "merges chunks")])` 行为一致。（装饰器对象的确切 repr 形式待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `lazy_cython` 要带 `warnings=[("dask.array", "merges chunks")]`，而不是像 `fcluster` 那样用无参的 `@xp_capabilities()`？

**参考答案**：因为这些函数最终要把**整个**压缩距离矩阵交给 Cython 内核（成对距离无法按块独立算），分块的 dask 数组必须先合并成单块，开销可能很大，故主动告警提醒用户；`fcluster` 等纯 Python 函数没有这种「必须合并分块」的硬性需求，能力约束更宽松，无需此告警。

**练习 2**：如果将来某个函数的 Cython 内核被改写成了「可以分块独立计算」的版本，「merges chunks」这条告警还应不应该挂在它头上？

**参考答案**：不应该。该告警的语义是「输入会被强制合并成单块」。若内核已支持真正的分块计算，再发这条告警就是误导。届时应改用 `@xp_capabilities(cpu_only=True, allow_dask_compute=True, ...)` 之类的声明，允许 dask 自然分块计算而不再合并——这正是 vq 模块里 `_py_vq` 等函数的做法（见 [vq/_vq_impl.py:L86-L87](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L86-L87)）。

---

### 4.2 `cy_linkage` 闭包：把分派封装成可桥接的标准形态

#### 4.2.1 概念说明

桥接层（`lazy_apply`）要能统一处理「即时 NumPy 输入」和「惰性 dask 输入」，前提是：**真正干活的那个函数有一个固定、统一的调用形态**。这个形态就是

```
func(<若干数据参数>, validate)
```

即：前面是若干个数据参数（如距离矩阵 `y`），**最后一个**永远是布尔型 `validate` 标志，由桥接层在调用时注入。

为什么需要这个 `validate` 标志？因为**有限性校验（有没有 NaN/Inf）必须推迟到惰性数组真正求值的那一刻**才能做（见 u2-l1：对惰性数组提前 `isfinite` 会触发整图求值）。于是校验被拆成两段：

- **外层 `linkage`**（[L948](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L948)）：`if not lazy and not xp.all(xp.isfinite(y))` —— 只对**即时**数组校验，惰性数组跳过（因为现在还没法查）。
- **闭包 `cy_linkage`**（[L956](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L956)）：`if validate and not np.all(np.isfinite(y))` —— 当 `validate=True` 时补做校验。对于 dask 输入，这一刻发生在「分块已落地、即将进入 Cython」之时，正是检验 NaN/Inf 的正确时机。

`cy_linkage` 之所以是**闭包**（定义在 `linkage` 函数体内部），是因为它需要捕获外层算好的 `method`、`method_code`、`n` 三个变量——这些是「方法分派」的产物（见 u3-l2），不该再暴露给桥接层。桥接层只需知道「调用 `cy_linkage(y, validate)` 就能得到 Z」，分派细节全藏在闭包里。

#### 4.2.2 核心流程

```
linkage 内部：
   ... 校验、归约输入、算出 n 和 method_code ...
   lazy = is_lazy_array(y)                 # 记录输入是否惰性

   def cy_linkage(y, validate):            # 统一形态：(数据..., validate)
       if validate and 有非有限值: raise ValueError   # 推迟到此刻的有限性校验
       ┌─ method == 'single'    → _hierarchy.mst_single_linkage(y, n)
       ├─ method ∈ {complete,average,weighted,ward} → _hierarchy.nn_chain(y, n, method_code)
       └─ else (centroid,median)            → _hierarchy.fast_linkage(y, n, method_code)

   result = xpx.lazy_apply(cy_linkage, y, validate=lazy, ...)   # 把闭包交给桥接层
```

#### 4.2.3 源码精读

闭包本体——一个统一的 `(y, validate)` 签名 + 三路分派：

```python
def cy_linkage(y, validate):
    if validate and not np.all(np.isfinite(y)):
        raise ValueError("The condensed distance matrix must contain only "
                         "finite values.")

    if method == 'single':
        return _hierarchy.mst_single_linkage(y, n)
    elif method in ('complete', 'average', 'weighted', 'ward'):
        return _hierarchy.nn_chain(y, n, method_code)
    else:
        return _hierarchy.fast_linkage(y, n, method_code)
```

参见 [_hierarchy_impl.py:L955-L965](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L955-L965)。三个要点：

1. **签名固定**：`(y, validate)`。`y` 是数据，`validate` 是桥接层注入的布尔值。注意这里直接用了 `np.all(np.isfinite(y))`（裸 `numpy`），因为走到这一行时 `y` 一定已是落地的 NumPy 数组（Cython 内核本来就只吃 NumPy）。
2. **闭包捕获**：`method`（字符串，给 `if` 判断）、`method_code`（整数，传给 `nn_chain`/`fast_linkage` 内部的 Lance-Williams 函数指针表）、`n`（观测数）都来自外层 `linkage`。`mst_single_linkage` 专服务 `single`，故不需要 `method_code`（详见 u3-l2 练习）。
3. **`else` 分支**恰好覆盖 `centroid/median`——即 `_EUCLIDEAN_METHODS` 减去 `ward` 后剩下的两个。

这条「闭包统一形态」的约定**全模块一致**。例如 `cophenet` 里的闭包：

```python
def cy_cophenet(Z, validate):
    if validate:
        _is_valid_linkage(Z, throw=True, name='Z', xp=np)
    n = Z.shape[0] + 1
    zz = np.zeros((n * (n-1)) // 2, dtype=np.float64)
    _hierarchy.cophenetic_distances(Z, zz, n)
    return zz
```

参见 [_hierarchy_impl.py:L1592-L1598](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1592-L1598)：同样是 `(数据, validate)`，`validate=True` 时才补做校验，再调 Cython 后端。`optimal_leaf_ordering` 的 `cy_optimal_leaf_ordering(Z, y, validate)`（[L1467-L1473](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1467-L1473)）也是同一套范式——可见这是本模块桥接层的**通用设计模式**。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「闭包捕获了外层变量」「`validate` 控制是否补做校验」这两件事。

**操作步骤**：

```python
# 示例代码（源码阅读型实践，建议配合源码一起看）
import numpy as np
from scipy.spatial.distance import pdist
from scipy.cluster.hierarchy import linkage

X = np.array([[0.],[1.],[2.],[10.]], dtype=float)
y = pdist(X)

# linkage 内部会构造 cy_linkage 闭包并捕获 method/n/method_code。
# 我们无法直接拿到闭包，但可以观察 validate 的效果：
#  - 对即时数组：外层 L948 已校验 finite，闭包里 validate=False，跳过二次校验。
Z = linkage(y, method='single')
print("正常输入得到 Z, shape =", Z.shape)

# 故意造一个含 NaN 的距离矩阵：
y_bad = y.copy()
y_bad[0] = np.nan
try:
    linkage(y_bad, method='single')     # 即时数组 → 外层 L948 直接拦下
except ValueError as e:
    print("即时输入在【外层】被拦:", str(e)[:30], "...")
```

**需要观察的现象**：正常输入返回 `(n-1, 4)` 的 Z；含 NaN 的即时输入在**外层**（[L948](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L948)，`validate=False` 之前的 `not lazy` 分支）就被拦下，根本进不到闭包。这说明对即时数组，有限性校验由外层完成、闭包里的 `validate` 分支不触发。

**预期结果**：第一次调用成功打印 `shape = (3, 4)`；第二次抛出 `ValueError: The condensed distance matrix must contain only finite values.`。（数值结果待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么闭包里的有限性校验用 `np.all(np.isfinite(y))`（裸 numpy），而外层 [L948](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L948) 用 `xp.all(xp.isfinite(y))`（带 `xp`）？

**参考答案**：外层在校验时 `y` 可能还是 dask/jax 等后端数组，必须用对应命名空间 `xp` 的函数才能正确处理；而闭包里的校验只在「即将进入 Cython」时执行，此刻 `y` 已经是落地的 NumPy 数组（Cython 只吃 NumPy），用裸 `numpy` 即可。

**练习 2**：闭包为什么不直接写成 `linkage` 的一段顺序代码，而要特意定义成一个内部函数？

**参考答案**：因为这段逻辑要被 `xpx.lazy_apply` 当作一个**可调用的整体**来处理——对惰性输入，`lazy_apply` 需要把「这段逻辑」注册进 dask 任务图（每个分块落地时执行一次）。把逻辑封装成一个闭包，才能把它作为参数传给 `lazy_apply`，并让 `lazy_apply` 在合适的时机、以正确的 `validate` 值去调用它。闭包还顺便捕获了 `method/method_code/n`，免去把这些当参数层层传递。

---

### 4.3 `xpx.lazy_apply`：即时数组与惰性数组的统一入口

#### 4.3.1 概念说明

`xpx.lazy_apply`（来自 `array_api_extra`）是整个桥接的「总开关」。它的设计目标是：**让一个原本只吃即时 NumPy 数组的函数，也能透明地吃惰性 dask 数组**，而函数作者只需用一种写法。

它的可观察契约是：

```
result = xpx.lazy_apply(func, *data_args, validate=<bool>,
                        shape=<输出形状>, dtype=<输出 dtype>,
                        as_numpy=<bool>, xp=<namespace>, **kwargs)
```

- `func`：一个 `(数据..., validate)` 形态的函数（如 `cy_linkage`）。桥接层会在调用时把 `validate` 作为最后一个位置参数注入。
- `*data_args`：要喂给 `func` 的数据参数（如 `y`）。
- `validate`：布尔值，传给 `func` 的 `validate` 形参。本模块一律传 `is_lazy_array(输入)`——惰性则 `True`（落地后逐块校验），即时则 `False`（外层已校验过）。
- `shape` / `dtype`：描述**输出**的形状与类型。对惰性输入，dask 需要预先知道结果的形状才能把新节点接进任务图；对即时输入，这俩参数基本不参与计算。
- `as_numpy=True`：最终结果强制转成 NumPy 数组。
- `xp`：数组命名空间。

两种执行路径（这是本讲的核心，也是综合实践要验证的「短路」现象）：

- **即时输入**（`is_lazy_array(y)` 为 `False`）：`lazy_apply` 走「快路径」——直接调用 `func(*data_args, validate)`，把返回值原样交回。整层 `lazy_apply` 几乎是个**透传 no-op**，不构造任何任务图。这就是「短路直接计算」。
- **惰性输入**（`is_lazy_array(y)` 为 `True`）：`lazy_apply` 走「惰性路径」——把 `func` 包装成 dask 任务图里的一个节点（函数名会显示在用户的 Dask 仪表盘上），先把分块的 dask 数组合并成单块（触发 4.1 的「merges chunks」告警），再在 `.compute()` 时真正调用 `func(..., validate=True)`。结果仍是一个（惰性的）dask 数组，最终按 `as_numpy=True` 落地为 NumPy。

> 一句话：对普通 NumPy 用户，`lazy_apply` 是「透明直通」；对 dask 用户，它把 Cython 内核无缝接进了 dask 的任务图。**同一份 Cython 代码，两种输入形态，一个调用点搞定。**

#### 4.3.2 核心流程

```
                 ┌── is_lazy_array(y) == False（即时 NumPy）
                 │     lazy_apply 直接调用 cy_linkage(y, False)
                 │     → 命中 mst_single_linkage / nn_chain / fast_linkage
linkage ──► xpx.lazy_apply(cy_linkage, y, validate=lazy, ...)
                 │     （无任务图，立即返回 Z，即「短路」）
                 │
                 └── is_lazy_array(y) == True（惰性 dask）
                       lazy_apply 把 cy_linkage 注册为 dask 任务图节点
                       （函数名 "cy_linkage" 显示在 Dask 仪表盘）
                       合并 dask 分块（发 "merges chunks" 告警）
                       .compute() 时对落地块调用 cy_linkage(y, True)
                       → 同样命中三个 Cython 后端之一
                       返回（惰性）dask 数组，as_numpy=True 落地
```

#### 4.3.3 源码精读

`linkage` 末尾的桥接调用：

```python
result = xpx.lazy_apply(cy_linkage, y, validate=lazy,
                        shape=(n - 1, 4), dtype=xp.float64,
                        as_numpy=True, xp=xp)

if optimal_ordering:
    return optimal_leaf_ordering(result, y)
else:
    return result
```

参见 [_hierarchy_impl.py:L967-L974](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L967-L974)。逐参数说明：

- `cy_linkage`：上一节定义的闭包，被当作可调用对象传入。
- `y`：唯一的 data 参数。
- `validate=lazy`：`lazy = is_lazy_array(y)`（[L925](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L925)）。即时→`False`，惰性→`True`。
- `shape=(n - 1, 4)`：linkage matrix 的标准形状（见 u3-l1）。dask 路径用它声明输出节点的形状。
- `dtype=xp.float64`：输出为 float64（Z 的距离列是浮点）。
- `as_numpy=True`：最终落地为 NumPy 数组。
- `xp=xp`：数组命名空间。

「函数名会显示在 Dask 仪表盘」这一点，是模块作者特意留注释强调的。在 `optimal_leaf_ordering` 里能看到：

```python
# The function name is prominently visible on the user-facing Dask dashboard;
# make sure it is meaningful.
def cy_optimal_leaf_ordering(Z, y, validate):
    ...
```

参见 [_hierarchy_impl.py:L1465-L1467](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1465-L1467)。这条注释反向印证了 `lazy_apply` 的惰性路径：**闭包的名字会作为任务名出现在 dask 图里**，所以作者才要求「名字要有意义」（叫 `cy_linkage` 而不是随便一个 `_lambda`）。若 `lazy_apply` 对所有输入都立即执行，函数名根本不会上仪表盘，这条注释也就没意义了。

> 边界：`as_numpy=True` 主要影响**惰性路径**——即时路径下 `cy_linkage` 本就返回 NumPy 数组，转不转都一样。同理 `shape`/`dtype` 也主要服务于 dask 任务图的拼接。这就是为什么普通 NumPy 用户完全感受不到这层桥接的存在。

#### 4.3.4 代码实践

**实践目标**：分别用即时 NumPy 数组和（若装了 dask 的）惰性 dask 数组调用 `linkage`，对比两条路径。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.spatial.distance import pdist
from scipy.cluster.hierarchy import linkage

X = np.array([[0.,0.],[0.,1.],[1.,0.],[5.,5.],[5.,6.],[6.,5.]])
y = pdist(X)                       # 即时 NumPy 数组

# 路径 A：即时输入 → lazy_apply 走快路径，直接出 Z
Z = linkage(y, method='single')
print("A 即时输入:", type(Z).__name__, Z.shape)   # 期望: ndarray (5, 4)

# 路径 B：惰性输入（需要安装 dask，否则跳过本段）
try:
    import dask.array as da
    import warnings
    y_lazy = da.from_array(y, chunks=y.shape)   # 单块 dask 数组
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        Z2 = linkage(y_lazy, method='single')
        dask_warns = [str(x.message) for x in w if "merges chunks" in str(x.message)]
    print("B 惰性输入结果类型:", type(Z2).__name__)
    print("B 是否触发 merges chunks 告警:", bool(dask_warns))
    print("B 结果与 A 一致:", np.allclose(np.asarray(Z2), Z))
except ImportError:
    print("B 未安装 dask，跳过惰性路径演示")
```

**需要观察的现象**：

- 路径 A：结果是 `numpy.ndarray`，形状 `(5, 4)`，过程无任何 dask 告警。
- 路径 B（若装了 dask 且开启了 array API）：触发「merges chunks」告警；结果落地后与路径 A 数值一致。若未开 `SCIPY_ARRAY_API` 环境变量，`array_namespace(y_lazy)` 仍可能把输入先转成 numpy，惰性路径可能不被触发——这一点取决于运行环境。

**预期结果**：即时输入走快路径直接返回 `ndarray`；惰性输入走 dask 路径并告警，最终数值一致。**dask 路径的具体行为依赖环境与 `SCIPY_ARRAY_API` 开关，待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：对即时 NumPy 输入，`shape=(n-1, 4)` 和 `as_numpy=True` 这两个参数有没有实际作用？

**参考答案**：基本没有。即时路径下 `lazy_apply` 直接调用 `cy_linkage` 并原样返回其结果，而 `cy_linkage` 内部的 Cython 后端本就返回形状 `(n-1, 4)` 的 NumPy 数组。这两个参数主要服务于**惰性路径**——dask 任务图需要预先知道输出形状/dtype 才能拼接节点。它们的「冗余」恰恰是统一接口的代价：用一套参数同时描述两种路径。

**练习 2**：为什么 `validate` 取值是 `is_lazy_array(y)`，而不是恒为 `True`（总是校验）？

**参考答案**：恒为 `True` 会对即时输入做**重复**校验——外层 [L948](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L948) 已经查过 finite 了。让 `validate` 跟随「是否惰性」，即时输入 `validate=False` 跳过二次校验（省一次全扫），惰性输入 `validate=True` 在落地块上补做校验（外层当时没法查）。这样既不重复、又不漏检。

---

### 4.4 三路 Cython 后端的桥接接口

#### 4.4.1 概念说明

桥接层的终点是 `_hierarchy.pyx` 编译出的三个函数。从**桥接**视角，我们只关心它们对调用方暴露的**接口**（签名 + 输入输出），不关心算法内部（那是 u4 的主题）。三者都满足一个共同前提：**吃一个连续的 1D 压缩距离矩阵（`double[:]`）+ 观测数 `n`，吐一个 `(n-1, 4)` 的 linkage matrix**。这正是 `cy_linkage` 能用统一方式调用它们的根本原因。

| 后端函数 | 签名 | 服务的方法 | 算法 / 复杂度（详见 u4） |
|----------|------|-----------|--------------------------|
| `_hierarchy.mst_single_linkage(dists, n)` | 2 个参数 | `single` | 最小生成树（Prim 风格），\(O(n^2)\) |
| `_hierarchy.nn_chain(dists, n, method)` | 3 个参数 | `complete/average/weighted/ward` | 最近邻链，\(O(n^2)\) |
| `_hierarchy.fast_linkage(dists, n, method)` | 3 个参数 | `centroid/median` | 朴素算法（堆+并查集），\(O(n^3)\) |

注意 `mst_single_linkage` 只收 2 个参数（无 `method`，因为只服务 `single`），而另两个收 3 个（多一个 `method` 整数编码，用于在内部查 Lance-Williams 更新公式，见 u3-l4）。这种「参数个数不齐」正是 `cy_linkage` 里必须用 `if/elif/else` 分别调用、而不能用一张函数表统一调用的原因之一。

#### 4.4.2 核心流程

```
cy_linkage 根据 method 选中后端：
   single                                   → _hierarchy.mst_single_linkage(y, n)
   complete/average/weighted/ward           → _hierarchy.nn_chain(y, n, method_code)
   centroid/median                          → _hierarchy.fast_linkage(y, n, method_code)

三者约定：
   入参 dists：1D float64 C 连续压缩距离矩阵（pdist 形式，长度 n(n-1)/2）
   入参 n：原始观测数
   入参 method（仅 nn_chain/fast_linkage）：0..6 的整数编码（_LINKAGE_METHODS）
   返回：Z，形状 (n-1, 4) 的 float64 NumPy 数组
```

#### 4.4.3 源码精读

三个后端的签名（本讲只看签名与 docstring 头部，实现见 u4）：

`mst_single_linkage`——最小生成树单链接，只收 `dists` 与 `n`：

```python
def mst_single_linkage(const double[:] dists, int n):
    """Perform hierarchy clustering using MST algorithm for single linkage.
    ...
    Z : ndarray, shape (n - 1, 4)
        Computed linkage matrix.
    """
```

参见 [_hierarchy.pyx:L1032-L1046](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1032-L1046)。`const double[:] dists` 是 Cython 的「类型化内存视图」，要求传入连续的 1D float64 数组——这正是 `linkage` 开头 `y = _asarray(y, order='C', dtype=xp.float64, ...)`（[L924](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L924)）做规整的原因：保证喂给后端的数组满足这个内存契约。

`nn_chain`——最近邻链，多一个 `method`：

```python
def nn_chain(const double[:] dists, int n, int method):
    """Perform hierarchy clustering using nearest-neighbor chain algorithm.
    ...
    method : int
        The linkage method. 0: single 1: complete 2: average 3: centroid
        4: median 5: ward 6: weighted
    """
```

参见 [_hierarchy.pyx:L924-L936](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L924-L936)。docstring 里列出了编码 `0..6` 的含义，与 Python 层 `_LINKAGE_METHODS`（[L51-L52](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L51-L52)）一一对应——`method_code` 跨越桥接层时语义不变。

`fast_linkage`——朴素算法，同样 `dists, n, method`：

```python
def fast_linkage(const double[:] dists, int n, int method):
    """Perform hierarchy clustering.

    It implements "Generic Clustering Algorithm" from [1]. The worst case
    time complexity is O(N^3), but the best case time complexity is O(N^2) and
    it usually works quite close to the best case.
    """
```

参见 [_hierarchy.pyx:L792-L798](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L792-L798)。worst case \(O(n^3)\)。

> 桥接视角的结论：**三个后端对调用方的契约高度一致**（同样的 `dists`/`n`、同样的 `(n-1,4)` 输出），差异仅在「是否需要 `method`」与「内部复杂度」。`cy_linkage` 的 `if/elif/else` 正是对这一「大同小异」的精准适配。

#### 4.4.4 代码实践

**实践目标**：从 Python 侧直接触达编译后端，验证三者都接受 `(dists, n[, method])` 并返回 `(n-1, 4)` 的 Z。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.spatial.distance import pdist
from scipy.cluster.hierarchy._hierarchy_impl import _hierarchy   # 编译后的 Cython 模块

X = np.array([[i] for i in [2, 8, 0, 4, 1, 9, 9, 0]], dtype=float)
y = np.ascontiguousarray(pdist(X), dtype=np.float64)   # 满足 const double[:] 契约
n = len(X)

Z_mst = _hierarchy.mst_single_linkage(y, n)                       # single
Z_nn  = _hierarchy.nn_chain(y, n, 1)                              # complete (code=1)
Z_fast= _hierarchy.fast_linkage(y, n, 3)                          # centroid (code=3)

for name, Z in [("mst(single)", Z_mst), ("nn_chain(complete)", Z_nn),
                ("fast_linkage(centroid)", Z_fast)]:
    print(f"{name:24s} shape={Z.shape} dtype={Z.dtype}")
```

**需要观察的现象**：三个后端都能被直接调用，返回值形状都是 `(7, 4)`（n=8，故 n-1=7）、dtype 都是 `float64`。这印证了「桥接层终点的统一契约」。

**预期结果**：三行输出形状均为 `(7, 4)`、dtype `float64`。（直接 import `_hierarchy` 是否成功取决于 SciPy 是否已编译安装；若失败可改为通过 `linkage(y, method=...)` 间接验证。数值结果待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：既然三个后端接口如此相似，为什么不把它们合并成一个 `_hierarchy.linkage(dists, n, method)`，由 Cython 内部再分派？

**参考答案**：其实 Cython 层**确实**有一个内部 `linkage(dists, n, method)` 函数（见 [_hierarchy.pyx:L686](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L686)）。但 Python 层的 `cy_linkage` 没有调用它，而是**直接**调三个专用函数。原因是 Python 层想让 `single` 走专用的 MST 实现、让可还原方法走 `nn_chain`，以获得更好的复杂度（\(O(n^2)\) 而非朴素 \(O(n^3)\)）；那个内部 `linkage` 更像是「朴素通用版」。这体现了「Python 层做策略选择、Cython 层提供多个专用引擎」的分工。

**练习 2**：`cy_linkage` 调用后端时传的是 `y`（桥接层注入的参数），而非外层原始的 `y`。这两者一定相同吗？

**参考答案**：对即时输入，闭包拿到的 `y` 就是外层规整后的 `y`（float64、C 连续、已校验 finite），二者一致。对惰性输入，闭包拿到的 `y` 是 dask 分块**落地**后的 NumPy 数组（可能就是合并后的单块），与原始 dask 数组「数值相同、形态不同」（一个是惰性图、一个是已求值的 ndarray）。这正是桥接层把「原始惰性输入」翻译成「Cython 能吃的即时输入」的体现。

---

## 5. 综合实践

把本讲四节内容串起来，完成实践任务：**画出 `linkage → lazy_apply → cy_linkage → _hierarchy.*` 的完整调用链，并说清普通 NumPy 输入如何短路。**

**任务步骤**：

1. **画调用链图**。对照源码，在纸上画出下面这条链，并在每个箭头旁标注「发生在哪一行、传了什么」：

   ```
   linkage(y, method)                        # _hierarchy_impl.py L722
        │  xp=array_namespace(y); y=_asarray(...); lazy=is_lazy_array(y)   # L923-L925
        │  校验 + 归约输入 + n=..., method_code=...                         # L927-L953
        ▼
   xpx.lazy_apply(cy_linkage, y, validate=lazy, shape=(n-1,4), ...)        # L967-L969
        │
        ├─[lazy=False 即时]──► 直接调用 cy_linkage(y, False)   （短路，无任务图）
        │                          │
        └─[lazy=True  惰性]──► 注册进 dask 任务图，合并分块(告警)，.compute() 时调用 cy_linkage(y, True)
                                   │
                                   ▼
                          cy_linkage(y, validate)                          # L955-L965
                          │ if validate: 补做 finite 校验                   # L956
                          │ method=='single'      → _hierarchy.mst_single_linkage(y, n)      # L961
                          │ method∈{complete,…}   → _hierarchy.nn_chain(y, n, method_code)   # L963
                          │ else (centroid/median)→ _hierarchy.fast_linkage(y, n, method_code)# L965
                                   ▼
                          Z (shape (n-1,4), float64 NumPy 数组)
   ```

2. **解释「短路」**。用一段话说明：当 `y` 是普通 `numpy.ndarray` 时，`is_lazy_array(y)` 为 `False`，`validate=False`；`xpx.lazy_apply` 走快路径，**不构造任何 dask 任务图**，而是直接调用 `cy_linkage(y, False)`，闭包因 `validate=False` 跳过二次 finite 校验（外层 [L948](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L948) 已校验过），随即命中三个 Cython 后端之一并返回 Z。整层 `lazy_apply` 对 NumPy 用户近乎「透明不存在」。

3. **代码验证短路**。用下面的脚本确认即时输入下结果就是普通 `ndarray`、且全程无 dask 告警：

   ```python
   # 示例代码
   import warnings, numpy as np
   from scipy.spatial.distance import pdist
   from scipy.cluster.hierarchy import linkage

   X = np.array([[0.,0.],[0.,1.],[1.,0.],[5.,5.],[5.,6.],[6.,5.]])
   y = pdist(X)
   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter("always")
       Z = linkage(y, method='ward')
       print("结果类型:", type(Z).__name__)                 # 期望 ndarray
       print("形状:", Z.shape)                              # 期望 (5, 4)
       print("是否有 merges chunks 告警:",
             any("merges chunks" in str(x.message) for x in w))   # 期望 False
   ```

**验收标准**：能不看讲义，凭源码行号讲清「`linkage` → `lazy_apply`（按 `is_lazy_array` 二选一）→ `cy_linkage`（按 `method` 三选一）→ Cython 后端」这条链，并能解释为什么普通 NumPy 输入下 `lazy_apply` 等价于直接调用 `cy_linkage`。

## 6. 本讲小结

- `lazy_cython` 只是 `xp_capabilities(cpu_only=True, reason="Cython code", warnings=[("dask.array", "merges chunks")])` 的**模块级复用别名**，装饰在全部 15 个「最终落到 Cython」的函数上，统一声明「CPU-only + 对 dask 输入发『merges chunks』告警」。
- 桥接层要求干活函数有统一形态 `func(数据..., validate)`：`validate` 是桥接层注入的布尔值，控制是否在「即将进入 Cython」时补做有限性校验。
- `cy_linkage(y, validate)` 是 `linkage` 的内嵌闭包：捕获外层的 `method/method_code/n`，把 u3-l2 的「三路分派」封装成桥接层可调用的标准形态；`validate` 由桥接层按「输入是否惰性」注入。
- `xpx.lazy_apply(cy_linkage, y, validate=lazy, shape=(n-1,4), as_numpy=True, ...)` 是桥接总开关：**即时输入走快路径直接调用闭包（短路）**，**惰性 dask 输入把闭包注册进任务图、合并分块后再调用**——同一份 Cython 代码，两种输入形态，一个调用点。
- 三个 Cython 后端（`mst_single_linkage`/`nn_chain`/`fast_linkage`）对调用方的契约高度一致：都吃 `float64` 连续 1D 压缩距离矩阵 + `n`（后两者多一个 `method` 编码），都吐 `(n-1,4)` 的 Z；差异仅在「是否需要 `method`」与「内部复杂度」，算法内部留待 u4。
- 对普通 NumPy 用户，整层桥接（`lazy_cython` 装饰器、`lazy_apply`、`validate` 机制）**近乎透明**——`is_lazy_array` 为假，`lazy_apply` 退化为直接调用，结果就是普通的 `ndarray`。

## 7. 下一步学习建议

- **想看三种算法的内部**：`mst_single_linkage` 的 Prim 实现、`nn_chain` 的最近邻链、`fast_linkage` 的堆与并查集——见 **u4 层次聚类核心算法（Cython 后端）**，本讲提到的 `condensed_index`、`LinkageUnionFind`、`Heap` 等数据结构在那里展开。
- **想看距离更新公式**：`nn_chain`/`fast_linkage` 内部如何用 `method_code` 查 Lance-Williams 更新函数——见 **u3-l4 Lance-Williams 距离更新公式**。
- **想看桥接层的底层实现**：`xp_capabilities` 装饰器到底如何拦截 dask 输入并发告警、`lazy_apply` 如何用 `dask.array` 拼接任务图——这些在 `scipy/_lib/_array_api.py` 与 `array_api_extra` 里，见 **u7-l3 array API 兼容、惰性数组与测试体系**。
- **想看更多桥接范例**：本模块里 `cophenet`、`inconsistent`、`leaves_list`、`optimal_leaf_ordering` 都用了完全相同的「`@lazy_cython` + 闭包 `(数据, validate)` + `lazy_apply`」范式，可任选一个对照阅读，巩固本讲的桥接模式。

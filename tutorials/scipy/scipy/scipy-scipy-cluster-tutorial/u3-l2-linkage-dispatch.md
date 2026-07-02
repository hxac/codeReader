# linkage() 总入口与七种方法分发

## 1. 本讲目标

本讲聚焦 `scipy.cluster.hierarchy` 子模块的「总入口」——`linkage()` 函数。学完本讲你应当能够：

- 说清 `_LINKAGE_METHODS` 与 `_EUCLIDEAN_METHODS` 这两个常量各自的含义与作用。
- 解释 `single/complete/average/weighted/centroid/median/ward` 这七个「便捷包装函数」与 `linkage()` 的关系。
- 跟踪 `linkage()` 内部从「输入校验」→「1D/2D 输入处理」→「方法分派」的完整控制流。
- 说清为何 `centroid/median/ward` 三种方法强制要求欧氏度量，以及 `ward` 为何落到 `nn_chain` 而 `centroid/median` 只能落到 `fast_linkage`。
- 用同一个距离矩阵跑通七种方法，并定性解释 `Z[:,2]`（距离列）的差异来源。

本讲只讲「分派（dispatch）」，即 `linkage()` 如何把请求路由到正确的 Cython 后端；**不**展开 Lance-Williams 距离更新公式的数学（见 u3-l4），也**不**展开三种 Cython 算法本身的实现（见 u4）。Python 与 Cython 的桥接细节（`lazy_cython`、`lazy_apply`）见 u3-l3，本讲只点到为止。

## 2. 前置知识

阅读本讲前，你需要已经建立以下认知（均来自前置讲义）：

- **linkage matrix Z**：形状恒为 \((n-1)\times 4\)，四列依次是「被合并的两簇编号 / 合并距离 / 新簇包含的原始观测数」；簇编号采用 \(n+i\) 约定（见 u3-l1）。
- **凝聚式聚类**：每个观测先自成一簇，每步合并当前最相似的两簇，共 \(n-1\) 步（见 u3-l1）。
- **两种输入形态**：1D 压缩距离矩阵（`pdist` 输出，长度 \(n(n-1)/2\)）与 2D 观测矩阵（\(m\times n\)）。
- **双层架构**：`_*_impl.py`（纯 Python 封装/校验层）调用 `_*.pyx`（Cython 性能层）（见 u1-l2）。

本讲会用到的两个新术语：

- **method（链接方法）**：决定「合并两簇后，新簇到其它簇的距离如何由原来的两两距离推导」。本模块支持七种方法。
- **分派（dispatch）**：`linkage()` 根据用户指定的 `method` 字符串，选择不同的 Cython 后端函数来执行实际聚类。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开：

| 文件 | 作用 |
|------|------|
| `hierarchy/_hierarchy_impl.py` | hierarchy 子模块的纯 Python 实现层。本讲的全部内容——两个方法常量、七个包装函数、`linkage()` 主函数及其内部的 `cy_linkage` 分派闭包——都在这里。 |

涉及的关键符号：

- `_LINKAGE_METHODS` / `_EUCLIDEAN_METHODS`：方法名到整数编码的映射、需要欧氏度量的方法集合。
- `single/complete/average/weighted/centroid/median/ward`：七个便捷包装函数。
- `linkage(y, method, metric, optimal_ordering)`：总入口。
- `cy_linkage`：`linkage` 内部的闭包，负责真正调用 Cython 后端 `_hierarchy.mst_single_linkage` / `_hierarchy.nn_chain` / `_hierarchy.fast_linkage`。

> 说明：`_hierarchy` 是 `_hierarchy.pyx` 编译后的扩展模块（见 u1-l2），本讲只把它当作「三个聚类后端函数的来源」，不进入其源码。

## 4. 核心概念与源码讲解

### 4.1 七种链接方法与编码：`_LINKAGE_METHODS` / `_EUCLIDEAN_METHODS`

#### 4.1.1 概念说明

层次聚类的「链接方法」回答一个问题：当我们把簇 \(s\) 和簇 \(t\) 合并成新簇 \(u\) 之后，\(u\) 到剩余任意簇 \(v\) 的距离该怎么算？不同的回答方式就对应不同的方法。SciPy 支持七种：

| 方法名 | 别名 | 直觉（合并后 \(d(u,v)\) 的定义） |
|--------|------|----------------------------------|
| `single` | 最近点法 | \(\min\) over 所有跨簇点对距离 |
| `complete` | 最远点法 | \(\max\) over 所有跨簇点对距离 |
| `average` | UPGMA | 跨簇点对距离的（按基数）加权平均 |
| `weighted` | WPGMA | 两个子簇到 \(v\) 距离的简单平均 |
| `centroid` | UPGMC | 两簇**质心**的欧氏距离 |
| `median` | WPGMC | 两簇质心中点为新质心 |
| `ward` | Ward 方差最小化 | 使合并后簇内方差增量最小 |

这七种方法在 Cython 后端里是用**整数编码**来标识的——后端维护一张「编码 → Lance-Williams 距离更新函数」的函数指针表（见 u3-l4），所以 Python 层需要把字符串 `method` 翻译成整数。这正是 `_LINKAGE_METHODS` 字典的职责。

此外，`centroid/median/ward` 这三种方法在数学上**只在欧氏度量下才有良好定义**：`centroid/median` 直接操作质心（质心依赖欧氏空间的线性结构），`ward` 基于方差（方差是欧氏距离的平方和）。所以需要另一个集合 `_EUCLIDEAN_METHODS` 来标记「这些方法必须配欧氏度量」。

#### 4.1.2 核心流程

```
用户传入字符串 method
        │
        ▼
_LINKAGE_METHODS[method]  ──►  整数编码（0..6），交给 Cython 后端的函数指针表
        │
        ▼
if method in _EUCLIDEAN_METHODS:
    强制 metric == 'euclidean'（对 2D 观测输入会校验，见 4.3）
```

#### 4.1.3 源码精读

两个常量紧挨着定义在文件开头、导入语句之后：

```python
_LINKAGE_METHODS = {'single': 0, 'complete': 1, 'average': 2, 'centroid': 3,
                    'median': 4, 'ward': 5, 'weighted': 6}
_EUCLIDEAN_METHODS = ('centroid', 'median', 'ward')
```

参见 [_hierarchy_impl.py:L51-L53](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L51-L53)：`_LINKAGE_METHODS` 把每个方法名映射到一个整数；`_EUCLIDEAN_METHODS` 是一个元组，列出必须使用欧氏度量的三种方法。注意编码顺序并非按字典序——例如 `ward` 是 5、`weighted` 是 6，这与 Cython 后端 `_hierarchy_distance_update.pxi` 中函数指针表的下标一一对应（见 u3-l4）。

> 重点：**编码本身不代表算法**。同一个编码在不同后端函数里意义不同，方法名到「具体聚类算法」的映射发生在 `cy_linkage` 闭包里（见 4.4），而不是在这张表里。这张表只解决「方法名 → 后端函数指针表下标」这一步。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `_LINKAGE_METHODS` 的存在与编码，并理解它是模块级私有常量。

**操作步骤**：

1. 写一段脚本，从实现层导入这两个常量并打印。

```python
# 示例代码
from scipy.cluster.hierarchy._hierarchy_impl import _LINKAGE_METHODS, _EUCLIDEAN_METHODS
print(_LINKAGE_METHODS)
print(_EUCLIDEAN_METHODS)
```

2. 对照 `linkage()` 的 docstring（见 [L773-L841](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L773-L841)）里每种方法对应的数学公式，在纸上为每个编码标注它属于哪种方法。

**需要观察的现象**：`_LINKAGE_METHODS` 共 7 项，值是 0 到 6 的整数；`_EUCLIDEAN_METHODS` 恰好是 `('centroid', 'median', 'ward')`。

**预期结果**：编码与 4.1.1 表格完全一致。注意 `ward` 是 5、`weighted` 是 6。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_LINKAGE_METHODS` 用字典（字符串→整数），而不直接在代码里写一堆 `if method == 'single': code = 0`？

**参考答案**：字典把「方法名 → 编码」的映射集中成一张表，既用于 `cy_linkage` 里把 `method` 转成 `method_code` 传给后端，也用于在入口处一句 `if method not in _LINKAGE_METHODS` 统一做合法性校验（见 4.3）。集中成表比散落的 `if` 更易维护、不易写错。

**练习 2**：`average`（UPGMA）和 `weighted`（WPGMA）都不在 `_EUCLIDEAN_METHODS` 里，这意味着它们可以用任意度量吗？

**参考答案**：是的。`average/weighted` 只对「两两距离」做线性加权平均，不涉及质心或方差，因此对任意距离度量都有良好定义。只有 `centroid/median/ward` 因为依赖欧氏空间的线性/方差结构才被列入 `_EUCLIDEAN_METHODS`。

---

### 4.2 七个便捷包装函数：`single/complete/...`

#### 4.2.1 概念说明

模块顶层（`hierarchy/__init__.py`）除了导出万能的 `linkage()`，还导出了七个「方法专用」的便捷函数：`single`、`complete`、`average`、`weighted`、`centroid`、`median`、`ward`。它们的存在意义有两点：

1. **API 便捷**：`single(y)` 比 `linkage(y, method='single')` 更短、更直观。
2. **文档分流**：每个函数有自己的 docstring 和示例，用户查 `help(ward)` 就能看到 ward 专属的输出例子，而不必在 `linkage` 的长文档里翻找。

它们的本质都是**对 `linkage()` 的薄封装**——固定 `method` 参数后转发。它们本身**不含任何聚类逻辑**。

#### 4.2.2 核心流程

```
single(y)  ──►  linkage(y, method='single',   metric='euclidean')  ──►  Z
complete(y) ──► linkage(y, method='complete', metric='euclidean')  ──►  Z
...（其余 5 个同理）
```

注意它们都硬编码了 `metric='euclidean'`。这并不与「`single/complete/...` 可用任意度量」矛盾：因为这七个包装函数只接受**已算好的压缩距离矩阵 `y`**（`pdist` 的输出），此时 `metric` 参数在 `linkage` 内部会被忽略（见 4.3）。也就是说，想用非欧氏度量，只需先用 `pdist(X, metric='cityblock')` 算出 `y`，再 `average(y)` 即可。

#### 4.2.3 源码精读

七个函数的签名与装饰器完全一致，函数体都只有一行 `return`。以 `single` 为例：

```python
@lazy_cython
def single(y):
    """ ...（很长的 docstring，含示例）... """
    return linkage(y, method='single', metric='euclidean')
```

- 参见 [single 定义：L88-L89](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L88-L89) 与 [single 的 return：L164](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L164)。
- `lazy_cython` 装饰器定义见 [L83-L85](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L83-L85)，它等价于 `xp_capabilities(cpu_only=True, reason="Cython code", warnings=[("dask.array", "merges chunks")])`——声明这些函数只能在 CPU 上跑（因为最终会落到 Cython），并对 dask 输入给出告警。其工作机理（让 Cython 后端能处理惰性数组）见 u3-l3。

七个 `return` 语句整齐排布，固定 `method`、转发 `y`：

| 函数 | return 行号 | 转发的方法 |
|------|------------|-----------|
| `single` | [L164](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L164) | `method='single'` |
| `complete` | [L247](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L247) | `method='complete'` |
| `average` | [L330](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L330) | `method='average'` |
| `weighted` | [L416](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L416) | `method='weighted'` |
| `centroid` | [L519](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L519) | `method='centroid'` |
| `median` | [L619](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L619) | `method='median'` |
| `ward` | [L719](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L719) | `method='ward'` |

> 结论：**所有真正的逻辑都在 `linkage()` 里**。七个包装函数只是带文档的「语法糖」。理解了 `linkage()`，就理解了这七个函数。

#### 4.2.4 代码实践

**实践目标**：验证「包装函数 == 固定 method 的 linkage」这一等价关系。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.spatial.distance import pdist
from scipy.cluster.hierarchy import linkage, single, ward

X = np.array([[0, 0], [0, 1], [1, 0], [4, 0], [4, 1]], dtype=float)
y = pdist(X)

Z_a = single(y)
Z_b = linkage(y, method='single')
print("single(y) == linkage(y, 'single'):", np.array_equal(Z_a, Z_b))

Z_c = ward(y)
Z_d = linkage(y, method='ward')
print("ward(y)   == linkage(y, 'ward'):  ", np.array_equal(Z_c, Z_d))
```

**需要观察的现象**：两次比较都打印 `True`。

**预期结果**：`np.array_equal` 返回 `True`，证明包装函数确实只是转发了参数。（数值结果待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：既然 `ward(y)` 和 `linkage(y, method='ward')` 完全等价，为什么 SciPy 还要同时保留两者？

**参考答案**：`ward(y)` 提供更短、更可读的调用形式，且拥有自己的 docstring/示例，便于发现与教学；`linkage(y, method=...)` 则适合 `method` 是运行时变量（例如在循环里遍历多种方法）的场景。两者面向不同使用习惯。

**练习 2**：调用 `centroid(X)`（直接传 2D 观测矩阵而非距离矩阵）会发生什么？

**参考答案**：`centroid` 把请求转发给 `linkage(X, method='centroid', metric='euclidean')`。由于 `centroid` 属于 `_EUCLIDEAN_METHODS` 且 `metric` 正是默认的 `'euclidean'`，不会触发 4.3 中的欧氏校验报错；`linkage` 会用 `pdist(X, 'euclidean')` 把观测矩阵转成距离矩阵后再聚类。

---

### 4.3 `linkage()` 主函数：输入校验与 1D/2D 处理

#### 4.3.1 概念说明

`linkage()` 是 hierarchy 子模块真正的「总入口」。它做了三件 Python 层该做的事，然后把「脏活累活」交给 Cython：

1. **规整与校验输入**：转成 `float64` 的 C 连续数组、校验 `method` 合法性、校验 `_EUCLIDEAN_METHODS` 的度量约束。
2. **统一输入形态**：无论用户给的是 1D 压缩距离矩阵还是 2D 观测矩阵，最终都归约成 1D 压缩距离矩阵。
3. **分派后端**：根据 `method` 选择 `mst_single_linkage` / `nn_chain` / `fast_linkage` 之一（见 4.4）。

本节讲前两步，下一节讲第三步。

#### 4.3.2 核心流程

```
linkage(y, method, metric, optimal_ordering)
 │
 ├─ xp = array_namespace(y); y = _asarray(y, float64, C序); lazy = is_lazy_array(y)
 │
 ├─ 校验 1: if method not in _LINKAGE_METHODS:  raise ValueError   # 方法名非法
 │
 ├─ 校验 2: if method in _EUCLIDEAN_METHODS and metric != 'euclidean' and y.ndim == 2:
 │            raise ValueError   # 这三种方法必须欧氏度量
 │
 ├─ 归约输入形态:
 │     y.ndim == 1 ──► distance.is_valid_y(y, throw=True)            # 已是压缩距离矩阵，校验长度
 │     y.ndim == 2 ──► 若像"未压缩的距离矩阵"则告警; y = distance.pdist(y, metric)
 │     其它        ──► raise ValueError("`y` must be 1 or 2 dimensional.")
 │
 ├─ 校验 3: if not lazy and not all(isfinite(y)): raise ValueError  # 不允许 NaN/inf
 │
 ├─ n = distance.num_obs_y(y)          # 由压缩长度反推原始观测数
 ├─ method_code = _LINKAGE_METHODS[method]
 │
 └─► 进入 cy_linkage 闭包（见 4.4）
```

#### 4.3.3 源码精读

函数开头：取数组后端、强制 `float64` C 序、检测惰性（dask）：

```python
xp = array_namespace(y)
y = _asarray(y, order='C', dtype=xp.float64, xp=xp)
lazy = is_lazy_array(y)
```

参见 [_hierarchy_impl.py:L923-L925](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L923-L925)。`_asarray` 是 SciPy 的数组规整工具（见 u2-l1），这里把任意输入统一成 Cython 后端能直接吃的 `float64` C 连续数组；`is_lazy_array` 用来识别 dask 等惰性数组，后续多处用 `lazy` 分支避免对惰性数组强制求值。

**校验 1：方法名合法性**——一句查表即可：

```python
if method not in _LINKAGE_METHODS:
    raise ValueError(f"Invalid method: {method}")
```

参见 [L927-L928](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L927-L928)。

**校验 2：欧氏度量约束**——本节最值得玩味的一处：

```python
if method in _EUCLIDEAN_METHODS and metric != 'euclidean' and y.ndim == 2:
    msg = f"`method={method}` requires the distance metric to be Euclidean"
    raise ValueError(msg)
```

参见 [L930-L932](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L930-L932)。这里有一个**容易被忽略的 `y.ndim == 2` 条件**：校验只在「输入是 2D 观测矩阵、即将由 `linkage` 自己调 `pdist` 算距离」时才生效。如果用户直接传 1D 压缩距离矩阵（`y.ndim == 1`），函数无从知道当初用了什么度量，于是**放弃校验**，把「保证欧氏」的责任甩给用户——这一点在 docstring 的 Notes 第 2 条里有明确说明（见 [L890-L894](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L890-L894)）。

**输入形态归约**：

```python
if y.ndim == 1:
    distance.is_valid_y(y, throw=True, name='y')
elif y.ndim == 2:
    if (not lazy and y.shape[0] == y.shape[1]
        and xp.all(xpx.isclose(xp.linalg.diagonal(y), 0))
        and xp.all(y >= 0) and xp.all(xpx.isclose(y, y.T))):
        warnings.warn('The symmetric non-negative hollow observation '
                      'matrix looks suspiciously like an uncondensed '
                      'distance matrix', ClusterWarning, stacklevel=2)
    y = distance.pdist(y, metric)
else:
    raise ValueError("`y` must be 1 or 2 dimensional.")
```

参见 [L934-L946](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L934-L946)。关键点：

- 1D 路径只做合法性校验（`is_valid_y` 检查长度是否满足 \(\binom{n}{2}\)）。
- 2D 路径先做一个**贴心告警**：如果这个方阵「对角线全 0、非负、对称」，那它八成是个「没压缩的距离矩阵」而不是观测矩阵（这是初学者最常犯的错），于是发 `ClusterWarning` 提醒；随后无论如何都用 `pdist(y, metric)` 把它当观测矩阵处理。
- 3D 及以上直接报错。

**有限性校验与观测数反推**：

```python
if not lazy and not xp.all(xp.isfinite(y)):
    raise ValueError("The condensed distance matrix must contain only finite values.")

n = distance.num_obs_y(y)
method_code = _LINKAGE_METHODS[method]
```

参见 [L948-L953](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L948-L953)。`num_obs_y` 由压缩长度 \(L=n(n-1)/2\) 反解出 \(n\)；`method_code` 是给 Cython 后端函数指针表用的整数。注意有限性检查同样有 `not lazy` 守卫——对惰性数组强制 `isfinite` 会触发整块计算，违背惰性本意。

#### 4.3.4 代码实践

**实践目标**：亲眼看到三类校验/告警的触发条件，尤其是「欧氏约束只在 2D 输入时生效」和「疑似未压缩距离矩阵告警」。

**操作步骤**：

```python
# 示例代码
import warnings
import numpy as np
from scipy.cluster.hierarchy import linkage, ClusterWarning

# (1) 方法名非法
try:
    linkage(np.array([[1.0],[2.0]]), method='typo')
except ValueError as e:
    print("校验1:", e)

# (2) ward + 非欧氏度量 + 2D 观测矩阵  →  报错
try:
    linkage(np.array([[1.0,2.0],[3.0,4.0],[5.0,6.0]]), method='ward', metric='cityblock')
except ValueError as e:
    print("校验2:", e)

# (2') ward + 非欧氏度量 + 1D 距离矩阵  →  不报错（由用户自负其责）
from scipy.spatial.distance import pdist
y1 = pdist(np.array([[1.0,2.0],[3.0,4.0],[5.0,6.0]]), metric='cityblock')
Z = linkage(y1, method='ward')   # 不会报错，但结果在数学上未必正确
print("校验2': 1D + ward + cityblock 距离 跑通，Z 形状 =", Z.shape)

# (3) 疑似未压缩距离矩阵  →  告警
D = np.array([[0.,1.,2.],[1.,0.,3.],[2.,3.,0.]])
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    linkage(D, method='single')
    print("校验3:", "触发告警" if any(issubclass(x.category, ClusterWarning) for x in w) else "无告警")
```

**需要观察的现象**：

- (1) 抛出 `Invalid method: typo`。
- (2) 抛出 ``method=ward` requires the distance metric to be Euclidean``。
- (2′) 不报错，正常返回 \((n-1)\times 4\) 的 Z。
- (3) 打印「触发告警」。

**预期结果**：与上述现象一致——这正对应源码里三个独立的校验/告警分支。（数值结果待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么校验 2（欧氏约束）要带 `y.ndim == 2` 这个条件，而对 1D 输入不做校验？

**参考答案**：2D 输入意味着 `linkage` 即将用 `metric` 自己调 `pdist` 算距离，此时函数「知道」用户想用什么度量，因此有能力拦截「ward 配 cityblock」这种非法组合。1D 输入是用户预先算好的距离矩阵，函数无法反推度量，强行校验会误伤合法用法，故选择「放行 + 文档警示」——把正确性责任交给用户。

**练习 2**：2D 路径里那段「对角线全 0、非负、对称」的检测，为什么用 `xpx.isclose` 而不是 `==`？

**参考答案**：浮点距离矩阵的对角线和对称性可能因计算误差有极小偏差，`==` 会漏判；`xpx.isclose` 容忍浮点误差，且是 array API 兼容写法（兼容 NumPy/JAX/Dask 等后端，见 u2-l1）。

---

### 4.4 方法分派：`cy_linkage` 闭包与三路 Cython 后端

#### 4.4.1 概念说明

校验与归约完成后，`linkage()` 把实际聚类交给一个**内嵌闭包** `cy_linkage(y, validate)`。这个闭包做的就是本讲的「核心动作」——**把七种方法分派到三个 Cython 后端函数**：

| 方法 | 后端函数 | 算法 | 复杂度 |
|------|---------|------|--------|
| `single` | `_hierarchy.mst_single_linkage` | 最小生成树（Prim 风格） | \(O(n^2)\) |
| `complete`/`average`/`weighted`/`ward` | `_hierarchy.nn_chain` | 最近邻链（nearest-neighbor chain） | \(O(n^2)\) |
| `centroid`/`median` | `_hierarchy.fast_linkage` | 朴素算法（堆 + 距离更新表） | \(O(n^3)\) |

为什么是这三组、而不是七组？因为聚类算法的可用性取决于方法的**数学性质**（尤其是「可还原性 reducibility」），而非方法名：

- `single` 等价于求距离矩阵的最小生成树，有专用的高效 MST 算法。
- `complete/average/weighted/ward` 都是**可还原方法**，最近邻链算法能保证对这类方法在 \(O(n^2)\) 完成。
- `centroid/median` **不是可还原方法**（合并可能出现「距离倒挂/反转 inversion」，即新簇到某簇的距离反而小于合并前），最近邻链的正确性前提被破坏，只能退回朴素的 \(O(n^3)\) `fast_linkage`。

> 这正是为什么 `ward` 虽属于 `_EUCLIDEAN_METHODS`，却和 `complete/average/weighted` 同组走 `nn_chain`——是否欧氏与是否可还原是两个**独立**的维度。可还原性的严格定义与三种算法的实现细节见 u4。

闭包外面还套了一层 `xpx.lazy_apply(...)`，作用是让这条 Cython 调用链能透明地处理 dask 等惰性数组（拆块计算再合并）。其机理是 u3-l3 的主题，本讲只把它当成「调用 `cy_linkage` 的一种方式」。

#### 4.4.2 核心流程

```
cy_linkage(y, validate):
    if validate and 存在非有限值: raise ValueError
    ┌─ method == 'single'                                   → mst_single_linkage(y, n)
    ├─ method in ('complete','average','weighted','ward')   → nn_chain(y, n, method_code)
    └─ else (即 'centroid','median')                        → fast_linkage(y, n, method_code)

result = xpx.lazy_apply(cy_linkage, y, ...)     # 对惰性数组透明地套用 cy_linkage
if optimal_ordering: return optimal_leaf_ordering(result, y)
else:               return result
```

#### 4.4.3 源码精读

闭包本体——七种方法的三路 `if/elif/else`：

```python
def cy_linkage(y, validate):
    if validate and not np.all(np.isfinite(y)):
        raise ValueError("The condensed distance matrix must contain only finite values.")

    if method == 'single':
        return _hierarchy.mst_single_linkage(y, n)
    elif method in ('complete', 'average', 'weighted', 'ward'):
        return _hierarchy.nn_chain(y, n, method_code)
    else:
        return _hierarchy.fast_linkage(y, n, method_code)
```

参见 [_hierarchy_impl.py:L955-L965](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L955-L965)。注意三点：

1. 闭包捕获了外层的 `method`（字符串，用于 `if` 判断）、`method_code`（整数，传给后端函数指针表）、`n`（观测数）。`nn_chain` 和 `fast_linkage` 都需要 `method_code` 来在内部查 Lance-Williams 更新函数（见 u3-l4）；而 `mst_single_linkage` 只服务于 `single` 一种方法，故无需 `method_code`。
2. 闭包内还有一个二次的有限性检查，参数 `validate` 由外层 `lazy_apply` 根据输入是否惰性来决定（惰性输入会在拆块后逐块校验）。
3. `else` 分支覆盖的就是 `centroid/median`——即 `_EUCLIDEAN_METHODS` 减去 `ward` 后剩下的两个。

调用入口与收尾：

```python
result = xpx.lazy_apply(cy_linkage, y, validate=lazy,
                        shape=(n - 1, 4), dtype=xp.float64,
                        as_numpy=True, xp=xp)

if optimal_ordering:
    return optimal_leaf_ordering(result, y)
else:
    return result
```

参见 [L967-L974](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L967-L974)。`shape=(n-1, 4)` 正是 linkage matrix 的标准形状（见 u3-l1）；`as_numpy=True` 表示最终结果强制转成 NumPy 数组（Cython 后端本来就返回 NumPy 数组，这里主要影响惰性输入路径）。若 `optimal_ordering=True`，则在已有 Z 的基础上再跑一次 `optimal_leaf_ordering` 重排叶子（见 u6-l3），注意它**只改叶序、不改聚类结构**。

#### 4.4.4 代码实践

**实践目标**：对同一个压缩距离矩阵跑全部七种方法，记录 `Z[:,2]`（合并距离列），并定性解释差异。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.spatial.distance import pdist
from scipy.cluster.hierarchy import linkage

# 用 linkage docstring 里的 8 个一维观测点：值 = [2,8,0,4,1,9,9,0]
X = np.array([[i] for i in [2, 8, 0, 4, 1, 9, 9, 0]], dtype=float)
y = pdist(X)                      # 1D 压缩距离矩阵
print("n =", len(X), " condensed len =", len(y))

methods = ['single', 'complete', 'average', 'weighted', 'centroid', 'median', 'ward']
for m in methods:
    Z = linkage(y, method=m)
    print(f"{m:10s} 距离列 = {np.round(Z[:,2], 3)}")
```

**需要观察的现象与定性解释**（具体数值待本地验证）：

- **`single`**：距离列里出现 0（因为有两个 0 点、两个 9 点，最近点对距离为 0），且整体距离范围最小——它每步取的是「跨簇点对的最小距离」，是七种方法里最「乐观」的。
- **`complete`**：距离列最大——每步取「跨簇点对的最大距离」，是七种方法里最「悲观」的；最后一次合并的距离接近数据直径（约 9）。
- **`average`/`weighted`**：介于 single 与 complete 之间，是两者的折中。
- **`centroid`/`median`**：基于质心，距离可能**非单调**（出现倒挂），这是它们必须用 `fast_linkage` 的根本原因；在某些数据上你甚至可能看到后一步的合并距离比前一步更小。
- **`ward`**：距离整体偏大。因为 Ward 的更新公式（见 [L828-L835](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L828-L835)）

  \[ d(u,v) = \sqrt{\tfrac{|v|+|s|}{T}d(v,s)^2 + \tfrac{|v|+|t|}{T}d(v,t)^2 - \tfrac{|v|}{T}d(s,t)^2},\quad T=|v|+|s|+|t| \]

  带有基数加权和开方，量纲与原始点对距离不同，故绝对值通常更大。

**预期结果**：`single` 的距离列含 0 且最大值最小；`complete` 的最大值最大；`ward` 的数值整体最大；`centroid/median` 可能非单调。这些定性规律与源码各方法的数学定义一一对应。

#### 4.4.5 小练习与答案

**练习 1**：`cy_linkage` 里 `method_code` 这个变量传给了 `nn_chain` 和 `fast_linkage`，却没传给 `mst_single_linkage`，为什么？

**参考答案**：`mst_single_linkage` 专属于 `single` 一种方法，方法已确定，无需再用编码去查距离更新函数；而 `nn_chain` 要服务 `complete/average/weighted/ward` 四种、`fast_linkage` 要服务 `centroid/median` 两种，它们内部需要用 `method_code` 在 Lance-Williams 函数指针表里查出当前方法对应的距离更新公式（见 u3-l4）。

**练习 2**：假如某天有人提出 `ward` 不再是可还原方法（仅作假设），`cy_linkage` 的分派需要怎么改？

**参考答案**：需把 `ward` 从 `nn_chain` 分支的元组里移到 `else` 分支，让它走 `fast_linkage`。这正是分派逻辑与「可还原性」紧耦合的体现——分组的依据是方法的数学性质，不是方法名本身。

---

## 5. 综合实践

把本讲的全部分派知识串起来，完成一个「分派追踪 + 行为对比」的小任务。

**任务**：给一个 2D 观测矩阵 `X`，完成以下三件事，并解释每一步「走的是源码里的哪个分支」。

```python
# 示例代码
import numpy as np
from scipy.spatial.distance import pdist
from scipy.cluster.hierarchy import linkage

X = np.array([[0.,0.],[0.,1.],[1.,0.],[5.,5.],[5.,6.],[6.,5.]], dtype=float)
y = pdist(X)
```

1. **追踪欧氏校验**。先解释：为什么 `linkage(X, method='ward', metric='cityblock')` 会报错，而 `linkage(y, method='ward')`（`y` 是用 cityblock 算的压缩距离矩阵）不报错？对照 [L930-L932](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L930-L932) 说明 `y.ndim` 在这里的决定性作用。

2. **追踪分派分支**。对 `methods = ['single','complete','average','weighted','centroid','median','ward']`，逐个调用 `linkage(y, method=m)`，并按下表填写每个方法命中的 Cython 后端（`mst_single_linkage` / `nn_chain` / `fast_linkage`）。答案应与 4.4.1 的表格一致。

   | method | 后端 |
   |--------|------|
   | single | ? |
   | complete | ? |
   | ... | ? |

3. **解释距离差异**。打印七种方法的 `Z[:,2]`，用一句话分别解释 `single` 为何最小、`complete` 为何最大、`ward` 为何偏大、`centroid/median` 为何可能非单调。所有解释都要能对应回 `linkage` docstring 里各方法的数学定义（[L773-L841](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L773-L841)）。

**验收标准**：能不看讲义，凭源码里的 `if/elif/else` 说出任意一个 `method` 会落到哪个后端函数，并能解释「为什么 `ward` 走 `nn_chain` 而 `centroid` 走 `fast_linkage`」。

## 6. 本讲小结

- `scipy.cluster.hierarchy` 的聚类逻辑全部集中在 `linkage()`；七个 `single/complete/...` 只是固定 `method` 后转发 `linkage` 的薄包装。
- `_LINKAGE_METHODS` 把方法名映射为整数编码（供 Cython 函数指针表用），`_EUCLIDEAN_METHODS = ('centroid','median','ward')` 标记必须配欧氏度量的方法。
- `linkage()` 的 Python 层做三件事：规整/校验输入、把 1D/2D 输入统一归约成压缩距离矩阵、按方法分派后端。
- 欧氏度量约束**只在 2D 观测矩阵输入时校验**；传 1D 预算距离矩阵时不校验，正确性由用户自负。
- 分派核心是 `cy_linkage` 闭包：`single→mst_single_linkage`，`complete/average/weighted/ward→nn_chain`，`centroid/median→fast_linkage`。
- 分组依据是方法的**可还原性**而非方法名：`centroid/median` 不可还原（可能距离倒挂），故只能用 \(O(n^3)\) 的 `fast_linkage`；`ward` 虽属欧氏方法但可还原，故仍走 `nn_chain`。

## 7. 下一步学习建议

- **想理解桥接层**：`lazy_cython` 装饰器如何让 Cython 后端处理 dask 惰性数组、`xpx.lazy_apply` 如何拆块——见 **u3-l3 Python 与 Cython 的桥接**。
- **想理解距离更新**：`nn_chain`/`fast_linkage` 内部如何用 `method_code` 查 Lance-Williams 更新公式——见 **u3-l4 Lance-Williams 距离更新公式**。
- **想理解三种算法**：`mst_single_linkage` 的 Prim 实现、`nn_chain` 的最近邻链、`fast_linkage` 的堆与并查集——见 **u4 层次聚类核心算法（Cython 后端）**。
- **想理解 Z 的后续用途**：得到 Z 之后如何切平成簇（`fcluster`）、如何可视化（`dendrogram`）——见 **u5** 与 **u6**。

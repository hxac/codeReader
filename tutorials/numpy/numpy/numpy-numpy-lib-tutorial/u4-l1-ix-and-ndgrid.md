# ix_ 与 nd_grid 网格构造

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `np.ix_` 解决的「交叉索引」问题，并能用「广播」解释它返回的数组为什么是 `(n,1)`、`(1,m)` 这种形状。
- 读懂 `nd_grid.__getitem__` 如何把一串切片 `start:stop:step` 解析成网格坐标，并理解「复数步长 = 点数」这一约定。
- 区分 `mgrid`（密集，单个堆叠数组）与 `ogrid`（稀疏，开放网格元组）的输出形态，并说出 `ogrid` 的输出与 `ix_` 形态等价这一关键事实。

本讲只聚焦「用索引语法构造坐标网格」这一件事，所有代码都来自 [`_index_tricks_impl.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py)。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：花式索引会广播。** 当你写 `a[i, j]`，而 `i`、`j` 是数组时，NumPy 会先把它们按广播规则对齐，再在每一对 `(i[k], j[k])` 处取值。如果 `i.shape=(2,1)`、`j.shape=(1,2)`，二者广播成 `(2,2)`，于是取到的是「`i` 的所有行」与「`j` 的所有列」的**全部组合**——这正是「交叉索引（cross index）」想要的效果。

**直觉二：「开放网格」就是只填一维的形状。** 形如 `(3,1)` 与 `(1,5)` 的两个数组，本身很省内存（只存了 3+5=8 个数），但它们广播后能覆盖整个 `(3,5)` 平面。我们把这种「每一项只有一维大于 1」的形态叫**开放网格（open mesh）**，把「每一项都长成完整 `(3,5)`」的形态叫**密集网格（dense / fleshed out）**。

**直觉三：方括号也是一种「函数调用」。** `np.mgrid[0:5, 0:5]` 看起来像取切片，其实是触发了 `MGridClass.__getitem__`。`mgrid`、`ogrid`、`r_`、`c_` 都是「**把方括号里的内容当参数**」的实例对象，这是 NumPy 一类「索引技巧（index tricks）」的共同写法。

承接 [u1-l2](u1-l2-module-organization.md)：本讲涉及的 `ix_`、`mgrid`、`ogrid` 都属于「无私有薄模块、由顶层 `numpy/__init__.py` 直接从 `_index_tricks_impl` 取名」的一类，且 `ix_` 是其中唯一使用 `array_function_dispatch` 装饰器的（可被 NEP-18 `__array_function__` 拦截）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`_index_tricks_impl.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py) | 本讲全部实现的所在文件：`ix_`、`nd_grid`、`MGridClass`、`OGridClass` 四个最小模块都在这里。 |
| [numpy/__init__.py:510-525](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py#L510-L525) | 顶层再导出处。`ix_`、`mgrid`、`ogrid` 没有私有薄模块，直接 `from .lib._index_tricks_impl import (...)` 取名后暴露为 `np.ix_` 等。 |

补充：`nd_grid` 的网格数值最终由 `_nx.indices`（密集路径）与 `_nx.arange`（稀疏路径）生成，二者来自 `numpy._core.numeric`（[_index_tricks_impl.py:7](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L7) 的 `import numpy._core.numeric as _nx`）。本讲只讲 lib 层如何调度它们，不展开 core。

一个细节：`nd_grid`/`MGridClass`/`OGridClass` 都**没有** `@set_module('numpy')` 装饰（对比同文件的 `ndenumerate`、`ndindex` 有），因此 `type(np.mgrid).__module__` 是 `numpy.lib._index_tricks_impl`，而非 `numpy`。这不影响使用，只是回忆 [u1-l1](u1-l1-overview.md) 的「看 `__module__`」练习时会留意到。

## 4. 核心概念与源码讲解

### 4.1 ix_：从一维序列构造开放网格

#### 4.1.1 概念说明

`np.ix_` 解决的问题是：**给定若干个一维下标序列，快速拼出一组能做「交叉索引」的索引数组。**

例如对 `a = np.arange(10).reshape(2,5)`，想一次性取出「第 0、1 行」与「第 2、4 列」的全部组合（共 4 个元素），写成 `a[[0,1], [2,4]]` 是错的——它会被当成「逐对配对」`(a[0,2], a[1,4])`，只取 2 个元素且要求形状匹配。正确做法是把两个下标序列改成可广播的开放网格：第 0 维下标摆成 `(2,1)`，第 1 维下标摆成 `(1,2)`，再用 `a[(arr0, arr1)]` 索引。`ix_` 就是帮你做这个「摆形状」的动作。

它返回**一个元组**，元组里第 k 个数组的形状是「第 k 维非 1、其余维全 1」——这正是上一节定义的开放网格。

#### 4.1.2 核心流程

`ix_` 的算法很直白，对 N 个输入序列循环：

1. 把每个输入转成 1 维 `ndarray`（空数组显式转成 `intp`，避免默认成浮点）。
2. 若是布尔数组，先 `nonzero()` 转成整数下标（等价于「取 True 的位置」）。
3. 把第 k 个数组 `reshape` 成 `(1, …, 1, len, 1, …, 1)`：前 k 个 1、中间放长度、后面再补 `N-k-1` 个 1。

伪代码：

```
out = []
nd = len(args)
for k, new in enumerate(args):
    new = asarray_1d(new)            # 强制 1 维
    if new.dtype == bool: new = new.nonzero()[0]
    shape = (1,)*k + (new.size,) + (1,)*(nd-k-1)
    out.append(new.reshape(shape))
return tuple(out)
```

关键就是第 3 步那个形状：第 k 维非 1、其余维全 1。这一步之后，N 个数组天然就能按广播规则铺成完整的 N 维网格。

#### 4.1.3 源码精读

dispatcher 与公开函数定义在 [_index_tricks_impl.py:27-32](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L27-L32)：`_ix__dispatcher` 原样返回所有参数，是为了支持 NEP-18 的 `__array_function__` 派发（见 [u1-l2](u1-l2-module-organization.md) 的 dispatcher 模式）。

核心实现 [_index_tricks_impl.py:90-104](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L90-L104)：

```python
out = []
nd = len(args)
for k, new in enumerate(args):
    if not isinstance(new, _nx.ndarray):
        new = np.asarray(new)
        if new.size == 0:
            # Explicitly type empty arrays to avoid float default
            new = new.astype(_nx.intp)
    if new.ndim != 1:
        raise ValueError("Cross index must be 1 dimensional")
    if issubdtype(new.dtype, _nx.bool):
        new, = new.nonzero()
    new = new.reshape((1,) * k + (new.size,) + (1,) * (nd - k - 1))
    out.append(new)
return tuple(out)
```

逐点说明：

- **空数组特判**（第 95-97 行）：`np.asarray([])` 默认是 `float64`，但当下标用必须是整数，所以显式 `astype(_nx.intp)`。
- **必须 1 维**（第 98-99 行）：交叉索引的语义要求每个序列对应一个维度，多维输入直接报错。
- **布尔转下标**（第 100-101 行）：`new.nonzero()` 返回一个元组（1 维数组时只有一个元素），用 `new, =` 解包。等价于「传 True/False 掩码」。
- **形状重塑**（第 102 行）：`(1,)*k + (new.size,) + (1,)*(nd-k-1)` 就是「前 k 个 1、长度、后 `nd-k-1` 个 1」。

> 与 `ogrid` 的伏笔：注意第 102 行产出的形状 `(1,…,len,…,1)` 与 4.4 节 `ogrid` 输出的形状完全一致——`ix_` 与 `ogrid` 在「形态」上是同一件事，只是输入一个是「任意序列」、一个是「切片区间」。

#### 4.1.4 代码实践

**实践目标**：用 `ix_` 取交叉子块，并验证它返回的就是「可广播的开放网格」。

操作步骤（可在装好 numpy 的任意 Python 环境运行，示例代码）：

```python
import numpy as np

a = np.arange(10).reshape(2, 5)
g = np.ix_([0, 1], [2, 4])
print(g[0].shape, g[1].shape)   # (2, 1) (1, 2)
print(a[g])                      # [[2, 4], [7, 9]]

# 布尔掩码等价写法
g2 = np.ix_([True, True], [False, False, True, False, True])
print(a[g2])                     # 同样 [[2, 4], [7, 9]]
```

需要观察的现象：

1. `g[0]` 形状 `(2,1)`、`g[1]` 形状 `(1,2)`——开放网格。
2. `a[g]` 形状 `(2,2)`——正好是两个序列长度的笛卡尔积。
3. 布尔掩码 `[True,True]` 与整数 `[0,1]` 结果一致。

预期结果：`a[g] == [[2,4],[7,9]]`。如运行结果与此不符，「待本地验证」环境是否装的是兼容版本的 numpy。

#### 4.1.5 小练习与答案

**练习 1**：`np.ix_([0,1,2], [0,1,2,3,4])` 返回的两个数组形状分别是什么？

答案：`(3,1)` 与 `(1,5)`。

**练习 2**：为什么 `a[[0,1], [2,4]]`（不加 `ix_`）取不到「2 行 × 2 列」的全部组合？

答案：不包装时，NumPy 把两个等长的整数数组当作「逐对配对」的花式索引，只取 `a[0,2]` 与 `a[1,4]` 两个元素；要用 `ix_` 把它们改成 `(2,1)` 与 `(1,2)` 才会广播成全部 4 个组合。

**练习 3**：传入 `np.ix_([True, False, True])`（单个布尔序列）会发生什么？

答案：布尔序列被 `nonzero()` 转成 `[0, 2]`，再 reshape 成 `(2,)`（因为 `nd=1`，前后都没有 1）。返回 `(array([0,2]),)`。

---

### 4.2 nd_grid：用切片构造网格的通用引擎

#### 4.2.1 概念说明

`nd_grid` 是一个**类**，它的实例在被「用方括号索引」时返回一个坐标网格。它有两个公开的预定义实例：

- `mgrid = nd_grid(sparse=False)` —— 密集
- `ogrid = nd_grid(sparse=True)` —— 稀疏

本节只讲**引擎本身**——也就是 `__getitem__` 如何解析方括号里的切片、如何决定网格数值与数据类型、如何区分「普通步长」与「复数步长（点数）」。`sparse` 标志在 4.3、4.4 两节分别展开。

#### 4.2.2 核心流程

`nd_grid.__getitem__` 接收一个切片元组（如 `(slice(0,5,1), slice(0,5,1))`），主流程分四步：

1. **解析每个切片**，算出每一维的「点数」`size`，并收集所有 `start/stop/step` 到 `num_list`。
2. **决定数据类型** `typ = result_type(*num_list)`。
3. **生成原始整数网格**：稀疏走「每维一个 `arange`」，密集走 `indices(size)`。
4. **缩放平移**：对每一维 `nn[k] = nn[k] * step + start`，把整数序号变成实际坐标。

复数步长是唯一的「魔法」：当 `step` 是复数（如 `5j`）时，它的模长 `abs(5j)=5` 被解释为**点数**，而非步长；此时 `stop` 是**包含**的。设点数为 \(N\)，则该维生成 \(N\) 个等距点：

\[
x_i = \text{start} + i\cdot\frac{\text{stop}-\text{start}}{N-1},\quad i=0,1,\ldots,N-1
\]

这与 `np.linspace(start, stop, N, endpoint=True)` 等价。而普通步长则等价于 `np.arange(start, stop, step)`，`stop` **不包含**。

> 单切片回退：若方括号里只有一个切片（如 `mgrid[0:4]`），`len(key)` 会抛 `TypeError`，被 `except` 捕获后走更简单的回退分支，直接返回一个 1 维 `arange`/`linspace`。

#### 4.2.3 源码精读

类骨架 [_index_tricks_impl.py:141-144](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L141-L144)：`__slots__ = ('sparse',)` 只存一个标志位，`__init__` 记下 `sparse`。

主路径在 [_index_tricks_impl.py:146-192](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L146-L192)，关键片段：

```python
for k in range(len(key)):
    step = key[k].step
    start = key[k].start
    stop = key[k].stop
    if start is None: start = 0
    if step is None:  step = 1
    if isinstance(step, (_nx.complexfloating, complex)):
        step = abs(step)
        size.append(int(step))               # 复数步长 → 点数
    else:
        size.append(math.ceil((stop - start) / step))  # 普通步长 → 点数
    num_list += [start, stop, step]
typ = _nx.result_type(*num_list)              # 由所有切片参数推 dtype
if self.sparse:
    nn = [_nx.arange(_x, dtype=_t) for _x, _t in zip(size, (typ,)*len(size))]
else:
    nn = _nx.indices(size, typ)               # 密集：一次生成
for k, kk in enumerate(key):
    ...
    nn[k] = (nn[k] * step + start)            # 缩放平移：序号 → 坐标
```

逐点说明：

- **点数计算**（普通步长，第 164-165 行）：`math.ceil((stop-start)/step)` 与 `np.arange` 的元素个数一致（向上取整）。
- **类型推断**（第 151、167 行）：`num_list = [0]` 预置一个 `0`（注释说「至少与 `np.int_` 一样大」），再把每维的 `start/stop/step` 追加进去，最后 `result_type` 一次性推出结果 dtype——纯整数切片得整数网格，只要有一个是浮点就提升为浮点。
- **复数步长的缩放**（第 180-183 行）：

  ```python
  step = int(abs(step))
  if step != 1:
      step = (kk.stop - start) / float(step - 1)
  ```
  
  即把「点数 N」换算回真正的步长 `(stop-start)/(N-1)`，再交给统一的 `nn[k]*step+start`。当 `N==1`（`1j`）时不除，得到单点 `[start]`。
- **单切片回退**（[_index_tricks_impl.py:193-208](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L193-L208)）：处理 `key` 是单个 slice、无法 `len()` 的情况，复数步长分支额外用 `typ = result_type(start, stop, step_float)` 防止生成整数数组（因为缩放会引入小数）。

#### 4.2.4 代码实践

**实践目标**：亲手验证「普通步长不包含 stop」与「复数步长包含 stop」的差异。

```python
import numpy as np

# 普通步长：等价于 arange，stop 不含
print(np.mgrid[0:1:0.25])
# 期望：[0.   0.25 0.5  0.75]   —— 注意没有 1.0

# 复数步长：等价于 linspace，stop 含
print(np.mgrid[0:1:5j])
# 期望：[0.   0.25 0.5  0.75 1. ]   —— 包含 1.0，共 5 个点
```

需要观察的现象：

1. 普通步长 `0:1:0.25` 末尾停在 `0.75`（4 个点），不含 `1.0`。
2. 复数步长 `0:1:5j` 末尾正好是 `1.0`（5 个点），与上式套用 \(x_i = i\cdot\tfrac{1-0}{5-1}\) 一致。

预期结果：如上注释所示。若想确认数据类型，可加 `print(np.mgrid[0:5].dtype)`（纯整数切片应得整数类型）。

#### 4.2.5 小练习与答案

**练习 1**：`np.mgrid[-1:1:5j]` 的输出是什么？

答案：`[-1. , -0.5,  0. ,  0.5,  1. ]`——5 个等距点，两端都包含。

**练习 2**：为什么 `nd_grid` 要在 `num_list` 最前面预置一个 `0`？

答案：注释说「使用至少与 `np.int_` 一样大的类型」。预置 `0`（Python int）参与 `result_type`，使纯整数切片得到平台默认整数类型，行为与 `np.arange` 对齐，而不是退化成更窄的类型或默认浮点。

**练习 3**：`np.mgrid[0:4]` 走的是主路径还是回退分支？为什么？

答案：回退分支。`0:4` 是单个 `slice` 对象，主路径里 `len(key)` 会抛 `TypeError`（slice 没有 `len`），被 `except (IndexError, TypeError)` 捕获后走第 193-208 行，直接 `arange(0,4)` 返回 1 维数组。

---

### 4.3 MGridClass：密集网格

#### 4.3.1 概念说明

`MGridClass` 是 `nd_grid` 的子类，构造时固定 `sparse=False`，实例化为全局对象 `mgrid`。它的语义是「**密集**网格」：每个维度的坐标都被「铺满」成相同的完整形状，最后把所有维度沿第 0 轴**堆叠**成一个数组返回。

例如 `np.mgrid[0:5, 0:5]` 返回一个形状为 `(2, 5, 5)` 的数组：`[0]` 是「行坐标」层（每一行填同一个行号），`[1]` 是「列坐标」层（每一列填同一个列号）。注意返回的是**单个 ndarray**，不是元组。

#### 4.3.2 核心流程

密集路径在 4.2 的通用流程里只占两行，但语义关键：

1. `nn = _nx.indices(size, typ)`：`indices((n0,n1,...))` 返回形状 `(ndim, n0, n1, ...)` 的数组，其中第 k 层给出「沿第 k 维的坐标」——这正是把 4.2 算出的整数序号预先排成完整网格。
2. 对每一层做 `nn[k] = nn[k]*step + start`，把序号换成实际坐标。
3. 因为 `sparse=False`，跳过「插 newaxis」那段，直接 `return nn`——返回的就是堆叠后的单个数组。

也就是说，密集 = 「`indices` 一次性把开放网格广播成实心」+「逐层缩放」。

#### 4.3.3 源码精读

`MGridClass` 的定义极薄 [_index_tricks_impl.py:211-270](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L211-L270)：

```python
class MGridClass(nd_grid):
    __slots__ = ()
    def __init__(self):
        super().__init__(sparse=False)

mgrid = MGridClass()
```

真正干活的是 `nd_grid.__getitem__` 里与 `sparse` 相关的两处：

- **生成阶段**（[_index_tricks_impl.py:171-172](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L171-L172)）：`else: nn = _nx.indices(size, typ)` —— 密集路径用 `indices` 一次生成完整网格。
- **返回阶段**（[_index_tricks_impl.py:192](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L192)）：`return nn  # mgrid -> ndarray` —— 密集直接返回堆叠数组，不插 `newaxis`。

「`MGridClass` 自己几乎没代码」正是 `nd_grid` 引擎设计的回报：把 `sparse` 标志抽到基类，子类只负责「选哪个标志」。

#### 4.3.4 代码实践

**实践目标**（本讲指定实践之一）：用 `mgrid` 构造一个 5×5 坐标网格，看清它的形状。

```python
import numpy as np

G = np.mgrid[0:5, 0:5]
print(G.shape)       # (2, 5, 5)
print(G[0])          # 行坐标层：每行同号
print(G[1])          # 列坐标层：每列同号
```

需要观察的现象：

1. `G.shape == (2, 5, 5)`——**不是** `(5, 5)`！第 0 轴是「坐标分量」，后两轴才是网格平面。
2. `G[0]` 的每一**行**都填同一个数（行号），`G[1]` 的每一**列**都填同一个数（列号）。

预期结果：与 [MGridClass 文档示例](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L242-L252)一致。

#### 4.3.5 小练习与答案

**练习 1**：`np.mgrid[0:4, 0:5, 0:6].shape` 是什么？

答案：`(3, 4, 5, 6)`——切片数决定第 0 轴大小，三个区间长度决定后三维。

**练习 2**：若想把 `G` 的「行坐标」与「列坐标」分别拿到两个 `(5,5)` 数组，怎么取？

答案：`rows, cols = G[0], G[1]`。或直接用 `ogrid`（见 4.4）省内存。

**练习 3**：`np.mgrid[0:5, 0:5]` 与 `np.indices((5,5))` 的数值有什么关系？

答案：前者在纯整数切片下就是后者再经过「缩放平移」；由于 `start=0, step=1`，二者数值完全相等，只是 `mgrid` 还能接受任意 `start:stop:step`（含复数步长）。

---

### 4.4 OGridClass：稀疏网格（与 ix_ 形态等价）

#### 4.4.1 概念说明

`OGridClass` 也是 `nd_grid` 的子类，构造时固定 `sparse=True`，实例化为全局对象 `ogrid`。它的语义是「**稀疏/开放**网格」：每一维只保留自己那一维的长度，其余维度全为 1，最后返回一个**元组**。

例如 `np.ogrid[0:5, 0:5]` 返回 `(shape (5,1) 的数组, shape (1,5) 的数组)`。这两个数组广播后能覆盖整个 `(5,5)` 平面，但只占了 5+5=10 个数（而非密集的 50 个）。这正是 4.1 节 `ix_` 产出的同一形态。

> **本讲的核心串联**：`ogrid` 的输出形状 `(n,1)`/`(1,m)` 与 `ix_` 的输出形状**完全一致**。区别仅在于输入——`ogrid` 吃切片（均匀区间），`ix_` 吃任意一维序列（含布尔、非均匀下标）。

#### 4.4.2 核心流程

稀疏路径在通用流程里多了「插 `newaxis`」这一步：

1. `nn = [arange(n_k, dtype=typ) for each dim]`：每维生成一个 1 维 `arange`（长度 `n_k`）。
2. 对每个 `nn[k]` 做 `nn[k] = nn[k]*step + start`（缩放平移）。
3. **插 `newaxis`**：构造一个全为 `newaxis` 的切片表 `slobj`，对第 k 个数组把第 k 位换成 `slice(None)`，使该数组在第 k 维保留真实长度、其余维为 1。
4. 返回 `tuple(nn)`。

#### 4.4.3 源码精读

`OGridClass` 同样极薄 [_index_tricks_impl.py:273-322](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L273-L322)：

```python
class OGridClass(nd_grid):
    __slots__ = ()
    def __init__(self):
        super().__init__(sparse=True)

ogrid = OGridClass()
```

稀疏专属的「生成」与「插轴」逻辑在 `nd_grid.__getitem__`：

- **生成阶段**（[_index_tricks_impl.py:168-170](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L168-L170)）：`nn = [arange(_x, dtype=typ) ...]` —— 每维一个 1 维数组。
- **插 newaxis 阶段**（[_index_tricks_impl.py:185-191](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L185-L191)）：

  ```python
  if self.sparse:
      slobj = [_nx.newaxis] * len(size)
      for k in range(len(size)):
          slobj[k] = slice(None, None)
          nn[k] = nn[k][tuple(slobj)]
          slobj[k] = _nx.newaxis
      return tuple(nn)  # ogrid -> tuple of arrays
  ```
  
  这个 `slobj` 「拨盘」技巧很巧妙：初始全 `newaxis`，每次把第 k 位拨成 `slice(None)`（保留该维），取完视图后拨回 `newaxis`。结果是第 k 个数组形状变成 `(1,…,n_k,…,1)`——与 `ix_` 第 102 行 `reshape((1,)*k + (size,) + (1,)*(nd-k-1))` 的产物**逐位相同**。

#### 4.4.4 代码实践

**实践目标**（本讲指定实践之二）：用 `ogrid` 构造 5×5 坐标网格，并与 `mgrid`、`ix_` 对照形状。

```python
import numpy as np

# ogrid：稀疏
ox, oy = np.ogrid[0:5, 0:5]
print(ox.shape, oy.shape)        # (5, 1) (1, 5)

# mgrid：密集
gx, gy = np.mgrid[0:5, 0:5]
print(gx.shape, gy.shape)        # (5, 5) (5, 5)

# 形态对照：ogrid 与 ix_ 同构
ix_out = np.ix_(np.arange(5), np.arange(5))
print(ix_out[0].shape, ix_out[1].shape)   # (5, 1) (1, 5)

# 数值对照：广播后 ogrid 与 mgrid 完全一致
print(np.array_equal(ox + oy*0, gx))      # True
```

需要观察的现象：

1. `ogrid` 返回**两个**数组，形状 `(5,1)` 与 `(1,5)`（共 10 个数）。
2. `mgrid` 返回**两个**数组（拆开后），形状都是 `(5,5)`（共 50 个数）。
3. `ix_(arange(5), arange(5))` 的形状与 `ogrid` **完全相同**——证明二者形态等价。

预期结果：注释所示。可见「同样是 5×5 网格，`ogrid` 比 `mgrid` 省 5 倍内存」。

#### 4.4.5 小练习与答案

**练习 1**：`np.ogrid[0:5, 0:5]` 与 `np.ix_(range(5), range(5))` 返回的形状是否相同？

答案：相同，都是 `(5,1)` 与 `(1,5)`。这是本讲的串联结论。

**练习 2**：稀疏路径里，把 `slobj[k]` 拨成 `slice(None)` 的作用是什么？

答案：让第 k 个数组**只在第 k 维**保留真实长度，其余维仍是 `newaxis`（长度 1）。这样它就能与其它维的数组按广播规则拼出完整网格，而不实际复制数据。

**练习 3**：既然 `ogrid` 更省内存，什么时候仍该用 `mgrid`？

答案：当你需要**直接拿到铺满的坐标矩阵**（例如把 `G[0]`、`G[1]` 作为整体喂给某个不接受广播的函数、或需要持久化完整网格）时，`mgrid` 更顺手；若只是参与向量化运算，`ogrid` 因能靠广播达到相同结果而更省内存。

---

## 5. 综合实践

把本讲的三个工具串起来：在 5×5 的网格上求 \(f(x,y) = \sin(x) + \cos(y)\) 的值，并验证三种构造方式数值一致。

```python
import numpy as np

# 方式 A：ogrid（省内存，靠广播）
ox, oy = np.ogrid[0:5, 0:5]
fA = np.sin(ox) + np.cos(oy)

# 方式 B：mgrid（铺满）
gx, gy = np.mgrid[0:5, 0:5]
fB = np.sin(gx) + np.cos(gy)

# 方式 C：ix_（与 ogrid 同构，但用任意下标序列）
ix, iy = np.ix_(np.arange(5), np.arange(5))
fC = np.sin(ix) + np.cos(iy)

print(fA.shape, fB.shape, fC.shape)          # (5,5) 三者同形
print(np.array_equal(fA, fB), np.array_equal(fA, fC))   # True True
```

思考题（可不写代码）：

1. 三种方式算出的 `f` 数值为什么完全相同？——因为它们都只是「行坐标 + 列坐标」的不同打包，广播后等价。
2. 如果把网格改成 `0:1:100j, 0:1:100j`（100×100），`ogrid` 相对 `mgrid` 的内存优势会变大还是变小？——变大（密集是 10000 个/层，稀疏是 100 个/层）。

## 6. 本讲小结

- `np.ix_` 把若干个一维序列（含布尔掩码）reshape 成「第 k 维非 1、其余维为 1」的开放网格，用于花式索引的笛卡尔交叉取值；它返回一个元组。
- `nd_grid` 是「用方括号里的切片构造坐标网格」的通用引擎，核心是 `__getitem__`：解析切片 → 推 dtype（`result_type`）→ 生成整数序号 → 缩放平移成坐标。
- 「复数步长 = 点数」是关键约定：`start:stop:Nj` 等价于含端点的 `linspace`，而普通步长等价于不含端点的 `arange`。
- `MGridClass`（`sparse=False`）用 `_nx.indices` 一次性铺满，返回**单个堆叠数组**，形状 `(ndim,)+size`。
- `OGridClass`（`sparse=True`）用「每维一个 `arange` + 拨盘式插 `newaxis`」，返回**元组**，每项形状 `(1,…,n_k,…,1)`。
- **核心串联**：`ogrid` 的输出形状与 `ix_` 完全相同——两者是「开放网格」的两种输入入口（切片 vs 任意序列）。

## 7. 下一步学习建议

- 下一篇 [u4-l2：r_ 与 c_ 轴向拼接器](u4-l2-r-and-c-concatenator.md) 讲同一个文件里的 `AxisConcatenator`/`RClass`/`CClass`——它们和 `nd_grid` 一样是「把方括号当参数」的索引技巧对象，但语义从「构造网格」变成「沿轴拼接」，并会引入「字符串指令」这一更强的方括号语法。
- 想深入「花式索引为什么能广播」的底层，可去 `numpy/_core/src/multiarray/mapping.c` 读索引派发（超出 numpy.lib 范围，作为延伸阅读）。
- 若想验证本讲对 `_nx.indices` / `result_type` 的描述，可直接在 REPL 里 `help(np.indices)` 与 `help(np.result_type)` 对照本讲 4.2.3 的源码解读。

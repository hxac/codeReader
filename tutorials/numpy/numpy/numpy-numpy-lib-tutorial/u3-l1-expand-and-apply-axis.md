# 维度扩展与轴向应用

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `expand_dims` 如何在任意位置插入一个或多个长度为 1 的新轴，并理解它本质上是对 `reshape` 的一层薄包装、返回视图而非拷贝。
- 读懂 `apply_along_axis` 把任意「一维函数」沿某个轴铺开执行的实现套路：转置→切片→建缓冲→回填→再转置，并理解它为什么要「先跑一次」探测输出形状。
- 理解 `apply_over_axes` 如何在多个轴上反复调用一个函数，并在函数「少返回一维」时用 `expand_dims` 把那一维补回去。
- 掌握 `_make_along_axis_idx` 构造「正交花式索引」的过程，从而彻底分清 `take_along_axis` / `put_along_axis` 与普通 `take` 在广播语义上的差别。
- 能用这几个函数写出按行归一化、按行排序、按索引批量改写等真实数据处理代码。

## 2. 前置知识

进入源码前，先建立三个直觉。

**轴（axis）与维度（ndim）。** 一个形状为 `(2, 3, 4)` 的数组有 3 个维度（`ndim == 3`），轴编号从 `0` 开始：`axis=0` 是第一个维度（大小 2），`axis=1` 是第二个（大小 3），`axis=2` 是第三个（大小 4）。负数轴从末尾倒数：`axis=-1` 等同于 `axis=2`。本讲几乎所有函数都要先把「用户给的轴」规整成「非负整数轴」，这件事由 `normalize_axis_index`（单轴）和 `normalize_axis_tuple`（多轴）完成。

**视图（view）与 reshape。** `reshape` 在不改变元素总个数、且内存布局允许时，返回的是一个**视图**——它与原数组共享同一块内存，只是解读形状不同。`expand_dims` 的全部魔力就是算出新形状后调用 `reshape`，因此它几乎不拷贝数据。

**花式索引与广播。** 当用一组数组组成的元组去索引 `arr[i0, i1, ...]`，且这些数组形状能互相广播时，NumPy 会逐元素地组合索引，得到一个形状等于「广播后形状」的结果。本讲的 `_make_along_axis_idx` 正是利用这一点，把「沿轴逐切片取值」转换成一次花式索引。

此外，本讲所有公开函数都采用 [u1-l2](u1-l2-module-organization.md) 讲过的 **dispatcher + impl 双函数写法**：`@array_function_dispatch(_xxx_dispatcher)` 装饰的公开函数背后，dispatcher 只返回参与运算的数组参数（用于 NEP-18 的 `__array_function__` 协议），真正的逻辑写在被装饰函数体内。本讲不再重复这套机制，只聚焦算法本身。

## 3. 本讲源码地图

本讲的核心实现集中在一个文件里：

| 文件 | 作用 |
| --- | --- |
| `numpy/lib/_shape_base_impl.py` | 形状与维度操作函数的实现层，本讲覆盖 `expand_dims`、`apply_along_axis`、`apply_over_axes`、`take_along_axis`、`put_along_axis` 及内部辅助 `_make_along_axis_idx` |

该文件的函数没有像 `npyio.py` 那样的薄再导出模块，而是由顶层 `numpy/__init__.py` 直接取名并收进 `np.` 命名空间——这正是 [u1-l2](u1-l2-module-organization.md) 所述「无薄模块、顶层直接 `from .lib._shape_base_impl import ...`」的情形，可对照 [numpy/\_\_init\_\_.py:L567](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py#L567) 与 [numpy/\_\_init\_\_.py:L682](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py#L682)（用 `set(lib._shape_base_impl.__all__)` 收集公开名）确认。

涉及的两个外部依赖（用于轴规整与迭代）：

| 文件 | 作用 |
| --- | --- |
| `numpy/_core/numeric.py` | 提供 `normalize_axis_tuple`，把单/多轴规整为无重复的非负整数元组 |
| `numpy/lib/_index_tricks_impl.py` | 提供 `ndindex`，本讲在 `apply_along_axis` 中用它生成多维迭代坐标 |

测试与行为依据：

| 文件 | 作用 |
| --- | --- |
| `numpy/lib/tests/test_shape_base.py` | 对应单测，含 `TestApplyAlongAxis`、`TestTakeAlongAxis`、`TestPutAlongAxis` 等，可用于核对本讲描述的行为 |

## 4. 核心概念与源码讲解

### 4.1 expand_dims：在指定位置插入新维度

#### 4.1.1 概念说明

`expand_dims(a, axis)` 给数组**增加一个长度为 1 的新维度**。它最常见的用途是把一维数组「立起来」或「放平」：把形状 `(3,)` 变成 `(1, 3)`（行向量）或 `(3, 1)`（列向量）。它和 `a[np.newaxis, :]` 完全等价（`np.newaxis is None`），但 `expand_dims` 更适合写在参数化代码里，因为 `axis` 可以是变量，也可以是元组——一次插入多个新轴。

它解决的问题是：很多运算对维度数有硬性要求（矩阵乘法要求至少二维、广播对齐时需要显式的「哑维度」），而又不想真的拷贝数据。`expand_dims` 返回视图，零拷贝地把形状「撑」到目标维度数。它不只是公开 API，还是本讲 `apply_over_axes` 与同文件 `kron` 的内部依赖。

#### 4.1.2 核心流程

```
输入 a (ndim=k)、axis (int 或 tuple/list)
1. a = asanyarray(a)            # matrix 特例转 asarray
2. 若 axis 不是 tuple/list，包成 (axis,)
3. out_ndim = len(axis) + k     # 目标维度数
4. axis = normalize_axis_tuple(axis, out_ndim)   # 规整为非负、无重复
5. 用迭代器消费 a.shape，按下标决定每个位置是「1」还是「原维大小」
6. return a.reshape(new_shape)  # 视图，不拷贝
```

关键直觉：插入新轴后，结果共有 `out_ndim` 个位置。`normalize_axis_tuple` 是**针对 `out_ndim`（而不是 `a.ndim`）做规整的**——这正是「新轴可以插在末尾」的原因。

#### 4.1.3 源码精读

dispatcher 只返回数组本身，签名即公开函数：[_shape_base_impl.py:L509-L514](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L509-L514)

实现体分三段。第一段处理输入类型——`matrix` 子类是历史包袱，强制降级为普通 `asarray`，其余走 `asanyarray` 以保留子类：[_shape_base_impl.py:L586-L590](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L586-L590)

第二段是本函数的灵魂——把 `axis` 统一成元组，并**针对 `out_ndim` 而非 `a.ndim` 规整**：[_shape_base_impl.py:L591-L595](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L591-L595)

```python
if not isinstance(axis, (tuple, list)):
    axis = (axis,)
out_ndim = len(axis) + a.ndim
axis = normalize_axis_tuple(axis, out_ndim)
```

为什么是 `out_ndim`？因为新轴可插入的位置范围是 `0..a.ndim`（共 `a.ndim+1` 个缝隙），单轴插入时 `out_ndim = a.ndim + 1`，正好覆盖这 `a.ndim+1` 个缝隙。`normalize_axis_tuple` 的定义见 [numeric.py:L1429-L1483](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/numeric.py#L1429-L1483)：它先用 `operator.index` 把单个整数包成列表，再对每个轴调 `normalize_axis_index` 转非负，最后默认禁止重复轴（`allow_duplicate=False`）。

第三段用一个迭代器优雅地构造新形状——遍历 `out_ndim` 个位置，若该位置属于「新轴集合」就填 `1`，否则从原形状里取下一个大小：[_shape_base_impl.py:L597-L600](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L597-L600)

```python
shape_it = iter(a.shape)
shape = [1 if ax in axis else next(shape_it) for ax in range(out_ndim)]
return a.reshape(shape)
```

这一行列表推导同时处理了「多轴插入到不同位置」的复杂情况，是全函数最值得品味的写法。

#### 4.1.4 代码实践

1. 实践目标：验证 `expand_dims` 返回视图、可批量插轴，并体会「针对 `out_ndim` 规整」的边界效果。
2. 操作步骤（示例代码）：

```python
import numpy as np
x = np.array([1, 2])              # shape (2,) ，ndim=1
r = np.expand_dims(x, 0)          # 期望 (1, 2)
c = np.expand_dims(x, 1)          # 期望 (2, 1)，这是 1D「插到末尾」
m = np.expand_dims(x, (0, 1))     # 期望 (1, 1, 2)
print(r.shape, c.shape, m.shape)
print(np.shares_memory(x, r))     # 期望 True，说明是视图

# 演示「针对 out_ndim 规整」：对 2D 数组，axis 可以等于 a.ndim
a2 = np.arange(6).reshape(2, 3)   # ndim=2
e = np.expand_dims(a2, 2)         # out_ndim=3，axis=2 合法 → (2, 3, 1)
print(e.shape)                    # 期望 (2, 3, 1)
```

3. 需要观察的现象：前三者 `shape` 应为 `(1, 2)`、`(2, 1)`、`(1, 1, 2)`；`shares_memory` 应为 `True`；`e.shape` 应为 `(2, 3, 1)`。
4. 预期结果：如上。关键点：对 1D 数组 `x`，单轴插入时 `out_ndim = 1+1 = 2`，合法的 `axis` 只有 `0` 和 `1`，`np.expand_dims(x, 2)` 会抛 `AxisError`；而对 2D 数组 `a2`，`out_ndim = 2+1 = 3`，`axis=2`（等于 `a.ndim`）合法。这正是「规整针对 `out_ndim` 而非 `a.ndim`」带来的差别。
5. 「待本地验证」部分：以上结论可由源码直接推出，运行应一致；可额外尝试 `np.expand_dims(x, 2)` 以确认它确实抛 `AxisError`。

#### 4.1.5 小练习与答案

**练习 1**：对形状 `(2, 3)` 的数组 `a`，执行 `np.expand_dims(a, (0, 3))` 后结果形状是什么？

答案：`out_ndim = 2 + 2 = 4`，新轴在位置 `0` 和 `3`。遍历位置 `0,1,2,3`：位置 0∈axis→1；位置 1→`next` 取 2；位置 2→`next` 取 3；位置 3∈axis→1。结果形状 `(1, 2, 3, 1)`。

**练习 2**：对一维数组 `x`，`np.expand_dims(x, 1)` 与 `np.expand_dims(x, 2)` 一个合法、一个非法，为什么？

答案：单轴插入时 `out_ndim = a.ndim + 1 = 2`，`normalize_axis_tuple` 用 `out_ndim=2` 校验，合法轴只有 `0` 和 `1`（负数即 `-2,-1`）。`axis=1` 合法 → 形状 `(2,1)`，这就是一维数组的「插到末尾」；`axis=2` 越界 → 抛 `AxisError`。可见一维数组插一个轴，可插入的位置是 `0` 和 `1`，而非 `0` 和 `2`——判断边界前先算出 `out_ndim` 即可避免这类错误。

---

### 4.2 apply_along_axis：沿轴对一维切片应用函数

#### 4.2.1 概念说明

`apply_along_axis(func1d, axis, arr)` 把一个**只能处理一维数组的函数 `func1d`**，沿 `arr` 的指定轴「铺开」执行。例如 `func1d = np.sum`、`axis = 0`，就等价于沿 `axis=0` 求和；但 `func1d` 可以是任意自定义函数（比如「对一行做标准化」），这是普通 `sum`/`mean` 做不到的。

它解决的问题是：你手上有一个现成的一维函数（可能是第三方库的、或自己写的复杂逻辑），想把它无脑套到 N 维数组的每个一维切片上，又不想手写嵌套循环。官方文档明确指出它「等价于但快于」一段用 `ndindex` + `s_` 写出的双重循环——「快于」的来源是它把每次结果写到一块连续缓冲上，而不是临时拼装。

#### 4.2.2 核心流程

设 `arr` 形状为 `(Ni..., M, Nk...)`，即轴 `axis` 大小为 `M`，其前各有 `Ni...` 维、其后各有 `Nk...` 维。流程如下：

```
1. 把 axis 转置到最后：inarr_view 形状 (Ni..., Nk..., M)
2. inds = 所有 (Ni..., Nk...) 的迭代坐标，每条末尾补 Ellipsis
3. 取第一条坐标 ind0，先调用一次 func1d 探测输出形状 res.shape（Nj...）
4. 建缓冲 buff，形状 = (Ni..., Nk..., Nj...)，dtype 同 res
5. 把每个切片的 func1d 结果写入 buff
6. 用 buff_permute 把 Nj... 这几维从末尾搬回 axis 位置
7. conv.wrap(res) 还原输入类型
```

最终输出形状为 `(Ni..., Nj..., Nk...)`：原 `axis` 那一维被 `func1d` 的输出形状替换。若 `func1d` 返回标量（`Nj...` 为空），输出比输入少一维；若返回一维数组，维度数不变；若返回更高维数组，新维度插在 `axis` 处。

\[ \text{out.shape} = (N_{i\dots},\ N_{j\dots},\ N_{k\dots}) \quad\text{其中}\quad \text{arr.shape} = (N_{i\dots},\ M,\ N_{k\dots}) \]

#### 4.2.3 源码精读

dispatcher 只暴露数组 `arr`（其余 `*args, **kwargs` 透传给 `func1d`）：[_shape_base_impl.py:L272-L277](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L272-L277)

实现体开头用 `_array_converter` 统一处理「数组或数组列表」输入，并规整单轴（注意这里用的是 `normalize_axis_index`，不是元组版）：[_shape_base_impl.py:L365-L370](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L365-L370)

接着把 `axis` 转置到最后一维，使「切片轴」固定在末尾，其余全是「迭代轴」：[_shape_base_impl.py:L372-L374](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L372-L374)

```python
in_dims = list(range(nd))
inarr_view = transpose(arr, in_dims[:axis] + in_dims[axis + 1:] + [axis])
```

随后生成迭代坐标，**每条坐标末尾补一个 `Ellipsis`**，注释指出这是为了修复 gh-8642——防止 `func1d` 返回 0 维数组时退化为标量：[_shape_base_impl.py:L376-L379](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L376-L379)

```python
inds = ndindex(inarr_view.shape[:-1])
inds = (ind + (Ellipsis,) for ind in inds)
```

取出第一条坐标并执行一次 `func1d`，用结果探测输出形状；若取不到第一条（说明某个迭代维大小为 0），直接抛 `ValueError`：[_shape_base_impl.py:L381-L388](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L381-L388)

这是「先跑一次再决定缓冲区形状」的设计——`func1d` 的输出形状无法从签名静态推断，只能实测。

按探测到的形状建缓冲区（`matrix` 子类走特例，避开重塑时的怪行为）：[_shape_base_impl.py:L394-L398](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L394-L398)

然后计算「搬运排列」`buff_permute`，目的是把 `func1d` 输出维度从末尾搬回 `axis` 位置：[_shape_base_impl.py:L400-L406](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L400-L406)

```python
buff_dims = list(range(buff.ndim))
buff_permute = (
    buff_dims[0 : axis] +
    buff_dims[buff.ndim - res.ndim : buff.ndim] +
    buff_dims[axis : buff.ndim - res.ndim]
)
```

理解这块切片：`buff` 的维度被分成三段——`[0,axis)` 是 `axis` 之前的迭代维、`[buff.ndim-res.ndim, buff.ndim)` 是末尾的 `func1d` 输出维、中间是 `axis` 之后的迭代维。`buff_permute` 把它们重排成「前迭代维 + 输出维 + 后迭代维」，正是最终想要的轴序。

最后回填缓冲区、转置还原、用 `conv.wrap` 恢复输入类型：[_shape_base_impl.py:L408-L414](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L408-L414)

```python
buff[ind0] = res
for ind in inds:
    buff[ind] = asanyarray(func1d(inarr_view[ind], *args, **kwargs))
res = transpose(buff, buff_permute)
return conv.wrap(res)
```

这套「转置到末尾→建缓冲→回填→再转置」的写法，让每次 `buff[ind] = ...` 都落在连续内存上，是它比朴素双循环快的根源。注意它仍是 Python 层循环，性能上不能与真正的 ufunc 相提并论——它的价值在「灵活性」而非「速度」。

#### 4.2.4 代码实践（本讲主实践）

1. 实践目标：用 `apply_along_axis` 对 2D 数组每一行做自定义归一化（减均值、除标准差）。
2. 操作步骤：

```python
import numpy as np

def standardize(row):
    """对一维数组做 z-score 标准化：减均值、除标准差。"""
    mu = row.mean()
    sigma = row.std()
    return (row - mu) / sigma

a = np.array([[1, 2, 3],
              [10, 20, 30],
              [0, 5, 10]], dtype=float)

# axis=1 表示沿「每一行」应用，即 func1d 收到的是一行（一维切片）
out = np.apply_along_axis(standardize, axis=1, arr=a)
print(out)
print(out.mean(axis=1))   # 每行均值
print(out.std(axis=1))    # 每行标准差
```

3. 需要观察的现象：`out` 与 `a` 同形状 `(3, 3)`；每行均值近似为 `0`、每行标准差近似为 `1`。
4. 预期结果：三行的标准化结果大致为 `[-1.225, 0, 1.225]`（每行都是这个模式，因为 `[1,2,3]`、`[10,20,30]`、`[0,5,10]` 都是等差数列，标准化形状相同）。`out.mean(axis=1)` 应显示接近 `0`（浮点误差量级 1e-16），`out.std(axis=1)` 应显示接近 `1`。
5. 若想验证「输出维度插入」语义，可把 `standardize` 换成返回二维数组的函数，例如 `np.apply_along_axis(np.diag, -1, b)`（见官方 docstring 示例 [_shape_base_impl.py:L353-L364](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L353-L364)），观察结果比输入多出的维度。

#### 4.2.5 小练习与答案

**练习 1**：`np.apply_along_axis(np.sum, 0, np.ones((20, 10)))` 的结果形状是什么？

答案：`func1d = np.sum` 返回标量，`Nj...` 为空，故 `axis=0` 那一维被「吃掉」。输入 `(20, 10)` → 输出 `(10,)`，每个元素是 20。这与 [test_shape_base.py:L137-L145](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_shape_base.py#L137-L145) 中 `apply_along_axis(len, 0, a)` 的用法同构（`len` 也返回标量，把对应轴消掉）。

**练习 2**：为什么输入只要有一个迭代维大小为 0，就会抛「Cannot apply_along_axis when any iteration dimensions are 0」？

答案：见 [_shape_base_impl.py:L382-L387](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L382-L387)。函数必须先调用一次 `func1d` 才能探测输出形状 `res.shape`，进而分配缓冲区；若迭代维为 0，`next(inds)` 取不到第一条坐标，无从探测，只能报错。

---

### 4.3 apply_over_axes：在多个轴上重复应用函数

#### 4.3.1 概念说明

`apply_over_axes(func, a, axes)` 把一个签名固定为 `func(arr, axis)` 的函数，**依次**在 `axes` 列表里的每个轴上调用一次，每次的输入是上一次的输出。它解决「想沿多个轴累计归约，但手头函数只接受单轴」的场景。

它对 `func` 的返回值有严格要求：返回数组的维度数必须**等于**输入，或**少一**。若少一维，`apply_over_axes` 会用 `expand_dims` 把那一维补回去，从而保证每轮迭代维度数稳定。官方 Notes 指出，它等价于「可重排 ufunc 配合 `keepdims=True` 的元组轴参数」——例如 `apply_over_axes(np.sum, a, [0,2])` 等价于 `np.sum(a, axis=(0,2), keepdims=True)`。

#### 4.3.2 核心流程

```
val = asarray(a); N = a.ndim
若 axes 是标量，包成 (axes,)
for axis in axes:
    axis < 0 时转为 N + axis
    res = func(val, axis)
    if res.ndim == val.ndim:  val = res        # func 自带 keepdims 语义
    else:
        res = expand_dims(res, axis)            # func 少返回一维，补回去
        if res.ndim == val.ndim: val = res
        else: raise ValueError                  # 形状不对
return val
```

注意：负轴用 `N`（原始维度数，常量）规整，而不是当前 `val.ndim`——因为循环每步都保证 `val.ndim == N`，二者等价但用 `N` 更直观。这里直接复用了本讲 4.1 的 `expand_dims`，体现了两个函数的配合关系。

#### 4.3.3 源码精读

dispatcher 只暴露数组 `a`：[_shape_base_impl.py:L417-L422](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L417-L422)

实现体把 `axes` 标量包成元组，然后逐轴循环：[_shape_base_impl.py:L488-L506](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L488-L506)

```python
val = asarray(a)
N = a.ndim
if array(axes).ndim == 0:
    axes = (axes,)
for axis in axes:
    if axis < 0:
        axis = N + axis
    args = (val, axis)
    res = func(*args)
    if res.ndim == val.ndim:
        val = res
    else:
        res = expand_dims(res, axis)
        if res.ndim == val.ndim:
            val = res
        else:
            raise ValueError("function is not returning "
                             "an array of the correct shape")
return val
```

核心是那个 `if/else`：它同时兼容两种风格的 `func`——「保留维度」（如 `np.sum(..., keepdims=True)` 包装版）和「减少维度」（如默认 `np.sum`）。后者会被 `expand_dims` 补回一维，使下一轮 `axis` 编号依然有效。

#### 4.3.4 代码实践

1. 实践目标：对比 `apply_over_axes` 与「元组轴 + keepdims」的等价性。
2. 操作步骤：

```python
import numpy as np
a = np.arange(24).reshape(2, 3, 4)
r1 = np.apply_over_axes(np.sum, a, [0, 2])
r2 = np.sum(a, axis=(0, 2), keepdims=True)
print(r1.shape, r2.shape)
print(np.array_equal(r1, r2))
```

3. 需要观察的现象：两者形状都应为 `(1, 3, 1)`（轴 0 和轴 2 被归约后保留为 1）；`array_equal` 应为 `True`。
4. 预期结果：如上，与官方 docstring 示例 [_shape_base_impl.py:L460-L485](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L460-L485) 一致。
5. 思考题（不必运行）：若把 `np.sum` 换成一个返回二维数组（维度数少 2）的函数，会怎样？答：`expand_dims` 只补一维，仍少一维，触发 `ValueError`。

#### 4.3.5 小练习与答案

**练习 1**：`apply_over_axes` 为什么要求 `func` 返回的维度数「等于或少一」，而不是任意？

答案：见 [_shape_base_impl.py:L497-L505](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L497-L505)。它用 `expand_dims(res, axis)` 最多补回一维；若 `func` 一次少返回多于 1 维，补一维后仍不等于 `val.ndim`，只能报错。这条约束把「多轴累计归约」限制在「每步至多消掉一维」的安全范围内。

**练习 2**：`np.apply_over_axes(np.sum, a, [0, 2])` 与 `a.sum(axis=(0, 2))`（不带 `keepdims`）结果形状分别是什么？

答案：前者每步保留维度，最终形状 `(1, 3, 1)`；后者一次性归约掉两维，形状 `(3,)`。差异正是 `keepdims` 带来的——`apply_over_axes` 内置了等价于 `keepdims=True` 的行为。

---

### 4.4 _make_along_axis_idx：构造正交花式索引

#### 4.4.1 概念说明

`_make_along_axis_idx` 是 `take_along_axis` 和 `put_along_axis` 共用的**内部核心**（以下划线开头，不对外暴露）。它解决的问题可以这样描述：

给定数组 `arr`（形状 `(Ni..., M, Nk...)`）和索引数组 `indices`（形状 `(Ni..., J, Nk...)`），想要得到

\[ \text{out}[i_0,\dots,j,\dots,i_{d-1}] = \text{arr}\big[i_0,\dots,\ \text{indices}[i_0,\dots,j,\dots,i_{d-1}],\ \dots,i_{d-1}\big] \]

也就是「对每条沿 `axis` 的一维切片，用 `indices` 里对应的那组下标去取值」。直接用 `arr[indices]` 做不到，因为那会把 `indices` 当成对整个数组的下标，而不是「逐切片」的下标。`_make_along_axis_idx` 的办法是构造一个**由若干个互相正交（可广播）的 arange 加上 indices 组成的元组**，让花式索引的广播天然实现「逐切片取值」。

#### 4.4.2 核心流程

```
输入 arr_shape、indices、axis
1. 校验 indices 必须是整数类型（否则 IndexError）
2. 校验 len(arr_shape) == indices.ndim（否则 ValueError）
3. dest_dims = [0..axis-1] + [None] + [axis+1..ndim-1]   # None 占位 axis 维
4. 对每个 (dim, n)：
       若 dim is None：直接放 indices
       否则：放 arange(n).reshape(全 1、仅 dim 维为 -1)   # 广播用的「全选」下标
5. 返回 tuple(这些数组)
```

直觉：除了 `axis` 那一维放真正的 `indices`，其余每一维都放一个「形状只在那一维展开」的 `arange`，它们两两广播后正好覆盖所有「非 axis」位置的元素，再把 `indices` 嵌进去，就实现了逐切片索引。

#### 4.4.3 源码精读

完整的内部函数，含两条校验：[_shape_base_impl.py:L33-L53](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L33-L53)

```python
def _make_along_axis_idx(arr_shape, indices, axis):
    if not _nx.issubdtype(indices.dtype, _nx.integer):
        raise IndexError('`indices` must be an integer array')
    if len(arr_shape) != indices.ndim:
        raise ValueError(
            "`indices` and `arr` must have the same number of dimensions")
    shape_ones = (1,) * indices.ndim
    dest_dims = list(range(axis)) + [None] + list(range(axis + 1, indices.ndim))

    fancy_index = []
    for dim, n in zip(dest_dims, arr_shape):
        if dim is None:
            fancy_index.append(indices)
        else:
            ind_shape = shape_ones[:dim] + (-1,) + shape_ones[dim + 1:]
            fancy_index.append(_nx.arange(n).reshape(ind_shape))

    return tuple(fancy_index)
```

两条校验对应测试里的报错：`indices` 为 bool/float 时抛 `IndexError`（见 [test_shape_base.py:L70-L72](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_shape_base.py#L70-L72)），`indices` 维度数不匹配时抛 `ValueError`（见 [test_shape_base.py:L68](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_shape_base.py#L68)）。

`ind_shape` 的构造是关键：`shape_ones[:dim] + (-1,) + shape_ones[dim+1:]` 生成一个除第 `dim` 维为 `-1`（取满）外其余全 `1` 的形状，使 `arange(n)` 只沿第 `dim` 维展开，其余维度为 1 以便广播。

举个例子：`arr` 形状 `(3, 4, 5)`、`indices` 形状 `(3, 2, 5)`、`axis=1`。则 `dest_dims = [0, None, 2]`：

- 第 0 维：`arange(3).reshape(-1,1,1)` → 形状 `(3,1,1)`
- 第 1 维（None）：`indices` → 形状 `(3,2,5)`
- 第 2 维：`arange(5).reshape(1,1,-1)` → 形状 `(1,1,5)`

三者广播到 `(3,2,5)`，`out[i,j,k] = arr[i, indices[i,j,k], k]`，正是「逐切片取值」。

#### 4.4.4 代码实践

1. 实践目标：手工调用这个内部函数，验证它构造的索引确实等价于「逐切片取值」。
2. 操作步骤：

```python
import numpy as np
from numpy.lib._shape_base_impl import _make_along_axis_idx

arr = np.array([[10, 30, 20], [60, 40, 50]])   # (2, 3)
idx = np.argsort(arr, axis=1)                   # (2, 3)
fancy = _make_along_axis_idx(arr.shape, idx, axis=1)
print([f.shape for f in fancy])                 # 期望 [(2,1), (2,3)]
print(arr[fancy])                               # 期望每行升序
print(np.array_equal(arr[fancy], np.take_along_axis(arr, idx, axis=1)))
```

3. 需要观察的现象：`fancy` 是两个数组的元组，形状分别为 `(2,1)`（第 0 维的 arange）和 `(2,3)`（即 `idx`）；`arr[fancy]` 每行升序；与 `take_along_axis` 结果完全相等。
4. 预期结果：`arr[fancy]` 应为 `[[10,20,30],[40,50,60]]`；最后的 `array_equal` 应为 `True`。
5. 提示：`_make_along_axis_idx` 是下划线开头的内部函数，仅用于理解原理；实际代码请用 `take_along_axis` / `put_along_axis`。

#### 4.4.5 小练习与答案

**练习 1**：把上例的 `idx` 改成 `np.argmax(arr, axis=1, keepdims=True)`（形状 `(2,1)`），`_make_along_axis_idx` 还能工作吗？结果形状是什么？

答案：能。`indices` 形状 `(2,1)`、`axis=1`、`dest_dims=[0,None]`：第 0 维 `arange(2).reshape(-1,1)` 形状 `(2,1)`；第 1 维 `idx` 形状 `(2,1)`。广播后形状 `(2,1)`，`arr[fancy]` 形状 `(2,1)`，给出每行的最大值 `[30],[60]`。这正是 docstring 里「配合 `keepdims=True` 取 max」的用法（[_shape_base_impl.py:L141-L153](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L141-L153)）。

**练习 2**：为什么 `indices` 必须与 `arr` 维度数相同（不能少）？

答案：见 [_shape_base_impl.py:L37-L39](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L37-L39)。`dest_dims` 是按 `indices.ndim` 构造的，每个维度都要决定「放 arange 还是放 indices」；维度数不一致就无法对齐「哪一维是切片轴」。这也是 [test_shape_base.py:L68](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_shape_base.py#L68) 中 `take_along_axis(a, np.array(1), axis=1)` 抛 `ValueError` 的根因。

---

### 4.5 take_along_axis 与 put_along_axis：按索引沿轴读写

#### 4.5.1 概念说明

`take_along_axis`（读）和 `put_along_axis`（写）是 `_make_along_axis_idx` 的两个对外封装。它们解决一个高频需求：「按另一个函数算出的下标，沿某轴逐切片地取值或赋值」。最典型的搭档是 `argsort` / `argpartition` / `argmax` / `argmin`（配合 `keepdims=True`）。

它们与普通 `np.take` 的根本区别在于**广播语义**：

- `np.take(arr, indices, axis)` 对**所有**一维切片使用「同一份」`indices`（`indices` 相对于整张数组广播）。
- `np.take_along_axis(arr, indices, axis)` 对**每条**一维切片使用 `indices` 中对应那一行的下标（`indices` 与 `arr` 在「非 axis 维」上逐元素对齐，`axis` 维长度 `J` 可以不同于 `M`）。

换句话说，`take_along_axis` 是「逐切片的可变下标」，这正是排序、top-k 等任务需要的。

#### 4.5.2 核心流程

两者结构对称：

```
take_along_axis(arr, indices, axis):
    if axis is None:
        要求 indices.ndim == 1；arr 拍平成 1D；axis = 0
    else:
        axis = normalize_axis_index(axis, arr.ndim)
    return arr[ _make_along_axis_idx(arr.shape, indices, axis) ]

put_along_axis(arr, indices, values, axis):
    （同样的 axis 规整）
    arr[ _make_along_axis_idx(arr.shape, indices, axis) ] = values   # 原地写
```

注意 `take_along_axis` 的默认 `axis=-1`（自 2.3 起改为 `-1`，见 docstring 的 `versionchanged` 说明 [_shape_base_impl.py:L85-L86](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L85-L86)），而 `put_along_axis` 的 `axis` 是必填位置参数（无默认值）。

#### 4.5.3 源码精读

`take_along_axis` 的 dispatcher 暴露 `arr` 和 `indices`：[_shape_base_impl.py:L56-L61](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L56-L61)

实现体只有两步——规整 `axis`、用 `_make_along_axis_idx` 取值：[_shape_base_impl.py:L168-L179](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L168-L179)

```python
if axis is None:
    if indices.ndim != 1:
        raise ValueError(
            'when axis=None, `indices` must have a single dimension.')
    arr = np.array(arr.flat)
    axis = 0
else:
    axis = normalize_axis_index(axis, arr.ndim)
return arr[_make_along_axis_idx(arr.shape, indices, axis)]
```

`axis=None` 分支要求 `indices` 必须是一维（见 [test_shape_base.py:L76](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_shape_base.py#L76) 的报错用例），把 `arr` 拍平后当作 `axis=0` 处理，与 `sort`/`argsort` 的 `axis=None` 语义一致。

`put_along_axis` 几乎是 `take_along_axis` 的「写入镜像」，dispatcher 暴露 `arr, indices, values`：[_shape_base_impl.py:L182-L187](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L182-L187)

唯一区别是最后一行——把 `take` 的「读取」换成赋值：[_shape_base_impl.py:L258-L269](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L258-L269)

```python
if axis is None:
    if indices.ndim != 1:
        raise ValueError(
            'when axis=None, `indices` must have a single dimension.')
    arr = np.array(arr.flat)
    axis = 0
else:
    axis = normalize_axis_index(axis, arr.ndim)
arr[_make_along_axis_idx(arr.shape, indices, axis)] = values
```

`values` 会被广播到 `indices` 的形状（赋值时 NumPy 自动广播右端）。`put_along_axis` 是**就地修改**，无返回值——这一点和 `take_along_axis`（返回新数组）相反，使用时要注意。

#### 4.5.4 代码实践

1. 实践目标：用 `argsort` + `take_along_axis` 给每行排序；用 `argmax` + `put_along_axis` 把每行最大值改写成 `-99`。
2. 操作步骤：

```python
import numpy as np

a = np.array([[10, 30, 20], [60, 40, 50]])

# 读：每行升序排序
order = np.argsort(a, axis=1)
sorted_a = np.take_along_axis(a, order, axis=1)
print(sorted_a)                       # 期望 [[10,20,30],[40,50,60]]

# 写：把每行最大值改成 -99
b = a.copy()
i_max = np.argmax(b, axis=1, keepdims=True)   # keepdims 保持维度，形状 (2,1)
np.put_along_axis(b, i_max, -99, axis=1)
print(b)                              # 期望 [[10,-99,20],[-99,40,50]]
```

3. 需要观察的现象：`sorted_a` 每行升序；`b` 中原本每行的最大值位置（行 0 是 30、行 1 是 60）被替换为 `-99`。
4. 预期结果：`sorted_a = [[10,20,30],[40,50,60]]`；`b = [[10,-99,20],[-99,40,50]]`，与官方 docstring 示例 [_shape_base_impl.py:L243-L255](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L243-L255) 一致。
5. 对比实验：把 `take_along_axis(a, order, axis=1)` 换成 `np.take(a, order, axis=1)`，观察后者结果不同——`np.take` 不会逐行对应下标，从而得到错误排序，这正是「逐切片下标」与「全局下标」的区别。

#### 4.5.5 小练习与答案

**练习 1**：为什么用 `argmax`/`argmin` 喂给 `take_along_axis` 时要加 `keepdims=True`？

答案：`_make_along_axis_idx` 要求 `indices.ndim == arr.ndim`（[_shape_base_impl.py:L37-L39](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L37-L39)）。不加 `keepdims` 时 `argmax` 会把归约轴消掉，使 `indices` 比 `arr` 少一维，直接抛 `ValueError`。加 `keepdims=True` 后 `indices` 形状为 `(Ni..., 1, Nk...)`，满足维度数要求，且第 `axis` 维长度为 1，广播后正好取出每切片的一个元素。

**练习 2**：`take_along_axis` 的非索引维可以广播吗？

答案：可以。`_make_along_axis_idx` 给非 axis 维构造的 `arange` 形状是「仅该维展开、其余为 1」，因此非索引维会与 `indices` 的对应维广播。测试 [test_shape_base.py:L86-L91](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_shape_base.py#L86-L91) 用 `arr (3,4,1)` × `indices (1,2,5)`、`axis=1` 得到 `(3,2,5)` 即是此理——「两个方向都广播」。

---

## 5. 综合实践

把本讲的 `expand_dims`、`apply_along_axis`、`take_along_axis`、`put_along_axis` 串起来，完成一个小型数据处理任务：**对一批样本（2D）做按行标准化，再按标准化后的分数从高到低重排每行的列，并给每行最高分打上标记。**

1. 实践目标：综合运用维度扩展与轴向应用，体会「一维函数铺开」+「逐切片索引读写」的协作。
2. 操作步骤：

```python
import numpy as np

# 1) 构造数据：3 个样本，每个样本 4 个特征
data = np.array([[1, 2, 3, 4],
                 [10, 30, 20, 40],
                 [5, 5, 8, 2]], dtype=float)

# 2) 用 apply_along_axis 对每一行做 z-score 标准化
def zscore(row):
    return (row - row.mean()) / row.std()
normed = np.apply_along_axis(zscore, axis=1, arr=data)

# 3) 用 expand_dims 增加一个「batch」维度，把它变成 (1, 3, 4)
batched = np.expand_dims(normed, axis=0)
print(batched.shape)                  # 期望 (1, 3, 4)

# 4) 用 argsort + take_along_axis 把每行的特征按分数降序排列
#    （argsort 默认升序，对负分数取 argsort 得到降序的索引）
desc_order = np.argsort(-batched, axis=2)
ranked = np.take_along_axis(batched, desc_order, axis=2)
print(ranked[0])                      # 每行应是降序

# 5) 用 argmax + put_along_axis 在「原始 normed」上把每行最大值改写为 99
marked = normed.copy()
i_max = np.argmax(marked, axis=1, keepdims=True)
np.put_along_axis(marked, i_max, 99, axis=1)
print(marked)                         # 每行最大值位置变为 99
```

3. 需要观察的现象：
   - `normed` 每行均值≈0、标准差≈1；
   - `batched` 比 `normed` 多出开头的长度 1 维度；
   - `ranked[0]` 每行严格降序；
   - `marked` 每行恰有一个元素变为 `99`，位置对应该行标准化后的最大特征。
4. 预期结果：可由前述各模块的行为推出；运行应一致。若结果不符，重点检查第 4 步是否用了 `take_along_axis`（而非 `np.take`）以及第 5 步是否加了 `keepdims=True`。
5. 待本地验证：第 4 步对负数取 `argsort` 得到降序索引的技巧，取决于「分数各不相同」；若存在并列，稳定排序会保留原顺序，可改用 `np.argsort(..., kind='stable')` 观察。

## 6. 本讲小结

- `expand_dims` 的全部实现是「针对 `out_ndim` 规整轴 → 用迭代器构造新形状 → `reshape`」，返回视图、零拷贝；`axis` 既可是单值也可是元组，能一次插多个轴。
- `apply_along_axis` 采用「转置切片轴到末尾→先跑一次探测输出形状→建连续缓冲→回填→再转置还原」的套路，把任意一维函数沿轴铺开；空迭代维会因无法探测形状而报错。
- `apply_over_axes` 在多轴上反复调用 `func(arr, axis)`，并用本讲的 `expand_dims` 把「少返回一维」的结果补回，等价于「可重排 ufunc + 元组轴 + keepdims=True」。
- `_make_along_axis_idx` 通过「非 axis 维放可广播 arange、axis 维放 indices」构造正交花式索引，是 `take/put_along_axis` 的共用内核。
- `take_along_axis`（返回新数组）与 `put_along_axis`（就地修改）是「逐切片可变下标」的读写对，与普通 `take` 的「全局统一下标」语义不同；与 `argmax/argsort`（`keepdims=True`）是天生搭档。
- 几乎所有函数都先经 `normalize_axis_index` / `normalize_axis_tuple` 把轴规整为非负整数，这是贯穿本讲的统一约定。

## 7. 下一步学习建议

- 下一讲 [u3-l2](u3-l2-stack-and-split.md) 会继续在 `_shape_base_impl.py` 中讲解 `column_stack`/`dstack`/`split`/`array_split`/`hsplit`/`vsplit`/`dsplit` 家族，与本讲的 `expand_dims` 紧密相关（很多拼接函数内部依赖维度对齐），建议顺读。
- 若想深入「花式索引与广播」的底层，可阅读 `numpy/_core/numeric.py` 中 `normalize_axis_tuple` 的完整实现，以及 `numpy/lib/_index_tricks_impl.py` 中 `ndindex`、`s_` 的源码——它们是本讲 `apply_along_axis` 等价双循环里出现的老朋友。
- 对「轴操作」想看更多实践，可通读 `numpy/lib/tests/test_shape_base.py` 中的 `TestTakeAlongAxis`、`TestPutAlongAxis`、`TestApplyAlongAxis` 三个测试类，它们是本讲行为描述的权威依据。

# 矩阵构造、平铺与翻转：kron/tile/eye/diag/tri

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `tile` 与 `kron` 这两种「平铺」的差别：`tile` 是「按份数复制」，结果是「元素级的循环」；`kron` 是「按块放大」，结果是「每个元素都长成一块」，并能据此预测两者的输出形状。
- 读懂 `eye` / `diag` / `diagflat` 共用的同一个核心技巧：在一个全零矩阵上，用「跨步切片写一维扁平内存」把数据精确摆到某条对角线上。
- 理解 `vander` 如何用一个「反向视图 + 累乘」的招数，一次循环就生成范德蒙矩阵的全部列。
- 知道 `fliplr` / `flipud` 之所以是 \(\mathcal O(1)\)，是因为它们只改步长符号、返回视图。
- 读懂 `tri` 用 `greater_equal.outer` 生成三角掩码、`tril`/`triu` 用 `k` 与 `k-1` 的偏移互为补集、以及 `mask_indices` 与 `tril_indices`/`triu_indices` 之间的关系。

## 2. 前置知识

进入源码前，先建立四个直觉。

**C 序扁平内存与跨步。** 一个 C 序（row-major）的 `(N, M)` 矩阵，在内存里是「一行接一行」铺开的：元素 `(r, c)` 的扁平下标是 `r*M + c`。主对角线元素 `(0,0),(1,1),...` 的扁平下标恰好是 `0, M+1, 2*(M+1), ...`，即每隔 `M+1` 个元素一个。本讲 `eye`/`diag` 正是用 `m.flat[i::M+1]` 这种「跨步切片」来对齐对角线。

**outer 运算。** 对两个一维数组 `a`、`b`，`ufunc.outer(a, b)` 会算出「外积式」的二维结果：`result[i, j] = a[i] (op) b[j]`。本讲 `tri` 用 `greater_equal.outer` 一次性生成「第 i 行第 j 列是否 `i >= j-k`」的布尔掩码。

**视图（view）与步长翻转。** `arr[:, ::-1]` 不复制数据，它只是把第 1 轴的步长取反，于是遍历方向反过来——这正是 `fliplr` 零拷贝的原因。同理 `arr[:, ::-1]` 作为「左值」被写入时，写入会落到原数组的对应列上（`vander` 用到了这一点）。

**dispatcher + impl 双函数写法。** 本讲函数大多沿用 [u1-l2](u1-l2-module-organization.md) 讲过的 `@array_function_dispatch(_xxx_dispatcher)` 模式：dispatcher 只返回参与运算的数组参数（服务 NEP-18 的 `__array_function__` 协议），真正逻辑在被装饰函数体内。但 `eye`、`tri`、`mask_indices`、`tril_indices`、`triu_indices` 走的是另一套——用 `@set_module('numpy')` 标注模块归属，`eye`/`tri` 额外加 `@finalize_array_function_like` 以支持 NEP-35 的 `like=` 参数（让你能「照着」一个数组在同类后端上建新数组）。这点差别会在 4.2 单独点出。

## 3. 本讲源码地图

本讲的实现分散在两个文件里：

| 文件 | 作用 |
| --- | --- |
| `numpy/lib/_shape_base_impl.py` | 「形状操作」实现层。本讲取其中的 `kron`、`tile`（以及它们各自的 `_xxx_dispatcher`） |
| `numpy/lib/_twodim_base_impl.py` | 「二维矩阵构造」实现层。本讲取其中的 `eye`、`diag`、`diagflat`、`fliplr`、`flipud`、`tri`、`tril`、`triu`、`vander`、`mask_indices` |

两个文件都没有薄再导出模块，而是由顶层 `numpy/__init__.py` 直接 `from .lib._xxx_impl import ...` 取名并收进 `np.` 命名空间——这属于 [u1-l2](u1-l2-module-organization.md) 所述「无薄模块、顶层直接再导出」的情形。

测试与行为依据：

| 文件 | 作用 |
| --- | --- |
| `numpy/lib/tests/test_shape_base.py` | 含 `TestKron`、`TestTile`，可用于核对本讲对 `kron`/`tile` 形状规则的描述 |
| `numpy/lib/tests/test_twodim_base.py` | 含 `eye`/`diag`/`tri`/`triu`/`vander`/`mask_indices` 等单测 |

## 4. 核心概念与源码讲解

### 4.1 tile 与 kron：两种「平铺」及其维度规则差异

#### 4.1.1 概念说明

`tile(A, reps)` 和 `kron(a, b)` 都能把一个小数组「变大」，但变大方式完全不同，初学者很容易混。

- `tile` 是**复印机**：把 `A` 当作一块「瓷砖」，沿每个轴重复 `reps` 份。`np.tile([0,1,2], 2)` 得到 `[0,1,2,0,1,2]`——元素本身没变，只是被复制排列。
- `kron` 是**放大镜**：把 `b` 当作「像素块」，`a` 的每一个元素决定一块「被该元素放缩后的 `b`」。`np.kron([1,10],[5,6,7])` 得到 `[5,6,7,50,60,70]`——`a` 的每个元素乘到整块 `b` 上。

两者形状规则也不同：`tile` 的输出维度数 = `max(A.ndim, len(reps))`，每个轴的输出大小是「该轴输入大小 × 重复份数」；`kron` 则要求两个数组**先对齐到相同维度数**（小的在前补 1），输出每个轴的大小是「两数组在该轴大小的乘积」。核心差异在于 `kron` 的输出形状是**逐轴相乘**，且元素带「块结构」。

#### 4.1.2 核心流程

**tile 的流程：**

```
输入 A、reps
1. tup = tuple(reps)；d = len(tup)
2. 若所有份数都为 1 且 A 是 ndarray：直接返回 A 的一份拷贝（特例，避免 0 拷贝踩坑）
3. 否则 c = array(A, ndmin=d)        # 把 A 提到 d 维
4. 若 d < c.ndim：在 tup 前补若干个 1
5. shape_out = 每个轴 (输入大小 × 份数)
6. 从最末轴往回，对每个「份数≠1」的轴做 reshape(-1,n).repeat(份数,0)，逐轴膨胀
7. reshape 到 shape_out
```

**kron 的流程：**

```
输入 a、b
1. 把 ndim 较小者前面补 1，对齐到相同维度数 nd
2. 关键招数——交错插轴：
   - a 在「奇数位置」(1,3,5,...) 插长度为 1 的新轴
   - b 在「偶数位置」(0,2,4,...) 插长度为 1 的新轴
3. 两者相乘（广播后形状恰好是 (r0,s0,r1,s1,...)）
4. reshape 回 (r0*s0, r1*s1, ...)
```

交错插轴是 `kron` 的灵魂。以二维 `a.shape=(r0,r1)`、`b.shape=(s0,s1)` 为例：插轴后 `a` 变成 `(r0,1,r1,1)`、`b` 变成 `(1,s0,1,s1)`，广播相乘得到 `(r0,s0,r1,s1)`——这正是「`a` 的每个元素撑开成一块 `b`」的内存布局。数学上：

\[
\text{kron}(a,b)[k_0,\dots,k_N] = a[i_0,\dots,i_N]\cdot b[j_0,\dots,j_N],\quad k_t = i_t\cdot s_t + j_t
\]

#### 4.1.3 源码精读

`tile` 的 dispatcher 与特例分支。dispatcher 仅返回数组本身：[_shape_base_impl.py:L1153-L1157](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L1153-L1157)

实现体里最值得品味的是「份数全为 1 时强制拷贝」的特例，以及逐轴膨胀的循环：[_shape_base_impl.py:L1225-L1247](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L1225-L1247)

```python
if all(x == 1 for x in tup) and isinstance(A, _nx.ndarray):
    # 份数全 1 时，原版逻辑不会拷贝，这里强制拷贝以免误覆盖
    return _nx.array(A, copy=True, subok=True, ndmin=d)
...
shape_out = tuple(s * t for s, t in zip(c.shape, tup))
n = c.size
if n > 0:
    for dim_in, nrep in zip(c.shape, tup):
        if nrep != 1:
            c = c.reshape(-1, n).repeat(nrep, 0)
        n //= dim_in
return c.reshape(shape_out)
```

逐轴膨胀的招数是 `c.reshape(-1, n).repeat(nrep, 0)`：把当前最末块重排成「行=一个完整块」，再 `repeat` 把每行复制 `nrep` 份。注意 `n //= dim_in` 在每轮缩小「当前块的元素总数」，从而每轮 `reshape(-1, n)` 的 `n` 都指向「当前层级的块大小」。

`kron` 的 dispatcher 同样只返回两个数组：[_shape_base_impl.py:L1034-L1038](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L1034-L1038)。实现体里「对齐 + 交错插轴 + 相乘 + reshape」四步紧凑写在一段里：[_shape_base_impl.py:L1133-L1148](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_shape_base_impl.py#L1133-L1148)

```python
# Equalise the shapes by prepending smaller one with 1s
as_ = (1,) * max(0, ndb - nda) + as_
bs = (1,) * max(0, nda - ndb) + bs

# Insert empty dimensions
a_arr = expand_dims(a, axis=tuple(range(ndb - nda)))
b_arr = expand_dims(b, axis=tuple(range(nda - ndb)))

# Compute the product
a_arr = expand_dims(a_arr, axis=tuple(range(1, nd * 2, 2)))
b_arr = expand_dims(b_arr, axis=tuple(range(0, nd * 2, 2)))
result = _nx.multiply(a_arr, b_arr, subok=(not is_any_mat))

# Reshape back
result = result.reshape(_nx.multiply(as_, bs))
```

注意它复用了同文件的 `expand_dims`（[u3-l1](u3-l1-expand-and-apply-axis.md) 讲过）来插轴。最后 `result.reshape(_nx.multiply(as_, bs))` 把形状从交错形式 `(r0,s0,r1,s1,...)` 合并回 `(r0*s0, r1*s1, ...)`，这是「逐轴相乘」规则的直接体现。

#### 4.1.4 代码实践

1. 实践目标：亲手对比 `tile` 与 `kron` 的形状规则与元素布局，验证「逐轴相乘 vs 逐轴相乘份数」的差别。
2. 操作步骤（示例代码）：

```python
import numpy as np

# (a) tile：复印机
print(np.tile([0, 1, 2], 2))               # 期望 [0 1 2 0 1 2]
print(np.tile([0, 1, 2], (2, 2)).shape)    # 期望 (2, 4)：1D 被提到 2 维

# (b) kron：放大镜
print(np.kron([1, 10], [5, 6, 7]))         # 期望 [5 6 7 50 60 70]
print(np.kron(np.eye(2), np.ones((2, 2))))
# 期望：把 eye(2) 的每个元素乘到整块 ones(2,2) 上，得到 4x4 块矩阵

# (c) 形状对比：同是 2x2 与 2x2
A = np.arange(4).reshape(2, 2)
print(np.tile(A, (2, 2)).shape)            # 期望 (4, 4)
print(np.kron(A, A).shape)                 # 期望 (4, 4)，但元素排布完全不同
print(np.tile(A, (2, 2)))
print(np.kron(A, A))
```

3. 需要观察的现象：`tile` 输出里能看到清晰的「原块复制品」并排；`kron` 输出里每个原元素都「撑开」成一块。两者形状同为 `(4,4)` 但内容不同。
4. 预期结果：`kron(np.eye(2), np.ones((2,2)))` 应得到对角块为全 1、非对角块为全 0 的 `4×4` 矩阵（见源码 docstring 示例）。`tile(A,(2,2))` 与 `kron(A,A)` 形状相同但元素不同。
5. 待本地验证：以上均可由源码规则推出，运行应一致；可额外尝试 `np.kron([1,10,100],[5,6,7])` 与 docstring 给出的 `[5,6,7,...,500,600,700]` 对照。

#### 4.1.5 小练习与答案

**练习 1**：`a.shape=(2,3)`、`b.shape=(4,5)`，`np.kron(a,b).shape` 是什么？

答案：先对齐维度（都已 2 维），输出形状逐轴相乘 = `(2*4, 3*5) = (8, 15)`。

**练习 2**：`np.tile(np.ones((2,3)), (1,))` 的形状是什么？为什么源码要为「份数全 1」单独写一个分支？

答案：形状仍是 `(2,3)`。`reps=(1,)` 时 `d=1 < c.ndim=2`，于是 `tup` 前补一个 1 变成 `(1,1)`，`shape_out=(2*1,3*1)=(2,3)`。单独分支是因为 `d<c.ndim` 的常规路径在某些情况下不会产生拷贝，而「份数全 1」时用户通常期望得到一份独立的拷贝，故强制 `copy=True`。

### 4.2 eye / diag / diagflat：单位矩阵与对角线构造

#### 4.2.1 概念说明

这三个函数都围绕「对角线」构造矩阵，且共享同一种底层招数——**跨步写扁平内存**。

- `eye(N, M, k)`：构造一个 `(N,M)` 的矩阵，第 `k` 条对角线上全是 1，其余为 0。`k=0` 主对角线、`k>0` 主对角线上方、`k<0` 下方。
- `diag(v, k)`：**双向**函数。`v` 是 1D 时，构造一个以 `v` 为第 `k` 条对角线的方阵；`v` 是 2D 时，抽取它的第 `k` 条对角线（返回一维数组）。
- `diagflat(v, k)`：把 `v` **先展平**，再当作对角线放进方阵——区别于 `diag` 的是它接受任意形状输入并先 `ravel()`。

#### 4.2.2 核心流程

三者的共同招数可以这样描述（以 `(n,n)` 方阵、偏移 `k` 为例）：

```
1. res = zeros((n, n))
2. 算出第 k 条对角线在扁平内存里的「起点下标」i：
     k >= 0：i = k            （第 0 行第 k 列）
     k <  0：i = (-k) * n     （第 -k 行第 0 列）
3. res[:n-k].flat[i::n+1] = 数据   # 每隔 n+1 个元素一个，恰好落在对角线上
```

为什么是 `n+1`？因为 C 序 `(n,n)` 矩阵里，从 `(r,c)` 走到 `(r+1,c+1)`，扁平下标增加 `n*1 + 1 = n+1`。

为什么前面要套 `res[:n-k]`？因为「每隔 `n+1` 取一个」若不限范围，会在走完有效对角线后**串到下一行的开头**，把错误的位置也置 1。`res[:n-k]` 把行数截到 `n-k`（对角线刚好用完这些行），避免越界串味。

#### 4.2.3 源码精读

**eye** 用 `@finalize_array_function_like` + `@set_module('numpy')`（不是 `array_function_dispatch`），支持 `like=` 参数：[_twodim_base_impl.py:L178-L180](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L178-L180)。`_eye_with_like = array_function_dispatch()(eye)` 才是给 NEP-18 用的派发版本：[_twodim_base_impl.py:L253](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L253)

`eye` 的跨步写法：[_twodim_base_impl.py:L237-L250](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L237-L250)

```python
m = zeros((N, M), dtype=dtype, order=order, device=device)
if k >= M:
    return m                       # 对角线完全在矩阵右侧之外，全零直接返回
M = operator.index(M)
k = operator.index(k)
if k >= 0:
    i = k
else:
    i = (-k) * M
m[:M - k].flat[i::M + 1] = 1
return m
```

`operator.index` 把可能的 `np.uint64` 等非纯整数安全转成 Python int，避免 `M-k`、`M+1` 表达式里出现意外类型转换。

**diag** 按 `v` 的维度数分两条路，1D 走「构造」、2D 委托给 `diagonal`：[_twodim_base_impl.py:L316-L330](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L316-L330)

```python
v = asanyarray(v)
s = v.shape
if len(s) == 1:
    n = s[0] + abs(k)
    res = zeros((n, n), v.dtype)
    if k >= 0:
        i = k
    else:
        i = (-k) * n
    res[:n - k].flat[i::n + 1] = v
    return res
elif len(s) == 2:
    return diagonal(v, k)
else:
    raise ValueError("Input must be 1- or 2-d.")
```

注意 1D 分支里写的是 `= v`（把整个向量摆上对角线），而 `eye` 写的是 `= 1`。两者用完全相同的索引招数。

**diagflat** 先展平再构造，但用 `_array_converter` 处理子类、并用 `arange` 算出扁平下标数组（而非跨步切片）：[_twodim_base_impl.py:L374-L388](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L374-L388)

```python
conv = _array_converter(v)
v, = conv.as_arrays(subok=False)
v = v.ravel()
s = len(v)
n = s + abs(k)
res = zeros((n, n), v.dtype)
if (k >= 0):
    i = arange(0, n - k, dtype=intp)
    fi = i + k + i * n
else:
    i = arange(0, n + k, dtype=intp)
    fi = i + (i - k) * n
res.flat[fi] = v
return conv.wrap(res)
```

`fi = i + k + i*n`（`k>=0`）展开就是 `i*(n+1) + k`，与 `diag` 的 `flat[i::n+1]` 等价——只是这里用显式下标数组 `fi` 一次性写入。`conv.wrap(res)` 把结果包回原输入的子类（如 `matrix`）。

#### 4.2.4 代码实践

1. 实践目标：验证 `eye`/`diag`/`diagflat` 的对角线偏移 `k`，并体会 `diag` 的双向性。
2. 操作步骤（示例代码）：

```python
import numpy as np

# eye 的偏移
print(np.eye(3, k=1))
# 期望：主对角线上一条全 1，即 (0,1),(1,2) 为 1
print(np.eye(3, 5, 2, dtype=int))   # 3x5，k=2

# diag 的双向性
M = np.arange(9).reshape(3, 3)
print(np.diag(M))          # 抽主对角线：[0 4 8]
print(np.diag(M, k=1))     # 抽上一条：[1 5]
print(np.diag([10, 20]))   # 构造方阵：对角线 10,20

# diagflat 先展平
print(np.diagflat([[1, 2], [3, 4]]))   # 展平为 [1,2,3,4] 再摆对角线 → 4x4
```

3. 需要观察的现象：`eye(3,k=1)` 只有 2 个 1；`diag` 对 1D 输入构造、对 2D 输入抽取；`diagflat` 把嵌套输入先拉平。
4. 预期结果：`np.diagflat([[1,2],[3,4]])` 应得到 `diag([1,2,3,4])`，即 `4×4` 对角阵 `[1,2,3,4]`。
5. 待本地验证：以上均可由源码推出；可额外试 `np.eye(3, 3, k=3)`，按源码 `k>=M`（`3>=3`）应直接返回全零矩阵。

#### 4.2.5 小练习与答案

**练习 1**：`np.diag(np.array([2,3,4]), k=-1)` 的形状和内容是什么？

答案：`n = 3 + abs(-1) = 4`，结果是 `4×4` 方阵，第 `-1` 条对角线（主对角线下方一条）为 `[2,3,4]`，即 `(1,0),(2,1),(3,2)` 处分别为 `2,3,4`，其余为 0。

**练习 2**：为什么 `diagflat` 要先 `v.ravel()` 而 `diag` 对 1D 输入不需要？

答案：`diag` 对 1D 输入直接当对角线用，假设调用者已传一维；若传 2D 它会走「抽取」分支。`diagflat` 的语义是「任意形状输入都先展平」，所以无条件 `ravel()` 后再摆对角线，二者分工不同。

### 4.3 vander：范德蒙矩阵与累乘技巧

#### 4.3.1 概念说明

范德蒙矩阵（Vandermonde）是数值分析里的常客——多项式拟合、插值都离不开它。给定一维数组 `x` 和列数 `N`，它的第 `i` 行第 `j` 列是 `x[i]` 的某次幂：

- `increasing=False`（默认）：列从高次到低次，第 `j` 列 = `x^(N-1-j)`，即首列 `x^(N-1)`、末列 `x^0=1`。
- `increasing=True`：列从低次到高次，第 `j` 列 = `x^j`。

一个朴素实现是「对每列单独算 `x**p`」，要做 `N` 次幂运算。NumPy 的实现只用**一次累乘**就生成全部列，关键在于「反向视图」这一招。

#### 4.3.2 核心流程

```
输入 x (1D)、N、increasing
1. v = empty((len(x), N))
2. tmp = v[:, ::-1] if not increasing else v     # 关键：默认情况把写目标「反过来」
3. tmp[:, 0] = 1                                  # 第一列（tmp 视角）置 1
4. tmp[:, 1:] = x[:, None]                        # 其余列先全填 x
5. multiply.accumulate(tmp[:, 1:], axis=1)        # 沿轴 1 累乘 → [x, x², x³, ...]
6. return v
```

为什么这样就能得到正确的幂次？累乘 `[1, x, x, x, ...]` 沿轴 1 的累积积是 `[1, x, x², x³, ...]`。当 `increasing=True`，`tmp` 就是 `v`，结果自然是升幂；当 `increasing=False`，`tmp` 是 `v` 的反向视图，写入 `tmp` 等于写入 `v` 的反向列，于是 `v` 自动变成降幂。一个布尔分支换一个视图，复用同一段累乘逻辑。

#### 4.3.3 源码精读

`vander` 的 dispatcher 只返回 `x`：[_twodim_base_impl.py:L582-L588](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L582-L588)。实现体非常紧凑：[_twodim_base_impl.py:L659-L674](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L659-L674)

```python
x = asarray(x)
if x.ndim != 1:
    raise ValueError("x must be a one-dimensional array or sequence.")
if N is None:
    N = len(x)

v = empty((len(x), N), dtype=promote_types(x.dtype, int))
tmp = v[:, ::-1] if not increasing else v

if N > 0:
    tmp[:, 0] = 1
if N > 1:
    tmp[:, 1:] = x[:, None]
    multiply.accumulate(tmp[:, 1:], out=tmp[:, 1:], axis=1)

return v
```

三个细节值得注意：

- `dtype=promote_types(x.dtype, int)`：即便 `x` 是浮点，结果也会与 `int` 提升后确定类型，保证 `x=1` 时列也安全。
- `tmp[:, ::-1]` 是 `v` 的**视图**（不拷贝），所以对 `tmp` 的写入直接落到 `v` 上。
- `multiply.accumulate(..., out=tmp[:, 1:], axis=1)`：`multiply` 是 ufunc，`.accumulate` 是它的「累积」方法（等价于 `cumprod`），`out=` 让结果原地写回，省一次分配。

源码注释标注「Originally borrowed from John Hunter and matplotlib」——这套写法是从 matplotlib 借来的。

#### 4.3.4 代码实践

1. 实践目标：构造 4 阶范德蒙矩阵，对比 `increasing` 两种顺序，并验证「累乘」招数的正确性。
2. 操作步骤（示例代码）：

```python
import numpy as np

x = np.array([1, 2, 3, 5])
V = np.vander(x, 4)                 # 默认 decreasing
print(V)
# 期望（decreasing）：每行 [x^3, x^2, x^1, x^0]
#   [[  1,  1,  1,  1],
#    [  8,  4,  2,  1],
#    [ 27,  9,  3,  1],
#    [125, 25,  5,  1]]

print(np.vander(x, 4, increasing=True))
# 期望（increasing）：每行 [x^0, x^1, x^2, x^3]

# 验证累乘招数：手写一遍
tmp = np.empty((4, 4), dtype=int)
tmp[:, 0] = 1
tmp[:, 1:] = x[:, None]
np.multiply.accumulate(tmp[:, 1:], out=tmp[:, 1:], axis=1)
print(tmp)        # 期望与 v[:, ::-1] 一致，即等于 decreasing 的「反向」
```

3. 需要观察的现象：`decreasing` 的首列是 `x^3`、末列全是 `1`；`increasing` 恰好相反；手写的 `tmp` 应等于 `V[:, ::-1]`。
4. 预期结果：`np.vander([1,2,3,5], 4)` 与 docstring 一致；方阵时其行列式等于两两差之积（见 docstring 的 `np.linalg.det` 示例，结果为 `48`）。
5. 待本地验证：以上可由源码直接推出。

#### 4.3.5 小练习与答案

**练习 1**：`np.vander([2,3], N=3)` 的第二行是什么？

答案：`decreasing` 下第二行 `[x^2, x^1, x^0] = [9, 3, 1]`。

**练习 2**：如果把 `tmp[:, ::-1]` 改成 `tmp`（即不反向），`increasing=False` 时结果会变成什么样？

答案：会变成升幂（等价于 `increasing=True` 的结果），即首列全 1、末列是 `x^(N-1)`。反向视图正是用来在「同一段累乘逻辑」下翻转幂序的。

### 4.4 fliplr 与 flipud：零拷贝翻转

#### 4.4.1 概念说明

`fliplr`（flip left-right）沿轴 1 反转，`flipud`（flip up-down）沿轴 0 反转。它们是更通用的 `np.flip(m, axis)` 的两个常用快捷方式。关键特性：它们返回的是**视图**，时间复杂度是 \(\mathcal O(1)\)——因为翻转只是把对应轴的步长取负，并不搬运数据。

#### 4.4.2 核心流程

```
fliplr(m)：要求 m.ndim >= 2，返回 m[:, ::-1]
flipud(m)：要求 m.ndim >= 1，返回 m[::-1, ...]
```

`m[:, ::-1]` 把第 1 轴步长取负，于是「从右往左」读；`m[::-1, ...]` 把第 0 轴步长取负，于是「从下往上」读。`...`（Ellipsis）保证对任意维度数都正确。

#### 4.4.3 源码精读

两者共用同一个 dispatcher `_flip_dispatcher`（只返回 `m`）：[_twodim_base_impl.py:L60-L64](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L60-L64)。

`fliplr` 的实现只有三行：[_twodim_base_impl.py:L114-L117](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L114-L117)

```python
m = asanyarray(m)
if m.ndim < 2:
    raise ValueError("Input must be >= 2-d.")
return m[:, ::-1]
```

`flipud` 几乎一样，只是轴和维度下限不同：[_twodim_base_impl.py:L172-L175](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L172-L175)

```python
m = asanyarray(m)
if m.ndim < 1:
    raise ValueError("Input must be >= 1-d.")
return m[::-1, ...]
```

注意 `flipud` 接受 1D 输入（如 `np.flipud([1,2])` 得到 `[2,1]`），而 `fliplr` 至少要 2D——因为「左右翻转」对一维数组没有定义。

#### 4.4.4 代码实践

1. 实践目标：确认翻转返回视图（共享内存）、且 `fliplr`/`flipud` 对维度数的要求不同。
2. 操作步骤（示例代码）：

```python
import numpy as np

A = np.arange(12).reshape(3, 4)
print(np.fliplr(A))            # 每行倒序
print(np.flipud(A))            # 行顺序倒过来

# 验证视图
B = np.fliplr(A)
print(np.shares_memory(A, B))  # 期望 True

# 维度要求差异
print(np.flipud([1, 2, 3]))    # 合法：[3 2 1]
try:
    np.fliplr([1, 2, 3])       # 非法：1D 不允许
except ValueError as e:
    print("fliplr 报错:", e)
```

3. 需要观察的现象：`shares_memory` 为 `True`；`flipud` 对 1D 合法、`fliplr` 对 1D 抛 `ValueError`。
4. 预期结果：如上。可对比更通用的 `np.flip(A, axis=1)`，结果与 `fliplr` 完全相同。
5. 待本地验证：以上可由源码推出。

#### 4.4.5 小练习与答案

**练习 1**：对一个 `(2,3,4)` 的数组 `A`，`np.fliplr(A)` 翻转的是哪个轴？与 `A[:, ::-1, ...]` 等价吗？

答案：翻转轴 1。等价——源码就是 `m[:, ::-1]`，对三维数组而言 `[:, ::-1]` 作用在第 1 轴，等价于 `A[:, ::-1, ...]`。

**练习 2**：为什么 `fliplr` 的 docstring 说它是 \(\mathcal O(1)\)？

答案：因为它只返回 `m[:, ::-1]`，这是一个步长取负的视图，不复制任何数据，耗时与数组大小无关。

### 4.5 tri / tril / triu / mask_indices：三角矩阵工具

#### 4.5.1 概念说明

这是一组围绕「三角矩阵」的工具。`tri` 是地基，`tril`/`triu` 在它之上盖楼，`mask_indices` 则把「三角掩码」抽象成通用接口。

- `tri(N, M, k)`：直接生成一个 `(N,M)` 的三角矩阵——第 `k` 条对角线及其下方全 1，其余 0。即 `T[i,j] == 1 ⟺ j <= i+k`。
- `tril(m, k)`：返回 `m` 的副本，把第 `k` 条对角线**上方**的元素置 0。
- `triu(m, k)`：返回 `m` 的副本，把第 `k` 条对角线**下方**的元素置 0。
- `mask_indices(n, mask_func, k)`：通用接口——给一个「像 `tril`/`triu` 那样接收 `(a, k)` 返回掩码数组」的函数，返回掩码非零处的 `(行, 列)` 下标。

#### 4.5.2 核心流程

**tri 的流程**（用 outer 运算一次性生成）：

```
1. m = greater_equal.outer(arange(N), arange(-k, M-k))
   # m[i,j] = (i >= j-k) = (j <= i+k)，正是「k 对角线及以下」
2. m = m.astype(dtype)
```

**tril / triu 的流程**（复用 tri 当掩码）：

```
tril(m, k):
  mask = tri(N, M, k=k)         # 下三角含对角线 k 为 True
  return where(mask, m, 0)

triu(m, k):
  mask = tri(N, M, k=k-1)       # 下三角含对角线 k-1 为 True
  return where(mask, 0, m)      # 把这部分置 0，留下「对角线 k 及以上」
```

`triu` 用 `k-1` 是个巧妙点：「对角线 k 及以上」恰好是「对角线 k-1 以下」的补集，于是 `tri` 这一个函数就能服务上下两个三角。

**mask_indices 的流程**：

```
1. m = ones((n, n))            # 全 1 方阵
2. a = mask_func(m, k)         # 套上掩码函数（如 tril）
3. return nonzero(a != 0)      # 返回非零处的 (行, 列) 下标
```

#### 4.5.3 源码精读

**tri** 同样用 `@finalize_array_function_like` + `@set_module`（支持 `like=`）：[_twodim_base_impl.py:L390-L392](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L390-L392)。核心一行用 `greater_equal.outer`：[_twodim_base_impl.py:L456-L460](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L456-L460)

```python
m = greater_equal.outer(arange(N, dtype=_min_int(0, N)),
                        arange(-k, M - k, dtype=_min_int(-k, M - k)))
# Avoid making a copy if the requested type is already bool
m = m.astype(dtype, copy=False)
```

`_min_int(low, high)`（[_twodim_base_impl.py:L49-L57](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L49-L57)）按取值范围挑最小的整数类型（int8/int16/int32/int64），避免大数组上无谓用 int64。注意 tri 里还有一段 NumPy 2.5（2026-03）新增的弃用警告：若 `N`/`M`/`k` 不能安全转整数会发 `DeprecationWarning`：[_twodim_base_impl.py:L462-L469](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L462-L469)

**tril** / **triu** 共用 dispatcher `_trilu_dispatcher`：[_twodim_base_impl.py:L477-L481](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L477-L481)。两者都用 `m.shape[-2:]` 取最后两维建掩码，因此对 `ndim>2` 的数组会作用在最后两个轴上：[_twodim_base_impl.py:L531-L534](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L531-L534) 与 [_twodim_base_impl.py:L576-L579](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L576-L579)

```python
# tril
m = asanyarray(m)
mask = tri(*m.shape[-2:], k=k, dtype=bool)
return where(mask, m, zeros(1, m.dtype))

# triu
m = asanyarray(m)
mask = tri(*m.shape[-2:], k=k - 1, dtype=bool)
return where(mask, zeros(1, m.dtype), m)
```

注意 `triu` 传给 `tri` 的是 `k - 1`，而 `tril` 传 `k`——这是「上三角 = 下三角(k-1) 的补」的实现。

**mask_indices** 用 `@set_module('numpy')`（不是 dispatcher），实现极简：[_twodim_base_impl.py:L865-L931](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L865-L931)，核心三行：[_twodim_base_impl.py:L929-L931](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L929-L931)

```python
m = ones((n, n), int)
a = mask_func(m, k)
return nonzero(a != 0)
```

**mask_indices 与 tril_indices/triu_indices 的关系。** 同文件还有 `tril_indices`/`triu_indices`（[_twodim_base_impl.py:L934-L1016](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L934-L1016)、[_twodim_base_impl.py:L1081-L1179](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L1081-L1179)），它们返回的也是「三角元素的下标」，语义上等价于 `mask_indices(n, np.tril, k)` / `mask_indices(n, np.triu, k)`。但实现不同——`tril_indices` 直接用 `tri` 当掩码、配合 `indices(..., sparse=True)` 与 `broadcast_to` 取下标，不必真的物化一个被 `tril` 处理过的数组：[_twodim_base_impl.py:L1013-L1016](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L1013-L1016)

```python
tri_ = tri(n, m, k=k, dtype=bool)
return tuple(broadcast_to(inds, tri_.shape)[tri_]
             for inds in indices(tri_.shape, sparse=True))
```

一句话总结关系：`mask_indices` 是「给我任意掩码函数，我返回下标」的通用接口（实现朴素：建 ones→套函数→取 nonzero）；`tril_indices`/`triu_indices` 是针对三角的特化、更直接的实现。两者结果一致，选哪个看是否需要自定义掩码函数。

#### 4.5.4 代码实践

1. 实践目标：用 `tril`/`triu` 取三角、用 `mask_indices` 拿下标，验证 `mask_indices(n, np.tril)` 与 `tril_indices(n)` 等价。
2. 操作步骤（示例代码）：

```python
import numpy as np

A = np.arange(9).reshape(3, 3)
print(np.tril(A))              # 下三角
print(np.triu(A, k=1))         # 严格上三角（主对角线及以上一条）
print(np.tri(3, 5, 2, dtype=int))   # tri 直接生成

# mask_indices 与 tril_indices 的关系
il_mask = np.mask_indices(3, np.tril)
il_direct = np.tril_indices(3)
print(il_mask[0].tolist(), il_mask[1].tolist())
print(il_direct[0].tolist(), il_direct[1].tolist())
print((il_mask[0].tolist() == il_direct[0].tolist()) and
      (il_mask[1].tolist() == il_direct[1].tolist()))   # 期望 True

# 用下标批量取值
print(A[il_direct])            # 取下三角元素
```

3. 需要观察的现象：`triu(A, k=1)` 把主对角线也置 0；`mask_indices(3, np.tril)` 与 `tril_indices(3)` 返回完全相同的两组下标。
4. 预期结果：最后一行打印 `True`，说明两者等价。
5. 待本地验证：以上可由源码推出；可额外试 `np.mask_indices(3, np.triu, 1)` 与 `np.triu_indices(3, 1)` 对照。

#### 4.5.5 小练习与答案

**练习 1**：对一个 `(2,3,4)` 的数组 `A`，`np.tril(A)` 作用在哪些轴上？输出形状是什么？

答案：作用在最后两个轴（`m.shape[-2:]`，即 `3×4` 那两维），对每个 `3×4` 切片取下三角。输出形状仍是 `(2,3,4)`，只是每个切片的上三角被置 0。

**练习 2**：为什么 `triu` 内部调用的是 `tri(*shape, k=k-1)` 而不是 `tri(*shape, k=k)`？

答案：`tri(k=k-1)` 标出的是「对角线 k-1 及以下」为 True，即「严格在对角线 k 之下」。`triu` 要保留「对角线 k 及以上」，正好是前者的补集，于是 `where(mask, 0, m)` 把「严格在对角线 k 之下」置 0。若用 `tri(k=k)`，会把主对角线 k 也误置 0。

## 5. 综合实践

把本讲的 `vander`、`kron`、`eye`、`tril` 串起来，完成一个小任务：**构造一个「分块上三角」的混合矩阵，并验证其结构。**

要求：

1. 用 `np.vander([1, 2, 3], 3)` 得到一个 `3×3` 范德蒙矩阵 `V`（降幂）。
2. 用 `np.kron(np.eye(2), np.ones((2, 2)))` 得到一个 `4×4` 的分块对角矩阵 `B`（对角块为全 1）。
3. 把 `V`（补 0 到 `4×4`）放在左上角、`B` 放在右下角，拼成一个 `7×7` 的「块上三角」矩阵——上三角由 `np.triu` 保证。

参考实现（示例代码）：

```python
import numpy as np

V = np.vander([1, 2, 3], 3)                       # 3x3 范德蒙
B = np.kron(np.eye(2, dtype=int), np.ones((2, 2), dtype=int))  # 4x4 分块对角

M = np.zeros((7, 7), dtype=int)
M[:3, :3] = V                                     # 左上角放 V
M[3:, 3:] = B                                     # 右下角放 B
M = np.triu(M)                                    # 强制成上三角（挖掉左下角的零块之外的部分）

print(M)
print("V 是否在左上角:", np.array_equal(M[:3, :3], V))
print("B 是否在右下角:", np.array_equal(M[3:, 3:], B))
```

观察要点：

- `V` 应为 `[[1,1,1],[4,2,1],[9,3,1]]`（降幂范德蒙）；`B` 应为对角块全 1、非对角块全 0 的 `4×4`。
- `np.triu(M)` 会把 `M` 左下角（含 `V` 下方、`B` 左侧的零区域以及任何非零）清零，由于原本左下就是 0，结构不变——这正体现了「上三角工具如何保护块结构」。
- 进阶：把 `V` 改成 `np.vander([1,2,3], 4)`（变 `3×4`），思考 `M` 的形状与分块位置该如何调整。

## 6. 本讲小结

- `tile` 是「复印机」（按份数复制元素），`kron` 是「放大镜」（每个元素撑成一块 `b`）；`kron` 输出形状是两数组逐轴相乘，靠「奇/偶位置交错插轴 + 广播相乘 + reshape」实现。
- `eye`/`diag`/`diagflat` 共用「跨步写扁平内存」招数：`res[:n-k].flat[i::n+1] = data`，其中 `i` 由对角线偏移 `k` 决定，`[:n-k]` 防止跨步串到下一行。
- `diag` 是双向函数：1D 输入构造方阵、2D 输入抽取对角线（委托 `diagonal`）；`diagflat` 无条件先 `ravel()`。
- `vander` 用「反向视图 + 一次累乘」生成范德蒙矩阵：累乘 `[1,x,x,...]` 得 `[1,x,x²,...]`，反向视图让 `increasing=False` 复用同一段逻辑。
- `fliplr`/`flipud` 返回步长取负的视图，是 \(\mathcal O(1)\)；`fliplr` 至少要 2D、`flipud` 接受 1D。
- `tri` 用 `greater_equal.outer` 一次生成三角掩码；`tril` 传 `k`、`triu` 传 `k-1`（上三角 = 下三角 k-1 的补）；`mask_indices` 是通用「掩码→下标」接口，与特化的 `tril_indices`/`triu_indices` 结果等价但实现不同。

## 7. 下一步学习建议

- 本讲的 `kron`/`tile` 都属于「形状操作」，与 [u3-l1](u3-l1-expand-and-apply-axis.md)（`expand_dims`/`apply_along_axis`）、[u3-l2](u3-l2-stack-and-split.md)（`stack`/`split`）同属 `_shape_base_impl.py` 家族，建议把它们连起来读，建立「形状操作全景图」。
- 想深入「步长与视图」的底层机制，可继续学习 [u5-l1](u5-l1-as-strided-and-sliding-window.md)：`as_strided` 与 `sliding_window_view`，那里会直接操作 `strides` 字段，是本讲 `fliplr`「步长取负」思想的极端版。
- `eye`/`tri` 用的 `@finalize_array_function_like` 与 `like=` 参数属于 NEP-35，若对「跨后端建数组」（如 CuPy）感兴趣，可阅读 `numpy/_core/overrides.py` 与 `finalize_array_function_like` 的实现。
- 范德蒙矩阵是多项式拟合的基础，学完本讲可顺势进入 [u10-l2](u10-l2-polynomial-poly1d.md)：`polyfit`/`poly1d`，那里会真正用上 `vander` 的列来解最小二乘问题。

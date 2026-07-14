# 迭代器与对角线索引：ndindex/ndenumerate/Arrayterator/fill_diagonal

## 1. 本讲目标

学完本讲后，你应该能够：

- 区分 `np.ndindex` 与 `np.ndenumerate`：一个按形状生成「纯坐标」，另一个按数组生成「坐标 + 值」。
- 理解 `s_` / `index_exp` 这两个 `IndexExpression` 实例如何用「索引语法」构造可复用的索引元组。
- 读懂 `fill_diagonal` 的「跨步写扁平内存」技巧，尤其是 `wrap` 参数对高瘦矩阵的作用。
- 掌握 `diag_indices` / `diag_indices_from` 的「纯索引构造」风格，并理解它与 `fill_diagonal` 的取舍。
- 理解 `Arrayterator` 如何用「running dimension（运行维度）」算法分块缓冲读取大数组，避免一次性载入内存。

## 2. 前置知识

本讲默认你已经读过 **u4-l1（ix_ 与 nd_grid 网格构造）**，知道 `_index_tricks_impl.py` 里的对象（如 `mgrid`/`ogrid`/`r_`/`c_`）都不是普通函数，而是用 `__getitem__` 把「方括号语法」当参数接收的实例对象。本讲里的 `IndexExpression`（即 `s_`/`index_exp`）也属于这一族。

此外需要几个基础概念：

- **扁平视图 `arr.flat`**：把任意形状的数组当成一维序列来遍历的对象，类型是 `numpy.flatiter`。它有一个关键属性 `.coords`，返回当前位置的 N 维坐标元组。`ndenumerate` 正是靠它工作的。
- **C 序（行主序）扁平下标**：对于一个形状为 \((d_0, d_1, \dots, d_{n-1})\) 的数组，元素 \((i_0, i_1, \dots, i_{n-1})\) 在扁平视图里的位置是
  \[
  \text{flat} = \sum_{k=0}^{n-1} i_k \cdot \prod_{j=k+1}^{n-1} d_j
  \]
  这是理解 `fill_diagonal`「跨步写」为何成立的关键。
- **就地修改（in-place）**：`fill_diagonal` 不返回新数组，而是直接改写传入的数组。
- **dispatcher + impl 双函数写法**：见 u1-l2，`@array_function_dispatch(...)` 装饰的公开函数背后挂着 NEP-18 的 `__array_function__` 派发协议。本讲的 `fill_diagonal`、`diag_indices_from` 都是这种写法。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `numpy/lib/_index_tricks_impl.py` | 收纳 `ndenumerate`、`ndindex`、`IndexExpression`（`s_`/`index_exp`）、`fill_diagonal`、`diag_indices`、`diag_indices_from` 等索引/对角工具。本讲的 6 个公开名字都在这里。 |
| `numpy/lib/_arrayterator_impl.py` | 只定义一个类 `Arrayterator`，是「大数组分块缓冲迭代器」的全部实现。 |

这些名字的对外暴露路径（承接 u1-l2 的「再导出层」认知）：

- `ndenumerate`/`ndindex`/`fill_diagonal`/`diag_indices`/`diag_indices_from`/`index_exp`/`s_` 由顶层 [numpy/__init__.py:512-523](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py#L512-L523) 从 `_index_tricks_impl` 取名，搬到 `np.` 命名空间。
- `Arrayterator` 由 [numpy/lib/__init__.py:45](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py#L45) 从 `_arrayterator_impl` 导入，进 `numpy.lib.__all__`（[第 49 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py#L49)），用 `np.lib.Arrayterator` 访问。

## 4. 核心概念与源码讲解

本讲把 7 个最小模块归并为 4 个主题：
1. 索引生成型迭代器：`ndindex` + `ndenumerate`
2. 索引表达式对象：`IndexExpression`（`s_`/`index_exp`）
3. 对角线操作：`fill_diagonal` + `diag_indices` + `diag_indices_from`
4. 大数组缓冲迭代器：`Arrayterator`

### 4.1 索引生成型迭代器：ndindex 与 ndenumerate

#### 4.1.1 概念说明

这两个类解决的都是「我要把一个 N 维数组的每个位置都走一遍」的遍历问题，但输入和产出不同：

- **`ndindex`** 的输入是「形状」（一组维度大小，不是数组），产出是**纯粹的坐标元组**，例如 `(0,0,0)`、`(0,0,1)`、…。它根本不需要任何数据。
- **`ndenumerate`** 的输入是一个**真实的数组**，产出是「(坐标, 值)」二元组，例如 `((0,0), 1.0)`。它要同时给你位置和那里的元素。

一句话区分：`ndindex` 只管「坐标」，`ndenumerate` 还要捎上「值」。

#### 4.1.2 核心流程

**`ndenumerate`** 的实现极其轻量——它把数组摊平成 `flatiter`，每一步同时取「当前坐标」和「下一个值」：

```
初始化：self.iter = asarray(arr).flat   # 拿到 flatiter
每次 __next__：return self.iter.coords, next(self.iter)
```

`flatiter.coords` 是 C 层维护的「当前位置 N 维坐标」，每调用一次 `next()` 它就自动前进一格并更新坐标，于是 `ndenumerate` 几乎是白嫖了底层迭代器。

**`ndindex`** 不依赖任何数组，而是用 Python 标准库的 `itertools.product` 直接对「每一维的 range」做笛卡尔积：

```
初始化：
    若只传了一个元组，就把它展开成多个参数
    若有负维度，抛 ValueError
    self._iter = product(*map(range, shape))
每次 __next__：return next(self._iter)
```

`product(range(d0), range(d1), ...)` 正好按「最后一维变化最快」的顺序枚举所有坐标，与 C 序遍历一致。等价地，`list(ndindex(shape))` 得到的坐标序列，和 `ndenumerate(zeros(shape))` 取出的坐标序列**完全相同**——这正是测试 `test_ndindex_against_ndenumerate_compatibility` 验证的事实。

#### 4.1.3 源码精读

先看 `ndenumerate`，它就是「flatiter 的坐标 + 值」的薄包装：

[numpy/lib/_index_tricks_impl.py:619-620](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L619-L620) —— `__init__` 把数组转成 `flatiter` 存起来。

[numpy/lib/_index_tricks_impl.py:622-634](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L622-L634) —— `__next__` 同时返回 `self.iter.coords`（坐标元组）和 `next(self.iter)`（标量值）。整个类的「业务逻辑」就这一行。

再看 `ndindex`：

[numpy/lib/_index_tricks_impl.py:687-692](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L687-L692) —— 构造逻辑：若只传一个元组就拆开；用 `min(shape, default=0) < 0` 拦截负维度；最终把形状喂给 `product(*map(range, shape))`。注意 `default=0` 让空形状 `ndindex()` 也能通过（返回单元素 `[()]`）。

[numpy/lib/_index_tricks_impl.py:697-709](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L697-L709) —— `__next__` 直接 `return next(self._iter)`，把笛卡尔积的下一项交出去。

一个容易踩的坑：两个类的 `__iter__` 都是 `return self`（见 ndenumerate 的 [`__iter__`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L636-L637) 与 ndindex 的 [`__iter__`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L694-L695)）。这意味着**每个实例是一次性迭代器**：一旦耗尽就保持耗尽，`next()` 会抛 `StopIteration`。要重新遍历必须新建实例——这正是测试 `test_ndindex_stop_iteration_behavior` 检查的行为。

#### 4.1.4 代码实践

1. **实践目标**：直观对比 `ndindex`（只给坐标）与 `ndenumerate`（给坐标 + 值）的产出差异。
2. **操作步骤**：运行下面这段「示例代码」。

```python
# 示例代码
import numpy as np

a = np.array([[1, 2], [3, 4]])

print("== ndenumerate：坐标 + 值 ==")
for idx, val in np.ndenumerate(a):
    print(idx, val)

print("== ndindex：只有坐标 ==")
for idx in np.ndindex(2, 2):
    print(idx)
```

3. **需要观察的现象**：`ndenumerate` 打印 `(0, 0) 1`、`(0, 1) 2`、…；`ndindex` 打印同样的坐标但不带值。
4. **预期结果**：两者的坐标序列完全一致，仅差「是否附带值」。可直接对照源码 docstring 里给出的输出（[ndenumerate 示例](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L607-L615)、[ndindex 示例](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L665-L683)）。
5. 若本地未装 numpy，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`list(np.ndindex(0, 5))` 的结果是什么？为什么？

**答案**：结果是空列表 `[]`。因为某一维大小为 0，`product(range(0), range(5))` 笛卡尔积里 `range(0)` 是空的，整个乘积为空。这正是测试 `test_ndindex_zero_dimensions_explicit` 的断言。

**练习 2**：用 `ndindex` 遍历一个形状为 `(2, 3)` 的全零数组，并断言它产出的坐标与 `ndenumerate` 的坐标完全相同。

**答案**：

```python
shape = (2, 3)
z = np.zeros(shape)
assert list(np.ndindex(shape)) == [ix for ix, _ in np.ndenumerate(z)]
```

---

### 4.2 索引表达式对象：IndexExpression（s_ / index_exp）

#### 4.2.1 概念说明

在写复杂索引时，你常常想「把一段切片先存起来，以后再用」。直接写 `slice(2, None, 2)` 太啰嗦，而 `np.s_[2::2]` 能让你用熟悉的方括号语法构造它。`IndexExpression` 就是这个语法糖背后的类，它有两个预定义实例：

- `s_ = IndexExpression(maketuple=False)`
- `index_exp = IndexExpression(maketuple=True)`

唯一区别是 `maketuple`：`s_` 原样返回切片对象，`index_exp` 会把单个对象包成单元素元组。

#### 4.2.2 核心流程

```
__getitem__(item):
    若 maketuple 且 item 不是 tuple：返回 (item,)
    否则：原样返回 item
```

就这么简单——它只是一个「用方括号语法捕获索引表达式」的壳。

#### 4.2.3 源码精读

[numpy/lib/_index_tricks_impl.py:773-777](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L773-L777) —— `__getitem__` 的全部逻辑：`maketuple=True` 时把非元组的 item 包成单元素元组，否则原样返回。

[numpy/lib/_index_tricks_impl.py:780-781](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L780-L781) —— 预定义两个实例：`index_exp`（包元组）和 `s_`（不包）。

典型效果（见 docstring 示例 [第 758-765 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L758-L765)）：

```
np.s_[2::2]          -> slice(2, None, 2)
np.index_exp[2::2]   -> (slice(2, None, 2),)
```

为什么要分两种？`index_exp` 产出的元组可以直接拼进更大的索引表达式里（比如 `a[index_exp[2:] + index_exp[:3]]`），而 `s_` 更适合「我就想要这个切片本身」的场景。注意源码注释 [第 712-717 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L712-L717) 说明它源自 1999 年 Konrad Hinsen 的贡献，动机就是「比手写 `slice()` 加几个特殊对象好记得多」。

#### 4.2.4 代码实践

1. **实践目标**：体会 `maketuple` 的差异，并把构造出的索引用于真实取数。
2. **操作步骤**：运行下面这段「示例代码」。

```python
# 示例代码
import numpy as np

a = np.arange(10)
print(np.s_[2::2])         # slice(2, None, 2)
print(np.index_exp[2::2])  # (slice(2, None, 2),)

# 二者用于取数结果相同
assert (a[np.s_[2::2]] == a[2::2]).all()
```

3. **需要观察的现象**：`s_` 返回裸 `slice`，`index_exp` 返回单元素元组。
4. **预期结果**：两条取数结果一致，都是 `array([2, 4, 6, 8])`。

#### 4.2.5 小练习与答案

**练习**：用 `s_` 构造一个「插入新轴 + 切片」的索引，把一维数组 `a = np.arange(5)` 变成形状 `(5, 1)` 的列向量。

**答案**：`a[np.s_[:, None]]` 等价于 `a[:, None]`，得到形状 `(5, 1)`。`None`/`newaxis` 也是 `IndexExpression` 会原样透传的「特殊对象」之一。

---

### 4.3 对角线操作：fill_diagonal 与 diag_indices / diag_indices_from

#### 4.3.1 概念说明

「往数组主对角线写值」有两种风格，本模块正好各占一种：

- **就地跨步写（`fill_diagonal`）**：不构造任何下标数组，直接算出一个「跨步」，用 `a.flat[::step] = val` 一次性把值刷进对角线。快、省内存，但只能写、且要求高维数组各维等长。
- **显式索引构造（`diag_indices` / `diag_indices_from`）**：先算出对角线的坐标元组（一组 `arange`），再让你自己决定是读还是写。灵活、可读、可复用，但要分配索引数组。

docstring 里 [第 822-825 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L822-L825) 明确点出：`fill_diagonal` 的功能也能用 `diag_indices` 实现，但内部用的是「不构造索引、只用简单切片」的更快实现。

#### 4.3.2 核心流程

**`fill_diagonal(a, val, wrap=False)`** 的关键是算出 C 序扁平视图里相邻两个对角元素的间距 `step`：

- 对 2 维形状 `(rows, cols)`，对角元素 \((i,i)\) 的扁平下标是 \(i\cdot cols + i = i\cdot(cols+1)\)，所以相邻间距
  \[
  \text{step}_{2D} = cols + 1 = \text{shape}[1] + 1
  \]
- 对 \(d\) 维且各维都等于 \(n\) 的数组，对角元素 \((i,i,\dots,i)\) 的扁平下标是
  \[
  i\cdot(n^{d-1} + n^{d-2} + \cdots + n + 1)
  \]
  于是间距
  \[
  \text{step}_{dD} = 1 + \sum_{k=1}^{d-1} n^k
  \]
  代码用 `1 + cumprod(shape[:-1]).sum()` 等价地算出它（见下方源码）。

算出 `step` 后，执行 `a.flat[:end:step] = val`。其中 `end` 控制是否让高瘦矩阵「折返」：

- `wrap=False`（默认）：令 `end = cols * cols`，正好覆盖前 `cols` 个对角位置，**不折返**，多余行保持 0。
- `wrap=True`：令 `end = None`，切片一直走到数组末尾，于是当行数大于列数时，对角线会在底部「折返」再画一条。

**`diag_indices(n, ndim=2)`** 极简：`idx = arange(n); return (idx,) * ndim`，即返回 `ndim` 份相同的 `[0,1,...,n-1]`，作为花式索引直接定位 `a[i,i,...,i]`。

**`diag_indices_from(arr)`** 只是 `diag_indices` 的「从数组推断参数」版本：先校验 `arr.ndim >= 2` 且各维等长，再调用 `diag_indices(arr.shape[0], arr.ndim)`。

#### 4.3.3 源码精读

[numpy/lib/_index_tricks_impl.py:905-923](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L905-L923) —— `fill_diagonal` 的全部主体。逐段看：

- [第 905-906 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L905-L906)：`a.ndim < 2` 直接报错。
- [第 908-914 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L908-L914)：2 维分支。`step = a.shape[1] + 1` 就是上面推导的 \(cols+1\)；当 `not wrap` 时 `end = a.shape[1] * a.shape[1]` 截断，防止高瘦矩阵折返。注释 [第 912 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L912) 直说这条 `end` 就是「不让高瘦矩阵折返」。
- [第 916-920 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L916-L920)：\(d>2\) 分支。先用 `diff(a.shape) == 0` 校验各维等长（否则跨步公式不成立），再 `step = 1 + (np.cumprod(a.shape[:-1])).sum()` 算出多维间距。
- [第 923 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L923)：真正的一行写操作 `a.flat[:end:step] = val`，`val` 若是数组会被广播/重复填满。

`fill_diagonal` 走的是 `array_function_dispatch` 派发写法，dispatcher 见 [第 790-791 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L790-L791)，只把 `a` 暴露给 NEP-18（`val` 不参与派发）。

[numpy/lib/_index_tricks_impl.py:988-990](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L988-L990) —— `diag_indices` 主体：`idx = arange(n); return (idx,) * ndim`。`(idx,) * ndim` 是「同一份 `arange` 引用重复 `ndim` 次」组成的花式索引元组。

[numpy/lib/_index_tricks_impl.py:1041-1048](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L1041-L1048) —— `diag_indices_from` 主体：先 `arr.ndim >= 2` 校验，再 `diff(arr.shape) == 0` 校验各维等长（报错文案与 `fill_diagonal` 完全一致），最后委托 `diag_indices(arr.shape[0], arr.ndim)`。它的 dispatcher 在 [第 993-994 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L993-L994)。

#### 4.3.4 代码实践

1. **实践目标**：观察 `wrap` 对高瘦矩阵的影响，并比较 `fill_diagonal`（就地写）与 `diag_indices`（先取索引再写）两条路径。
2. **操作步骤**：运行下面这段「示例代码」。

```python
# 示例代码
import numpy as np

# (1) wrap=False vs True，高瘦矩阵
a = np.zeros((5, 3), int)
np.fill_diagonal(a, 4)              # 默认不折返
print("no wrap:\n", a)

b = np.zeros((5, 3), int)
np.fill_diagonal(b, 4, wrap=True)   # 折返
print("wrap:\n", b)

# (2) 用 diag_indices 走「显式索引」路径，效果等同 fill_diagonal
c = np.zeros((3, 3), int)
c[np.diag_indices(3)] = 7
print("via diag_indices:\n", c)
```

3. **需要观察的现象**：
   - `no wrap` 的 `(5,3)` 矩阵只有前 3 行主对角为 4，第 4、5 行全 0。
   - `wrap` 的版本在第 4 行 `[4,0,0]` 又出现一个 4（折返）。
4. **预期结果**：与 docstring 示例 [第 859-878 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L859-L878) 完全一致；`c` 的主对角线为 `7`，与 `fill_diagonal(c, 7)` 等价。这与测试 `test_tall_matrix` / `test_tall_matrix_wrap`（[test_index_tricks.py:444-474](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_index_tricks.py#L444-L474)）断言的矩阵逐元素相同。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `fill_diagonal` 对 `d>2` 的数组要求「各维等长」，而 2 维却允许矩形 `(rows, cols)`？

**答案**：2 维的跨步公式 \(step = cols+1\) 与 `end` 截断对任意 `rows >= cols` 都成立，主对角线就是 `min(rows, cols)` 个元素。但 \(d>2\) 的公式 \(step = 1 + \sum n^k\) 只在各维都是同一个 \(n\) 时才正确描述对角线间距；若各维不等长，「主对角」本身就没有良好定义，所以 [第 918-919 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_index_tricks_impl.py#L918-L919) 直接报错。

**练习 2**：用 `diag_indices_from` 取出一个 `(4,4)` 数组的主对角元素（只读不改）。

**答案**：

```python
a = np.arange(16).reshape(4, 4)
print(a[np.diag_indices_from(a)])   # [ 0  5 10 15]
```

---

### 4.4 大数组缓冲迭代器：Arrayterator

#### 4.4.1 概念说明

当你面对一个「存在磁盘上、比内存大得多」的数组（比如 NetCDF 变量、`np.memmap` 映射的大文件），想逐块处理又不想一次性读进内存时，`Arrayterator` 就是答案。它包装任意「支持多维切片」的对象，迭代时每次吐出一个「元素数不超过 `buf_size`」的小块。

要点：`Arrayterator` 只认「切片」协议，不要求对象真的是 `ndarray`——所以它也能包 NetCDF 变量等。

#### 4.4.2 核心流程

构造时记录三份「逐维起点/终点/步长」：`start`、`stop`、`step`，分别初始化为 `[0,...]`、`shape`、`[1,...]`。它有四种用法：

- `iter(at)`：核心算法，按块吐出子数组。
- `at.shape`：当前覆盖区域的形状（由 start/stop/step 算出）。
- `at[切片]`：返回一个**新的 `Arrayterator`**，把覆盖区域再切小（不读数据，类似视图）。
- `at.flat`：逐元素生成器（内部就是 `for block in self: yield from block.flat`）。
- `np.asarray(at)`：触发 `__array__`，把当前覆盖区域真正读成一个数组。

**「运行维度」（running dimension）算法** 是 `__iter__` 的灵魂。给定 `count = buf_size`，它从**最后一维往前**扫描，决定每一维这次读到哪儿：

```
count = buf_size  (或总元素数)
从 i = 最后一维 到 第 0 维：
    若 count == 0：本维只读 1 个位置（高位维度已无元素可读）
    若 count <= shape[i]：本维限制为 count 个 → stop[i] = start[i] + count*step[i]，记 rundim = i
    否则：本维读满，count //= shape[i]，继续看更低的维度
吐出这一块：yield var[ (start,stop,step) 切片 ]
然后 start[rundim] = stop[rundim]（从停下的地方接着读）
处理「进位」：当某维读满就归零并把更高维 +step
直到 start[0] >= stop[0] 结束
```

直觉：`buf_size` 决定「一块最多多少元素」。算法从最低维开始「尽量读满」，当剩余预算 `count` 装不下整维时，就在那一维截断——这一维就成了「运行维度」，块沿它推进。

#### 4.4.3 源码精读

[numpy/lib/_arrayterator_impl.py:88-94](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arrayterator_impl.py#L88-L94) —— `__init__`：存 `var`、`buf_size`，初始化 `start=[0,...]`、`stop=list(shape)`、`step=[1,...]`。

[numpy/lib/_arrayterator_impl.py:96-97](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arrayterator_impl.py#L96-L97) —— `__getattr__` 把一切未知属性（如 `dtype`）委托给 `self.var`，所以 `Arrayterator` 用起来像它包装的对象。注意 `shape` 被下面的 property 覆盖。

[numpy/lib/_arrayterator_impl.py:99-129](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arrayterator_impl.py#L99-L129) —— `__getitem__` 返回**新的 `Arrayterator`**（同 `var`、同 `buf_size`），把传入切片并入 start/stop/step。关键：整数下标被转成 `slice(i, i+1, 1)`（保留该维长度为 1，而非降维），`Ellipsis` 展开成若干 `slice(None)`，不足的维度补全 `slice(None)`。这一步**不读数据**。

[numpy/lib/_arrayterator_impl.py:131-138](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arrayterator_impl.py#L131-L138) —— `__array__`：把 start/stop/step 拼成切片元组，真正执行 `self.var[slice_]` 读出数据。这就是 `np.asarray(at)` 或任何需要数组的场合触发的「物化」点。

[numpy/lib/_arrayterator_impl.py:169-178](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arrayterator_impl.py#L169-L178) —— `shape` property：用 \((\text{stop}-\text{start}-1)//\text{step}+1\) 逐维算出当前覆盖区域的形状（这是「长度为 L、步长 s 的切片有多少元素」的标准公式 \(\lceil L/s \rceil\) 的整数版）。

[numpy/lib/_arrayterator_impl.py:180-224](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arrayterator_impl.py#L180-L224) —— `__iter__`，运行维度算法的完整实现：
- [第 191 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arrayterator_impl.py#L191)：`count = self.buf_size or 总元素数`（`buf_size=None` 时读尽可能多）。
- [第 197-210 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arrayterator_impl.py#L197-L210)：从最后一维向第 0 维回扫，按上面流程决定每维 `stop`，并定位 `rundim`。
- [第 213-214 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arrayterator_impl.py#L213-L214)：拼切片、`yield self.var[slice_]` 吐出这一块。
- [第 218-224 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arrayterator_impl.py#L218-L224)：推进 `start[rundim]`，处理跨维进位，`start[0] >= self.stop[0]` 时 `return` 结束。

[numpy/lib/_arrayterator_impl.py:140-167](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arrayterator_impl.py#L140-L167) —— `flat` property：对每块 `yield from block.flat`，把分块迭代展平成逐元素流。注释里指出它类似 `flatiter`。

模块级 docstring [第 1-13 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arrayterator_impl.py#L1-L13) 与类 docstring [第 56-65 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arrayterator_impl.py#L56-L65) 给出了运行维度算法的官方直觉说明，可直接对照阅读。

#### 4.4.4 代码实践

1. **实践目标**：用小 `buf_size` 让 `Arrayterator` 把一个大数组切成多块，验证「每块不超过 `buf_size`」且「合起来等于原数组」。
2. **操作步骤**：运行下面这段「示例代码」（思路同官方测试 [test_arrayterator.py:10-45](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_arrayterator.py#L10-L45)）。

```python
# 示例代码
import numpy as np
from numpy.lib import Arrayterator

a = np.arange(24).reshape(2, 3, 4)
at = Arrayterator(a, buf_size=5)   # 每块最多 5 个元素

print("shape:", at.shape)          # (2, 3, 4)，等同 a.shape
for i, block in enumerate(at):
    print(f"block {i}: shape={block.shape}, size={block.size}")
    assert block.size <= 5

# 合起来应等于原数组的扁平序列
assert list(at.flat) == list(a.flat)
```

3. **需要观察的现象**：每块 `size <= 5`；块的形状是原数组的某个子切片；`at.flat` 展平后与 `a.flat` 逐元素相同。
4. **预期结果**：`assert` 全部通过，说明「分块遍历覆盖了全部元素且无重复」。若本地未装 numpy，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`buf_size=None`（默认）时，`Arrayterator` 迭代会吐出几块？

**答案**：只吐出一块——整个数组。因为 [第 191 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arrayterator_impl.py#L191) `count = self.buf_size or reduce(mul, self.shape)`，`buf_size=None` 时 `count` 等于总元素数，第一轮从最后一维回扫时每一维都能「读满」，最终一块覆盖整个数组。

**练习 2**：为什么 `at[1:3]` 返回的是新的 `Arrayterator` 而不是一个 `ndarray`？

**答案**：因为 [第 122-129 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arrayterator_impl.py#L122-L129) 的 `__getitem__` 构造并返回了一个新的 `Arrayterator`（同 `var`、同 `buf_size`，仅收紧 start/stop/step），它只是「记住」要怎么切，并不真正读数据。只有当你对它调 `np.asarray()`（触发 [第 131-138 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arrayterator_impl.py#L131-L138) 的 `__array__`）或迭代它时，数据才被读出。

---

## 5. 综合实践

把本讲的 `fill_diagonal`、`ndenumerate`、`ndindex`、`IndexExpression` 串成一个完整任务：

> 构造一个 \(5\times5\) 零矩阵，用 `fill_diagonal` 把主对角线置为 1；再用 `ndenumerate` 遍历它，打印**所有非零元素**的坐标与值；最后用 `ndindex` 验证：对同样形状的全零数组，`ndindex` 产出的坐标序列与 `ndenumerate` 的坐标序列完全一致。

参考实现（「示例代码」）：

```python
# 示例代码
import numpy as np

# 第 1 步：填充主对角线
a = np.zeros((5, 5), int)
np.fill_diagonal(a, 1)

# 第 2 步：用 ndenumerate 打印非零元素坐标
print("非零元素：")
for idx, val in np.ndenumerate(a):
    if val != 0:
        print(f"  a{idx} = {val}")

# 第 3 步：用 ndindex 验证坐标序列一致
z = np.zeros_like(a)
coords_from_ndenumerate = [idx for idx, _ in np.ndenumerate(z)]
coords_from_ndindex = list(np.ndindex(z.shape))
assert coords_from_ndenumerate == coords_from_ndindex
print("ndenumerate 与 ndindex 坐标序列一致 ✓")

# 第 4 步（延伸）：用 s_ 构造一个可复用的「跳步」索引
skip = np.s_[::2]            # slice(None, None, 2)
print("每隔一行的主对角线：", a[skip].diagonal())
```

**预期结果**：第 2 步打印出 `(0,0)`、`(1,1)`、`(2,2)`、`(3,3)`、`(4,4)` 五个坐标，值均为 1；第 3 步断言通过；第 4 步 `a[::2].diagonal()` 给出 `array([1, 1, 1])`（第 0、2、4 行的子对角线）。

> 进阶：把 `fill_diagonal(a, 1)` 换成 `a[np.diag_indices(5)] = 1`，观察结果是否完全相同（应当相同——这就是 4.3 说的「两种风格，殊途同归」）。

## 6. 本讲小结

- `ndenumerate` 包 `flatiter`，靠 `.coords` 同时给出「坐标 + 值」；`ndindex` 只接收形状，靠 `itertools.product` 生成「纯坐标」。两者坐标序列等价，但每个实例都是一次性迭代器。
- `IndexExpression` 是「用方括号语法捕获索引」的壳，`s_`（不包元组）与 `index_exp`（包成单元素元组）仅差一个 `maketuple` 标志。
- `fill_diagonal` 用「跨步写扁平内存」`a.flat[:end:step] = val` 就地刷对角线：2 维 `step = shape[1]+1`、多维 `step = 1 + cumprod(shape[:-1]).sum()`；`wrap` 只影响高瘦矩阵是否折返（通过 `end` 是否截断实现）。
- `diag_indices`/`diag_indices_from` 走「显式索引」路线，返回 `ndim` 份 `arange(n)`；功能与 `fill_diagonal` 重叠但更灵活（可读可写、可复用），代价是要分配索引数组。
- `Arrayterator` 用「运行维度」算法把大对象切成元素数不超过 `buf_size` 的小块，`__getitem__` 只记切片不读数据，`__array__` 才真正物化；适合磁盘/内存映射的大数组分块处理。

## 7. 下一步学习建议

- **接续索引主线**：本讲之后，建议进入 **u5-l1（as_strided 与 sliding_window_view）**，那里会再次用到 `__array_interface__` 与「跨步/视图」的内存关系，与本讲的 `fill_diagonal` 跨步写、`Arrayterator` 切片视图一脉相承。
- **横向扩展对角线知识**：可阅读 `numpy/lib/_twodim_base_impl.py` 中的 `diag`/`diagflat`（见 u3-l3），它们是「构造/抽取」对角线，与这里的「填充/定位」对角线互补。
- **深入 flatiter**：`ndenumerate` 高度依赖 `flatiter.coords`，若想理解其 C 层实现，可追踪 `numpy/_core/src/multiarray/iterator.c`。
- **大数组 IO**：`Arrayterator` 的典型搭档是 `np.memmap`，后续 **u12-l3（数组读写与内存映射）** 会系统讲内存映射，可与本讲对照阅读。

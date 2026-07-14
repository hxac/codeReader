# 归约方法与 _collapse / _align 辅助函数

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `matrix` 为什么必须重写 `sum / mean / std / var / prod / any / all / max / min / argmax / argmin / ptp` 这一整套归约方法。
- 解释「朝向（orientation）」问题：为什么沿 `axis=0` 归约应得到行向量 `(1, N)`，沿 `axis=1` 归约应得到列向量 `(N, 1)`。
- 区分两个私有辅助函数 `_collapse` 与 `_align`：前者依赖 `keepdims=True`，只需把 `(1,1)` 压成标量；后者在**不使用** `keepdims` 时负责手动修正朝向。
- 回答一个看似奇怪的问题：为什么 `argmax / argmin / ptp` 走的是 `_align`，而不是和 `sum` 们一样的 `_collapse`？这背后是一段 numpy 演进史。

本讲承接 [u2-l4](u2-l4-ndarray-subclass-array-finalize.md) 的 `__array_finalize__` 与「永远二维」不变量，把这条不变量落实到**归约**这条最容易掉维的路径上。

## 2. 前置知识

在进入源码前，先用通俗语言对齐几个概念。

- **归约（reduction）**：沿某个轴把多个元素「压」成一个。例如对 `(3, 4)` 的矩阵沿 `axis=0` 求和，就是把每一列的 3 个数加成 1 个数，结果有 4 个数。归约会让维度「缩水」。
- **keepdims**：numpy 归约函数的一个参数。`keepdims=True` 表示被压缩的轴不要删掉，而是保留成长度为 1 的轴。对 `(3, 4)` 沿 `axis=1` 用 `keepdims=True` 求和，结果是 `(3, 1)` 而不是 `(3,)`。
- **朝向（orientation）**：本讲的关键词。沿 `axis=0`（压行）得到「每列一个汇总值」，自然排成**行向量** `(1, N)`；沿 `axis=1`（压列）得到「每行一个汇总值」，自然排成**列向量** `(N, 1)`。
- **`__array_finalize__` 的 1-D 规则**（见 [u2-l4](u2-l4-ndarray-subclass-array-finalize.md)）：任何 1 维中间结果都会被 `__array_finalize__` 补成 `(1, N)` 的**行向量**。这条规则是 `_align` 必须存在的根本原因。
- **`matrix` 处于维护模式**：每次构造都会发 `PendingDeprecationWarning`（见 [u1-l1](u1-l1-project-overview.md)），官方不再新增功能，只做兼容性维护。这一点直接解释了第 4.4 节的「化石代码」现象。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但聚焦其中三类代码点：

| 代码点 | 位置 | 作用 |
| --- | --- | --- |
| `_collapse` | `defmatrix.py` | 给「用了 `keepdims=True`」的归约方法收尾：把 `axis=None` 的 `(1,1)` 结果压成标量 |
| `_align` | `defmatrix.py` | 给「没用 `keepdims`」的归约方法收尾：按轴返回自身 / 转置 / 取标量，修正朝向 |
| 9 个 `_collapse` 方法 | `defmatrix.py` | `sum/mean/std/var/prod/any/all/max/min` |
| 3 个 `_align` 方法 | `defmatrix.py` | `argmax/argmin/ptp` |
| `__array_finalize__` 的 1-D 分支 | `defmatrix.py` | 把 1 维中间结果补成 `(1, N)` 行向量，是 `_align` 要对抗的对象 |

## 4. 核心概念与源码讲解

### 4.1 归约方法总览与「朝向」问题

#### 4.1.1 概念说明

`matrix` 是强制二维的 `ndarray` 子类。但 numpy 基类的归约方法（`ndarray.sum` 等）默认会把结果**降维**：对 `(3, 4)` 的数组 `a`，`a.sum(axis=1)` 返回的是一维的 `(3,)`。这直接破坏了「永远二维」不变量，所以 `matrix` 必须把这些方法逐个重写。

重写时要同时满足两条要求：

1. **保二维**：除「整体归约（`axis=None`）应得到标量」外，结果都必须是二维 `matrix`。
2. **保朝向**：沿 `axis=0` 得行向量 `(1, N)`，沿 `axis=1` 得列向量 `(N, 1)`。

`matrixlib` 用了两套收尾策略来实现这两点：`_collapse` 与 `_align`。

#### 4.1.2 核心流程

12 个被重写的归约方法分成两组：

```
┌─────────────────────────────────────────────┐
│  第一组（9 个）：sum mean std var           │
│   prod any all max min                      │
│   策略：ndarray.<m>(self, ..., keepdims=True)│
│         结果天然 2D 且朝向正确              │
│         → 只需 _collapse 把 (1,1) 压成标量  │
└─────────────────────────────────────────────┘
┌─────────────────────────────────────────────┐
│  第二组（3 个）：argmax argmin ptp          │
│   策略：不传 keepdims，结果是 1D            │
│         __array_finalize__ 把它补成 (1,N)   │
│         → _align 对 axis=1 做 transpose 修正│
└─────────────────────────────────────────────┘
```

两组的差异不在「算什么」，而在「算完之后如何把形状修回二维」。

#### 4.1.3 源码精读

这一节先建立全局印象，具体方法逐行放在 4.2 / 4.3。以 `sum` 为例，它只有一行真正的逻辑：

[defmatrix.py#L293-L325](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L293-L325) —— `sum` 的完整定义；注意第 325 行的返回语句：

```python
return N.ndarray.sum(self, axis, dtype, out, keepdims=True)._collapse(axis)
```

这句话做了三件事：① 调用基类 `ndarray.sum`（绕过 `matrix` 自己，避免递归）；② 显式传 `keepdims=True`；③ 把结果交给 `_collapse(axis)` 收尾。

而 `argmax` 长得几乎一样，却有两处关键不同：

[defmatrix.py#L689](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L689) —— `argmax` 的返回语句：

```python
return N.ndarray.argmax(self, axis, out)._align(axis)
```

没有 `keepdims=True`，收尾函数也换成了 `_align`。这两处差异就是本讲的全部主线。

#### 4.1.4 代码实践

**实践目标**：用「读返回语句」的方式，把 12 个归约方法分成两组。

**操作步骤**：

1. 打开 [defmatrix.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py)，依次跳到下列行号，只看每个方法最后一行 `return`。
2. 记下每个 `return` 里是否出现 `keepdims=True`，以及收尾函数是 `_collapse` 还是 `_align`。

**预期结果**（可直接对照）：

| 方法 | 定义行 | 返回行 | keepdims | 收尾函数 |
| --- | --- | --- | --- | --- |
| `sum` | 293 | 325 | True | `_collapse` |
| `mean` | 417 | 449 | True | `_collapse` |
| `std` | 451 | 483–484 | True | `_collapse` |
| `var` | 486 | 518–519 | True | `_collapse` |
| `prod` | 521 | 552 | True | `_collapse` |
| `any` | 554 | 575 | True | `_collapse` |
| `all` | 577 | 615 | True | `_collapse` |
| `max` | 617 | 650 | True | `_collapse` |
| `min` | 691 | 724 | True | `_collapse` |
| `argmax` | 652 | 689 | —— | `_align` |
| `argmin` | 726 | 763 | —— | `_align` |
| `ptp` | 765 | 796 | —— | `_align` |

9 个用 `_collapse`，3 个用 `_align`，泾渭分明。

#### 4.1.5 小练习与答案

**练习 1**：如果 `matrix` 不重写 `sum`，直接用 `ndarray.sum`，`m.sum(axis=1)` 会得到什么形状？

**答案**：得到一维 `(3,)` 的 `ndarray`（而非 `(3,1)` 的 `matrix`），既丢了二维不变量也丢了列朝向。

**练习 2**：`m.sum()` 不传 `axis` 时，`keepdims=True` 会让中间结果的形状变成什么？

**答案**：所有轴都被压成 1，中间结果是 `(1, 1)` 的 `matrix`，这正是 `_collapse` 接下来要压成标量的对象。

---

### 4.2 keepdims=True + _collapse：第一组归约的统一实现

#### 4.2.1 概念说明

`keepdims=True` 的妙处在于：被压缩的轴**保留为长度 1**，所以结果天然是二维的，而且轴的位置没有动。对 `(3, 4)` 的矩阵：

- `axis=0` → `(1, 4)`：行向量，每列一个汇总值，朝向正确。
- `axis=1` → `(3, 1)`：列向量，每行一个汇总值，朝向正确。
- `axis=None` → `(1, 1)`：整体归约，但 `matrix` 希望返回**标量**而非 `(1,1)` 矩阵。

于是 `_collapse` 的职责非常简单：**只有 `axis=None` 时把 `(1,1)` 取成标量，其余情况原样返回**。它不需要管朝向，因为 `keepdims=True` 已经把朝向管好了。

> 名字辨析：`collapse` 是「坍缩」。这里坍缩的不是维度（维度已被 `keepdims` 保住），而是把那个多余的 `(1,1)` 外壳「坍缩」回一个标量。

#### 4.2.2 核心流程

以方差为例，归约的数学定义（`ddof` 为自由度修正）：

\[
\mathrm{var} = \frac{1}{N - \mathrm{ddof}} \sum_{i} (x_i - \bar{x})^2
\]

`var` 方法把这个计算交给基类，自己只管形状收尾：

```
matrix.var(axis, ddof)
  └─ N.ndarray.var(self, axis, dtype, out, ddof, keepdims=True)
        │  基类完成真正的数学计算，结果保持二维
        └─ 返回一个 matrix（subtype 经 __array_finalize__ 保留）
              │  此时形状已是 (1,N) / (N,1) / (1,1)
              └─ ._collapse(axis)
                    ├─ axis is None → self[0,0]   取标量
                    └─ 否则        → self         原样返回
```

#### 4.2.3 源码精读

先看 `_collapse` 全文，它只有 4 行有效逻辑：

[defmatrix.py#L259-L266](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L259-L266) —— `_collapse`：给「用了 `keepdims=True`」的归约方法收尾：

```python
def _collapse(self, axis):
    """A convenience function for operations that want to collapse
    to a scalar like _align, but are using keepdims=True
    """
    if axis is None:
        return self[0, 0]
    else:
        return self
```

要点：

- `self` 是 `keepdims=True` 之后的 `(1,N)/(N,1)/(1,1)` 结果。
- `axis is None` 时 `self` 是 `(1,1)`，`self[0, 0]` 经 `__getitem__` 的标量分支返回一个 numpy 标量（如 `numpy.int64`），这就是 `m.sum()` 得到标量的来源。
- 其余情况直接 `return self`——`keepdims=True` 已经把活干完，这里什么都不用做。

再看第一组里最有代表性的几个返回语句，结构完全一致：

[defmatrix.py#L449](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L449) —— `mean` 的返回：

```python
return N.ndarray.mean(self, axis, dtype, out, keepdims=True)._collapse(axis)
```

[defmatrix.py#L518-L519](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L518-L519) —— `var` 的返回（多了一个 `ddof` 参数）：

```python
return N.ndarray.var(self, axis, dtype, out, ddof,
                     keepdims=True)._collapse(axis)
```

[defmatrix.py#L650](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L650) —— `max` 的返回：

```python
return N.ndarray.max(self, axis, out, keepdims=True)._collapse(axis)
```

它们都是同一个模板：`基类方法(..., keepdims=True)._collapse(axis)`。

#### 4.2.4 代码实践

**实践目标**：验证 `sum` 的三种朝向，并确认 `_collapse` 的行为。

**操作步骤**：运行下面这段脚本（示例代码）。如本机环境受限无法运行，按「预期结果」对照阅读即可，相关断言标为「待本地验证」。

```python
# 示例代码
import warnings
warnings.simplefilter("ignore")          # 屏蔽 matrix 的 PendingDeprecationWarning
import numpy as np

m = np.matrix(np.arange(12).reshape(3, 4))   # 3 行 4 列

s0 = m.sum(axis=0)
s1 = m.sum(axis=1)
s_all = m.sum()

print("sum(axis=0):", s0.shape, type(s0).__name__)   # 预期 (1, 4) matrix
print("sum(axis=1):", s1.shape, type(s1).__name__)   # 预期 (3, 1) matrix
print("sum()     :", repr(s_all), type(s_all).__name__)  # 预期 66, int64

# 断言（待本地验证）
assert s0.shape == (1, 4)
assert s1.shape == (3, 1)
assert s_all == 66                                  # 0+1+...+11
assert not isinstance(s_all, np.matrix)             # 是标量而非 matrix
```

**需要观察的现象**：

- `axis=0` 给出行向量 `(1,4)`，`axis=1` 给出列向量 `(3,1)`——朝向与轴一致。
- `sum()` 返回的是 numpy 标量（`int64`），**不是** `(1,1)` 的 `matrix`——这正是 `_collapse` 里 `self[0,0]` 的效果。

**预期结果**：三条 `shape`/值断言全部通过。若 `s_all` 显示为 `matrix` 类型，说明 `_collapse` 的标量分支没生效（不应出现）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_collapse` 里对 `axis=0` 和 `axis=1` 用同一个 `return self`，不需要区分？

**答案**：因为调用方已经传了 `keepdims=True`，被压缩的轴保留为长度 1，结果既已二维、朝向也已正确，`_collapse` 无需再调整。

**练习 2**：把 `m.sum(axis=1)` 换成 `np.sum(m, axis=1)`，结果一样吗？

**答案**：一样。`np.sum` 是 ufunc 包装，会分派到 `matrix.sum`（因为输入是 `matrix`，`__array_priority__ = 10.0` 让结果保持 `matrix` 子类型）。源码注释里 `np.sum(M, axis=1)` 的等价性在 [test_defmatrix.py#L79-L81](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L79-L81) 有断言保护。

---

### 4.3 _align：不用 keepdims 时的方向修正

#### 4.3.1 概念说明

第二组的三个方法 `argmax / argmin / ptp` 在源码里**没有**传 `keepdims=True`。于是它们的中间结果是 1 维的：对 `(3, 4)` 的矩阵，`argmax(axis=1)` 本应是 `(3,)`。

麻烦在于 [u2-l4](u2-l4-ndarray-subclass-array-finalize.md) 讲过的那条 `__array_finalize__` 规则——**任何 1 维中间结果都会被补成 `(1, N)` 行向量**。这会把沿 `axis=1` 归约的结果错误地摆成行向量 `(1, 3)`，而它本该是列向量 `(3, 1)`。

`_align` 就是来修正这个朝向错误的：

- `axis is None`：整体归约，`self` 已是 `(1,1)`，取 `self[0,0]` 标量。
- `axis == 0`：`__array_finalize__` 补成的 `(1, N)` 行向量恰好正确，原样返回。
- `axis == 1`：`(1, N)` 是错的，需要 `self.transpose()` 翻成 `(N, 1)` 列向量。

> 名字辨析：`align` 是「对齐」。把被 `__array_finalize__` 摆歪的朝向「对齐」回正确的行列方向。

#### 4.3.2 核心流程

```
matrix.argmax(axis=1)
  └─ N.ndarray.argmax(self, axis=1, out)        # 注意：没有 keepdims
        │  基类返回 1 维 (3,) 的整数索引数组
        │  subtype 经 __array_finalize__ 保留为 matrix
        └─ __array_finalize__ 把 1 维补成 (1, 3) 行向量   ← 朝向错了！
              └─ ._align(axis=1)
                    └─ axis == 1 → self.transpose()       ← 修正成 (3, 1)
```

对比 `_collapse` 路径：那里 `keepdims=True` 直接给出 `(3,1)`，根本不会经过「先变 `(1,3)` 再 transpose」的弯路。

#### 4.3.3 源码精读

先看 `_align` 全文，它的分支比 `_collapse` 多：

[defmatrix.py#L246-L257](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L246-L257) —— `_align`：为「需要保留轴朝向」的操作收尾：

```python
def _align(self, axis):
    """A convenience function for operations that need to preserve axis
    orientation.
    """
    if axis is None:
        return self[0, 0]
    elif axis == 0:
        return self
    elif axis == 1:
        return self.transpose()
    else:
        raise ValueError("unsupported axis")
```

要点：

- `axis == 0` 返回 `self`：因为 `__array_finalize__` 给的 `(1,N)` 正是「每列一个值」的行向量，朝向正确。
- `axis == 1` 返回 `self.transpose()`：把错误的 `(1,N)` 行向量翻成 `(N,1)` 列向量。
- 多了一个 `else: raise ValueError("unsupported axis")`，因为 `matrix` 只有 2 维，`axis` 只能是 `None/0/1`。

要理解「`(1,N)` 是错的」这件事，必须回到 `__array_finalize__` 的 1-D 分支：

[defmatrix.py#L189-L192](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L189-L192) —— `__array_finalize__` 里把 1 维结果补成行向量的逻辑：

```python
if ndim == 0:
    self._set_shape((1, 1))
elif ndim == 1:
    self._set_shape((1, newshape[0]))
```

第 192 行 `(1, newshape[0])` 就是「1 维 → `(1, N)` 行向量」的来源。`argmax(axis=1)` 的 `(3,)` 中间结果就是在这里被改成 `(1, 3)` 的，然后才轮到 `_align` 去 transpose。

最后看三个走 `_align` 的返回语句：

[defmatrix.py#L689](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L689) —— `argmax`：

```python
return N.ndarray.argmax(self, axis, out)._align(axis)
```

[defmatrix.py#L763](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L763) —— `argmin`：

```python
return N.ndarray.argmin(self, axis, out)._align(axis)
```

[defmatrix.py#L796](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L796) —— `ptp`（注意它用的是函数形式 `N.ptp`，而非方法形式）：

```python
return N.ptp(self, axis, out)._align(axis)
```

三处都**没有** `keepdims=True`，结尾都是 `_align(axis)`。

#### 4.3.4 代码实践

**实践目标**：观察 `argmax` 在 `axis=1` 时的朝向修正，并对照 `axis=0`。

**操作步骤**：运行下面脚本（示例代码），重点关注 `axis=1` 的形状。

```python
# 示例代码
import warnings
warnings.simplefilter("ignore")
import numpy as np

m = np.matrix(np.arange(12).reshape(3, 4))

a0 = m.argmax(axis=0)      # 每列最大值的行号
a1 = m.argmax(axis=1)      # 每行最大值的列号
a_all = m.argmax()

print("argmax(axis=0):", a0.shape, "值=", np.asarray(a0).tolist())  # 预期 (1,4)
print("argmax(axis=1):", a1.shape, "值=", np.asarray(a1).tolist())  # 预期 (3,1)
print("argmax()      :", a_all, type(a_all).__name__)               # 预期标量 11

# 待本地验证
assert a0.shape == (1, 4)
assert a1.shape == (3, 1)
assert a_all == 11
```

**需要观察的现象**：

- `argmax(axis=0)` 是 `(1,4)`：4 列各取一个行号，排成行向量。
- `argmax(axis=1)` 是 `(3,1)`：3 行各取一个列号，排成列向量——这正是 `_align` 里 `self.transpose()` 的功劳。若没有 `_align`，它会是 `(1,3)`。

**预期结果**：断言通过，`axis=1` 结果确实是 `(3,1)` 而非 `(1,3)`。

#### 4.3.5 小练习与答案

**练习 1**：假如把 `_align` 里 `axis == 1` 的 `self.transpose()` 删掉，`m.argmax(axis=1)` 会变成什么形状？

**答案**：会变成 `(1, 3)` 行向量（`__array_finalize__` 给出的原始朝向），朝向错误——本应每行一个值排成列。

**练习 2**：`_align` 比 `_collapse` 多了一个 `raise ValueError("unsupported axis")`，`_collapse` 为什么不需要？

**答案**：`_collapse` 用 `if axis is None / else` 二分，任何非 `None` 的 `axis` 都走 `return self`，没有「非法轴」概念；`_align` 显式列举 `0/1`，落到 `else` 说明传了 `None/0/1` 以外的值（对 2 维 `matrix` 无意义），故主动报错。

---

### 4.4 为什么 argmax / argmin / ptp 走 _align（历史与维护模式）

#### 4.4.1 概念说明

这里回答本讲的核心疑问：既然 `keepdims=True` 这么好用，为什么 `argmax / argmin / ptp` 不学 `sum` 那样用它，反而要走更繁琐的 `_align`？

答案是**历史原因 + 维护模式**：

1. **`argmax / argmin` 历史上不支持 `keepdims`。** 在较老的 numpy 里，`argmax/argmin` 的签名里根本没有 `keepdims` 参数（该参数是后来才加上的）。当年写 `matrixlib` 时，为了让这两个方法也能保朝向，只能绕开 `keepdims`，自己用 `_align` 的 transpose 来修。
2. **`ptp` 用的是函数形式 `np.ptp`。** 它当时的调用约定里也没有顺手用上 `keepdims`，于是同样落到 `_align` 路径。
3. **`matrix` 已进入维护模式。** 自从 `matrix` 被标记 `PendingDeprecationWarning`（见 [u1-l1](u1-l1-project-overview.md)），官方不再为它做「重构清理」，只保证不坏。所以哪怕今天 `argmax/argmin/ptp` 都已经支持 `keepdims`，这段 `_align` 「化石代码」依然原样保留。

换句话说，`_align` 是一段**因为基类当年能力不足而写的补丁**，又因为 `matrix` 不再演进而**留在了代码里**。读它，等于读一段 numpy 的演进史。

#### 4.4.2 核心流程

判断一个方法属于哪一组，只看它的 `return`：

```
return ... keepdims=True ... ._collapse(axis)   →  第一组（现代写法）
return ...              ... ._align(axis)        →  第二组（历史补丁）
```

如果今天从零重写 `argmax`，完全可以写成
`return N.ndarray.argmax(self, axis, out, keepdims=True)._collapse(axis)`，
和 `sum` 完全对称——但维护模式下没人去动它。

#### 4.4.3 源码精读

把两组的返回语句并排放，差异一目了然：

第一组（`max`，[defmatrix.py#L650](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L650)）：

```python
return N.ndarray.max(self, axis, out, keepdims=True)._collapse(axis)
```

第二组（`argmax`，[defmatrix.py#L689](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L689)）：

```python
return N.ndarray.argmax(self, axis, out)._align(axis)
```

两边都是「求极值」，写法却不对称——这正是历史留下的痕迹。

作为旁证，可以确认**今天的 numpy 其实的确支持 `keepdims`**：

- `np.argmax` 的签名已是 `argmax(a, axis=None, out=None, *, keepdims=np._NoValue)`（见 [fromnumeric.py#L1301](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/fromnumeric.py#L1301)）。
- 底层 `_ptp` 也带 `keepdims` 参数（见 [_methods.py#L231](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/_methods.py#L231)）。

也就是说，`_align` 在今天**并非技术上必须**，而是历史遗留。

#### 4.4.4 代码实践

**实践目标**：聚焦任务里「解释 `m.ptp(axis=1)` 为何走 `_align` 分支」。

**操作步骤**：

1. 打开 [defmatrix.py#L765-L796](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L765-L796)，阅读 `ptp` 的返回语句。
2. 按下面的推理链，口头（或在注释里）复述 `m.ptp(axis=1)` 的完整形状变化过程。

**推理链（答案）**：

1. `ptp` 调 `N.ptp(self, axis=1, out)`，**没有 `keepdims`**，于是基类返回 1 维 `(3,)`（每行的峰谷值）。
2. 该 1 维结果经 `__array_finalize__`（[第 192 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L192)）被补成 `(1, 3)` 行向量——朝向错误。
3. 接着调 `_align(axis=1)`（[第 254-255 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L254-L255)），命中 `axis == 1` 分支，执行 `self.transpose()`。
4. 最终得到 `(3, 1)` 列向量，朝向正确。

**结论**：`ptp(axis=1)` 走 `_align` 而非 `_collapse`，根因是它的返回语句没有传 `keepdims=True`，中间结果是 1 维、被 `__array_finalize__` 摆成了行向量，必须靠 `_align` 的 transpose 修正回列向量。`ptp()` 与 `ptp(axis=0)` 的测试断言见 [test_defmatrix.py#L116-L121](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L116-L121)。

#### 4.4.5 小练习与答案

**练习 1**：如果今天给 `matrix.argmax` 打一个「现代化」补丁，让它和 `sum` 对称，该怎么写返回语句？

**答案**：`return N.ndarray.argmax(self, axis, out, keepdims=True)._collapse(axis)`。前提是确认目标 numpy 版本的 `argmax` 已支持 `keepdims`（本仓库 HEAD 已支持）。

**练习 2**：`_align` 里 `axis is None` 分支返回 `self[0,0]`，而 `argmax(axis=None)` 基类返回的本来就是标量，为什么还要再 `self[0,0]`？

**答案**：基类 `argmax` 在 `matrix` 上调用时，结果会经 `__array_finalize__` 被补成 `(1,1)` 的 `matrix`（0 维先变 `(1,1)`，见[第 189-190 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L189-L190)）。`self[0,0]` 把这个 `(1,1)` 外壳拆成裸标量，与 `sum()` 的行为保持一致。

## 5. 综合实践

**任务**：写一个「归约方法巡检表」生成器，把本讲全部 12 个方法 × 三种 `axis` 的形状一次性打印出来，并自动标注它走的是 `_collapse` 还是 `_align`。

**操作步骤**：运行下面脚本（示例代码），观察输出并对照本讲结论。

```python
# 示例代码
import warnings
warnings.simplefilter("ignore")
import numpy as np

m = np.matrix(np.arange(12).reshape(3, 4))

COLLAPSE = {"sum", "mean", "std", "var", "prod", "any", "all", "max", "min"}
ALIGN    = {"argmax", "argmin", "ptp"}

print(f"{'method':8} {'helper':9} {'axis=None':12} {'axis=0':10} {'axis=1':10}")
for name in sorted(COLLAPSE | ALIGN, key=lambda n: (n in ALIGN, n)):
    helper = "_collapse" if name in COLLAPSE else "_align"
    fn = getattr(m, name)
    shapes = []
    for ax in (None, 0, 1):
        r = fn(ax)
        shapes.append("scalar" if np.ndim(r) == 0 else str(np.shape(r)))
    print(f"{name:8} {helper:9} {shapes[0]:12} {shapes[1]:10} {shapes[2]:10}")
```

**需要观察的现象**：

- 第一组（`_collapse`）与第二组（`_align`）的 `axis=0 / axis=1` 形状**完全一致**：都是 `(1,4)` 与 `(3,1)`。这说明 `_align` 的 transpose 成功把朝向修成了和 `keepdims=True` 一样的结果。
- 两者的 `axis=None` 都是 `scalar`——`_collapse` 与 `_align` 在这一列殊途同归。

**预期结果（待本地验证）**：表格里 `axis=0` 一列全是 `(1, 4)`，`axis=1` 一列全是 `(3, 1)`，`axis=None` 一列全是 `scalar`。如果出现 `(1,3)`，说明某个 `_align` 方法的 transpose 没生效（不应出现）。

**进阶思考**：表格证明了一件事——对使用者而言，`_collapse` 与 `_align` 的**外部行为相同**；它们的差别完全是 `matrixlib` 内部的两种实现策略。理解这一点，就真正读懂了这两个辅助函数。

## 6. 本讲小结

- `matrix` 重写 12 个归约方法，目的是同时保住「二维」与「朝向」：`axis=0` 得行向量 `(1,N)`，`axis=1` 得列向量 `(N,1)`，`axis=None` 得标量。
- 第一组 9 个方法（`sum/mean/std/var/prod/any/all/max/min`）用 `keepdims=True` 让结果天然二维、朝向正确，再交给 `_collapse` 把 `axis=None` 的 `(1,1)` 压成标量。
- `_collapse` 极简：`axis is None` 取 `self[0,0]`，否则原样返回 `self`。
- 第二组 3 个方法（`argmax/argmin/ptp`）不传 `keepdims`，中间结果是 1 维，被 `__array_finalize__` 补成 `(1,N)` 行向量，故需 `_align` 按 `axis` 返回自身 / transpose / 取标量。
- `_align` 的 `axis==1` 分支 `self.transpose()` 是修正朝向的关键：把错误的 `(1,N)` 翻成正确的 `(N,1)`。
- `argmax/argmin/ptp` 走 `_align` 是历史遗留——当年 `argmax/argmin` 不支持 `keepdims`；如今虽已支持，但 `matrix` 处于维护模式，这段「化石代码」未被重构。

## 7. 下一步学习建议

- 继续向「形状收尾」的姊妹方法延伸：[u3-l4](u3-l4-shape-methods-flatten-ravel.md) 会讲 `squeeze / flatten / ravel / tolist`，它们同样围绕「保二维」改写，可与本讲的 `_collapse/_align` 对照阅读。
- 若想彻底吃透「朝向修正」的底层依赖，回头精读 [u2-l4](u2-l4-ndarray-subclass-array-finalize.md) 中 `__array_finalize__` 的 1-D 分支（[第 191-192 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L191-L192)），它是 `_align` 必须存在的根因。
- 想看这些归约行为如何被回归测试锁死，可阅读 [test_defmatrix.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py) 中的 `TestProperties`（`test_sum / test_prod / test_max / test_min / test_ptp / test_var`），它们是本讲所有形状断言的事实来源。

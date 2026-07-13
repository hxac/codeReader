# 形状方法 tolist / squeeze / flatten / ravel

## 1. 本讲目标

本讲精读 `np.matrix` 重写的四个「形状变换」方法——`tolist`、`squeeze`、`flatten`、`ravel`，并引入验证它们视图/副本行为的工具 `np.may_share_memory`。

学完后你应当能够：

- 说清为什么 `matrix.ravel()` 与 `matrix.flatten()` 都返回 `(1, N)` 的二维 matrix，而不是一维数组。
- 区分 `ravel`（尽量共享内存，可能返回视图）与 `flatten`（永远复制）的本质差异。
- 理解 `squeeze` 把 `(N, 1)` 列向量变成 `(1, N)` 行向量的特殊二维保形语义。
- 解释 `m.ravel()` 返回 `(1, N)` matrix、而 `np.ravel(m)` 返回 `(N,)` ndarray 的分流原因。
- 用 `np.may_share_memory` 设计断言来验证视图关系。

## 2. 前置知识

本讲建立在前面几讲已建立的认知之上，不重复推导，只承接使用：

- **二维不变量**：`matrix` 强制保持二维，0 维补成 `(1,1)`、1 维补成 `(1,N)`、超过二维抛 `ValueError`（见 u2-l1）。
- **`__array_finalize__` 是二维守护者**：每次 NumPy 派生新数组（视图、切片、`astype`、ufunc 输出、`ravel`/`flatten`/`squeeze` 的结果……）都会自动调用这个收尾钩子；它捕获掉到 1 维的中间结果，再用 `_set_shape` 把形状补回二维（见 u2-l4）。
- **`_set_shape` 是私有内部通道**：直接改写数组对象的维度与 strides、就地重塑，绕开 `.shape =` 赋值可能触发的弃用警告与 `__array_finalize__` 再回调（见 u2-l4）。其 C 实现见后文「源码精读」。
- **`__array__()` 脱壳为 ndarray**：`matrix.__array__()` 返回一个同形状、共享内存的**普通 ndarray 视图**，剥掉 matrix 子类型（见 u3-l3 的 `.A` 属性）。这是 `tolist` 能正常工作的关键。
- **视图（view）与副本（copy）**：视图共享底层内存缓冲区，改一方影响另一方；副本是独立内存。`ravel` 倾向视图、`flatten` 永远副本，是本讲反复对比的核心。

一句话直觉：**这四个方法的方法体几乎都是一行委托调用**，真正让结果「保持二维」的不是方法体里的逻辑，而是 `__array_finalize__` 在结果派生时的收尾。看懂这一点，本讲就懂了一半。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [numpy/matrixlib/defmatrix.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py) | `matrix` 类本体，本讲涉及的 `tolist`/`squeeze`/`flatten`/`ravel` 与 `__array_finalize__`/`__getitem__` 全在此文件。 |
| [numpy/matrixlib/tests/test_defmatrix.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py) | `TestShape` 测试类用断言钉死了这四个方法的形状与内存共享行为，是本讲实践的「行为契约」。 |
| numpy/_core/fromnumeric.py | 自由函数 `np.ravel` 在此定义，其中对 `matrix` 输入做了**特殊分流**，解释了 `np.ravel(m)` 与 `m.ravel()` 的差异。 |
| numpy/_core/multiarray.py | `np.may_share_memory` 的 Python 包装与文档字符串所在，是验证视图/副本的工具。 |
| numpy/_core/src/multiarray/getset.c | `_set_shape` 的 C 实现 `array_shape_set_internal`，就地重塑的底层。 |

永久链接 base（本目录内文件）：`https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/`

## 4. 核心概念与源码讲解

### 4.1 tolist：脱壳为纯 Python 嵌套列表

#### 4.1.1 概念说明

`tolist` 把数组转换成（可能是嵌套的）纯 Python `list`，元素是 Python 标量（`int`/`float` 等），不再是 numpy 类型。对二维数组，结果是「列表的列表」。

`ndarray.tolist` 本来就能做这件事，`matrix` 为什么要重写？因为基类 `tolist` 内部依赖「按 `x[0]` 逐层降维」的递归：取第一行、再对第一行取第一元素……而 `matrix.__getitem__`（见 u3-l1）为了保二维，会把 `x[0]` 返回成 `(1, N)` 的二维 matrix 而非一维数组，这会破坏基类降维递归的预期。`matrix` 的解法是**先脱壳**：调用 `self.__array__()` 拿到一个不带 matrix 子类型的普通 ndarray，再对这个普通 ndarray 调 `tolist`，递归就能正常终止。

#### 4.1.2 核心流程

```
matrix.tolist()
   │
   ├─ self.__array__()      # 脱壳：返回同形状、共享内存的普通 ndarray 视图
   │
   └─ .tolist()             # 在普通 ndarray 上递归降维 → 嵌套 list
                             # （不再经过 matrix.__getitem__，保二维逻辑不干扰）
```

注意：`tolist` 返回的是 `list`，不是 `matrix`，所以「二维不变量」在这里不适用——它根本不返回数组类型。重写的目的只是为了让降维递归在 matrix 上能跑通。

#### 4.1.3 源码精读

源码上方有一句注释点明重写原因：

[defmatrix.py:L268-L290](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L268-L290) —— 注释「`Necessary because base-class tolist expects dimension reduction by x[0]`」说明基类 `tolist` 依赖 `x[0]` 降维；方法体仅一行 `return self.__array__().tolist()`，先脱壳再转列表。

测试 `test_array_to_list` 钉死行为：

[tests/test_defmatrix.py:L336-L338](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L336-L338) —— 断言 `a.tolist() == [[1, 2], [3, 4]]`，结果是嵌套 list，元素是 Python int。

#### 4.1.4 代码实践

1. **目标**：验证 `matrix.tolist()` 返回纯 list，且元素是 Python 标量而非 `numpy.int_`。
2. **步骤**：
   ```python
   import numpy as np
   m = np.matrix([[1, 2], [3, 4]])
   t = m.tolist()
   print(type(t), t)
   print(type(t[0]), type(t[0][0]))
   ```
3. **现象**：第一行打印 `<class 'list'> [[1, 2], [3, 4]]`；第二行打印 `<class 'list'> <class 'int'>`。
4. **预期结果**：外层与每行都是 `list`，最内层元素是 Python `int`（与 `ndarray.tolist` 行为一致）。
5. 若本地 numpy 版本不同导致元素类型显示差异，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果 `matrix` 不重写 `tolist`，直接继承基类，`np.matrix([[1,2],[3,4]]).tolist()` 可能出什么问题？

**参考答案**：基类 `tolist` 用 `x[0]` 降维，而 `matrix.__getitem__` 把 `x[0]` 返回成 `(1, 2)` 的二维 matrix 而非一维数组，降维递归无法按预期逐层剥到标量，结果可能仍是 matrix 包装或递归异常。重写通过 `__array__()` 脱壳避开此路径。

**练习 2**：`np.matrix([[1,2],[3,4]]).tolist()` 与 `np.matrix([[1,2],[3,4]]).A.tolist()` 结果相同吗？为什么？

**参考答案**：相同。`tolist` 内部第一步就是 `self.__array__()`，而 `.A` 属性正是返回 `self.__array__()`（见 u3-l3），两者拿到的是同一个普通 ndarray 视图，再 `.tolist()` 自然同结果。

---

### 4.2 squeeze：单列变单行的二维保形

#### 4.2.1 概念说明

`ndarray.squeeze` 移除所有长度为 1 的轴，把 `(2,1)` 变成 `(2,)`、把 `(1,3)` 变成 `(3,)`，结果是一维。但 `matrix` 不能容忍一维结果——二维不变量必须守住。于是 `matrix.squeeze` 的行为被 `__array_finalize__` 改写：一维中间结果会被重新补成 `(1, N)`。

由此产生一个反直觉但稳定的语义：对 `(N, 1)` 的**列向量**调用 `.squeeze()`，得到的不是 `(N,)`，而是 `(1, N)` 的**行向量** matrix。也就是说，squeeze 在 matrix 上「把列向量横过来」，而不是降维。

#### 4.2.2 核心流程

```
matrix.squeeze(axis=None)
   │
   ├─ N.ndarray.squeeze(self, axis=axis)   # 委托基类：移除长度为 1 的轴
   │      └─ 对 (2,1) → 产生 (2,) 的一维 matrix 子类型视图
   │
   └─ __array_finalize__(self) 被触发        # 见 defmatrix.py L172-193
          └─ ndim == 1 分支 → self._set_shape((1, N))   # 补回二维 → (1, 2)
```

对没有长度为 1 轴的 `(2,2)` matrix，基类 `squeeze` 无轴可移、原样返回，`__array_finalize__` 看到 `ndim == 2` 直接 `return`，结果仍是 `(2,2)`。

`axis` 参数的作用因此被削弱：它只能**触发错误**（指向非 1 长度轴时抛 `ValueError`），但无法改变「最终一定二维」这一结果——即便指定了合法的 `axis`，一维中间结果仍会被 `__array_finalize__` 补回 `(1, N)`。

#### 4.2.3 源码精读

[defmatrix.py:L328-L377](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L328-L377) —— `squeeze` 方法体仅一行 `return N.ndarray.squeeze(self, axis=axis)`，二维保形完全依赖后续 `__array_finalize__`。文档字符串明确写出关键语义：「`The matrix, but as a (1, N) matrix if it had shape (N, 1).`」「`If m has a single column then that column is returned as the single row of a matrix.`」以及「`Supplying an axis keyword argument will not affect the returned matrix but it may cause an error to be raised.`」

补二维的实际执行者是 `__array_finalize__` 的 1 维分支：

[defmatrix.py:L189-L192](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L189-L192) —— `ndim == 1` 时调用 `self._set_shape((1, newshape[0]))`，把 squeeze 产生的一维视图就地重塑为 `(1, N)`。这就是「列向量变行向量」的源头。

`_set_shape` 的 C 实现确认它是「就地改维度」的内部通道：

[getset.c:L57-L74](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/src/multiarray/getset.c#L57-L74) —— `array_shape_set_internal` 先 `PyArray_Reshape`，校验新形状与原数据指针一致（`PyArray_DATA(ret) == PyArray_DATA(self)`，即不复制），随后直接改写对象自身的 `dimensions`/`strides` 字段，不经过 `.shape =` 赋值路径，因而不会再次回调 `__array_finalize__`、也不触发 `.shape=` 的弃用警告。

#### 4.2.4 代码实践

1. **目标**：验证 `(N,1)` 列向量 squeeze 后变成 `(1,N)` 行向量，且结果仍是 matrix。
2. **步骤**：
   ```python
   import numpy as np
   c = np.matrix([[1], [2]])          # shape (2, 1)
   r = c.squeeze()
   print(r.shape, type(r).__name__, r)
   print(c.T.squeeze().shape)         # 对 (1,2) 行向量 squeeze
   m = np.matrix([[1, 2], [3, 4]])
   print(m.squeeze().shape)           # 无长度为 1 的轴
   ```
3. **现象**：`r` 打印 `(1, 2) matrix [[1 2]]`；`c.T.squeeze()` 仍是 `(1, 2)`；`m.squeeze()` 是 `(2, 2)`。
4. **预期结果**：列向量 squeeze 得 `(1, 2)` 行向量 matrix；行向量 squeeze 仍 `(1, 2)`；普通方阵 squeeze 不变。与 `squeeze` 文档字符串示例一致。
5. 想观察 `axis` 只会报错：执行 `c.squeeze(axis=0)`（axis 0 长度为 2，非 1），预期抛 `ValueError`。

#### 4.2.5 小练习与答案

**练习 1**：`np.matrix([[1,2,3]]).squeeze()` 的形状是什么？为什么不是 `(3,)`？

**参考答案**：形状是 `(1, 3)`。基类 squeeze 会移除 axis 0（长度 1）产生 `(3,)` 一维中间结果，但 `__array_finalize__` 的 `ndim == 1` 分支用 `_set_shape((1, 3))` 把它补回二维行向量，故结果仍是 `(1, 3)`。

**练习 2**：为什么 `squeeze` 的 `axis` 参数「不影响返回的 matrix，但可能引发错误」？

**参考答案**：因为即便 `axis` 合法地移除了某个长度为 1 的轴，产生的一维结果仍会被 `__array_finalize__` 补成 `(1, N)`，最终形状由这条收尾逻辑决定，与 `axis` 无关；只有当 `axis` 指向长度大于 1 的轴时，基类 `ndarray.squeeze` 才会抛 `ValueError`，这是 `axis` 唯一可观测的效果。

---

### 4.3 flatten：总是复制的 (1, N) 展平

#### 4.3.1 概念说明

`flatten` 把整个矩阵的所有元素摊平成一行。在 `ndarray` 上，`flatten` 返回一维数组且**永远复制**（与 `ravel` 相对）。在 `matrix` 上，同样永远复制，但结果被 `__array_finalize__` 补成 `(1, N)` 的二维行向量 matrix。

所以 `matrix([[1,2],[3,4]]).flatten()` 得到 `matrix([[1, 2, 3, 4]])`，形状 `(1, 4)`，且与原矩阵不共享内存。`order` 参数（`'C'`/`'F'`/`'A'`/`'K'`）控制元素读取顺序，与 `ndarray.flatten` 完全一致。

#### 4.3.2 核心流程

```
matrix.flatten(order='C')
   │
   ├─ N.ndarray.flatten(self, order=order)   # 委托基类：永远复制，产生 (N,) 一维副本
   │      └─ 副本是 matrix 子类型（继承自 self 的类型）
   │
   └─ __array_finalize__(self) 被触发
          └─ ndim == 1 分支 → self._set_shape((1, N))   # 补回 (1, N)
```

因为 `ndarray.flatten` 永远产生副本，`__array_finalize__` 拿到的是一块新内存上的一维数组，补形后与原矩阵**不可能共享内存**。

#### 4.3.3 源码精读

[defmatrix.py:L380-L415](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L380-L415) —— `flatten` 方法体仅一行 `return N.ndarray.flatten(self, order=order)`。文档字符串明确：「`A copy of the matrix, flattened to a (1, N) matrix`」，并标注 `flatten` 与 `ravel` 的区别在于「returns a similar output matrix but always a copy」（见 ravel 的 See Also）。

测试 `test_member_flatten` 与 `test_matrix_memory_sharing` 共同钉死形状与「不共享内存」两条性质：

[tests/test_defmatrix.py:L416-L418](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L416-L418) —— 断言 `self.m.flatten().shape == (1, 2)`（`m = matrix([[1],[2]])`，共 2 个元素）。

[tests/test_defmatrix.py:L443-L445](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L443-L445) —— 断言 `not np.may_share_memory(self.m, self.m.flatten())`，即 flatten 结果与原矩阵不共享内存。

#### 4.3.4 代码实践

1. **目标**：验证 `flatten` 返回 `(1, N)` matrix 副本，修改副本不影响原矩阵。
2. **步骤**：
   ```python
   import numpy as np
   m = np.matrix([[1, 2], [3, 4]])
   f = m.flatten()
   print(f.shape, type(f).__name__, f)
   f[0, 0] = 999
   print(m[0, 0])          # 原矩阵应不变
   print(np.may_share_memory(m, f))
   ```
3. **现象**：`f` 打印 `(1, 4) matrix [[1 2 3 4]]`；改 `f[0,0]` 后 `m[0,0]` 仍为 `1`；`may_share_memory` 为 `False`。
4. **预期结果**：`flatten` 返回 `(1, 4)` 副本，与原矩阵独立。
5. 再试 `m.flatten('F')`，预期得到 `matrix([[1, 3, 2, 4]])`（按列优先读取）。

#### 4.3.5 小练习与答案

**练习 1**：既然 `flatten` 和 `ravel` 在 matrix 上都返回 `(1, N)` matrix，为什么还要同时保留两个方法？

**参考答案**：语义不同——`flatten` 永远复制、保证返回独立内存，适合需要安全修改结果而不影响原数据的场景；`ravel` 尽量返回视图、省内存，但修改结果可能波及原矩阵。形状相同不代表可交换使用，内存语义是关键差别。

**练习 2**：`m.flatten()` 之后对结果调 `.tolist()`，会得到什么结构？

**参考答案**：得到一个单层 list，如 `m = np.matrix([[1,2],[3,4]])` 时 `m.flatten().tolist()` 为 `[[1, 2, 3, 4]]`（外层 list 包一个内层 list，因为结果仍是 `(1, 4)` 二维）。注意它**不是** `[1, 2, 3, 4]`——若要一维 list，应先 `.A1` 或 `np.ravel(m).tolist()`。

---

### 4.4 ravel：尽量共享内存的 (1, N) 展平 与 np.ravel 的差异

#### 4.4.1 概念说明

`ravel` 同样把矩阵摊平成一行，但与 `flatten` 不同，它**尽量返回视图**（共享内存），只在必要时才复制。在 `matrix` 上，结果同样被 `__array_finalize__` 补成 `(1, N)` 的二维行向量 matrix。

本模块最重要的对比是**方法与自由函数的分流**：

- `m.ravel()`（绑定方法）：保留 matrix 子类型，一维中间结果经 `__array_finalize__` 补成 `(1, N)` matrix，**可能共享内存**。
- `np.ravel(m)`（自由函数）：在源码里对 `matrix` 输入做了**显式特殊分流**，先 `asarray(a)` 剥掉 matrix 子类型再 `.ravel()`，返回 `(N,)` 的普通一维 ndarray，**不再保持二维**。

这解释了测试中 `np.ravel(self.m).shape == (2,)` 而 `self.m.ravel().shape == (1, 2)` 的差异——它不是 `__array_finalize__` 的副作用，而是自由函数源码里一行 `isinstance(a, np.matrix)` 判断主动选择的结果。

#### 4.4.2 核心流程

```
# 绑定方法路径
matrix.ravel(order='C')
   ├─ N.ndarray.ravel(self, order=order)    # 尽量视图，产生 (N,) 一维 matrix 子类型
   └─ __array_finalize__ → _set_shape((1, N))   # 补回 (1, N)，可能仍共享内存

# 自由函数路径（fromnumeric.py）
np.ravel(m)
   ├─ isinstance(m, np.matrix) 为真
   ├─ asarray(m)                             # 剥掉子类型 → 普通 ndarray
   └─ .ravel(order=order)                    # 普通 ndarray 的 ravel → (N,) 一维 ndarray
```

为什么自由函数要主动剥掉 matrix？因为一维结果本就不是合法的 matrix（matrix 必须二维），保留子类型只会让 `__array_finalize__` 再强行补成 `(1, N)`，这会与「`np.ravel` 应返回一维」的通用契约冲突。对 matrix 这类强制二维的子类型，自由函数选择直接降级为 ndarray。

#### 4.4.3 源码精读

绑定方法：

[defmatrix.py:L902-L938](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L902-L938) —— `ravel` 方法体仅一行 `return N.ndarray.ravel(self, order=order)`。文档字符串明确：「`Return the matrix flattened to shape (1, N)`」「`A copy is made only if necessary.`」

自由函数的特殊分流（本讲最关键的一段源码）：

[fromnumeric.py:L2062-L2065](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/fromnumeric.py#L2062-L2065) ——
```python
if isinstance(a, np.matrix):
    return asarray(a).ravel(order=order)
else:
    return asanyarray(a).ravel(order=order)
```
对 `matrix` 走 `asarray`（剥子类型），对其它类型走 `asanyarray`（保留子类型）。这一条 `isinstance` 判断就是 `np.ravel(m)` 返回一维 ndarray 的根因。

测试同时钉死两条路径的形状与内存共享：

[tests/test_defmatrix.py:L408-L414](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L408-L414) —— `test_numpy_ravel` 断言 `np.ravel(self.m).shape == (2,)`；`test_member_ravel` 断言 `self.m.ravel().shape == (1, 2)`。

[tests/test_defmatrix.py:L443-L445](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L443-L445) —— `test_matrix_memory_sharing` 断言 `np.may_share_memory(self.m, self.m.ravel())` 为真，即 ravel 结果可能与原矩阵共享内存。

#### 4.4.4 代码实践

1. **目标**：对比 `m.ravel()` 与 `np.ravel(m)` 的形状、类型与内存共享。
2. **步骤**：
   ```python
   import numpy as np
   m = np.matrix([[1, 2], [3, 4]])
   rm = m.ravel()
   rn = np.ravel(m)
   print("m.ravel():", rm.shape, type(rm).__name__)     # (1,4) matrix
   print("np.ravel(m):", rn.shape, type(rn).__name__)   # (4,)  ndarray
   print("share(m, m.ravel()):", np.may_share_memory(m, rm))
   rm[0, 0] = 999
   print("after edit rm, m[0,0] =", m[0, 0])             # 视图则被波及
   ```
3. **现象**：`m.ravel()` 为 `(1, 4)` matrix；`np.ravel(m)` 为 `(4,)` ndarray；`may_share_memory` 为 `True`；改 `rm[0,0]` 后 `m[0,0]` 变为 `999`（证明共享内存）。
4. **预期结果**：与上述一致；若内存布局导致 `ravel` 必须复制（例如某些非 C/F 连续矩阵），则 `may_share_memory` 可能为 `False`、改 `rm` 不波及 `m`，此时标注「待本地验证布局依赖」。
5. 进阶：执行 `m2 = np.asfortranarray(m); print(np.may_share_memory(m2, m2.ravel()))`，观察 Fortran 连续矩阵的 ravel 是否仍共享内存。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `np.ravel(m)` 返回 `(N,)` 而 `m.ravel()` 返回 `(1, N)`？请用源码定位回答。

**参考答案**：`np.ravel` 在 [fromnumeric.py:L2062-L2065](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/fromnumeric.py#L2062-L2065) 对 `isinstance(a, np.matrix)` 为真的输入走 `asarray(a).ravel()`，`asarray` 剥掉 matrix 子类型，`.ravel()` 返回普通一维 ndarray `(N,)`，不触发 matrix 的 `__array_finalize__` 补形；而 `m.ravel()` 调 `N.ndarray.ravel(self)` 保留 matrix 子类型，一维结果被 `__array_finalize__` 补成 `(1, N)`。

**练习 2**：`m.ravel()[0,0] = 999` 是否一定改变 `m[0,0]`？

**参考答案**：不一定。`ravel`「尽量」返回视图，但当矩阵内存布局无法用一维视图表达时（如某些非连续布局）会复制，此时修改不影响原矩阵。测试 `test_matrix_memory_sharing` 用 `assert_(np.may_share_memory(...))` 表达「可能共享」而非「必然共享」。要保证独立，应改用 `flatten`。

---

### 4.5 may_share_memory：视图/副本判定工具

#### 4.5.1 概念说明

`np.may_share_memory(a, b)` 判断两个数组**是否可能共享内存**。它是一个**保守启发式**：返回 `True` 只表示「可能共享」，并不保证真的有共同元素；返回 `False` 则基本可以确定不共享。默认只做**内存边界检查**（`max_work=0`），速度快，但可能有假阳性。

它是本讲验证 `ravel`/`flatten` 视图与副本语义的标准工具——测试套件正是用它来断言 `ravel` 可能共享、`flatten` 不共享。

#### 4.5.2 核心流程

```
np.may_share_memory(a, b, max_work=0)
   │
   ├─ 默认 max_work=0：仅比较 a、b 的内存地址区间是否相交（边界检查）
   │      └─ 相交 → True（可能共享，但未必有共同元素）
   │      └─ 不相交 → False（基本可断定不共享）
   │
   └─ 真正的判定逻辑在 C 层 _multiarray_umath.may_share_memory
      （Python 包装只是分发器，body 为 return (a, b) 的占位）
```

与之对比，`np.shares_memory` 默认做更深入的逐元素重叠求解（`max_work` 更大），更精确但更慢。日常区分视图/副本，`may_share_memory` 足够且更快。

#### 4.5.3 源码精读

[ multiarray.py:L1401-L1439 ](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/multiarray.py#L1401-L1439) —— `may_share_memory` 的 Python 包装与文档字符串。文档明确：「`A return of True does not necessarily mean that the two arrays share any element. It just means that they might.`」「`Only the memory bounds of a and b are checked by default.`」函数体 `return (a, b)` 只是分发器占位，真正实现在 C 层。

测试套件用它表达「可能共享」与「确定不共享」两种断言：

[tests/test_defmatrix.py:L439-L445](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L439-L445) ——
- `test_array_memory_sharing`：`assert_(np.may_share_memory(self.a, self.a.ravel()))` 与 `assert_(not np.may_share_memory(self.a, self.a.flatten()))`（ndarray 侧）。
- `test_matrix_memory_sharing`：`assert_(np.may_share_memory(self.m, self.m.ravel()))` 与 `assert_(not np.may_share_memory(self.m, self.m.flatten()))`（matrix 侧）。

注意 `assert_(np.may_share_memory(...))` 用的是「可能为真」语义——这与 `may_share_memory` 的保守性一致：只要边界相交就断言通过，恰好契合 `ravel`「可能返回视图」的不确定语义。

#### 4.5.4 代码实践

1. **目标**：用 `may_share_memory` 验证 matrix 上 `ravel`（可能共享）与 `flatten`（不共享）的内存关系。
2. **步骤**：
   ```python
   import numpy as np
   m = np.matrix([[1], [2]])
   print("ravel  share:", np.may_share_memory(m, m.ravel()))    # 预期 True
   print("flatten share:", np.may_share_memory(m, m.flatten())) # 预期 False
   # 对照：转置是视图，应共享
   print("T share:", np.may_share_memory(m, m.T))
   ```
3. **现象**：`ravel` 为 `True`，`flatten` 为 `False`，`T` 为 `True`。
4. **预期结果**：与 test_defmatrix.py 的 `test_matrix_memory_sharing` 断言完全一致。
5. 若本地构造的矩阵布局使 `ravel` 强制复制，`ravel share` 可能变为 `False`，此时标注「待本地验证布局依赖」。

#### 4.5.5 小练习与答案

**练习 1**：`np.may_share_memory(a, b)` 返回 `True`，能否断定修改 `a` 会影响 `b`？

**参考答案**：不能。`True` 只表示两者的内存边界区间相交，可能存在假阳性（两个数组恰好分配在同一地址区间但无共同元素）。要更确定应改用 `np.shares_memory`（默认更深入的逐元素重叠求解）。只有返回 `False` 才基本可断定不共享。

**练习 2**：测试为何用 `assert_(np.may_share_memory(m, m.ravel()))` 而非 `assert_equal(np.may_share_memory(...), True)`？

**参考答案**：`ravel` 的视图行为是「尽量」而非「必然」——在某些内存布局下会复制。`may_share_memory` 的保守语义（边界相交即为真）与 `ravel` 的「可能共享」语义天然契合：只要边界相交就通过，恰好覆盖了视图存在的情况，不会因偶发复制而误报失败。这与 `flatten` 用 `assert_(not ...)` 表达「确定不共享」形成对照。

---

## 5. 综合实践

**任务**：构造一个 `(2, 1)` 列向量 matrix，对它依次调用 `tolist`、`squeeze`、`flatten`、`ravel`，并对照 `np.ravel`，整理一张「形状 / 类型 / 是否共享内存」对比表，最后用一句话解释每行的成因。

```python
import numpy as np
import warnings
warnings.simplefilter("ignore", PendingDeprecationWarning)

c = np.matrix([[1], [2]])          # (2, 1)

rows = []
rows.append(("c",            c.shape,             type(c).__name__,            "-"))
rows.append(("c.tolist()",   None,                type(c.tolist()).__name__,  "-"))   # list
rows.append(("c.squeeze()",  c.squeeze().shape,   type(c.squeeze()).__name__,
             np.may_share_memory(c, c.squeeze())))
rows.append(("c.flatten()",  c.flatten().shape,   type(c.flatten()).__name__,
             np.may_share_memory(c, c.flatten())))
rows.append(("c.ravel()",    c.ravel().shape,     type(c.ravel()).__name__,
             np.may_share_memory(c, c.ravel())))
rows.append(("np.ravel(c)",  np.ravel(c).shape,   type(np.ravel(c)).__name__,
             np.may_share_memory(c, np.ravel(c))))

for name, shape, typ, share in rows:
    print(f"{name:14s} shape={shape} type={typ:10s} share={share}")
```

**需要观察与解释的现象**：

1. `c.tolist()` 返回 `list`（`[[1], [2]]`），与内存无关——它先脱壳再降维，不返回数组。
2. `c.squeeze()` 形状 `(1, 2)`、类型 `matrix`——列向量被 `__array_finalize__` 补成行向量。
3. `c.flatten()` 形状 `(1, 2)`、`share=False`——永远复制。
4. `c.ravel()` 形状 `(1, 2)`、`share=True`（可能）——尽量视图，且保留 matrix 子类型被补成二维。
5. `np.ravel(c)` 形状 `(2,)`、类型 `ndarray`——自由函数对 matrix 走 `asarray` 剥子类型，返回一维。

**预期结果**：上表应与 test_defmatrix.py::TestShape 的断言一致（`ravel` 形状 `(1,2)`、`np.ravel` 形状 `(2,)`、`flatten` 不共享、`ravel` 可能共享）。若某项与预期不符，先检查 numpy 版本与矩阵内存布局，标注「待本地验证」。

**进阶**：阅读 [fromnumeric.py:L2062-L2065](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/fromnumeric.py#L2062-L2065)，回答：如果把你自己的 `ndarray` 子类（非 matrix）传入 `np.ravel`，它会保留子类型还是降级？为什么 matrix 被特殊对待？（提示：`asanyarray` vs `asarray`，以及「强制二维子类无法表达一维结果」。）

## 6. 本讲小结

- `tolist`、`squeeze`、`flatten`、`ravel` 的方法体几乎都是一行委托调用（`N.ndarray.xxx(self, ...)`），**二维保形并非方法体所为**，而是 `__array_finalize__` 在结果派生时把一维中间结果用 `_set_shape((1, N))` 补回二维。
- `tolist` 是例外：它通过 `self.__array__()` 先脱壳成普通 ndarray 再转 list，绕开基类 `tolist` 对 `x[0]` 降维的依赖（matrix 的 `__getitem__` 会把 `x[0]` 返回成二维行向量）。
- `squeeze` 把 `(N, 1)` 列向量变成 `(1, N)` 行向量；`axis` 参数只能触发错误，无法改变「最终二维」这一结果。
- `flatten` 永远复制、`ravel` 尽量视图——形状都是 `(1, N)`，但内存语义相反，`np.may_share_memory` 是区分两者的标准工具。
- 关键分流：`m.ravel()` 返回 `(1, N)` matrix，而 `np.ravel(m)` 因 [fromnumeric.py:L2062-L2065](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/fromnumeric.py#L2062-L2065) 的 `isinstance(a, np.matrix)` 特判走 `asarray`，返回 `(N,)` 普通 ndarray。
- `np.may_share_memory` 是保守启发式：`True` 表示「可能共享」（边界相交），`False` 基本可断定不共享；测试用它表达 `ravel` 的「可能视图」与 `flatten` 的「确定副本」。

## 7. 下一步学习建议

- **横向打通归约方法**：本讲的 `(1, N)` 保形机制与 u3-l2 的 `_collapse`/`_align` 是同一套「二维不变量」在不同方法族上的体现。建议回看 `sum/mean/argmax/ptp` 如何用 `keepdims=True` 或 `_align` 收尾，对比 shape 方法「完全不动手、全靠 `__array_finalize__`」的策略差异。
- **纵向深入 `__array_finalize__` 全景**：本讲反复依赖它的 1 维分支。建议结合 u2-l4、u3-l1，把 `__array_finalize__` 的五条分支（含 `_getitem` 短路）与 `_set_shape` 的 C 实现串成一张完整的状态图。
- **扩展到生态交互**：`np.ravel` 对 matrix 的特判揭示了「自由函数会主动处理强制二维子类」的设计模式。下一讲 u3-l5（subok、nditer、nanfunctions、MaskedArray）会从更多角度展示 matrix 与 numpy 生态的交互边界，建议接着阅读 `test_interaction.py`。
- **类型存根视角**：若关心静态类型，可对照 u3-l6 的 `defmatrix.pyi`，看 `ravel`/`flatten`/`squeeze` 的返回类型如何被标注为 `matrix[_2D, ...]`，把本讲的运行时行为与编译期类型对应起来。

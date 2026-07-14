# 与 numpy 生态交互：subok、nditer、nanfunctions、MaskedArray

## 1. 本讲目标

前面几讲我们一直把 `matrix` 当成一个「自洽的小世界」来看：构造、索引、归约、属性、形状方法都关在 `defmatrix.py` 一个文件里。但真实使用中，`matrix` 会被丢进 numpy 的各种通用机制里——类型转换、`*_like` 系列函数、`nditer` 迭代器、`nan*` 缺失值函数，甚至和 `MaskedArray` 做多重继承。这些机制本不是为 `matrix` 写的，`matrix` 之所以还能「保持二维、保持类型」，靠的是 numpy 在若干接口里预留的子类友好开关（`subok`）和优先级协议（`__array_priority__`）。

学完本讲，你应当能够：

- 说清 `subok=True/False` 这一个开关如何决定 `astype`、`zeros_like` 等函数的返回类型。
- 解释 `nditer` 在「自动分配输出」时为何会按 `__array_priority__` 选出 `matrix` 子类型，以及为什么三维输出会让 `matrix` 崩、`no_subtype` 又能救回来。
- 说出 `np.nansum`/`np.nanmin` 等 `nan*` 函数保持 `matrix` 类型与朝向的真正路径（不是「直接走 `__array_finalize__`」，而是「慢路径 + 委托给 matrix 自己重写过的归约方法」）。
- 看懂 `MMatrix(MaskedArray, np.matrix)` 这种多重继承为何必须在 `__array_finalize__` 里**手动调用两个基类的 finalize**。

## 2. 前置知识

本讲是专家层，默认你已经掌握下面几条来自前序讲义的结论（不会再重复推导）：

- **matrix 恒二维不变量**：`matrix` 是 `ndarray` 子类，`__array_finalize__` 是这个不变量的守护者——每次派生新数组（视图、切片、`astype`、ufunc 输出等）都会被自动调用，把掉到 0/1 维的结果补回二维。见 [defmatrix.py:172-193](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L172-L193)。
- **`__array_priority__`**：基类 `ndarray` 默认 `0.0`，`matrix` 设为 `10.0`（[defmatrix.py:117](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L117)）。当两个不同子类参与同一个 ufunc 时，优先级高的那个「赢」，结果采用它的子类型。
- **重写过的归约方法**：`matrix.sum/min/max/...` 全部加了 `keepdims=True` 并配合 `_collapse`，因此 `axis=0` 出 `(1,N)`、`axis=1` 出 `(N,1)`、`axis=None` 出标量。这是 u3-l2 的核心结论，本讲会反复用到。
- **基本索引 vs 花式索引**、**视图 vs 副本** 等术语来自 u3-l1 / u3-l4，这里直接使用。

本讲用到、但来自 numpy 其它子系统的两个概念，先在这里用一句话交代：

- **`subok`**（subclasses OK）：很多 numpy 函数都有的布尔参数。`True` 表示「结果保留输入的子类型」，`False` 表示「无论输入是什么，结果一律是基类 `ndarray`」。它就是子类能不能「传染」给输出的总开关。
- **`__array_finalize__` 的多重继承问题**：Python 的方法解析顺序（MRO）只会在派生时自动调用**一个** `__array_finalize__`，多重继承下第二个基类的 finalize 会被跳过，必须手动补调。

## 3. 本讲源码地图

本讲涉及的关键文件，按出现顺序：

| 文件 | 作用 |
| --- | --- |
| [defmatrix.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py) | `matrix` 类本体。本讲主要引用 `__array_priority__`、`__array_finalize__` 与重写过的归约方法。 |
| [tests/test_interaction.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_interaction.py) | 「matrix 与 numpy 其它部分交互」的测试集，是本讲四个最小模块（`subok`/`nditer`/`nanfunctions`/`*_like`）的行为契约来源。 |
| [tests/test_masked_matrix.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_masked_matrix.py) | `MaskedArray` × `matrix` 的测试集，定义了多重继承样本类 `MMatrix`，是第 5 个最小模块的来源。 |
| [lib/_nanfunctions_impl.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py) | `nan*` 函数实现（位于 `matrixlib` 之外）。本讲只引用它「对子类安全的慢路径」这一段，用来解释 `nan*` 函数为何保形。 |

> 说明：本讲的「源码」大多以**测试文件**为契约入口（matrixlib 自己并不实现 `subok`/`nditer`/`nanfunctions`/`MaskedArray`，它只是「被这些机制使用」）。因此精读部分会频繁引用测试断言，并辅以 `defmatrix.py` 里的实现点来解释「为什么是这个行为」。

## 4. 核心概念与源码讲解

### 4.1 subok：控制子类型是否「传染」给输出

#### 4.1.1 概念说明

`astype`、`zeros_like`、`ones_like`、`empty_like` 这些函数都有一个布尔参数 `subok`（subclasses OK）。它的语义非常统一：

- `subok=True`：结果**继承输入的子类型**。输入是 `matrix`，输出也是 `matrix`。
- `subok=False`：结果**永远是基类 `ndarray`**，无论输入是不是子类。

可以把 `subok` 理解成「子类型传染」的总开关。`matrix` 之所以能在 numpy 的通用函数里存活（而不是被悄悄剥成普通数组），就是因为这些函数默认 `subok=True`。一旦你显式关掉，`matrix` 就被「降级」回 `ndarray`。

#### 4.1.2 核心流程

以 `astype` 为例，决策分三种情况：

```
输入 a 是 matrix(dtype=f4)
│
├─ a.astype('f4', subok=True,  copy=False)  # 同 dtype + 不复制
│      → 直接返回 a 自己（a is b）
│
├─ a.astype('i4', copy=False)               # subok 默认 True，发生类型转换
│      → 新对象，但仍是 matrix
│
└─ a.astype('f4', subok=False, copy=False)  # 强制降级
       → 新对象，类型是 ndarray（不再是 matrix）
```

注意第二种情况：哪怕发生了 dtype 转换、哪怕 `copy=False`，只要 `subok=True`（`astype` 的默认值），结果依然是 `matrix`。`subok` 管的是「类型」，`copy` 管的是「内存是否复制」，二者是正交的。

#### 4.1.3 源码精读

这段行为契约写死在测试里，逐行对应上面的流程图：

[test_interaction.py:132-148](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_interaction.py#L132-L148) —— `test_array_astype`，三段断言分别验证「同 dtype 直通」「转换仍保子类型」「`subok=False` 强制降级」：

```python
a = np.matrix([[0, 1, 2], [3, 4, 5]], dtype='f4')
# subok=True 且 copy=False：同 dtype，直接返回原对象
b = a.astype('f4', subok=True, copy=False)
assert_(a is b)

# subok 默认 True，发生 i4 转换，结果仍是 matrix
b = a.astype('i4', copy=False)
assert_equal(type(b), np.matrix)

# subok=False：永不返回 matrix，且是另一个对象
b = a.astype('f4', subok=False, copy=False)
assert_(not (a is b))
assert_(type(b) is not np.matrix)
```

为什么 `subok=False` 能把 `matrix` 剥掉？因为 `astype` 在底层构造结果数组时，`subok=False` 会强制把目标类型设为 `ndarray` 而非 `type(a)`，于是新建的数组走的是基类路径，`matrix.__array_finalize__` 虽仍会被触发，但对象本身就是 `ndarray`，自然不再是 `matrix`。换句话说，`subok` 决定的是「**拿哪个类去 new**」，这一步在 `__array_finalize__` 之前。

#### 4.1.4 代码实践

1. **实践目标**：亲手确认 `subok` 是 `matrix` 子类型的「生死开关」。
2. **操作步骤**：
   ```python
   import numpy as np
   a = np.matrix([[0, 1, 2], [3, 4, 5]], dtype='f4')
   print(type(a.astype('i4', copy=False)))            # 期望 matrix
   print(type(a.astype('f4', subok=False, copy=False)))  # 期望 ndarray
   print(a.astype('f4', subok=True, copy=False) is a)    # 期望 True
   ```
3. **需要观察的现象**：第一行打印 `<class 'numpy.matrix'>`，第二行打印 `<class 'numpy.ndarray'>`，第三行打印 `True`。
4. **预期结果**：与上述一致。若第二行仍是 `matrix`，说明 `subok` 没有传到 `astype`（检查拼写与版本）。
5. **待本地验证**：上述为按测试契约推断，请在本机 numpy 上实际运行确认。

#### 4.1.5 小练习与答案

**练习 1**：`a.astype('i4')`（不写 `subok`）返回什么类型？
**答案**：`matrix`。`astype` 的 `subok` 默认是 `True`，类型转换不改变子类型。

**练习 2**：若希望把一个 `matrix`「干净地」变成普通 `ndarray`，除了 `np.asarray(m)` 之外，用本节的 API 怎么写？
**答案**：`m.astype(m.dtype, subok=False)`，或 `m.astype('f8', subok=False)`。关键是显式 `subok=False`。

---

### 4.2 zeros_like / ones_like / empty_like 的 subok 行为

#### 4.2.1 概念说明

`np.zeros_like`、`np.ones_like`、`np.empty_like` 这一组 `*_like` 函数的作用是「照着输入的形状和 dtype 造一个新数组」。它们同样有 `subok` 参数，且**默认 `subok=True`**。这意味着：给一个 `matrix`，默认会还你一个 `matrix`（形状被 `matrix` 的二维约束接管，所以仍是二维）。

这与 4.1 的 `astype` 完全同构——`subok` 是贯穿 numpy 的统一开关，不是某个函数的特殊行为。

#### 4.2.2 核心流程

```
a = matrix(2x2)
│
├─ zeros_like(a)                # subok 默认 True
│      → matrix，全 0
│
└─ zeros_like(a, subok=False)   # 强制降级
       → ndarray，全 0
```

#### 4.2.3 源码精读

契约来自 `like_function` 测试，它用一个循环同时覆盖了三种 `*_like`：

[test_interaction.py:121-129](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_interaction.py#L121-L129)：

```python
a = np.matrix([[1, 2], [3, 4]])
for like_function in np.zeros_like, np.ones_like, np.empty_like:
    b = like_function(a)              # subok 默认 True → matrix
    assert_(type(b) is np.matrix)

    c = like_function(a, subok=False) # 关掉 → ndarray
    assert_(type(c) is not np.matrix)
```

> 小八卦：这个测试函数名叫 `like_function` 而不是 `test_like_function`，按 pytest 的命名约定它**不会被 pytest 自动收集**。这是个历史遗留的小瑕疵，但它清楚地表达了 `*_like` 家族对 `subok` 的统一行为，所以本讲仍以它为契约依据。

#### 4.2.4 代码实践

1. **实践目标**：验证 `*_like` 默认保子类型，`subok=False` 降级。
2. **操作步骤**：
   ```python
   import numpy as np
   a = np.matrix([[1, 2], [3, 4]])
   for fn in (np.zeros_like, np.ones_like, np.empty_like):
       print(fn.__name__, type(fn(a)), type(fn(a, subok=False)))
   ```
3. **需要观察的现象**：每行第一个类型是 `numpy.matrix`，第二个是 `numpy.ndarray`。
4. **预期结果**：三行均如此。
5. **待本地验证**：请在本机运行确认（尤其 `empty_like` 的未初始化值无需关心，只看类型）。

#### 4.2.5 小练习与答案

**练习**：`np.zeros_like(a)` 返回的 `matrix` 形状一定是二维吗？如果 `a` 是 `matrix(np.arange(6).reshape(2,3))` 呢？
**答案**：是二维。`a` 本身是 `(2,3)` 的 matrix，`zeros_like` 照搬形状，结果也是 `(2,3)` matrix。`*_like` 不会凭空改变维数，而 `matrix` 又不允许非二维，所以结果恒二维。

---

### 4.3 nditer 按优先级分配输出子类型与 no_subtype 降级

#### 4.3.1 概念说明

`np.nditer` 是 numpy 的高效多维迭代器。它有一个强大但也容易踩坑的特性：当你把某个操作数写成 `None` 并打上 `'allocate'` 标志时，`nditer` 会**自动为这个输出分配一个新数组**。问题是——这个新数组该是什么子类型？

答案是：**在所有输入操作数里，谁的 `__array_priority__` 最高，输出就采用谁的子类型。** 因为 `matrix` 的优先级是 `10.0`，远高于普通 `ndarray` 的 `0.0`，所以只要有一个输入是 `matrix`，自动分配的输出就会被「感染」成 `matrix`。

但这里埋着一个雷：`matrix` 死守二维，而 `nditer` 的输出形状由**广播**决定。一旦广播出来的形状是三维，`matrix` 就装不下——`nditer` 会在分配阶段直接抛 `RuntimeError`。救场的办法是给输出操作数加 `'no_subtype'` 标志：它告诉 `nditer`「别管子类型了，就用基类 `ndarray`」，于是三维形状被普通数组接住。

#### 4.3.2 核心流程

```
输入 a=matrix(2,2)，b=ndarray(2,2).T，输出=None[allocate]
│  广播形状=(2,2)，matrix 优先级胜出
└─ 输出 = matrix(2,2)   ✓

输入 a=matrix(2,2)，b=ndarray(1,2,2)，输出=None[allocate]
│  广播形状=(1,2,2)，matrix 胜出，但 matrix 装不下三维
└─ RuntimeError ✗

同上，但输出加 'no_subtype'
│  跳过子类型选择，用基类 ndarray
└─ 输出 = ndarray(1,2,2)   ✓
```

优先级的比较可以写成一个简单的取最大：

\[
\text{输出子类型} = \arg\max_{\text{输入 } x} \; \text{priority}(x)
\]

当该最大值对应的子类型（这里是 `matrix`）无法承载广播形状时，分配失败。

#### 4.3.3 源码精读

契约全部在 `test_iter_allocate_output_subtype`：

[test_interaction.py:102-106](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_interaction.py#L102-L106) —— 「优先级胜出」分支：`a` 是 matrix、`b` 是普通 ndarray，输出被分配成 matrix：

```python
a = np.matrix([[1, 2], [3, 4]])
b = np.arange(4).reshape(2, 2).T
i = np.nditer([a, b, None], [],
              [['readonly'], ['readonly'], ['writeonly', 'allocate']])
assert_(type(i.operands[2]) is np.matrix)
assert_equal(i.operands[2].shape, (2, 2))
```

[test_interaction.py:108-111](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_interaction.py#L108-L111) —— 「三维输出崩」分支：把 `b` 换成 `(1,2,2)`，广播形状变成三维，matrix 装不下，直接抛 `RuntimeError`：

```python
# matrix always wants things to be 2D
b = np.arange(4).reshape(1, 2, 2)
assert_raises(RuntimeError, np.nditer, [a, b, None], [],
              [['readonly'], ['readonly'], ['writeonly', 'allocate']])
```

[test_interaction.py:112-118](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_interaction.py#L112-L118) —— 「`no_subtype` 救场」分支：给输出加 `'no_subtype'`，改用基类 ndarray，三维形状 `(1,2,2)` 被接住：

```python
# but if subtypes are disabled, the result can still work
i = np.nditer([a, b, None], [],
              [['readonly'], ['readonly'],
               ['writeonly', 'allocate', 'no_subtype']])
assert_(type(i.operands[2]) is np.ndarray)
assert_equal(i.operands[2].shape, (1, 2, 2))
```

为什么 matrix 装不下三维？回到 [defmatrix.py:172-193](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L172-L193) 的 `__array_finalize__`：当一个三维视图被创建并交给 matrix 收尾时，它会把长度为 1 的维度挤掉、试图压成二维；若压完仍超过二维就抛 `ValueError("shape too large to be a matrix.")`。`nditer` 在分配输出子类型时正是撞上了这条不变量——输出需要一个 `(1,2,2)` 的缓冲，而 matrix 的构造/收尾链拒绝承载它，于是 `nditer` 把这个失败上抛为 `RuntimeError`。`'no_subtype'` 绕开了「用 matrix 这个子类型」这一步，直接用 `ndarray`，自然没有这条约束。

#### 4.3.4 代码实践

1. **实践目标**：复现「matrix 胜出」「三维崩」「no_subtype 救场」三态。
2. **操作步骤**：
   ```python
   import numpy as np
   a = np.matrix([[1, 2], [3, 4]])

   # (1) matrix 胜出
   b = np.arange(4).reshape(2, 2).T
   i = np.nditer([a, b, None], [],
                 [['readonly'], ['readonly'], ['writeonly', 'allocate']])
   print(type(i.operands[2]), i.operands[2].shape)

   # (2) 三维输出 → 崩
   b3 = np.arange(4).reshape(1, 2, 2)
   try:
       np.nditer([a, b3, None], [],
                 [['readonly'], ['readonly'], ['writeonly', 'allocate']])
   except RuntimeError as e:
       print("RuntimeError:", e)

   # (3) no_subtype 救场
   i2 = np.nditer([a, b3, None], [],
                  [['readonly'], ['readonly'],
                   ['writeonly', 'allocate', 'no_subtype']])
   print(type(i2.operands[2]), i2.operands[2].shape)
   ```
3. **需要观察的现象**：第 (1) 步打印 `numpy.matrixlib.defmatrix.matrix (2, 2)`；第 (2) 步捕获到 `RuntimeError`；第 (3) 步打印 `numpy.ndarray (1, 2, 2)`。
4. **预期结果**：与上述一致。注意第 (2) 步的 `RuntimeError` 报错信息可能随版本变化，关键是异常**类型**是 `RuntimeError`。
5. **待本地验证**：请在本机运行确认（特别是报错文案）。

#### 4.3.5 小练习与答案

**练习 1**：如果把第 (1) 步的 `a` 也换成普通 `ndarray`（输入里没有任何 matrix），输出还会是 matrix 吗？
**答案**：不会。没有任何高优先级输入，输出按基类 `ndarray` 分配。

**练习 2**：第 (2) 步抛的是 `RuntimeError` 而不是 `ValueError`，这说明异常来自哪里？
**答案**：来自 `nditer` 的 C 层分配逻辑（它在尝试用 matrix 子类型构造输出、并发现形状不可承载时上抛），而不是直接来自 Python 层的 `__array_finalize__`。这正是它表现为 `RuntimeError` 的原因。

---

### 4.4 nanfunctions 保持 matrix 类型与朝向

#### 4.4.1 概念说明

`np.nansum`、`np.nanmin`、`np.nanmax`、`np.nanmean`……这一组 `nan*` 函数的作用是「忽略 NaN 做归约」。表面看它们和 `matrix` 毫无关系（`matrix` 里并没有 `nansum` 方法），但测试要求它们对 `matrix` 输入也必须**保住类型与朝向**：`axis=0` 出 `(1,N)`、`axis=1` 出 `(N,1)`、`axis=None` 出标量。

这就引出一个有意思的问题：`matrix` 的朝向（`(N,1)` 列向量）来自它**自己重写过的** `sum/min/max`（都加了 `keepdims=True`，见 u3-l2）。可 `np.nansum` 是顶层函数、不是 `matrix.nansum`，它怎么也能得到正确的 `(N,1)`？

答案藏在 `nan*` 函数的「子类安全慢路径」里。

#### 4.4.2 核心流程

`nanmin` 内部对输入分两条路：

```
a 是普通 ndarray（且非 object）？
│
├─ 是 → 快路径：np.fmin.reduce(...)   ← ufunc 归约，不保证子类语义
│
└─ 否（matrix/object 等子类）→ 慢路径：
        _replace_nan(a, +inf)          # 把 NaN 替换成 +inf
        np.amin(a, axis=...)           # ← 关键：调用的是顶层 amin
           │
           └─ numpy 内部 _wrapfunc(a, 'min', ...)
                  └─ a.min(axis=...)   # 委托给 matrix 自己重写过的 min！
                         └─ matrix.min（keepdims=True）→ 正确朝向
```

也就是说，`matrix` 走的是慢路径，而慢路径里的 `np.amin`/`np.sum` 会通过 `_wrapfunc` **回调 `matrix` 自己重写过的归约方法**——这些方法带着 `keepdims=True`，于是 `(N,1)`/`(1,N)` 的朝向自然就保住了。类型之所以还是 `matrix`，是因为整条链路都在 `matrix` 子类上完成，结果经 `__array_finalize__` 收尾仍是 `matrix`。

> 这也解释了一个反直觉的点：`nan*` 保形**不是**靠 `__array_finalize__` 单独完成的。光靠 `__array_finalize__` 只能把 1 维补成 `(1,N)` 行向量，得不到 `(N,1)` 列向量。真正给出列向量的，是 matrix 自己那套 `keepdims=True` 的归约方法。

#### 4.4.3 源码精读

行为契约分两个测试。先看 `nanmin`/`nanmax`（含全 NaN 切片的告警行为）：

[test_interaction.py:172-181](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_interaction.py#L172-L181) —— 类型与朝向：

```python
mat = np.matrix(np.eye(3))
for f in [np.nanmin, np.nanmax]:
    res = f(mat, axis=0); assert_(isinstance(res, np.matrix) and res.shape == (1, 3))
    res = f(mat, axis=1); assert_(isinstance(res, np.matrix) and res.shape == (3, 1))
    res = f(mat);         assert_(np.isscalar(res))
```

[test_interaction.py:182-206](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_interaction.py#L182-L206) —— 把第 1 行整体置 NaN 后的告警分支（对应 issue #4628）：

- `axis=0`：每一列在第 1 行有 NaN，但其余行有值，`nanmin` 忽略 NaN 后仍有结果 → **不告警**。
- `axis=1`：第 1 行整行是 NaN，归约出 NaN，并发出一条 `RuntimeWarning("All-NaN slice encountered")` → 测试断言 `len(w) == 1` 且 `w[0].category` 是 `RuntimeWarning`，同时 `res[1,0]` 是 NaN、其余位置不是。
- `axis=None`：全矩阵仍有非 NaN 值，结果是标量、不是 NaN → **不告警**。

再看更广的一组（含 `nancumsum`/`nancumprod` 的特殊形状）：

[test_interaction.py:213-234](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_interaction.py#L213-L234) —— `nanargmin/nanargmax/nansum/nanprod/nanmean/nanvar/nanstd` 同样保 `(1,N)`/`(N,1)`/标量；而 `nancumsum/nancumprod` 因为是「累积」而非「归约」，`axis=0`、`axis=1` 都得到 `(3,3)`，`axis=None` 得到 `(1,9)`（即 `3*3` 展平成一行）。

这套行为的实现根源，在 `nanmin` 的慢路径分支：

[_nanfunctions_impl.py:345-369](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py#L345-L369)：

```python
kwargs = {}
if keepdims is not np._NoValue:
    kwargs['keepdims'] = keepdims
...
if (type(a) is np.ndarray or type(a) is np.memmap) and a.dtype != np.object_:
    # 快路径：ufunc 归约，对子类不安全
    res = np.fmin.reduce(a, axis=axis, out=out, **kwargs)
    ...
else:
    # 慢路径，但对子类安全：matrix 走这里
    a, mask = _replace_nan(a, +np.inf)
    res = np.amin(a, axis=axis, out=out, **kwargs)   # ← 委托给 matrix.min
    ...
```

因为 `type(a) is np.ndarray` 对 `matrix` 为**假**（`matrix` 是子类，`type` 不等于 `ndarray`），所以 `matrix` 必走慢路径，慢路径里的 `np.amin(a, ...)` 经 `_wrapfunc` 委托给 `a.min(...)`，也就是 [defmatrix.py:691-724](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L691-L724) 里那个带 `keepdims=True` 的 `matrix.min`。朝向由此而来。

#### 4.4.4 代码实践

1. **实践目标**：验证 `nan*` 对 matrix 既保类型又保朝向，并复现「全 NaN 行」的告警。
2. **操作步骤**：
   ```python
   import warnings, numpy as np
   mat = np.matrix(np.eye(3))
   print(np.nansum(mat, axis=0).shape)   # (1, 3)
   print(np.nansum(mat, axis=1).shape)   # (3, 1)
   print(type(np.nansum(mat, axis=1)))   # matrix

   mat[1] = np.nan                        # 整行置 NaN
   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter('always')
       r = np.nanmin(mat, axis=1)
       print(len(w), w[0].category if w else None)  # 1, RuntimeWarning
       print(np.isnan(r[1, 0]), not np.isnan(r[0, 0]))
   ```
3. **需要观察的现象**：前三行依次打印 `(1, 3)`、`(3, 1)`、`<class 'numpy.matrixlib.defmatrix.matrix'>`；告警分支打印 `1 RuntimeWarning` 与 `True True`。
4. **预期结果**：与上述一致。注意 `nansum(axis=1)` 得到列向量是关键证据，说明走了 matrix 自己的 `sum`。
5. **待本地验证**：请在本机运行确认。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `mat` 换成普通 `np.eye(3)`（非 matrix），`np.nansum(mat, axis=1)` 的形状是什么？
**答案**：`(3,)`。普通 ndarray 走快路径（`np.add.reduce`），没有 matrix 那套 `keepdims=True`，结果是一维。

**练习 2**：为什么 `mat[1]=np.nan` 后 `np.nanmin(mat, axis=None)` **不**告警，而 `axis=1` 告警？
**答案**：`axis=None` 在全矩阵范围归约，只要矩阵里还有任何非 NaN 值，结果就不是 NaN、不触发「All-NaN slice」告警；`axis=1` 是逐行归约，第 1 行**整行**皆 NaN，这一行的归约结果是 NaN 并触发告警。

---

### 4.5 MMatrix 多重继承与 __array_finalize__ 链

#### 4.5.1 概念说明

到目前为止我们讨论的都是「matrix 单独被某个 numpy 机制使用」。本节要面对一个更极端的场景：**同时**拥有 `matrix`（恒二维、`*` 是矩阵乘）和 `MaskedArray`（带缺失值掩码）两套行为——也就是定义一个类同时继承这两个。

`MaskedArray` 也是 `ndarray` 的子类，它用 `_mask` 数组记录哪些元素被「掩掉」。如果我们想要「既是 matrix、又能 mask」，最直接的想法是 `class MMatrix(MaskedArray, np.matrix)`。但这立刻带来一个 Python 层面的难题：**派生新数组时，Python 只会自动调用一个 `__array_finalize__`。**

具体说，NumPy 在 C 层创建子类视图时会调用一次 `__array_finalize__`，而方法查找走 MRO（方法解析顺序）。`MMatrix` 的 MRO 是 `[MMatrix, MaskedArray, matrix, ndarray, object]`——如果不自己重写 `__array_finalize__`，查找会命中 `MaskedArray.__array_finalize__`，于是 `matrix.__array_finalize__` **永远不会被调用**，二维不变量随之失效。反之亦然。解决办法是：在 `MMatrix` 里显式重写 `__array_finalize__`，并**手动把两个基类的 finalize 都调一遍**。

#### 4.5.2 核心流程

```
MMatrix(MaskedArray, np.matrix)
│
├─ __new__(cls, data, mask):
│      mat = np.matrix(data)                       # 先拿到一个二维 matrix
│      return MaskedArray.__new__(cls, data=mat, mask=mask)  # 再叠加掩码
│
└─ __array_finalize__(self, obj):                  # 每次派生都跑
       np.matrix.__array_finalize__(self, obj)     # ① 守二维不变量
       MaskedArray.__array_finalize__(self, obj)   # ② 守掩码(_mask 等)不变量
```

两个基类的 finalize 各管一摊：`matrix` 的负责把形状补回二维；`MaskedArray` 的负责在视图上传播 `_mask`、`fill_value` 等掩码属性。**缺任何一个，对应的不变量就破。**

#### 4.5.3 源码精读

样本类定义在测试文件顶部：

[test_masked_matrix.py:22-37](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_masked_matrix.py#L22-L37)：

```python
class MMatrix(MaskedArray, np.matrix,):

    def __new__(cls, data, mask=nomask):
        mat = np.matrix(data)
        _data = MaskedArray.__new__(cls, data=mat, mask=mask)
        return _data

    def __array_finalize__(self, obj):
        np.matrix.__array_finalize__(self, obj)
        MaskedArray.__array_finalize__(self, obj)

    @property
    def _series(self):
        _view = self.view(MaskedArray)
        _view._sharedmask = False
        return _view
```

逐行解读：

- `__new__`：先用 `np.matrix(data)` 把数据规整成二维 matrix（拿到 matrix 的形状/dtype/内存），再把它交给 `MaskedArray.__new__(cls, data=mat, mask=mask)` 完成掩码叠加。注意这里显式写成 `MaskedArray.__new__(...)` 而非 `super().__new__(...)`，是为了**精确**调用 `MaskedArray` 的构造，确保掩码逻辑一定被执行。
- `__array_finalize__`：核心就在这两行——**显式调用两个基类**的 finalize。这就是多重继承下「让两套不变量都生效」的标准写法。
- `_series`：返回一个 `MaskedArray` 视图（剥掉 matrix 身份），并把 `_sharedmask` 置 `False` 避免与原对象共享掩码——这是 `MaskedArray` 的惯用法，与本讲主题关系不大，略过。

它「同时满足两个基类」的证据，在 `TestSubclassing` 里：ufunc 作用后结果仍是 `MMatrix`，且其 `_data` 是 `np.matrix`：

[test_masked_matrix.py:195-214](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_masked_matrix.py#L195-L214)：

```python
x, mx = self._create_data()            # mx = MMatrix(arange(5), mask=[0,1,0,0,0])
assert_(isinstance(log(mx), MMatrix))  # 一元 ufunc 仍是 MMatrix
assert_(isinstance(add(mx, mx), MMatrix))   # 二元 ufunc 仍是 MMatrix
assert_(isinstance(add(mx, mx)._data, np.matrix))  # 内层 _data 是 matrix
```

`log(mx)`、`add(mx, mx)` 都能返回 `MMatrix` 且 `_data` 是 `matrix`，说明两套 finalize（保二维 + 保掩码）都被正确触发——这正是手动双调 `__array_finalize__` 的回报。

> 为什么结果类型是 `MMatrix` 而非 `matrix`？因为 ufunc 输出子类型由 `__array_priority__` 决定，`MMatrix`（经 `MaskedArray`）的优先级高于 `matrix`，故 `log`/`add` 选用 `MMatrix` 收尾，再走它那个「双调」finalize。

#### 4.5.4 代码实践

1. **实践目标**：亲手构造 `MMatrix`，验证它「既二维又可掩码」，并观察双调 finalize 的必要性。
2. **操作步骤**：
   ```python
   import numpy as np
   from numpy.ma.core import MaskedArray, nomask, log

   class MMatrix(MaskedArray, np.matrix):
       def __new__(cls, data, mask=nomask):
           mat = np.matrix(data)
           return MaskedArray.__new__(cls, data=mat, mask=mask)
       def __array_finalize__(self, obj):
           np.matrix.__array_finalize__(self, obj)
           MaskedArray.__array_finalize__(self, obj)

   mx = MMatrix([[1., 2.], [3., 4.]], mask=[0, 1, 0, 0])
   print(mx.shape, type(mx._data))          # (2,2), matrix
   print(type(log(mx)))                     # MMatrix
   ```
   再做对照实验：把 `__array_finalize__` 里 `np.matrix.__array_finalize__(self, obj)` 那行**注释掉**，重新构造，观察 `mx[0]` 之类的视图是否还能保持二维。
3. **需要观察的现象**：完整版打印 `(2, 2) <class 'numpy.matrixlib.defmatrix.matrix'>` 与 `MMatrix`；注释掉 matrix finalize 那行后，某些派生视图会丢掉二维约束（行为可能表现为形状异常或不再是 `(1,N)`）。
4. **预期结果**：双调时一切正常；只调 `MaskedArray` 的 finalize 时，matrix 的二维不变量失去保障。
5. **待本地验证**：对照实验的具体表现（是否抛错、抛什么错）与 numpy 版本相关，请在本机运行确认。

#### 4.5.5 小练习与答案

**练习 1**：`MMatrix` 的 MRO 中，`MaskedArray` 排在 `matrix` 前面。如果不重写 `__array_finalize__`，哪个基类的 finalize 会被自动调用、哪个会被跳过？
**答案**：会自动调用 `MaskedArray.__array_finalize__`（MRO 中靠前），`matrix.__array_finalize__` 会被跳过，导致二维不变量失守。这正是必须手动双调的原因。

**练习 2**：`MMatrix.__new__` 里为什么写成 `MaskedArray.__new__(cls, data=mat, mask=mask)`，而不是 `super().__new__(cls, ...)`？
**答案**：为了精确、稳定地走 `MaskedArray` 的构造路径（它负责掩码）。`super()` 在多重继承下的解析顺序未必指向你期望的那个基类，显式写全类名更可控、意图更清晰。

---

## 5. 综合实践

把本讲五个最小模块串成一个排查任务。

**场景**：你接手一段别人的代码，里面混用了 `matrix`、`nan*` 函数、`nditer` 和一个自定义子类，行为很迷。请你用本讲学到的「子类型传染」视角，预测并验证每一步的**类型与形状**。

```python
import warnings, numpy as np

# (a) subok 开关：同一矩阵，两种降级姿势
m = np.matrix(np.arange(6, dtype='f4').reshape(2, 3))
a1 = m.astype('f4', subok=False)
a2 = np.zeros_like(m, subok=False)
assert type(a1) is np.ndarray and type(a2) is np.ndarray, "subok=False 应降级为 ndarray"

# (b) nan* 保形：列向量从哪来？
with warnings.catch_warnings():
    warnings.simplefilter('ignore')
    s0 = np.nansum(m, axis=0); s1 = np.nansum(m, axis=1)
assert isinstance(s0, np.matrix) and s0.shape == (1, 3)
assert isinstance(s1, np.matrix) and s1.shape == (3, 1)   # ← 关键：列向量

# (c) nditer：matrix 胜出 / 三维崩 / no_subtype 救场
i = np.nditer([m, np.arange(6).reshape(2, 3).T, None], [],
              [['readonly'], ['readonly'], ['writeonly', 'allocate']])
assert type(i.operands[2]) is np.matrix
b3 = np.arange(6).reshape(1, 2, 3)
try:
    np.nditer([m, b3, None], [],
              [['readonly'], ['readonly'], ['writeonly', 'allocate']])
    raised = False
except RuntimeError:
    raised = True
assert raised, "三维输出应触发 RuntimeError"
i2 = np.nditer([m, b3, None], [],
               [['readonly'], ['readonly'],
                ['writeonly', 'allocate', 'no_subtype']])
assert type(i2.operands[2]) is np.ndarray and i2.operands[2].shape == (1, 2, 3)

print("全部断言通过")
```

**任务要求**：

1. 先**不运行**，逐段写下你预测的类型/形状/是否抛错。
2. 再运行，对照预测，把预测错的点用本讲的概念解释清楚（例如 (b) 的列向量来自 `matrix.min/sum` 的 `keepdims=True`，经 `nan*` 慢路径委托触发）。
3. 进阶：把 (b) 的 `m` 换成普通 `np.arange(6).reshape(2,3)`（非 matrix），重跑，解释为什么 `s1.shape` 变成 `(2,)`。

> 多数步骤的运行结果需**待本地验证**；上面的断言是按本讲引用的测试契约写出的预期。

## 6. 本讲小结

- **`subok` 是子类型传染的总开关**：`astype`/`zeros_like`/`ones_like`/`empty_like` 默认 `subok=True` 保子类型，`subok=False` 强制降级为 `ndarray`。
- **`nditer` 自动分配输出按 `__array_priority__` 选子类型**：matrix（10.0）胜过 ndarray（0.0）；但 matrix 的二维约束与三维广播输出冲突会抛 `RuntimeError`，`'no_subtype'` 标志可绕回基类 ndarray。
- **`nan*` 函数保形走的是「子类安全慢路径」**：matrix 因 `type(a) is not np.ndarray` 必走慢路径，慢路径里的 `np.amin/np.sum` 经 `_wrapfunc` 委托给 matrix 自己 `keepdims=True` 的归约方法，于是类型与 `(1,N)`/`(N,1)` 朝向都保住——这并非单靠 `__array_finalize__` 能做到。
- **多重继承必须手动双调 `__array_finalize__`**：MRO 只自动命中一个基类的 finalize，`MMatrix(MaskedArray, np.matrix)` 必须显式调用两个基类的 finalize，才能同时守住「二维」与「掩码传播」两套不变量。
- **贯穿全讲的两个协议**：`subok`（构造时选不选子类型）与 `__array_priority__`（混合运算/分配时谁赢）共同决定了 matrix 在 numpy 生态里的「存活方式」。

## 7. 下一步学习建议

- **u3-l6（类型存根 defmatrix.pyi）**：本讲多次涉及「返回类型」，下一讲正好讲 `.pyi` 如何用泛型签名把 `matrix[_2D, _DTypeT_co]`、归约方法的多个 `overload` 表达出来，可以和本讲的运行时行为对照阅读。
- **u3-l7（测试体系）**：本讲大量以测试文件为契约，下一讲会系统梳理 `tests/` 目录的职责划分与命名约定，并教你用 pytest 选择性运行（比如本讲的 `test_iter_allocate_output_subtype`、`test_nanfunctions_matrices`）。
- **向 numpy 核心延伸**：若想彻底搞清 `nditer` 的子类型分配与 `no_subtype` 的 C 层实现，可阅读 `numpy/_core/src/multiarray/nditer_constr.c`（本仓库未纳入 matrixlib，需切到 numpy 根目录查找）；`nan*` 慢路径的完整逻辑见 [lib/_nanfunctions_impl.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py)。
- **关于多重继承的更一般规律**：可以再读 `numpy/ma/core.py` 里 `MaskedArray.__array_finalize__` 的实现，理解它传播了哪些属性（`_mask`、`fill_value`、`_hardmask`、`_sharedmask`），从而明白 4.5 里「为什么缺它就不行」。

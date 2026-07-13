# ndarray 子类化机制 __array_finalize__ 与 __array_priority__

## 1. 本讲目标

本讲聚焦 `np.matrix` 作为 `ndarray` 子类的两条「生命线」：

- **`__array_finalize__`**：NumPy 提供给子类的统一收尾钩子。每当 NumPy 内部（切片、视图、`copy`、`astype`、ufunc 等）从一个已有数组派生出新数组时，C 层都会自动调用它。`matrix` 正是利用它把结果**重新补回二维**，从而兑现「永远二维」的承诺。
- **`__array_priority__`**：决定两个不同子类做二元运算时，结果采用谁的类型。

学完本讲，你应当能够：

1. 说清楚 `matrix` 为什么不会因为一次切片或一次加法就「掉」成普通 `ndarray`。
2. 读懂 `matrix.__array_finalize__` 的四条分支，并解释它如何把 0 维、1 维、>2 维的中间结果重新定型。
3. 解释 `_set_shape` 这个下划线方法的作用——以及为什么 `matrix` **绝不能**用 `self.shape = ...` 来改形状。
4. 解释 `_getitem` 标志如何让 `__getitem__` 与 `__array_finalize__` 协作，从而正确区分「行向量」与「列向量」。
5. 用 `__array_priority__` 预测并验证 `matrix` 与自定义子类混合运算时的胜出类型。

## 2. 前置知识

本讲默认你已掌握 u1-l1 ~ u2-l3 的内容，特别是：

- **ndarray 用 `__new__` 构造**（而非 `__init__`）。`matrix` 没有 `__init__`，全部构造在 `__new__` 中完成（见 u2-l1）。
- **视图（view）**：两个数组可以共享同一块内存，却有不同形状/dtype/子类型。`arr.view(cls)` 会把 `arr` 重新解释为 `cls` 类型的视图。
- **强制二维**：`matrix` 把 0 维补成 `(1,1)`、1 维补成 `(1,N)`、超过二维直接报错（见 u2-l1 的 `__new__`）。

为什么「子类化 ndarray 很难」？因为 NumPy 的绝大多数操作（切片、转置、`+`、`*`、`copy`、`astype` …）都走 C 层，由 C 代码创建**新数组**。如果这些新数组只是普通 `ndarray`，子类辛辛苦苦维护的约束（比如 `matrix` 的二维性）就会在一次切片后瞬间丢失。NumPy 的解法是给子类留一个「收尾钩子」——也就是本讲的主角 `__array_finalize__`。

> 关键直觉：`__new__` 只在「显式构造」时跑一次；而 `__array_finalize__` 在「每次派生」时都跑，所以它才是子类约束的真正守护者。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [numpy/matrixlib/defmatrix.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py) | `matrix` 类定义。本讲直接精读其中的 `__array_priority__`、`__array_finalize__`、`__getitem__` 三段。 |
| numpy/_core/src/multiarray/methods.c | C 层。定义私有方法 `_set_shape`，并把它注册进 `ndarray` 的方法表。 |
| numpy/_core/src/multiarray/getset.c | C 层。定义公开 `.shape` 设置器（带弃用警告）、`_set_shape` 真正调用的 `array_shape_set_internal`，以及 `__array_priority__` 的默认 getter。 |
| numpy/_core/include/numpy/ndarraytypes.h | C 层。定义 `NPY_PRIORITY` 常量，即 `ndarray` 基类的默认优先级。 |
| numpy/_core/src/multiarray/nditer_constr.c | C 层。`nditer` 按 `__array_priority__` 挑选输出子类型的逻辑。 |
| numpy/matrixlib/tests/test_defmatrix.py | 测试。`TestNewScalarIndexing` 等覆盖了「永远二维」的索引语义，可作为行为的权威断言。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，与规格中的四个最小模块一一对应：

- 4.1 `__array_finalize__`：matrix 保形的总开关
- 4.2 `_set_shape`：就地改形状，且绕开弃用警告
- 4.3 `_getitem` 标志：`__getitem__` 与 `__array_finalize__` 的握手
- 4.4 `__array_priority__`：二元运算的子类胜出权

---

### 4.1 `__array_finalize__`：matrix 保形的总开关

#### 4.1.1 概念说明

`__array_finalize__` 是 NumPy 给所有 `ndarray` 子类的统一钩子。它的调用契约是：

> 每当 NumPy 内部代码基于一个「来源数组 `obj`」派生出一个新的子类数组 `self` 时，C 层会调用 `self.__array_finalize__(obj)`。

典型触发时机包括（不限于）：

- `arr.view(SomeSubclass)` —— 把 `arr` 当成 `SomeSubclass` 视图。
- 切片 / 索引返回数组：`arr[1:]`、`arr[:, 0]`。
- `arr.copy()`、`arr.astype(...)`。
- ufunc 的输出分配：`arr + 1`、`np.add(a, b)`。
- 显式构造：`SomeSubclass(np.arange(4))` 的 `__new__` 末尾。

对 `matrix` 而言，这些都是「危险时刻」：切片 `m[0]` 在普通 `ndarray` 上会返回 1 维数组，转置、运算也会产生非二维中间结果。`matrix` 必须在 `__array_finalize__` 里把这些结果**重新补回二维**，否则它的核心不变量就被破坏了。

`__array_finalize__` 的另一个标准职责是「从 `obj` 复制子类自定义属性」。`matrix` 没有额外属性（它的「二维性」靠形状本身保证），所以它的实现只关心形状。

#### 4.1.2 核心流程

`matrix.__array_finalize__(self, obj)` 的判断流程（伪代码）：

```
1. 先无条件 self._getitem = False       # 重置标志（4.3 节详述）
2. if obj 是 matrix 且 obj._getitem:    # 正处于索引中途
       直接 return                       # 让 __getitem__ 自己收尾
3. if self.ndim == 2:  return            # 已经二维，无事可做
4. if self.ndim > 2:                      # 维度太多
       去掉所有长度为 1 的维度
       if 剩下正好 2 维:  _set_shape(...) ; return
       else:  raise ValueError("shape too large to be a matrix.")
5. if self.ndim <= 1:                     # 0 维或 1 维，补成行向量
       0 维 → _set_shape((1, 1))
       1 维 → _set_shape((1, N))
```

要点：

- **第 2 步是「短路」**：只有在索引中途才走这条路，平时 `obj._getitem` 都是 `False`。
- **第 4 步解释了那个奇怪的报错**：u2-l1 提过 ndarray 输入路径会抛 `shape too large to be a matrix.`，根因就在这里——`data.view(cls)` 会触发 `__array_finalize__`，当原数组 >2 维时走到这一分支。
- **第 5 步统一把低维补成行向量** `(1, N)`。注意：`__array_finalize__` 永远只能补成行向量，因为它看不到「索引表达式」本身，无从判断用户到底想要行还是列。区分行/列的工作交给了 `__getitem__`（见 4.3）。

#### 4.1.3 源码精读

类属性优先级（4.4 节详述）：

- [numpy/matrixlib/defmatrix.py:L117-L117](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L117-L117) —— `__array_priority__ = 10.0`，定义在 `class matrix` 紧接着文档字符串的位置，是一个**类属性**。

`__array_finalize__` 主体：

- [numpy/matrixlib/defmatrix.py:L172-L193](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L172-L193) —— 完整实现。注意第一行 `self._getitem = False` 与紧接着的 `if (isinstance(obj, matrix) and obj._getitem): return` 构成「索引短路」。

逐段对应：

- [numpy/matrixlib/defmatrix.py:L172-L175](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L172-L175) —— 重置 `_getitem` 标志 + 索引短路返回。
- [numpy/matrixlib/defmatrix.py:L176-L178](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L176-L178) —— 已是二维，直接返回。
- [numpy/matrixlib/defmatrix.py:L179-L186](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L179-L186) —— 超过二维：用生成器 `tuple(x for x in self.shape if x > 1)` 挤掉长度为 1 的维度，恰好剩二维就 `_set_shape` 收尾，否则抛 `shape too large to be a matrix.`。
- [numpy/matrixlib/defmatrix.py:L187-L193](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L187-L193) —— 0 维/1 维补成 `(1,1)` / `(1, N)` 行向量。

可以看到，`__array_finalize__` 改形状一律走 `self._set_shape(...)`，**没有一处**用 `self.shape = ...`。为什么？这正是下一节的主题。

#### 4.1.4 代码实践

写一个继承 `ndarray` 的玩具子类，在 `__array_finalize__` 里打印，直观感受它的「每次派生都触发」。

```python
# 示例代码
import numpy as np

class MyArr(np.ndarray):
    def __array_finalize__(self, obj):
        # 标准写法：先打印（真实子类应在这里从 obj 拷贝自己的属性）
        print(f"  finalize: obj type = {type(obj).__name__}")
        if obj is None:
            return
        # MyArr 没有额外属性，所以这里无需拷贝

base = np.arange(4)

print("A) base.view(MyArr):")
a = base.view(MyArr)          # 预期触发 1 次

print("B) a[1:] (切片):")
c = a[1:]                     # 预期触发 1 次

print("C) a.copy():")
d = a.copy()                  # 预期触发 1 次

print("D) a.astype(float):")
e = a.astype(float)           # 预期触发 1 次

print("E) a + 1 (ufunc):")
f = a + 1                     # 预期触发 1 次（ufunc 输出按子类型分配）
```

操作步骤：

1. 把上面的代码存为 `finalize_probe.py`，`python finalize_probe.py` 运行。
2. 数一数每段打印了几次 `finalize:`。

需要观察的现象：

- A、B、C、D 每段都打印**一次**，证明 `view` / 切片 / `copy` / `astype` 都触发了 `__array_finalize__`。
- E（`a + 1`）也应触发——ufunc 会把输出分配为子类型，分配过程调用 `__array_finalize__`，`obj` 指向输入 `a`。

预期结果：每段各触发一次。各次触发的具体 `obj type` 与计数**待本地验证**（不同 NumPy 版本下 ufunc 路径偶有差异，但视图/切片/拷贝一定各一次）。

> 对比思考：把 `MyArr` 换成 `np.matrix` 再做 `m[0]`，返回的仍是 `(1,N)` 的 `matrix`——这就是 `matrix.__array_finalize__` 在第 5 步把 1 维补成行向量的效果。普通 `ndarray` 的 `arr[0]` 则是 1 维。

#### 4.1.5 小练习与答案

**练习 1**：`m = np.matrix(np.arange(6).reshape(2,3))`，执行 `m.T`。转置触发了 `__array_finalize__` 吗？为什么结果仍是二维 `matrix`？

**参考答案**：触发了。`.T` 返回转置视图，是「派生」操作，会调用 `__array_finalize__(obj=m)`。由于转置后 `self.ndim == 2`，命中 [L176-L178](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L176-L178) 的早退分支，形状保持二维。

**练习 2**：`np.stack([m, m])` 会抛 `shape too large to be a matrix.`（见 test_interaction.py 的 `test_stack`）。结合本节，说明这个错误是哪一行抛出的？

**参考答案**：`stack` 把两个 `(2,3)` matrix 沿新轴拼成 `(2,2,3)`，结果作为 `matrix` 子类型派生时触发 `__array_finalize__`，`self.ndim == 3 > 2`，进入 [L179-L186](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L179-L186)，挤掉长度为 1 的维度后仍为 3 维，于是在 [L186](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L186) 抛错。

---

### 4.2 `_set_shape`：就地改形状，且绕开弃用警告

#### 4.2.1 概念说明

`__array_finalize__` 改形状时调用的是 `self._set_shape(newshape)`，而不是 `self.shape = newshape`。这并非风格偏好，而是**必须如此**，原因有二：

1. **绕开弃用警告**。在当前 HEAD（NumPy 2.5+）中，对任意 `ndarray` 做 `arr.shape = ...` 赋值会发出 `DeprecationWarning`（源码注释标记 `Deprecated NumPy 2.5, 2026-01-05`）。`matrix` 在几乎每次切片/运算后都会改形状，如果走 `.shape =`，就会在正常使用中疯狂刷警告——这显然不可接受。
2. **就地重塑、不再次触发 `__array_finalize__`**。`_set_shape` 与 `.shape =` 最终都调用同一个内部函数 `array_shape_set_internal`，它**就地**修改现有数组的维度与步长（不创建新对象），因此不会再次回调 `__array_finalize__`，避免了无限递归。

NumPy 专门保留了 `_set_shape` 这个下划线方法，注释直白地写着 `// For deprecation of ndarray setters`——即「为了让内部代码能在不触发弃用警告的前提下设置形状」。

#### 4.2.2 核心流程

两条改形状路径对比：

```
self.shape = new          self._set_shape(new)
      │                          │
      ▼                          ▼
 array_shape_set            array__set_shape        (都在 methods.c / getset.c)
 (getset.c，getset 描述符)  (methods.c，私有方法)
      │                          │
      ├── 若 val 为 None → 报错  │
      ├── 发出 DeprecationWarning│  ← 关键差异
      └──────────┬───────────────┘
                 ▼
        array_shape_set_internal(self, val)   (getset.c)
                 │
                 ▼
        PyArray_Reshape → 就地改 nd/dimensions/strides
        （不新建数组，因此不回调 __array_finalize__）
```

#### 4.2.3 源码精读

C 层三处关键定义：

- [numpy/_core/src/multiarray/methods.c:L2864-L2872](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/src/multiarray/methods.c#L2864-L2872) —— `array__set_shape`，即 `_set_shape` 的实现，直接调用 `array_shape_set_internal`，**没有任何警告**。
- [numpy/_core/src/multiarray/methods.c:L3098-L3101](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/src/multiarray/methods.c#L3098-L3101) —— 把 `_set_shape` 注册进 `ndarray` 方法表，注释 `// For deprecation of ndarray setters` 点明了它的存在意义。
- [numpy/_core/src/multiarray/getset.c:L109-L127](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/src/multiarray/getset.c#L109-L127) —— 公开 `.shape` 设置器 `array_shape_set`。注意 [L118-L124](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/src/multiarray/getset.c#L118-L124) 的 `DEPRECATE("Setting the shape on a NumPy array has been deprecated in NumPy 2.5...")`，这就是 `matrix` 必须躲开的警告。末尾才调用 `array_shape_set_internal`。

两者共同调用的就地重塑函数：

- [numpy/_core/src/multiarray/getset.c:L56-L107](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/src/multiarray/getset.c#L56-L107) —— `array_shape_set_internal`。核心是 `PyArray_Reshape` 得到 `ret`，校验 `PyArray_DATA(ret) == PyArray_DATA(self)`（确保是同一块内存、即就地视图），然后把新的 `nd/dimensions/strides` 直接 `memcpy` 进 `self` 的字段。全程**不创建新的 Python 数组对象**，所以不会触发 `__array_finalize__`。

#### 4.2.4 代码实践

亲眼看到 `.shape =` 会刷弃用警告，而 `matrix` 的内部路径不会。

```python
# 示例代码
import warnings
import numpy as np

arr = np.arange(6)

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    arr.shape = (2, 3)          # 公开 setter

print("警告数量:", len(w))
for x in w:
    print("  类别:", x.category.__name__)
    print("  消息:", str(x.message)[:60])
```

操作步骤：

1. 运行上面的脚本。
2. 再运行一行 `m = np.asmatrix(np.arange(6).reshape(2,3)); _ = m[0]`，用同样的 `catch_warnings` 包住它，观察是否出现形状相关警告。

需要观察的现象：

- 第一段：应捕获到至少一条 `DeprecationWarning`，消息包含 `Setting the shape on a NumPy array has been deprecated`。
- 第二段：`matrix` 切片 `m[0]` 内部走了 `_set_shape`，**不应**出现该弃用警告（即便开了 `simplefilter("always")`）。

预期结果：如上。具体警告条数**待本地验证**（取决于 NumPy 小版本是否已把该警告升级）。

#### 4.2.5 小练习与答案

**练习 1**：既然 `_set_shape` 和 `.shape =` 最终都走 `array_shape_set_internal`，为什么 NumPy 不直接让 `.shape =` 不报警，反而新开一个 `_set_shape`？

**参考答案**：弃用是有意面向**用户**的：官方希望用户改用 `np.reshape(...)` 显式创建视图，而不是原地改形状。但 `matrix` 这类内部子类有「保形」的强需求，必须原地、无副作用地改形状，因此需要一个「员工通道」绕开面向用户的弃用警告——这就是 `_set_shape` 存在的理由（见 methods.c 注释）。

**练习 2**：假设把 `matrix.__array_finalize__` 里所有 `self._set_shape(...)` 改成 `self.shape = ...`，除了刷警告外，会不会触发无限递归？

**参考答案**：不会无限递归。因为 `array_shape_set_internal` 是就地修改、不新建数组对象，`__array_finalize__` 不会被再次调用。真正的危害是「每次 matrix 运算都刷一条弃用警告」的体验灾难，而非递归。

---

### 4.3 `_getitem` 标志：`__getitem__` 与 `__array_finalize__` 的握手

#### 4.3.1 概念说明

回顾 4.1：`__array_finalize__` 处理 1 维结果时，只会无脑补成行向量 `(1, N)`。但索引语义里，`m[:, 0]`（取一列）理应得到**列向量** `(N, 1)`。`__array_finalize__` 看不到索引表达式，做不了这个判断——只有 `__getitem__` 知道用户写了什么索引。

于是 `matrix` 设计了一个握手协议：

- `__getitem__` 在调用基类 `ndarray.__getitem__` **之前**，把 `self._getitem` 置为 `True`；调用结束（无论是否异常）在 `finally` 里置回 `False`。
- `__array_finalize__` 开头看到 `obj 是 matrix 且 obj._getitem` 为真，就**立即返回**，不做任何形状修补。
- 这样，索引过程中产生的中间视图保持其「自然形状」（往往是 1 维），交给 `__getitem__` 末尾自己的逻辑去决定行向量还是列向量。

一句话：`_getitem` 是 `__getitem__` 对 `__array_finalize__` 说的「这次让我来，你别插手」。

#### 4.3.2 核心流程

```
m[:, 0] 的执行链：

matrix.__getitem__(m, (slice(None), 0))
 │
 ├─ self._getitem = True                         # 挂牌：我在索引
 │
 ├─ out = ndarray.__getitem__(self, index)       # 基类取索引
 │       └─ 内部创建视图 → 触发 __array_finalize__(view, m)
 │              ├─ self._getitem = False          # 仍会执行这行（复位）
 │              ├─ isinstance(m, matrix) and m._getitem == True → return  # 短路！不补形
 │              └─ 所以 view 保持 ndarray 的自然形状（1 维）
 │
 ├─ finally: self._getitem = False               # 摘牌
 │
 └─ out.ndim == 1:                                # __getitem__ 自己决定朝向
        若 len(index) > 1 且 index[1] 是标量 → reshape((N, 1))   # 列向量
        否则                                      → reshape((1, N))   # 行向量
```

#### 4.3.3 源码精读

`__getitem__` 全貌：

- [numpy/matrixlib/defmatrix.py:L195-L219](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L195-L219) —— 完整实现。

逐段对应：

- [numpy/matrixlib/defmatrix.py:L195-L201](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L195-L201) —— 「挂牌 / 调基类 / `finally` 摘牌」三件套。`try/finally` 保证即使基类取索引抛异常，`_getitem` 也会被复位，不会污染后续操作。
- [numpy/matrixlib/defmatrix.py:L203-L204](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L203-L204) —— 若结果不是 `ndarray`（如标量），原样返回，跳过一切形状逻辑。
- [numpy/matrixlib/defmatrix.py:L206-L207](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L206-L207) —— 0 维结果用 `out[()]` 取出真正的 Python 标量（这是 `matrix` 索引能返回裸标量的关键）。
- [numpy/matrixlib/defmatrix.py:L208-L219](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L208-L219) —— 1 维结果的朝向判定。`try: n = len(index)` 兼容标量索引（`len` 会抛异常，落入 `n = 0`）；当 `n > 1 and isscalar(index[1])`，说明是 `m[行, 列]` 形式且第二维是标量列号 → 列向量 `(sh, 1)`，否则行向量 `(1, sh)`。

`__array_finalize__` 的配合点（与 4.1 同一段，这里强调短路）：

- [numpy/matrixlib/defmatrix.py:L172-L175](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L172-L175) —— 先复位 `self._getitem = False`，再判断「来源 `obj` 是处于索引中途的 `matrix`」，是则直接 `return`。

#### 4.3.4 代码实践

用断言验证 `_getitem` 握手带来的「行/列向量」差异。

```python
# 示例代码
import numpy as np
from numpy.testing import assert_array_equal

m = np.matrix([[1, 2], [3, 4]])

# 取一行：m[0] → 行向量 (1, 2)
assert m[0].shape == (1, 2), m[0].shape

# 取一列：m[:, 0] → 列向量 (2, 1)
assert m[:, 0].shape == (2, 1), m[:, 0].shape

# 标量索引：m[0, 0] → 裸标量（走了 L206-L207 的 out[()]）
assert m[0, 0] == 1
assert not isinstance(m[0, 0], np.matrix)

# 花式索引仍是 matrix（test_interaction.test_fancy_indexing）
assert isinstance(m[[0, 1], :], np.matrix)

print("all assertions passed")
```

操作步骤：

1. 运行脚本，确认全部断言通过。
2. 把 `np.matrix` 换成普通 `np.array`（`a = np.array([[1,2],[3,4]])`），重跑断言，看哪些会失败。

需要观察的现象：

- `matrix` 版：`m[0]` 是 `(1,2)`，`m[:, 0]` 是 `(2,1)`，`m[0,0]` 是裸 `int`。
- `ndarray` 版：`a[0]` 是 `(2,)` 一维，`a[:, 0]` 也是 `(2,)` 一维——正是 `matrix` 要通过 `_getitem` 握手去避免的「降维」。

预期结果：如上（断言通过即说明行为正确）。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 [L195-L201](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L195-L201) 里的 `_getitem = True/False`（即不做握手），`m[:, 0]` 会变成什么形状？为什么？

**参考答案**：会变成 `(1, 2)` 行向量，破坏列向量语义。因为没有 `_getitem` 标志，基类取索引产生的 1 维中间视图会进入 `__array_finalize__` 第 5 步，被无脑补成 `(1, N)` 行向量；随后 `__getitem__` 末尾 `out.ndim` 已被改成 1 之外的逻辑……总之朝向判定失效。

**练习 2**：`__getitem__` 用 `try/finally` 而不是直接 `self._getitem = False` 放在末尾，是出于什么考虑？

**参考答案**：防御性编程。若 `ndarray.__getitem__` 抛异常，`finally` 仍能复位 `_getitem`，避免某个 `matrix` 实例长期停留在 `_getitem = True` 状态，进而让后续无关操作的 `__array_finalize__` 全部短路、形状约束失效。

---

### 4.4 `__array_priority__`：二元运算的子类胜出权

#### 4.4.1 概念说明

当两个**不同子类型**的数组做二元运算（如 `matrix + ndarray_subclass`），结果的类型选谁？NumPy 用 `__array_priority__` 这个类属性来裁决：**优先级数值大的子类型胜出**，结果采用它的类型。

数值约定：

- 基类 `ndarray` 的默认优先级是 `0.0`（C 常量 `NPY_PRIORITY`）。
- `matrix` 把它抬高到 `10.0`，因此在 `matrix` 与普通 `ndarray` 运算时，结果总是 `matrix`。
- 用户自定义子类可以设更高的值（如 `100.0`）来「压过」`matrix`。

为什么 `matrix` 只设 `10.0` 而不是天文数字？因为优先级是个**协商值**：`matrix` 只是声明「我比普通数组重要」，但仍把决定权留给更「专业」的子类（比如 `MaskedArray`）。这正是多重继承与混合运算能工作的基础。

需要注意：`__array_priority__` 主要影响**输出子类型的选择**；输出产生后，仍会调用胜出子类的 `__array_finalize__`（以及 ufunc 的 `__array_wrap__`）。

#### 4.4.2 核心流程

二元运算挑选输出子类型（以 `np.add(a, b)` 为例）：

```
priority_a = PyArray_GetPriority(a, 0.0)   # 读 a.__array_priority__，无则 0.0
priority_b = PyArray_GetPriority(b, 0.0)
winner_type = (a 的类型) if priority_a >= priority_b else (b 的类型)
# 实际取严格更大者；相等时由操作数顺序等因素决定

result = 用 winner_type 分配输出数组
result.__array_finalize__(...)              # 胜出子类的收尾钩子仍会跑
```

`nditer`（通用迭代器）分配输出时遵循同一规则：遍历所有「只读」操作数，取 `__array_priority__` 最大者的类型作为输出子类型；若指定 `no_subtype` 标志，则强制降级为基类 `ndarray`。

#### 4.4.3 源码精读

`matrix` 设定优先级：

- [numpy/matrixlib/defmatrix.py:L117-L117](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L117-L117) —— `__array_priority__ = 10.0`。

基类默认值（C 层）：

- [numpy/_core/include/numpy/ndarraytypes.h:L93-L93](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/include/numpy/ndarraytypes.h#L93-L93) —— `#define NPY_PRIORITY 0.0`，即基类默认优先级。
- [numpy/_core/src/multiarray/getset.c:L216-L220](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/src/multiarray/getset.c#L216-L220) —— `array_priority_get`，`__array_priority__` 的默认 getter，返回 `NPY_PRIORITY`。子类若未覆盖该属性，查找会落到这个 C getter，得到 `0.0`。

`nditer` 按优先级选子类型：

- [numpy/_core/src/multiarray/nditer_constr.c:L3383-L3405](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/src/multiarray/nditer_constr.c#L3383-L3405) —— `npyiter_get_priority_subtype`，注释直说「`__array_priority__` 决定输出子类型，取优先级最高的输入类型」。

对应测试（行为权威）：

- [numpy/matrixlib/tests/test_interaction.py:L94-L118](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_interaction.py#L94-L118) —— `test_iter_allocate_output_subtype`：`nditer([matrix, ndarray, None])` 的输出是 `matrix`（优先级胜出）；当 `b` 为 `(1,2,2)` 时，`matrix` 试图保二维会失败而抛 `RuntimeError`；加 `no_subtype` 标志后输出降级为普通 `ndarray`，形状 `(1,2,2)` 才能成立。

#### 4.4.4 代码实践

用自定义子类验证「优先级高者胜出」。

```python
# 示例代码
import numpy as np

# 子类 A：不设优先级，继承默认 0.0
class LowP(np.ndarray):
    pass

# 子类 B：优先级 100.0，高于 matrix 的 10.0
class HighP(np.ndarray):
    __array_priority__ = 100.0

m  = np.matrix([[1, 2], [3, 4]])
lo = np.ones((2, 2)).view(LowP)
hi = np.ones((2, 2)).view(HighP)

r1 = np.add(m, lo)
print("matrix(10) + LowP(0)  ->", type(r1).__name__)   # 预期 matrix

r2 = np.add(m, hi)
print("matrix(10) + HighP(100) ->", type(r2).__name__)  # 预期 HighP
```

操作步骤：

1. 运行脚本，记录两次 `type(...).__name__`。
2. 把 `HighP.__array_priority__` 改成 `5.0`（低于 `matrix` 的 `10.0`），重跑，看 `r2` 的类型。

需要观察的现象：

- `r1`：`LowP` 默认优先级 `0.0 < 10.0`，`matrix` 胜出 → `matrix`。
- `r2`：`HighP` 优先级 `100.0 > 10.0`，`HighP` 胜出 → `HighP`。
- 改成 `5.0` 后：`matrix` 重新胜出 → `matrix`。

预期结果：如上。运行输出**待本地验证**，但胜负关系由优先级数值严格决定，可由源码 [nditer_constr.c:L3383-L3405](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/src/multiarray/nditer_constr.c#L3383-L3405) 推得。

#### 4.4.5 小练习与答案

**练习 1**：`LowP` 类里并没有写 `__array_priority__ = 0.0`，为什么它的有效优先级是 `0.0`？

**参考答案**：属性查找会沿 MRO 回溯到基类 `ndarray`。`ndarray` 的 `__array_priority__` 是一个 C getset 描述符（[getset.c:L216-L220](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/src/multiarray/getset.c#L216-L220)），getter 返回 `NPY_PRIORITY = 0.0`。所以子类不覆盖时，默认就是 `0.0`。

**练习 2**：`np.add(m, hi)` 返回 `HighP` 后，`HighP.__array_finalize__` 会被调用吗？如果 `HighP` 重写了它，能拿到什么信息？

**参考答案**：会调用。胜出子类型分配输出后，仍走标准的 `__array_finalize__(obj=...)` 收尾。`obj` 通常是参与运算的某个输入数组（如 `m` 或 `hi`）。`HighP` 可借此从 `obj` 拷贝自定义属性——这与 4.1 的机制完全一致，说明「优先级决定类型，`__array_finalize__` 决定该类型的收尾」是两条互补的管线。

---

## 5. 综合实践

把四个最小模块串起来：实现一个 `RowMatrix` 子类，它（1）用一个**较高**的 `__array_priority__` 压过 `np.matrix`；（2）在 `__array_finalize__` 里复用 `matrix` 的思路把结果补成二维；（3）用 `_getitem` 思路体会索引握手。

任务：

```python
# 示例代码（骨架，需你补全并运行）
import numpy as np
from numpy.matrixlib.defmatrix import matrix

class RowMatrix(np.ndarray):
    __array_priority__ = 50.0   # 任务 1：高于 matrix 的 10.0

    def __array_finalize__(self, obj):
        # 任务 2：模仿 matrix，把结果补成二维
        # 提示：判断 self.ndim，必要时用 self._set_shape(...)
        ...   # 请补全

m = np.asmatrix(np.arange(6).reshape(2, 3))
r = np.asmatrix(np.arange(6).reshape(2, 3)).astype(float)
rm = np.arange(6).reshape(2, 3).view(RowMatrix)

# 验证 1：优先级胜出
out = np.add(m, rm)
assert type(out) is RowMatrix, type(out)

# 验证 2：你的 __array_finalize__ 是否让 RowMatrix 在切片后保持二维？
# （注意：RowMatrix 没有重写 __getitem__，所以切片走的是 ndarray 默认行为 +
#  __array_finalize__；思考它能保证二维吗？）
sub = rm[0]
print("rm[0].shape =", sub.shape, "type =", type(sub).__name__)

# 验证 3：对照 matrix
print(" m[0].shape =", m[0].shape, "type =", type(m[0]).__name__)
```

请你：

1. 补全 `__array_finalize__`（参考 [defmatrix.py:L172-L193](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L172-L193)），让 `rm` 在派生后保持二维。
2. 解释为什么 `rm[0]` 的形状/类型与 `m[0]` **不同**：`RowMatrix` 没有重写 `__getitem__`，也就没有 `_getitem` 握手，`rm[0]` 会走 `ndarray` 默认的 1 维切片，再被你的 `__array_finalize__` 补成 `(1, N)` 行向量——但它无法区分行列。
3. 用一句话总结：要完整复刻 `matrix` 的索引语义，除了 `__array_finalize__` 还必须重写哪个方法？为什么？

预期结果（验证 1）：`type(out) is RowMatrix` 断言通过，证明 `__array_priority__ = 50.0` 在与 `matrix(10.0)` 的混合运算中胜出。`rm[0]` 与 `m[0]` 的具体形状差异**待本地验证**，但理论分析如任务 2 所述。

## 6. 本讲小结

- `__array_finalize__` 是 NumPy 给子类的「每次派生都跑」的收尾钩子；`matrix` 用它把切片/视图/运算后可能掉维的结果**重新补回二维**，是「永远二维」不变量的真正守护者（[defmatrix.py:L172-L193](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L172-L193)）。
- 改形状一律用私有方法 `_set_shape`，而不是 `.shape =`：前者绕开 NumPy 2.5 起的弃用警告，且就地重塑不触发 `__array_finalize__`（[methods.c:L3098-L3101](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/src/multiarray/methods.c#L3098-L3101)、[getset.c:L109-L127](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/src/multiarray/getset.c#L109-L127)）。
- `_getitem` 标志是 `__getitem__` 与 `__array_finalize__` 的握手信号：索引中途挂起该标志，让 `__array_finalize__` 短路，把行/列向量的判定权交还给 `__getitem__`（[defmatrix.py:L172-L175](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L172-L175) 与 [L195-L219](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L195-L219)）。
- `__array_priority__ = 10.0` 让 `matrix` 在与普通 `ndarray`（默认 `0.0`）的二元运算中胜出，结果保持 `matrix`；更高的子类（如 `MaskedArray` 或自定义类）可设更大值压过它（[defmatrix.py:L117-L117](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L117-L117)）。
- 优先级决定「结果的类型」，`__array_finalize__` 决定「该类型的收尾」——两条管线互补，共同支撑 `matrix` 在 numpy 生态中的行为。

## 7. 下一步学习建议

- **下一讲 u2-l5（运算符重载）**：`__array_priority__` 在 `matrix.__mul__` / `__rmul__` 与其它子类混合时同样生效，建议结合本讲阅读运算符重写如何返回 `NotImplemented` 触发反射与优先级裁决。
- **专家层 u3-l1（`__getitem__` 与永远二维）**：本讲只讲了 `_getitem` 握手机制，u3-l1 会把 `__getitem__` 的列向量判定细节讲透。
- **专家层 u3-l5（与 numpy 生态交互）**：`subok`、`nditer` 输出子类型、`MaskedArray` 与 `matrix` 多重继承下的 `__array_finalize__` 链，是本讲优先级与 finalize 机制在更大舞台上的应用。
- **延伸阅读**：NumPy 官方文档「Subclassing ndarray」对 `__array_finalize__` / `__array_wrap__` / `__array_priority__` 三件套有完整论述，可作为本讲的权威补充。

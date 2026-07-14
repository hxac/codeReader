# 视图、拷贝与 record/void dtype 转换

> 本讲属于「专家层」第五单元第一篇。真实实现全部在 [`numpy/_core/records.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py)，`numpy/rec/__init__.py` 只是再导出垫片（详见 u1-l1）。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `arr.view(np.recarray)` 为什么**不拷贝数据**就能让 `arr.x` 这种属性访问生效；
- 解释「二元 dtype」`np.dtype((np.record, descr))` 的含义——它如何在不改变字段结构的前提下，把标量类型从 `numpy.void` 替换成 `numpy.record`；
- 描述 `__array_finalize__` 与 `__setattr__` 是如何分工协作，**在任何视图、切片、赋值之后，都把 `dtype.type` 自动拉回 `record`**；
- 区分 `recarray` 的拷贝语义与 `np.rec.array(..., copy=...)` 中 `copy` 参数的实际效果。

本讲的核心是一条**不变量（invariant）**：

\[
\text{对任何结构化 recarray } r,\quad r.\text{dtype}.\text{type} \equiv \texttt{numpy.record}
\]

整篇讲义就是在回答：NumPy 用哪些机制、在哪些时机，维持这条不变量不被破坏。

## 2. 前置知识

本讲默认你已经读过 u3-l1（`recarray.__new__` 与 `__array_finalize__`）和 u4-l4（`array` 总调度）。回顾三个关键事实：

1. **结构化 dtype 的标量类型是 `void`。** 一个像 `[('x','f8'),('y','i4')]` 这样的 dtype，其 `dtype.type` 是 `numpy.void`（`kind='V'`）。取单条记录 `a[0]` 得到的是 `numpy.void` 标量，只能 `a[0]['x']` 字典访问。参见 [`numerictypes.py` 类型树中的 void(king=V)](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/numerictypes.py#L70-L76)。

2. **`record` 是 `nt.void` 的子类。** [`record(nt.void)`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L196) 在字节布局上与 `void` 完全一致，只是多了属性式取字段与美化打印。把 `dtype.type` 从 `void` 换成 `record`，**不搬动任何内存字节**，只改「取标量时用哪个 Python 类来包装」。

3. **`recarray` 是 `ndarray` 的子类。** 二者内存布局相同，区别只在属性查找走字段（详见 u3-l2）。

一个常被忽略但本讲反复用到的事实：`dtype` 对象**只携带「结构 + 标量类型」两层信息**。`np.dtype((np.record, [('x','f8')]))` 与 `np.dtype([('x','f8')])` 的字段、偏移、itemsize 完全相同，**唯一差别是 `.type`**（前者是 `record`，后者是 `void`）。本讲大量机制都建立在这个「换 type 不换结构」的把戏上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`numpy/_core/records.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py) | 全部实现。本讲聚焦 `recarray.__new__`、`__array_finalize__`、`__getattribute__`、`__setattr__`、`__getitem__`，以及 `record.__getitem__` 与 `array()` 的 `copy` 分支。 |
| [`numpy/_core/numerictypes.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/numerictypes.py) | 定义 `void`（`kind='V'`）在类型树中的位置，是理解「为什么标量类型是 void」的根。 |
| [`numpy/_core/tests/test_records.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py) | `test_recarray_views`、`test_assign_dtype_attribute`、`test_nested_fields_are_records` 三组用例，是本讲所有行为的权威证据。 |

## 4. 核心概念与源码讲解

### 4.1 view(recarray)：不拷贝数据，只改视图类型

#### 4.1.1 概念说明

`ndarray.view(...)` 是 NumPy 的「换视角」操作：它**新建一个数组对象，但复用同一段内存缓冲**，只改变这段字节「被当作什么 dtype / 什么 Python 类型来解释」。因此 `view` 不搬运数据，开销极小。

对结构化数组，最典型的用法是：

```python
a = np.array([(1,'ABC'),(2,'DEF')], dtype=[('foo',int),('bar','S4')])
r = a.view(np.recarray)   # 不拷贝，r 与 a 共享内存
```

`view` 之后 `r` 是 `recarray`、`r.foo` 能用属性访问，而 `a` 的字节一个都没动。这正是 [recarray 文档「方法 1」](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L343-L352) 推荐的转换路径。

#### 4.1.2 核心流程

`arr.view(np.recarray)` 在 C 层做了三件事：

1. 分配一个新的 `recarray` 实例（Python 类型变成 `recarray`）；
2. 让它**共享** `arr` 的数据缓冲、dtype、shape、strides；
3. 调用钩子 `__array_finalize__(self, obj)`，让子类有机会「收尾」。

关键在第 3 步：`arr` 的 `dtype.type` 是 `void`，直接共享会导致新 `recarray` 的标量类型仍是 `void`，属性访问会失效。于是 [`__array_finalize__`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L407-L413) 负责把 `void` 提升回 `record`：

```python
def __array_finalize__(self, obj):
    if (self.dtype.type is not record and
            issubclass(self.dtype.type, nt.void) and
            self.dtype.names is not None):
        ndarray._set_dtype(self, sb.dtype((record, self.dtype)))
```

三个条件**同时**成立才动手（详见 4.3）。`ndarray._set_dtype` 是 C 层提供的「就地替换 dtype」入口；类属性 [`_set_dtype = None`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L405) 是向 C 层声明：「dtype 变化请交给我这个钩子处理，不要走默认路径」。

#### 4.1.3 源码精读

- [`__array_finalize__` 钩子（records.py:407-413）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L407-L413)：视图/切片等「派生操作」的统一收尾点，判定并完成 `void→record` 提升。
- 测试 [`test_recarray_views` 的前两条断言（test_records.py:199-200）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L199-L200)：`a.view(np.recarray).dtype.type == np.record` 且 `type(...) == np.recarray`——这就是 `view(recarray)` 的保证。

#### 4.1.4 代码实践

1. **目标**：确认 `view(recarray)` 不拷贝数据，且把标量类型提升为 `record`；再探究「如何把 record array 转回普通 ndarray」。
2. **操作步骤**：

   ```python
   import numpy as np
   a = np.array([(1, 2.0), (3, 4.0)], dtype=[('x', 'i8'), ('y', 'f8')])
   r = a.view(np.recarray)

   print(type(r).__name__, r.dtype.type)        # recarray  <class 'numpy.record'>
   print(r.x, r.y)                              # 属性访问生效

   # 证明共享内存：改 r 即改 a
   r.x[:] = 100
   print(a['x'])                               # [100 100]，a 被改写 -> 没拷贝
   ```

3. **进阶探究（重点）**：试试 `r.view(np.ndarray)` 能否「转回」普通数组。

   ```python
   back = r.view(np.ndarray)
   print(type(back).__name__, back.dtype.type)  # ndarray  numpy.record  ← 仍是 record！
   ```

4. **预期现象 / 待本地验证**：`back` 的 Python 类型变回了 `ndarray`，但 **`dtype.type` 仍然是 `numpy.record`，并没有变回 `numpy.void`**。原因是 `__array_finalize__` 只做「`void→record`」的单向提升，从不反向降级；而且 `view(np.ndarray)` 产生的是普通 `ndarray`，根本不会再调用 `recarray.__array_finalize__`。

5. **正确的「撤销」写法**（来自测试）：

   ```python
   back2 = r.view(r.dtype.fields or r.dtype, np.ndarray)
   print(back2.dtype.type)                      # numpy.void  ← 这次才真正回到 void
   ```

   这里**显式传入一个不带 record 的 dtype 描述**（结构化时用 `r.dtype.fields`，非结构化时退回 `r.dtype`），把 record 身份剥掉。参见 [`test_recarray_views` 末尾的 recommended undo（test_records.py:240-247）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L240-L247)。

#### 4.1.5 小练习与答案

- **练习 1**：对一个**非结构化**的 `b = np.array([1,2,3,4,5])`，`b.view(np.recarray)` 之后 `dtype.type` 是什么？
  - **答案**：仍是 `np.int64`（或对应整数类型）。`__array_finalize__` 的第三个条件 `self.dtype.names is not None` 不成立（非结构化 dtype 没有 names），所以不会触发任何提升。对应测试 [test_records.py:201-202](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L201-L202)。
- **练习 2**：为什么说 `view(recarray)` 比 `np.rec.fromrecords(...)`「便宜」？
  - **答案**：`view` 只新建数组对象、共享缓冲，零数据搬运；`fromrecords`（尤其不指定 dtype 时）要把数据铺成 object 数组、逐列类型推断、再装填拷贝（见 u4-l1）。

---

### 4.2 二元 dtype (record, descr)：把 void 标量类型替换成 record

#### 4.2.1 概念说明

NumPy 的 `dtype` 构造器支持一种「二元」写法：

```python
np.dtype((scalar_type, base_dtype))
```

它的语义是：**沿用 `base_dtype` 的全部结构（字段、偏移、itemsize、字节序），只把标量类型 `.type` 换成 `scalar_type`**。当 `scalar_type` 是 `np.record`、`base_dtype` 是一个结构化 dtype 时，就得到了一个「字段一模一样、但标量是 record」的 dtype。

这是整条 record array 机制的「原子操作」——`record` 比 `void` 多出的属性访问能力，本质上就是靠这个二元 dtype「贴」上去的。

#### 4.2.2 核心流程

设 `descr = np.dtype([('x','f8'),('y','i4')])`，则：

\[
\text{descr}.\text{type} = \texttt{numpy.void},\quad
\text{np.dtype}((\text{record}, \text{descr})).\text{type} = \texttt{numpy.record}
\]

两者的 `names`、`fields`、`itemsize` 完全相同。把一个底层缓冲用前者解释 → 标量是 `void`；用后者解释 → 标量是 `record`。**内存字节不变，变的只是「取一个元素时用哪个 Python 类包装它」。**

`np.dtype` 对二元形式的解析要求第一个元素是 `np.generic` 的子类（标量类型），第二个是被继承的 dtype。`record` 恰好是 `nt.void` 的子类，与 `void` 结构化 dtype「同构」，因此替换合法。

#### 4.2.3 源码精读

二元 dtype 在 records.py 中至少出现四处，都是「贴 record 身份」的关键点：

- [`recarray.__new__` 两条内存路径都以 `(record, descr)` 调用 `ndarray.__new__`（records.py:396-402）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L396-L402)：无论是新分配还是复用 `buf`，dtype 都直接带上 record，保证「出厂即 record」。
- [`__array_finalize__` 用 `sb.dtype((record, self.dtype))` 提升（records.py:413）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L413)：派生操作后补救。
- [`__setattr__` 给 `dtype` 赋值时用 `sb.dtype((record, val))` 包装（records.py:453-458）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L453-L458)：用户改 dtype 后补救。
- [`fromrecords` 路径 B 用 `sb.dtype((record, dtype))`（records.py:716-717）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L716-L717)：直接构造时贴 record。

测试里也直接演示了这种构造：`recordview = a.view(np.dtype((np.record, a.dtype)))`，结果 [`type == np.ndarray` 但 `dtype.type == np.record`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L183-L184)——说明「record 标量」与「recarray 类型」是**两个相互独立的维度**：你可以拥有一个标量是 record 的普通 ndarray（属性访问在数组级不可用，但标量级 `arr[0].x` 可用）。

#### 4.2.4 代码实践

1. **目标**：亲手构造二元 dtype，对比它与普通结构化 dtype 的异同。
2. **操作步骤**：

   ```python
   import numpy as np
   descr = np.dtype([('x','f8'), ('y','i4')])
   rec_dt = np.dtype((np.record, descr))

   print('descr   .type =', descr.type,    'itemsize =', descr.itemsize)
   print('rec_dt  .type =', rec_dt.type,   'itemsize =', rec_dt.itemsize)
   print('names 相同？', descr.names == rec_dt.names)
   print('fields 相同？', dict(descr.fields) == dict(rec_dt.fields))
   ```

3. **预期结果（待本地验证）**：两者的 `.type` 不同（`void` vs `record`），但 `itemsize`、`names`、`fields` 完全一致。
4. **延伸**：用 `rec_dt` 取一个标量，验证它是 `numpy.record`：

   ```python
   arr = np.zeros(2, dtype=rec_dt)
   print(type(arr[0]).__name__)   # record
   print(arr[0].x)                # 标量级属性访问可用
   ```

#### 4.2.5 小练习与答案

- **练习 1**：`np.dtype((np.record, descr))` 与 `np.dtype((np.void, rec_dt))` 是否互为逆操作？
  - **答案**：在「结构」上是的。后者会把 `.type` 从 `record` 换回 `void`，这正是 [`__repr__` 里把 record 换回 void 以便打印的写法（records.py:515-516）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L515-L516)：`sb.dtype((nt.void, repr_dtype))`。
- **练习 2**：能否写 `np.dtype((np.int64, descr))`？为什么？
  - **答案**：不行。二元 dtype 要求第一个元素是被继承 dtype 标量类型的**子类**；`descr.type` 是 `void`，`int64` 不是 `void` 的子类，构造会报错。这正是为什么只有 `record`（`void` 的子类）能贴上去。

---

### 4.3 __array_finalize__ 与 __setattr__ 的协作：维持 record 身份

#### 4.3.1 概念说明

回到本讲开头的不变量：**结构化 recarray 的 `dtype.type` 必须恒为 `record`**。问题在于，能「派生」出新数组的操作有很多——`view`、切片 `r[1:3]`、取字段 `r['c']`、甚至直接给 `r.dtype = ...` 赋值——其中不少会让标量类型「掉回」`void`。NumPy 用两个钩子分兵把守：

- **`__array_finalize__`**：守住所有「派生新数组」的操作（view / 切片 / 取字段 / ufunc 输出等）；
- **`__setattr__`**：守住「直接给 `dtype` 属性赋值」这一条特殊路径。

两者都用同一个原子操作 `sb.dtype((record, ...))` 把 `void` 贴回 `record`。

#### 4.3.2 核心流程

`__array_finalize__` 的判定是一个**三条件 AND**（任一不满足就不动手）：

1. `self.dtype.type is not record` —— 已经是 record 就别再折腾（避免无限递归）；
2. `issubclass(self.dtype.type, nt.void)` —— 只处理 void 家族（结构化标量）；非结构化的 `int64`/`float64` 等直接放行；
3. `self.dtype.names is not None` —— 必须是「带字段的」结构化 dtype；裸 `void`（如 `'V8'`，一坨 8 字节、无字段）不提升。

三条同时成立 → `ndarray._set_dtype(self, sb.dtype((record, self.dtype)))`。

`__setattr__` 对 `attr == 'dtype'` 单独开了个口子：当新值 `val` 是「带字段的 void 结构化 dtype」时，先把它包成 `(record, val)` 再赋，于是**即便用户故意塞一个 void dtype 进来，结果仍是 record**。

此外，「取字段」返回的子数组也要保持 record 身份。这由 [`__getattribute__` 与 `__getitem__`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L438-L443) 内部用 `obj.view(dtype=(self.dtype.type, obj.dtype))` 完成——又一个二元 dtype 的应用，把 record 身份向下传递给嵌套结构化字段。

#### 4.3.3 源码精读

- [`__array_finalize__`（records.py:407-413）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L407-L413)：派生操作的守门人。
- [`__setattr__` 中 dtype 的自动转换（records.py:449-458）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L449-L458)：注意三条限定 `attr=='dtype' and issubclass(val.type, void) and val.names is not None`，与 `__array_finalize__` 的后两条完全对称——「非 void 结构、子数组、裸 void」都不动。
- [`__getattribute__` 取字段后保持 record（records.py:438-443）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L438-L443)：嵌套结构化字段 `view((self.dtype.type, obj.dtype))`；非结构化字段则 `view(ndarray)` 退回普通数组。
- [`__getitem__`（records.py:486-501）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L486-L501)：切片结果先 `view(type(self))` 保持 recarray 身份，再按 void/非 void 决定是否贴 record。
- 标量侧对称逻辑：[`record.__getitem__`（records.py:251-259）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L251-L259) 用 `obj.view((self.__class__, obj.dtype))` 让 `a[0].bar.A` 这类嵌套访问也拿到 record。

测试佐证：

- `r.view('f8').view('f4,i4')` 绕了一大圈回到结构化，[`dtype.type 仍是 record`（test_records.py:206-208）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L206-L208)——证明 `__array_finalize__` 在每次 view 都补救。
- `r.view('V8')` 视为裸 void（无 names），[`dtype.type 是 void`（test_records.py:237）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L237)——证明第 3 个条件 `names is not None` 的作用。
- 给 `data.dtype` 赋一个 void dtype 后，[`dtype.type 仍是 record`（test_records.py:502-504）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L502-L504)——证明 `__setattr__` 的补救。

#### 4.3.4 代码实践

1. **目标**：复现「给 dtype 赋值后仍是 record」与「裸 void 视图不提升」两条行为。
2. **操作步骤**：

   ```python
   import numpy as np, warnings

   # (A) 直接给 dtype 属性赋一个 void 结构化 dtype
   data = np.zeros(3, np.dtype([('a', np.uint8), ('b', np.uint8)])).view(np.recarray)
   print('before:', data.dtype.type)            # numpy.record
   with warnings.catch_warnings():
       warnings.simplefilter('ignore')          # 赋 dtype 会发 DeprecationWarning
       data.dtype = np.dtype([('a', np.uint8), ('b', np.uint8)])
   print('after :', data.dtype.type)            # numpy.record  ← __setattr__ 救回来了

   # (B) 视为裸 void（无字段名）
   r = np.rec.array(np.ones(4, dtype='i4,i4'))
   print('V8 view:', r.view('V8').dtype.type)   # numpy.void  ← names 为 None，不提升
   ```

3. **预期结果（待本地验证）**：(A) 赋值前后 `dtype.type` 都是 `numpy.record`；(B) `view('V8')` 后是 `numpy.void`。两相对照，正好刻画了 `__array_finalize__`/`__setattr__`「该救才救」的边界。
4. **观察建议**：去掉 `catch_warnings`，你会看到 `DeprecationWarning: Setting the dtype ...`——这提醒「直接给 dtype 赋值」本就是被劝阻的用法，而 `__setattr__` 仍兼容地维持 record 身份。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `r.view('V8')` 不触发 record 提升，而 `r.view('f4,i4')` 会？
  - **答案**：`'V8'` 是无字段名的裸 void（`names is None`），`__array_finalize__` 第 3 条件不满足；`'f4,i4'` 是带字段的结构化 dtype，三条件全满足，故提升。对应 [test_records.py:235-237](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L235-L237)。
- **练习 2**：`r.view(('i8','i4,i4'))` 的 `dtype.type` 是什么？为什么？
  - **答案**：是 `np.int64`。这里二元 dtype 的标量类型指定为 `i8`（非 void），第 2 条件 `issubclass(..., void)` 不成立，不提升。对应 [test_records.py:238](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L238)。

---

### 4.4 copy 语义：recarray 的拷贝与 array() 的 copy 参数

#### 4.4.1 概念说明

「视图」共享内存，「拷贝」独立内存。对 recarray，拷贝有两层问题：

1. **`.copy()` 是否保留 record 身份？** —— 是。`.copy()` 复制数据并保留子类，新数组的 `dtype.type` 仍是 record（拷贝不会把 type 掉回 void，且即便掉回，`__array_finalize__` 也会补）。
2. **`np.rec.array(obj, copy=...)` 的 `copy` 参数到底控什么？** —— 它**只对 `obj` 是 `ndarray`/`recarray` 时生效**，决定是否复制底层数据；对 `bytes`/`list`/文件等输入无效。

#### 4.4.2 核心流程

`array()` 的两条相关分支（[`records.py:1061-1080`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1061-L1080)）：

```
obj 是 recarray:
    若指定了与原 dtype 不同的 dtype -> 先 view(dtype)
    若 copy=True -> new.copy()   ← 这里才真正复制数据
    返回 new（recarray）

obj 是 ndarray:
    若指定了不同的 dtype -> 先 view(dtype)
    若 copy=True -> new.copy()
    最后 new.view(recarray)       ← 末尾一次性贴 recarray 身份
```

要点：

- **`copy=False`**：不复制数据，结果与输入**共享内存**（对 ndarray 分支，是原数组的 recarray 视图；改字段会写回原数组）。
- **`copy=True`（默认）**：先复制成独立缓冲，再贴 recarray 身份；修改结果不影响原数组。
- 两条分支末尾都对 recarray 身份有保证：recarray 分支天然是 recarray；ndarray 分支靠最后的 `.view(recarray)`（由 `__array_finalize__` 补 record）。

#### 4.4.3 源码精读

- [`array()` 的 recarray 分支（records.py:1061-1068）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1061-L1068)：`copy` 仅决定是否 `.copy()`。
- [`array()` 的 ndarray 分支（records.py:1073-1080）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1073-L1080)：注意最后的 `return new.view(recarray)`——这是普通 ndarray 变身 recarray 的统一出口。
- 文档对 `copy` 的限定（[`records.py:970-973`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L970-L973)）：「This option only applies when the input is an ndarray or recarray.」

#### 4.4.4 代码实践

1. **目标**：对比 `copy=True` 与 `copy=False` 的内存共享行为。
2. **操作步骤**：

   ```python
   import numpy as np
   base = np.rec.array(np.ones(3, dtype='i4,i4'))

   ct = np.rec.array(base, copy=True)    # 默认就是 True
   cf = np.rec.array(base, copy=False)

   ct.x[:] = 9
   print('copy=True  后 base.x =', base.x)   # [1 1 1]，未受影响

   cf.x[:] = 7
   print('copy=False 后 base.x =', base.x)   # [7 7 7]，被写回
   ```

3. **预期结果（待本地验证）**：`copy=True` 的修改不波及 `base`；`copy=False` 的修改写回 `base`。两者 `type` 都是 `recarray`、`dtype.type` 都是 `record`。
4. **进阶**：把一个**普通 ndarray** 传进去，验证末尾 `.view(recarray)` 的效果：

   ```python
   plain = np.array([(1,2),(3,4)], dtype=[('x','i4'),('y','i4')])
   rec = np.rec.array(plain, copy=False)
   print(type(rec).__name__, rec.dtype.type)  # recarray  numpy.record
   ```

#### 4.4.5 小练习与答案

- **练习 1**：`np.rec.array(some_bytes, copy=False)` 会因为 `copy=False` 而省内存吗？
  - **答案**：`copy` 对 `bytes` 输入**无效**。`bytes` 走的是 [`fromstring` 分支（records.py:1052-1053）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1052-L1053)，那里本来就通过 `buf=` 共享缓冲（见 u4-l2），与 `copy` 参数无关。
- **练习 2**：为什么 ndarray 分支末尾非要再 `new.view(recarray)` 一次，而不能直接返回 `new`？
  - **答案**：`new` 此刻可能是个普通 ndarray（`copy()` 出来的就是 ndarray），不具备属性访问能力；末尾的 `.view(recarray)` 才把 Python 类型升成 recarray，并由 `__array_finalize__` 把标量类型补成 record。

## 5. 综合实践

把本讲四个模块串起来，完成一个「身份追踪」小实验：对同一个结构化数组，分别用 `view`、`np.rec.array`、二元 dtype 三条路径制造 record array，再依次做 view→切片→改 dtype→拷贝，沿途**打印每一步的 `type(arr)` 与 `arr.dtype.type`**，验证「record 身份」是否始终稳定。

```python
import numpy as np, warnings

def show(tag, arr):
    print(f"{tag:28s} type={type(arr).__name__:9s} dtype.type={arr.dtype.type.__name__}")

a = np.array([(1,2.0),(3,4.0)], dtype=[('x','i4'),('y','f8')])
show("原始 ndarray", a)                              # ndarray / void

r1 = a.view(np.recarray)                            # 路径1: view
r2 = np.rec.array(a)                                # 路径2: array(默认 copy=True)
r3 = a.view(np.dtype((np.record, a.dtype)))         # 路径3: 二元 dtype
show("view(recarray)", r1)                           # recarray / record
show("rec.array(a)", r2)                             # recarray / record
show("view((record,dt))", r3)                        # ndarray  / record  ← 注意类型是 ndarray

# 沿途扰动 r1
show("r1[1:]", r1[1:])                              # 切片仍是 recarray/record
show("r1.view('f4,i4')", r1.view('f4,i4'))          # 换结构仍 record
show("r1.view('V8')", r1.view('V8'))                # 裸 void -> void（例外）

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    r1.dtype = np.dtype([('x','i4'),('y','f8')])     # 改 dtype
show("after r1.dtype=...", r1)                       # 仍是 record（__setattr__ 救场）

c = r1.copy()
show("r1.copy()", c)                                # 拷贝仍是 recarray/record
```

**关注点**：

1. 三条路径都能得到 `dtype.type == record`，但 `r3` 的 Python 类型是 `ndarray` 而非 `recarray`——印证「标量 record」与「recarray 类型」是两个独立维度；
2. 切片、换结构、改 dtype、拷贝之后，只要仍是「带字段的 void 结构」，`dtype.type` 都被自动拉回 record；唯独 `view('V8')` 因为丢失字段名而退回 void；
3. 修改 `r2`（`copy=True`）不应影响 `a`，而 `r1 = a.view(recarray)` 与 `a` 共享内存。

> 说明：以上代码片段为「示例代码」，便于你理解调用链；运行具体输出请以本地环境为准（待本地验证）。

## 6. 本讲小结

- `arr.view(np.recarray)` **不拷贝数据**，只换 Python 类型与（经 `__array_finalize__`）标量类型；它与原数组共享内存。
- 「二元 dtype」`np.dtype((np.record, descr))` 是 record 机制的原子操作：**保留字段结构，只把 `.type` 从 `void` 换成 `record`**，不搬动任何字节。
- `__array_finalize__` 守「派生操作」（view/切片/取字段），`__setattr__` 守「给 dtype 赋值」，两者用同一个 `(record, ...)` 把 `void` 贴回 `record`，共同维持「结构化 recarray 的 `dtype.type` 恒为 record」这条不变量。
- 该提升是**有条件且单向**的：必须是「带字段的 void」才提升（裸 `void` 如 `'V8'`、非 void 标量如 `int64` 不动），且只升不降——`view(np.ndarray)` 不会把 record 还原成 void，要还原必须显式传入非 record 的 dtype。
- `np.rec.array(..., copy=...)` 的 `copy` 只对 `ndarray`/`recarray` 输入生效：`True` 复制数据、`False` 共享内存；ndarray 分支靠末尾 `.view(recarray)` 贴身份。

## 7. 下一步学习建议

- 下一篇 **u5-l2（`__repr__` 打印格式与 legacy 打印模式）** 会用到本讲的「二元 dtype 逆操作」`sb.dtype((nt.void, repr_dtype))`——打印时要把 record 换回 void 才不在 dtype 里显示 `numpy.record`，正好是 4.2 练习 1 的延伸。
- 想巩固「字段取值保持 record」的细节，可回看 u3-l2（`__getattribute__`/`__setattr__`）与本讲 4.3 的对照。
- 若想验证本讲所有行为，最直接的方式是阅读并运行 [`test_records.py` 的 `test_recarray_views`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L187-L247)，它是本讲事实来源；u5-l3 会系统讲解这套测试套件。

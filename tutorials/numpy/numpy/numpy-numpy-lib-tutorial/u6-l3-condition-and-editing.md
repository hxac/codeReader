# 条件选择与数组编辑：select/piecewise/extract/place/copy 等

## 1. 本讲目标

本讲集中精读 `numpy/lib/_function_base_impl.py` 中的一组「条件选择与数组编辑」函数。学完后你应该能够：

- 用 `select` / `piecewise` 实现「按条件优先级取值」与「分段函数」；
- 区分 `extract`（掩码→一维数组）与 `place`（掩码+值→就地改写）这对互逆操作；
- 说清 `copy` 的 `order` 参数、`average` 的加权公式与 `asarray_chkfinite` 的有限性检查；
- 理解 `append` / `delete` / `insert` 都是「分配新数组再搬运」的非就地操作，以及 `insert` 多点插入时的下标位移修正；
- 用 `meshgrid` 生成 `xy` / `ij` 两种坐标网格，用 `digitize` 做分箱，并理解 `digitize` 对 `searchsorted` 的封装；
- 知道 `rot90` 返回视图、`trim_zeros` 用 `argwhere` 找边界、`sort_complex` 强制复数输出。

这些函数看似零散，但共享同一种工程范式：**先用 `asarray`/`asanyarray` 把输入规整成数组，再用布尔掩码或下标搬运数据，最后（多数情况）返回一个新数组**。抓住这条主线，16 个函数就能串成一张网。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **布尔掩码（boolean mask）**：一个与目标数组同形的布尔数组，`arr[mask]` 会取出 `True` 位置的元素，得到一维结果。
- **视图（view）与拷贝（copy）**：切片、`flip`、`transpose` 通常返回指向同一块内存的视图（零拷贝）；而 `copy`、`insert`、`delete` 会分配新内存。本讲的 u5-l1 已深入讨论过步长与视图。
- **NEP-18 `__array_function__` 与 dispatcher+impl 双函数写法**：本讲所有带 `@array_function_dispatch(...)` 装饰的函数都遵循这套模式（详见 u1-l2）。`_xxx_dispatcher` 只负责把「参与运算的数组参数」yield 出去供派发，真正的逻辑在 impl 函数体里。
- **广播（broadcasting）**：`select` / `average` 内部都用广播对齐形状（详见 u5-l2）。
- **C 序与 F 序**：行主序（C，默认）与列主序（F）。`copy` 的 `order='K'` 表示「尽量保持原布局」。

> 约定：本讲出现的 `_nx` 是文件顶部 `import numpy._core.numeric as _nx`（[_function_base_impl.py:9](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L9)）的别名，`_nx.asarray`、`_nx.take`、`_nx.searchsorted` 等都是 core 层的同一批函数。

## 3. 本讲源码地图

本讲只涉及一个源文件，但其中函数众多，按下表分组定位：

| 分组 | 函数 | 行号区间 | 作用 |
| --- | --- | --- | --- |
| 条件选择 | `select` | 824–928 | 多条件优先级取值，带 `default` |
| 条件选择 | `piecewise` | 697–815 | 按条件分段求值（可传函数） |
| 掩码读写 | `extract` | 2055–2104 | 掩码→一维数组（取出） |
| 掩码读写 | `place` | 2112–2149 | 掩码+值→就地改写（写入） |
| 复制/统计 | `copy` | 936–1003 | 数组深拷贝，可控内存布局 |
| 复制/统计 | `average` | 451–611 | 加权平均，含 `_weights_are_valid` |
| 复制/统计 | `asarray_chkfinite` | 615–686 | 转数组并拒绝 NaN/Inf |
| 复制/统计 | `iterable` | 375–417 | 判断对象是否可迭代 |
| 增删改 | `append` | 5599–5665 | 末尾追加（`concatenate` 封装） |
| 增删改 | `delete` | 5218–5394 | 按下标/掩码删除 |
| 增删改 | `insert` | 5402–5591 | 按下标插入，含多点位移修正 |
| 网格/分箱 | `meshgrid` | 5060–5210 | 坐标网格（`xy`/`ij`） |
| 网格/分箱 | `digitize` | 5673–5781 | 分箱下标（封装 `searchsorted`） |
| 网格/分箱 | `sort_complex` | 1871–1905 | 复数字典序排序 |
| 翻转/修剪 | `rot90` | 180–276 | 平面内旋转 90°（返回视图） |
| 翻转/修剪 | `trim_zeros` | 1952–2047 | 裁掉全零边缘 |

此外会顺带引用两个内部助手：`_weights_are_valid`（[420–442](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L420-L442)）与 `_arg_trim_zeros`（[1908–1944](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1908-L1944)）。所有函数都登记在该文件的 `__all__` 中（[_function_base_impl.py:66-75](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L66-L75)），并由顶层 `numpy/__init__.py` 直接挂到 `np.` 命名空间。

---

## 4. 核心概念与源码讲解

### 4.1 条件选择：select 与 piecewise

#### 4.1.1 概念说明

`select` 与 `piecewise` 都解决「不同位置用不同值/不同函数」的问题，但侧重不同：

- **`select(condlist, choicelist, default=0)`**：你**已经算好**了每个候选值数组 `choicelist`，只需按条件挑。当多个条件同时为真时，**`condlist` 中靠前的条件优先**；全不命中时取 `default`。
- **`piecewise(x, condlist, funclist)`**：你给的是**函数**（或标量），它会在满足条件的子集上**调用**对应函数。若 `funclist` 比条件多一个，多出的那个是「否则」分支。

#### 4.1.2 核心流程

`select` 的关键技巧是「**反向烙印**」：

```
1. 校验 condlist 与 choicelist 等长，且非空
2. 用 result_type 推导统一 dtype（NEP 50 下手工保留 Python int/float/complex）
3. 广播 condlist 与 choicelist 到公共形状
4. result = full(shape, default, dtype)        # 先铺满 default
5. 把 choice 反向逐个 copyto(result, choice, where=cond)
   → 后面的先写，前面的后写并覆盖 → 靠前的条件最终胜出
```

`piecewise` 的流程更直白：

```
1. 若函数数 == 条件数+1，用 ~any(conditions) 构造「否则」条件并拼到末尾
2. y = zeros_like(x)
3. 逐对 (cond, func)：不可调用则 y[cond]=标量；可调用则 y[cond]=func(x[cond])
```

#### 4.1.3 源码精读

`select` 的优先级靠反向遍历实现（[_function_base_impl.py:918-926](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L918-L926)）：

```python
result = np.full(result_shape, choicelist[-1], dtype)
# 反向遍历：choicelist[-2::-1] 跳过末尾的 default，倒着取
choicelist = choicelist[-2::-1]
condlist = condlist[::-1]
for choice, cond in zip(choicelist, condlist):
    np.copyto(result, choice, where=cond)
```

这里 `choicelist[-1]` 是前面 `append(default)` 后的最后一个元素（即 `default`），所以 `np.full` 一开始就把整个结果铺成 `default`；随后倒序用 `copyto` 覆盖，**最后一个条件先写、第一个条件最后写**，从而保证 `condlist` 靠前的优先级最高。

NEP 50 下的 dtype 推导有一段刻意为之的处理（[_function_base_impl.py:888-892](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L888-L892)）：只有当 choice 不是 Python 原生 `int/float/complex` 时才 `np.asarray`，目的是让纯 Python 标量参与 `result_type` 时保留「弱类型」语义。此外条件必须是真正的布尔数组，否则抛 `TypeError`（[_function_base_impl.py:907-910](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L907-L910)）——这正是测试 `test_non_bool_deprecation` 验证的行为。

`piecewise` 的「否则」分支构造（[_function_base_impl.py:797-800](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L797-L800)）：

```python
if n == n2 - 1:  # 函数比条件多一个 → 多出的是 otherwise
    condelse = ~np.any(condlist, axis=0, keepdims=True)
    condlist = np.concatenate([condlist, condelse], axis=0)
```

随后对每个条件做掩码赋值（[_function_base_impl.py:807-813](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L807-L813)）：标量直接填，函数则在 `x[cond]` 子集上求值再填回。

> 完整函数体：[`select` _function_base_impl.py:824-928](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L824-L928)、[`piecewise` _function_base_impl.py:697-815](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L697-L815)。

#### 4.1.4 代码实践

**目标**：用 `select` 实现一个分段函数，并体会「靠前优先」与 `default`。

**步骤**：

```python
import numpy as np
x = np.arange(6)                       # [0,1,2,3,4,5]
condlist  = [x < 3, x > 3]             # 两段条件
choicelist = [-x, x**2]                # 对应取值
print(np.select(condlist, choicelist, 42))
```

**预期输出**（根据源码逻辑推导，建议本地运行核对）：

```
[ 0, -1, -2, 42, 16, 25]
```

- `x=0,1,2`：命中 `x<3`，取 `-x` → `0,-1,-2`；
- `x=3`：两条件都不命中 → `default=42`；
- `x=4,5`：命中 `x>3`，取 `x**2` → `16,25`。

**观察现象**：把 `condlist` 两个条件改成有重叠（例如 `[x<=4, x>3]`），观察 `x=4` 处取的是哪一个——应为第一个条件 `x<=4` 的值，验证「靠前优先」。

#### 4.1.5 小练习与答案

**练习 1**：`np.select([], [])` 会发生什么？为什么？

**答案**：抛 `ValueError("select with an empty condition list is not possible")`。见 [_function_base_impl.py:882-883](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L882-L883)。早期版本允许空列表（返回空），现已废弃并直接报错。

**练习 2**：用 `piecewise` 实现符号函数 `sign(x)`（负→-1，正→+1，零→0）。

**答案**：`np.piecewise(x, [x<0, x>0, x==0], [-1, 1, 0])`。这里函数数（3）== 条件数（3），没有「否则」分支；若写成两条件 `[x<0, x>=0]` 配两函数 `[-1, 1]`，则 `x==0` 处取 `+1`。

---

### 4.2 掩码读写：extract 与 place

#### 4.2.1 概念说明

`extract` 与 `place` 是一对**互逆**的掩码操作（二者文档互相点名 "does the exact opposite"）：

- **`extract(condition, arr)`**：把 `arr` 中 `condition` 为真的元素**抽出来**，返回一维数组。等价于 `arr[condition]`（当 condition 为布尔时）。
- **`place(arr, mask, vals)`**：把 `vals` 的元素**塞进** `arr` 中 `mask` 为真的位置，**就地修改** `arr`（返回 `None`）。

#### 4.2.2 核心流程

`extract` 的实现只有一行思路：

```
take(ravel(arr), nonzero(ravel(condition))[0])
# 即：把两个数组都拉平，找到 condition 的真值下标，从 arr 里 take 出来
```

`place` 委托给 C 函数 `_place`，其语义与 `copyto` 有微妙差别：

| 函数 | 用 `vals` 的哪些元素 | 是否要求 `vals` 长度 ≥ 真值数 |
| --- | --- | --- |
| `np.copyto(arr, vals, where=mask)` | 用 `vals` 中**与 mask 同位置**的元素 | 是（按位置对齐） |
| `np.place(arr, mask, vals)` | 用 `vals` 的**前 N 个**元素（N=mask 真值数），不够则**循环复用** | 否（可循环） |

#### 4.2.3 源码精读

`extract`（[_function_base_impl.py:2104](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2104)）：

```python
return _nx.take(ravel(arr), nonzero(ravel(condition))[0])
```

`ravel` 把任意形状拉平成 1D，`nonzero(...)[0]` 取一维真值下标，`take` 按下标取值。注意它对 `condition` **不强制布尔**——任何「非零即真」的数组都行，这与 `arr[condition]` 的语义一致。

`place`（[_function_base_impl.py:2149](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2149)）只是个薄封装：

```python
return _place(arr, mask, vals)
```

`_place` 是 C 实现，从 `numpy._core.multiarray` 导入（[_function_base_impl.py:13-20](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L13-L20)，第 15 行 `_place,`）。其「循环复用」语义可由测试 `test_place` 印证：`place(a, [1,0,1,0,1,0,1], [8,9])` 把 7 个真位用 `[8,9]` 循环填成 `[8,2,9,4,8,6,9]`（见 [test_function_base.py:1561-1562](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_function_base.py#L1561-L1562)）。

#### 4.2.4 代码实践

**目标**：体验 `extract` / `place` 的互逆往返。

**步骤**：

```python
import numpy as np
a = np.array([1, 4, 3, 2, 5, 8, 7])
mask = a > 4                # [F,F,F,F,T,T,T]
c = np.extract(mask, a)     # 取出 >4 的元素 → [5,8,7]
print("extract:", c)

b = a.copy()
np.place(b, mask, 0)        # 把 >4 的位置就地改成 0
print("after place:", b)
```

**预期输出**：

```
extract: [5 8 7]
after place: [1 4 3 2 0 0 0]
```

**观察现象**：测试 `test_both`（[test_function_base.py:1571-1578](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_function_base.py#L1571-L1578)）展示了一个完整往返：先用 `extract` 把大于 0.5 的元素存到 `c`，再用 `place` 把这些位置清零，最后用 `place(a, mask, c)` 原样写回——结果与原始数组完全一致。你可以照此验证「取出→清零→写回」无损。

#### 4.2.5 小练习与答案

**练习 1**：`np.place([1,2,3], [True,False], [0,1])`（注意第一个参数是 list）会怎样？

**答案**：抛 `TypeError`。见 [test_function_base.py:1552](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_function_base.py#L1552)。`place` 要求第一个参数是 `np.ndarray`（它就地修改，list 无法承接），不像 `extract` 会自动转换。

**练习 2**：`place` 在 `vals` 长度小于真值数时会循环复用；那 `copyto(..., where=mask)` 会吗？

**答案**：不会。`copyto` 按**位置对齐**取 `vals`，要求形状匹配；`place` 按**前 N 个 + 循环**取 `vals`。这是二者最核心的差异。

---

### 4.3 数组复制与数值统计：copy、average、asarray_chkfinite、iterable

#### 4.3.1 概念说明

这一组是「工具型」函数：

- **`copy(a, order='K', subok=False)`**：返回 `a` 的深拷贝。注意它的默认 `order='K'`（保持布局），与 `ndarray.copy()` 的默认 `order='C'` **不同**。
- **`average(a, axis, weights, returned, keepdims)`**：加权平均。无权重时退化为 `mean`；有权重时按 \(\bar{x}=\sum a_i w_i / \sum w_i\) 计算。
- **`asarray_chkfinite(a, dtype, order)`**：转数组，但若结果含 NaN/Inf 则抛 `ValueError`——常用于从不可信输入（如文件读取）构造数组前的安全检查。
- **`iterable(y)`**：判断对象是否可迭代。对 0 维数组返回 `False`（与 `collections.abc.Iterable` 不同）。

#### 4.3.2 核心流程

加权平均公式：

\[
\bar{x} = \frac{\sum_{i} a_i\, w_i}{\sum_{i} w_i}
\]

`average` 的流程：

```
1. a = asanyarray(a)；规整 axis
2. weights 为 None：avg = a.mean(axis)；scl = a.size / avg.size  （即元素计数）
3. weights 非 None：
   a. wgt = _weights_are_valid(...)   （校验形状或按轴广播）
   b. result_dtype = result_type(a, wgt [, 'f8'])   （整数/布尔强制升 float64）
   c. scl = wgt.sum(axis)；若 scl 含 0 → ZeroDivisionError
   d. avg = (a*wgt).sum(axis) / scl
4. returned=True 时返回 (avg, scl) 元组
```

#### 4.3.3 源码精读

`copy` 只有一行（[_function_base_impl.py:1003](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1003)）：

```python
return array(a, order=order, subok=subok, copy=True)
```

它额外清掉 `WRITEABLE=False` 标志（文档注明），因此对只读数组做 `copy` 后可写。

`average` 的核心分支（[_function_base_impl.py:586-604](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L586-L604)）：

```python
if weights is None:
    avg = a.mean(axis, **keepdims_kw)
    scl = avg_as_array.dtype.type(a.size / avg_as_array.size)
else:
    wgt = _weights_are_valid(weights=weights, a=a, axis=axis)
    if issubclass(a.dtype.type, (np.integer, np.bool)):
        result_dtype = np.result_type(a.dtype, wgt.dtype, 'f8')   # 强制升精度
    ...
    scl = wgt.sum(axis=axis, dtype=result_dtype, **keepdims_kw)
    if np.any(scl == 0.0):
        raise ZeroDivisionError("Weights sum to zero, can't be normalized")
    avg = np.multiply(a, wgt, dtype=result_dtype).sum(axis, ...) / scl
```

权重校验 `_weights_are_valid`（[_function_base_impl.py:420-442](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L420-L442)）允许两种合法形状：与 `a` 完全同形（`axis=None`），或沿指定轴的形状匹配（随后用 `transpose`+`reshape` 广播到各轴）。

`asarray_chkfinite`（[_function_base_impl.py:682-686](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L682-L686)）：

```python
a = asarray(a, dtype=dtype, order=order)
if a.dtype.char in typecodes['AllFloat'] and not np.isfinite(a).all():
    raise ValueError("array must not contain infs or NaNs")
return a
```

注意检查只对浮点类型生效（`typecodes['AllFloat']`），整数数组天然不含 NaN/Inf，直接放行。

`iterable`（[_function_base_impl.py:413-417](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L413-L417)）用「试错法」：

```python
try:
    iter(y)
except TypeError:
    return False
return True
```

0 维数组 `np.array(1.0)` 调 `iter()` 会抛 `TypeError`（"iteration over a 0-d array"），所以 `np.iterable` 返回 `False`，而 `isinstance(..., Iterable)` 却是 `True`——这是文档特意点名的差异。

> 完整函数体：[`copy`:936-1003](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L936-L1003)、[`average`:451-611](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L451-L611)、[`asarray_chkfinite`:615-686](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L615-L686)、[`iterable`:375-417](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L375-L417)。

#### 4.3.4 代码实践

**目标**：对比 `average` 的加权与无权重，并体验 `asarray_chkfinite` 的拦截。

**步骤**：

```python
import numpy as np
data = np.arange(1, 11)                      # 1..10
w    = np.arange(10, 0, -1)                  # 10..1
print("mean   :", np.average(data))          # 无权重 = 5.5
print("weighted:", np.average(data, weights=w))   # 加权 → 4.0
print("returned:", np.average(data, weights=w, returned=True))

try:
    np.asarray_chkfinite([1, 2, np.inf])
except ValueError as e:
    print("caught:", e)
```

**预期输出**：

```
mean   : 5.5
weighted: 4.0
returned: (4.0, 55.0)
caught: array must not contain infs or NaNs
```

加权 4.0 的来历：\(\sum i\cdot w_i = 1\cdot10+2\cdot9+\dots+10\cdot1 = 220\)，\(\sum w_i = 55\)，\(220/55 = 4.0\)。`returned=True` 时第二项是权重和 55.0。

**观察现象**：构造 `weights=[1,1,1,0,0]` 对一个长度 5 的数组沿全部元素求 `average`，会得到 `ZeroDivisionError`（因为整组权重和为 0），对应源码 [599-601](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L599-L601) 行的检查。

#### 4.3.5 小练习与答案

**练习 1**：`np.copy(a, order='K')` 与 `a.copy()`（默认 `order='C'`）对一个 F 序连续数组有何不同？

**答案**：`order='K'` 尽量保持原布局，因此对 F 序数组拷贝后仍是 F 序；`a.copy()` 默认 `order='C'`，会重排成 C 序。可分别检查 `b.flags['F_CONTIGUOUS']` 验证。

**练习 2**：为什么 `asarray_chkfinite` 只对 `AllFloat` 类型检查 NaN/Inf？

**答案**：整数类型无法表示 NaN/Inf（它们是 IEEE 754 浮点的特殊值），检查毫无意义且浪费；故只对浮点类型（含半精度/单/双/扩展）做 `isfinite` 校验。

**练习 3**：`np.iterable(np.array(1.0))` 返回什么？为什么和 `isinstance(np.array(1.0), Iterable)` 不同？

**答案**：返回 `False`。因为 `iter(np.array(1.0))` 抛 `TypeError`（0 维数组不可迭代）；而 0 维数组注册了 `__iter__`，所以 `isinstance(..., Iterable)` 是 `True`。`np.iterable` 用「真试 iter」而非「查协议」，因而给出更贴近直觉的答案。

---

### 4.4 数组增删改：append、delete、insert

#### 4.4.1 概念说明

这三个函数都**返回新数组**（非就地），分别对应「末尾追加」「按下标删除」「按下标插入」：

- **`append(arr, values, axis=None)`**：最简单，本质是 `concatenate`。`axis=None` 时两边都拉平。
- **`delete(arr, obj, axis=None)`**：`obj` 可以是整数、整数序列、切片或布尔掩码。
- **`insert(arr, obj, values, axis=None)`**：在 `obj` 指定的位置**之前**插入。支持多点插入，且需处理「每插一次、后续下标整体右移」的位移。

> 设计提示：`delete` 的文档 Notes 明确建议——若要反复删除，**用布尔掩码 `arr[mask]` 更高效**，因为 `delete` 每次都分配新数组。

#### 4.4.2 核心流程

`append`：

```
axis=None → arr.ravel() 与 values.ravel() 拼接
否则      → concatenate((arr, values), axis)
```

`delete`（按 `obj` 类型分三条路）：

```
1. axis=None → ravel
2. obj 是 slice：用 obj.indices(N) 算出要删的下标集合，分配新数组，分块搬运「前段/后段/中间跳过」
3. obj 是单个 int：边界检查后，切两半拼接（跳过该下标）
4. obj 是数组：
   - 布尔 → keep = ~obj              （掩码取反）
   - 整数 → keep = ones(N, bool); keep[obj] = False
   - new = arr[keep]                 （高级索引，自动拷贝）
```

`insert` 的难点是**多点插入的下标位移**。例如在下标 `[2,2]` 都插入，第二个「下标 2」实际指的是原数组位移后的位置。源码用稳定排序 + 累加修正：

```
1. 把 obj 归一为 indices（slice→arange；bool→flatnonzero）
2. 单点(size==1)：切三段 [前 | values | 后] 拼接
3. 多点：
   order = indices.argsort(kind='mergesort')      # 稳定排序
   indices[order] += np.arange(numnew)            # 关键：补偿位移
   old_mask = ones(newlen, bool); old_mask[indices] = False
   new[indices] = values; new[old_mask] = arr     # 一次性散射
```

#### 4.4.3 源码精读

`append` 全貌（[_function_base_impl.py:5659-5665](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5659-L5665)）：

```python
arr = asanyarray(arr)
if axis is None:
    if arr.ndim != 1:
        arr = arr.ravel()
    values = ravel(values)
    axis = arr.ndim - 1
return concatenate((arr, values), axis=axis)
```

注意一个 dtype 陷阱（文档示例）：`np.append([1,2], [])` 会得到 `float64` 的 `[1., 2.]`，因为空数组默认 `float64`，`concatenate` 做了类型提升。

`delete` 的掩码分支最简洁（[_function_base_impl.py:5378-5392](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5378-L5392)）：

```python
if obj.dtype == bool:
    if obj.shape != (N,):
        raise ValueError(...)
    keep = ~obj            # 布尔：直接取反
else:
    keep = ones(N, dtype=bool)
    keep[obj,] = False     # 整数：把要删的置 False
slobj[axis] = keep
new = arr[tuple(slobj)]    # 高级索引 → 新数组
```

`insert` 多点位移的关键两行（[_function_base_impl.py:5577-5578](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5577-L5578)）：

```python
order = indices.argsort(kind='mergesort')   # 稳定排序
indices[order] += np.arange(numnew)         # 每个插入点向后挪
```

`arange(numnew)` 给排好序的每个插入点依次加 0,1,2,…，正好补偿「前面已插入的元素个数」。随后用一个布尔 `old_mask` 把「新值」与「旧值」分别散射到新数组的对应位置（[_function_base_impl.py:5581-5589](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5581-L5589)）。

> 完整函数体：[`append`:5599-5665](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5599-L5665)、[`delete`:5218-5394](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5218-L5394)、[`insert`:5402-5591](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5402-L5591)。

#### 4.4.4 代码实践

**目标**：验证 `insert` 的多点位移行为，对比 `obj=1` 与 `obj=[1]` 的差异。

**步骤**：

```python
import numpy as np
b = np.arange(6)                    # [0,1,2,3,4,5]
print(np.insert(b, [2, 2], [6, 7])) # 在下标 2 前插两次 → [0,1,6,7,2,3,4,5]

a = np.arange(6).reshape(3, 2)
print(np.insert(a, 1, 6, axis=1))   # 标量 obj：每行同一位置插 6
print(np.insert(a, [1], [[7],[8],[9]], axis=1))  # 序列 obj：广播插入
```

**预期输出**（根据源码推导，建议本地核对）：

```
[0 1 6 7 2 3 4 5]
[[0 6 1]
 [2 6 3]
 [4 6 5]]
[[0 7 1]
 [2 8 3]
 [4 9 5]]
```

**观察现象**：`obj=1`（标量）与 `obj=[1]`（单元素序列）结果相同（见 [test_function_base.py:5476-5478](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_function_base.py#L5476-L5478) 的 `np.array_equal` 断言）；但 `obj=1` 配多维 `values` 时会广播成多列（`[[7],[8],[9]]` → `[7,8,9]` 三列），而 `obj=[1]` 则把整个 `values` 视为一次插入。这正是文档 Notes 强调的「basic vs advanced indexing」差异。

#### 4.4.5 小练习与答案

**练习 1**：`np.delete(arr, [0,2,4], axis=0)` 与布尔掩码 `arr[mask]`（`mask[[0,2,4]]=False`）结果是否相同？哪个更快？

**答案**：结果相同。`delete` 内部就是构造 `keep` 掩码再高级索引（见 [5388-5392](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5388-L5392)）。但直接用 `arr[mask]` 省去了 `delete` 的输入转换与轴规整开销，且掩码可复用，文档建议频繁删除时优先掩码。

**练习 2**：`np.append([1,2,3], [])` 的 dtype 是什么？为什么？

**答案**：`float64`。空列表 `[]` 转成默认 `float64` 数组，`concatenate` 把 `int` 提升为 `float64`。见文档示例 [5648-5653](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5648-L5653)。

---

### 4.5 坐标网格与分箱：meshgrid、digitize、sort_complex

#### 4.5.1 概念说明

- **`meshgrid(x1,...,xn, indexing='xy', sparse=False, copy=True)`**：由一组一维坐标向量生成 N 维坐标矩阵，用于在网格上向量化求值。`indexing='xy'`（笛卡尔，默认）与 `'ij'`（矩阵）决定输出形状的轴顺序。
- **`digitize(x, bins, right=False)`**：把 `x` 中每个值归入 `bins` 的某个区间，返回区间下标。它是对 `searchsorted`（二分查找）的封装。
- **`sort_complex(a)`**：按「先实部、后虚部」排序，**总是返回复数 dtype**（即使输入是实数）。

#### 4.5.2 核心流程

`meshgrid` 的思路是「**先各自升维成稀疏网格，再（可选）广播铺满**」：

```
1. 每个向量 xi 重塑为 (1,...,Ni,...,1)（第 i 维是 Ni，其余 1）
2. indexing='xy' 且 ndim>1：交换前两轴（output[0] → (1,-1,...)，output[1] → (-1,1,...)）
3. sparse=False → broadcast_arrays 铺成全矩阵
4. copy=True → 逐个 copy
```

`digitize` 的流程：

```
1. 校验 x 非复数、bins 单调（_monotonicity 返回 +1/-1，0 报错）
2. side = 'left' if right else 'right'   （注意：与 right 反向，因参数顺序对调）
3. bins 递减 → 反转 bins，返回 len(bins) - searchsorted(bins[::-1], x, side)
   bins 递增 → 直接 searchsorted(bins, x, side)
```

`sort_complex`：拷贝 → `sort()` → 若非复数则 `astype` 到对应复数类型。

#### 4.5.3 源码精读

`meshgrid` 的升维与 `xy` 轴交换（[_function_base_impl.py:5191-5198](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5191-L5198)）：

```python
s0 = (1,) * ndim
output = [np.asanyarray(x).reshape(s0[:i] + (-1,) + s0[i + 1:])
          for i, x in enumerate(xi)]
if indexing == 'xy' and ndim > 1:
    output[0] = output[0].reshape((1, -1) + s0[2:])   # x 横铺
    output[1] = output[1].reshape((-1, 1) + s0[2:])   # y 竖铺
```

这就是 `xy` 模式下输出形状为 `(N2, N1)` 而 `ij` 模式为 `(N1, N2)` 的根源——`xy` 把第一个向量的轴摆到第 1 维、第二个摆到第 0 维，模拟笛卡尔坐标 `(x[j], y[i])`。

`digitize` 的递减分支（[_function_base_impl.py:5771-5781](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5771-L5781)）：

```python
mono = _monotonicity(bins)
if mono == 0:
    raise ValueError("bins must be monotonically increasing or decreasing")
side = 'left' if right else 'right'
if mono == -1:
    return len(bins) - _nx.searchsorted(bins[::-1], x, side=side)
else:
    return _nx.searchsorted(bins, x, side=side)
```

`side` 与 `right` 「反向」是因为：`right=False` 表示区间 `bins[i-1] <= x < bins[i]`（左闭右开），而 `searchsorted(side='right')` 返回的是「插入后仍有序的最右位置」，恰好给出满足 `bins[i-1] <= x < bins[i]` 的 `i`。

`sort_complex`（[_function_base_impl.py:1895-1905](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1895-L1905)）：

```python
b = array(a, copy=True)
b.sort()
if not issubclass(b.dtype.type, _nx.complexfloating):
    if b.dtype.char in 'bhBH':     # 8/16 位整数 → 单精度复数 'F'
        return b.astype('F')
    elif b.dtype.char == 'g':       # 扩展精度浮点 → 扩展复数 'G'
        return b.astype('G')
    else:
        return b.astype('D')        # 其余 → 双精度复数 'D'
else:
    return b
```

> 完整函数体：[`meshgrid`:5060-5210](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5060-L5210)、[`digitize`:5673-5781](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5673-L5781)、[`sort_complex`:1871-1905](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1871-L1905)。

#### 4.5.4 代码实践

**目标**：对比 `meshgrid` 的 `xy` 与 `ij` 形状，并用 `digitize` 做分箱。

**步骤**：

```python
import numpy as np
x = np.array([0, 1, 2])   # 长度 3
y = np.array([0, 1])      # 长度 2

xv_xy, yv_xy = np.meshgrid(x, y, indexing='xy')   # 形状 (2,3)
xv_ij, yv_ij = np.meshgrid(x, y, indexing='ij')   # 形状 (3,2)
print("xy shapes:", xv_xy.shape, yv_xy.shape)
print("ij shapes:", xv_ij.shape, yv_ij.shape)

vals = np.array([0.2, 6.4, 3.0, 1.6])
bins = np.array([0.0, 1.0, 2.5, 4.0, 10.0])
print("digitize:", np.digitize(vals, bins))   # [1,4,3,2]
```

**预期输出**：

```
xy shapes: (2, 3) (2, 3)
ij shapes: (3, 2) (3, 2)
digitize: [1 4 3 2]
```

**观察现象**：`digitize` 的结果满足 `bins[i-1] <= x < bins[i]`（`right=False` 默认）。把 `right=True` 再跑一次，对比 `6.4` 等边界值的归箱变化。也可验证 `np.digitize(x, bins, right=True)` 与 `np.searchsorted(bins, x, side='left')` 完全等价（文档明示）。

#### 4.5.5 小练习与答案

**练习 1**：`np.sort_complex([5,3,6,2,1])` 的 dtype 是什么？

**答案**：`complex128`（即 `'D'`）。输入是默认 `int64`，落在 `else` 分支 `astype('D')`，故返回 `[1.+0.j, 2.+0.j, ..., 6.+0.j]`。见 [_function_base_impl.py:1903](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1903)。

**练习 2**：为什么 `digitize` 对递减 `bins` 要返回 `len(bins) - searchsorted(bins[::-1], x)`？

**答案**：`searchsorted` 只能处理递增数组，所以先把递减 `bins` 反转成递增，查到下标 `k` 后，原递减数组中对应的「从右数」位置就是 `len(bins) - k`。这样保证无论 `bins` 递增还是递减，返回的下标语义一致。

**练习 3**：`meshgrid(..., sparse=True)` 相比 `sparse=False` 节省了什么？

**答案**：`sparse=True` 跳过 `broadcast_arrays`，每个输出只保留 `(1,...,Ni,...,1)` 形状（即 u4-l1 讲过的「开放网格」），不实际复制数据。后续参与运算时靠广播自动铺满，省内存——这对大网格（如 1000×1000）尤其有意义。

---

### 4.6 形状翻转与边缘修剪：rot90、trim_zeros

#### 4.6.1 概念说明

- **`rot90(m, k=1, axes=(0,1))`**：在 `axes` 指定的平面内把数组旋转 90° 的 `k` 倍。**返回视图**（由 `flip` + `transpose` 组合而成，零拷贝）。
- **`trim_zeros(filt, trim='fb', axis=None)`**：裁掉数组边缘「全零」的行/列。`trim` 控制裁前端（`f`）/后端（`b`），`axis` 指定沿哪一维裁（2.2.0 新增）。

#### 4.6.2 核心流程

`rot90` 把 `k` 折算到 `{0,1,2,3}` 后分四种情况：

```
k %= 4
k==0 → 返回 m[:]                        （不变）
k==2 → flip(flip(m, axes[0]), axes[1])  （转 180°）
k==1 → transpose(flip(m, axes[1]), axes_list)   （逆时针 90°）
k==3 → flip(transpose(m, axes_list), axes[1])   （顺时针 90°）
其中 axes_list 是把 axes[0] 与 axes[1] 互换的转置轴序
```

`trim_zeros` 用 `argwhere` 找非零元素的边界：

```
1. (start, stop) = _arg_trim_zeros(filt)   # argwhere 后取每维 min/max
2. stop += 1                                # 闭区间转切片
3. 全零 → start=stop=0（结果为空）
4. 'f' 不在 trim → start = None（不裁前端）；'b' 不在 trim → stop = None
5. 构造切片元组（axis 外的维保持 slice(None)）
6. 1D 特判：直接 filt[sl[0]]，以保留 list/tuple 输入类型
```

#### 4.6.3 源码精读

`rot90` 的转置轴序构造（[_function_base_impl.py:268-276](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L268-L276)）：

```python
axes_list = arange(0, m.ndim)
(axes_list[axes[0]], axes_list[axes[1]]) = (axes_list[axes[1]], axes_list[axes[0]])
if k == 1:
    return transpose(flip(m, axes[1]), axes_list)
else:  # k == 3
    return flip(transpose(m, axes_list), axes[1])
```

`flip` 与 `transpose` 都是视图操作（u3-l3 讲过 `flip` 返回步长取负的视图），故 `rot90` 是 O(1) 的零拷贝操作。

`trim_zeros` 的边界查找委托 `_arg_trim_zeros`（[_function_base_impl.py:1932-1944](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1932-L1944)）：

```python
nonzero = (
    np.argwhere(filt)
    if filt.dtype != np.object_
    else np.argwhere(filt != 0)   # 对象数组：把 None 也当非零（历史行为）
)
if nonzero.size == 0:
    start = stop = np.array([], dtype=np.intp)
else:
    start = nonzero.min(axis=0)
    stop = nonzero.max(axis=0)
```

随后 `trim_zeros` 据 `start/stop` 与 `trim` 字符串构造切片（[_function_base_impl.py:2036-2042](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2036-L2042)）：

```python
if 'f' not in trim:
    start = (None,) * filt_.ndim
if 'b' not in trim:
    stop = (None,) * filt_.ndim
sl = tuple(slice(start[ax], stop[ax]) if ax in axis_tuple else slice(None)
           for ax in range(filt_.ndim))
```

> 完整函数体：[`rot90`:180-276](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L180-L276)、[`trim_zeros`:1952-2047](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1952-L2047)。

#### 4.6.4 代码实践

**目标**：验证 `rot90` 返回视图（修改原数组会影响结果），并用 `trim_zeros` 处理二维数组。

**步骤**：

```python
import numpy as np
m = np.array([[1, 2], [3, 4]])
r = np.rot90(m)            # 逆时针 90° → [[2,4],[1,3]]
print("rot90:\n", r)
m[0, 0] = 99               # 改原数组
print("rot90 after edit:\n", r)   # r 也变了 → 证明是视图

b = np.array([[0, 0, 2, 3, 0, 0],
              [0, 1, 0, 3, 0, 0],
              [0, 0, 0, 0, 0, 0]])
print("trim both:\n", np.trim_zeros(b))
print("trim axis=-1:\n", np.trim_zeros(b, axis=-1))
```

**预期输出**（`rot90` 的视图行为待本地验证，因其依赖 `transpose`+`flip` 的视图组合）：

```
rot90:
 [[2 4]
 [1 3]]
rot90 after edit:
 [[ 4 99]
 [ 2  1]]
trim both:
 [[0 2 3]
 [1 0 3]]
trim axis=-1:
 [[0 2 3]
 [1 0 3]
 [0 0 0]]
```

**观察现象**：`trim_zeros(b)`（`axis=None`）会沿**所有**维裁到「最小包围盒」，故全零的第三行也被裁掉；而 `axis=-1` 只裁最后一维（列）的两端零，第三行全零但保留。这正是 `axis` 参数（2.2.0 引入）的意义。

#### 4.6.5 小练习与答案

**练习 1**：`np.rot90(m, k=1, axes=(1,0))` 与 `np.rot90(m, k=3, axes=(0,1))` 结果有何关系？

**答案**：二者等价（互为反向旋转）。文档 Notes 明确：`rot90(m, k=1, axes=(1,0))` 是 `rot90(m, k=1, axes=(0,1))` 的逆，也等于 `rot90(m, k=-1, axes=(0,1))`，而 `k=-1` 折算后就是 `k==3`。

**练习 2**：`trim_zeros([0,1,2,0])` 返回什么类型？

**答案**：返回 `list` `[1, 2]`。源码对 1D 输入走 `filt[sl[0]]` 特判（[2043-2046](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2043-L2046)），保留输入的 `list`/`tuple` 类型；多维才走 `filt[sl]`（返回 ndarray）。

**练习 3**：`trim_zeros` 对全零数组返回什么？

**答案**：返回空数组（1D 时是空列表/空数组）。源码在 `start.size == 0` 时把 `start=stop=zeros(ndim)`（[2031-2034](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2031-L2034)），切片结果为空。文档 Notes 补充：多维全零时先裁第一轴。

---

## 5. 综合实践

把本讲的 `select`、`place`、`extract` 串成一个「数据清洗 + 分段标记」的小任务。

**场景**：你有一组传感器读数，需要：(1) 把异常值（NaN 或超界）替换为分段阈值；(2) 给正常读数打上「低/中/高」标签；(3) 用 `place` 把高读数批量改为上限值。

**参考实现**：

```python
import numpy as np

raw = np.array([0.3, np.nan, 2.5, 8.1, 1.2, np.nan, 7.7, -0.5])

# 步骤 1：用 select 做分段标记（NaN 用 default 标 0=异常）
is_bad = np.isnan(raw)
low  = (raw >= 0)   & (raw < 2)
mid  = (raw >= 2)   & (raw < 5)
high = (raw >= 5)

labels = np.select([low, mid, high], [1, 2, 3], default=0)
print("labels:", labels)        # 期望: [1,0,2,3,1,0,3,0]

# 步骤 2：用 extract 把所有「高」读数抽出来
high_vals = np.extract(high, raw)
print("high vals:", high_vals)  # 期望: [8.1 7.7]

# 步骤 3：用 place 把高读数就地截断为上限 5.0
cleaned = raw.copy()
np.place(cleaned, high, 5.0)
np.place(cleaned, is_bad, 0.0)  # 顺手把 NaN 也清零
print("cleaned:", cleaned)      # 期望: [0.3,0.0,2.5,5.0,1.2,0.0,5.0,0.0]
```

**预期输出**（建议本地运行核对）：

```
labels: [1 0 2 3 1 0 3 0]
high vals: [8.1 7.7]
cleaned: [0.3 0.  2.5 5.  1.2 0.  5.  0. ]
```

**思考题**（不必写代码）：

1. 为什么 `labels` 里 `raw=-0.5` 和两个 `NaN` 都标成了 `0`？——前者三个条件都不命中（`>=0` 失败），后者 `NaN` 与任何数比较都为 `False`，都落到 `default=0`。
2. 如果改用 `piecewise` 实现「分段标记」，需要传几个函数？——传 3 个 `lambda`（或标量）配 3 个条件，或传 4 个（第 4 个作「否则」处理异常）。
3. `place(cleaned, high, 5.0)` 中的 `5.0` 是标量，为什么合法？——`place` 用「前 N 个 + 循环复用」语义，标量相当于长度 1 的序列循环填入所有真值位。

---

## 6. 本讲小结

- **`select` 的优先级靠「反向 `copyto`」实现**：先铺满 `default`，再倒序烙印每个 choice，靠前的条件最后写、最终胜出（[918-926](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L918-L926)）。
- **`extract`/`place` 互逆**：前者 `take(ravel(arr), nonzero(cond))` 抽出一维，后者用「前 N 个 + 循环」语义就地写入，与 `copyto` 的「按位置对齐」截然不同。
- **`copy` 默认 `order='K'`**（保持布局），区别于 `ndarray.copy()` 的 `'C'`；`average` 整数输入强制升 `float64`，权重和为 0 抛 `ZeroDivisionError`；`asarray_chkfinite` 只对浮点类型查 NaN/Inf。
- **`append`/`delete`/`insert` 都返回新数组**：`append` 是 `concatenate` 封装；`delete` 的掩码分支等价于 `arr[~obj]`；`insert` 多点插入用「稳定排序 + `arange` 累加」补偿下标位移。
- **`meshgrid` 的 `xy`/`ij` 差异源于前两轴交换**（[5195-5198](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5195-L5198)）；`digitize` 是 `searchsorted` 的封装，`side` 与 `right` 因参数对调而反向；`sort_complex` 总返回复数。
- **`rot90` 返回视图**（`flip`+`transpose` 零拷贝）；`trim_zeros` 用 `argwhere` 的 min/max 找边界，`axis` 控制沿哪维裁；`iterable` 用「试 `iter`」判断，对 0 维数组返回 `False`。

## 7. 下一步学习建议

本讲讲的是「逐元素条件选择与数组编辑」，下一步可以：

1. **进入统计与归约**（u7-l1）：`_ureduce` 通用归约框架、`median`、`cov`/`corrcoef`——它们与本讲的 `average` 同处 `_function_base_impl.py`，共享 axis/keepdims 处理范式。
2. **学习 NaN 感知版本**（u9-l1）：`_nan_mask`/`_replace_nan`/`_divide_by_count` 三段式套路，本质上是把本讲的 `select`/`place` 思路用在 NaN 处理上（先替换、再聚合、再还原）。
3. **对比 `np.where`/`np.choose`**：这两个不在本讲范围，但与 `select` 同属「条件取值」家族，阅读它们能加深对 `select` 反向烙印技巧的理解。
4. **源码延伸阅读**：精读 [_function_base_impl.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py) 中 `vectorize`（[2278 行起](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2278)）与 `_parse_gufunc_signature`（[2160 行起](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2160)），那是本文件中最复杂的机制，将在 u11-l2 专门讲解。

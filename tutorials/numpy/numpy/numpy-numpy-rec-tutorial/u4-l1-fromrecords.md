# fromrecords：行式构建与自动格式探测

## 1. 本讲目标

本讲深入 `numpy.rec.fromrecords`——从「一整行一整行」的文本数据构建 record array 的函数。学完后你应当能够：

- 说清 `fromrecords` 接收的是**行方向**数据（每行一条记录），并与 u2-l3 讲过的列方向 `fromarrays` 区分；
- 准确描述它的**两条主路径**：无 `dtype`/`formats` 时走「慢速 object 数组逐列探测」（路径 A），有 `dtype`/`formats` 时走「直接 `sb.array` 构造」（路径 B）；
- 解释路径 A 为什么慢、它是如何把行式数据「转置」成列式再交给 `fromarrays`；
- 解释 list-of-lists（行是 `list` 而非 `tuple`）何时触发 `FutureWarning` 降级分支；
- 理解模块级辅助函数 `_deprecate_shape_0_as_None` 如何把已弃用的 `shape=0` 翻译成 `None`。

---

## 2. 前置知识

本讲默认你已经掌握前置讲义的三块认知（不会重复展开，只承接）：

1. **行式 vs 列式数据（来自 u1-l3 / u2-l3）**：`fromrecords` 吃的是「行方向」输入——外层每项是一条记录，内层是各字段的值；`fromarrays` 吃的是「列方向」输入——每项是一整列。二者互为转置。
2. **二元 dtype `(record, descr)`（来自 u3-l1）**：`sb.dtype((record, dtype))` 不改变结构，只把标量类型从 `void` 换成 `numpy.record`，从而让数组级 `r.x` 与标量级 `r[0].x` 同时可用。本讲路径 B 会再次用到它。
3. **format_parser 产出 dtype（来自 u2-l1 / u2-l2）**：当只给 `formats`/`names` 而不给 `dtype` 时，`format_parser` 负责把这些描述翻译成结构化 dtype。本讲路径 B 的 `formats` 子分支就调用它。

一句话回顾：真实实现全部在 [numpy/_core/records.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py)，`numpy/rec/__init__.py` 只是再导出垫片（见 u1-l1）。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [numpy/_core/records.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py) | 本讲全部实现：`fromrecords`、`fromarrays`（被路径 A 复用）、`_deprecate_shape_0_as_None` |
| [numpy/_core/tests/test_records.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py) | `TestFromrecords` 测试组，覆盖自动探测、显式 dtype、2D 行式输入等用例 |

本讲只引用这两个文件。`fromarrays` 的内部细节已在 u2-l3 讲透，本讲只把它当作「路径 A 的下游出口」使用。

---

## 4. 核心概念与源码讲解

### 4.1 fromrecords 函数总览：行式输入与两条主路径

#### 4.1.1 概念说明

`fromrecords` 解决的问题是：你手里有一堆「逐行」的异构数据（例如从文本读出来的、每行一条记录），想把它们直接装进一个 record array。典型输入长这样：

```python
recList = [(456, 'dbe', 1.2), (2, 'de', 1.3)]   # 两条记录，每条 3 个字段
```

外层是「行」，内层每项是一条记录的各字段值。这正是它与 `fromarrays`（外层是「列」）的根本区别。

函数签名带一长串可选参数，但决定走哪条路的关键只有**两个**：`dtype` 和 `formats`。源码据此一分为二：

- **路径 A（自动探测，较慢）**：`dtype` 和 `formats` **都没给**。函数自己逐列推断每个字段的类型。
- **路径 B（直接构造）**：给了 `dtype` 或 `formats` 中的至少一个。函数直接按指定类型一次性构造。

#### 4.1.2 核心流程

`fromrecords` 的判定逻辑可以用下面的伪代码概括：

```text
def fromrecords(recList, dtype, shape, formats, names, ...):
    if formats is None and dtype is None:        # ← 路径 A
        把 recList 转成 2D object 数组（保住异构值）
        逐列取出，让每列独立推断 dtype → arrlist
        return fromarrays(arrlist, names=names, ...)   # 复用列式构造

    # ↓ 路径 B：已有类型描述
    descr = (record, dtype) 或 format_parser(formats,...).dtype
    try:
        retval = sb.array(recList, dtype=descr)   # 期望行是 tuple
    except (TypeError, ValueError):               # 行是 list → 降级分支
        shape = _deprecate_shape_0_as_None(shape)
        逐行 _array[k] = tuple(recList[k])
        发出 FutureWarning
        return _array
    else:                                         # 成功分支
        必要时 reshape
        return retval.view(recarray)
```

两个关键观察：

1. **路径 A 不在本函数内「装数据」**，它把行式数据转置成列式后，直接 `return fromarrays(...)`，把真正分配内存与逐列填写的活儿外包给 u2-l3 讲过的 `fromarrays`。
2. **路径 B 的 try/except 才是本函数真正干活的地方**：成功就直接 `view(recarray)`；失败（通常是行用了 `list` 而非 `tuple`）就进降级分支，逐行手动填写并发出 `FutureWarning`。

#### 4.1.3 源码精读

整个函数的真实代码（含装饰器与文档串）见：

[numpy/_core/records.py:664-750](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L664-L750) —— `@set_module("numpy.rec")` 装饰器让这个物理上位于 `_core` 的函数对外显示为 `numpy.rec.fromrecords`（机制见 u1-l1）。

判定两条路径的那一行是函数体的第一句：

[numpy/_core/records.py:708](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L708) —— `if formats is None and dtype is None:` 注释里直白写着 `# slower`，点明这是「较慢」的自动探测路径。

顺便提一句：总调度入口 `np.rec.array` 在收到 `list`/`tuple` 时，会看**首元素**是 `tuple`/`list` 还是 `ndarray` 来决定调 `fromrecords` 还是 `fromarrays`：

[numpy/_core/records.py:1055-1059](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1055-L1059) —— 首元素是 `tuple`/`list` 走 `fromrecords`（行式），否则走 `fromarrays`（列式）。这条调度规则会在 u4-l4 详讲，这里只作为「`fromrecords` 的行式语义在调度层就被确认」的佐证。

#### 4.1.4 代码实践

**目标**：不运行，只读源码，预测给定输入会走哪条路径。

**操作步骤**：对下面四个调用，逐一判断它们进入路径 A 还是路径 B（以及路径 B 的成功分支还是降级分支）：

```python
# 示例代码（用于阅读判断，非项目原有代码）
a = np.rec.fromrecords([(1,'a'),(2,'b')], names='id,name')
b = np.rec.fromrecords([(1,'a'),(2,'b')], dtype=[('id','i4'),('name','U1')])
c = np.rec.fromrecords([[1,'a'],[2,'b']], dtype=[('id','i4'),('name','U1')])
d = np.rec.fromrecords([(1,'a'),(2,'b')], formats='i4,U1')
```

**需要观察的现象 / 预期结果**：

- `a`：`dtype=None` 且 `formats=None` → **路径 A**（自动探测）。
- `b`：给了 `dtype` → **路径 B**；行是 `tuple` → `sb.array` 成功 → **成功分支**。
- `c`：给了 `dtype` → **路径 B**；行是 `list` → `sb.array` 抛错 → **降级分支**（发 `FutureWarning`）。
- `d`：给了 `formats`（`dtype` 仍为 `None`）→ 注意条件是 `formats is None and dtype is None`，二者不全是 `None`，故进入 **路径 B**；用 `format_parser` 解析 `formats`；行是 `tuple` → 成功分支。

> 注意 `d` 这个易错点：判据是「**两个都为 None** 才走路径 A」，只要给了 `formats`，哪怕没给 `dtype`，也走路径 B。

#### 4.1.5 小练习与答案

**练习 1**：若调用 `np.rec.fromrecords([(1,2),(3,4)])`（既无 `dtype` 也无 `formats` 也无 `names`），会走哪条路径？字段名会是什么？

**答案**：走**路径 A**（自动探测）。字段名未指定，最终由 `fromarrays → format_parser` 补齐为默认的 `f0`、`f1`（见 u2-l2 的默认命名规则）。

**练习 2**：为什么路径 A 的注释写 `# slower`？

**答案**：因为它要先 `sb.array(recList, dtype=object)` 把所有值装箱成 Python 对象（产生大量装箱开销），再用 Python 层循环**逐列**取出并各自重新推断类型；而路径 B 是一次 C 层的 `sb.array(recList, dtype=descr)`，省去了逐列 Python 循环与对象装箱。

---

### 4.2 路径 A：无 dtype/formats 的自动逐列探测

#### 4.2.1 概念说明

当你既不给 `dtype` 也不给 `formats` 时，`fromrecords` 不知道每个字段该用什么类型，只能「**看数据本身**」来推断。它的策略很聪明：先把整张表当成一个 `dtype=object` 的二维数组（这样 int、str、float 各自的原生 Python 类型都不会被强行统一），再**按列切开**，让每一列单独跑一次类型推断——同列内的值会被「提升」到统一类型，而**不同列互不影响**。

这就是「逐列探测」：类型提升只发生在**列内**，不会跨列。

#### 4.2.2 核心流程

路径 A 的三步：

```text
1. obj = sb.array(recList, dtype=object)      # 2D object 数组，shape=(行数, 字段数)
2. 对第 i 列：obj[..., i].tolist()            # 取出该列，转成纯 Python list
            sb.array(上一步的 list)           # 让 NumPy 为这一列独立推断 dtype
   → 得到 arrlist（列方向数组列表）
3. return fromarrays(arrlist, names=names, ...)  # 交给列式构造装填
```

两个细节：

- `obj.shape[-1]` 是**字段数**（列数），所以循环 `range(obj.shape[-1])` 正好枚举每一列。
- 调 `fromarrays` 时传的 `formats` 仍是 `None`；`fromarrays` 内部见 `formats is None and dtype is None`，会取 `formats = [obj.dtype for obj in arrayList]`——也就是把上一步逐列推断出的 dtype 当作 formats 再交给 `format_parser`（见 u2-l3）。所以类型推断的成果被原样复用，不会重复猜测。

#### 4.2.3 源码精读

路径 A 的完整代码只有 7 行：

[numpy/_core/records.py:708-714](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L708-L714) —— 第 709 行 `obj = sb.array(recList, dtype=object)` 把行式数据铺成二维 object 数组；第 710-712 行的列表推导 `sb.array(obj[..., i].tolist())` 用 `obj[..., i]` 取第 `i` 列、`.tolist()` 转回 Python list、再 `sb.array(...)` 让该列独立推断类型；第 713-714 行把结果作为列交给 `fromarrays`。

注意 `sb` 是 `numpy._core.numeric` 的别名（见文件顶部 `from . import numeric as sb`），`sb.array` 即 `np.array`，`sb.dtype` 即 `np.dtype`。理解这一节用到的「自动类型提升」可参考 `fromarrays` 中的推断逻辑：

[numpy/_core/records.py:628-631](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L628-L631) —— `fromarrays` 在 `formats is None and dtype is None` 时取 `formats = [obj.dtype for obj in arrayList]`，即直接复用路径 A 已为每列推断好的 dtype。

#### 4.2.4 代码实践

**目标**：观察路径 A 的**逐列**类型提升结果，并对比 list-of-tuple 与 list-of-list 在「不指定 dtype」时是否行为一致、是否触发 `FutureWarning`。

**操作步骤**：

```python
# 示例代码（非项目原有代码）
import warnings
import numpy as np
warnings.simplefilter('always')        # 让所有警告都显式抛出，便于观察

# (1) list of tuple，不指定 dtype
r1 = np.rec.fromrecords([(1, 'a', 1.1), (2, 'b', 2.2)], names='id,name,val')
print(r1.dtype)                        # 看逐列推断出的 dtype
print(r1.id, r1.name, r1.val)

# (2) list of list，同样不指定 dtype
r2 = np.rec.fromrecords([[1, 'a', 1.1], [2, 'b', 2.2]], names='id,name,val')
print(r2.dtype)
```

**需要观察的现象 / 预期结果**：

- `r1.dtype` 预期为 `[('id', '<i8'), ('name', '<U1'), ('val', '<f8')]`：
  - 第 0 列 `[1, 2]` → 整数，提升为 `int64`；
  - 第 1 列 `['a', 'b']` → 字符串，按最长长度提升为 `'<U1'`；
  - 第 2 列 `[1.1, 2.2]` → 浮点，提升为 `float64`。
- `r1.id` 为 `array([1, 2])`，`r1.name` 为 `array(['a', 'b'], dtype='<U1')`，`r1.val` 为 `array([1.1, 2.2])`。
- **关键发现**：`r2`（list of list）与 `r1` 的 `dtype` 和数据**完全一致**，且**两者都没有触发 `FutureWarning`**。原因是「不指定 dtype」时一律走路径 A，而路径 A 用的是 `sb.array(recList, dtype=object)`——它对 list-of-tuple 和 list-of-list 一视同仁，都铺成二维 object 数组，因此行为相同。

> 也就是说：**仅在「不指定 dtype」时，list-of-list 不会触发 `FutureWarning`**。`FutureWarning` 实际发生在路径 B（见 4.3），需要同时满足「给了 dtype/formats」+「行是 list」。这一点容易记反，务必亲手验证。

#### 4.2.5 小练习与答案

**练习 1**：把第 1 列数据改成 `(1, 2.5)`（int 与 float 混在同一列），路径 A 会把这一列推断成什么 dtype？为什么？

**答案**：推断成 `float64`。因为路径 A 的类型提升是**列内**提升：同一列的 `1`（int）和 `2.5`（float）会被提升到能同时容纳二者的 `float64`；这与另一列无关。

**练习 2**：路径 A 里 `obj[..., i].tolist()` 这一步能否省掉，直接写 `sb.array(obj[..., i])`？

**答案**：不建议。`obj[..., i]` 本身是一个 `dtype=object` 的一维数组；直接 `sb.array` 它可能仍保留 object dtype 或行为不稳定。`.tolist()` 先把它还原成纯 Python 对象列表（如 `[1, 2]`、`['a','b']`），再交给 `sb.array` 做干净的类型推断，结果才符合「按值的内容推断最小类型」的预期。这是源码刻意这么写的原因。

---

### 4.3 路径 B：直接构造与 list-of-lists 的 FutureWarning 降级

#### 4.3.1 概念说明

当你给了 `dtype` 或 `formats`，类型已经明确，`fromrecords` 就不必逐列猜了——它直接构造一个带该 dtype 的数组。但这里有个历史包袱：NumPy 的 C 层在用结构化 dtype 从「序列的序列」构造数组时，**要求每一行是 `tuple`**；如果行是 `list`，构造会抛错。`fromrecords` 为了向后兼容，用 `try/except` 兜住这个错误，退而求其次地**逐行手动填写**，并同时发出 `FutureWarning`：提醒你「未来这会直接报错，请改用 list of tuples」。

#### 4.3.2 核心流程

路径 B 分两段：

```text
# 第一段：解析出 descr（结构化 dtype，且尽量把标量类型设为 record）
if dtype is not None:
    descr = sb.dtype((record, dtype))                 # 二元 dtype，承接 u3-l1
else:
    descr = format_parser(formats, names, ...).dtype  # 只有 formats 时

# 第二段：构造 + 兜底
try:
    retval = sb.array(recList, dtype=descr)           # 期望行是 tuple
except (TypeError, ValueError):                        # 行是 list 等情况
    shape = _deprecate_shape_0_as_None(shape)         # 顺带处理 shape=0 弃用
    if shape is None: shape = len(recList)
    if isinstance(shape, int): shape = (shape,)
    if len(shape) > 1: raise ValueError("Can only deal with 1-d array.")
    _array = recarray(shape, descr)
    for k in range(_array.size):
        _array[k] = tuple(recList[k])                 # 关键：把每行包成 tuple 再赋值
    warnings.warn(...FutureWarning...)                 # 提醒改用 list of tuples
    return _array
else:                                                  # 成功分支
    if shape is not None and retval.shape != shape:
        retval = retval.reshape(shape)
    return retval.view(recarray)
```

三个要点：

1. **降级分支只能处理 1 维**：`if len(shape) > 1: raise ValueError("Can only deal with 1-d array.")`，所以「行是 list」的兼容兜底**不支持 2D** 行式输入；2D 数据必须用 `tuple` 走成功分支（见 4.3.4 的 `test_fromrecords_2d`）。
2. **降级分支的核心修复**是 `tuple(recList[k])`：把每行 `list` 强行包成 `tuple` 再赋给已分配好的 `_array[k]`，绕过 C 层「行必须是 tuple」的限制。
3. 成功分支的 `retval.view(recarray)` 把普通结构化 ndarray 升级为 record array（视图，不拷贝数据；`__array_finalize__` 会把 void 提升为 record，见 u3-l1）。

#### 4.3.3 源码精读

解析 `descr` 的两行：

[numpy/_core/records.py:716-721](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L716-L721) —— 给了 `dtype` 就用 `sb.dtype((record, dtype))`（把标量类型替换为 `record`）；否则用 `format_parser` 从 `formats/names/titles` 组装 dtype。

try/except 整段（含降级分支与 `FutureWarning`）：

[numpy/_core/records.py:723-743](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L723-L743) —— 第 724 行尝试一次性构造；第 725 行 `except (TypeError, ValueError)` 兜底；第 736 行 `tuple(recList[k])` 是「把 list 行修复成 tuple 行」的关键；第 739-742 行发出 `FutureWarning`，`stacklevel=2` 让警告指向**调用 `fromrecords` 的用户代码**而非 `fromrecords` 自身。

成功分支：

[numpy/_core/records.py:744-750](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L744-L750) —— 构造成功后，若给了 `shape` 且与实际不符则 `reshape`，最后 `retval.view(recarray)` 升级为 record array 返回。

测试侧的两个对照用例很有说服力。`test_fromrecords_with_explicit_dtype` 用 list of **tuples** + 显式 dtype，走成功分支、无警告：

[numpy/_core/tests/test_records.py:282-295](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L282-L295) —— 行是 `tuple`，给 `dtype`，断言 `a.a == [1,2]`、`a.b == ['a','bbb']`，全程无 `FutureWarning`。

而 `test_fromrecords_2d` 同时演示了 2D 数据的两种走法，且断言二者结果相等：

[numpy/_core/tests/test_records.py:37-55](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L37-L55) —— 给 `dtype` 时（`r1`）行内元素是 `tuple`，走路径 B 成功分支得到 shape `(2,3)`；只给 `names` 时（`r2`）走路径 A 自动探测；最后 `assert_equal(r1, r2)` 证明两条路径殊途同归。

#### 4.3.4 代码实践

**目标**：亲手触发路径 B 的降级分支，观察 `FutureWarning`；并与「不指定 dtype 的 list of list（路径 A）」对比，彻底厘清警告究竟何时出现。

**操作步骤**：

```python
# 示例代码（非项目原有代码）
import warnings
import numpy as np
warnings.simplefilter('always')

# (A) list of tuple + dtype  → 路径 B 成功分支，预期：无警告
rA = np.rec.fromrecords([(1, 'a'), (2, 'b')], dtype=[('id', 'i4'), ('name', 'U1')])
print('A ok', rA.id, rA.name)

# (B) list of list + dtype   → 路径 B 降级分支，预期：FutureWarning
rB = np.rec.fromrecords([[1, 'a'], [2, 'b']], dtype=[('id', 'i4'), ('name', 'U1')])
print('B ok', rB.id, rB.name)
```

**需要观察的现象 / 预期结果**：

- `(A)` 不产生任何警告，`rA.id` 为 `array([1, 2], dtype=int32)`，`rA.name` 为 `array(['a', 'b'], dtype='<U1')`。
- `(B)` **会**产生一条 `FutureWarning`，内容大致为："fromrecords expected a list of tuples, may have received a list of lists instead. In the future that will raise an error"；同时 `rB.id`、`rB.name` 的数据与 `(A)` 相同（降级分支已用 `tuple(recList[k])` 修复）。
- 把本实践与 4.2.4 对照：**同样是 list of list，不指定 dtype 时不警告（路径 A），指定 dtype 时才警告（路径 B 降级）**。这正是本讲最容易混淆、也最值得亲手验证的一点。

> 关于 `(B)` 中 `sb.array(recList, dtype=descr)` 抛出的**具体异常文本**，取决于 NumPy C 层的报错措辞，**待本地验证**；但源码用 `except (TypeError, ValueError)` 同时兜住两类异常，无论抛哪一种都会进入降级分支。

#### 4.3.5 小练习与答案

**练习 1**：降级分支里有 `if len(shape) > 1: raise ValueError("Can only deal with 1-d array.")`。请据此判断：`np.rec.fromrecords([[[1,2],[3,4]]], dtype=[('a',int),('b',int)])`（2D 行式输入且行是 list）会发生什么？

**答案**：行是 `list` → `sb.array` 抛错 → 进入降级分支；此时若推断出的 `shape` 多于一维，降级分支会再抛 `ValueError("Can only deal with 1-d array.")`。也就是说，**2D 行式输入必须用 tuple**（走成功分支，正如 `test_fromrecords_2d` 的 `r1`）；用 list 既触发 `FutureWarning` 又可能在多维时直接失败。

**练习 2**：成功分支最后是 `retval.view(recarray)`。如果改成在降级分支也只做 `view`、不做逐行 `tuple` 填写，会有什么问题？

**答案**：行不通。降级分支之所以存在，正是因为 `sb.array(recList, dtype=descr)` 对 list 行**构造失败**，根本没有可 `view` 的 `retval`。降级分支必须先 `recarray(shape, descr)` 分配空数组，再用 `tuple(recList[k])` 逐行把数据「塞」进去，才能在不依赖 C 层 list 构造的前提下得到结果。

---

### 4.4 _deprecate_shape_0_as_None：shape=0 的弃用守卫

#### 4.4.1 概念说明

历史上，调用方曾用 `shape=0` 表示「请帮我推断 shape」。这个用法已被弃用：未来 `shape=0` 将等价于 `shape=(0,)`（一个长度为 0 的数组），而**不再是**「推断」。`_deprecate_shape_0_as_None` 就是这个迁移的守卫：它检测到 `shape == 0` 时，发出 `FutureWarning` 并把 `0` 翻译回 `None`（即「请推断」），从而在弃用期内保持旧代码不崩。

它是**模块级私有辅助函数**（不在 `__all__` 里），被 `fromarrays`、`fromrecords`、`fromstring`、`fromfile` 四处复用。

#### 4.4.2 核心流程

```text
def _deprecate_shape_0_as_None(shape):
    if shape == 0:                 # 仅命中「整数 0」
        warnings.warn(..., FutureWarning, stacklevel=3)
        return None                # 翻译回 None，交给后续「shape 缺省则推断」逻辑
    else:
        return shape               # 其它情况原样返回（含 None、tuple 等）
```

注意判定用的是 `shape == 0`：

- `None == 0` 为 `False` → 原样返回 `None`；
- `(0,) == 0` 为 `False` → 原样返回 `(0,)`；
- 只有字面量整数 `0` 才命中。

`stacklevel=3` 的含义：第 1 层是这个辅助函数自身，第 2 层是调用它的 `fromrecords`/`fromarrays` 等，第 3 层是**用户的调用代码**——所以警告会精准指向用户写 `shape=0` 的那一行。

#### 4.4.3 源码精读

辅助函数本体：

[numpy/_core/records.py:557-566](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L557-L566) —— 第 558 行 `if shape == 0` 判定，第 559-563 行发 `FutureWarning`（`stacklevel=3`），第 564 行 `return None` 完成翻译。

在 `fromrecords` 中，它**只在路径 B 的降级分支里**被调用（成功分支和路径 A 都不调）：

[numpy/_core/records.py:727](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L727) —— `shape = _deprecate_shape_0_as_None(shape)`，位于 `except` 块内、紧接在 `sb.array` 构造失败之后。

> 一处易忽略的事实：`fromrecords` 调用 `_deprecate_shape_0_as_None` 的位置在**降级分支**里。这意味着在 `fromrecords` 中，`shape=0` 的弃用警告只有在「同时触发了 list-of-lists 降级」时才会冒出来；在路径 A 和成功分支里，`shape` 不会被这个守卫处理。其它三个函数（`fromarrays`/`fromstring`/`fromfile`）则是在主流程里无条件调用它。

#### 4.4.4 代码实践

**目标**：触发 `shape=0` 弃用警告，并验证它被翻译成了 `None`（即「推断 shape」）。

**操作步骤**：

```python
# 示例代码（非项目原有代码）
import warnings
import numpy as np
warnings.simplefilter('always')

# 同时满足「给了 dtype」+「行是 list」→ 进入降级分支；再传 shape=0
r = np.rec.fromrecords([[1, 'a'], [2, 'b']],
                       dtype=[('id', 'i4'), ('name', 'U1')],
                       shape=0)
print(r.shape)
```

**需要观察的现象 / 预期结果**：

- 会看到**两条**警告：一条是 4.3 讲的 list-of-lists `FutureWarning`，另一条是 `shape=0` 的 `FutureWarning`（"Passing `shape=0` to have the shape be inferred is deprecated ..."）。
- 由于 `shape=0` 被翻译成 `None`，降级分支里 `if shape is None: shape = len(recList)` 生效，最终 `r.shape` 预期为 `(2,)`（两条记录）。
- 若想推断 shape 且**不发警告**，应直接传 `shape=None`。

> 说明：要在 `fromrecords` 里单独观察 `shape=0` 警告比较绕（必须同时进入降级分支）。若只想干净地复现该警告，更简单的办法是用 `np.rec.fromarrays([np.array([1,2,3])], shape=0)`——`fromarrays` 在主流程第 621 行无条件调用此守卫。

#### 4.4.5 小练习与答案

**练习 1**：`_deprecate_shape_0_as_None((0,))` 返回什么？为什么？

**答案**：返回 `(0,)` 原值，且**不发警告**。因为判定是 `shape == 0`，而 `(0,) == 0` 为 `False`。这正符合迁移目标：未来 `shape=0` 等价于 `shape=(0,)`，所以现在传 `(0,)` 本就是合法的「长度为 0」语义，无需警告。

**练习 2**：为什么 `_deprecate_shape_0_as_None` 用 `stacklevel=3` 而不是 `stacklevel=2`？

**答案**：因为调用层级是「用户代码 → `fromrecords` → `_deprecate_shape_0_as_None`」三层。`stacklevel=3` 让 `warnings.warn` 把警告归因到**用户代码**那一行（即写 `shape=0` 的地方），而不是 `fromrecords` 内部，便于用户定位自己的待迁移代码。对比 4.3 里 `fromrecords` 自己发的 `FutureWarning` 用的是 `stacklevel=2`（用户 → `fromrecords`，两层）。

---

## 5. 综合实践

把本讲四条线索串起来：**行式语义 → 两条路径 → 类型提升 → 弃用守卫**。

**任务**：你有一份「迷你成绩单」的行式数据，要求分别用「自动探测」和「显式 dtype」两种方式构建 record array，并诊断其中一处会触发弃用警告的写法。

```python
# 示例代码（非项目原有代码）
import warnings
import numpy as np
warnings.simplefilter('always')

rows = [(1, 'alice', 90.5), (2, 'bob', 78.0), (3, 'cy', 88.5)]

# (1) 自动探测（路径 A）：不指定 dtype，观察逐列类型提升
r1 = np.rec.fromrecords(rows, names='id,name,score')
print('r1.dtype =', r1.dtype)        # 预期 id:i8, name:<U5, score:f8
print('r1.name  =', r1.name)         # 预期 array(['alice','bob','cy'], dtype='<U5')

# (2) 显式 dtype（路径 B 成功分支）：行用 tuple
r2 = np.rec.fromrecords(rows, dtype=[('id','i4'),('name','U5'),('score','f4')])
print('r2.score =', r2.score)        # 预期 float32 数组

# (3) 诊断：把 (2) 的行改成 list，并加 shape=0，观察两条弃用警告
bad = [[1,'alice',90.5],[2,'bob',78.0],[3,'cy',88.5]]
r3 = np.rec.fromrecords(bad,
                        dtype=[('id','i4'),('name','U5'),('score','f4')],
                        shape=0)
print('r3.shape =', r3.shape)        # 预期 (3,)，因为 shape=0 被翻译成 None 后推断
```

**自查清单**：

1. `r1.name` 为什么是 `'<U5'`？→ 路径 A 第 1 列 `['alice','bob','cy']` 最长 5 字符，列内提升为 `<U5`。
2. `r2` 为什么没有警告？→ 行是 `tuple`，路径 B 成功分支。
3. `r3` 为什么有**两条** `FutureWarning`？→ 一条来自「list of lists」（4.3），一条来自 `shape=0`（4.4，因进入降级分支才被守卫处理）。
4. `r3.shape` 为什么是 `(3,)` 而非 `(0,)`？→ `shape=0` 在弃用期内被翻译成 `None`，随后 `shape = len(recList) = 3`。未来这个翻译会取消，届时 `shape=0` 将真正意味着长度 0。

> 若你在本机运行 `(3)`，具体异常/警告文本依赖 NumPy 版本，相关细节**待本地验证**；但「出现两条 `FutureWarning` 且 `r3.shape == (3,)`」是基于源码逻辑的预期结论。

---

## 6. 本讲小结

- `fromrecords` 吃**行方向**数据（外层每项一条记录），与吃列方向的 `fromarrays` 互为转置；总调度 `np.rec.array` 也是看首元素是否为 `tuple`/`list` 来确认这种行式语义。
- 判定两条路径的唯一开关是 `if formats is None and dtype is None:`——**两个都为 None** 才走路径 A，否则走路径 B。
- **路径 A（自动探测）**：先 `sb.array(recList, dtype=object)` 铺成二维 object 数组，再**逐列** `tolist()` + `sb.array()` 独立推断类型，最后委托 `fromarrays` 装填；类型提升只在列内发生，故较慢。
- **路径 B（直接构造）**：`try: sb.array(recList, dtype=descr)`；行是 `tuple` 走成功分支 `.view(recarray)`，行是 `list` 走降级分支逐行 `tuple(recList[k])` 填写并发 `FutureWarning`（`stacklevel=2`）。
- **关键澄清**：list-of-list **仅在指定了 dtype/formats（路径 B）时**才触发 `FutureWarning`；不指定 dtype（路径 A）时，list-of-tuple 与 list-of-list 行为完全一致、都不警告。
- **`_deprecate_shape_0_as_None`**：把已弃用的 `shape=0` 翻译成 `None`（即「推断 shape」），`stacklevel=3` 指向用户代码；在 `fromrecords` 中它只在降级分支被调用。

---

## 7. 下一步学习建议

- 本讲把「行式文本数据」讲透了，但 `fromrecords` 只能处理内存里的 Python 序列。**二进制来源**的两种构建函数请接着读 u4-l2（`fromstring`，从 bytes 缓冲）与 u4-l3（`fromfile`，从文件读取），它们同样会用到 `_deprecate_shape_0_as_None` 和 `recarray(shape, descr, buf=..., offset=...)`。
- 想看清「总调度入口如何把这四个 `from*` 函数串起来」，继续读 u4-l4（`array` 调度函数），它会完整展示 `obj` 类型分支判定，本讲引用的 [numpy/_core/records.py:1055-1059](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1055-L1059) 正是其中一条分支。
- 建议同步翻阅 [numpy/_core/tests/test_records.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py) 的 `TestFromrecords` 组，用断言反查行为：`test_fromrecords`（路径 A）、`test_fromrecords_2d`（两路径等价）、`test_fromrecords_with_explicit_dtype`（路径 B 成功）三个用例恰好覆盖本讲全部路径。

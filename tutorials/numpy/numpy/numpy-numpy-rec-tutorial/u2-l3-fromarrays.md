# fromarrays：按列从数组列表构建

## 1. 本讲目标

前两讲（u2-l1、u2-l2）我们一直在「造 `dtype`」：`format_parser` 把 `formats/names/titles` 翻译成一个严格的结构化 `dtype`，但全程**没装一比特数据**。本讲是整个 u2 单元的收口——`fromarrays` 拿到一个「列方向的数组列表」，把它们**逐列装进一个新分配的 `recarray`**，真正产出有数据的 record array。

学完本讲，你应该能够：

- 说清 `fromarrays` 接收的是**列方向**输入（列表里每个数组对应一个字段），并把它和 `fromrecords` 的**行方向**输入（列表里每个元素是一条记录）彻底区分开；
- 推断在「不传 `shape`」时，结果的形状如何从 `arrayList[0].shape` 得来，以及当字段本身是**多维子数组**（subarray，如 `'(2,3)f8'`）时，源码如何把子数组那几维从总形状里**裁掉**；
- 解释「字段数 ≠ 数组数」时为什么抛 `ValueError("mismatch between the number of fields and the number of arrays")`，以及 `dtype` 与 `formats` 同时给出时谁优先；
- 讲明白 `_array[name] = obj` 这一句为什么**总是拷贝**数据（修改源数组不会影响结果），并区分「`_array[name]=obj`（`__setitem__`）」与「`_array.name=obj`（`__setattr__`）」两条赋值路径。

真实实现全部在 [numpy/_core/records.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py)，`numpy/rec/__init__.py` 仅是再导出垫片（u1-l1 已讲）。

## 2. 前置知识

承接 u2-l1、u2-l2，你需要先记住四件事：

1. **`format_parser` 产出 `dtype`**：给它 `formats/names/titles`，它返回一个挂在 `.dtype` 上的结构化 dtype（u2-l1 讲过三步流水线 `_parseFormats → _setfieldnames → _createdto`，u2-l2 讲过字段命名与查重）。`fromarrays` 在你没显式给 `dtype` 时会**复用**它。

2. **列方向 vs 行方向**——这是本讲最关键的直觉：
   - **列方向（column-wise）**：外层列表的每一项是「一整列」。`[[1,2,3,4], ['a','b','c','d']]` 表示两列、四行。
   - **行方向（row-wise）**：外层列表的每一项是「一整行」。`[(1,'a'), (2,'b'), (3,'c')]` 表示三行两列。
   - `fromarrays` 吃**列方向**，`fromrecords` 吃**行方向**，两者数据排布正好互为转置，别混了。

3. **`sb` 是 `numpy._core.numeric` 的别名**（见 [numpy/_core/records.py:11](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L11)），所以 `sb.asarray` 就是你常用的 `np.asarray`，`sb.dtype` 就是 `np.dtype`。本讲里一律把 `sb.X` 读作 `np.X`。

4. **`recarray` 是 `ndarray` 的子类**（u1-l2、u3-l1 讲），构造时以 `(record, descr)` 作为 dtype 的 `.type`，使每条标量记录是 `numpy.record`。本讲只关心「装数据」，`record` 标量与属性访问的魔法留到 u3 单元。

再统一三个口径（本讲反复用到）：

| 概念 | 含义 | 例子 |
|------|------|------|
| `arrayList` | 列方向的输入：列表/元组，每项对应一个字段 | `[x1, x2, x3]` |
| `descr` | 最终结构化 dtype | `[('num','<i8'), ('txt','<U4')]` |
| subarray 字段 | 字段本身是多维数组，而非标量 | `('m', '(2,3)f8')` 中 `m` 的形状是 `(2,3)` |

## 3. 本讲源码地图

本讲只盯住一个文件里的几段代码：

- [numpy/_core/records.py:569-661](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L569-L661) — `fromarrays` 全文（本讲主角）。
- [numpy/_core/records.py:557-566](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L557-L566) — 模块级辅助函数 `_deprecate_shape_0_as_None`（处理 `shape=0` 的弃用）。
- [numpy/_core/records.py:385-403](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L385-L403) — `recarray.__new__`，`fromarrays` 用它造一个空的 `recarray` 容器。
- [numpy/_core/records.py:449-484](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L449-L484) — `recarray.__setattr__`，属性式赋值 `_array.name=obj` 走这里（本讲末尾对比两条赋值路径时用）。

两条「上游调用方」，帮你看清 `fromarrays` 在整个子包里的位置：

- [numpy/_core/records.py:708-714](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L708-L714) — `fromrecords` 在「无 dtype 无 formats」的慢速路径里，把行数据逐列拆开后**转手交给 `fromarrays`**。
- [numpy/_core/records.py:1055-1059](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1055-L1059) — 总调度入口 `array` 在「`obj` 是 list/tuple 且首元素不是 tuple/list」时，分发到 `fromarrays`。

## 4. 核心概念与源码讲解

### 4.1 fromarrays 的定位：列式输入与「逐列装填」整体流程

#### 4.1.1 概念说明

你已经会用 `format_parser` 造出一个 dtype（u2-l1），但 dtype 只是「字段说明书」，里面没有数据。`fromarrays` 解决的问题是：**我手上已经有几列现成的数组，想直接拼成一个 record array，怎么办？**

它的输入 `arrayList` 是**列方向**的——列表里第 0 项是第 0 个字段的整列数据，第 1 项是第 1 个字段的整列数据，依此类推。例如三列四行：

```python
x1 = np.array([1, 2, 3, 4])                 # 第 0 列
x2 = np.array(['a', 'dd', 'xyz', '12'])     # 第 1 列
x3 = np.array([1.1, 2, 3, 4])               # 第 2 列
r = np.rec.fromarrays([x1, x2, x3], names='a,b,c')   # 4 条记录、3 个字段
```

注意：列的**顺序**就是字段的**顺序**，列数必须等于字段数。这和 `fromrecords([(1,'a',1.1),(2,'b',2.2)], ...)`（行式，每项是一条完整记录）在数据排布上正好相反。

`fromarrays` 的整体职责可以概括成五步：**规整输入 → 推断 shape → 组装 dtype → 校验字段数 → 逐列装填**。其中「组装 dtype」直接复用 u2-l1 的 `format_parser`，本讲不重复其内部细节。

#### 4.1.2 核心流程

```
fromarrays(arrayList, dtype, shape, formats, names, titles, ...)
│
├─ ① 规整输入
│      arrayList = [sb.asarray(x) for x in arrayList]   # 每项转成 ndarray（已是数组则不拷贝）
│
├─ ② 推断 shape（见 4.2）
│      shape = _deprecate_shape_0_as_None(shape)         # shape=0 弃用告警 → None
│      if shape is None:  shape = arrayList[0].shape     # 默认取第 0 列的形状
│      elif isinstance(shape, int): shape = (shape,)     # 整数 → 单元素元组
│
├─ ③ 组装 dtype（见 4.3）
│      if formats is None and dtype is None:
│          formats = [obj.dtype for obj in arrayList]    # 没给类型？用每列自己的 dtype 当 formats
│      descr = sb.dtype(dtype)  或  format_parser(formats,...).dtype
│      _names = descr.names
│
├─ ④ 校验字段数（见 4.3）
│      if len(descr) != len(arrayList):
│          raise ValueError("mismatch between the number of fields and the number of arrays")
│
├─ ⑤ 处理 subarray 字段（见 4.4）
│      d0 = descr[0].shape;  nn = len(d0)
│      if nn > 0:  shape = shape[:-nn]                   # 把第 0 字段的子数组维度从总 shape 裁掉
│
├─ ⑥ 逐列装填
│      _array = recarray(shape, descr)                   # 造一个【空】容器
│      for k, obj in enumerate(arrayList):
│          ...逐列校验 + _array[name] = obj（拷贝）...
│
└─ return _array
```

整个函数只有约 90 行，没有任何递归或复杂算法，难度全在「shape 与 subarray 维度怎么对齐」这一处。下面四节分别拆开讲。

#### 4.1.3 源码精读

函数签名与文档串在 [numpy/_core/records.py:569-571](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L569-L571)：

```python
@set_module("numpy.rec")
def fromarrays(arrayList, dtype=None, shape=None, formats=None,
               names=None, titles=None, aligned=False, byteorder=None):
```

`@set_module("numpy.rec")` 让它对外显示为 `numpy.rec.fromarrays`（u1-l1 讲过这套命名空间把戏）。参数分两组：

- **数据参数**：`arrayList`（列方向输入）、`shape`（结果形状，可省）。
- **类型参数**：`dtype`（直接给完整 dtype）、`formats/names/titles/aligned/byteorder`（不给 `dtype` 时转交 `format_parser`）。`dtype` 与 `formats` 二选一，**`dtype` 优先**（4.3 详解）。

文档串里给了两个典型例子，其中第二个值得记住——它展示了「显式给 `dtype`」的写法，[numpy/_core/records.py:606-615](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L606-L615)：

```python
>>> r = np.rec.fromarrays(
...     [x1, x2, x3],
...     dtype=np.dtype([('a', np.int32), ('b', 'S3'), ('c', np.float32)]))
>>> r
rec.array([(1, b'a', 1.1), (2, b'dd', 2. ), (3, b'xyz', 3. ),
           (4, b'12', 4. )],
          dtype=[('a', '<i4'), ('b', 'S3'), ('c', '<f4')])
```

这里源数组 `x2` 是字符串，显式 dtype 把它**强制收窄**成 `'S3'`（3 字节字节串）——说明 dtype 一旦显式给出，**以 dtype 为准**，源数组的 dtype 不再参与推导。

#### 4.1.4 代码实践

本实践对应任务规格里要求的主实践。

**实践目标**：用最朴素的列式输入构建 record array，验证字段的 dtype 由**源数组自动推导**，并确认属性访问生效。

**操作步骤**：

```python
import numpy as np

x1 = np.array([1, 2, 3, 4])                 # 整数列
x2 = np.array(['a', 'bb', 'ccc', 'dddd'])   # 字符串列，最长 4 字符 → '<U4'
r = np.rec.fromarrays([x1, x2], names='num,txt')

print(r)            # 整个 record array
print(r.num)        # 属性式取第 0 列
print(r.txt)        # 属性式取第 1 列
print(r.dtype)      # 看字段类型
print(r[1])         # 取第 1 条记录（numpy.record 标量）
```

**需要观察的现象 / 预期结果**：

- `r.num` 的 dtype 是平台默认整数（小端机上是 `<i8` 即 `int64`），因为 `x1` 是 `int64`。
- `r.txt` 的 dtype 是 `<U4`，因为 `x2` 中最长字符串 `'dddd'` 占 4 个字符，NumPy 自动推导出 4 宽 Unicode 串。这正是「字段 dtype 由源数组推导」的体现——你没给 `dtype`/`formats`，源码在 4.3 节那步用 `[obj.dtype for obj in arrayList]` 把每列的 dtype 当成了 formats。
- `r.dtype` 形如 `[('num', '<i8'), ('txt', '<U4')]`。
- `r[1]` 打印出类似 `(2, 'bb')` 的 `numpy.record` 标量，并能用 `r[1].txt` 取单个字段（标量级属性访问，u3-l3 详讲）。

（输出中的精确字节序 `<` 与整数字宽 `i8` 依平台而定，可在本机确认。）

#### 4.1.5 小练习与答案

**练习 1**：把上面的 `x2` 换成 `np.array(['a', 'bb', 'cccccc'])`（最长 6 字符），`r.txt` 的 dtype 会变成什么？

**参考答案**：`<U6`。字符串列的宽度取**全列最大长度**，这是 `np.asarray` 在构造 `<U` 数组时的行为；`fromarrays` 只是把这个 dtype 原样当成 formats（见 4.3）。

**练习 2**：列方向与行方向。下面哪个能直接喂给 `fromarrays`、哪个该喂给 `fromrecords`？
(a) `[[1,2,3], [4,5,6]]`  (b) `[(1,4), (2,5), (3,6)]`

**参考答案**：(a) 是「两列、每列 3 个」→ 列方向 → `fromarrays`（得到 3 条记录、2 个字段）；(b) 是「三条记录、每条 2 个字段」→ 行方向 → `fromrecords`。两者最终表达的是同一张表，只是输入排布相反。

---

### 4.2 输入规整与 shape 推断

#### 4.2.1 概念说明

`fromarrays` 的第一步不是组装 dtype，而是**把输入里每个元素都变成 ndarray**，再**决定结果数组的形状**。这两件事看似平淡，却各有讲究：

- **`sb.asarray(x)`**：`asarray` 的语义是「能不拷贝就不拷贝」——如果 `x` 已经是 dtype/内存布局都合适的 ndarray，就直接返回原对象；否则才转（比如把 Python list 转成 ndarray）。所以传 ndarray 进来几乎零成本，传 list/tuple 也没问题。

- **shape 默认值**：用户大多数时候不会传 `shape`，因为「第 0 列有多长」一眼可见。源码就用 `arrayList[0].shape` 当默认形状。这里隐含一个约定：**所有列应当等长**（subarray 字段除外，见 4.4），第 0 列被当作「标尺」。

- **`shape` 既是「记录数」也可能是多维**：如果第 0 列本身是 `(2,3)` 的二维数组，那 `shape=(2,3)` 表示「2×3 共 6 条记录」的二维 record array，`fromarrays` 照单全收。

- **弃用处理**：历史上 `shape=0` 表示「请帮我推断」。NumPy 从 1.19 起把它判为弃用，要求改用 `shape=None`。`_deprecate_shape_0_as_None` 就是干这件事。

#### 4.2.2 核心流程

```
① 规整
   arrayList = [sb.asarray(x) for x in arrayList]

② 处理 shape=0 弃用
   shape = _deprecate_shape_0_as_None(shape)
        # shape == 0  → 发 FutureWarning，返回 None
        # 否则        → 原样返回 shape（包括 None）

③ 推断 / 规整 shape
   if shape is None:           shape = arrayList[0].shape      # 默认：取第 0 列形状
   elif isinstance(shape, int): shape = (shape,)               # 5 → (5,)
   # 若 shape 已是 tuple（如 (2,3)），原样使用
```

注意第 ③ 步**只认 `int` 和 `tuple`**：传 `shape=5` 会被包成 `(5,)`；传 `shape=(2,3)` 直接用；传 `shape=None` 走默认分支。它不会再去猜别的类型。

#### 4.2.3 源码精读

输入规整只有一行，[numpy/_core/records.py:618](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L618)：

```python
arrayList = [sb.asarray(x) for x in arrayList]
```

这行保证后续 `obj.shape`、`obj.dtype`、`obj.ndim` 一定可用——无论用户传进来的是 ndarray、Python list、还是嵌套序列，此刻都已是 ndarray。

shape 推断三连，[numpy/_core/records.py:620-626](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L620-L626)：

```python
# NumPy 1.19.0, 2020-01-01
shape = _deprecate_shape_0_as_None(shape)

if shape is None:
    shape = arrayList[0].shape
elif isinstance(shape, int):
    shape = (shape,)
```

弃用辅助函数全文，[numpy/_core/records.py:557-566](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L557-L566)：

```python
def _deprecate_shape_0_as_None(shape):
    if shape == 0:
        warnings.warn(
            "Passing `shape=0` to have the shape be inferred is deprecated, "
            "and in future will be equivalent to `shape=(0,)`. To infer "
            "the shape and suppress this warning, pass `shape=None` instead.",
            FutureWarning, stacklevel=3)
        return None
    else:
        return shape
```

两点值得注意：一是警告文案明确说了「将来 `shape=0` 会等价于 `shape=(0,)`（零长度数组）而非推断」，所以现在就该改用 `None`；二是 `stacklevel=3`，意思是警告指向**调用 `fromarrays` 的用户代码**，而不是 `fromarrays` 内部这行——因为中间隔了「用户 → `fromarrays` → `_deprecate_shape_0_as_None`」正好三层。

#### 4.2.4 代码实践

**实践目标**：对比「不传 shape」「传整数 shape」「传二维列」三种情况，验证 shape 推断与规整行为。

**操作步骤**：

```python
import numpy as np

# (A) 不传 shape：默认取 arrayList[0].shape
a0 = np.rec.fromarrays([np.array([1,2,3,4])], names='c')
print("(A) shape:", a0.shape)          # 预期 (4,)

# (B) 传整数 shape=2：只取前 2 条
a1 = np.rec.fromarrays([np.array([1,2,3,4])], shape=2, names='c')
print("(B) shape:", a1.shape, "data:", a1.c)   # 预期 (2,) / [1 2]

# (C) 第 0 列本身就是二维 → 得到二维 record array
col = np.array([[1, 2], [3, 4]])       # shape (2,2)
a2 = np.rec.fromarrays([col], names='c')
print("(C) shape:", a2.shape)          # 预期 (2, 2)
```

**需要观察的现象 / 预期结果**：

- (A) `shape=(4,)`：`shape is None` 分支，取 `arrayList[0].shape = (4,)`。
- (B) `shape=(2,)` 且 `a1.c == [1 2]`：`isinstance(shape, int)` 分支把 `2` 包成 `(2,)`，随后装填时只装进前 2 条（第 4.4 节会看到，装填是把 4 元素列赋给 2 元素字段区，靠广播/截断对齐）。
- (C) `shape=(2,2)`：二维列直接成为二维 record array，共 4 条记录。

**待本地验证**：(B) 中把一个 4 元素列装进 `shape=(2,)` 的容器，是否报错还是静默截断——这取决于 `ndarray` 的结构化赋值规则。如果报 `ValueError`（形状不匹配），说明源码 4.4 节的 `testshape != shape` 校验先拦下了；请在本地确认实际行为。

#### 4.2.5 小练习与答案

**练习 1**：用户写 `np.rec.fromarrays([x], shape=0, names='c')` 会发生什么？

**参考答案**：触发一条 `FutureWarning`（"Passing `shape=0` ... is deprecated"），然后 `_deprecate_shape_0_as_None` 把 `0` 变成 `None`，于是走默认分支 `shape = arrayList[0].shape`，等价于「自动推断」。将来这个写法会变成「零长度数组」而不再推断，所以现在就该写 `shape=None`。

**练习 2**：`shape` 推断用的是 `arrayList[0]`，如果第 0 列是 subarray 字段（如形状 `(4,2,3)`、字段类型 `'(2,3)f8'`），直接拿 `(4,2,3)` 当 record array 的形状对吗？

**参考答案**：不对。`(4,2,3)` 里末尾的 `(2,3)` 是「每条记录里该字段的子数组形状」，不是记录数。源码在 4.4 节会用 `shape = shape[:-nn]` 把这 2 维裁掉，最终 record array 形状是 `(4,)`。4.2 节这一步只是「暂存」原始形状，真正的修正发生在后面。

---

### 4.3 dtype 组装与字段数校验

#### 4.3.1 概念说明

shape 定了，接下来要确定 dtype。`fromarrays` 面对三种用户姿势：

1. **显式给 `dtype`**：最直接，`descr = sb.dtype(dtype)`。源数组的 dtype 被无视，一切以 `dtype` 为准（见 4.1.3 那个 `'S3'` 收窄的例子）。
2. **给 `formats`（不给 `dtype`）**：交给 `format_parser`，它把 `formats/names/titles` 翻译成 dtype（u2-l1）。
3. **啥都不给**：这是最常见的「省事」用法。源码自动生成 `formats = [obj.dtype for obj in arrayList]`——**把每列源数组自己的 dtype 收集起来当 formats**，再交给 `format_parser`。这就是 4.1.4 实践里「字段 dtype 由源数组推导」的真正来源。

注意 `formats is None and dtype is None` 这个判定：只有「两者都没给」才走自动推导。只要你给了 `formats`，就不会再用源数组的 dtype 去覆盖它。

dtype 拿到后，还要做一道**字段数校验**：`len(descr)`（dtype 的字段数）必须等于 `len(arrayList)`（你给的列数）。列数给多给少都会被拦下，抛一条很明确的 `ValueError`。

#### 4.3.2 核心流程

```
① 决定 formats（仅在 dtype 与 formats 都没给时）
   if formats is None and dtype is None:
       formats = [obj.dtype for obj in arrayList]   # 每列自己的 dtype

② 组装 descr
   if dtype is not None:   descr = sb.dtype(dtype)                      # 路径 1：dtype 优先
   else:                   descr = format_parser(formats, names, titles,
                                                aligned, byteorder).dtype  # 路径 2/3：复用 format_parser
   _names = descr.names

③ 字段数校验
   if len(descr) != len(arrayList):
       raise ValueError("mismatch between the number of fields "
                         "and the number of arrays")
```

`len(descr)` 对结构化 dtype 返回的就是字段个数（等价于 `len(descr.names)`）。这道校验把「3 个字段却喂了 2 列」这类错误挡在装填之前，避免后面越界。

#### 4.3.3 源码精读

formats 自动推导，[numpy/_core/records.py:628-631](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L628-L631)：

```python
if formats is None and dtype is None:
    # go through each object in the list to see if it is an ndarray
    # and determine the formats.
    formats = [obj.dtype for obj in arrayList]
```

注释说「遍历列表里每个对象看它是不是 ndarray 并决定 formats」——其实经过 4.2 节的 `sb.asarray`，此刻它们**都已是 ndarray**，所以直接取 `obj.dtype` 即可。这条列表随后喂给 `format_parser`，由它（u2-l1 的 `_parseFormats`）把一串 dtype 组装成结构化 dtype 并算好对齐与偏移。

dtype 组装二选一，[numpy/_core/records.py:633-637](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L633-L637)：

```python
if dtype is not None:
    descr = sb.dtype(dtype)
else:
    descr = format_parser(formats, names, titles, aligned, byteorder).dtype
_names = descr.names
```

`dtype` 分支在前，所以**`dtype` 优先级高于 `formats`**：两者都给时，`formats` 被忽略（实际上调用方 `array` 在 L1034-L1038 会先把 `formats` 折算成 `dtype`，但在 `fromarrays` 自身这里，`dtype` 不为 None 就直接用它）。

字段数校验，[numpy/_core/records.py:640-642](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L640-L642)：

```python
if len(descr) != len(arrayList):
    raise ValueError("mismatch between the number of fields "
            "and the number of arrays")
```

#### 4.3.4 代码实践

**实践目标**：触发字段数不匹配的报错，并对比「自动推导」「给 formats」「给 dtype」三种类型来源。

**操作步骤**：

```python
import numpy as np

# (A) 字段数 ≠ 列数 → 报错
try:
    np.rec.fromarrays([np.array([1,2]), np.array([3,4])],
                      dtype=[('only','i8')])           # 2 列、1 个字段
except ValueError as e:
    print("(A) ValueError:", e)

# (B) 啥都不给 → 自动推导（等价于 formats=[int64, <U4]）
r_auto = np.rec.fromarrays([np.array([1,2]), np.array(['ab','cd'])])
print("(B) dtype:", r_auto.dtype, " names:", r_auto.dtype.names)   # f0,f1

# (C) 给 formats → 以 formats 为准（把整数列强制成 f4）
r_fmt = np.rec.fromarrays([np.array([1,2])], formats=['f4'], names='x')
print("(C) dtype:", r_fmt.dtype, " data:", r_fmt.x)   # x 是 float32
```

**需要观察的现象 / 预期结果**：

- (A) 抛 `ValueError: mismatch between the number of fields and the number of arrays`，因为 `len(descr)=1` 而 `len(arrayList)=2`。
- (B) `dtype=[('f0','<i8'), ('f1','<U2')]`，字段名是默认的 `f0/f1`（u2-l2 讲过没给 `names` 就补默认名）。这印证了「源数组 dtype → formats → format_parser」的链路。
- (C) `dtype=[('x','<f4')]`，`r_fmt.x` 是 `array([1., 2.], dtype=float32)`——源数组虽是 `int64`，但 `formats=['f4']` 把字段类型定死成 `float32`，装填时发生 `int64 → float32` 的类型转换。

#### 4.3.5 小练习与答案

**练习 1**：`np.rec.fromarrays([x1, x2], dtype=[('a','i8'),('b','f8'),('c','i8')])`（3 个字段、2 列）会发生什么？

**参考答案**：在 [records.py:640](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L640) 抛 `ValueError: mismatch between the number of fields and the number of arrays`。字段数 3 ≠ 列数 2。要么把 dtype 改成 2 个字段，要么再补一列。

**练习 2**：同时给了 `dtype` 和 `formats`，哪个生效？

**参考答案**：`dtype` 生效。源码 [records.py:633-636](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L633-L636) 先判 `if dtype is not None`，成立就直接 `descr = sb.dtype(dtype)`，根本不会走到 `format_parser` 那条 `formats` 分支。

---

### 4.4 逐字段装填：subarray 形状裁剪与 `_array[name] = obj` 的拷贝语义

#### 4.4.1 概念说明

dtype 和 shape 都备齐了，最后一步是「把每一列的数据写进容器」。这里有两件容易困惑的事：**subarray 字段的形状对齐**，以及**赋值到底拷不拷贝**。

**subarray 字段**。绝大多数字段是标量（如 `'f8'`），但结构化 dtype 允许字段本身是个小数组，比如 `('m', '(2,3)f8')` 表示「每条记录里 `m` 这个字段是一个 2×3 的浮点矩阵」。对这种字段，喂进来的那一列形状就不是 `(N,)`，而是 `(N, 2, 3)`——末尾的 `(2,3)` 是「每条记录内的子数组」，前面的 `N` 才是记录数。

源码的处理思路：先看**第 0 个字段**的子数组形状 `descr[0].shape`，从（4.2 节暂存的）总 shape 里**裁掉**这几个尾维度，得到真正的「记录形状」；随后在逐列装填时，对**每一列**都做同样的裁剪来校验形状是否一致。

用记号表示。设第 \(k\) 列输入形状为 \(S_k\)，该字段的子数组形状为 \(s_k\)（标量字段时 \(s_k=()\)，长度 \(|s_k|=0\)）。则：

\[
\text{记录形状} = S_k\text{ 去掉末尾 } |s_k| \text{ 维} \quad(\text{记作 } S_k[:-|s_k|])
\]

所有列的 \(S_k[:-|s_k|]\) 必须相等，且等于 record array 的形状。

**拷贝语义**。容器是 `recarray(shape, descr)` 新分配的空数组（u3-l1 讲过 `recarray.__new__`），随后 `_array[name] = obj` 把 `obj` 的数据**写入**这个新缓冲。这是结构化数组的字段赋值（走 `ndarray.__setitem__`），本质是把 `obj` 广播/拷进字段的内存区。因此 **`fromarrays` 的结果与源数组不共享内存**——事后改源数组，结果不变。文档串与测试都明确演示了这一点。这与 `arr.view(np.recarray)`（只换视图、共享内存，u1-l2 讲过）形成鲜明对比。

#### 4.4.2 核心流程

```
① 用第 0 字段的子数组形状修正总 shape
   d0 = descr[0].shape ;  nn = len(d0)
   if nn > 0:  shape = shape[:-nn]          # 裁掉末尾 nn 维（subarray 维）

② 造空容器
   _array = recarray(shape, descr)          # 全新内存，未填数据

③ 逐列校验 + 装填（拷贝）
   for k, obj in enumerate(arrayList):
       nn        = descr[k].ndim            # 第 k 字段的子数组维数
       testshape = obj.shape[: obj.ndim - nn]   # 裁掉该列末尾的子数组维
       name      = _names[k]
       if testshape != shape:
           raise ValueError('array-shape mismatch in array {k} ("{name}")')
       _array[name] = obj                   # 写入字段内存（拷贝）

④ return _array
```

第 ③ 步是关键：`testshape` 是「把这一列末尾的子数组维裁掉后剩下的形状」，它必须等于第 ①② 步定下的 record array 形状 `shape`。否则说明这一列的「记录数」和别的列对不上，抛带字段名的 `ValueError`。

#### 4.4.3 源码精读

第 0 字段形状修正，[numpy/_core/records.py:644-647](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L644-L647)：

```python
d0 = descr[0].shape
nn = len(d0)
if nn > 0:
    shape = shape[:-nn]
```

对全部标量字段，`descr[0].shape == ()`，`nn == 0`，不做任何裁剪——这是最常见的情形，平时用 `fromarrays` 几乎不会碰到这段逻辑生效。只有当**第 0 个字段**是 subarray 时它才动手。

造空容器，[numpy/_core/records.py:649](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L649)：

```python
_array = recarray(shape, descr)
```

这调用 `recarray.__new__`（[records.py:385-403](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L385-L403)），由于 `buf=None`，走 `ndarray.__new__(cls, shape, (record, descr), order=order)`——分配一块全新的、未初始化的内存，dtype 的标量类型被包成 `(record, descr)`，所以每个标量记录是 `numpy.record`。

逐列装填循环，[numpy/_core/records.py:652-659](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L652-L659)：

```python
# populate the record array (makes a copy)
for k, obj in enumerate(arrayList):
    nn = descr[k].ndim
    testshape = obj.shape[:obj.ndim - nn]
    name = _names[k]
    if testshape != shape:
        raise ValueError(f'array-shape mismatch in array {k} ("{name}")')

    _array[name] = obj
```

注释 `populate the record array (makes a copy)` 一语道破：**装填即拷贝**。

关于 `_array[name] = obj` 的赋值路径，要分清两种写法：

- **`_array[name] = obj`（方括号，源码用的就是这种）**：这是**项赋值**，走 `ndarray.__setitem__`。对结构化数组，`arr[fieldname] = x` 把 `x` 广播写入该字段的内存区。`recarray` 没有 override `__setitem__`，所以直接用 ndarray 的实现。
- **`_array.name = obj`（点号，属性赋值）**：走 `recarray.__setattr__`（[records.py:449-484](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L449-L484)）。它发现 `name` 是字段名后，调用 `self.setfield(val, *res)`——殊途同归，也写到同一块字段内存。这条魔法路径 u3-l2 会详讲。

两条路径最终都把数据写进字段内存，且都**拷贝**（不与 `obj` 共享内存）。

返回，[numpy/_core/records.py:661](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L661)：

```python
return _array
```

#### 4.4.4 代码实践

**实践目标**：验证 subarray 字段的形状裁剪，以及「装填即拷贝」的内存独立性。

**操作步骤**：

```python
import numpy as np

# (A) subarray 字段：每条记录的 m 是 2×3 矩阵
col = np.ones((4, 2, 3))            # 4 条记录，每条里 m 是 2×3
r = np.rec.fromarrays([col], dtype=[('m', '(2,3)f8')])
print("(A) r.shape:", r.shape)               # 预期 (4,) —— (2,3) 被裁掉了
print("(A) r[0].m.shape:", r[0].m.shape)     # 预期 (2, 3)
print("(A) r[0].m:\n", r[0].m)

# (B) 拷贝语义：改源数组不影响结果
x = np.array([1, 2, 3, 4])
r2 = np.rec.fromarrays([x], names='c')
x[1] = 99                          # 事后改源数组
print("(B) r2.c:", r2.c)                     # 预期 [1 2 3 4]，而不是 [1 99 3 4]
```

**需要观察的现象 / 预期结果**：

- (A) `r.shape == (4,)`：第 0 字段 `m` 的子数组形状是 `(2,3)`，`nn=2`，源码把暂存的 `(4,2,3)` 裁成 `(4,)`。每条记录 `r[0].m` 是一个 `(2,3)` 的 `numpy.matrix`/`ndarray` 视图，全是 1。
- (B) `r2.c == [1 2 3 4]`：即便把 `x[1]` 改成 99，`r2.c` 仍是原来的 `[1 2 3 4]`。这正是「`_array[name] = obj` makes a copy」——结果不与源数组共享内存。这与 docstring [records.py:602-604](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L602-L604) 的演示、以及测试 [test_records.py:89-90](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L89-L90) 的断言完全一致。

#### 4.4.5 小练习与答案

**练习 1**：`np.rec.fromarrays([np.ones((4,2,3)), np.zeros(4)], dtype=[('m','(2,3)f8'),('n','i8')])` 能成功吗？

**参考答案**：能。第 0 列形状 `(4,2,3)`，字段 `m` 子数组形状 `(2,3)`，裁掉后记录形状 `(4,)`；第 1 列形状 `(4,)`，字段 `n` 是标量（`nn=0`），`testshape = (4,)[:1-0] = (4,)`，等于记录形状 `(4,)`，校验通过。结果 `r.shape == (4,)`，`r.m` 每条是 `(2,3)`，`r.n` 每条是标量 0。

**练习 2**：为什么 `fromarrays([x], names='c')` 之后改 `x` 不影响结果，而 `np.array(x).view(np.recarray)` 之后改 `x`（当 `x` 是同一块内存时）会影响？

**参考答案**：`fromarrays` 的装填 `_array[name] = obj` 是**字段赋值（拷贝）**，结果用 `recarray(shape, descr)` 新分配的内存，不与 `obj` 共享。而 `.view(np.recarray)` 只是**换视图类型**，底层缓冲不变，所以与原数组共享内存。一句话：`fromarrays` 拷贝，`view` 不拷贝。

---

## 5. 综合实践

把本讲四个模块串起来：**造一个含 subarray 字段的二维 record array，并验证「自动 dtype 推导」「字段数校验」「subarray 形状裁剪」「拷贝语义」四件事**。

任务：用 `fromarrays` 构建一个 `(2,2)` 的 record array（共 4 条记录），含三个字段——一个整数列、一个字符串列（自动推导宽度）、一个 2×2 矩阵列。

```python
import numpy as np

# 第 0 列：2×2 的整数记录矩阵
col_id = np.array([[1, 2], [3, 4]])
# 第 1 列：2×2 的字符串记录矩阵，最长 'dddd' → <U4
col_name = np.array([['a', 'bb'], ['ccc', 'dddd']])
# 第 2 列：每条记录里 vec 是长度 2 的向量 → 形状 (2,2,2)
col_vec = np.arange(8, dtype='f4').reshape(2, 2, 2)

# 不给 dtype/formats：前两列自动推导；但第 2 列是 subarray，
# 自动推导会把 '(2,)f4' 当成 formats，需手动给 dtype 才能精确控制。
dt = np.dtype([('id', 'i8'), ('name', '<U4'), ('vec', '(2,)f4')])
r = np.rec.fromarrays([col_id, col_name, col_vec], dtype=dt)

print("shape:", r.shape)          # 预期 (2, 2)
print("dtype:", r.dtype)
print("r.id:\n", r.id)            # 2×2 整数
print("r.name:\n", r.name)        # 2×2 字符串
print("r.vec.shape:", r.vec.shape)# 预期 (2, 2, 2)
print("r[0,0]:", r[0,0])          # 一条 record 标量
print("r[0,0].vec:", r[0,0].vec)  # 这条记录里 vec 的 2 元素向量

# 验证拷贝语义
col_id[0,0] = 999
print("after mutate, r.id[0,0]:", r.id[0,0])   # 预期仍是 1（拷贝）

# 验证字段数校验
try:
    np.rec.fromarrays([col_id, col_name], dtype=dt)   # 少给一列
except ValueError as e:
    print("mismatch:", e)
```

**需要观察的现象 / 预期结果**：

- `r.shape == (2,2)`：二维 record array，4 条记录。
- `r.vec.shape == (2,2,2)`：`vec` 字段把「记录形状 `(2,2)`」与「子数组形状 `(2,)`」拼起来。
- 修改 `col_id` 后 `r.id[0,0]` 仍为 1：装填即拷贝。
- 少给一列触发 `ValueError: mismatch between the number of fields and the number of arrays`。

（精确字节序、`r[0,0]` 的打印格式依平台与 NumPy 版本而定，可在本机确认。）

## 6. 本讲小结

- `fromarrays` 吃**列方向**输入（列表每项 = 一个字段的整列），与 `fromrecords` 的**行方向**输入相反；列数必须等于字段数。
- shape 默认取 `arrayList[0].shape`；`shape=0` 已弃用（`_deprecate_shape_0_as_None` 发 `FutureWarning` 后当 `None`）；整数 shape 会被包成单元素元组。
- dtype 有三种来源，优先级 **`dtype` > `formats` > 自动推导**；啥都不给时 `formats = [obj.dtype for obj in arrayList]`，即「字段类型由源数组推导」。
- `len(descr) != len(arrayList)` 抛 `ValueError("mismatch between the number of fields and the number of arrays")`，字段数与列数必须对齐。
- subarray 字段（如 `'(2,3)f8'`）：先用第 0 字段子数组形状把总 shape 裁成「记录形状」，再逐列把每列末尾的子数组维裁掉来校验一致性。
- 装填 `_array[name] = obj` 走 `ndarray.__setitem__`，**总是拷贝**，结果不与源数组共享内存；属性式 `_array.name = obj` 则走 `recarray.__setattr__` → `setfield`，殊途同归（魔法细节见 u3-l2）。

## 7. 下一步学习建议

本讲把「装数据」讲完了，但 `recarray` 作为 `ndarray` 子类的**构造与属性访问魔法**我们只是顺手用到，还没展开。建议接着学：

- **u3-l1（recarray 类的 `__new__` 构造与 `__array_finalize__`）**：本讲的 `recarray(shape, descr)` 就是直接调它。去读 [records.py:385-413](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L385-L413)，理解 `(record, descr)` 这个二元 dtype 如何让标量记录变成 `numpy.record`，以及 `__array_finalize__` 如何在视图操作时把 `void` 自动提升成 `record`。
- **u3-l2（属性访问魔法 `__getattribute__`/`__setattr__`）**：本讲末尾对比的两条赋值路径，其「点号赋值」那条的完整逻辑在那里讲透。
- **u4-l1（fromrecords）**：去看 [records.py:708-714](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L708-L714) 那条慢速路径——`fromrecords` 在无 dtype 时把行数据逐列拆成 `arrlist` 后**转手调用本讲的 `fromarrays`**，你会看到本讲函数如何被复用。

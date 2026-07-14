# `__repr__` 打印格式与 legacy 打印模式

> 本讲属于「专家层」第五单元第二篇。真实实现全部在 [`numpy/_core/records.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py)（`recarray.__repr__` 与 `record.__repr__`/`__str__`），打印引擎在 [`numpy/_core/arrayprint.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py)。`numpy/rec/__init__.py` 只是再导出垫片（详见 u1-l1）。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `repr(recarray)` 为什么有时以 `rec.array(` 开头、有时却以 `array(...).view(numpy.recarray)` 结尾——判定条件完全取决于 `dtype.type`；
- 解释打印前 `recarray.__repr__` 为什么要做一次 `sb.dtype((nt.void, repr_dtype))` 的「dtype 反转」，它和 u5-l1 讲过的二元 dtype 是什么关系；
- 描述 `sb.array2string(...)` 在 `__repr__` 里扮演的角色：`prefix`/`suffix` 如何对齐换行、`separator=', '` 如何决定元素分隔、以及空数组为什么会走 `[], shape=...` 这条特殊分支；
- 理解 `_get_legacy_print_mode()` 这个整数开关：为什么阈值恰好是 `113`，以及 `legacy='1.13'` 下 `record` 标量的 `__repr__`/`__str__` 会退化成「取 tuple 再 `str()`」的旧行为。

本讲的核心是一条**判定链**：

\[
\texttt{dtype.type} \;\longrightarrow\; \text{前缀分支} \;\longrightarrow\; \text{最终 repr 字符串}
\]

整篇讲义就是在回答：当你对一个 record array 调用 `repr()`（或交互式回车）时，NumPy 是如何把「字段数据 + dtype + 换行缩进」拼成那一段可粘贴回 `rec.array(...)` 的文本的。

## 2. 前置知识

本讲默认你已经读过 u3-l2（`__getattribute__`/`__setattr__` 属性访问魔法）和 u5-l1（视图、拷贝与 record/void dtype 转换）。回顾三条关键事实：

1. **结构化 recarray 的 `dtype.type` 恒为 `numpy.record`。** 这是 u5-l1 反复强调的不变量，由 `__array_finalize__` 与 `__setattr__` 维持。本讲的 `__repr__` 正是靠 `self.dtype.type is record` 来识别「这是一个正经的 record array」。参见 [`record(nt.void)` 类定义（records.py:196）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L196)。

2. **二元 dtype `(record, descr)` 把 `void` 换成 `record`，反向 `(void, rec_descr)` 再换回来。** 本讲会用到这个「逆操作」：打印时把 record 的 dtype **换回 void 形式**，让输出的 `dtype=...` 片段干净、可粘贴回 `rec.array()`。这是 u5-l1 第 4.2 节练习 1 的直接延伸。

3. **`record` 是标量类型，`recarray` 是数组类型，二者是两个独立维度。** `repr()` 对这两个层级各有一套打印逻辑：数组级走 `recarray.__repr__`，标量级走 `record.__repr__`/`__str__`。本讲两条线都会讲。

一个常被忽略的小事实：`recarray.__repr__` 是**专门重写**的，它没有沿用 `ndarray.__repr__`，而是自己拼字符串（因为要带 `rec.array(` 前缀、要做 dtype 反转）。理解这一点，才能理解为什么 record array 的 repr 和普通结构化 ndarray 的 repr 长得不一样。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`numpy/_core/records.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py) | 全部实现。本讲聚焦 [`recarray.__repr__`（503-537）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L503-L537) 与 [`record.__repr__`/`__str__`（205-213）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L205-L213)，以及顶部的 [`from .arrayprint import _get_legacy_print_mode`（第 12 行）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L12)。 |
| [`numpy/_core/arrayprint.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py) | 打印引擎。本讲用到 [`array2string`（644-810）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L644-L810)、[`_get_legacy_print_mode`（391-393）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L391-L393)、legacy 字符串到整数的[映射表（79-101、384-387）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L79-L101)，以及用户入口 [`set_printoptions`（123）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L123) / [`printoptions` 上下文管理器（396-399）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L396-L399)。 |

## 4. 核心概念与源码讲解

### 4.1 `recarray.__repr__` 的两套前缀：按 `dtype.type` 分发

#### 4.1.1 概念说明

在交互式环境里敲一个变量名回车，Python 会调用它的 `__repr__` 得到一段「尽量能复现对象」的文本。NumPy 对普通 ndarray 的 repr 是 `array([...], dtype=...)`；而 record array 想要的是 `rec.array([...], dtype=...)`——这样用户直接把输出粘贴回去就能重建一个 record array。

但事情没那么简单：一个 `recarray` 实例的 `dtype.type` 并不总是 `record`。绝大多数情况下它是 `record`（正常 record array），但用户也可能「玩花活」——比如把一个裸 `void` 视图（`view('V8')`）当成 recarray，此时 `dtype.type` 会是 `numpy.void` 而不是 `record`（见 u5-l1 第 4.3 节，裸 void 因 `names is None` 不被 `__array_finalize__` 提升）。`__repr__` 必须对所有这些情况都给出合理、可粘贴的输出，于是它按 `dtype.type` 分成两套前缀。

#### 4.1.2 核心流程

[`recarray.__repr__`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L503-L537) 第一步是判定 `dtype.type`，决定前缀和最终模板 `fmt`。判定是一个 **OR 条件**：

\[
\text{if}\quad \bigl(\,\texttt{dtype.type is record}\,\bigr)\;\;\text{or}\;\;\bigl(\,\texttt{not issubclass(dtype.type, void)}\,\bigr)
\]

满足这个 OR，走 **`rec.array(` 前缀**分支；否则（`dtype.type` 是 void 的子类、但又不是 record）走 **`array(...).view(numpy.recarray)` 前缀**分支。展开成三种实际情形：

| 情形 | `dtype.type` | `is record` | `issubclass(void)` | 前缀模板 | 是否反转 dtype |
| --- | --- | --- | --- | --- | --- |
| ① 正经 record array | `numpy.record` | ✓ | ✓ | `rec.array(%s,...,dtype=%s)` | **是**，反转回 void |
| ② 非 void 标量 dtype | 如 `numpy.float64` | ✗ | ✗ | `rec.array(%s,...,dtype=%s)` | 否 |
| ③ 「玩花活」：void 子类但非 record | `numpy.void`（裸 `V8`） | ✗ | ✓ | `array(%s,...,dtype=%s).view(numpy.recarray)` | 否 |

第 ① 种是日常最常见的；第 ② 种是防御性分支（recarray 的 dtype 理论上可以是非结构化标量，比如 `np.array([1.0,2.0]).view(np.recarray)`，`__array_finalize__` 因 `issubclass(void)` 不成立而不提升）；第 ③ 种是源码注释里说的「strange games」——只有当用户刻意构造出「标量是 void、又不是 record」的 recarray 时才会触发。

第 ① 种情形里还有一个关键动作：**dtype 反转**。因为 `rec.array()` 本身会把传入的 dtype 自动提升为 record（见 u4-4 的 `array` 调度），所以在 repr 里若直接打印 record 形式的 dtype，反而不能干净地粘贴回去。于是源码做了一次「换标量类型」的逆操作：

```python
if repr_dtype.type is record:
    repr_dtype = sb.dtype((nt.void, repr_dtype))   # 把 record 换回 void
prefix = "rec.array("
fmt = 'rec.array(%s,%sdtype=%s)'
```

这正是 u5-l1 讲过的二元 dtype：`np.dtype((void, rec_dtype))` 保留字段结构、偏移、itemsize 不变，只把 `.type` 从 `record` 退回 `void`。打印出来的 `dtype=[('x','<f8'),...]` 干净利落，可粘贴。

#### 4.1.3 源码精读

- [`recarray.__repr__` 的前缀判定与 dtype 反转（records.py:503-524）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L503-L524)：`if self.dtype.type is record or not issubclass(self.dtype.type, nt.void)` 决定走哪条分支；分支内 `if repr_dtype.type is record: repr_dtype = sb.dtype((nt.void, repr_dtype))` 是 dtype 反转，注释原文「Since rec.array converts dtype to a numpy.record for us, convert back to non-record before printing」直接说明了动机。
- [`sb` 与 `nt` 的别名（records.py:11）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L11)：`from . import numeric as sb, numerictypes as nt`——`sb.dtype` 即 `np.dtype`，`nt.void` 即 `numpy.void`。
- 分支 ③ 的 else 模板 `'array(%s,%sdtype=%s).view(numpy.recarray)'`（[records.py:519-524](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L519-L524)）：末尾 `.view(numpy.recarray)` 正好对应「这个数组的 dtype.type 还不是 record，需要再 view 一次才能成为正经 record array」的语义。

#### 4.1.4 代码实践

1. **目标**：亲手触发三种前缀分支，对照上表验证 `dtype.type` 与前缀的对应关系。
2. **操作步骤**：

   ```python
   import numpy as np

   # 情形 ①：正经 record array（dtype.type is record）
   r1 = np.rec.array([(1, 2.0), (3, 4.0)], dtype='i4,f8')
   print('r1.dtype.type =', r1.dtype.type)        # numpy.record
   print(repr(r1))                                # rec.array(... 前缀

   # 情形 ③：把裸 void 视图当成 recarray（dtype.type 是 void，不是 record）
   r3 = np.rec.array(np.ones(2, dtype='i4,i4')).view('V8')
   print('r3.dtype.type =', r3.dtype.type)        # numpy.void  ← names 为 None，未被提升
   print(repr(r3))                                # array(...).view(numpy.recarray) 前缀
   ```

3. **预期现象**：`repr(r1)` 以 `rec.array(` 开头，且 `dtype=` 后面是 `void` 形式（如 `dtype=[('f0','<i4'),('f1','<f4')]`，看不到 `record` 字样）——这正是 dtype 反转的效果；`repr(r3)` 以 `array(` 开头、以 `.view(numpy.recarray)` 结尾。两段输出都可整体粘贴回 Python 重建对象（待本地验证具体 dtype 字符串）。
4. **进阶探究**：补一个情形 ②，看它仍走 `rec.array(` 前缀但不做 dtype 反转：

   ```python
   r2 = np.array([1.0, 2.0]).view(np.recarray)
   print('r2.dtype.type =', r2.dtype.type)        # numpy.float64
   print(repr(r2))                                # 仍 rec.array( 前缀，dtype=float64
   ```

#### 4.1.5 小练习与答案

- **练习 1**：为什么情形 ① 要把 dtype 从 record 反转回 void 再打印，而情形 ③ 不做任何 dtype 处理？
  - **答案**：情形 ① 用 `rec.array(` 前缀，而 `rec.array()` 调度时本就会把 dtype 自动提升成 record（见 u4-4），所以打印 void 形式更干净、粘贴回去能正确重建；情形 ③ 的 `dtype.type` 本就是 void（不是 record），没有「record 外壳」需要剥，直接打印即可，再用 `.view(numpy.recarray)` 把它升成 record array。
- **练习 2**：`sb.dtype((nt.void, repr_dtype))` 这一行的「逆操作」在 u5-l1 里对应哪段代码？
  - **答案**：对应 u5-l1 的 `sb.dtype((record, ...))`（如 [`__array_finalize__` 的 records.py:413](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L413)）。一个是 `void→record`（贴身份），一个是 `record→void`（打印时剥身份），方向相反、原子操作相同。

---

### 4.2 `array2string` 拼接：数据串、prefix 对齐与空数组特例

#### 4.2.1 概念说明

前缀和 dtype 决定后，`__repr__` 还需要把「数组里的字段数据」渲染成一段字符串 `lst`，再套进模板 `fmt`。这段数据串不是手写的，而是委托给通用打印引擎 [`sb.array2string`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L644-L810)（即 `np.array2string`）。`recarray.__repr__` 给它传了三个关键参数：`separator=', '`（元素用逗号+空格分隔）、`prefix=prefix`（用于续行对齐）、`suffix=','`（用于行宽计算时扣除末尾逗号）。

此外还有一个**空数组特例**：当数组完全没有元素、且 shape 不是 `(0,)`（即不是「一维零长度」）时，`__repr__` 跳过 `array2string`，直接打印 `[], shape=<shape>`。这是为了让 `rec.array(np.zeros((0,3), dtype=...))` 这类多维空数组也能清楚显示其形状。

#### 4.2.2 核心流程

数据串的生成分两条路径：

\[
\text{lst} =
\begin{cases}
\texttt{sb.array2string(self, separator=', ', prefix=prefix, suffix=',')} & \text{if } \texttt{self.size > 0}\;\text{or}\;\texttt{self.shape == (0,)} \\
\texttt{f'[], shape=\{repr(self.shape)\}'} & \text{otherwise}
\end{cases}
\]

注意判定的细节：

- **`self.size > 0`**：有任何元素，正常渲染。
- **`self.shape == (0,)`**：一维零长度数组（`size == 0` 但 shape 恰好是 `(0,)`）仍走 `array2string`，打印成 `[]`（不带 shape 注释）——因为 `(0,)` 的形状已被 `[]` 本身隐含表达。
- **其它 `size == 0`**（如 `(0, 3)`、`(2, 0)`）：走特例分支，打印 `[], shape=(0, 3)`，把退化掉的维度显式标注出来。

拿到 `lst` 后，`__repr__` 计算一个**换行缩进串** `lf`：

```python
lf = '\n' + ' ' * len(prefix)     # 换行 + 与 prefix 等宽的缩进
if _get_legacy_print_mode() <= 113:
    lf = ' ' + lf                 # legacy 下额外加一个尾随空格
return fmt % (lst, lf, repr_dtype)
```

`len(prefix)` 对 `rec.array(` 是 10，于是 `dtype=` 会缩进到第 10 列、正好对齐在 `(` 之后；对 `array(` 是 6。最终模板 `'rec.array(%s,%sdtype=%s)'` 把 `lst`、`lf`、`repr_dtype` 三段拼起来，得到多行 repr。

`array2string` 内部对 `prefix`/`suffix` 的使用见[其文档（arrayprint.py:668-677）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L668-L677)：`prefix` 的长度用于左对齐与续行缩进，`suffix` 的长度在现代模式下会从行宽里扣除（`linewidth -= len(suffix)`，[arrayprint.py:803-804](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L803-L804)），让换行时机准确。

#### 4.2.3 源码精读

- [`array2string` 调用点（records.py:527-529）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L527-L529)：`sb.array2string(self, separator=', ', prefix=prefix, suffix=',')`。注意 `separator=', '` 是 record array 特有的（`array2string` 默认 `separator=' '`），这就是 record array 元素之间是逗号分隔的原因。
- [空数组特例分支（records.py:530-532）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L530-L532)：`lst = f"[], shape={repr(self.shape)}"`，注释「show zero-length shape unless it is (0,)」。
- [换行缩进 `lf` 的计算与 legacy 尾随空格（records.py:534-537）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L534-L537)：`lf = '\n' + ' ' * len(prefix)`，legacy ≤113 时前置一个空格。
- [`array2string` 函数体（arrayprint.py:644-810）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L644-L810)：合并用户传入参数与全局 `format_options`（[793-797](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L793-L797)），处理 legacy 0d 特例（[799-801](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L799-L801)）、`size == 0` 时返回 `"[]"`（[807-808](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L807-L808)），否则委托 `_array2string`（[810](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L810)）。注意：`array2string` 自己的 `size==0 → "[]"` 与 `__repr__` 的空数组特例分工不同——前者管单维 `[]`，后者管多维空 shape 的标注。

#### 4.2.4 代码实践

1. **目标**：观察 `(0,)` 与 `(0, N)` 两种空数组在 `__repr__` 下的不同输出，验证空数组特例分支。
2. **操作步骤**：

   ```python
   import numpy as np

   # 一维零长度：shape==(0,) -> 走 array2string，打印 '[]'，无 shape 注释
   a0 = np.rec.array(np.zeros(0, dtype='i4,f8'))
   print('shape=', a0.shape, '| size=', a0.size)
   print(repr(a0))

   # 多维空：shape==(0,3) -> 走特例分支，打印 '[], shape=(0, 3)'
   a03 = np.recarray((0, 3), dtype='i4,f8')
   print('shape=', a03.shape, '| size=', a03.size)
   print(repr(a03))
   ```

3. **预期结果（待本地验证）**：`a0` 的 repr 形如 `rec.array([], dtype=...)`（只有 `[]`，没有 `shape=`）；`a03` 的 repr 形如 `array([], shape=(0, 3), dtype=...).view(numpy.recarray)`——注意它因 `size==0` 且 `shape != (0,)` 走了特例 `lst`，又因为 dtype 是默认 void（`np.recarray(...)` 直接构造的多维空数组，`dtype.type` 未必是 record）可能走分支 ③。两相对照即可看到「shape 标注只在多维空数组出现」。
4. **观察建议**：把上面的 `repr(a03)` 输出整体复制、粘回 Python 解释器，验证它能否重建一个等价对象——这正是 `__repr__` 设计成「可粘贴」的初衷。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `recarray.__repr__` 要把 `separator` 显式设为 `', '`，而不是用 `array2string` 的默认 `' '`？
  - **答案**：record array 的每条记录是一个结构化标量，`repr` 想让它看起来像 `rec.array([(1, 2.), (3, 4.)], ...)` 这种 Python 列表字面量风格，元素间需要逗号；而 `array2string` 默认的空格分隔是给普通数值数组用的（如 `[1 2 3]`）。
- **练习 2**：一个 `shape=(2, 0)` 的 recarray（`size==0` 但 shape 不是 `(0,)`），它的 `repr` 里会出现 `shape=` 吗？
  - **答案**：会。判定条件是 `self.size > 0 or self.shape == (0,)`，`(2,0)` 既不满足 `size>0` 也不等于 `(0,)`，故走特例分支，`lst` = `[], shape=(2, 0)`。

---

### 4.3 legacy 打印模式：`_get_legacy_print_mode` 与 `record.__repr__`/`__str__`

#### 4.3.1 概念说明

NumPy 的打印格式随版本演进过多轮（1.13、1.21、1.25、2.1、2.2……）。为了不破坏老代码的字符串比对与 doctest，NumPy 提供了一个「legacy 打印模式」开关：用户可以用 `np.set_printoptions(legacy='1.13')`（或上下文管理器 `np.printoptions(legacy='1.13')`）切回旧版打印风格。

在 records.py 里，legacy 模式影响两处：一是上一节看到的 `lf` 尾随空格（[records.py:535-536](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L535-L536)），二是 `record` 标量的 `__repr__`/`__str__`——在 legacy ≤113 时，标量打印会退化成「取 Python tuple 再 `str()`」的旧行为。本模块讲透这个开关的取值与阈值。

#### 4.3.2 核心流程

legacy 模式在内部用一个**整数**表示，由 [`_get_legacy_print_mode()`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L391-L393) 从线程局部的 `format_options` 里取出：

```python
def _get_legacy_print_mode():
    """Return the legacy print mode as an int."""
    return format_options.get()['legacy']
```

字符串到整数的映射在 [`set_printoptions`/`_make_options_dict`（arrayprint.py:79-101）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L79-L101)：

| 用户传入 | 内部整数 | 含义 |
| --- | --- | --- |
| `legacy='1.13'` | `113` | 最旧的兼容模式 |
| `legacy='1.21'` | `121` | — |
| `legacy='1.25'` | `125` | — |
| `legacy='2.1'` | `201` | — |
| `legacy='2.2'` | `202` | — |
| `legacy=False` | `sys.maxsize` | 关闭 legacy（最现代） |
| 不传（默认） | `sys.maxsize` | 同上 |

反向（整数→字符串）的[映射表在 arrayprint.py:384-387](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L383-L388)。

records.py 里所有 legacy 判定都写成 `_get_legacy_print_mode() <= 113`。注意阈值是 **`113`**：意味着只有 `'1.13'` 这一个模式会触发旧逻辑，`'1.21'`/`'1.25'`/`'2.1'`/`'2.2'` 全都大于 113，对 records.py 的这几处而言**行为等同于现代模式**。

`record` 标量的两级打印在 legacy 下退化得很明显：

```python
def __repr__(self):
    if _get_legacy_print_mode() <= 113:
        return self.__str__()
    return super().__repr__()

def __str__(self):
    if _get_legacy_print_mode() <= 113:
        return str(self.item())     # 取出 tuple，再 str()
    return super().__str__()
```

- **现代模式（>113）**：`__repr__` 与 `__str__` 都走 `super()`，即 `nt.void`（C 层）的实现，输出带字段结构的结构化标量表示。
- **legacy ≤113**：`__str__` 退化为 `str(self.item())`——`record.item()` 把结构化标量转成一个 Python tuple（各字段值按顺序），再对 tuple 取 `str()`；`__repr__` 则直接委托 `__str__`，于是 `repr(record标量) == str(record标量)`。这就是老版本里 record 标量打印成像 `(1, 2.0)` 这种 tuple 字面量的原因。

#### 4.3.3 源码精读

- [`record.__repr__` 与 `record.__str__`（records.py:205-213）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L205-L213)：legacy ≤113 时 `__str__` 走 `str(self.item())`、`__repr__` 委托 `__str__`；否则 `super()` 走 `nt.void`。
- [`_get_legacy_print_mode` 定义（arrayprint.py:391-393）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L391-L393)：从 `format_options` 线程局部存储取整数。
- [legacy 字符串→整数映射（arrayprint.py:79-101）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L79-L101)：`'1.13'→113`、`'1.21'→121`、…、`False→sys.maxsize`。
- [整数→字符串反向映射表（arrayprint.py:383-388）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L383-L388)：`{113:'1.13', 121:'1.21', ...}`。
- 用户入口：[`set_printoptions`（arrayprint.py:123）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L123) 全局设置；[`printoptions` 上下文管理器（arrayprint.py:396-399）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L396-L399) 临时设置（`with np.printoptions(legacy='1.13'): ...`）。
- records.py 顶部的[导入（第 12 行）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L12)：`from .arrayprint import _get_legacy_print_mode`，这是 records.py 与打印引擎唯一的耦合点。

#### 4.3.4 代码实践

1. **目标**：切换 legacy 模式，对比 `record` 标量与 `recarray` 在两种模式下的打印差异。
2. **操作步骤**：

   ```python
   import numpy as np

   r = np.rec.array([(1, 2.0), (3, 4.0)], dtype='i4,f8')

   # 现代模式（默认）
   print('--- modern ---')
   print('scalar str :', str(r[0]))
   print('scalar repr:', repr(r[0]))
   print('array  repr:', repr(r))

   # legacy 1.13 模式（用上下文管理器临时切换）
   print('--- legacy 1.13 ---')
   with np.printoptions(legacy='1.13'):
       print('scalar str :', str(r[0]))
       print('scalar repr:', repr(r[0]))
       print('array  repr:', repr(r))
   ```

3. **预期现象（待本地验证）**：
   - 现代模式下，`r[0]` 的 `str`/`repr` 是 `nt.void` 的结构化表示；
   - legacy 1.13 下，`r[0]` 的打印退化为 `str(r[0].item())`，即形如 `(1, 2.0)` 的 tuple 字面量风格，且 `repr(r[0]) == str(r[0])`；
   - legacy 1.13 下，`repr(r)` 的换行处会多一个尾随空格（`lf = ' ' + lf` 的效果），与 1.13 旧版逐字节兼容（这对依赖 repr 字符串的 doctest 很重要）。
4. **验证阈值**：把 `legacy='1.13'` 换成 `legacy='1.21'` 再跑一次，观察标量打印是否**回到现代风格**——这印证了阈值 `<=113` 只命中 `'1.13'` 一个模式。

#### 4.3.5 小练习与答案

- **练习 1**：`np.printoptions(legacy='2.2')` 下，`record` 标量的 `__str__` 走哪条分支？
  - **答案**：走 `super().__str__()`（现代分支）。因为 `'2.2'→202`，`202 <= 113` 为假。records.py 里所有 legacy 判定都以 `113` 为阈值，只有 `'1.13'`（=113）命中旧逻辑。
- **练习 2**：为什么 `record.__repr__` 在 legacy 下要 `return self.__str__()`，而不是 `return str(self.item())`？
  - **答案**：为了保证「legacy 下 `repr` 与 `str` 输出完全一致」（老版本就是这行为）。`__str__` 内部已经做了 `str(self.item())`，`__repr__` 直接复用 `__str__` 即可，避免逻辑重复；如果 `__repr__` 另写一份 `str(self.item())`，未来若 `__str__` 的退化策略调整，两处容易不一致。

## 5. 综合实践

把本讲三个模块串起来，写一个「repr 体检」脚本：构造四种典型 recarray，分别打印它们的 `dtype.type` 与 `repr()`，再切到 legacy 1.13 复印一次，对照源码逐条解释你看到的前缀、dtype 形式、换行缩进与标量风格。

```python
import numpy as np

def show(tag, arr):
    print(f"\n=== {tag} ===")
    print("dtype.type :", arr.dtype.type)
    print("repr      :", repr(arr))

# ① 正经 record array
show("① 正经 record",
     np.rec.array([(1, 2.0), (3, 4.0)], dtype='i4,f8'))

# ② 非 void 标量 dtype 的 recarray
show("② 非 void 标量",
     np.array([1.0, 2.0]).view(np.recarray))

# ③ 裸 void 视图当 recarray（strange games）
show("③ 裸 void 视图",
     np.rec.array(np.ones(2, dtype='i4,i4')).view('V8'))

# ④ 多维空数组（走 [], shape=... 特例）
show("④ 多维空数组",
     np.recarray((0, 3), dtype='i4,f8'))

# 再切 legacy 1.13，对比标量与换行
r = np.rec.array([(1, 2.0), (3, 4.0)], dtype='i4,f8')
print("\n=== legacy 1.13 标量 ===")
with np.printoptions(legacy='1.13'):
    print("str (r[0]) :", str(r[0]))
    print("repr(r[0]):", repr(r[0]))
```

**关注点**：

1. ① 与 ② 都以 `rec.array(` 开头，但 ① 的 `dtype=` 是 void 形式（dtype 反转生效）、② 是 `float64`（无 record 外壳可剥）；
2. ③ 以 `array(` 开头、`.view(numpy.recarray)` 结尾，对应「标量是 void 但不是 record」的分支；
3. ④ 出现 `[], shape=(0, 3)`，印证多维空数组特例；
4. legacy 1.13 下 `repr(r[0]) == str(r[0])`，且数组 repr 换行处有尾随空格。

> 说明：以上代码片段为「示例代码」，便于你理解调用链；运行具体输出请以本地环境为准（待本地验证）。

## 6. 本讲小结

- `recarray.__repr__` 按 `dtype.type` 分发前缀：`is record` 或非 void 子类 → `rec.array(...)` 前缀；是 void 子类但非 record → `array(...).view(numpy.recarray)` 前缀（源码注释称之为「strange games」）。
- 正经 record array 在打印前会做一次 dtype 反转 `sb.dtype((nt.void, repr_dtype))`——把 record 外壳剥回 void，让输出的 `dtype=` 干净可粘贴回 `rec.array()`；这是 u5-l1 二元 dtype `(record, ...)` 的逆操作。
- 数据串由 `sb.array2string(self, separator=', ', prefix=prefix, suffix=',')` 渲染；`separator=', '` 是 record array 用逗号分隔的原因，`prefix` 决定 `dtype=` 的续行缩进（`'\n' + ' '*len(prefix)`）。
- 多维空数组走特例分支 `lst = "[], shape=" + repr(self.shape)`，只在 `size==0` 且 `shape != (0,)` 时触发；一维零长度 `(0,)` 仍走 `array2string` 打印 `[]`。
- legacy 模式在内部是整数，由 `_get_legacy_print_mode()` 从线程局部 `format_options` 取出；records.py 的阈值统一是 `<= 113`，只命中 `'1.13'`——此时 `record.__str__` 退化为 `str(self.item())`、`__repr__` 委托 `__str__`，且 `__repr__` 换行处多一个尾随空格，以逐字节兼容旧版 doctest。

## 7. 下一步学习建议

- 下一篇 **u5-l3（测试套件与常见陷阱）** 会带你看 `numpy/_core/tests/test_records.py`，其中多组用例（如 record 属性访问、2D fromrecords、pickle 往返）正好可作为本讲打印行为的验证素材——你可以用本讲学到的「前缀判定」去预测这些测试里 `repr` 的输出。
- 想深入打印引擎本身，可顺着本讲的 [`array2string`（arrayprint.py:644-810）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/arrayprint.py#L644-L810) 继续读 `_array2string`、`FloatingFormat` 与 `_get_format_function`，理解精度、`floatmode`、行宽换行的完整机制。
- 回顾 u5-l1 第 4.2 节，巩固「二元 dtype 换标量类型」这一贯穿 record 机制的原子操作——本讲的 dtype 反转正是它的反向应用。

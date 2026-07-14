# 对齐与填充类：center / ljust / rjust / zfill / expandtabs

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `np.strings.center` / `ljust` / `rjust` / `zfill` / `expandtabs` 这五个函数在 Python 包装层里共享的「四步套路」是什么。
- 解释为什么定长 dtype（`str_` / `bytes_`）下，Python 层必须先用 `str_len` 预计算输出宽度、再 `np.empty_like` 预分配 `out`、最后才把 `out` 交给 C 层 ufunc。
- 看懂「StringDType（`'T'`）快速路径」与「定长预算路径」的分支条件差异：为什么 `center/ljust/rjust` 用 `np.result_type(a, fillchar).char == "T"`，而 `zfill/expandtabs` 用 `a.dtype.char == "T"`。
- 理解 `fillchar` 必须恰好一个字符的校验逻辑，以及 `zfill` 为什么没有 `fillchar` 参数却能把 `0` 插在符号 `+`/`-` 之后。
- 看懂 `expandtabs` 独有的「两遍式」设计：先用 `_expandtabs_length` 量出每个元素的输出长度，再用 `_expandtabs` 填充。

## 2. 前置知识

本讲承接 u1-l1（门面架构）、u1-l2（三种字符串 dtype 与 `str_len`）、u2-l4（`set_module` 与 `array_function_dispatch` 装饰器）、u2-l5（三个辅助函数与 dtype 分发套路）、u2-l8（`_vec_string` 委托）。在进入正文前，请确认你已经理解下面几个概念：

- **三种字符串 dtype**：变长 `StringDType`（`dtype.char == 'T'`，内部 UTF-8 动态存储）、定长 `bytes_`（`'S'`，1 字符 = 1 字节）、定长 `str_`（`'U'`，UCS4，1 字符固定 4 字节）。
- **`str_len`**：一个真正的 ufunc（由 `numpy._core.umath` 导入），逐元素返回字符串的**实际字符数**，对三种 dtype 语义统一。
- **`@set_module('numpy.strings')` + `@array_function_dispatch(dispatcher)`**：前者把函数的 `__module__` 改写成门面模块名（管「身份」），后者用 C 对象包住实现函数、启用 NEP-18 `__array_function__` 分发（管「行为」）；`dispatcher` 只声明「哪些参数相关」，签名要和主函数一致，可选参数默认值只能是 `None`。
- **输出 dtype 路径 A**（u2-l5 总结的四条路径之一）：当输出宽度不能仅由输入 dtype 决定、但能由输入数据预算出来时，Python 层先用 `str_len` 预算宽度、拼出一个定长 `out_dtype`、`empty_like` 预分配 `out`，再交给 C 层 ufunc 写入。本讲的五个函数几乎全部走这条路径。

一个反复出现的核心难点是：**C 层的定长 ufunc 没法从「输入 dtype」推断出「输出宽度」**。比如 `np.strings.center(['abc'], 10)` 的输入是 `<U3`，输出却是 `<U10`，这个 `10` 来自调用者传入的 `width` 参数，而不是输入 dtype。`resolve_descriptors`（C 层决定输出 dtype 的钩子）拿不到这个语义，于是 Python 层就承担起「量尺寸、开缓冲区」的责任——这正是本讲所有函数长得几乎一模一样的原因。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [numpy/strings/__init__.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/strings/__init__.py#L1-L2) | 门面文件，两行 `import *`，本讲的五个函数都经它转发，自身无实现（u1-l1 已讲）。 |
| [numpy/_core/strings.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py) | **本讲主角**。所有 Python 包装函数的实现都在这里。 |
| numpy/_core/umath（C 扩展） | 提供 `_center` / `_ljust` / `_rjust` / `_zfill` / `_expandtabs` / `_expandtabs_length` 这几个「私有 ufunc」，是 Python 层最终委托的对象（它们的 C 循环注册在 u3-l12 讲）。 |
| [numpy/_core/tests/test_strings.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py) | 覆盖三种 dtype 的参数化测试，是本讲实践任务的依据。 |

本讲涉及的 Python 函数在 `strings.py` 中的行号一览：

- `_expandtabs_dispatcher` / `expandtabs`：约 L630 / L636
- `_just_dispatcher` / `center` / `ljust` / `rjust`：L687 / L693 / L762 / L827
- `_zfill_dispatcher` / `zfill`：L890 / L896

## 4. 核心概念与源码讲解

### 4.1 共享的四步套路与 `str_len` 的角色

#### 4.1.1 概念说明

`center` / `ljust` / `rjust` / `zfill` / `expandtabs` 这五个函数虽然语义不同（居中、左对齐、右对齐、左补零、制表符展开），但在 Python 包装层里，它们解决的是**同一个工程问题**：定长 dtype 下，输出宽度由调用参数（`width` / `tabsize`）和数据内容决定，C 层 ufunc 无法自行推断。于是它们都采用同一套「四步套路」：

1. **校验参数类型**（`width` 必须是整数；`fillchar` 必须恰好一个字符）。
2. **分流**：若结果是变长 `StringDType`（`'T'`），直接调 C 层 ufunc 返回（变长存储由 C 层自行分配，Python 无需预算）。
3. **预算输出宽度**：用 `str_len` 量出每个元素所需的目标宽度，取全数组最大值，拼出定长 `out_dtype`（形如 `"U10"` / `"S7"`）。
4. **预分配 `out` 并写入**：`np.empty_like` 开缓冲区，把 `out=` 交给 C 层 ufunc。

这正是 u2-l5 归纳的**输出 dtype 路径 A**。`str_len` 在其中扮演「尺子」：它量出每个字符串的实际字符数，配合 `width` 取上界，得到该元素的输出宽度。

#### 4.1.2 核心流程

四步套路的伪代码（以定长分支为准）：

```
def 某对齐函数(a, width, ...):
    width = np.asanyarray(width)
    校验 width 是整数        # 第 1 步
    a = np.asanyarray(a)
    若涉及 fillchar：校验 str_len(fillchar) == 1
    若 result_type 是 'T'：return C层ufunc(a, width, ...)   # 第 2 步：变长快速路径
    若涉及 fillchar：fillchar = fillchar.astype(a.dtype)    # 第 3 步：预算
    目标宽度 = np.maximum(str_len(a), width)   # 输出至少和字符串一样长
    out_dtype = f"{a.dtype.char}{目标宽度.max()}"
    out = np.empty_like(a, shape=广播shape, dtype=out_dtype)  # 第 4 步：预分配 + 写入
    return C层ufunc(a, width, ..., out=out)
```

两个关键细节：

- **`np.maximum(str_len(a), width)`**：当 `width < str_len(a)` 时，输出宽度就是字符串自身长度（即「不截断、不填充」），所以 `center('abc', 2)` 的结果仍是 `'abc'` 而不是 `'ab'`。这与 Python `str.center` 的行为一致。
- **`out_dtype = f"{a.dtype.char}{宽度.max()}"`**：注意是 `.max()`——整个数组共用**一个**定宽 dtype，宽度取所有元素中的最大值。较短的元素会在 C 层用空格/`0`/`fillchar` 补齐到这个统一宽度。这也是定长 dtype 必须「统一宽度」的本质约束。

#### 4.1.3 源码精读

这些 C 层「私有 ufunc」是从 `numpy._core.umath` 导入的（注意它们都带下划线前缀，表示私有）：

[numpy/_core/strings.py:L22-L58](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L22-L58) —— 导入 `_center`、`_ljust`、`_rjust`、`_zfill`、`_expandtabs`、`_expandtabs_length` 以及 `str_len`。

其中 `str_len` 和 `isalpha` 等裸 ufunc 一样，无法被 `@set_module` 装饰，改由 `_override___module__()` 在 import 时就地改写 `__module__`：

[numpy/_core/strings.py:L61-L70](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L61-L70) —— 把 `str_len` 等的 `__module__` 改成 `"numpy.strings"`，使其对外归属正确。

#### 4.1.4 代码实践

**实践目标**：脱离 `np.strings`，手动复刻「预算宽度 + 构造 out + 写入」这三步，体会 Python 层为什么要这样做。

**操作步骤**（示例代码，非项目原有代码）：

```python
import numpy as np
from numpy._core.umath import _ljust   # 直接借用 C 层 ufunc

a = np.array(['abc', 'xy'])
width = 10

# 第 3 步：预算输出宽度（每个元素取 max(自身长度, width)，再取全数组最大值）
target = np.maximum(np.strings.str_len(a), width)
out_dtype = f"{a.dtype.char}{target.max()}"      # -> 'U10'
out = np.empty_like(a, shape=a.shape, dtype=out_dtype)

# 第 4 步：把预分配的 out 交给 C 层 ufunc
result = _ljust(a, width, np.array(' '), out=out)
print(repr(out_dtype), repr(result))
```

**需要观察的现象**：`out_dtype` 是 `'U10'`；`result` 是 `array(['abc       ', 'xy        '], dtype='<U10')`，两个元素都被补齐到统一的 10 字符宽。

**预期结果**：与 `np.strings.ljust(a, 10)` 完全一致。这说明 `np.strings.ljust` 在定长分支里做的，正是上面这三步加一层参数校验。

> 若本地 NumPy 版本/构建不同导致 `from numpy._core.umath import _ljust` 不可用，则该导入步骤「待本地验证」；可改为只运行 `np.strings.ljust(a, 10)` 并观察其 `dtype` 为 `<U10`，同样能验证「统一宽度」这一结论。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `out_dtype` 用的是 `宽度.max()` 而不是 `宽度`？
**答案**：`宽度` 是逐元素的数组（每个元素可能不同），而定长 dtype 只能有一个统一的字段宽度。为了让最长的那个元素放得下，必须取全数组的最大值作为统一宽度，较短的元素在 C 层补齐。

**练习 2**：`np.strings.center(np.array(['abc']), 1)` 的输出 dtype 是什么？为什么不是 `<U1`？
**答案**：是 `<U3`。因为内部做了 `np.maximum(str_len(a), width)` = `max(3, 1)` = `3`，输出宽度不会小于字符串自身长度，所以不会截断。

---

### 4.2 `center` / `ljust` / `rjust`：共享 `_just_dispatcher` 的三胞胎

#### 4.2.1 概念说明

`center`（居中）、`ljust`（左对齐）、`rjust`（右对齐）这三个函数在 Python 层是**逐行同构**的。它们：

- 共用**同一个** dispatcher `_just_dispatcher`，因为 NEP-18 分发只关心数组参数 `a`（`width` 和 `fillchar` 是普通标量/数组，不属于「需要被覆盖的类型」）。
- 共用同一套四步流程。
- **唯一的实质差异**是最终调用的 C 层 ufunc 名字：`_center` / `_ljust` / `_rjust`。也就是说，居中、左对齐、右对齐的「填充策略」完全由 C 层循环决定，Python 层只负责量尺寸和开缓冲区。

至于「填充字符放左边还是右边、两边如何分配」这种语义差别，**全部下沉到了 C 层**（u3-l12 会讲这三个函数其实共用同一个循环 `string_center_ljust_rjust_loop`，靠参数区分）。Python 层对此一无所知。

#### 4.2.2 核心流程

以 `center` 为代表，三胞胎的执行流程：

1. `width = np.asanyarray(width)`：把 `width` 转成数组（支持逐元素不同的宽度）。
2. **整数校验**：`width` 必须是整数 dtype，否则 `TypeError`。
3. `a`、`fillchar` 各自 `asanyarray`。
4. **fillchar 校验**：`str_len(fillchar)` 必须处处等于 1，否则 `TypeError("The fill character must be exactly one character long")`。
5. **变长分流**：`np.result_type(a, fillchar).char == "T"` 时直接 `return _center(a, width, fillchar)`（无需预算）。
6. **预算**：`fillchar` 强制转到 `a` 的 dtype；`width = np.maximum(str_len(a), width)`；算 `out_dtype`、广播 `shape`、`empty_like` 开 `out`。
7. **写入**：`return _center(a, width, fillchar, out=out)`。

#### 4.2.3 源码精读

先看三者共用的 dispatcher——它只返回 `(a,)`，声明「只有 `a` 是 NEP-18 相关参数」：

[numpy/_core/strings.py:L687-L688](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L687-L688) —— `_just_dispatcher(a, width, fillchar=None)` 只把 `a` 列为相关参数。

再看 `center` 的实现体（重点看校验、变长分流、预算与写入四段）：

[numpy/_core/strings.py:L736-L757](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L736-L757) —— `center` 函数体。

其中几行特别值得拆开看：

- [L738-L739](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L738-L739)：`width` 整数校验，失败抛 `TypeError`。
- [L744-L746](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L744-L746)：`fillchar` 恰好一字符校验，用 `str_len` 量长度、`np.any` 兼容数组化 `fillchar`。
- [L748-L749](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L748-L749)：**变长快速路径**——注意条件是 `np.result_type(a, fillchar).char == "T"`，只要 `a` 或 `fillchar` 任一是 `StringDType`，结果类型就是 `'T'`，走变长 C 循环、无需预算。
- [L751-L755](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L751-L755)：预算 + 预分配。`fillchar.astype(a.dtype)` 保证填充字符与数据同 dtype（这也是 docstring 里「`S` dtype 传非 ASCII fillchar 会 `ValueError`」的根源）；`np.broadcast_shapes` 算出 `a`/`width`/`fillchar` 三者广播后的输出形状。
- [L757](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L757)：把 `out=` 交给 `_center`。

`ljust` 与 `rjust` 的实现体见 [L802-L822](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L802-L822) 与 [L867-L887](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L867-L887)。

#### 4.2.4 代码实践

**实践目标**：对比 `center` / `ljust` / `rjust` 三者的源码，找出「它们只有填充分支不同」的硬证据。

**操作步骤**：

1. 打开 `numpy/_core/strings.py`，分别定位 `center`（L693）、`ljust`（L762）、`rjust`（L827）的函数体。
2. 逐行比对三者的实现部分（跳过 docstring）。记录每一处不一致的行。

**需要观察的现象**：三者实现体里**唯一**的实质差异，只有 C 层 ufunc 的名字。具体地：

| 位置 | center | ljust | rjust |
| --- | --- | --- | --- |
| 变长快速路径返回 | `return _center(a, width, fillchar)` | `return _ljust(a, width, fillchar)` | `return _rjust(a, width, fillchar)` |
| 写入返回 | `return _center(a, width, fillchar, out=out)` | `return _ljust(a, width, fillchar, out=out)` | `return _rjust(a, width, fillchar, out=out)` |

此外只有一处**无行为影响的**行序微调：`center` 是先 `out_dtype`（L753）后 `shape`（L754），而 `ljust`/`rjust` 是先 `shape`（L818/L883）后 `out_dtype`（L819/L884）——操作集合完全相同，仅书写顺序不同。

**预期结果**：除上述 C 层 ufunc 名字替换（及一处行序微调）外，三个函数的 Python 实现逐行一致。这证明「居中/左对齐/右对齐」的策略差异完全由 C 层循环承担，Python 层是同一套「量尺寸 + 开缓冲区」的模板。

> 你还可以用一个运行时小实验佐证「填充策略在 C 层」：对 `np.array(['abc'])` 分别调用 `center/ljust/rjust` 且 `width=7`，观察填充字符的位置（`'  abc  '` / `'abc    '` / `'    abc'`），这三个差异在 Python 源码里**找不到任何对应分支**——它们来自 C 层的 `_center/_ljust/_rjust` 循环。

#### 4.2.5 小练习与答案

**练习 1**：为什么 dispatcher 是 `_just_dispatcher(a, width, fillchar=None)` 里 `fillchar` 的默认值必须是 `None`，而不能是 `' '`？
**答案**：u2-l4 讲过，`array_function_dispatch` 要求 dispatcher 的可选参数默认值只能是 `None`（这是 NEP-18 机制的约束）。主函数 `center(a, width, fillchar=' ')` 可以用真实默认值 `' '`，但 dispatcher 这个「签名镜像」只能用 `None`。

**练习 2**：`np.strings.center(np.array(['abc']), 10, fillchar=np.array(['**']))` 会发生什么？为什么？
**答案**：抛 `TypeError("The fill character must be exactly one character long")`。因为 `str_len('**') == 2 != 1`，`np.any(str_len(fillchar) != 1)` 为真，在校验阶段就被拦截。该行为有专门测试 [test_center_raises_multiple_character_fill](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py#L849-L853) 覆盖。

**练习 3**：`center` 用 `np.result_type(a, fillchar).char == "T"` 判断变长路径，而 `zfill` 用 `a.dtype.char == "T"`。为什么 `center` 不能像 `zfill` 那样只看 `a`？
**答案**：`center/ljust/rjust` 接受 `fillchar` 参数，`fillchar` 本身可能是 `StringDType`。只要 `a` 或 `fillchar` 任一是 `'T'`，`np.result_type(a, fillchar)` 就提升为 `'T'`，必须走变长循环。而 `zfill` 没有 `fillchar` 参数（填充字符固定为 `'0'`），所以只看 `a` 即可。

---

### 4.3 `zfill`：符号敏感的左补零

#### 4.3.1 概念说明

`zfill`（zero fill）把数字字符串左补 `0` 到指定宽度。它与三胞胎有两个表面差异、一个本质差异：

- **表面差异 1**：没有 `fillchar` 参数（填充字符固定为 `'0'`），所以校验步骤更短，dispatcher 也只有 `(a, width)`。
- **表面差异 2**：变长分流条件用 `a.dtype.char == "T"`（因为没有 `fillchar` 需要参与类型提升）。
- **本质差异**：当字符串以符号 `+` 或 `-` 开头时，`0` 要补在符号**之后**而不是之前。例如 `'+123'` 补到宽度 5 是 `'+0123'` 而不是 `'0+123'`。

关键点是：**这个符号规则在 Python 包装层里完全不可见**。Python 层的 `zfill` 和三胞胎一样，只做「量尺寸 + 开缓冲区」，符号处理全部在 C 层 `_zfill` 循环里完成。Python 源码里搜不到任何关于 `+`/`-` 的分支。

#### 4.3.2 核心流程

`zfill` 的四步流程：

1. `width = np.asanyarray(width)` + 整数校验。
2. `a = np.asanyarray(a)`。
3. 变长分流：`a.dtype.char == "T"` → `return _zfill(a, width)`。
4. 预算：`width = np.maximum(str_len(a), width)`；`out_dtype = f"{a.dtype.char}{width.max()}"`；`np.empty_like` 开 `out`（注意 `shape` 只广播 `a` 和 `width`，没有 `fillchar`）。
5. 写入：`return _zfill(a, width, out=out)`。

符号处理（`+`/`-` 后补零）发生在第 5 步的 C 层循环内部，Python 不参与。

#### 4.3.3 源码精读

[numpy/_core/strings.py:L890-L891](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L890-L891) —— `_zfill_dispatcher(a, width)`，注意它**没有** `fillchar`，且 `width` 无默认值（必填）。

[numpy/_core/strings.py:L926-L939](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L926-L939) —— `zfill` 实现体。可以逐行和 `center` 的定长分支对照：

- [L927-L928](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L927-L928)：`width` 整数校验。
- [L932-L933](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L932-L933)：变长快速路径，条件是 `a.dtype.char == "T"`。
- [L935-L938](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L935-L938)：预算 + 预分配，与 `center` 几乎相同，只是 `shape` 用 `np.broadcast_shapes(a.shape, width.shape)`（少一个 `fillchar.shape`）。
- [L939](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L939)：`return _zfill(a, width, out=out)`。符号规则就在这个 `_zfill` 里。

#### 4.3.4 代码实践

**实践目标**：用真实测试用例验证「符号前缀后补零」的规则，并确认该规则在 Python 层不可见。

**操作步骤**：

1. 运行下面的脚本（示例代码），覆盖 `test_zfill` 的几组典型用例：

```python
import numpy as np

a = np.array(['123', '+123', '-123', '+0123', '34'])
print(np.strings.zfill(a, [5, 5, 5, 5, 5]))
```

2. 然后打开 `numpy/_core/strings.py` 的 `zfill` 函数（L896-L939），用编辑器搜索 `+` 或 `-`，确认 Python 实现里没有任何符号判断分支。

**需要观察的现象**：

- `'+123'` → `'+0123'`（`0` 在 `+` 之后），`'-123'` → `'-0123'`（`0` 在 `-` 之后）。
- `'123'` → `'00123'`（无符号，`0` 在最前面）。
- `'+0123'` 宽度 5 时仍是 `'+0123'`（已达标，不补）。
- Python 源码里搜不到符号判断。

**预期结果**：输出为 `array(['00123', '+0123', '-0123', '+0123', '00034'], dtype='<U5')`。这些用例与项目测试 [test_zfill](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py#L909-L928) 完全对应，证明符号规则确实由 C 层 `_zfill` 实现、Python 层只是开缓冲区。

> 运行结果「待本地验证」：若你的 NumPy 构建里 `np.strings.zfill` 行为与此不一致，请以本地实际输出为准，并对照 C 层 `_zfill` 循环排查。

#### 4.3.5 小练习与答案

**练习 1**：`np.strings.zfill(np.array(['34']), -1)` 的结果是什么？为什么？
**答案**：是 `'34'`（dtype `<U2`，不变）。因为 `np.maximum(str_len('34'), -1)` = `max(2, -1)` = `2`，输出宽度等于字符串自身长度，不补零。这与 `center('abc', -2)` 仍得 `'abc'` 是同一个 `maximum` 机制。

**练习 2**：`zfill` 的 `shape` 为什么是 `np.broadcast_shapes(a.shape, width.shape)`，而 `center` 是三者广播？
**答案**：因为 `zfill` 没有 `fillchar` 参数，参与广播的只有 `a` 和 `width` 两个数组；`center/ljust/rjust` 多一个 `fillchar` 数组，所以要三者一起广播。

---

### 4.4 `expandtabs`：先量长度、再填充的两遍式

#### 4.4.1 概念说明

`expandtabs(a, tabsize=8)` 把每个字符串里的制表符 `\t` 替换成若干空格，使后续文本对齐到 `tabsize` 的整数倍列。它和前四个函数有一个**结构性差异**：

`center/ljust/rjust/zfill` 的目标宽度由调用者直接给出（`width` 参数），所以 Python 层只要 `np.maximum(str_len(a), width)` 就得到每个元素的输出宽度。但 `expandtabs` **没有 `width` 参数**——输出宽度取决于「`\t` 出现的位置 + `tabsize`」，这是数据相关的，无法用一个简单的 `maximum` 算出。

因此 `expandtabs` 采用**两遍式（two-pass）设计**：

- **第一遍**：调用另一个 ufunc `_expandtabs_length(a, tabsize)`，只计算每个元素展开**之后**的长度（不真正展开），得到一个逐元素的长度数组 `buffersizes`。
- **第二遍**：用 `buffersizes.max()` 拼出定长 `out_dtype`，`empty_like` 开 `out`，再调 `_expandtabs(a, tabsize, out=out)` 真正写入。

换句话说，`_expandtabs_length` 是「干跑一遍量尺寸」，`_expandtabs` 是「真跑一遍填数据」。这种「先量后填」是输出宽度强数据相关时的通用解法。

#### 4.4.2 核心流程

`expandtabs` 的执行流程：

1. `a = np.asanyarray(a); tabsize = np.asanyarray(tabsize)`。
   - 注意：与 `width` 不同，`tabsize` **没有整数类型校验**。
2. 变长分流：`a.dtype.char == "T"` → `return _expandtabs(a, tabsize)`。
3. **第一遍量长**：`buffersizes = _expandtabs_length(a, tabsize)`，得到每个元素展开后的长度（int 数组）。
4. **拼 dtype + 开 out**：`out_dtype = f"{a.dtype.char}{buffersizes.max()}"`；`out = np.empty_like(a, shape=buffersizes.shape, dtype=out_dtype)`。
5. **第二遍填充**：`return _expandtabs(a, tabsize, out=out)`。

#### 4.4.3 源码精读

[numpy/_core/strings.py:L630-L631](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L630-L631) —— `_expandtabs_dispatcher(a, tabsize=None)`，只声明 `a` 为相关参数。

[numpy/_core/strings.py:L675-L684](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L675-L684) —— `expandtabs` 实现体。逐行拆解：

- [L675-L676](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L675-L676)：把 `a` 和 `tabsize` 都 `asanyarray`（`tabsize` 也支持逐元素不同的值）。
- [L678-L679](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L678-L679)：变长快速路径。
- [L681](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L681)：**第一遍**——`_expandtabs_length` 量出每个元素展开后的长度。
- [L682-L683](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L682-L683)：用 `buffersizes.max()` 拼定长 `out_dtype`，`empty_like` 预分配（`shape=buffersizes.shape`，因为 `buffersizes` 已经是 `a` 与 `tabsize` 广播后的形状）。
- [L684](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L684)：**第二遍**——`_expandtabs` 真正写入。

#### 4.4.4 代码实践

**实践目标**：验证「两遍式」中 `_expandtabs_length` 的输出确实是「展开后的长度」，并对照测试用例理解 `tabsize` 如何影响对齐。

**操作步骤**：

1. 运行下面的脚本（示例代码），对照项目测试 [test_expandtabs](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py#L815-L826) 的用例：

```python
import numpy as np
from numpy._core.umath import _expandtabs_length   # 第一遍：只量长度

a = np.array(['abc\rab\tdef\ng\thi'])
print('展开后长度:', _expandtabs_length(a, 8))     # 第一遍
print('展开后长度:', _expandtabs_length(a, 4))
print('真正展开  :', np.strings.expandtabs(a, 8))  # 第一遍+第二遍
```

2. 再观察溢出保护：运行 `np.strings.expandtabs(np.array(['\ta\n\tb']), sys.maxsize)`。

**需要观察的现象**：

- `_expandtabs_length(a, 8)` 应返回 `19`（对应测试里 `"abc\rab      def\ng       hi"` 的长度），`_expandtabs_length(a, 4)` 应返回 `15`。
- `np.strings.expandtabs(a, 8)` 的结果里，`\t` 被替换成若干空格，使 `def` 对齐到第 8 列、`hi` 对齐到下一个 `tabsize` 倍数列。
- 第 2 步会抛 `OverflowError("new string is too long")`。

**预期结果**：第一遍返回的整数恰好等于第二遍展开结果字符串的字符数，证明 `_expandtabs_length` 就是「干跑量尺寸」；溢出用例与测试 [test_expandtabs_raises_overflow](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py#L828-L831) 一致（该溢出检查在 C 层 `_expandtabs_length` 内完成）。

> 运行结果「待本地验证」：`_expandtabs_length` 是私有 ufunc，若无法直接导入，可改为只比较 `np.strings.expandtabs(a, 8)` 与 `np.strings.expandtabs(a, 4)` 两个结果的 `str_len`，同样能看出「`tabsize` 越小、展开后越短」的趋势。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `expandtabs` 需要「两遍」，而 `center` 只需要「一遍」预算？
**答案**：`center` 的目标宽度由调用者用 `width` 参数直接给出，`np.maximum(str_len(a), width)` 一步就能算出输出宽度；`expandtabs` 没有类似的「目标宽度」参数，输出宽度取决于 `\t` 的位置和 `tabsize`，必须先把展开逻辑「干跑一遍」（`_expandtabs_length`）才能知道每个元素的输出长度。

**练习 2**：`expandtabs` 没有 `tabsize` 的整数类型校验，而 `center` 校验了 `width`。结合测试 [test_expandtabs_length_not_cause_segfault](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py#L833-L845)，传一个非字符串数组（如 `np.zeros(200)`）会发生什么？
**答案**：`_expandtabs_length` 对浮点等非字符串 dtype 没有对应的 ufunc 循环，会抛 `_UFuncNoLoopError`（`"did not contain a loop with signature matching types"`）。该测试正是为了防止 gh-28829 这类「无循环却误入计算导致段错误」的回归。

---

## 5. 综合实践

**任务**：给本讲的五个函数画一张「Python 分支决策图」，并用一张表把它们在定长分支里的预算方式对照清楚，最后写一个脚本验证你的结论。

**要求**：

1. 阅读这五个函数的 Python 实现（[center](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L693-L757) / [ljust](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L762-L822) / [rjust](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L827-L887) / [zfill](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L896-L939) / [expandtabs](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L636-L684)），填完下表：

   | 函数 | 变长分流条件 | 输出宽度来源（定长分支） | 填充/语义差异在哪一层 |
   | --- | --- | --- | --- |
   | center | `result_type(a, fillchar).char == "T"` | `np.maximum(str_len(a), width).max()` | C 层 `_center` |
   | ljust | … | … | … |
   | rjust | … | … | … |
   | zfill | … | … | C 层 `_zfill`（含符号规则） |
   | expandtabs | `a.dtype.char == "T"` | `_expandtabs_length(a, tabsize).max()`（两遍式） | C 层 `_expandtabs` |

2. 用下面这个脚本（示例代码）验证「输出 dtype 统一取最大宽度」：

```python
import numpy as np
a = np.array(['a', 'bbb', 'cc'])           # str_，长度不一
print(np.strings.center(a, 5).dtype)        # 期望 <U5
print(np.strings.zfill(a, 4).dtype)         # 期望 <U4
print(np.strings.expandtabs(np.array(['a\tb'])).dtype)  # tabsize=8 默认
```

3. 解释为什么三个结果的 dtype 都是「统一宽度」，而不是逐元素不同。

**预期产出**：一张填好的对照表 + 一段脚本输出 + 一句话结论（「定长 dtype 只能有一个统一字段宽度，故取全数组最大值；填充策略与符号规则全部下沉到 C 层，Python 层只负责量尺寸与开缓冲区」）。

## 6. 本讲小结

- `center/ljust/rjust/zfill/expandtabs` 在 Python 层共享「校验 → 变长分流 → 预算宽度 → 预分配 `out` → 交给 C 层 ufunc」的四步套路，对应 u2-l5 的「输出 dtype 路径 A」。
- `center/ljust/rjust` 是逐行同构的三胞胎，共用 `_just_dispatcher`，唯一实质差异是调用的 C 层 ufunc（`_center/_ljust/_rjust`）；填充策略完全在 C 层。
- `str_len` 在本讲扮演「尺子」：配合 `width` 取 `np.maximum` 得到逐元素输出宽度，再 `.max()` 取全数组上界拼出统一的定长 `out_dtype`。
- 变长分流条件有两种：`center/ljust/rjust` 用 `np.result_type(a, fillchar).char == "T"`（因为 `fillchar` 可能是 `StringDType`），`zfill/expandtabs` 用 `a.dtype.char == "T"`（无 `fillchar`）。
- `zfill` 的「符号 `+`/`-` 后补零」规则在 Python 层不可见，全部由 C 层 `_zfill` 实现；Python 层只是开缓冲区。
- `expandtabs` 是独有的「两遍式」：先用 `_expandtabs_length` 量出每个元素展开后的长度（因为输出宽度强数据相关、无 `width` 参数），再用 `_expandtabs` 填充。

## 7. 下一步学习建议

- 下一讲 u2-l10 会讲 `strip/lstrip/rstrip`，它们同样用 `str_len` + 定长 `out` 的套路，但多了 `chars=None`（去空白）vs `chars=...`（去指定字符集）的两条 C ufunc 分支，可以与本讲的「双分支」对照。
- 若想看清「填充策略 / 符号规则 / 两遍式量长」的 C 层实现，可直接进入 u3-l12（`string_ufuncs.cpp` 的循环注册）与 u3-l13（`string_buffer` / `string_fastsearch` 原语），那里会解释 `_center/_ljust/_rjust` 为何能共用同一个循环、`_expandtabs_length` 如何在 C 层干跑量尺寸。
- 建议顺带阅读 `numpy/_core/tests/test_strings.py` 中 [L815-L928](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py#L815-L928) 这一段测试，它用参数化用例覆盖了本讲五个函数在三种 dtype 下的边界行为，是验证你理解的最佳参照。

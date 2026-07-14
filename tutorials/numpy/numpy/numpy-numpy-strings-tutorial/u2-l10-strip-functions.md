# 裁剪类：strip / lstrip / rstrip 的双分支

## 1. 本讲目标

本讲聚焦 `numpy.strings` 里「裁剪（strip）」一族三个函数：`strip`、`lstrip`、`rstrip`。学完后你应该能够：

- 看懂这三个函数的 Python 包装层只有薄薄两行，靠 `chars` 是否为 `None` 在两条 C ufunc 路径之间分发；
- 解释为什么 strip 族**不需要**像 `center`/`ljust`/`rjust`/`expandtabs` 那样「预算输出宽度 + 预分配 `out`」；
- 理解 C 层 6 个底层 ufunc（`_lstrip`/`_rstrip`/`_strip` × `_whitespace`/`_chars`）如何用一个 `STRIPTYPE` 静态数据共用同一套循环函数；
- 准确说出 `chars` 是一个「字符集合」而不是前缀/后缀子串，并解释由此带来的「被保护字符夹住就剥不掉」的现象。

## 2. 前置知识

在进入本讲前，建议你已经掌握（这些在前面几讲已建立）：

- **Python 字符串的 `strip`/`lstrip`/`rstrip` 语义**：从字符串两端移除字符，直到遇到不在「可移除集合」里的字符为止。
- **三种字符串 dtype**（[u1-l2](u1-l2-three-string-dtypes.md)）：变长 `StringDType`（`dtype.char == 'T'`）、定长 `bytes_`（`'S'`，ASCII，1 字符 1 字节）、定长 `str_`（`'U'`，UCS4，1 字符 4 字节）。
- **装饰器两件套**（[u2-l4](u2-l4-decorators-and-dispatch.md)）：`@set_module('numpy.strings')` 管「身份」（改写 `__module__`），`@array_function_dispatch(dispatcher)` 管「行为」（启用 NEP-18 `__array_function__` 协议）。
- **输出 dtype 路径**（[u2-l5](u2-l5-helpers-and-dtype-dispatch.md)）：当输出宽度**能仅由输入 dtype 决定**时走「路径 C：直接复用输入 dtype」；当输出宽度**数据相关**（可能更宽）时走「路径 A：用 `str_len` 预算宽度再 `empty_like` 预分配 `out`」。
- **对齐填充函数**（[u2-l9](u2-l9-justify-and-pad.md)）：`center`/`ljust`/`rjust` 是「路径 A」的典型代表。

本讲的核心反差正是：**strip 族走的是「路径 C」，所以它比对齐函数简单得多。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [numpy/_core/strings.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py) | Python 包装层：定义 `strip`/`lstrip`/`rstrip`，并从 `numpy._core.umath` 导入 6 个底层 ufunc。 |
| [numpy/_core/src/umath/string_ufuncs.cpp](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp) | 定长 `bytes_`/`str_` 的 strip 循环注册：循环函数、`resolve_descriptors`、批量注册表。 |
| [numpy/_core/src/umath/string_buffer.h](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h) | 真正的剥离算法 `string_lrstrip_whitespace` / `string_lrstrip_chars`，以及 `STRIPTYPE` 枚举。 |
| [numpy/_core/src/umath/stringdtype_ufuncs.cpp](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp) | 变长 `StringDType('T')` 的 strip 循环注册（与定长版共用同样的 ufunc 名字）。 |

## 4. 核心概念与源码讲解

### 4.1 Python 包装：极薄的双分支分发

#### 4.1.1 概念说明

`strip`/`lstrip`/`rstrip` 是 `numpy.strings` 里**最薄**的一类包装函数。它们只接收两个参数：

- `a`：字符串数组（`StringDType` / `bytes_` / `str_`）；
- `chars`：一个标量，表示「要剥掉的字符集合」，可缺省。

包装函数的全部职责只有一件事：**根据 `chars` 是不是 `None`，把调用转发给两个底层 ufunc 中的一个**：

- `chars is None`（缺省） → 剥**空白字符**，交给 `_xxx_whitespace`；
- `chars` 给定 → 剥**指定字符集合**，交给 `_xxx_chars`。

这里有一个和 u2-l9 对齐函数的关键对比值得记住：

> `center`/`ljust`/`rjust`/`expandtabs`/`zfill` 的输出**可能比输入更长**（要填充字符），输出宽度是数据相关的，所以 Python 层必须用 `str_len` 量尺寸、`empty_like` 开缓冲区（路径 A）。
>
> 而 strip 的输出**永远不超过输入长度**——它只会变短或不变。于是 C 层的 `resolve_descriptors` 可以直接「复用输入 dtype」作为输出 dtype（路径 C），Python 层完全不需要预算宽度。

这就是 strip 族 Python 包装能精简到两行的根本原因。

#### 4.1.2 核心流程

三个函数的伪代码完全同构（只差调用哪个底层 ufunc）：

```python
@set_module("numpy.strings")
def strip(a, chars=None):
    if chars is None:
        return _strip_whitespace(a)    # 走「去空白」C ufunc（1 入 1 出）
    return _strip_chars(a, chars)      # 走「去字符集」C ufunc（2 入 1 出）
```

注意两个细节：

1. **只有 `@set_module`，没有 `@array_function_dispatch`**。也就是说这三个函数**没有 dispatcher**，不会触发 NEP-18 的 `__array_function__` 分发逻辑（对比 [u2-l9](u2-l9-justify-and-pad.md) 里 `center`/`ljust`/`rjust` 都带 `_just_dispatcher`）。原因是它们唯一的数组参数就是 `a`，`chars` 是标量；设计上没有为它们额外声明「相关参数」。
2. **两个分支的底层 ufunc 输入个数不同**：`_strip_whitespace(a)` 是单参数，`_strip_chars(a, chars)` 是双参数。这会直接反映到 C 层的注册参数（`nin`）上。

#### 4.1.3 源码精读

6 个底层 ufunc 都从 `numpy._core.umath` 导入，以下导入块里的第 27–39 行就是它们（`_lstrip_chars`、`_lstrip_whitespace`、`_rstrip_chars`、`_rstrip_whitespace`、`_strip_chars`、`_strip_whitespace`）：

- [numpy/_core/strings.py:L22-L58](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L22-L58) —— 从 `numpy._core.umath` 导入全部底层字符串 ufunc（strip 族在 L27–L39）。

三个包装函数本身：

- [numpy/_core/strings.py:L942-L987](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L942-L987) —— `lstrip`：`chars is None` 走 `_lstrip_whitespace(a)`，否则 `_lstrip_chars(a, chars)`（实现见 L985–L987）。
- [numpy/_core/strings.py:L990-L1030](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L990-L1030) —— `rstrip`：结构同上，分支为 `_rstrip_whitespace` / `_rstrip_chars`（实现见 L1028–L1030）。
- [numpy/_core/strings.py:L1033-L1077](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1033-L1077) —— `strip`：分支为 `_strip_whitespace` / `_strip_chars`（实现见 L1075–L1077）。

对比一下对齐函数 `ljust` 的函数体，体会「路径 A vs 路径 C」的繁简差距——`ljust` 要校验 `width` 类型、校验 `fillchar` 恰好一字符、分流 `T`、用 `np.maximum(str_len(a), width)` 预算宽度、`empty_like` 开缓冲区、最后带 `out=` 调用 C ufunc：

- [numpy/_core/strings.py:L800-L822](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L800-L822) —— `ljust` 函数体：预算输出宽度并预分配 `out`（路径 A），与 strip 的两行实现形成鲜明对比。

#### 4.1.4 代码实践

**实践目标**：直观感受 strip 族包装层与对齐函数包装层的「厚度差」，并确认 strip 不做宽度预算。

**操作步骤**：

1. 打开 [numpy/_core/strings.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py)，分别定位 `strip`（L1033）与 `ljust`（L800 附近）。
2. 数一数两者的「非空行」函数体：`strip` 的实现只有 L1075–L1077 两行；`ljust` 的实现从 L802 到 L822 共二十余行。
3. 在 `strip` 的函数体里搜索 `empty_like`、`str_len`、`out_dtype`——你会发现**一个都没有**；而在 `ljust` 里这三个都出现。

**需要观察的现象**：strip 的 Python 层完全没有「量尺寸 / 开缓冲区」的痕迹，所有这些活都下沉到了 C 层。

**预期结果**：strip 族是「路径 C（复用输入 dtype）」的典型，对齐族是「路径 A（预算宽度）」的典型。

#### 4.1.5 小练习与答案

**Q1**：`strip`/`lstrip`/`rstrip` 为什么没有 `@array_function_dispatch` 装饰器？

> 参考答案：它们唯一的数组参数是 `a`，`chars` 是标量；设计者没有为它们声明 dispatcher，因此不会走 NEP-18 的 `__array_function__` 分发。这与 `center`/`ljust`/`rjust`（带 `_just_dispatcher`，因为它们有多个数组/类数组参数）形成对比。

**Q2**：把 `chars=None` 这条分支改成「在 Python 层用 `_vec_string` 调 `''.lstrip()`」可行吗？为什么 NumPy 没这么做？

> 参考答案：技术上可行但很慢。NumPy 选择为空白剥离和字符集剥离各写一个专用 C ufunc（`_xxx_whitespace` / `_xxx_chars`），以获得向量化性能；只有那些暂无专用 C 循环的函数（如 `upper`/`mod`/`translate`）才退而求其次走 `_vec_string`（见 [u2-l8](u2-l8-case-and-vecstring.md)）。

### 4.2 C 层：6 个 ufunc 与 `STRIPTYPE` 共用循环

#### 4.2.1 概念说明

剥字符一共有 6 个变种，对应 6 个底层 ufunc 名字：

| 方向 | 去空白 | 去字符集 |
| --- | --- | --- |
| 只剥左（lstrip） | `_lstrip_whitespace` | `_lstrip_chars` |
| 只剥右（rstrip） | `_rstrip_whitespace` | `_rstrip_chars` |
| 两端都剥（strip） | `_strip_whitespace` | `_strip_chars` |

这里的精彩设计在于**代码复用**：

- **方向维度（左/右/两端）** 三个变种**共用同一个循环函数**，仅靠一个附加在每个 ufunc 上的「静态数据」`STRIPTYPE`（取值 `LEFTSTRIP` / `RIGHTSTRIP` / `BOTHSTRIP`）来区分行为。
- **模式维度（空白 vs 字符集）** 两种是**不同的循环函数**，因为输入个数不同：去空白是 1 入 1 出，去字符集是 2 入 1 出（多一个 `chars` 输入）。

所以 6 个 ufunc 实际只对应 **2 个循环函数** + **3 个 `STRIPTYPE` 值**。

#### 4.2.2 核心流程

注册阶段（`init_string_ufuncs` 内）用两个 `for` 循环批量注册：

```
# 去空白族：1 入 1 出
names = ["_lstrip_whitespace", "_rstrip_whitespace", "_strip_whitespace"]
types = [LEFTSTRIP, RIGHTSTRIP, BOTHSTRIP]
for i in 0..2:
    注册 names[i]，ASCII 编码（bytes_），循环 = string_lrstrip_whitespace_loop<ASCII>，
         resolve = string_strip_whitespace_resolve_descriptors，静态数据 = types[i]
    注册 names[i]，UTF32 编码（str_），  循环 = string_lrstrip_whitespace_loop<UTF32>，
         resolve = string_strip_whitespace_resolve_descriptors，静态数据 = types[i]

# 去字符集族：2 入 1 出（结构同上，换 names/循环/resolve）
```

每个名字注册两次：`ENCODING::ASCII` 对应 `bytes_`（`'S'`），`ENCODING::UTF32` 对应 `str_`（`'U'`）。注意定长 ufunc **不处理 `StringDType('T')`**——那是 `stringdtype_ufuncs.cpp` 的事（见 4.5）。

#### 4.2.3 源码精读

`STRIPTYPE` 枚举定义在缓冲区头文件里：

- [numpy/_core/src/umath/string_buffer.h:L1156-L1158](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L1156-L1158) —— `enum class STRIPTYPE { LEFTSTRIP, RIGHTSTRIP, BOTHSTRIP }`，三个方向共用循环时靠它区分。

两个循环函数（注意它们如何从 `context->method->static_data` 取出 `STRIPTYPE`，再把它原样传给算法函数）：

- [numpy/_core/src/umath/string_ufuncs.cpp:L420-L445](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L420-L445) —— `string_lrstrip_whitespace_loop`：1 入 1 出，逐元素调用 `string_lrstrip_whitespace(buf, outbuf, striptype)`。
- [numpy/_core/src/umath/string_ufuncs.cpp:L448-L475](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L448-L475) —— `string_lrstrip_chars_loop`：2 入 1 出（`in1`=字符串、`in2`=字符集），逐元素调用 `string_lrstrip_chars(buf1, buf2, outbuf, striptype)`。

批量注册表（两个 `for` 循环、每个名字注册 ASCII 与 UTF32 两种编码）：

- [numpy/_core/src/umath/string_ufuncs.cpp:L1695-L1718](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1695-L1718) —— 注册 `_lstrip/_rstrip/_strip_whitespace`，循环共用、靠 `striptypes[i]` 区分方向。
- [numpy/_core/src/umath/string_ufuncs.cpp:L1722-L1741](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1722-L1741) —— 注册 `_lstrip/_rstrip/_strip_chars`（`2, 1` 表示 2 入 1 出）。

注册助手 `init_ufunc` 的签名（理解参数含义：名字、入参个数、出参个数、类型表、编码、循环、resolve_descriptors、静态数据）：

- [numpy/_core/src/umath/string_ufuncs.cpp:L1357-L1361](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1357-L1361) —— `init_ufunc(umath, name, nin, nout, typenums, enc, loop, resolve_descriptors, static_data)`；其中 `NPY_OBJECT` 会按 `enc` 被解析成 `PyArray_BytesDType`（ASCII）或 `PyArray_UnicodeDType`（UTF32）。

#### 4.2.4 代码实践

**实践目标**：把 6 个 ufunc 的注册关系画成一张表，验证「2 个循环 + 3 个 STRIPTYPE」的复用结构。

**操作步骤**：

1. 阅读 [string_ufuncs.cpp:L1695-L1741](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1695-L1741)。
2. 手工填写下表（已在源码里确认）：

| ufunc 名 | nin/nout | 循环函数 | 编码 → dtype | STRIPTYPE |
| --- | --- | --- | --- | --- |
| `_lstrip_whitespace` | 1/1 | `string_lrstrip_whitespace_loop` | ASCII→S, UTF32→U | LEFTSTRIP |
| `_rstrip_whitespace` | 1/1 | 同上 | ASCII→S, UTF32→U | RIGHTSTRIP |
| `_strip_whitespace` | 1/1 | 同上 | ASCII→S, UTF32→U | BOTHSTRIP |
| `_lstrip_chars` | 2/1 | `string_lrstrip_chars_loop` | ASCII→S, UTF32→U | LEFTSTRIP |
| `_rstrip_chars` | 2/1 | 同上 | ASCII→S, UTF32→U | RIGHTSTRIP |
| `_strip_chars` | 2/1 | 同上 | ASCII→S, UTF32→U | BOTHSTRIP |

**预期结果**：去空白 3 个名字共用 `string_lrstrip_whitespace_loop`，去字符集 3 个名字共用 `string_lrstrip_chars_loop`；方向差异完全由 `STRIPTYPE` 静态数据承载。

#### 4.2.5 小练习与答案

**Q1**：为什么「方向」用静态数据区分，而「模式（空白/字符集）」要用两个不同的循环函数？

> 参考答案：方向只是「剥不剥左、剥不剥右」的开关，算法主体相同，适合用一个枚举参数控制；而模式差异改变了**输入个数**（去字符集多一个 `chars` 输入），进而改变了循环签名、`resolve_descriptors` 的描述符个数（2 vs 3）和注册参数（`nin` 1 vs 2），无法用一个参数统一，只能拆成两个循环函数。

### 4.3 `resolve_descriptors`：输出 dtype 为何就是输入 dtype

#### 4.3.1 概念说明

这是本讲最关键的一节，它解释了「为什么 strip 族在 Python 层那么薄」。

NumPy 的 ufunc 在真正执行循环前，会调用一个叫 `resolve_descriptors` 的回调，由它根据「给定的输入描述符」决定「循环真正使用的输入/输出描述符」。对于大多数函数，这个回调要做的核心工作是**确定输出 dtype**。

- 对于 `center`/`ljust` 这类，输出宽度数据相关，`resolve_descriptors` 必须**强制要求调用方提供 `out`**（Python 层于是要预算宽度、开缓冲区）。
- 对于 strip 族，输出宽度**最多等于输入宽度**（只会变短），所以 `resolve_descriptors` 直接**把输出描述符设为输入描述符的副本**即可——输出字段宽度不变，剥短后的内容写入同宽缓冲区，剩余字节用 0 填充。

换句话说：`<U7` 进，`<U7` 出，只是内容变短了。这正是 u2-l5 归纳的「路径 C」。

#### 4.3.2 核心流程

去空白（2 个描述符：1 入 1 出）：

```
loop_descrs[0] = canonicalize(given_descrs[0])   # 规范化输入
loop_descrs[1] = loop_descrs[0]                  # 输出 == 输入，直接复用
return NPY_NO_CASTING
```

去字符集（3 个描述符：2 入 1 出）：

```
loop_descrs[0] = canonicalize(given_descrs[0])   # 字符串 a
loop_descrs[1] = canonicalize(given_descrs[1])   # 字符集 chars
loop_descrs[2] = loop_descrs[0]                  # 输出 == a 的 dtype
return NPY_NO_CASTING
```

#### 4.3.3 源码精读

- [numpy/_core/src/umath/string_ufuncs.cpp:L806-L823](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L806-L823) —— `string_strip_whitespace_resolve_descriptors`：把 `loop_descrs[1]` 直接指向 `loop_descrs[0]`（输出复用输入 dtype）。
- [numpy/_core/src/umath/string_ufuncs.cpp:L826-L848](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L826-L848) —— `string_strip_chars_resolve_descriptors`：规范化两个输入后，令 `loop_descrs[2] = loop_descrs[0]`（输出 dtype 跟随 `a`，而非 `chars`）。

注意两个回调都返回 `NPY_NO_CASTING`——strip 不做任何类型转换，输入和输出在类型层完全一致。

#### 4.3.4 代码实践

**实践目标**：从源码层面确认「strip 的输出 dtype 永远等于输入 dtype」。

**操作步骤**：

1. 阅读上面两个 `resolve_descriptors`（L806–L848）。
2. 思考：如果输入是 `dtype='<U7'`，输出描述符会被设成什么？如果是 `'S3'` 呢？

**预期结果**：无论剥掉多少字符，输出 dtype 的字段宽度都和输入一致（`<U7` → `<U7`，`'S3'` → `'S3'`）。这也是为什么本机调用 `np.strings.strip(np.array(['  aA  ']))` 得到的数组 `dtype` 仍是 `<U6`，即便内容只剩 `'aA'`。

#### 4.3.5 小练习与答案

**Q1**：如果有人想给 strip 加一个「自动收缩 dtype 宽度到实际长度」的特性，需要在哪一层动手？

> 参考答案：需要在 `resolve_descriptors` 里基于**实际数据**计算输出宽度——但这违背了 ufunc 的设计原则（`resolve_descriptors` 只看 dtype、不看数据内容，数据相关的宽度本应由 Python 层预算）。因此 strip 选择保留原宽度、内容右端补 0，而不是收缩 dtype。

### 4.4 剥离算法：从两端向内，遇非目标即停

#### 4.4.1 概念说明

理解 strip 的行为，要抓住两条规则：

1. **`chars` 是「字符集合」，不是前缀/后缀子串。** 比如 `strip('ab')` 会把 `'a'` 和 `'b'` 的**任意组合**从两端剥掉：`'abba'` → `''`，`'xabay'` → `'xabay'`（两端都不是 a/b）。文档原话是「*not a prefix or suffix; rather, all combinations of its values are stripped*」。
2. **剥离只发生在两端，遇到第一个「非目标字符」就停。** 算法从左端向右扫、从右端向左扫，一旦遇到不在目标集合里的字符，立即停止该方向的剥离。由此得到一个重要推论：**被「保护字符」夹在中间的目标字符不会被剥掉。**

第二条推论正是本讲实践任务要解释的现象：对 `'  aA  '`（两端是空格、中间有 `'a'`）调用 `strip('a')`，结果原样不变——因为两端都是空格（不在 `{'a'}` 里），左扫第一步就停、右扫第一步也停，根本轮不到中间的 `'a'`。

#### 4.4.2 核心流程

两个算法函数都按「先剥左、再剥右」组织，方向由 `STRIPTYPE` 控制：

- **剥左**：当 `strip_type != RIGHTSTRIP` 时执行（即 `LEFTSTRIP` 和 `BOTHSTRIP` 会剥左）。从下标 0 向右遍历，每读一个字符判断是否「该剥」（空白版用 `isspace`，字符集版用 `find_char` 在 `chars` 里查），命中就推进起点、不命中就 `break`。
- **剥右**：当 `strip_type != LEFTSTRIP` 时执行（即 `RIGHTSTRIP` 和 `BOTHSTRIP` 会剥右）。从末尾向左遍历，逻辑对称。
- **空字符集特判**（仅字符集版）：若 `chars` 为空（`len2 == 0`），直接把原串原样复制到输出，不剥任何东西。这解释了文档里的 `(lstrip(c, ' ') == lstrip(c, '')).all() == False`——`''` 是空集合（什么都不剥），而 `' '` 才会剥空格。

最后把 `[new_start, new_stop)` 这段内容 `memcpy` 到输出缓冲区，并把剩余字节填 0（定长编码才需要补 0；UTF8 变长不补）。

#### 4.4.3 源码精读

去空白算法（左剥用 `first_character_isspace()` 判定，右剥额外排除 `'\0'` 填充字节）：

- [numpy/_core/src/umath/string_buffer.h:L1161-L1222](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L1161-L1222) —— `string_lrstrip_whitespace`：左剥在 L1178–L1187（`strip_type != RIGHTSTRIP`），右剥在 L1197–L1212（`strip_type != LEFTSTRIP`），最后 `buffer_memcpy` + `buffer_fill_with_zeros_after_index` 写出。

去字符集算法（含空字符集特判、按编码分派 `find_char` / `fastsearch`）：

- [numpy/_core/src/umath/string_buffer.h:L1225-L1349](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L1225-L1349) —— `string_lrstrip_chars`：左剥在 L1253–L1289，右剥在 L1299–L1338，写出在 L1341–L1348。
- [numpy/_core/src/umath/string_buffer.h:L1238-L1246](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L1238-L1246) —— 空字符集特判：`len2 == 0` 时原样复制 `buf1`，不剥任何东西。

读这段代码时注意一个细节：右剥循环里对定长编码有 `(enc == ENCODING::UTF8 || *traverse_buf != 0)` 的额外条件（去空白版 L1200），目的是不把字段右端的 `'\0'` 填充字节误判成「可剥的空白」——这与 u1-l2 讲过的「str_ 字段宽度是字符数的 4 倍、末尾用 0 填充」直接相关。

#### 4.4.4 代码实践

这是本讲的主实践任务。

**实践目标**：用 `np.array(['  aA  '])` 演示三种裁剪，并解释「`strip('a')` 为什么剥不掉中间的 `a`」。

**操作步骤**：

1. 准备输入数组：

   ```python
   import numpy as np
   c = np.array(['  aA  '])          # dtype='<U6'：2 空格 + 'a' + 'A' + 2 空格
   ```

2. 分别调用三种裁剪：

   ```python
   np.strings.lstrip(c, ' ')   # 只剥左端空格
   np.strings.strip(c, 'a')    # 剥两端 'a'
   np.strings.strip(c)         # 剥两端空白（默认）
   ```

3. 再补充一个对比，体会「字符集合 vs 子串」：

   ```python
   d = np.array(['aabbAA'])
   np.strings.strip(d, 'ab')   # 两端的 a/b 任意组合都被剥
   ```

**需要观察的现象与预期结果**（以下结果由源码语义与官方文档中的 docstring 示例共同确定，`strip(c)` 与 `strip(c, 'a')` 对 `'  aA  '` 的输出直接出现在 [strings.py 的 docstring](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1065-L1072) 中）：

| 调用 | 结果 | 解释（对照源码） |
| --- | --- | --- |
| `lstrip(c, ' ')` | `array(['aA  '], dtype='<U6')` | 左剥：前 2 个空格在集合 `{' '}` 中 → 剥掉；遇到 `'a'` 不在集合 → 停。右端不动。 |
| `strip(c, 'a')` | `array(['  aA  '], dtype='<U6')` | **原样不变**。左端第一个字符是空格，不在 `{'a'}` → 左剥立即停（`new_start=0`）；右端第一个字符也是空格 → 右剥立即停（`new_stop=6`）。中间的 `'a'` 被「两端空格」保护，根本轮不到。 |
| `strip(c)` | `array(['aA'], dtype='<U6')` | 默认剥空白：两端各 2 个空格全剥，留下 `'aA'`。注意 dtype 仍是 `<U6`，只是右侧用 `'\0'` 填充——印证 4.3「输出 dtype 复用输入 dtype」。 |
| `strip(d, 'ab')` | `array(['AA'], dtype='<U6')` | `{'a','b'}` 的任意组合从两端剥：`'aabb'` 全剥，剩 `'AA'`。证明 `chars` 是集合而非子串。 |

**核心解释**（为何 `strip('a')` 不动 `'  aA  '`）：strip 只从两端向内剥离，遇到第一个非目标字符就停。`'  aA  '` 的两端都是空格，对集合 `{'a'}` 而言「第一步就停」，左右两端各剥掉 0 个字符；位于中间的 `'a'` 不在「端点可达区域」内，因此被保留。要让那个 `'a'` 也被剥掉，得先用 `strip()` 去掉两端空格，把它「暴露」到端点。

> 说明：本实践未在本文档生成环境中实运行（执行 Python 需要审批）；上述结果严格依据源码剥离算法与官方 docstring 示例推出。若你在本地运行得到不同结果，请以本地为准并回头核对源码。

#### 4.4.5 小练习与答案

**Q1**：对 `np.array(['xyxabcxyx'])` 调用 `np.strings.strip(_, 'xy')`，结果是什么？

> 参考答案：`'abc'`。两端 `'xyx'` 是 `{'x','y'}` 的组合，全部被剥；遇到 `'a'`（不在集合）停止，右端对称。结果 `array(['abc'], dtype='<U9')`。

**Q2**：`np.strings.strip(c, '')` 与 `np.strings.strip(c)` 结果是否相同？为什么？

> 参考答案：不同。`strip(c, '')` 的 `chars` 是空字符串（空集合），命中 [string_buffer.h:L1238-L1246](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L1238-L1246) 的特判，什么都不剥，原样返回；`strip(c)`（`chars=None`）走的是 `_strip_whitespace`，剥掉两端空白。这正是 docstring 里 `(lstrip(c, ' ') == lstrip(c, '')).all() == False` 的根源。

**Q3**：为什么去空白算法在右剥时多了一个 `*traverse_buf != 0` 的判断？

> 参考答案：定长 dtype（`str_`/`bytes_`）的字段是固定宽度，实际内容末尾用 `'\0'` 填充。右剥时不把 `'\0'` 当成「空白」误剥，避免越过真实内容边界（去空白版见 L1200）。

### 4.5 StringDType（`'T'`）的 strip 循环

#### 4.5.1 概念说明

回顾前面的函数（如 [u2-l6](u2-l6-compare-and-concat.md) 的 `multiply`、[u2-l9](u2-l9-justify-and-pad.md) 的 `center`），它们的 Python 包装里都有形如 `if a.dtype.char == 'T': return ...` 的**变长分流**。

**strip 族是个例外**：它的 Python 包装里**没有** `char == 'T'` 分支（见 4.1.3 的源码，L1075–L1077 只有 `chars is None` 判断）。那么 `StringDType` 数组是怎么被正确处理的？

答案是 **ufunc 按 dtype 自动分发**：`_strip_whitespace` / `_strip_chars` 这几个 ufunc 名字同时被注册了三套循环——定长 `bytes_`（ASCII）、定长 `str_`（UTF32）在 `string_ufuncs.cpp`，变长 `StringDType` 在 `stringdtype_ufuncs.cpp`。调用时 NumPy 根据输入数组的 dtype 自动挑选对应循环。Python 层因此无需分流。

#### 4.5.2 核心流程

`stringdtype_ufuncs.cpp` 里用与定长版**完全相同**的三个名字（`_lstrip_whitespace` / `_rstrip_whitespace` / `_strip_whitespace`、`_lstrip_chars` / `_rstrip_chars` / `_strip_chars`）和**完全相同**的 `STRIPTYPE` 三元组（`LEFTSTRIP`/`RIGHTSTRIP`/`BOTHSTRIP`）注册针对 `StringDType` 的循环；底层同样复用 `string_buffer.h` 里的 `string_lrstrip_whitespace` / `string_lrstrip_chars` 算法（只是缓冲区封装不同）。

#### 4.5.3 源码精读

- [numpy/_core/src/umath/stringdtype_ufuncs.cpp:L2889-L2911](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L2889-L2911) —— 为 `StringDType` 注册 `_lstrip/_rstrip/_strip_whitespace`（1 入 1 出），`STRIPTYPE` 与定长版一致。
- [numpy/_core/src/umath/stringdtype_ufuncs.cpp:L2913-L2939](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L2913-L2939) —— 为 `StringDType` 注册 `_lstrip/_rstrip/_strip_chars`（2 入 1 出），并挂上 `all_strings_promoter` 促进器，使得 `a` 与 `chars` 即使一个是 `StringDType`、一个是普通 `str` 也能统一到 `StringDType`。
- [numpy/_core/src/umath/stringdtype_ufuncs.cpp:L1056-L1129](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L1056-L1129) —— `string_lrstrip_chars_strided_loop`：`'T'` 版的字符集剥离循环，内部仍调用 `string_lrstrip_chars(buf1, buf2, outbuf, striptype)`，算法与定长版同源。

#### 4.5.4 代码实践

**实践目标**：验证 `StringDType` 数组也能用同一套 `np.strings.strip` 接口，且无需 Python 层分流。

**操作步骤**：

1. 构造一个 `StringDType` 数组并裁剪：

   ```python
   import numpy as np
   s = np.array(['  aA  '], dtype=np.dtypes.StringDType())
   np.strings.strip(s)        # 期望: array(['aA'], dtype=StringDType())
   np.strings.strip(s, 'a')   # 期望: array(['  aA  '], dtype=StringDType())（两端空格保护）
   ```

2. 思考：这次 `strip(s)` 的结果 dtype 是 `StringDType()`（变长），不再是 `<U6`——因为变长存储本身就没有「字段宽度」的概念，剥短后直接存储实际字节（UTF-8）。

**预期结果**：行为与 `str_` 版一致（裁剪逻辑相同），但 dtype 是 `StringDType()`。这印证了「同一个 ufunc 名、按 dtype 分发到不同循环」的设计。

> 说明：`StringDType` 版的运行结果同样依据源码语义推出，未在本环境实运行（执行 Python 需要审批）。

## 5. 综合实践

把本讲的知识串起来，跟踪一条完整的调用链：**`np.strings.strip(np.array(['  aA  ']), 'a')` 从 Python 到 C 到算法**。

请按以下顺序自行梳理并填空（答案可对照前文源码链接）：

1. **Python 层**（[strings.py:L1033-L1077](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1033-L1077)）：`chars='a'` 不是 `None`，所以走哪个分支？调用哪个 ufunc？参数是什么？
2. **ufunc 分发**：输入 dtype 是 `<U6`（即 `str_`），于是从 `_strip_chars` 已注册的循环里挑哪一个编码？（提示：[L1722-L1741](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1722-L1741)，ASCII 对应 `bytes_`，UTF32 对应 `str_`。）
3. **resolve_descriptors**（[L826-L848](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L826-L848)）：输出描述符被设成什么？返回值是什么？
4. **循环**（[L448-L475](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L448-L475)）：逐元素调用哪个算法函数？传进去的 `STRIPTYPE` 是哪个值？
5. **算法**（[string_buffer.h:L1225-L1349](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L1225-L1349)）：对 `'  aA  '` 与字符集 `'a'`，左剥为什么立刻停在 `new_start=0`？右剥为什么立刻停在 `new_stop=6`？最终 `memcpy` 的长度是多少？

**参考答案**：

1. 走 `return _strip_chars(a, chars)`；调用 ufunc `_strip_chars`，参数 `(a, 'a')`。
2. 挑 `ENCODING::UTF32` 版循环（`str_` → `PyArray_UnicodeDType`）。
3. `loop_descrs[2] = loop_descrs[0]`，输出 dtype 等于输入 `<U6`；返回 `NPY_NO_CASTING`。
4. 调用 `string_lrstrip_chars`，`STRIPTYPE = BOTHSTRIP`（因为名字是 `_strip_chars`）。
5. 左剥：第一个字符是空格，`find_char` 在 `'a'` 中查不到空格（`res < 0`）→ 立即 `break`，`new_start=0`；右剥：最后一个字符也是空格 → 立即 `break`，`new_stop=6`；`memcpy` 长度 = `new_stop - new_start = 6`，即原样复制，剩余 0 字节补 0。结果 `'  aA  '` 不变。

完成这条链路后，你就把本讲的「双分支分发 → ufunc 注册 → resolve_descriptors → 共用循环 → 剥离算法」五层全部打通了。

## 6. 本讲小结

- `strip`/`lstrip`/`rstrip` 的 Python 包装极薄：只有 `@set_module`（**无 dispatcher**），函数体仅按 `chars is None` 在 `_xxx_whitespace` 与 `_xxx_chars` 两条 C ufunc 路径间二选一。
- 与 `center`/`ljust` 不同，strip 族**不预算输出宽度、不预分配 `out`**，因为它的 `resolve_descriptors` 直接令「输出 dtype == 输入 dtype」（路径 C）——输出只会变短，字段宽度不变、右端补 0。
- 6 个底层 ufunc（3 方向 × 2 模式）只对应 **2 个循环函数**：方向（左/右/两端）靠 `STRIPTYPE` 静态数据共用循环，模式（空白/字符集）因输入个数不同而拆成两个函数。
- `chars` 是**字符集合**而非子串，从两端向内剥离、遇非目标即停；因此「被保护字符夹住的目标字符」不会被剥（`strip('  aA  ', 'a')` 原样不变）。
- `StringDType('T')` 与定长 `S`/`U` 共用同一组 ufunc 名字，靠 dtype 自动分发到各自循环——所以 strip 的 Python 层不需要 `char == 'T'` 分流。

## 7. 下一步学习建议

- 顺着「Python 包装 → C 循环」的套路继续，下一讲 [u2-l11 切分与切片：partition/rpartition/slice](u2-l11-partition-and-slice.md) 会引入**结构化 dtype 输出**（`partition` 的三段字段）和 `np._NoValue` 哨兵，是 Python 层更复杂的一类包装。
- 如果想深入 C 层注册机制的全貌（`add_loop` 模板、`ENCODING` 枚举、`init_ufunc`），可跳到 [u3-l12 C++ ufunc 循环注册：string_ufuncs.cpp](u3-l12-cpp-ufunc-registration.md)，那里会系统讲解本讲提到的 `init_ufunc` 与 `resolve_descriptors` 在整个字符串 ufunc 家族中的位置。
- 想了解变长 `StringDType` 为何需要独立循环文件，可参考 [u3-l14 StringDType（'T'）专用 ufunc 循环](u3-l14-stringdtype-ufuncs.md)。

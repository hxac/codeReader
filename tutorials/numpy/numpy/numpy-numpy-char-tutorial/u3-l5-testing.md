# 测试体系与 _vec_string

## 1. 本讲目标

本讲是单元 u3（专家层）的最后一篇，把前面几讲学到的「char 门面 → defchararray 实现 → strings 现代层」三层关系，落到**测试**这一工程视角上。学完后你应该能够：

- 看懂 `numpy/_core/tests/test_defchararray.py` 的整体组织，说出每个测试类在测什么、和哪一篇讲义对应。
- 解释 `ignore_charray_deprecation` 这个 pytest 标记为什么存在、它的过滤正则如何写、为什么有些测试类**故意不加**它。
- 理解 `_vec_string` 这个 C 层底层入口在测试中扮演的角色，以及它和 `numpy.strings` 自由函数的关系。
- 自己动手写一个 `pytest.mark.filterwarnings` 标记，让一个调用 `np.char.asarray` 的测试既通过、又不在测试报告中冒出 `DeprecationWarning`。

本讲承接 u2-l1（弃用警告的来源）与 u3-l3（`array`/`asarray` 工厂函数），不再重复它们的细节，而是用测试来「反向验证」那些机制。

## 2. 前置知识

在进入测试之前，先用一句话回顾三个关键事实（详见 u2-l1、u3-l3、u3-l4）：

1. **只有三个名字会触发弃用警告**：`chararray`、`array`、`asarray`。它们被收集在 `numpy/char/__init__.py` 的 `__DEPRECATED` 集合里，访问时由模块级 `__getattr__` 发出 `DeprecationWarning`。其余几十个自由函数（`upper`、`add`、`equal`……）**不会**触发警告。
2. **警告文本是写死的**：无论你访问的是 `chararray`、`array` 还是 `asarray`，告警消息都以 "The chararray class is deprecated..." 开头。这一点对写过滤正则至关重要。
3. **`_vec_string` 是 C 层调度器**：`numpy.strings` 里那些「还没变成 ufunc」的字符串方法（如 `upper`、`mod`、`decode`）最终都委托给 `_vec_string`，由它逐元素调用 Python 的 `str`/`bytes` 方法。

还需要一点 pytest 的背景：`@pytest.mark.filterwarnings("action:message:category")` 是一个**逐测试**的标记，等价于在测试内部调用 `warnings.filterwarnings(...)`。其中 `message` 是一段正则，会被拿去和警告消息的**开头**做匹配（`re.match`，且默认大小写不敏感）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [numpy/_core/tests/test_defchararray.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_defchararray.py) | char / defchararray 的主测试文件 | 测试类划分、`ignore_charray_deprecation` 标记、`TestVecString` |
| [numpy/char/__init__.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py) | char 门面，发弃用警告的地方 | 警告消息文本与 `__DEPRECATED` |
| [numpy/_core/defchararray.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py) | 真正实现层 | 被测的 `chararray` 类、`array`/`asarray`、自由函数 |
| [numpy/_core/src/multiarray/multiarraymodule.c](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/multiarraymodule.c) | `_vec_string` 的 C 实现 | 底层入口的工作机理 |
| [numpy/_core/strings.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py) | 现代层，`_vec_string` 的调用方 | 哪些函数仍走 `_vec_string` |

## 4. 核心概念与源码讲解

### 4.1 测试组织：以测试类映射功能模块

#### 4.1.1 概念说明

`test_defchararray.py` 不是一锅大杂烩，而是把 char 的功能切成若干「测试类（`TestXxx`）」，每个类对应一个功能簇。这种**按功能分块、用类聚合相关用例**的组织方式，让测试和被测代码一一对应，也方便定位回归。

更关键的是：测试文件本身就**隐式地表达了一个设计判断**——哪些用例会触碰已弃用的 `chararray`，哪些不会。这一判断直接体现在「类上有没有挂 `@ignore_charray_deprecation`」。

#### 4.1.2 核心流程

测试文件的大致结构如下（按出现顺序）：

```
test_defchararray.py
├── 导入：pytest, numpy, _vec_string, numpy.testing 断言工具
├── 模块级常量：ignore_charray_deprecation（弃用过滤标记）
├── TestBasic            ← @ignore_charray_deprecation  测 array/asarray/chararray 构造
├── TestVecString        ← 无标记                        测 _vec_string 的错误路径
├── TestWhitespace       ← @ignore_charray_deprecation  测比较前的 rstrip 语义
├── TestChar             ← @ignore_charray_deprecation  测 'c' dtype 的 chararray
├── TestComparisons      ← @ignore_charray_deprecation  测六个比较运算符
├── TestComparisonsMixed1/2 ← @ignore_charray_deprecation  继承 TestComparisons
├── TestInformation      ← @ignore_charray_deprecation  测 str_len/count/find/is*
├── TestMethods          ← @ignore_charray_deprecation  测 capitalize/center/upper/...
├── TestOperations       ← @ignore_charray_deprecation  测 +、*、%、argsort
├── TestMethodsEmptyArray ← 无标记                        测空数组的自由函数行为
├── TestMethodsScalarValues ← 无标记                     测标量输入的自由函数行为
└── test_empty_indexing  ← @ignore_charray_deprecation  模块级函数
```

一条贯穿全表的规律：

> **凡是直接用到 `chararray` / `array` / `asarray` 的类，都挂了 `@ignore_charray_deprecation`；只用自由函数（`np.char.mod`、`np.char.decode`）或只用 `_vec_string` 的类，不挂。**

这条规律正是 u2-l1 弃用机制的「测试侧证据」：自由函数不走 `__DEPRECATED` 分支，不报警，自然无需过滤。

#### 4.1.3 源码精读

先看文件顶部的导入与标记定义：

导入区把底层入口 `_vec_string` 直接从 C 模块拿进来，测试断言则统一用 `numpy.testing`：

[numpy/_core/tests/test_defchararray.py:L1-L11](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_defchararray.py#L1-L11) — 导入 `pytest`、`numpy`、C 层 `_vec_string` 以及一组 `numpy.testing` 断言工具（`assert_equal` / `assert_array_equal` / `assert_raises` 等）。

`TestBasic` 是被标记的典型例子，它专门测 u3-l3 讲过的 `array` / `asarray` 工厂：

[numpy/_core/tests/test_defchararray.py:L21-L29](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_defchararray.py#L21-L29) — 类装饰器 `@ignore_charray_deprecation` + `class TestBasic`；`test_from_object_array` 用 object 数组喂给 `np.char.array`，断言 `itemsize == 10`（按最长元素定宽）且尾部空白被剥成 `b'long'`。

而 `TestMethodsScalarValues` 则是「不挂标记」的反例，它只把标量喂给自由函数，从不触碰三个弃用名：

[numpy/_core/tests/test_defchararray.py:L829-L837](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_defchararray.py#L829-L837) — 无装饰器的 `TestMethodsScalarValues`，`test_mod` 调用的是自由函数 `np.char.mod(...)`，输入是普通 `dtype='S'` 数组，整条路径不经过 `__DEPRECATED`，故不报警、也不需要过滤标记。

> **交叉验证**：被测的 `array` / `asarray` 实现见 [numpy/_core/defchararray.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py)（详见 u3-l3）；而它们之所以报警，是因为名字进了 [numpy/char/__init__.py:L3](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L3) 的 `__DEPRECATED`（详见 u2-l1）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：验证「挂标记 ↔ 用了弃用名」这条规律。
2. **操作步骤**：在 `test_defchararray.py` 中，用 `TestVecString`、`TestMethodsEmptyArray`、`TestMethodsScalarValues` 三个**未挂标记**的类为对象，逐一打开它们的测试方法，确认它们的方法体里只出现自由函数（`np.char.xxx`）或 `_vec_string`，**完全没有** `np.char.chararray`、`np.char.array`、`np.char.asarray`、或 `.view(np.char.chararray)`。
3. **需要观察的现象**：这三个类里即便调用了 `np.char.encode` / `np.char.decode`（这些访问也会穿过模块级 `__getattr__`），但因为名字不在 `__DEPRECATED` 里，所以全程不报警。
4. **预期结果**：你能为每个「未挂标记」的类，给出一句「因为它只用了……，不触碰弃用名」的解释。
5. 如果想运行确认，可执行 `python -m pytest numpy/_core/tests/test_defchararray.py::TestMethodsScalarValues -W error::DeprecationWarning`，预期全部通过（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`TestComparisonsMixed1` 继承自 `TestComparisons`，但它自己只重写了 `B()` 方法，为什么源码里还是给它**再挂一次** `@ignore_charray_deprecation`？

> **参考答案**：这是 NumPy 刻意采用的「显式即安全」写法。pytest 的标记作用域与 `pytestmark` 在继承链上的传播有诸多细节（例如某个类若被**其它**标记装饰，可能用自身的 `pytestmark` 列表遮蔽父类的同名属性）。与其依赖「父类标记会不会传给子类」这种易变行为，不如在每个**会触碰弃用名**的类上都显式挂一次，确保它从父类**继承来的**那些比较用例（它们会执行被重写的 `B()`，进而 `.view(np.char.chararray)`）在严格模式（`-W error`）下也被覆盖。可观察的事实是：源码里每个会触发 chararray 的类（含全部子类）都各自挂了标记。

**练习 2**：模块级函数 `test_empty_indexing`（见文件末尾）为什么要单独挂标记，而不是放进某个类里？

> **参考答案**：它直接 `np.char.chararray((4,))`，触碰了弃用名，必须过滤；而它是个游离的回归测试（ticket 1948），逻辑上不属于任何已有测试类，于是作为模块级函数独立存在，并自行挂上标记。

---

### 4.2 弃用过滤：ignore_charray_deprecation 标记

#### 4.2.1 概念说明

`ignore_charray_deprecation` 是一个**模块级常量**，值是一个 pytest 标记。它解决的问题很具体：`chararray` / `array` / `asarray` 在 NumPy 2.5 被软弃用，每次访问都报警；但 `test_defchararray.py` 的本职工作恰恰就是测试这些（暂时还没删的）对象。如果不做处理，要么测试报告里警告刷屏，要么在「警告即错误」的严格模式下直接挂掉。

这个标记的作用就是：**对这些特定的弃用警告，在当前测试内静默**，既不影响断言，也不污染输出。

#### 4.2.2 核心流程

pytest 的 `filterwarnings` 标记字符串用冒号分成三段 `action:message:category`：

- `action` = `ignore`：匹配到的警告直接丢弃。
- `message` = 一段正则，匹配警告消息的**开头**（CPython 用 `re.compile(message, re.I).match(...)`，即大小写不敏感、锚定在起始）。
- `category` = `DeprecationWarning`：只过滤这一类警告。

匹配能否成功，取决于 `message` 正则能否匹配到「The chararray class is deprecated...」的开头。等价关系是：

\[ \text{标记生效} \iff \text{regex.match}(\text{"The chararray class is deprecated..."}) \neq \text{None} \]

#### 4.2.3 源码精读

先看标记本身的定义：

[numpy/_core/tests/test_defchararray.py:L16-L18](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_defchararray.py#L16-L18) — 定义 `ignore_charray_deprecation`，正则为 `r"\w+ (chararray|array|asarray) \w+"`，分类为 `DeprecationWarning`。

把这段正则和实际警告消息对齐着读：

```
消息：The   chararray   class   is deprecated ...
正则：\w+  (chararray|…) \w+
      ───   ───────────  ────
      The   chararray    class
```

- `\w+` 吃掉 `The`；
- `(chararray|array|asarray)` 命中 `chararray`；
- `\w+` 吃掉 `class`；
- 因为是 `re.match`，只要匹配**开头**即可，后面 `is deprecated...` 不用管。

再看报警侧——消息文本是写死的，与被访问的名字无关：

[numpy/char/__init__.py:L11-L18](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L11-L18) — `warnings.warn(...)` 的消息字符串恒为 "The chararray class is deprecated and will be removed..."，**不随 `name` 变化**。

> **关键洞察**：正则里的 `(chararray|array|asarray)` 三选一看似在区分三个名字，但因为消息里**永远**写的是 "chararray class"，实际只会命中 `chararray` 这一支。这个三选一是「防御性/历史性」写法——哪怕将来消息文案改成随名字变化，它也能继续兜住。这也是为什么 u2-l1 / u3-l4 强调「测试过滤用的正则依赖固定文案」。

#### 4.2.4 代码实践（可运行）

这是本讲的核心实践：仿照 `ignore_charray_deprecation`，自己写一个标记，并验证它的效果。

1. **实践目标**：亲手写一个 `filterwarnings` 标记，让一个调用 `np.char.asarray` 的测试在 `-W error::DeprecationWarning` 下仍能通过。
2. **操作步骤**：新建一个临时文件（**示例代码**，不要写进仓库）：

   ```python
   # tmp_test_marker.py —— 示例代码
   import pytest
   import numpy as np

   # 仿照 ignore_charray_deprecation：正则必须匹配 "The chararray class" 这个开头
   ignore_charray = pytest.mark.filterwarnings(
       r"ignore:\w+ chararray \w+:DeprecationWarning"
   )

   @ignore_charray
   def test_asarray_quiet():
       a = np.char.asarray(['abc', 'de'])   # 触碰弃用名，会报警
       assert a.dtype.itemsize == 3 * 4     # '<U3' → 12 字节
       assert a[1] == 'de'
   ```

   然后分别用两种方式运行（待本地验证）：

   ```bash
   # (a) 把弃用警告升级为错误：没有标记会失败，有标记应通过
   python -m pytest tmp_test_marker.py -W error::DeprecationWarning

   # (b) 先去掉 @ignore_charray 再跑，观察测试是否因 DeprecationWarning 被当成错误而失败
   ```

3. **需要观察的现象**：去掉标记、在 `-W error::DeprecationWarning` 下，测试应当**失败**，报错指向 `np.char.asarray` 抛出的 `DeprecationWarning`；加上标记后，同一测试**通过**，且测试摘要里不再出现该警告。
4. **预期结果**：你写出的正则只要能匹配 "The chararray class" 开头即可生效——例如 `r"ignore:The chararray:DeprecationWarning"`、`r"ignore::DeprecationWarning"`（空 message 匹配所有同类警告）都能让测试通过。注意写成 `r"ignore:chararray:DeprecationWarning"` 会**失效**，因为消息开头是 `The `，`re.match` 锚在起始，匹配不到 `chararray`。
5. 若无法本地运行，标注「待本地验证」，但上面的正则结论可由 `re.match` 语义直接推出。

#### 4.2.5 小练习与答案

**练习 1**：把上面的标记正则改成 `r"ignore::DeprecationWarning"`（中间 message 为空）会怎样？

> **参考答案**：它匹配**所有** `DeprecationWarning`，测试同样能通过。代价是「打击面」过大——万一被测代码里意外冒出**别的** `DeprecationWarning`（比如某个依赖库的），也会被一起静默，掩盖问题。所以官方选了更精确的 `\w+ (chararray|array|asarray) \w+` 而非空 message。

**练习 2**：为什么 `ignore_charray_deprecation` 用的是 `re.match`（锚定开头）语义，却还要在正则末尾放一个 `\w+`？

> **参考答案**：`re.match` 已锚定开头，结尾不需要 `$`。末尾的 `\w+` 作用是**收紧匹配**——确保命中位置确实是「The chararray **class**」这种结构，而不是某条恰好以 "The chararray" 开头但语义无关的消息。这是一种用正则形状表达「我只认这条特定告警」的约束。

---

### 4.3 _vec_string：C 层底层入口与 TestVecString

#### 4.3.1 概念说明

`_vec_string` 是定义在 C 扩展模块 `numpy._core.multiarray` 里的底层函数。它做的事情很朴素：给定一个字符串数组、一个输出 dtype、一个方法名（和可选参数），**逐元素**地对该数组的每个标量调用对应的 Python `str`/`bytes` 方法，把结果写回一个新数组。

它是「前 ufunc 时代」的产物。u2-l2 提到，`numpy.strings` 里的函数分两类：一类已经做成真正的 ufunc（`add`、`equal` 等，C 层直接向量执行）；另一类（`upper`、`mod`、`decode`、`translate` 等）**仍走 `_vec_string`**，本质是 Python 层的逐元素循环。

#### 4.3.2 核心流程

`_vec_string` 的调用签名（从 C 的 `PyArg_ParseTuple` 反推）是：

```
_vec_string(array, out_dtype, method_name, args_seq=None)
```

执行流程：

```
1. 把 array 转成 ndarray，把 out_dtype 转成 dtype 描述符。
2. 按 array 的 dtype 选「方法宿主类型」：
     NPY_STRING (bytes)     → 在 bytes 类型上 getattr(method)
     NPY_UNICODE / StringDType (str) → 在 str 类型上 getattr(method)
     用户自定义字符串 dtype → 在其 scalar_type 上 getattr(method)
     其它                   → 报错（非字符串数组）
3. 若 args_seq 为空：走 _vec_string_no_args——
     遍历 array 每个标量 s，调用 method(s)，写入输出数组。
   若 args_seq 非空：走 _vec_string_with_args——
     把 array 与 args_seq 广播，遍历每组的 (s, *args)，
     调用 method(s, *args)，写入输出数组。
```

注意第 2 步：`_vec_string` 不直接认识 `'upper'`，它只是**按名字**去 `bytes`/`str` 类型上查方法。所以传入一个不存在的名字（如 `'bogus'`）会触发 `AttributeError`——这正是测试要覆盖的错误路径。

#### 4.3.3 源码精读

先看现代层如何使用它（`numpy.strings.upper` 的全部实现只有两行）：

[numpy/_core/strings.py:L1118-L1119](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L1118-L1119) — `a_arr = np.asarray(a)` 后直接 `return _vec_string(a_arr, a_arr.dtype, 'upper')`，把"对每个元素调 `str.upper`"完全交给 C 层。

而哪些函数仍走 `_vec_string`，`strings.py` 顶部的 `__all__` 注释写得很清楚：

[numpy/_core/strings.py:L82-L90](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L82-L90) — `__all__` 里把名字分组：已经 ufunc 化的一批；标注「Will gradually become ufuncs」的 `upper/lower/swapcase/capitalize/title`（暂时仍走 `_vec_string`）；标注「Will probably not become ufuncs」的 `mod/decode/encode/translate`；以及 `join/split/rsplit/splitlines` 因行为未稳定暂被注释在外（这就是 char 要从私有模块捞它们的原因，见 u2-l2）。

再看 C 层主入口的分发逻辑（按 dtype 选宿主类型）：

[numpy/_core/src/multiarray/multiarraymodule.c:L4133-L4185](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/multiarraymodule.c#L4133-L4185) — `_vec_string` 解析四个参数，按 `NPY_STRING` / `NPY_UNICODE` / 用户自定义分别从 `PyBytes_Type` / `PyUnicode_Type` / scalar 类型上 `getattr` 方法；最后根据 `args_seq` 是否为空，分派到 `_vec_string_no_args` 或 `_vec_string_with_args`。

无参版本的逐元素循环（`upper`、`lower` 等走的就是它）：

[numpy/_core/src/multiarray/multiarraymodule.c:L4063-L4130](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/multiarraymodule.c#L4063-L4130) — `_vec_string_no_args` 注释说明这是「无额外参数时的快路径，不需要广播迭代器」；循环里把每个元素取出为标量、`PyObject_CallFunctionObjArgs(method, item, NULL)` 调用方法、再用 `PyArray_SETITEM` 写回输出数组。

最后是测试侧——`TestVecString` 专门测 `_vec_string` 的**错误契约**，这是它在本讲的特殊地位：

[numpy/_core/tests/test_defchararray.py:L93-L141](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_defchararray.py#L93-L141) — `TestVecString` 七个用例分别断言不同非法输入抛出的异常：不存在的方法→`AttributeError`、非字符串数组→`TypeError`、args 不是元组→`TypeError`、非法 dtype 描述符→`TypeError`、结果类型与函数不匹配→`TypeError`、形状无法广播→`ValueError`。注意这个类**没有挂** `@ignore_charray_deprecation`，因为它只测 C 层入口本身，不碰任何弃用名。

> **为什么直接测 `_vec_string`？** 这些用例验证的是「C 层入口的输入校验」，与上层 `numpy.strings` / `numpy.char` 的包装无关。把它们放在最低层测，能精确定位是 C 调度器本身的行为，还是上层包装引入的行为。

#### 4.3.4 代码实践（可运行）

1. **实践目标**：直接调用 `_vec_string`，亲手复现 `numpy.strings.upper` 的底层行为，并触发一个错误路径。
2. **操作步骤**（示例代码）：

   ```python
   # tmp_vec_string.py —— 示例代码
   import numpy as np
   from numpy._core.multiarray import _vec_string

   a = np.array(['ab', 'cd'])                 # dtype '<U2'
   out = _vec_string(a, a.dtype, 'upper')     # 等价于 np.strings.upper(a)
   print(out)                                 # 预期 ['AB' 'CD']

   # 复现 TestVecString.test_non_existent_method
   try:
       _vec_string('a', np.bytes_, 'bogus')
   except AttributeError as e:
       print('AttributeError:', e)            # bytes 没有 'bogus' 方法
   ```

3. **需要观察的现象**：第一段输出的 `out` 与 `np.strings.upper(a)`、`np.char.upper(a)` 完全一致（三者最终都落到同一个 C 调用）；第二段抛 `AttributeError`，和测试断言一致。
4. **预期结果**：`out == np.array(['AB', 'CD'])`，dtype `<U2`；`AttributeError` 被捕获。（待本地验证）
5. 进阶：把 `'upper'` 换成 `'find'` 并传入参数 `(['a', 'c'],)`，观察它走的是 `_vec_string_with_args` 分支，结果是一个整数数组 `[0, 0]`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_vec_string` 需要 `_vec_string_no_args` 和 `_vec_string_with_args` 两个实现，而不是合并成一个？

> **参考答案**：无参时无需广播——只有一个输入数组，用普通迭代器逐元素取标量即可；带参时需要把输入数组和参数数组**广播**到同一形状，再同步迭代（`PyArray_MultiIter`）。注释明确说「broadcast 迭代器对单参数不适用」，所以拆成两条快/慢路径，无参的更省。

**练习 2**：`_vec_string(np.array(['a','b']), np.int_, 'upper')` 会发生什么？对应哪条测试？

> **参考答案**：`'upper'` 返回字符串，但指定的输出 dtype 是 `np.int_`，C 层在 `PyArray_SETITEM` 时无法把字符串写进整数数组，于是抛 `TypeError: result array type does not match underlying function`。这对应 `TestVecString.test_invalid_result_type`（断言 `TypeError`）。

---

## 5. 综合实践

把本讲三个最小模块串起来，写一个迷你测试模块 `tmp_char_test.py`（**示例代码**，不入库），要求它同时包含三类用例，并解释它们各自是否需要 `ignore_charray_deprecation`：

```python
# tmp_char_test.py —— 示例代码
import pytest
import numpy as np
from numpy._core.multiarray import _vec_string

ignore_charray = pytest.mark.filterwarnings(
    r"ignore:\w+ (chararray|array|asarray) \w+:DeprecationWarning"
)

@ignore_charray                      # (1) 用了 asarray → 必须过滤
def test_factory_path():
    a = np.char.asarray(['x', 'yy'])
    assert a.dtype.itemsize == 2 * 4

def test_free_function_path():       # (2) 只用自由函数 → 不需要过滤
    assert list(np.char.upper(np.array(['a', 'b']))) == ['A', 'B']

def test_vec_string_path():          # (3) 直接测 C 层 → 不需要过滤
    out = _vec_string(np.array(['a', 'b']), np.str_, 'upper')
    assert list(out) == ['A', 'B']
```

**任务**：

1. 运行 `python -m pytest tmp_char_test.py -W error::DeprecationWarning`，预期三个用例**全部通过**（待本地验证）。
2. **删掉**用例 (1) 上的 `@ignore_charray`，再跑一次，预期**只有 (1) 失败**（`DeprecationWarning` 被升级为错误），(2)(3) 仍然通过。这一对照直接证明：弃用警告只来自 `chararray`/`array`/`asarray` 三个名字，自由函数与 `_vec_string` 路径干净。
3. 用一句话写下三类用例与「是否需要过滤」的对应关系，作为本讲的总结。

## 6. 本讲小结

- `test_defchararray.py` 按**功能簇**划分测试类，且「是否挂 `@ignore_charray_deprecation`」精确反映了该类是否触碰 `chararray`/`array`/`asarray` 三个弃用名。
- `ignore_charray_deprecation` 是 `pytest.mark.filterwarnings` 标记，靠正则 `\w+ (chararray|array|asarray) \w+` 匹配警告开头；它之所以稳定，是因为告警文案写死为 "The chararray class is deprecated..."。
- `filterwarnings` 的 `message` 段是 `re.match`（锚定起始、大小写不敏感）的 regex，空 message 会匹配全部同类警告，故官方选了更精确的写法。
- `_vec_string` 是 C 层逐元素调度器，按数组 dtype 在 `bytes`/`str`/用户类型上按名字查方法；`numpy.strings` 里尚未 ufunc 化的函数（`upper`、`mod`、`decode` 等）仍走它。
- `TestVecString` 是唯一**直接**测 `_vec_string` 的类，专门覆盖其错误契约（`AttributeError`/`TypeError`/`ValueError`），且无需弃用过滤。
- 测试本身是机制文档：它用「挂不挂标记」反向印证了 u2-l1 的弃用边界，用 `TestVecString` 印证了 u2-l2 的「哪些函数仍走 `_vec_string`」。

## 7. 下一步学习建议

- **横向对比**：去读 [numpy/_core/tests/test_strings.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_strings.py)，对比它如何测试**现代层** `numpy.strings`——你会发现那里**完全不需要**任何 chararray 弃用过滤，这就是迁移后的理想形态。
- **深入 C 层**：把 [multiarraymodule.c](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/multiarraymodule.c) 中 `_vec_string_with_args` 的广播迭代（`PyArray_MultiIter`）读完，理解 `_vec_string(['abc'], np.int_, 'find', (['a','d','j'],))` 为何抛 `ValueError`（广播形状不一致）。
- **回到迁移主题**：结合 u3-l4，试着把本讲「综合实践」里的 (1) 用例，改写成不依赖 `asarray` 的等价形式（`np.array(...) + np.strings.upper`），验证它不再需要任何弃用过滤，从而亲手完成一次「迁移」。
- 如果继续本系列，可转向 numpy.strings 的 ufunc 化实现（`_umath_strings`）与 StringDType，它们是 char 之后的长期方向。

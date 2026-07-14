# vectorize 与 gufunc 签名解析

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `np.vectorize` 到底是不是一个真正的 ufunc，以及它为什么「方便但不快」。
- 读懂 gufunc 签名（如 `(m,n),(n,p)->(m,p)`）每一部分的含义，并能解释「核心维度（core dimension）」与「循环/广播维度」的差别。
- 手工追踪 `_parse_gufunc_signature` 用正则把签名字符串拆成 `input_core_dims / output_core_dims` 的全过程。
- 沿着 `_update_dim_sizes` → `_parse_input_dimensions` → `_calculate_shapes` → `_create_arrays` 这条链，说清楚输入如何被拆成「广播形状 + 核心维度尺寸」、输出数组又如何被构造出来。
- 区分 `vectorize` 的两条执行路径：无签名时走 `frompyfunc`，有签名时走 `np.ndindex` 显式循环。

本讲所有源码集中在单一文件 `numpy/lib/_function_base_impl.py`，这是 numpy.lib 中体量最大的「数值函数实现仓库」之一。

## 2. 前置知识

本讲默认你已经理解以下概念（在前面讲义中已建立）：

- **ufunc（通用函数）**：numpy 里对数组做逐元素运算的、底层为 C 编译的函数，如 `np.add`、`np.sqrt`。它天然支持广播，且非常快。
- **广播（broadcasting）**：形状不同的数组按规则对齐到公共形状再运算的机制。u5-l2 讲过，广播的本质是「插入步长为 0 的维度」，结果是零拷贝视图。
- **`__array_interface__` 与 `as_strided`**：u5-l1 讲过，可以用 `as_strided` 改写 shape/strides 制造「零内容的占位数组」，本讲 `_parse_input_dimensions` 会用到这个技巧。
- **dispatcher + impl 双函数写法与 `_impl` 分层**：u1-l2 讲过，`vectorize` 同样没有私有「薄再导出模块」，而是由顶层 `numpy/__init__.py` 直接 `from .lib._function_base_impl import vectorize` 取名暴露到 `np.`；其 `__module__` 被 `@set_module('numpy')` 钉为 `numpy`。

本讲引入两个新术语：

- **gufunc（generalized ufunc，广义 ufunc）**：普通 ufunc 的每个「元素」是标量；gufunc 的每个「元素」可以是一个小数组（如矩阵）。`np.matmul` 就是一个 gufunc，它的「元素」是矩阵，对矩阵做矩阵乘法，再在外层（循环维度）上广播。
- **核心维度（core dimension）与循环维度（loop dimension）**：这是理解 gufunc 的关键。核心维度是「元素内部」的维度，参与 `pyfunc` 的真实计算；循环维度是「元素之外」的维度，只用来广播和迭代。签名 `(n)->()` 里的 `n` 是核心维度，输入数组其余的维度都是循环维度。

## 3. 本讲源码地图

本讲只涉及一个源文件，外加它的测试文件：

| 文件 | 作用 |
| --- | --- |
| `numpy/lib/_function_base_impl.py` | `vectorize` 类及其全部私有助手（签名解析、维度拆解、输出构造、两条执行路径）都在这里。 |
| `numpy/lib/tests/test_function_base.py` | `TestVectorize` 测试类（约 L1591 起）提供了大量可验证的签名用法与边界用例，是本讲代码实践的依据。 |

本讲涉及的关键符号及其所在行：

- 签名正则定义：`_DIMENSION_NAME / _CORE_DIMENSION_LIST / _ARGUMENT / _ARGUMENT_LIST / _SIGNATURE`
- 签名解析：`_parse_gufunc_signature`
- 维度一致性校验：`_update_dim_sizes`
- 输入维度拆解：`_parse_input_dimensions`
- 形状计算：`_calculate_shapes`
- 输出数组分配：`_create_arrays`
- 字符串 dtype 归一化：`_get_vectorize_dtype`
- 主类：`vectorize`（含 `__init__` / `__call__` / `_call_as_normal` / `_get_ufunc_and_otypes` / `_vectorize_call` / `_vectorize_call_with_signature`）

## 4. 核心概念与源码讲解

### 4.1 vectorize 类：从普通函数到「广义 ufunc」包装器

#### 4.1.1 概念说明

`np.vectorize(pyfunc, otypes=None, ..., signature=None)` 接收一个普通 Python 函数 `pyfunc`，返回一个**可调用对象**（`vectorize` 的实例），它表现得像一个「能吃数组」的函数。

最关键的一点要先说清楚：**`vectorize` 不是编译型 ufunc，本质上是一个 for 循环**。它的 docstring 明确写道：

> The `vectorize` function is provided primarily for convenience, not for performance. The implementation is essentially a for loop.

它的价值在于「方便」：让你用广播规则调用一个只懂标量（或只懂小数组）的函数，而不用自己手写 `np.ndindex` 循环。它的代价是慢——因为每个元素都要回调 Python。如果你追求性能，应该直接写 ufunc 或用 `numpy` 原生向量化操作。

`vectorize` 的核心参数：

- `pyfunc`：被包装的函数。可省略——省略时 `vectorize(otypes=...)` 返回一个**装饰器**，支持 `@np.vectorize` 语法。
- `otypes`：输出类型。可以是类型字符组成的字符串（如 `'i'`、`'ff'`），也可以是 dtype 列表（如 `[float]`）。不指定时，`vectorize` 会**先用输入的第一个元素调一次 `pyfunc` 来推断**输出个数与类型。
- `excluded`：一个集合，列出哪些位置参数或关键字参数**不参与向量化**，原样透传给 `pyfunc`（如 `polyval` 的系数向量）。
- `cache`：是否缓存「探测调用」的结果，避免对第一个元素重复调用。
- `signature`：gufunc 签名字符串。不指定时，`pyfunc` 被假定接收标量、返回标量；指定后，`pyfunc` 接收/返回的是「核心维度」描述的小数组。

#### 4.1.2 核心流程

`vectorize` 的初始化与调用流程：

```text
np.vectorize(pyfunc, otypes, signature=...)    # 构造：解析 otypes 与 signature
        │
        ▼
vfunc(*args)  ──►  __call__                     # 既支持装饰器绑定，也支持正常调用
        │
        ▼
_call_as_normal:                                 # 处理 excluded 参数
        │  若无 kwargs 且无 excluded：直接用原 pyfunc
        │  否则：构造一个 func 包装器，把 excluded 参数原样塞回
        ▼
_vectorize_call(func, args):                     # 分发到两条路径
        │
        ├── signature is not None ──► _vectorize_call_with_signature   （本讲 4.5 重点）
        │
        └── 否则 ──► _get_ufunc_and_otypes + frompyfunc                 （本讲 4.5 重点）
```

#### 4.1.3 源码精读

类定义带 `@set_module('numpy')`，说明它对外的「家」是 `numpy` 而非 `numpy.lib._function_base_impl`：[\_function_base_impl.py:L2278-L2279](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2278-L2279) 把 `__module__` 钉为 `numpy`，配合顶层再导出，使 `np.vectorize` 成立。

`__init__` 负责「构造期」的参数规整：[\_function_base_impl.py:L2442-L2484](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2442-L2484) 做三件事——校验 `pyfunc` 可调用、规整 `otypes`（字符串逐字符校验、列表逐项用 `_get_vectorize_dtype` 归一化）、把 `signature` 交给 `_parse_gufunc_signature` 解析成 `_in_and_out_core_dims`。注意 `pyfunc` 默认值是 `np._NoValue` 哨兵，正是它让「省略 `pyfunc` 当装饰器」成为可能。

`__call__` 是入口，用一个 `np._NoValue` 判断分出装饰器路径与正常路径：[\_function_base_impl.py:L2524-L2529](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2524-L2529)。当 `pyfunc is np._NoValue`（即 `@np.vectorize` 装饰器场景），它把被装饰的函数交给 `_init_stage_2` 绑定后返回 `self`。

`excluded` 参数的处理在 `_call_as_normal`：[\_function_base_impl.py:L2494-L2522](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2494-L2522)。当有 `kwargs` 或 `excluded` 时，它构造一个闭包 `func`：闭包接收「参与向量化的参数」`vargs`，把它们按 `inds`（位置）和 `names`（关键字）塞回原参数列表，再调用真正的 `self.pyfunc`。这样 `pyfunc` 看到的始终是完整参数，而向量化只发生在非 `excluded` 参数上。

#### 4.1.4 代码实践

**实践目标**：验证 `vectorize` 的装饰器用法与 `otypes` 推断行为。

**操作步骤**：

```python
import numpy as np

# 1) 装饰器用法：省略 pyfunc，返回装饰器
@np.vectorize
def addsub(a, b):
    return a - b if a > b else a + b

print(addsub([0, 3, 6, 9], [1, 3, 5, 7]))   # 期望 [1, 6, 1, 2]

# 2) 不指定 otypes：用第一个元素推断输出类型
v = np.vectorize(lambda x: x if x > 0 else 0)
print(v([-1, 2, -3]).dtype)                  # 推断自首个元素 -1 → int64

# 3) 显式 otypes 强制类型
vf = np.vectorize(lambda x: x, otypes=[float])
print(vf([1, 2, 3]).dtype)                    # float64
```

**需要观察的现象**：第 2 步的输出 dtype 取决于「第一个元素」经 `pyfunc` 后的类型；第 3 步无论输入如何都被强制成 `float64`。

**预期结果**：与上述注释一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `np.vectorize(lambda x: x)(np.zeros(0))` 会报错？如何修复？

**答案**：输入 size 为 0 时，`vectorize` 没有「第一个元素」可用来推断 `otypes`，于是抛出 `ValueError: cannot call vectorize on size 0 inputs unless otypes is set`（见 4.5 的 `_get_ufunc_and_otypes`）。修复方法是显式给出 `otypes`，例如 `np.vectorize(lambda x: x, otypes='d')(np.zeros(0))`。

**练习 2**：`@np.vectorize` 与 `@np.vectorize(otypes=[float])` 在用法上有何区别？

**答案**：前者 `pyfunc` 缺省，返回装饰器，只能装饰函数；后者 `pyfunc` 同样缺省，但带上了关键字参数，仍是装饰器，只是预先配置了 `otypes`。两者都依赖 `__call__` 里 `pyfunc is np._NoValue` 的分支走 `_init_stage_2`。

---

### 4.2 签名 DSL：_parse_gufunc_signature 解析核心维度

#### 4.2.1 概念说明

gufunc 签名是一个微型 DSL（领域专用语言），用一串字符描述「函数的每个输入/输出元素长什么样」。例如：

- `(n)->()`：输入是一个长度为 `n` 的一维向量，输出是一个标量。`np.mean` 就是这种形状。
- `(m,n),(n,p)->(m,p)`：两个输入矩阵相乘，输出一个矩阵。这是 `np.matmul`。
- `(n),(n)->(),()`：两个等长向量输入，输出两个标量（如相关系数及其 p 值）。

括号里的小写字母（`n`、`m`、`p`）是**核心维度的名字**。同名代表「同一个尺寸」，例如 `(n),(n)` 强制两个输入向量等长；不同名（如 `(a),(b)`）代表尺寸互相独立。空括号 `()` 表示标量（无核心维度）。

#### 4.2.2 核心流程

`_parse_gufunc_signature` 的处理分三步：

1. 用 `re.sub(r'\s+', '', signature)` 删掉所有空白（所以签名里随便加空格都行）。
2. 用 `_SIGNATURE` 正则做整体校验，不匹配就抛 `ValueError`。
3. 按 `->` 切成「输入侧 / 输出侧」，每侧再用 `_ARGUMENT` 正则逐个抓出参数，最后用 `_DIMENSION_NAME` 抓出每个参数里的核心维度名，组装成嵌套元组返回。

返回结构是 `(input_core_dims, output_core_dims)`，每个都是 `List[Tuple[str, ...]]`。例如 `'(m,n),(n)->(m)'` 解析为 `([('m','n'), ('n',)], [('m',)])`。

#### 4.2.3 源码精读

先看五条层层嵌套的正则：[\_function_base_impl.py:L2153-L2157](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2153-L2157)。它们像积木一样搭出签名语法：

```python
_DIMENSION_NAME = r'\w+'                                       # 一个核心维度名：若干单词字符
_CORE_DIMENSION_LIST = f'(?:{_DIMENSION_NAME}(?:,{_DIMENSION_NAME})*)?'  # 逗号分隔的列表，可为空
_ARGUMENT = fr'\({_CORE_DIMENSION_LIST}\)'                      # 用括号包起来，如 (m,n) 或 ()
_ARGUMENT_LIST = f'{_ARGUMENT}(?:,{_ARGUMENT})*'                # 逗号分隔的若干参数
_SIGNATURE = f'^{_ARGUMENT_LIST}->{_ARGUMENT_LIST}$'            # 整体：输入列表->输出列表
```

注意 `_CORE_DIMENSION_LIST` 末尾的 `?`——它让 `()`（空括号、标量）合法；而 `_ARGUMENT_LIST` 不带 `?`，所以 `->` 两侧都至少要有一个参数，这就是测试里 `'(x),(y)->'` 会抛错的原因。

再看解析函数本体：[\_function_base_impl.py:L2160-L2182](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2160-L2182) 先删空白、再整体校验，最后用一个精巧的嵌套推导把字符串变元组：

```python
return tuple([tuple(re.findall(_DIMENSION_NAME, arg))
              for arg in re.findall(_ARGUMENT, arg_list)]
             for arg_list in signature.split('->'))
```

外层按 `->` 切成输入/输出两段；每段用 `re.findall(_ARGUMENT, ...)` 抓出所有形如 `(...)` 的参数；每个参数再用 `re.findall(_DIMENSION_NAME, ...)` 抓出括号里的名字。

测试 `test_signature_parse`（[tests/test_function_base.py:L1867-L1885](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_function_base.py#L1867-L1885)）钉死了典型输入与输出，例如 `(  ), ( a,  b,c )  ,(  d)   ->   (d  ,  e)` 解析为 `([(), ('a','b','c'), ('d',)], [('d','e')])`，同时确认 `'(x)(y)->()'`（缺逗号）、`'(x),(y)->'`（输出侧空）、`'((x))->(x)'`（双重括号）都会抛 `ValueError`。

#### 4.2.4 代码实践

**实践目标**：亲手调用私有函数 `_parse_gufunc_signature`，观察它如何把签名字符串拆成元组结构。

**操作步骤**：

```python
import numpy as np
import numpy.lib._function_base_impl as nfb

print(nfb._parse_gufunc_signature('(n)->()'))            # ([('n',)], [()])
print(nfb._parse_gufunc_signature('(m,n),(n,p)->(m,p)')) # ([('m','n'), ('n','p')], [('m','p')])
print(nfb._parse_gufunc_signature('  ( a , b ) -> ( ) '))# ([('a','b')], [()])
```

**需要观察的现象**：空白被剥离；同名维度在不同参数里保留同一个字符串；空括号变成空元组 `()`。

**预期结果**：与注释一致。

#### 4.2.5 小练习与答案

**练习 1**：签名 `(n),(n)->(),()` 描述了一个怎样的函数？输出侧的两个 `()` 分别表示什么？

**答案**：输入是两个等长（都叫 `n`）的一维向量，输出是两个标量。这正是 docstring 里 `scipy.stats.pearsonr` 的向量化例子——相关系数和 p 值，二者都是标量，所以输出侧是 `((), ())`，即两个空元组。

**练习 2**：为什么 `_parse_gufunc_signature('((x))->(x)')` 会报错？

**答案**：`_ARGUMENT` 只允许单层括号 `\(核心维度列表\)`，不支持嵌套。`((x))` 无法被 `_SIGNATURE` 整体匹配，因此抛 `ValueError: not a valid gufunc signature`。

---

### 4.3 输入维度拆解：_update_dim_sizes 与 _parse_input_dimensions

#### 4.3.1 概念说明

有了核心维度名，下一步要把**实际输入数组**拆成两部分：

- **循环维度（广播形状）**：核心维度之外的那些维度，按 numpy 广播规则对齐，决定「函数要被调用多少次」。
- **核心维度尺寸表 `dim_sizes`**：一个 `{名字: 长度}` 字典，记录每个核心维度实际是多长，供后面构造输入/输出数组用。

例如签名 `(n)->()`，输入 `[[1,3],[2,4]]`（形状 `(2,2)`）：最后一维 `2` 是核心维度 `n`，前面的 `(2,)` 是循环维度，于是函数会被调用 2 次，每次拿到一个长度为 2 的向量。

这里有一条硬规则：**同名的核心维度必须在所有输入里尺寸一致**，否则报错。这就是 `(n),(n)->()` 强制两个向量等长的来源。

#### 4.3.2 核心流程

`_parse_input_dimensions` 对每个输入参数：

1. 调 `_update_dim_sizes` 更新/校验核心维度尺寸表 `dim_sizes`；
2. 算出循环维度数 `ndim = arg.ndim - len(core_dims)`；
3. 用 `as_strided(0, arg.shape[:ndim])` 造一个**只有循环形状、没有真实数据**的「占位数组」；
4. 把所有占位数组交给 `_broadcast_shape`（u5-l2 讲过的广播内核）算出公共广播形状。

返回 `(broadcast_shape, dim_sizes)`。

#### 4.3.3 源码精读

`_update_dim_sizes` 负责一致性校验：[\_function_base_impl.py:L2185-L2216](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2185-L2216) 取参数末尾 `len(core_dims)` 维作为核心维度，逐个名字比对：名字已在表里且尺寸不符就抛 `inconsistent size for core dimension`；名字不在表里就登记。它还校验「参数维度够不够」（`arg.ndim < num_core_dims` 时报错），这正是测试 `test_signature_invalid_inputs` 里 `f(1, 2)` 报 `does not have enough dimensions` 的来源。

`_parse_input_dimensions` 把校验与广播合到一起：[\_function_base_impl.py:L2219-L2247](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2219-L2247)。最巧妙的一行是：

```python
dummy_array = np.lib.stride_tricks.as_strided(0, arg.shape[:ndim])
```

它用 u5-l1 讲过的 `as_strided`，把标量 `0`「撑」成指定形状的视图——**不分配任何真实数据**，只为了借它的 shape 去参与广播推导。这是一种典型的「借形不借值」技巧，避免为广播预检分配大数组。最后调用 u5-l2 的内部内核 `_broadcast_shape(*broadcast_args)` 算出公共形状（这里特意用 `np.lib._stride_tricks_impl._broadcast_shape` 而非公开 `broadcast_shapes`，是因为它需要直接拿到元组结果）。

#### 4.3.4 代码实践

**实践目标**：手工模拟 `_parse_input_dimensions` 的拆解过程，验证「循环形状 + 核心尺寸表」的正确性。

**操作步骤**：

```python
import numpy as np
import numpy.lib._function_base_impl as nfb

# 签名 (n)->()，输入形状 (2,2)：核心维 n=2，循环形状 (2,)
a = np.array([[1, 3], [2, 4]])
in_core, out_core = nfb._parse_gufunc_signature('(n)->()')
bshape, dims = nfb._parse_input_dimensions((a,), in_core)
print('broadcast_shape =', bshape)   # (2,)
print('dim_sizes =', dims)           # {'n': 2}
```

**需要观察的现象**：`bshape` 是 `(2,)`（要调用 2 次函数），`dim_sizes` 把名字 `n` 映射到长度 `2`。

**预期结果**：与注释一致。

#### 4.3.5 小练习与答案

**练习 1**：对签名 `(n),(n)->(n)`，输入 `([1,2], [1,2,3])` 会发生什么？为什么？

**答案**：会抛 `ValueError: inconsistent size for core dimension 'n'`。因为两个输入的核心维度都叫 `n`，`_update_dim_sizes` 第一次登记 `n=2`，第二次发现 `n=3` 不符。这与 `test_signature_invalid_inputs` 的断言一致。

**练习 2**：`as_strided(0, shape)` 造的占位数组为什么不会引发越界错误？

**答案**：因为它**从不被读取数据**，只被 `_broadcast_shape` 用来读取 `.shape` 属性推导广播形状。`as_strided` 本身的越界风险只在实际访问数据时才暴露，而这里全程不读元素，所以安全。

---

### 4.4 输出数组构造：_calculate_shapes、_create_arrays、_get_vectorize_dtype

#### 4.4.1 概念说明

知道了循环形状 `broadcast_shape` 和核心尺寸表 `dim_sizes`，每个输入/输出数组的完整形状就是「循环形状 + 自己的核心维度尺寸」。例如签名 `(n)->(n)`，循环形状 `(2,)`、`n=2`，则输出形状是 `(2,) + (2,) = (2,2)`。

输出数组的 dtype 有两个来源：要么用户在 `otypes` 里指定，要么由「第一次调用 `pyfunc` 的真实返回值」决定（见 4.5）。`_create_arrays` 在第一次拿到真实返回值后，用 `np.empty_like` 来保留子类信息。

`_get_vectorize_dtype` 是一个小工具：处理 `otypes` 列表里的字符串族 dtype（字节串 `S` / Unicode `U`），把它们归一成单个字符，避免把带长度的 dtype（如 `S5`）直接塞进 `otypes`。

#### 4.4.2 核心流程

```text
_calculate_shapes(broadcast_shape, dim_sizes, list_of_core_dims):
    对每一组 core_dims：shape = broadcast_shape + (dim_sizes[d] for d in core_dims)

_create_arrays(broadcast_shape, dim_sizes, list_of_core_dims, dtypes, results=None):
    shapes = _calculate_shapes(...)
    若 results 为 None：每个输出 np.empty(shape, dtype)
    否则（已有真实返回值）：每个输出 np.empty_like(result, shape=shape, dtype)  # 保留子类
```

#### 4.4.3 源码精读

`_calculate_shapes` 只有一行，把循环形状和核心尺寸拼起来：[\_function_base_impl.py:L2250-L2253](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2250-L2253)。注意 `dim_sizes[dim]` 在这一步必须已存在——所以**输出里如果出现全新的核心维度名**（如 `(n)->(m)` 里的 `m`），必须等到「第一次真实调用 `pyfunc`」之后才能确定尺寸，这解释了 4.5 里「输出数组在第一次循环才创建」的设计。

`_create_arrays` 负责真正分配内存：[\_function_base_impl.py:L2256-L2269](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2256-L2269)。它根据是否已有 `results`（真实返回值）分两条：没有就用 `np.empty`，有就用 `np.empty_like`。后者是子类友好的关键——测试里 `np.vectorize(np.matmul, signature='(m,m),(m)->(m)')` 对 ndarray 子类返回同子类，靠的就是这里。

`_get_vectorize_dtype`：[\_function_base_impl.py:L2272-L2275](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2272-L2275) 对 `dtype.char in "SU"` 返回字符本身，其余返回 dtype 对象。它在 `__init__` 规整 `otypes` 列表时被调用（[\_function_base_impl.py:L2471](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2471)），作用是把 `[np.dtype('S5')]` 这类条目归一成 `'S'`，使后续 `np.empty(shape, dtype='S')` 的元素长度由实际写入的数据决定，而不是被 `S5` 写死。

#### 4.4.4 代码实践

**实践目标**：用 `_calculate_shapes` 预测输出形状，再与真实 `vectorize` 结果对照。

**操作步骤**：

```python
import numpy as np
import numpy.lib._function_base_impl as nfb

# 签名 (n)->(n)，循环形状 (2,)，n=2 → 输出形状应为 (2,2)
bshape, dims = (2,), {'n': 2}
in_core, out_core = nfb._parse_gufunc_signature('(n)->(n)')
out_shapes = nfb._calculate_shapes(bshape, dims, out_core)
print('predicted out shapes =', out_shapes)   # [(2, 2)]

# 真实运行对照
f = np.vectorize(lambda a: a - a.mean(), signature='(n)->(n)')
print(f([[1, 3], [2, 4]]).shape)              # (2, 2)
```

**需要观察的现象**：预测的输出形状 `(2,2)` 与真实 `vectorize` 结果的 `.shape` 一致。

**预期结果**：两者都是 `(2, 2)`。

#### 4.4.5 小练习与答案

**练习 1**：签名 `(n)->(m)` 里，输出维度 `m` 的尺寸什么时候才能确定？

**答案**：必须在「第一次真实调用 `pyfunc`」之后。因为 `m` 是只出现在输出侧的新名字，输入数组无法提供它的尺寸，只能由 `pyfunc` 实际返回的数组形状决定（见 4.5 的 `_vectorize_call_with_signature`：第一次循环用 `_update_dim_sizes` 登记 `m`，再 `_create_arrays`）。测试 `test_signature_computed_size` 即用 `lambda x: x[:-1]` 验证此行为。

**练习 2**：为什么 `_create_arrays` 在有 `results` 时改用 `np.empty_like(result, ...)` 而非 `np.empty`？

**答案**：为了保留 `pyfunc` 返回值的数组子类。`np.empty_like` 会沿用 `result` 的类型（如 ndarray 子类），使得 `vectorize` 对子类输入返回同子类——这正是 matmul 子类测试通过的原因。

---

### 4.5 两条执行路径：标量路径（frompyfunc）与签名路径（ndindex 循环）

#### 4.5.1 概念说明

`vectorize` 真正执行时走哪条路，取决于**有没有 `signature`**。分发逻辑在 `_vectorize_call`：[\_function_base_impl.py:L2600-L2617](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2600-L2617)。

**路径 A：无签名（标量函数）**。`pyfunc` 被假定接收标量、返回标量。`vectorize` 用 `np.frompyfunc(func, nin, nout)` 把它包成一个**真正的 C ufunc**——但这个 ufunc 的 dtype 是 `object`，每个元素仍回调 Python。这就是 docstring 里「essentially a for loop」的另一面：虽借了 ufunc 的广播机制，但每个元素都慢。

**路径 B：有签名（gufunc 函数）**。`pyfunc` 接收/返回小数组。此时不再用 `frompyfunc`，而是 `_vectorize_call_with_signature` 用一个**显式的 `for index in np.ndindex(*broadcast_shape)` 循环**，逐个广播下标调用 `pyfunc`，把结果写回预分配的输出数组。这条路径彻底暴露了「for 循环」本质。

#### 4.5.2 核心流程

路径 A（`_get_ufunc_and_otypes` + `frompyfunc`）：

```text
若 otypes 已指定：
    nin = len(args); nout = len(otypes)
    ufunc = frompyfunc(func, nin, nout)        # 按 nin 缓存到 self._ufunc
否则：
    取每个参数 flat[0] 调一次 func → 推断 nout 与 otypes（dtype.char 拼串）
    ufunc = frompyfunc(_func, len(args), nout)
然后：
    args = [asanyarray(a, dtype=object) for a in args]
    outputs = ufunc(*args, out=...)
    转成 otypes dtype 返回
```

路径 B（`_vectorize_call_with_signature`）：

```text
校验参数个数 == len(input_core_dims)
args = [asanyarray(arg) for arg in args]
(broadcast_shape, dim_sizes) = _parse_input_dimensions(args, input_core_dims)
input_shapes = _calculate_shapes(broadcast_shape, dim_sizes, input_core_dims)
args = [broadcast_to(arg, shape, subok=True) ...]      # 把每个输入广播到完整形状

for index in np.ndindex(*broadcast_shape):              # ★ 显式循环
    results = func(*(arg[index] for arg in args))       # 每个 arg[index] 是「核心维度数组」
    校验结果个数 == nout
    首轮：用 _update_dim_sizes 登记「输出新维度」尺寸 → _create_arrays 分配输出
    output[index] = result                              # 就地写入

处理 size-0 输入（outputs 仍为 None）的兜底
返回 outputs[0] 或 outputs
```

#### 4.5.3 源码精读

`_get_ufunc_and_otypes` 是路径 A 的核心：[\_function_base_impl.py:L2531-L2598](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2531-L2598)。`otypes` 已指定时（L2537-L2554）按 `nin=len(args)` 缓存 `frompyfunc` 结果到 `self._ufunc` 字典——之所以用字典、以 `nin` 为键，是因为带默认参数的 `pyfunc` 不同调用入参数量可能不同。未指定 `otypes` 时（L2555-L2596）先用 `[arg.flat[0] for arg in args]` 取每组第一个元素调一次 `func`，把返回值的 `dtype.char` 拼成 `otypes` 串；若 `cache=True`，还用一个单元素列表 `_cache` 把这次「探测结果」缓存下来，避免后面 `frompyfunc` 再调一次第一个元素。注意 L2562 对 size-0 输入的拦截——没有第一个元素可探测，故强制要求 `otypes`。

随后在 `_vectorize_call` 里执行：[\_function_base_impl.py:L2607-L2617](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2607-L2617) 把每个参数转成 `dtype=object` 数组（L2609 注释 `gh-29196: dtype=object should eventually be removed` 指出这是历史包袱），调用 `ufunc(*args, out=...)`，再把 object 结果按 `otypes` 转回正确 dtype。

路径 B 的 `_vectorize_call_with_signature`：[\_function_base_impl.py:L2619-L2679](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L2619-L2679)。注意几个关键点：

- L2634 用 `np.broadcast_to(arg, shape, subok=True)` 把每个输入广播到「循环形状 + 自身核心维度」的完整形状，`subok=True` 保留子类。
- L2641 `for index in np.ndindex(*broadcast_shape)` 就是那个「for 循环」——遍历每一个循环下标。
- L2642 `func(*(arg[index] for arg in args))`：此时 `arg[index]` 取出的正是「核心维度数组」，交给 `pyfunc`。
- L2655-L2660 **输出数组延迟创建**：首轮先 `_update_dim_sizes` 登记 `pyfunc` 返回值里的输出核心维度（处理 `(n)->(m)` 这种输出新维度），再 `_create_arrays` 分配；这呼应了 4.4 练习 1。
- L2665-L2677 处理 size-0 输入的兜底：循环一次都没跑时，若 `otypes` 缺失或输出含未知新维度，就报错。

整段流程与 docstring 的「essentially a for loop」完全吻合——签名路径下，`vectorize` 就是手写了一个广播循环。

#### 4.5.4 代码实践

**实践目标**：用一个接受 1D 向量、返回标量的函数 `row_norm`，带 `(n)->()` 签名把 `np.vectorize` 包装成「按行处理」，并用测试 `test_signature_mean_last` 作为正确性依据。

**操作步骤**：

```python
import numpy as np

# 一个接受 1D 向量、返回其标准化 L2 范数的函数（标量输出）
def row_norm(vec):
    return np.sqrt((vec * vec).sum())

# 用 (n)->() 签名包装：n 是核心维度（向量长度），输出 () 是标量
vnorm = np.vectorize(row_norm, signature='(n)->()')

data = np.array([[3.0, 4.0],        # L2 范数 = 5
                 [5.0, 12.0],       # L2 范数 = 13
                 [8.0, 15.0]])      # L2 范数 = 17
print(vnorm(data))                  # 期望 [ 5. 13. 17.]

# 对照：用 test_signature_mean_last 的同款例子验证签名语义
vmean = np.vectorize(lambda a: a.mean(), signature='(n)->()')
print(vmean([[1, 3], [2, 4]]))      # 期望 [2. 3.]，与测试断言一致
```

**需要观察的现象**：

1. `vnorm(data)` 对 `data` 的**每一行**（长度为 2 的向量）调用一次 `row_norm`，返回长度为 3 的标量数组。
2. `vmean([[1,3],[2,4]])` 返回 `[2., 3.]`，与 `tests/test_function_base.py` 的 `test_signature_mean_last` 断言（[tests/test_function_base.py:L1898-L1904](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_function_base.py#L1898-L1904)）完全一致。

**预期结果**：`vnorm(data) == [5. 13. 17.]`，`vmean(...) == [2. 3.]`。

**进阶观察（可选）**：在 `row_norm` 里加一行 `print('called with', vec)`，你会看到它被**逐行**调用 3 次，亲眼确认「for 循环」本质。这一现象无法仅凭静态阅读完全确定输出顺序，可标注「待本地验证」后实测。

#### 4.5.5 小练习与答案

**练习 1**：同样是逐元素相加，`np.vectorize(operator.add, signature='(),()->()')` 与 `np.vectorize(operator.add)`（无签名）在内部走的是哪条路径？有何共同点？

**答案**：前者走路径 B（`_vectorize_call_with_signature` 的 `ndindex` 循环），后者走路径 A（`_get_ufunc_and_otypes` + `frompyfunc`）。共同点是：两者都**每个元素回调一次 Python**，都不快。`test_signature_simple` 与无签名默认行为分别覆盖二者。

**练习 2**：为什么签名路径下，`(n)->(m)` 这种「输出出现新维度名」的函数在 size-0 输入上会报错？

**答案**：`m` 的尺寸只能由 `pyfunc` 的真实返回值确定（4.4 练习 1）。size-0 输入导致 `for index in np.ndindex(...)` 一次都不执行，`_update_dim_sizes` 永远没有机会登记 `m`，于是 `outputs` 保持 `None`，最终在 L2670-L2675 因 `dim not in dim_sizes` 抛错（`cannot call vectorize with a signature including new output dimensions on size 0 inputs`）。

## 5. 综合实践

把本讲的知识串起来：实现一个「按行计算皮尔逊相关系数」的向量化函数，并解释它经过的每一步。

**任务**：给定两组观测 `x` 和 `y`（都是形状 `(K, N)` 的数组，K 次观测、每次 N 个样本），用一个带签名 `(n),(n)->()` 的 `vectorize` 包装函数，对每一对对应的行算相关系数。

**步骤**：

```python
import numpy as np

def pearson(x_row, y_row):
    xm = x_row - x_row.mean()
    ym = y_row - y_row.mean()
    denom = np.sqrt((xm * xm).sum() * (ym * ym).sum())
    return float((xm * ym).sum() / denom) if denom else 0.0

vp = np.vectorize(pearson, signature='(n),(n)->()')

x = np.array([[0., 1., 2., 3.],
              [0., 1., 2., 3.]])
y = np.array([[1., 2., 3., 4.],   # 与 x 完全正相关 → 1.0
              [4., 3., 2., 1.]])  # 与 x 完全负相关 → -1.0
print(vp(x, y))                   # 期望 [ 1. -1.]
```

**对照源码解释执行过程**（用本讲术语填空）：

1. `__init__` 用 `_parse_gufunc_signature('(n),(n)->()')` 解析出 `input_core_dims=[('n',),('n',)]`、`output_core_dims=[()]`。
2. 调用时 `_vectorize_call` 因 `signature is not None` 走 **路径 B**。
3. `_parse_input_dimensions` 校验两输入最后一维都叫 `n` 且尺寸一致（都是 4），并算出循环形状 `(2,)`（要调用 2 次）。
4. `_calculate_shapes` 算出每个输入完整形状 `(2,4)`、输出形状 `(2,)`。
5. `for index in np.ndindex(2)` 循环 2 次，每次 `arg[index]` 取出一对长度为 4 的向量交给 `pearson`，结果写入预分配输出数组的对应位置。
6. 返回 `[1., -1.]`。

**思考题**：如果把签名写成 `(n),(m)->()`（两个维度不同名），上面的调用会发生什么？为什么？（答案：仍能运行，因为 `_update_dim_sizes` 不再强制两输入等长——但语义上 `pearson` 仍要求两行等长，长度不等时会在 `pearson` 内部的广播处报错。可见签名只约束「同名必相等」，不约束「运算合法」。）

## 6. 本讲小结

- `np.vectorize` **不是编译型 ufunc**，本质是 for 循环，定位是「方便」而非「性能」。它由顶层 `numpy/__init__.py` 直接再导出，`__module__` 被 `@set_module('numpy')` 钉为 `numpy`。
- gufunc 签名是一个微型 DSL：括号内的字母是**核心维度名**（同名必等长），括号外的维度是**循环/广播维度**。`_parse_gufunc_signature` 用五条嵌套正则把签名字符串拆成 `input_core_dims / output_core_dims`。
- `_update_dim_sizes` 强制同名核心维度尺寸一致；`_parse_input_dimensions` 借 `as_strided(0, shape)` 造「零数据占位数组」参与广播形状推导，把输入拆成 `(broadcast_shape, dim_sizes)`。
- `_calculate_shapes` 把「循环形状 + 核心尺寸」拼成完整形状；`_create_arrays` 用 `np.empty_like` 保留子类；`_get_vectorize_dtype` 把字符串族 dtype 归一成字符。
- 执行有**两条路径**：无签名走 `frompyfunc`（借 ufunc 机制但每元素回调 Python），有签名走 `_vectorize_call_with_signature` 的 `np.ndindex` 显式循环，输出数组在第一次真实调用后才创建（以支持 `(n)->(m)` 这类输出新维度）。

## 7. 下一步学习建议

- **对比真正的 gufunc**：阅读 `np.matmul`、`np.linalg.solve` 等 gufunc 的行为，体会「编译型 gufunc」与 `vectorize` 的性能差距。可参考 numpy 文档 *Generalized Universal Function API*。
- **回到 `_function_base_impl.py` 的其它函数**：本文件还包含 `diff`/`gradient`/`trapezoid`（u6-l1）、`cov`/`corrcoef`/`median`/`percentile`（u7-l1、u7-l2）等，它们与 `vectorize` 共享同一文件，阅读时可对照它们的 dispatcher+impl 写法。
- **深入广播内核**：本讲的 `_parse_input_dimensions` 复用了 u5-l2 的 `_broadcast_shape`。若想彻底理解占位数组技巧，建议回看 u5-l1 的 `as_strided` 与 `__array_interface__`。
- **子类与 NEP-18**：本讲多次出现 `subok=True` 与子类保留。后续若学习 `mixins.py`（u2-l3）的 `__array_ufunc__` 协议，可更完整地理解 `vectorize` 为何要小心处理子类。

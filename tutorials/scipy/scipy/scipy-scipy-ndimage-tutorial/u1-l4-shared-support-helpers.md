# 共享支撑工具：边界模式、输出、axes、文档

## 1. 本讲目标

本讲是入门单元（u1）的最后一讲。前面三讲（u1-l1 ~ u1-l3）你已经知道 `scipy.ndimage` 能做什么、源码在硬盘上如何分层、公开 API 如何经四层架构装配出来。本讲把视角下沉一层，去看那些**几乎被所有 ndimage 函数共享、却很容易被忽略的「支撑工具」**。

学完后你应当能够：

- 理解 `_extend_mode_to_code` 如何把人类可读的边界模式字符串（如 `'reflect'`、`'grid-wrap'`）翻译成 C 内核需要的整数码，以及 `is_filter` 分支为什么存在。
- 理解 `_get_output` 如何把 `output=None`、`output=np.float32`、`output=已有数组` 三种截然不同的输入统一成一个可以写入结果的真实数组。
- 理解 `_normalize_sequence` 与 `_check_axes` 如何把「标量广播成各维序列」「把 `axes` 规范成合法、唯一、非负的轴元组」这两件重复劳动集中处理。
- 理解 `docfiller` 如何用占位符机制让数十个函数共享同一份参数文档，避免文档维护噩梦。

掌握这四个工具之后，再读后续任何一篇功能域讲义（滤波、插值、测量、形态学）时，你会发现自己能跳过每个函数开头那段「千篇一律的参数预处理」，直接关注该函数真正独特的算法部分。

## 2. 前置知识

本讲假设你已经读过 u1-l3，知道 ndimage 函数的调用链大致是：

```
公开函数（如 gaussian_filter）
  → _ni_support._get_output(...)      # 准备输出数组
  → _ni_support._normalize_sequence() # 把标量参数广播成各维序列
  → _ni_support._extend_mode_to_code()# 把 mode 字符串编码成整数码
  → _nd_image.<C 内核>(...)           # 真正的计算
```

你需要了解的几个基础概念：

- **边界扩展（boundary extension）**：当滤波器或插值核需要读取数组边界之外的像素时，必须用某种规则「虚拟地」把数组往外延伸。比如 `'nearest'` 就是把边缘像素复制出去，`'reflect'` 就是镜像翻转。
- **dtype（数据类型）**：NumPy 数组的元素类型，如 `np.float32`、`np.int64`、`np.complex128`。
- **轴（axis）**：多维数组的第几个维度。一个形状为 `(10, 20, 30)` 的数组有 3 个轴，编号 0、1、2；也允许用负数 `-1` 表示最后一个轴。
- **装饰器（decorator）**：Python 中 `@something` 写在 `def` 之上的语法糖，等价于在函数定义后执行 `func = something(func)`。
- **`%(...)s` 占位符**：Python 字符串格式化的老式写法，`'%(mode)s' % {'mode': 'hello'}` 会得到 `'hello'`。`docfiller` 正是基于它工作。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们是整个 ndimage 子包的「地基」：

| 文件 | 作用 | 本讲关注的关键符号 |
| --- | --- | --- |
| [_ni_support.py](_ni_support.py) | 所有功能域函数共享的低层支撑：边界模式编码、输出数组获取、序列规范化、轴校验 | `_extend_mode_to_code`、`_get_output`、`_normalize_sequence`、`_check_axes`、`_skip_if_dtype` |
| [_ni_docstrings.py](_ni_docstrings.py) | 集中存放公共参数文档片段，并提供 `docfiller` 装饰器 | `docdict`、`docfiller` |

值得一提的是：`_ni_support.py` 里还有一个 [_skip_if_dtype](_ni_support.py#L128-L140)，它处理「参数既可能是数组、也可能是 dtype」的多态判定，主要被后端委托层（u7-l1）使用，本讲只在源码地图中点名，不做深入。

## 4. 核心概念与源码讲解

### 4.1 边界模式编码 `_extend_mode_to_code`

#### 4.1.1 概念说明

任何一个邻域滤波器（如 `correlate1d`）或插值函数（如 `map_coordinates`）在处理数组边缘的像素时，滤波核都会「探出」数组边界，需要读取不存在的像素。怎么处理这些不存在的像素？这就是 **边界模式（mode）** 要回答的问题。

ndimage 提供了一套统一的模式名，例如：

- `'reflect'`：关于边缘像素的**边**做镜像（半样本对称），会重复边缘像素。
- `'mirror'`：关于边缘像素的**中心**做镜像（全样本对称），不重复边缘像素。
- `'nearest'`：复制最近的边缘像素。
- `'wrap'`：把数组当成周期，绕到对面去取。
- `'constant'`：用一个常量 `cval` 填充边界外区域。

为了和插值函数保持命名一致，又增加了 `'grid-mirror'`、`'grid-wrap'`、`'grid-constant'` 三个 `grid-` 前缀的别名。

人类喜欢字符串（语义清晰），但 C 内核只想要一个整数（用来在 `switch` 里分发）。`_extend_mode_to_code` 就是这二者之间的翻译官。

#### 4.1.2 核心流程

```
_extend_mode_to_code(mode, is_filter=False):
    1. 用 if/elif 链逐个匹配 mode 字符串
    2. 对 grid-wrap / grid-constant：先看 is_filter 标志
       - is_filter=True  → 归并到与 wrap / constant 相同的码（C 滤波内核可复用）
       - is_filter=False → 使用独立码（插值场景边界语义不同，必须区分）
    3. 匹配失败 → raise RuntimeError('boundary mode not supported')
```

完整的映射关系如下（共 8 个模式名 → 7 个整数码 0–6，因为 `'grid-mirror'` 与 `'reflect'` 同码）：

| mode 字符串 | `is_filter=False`（默认/插值） | `is_filter=True`（秩滤波） |
| --- | --- | --- |
| `'nearest'` | 0 | 0 |
| `'wrap'` | 1 | 1 |
| `'reflect'` / `'grid-mirror'` | 2 | 2 |
| `'mirror'` | 3 | 3 |
| `'constant'` | 4 | 4 |
| `'grid-wrap'` | **5** | **1**（归并到 wrap） |
| `'grid-constant'` | **6** | **4**（归并到 constant） |

注意表格中加粗的两行：这正是 `is_filter` 参数发挥作用的唯一地方。

#### 4.1.3 源码精读

完整实现非常短，请直接看 [_ni_support.py:37-59](_ni_support.py#L37-L59)：

```python
def _extend_mode_to_code(mode, is_filter=False):
    """Convert an extension mode to the corresponding integer code."""
    if mode == 'nearest':
        return 0
    elif mode == 'wrap':
        return 1
    elif mode in ['reflect', 'grid-mirror']:
        return 2
    elif mode == 'mirror':
        return 3
    elif mode == 'constant':
        return 4
    elif mode == 'grid-wrap' and is_filter:
        return 1
    elif mode == 'grid-wrap':
        return 5
    elif mode == 'grid-constant' and is_filter:
        return 4
    elif mode == 'grid-constant':
        return 6
    else:
        raise RuntimeError('boundary mode not supported')
```

几个要点：

- [_ni_support.py:44-45](_ni_support.py#L44-L45)：`'reflect'` 和 `'grid-mirror'` 走同一个分支返回 `2`，所以 `grid-mirror` 就是 `reflect` 的纯别名。
- [_ni_support.py:50-57](_ni_support.py#L50-L57)：`is_filter` 只在 `'grid-wrap'` 和 `'grid-constant'` 上有区分。其原因是：在纯滤波（滑窗求和/求秩）场景下，`grid-wrap` 与 `wrap`、`grid-constant` 与 `constant` 的边界行为**完全等价**，所以归并到同一整数码，让 C 内核只实现一套；而在插值场景下，`grid-` 版本涉及亚像素采样，边界语义不同，必须用独立码 5、6 区分。
- [_ni_support.py:58-59](_ni_support.py#L58-L59)：未知模式抛 `RuntimeError`（注意不是 `ValueError`，这是该函数的历史风格）。

**谁调用它？** 大多数滤波函数用默认的 `is_filter=False`，例如 [correlate1d 在 _filters.py:609](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L609) 直接写 `mode = _ni_support._extend_mode_to_code(mode)`。而 `is_filter=True` 在整个子包里几乎只出现在秩滤波路径上，见 [_filters.py:1994](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1994) 的 `mode = _ni_support._extend_mode_to_code(mode, is_filter=True)`。

#### 4.1.4 代码实践

**实践目标**：亲眼看到每个模式字符串对应的整数码，并体会 `is_filter` 的差异。

**操作步骤**：

```python
# 示例代码：直接调用私有支撑函数
from scipy.ndimage import _ni_support

modes = ['nearest', 'wrap', 'reflect', 'grid-mirror', 'mirror',
         'constant', 'grid-wrap', 'grid-constant']

print("mode            | is_filter=False | is_filter=True")
print("-" * 52)
for m in modes:
    code_default = _ni_support._extend_mode_to_code(m)
    code_filter = _ni_support._extend_mode_to_code(m, is_filter=True)
    print(f"{m:15s} |       {code_default}        |       {code_filter}")

# 再试一个非法模式
try:
    _ni_support._extend_mode_to_code('foobar')
except RuntimeError as e:
    print("非法模式抛出:", e)
```

**需要观察的现象**：

- `'reflect'` 与 `'grid-mirror'` 两行的码完全相同（都是 2）。
- `'grid-wrap'` 在两列分别是 5 和 1；`'grid-constant'` 在两列分别是 6 和 4——这两行的差异就是 `is_filter` 的全部作用。
- 非法模式 `'foobar'` 抛出 `RuntimeError`。

**预期结果**：输出一张和上面 4.1.2 节表格完全一致的对照表。如果运行报 `ModuleNotFoundError` 或无法导入 `_ni_support`，请确认使用的是源码安装或开发模式的 SciPy。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `'grid-wrap'` 在 `is_filter=True` 时返回 `1` 而不是 `5`？

**参考答案**：在纯滤波（求和、求秩等滑窗操作）场景下，`grid-wrap` 与 `wrap` 的边界行为等价，归并到同一码 `1` 可让 C 滤波内核只实现一套逻辑；只有在插值场景（`is_filter=False`）下，二者亚像素语义不同，才需要独立的码 `5`。

**练习 2**：`'reflect'` 和 `'mirror'` 都是「镜像」，它们的本质区别是什么？

**参考答案**：`'reflect'` 是半样本对称，关于边缘像素的**边**反射，会重复边缘像素（`d c b a | a b c d | d c b a`）；`'mirror'` 是全样本对称，关于边缘像素的**中心**反射，不重复边缘像素（`d c b | a b c d | c b a`）。它们对应不同的整数码（2 与 3），C 内核会分别处理。

---

### 4.2 输出数组获取 `_get_output`

#### 4.2.1 概念说明

几乎每个 ndimage 函数都有这样一个参数：

```python
def some_filter(input, ..., output=None, ...):
```

`output` 是个**多态**参数，它可以是三种完全不同的东西：

1. `None`——「我不知道要什么类型，你看着办」，函数新建一个和输入同 dtype 的数组。
2. 一个 dtype——「我想要这个类型的结果」，函数按指定 dtype 新建数组。
3. 一个已经存在的数组——「把结果直接写进这个数组」，函数就地写入，省一次内存分配。

把这三类输入统一成一个「可以安全写入结果的 NumPy 数组」，就是 `_get_output` 的职责。它还要额外处理「复数输出」的特殊情况（如傅里叶滤波、复数卷积）。

#### 4.2.2 核心流程

```
_get_output(output, input, shape=None, complex_output=False):
    若 shape 为 None：shape = input.shape
    分支判断 output 的类型：
      ① None                  → 按 complex_output 决定 dtype，np.zeros(shape)
      ② type 或 np.dtype 实例 → 若需要复数而给的不是复数：警告并提升
                                 np.zeros(shape, dtype=output)
      ③ 字符串（如 'f4'）      → np.dtype(output) 后同 ②
      ④ 其它（当作已存在数组） → np.asarray(output)
                                 校验 shape 匹配、复数 dtype 匹配
    返回可直接写入的数组
```

复数处理的两条规则值得记住：

- 给 dtype 但不是复数，而函数需要复数输出 → **警告并自动提升**（`warnings.warn` + `np.promote_types`）。
- 给已存在数组但 dtype 不匹配复数要求 → **直接报错**（`RuntimeError`），不自动提升。

#### 4.2.3 源码精读

完整实现见 [_ni_support.py:78-107](_ni_support.py#L78-L107)：

```python
def _get_output(output, input, shape=None, complex_output=False):
    if shape is None:
        shape = input.shape
    if output is None:
        if not complex_output:
            output = np.zeros(shape, dtype=input.dtype.name)
        else:
            complex_type = np.promote_types(input.dtype, np.complex64)
            output = np.zeros(shape, dtype=complex_type)
    elif isinstance(output, type | np.dtype):
        # Classes (like `np.float32`) and dtypes are interpreted as dtype
        if complex_output and np.dtype(output).kind != 'c':
            warnings.warn("promoting specified output dtype to complex", stacklevel=3)
            output = np.promote_types(output, np.complex64)
        output = np.zeros(shape, dtype=output)
    elif isinstance(output, str):
        output = np.dtype(output)
        if complex_output and output.kind != 'c':
            raise RuntimeError("output must have complex dtype")
        elif not issubclass(output.type, np.number):
            raise RuntimeError("output must have numeric dtype")
        output = np.zeros(shape, dtype=output)
    else:
        # output was supplied as an array
        output = np.asarray(output)
        if output.shape != shape:
            raise RuntimeError("output shape not correct")
        elif complex_output and output.dtype.kind != 'c':
            raise RuntimeError("output must have complex dtype")
    return output
```

逐段说明：

- [_ni_support.py:81-86](_ni_support.py#L81-L86)（① None 分支）：默认用 `input.dtype.name` 新建同类型数组；若 `complex_output=True`，则用 `np.promote_types(input.dtype, np.complex64)` 推断一个能容纳输入的复数类型。
- [_ni_support.py:87-92](_ni_support.py#L87-L92)（② dtype 类分支）：注意 `isinstance(output, type | np.dtype)` 这个新式写法——`type` 匹配 `np.float32` 这种类，`np.dtype` 匹配 `np.dtype('float32')` 这种实例。复数不匹配时只**警告**并提升。
- [_ni_support.py:93-99](_ni_support.py#L93-L99)（③ 字符串分支）：先把字符串转成 `np.dtype`，再做复数与数值类型校验。注意这里复数不匹配是**报错**，与 ② 分支的「警告并提升」策略不同。
- [_ni_support.py:100-107](_ni_support.py#L100-L107)（④ 数组分支）：`np.asarray` 包一层（避免传入列表），校验 shape 必须与目标 `shape` 完全一致，否则 `RuntimeError("output shape not correct")`。

**真实调用示例**——这是 ndimage 里出现频率最高的函数之一。在 [correlate1d（_filters.py:598）](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L598) 中：

```python
output = _ni_support._get_output(output, input)
```

而在处理复数输入时（[_filters.py:594](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L594)）则带上 `complex_output=True`：

```python
output = _ni_support._get_output(output, input, complex_output=True)
```

#### 4.2.4 代码实践

**实践目标**：分别用三种 `output` 形式调用 `_get_output`，观察返回数组的 dtype、shape 与是否复用传入的数组对象。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.ndimage import _ni_support

input_arr = np.arange(12, dtype=np.int32).reshape(3, 4)
print("input dtype/shape:", input_arr.dtype, input_arr.shape)

# 情况 ①：output=None
out1 = _ni_support._get_output(None, input_arr)
print("None        ->", out1.dtype, out1.shape, "全零?", np.all(out1 == 0))

# 情况 ②：output=一个 dtype 类
out2 = _ni_support._get_output(np.float32, input_arr)
print("np.float32  ->", out2.dtype, out2.shape)

# 情况 ②'：output=字符串 dtype
out2b = _ni_support._get_output('float64', input_arr)
print("'float64'   ->", out2b.dtype, out2b.shape)

# 情况 ③：output=已存在数组（就地写入）
pre = np.zeros((3, 4), dtype=np.int32)
out3 = _ni_support._get_output(pre, input_arr)
print("已有数组     ->", out3.dtype, out3.shape, "就是 pre 吗?", out3 is pre)

# 情况 ④：shape 不匹配，应当报错
try:
    _ni_support._get_output(np.zeros((5, 5)), input_arr)
except RuntimeError as e:
    print("shape 不匹配抛出:", e)
```

**需要观察的现象**：

- 情况 ① 返回的数组 dtype 与 `input_arr` 一致（`int32`），且是全零的新数组。
- 情况 ② / ②' 返回的 dtype 分别是 `float32` / `float64`。
- 情况 ③ 中 `out3 is pre` 应为 `True`——说明传入的数组被**原样复用**，没有拷贝。
- 情况 ④ 抛出 `RuntimeError("output shape not correct")`。

**预期结果**：上述四点全部成立。若你想验证 `complex_output` 分支，可对 `np.complex128` 这类 dtype 再做一次实验（详见 4.2.5 练习）。

#### 4.2.5 小练习与答案

**练习 1**：当 `output=None` 且 `complex_output=True`、输入是 `int32` 数组时，返回数组的 dtype 是什么？

**参考答案**：会调用 `np.promote_types(np.int32, np.complex64)`，结果是 `complex64`（能容纳 `int32` 的最小复数类型）。

**练习 2**：为什么传入 dtype 类时复数不匹配只是「警告并提升」，而传入已存在数组时却是「直接报错」？

**参考答案**：dtype 类只是「意图描述」，提升它没有副作用，所以警告后自动提升即可；而已存在数组是一块具体的内存，若 dtype 不匹配就无法把复数结果正确写入这块内存，强行转换还可能丢失数据，因此直接报错更安全。

---

### 4.3 序列规范化 `_normalize_sequence` 与轴校验 `_check_axes`

#### 4.3.1 概念说明

ndimage 的很多参数都支持「**标量广播**」：你可以给一个标量（对所有轴生效），也可以给一个序列（对每个轴分别指定）。例如 `gaussian_filter` 的 `sigma`：

- `sigma=1.0` → 每个轴都用 1.0。
- `sigma=(1.0, 2.0, 3.0)` → 三个轴分别用 1.0、2.0、3.0。

同理 `mode`、`origin`、`order`、`cval` 等都遵循这一约定。**问题**：C 内核和后续逻辑需要的是一个「长度恰好等于维度数」的列表。`_normalize_sequence` 就负责把标量广播成等长序列、把序列校验成等长。

类似地，`axes` 参数（指定「只在哪几个轴上操作」）也需要规范化：它可以是 `None`（所有轴）、一个整数、或一个整数序列；还要处理负轴（`-1` 表示最后一个）、范围校验和唯一性校验。这就是 `_check_axes` 的工作。

把这两件重复劳动集中到 `_ni_support.py`，几十个函数就不用各自重写一遍了。

#### 4.3.2 核心流程

```
_normalize_sequence(input, rank):
    若 input 是字符串               → 当作标量（避免把 'abc' 当成可迭代）
    否则若 input 可迭代（np.iterable）→ 转成 list，校验 len == rank，否则报错
    否则（标量）                    → 复制成 [input] * rank

_check_axes(axes, ndim):
    若 axes is None                 → 返回 tuple(range(ndim))
    若 axes 是标量                   → operator.index 转成单元素元组
    若 axes 是可迭代                 → 逐个 operator.index 转 int，
                                       校验 -ndim <= ax <= ndim-1，
                                       负轴用 ax % ndim 归一化
    否则                            → 报 ValueError
    最后校验轴无重复                 → 否则 ValueError("axes must be unique")
```

注意 `_normalize_sequence` 对字符串的特殊处理：因为字符串本身是可迭代的（会迭代出字符），所以必须先排除，否则 `mode='reflect'` 会被错误地当成 `['r','e','f','l','e','c','t']`。

#### 4.3.3 源码精读

先看 [_ni_support.py:62-75](_ni_support.py#L62-L75) 的 `_normalize_sequence`：

```python
def _normalize_sequence(input, rank):
    """If input is a scalar, create a sequence of length equal to the
    rank by duplicating the input. If input is a sequence,
    check if its length is equal to the length of array.
    """
    is_str = isinstance(input, str)
    if not is_str and np.iterable(input):
        normalized = list(input)
        if len(normalized) != rank:
            err = "sequence argument must have length equal to input rank"
            raise RuntimeError(err)
    else:
        normalized = [input] * rank
    return normalized
```

要点：

- [_ni_support.py:67-68](_ni_support.py#L67-L68)：`is_str` 先排除字符串，再用 `np.iterable` 判断是否可迭代。这一步是关键防御。
- [_ni_support.py:70-72](_ni_support.py#L70-L72)：序列长度必须严格等于 `rank`（维度数），否则 `RuntimeError`。

再看 [_ni_support.py:110-126](_ni_support.py#L110-L126) 的 `_check_axes`：

```python
def _check_axes(axes, ndim):
    if axes is None:
        return tuple(range(ndim))
    elif np.isscalar(axes):
        axes = (operator.index(axes),)
    elif isinstance(axes, Iterable):
        for ax in axes:
            axes = tuple(operator.index(ax) for ax in axes)
            if ax < -ndim or ax > ndim - 1:
                raise ValueError(f"specified axis: {ax} is out of range")
        axes = tuple(ax % ndim if ax < 0 else ax for ax in axes)
    else:
        message = "axes must be an integer, iterable of integers, or None"
        raise ValueError(message)
    if len(tuple(set(axes))) != len(axes):
        raise ValueError("axes must be unique")
    return axes
```

要点：

- [_ni_support.py:111-112](_ni_support.py#L111-L112)：`None` 表示「所有轴」，直接返回 `tuple(range(ndim))`。
- [_ni_support.py:113-114](_ni_support.py#L113-L114)：标量用 `operator.index` 转成整数（拒绝 `3.0` 这种浮点）。
- [_ni_support.py:120](_ni_support.py#L120)：负轴通过 `ax % ndim` 归一化为非负（如 `ndim=3` 时 `-1 → 2`）。
- [_ni_support.py:124-125](_ni_support.py#L124-L125)：用 `set` 去重后比较长度来检测重复轴，例如 `axes=(0, 0)` 会被拒绝。

**真实调用示例**——`gaussian_filter` 同时用到了本节的两个工具，见 [_filters.py:846-851](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L846-L851)：

```python
axes = _ni_support._check_axes(axes, input.ndim)
...
orders = _ni_support._normalize_sequence(order, num_axes)
sigmas = _ni_support._normalize_sequence(sigma, num_axes)
modes  = _ni_support._normalize_sequence(mode, num_axes)
radiuses = _ni_support._normalize_sequence(radius, num_axes)
```

这是典型的「先 `_check_axes` 确定要操作的轴数 `num_axes`，再用 `num_axes` 把各个标量/序列参数规范化」的模式。

#### 4.3.4 代码实践

**实践目标**：直接调用这两个函数，理解标量广播、负轴归一化、长度校验与唯一性校验。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.ndimage import _ni_support

# —— _normalize_sequence ——
print("标量 1.0 广播到 rank=3:", _ni_support._normalize_sequence(1.0, 3))
print("序列广播到 rank=3     :", _ni_support._normalize_sequence([1, 2, 3], 3))
print("字符串 'reflect' 当标量:", _ni_support._normalize_sequence('reflect', 2))

try:
    _ni_support._normalize_sequence([1, 2], 3)   # 长度不匹配
except RuntimeError as e:
    print("长度不匹配抛出:", e)

# —— _check_axes ——
print("None, ndim=3          :", _ni_support._check_axes(None, 3))
print("标量 1, ndim=3        :", _ni_support._check_axes(1, 3))
print("负轴 -1, ndim=3       :", _ni_support._check_axes(-1, 3))
print("序列 (0, 2), ndim=3   :", _ni_support._check_axes((0, 2), 3))

try:
    _ni_support._check_axes((0, 0), 3)           # 重复轴
except ValueError as e:
    print("重复轴抛出:", e)

try:
    _ni_support._check_axes(5, 3)                # 越界
except ValueError as e:
    print("越界轴抛出:", e)
```

**需要观察的现象**：

- 标量 `1.0` 被广播成 `[1.0, 1.0, 1.0]`。
- `'reflect'` 被当成标量（而不是拆成字符序列），广播成 `['reflect', 'reflect']`。
- `_check_axes(-1, 3)` 返回 `(2,)`，负轴被归一化。
- `_check_axes(None, 3)` 返回 `(0, 1, 2)`。
- 重复轴和越界轴都抛 `ValueError`（注意：和 `_normalize_sequence` 的 `RuntimeError` 不同）。

**预期结果**：以上各点全部成立。若运行时发现 `_check_axes((0, 2), 3)` 返回 `(0, 2)` 而 `_check_axes(-1, 3)` 返回 `(2,)`，说明你已正确理解负轴归一化。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_normalize_sequence` 要先判断 `isinstance(input, str)` 再判断 `np.iterable(input)`？

**参考答案**：因为字符串是可迭代的（`list('abc')` 会得到 `['a','b','c']`），若不先排除，`mode='reflect'` 会被错误地拆成字符序列。先排除字符串，才能让字符串参数被当成标量正确广播。

**练习 2**：`_check_axes((0, -1), 2)` 在 `ndim=2` 时返回什么？为什么 `(0, 0)` 会报错？

**参考答案**：`(0, -1)` 中 `-1` 经 `ax % 2` 归一化为 `1`，所以返回 `(0, 1)`。而 `(0, 0)` 归一化后仍是 `(0, 0)`，`set` 去重后长度为 1，不等于原长度 2，因此触发 `"axes must be unique"` 报错——同一个轴被指定两次在滤波语义上没有意义。

---

### 4.4 共享文档 `docfiller`

#### 4.4.1 概念说明

ndimage 有几十个公开函数，它们大量共享同一批参数：`input`、`output`、`mode`、`cval`、`origin`、`axis`、`size`/`footprint` 等。如果每个函数的 docstring 都把 `mode` 的 7 种边界行为完整抄一遍，就会有几十份重复文档——改一个错别字要改几十处，维护成本极高。

解决方案是**占位符 + 字典填充**：

1. 把每段公共文档（如 `mode` 的说明）写一次，存进字典。
2. 各函数的 docstring 里只写占位符 `%(mode_reflect)s`。
3. 用 `docfiller` 装饰器在函数定义时把占位符替换成真实文档。

这套机制由 SciPy 公共库 `scipy._lib.doccer` 提供，ndimage 只是把公共片段集中放在 `_ni_docstrings.py`。

#### 4.4.2 核心流程

```
1. 在 _ni_docstrings.py 顶部定义各文档片段常量：
     _input_doc, _output_doc, _mode_reflect_doc, _cval_doc, _origin_doc ...
2. 组装成字典：
     docdict = {'input': _input_doc, 'output': _output_doc, ...}
3. 创建装饰器：
     docfiller = doccer.filldoc(docdict)
4. 在每个函数上使用：
     @_ni_docstrings.docfiller
     def correlate1d(...):
         """...
         %(input)s
         %(mode_reflect)s
         %(cval)s
         ..."""
```

`doccer.filldoc` 返回的装饰器会在被装饰时对 `func.__doc__` 执行 `docstring % docdict`，把所有 `%(key)s` 替换成对应文本。替换完成后，函数的 `__doc__` 就是完整的、无占位符的文档。

#### 4.4.3 源码精读

文档片段字典的定义见 [_ni_docstrings.py:202-218](_ni_docstrings.py#L202-L218)：

```python
docdict = {
    'input': _input_doc,
    'axis': _axis_doc,
    'output': _output_doc,
    'size_foot': _size_foot_doc,
    'mode_interp_constant': _mode_interp_constant_doc,
    'mode_interp_mirror': _mode_interp_mirror_doc,
    'mode_reflect': _mode_reflect_doc,
    'mode_multiple': _mode_multiple_doc,
    'cval': _cval_doc,
    'origin': _origin_doc,
    'origin_multiple': _origin_multiple_doc,
    'extra_arguments': _extra_arguments_doc,
    'extra_keywords': _extra_keywords_doc,
    'prefilter': _prefilter_doc,
    'nan': _nan_doc,
    }
```

装饰器本身的创建只有一行，见 [_ni_docstrings.py:220](_ni_docstrings.py#L220)：

```python
docfiller: Final = doccer.filldoc(docdict)
```

字典里每个 key 对应一段精心维护的文档。例如 `mode` 在不同场景有不同变体——滤波函数默认 `'reflect'`，用 [`_mode_reflect_doc`（_ni_docstrings.py:37-73）](_ni_docstrings.py#L37-L73)；插值函数默认 `'constant'`，用 [`_mode_interp_constant_doc`（_ni_docstrings.py:75-122）](_ni_docstrings.py#L75-L122)。这两段文档对边界模式的描述详尽程度不同（插值版多了 `grid-constant` 与亚像素语义的说明），所以分开维护。

注意 [_ni_docstrings.py:123-128](_ni_docstrings.py#L123-L128) 还有一个巧妙用法：插值的 mirror 变体是直接在 constant 变体上做字符串替换得到的：

```python
_mode_interp_mirror_doc = (
    _mode_interp_constant_doc.replace("Default is 'constant'",
                                      "Default is 'mirror'")
)
assert _mode_interp_mirror_doc != _mode_interp_constant_doc, \
    'Default not replaced'
```

这样两段文档只有「默认值」一处不同，避免了大段重复。后面的 `assert` 是一道保险——如果将来有人改了 constant 版的措辞导致替换失效，`assert` 会立刻报警。

**真实调用示例**——`correlate1d` 是最干净的例子，见 [_filters.py:555-572](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L555-L572)：

```python
@_ni_docstrings.docfiller
def correlate1d(input, weights, axis=-1, output=None, mode="reflect",
                cval=0.0, origin=0):
    """Calculate a 1-D correlation along the given axis.
    ...
    Parameters
    ----------
    %(input)s
    weights : array
        1-D sequence of numbers.
    %(axis)s
    %(output)s
    %(mode_reflect)s
    %(cval)s
    %(origin)s
    ...
    """
```

源码里写的是 `%(mode_reflect)s` 这样的占位符；但当你 `print(scipy.ndimage.correlate1d.__doc__)` 时，看到的是被填充后的完整 `mode` 说明。这正是 `docfiller` 装饰器在工作。

#### 4.4.4 代码实践

**实践目标**：对比「源码里的占位符」与「运行时被填充后的完整 docstring」，直观感受 `docfiller` 的作用。

**操作步骤**：

```python
# 示例代码
import scipy.ndimage as ndi

doc = ndi.correlate1d.__doc__

# 1. 确认占位符已被替换：填充后的 docstring 里不应再出现 %(xxx)s
has_placeholder = '%(mode_reflect)s' in doc
print("docstring 里还残留占位符吗?", has_placeholder)

# 2. 确认 mode 的完整说明被填进来了
has_mode_detail = "reflect" in doc and "half-sample" in doc
print("mode 完整说明已填充?", has_mode_detail)

# 3. 打印 docstring 里 Parameters 段附近的内容，肉眼确认
start = doc.find("Parameters")
print("--- Parameters 段（节选）---")
print(doc[start:start + 400])
```

**需要观察的现象**：

- `has_placeholder` 应为 `False`——说明所有 `%(key)s` 都已被替换。
- `has_mode_detail` 应为 `True`——说明 `_mode_reflect_doc` 的内容（含 `half-sample symmetric` 等字样）已被填入。
- 节选打印出来的 `Parameters` 段是完整的人类可读文本，看不到任何 `%(...)` 痕迹。

**预期结果**：上述三点全部成立。如果想进一步对比，可以直接打开 [_filters.py:565-572](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L565-L572) 看源码里的占位符写法，再和上面打印的结果对照——同一个函数，源码里是占位符、运行时是完整文档，二者一一对应。

#### 4.4.5 小练习与答案

**练习 1**：如果想在某个新函数的 docstring 里复用 `output` 参数的说明，应该怎么写？需要自己重新描述吗？

**参考答案**：不需要重新描述。只要在 docstring 里写 `%(output)s`，再用 `@_ni_docstrings.docfiller` 装饰该函数，`doccer.filldoc` 就会自动把 `docdict['output']` 的内容填进去。这正是共享文档机制的价值。

**练习 2**：为什么 `_mode_interp_mirror_doc` 要用 `.replace()` 从 `_mode_interp_constant_doc` 派生，并在后面跟一个 `assert`？

**参考答案**：两段插值 mode 文档只有「默认值」一处不同（`'constant'` vs `'mirror'`），用 `.replace()` 派生可以避免重复维护几十行几乎相同的文字。`assert` 是防御性检查：一旦将来有人修改了 constant 版的措辞，导致 `"Default is 'constant'"` 这个子串不再出现、替换失效，`assert` 会立即让导入失败，强制开发者注意到这个不一致。

---

## 5. 综合实践

本讲四个工具并不是孤立存在的——它们在每个 ndimage 函数里**协同工作**。本综合实践让你把这条「参数预处理流水线」一次性串起来。

**任务**：阅读 [correlate1d 的完整实现（_filters.py:555-612）](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L555-L612)，然后回答下面的问题，并用代码验证你的判断。

**问题与验证步骤**：

1. **画流水线**：用文字（或简图）标出 `correlate1d` 在调用 C 内核 `_nd_image.correlate1d` 之前，依次调用了本讲的哪几个支撑工具、各在哪一行。
   - 参考答案：第 594 或 598 行调用 `_get_output`（取决于是否定复数）；第 609 行调用 `_extend_mode_to_code`；docstring 通过第 555 行的 `@_ni_docstrings.docfiller` 装饰器在定义期填充。（注意 `correlate1d` 因为是 1-D，没有用到 `_normalize_sequence` / `_check_axes`，但它在 N-D 的 `gaussian_filter` 等函数里大量出现。）

2. **验证 `output` 多态**：对同一个输入，分别用 `output=None`、`output=np.float64`、`output=已存在的 float64 数组` 三种方式调用 `scipy.ndimage.correlate1d`，打印三次结果的 `dtype`，并验证「传数组」那种是否真的写进了你传入的数组。

   ```python
   import numpy as np
   from scipy.ndimage import correlate1d
   x = np.array([2, 8, 0, 4, 1, 9, 9, 0], dtype=np.float64)

   r1 = correlate1d(x, weights=[1, 3])                 # None
   r2 = correlate1d(x, weights=[1, 3], output=np.float32)
   pre = np.zeros_like(x)
   r3 = correlate1d(x, weights=[1, 3], output=pre)      # 已存在数组
   print(r1.dtype, r2.dtype, r3.dtype, r3 is pre)
   ```

   预期：`r1.dtype=float64`、`r2.dtype=float32`、`r3.dtype=float64` 且 `r3 is pre` 为 `True`。

3. **验证 mode 编码**：分别用 `mode='reflect'`、`mode='grid-mirror'`、`mode='nearest'` 调用 `correlate1d`，比较结果。前两者应当完全相同（因为它们在 [_ni_support.py:44-45](_ni_support.py#L44-L45) 同码为 2），而 `'nearest'` 会不同（码 0）。

完成这三步后，你应该能体会到：本讲的四个支撑工具构成了 ndimage 一切函数的「公共骨架」——理解了它们，就理解了所有 ndimage 函数前 80% 的样板代码。

## 6. 本讲小结

- `_extend_mode_to_code` 把 8 个边界模式字符串翻译成 C 内核需要的 7 个整数码（0–6）；`is_filter` 参数只在 `'grid-wrap'` / `'grid-constant'` 上起作用，让纯滤波场景归并复用 C 内核，而插值场景保持独立语义。
- `_get_output` 统一处理 `output` 的三种形态——`None`（新建同类型）、dtype 类/字符串（按指定类型新建）、已存在数组（就地复用）——并额外管理复数输出的提升与校验策略。
- `_normalize_sequence` 把标量广播成等长序列、把序列校验成等长（先排除字符串以免误拆）；`_check_axes` 把 `None`/标量/序列统一成合法、唯一、非负的轴元组。
- `docfiller` 基于 `scipy._lib.doccer`，用 `%(key)s` 占位符 + `docdict` 字典，让数十个函数共享同一份参数文档，并把「只有默认值不同」的文档变体用 `.replace()` 派生。
- 这四个工具是 ndimage 所有函数的公共骨架：先 `docfiller` 填文档，再 `_get_output` 备输出，再用 `_normalize_sequence` / `_check_axes` 规范参数，最后 `_extend_mode_to_code` 把 mode 编码交给 C 内核。
- 它们全部集中在 [_ni_support.py](_ni_support.py) 与 [_ni_docstrings.py](_ni_docstrings.py) 两个文件，是整个子包「薄 Python 层」中最值得先读的部分。

## 7. 下一步学习建议

入门单元（u1）到此结束，你已经建立了 ndimage 的全局视图与公共骨架认知。接下来建议按功能域深入：

- **下一站 u2-l1（一维相关与卷积基础）**：本讲多次出现的 `correlate1d` 将在那里被完整剖析，你会看到 `weights` / `origin` / `mode` 如何真正参与一维卷积计算，以及复数输入为何要拆成实部虚部。
- **若对插值更感兴趣**，可跳到 u3-l1（样条预滤波），但要先读完 u2-l1 理解 `mode` 与 `origin` 的语义。
- **想下探到 C 层**的读者，可以在读完 u2 几篇后直接看 u6-l2（C 端迭代器、行缓冲与边界扩展），届时你会看到本讲的 `_extend_mode_to_code` 输出的整数码如何对应到 C 端 `NI_ExtendMode` 枚举与 `NI_ExtendLine` 的边界扩展实现。

无论走哪条线，本讲的四个工具都会反复出现——遇到看不懂的「参数预处理」段落，随时回来查阅。

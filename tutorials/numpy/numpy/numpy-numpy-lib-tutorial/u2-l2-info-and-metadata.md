# info、元信息与元数据工具

## 1. 本讲目标

本讲专讲 `numpy/lib/_utils_impl.py` 这个「运行期信息与元数据工具箱」。读完本讲，你应当能够：

- 用 `np.info(...)` 查看一个对象（数组、函数、类、字符串名）的文档与结构信息，并能讲清它对**不同类型对象**的分发逻辑。
- 读懂 `_info` 如何打印一个 `ndarray` 的底层布局（shape/strides/data pointer/byteorder 等）。
- 理解 `get_include` 返回的头文件目录在「编译扩展模块」时的用途。
- 用 `show_runtime` 和 `_opt_info` 查看当前 NumPy 构建支持的 CPU 特性，并区分两者的输出粒度。
- 掌握 `drop_metadata` 如何递归地把一个带 `metadata` 的 dtype「剥」成无元数据的等价 dtype，以及它为什么被 `np.save`/`np.savez` 调用。

---

## 2. 前置知识

本讲假设你已经建立 u1-l1、u1-l2 的认知框架，知道：

- **实现藏 `_impl`、对外只露薄模块**：`_utils_impl.py` 是实现层，里面的函数被向上搬到 `numpy` 顶层命名空间（如 `np.info`）。
- **`@set_module('numpy')`**：一个私有装饰器，把函数的 `__module__` 改写成 `'numpy'`，让 `np.info.__module__ == 'numpy'`，即使它真正定义在 `numpy.lib._utils_impl` 里。它的定义在 [_utils/__init__.py:L17-L38](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_utils/__init__.py#L17-L38)。

此外需要一点 Python 基础概念：

- **`inspect` 模块**：标准库里用来「内省（introspect）」对象的工具，如 `inspect.signature` 取函数签名、`inspect.getdoc` 取文档字符串、`inspect.isfunction/isclass` 判断对象种类。
- **dtype 的 `metadata`**：NumPy 的 dtype 可以挂一个任意的 Python 字典作为「元数据」（`np.dtype('f8', metadata={...})`），它不参与数值计算，只是附在类型上的标签。
- **dtype 的 `byteorder`**：多字节数值的字节序标记，取值 `|`（单字节，无序）、`=`（本机序）、`<`（小端）、`>`（大端）。

---

## 3. 本讲源码地图

本讲只涉及一个主源文件，外加两处真实调用点：

| 文件 | 作用 |
| --- | --- |
| [numpy/lib/_utils_impl.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py) | 本讲主角：`info`/`_info`/`get_include`/`show_runtime`/`_opt_info`/`drop_metadata` 全部在此 |
| [numpy/lib/_format_impl.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py) | `dtype_to_descr` 内调用 `drop_metadata`（保存数组前剥元数据） |
| [numpy/_pytesttester.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py) | `_show_numpy_info` 调用 `_opt_info()`，打印测试启动时的 CPU 特性行 |

文件顶部的公开声明只有三个名字：

```python
__all__ = [
    'get_include', 'info', 'show_runtime'
]
```

参考 [_utils_impl.py:L10-L12](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L10-L12)。注意：`drop_metadata` 与 `_opt_info` **不在** `__all__` 里——前者是「半公开（semi-public）」工具，后者是内部工具，它们靠「不带前缀下划线 / 带下划线」和文档警告来表明可见性，而不是靠 `__all__`。

> 小提示：这个文件里还住着一个与本讲无关的函数 `_median_nancheck`（[_utils_impl.py:L407-L447](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L407-L447)），它是 `median` 系列的 NaN 兜底逻辑，属于 u7-l1 的内容，阅读时跳过即可，不要被它干扰。同样地，`_get_indent`（[_utils_impl.py:L123-L134](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L123-L134)）在整个源码树里**没有任何调用方**，是一段历史遗留的死代码，知道它的存在即可，不必去追它的调用链。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**`info`/`_info`**（看对象）、**`show_runtime`/`_opt_info`**（看环境）、**`get_include`**（找头文件）、**`drop_metadata`**（剥元数据）。

### 4.1 info 与 _info：查看对象与数组的运行期信息

#### 4.1.1 概念说明

`np.info(object)` 是一个「万能帮助函数」：你丢给它任何东西——数组、函数、类、字符串——它都能给出一段可读信息。它的定位介于 `print(obj.__doc__)` 和 `help(obj)` 之间：比前者更有结构，又不像 `help()` 那样接管整个终端分页器（源码注释里明确写了 "pydoc defines a help function which works similarly to this except it uses a pager"，见 [_utils_impl.py:L140-L141](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L140-L141)）。

关键设计：`info` 本身**只做「按类型分发」**，真正干活的是各分支：

- 数组 → 委托给私有 `_info`，打印底层内存布局。
- 函数/方法 → 打印「签名 + docstring」。
- 类 → 打印「签名 + docstring + 公开方法列表」。
- 字符串 → 在整个 numpy 模块树里**按名字搜索**，列出所有同名对象。
- 其它有 `__doc__` 的对象 → 打印 docstring。

`_info` 则是一个「数组体检报告」：它不关心数值，只关心这块内存**长什么样、怎么排布**。

#### 4.1.2 核心流程

`info` 的分发流程可以用下面这段伪代码描述：

```
info(object, maxwidth, output, toplevel):
    处理 ppimport 延迟导入代理（历史遗留）
    if object is None:        info(info)            # 打印 info 自己
    elif object 是 ndarray:    _info(object)         # 数组体检
    elif object 是 str:        在 toplevel 模块树里搜名字
    elif object 是函数/方法:    打印 签名 + doc
    elif object 是类:          打印 签名 + doc + 方法表
    elif object 有 __doc__:    打印 doc
```

`_info` 对数组的打印项与判定逻辑：

| 打印项 | 取值来源 | 含义 |
| --- | --- | --- |
| `class` | `type(obj).__name__` | 对象类型名（通常是 `ndarray`） |
| `shape` / `strides` / `itemsize` | `obj.shape` 等 | 几何与步长信息 |
| `aligned` / `contiguous` / `fortran` | `obj.flags.*` | 内存对齐与连续性标志 |
| `data pointer` | `hex(obj.ctypes._as_parameter_.value)` | 数据缓冲区的起始地址 |
| `byteorder` / `byteswap` | 由 `dtype.byteorder` 推导 | 字节序与是否需要翻转 |
| `type` | `obj.dtype` | 元素类型 |

其中 `byteorder`/`byteswap` 的推导最值得记：

| `dtype.byteorder` | 打印的 byteorder | `byteswap` 取值 |
| --- | --- | --- |
| `\|`（单字节）或 `=`（本机序） | `sys.byteorder`（当前机器序） | `False` |
| `>`（大端） | `big` | `sys.byteorder != 'big'` |
| `<`（小端，else 分支） | `little` | `sys.byteorder != 'little'` |

也就是说，`byteswap` 回答的是：**这块数据如果直接按本机序读，要不要先翻一下字节**。

#### 4.1.3 源码精读

先看 `info` 的签名与整体分发。注意它用 `@set_module('numpy')` 把自己「伪装」成顶层函数：

```python
@set_module('numpy')
def info(object=None, maxwidth=76, output=None, toplevel='numpy'):
```

参考 [_utils_impl.py:L245-L246](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L245-L246)。`maxwidth=76` 控制签名换行宽度，`output` 默认走 `sys.stdout`，`toplevel='numpy'` 是字符串搜索的起点。

字符串搜索分支最能体现「按名字找对象」的设计：它先把整个 numpy 模块树展平成「模块名 → 该模块字典」的缓存，再逐个模块去查这个名字：

```python
if _namedict is None:
    _namedict, _dictlist = _makenamedict(toplevel)
...
for namestr in _dictlist:
    try:
        obj = _namedict[namestr][object]
        ...
```

参考 [_utils_impl.py:L327-L337](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L327-L337)。`_namedict`/`_dictlist` 是模块级全局变量（[_utils_impl.py:L166-L167](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L166-L167)），**只构建一次就缓存住**，避免每次 `info('fft')` 都重新遍历模块树。构建逻辑在 `_makenamedict`，它用广度优先遍历所有子模块（[_utils_impl.py:L171-L188](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L171-L188)）。同一个对象可能在多个模块里出现（别名），代码用 `id(obj)` 去重并打印「Repeat reference found」。

函数/方法分支会调用 `_split_line` 把过长的签名按 `maxwidth` 折行（[_utils_impl.py:L146-L163](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L146-L163)），再打印 docstring：

```python
print(" " + argstr + "\n", file=output)
print(inspect.getdoc(object), file=output)
```

参考 [_utils_impl.py:L367-L368](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L367-L368)。类分支（[_utils_impl.py:L370-L401](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L370-L401)）在此基础上额外用 `pydoc.allmethods` 列出**公开方法**（名字不以 `_` 开头），并给每个方法配一行摘要——这就是为什么 `info(某个类)` 末尾会出现一段 `Methods:` 列表。

再看 `_info`，它对 `byteorder`/`byteswap` 的推导就是上面那张表的真实代码：

```python
if endian in ['|', '=']:
    print(f"{tic}{sys.byteorder}{tic}", file=output)
    byteswap = False
elif endian == '>':
    print(f"{tic}big{tic}", file=output)
    byteswap = sys.byteorder != "big"
else:
    print(f"{tic}little{tic}", file=output)
    byteswap = sys.byteorder != "little"
```

参考 [_utils_impl.py:L231-L241](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L231-L241)。数据指针那行用的是 ctypes 暴露的句柄：

```python
print(f"data pointer: {hex(obj.ctypes._as_parameter_.value)}{extra}", file=output)
```

参考 [_utils_impl.py:L227-L230](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L227-L230)。`_as_parameter_` 是 ctypes 协议里「把对象转成 C 可用句柄」的钩子，对 ndarray 就是数据缓冲区的指针。

#### 4.1.4 代码实践

> 实践目标：亲眼看到 `info` 对「ufunc」「数组」「字符串」三类对象的不同输出，并验证 `_info` 的 `byteswap` 判定。

操作步骤：

1. 在已安装 NumPy 的环境里运行下面脚本（**待本地验证**，因为 `data pointer`、`sys.byteorder`、CPU 特性都因机器而异）：

   ```python
   # 示例代码
   import sys
   import numpy as np

   # ① ufunc：落到「有 __doc__」分支，打印 docstring
   np.info(np.add)

   # ② 数组：走 _info，打印底层布局
   a = np.array([[1+2j, 3, -4], [-5j, 6, 0]], dtype=np.complex64)
   np.info(a)

   # ③ 字符串：走模块树搜索分支
   np.info('fft')

   # ④ 构造一个大端数组，观察 byteswap
   big = np.arange(4, dtype='>i4')
   print("本机 byteorder =", sys.byteorder)
   np.info(big)
   ```

2. 需要观察的现象：
   - `np.info(np.add)` 应只打印一段 `add(x1, x2, /, ...)` 的说明文档（ufunc 不是 Python 函数，不会出现 `Methods:` 列表）。
   - `np.info(a)` 应打印 `class / shape / strides / itemsize / aligned / contiguous / fortran / data pointer / byteorder / byteswap / type` 共 11 行。
   - `np.info(big)` 的 `byteorder` 行应为 `big`；当本机是小端机时，`byteswap` 应为 `True`。

3. 预期结果：与本机字节序相关的那两行会随机器变化，其余结构稳定。

> 你也可以直接阅读官方测试 [tests/test_utils.py:L15-L31](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_utils.py#L15-L31) 里的 `test_info_method_heading`，它用 `StringIO` 捕获 `np.info(cls, output=out)` 的输出来断言「只有存在公开方法时才打印 `Methods:` 标题」——这正是 4.1.3 里类分支的逻辑。

#### 4.1.5 小练习与答案

**练习 1**：`np.info(np.add)` 为什么不会进入 `info` 的「函数/方法」分支？

> **参考答案**：`np.add` 是 `numpy.ufunc` 实例，既不是 Python 函数也不是方法（`inspect.isfunction(np.add)` 与 `inspect.ismethod(np.add)` 都为 `False`），也不是类，于是落到最后的 `elif hasattr(object, '__doc__')` 分支，只打印 docstring。

**练习 2**：为什么 `np.info('fft')` 第二次调用比第一次快？

> **参考答案**：第一次调用字符串分支时，`_makenamedict('numpy')` 会广度优先遍历整个 numpy 模块树，结果缓存进模块级全局变量 `_namedict`/`_dictlist`（[_utils_impl.py:L166-L167](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L166-L167)）；之后 `_namedict is None` 不再成立，直接复用缓存，省去了再次遍历的开销。

**练习 3**：把一个 `'>i4'` 数组传给 `_info`，在**小端机**上 `byteswap` 会打印什么？为什么？

> **参考答案**：会打印 `True`。`dtype.byteorder == '>'`，进入 `elif endian == '>'` 分支，`byteswap = sys.byteorder != 'big'`；小端机上 `sys.byteorder == 'little'`，故结果为 `True`，表示这块数据若按本机序直读需先翻转字节。

---

### 4.2 show_runtime 与 _opt_info：运行期环境与 CPU 特性

#### 4.2.1 概念说明

这一组函数回答的是「**我这套 NumPy 在这台机器上到底能用上哪些硬件能力**」：

- `show_runtime`（**公开**，`np.show_runtime()`）：打印一份完整的环境报告，包含 NumPy/Python 版本、操作系统、SIMD 基线与分发特性、BLAS 是否忽略浮点错误、以及（若装了 `threadpoolctl`）底层线程池信息。
- `_opt_info`（**内部**，带下划线）：只返回一个**字符串**，描述 CPU 特性，用 `*`/`?` 后缀区分「本机支持/不支持」的分发特性。

二者读取的是同一组 C 层常量 `__cpu_baseline__`/`__cpu_dispatch__`/`__cpu_features__`，只是 `show_runtime` 是给人看的详细报告，`_opt_info` 是给程序拼字符串用的精简摘要。

#### 4.2.2 核心流程

`_opt_info` 的编码规则（直接抄自其 docstring）：

- 基线特性（`__cpu_baseline__`）：**无后缀**，原样用空格拼接。
- 分发特性（`__cpu_dispatch__`）：
  - 本机**支持** → 特性名后加 `*`；
  - 本机**不支持** → 特性名后加 `?`。

即输出形如 `BASELINE SSE2 SSE42* AVX512F?`（示意，待本地验证实际值）。

`show_runtime` 的流程：

```
show_runtime():
    收集 [numpy_version, python, uname]                # 第一块
    遍历 __cpu_dispatch__，按 __cpu_features__ 分到 found/not_found   # 第二块
    追加 BLAS 是否忽略浮点错误                          # 第三块
    尝试 import threadpoolctl，成功则追加线程池信息     # 第四块（可选）
    pprint 打印整张列表
```

#### 4.2.3 源码精读

`_opt_info` 的核心就是一个循环加后缀拼接，并在「没有任何基线/分发特性」时返回空串：

```python
if len(__cpu_baseline__) == 0 and len(__cpu_dispatch__) == 0:
    return ''

enabled_features = ' '.join(__cpu_baseline__)
for feature in __cpu_dispatch__:
    if __cpu_features__[feature]:
        enabled_features += f" {feature}*"
    else:
        enabled_features += f" {feature}?"
return enabled_features
```

参考 [_utils_impl.py:L449-L479](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L449-L479)。注意 `__cpu_features__` 是一个「特性名 → 本机是否支持」的字典，所以 `__cpu_features__[feature]` 就是判定 `*` 还是 `?` 的依据。

`show_runtime` 同样从 C 层取这三组常量，并按「分发特性是否被本机支持」分成两组：

```python
for feature in __cpu_dispatch__:
    if __cpu_features__[feature]:
        features_found.append(feature)
    else:
        features_not_found.append(feature)
```

参考 [_utils_impl.py:L49-L60](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L49-L60)。它还会尝试加载可选依赖 `threadpoolctl`，找不到时打印一条安装提示而非崩溃（[_utils_impl.py:L66-L73](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L66-L73)）——这是「软依赖」的常见写法。

`_opt_info` 的真实调用方是测试启动器。每次你跑 `numpy.lib.test()` 时，`_show_numpy_info` 会先打印两行：

```python
def _show_numpy_info():
    import numpy as np
    print(f"NumPy version {np.__version__}")
    info = np.lib._utils_impl._opt_info()
    print("NumPy CPU features: ", (info or 'nothing enabled'))
```

参考 [_pytesttester.py:L37-L42](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py#L37-L42)。这正是 u1-l3 里提到的「测试启动时最先看到的 NumPy version / NumPy CPU features 两行」的真正来源。当 `_opt_info()` 返回空串时，`info or 'nothing enabled'` 兜底成可读文案。

#### 4.2.4 代码实践

> 实践目标：对比 `show_runtime`（详细报告）与 `_opt_info`（精简字符串）的输出粒度。

操作步骤：

1. 运行下面脚本（**待本地验证**，输出与机器/CPU 强相关）：

   ```python
   # 示例代码
   import numpy as np
   import numpy.lib._utils_impl as _u

   print("=== _opt_info() ===")
   print(repr(_u._opt_info()))

   print("\n=== show_runtime() ===")
   np.show_runtime()
   ```

2. 需要观察的现象：
   - `_opt_info()` 返回一行字符串，其中基线特性无后缀、支持的分发特性带 `*`、不支持的带 `?`。
   - `show_runtime()` 打印一个嵌套字典/列表，包含 `numpy_version`、`python`、`uname`、`simd_extensions`（含 `baseline/found/not_found`）、`ignore_floating_point_errors_in_matmul` 等键。
   - 若未安装 `threadpoolctl`，会看到一条 `WARNING: threadpoolctl not found ...` 提示。

3. 预期结果：两份输出描述的是**同一组** CPU 特性，但 `_opt_info` 是单行字符串，`show_runtime` 是结构化报告。

#### 4.2.5 小练习与答案

**练习 1**：在 `_opt_info` 的输出里，`AVX512F*` 和 `AVX512F?` 分别代表什么？

> **参考答案**：二者都说明 `AVX512F` 属于「分发特性」（出现在 `__cpu_dispatch__` 里，NumPy 编译了它的代码路径但运行时按需启用）。带 `*` 表示**当前机器支持**该特性、会被实际分用到；带 `?` 表示**当前机器不支持**、该路径不会被启用。无后缀的特性则是「基线」（`__cpu_baseline__`），编译时就要求必须支持。

**练习 2**：为什么 `show_runtime` 对 `threadpoolctl` 的缺失只是打印警告而不是报错？

> **参考答案**：因为 `threadpoolctl` 是**可选依赖**：有它就能额外打印底层 BLAS/OpenMP 线程池信息，没有它也不影响 NumPy 本身工作。代码用 `try/except ImportError`（[_utils_impl.py:L66-L73](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L66-L73)）把它做成「软依赖」，降级为提示信息而非中断。

---

### 4.3 get_include：定位 NumPy 的 C 头文件目录

#### 4.3.1 概念说明

当你写 C 扩展（或 Cython/pybind11 模块）需要调用 NumPy 的 C-API（比如 `PyArray_SimpleNew`、`PyArray_DATA`），编译器必须能找到 `numpy/arrayobject.h` 这类头文件。`get_include()` 就是返回**这些头文件所在的目录**，供你在 `setup.py` 里写 `include_dirs=[np.get_include()]`。

它的 docstring 还提示：NumPy 2.0 起推荐用命令行工具 `numpy-config --cflags` 或 `pkg-config`，对非 setuptools 的构建系统更友好（[_utils_impl.py:L95-L104](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L95-L104)）。

#### 4.3.2 核心流程

```
get_include():
    if numpy.show_config is None:        # 从源码目录运行（未安装）
        d = numpy/__file__ 同级 / _core / include
    else:                                # 已安装
        d = numpy/_core/__file__ 同级 / include
    return d
```

它用一个很巧妙的「探针」区分两种运行场景：`numpy.show_config` 这个属性是否存在（非 None）。

#### 4.3.3 源码精读

```python
import numpy
if numpy.show_config is None:
    # running from numpy source directory
    d = os.path.join(os.path.dirname(numpy.__file__), '_core', 'include')
else:
    # using installed numpy core headers
    import numpy._core as _core
    d = os.path.join(os.path.dirname(_core.__file__), 'include')
return d
```

参考 [_utils_impl.py:L112-L120](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L112-L120)。

要点：

- **`numpy.show_config is None` 当探针**：在已安装的 NumPy 里，`show_config` 是一个真实函数（非 None）；而在源码树里直接运行时它被设成 `None`。借此判断当前是「开发态」还是「安装态」，从而选对头文件目录。这其实是个有点 hack 的判定，但在实践中有效。
- 两条路径最终都指向同一个相对位置：`numpy/_core/include`，那里就是 `arrayobject.h` 等头文件的家。

#### 4.3.4 代码实践

> 实践目标：拿到头文件目录，并确认里面确实有 `numpy/arrayobject.h`。

操作步骤：

1. 运行：

   ```python
   # 示例代码
   import os
   import numpy as np

   inc = np.get_include()
   print("include dir:", inc)
   print("arrayobject.h exists:",
         os.path.exists(os.path.join(inc, 'numpy', 'arrayobject.h')))
   ```

2. 需要观察的现象：第一行打印一个绝对路径（通常以 `.../numpy/_core/include` 结尾）；第二行打印 `True`。

3. 预期结果：路径随安装方式变化（pip 装、conda 装、源码运行各不同），但 `arrayobject.h` 应当存在（**待本地验证**）。

> 拓展（源码阅读型）：对照 docstring 里给的 `numpy-config --cflags` 用法（[_utils_impl.py:L95-L104](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L95-L104)），理解现代构建系统为何更倾向用 `pkg-config` 而不是在 `setup.py` 里硬调 `np.get_include()`——本质都是为了拿到同一个 `-I.../numpy/_core/include`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `get_include` 要用 `numpy.show_config is None` 来分流，而不是直接判断 `numpy.__file__`？

> **参考答案**：因为「文件在哪里」和「是不是安装态」并不完全等价。源码树里运行时 `numpy.__file__` 指向源码目录，但头文件未必随源码铺开；而已安装时 `show_config` 是真实函数。用 `show_config is None` 作探针能更稳地区分「开发态 vs 安装态」，再据此拼出 `_core/include`。这是历史形成的实用判定，并非语言层面的保证。

**练习 2**：`get_include()` 返回的目录与 `np.__file__` 所在目录是什么关系？

> **参考答案**：二者都在 NumPy 包目录下。已安装时，返回值是 `numpy._core` 包目录下的 `include` 子目录，即 `numpy包根/_core/include`；因此它比 `np.__file__`（包根的 `__init__.py`）多出 `_core/include` 这段后缀。

---

### 4.4 drop_metadata：去除 dtype 的元数据

#### 4.4.1 概念说明

NumPy 的 dtype 可以挂一个 `metadata` 字典（如 `np.dtype('f8', metadata={'units': 'm'})`）。这个字典是任意 Python 对象，**不可序列化为 .npy/.npz 的二进制格式**。因此 `np.save`/`np.savez` 在落盘前必须先把它剥掉，否则保存出的文件没法被稳定还原。

`drop_metadata(dtype)` 干的就是这件事：**若 dtype（或其任意嵌套子 dtype）带了 metadata，返回一个等价但无 metadata 的新 dtype；若完全没带，原样返回同一个对象**（用 `is` 判等价）。

> 它的 docstring 自称「semi-public API only」，并警告对 record dtype / 用户自定义 dtype 可能不精确，建议用 `np.can_cast(new_dtype, dtype, casting="no")` 复核（[_utils_impl.py:L481-L500](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L481-L500)）。这也是它**不在** `__all__` 里的原因。

#### 4.4.2 核心流程

`drop_metadata` 按 dtype 的形态分三条路，且**递归**处理嵌套结构：

```
drop_metadata(dtype):
    if dtype.fields is not None:        # 结构化 dtype
        逐字段递归 drop_metadata(field[0])
        只要任一处发现 metadata，就用 {names,formats,offsets,titles,itemsize} 重建
        否则原样返回 dtype
    elif dtype.subdtype is not None:    # 子数组 dtype，如 '8f'
        递归 drop_metadata(子dtype)，连同 shape 重建
        无变化则原样返回 dtype
    else:                               # 普通标量 dtype
        metadata is None → 原样返回 dtype
        否则用 dtype.str 重建一个无 metadata 的 dtype
```

关键设计有两处：

- **递归**：结构化 dtype 的字段本身还可能是结构化 dtype，所以每个字段都要递归剥一遍。
- **惰性短路**：只有真的发现 metadata 时才重建 dtype；否则返回原对象，保证 `drop_metadata(d) is d`（身份相等），让调用方能用 `is` 快速判断「有没有发生改动」。

#### 4.4.3 源码精读

结构化分支里，它把每个字段的 `(name, (dtype, offset, title))` 拆开，递归处理字段 dtype，并记录 `found_metadata`：

```python
for name, field in dtype.fields.items():
    field_dt = drop_metadata(field[0])
    if field_dt is not field[0]:
        found_metadata = True
    names.append(name)
    formats.append(field_dt)
    offsets.append(field[1])
    titles.append(None if len(field) < 3 else field[2])
...
# NOTE: Could pass (dtype.type, structure) to preserve record dtypes...
return np.dtype(structure, align=dtype.isalignedstruct)
```

参考 [_utils_impl.py:L509-L527](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L509-L527)。注意两点：

- 重建时用 `align=dtype.isalignedstruct` **保留了原 dtype 的对齐设置**，这对带 padding 的结构化数组很关键。
- 那行注释承认了一个取舍：这里没有传 `(dtype.type, structure)`，所以 **record dtype（`np.recarray` 用的那种）的类型信息不会被保留**——这正是 docstring 警告的「不保证 record/user dtype 正确」的来源。

普通标量分支最简单，靠 `dtype.str` 重建：

```python
else:
    # Normal unstructured dtype
    if dtype.metadata is None:
        return dtype
    # Note that `dt.str` doesn't round-trip e.g. for user-dtypes.
    return np.dtype(dtype.str)
```

参考 [_utils_impl.py:L537-L541](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L537-L541)。`dtype.str` 是 dtype 的「规范字符串」（如 `'<f8'`），重建出来的新 dtype 天然没有 metadata；但注释再次提醒，对用户自定义 dtype，`dtype.str` 不能无损往返。

它的真实调用方在 `_format_impl.py` 的 `dtype_to_descr`（把 dtype 转成可序列化的 descr 时）：

```python
new_dtype = drop_metadata(dtype)
if new_dtype is not dtype:
    warnings.warn("metadata on a dtype is not saved to an npy/npz. "
                  "Use another format (such as pickle) to store it.",
                  UserWarning, stacklevel=2)
dtype = new_dtype
```

参考 [_format_impl.py:L274-L281](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py#L274-L281)。这里正是利用了「身份相等 ⇔ 无改动」的短路约定：`is not dtype` 一旦成立，就说明确实剥掉了元数据，于是给用户发一条 `UserWarning` 提示「元数据不会被保存」。这就是 `np.save` 一个带 metadata 的数组会弹出警告的根因。

#### 4.4.4 代码实践

> 实践目标：亲手剥一个带 metadata 的 dtype，验证「身份保留」与「递归剥离」两条性质。

操作步骤：

1. 运行下面脚本（**待本地验证**）：

   ```python
   # 示例代码
   import numpy as np
   from numpy.lib._utils_impl import drop_metadata

   # (a) 无 metadata：身份不变
   plain = np.dtype('float64')
   print("plain is drop_metadata(plain):", drop_metadata(plain) is plain)

   # (b) 标量带 metadata：剥成新对象
   tagged = np.dtype('float64', metadata={'units': 'm'})
   cleaned = drop_metadata(tagged)
   print("tagged.metadata:", tagged.metadata)
   print("cleaned.metadata:", cleaned.metadata)
   print("cleaned is tagged:", cleaned is tagged)

   # (c) 嵌套结构化 dtype：递归剥离
   nest = np.dtype(
       [('pos', [('x', np.dtype('f8', metadata={'msg': 'inner'}))])],
       metadata={'msg': 'outer'},
   )
   dn = drop_metadata(nest)
   print("dn.metadata:", dn.metadata)
   print("dn['pos'].metadata:", dn['pos'].metadata)
   print("dn['pos']['x'].metadata:", dn['pos']['x'].metadata)
   ```

2. 需要观察的现象：
   - (a) 打印 `True`（无 metadata 时原样返回）。
   - (b) `tagged.metadata` 为 `{'units': 'm'}`，`cleaned.metadata` 为 `None`，`cleaned is tagged` 为 `False`。
   - (c) 三层 `metadata` 全部变成 `None`，证明递归剥到了最内层字段。

3. 预期结果：与官方测试 [tests/test_utils.py:L34-L67](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_utils.py#L34-L67) 的 `test_drop_metadata` 一致——该测试对结构化、对齐、子数组、标量四种形态都断言 `np.can_cast(dt, dt_m, casting='no')` 为 `True` 且 `dt_m.metadata is None`。

> 拓展：把 (b) 的数组 `np.save` 到临时文件，应当看到一条 `UserWarning: metadata on a dtype is not saved to an npy/npz`——这就是 4.4.3 里 `dtype_to_descr` 触发的。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `drop_metadata` 在「无 metadata」时要返回原对象（`return dtype`）而不是 `return np.dtype(dtype.str)`？

> **参考答案**：为了让调用方用 `is`（身份比较）**零成本判断「有没有发生改动」**。`_format_impl.py` 里正是 `if new_dtype is not dtype:` 来决定是否发警告（[_format_impl.py:L277-L280](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py#L277-L280)）。若每次都重建一个等价 dtype，`is` 永远为 `False`，这条短路就失效了，还会无谓地多造对象。

**练习 2**：对一个嵌套结构化 dtype，`drop_metadata` 如何保证不漏掉内层字段的 metadata？

> **参考答案**：它在 `dtype.fields` 分支里对**每个字段的 dtype 递归调用** `drop_metadata(field[0])`（[_utils_impl.py:L509-L510](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L509-L510)）。只要任一层返回了新对象（`field_dt is not field[0]`），就把 `found_metadata` 置真，最终用收集到的 `names/formats/offsets/titles/itemsize` 重建整个外层 dtype，从而把所有层的 metadata 一起剥净。

**练习 3**：docstring 为什么建议用 `np.can_cast(new_dtype, dtype, casting="no")` 复核结果？

> **参考答案**：因为对 record dtype 和用户自定义 dtype，`drop_metadata` 的重建并不保证完全等价（见 [_utils_impl.py:L526](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_utils_impl.py#L526) 那行 NOTE 与 docstring 警告）。`casting="no"` 表示「只允许完全相同的类型转换」，用它做一次校验能确认剥完元数据后的 dtype 与原来在数值语义上完全一致，捕捉那些被静默改坏的情况。

---

## 5. 综合实践

> 综合任务：写一个小脚本，把本讲的四个工具串起来，给一个数组做「体检 + 元数据清洗」并打印环境摘要。

操作步骤：

1. 运行下面脚本（**待本地验证**）：

   ```python
   # 示例代码
   import numpy as np
   from numpy.lib._utils_impl import drop_metadata, _opt_info

   # 1) 环境摘要：用 _opt_info 拿 CPU 特性字符串
   print("CPU features:", _opt_info() or "nothing enabled")

   # 2) 构造一个带 metadata 的结构化数组
   dt = np.dtype([('id', 'i4'), ('val', np.dtype('f8', metadata={'u': 'm'}))],
                 metadata={'owner': 'lab'})
   arr = np.zeros(3, dtype=dt)

   # 3) 用 info 给数组做体检（走 _info 分支）
   print("\n-- array info --")
   np.info(arr)

   # 4) 落盘前剥元数据，验证递归剥离
   clean_dt = drop_metadata(arr.dtype)
   print("\norig metadata:", arr.dtype.metadata)
   print("clean metadata:", clean_dt.metadata)
   print("clean['val'].metadata:", clean_dt['val'].metadata)
   print("identity preserved?", drop_metadata(np.dtype('i4')) is np.dtype('i4'))
   ```

2. 需要观察的现象：
   - 第 1 行打印 CPU 特性字符串（基线无后缀，分发特性带 `*`/`?`）。
   - `-- array info --` 段打印结构化数组的 `class/shape/strides/...` 布局信息。
   - `orig metadata` 为 `{'owner': 'lab'}`；`clean metadata` 与 `clean['val'].metadata` 均为 `None`。
   - `identity preserved?` 为 `True`。

3. 这条链路覆盖了：`_opt_info`（环境）→ `info`/`_info`（对象内省）→ `drop_metadata`（元数据清洗，对接 u12/u13 的 IO 流程）。`get_include` 因涉及 C 编译，留作可选拓展：在脚本末尾加一行 `print(np.get_include())` 并去该目录核对 `numpy/arrayobject.h`。

---

## 6. 本讲小结

- `numpy/lib/_utils_impl.py` 是一个「运行期信息 + 元数据工具箱」，公开 API 只有 `get_include`/`info`/`show_runtime` 三个（见 `__all__`），`drop_metadata` 是半公开、`_opt_info` 是内部工具。
- `info` 是按对象类型分发的「万能帮助函数」：数组走 `_info` 打印底层布局，函数/类走 `inspect` 取签名与方法表，字符串走 `_makenamedict` 缓存的模块树搜索；`_info` 的 `byteorder`/`byteswap` 由 `dtype.byteorder` 与 `sys.byteorder` 共同推导。
- `show_runtime` 打印结构化的环境报告（版本/SIMD/BLAS/可选线程池），`_opt_info` 则把同一组 CPU 特性压成带 `*`/`?` 后缀的单行字符串，后者被 `_pytesttester` 用于测试启动横幅。
- `get_include` 用 `numpy.show_config is None` 探针区分源码态/安装态，返回 `numpy/_core/include` 头文件目录，供 C 扩展编译用。
- `drop_metadata` 递归地按「结构化 / 子数组 / 标量」三条路剥除 dtype 的 metadata，**无改动时原样返回同一对象**，使调用方（`dtype_to_descr`）能用 `is` 判断是否需要发保存警告。

---

## 7. 下一步学习建议

- **横向到 u2-l3**：本讲的 `info`/`_info` 属于「对象内省」，u2-l3 会继续讲数组内省与接入工具（`opt_func_info`、`byte_bounds`、`NDArrayOperatorsMixin`），其中 `opt_func_info` 与本讲的 `_opt_info` 在「CPU 分发」主题上互为补充，可以对照阅读。
- **纵向到 IO（u12/u13）**：`drop_metadata` 的真正舞台是 `np.save`/`np.savez`。学完本讲后，到 u12-l2（header 序列化）和 u13-l1（load/save/NpzFile）去追踪 `dtype_to_descr → drop_metadata` 这条完整落盘链路，你会看到 metadata 是如何被「剥掉 → 写 header → 警告用户」的。
- **源码延伸**：想深入了解 SIMD 分发机制，可从 `_opt_info`/`show_runtime` 读取的 `__cpu_baseline__`/`__cpu_dispatch__`/`__cpu_features__` 这三个 C 层常量入手，去 `numpy/_core` 查它们的生成来源（属于 advanced 层的 CPU 优化主题）。

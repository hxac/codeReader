# 在自己的 Cython 代码中使用 cython_special

## 1. 本讲目标

上一篇（u6-l1）我们读懂了 `cython_special` 的 `.pxd/.pyx/.pyi` 三件套，知道它是一组「吃 C 标量、按融合类型编译期特化、可 `noexcept nogil` 调用」的特殊函数，与命名空间里的 ufunc 同名但签名迥异。本篇把视角从「读源码」切到「写代码」——目标是让你在自己的 `.pyx` 里真正用上它。学完后你应当掌握：

1. 用 `cimport scipy.special.cython_special` 把类型化声明引入自己的 Cython 模块，并能正确处理融合类型的「特化」问题。
2. 在 `with nogil:` 释放 GIL 的热循环里逐标量调用特殊函数，并理解单返回值 / 多返回值（指针输出）两种调用写法。
3. 客观对比「cython_special 标量循环」与「`special.gamma` 的 ufunc 批量调用」在性能与适用场景上的权衡，知道何时该用哪个，而不是盲目相信某一方更快。

## 2. 前置知识

在动手之前，请确认你理解以下几个概念（不熟悉的话先补一下）：

- **Cython 基础**：`.pyx` 是 Cython 源文件，`.pxd` 是声明文件（相当于 C 的头文件）；`cdef` 声明 C 变量/函数，`cpdef` 同时给出 C 函数和 Python 包装。Cython 会被编译成 C/C++ 再编成 Python 扩展模块（`.so`）。
- **融合类型（fused type）**：Cython 里用 `ctypedef fused` 定义的「一组类型」的别名。一个声明使用了融合类型的函数，在编译时会被复制成多份「特化」版本（每种具体类型一份）。调用时编译器根据实参的静态类型选择对应版本。
- **GIL 与 `nogil`**：CPython 有全局解释器锁（GIL）。`with nogil:` 块临时释放它，允许多线程真正并行；但块内代码绝不能碰 Python 对象，只能用纯 C 类型。函数标注 `nogil` 表示它可在无 GIL 环境下安全调用。
- **typed memoryview**：Cython 里 `double[::1]` 这类语法，把一个连续缓冲区（如 NumPy 数组）以纯 C 指针的方式零开销访问，是连接「NumPy 数组」与「nogil 标量循环」的桥。
- **ufunc 内层循环**：回顾 u2-l1，`special.gamma(arr)` 之所以快，是因为它内部跑的是一个纯 C 写的、逐元素的循环，并非对每个元素回到 Python 调用一次。这一点对理解本讲的性能结论至关重要。

本讲承接 u6-l1（cython_special 的内部设计）、u2-l1（ufunc 基石）、u3-l3（meson 构建）。

## 3. 本讲源码地图

本讲主要「使用」而非「修改」以下文件，引用它们的目的是让你看清 cimport 进来的符号到底长什么样、由谁保证可用：

| 文件 | 作用 |
| --- | --- |
| [cython_special.pxd](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd) | 声明文件（头文件）。你 `cimport` 时拿到的就是这里的签名：融合类型定义 + 每个函数的 `cpdef/cdef ... noexcept nogil` 声明。 |
| [cython_special.pyx](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx) | 实现文件。顶部模块文档串写明了使用约定；函数体里 `if 融合类型 is double:` 是编译期分支。 |
| [cython_special.pyi](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyi) | 给 IDE/类型检查器看的桩文件，仅一句 `__getattr__`，因为类型化接口主要面向 Cython 而非 Python。 |
| [\_\_init\_\_.py](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/__init__.py) | 顶部文档串把 `scipy.special.cython_special` 列为「Typed Cython versions of special functions」，是官方公开用法入口。 |
| [meson.build](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build) | 把 `cython_special.pxd` 列入安装清单（L231），保证你 `cimport` 时能从已安装的 SciPy 里找到声明。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先讲如何 `cimport` 并处理融合类型（4.1），再讲在 `nogil` 热循环里的两种调用写法（4.2），最后给出客观的性能权衡与选型建议（4.3）。

### 4.1 cimport cython_special:把类型化声明引入自己的 .pyx

#### 4.1.1 概念说明

`cimport` 是 Cython 的「编译期导入」：它读取某个模块的 `.pxd` 声明文件，把其中声明的 C 函数、类型当作「编译期符号」引入当前 `.pyx`。这与运行期的 Python `import` 是两套机制——`cimport` 拿到的是带类型的 C 符号，调用时走的是直接 C 调用，没有 Python 方法分派开销。

`scipy.special.cython_special` 的 `.pxd` 把全部特殊函数都声明为 `noexcept nogil` 的 C 函数，并大量使用融合类型。这意味着：你 `cimport gamma` 之后得到的不是一个 Python 函数对象，而是一个**在编译期需要被「特化」为具体类型版本**的 C 函数名。

#### 4.1.2 核心流程

在自己 `.pyx` 里使用 cython_special 的最小流程：

1. 在 `.pyx` 顶部写 `cimport scipy.special.cython_special`（或 `from scipy.special.cython_special cimport gamma`）。
2. 编译该 `.pyx` 时，Cython 会在 `sys.path` 里定位已安装 SciPy 中的 `scipy/special/cython_special.pxd`，把它作为声明读入。
3. 调用融合类型函数时，**必须让编译器能唯一推断出特化版本**，否则编译报错。两种满足方式：
   - 把实参声明成确定的 C 类型（如 `cdef double x`），让编译器按实参类型推断；
   - 或用方括号**显式特化**：`gamma[double](x)`。
4. 因为这些函数都是 `nogil` 的，调用可以直接放进 `with nogil:` 块。

#### 4.1.3 源码精读

先看 `.pxd` 顶部的融合类型定义与几个代表性声明：

[cython_special.pxd:L2-L9](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd#L2-L9) 定义了第一组融合类型 `number_t`（`double complex` / `double`），并声明了四个球贝塞尔函数，注意 `bint derivative=*`——`=*` 表示「沿用 .pyx 实现里的默认值」，所以你在 cimport 侧调用时可省略 `derivative` 参数。

[cython_special.pxd:L11-L27](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd#L11-L27) 定义了本讲会用到的几组融合类型：

```cython
ctypedef fused Dd_number_t:   # double | double complex
    double complex
    double

ctypedef fused df_number_t:   # double | float
    double
    float

ctypedef fused dfg_number_t:  # double | float | long double
    double
    float
    long double

ctypedef fused dlp_number_t:  # double | long | Py_ssize_t
    double
    long
    Py_ssize_t
```

最关键的一条声明是 `gamma`，它使用了 `Dd_number_t`：

[cython_special.pxd:L114](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd#L114) 告诉 Cython：`gamma` 接受一个 `double` 或 `double complex`，返回同类型，且 `noexcept nogil`。当你 `cimport gamma` 并传入一个 `double` 变量时，Cython 就挑选 `Dd_number_t = double` 那一份特化。

`.pyx` 里 `gamma` 的实现印证了「融合类型 = 编译期多份特化代码」这件事：

[cython_special.pyx:L2445-L2455](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L2445-L2455)

```cython
cpdef Dd_number_t gamma(Dd_number_t x0) noexcept nogil:
    """See the documentation for scipy.special.gamma"""
    if Dd_number_t is double_complex:
        return _complexstuff.double_complex_from_npy_cdouble(xsf_cgamma(_complexstuff.npy_cdouble_from_double_complex(x0)))
    elif Dd_number_t is double:
        return xsf_gamma(x0)
    else:
        ...
```

这里的 `if Dd_number_t is double:` 不是运行期判断，而是**编译期分支**：Cython 为每种特化生成一份代码，每份里只保留命中的那个分支，其余被编译器丢弃，运行时零开销。复数分支需要 `_complexstuff` 在 `npy_cdouble`（NumPy 复数）与 `double complex`（C 复数）之间做纯 C 值拷贝，正是为了在 `nogil` 下绝不触碰 Python 对象（详见 u6-l1）。

最后，`cimport` 之所以能在「已安装的 SciPy」上工作，是因为构建时把 `.pxd` 装进了包目录：

[meson.build:L231-L232](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L231-L232) 把 `cython_special.pxd` 与 `cython_special.pyi` 列入安装文件清单。所以只要 `pip install scipy`，Cython 就能在 `site-packages/scipy/special/` 找到这个声明文件。

#### 4.1.4 代码实践

**实践目标**：验证 `cimport` 能解析、融合类型能正确特化，并体会「实参类型决定特化」。

**操作步骤**（示例代码，需本地有 Cython 与已安装的 SciPy）：

新建 `fused_demo.pyx`（**示例代码**，非项目源码）：

```cython
# cython: language_level=3
from scipy.special.cython_special cimport gamma

def gamma_scalar_double(double x):
    # x 是 double，编译器自动选 gamma 的 double 特化
    return gamma(x)

def gamma_scalar_explicit(double x):
    # 等价写法：显式特化，避免任何歧义
    return gamma[double](x)

# 取消下面注释会编译报错：Python 对象类型无法确定融合类型特化
# def gamma_ambiguous(x):
#     return gamma(x)
```

用最简 `setup.py` 编译（**示例代码**）：

```python
from setuptools import setup, Extension
from Cython.Build import cythonize
import numpy as np

setup(ext_modules=cythonize(
    [Extension("fused_demo", ["fused_demo.pyx"])],
    include_dirs=[np.get_include()],
),)
```

编译并验证：

```bash
python setup.py build_ext --inplace
python -c "from fused_demo import gamma_scalar_double; from scipy import special; print(gamma_scalar_double(5.0), special.gamma(5.0))"
```

**需要观察的现象**：`gamma_scalar_double(5.0)` 与 `special.gamma(5.0)` 输出一致（约 24.0）；取消注释 `gamma_ambiguous` 后重新编译，Cython 会报「Invalid use of fused types」之类的错误。

**预期结果**：实参为 `double` 时自动特化成功；无类型提示的 Python 对象实参无法特化、编译失败。这一步纯粹验证编译期能否解析符号，**不涉及性能**。

**结果是否运行**：待本地验证（本环境未执行编译）。

#### 4.1.5 小练习与答案

**练习 1**：若想把 `gamma` 用于复数输入，`cimport` 侧该如何写？
**答案**：把实参声明为 `cdef double complex z`，调用 `gamma(z)`，编译器即选 `Dd_number_t = double_complex` 特化；或写 `gamma[double complex](z)`。

**练习 2**：为什么 `cython_special.pyi` 只有一句 `def __getattr__(name) -> Any: ...`，却仍能被 `cimport`？
**答案**：`cimport` 读的是 `.pxd`（声明文件），与 `.pyi`（Python 静态类型桩，给 IDE/`mypy` 看）无关。`.pyi` 之所以这么「空」，是因为这套类型化接口是为 Cython 设计的，从 Python 侧直接调用本就不是它的主要用途。

### 4.2 nogil 热循环:在释放 GIL 的循环里逐标量调用

#### 4.2.1 概念说明

「热循环」指那种执行次数极多、单次计算又轻的循环，它的总耗时由「循环体里的每次调用」累积而成。把特殊函数调用塞进 `with nogil:` 块有两个好处：一是循环体不再持有 GIL，可以被多线程并行执行；二是每次调用是一次直接的 C 函数调用，没有 Python 方法分派、没有元组装箱、没有错误信号跨语言桥接的开销。

但 `nogil` 块有一条铁律：**块内绝不能触碰 Python 对象**。这恰好与 cython_special 全员 `noexcept nogil` 的设计严丝合缝——它的函数体本身就不碰 Python 对象（出错只返回 `nan`，不发 Python 告警）。

#### 4.2.2 核心流程

在 nogil 循环里调用 cython_special，按返回值个数分两种写法：

- **单返回值函数**（如 `gamma`、`erf`、`voigt_profile`）：直接赋值即可。
  ```cython
  with nogil:
      for i in range(n):
          out[i] = gamma(x[i])
  ```
- **多返回值函数**（如 `airy` 返回 4 个值、`sici` 返回 2 个值）：在 `.pxd` 中它们是 `cdef void`，输出走末尾的**指针参数**。需要先声明 `cdef` 局部变量，再传地址：
  ```cython
  cdef double Ai, Aip, Bi, Bip
  with nogil:
      for i in range(n):
          airy(x[i], &Ai, &Aip, &Bi, &Bip)
          out0[i], out1[i], out2[i], out3[i] = Ai, Aip, Bi, Bip
  ```

注意：多返回值函数在 `.pxd` 里是 `cdef`（非 `cpdef`），意味着它们没有 Python 包装、不能从 Python 直接调用；若需从 Python 触达，源码另提供了 `_xxx_pywrap` 这类 `def` 包装（见 4.2.3）。

#### 4.2.3 源码精读

**单返回值的最简形态**——`voigt_profile` 直接转发 C++ 内核：

[cython_special.pyx:L1708-L1714](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1708-L1714)

```cython
cpdef double voigt_profile(double x0, double x1, double x2) noexcept nogil:
    """See the documentation for scipy.special.voigt_profile"""
    return xsf_voigt_profile(x0, x1, x2)

cpdef double agm(double x0, double x1) noexcept nogil:
    """See the documentation for scipy.special.agm"""
    return special_agm(x0, x1)
```

它们参数和返回值都是确定类型 `double`（非融合类型），在 nogil 循环里调用零歧义、零开销。

**多返回值的指针输出形态**——`airy`：

[cython_special.pyx:L1716-L1748](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1716-L1748)

```cython
cdef void airy(Dd_number_t x0, Dd_number_t *y0, Dd_number_t *y1,
               Dd_number_t *y2, Dd_number_t *y3) noexcept nogil:
    ...
    if Dd_number_t is double:
        special_airy(x0, y0, y1, y2, y3)
    elif Dd_number_t is double_complex:
        special_cairy(... &tmp3)
        y0[0] = _complexstuff.double_complex_from_npy_cdouble(tmp0)
        ...

def _airy_pywrap(Dd_number_t x0):
    cdef Dd_number_t y0, y1, y2, y3
    airy(x0, &y0, &y1, &y2, &y3)
    return y0, y1, y2, y3
```

要点有二：(1) `cdef void` + 指针输出，是因为 C 只能返回单值、且 `nogil` 下无法构造 Python 元组；(2) 紧跟其后的 `_airy_pywrap` 是 `def`（持有 GIL 的 Python 函数），它先在栈上声明四个标量、传地址给 `airy`，再把结果打包成 Python 元组返回——这是「让多输出函数能被 Python 触达」的桥。在你自己的 nogil 循环里，你扮演的是 `_airy_pywrap` 内部那段：声明标量、传指针、取结果，但**不构造元组**、**不释放 nogil**。

**错误语义**在模块文档串里写得很清楚：

[cython_special.pyx:L25-L26](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L25-L26) 明确：cython_special 版本「只能通过返回 `nan` 表示错误，无法像 `scipy.special` 里的对应函数那样发出告警」。原因是发告警需要拿 GIL 并调用 Python 对象（见 u7-l1 的 `sf_error_v`），这与 `nogil` 契约冲突。因此在 nogil 循环里，你需要在循环结束后自行检查输出中是否出现 `nan`，以此判断是否有元素触发 domain/overflow 等错误。

#### 4.2.4 代码实践

**实践目标**：在一个真正的 `nogil` 循环里对一万（可调大）个 double 调用 `gamma`，确认它能编译、能运行、结果与 ufunc 一致。

新建 `gamma_bench.pyx`（**示例代码**）：

```cython
# cython: boundscheck=False, wraparound=False, cdivision=True
# cython: language_level=3
from scipy.special.cython_special cimport gamma

def gamma_loop_cython(double[::1] x, double[::1] out):
    cdef Py_ssize_t i, n = x.shape[0]
    with nogil:
        for i in range(n):
            # x[i] 是 double，gamma 自动特化为 double 版本
            out[i] = gamma(x[i])
```

> 说明：`double[::1]` 是 typed memoryview，要求传入 C 连续的 `float64` 数组。`x[i]` 的静态类型是 `double`，编译器据此为 `gamma` 选 `Dd_number_t = double` 特化。整个 `for` 循环在 `nogil` 区内，是纯 C 循环。

驱动脚本 `run_bench.py`（**示例代码**）：

```python
import timeit
import numpy as np
from scipy import special
from gamma_bench import gamma_loop_cython

x = np.linspace(0.1, 5.0, 10_000)
out = np.empty_like(x)

# 正确性
gamma_loop_cython(x, out)
ref = special.gamma(x)
print("max abs diff:", np.max(np.abs(out - ref)))

# 计时（注意：循环里每轮都要重新写一遍 out，公平对比）
n_repeat = 1000
t_cython = timeit.timeit(lambda: gamma_loop_cython(x, out), number=n_repeat)
t_ufunc  = timeit.timeit(lambda: special.gamma(x),       number=n_repeat)
print(f"cython_special loop: {t_cython:.4f}s")
print(f"ufunc batch        : {t_ufunc:.4f}s")
```

**需要观察的现象**：`max abs diff` 应接近 0（两者调用同一批 C 内核，结果一致）；两路计时都应远小于「Python 层 for 循环逐个调用 `special.gamma(x[i])`」。

**预期结果**：正确性通过；cython_special 循环与 ufunc 批量调用的吞吐量在同一量级（具体谁快见 4.3 的分析）。

**结果是否运行**：待本地验证（本环境未执行编译与计时）。

#### 4.2.5 小练习与答案

**练习 1**：把 `gamma_loop_cython` 改成同时调用 `airy` 计算 4 个输出，写出循环体。
**答案**：
```cython
cdef double Ai, Aip, Bi, Bip
with nogil:
    for i in range(n):
        airy(x[i], &Ai, &Aip, &Bi, &Bip)
        out0[i] = Ai; out1[i] = Aip; out2[i] = Bi; out3[i] = Bip
```
其中 `airy` 需 `from scipy.special.cython_special cimport gamma, airy`，四个 `out*` 是独立的 `double[::1]` memoryview。

**练习 2**：为什么不能在 `with nogil:` 块内直接 `print(out[i])` 或给一个 Python 列表 `lst.append(out[i])`？
**答案**：`print` 与 `list.append` 都需要操作 Python 对象、因而需要 GIL，与 `nogil` 契约冲突，Cython 会在编译期拒绝。nogil 块内只能做纯 C 操作。

### 4.3 性能权衡:cython_special 标量调用 vs ufunc 批量调用

#### 4.3.1 概念说明

很多初学者会想当然地认为「cython_special 在 nogil 循环里调用一定比 ufunc 快」。这是一个需要纠正的直觉。关键事实是：**`special.gamma(arr)` 的 ufunc 路径本身就是一个纯 C 写的逐元素内层循环**（回顾 u2-l1 与 u3-l2 的 `generate_loop`），它并非「对每个元素回到 Python 调用一次」。所以对一个「孤立的特殊函数批量求值」任务，ufunc 与 cython_special 循环都在跑 C 级循环，吞吐量通常在同一量级，谁更快取决于具体函数与数据规模，没有定论。

真正让 cython_special 胜出的，不是「单函数批量求值」，而是下面这些 ufunc 做不到或做不好的场景。

#### 4.3.2 核心流程:何时选哪个

| 场景 | 选 ufunc | 选 cython_special + nogil |
| --- | --- | --- |
| 只是对一个数组求某特殊函数 | ✅ 首选，简洁 | 不必要 |
| 特殊函数调用嵌在**更大的 Cython/nogil 计算核**里 | 需跨 Python 边界、产生中间数组 | ✅ 内联调用，无中间数组 |
| 需要 OpenMP/`prange` 多线程并行逐元素 | ufunc 不易控制线程粒度 | ✅ 可与 `cython.parallel.prange` 配合 |
| 需要函数发出 domain/overflow 告警 | ✅ 支持（见 u2-l3、u7） | ❌ 只能返回 nan |
| 需要批量输入、自动广播、`out=` | ✅ ufunc 通用能力 | ❌ 需自己写循环与边界检查 |

一句话选型：**「数据已经在 NumPy 数组里、只想求个函数」用 ufunc；「数据在一个更大的 nogil 计算流的中间、不想为了调一次特殊函数就回到 Python 或分配临时数组」用 cython_special。**

#### 4.3.3 源码精读

为什么 ufunc 路径「已经很快」？因为它的内层循环是代码生成器在编译期用纯 C 生成的。回顾 [cython_special.pyx:L1104-L1125](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1104-L1125) 的导入区：cython_special 与 _ufuncs 共用同一批 C/C++ 内核符号（`xsf_gamma`、`special_airy`、`cephes_igam` 等）。换言之，**两条路径最终调用的是同一个数值内核**，差异只在「循环壳子」：ufunc 的壳子是 NumPy 的 `PyUFunc` 机制（一次性处理整个数组、可广播），cython_special 的壳子是你自己写的 `for` 循环。

而 cython_special 的真正优势——「避免中间数组」——可以这样理解：假设你的算法是 `out[i] = gamma(a[i]) + jv(0, b[i])`。
- 用 ufunc：要分配两个临时数组 `g = special.gamma(a)` 和 `j = special.jv(0, b)`，再相加，**三次数组遍历 + 两次临时内存**。
- 用 cython_special + nogil：在一个循环里 `out[i] = gamma(a[i]) + jv[double](0.0, b[i])`，**一次遍历、零临时内存**。当数组很大或这步在更深处被反复调用时，省下的内存带宽与分配开销才是 cython_special 的核心价值。

错误语义的差异同样源于实现：[cython_special.pyx:L25-L26](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L25-L26) 写明 cython_special 不能发告警；而 ufunc 路径经由 [cython_special.pyx:L1112-L1117](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1112-L1117) 的 `wrap_PyUFunc_getfperr` 捕获浮点异常并桥接到 `sf_error`（u7-l2 详述），从而能发出 `SpecialFunctionWarning`。这是选型表中「需要告警 → 选 ufunc」的根源。

#### 4.3.4 代码实践

**实践目标**：用 4.2.4 的基准脚本，亲自测出「孤立批量 gamma」时两路的真实耗时，并理解为何差距不大；再构造一个「嵌在更大计算里」的例子体会 cython_special 的优势。

**操作步骤**：

1. 先运行 4.2.4 的 `run_bench.py`，记录 `t_cython` 与 `t_ufunc`。
2. 再加一组「复合计算」对比。在 `gamma_bench.pyx` 中追加（**示例代码**）：

```cython
from scipy.special.cython_special cimport gamma, jv

# 路径 A：nogil 单循环，无中间数组
def fused_nogil(double[::1] a, double[::1] b, double[::1] out):
    cdef Py_ssize_t i, n = a.shape[0]
    with nogil:
        for i in range(n):
            out[i] = gamma(a[i]) + jv[double](0.0, b[i])
```

3. 在 `run_bench.py` 里对比 `fused_nogil(a, b, out)` 与「先 `special.gamma(a)` 再 `special.jv(0, b)` 再相加」的 ufunc 写法的耗时与峰值内存（可用 `tracemalloc` 观察）。

**需要观察的现象**：
- 第 1 步：`t_cython` 与 `t_ufunc` 通常同量级，差距不会是一个数量级。
- 第 3 步：nogil 单循环写法**不分配两个大临时数组**，`tracemalloc` 下峰值内存更低；当 `n` 很大时（如百万级），这种内存带宽优势会转化为可测的时间优势。

**预期结果**：印证「孤立单函数 → 两者差不多」「复合 nogil 计算 → cython_special 省内存、可能更快」的结论。

**结果是否运行**：待本地验证。请把你机器上的真实数字填回本讲对应位置，作为日后选型依据。

#### 4.3.5 小练习与答案

**练习 1**：若任务只是 `special.gamma(np.linspace(...))`，有人建议「改用 cython_special 会更快」，你如何回应？
**答案**：不必要。ufunc 路径内部已是 C 级逐元素循环，与 cython_special 同源同内核，孤立批量求值两者同量级；cython_special 的优势在「嵌在更大 nogil 计算里、避免中间数组」，而非单函数批量。

**练习 2**：为什么在 cython_special 的 nogil 循环里，遇到 `gamma(-1.0)`（应为 domain error）不会抛 `SpecialFunctionError`，而是悄悄返回 `nan`？
**答案**：因为发告警/抛异常需要拿 GIL 并操作 Python 对象（`sf_error_v` 的机制，见 u7-l1），与 `noexcept nogil` 契约冲突；cython_special 选择了「只返回 nan、不发信号」。所以循环结束后需自行用 `np.isnan(out)` 复查。

## 5. 综合实践

把本讲三块内容串起来，完成一个最小的「数值积分加速核」：

**任务**：用 Gauss-Legendre 求积近似计算 \(\int_{0}^{1} \Gamma(x)\,\mathrm{d}x\)，其中被积函数含特殊函数 `gamma`。

要求：

1. 用 `scipy.special.roots_legendre(N)` 拿到节点 `x` 与权重 `w`（回顾 u5-l1）。
2. 写一个 Cython 核 `gl_integrand_gamma(double[::1] x, double[::1] w, double a, double b)`：
   - 在 `with nogil:` 循环里，把节点从 \([-1,1]\) 仿射变换到 \([a,b]\)；
   - 调 `from scipy.special.cython_special cimport gamma` 算 \(\Gamma\)；
   - 累加 \(\sum_i w_i\,f(\text{变换后的 } x_i)\)，返回该 double。
3. 与纯 NumPy 写法（`np.sum(w * special.gamma(变换后的 x))`）对比结果一致性，并比较 `n_repeat` 次的耗时与内存。
4. 在循环外用 `np.isnan` 检查是否有节点落到 `gamma` 的定义域外（本例节点在 \((0,1)\)，应无 nan，但养成检查习惯）。

**评价要点**：结果应一致；由于这里只有一次求和、且 `gamma` 是「嵌在变换+加权求和」的更大计算里，cython_special 写法可避免分配 `gamma(变换后x)` 这个临时数组——这正是它的典型用武之地。

> 本综合实践为示例任务，编译与数值结果待本地验证。

## 6. 本讲小结

- `cimport scipy.special.cython_special` 是编译期导入：它读取随包安装的 `.pxd`（meson.build L231 已确保安装），把 `noexcept nogil` 的 C 函数声明引入你的 `.pyx`。
- 融合类型函数（如 `gamma` 用 `Dd_number_t`）在调用时必须被「特化」：要么用确定类型的 `cdef` 变量让编译器推断，要么用 `gamma[double](x)` 显式特化，否则编译报错。
- 单返回值函数直接赋值；多返回值函数（`cdef void` + 指针输出）需先声明栈标量、传地址取回，循环内不构造 Python 元组。
- `nogil` 块内只能做纯 C 操作；cython_special 全员 `nogil` 且出错误只返回 `nan`（不发告警），所以循环后需自查 `isnan`。
- 性能真相：ufunc 路径内部已是 C 级逐元素循环、与 cython_special 同源同内核，「孤立单函数批量」两者同量级；cython_special 的真正优势是「嵌在更大 nogil 计算里、避免中间数组与 Python 边界往返」，并可配合 `prange` 多线程。
- 选型口诀：数据已在数组里只求个函数 → ufunc；在更大的 nogil 计算流中间调特殊函数 → cython_special。

## 7. 下一步学习建议

- **本讲给出的「nogil + gamma」并不能发告警**，下一步建议学习 **u7-l1（sf_error 的 C 层贯通）** 与 **u7-l2（_ufuncs_extra_code.pxi 的 FPE 检测）**，搞懂 ufunc 路径如何隔着 GIL 把硬件浮点异常桥接成 `SpecialFunctionWarning`，从而理解两条路径在错误语义上的根本差异。
- 想看 cython_special 调用的那些 C/C++ 内核（`xsf_gamma`、`special_airy`）长什么样，进入 **u8-l1（xsf 与 xsf_wrappers）** 与 **u8-l2（boost_special_functions.h）**，理解 `extern "C"` 与复数桥接如何把 C++ 库暴露给这里的 Cython 层。
- 若你对「逐元素并行」感兴趣，可自行在 4.3 的实践中把 `for i in range(n)` 改成 `for i in prange(n, nogil=True)`（需 `from cython.parallel cimport prange` 并在编译时打开 OpenMP），体会 cython_special 与多线程的天然契合——这是 ufunc 难以精细控制的领域。

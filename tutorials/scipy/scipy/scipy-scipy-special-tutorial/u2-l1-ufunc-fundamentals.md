# NumPy ufunc 基石：类型签名、广播与 `out` 参数

## 1. 本讲目标

u1-l1 已经建立一个核心结论：`scipy.special` 里**几乎所有函数都是 NumPy 通用函数（ufunc）**。但「是 ufunc」到底意味着什么？为什么我们写 `special.erf(np.array([0, 1, 2]))` 能直接拿到一个同形状的数组，而不用自己写 `for` 循环？为什么 `special.erf(1j)`（复数输入）不会报错，反而自动切到一套复数实现？这些问题的答案，都藏在 ufunc 的**类型签名、广播规则与 `out=`/`where=` 机制**里。

学完本讲，你应该能够：

- 说清 ufunc 与普通 Python 函数的本质区别：ufunc 是「按类型分发、逐元素求值、可批量」的 C 级对象，而不是一段 Python 代码。
- 读懂 ufunc 的**类型签名**字符串，例如 `d->d`（一个 double 进、一个 double 出）、`dddD->D`（三个 double 加一个复数 double 进、一个复数 double 出），并理解 NumPy 如何根据输入 dtype **自动选择**对应的内核实现（「多类型分发」）。
- 掌握 ufunc 的三大通用能力：**广播**（broadcasting）、`out=`（写入预分配数组）、`where=`（掩码选择），以及「必然逐元素」的含义。
- 学会从类型桩 [`_ufuncs.pyi`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi) 读取任意一个 ufunc 的参数与返回类型。

本讲精读 [`_ufuncs.pyi`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi) 与 [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py) 中的 ufunc 契约段，并辅以类型码的权威定义 [`_generate_pyx.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py) 与 ufunc 内核注册 [`_special_ufuncs.cpp`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_special_ufuncs.cpp) 作为佐证。

> 一句话定位：ufunc 是 `scipy.special` 一切便利的来源——它让你用「写标量函数的语法」得到「自动广播、自动选类型、可批量、可写回」的数组行为，而这些行为全部由 NumPy 在 C 层统一提供，`special` 只负责把数学内核「挂」上去。

## 2. 前置知识

- **NumPy 数组（`ndarray`）与 dtype**：数组是同类型元素的连续内存块；`dtype` 描述元素类型，如 `float64`（双精度浮点）、`complex128`（双精度复数）。本讲反复出现的类型，就是 dtype 在 ufunc 层的简写。
- **逐元素（element-wise）**：对一个数组「逐元素」求值，意思是「对每个位置独立套用同一个函数」。例如 `np.sqrt([4, 9, 16]) == [2, 3, 4]`。ufunc 的本质就是「在 C 层把逐元素循环跑得飞快」。
- **ufunc**（NumPy universal function）：NumPy 里一类特殊的 C 对象（`numpy.ufunc` 类型）。它和普通 Python 函数最大的区别是：它内部注册了**多套**「输入类型 → 输出类型」的实现（称为 *loops*），NumPy 会根据你传入的实际 dtype 自动挑一套来执行，并自动处理广播和逐元素循环。`+`、`*`、`np.sin` 这些都是 ufunc。
- **`.pyi` 类型桩（stub）**：一个只写「函数签名、不写实现」的文件，给类型检查器（mypy / pyright）和 IDE 用。`_ufuncs.pyi` 就是 `_ufuncs` 这个编译扩展模块的「身份证」——因为真正的 `_ufuncs` 是 `.so` 共享库，没有可直接阅读的 `.py` 源码，类型桩补上了「它导出哪些名字、各是什么类型」的信息（见 u1-l4）。
- **承接 u1-l1 / u1-l4**：你已经知道 `special` 命名空间是拼装出来的，绝大多数函数住在 `_ufuncs` 扩展模块里。本讲就钻进这些函数「作为 ufunc」的那一面。

> 名词速查：**loop（循环/类型环）**指 ufunc 针对某一种具体 dtype 组合注册的一段 C 实现。一个 ufunc 通常挂多个 loop，例如 `erf` 挂了 4 个 loop（float、double、complex float、complex double 各一个）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲怎么用它 |
|------|------|--------------|
| [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py) | 包入口。顶部文档字符串给出「几乎所有函数都是 ufunc」的**行为契约** | 4.1 论证 ufunc 是默认情况、例外才警告 |
| [`_ufuncs.pyi`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi) | `_ufuncs` 扩展模块的**类型桩**；逐个声明 `erf: np.ufunc`、`hyp2f1: np.ufunc` | 4.4 全程主战场：用静态类型描述动态 ufunc |
| [`_generate_pyx.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py) | 代码生成器。其中 `CY_TYPES`/`C_TYPES`/`TYPE_NAMES` 三张表是**类型码的权威定义**；顶部注释给出签名语法 | 4.2 解释 `f`/`d`/`F`/`D` 等单字符类型码 |
| [`_special_ufuncs.cpp`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_special_ufuncs.cpp) | C++ 层用 `xsf::numpy::ufunc` 注册 ufunc 的源文件；每条注册语句列出了该函数挂的**全部 loop** | 4.2 / 4.3 给出 `erf`、`hyp2f1` 的真实 loop 注册，作为 `.types` 输出的来源 |
| [`functions.json`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/functions.json) | 声明式 ufunc 签名表（另一条注册路径） | 4.3 用 `sici` 的多输出签名演示 ufunc 可有多个输出 |

> 说明：`_generate_pyx.py`、`_special_ufuncs.cpp`、`functions.json` 这三条「注册路径」的完整机制属于 u3 / u8 单元；本讲**只借用它们来回答两个具体问题**——「类型码是什么意思」「`.types` 里那些字符串从哪来」，不展开代码生成细节。

## 4. 核心概念与源码讲解

### 4.1 为什么 special 的函数「几乎都是 ufunc」

#### 4.1.1 概念说明

如果你自己用纯 Python 写一个 `def erf(x): ...`，那么 `erf(1.0)` 能算，但 `erf(np.array([0,1,2]))` 通常得在函数体里手动循环。而 `scipy.special` 的函数不需要——你传标量它算标量，传数组它算数组，传形状不同的多个数组它会自动对齐。这种「一次定义、到处批量」的能力，正是 ufunc 带来的。

更关键的是，ufunc 是一个 **C 级对象**（`numpy.ufunc` 的实例），不是 Python 函数。它内部捆绑了若干段高度优化的 C 循环，由 NumPy 调度。这意味着：

- **快**：逐元素循环跑在 C 层，没有 Python 解释器开销。
- **统一**：广播、类型提升、`out=`、`where=` 等行为由 NumPy 一处实现，所有 special 函数免费共享。
- **可组合**：因为遵守共同的协议，它们能和 NumPy 的其它 ufunc（`+`、`*`、`np.exp`…）无缝混用。

#### 4.1.2 核心流程

ufunc 的执行流程可以概括为三步：

1. **收集输入**：把所有输入参数转成 `ndarray`（标量也被包成 0 维数组），并确定它们的 dtype。
2. **类型分发（type resolution）**：NumPy 根据输入的 dtype 组合，从该 ufunc 注册的多个 loop 里挑出最合适的一个。例如全是 `float64` 就走 double loop，出现复数就走 complex loop。如果没有任何 loop 精确匹配，NumPy 会尝试**类型提升**（把输入提升到某个 loop 能接受的更宽类型）。
3. **广播 + 逐元素循环**：把所有输入按广播规则对齐到同一形状，然后用选中的 loop 逐元素求值，结果写进输出数组。

第 2 步是本讲的重点之一：「多类型分发」让同一个 `erf` 既能算实数又能算复数，而你完全不用关心切换。

#### 4.1.3 源码精读

- [`__init__.py:13-19`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L13-L19)：模块开篇的行为契约。原文要点：

  > Almost all of the functions below accept NumPy arrays as input arguments as well as single numbers. This means they follow broadcasting and automatic array-looping rules. **Technically, they are NumPy universal functions.**

  这段话明确：**「是 ufunc」是默认情况**，凡是不接受数组的例外，会在文档小节里单独警告（见 u1-l4 的 `lmbda` 例子）。本讲研究的正是这个「默认情况」的内部机理。

- [`_ufuncs.pyi:341`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L341)：`erf: np.ufunc`——类型桩把 `erf` 的类型明确标成 `np.ufunc`，这是「erf 是 ufunc」在源码层面的白纸黑字。同列还有 `gamma: np.ufunc`（[:375](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L375)）、`hyp2f1: np.ufunc`（[:394](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L394)）、`airy: np.ufunc`（[:292](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L292)）、`jv: np.ufunc`（[:415](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L415)）等。

> 旁证：u1-l4 讲过 `from ._ufuncs import *` 把这些 ufunc 灌进 `special` 命名空间（[`__init__.py:788-789`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L788-L789)）。所以 `special.erf`、`special.gamma` 与 `_ufuncs.erf`、`_ufuncs.gamma` 是同一个 ufunc 对象。

#### 4.1.4 代码实践

- **实践目标**：亲手确认 `special` 里的函数真的是 `np.ufunc` 实例，并体会「标量与数组同源」。
- **操作步骤**：
  ```python
  import numpy as np
  import scipy.special as sc

  # 1) 确认 ufunc 身份
  print(type(sc.erf))                 # 应为 numpy.ufunc
  print(isinstance(sc.erf, np.ufunc)) # True
  print(isinstance(sc.hyp2f1, np.ufunc))  # True

  # 2) 标量输入 → 标量输出（被包成 numpy 标量）
  print(sc.erf(1.0))                  # 0.8427007929497149

  # 3) 数组输入 → 同形状数组（逐元素）
  print(sc.erf(np.array([0.0, 0.5, 1.0])))
  ```
- **需要观察的现象**：`type(sc.erf)` 打印出 `numpy.ufunc`，而不是 `<class 'function'>`；对数组调用得到同形状数组，无需自己写循环。
- **预期结果**：三处 `isinstance(...) == True`；`erf` 数组结果约 `[0.0, 0.5205, 0.8427]`。
- **运行结果**：待本地验证（具体打印数值以本地环境为准；`isinstance` 判定为 `True` 是确定的）。

#### 4.1.5 小练习与答案

**练习 1**：`special.logsumexp`（来自 `_logsumexp.py`）是 ufunc 吗？如何用一行代码判断？

> **答案**：通常**不是**。`isinstance(sc.logsumexp, np.ufunc)` 返回 `False`。`logsumexp` 是纯 Python 包装函数（见 u4-l3），它内部调用 ufunc，但自身不是 ufunc——这正是「几乎所有」而非「全部」的含义。

**练习 2**：为什么 `special` 选择把数学函数做成 ufunc，而不是写成「先 `np.asarray` 再 `for` 循环」的纯 Python 函数？

> **答案**：ufunc 把逐元素循环下沉到 C 层，速度快几个数量级；同时广播、类型分发、`out=` 等能力由 NumPy 统一提供，避免每个函数各自重写一遍。

---

### 4.2 ufunc 的类型系统：类型码与多类型分发

#### 4.2.1 概念说明

每个 ufunc 都有一组「类型环（loop）」，每个 loop 对应**一种具体的输入/输出 dtype 组合**。NumPy 用**单字符类型码**来紧凑地描述它们。理解这套类型码，是读懂 `.types` 输出、预判类型分发结果的关键。

`scipy.special` 的类型码沿用 NumPy 约定，并在生成器 [`_generate_pyx.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py) 里有明确的三张对照表。**「多类型分发」**就是：同一个函数（如 `erf`）针对 float、double、complex float、complex double 各注册一个 loop，运行时按输入 dtype 自动选用。

#### 4.2.2 核心流程

类型码与含义（依据 `_generate_pyx.py` 的 `CY_TYPES`/`C_TYPES`/`TYPE_NAMES` 三表）：

| 类型码 | Cython 类型 | NumPy 枚举 | 对应 Python/NumPy dtype |
|--------|-------------|------------|--------------------------|
| `f` | `float` | `NPY_FLOAT` | `float32`（单精度） |
| `d` | `double` | `NPY_DOUBLE` | `float64`（双精度，最常用） |
| `g` | `long double` | `NPY_LONGDOUBLE` | `longdouble`（平台相关） |
| `F` | `float complex` | `NPY_CFLOAT` | `complex64` |
| `D` | `double complex` | `NPY_CDOUBLE` | `complex128`（双精度复数） |
| `G` | `long double complex` | `NPY_CLONGDOUBLE` | `clongdouble` |
| `i` | `int` | `NPY_INT` | `int32`（多数平台） |
| `l` | `long` | `NPY_LONG` | `long` |
| `p` | `Py_ssize_t` | `NPY_INTP` | `intp`（指针宽度整数） |

> 大小写区分实数与复数：`d` 是实数双精度，`D` 是复数双精度；`f`/`F`、`g`/`G` 同理。`special` 里 `d` 和 `D` 是绝对主力。

**类型签名字符串**的语法（依据 `_generate_pyx.py:12-19` 的签名语法注释）：

```
<name> ':' <input类型码串> '*' <output类型码串> '->' <retval类型码> '*' <被忽略的retval>
```

其中 `*output` 部分用于**多输出**函数（见 4.3），单输出函数常常省略 `*`，只剩 `<input>-><retval>`。在 ufunc 对外暴露的 `.types` 属性里，我们看到的是更简洁的 `<input>-><output>` 形式，例如：

- `d->d`：1 个 double 输入 → 1 个 double 输出（如 `erf` 的实数 loop）。
- `D->D`：1 个复数 double 输入 → 1 个复数 double 输出（如 `erf` 的复数 loop）。
- `dddd->d`：4 个 double 输入 → 1 个 double 输出（如 `hyp2f1` 的实数 loop）。
- `dddD->D`：3 个 double + 1 个复数 double → 1 个复数 double（如 `hyp2f1` 在「最后一个参数为复数」时的提升 loop）。

**类型分发的规则**：NumPy 会按 loop 在列表中的顺序寻找「能精确接受（或经类型提升后接受）当前输入」的第一个 loop。

#### 4.2.3 源码精读

类型码的权威定义在生成器里，三张表一一对应：

- [`_generate_pyx.py:339-350`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L339-L350)：`CY_TYPES`，把类型码映射到 Cython 类型（`'d': 'double'`、`'D': 'double complex'` 等）。
- [`_generate_pyx.py:352-363`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L352-L363)：`C_TYPES`，映射到 NumPy 的 C 类型（`'d': 'npy_double'`）。
- [`_generate_pyx.py:365-374`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L365-L374)：`TYPE_NAMES`，映射到 NumPy 的枚举常量（`'d': 'NPY_DOUBLE'`），这些常量就是注册 loop 时告诉 NumPy「这是哪种 dtype」用的。

签名字符串的语法说明则在文件顶部：

- [`_generate_pyx.py:12-19`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L12-L19)：明确规定 `<input> '*' <output> '->' <retval> '*' <ignored_retval>` 的语法——这是 4.3 多输出讲解的依据。

真实的 loop 注册在 C++ 层。`erf` 挂了 4 个 loop：

- [`_special_ufuncs.cpp:577-580`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_special_ufuncs.cpp#L577-L580)：`xsf::numpy::ufunc({f_f, d_d, F_F, D_D}, "erf", ...)`——即 `f->f`、`d->d`、`F->F`、`D->D` 四个 loop。命名约定 `X_Y` 表示「输入 X 类型、输出 Y 类型」。所以 `special.erf.types` 会列出这四种组合，且**支持复数输入**。

`hyp2f1`（4 输入）挂的 loop 更能体现「类型提升分发」：

- [`_special_ufuncs.cpp:722-726`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_special_ufuncs.cpp#L722-L726)：`{ffff_f, dddd_d, fffF_F, dddD_D}`。前两个是纯实数 loop（4 个 float → float、4 个 double → double）；后两个是「前 3 个实数 + 第 4 个复数」的混合 loop（`fffF->F`、`dddD->D`）。这意味着：当 `hyp2f1(a,b,c,z)` 的 `z` 是复数而 `a,b,c` 是实数时，NumPy 会自动选 `dddD->D` loop，结果升级为复数。

> 对照：并非所有函数都有复数 loop。例如 `exprel`（[`_special_ufuncs.cpp:559-561`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_special_ufuncs.cpp#L559-L561)）只挂了 `f_f`、`d_d` 两个实数 loop，给它传复数就会因找不到匹配 loop而报错。**「某函数支不支持复数」取决于它注册了哪些 loop，可从 `.types` 一眼看出。**

#### 4.2.4 代码实践

- **实践目标**：打印 `erf` 与 `hyp2f1` 注册的全部 loop，并验证复数输入会触发类型分发。
- **操作步骤**：
  ```python
  import numpy as np
  import scipy.special as sc

  # 1) erf 的全部类型环
  print("erf.types   =", sc.erf.types)
  print("erf.nin     =", sc.erf.nin, " erf.nout =", sc.erf.nout)

  # 2) hyp2f1 的全部类型环
  print("hyp2f1.types=", sc.hyp2f1.types)
  print("hyp2f1.nin  =", sc.hyp2f1.nin, " hyp2f1.nout =", sc.hyp2f1.nout)

  # 3) 类型分发：实数 vs 复数
  r_real = sc.hyp2f1(1.0, 1.0, 1.0, 0.5)      # 4 个实数 -> 走 dddd->d
  r_cplx = sc.hyp2f1(1.0, 1.0, 1.0, 0.5 + 0j) # z 为复数 -> 走 dddD->D
  print(r_real, type(r_real))   # float64
  print(r_cplx, type(r_cplx))   # complex128
  ```
- **需要观察的现象**：`erf.types` 含 `'D->D'`（支持复数）；`hyp2f1.types` 含 `'dddD->D'`（混合提升 loop）；把 `z` 从 `0.5` 换成 `0.5+0j` 后，结果 dtype 从 `float64` 变成 `complex128`。
- **预期结果**：
  - `erf.types == ('f->f', 'd->d', 'F->F', 'D->D')`
  - `hyp2f1.types == ('ffff->f', 'dddd->d', 'fffF->F', 'dddD->D')`
  - `erf.nin == 1, erf.nout == 1`；`hyp2f1.nin == 4, hyp2f1.nout == 1`
  - `r_real` 为 `numpy.float64`（值约 `2.0`），`r_cplx` 为 `numpy.complex128`。
- **运行结果**：`.types` 的字符串内容与顺序由源码注册顺序决定（见上文引用的 cpp 行），可确定；具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`special.erf.types` 里有 `'g->g'`（long double）吗？为什么？

> **答案**：没有。`_special_ufuncs.cpp:577-580` 只注册了 `f/d/F/D` 四个 loop，不含 `g`/`G`。绝大多数 special 函数只覆盖到 double 与 complex double，long double 支持很少。

**练习 2**：若某 ufunc 的 `.types` 既无 `'D->D'` 也无任何含 `D` 的 loop，给它传复数数组会发生什么？

> **答案**：NumPy 找不到匹配 loop、且无法把复数「安全地」提升为某个实数 loop，于是抛出 `TypeError: No matching signature found`。这就是上文 `exprel` 传复数会报错的原因。

---

### 4.3 广播、`out=`、`where=` 与「必然逐元素」

#### 4.3.1 概念说明

「是 ufunc」还带来三项通用能力，它们对 `special` 里**所有** ufunc 一视同仁：

1. **广播（broadcasting）**：多个输入数组形状不同时，NumPy 按广播规则把它们对齐到同一形状再逐元素求值。规则简述：从末尾维逐维对齐，某维为 1（或缺失）则复制扩展到与另一操作数相同。
2. **`out=`**：把结果**直接写入**一个预分配数组，而不是新建数组返回。对单输出 ufunc，`out` 是一个数组；对多输出 ufunc（如 `airy`、`sici`），`out` 是一个**数组元组**。
3. **`where=`**：用一个布尔掩码选择「在哪些位置真正计算」，其余位置保持原值（通常配合 `out=` 使用）。

「**必然逐元素**」是指：ufunc 不做任何「跨元素」的聚合或重排，每个输出位置只依赖同位置的输入。这与 `logsumexp`（沿某轴求和，跨元素）形成对比——后者因此**不是** ufunc（见 4.1.5 练习 1）。

#### 4.3.2 核心流程

**广播**的三个典型场景（以 `special.jv(v, z)` 为例，`v` 是阶数、`z` 是自变量）：

```
jv(0,        z_1d)            # 标量 v 广播到 z 的每个位置
jv(v_1d_col, z_1d_row)        # (n,1) 与 (m,) 广播成 (n,m)
jv(v_2d,     z_2d)            # 同形状，逐元素
```

**`out=`** 的用法：

```
out = np.empty(3)
sc.erf([0.0, 0.5, 1.0], out=out)   # 结果写进 out，而非新建数组
```

**多输出**的 `out=`（`sici` 返回 `(Si, Ci)` 两个数组）：

```
si = np.empty(3); ci = np.empty(3)
sc.sici([0.5, 1.0, 2.0], out=(si, ci))   # out 是元组
```

为什么 `special` 会有多输出 ufunc？因为很多特殊函数天然同时返回一组相关量：`airy` 返回 Ai、Aip、Bi、Bip 共 4 个；`sici` 返回 Si、Ci 共 2 个。把它们做成**一个多输出 ufunc**，比做成多个独立函数更高效（底层只算一次公共中间量），也更符合数学上的「成组」关系。

#### 4.3.3 源码精读

- [`functions.json:466-471`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/functions.json#L466-L471)：`sici` 的签名 `"xsf_csici": "D*DD->*i"` 与 `"xsf_sici": "d*dd->*i"`。按 [`_generate_pyx.py:12-19`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L12-L19) 的语法解析 `D*DD->*i`：
  - 输入：`D`（1 个复数 double，即自变量 `z`）；
  - `*` 分隔出输出：`DD`（2 个复数 double，即 `Si`、`Ci`）；
  - `->` 后是 retval：为空；
  - `*i`：被忽略的整数返回值（C 内核返回的状态/错误码，不作为 ufunc 输出）。
  
  这就从源码层面解释了 `sici.nin == 1`、`sici.nout == 2`，以及为什么 `out=` 要传一个二元组。

- [`_ufuncs.pyi:292`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L292) 与 [:374](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L374)：`airy: np.ufunc`、`fresnel: np.ufunc` 都是多输出 ufunc（`airy` 返回 4 个，`fresnel` 返回 2 个）。类型桩里它们和单输出 ufunc 写法一致，**多输出语义不在类型层体现**——这是 `.pyi` 的局限（见 4.4.5）。

- 关于「不是 ufunc」的反例：`_ufuncs.pyi` 里**没有** `logsumexp`、`jn_zeros`、`lmbda` 这些名字（它们来自 `_logsumexp.py`、`_basic.py`，是纯 Python 函数）。这印证了「多输出/批量虽好，但跨元素聚合或返回序列的函数做不到 ufunc」。

> 数学注记——广播即张量外积的退化形式。对两个形状分别为 \((n,1)\) 与 \((1,m)\) 的输入，广播结果形状为 \((n,m)\)，相当于在 \((n,m)\) 网格的每个点 \((i,j)\) 上独立求值 \(\mathrm{J}_v(z)\)：\[ y_{ij} = J_{v_i}(z_j). \] 整个过程不涉及任何跨 \(i\) 或跨 \(j\) 的求和，因此天然适合 ufunc 的逐元素模型。

#### 4.3.4 代码实践

- **实践目标**：用 `jv` 体验广播；用 `erf` 体验 `out=`/`where=`；用 `sici`/`airy` 体验多输出。
- **操作步骤**：
  ```python
  import numpy as np
  import scipy.special as sc

  # --- 广播 ---
  v = np.array([0.0, 1.0, 2.0])[:, None]   # 形状 (3,1)
  z = np.array([0.5, 1.0, 1.5, 2.0])[None, :]  # 形状 (1,4)
  J = sc.jv(v, z)                            # 广播成 (3,4)
  print(J.shape)                             # (3, 4)

  # --- out= 写入预分配数组 ---
  out = np.empty(3)
  ret = sc.erf([0.0, 0.5, 1.0], out=out)
  print(ret is out)                          # True（就地写入）

  # --- where= 掩码选择 ---
  buf = np.full(3, -1.0)
  sc.erf([0.0, 0.5, 1.0], out=buf, where=[False, True, True])
  print(buf)                                 # [-1. , 0.5205..., 0.8427...]

  # --- 多输出 ufunc ---
  si, ci = sc.sici([0.5, 1.0, 2.0])          # 返回两个数组
  print(sc.sici.nout)                        # 2
  Ai, Aip, Bi, Bip = sc.airy([0.0, 1.0])     # 返回四个数组
  print(sc.airy.nout)                        # 4
  ```
- **需要观察的现象**：`jv(v, z)` 自动得到 `(3,4)` 结果；`erf(..., out=out)` 返回的对象与 `out` 是同一个（`ret is out` 为 `True`）；`where=` 为 `False` 的位置保留了 `buf` 原值 `-1.0`；`sici` 返回 2 个、`airy` 返回 4 个数组。
- **预期结果**：`J.shape == (3, 4)`；`ret is out == True`；`buf == [-1.0, 0.5205..., 0.8427...]`；`sici.nout == 2`、`airy.nout == 4`。
- **运行结果**：待本地验证（具体数值以本地为准；形状、`nout`、`ret is out` 等结构性结论是确定的）。

#### 4.3.5 小练习与答案

**练习 1**：`sc.erf(np.array([[1,2],[3,4]]), out=buf)` 中，`buf` 必须满足什么条件？

> **答案**：`buf` 必须是形状与广播后结果一致（`(2,2)`）、dtype 能容纳 `float64` 结果的数组（通常 `float64`）。若形状不符会抛 `ValueError`。

**练习 2**：为什么 `logsumexp` 不能做成 ufunc，而 `erf` 可以？

> **答案**：`erf` 是纯逐元素（每个输出只依赖同位置输入），符合 ufunc 模型；`logsumexp` 需要沿某轴做**跨元素求和**（\(\log\sum_i e^{x_i}\)），这违背 ufunc「必然逐元素」的契约，因此只能用普通函数实现（内部再调用 ufunc）。

---

### 4.4 类型桩 `_ufuncs.pyi`：用静态类型描述动态 ufunc

#### 4.4.1 概念说明

`_ufuncs` 是编译出的共享库（`.so`），没有可读的 `.py` 源码。但类型检查器（mypy/pyright）和 IDE 需要「这个模块导出哪些名字、各是什么类型」的信息——这就是 [`_ufuncs.pyi`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi) 的作用：它是一份**类型桩（stub）**，只写签名、不写实现。

`_ufuncs.pyi` 做了三件事：

1. 用一个 `__all__` 列表声明 `_ufuncs` 对外公开的名字（这是 `__init__.py` 里 `__all__` 四路聚合的最大一路，见 u1-l4）。
2. 用 `def geterr(...) / seterr(...) / class errstate` 描述错误控制三件套（详见 u2-l3）。
3. 用大量 `名字: np.ufunc` 声明每一个特殊函数都是 ufunc。

#### 4.4.2 核心流程

阅读 `_ufuncs.pyi` 的方法：

1. **找名字**：在文件里搜 `erf`、`hyp2f1` 等，看它是否被声明为 `np.ufunc`。若在 `_ufuncs.pyi` 里出现且标注为 `np.ufunc`，说明它是注册在 `_ufuncs` 扩展里的真 ufunc。
2. **区分公开与私有**：以下划线开头的（`_lambertw`、`_spherical_jn` 等，[:268](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L268)、[:282](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L282)）是**内部 ufunc**，被 `_lambertw.py`、`_spherical_bessel.py` 等薄包装引用（见 u4-l4），不在 `__all__` 里；不带下划线的是公开 ufunc。
3. **认清局限**：`.pyi` 只告诉你「它是个 ufunc」，**不**告诉你它的 loop 列表、输入个数、是否多输出。这些动态信息要在运行时用 `.nin`、`.nout`、`.types` 查询（见 4.2.4）。

#### 4.4.3 源码精读

- [`_ufuncs.pyi:1-3`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L1-L3)：桩文件导入 `numpy as np`，因为所有函数都标注成 `np.ufunc`。
- [`_ufuncs.pyi:5-243`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L5-L243)：`__all__` 列表，逐行列出 `_ufuncs` 公开的所有名字。这份清单既被 `from ._ufuncs import *` 用作白名单，又被 [`__init__.py:825`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L825) 拼进 `special.__all__`。
- [`_ufuncs.pyi:245-256`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L245-L256)：`geterr`/`seterr`/`errstate` 的静态签名（错误处理是 u2-l3 的主题）。
- [`_ufuncs.pyi:258-290`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L258-L290)：内部 ufunc，如 `_lambertw: np.ufunc`、`_spherical_jn: np.ufunc`、`_stirling2_inexact: np.ufunc` 等——它们是「货架背后的半成品」，供包装层使用。
- [`_ufuncs.pyi:291-524`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L291-L524)：公开 ufunc 区，从 `agm: np.ufunc`（[:291](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L291)）到 `zetac: np.ufunc`（[:524](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L524)）。其中 `erf` 在 [:341](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L341)、`hyp2f1` 在 [:394](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L394)。

> 一处重要的「静态信息缺失」：`.pyi` 把 `airy`、`sici`、`fresnel` 这些**多输出** ufunc 也只写成 `airy: np.ufunc`，没有体现「它返回 4 个值」。因此类型检查器并不知道 `si, ci = sc.sici(x)` 合法——这是动态 ufunc 信息无法静态表达的固有局限，运行时只能靠 `.nout` 查询。

#### 4.4.4 代码实践

- **实践目标**：用 `_ufuncs.pyi` 作为「目录」，回答「某函数是不是 `_ufuncs` 里的公开 ufunc」。
- **操作步骤**：
  ```python
  # 1) 在源码里查 _ufuncs.pyi 是否含某名字（也可直接 grep 该文件）
  import scipy.special as sc, numpy as np

  for name in ["erf", "hyp2f1", "logsumexp", "jn_zeros", "airy"]:
      obj = getattr(sc, name, None)
      in_ufuncs = name in sc._ufuncs.__all__   # 是否登记在 _ufuncs 公开清单
      print(f"{name:12} is np.ufunc={isinstance(obj, np.ufunc)!s:5} in _ufuncs.__all__={in_ufuncs}")
  ```
  并在终端执行只读检索（不修改任何文件）：
  ```bash
  grep -n -E '^\s*(erf|hyp2f1|airy|logsumexp|jn_zeros): ' scipy/special/_ufuncs.pyi
  ```
- **需要观察的现象**：`erf`、`hyp2f1`、`airy` 三项 `is np.ufunc=True` 且 `in _ufuncs.__all__=True`；`logsumexp`、`jn_zeros` 两项 `is np.ufunc=False` 且不在 `_ufuncs.__all__`。`grep` 只会命中 `erf/hyp2f1/airy` 三行，证实它们来自 `_ufuncs.pyi`。
- **预期结果**：表格如下——

  | name | is np.ufunc | in `_ufuncs.__all__` |
  |------|-------------|----------------------|
  | erf | True | True |
  | hyp2f1 | True | True |
  | airy | True | True |
  | logsumexp | False | False |
  | jn_zeros | False | False |

- **运行结果**：结构性判定（是否 ufunc、是否在清单）确定；待本地验证打印细节。

#### 4.4.5 小练习与答案

**练习 1**：`_lambertw`（带下划线）出现在 `_ufuncs.pyi`，但 `special._lambertw` 为什么不能直接用？

> **答案**：`_lambertw` 是内部 ufunc，不在 `_ufuncs.__all__`（公开清单只列不带下划线的名字）。它被 [`_lambertw.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_lambertw.py) 里的 `lambertw` 薄包装引用并做参数预处理后，才以 `special.lambertw` 的公开身份暴露（见 u4-l4）。

**练习 2**：`_ufuncs.pyi` 写 `airy: np.ufunc`，但没写它有 4 个输出。如果你想写 `Ai, Aip, Bi, Bip = sc.airy(x)`，类型检查器会接受吗？

> **答案**：静态层面，`np.ufunc` 的调用签名约定返回单个数组，类型检查器通常**无法**确认 4 路解包合法（可能报「解包数量不符」之类警告）。这是 `.pyi` 表达力的局限；运行时则完全正确，因为 `airy.nout == 4`。多输出语义只能靠运行时 `.nout` 或文档获知。

---

## 5. 综合实践

把本讲四块知识串成一个「ufunc 体检脚本」，对一个你选定的函数（建议 `hyp2f1`）做全面体检：

1. **身份确认**：`isinstance(sc.hyp2f1, np.ufunc)` → 应为 `True`。
2. **签名体检**：打印 `sc.hyp2f1.types`、`sc.hyp2f1.nin`、`sc.hyp2f1.nout`，逐条用中文解释每个类型环（如 `dddD->D` 表示「前 3 参数 double、第 4 参数复数 double、输出复数 double」），并对照 [`_special_ufuncs.cpp:722-726`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_special_ufuncs.cpp#L722-L726) 确认注册来源。
3. **类型分发实验**：构造 4 个 `float64` 输入与「第 4 个换成 `complex128`」两组调用，比较返回值 dtype，说明 NumPy 分别走了 `dddd->d` 与 `dddD->D` 哪个 loop。
4. **广播实验**：让前 3 个参数为标量、第 4 个为形状 `(2,3)` 的数组，验证返回形状也是 `(2,3)`。
5. **`out=` 实验**：预分配一个 `(2,3)` 的 `float64` 数组，用 `out=` 写入，确认返回对象与该数组是同一个。
6. **静态 vs 动态对照**：在 [`_ufuncs.pyi`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L394) 里找到 `hyp2f1: np.ufunc`，指出 `.pyi` **没有**告诉你但运行时 `.types`/`.nin`/`.nout` **能**告诉你的信息（loop 列表、输入个数）。

> 预期：你会清楚地看到「`.pyi` 给静态身份，`.types` 给动态能力，源码 cpp/json 给注册来源」三者如何互补地描述同一个 ufunc。

## 6. 本讲小结

- `scipy.special` 里**几乎所有函数都是 `np.ufunc`**——这是 [`__init__.py:13-19`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L13-L19) 写明的默认契约，例外才在文档小节单独警告。
- ufunc 用**单字符类型码**（`f`/`d`/`g` 实数，`F`/`D`/`G` 复数，`i`/`l`/`p` 整数，定义见 [`_generate_pyx.py:339-374`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_generate_pyx.py#L339-L374)）描述 loop，`.types` 返回 `d->d`、`dddD->D` 这类「输入→输出」字符串。
- **多类型分发**让 `erf`/`hyp2f1` 等根据输入 dtype 自动选 loop（[`_special_ufuncs.cpp:577-580`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_special_ufuncs.cpp#L577-L580)、[:722-726](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_special_ufuncs.cpp#L722-L726)）；实数输入走 `d` 系、复数输入走 `D` 系。
- ufunc 三大通用能力由 NumPy 统一提供：**广播**、`out=`（多输出时传元组，如 `sici` 见 [`functions.json:466-471`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/functions.json#L466-L471)）、`where=`；且**必然逐元素**，因此 `logsumexp` 这类跨元素函数做不成 ufunc。
- [`_ufuncs.pyi`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi) 是编译扩展模块的**类型桩**，给出静态身份（`名字: np.ufunc`）与 `__all__` 公开清单；但 loop 列表、输入个数、是否多输出等动态信息只能在运行时用 `.types`/`.nin`/`.nout` 查询。

## 7. 下一步学习建议

- **横向（同层）**：u2-l2 会用本讲建立的「`.types` / 分类」视角，建立 250+ 函数的**家族分类地图**；u2-l3 会进入与 ufunc 配套的**错误处理**机制（`seterr`/`geterr`/`errstate`，其静态签名已在 [`_ufuncs.pyi:245-256`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L245-L256) 出现）。
- **纵向（下钻）**：想搞清「类型码和 loop 是怎么变成可运行 ufunc 的」，请进入 u3 单元——u3-l1 讲 `functions.json` 的声明结构，u3-l2 讲 `_generate_pyx.py` 如何生成 Cython ufunc 注册代码，u8-l3 讲 `_special_ufuncs.cpp` 这条纯 C++ 注册路径。
- **源码延伸阅读**：动手 grep [`_special_ufuncs.cpp`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_special_ufuncs.cpp) 里 `xsf::numpy::ufunc` 的其它注册语句，挑 2 个函数预测它们的 `.types`，再到 Python 里验证——这是巩固本讲最快的办法。

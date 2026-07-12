# ndarray 初体验与核心属性

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `ndarray` 是什么、它为什么是 NumPy 的核心对象；
- 自己动手创建一个 `ndarray`，并读懂它的 `shape`、`ndim`、`dtype`、`strides`、`flags` 等属性；
- 从 `np.ndarray` 这个 Python 名字一路追到它真正的来源——C 扩展模块 `_multiarray_umath`；
- 理解每个属性在 C 层是如何被「算出来」的（即 `getset.c` 里的属性 getter）；
- 用一句话解释 `strides` 数组里每个数字的物理含义。

本讲只读三个文件就能把上述问题讲透：`numpy/_core/multiarray.py`、`numpy/_core/numeric.py`、`numpy/_core/__init__.py`，并下钻到 C 层的 `numpy/_core/src/multiarray/getset.c`。

## 2. 前置知识

在正式开始前，先用最朴素的语言建立两个直觉。

**第一，什么是「N 维数组」。** Python 自带的 `list` 可以装任意对象，但代价是每个元素都要单独当作 Python 对象来管理，既慢又费内存。NumPy 的 `ndarray`（N-dimensional array）则要求**同一块连续内存里只存同一种类型的数据**（比如全是 64 位浮点数）。这样 CPU 可以成片地、可预测地读取数据，从而跑得飞快。「N 维」指的是这块内存可以被解释成 1 维、2 维、3 维……的逻辑形状，比如一张灰度图是 2 维 `(高, 宽)`，一段视化的体数据是 3 维。

**第二，为什么要区分「逻辑形状」和「物理内存」。** 数据在内存里永远是一维的字节串，但同一个一维字节串可以用不同的 `shape` 去解释，甚至可以「跳着读」（这就是 `strides`）。理解这一点，是理解后续切片、转置、广播都不需要复制数据的关键。

**本讲承接的旧知识。** 在「u1-l3 顶层目录结构与模块导出」中我们已经知道：`_core` 是用 C 实现的底层核心，`np.` 命名空间里的对象大多是从 `_core` 再导出上来的。本讲就要追其中最重要的一类对象——`ndarray`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用来回答什么 |
|------|------|------------------|
| `numpy/_core/multiarray.py` | 纯 Python 的「桥接层」，把 C 扩展 `_multiarray_umath` 里的对象/函数重新导出，并补上一些 dispatcher 包装 | `ndarray`、`array`、`asarray`、`empty`、`zeros` 这些名字到底从哪来 |
| `numpy/_core/numeric.py` | 纯 Python 实现的一批常用函数，如 `ones`、`full`、`identity`、`astype`、`roll` 等 | 「创建函数」是怎么用底层 `empty` + 填充组合出来的 |
| `numpy/_core/__init__.py` | `_core` 包的入口，负责把 `multiarray`、`numeric` 等子模块汇聚起来 | `np.ndarray` 在导入链上的中转站 |
| `numpy/_core/src/multiarray/getset.c` | C 层的「属性读写表」（getset），定义 `ndarray` 上每个属性的取值/赋值函数 | `arr.shape`、`arr.strides`、`arr.dtype`、`arr.ndim` 在 C 层是怎么算出来的 |

> 小提示：在 NumPy 源码里，凡是名字以下划线开头的模块（如 `_core`、`_multiarray_umath`）都是「私有」的，官方建议用户直接用 `np.` 命名空间，而不是去 `import numpy._core.xxx`。我们读源码时才需要钻进这些私有模块。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **ndarray 类的来源**：追 `np.ndarray` 的导入链；
2. **常用数组创建函数**：看 `array` / `asarray` / `empty` 与 `ones` / `full` 的分工；
3. **核心属性与 C 层 getset**：看 `shape` / `strides` / `dtype` / `ndim` 在 C 层如何实现。

---

### 4.1 ndarray 类的来源：从 np.ndarray 追到 C 层

#### 4.1.1 概念说明

当你在 Python 里写下 `np.ndarray` 或 `type(np.array([1,2,3]))` 时，你拿到的 `ndarray` **并不是一个用 Python 写的类**。它是一个用 C 语言定义、然后注册到 Python 解释器里的「内置类型」。这样做有两个根本好处：

- **性能**：创建数组、读写元素都走 C 代码，没有 Python 解释器的逐行开销；
- **直接操控内存**：C 可以直接申请一块裸内存（`malloc`），并让数组对象指向它，这是 `strides`、`flags` 这些机制的物理基础。

但用户日常只会接触到 Python 名字 `np.ndarray`。所以 NumPy 做了一件事：**在 C 扩展模块 `_multiarray_umath` 里定义好 `ndarray` 类型，再通过一系列「再导出（re-export）」把它一路搬运到顶层的 `np.` 命名空间**。本节就来追这条链。

#### 4.1.2 核心流程

`np.ndarray` 的导入链可以这样画：

```text
np.ndarray
   ↑  (numpy/__init__.py: from ._core import ...)
numpy._core.ndarray
   ↑  (numpy/_core/__init__.py: from .numeric import * ，
        而 numeric 等子模块的符号最初来自 multiarray)
numpy._core.multiarray.ndarray
   ↑  (numpy/_core/multiarray.py: from ._multiarray_umath import *)
numpy._core._multiarray_umath.ndarray   ← 真正用 C 定义的类型
```

关键点：

1. **真正的定义在 C 扩展 `_multiarray_umath` 里**（编译产物，源码在 `numpy/_core/src/multiarray/` 下的若干 `.c` 文件）。
2. **`multiarray.py` 只是个「搬运工」**：它 `from ._multiarray_umath import *`，把 C 扩展里的 `ndarray`、`array`、`empty` 等名字原样暴露出来，再加上一个 `__all__` 列表声明「这些是公开的」。
3. **`_core/__init__.py` 做汇聚**：它导入 `multiarray`、`numeric` 等子模块，并用 `from .numeric import *` 等语句把它们的名字合并到 `_core` 这一层的命名空间。
4. **顶层 `numpy/__init__.py` 做最终再导出**：`from ._core import (...)` 把 `_core` 里的名字搬到 `np.` 下。

这条链上的每一跳都只是「换一个名字指向同一个对象」，因此 `np.ndarray is numpy._core.multiarray.ndarray` 在运行时为 `True`（待本地验证）。

#### 4.1.3 源码精读

**第一跳：`multiarray.py` 从 C 扩展拿符号。** 整个文件的开头注释就点明了它的「桥接」定位——把旧的 `multiarray`/`umath` 两个 C 扩展合并后的 `_multiarray_umath` 重新拼出旧命名空间：

[numpy/_core/multiarray.py:L1-L12](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L1-L12) —— 模块开头说明它是「为向后兼容而创建的命名空间」，并用 `from ._multiarray_umath import *` 把 C 扩展里的对象全部搬进来。

紧接着的 `__all__` 列表里，明确写着 `'ndarray'`、`'array'`、`'asarray'`、`'empty'`、`'zeros'`、`'dtype'`、`'nditer'` 等名字，这些都是直接来自 C 扩展的对象：

[numpy/_core/multiarray.py:L30-L50](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L30-L50) —— `__all__` 中能看到 `'ndarray'`、`'array'`、`'asarray'`、`'empty'`、`'zeros'`、`'dtype'`、`'nditer'` 等核心符号。

注意这一句尤其重要：

[numpy/_core/multiarray.py:L59-L66](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L59-L66) —— `array.__module__ = 'numpy'` 等赋值，把 `array`、`asarray`、`empty` 等函数的 `__module__` 「伪装」成 `numpy`，这样交互式帮助和 pickle 仍认为它们属于顶层 `numpy`，而不是私有的 `_multiarray_umath`。

**第二跳：`_core/__init__.py` 汇聚子模块。** 这里有两句关键的导入：

[numpy/_core/__init__.py:L23-L25](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L23-L25) —— `from . import multiarray`，并在导入失败时给出详细的「C 扩展没编译好」的诊断信息。

[numpy/_core/__init__.py:L119](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L119) —— `from .numeric import *`，把 `numeric.py` 里的一批纯 Python 函数（`ones`、`full`、`identity` 等）合并进 `_core` 命名空间。注意 `_core/__init__.py` 自己**没有**定义 `ndarray`，它只是把 `multiarray` 和 `numeric` 提供的名字凑齐。

**第三跳：顶层 `numpy/__init__.py` 再导出到 `np.`。**

[numpy/__init__.py:L119-L148](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L119-L148) —— `from ._core import (...)` 把 `_core` 里的名字（包括 `array`、`asarray` 等）逐一搬到顶层 `numpy` 命名空间。

至此，`np.ndarray`、`np.array` 才真正可用，而它们的「本体」始终停留在 C 扩展 `_multiarray_umath` 中。

#### 4.1.4 代码实践

**实践目标**：验证「`np.ndarray` 经过多跳再导出，最终指向同一个 C 类型对象」。

**操作步骤**：

1. 在装好 NumPy 的环境里启动 `python`；
2. 依次执行下面的断言；
3. 观察是否全部为 `True`。

```python
import numpy as np

# 追踪 np.ndarray 的「本体」
print(type(np.array([1, 2, 3])))          # <class 'numpy.ndarray'>
print(np.ndarray)                          # <class 'numpy.ndarray'>

# 验证多跳再导出指向同一个对象
print(np.ndarray is np._core.multiarray.ndarray)          # 预期 True
print(np.ndarray is np._core._multiarray_umath.ndarray)   # 预期 True

# 看 ndarray 真正的「出身」
print(np.ndarray.__module__)   # 预期 numpy._core._multiarray_umath（C 扩展名）
```

**需要观察的现象**：第三条断言为 `True`，说明 Python 名字 `np.ndarray` 和 C 扩展里的 `ndarray` 是同一个对象；`__module__` 显示 `numpy._core._multiarray_umath`，证明类型本体来自 C 扩展。

**预期结果**：两条 `is` 断言为 `True`；`__module__` 为 `numpy._core._multiarray_umath`。若你用的是非常旧的 NumPy 版本（< 1.16，那时 multiarray 和 umath 尚未合并），路径名会有所不同——本讲以当前仓库版本为准，结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 NumPy 要费力气用 `array.__module__ = 'numpy'` 把函数的模块名「伪装」成 `numpy`？

**参考答案**：因为这些函数的本体来自私有 C 扩展 `_multiarray_umath`，但用户的代码、文档示例、交互式帮助里都用 `np.array`。把 `__module__` 设成 `numpy`，能让帮助文本（`np.array?`）和 `pickle` 序列化（保存的是 `numpy.array` 这个公开路径）都指向稳定、公开的名字，避免把私有的 `_multiarray_umath` 路径泄露给用户。

**练习 2**：如果 `_core/__init__.py` 里 `from . import multiarray` 失败了（即 C 扩展没编出来），会发生什么？

**参考答案**：`_core/__init__.py` 不会直接崩溃，而是抛出一段很长的、面向用户的诊断信息（提示「C 扩展导入失败」并列出排查链接）。这正是 [numpy/_core/__init__.py:L23-L85](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L23-L85) 那一大段 `except ImportError` 的用意。

---

### 4.2 常用数组创建函数

#### 4.2.1 概念说明

NumPy 提供了一大批创建数组的函数，按「数据从哪来」可以分成三类：

| 类别 | 代表函数 | 实现位置 |
|------|----------|----------|
| 从已有数据构造 | `array`、`asarray`、`asanyarray` | C 扩展（`_multiarray_umath`），经 `multiarray.py` 再导出 |
| 分配未初始化/填充内存 | `empty`、`zeros` | C 扩展 |
| 在纯 Python 里组合底层函数 | `ones`、`full`、`identity` | `numeric.py` |

理解这种分工很重要：**`array`/`asarray`/`empty`/`zeros` 是「底层原语」，用 C 实现，速度最快；`ones`/`full` 这类则是「在 Python 里调用底层原语再加工」**。比如 `ones` 其实是「先 `empty` 申请内存，再把每个元素填成 1」。

#### 4.2.2 核心流程

以 `np.ones((3,4))` 为例，纯 Python 层的执行过程：

```text
np.ones(shape, dtype=None, order='C')
   ├─ 若给了 like= 参数  → 委托给 like 对象的同名函数
   └─ 否则：
        ├─ a = empty(shape, dtype, order)   # 底层 C：分配未初始化内存
        └─ multiarray.copyto(a, 1, casting='unsafe')  # 把 1 广播填进 a
        → 返回 a
```

也就是说，`ones` 本身不直接碰内存，它复用了 `empty` + `copyto` 两个底层能力。这种「薄封装」是 `numeric.py` 里很多函数的共同风格。

#### 4.2.3 源码精读

先确认 `array`、`asarray`、`empty`、`zeros` 都是从 C 扩展再导出的——它们出现在 `multiarray.py` 的 `__all__` 里：

[numpy/_core/multiarray.py:L36-L42](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L36-L42) —— `__all__` 中包含 `'arange'`、`'array'`、`'asarray'`、`'asanyarray'`、`'empty'`、`'zeros'` 等，这些都是 C 扩展直接提供的函数（`numeric.py` 里**没有**同名定义）。

然后看 `ones` 这个纯 Python 实现的典型代表：

[numpy/_core/numeric.py:L172-L234](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4a60e/numpy/_core/numeric.py#L172-L234) —— `ones` 的实现体。注意最后几行：先调用 `empty(shape, dtype, order, ...)` 拿到一块未初始化内存，再用 `multiarray.copyto(a, 1, casting='unsafe')` 把标量 `1` 广播填进去。

聚焦核心两步的实现：

[numpy/_core/numeric.py:L227-L234](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L227-L234) —— `ones` 函数体的核心：`empty` 分配 + `copyto` 填充。

同样的「`empty` + 填充」模式也出现在 `full` 中：

[numpy/_core/numeric.py:L325-L392](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L325-L392) —— `full(shape, fill_value, ...)`，用指定值填满整个数组。

还有创建单位矩阵的 `identity`：

[numpy/_core/numeric.py:L2181-L2222](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L2181-L2222) —— `identity(n, ...)`，生成对角线为 1、其余为 0 的方阵。

另外提一个与「类型」相关的纯 Python 函数 `astype`（注意它和 `ndarray.astype` 方法是配套的 Array API 风格函数）：

[numpy/_core/numeric.py:L2615-L2616](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L2615-L2616) —— `astype(x, dtype, /, *, copy=True, ...)`，把数组复制并转换到指定 dtype。

#### 4.2.4 代码实践

**实践目标**：体会「`ones` = `empty` + 填充」这一模式，并验证它和底层结果一致。

**操作步骤**：

```python
import numpy as np
from numpy._core import multiarray, numeric

# 方式 A：用高层函数 np.ones
a = np.ones((3, 4))

# 方式 B：手动复现 ones 的内部两步
b = np.empty((3, 4))            # 未初始化内存（内容随机）
multiarray.copyto(b, 1, casting='unsafe')  # 填入 1

print(a)
print(b)
print(np.array_equal(a, b))     # 预期 True
```

**需要观察的现象**：`b` 在 `copyto` 之前打印会是「乱码」（未初始化内存的残留值），`copyto` 之后与 `a` 完全相等。

**预期结果**：`np.array_equal(a, b)` 为 `True`，证明 `ones` 内部确实是 `empty` + `copyto`。`empty` 阶段的残留值随平台而异，属正常现象（待本地验证其具体数值）。

#### 4.2.5 小练习与答案

**练习 1**：`np.array([1,2,3])` 和 `np.asarray([1,2,3])` 的关键区别是什么？

**参考答案**：`array` 默认总是会创建一个**新**数组（默认 `copy=True`）；`asarray` 则在输入已经是合适 dtype 的 `ndarray` 时**不复制**，直接返回原对象，因此常用于「把输入规整成数组、但能省内存就省」的场景。二者都是 C 扩展函数（见 `multiarray.py` 的 `__all__`）。

**练习 2**：为什么 `np.empty` 打印出来常常是一堆奇怪的数，而不是 0？

**参考答案**：`empty` 只调用底层分配内存，**不初始化**，内存里保留的是上一次使用留下的任意字节。这是为了速度——如果你马上就会覆写每个元素，就不必浪费时间去先清零。`numeric.py` 里 `ones`/`full` 紧跟在 `empty` 后面用 `copyto` 覆盖，正是因为单独的 `empty` 内容不可信。

---

### 4.3 核心属性与 C 层 getset

#### 4.3.1 概念说明

每个 `ndarray` 都带有一组「属性（attribute）」，它们是数组的「元信息」：形状、步长、数据类型、维度数等。在 Python 里你只需 `arr.shape` 就能取到，但这些属性**不是普通的 Python 实例变量**，而是由 C 层的「属性表（getset）」动态计算返回的。

用一个比喻：`ndarray` 的 C 结构体里存的是「原始字段」（比如指向 `dimensions` 数组、`strides` 数组的指针）。你在 Python 里看到的 `arr.shape`，是 C 层的一个 getter 函数**在访问时**把那个指针数组读出来、转换成一个 Python 元组再返回给你的。这张「属性名 ↔ getter/setter 函数」的对照表，就定义在 `getset.c` 的 `array_getsetlist[]` 数组里。

本节的几个核心属性含义如下：

| 属性 | 含义 | 单位/类型 |
|------|------|-----------|
| `ndim` | 维度数（轴的个数） | 整数 |
| `shape` | 每个轴的长度 | 整数元组 |
| `dtype` | 元素的数据类型 | `dtype` 对象 |
| `strides` | 沿每个轴前进「一个元素」需要跨过的**字节数** | 整数元组 |
| `itemsize` | 单个元素占多少**字节** | 整数 |
| `size` | 元素总数 = `shape` 各项之积 | 整数 |
| `nbytes` | 数据占多少字节 = `size * itemsize` | 整数 |
| `flags` | 内存布局标志（是否 C/F 连续、是否可写等） | `flags` 对象 |
| `T` | 转置视图（二维即矩阵转置） | `ndarray` |

#### 4.3.2 核心流程

**strides 的数学含义**（本讲的重点）。设数组 `ndim = n`，`shape = (d_0, d_1, ..., d_{n-1})`，`itemsize = s`（每个元素的字节数）。对**C 连续**（行主序）数组，从最后一轴往前递推：

\[
\mathrm{strides}[n-1] = s
\]

\[
\mathrm{strides}[i] = \mathrm{strides}[i+1] \cdot d_{i+1} \quad (i = n-2, n-3, \dots, 0)
\]

直观理解：在 C 连续布局里，最右边的轴（最后一维）相邻元素紧挨着，所以跨一个元素就是 \(s\) 个字节；往左每升一轴，就要「跨过」右边那一整维的所有元素，于是 strides 乘上对应维的长度。

给定一个多维下标 \((i_0, i_1, \dots, i_{n-1})\)，元素在内存中的字节偏移为：

\[
\mathrm{offset}(i_0,\dots,i_{n-1}) = \sum_{k=0}^{n-1} i_k \cdot \mathrm{strides}[k]
\]

这就是 NumPy 用 `strides` 实现「任意形状、任意步长」访问的统一公式——转置、切片、广播之所以常常不需要复制数据，本质上都只是**改写 `shape` 和 `strides` 两个小数组**，数据本身不动。

**flags 的关键位**：

- `C_CONTIGUOUS`：数据按 C（行主序）连续存放；
- `F_CONTIGUOUS`：数据按 Fortran（列主序）连续存放；
- `OWNDATA`：数组自己拥有这块内存（不是别人的视图）；
- `WRITEABLE`：可写；
- `ALIGNED`：数据起始地址按类型对齐。

**getset 的工作机制**。Python 在 `type` 对象里维护一张 `tp_getset` 表，每一项是 `{名字, getter, setter, doc, closure}`。当你访问 `arr.shape`，解释器查表找到 `array_shape_get` 并调用它。`getset.c` 末尾的 `array_getsetlist[]` 就是这张表。

#### 4.3.3 源码精读

**ndim 的 getter**——直接读 C 结构体里的 `nd` 字段并包成 Python 整数：

[numpy/_core/src/multiarray/getset.c:L37-L41](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L37-L41) —— `array_ndim_get` 调用 `PyArray_NDIM(self)` 返回维度数。这印证了「属性是动态算出来的」：每次访问 `arr.ndim` 都会执行这个小函数。

**shape 的 getter**——把 C 里的 `dimensions` 指针数组转成 Python 整数元组：

[numpy/_core/src/multiarray/getset.c:L49-L53](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L49-L53) —— `array_shape_get` 用 `PyArray_IntTupleFromIntp(PyArray_NDIM(self), PyArray_DIMS(self))` 把内部 `dimensions` 数组转成 Python 元组返回。

**strides 的 getter**——和 shape 完全对称，只是读的是 `strides` 数组：

[numpy/_core/src/multiarray/getset.c:L129-L133](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L129-L133) —— `array_strides_get` 用同样的 `PyArray_IntTupleFromIntp` 把内部 `strides` 数组转成元组返回。注意 `strides` 的单位是**字节**，这正是 4.3.2 公式里 \(s\)（itemsize）能直接乘进去的原因。

**dtype 的 getter**——返回内部 `descr`（描述符）指针，增加引用计数后交出：

[numpy/_core/src/multiarray/getset.c:L222-L227](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L222-L227) —— `array_descr_get` 取出 `PyArray_DESCR(self)`，`Py_INCREF` 后返回，对应 Python 层的 `arr.dtype`。

**T（转置）的 getter**——调用 C 函数 `PyArray_Transpose` 生成转置视图：

[numpy/_core/src/multiarray/getset.c:L731-L735](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L731-L735) —— `array_transpose_get` 调用 `PyArray_Transpose(self, NULL)`，它返回一个**新视图**（共享数据，只翻转 `shape` 和 `strides`），这正是 `arr.T` 不复制数据的原因。

最后看「对照表」本身，它把上面所有 getter 绑定到 Python 属性名：

[numpy/_core/src/multiarray/getset.c:L743-L825](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L743-L825) —— `array_getsetlist[]` 数组：每一项形如 `{"shape", (getter)array_shape_get, (setter)array_shape_set, NULL, NULL}`，把名字 `shape` 绑到 getter/setter 函数。`ndim`/`flags`/`shape`/`strides`/`data`/`itemsize`/`size`/`nbytes`/`base`/`dtype`/`real`/`imag`/`flat`/`ctypes`/`T`/`mT`/`device` 等都在这里登记。

> 读这张表能学到一件实用技巧：想知道 `ndarray` 到底有哪些「真正的」属性，与其查文档，不如直接读 `array_getsetlist[]`——它就是权威清单。

#### 4.3.4 代码实践（本讲主实践任务）

**实践目标**：创建一个 `3x4` 的 `float64` 数组，打印它的核心属性，并用自己的话解释 `strides` 数组。

**操作步骤**：

```python
import numpy as np

a = np.arange(12, dtype=np.float64).reshape(3, 4)

print("a      =", a)
print("shape  =", a.shape)     # 预期 (3, 4)
print("ndim   =", a.ndim)      # 预期 2
print("dtype  =", a.dtype)     # 预期 float64
print("itemsize =", a.itemsize)# 预期 8（字节）
print("size   =", a.size)      # 预期 12
print("nbytes =", a.nbytes)    # 预期 96（= 12 * 8）
print("strides =", a.strides)  # 预期 (32, 8)
print("flags  =")
print(a.flags)
```

**需要观察的现象**：

- `strides` 是 `(32, 8)`；
- `flags` 里 `C_CONTIGUOUS = True`、`F_CONTIGUOUS = False`、`OWNDATA = True`（或取决于 reshape 实现，见下方说明）、`WRITEABLE = True`。

**预期结果与解释（一句话解释 strides）**：

> `strides = (32, 8)` 的含义是：在内存中，**沿第 0 轴（行）前进一个元素要跨过 32 个字节（即一整行 4 个 float64 = 4×8 字节），沿第 1 轴（列）前进一个元素要跨过 8 个字节（即一个 float64 的字节数）**。

用 4.3.2 的公式核验：`itemsize s = 8`，`shape = (3, 4)`，C 连续时

\[
\mathrm{strides}[1] = s = 8, \qquad \mathrm{strides}[0] = \mathrm{strides}[1] \cdot d_1 = 8 \times 4 = 32
\]

与程序输出 `(32, 8)` 完全一致。

> 关于 `OWNDATA`：`reshape` 在能不复制时返回**视图**（此时 `OWNDATA` 可能为 `False`，`base` 指向 `arange` 的原数组）；若你改用 `np.array(np.arange(12).reshape(3,4))` 直接构造，则 `OWNDATA = True`。具体取决于 NumPy 对该形状能否就地重排，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：把上面的数组转置成 `a.T`，打印它的 `shape` 和 `strides`，并解释为什么转置「不需要复制数据」。

**参考答案**：`a.T.shape = (4, 3)`，`a.T.strides = (8, 32)`——即原 `strides` 翻转。因为转置仅仅是调换了 `shape` 和 `strides` 两个小数组（对应 [getset.c:L731-L735](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L731-L735) 里的 `PyArray_Transpose` 生成的新视图），底层数据字节串原封不动，所以不需要复制。

**练习 2**：一个 `shape=(2,3,4)`、`dtype=int32`（itemsize=4）的 **C 连续**数组，它的 `strides` 是多少？

**参考答案**：从最后一轴往前递推：`strides[2] = 4`；`strides[1] = 4 × 4 = 16`；`strides[0] = 16 × 3 = 48`。故 `strides = (48, 16, 4)`。

**练习 3**：`arr.strides` 的单位是「元素个数」还是「字节」？源码里的哪一行能证明？

**参考答案**：是**字节**。`getset.c` 里 `array_strides_get`（[L129-L133](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L129-L133)）直接把内部 `strides` 数组转成元组返回，而该内部数组记录的就是字节偏移——正因如此，偏移公式 \(\sum i_k \cdot \mathrm{strides}[k]\) 才能直接给出字节地址。

---

## 5. 综合实践

把本讲的三条主线串起来，完成下面这个小任务。

**任务**：写一段代码，分别用三种方式创建一个内容为「0 到 11 的 3×4 整数矩阵」，并对每一种结果都打印 `shape`、`strides`、`dtype`、`flags`，最后判断哪些结果**两两共享内存**。

```python
import numpy as np

m1 = np.arange(12).reshape(3, 4)                 # 方式 1：arange + reshape（视图）
m2 = np.array([[0,1,2,3],[4,5,6,7],[8,9,10,11]]) # 方式 2：直接从嵌套列表构造（新内存）
m3 = np.ones((3, 4), dtype=np.int64)
np.copyto(m3, np.arange(12).reshape(3, 4))       # 方式 3：empty/ones + copyto 填充

for name, m in [("m1", m1), ("m2", m2), ("m3", m3)]:
    print(name, "shape=", m.shape, "strides=", m.strides,
          "dtype=", m.dtype, "C_CONTIG=", m.flags['C_CONTIGUOUS'])

# 判断内存共享关系
print("m1 vs m2 share memory?", np.shares_memory(m1, m2))  # 预期 False
print("m1 vs m3 share memory?", np.shares_memory(m1, m3))  # 预期 False
```

**需要观察与思考**：

1. 三者的 `shape` 都是 `(3, 4)`，`strides` 都应是 `(32, 8)` 左右（`int64` 时）或 `(48, 4)`（`int32`/默认 `int64` 视平台而定），说明相同形状 + 相同 dtype 的 C 连续数组 strides 必然相同；
2. 三者互不共享内存——因为 `reshape` 在跨 `arange` 边界时是否复制取决于能否就地重排，而 `m2`、`m3` 都明显是新分配；
3. 用一句话总结：**数组的「身份」由数据指针决定，而「逻辑形态」由 `shape`/`strides` 决定**——这正是后续切片、转置、广播都不复制数据的根本原因。

> 该综合实践的具体 strides 数值随平台默认整数宽度（`int64` 或 `int32`）而变，请以本地实际输出为准（待本地验证）。

## 6. 本讲小结

- `ndarray` 是 NumPy 的核心对象，但它**不是 Python 写的类**，而是在 C 扩展 `_multiarray_umath` 中定义的内置类型，经 `multiarray.py` → `_core/__init__.py` → `numpy/__init__.py` 三跳再导出为 `np.ndarray`。
- 创建函数分两类：`array`/`asarray`/`empty`/`zeros` 是 C 层底层原语；`ones`/`full`/`identity` 是 `numeric.py` 里用「`empty` + `copyto` 填充」组合出来的纯 Python 薄封装。
- Python 里看到的属性（`shape`/`strides`/`dtype`/`ndim`/`T` …）由 C 层 `getset.c` 的 `array_getsetlist[]` 表登记，每次访问都调用对应的 getter 动态计算。
- `strides` 的单位是**字节**，C 连续数组的递推公式为 `strides[n-1] = itemsize`、`strides[i] = strides[i+1] * shape[i+1]`；元素偏移为 \(\sum i_k \cdot \mathrm{strides}[k]\)。
- 转置、切片常常「不复制数据」，本质是只改写了 `shape` 和 `strides` 这两个小数组，底层数据字节串不动。

## 7. 下一步学习建议

本讲让你在 Python 层和 C 层都「认识」了 `ndarray`。接下来建议：

1. **学下一讲 u2-l1「数组创建方式全览」**：系统梳理 `array`/`zeros`/`ones`/`arange`/`linspace`/`empty` 的差异，以及 `asarray` 与 `array(copy=True)` 在内存共享上的区别。
2. **下钻 C 结构体**：在 u4-l2「ndarray 内存模型（C 层）」中阅读 `numpy/_core/include/numpy/ndarraytypes.h` 里的 `PyArrayObject_fields`，你会看到本讲反复提到的 `nd`、`dimensions`、`strides`、`descr`、`flags` 字段的真正定义。
3. **延伸阅读**：`numpy/_core/src/multiarray/getset.c` 里 `array_shape_set`、`array_strides_set` 的实现，能让你理解「为什么直接给 `arr.shape` 赋值、`arr.strides` 赋值在 2.4/2.5 已被弃用」——这是连接本讲与「视图与 strides 技巧」那一讲的桥梁。

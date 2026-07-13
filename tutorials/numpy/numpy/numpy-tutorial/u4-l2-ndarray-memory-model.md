# ndarray 内存模型（C 层）

## 1. 本讲目标

本讲把视线从 Python 层下沉到 C 层，拆开 `np.ndarray` 这个「黑盒」，看清它在内存里到底是什么。

学完后你应该能够：

1. 看懂 C 结构体 `PyArrayObject_fields` 的每一个字段（`data` / `nd` / `dimensions` / `strides` / `base` / `descr` / `flags`）及其物理含义。
2. 理解为什么访问 ndarray 的属性要经过「内联访问宏」（`PyArray_DATA` / `PyArray_STRIDES` 等），以及「不透明结构体」（opaque struct）的设计原因。
3. 掌握元素定位的偏移公式，并解释 `strides` 为什么以**字节**（`char *`）为单位、而不是以元素为单位。
4. 对照官方 `internals.code-explanations.rst` 文档理解「一段连续内存 + 一组步长信息」如何描述任意 N 维数组。

本讲是后续 `u4-l3 ufunc 内部实现`、`u8-2 C-API 头文件体系` 的地基——不知道 `PyArrayObject` 长什么样，就读不懂 ufunc 循环里 `char *` 指针怎么推进。

---

## 2. 前置知识

阅读本讲前，你需要先具备以下认知（来自前置讲义）：

- **ndarray 是 C 扩展类型**：它不是用 Python 写的类，而是定义在 C 扩展 `_multiarray_umath` 中的内置类型，经 `multiarray.py` → `_core/__init__.py` → `numpy/__init__.py` 三跳再导出为 `np.ndarray`（见 u1-l4）。
- **shape / strides / dtype / flags 是属性**：它们在 Python 层表现为只读属性，运行时由 C 层的 getter 动态计算返回，而不是普通实例变量（见 u1-l4）。
- **strides 单位是字节**：C 连续数组满足 `strides[n-1] == itemsize`、`strides[i] == strides[i+1] * shape[i+1]`（见 u1-l4）。
- **视图不复制数据**：转置、切片、广播常常只改写 `shape` 与 `strides` 两个小数组，底层 `data` 缓冲区不动（见 u3-l2、u3-l3）。

本讲会补充几个 C/CPython 层的术语：

- **`PyObject_HEAD`**：每个 Python 对象在 C 层开头都有的「对象头」，包含引用计数 `ob_refcnt` 和类型指针 `ob_type`。所有 Python 对象的结构体都以它打头。
- **`Py_ssize_t` / `npy_intp`**：带符号的、能装下指针宽度整数（在 64 位系统上为 8 字节）。NumPy 用 `npy_intp` 作为形状、步长、下标的统一整数类型。
- **`char *` 指针运算**：C 语言中 `char` 恰好占 1 字节，因此 `char *` 的指针加减按**字节**步进——这正是步长以字节为单位的实现基础。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `numpy/_core/include/numpy/ndarraytypes.h` | 定义 `PyArrayObject_fields` 结构体、所有 `NPY_ARRAY_*` 标志位宏、以及 `PyArray_DATA`/`PyArray_STRIDES` 等内联访问函数。这是本讲的**主战场**。 |
| `numpy/_core/include/numpy/ndarrayobject.h` | C-API 的「便捷层」头文件，定义 `PyArray_GETPTR1..4` 等指针定位宏，是对内联访问宏的进一步包装。 |
| `doc/source/dev/internals.code-explanations.rst` | 官方对「内存模型 / 数据类型封装 / N-D 迭代器 / 广播 / 索引 / ufunc」等 C 代码逻辑的逐段注解，本讲引用其「Memory model」小节。 |
| `numpy/_core/include/numpy/npy_common.h` | 定义 `npy_intp`（本讲用到，理解字段类型的宽度）。 |
| `numpy/_core/include/numpy/utils.h` | 定义 `_NPY_OPAQUE_FIRST_FIELD` 宏，解释为什么结构体第一个字段要额外对齐。 |

> 提示：很多内联访问函数（`PyArray_DATA` 等）虽然名字像在 `ndarrayobject.h`，实际定义在 `ndarraytypes.h` 末尾——`ndarrayobject.h` 第 12 行 `#include "ndarraytypes.h"` 把它们带进来。下文给出的行号以 `ndarraytypes.h` 为准。

---

## 4. 核心概念与源码讲解

本讲分三个最小模块：

- **4.1 `PyArrayObject_fields` 结构**：拆解 ndarray 在内存里的真实字段。
- **4.2 内联访问宏与不透明结构体**：为什么不能直接 `arr->data`，而要用 `PyArray_DATA(arr)`。
- **4.3 内存模型文档精读**：对照官方文档，把「内存块 + 步长」的抽象讲透。

---

### 4.1 PyArrayObject_fields 结构

#### 4.1.1 概念说明

在 Python 层，你看到的是一个 `np.ndarray` 对象，有 `.shape`、`.dtype`、`.strides` 等属性。但在 C 层，这个对象**本质上就是一个 C 结构体**，结构体的字段记录了「数据在哪、什么形状、怎么解读」这三件事。

NumPy 把这个结构体命名为 `PyArrayObject_fields`，并用注释明确写出：「The main array object structure」（主数组对象结构），并提示直接访问字段已被弃用、应改用下文的内联访问宏。

#### 4.1.2 核心流程

ndarray 的全部信息可以分成三组：

1. **数据在哪里**：`data` 指针指向一块连续的原始字节缓冲区。
2. **怎么解读**：`nd`（维数）、`dimensions`（每维长度，即 shape）、`strides`（每维步长）、`descr`（dtype，决定每个元素占多少字节、如何编码）。
3. **元信息**：`flags`（连续/对齐/可写等布尔标志）、`base`（视图所基于的原对象，用于保活与释放）、`weakreflist`、`mem_handler` 等。

元素在缓冲区中的字节地址由**偏移公式**给出：

\[
\mathrm{addr}(i_0, i_1, \dots, i_{n-1}) \;=\; \mathrm{data} \;+\; \sum_{k=0}^{n-1} i_k \cdot \mathrm{strides}[k]
\]

也就是说，给定一组下标，把每个下标乘上对应维的步长（字节），累加到 `data` 指针上，就得到该元素的起始字节地址。整个 ndarray 的「N 维」感觉，完全是由 `dimensions` 与 `strides` 这两个长度为 `nd` 的小数组**解释**出来的，底层的 `data` 只是一段扁平字节。

#### 4.1.3 源码精读

下面是 `PyArrayObject_fields` 的完整定义，位于 `ndarraytypes.h`：

[numpy/_core/include/numpy/ndarraytypes.h:794-844](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ndarraytypes.h#L794-L844) — 定义 `PyArrayObject_fields`，注释强调「直接访问字段已弃用，应使用内联函数」。

只保留关键字段的精简版（行号对应原文件）：

```c
typedef struct tagPyArrayObject_fields {
#ifndef Py_TARGET_ABI3T
    PyObject_HEAD
#endif
    char *data;            // L800: 指向原始数据缓冲区（裸字节）
    int nd;                // L802: 维数（Python 层叫 ndim）
    npy_intp *dimensions;  // L804: 每维长度（Python 层叫 shape）
    npy_intp *strides;     // L809: 每跨到下一元素需跳过的字节数
    PyObject *base;        // L828: 视图指向的原对象；析构时 decref
    PyArray_Descr *descr;  // L830: 类型结构指针（dtype）
    int flags;             // L832: 描述内存属性的标志位
    PyObject *weakreflist; // L834: 弱引用链表
    void *_buffer_info;    // L836: 私有缓冲信息（1.20+）
    PyObject *mem_handler; // L842: 每对象的自定义分配器（1.22+）
} PyArrayObject_fields;
```

逐字段说明：

- **`data`（`char *`）**：指向数组数据的第一个字节。注意类型是 `char *`——「字节指针」，这正是 strides 以字节为单位的根源（详见 4.1.4 的实践）。
- **`nd`（`int`）**：数组维数。一个形状为 `(2, 3, 4)` 的数组 `nd == 3`。
- **`dimensions`（`npy_intp *`）**：长度为 `nd` 的数组，第 `k` 个元素就是第 `k` 维的长度。Python 的 `arr.shape` 就是它。
- **`strides`（`npy_intp *`）**：长度为 `nd` 的数组，第 `k` 个元素表示「在第 `k` 维上下标加 1，需要在 `data` 上前进多少**字节**」。Python 的 `arr.strides` 就是它。
- **`base`（`PyObject *`）**：注释写得非常清楚（L810-L828）：对视图，它指向原始数组（并且「折叠」以避免视图链）；对从 buffer 创建的数组，它指向一个需要在删除时 decref 的对象；对带 `WRITEBACKIFCOPY` 标志的数组，它指向待回写的目标数组。**这是「视图不复制数据」能成立的关键**——视图有自己的 `data` 指针，但 `base` 持有真正拥有数据的那一方，保证数据不会被提前释放。
- **`descr`（`PyArray_Descr *`）**：dtype 描述符，记录 `kind`、`type`、`elsize`（每个元素占多少字节，即 itemsize）、字节序等。其中 `elsize` 决定了偏移公式里的「元素粒度」（详见 [numpy/_core/include/numpy/ndarraytypes.h:651-654](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ndarraytypes.h#L651-L654)）。
- **`flags`（`int`）**：一个位掩码整数，每一位代表一种内存属性（连续性、对齐、可写等），详见 4.2.3。

> 关于 `npy_intp`：它是 `Py_ssize_t` 的别名，宽度与指针一致（64 位机器上为 8 字节），见 [numpy/_core/include/numpy/npy_common.h:214-218](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/npy_common.h#L214-L218)。这意味着 `dimensions` 和 `strides` 能装下「极大数组」的下标与步长。

#### 4.1.4 代码实践

**实践目标**：在 `ndarraytypes.h` 中亲手找到 `PyArrayObject_fields`，逐字段列出，并写一段说明「`strides` 为何用 `char *` 字节单位而非元素单位」。这是本讲指定的核心实践任务。

**操作步骤**：

1. 打开 `numpy/_core/include/numpy/ndarraytypes.h`，跳到第 795 行附近，确认你看到了 `typedef struct tagPyArrayObject_fields {`。
2. 逐行读 `data`、`nd`、`dimensions`、`strides`、`base`、`descr`、`flags` 七个字段的注释，用本讲 4.1.3 的表格核对。
3. 用下面这段「**示例代码**」（非项目原有代码，仅用于从 Python 侧反推 C 字段）观察字段间的关系：

```python
# 示例代码：从 Python 侧验证 C 结构体字段的物理含义
import numpy as np

a = np.arange(12, dtype=np.float64).reshape(3, 4)
print("ndim   :", a.ndim)        # 对应 C 字段 nd == 2
print("shape  :", a.shape)       # 对应 C 字段 dimensions == [3, 4]
print("strides:", a.strides)     # 对应 C 字段 strides  == [32, 8]（字节！）
print("itemsize:", a.itemsize)   # 来自 descr->elsize == 8
print("dtype  :", a.dtype)       # 对应 C 字段 descr
print("flags  :\n", a.flags)     # 对应 C 字段 flags（C_CONTIGUOUS 等）
```

4. **需要观察的现象**：`a.strides` 是 `(32, 8)`，单位是**字节**。其中 `8 == a.itemsize`（一个 float64 占 8 字节），`32 == 4 * 8`（第二维有 4 个元素，每跨一行前进 4×8=32 字节）。
5. **预期结果**：你会清楚地看到「步长 = 字节数」，并能用偏移公式手算：元素 `a[1, 2]` 的字节偏移 = `1×32 + 2×8 = 48` 字节，即 `data` 起始后的第 6 个 float64（`48 / 8 = 6`），正好是 `a.flat[6] == 6.0`。
6. **关于「为何用字节单位」的说明**（请在你的笔记里写下，参考 4.3 官方文档）：

   > `strides` 用字节（`char *`）而非元素为单位，是因为字节是描述内存的唯一「通用货币」。理由有三：(1) **同一缓冲区可承载不同尺寸的元素**——结构化数组（structured dtype）一条记录里有多个字段、各字段宽度不同，只有字节能统一表达偏移；(2) **切片与视图会产生任意字节偏移**——视图共享同一块 `data` 但拥有不同的 `strides`，甚至允许 `strides[k] == 0`（广播），用元素为单位无法表达这些情况；(3) **C 层实现自然**——`char *` 指针的加减本身就按字节步进，于是「指针 + 步长」直接得到下一个元素的地址，无需再乘 itemsize。官方文档明确指出「strides 不必是元素尺寸的整数倍」，正因如此才必须用字节。

7. 如果你想确认 C 层确实用 `char *` 做指针运算，看 [numpy/_core/include/numpy/ndarrayobject.h:129-145](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ndarrayobject.h#L129-L145) 的 `PyArray_GETPTR1..4` 宏——它们就是偏移公式的直接代码化（详见 4.2.3）。

> 本实践为「源码阅读 + Python 反推」型，不要求编译 C 扩展；若要真正在 C 里写数组，见 4.2.4。

#### 4.1.5 小练习与答案

**练习 1**：一个形状 `(2, 3, 4)`、dtype 为 `int32`（itemsize=4）的 C 连续数组，它的 `strides` 是多少？

**参考答案**：C 连续意味着最后一维步长 = itemsize = 4，其余维度 `strides[i] = strides[i+1] * shape[i+1]`。所以 `strides = (3×4×4, 4×4, 4) = (48, 16, 4)`。

**练习 2**：为什么对 ndarray 做转置（`a.T`）几乎不花时间、也不复制数据？

**参考答案**：转置只交换了结构体里 `dimensions` 和 `strides` 这两个小数组里元素的顺序，`data` 指针和缓冲区完全不变。所以它只是「换了一种解释同一块内存的方式」，开销是 O(ndim)，与元素总数无关。

---

### 4.2 内联访问宏与不透明结构体

#### 4.2.1 概念说明

知道了字段名，初学者很容易想直接写 `arr->data` 来取数据指针。但 NumPy **强烈不鼓励**直接访问字段——在「未弃用 API」模式下，`PyArrayObject` 甚至被故意定义成一个**只有 `PyObject_HEAD`、看不见任何字段的不透明结构体**（opaque struct），强制你通过一组「内联访问函数」（inline accessor）来读字段。

这么做的原因是 **ABI 稳定性**：字段顺序、是否有 `PyObject_HEAD`、是否插入对齐填充（`_buffer_info`、`mem_handler`、`_NPY_OPAQUE_FIRST_FIELD`）会随 NumPy 版本与 Python 是否启用稳定 ABI（abi3）而变化。只要你只调用 `PyArray_DATA(arr)` 这样的访问函数，NumPy 内部怎么调整结构体都不会破坏你的 C 扩展。

#### 4.2.2 核心流程

访问字段的统一模式是：

1. 你的 C 代码持有 `PyArrayObject *arr`。
2. 调用 `PyArray_DATA(arr)` / `PyArray_STRIDES(arr)` / `PyArray_DIMS(arr)` / `PyArray_NDIM(arr)` / `PyArray_FLAGS(arr)` 等内联函数。
3. 这些函数内部把 `arr` 强转为 `_PyArray_GET_ITEM_DATA(arr)` 指向的内部 fields 结构，再返回对应字段。

判定标志位则用 `PyArray_CHKFLAGS(arr, flags)`，它做的是位与运算：

\[
\text{has\_flags} \;=\; \big((\,\text{arr}\!\to\!\text{flags}\;\&\; \text{mask}\,) \;==\; \text{mask}\big)
\]

即「所需标志位全部命中」才返回真。

#### 4.2.3 源码精读

**不透明结构体的定义**：在「未弃用 API」下，`PyArrayObject` 只剩对象头——

[numpy/_core/include/numpy/ndarraytypes.h:850-862](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ndarraytypes.h#L850-L862) — 当 `NPY_NO_DEPRECATED_API >= NPY_1_7_API_VERSION` 时，`PyArrayObject` 退化为只含 `PyObject_HEAD` 的不透明结构体；只有旧式弃用 API 下它才等于 `PyArrayObject_fields`。这从类型层面**阻止**了你写 `arr->data`。

```c
#if !defined(NPY_NO_DEPRECATED_API) || \
    (NPY_NO_DEPRECATED_API < NPY_1_7_API_VERSION)
/* 旧式、已弃用：字段直接可见 */
typedef PyArrayObject_fields PyArrayObject;
#else
/* 推荐用法：不透明，只能用内联访问函数 */
typedef struct tagPyArrayObject {
        PyObject_HEAD
} PyArrayObject;
#endif
```

**内联访问函数**（节选，位于 `ndarraytypes.h` 末尾）：

[numpy/_core/include/numpy/ndarraytypes.h:1599-1657](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ndarraytypes.h#L1599-L1657) — 一组 `static inline` 函数，封装对每个字段的读取。注释说明：推荐用 `PyArrayObject *` 而非 `PyObject *` 以获得编译期类型检查。

```c
static inline int      PyArray_NDIM(const PyArrayObject *arr)   { return ...->nd; }
static inline void *   PyArray_DATA(const PyArrayObject *arr)   { return ...->data; }
static inline char *   PyArray_BYTES(const PyArrayObject *arr)  { return ...->data; }
static inline npy_intp *PyArray_DIMS(const PyArrayObject *arr)  { return ...->dimensions; }
static inline npy_intp *PyArray_STRIDES(const PyArrayObject *arr){ return ...->strides; }
static inline npy_intp PyArray_DIM(const PyArrayObject *arr, int idim)    { return ...->dimensions[idim]; }
static inline npy_intp PyArray_STRIDE(const PyArrayObject *arr, int istride){ return ...->strides[istride]; }
static inline PyObject *PyArray_BASE(const PyArrayObject *arr)   { return ...->base; }
static inline PyArray_Descr *PyArray_DESCR(const PyArrayObject *arr){ return ...->descr; }
static inline int      PyArray_FLAGS(const PyArrayObject *arr)   { return ...->flags; }
static inline int      PyArray_CHKFLAGS(const PyArrayObject *arr, int flags)
{ return (PyArray_FLAGS(arr) & flags) == flags; }
```

> 注意 `PyArray_DATA` 返回 `void *`、`PyArray_BYTES` 返回 `char *`，但二者底层都取 `->data`——提供两种返回类型只是方便调用方按需使用字节指针。

**指针定位宏 GETPTR**——把偏移公式直接写成宏：

[numpy/_core/include/numpy/ndarrayobject.h:129-145](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ndarrayobject.h#L129-L145) — `PyArray_GETPTR1..4` 用 `PyArray_BYTES + Σ i_k * strides[k]` 计算元素地址，是偏移公式的逐字实现。

```c
#define PyArray_GETPTR1(obj, i) ((void *)(PyArray_BYTES(obj) + \
                                         (i)*PyArray_STRIDES(obj)[0]))
#define PyArray_GETPTR2(obj, i, j) ((void *)(PyArray_BYTES(obj) + \
                                            (i)*PyArray_STRIDES(obj)[0] + \
                                            (j)*PyArray_STRIDES(obj)[1]))
```

对比 4.1.2 的偏移公式，你会看到 `GETPTR2` 就是 \(\mathrm{data} + i\cdot s_0 + j\cdot s_1\) 的代码化。

**标志位宏**——`flags` 字段每一位的含义：

[numpy/_core/include/numpy/ndarraytypes.h:918-1029](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ndarraytypes.h#L918-L1029) — 定义各 `NPY_ARRAY_*` 标志位，注释逐一说明「可否在构造时请求 / 可否用 `PyArray_FLAGS` 测试」。

| 标志位 | 值 | 含义 |
| --- | --- | --- |
| `NPY_ARRAY_C_CONTIGUOUS` | 0x0001 | C 风格连续（最后一维变化最快） |
| `NPY_ARRAY_F_CONTIGUOUS` | 0x0002 | Fortran 风格连续（第一维变化最快） |
| `NPY_ARRAY_OWNDATA` | 0x0004 | 数组自己拥有数据（删除时会释放） |
| `NPY_ARRAY_ALIGNED` | 0x0100 | 数据起始地址按类型要求对齐 |
| `NPY_ARRAY_WRITEABLE` | 0x0400 | 数据区可写 |
| `NPY_ARRAY_WRITEBACKIFCOPY` | 0x2000 | `base` 指向待回写的目标数组 |
| `NPY_ARRAY_ENSURENOCOPY` | 0x4000 | 构造参数：禁止复制（结果必须是视图） |

其中 `NPY_ARRAY_OWNDATA` 与 `base` 字段是一对互补概念：拥有数据者置 `OWNDATA`、`base` 为空；视图不置 `OWNDATA`、`base` 指向原对象。

#### 4.2.4 代码实践

**实践目标**：读懂 `PyArray_GETPTR2` 与偏移公式的等价性，并手算一个真实元素地址。

**操作步骤**：

1. 用「示例代码」构造一个非平凡视图，观察其 strides：

```python
# 示例代码：构造一个「非 C 连续」的视图，验证 GETPTR 公式
import numpy as np
a = np.arange(20, dtype=np.int64).reshape(4, 5)   # itemsize=8
b = a[::2, ::3]                                    # 行步长2、列步长3 的视图
print("b.shape  :", b.shape)        # (2, 2)
print("b.strides:", b.strides)      # (80, 24)  —— 80=2*5*8, 24=3*8
print("b.flags['C_CONTIGUOUS']:", b.flags['C_CONTIGUOUS'])  # False
```

2. **手算** `b[1, 1]` 对应的原数组元素：按 `GETPTR2` 公式，字节偏移 = `1×80 + 1×24 = 104`，换算成 int64 下标 = `104 / 8 = 13`，即 `a.flat[13]`。
3. **需要观察的现象**：`b[1, 1]` 应等于 `a.flat[13]`，且 `b` 与 `a` **共享内存**（`np.shares_memory(a, b)` 为 `True`）。
4. **预期结果**：

```python
print(b[1, 1], a.flat[13], np.shares_memory(a, b))
# 输出: 13 13 True
```

5. 回到 `PyArray_GETPTR2` 宏，确认你的手算与宏的展开式 `PyArray_BYTES(b) + 1*strides[0] + 1*strides[1]` 完全一致。
6. 若本地已按 u1-l2 构建好 NumPy 并想写真正的 C 扩展，可参考 [numpy/_core/include/numpy/ndarrayobject.h:129-130](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ndarrayobject.h#L129-L130)，用 `PyArray_GETPTR2(arr, i, j)` 取指针再按 dtype 解引用；具体编译流程留待 u8-l2。**待本地验证**：C 扩展的编译与导入结果取决于你的环境。

#### 4.2.5 小练习与答案

**练习 1**：在「不透明结构体」模式下，`sizeof(PyArrayObject)` 等于什么？为什么不等于 `sizeof(PyArrayObject_fields)`？

**参考答案**：等于 `sizeof(PyObject_HEAD)`（即一个对象头的大小，通常 16 字节左右）。因为此时 `PyArrayObject` 被故意定义成只含 `PyObject_HEAD`，所有字段都被「藏」起来了，真实字段存在于内部 fields 结构里，只能经 `_PyArray_GET_ITEM_DATA` 访问。这就是「不透明」的含义。

**练习 2**：`PyArray_CHKFLAGS(arr, NPY_ARRAY_C_CONTIGUOUS | NPY_ARRAY_ALIGNED)` 在什么情况下返回真？

**参考答案**：当且仅当 `arr->flags` **同时**置了 `C_CONTIGUOUS` 和 `ALIGNED` 两个位时返回真（因为判定是 `(flags & mask) == mask`）。只命中其中一个会返回假。

---

### 4.3 内存模型文档精读

#### 4.3.1 概念说明

前两个模块从结构体和宏的角度看了 ndarray。NumPy 官方在 `doc/source/dev/internals/code-explanations.rst` 的「Memory model」小节给出了更抽象、更原理性的总结。它的核心一句话是：**「一个数组被看作一段从某地址开始的内存块（chunk），对这段内存的解释取决于步长信息。」**

这句话把 ndarray 的本质说透了：数据是「死」的字节，**形状、步长、dtype、flags 全是「解释规则」**。同样的字节，换一组 `strides` 就是另一个数组（这正是视图的本质）。

#### 4.3.2 核心流程

官方文档把内存模型归纳为四条要点：

1. **「内存块 + 步长」二元组**：数组 = 一段连续内存（`data`）+ 每维一个整数（`stride`，单位字节）。要遍历数组，就必须查阅步长信息。
2. **必须用 `char *` 指针**：因为步长以字节为单位，C 代码里推进指针要用 `char *`（字节指针）。
3. **步长不必是元素尺寸的整数倍**：这意味着你不能假设「下标加 1 一定前进整数个元素」。
4. **0 维数组的特例**：当 `nd == 0`（rank-0 数组）时，`strides` 与 `dimensions` 指针为 `NULL`。

此外，文档强调 `flags` 里的两个关键位：

- `NPY_ARRAY_ALIGNED`：置位时，才能安全地把元素地址解引用为「类型化指针」（如 `((int *)ptr)`）；否则在某些平台（如 Solaris）会触发总线错误（bus error）。
- `NPY_ARRAY_WRITEABLE`：写数据前必须确认此位置位；写到只读区（如只读内存映射文件）可能直接崩溃。

#### 4.3.3 源码精读

下面是「Memory model」小节的原文要点（中文意译，行号对应原文件）：

[doc/source/dev/internals.code-explanations.rst:25-59](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/doc/source/dev/internals.code-explanations.rst#L25-L59) — 官方对 ndarray 内存模型的权威说明，明确「步长以字节为单位」「必须用 `char *`」「步长不必是元素尺寸整数倍」「rank-0 时 strides/dimensions 为 NULL」「ALIGNED/WRITEABLE 决定能否安全解引用或写入」。

要点摘录（与 4.1 的字段一一对应）：

> 「数组被看作从某地址开始的一段内存块。对它的解释取决于步长信息。对 N 维数组的每一维，一个整数（步长）规定了要跳过多少**字节**才能到达该维的下一个元素……你必须使用 `char *` 指针，因为步长以字节为单位。还要记住，步长不必是元素尺寸的整数倍。如果数组维数为 0，那么 strides 和 dimensions 变量为 `NULL`。」

> 「除了 strides 和 dimensions 里的结构信息，flags 包含关于数据如何被访问的重要信息。特别是，仅当 `NPY_ARRAY_ALIGNED` 置位时，把元素解引用为类型化指针才是安全的……在某些平台上（如 Solaris）否则会引发总线错误。如果你打算写入，还应确保 `NPY_ARRAY_WRITEABLE` 置位。」

把这段文档和 4.1 的字段表对照，你会发现：文档讲的「步长」「内存块」「标志位」，分别就是结构体里的 `strides`、`data`、`flags`。文档是「为什么这么设计」的注解，结构体是「具体怎么实现」的代码。

> 补充：为什么结构体第一个字段（如 `data`、`descr` 的 `typeobj`）前面都有一个 `_NPY_OPAQUE_FIRST_FIELD` 宏？见 [numpy/_core/include/numpy/utils.h:80-84](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/utils.h#L80-L84)：在 Python 3.15+（`PY_VERSION_HEX >= 0x030f0000`）启用稳定 ABI 时，对象头被移除，该宏插入 `NPY_DECL_ALIGNED(8)` 把首个字段对齐到 8 字节，保证不同编译场景下字段偏移一致。这正是「不透明结构体 + 内联访问」设计要解决的 ABI 稳定性问题的延伸。

#### 4.3.4 代码实践

**实践目标**：用 Python 侧的属性验证文档里的四条要点，尤其是「步长不必是元素尺寸整数倍」与「0 维数组的 NULL」。

**操作步骤**：

1. 运行下面这段「**示例代码**」：

```python
# 示例代码：验证内存模型文档的四条要点
import numpy as np

# 要点 1 & 2：内存块 + 字节步长
a = np.arange(6, dtype='<u2').reshape(2, 3)   # uint16, itemsize=2
print("strides:", a.strides)   # (6, 2)：6 = 3*2 字节, 2 = itemsize 字节

# 要点 3：步长不是元素尺寸整数倍的「极端」例子——广播
b = a[:, ::2]                                   # 列步长 = 2 个元素 = 4 字节
print("b.strides:", b.strides)                  # (6, 4)
row = a[:1]                                     # 长度1的轴
bc = np.broadcast_to(row, (5, 3))               # 广播：被拉伸的轴 stride=0
print("broadcast strides:", bc.strides)         # (0, 2) —— 0 不是 itemsize 的整数倍? 其实 0 是
print("is 0 a multiple of itemsize?", 0 % a.itemsize == 0)

# 用 dtype 切片得到「步长 > itemsize 且非简单倍数」的结构化场示例
dt = np.dtype([('x', '<i4'), ('y', '<i4')])     # 每条记录 8 字节
s = np.zeros(3, dtype=dt)
y_only = s['y']                                 # 只看 y 字段：每跨一个 y, 前进 8 字节
print("y_only.strides:", y_only.strides, "itemsize:", y_only.itemsize)
# strides=(8,), itemsize=4 —— 步长是字段宽度4的 2 倍，但相对于「整条记录」是任意字节偏移

# 要点 4：0 维数组
z = np.array(42)
print("z.ndim:", z.ndim, "z.shape:", z.shape, "z.strides:", z.strides)
# 0 维数组 strides 为 ()（空元组），对应 C 层 strides/dimensions 为 NULL
```

2. **需要观察的现象**：
   - `a.strides` 是 `(6, 2)`，单位为字节，验证要点 1、2。
   - `bc.strides` 出现 `0`，说明「广播」通过把某维步长置 0 实现，这是「步长可为任意值（包括 0）」的体现（呼应 u3-l4）。
   - `y_only.strides` 为 `(8,)` 而 `itemsize` 为 `4`：在同一块结构化缓冲区里「挑字段」得到的视图，其步长（记录宽 8 字节）并不等于被取字段本身的宽度（4 字节），这就是文档所说「步长不必是元素尺寸整数倍」（这里它恰好是整数倍，但相对于原缓冲区的「元素」概念，它是任意的字节偏移）。
   - `z.strides` 为空元组 `()`，对应 C 层 0 维数组 `strides/dimensions` 为 `NULL`。
3. **预期结果**：与上述注释里的输出一致。如果某行输出不符，**待本地验证**你的 NumPy 版本与字节序设置。
4. 把每条输出回填到 4.3.3 的文档要点，确认「文档说的每一条都能在运行时观察到」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `np.broadcast_to(a, (5, 3))`（`a` 形状 `(1, 3)`）不真正复制 5 份数据？

**参考答案**：广播只是构造了一个视图，把「长度为 1 的那一维」的 `strides` 置为 `0`。于是无论下标在第 0 维取 0~4 的哪个值，偏移公式里 `i_0 * strides[0] = i_0 * 0 = 0`，都指向同一行内存——零拷贝地「假装」有 5 行。

**练习 2**：官方文档说「仅当 `NPY_ARRAY_ALIGNED` 置位时才能安全解引用类型化指针」。请结合 `flags` 字段解释，为什么 `np.frombuffer` 读到的数据可能不对齐、从而需要这个保护。

**参考答案**：`np.frombuffer` 直接把一段已有内存包成数组，这段内存的起始地址可能不是类型要求对齐边界的整数倍（例如某个奇数地址被当作 `int32` 起始）。此时 `flags` 不会置 `ALIGNED` 位。某些 CPU 架构（如部分 SPARC/Solaris）对未对齐访问会触发总线错误，所以 C 代码必须先用 `PyArray_CHKFLAGS(arr, NPY_ARRAY_ALIGNED)` 判断，未对齐时只能逐字节搬运而不能直接 `*(int *)ptr`。

---

## 5. 综合实践

把三个模块串起来，完成一个「**用 C 结构体字段解释一个真实视图**」的小任务。

**任务**：给定下面的代码，请你**不运行**先在纸上作答，再运行验证。

```python
# 示例代码：综合实践
import numpy as np

base = np.arange(24, dtype=np.int32).reshape(2, 3, 4)   # itemsize=4
view = base[:, 1, :]                                     # 形状 (2, 4)
```

**作答要求**（对应 C 结构体字段）：

1. 写出 `view` 的 `nd`、`dimensions`、`strides`（提示：`base` 是 C 连续的，先算 `base.strides`）。
2. 写出 `view` 的 `base` 字段指向谁、`flags` 里 `OWNDATA` 是否置位。
3. 用偏移公式手算 `view[1, 2]` 在 `base` 中的扁平下标，并说明它等于 `base.flat[?]`。
4. 判断 `view.flags['C_CONTIGUOUS']` 是 `True` 还是 `False`，并用 `strides` 解释原因。

**验证**：

```python
print("view.shape   :", view.shape)        # 期望 (2, 4)
print("view.strides :", view.strides)      # 期望 (48, 4)
print("view[1,2]    :", view[1, 2])        # 期望 18
print("base.flat[18]:", base.flat[18])     # 期望 18，证明偏移公式正确
print("C_CONTIGUOUS :", view.flags['C_CONTIGUOUS'])  # 期望 True
print("OWNDATA      :", view.flags['OWNDATA'])       # 期望 False（视图）
print("shares_memory:", np.shares_memory(base, view))  # 期望 True
```

**参考解析**：

- `base` 的 strides：C 连续 + int32(itemsize=4)，`strides = (3×4×4, 4×4, 4) = (48, 16, 4)`。
- `view = base[:, 1, :]`：第 0 维全取、第 1 维取下标 1（固定）、第 2 维全取。结果是 `nd=2`，`dimensions=(2, 4)`；第 0 维步长沿用 `48`，第 1 维（原第 2 维）步长沿用 `4`，故 `strides=(48, 4)`。注意第 1 维被「整数索引」消去后，剩下的两维对应原 `strides[0]` 与 `strides[2]`。
- `base` 字段指向 `base`（原数组保活），`OWNDATA` 为 `False`（视图不拥有数据）。
- `view[1, 2]` 字节偏移 = `1×48 + 2×4 = 56`，换算成 int32 下标 = `56 / 4 = 14`？——注意还要加上「第 1 维固定为 1」带来的起点偏移 `1 × strides[1] = 1×16 = 16` 字节 = 4 个元素。所以总扁平下标 = `14 + 4 = 18`，即 `base.flat[18] == 18`。✅
- `view` 的 strides `(48, 4)` 满足 `strides[1]==itemsize(4)` 且 `strides[0]==4×strides[1]`，故 `C_CONTIGUOUS` 为 `True`。

> 这个练习让你同时用到了 `data`/`dimensions`/`strides`/`base`/`flags` 五个字段，以及偏移公式与 `PyArray_GETPTR` 的等价性。

---

## 6. 本讲小结

- ndarray 在 C 层就是结构体 `PyArrayObject_fields`，关键字段是 `data`（数据指针）、`nd`/`dimensions`（形状）、`strides`（字节步长）、`descr`（dtype）、`flags`（内存属性）、`base`（视图保活）。
- 元素地址由偏移公式 \(\mathrm{data} + \sum_k i_k \cdot \mathrm{strides}[k]\) 决定，`PyArray_GETPTR1..4` 宏是它的逐字实现；形状、转置、切片、广播「不复制数据」的本质，是只改写 `dimensions`/`strides` 而不动 `data`。
- 直接访问字段（`arr->data`）已弃用；推荐用法下 `PyArrayObject` 被定义成**不透明结构体**，必须用 `PyArray_DATA`/`PyArray_STRIDES`/`PyArray_NDIM`/`PyArray_FLAGS` 等内联访问函数，以保证跨 NumPy 版本与 abi3 的 ABI 稳定。
- `flags` 是位掩码：`C_CONTIGUOUS`/`F_CONTIGUOUS` 表连续性、`OWNDATA` 表是否拥有数据、`ALIGNED` 表能否安全解引用类型化指针、`WRITEABLE` 表可否写入；用 `PyArray_CHKFLAGS` 做「全部命中」判定。
- 步长以**字节**为单位（用 `char *` 推进指针），因为字节是描述内存的通用单位——结构化字段、切片视图、广播（stride=0）都会产生任意字节偏移，用「元素个数」无法统一表达。
- 官方 `internals.code-explanations.rst` 的「Memory model」小节是理解上述设计的权威注解，与结构体字段一一对应。

---

## 7. 下一步学习建议

- **下一篇 u4-l3（ufunc 内部实现与类型解析）**：会用到本讲的 `PyArray_DATA`/`PyArray_STRIDES` 来理解 ufunc 内层循环（kernel）如何用 `char *` 指针按步长推进、对输入输出数组逐元素计算。读完本讲再看 ufunc 循环代码会非常自然。
- **u8-l2（C-API 头文件体系）**：如果你想真正动手写一个 C 扩展、用 `PyArray_SimpleNew` 建数组、用 `PyArray_DATA` 写数据，那篇会给出最小可编译示例与本讲提到的 `arrayobject.h`/`ndarrayobject.h`/`npy_common.h` 的分层关系。
- **延伸阅读**：继续读 `doc/source/dev/internals.code-explanations.rst` 的「Data-type encapsulation」「N-D iterators」「Broadcasting」小节，它们都建立在本讲的内存模型之上；以及 `numpy/_core/include/numpy/utils.h` 里 `_NPY_OPAQUE_FIRST_FIELD` 与 `_PyArray_GET_ITEM_DATA` 的成对注释，能帮你彻底理解「不透明结构体」的 ABI 设计。

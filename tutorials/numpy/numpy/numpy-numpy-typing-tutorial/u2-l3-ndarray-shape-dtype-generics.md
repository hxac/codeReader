# NDArray 与 ndarray 的形状/元素类型泛型

## 1. 本讲目标

学完本讲后，你应该能够：

- 理解 `numpy.ndarray` 在类型系统中是一个**接收两个类型参数的泛型类**：形状（`Shape`）和元素类型（`DType`）。
- 把公共别名 `NDArray` 展开为 `np.ndarray[_AnyShape, np.dtype[ScalarT]]`，并解释它「形状未知、元素类型可参数化」的设计。
- 读懂私有文件 `_shape.py` 中三个形状别名 `_Shape`、`_AnyShape`、`_ShapeLike` 各自的含义与使用场景。
- 用 PEP 695 的 `type` 语法，自己定义一个带「具体形状 + 具体元素类型」的数组别名（如一张 RGB 图像的类型）。

本讲是单元 2「三大核心类型别名」的第三篇。前两篇讲了**输入侧**：`ArrayLike`（什么能变成数组）、`DTypeLike`（什么能变成 dtype）。本讲转到**输出侧 / 返回侧**：当我们要标注「这是一个 NumPy 数组」时，该用什么类型。

---

## 2. 前置知识

阅读本讲前，请先具备以下概念（前序讲义已建立，这里只做一句话回顾，不展开）：

- **静态类型检查 vs 运行时**（u1-l1）：类型检查器（mypy / pyright）在程序运行**之前**依据注解做推理，结论可能与运行时不同；注解本身不会改变运行时行为。
- **公共壳 + 私有实现**（u1-l2）：`numpy.typing` 是极薄的公共壳，仅靠 `__all__` 暴露 `ArrayLike`、`DTypeLike`、`NDArray`、`NBitBase` 四个名字；真正的实现藏在私有的 `numpy._typing`，公共壳用一行 `from numpy._typing import ...` 把它们搬过来。
- **`np.dtype[ScalarT]` 承载元素类型**（u2-l1 / u2-l2）：数组里的「每个元素是什么类型」由标量类 `ScalarT`（如 `np.float64`）决定，`np.dtype[ScalarT]` 把这个信息包成 dtype 类型参数。

本讲还需要一点点 **PEP 484 / PEP 585 的泛型与「变长同质元组」语法**，下面用一个最小模块专门讲清楚。

### 2.1 变长同质元组：`tuple[X, ...]`

在 Python 类型系统里，`tuple` 的标注有两种写法，区别就在那个 `...`（`Ellipsis`）：

| 写法 | 含义 | 形状语境下的例子 |
| --- | --- | --- |
| `tuple[int, int]` | 长度**恰好为 2**，两个元素都是 `int` | 一个 2 维形状，如 `(3, 4)` |
| `tuple[int, ...]` | **任意长度**，每个元素都是 `int` | 「某个形状，但维数未知」，如 `(3,)`、`(2, 3, 4)` |
| `tuple[Any, ...]` | 任意长度，元素是 `Any` | 「形状完全未知」 |
| `tuple[()]` | 空元组 | 0 维（标量）形状 `()` |

这里的 `...` 不是省略号，而是一个**类型层面的固定记号**：`tuple[元素类型, ...]` 表示「由这种元素组成的、长度任意的元组」。NumPy 的 `shape` 在运行时永远是「一个由 `int` 组成的元组」，所以形状类型天然映射到 `tuple[int, ...]` 这一族。

---

## 3. 本讲源码地图

本讲只涉及两个核心私有文件，外加公共壳里的官方文档段落：

| 文件 | 作用 |
| --- | --- |
| [`numpy/_typing/_shape.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_shape.py) | 全文件只有 3 行 `type` 定义，给出 `_Shape`、`_AnyShape`、`_ShapeLike` 三个形状别名。本讲最小的文件，却是理解 NDArray 的钥匙。 |
| [`numpy/_typing/_array_like.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py) | 第 15 行就是公共别名 `NDArray` 的定义；同时定义了上一讲的 `ArrayLike`，让输入侧与输出侧在此会合。 |
| [`numpy/typing/__init__.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py) | 模块 docstring 的「API / ndarray」一节是官方对双参数泛型的权威说明，并给出了 PEP 695 `type` 语法示例。 |

辅助阅读（用于实践与对照）：

| 文件 | 作用 |
| --- | --- |
| [`numpy/typing/tests/data/reveal/ufuncs.pyi`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/ufuncs.pyi) | reveal 夹具，大量使用 `npt.NDArray[np.float64]`，并在文件末尾用 `type _Array1D[...] = np.ndarray[tuple[int], np.dtype[ScalarT]]` 演示了「带具体形状」的别名。 |
| [`numpy/_typing/_ufunc.pyi`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi) | ufunc 的类型桩，其中 `axis: _ShapeLike \| None = 0` 是 `_ShapeLike` 在真实签名里的用例。 |

> 提醒：这些 `numpy/_typing/...` 文件是**私有**的，公共用户从不直接 import 它们；我们读它们只是为了理解公共别名 `NDArray` 是怎么拼出来的。引用私有文件中的符号时，下面会统一用「`_` 前缀 = 私有内部实现」的口吻说明。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，对应四个目标别名：

- **4.1** 先建立地基：`ndarray` 本身是双参数泛型（形状 + 元素类型）。
- **4.2** 在地基上理解公共别名 `NDArray`。
- **4.3** 拆开形状这一维：`_Shape`、`_AnyShape`、`_ShapeLike`。

### 4.1 ndarray 是双参数泛型：形状与元素类型

#### 4.1.1 概念说明

很多 Python 库把「数组」当成一个不可分的整体来标注（比如 `np.ndarray` 当一个裸类用）。但 NumPy 的类型系统走得更远：它把 `ndarray` 建模成一个**泛型类**，并且接收**两个**类型参数：

1. **形状（Shape）**：描述数组有几个维度、每一维有多大。它是一个「由 `int` 组成的元组」类型，如 `tuple[int, int]` 表示 2 维。
2. **元素类型（DType）**：描述数组里**每一个元素**的数据类型，如 `np.dtype[np.float64]`。

这样一来，「一个 2 维、元素是 float64 的数组」就不再是模糊的「`np.ndarray`」，而是精确的：

```
np.ndarray[tuple[int, int], np.dtype[np.float64]]
```

注意「形状」和「元素类型」是**两个相互独立的维度**：你可以形状已知而元素类型未知，也可以反过来。这种正交拆分正是后面 `NDArray` 能「锁住元素类型、放开形状」的前提。

#### 4.1.2 核心流程

把 `ndarray` 当泛型类来用，流程是：

```
np.ndarray                    ← 泛型类，接受 2 个类型参数
   │
   ├─ 参数 1: Shape  ── 形状元组的类型
   │     ├─ tuple[int, int]      (已知 2 维)
   │     ├─ tuple[()]            (0 维 / 标量)
   │     └─ tuple[Any, ...]      (默认：形状未知)
   │
   └─ 参数 2: DType  ── 元素 dtype 的类型
         ├─ np.dtype[np.float64]
         ├─ np.dtype[np.uint8]
         └─ np.dtype[Any]        (默认：元素类型未知)
```

两点关键约束（来自官方文档，下一节给出源码）：

- **形状必须是「由 `int` 组成的元组」**。目前**不支持**用 `Literal` 把每一维的具体数值写进类型，例如 `tuple[Literal[3], Literal[3]]` 是不行的——类型系统只跟踪「维数与每维是不是 int」，不跟踪「第 0 维恰好是 3」这种运行时数值。
- **元素类型必须是 `np.dtype` 的子类型**，如 `np.dtype[np.float64]`。省略时默认 `np.dtype[Any]`。

#### 4.1.3 源码精读

官方对这两个类型参数的权威说明，就写在公共壳 `numpy/typing/__init__.py` 的模块 docstring 里。这段文字既是文档，也是 NumPy 类型契约的一部分：

[numpy/typing/__init__.py:L150-L165](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L150-L165) — 说明 `ndarray` 是接收两个类型参数的泛型类：第 1 个是形状（`tuple` of `int`，默认 `tuple[Any, ...]`，目前不支持 `Literal`）；第 2 个是元素 dtype（`np.dtype` 子类型，默认 `np.dtype[Any]`）。紧接着给出 PEP 695 `type` 语法的两个示例。

其中最关键的两行示例是：

```python
type ImageRGB = np.ndarray[tuple[int, int, int], np.dtype[np.uint8]]
type Vector[S: np.generic] = np.ndarray[tuple[int], np.dtype[S]]
```

第一行定义了一个**固定形状**的别名：3 个 `int` 形状（语义上可理解为 H×W×3 之类的 3 维结构）+ `uint8` 元素。第二行则把元素类型本身做成一个 PEP 695 类型参数 `S`，形状固定为 1 维 `tuple[int]`。

> 这就是本讲的核心「姿势」：把形状与元素类型分别填进 `np.ndarray[...]` 的两个槽位，就能得到任意精度需求的数组别名。

#### 4.1.4 代码实践

这是本讲的主实践任务。

**实践目标**：用 PEP 695 `type` 语法，定义一个「带具体形状 + 具体元素类型」的数组别名，并用它标注一个变量，再用类型检查器观察推断结果。

**操作步骤**：

1. 新建文件 `shape_dtype_demo.py`，写入（示例代码）：

   ```python
   # 示例代码
   from typing import Any, reveal_type
   import numpy as np

   # 一张 RGB 图像：3 维形状，元素 uint8
   type ImageRGB = np.ndarray[tuple[int, int, int], np.dtype[np.uint8]]

   img: ImageRGB = np.zeros((480, 640, 3), dtype=np.uint8)

   reveal_type(img)          # 期望：ndarray[tuple[int, int, int], dtype[uint8]]
   reveal_type(img.shape)    # 期望：tuple[int, int, int]（由形状参数决定）
   reveal_type(img.dtype)    # 期望：dtype[uint8]（由元素类型参数决定）
   ```

2. 用 mypy 或 pyright 检查该文件（示例命令，待本地验证）：

   ```bash
   python -m mypy shape_dtype_demo.py
   # 或
   pyright shape_dtype_demo.py
   ```

**需要观察的现象**：

- `reveal_type(img)` 应显示形状被精确跟踪为 `tuple[int, int, int]`、元素为 `uint8`，而不是一个笼统的 `ndarray[Any, dtype[Any]]`。
- `img.shape` 与 `img.dtype` 的推断结果分别来自你填进 `np.ndarray[...]` 的两个参数，说明「形状」和「元素类型」确实是两条独立的类型通道。

**预期结果**：类型检查器报告的 `img` 类型与你写的 `ImageRGB` 一致；如果检查器对 `reveal_type` 的形状给出 `tuple[int, int, int]`，说明形状参数被成功跟踪。**待本地验证**：不同版本 mypy / pyright 对「形状中 `tuple[int, int, int]`」的支持程度可能略有差异；若形状被报告为 `tuple[Any, ...]`，说明当前工具版本尚未跟踪到具体维数，这属于工具能力问题，不影响「双参数」这一概念本身。

#### 4.1.5 小练习与答案

**练习 1**：把上面的 `ImageRGB` 改成「2 维、元素是 `float32`」的别名，应该怎么写？

> **参考答案**：`type MatrixF32 = np.ndarray[tuple[int, int], np.dtype[np.float32]]`。把形状从 `tuple[int, int, int]` 改成 `tuple[int, int]`，把 `np.uint8` 改成 `np.float32` 即可。

**练习 2**：为什么不能写成 `np.ndarray[tuple[3, 3], np.dtype[np.int64]]` 来表达「3×3 的整数矩阵」？

> **参考答案**：因为形状参数要求是「由 `int` **类型**组成的元组」，即元组里每个元素的**类型**是 `int`，而不是把具体数值 `3` 当成类型填进去。`3` 是值不是类型；官方文档也明确「目前不支持 `Literal` int」，所以维度上的具体数值无法进入类型层面。你最多只能表达「这是一个 2 维数组」，无法表达「每维恰好是 3」。

---

### 4.2 NDArray：形状未知、元素类型可参数化的便捷别名

#### 4.2.1 概念说明

上一节我们看到，精确写一个数组的类型要敲一长串：`np.ndarray[tuple[...], np.dtype[...]]`。但绝大多数函数其实**只关心元素类型，不关心具体形状**——比如 `np.sin(x)`，无论 `x` 是 1 维、2 维还是 100 维，只要元素是浮点数就行。

公共别名 `NDArray` 就是为此而生：它**预先帮你把形状固定成「未知」**，只把元素类型留给你做参数：

```
npt.NDArray[np.float64]
```

读作「元素是 `float64` 的 NumPy 数组，形状未知」。它是日常标注中最常用的返回类型，与上一讲的输入侧别名 `ArrayLike` 正好配对：`ArrayLike` 描述「能转成数组的各种输入」，`NDArray` 描述「确定是数组的输出」。

#### 4.2.2 核心流程

`NDArray` 的定义只有一行，但信息量很大。它的构造逻辑是：

1. 接受一个**类型参数** `ScalarT`，并用 `np.generic` 给它设上界（`ScalarT: np.generic`），即「必须是某种 NumPy 标量」。
2. 把形状槽位填成 `_AnyShape`（形状完全未知）。
3. 把元素类型槽位填成 `np.dtype[ScalarT]`（元素类型由你传入的标量决定）。

展开后：

\[
\texttt{NDArray[ScalarT]} \;=\; \texttt{np.ndarray[\_AnyShape,\; np.dtype[ScalarT]]}
\]

也就是说，`NDArray` 把 4.1 节「双参数泛型」中的**形状参数**预先钉死成 `_AnyShape`，只把**元素类型参数** `ScalarT` 暴露出来。这正是「形状未知、元素类型可参数化」的含义。

> 为什么形状要用 `_AnyShape`（`tuple[Any, ...]`）而不是 `_Shape`（`tuple[int, ...]`）？这是 4.3 节的主题。一句话预告：`NDArray` 作为通用别名，主动放弃对形状的任何跟踪，让形状维度保持「完全空白」，以免在通用函数签名里过度约束。

#### 4.2.3 源码精读

`NDArray` 的定义在私有文件 `_array_like.py` 的第 15 行，使用 PEP 695 的**带类型参数的 `type` 语句**：

[numpy/_typing/_array_like.py:L15](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L15) — 定义公共别名 `NDArray[ScalarT: np.generic] = np.ndarray[_AnyShape, np.dtype[ScalarT]]`。`ScalarT` 被上界约束为 `np.generic`（所有 NumPy 标量的根基类），形状槽位用 `_AnyShape`，元素槽位用 `np.dtype[ScalarT]`。

这行能成立，依赖文件顶部从 `_shape` 导入了 `_AnyShape`：

[numpy/_typing/_array_like.py:L13](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L13) — `from ._shape import _AnyShape`，把形状别名搬进来供 `NDArray` 使用。

公共壳再把它搬给用户：

[numpy/typing/__init__.py:L175](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L175) — `from numpy._typing import ArrayLike, DTypeLike, NBitBase, NDArray`，公共壳把私有实现里的 `NDArray` 转发给用户（这就是 u1-l2 讲过的「公共壳 + 私有实现」模式）。

[numpy/typing/__init__.py:L177](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L177) — `__all__ = ["ArrayLike", "DTypeLike", "NBitBase", "NDArray"]`，`NDArray` 正式进入公共契约。

真实用例可以看 reveal 夹具 `ufuncs.pyi` 的开头，它就是用 `npt.NDArray[具体标量]` 来声明测试用的数组变量：

[numpy/typing/tests/data/reveal/ufuncs.pyi:L8-L9](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/ufuncs.pyi#L8-L9) — `AR_f8: npt.NDArray[np.float64]` 与 `AR_i8: npt.NDArray[np.int64]`：把元素类型作为唯一参数传给 `NDArray`，形状完全不在签名里出现。

#### 4.2.4 代码实践

**实践目标**：写一个只关心元素类型、不关心形状的函数，用 `npt.NDArray[ScalarT]` 标注，体会它比完整写 `np.ndarray[...]` 简洁多少。

**操作步骤**：

1. 新建 `ndarray_demo.py`（示例代码）：

   ```python
   # 示例代码
   from typing import Any, reveal_type
   import numpy as np
   import numpy.typing as npt

   def normalize(x: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
       # 无论 x 是 1 维还是 100 维，元素都是 float64
       return (x - x.mean()) / x.std()

   a = np.arange(10, dtype=np.float64)
   b = normalize(a)
   reveal_type(b)   # 期望：ndarray[Any, dtype[float64]]
   ```

2. （可选）把签名「完整展开」成等价形式，对照体会简洁性：

   ```python
   # 示例代码：与上面 npt.NDArray[np.float64] 在类型层面等价
   from numpy._typing import _AnyShape  # 仅用于演示，请勿在生产里 import 私有名
   def normalize_full(x: np.ndarray[_AnyShape, np.dtype[np.float64]]
                      ) -> np.ndarray[_AnyShape, np.dtype[np.float64]]:
       ...
   ```

**需要观察的现象**：`reveal_type(b)` 的形状部分是 `Any`（即 `_AnyShape` 的展开），元素部分是 `float64`——说明 `NDArray` 确实「锁住元素类型、放开形状」。**待本地验证**：`reveal_type` 的确切文本以本地 mypy / pyright 输出为准。

#### 4.2.5 小练习与答案

**练习 1**：把 `npt.NDArray[np.float64]` 完整展开成 `np.ndarray[...]` 形式。

> **参考答案**：`np.ndarray[_AnyShape, np.dtype[np.float64]]`，其中 `_AnyShape = tuple[Any, ...]`。也可以进一步写死成 `np.ndarray[tuple[Any, ...], np.dtype[np.float64]]`。

**练习 2**：如果一个函数对形状有要求（比如「输入必须是 1 维向量」），用 `npt.NDArray` 还够用吗？该怎么办？

> **参考答案**：不够。`npt.NDArray[...]` 主动放弃了形状信息，无法表达「1 维」。这时应像 4.1 节那样直接用 `np.ndarray[tuple[int], np.dtype[...]]`，或参考 `ufuncs.pyi` 里的 `_Array1D[ScalarT] = np.ndarray[tuple[int], np.dtype[ScalarT]]` 自己定义一个带形状的别名。这正是 4.3 节 `_Shape` 等别名存在的意义。

---

### 4.3 _Shape / _AnyShape / _ShapeLike：形状的三种表达

#### 4.3.1 概念说明

形状这一维，NumPy 用三个别名表达不同的「精确度」与「用途」：

- **`_Shape = tuple[int, ...]`**：「任意维数，但每一维都是 `int`」。这是对运行时 `shape` 的**忠实描述**——运行时 `.shape` 永远是一个 int 元组。它常被内部签名用来表示「某个形状，但不具体跟踪维数」。
- **`_AnyShape = tuple[Any, ...]`**：「形状完全未知」。比 `_Shape` 更宽松：连「每一维是 int」都不保证，等于在形状维度上彻底「留白」。公共 `NDArray` 用的就是它。
- **`_ShapeLike = SupportsIndex | Sequence[SupportsIndex]`**：「能被**强制转换**成一个形状元组的东西」。注意它不是形状本身，而是「形状的候选输入」——一个下标（`int`），或一个由下标组成的序列（`tuple[int, ...]` / `list[int]`）。它出现在「需要接收一个形状参数」的函数签名里（如 `axis`）。

一句话区分：`_Shape` / `_AnyShape` 描述「**是**什么形状」，`_ShapeLike` 描述「**能变成**什么形状」——与 u2-l2 讲过的 `DTypeLike`（能变成 dtype 的对象）是同一种「Like」命名思路。

#### 4.3.2 核心流程

三个别名在类型系统里的关系与分工：

```
形状这一维的两个不同问题
   │
   ├─「数组当前的形状是什么类型？」
   │     ├─ _Shape   = tuple[int, ...]   (任意维、每维 int：忠实于运行时)
   │     └─ _AnyShape= tuple[Any, ...]   (完全留白：NDArray 用它)
   │
   └─「调用方可以传什么来描述一个形状？」
         └─ _ShapeLike = SupportsIndex | Sequence[SupportsIndex]
                ├─ SupportsIndex            (= 一个 int，如 axis=1)
                └─ Sequence[SupportsIndex]  (= 一串 int，如 axis=(0, 1))
```

`_ShapeLike` 里的 `SupportsIndex` 是 Python 标准库的类型，表示「任何能当索引用的对象」（基本就是 `int`）。`Sequence[SupportsIndex]` 则是「一串索引」，涵盖 `tuple[int, ...]`、`list[int]` 等。这就是为什么很多 NumPy 函数的 `axis` 参数能同时接受 `int` 和 `(int, int)` ——它的类型正是 `_ShapeLike`。

#### 4.3.3 源码精读

三个别名都定义在 `_shape.py`，全文件极其精简：

[numpy/_typing/_shape.py:L4-L5](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_shape.py#L4-L5) — `type _Shape = tuple[int, ...]` 与 `type _AnyShape = tuple[Any, ...]`。两者都用了 2.1 节讲的「变长同质元组」语法；差别只在元素类型 `int` vs `Any`。

[numpy/_typing/_shape.py:L7-L8](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_shape.py#L7-L8) — 注释「Anything that can be coerced to a shape tuple」点明 `_ShapeLike` 的语义，定义 `type _ShapeLike = SupportsIndex | Sequence[SupportsIndex]`。注意第 1–2 行从 `collections.abc` 与 `typing` 导入 `Sequence`、`Any`、`SupportsIndex`。

这三个别名随后被 `_typing/__init__.py` 聚合再导出，供 NumPy 各处桩文件使用：

[numpy/_typing/__init__.py:L138](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/__init__.py#L138) — `from ._shape import _AnyShape as _AnyShape, _Shape as _Shape, _ShapeLike as _ShapeLike`，用 u1-l2 讲过的 `import X as X` 显式再导出技巧，把私有别名汇聚到 `_typing` 命名空间。

`_ShapeLike` 的真实用例——ufunc 的 `axis` 参数：

[numpy/_typing/_ufunc.pyi:L167](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L167) — `axis: _ShapeLike | None = 0`：`axis` 既可传单个 `int`（`SupportsIndex`），也可传一串 `int`（`Sequence[SupportsIndex]`），或 `None`。这正是 `_ShapeLike` 设计意图的体现。

#### 4.3.4 代码实践

**实践目标**：通过 `reveal_type` 直观对比 `_Shape`（忠实 int）与 `_AnyShape`（完全留白）在检查器眼中的差别，并体会 `_ShapeLike` 为何能同时接纳 `int` 与元组。

**操作步骤**：

1. 新建 `shape_aliases_demo.py`（示例代码，仅供类型检查，注意 `_Shape` 等是私有名，仅用于观察）：

   ```python
   # 示例代码
   from typing import Any, reveal_type
   import numpy as np
   from numpy._typing import _Shape, _AnyShape, _ShapeLike  # 私有名，仅演示

   a: np.ndarray[_Shape, np.dtype[np.float64]] = np.zeros((2, 3), dtype=np.float64)
   b: np.ndarray[_AnyShape, np.dtype[np.float64]] = np.zeros((2, 3), dtype=np.float64)

   reveal_type(a.shape)   # 期望：tuple[int, ...]
   reveal_type(b.shape)   # 期望：tuple[Any, ...]

   # _ShapeLike 同时接纳「一个 int」和「一串 int」
   def take_axis(axis: _ShapeLike) -> None:
       reveal_type(axis)

   take_axis(1)           # 单个 int：合法
   take_axis((0, 1))      # 一串 int：合法
   take_axis([0, 1, 2])   # list[int]：合法
   ```

2. 用类型检查器跑该文件（示例命令，待本地验证）：`python -m mypy shape_aliases_demo.py`。

**需要观察的现象**：

- `a.shape` 与 `b.shape` 的推断结果**不同**：前者元素类型是 `int`，后者是 `Any`——这就是 `_Shape` 与 `_AnyShape` 唯一但关键的差别。
- `take_axis` 能同时接受 `1`、`(0, 1)`、`[0, 1, 2]` 三种形态而不报错，说明 `_ShapeLike = SupportsIndex | Sequence[SupportsIndex]` 确实覆盖了「单个索引」与「索引序列」两种用法。

**预期结果**：三处 `reveal_type` 各自给出上述形状/元素类型；`take_axis` 的三个调用都不产生类型错误。**待本地验证**：`reveal_type` 的确切字符串以本地工具输出为准。

#### 4.3.5 小练习与答案

**练习 1**：`_Shape`（`tuple[int, ...]`）和 `_AnyShape`（`tuple[Any, ...]`）只差一个元素类型，但为什么公共 `NDArray` 偏偏选 `_AnyShape` 而不是 `_Shape`？

> **参考答案**：因为 `NDArray` 是**最通用**的公共别名，设计意图是「完全不在形状维度上做任何承诺」。用 `tuple[int, ...]` 会让检查器认为「每个维度都是 `int`」，在某些数组运算的类型推导里可能引入不必要的约束或假冲突；用 `tuple[Any, ...]` 则把形状维度彻底留白，等于声明「形状我不管」。当某个签名**确实**想忠实表达「形状是个 int 元组」时，才用 `_Shape`。

**练习 2**：`_ShapeLike` 和 `_Shape` 字面很像，语义却完全不同。请用一句话区分。

> **参考答案**：`_Shape` 描述「一个数组**当前的形状**是什么类型」（`tuple[int, ...]`）；`_ShapeLike` 描述「调用方**可以传入什么**来表示一个形状」（一个索引或一串索引）。前者是「是」，后者是「能变成」，与 `DTypeLike` 的命名逻辑一致。

---

## 5. 综合实践

把本讲三个模块串起来：为一段「读取并处理图像」的代码写完整类型注解。

**任务**：实现一个函数 `to_grayscale(img)`，它接收一张 RGB 图像（3 维、`uint8`），返回一张灰度图（2 维、`float64`）。要求：

1. 用 PEP 695 `type` 语法分别定义 `RGBImage`（3 维 `uint8`）和 `GrayImage`（2 维 `float64`）两个别名，**显式写出形状**。
2. 用这两个别名标注 `to_grayscale` 的入参与返回值。
3. 再写一个通用函数 `sum_pixels(x)`，它**不关心形状**、只关心元素是 `float64`，用 `npt.NDArray[np.float64]` 标注。
4. 用 `reveal_type` 观察 `to_grayscale(rgb)` 与 `sum_pixels(gray)` 的返回类型，验证前者形状是 2 维、后者形状是 `Any`。

参考实现（示例代码）：

```python
# 示例代码
from typing import reveal_type
import numpy as np
import numpy.typing as npt

type RGBImage = np.ndarray[tuple[int, int, int], np.dtype[np.uint8]]
type GrayImage = np.ndarray[tuple[int, int], np.dtype[np.float64]]

def to_grayscale(img: RGBImage) -> GrayImage:
    # 按 RGB 通道加权求和（0.299R + 0.587G + 0.114B）
    weights = np.array([0.299, 0.587, 0.114], dtype=np.float64)
    return img.astype(np.float64) @ weights   # 结果形状 (H, W)

def sum_pixels(x: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    return x.sum(keepdims=True)

rgb: RGBImage = (np.random.rand(4, 5, 3) * 255).astype(np.uint8)
gray = to_grayscale(rgb)
total = sum_pixels(gray)

reveal_type(gray)    # 期望：ndarray[tuple[int, int], dtype[float64]]
reveal_type(total)   # 期望：ndarray[Any, dtype[float64]]
```

**观察要点**：

- `to_grayscale` 用了「带具体形状」的别名（4.1 模块），返回类型形状被精确跟踪为 `tuple[int, int]`。
- `sum_pixels` 用了「只锁元素类型」的 `NDArray`（4.2 模块），形状是 `Any`（4.3 模块的 `_AnyShape`）。
- 两种写法并存，正好展示了「形状已知」与「形状未知」两条路线的取舍。**待本地验证**：运行时数值与 `reveal_type` 文本以本地为准；该示例意在演示类型标注，运行结果不是重点。

---

## 6. 本讲小结

- `numpy.ndarray` 在类型系统中是一个**接收两个类型参数的泛型类**：第 1 个是**形状**（`int` 元组类型，如 `tuple[int, int]`），第 2 个是**元素类型**（`np.dtype[...]` 的子类型）；二者相互独立，可分别已知或未知。
- 公共别名 `NDArray` 展开为 `np.ndarray[_AnyShape, np.dtype[ScalarT]]`：它把形状预先钉成「完全未知」的 `_AnyShape`，只把元素类型 `ScalarT`（上界 `np.generic`）暴露给用户参数化，因此最适合「只关心元素类型」的通用签名。
- 私有文件 `_shape.py` 用三个别名表达形状的不同侧面：`_Shape = tuple[int, ...]`（忠实描述运行时形状）、`_AnyShape = tuple[Any, ...]`（彻底留白，NDArray 用它）、`_ShapeLike = SupportsIndex | Sequence[SupportsIndex]`（能被强制转换成形状的输入，如 `axis` 参数）。
- 想要精确跟踪形状时，直接用 `np.ndarray[tuple[int, ...], np.dtype[...]]` 或自定义 PEP 695 别名（参考 reveal 夹具里的 `_Array1D` / `_Array2D`）；想要简洁时，用 `npt.NDArray[标量]`。
- 「形状」与「元素类型」目前都无法承载运行时的**具体数值**（如「恰好 3×3」或「恰好 float64 的某精度」），类型系统只跟踪「维数/元素类型层面」的信息——这是静态类型比运行时更「粗」的地方。

---

## 7. 下一步学习建议

本讲把「形状」和「元素类型」两条维度讲清楚了，但**元素类型那一维的「精度」还没展开**。建议接下来：

- **单元 4「数值精度与协变体系」**：`NDArray[np.float64]` 里的 `float64` 是固定精度；当你想表达「输入 float16 → 输出 float16、输入 float32 → 输出 float32」这种**精度跟随**关系时，就需要 `NBitBase`（及其 2.3 弃用后的替代方案：上界 `TypeVar` 与 `@overload`）。本讲只解决了「形状 + 元素类型是什么」，下一层解决「元素类型的精度如何随输入协变」。
- **延伸阅读源码**：回到 [`numpy/typing/tests/data/reveal/ufuncs.pyi`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/ufuncs.pyi) 第 107–108 行的 `_Array1D` / `_Array2D`，把它当作「带形状别名」的标准范例；再翻看 `numpy/_typing/_ufunc.pyi` 中所有 `axis: _ShapeLike | None = 0`，体会 `_ShapeLike` 在真实 API 中的 pervasive 使用。
- **动手**：用本讲的 `ImageRGB` / `GrayImage` 思路，为你自己常用的数据（时间序列、批次数据、张量）各定义一个带形状的别名，并尝试让一个函数的返回类型形状被类型检查器精确跟踪。

# 类继承体系：_spbase / sparray / spmatrix

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `_spbase` 作为「所有稀疏类的公共基类」提供了哪些公共能力，以及为什么它不能被直接实例化。
- 区分 `sparray`（新式数组接口）与 `spmatrix`（待弃用的矩阵接口）这两套并行的命名空间基类，理解它们各自代表的设计取向。
- 理解具体格式类（如 `csr_array` / `csr_matrix`）是如何通过**多重继承 + 基类顺序**把「格式实现」与「数组/矩阵身份」组装到一起的。
- 用 MRO（方法解析顺序）解释：为什么用同一个矩阵，`csr_array * B` 是逐元素乘，而 `csr_matrix * B` 是矩阵乘。
- 知道 `_allow_nd`、`_format`、`_shape` 等类属性在继承链里如何被覆盖、如何影响维度与格式。

> 承接前置讲义：u1-l4 已经让你动手用过 `coo_array` / `csr_array`。本讲从「会用」退一步，去看这些类到底「是谁」，把对象背后那张继承关系图建立起来。这一讲是为后续 u2-l2～u2-l5 逐格式精读铺好「公共骨架」。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，什么是「基类（base class）」与「命名空间类（namespace class）」。**
面向对象里，基类把多个子类的共性抽到一个地方，子类只写自己的差异。scipy.sparse 里有两种基类：一种是**真正的实现基类**，里面装满了方法（比如算术、转置、格式转换）；另一种是**命名空间类/标记类（marker / mixin）**，它本身几乎不干活，只用来给对象「打标签」——运行时用 `isinstance(obj, sparray)` 来区分一个稀疏对象到底走「数组语义」还是「矩阵语义」。

**第二，什么是 MRO（Method Resolution Order，方法解析顺序）。**
Python 支持多重继承，当一个方法在多个基类里都存在时，Python 用一套叫 C3 线性化的算法决定「先查谁、后查谁」，这个查找顺序就是 MRO。你可以用 `SomeClass.__mro__` 看到它。**多重继承里基类的书写顺序会改变 MRO**，从而改变一个方法最终调到哪个版本——本讲最重要的一处「魔法」就来自这种顺序差异。

**第三，为什么 scipy.sparse 同时存在 array 和 matrix 两套接口。**
历史上 scipy.sparse 只有 `*_matrix`，它的 `*` 运算符被定义成矩阵乘法（模仿 `numpy.matrix`）。但 NumPy 早已把 `numpy.matrix` 列为不推荐使用，社区共识是「`*` 应当表示逐元素乘，矩阵乘用 `@`」。于是 scipy.sparse 引入了新的 `*_array` 接口：`*` 是逐元素、`@` 才是矩阵乘，并计划在未来版本弃用 `*_matrix`。理解 `sparray` 与 `spmatrix` 的分工，就是理解这次接口迁移的源码落点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [_base.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py) | 定义公共基类 `_spbase`、数组命名空间类 `sparray`，以及 `isspmatrix`/`issparse` 的导出、`_formats` 字典等「全包级公共设施」。 |
| [_matrix.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_matrix.py) | 定义矩阵命名空间类 `spmatrix`，承载 `*` 矩阵乘、`**` 矩阵幂、可写 `shape` 等「矩阵语义」。 |
| [_data.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_data.py) | 定义 `_data_matrix(_spbase)`：所有「带 `.data` 属性」的格式（CSR/CSC/COO/BSR/DIA）的中间基类，用 `.data` 实现逐元素运算。 |
| [_csr.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py) | 给出本讲两个「组装范本」：`csr_array(_csr_base, sparray)` 与 `csr_matrix(spmatrix, _csr_base)`。 |
| [_compressed.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py) | `_cs_matrix(_data_matrix, _minmax_mixin, IndexMixin)` 把「公共压缩存储实现 + 最值混入 + 索引混入」三者拼到一起，是 `_csr_base` 的父类。 |

## 4. 核心概念与源码讲解

### 4.1 `_spbase`：所有稀疏类的公共基类

#### 4.1.1 概念说明

`_spbase` 是 scipy.sparse 里**一切稀疏数组/矩阵类的根**（注意是「scipy.sparse 之内的根」，它本身又继承自 `scipy._lib._sparse.SparseABC`）。它把「任意稀疏对象都该有的能力」全部收拢进来：

- 基本属性：`shape`、`ndim`、`nnz`（已存储元素数）、`dtype`、`format`；
- 格式转换：`tocsr/tocsc/tocoo/tolil/todia/tobsr/todok` 与统一入口 `asformat`；
- 算术运算：`+ - * / @` 以及比较运算、`multiply`、`dot`、`power`；
- 工具方法：`sum`、`mean`、`diagonal`、`setdiag`、`toarray`、`todense`、`copy`、`transpose` 等。

它有一个重要约束：**本身不能被直接实例化**。它只定义骨架，真正的存储逻辑（怎么放 `data`、怎么放 `indices`）由各格式子类提供。

#### 4.1.2 核心流程

当一个具体格式（如 CSR）的对象调用某个公共方法时，流程通常是：

1. 方法在 `_spbase` 上定义，但往往**先转成 CSR 再算**——源码注释里写得很直白：「所有算术运算默认走 csr_matrix，新格式只要实现 `tocsr()` 就能获得算术支持」。
2. 子类按需**覆盖**某个方法以提高效率（例如 `csr_array` 覆盖 `tocsr` 返回自身、`_data_matrix` 覆盖 `__abs__` 直接作用在 `.data` 上）。
3. 运行时通过 `isinstance(self, sparray)` 判断身份，决定返回 `ndarray` 还是 `np.matrix`、决定降维后的形状。

```
具体格式对象 (e.g. csr_array)
        │  继承
        ▼
     _spbase  ──── 提供 shape/nnz/format/asformat/tocsr… 算术/比较/求和…
        │  继承
        ▼
   SparseABC (scipy._lib._sparse)  ── 让外部用 issparse() 识别稀疏对象
```

#### 4.1.3 源码精读

类的定义与「不可实例化」约束，见 [_base.py:L85-L88](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L85-L88)——注释说明 `_spbase` 继承 `SparseABC`，是为了让别的子模块无需 `import scipy.sparse` 也能用 `issparse` 识别稀疏对象。

三个关键类属性见 [_base.py:L90-L92](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L90-L92)：

```python
__array_priority__ = 10.1
_format = 'und'  # undefined
_allow_nd: tuple[int, ...] = (2,)
```

- `__array_priority__` 让稀疏对象在与 ndarray 混合运算时优先决定结果类型；
- `_format = 'und'` 是「未定义」，每个格式子类会覆盖（CSR 覆盖为 `'csr'`）；
- `_allow_nd = (2,)` 表示默认**只允许 2 维**，CSR/DIA 等会放宽到 `(1, 2)` 以支持 1 维。

「不可直接实例化」在构造器里强制执行，见 [_base.py:L138-L147](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L138-L147)：当类名就是 `_spbase` 时直接抛 `ValueError`；同时对 `sparray` 拒绝用标量构造。

公共属性由 `_spbase` 统一提供：`shape` 见 [_base.py:L149-L151](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L149-L151)、`nnz` 见 [_base.py:L372-L380](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L372-L380)、`format` 见 [_base.py:L392-L395](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L392-L395)。其中 `nnz` 走 `_getnnz()`，而 `_getnnz` 默认抛 `NotImplementedError`（见同段），**逼着每个格式子类去实现自己的计数方式**——这就是「骨架 + 子类填充」的设计。

格式转换的统一入口 `asformat` 见 [_base.py:L471-L502](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L471-L502)：它把字符串 `'csr'` 翻译成调用 `self.tocsr(copy=...)`，等价于「`to` + 格式名」的方法分发。

`_spbase.__mul__` 是理解 `*` 行为的关键之一，见 [_base.py:L969-L970](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L969-L970)——它**返回 `self.multiply(other)`，即逐元素乘**。这条会在 4.4 节与 `spmatrix.__mul__` 形成对照。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 `_spbase` 不可实例化，且它定义了所有子类共享的公共属性。

**操作步骤**（示例代码，请在本机运行）：

```python
# 示例代码
import scipy.sparse as sp
from scipy.sparse._base import _spbase

# 1) 尝试直接实例化基类 —— 应当抛错
try:
    _spbase((3, 3))
except ValueError as e:
    print("直接实例化被拒绝:", e)

# 2) 观察公共属性来自哪里
A = sp.csr_array([[1, 0, 2], [0, 0, 0], [4, 0, 5]])
for attr in ("shape", "ndim", "nnz", "format", "dtype"):
    print(f"{attr:>6} =", getattr(A, attr))
```

**需要观察的现象**：第 1 步抛出 `ValueError: This class is not intended to be instantiated directly.`；第 2 步打印出 `(3, 3)`、`2`、`3`、`'csr'`、`int64`（dtype 可能因平台而异）。

**预期结果**：`_spbase` 只能被子类继承使用；`csr_array` 对象身上的 `shape/nnz/format` 等属性，其定义都能在 `_base.py` 里追溯到 `_spbase`。如运行结果与本说明不符，请以本地实际输出为准（标注「待本地验证」的细节：`int64` vs `int32`）。

#### 4.1.5 小练习与答案

**练习 1**：`_spbase` 里 `_getnnz` 默认抛 `NotImplementedError`，这种「基类声明、子类实现」的设计有什么好处？

> **参考答案**：它强制每个稀疏格式都必须自己说清楚「我有多少个已存储元素」，因为不同格式的计数方式不同（CSR 读 `indptr[-1]`，DOK 数字典长度）。把签名与文档放在基类，保证了接口统一；把实现下放给子类，保证了正确性与效率。

**练习 2**：`_format` 为什么在 `_spbase` 里设成 `'und'` 而不是干脆不定义？

> **参考答案**：这样 `format` 属性永远有值可读，`__repr__`、`asformat` 等公共逻辑不必每处都判空；任何忘记覆盖 `_format` 的子类会立即在打印时暴露「undefined」，便于发现遗漏。

---

### 4.2 `sparray`：新式数组命名空间基类

#### 4.2.1 概念说明

`sparray` 是一个**命名空间类（namespace class）/ 标记混入（marker mixin）**。注意它和 `_spbase` 的本质区别：

- `_spbase` 是**实现基类**，装满了真实方法；
- `sparray` 是**裸类**（`class sparray:`，不继承任何东西），几乎没有方法。

它的全部作用是给具体类「盖章」：只要一个类在继承链里混入了 `sparray`，运行时 `isinstance(obj, sparray)` 就返回 `True`，整套代码据此判断「这是数组语义」。scipy.sparse 内部大量分支用这个标记来决定行为，比如 `__repr__` 里判断打印 `array` 还是 `matrix`、`sum`/`max` 里决定降维后的形状。

#### 4.2.2 核心流程

`sparray` 的「身份判定」如何在运行时发挥作用：

```
csr_array(_csr_base, sparray)
        │ isinstance(x, sparray) == True
        ▼
   代码分支：x 是「数组」
   - __repr__ 打印 "sparse array"
   - sum/max 沿轴返回 1-D 结果
   - 不允许设 shape
```

#### 4.2.3 源码精读

`sparray` 的定义见 [_base.py:L1720-L1725](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L1720-L1725)：注意它**没有基类**，文档明说「它不能被实例化，被设计为 mixin class」。

一个巧妙的细节：`sparray.__doc__ = _spbase.__doc__`（见 [_base.py:L1751](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L1751)）——让 `sparray` 在文档里「借用」`_spbase` 的说明，因为它自己并不承载实现。

`_spbase.__repr__` 用 `isinstance(self, sparray)` 决定打印 `array` 还是 `matrix`，见 [_base.py:L423-L429](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L423-L429)。

`_spbase._ascontainer` 用 `issubclass(cls, sparray)` 决定结果是 `np.ndarray` 还是 `np.matrix`，见 [_base.py:L259-L264](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L259-L264)——这是 array/matrix 两套接口在「返回类型」上的分水岭。

`_data.py` 里的最值计算也用 `isinstance(self, sparray)` 区分降维形状，见 [_data.py:L206-L220](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_data.py#L206-L220)：数组沿轴返回一维 `(M,)`，矩阵返回二维 `(1, M)` 或 `(M, 1)`。

`_data_matrix(_spbase)` 这个中间基类见 [_data.py:L20](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_data.py#L20)，它在文件头 `from ._base import _spbase, sparray`（[_data.py:L12](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_data.py#L12)）同时引入两者——实现靠 `_spbase`，身份判定靠 `sparray`。

#### 4.2.4 代码实践

**实践目标**：验证 `sparray` 只是一个「身份标记」，并观察它如何影响 `__repr__` 与降维结果的形状。

**操作步骤**：

```python
# 示例代码
import numpy as np
import scipy.sparse as sp
from scipy.sparse._base import sparray

A = sp.csr_array([[1, 0, 2], [0, 0, 0]])
print("isinstance(sparray) =", isinstance(A, sparray))
print("repr(A) =", repr(A))           # 应出现 "sparse array"
print("sum(axis=0).shape =", A.sum(axis=0).shape)   # 数组语义 -> (3,)
```

**需要观察的现象**：`isinstance` 为 `True`；`repr` 里是 `Compressed Sparse Row sparse array`；`sum(axis=0)` 结果形状是 `(3,)`（1 维）。

**预期结果**：数组语义下，沿轴归约会**真正降维**到 1 维。这与 4.3 节矩阵语义下「保持 2 维」形成对照。请以本地输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sparray` 不直接继承 `_spbase`，而是做成一个独立的裸类？

> **参考答案**：因为「身份标记」和「实现继承」是两件正交的事。把 `sparray` 做成独立裸类，具体格式类就能用**多重继承**自由组合：`csr_array(_csr_base, sparray)` 同时拿到「CSR 实现（经 `_csr_base`→`_spbase`）」和「数组身份（经 `sparray`）」。如果 `sparray` 继承了 `_spbase`，就会与 `_csr_base` 那条链产生重复继承，徒增 MRO 复杂度。

**练习 2**：`sparray.__doc__ = _spbase.__doc__` 这一行解决了什么问题？

> **参考答案**：`sparray` 本身没有实现，直接写文档字符串会与 `_spbase` 重复；把 `_spbase` 的文档赋给它，既让 `help(sparray)` 有内容可看，又只维护一份文档。

---

### 4.3 `spmatrix`：待弃用的矩阵命名空间基类

#### 4.3.1 概念说明

`spmatrix` 与 `sparray` 是**对称**的命名空间类：它代表「矩阵语义」，对应待弃用的 `*_matrix` 接口。它与 `sparray` 的关键差异不在「存不存在方法」（两者都是裸类），而在于 `spmatrix` **额外覆盖了三个运算符**，把矩阵语义钉死：

- `__mul__`：`*` 表示**矩阵乘法**（而不是逐元素乘）；
- `__pow__`：`**` 表示**矩阵幂**（而不是逐元素幂）；
- `shape`：是一个**可读可写**的属性（数组接口下 `shape` 是只读的）。

`spmatrix` 顶部文档直接发出警告：SciPy sparse 正在从矩阵接口迁移到数组接口，未来版本会弃用矩阵接口。

#### 4.3.2 核心流程

`spmatrix` 如何把矩阵语义注入到 `csr_matrix` 等具体类：

```
csr_matrix(spmatrix, _csr_base)   # 注意：spmatrix 写在前面
        │  MRO 中 spmatrix 排在 _spbase 之前
        ▼
   解析 `*` 时优先找到 spmatrix.__mul__  ->  self._matmul_dispatch(other)
   即：csr_matrix * B  ==  csr_matrix @ B  （矩阵乘）
```

#### 4.3.3 源码精读

`spmatrix` 的定义与「迁移警告」见 [_matrix.py:L1-L15](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_matrix.py#L1-L15)：同样是个裸类，文档说明它同时是矩阵类型的命名空间，且不可实例化。

矩阵接口的关键差异——`*` 走矩阵乘、`**` 走矩阵幂——见 [_matrix.py:L53-L64](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_matrix.py#L53-L64)：

```python
# Restore matrix multiplication
def __mul__(self, other):
    return self._matmul_dispatch(other)

def __rmul__(self, other):
    return self._rmatmul_dispatch(other)

# Restore matrix power
def __pow__(self, power):
    from .linalg import matrix_power
    return matrix_power(self, power)
```

注释「Restore matrix multiplication」点明了设计意图：基类 `_spbase` 把 `*` 定义成逐元素，`spmatrix` 在这里把它「还原」回矩阵乘。

可写的 `shape` 属性见 [_matrix.py:L66-L80](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_matrix.py#L66-L80)（`get_shape`/`set_shape` + `shape = property(fget, fset)`），以及一批向后兼容方法（`getnnz`、`getH`、`getcol`、`getrow`、`asfptype` 等）——这些都是矩阵时代遗留、数组接口里已被精简或替代的 API。

`_allow_nd = (2,)` 见 [_matrix.py:L16](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_matrix.py#L16)：矩阵接口**强制 2 维**，这是 matrix 与 array（允许 1 维）的另一个硬性区别。

判断对象身份的 `isspmatrix` 见 [_base.py:L1754-L1792](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L1754-L1792)：`isspmatrix(x)` 就是 `isinstance(x, spmatrix)`，且**对稀疏数组返回 `False`**；通用判断则用 `issparse`（对两者都返回 `True`）。

#### 4.3.4 代码实践

**实践目标**：验证 `csr_matrix` 的 `*` 是矩阵乘、`shape` 可写，且 `isspmatrix` 对数组返回 `False`。

**操作步骤**：

```python
# 示例代码
import numpy as np
import scipy.sparse as sp

M = sp.csr_matrix([[1, 2], [0, 4]])
B = sp.csr_matrix([[1, 2], [0, 4]])

print("csr_matrix * B  =\n", (M * B).toarray())   # 矩阵乘
print("isspmatrix(M)  =", sp.isspmatrix(M))
print("isspmatrix(csr_array) =", sp.isspmatrix(sp.csr_array(M)))

# 矩阵接口下 shape 可写
M.shape = (4, 1) if False else M.shape  # 仅占位；真正写入见下
try:
    sp.csr_array([[1,2],[0,4]]).shape = (4,1)
except (AttributeError, ValueError) as e:
    print("数组 shape 不可写:", type(e).__name__)
```

**需要观察的现象**：`M * B` 的结果是矩阵乘 `[[1,10],[0,16]]`（不是逐元素 `[[1,4],[0,16]]`）；`isspmatrix(M)` 为 `True`，对 `csr_array` 为 `False`；给 `csr_array` 设 `shape` 会抛错。

**预期结果**：矩阵接口与数组接口在 `*`、`shape`、`isspmatrix` 三处都不同。具体异常类型以本地 Python/SciPy 版本为准（标注「待本地验证」：抛 `AttributeError` 还是 `ValueError`）。

#### 4.3.5 小练习与答案

**练习 1**：`spmatrix.__mul__` 的注释为什么写「Restore matrix multiplication」？

> **参考答案**：因为继承链里更底层的 `_spbase.__mul__` 已经把 `*` 定义成逐元素乘（`multiply`）。`spmatrix` 作为更靠近派生类的基类，把矩阵乘「还原」回来，使 `csr_matrix * B` 表现成矩阵乘——这是为了保持旧 `*_matrix` 接口的历史行为。

**练习 2**：`isspmatrix(csr_array([[5]]))` 返回什么？为什么？

> **参考答案**：返回 `False`。因为 `csr_array` 继承的是 `sparray` 而非 `spmatrix`，`isspmatrix` 检查的是 `isinstance(x, spmatrix)`。若想统一判断「是不是稀疏对象」，应改用 `issparse`。

---

### 4.4 MRO 与 `*`：array 与 matrix 的核心行为分野

#### 4.4.1 概念说明

本节是把前三节串起来的「综合」模块，也是本讲理解 SciPy sparse 设计最关键的一环。同一个 CSR 矩阵，用 `csr_array` 包装还是 `csr_matrix` 包装，`*` 的语义截然不同。这并不是 if-else 判断出来的，而是**多重继承的基类书写顺序 + Python MRO**自然产生的结果。

两个范本定义在 [_csr.py:L333](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L333) 与 [_csr.py:L465](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L465)：

```python
class csr_array(_csr_base, sparray): ...
class csr_matrix(spmatrix, _csr_base): ...
```

注意**基类顺序是故意对调的**：`csr_matrix` 把 `spmatrix` 写在第一位，`csr_array` 把 `sparray` 写在第二位。

#### 4.4.2 核心流程

两个类的 MRO（按 C3 线性化推导；完整顺序请用 `__mro__` 在本地确认）：

```
csr_array :  csr_array → _csr_base → _cs_matrix → _data_matrix
             → _minmax_mixin → IndexMixin → _spbase → SparseABC
             → sparray → object

csr_matrix:  csr_matrix → spmatrix → _csr_base → _cs_matrix → _data_matrix
             → _minmax_mixin → IndexMixin → _spbase → SparseABC → object
```

关键点：

- `csr_array` 的 MRO 里，`sparray` 排在**最末尾**（紧挨 `object`），而 `sparray` 没有定义 `__mul__`。于是 `*` 沿 MRO 往上找到 `_spbase.__mul__` → **逐元素乘**。
- `csr_matrix` 的 MRO 里，`spmatrix` 排在**第二位**（在 `_csr_base`、`_spbase` 之前），而 `spmatrix` 定义了 `__mul__` → **矩阵乘**。

一句话：**基类书写顺序决定了 `*` 先命中谁**。

#### 4.4.3 源码精读

`_cs_matrix` 把三条链拼到一起，见 [_compressed.py:L25](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L25)：`class _cs_matrix(_data_matrix, _minmax_mixin, IndexMixin)`。它本身既是「压缩存储实现」，又通过多重继承混入了「最值能力（`_minmax_mixin`）」和「索引能力（`IndexMixin`）」。

`_csr_base` 在此之上挂上 CSR 的格式标记与维度支持，见 [_csr.py:L18-L20](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L18-L20)：

```python
class _csr_base(_cs_matrix):
    _format = 'csr'
    _allow_nd: tuple[int, ...] = (1, 2)
```

两个终点类的对调写法见 [_csr.py:L333](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L333)（`csr_array(_csr_base, sparray)`）与 [_csr.py:L465](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L465)（`csr_matrix(spmatrix, _csr_base)`）。

两条 `__mul__` 实现作对照：

- 数组侧（逐元素）：[_base.py:L969-L970](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L969-L970) — `return self.multiply(other)`；
- 矩阵侧（矩阵乘）：[_matrix.py:L54-L55](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_matrix.py#L54-L55) — `return self._matmul_dispatch(other)`。

维度支持也由 MRO 决定：`csr_matrix` 先命中 `spmatrix._allow_nd = (2,)`（[_matrix.py:L16](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_matrix.py#L16)）所以只能 2 维；`csr_array` 不经过 `spmatrix`，落到 `_csr_base._allow_nd = (1, 2)`（[_csr.py:L20](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L20)）所以支持 1 维。

#### 4.4.4 代码实践

**实践目标**：用 `__mro__` 内省打印继承链，并对照确认 `*` 在 array 与 matrix 下的不同结果。**这是本讲的主实践任务。**

**操作步骤**：

```python
# 示例代码
import numpy as np
import scipy.sparse as sp
from scipy.sparse._base import _spbase, sparray
from scipy.sparse._matrix import spmatrix

# ---- 第 1 步：打印 MRO，定位 _spbase / sparray / spmatrix ----
print("=== csr_array.__mro__ ===")
for c in sp.csr_array.__mro__:
    print(" ", c.__name__)

print("=== csr_matrix.__mro__ ===")
for c in sp.csr_matrix.__mro__:
    print(" ", c.__name__)

# 在 MRO 里找标记类与基类的位置
def pos(cls, chain): 
    return [c.__name__ for c in chain].index(cls.__name__)
print("csr_array  中 sparray 位置:", pos(sparray,  sp.csr_array.__mro__))
print("csr_matrix 中 spmatrix 位置:", pos(spmatrix, sp.csr_matrix.__mro__))
print("csr_matrix 中 _spbase  位置:", pos(_spbase,  sp.csr_matrix.__mro__))

# ---- 第 2 步：同一个矩阵，对比 * 的行为 ----
data = [[1, 2], [0, 4]]
A_arr = sp.csr_array(data)
A_mat = sp.csr_matrix(data)
B = sp.csr_array(data)

print("csr_array * B (逐元素):\n", (A_arr * B).toarray())
print("csr_matrix * B (矩阵乘):\n", (A_mat * B).toarray())
```

**需要观察的现象**：

1. `csr_array.__mro__` 末尾出现 `... _spbase, SparseABC, sparray, object`，`sparray` 在 `_spbase` 之后；
2. `csr_matrix.__mro__` 开头就是 `csr_matrix, spmatrix, _csr_base, ...`，`spmatrix` 在 `_spbase` 之前（位置数字小于 `_spbase` 的位置数字）；
3. `csr_array * B = [[1, 4], [0, 16]]`（逐元素），`csr_matrix * B = [[1, 10], [0, 16]]`（矩阵乘）。

**预期结果**：第 3 步两组结果不同，正好印证「MRO 命中不同的 `__mul__`」。MRO 的完整尾部顺序（如 `_minmax_mixin`、`IndexMixin`、`SparseABC` 的相对次序）建议以本地 `__mro__` 实际输出为准（标注「待本地验证」：混入类的精确先后次序）。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `csr_matrix` 的定义改成 `class csr_matrix(_csr_base, spmatrix)`（把 `spmatrix` 挪到后面），`csr_matrix * B` 的行为会变成什么？

> **参考答案**：会变成**逐元素乘**。因为此时 MRO 里 `_csr_base → … → _spbase` 排在 `spmatrix` 之前，`*` 会先命中 `_spbase.__mul__`（`multiply`）。这正是为什么源码**故意**把 `spmatrix` 写在第一位——用基类顺序来锁定矩阵语义。

**练习 2**：为什么 `csr_array` 能表示 1 维稀疏数组，而 `csr_matrix` 不能？

> **参考答案**：`csr_matrix` 的 MRO 先命中 `spmatrix._allow_nd = (2,)`，强制 2 维；`csr_array` 不经过 `spmatrix`，命中的是 `_csr_base._allow_nd = (1, 2)`，允许 1 维。维度支持同样由 MRO 决定，与 `*` 的机理一致。

## 5. 综合实践

把本讲的知识串起来，完成一个「继承关系调查表」任务：

1. **建表**：对 `csr_array`、`csr_matrix`、`coo_array`、`coo_matrix` 四个类，分别打印 `__mro__` 里 `sparray`、`spmatrix`、`_spbase` 三者的相对位置（用 4.4.4 的 `pos` 辅助函数），做成一张表格。
2. **预测并验证**：根据表格里「`spmatrix` 是否排在 `_spbase` 之前」，预测每个类的 `*` 是矩阵乘还是逐元素乘，再用一个固定矩阵 `[[1,2],[0,4]]` 实测验证。
3. **解释**：用一句话写出 `_allow_nd` 对这四个类分别取什么值，并说明它来自 MRO 里的哪个类。

参考答案要点：

- `*_array` 类的 MRO 含 `sparray`、不含 `spmatrix`，`*` 为逐元素乘，`_allow_nd` 多为 `(1, 2)`（来自格式基类）；`*_matrix` 类的 MRO 含 `spmatrix` 且排在 `_spbase` 之前，`*` 为矩阵乘，`_allow_nd` 为 `(2,)`（来自 `spmatrix`）。
- COO 与 CSR 在「身份语义」上完全一致，差别只在存储格式——这正是「身份（sparray/spmatrix）」与「格式（_csr_base/_coo_base）」正交分离的体现。

## 6. 本讲小结

- `_spbase` 是所有稀疏类的**公共实现基类**，提供 `shape/nnz/format/asformat` 及全部算术、转换、归约方法；它不可直接实例化，关键方法（如 `_getnnz`）留给子类实现。
- `sparray` 与 `spmatrix` 是两个**对称的命名空间标记类**（裸类、几乎无方法），用 `isinstance` 在运行时区分「数组语义」与「矩阵语义」。
- `spmatrix` 额外覆盖 `__mul__`/`__pow__`/`shape`，把 `*` 锁成矩阵乘、`**` 锁成矩阵幂、`shape` 设为可写、维度限定为 2。
- 具体格式类通过**多重继承**组装：`csr_array(_csr_base, sparray)`、`csr_matrix(spmatrix, _csr_base)`——基类顺序是**故意对调**的。
- MRO 决定了 `*`、`**`、`_allow_nd` 最终命中谁：`csr_matrix` 先命中 `spmatrix` → 矩阵乘 / 强制 2 维；`csr_array` 落到 `_spbase`/`_csr_base` → 逐元素乘 / 允许 1 维。
- 新代码应统一使用 `*_array`（数组接口），`*_matrix`（矩阵接口）处于待弃用状态；判断「是不是稀疏」用 `issparse`，判断「是不是矩阵」用 `isspmatrix`。

## 7. 下一步学习建议

- 下一讲 **u2-l2 COO 坐标（三元组）格式** 将进入第一个具体格式的源码精读。届时你会看到 `_coo_base` 如何继承 `_data_matrix`（本讲的 `_data.py`）与 `IndexMixin`，并把「COO 身份」挂在 `sparray`/`spmatrix` 之下——本讲建立的继承骨架就是它的脚手架。
- 建议继续阅读：[_coo.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py) 中 `coo_array`/`coo_matrix` 的 class 行，亲手验证它们与 `csr_array`/`csr_matrix` 走的是同一套「格式基类 + 命名空间标记」组装方式。
- 若想深入 MRO 原理，可阅读 Python 官方文档对 C3 线性化的描述，并对照本讲给出的 `__mro__` 输出逐一验证推导。

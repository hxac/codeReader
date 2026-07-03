# 第 1 讲：项目定位与稀疏存储思想

## 1. 本讲目标

本讲是 scipy.sparse 学习手册的第一篇。读完本讲后，你应该能够：

- 用自己的话说清「稀疏矩阵/数组」是什么，以及它在什么场景下值得使用。
- 区分稠密存储（NumPy `ndarray`）与稀疏存储在**内存占用**和**计算代价**上的本质差别。
- 看懂 `scipy.sparse/__init__.py` 顶部文档字符串对整个子包的定位说明。
- 知道 `_formats` 字典里登记了哪些稀疏格式，以及为什么只有其中一部分被真正实现。
- 认识 scipy.sparse 在 SciPy 生态中的历史来源（Oliphant 等），以及它当前「从 matrix 迁移到 array」的方向。

本讲**不**要求你已经会用任何稀疏格式，所有概念都从零讲起。后续讲义（U2）才会逐一深入每种格式的内部结构。

## 2. 前置知识

读本讲前，你最好已经了解：

- **Python 基础**：会写循环、会用 `import`。
- **NumPy 的 `ndarray`**：知道它是按「连续内存块」存放数值的，一个形状为 `(m, n)`、dtype 为 `float64` 的二维数组在内存里要占 `m × n × 8` 个字节。
- **矩阵-向量乘法**的直觉：给一个矩阵 \(A\) 和向量 \(x\)，\(y = Ax\) 是把 \(A\) 的每一行与 \(x\) 做点积。

如果你对上面两点还陌生也没关系，本讲会用尽量通俗的方式引入。

### 一个关键直觉

很多真实世界的数据是「**绝大部分是 0**」的。比如：

- 一个社交网络的「用户 × 用户」好友矩阵：上亿用户，但每人只和几百人有连接，非零占比可能只有百万分之一。
- 有限元分析里的刚度矩阵：每个节点只和相邻节点耦合，矩阵里大量位置是 0。
- 推荐系统的「用户 × 商品」评分矩阵：一个人只评过极少数商品。

如果还按稠密方式把所有 0 都存下来，内存会爆炸；而很多运算（如矩阵乘法）对 0 的处理是「乘了等于没乘」。**稀疏存储**的核心思想就是：**只存非零元素及其位置，把零当作「隐式的零」**，从而既省内存又省计算。

## 3. 本讲源码地图

本讲只涉及两个文件，它们是整个 scipy.sparse 的「门面」和「根基」：

| 文件 | 作用 |
|------|------|
| [`__init__.py`](__init__.py) | 子包入口。顶部是一段很长的**文档字符串**，相当于整个子包的说明书；后半部分用 `from ._xxx import *` 把各个格式类、构造函数、子模块聚合导出。 |
| [`_base.py`](_base.py) | 所有稀疏类的**共同基类** `_spbase` 与命名空间基类 `sparray`。其中定义了 `_formats` 字典，登记了 scipy 能识别的全部格式名。 |

> 提示：本讲引用的永久链接都以当前 HEAD `ce1f64777e` 为基准。点击链接可以直接跳到 GitHub 上对应代码行。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- **4.1** 稠密 vs 稀疏：内存与计算的取舍（纯概念）
- **4.2** scipy.sparse 是什么：包文档与定位（`__init__.py`）
- **4.3** 七种格式与 `_formats` 字典（`_base.py`）
- **4.4** 统一基类 `_spbase` 与 array/matrix 两套接口（`_base.py` / `_matrix.py`）

### 4.1 稠密 vs 稀疏：内存与计算的取舍

#### 4.1.1 概念说明

设有一个 \(N \times N\) 的二维数值矩阵，其中非零元素占比（**稀疏度 / fill-in**）记为 \(f\)。例如 \(f = 0.01\) 表示 1% 的位置非零。

- **稠密存储**（NumPy `ndarray`，`float64`）所需内存：

\[
M_{\text{dense}} = N^2 \times 8 \ \text{字节}
\]

它**与稀疏度无关**——哪怕 99% 是 0，也得老老实实把这些 0 全存下来。

- **稀疏存储**（以 CSR 为例）只存非零元。每个非零元需要：一个 8 字节的数值 + 一个 4 字节的列索引（`int32`），再加上每行一个指针（共 \(N+1\) 个 `int32`）：

\[
M_{\text{sparse}} \approx (f \cdot N^2) \times 12 + (N+1) \times 4 \ \text{字节}
\]

当 \(f\) 很小、\(N\) 很大时，\(M_{\text{sparse}} \ll M_{\text{dense}}\)。

举个数：\(N=1000\)、\(f=0.01\)（约 10,000 个非零元）

\[
M_{\text{dense}} = 10^6 \times 8 = 8{,}000{,}000 \ \text{字节} \approx 7.63 \ \text{MiB}
\]

\[
M_{\text{sparse}} \approx 10{,}000 \times 12 + 1001 \times 4 \approx 124{,}000 \ \text{字节} \approx 121 \ \text{KiB}
\]

差距大约 **64 倍**。当 \(N\) 涨到 \(10^5\)，稠密存储需要 \(10^{10} \times 8 \approx 80\,\text{GiB}\)，普通机器根本存不下；而只要稀疏度足够低，稀疏存储仍然可行。

#### 4.1.2 核心流程：稀疏存储带来的「两面性」

稀疏存储不是「免费午餐」，它带来一个核心权衡：

```
                 ┌── 内存：只存非零 → 大幅省内存
稀疏存储的优势 ──┤
                 └── 计算：运算时跳过零 → 矩阵-向量乘、线性求解更快

                 ┌── 随机访问 A[i,j]：要先定位，比稠密慢
稀疏存储的代价 ──┤
                 └── 逐元素频繁改动：需要维护索引结构，效率低
```

所以「该不该用稀疏」取决于你的**访问模式**：

- 如果你的矩阵很大、大部分是 0、主要做矩阵乘法 / 求解 → **用稀疏**。
- 如果你的矩阵不大，或者你需要频繁随机读写单个元素 → **用稠密 ndarray 更简单**。

后续讲义（U2、U6）会讲每种格式各自擅长的访问模式；本讲你只需要记住这个大方向。

> 说明：本讲公式给出的是理论估算，用于建立直觉。实际字节数会因 dtype、对齐、是否含显式零等因素略有出入，**以你本地 `nbytes` / 实测为准**。

### 4.2 scipy.sparse 是什么：包文档与定位

#### 4.2.1 概念说明

`scipy.sparse` 是 SciPy 仓库下的一个**子包**（sub-package），专门提供「2-D 数值数据的稀疏数组」及其相关算法。它的入口 `__init__.py` 顶部有一段很长的文档字符串，相当于整本说明书，强烈建议你完整读一遍。

#### 4.2.2 核心流程：从文档看定位

`__init__.py` 的文档字符串第一句就把定位说清楚了：

[__init__.py:16](__init__.py)
> 这一行原文是 `SciPy 2-D sparse array package for numeric data.`（面向数值数据的 SciPy 二维稀疏数组包）。

紧接着是一段重要警告（[__init__.py:18-25](__init__.py)）：SciPy 正在**从稀疏 matrix 接口迁移到稀疏 array 接口**，未来几个版本会弃用 matrix 接口。这就是为什么本手册统一使用新式 `*_array` 接口（如 `csr_array`、`coo_array`），而把旧式 `*_matrix` 当作待移除的兼容层。

文档随后列出了全部七种可用格式（[__init__.py:127-135](__init__.py)）：

```
1. csc_array: Compressed Sparse Column format
2. csr_array: Compressed Sparse Row format
3. bsr_array: Block Sparse Row format
4. lil_array: List of Lists format
5. dok_array: Dictionary of Keys format
6. coo_array: COOrdinate format (aka IJV, triplet format)
7. dia_array: DIAgonal format
```

文档还给出了几条**关键使用建议**，本讲先记住结论，机制在后续讲义展开：

1. **高效构造**用 `coo_array` / `dok_array` / `lil_array`（[__init__.py:137-141](__init__.py)）。
2. **不要直接拿 NumPy 函数作用于稀疏数组**——NumPy 会把它当成普通 Python 对象，结果往往错误；应先 `toarray()` 转成 ndarray（[__init__.py:143-150](__init__.py)）。
3. CSR / CSC / COO 之间的互相转换都是**线性时间**的高效操作（[__init__.py:152-153](__init__.py)）。
4. 矩阵-向量乘法用 `@` 运算符（[__init__.py:160-174](__init__.py)），CSR 尤其适合快速 SpMV。

文档最后还附了两个完整示例（`Example 1` 用 `lil_array` 构造再转 CSR 求解；`Example 2` 用 `coo_array` 的坐标三元组构造），这些会成为后续讲义 U1-L4 的实操素材。

#### 4.2.3 源码精读：导入聚合与子模块

文档字符串之后是真正的 Python 代码。注意文件第 243 行有一段作者署名，点出 scipy.sparse 的历史来源：

[__init__.py:243-245](__init__.py)
> 注释写明「Original code by Travis Oliphant.」（Travis Oliphant 既是 NumPy 的创始人，也是 SciPy 的早期核心作者），并由 Ed Schofield、Robert Cimrman、Nathan Bell、Jake Vanderplas 修改扩展。这说明 scipy.sparse 是一个有近 20 年积累的成熟子包。

随后是典型的「聚合导出」写法：

[__init__.py:250-262](__init__.py)
> 这一段用 `from ._base import *`、`from ._csr import *` ……把各个格式类、构造函数、提取函数全部汇聚到 `scipy.sparse` 命名空间。所以你写 `from scipy.sparse import csr_array` 时，`csr_array` 其实定义在 `_csr.py`，只是被这里 re-export 出来。

紧接着是一段「已弃用命名空间」：

[__init__.py:265-269](__init__.py)
> 注释明确写着「Deprecated namespaces, to be removed in v2.0.0」。注意这里 import 的是不带下划线的旧模块名（`base, bsr, compressed, coo, csc, csr, ...`），它们是**单字母前缀旧模块**的兼容层。新代码请认准带下划线的新模块（`_base, _csr, _coo ...`）。

最后两行还透露了两个**子模块**：

[__init__.py:271](__init__.py)
> `_submodules = ["csgraph", "linalg"]`。也就是说 `scipy.sparse.csgraph`（图算法）和 `scipy.sparse.linalg`（稀疏线性代数）是本子包的两大应用方向，本手册 U4、U5 会专门讲它们。

#### 4.2.4 代码实践：阅读包文档

1. **实践目标**：亲手打开 scipy.sparse 的「说明书」，确认本节描述的几条结论。
2. **操作步骤**：
   - 在仓库里打开 `scipy/sparse/__init__.py`，通读顶部文档字符串（第 1–241 行）。
   - 重点圈出：七种格式列表、构造建议、`@` 矩阵乘示例、对 NumPy 函数的警告。
3. **需要观察的现象**：文档是否真的给出了 `A @ v` 的可运行示例？它推荐的构造格式是哪几种？
4. **预期结果**：你会看到 `>>> A @ v` 输出 `array([ 1, -3, -1], dtype=int64)` 这段 doctest；构造推荐 `coo_array / dok_array / lil_array`。
5. **待本地验证**：你也可以在装有 scipy 的环境里执行 `help(scipy.sparse)` 或 `print(scipy.sparse.__doc__)`，确认运行时看到的文档与此处一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么文档强烈反对「直接对稀疏数组调用 NumPy 函数」？

> **参考答案**：因为 NumPy 通常把稀疏数组当作「普通 Python 对象」而非数组，会走对象分支（object-array）逐元素处理，结果既慢又常常错误。正确做法是先查 scipy 是否提供了对应的稀疏实现；没有就先 `toarray()` 转成 ndarray 再用 NumPy。

**练习 2**：`scipy.sparse` 当前最大的接口迁移方向是什么？

> **参考答案**：从「稀疏 matrix 接口」（`*_matrix`，`*` 表示矩阵乘）迁移到「稀疏 array 接口」（`*_array`，`@` 表示矩阵乘）。`spmatrix` 接口将在未来版本弃用。

### 4.3 七种格式与 `_formats` 字典

#### 4.3.1 概念说明

上面看到 `__init__.py` 文档列了 **7 种**可用格式。但在 `_base.py` 里有一个 `_formats` 字典，登记的格式名远不止 7 种——这是理解 scipy.sparse 内部命名的一把钥匙。

#### 4.3.2 核心流程：_formats 的作用

`_formats` 是一个「格式名 → `[编号, 人类可读全称]`」的查找表，主要被两个地方用到：

- `__repr__` 打印稀疏对象时，用格式名查全称（如 `csr` → `Compressed Sparse Row`）。
- 作为「我们**可能**认识的所有格式」的登记表，方便校验用户传入的格式字符串。

注意它的注释原文是 `# The formats that we might potentially understand.`（我们**可能**认识的格式）——也就是说字典里很多格式只是「预留的编号」，并未被真正实现。

#### 4.3.3 源码精读：_formats 字典

[_base.py:36-56](_base.py#L36-L56)
> 这就是 `_formats` 字典的完整定义。可以看到它登记了 20 个格式名，每个对应一个整数编号和一段全称。其中真正被 scipy.sparse 实现并对外暴露的是这几个：`csc`(0)、`csr`(1)、`dok`(2)、`lil`(3)、`coo`(6)、`dia`(9)、`bsr`(10)——正好对应 `__init__.py` 文档里的 7 种 `*_array` 类。

字典里其余条目（如 `sss` 对称稀疏天际线、`jad` 锯齿对角、`vbr` 变块行 等）属于历史保留或预留槽位，你在公开 API 里用不到，但能从名字里窥见稀疏线性代数领域的丰富传统。

紧接着 `_formats`，`_base.py` 还定义了一个常量列表，与本讲 4.1 节的「零是隐式的」思想直接相关：

[_base.py:60-63](_base.py#L60-L63)
> `_ufuncs_with_fixed_point_at_zero` 列出了一批「以 0 为不动点」的 ufunc（如 `np.sin`、`np.sqrt`、`np.sign`）。因为对稀疏数组而言「0 是隐式的」，所以只有这类「输入 0 输出必为 0」的函数，才能保证逐元素作用于稀疏数组时结果仍正确。这个细节会在 U3-L4（逐元素运算）深入，本讲你只需理解：**「零被隐式存储」这一设计选择，深刻影响了哪些运算能高效、正确地执行**。

我们再看一下 `_formats` 在运行时怎么被用到。打印任何一个稀疏对象时，`__repr__` 会查这个表：

[_base.py:423-429](_base.py#L423-L429)
> 这就是 `__repr__` 的实现：`_, format_name = _formats[self.format]` 取出格式全称，再拼成类似 `<Compressed Sparse Row sparse array of dtype 'float64' with 10000 stored elements and shape (1000, 1000)>` 的字符串。换句话说，你在交互式环境里看到的稀疏对象「自我介绍」，全靠 `_formats` 提供格式全称。

#### 4.3.4 代码实践：观察 _formats 与 __repr__

1. **实践目标**：亲手看到 `_formats` 字典的内容，并理解它如何影响 `repr()` 输出。
2. **操作步骤**（在装有 scipy 的环境里运行）：
   ```python
   # 示例代码
   import numpy as np
   from scipy.sparse import csr_array
   from scipy.sparse._base import _formats   # 访问内部字典（仅用于学习）

   A = csr_array(([5.0, 7.0], ([0, 1], [0, 1])), shape=(3, 3))
   print(A.format)          # 期望: 'csr'
   print(_formats[A.format]) # 期望: [1, 'Compressed Sparse Row']
   print(repr(A))           # 注意 repr 里出现 'Compressed Sparse Row'
   ```
3. **需要观察的现象**：`A.format` 返回的字符串、`_formats` 查到的全称，与 `repr(A)` 文本里出现的格式名是否一致。
4. **预期结果**：`A.format` 为 `'csr'`；`_formats['csr']` 为 `[1, 'Compressed Sparse Row']`；`repr(A)` 中包含 `Compressed Sparse Row sparse array ... with 2 stored elements and shape (3, 3)`。
5. **待本地验证**：`_formats` 属内部私有对象，导入路径 `scipy.sparse._base` 在不同 scipy 版本可能变动；如导入失败，可直接对照本讲引用的源码理解，不影响后续学习。

#### 4.3.5 小练习与答案

**练习 1**：`_formats` 字典里登记了 20 个格式，但 `__init__.py` 文档只列了 7 种。多余的条目为什么存在？

> **参考答案**：`_formats` 的注释说是「我们**可能**认识的格式」。它更像一份预留登记表 / 命名规范，其中很多格式（如 `sss`、`jad`、`vbr`）未被 scipy.sparse 实现，只是占用了编号，便于将来扩展或与其他稀疏库对接。

**练习 2**：`csr`、`csc`、`coo`、`bsr`、`lil`、`dok`、`dia` 分别对应 `_formats` 里的哪个编号？

> **参考答案**：`csc`=0、`csr`=1、`dok`=2、`lil`=3、`coo`=6、`dia`=9、`bsr`=10。

### 4.4 统一基类 `_spbase` 与 array/matrix 两套接口

#### 4.4.1 概念说明

无论你用 `csr_array`、`coo_array` 还是 `dia_array`，它们都共享一大批通用能力：`.shape`、`.nnz`、`.format`、`.toarray()`、`.asformat()` 等。这些「所有稀疏类都有的公共行为」被抽到一个共同基类 `_spbase` 里。理解了 `_spbase`，就理解了所有格式的「公共底座」。

#### 4.4.2 核心流程：_spbase 提供了什么

```
        ┌─ 类属性  _format（格式名）、_allow_nd（允许的维度）、_shape（形状）
        │
_spbase ─┼─ 属性    shape / nnz / size / format / T
        │
        └─ 方法    asformat（格式互转）、toarray（转稠密）、multiply、dot ...
```

`_spbase` 自己**不能被实例化**（构造函数里有断言），它的具体行为由各格式子类提供。换句话说，`_spbase` 定义「契约」，子类实现「细节」。

#### 4.4.3 源码精读：_spbase 与两套接口

[_base.py:85-92](_base.py#L85-L92)
> 这就是共同基类 `_spbase` 的开头。注释说明它是「所有稀疏 array 和 matrix 类的基类，不能被实例化，大部分工作由子类完成」。注意三个关键类属性：`_format = 'und'`（默认格式「未定义」，子类覆盖）、`_allow_nd: tuple[int, ...] = (2,)`（默认只接受 2-D，COO 等子类会放宽到 1-D）。

它的构造函数还会主动拦截错误用法：

[_base.py:138-147](_base.py#L138-L147)
> `_spbase.__init__` 一开始就检查：如果有人直接实例化 `_spbase` 本身就抛错；并且 array 接口不允许从标量构造。`self._shape` 在这里被初始化为 `None`，由子类填上真实形状。

几个最常用的属性都定义在 `_spbase` 上：

[_base.py:149-151](_base.py#L149-L151)
> `shape` 属性只是把内部 `_shape` 暴露出来。

[_base.py:373-380](_base.py#L373-L380)
> `nnz` 返回**已存储元素个数**（注意「包括显式零」，所以它不等于「非零个数」，后者另有 `count_nonzero`）。这是衡量稀疏存储代价最直接的量——内存占用大致正比于 `nnz`。

[_base.py:392-395](_base.py#L392-L395)
> `format` 属性返回该对象当前格式的短名（如 `'csr'`），它会被 `_formats` 查表得到全称（见 4.3.3）。

格式互转的统一入口也在 `_spbase` 上：

[_base.py:471-502](_base.py#L471-L502)
> `asformat(format)` 把对象转成指定格式。逻辑很直观：如果目标格式与当前相同就直接返回（可选 copy）；否则去调用 `self.to<format>()` 方法（例如 `self.tocsr()`），找不到对应方法就报错。这就是 4.2 节里「CSR/CSC/COO 之间线性时间互转」的统一调度入口。

最后看「array vs matrix 两套接口」的分水岭——命名空间基类 `sparray`：

[_base.py:1720-1751](_base.py#L1720-L1751)
> `sparray` 被定义为一个「命名空间类（namespace class）」，用来把新式 array 类型和旧式 matrix 类型区分开。它本身只是个 mixin，没有多少实代码。配合 [`_matrix.py:1-15`](_matrix.py#L1-L15) 里的 `spmatrix`，scipy.sparse 用「是否继承 `sparray` 还是 `spmatrix`」来决定对象走哪套语义（最典型的差别：`*` 在 matrix 里是矩阵乘，在 array 里是逐元素乘）。这就是 `__init__.py` 顶部那个「迁移」警告在代码层面的体现。

> 本讲只需建立「有两套接口、新代码用 array」的认知；`*` vs `@` 的具体行为差别、MRO 继承链，会在 U2-L1 专门展开。

#### 4.4.4 代码实践：观察公共属性

1. **实践目标**：验证所有稀疏类共享的 `_spbase` 公共属性。
2. **操作步骤**（示例代码）：
   ```python
   # 示例代码
   import numpy as np
   from scipy.sparse import csr_array, coo_array

   A = csr_array(np.array([[0, 2], [0, 0], [5, 0]]))
   print("format:", A.format)   # 'csr'
   print("shape :", A.shape)    # (3, 2)
   print("nnz   :", A.nnz)      # 2
   print("repr  :", repr(A)[:60])
   B = A.asformat('coo')        # 转成 COO（见 asformat 源码）
   print("B format:", B.format) # 'coo'
   ```
3. **需要观察的现象**：不同格式对象是否都能用 `.format / .shape / .nnz / .asformat` 这些属性？`asformat` 返回的对象格式是否真的变了？
4. **预期结果**：`A.format` 为 `'csr'`，`A.nnz` 为 2；`B = A.asformat('coo')` 后 `B.format` 变成 `'coo'`，但 `B.shape`、`B.nnz` 与 A 相同（互转不改数据）。
5. **待本地验证**：可在本地实际运行确认输出。

#### 4.4.5 小练习与答案

**练习 1**：`nnz` 一定等于「非零元素个数」吗？

> **参考答案**：不一定。`nnz` 是「**已存储**元素个数，包括显式零」。一个稀疏数组完全可能存了一些值为 0 的项，此时 `nnz` 会大于真正的非零个数。后者要用 `count_nonzero()`。（显式零的清理在 U6-L1 讲。）

**练习 2**：`_spbase` 为什么设计成「不能被实例化」？

> **参考答案**：`_spbase` 只定义公共契约（`shape/nnz/format/asformat` 等），但不知道具体怎么存数据——CSR 用 `indptr/indices`，COO 用 `coords`，各不相同。真正能用的对象必须由具体格式子类构造，所以 `_spbase.__init__` 主动拦截了对自身的直接实例化。

## 5. 综合实践

把本讲三个关键结论（稀疏省内存、`csr_array` 的构造、`@` 做矩阵-向量乘）串起来，完成下面这个对比实验。这是本讲规格里指定的代码实践任务。

**任务**：构造一个 \(1000 \times 1000\)、非零占比约 1% 的稠密 `ndarray`，记录其内存占用；再用 `csr_array` 构造等价稀疏数组，对比两者大小，并各做一次矩阵-向量乘法，打印耗时差。

**操作步骤**（示例代码）：

```python
# 示例代码
import time
import numpy as np
from scipy.sparse import csr_array

rng = np.random.default_rng(0)

# 1) 稠密：1000x1000，约 1% 非零
dense = (rng.random((1000, 1000)) < 0.01).astype(np.float64)
print("dense nnz  :", int(np.count_nonzero(dense)))     # 期望 ~10000
print("dense bytes:", dense.nbytes)                      # 期望 8_000_000

# 2) 等价稀疏 (CSR)
A = csr_array(dense)
print("sparse nnz :", A.nnz)                              # 期望 ~10000
print("sparse bytes:", A.data.nbytes + A.indices.nbytes + A.indptr.nbytes)

# 3) 矩阵-向量乘法 y = A @ x
x = rng.standard_normal(1000)

t0 = time.perf_counter()
for _ in range(50):
    y_dense = dense @ x
t_dense = time.perf_counter() - t0

t0 = time.perf_counter()
for _ in range(50):
    y_sparse = A @ x
t_sparse = time.perf_counter() - t0

print(f"dense  matvec x50: {t_dense*1e3:.2f} ms")
print(f"sparse matvec x50: {t_sparse*1e3:.2f} ms")
print("结果一致:", np.allclose(y_dense, y_sparse))
```

**需要观察的现象**：

1. 稀疏版本的存储字节数是否显著小于 `dense.nbytes`？比值是否接近本讲 4.1 节估算的 ~64 倍？
2. `csr_array @ x` 的结果与 `dense @ x` 是否数值一致（`np.allclose` 为 True）？
3. 矩阵-向量乘法的耗时差：在「稀疏度很低（约 1%）」时，稀疏 SpMV 通常比稠密快得多，因为它只遍历非零元。

**预期结果**（理论值，**待本地验证**实际数字）：

- `dense.nbytes = 8,000,000` 字节。
- `sparse bytes ≈ 120,000–130,000` 字节（取决于索引 dtype）。
- 两者相乘结果一致。
- 计时差会随机器、稀疏度波动；本任务不追求精确数字，重点是**亲自看到「省内存」与「跳过零」两件事同时成立**。

> 提示：如果你的环境 `csr_array(dense)` 构造后 `nnz` 与 `count_nonzero` 不完全相等，是正常现象——见 4.4.5 关于「显式零」的讨论。

## 6. 本讲小结

- **稀疏存储**的核心思想是「只存非零元及其位置，把零视为隐式」，从而在矩阵很大且大部分为零时同时省内存、省计算。
- 稠密 ndarray 的内存正比于 \(N^2\) 且与稀疏度无关；稀疏（如 CSR）的内存正比于非零元数 \(fN^2\)，当 \(f\) 很小时优势巨大（本讲估算 ~64×）。
- `scipy.sparse/__init__.py` 的顶部文档字符串是整个子包的说明书：它列出了 7 种可用格式、构造建议、`@` 矩阵乘示例，以及「禁止直接套用 NumPy 函数」的警告。
- 该子包正处在**从 `spmatrix` 迁移到 `sparray`** 的过程中，新代码应统一使用 `*_array` 接口。
- `_base.py` 的 `_formats` 字典登记了 20 种格式名（含预留槽位），其中 7 种被真正实现；它同时被 `__repr__` 用来把短名翻译成全称。
- 所有稀疏类共享共同基类 `_spbase`，提供 `shape / nnz / format / asformat / toarray` 等公共能力；`_spbase` 不可直接实例化，具体存储由各格式子类实现。

## 7. 下一步学习建议

本讲建立了「为什么要稀疏」和「scipy.sparse 整体长什么样」的认知。下一步建议：

- **U1-L2（目录结构、入口与依赖关系）**：动手梳理 `scipy/sparse` 的目录分层，看懂 `__init__.py` 如何聚合导出、deprecated 旧命名空间如何组织、`csgraph` 与 `linalg` 两个子模块如何挂载。
- 之后 **U2** 会逐一深入 7 种格式的内部数据布局与构造方式，建议先学好 U1-L4 的第一次实操。
- 想直接看运行示例的话，可以先把 `__init__.py` 文档里的 `Example 1`、`Example 2` 抄进本地跑一遍，建立手感，再回到本讲 4.1 的内存公式对照体会。

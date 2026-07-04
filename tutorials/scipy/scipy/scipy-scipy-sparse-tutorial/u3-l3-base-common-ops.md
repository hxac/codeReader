# 基类公共操作：_base.py

## 1. 本讲目标

上一篇（u2-l1）我们已经看清了 `_spbase`、`sparray`、`spmatrix` 的类继承骨架。本讲换一个视角：**不看类怎么搭，看类能做什么**。

`_spbase`（位于 [`_base.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L85-L88)）把所有格式共享的「公共能力」集中写在基类里，七种格式子类只需按需覆盖少数方法。这些公共能力包括：

- **格式转换**：`asformat('coo')`、`tocsr()`、`toarray()` 等——在任何格式间穿梭的统一入口。
- **两种乘法**：`multiply`（逐元素乘）与 `dot`（矩阵乘），以及它们背后的 `*` 与 `@` 分发。
- **归约与对角线**：`sum`、`mean`、`diagonal`、`setdiag`——关键在于它们如何处理「隐式零」。

学完本讲你应该能：

1. 掌握 `asformat` 作为格式转换统一入口的工作方式，理解 `to{format}` 方法家族与 `toarray`/`todense` 的区别。
2. 区分 `multiply`（逐元素）与 `dot`（矩阵乘），看懂 `*`、`@`、`_matmul_dispatch` 三层分发。
3. 说出 `sum`、`mean`、`diagonal`、`setdiag` 各自如何对待隐式零，并理解背后的正确性依据。

## 2. 前置知识

本讲默认你已经掌握：

- **稀疏存储与隐式零**：稀疏数组只存非零元，零被「隐式」省略。同一个逻辑零可能在 `data` 里根本不存在。
- **`nnz` vs `count_nonzero`**：`nnz` 是「已存储元素数」（含显式零），`count_nonzero` 才是真正非零数。
- **`_spbase` / `sparray` / `spmatrix`**：所有格式的公共基类与两个命名空间标记类（u2-l1）。
- **CSR/CSC 的 `data/indices/indptr` 与 `_swap` 机制**：主轴/副轴抽象（u2-l3）。

一个贯穿全讲的核心问题：**当一个方法只看 `data` 数组时，它算对了吗？** 答案取决于「隐式零是否影响结果」。比如求和时零无所谓，但求平均值、最大值时，隐式零必须参与计算。本讲会反复回到这一点。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`_base.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L85-L88) | 定义 `_spbase`，集中提供 `asformat`、`multiply`、`dot`、`sum`、`mean`、`diagonal`、`setdiag`、`toarray` 等所有跨格式公共方法。 |
| [`_data.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_data.py#L20-L35) | 定义 `_data_matrix`（带 `.data` 属性的格式的中间基类，如 CSR/CSC/COO/DIA）与 `_minmax_mixin`，提供 `_deduped_data`、逐元素 ufunc、`_mul_scalar`、`min/max/argmin`。 |
| [`_sputils.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_sputils.py#L590-L615) | 工具函数 `_todata`（按格式安全取出非零值数组）、`get_sum_dtype`、`validateaxis`。 |
| [`_compressed.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L477-L485) | CSR/CSC 公共基类 `_cs_matrix`，提供基类方法的「高性能覆盖版」：`diagonal`（C++ 内核）、`_minor_reduce`、`_matmul_vector`、`_multiply_2d_with_broadcasting`。 |

**记忆口诀**：基类（`_base.py`）写「对就行」的通用实现，子类（`_compressed.py` 等）写「快才好」的覆盖实现。`asformat`、`dot`、`sum` 这些方法你在任何稀疏对象上都能调，但真正干活的代码可能已经被 CSR/CSC 覆盖了。

## 4. 核心概念与源码讲解

### 4.1 格式转换统一入口：asformat 与 to{format} 家族

#### 4.1.1 概念说明

七种格式各有擅长的访问模式（CSR 利于行切片、COO 利于组装、DIA 利于带状……），实际工程中经常需要在它们之间来回转换。`_spbase` 提供了两层转换 API：

- **`asformat(format)`**：最高层入口，传一个格式名字字符串，返回该格式的新对象。
- **`to{format}()` 家族**：`tocsr()`、`tocoo()`、`tocsc()`、`todia()`、`tolil()`、`todok()`、`tobsr()`，以及「离开稀疏世界」的 `toarray()`、`todense()`。

设计上的关键约束写在 [`_base.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L1308-L1310) 的一句注释里：

> Any sparse array format deriving from `_spbase` must define one of `tocsr` or `tocoo`. The other conversion methods may be implemented for efficiency, but are not required.

也就是说：**一个新格式只要实现 `tocsr()` 或 `toco()` 中的一个，就能自动获得全套转换能力**——其余转换方法会经中间格式间接完成。这是「最小实现成本换最大功能覆盖」的典型设计。

#### 4.1.2 核心流程

`asformat` 的分发逻辑很简洁：

```text
asformat(format, copy=False):
  if format 为 None 或 等于当前格式:
      return self.copy() if copy else self
  else:
      method = getattr(self, 'to' + format)   # 找 tocsr / tocoo / ...
      尝试 method(copy=copy)，失败则 method()   # 兼容不支持 copy 的老方法
```

而基类里那些「桥接」方法形成了一张转换网，核心枢纽是 `tocsr` 与 `tocoo`：

```text
基类 tocsr()  = tocoo().tocsr()
基类 tocoo()  = tocsr().tocoo()        # 互相托底
基类 tocsc()  = tocsr().tocsc()
基类 todia()  = tocoo().todia()
基类 tolil()  = tocsr().tolil()
基类 tobsr()  = tocsr().tobsr()
基类 todok()  = tocoo().todok()
```

只要某个格式自己实现了真正的 `tocsr()`（如 COO 调用 C++ 的 `coo_tocsr`），整张网就活了。

#### 4.1.3 源码精读

`asformat` 本体——注意它用字符串拼接 `'to' + format` 动态查找方法，并对不接受 `copy` 参数的旧方法做了 `try/except TypeError` 兜底：

[asformat 动态分发（_base.py:L471-L502）](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L471-L502) —— 把格式名翻译成 `to{format}` 方法调用，并按需转发 `copy` 参数。

桥接方法的典型写法（以 `tocsr`/`tocoo` 为例）：

[基类转换桥接（_base.py:L1311-L1357）](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L1311-L1357) —— `tocsr` 委托给 `tocoo().tocsr()`，`tocoo` 委托给 `tocsr().tocoo()`，构成互相托底的转换网。

离开稀疏世界的两个出口。注意 `toarray` 返回 `ndarray`，而 `todense` 会再包一层（对 `spmatrix` 包成 `np.matrix`，对 `sparray` 仍是 `ndarray`）：

[toarray / todense（_base.py:L1246-L1306）](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L1246-L1306) —— `toarray` 经 `tocoo(copy=False).toarray(...)` 落地；`todense` 用 `_ascontainer` 把结果按 array/matrix 语义重新包装。

#### 4.1.4 代码实践

1. 实践目标：验证 `asformat` 与 `to{format}` 的等价性，观察转换是否产生新对象。
2. 操作步骤（示例代码，非项目原有）：

   ```python
   import scipy.sparse as sp
   A = sp.csr_array([[1, 0, 2], [0, 0, 3], [4, 0, 5]])

   B1 = A.asformat('coo')        # 经 asformat 入口
   B2 = A.tocoo()                # 直接调 to{format}
   print(B1.format, B2.format)   # 都应是 'coo'
   print(B1 is A, B2 is A)       # 都应是 False（新对象）

   C = A.asformat('csr')         # 同格式
   print(C is A)                 # True（copy=False 时不复制）

   D = sp.csc_array(A.toarray()) # 走 toarray 出稀疏再回来
   print((D != A.tocsc()).nnz)    # 0，内容一致
   ```

3. 需要观察的现象：`asformat('coo')` 与 `tocoo()` 结果一致；同格式且 `copy=False` 时返回自身。
4. 预期结果：`coo coo` / `False False` / `True` / `0`。
5. 若环境未安装 scipy，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果你要新增一种稀疏格式 `xxx_array`，最少需要实现哪个方法，就能让 `asformat('csr')`、`asformat('coo')`、`toarray()` 都可用？

> **答案**：至少实现 `tocsr()` 或 `toco()` 其一。因为基类的 `tocoo`/`tocsr` 互相托底，`toarray` 经 `tocoo` 落地。实现了其中一个，整张转换网就接通了。

**练习 2**：`A.asformat('csr')` 与 `A.tocsr()` 在默认参数下行为是否完全一致？

> **答案**：基本一致——`asformat` 内部正是调用 `tocsr`。细微差别在于 `asformat` 多了一层「同格式直接返回 self」的短路，且对老方法做了 `copy` 参数兜底。

---

### 4.2 逐元素乘 multiply 与矩阵乘 dot

#### 4.2.1 概念说明

稀疏数组上最容易混淆的两种乘法：

| 操作 | 运算符 | 方法 | 语义 | 维度要求 |
|------|--------|------|------|----------|
| 逐元素乘 | `*`（sparray） | `multiply` | \( (A \circ B)_{ij} = A_{ij} \cdot B_{ij} \) | 形状相同（可广播） |
| 矩阵乘 | `@` | `dot` / `__matmul__` | \( (AB)_{ij} = \sum_k A_{ik} B_{kj} \) | \(A\) 的列数 = \(B\) 的行数 |

> ⚠️ 注意：`*` 的语义在 `sparray` 与 `spmatrix` 上不同（u2-l1 已讲 MRO）。对 `*_array`，`*` 命中 `_spbase.__mul__` → `multiply`（逐元素）；对 `*_matrix`，`*` 命中 `spmatrix.__mul__` → 矩阵乘。新代码请一律用 `@` 表示矩阵乘、用 `multiply()` 表示逐元素乘，避免歧义。

`dot` 是一个**薄分发器**：标量走 `*`，其余走 `@`。真正的矩阵乘逻辑在 `_matmul_dispatch` 里。

#### 4.2.2 核心流程

`multiply` 的分发（按 `other` 类型）：

```text
multiply(other):
  if other 是标量:        return _mul_scalar(other)        # data 整体乘标量
  if self 是 2-D:         return _multiply_2d_with_broadcasting(other)  # 压缩格式实现
  if self 是 >2-D(必为 COO):
      if other 是 dense:  取 self.coords 处的值逐元素乘
      if other 是 sparse: 两者 reshape 成 1×N 的 CSR，做 _binopt('_elmul_') 再 reshape 回
```

`dot` 的分发：

```text
dot(other):
  if np.isscalar(other): return self * other     # 标量→逐元素乘
  else:                  return self @ other     # 其余→矩阵乘
```

`@`（`__matmul__`）→ `_matmul_dispatch`，它按 `other` 的形状走不同快速路径：

```text
_matmul_dispatch(other):
  if other 是 ndarray 且形状匹配:  _matmul_vector / _matmul_multivector（快速路径）
  if other 是标量:                  报错（@ 不允许标量，提示用 *）
  if other 是 sparse:               _matmul_sparse（校验列数后调稀疏×稀疏内核）
  否则当作 dense 向量/矩阵:          _matmul_vector / _matmul_multivector
```

#### 4.2.3 源码精读

`multiply` 的分发主体——注意它把 2-D 情况整体甩给压缩格式的 `_multiply_2d_with_broadcasting`，>2-D 才在基类里处理（因为只有 COO 支持 >2-D）：

[multiply 分发（_base.py:L511-L566）](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L511-L566) —— 标量走 `_mul_scalar`；2-D 委托给压缩格式的广播乘法；>2-D 的 COO 把 sparse×sparse 拍平成 1×N 的 CSR 再用 `_binopt('_elmul_')`。

`dot` 极其简短——它本身不计算，只做「标量 vs 非标量」的二分：

[dot 薄分发（_base.py:L647-L676）](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L647-L676) —— 标量 `self * other`，否则 `self @ other`。

运算符到方法的绑定——这解释了为何 `*` 和 `multiply` 等价、`@` 和 matmul 等价：

[__mul__ / __matmul__ 绑定（_base.py:L969-L1010）](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L969-L1010) —— `__mul__` 直接返回 `self.multiply(other)`；`__matmul__` 先拒绝标量，再调 `_matmul_dispatch`。

矩阵乘的 ndarray 快速路径——`other` 是原生 `np.ndarray` 时按形状精确分流，避免不必要的 `asarray` 开销：

[_matmul_dispatch ndarray 快速路径（_base.py:L892-L914）](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L892-L914) —— `(N,)` 一维向量 → `_matmul_vector`；`(N,1)` 列向量 → 拍平后乘再 reshape；二维 → `_matmul_multivector`；sparse → 校验维度后 `_matmul_sparse`。

逐元素乘的标量真正实现在 `_data_matrix` 里——它揭示了「带 `.data` 的格式」如何复用同一份逐元素逻辑：

[_mul_scalar 与 _deduped_data（_data.py:L32-L35,L140-L141）](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_data.py#L140-L141) —— `_mul_scalar` 用 `_with_data(self.data * other)` 生成同结构新对象；注意标量乘零会产生显式零（这是稀疏运算的常见副作用）。

#### 4.2.4 代码实践

1. 实践目标：亲手对比 `multiply` 与 `dot`、`*` 与 `@` 的语义差异。
2. 操作步骤（示例代码）：

   ```python
   import numpy as np, scipy.sparse as sp
   A = sp.csr_array([[1, 2, 0], [0, 0, 3]])
   v = np.array([1, 0, -1])

   print("multiply 标量:\n", A.multiply(2).toarray())     # 逐元素 ×2
   print("* 标量 等价?:", (A * 2 != A.multiply(2)).nnz == 0)
   print("dot 向量:", A.dot(v))                            # 矩阵×向量 [1,-3]
   print("@ 向量 等价?:", np.array_equal(A @ v, A.dot(v)))
   ```

3. 需要观察的现象：`A.multiply(2)` 把每个非零元翻倍、零仍是零；`A @ v` 是矩阵-向量乘，结果长度 = A 的行数。
4. 预期结果：逐元素乘结果 `[[2,4,0],[0,0,6]]`；`A.dot(v)` 为 `[1, -3]`；两个等价判断均为 `True`。
5. 待本地验证：若 scipy 版本较旧，`@` 行为请以本机实测为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_spbase.dot` 对标量走 `self * other`，而 `__matmul__` 遇到标量直接报错？

> **答案**：`dot` 的设计兼容「标量乘 = 逐元素乘」的老语义（等价 `multiply`），所以标量交给 `*`；而 `@` 是纯矩阵乘运算符，数学上矩阵乘标量无定义，故 `__matmul__` 显式 `raise ValueError` 并提示改用 `*`。

**练习 2**：`A.multiply(0)` 后，`A` 的 `nnz` 会变成 0 吗？

> **答案**：不会。`multiply(0)` 把每个已存储元素变成 0，但这些「显式零」仍占用 `data` 数组，`nnz` 不变。需要再调 `eliminate_zeros()` 才会真正剔除（见 u6-l1）。

---

### 4.3 归约与隐式零：sum 与 mean

#### 4.3.1 概念说明

这是本讲最重要的概念点：**同一个「隐式零」，对不同的归约运算意义不同**。

设一个 \( 3 \times 3 \) 矩阵有 2 个非零元 \( a, b \)，其余 7 个是隐式零。

- **`sum`**：零是加法单位元，\( 0 + x = x \)。所以求和时**忽略隐式零完全正确**——直接把 `data` 数组加起来即可。
- **`mean`**：平均值 = 总和 / **元素总数**。元素总数是 9（含 7 个零），不是 2。所以 mean **必须把隐式零算进分母**。
- **`max` / `min`**（来自 `_data.py` 的 `_minmax_mixin`）：若所有非零元都为正，则最小值应是 0（隐式零），不能只看 `data`。

下表汇总（这是本讲的核心心智模型）：

| 运算 | 隐式零是否影响结果 | 基类实现如何处理 |
|------|-------------------|------------------|
| `sum` | 否（零是单位元） | `axis=None` 直接 `np.sum(_todata(self))`，只加非零值 |
| `mean` | **是**（进分母） | `(self * (1/总数)).sum()`，分母用 `math.prod(shape)` |
| `max/min` | **是**（可能是极值） | 若 `nnz != 元素总数` 则与 0 比较 |
| `diagonal` | 否（只取固定位置） | 直接按位置抽取 |

#### 4.3.2 核心流程

`sum` 的逻辑（`axis=None` 是关键路径）：

```text
sum(axis, dtype, out):
  axis = validateaxis(axis, ndim)
  if dtype 指定: 先 sum_duplicates 去重，转 dtype 后递归
  if axis is None:
      return np.sum(_todata(self))      # _todata 内部已 _deduped_data
  else:
      用 ones 矩阵做乘法求和：           # (1×M)@self 求列和，self@(N×1) 求行和
          axis=(0,) -> ones @ self      # 等价于「每列求和」
          axis=(1,) -> self @ ones      # 等价于「每行求和」
```

`mean` 的逻辑——核心是 `denom` 取的是**沿归约轴的元素总数**（含隐式零）：

```text
mean(axis, dtype, out):
  inter_self = self.astype(float64 if 整数 else self.dtype)
  denom = math.prod(shape[ax] for ax in axis)   # 元素总数，含零！
  res = (inter_self * (1.0/denom)).sum(axis=axis)  # 先缩放 data，再求和
  return res
```

为什么 `mean` 这样写是对的？因为 \( \text{mean} = \frac{1}{n}\sum x_i = \sum \frac{x_i}{n} \)。先把每个存储元除以 \( n \)，再求和；隐式零乘 \( 1/n \) 仍是零、不进 `data`，所以不会被显式相加，但 \( n \) 已经包含了它们——分母正确。

#### 4.3.3 源码精读

`sum` 的 `axis=None` 路径——它只对「去重后的 data」求和，隐式零天然不参与（因为零不影响和）：

[sum 的 axis=None 与 ones 乘法路径（_base.py:L1490-L1520）](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L1490-L1520) —— 全局求和用 `np.sum(_todata(self))`；按轴求和用 `ones @ self`（列和）或 `self @ ones`（行和）。

`_todata` 为什么对求和是安全的——它对 `_data_matrix` 子类返回 `_deduped_data()`（先合并重复坐标），所以重复项不会导致重复计数：

[_todata 去重取值（_sputils.py:L590-L615）](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_sputils.py#L590-L615) —— 按格式分支取出非零值数组；对带 `.data` 的格式先 `_deduped_data()`，对 DOK/LIL 走专用路径。

`mean` 如何把隐式零算进分母——`denom = math.prod(self.shape[ax] for ax in axis)`，正是「沿归约轴的元素总数」：

[mean 的缩放-求和（_base.py:L1570-L1577）](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L1570-L1577) —— `denom` 用形状乘积（含隐式零）；`(inter_self * (1.0/denom)).sum(axis=axis)` 先缩放再求和。

（拓展）CSR/CSC 对副轴求和的高性能覆盖——用 `_minor_reduce(np.add)` 直接在 `indptr` 分段上 `reduceat`，比「乘 ones」快得多：

[压缩格式 sum 快速路径（_compressed.py:L493-L516）](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L493-L516) —— 仅对副轴走快速路径，主轴或 nD 仍回退到 `_spbase.sum`。

#### 4.3.4 代码实践

1. 实践目标：亲眼看到 `sum` 与 `mean` 对隐式零的不同处理。
2. 操作步骤（示例代码）：

   ```python
   import numpy as np, scipy.sparse as sp
   A = sp.csr_array(([5.0, 7.0], ([0, 2], [0, 2])), shape=(3, 3))  # 只有 2 个非零
   dense = A.toarray()
   print("nnz:", A.nnz, " 元素总数:", 9)

   print("sum(axis=None):", A.sum())          # = 12，等价 np.sum(dense)
   print("sum(axis=0):", A.sum(axis=0).toarray())
   print("mean(axis=None):", A.mean())        # = 12/9，不是 12/2！
   print("对比 np.mean(dense):", np.mean(dense))
   ```

3. 需要观察的现象：`A.sum()` = 12；`A.mean()` ≈ 1.333（=12/9），与 `np.mean(dense)` 一致；若误把 mean 当成「sum/nnz」会得到 6（=12/2），是错的。
4. 预期结果：`sum=12.0`，`mean≈1.3333`，两者与稠密版完全相同。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：一个全零的 `csr_array((3,3))`（`nnz=0`），调用 `sum()` 和 `mean()` 分别返回什么？

> **答案**：`sum()` 返回 `0`（`_spbase.sum` 在 `nnz==0` 时走 `np.sum([0])` 分支）；`mean()` 返回 `0.0`（总和 0 除以 9 仍为 0）。两者都不会报错。

**练习 2**：为什么不直接把 `mean` 实现成 `self.sum(axis) / self.nnz`？

> **答案**：因为分母错了。`mean` 要除以**元素总数**（含隐式零），而 `nnz` 只是已存储元素数。`sum/nnz` 会高估平均值。正确分母是 `math.prod(shape[ax])`，所以基类写成 `(self * (1/总数)).sum()`。

---

### 4.4 对角线操作：diagonal 与 setdiag

#### 4.4.1 概念说明

对角线操作有读取和写入两类：

- **`diagonal(k=0)`**：返回第 `k` 条对角线（\( a[i, i+k] \)）上的值，返回 1-D `ndarray`。`k=0` 是主对角线，`k>0` 是上对角线，`k<0` 是下对角线。
- **`setdiag(values, k=0)`**：把 `values` 写到第 `k` 条对角线上。
- **`trace(offset=0)`**：`diagonal(offset).sum()`，即对角线元素之和。

这两个方法看似简单，却体现了 `_spbase` 的「通用兜底 + 子类覆盖」模式：基类用「逐元素赋值」的笨办法保证正确，CSR/CSC 用 C++ 内核 `csr_diagonal` 提速。

#### 4.4.2 核心流程

`diagonal`：

```text
diagonal(k):
  return self.tocsr().diagonal(k=k)     # 基类委托给 CSR 的覆盖版
```

CSR 的 `diagonal`（高性能）：

```text
用 _swap 把 CSR/CSC 问题统一成 CSR:
  M, N = self._swap(self.shape)
  k, _ = self._swap((k, -k))
  调用 C++ 内核 csr_diagonal(k, M, N, indptr, indices, data, y) 抽取
```

`setdiag`：

```text
setdiag(values, k):
  校验 k 是否越界 (k>=N 或 -k>=M 报错)
  self._setdiag(np.asarray(values), k)   # 基类默认实现
```

`_setdiag` 的默认实现——按对角线逐位置赋值（格式可覆盖以提速）：

```text
对每个对角线位置 i:
  if values 是标量: self[i, i+k] = values
  else:             self[i, i+k] = values[i]
```

> 注意 `setdiag` 会改变稀疏结构（写入新位置），对 CSR 这意味着重建 `indptr/indices/data`，每次 `self[i,j] = v` 都代价不菲——这是典型的低效模式（u6-l5 会讲 `SparseEfficiencyWarning`）。若要整条对角线赋值，更好的做法是用 `diags_array` 重新构造（见 u3-l1）。

#### 4.4.3 源码精读

基类 `diagonal` 委托——一行就把活交给 CSR：

[diagonal 委托（_base.py:L1580-L1609）](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L1580-L1609) —— `return self.tocsr().diagonal(k=k)`；含文档示例 `A.diagonal(k=1) → array([2, 3])`。

CSR 的 C++ 内核版——用 `_swap` 让 CSR 与 CSC 共用同一份 `csr_diagonal` 代码：

[compressed.diagonal 与 _swap（_compressed.py:L477-L485）](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L477-L485) —— 先 `_swap` 把形状与 `k` 统一到 CSR 视角，再调 `csr_diagonal` 内核抽取对角线到预分配的 `y`。

`setdiag` 的越界校验 + 委托，以及默认逐元素赋值实现：

[setdiag 与 _setdiag（_base.py:L1627-L1680）](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L1627-L1680) —— 先校验 `k` 是否超出维度；`_setdiag` 按 `k` 正负分两支，沿对角线逐位置 `self[i, i+k] = v`（标量则广播）。

#### 4.4.4 代码实践

1. 实践目标：读取与设置对角线，观察 `setdiag` 改变稀疏结构。
2. 操作步骤（示例代码）：

   ```python
   import numpy as np, scipy.sparse as sp

   def fill_main_diag(A, values):
       """把 values 填到 A 的主对角线上，返回新对象。"""
       B = A.copy()
       B.setdiag(values)
       return B

   A = sp.csr_array((3, 3))
   B = fill_main_diag(A, [10, 20, 30])
   print(B.toarray())
   print("diagonal():", B.diagonal())        # [10 20 30]
   print("diagonal(k=1):", B.diagonal(k=1))  # 空数组
   print("trace():", B.trace())              # 60
   ```

3. 需要观察的现象：`setdiag` 把原本全零的矩阵变成了对角阵，`nnz` 从 0 变为 3；`diagonal(k=1)` 返回空数组（上对角线无元素）。
4. 预期结果：`B.toarray()` 为对角 `[[10,0,0],[0,20,0],[0,0,30]]`，`trace()` = 60。
5. 待本地验证：`setdiag` 在 CSR 上是较慢操作，大矩阵请改用 `diags_array`。

#### 4.4.5 小练习与答案

**练习 1**：对一个非方矩阵 `csr_array((2, 3))`，`diagonal()` 返回多长的数组？`setdiag([1,2,3], k=0)` 会发生什么？

> **答案**：`diagonal()` 返回长度 `min(M, N) = 2` 的数组。`setdiag([1,2,3])` 只会写前 2 个值（`min(M, N-k, len(values))`），第 3 个值被忽略。

**练习 2**：为何 CSR 上对整条对角线赋值，推荐用 `diags_array` 而非 `setdiag`？

> **答案**：`setdiag` 的默认实现逐位置 `self[i,i] = v`，每次写入都触发 CSR 稀疏结构重建，O(n) 次 × 每次重建 = 很慢。`diags_array` 直接按 DIA→CSR 构造，一次成型，是 O(nnz) 的正道（见 u3-l1 构造函数）。

---

## 5. 综合实践

把本讲的 `asformat`、`multiply`、`dot`、`sum`、`diagonal`、`setdiag` 串成一个完整任务：**手工构造一个小线性系统，做一次「行归一化」并验证**。

任务：给定稀疏矩阵 \( A \)，计算行归一化矩阵 \( D^{-1}A \)，其中 \( D = \mathrm{diag}(\text{每行绝对值之和}) \)。要求：

1. 用坐标三元组构造一个 4×4 的 `csr_array`（含一行全零）。
2. 用 `sum(axis=1)` 取每行绝对值之和（提示：先 `abs()`）。
3. 用 `setdiag` 构造对角阵 \( D \)，再用 `diagonal()` 回读验证。
4. 用 `dot`（即 `@`）计算 \( D^{-1} A \)，用 `multiply` 做一次逐元素对照。
5. 全程用 `asformat` 至少切换一次格式，最后 `toarray()` 打印结果。

参考思路（示例代码，非项目原有）：

```python
import numpy as np, scipy.sparse as sp

# 1) 构造
A = sp.csr_array(([3., -2., 4., 1., 5.],
                  ([0, 0, 1, 2, 3], [0, 1, 1, 2, 3])), shape=(4, 4))

# 2) 每行绝对值之和
row_sums = abs(A).sum(axis=1).toarray().ravel()      # [5, 4, 1, 5]，第 2 行原为 0→0? 自检
row_sums[row_sums == 0] = 1                            # 避免除零

# 3) 构造对角阵 D 并回读
D = sp.csr_array((4, 4))
D.setdiag(row_sums)
assert np.array_equal(D.diagonal(), row_sums)

# 4) D^{-1} A：逐行除以行和（用 multiply 做逐元素除法对照）
Dinv = sp.csr_array((4, 4)); Dinv.setdiag(1.0 / row_sums)
normalized = Dinv @ A                                  # dot / 矩阵乘

# 5) 切换格式 + 打印
print(normalized.asformat('coo').toarray())
print("列和:", normalized.sum(axis=0).toarray())
```

**自检要点**：全零行经 `row_sums==0 → 1` 处理后保持全零；`Dinv @ A` 用的是矩阵乘（`dot`），而若误用 `Dinv.multiply(A)` 会得到逐元素乘的错误结果——这正是本讲强调的 `multiply` vs `dot` 区别。运行后请核对你理解的 `sum`（忽略隐式零）与 `mean`（含隐式零分母）是否一致。

> 待本地验证：上述代码的具体数值结果请以本机运行为准。

## 6. 本讲小结

- `_spbase` 把**所有格式共享的公共操作**写在基类，子类只需覆盖少数方法（如 CSR/CSC 覆盖 `diagonal`、`_matmul_vector` 提速）。
- **`asformat(format)` 是格式转换的统一入口**，内部通过 `getattr(self, 'to'+format)` 找到 `to{format}` 方法；新格式只要实现 `tocsr` 或 `tocoo` 之一，就接通整张转换网。`toarray`/`todense` 是离开稀疏世界的出口。
- **`multiply` 是逐元素乘、`dot` 是矩阵乘**。`dot` 是薄分发器（标量→`*`，其余→`@`）；`*` 对 `sparray` 等价 `multiply`，`@` 走 `_matmul_dispatch`。
- **隐式零的处因运算而异**：`sum` 可忽略（零是单位元，且 `_todata` 已去重）；`mean` 必须算进分母（`denom = math.prod(shape)`）；`max/min` 要与零比较；`diagonal` 只按位置抽取。
- **`setdiag` 改变稀疏结构**，在 CSR 上是较慢操作，整条对角线赋值更推荐 `diags_array`；`diagonal` 在 CSR/CSC 用 `_swap` + C++ 内核 `csr_diagonal` 高效实现。

## 7. 下一步学习建议

- **横向**：本讲的 `_data_matrix`（`_data.py`）是带 `.data` 格式的中间基类，下一篇 u3-l4 会专门讲它的逐元素 ufunc 注入机制（`_ufuncs_with_fixed_point_at_zero` 循环）与 `_with_data` 复制契约，与本讲的 `_mul_scalar`、`_deduped_data` 紧密衔接。
- **纵向**：想深入「格式互转的真实代价」（去重、排序、`has_canonical_format` 不变量），直接进 u6-l1（格式转换、去重与 canonical format）。
- **工具层**：本讲多次提到 `_todata`、`get_sum_dtype`、`validateaxis`，它们都来自 `_sputils.py`，u3-l5 会系统讲这个工具函数库。
- **源码练习**：打开 [`_base.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L85-L88) 的 `sum` 方法，找到 `axis=None` 路径，再打开 [`_compressed.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L493-L516) 的覆盖版 `sum`，对比「乘 ones」与 `_minor_reduce` 两种实现的差异——这是理解「基类兜底 + 子类覆盖」最好的练习。

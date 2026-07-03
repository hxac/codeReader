# BSR 块稀疏行格式

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 BSR（Block Sparse Row，块稀疏行）格式与 CSR 格式的本质区别：**BSR 的存储原子是「一个 R×C 稠密小块」而不是单个标量**。
- 解释 BSR 的三数组 `data / indices / indptr` 为什么按「块」而不是按「元素」组织，并理解 `data` 为什么是三维的。
- 掌握 `blocksize=(R,C)` 这个核心参数的约束（`M % R == 0` 且 `N % C == 0`）以及它对存储与效率的影响。
- 读懂 `_bsr_base.__init__` 对五类输入的分发逻辑，知道从稠密数组构造 BSR 时块大小是怎么定的。
- 理解 `estimate_blocksize` / `count_blocks` 这两个工具如何根据矩阵的非零结构自动「猜」出一个合适的块大小。
- 明白 BSR 为什么特别适合「向量值有限元」这类带稠密块结构的稀疏矩阵。

本讲承接 [u2-l3](u2-l3-csr-csc-format.md)：CSR/CSC 用 `data/indices/indptr` 三数组按行/列压缩存储标量非零元，BSR 沿用了这套「压缩行」骨架，只是把每个条目换成了一个稠密块。如果你还没读过 CSR/CSC 讲义，建议先回去看 `_swap` 与主轴/副轴的概念。

## 2. 前置知识

在进入 BSR 之前，请确认你已经熟悉下面这些概念（前几讲已建立）：

- **稀疏存储的基本思想**：只存非零元及其位置，零是隐式的（见 [u1-l1](u1-l1-sparse-overview.md)）。
- **CSR 三数组**：`data` 存值、`indices` 存列号、`indptr`（长度 = 行数 + 1）划定每行段，且 `nnz = indptr[-1]`（见 [u2-l3](u2-l3-csr-csc-format.md)）。
- **`_cs_matrix` 公共基类与 `_swap` 机制**：CSR/CSC 共用逻辑靠 `_swap` 区分主轴（见 [u2-l3](u2-l3-csr-csc-format.md)）。
- **`sparray` / `spmatrix` 两套接口**：新代码用 `*_array`，旧代码用 `*_matrix`（见 [u2-l1](u2-l1-class-hierarchy.md)）。
- **`nnz` 的含义**：已存储元素数（含显式零），区别于真正非零的 `count_nonzero`。

为了帮助你建立直觉，先看一张「同一个矩阵，CSR 视角 vs BSR 视角」的对比（**示例代码**，非项目源码）：

```python
# 一个 4x4 矩阵，左上和右下各有一个 2x2 稠密块
dense = np.array([
    [1, 2, 0, 0],
    [3, 4, 0, 0],
    [0, 0, 5, 6],
    [0, 0, 7, 8],
])
```

- **CSR 视角**：8 个非零标量，`data` 长度 = 8，`indices` 长度 = 8。
- **BSR 视角（blocksize=(2,2)）**：只有 **2 个块**，`data` 长度 = 2（但每个元素是一个 2×2 小矩阵），`indices` 长度 = 2（存的是**块列号** 0 和 1），`indptr = [0, 1, 2]`（每个块行一个块）。

同样的矩阵，BSR 的「索引开销」只有 CSR 的 1/4——这就是 BSR 的核心收益。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`_bsr.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_bsr.py) | BSR 格式的全部实现：`_bsr_base` 基类 + `bsr_array` / `bsr_matrix` 两个容器类，包含构造、`blocksize`、`nnz`、块级乘法、转置、格式互转。 |
| [`_compressed.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py) | `_cs_matrix` 公共基类。`_bsr_base` 继承它，复用「压缩行」的概念骨架。 |
| [`_csr.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py) | `_csr_base.tobsr`：CSR→BSR 的真正转换入口，调 C++ 内核 `csr_tobsr`，并在缺省时调 `estimate_blocksize`。 |
| [`_spfuncs.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_spfuncs.py) | `estimate_blocksize`（猜块大小）与 `count_blocks`（数块）两个工具函数。 |

> 选文件口诀：BSR 的「肉」在 `_bsr.py`，但「块大小怎么定」要追到 `_csr.py` 的 `tobsr` 和 `_spfuncs.py`。

---

## 4. 核心概念与源码讲解

### 4.1 BSR 是「块化的 CSR」

#### 4.1.1 概念说明

CSR 的存储原子是**一个标量非零值**：`data[k]` 是一个数，`indices[k]` 是它所在列。BSR 的存储原子是**一个 R×C 的稠密小块**：`data[k]` 是一整个 `R×C` 的矩阵，`indices[k]` 是这个块所在的**块列号**。

换句话说，BSR = 「先把矩阵按 `blocksize=(R,C)` 切成块网格，再对块网格做 CSR」。这个类比是理解 BSR 的一把钥匙：

| 维度 | CSR | BSR |
|------|-----|-----|
| 存储原子 | 标量 | R×C 稠密块 |
| `data` 形状 | 1-D `(nnz,)` | 3-D `(n_blocks, R, C)` |
| `indices[k]` 含义 | 列号 ∈ `[0, N)` | **块列号** ∈ `[0, N//C)` |
| `indptr` 长度 | `M + 1`（每行一段） | `M//R + 1`（每**块行**一段） |
| `nnz` | `indptr[-1]` | `indptr[-1] * R * C` |

为什么要有 BSR？当一个稀疏矩阵的**非零元天然聚集成稠密小块**时（最典型的就是向量值有限元离散、多自由度节点），用 CSR 会浪费大量索引：每个标量都要配一个列号。BSR 把整个块当成一个单位，索引数缩小 R×C 倍，运算时还能直接套用 BLAS 风格的稠密小块乘法，缓存友好得多。`bsr_array` 的文档字符串对此有明确说明（见下方源码精读）。

#### 4.1.2 核心流程

BSR 的「块行压缩」可以这样描述（伪代码）：

```
给定 M×N 矩阵，blocksize=(R,C)，要求 M%R==0 且 N%C==0
1. 把矩阵切成 (M//R) × (N//C) 的「块网格」
2. 遍历每个块行 i ∈ [0, M//R):
     对该块行中所有「非空块」j（块列号）:
        data[ptr]   = 这个 R×C 小块的稠密内容
        indices[ptr] = j          # 块列号，不是标量列号
        ptr += 1
     indptr[i+1] = ptr            # 标记块行 i 的结束
3. nnz = indptr[-1] * (R*C)       # 每个块贡献 R*C 个标量槽位
```

注意第 3 步：`nnz` 把每个存储块**整块**的 R×C 个槽位都算进去，哪怕块内部有零——这正是 BSR 与 CSR `nnz` 语义的关键差别（见 4.2 节）。

#### 4.1.3 源码精读

BSR 的实现类 `_bsr_base` 直接继承 `_cs_matrix`，复用「压缩行」的骨架：

[_bsr.py:L24-L25](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_bsr.py#L24-L25) —— `_bsr_base` 继承 `_cs_matrix`（压缩行公共基类）和 `_minmax_mixin`（min/max 能力），并设置 `_format = 'bsr'`。

但要注意：`_bsr_base` **重写了** `_cs_matrix` 的大量方法（`__init__`、`_getnnz`、`tocsr`、`transpose`、`eliminate_zeros` 等），因为 CSR 那套按标量的逻辑到了块级别必须改写。例如矩阵-向量乘：

[_bsr.py:L273-L283](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_bsr.py#L273-L283) —— `_matmul_vector` 调 C++ 内核 `bsr_matvec`，注意它把 `self.data.ravel()`（把三维块数据拍平成一维）传进去，参数是 `M//R, N//C, R, C`（块网格尺寸 + 块大小），而不是 CSR 的 `M, N`。这说明内核在**块网格**上做 SpMV，每命中一个块就做一次 `R×C` 与 `C` 向量的稠密乘加。

文档字符串把适用场景说得很直白：

[_bsr.py:L720-L727](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_bsr.py#L720-L727) —— 「BSR 适合带稠密子矩阵的稀疏矩阵……这类块稀疏矩阵常出现于**向量值有限元离散**，此时 BSR 比 CSR/CSC 高效得多」。

#### 4.1.4 代码实践

**实践目标**：直观感受 BSR「按块压缩」与 CSR「按标量压缩」在索引开销上的差别。

**操作步骤**（示例代码）：

```python
import numpy as np
from scipy.sparse import bsr_array

dense = np.array([
    [1, 2, 0, 0],
    [3, 4, 0, 0],
    [0, 0, 5, 6],
    [0, 0, 7, 8],
])

bsr = bsr_array(dense, blocksize=(2, 2))
csr = bsr.tocsr()

print("bsr.indptr   =", bsr.indptr)      # [0 1 2]      长度 = M//R + 1 = 3
print("bsr.indices  =", bsr.indices)     # [0 1]        长度 = 块数 = 2
print("bsr.data.shape =", bsr.data.shape) # (2, 2, 2)   三维！
print("csr.indices  =", csr.indices)     # 长度 = 8（每个标量一个列号）
print("len(bsr.indices) =", len(bsr.indices), " len(csr.indices) =", len(csr.indices))
```

**需要观察的现象**：`bsr.indices` 只有 2 个元素，`csr.indices` 有 8 个；`bsr.data` 是 `(2,2,2)` 的三维数组。

**预期结果**：BSR 的索引数组长度是 CSR 的 1/4（因为 blocksize 是 2×2=4），这正是 BSR 省索引开销的体现。如果你拿到的是一个更大的、块结构更明显的矩阵，差距会更大。

#### 4.1.5 小练习与答案

**练习 1**：把上面的 `dense` 改成一个 6×6、含 9 个 2×2 稠密块（即完全稠密）的矩阵，`bsr.indices` 和 `csr.indices` 分别多长？

**答案**：`bsr.indices` 长度 = 块数 = (6/2)×(6/2) = 9；`csr.indices` 长度 = 36（全部标量）。比值仍是 1/4。

**练习 2**：BSR 有没有像 CSC 那样的「列优先」对应物（BSC）？为什么 `_bsr_base` 不需要 `_swap` 机制？

**答案**：SciPy 公开接口里没有 `bsc_array`/`bsc_matrix`（BSR 只有行优先这一种）。`_bsr_base` 重写了构造与运算、不调用 `_cs_matrix.__init__` 里用到 `_swap` 的分支，且不支持 `__getitem__`（见 4.3.3），所以不需要 `_swap` 来区分主轴/副轴——它永远是「块行」为主轴。

---

### 4.2 blocksize 约束与三数组布局

#### 4.2.1 概念说明

`blocksize=(R,C)` 是 BSR 区别于 CSR 的灵魂参数。它规定：

1. **每个存储块**是 `R` 行 `C` 列的稠密矩阵，所以 `data` 是三维的：`(块数, R, C)`。
2. **矩阵形状必须能被块大小整除**：`M % R == 0` 且 `N % C == 0`，否则报错。
3. **`blocksize` 不是独立存储的字段**，而是直接从 `data.shape` 读出来：`blocksize = data.shape[1:]`。

`nnz` 的语义也因此和 CSR 不同：CSR 的 `nnz = indptr[-1]`（标量数），BSR 的 `nnz = indptr[-1] * R * C`（块数 × 每块槽位数）。这意味着**BSR 的 `nnz` 把每个存储块内部的零也算进去**——这是后面实践任务的核心观察点。

#### 4.2.2 核心流程

三数组的布局可以这样记：

```
data   : shape (n_blocks, R, C)   # 第 k 个块是 data[k]，一个 R×C 矩阵
indices: shape (n_blocks,)        # indices[k] = 第 k 个块的「块列号」∈[0, N//C)
indptr : shape (M//R + 1,)        # indptr[i]:indptr[i+1] = 块行 i 包含的块在 data/indices 中的下标段
nnz    = indptr[-1] * R * C       # 每个块贡献 R*C 个标量槽位（含块内零）
```

举个具体例子（**示例代码**，对应 `bsr_array` 文档里的第三个 Example）：

```
indptr  = [0, 2, 3, 6]            # 3 个块行，分别含 2、1、3 个块
indices = [0, 2, 2, 0, 1, 2]      # 6 个块各自的块列号
data    = shape (6, 2, 2)          # 6 个 2×2 块
nnz     = indptr[-1] * 2 * 2 = 6 * 4 = 24
```

#### 4.2.3 源码精读

`blocksize` 是一个只读属性，直接从 `data` 的后两维推出：

[_bsr.py:L212-L215](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_bsr.py#L212-L215) —— `blocksize` 返回 `self.data.shape[1:]`。这就是为什么构造 BSR 时不需要单独存 R、C：它们就藏在 `data` 的形状里。

`nnz` 的计算体现了「按块计数」的语义：

[_bsr.py:L217-L222](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_bsr.py#L217-L222) —— `_getnnz` 返回 `int(self.indptr[-1]) * R * C`，即「块数 × 每块槽位」。注意它**不**扣除块内部的零。

对照 CSR 的 `nnz`（`indptr[-1]`，纯标量计数），你能清楚看到两者语义的差别。`__repr__` 也特意把 `blocksize` 打印出来，因为它对理解一个 BSR 对象至关重要：

[_bsr.py:L233-L240](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_bsr.py#L233-L240) —— repr 里会出现 `blocksize=Rx C` 字样，帮你一眼看出块大小。

#### 4.2.4 代码实践

**实践目标**：验证 BSR 的 `nnz` 把「块内零」也计入，从而可能大于「真正非零数」。

**操作步骤**（示例代码）：

```python
import numpy as np
from scipy.sparse import bsr_array

# 左上 2x2 块里故意放一个 0
dense = np.array([
    [1, 0, 0, 0],
    [3, 4, 0, 0],
    [0, 0, 5, 6],
    [0, 0, 7, 8],
], dtype=float)

bsr = bsr_array(dense, blocksize=(2, 2))
csr = bsr.tocsr()
csr.eliminate_zeros()   # 去掉 CSR 里的显式零

print("bsr.nnz                  =", bsr.nnz)            # 2 块 × 4 = 8（含块内零）
print("csr.nnz (before elim)    =", bsr.tocsr().nnz)    # 8（BSR→CSR 不丢块内零）
print("csr.nnz (after elim zeros)=", csr.nnz)           # 7（去掉了那个块内零）
print("bsr.count_nonzero()      =", bsr.count_nonzero())# 7（真正非零数）
```

**需要观察的现象**：`bsr.nnz` 是 8，但真正非零只有 7 个；`count_nonzero()` 调用 `_deduped_data()` 后用 `np.count_nonzero` 才给出真实的 7。

**预期结果**：`bsr.nnz == 8 > bsr.count_nonzero() == 7`。这说明 BSR 会为了「整块存储」而在块内零上付出槽位代价——这正是 BSR 不适合「块内部也很稀疏」的矩阵的原因。注意 `_getnnz` 还明确不支持按 axis 计数：

[_bsr.py:L217-L220](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_bsr.py#L217-L220) —— `_getnnz(axis=...)` 直接 `raise NotImplementedError`。

#### 4.2.5 小练习与答案

**练习 1**：一个 1000×1000、`blocksize=(5,5)` 的 BSR 矩阵，`indptr` 长度是多少？

**答案**：`M//R + 1 = 1000//5 + 1 = 201`。

**练习 2**：为什么 BSR 的 `nnz` 要「连块内零一起算」，而不是只算真正非零？

**答案**：因为 `data` 数组按整块分配内存，每个存储块固定占用 R×C 个连续槽位（不论内部是否有零）。`nnz` 反映的是「已分配的标量槽位数」，这与存储结构一致；想知道真正非零数要用 `count_nonzero()`。

---

### 4.3 `_bsr_base.__init__` 的构造分发

#### 4.3.1 概念说明

`bsr_array` / `bsr_matrix` 的构造器和 CSR 类似，接受多种输入：稠密数组、其它稀疏对象、纯形状、`(data,(row,col))` 坐标三元组、`(data,indices,indptr)` 原生 BSR 三元组。但 BSR 多了一个关键参数 `blocksize`，构造器需要在每个分支里处理它。

最有意思的一点：当你用**稠密数组**或**坐标三元组**构造、却没给 `blocksize` 时，构造器会走 `tobsr(blocksize=None)`，最终调 `estimate_blocksize` **自动猜**一个块大小（见 4.4 节）。

#### 4.3.2 核心流程

`_bsr_base.__init__` 的分发树：

```
arg1 是稀疏对象?
 ├─ 是 → arg1.tobsr(blocksize=...) 转成 BSR，直接搬 indptr/indices/data/shape
 └─ 否 → arg1 是 tuple?
          ├─ 是 → isshape(arg1)?         # (M,N) 纯形状
          │       → 建空矩阵: data=zeros((0,)+blocksize), 校验 M%R==0 且 N%C==0
          │       →   blocksize 为 None 时默认 (1,1)
          │
          │      len(arg1)==2?           # (data,(row,col)) 坐标三元组
          │       → 经 COO 中转: coo.tobsr(blocksize=...)
          │
          │      len(arg1)==3?           # (data,indices,indptr) 原生 BSR
          │       → 校验 data.ndim==3、blocksize 与 data.shape[1:] 一致
          │
          └─ 否（稠密）→ arg1 = np.asarray(arg1); coo.tobsr(blocksize=...)
最后: 推断/校验 shape，check_format(full_check=False) 做基本合法性检查
```

#### 4.3.3 源码精读

构造器签名与 `blocksize` 参数：

[_bsr.py:L27-L29](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_bsr.py#L27-L29) —— `__init__(self, arg1, shape=None, dtype=None, copy=False, blocksize=None, *, maxprint=None)`，比 CSR 多了 `blocksize`。

纯形状分支最能体现「块」的逻辑——`data` 一开始就建成三维，并强制校验整除：

[_bsr.py:L41-L62](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_bsr.py#L41-L62) —— 注意三处关键：(1) `blocksize is None` 时默认 `(1,1)`；(2) `self.data = np.zeros((0,) + blocksize, ...)` 是**三维**的（哪怕没元素，末两维也保留 R、C）；(3) `if (M % R) != 0 or (N % C) != 0: raise ValueError('shape must be multiple of blocksize')` —— 这就是整除约束；(4) `indptr = np.zeros(M//R + 1, ...)`，长度按**块行**算。

原生 `(data,indices,indptr)` 分支校验 `data` 必须三维、块大小必须自洽：

[_bsr.py:L72-L101](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_bsr.py#L72-L101) —— `if self.data.ndim != 3: raise ValueError('BSR data must be 3-dimensional, ...')`，并要求传入的 `blocksize` 与 `self.data.shape[1:]` 完全一致，否则报 `mismatching blocksize`。

稠密分支会拒绝非二维输入（BSR 不支持 1-D）：

[_bsr.py:L104-L116](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_bsr.py#L104-L116) —— `if isinstance(self, sparray) and arg1.ndim != 2: raise ValueError("BSR arrays don't support ND input. Use 2D")`，然后经 COO 中转 `tobsr(blocksize=...)`。

最后，构造结束前调用 `check_format(full_check=False)` 做 O(1) 基本检查：

[_bsr.py:L144-L188](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_bsr.py#L144-L188) —— 校验 `data.ndim==3`、`len(indptr)==M//R+1`、`indptr[0]==0`、`len(indices)==len(data)`、`indptr[-1] <= len(indices)` 等不变量。

需要特别提醒一个限制：BSR **不支持切片/逐项读写**：

[_bsr.py:L260-L264](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_bsr.py#L260-L264) —— `__getitem__` 和 `__setitem__` 直接 `raise NotImplementedError`。文档里也明说「Block Sparse Row format sparse arrays do not support slicing」。要做元素级访问，先 `tocsr()` 或 `tocoo()`。

#### 4.3.4 代码实践

**实践目标**：用四种方式构造同一个 BSR 矩阵，体会构造分发；并触发一次整除约束报错。

**操作步骤**（示例代码）：

```python
import numpy as np
from scipy.sparse import bsr_array

target = np.array([
    [1, 2, 0, 0],
    [3, 4, 0, 0],
    [0, 0, 5, 6],
    [0, 0, 7, 8],
], dtype=float)

# 方式 1: 稠密数组 + 显式 blocksize
a = bsr_array(target, blocksize=(2, 2))

# 方式 2: 原生三数组 (data, indices, indptr)
indptr  = np.array([0, 1, 2])
indices = np.array([0, 1])
data    = np.array([[[1, 2], [3, 4]], [[5, 6], [7, 8]]], dtype=float)  # (2,2,2)
b = bsr_array((data, indices, indptr), shape=(4, 4))

# 方式 3: 纯形状建空
c = bsr_array((4, 4), blocksize=(2, 2), dtype=float)

# 方式 4: 坐标三元组经 COO 中转
row = [0, 0, 1, 1, 2, 2, 3, 3]
col = [0, 1, 0, 1, 2, 3, 2, 3]
vals = [1, 2, 3, 4, 5, 6, 7, 8]
d = bsr_array((vals, (row, col)), shape=(4, 4), blocksize=(2, 2))

print(np.array_equal(a.toarray(), b.toarray()), a.toarray().tolist())
print("blocksize:", a.blocksize, " data.ndim:", a.data.ndim)

# 触发整除约束
try:
    bsr_array((5, 4), blocksize=(2, 2))   # 5 不能被 2 整除
except ValueError as e:
    print("整除约束报错:", e)
```

**需要观察的现象**：四种方式得到的矩阵一致；`a.data.ndim == 3`；方式 4 的坐标三元组会被 `sum_duplicates` 合并重复坐标（经 COO→BSR）；最后会抛出 `shape must be multiple of blocksize`。

**预期结果**：`a.blocksize == (2, 2)`，`a.data.shape == (2, 2, 2)`；非法形状构造抛 `ValueError: shape must be multiple of blocksize`。

#### 4.3.5 小练习与答案

**练习 1**：用 `bsr_array(dense)`（不传 `blocksize`）构造一个块结构明显的矩阵，打印它的 `blocksize`。块大小是从哪来的？

**答案**：来自 `tobsr(blocksize=None)` → `estimate_blocksize(self)` 的自动猜测（见 4.4 节）。构造稠密分支调 `_coo_container(arg1).tobsr(blocksize=blocksize)`，而 `blocksize=None` 会触发 `_csr_base.tobsr` 里的估计逻辑。

**练习 2**：`_bsr_base.__init__` 在「纯形状」分支里把 `blocksize=None` 默认成 `(1,1)`，这意味着什么？

**答案**：意味着「不指定块大小的空 BSR 矩阵」退化为等价于 CSR（每块 1×1，块即标量）。这是合理的默认：空矩阵没有任何非零结构可供猜测块大小，先用最小块。

---

### 4.4 estimate_blocksize 与 count_blocks：自动猜块大小

#### 4.4.1 概念说明

当你有一个现成的稀疏矩阵、想转成 BSR 却不知道该选多大的块时，`estimate_blocksize(A)` 能根据非零结构**猜**一个 `(r,c)`。它的核心度量叫「效率」：

\[ \text{efficiency}(r,c) = \frac{\text{nnz}(A)}{r \cdot c \cdot \text{count\_blocks}(A,(r,c))} \]

其中 `count_blocks(A,(r,c))` 是「按 r×c 切块后，有多少个块是非空的」。直觉是：如果矩阵真有 r×c 的稠密块结构，那么非空块几乎都被填满，效率接近 1；反之效率低，说明这个块大小不合适（块里大多是零）。

`estimate_blocksize` 只在小候选集 `{(2,2),(3,3),(4,4),(6,6)}` 里挑（且要求形状能被整除），挑一个效率超过阈值（默认 0.7）的最大者；都不达标就返回 `(1,1)`（即退化为 CSR）。

`count_blocks` 则是把矩阵当 CSR，调 C++ 内核 `csr_count_blocks` 数出非空块数。

#### 4.4.2 核心流程

`estimate_blocksize` 的决策树：

```
若 A 不是 csr/csc → 先转 csr_array
若 nnz == 0 → 返回 (1,1)
high_efficiency = (1 + efficiency阈值) / 2   # 默认 0.85
e22 = nnz / (4  * count_blocks((2,2)))   # 仅当 M,N 都能被 2 整除
e33 = nnz / (9  * count_blocks((3,3)))   # 仅当 M,N 都能被 3 整除
若 e22 > high_eff 且 e33 > high_eff:
    e66 = nnz / (36 * count_blocks((6,6)))
    返回 (6,6) if e66 > 阈值 else (3,3)
否则:
    e44 = nnz / (16 * count_blocks((4,4)))   # 仅当能被 4 整除
    若 e44 > 阈值: 返回 (4,4)
    否则若 e33 > 阈值: 返回 (3,3)
    否则若 e22 > 阈值: 返回 (2,2)
    否则: 返回 (1,1)
```

`count_blocks(A,(r,c))` 的流程：

```
若 A 是 csr → 调 csr_count_blocks(M,N,r,c,A.indptr,A.indices)  # C++ 内核
若 A 是 csc → 递归 count_blocks(A.T,(c,r))
否则 → 先转 csr_array 再数
```

#### 4.4.3 源码精读

`estimate_blocksize` 的效率计算与决策：

[_spfuncs.py:L11-L24](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_spfuncs.py#L11-L24) —— 先确保是 csr/csc，`nnz==0` 直接返回 `(1,1)`；`efficiency` 必须在 (0,1) 区间，否则 `ValueError`。

[_spfuncs.py:L30-L45](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_spfuncs.py#L30-L45) —— 计算 `e22`、`e33`，并在两者都超过 `high_efficiency` 时尝试更大的 `(6,6)`。注意 `e22 = nnz / (4 * count_blocks(A,(2,2)))` 正是上面的效率公式。

[_spfuncs.py:L46-L59](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_spfuncs.py#L46-L59) —— else 分支按 `(4,4)→(3,3)→(2,2)→(1,1)` 的优先级回退，挑第一个达标的。

`count_blocks` 委托给 C++ 内核：

[_spfuncs.py:L62-L76](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_spfuncs.py#L62-L76) —— CSR 直接调 `csr_count_blocks(M,N,r,c,A.indptr,A.indices)`；CSC 转置后按 `(c,r)` 递归；其余先转 CSR。第 8 行 `from ._sparsetools import csr_count_blocks` 就是导入那个 C++ 内核。

而真正在「缺省块大小」时触发估计的，是 CSR→BSR 的转换入口：

[_csr.py:L97-L106](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L97-L106) —— `_csr_base.tobsr`：`blocksize is None` 时 `from ._spfuncs import estimate_blocksize; return self.tobsr(blocksize=estimate_blocksize(self))`；`blocksize == (1,1)` 时只需把 `data` 重排成 `(n,1,1)` 三维即可（轻量）；其余情况调 `csr_count_blocks` 数块、再调 C++ 内核 `csr_tobsr` 真正转换（见 [_csr.py:L108-L131](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L108-L131)）。

#### 4.4.4 代码实践

**实践目标**：手造一个有明显 3×3 块结构的矩阵，看 `estimate_blocksize` 是否能猜出 `(3,3)`；再造一个无块结构的，看是否退化成 `(1,1)`。

**操作步骤**（示例代码）：

```python
import numpy as np
from scipy.sparse import csr_array
from scipy.sparse._spfuncs import estimate_blocksize, count_blocks

# (1) 6x6 块对角，两个 3x3 稠密块 —— 期望猜出 (3,3)
blk = np.arange(1, 10).reshape(3, 3).astype(float)
A = np.zeros((6, 6))
A[:3, :3] = blk
A[3:, 3:] = blk
A_csr = csr_array(A)
print("A 的估计块大小:", estimate_blocksize(A_csr))         # 预期 (3,3)
print("A 在 (3,3) 下的非空块数:", count_blocks(A_csr, (3, 3)))  # 预期 2
print("A 的 nnz:", A_csr.nnz)                                # 预期 18

# (2) 随机稀疏矩阵 —— 没有块结构，期望退化成 (1,1)
rng = np.random.default_rng(0)
B = csr_array((rng.random((20, 20)) > 0.9).astype(float))
print("B 的估计块大小:", estimate_blocksize(B))              # 多半 (1,1)
```

**需要观察的现象**：A 的估计结果是 `(3,3)`，且 `count_blocks((3,3))==2`、效率 = 18/(9×2)=1.0；B 没有块结构，结果多半是 `(1,1)`。

**预期结果**：A → `(3,3)`（高置信度，因为效率为 1.0 远超阈值）；B → `(1,1)` 或某个小块（取决于随机种子的非零分布）。`estimate_blocksize` 是确定性的（给定输入结果固定），但 B 的具体结果**待本地验证**，因为它依赖随机非零分布。

#### 4.4.5 小练习与答案

**练习 1**：`estimate_blocksize` 为什么只在 `{2,3,4,6}` 这么小的候选集里挑，而不是穷举所有可能的块大小？

**答案**：穷举代价高（要对每个候选调一次 `count_blocks`，每次都跑一遍 C++ 内核扫描 indptr/indices）；而且常见块结构多为 2/3/4/6（尤其有限元里的 2D/3D 单元自由度）。小候选集是「性价比」与「覆盖常见场景」的折中。

**练习 2**：`count_blocks` 对 CSC 输入为什么写成 `count_blocks(A.T, (c,r))`（块大小转置）？

**答案**：CSC 是按列压缩，等价于其转置的 CSR。把 `A`（CSC）转置后得到 CSR，此时「行」对应原来的「列」，所以块大小也要从 `(r,c)` 换成 `(c,r)` 才能在 CSR 视角下正确数块。

---

## 5. 综合实践

把本讲的几个要点串起来：构造一个**向量值有限元风格**的块稀疏矩阵，对比 BSR 与 CSR 的存储开销，并用 `estimate_blocksize` 验证块结构。

**任务**（示例代码）：

```python
import numpy as np
from scipy.sparse import bsr_array, csr_array
from scipy.sparse._spfuncs import estimate_blocksize, count_blocks

# 模拟一个 4 节点、每节点 2 自由度的 1D 问题：刚度矩阵是 2x2 块对角-ish 结构
# 构造 4x4 个 2x2 块 = 8x8 矩阵，主对角块和相邻耦合块稠密
def make_block_fe():
    n = 4                      # 块网格 4x4
    R = C = 2
    A = np.zeros((n * R, n * C))
    for i in range(n):
        # 主对角块
        A[i*R:(i+1)*R, i*C:(i+1)*C] = np.array([[2, -1], [-1, 2]], dtype=float)
        if i + 1 < n:
            # 与下一个节点的耦合块
            A[i*R:(i+1)*R, (i+1)*C:(i+2)*C] = np.array([[-1, 0], [0, 0]], dtype=float)
    return A

dense = make_block_fe()
bsr = bsr_array(dense, blocksize=(2, 2))
csr = csr_array(dense)

print("形状            :", bsr.shape)
print("blocksize       :", bsr.blocksize)
print("bsr.data.shape  :", bsr.data.shape, " (块数, R, C)")
print("bsr.nnz         :", bsr.nnz, " (= 块数*4)")
print("csr.nnz         :", csr.nnz)
print("len(bsr.indices):", len(bsr.indices), " vs len(csr.indices):", len(csr.indices))
print("count_nonzero   :", bsr.count_nonzero())
print("估计块大小      :", estimate_blocksize(csr))

# 验证运算正确性：BSR 与 CSR 的 SpMV 结果必须一致
x = np.ones(bsr.shape[1])
assert np.allclose(bsr @ x, csr @ x)
print("SpMV 一致性检查通过")
```

**需要观察与思考**：

1. `bsr.nnz` 与 `csr.nnz` 是否相等？为什么？（提示：本例块内部也有零，例如耦合块 `[[-1,0],[0,0]]` 只有一个非零却被整块存储）。
2. `len(bsr.indices)` 比 `len(csr.indices)` 小多少？这就是 BSR 省索引的体现。
3. `estimate_blocksize(csr)` 是否返回 `(2,2)`？为什么耦合块里有零，效率仍可能达标？
4. 把 `blocksize` 改成 `(4,4)` 会发生什么？（提示：要检查 `M%4==0` 是否成立，以及块内零比例上升导致效率下降）。

**预期结果**：`bsr @ x == csr @ x`（运算正确）；BSR 的 `indices` 数组明显短于 CSR；`estimate_blocksize` 应能识别出 `(2,2)` 的块结构。具体数值**待本地验证**（依赖你的 `make_block_fe` 实现）。

---

## 6. 本讲小结

- **BSR = 块化的 CSR**：存储原子从「标量」升级为「R×C 稠密块」，`_bsr_base` 继承 `_cs_matrix` 复用「压缩行」骨架，但重写了几乎所有按块级别的运算。
- **三数组按块组织**：`data` 是三维 `(块数, R, C)`、`indices[k]` 是**块列号** ∈ `[0, N//C)`、`indptr` 长度 = `M//R + 1`。
- **blocksize 约束**：必须 `M % R == 0` 且 `N % C == 0`；`blocksize` 不是独立字段，而是直接读 `data.shape[1:]`。
- **nnz 语义特殊**：`nnz = indptr[-1] * R * C`，把块内零也算进去，因此 `bsr.nnz` 可能大于 `count_nonzero()`；想知道真实非零数要用 `count_nonzero()`。
- **构造分发**：`_bsr_base.__init__` 处理稀疏/形状/坐标三元组/原生三数组/稠密五类输入；稠密与坐标输入缺省 `blocksize` 时经 `tobsr` → `estimate_blocksize` 自动猜块大小。
- **自动猜块**：`estimate_blocksize` 用效率公式 `nnz / (r·c·count_blocks)` 在 `{2,3,4,6}` 候选集里挑；`count_blocks` 委托 C++ 内核 `csr_count_blocks`。
- **关键限制**：BSR 不支持切片/逐项读写（`__getitem__` 直接 `raise NotImplementedError`）；只支持 2-D。

## 7. 下一步学习建议

- **下一讲 [u2-l5](u2-l5-lil-dok-dia-format.md)**：转向 LIL / DOK / DIA 三种「便于增量构造或特殊结构」的格式，补齐七种格式的最后三块拼图。届时你会更清楚：为什么「逐元素写入」要选 LIL/DOK 而不是 BSR。
- **进阶到核心机制**：学完七种格式后，进入 U3。建议重点读 [_csr.py:L97-L131 的 `tobsr`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L97-L131) 与 [u3-l6（sparsetools C++ 后端）](u3-l6-sparsetools-cpp-codegen.md)，理解 `csr_tobsr` / `bsr_matvec` / `bsr_tocsr` 这些 C++ 内核是如何被代码生成器 `_generate_sparsetools.py` 产出的。
- **动手延伸**：试着把本讲综合实践的 FE 矩阵分别存成 BSR 和 CSR，用 `sys.getsizeof` 比较 `data` 数组字节数，并各自做一次 `@ x` 计时，直观感受 BSR 在块结构矩阵上的性能优势（待本地验证）。

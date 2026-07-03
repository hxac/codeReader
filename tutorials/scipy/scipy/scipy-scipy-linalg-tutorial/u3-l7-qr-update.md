# QR 增量更新（Cython _decomp_update）

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清「为什么要更新一个已有的 QR 分解」——即在什么场景下重做一次 `qr` 是浪费，而增量更新能把代价从 \(O(MN^2)\) 降到 \(O(MN)\) 量级。
2. 区分并正确使用 `scipy.linalg` 的三个增量更新函数 `qr_update`、`qr_insert`、`qr_delete`，知道它们各自的输入输出约定与边界条件。
3. 理解底层把这套逻辑写在一个 **Cython + Tempita 模板**文件 `_decomp_update.pyx.in` 中，用「融合类型 + 模板展开」一份代码生成 `s/d/c/z` 四种数值类型的实现，并直接内联调用 BLAS/LAPACK（`rot`、`lartg`、`larfg`、`ormqr`、`geqrf` 等）。
4. 能复述秩-1 更新、行/列插入、行/列删除背后的核心算法思路（Givens 旋转、上 Hessenberg 化简、重新正交化）。

## 2. 前置知识

### 2.1 QR 分解的两种形态

矩阵 \(A\in\mathbb{R}^{M\times N}\)（或复矩阵）的 QR 分解把它写成 \(A=QR\)，其中 \(Q\) 是正交/酉矩阵，\(R\) 是上三角。在 `scipy.linalg` 里有两套约定（详见 [u3-l3](u3-l3-qr-decomposition.md)）：

- **full**（完全）：\(Q\) 是 \(M\times M\)，\(R\) 是 \(M\times N\)。
- **economic**（瘦/thin）：\(Q\) 是 \(M\times K\)，\(R\) 是 \(K\times N\)，\(K=\min(M,N)\)。

本讲的三个函数对两种形态都支持，但在 economic 形态下走的是更省内存的算法分支。

### 2.2 Householder 反射 vs Givens 旋转

LAPACK 的标准 QR（`geqrf`）用 **Householder 反射**：每个反射 \(H=I-\tau vv^H\) 一次性把一整列的下三角部分清零，效率高。而 QR 增量更新大量使用 **Givens 旋转**：它只作用在两行（或两列）上，用一个 \(2\times2\) 平面旋转把某个元素精确清零。

一个 Givens 旋转由一对 \((c,s)\) 描述（\(c^2+|s|^2=1\)），作用在二维向量 \((a,b)^T\) 上得到 \((r,0)^T\)。LAPACK 的 `lartg` 负责从 \((a,b)\) 计算 \((c,s)\)，BLAS 的 `rot` 负责把 \((c,s)\) 作用到两列/行上。本讲的增量更新几乎处处都是「造一个 Givens → 同时作用到 \(R\) 和 \(Q\)」。

### 2.3 什么是「更新」与为什么不重新分解

重新做一次 QR 分解的代价约为 \(O(MN^2)\)（瘦长）到 \(O(M^3)\)（方阵）。但如果矩阵只发生**小改动**，例如：

- 加一个秩-1 项：\(A \leftarrow A + uv^T\)；
- 插入或删除若干行/列；

那么可以从已有的 \((Q,R)\) 出发，只做局部修正得到新的 \((Q_1,R_1)\)，代价通常是 \(O(MN)\)（秩-1 更新）或 \(O(M^2)\)（删除一行）量级，远低于重新分解。典型应用：**在线最小二乘**、**滑动窗口回归**、**递推滤波**——数据一条条到来，每来一条只做一次廉价更新，而不是全量重算。

> 术语提醒：本讲反复出现「full / economic」、「上 Hessenberg」、「次对角元」「重新正交化（reorth）」等词，先有个印象，下文会在用到时解释。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [`_decomp_update.pyx.in`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in) | **本讲主角**。Cython + Tempita 模板源文件，定义 `qr_update`/`qr_insert`/`qr_delete` 三个公共函数，以及一堆内联 BLAS/LAPACK 包装和算法内核。 |
| [`_decomp_qr.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qr.py) | 提供 `qr`（基础 QR 分解），是增量更新的输入来源；其中 `safecall`/`mode` 映射在 u3-l3 讲过，本讲会用到 `qr` 产生 \((Q,R)\)。 |
| [`meson.build`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build) | 用 Tempita 把 `.pyx.in` 渲染成 `.pyx`，再由 Cython 编译成 `_decomp_update.*.so` 扩展模块。 |
| [`__init__.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py) | 通过 `from ._decomp_update import *` 把三个函数搬进顶层 `scipy.linalg` 命名空间。 |
| [`tests/test_decomp_update.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_decomp_update.py) | 系统的回归测试，对比「增量更新」与「直接重做 QR」的结果是否一致。 |

## 4. 核心概念与源码讲解

### 4.1 Tempita 模板与融合类型：一份代码管四种数值类型

#### 4.1.1 概念说明

数值线性代数代码最常见的重复是：同一个算法要对 `float32 / float64 / complex64 / complex128` 四种类型各写一遍，唯一区别是调用的 BLAS/LAPACK 例程名前缀不同（`s/d/c/z`）。`_decomp_update.pyx.in` 用两层机制消除这种重复：

1. **Cython 融合类型（fused type）**：用 `ctypedef fused blas_t: float; double; ...` 声明一个「类型变量」，Cython 编译器会为每个具体类型生成一份特化代码，运行时按输入 dtype 自动选择，分支 `if blas_t is double:` 是**编译期**分派。
2. **Tempita 模板**：文件后缀 `.pyx.in` 表示它先经过 Tempita 模板引擎渲染成真正的 `.pyx`，再交给 Cython。模板里的 `{{for ...}}` 块在**渲染期**被展开成显式的 `if/elif/else` 链，自动写出 `scopy`/`dcopy`/`ccopy`/`zcopy` 这类不同前缀的调用。

二者配合：融合类型负责类型分派，Tempita 负责把「调哪个前缀的例程」这层样板代码自动写出来。

#### 4.1.2 核心流程

构建期的流水线是：

```text
_decomp_update.pyx.in
   │  (Tempita 渲染，展开 {{for}} 块)
   ▼
_decomp_update.pyx
   │  (Cython 编译，融合类型特化)
   ▼
_decomp_update.c  ──(C 编译链接)──►  _decomp_update.*.so
```

模板开头的四个列表是渲染的全部「配方」：

```python
TCODES = ['cnp.NPY_FLOAT', 'cnp.NPY_DOUBLE', 'cnp.NPY_CFLOAT', 'cnp.NPY_CDOUBLE']
CNAMES = ['float', 'double', 'float_complex', 'double_complex']
CONDS  = ['if', 'elif', 'elif', 'else:  #']
PREFIX = ['s', 'd', 'c', 'z']
```

每条 `{{for COND, CNAME, C in zip(CONDS, CNAMES, PREFIX)}}` 都会展开成四段 `if blas_t is float: scopy(...) / elif ...: dcopy(...) / ...`。

#### 4.1.3 源码精读

模板配方定义在这里：

[_decomp_update.pyx.in:37-44](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L37-L44) —— 四个列表 `TCODES/CNAMES/CONDS/PREFIX` 决定每种数值类型对应的 NumPy 类型码、Cython 类型名、控制流关键字、BLAS 前缀。

融合类型声明：

[_decomp_update.pyx.in:83-87](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L83-L87) —— `ctypedef fused blas_t` 把四种类型打包，使后续 `cdef inline` 函数自动特化四份。

以最简单的 `copy` 为例看模板如何展开：

[_decomp_update.pyx.in:101-105](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L101-L105) —— `{{for}}` 渲染后等价于 `if blas_t is float: scopy(...) elif blas_t is double: dcopy(...) ...` 四个分支，每个调用对应前缀的 BLAS `copy`。

注意一个细节：复数向量的 2-范数例程前缀不是单字母，而是 `scnrm2`/`dznrm2`，因此这里 `PREFIX` 临时换成 `['s','d','sc','dz']`：

[_decomp_update.pyx.in:126-130](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L126-L130) —— `nrm2` 的模板为复数类型使用 `sc`/`dz` 前缀。

> 说明：所有这些内联包装都带 `noexcept nogil`，意味着它们**释放 GIL**、直接在 C 层调用 BLAS/LAPACK，是性能关键路径。这也解释了为什么后面所有算法内核都能整体放在 `with nogil:` 块里运行。

构建规则在 Meson 中两步走：

[meson.build:243-256](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L243-L256) —— 先 `custom_target` 调 `tempita` 把 `.pyx.in` 渲染成 `.pyx`，再 `extension_module` 用 `linalg_cython_gen`（Cython generator）编译成扩展模块 `_decomp_update`。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：体会 Tempita 渲染前后的体量差异。
2. **步骤**：在仓库根目录执行（只读，不改源码）：
   ```bash
   python -c "from scipy.linalg import _decomp_update; print(_decomp_update.__file__)"
   ```
   找到编译产物 `.so`。再对照 `_decomp_update.pyx.in` 中任意一个 `{{for}}` 块（如 `copy`），人工把它展开成四段 `if/elif/else`，想象渲染后的 `.pyx` 比模板长了多少。
3. **观察**：模板里短短 4 行的 `{{for}}`，渲染后变成约 10 行；全文件有约 30 处 `{{for}}`，渲染产物显著膨胀。
4. **预期结果**：理解「模板 = 用循环写样板」，融合类型 + Tempita 把一份算法源码同时覆盖四种 dtype，作者只需维护一份逻辑。

#### 4.1.5 小练习与答案

- **练习 1**：如果把 `PREFIX` 里 `complex64` 对应的前缀从 `c` 改成 `z`，会出什么问题？
  - **答案**：`complex64`（单精度复）的 BLAS 例程前缀应是 `c`（如 `ccopy`、`cgeqrf`），改成 `z` 会去调双精度复例程 `zcopy`，类型与指针宽度不匹配，产生错误结果或崩溃。
- **练习 2**：为什么这些内联包装要标 `nogil`？
  - **答案**：这样算法内核（如 `qr_rank_1_update`）整体可以放在 `with nogil:` 段中连续调用 BLAS/LAPACK 而不必反复获取/释放 GIL，对大矩阵的纯数值循环能显著提速。

### 4.2 内联 BLAS/LAPACK 工具层（rot、lartg、larfg 等）

#### 4.2.1 概念说明

整个文件第 78–233 行是一组「内联包装」，把裸 BLAS/LAPACK 的 C 签名包成更顺手、带融合类型分派的 Cython 函数。按用途分三类：

| 类别 | 例程 | 作用 |
|---|---|---|
| 向量运算 | `copy`/`swap`/`scal`/`axpy`/`nrm2` | 复制、交换、数乘、累加、求范数 |
| Givens/Householder | `lartg`/`rot`/`larfg`/`larf` | 生成并应用平面旋转或 Householder 反射 |
| 矩阵运算 | `ger`/`gemv`/`gemm`/`trmm` | 秩-1 更新、矩阵-向量、矩阵-矩阵乘 |
| LAPACK QR | `geqrf`/`ormqr` | QR 分解、应用 Q |

其中 `lartg`（生成 Givens）+ `rot`（应用 Givens）是本讲算法的核心积木；`geqrf`/`ormqr` 只在「块更新」（rank-p、批量列插入）里偶尔使用。

#### 4.2.2 核心流程

一个 Givens 旋转消除元素的流程：

```text
给定要消去的 (a, b):
  lartg(&a, &b, &c, &s)        # 计算 c, s 使 [c  s; -conj(s)  c] @ (a,b)^T = (r, 0)^T
  rot(n, x, ..., y, ..., c, s) # 把旋转同时作用到 x、y 两列/行
```

`lartg` 是 LAPACK 的「改进版」旋转生成器（比老的 `rotg` 更稳），它还会把结果写回 `a[0]=r, b[0]=0`，使其行为更像 BLAS 的 `drotg`：

[_decomp_update.pyx.in:132-146](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L132-L146) —— `lartg` 包装 LAPACK `?lartg`，对复数类型额外用 `<float*>c` 取实部地址，并在末尾令 `a[0]=g; b[0]=0`。

`rot` 的实现：

[_decomp_update.pyx.in:148-157](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L148-L157) —— 实数走 BLAS `srot`/`drot`，复数走 LAPACK `crot`/`zrot`，统一接口。

块更新用到的高级例程：

[_decomp_update.pyx.in:209-216](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L209-L216) —— `geqrf` 包装 LAPACK `?geqrf`，返回 `info`（0 表示成功）。

[_decomp_update.pyx.in:218-233](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L218-L233) —— `ormqr`/`unmqr` 包装：实数用 `?ormqr`，复数用 `?unmqr`，用于把隐式 Q 作用到一个矩阵上。

#### 4.2.3 源码精读

此外还有两个重要的「nogil 错误汇报」机制。第一是 **`MEMORY_ERROR` 哨兵**：

[_decomp_update.pyx.in:61](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L61) —— `MEMORY_ERROR = libc.limits.INT_MAX`。`nogil` 内核里 `malloc` 失败时无法抛 Python 异常，于是返回 `INT_MAX`；外层 Python 驱动检查到这个值再抛 `MemoryError`（如 1561、1983 行）。

第二是 `validate_array` 里的 **整数溢出防护**：

[_decomp_update.pyx.in:1314-1334](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L1314-L1334) —— 检查 `strides` 非负且除以元素大小后不超过 `INT_MAX`，因为 BLAS/LAPACK 用 32 位整数描述维度/增量（ILP64 构建下才放宽）。超界就强制拷成 F 序连续数组；同时按 `check_finite` 拦 NaN/Inf。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：理解 `lartg`+`rot` 如何「消元」。
2. **步骤**：阅读 `qr_rank_1_update`（4.3 节）里 `for j in range(m-2, -1, -1)` 的循环体，画出对一对 \((u_j, u_{j+1})\) 调 `lartg` 后，`rot` 分别作用到 \(R\) 的第 \(j,j+1\) 行和 \(Q\) 的第 \(j,j+1\) 列的过程。
3. **观察**：每次迭代消去一个分量，且旋转同时更新 \(R\) 与 \(Q\)，保证 \(Q R\) 乘积不变。
4. **预期结果**：能口述「一个 Givens 旋转 = 一次 `lartg` + 两次 `rot`」。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `nogil` 内核用返回值 `MEMORY_ERROR` 而不是直接 `raise`？
  - **答案**：Cython 在 `nogil` 段中不能抛 Python 异常（那需要 GIL）；约定一个不可能与正常 info 冲突的哨兵值（`INT_MAX`），让外层（持有 GIL）翻译成真正的 `MemoryError`。
- **练习 2**：`geqrf`/`ormqr` 只在哪些场景被用到？
  - **答案**：只在「块（block）」更新里用——`qr_rank_p_update`（rank>1 全分解更新）和批量列插入/删除等需要一次性处理多列的分支；rank-1 与单行列操作都靠 Givens 旋转手工完成。

### 4.3 qr_update：秩-k 更新与 Givens 旋转

#### 4.3.1 概念说明

`qr_update(Q, R, u, v)` 解决「已知 \(A=QR\)，求 \(A+uv^T\)（实）或 \(A+uv^H\)（复）的 QR 分解」。当 \(u,v\) 是向量时是**秩-1 更新**；当它们是 \((M,k)\)、\((N,k)\) 矩阵时是**秩-k 更新**（等价于连续做 \(k\) 次秩-1，或一次性块处理）。

关键数学技巧：把扰动先「投影」到 \(Q\) 的坐标系，再在 \(R\) 上局部修整。对实数情形：

\[
A + uv^T = Q R + u v^T = Q\bigl(R + \underbrace{Q^T u}_{w}\, v^T\bigr)
\]

于是问题化为「已知 \(R\) 上三角，求 \(R + w v^T\) 的 QR」。由于 \(w v^T\) 只在第一行（用 Givens 把 \(w\) 化成 \((\|w\|,0,\dots,0)\) 后）注入扰动，\(R\) 会变成**上 Hessenberg**（只有一条次对角线非零），再用一轮 Givens 把它化回上三角。

#### 4.3.2 核心流程

全分解（full）rank-1 更新的算法（对应内核 `qr_rank_1_update`）：

```text
w = Q^T u                 # 由 Python 层 form_qTu 预先算好，传入内核
for j = m-2 down to 0:
    (c, s) = lartg(w[j], w[j+1])      # 用 Givens 把 w[j+1] 清零
    rot 作用于 R 的第 j、j+1 行        # R 引入一条次对角元 → 上 Hessenberg
    rot 作用于 Q 的第 j、j+1 列        # 保持 QR 乘积不变
R[0,:] += w[0] * conj(v)              # 把扰动加到第一行
用 hessenberg_qr 把 R 化回上三角      # 再一轮 Givens
```

economic（瘦）rank-1 更新 `thin_qr_rank_1_update` 思路类似，但 \(Q\) 不是方阵，需要先用 `reorth` 把新向量 \(u\) 正交化到 \(Q\) 的列空间之外，多出一个额外列参与旋转。

#### 4.3.3 源码精读

公共入口 `qr_update`：

[_decomp_update.pyx.in:2035](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L2035) —— 函数定义。注意它带 `@_apply_over_batch`（2033 行），所以天然支持批处理维度，本讲先聚焦单矩阵。

输入校验与秩数确定：

[_decomp_update.pyx.in:2218-2250](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L2218-L2250) —— 校验 dtype 一致、`u`/`v` 维数与首维匹配；若 `u`、`v` 形状 \((M,1)\)、\((N,1)\) 会被压缩成 1-D（退化为 rank-1）；并限制 \(p \le \min(m,n)\)。

economic 分支按 rank 分派到内核：

[_decomp_update.pyx.in:2279-2293](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L2279-L2293) —— `p==1` 调 `thin_qr_rank_1_update`，否则调 `thin_qr_rank_p_update`。模板 `{{for}}` 仍按 typecode 选择指针类型。

全分解 rank-1 分支先投影再调内核：

[_decomp_update.pyx.in:2303-2316](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L2303-L2316) —— `form_qTu(q1, u1, qTuptr, ...)` 算出 \(w=Q^H u\) 写入 `qTu`，再把 `qTu`（而非原始 `u`）传给 `qr_rank_1_update`。

秩-1 更新内核本身（算法的精华）：

[_decomp_update.pyx.in:857-889](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L857-L889) —— 注释清晰说明了三步：用 Givens 把 \(w\) 化简为 \((\cdot,0,\dots,0)\) 同时作用于 \(R\)（使其变上 Hessenberg）；把 \(v\) 加到 \(R\) 第一行；最后 `hessenberg_qr` 把 \(R\) 化回上三角。注意作用到 \(Q\) 时用 `s.conjugate()`，复数下保证酉性。

把上 Hessenberg 矩阵化回上三角：

[_decomp_update.pyx.in:983-1002](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L983-L1002) —— `hessenberg_qr`：从第 \(k\) 行起，对每个次对角元 `lartg(r[j,j], r[j+1,j])` 生成 Givens，消去它并作用到 \(R\) 右侧剩余列与 \(Q\) 的对应列。删除列时也复用它（见 4.5 节）。

> 说明：rank>1 的全分解更新 `qr_rank_p_update` 不再逐个 Givens，而是对 \(u\) 下方的「胖」部分调 `geqrf`/`ormqr` 做一次小型 QR，再用 Givens 收尾，见 [891–942 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L891-L942)。

#### 4.3.4 代码实践

1. **目标**：验证秩-1 更新等价于「修改矩阵后重做 QR」。
2. **步骤**：
   ```python
   import numpy as np
   from scipy.linalg import qr, qr_update

   rng = np.random.default_rng(0)
   A = rng.standard_normal((6, 4))
   Q, R = qr(A)
   u = rng.standard_normal(6)
   v = rng.standard_normal(4)

   Q1, R1 = qr_update(Q, R, u, v)          # 增量更新
   Qd, Rd = qr(A + np.outer(u, v))         # 直接重做（对比基准）
   ```
3. **观察**：打印 `np.allclose(Q1 @ R1, A + np.outer(u, v))`、`np.allclose(Q1.T @ Q1, np.eye(6))`，并与 `Qd@Rd` 对比。
4. **预期结果**：两个布尔值都为 `True`；`R1` 是上三角、`Q1` 正交。注意 `R1` 与 `Rd` 的对角元**符号可能不同**（QR 不唯一，`qr_update` 不保证对角元为正，见其 Notes），但 `Q1@R1` 重建正确。
5. 计时对比（可选）：对更大的 `A`（如 `2000×1000`），用 `timeit` 比较 `qr_update` 与 `qr` 的耗时，应观察到更新明显更快。

#### 4.3.5 小练习与答案

- **练习 1**：为什么内核 `qr_rank_1_update` 的注释强调「传入的 `u` 已经是 \(Q^T u\)」？
  - **答案**：因为把原始 \(u\) 投影到 \(Q\) 坐标系这一步（`form_qTu`）涉及一次矩阵-向量乘，需要在持有 GIL 的 Python 层用 `gemv` 完成；内核只负责纯数值的 Givens 消元，分工后 `nogil` 段更紧凑。
- **练习 2**：秩-k 更新在 economic 形态下是怎么实现的？
  - **答案**：`thin_qr_rank_p_update` 就是对 \(j=0..p-1\) 逐列调用 `thin_qr_rank_1_update`（[845–855 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L845-L855)），即把秩-k 拆成 k 次秩-1。

### 4.4 qr_insert：行列插入

#### 4.4.1 概念说明

`qr_insert(Q, R, u, k, which='row')` 在 \(A\) 的第 \(k\) 行（或列）前插入若干行（或列），返回插入后矩阵的 QR。`which='row'` 与 `'col'` 走完全不同的算法：

- **插行**：把新行 \(u\) 放到 \(R\) 底部，\(Q\) 嵌入一维（单位阵扩展），用 Givens 把这行非零元消去、\(R\) 重回上三角，最后把对应的 \(Q\) 行**置换**到目标位置 \(k\)。
- **插列**：先算 \(w=Q^H u\)（`form_qTu`），把 \(w\) 作为新列插入 \(R\) 对应位置，再用 `geqrf`+`ormqr` 或 Givens 把增广后的 \(R\) 重新三角化；economic 形态下若 \(u\) 几乎落在 \(Q\) 列空间内（条件数小于 `rcond`）会抛 `LinAlgError`。

#### 4.4.2 核心流程

`which` 分派：

```text
if which == 'row':  qr_insert_row(Q, R, u, k, ...)   # rcond 必须为 None
elif which == 'col': qr_insert_col(Q, R, u, k, rcond, ...)
```

单行插入（内核 `qr_row_insert`）核心：

```text
# R 的最后一行(m-1)是新行 u 的内容（已先放入）
for j = 0 .. min(m-1, n)-1:
    (c, s) = lartg(R[j,j], R[m-1, j])     # 消去新行第 j 列
    rot 作用于 R 第 j、m-1 行的剩余列
    rot 作用于 Q 第 j、m-1 列
# 再把 Q 的第 m-1 行向上置换到位置 k
```

#### 4.4.3 源码精读

公共入口分派：

[_decomp_update.pyx.in:1751-1759](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L1751-L1759) —— `which=='row'` 时强制 `rcond` 为 `None`（插行用不到条件数）；`'col'` 时把 `rcond` 传下去。

economic 插行先「搭骨架」：

[_decomp_update.pyx.in:1810-1847](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L1810-L1847) —— 构造更大的 `qnew`（左上角放旧 \(Q\)，右下角嵌入 \(p\times p\) 单位块），把新行放入 \(R\)，再按 \(p=1\) 或 \(p>1\) 调 `thin_qr_row_insert` 或 `thin_qr_block_row_insert`。

单行插入内核（算法清晰）：

[_decomp_update.pyx.in:500-514](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L500-L514) —— 逐列用 `lartg` 把新行（\(R\) 的第 \(m-1\) 行）的元素清零，旋转同时作用于 \(Q\) 的列；最后用 `swap` 把 \(Q\) 的最后一行置换到位置 \(k\)。

economic 单行插入内核：

[_decomp_update.pyx.in:484-498](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L484-L498) —— 思路相同，但 \(Q\) 列数是 \(n\)，新行作为第 \(n\) 列的扩展参与旋转。

插列的条件数检查与报错：

[_decomp_update.pyx.in:1979-1984](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L1979-L1984) —— economic 插列时，若内核返回 `info==2`（新列几乎落在 \(Q\) 列空间内，`reorth` 检测到条件数过小），抛 `LinAlgError`；`info==MEMORY_ERROR` 抛 `MemoryError`。

> 说明：插列本质是「往正交基里添加一个可能相关的向量」，所以才有条件数问题；插行只是增加行数，\(Q\) 的列空间维度不变，无此风险，故 `rcond` 对插行无意义。

#### 4.4.4 代码实践（见 §5 综合实践）

§5 给出了一个完整的「插行 → 对比重做 QR」脚本，这里先理解接口约定即可。

#### 4.4.5 小练习与答案

- **练习 1**：插入一行后，\(Q\) 的规模如何变化？\(R\) 呢？
  - **答案**：full 形态下 \(Q\) 从 \(M\times M\) 变 \((M+p)\times(M+p)\)，\(R\) 从 \(M\times N\) 变 \((M+p)\times N\)；economic 形态下 \(Q\) 变 \((M+p)\times N\)（当仍为瘦长），\(R\) 仍是 \(N\times N\)。
- **练习 2**：为什么 `qr_insert(..., which='row')` 时传 `rcond` 会报错？
  - **答案**：插行不涉及「把新向量正交化进列空间」，没有条件数概念；代码在 [1752–1754 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L1752-L1754) 显式拒绝。

### 4.5 qr_delete：行列删除

#### 4.5.1 概念说明

`qr_delete(Q, R, k, p=1, which='row')` 从 \(A\) 中删去从第 \(k\) 个起的 \(p\) 行（或列），返回删除后的 QR。两种情况难度差别很大：

- **删列**：删除 \(R\) 的对应列后，\(R\) 不再是上三角，而是出现一条次对角元（上 Hessenberg）；用 `hessenberg_qr` 一轮 Givens 化回上三角即可。\(Q\) 不变（full）或裁剪（economic）。
- **删行**：删掉 \(Q\) 的对应行后 \(Q\) **不再正交**，必须**重新正交化**（reorthogonalization），用 Daniel–Gragg–Kaufman–Stewart 算法（参考文献 [2]/[4]），复杂且可能失败。

#### 4.5.2 核心流程

删列（内核 `qr_col_delete`，单列）：

```text
把 R 中 k 之后的列整体左移一格（删掉第 k 列）
→ R 出现一条次对角元（上 Hessenberg）
hessenberg_qr(...)   # 用 Givens 消去次对角元，同时更新 Q
```

删行（economic，内核 `thin_qr_row_delete`）思路：把要删的行换到 \(Q\) 末尾，对其做重新正交化 `reorthx`，找出与之正交的新方向，再用 Givens 把它从基中移除；若该行完全落在剩余列空间内（数值上），算法可能失败并报 `ValueError('Reorthogonalization Failed, ...')`。

#### 4.5.3 源码精读

删行分支与失败处理：

[_decomp_update.pyx.in:1559-1564](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L1559-L1564) —— economic 删行后检查 `info`：`1` 成功返回切片；`MEMORY_ERROR` 抛 `MemoryError`；其余抛 `ValueError`（重新正交化失败）。

删列分支：

[_decomp_update.pyx.in:1595-1614](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L1595-L1614) —— `p==1` 调 `qr_col_delete`（无需工作数组，更省），`p>1` 调 `qr_block_col_delete`（需 `malloc` 工作区，失败返 `MEMORY_ERROR`）；按 economic/full 返回裁剪后的切片。

单列删除内核：

[_decomp_update.pyx.in:446-458](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L446-L458) —— 先 `copy` 把列左移，再调 `hessenberg_qr(m, n-1, ...)` 化回上三角。注意它同时支持 full 和 economic（参数 `o` 是 \(Q\) 的列数）。

重新正交化的数学含义：

[_decomp_update.pyx.in:1043-1058](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_update.pyx.in#L1043-L1058) —— `reorth` 的 docstring：给定列正交的 \(Q\) 和向量 \(u\)，分解 \(u = Q s + p w\)，其中 \(w\) 单位长且与 \(Q\) 各列正交；返回条件数信息（用于插列判断），删行时 `reorthx` 用它找新正交方向。

> 说明：删行之所以难，根源是「\(Q\) 删一行后，剩余行不再构成正交矩阵」——这和删列的「\(R\) 删一列后只是多个次对角元」难度截然不同，所以删行列只需廉价 Givens，删行要做较贵的重新正交化。

#### 4.5.4 代码实践（源码阅读型）

1. **目标**：理解删列为何只产生「上 Hessenberg」。
2. **步骤**：手写一个 \(4\times4\) 上三角 \(R\)，删去第 2 列（索引 1），把剩下 3 列左移，观察 \(R\) 哪个位置出现了次对角非零元。然后阅读 `qr_col_delete` 确认。
3. **观察**：删除后会发现在第 \(k\) 个位置下方多出一个次对角元。
4. **预期结果**：能指出该次对角元正是 `hessenberg_qr` 从第 \(k\) 行开始要消去的对象。

#### 4.5.5 小练习与答案

- **练习 1**：删一行 vs 删一列，哪个更便宜？
  - **答案**：删列便宜——只需 `copy` 左移加一轮 Givens（`hessenberg_qr`），\(O(n^2)\)；删行需要重新正交化，更贵且可能失败。
- **练习 2**：删行失败（`ValueError('Reorthogonalization Failed, ...')`）意味着什么？
  - **答案**：被删的行（数值上）完全落在剩余 \(Q\) 行所张的子空间内，找不到与之正交的新方向来完成重新正交化；通常说明原矩阵该行是其它行的线性组合（秩亏损场景）。

## 5. 综合实践

下面把 `qr` + `qr_insert` + `qr_update` + `qr_delete` 串成一个端到端的小任务：构造矩阵 → 分解 → 插入一行并与重做 QR 对比 → 做一次秩-1 更新 → 删去一行，全程验证 \(Q\) 正交、\(QR\) 重建正确。

```python
# 示例代码：可在本地 SciPy 环境直接运行
import numpy as np
from scipy.linalg import qr, qr_insert, qr_update, qr_delete

rng = np.random.default_rng(42)

# 1) 原始矩阵与 QR 分解
A = rng.standard_normal((5, 3))
Q, R = qr(A)

# 2) 在第 2 行（k=2）前插入一行 u
u = rng.standard_normal(3)
Q1, R1 = qr_insert(Q, R, u, 2, which='row')

# 直接重做 QR 作为对照基准
A1_direct = np.insert(A, 2, u, axis=0)
Q1d, R1d = qr(A1_direct)

print("insert 重建:", np.allclose(Q1 @ R1, A1_direct))      # 预期 True
print("insert Q正交:", np.allclose(Q1.T @ Q1, np.eye(6)))   # 预期 True
# 注意：Q1@R1 与 Q1d@R1d 都等于 A1_direct，但 R1 与 R1d 对角元符号可能不同（QR 不唯一）

# 3) 对更新后的分解做一次秩-1 更新：A1 <- A1 + x y^T
x = rng.standard_normal(6)
y = rng.standard_normal(3)
Q2, R2 = qr_update(Q1, R1, x, y)
print("update 重建:", np.allclose(Q2 @ R2, A1_direct + np.outer(x, y)))  # 预期 True

# 4) 再删去第 0 行（索引 0），p=1
Q3, R3 = qr_delete(Q2, R2, 0, 1, which='row')
A3_direct = np.delete(A1_direct + np.outer(x, y), 0, axis=0)
print("delete 重建:", np.allclose(Q3 @ R3, A3_direct))      # 预期 True
print("delete Q正交:", np.allclose(Q3.T @ Q3, np.eye(5)))   # 预期 True
```

**实践要点与预期**：

- 四个 `print` 都应输出 `True`。
- 若把 `which='row'` 的插入改成 economic 分解（`qr(A, mode='economic')`），`Q` 变成 \(5\times3\)，`qr_insert` 仍能工作，但 `Q1` 形状变为 \(6\times3\)——可自行修改验证。
- 进阶：用 `timeit` 对更大的矩阵（如 `A = rng.standard_normal((2000, 500))`）比较「循环里每次 `qr_insert` 一行」与「每次 `qr` 重做」的总耗时，体会增量更新的性能优势。
- 对 `qr_delete(..., which='row')`，可尝试构造一个秩亏损矩阵（两行成比例），观察是否会触发 `ValueError('Reorthogonalization Failed, ...')`。**待本地验证**具体触发条件。

对应的测试基准在仓库里：

[tests/test_decomp_update.py:30-33](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_decomp_update.py#L30-L33) —— `check_qr` 同时校验「\(Q\) 酉」「\(R\) 上三角」「\(QR\approx A\)」三项，正是上面断言的来源。该测试文件对每种组合（sqr/tall/fat/Mx1/1xN、full/economic、四种 dtype、单/多行列）都有覆盖。

## 6. 本讲小结

- `_decomp_update.pyx.in` 是一个 **Cython + Tempita 模板**文件，用融合类型 + `{{for}}` 模板展开，一份源码生成覆盖 `s/d/c/z` 四种 dtype 的 `qr_update`/`qr_insert`/`qr_delete` 实现。
- 三个函数的公共入口都极薄：做形状/dtype 校验（`validate_qr`）、决定内存布局与是否覆写、用 `{{for}}` 按 typecode 选指针类型、把数值工作委派给 `with nogil:` 的算法内核，再用 `info`/`MEMORY_ERROR` 哨兵翻译错误。
- **`qr_update`** 把 \(A+uv^H\) 的扰动先投影到 \(Q\) 坐标系（`form_qTu`），再用 Givens（`lartg`+`rot`）把 \(R\) 变上 Hessenberg 再 `hessenberg_qr` 化回上三角。
- **`qr_insert`** 插行用 Givens 消元 + 行置换；插列用 `form_qTu` + `geqrf`/`ormqr`，economic 形态下会检查新列是否落在 \(Q\) 列空间内（`rcond`/条件数）。
- **`qr_delete`** 删列只需把列左移 + `hessenberg_qr`（廉价）；删行需重新正交化（`reorthx`/Daniel 等），更贵且可能失败。
- 共性积木：内联 BLAS/LAPACK 包装（`copy/swap/scal/axpy/nrm2/lartg/rot/larfg/larf/ger/gemv/gemm/trmm/geqrf/ormqr`）全部 `noexcept nogil`，使算法内核能在无 GIL 段内连续调用底层例程。

## 7. 下一步学习建议

- **横向对照**：阅读 [u3-l3 QR 分解](u3-l3-qr-decomposition.md)，对比 `qr`（用 LAPACK `geqrf`+`orgqr`，Householder）与本讲增量更新（Givens 为主）的实现风格差异。
- **深入 Cython 后端**：本讲是 `nogil` + memoryview + 融合类型的典型范例，可接着看 [u7-l3（cython_blas/cython_lapack）](u7-l3-cython-blas-lapack.md) 与 [u7-l4（Cython 扩展实践）](u7-l4-cython-extensions.md)，理解本讲顶部 `from . cimport cython_blas` 的来源。
- **批量维度**：本讲的 `@_apply_over_batch` 让这些函数支持前导批处理维度，相关机制在 [u8-l1（批量线性代数）](u8-l1-batched-python-api.md) 详述。
- **应用阅读**：在线/滑动窗口最小二乘（如递推辨识）是 `qr_insert`/`qr_delete` 的经典用武之地，可结合文献（文件头注释列出的 Golub & Van Loan、Daniel 等）理解算法稳定性取舍。

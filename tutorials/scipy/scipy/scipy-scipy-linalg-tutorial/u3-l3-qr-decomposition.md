# QR 分解、qr_multiply 与 RQ

## 1. 本讲目标

本讲专讲 `scipy.linalg` 中三个与正交三角分解相关的函数：`qr`、`qr_multiply`、`rq`，全部集中在 [_decomp_qr.py](_decomp_qr.py) 一个文件里。学完后你应当能够：

- 说清 QR 分解 \(A=QR\) 的几何含义，以及 `mode='full' / 'r' / 'economic' / 'raw'` 四种返回结构各自适合什么场景；
- 理解 `pivoting=True` 列主元（rank-revealing）QR 的作用与 \(A[:,P]=QR\) 的列置换约定；
- 掌握 `qr_multiply` 「先求 raw 分解、再用 LAPACK 的 `ormqr/unmqr` 把 Q 乘到另一个矩阵上」的实现路径，以及它为何比「先 `qr` 再手动相乘」更省内存、更快；
- 读懂 `safecall` 这个工具函数如何「自动探测最优工作数组长度 lwork、并翻译 LAPACK 的错误码」，理解 `qr` 中已被废弃的 `lwork` 参数与 `_NoValue` 哨兵的关系。

本讲承接 [u3-l1（LU 分解）](u3-l1-lu-decomposition.md)：LU 是「三角分解」，QR 是「正交三角分解」，二者是矩阵分解里最基础的两块积木。

## 2. 前置知识

- **正交矩阵与酉矩阵**：实数方阵 \(Q\) 满足 \(Q^{T}Q=I\) 称为正交矩阵；复数方阵满足 \(Q^{H}Q=I\)（\(H\) 表示共轭转置）称为酉矩阵。它们的共同好处是「乘上去不改变向量长度」，数值上非常稳定。
- **Householder 反射**（直觉版）：把一个向量 \(x\) 镜面反射成与某个坐标轴同方向的向量，只需记录「镜面」就能表示这次反射。LAPACK 的 `geqrf` 正是用一连串 Householder 反射把矩阵 \(A\) 逐步「拍」成上三角 \(R\)，每个反射用一个数 `tau` 加一段向量表示——这就是后面 `mode='raw'` 返回的 `(Q, tau)`。
- **上三角矩阵**：主对角线以下全为 0 的矩阵，记作 \(R\)。QR 分解中 \(R\) 上三角、\(Q\) 正交/酉。
- **LAPACK 的 s/d/c/z 前缀**：`s`=单精度实、`d`=双精度实、`c`=单精度复、`z`=双精度复。同一个算法有四套例程，函数名只差前缀，详见 [u7-l1](u7-l1-blas-lapack-dispatch.md)。本讲里你会看到 `geqrf/orgqr/ormqr/geqp3/gerqf/orgrq` 等名字，它们都没有前缀，表示「四套都有」。
- **lwork（工作数组长度）**：LAPACK 很多例程需要调用者提供一块临时工作内存，长度由参数 `lwork` 指定。传 `lwork=-1` 做「查询调用」可让 LAPACK 返回它推荐的最优长度，再用该长度正式调用一次——这是数值库的通用套路。
- **批处理维度**：`qr` 支持 `(..., M, N)` 形状，前导的 `...` 是「一摞矩阵」，详见 [u8-l1](u8-l1-batched-python-api.md)；`qr_multiply` 和 `rq` 则只接受单个二维矩阵。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [_decomp_qr.py](_decomp_qr.py) | 本讲主角，定义 `qr`、`qr_multiply`、`rq` 三个公共函数与工具函数 `safecall` |
| [_common_array_utils.hh](src/_common_array_utils.hh) | C++ 后端的 `QR_mode` 枚举（`FULL=1, R=11, RAW_MODE=21, ECONOMIC=31`），Python 侧 `modeFlag` 要和它对齐 |
| [src/_linalg_qr.hh](src/_linalg_qr.hh) | C++ 批量 QR 内核 `_qr`，`qr` 的真正算力来源 |
| [lapack.py](lapack.py) | `get_lapack_funcs`（按 dtype 选前缀）、`_normalize_lapack_dtype`、`HAS_ILP64` |
| [_batched_linalg.py](_batched_linalg.py) | `_batched_linalg._qr`，`qr` 委派的 C++ 扩展入口 |
| [_basic.py](_basic.py) | `_format_emit_errors_warnings`，`qr` 复用的批量错误聚合函数（[u2-l2](u2-l2-solve-and-dispatch.md) 已讲） |

## 4. 核心概念与源码讲解

### 4.1 qr：QR 分解的多种 mode 与返回结构

#### 4.1.1 概念说明

QR 分解把任意（未必方）矩阵 \(A\in\mathbb{F}^{M\times N}\) 写成

\[
A = QR
\]

其中 \(Q\) 是正交/酉矩阵，\(R\) 是上三角矩阵。设 \(K=\min(M,N)\)，常见的两种「返回粒度」：

- **完整（full）QR**：\(Q\) 是 \(M\times M\)，\(R\) 是 \(M\times N\)。\(Q\) 把 \(A\) 的列空间补齐成整个 \(\mathbb{F}^M\)。
- **经济（economic / reduced）QR**：\(Q\) 是 \(M\times K\)，\(R\) 是 \(K\times N\)。只保留 \(A\) 列空间所需的最少反射，更省内存。

> 术语提示：很多教材和 NumPy 把「经济版」叫 reduced（`numpy.linalg.qr` 的 `mode='reduced'`）。但 **`scipy.linalg.qr` 没有 `'reduced'` 这个名字**，对应的概念叫 `'economic'`。下文实践里我们用 `mode='economic'` 来得到「瘦长矩阵的 reduced QR」。

`scipy.linalg.qr` 额外提供两个模式：

- `'r'`：只要 \(R\)，连 \(Q\) 都不显式生成，最省。
- `'raw'`：返回 LAPACK 内部的 `(Q, tau)`——这里 `Q` 其实是被 Householder 反射改写过的矩阵，`tau` 是各反射的标量系数。`raw` 是给 `qr_multiply` 内部用的，普通用户一般不直接用。

#### 4.1.2 核心流程

`qr` 的 Python 层是一层「校验 + 委派」的薄壳，真正算力在 C++ 后端：

1. **校验**：`mode` 必须在四种里；输入至少二维；`check_finite` 拦截 NaN/Inf。
2. **mode 编码**：把字符串 `mode` 查表翻译成整数 `modeFlag`（与 C++ 枚举一一对应）。
3. **归一化**：`_normalize_lapack_dtype` 统一 dtype 与内存布局。
4. **空数组快路径**：`a1.size == 0` 时直接返回单位阵/空阵，不走 LAPACK。
5. **覆写门控**：按 u2-l2 建立的规则，只有「二维 + Fortran 列主序连续」时 `overwrite_a` 才真正生效。
6. **委派**：调用 C++ 后端 `_batched_linalg._qr(a1, overwrite_a, modeFlag, pivoting)`，返回 `Q, R, tau, jpvt, err_lst`。
7. **错误汇报**：`err_lst` 非空则用 `_format_emit_errors_warnings` 统一抛错/告警。
8. **拼装返回**：按 `mode` 与 `pivoting` 组装出不同元组。

#### 4.1.3 源码精读

**mode 字符串 → 整数枚举**。注意源码里的注释「keep in sync with the C side」，意思是这里的四个数字必须和 C++ 后端的 `QR_mode` 枚举严格一致：

[modes 映射表](_decomp_qr.py#L142-L148)：`full/qr→1`、`r→11`、`raw→21`、`economic→31`。

C++ 那边 [_common_array_utils.hh 的 QR_mode 枚举](src/_common_array_utils.hh#L926-L933) 正是 `FULL=1, R=11, RAW_MODE=21, ECONOMIC=31`，两边靠这套整数「握手」。这种「Python 侧传整数、C 侧 `enum` 解读」的模式在 u3-l1 的 LU 里也见过（`_linalg_lu_det.hh`）。

随后 `modeFlag = modes[mode]` 完成转换：[L156](_decomp_qr.py#L156)。

**校验与归一化**：[L153-L184](_decomp_qr.py#L153-L184) 检查 mode 合法性、`check_finite`、维度、dtype 归一化。其中 [_normalize_lapack_dtype 调用](_decomp_qr.py#L184) 保证返回类型一致（这是 u7-l1 的内容）。

**覆写门控**（承接 u2-l2 的 `_datacopied` 与「二维+F 连续」约束）：

[overwrite_a 的三级判定](_decomp_qr.py#L213-L218)：未对齐或非本地字节序则强制拷贝；`overwrite_a` 还要满足 `ndim==2` 且 `F_CONTIGUOUS`，否则降级为不覆写，避免污染用户数据。

**委派 C++ 后端**（「heavy lifting」）：

[调用 _batched_linalg._qr](_decomp_qr.py#L221)：一行把脏活全交给编译后端，拿到 `Q, R, tau, jpvt, err_lst`。`jpvt` 是列主元后的列索引（仅 `pivoting=True` 有意义）。后端内核 [src/_linalg_qr.hh 的 _qr 签名](src/_linalg_qr.hh#L9) 会根据 `mode` 决定 `Q` 的列数与是否生成显式 `Q`（见 4.2.3）。

**错误汇报**：[err_lst 聚合](_decomp_qr.py#L223-L224)，复用 `_format_emit_errors_warnings`（u2-l2 讲过的「奇异>内部错误>病态」优先级翻译）。

**拼装返回值**（这段逻辑决定了不同 mode 的返回形状）：

[返回对象组装](_decomp_qr.py#L226-L240)：
- `pivoting=True` → `Rj = (R, jpvt)`，否则 `Rj = (R,)`；
- `mode=='raw'` → 把 `Q` 替换成 `(Q, tau)` 元组；
- `mode=='economic'` 且宽矩阵 \(M<N\) → 对 `Q` 做列切片 `Q[..., :, :M]`；
- `mode=='r'` → 只返回 `Rj`；其余返回 `(Q,) + Rj`。

#### 4.1.4 代码实践

> 实践目标：直观感受 `full` 与 `economic` 两种 mode 的返回形状差异，并验证 \(A\approx QR\)。

操作步骤（示例代码，请自行运行）：

```python
import numpy as np
from scipy.linalg import qr

rng = np.random.default_rng(0)
A = rng.standard_normal((9, 6))   # 瘦长矩阵 M=9, N=6, K=6

Qf, Rf = qr(A, mode='full')        # 完整 QR
Qe, Re = qr(A, mode='economic')    # 经济 QR（即 reduced）

print(Qf.shape, Rf.shape)          # 预期 (9,9) (9,6)
print(Qe.shape, Re.shape)          # 预期 (9,6) (6,6)
print(np.allclose(A, Qf @ Rf))     # 预期 True
print(np.allclose(A, Qe @ Re))     # 预期 True
print(np.allclose(Qf.T @ Qf, np.eye(9)))  # Q 正交，预期 True
```

需要观察的现象：
- `full` 的 \(Q\) 是方阵（\(9\times9\)），`economic` 的 \(Q\) 是瘦长（\(9\times6\)）。
- 两种 mode 都满足 \(QR\approx A\)，且 \(Q\) 的列两两正交。

预期结果：`full` 返回 `(9,9)` 与 `(9,6)`；`economic` 返回 `(9,6)` 与 `(6,6)`；三个 `allclose` 均为 `True`。具体数值随随机种子变化（**待本地验证**，但形状与布尔结果是确定的）。

#### 4.1.5 小练习与答案

**练习 1**：若把上面矩阵换成「宽矩阵」`A = rng.standard_normal((6, 9))`（\(M=6<N=9\)），`mode='full'` 和 `mode='economic'` 的 \(Q,R\) 形状分别是什么？

**答案**：\(K=\min(6,9)=6\)。`full`：\(Q\) 为 \((6,6)\)，\(R\) 为 \((6,9)\)；`economic`：\(Q\) 为 \((6,6)\)，\(R\) 为 \((6,9)\)。注意此时 full 与 economic 的形状相同（因为 \(M<N\) 时「补齐列空间」本来就只需 \(M\) 列）。

**练习 2**：`mode='r'` 时函数返回什么？为什么说它「最省」？

**答案**：只返回上三角 \(R\)，不生成 \(Q\)。因为很多应用（如判断秩、解最小二乘）只需要 \(R\)，省掉了显式构造 \(Q\) 这一整步 LAPACK 调用（对应 C++ 后端跳过 `or_un_gqr`），所以最省计算与内存。

---

### 4.2 pivoting：列主元 QR 与秩揭示

#### 4.2.1 概念说明

普通 QR 对**秩亏损**矩阵（列向量线性相关）并不「诚实」——\(R\) 的对角元可能接近 0 却不告诉我们哪几列才是「真正独立」的。**列主元 QR（pivoted QR / rank-revealing QR）** 在分解时允许交换 \(A\) 的列：

\[
A[:,P] = QR
\]

其中 \(P\) 是列置换（一个整数索引数组），且 \(R\) 的对角线按绝对值**非增**排列：\(|R_{00}|\ge|R_{11}|\ge\cdots\)。这样一来，对角线骤降到 0 的位置就暴露了矩阵的「数值秩」，故称「秩揭示」。

#### 4.2.2 核心流程

- 用户传 `pivoting=True`，`qr` 把该布尔值透传给后端 `_batched_linalg._qr`。
- 后端改用 LAPACK 的 `geqp3`（带列主元的 QR）而非 `geqrf`。
- 返回时多带一个 `jpvt`：长度为 \(N\) 的整数数组，表示列置换 \(P\)。
- 满足恒等式 \(A[:,P] = Q@R\)，等价地 \(A = Q@R[:,inv]\) 或 `A @ P_matrix == Q @ R`（见 docstring 示例）。

#### 4.2.3 源码精读

docstring 里有最权威的恒等关系说明：[pivoting 的数学约定](_decomp_qr.py#L61-L68)，明确写出 `A[..., :, P] = Q @ R`，且 \(R\) 对角非增。

后端调用处 [L221](_decomp_qr.py#L221) 的第 4 个参数 `pivoting` 控制后端走 `geqrf` 还是 `geqp3`（docstring 的 [Notes](_decomp_qr.py#L91-L94) 列出了底层例程 `dgeqrf/zgeqrf/dorgqr/zungqr/dgeqp3/zgeqp3`）。

返回拼装时，[pivoting 决定是否附带 jpvt](_decomp_qr.py#L227-L230)：`pivoting=True` 时返回元组末尾多一个 `jpvt`。

docstring 末尾的示例非常清楚（[L120-L139](_decomp_qr.py#L120-L139)），演示了 `np.abs(np.diag(r4))` 非增、以及三种等价写法 `a[:, p4] == q4@r4`、`a == q4@r4 @ P`、`a @ P.T == q4@r4`，建议读者对照阅读。

#### 4.2.4 代码实践

> 实践目标：构造一个秩亏损矩阵，观察列主元 QR 如何把「强列」排到前面、\(R\) 对角线非增。

```python
import numpy as np
from scipy.linalg import qr

# 第 3 列 = 第 1 列 + 第 2 列，故秩为 2（亏损）
A = np.array([[1., 2., 3.],
              [0., 1., 1.],
              [1., 0., 1.],
              [2., 1., 3.]])
Q, R, P = qr(A, pivoting=True)
print(P)                                 # 列置换
print(np.abs(np.diag(R)))                # 预期：非增序列
print(np.allclose(A[:, P], Q @ R))       # 预期 True
```

需要观察的现象：`np.abs(np.diag(R))` 严格非增；最小的对角元应当明显小于最大者，提示数值秩为 2。

预期结果：`A[:, P] == Q@R` 成立（`True`）；对角线绝对值非增。具体 `P` 与 `R` 数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习**：为什么说列主元 QR「秩揭示」？给出一个判断数值秩的简单规则。

**答案**：因为列主元把能量大的列（对应大 \(|R_{ii}|\)）排到对角线前面，对角线从大到小排列。若设定阈值 \(\tau\)（如 \(\tau = \max(|R_{ii}|)\cdot \varepsilon\cdot\max(M,N)\)），则「\(R\) 对角线上大于 \(\tau\) 的元素个数」即为矩阵的数值秩估计。

---

### 4.3 safecall 与 lwork / _NoValue 处理

#### 4.3.1 概念说明

`qr_multiply` 和 `rq` 还没迁移到 C++ 批量后端，仍在 Python 层直接调用 f2py 包装的 LAPACK 例程。这些例程普遍需要 `lwork` 参数，且通过最后一个返回值 `info` 报告错误。手动管理这两件事很繁琐，于是有了 `safecall`：一个把「lwork 自动探测」和「info 错误翻译」封装好的小工具。

而 `qr` 里那个 `lwork` 关键字已经在 SciPy 1.18.0 被废弃（因为它迁移到了 C++ 后端，lwork 由后端自行管理），用一个叫 `_NoValue` 的哨兵来检测「用户是否还显式传了 lwork」。

#### 4.3.2 核心流程

`safecall(f, name, *args, **kwargs)` 的逻辑：

1. 如果没传 `lwork` 或传了 `-1`：
   - 先临时设 `lwork=-1` 调一次 `f`（查询调用），LAPACK 会把它推荐的最优长度放进返回值里；
   - 取出该长度，转成整数，正式写回 `kwargs['lwork']`。
2. 用确定好的 `lwork` 再调一次 `f`，拿到真实结果。
3. 检查返回的 `info`（倒数第一个）：`info < 0` 表示第 `-info` 个参数非法 → 抛 `ValueError`；否则丢弃末尾两个返回值（info 与 lwork 查询结果），只把有效结果返回给调用者。

#### 4.3.3 源码精读

[safecall 实现](_decomp_qr.py#L18-L29)：注意第 25 行 `ret[-2][0].real.astype(np.int_)`——查询调用返回的「最优 lwork」是一个浮点数（LAPACK 约定），取实部转整数；第 27-28 行翻译 `info<0` 的非法参数错误。

`qr` 里 `lwork` 的废弃处理，依赖 [_NoValue 的导入](_decomp_qr.py#L5) 与 [默认值 `lwork=_NoValue`](_decomp_qr.py#L32)。判断逻辑在 [L171-L181](_decomp_qr.py#L171-L181)：如果用户传的不是 `_NoValue`（说明显式用了这个已废弃关键字），则按旧语义校验后发 `DeprecationWarning`，提示将在 1.20.0 移除。`_NoValue` 是 SciPy 内部的一个「哨兵对象」，专门用来区分「用户没传」与「用户显式传了 None」——这是比 `None` 更精确的默认值检测手段。

对比之下，[rq 的签名](_decomp_qr.py#L399) 仍是 `lwork=None`（没有废弃，因为 rq 还没迁后端），并在内部把 `lwork` 透传给 `safecall`（[L497](_decomp_qr.py#L497)）。

#### 4.3.4 代码实践

> 实践目标：观察 `lwork` 在 `qr` 中已废弃、在 `rq` 中仍可用，并理解 `_NoValue` 哨兵的作用。

```python
import warnings
from scipy.linalg import qr, rq
import numpy as np

A = np.array([[1., 2.], [3., 4.], [5., 6.]])

# qr 的 lwork 已废弃：应发出 DeprecationWarning
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    qr(A, lwork=None)
    print([str(x.category.__name__) for x in w])  # 预期含 'DeprecationWarning'

# rq 的 lwork 仍可用（透传给 safecall 做查询）
R, Q = rq(A, lwork=None)   # 正常返回，无废弃警告
```

需要观察的现象：调用 `qr(A, lwork=...)` 会触发 `DeprecationWarning`；`rq(A, lwork=None)` 不触发。

预期结果：`qr` 的告警列表中包含 `'DeprecationWarning'`；`rq` 正常返回。**待本地验证**具体告警文本。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `safecall` 要调两次 `f`？

**答案**：第一次用 `lwork=-1` 做「查询调用」，目的是让 LAPACK 算出最优工作数组长度；第二次用该长度做「正式调用」拿到真实数值结果。这是 LAPACK 例程查询最优 lwork 的标准两步法。

**练习 2**：`info < 0` 和 `info > 0` 在 LAPACK 里通常各代表什么？`safecall` 处理了哪一种？

**答案**：`info < 0` 表示第 `-info` 个参数非法（调用方式错误）；`info > 0` 通常表示算法层面的失败（如某对角元为 0、分解失败）。`safecall` 只处理 `info < 0`（抛 `ValueError`），`info > 0` 这类由具体例程语义决定，留给上层函数（或 docstring 约定的告警）处理。

---

### 4.4 qr_multiply：先求 raw 分解，再把 Q 乘到 c 上

#### 4.4.1 概念说明

很多场景下我们并不需要显式的 \(Q\)，只需要 \(Q\) 作用在另一个向量/矩阵 \(c\) 上的乘积 \(Qc\) 或 \(cQ\)。例如最小二乘里要把 \(A^T b\) 变换到 \(R^T\) 的坐标系。如果先 `qr` 拿到完整 \(Q\) 再做矩阵乘法，既要存储整个 \(Q\)、又要做一次 \(O(M^2\cdot\text{cols})\) 的稠密乘法，浪费。

`qr_multiply` 的思路是：只求 `mode='raw'` 的紧凑分解（Householder 反射 + tau），然后用 LAPACK 的 `ormqr`（实）/`unmqr`（复）例程**直接把 Q 的作用乘到 c 上**，从不显式构造完整 Q。这样既省内存又省算力。

`mode` 参数决定乘法方向：`'left'` 算 \(Qc\)，`'right'` 算 \(cQ\)；`conjugate=True` 则用 \(Q^H\) 代替 \(Q\)（对实矩阵即 \(Q^T\)），这往往比显式共轭更快。

#### 4.4.2 核心流程

1. 校验 `mode`（left/right），把一维 `c` 提升为二维，校验形状兼容性。
2. **复用 `qr`**：以 `mode='raw'` 调一次 `qr`，拿到 `(Q, tau)` 与（可能的）`jpvt`、`R`。
3. 按 dtype 选 `ormqr`/`unmqr`，确定 `trans`（实数 `T`、复数 `C`）。
4. 根据 \(M\) 与 \(N\) 的大小关系、`mode`、`conjugate`，安排好工作矩阵 `cc`、左右侧标志 `lr`（`'L'`/`'R'`）与 `trans`。
5. 用 `safecall` 调 `ormqr/unmqr`，得到乘积 `cQ`。
6. 收尾：必要时转置、列切片、还原一维，连同 `R`（与 `jpvt`）返回。

#### 4.4.3 源码精读

函数签名带 [@\_apply\_over\_batch 装饰器](_decomp_qr.py#L243-L245)，声明 `a` 是二维、`c` 是一维或二维，从而支持批处理（u8-l1 的基础设施）。

关键一步——**复用 qr 的 raw 模式**：[L346-L347](_decomp_qr.py#L346-L347) 直接 `raw = qr(a, overwrite_a, mode="raw", pivoting=pivoting)`，解包出 `Q, tau`。这就是为什么 4.1 里说 `'raw'` 模式「主要给 `qr_multiply` 内部用」。

选例程并定 trans：[get_lapack_funcs 取 ormqr](_decomp_qr.py#L353-L357)，实数用 `"T"`、复数用 `"C"`（共轭转置）。

形状与侧别的复杂分支：[L360-L385](_decomp_qr.py#L360-L385) 处理「\(M>N\) 且 left 且不覆写」时需要把 `c` 补零扩成 \(M\) 行、以及 `cc = c.T` 转置等细节，目的是把数据整理成 LAPACK `ormqr` 期望的 Fortran 列主序布局。

真正的乘法调用：[safecall 调 gormqr/gunmqr](_decomp_qr.py#L386-L387)，`lr`（L/R）、`trans`、`Q`、`tau`、`cc` 一并传入。`safecall` 在这里负责自动探测 lwork（见 4.3）。

收尾与返回：[L388-L395](_decomp_qr.py#L388-L395) 处理转置还原、`mode='right'` 的列切片、一维还原，并把 `R`（及 `jpvt`）拼到返回元组里。所以 `qr_multiply` 返回 `(cQ, R)` 或 `(cQ, R, P)`。

#### 4.4.4 代码实践

> 实践目标：用 `qr_multiply` 计算 \(Q^T c\)，并与「先 `qr` 得到显式 \(Q\) 再手动相乘」对比，验证二者一致。

```python
import numpy as np
from scipy.linalg import qr, qr_multiply

rng = np.random.default_rng(0)
A = rng.standard_normal((9, 6))   # 瘦长矩阵 M=9, N=6
c = rng.standard_normal((9, 4))   # 与 Q 同高的矩阵

# 方法一：qr_multiply 直接得到 Q^T @ c（conjugate=True 对实矩阵即转置）
QTc, R = qr_multiply(A, c, mode='left', conjugate=True)

# 方法二：先显式求 Q，再手动相乘作对照
Q, _ = qr(A, mode='full')
print(np.allclose(QTc, Q.T @ c))  # 预期 True
print(QTc.shape)                  # 预期 (9, 4)
```

需要观察的现象：两种算法路径得到的 \(Q^Tc\) 完全一致（在浮点误差内）；`qr_multiply` 从未显式构造完整的 \(Q\)。

预期结果：`np.allclose(QTc, Q.T @ c)` 为 `True`，`QTc.shape == (9, 4)`。具体数值随种子变化（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `qr_multiply` 比「`qr` + 手动 `@`」更高效？

**答案**：`qr_multiply` 用 `mode='raw'` 只保留紧凑的 Householder 反射 `(Q, tau)`，然后用 `ormqr/unmqr` 把每个反射**隐式地**依次作用到 `c` 上，既不分配完整的 \(M\times M\) 矩阵 \(Q\)，也避免了显式构造 \(Q\) 的 \(O(M^2K)\) 工作量与一次稠密矩阵乘法。

**练习 2**：对实矩阵，`conjugate=True` 等价于什么？为什么 docstring 说它「可能比显式共轭更快」？

**答案**：对实矩阵 \(Q^H=Q^T\)，所以 `conjugate=True` 等价于求 \(Q^Tc\)。LAPACK 的 `ormqr` 通过把 `trans='T'` 直接传给底层例程，在反射作用时就完成转置，省去了「先共轭/转置再乘」的额外数据搬运，因此更快。

---

### 4.5 rq：RQ 分解

#### 4.5.1 概念说明

RQ 分解把 \(A\) 写成

\[
A = RQ
\]

注意顺序：**R 在左、Q 在右**，与 QR 相反。这里 \(R\) 仍是上三角，\(Q\) 正交/酉。RQ 分解常用于计算机视觉中从射影矩阵恢复相机内参（K 分解）等场景。

直觉上，RQ 可以看作「从右下角往左上角」做的正交三角化，对应 LAPACK 的 `gerqf`（RQ 分解）与 `orgrq`（生成 Q）。

#### 4.5.2 核心流程

`rq` 不走 C++ 批量后端，而是直接在 Python 层用 f2py 包装的 LAPACK：

1. 校验 `mode`（full/r/economic）、`check_finite`、必须二维。
2. 空数组快路径。
3. `get_lapack_funcs` 取 `gerqf`，用 `safecall` 做 RQ 分解，得到分解后的矩阵与 `tau`。
4. 用 `np.triu` 从分解结果里切出上三角 \(R\)（宽窄矩阵切片方式不同）。
5. 若 `mode != 'r'`，再取 `orgrq` 生成显式 \(Q\)（按 \(M,N\) 关系与 mode 选不同切片）。

#### 4.5.3 源码精读

取例程并分解：[safecall gerqf](_decomp_qr.py#L496-L498)，`get_lapack_funcs(('gerqf',), ...)` 按 dtype 自动选 s/d/c/z 前缀，`safecall` 负责自动 lwork（4.3 讲过）。

切出上三角 R：[L499-L502](_decomp_qr.py#L499-L502)，`np.triu(rq, N-M)`（非 economic 或 \(N<M\)）或 `np.triu(rq[-M:, -M:])`（economic 且 \(N\ge M\)）。`N-M` 是偏移量，因为 RQ 的 \(R\) 在矩阵右下角的上方三角带。

生成显式 Q 的三种分支：[L507-L519](_decomp_qr.py#L507-L519) 分别处理 \(N<M\)（取 `rq[-N:]`）、economic（直接用 `rq`）、full（把 `rq` 嵌进 \(N\times N\) 大矩阵 `rq1` 再调 `orgrq`）。三处都用 `safecall` 调 `gorgrq/gungrq`。

注意与 `qr` 的对比：[rq 签名](_decomp_qr.py#L398-L399) 的 `lwork=None` 仍有效（未废弃），且 `rq` **不支持** `pivoting`，也不支持批处理以外的 raw 模式——它的 mode 只有 `full/r/economic` 三种（[L465-L467](_decomp_qr.py#L465-L467)）。

#### 4.5.4 代码实践

> 实践目标：对宽矩阵做 RQ 分解，验证 \(A\approx RQ\)，并对比 full 与 economic 的形状。

```python
import numpy as np
from scipy.linalg import rq

rng = np.random.default_rng(0)
A = rng.standard_normal((6, 9))   # 宽矩阵 M=6, N=9

R, Q = rq(A, mode='full')
Re, Qe = rq(A, mode='economic')
print(R.shape, Q.shape)           # 预期 (6,9) (9,9)
print(Re.shape, Qe.shape)         # 预期 (6,6) (6,9)
print(np.allclose(A, R @ Q))      # 预期 True
print(np.allclose(Q.T @ Q, np.eye(9)))  # Q 正交，预期 True
```

需要观察的现象：`full` 的 \(Q\) 是 \(9\times9\) 方阵，\(R\) 是 \(6\times9\) 且上半部分上三角；`economic` 的 \(R\) 缩成 \(6\times6\)。

预期结果：`full` 返回 `(6,9)` 与 `(9,9)`；`economic` 返回 `(6,6)` 与 `(6,9)`；`A ≈ R@Q` 成立。具体数值**待本地验证**。

#### 4.5.5 小练习与答案

**练习**：QR 与 RQ 在「分解方向」上有何区别？为什么 RQ 切 \(R\) 时要用 `np.triu(rq, N-M)`？

**答案**：QR 从左上角向右下角逐列正交化，\(R\) 落在矩阵左上的标准上三角位置；RQ 则从右下角向左上角处理，分解后 \(R\) 占据矩阵**右下角**的上三角带，相对主对角线的偏移量为 \(N-M\)（列数减行数），所以要用 `np.triu(rq, N-M)` 把这条偏移的上三角带取出来。

---

## 5. 综合实践

把本讲的知识串起来：用列主元 QR 解一个**秩亏损的最小二乘问题**，并用 `qr_multiply` 高效地完成坐标变换。

背景：超定方程 \(Ax\approx b\)，但 \(A\) 的某些列线性相关（秩亏损）。直接解会不稳定；用列主元 QR 可得一个有意义的「基础解」。

```python
import numpy as np
from scipy.linalg import qr, qr_multiply

rng = np.random.default_rng(42)
# 构造秩亏损的 A：4 行 3 列，第 3 列 = 第 1 列 + 2*第 2 列
A = rng.standard_normal((4, 2))
A = np.column_stack([A, A[:, 0] + 2*A[:, 1]])   # 第 3 列相关，秩=2
b = rng.standard_normal(4)

# 第 1 步：列主元 QR
Q, R, P = qr(A, pivoting=True)
print("R 对角绝对值:", np.abs(np.diag(R)))      # 应非增，最后一个明显小
print("列置换 P:", P)

# 第 2 步：高效计算 Q^T b（不显式构造完整 Q）
QTb, _ = qr_multiply(A, np.atleast_2d(b).T, mode='left', conjugate=True)
QTb = QTb.ravel()
print("Q^T b:", QTb)

# 第 3 步：对照——显式 Q 手动算
print("对照 Q.T@b:", Q.T @ b)
print("一致:", np.allclose(QTb, Q.T @ b))        # 预期 True

# 第 4 步：解上三角系统 R[:, :r] y = (Q^T b)[:r]，再按 P 还原 x（r 为数值秩）
r = 2   # 已知秩为 2
y = np.linalg.solve(R[:r, :r], QTb[:r])
x_perm = np.zeros(3)
x_perm[:r] = y
x = np.zeros(3); x[P] = x_perm                    # 按置换还原列顺序
print("残差 ||Ax-b||:", np.linalg.norm(A @ x - b))
print("x:", x)
```

需要观察的现象：
- `R` 的对角线绝对值非增，且最后一个明显偏小（揭示秩亏损）。
- `qr_multiply` 得到的 \(Q^Tb\) 与显式 `Q.T @ b` 一致。
- 还原后的 `x` 满足 \(Ax\approx b\)（残差较小），且对应被选中列有非零值、其余列置零。

预期结果：`一致` 为 `True`；残差为该秩亏损问题下的最小二乘残差。具体数值**待本地验证**（依赖随机种子与 LAPACK 版本的列主元选择）。

> 本实践综合了 4.1（mode）、4.2（pivoting 秩揭示）、4.3（safecall 支撑的 qr_multiply）、4.4（qr_multiply 求 \(Q^Tb\)）四个最小模块。

## 6. 本讲小结

- `qr` 是一层薄壳，把 `mode` 字符串编码成整数（`full=1/r=11/raw=21/economic=31`）后委派 C++ 后端 `_batched_linalg._qr`，真正算力在编译层；Python 只负责校验、归一化、错误聚合与返回值拼装。
- 四种 mode 决定返回粒度：`full` 给完整方阵 \(Q\)，`economic` 给瘦长 \(Q\)，`r` 只给 \(R\)，`raw` 给 LAPACK 内部的 `(Q, tau)`（主要供 `qr_multiply` 复用）。
- `pivoting=True` 切换到列主元 QR（LAPACK `geqp3`），满足 \(A[:,P]=QR\) 且 \(R\) 对角非增，可揭示数值秩。
- `safecall` 封装了「`lwork=-1` 查询最优工作长度 + 翻译 `info<0` 非法参数错误」两件事，是 `qr_multiply`/`rq` 调用 LAPACK 的统一入口。
- `qr` 的 `lwork` 关键字已在 1.18.0 废弃（用 `_NoValue` 哨兵检测），因为它已迁后端；`rq` 的 `lwork` 仍有效。
- `qr_multiply` 复用 `qr(mode='raw')` 拿到紧凑反射，再用 `ormqr/unmqr` 把 \(Q\) 隐式乘到 `c` 上，避免显式构造 \(Q\)；`rq` 则直接走 f2py 的 `gerqf`/`orgrq`，\(A=RQ\)，\(R\) 在右下角上三角带。

## 7. 下一步学习建议

- 阅读 [_decomp_qr.py](_decomp_qr.py) 全文，对照本讲每个 `safecall` 调用点，确认你理解 `lwork` 探测的两步法。
- 进入 [u3-l5（Schur 与 QZ 分解）](u3-l5-schur-and-qz.md)：Schur 分解 \(A=QTQ^H\) 是另一种「正交相似化」，与 QR 互补，是后续矩阵函数（u5）的基础。
- 想了解 QR 增量更新（不重新分解、对已分解的 Q/R 做秩更新或行列增删），看 [u3-l7（Cython _decomp_update）](u3-l7-qr-update.md)，那里会用到本讲提到的 Householder 反射工具。
- 想钻进 C++ 批量后端如何为每种 `QR_mode` 安排缓冲区与是否调用 `or_un_gqr`，读 [src/_linalg_qr.hh](src/_linalg_qr.hh) 与 [u8-l2（C++ 批量后端）](u8-l2-batched-cpp-backend.md)。

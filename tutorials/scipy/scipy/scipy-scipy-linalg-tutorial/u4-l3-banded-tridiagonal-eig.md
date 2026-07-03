# 带状与三对角特征值问题

## 1. 本讲目标

本讲是特征值问题专题的第三篇（承接 u4-l1 一般特征值 `eig` 与 u4-l2 对称/Hermitian 特征值 `eigh`）。

学完本讲，你应该能够：

- 看懂并手工填写「带状矩阵压缩存储格式」（`lower` / upper 两种），知道 `eig_banded` 为何只接收一个 `(u+1, M)` 的小数组而不是整个稠密矩阵。
- 区分 `eig_banded` 与 `eigvals_banded`、`eigh_tridiagonal` 与 `eigvalsh_tridiagonal` 这四组「求特征值 + 特征向量」与「只求特征值」的成对接口，理解后者只是前者的薄封装。
- 掌握三对角问题的 `d` / `e` 输入约定，以及 `eigh_tridiagonal` 如何在 `stevd` / `stebz` / `sterf` / `stev` / `stemr` 五种 LAPACK 驱动间根据 `select` 与 `eigvals_only` 自动分派。
- 理解 `select='a'/'v'/'i'` 旧式部分特征值选择机制，以及它如何经 `_check_select` 翻译成 LAPACK 所需的 Fortran 风格参数 `vl,vu,il,iu,max_ev`。

## 2. 前置知识

在进入源码之前，先用通俗语言铺垫几个概念。

### 2.1 带状矩阵（band matrix）

一个 \(n\times n\) 矩阵 \(A\) 如果只在主对角线附近的一条「带」内可能有非零元，带外全是 0，就称为带状矩阵。形式化地，若存在整数 \(l,u\) 使得

\[
A_{ij}=0 \quad \text{当 } i-j>l \text{ 或 } j-i>u
\]

则 \(l\) 称为下带宽，\(u\) 称为上带宽。\(l=u=0\) 是对角矩阵，\(l=u=1\) 就是三对角矩阵。

既然带外都是 0，就不必把它们存进内存。LAPACK 用一种**压缩存储**只保留带内的非零元，本讲的 `eig_banded` 严格遵循这一约定。

### 2.2 实对称 / Hermitian 特征值回顾

承接 u4-l2：实对称矩阵（\(A=A^\top\)）与复 Hermitian 矩阵（\(A=A^H\)）满足谱定理，特征值全是实数，特征向量可取成正交归一。因此有一类比一般 `eig` 更快、更稳、更省内存的专用驱动。本讲的四个函数都属于这一族——只是把矩阵从「完整稠密」换成了「带状」或「三对角」的压缩表示。

### 2.3 LAPACK 命名速记

LAPACK 例程名形如 `Xyyyt`，本讲涉及前缀：

- `s`/`d` = 单/双精度实数，`c`/`z` = 单/双精度复数。
- `bev` = Band EigonVectors（带状特征值），后缀 `d`（divide-and-conquer 分治）/`x`（expert，可按区间或下标选特征值）。
- `ste` = STridiagonal Eigen（三对角特征值），后缀 `v`/`vd`/`vf`/`mr`/`bz` 是不同的驱动算法。

例如 `dsbevd` = double、symmetric、band、eigenvectors、divide-and-conquer。源码里用 `sbevd` / `hbevd` 这种**去掉类型前缀**的「短名」交给 `get_lapack_funcs`，由它按 dtype 自动补上 `s/d/c/z` 前缀（u7-l1 会详细讲这套分发）。

## 3. 本讲源码地图

本讲四个函数全部集中在同一个文件里，是一个很好的「单文件主题」：

| 函数 | 行号区间 | 作用 |
|------|----------|------|
| `eig_banded` | 688–866 | 对称/Hermitian **带状**矩阵求特征值（及特征向量） |
| `eigvals_banded` | 1091–1182 | `eig_banded(eigvals_only=True)` 的一行薄封装 |
| `eigh_tridiagonal` | 1267–1449 | 对称**三对角**矩阵求特征值（及特征向量），多驱动分派 |
| `eigvalsh_tridiagonal` | 1185–1264 | `eigh_tridiagonal(eigvals_only=True)` 的一行薄封装 |
| `_check_select` | 655–685 | 共享辅助：把 `select`/`select_range` 翻译成 Fortran 风格参数 |
| `_conv_dict` | 650–652 | `select` 字符串 → 整数码的查表 |
| `_check_info` | 1452–1457 | 共享辅助：把 LAPACK 的 `info` 返回值翻译成异常 |

文件头部的导出声明确认这四个函数都进了公共命名空间：

[\_decomp.py#L15-L17](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L15-L17)：把 `eig_banded`、`eigvals_banded`、`eigh_tridiagonal`、`eigvalsh_tridiagonal` 列入 `__all__`，经 `__init__.py` 星号导入汇聚到 `scipy.linalg` 顶层。

## 4. 核心概念与源码讲解

本讲按「先带状、后三对角；先主函数、后薄封装」的顺序拆成四个最小模块，外加一个共享的选择器辅助。

### 4.1 带状特征值问题：eig_banded

#### 4.1.1 概念说明

`eig_banded` 求解

\[
A v_{:,i} = w_i\, v_{:,i}, \qquad v^H v = I
\]

其中 \(A\) 是对称（实）或 Hermitian（复）**带状**矩阵。它的卖点是「输入只占带状压缩存储的 \((u+1)\times M\) 而不是 \(M\times M\)」，对带宽很小的矩阵既省内存又省时间——LAPACK 的带状分治算法复杂度与带宽强相关，远优于把矩阵补成稠密再算。

#### 4.1.2 核心流程

`eig_banded` 是一个典型的「校验 → 归一化 → 选择器翻译 → 委派 LAPACK → 错误翻译」薄壳：

1. **校验与覆写决策**：判断是否需要特征向量、是否允许覆写，决定是否复制输入。
2. **空矩阵特判**：对 \((0,0)\) 形状直接返回空结果。
3. **选择器翻译**：调 `_check_select` 把 `select`/`select_range`/`max_ev` 翻译成 `vl,vu,il,iu,max_ev`。
4. **按 `select` 分派**：
   - `select='a'`（全部）→ `?bevd` 分治驱动（一次给所有特征值/向量）。
   - `select='v'` 或 `'i'`（部分）→ `?bevx` expert 驱动（二分+反迭代，只给需要的特征值/向量）。
5. **裁剪与错误翻译**：expert 驱动只返回 `m` 个特征值，需把数组裁到前 `m` 个，再调 `_check_info` 把 `info` 翻译成异常。

#### 4.1.3 源码精读

先看带状压缩存储的定义，这是理解整个函数的钥匙。docstring 里写得很清楚：

[\_decomp.py#L699-L719](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L699-L719)：定义压缩格式 `a_band[u+i-j, j] == a[i,j]`（upper 形式，\(i\le j\)）或 `a_band[i-j, j] == a[i,j]`（lower 形式，\(i\ge j\)）。即把每一条对角线存成 `a_band` 的一**行**，upper 形式主对角线在最底下一行、最上方留空（`*`），lower 形式主对角线在最顶上一行、最下方留空。

`lower` 默认是 `False`（即 upper 形式）。`u` 是主对角线之上的带数，因此 `a_band` 的形状是 `(u+1, M)`。

再看函数体的关键分支。函数先用装饰器声明按二维批量分片：

[\_decomp.py#L688-L690](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L688-L690)：`@_apply_over_batch(('a_band', 2))` 表示 `a_band` 的核心维度是后两维，前导维度可任意（批量）。注意这是**Python 层**逐片处理，区别于 u4-l1 里 `eig` 的 C++ 原生批处理。

覆写决策里有一个容易忽略的细节：

[\_decomp.py#L798-L805](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L798-L805)：当用户要特征向量（`not eigvals_only`）且**没**显式要求覆写时，函数会 `array(a_band)` **主动复制一份**，并把 `overwrite_a_band` 强制设为 `1`。原因是带状驱动为了算特征向量会原地改写带状存储——为了不污染用户传入的数据，必须先复制。这与 u2-l2 里 `solve` 的「二维 + F 列主序连续」覆写门控是同一类安全考量。

然后是核心的分派逻辑。`select == 0`（即 `'a'`，全部）走分治驱动：

[\_decomp.py#L825-L839](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L825-L839)：按 dtype 选 `sbevd`（实）或 `hbevd`（复），即 `?bevd` 分治驱动，一次算出全部特征值（可选向量）。注释里两段 `# FIXME: implement this somewhen` 说明作者原本想查最优 `lwork`，目前用 LAPACK 内部默认值。

`select in [1, 2]`（按值或按下标选）走 expert 驱动：

[\_decomp.py#L840-L861](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L840-L861)：选 `sbevx`（实）/`hbevx`（复），即 `?bevx`（二分法 + 反迭代）。这里有两个细节值得注意：其一，先用 `lamch('s')`（机器最小正规数）算出 `abstol = 2*lamch('s')`，这是 LAPACK 手册推荐的最优容差，能让 `bevx` 把特征值算到满精度；其二，`bevx` 返回 `m`（实际找到的特征值个数）、`w`（按预留长度开的数组）、`v`（同上）、`ifail`（未收敛向量标记）、`info`，所以末尾要 `w = w[:m]`、`v = v[:, :m]` 裁掉没用到的尾巴。

#### 4.1.4 代码实践

实践目标：用 docstring 自带的例子，亲手把一个稠密对称矩阵转写成带状存储，再调 `eig_banded`，验证结果与稠密 `eigh` 一致。

操作步骤：

```python
import numpy as np
from scipy.linalg import eig_banded, eigh

# 一个 4x4 对称矩阵，上带宽 u=2
A = np.array([[1, 5, 2, 0],
              [5, 2, 5, 2],
              [2, 5, 3, 5],
              [0, 2, 5, 4]], dtype=float)

# lower=True 的带状存储：(u+1, M) = (3, 4)
#   第 0 行 = 主对角线  [1,2,3,4]
#   第 1 行 = 下一条对角线 [5,5,5,0]  (末位 0 占位)
#   第 2 行 = 再下一条     [2,2,0,0]  (后两位 0 占位)
Ab = np.array([[1, 2, 3, 4],
               [5, 5, 5, 0],
               [2, 2, 0, 0]])

w_b, v_b = eig_banded(Ab, lower=True)
w_d,  v_d = eigh(A)

print("特征值是否一致:", np.allclose(np.sort(w_b), np.sort(w_d)))

# 验证特征方程 A @ v = w * v（特征向量顺序可能不同，逐列检查）
res = A @ v_b - v_b * w_b
print("残差 Frobenius 范数:", np.linalg.norm(res))
```

需要观察的现象：

1. 两种方法得到的特征值集合一致（升序后 `allclose` 为真）。
2. 残差的 Frobenius 范数应在 \(10^{-14}\) 量级（双精度）。
3. 把 `Ab` 改成错误的存储（例如交换两行），残差会显著变大甚至特征值都不同——这能帮你体会压缩格式的易错性。

预期结果：特征值一致，残差极小。若运行环境无 SciPy 则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：上例若改用 upper 形式（`lower=False`，默认），`Ab` 应该怎么写？

**答案**：upper 形式主对角线在**最底一行**，越往上的行对应越远的上对角线，行首用 0 占位：

```python
Ab_up = np.array([[0, 0, 2, 2],
                  [0, 5, 5, 5],
                  [1, 2, 3, 4]])
w_up, _ = eig_banded(Ab_up)   # lower 默认 False
np.allclose(np.sort(w_up), np.sort(w_d))   # True
```

**练习 2**：只想取 \([-3, 4]\) 区间内的特征值，参数怎么传？

**答案**：`eig_banded(Ab, lower=True, select='v', select_range=[-3, 4])`，会走 `?bevx` 路径，返回区间内的特征值（见 docstring 示例输出 `[-2.22987175, 3.95222349]`）。

### 4.2 eigvals_banded：一行薄封装

#### 4.2.1 概念说明

很多场景只要特征值、不要特征向量（特征向量是 \(M\times M\) 的大数组，算起来也更贵）。`eigvals_banded` 就是为此提供的便捷入口，避免每次都写 `eigvals_only=True`。

#### 4.2.2 核心流程

它的实现极其简洁——直接把 `eigvals_only` 硬编码为真，其余参数原样转发给 `eig_banded`：

[\_decomp.py#L1180-L1182](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1180-L1182)：`return eig_banded(a_band, lower=lower, eigvals_only=1, overwrite_a_band=overwrite_a_band, select=select, select_range=select_range, check_finite=check_finite)`。

注意它转发时**没有**把 `max_ev` 透传——`eigvals_banded` 的签名里压根没有 `max_ev` 参数，统一用 `eig_banded` 的默认值 `0`。这对绝大多数用法无影响。

#### 4.2.3 源码精读

整个函数除了 docstring 就只有上面这一行 `return`。它不在 `eig_banded` 外包一层装饰器，而是靠 `eig_banded` 自身的 `@_apply_over_batch` 享受批处理能力——薄封装只透传，不重复实现。

#### 4.2.4 代码实践

实践目标：体会 `eigvals_banded` 与 `eig_banded(eigvals_only=True)` 完全等价。

```python
from scipy.linalg import eigvals_banded, eig_banded
w1 = eigvals_banded(Ab, lower=True)
w2 = eig_banded(Ab, lower=True, eigvals_only=True)
print("两者完全相同:", np.array_equal(w1, w2))
```

需要观察的现象：两者逐元素相同（连浮点位都一样，因为底层是同一个 LAPACK 调用）。

#### 4.2.5 小练习与答案

**练习**：既然只是薄封装，为什么 SciPy 还要单独提供 `eigvals_banded`？

**答案**：为了 API 的对称与可发现性——`eigh`/`eigvalsh`、`eig`/`eigvals`、`eig_banded`/`eigvals_banded`、`eigh_tridiagonal`/`eigvalsh_tridiagonal` 是四组成对接口，让用户一眼能看出「这个函数只算特征值」，不必每次都翻文档确认 `eigvals_only` 的默认值是 `False`。

### 4.3 三对角特征值问题：eigh_tridiagonal 的多驱动分派

#### 4.3.1 概念说明

三对角矩阵是带状矩阵中**带宽恰好为 1** 的特例（\(l=u=1\)）。它在科学与工程里极其常见——许多对称特征值算法（包括 `eigh` 用的 `?syevr`）内部都先把矩阵三对角化再求解，所以 LAPACK 为实对称三对角矩阵专门准备了一整套 `STE*` 例程，速度极快。

`eigh_tridiagonal` 接受的输入不是矩阵，而是两条向量：

\[
A = \mathrm{diag}(d) + \mathrm{diag}(e, +1) + \mathrm{diag}(e, -1)
\]

其中 `d` 形状 `(ndim,)` 是主对角线，`e` 形状 `(ndim-1,)` 是非主对角线（上下共用同一条，因为对称）。

#### 4.3.2 核心流程

`eigh_tridiagonal` 的特别之处是它要在**五种 LAPACK 驱动**间做精细分派，这是本讲最值得读的部分。流程：

1. **校验**：`d`/`e` 必须一维、实数（`'GFD'` 复数直接报 `TypeError`），且 `len(d) == len(e)+1`。
2. **选择器翻译**：调 `_check_select`（但忽略它返回的 `max_ev`，三对角驱动不用这个）。
3. **驱动合法性**：`lapack_driver` 必须是 `('auto','stemr','sterf','stebz','stev','stevd')` 之一；`auto` 按 `select` 选 `stevd`（全部）或 `stebz`（部分）。
4. **\(1\times1\) 快速出口**：退化情况直接返回。
5. **按驱动分派**：`sterf`/`stev`/`stevd`/`stebz`/`stemr` 五条分支，各自调用相应例程。
6. **特征向量补算**：若用 `stebz`（只算特征值）又需要特征向量，再补一次 `stein`（反迭代）算向量，并按升序重排。

各驱动的特点对照：

| 驱动 | 算特征值 | 算特征向量 | 支持部分选择 (`select!='a'`) | 算法 |
|------|:---:|:---:|:---:|------|
| `stevd` | ✅ | ✅ | ❌ | 分治（divide-and-conquer） |
| `stev` | ✅ | ✅ | ❌ | 经典 QR |
| `sterf` | ✅ | ❌ | ❌ | Pal-Walker-Kahan（无向量） |
| `stebz` | ✅ | ❌（需配 `stein`） | ✅ | 二分法 |
| `stemr` | ✅ | ✅ | ✅ | MRRR（多相对稳健表示） |

`auto` 默认策略（`select='a'` → `stevd`，否则 → `stebz`）兼顾了「全量要向量时分治最快」与「部分选择时必须用支持子集的驱动」。

#### 4.3.3 源码精读

先看校验段：

[\_decomp.py#L1360-L1370](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1360-L1370)：`d`/`e` 都要一维、实数（`'GFD'` 复数报 `TypeError: Only real arrays currently supported`——注意即使复 Hermitian 三对角也不支持），且 `d.size == e.size + 1`。然后调 `_check_select` 翻译选择器，第六个返回值 `max_ev` 用 `_` 丢弃。

`auto` 的智能选择：

[\_decomp.py#L1377-L1378](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1377-L1378)：`lapack_driver = 'stevd' if select == 0 else 'stebz'`。`select == 0` 即 `'a'`（全部）。

\(1\times1\) 快速出口避免无谓的 LAPACK 调用：

[\_decomp.py#L1380-L1392](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1380-L1392)：单元素矩阵的特征值就是 `d[0]`、特征向量就是 `[1.0]`，并对 `select='v'`（按值选）时该值落在区间外的情况做了空数组返回。

接下来是五条驱动分支中最有代表性的三条。先看 `stebz`（二分法，支持部分选择）：

[\_decomp.py#L1414-L1422](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1414-L1422)：`stebz` 只算特征值。参数 `order = 'E' if eigvals_only else 'B'` 是个细节——只要特征值时按「矩阵顺序」(E) 返回，需要特征向量时按「分块顺序」(B) 返回（因为后面的 `stein` 反迭代要按块组织），最后再统一 `argsort` 重排成升序。

再看 `stemr`（MRRR，最现代的驱动，支持子集且能直接给向量）：

[\_decomp.py#L1423-L1432](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1423-L1432)：注释 `# ?STEMR annoyingly requires size N instead of N-1` 点出一个 LAPACK 接口怪癖——`stemr` 要求非主对角线数组长度为 \(N\) 而非 \(N-1\)，所以代码先 `e_ = empty(e.size+1, ...)` 补零。另外它要先调 `stemr_lwork` 查询最优工作数组长度 `lwork`/`liwork`（「`lwork=-1` 两步法」，u3-l3 讲过这种模式）。

最后看「用 `stebz` 求了特征值、却还要特征向量」的补算逻辑：

[\_decomp.py#L1438-L1448](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1438-L1448)：调 `stein`（反迭代）算特征向量，再用 `argsort(w)` 把前面 `stebz` 的「分块顺序」重排为升序的「矩阵顺序」。这就是上面 `order='B'` 的伏笔。

#### 4.3.4 代码实践

实践目标：把同一矩阵分别写成「`d,e` 三对角」和「稠密」两种形式，验证 `eigh_tridiagonal` 与 `eigh` 给出一致结果；再试一次部分选择。

```python
import numpy as np
from scipy.linalg import eigh_tridiagonal, eigh

d = np.array([3., 3., 3., 3.])
e = np.array([-1., -1., -1.])

# 三对角路径
w_t, v_t = eigh_tridiagonal(d, e)

# 稠密路径：手工拼出同一个矩阵
A = np.diag(d) + np.diag(e, k=1) + np.diag(e, k=-1)
w_d, v_d = eigh(A)

print("特征值一致:", np.allclose(w_t, w_d))
print("残差:", np.linalg.norm(A @ v_t - v_t * w_t))

# 部分选择：只取下标 1..2 的两个特征值
w_sub = eigh_tridiagonal(d, e, eigvals_only=True,
                         select='i', select_range=[1, 2])
print("部分特征值:", w_sub)   # 应为 w_d[1] 和 w_d[2]
```

需要观察的现象：

1. 两条路径特征值完全一致。
2. `select='i', select_range=[1,2]` 取回的恰是升序特征值的第 2、3 个（下标从 0 数）。注意 `select_range` 用的是 Python 0 基下标，`_check_select` 内部会 `+1` 转成 Fortran 1 基。
3. 把 `lapack_driver` 显式设为 `'sterf'` 又同时要特征向量，会抛 `ValueError`——这能帮你理解驱动的能力约束。

#### 4.3.5 小练习与答案

**练习 1**：`eigh_tridiagonal` 为什么不支持复 Hermitian 三对角？

**答案**：源码在 [_decomp.py#L1365-L1366](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1365-L1366) 显式拒绝 `dtype.char in 'GFD'`（复数）。原因是 LAPACK 的 `STE*` 三对角驱动只针对**实对称**矩阵设计；复 Hermitian 三对角没有对应的一阶驱动，得走 `eigh`/`eig_banded` 的一般路径。

**练习 2**：`lapack_driver='auto'`、`select='v'`、又要特征向量时，实际走的是哪条链路？

**答案**：`select='v'`（即 `select != 0`）→ `auto` 选 `stebz`（[L1378](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1378)）→ `stebz` 用 `order='B'` 算区间内特征值 → 再调 `stein` 算对应特征向量 → `argsort` 重排（[L1439-L1446](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1439-L1446)）。

### 4.4 eigvalsh_tridiagonal：又一行薄封装

#### 4.4.1 概念说明

与 `eigvals_banded` 完全对称，`eigvalsh_tridiagonal` 是 `eigh_tridiagonal(eigvals_only=True)` 的便捷封装。但它比 `eigvals_banded` 多透传了 `tol` 与 `lapack_driver` 两个三对角专属参数。

#### 4.4.2 核心流程

[\_decomp.py#L1262-L1264](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1262-L1264)：`return eigh_tridiagonal(d, e, eigvals_only=True, select=select, select_range=select_range, check_finite=check_finite, tol=tol, lapack_driver=lapack_driver)`。

注意它用 `@_apply_over_batch(('d', 1), ('e', 1))`（[L1185](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1185)）声明 `d`、`e` 的核心维度都是 1——这意味着你可以传一堆三对角矩阵（`d` 形状 `(..., M)`、`e` 形状 `(..., M-1)`）批量求解，由 `@_apply_over_batch` 在 Python 层逐片调度。

#### 4.4.3 源码精读

与 4.2 同理：函数体除 docstring 外只有一行 `return`。`tol` 只在 `lapack_driver='stebz'` 时生效（见 docstring [L1221-L1227](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1221-L1227)），表示特征值收敛的绝对区间宽度，默认 `<=0` 时用 `eps*|a|`。

#### 4.4.4 代码实践

```python
from scipy.linalg import eigvalsh_tridiagonal, eigh_tridiagonal
w1 = eigvalsh_tridiagonal(d, e)
w2 = eigh_tridiagonal(d, e, eigvals_only=True)
print("两者完全相同:", np.array_equal(w1, w2))
```

需要观察的现象：逐元素完全相同。

#### 4.4.5 小练习与答案

**练习**：默认 `lapack_driver='auto'` 且 `select='a'` 时，`eigvalsh_tridiagonal` 实际调的是哪个 LAPACK 例程？

**答案**：`auto` + `select='a'`（`select==0`）→ `stevd`（[L1378](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1378)），即双精度实对称三对角的分治驱动 `dstevd`。

### 4.5 共享选择器：_check_select 与 _conv_dict

虽然不在「四个最小模块」之列，但 `_check_select` 被带状与三对角两条路径**共用**，理解它能一次性打通两者的 `select` 语义，值得单列一节。

#### 4.5.1 概念说明

LAPACK 的 `?bevx` / `stebz` 等 expert 驱动用一套 Fortran 风格的参数描述「要哪些特征值」：`range`（0=全、1=按值、2=按下标）、`vl,vu`（值区间）、`il,iu`（下标区间，1 基）、`mmax`（最多要几个）。`_check_select` 把 Python 用户友好的 `select='a'/'v'/'i'` + `select_range=[min,max]` 翻译成这套参数。

#### 4.5.2 源码精读

[\_decomp.py#L650-L652](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L650-L652)：`_conv_dict` 把字符串/整数统一映射成内部码 0/1/2。支持 `'a'/'all'/0`、`'v'/'value'/1`、`'i'/'index'/2` 多种写法。

[\_decomp.py#L655-L685](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L655-L685)：核心逻辑。两处要点：

- `select==1`（按值）：`vl, vu = select_range`；若用户没给 `max_ev`（`max_ev==0`），则默认取 `max_len`（即矩阵阶数，保守上界）。
- `select==2`（按下标）：`il, iu = select_range + 1`——这一步把 Python 的 0 基下标转成 Fortran 的 1 基；并要求 `select_range` 必须是整数 dtype（`sr.dtype.char.lower() in 'hilqp'`），否则报错；`max_ev = iu - il + 1` 精确给出请求个数。

两个函数调用时的差异：

- `eig_banded` 传 `max_ev`（来自用户参数）与 `max_len=a1.shape[1]`（[L821-L822](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L821-L822)）。
- `eigh_tridiagonal` 固定传 `max_ev=0` 与 `max_len=d.size`（[L1369-L1370](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp.py#L1369-L1370)），并丢弃返回的 `max_ev`（三对角驱动用各自的内部计数）。

## 5. 综合实践

设计一个贯穿本讲的小任务：用「三对角矩阵族」对比四种接口，亲手走一遍选择器翻译与驱动分派。

**任务背景**：一维离散拉普拉斯算子（二阶差分）对应的矩阵是经典对称三对角矩阵——主对角线全 2，两条副对角线全 -1。它的特征值有解析解

\[
\lambda_k = 2 - 2\cos\!\left(\frac{k\pi}{N+1}\right),\quad k=1,\dots,N
\]

正好可以拿来验证数值结果。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import (
    eigh_tridiagonal, eigvalsh_tridiagonal,
    eig_banded, eigvals_banded, eigh,
)

N = 50
d = 2.0 * np.ones(N)
e = -1.0 * np.ones(N - 1)

# (1) 三对角路径：四个接口都来一遍
w_full, v = eigh_tridiagonal(d, e)                      # 特征值+向量
w_only    = eigvalsh_tridiagonal(d, e)                  # 仅特征值
w_v       = eigvalsh_tridiagonal(d, e, select='v',
                                 select_range=[0.0, 1.0])  # 按值选
w_i       = eigvalsh_tridiagonal(d, e, select='i',
                                 select_range=[0, 4])      # 按下标选前5个

# (2) 把同一矩阵压成带状存储（u=1），用带状接口交叉验证
Ab = np.vstack([d, np.r_[e, 0.0]])   # lower=True, shape (2, N)
w_b = eigvals_banded(Ab, lower=True)

# (3) 稠密 eigh 作为基线
A = np.diag(d) + np.diag(e, 1) + np.diag(e, -1)
w_dense, _ = eigh(A)

# (4) 解析解
k = np.arange(1, N + 1)
w_exact = 2 - 2 * np.cos(k * np.pi / (N + 1))

print("四条路径一致:",
      np.allclose(w_full, w_only),
      np.allclose(w_full, w_b),
      np.allclose(w_full, w_dense))
print("与解析解吻合:", np.allclose(w_full, w_exact))
print("按值选 [0,1] 的个数:", w_v.size)
print("按下标选前5个:", w_i)
```

**需要观察的现象与预期结果**：

1. `w_full`、`w_only`、`w_b`、`w_dense` 四者完全一致（升序）。
2. 数值特征值与解析解 `w_exact` 在 \(10^{-13}\) 量级吻合。
3. `select='v', select_range=[0,1]` 取回落在 \((0,1]\) 区间的特征值，个数取决于 \(N\)（可数一数解析解里有多少 \(\lambda_k\in(0,1]\)）。
4. `select='i', select_range=[0,4]` 恰好取回最小的 5 个特征值（0 基下标 0..4），与 `w_full[:5]` 一致。

完成后再做一个破坏性实验：把 `eig_banded` 的 `Ab` 第一行（主对角线）与第二行（副对角线）互换，观察特征值如何崩坏——这能强化对「带状压缩存储易错」的印象。若运行环境无 SciPy，则标注「待本地验证」。

## 6. 本讲小结

- `eig_banded` 求对称/Hermitian **带状**矩阵特征值，输入是 `(u+1, M)` 的压缩存储（`lower` 选 upper/lower 两种形式），`select='a'` 走分治 `?bevd`、部分选择走 expert `?bevx`；要特征向量时会主动复制输入以防 LAPACK 原地覆写。
- `eigvals_banded` 与 `eigvalsh_tridiagonal` 都是一行薄封装，把 `eigvals_only` 硬编码为真后转发，不重复实现，但保留 `@_apply_over_batch` 的批量能力。
- `eigh_tridiagonal` 把实对称三对角矩阵（用 `d`/`e` 两条向量表示）的特征值问题分派给 `stevd`/`stev`/`sterf`/`stebz`/`stemr` 五种驱动，`auto` 按 `select` 自动选 `stevd`（全量）或 `stebz`（部分）；`stebz` 只算值，要向量时补 `stein` 反迭代再 `argsort` 重排。
- `select='a'/'v'/'i'` 旧式部分选择经共享的 `_check_select` + `_conv_dict` 翻译成 Fortran 风格参数，注意 `select='i'` 的 `select_range` 是 Python 0 基下标、内部 `+1` 转 Fortran 1 基。
- 这四个函数都用 **Python 层** `@_apply_over_batch` 处理批量维度（区别于 u4-l1 `eig` 的 C++ 原生批处理），且 `_check_info` 把 LAPACK 的 `info` 统一翻译成 `ValueError`（参数非法）或 `LinAlgError`（不收敛）。
- 选型建议：带宽小且矩阵大 → `eig_banded`；纯三对角 → `eigh_tridiagonal`（最快）；只需要少数特征值 → 用 `select='v'/'i'` 让 expert 驱动只算需要的部分。

## 7. 下一步学习建议

- 本讲把特征值专题（u4 单元）收尾。下一步可进入 **u5 矩阵函数**（`expm`、`logm`、`sqrtm`），其中 `sqrtm` 的 Schur 分块算法与本讲的驱动选择思想相通——都是「先把矩阵化简到特殊结构（三角/三对角），再用专用快速算法」。
- 若对底层分发感兴趣，可跳到 **u7-l1（`get_lapack_funcs` 与类型分发）** 深读本讲反复出现的 `get_lapack_funcs((internal_name,), (a1,))` 如何按 dtype 自动补 `s/d/c/z` 前缀。
- 想验证本讲结论，可阅读 `scipy/linalg/tests/test_decomp.py` 中针对 `eig_banded`、`eigh_tridiagonal` 的测试用例，看官方如何构造带状/三对角矩阵并断言正确性——这也是为这些函数贡献边界用例的起点。

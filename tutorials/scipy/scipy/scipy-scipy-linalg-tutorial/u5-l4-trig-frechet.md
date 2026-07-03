# 三角/双曲矩阵函数与 expm 的 Fréchet 导数

## 1. 本讲目标

本讲承接 u5-l1（矩阵指数 `expm`）和 u5-l2（`logm`/`funm`/分数幂）。学完本讲，你应当能够：

- 说出 `cosm`/`sinm`/`tanm`/`coshm`/`sinhm`/`tanhm` 这六个矩阵函数在源码层面**并非逐元素**运算，也**并非**走通用的 `funm`，而是借助欧拉恒等式统一转换为 `expm` 来计算；
- 理解什么是矩阵函数的 **Fréchet 导数**，以及 `expm_frechet` 如何用「缩放-Padé-平方（SPS）」和「分块放大（blockEnlarge）」两种算法求出它；
- 掌握 `expm_cond` 如何通过 Fréchet 导数的 **Kronecker 形式**给出矩阵指数的相对条件数，从而判断 `expm` 对输入扰动的敏感程度；
- 能够动手验证欧拉恒等式 `expm(1j*A) ≈ cosm(A) + 1j*sinm(A)`，并用 `expm_frechet` 估计 `expm` 的方向导数。

## 2. 前置知识

### 2.1 矩阵函数不是逐元素函数

对于标量函数 \(f(x)\)，我们常常希望把它「提升」成矩阵函数 \(f(A)\)。最关键的一点是：**矩阵函数 \(f(A)\) 绝大多数情况下不是把 \(f\) 逐元素作用到矩阵的每个元素上**。例如：

\[ \cos\!\begin{pmatrix} 0 & \pi/2 \\ 0 & 0 \end{pmatrix} \neq \begin{pmatrix} \cos 0 & \cos(\pi/2) \\ \cos 0 & \cos 0 \end{pmatrix} \]

矩阵函数的严格定义来自**矩阵幂级数**或等价的**泰勒展开**。以矩阵指数和矩阵正弦为例：

\[ e^A = \sum_{k=0}^{\infty} \frac{A^k}{k!}, \qquad \sin A = \sum_{k=0}^{\infty} \frac{(-1)^k A^{2k+1}}{(2k+1)!} \]

这里的 \(A^k\) 是**矩阵乘法**（反复相乘），不是逐元素乘方。只有当 \(A\) 是对角矩阵时，矩阵函数才退化成「对角元逐个作用」。

### 2.2 欧拉恒等式（标量版）

对任意标量 \(\theta\)，欧拉恒等式成立：

\[ e^{i\theta} = \cos\theta + i\sin\theta \]

由此可反解出：

\[ \cos\theta = \frac{e^{i\theta}+e^{-i\theta}}{2}, \qquad \sin\theta = \frac{e^{i\theta}-e^{-i\theta}}{2i} \]

对应的双曲恒等式：

\[ \cosh\theta = \frac{e^{\theta}+e^{-\theta}}{2}, \qquad \sinh\theta = \frac{e^{\theta}-e^{-\theta}}{2} \]

### 2.3 为什么欧拉恒等式能直接搬到矩阵上

矩阵函数的幂级数定义保证了：**任何在标量上成立的、由幂级数推出的恒等式，对矩阵也同样成立**（只要涉及的级数都收敛，这里 \(e^A\)、\(\sin A\) 等整函数级数对任意方阵都收敛）。所以：

\[ e^{iA} = \cos A + i\sin A \]

对任意方阵 \(A\) 成立。这是本讲全部三角/双曲矩阵函数的数学基石。

### 2.4 Fréchet 导数（一句话直觉）

标量函数的导数 \(f'(a)\) 告诉我们：自变量微小扰动 \(h\) 时，函数值大约变化 \(f'(a)\,h\)。矩阵函数同理：我们想用一个**关于扰动方向 \(E\) 线性**的算子 \(L_f(A,E)\) 来刻画 \(f(A)\) 对 \(A\) 的微小变化 \(E\) 的一阶响应：

\[ f(A+E) = f(A) + L_f(A,E) + O(\|E\|^2) \]

这个 \(L_f(A,E)\) 就叫 \(f\) 在 \(A\) 处沿方向 \(E\) 的 **Fréchet 导数**。本讲第三、四个模块专门讲 `expm` 的 Fréchet 导数。

> 术语提示：本讲反复出现 `expm`（矩阵指数）、`funm`（通用矩阵函数，基于 Schur 分解，u5-l2 讲过）、LAPACK 等概念，都是前几讲已建立的。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`_matfuncs.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py) | 矩阵函数主模块。本讲的六个三角/双曲函数 `cosm`/`sinm`/`tanm`/`coshm`/`sinhm`/`tanhm` 全部在此，且都通过 `expm` 实现。 |
| [`_expm_frechet.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_expm_frechet.py) | `expm` 的 Fréchet 导数与条件数。`expm_frechet`、`expm_cond` 以及内部算法 `expm_frechet_algo_64`、`expm_frechet_block_enlarge`、`expm_frechet_kronform` 都在此。 |

两个文件之间的关系：`_matfuncs.py` 在文件头部直接 `from ._expm_frechet import expm_frechet, expm_cond`，把这两个函数并入自己的 `__all__`，再经 `__init__.py` 星号导入到顶层命名空间。

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：

1. 三角矩阵函数 `cosm`/`sinm`/`tanm`；
2. 双曲矩阵函数 `coshm`/`sinhm`/`tanhm`；
3. `expm` 的 Fréchet 导数 `expm_frechet`；
4. `expm` 的相对条件数 `expm_cond`。

### 4.1 三角矩阵函数 cosm / sinm / tanm

#### 4.1.1 概念说明

矩阵正弦 \(\sin A\) 与矩阵余弦 \(\cos A\) 由幂级数定义（见 2.1）。直接按幂级数求和既慢又不稳定。scipy.linalg 的做法很巧妙：**不另起炉灶，而是借助欧拉恒等式把它们转成已经实现得很好的 `expm`**。

由 \(e^{iA} = \cos A + i\sin A\) 可得：

\[ \cos A = \frac{e^{iA}+e^{-iA}}{2}, \qquad \sin A = \frac{e^{iA}-e^{-iA}}{2i} \]

于是只要会算 `expm`，就会算 `cosm` 和 `sinm`。

矩阵正切则定义为正弦除以余弦——但因为这是**矩阵除法**，必须用矩阵求解（求逆）来表达：

\[ \tan A = (\sin A)\,(\cos A)^{-1} \]

在源码里对应 `solve(cosm(A), sinm(A))`，即「解方程 \(\cos A \cdot X = \sin A\)」。

> **澄清一个常见误解**：本系列讲义大纲里把三角/双曲矩阵函数描述为「基于 `funm` 的实现」，但**真实源码并非如此**。`funm` 是一个通用的、基于 Schur 分解 + Parlett 递归的矩阵函数求值器（u5-l2 详讲），它对**任意**标量函数都适用，但对重特征值处会失效。scipy.linalg 对三角/双曲这一族**特定**函数采用了更直接、更稳定的欧拉恒等式 + `expm` 路线，绕开了 `funm`。本模块的「源码精读」会清楚地展示这一点。

#### 4.1.2 核心流程

三个函数都是「校验 + 委派给 `expm`」的薄壳：

```
cosm(A):
    A = 校验为方阵 (_asarray_square)
    if A 是复数矩阵:
        return 0.5 * (expm(1j*A) + expm(-1j*A))      # 完整复数公式
    else:
        return expm(1j*A).real                          # 实矩阵：直接取实部

sinm(A):
    A = 校验为方阵
    if A 是复数矩阵:
        return -0.5j * (expm(1j*A) - expm(-1j*A))     # 完整复数公式
    else:
        return expm(1j*A).imag                          # 实矩阵：直接取虚部

tanm(A):
    A = 校验为方阵
    return solve(cosm(A), sinm(A))                      # 解 cos(A)·X = sin(A)
```

为什么实矩阵可以直接取实部/虚部？因为对实矩阵 \(A\)，\(e^{iA} = \cos A + i\sin A\) 中 \(\cos A\)、\(\sin A\) 本身就是实矩阵，所以 `expm(1j*A)` 的实部就是 `cosm(A)`、虚部就是 `sinm(A)`。这比复数分支少算一次 `expm`，是个小优化。

#### 4.1.3 源码精读

先看 `cosm`（包含装饰器与函数体）：

[&#95;matfuncs.py:L437-L475](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L437-L475)：`cosm` 整个实现。第 437 行的 `@_apply_over_batch(('A', 2))` 装饰器让它支持「批处理维度」（前导若干维 + 末尾两个方阵维，详见 u8-l1）；第 471 行 `_asarray_square(A)` 把输入转成 ndarray 并校验是二维方阵；第 472–475 行按复/实分支套用欧拉公式。

关键的两行：

```python
if np.iscomplexobj(A):
    return 0.5*(expm(1j*A) + expm(-1j*A))   # 复矩阵：完整公式
else:
    return expm(1j*A).real                   # 实矩阵：取实部即可
```

再看 `sinm`：

[&#95;matfuncs.py:L478-L516](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L478-L516)：`sinm` 的实现，结构同 `cosm`，只是公式换成 \(-0.5j(e^{iA}-e^{-iA})\)（复）或取 `expm(1j*A).imag`（实）。注意 `-0.5j` 等价于 \(1/(2i)\)。

最后看 `tanm`：

[&#95;matfuncs.py:L519-L556](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L519-L556)：`tanm` 通过 `solve(cosm(A), sinm(A))` 实现「矩阵除法」。第 556 行调用的 `_maybe_real` 是一个善后小工具：

[&#95;matfuncs.py:L60-L91](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L60-L91)：`_maybe_real(A, B)`。当 \(A\) 是实矩阵、但 \(B\)（即 `solve` 的结果）由于浮点误差带上极小的虚部时，它会把 \(B\) 降回实数组。判定阈值与 dtype 精度挂钩（第 88 行 `tol` 取值）。

> 三个函数都依赖的 `_asarray_square` 在 [&#95;matfuncs.py:L36-L57](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L36-L57)，它的作用是 `np.asarray(A)` 后强制要求 `A.ndim==2` 且为方阵，否则抛 `ValueError`。

#### 4.1.4 代码实践

**实践目标**：验证欧拉恒等式 `expm(1j*A) == cosm(A) + 1j*sinm(A)`，并体会实/复分支的差异。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import expm, cosm, sinm

# 1) 实矩阵：直接验证欧拉恒等式
A = np.array([[1.0, 2.0],
              [-1.0, 3.0]])

lhs = expm(1j * A)                       # 左边：直接算 e^{iA}
rhs = cosm(A) + 1j * sinm(A)             # 右边：cos(A) + i·sin(A)
print("实矩阵最大误差：", np.abs(lhs - rhs).max())

# 2) 观察 cosm/sinm 对实矩阵返回实数组（dtype）
print("cosm 返回 dtype：", cosm(A).dtype)   # 应为 float64
print("sinm 返回 dtype：", sinm(A).dtype)

# 3) 复矩阵：走另一个分支，验证恒等式仍成立
Ac = A + 1j * np.array([[0.5, 0.0],
                        [0.0, -0.5]])
lhs_c = expm(1j * Ac)
rhs_c = cosm(Ac) + 1j * sinm(Ac)
print("复矩阵最大误差：", np.abs(lhs_c - rhs_c).max())
print("cosm(复) 返回 dtype：", cosm(Ac).dtype)   # 应为 complex128
```

**需要观察的现象**：第 1 步的最大误差应在 \(10^{-15}\) 量级（纯浮点噪声）；第 2 步 `cosm`/`sinm` 对实矩阵返回 `float64`；第 3 步复矩阵同样满足恒等式，且 `cosm` 返回 `complex128`。

**预期结果**：三处误差都接近机器精度，说明恒等式在实、复两种分支下都精确成立。

#### 4.1.5 小练习与答案

**练习 1**：为什么对**实**矩阵，`cosm` 用 `expm(1j*A).real` 而不是 `0.5*(expm(1j*A)+expm(-1j*A))`？两者结果一样吗？

**参考答案**：数学上两者完全等价（实矩阵的 \(\cos A\) 是实矩阵，等于 \(e^{iA}\) 的实部）。但 `.real` 只需调用**一次** `expm`，而完整公式要调用**两次**（`expm(1j*A)` 和 `expm(-1j*A)`），所以 `.real` 路线更省算力。源码就是据此做了优化。

**练习 2**：若 \(A\) 使得 \(\cos A\) 奇异，调用 `tanm(A)` 会发生什么？

**参考答案**：`tanm` 内部是 `solve(cosm(A), sinm(A))`。当 \(\cos A\) 奇异（即 \(A\) 有形如 \(\pi/2 + k\pi\) 的特征值）时，`solve` 会在 LU 分解时发现主元为零并抛出 `LinAlgError`（与 u2-l2 的奇异处理一致）。这正是标量情形 \(\tan(\pi/2)\) 无定义在矩阵层面的体现。

---

### 4.2 双曲矩阵函数 coshm / sinhm / tanhm

#### 4.2.1 概念说明

双曲矩阵函数 \(\cosh A\)、\(\sinh A\) 由各自的幂级数定义，同样可借恒等式转成 `expm`：

\[ \cosh A = \frac{e^{A}+e^{-A}}{2}, \qquad \sinh A = \frac{e^{A}-e^{-A}}{2} \]

注意它与三角函数的区别：**没有 \(i\)**。双曲函数直接用 \(e^A\) 和 \(e^{-A}\)，所以即便输入是实矩阵，结果也天然是实的，不需要像 `cosm`/`sinm` 那样区分实/复分支取实部。

矩阵双曲正切同样定义为「矩阵除法」：

\[ \tanh A = (\sinh A)\,(\cosh A)^{-1} \]

#### 4.2.2 核心流程

```
coshm(A):  return _maybe_real(A, 0.5 * (expm(A) + expm(-A)))
sinhm(A):  return _maybe_real(A, 0.5 * (expm(A) - expm(-A)))
tanhm(A):  return _maybe_real(A, solve(coshm(A), sinhm(A)))
```

三者都套 `_maybe_real` 做善后：如果输入 \(A\) 是实矩阵但中间结果因浮点误差带上微小虚部，就降回实数组。对双曲函数来说，实输入的理论结果本就是实的，所以 `_maybe_real` 在这里主要是清掉数值噪声。

#### 4.2.3 源码精读

[&#95;matfuncs.py:L559-L596](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L559-L596)：`coshm`，核心即 `0.5 * (expm(A) + expm(-A))`，外面包 `_maybe_real`。

[&#95;matfuncs.py:L599-L636](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L599-L636)：`sinhm`，核心即 `0.5 * (expm(A) - expm(-A))`。

[&#95;matfuncs.py:L639-L676](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L639-L676)：`tanhm`，通过 `solve(coshm(A), sinhm(A))` 实现。

值得注意的是：`coshm`/`sinhm` **没有**像 `cosm`/`sinm` 那样写 `if np.iscomplexobj(A)` 分支，因为公式里没有 \(i\)，实矩阵的 \(e^A\)、\(e^{-A}\) 都是实的，结果自然为实，无需特殊处理。

docstring 里的示例还揭示了一个验证恒等式的好办法：`tanhm(a)` 应当等于 `sinhm(a) @ inv(coshm(a))`，源码示例打印出差值为 \(10^{-15}\) 量级，正说明 `solve` 与显式求逆两种实现一致。

#### 4.2.4 代码实践

**实践目标**：验证双曲恒等式，并确认实矩阵输入得到实数组。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import expm, coshm, sinhm, tanhm, inv

A = np.array([[1.0, 3.0],
              [1.0, 4.0]])

# 1) 验证 cosh/sinh 的 expm 恒等式
ch = 0.5 * (expm(A) + expm(-A))
sh = 0.5 * (expm(A) - expm(-A))
print("coshm 误差：", np.abs(coshm(A) - ch).max())
print("sinhm 误差：", np.abs(sinhm(A) - sh).max())

# 2) 验证 tanhm = sinhm @ inv(coshm)
t_via_solve = tanhm(A)
t_via_inv = sinhm(A) @ inv(coshm(A))
print("tanhm 两种实现误差：", np.abs(t_via_solve - t_via_inv).max())

# 3) dtype 检查：实矩阵输入得到 float64
print("coshm dtype：", coshm(A).dtype)
```

**需要观察的现象**：三处误差都应在 \(10^{-15}\) 量级；`coshm` 返回 `float64`。

**预期结果**：恒等式精确成立，与三角函数模块一样，说明源码忠实地实现了数学定义。

#### 4.2.5 小练习与答案

**练习 1**：把 `coshm` 和 `cosm` 放在一起看：对一个实矩阵 \(A\)，是否有简单的代数关系把双曲余弦和三角余弦联系起来？

**参考答案**：有：\(\cosh A = \cos(iA)\)，\(\sinh A = -i\sin(iA)\)。这从恒等式 \(e^{i(iA)} = e^{-A}\) 即可推出。在源码层面也可验证：`coshm(A)` 与 `cosm(1j*A).real` 数值一致（注意 `cosm` 的复数分支返回复数，需取实部）。

**练习 2**：`coshm` 调用了两次 `expm`（`expm(A)` 与 `expm(-A)`）。能否改写得更省？这种改写有风险吗？

**参考答案**：理论上可令 `eA = expm(A)` 后用 `eA_inv = inv(eA)` 当作 \(e^{-A}\)（因为 \(e^A e^{-A}=I\)）。但这要多做一次矩阵求逆，且当 \(e^A\) 病态时求逆会引入额外误差；而直接 `expm(-A)` 让底层 C 后端独立、稳定地再算一次指数，通常更可靠。scipy 选择了「两次独立 `expm`」的稳健路线，与它「稳定性优先于微小性能」的整体取向一致。

---

### 4.3 expm 的 Fréchet 导数 expm_frechet

#### 4.3.1 概念说明

`expm` 把矩阵 \(A\) 映射成 \(e^A\)。我们自然要问：**当 \(A\) 有微小扰动 \(E\) 时，\(e^A\) 会变化多少？** Fréchet 导数 \(L_{\exp}(A, E)\) 就是这个一阶响应：

\[ e^{A+E} = e^A + L_{\exp}(A,E) + O(\|E\|^2) \]

它对方向 \(E\) 是**线性**的，对基点 \(A\) 一般不是。直觉上，它就是「矩阵指数的方向导数」。

为什么需要它？两个典型场景：

1. **误差/敏感性分析**：想知道「\(A\) 里的一点测量噪声，会让 \(e^A\) 偏多少」，正是 Fréchet 导数的工作。
2. **条件数估计**：把 Fréchet 导数写成矩阵形式后取范数，就能得到条件数（下一模块的 `expm_cond`）。

scipy.linalg 提供两种算法，由 `method` 参数选择：

- **`'blockEnlarge'`**（朴素算法）：构造一个 \(2n\times 2n\) 的分块矩阵 \(\begin{pmatrix} A & E \\ 0 & A \end{pmatrix}\)，对它求一次 `expm`，右上角 \(n\times n\) 块恰好就是 \(L_{\exp}(A,E)\)。原理来自分块上三角矩阵的指数公式。简单但慢（要在 \(2n\) 维上做指数）。
- **`'SPS'`**（默认，Scaling-Padé-Squaring）：Al-Mohy & Higham (2009) 的专用算法，把 `expm` 本身的「缩放-平方 + Padé」流程**对 \(E\) 求微分**，整体只花朴素算法约 \(3/8\) 的时间，渐近复杂度相同。

#### 4.3.2 核心流程

入口 `expm_frechet` 的流程：

```
expm_frechet(A, E, method=None, compute_expm=True, check_finite=True):
    check_finite: 把 A, E 转 ndarray 并拦截 NaN/Inf
    校验 A、E 都是方阵且同形状
    if method is None: method = 'SPS'
    if method == 'SPS':           expm_A, L = expm_frechet_algo_64(A, E)
    elif method == 'blockEnlarge': expm_A, L = expm_frechet_block_enlarge(A, E)
    if compute_expm: return expm_A, L     # 同时返回 e^A 和导数
    else:            return L             # 只要导数
```

**SPS 算法**（`expm_frechet_algo_64`）的骨架，与 u5-l1 讲过的 `expm` 缩放-平方同源，只是处处「配上微分」：

1. 估计 \(\|A\|_1\)，按下表选 Padé 阶数 \(m\in\{3,5,7,9\}\)；若 \(\|A\|_1\) 太大，则先缩放 \(A\leftarrow 2^{-s}A\)、\(E\leftarrow 2^{-s}E\)，改用 \(m=13\)。
2. 对缩放后的 \(A,E\) 计算**微分 Padé**：除了 Padé 多项式 \(U(A),V(A)\)，还要算它们对 \(E\) 的微分 \(\mathrm{d}U,\mathrm{d}V\)。关键在于矩阵乘法的微分满足乘积法则，例如 \(\mathrm{d}(A^2)=A E + E A\)（这正是源码里反复出现的 `M2 = A@E + E@A`）。
3. 做一次 LU 分解、两次求解，得到缩放尺度下的 \(R\approx e^{2^{-s}A}\) 与 \(L\)（即缩放尺度下的 Fréchet 导数）。
4. **平方还原** \(s\) 次：\(R\leftarrow R^2\)，\(L\leftarrow R\cdot L + L\cdot R\)（这一步的 \(L\) 更新来自乘积法则 \(e^{2A}\) 对扰动的一阶展开）。

**blockEnlarge 算法**的数学依据：

\[ \exp\!\begin{pmatrix} A & E \\ 0 & A \end{pmatrix} = \begin{pmatrix} e^A & L_{\exp}(A,E) \\ 0 & e^A \end{pmatrix} \]

这可由分块上三角矩阵的指数级数推出。于是只需对 \(2n\times 2n\) 矩阵求一次 `expm`，左上块得 \(e^A\)、右上块得 \(L_{\exp}(A,E)\)。

#### 4.3.3 源码精读

[&#95;expm&#95;frechet.py:L11-L117](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_expm_frechet.py#L11-L117)：`expm_frechet` 入口。第 10 行的 `@_apply_over_batch(('A', 2), ('E', 2))` 表示它同时支持 \(A\) 和 \(E\) 的批处理维度。第 94–99 行按 `check_finite` 决定是否用 `asarray_chkfinite` 拦截非有限值；第 100–105 行校验 \(A,E\) 均为同形方阵；第 106–113 行按 `method` 分派到两个算法。

[&#95;expm&#95;frechet.py:L120-L130](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_expm_frechet.py#L120-L130)：`expm_frechet_block_enlarge`，朴素算法的全部实现——`vstack/hstack` 拼 \(2n\times 2n\) 块矩阵，调 `scipy.linalg.expm`，再切出两块返回。docstring 自述「mostly for testing and profiling（主要用于测试与性能剖析）」，即它常被当作 SPS 的正确性参照。

[&#95;expm&#95;frechet.py:L137-L161](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_expm_frechet.py#L137-L161)：`ell_table_61`，反向误差阈值表。第 \(m\) 项给出「\(\|2^{-s}A\|\) 不超过该值时，Padé 近似反向误差不超过 \(2^{-53}\)」的上界，SPS 据此挑选阶数与缩放次数。

[&#95;expm&#95;frechet.py:L168-L225](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_expm_frechet.py#L168-L225)：四个 `_diff_pade{3,5,7,9}` 函数。以 [`_diff_pade3`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_expm_frechet.py#L168-L176) 为例，它同时返回 Padé 的 \(U,V\) 与它们的微分 \(Lu,Lv\)。注意第 171 行 `M2 = np.dot(A, E) + np.dot(E, A)` 正是 \(A^2\) 的方向导数（乘积法则）。

[&#95;expm&#95;frechet.py:L228-L281](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_expm_frechet.py#L228-L281)：`expm_frechet_algo_64`，SPS 主算法。第 232 行 `A_norm_1 = scipy.linalg.norm(A, 1)` 用 1-范数选阶；第 238–242 行尝试 \(m=3,5,7,9\)；第 243–272 行处理需要缩放的 \(m=13\) 分支（同时缩放 \(A\) 与 \(E\)，见 246–247 行）；第 274–276 行做一次 `lu_factor`、两次 `lu_solve`（「分解一次、求解两次」，与 u3-l1 的 LU 复用模式一致）；第 278–280 行的平方还原循环里，\(L\) 的更新 `L = R@L + L@R` 来自 \(e^{2A}\) 对扰动的一阶展开。

#### 4.3.4 代码实践

**实践目标**：用 `expm_frechet` 估计 `expm` 对扰动的方向导数，并用 blockEnlarge 与默认 SPS 两种方法交叉验证。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import expm, expm_frechet

rng = np.random.default_rng(0)
A = rng.standard_normal((3, 3))
E = rng.standard_normal((3, 3))

# 1) 默认 SPS 方法，同时返回 e^A 和 Fréchet 导数
expm_A, L_sps = expm_frechet(A, E)
print("e^A 形状：", expm_A.shape, "；L 形状：", L_sps.shape)

# 2) 用 blockEnlarge 方法交叉验证
_, L_blk = expm_frechet(A, E, method='blockEnlarge', compute_expm=False)
print("两种方法 L 的最大差：", np.abs(L_sps - L_blk).max())

# 3) 用「有限差分」验证 L 确实是方向导数
eps = 1e-6
fd = (expm(A + eps*E) - expm(A)) / eps   # 一阶前向差分近似 d/dt expm(A + tE)|_{t=0}
print("L 与有限差分的最大差：", np.abs(L_sps - fd).max())
```

**需要观察的现象**：第 2 步两种算法的 \(L\) 差值应在 \(10^{-14}\) 量级（它们算的是同一个数学量）；第 3 步有限差分与 \(L\) 的差值应随 `eps` 减小而先减小后增大（截断误差 vs 舍入误差的权衡），取 `eps=1e-6` 时通常在 \(10^{-9}\sim10^{-10}\) 量级——这正说明 \(L\) 是导数、有限差分只是它的近似。

**预期结果**：`expm_frechet` 给出的 \(L\) 是精确（机器精度内）的方向导数，远比有限差分可靠。

#### 4.3.5 小练习与答案

**练习 1**：`compute_expm=False` 时返回什么？为什么有时要设成 `False`？

**参考答案**：设 `compute_expm=False` 时只返回 Fréchet 导数 \(L\)，不返回 \(e^A\)。如果你只关心扰动敏感性、不需要 \(e^A\) 本身（例如下一模块 `expm_cond` 内部就是这样），关掉它可以避免一次多余的返回与潜在的计算。

**练习 2**：blockEnlarge 方法的计算量大约是 SPS 的多少倍？依据是什么？

**参考答案**：docstring 明确说 SPS「只花朴素算法约 \(3/8\) 的时间」，即 blockEnlarge 约是 SPS 的 \(8/3\) 倍。原因是 blockEnlarge 要在 \(2n\times 2n\)（4 倍规模、矩阵乘法约 8 倍工作量）上算 `expm`，而 SPS 直接在原规模上微分 Padé。两者渐近复杂度相同，但常数因子差距明显。

---

### 4.4 expm 的相对条件数 expm_cond

#### 4.4.1 概念说明

条件数刻画「输入的相对扰动会被放大成输出的多少倍相对扰动」。对矩阵指数，相对条件数定义为：

\[ \kappa_{\exp}(A) = \max_{E\neq 0} \frac{\|L_{\exp}(A,E)\|/\|e^A\|}{\|E\|/\|A\|} \]

分子是输出的相对一阶变化，分母是输入的相对扰动；取所有方向 \(E\) 的最坏比值，就是最坏情况下的放大倍数。

由于 \(L_{\exp}(A,E)\) 对 \(E\) 线性，可以把它写成矩阵-向量乘法的形式。把 \(E\) 按列拉直成 \(n^2\) 维向量 \(\mathrm{vec}(E)\)，则存在唯一的 \(n^2\times n^2\) 矩阵 \(K_A\) 使得：

\[ \mathrm{vec}\bigl(L_{\exp}(A,E)\bigr) = K_A\,\mathrm{vec}(E) \]

\(K_A\) 称为 Fréchet 导数的 **Kronecker 形式**。于是最坏放大比就归结为：

\[ \kappa_{\exp}(A) = \frac{\|K_A\|_2 \cdot \|A\|_F}{\|e^A\|_F} \]

这里 \(\|K_A\|_2\) 是诱导 2-范数（最大奇异值），\(\|A\|_F\)、\(\|e^A\|_F\) 是 Frobenius 范数。`expm_cond` 返回的就是这个标量 \(\kappa_{\exp}(A)\)：它越大，说明 \(e^A\) 对 \(A\) 的扰动越敏感。

#### 4.4.2 核心流程

```
expm_cond(A):
    校验 A 为方阵
    X = expm(A)
    K = expm_frechet_kronform(A)         # 构造 n²×n² 的 Kronecker 形式
    A_norm = norm(A, 'fro')
    X_norm = norm(X, 'fro')
    K_norm = norm(K, 2)                  # 诱导 2-范数（最大奇异值）
    return (K_norm * A_norm) / X_norm
```

其中 `expm_frechet_kronform` 构造 \(K_A\) 的方法是「逐基向量求导」：对每个标准基 \(E_{ij}=e_i e_j^{\top}\)，调用 `expm_frechet(A, E_{ij})` 得一列，把所有列拼成 \(K_A\)。这要调用 \(n^2\) 次 `expm_frechet`，代价不低，所以 `expm_cond` 主要用于诊断而非热循环。

#### 4.4.3 源码精读

[&#95;expm&#95;frechet.py:L357-L416](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_expm_frechet.py#L357-L416)：`expm_cond`。第 356 行的 `@_apply_over_batch(('A', 2))` 让它支持批量输入。第 405 行算 \(X=e^A\)；第 406 行调 `expm_frechet_kronform` 取 \(K_A\)；第 411–415 行正是公式 \(\kappa = \|K\|_2\|A\|_F/\|X\|_F\)。源码在第 408–410 行特意写了注释「The following norm choices are deliberate（以下范数选择是有意为之）」，强调三种范数各取所需：\(A\)、\(X\) 取 Frobenius，\(K\) 取诱导 2-范数。

[&#95;expm&#95;frechet.py:L304-L353](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_expm_frechet.py#L304-L353)：`expm_frechet_kronform`，构造 Kronecker 形式。第 347–352 行的双重循环遍历所有 \((i,j)\)，第 349 行 `E = np.outer(ident[i], ident[j])` 造标准基 \(E_{ij}\)，第 350–351 行调 `expm_frechet(A, E, compute_expm=False)`，第 352 行用 `vec(F)` 把结果拉直成一列，最后 `vstack(...).T` 拼成 \(K_A\)。

[&#95;expm&#95;frechet.py:L284-L301](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_expm_frechet.py#L284-L301)：`vec(M)`，把矩阵按列拉直成向量（`M.T.ravel()`），是构造 Kronecker 形式时的列堆叠工具。

#### 4.4.4 代码实践

**实践目标**：用 `expm_cond` 估计矩阵指数的条件数，并通过随机扰动验证它确实是「最坏放大倍数」的上界。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import expm, expm_cond

# docstring 里的示例矩阵
A = np.array([[-0.3, 0.2, 0.6],
              [0.6, 0.3, -0.1],
              [-0.7, 1.2, 0.9]])
kappa = expm_cond(A)
print("expm 条件数：", kappa)

# 用随机扰动验证：相对误差应不超过 kappa × 相对扰动
rng = np.random.default_rng(1)
X = expm(A)
eps = 1e-7
E = eps * rng.standard_normal(A.shape) * np.linalg.norm(A)   # 控制相对扰动量级
rel_in  = np.linalg.norm(E) / np.linalg.norm(A)
rel_out = np.linalg.norm(expm(A + E) - X) / np.linalg.norm(X)
print("相对输入扰动：", rel_in)
print("相对输出变化：", rel_out)
print("放大倍数（实测）：", rel_out / rel_in)
print("条件数（理论上界）：", kappa)
```

**需要观察的现象**：实测放大倍数（`rel_out/rel_in`）通常**小于或接近**条件数 `kappa`，因为条件数是「最坏方向」的上界，随机方向一般达不到最坏。若多试几次随机种子，偶尔能接近 `kappa`。

**预期结果**：`rel_out / rel_in <= kappa` 大致成立（在浮点噪声允许范围内），说明 `expm_cond` 确实给出了 `expm` 对扰动的相对敏感性上界。

> 待本地验证：不同随机种子下实测放大倍数的分布；当 \(A\) 本身病态（如有接近 \(2\pi\) 间距的复特征值导致 \(e^A\) 范数结构敏感）时，`kappa` 是否显著变大。

#### 4.4.5 小练习与答案

**练习 1**：`expm_cond` 里 \(K\) 为什么用诱导 2-范数，而 \(A\)、\(X\) 用 Frobenius 范数？

**参考答案**：因为 \(K_A\) 作用在拉直后的向量 \(\mathrm{vec}(E)\) 上，\(\|K_A \mathrm{vec}(E)\| \le \|K_A\|_2 \|\mathrm{vec}(E)\|\)，而 \(\|\mathrm{vec}(E)\|\) 正是 \(\|E\|_F\)。所以用诱导 2-范数刻画「线性算子 \(K_A\) 的最大放大率」、用 Frobenius 范数刻画「向量的长度」，二者搭配才能严格推出 \(\kappa = \|K\|_2\|A\|_F/\|X\|_F\) 这个上界。源码注释「norm choices are deliberate」指的就是这一点。

**练习 2**：`expm_cond` 的计算成本随矩阵阶数 \(n\) 如何增长？

**参考答案**：构造 \(K_A\) 需要对 \(n^2\) 个标准基各调一次 `expm_frechet`，因此成本大致随 \(n^2\) 线性叠加 `expm_frechet` 的开销（每次约 \(O(n^3)\)），总体约 \(O(n^5)\)。这意味着 `expm_cond` 只适合中小规模矩阵的诊断，不适合大矩阵或性能敏感路径。docstring 也提到「1-范数下的更快条件数估计尚未实现」。

---

## 5. 综合实践

把本讲的恒等式与敏感性分析串起来。给定一个实矩阵 \(A\)，请完成：

1. **三角恒等式闭环**：计算 `expm(1j*A)`、`cosm(A)`、`sinm(A)`，验证 `expm(1j*A) ≈ cosm(A) + 1j*sinm(A)`；再验证 `tanm(A)` 与 `sinm(A) @ inv(cosm(A))` 一致。

2. **双曲 vs 三角的桥梁**：验证 `coshm(A) ≈ cosm(1j*A).real`（即 \(\cosh A = \cos(iA)\)），把两个模块的恒等式打通。

3. **Fréchet 导数 vs 有限差分**：取一个扰动方向 \(E\)，用 `expm_frechet(A, E)` 得到精确的方向导数 \(L\)，再用 `(expm(A+eps*E) - expm(A))/eps` 做有限差分近似，比较二者；尝试把 `eps` 从 \(10^{-3}\) 扫到 \(10^{-12}\)，画出误差曲线，观察「先降后升」的典型有限差分行为。

4. **敏感性诊断**：用 `expm_cond(A)` 得到条件数，再用随机扰动验证实测放大倍数不超过该上界。

参考骨架代码：

```python
import numpy as np
from scipy.linalg import expm, cosm, sinm, tanm, coshm, inv, expm_frechet, expm_cond

A = np.array([[1.0, 2.0],
              [-1.0, 3.0]])
E = np.array([[0.5, -0.3],
              [0.2, 0.1]])

# 1) 三角恒等式
assert np.allclose(expm(1j*A), cosm(A) + 1j*sinm(A))
assert np.allclose(tanm(A), sinm(A) @ inv(cosm(A)))

# 2) 双曲-三角桥梁
assert np.allclose(coshm(A), cosm(1j*A).real)

# 3) Fréchet 导数 vs 有限差分
_, L = expm_frechet(A, E)
for eps in [1e-3, 1e-6, 1e-9, 1e-12]:
    fd = (expm(A + eps*E) - expm(A)) / eps
    print(f"eps={eps:.0e}  |L-fd|={np.abs(L-fd).max():.3e}")

# 4) 条件数诊断
kappa = expm_cond(A)
print("expm 条件数 kappa =", kappa)
```

完成本实践后，你应能清楚地解释：为什么这些矩阵函数都最终落到 `expm`，以及 `expm` 的「一阶敏感性」如何被 Fréchet 导数与条件数精确刻画。

## 6. 本讲小结

- `cosm`/`sinm`/`tanm`/`coshm`/`sinhm`/`tanhm` 这六个矩阵函数**不是逐元素运算**，也不是走通用 `funm`，而是借助欧拉恒等式统一转成 `expm` 来计算（澄清了大纲里「基于 funm」的说法）。
- 三角函数用 \(e^{iA}\)（实矩阵可只算一次 `expm` 取实/虚部），双曲函数用 \(e^{A}\)；`tanm`/`tanhm` 通过 `solve` 实现「矩阵除法」。
- 所有六个函数都带 `@_apply_over_batch` 装饰器，支持批处理维度；`_maybe_real` 负责清掉实矩阵结果的微小数值虚部。
- Fréchet 导数 \(L_{\exp}(A,E)\) 是 `expm` 对扰动的一阶线性响应；`expm_frechet` 提供默认的 SPS 算法和用于交叉验证的 blockEnlarge 朴素算法。
- SPS 算法把 `expm` 的缩放-Padé-平方流程对 \(E\) 微分，关键在于矩阵乘积的微分遵循乘积法则（`M2 = A@E + E@A`），平方还原阶段用 `L = R@L + L@R` 更新导数。
- `expm_cond` 通过 Fréchet 导数的 Kronecker 形式 \(K_A\) 给出相对条件数 \(\kappa = \|K_A\|_2\|A\|_F/\|e^A\|_F\)，刻画 `expm` 对输入扰动的最坏放大倍数。

## 7. 下一步学习建议

- 本讲的 Fréchet 导数与条件数只覆盖了 `expm`。若想理解其它矩阵函数（如 `logm`、`sqrtm`、`signm`）的敏感性，可阅读 Higham《Functions of Matrices》中关于各类函数 Fréchet 导数的章节，并对照 scipy 源码看哪些已实现、哪些未实现（如 `expm_cond` docstring 提到的 1-范数快速估计仍属 TODO）。
- 下一单元（u6）将进入**矩阵方程**（Sylvester/Lyapunov/Riccati）与**特殊矩阵构造**。其中 Sylvester 方程 \(RX + XR = T\) 正是 u5-l3 `sqrtm` 块间求解用到的工具，可作为矩阵函数与矩阵方程之间的衔接点继续深读 `_solvers.py`。
- 若对底层实现感兴趣，可回到 u5-l1/u8-l3，对照 C 后端 `src/_matfuncs_expm.c` 看 `expm` 的 Padé 求值如何与本讲 Python 层的 `expm_frechet_algo_64`（Padé 系数表 `b`、阈值表 `ell_table_61`）相互呼应，体会「同一算法在不同语言层的两份实现」。

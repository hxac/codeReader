# Schur 分解、rsf2csf 与 QZ 分解

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚什么是 Schur 分解 \(A=ZTZ^H\)，以及 `output='real'` 与 `output='complex'` 两种返回形式的差异（准上三角 vs 上三角）。
- 知道 `schur` 的 `sort` 参数如何把满足条件的特征值排到左上角，并能看懂返回的 `sdim`。
- 理解 `rsf2csf` 如何用 Givens 旋转把实 Schur 形式里的 2×2 块「拍平」成复上三角。
- 掌握广义 Schur 分解（QZ）\( (A,B)=(QAAZ^*,QBBZ^*) \) 的含义，以及广义特征值为何写成 \(\alpha/\beta\)。
- 能用 `ordqz` 配合 `lhp/rhp/iuc/ouc` 对广义特征值排序，并看懂 `_select_function` 系列区域选择器的实现。

## 2. 前置知识

本讲假设你已经掌握以下概念（前序讲义已建立）：

- **相似变换与特征值**：方阵 \(A\) 的 Schur 分解本质上是一连串酉相似变换，把矩阵化简为「特征值显式出现在对角线上」的形式。如果对特征值本身还不熟，可先看 u4-l1（`eig`）。
- **酉（unitary）/正交矩阵**：满足 \(U^HU=I\) 的复矩阵称为酉矩阵；实矩阵时就是正交矩阵 \(Q^TQ=I\)。本讲里的 \(Z\)、\(Q\) 都是这一类，它保证相似变换不放大数值误差。
- **LAPACK 例程与 `get_lapack_funcs`**：`schur` 调用 LAPACK 的 `gees`，`qz` 调用 `gges`，`ordqz` 调用 `tgsen`。Python 层只做参数校验、dtype 归一化和错误翻译，真正的数值算力在这些编译例程里（见 u7-l1）。
- **`@_apply_over_batch` 装饰器**：本讲的 `schur`、`rsf2csf`、`qz`、`ordqz` 全部带有这个装饰器，意味着它们都支持「批量维度」——前导形状可以是一叠矩阵（见 u3-l1、u8-l1）。

几个本讲特有的术语先点出来：

- **准上三角（quasi-upper triangular）**：对角线上「几乎全是 0」的上三角，但允许在对角线附近出现 2×2 的小块。实 Schur 形式就是这种，2×2 块承载一对复共轭特征值。
- **广义特征值**：对矩阵对 \((A,B)\)，广义特征值问题 \(Ax=\lambda Bx\) 的「特征值」常写成 \(\lambda=\alpha/\beta\)，分别存 \(\alpha\) 和 \(\beta\) 而不是直接相除，是为了让「无穷特征值」（\(B\) 奇异时）也能被表示。

## 3. 本讲源码地图

本讲涉及两个文件，二者结构高度对称：都是「Python 校验薄壳 + LAPACK 例程 + 错误翻译」。

| 文件 | 作用 | 公开函数 |
|---|---|---|
| [_decomp_schur.py](_decomp_schur.py) | 单矩阵的 Schur 分解，以及把实 Schur 形式转复 Schur 形式 | `schur`、`rsf2csf` |
| [_decomp_qz.py](_decomp_qz.py) | 矩阵对的广义 Schur（QZ）分解与重排序 | `qz`、`ordqz`（外加私有 `_select_function` 与 `_lhp/_rhp/_iuc/_ouc`） |

阅读顺序建议：先读 `schur`（最经典），再看 `rsf2csf`（纯 Python 的 Givens 旋转循环，便于理解 2×2 块的含义），然后读 `qz`（结构与 `schur` 几乎镜像），最后读 `ordqz` + `_select_function`（在 QZ 之上多一层重排序）。

## 4. 核心概念与源码讲解

### 4.1 Schur 分解 schur（含 output 与 sort）

#### 4.1.1 概念说明

任意方阵 \(A\)（实或复）都存在 **Schur 分解**：

\[
A = Z\,T\,Z^H
\]

其中 \(Z\) 是酉矩阵（\(Z^HZ=I\)），\(T\) 是上三角矩阵。由于上三角矩阵的特征值就是它的对角元，所以 Schur 分解一次性把「所有特征值」显式地摆在 \(T\) 的对角线上。

这里有个对实矩阵特别重要的小坑：**实数运算做不出复数特征值对应的上三角**。一个实矩阵可以有复特征值（成共轭对出现），但实相似变换无法把矩阵化成「对角线上是复数」的复上三角——因为我们坚持全程用实数。LAPACK 的妥协是：实 Schur 形式（`output='real'`）允许 \(T\) 在对角线上出现 2×2 小块，每个 2×2 块承载一对复共轭特征值；这种形式叫**准上三角**。如果非要纯粹的上三角，就用 `output='complex'`，全程在复数域做，\(T\) 严格上三角，但 \(T\)、\(Z\) 都是复数矩阵。

复矩阵（或 `output='complex'`）则没有这个妥协，\(T\) 直接是复上三角。

`sort` 参数是 Schur 分解的「加分项」：在分解的同时，把满足某个条件的特征值重排到 \(T\) 的左上角，并返回满足条件的个数 `sdim`。这在控制理论里很常用（比如把「稳定」特征值和「不稳定」特征值分开）。

#### 4.1.2 核心流程

`schur` 的 Python 层是一条标准的「校验→归一化→委派→翻译」流水线：

1. **校验输出模式**：`output` 只能是 `'real'`/`'complex'`（或缩写 `'r'`/`'c'`）。
2. **有限性检查**：`check_finite=True` 时用 `asarray_chkfinite` 拦截 NaN/Inf。
3. **整数提升**：整数输入矩阵提升为 `long`（避免整数运算丢失）。
4. **方阵检查**：必须是二维方阵。
5. **dtype 归一化**：若 `output='complex'` 但输入是实数，整体 cast 成 `D`（double complex）或 `F`（single complex）。
6. **空矩阵特判**：返回同形状空数组。
7. **选例程**：用 `get_lapack_funcs(('gees',), ...)` 按 dtype 选 `sgees/dgees/cgees/zgees`。
8. **查询最优工作数组**：先以 `lwork=-1` 调一次 `gees`，拿到 LAPACK 建议的最优 `lwork`，再正式调一次。
9. **构造排序函数**：把 `sort`（callable 或字符串）统一封装成一个 `sfunction`。
10. **正式调用** `gees(sfunction, a1, lwork=lwork, overwrite_a=overwrite_a, sort_t=sort_t)`。
11. **翻译 `info`**：把 LAPACK 返回的错误码翻译成 `ValueError` 或 `LinAlgError`。
12. **组装返回**：无 `sort` 时返回 `(T, Z)`；有 `sort` 时返回 `(T, Z, sdim)`。

伪代码：

```
def schur(a, output='real', sort=None, ...):
    a1 = check + cast(a, output)
    gees = get_lapack_funcs(('gees',), (a1,))
    lwork = gees(..., lwork=-1)[-2][0]      # 查询
    sfunction = build_sfunction(sort)        # 字符串/callable → 函数
    result = gees(sfunction, a1, lwork, overwrite_a, sort_t)
    translate_info(result.info)
    return (T, Z) or (T, Z, sdim)
```

#### 4.1.3 源码精读

**函数签名与装饰器**。`schur` 被 `@_apply_over_batch(('a', 2))` 装饰，说明它支持批量维度（前导形状 + 最后两维是矩阵）：

[_decomp_schur.py:17-19](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_schur.py#L17-L19) —— 装饰器声明 `a` 的最后两维是矩阵维度，其余前导维度按批处理；默认 `output='real'`、`sort=None`。

**Schur 分解的数学定义写在 docstring 里**，务必先读这段建立直觉：

[_decomp_schur.py:23-30](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_schur.py#L23-L30) —— 明确 \(A=ZTZ^H\)，并解释 `output='real'` 时 \(T\) 是准上三角，2×2 块承载复特征值对。

**校验 + 整数提升 + 方阵检查**：

[_decomp_schur.py:141-150](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_schur.py#L141-L150) —— `output` 取值校验、`check_finite` 分支、整数矩阵 cast 成 `long`、非方阵报错。

**output='complex' 的 dtype 归一化**：

[_decomp_schur.py:152-157](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_schur.py#L152-L157) —— 双精度系（`i/l/d`）转 `D`，其余转 `F`，保证后续 `gees` 收到复数。

**选例程 + lwork 两步查询**。这是整个包反复出现的 LAPACK 调用范式（先问工作数组要多大，再正式算）：

[_decomp_schur.py:169-174](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_schur.py#L169-L174) —— `overwrite_a` 用 `_datacopied` 判断输入是否已被拷贝；`get_lapack_funcs` 选 `gees`；`lwork=-1` 查询最优工作数组长度。

**sort 字符串 → sfunction**。注意 lhp/rhp 是实部判定，iuc/ouc 是模长判定，且这个 sfunction 既要支持复数情形（一个参数 \(x\)）也要支持实数情形（两个参数 \(x,y\)，分别是实部和虚部）：

[_decomp_schur.py:182-200](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_schur.py#L182-L200) —— callable 直接用；字符串 `'lhp'/'rhp'/'iuc'/'ouc'` 各对应一个 lambda，签名统一为 `(x, y=None)`，便于 LAPACK 在实/复两种回调下都能调用。

**正式调用 gees**：

[_decomp_schur.py:202-203](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_schur.py#L202-L203) —— 把 `sfunction`、`lwork`、`overwrite_a`、`sort_t`（0/1 开关）一并交给 `gees`。

**info 错误翻译**。LAPACK 约定 `info<0` 是参数非法，`info>0` 是算法失败。`schur` 把几种与排序相关的特殊码分别翻译：

[_decomp_schur.py:205-213](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_schur.py#L205-L213) —— `info<0` → `ValueError`；`info==N+1` → 排序时特征值无法分离；`info==N+2` → 重排后舍入误差导致前导特征值不再满足条件；其余 `info>0` → Schur 形式未收敛（可能病态）。

**返回组装**。注意 `result` 是 `gees` 返回的元组，`result[0]` 是 T，`result[-3]` 是 Z，`result[1]` 是 `sdim`：

[_decomp_schur.py:215-218](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_schur.py#L215-L218) —— 无 `sort` 返回 `(T, Z)`；有 `sort` 多返回 `sdim`。

> 💡 一个容易踩的细节：实模式下 `sort` 的 callable 接收**两个实参**（实部、虚部），复模式下接收**一个复参**。docstring 里专门给了对照示例（见下方实践）。复共轭对只要其中一个满足条件，`sdim` 就 +2。

#### 4.1.4 代码实践

**实践目标**：亲手验证 Schur 分解 \(A=ZTZ^H\)，对比实/复两种输出，并观察 `sort` 的 `sdim`。

把下面脚本存为 `schur_demo.py` 并运行（需要已安装 scipy）：

```python
# 示例代码
import numpy as np
from scipy.linalg import schur

# 用 docstring 里那个有复特征值的矩阵：它的两个复特征值是一对共轭
A = np.array([[0, 2, 2],
              [0, 1, 2],
              [1, 0, 1]], dtype=float)

# 1) 实 Schur：T 是准上三角，2x2 块承载复共轭对
T, Z = schur(A, output='real')
print("实模式 T 对角附近是否有 2x2 块：")
print(T)
# 验证 A = Z @ T @ Z^T（Z 是实正交，故 Z^H = Z^T）
print("实模式残差 ||A - Z@T@Z.T|| =", np.linalg.norm(A - Z @ T @ Z.T))

# 2) 复 Schur：T 是严格上三角
Tc, Zc = schur(A, output='complex')
print("复模式 T 对角线 =", np.diag(Tc))          # 直接看到三个特征值
# 复模式验证要用 Z.conj().T
print("复模式残差 ||A - Zc@Tc@Zc.conj().T|| =",
      np.linalg.norm(A - Zc @ Tc @ Zc.conj().T))

# 3) sort：把虚部 > 0 的特征值排到左上角
#    复模式 callable 接一个复参 x
_, _, sdim_c = schur(A, output='complex', sort=lambda x: x.imag > 1e-15)
print("复模式 sort: 虚部>0 的个数 sdim =", sdim_c)   # 期望 1

#    实模式 callable 接两个实参 (实部, 虚部)；复共轭对算 2 个
_, _, sdim_r = schur(A, output='real', sort=lambda x, y: y > 1e-15)
print("实模式 sort: 虚部>0 的个数 sdim =", sdim_r)   # 期望 2（整对计入）
```

**需要观察的现象**：

- 实模式下 `T` 的右下角是一个 2×2 块（`T[1,0]` 或 `T[2,1]` 附近有非零次对角元），它对应一对复共轭特征值；复模式下 `Tc` 的对角线直接给出三个（可能是复的）特征值。
- 两个残差都应在 \(10^{-14}\) 量级（接近机器精度）。
- `sdim_c` 为 1，`sdim_r` 为 2——这正是 docstring 强调的「实模式下复共轭对算 2 个」。

**预期结果**：残差极小；对角线特征值约为 `2.659` 和 `-0.329 ± 0.802j`。

#### 4.1.5 小练习与答案

**练习 1**：对一个**对称**实矩阵做 `schur(output='real')`，`T` 会是什么形状？还会出现 2×2 块吗？

> **答案**：对称实矩阵的特征值全是实数，`T` 会是严格上三角（且实对称矩阵的 Schur 形其实就是对角化，`T` 实际是对角的，因为对称矩阵的 `T` 也对称、又是上三角，故只有对角线非零）。不会出现 2×2 块——2×2 块只用来承载复共轭特征值对。

**练习 2**：如果对一个有复特征值的实矩阵强行 `schur(output='real', sort='lhp')`（左半平面），`sdim` 会怎么算？

> **答案**：`'lhp'` 判定实部小于 0。对于复共轭对，两个成员实部相同，要么同时满足要么同时不满足；满足的一对会让 `sdim` +2（因为 LAPACK 把整对计入），不满足的整对算 0。docstring 里关于「complex conjugate pairs ... count as 2」说的就是这个。

---

### 4.2 rsf2csf：实 Schur 形转复 Schur 形

#### 4.2.1 概念说明

`rsf2csf` 的名字是 **R**eal **S**chur **F**orm → **C**omplex **S**chur **F**orm 的缩写。它解决一个非常具体的问题：

> 我已经有一个实 Schur 分解 \((T, Z)\)（\(T\) 准上三角，含 2×2 块），我想把它变成复 Schur 分解 \((T', Z')\)（\(T'\) 严格上三角），但**不想重新跑一遍 `schur(output='complex')`**。

为什么不直接重跑？因为重跑要在复数域从头做 QR 迭代，开销大；而 `rsf2csf` 只需要「拍平」那几个 2×2 块——每个 2×2 块对应一对复共轭特征值，用一个 2×2 的 **Givens 旋转**（一种只搅动两个坐标的酉变换）就能把它的次对角元消成 0，同时保持相似性。

数学上，对每个 2×2 块，我们构造一个酉矩阵 \(G\)，在原相似变换上再叠一层：

\[
T' = G\,T\,G^H,\qquad Z' = Z\,G^H
\]

这样 \(A=ZTZ^H=Z'T'Z'^H\) 依然成立，且 \(T'\) 比原来「更三角」。

#### 4.2.2 核心流程

`rsf2csf` 的核心是一个**从下往上**扫的循环（`m` 从 `N-1` 递减到 1），逐个检查次对角元 `T[m, m-1]` 是否构成 2×2 块：

1. **校验** `T`、`Z` 都是方阵且同阶。
2. **dtype 提升**：用 `_commonType` 把 `T`、`Z` 提升到复数（`_castCopy`），因为结果一定是复的。
3. **主循环** `for m in range(N-1, 0, -1)`：
   - 若 \(|T[m,m-1]|\) 超过阈值（相对地「不可忽略」），说明这是一个 2×2 块：
     - 求 2×2 块 \(T[m-1:m+1, m-1:m+1]\) 的特征值，记 \(\mu\)。
     - 构造 Givens 旋转的 \(c, s\)，组装 2×2 酉矩阵 \(G\)。
     - 把相似变换作用到 `T` 的对应行列，并同步更新 `Z`。
   - **无论是否处理**，都把 `T[m, m-1]` 显式置 0（数值上清零次对角）。
4. 返回复的 `(T, Z)`。

为什么从下往上？因为消去一个 2×2 块时，相似变换会扰动它**上方**的列；自底向上处理可以避免已处理过的块被再次搅乱。

#### 4.2.3 源码精读

**装饰器与签名**：同样支持批量：

[_decomp_schur.py:253-254](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_schur.py#L253-L254) —— `rsf2csf(T, Z, check_finite=True)`，两个矩阵参数都按 `@_apply_over_batch` 处理。

**校验与 dtype 提升**：

[_decomp_schur.py:308-322](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_schur.py#L308-L322) —— `check_finite` 分支；分别校验 `Z`、`T` 方阵（错误信息用 `'ZT'[ind]` 指出是哪个出错）；要求两者同阶；`_commonType(Z, T, array([3.0], 'F'))` 强制结果为复数，`_castCopy` 做带拷贝的类型转换。

**核心 Givens 循环**——这是本函数的全部算法：

[_decomp_schur.py:324-337](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_schur.py#L324-L337) —— 自底向上扫描；用相对阈值 `eps*(|T[m-1,m-1]|+|T[m,m]|)` 判断 2×2 块是否「真」存在；对真块求 2×2 特征值 `mu`、算 Givens 参数 `c, s`、组装 `G`，对 `T` 的第 `m-1:m+1` 行（以及第 `m-1:m+1` 列）做相似变换，并同步右乘 `G.conj().T` 更新 `Z`；最后无条件 `T[m, m-1] = 0.0` 清零次对角。

逐行拆解关键计算：

- `mu = eigvals(T[m-1:m+1, m-1:m+1]) - T[m, m]`：求这个 2×2 块的两个特征值，减去 `T[m,m]` 是为了把旋转参数对齐到要消去的那个位置（这里复用了 `_decomp.eigvals`）。
- `r = norm([mu[0], T[m, m-1]])`、`c = mu[0]/r`、`s = T[m, m-1]/r`：构造 Givens 旋转，使得旋转后能把 `T[m, m-1]` 方向的分量消掉。
- `G = [[c.conj(), s], [-s, c]]`：2×2 酉矩阵。
- `T[m-1:m+1, m-1:] = G.dot(T[...])`、`T[:m+1, m-1:m+1] = T[...].dot(G.conj().T)`：左乘 `G`、右乘 `G^H` 是一次酉相似变换，保持特征值不变。
- `Z[:, m-1:m+1] = Z[:, m-1:m+1].dot(G.conj().T)`：把同一个变换累积到 `Z` 上，保证 \(A=ZTZ^H\) 全程成立。

> 💡 这段代码是「相似变换必须成对出现（左乘 + 右乘）」的绝佳教学样例：只左乘或只右乘都会改变特征值，必须 \(G(\cdot)G^H\) 配对才保特征值。

#### 4.2.4 代码实践

**实践目标**：用 `rsf2csf` 把实 Schur 形转成复 Schur 形，并验证它和直接 `schur(output='complex')` 得到的特征值一致。

```python
# 示例代码
import numpy as np
from scipy.linalg import schur, rsf2csf

A = np.array([[0, 2, 2],
              [0, 1, 2],
              [1, 0, 1]], dtype=float)

# 先拿实 Schur
T, Z = schur(A, output='real')

# 再转复 Schur
T2, Z2 = rsf2csf(T, Z)

print("转换后 T2 是否上三角（严格）:", np.allclose(T2, np.triu(T2)))
print("T2 对角线（即特征值）:", np.diag(T2))

# 验证相似关系仍成立：A = Z2 @ T2 @ Z2.conj().T
print("rsf2csf 残差:", np.linalg.norm(A - Z2 @ T2 @ Z2.conj().T))

# 与直接做复 Schur 比，特征值（排序后）应一致
Tc, Zc = schur(A, output='complex')
ev_rsf = np.sort_complex(np.diag(T2))
ev_dir = np.sort_complex(np.diag(Tc))
print("两者特征值最大差:", np.max(np.abs(ev_rsf - ev_dir)))
```

**需要观察的现象**：转换后 `T2` 严格上三角（`np.triu` 判定通过）；残差接近机器精度；两条路径得到的特征值几乎相同（差 \(10^{-14}\) 量级）。

**预期结果**：`T2` 对角线约为 `2.659`、`-0.329+0.802j`、`-0.329-0.802j`。

#### 4.2.5 小练习与答案

**练习 1**：为什么循环结束后要无条件执行 `T[m, m-1] = 0.0`，即使这个位置不是 2×2 块？

> **答案**：即便某个次对角元已经「很小」（低于阈值），它也不是精确的 0，直接当 0 看会让 `T` 在数值上严格上三角。显式置 0 是为了把那些本就是「实特征值之间的耦合」、因浮点误差残留的微小次对角清掉，得到干净的复上三角形式。

**练习 2**：能不能对 `schur(output='complex')` 直接得到的 `(T, Z)` 再调用 `rsf2csf`？

> **答案**：可以调用，但没有意义——复 Schur 形的 `T` 已经是严格上三角，循环里所有 `|T[m,m-1]|` 都低于阈值，循环只是把次对角再清一遍零，结果几乎不变。`rsf2csf` 是专门为「实 Schur 形」设计的。

---

### 4.3 QZ 分解（广义 Schur）qz

#### 4.3.1 概念说明

QZ 分解是 Schur 分解对**矩阵对** \((A, B)\) 的推广，又叫**广义 Schur 分解**：

\[
A = Q\,(AA)\,Z^*,\qquad B = Q\,(BB)\,Z^*
\]

其中 \(Q\)、\(Z\) 都是酉矩阵，\(AA\)、\(BB\) 是广义 Schur 形：\(BB\) 上三角且对角非负，\(AA\) 上三角（复情形）或准上三角（实情形，带 1×1/2×2 块）。

它为什么重要？因为它解决**广义特征值问题** \(Ax=\lambda Bx\)。把上面的分解代入：

\[
Ax=\lambda Bx \;\Longleftrightarrow\; Q(AA)Z^*x=\lambda\,Q(BB)Z^*x
\]

令 \(y=Z^*x\)，得到 \((AA)y=\lambda(BB)y\)。由于 \(AA\)、\(BB\) 都是（准）上三角，广义特征值就是它们**对角元的比值**：

\[
\lambda_j = \frac{\alpha_j}{\beta_j},\qquad \alpha_j=(AA)_{jj},\;\beta_j=(BB)_{jj}
\]

之所以分别存 \(\alpha\) 和 \(\beta\) 而不直接存 \(\lambda\)，是为了能表示**无穷特征值**：当 \(B\) 奇异，某个 \(\beta_j=0\) 而 \(\alpha_j\ne 0\) 时，对应特征值是「无穷大」，直接相除会溢出，分开存就没事。

> ⚠️ 重要变化：`qz` 的 `sort` 参数**已被禁用**（会直接 `raise ValueError`）。排序广义特征值请用下一节的 `ordqz`。这是出于历史上 win32 上的段错误（见源码注释「ticket 1717」）。

#### 4.3.2 核心流程

`qz` 是对私有 `_qz` 的一层薄包装。`_qz` 做全部脏活，结构几乎是 `schur` 的镜像：

1. **拒绝 `sort`**：若 `sort is not None`，立即 `raise ValueError`，引导用户改用 `ordqz`。
2. **校验** `output` 取值；`check_finite` 拦截 NaN/Inf。
3. **方阵 + 同阶检查**：\(A\)、\(B\) 必须都是方阵且同阶。
4. **dtype 归一化**：`output='complex'` 时把两个矩阵都 cast 成复数。
5. **overwrite 判定**：用 `_datacopied` 判断是否已被拷贝。
6. **选例程 + lwork 查询**：`get_lapack_funcs(('gges',))`，先 `lwork=-1` 查询。
7. **正式调用** `gges(sfunction, a1, b1, lwork=..., sort_t=0)`（注意 `sort_t=0`，即不排序）。
8. **翻译 `info`**：`info<0` 非法参数；`0<info<=N` QZ 迭代未完全收敛（发 `LinAlgWarning`，但 \(\alpha,\beta\) 仍可用）；`info==N+1` 其他失败；`info==N+2/N+3` 排序相关失败。
9. `qz` 在 `_qz` 之上只挑出 `AA, BB, Q, Z` 四个返回（丢弃 `sdim`、`alpha`、`beta`、`work`、`info`）。

#### 4.3.3 源码精读

**`sort` 被禁用**——这是和 `schur` 最显眼的区别：

[_decomp_qz.py:73-76](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L73-L76) —— `sort is not None` 时直接报错，注释说明是 win32 上的段错误（ticket 1717），引导改用 `ordqz`。

**dtype 归一化（对 A、B 各做一次）**：

[_decomp_qz.py:93-108](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L93-L108) —— 对 `a1`、`b1` 分别按双精度系/单精度系 cast 成 `D` 或 `F`，与 `schur` 同构。

**选例程 + lwork 查询**：

[_decomp_qz.py:113-118](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L113-L118) —— `get_lapack_funcs(('gges',), (a1, b1))` 选例程；`lwork=-1` 查询最优工作数组。

**正式调用 gges（不排序）**：

[_decomp_qz.py:120-123](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L120-L123) —— 用一个永远返回 `None` 的 `sfunction` 占位，`sort_t=0` 表示不排序。

**info 翻译（含 LinAlgWarning 分支）**：

[_decomp_qz.py:125-142](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L125-L142) —— `0<info<=N` 时只警告不报错（因为 \(\alpha,\beta\) 仍可信）；`N+1/N+2/N+3` 才抛 `LinAlgError`。

**`qz` 薄包装——只挑四个返回**：

[_decomp_qz.py:318-321](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L318-L321) —— 从 `_qz` 返回的 `result` 元组里取 `result[0]=AA`、`result[1]=BB`、`result[-4]=Q`、`result[-3]=Z`；其余（`sdim`、`alpha/beta`、`work`、`info`）丢弃。

> 💡 注意返回顺序是 `AA, BB, Q, Z`，而**不**含 `alpha/beta`。想要广义特征值，要么自己用 `scipy.linalg.eig(A, B)`，要么用下一节的 `ordqz`（它会返回 `alpha/beta`）。还要注意：实情形验证用 `Z.T`，复情形用 `Z.conj().T`——这和 docstring 示例一致。

#### 4.3.4 代码实践

**实践目标**：对矩阵对 \((A,B)\) 做 QZ 分解，验证 \(A\approx Q\,AA\,Z^T\) 与 \(B\approx Q\,BB\,Z^T\)，并对比实/复输出。

```python
# 示例代码
import numpy as np
from scipy.linalg import qz

A = np.array([[1, 2, -1],
              [5, 5, 5],
              [2, 4, -8]], dtype=float)
B = np.array([[1, 1, -3],
              [3, 1, -1],
              [5, 6, -2]], dtype=float)

# 实 QZ
AA, BB, Q, Z = qz(A, B, output='real')
print("实 QZ 验证 A:", np.allclose(Q @ AA @ Z.T, A))
print("实 QZ 验证 B:", np.allclose(Q @ BB @ Z.T, B))

# 广义特征值 = AA 对角 / BB 对角（仅当 BB 对角非零时；含 2x2 块时需谨慎）
print("广义特征值(粗略) =", np.diag(AA) / np.diag(BB))

# 复 QZ：T 是严格上三角，alpha/beta 显式
AAc, BBc, Qc, Zc = qz(A, B, output='complex')
print("复 QZ 验证 A:", np.allclose(Qc @ AAc @ Zc.conj().T, A))
print("复 QZ 广义特征值 = alphac/betac =", np.diag(AAc) / np.diag(BBc))
```

**需要观察的现象**：两个 `allclose` 都为 `True`；实模式下 `BB` 上三角且对角非负、`AA` 准上三角；复模式下 `AAc`、`BBc` 都严格上三角，对角元之比给出全部广义特征值。

**预期结果**：验证全部通过；广义特征值约为 `-1.369/1.719≈-0.796`、复共轭对等（具体值随 LAPACK 版本略有不同，docstring 已注明「may vary」）。

#### 4.3.5 小练习与答案

**练习 1**：`qz` 返回的 `Q` 和 MATLAB 同名函数的 `Q` 有什么差别？

> **答案**：docstring「Notes」明确写了「Q is transposed versus the equivalent function in Matlab」——SciPy 返回的 `Q` 是 MATLAB 版本的转置。验证关系式时 SciPy 用 `Q @ AA @ Z.T`，而 MATLAB 习惯是 `Q' @ A @ Z` 之类，方向相反。

**练习 2**：为什么 `qz` 在 `0 < info <= N` 时只发 `LinAlgWarning` 而不是抛异常？

> **答案**：这种情况表示 QZ 迭代没有完全把矩阵化成 Schur 形，但对角元 \(\alpha_j,\beta_j\)（从而广义特征值）在 `info-1,...,N` 范围内**仍然正确**。所以广义特征值还是可信的，值得用警告提示用户「矩阵本身没化简完」，但不至于让程序崩溃。

---

### 4.4 ordqz 重排序与 _select_function 区域选择器

#### 4.4.1 概念说明

`ordqz` = **ord**er + **qz**，它做两件事：

1. 先对 \((A,B)\) 做一次普通 QZ 分解（复用 `_qz`），拿到广义 Schur 形和 \(\alpha,\beta\)。
2. 再调 LAPACK 的 `tgsen`，把**满足条件的广义特征值重排**到左上角。

为什么要重排？和 `schur` 的 `sort` 同理：很多算法只关心「稳定/不稳定」「单位圆内/外」的特征值，把它们排到一起后，后续可以只处理左上角的子块。典型应用是控制论里把「稳定特征值」（左半平面）和「不稳定特征值」分开，从而计算稳定子空间。

`ordqz` 的 `sort` 参数可以是：

- 一个 callable：接收 `(alpha, beta)` 两个数组（注意是数组，不是单值），返回布尔数组。
- 一个字符串快捷方式，由 `_select_function` 映射到内置选择器：

| 字符串 | 含义 | 判定（对 \(\lambda=\alpha/\beta\)） |
|---|---|---|
| `'lhp'` | 左半平面 | \(\mathrm{Re}(\alpha/\beta) < 0\) |
| `'rhp'` | 右半平面 | \(\mathrm{Re}(\alpha/\beta) > 0\) |
| `'iuc'` | 单位圆内 | \(|\alpha/\beta| < 1\) |
| `'ouc'` | 单位圆外 | \(|\alpha/\beta| > 1\) |

四个内置选择器都**向量化**实现（直接对数组运算），并小心处理两个边界：\((\alpha,\beta)=(0,0)\) 一律返回 `False`；无穷特征值（\(\beta=0,\alpha\ne 0\)）按 docstring 约定——既不在左半也不在右半平面，但算「单位圆外」。

#### 4.4.2 核心流程

`ordqz` 的流程比 `qz` 多一步重排序：

1. 调 `_qz(A, B, output, sort=None, ...)` 得到广义 Schur 形 `(AA, BB, sdim, alphar/alphai/beta 或 alpha/beta, Q, Z, work, info)` 和类型码 `typ`。
2. **组装 `alpha, beta`**：根据 `typ`（`s`/`d`/`c`/`z`）把实情形的 `alphar + alphai*1j` 拼成复 `alpha`；`beta` 直接取。
3. `sfunction = _select_function(sort)`，`select = sfunction(alpha, beta)` 得到布尔数组。
4. `tgsen = get_lapack_funcs('tgsen', (AA, BB))`，按 `typ` 给定 `lwork`（实情形 `4N+16`，复情形 `1` 由 LAPACK 内部定）。
5. 调 `tgsen(select, AA, BB, Q, Z, ijob=0, lwork=..., liwork=1)` 做重排。
6. 再次从 `tgsen` 输出里组装 `alpha, beta`（重排后顺序变了）。
7. 翻译 `info`（`<0` 非法；`==1` 重排因过于病态失败）。
8. 返回 `AAA, BBB, alpha, beta, QQ, ZZ`。

`_select_function` 本身极简：纯查表。

#### 4.4.3 源码精读

**`_select_function` 查表**：

[_decomp_qz.py:15-31](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L15-L31) —— callable 直接返回；字符串 `'lhp'/'rhp'/'iuc'/'ouc'` 映射到 `_lhp/_rhp/_iuc/_ouc`；其余报错。

**内置选择器（向量化 + 边界处理）**。以 `_lhp` 和 `_ouc` 为例，注意它们如何避开除以零：

[_decomp_qz.py:34-40](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L34-L40) —— `_lhp`：先标记 `y!=0` 的位置，仅在那些位置算 `real(x/y)<0`；`(0,0)` 直接置 `False`，避免除零。

[_decomp_qz.py:61-68](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L61-L68) —— `_ouc`（单位圆外）最特别：\((0,0)\) 置 `False`，但 \(\beta=0,\alpha\ne 0\)（无穷特征值）置 `True`——这正对应 docstring「无穷特征值算单位圆外」的约定。

**`ordqz` 第一步：复用 `_qz` 拿广义 Schur 形**：

[_decomp_qz.py:417-420](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L417-L420) —— 注意是 `sort=None`（绕开 `qz` 的禁用），并把返回元组解包。

**组装 alpha/beta（按类型分三种）**：

[_decomp_qz.py:422-427](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L422-L427) —— 单精度实（`s`）拼 `np.complex64`；双精度实（`d`）拼 `1.j`；复情形 `alpha/beta` 已经分开直接取。

**构造 select 并调 tgsen 重排**：

[_decomp_qz.py:429-437](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L429-L437) —— `sfunction(alpha, beta)` 得布尔 `select` 数组；`tgsen` 按 `select` 重排，`ijob=0` 表示只重排不额外算特征向量/条件数（最快的档位）。

**重排后重新组装 alpha/beta + 翻译 info**：

[_decomp_qz.py:440-454](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L440-L454) —— 重排会改变顺序，所以再拼一次 `alpha/beta`；`info==1` 表示矩阵对离广义 Schur 形太远、重排失败（病态），抛 `ValueError`。

> 💡 `_select_function` 接收的是**数组**（`alpha`、`beta` 是长度 N 的向量），所以内置选择器全用 NumPy 向量化写法（`np.real(x/y)`、`abs(x/y)`），而 `schur` 的 `sfunction` 接收的是**单个特征值**（LAPACK 逐个回调）。这是两套排序 API 最容易混淆的地方。

#### 4.4.4 代码实践

**实践目标**：对 \((A,B)\) 做 `ordqz`，把左半平面的广义特征值排到前面，验证排序结果。

```python
# 示例代码
import numpy as np
from scipy.linalg import ordqz

np.random.seed(0)
A = np.array([[2, 5, 8, 7],
              [5, 2, 2, 8],
              [7, 5, 6, 6],
              [5, 4, 4, 8]], dtype=float)
B = np.array([[0, 6, 0, 0],
              [5, 0, 2, 1],
              [5, 2, 6, 6],
              [4, 7, 7, 7]], dtype=float)

# 按左半平面排序：Re(alpha/beta) < 0 的排前面
AA, BB, alpha, beta, Q, Z = ordqz(A, B, sort='lhp')

ev = alpha / beta
print("广义特征值 =", ev)
print("实部 < 0 ?", (ev.real < 0))
print("左半平面特征值个数 =", int(np.sum(ev.real < 0)))

# 验证：前若干个（满足条件的）应排在左上角
# 即前 sdim 个 ev 的实部都应为负，之后的都应为正
print("是否前缀满足 lhp:", np.all(ev.real[:np.sum(ev.real<0)] < 0))

# 复验分解仍成立
print("QZ 关系 A 成立:", np.allclose(Q @ AA @ Z.T, A))
print("QZ 关系 B 成立:", np.allclose(Q @ BB @ Z.T, B))
```

**需要观察的现象**：排序后 `(alpha/beta).real < 0` 中，前若干个为 `True`、之后全为 `False`（满足条件的被排到前面）；分解关系 `Q@AA@Z.T≈A`、`Q@BB@Z.T≈B` 仍然成立（重排不破坏广义 Schur 形）。

**预期结果**：左半平面特征值个数与 docstring 示例一致（`array([True, True, False, False])`，即 2 个）。

> 🔬 **源码阅读型实践（推荐）**：打开 `_decomp_qz.py`，对比 `_lhp`（[_decomp_qz.py:34-40](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L34-L40)）与 `_ouc`（[_decomp_qz.py:61-68](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L61-L68)）对「\(\beta=0\)（无穷特征值）」的处理：`_lhp`/`_rhp`/`_iuc` 都把 `y=0` 的位置直接置 `False`，唯独 `_ouc` 把 `\alpha\ne 0, \beta=0` 置 `True`。在本地用一个 `B` 奇异的矩阵对验证：构造 `B` 使其有一行/列为零（从而出现无穷特征值），观察 `ordqz(..., sort='ouc')` 是否把无穷特征值排到了前面。**待本地验证**具体下标。

#### 4.4.5 小练习与答案

**练习 1**：`ordqz` 返回的 `alpha`、`beta` 是复数数组，但输入是实矩阵对。它们是怎么来的？

> **答案**：见 [_decomp_qz.py:422-427](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L422-L427)。实情形下 `_qz` 返回三个实数组 `alphar, alphai, beta`，`ordqz` 把它们拼成 `alpha = alphar + alphai*1j`，`beta` 保持实数。这样无论实/复情形，对外的 `alpha/beta` 接口统一为复数，方便用户直接算 `alpha/beta` 得到（可能是复的）广义特征值。

**练习 2**：如果我想自己定义一个排序条件「广义特征值的模在 \([0.5, 2]\) 之间排前面」，该怎么调用 `ordqz`？

> **答案**：传一个 callable，它接收 `(alpha, beta)` 两个数组、返回布尔数组：
> ```python
> AA, BB, a, b, Q, Z = ordqz(A, B, sort=lambda a, b: (np.abs(a/b) >= 0.5) & (np.abs(a/b) <= 2))
> ```
> 注意 callable 必须能接受 NumPy 数组（向量化），不能只接受标量——这是 `_select_function` 的约定（见 [_decomp_qz.py:16-18](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_qz.py#L16-L18)）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「分析矩阵谱」的小任务：

**任务**：给定一个实矩阵 \(A\) 和一个矩阵对 \((C,D)\)：

1. 对 \(A\) 做 `schur(output='real')`，再用 `rsf2csf` 转复 Schur 形，打印 \(A\) 的全部特征值（从 `T2` 对角线读）。
2. 把这些特征值按「是否在左半平面」分类（用 `numpy`），统计稳定/不稳定个数。
3. 对 \((C,D)\) 做 `ordqz(sort='lhp')`，把左半平面广义特征值排到前面，并验证重排后 `Q@AA@Z.T≈C`、`Q@BB@Z.T≈D`。
4. 对比：`schur+rsf2csf` 得到的（单矩阵）特征值判定，与 `ordqz` 的（广义）特征值判定，二者的「左半平面」计数逻辑是否同构？

参考框架代码：

```python
# 示例代码
import numpy as np
from scipy.linalg import schur, rsf2csf, ordqz

A = np.array([[0, 2, 2],
              [0, 1, 2],
              [1, 0, 1]], dtype=float)
C = np.array([[2., 5., 8., 7.],
              [5., 2., 2., 8.],
              [7., 5., 6., 6.],
              [5., 4., 4., 8.]])
D = np.array([[0., 6., 0., 0.],
              [5., 0., 2., 1.],
              [5., 2., 6., 6.],
              [4., 7., 7., 7.]])

# 1) 单矩阵：schur -> rsf2csf -> 读特征值
T, Z = schur(A, output='real')
T2, Z2 = rsf2csf(T, Z)
ev_A = np.diag(T2)
print("A 的特征值:", ev_A)
print("A 左半平面个数:", int(np.sum(ev_A.real < 0)))

# 2) 矩阵对：ordqz 重排
AA, BB, alpha, beta, Q, Zqz = ordqz(C, D, sort='lhp')
ev_CD = alpha / beta
print("(C,D) 广义特征值:", ev_CD)
print("(C,D) 左半平面个数:", int(np.sum(ev_CD.real < 0)))
print("分解关系仍成立:", np.allclose(Q @ AA @ Zqz.T, C),
      np.allclose(Q @ BB @ Zqz.T, D))
```

**思考点**：单矩阵的 Schur 把特征值放在 \(T\) 对角线；广义情形把「特征值」拆成 \(\alpha/\beta\) 放在两个矩阵的对角线。两者都用「上三角/准上三角」让特征值显式化，这是 Schur 类分解的核心思想。重排序（`sort` / `ordqz`）则是在不破坏这个结构的前提下，按区域（左/右半平面、单位圆内/外）把特征值分组。

## 6. 本讲小结

- **Schur 分解** \(A=ZTZ^H\) 把任意方阵化成（准）上三角，特征值显式出现在对角线上；实矩阵用 `output='real'` 得准上三角（2×2 块承载复共轭对），`output='complex'` 得严格上三角。
- **`schur` 的 `sort`** 能在分解同时把满足条件的特征值排到左上角，返回 `sdim`；实模式 callable 接两实参、复模式接一复参，复共轭对算 2 个。
- **`rsf2csf`** 用 Givens 旋转自底向上「拍平」实 Schur 形里的 2×2 块，把准上三角变成复上三角；核心是 \(G(\cdot)G^H\) 成对的酉相似变换。
- **QZ 分解** \( (A,B)=(QAAZ^*,QBBZ^*) \) 是 Schur 对矩阵对的推广，广义特征值 \(=\alpha_j/\beta_j\)；`qz` 的 `sort` 已禁用（改用 `ordqz`）。
- **`ordqz`** 先 `_qz` 再 `tgsen`，把满足条件的广义特征值重排；`_select_function` 把 `'lhp'/'rhp'/'iuc'/'ouc'` 映射到向量化选择器，并妥善处理 \((0,0)\) 与无穷特征值两类边界。
- 共性：这四个函数都是「Python 校验薄壳 + LAPACK（`gees`/`gges`/`tgsen`）+ `info` 错误翻译」，都带 `@_apply_over_batch` 支持批量维度，都复用 `_datacopied` 判定 overwrite。

## 7. 下一步学习建议

- **继续矩阵分解线**：本讲承接 u3-l1（LU）、u3-l3（QR）。Schur 分解是矩阵函数（u5）的基石——下一节 `expm`/`sqrtm`/`logm` 都会先用 Schur 分解再处理三角因子，建议直接进入 **u5-l1（expm）** 和 **u5-l3（sqrtm）**。
- **特征值线**：`schur` 和 `qz` 是特征值算法的底层。如果想看 SciPy 怎么把 Schur 分解包装成面向用户的 `eig`/`eigh`，读 **u4-l1（eig）**、**u4-l2（eigh）**，并打开 `scipy/linalg/_decomp.py` 看 `eig` 如何选择 driver。
- **底层例程线**：想弄清 `get_lapack_funcs(('gees',))` 到底怎么按 dtype 选 `s/d/c/zgees`，读 **u7-l1（BLAS/LAPACK 分发）** 和 **u7-l2（f2py 签名文件）**，并在 `flapack*.pyf.src` 里找 `gees`/`gges`/`tgsen` 的签名。
- **建议动手源码**：把 `_decomp_schur.py` 和 `_decomp_qz.py` 并排读，体会两者「镜像」结构；再读 `_decomp.py` 里 `eig` 的 `driver` 选择，理解 Schur 分解在完整特征值计算中的位置。

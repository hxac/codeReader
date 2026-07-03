# 矩阵指数 expm 与 Padé 近似 C 后端

## 1. 本讲目标

矩阵指数 \(e^A\) 是线性代数里最常见的「矩阵函数」，出现在微分方程 \(\dot{x}=Ax\) 的解 \(x(t)=e^{At}x(0)\)、控制论、马尔可夫链、网络中心性等无数场景。本讲聚焦 `scipy.linalg.expm` 的完整实现链路，读完本讲你应当能够：

1. 说清矩阵指数的数学定义，以及为什么不能「逐元素取指数」。
2. 解释「缩放-平方（scaling and squaring）+ Padé 近似」这一工业级算法的整体思路与每一步的作用。
3. 顺着源码走通 `expm` 的调用链：Python 薄壳 → C 模块 `matrix_exponential` → `pick_pade_structure_*`（选阶 + 1-范数估计）→ `pade_UV_calc_*`（Padé 有理式求值）→ 反复平方。
4. 理解 `_matfuncs_expm.c` 作为「底层 Padé 求值与缩放-平方内核」的角色，以及它与 `_matfuncsmodule.c` 的分工。
5. 厘清一个常见误解：当前 `expm` 并**不**做 balancing（平衡），1-范数估计也已下沉到 C 后端内部。

本讲承接 u2-l1（范数与结构检测）与 u3-l5（Schur 分解），是矩阵函数族（u5）的入口。

## 2. 前置知识

### 2.1 矩阵指数的泰勒级数

对标量指数 \(e^x=\sum_{k=0}^{\infty}\frac{x^k}{k!}\)，方阵 \(A\) 的指数用**同一个**幂级数定义：

\[
e^A = \sum_{k=0}^{\infty}\frac{A^k}{k!} = I + A + \frac{A^2}{2!} + \frac{A^3}{3!} + \cdots
\]

注意：\(e^A\) 把「作用在向量上的指数」，而不是对每个元素单独取 `np.exp`。只有当 \(A\) 是对角阵时，\(e^A\) 才恰好等于「逐元素取指数」。

直接截断这个级数（泰勒法）在数值上很差：当 \(\|A\|\) 较大时，中间项 \(A^k/k!\) 会非常大、又互相抵消，浮点精度被瞬间吃光。所以实际实现几乎从不直接求和泰勒级数——它是我们理解定义和做对比基准用的。

### 2.2 缩放-平方思想

利用指数律 \(e^A=(e^{A/2^s})^{2^s}\)：把 \(A\) 先除以 \(2^s\) 缩小到「范数足够小」，对小矩阵用近似方法算 \(e^{A/2^s}\)，再把结果**反复平方** \(s\) 次还原。缩放让近似很准，平方是精确的矩阵乘法。这就是标题里的「scaling and squaring」。

### 2.3 Padé 近似

对小火力（\(\|A\|\) 小）的目标，比泰勒级数好得多的是 **Padé 近似**——用两个多项式之比 \(p_m(A)\,q_m(A)^{-1}\) 逼近 \(e^A\)。同阶下 Padé 比泰勒精确得多，且可以用矩阵乘法 + 一次线性方程组求解完成。本讲会看到 scipy 在阶数 \(m\in\{3,5,7,9,13\}\) 之间挑选。

### 2.4 矩阵 1-范数与它的估计

选阶需要知道 \(\|A\|_1\)（最大列绝对值之和）。精确算 \(\|A\|_1\) 是 \(O(n^2)\) 的便宜操作，但选阶还需要 \(\|A^k\|_1\)（\(k=4,6,8,10\)）这类「幂的范数」。对大矩阵，scipy 用 LAPACK 的 `?lacn2` 做**估计**（只乘几次矩阵-向量就给出下界），这就是后面会看到的 1-范数估计器。

> 术语提示：`?lacn2` 实现的是 Higham–Tisseur 的块 1-范数估计算法（expm 文档里的参考文献 [2]），与 `scipy.sparse.linalg.onenormest` 是同一族方法。本讲后文会专门澄清二者关系。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [`_matfuncs.py`](_matfuncs.py) | 矩阵函数族的 Python 实现。`expm` 的公共入口在这里，是一个只做校验与归一化的薄壳。 |
| [`_matfuncsmodule.c`](_matfuncsmodule.c) | C 扩展 `_internal_matfuncs` 的模块层：解析参数、按 dtype 分派、管理返回内存与引用计数。 |
| [`src/_matfuncs_expm.c`](src/_matfuncs_expm.c) | 真正的算力内核：四种 dtype（s/d/c/z）各自的「选阶 + 范数估计 + Padé 求值 + 缩放-平方」实现。 |
| [`meson.build`](meson.build) | 把上面三个 C 源编译链接成 `_internal_matfuncs` 扩展。 |

依赖关系回顾：`_matfuncs.py` 第 19 行 `from ._internal_matfuncs import recursive_schur_sqrtm, matrix_exponential`，把 C 后端的两个函数挂进 Python 命名空间。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1** `expm` 的 Python 入口（公共薄壳）
- **4.2** C 后端 `matrix_exponential`：缩放-平方主循环
- **4.3** Padé 阶数选择与 1-范数估计（含 balancing 澄清）
- **4.4** Padé 有理式求值 `pade_UV_calc`

### 4.1 expm 的 Python 入口：一层只管校验的薄壳

#### 4.1.1 概念说明

`scipy.linalg.expm(A)` 对外接受形状 `(..., n, n)` 的数组（末尾两维是方阵，前面可以有任意「批处理」维度），返回同形状的 \(e^A\)。但 Python 这一层**不做任何数值计算**——它只负责：

1. 把输入变成 `ndarray`；
2. 处理一堆「退化情形」（标量、空数组、\(1\times1\)、整数/float16 dtype）；
3. 把正常情形交给 C 后端 `matrix_exponential`；
4. 翻译 C 后端回传的 `info` 错误码。

真正的「缩放-平方 + Padé」全部在 C 里。这与本手册反复强调的 scipy.linalg 架构一致：**Python 薄壳做校验与错误聚合，编译后端做数值计算**。

#### 4.1.2 核心流程

```
expm(A)
 ├─ np.asarray(A)，并对罕见 dtype 做弃用提示 (_deprecate_dtypes)
 ├─ 退化快路径：
 │    ├─ 标量（size==1 且 ndim<2）        → 返回 [[exp(标量)]]
 │    ├─ ndim<2 或末尾两维不等             → 抛 LinAlgError
 │    ├─ 空数组                            → 返回同形空数组
 │    └─ (1,1) 末尾切片                    → 逐元素 np.exp
 ├─ dtype 归一化：整数→float64，float16→float32
 ├─ eA, info = matrix_exponential(a)      ← 进入 C 后端
 └─ 按 info 翻译错误：
      info <= -11  → MemoryError
      其它 info!=0 → RuntimeError
```

#### 4.1.3 源码精读

公共入口与文档字符串：[\[`_matfuncs.py`:L210-L280\]](_matfuncs.py#L210-L280) 定义 `expm`，文档里点明算法就是参考文献 [1]（Al-Mohy & Higham 2009）的「变阶 Padé + scaling and squaring」，并提示 \(n\ge 400\) 时改用 1-范数估计（参考文献 [2]）来决定阶数。

退化情形与 dtype 归一化：[\[`_matfuncs.py`:L281-L304\]](_matfuncs.py#L281-L304)。注意 \(1\times1\) 走 `np.exp`、整数被提升为 `float64`、`float16` 提升为 `float32`——C 后端只接受 `float32/float64/complex64/complex128` 四种 dtype。

委派与错误翻译：[\[`_matfuncs.py`:L310-L323\]](_matfuncs.py#L310-L323)。核心就一行 `eA, info = matrix_exponential(a)`，随后按 `info` 把 C 层的失败翻译成 Python 异常。`info <= -11` 是 C 层 `malloc` 失败的约定（见 4.2.3），故映射到 `MemoryError`。

C 后端的导入位置：[\[`_matfuncs.py`:L19\]](_matfuncs.py#L19)，`matrix_exponential` 来自 `._internal_matfuncs`。

#### 4.1.4 代码实践

**目标**：验证「退化快路径」确实绕过了重型算法。

```python
# 示例代码
import numpy as np
from scipy.linalg import expm

# 1) 零矩阵 → 单位阵（数学事实：e^0 = I）
Z = np.zeros((3, 3))
print(expm(Z))
# 预期：3x3 单位阵

# 2) 标量路径：size==1 且 ndim<2
print(expm(np.array(1.0)))        # 预期：[[e]] 即 [[2.71828183]]

# 3) (1,1) 末尾切片走 np.exp
print(expm(np.array([[2.0]])))    # 预期：[[7.3890561]]

# 4) 非方阵末尾两维 → 抛 LinAlgError
try:
    expm(np.zeros((2, 3)))
except np.linalg.LinAlgError as e:
    print("caught:", e)
```

**操作步骤**：把上面的代码存成 `expm_intro.py` 用 `python expm_intro.py` 运行。

**需要观察的现象**：前三个调用都「立刻」返回且结果与数学预期一致；第四个抛出 `LinAlgError`（注意 scipy 复用了 numpy 的 `LinAlgError`，参见 u1-l1）。

**预期结果**：零矩阵得到单位阵；标量与 `(1,1)` 走的是 `np.exp` 快路径，不会触发 C 后端的 Padé 内核。

> 说明：以上结果由 `expm` 的退化分支与数学定义共同保证，可放心验证。

#### 4.1.5 小练习与答案

**练习 1**：传一个整数矩阵 `np.array([[0,1],[1,0]])` 给 `expm`，会发生什么？返回的 dtype 是什么？

**答案**：整数不是 `inexact`，会先被 `astype(np.float64)` 提升（[\[`_matfuncs.py`:L301-L302\]](_matfuncs.py#L301-L302)），再进入 C 后端。返回的是 `float64` 数组。结果为 \(\cosh(1)I+\sinh(1)\cdot\begin{pmatrix}0&1\\1&0\end{pmatrix}\)（因为该矩阵平方等于 \(I\)）。

**练习 2**：为什么 `expm` 在末尾两维不等时抛 `LinAlgError` 而非 `ValueError`？

**答案**：矩阵指数只对**方阵**有定义（幂级数里 \(A^k\) 要求 \(A\) 可自乘），这是线性代数层面的非法输入，故用 `LinAlgError`。

---

### 4.2 C 后端 matrix_exponential：缩放-平方主循环

#### 4.2.1 概念说明

C 扩展 `_internal_matfuncs` 暴露了两个方法（`matrix_exponential` 和 `recursive_schur_sqrtm`，后者服务 `sqrtm`）。`matrix_exponential` 这一层做三件事：

1. **模块层**（`_matfuncsmodule.c`）：解析 Python 传入的数组、校验 dtype 与方阵性、按 dtype 选 `matrix_exponential_s/d/c/z` 之一、用 capsule 管理 `malloc` 出来的结果内存；
2. **内核层**（`src/_matfuncs_expm.c` 里的 `matrix_exponential_d` 等）：对每个 `n×n` 切片执行完整算法；
3. 支持批处理：`(..., n, n)` 在内核里用一个外层循环逐片处理。

四种 dtype 的实现是平行的四份代码（`_s/_d/_c/_z`），结构完全一致，只是元素类型与 BLAS 前缀（`s/d`）或 LAPACK 前缀不同。下面以双精度 `matrix_exponential_d` 为代表作精读。

#### 4.2.2 核心流程

对每个切片，内核执行：

```
matrix_exponential_d(a, result, info):
  分配工作区 Am[6*n*n + 4*n] 与 ipiv[n]
  for 每个切片:
    1. 把切片拷进连续的列主序缓冲 Am1
    2. 算带宽 (lband, uband)
       ├─ 全 0（对角阵）→ 逐元素 exp 对角元，continue
       └─ 否则 swap_cf 转列主序存入 Am[0]
    3. 若是三角阵（lband 或 uband 为 0）：抽出对角 diag_aw 与次对角 sd 备用
    4. pick_pade_structure_d(Am, n, &m, &s)   ← 选 Padé 阶数 m 与缩放次数 s
    5. pade_UV_calc_d(Am, ipiv, n, m, info)   ← 在 Am[0] 写入 e^{A/2^s} 的近似
    6. 若 s>0：反复平方 s 次
       ├─ 三角阵：用 Fragment 2.1（Al-Mohy & Higham 2009）保精度
       └─ 一般稠密：dgemm 自乘 s 次
    7. 把结果拷回 result
  释放工作区
```

整体仍是「缩放（在第 4 步隐式确定 \(s\)，第 5 步实际算的是 \(e^{A/2^s}\)）→ 平方（第 6 步）」的两段式。

#### 4.2.3 源码精读

模块层入口 `matrix_exponential`：[\[`_matfuncsmodule.c`:L184-L211\]](_matfuncsmodule.c#L184-L211) 解析参数、校验「dtype 属于四种之一且 ndim≥2」「末尾两维相等」。

按 dtype 分派并分配结果缓冲：[\[`_matfuncsmodule.c`:L216-L241\]](_matfuncsmodule.c#L216-L241)。注意结果用 `calloc`（清零），再调对应的 `matrix_exponential_s/d/c/z`。

内存所有权与错误回传：[\[`_matfuncsmodule.c`:L243-L272\]](_matfuncsmodule.c#L243-L272)。若 `info<0`，先 `free` 掉缓冲、返回 `(None, info)`；否则把裸缓冲包成 `ndarray`，再用 `PyCapsule` 持有那块 `malloc` 内存（析构时 `free`），保证 Python 侧 `del` 数组时内存被正确回收。

> 这正是 4.1.3 里 `info <= -11 → MemoryError` 的来源：C 内核在 `malloc` 失败时把 `info` 写成 `-100`/`-101`（见下），经此层透传回 Python。

模块方法表与初始化：[\[`_matfuncsmodule.c`:L279-L287\]](_matfuncsmodule.c#L279-L287) 注册 `matrix_exponential`；[\[`_matfuncsmodule.c`:L317-L329\]](_matfuncsmodule.c#L317-L329) 定义模块 `_internal_matfuncs` 并实现 `PyInit__internal_matfuncs`（模块名由此固定，参见 u1-l3 的 `PyInit_` 命名约定）。

内核主循环开头与工作区分配：[\[`src/_matfuncs_expm.c`:L2349-L2381\]](src/_matfuncs_expm.c#L2349-L2381)。注释写明工作区为何是 `6*n*n + 4*n`：`5*n*n` 存放 \(A\) 的各次幂，`n*n` 存 \(|A|\)，`2*n` 做 1-范数，`2*n` 给三角路径。`malloc` 失败置 `*info=-100/-101`。

对角阵快路径与转列主序：[\[`src/_matfuncs_expm.c`:L2400-L2407\]](src/_matfuncs_expm.c#L2400-L2407)。带宽全 0 时直接 `exp` 对角元——这正是 \(e^{\text{对角阵}}\) 等于「逐元素取指数」的特例。否则用 `swap_cf_d` 把任意内存布局的切片规整成列主序，供 BLAS/LAPACK 使用。

调用选阶与 Padé 求值：[\[`src/_matfuncs_expm.c`:L2429-L2442\]](src/_matfuncs_expm.c#L2429-L2442)，`pick_pade_structure_d` 选 `(m, s)`，`pade_UV_calc_d` 求值；任一返回 `info<0` 即释放资源并带上错误码返回。

平方阶段（一般稠密情形）：[\[`src/_matfuncs_expm.c`:L2484-L2493\]](src/_matfuncs_expm.c#L2484-L2493)，`s` 次 `dgemm` 自乘并把指针来回交换——即 \((e^{A/2^s})^{2^s}=e^A\) 的「squaring」。

三角情形的精度强化路径：[\[`src/_matfuncs_expm.c`:L2448-L2483\]](src/_matfuncs_expm.c#L2448-L2483)。对三角矩阵，反复平方会让对角线精度受损，故每次平方后用 `exp(d_i)` 重算对角、用 `exp_sinch`（即 \((e^{b}-e^{a})/(b-a)\) 的稳定求值）重算次对角——这是 Al-Mohy & Higham (2009) 的 Fragment 2.1。

#### 4.2.4 代码实践

**目标**：用「带宽快路径」的存在解释一个现象——`expm` 对对角阵极快且精确。

```python
# 示例代码
import numpy as np
from scipy.linalg import expm
import time

n = 2000
D = np.diag(np.arange(1.0, n+1.0))   # 纯对角阵
t0 = time.perf_counter(); E = expm(D); t1 = time.perf_counter()
print("expm(diag) 耗时 %.4fs" % (t1 - t0))

# 与逐元素 exp 对比（对角阵的精确解）
expected = np.diag(np.exp(np.arange(1.0, n+1.0)))
print("最大误差:", np.abs(E - expected).max())
```

**操作步骤**：运行脚本，观察耗时与误差。

**需要观察的现象**：即便 \(n=2000\)，`expm` 也几乎瞬间完成，且与逐元素 `np.exp` 完全吻合——因为内核在 [\`bandwidth\`](src/_matfuncs_expm.c#L2400-L2406) 检测到全 0 带宽后直接走了对角快路径，根本没有进入 Padé/平方。

**预期结果**：耗时在毫秒量级，最大误差为 0（或几个 ulp 内的舍入）。

> 说明：对角快路径的误差仅来自 `exp` 本身，可放心验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么工作区要预留 `5*n*n` 给「\(A\) 的各次幂」？算法最高用到 \(A\) 的几次幂？

**答案**：选阶阶段会预计算 \(A^2, A^4, A^6\)（见 4.3），Padé 求值最高阶 \(m=13\) 时还用到 \(A^4\)、\(A^6\) 的组合；`Am[0..4]` 共 5 块用来滚动存放这些幂次（\(A, A^2, A^4, A^6\) 及一块临时），外加 `Am[5]` 存 \(|A|\)。最高显式用到 \(A^{10}\) 的范数（4.3 中的 `d10`）。

**练习 2**：`matrix_exponential`（模块层）在 `info<0` 时为什么必须 `free(mem_ret)` 再返回？

**答案**：失败时还没有把缓冲绑定到 `ndarray`/capsule，若不手动 `free` 就会泄漏；成功路径才由 capsule 析构负责释放。这是 C 扩展里典型的「失败路径显式释放、成功路径交给 Python GC」模式。

---

### 4.3 Padé 阶数选择与 1-范数估计（pick_pade_structure）

#### 4.3.1 概念说明

「选阶」要回答两个问题：

- **用几阶 Padé？** 候选 \(m\in\{3,5,7,9,13\}\)。阶越高越准但越贵。
- **要缩放几次？** 即找最小的 \(s\) 使得 \(\|A/2^s\|_1\) 落到「该阶 Padé 误差低于单位舍入」的门槛之内。

理论给出每个阶 \(m\) 对应的阈值 \(\theta_m\)：只要 \(\|A\|_1<\theta_m\)，\([m/m]\) Padé 近似的相对误差就低于 `eps`。scipy 里这五个阈值硬编码为：

\[
\theta_3\approx1.4956\times10^{-2},\quad
\theta_5\approx0.25394,\quad
\theta_7\approx0.95042,\quad
\theta_9\approx2.09785,\quad
\theta_{13}=4.25
\]

算法从小阶往大阶试：若某阶的范数判据满足、且**无需额外缩放**，就用它返回；否则继续。最后兜底用 \(m=13\) 并计算需要的缩放次数

\[
s=\bigl\lceil\log_2(\eta/\theta_{13})\bigr\rceil
\]

其中 \(\eta\) 是 \(\|A^4\|^{1/4},\|A^6\|^{1/6},\|A^8\|^{1/8},\|A^{10}\|^{1/10}\) 的某种极小组合（用幂的范数估计 \(\|A\|\)，避免直接算高次幂造成的溢出与精度损失）。

#### 4.3.2 核心流程

```
pick_pade_structure_d(Am, n, &m, &s):
  置 theta[5]、coeff[5]
  算 |A|，并估算 ||A||_1（一次 absA·1 幂迭代）
  显式算 A^2、A^4、A^6，得 d4=||A^4||^(1/4)、d6=||A^6||^(1/6)
  试 m=3：估 ||A^7||_1，结合 coeff[0] 算「该阶所需缩放」lm
         若 max(d4,d6)<theta[0] 且 lm==0 → m=3, 返回
  试 m=5：估 ||A^11||_1，类似判断 → m=5, 返回
  试 m=7：算 ||A^8||（n<400 精确 / n>=400 估计），估 ||A^15||_1 → m=7
  试 m=9：估 ||A^19||_1 → m=9
  兜底 m=13：算 ||A^10||，取 eta=min(...), s=ceil(log2(eta/theta[13]))，并把 |A| 与幂迭代向量按 2^s 缩放
```

注意 \(n\ge400\) 这条分界：精确算 \(\|A^8\|,\|A^{10}\|\) 要先做两次 \(O(n^3)\) 的 `dgemm`，对大矩阵代价超过一次 1-范数估计，于是改用估计器。这正是 `expm` 文档里「\(n\ge400\) 起改用估计算法」的由来。

#### 4.3.3 源码精读

阈值与系数表：[\[`src/_matfuncs_expm.c`:L433-L442\]](src/_matfuncs_expm.c#L433-L442)。`theta[5]` 即上面的五个 \(\theta_m\)；`coeff[5]` 是把 \(\|A^{2m+1}\|\) 折算成「所需缩放次数」时用到的常数（来自 Higham 的误差分析）。

预计算幂次 \(A^2,A^4,A^6\) 与 \(d_4,d_6\)：[\[`src/_matfuncs_expm.c`:L462-L468\]](src/_matfuncs_expm.c#L462-L468)，三次 `dgemm` 得到 \(A^2,A^4,A^6\)，再算 `d4=||A^4||^(1/4)`、`d6=||A^6||^(1/6)`。

幂迭代估计 \(\|A^{2m+1}\|_1\)：以 m=3（估 \(\|A^7\|\)）为例，[\[`src/_matfuncs_expm.c`:L476-L490\]](src/_matfuncs_expm.c#L476-L490)。这里用 `absA`（即 \(|A|\)）反复乘向量，因为 \(\|A^k\|_1\le\||A|^k\|_1\) 且 \(|A|^k\) 的幂迭代收敛到 \(\|A^k\|_1\) 的下界——只做几次 `dgemv`（矩阵-向量）而非 `dgemm`（矩阵-矩阵），非常便宜。

\(n\ge400\) 时改用估计器：[\[`src/_matfuncs_expm.c`:L514-L523\]](src/_matfuncs_expm.c#L514-L523)（m=7 分支）与 [\[`src/_matfuncs_expm.c`:L571-L580\]](src/_matfuncs_expm.c#L571-L580)（m=13 分支），调用 `dnorm1est`；若估计器内部 `malloc` 失败（返回 \(\le -100\)），把 `m` 置为负值作为「内存不足」哨兵向上传播（与 4.2.3 的 `info=-100/-101` 呼应）。

兜底计算缩放次数 \(s\) 并缩放 \(|A|\)：[\[`src/_matfuncs_expm.c`:L582-L596\]](src/_matfuncs_expm.c#L582-L596)，`s=ceil(log2(eta4/theta[4]))`，再把 `absA` 与已幂迭代 19 次的向量按 \(2^{-s}\) 缩放，供后续 `pade_UV_calc` 直接使用。

1-范数估计器 `dnorm1est`：[\[`src/_matfuncs_expm.c`:L105-L134\]](src/_matfuncs_expm.c#L105-L134)。它通过 LAPACK 的 **反向通信**（reverse communication）例程 `dlacn2` 实现：`dlacn2` 每次告诉调用方「请把当前向量乘以 \(A\) 或 \(A^T\)」，调用方就用 `dgemv` 做这一乘法、再把结果喂回去，几轮迭代后 `dlacn2` 给出 \(\|A\|_1\) 的估计。这是 Higham–Tisseur (2000) 块 1-范数估计算法的标准实现。

精确 1-范数 `dnorm1`：[\[`src/_matfuncs_expm.c`:L31-L43\]](src/_matfuncs_expm.c#L31-L43)，就是朴素的「逐列绝对值求和取最大」，用于 \(n<400\) 时算 \(\|A^4\|,\|A^6\|\) 等。

> **关于 `_onenormest` 与 balancing 的澄清**（重要，易误解）：
> - **1-范数估计**：`expm` 文档与早期纯 Python 实现里的「onenormest」如今已**下沉到 C 后端内部**，即上面的 `dnorm1est`→`dlacn2`。Python 侧 `scipy.linalg` 不再为 `expm` 调用任何范数估计；同名工具 `scipy.sparse.linalg.onenormest` 仍存在，但只被同族的 `logm`/`fractional_matrix_power`（见 `[_matfuncs_inv_ssq.py:14](_matfuncs_inv_ssq.py#L14)`）使用。
> - **balancing（平衡）**：当前 `expm` **不做**平衡。平衡（`matrix_balance`，定义在 `[_basic.py:1704](_basic.py#L1704)`）是一个独立的公共函数，用相似变换均衡行列范数，主要被 Riccati 方程求解器 `solve_continuous_are`/`solve_discrete_are` 使用（见 `[_solvers.py](_solvers.py)`）。历史上旧版纯 Python `expm` 曾配合 balancing + onenormest 使用，但迁移到 C 后端后这条路径已被移除，取而代之的是更稳的「变阶 Padé + Fragment 2.1」。所以请不要在阅读 `expm` 时期待看到 balancing 步骤。

#### 4.3.4 代码实践

**目标**：观察「阶数随 \(\|A\|\) 增大而升高、并最终触发缩放」这一行为。由于 `(m, s)` 是 C 内部局部变量、不直接暴露，我们通过**间接证据**来验证：构造一个可对角化矩阵 \(A=V\mathrm{diag}(\lambda)V^{-1}\)，其 \(e^A=V\mathrm{diag}(e^\lambda)V^{-1}\) 可手算，比较不同尺度下 `expm` 的精度。

```python
# 示例代码
import numpy as np
from scipy.linalg import expm

# 一个可对角化的 4x4 矩阵：A = V diag(lam) V^-1
lam = np.array([0.01, 0.2, 1.5, 3.0])
V = np.array([[1., 0., 0., 0.],
              [1., 1., 0., 0.],
              [0., 1., 1., 0.],
              [0., 0., 1., 1.]])
A = V @ np.diag(lam) @ np.linalg.inv(V)

# 解析解
expA_true = V @ np.diag(np.exp(lam)) @ np.linalg.inv(V)

# 小尺度（前两特征值很小）—— Padé 低阶即可，精度极高
# 大尺度（把 A 放大 100 倍）—— 触发缩放-平方
for scale in [1.0, 100.0]:
    E = expm(scale * A)
    err = np.abs(E - V @ np.diag(np.exp(scale*lam)) @ np.linalg.inv(V)).max()
    print(f"scale={scale:6.1f}  最大误差={err:.2e}")
```

**操作步骤**：运行脚本。

**需要观察的现象**：两种尺度下误差都应接近机器精度（\(\sim10^{-15}\) 量级），即便 `scale=100` 时 \(\|A\|\) 远超 \(\theta_{13}=4.25\)。这说明缩放-平方成功把大范数情形也压回了 Padé 的安全区。

**预期结果**：两条误差都在 \(10^{-15}\sim10^{-13}\) 之间，体现「变阶 + 缩放」的自适应性。

> 待本地验证：具体误差数值会因 LAPACK 后端与平台略有差异，但都应在机器精度量级。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 \(\|A^4\|^{1/4},\|A^6\|^{1/6}\) 而不是直接用 \(\|A\|_1\) 来判断？

**答案**：理论上 \(\lim_{k\to\infty}\|A^k\|^{1/k}=\rho(A)\)（谱半径），取几次幂的几何均值比单次 \(\|A\|_1\) 更贴近 Padé 误差的真实依赖关系，从而避免「过度缩放」（浪费算力）或「欠缩放」（精度不足）。这是 Al-Mohy & Higham (2009) 相对老版算法的关键改进。

**练习 2**：`dnorm1est` 为什么用「反向通信」而不是直接传一个函数指针给 `dlacn2`？

**答案**：LAPACK 的 Fortran 例程 `?lacn2` 设计为「反向通信」接口——它不接收回调，而是返回一个 `kase` 告诉调用方「下一步请乘 \(A\) 还是 \(A^T\)」。这样 LAPACK 不必关心矩阵怎么存、用什么 BLAS，由 C 调用方用自己的 `dgemv` 完成乘法。这是 LAPACK 估计算法的通用编程模型。

---

### 4.4 Padé 有理式求值 pade_UV_calc

#### 4.4.1 概念说明

选定阶数 \(m\) 后，要计算 \([m/m]\) Padé 近似。把它的分子分母按奇偶重排为两个矩阵多项式 \(U\)（奇次部分）和 \(V\)（偶次部分）：

\[
U = A\bigl(b_1 I + b_3 A^2 + b_5 A^4 + \cdots\bigr),\qquad
V = b_0 I + b_2 A^2 + b_4 A^4 + \cdots
\]

其中系数 \(b_j=\dfrac{(2m-j)!\,m!}{(2m)!\,j!\,(m-j)!}\)，并归一化使最高次系数 \(b_m=1\)。于是

\[
e^A \approx q_m(A)^{-1}p_m(A)=(V-U)^{-1}(V+U)
\]

这里有个绝妙的代数技巧省掉一次矩阵求逆：

\[
(V-U)^{-1}(V+U)=(V-U)^{-1}\bigl((V-U)+2U\bigr)=I+2(V-U)^{-1}U
\]

也就是说，只要解一次线性方程组 \((V-U)X=U\)，结果就是 \(I+2X\)。而解线性方程组用 LU 分解（`dgetrf`）+ 三角求解（`dgetrs`），正是 u3-l1 讲过的 `lu_factor`+`lu_solve` 的底层例程。

> 系数验证（m=3）：\(b_j=(6-j)!\cdot 6/(720\cdot j!\cdot(3-j)!)\)，得 \(b_0=120,\ b_1=60,\ b_2=12,\ b_3=1\)。这正是源码里 `b[0]=120, b[1]=60, b[2]=12`（\(b_3=1\) 由「\(A\cdot A^2\)」项自然体现，代码注释写明 `b[m] = 1.0 for all m`）。

#### 4.4.2 核心流程

```
pade_UV_calc_d(Am, ipiv, n, m, info):
  按 m 分支，硬编码 b[] 系数
  用 Am[0..] 里已有的 A、A^2、A^4、A^6 与若干 dgemm/dcopy/dscal
  组装出 U（最终放 Am[3]）、V（最终放 Am[1]）
  —— m=13 用嵌套形式 U=A·(A^4·(...)+...)、V=A^4·(...)+... 减少矩阵乘法次数
  V := V - U                  (daxpy)
  对 V-U 做 dgetrf（LU 分解，ipiv 记主元）
  解 (V-U) X = U              (dgetrs)
  结果 := 2*X，再把对角 +1   ← 即 I + 2X
  结果留在 Am[0]
```

注意此时算出的是 \(e^{A/2^s}\)（因为 4.3 里已把缩放纳入选阶），后续由 4.2 的平方阶段还原成 \(e^A\)。

#### 4.4.3 源码精读

`pade_UV_calc_d` 入口与 `b[m]=1` 约定：[\[`src/_matfuncs_expm.c`:L1309-L1320\]](src/_matfuncs_expm.c#L1309-L1320)。

m=3 分支（最小阶，便于核对）：[\[`src/_matfuncs_expm.c`:L1321-L1337\]](src/_matfuncs_expm.c#L1321-L1337)。`U=A@A^2 + 60·A`、`V=12·A^2 + 120·I`，与 4.4.1 的系数完全对应。

m=13 分支（嵌套求值，最高阶）：[\[`src/_matfuncs_expm.c`:L1449-L1507\]](src/_matfuncs_expm.c#L1449-L1507)。注释把 U、V 都写成 \(K@(L@M+N)\) 的嵌套形式，目的是把朴素的 7 次矩阵乘降到 4 次（用 Horner-like 嵌套），这是高阶 Padé 高效实现的关键。

最终的「减、分解、解、加单位」四步：[\[`src/_matfuncs_expm.c`:L1509-L1522\]](src/_matfuncs_expm.c#L1509-L1522)。

- [\`L1511\`](src/_matfuncs_expm.c#L1511)：`daxpy(-1, U, V)` 实现 \(V\leftarrow V-U\)；
- [\`L1516\`](src/_matfuncs_expm.c#L1516)：`dgetrf` 对 \(V-U\) 做 LU 分解（带部分主元，存入 `ipiv`）；
- [\`L1517\`](src/_matfuncs_expm.c#L1517)：`dgetrs` 解 \((V-U)X=U\)（注意 `"T"` 转置标志与列主序存储配合）；
- [\`L1518-L1519\`](src/_matfuncs_expm.c#L1518-L1519)：`dscal(2,...)` 后在对角 `+1`，即落地 \(I+2X\)。

这四步与 u3-l1 的 `lu_factor`+`lu_solve` 是**同一对 LAPACK 例程**（`getrf`/`getrs`），只是这里在 C 内核里直接调用，不经 Python。

#### 4.4.4 代码实践

**目标**：用截断泰勒级数（标量定义的直接推广）手算一个小矩阵的 \(e^A\)，与 `expm` 对比，体会「Padé 比朴素泰勒更准」。

```python
# 示例代码
import numpy as np
from scipy.linalg import expm

# 一个小范数矩阵，保证泰勒级数也收敛得动
A = np.array([[0.0, 1.0],
              [-0.5, -0.3]])

# 泰勒级数手算：e^A ≈ I + A + A^2/2! + A^3/3! + ... + A^K/K!
def expm_taylor(A, K=40):
    n = A.shape[0]
    S = np.eye(n)
    term = np.eye(n)
    for k in range(1, K+1):
        term = term @ A / k
        S = S + term
    return S

E_scipy = expm(A)
E_taylor = expm_taylor(A, K=60)

print("scipy.expm:\n", E_scipy)
print("taylor(60):\n", E_taylor)
print("|scipy - taylor|_max =", np.abs(E_scipy - E_taylor).max())

# 验证指数律：expm(A) @ expm(-A) == I
print("|expm(A)@expm(-A) - I|_max =",
      np.abs(expm(A) @ expm(-A) - np.eye(2)).max())
```

**操作步骤**：运行脚本；可尝试把 `A` 改成更大范数（如 `5*A`）再看泰勒是否还能跟上。

**需要观察的现象**：小范数下泰勒（取够多项）与 `expm` 高度一致；指数律 `expm(A)@expm(-A)≈I` 成立。若把 `A` 放大到 `5*A`，泰勒级数会因中间项巨大抵消而明显失精，但 `expm` 仍保持机器精度——这就是缩放-平方 + Padé 的价值。

**预期结果**：小范数下两者误差 \(\sim10^{-16}\)；放大范数后 `expm` 仍精确而朴素泰勒退化。

> 待本地验证：放大范数后泰勒退化的具体位数取决于阶数 K 与平台浮点行为。

#### 4.4.5 小练习与答案

**练习 1**：为什么用 \(I+2(V-U)^{-1}U\) 而不直接算 \((V-U)^{-1}(V+U)\)？

**答案**：少算一次矩阵乘（不必显式构造 \(V+U\) 再乘以逆），且把「求逆」严格转化为「解线性方程组」，后者用 LU 分解稳定高效。代数上二者完全等价。

**练习 2**：m=13 分支为什么把 U、V 写成嵌套形式 \(A\cdot(A^4\cdot(\cdots)+\cdots)\)？

**答案**：直接按多项式硬乘需要 7 次 `dgemm`；写成关于 \(A^2\)、\(A^4\) 的 Horner 嵌套后只需 4 次 `dgemm`（注释 [\`L1464-L1472\`](src/_matfuncs_expm.c#L1464-L1472) 明确给出该结构）。对 \(O(n^3)\) 的矩阵乘法，省下 3 次乘法在大矩阵上收益显著。

---

## 5. 综合实践

把四个最小模块串起来，完成一次「黑箱探测 + 源码对照」的小任务：

1. **构造被测矩阵**：取一个 \(6\times6\) 的实矩阵 \(A\)（例如 `np.random.default_rng(0).standard_normal((6,6))`），再构造它的一个三角版本 `np.triu(A)` 与一个对角版本 `np.diag(np.diag(A))`。
2. **预测并验证路径**：对这三个矩阵分别调用 `expm`，根据本讲所学，预测它们各自会走哪条内核路径（对角快路径 / 三角 Fragment 2.1 / 一般稠密缩放-平方）。用「结果正确性」做验证：
   - 对角版：与 `np.diag(np.exp(np.diag(A)))` 对比；
   - 三角版：检查 `expm(np.triu(A))` 是否仍是上三角（\(e^{\text{上三角}}\) 仍是上三角）；
   - 一般版：用 `expm(A)@expm(-A)≈I` 验证。
3. **精度标定**：选一个可对角化的小矩阵 \(A=V\mathrm{diag}(\lambda)V^{-1}\)，比较 `expm(A)` 与解析解 \(V\mathrm{diag}(e^\lambda)V^{-1}\) 的最大误差，确认在机器精度量级。
4. **源码对照**：把每一步现象回扣到具体源码行——对角路径对应 [\`bandwidth\` 快路径](src/_matfuncs_expm.c#L2400-L2406)、缩放-平方对应 [\`pick_pade_structure_d\`](src/_matfuncs_expm.c#L417-L596) 与 [\`pade_UV_calc_d\`](src/_matfuncs_expm.c#L1309-L1523)、三角特化对应 [Fragment 2.1](src/_matfuncs_expm.c#L2448-L2483)。

> 待本地验证：第 2 步中三角版的「上三角性」与各精度数值需本地运行确认；但「对角路径、上三角保持性、指数律」都是数学保证的结论。

## 6. 本讲小结

- `expm` 的 Python 入口（[\`_matfuncs.py:210\`](_matfuncs.py#L210)）是纯校验薄壳，数值计算全部在 C 扩展 `_internal_matfuncs` 里。
- C 后端采用 **缩放-平方 + 变阶 Padé**（Al-Mohy & Higham 2009）：`pick_pade_structure_*` 在 \(m\in\{3,5,7,9,13\}\) 间选阶并定缩放次数 \(s\)，`pade_UV_calc_*` 求值，最后平方 \(s\) 次还原。
- Padé 求值用 \(e^A\approx I+2(V-U)^{-1}U\) 的技巧，把「矩阵求逆」化为一次 `getrf`+`getrs`（与 u3-l1 LU 分解同源）。
- 选阶依赖 1-范数与「幂的范数」：\(n<400\) 精确算，\(n\ge400\) 用 LAPACK `dlacn2` 估计（[\`dnorm1est\`](src/_matfuncs_expm.c#L105-L134)）。
- **重要澄清**：当前 `expm` 不做 balancing；`scipy.sparse.linalg.onenormest` 也未被 `expm` 调用，1-范数估计已内嵌于 C 后端。
- 内核对对角阵走逐元素 `exp` 快路径、对三角阵走 Fragment 2.1 精度强化路径，体现「抓住结构、降复杂度」的设计思想。

## 7. 下一步学习建议

- **u5-l2 矩阵对数 logm、funm 与分数幂**：`logm` 是 `expm` 的反函数，依赖 `scipy.sparse.linalg.onenormest` 与 Schur 分解，正好承接本讲对 1-范数估计的讨论，并复用 u3-l5 的 Schur 框架。
- **u5-l3 矩阵平方根 sqrtm**：与本讲共用同一个 `_internal_matfuncs` 扩展（`_matfuncs_sqrtm.c`），可对比「同为矩阵函数、却走 Schur 分块路径」的另一种实现风格。
- **u8-l3 矩阵函数 C 后端**：从构建系统（[\`meson.build:258-268\`](meson.build#L258-L268)）视角统看 `_matfuncsmodule.c` 如何把 expm 与 sqrtm 两个内核打包成一个扩展模块。
- 延伸阅读：Al-Mohy & Higham (2009) 原文（`expm` 文档里的参考文献 [1]），对照阅读可把本讲的 \(\theta_m\) 表与 Fragment 2.1 看得更透。

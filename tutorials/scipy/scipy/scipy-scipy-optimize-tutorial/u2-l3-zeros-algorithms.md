# 一维求根算法实现：bisect / newton / brentq / ridder / toms748

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `scipy.optimize` 里 6 个一维求根算法各自的**迭代策略**与**收敛阶**：`bisect`（二分）、`brentq`/`brenth`（Brent 系列）、`ridder`（指数拟合）、`newton`（牛顿/割线/Halley）、`toms748`（Algorithm 748）。
- 区分**括号法**（保证收敛、只能实数）和**点法**（不保证收敛、可用复数）两类，并知道它们分别落在 `_zeros_py.py` 的哪些函数里。
- 看懂「C 后端 + Python 包装」的三件套：`_zeros_py.py` 做参数校验与结果封装、`_zerosmodule.c` 做跨语言胶水、`Zeros/*.c` 才是括号法四兄弟的真正算法体。
- 解释 `newton` 的向量化实现 `_array_newton` 如何用一个 `failures` 掩码数组**同时**推进上百个初值。
- 理解 `RootResults` 如何把 `(root, nfev, nit, flag)` 打包成一个既支持 `res.root` 又支持 `res['root']` 的对象。

> 本讲承接 [u2-l2](u2-l2-root-scalar-interface.md) 讲过的「`root_scalar` 只是调度器」这一结论。上一讲我们只看了**接口层**如何选方法；本讲往下钻一层，进入 `_zeros_py.py` 与 `Zeros/*.c`，看这些方法**到底怎么迭代**。`root_scalar` 最终都是把请求派发到本讲的这些函数（通过 `getattr(optzeros, method)`），所以本讲是上一讲的「算法落地」。

## 2. 前置知识

### 2.1 复习：两类求根策略与收敛阶

上一讲 [u2-l2](u2-l2-root-scalar-interface.md) 已经建立了两大阵营的图景，这里只做最精炼的复述：

| 阵营 | 输入材料 | 代表方法 | 是否保证收敛 | 收敛阶（典型） |
|---|---|---|---|---|
| 括号法 | 异号区间 \([a,b]\) | `bisect` / `brentq` / `brenth` / `ridder` / `toms748` | **是**（介值定理） | 1 → 约 2 → 最高约 4.6 |
| 点法 | 初值 \(x_0\)（可选导数） | `newton` / `secant` / `halley` | 否 | 1.62 / 2 / 3 |

设第 \(n\) 步误差 \(e_n=\lvert x_n-x^\*\rvert\)，若 \(e_{n+1}\approx C\,e_n^{\,p}\)，则称该方法**收敛阶**为 \(p\)。\(p\) 越大，逼近根的速度越快，但「快」往往以「不稳」或「多花函数求值」为代价。

### 2.2 括号与介值定理：为什么括号法「保证收敛」

如果你能给一个区间 \([a,b]\)，使 \(f(a)\) 与 \(f(b)\) **异号**（\(f(a)\cdot f(b)<0\)），且 \(f\) 连续，那么由**介值定理**，\([a,b]\) 内至少有一个根。所有括号法都围绕一个不变量工作：

> **不变量**：当前区间 \([a,b]\) 两端函数值始终异号，因此根始终被「夹」在区间里。

每次迭代都试图**把区间缩小**，同时**保持异号**。只要区间长度趋于 0，根就被逼出来了——这就是括号法「保证收敛」的数学根源。代价是：你**必须先找到一个变号区间**，且只能用于实数轴。

### 2.3 这一层在 SciPy 里是怎么组织的

来自 [u1-l2](u1-l2-directory-build-and-backends.md) 的心智模型在这里特别重要：

```
scipy/optimize/
├── _zeros_py.py        ← Python 包装层：参数校验 + 结果封装
│                          newton / _array_newton / toms748 是纯 Python
│                          bisect/brentq/brenth/ridder 在这里调 _zeros._xxx
├── _zerosmodule.c      ← C 胶水层：把 Python 函数包装成 C 回调
├── Zeros/              ← 真正的 C 算法体
│   ├── zeros.h         ← 共享的状态码与函数原型
│   ├── bisect.c        ← 二分法
│   ├── brentq.c        ← Brent（逆二次外推）
│   ├── brenth.c        ← Brent（双曲外推）
│   └── ridder.c        ← Ridder 指数拟合
```

关键事实（本讲会反复用到）：

- `newton`、`_array_newton`、`toms748` 及其 `TOMS748Solver` 是**纯 Python**，算法体就在 `_zeros_py.py` 里，你能直接逐行读。
- `bisect`、`brentq`、`brenth`、`ridder` 在 `_zeros_py.py` 里只是**薄薄的包装**，真正迭代在 `Zeros/*.c` 的 C 代码里，Python 通过 `_zeros._brentq` 这样的名字跨语言调用。
- C 与 Python 必须共享同一套**状态码**：`CONVERGED=0`、`SIGNERR=-1`、`CONVERR=-2`、`INPROGRESS=1`。`zeros.h` 顶部和 `_zeros_py.py` 顶部各定义一份，注释明确要求两边一致。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [_zeros_py.py](_zeros_py.py) | 求根算法的 Python 实现/包装 + `RootResults` | 全篇核心；4.1/4.5/4.6 |
| [_zerosmodule.c](_zerosmodule.c) | C 扩展胶水：解析参数、回调 Python、打包结果 | 4.1、4.2（理解调用链） |
| [Zeros/zeros.h](Zeros/zeros.h) | 共享状态码与 4 个 C 求解器的函数原型 | 4.1（状态码）、4.2/4.3/4.4 |
| [Zeros/bisect.c](Zeros/bisect.c) | 二分法算法体 | 4.2 |
| [Zeros/brentq.c](Zeros/brentq.c) | Brent 法（逆二次外推）算法体 | 4.3 |
| [Zeros/brenth.c](Zeros/brenth.c) | Brent 法（双曲外推）算法体 | 4.3 |
| [Zeros/ridder.c](Zeros/ridder.c) | Ridder 法算法体 | 4.4 |

> 一个贯穿全篇的「不变接口」：所有括号法都返回同一个停止判据——
> \[
> \lvert x - x_0\rvert \le \text{xtol} + \text{rtol}\cdot\lvert x_0\rvert
> \]
> 也就是 `np.isclose(x, x0, atol=xtol, rtol=rtol)`。默认 `xtol=2e-12`、`rtol=4*eps`、`maxiter=100`，由 [_zeros_py.py:9-11](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L9-L11) 的 `_iter/_xtol/_rtol` 统一定义。

---

## 4. 核心概念与源码讲解

### 4.1 结果封装基础设施：RootResults、状态码与 NaN 防护

#### 4.1.1 概念说明

无论哪种求根算法，最终都要回答读者三件事：**根是多少？算了多少次函数？成功了吗？** `scipy.optimize` 用一个统一的容器 `RootResults` 来装这些信息，并用一套**整数状态码**在 C 与 Python 之间传递「为什么停下来了」。这一小节不谈任何具体算法，只把这个「公共底座」讲清楚——因为后面 6 个算法全部复用它。

#### 4.1.2 核心流程

1. 底层求解器（C 或 Python）算完后，吐出一个**四元组** `(root, function_calls, iterations, flag)`。
2. `flag` 是整数：`0`=收敛、`-1`=符号错误、`-2`=收敛失败、`1`=进行中。
3. Python 侧用 `_results_select`（纯 Python 求解器）或 `results_c`（C 求解器）把它包成 `RootResults`。
4. `RootResults` 把整数 `flag` 翻译成人话字符串（`"converged"` 等），并据此设 `converged` 布尔。

#### 4.1.3 源码精读

**状态码（Python 侧）** —— 与 C 侧必须一致：

[_zeros_py.py:17-32](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L17-L32) 定义了 `_ECONVERGED=0`、`_ESIGNERR=-1`、`_ECONVERR=-2`、`_EINPROGRESS=1`，以及一张把整数映射成中文/英文字符串的 `flag_map`。注意第 16 行的注释 `# Must agree with CONVERGED, SIGNERR, CONVERR, ...  in zeros.h`——这是 C/Python 两套常量必须同步的硬约束。

**C 侧的同一套常量** —— [Zeros/zeros.h:16-21](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/Zeros/zeros.h#L16-L21)：

```c
/* Must agree with _ECONVERGED, _ESIGNERR, _ECONVERR  in zeros.py */
#define CONVERGED 0
#define SIGNERR -1
#define CONVERR -2
#define INPROGRESS 1
```

**RootResults 类** —— 它继承自 `OptimizeResult`（来自 [u1-l1](u1-l1-overview-and-common-objects.md) 讲过的公共对象），所以既能 `res.root` 也能 `res['root']`：

[_zeros_py.py:35-77](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L35-L77) —— 构造函数接收 `(root, iterations, function_calls, flag, method)`，核心两行是 `self.converged = flag == _ECONVERGED`（只有 `flag==0` 才算收敛）和把 `flag` 经 `flag_map` 翻成字符串。

**两条封装路径**（注意区别，后面会用到）：

- C 求解器用 [results_c](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L80-L89)：当 `full_output=False` 时**直接返回标量 `r`**（C 已经算好的根）。
- 纯 Python 求解器（`newton`/`toms748`）用 [_results_select](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L92-L101)：始终先解包四元组再决定返回根还是 `(根, RootResults)`。

**NaN 防护 `_wrap_nan_raise`** —— [_zeros_py.py:104-119](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L104-L119)。所有括号法的 Python 包装在调 C 之前都会先用它包一层目标函数：一旦函数值是 `NaN`，就抛 `ValueError`（带上下文 `err._x`、`err._function_calls`）。这避免了「NaN 把区间逻辑搞乱却默默返回错误根」的隐患。

#### 4.1.4 代码实践

**目标**：直观看到 `RootResults` 的字段结构与两种返回形态。

操作步骤（示例代码）：

```python
# 示例代码
from scipy.optimize import brentq, toms748

f = lambda x: x**3 - 1   # 根在 x=1

# full_output=False：直接拿到标量根
root = brentq(f, 0, 2)
print("root =", root)

# full_output=True：拿到 (root, RootResults)
root, r = toms748(f, 0, 2, full_output=True)
print(r)                 # 打印整个对象
print(type(r))           # <class 'scipy.optimize._zeros_py.RootResults'>
print(r.root, r.iterations, r.function_calls, r.converged, r.flag)
print(r['function_calls'])  # 也能用 dict 下标访问
```

**需要观察的现象**：

- `full_output=False` 时返回的就是一个 `float`；`True` 时是 `(float, RootResults)`。
- `r.converged` 是布尔，`r.flag` 是字符串 `"converged"`，二者来自同一个整数 `flag`。
- `r['function_calls']` 和 `r.function_calls` 返回同一个值——这是 `OptimizeResult` 子类带来的能力。

**预期结果**：根约为 `1.0`；`toms748` 的 `iterations` 约为 5、`function_calls` 约为 11（与 `toms748` 文档示例一致，见 [_zeros_py.py:1456-1461](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L1456-L1461)）。brentq 的具体 `nfev`/`nit` 待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果 `f(a)` 与 `f(b)` 同号，括号法会怎样？`flag` 是几？

> **答案**：C 侧返回 `SIGNERR=-1`，Python 包成 `RootResults` 后 `converged=False`、`flag='sign error'`。若 `disp=True`（默认），`_zerosmodule.c` 会直接抛 `ValueError("f(a) and f(b) must have different signs")`。

**练习 2**：为什么状态码要在 `zeros.h` 和 `_zeros_py.py` 各定义一份？

> **答案**：C 编译扩展和 Python 解释器是两个独立的世界，无法共享常量。靠「人工保持一致 + 注释互指」来对齐；任何一边改了数值，另一边必须同步，否则结果对象的 `converged` 判定会错乱。

---

### 4.2 bisect：二分法与括号法的稳健底线

#### 4.2.1 概念说明

二分法是最古老的求根法，也是最**稳健**的：只要区间两端异号，它每一步都机械地把区间**对半切**，保留仍含根的那一半。它的收敛阶是 1（线性），是所有括号法里**最慢但最稳**的——常被当作其他方法的「保底」策略（Brent 系列在插值失败时就退回二分）。理解它就理解了所有括号法的不变量。

#### 4.2.2 核心流程

```
输入：区间 [a,b]，f(a)·f(b) < 0
令 dm = b - a                 # 区间半宽
重复（最多 maxiter 次）：
    dm ← dm / 2               # 对半切
    xm ← a + dm               # 取中点
    fm ← f(xm)
    若 fm 与 fa 同号：a ← xm   # 根在右半，把 a 推到中点
    （否则 b 侧不动，根在左半）
    若 fm==0 或 |dm| < xtol + rtol·|xm|：返回 xm
```

每步恰好 1 次函数求值，区间宽度乘以 0.5。要把初始宽度 \(w\) 压到容差 \(\varepsilon\)，约需

\[
n \approx \log_2(w/\varepsilon)
\]

步。例如把 \([0,2]\) 压到 \(10^{-12}\)，约需 \(\log_2(2/10^{-12})\approx 51\) 步。

#### 4.2.3 源码精读

`bisect` 的 Python 包装非常薄，[Zeros/bisect.c](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/Zeros/bisect.c) 才是算法体：

[Zeros/bisect.c:6-47](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/Zeros/bisect.c#L6-L47) —— 关键片段：

```c
fa = (*f)(xa, func_data_param);          // 端点 a 的函数值
fb = (*f)(xb, func_data_param);
if (signbit(fa)==signbit(fb)) {          // 同号 → 符号错误
    solver_stats->error_num = SIGNERR; return 0.;
}
dm = xb - xa;                             // 区间半宽
for (i=0; i<iter; i++) {
    dm *= .5;                             // 对半切
    xm = xa + dm;                         // 中点
    fm = (*f)(xm, func_data_param);
    if (signbit(fm)==signbit(fa)) { xa = xm; }   // 保留含根的一半
    if (fm == 0 || fabs(dm) < xtol + rtol*fabs(xm)) {
        solver_stats->error_num = CONVERGED; return xm;
    }
}
```

注意几个细节：

- 第 14-16 行先算 `fa`、`fb`，`funcalls` 初始化为 2；这是所有括号法的共同起点。
- 第 25 行用 `signbit`（而不是 `fa*fb<0`）判同号，避免 `fa*fb` 下溢为 0 的边界 bug。
- 收敛判据 `fabs(dm) < xtol + rtol*fabs(xm)` 正是第 3 节说的统一公式（这里 `dm` 是区间半宽，对应误差上界）。

**跨语言调用链**（以 `bisect` 为例，其余三个括号法完全相同）：

- Python 包装 [_zeros_py.py:509-608](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L509-L608)：校验 `xtol/rtol/maxiter` → `_wrap_nan_raise(f)` 包一层 → 调 `_zeros._bisect(...)`。
- 胶水 [_zerosmodule.c:170-174](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zerosmodule.c#L170-L174)：`_bisect` → `call_solver(bisect, ...)`。
- [call_solver](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zerosmodule.c#L71-L135)：用 `setjmp/longjmp` 兜底错误，调真正的 C 函数 `bisect`，把结果打包成 `(zero, funcalls, iterations, flag)`。
- C 函数内部通过函数指针 `(*f)(xm, func_data_param)` **反向回调** Python 函数（[scipy_zeros_functions_func](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zerosmodule.c#L26-L64)）——C 不直接认识 Python，每次需要 `f(x)` 时就通过这个回调请 Python 算一次。

#### 4.2.4 代码实践

**目标**：用 `full_output` 直接看到二分法的「慢而稳」。

```python
# 示例代码
from scipy.optimize import bisect

f = lambda x: x**2 - 2          # 根 = sqrt(2)
root, r = bisect(f, 0, 2, full_output=True)
print(f"root={root}, nit={r.iterations}, nfev={r.function_calls}")
print(f"理论步数 ≈ {__import__('math').log2(2/2e-12):.1f}")
```

**需要观察的现象**：`nit` 与 `nfev` 应大致相等（每步 1 次求值，加上初始 2 次）；`nit` 接近理论步数（约 50）。对比下一节的 `brentq`，你会看到 `brentq` 用少得多的步数达到同样精度。

**预期结果**：`nit` 约为 50，`nfev` 约为 52。精确数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `bisect` 不需要像 Brent 那样维护「上一步」信息？

> **答案**：二分法每步只用当前区间 \([a,b]\)，对半切即可；它不依赖历史点的函数值做插值。Brent 需要三个点做逆二次插值，所以必须记住 `xpre/xblk` 等历史。

**练习 2**：把 `bisect` 的 `xtol` 调到 `5e-324`（最小次正规数）会发生什么？

> **答案**：会一直二分到区间宽度逼近浮点极限，得到「机器精度级别」的根，但 `nit` 会显著增加（约 50 多步）。这正是各方法文档里反复提醒的「默认 `xtol=2e-12` 已足够，更小只是多花函数求值」。

---

### 4.3 brentq 与 brenth：插值外推 + 二分保护

#### 4.3.1 概念说明

二分法稳但慢。Brent 系列的思想是：**在能插值的时候就插值加速，插值不可靠的时候就退回二分保命**。这样既保住了括号法「保证收敛」的优点，又能拿到接近二次的收敛速度。

`scipy.optimize` 提供两个 Brent 变体，差别只在「插值」这一步用哪种公式：

| 函数 | 外推方式 | 收敛阶（最坏） |
|---|---|---|
| `brentq` | **逆二次插值**（IQI，用 3 个点拟合一条抛物线） | \(\varphi\approx 1.618\) |
| `brenth` | **双曲外推**（Bus & Dekker 的 Algorithm M） | \(\varphi\approx 1.618\) |

> 「逆」插值的意思：普通插值是「给定 \(x\)，拟合 \(f(x)\)，再解 \(f(x)=0\)」；**逆**插值是把 \(f\) 值当自变量、\(x\) 当因变量，直接拟合 \(x = IP(f)\)，于是根就是 \(x = IP(0)\)，免去解方程。这对求根特别自然。

#### 4.3.2 核心流程（两者共享同一套循环骨架）

```
维护三个点：xcur（当前最佳）、xpre（上一步）、xblk（另一端点）
每步：
  1. 维护不变量 |fblk| < |fcur|（让 xcur 始终是最小 |f| 的点）
  2. 计算容差 delta = (xtol + rtol·|xcur|)/2，二分步 sbis = (xblk-xcur)/2
  3. 若 |sbis| < delta：收敛，返回 xcur
  4. 尝试插值步 stry：
       - 只有「上一步足够大」且「|fcur|<|fpre|」（在逼近）才尝试
       - 若 xpre==xblk：线性插值（退化为割线）
       - 否则：brentq 用逆二次 / brenth 用双曲
  5. 若 stry 太大或不可靠（2|stry| ≥ min(|spre|, 3|sbis|-delta)）：放弃，stry=sbis
  6. xcur += stry（或至少 ±delta），算 fcur
```

第 5 步的判据 `2*fabs(stry) < MIN(fabs(spre), 3*fabs(sbis) - delta)` 是「**接受插值步**」的守门员：插值步必须明显比二分步更短、且比上一步短，否则宁可二分。这是 Brent 法稳健性的关键。

#### 4.3.3 源码精读

[Zeros/brentq.c:36-130](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/Zeros/brentq.c#L36-L130)。文件顶部 [注释](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/Zeros/brentq.c#L8-L34) 解释了循环顶部的不变量。关键分支：

```c
if (fabs(spre) > delta && fabs(fcur) < fabs(fpre)) {
    if (xpre == xblk) {
        /* 线性插值（割线）：只有两个点可用 */
        stry = -fcur*(xcur - xpre)/(fcur - fpre);
    }
    else {
        /* 逆二次插值：三个点 (xcur,xpre,xblk) */
        dpre = (fpre - fcur)/(xpre - xcur);
        dblk = (fblk - fcur)/(xblk - xcur);
        stry = -fcur*(fblk*dblk - fpre*dpre)
                /(dblk*dpre*(fblk - fpre));
    }
    if (2*fabs(stry) < MIN(fabs(spre), 3*fabs(sbis) - delta)) {
        spre = scur; scur = stry;      /* 接受插值步 */
    } else {
        spre = sbis; scur = sbis;      /* 放弃，退回二分 */
    }
}
else { spre = sbis; scur = sbis; }     /* 不在逼近，直接二分 */
```

**brenth 的唯一差别**（[Zeros/brenth.c:95-100](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/Zeros/brenth.c#L95-L100)）：同样的 `else` 分支里，把逆二次换成了**双曲外推**：

```c
stry = -fcur*(fblk - fpre)/(fblk*dpre - fpre*dblk);
```

两份 C 文件除了这一行，几乎逐行相同。`brentq` 是社区更推荐、测试更充分的默认选择；`brenth`（Chuck Harris 实现）作为对照存在。

#### 4.3.4 代码实践

**目标**：观察 Brent 系列相对二分法的「步数骤减」。

```python
# 示例代码
from scipy.optimize import bisect, brentq, brenth

f = lambda x: x**2 - 2
for name, fn in [("bisect", bisect), ("brentq", brentq), ("brenth", brenth)]:
    root, r = fn(f, 0, 2, full_output=True)
    print(f"{name:8s} root={root:.15f} nit={r.iterations:3d} nfev={r.function_calls:3d}")
```

**需要观察的现象**：`brentq`/`brenth` 的 `nit` 远小于 `bisect`（通常个位数 vs 五十左右），且三者根几乎一致。

**预期结果**：`bisect` 约 50 步；`brentq`/`brenth` 约 7~9 步。`brentq` 与 `brenth` 的 `nit` 接近。精确数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`brentq` 什么时候退化为割线（线性插值）？

> **答案**：当 `xpre == xblk` 时，即上一轮发生了「区间翻转」，只剩两个有效点，无法做二次插值，于是用两点线性插值（即割线步）。

**练习 2**：为什么即便插值步看起来很好，Brent 仍可能选择二分？

> **答案**：守门员判据 `2|stry| < min(|spre|, 3|sbis|-delta)` 要求插值步既比上一步短、又比二分步的 3 倍还短。若不满足，说明插值不可靠（可能跳出区间或收敛太慢），此时宁可二分以维持「保证收敛」。这正是「快」与「稳」的平衡机制。

---

### 4.4 ridder：单步即可指数拟合收敛

#### 4.4.1 概念说明

Ridders（1979）提出了一个**极其简洁**的公式：在二分中点之外，利用指数假设 \(f(x)\) 在区间内形如 \(e^{\lambda x}\) 的形态，额外算一个「修正点」。这个修正点往往能把精度提高好几个数量级。Ridder 法保持括号（保证收敛），同时每步 2 次函数求值，收敛阶约为 2，常比 Brent 略快或相当。

#### 4.4.2 核心流程

```
输入：[a,b]，f(a)·f(b)<0
每步：
  xm = (a+b)/2                 # 中点
  fm = f(xm)
  # Ridders 核心公式（指数拟合修正）：
  ratio = fm / fa
  dn = dm * ratio / sqrt(ratio² - fb/fa)
  xn = xm + sign(dn) * min(|dn|, |dm| - 0.5*tol)   # 修正点，并夹在区间内
  fn = f(xn)
  # 用 (xn, fn) 重新括号（与 fn 异号的那一端被替换）
  根据符号关系更新 [a,b]/[fa,fb]
  若 fn==0 或 |b-a| < tol：返回 xn
```

核心是这条**指数拟合公式**。其直觉来源：假设在区间内 \(f(x)\approx (f(a)f(b))^{1/2}\,e^{\lambda(x-x_m)}\) 形态，则把 \(f(x_m)\) 也纳入后可解出一个对根的二次精度估计 `xn`。代码里用 `ratio = fm/fa` 归一化以**避免下溢**（见源码注释）。

#### 4.4.3 源码精读

[Zeros/ridder.c:17-89](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/Zeros/ridder.c#L17-L89)。核心片段：

```c
dm = 0.5*(xb - xa);
xm = xa + dm;
fm = (*f)(xm, func_data_param);
if (fm == 0.0) { ... return xm; }      // 中点恰好是根

/* 用 fa 归一化，避免 fm*fm 下溢（见注释） */
double ratio = fm / fa;
dn = dm * ratio / sqrt(ratio * ratio - fb / fa);

xn = xm + SIGN(dn) * MIN(fabs(dn), fabs(dm) - .5*tol);   // 修正点
fn = (*f)(xn, func_data_param);

if (signbit(fn) != signbit(fm))      { xa=xn; fa=fn; xb=xm; fb=fm; }   // 重新括号
else if (signbit(fn) != signbit(fa)) { xb=xn; fb=fn; }
else                                 { xa=xn; fa=fn; }
```

注意它与 Brent 的结构差异：Ridder **没有**历史点链，每步只用当前的 `a/b/xm/xn` 四个点，逻辑非常干净。代价是每步**固定 2 次函数求值**（`fm` 和 `fn`），所以 `nfev ≈ 2·nit`。

#### 4.4.4 代码实践

**目标**：比较 Ridder 与 Brent 的步数与求值数。

```python
# 示例代码
from scipy.optimize import brentq, ridder

f = lambda x: x**2 - 2
for name, fn in [("brentq", brentq), ("ridder", ridder)]:
    root, r = fn(f, 0, 2, full_output=True)
    print(f"{name:8s} nit={r.iterations:3d} nfev={r.function_calls:3d} root={root:.15f}")
```

**需要观察的现象**：Ridder 的 `nit` 通常比 `brentq` 小，但 `nfev` 约为 `2·nit`，所以总求值数两者接近。Ridder 收敛判据用的是**区间宽度** `|b-a| < tol`，而 Brent 用的是 `|sbis| < delta`（半宽）。

**预期结果**：两者根一致；Ridder 的 `nit` 个位数、`nfev ≈ 2·nit`。精确数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Ridder 要用 `ratio = fm/fa` 归一化而不是直接写 `dn = dm * fm / sqrt(fm*fm - fa*fb)`？

> **答案**：见 [Zeros/ridder.c:62-66](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/Zeros/ridder.c#L62-L66) 注释——直接用 `fm*fm` 在 `fm` 很小时会**下溢为 0**，导致 `sqrt(负数或0)` 出错。归一化为 `ratio` 后，`ratio*ratio - fb/fa` 更数值稳定。

**练习 2**：Ridder 是括号法吗？它保证收敛吗？

> **答案**：是。每步都用 `xn` 替换与 `fn` 同号的端点，始终保持区间两端异号，因此由介值定理保证收敛；这一点和 `brentq`/`bisect` 一致，与点法 `newton` 不同。

---

### 4.5 newton 与 _array_newton：点法与向量化

#### 4.5.1 概念说明

`newton` 是本讲里唯一的**点法**入口（`secant`、`halley` 实际都映射到它，见 [u2-l2](u2-l2-root-scalar-interface.md)）。它**不要求变号区间**，只需一个初值 \(x_0\)，根据你提供的导数信息自动切换三种公式：

| 你提供什么 | 用什么方法 | 更新公式 | 收敛阶 |
|---|---|---|---|
| `fprime`（一阶导） | Newton-Raphson | \(p = p_0 - f(p_0)/f'(p_0)\) | 2（平方） |
| 都不提供 | Secant（割线） | 用两点做差分代替导数 | \(\varphi\approx 1.62\) |
| `fprime` + `fprime2`（二阶导） | Halley | \(p = p_0 - \dfrac{f/f'}{1-\frac{f\,f''}{2(f')^2}}\) | 3（立方） |

与括号法不同，点法**不保证收敛**（初值不好可能发散），但**可用于复数**，且收敛极快。

> 上一讲提到 `newton` 在 `x0` 是数组时会**向量化**——这正是 `_array_newton`。当你有成百上千个相似的求根问题（例如对一组参数 \(\theta\) 各求一个根），逐个 `for` 循环调用 `newton` 很慢；`_array_newton` 让你**一次性把整个数组喂进去**，在 NumPy 里并行推进。

#### 4.5.2 核心流程

**标量 newton（Newton-Raphson 分支）**：

```
p0 = x0
重复（最多 maxiter 次）：
    fval = f(p0)
    若 fval == 0：返回（恰好是根）
    fder = fprime(p0)
    若 fder == 0：报「导数为零」
    newton_step = fval / fder
    若提供了 fprime2 且 |adj|<1：用 Halley 修正 newton_step
    p = p0 - newton_step
    若 isclose(p, p0)：返回 p（步长足够小）
    p0 = p
```

**割线分支**（无 `fprime`）：用两个点 `p0, p1`，用差分 `(f(p1)-f(p0))/(p1-p0)` 代替导数。

**向量化 `_array_newton`**：把上面循环里的标量换成**数组**，并用两个布尔掩码管理「哪些元素还没收敛」：

- `failures`：`|dp| >= tol` 的元素（还没收敛）。
- `nz_der`：导数非零的元素（可以更新）。

每步只更新 `nz_der` 为真、且仍 `failures` 的位置；当一个元素收敛后它就「冻结」不再迭代，但整个数组共享同一个 `maxiter` 循环。

#### 4.5.3 源码精读

[newton 的分流](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L300-L307)：进入函数后第一件事就是判断 `x0` 是否数组：

```python
if np.size(x0) > 1:
    return _array_newton(func, x0, fprime, args, tol, maxiter, fprime2,
                         full_output)
```

[Newton-Raphson 主循环](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L316-L358)：

```python
for itr in range(maxiter):
    fval = func(p0, *args); funcalls += 1
    if fval == 0: return _results_select(full_output, (p0, funcalls, itr, _ECONVERGED), method)
    fder = fprime(p0, *args); funcalls += 1
    ...
    newton_step = fval / fder
    if fprime2:                              # Halley 修正
        adj = newton_step * fder2 / fder / 2
        if np.abs(adj) < 1:                  # 仅当分母接近 1 才用
            newton_step /= 1.0 - adj
    p = p0 - newton_step
    if np.isclose(p, p0, rtol=rtol, atol=tol):   # 步长判据
        return _results_select(full_output, (p, funcalls, itr+1, _ECONVERGED), method)
    p0 = p
```

注意 `newton` 的收敛判据是**步长** `isclose(p, p0)`，**不是**括号法的区间宽度——这呼应了它的「不保证找到真根，只保证步长收敛」特性（文档 Notes 明确提醒「结果应当被验证」）。

[Halley 的保护](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L346-L353)：注释解释得很清楚——只有当 `|adj|<1`（即分母 `1-adj` 仍为正且接近 1）时才用 Halley，否则会「把 x 推向 Newton 的反方向」，退回普通 Newton 步。

**向量化核心** [_array_newton](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L410-L506)（Newton-Raphson 分支）：

```python
p = np.array(x0, copy=True)
failures = np.ones_like(p, dtype=bool)      # 全部待解
nz_der = np.ones_like(failures)
for iteration in range(maxiter):
    fval = np.asarray(func(p, *args))
    if not fval.any(): ... break             # 全部 fval==0
    fder = np.asarray(fprime(p, *args))
    nz_der = (fder != 0)
    dp = fval[nz_der] / fder[nz_der]         # 只更新非零导数
    ...
    p[nz_der] -= dp
    failures[nz_der] = np.abs(dp) >= tol     # 哪些还没收敛
    if not failures[nz_der].any(): break     # 全部收敛
```

`full_output=True` 时返回一个 `namedtuple('result', ('root','converged','zero_der'))`，其中 `converged` 和 `zero_der` 都是**与 `x0` 同形的布尔数组**，告诉你每个初值分别是什么状态——这是与标量 `newton`（返回单个 `RootResults`）的重大区别。

#### 4.5.4 代码实践

**目标**：感受向量化 `newton` 一次解一组问题的便利。

```python
# 示例代码
import numpy as np
from scipy.optimize import newton

# 对一组参数 a，各求 x**3 = a 的实根
f = lambda x, a: x**3 - a
fder = lambda x, a: 3 * x**2
a = np.arange(-50, 50, dtype=float)
x0 = np.ones_like(a)             # 全部从 1 出发

roots = newton(f, x0, fprime=fder, args=(a,), maxiter=200)
expected = np.sign(a) * np.abs(a)**(1/3)
print("max error =", np.max(np.abs(roots - expected)))
```

**需要观察的现象**：一次调用就得到 100 个根，与解析解高度吻合。对比用 `for` 循环逐个调用 `newton`（见 [_zeros_py.py:283-287](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L283-L287) 文档示例），向量版在大数组上更快。

**预期结果**：`max error` 极小（量级 \(10^{-15}\)）。性能差异待本地用 `timeit` 验证。

#### 4.5.5 小练习与答案

**练习 1**：如果 `fprime` 返回 0（导数为零），`newton` 会怎样？

> **答案**：标量分支里若 `fder==0`，`disp=True` 时抛 `RuntimeError("Derivative was zero...")`；`disp=False` 时发 `RuntimeWarning` 并返回带 `_ECONVERR` 的结果（见 [_zeros_py.py:329-339](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L329-L339)）。向量化分支则用 `nz_der` 掩码跳过这些元素、最后统计「多少个零导数」。

**练习 2**：为什么 `newton` 用步长判据，而括号法用区间宽度？

> **答案**：点法**没有区间**，无法用「区间宽度」判停。它只能用「连续两步之差」作判据——但这只说明「步子小了」，**并不保证**真的靠近真根（可能停在导数平坦处）。括号法因为始终夹住根，区间宽度就是误差的严格上界，所以判据可靠。这正是文档反复强调「`newton` 结果应被验证」的原因。

---

### 4.6 TOMS748Solver 与 toms748：最高效的括号法

#### 4.6.1 概念说明

`toms748` 实现 Alefeld、Potra、Shi 三人 1995 年发表的 **Algorithm 748**（发表在 ACM Transactions on Mathematical Software，"TOMS"）。它是目前已知**对四阶可微函数最渐近高效**的括号求根法：

| 参数 k | 收敛阶 | 每步函数求值 | 效率指数 |
|---|---|---|---|
| k=1（默认） | \(\geq 2.7\) | ~2 | ~1.65 |
| k=2 | ~4.6 | ~3 | ~1.66 |

与 Brent「只在最后一步缩小括号」不同，Algorithm 748 **每一步都以同样的渐近效率同时缩小括号**。它的核心是两种插值的混合：

- **逆三次插值**（`_inverse_poly_zero`）：用 4 个点 \((f_i, x_i)\) 拟合一条三次曲线，根就是 \(x=IP(0)\)。
- **Newton-二次步**（`_newton_quadratic`）：用三个点构造二次多项式 \(P(x)\)，对其做若干次 Newton-Raphson 步。

每步先尝试逆三次（条件好时），否则退回 Newton-二次；并保证括号始终成立。`TOMS748Solver` 是一个**有状态的对象**（与无状态的 C 函数不同），它把 `ab`、`d`、`e` 等历史点存在实例属性里。

#### 4.6.2 核心流程

```
solve():
  start()：算 fa, fb，校验异号
  首步：_secant 产生第三个点 c，建立 d=(c,fc)
  循环 iterate() 直到收敛或超 maxiter：
    iterate() 内层循环（k 次）：
       若 4 个 f 值「足够分开」：c = _inverse_poly_zero（逆三次）
       否则：                      c = _newton_quadratic（k 步 Newton-二次）
       用 (c, fc) 重新括号
    双 Newton 步：c = u - 2·fu/A（u 是 |f| 最小的端点，A 是首项差商）
       并夹在区间内
    若区间未充分缩小（> 0.5·原宽）：强制一次二分兜底
  get_status()：isclose(a,b)→收敛；iter≥maxiter→失败
```

`iterate()` 一次调用通常产生 2 个新点（加上可能的兜底二分共 2~3 个函数求值），所以 `nfev ≈ 2·nit`。

#### 4.6.3 源码精读

**辅助函数群**（理解 `iterate` 的前提）：

- [_notclose](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L996-L1002)：检查 4 个 f 值是否「都不为 0、都有限、且两两不太接近」。只有通过才敢做逆三次插值——否则插值会数值病态。
- [_inverse_poly_zero](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L1089-L1096)：逆三次插值。它把「f 值当自变量、x 值当因变量」调 [_interpolated_poly](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L1069-L1086)（Neville 算法）求 \(x=IP(0)\)。
- [_newton_quadratic](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L1099-L1131)：用三个点 `[a,b,d]` 的差商构造二次 \(P(x)=f_a+B(x-a)+A(x-a)(x-b)\)，再做 `k` 次 Newton 步 `r -= P(r)/P'(r)`，且步出区间就退回中点。
- [_compute_divided_differences](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L1034-L1066)：计算差商表（Newton 形式多项式的系数）。
- [_update_bracket](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L1024-L1031)：用 `(c,fc)` 替换与 `fc` 同号的端点，保持异号不变量。

**主迭代 [iterate()](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L1228-L1307)** 的关键部分：

```python
for nsteps in range(2, self.k+2):
    if _notclose(self.fab + [fd, fe], rtol=0, atol=32*eps):
        c0 = _inverse_poly_zero(self.ab[0], self.ab[1], d, e,
                                self.fab[0], self.fab[1], fd, fe)   # 逆三次
        if self.ab[0] < c0 < self.ab[1]:
            c = c0
    if c is None:
        c = _newton_quadratic(self.ab, self.fab, d, fd, nsteps)     # Newton-二次兜底
    fc = self._callf(c)
    e, fe = d, fd;  d, fd = self._update_bracket(c, fc)             # 重新括号

# 双 Newton 步
u, fu = self.ab[uix], self.fab[uix]                  # |f| 最小的端点
_, A = _compute_divided_differences(self.ab, self.fab, forward=(uix==0), full=False)
c = u - 2 * fu / A                                   # 双 Newton
...
# 兜底：若区间没充分缩小，强制二分
if self.ab[1] - self.ab[0] > self._mu * ab_width:
    z = sum(self.ab) / 2.0; ...; self._update_bracket(z, fz)
```

**[start()](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L1188-L1217)** 负责校验端点、算 `fa/fb`、检查异号；**[get_status()](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L1219-L1226)** 用 `isclose(a,b,rtol,atol=xtol)` 判收敛。

**公开入口 [toms748](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L1341-L1487)**：校验后 `solver = TOMS748Solver(); solver.solve(...)`，再用 `_results_select` 包成 `RootResults`。注意它对 `rtol` 的下限要求更松（`rtol < _rtol/4` 才报错，是其他方法 `_rtol` 的 1/4），因为高阶方法对相对容差更敏感。

#### 4.6.4 代码实践

**目标**：精读逆三次插值代码并写注释；比较 `brentq` 与 `toms748` 的 `nit`/`nfev`（对应本讲综合实践前的热身）。

操作步骤：

1. 打开 [_inverse_poly_zero](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L1089-L1096) 与它调用的 [_interpolated_poly](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L1069-L1086)（Neville 算法）。
2. 在自己的副本里给 `_interpolated_poly` 的每一行加中文注释，说明 Neville 算法如何用 `Q`/`D` 两张表递推地算出 \(p(x)\)。
3. 运行下面脚本比较：

```python
# 示例代码
from scipy.optimize import brentq, toms748
import math

f = lambda x: math.exp(x) - 3        # 根 = ln(3)，单调
for name, fn in [("brentq", brentq), ("toms748", toms748)]:
    root, r = fn(f, 0, 2, full_output=True)
    print(f"{name:8s} nit={r.iterations:3d} nfev={r.function_calls:3d} "
          f"root={root:.15f} err={abs(root-math.log(3)):.2e}")
```

**需要观察的现象**：`toms748` 的 `nit` 通常比 `brentq` 少；两者根精度都极高。

**预期结果**：`toms748` 约 3~5 步、`nfev` 约 9~14；`brentq` 约 6~9 步。精确数值待本地验证。

> 说明：本讲规格里把这段插值描述为「逆二次插值」。准确地说，`TOMS748Solver` 同时使用**逆三次插值**（`_inverse_poly_zero`，4 个点）和**Newton-二次步**（`_newton_quadratic`，二次多项式）。你阅读时两者都要看。

#### 4.6.5 小练习与答案

**练习 1**：`iterate()` 里为什么先用 `_notclose` 检查再决定是否做逆三次插值？

> **答案**：逆三次插值要在 4 个 f 值上拟合多项式。若这些 f 值有几个很接近或为 0，插值多项式的分母会接近 0（病态），算出的 `c` 可能跑出区间甚至变成 `NaN`。`_notclose` 是一个前置健康检查，不通过就退回更稳的 `_newton_quadratic`。

**练习 2**：`toms748` 既是括号法，为什么还需要「强制二分」兜底？

> **答案**：即使有插值，某一步可能恰好把新区间只缩小一点点（比如根非常靠近端点）。代码用 `if ab_width_new > 0.5 * ab_width_old` 检测「没充分缩小」，此时额外做一次二分，保证每步至少把区间减半——这是维持「保证收敛」的最后保险。

---

## 5. 综合实践

把本讲的知识串起来：实现一个**自定义单调函数**，用 6 种方法分别求根，做一张「算法 × (nit, nfev, 精度)」对照表，并据此回答「什么时候该用哪个」。

**任务**：求 \(f(x)=e^x - k\) 的根（即 \(x=\ln k\)），对 \(k=3\)。

1. 用括号法四兄弟 `bisect`、`brentq`、`brenth`、`ridder`、`toms748`（区间 \([0,2]\)，`full_output=True`）。
2. 用点法 `newton`（初值 \(x_0=1\)，提供 `fprime`）。
3. 对每个结果打印 `nit`、`nfev`、根、与真值 \(\ln 3\) 的误差。

```python
# 示例代码
import math
from scipy.optimize import bisect, brentq, brenth, ridder, toms748, newton

k = 3
f = lambda x: math.exp(x) - k
fp = lambda x: math.exp(x)
true_root = math.log(k)

def run(name, fn, *a, **kw):
    root, r = fn(f, *a, full_output=True, **kw)   # 统一取 (root, RootResults)
    return name, r, root

table = [
    run("bisect",  bisect,  0, 2),
    run("brentq",  brentq,  0, 2),
    run("brenth",  brenth,  0, 2),
    run("ridder",  ridder,  0, 2),
    run("toms748", toms748, 0, 2),
]
# newton 是点法（给初值而非区间），单独处理
root_n, r_n = newton(f, 1.0, fprime=fp, full_output=True)
table.append(("newton", r_n, root_n))

print(f"{'method':9s} {'nit':>4s} {'nfev':>5s} {'error':>12s}")
for name, r, root in table:
    print(f"{name:9s} {r.iterations:4d} {r.function_calls:5d} {abs(root-true_root):12.2e}")
```

**需要观察并思考的现象**：

- `bisect` 的 `nit` 最大（约 50），`nfev ≈ nit+2`。
- `brentq`/`brenth`/`ridder`/`toms748` 的 `nit` 都是个位数；`ridder`/`toms748` 的 `nfev ≈ 2·nit`，`brentq`/`brenth` 的 `nfev ≈ nit+2`。
- `newton` 的 `nit` 最少（通常 4~6 步），但**没有区间保证**——把 `x0` 改成远离根的值（如 `x0=10`）观察它是否仍收敛。
- 所有方法的最终误差都接近 `xtol`/机器精度。

**用结论回答**（填空式，待本地验证后完善）：

- 「我有变号区间、求稳妥」→ `brentq`（社区默认推荐）。
- 「我有变号区间、追求最少迭代」→ `toms748`。
- 「我没有区间、只有初值、且函数可导」→ `newton`（注意验证结果）。
- 「我有变号区间、想要最简单稳健」→ `bisect`（最慢但绝不出错）。

---

## 6. 本讲小结

- `_zeros_py.py` 是一维求根的**算法层**：`newton`/`_array_newton`/`toms748` 是**纯 Python**；`bisect`/`brentq`/`brenth`/`ridder` 只是薄包装，真正算法体在 `Zeros/*.c`。
- **括号法**（`bisect`/`brentq`/`brenth`/`ridder`/`toms748`）都靠「保持区间两端异号」这个不变量**保证收敛**；**点法**（`newton`/`secant`/`halley`）不保证收敛但更快且支持复数。
- `bisect` 是线性收敛（阶 1）的稳健底线；`brentq`/`brenth` 用「插值外推 + 二分保护」达到约 1.62 阶；`ridder` 用指数拟合达到约 2 阶；`toms748` 用逆三次插值 + Newton-二次步达到 ≥2.7（k=1）甚至 ~4.6（k=2）阶，是括号法里最快的。
- `brentq` 与 `brenth` 的 C 代码**几乎逐行相同**，差别仅在外推那一步：逆二次（IQI）vs 双曲。
- `newton` 的收敛判据是**步长**而非区间宽度，所以结果需验证；`_array_newton` 用 `failures`/`nz_der` 两个掩码把成百上千个初值一次性并行推进。
- `RootResults`（继承 `OptimizeResult`）+ C/Python 共享的整数状态码（`CONVERGED=0` 等）是所有求根结果的**统一封装底座**。

## 7. 下一步学习建议

- 本讲讲完了一维（标量）求根的全部底层算法。接下来按学习路线进入 **[u3 导数近似与函数封装基础设施](u3-l1-numerical-differentiation.md)**——`approx_derivative` 等会反过来被这些求根/优化算法使用（例如 `newton` 在无解析导数时的有限差分），是把「求根」与「优化」联系起来的关键工具。
- 若你想先看**多元**求根（解方程组 \(F(\mathbf{x})=\mathbf{0}\)），可以跳到 [u8-l1 root 统一接口与 fsolve/hybr](u8-l1-root-interface.md)，那里会复用本讲 `newton` 的思想并推广到向量函数。
- 对 C 底层感兴趣的读者，建议结合 [u1-l2](u1-l2-directory-build-and-backends.md) 重读 [_zerosmodule.c](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zerosmodule.c) 的 `setjmp/longjmp` 与「函数指针反向回调 Python」模式——这是 scipy.optimize 所有 C 扩展共用的跨语言交互范式，后续 [u10-l3](u10-l3-cython-and-c-backends.md) 会系统讲 Cython/C 后端。

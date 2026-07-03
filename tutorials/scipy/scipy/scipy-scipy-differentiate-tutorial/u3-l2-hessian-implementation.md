# hessian 的实现

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `hessian` 实现**二阶导数**的核心思路——它不发明任何新的多元差分公式，而是「**雅可比的雅可比**」：先对 `f` 求一次 `jacobian` 得到梯度，再对这个梯度函数求一次 `jacobian`，结果就是海森矩阵。
- 解释为什么嵌套调用时**内层 `rtol` 必须比外层紧 100 倍**（`rtol/100`），以及当用户把 `rtol` 设到低于 `100·eps` 时为什么会触发 `RuntimeWarning` 并被自动钳位（clamp）。
- 看懂 `hessian` 如何在嵌套调用中**累计 `nfev`**：用「列表收集 → `cumulative_sum` 求累计 → `take_along_axis` 按各元素自己的 `nit` 取值」三步，把内层每次求雅可比消耗的函数调用数正确归账到每一个海森元素 `[i, j]` 上。
- 理解结果对象的**属性重命名**：为什么 `res.df` 被改名为 `res.ddf`、`res.nit` 被删除，而 `nfev` 被重新计算。
- 能够独立用 `hessian` 计算 Rosenbrock 函数的海森矩阵并与 `scipy.optimize.rosen_hess` 对照，复现 `rtol` 告警。

本讲是「组合实现」单元的第二篇，承接 **u3-l1（jacobian 的实现）**。`jacobian` 已经把「多元函数求雅可比」翻译成了复用 `derivative` 的「逐元素标量求导」；`hessian` 则在这之上再套一层 `jacobian`，把问题升到二阶。

## 2. 前置知识

在进入本讲前，请确认你已理解以下概念（它们在前序讲义中讲过）：

- **海森矩阵（Hessian）**：设标量函数 \(f:\mathbf{R}^m \rightarrow \mathbf{R}\)，其在点 \(x\) 处的海森矩阵 \(H\) 是一个 \(m\times m\) 的对称矩阵，第 \((i, j)\) 个元素为二阶偏导数 \(\partial^2 f / \partial x_i \partial x_j\)。它是**梯度（一阶导）的雅可比**。
- **梯度与雅可比的关系**（u3-l1）：对标量函数 \(f\)，`jacobian(f, x).df` 给出的就是长度 \(m\) 的梯度向量 \(\nabla f\)，形状 \((m,)\)。
- **`jacobian` 的 `wrapped` 对角扰动技巧**（u3-l1）：`jacobian` 内部构造一个 \(m\times m\) 对角扰动矩阵，把多元雅可比拆成 \(n\cdot m\) 个独立的一元标量求导。`hessian` 不关心这个细节，只把 `jacobian` 当作一个可信的「求雅可比黑盒」。
- **`derivative` 的容差与终止**（u1-l3、u2-l5）：默认 `atol = smallest_normal`、`rtol = sqrt(eps)`；收敛判据是 `error < atol + rtol*|df|`。
- **嵌套有限差分的误差传播直觉**：如果内层求导本身有误差 \(\varepsilon_{\text{inner}}\)，外层差分会把这个误差放大并叠加到最终结果里，因此内层必须比外层「准得多」。

一句话直觉：**`hessian(f, x) = jacobian( gradient_of_f, x)`，而 `gradient_of_f(x) = jacobian(f, x).df`。** 二阶导 = 对一阶导再求一次雅可比。`hessian` 函数的全部代码，就是在「正确地把这两次 `jacobian` 串起来」，并处理好容差、求值次数统计与结果命名这三件配套工程。

## 3. 本讲源码地图

本讲几乎全部聚焦在单个文件里的一段代码：

| 文件 | 作用 |
| --- | --- |
| [`scipy/differentiate/_differentiate.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py) | `hessian` 的全部实现（签名、docstring 的嵌套说明、`df` 内层函数、容差收紧与告警、`nfev` 累计、属性重命名）都在第 951–1141 行。`jacobian`（第 723–948 行）被它当作黑盒调用。 |
| `scipy/differentiate/tests/test_differentiate.py` | `TestHessian` 用例（`test_example`、`test_nfev`、`test_small_rtol_warning` 等）是验证我们对 `nfev` 语义与告警行为理解的最佳旁证。 |

本讲不展开 `jacobian` 内部的对角扰动与 `derivative` 的差分权重（已在 u3-l1、u2 系列讲过），而是把 `jacobian` 当作一个可信的「求雅可比黑盒」来使用。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **嵌套 jacobian 求二阶导**（`hessian` 的核心数学与主流程）
2. **内层 rtol 收紧与告警**（为何 `rtol/100`、`rtol_min` 钳位与 `RuntimeWarning`）
3. **nfev 累计与属性重命名**（嵌套调用下的求值次数记账与 `df → ddf`）

---

### 4.1 嵌套 jacobian 求二阶导

#### 4.1.1 概念说明

`hessian` 要计算的是二阶导数。对一元函数，「二阶导」就是对导函数再求一次导；对多元函数，这个推广就是「**海森 = 梯度的雅可比**」。用数学语言：

设标量函数 \(f:\mathbf{R}^m \rightarrow \mathbf{R}\)，其梯度为向量函数

\[
g(x) = \nabla f(x) = \left(\frac{\partial f}{\partial x_1}, \dots, \frac{\partial f}{\partial x_m}\right)^{\top}, \qquad g:\mathbf{R}^m \rightarrow \mathbf{R}^m.
\]

对 \(g\) 再求一次雅可比，其第 \((i, j)\) 个元素是

\[
\bigl(J_g(x)\bigr)_{ij} = \frac{\partial g_i}{\partial x_j} = \frac{\partial^2 f}{\partial x_i \partial x_j} = H_{ij}(x).
\]

所以海森矩阵 \(H = J_g\)，即

\[
H(x) = \text{雅可比}\bigl(\,\nabla f\,\bigr)(x).
\]

而我们已经知道（u3-l1）：`jacobian(f, x).df` 正好给出梯度 \(\nabla f(x)\)。于是「求海森」只需把 `jacobian` 套两层：

```
内层 jacobian(f, x)        →  得到梯度 g(x) = ∇f(x)        （向量值函数 R^m → R^m）
外层 jacobian(g, x)        →  得到 g 的雅可比 = 海森 H       （矩阵，形状 (m, m)）
```

这就是 docstring 里那句「`hessian` is implemented by nesting calls to `jacobian`」（通过嵌套 `jacobian` 调用实现）的全部含义。`hessian` 自己**不写任何差分公式**，所有有限差分都由两层之下的 `derivative` 完成。

> 说明：「梯度」「雅可比」「海森」是数学概念；源码里没有叫 `gradient` 的变量，内层那个返回梯度的函数被命名为 `df`（因为它返回的是「`f` 的（一阶）导数」），外层 `jacobian` 对这个 `df` 再求雅可比。

#### 4.1.2 核心流程

`hessian` 主流程的伪代码（略去容差与 `nfev` 细节，它们在 4.2、4.3 展开）：

```
hessian(f, x, ...):
  1. 解析 tolerances，得到 atol / rtol
  2. xp = array_namespace(x)                      # 取后端命名空间
  3. x0 = xp_promote(x, force_floating=True)      # 整数 x 升浮点
  4. 解析默认 rtol = sqrt(eps)；必要时钳位、告警（见 4.2）
  5. 定义内层函数 df(x):
       temp = jacobian(f, x, tolerances=dict(rtol=外层rtol/100, atol=atol), ...)
       把 temp.nfev 记账到全局 nfev 列表（见 4.3）
       return temp.df          # 返回梯度 ∇f(x)
  6. res = jacobian(df, x, tolerances=用户tolerances, ...)   # ← 雅可比的雅可比 = 海森
  7. 用记账的 nfev 列表重算 res.nfev（见 4.3）
  8. res.ddf = res.df; del res.df; del res.nit    # 属性重命名（见 4.3）
  9. return res                                   # res.ddf 即海森矩阵
```

关键在第 5、6 步：第 5 步定义的 `df` 就是「梯度函数」，第 6 步对它求雅可比得到海森。两次 `jacobian` 用的 `x` 是同一个用户输入。

#### 4.1.3 源码精读

docstring 的 Notes 段落是「嵌套实现」这一设计的法定说明：

[`_differentiate.py`:L1059-L1065](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1059-L1065) —— 明文写明：当前 `hessian` 通过嵌套 `jacobian` 实现；除 `rtol` 外，所有选项对内、外两次调用都生效；内层 `rtol` 比外层紧 100 倍；因此 `rtol` 不应低于 `100` 倍 dtype 精度，否则会告警。这段是本讲三个模块的总纲。

核心的两层调用浓缩在主流程里。先看内层 `df` 与外层 `jacobian`：

[`_differentiate.py`:L1125-L1132](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1125-L1132) —— 这是整个 `hessian` 的心脏。逐行说明：

- **L1125** `def df(x):`：定义内层函数 `df`，它就是「梯度函数」\(g(x)=\nabla f(x)\)。注意它捕获了外层 `hessian` 作用域里的 `f`、`rtol`、`atol`、`kwargs` 以及 `nfev` 列表（闭包）。
- **L1126** `tolerances = dict(rtol=rtol/100, atol=atol)`：内层用「外层 `rtol` 除以 100」作为相对容差（理由见 4.2），`atol` 原样透传。
- **L1127** `temp = jacobian(f, x, tolerances=tolerances, **kwargs)`：**第一层 jacobian**——对原始 `f` 求雅可比，得到梯度。`kwargs` 包含 `maxiter`/`order`/`initial_step`/`step_factor`，与外层共用。
- **L1128** `nfev.append(...)`：把这次内层求雅可比消耗的函数调用数记账（详见 4.3）。
- **L1129** `return temp.df`：返回梯度。于是 `df` 成了一个 `R^m → R^m` 的向量值函数。
- **L1131** `nfev = []`：初始化记账列表（必须在定义 `df` 之后、调用之前创建，因为 `df` 闭包引用它）。
- **L1132** `res = jacobian(df, x, tolerances=tolerances, **kwargs)`：**第二层 jacobian**——对梯度函数 `df` 求雅可比，结果就是海森矩阵。行尾注释 `# jacobian of jacobian` 一语中的。注意外层用的是用户原始的 `tolerances`（见 4.2.3 对此的说明）。

函数签名本身也值得一看——它和 `jacobian` 几乎一样，但有意**收窄了**接口：

[`_differentiate.py`:L953-L954](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L953-L954) —— `hessian` 签名。与 `jacobian`（[L723-L724](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L723-L724)）相比，`hessian` **连 `step_direction` 都不暴露**。原因是二阶导需要同时扰动两个坐标方向，单侧差分在嵌套下语义复杂，索性只保留中心差分（`step_direction` 默认 `0`，由内层 `jacobian`/`derivative` 使用）。

签名上方的装饰器与 `derivative`/`jacobian` 完全一致：

[`_differentiate.py`:L951-L952](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L951-L952) —— `@xp_capabilities(...)` 声明跳过 `array_api_strict`、`dask.array`，并关闭 `jax_jit`。这是 u4-l4 的主题，本讲只需知道它存在。

#### 4.1.4 代码实践

**实践目标**：用 `hessian` 计算 Rosenbrock 函数在随机点的海森矩阵，与解析参照 `scipy.optimize.rosen_hess` 对照，验证「雅可比的雅可比」确实给出正确的 \((m, m)\) 海森。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.differentiate import hessian
from scipy.optimize import rosen, rosen_hess

rng = np.random.default_rng(4589245925010)
m = 3
x = rng.random(m)            # 随机点，形状 (m,)

res = hessian(rosen, x)      # 数值海森
ref = rosen_hess(x)          # 解析海森参照（rosen_hess(x) 直接返回 (m, m) 矩阵）

print("res.ddf.shape =", res.ddf.shape)
print("res.ddf =\n", np.round(res.ddf, 4))
print("ref    =\n", np.round(ref, 4))
print("allclose =", np.allclose(res.ddf, ref, atol=1e-8))
print("res.success =", res.success)
```

**需要观察的现象**：

- `res.ddf.shape` 应为 `(3, 3)`（海森是方阵）。
- `res.ddf` 与 `rosen_hess(x)` 在容差内一致；并且 `res.ddf` 应近似对称（海森的理论对称性；注意源码注释说明当前实现**未强制**对称，见 [test_differentiate.py:L659-L662](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L659-L662) 中被注释掉的对称性检查）。
- 属性名是 **`ddf`**（double derivative），不是 `df`——这是 `hessian` 对结果做的重命名（见 4.3）。

**预期结果**：`allclose` 为 `True`，`res.success` 全为 `True`。本结果已由 docstring 示例（[L1082-L1087](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1082-L1087)）与测试 `test_example`（[test_differentiate.py:L644-L657](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L644-L657)，`xp_assert_close(res.ddf, ref, atol=1e-8)`）佐证。

#### 4.1.5 小练习与答案

**练习 1**：`hessian` 要求 `f` 是 `R^m → R`（标量输出）。如果硬把一个向量值函数 `f: R^m → R^n`（`n>1`）传给 `hessian`，按本讲的嵌套逻辑会得到什么形状的结果？它还是「海森矩阵」吗？

**参考答案**：内层 `jacobian(f, x)` 会得到形状 `(n, m)` 的雅可比；外层 `jacobian(df, x)` 对这个 `(n, m)` 的向量值函数再求雅可比，结果形状会是 `(n, m, m)`——可以理解为 `n` 个标量分量各自的海森叠在一起。严格意义上「海森矩阵」专指标量函数的二阶导 \((m, m)\)，所以对向量值函数得到的是「海森张量」。`hessian` 的 docstring（[L1037-L1049](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1037-L1049)）只承诺标量输出 `f: R^m → R` 的情形。

**练习 2**：为什么 `hessian` 不像 `jacobian` 那样暴露 `step_direction` 参数？

**参考答案**：二阶导在嵌套实现下涉及「对梯度求雅可比」，单侧差分（`step_direction≠0`）在两层嵌套里的组合语义复杂且容易越界；`hessian` 只保留中心差分（`step_direction=0`）以保持实现简洁与稳健。代价是无法直接处理定义域边界附近的二阶导（这是当前实现的已知局限）。

---

### 4.2 内层 rtol 收紧与告警

#### 4.2.1 概念说明

把 `jacobian` 嵌套两层会带来一个**误差传播**问题。外层 `jacobian(df, x)` 用有限差分估计 `df`（梯度）的雅可比时，它调用的每一个 `df(x)` 本身就是一个**带误差的数值估计**（来自内层 `jacobian`）。如果内层误差 \(\varepsilon_{\text{inner}}\) 与外层想要分辨的步长量级相当，外层差分就会被噪声淹没，得到的海森毫无意义。

直观地说：外层在用「放大镜」看梯度的变化，那么梯度本身必须比放大镜能分辨的刻度**精细得多**。源码采取的做法是把**内层的相对容差收紧 100 倍**——内层 `rtol = 外层 rtol / 100`——「with the expectation that the inner error can be ignored」（期望内层误差可忽略，见 [L1059-L1065](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1059-L1065)）。

但这带来一个下限：内层 `rtol = 外层 rtol / 100` 至少要大于机器精度 `eps`，否则 `derivative` 的误差估计本身就全是浮点噪声、不可信。反推得**外层 `rtol` 不应低于 `100·eps`**。当用户把 `rtol` 设得更小时，`hessian` 会：

1. 发出一个 `RuntimeWarning`，提示误差估计可能不可靠；
2. 把 `rtol` **钳位**（clamp）到 `100·eps`，避免内层落到精度以下。

> 关键数字（以 float64 为例）：`eps ≈ 2.22e-16`，`sqrt(eps) ≈ 1.49e-8`（默认 `rtol`），`rtol_min = 100·eps ≈ 2.22e-14`。默认 `rtol = sqrt(eps)` 远大于 `rtol_min`，所以默认调用不会告警；只有用户主动把 `rtol` 压到 `1e-15` 这类量级才会触发。

#### 4.2.2 核心流程

```
容差解析与收紧流程：
  1. atol = tolerances.get('atol', None)
     rtol = tolerances.get('rtol', None)
  2. 若 rtol 为 None：rtol = sqrt(eps)            # 与 derivative 默认一致
  3. rtol_min = 100 * eps
  4. 若 0 < rtol < rtol_min：
       warnings.warn(...)                          # 提示不可靠
       rtol = rtol_min                             # 钳位到下限
  5. 内层 df 用 rtol/100；外层用用户原始 tolerances
```

注意第 4 步的条件是 `0 < rtol < rtol_min`——**只钳位正的、过小的 `rtol`**。`rtol <= 0` 被注释为「an error」（[L1121](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1121) 行尾注释），不在本层处理，会原样传给内层（若为负，最终由 `derivative` 的 `_derivative_iv` 校验抛 `ValueError`，见 u2-l1）。

#### 4.2.3 源码精读

容差解析与默认值，与 `derivative` 保持一致：

[`_differentiate.py`:L1107-L1115](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1107-L1115) —— 从 `tolerances` 字典取出 `atol`/`rtol`（缺省为 `None`）；`xp_promote` 把整数 `x` 升浮点；`finfo = xp.finfo(x0.dtype)` 取该 dtype 的浮点信息；默认 `rtol = finfo.eps**0.5`。行尾注释 `# keep same as `derivative`` 强调这个默认值与 `derivative` 完全一致（对照 [`derivative` 内的同一行注释 L402](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L402)）。之所以要在 `hessian` 里**自己**解析 `rtol` 而不是直接透传给 `jacobian`，正是为了下面这一步钳位与告警。

钳位与告警：

[`_differentiate.py`:L1117-L1123](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1117-L1123) —— `rtol_min = finfo.eps * 100`；用 f-string 拼出告警信息（会把当前 `rtol` 与 `rtol_min` 都嵌进文本，便于排错）；`if 0 < rtol < rtol_min:` 则 `warnings.warn(message, RuntimeWarning, stacklevel=2)` 并把 `rtol` 提到 `rtol_min`。`stacklevel=2` 让告警指向**用户调用 `hessian` 的那一行**，而不是 `hessian` 内部这一行——这是 `warnings` 模块的好习惯。

内层收紧 100 倍：

[`_differentiate.py`:L1125-L1126](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1125-L1126) —— 内层 `df` 里 `tolerances = dict(rtol=rtol/100, atol=atol)`。这里的 `rtol` 是经过上面钳位后的值，所以即便用户传 `rtol=1e-15`，内层用的也是 `rtol_min/100 = eps`，而非 `1e-15/100`（那样会跌到 eps 以下）。

一个容易忽略的细节：**外层 `jacobian` 用的是用户原始的 `tolerances` 字典**，不是钳位后的 `rtol`：

[`_differentiate.py`:L1132](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1132) —— `res = jacobian(df, x, tolerances=tolerances, **kwargs)`，这里的 `tolerances` 是函数参数（用户字典或 `{}`）。也就是说，钳位只作用于「内层用的 `rtol` 变量」；外层会按用户给的 `tolerances`（或缺省）自行解析 `rtol`。在正常使用（`rtol ≥ 100·eps`）下两者一致：外层 `rtol = X`，内层 `rtol = X/100`。

#### 4.2.4 代码实践

**实践目标**：复现 `rtol` 告警——给 `hessian` 传一个过小的 `rtol=1e-15`，观察并解释触发的 `RuntimeWarning`。

**操作步骤**：

```python
# 示例代码
import warnings
import numpy as np
from scipy.differentiate import hessian

x = np.array([1.0])

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    res = hessian(np.sin, x, tolerances=dict(rtol=1e-15))
    for w in caught:
        print(w.category.__name__, "->", str(w.message))

# 看看钳位后内层实际用的 rtol 推断：
eps = np.finfo(np.float64).eps
print("eps        =", eps)
print("rtol_min   =", 100*eps, "  （rtol 被钳位到这里）")
print("inner rtol =", 100*eps/100, "  （= eps，内层下限）")
```

**需要观察的现象**：

- 捕获到一条 `RuntimeWarning`，文本形如：``The specified `rtol=1e-15`, but error estimates are likely to be unreliable when `rtol < ...`.` ``。这与测试 `test_small_rtol_warning`（[test_differentiate.py:L700-L703](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L700-L703)，`pytest.warns(RuntimeWarning, match='The specified `rtol=1e-15`, but...')`）完全吻合。
- 打印显示 `eps ≈ 2.22e-16`，`rtol_min ≈ 2.22e-14`，内层 `rtol = eps ≈ 2.22e-16`。

**解释（结合源码）**：用户 `rtol=1e-15 < rtol_min=100·eps`，进入 [L1121-L1123](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1121-L1123) 的分支：先告警，再把 `rtol` 钳到 `rtol_min`。于是内层 `df` 用的 `rtol/100 = eps`，恰好卡在精度下限——若不钳位，内层 `rtol = 1e-15/100 = 1e-17 < eps`，`derivative` 的误差估计会全是浮点噪声，海森结果不可信。告警正是提醒用户：「你给的容差已经低于精度地板，误差估计别太当真」。

**预期结果**：上述打印与现象一致。本结果已由 `test_small_rtol_warning` 佐证；该测试用 `np_only=True` 标记（[L698-L699](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L698-L699)），因为 `[1.]` 这种 Python list 输入只走 NumPy 后端。

#### 4.2.5 小练习与答案

**练习 1**：默认调用 `hessian(np.sin, x)`（不传 `tolerances`）会不会触发告警？为什么？

**参考答案**：不会。默认 `rtol` 为 `None`，经 [L1115](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1115) 解析为 `sqrt(eps) ≈ 1.49e-8`，远大于 `rtol_min = 100·eps ≈ 2.22e-14`，不满足 `0 < rtol < rtol_min`，故不告警、不钳位。

**练习 2**：如果把 `rtol` 钳位的「100 倍」改成「10 倍」（即 `rtol_min = 10·eps`、内层 `rtol/10`），会有什么风险？

**参考答案**：内层 `rtol = 外层rtol/10`，要让内层 ≥ `eps`，外层只需 ≥ `10·eps`，门槛更低。但「紧 10 倍」意味着内层误差只比外层目标小一个量级，嵌套差分时内层噪声更容易渗透到外层结果，海森的数值精度会下降。100 倍是作者在「内层足够准」与「内层不要为追求过高精度而浪费函数调用」之间选的经验折中。

---

### 4.3 nfev 累计与属性重命名

#### 4.3.1 概念说明

这是 `hessian` 实现里最巧妙的一段工程。问题来自**嵌套调用**：

- 外层 `jacobian(df, x)` 在迭代过程中会**多次调用** `df`（每个外层差分求值点调一次，首轮还要多调一次做初始化校验）。
- 每调用一次 `df`，内层就跑一整次 `jacobian(f, x)`，消耗**若干次** `f` 求值（记在 `temp.nfev`）。
- 但 `jacobian` 返回的 `res.nfev` 只反映「外层这一层」对 `df` 的调用次数统计，**完全不包含内层 `f` 的真实求值次数**——因为对外层而言，`df` 是个黑盒，它不知道 `df` 内部调了 `f` 多少次。

如果直接把外层 `jacobian` 的 `res.nfev` 返回给用户，那个数字会**严重低估**真实的 `f` 求值次数（它只数了「调 `df` 的次数」，而非「`df` 内部调 `f` 的次数」）。所以 `hessian` 必须自己**重新记账**。

记账要满足 docstring 的承诺（[L1026-L1029](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1026-L1029)）：

> 元素 `[i, j]` of `nfev` = 为了计算海森元素 `ddf[i, j]` 而调用 `f` 的次数。

注意「每个海森元素收敛的迭代轮数可能不同」（逐元素自适应，见 u2-l6 的压缩机制），所以同一个 `[i,j]` 在不同迭代轮消耗的 `f` 调用要**累计到该元素自己收敛为止**。

至于**属性重命名**，则是为了语义清晰：

- 外层 `jacobian` 返回的 `df` 在这里是「梯度的雅可比」= 海森，所以重命名为 **`ddf`**（double derivative），避免与一阶导混淆。
- 外层的 `nit` 只反映**外层**的迭代轮数，对用户无意义（用户关心的是总收敛情况），所以**删除**。
- `nfev` 用上面记账的结果**覆盖**。

> 一个文档小瑕疵：docstring 第 1048 行把属性名写成了 ``dff``（`attribute ``dff`` of the result object will be an array of shape (m, m)``），但代码里实际创建的是 `ddf`（[L1137](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1137)）。这是文档笔误，以代码为准——读源码时注意别被误导。

#### 4.3.2 核心流程

`nfev` 记账分三步：

```
第一步：在 df 内层「边算边收集」
  每次 df 被外层调用 → 跑一次内层 jacobian → 得 temp.nfev
  nfev.append( temp.nfev            # 首轮：初始化单点调用，形状已与海森元素对齐
             if len(nfev)==0
             else temp.nfev.sum(axis=-1) )  # 后续：沿「外层求值点」轴求和

第二步：累计求和
  nfev = cumulative_sum( stack(nfev), axis=0 )
  # 形状 (T, ...)，T = 外层调用 df 的总次数
  # 沿 axis=0 累加 → 每个 [i, j] 处是「截至第 t 轮，为 [i,j] 已花的 f 调用数」

第三步：按各元素的 nit 取值
  res_nit = res.nit[newaxis]                # 每个海森元素自己的收敛轮数
  res.nfev = take_along_axis(nfev, res_nit, axis=0)[0]
  # 对 [i,j] 取「它收敛那一轮」对应的累计值
```

直觉理解第三步：不同海森元素收敛得有快有慢；`take_along_axis` 相当于「每个元素去累计表里查**自己**那轮的总开销」。早收敛的元素查到较小的数，晚收敛的查到较大的数——正对应「为这个元素花的 `f` 调用数」。

#### 4.3.3 源码精读

收集阶段（在 `df` 内层）：

[`_differentiate.py`:L1128](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1128) —— `nfev.append(temp.nfev if len(nfev) == 0 else temp.nfev.sum(axis=-1))`。首轮（`len(nfev)==0`，对应外层 `_initialize` 的单点校验调用）直接保留 `temp.nfev`，因为它已经按海森元素形状给出；后续每轮外层会**批量**送入多个求值点（嵌套 stencil，见 u2-l3），使 `temp.nfev` 多出一个尾部的「求值点」轴，需要 `sum(axis=-1)` 把它压掉，得到「按海森元素」的总数。

累计与取值阶段：

[`_differentiate.py`:L1134-L1136](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1134-L1136) —— 逐行说明：

- **L1134** `nfev = xp.cumulative_sum(xp.stack(nfev), axis=0)`：`xp.stack` 把列表沿新轴 0 堆成 `(T,) + 海森形状`；`cumulative_sum(axis=0)`（Array API 标准名，等价于 NumPy 的 `cumsum`）沿「外层轮次」轴累加，得到每个海森元素截至每一轮的累计 `f` 调用数。
- **L1135** `res_nit = xp.astype(res.nit[xp.newaxis, ...], xp.int64)`：`res.nit` 是外层 `jacobian` 给出的、**逐海森元素**的收敛轮数（形状与 `ddf` 相同）。`[newaxis]` 在最前加一轴以匹配 `nfev` 的轮次轴；`astype(int64)` 是「appease torch」——`take_along_axis` 在 Torch 后端要求索引为 `int64`。
- **L1136** `res.nfev = xp.take_along_axis(nfev, res_nit, axis=0)[0]`：用每个元素的 `nit` 在累计表里「按轮次轴取值」，`[0]` 去掉前面加的那一轴。结果 `res.nfev` 形状与 `ddf` 相同，元素 `[i,j]` = 为 `ddf[i,j]` 花费的总 `f` 调用数。

属性重命名：

[`_differentiate.py`:L1137-L1139](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1137-L1139) —— `res.ddf = res.df`（把「雅可比」改名为「二阶导」），`del res.df`（删掉旧名，避免同时存在 `df` 和 `ddf` 引起歧义），`del res.nit`（外层 `nit` 无意义，删掉）。`res.success`、`res.status`、`res.error`、`res.nfev`（已覆盖为新值）保留。最终 `return res`（[L1141](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1141)）。

这套记账机制的正确性由测试 `test_nfev` 直接验证：

[`test_differentiate.py`:L674-L695](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L674-L695) —— `test_nfev` 的核心断言（[L687](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L687)、[L691](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L691)）：用一个自计数的 `f1`，比较「一次完整 `hessian` 调用得到的 `res.nfev[0,0]`」与「只算单个海森元素的独立调用 `res00.nfev[0,0]`」，并要求两者都等于 `f1` 实际被调用的总次数。这正是「`nfev[i,j]` = 为 `ddf[i,j]` 花的 `f` 调用数」的可执行定义。

#### 4.3.4 代码实践

**实践目标**：亲手验证「`res.nfev[i, j]` 等于为了计算 `ddf[i, j]` 而调用 `f` 的总次数」，即复现 `test_nfev` 的思路。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.differentiate import hessian

z = np.array([0.5, 0.25])

def f1(z):
    # 自计数：每次被调用就把计数器 +1
    f1.count += 1
    x, y = z
    return np.sin(x) * y**3
f1.count = 0

res = hessian(f1, z, initial_step=10)
print("res.nfev =\n", res.nfev)
print("f1 实际被调用总次数 =", f1.count)

# 关键对照：只算 ddf[0,0] 这一个元素
f1.count = 0
res00 = hessian(lambda x: f1(np.array([x[0], z[1]])), z[0:1], initial_step=10)
print("res.nfev[0,0] =", res.nfev[0, 0])
print("单元素调用 f1 次数 =", f1.count)
print("res00.nfev[0,0] =", res00.nfev[0, 0])
```

**需要观察的现象**：

- 第一次 `hessian` 后，`f1.count` 等于某个整数 `N_total`（整个海森计算中 `f1` 被调用的总次数）。
- `res.nfev[0,0]` 应等于「只算 `ddf[0,0]` 时 `f1` 被调用的次数」（第二次的 `f1.count`），也等于 `res00.nfev[0,0]`。
- 注意 `res.nfev[0,0]` **不一定**等于 `N_total`——因为 `N_total` 是所有 `m·m` 个元素一起算时的总和（含为其他元素花的调用），而 `res.nfev[0,0]` 只归账给 `[0,0]` 的那部分。

**预期结果**：`res.nfev[0,0] == 单元素调用 f1 次数 == res00.nfev[0,0]` 三者相等（与 [test_differentiate.py:L687](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L687) 的断言一致）。若本地运行，可把 `z` 换成不同点观察 `res.nfev` 各元素是否不同（反映逐元素自适应收敛快慢不同）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `df` 里收集 `nfev` 时，首轮用 `temp.nfev`、后续轮用 `temp.nfev.sum(axis=-1)`？

**参考答案**：首轮对应外层 `_initialize` 的**单点**校验调用，内层 `temp.nfev` 已经是「按海森元素」的形状，无需再压轴。后续每轮外层会**批量**送入多个差分求值点（嵌套 stencil，u2-l3），使内层 `temp.nfev` 多出一个尾部的「求值点」轴；`sum(axis=-1)` 把这一轴求和掉，得到「该轮内、按海森元素」的总调用数，使每轮的记账条目形状一致，才能 `stack` 在一起。

**练习 2**：如果不做 4.3 这套 `nfev` 记账，直接返回外层 `jacobian` 的 `res.nfev`，用户看到的数字会偏大还是偏小？

**参考答案**：会**严重偏小**。外层 `jacobian` 把 `df` 当黑盒，它的 `nfev` 只统计「调用了多少次 `df`」，而每次 `df` 内部其实跑了完整一次 `jacobian(f, x)`、消耗了若干次 `f` 调用。所以原始 `res.nfev` 远小于真实 `f` 求值次数。`hessian` 的三步记账正是把这些内层调用「翻译」回 `f` 的真实调用数。

**练习 3**：`hessian` 删除了 `res.nit`，却保留 `res.nfev`。如果一个用户想从结果里**粗略**判断「外层迭代了几轮」，还能做到吗？

**参考答案**：不能直接拿到「外层 `nit`」了（已被 `del`）。`res.nfev` 反映的是 `f` 的总调用数，与外层轮数正相关但不成简单比例（内层每轮的调用数也会变）。这是有意的接口收窄：`hessian` 认为「外层 `nit`」对用户无意义（它是嵌套实现的内部细节），只暴露有物理意义的 `ddf`/`error`/`nfev`/`success`/`status`。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务（即规格要求的代码实践任务的完整版）：

**任务**：对 Rosenbrock 函数完成「数值海森 vs 解析海森」对照，并触发与解释 `rtol` 告警。

1. **对照 `rosen_hess`**（承接 4.1）：取 `m=3`、`x = rng.random(m)`（固定种子可复现），调用 `hessian(rosen, x)`，打印 `res.ddf.shape`、`res.ddf`，与 `rosen_hess(x)` 用 `np.allclose(..., atol=1e-8)` 对照。再打印 `res.success`、`res.status`、`res.nfev.shape`，确认 `ddf` 与 `nfev` 形状都是 `(3, 3)`。
2. **触发 `rtol` 告警**（承接 4.2）：用 `warnings.catch_warnings(record=True)` 捕获 `hessian(rosen, x, tolerances=dict(rtol=1e-15))` 的告警，打印告警类别与文本；并打印 `eps`、`100*eps`，说明 `rtol=1e-15` 为何被钳位、内层最终用的 `rtol` 是多少。
3. **理解 `nfev` 记账**（承接 4.3）：在上面的对照里额外打印 `res.nfev`，观察其 `(3,3)` 各元素是否**不完全相同**——这正说明不同海森元素的收敛快慢不同（逐元素自适应），`take_along_axis(nit)` 给每个元素归账了不同的总调用数。

**需要解释的现象**（结合本讲源码）：

- 为什么 `res.ddf` 是 `(m, m)`？（答：海森 = 梯度的雅可比，外层 `jacobian(df, x)` 对 `R^m→R^m` 的梯度函数求雅可比，得 `(m, m)`，见 4.1。）
- 为什么 `rtol=1e-15` 会告警且不影响结果正确性？（答：`1e-15 < 100·eps`，触发 [L1121-L1123](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1121-L1123) 的钳位，内层 `rtol` 被提到 `eps`，仍能正常收敛，见 4.2。）
- 为什么 `res.nfev` 各元素可能不同？（答：逐元素自适应让不同 `[i,j]` 在不同外层轮次收敛，`take_along_axis(res.nit)` 给它们归账了到各自收敛为止的累计调用数，见 4.3。）

**预期结果**：第 1 步 `allclose` 为 `True`；第 2 步捕获到一条 `RuntimeWarning`，文本以 ``The specified `rtol=1e-15`, but`` 开头；第 3 步 `res.nfev` 为 `(3,3)` 整数矩阵且元素间存在差异。三步分别对应本讲的三个最小模块。若本地运行，可对照 `test_example`（[test_differentiate.py:L644-L657](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L644-L657)）与 `test_small_rtol_warning`（[L700-L703](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L700-L703)）验证。

## 6. 本讲小结

- `hessian` 通过**嵌套两次 `jacobian`** 实现二阶导：内层 `df(x) = jacobian(f, x).df` 给出梯度，外层 `jacobian(df, x)` 对梯度求雅可比 = 海森矩阵 \(H\)，形状 `(m, m)`。`hessian` 自己不写任何差分公式。
- 为控制嵌套差分的误差传播，**内层 `rtol` 比外层紧 100 倍**（`rtol/100`）；当用户 `rtol < 100·eps` 时，发出 `RuntimeWarning` 并把 `rtol` 钳位到 `100·eps`，使内层 `rtol` 不低于精度地板 `eps`。
- 嵌套调用下外层 `jacobian` 的 `nfev` 只数「调 `df` 的次数」，严重低估真实 `f` 调用数；`hessian` 用「`df` 内层边算边收集 → `cumulative_sum` 累计 → `take_along_axis(res.nit)` 按各元素收敛轮取值」三步重新记账，使 `res.nfev[i,j]` 准确等于为 `ddf[i,j]` 花费的 `f` 调用数。
- 结果对象做**属性重命名**：`res.df → res.ddf`（语义为二阶导），`del res.nit`（外层 `nit` 无意义），`res.nfev` 用记账结果覆盖；保留 `success`/`status`/`error`。注意 docstring 第 1048 行把 `ddf` 误写成 `dff`，以代码为准。
- 接口比 `jacobian` 更窄：`hessian` 不暴露 `step_direction`（二阶导嵌套下只保留中心差分）、也不暴露 `args`/`kwargs`/`callback`；需要额外参数的 `f` 用 `functools.partial` 或 `lambda` 包装。
- `derivative`/`jacobian`/`hessian` 三者呈清晰分层：`jacobian` 复用 `derivative`，`hessian` 复用 `jacobian`——这是 `scipy.differentiate` 子包「逐元素自适应迭代」框架在不同导数阶上的统一复用。

## 7. 下一步学习建议

- **进入专家层**：本讲结束后，初阶与进阶单元（u1–u3）已全部完成。建议进入 **u4（高级主题与工程实践）**。若想更深入理解 `preserve_shape` 在向量值函数与高阶导数中的形状契约（`hessian` 嵌套两次 `jacobian` 时 `wrapped` 被反复套用的形状流转），可重点读 **u4-l1（向量化与 preserve_shape 模式）**。
- **数值精度与调参**：`hessian` 的嵌套实现让精度问题加倍放大——内层误差、零二阶导（如鞍点处海森元素为 0）、大 `|x|` 步长等。建议接着读 **u4-l3（数值精度、消去误差与调参）**，理解 `atol`/`rtol`/`initial_step` 在高阶导数场景下的调参直觉。
- **测试与边界**：本讲多处引用了 `TestHessian` 的用例。建议通读 **u4-l5（测试体系与边界情况）** 中的 `test_example`/`test_float32`/`test_nfev`/`test_small_rtol_warning`，从测试反推 `hessian` 在 float32、多点批量、过小容差等边界下的行为。
- **源码再读一遍**：把 [`_differentiate.py`:L1100-L1141](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1100-L1141) 这 42 行连同本讲三节对照阅读，你会看到「数学（雅可比的雅可比）+ 工程（容差收紧、nfev 记账、属性重命名）」如何在一个极简函数里完整落地。

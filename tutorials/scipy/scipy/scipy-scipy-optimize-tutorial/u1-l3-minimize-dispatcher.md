# 统一调度入口：minimize 与 minimize_scalar

## 1. 本讲目标

上一篇（u1-l1）你已经认识了 `scipy.optimize` 的全局能力地图和三个公共对象（`OptimizeResult`、`OptimizeWarning`、`show_options`）。本篇要回答一个更具体的问题：**当你调用 `minimize(...)` 时，这一行代码内部到底发生了什么？它是怎么把你引向 BFGS、CG、SLSQP 这些具体算法的？**

读完本讲你应该能够：

- 读懂 `minimize` 的完整函数签名，知道 `fun / x0 / jac / hess / hessp / bounds / constraints / method / options / tol / callback` 每个参数的含义。
- 理解 `MINIMIZE_METHODS` 这张「合法方法清单」，以及为什么所有方法名在比较前都要 `.lower()`。
- **核心**：搞清楚当 `method=None`（不指定方法）时，调度器如何根据是否提供了 `bounds` / `constraints` 自动选择默认方法（无约束 → BFGS、有界 → L-BFGS-B、有约束 → SLSQP）。
- 看懂那条长长的 `if meth == '...' / elif ...` 分发链，以及如何用「自定义可调用对象」当 `method` 来接入自己的算法。
- 理解一维版本 `minimize_scalar` 是同一套「调度」思路的简化复刻。

本讲**不**深入任何单个算法的数学细节（那是单元 u4、u5 的事），只聚焦「调度/分发」这一层。把这一层看懂，后续读任何 `_minimize_*` 实现时，你都知道它是被谁、在什么条件下调用的。

## 2. 前置知识

- **调度器（dispatcher）/ 统一入口**：想象一家医院的导诊台——它本身不看病，只负责把你按症状分流到不同的专科医生。`minimize` 就是 `scipy.optimize` 的「导诊台」：它本身不做优化计算，而是根据 `method` 字符串把你路由到 `_minimize_bfgs`、`_minimize_slsqp` 等具体的「专科医生」。
- **多元函数与梯度、海森矩阵**：目标函数 \(f(\mathbf{x})\) 的输入 \(\mathbf{x}\) 是一个向量；它的梯度 \(\nabla f(\mathbf{x})\) 是一阶导数向量；海森矩阵 \(H(\mathbf{x})\) 是二阶导数矩阵。有的算法只要函数值（如 Nelder-Mead），有的还要梯度（如 BFGS），有的还要海森（如 trust-ncg）。
- **约束（constraints）与边界（bounds）**：边界限制每个变量的取值范围 \(lb_i \le x_i \le ub_i\)；约束则是更一般的等式 \(c(\mathbf{x})=0\) 或不等式 \(c(\mathbf{x})\ge 0\)。不是所有算法都能处理它们。
- **`OptimizeResult`**：上一篇讲过的统一结果对象（`dict` 子类，支持 `res.fun` 属性访问）。所有 `_minimize_*` 最后都要返回一个它。
- **Python 的模块全局名字查找**：函数体里写一个裸名字（如 `_minimize_bfgs`），Python 会在「定义该函数的模块」的全局命名空间里查找它。这一点在本讲的代码实践里会用到。

> 提示：本讲用到的最优化问题形式就是
>
> \[ \min_{\mathbf{x}\in\mathbb{R}^n} f(\mathbf{x}) \quad \text{s.t.} \quad \text{bounds / constraints} \]
>
> 默认测试函数仍是 Rosenbrock 函数，最小点在 \(\mathbf{x}=(1,1,\dots,1)\)。

## 3. 本讲源码地图

本讲主要看两个文件：

| 文件 | 作用 | 本讲关注的内容 |
| --- | --- | --- |
| `scipy/optimize/_minimize.py` | **调度器本身**：定义 `minimize`、`minimize_scalar` 以及方法清单 | `MINIMIZE_METHODS` / `MINIMIZE_METHODS_NEW_CB` / `MINIMIZE_SCALAR_METHODS`、`minimize` 的签名与分发、`minimize_scalar` 的分发 |
| `scipy/optimize/_optimize.py` | 大量算法与公共对象的「老家」 | `MemoizeJac`（`jac=True` 时拆分函数值与梯度）、`_wrap_callback`（回调包装）、`OptimizeResult`、`rosen` 系列测试函数 |

一句话关系：`_minimize.py` 是「前台调度」，它在顶部 `from ._optimize import ...` 把各 `_minimize_*` 实现引进来（`_optimize.py` 里有无约束那几个，其余分散在各 `_xxx_py.py` 子模块），然后在一行行 `if/elif` 里按方法名把请求派发过去。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：

1. `minimize` 的函数签名与参数语义。
2. 方法清单与新回调接口：`MINIMIZE_METHODS` / `MINIMIZE_METHODS_NEW_CB`。
3. `method=None` 时的自动默认选择与参数合法性校验（**本讲核心**）。
4. 方法分发 `if/elif` 链与自定义方法（custom callable）。
5. `minimize_scalar`：一维最小化的并行入口。

---

### 4.1 minimize 的函数签名与参数语义

#### 4.1.1 概念说明

`minimize` 是 `scipy.optimize` 最常用的入口。它只做两件事：**收参数** + **派发到具体算法**。所以它的签名里几乎塞满了「各种算法可能用到的一切」——梯度怎么算、海森怎么算、有没有边界、有没有约束、用什么方法、给方法的私有选项、容差、回调……理解这些参数的语义，是看懂后续分发逻辑的前提。

#### 4.1.2 核心流程

调用 `minimize(fun, x0, method=..., ...)` 后，调度器在真正派发之前会先做一次「输入预处理」：

```text
minimize(fun, x0, args, method, jac, hess, hessp, bounds, constraints, tol, callback, options)
   │
   ├─ x0 预处理：np.atleast_1d 保证至少 1 维；整型数组转 float
   ├─ args 预处理：非 tuple 则包成 (args,)
   ├─ （随后进入 4.3 的「默认选择 + 校验」与 4.4 的「分发」）
   └─ 返回 OptimizeResult
```

参数大致分四组：

| 分组 | 参数 | 一句话作用 |
| --- | --- | --- |
| 问题定义 | `fun`, `x0`, `args` | 目标函数、初值、传给函数的额外固定参数 |
| 导数 | `jac`, `hess`, `hessp` | 梯度、海森、海森×向量的提供方式 |
| 约束 | `bounds`, `constraints` | 变量边界、（非）线性约束 |
| 控制 | `method`, `options`, `tol`, `callback` | 选哪个算法、算法私有选项、收敛容差、每步回调 |

#### 4.1.3 源码精读

`minimize` 的完整签名（注意 `method=None` 是默认值，这是 4.3 节的关键）：

[_minimize.py:54-56](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L54-L56) —— `def minimize(fun, x0, args=(), method=None, jac=None, hess=None, hessp=None, bounds=None, constraints=(), tol=None, callback=None, options=None)`。13 个参数里，`method` 默认为 `None`，意味着「我不指定，请你帮我选」。

`method` 参数的文档明确写了「不指定时的默认选择规则」：

[_minimize.py:82-103](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L82-L103) —— 文档列出 15 种内置方法名，并注明：「If not given, chosen to be one of `BFGS`, `L-BFGS-B`, `SLSQP`, depending on whether or not the problem has constraints or bounds.」这就是 4.3 节要追踪的规则。

入口处的 `x0` 预处理，确保后续算法拿到的总是一个一维浮点数组：

[_minimize.py:588-597](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L588-L597) —— `x0 = np.atleast_1d(np.asarray(x0))`；若 `x0.ndim != 1` 抛 `ValueError`；若 dtype 是整型则转成 `float`；`args` 不是 `tuple` 就包成单元素 tuple。这一段保证了「无论用户传列表还是整数数组，下游算法都看到统一的 `float64` 一维数组」。

> 说明：本节只是「认参数」。`jac/hess` 的具体处理（如 `jac=True` 时用 `MemoizeJac` 拆分函数值与梯度）会在 4.3 节的校验段一并讲。

#### 4.1.4 代码实践

**实践目标**：用 `inspect.signature` 打印 `minimize` 的完整参数列表，把每个参数和上面那张表对上号。

**操作步骤**：

```python
# 文件名建议：inspect_minimize_sig.py
import inspect
from scipy.optimize import minimize

sig = inspect.signature(minimize)
for name, p in sig.parameters.items():
    print(f"{name:12s} 默认值={p.default!r:10} 位置={p.kind.description}")
```

**需要观察的现象**：打印出的 13 个参数名、默认值与上表一致；其中 `method` 默认 `None`、`constraints` 默认 `()`（空 tuple）、`bounds` 默认 `None`。

**预期结果**：参数顺序与默认值与源码 `_minimize.py:54-56` 完全一致。具体输出格式随 Python 版本略有差异，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`method` 的默认值是什么？这意味着什么？

**答案**：默认 `None`。意味着「用户不指定方法时，由调度器根据 `bounds`/`constraints` 自动选择」。这正是 4.3 节要追踪的逻辑。

**练习 2**：`constraints` 的默认值是 `()` 而不是 `None`，为什么这对默认选择很重要？

**答案**：因为 4.3 节的默认选择用 `if constraints:` 来判断「有没有约束」。空 tuple `()` 在布尔上下文里是 `False`，所以「不传 constraints」等价于「无约束」，从而走到 `BFGS` 分支。如果默认是 `None`，代码就得写成 `if constraints is not None`。

---

### 4.2 方法清单与新回调接口：MINIMIZE_METHODS / MINIMIZE_METHODS_NEW_CB

#### 4.2.1 概念说明

调度器要「按名字分流」，首先得有一张「合法名字清单」。`_minimize.py` 在文件顶部用三个常量列出了所有合法方法名。同时，其中一张清单 `MINIMIZE_METHODS_NEW_CB` 还标记了「哪些方法支持新的回调接口」——回调（callback）是每一步迭代后让用户插一脚的钩子，新接口会传给你一个 `OptimizeResult`，旧接口只传当前的 `x`。

#### 4.2.2 核心流程

```text
用户传入 method（字符串或 callable）
   │
   ├─ callable? → meth = "_custom"
   └─ 字符串?  → meth = method.lower()   ← 统一转小写后再比较
                       │
                       ▼
              进入 if/elif 链与 MINIMIZE_METHODS 里的名字逐一匹配
```

关键设计：**所有方法名比较都基于小写形式**，所以用户写 `'BFGS'`、`'bfgs'`、`'Bfgs'` 都等价。这是通过 `method.lower()` 实现的。

#### 4.2.3 源码精读

三张方法清单都在文件开头：

[_minimize.py:42-45](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L42-L45) —— `MINIMIZE_METHODS` 列出 `minimize` 支持的全部 15 种方法（小写形式），从无导数的 `nelder-mead`、`powell`，到梯度法 `cg`、`bfgs`、`newton-cg`，到有界/约束法 `l-bfgs-b`、`tnc`、`cobyla`、`cobyqa`、`slsqp`、`trust-constr`，再到信赖域族 `dogleg`、`trust-ncg`、`trust-exact`、`trust-krylov`。

[_minimize.py:47-50](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L47-L50) —— `MINIMIZE_METHODS_NEW_CB` 标注「这些方法支持新回调接口（回调收到一个 `OptimizeResult`）」。注意它比 `MINIMIZE_METHODS` 少了 `tnc`——TNC 不支持 `intermediate_result` 形式的新回调。这张清单主要被测试套件用来参数化回调相关的测试。

[_minimize.py:52](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L52) —— `MINIMIZE_SCALAR_METHODS = ['brent', 'bounded', 'golden']`，是 `minimize_scalar` 的合法方法（4.5 节用）。

「转小写再比较」的实现：

[_minimize.py:608-611](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L608-L611) —— `if callable(method): meth = "_custom" else: meth = method.lower()`。自定义可调用对象走特殊标记 `"_custom"`（4.4 节详述）；否则一律小写化。后续整条 `if/elif` 链都用小写形式匹配。

新回调接口的包装逻辑在 `_optimize.py` 里：

[_optimize.py:88-109](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L88-L109) —— `_wrap_callback(callback, method)` 用签名内省判断回调形态：若签名正好是 `{intermediate_result}`，就包装成「收到 `OptimizeResult` 再转发」的新式回调；否则按旧式 `callback(xk)` 处理。`tnc/cobyla/cobyqa` 不在这里包装（它们各自内部处理回调）。包装对象上还挂了一个 `stop_iteration` 标志，供 4.4 节的 `StopIteration` 提前终止使用。

#### 4.2.4 代码实践

**实践目标**：验证「方法名大小写不敏感」，并确认 `MINIMIZE_METHODS` 的成员数量。

**操作步骤**：

```python
# 文件名建议：method_names.py
import numpy as np
from scipy.optimize import minimize, rosen
from scipy.optimize._minimize import MINIMIZE_METHODS

print("minimize 支持的方法数:", len(MINIMIZE_METHODS))

x0 = np.array([1.3, 0.7, 0.8, 1.9, 1.2])
r1 = minimize(rosen, x0, method='BFGS')
r2 = minimize(rosen, x0, method='bfgs')   # 全小写
# 两种写法都应收敛到同一点；用 nfev 是否接近来佐证「走的是同一个算法」
print("BFGS nfev:", r1.nfev, " bfgs nfev:", r2.nfev)
print("两者最优解距离:", np.linalg.norm(r1.x - r2.x))
```

**需要观察的现象**：方法数为 15；大小写两种写法的 `nfev` 相同（或非常接近），最优解距离接近 0——说明它们被路由到了同一个 `_minimize_bfgs`。

**预期结果**：`len(MINIMIZE_METHODS) == 15`；两次求解结果一致。`nfev` 的具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`MINIMIZE_METHODS_NEW_CB` 比 `MINIMIZE_METHODS` 少了哪个方法？为什么？

**答案**：少了 `tnc`。因为 TNC 不支持「回调收到 `OptimizeResult`」的新式回调接口（见 `_wrap_callback` 对 `tnc` 不做新式包装）。

**练习 2**：为什么用户写 `'BFGS'` 和 `'bfgs'` 效果完全一样？

**答案**：因为调度器在比较前执行了 `method.lower()`（`_minimize.py:611`），统一转成小写 `'bfgs'` 再去匹配 `if/elif` 链。

---

### 4.3 method=None 的自动默认选择与参数合法性校验（核心）

#### 4.3.1 概念说明

这是本讲最重要的一节。当用户**不指定** `method`（即 `method=None`）时，`minimize` 不会报错，而是自己挑一个默认方法。挑选规则极其简单，且只看两件事：**有没有约束**、**有没有边界**。理解这条规则，你就能预测「我不写 method 时到底跑了哪个算法」。

紧随其后的是一组**参数合法性校验**：调度器会检查你传的 `jac/hess/constraints/bounds` 是否与所选方法「兼容」，不兼容就发一条 `RuntimeWarning`（注意：是警告，不是报错，程序照跑）。

#### 4.3.2 核心流程

默认选择只有三行逻辑，但**优先级**很关键：

```text
if method is None:               # 用户没指定方法
    if constraints:              # ① 优先看约束
        method = 'SLSQP'
    elif bounds is not None:     # ② 再看边界
        method = 'L-BFGS-B'
    else:                        # ③ 都没有
        method = 'BFGS'
```

优先级含义：**约束 > 边界 > 无约束**。

- 有约束 → 一律 `SLSQP`（哪怕同时也有 bounds，因为 SLSQP 本身也能处理边界）。
- 只有边界、无约束 → `L-BFGS-B`（带边界的有限内存拟牛顿）。
- 既无约束也无边界 → `BFGS`（经典无约束拟牛顿，也是文档里写的「默认方法」）。

选完方法后，进入一组「与方法是否匹配」的校验，例如「Nelder-Mead 不用梯度，你却传了 `jac`」「这个方法不能处理约束，你却传了 `constraints`」等，每条不匹配都发一条 `RuntimeWarning`。

#### 4.3.3 源码精读

默认选择的三行核心代码：

[_minimize.py:599-606](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L599-L606) —— `if method is None:` 后依次判断 `constraints`（非空→`SLSQP`）、`bounds is not None`（→`L-BFGS-B`）、否则 `BFGS`。注意判断顺序决定了上面说的优先级。这与文档 `_minimize.py:102-103` 的承诺完全一致。

紧跟着是一长串校验警告。挑几个有代表性的：

[_minimize.py:617-619](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L617-L619) —— 若方法是无导数方法（`nelder-mead/powell/cobyla/cobyqa`）却传了 `jac`，发警告「does not use gradient information (jac)」。

[_minimize.py:633-636](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L633-L636) —— 若方法不在 `{cobyla, cobyqa, slsqp, trust-constr}` 之列却传了 `constraints`，发警告「cannot handle constraints」。

[_minimize.py:643-646](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L643-L646) —— `l-bfgs-b/tnc/cobyla/cobyqa/slsqp` 不支持 `return_all` 选项，传了会发警告。**这条在后面的实践里会当作「方法指纹」来用**——警告文本里就含方法名。

`jac` 参数的处理（其中 `jac=True` 表示「函数同时返回值和梯度」，需要拆开）：

[_minimize.py:649-667](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L649-L667) —— `jac is True` 时，用 `MemoizeJac(fun)` 包装目标函数：调用 `fun(x)` 得到 `(f, g)`，缓存下来，使得「取函数值」和「取梯度」共用同一次求值、不重复计算。`jac in FD_METHODS`（即 `'2-point'/'3-point'/'cs'`）则交给具体方法用相对步长有限差分；其余情况置 `None`（方法内部会用绝对步长前向差分）。

`MemoizeJac` 的实现很简单但很关键：

[_optimize.py:61-85](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L61-L85) —— `MemoizeJac` 把「返回 `(fun, grad)` 的函数」拆成两个入口：`__call__` 返回函数值、`derivative` 返回梯度；两者都先经过 `_compute_if_needed`，只有当 `x` 变了才真正调用一次原函数。这就是「函数值和梯度共享一次求值」的缓存机制。

`tol` 到各方法私有容差的映射：

[_minimize.py:670-689](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L670-L689) —— 用户传一个总的 `tol`，调度器按方法把它翻译成对应的私有容差：如 `bfgs/cg/...` 用 `gtol`、`powell/l-bfgs-b/tnc/slsqp` 用 `ftol`、`nelder-mead` 用 `xatol/fatol`、`trust-constr` 同时设 `xtol/gtol/barrier_tol`。这解释了「为什么只传一个 `tol` 就够用」。

#### 4.3.4 代码实践

**实践目标**：这是本讲指定的核心实践——**追踪并验证 `method=None` 时的默认选择规则**，对同一个 `rosen` 分别构造「无约束 / 有 bounds / 有 constraints」三种情形，打印**实际被选中的方法**。

由于 `OptimizeResult` 本身并不携带「方法名」字段，我们用一个小技巧：**临时把调度器内部引用的 `_minimize_*` 函数替换成「会记录自己名字」的包装**。因为 `minimize()` 在运行时按模块全局名字查找这些函数（见前置知识第 5 条），替换模块属性就能生效。

```python
# 文件名建议：trace_default_method.py
# 示例代码：用轻量 monkeypatch 观察 minimize(method=None) 实际选中了哪个算法
import numpy as np
import scipy.optimize as opt
import scipy.optimize._minimize as _m   # 调度器所在的模块

rosen = opt.rosen
x0 = np.array([1.3, 0.7, 0.8, 1.9, 1.2])

# 1) 「期望默认方法」→「调度器里实际调用的函数属性名」
targets = [
    ("BFGS",     "_minimize_bfgs"),    # 无约束时的默认
    ("L-BFGS-B", "_minimize_lbfgsb"),  # 有 bounds 时的默认
    ("SLSQP",    "_minimize_slsqp"),   # 有 constraints 时的默认
]

selected = {}
def _spy(label, real):                 # 用形参绑定 label，避免闭包晚绑定陷阱
    def wrapper(*a, **kw):
        selected["method"] = label
        return real(*a, **kw)
    return wrapper

# 2) 备份原始函数，再换成 spy 包装
backup = {attr: getattr(_m, attr) for _, attr in targets}
for label, attr in targets:
    setattr(_m, attr, _spy(label, backup[attr]))

def run(case, **kwargs):
    selected.clear()
    opt.minimize(rosen, x0, **kwargs)   # 故意不传 method → 触发默认选择
    print(f"{case:14s} → 实际选中: {selected.get('method')}")

try:
    run("无约束")
    run("有 bounds",     bounds=[(0, 2)] * 5)
    run("有 constraints",
         constraints=[{"type": "ineq", "fun": lambda x: x[0] - 0.5}])
finally:
    for attr, real in backup.items():   # 3) 无论成败都还原，避免污染后续调用
        setattr(_m, attr, real)
```

**需要观察的现象**：三行输出分别是 `BFGS`、`L-BFGS-B`、`SLSQP`，与 `_minimize.py:599-606` 的选择逻辑一一对应。

**预期结果**：

```text
无约束          → 实际选中: BFGS
有 bounds       → 实际选中: L-BFGS-B
有 constraints  → 实际选中: SLSQP
```

> 不想用 monkeypatch 也有更轻的「行为指纹」法：对后两种情形额外传 `options={'return_all': True}`，会触发 `_minimize.py:643-646` 的警告，警告文本里就含方法名（如 `Method L-BFGS-B does not support the return_all option.`）。无约束的 BFGS 支持 `return_all`，不会有此警告——这恰好印证它不是那几个受限方法。

#### 4.3.5 小练习与答案

**练习 1**：如果同时传了 `bounds` 和 `constraints`，但不指定 `method`，会选哪个？为什么？

**答案**：选 `SLSQP`。因为默认选择的判断顺序是 `constraints` 优先于 `bounds`（`_minimize.py:601-604` 先判 `if constraints`）。而 SLSQP 本身能同时处理边界和约束，所以这个选择是合理的。

**练习 2**：你给 `minimize(rosen, x0, method='Nelder-Mead', jac=rosen_der)`，会发生什么？程序会崩溃吗？

**答案**：不会崩溃。Nelder-Mead 是无导数方法，调度器在 `_minimize.py:617-619` 检测到 `bool(jac)` 为真，发一条 `RuntimeWarning`（「does not use gradient information」），然后照常运行——你传的梯度被忽略了。

**练习 3**：为什么传一个总的 `tol=1e-6` 就能同时控制不同方法的收敛？

**答案**：因为 `_minimize.py:670-689` 把 `tol` 按方法翻译成了各自的私有容差（BFGS→`gtol`、SLSQP→`ftol`、trust-constr→`xtol/gtol/barrier_tol` 等）。每个方法真正读取的是它自己认识的那个容差键。

---

### 4.4 方法分发 if/elif 链与自定义方法（custom callable）

#### 4.4.1 概念说明

选好（或用户指定好）方法、做完所有校验和预处理之后，调度器来到最后一步：**真正调用对应的 `_minimize_*` 函数**。这一步是一条很长的 `if meth == '...' / elif ...` 链，每个分支调用一个具体的算法实现。此外，`method` 还可以是一个**用户自己写的可调用对象**（custom），这让你能把 `minimize` 当作前端，接入第三方算法。

#### 4.4.2 核心流程

```text
meth (= method.lower() 或 "_custom")
   │
   ├─ meth == "_custom"? → 直接调用 method(fun, x0, args=..., jac=..., **options) 并返回
   │                        （在 bounds/constraints 标准化之前，原样透传）
   ├─ standardize_constraints(...) / standardize_bounds(...)
   ├─ _wrap_callback(callback, meth)
   └─ if meth == 'nelder-mead':  res = _minimize_neldermead(...)
      elif meth == 'powell':     res = _minimize_powell(...)
      elif meth == 'cg':         res = _minimize_cg(...)
      elif meth == 'bfgs':       res = _minimize_bfgs(fun, x0, args, jac, callback, **options)
      elif ...                   （其余方法同理，各自传它需要的参数）
      else:                      raise ValueError('Unknown solver ...')
```

注意一个细节：每个分支传给 `_minimize_*` 的参数组合是**量身定制**的——无导数方法不传 `jac`，牛顿类方法才传 `hess/hessp`，有界/约束方法才传 `bounds/constraints`。这也是为什么前面要做那么多校验。

#### 4.4.3 源码精读

自定义方法的分发，发生在 bounds/constraints 标准化**之前**：

[_minimize.py:691-697](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L691-L697) —— `if meth == '_custom':` 直接 `return method(fun, x0, args=args, jac=jac, hess=hess, hessp=hessp, bounds=bounds, constraints=constraints, callback=callback, **options)`。注释说明：custom 方法在 bounds/constraints 被标准化之前就被调用，因此它要能接受用户原始传入的任何 bounds/constraints 形式。`options` 字典的内容会被逐键展开成关键字参数传进去。

内置方法的分发主链（节选关键分支）：

[_minimize.py:771-815](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L771-L815) —— 从 `if meth == 'nelder-mead'` 一路 `elif` 到 `trust-exact`，每个分支调用对应的 `_minimize_*`。注意参数差异：`bfgs` 分支是 `_minimize_bfgs(fun, x0, args, jac, callback, **options)`（要梯度，不要海森/边界）；`slsqp` 分支是 `_minimize_slsqp(fun, x0, args, jac, bounds, constraints, callback=callback, **options)`（要边界和约束）；`trust-constr` 分支则把 `jac/hess/hessp/bounds/constraints` 全传上。链的末尾 `else: raise ValueError(f'Unknown solver {method}')` 兜住所有拼写错误的方法名。

分发完成后的收尾处理：

[_minimize.py:817-828](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L817-L828) —— 若之前为了处理「固定变量」而移除过某些变量（`remove_vars`，见 `_minimize.py:701-766`），这里把解出来的 `x` 和 `jac` 按原位置补回去；若回调抛过 `StopIteration`（通过包装对象上的 `stop_iteration` 标志），则把 `success` 置为 `False`、`status=99`、`message` 设为「`callback` raised `StopIteration`」。这就是「在回调里 `raise StopIteration` 可以提前优雅终止优化」的实现机制。

#### 4.4.4 代码实践

**实践目标**：体验「自定义方法」——把一个你自己写的最简求解器当作 `method` 传给 `minimize`，观察它如何被调用、收到哪些参数。

**操作步骤**：

```python
# 文件名建议：custom_method.py
# 示例代码：用自定义可调用对象当 method，打印它收到的参数
import numpy as np
from scipy.optimize import minimize, rosen, OptimizeResult

def my_solver(fun, x0, args=(), **kwargs):
    # 自定义方法必须：接受任意 kwargs、返回一个 OptimizeResult
    print("  my_solver 收到的关键字参数:", sorted(kwargs.keys()))
    # 最笨但最直观的「求解」：直接返回初值处的函数值
    x_best = np.asarray(x0, dtype=float)
    return OptimizeResult(x=x_best, fun=float(fun(x_best, *args)),
                          success=True, message="custom: did nothing smart",
                          nfev=1, nit=0)

x0 = np.array([1.3, 0.7, 0.8, 1.9, 1.2])
res = minimize(rosen, x0, method=my_solver, options={'foo': 42})
print("返回的 message:", res.message)
print("options 是否被展开:", 'foo' in my_solver.__code__.co_consts or True)  # 占位
```

**需要观察的现象**：`my_solver` 被调用，打印出的关键字参数里应包含 `jac/hess/hessp/bounds/constraints/callback` 以及被展开的 `foo`（来自 `options`）；最终 `res.message` 是你自定义的字符串。

**预期结果**：`minimize` 把 `options` 字典的内容逐键作为关键字参数传给了 `my_solver`（对应 `_minimize.py:695-697` 的 `**options`）。`options` 里的具体键名集合**待本地验证**（随 SciPy 版本可能增减）。

#### 4.4.5 小练习与答案

**练习 1**：为什么自定义方法的分支（`_custom`）在 `standardize_bounds` / `standardize_constraints` 之前就 `return` 了？

**答案**：因为自定义方法可能有自己的 bounds/constraints 表示约定，调度器不应擅自把它们改写成 SciPy 内部格式。所以 `_minimize.py:691-697` 在标准化之前就把原始参数原样透传给自定义方法，并由它自己负责处理。

**练习 2**：如果用户把方法名拼错成 `'BFGSS'`，会发生什么？

**答案**：`'BFGSS'.lower()` = `'bfgss'`，在整条 `if/elif` 链里都匹配不上，最后落到 `else: raise ValueError(f'Unknown solver BFGSS')`（`_minimize.py:814-815`）。

**练习 3**：怎样在回调里提前终止优化？

**答案**：在新式回调 `def cb(intermediate_result): ...` 里 `raise StopIteration`。`_wrap_callback` 包装对象会记录 `stop_iteration=True`，分发后的收尾代码（`_minimize.py:823-826`）据此把 `success` 置 `False`、`status=99`。

---

### 4.5 minimize_scalar：一维最小化的并行入口

#### 4.5.1 概念说明

`minimize` 处理的是多元函数 \(f(\mathbf{x})\)；如果你的目标函数只有一个标量变量 \(f(x)\)，就该用 `minimize_scalar`。它是同一套「调度」思路的简化版：方法只有三种（`brent / bounded / golden`），同样支持 `method=None` 自动选择，同样支持自定义方法。算法细节（Brent 的抛物线插值、黄金分割等）留给单元 u2 详讲，本节只看它的「调度层」。

#### 4.5.2 核心流程

```text
minimize_scalar(fun, bracket, bounds, args, method, tol, options)
   │
   ├─ method 是 callable?       → meth = "_custom"
   ├─ method is None?           → meth = 'brent' if bounds is None else 'bounded'
   └─ 否则                      → meth = method.lower()
   │
   ├─ 若给了 bounds 又选了 brent/golden → raise ValueError（互斥）
   └─ 分发：
      _custom  → method(fun, args=..., bracket=..., bounds=..., **options)
      brent    → _minimize_scalar_brent(...)（外包 _recover_from_bracket_error）
      bounded  → _minimize_scalar_bounded(...)（必须有 bounds）
      golden   → _minimize_scalar_golden(...)（外包 _recover_from_bracket_error）
      else     → raise ValueError
```

注意它的默认规则和 `minimize` 略有不同：**有没有 `bounds` 决定用 `bounded` 还是无界的 `brent`**——没有「约束」这一维。

#### 4.5.3 源码精读

签名与默认选择：

[_minimize.py:831-832](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L831-L832) —— `def minimize_scalar(fun, bracket=None, bounds=None, args=(), method=None, tol=None, options=None)`。注意它多了 `bracket`（括号区间，给 brent/golden 用），且 `bounds` 给 `bounded` 方法用。

[_minimize.py:988-993](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L988-L993) —— `if callable(method): meth = "_custom"`；`elif method is None: meth = 'brent' if bounds is None else 'bounded'`；`else: meth = method.lower()`。这就是「有 bounds 用 bounded、否则用 brent」的默认规则。

bounds 与 brent/golden 互斥的校验：

[_minimize.py:997-999](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L997-L999) —— 若既传了 `bounds` 又显式选了 `brent/golden`，直接 `raise ValueError`（「incompatible」）。注意这是**报错**而非警告，因为 brent/golden 根本不支持有界搜索。

分发主链：

[_minimize.py:1018-1032](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L1018-L1032) —— `_custom` 直接调用；`brent` 与 `golden` 都用 `_recover_from_bracket_error(...)` 包了一层（括号失败时回退，详见 u2-l1）；`bounded` 必须有 `bounds`，否则 `raise ValueError`；末尾 `else: raise ValueError(f'Unknown solver {method}')`。

返回前的形状归一化：

[_minimize.py:1037-1038](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L1037-L1038) —— `res.fun = np.asarray(res.fun)[()]`、`res.x = np.reshape(res.x, res.fun.shape)[()]`。注释提到这是为修复 `res.x` 输出形状不一致问题（gh-16196），并为将来函数向量化预留——保证 `res.x` 的形状与 `res.fun` 对齐。

#### 4.5.4 代码实践

**实践目标**：用 `minimize_scalar` 的默认规则分别求解「无界」和「有界」两种情形，观察默认方法的不同。

**操作步骤**：

```python
# 文件名建议：minimize_scalar_default.py
from scipy.optimize import minimize_scalar

def f(x):
    return (x - 2) * x * (x + 2) ** 2

# 1) 不传 bounds、不传 method → 默认 brent
r1 = minimize_scalar(f)
print("无界默认  x=%.6f  fun=%.6f" % (r1.x, r1.fun))

# 2) 传 bounds、不传 method → 默认 bounded
r2 = minimize_scalar(f, bounds=(-3, -1))
print("有界默认  x=%.6f  fun=%.6f" % (r2.x, r2.fun))

# 3) 显式给 brent 却又给 bounds → 应报 ValueError
try:
    minimize_scalar(f, bounds=(-3, -1), method='brent')
except ValueError as e:
    print("捕获 ValueError:", e)
```

**需要观察的现象**：第 1 步在 \(x\approx 1.28\) 附近找到无界局部最小；第 2 步在区间 \((-3,-1)\) 内找到最小（约 \(x=-2\)）；第 3 步抛 `ValueError`，信息含「incompatible」。

**预期结果**：与文档示例（`_minimize.py:956-983`）给出的数值一致；第 3 步抛错。具体浮点末位**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`minimize_scalar` 在 `method=None` 时的默认规则和 `minimize` 有什么不同？

**答案**：`minimize_scalar` 只看 `bounds`：有 bounds → `bounded`，无 bounds → `brent`。它没有「约束」概念，所以不存在 `minimize` 那套「约束 > 边界 > 无约束」的三级优先（`_minimize.py:990-991`）。

**练习 2**：为什么 `brent` 方法外面要包一层 `_recover_from_bracket_error`？

**答案**：brent 需要一个三点「括号」区间才能开始，而自动找括号可能失败。`_recover_from_bracket_error` 在括号失败时做回退处理，避免直接抛异常中断。这个机制的细节在 u2-l1 详讲。

---

## 5. 综合实践

把本讲五个模块串成一个「调度器观察站」脚本：既追踪 `minimize` 的默认选择（4.3），又跑一次自定义方法（4.4），再用 `minimize_scalar` 对比（4.5），最后用 `show_options`（u1-l1 学过）查阅被选中方法的选项。

```python
# 文件名建议：u1_l3_dispatcher_lab.py
import numpy as np
import scipy.optimize as opt
import scipy.optimize._minimize as _m

rosen = opt.rosen
x0 = np.array([1.3, 0.7, 0.8, 1.9, 1.2])

# ===== A. 追踪 minimize(method=None) 的默认选择（4.3）=====
targets = [("BFGS", "_minimize_bfgs"),
           ("L-BFGS-B", "_minimize_lbfgsb"),
           ("SLSQP", "_minimize_slsqp")]
selected = {}
def _spy(label, real):
    def w(*a, **kw):
        selected["method"] = label
        return real(*a, **kw)
    return w
backup = {a: getattr(_m, a) for _, a in targets}
for lab, a in targets:
    setattr(_m, a, _spy(lab, backup[a]))
try:
    for case, kw in [("无约束", {}),
                     ("有 bounds", {"bounds": [(0, 2)] * 5}),
                     ("有 constraints",
                      {"constraints": [{"type": "ineq",
                                        "fun": lambda x: x[0] - 0.5}]})]:
        selected.clear()
        opt.minimize(rosen, x0, **kw)
        print(f"[minimize   ] {case:16s} → {selected.get('method')}")
finally:
    for a, real in backup.items():
        setattr(_m, a, real)

# ===== B. 自定义方法（4.4）=====
from scipy.optimize import OptimizeResult
def echo(fun, x0, args=(), **kw):
    print(f"[custom     ] 收到 kwargs: {sorted(kw.keys())}")
    xb = np.asarray(x0, float)
    return OptimizeResult(x=xb, fun=float(fun(xb, *args)), success=True,
                          message="custom ok", nfev=1, nit=0)
opt.minimize(rosen, x0, method=echo, options={'maxiter': 7})

# ===== C. minimize_scalar 默认规则（4.5）=====
def f(x):
    return (x - 2) * x * (x + 2) ** 2
print("[scalar     ] 无界默认 fun = %.4f" % opt.minimize_scalar(f).fun)
print("[scalar     ] 有界默认 fun = %.4f"
      % opt.minimize_scalar(f, bounds=(-3, -1)).fun)

# ===== D. 查阅被选中默认方法的选项（衔接 u1-l1）=====
print("\n[BFGS options]")
print(opt.show_options('minimize', 'BFGS', disp=False))
```

**完成标准**：

1. A 段三行依次打印 `BFGS / L-BFGS-B / SLSQP`，与 `_minimize.py:599-606` 一致。
2. B 段打印的 kwargs 里能看到 `jac/hess/hessp/bounds/constraints/callback` 以及被展开的 `maxiter`。
3. C 段无界 `fun` 约 `-9.91`，有界 `fun` 约 `0`（区间内最小在 \(x=-2\)）。
4. D 段打印出 BFGS 的 options 文档（含 `gtol/maxiter` 等）。
5. 你能用自己的话说清：「`minimize` 不做计算，只按 `method` 字符串（或默认规则）把请求派发给某个 `_minimize_*`，再统一返回 `OptimizeResult`。」

> 注：A 段 monkeypatch 必须在 `finally` 里还原，否则会污染同进程后续的优化调用。各 `nfev/nit` 数值**待本地验证**。

## 6. 本讲小结

- `minimize` 是 `scipy.optimize` 的「导诊台」：本身不做优化，只负责收参数 + 派发到某个 `_minimize_*`。完整 13 个参数见 [`_minimize.py:54-56`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L54-L56)。
- 合法方法清单写在 [`MINIMIZE_METHODS`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L42-L45)；所有方法名比较前都 `method.lower()`，所以大小写不敏感（`_minimize.py:611`）。`MINIMIZE_METHODS_NEW_CB` 标注支持新回调接口的方法（少了 `tnc`）。
- **核心**：`method=None` 时按「约束 > 边界 > 无约束」选默认——有 `constraints` → `SLSQP`，仅有 `bounds` → `L-BFGS-B`，都没有 → `BFGS`（`_minimize.py:599-606`）。可用 monkeypatch 实测验证。
- 选完方法后有一组「参数与方法是否匹配」的校验，不匹配只发 `RuntimeWarning` 不报错（如 `_minimize.py:617-646`）；`tol` 会被按方法翻译成各自私有容差（`_minimize.py:670-689`）。
- 真正的派发是一条 [`if/elif` 链](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L771-L815)，每个分支按需传参；`method` 还可以是自定义可调用对象（`_custom`，`_minimize.py:691-697`），让你接入第三方算法。
- `minimize_scalar` 是一维简化版：默认「有 bounds → bounded、否则 brent」（`_minimize.py:990-991`），且 bounds 与 brent/golden 互斥会直接报错（`_minimize.py:997-999`）。

## 7. 下一步学习建议

本讲你掌握了「调度层」。接下来可以沿两条线深入：

- **想看具体算法怎么实现**：单元 u2 从最简单的 `minimize_scalar` 三方法（brent/bounded/golden）和一维求根（`root_scalar`）入门，门槛最低；本讲 4.5 提到的 `_recover_from_bracket_error`、`bracket` 机制会在 **u2-l1（标量函数最小化）** 里详讲。
- **想继续沿多元优化主线走**：单元 u3 讲「导数近似与函数封装基础设施」（`approx_derivative`、`ScalarFunction/VectorFunction`、BFGS/SR1 拟牛顿更新），这是理解 BFGS、trust-constr 等被本讲调度器派发出去的算法的必备前置。
- **想立刻摸到一个被调度的算法**：可直接读 [`_optimize.py` 里的 `_minimize_bfgs`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py)（本讲默认方法），对照本讲的分发链，确认「`minimize(method='BFGS')` 这一行最终落进了哪个函数」。

继续阅读建议源码：把本讲的 [`_minimize.py:599-606`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L599-L606)（默认选择）和 [`_minimize.py:771-815`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L771-L815)（分发链）反复对照，直到你能闭眼复述「一行 `minimize(...)` 的完整旅程」。

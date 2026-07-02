# jacobian 的实现

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `jacobian` 对用户函数 `f` 的**向量化签名与维度约定**：`f` 接受什么形状、返回什么形状、最终 `res.df` 为什么是 `(n, m)`。
- 看懂 `jacobian` 内部那个精巧的 **`wrapped` 函数**：它如何用一个「对角扰动矩阵」把「对向量函数求雅可比」转化成 `derivative` 已经擅长的「逐元素标量求导」。
- 解释为什么这里**必须**用 `preserve_shape=True` 委托给 `derivative`，以及结尾 `del res.x` 的原因。
- 能够独立用 `jacobian` 计算标量函数（梯度）与向量值函数的雅可比，并验证结果形状与数值。

本讲是「组合实现」单元的第一篇：我们不再剖析 `derivative` 的内部零件（u2 系列已完成），而是站在 `derivative` 这个已经可靠的「黑盒」之上，看 `jacobian` 如何**复用**它来处理多元函数。

## 2. 前置知识

在进入本讲前，请确认你已理解以下概念（它们在前序讲义中讲过）：

- **雅可比矩阵（Jacobian）**：设 \(f:\mathbf{R}^m \rightarrow \mathbf{R}^n\)，其在点 \(x\) 处的雅可比是一个 \(n \times m\) 矩阵 \(J\)，其中第 \((i, j)\) 个元素为偏导数 \(\partial f_i / \partial x_j\)。当 \(n=1\)（标量函数）时，雅可比退化为长度 \(m\) 的**梯度**。
- **`derivative` 的逐元素语义**（u1-l2、u2-l6）：`derivative(f, x)` 把 `x` 的每个元素当成一个**独立的标量求导问题**，逐元素估计一阶导，返回的 `df` 形状与 `f` 输出形状一致。
- **`preserve_shape` 两种模式**（u1-l2、u2-l6）：`False` 时框架可自由重塑/压缩传给 `f` 的数组；`True` 时 `f` 总是收到形状 `shape` 或 `shape + (n,)` 的完整数组，且 `f` 的输出形状可以大于 `x` 的形状（框架会把 `shape` 广播到「输入形状与输出形状的并集」）。
- **`eim._loop` 的钩子机制**（u2-l6）：`derivative` 通过 `pre_func_eval`、`post_func_eval`、`check_termination` 等钩子接入通用迭代框架；`preserve_shape=True` 还会关闭「已收敛元素的压缩」。

一句话直觉：**`jacobian` 不是重新发明一套多元有限差分，而是把多元问题「翻译」成一堆一元问题，再交给 `derivative` 求解。** 这个翻译的关键就是 `wrapped` 里的对角扰动矩阵。

## 3. 本讲源码地图

本讲几乎全部聚焦在单个文件里的一段代码：

| 文件 | 作用 |
| --- | --- |
| [`scipy/differentiate/_differentiate.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py) | `jacobian` 的全部实现（签名、docstring 的维度约定、`wrapped`、委托 `derivative`、结果清理）都在这里。 |
| `scipy/_lib/_elementwise_iterative_method.py` | `_initialize` 中 `preserve_shape=True` 分支决定了「输入 `shape` 如何被广播成 `(n, m)`」，是理解结果形状的关键旁证。 |

本讲不展开 `derivative` 内部的差分权重、stencil、终止条件等细节（已在 u2 讲过），而是把 `derivative` 当作一个可信的标量求导黑盒来使用。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **f 的向量化签名与维度约定**（用户视角的契约）
2. **wrapped 扰动注入**（对角扰动矩阵的构造，核心技巧）
3. **委托 derivative 与结果形状**（`preserve_shape=True` 的作用与 `del res.x`）

---

### 4.1 f 的向量化签名与维度约定

#### 4.1.1 概念说明

`derivative` 处理的是**一元**函数 \(f:\mathbf{R}\rightarrow\mathbf{R}\)（逐元素）。而 `jacobian` 要处理**多元**函数 \(f:\mathbf{R}^m\rightarrow\mathbf{R}^n\)。要让有限差分高效，`jacobian` 要求用户把 `f` 写成**向量化**形式：一次调用就能同时计算很多个点的函数值，而不是用 Python 循环一个点一个点算。

关键约定（摘自 docstring 的 Notes）：

- `x` 是形状 `(m,)` 的数组（单个点），或 `(m, k)`（同时算 `k` 个点）。
- `f` 必须接受形状 `(m, ...)` 的输入，其中 `...` 是「用于一次性计算多个点的任意额外维度」。
- `f` 必须返回形状 `(n, ...)` 的输出，其中 `n` 是输出维度（函数有几个分量）。
- 结果 `res.df` 的形状是 `(n, m)`（单点）或 `(n, m, k)`（多点）。

一个特殊情况：当 `n=1`（标量函数，如 Rosenbrock），`f` 返回的并不是 `(1, ...)`，而是直接返回 `(...)`。此时 `res.df` 是 `(m,)`（梯度），而不是 `(1, m)`。这一点在「代码实践」中会明确观察到。

#### 4.1.2 核心流程

用伪代码描述维度流转（单点情形，`x` 形状 `(m,)`）：

```
x  shape (m,)
   │  jacobian 调用 derivative(wrapped, x, preserve_shape=True)
   ▼
wrapped(x)           # derivative 扰动 x 后调用 wrapped
   │  内部构造对角扰动矩阵 xph，形状 (m, m) 或 (m, m, n_abs)
   ▼
f(xph)  shape (n, m)   # f 向量化：对 xph 的每一列求一次 f
   │  derivative 对 wrapped 的输出逐元素求导
   ▼
res.df  shape (n, m)   # 即雅可比矩阵
```

多点情形只需把 `k` 这一轴插到 `m` 之后：`x` 是 `(m, k)`，`f` 收到 `(m, k, ...)` 返回 `(n, k, ...)`，`res.df` 是 `(n, m, k)`。

#### 4.1.3 源码精读

维度约定的「法定文本」就是 docstring 的 Notes 段落：

[`_differentiate.py`:L815-L837](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L815-L837) —— `jacobian` 的 Notes，明文规定 `x`、`f` 输入、`f` 输出、`res.df` 四者的形状对应关系。其中第 823-831 行给出单点约定，第 833-837 行给出多点（`k` 个点）约定。

对应到函数签名：

[`_differentiate.py`:L723-L724](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L723-L724) —— `jacobian` 的签名。注意它与 `derivative` 的两点重要差异：

- **没有 `args` / `kwargs`**：`jacobian` 只接受 `f` 和 `x`，外加 `tolerances`、`maxiter`、`order`、`initial_step`、`step_factor`、`step_direction`。如果 `f` 需要额外参数，docstring 建议用 `functools.partial` 或 `lambda` 把参数包进去再传入。
- **没有 `preserve_shape` / `callback`**：`preserve_shape` 在内部被硬编码为 `True`（见 4.3），用户不能改；`callback` 不暴露。

签名上方还有一个装饰器：

[`_differentiate.py`:L721-L722](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L721-L722) —— `@xp_capabilities(...)` 声明跨后端能力（跳过 `array_api_strict` 和 `dask.array`、关闭 `jax_jit`）。这部分属于 u4-l4 的主题，本讲只需知道它存在。

#### 4.1.4 代码实践

**实践目标**：亲手验证「标量函数的雅可比 = 梯度，形状为 `(m,)`」以及「`f` 必须向量化」这一约定。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.differentiate import jacobian
from scipy.optimize import rosen, rosen_der

m = 3
x = np.full(m, 0.5)          # [0.5, 0.5, 0.5]
res = jacobian(rosen, x)     # rosen 接受 (m, ...) 返回 (...)，是标量函数
ref = rosen_der(x)           # 解析梯度作为参照

print("res.df.shape =", res.df.shape)
print("res.df       =", res.df)
print("ref (rosen_der) =", ref)
```

**需要观察的现象**：

- `res.df.shape` 应为 `(3,)`，**不是** `(1, 3)`——因为 `rosen` 是标量输出（`n=1` 被自然挤掉）。
- `res.df` 应接近 `[-51., -1., 50.]`，与 `rosen_der(x)` 一致（这是 docstring 给出的参照值）。

**预期结果**：`res.df` 与 `rosen_der(x)` 数值一致，`res.success` 全为 `True`。本结果已由 docstring 示例（`res.df, ref` 同时打印为 `array([-51., -1., 50.])`）佐证；若本地运行出现发散，可加大 `initial_step` 或调小 `rtol` 重试。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `rosen` 换成一个**非向量化**的函数 `f_bad(x)`（它只能接受 `(m,)`、不能处理额外维度），`jacobian` 会怎样？

**参考答案**：会在内部某次调用时报错（通常是 `xph` 多了一个维度后，`f_bad` 无法处理，抛出形状或索引错误）。解决办法按 docstring 第 839-844 行：用 `np.apply_along_axis(f_bad, axis=0, arr=x)` 包一层，使其满足 `(m, ...) -> (n, ...)` 的契约。

**练习 2**：`x` 传一个 0 维标量（如 `x = np.array(1.0)`）会发生什么？

**参考答案**：触发 `ValueError("Argument `x` must be at least 1-D.")`。因为雅可比本质上是多元概念，`m` 至少为 1，`x` 必须至少一维。

---

### 4.2 wrapped 扰动注入

#### 4.2.1 概念说明

这是 `jacobian` 最巧妙的部分。问题在于：`derivative` 只会做**逐元素标量求导**，它把输入 `x` 的每个元素当成互不相关的一维变量；而雅可比需要的是「扰动第 `j` 个坐标，观察所有 `n` 个输出的变化」。

`jacobian` 的解法是写一个 `wrapped(x)` 包装函数，它接收 `derivative` 传来的「被扰动的坐标向量 `x`」，构造一个 \(m \times m\) 的**对角扰动矩阵** `xph`，再交给真正的 `f` 求值。这个矩阵的几何含义是：

- 第 `j` **列** = 基准点 `x0`，但**只把第 `j` 个坐标替换成被扰动的值 `x[j]`**，其余坐标保持基准 `x0` 不变。
- 于是 `f(xph)[:, j]`（对第 `j` 列求 `f`）就是「只扰动坐标 `j` 时」的函数值。
- `derivative` 对 `wrapped` 的输出逐元素求导时，位置 `[i, j]` 恰好给出 \(\partial f_i / \partial x_j\)——正好是雅可比的第 `(i, j)` 个元素。

用数学语言：记第 `j` 个单位向量为 \(e_j\)，基准点为 \(x_0\)，则对角扰动矩阵的第 `j` 列为 \(x_0 + (x[j] - x_0[j])\,e_j\)。当 `derivative` 只对 `x[j]` 施加步长 \(h\)（其余坐标不动）时，该列变为 \(x_0 + h\,e_j\)，于是

\[
\frac{\partial f_i}{\partial x_j}(x_0) \;=\; \lim_{h\to 0}\frac{f_i(x_0 + h e_j) - f_i(x_0)}{h},
\]

这正是 `derivative` 在位置 `[i, j]` 估计的量。这样，一个 \(n\times m\) 的雅可比就被拆成了 \(n\cdot m\) 个独立的一元差分，全部交给 `derivative` 一次向量化完成。

> 说明：示例代码中的 \(e_j\)、\(x_0\)、\(h\) 是为解释概念而引入的记号，并非项目源码变量名；源码里对应的变量是 `xph`（扰动矩阵）、`x0`（基准点）、`x`（被 `derivative` 扰动后的坐标）。

#### 4.2.2 核心流程

`wrapped(x)` 的执行步骤（伪代码）：

```
def wrapped(x):
    # x 是 derivative 传来的「被扰动的坐标向量」
    # 形状要么是 (m,)（单个扰动点），要么是 (m, n_abs)（同时 n_abs 个扰动点）
    1. p = ()  或  (n_abs,)          # 由 x.ndim 与基准 x0.ndim 比较得出
    2. new_shape = (m, m) + x0.shape[1:] + p
    3. xph = 把 x0 扩维并广播成 new_shape   # 每一列都等于 x0（基准）
    4. xph[对角线 i, i] = x           # 用被扰动的 x 覆盖对角线
    5. return f(xph)                 # f 向量化求值，得到 (n, m[, n_abs])
```

关键在第 3、4 步：先让整个矩阵「全是基准 `x0`」，再只在对角线上写入扰动值。因为只有对角线上的 `xph[j, j]` 用到了被扰动的 `x[j]`，所以第 `j` 列里只有第 `j` 行被改动——这正是「只扰动坐标 `j`」的实现方式。

#### 4.2.3 源码精读

整个 `wrapped` 函数只有 10 行：

[`_differentiate.py`:L931-L940](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L931-L940) —— `wrapped` 的全部实现。逐行说明：

- **L932** `p = () if x.ndim == x0.ndim else (x.shape[-1],)`：判断 `derivative` 这次是不是一次送来多个扰动点（即嵌套 stencil 的多个横坐标）。若 `x` 比 `x0` 多一维，说明最后一维是「扰动点数」`n_abs`，记入 `p`；否则 `p` 为空。
- **L934** `new_shape = (m, m) + x0.shape[1:] + p`：目标扰动矩阵的形状。`(m, m)` 是核心的对角矩阵骨架；`x0.shape[1:]` 保留多点求雅可比时的 `k` 轴；`p` 是扰动点轴。
- **L935** `xph = xp.expand_dims(x0, axis=1)`：把 `(m, ...)` 的 `x0` 插一个轴变成 `(m, 1, ...)`，为广播成 `(m, m, ...)` 做准备——广播后每一列都复制了完整的 `x0`。
- **L936-L937** 若存在扰动点轴，再 `expand_dims(xph, axis=-1)` 补上最后一维，使形状对齐 `(m, m, ..., n_abs)`。
- **L938** `xph = xp_copy(xp.broadcast_to(xph, new_shape), xp=xp)`：广播到 `new_shape`，并用 `xp_copy` 复制成**可写**数组（`broadcast_to` 返回的是只读视图，而下一步要改对角线，必须可写；`xp_copy` 同时保证跨后端兼容）。
- **L939** `xph = xpx.at(xph)[i, i].set(x)`：**画龙点睛**——把对角线位置 `[i, i]`（`i = xp.arange(m)`）设为被扰动的 `x`。`xpx.at` 是 `array_api_extra` 提供的索引赋值接口，能在不可变后端（如 Torch/JAX）上完成「定点写入」（详见 u4-l4）。
- **L940** `return f(xph)`：把构造好的扰动矩阵交给真正的 `f`。

`i`（对角线索引）和 `m`（输入维数）在 `wrapped` 之外提前算好，避免每次调用重复计算：

[`_differentiate.py`:L928-L929](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L928-L929) —— `m = x0.shape[0]` 取输入维数；`i = xp.arange(m)` 作为对角线索引，供 `xpx.at(xph)[i, i].set(x)` 使用。

注意一个易被忽略的细节：**对角线写入的是 `x`（被 `derivative` 扰动后的值），而非对角线位置写入的是 `x0`（基准值）**。因为第 938 行广播出来的矩阵里，所有位置本就都是 `x0`；第 939 行只覆盖了对角线。这意味着当 `derivative` 仅扰动 `x[j]` 时，矩阵第 `j` 列只有第 `j` 行跟着变，其余行恒为 `x0[j]`——「只扰动坐标 `j`」的语义由此达成。

#### 4.2.4 代码实践

**实践目标**：用一个 `R^2 → R^2` 的向量值函数验证 `res.df` 形状为 `(2, 2)`，并与手算的解析雅可比对照。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.differentiate import jacobian

def f(x):
    # x 形状 (2,) 或 (2, ...)；用 stack 把两个分量沿新轴拼成 (2, ...)
    x1, x2 = x
    return np.stack([x1**2 + x2, x1 * x2])

x = np.array([1.0, 2.0])
res = jacobian(f, x)
print("res.df.shape =", res.df.shape)
print("res.df =\n", res.df)
```

**手算解析雅可比**：\(f_1 = x_1^2 + x_2\)，\(f_2 = x_1 x_2\)。在 \((1, 2)\) 处

\[
J = \begin{bmatrix} 2x_1 & 1 \\ x_2 & x_1 \end{bmatrix}_{(1,2)} = \begin{bmatrix} 2 & 1 \\ 2 & 1 \end{bmatrix}.
\]

**需要观察的现象**：

- `res.df.shape` 应为 `(2, 2)`。
- `res.df` 应接近 `[[2., 1.], [2., 1.]]`。

**预期结果**：形状与数值均与上式一致，`res.success` 全为 `True`。注意 `f` 必须用 `np.stack`（或返回长度为 `n` 的列表）把分量沿**首轴**拼起来，才能满足 `(n, ...)` 的输出约定；若直接返回 `[x1**2+x2, x1*x2]`（Python list），NumPy 也会自动转成 `(2, ...)` 数组（见 docstring 第 885-888 行的 `f4` 示例）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `wrapped` 要用 `xp_copy` 包裹 `broadcast_to` 的结果，而不是直接对 `broadcast_to` 的返回值做 `xpx.at(...)set(...)`？

**参考答案**：`broadcast_to` 返回的是**只读**视图（很多数组后端如此，直接写入会报错）。`wrapped` 接下来要在对角线上写入扰动值，因此需要 `xp_copy` 生成一份可写的独立副本；同时 `xp_copy` 是跨后端（NumPy/Torch/JAX）的统一接口，避免硬编码 `np.copy`。

**练习 2**：若 `m=1`（一元函数但写成 `R^1→R^n`），对角扰动矩阵退化成什么？`res.df` 形状是什么？

**参考答案**：`m=1` 时 `xph` 是 `(1, 1[, ...])`，对角线即唯一元素。`f` 返回 `(n, 1[, ...])`，`res.df` 形状为 `(n, 1)`（若 `f` 输出为首轴 `n`）。这等价于对一元向量值函数逐分量求导。

---

### 4.3 委托 derivative 与结果形状

#### 4.3.1 概念说明

构造好 `wrapped` 之后，`jacobian` 的工作就只剩「把它交给 `derivative`」。但这里有一个看似矛盾的问题需要解决：

- 传给 `derivative` 的 `x` 形状是 `(m,)`；
- 但 `wrapped(x)` 返回的形状是 `(n, m)`（或标量输出时 `(m,)`），**与 `x` 的形状不一致**。

按 `derivative` 的默认规则（`preserve_shape=False`），`_initialize` 会强制要求「`f` 输出形状 == `x` 的广播形状」，于是会抛 `ValueError`。为了让 `wrapped` 能合法地返回比 `x` 更「大」的输出，`jacobian` **必须**用 `preserve_shape=True` 调用 `derivative`。

`preserve_shape=True` 在共享框架 `_initialize` 里做了两件关键的事（详见 u2-l6）：

1. 把内部 `shape` 从 `(m,)` **广播扩展**为 `broadcast(fshape, (m,)) = (n, m)`——即把输出多出来的 `n` 轴并入迭代形状。于是框架把问题看成「`n·m` 个独立的标量求导」，每个 `(i, j)` 位置对应一个雅可比元素。
2. 关闭「已收敛元素的压缩」，保证 `wrapped` 每次都收到完整的坐标向量（否则压缩会破坏对角扰动矩阵的索引一致性）。

最后，`jacobian` 还做了一处结果清理：`del res.x`。原因是 `derivative` 返回的 `res.x` 是它**内部广播后**的 `x`（形状已被扩成 `(n, m)` 等），这个广播方式对用户没有意义（用户本来就知道自己传的 `x`），保留反而会引起误解，所以删掉。

#### 4.3.2 核心流程

```
jacobian 主流程：
  1. xp = array_namespace(x)                 # 取后端命名空间
  2. x0 = xp_promote(x, force_floating=True) # 整数 x 升浮点（求导需要）
  3. 校验 x0.ndim >= 1                       # 否则抛 ValueError
  4. 预计算 m、对角索引 i
  5. 定义 wrapped(x)                          # 见 4.2
  6. res = derivative(wrapped, x, preserve_shape=True, ...)   # 委托
  7. del res.x                                # 删除无意义的内部广播 x
  8. return res                               # res.df 即雅可比
```

#### 4.3.3 源码精读

主流程开头三行做后端与类型准备：

[`_differentiate.py`:L921-L926](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L921-L926) —— `array_namespace(x)` 取后端；`xp_promote(x, force_floating=True, xp=xp)` 把整数类型 `x` 提升为浮点（有限差分必须用浮点，否则整数运算会截断）；`if x0.ndim < 1` 校验 `x` 至少一维。

核心委托调用：

[`_differentiate.py`:L942-L945](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L942-L945) —— 把 `wrapped` 交给 `derivative`，**硬编码 `preserve_shape=True`**，并把 `tolerances`、`maxiter`、`order`、`initial_step`、`step_factor`、`step_direction` 透传下去（`step_direction` 可逐坐标不同，用于边界附近的多元函数）。

要理解 `preserve_shape=True` 为何能让 `wrapped` 合法返回 `(n, m)`，需要看共享框架里对应的分支：

[`scipy/_lib/_elementwise_iterative_method.py`:L105-L112](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L105-L112) —— `_initialize` 的 `preserve_shape` 分支。其中 `shape = np.broadcast_shapes(fshape, shape)` 把迭代形状从输入 `(m,)` 扩展为输入与输出形状的并集（即 `(n, m)`），并把 `x` 广播到这个新 `shape`。这是「`n·m` 个雅可比元素各自独立求导」的根源。

[`scipy/_lib/_elementwise_iterative_method.py`:L114-L120](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L114-L120) —— 形状一致性校验：`f.shape == shape`。若**不**开 `preserve_shape`，`shape` 仍是 `(m,)`，而 `wrapped` 输出 `(n, m)`，此处就会抛 `ValueError("The shape of the array returned by func must be the same as ...")`。这正是 `jacobian` 必须用 `preserve_shape=True` 的直接原因。

结果清理：

[`_differentiate.py`:L947-L948](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L947-L948) —— `del res.x` 删除 `derivative` 返回的内部广播 `x`（其形状已变成 `(n, m)` 等，对用户无意义），然后返回 `res`。`res.df` 即雅可比。

#### 4.3.4 代码实践

**实践目标**：通过「分别求每一列」来验证 `jacobian` 一次调用得到的 `(n, m)` 矩阵与「逐个偏导数独立求」的结果一致，从而理解 `preserve_shape` 把问题拆成 `n·m` 个标量求导的含义。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.differentiate import jacobian, derivative

# 一个 R^2 -> R^2 函数
def df1(z):
    x, y = z
    return np.stack([np.cos(0.5*x)*np.cos(y), np.sin(2*x)*y**2])

z = np.array([0.5, 0.25])
res = jacobian(df1, z, initial_step=10)
print("res.df.shape =", res.df.shape)   # (2, 2)

# 把每个雅可比元素当成「单输入单输出」的标量函数单独求导
def df1_0x(x):           # 固定 y=z[1]，对 x 求 f 的第 0 个分量
    return np.cos(0.5*x)*np.cos(z[1])

res00 = derivative(df1_0x, z[0:1], initial_step=10)
print("res.df[0,0] =", res.df[0, 0], "  独立求 =", res00.df[0])
```

**需要观察的现象**：

- `res.df.shape` 为 `(2, 2)`。
- `res.df[0, 0]` 与「固定 `y`、单独对 `x` 求 `f_0` 的导数」`res00.df` 数值一致——这说明 `(i, j)` 位置的雅可比元素确实等价于「其余坐标固定、只对 `x_j` 求 `f_i` 的导数」。

**预期结果**：两者数值在容差内一致。事实上，测试套件 `test_attrs`（见 u4-l5）正是用这种「四象限拆分」方式逐一对照 `res.df[i, j]` 来验证 `jacobian` 正确性的，可对照阅读 [`test_differentiate.py`:L601-L614](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L601-L614)。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `jacobian` 内部 `derivative(..., preserve_shape=True)` 改成 `preserve_shape=False`，会在哪一步报什么错？

**参考答案**：会在 `derivative` 调用 `eim._initialize` 时，于形状一致性校验处抛 `ValueError("The shape of the array returned by func must be the same as the broadcasted shape of x and all other args.")`——因为 `wrapped` 返回 `(n, m)` 而 `x` 形状为 `(m,)`，两者不一致。

**练习 2**：为什么 `jacobian` 要 `del res.x`，却保留 `res.df`、`res.error`、`res.nit`、`res.nfev` 等属性？

**参考答案**：`res.x` 是 `derivative` 内部把 `x` 广播到 `(n, m)` 后的结果，其形状与广播方式是框架内部的实现细节，对调用 `jacobian` 的用户没有意义（用户知道自己传的 `x`），保留会误导。而 `df`（雅可比）、`error`（每元素的误差估计）、`nit`/`nfev`（每元素的迭代与求值次数）都是用户关心的结果，因此保留。注意 `nit` 和 `nfev` 是**按雅可比元素**给出的逐元素数组（见 docstring 第 806-809 行）。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务：

**任务**：实现一个极坐标→直角坐标的映射 \(f:\mathbf{R}^2\rightarrow\mathbf{R}^2\)，\(f(r,\varphi) = (r\cos\varphi,\, r\sin\varphi)\)。完成以下三件事并用源码知识解释现象。

1. 写出满足 `jacobian` 向量化契约的 `f`（提示：用 `np.stack`，参考测试套件里的 `f2`，[`test_differentiate.py`:L504-L514](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L504-L514)）。
2. 在 \(r=2,\varphi=\pi/3\) 处调用 `jacobian(f, x)`，验证 `res.df.shape == (2, 2)`，并与解析雅可比
   \[
   J = \begin{bmatrix} \cos\varphi & -r\sin\varphi \\ \sin\varphi & r\cos\varphi \end{bmatrix}
   \]
   对照。
3. 用 `x = np.array([[2.0, 2.1, 2.2], [np.pi/3, np.pi/3, np.pi/3]])`（形状 `(2, 3)`，即同时算 3 个点）再调一次 `jacobian`，验证 `res.df.shape == (2, 2, 3)`——对应 docstring 第 833-837 行的多点约定。

**需要解释的现象**（结合本讲源码）：

- 为什么 `f` 必须写成 `(2, ...) -> (2, ...)` 的向量化形式？（答：`wrapped` 会用形状 `(2, 2[, ...])` 的扰动矩阵调用它，见 4.2。）
- 为什么 `res.df` 是 `(2, 2)` 而不是 `(2,)`？（答：`f` 有 2 个输出分量，输出首轴 `n=2` 被 `_initialize` 的 `preserve_shape` 分支并入 `shape`，见 4.3。）

**预期结果**：单点 `res.df` 与解析雅可比在容差内一致；多点 `res.df.shape == (2, 2, 3)`，且沿最后一轴的每个切片都等于对应点的解析雅可比。若本地运行，可在 `f` 内打印 `x.shape`，观察 `wrapped` 传给 `f` 的形状序列（应出现 `(2, 2)` 与 `(2, 2, n_abs)` 两类）。

## 6. 本讲小结

- `jacobian` 把「多元函数求雅可比」翻译成「`n·m` 个独立的一元标量求导」，全部复用 `derivative`，自己不实现任何差分公式。
- 用户契约：`x` 形状 `(m,)`（或 `(m, k)`），`f` 必须向量化接受 `(m, ...)`、返回 `(n, ...)`，结果 `res.df` 形状 `(n, m)`（或 `(n, m, k)`）；标量输出（`n=1`）时退化为梯度 `(m,)`。
- 核心技巧是 `wrapped` 里的**对角扰动矩阵**：广播让全矩阵等于基准 `x0`，再只在对角线写入扰动值，使第 `j` 列等价于「只扰动坐标 `j`」，于是位置 `[i, j]` 恰好给出 \(\partial f_i/\partial x_j\)。
- 必须用 `preserve_shape=True` 委托 `derivative`：否则 `wrapped` 输出 `(n, m)` 与 `x` 形状 `(m,)` 不符，会被 `_initialize` 的形状校验拒绝；`preserve_shape` 还把迭代 `shape` 广播扩展为 `(n, m)`、关闭压缩。
- `jacobian` 不暴露 `args`/`kwargs`/`callback`/`preserve_shape`（后者内部硬编码为 `True`）；需要额外参数的 `f` 用 `functools.partial` 或 `lambda` 包装。
- 收尾 `del res.x` 删除内部广播后无意义的 `x`，保留逐元素的 `df`/`error`/`nit`/`nfev`。

## 7. 下一步学习建议

- **继续本单元**：阅读下一讲 **u3-l2 hessian 的实现**，看 `hessian` 如何在 `jacobian` 之上再套一层 `jacobian`（雅可比的雅可比）来求二阶导，以及它为何要收紧内层 `rtol`、累计 `nfev` 并把 `df` 重命名为 `ddf`。
- **回顾框架**：若对 `preserve_shape` 广播、`x[0]` 切片、压缩关闭等机制仍有模糊，建议重读 **u2-l6（eim._loop 与 _initialize）**，特别是 `_initialize` 的 `preserve_shape` 分支与 `_loop` 中 `xp.reshape(x, (shape + (-1,)))` 那一段。
- **扩展阅读**：想了解 `wrapped` 里 `xpx.at`、`xp_copy`、`xp_promote` 如何让同一段代码跑在 Torch/JAX 等后端，可预习 **u4-l4（Array API 后端支持）**；想看更多 `jacobian` 的边界用例（受限定义域、逐坐标 `step_direction`），可阅读 **u4-l5（测试体系与边界情况）** 中的 `test_step_direction_size`。

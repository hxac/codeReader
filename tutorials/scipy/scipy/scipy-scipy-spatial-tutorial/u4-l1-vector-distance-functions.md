# 向量距离函数族

## 1. 本讲目标

本讲是「距离度量」单元（u4）的第一讲，专门讲 `scipy.spatial.distance` 中那些**接收两个一维向量、返回一个标量**的距离函数。学完后你应该能够：

- 说清 `euclidean`、`sqeuclidean`、`minkowski`、`chebyshev`、`cityblock` 这「范数族」各自的数学定义与代码关系；
- 理解 `correlation` 与 `cosine` 为何本质上是「中心化 / 去中心化相关」的同一种度量；
- 掌握 `seuclidean`、`mahalanobis`、`canberra`、`braycurtis`、`jensenshannon` 这些「带参数」度量的含义与参数（`V`、`VI`、`w`、`base`）；
- 看懂 `_validate_vector` / `_validate_weights` 两个输入校验函数，并能据此判断一个距离函数是否支持批量（`(..., N)`）输入。

> 注意：本讲只讲**两个向量之间**的标量距离函数。把整组点两两算距离的 `pdist` / `cdist` / `squareform` 留给 u4-l3，布尔/集合型度量（`hamming` / `jaccard` 等）留给 u4-l2，度量注册表 `MetricInfo` 留给 u9-l1。

## 2. 前置知识

### 2.1 什么是「距离」

直觉上，**距离** \(d(u, v)\) 是衡量两个对象「有多不一样」的一个非负数。一个合格的「度量（metric）」要满足四条公理：

1. **非负性**：\(d(u, v) \ge 0\)，且 \(d(u, v)=0 \iff u=v\)；
2. **对称性**：\(d(u, v) = d(v, u)\)；
3. **三角不等式**：\(d(u, w) \le d(u, v) + d(v, w)\)；
4. **同一性**：\(d(u, u) = 0\)。

本讲里 `euclidean`、`minkowski`(p≥1)、`cityblock`、`chebyshev`、`mahalanobis` 等是真正的度量；而 `cosine`、`correlation`、`jensenshannon`、`braycurtis` 在某些定义下只是「距离（dissimilarity）」，未必满足全部公理，使用时要心里有数。

### 2.2 向量的范数

把向量 \(u=(u_1,\dots,u_N)\) 看作 \(N\) 维空间里的一个点，它的 \(L_p\) 范数定义为：

\[
\lVert u \rVert_p = \left(\sum_{i=1}^{N} |u_i|^p\right)^{1/p}
\]

三个常见特例：

- \(p=1\)：\(L_1\) 范数，各分量绝对值之和，又称**曼哈顿范数**；
- \(p=2\)：\(L_2\) 范数，几何长度，又称**欧氏范数**；
- \(p=\infty\)：\(L_\infty\) 范数，各分量绝对值的最大值，又称**切比雪夫范数**。

两个向量 \(u, v\) 的**闵可夫斯基距离**就是它们之差的 \(L_p\) 范数：\(d_p(u,v)=\lVert u-v \rVert_p\)。本讲很多函数都是它的特例。

### 2.3 为什么需要这么多种距离

不同度量对「尺度异常」「方向」「分布」的敏感程度不同。例如：

- `euclidean` 对某个维度上的巨大偏差非常敏感（平方放大）；
- `cosine` 只看方向、不看大小，对整体放缩不敏感；
- `mahalanobis` 会「拉直」各维度之间的相关性；
- `canberra` 对接近 0 的小值更敏感。

正因为这些差异，现实任务（聚类、近邻、异常检测）里要按数据特点选度量，而不是无脑用欧氏距离。

## 3. 本讲源码地图

本讲只涉及两个文件，都在 `scipy/spatial/` 目录下：

| 文件 | 作用 |
|------|------|
| [distance.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py) | 全部距离函数的**纯 Python 实现**，本讲的主角。函数按字母序排列，我们关注 `_validate_vector`(L289)、`_validate_weights`(L297)、`minkowski`(L433)、`euclidean`(L504)、`sqeuclidean`(L543)、`correlation`(L595)、`cosine`(L675)、`seuclidean`(L900)、`cityblock`(L948)、`mahalanobis`(L994)、`chebyshev`(L1041)、`braycurtis`(L1102)、`canberra`(L1150)、`jensenshannon`(L1205) |
| [distance.pyi](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.pyi) | 类型存根（stub），给静态检查器看的「函数签名」，能帮我们一眼看清每个函数的参数与返回类型 |

模块顶部用到的几个关键依赖（[distance.py:104-117](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L104-L117)）：

- `from scipy._lib._array_api import _asarray` —— 一个「Array API 友好」版的 `np.asarray`，支持非 NumPy 后端；
- `from scipy.linalg import norm` —— 范数计算，`minkowski` 用它算 \(L_p\) 范数；
- `from scipy.special import rel_entr` —— 相对熵（KL 散度的被加项），`jensenshannon` 用它。

## 4. 核心概念与源码讲解

### 4.1 输入校验基座：`_validate_vector` 与 `_validate_weights`

#### 4.1.1 概念说明

几乎所有「两个向量」的距离函数在最开始都要做同一件事：**把用户传进来的列表 / 元组 / 数组统一转成一个干净的一维 NumPy 数组**。`distance.py` 把这件事抽成了 `_validate_vector`，而带权重参数的函数还会进一步用 `_validate_weights` 保证权重非负。

这两个函数看似简单，却决定了**一个距离函数能不能接收批量输入**（即形状 `(..., N)` 而不只是 `(N,)`），这是本讲的一个隐藏主线。

#### 4.1.2 核心流程

```
_validate_vector(u):
    1. 用 np.asarray 转成 C 连续数组（order='c'，便于 C 扩展直接读取）
    2. 若 ndim == 1，直接返回
    3. 否则 raise ValueError("Input vector should be 1-D.")

_validate_weights(w):
    1. 复用 _validate_vector(w, dtype=float64)
    2. 若存在负数权重，raise ValueError
    3. 返回
```

注意第 2 步的硬性要求：**超过一维就报错**。这意味着所有走 `_validate_vector` 的距离函数（`cityblock`、`correlation`、`mahalanobis`、`chebyshev`、`braycurtis`、`canberra`、`seuclidean` 等）**只接受一维向量**；而 `minkowski`、`euclidean`、`sqeuclidean`、`jensenshannon` 改用了支持任意前导维度的 `_asarray`，因此它们能批量处理 `(..., N)` 的输入。

#### 4.1.3 源码精读

[_validate_vector](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L289-L294) 把输入强制转成 C 连续、一维的数组：

```python
def _validate_vector(u, dtype=None):
    # XXX Is order='c' really necessary?
    u = np.asarray(u, dtype=dtype, order='c')
    if u.ndim == 1:
        return u
    raise ValueError("Input vector should be 1-D.")
```

- `order='c'`：保证内存按行优先排布，方便后续被 C/Cython 后端直接读取（与 KDTree 那一讲要求 C 连续 float64 是同一个道理）。
- `dtype=None`：默认不强制类型，由各调用方决定（例如 `seuclidean` 传 `np.float64`，`cityblock` 不传）。

[_validate_weights](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L297-L301) 在此基础上额外要求权重非负：

```python
def _validate_weights(w, dtype=np.float64):
    w = _validate_vector(w, dtype=dtype)
    if np.any(w < 0):
        raise ValueError("Input weights should be all non-negative")
    return w
```

权重非负是「带权距离」仍满足度量性质的必要条件——负权重会把距离越加权越小，破坏三角不等式。

> **小细节**：`_validate_weights` 一律把权重转成 `float64`，所以即使你传整数权重 `[1, 2, 3]`，内部也按浮点参与运算。

#### 4.1.4 代码实践

**实践目标**：直观感受「哪些函数拒绝多维输入」。

操作步骤（运行下面这段「示例代码」）：

```python
# 示例代码
from scipy.spatial import distance
import numpy as np

u2 = np.array([[1, 0, 0], [0, 1, 0]])   # 形状 (2, 3)，二维
v2 = np.array([[0, 1, 0], [1, 0, 0]])

# (a) minkowski / euclidean 用的是 _asarray，支持批量
print("euclidean batch:", distance.euclidean(u2, v2))

# (b) cityblock 用的是 _validate_vector，只接受 1-D
try:
    distance.cityblock(u2, v2)
except ValueError as e:
    print("cityblock 报错:", e)
```

需要观察的现象：第 (a) 行能正常运行（沿最后一维算，返回一维数组）；第 (b) 行抛出 `Input vector should be 1-D.`。

预期结果：`euclidean` 批量返回类似 `[1.414..., 1.414...]`；`cityblock` 报错。

> 待本地验证：你看到的精确数组形状与数值取决于 NumPy 版本对 `(...,N)` 广播的处理；但「`cityblock` 抛 ValueError」是确定的。

#### 4.1.5 小练习与答案

1. **练习**：为什么 `chebyshev` 不能像 `euclidean` 那样批量处理一整组向量？
   **答案**：因为 `chebyshev` 内部第一步就调用 `_validate_vector`，它会拒绝任何 `ndim>1` 的输入；而 `euclidean` 走的是 `_asarray` 路径，保留了前导维度。

2. **练习**：传一个含负值的权重 `w=[1, -1, 2]` 给 `minkowski`，会发生什么？
   **答案**：`minkowski` 在 `w is not None` 时调用 `_validate_weights`，它检测到 `w<0` 后抛出 `ValueError("Input weights should be all non-negative")`。

---

### 4.2 闵可夫斯基距离族：`minkowski` / `euclidean` / `sqeuclidean` / `chebyshev` / `cityblock`

#### 4.2.1 概念说明

这一族是「\(L_p\) 范数家族」的不同特例，是使用最频繁的距离。它们之间的关系可以用一张表概括：

| 函数 | 数学定义 | 等价的 \(L_p\) |
|------|----------|-----------------|
| `minkowski(u,v,p)` | \((\sum \lvert u_i-v_i\rvert^p)^{1/p}\) | 任意 \(p>0\) |
| `euclidean(u,v)` | \((\sum \lvert u_i-v_i\rvert^2)^{1/2}\) | \(p=2\) |
| `sqeuclidean(u,v)` | \(\sum \lvert u_i-v_i\rvert^2\) | \(p=2\)，但**不开方** |
| `cityblock(u,v)` | \(\sum \lvert u_i-v_i\rvert\) | \(p=1\)（曼哈顿） |
| `chebyshev(u,v)` | \(\max_i \lvert u_i-v_i\rvert\) | \(p=\infty\) |

关键直觉：

- **`sqeuclidean` 省掉开方**：在很多场景（如比较大小、构造核函数）只需要 \(\lVert u-v\rVert^2\)，省一次 `sqrt` 既快又数值更稳；它和 `euclidean` 的区别只在最后一步开根号。
- **`chebyshev` 是 \(p\to\infty\) 极限**：当 \(p\to\infty\) 时，和式中最大的那一项支配整体，故等价于取各维差值的最大绝对值。
- **权重 \(w\)**：带权时，闵可夫斯基距离把每个差值乘上 \(w_i^{1/p}\)，等价于定义 \((\sum w_i\lvert u_i-v_i\rvert^p)^{1/p}\)。

#### 4.2.2 核心流程

**`minkowski`** 的实现思路（关键在「把权重折算成可乘进差值的 `root_w`」）：

```
minkowski(u, v, p, w):
    u, v = _asarray(...)            # 支持 (..., N)
    若 p <= 0: 报错
    u_v = u - v
    若有 w:
        w = _validate_weights(w)
        root_w = w 的 1/p 次方（按 p 特判以提精度/提速）
        u_v = root_w * u_v          # 把权重揉进差值
    dist = norm(u_v, ord=p, axis=-1)
    return dist
```

为什么「揉进差值」等价于带权？因为：

\[
\lVert w^{1/p}\odot (u-v)\rVert_p = \left(\sum_i (w_i^{1/p}\lvert u_i-v_i\rvert)^p\right)^{1/p} = \left(\sum_i w_i\lvert u_i-v_i\rvert^p\right)^{1/p}
\]

**`euclidean`** 直接复用 `minkowski(p=2)`；**`sqeuclidean`** 不走 `minkowski`，而是自己用 `np.vecdot` 算 \(\sum w_i (u_i-v_i)^2\)；**`chebyshev`** 与 **`cityblock`** 各有独立实现，并不复用 `minkowski`。

#### 4.2.3 源码精读

[minkowski](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L433-L501) 的核心实现：

```python
def minkowski(u, v, p=2, w=None):
    ...
    u = _asarray(u, order='C')
    v = _asarray(v, order='C')
    if p <= 0:
        raise ValueError("p must be greater than 0")
    u_v = u - v
    if w is not None:
        w = _validate_weights(w)
        if p == 1:
            root_w = w
        elif p == 2:
            # better precision and speed
            root_w = np.sqrt(w)
        elif p == np.inf:
            root_w = (w != 0)
        else:
            root_w = np.power(w, 1/p)
        u_v = root_w * u_v
    dist = norm(u_v, ord=p, axis=-1)
    return dist
```

要点：
- `p <= 0` 直接拒绝（避免 `1/p` 除零或产生复数）。
- `p==1` 时 `root_w=w`（因为 \(w^1=w\)），`p==2` 时用 `np.sqrt(w)`（注释说精度和速度都更好——`sqrt` 比 `power(w, 0.5)` 快且稳定），`p==inf` 时 `root_w=(w!=0)`（退化为 0/1 掩码）。
- `norm(..., axis=-1)`：沿最后一维算范数，所以前导维度得以保留——这就是批量能力 `(...,N)` 的来源。

[euclidean](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L504-L540) 只有一行实现：

```python
def euclidean(u, v, w=None):
    ...
    return minkowski(u, v, p=2, w=w)
```

`euclidean` 就是 \(p=2\) 的 `minkowski` 的别名式封装，所以它也支持批量、也支持权重。

[sqeuclidean](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L543-L592) 走自己的路径，用 `np.vecdot` 实现并保留浮点类型：

```python
    ...
    # Preserve float dtypes, but convert everything else to np.float64
    utype, vtype = None, None
    if not (hasattr(u, "dtype") and np.issubdtype(u.dtype, np.inexact)):
        utype = np.float64
    if not (hasattr(v, "dtype") and np.issubdtype(v.dtype, np.inexact)):
        vtype = np.float64

    u = _asarray(u, dtype=utype, order='C')
    v = _asarray(v, dtype=vtype, order='C')
    u_v = u - v
    u_v_w = u_v  # only want weights applied once
    if w is not None:
        w = _validate_weights(w)
        u_v_w = w * u_v
    return np.vecdot(u_v, u_v_w)
```

两处巧妙之处：
- **类型保留**：若输入本来就是浮点（如 `float32`），就保持原类型；整数则升到 `float64`。这影响后续 `pdist`/`cdist` 的输出 dtype。
- **「权重只应用一次」**：结果 \(\sum_i w_i (u_i-v_i)^2 = \text{vecdot}(u_v,\; w\odot u_v)\)。如果写成 `vecdot(u_v_w, u_v_w)` 会把权重平方（多乘一遍），所以特意令 `u_v_w = w*u_v` 后用 `vecdot(u_v, u_v_w)`——一元一元带权，正合定义。

[chebyshev](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1041-L1099) 取各维差值绝对值的最大值：

```python
    u = _validate_vector(u)
    v = _validate_vector(v)
    if w is not None:
        w = _validate_weights(w)
        return max((w > 0) * abs(u - v))
    return max(abs(u - v))
```

注意带权分支：`(w > 0) * abs(u - v)` 是**逐元素**的乘法（`w>0` 产生 0/1 掩码），再取 `max`。也就是说 `chebyshev` 的权重只是「开关」——把权重为 0 的维度排除，权重的大小并不起线性作用。这与 `minkowski` 里权重的连续含义不同，是常见的认知坑。

[cityblock](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L948-L991) 是 \(L_1\) 范数：

```python
    u = _validate_vector(u)
    v = _validate_vector(v)
    l1_diff = abs(u - v)
    if w is not None:
        w = _validate_weights(w)
        l1_diff = w * l1_diff
    return l1_diff.sum()
```

`cityblock` 的权重是**连续**的（\(\sum w_i\lvert u_i-v_i\rvert\)），这点与 `minkowski(p=1)` 完全一致，只是实现更直接。

#### 4.2.4 代码实践

**实践目标**：用 `minkowski` 一个函数验证三个特例的自洽性。

操作步骤（示例代码）：

```python
# 示例代码
from scipy.spatial import distance
u, v = [1, 0, 0], [0, 1, 0]

print("minkowski p=1 :", distance.minkowski(u, v, 1))   # 应等于 cityblock
print("cityblock     :", distance.cityblock(u, v))
print("minkowski p=2 :", distance.minkowski(u, v, 2))   # 应等于 euclidean
print("euclidean     :", distance.euclidean(u, v))
print("minkowski p=inf:", distance.minkowski(u, v, float('inf')))  # 应等于 chebyshev
print("chebyshev     :", distance.chebyshev(u, v))
print("sqeuclidean   :", distance.sqeuclidean(u, v))    # 应等于 euclidean**2
```

需要观察的现象：三组「特例 vs 通用」的输出应两两相等（`sqeuclidean` 应等于 `euclidean` 的平方）。

预期结果：`p=1` 得 2.0；`p=2` 得 1.4142135…；`p=inf` 得 1.0；`sqeuclidean` 得 2.0。

> 待本地验证：`minkowski(u,v,float('inf'))` 与 `chebyshev` 的返回在数值上完全一致，可用于交叉验证。

#### 4.2.5 小练习与答案

1. **练习**：为什么 `sqeuclidean([1,0,0],[0,1,0])` 返回 2.0，而 `euclidean` 返回 1.414…？
   **答案**：前者返回 \(\sum(u_i-v_i)^2 = 1^2+1^2 = 2\)，不开方；后者是前者的平方根 \(\sqrt{2}\)。

2. **练习**：用 `w=[1,1,0]` 调用 `chebyshev([1,0,0],[0,5,9], w=w)`，结果是多少？
   **答案**：第三维被 `w>0`（值为 0）屏蔽，只看前两维差值 1 和 5 的最大值，结果为 5。

3. **练习**：`minkowski` 里 `p==2` 分支为什么用 `np.sqrt(w)` 而不是 `np.power(w, 0.5)`？
   **答案**：源码注释写明 `np.sqrt` 在精度和速度上更优（`sqrt` 是专门的快速指令，`power` 是通用函数，存在额外数值误差与开销）。

---

### 4.3 角度类度量：`correlation` 与 `cosine`

#### 4.3.1 概念说明

这两类度量不关注向量的「长度」，而是关注「方向/形状」：

- **`cosine`**（余弦距离）：\(1 - \dfrac{u\cdot v}{\lVert u\rVert_2\lVert v\rVert_2}\)。它等于 1 减去余弦相似度。两个向量方向一致时为 0，正交时为 1，反向时为 2。
- **`correlation`**（相关距离）：先把 `u`、`v` 各自**中心化**（减去均值），再算余弦距离。它衡量「去掉直流分量后两个序列的相似度」。

两者的代码关系极为直接：**`cosine` 就是 `centered=False` 的 `correlation`**（源码注释把 cosine 称作 'uncentered correlation' / 'reflective correlation'）。

#### 4.3.2 核心流程

`correlation` 的流程：

```
correlation(u, v, w, centered):
    u, v = _validate_vector(...)            # 1-D
    拒绝复数输入
    若有 w: w = _validate_weights(w); w = w / w.sum()   # 权重归一化
    若 centered:
        减去（加权）均值 umu, vmu
    用 (加权) 点积计算 uv, uu, vv
    dist = 1 - uv / sqrt(uu * vv)
    return clip(dist, 0, 2)                 # 防浮点误差越界
```

`cosine` 的全部实现就是一句 `return correlation(u, v, w=w, centered=False)`。

#### 4.3.3 源码精读

[correlation](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L595-L672) 的实现：

```python
    u = _validate_vector(u)
    v = _validate_vector(v)
    if np.iscomplexobj(u) or np.iscomplexobj(v):
        msg = "`u` and `v` must be real."
        raise TypeError(msg)
    if w is not None:
        w = _validate_weights(w)
        w = w / w.sum()
    if centered:
        if w is not None:
            umu = np.dot(u, w)
            vmu = np.dot(v, w)
        else:
            umu = np.mean(u)
            vmu = np.mean(v)
        u = u - umu
        v = v - vmu
    if w is not None:
        vw = v * w
        uw = u * w
    else:
        vw, uw = v, u
    uv = np.dot(u, vw)
    uu = np.dot(u, uw)
    vv = np.dot(v, vw)
    dist = 1.0 - uv / math.sqrt(uu * vv)
    # Clip the result to avoid rounding error
    return np.clip(dist, 0.0, 2.0)
```

要点：
- **复数被拒绝**：相关/余弦基于实数点积，复数会直接 `TypeError`。
- **权重先归一化** `w = w / w.sum()`：保证权重是「比例」，加权均值与加权点积语义一致。
- **中心化**：`centered=True` 时减均值（这正是 `correlation` 与 `cosine` 的唯一区别）。
- **clip 到 [0, 2]**：理论上相关距离范围是 [0, 2]（相关系数范围 [-1,1] 映射到 [0,2]），但浮点误差可能让完全相同的向量算出 `1 - 1.0000000001 = -1e-10`，`clip` 把它拉回 0。这是 `correlation`/`cosine` 独有的稳健化处理。

[cosine](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L675-L717) 的全部逻辑：

```python
def cosine(u, v, w=None):
    ...
    # cosine distance is also referred to as 'uncentered correlation',
    #   or 'reflective correlation'
    return correlation(u, v, w=w, centered=False)
```

这一句揭示了 cosine 与 correlation 的本质同源——余弦距离就是「不中心化」的相关距离。因此 cosine 也自动获得了权重、clip、复数校验等所有 `correlation` 的行为。

#### 4.3.4 代码实践

**实践目标**：体会「余弦距离只看方向」——对向量整体放缩不敏感。

操作步骤（示例代码）：

```python
# 示例代码
from scipy.spatial import distance

a = [1, 0, 0]
b = [100, 0, 0]      # 与 a 同方向，但放大 100 倍
c = [0, 1, 0]        # 与 a 正交

print("euclidean(a, b):", distance.euclidean(a, b))   # 被放缩放大
print("cosine(a, b)   :", distance.cosine(a, b))      # 仍为 0
print("cosine(a, c)   :", distance.cosine(a, c))      # 正交 → 1
```

需要观察的现象：`euclidean(a, b)` 因放缩变得很大（约 99），但 `cosine(a, b)` 仍为 0.0（方向相同）。

预期结果：`cosine(a,b)=0.0`、`cosine(a,c)=1.0`、`euclidean(a,b)=99.0`。

> 待本地验证：`correlation([1,0,1],[1,1,0])` 文档示例给 1.5，可用它确认你环境里的 `correlation` 行为符合预期。

#### 4.3.5 小练习与答案

1. **练习**：为什么完全相同的两个向量 `u=v`，`cosine(u,v)` 不一定是精确的 0.0？
   **答案**：理论上应为 0，但浮点运算让 `uv/sqrt(uu*vv)` 略微偏离 1，于是 `1 - 1.0000…1` 可能得到微小负数；源码用 `np.clip(dist, 0.0, 2.0)` 把它修正为 0.0。

2. **练习**：`correlation` 和 `cosine` 在数学上的唯一区别是什么？
   **答案**：`correlation` 先减去（加权）均值再算余弦，`cosine` 不减均值。源码里 `cosine` 就是 `correlation(..., centered=False)`。

3. **练习**：能否给 `correlation` 传复数向量？为什么？
   **答案**：不能。源码检测到 `np.iscomplexobj` 会抛 `TypeError("`u` and `v` must be real.")`，因为余弦/相关基于实数点积定义。

---

### 4.4 标准化、协方差与分布度量：`seuclidean` / `mahalanobis` / `canberra` / `braycurtis` / `jensenshannon`

#### 4.4.1 概念说明

这一组「参数化」度量各自解决欧氏距离照顾不到的特殊问题：

- **`seuclidean(u, v, V)`**（标准化欧氏距离）：每个维度按其**方差** \(V_i\) 归一化。方差大的维度（波动大）权重小，避免它主导距离。它实际就是带权重 \(w=1/V\) 的 `euclidean`。
- **`mahalanobis(u, v, VI)`**（马氏距离）：用**协方差矩阵的逆** \(VI\) 拉直各维度间的相关性，得到「在数据分布意义下的距离」。参数 `VI` 是协方差矩阵的**逆**（不是协方差本身）。
- **`canberra(u, v)`**（堪培拉距离）：\(\sum_i \dfrac{\lvert u_i-v_i\rvert}{\lvert u_i\rvert+\lvert v_i\rvert}\)，对接近 0 的小值分外敏感（分母小）。
- **`braycurtis(u, v)`**（Bray-Curtis）：\(\dfrac{\sum\lvert u_i-v_i\rvert}{\sum\lvert u_i+v_i\rvert}\)，常用于生态学丰度数据，取值落在 [0,1]（当坐标非负时）。
- **`jensenshannon(p, q)`**（Jensen-Shannon）：衡量两个**概率分布**的差异，是 KL 散度的对称、有界版本，返回值是 JS 散度的平方根。

#### 4.4.2 核心流程

**`seuclidean`** 的实现极其简短，因为它直接复用 `euclidean`：

```
seuclidean(u, v, V):
    u, v, V 各自 _validate_vector；V 强制 float64
    校验三者长度一致（否则 TypeError）
    return euclidean(u, v, w=1/V)        # 关键：1/V 当权重
```

**`mahalanobis`** 用矩阵运算：

```
mahalanobis(u, v, VI):
    u, v = _validate_vector
    VI = np.atleast_2d(VI)               # 保证是二维矩阵
    delta = u - v
    m = delta · VI · delta               # 二次型
    return sqrt(m)
```

**`canberra`** 逐项做比值再求和，并用 `np.errstate` + `np.nansum` 处理 0/0：

```
canberra(u, v, w):
    u, v = _validate_vector
    若有 w: w = _validate_weights
    在 errstate(invalid='ignore') 下:
        d = |u-v| / (|u| + |v|)
        若有 w: d = w * d
        d = np.nansum(d)                 # 0/0 的 nan 当作 0
    return d
```

**`braycurtis`**：分子分母分别求和再相除，分子分母都可加权。

**`jensenshannon`**：先归一化 `p`、`q` 为概率，取中点 `m=(p+q)/2`，用 `rel_entr`（相对熵被加项）算 \(D(p\|m)+D(q\|m)\)，再开方除以 2，支持沿任意 `axis` 计算。

#### 4.4.3 源码精读

[seuclidean](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L900-L945) 巧妙复用 `euclidean`：

```python
    u = _validate_vector(u)
    v = _validate_vector(v)
    V = _validate_vector(V, dtype=np.float64)
    if V.shape[0] != u.shape[0] or u.shape[0] != v.shape[0]:
        raise TypeError('V must be a 1-D array of the same dimension '
                        'as u and v.')
    return euclidean(u, v, w=1/V)
```

把方差向量 `V` 取倒数作为权重传给 `euclidean`——这正是「标准化欧氏距离」\(\sqrt{\sum_i (u_i-v_i)^2 / V_i}\) 的定义。注意 `V` 必须严格正，否则 `1/V` 会爆炸，这是调用方的责任。

[mahalanobis](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L994-L1038) 实现二次型：

```python
    u = _validate_vector(u)
    v = _validate_vector(v)
    VI = np.atleast_2d(VI)
    delta = u - v
    m = np.dot(np.dot(delta, VI), delta)
    return np.sqrt(m)
```

- `np.atleast_2d(VI)`：允许传入标量或一维（会被升维），但正常用法是传 \(N\times N\) 的 \(V^{-1}\)。
- `delta · VI · delta`：标量二次型 \((u-v)^\top V^{-1}(u-v)\)，最后开方得马氏距离。
- 文档强调「`VI` 是协方差矩阵的**逆**」，常见错误是直接传协方差矩阵。

[canberra](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1150-L1202) 处理分母为零的边界：

```python
    u = _validate_vector(u)
    v = _validate_vector(v, dtype=np.float64)
    if w is not None:
        w = _validate_weights(w)
    with np.errstate(invalid='ignore'):
        abs_uv = abs(u - v)
        abs_u = abs(u)
        abs_v = abs(v)
        d = abs_uv / (abs_u + abs_v)
        if w is not None:
            d = w * d
        d = np.nansum(d)
    return d
```

- 当 \(u_i=v_i=0\) 时，分母 `|u_i|+|v_i|=0`，产生 `0/0 = nan`。源码用 `np.errstate(invalid='ignore')` 抑制警告，再用 `np.nansum` 把 `nan` 当作 0 求和——这正对应文档「`u[i]` 和 `v[i]` 同时为 0 时，按 0/0=0 处理」。

[braycurtis](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1102-L1147) 分子分母分别求和：

```python
    u = _validate_vector(u)
    v = _validate_vector(v, dtype=np.float64)
    l1_diff = abs(u - v)
    l1_sum = abs(u + v)
    if w is not None:
        w = _validate_weights(w)
        l1_diff = w * l1_diff
        l1_sum = w * l1_sum
    return l1_diff.sum() / l1_sum.sum()
```

注意它同时给分子和分母加权（不是只加分子），保持比值的语义；当坐标全非负时结果落在 [0,1]。

[jensenshannon](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1205-L1290) 用相对熵实现：

```python
    p = np.asarray(p)
    q = np.asarray(q)
    p = p / np.sum(p, axis=axis, keepdims=True)
    q = q / np.sum(q, axis=axis, keepdims=True)
    m = (p + q) / 2.0
    left = rel_entr(p, m)
    right = rel_entr(q, m)
    left_sum = np.sum(left, axis=axis, keepdims=keepdims)
    right_sum = np.sum(right, axis=axis, keepdims=keepdims)
    js = left_sum + right_sum
    if base is not None:
        js /= np.log(base)
    return np.sqrt(js / 2.0)
```

要点：
- **自动归一化**：`p`、`q` 不要求和为 1，函数会先除以各自总和（注释说明 "will normalize p and q if they don't sum to 1.0"）。
- **`rel_entr(p, m)`**：`scipy.special.rel_entr(x, y) = x*log(x/y)`，是 KL 散度 \(D(x\|y)=\sum x_i\log(x_i/y_i)\) 的被加项。于是 `left_sum = D(p\|m)`，`right_sum = D(q\|m)`。
- **`base`**：改变对数底（默认用 `scipy.stats.entropy` 的底，即自然对数 e），相当于换信息单位（nats ↔ bits）。
- **`axis` / `keepdims`**：可以沿指定轴批量计算多对分布的 JS 距离。

#### 4.4.4 代码实践

**实践目标**：体会马氏距离如何「拉直」相关性，使各向异性的分布下也能正确度量。

操作步骤（示例代码）：

```python
# 示例代码
import numpy as np
from scipy.spatial import distance

# 构造一个强相关的二维分布，取其协方差逆
rng = np.random.default_rng(0)
X = rng.standard_normal((10000, 2))
X[:, 1] = X[:, 0] * 5 + 0.1 * rng.standard_normal(10000)   # y ≈ 5x，强相关
cov = np.cov(X.T)
VI = np.linalg.inv(cov)

u, v = np.array([0.0, 0.0]), np.array([1.0, 1.0])
print("euclidean  :", distance.euclidean(u, v))     # 不考虑相关性
print("mahalanobis:", distance.mahalanobis(u, v, VI))  # 考虑了协方差结构

# jensenshannon 自动归一化
print("js 不归一化:", distance.jensenshannon([2, 2], [1, 3]))  # 会被自动归一化
print("canberra   :", distance.canberra([1, 0, 0], [0, 1, 0]))
```

需要观察的现象：在强相关分布下，沿「分布长轴」方向移动 1 个单位的马氏距离，比沿短轴方向小得多（马氏距离把长轴方向的差异「打折」）。`jensenshannon` 对未归一化的 `[2,2]`、`[1,3]` 仍能正常算（内部已除以总和）。

预期结果：`mahalanobis` 远小于不考虑相关性的欧氏估计；`canberra([1,0,0],[0,1,0]) = 2.0`（与文档示例一致）。

> 待本地验证：马氏距离的精确值取决于 `rng` 生成的协方差；但「同一位移在长轴/短轴方向马氏距离差异显著」这一现象是确定的。

#### 4.4.5 小练习与答案

1. **练习**：`mahalanobis` 的参数 `VI` 应该传协方差矩阵还是其逆矩阵？
   **答案**：传**协方差矩阵的逆** \(V^{-1}\)。源码 `m = delta · VI · delta` 对应 \((u-v)^\top V^{-1}(u-v)\)，若误传协方差本身结果就错了。文档也明确强调 "the argument `VI` is the inverse of `V`"。

2. **练习**：`seuclidean` 内部如何用 `euclidean` 实现「标准化」？
   **答案**：`return euclidean(u, v, w=1/V)`——把方差倒数 `1/V` 当作权重。方差大的维度 \(V_i\) 大、权重 \(1/V_i\) 小，从而被弱化。

3. **练习**：`canberra` 在 \(u_i=v_i=0\) 时如何处理对应的项？
   **答案**：分母 `|u_i|+|v_i|=0`，产生 `nan`；源码用 `np.errstate(invalid='ignore')` 抑制告警、`np.nansum` 把该项当 0 求和，即 0/0 视作 0（与文档说明一致）。

---

## 5. 综合实践

**任务：度量对「尺度异常」的敏感性对比**

下面这个贯穿性小任务把本讲四个最小模块串起来。给定一对「正常」向量，再构造一个「把其中一个维度放大 100 倍」的异常版本，观察不同度量谁最敏感。

操作步骤（示例代码）：

```python
# 示例代码
import numpy as np
from scipy.spatial import distance

u = np.array([1.0, 1.0, 1.0])
v = np.array([1.1, 1.2, 0.9])        # 普通差异

# 人造异常：把 v 的第一维放大 100 倍
v_outlier = v.copy()
v_outlier[0] = 100.0

def table(name, func, a, b):
    print(f"{name:14s} 正常={func(a,b):10.4f}   异常={func(a,b if False else v_outlier):12.4f}")

table("euclidean",  distance.euclidean,  u, v)
table("cityblock",  distance.cityblock,  u, v)
table("chebyshev",  distance.chebyshev,  u, v)
table("cosine",     distance.cosine,     u, v)
table("correlation",lambda a,b: distance.correlation(a,b), u, v)

# 进阶：用 seuclidean 给方差大的维度降权，看能否缓解
V = np.array([100.0, 1.0, 1.0])      # 第一维方差大
print(f"{'seuclidean':14s} 正常={distance.seuclidean(u,v,V):10.4f}   "
      f"异常={distance.seuclidean(u,v_outlier,V):12.4f}")
```

需要观察并思考：

1. 哪个度量在「异常」列被放得最大？（通常 `euclidean`/`cityblock` 因平方或线性叠加而被严重放大，`chebyshev` 直接被最大维差值主导。）
2. `cosine` / `correlation` 在异常下变化是否相对小？为什么？（因为它们看的是「方向/形状」，单个维度的放缩改变了方向但不像范数那样线性放大。）
3. 引入 `seuclidean` 并把第一维方差设大后，「异常」列的值是否被压回来了？这验证了「标准化欧氏距离按方差降权」的设计意图。

预期结论（待本地验证精确数值）：欧氏/曼哈顿/切比雪夫这类范数族对尺度异常最敏感；角度类（cosine/correlation）相对稳健；`seuclidean` 通过 `1/V` 能主动压制已知高方差维度。

> 进阶练习（源码阅读型）：打开 [distance.pyi](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.pyi#L24-L44) 里的 `_MetricKind` 字面量类型，数一数 `euclidean` 有多少个别名（`euclid`、`eu`、`e`），思考这些别名在 `pdist`/`cdist` 里如何被解析——这是下一讲（u4-l3）和 u9-l1 的注册表机制要展开的内容。

## 6. 本讲小结

- `distance.py` 的「两向量标量距离」函数都遵循同一种结构：**校验输入 → 算差值 → 套公式**；输入校验由 `_validate_vector`（强一维）和 `_validate_weights`（非负）两个基座承担。
- **范数族** `minkowski`/`euclidean`/`sqeuclidean`/`chebyshev`/`cityblock` 都是 \(L_p\) 范数的特例：`euclidean` 直接复用 `minkowski(p=2)`；`sqeuclidean` 用 `vecdot` 省开方且「权重只应用一次」；`chebyshev` 的权重只是 0/1 开关，与 `minkowski` 的连续权重含义不同。
- 只有 `minkowski`/`euclidean`/`sqeuclidean`/`jensenshannon` 用 `_asarray`（支持批量 `(...,N)`），其余用 `_validate_vector`（只接受一维）。
- **角度类** `correlation` 与 `cosine` 同源：`cosine = correlation(centered=False)`，都靠 `np.clip(0,2)` 修正浮点越界，都拒绝复数。
- **参数化度量** 各显神通：`seuclidean` 把 `1/V` 当权重复用 `euclidean`；`mahalanobis` 用二次型 `delta·VI·delta` 且 `VI` 是协方差之逆；`canberra` 用 `errstate`+`nansum` 处理 0/0；`jensenshannon` 用 `rel_entr` 实现并支持 `axis`。
- 选度量的直觉：关注幅度用范数族、关注方向用角度类、关注分布结构用 `mahalanobis`/`seuclidean`/`jensenshannon`。

## 7. 下一步学习建议

- **u4-l2 布尔与集合型距离及加权**：继续学 `hamming`、`jaccard`、`dice`、`russellrao` 等「把向量当集合」的度量，它们依赖 `_nbool_correspond_*` 辅助函数，和本讲的连续数值度量是两条不同的路径。
- **u4-l3 pdist、cdist 与 squareform**：本讲的函数都只算「一对向量」的距离；当你需要算「一组点两两之间」的全部距离时，就该用 `pdist`/`cdist`，它们会通过度量名串（如 `'euclidean'`）调度到本讲这些函数。
- **u9-l1 MetricInfo 注册表与调度**：想了解 `cdist(XA, XB, 'euclid')` 这种「别名串」如何被解析、如何选择 C++ 后端，去读 `MetricInfo` 与 `_METRIC_INFOS` 注册表。
- **u5-l3 directed_hausdorff**：从「点对距离」上升到「点集对点集」的距离，那里会用到本讲的度量思想衡量两个形状的整体差异。

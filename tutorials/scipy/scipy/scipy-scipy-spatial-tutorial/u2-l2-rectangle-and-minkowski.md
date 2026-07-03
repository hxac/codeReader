# Rectangle 几何基元与 minkowski 距离

## 1. 本讲目标

上一讲（u2-l1）我们已经会用 `KDTree` 建树、做最近邻查询，并知道「真正干活的是 Cython 内核 `cKDTree`」。本讲往「下」走一层，回答一个更本质的问题：

> kd-tree 凭什么能把最近邻查询从朴素的 \(O(n)\) 降到约 \(O(\log n)\)？它靠的是什么数学工具来做「整棵子树一刀切掉」的剪枝？

答案是两个几何工具：**闵可夫斯基距离（Minkowski 距离）** 和 **轴对齐超矩形（axis-aligned hyperrectangle）**。本讲读完，你应当能够：

- 说清闵可夫斯基距离与 \(p\) 范数的关系，以及 \(p=1,2,\infty\) 各对应哪种常见距离；
- 读懂 `_kdtree.py` 里 `minkowski_distance_p` / `minkowski_distance` 的实现，并知道它们已被弃用；
- 掌握 `Rectangle` 类的 `volume` / `split` / `min_distance_*` / `max_distance_*` 计算原理；
- 用一句话讲明白「点到矩形的最近距离」为什么能支撑 kd-tree 的查询剪枝。

本讲是「先用后剖」路径里第一次真正碰到算法几何的地方，它会为后续 u2-l3（查询方法全景）和 u8（C++ 内核）打下地基。

## 2. 前置知识

- **范数（norm）**：把一个向量「量」成一个非负实数的规则。最常用的是 \(L_p\) 范数。如果你对它没概念，可以先把它理解成「衡量向量有多长」的一把尺子，不同的 \(p\) 对应不同形状的尺子。
- **广播（broadcasting）**：NumPy 里形状不同的数组按规则对齐运算的能力。本讲的距离函数大量依赖「最后一维是坐标维，前面维度自动广播」。
- **轴对齐**：矩形的每一条边都平行于某一条坐标轴。kd-tree 只用这种「方正」的矩形，正是因为它们在距离计算上极其便宜。
- 本讲默认你读过 u2-l1，知道 `KDTree` 是 `cKDTree` 的子类、`leafsize` 是什么。

> 小贴士：本讲里反复出现的「p」就是范数的阶数；它和上一讲 `tree.query(x, p=2.0)` 里的 `p` 是同一个东西。`p=2` 是默认的欧氏距离。

## 3. 本讲源码地图

本讲只盯一个文件，外加从 `distance.py` 借来的一个函数：

| 文件 | 语言 | 本讲关注什么 |
| --- | --- | --- |
| [`_kdtree.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py) | Python | `minkowski_distance_p`、`minkowski_distance`、`Rectangle` 类 |
| [`distance.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py) | Python | `Rectangle` 内部实际调用的 `minkowski` 函数 |
| [`tests/test_kdtree.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_kdtree.py) | Python | `Test_rectangle` 与 `test_distance_*`，给出可验证的期望值 |

一句话定位：`_kdtree.py` 的顶部放了两个**已弃用**的距离函数和一个 `Rectangle` 类，它们是 kd-tree 剪枝查询的「几何字典」。

## 4. 核心概念与源码讲解

### 4.1 闵可夫斯基距离与 p 范数

#### 4.1.1 概念说明

两个点 \(x, y \in \mathbb{R}^m\) 之间的**闵可夫斯基距离**（\(L_p\) 距离）定义为：

\[
d_p(x, y) = \left( \sum_{i=1}^{m} |x_i - y_i|^p \right)^{1/p}, \qquad p \ge 1
\]

它是一整「族」距离，不同的 \(p\) 取值给出我们熟悉的具体距离：

| \(p\) | 名字 | 形象叫法 | 公式 |
| --- | --- | --- | --- |
| 1 | \(L_1\) | 曼哈顿距离 / cityblock | \(\sum_i |x_i - y_i|\) |
| 2 | \(L_2\) | 欧氏距离 | \(\sqrt{\sum_i (x_i - y_i)^2}\) |
| \(\infty\) | \(L_\infty\) | 切比雪夫距离 | \(\max_i |x_i - y_i|\) |

为什么 kd-tree 要把 \(p\) 当成一个参数？因为同一棵树、同一套剪枝逻辑，对任意 \(p \ge 1\) 都成立——只要把「距离」换成对应的 \(L_p\) 版本即可。这正是 `tree.query(x, p=...)` 里那个 `p` 的来源。

> 一个工程上很重要的细节：对 \(p=1\) 和 \(p=\infty\)，「开 \(p\) 次根」这一步是恒等操作（\((\cdot)^1\) 或「取最大」），所以这两种情况**不需要**算昂贵的开方。对一般的 \(p\)，开方不可避免。下面的源码正是据此分情况处理。

#### 4.1.2 核心流程

`_kdtree.py` 提供了两个配套函数：

- `minkowski_distance_p(x, y, p)`：返回 \(d_p(x,y)\) 的 **\(p\) 次幂**，即**不开根**的 \(\sum |x_i-y_i|^p\)。省掉开方是为了在「只需要比较大小」的剪枝场景里提速。
- `minkowski_distance(x, y, p)`：返回真正的 \(d_p(x,y)\)，内部调用前者再补一次开方。

二者的分派逻辑（伪代码）：

```
minkowski_distance_p(x, y, p):
    若 p == inf:  返回 max_i |x_i - y_i|          # 等价于距离本身
    若 p == 1:    返回 sum_i |x_i - y_i|          # 等价于距离本身
    否则:         返回 sum_i |x_i - y_i|^p        # 距离的 p 次幂

minkowski_distance(x, y, p):
    若 p in {inf, 1}: 直接返回 minkowski_distance_p(x, y, p)   # 无需开根
    否则:             返回 minkowski_distance_p(x, y, p) ** (1/p)
```

注意：当 \(p\) 取 1 或 \(\infty\) 时，`minkowski_distance_p` 的返回值**就是**真实距离；其余情况下它是距离的 \(p\) 次幂，**比大小时可以直接用，但要注意单位**。

#### 4.1.3 源码精读

两个函数都定义在文件顶部，先看 `__all__` 与导入，确认它们的公开身份：

[_kdtree.py:L8-L13](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L8-L13) —— 第 8 行 `from .distance import minkowski`（`Rectangle` 内部实际用的距离函数），第 11–13 行 `__all__` 把 `minkowski_distance_p`、`minkowski_distance`、`distance_matrix`、`Rectangle`、`KDTree` 列为可被 `import *` 搬走的公开名字。

`minkowski_distance_p` 的核心是三路分支：

[_kdtree.py:L70-L75](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L70-L75) —— `p==inf` 走 `np.amax(np.abs(y-x), axis=-1)`（切比雪夫），`p==1` 走 `np.sum(np.abs(y-x), axis=-1)`（曼哈顿），其余走 `np.sum(np.abs(y-x)**p, axis=-1)`（一般 \(L_p\) 的 \(p\) 次幂）。注意 `axis=-1`：坐标维总是在最后一维，前面任意形状靠广播对齐。

`minkowski_distance` 套一层「补开方」：

[_kdtree.py:L117-L120](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L117-L120) —— `p` 为 `inf` 或 `1` 时直接复用 `minkowski_distance_p`（无需开根），否则对其结果取 `**(1./p)`。

> ⚠️ 重要事实：这两个模块级函数都带 `@xp_capabilities(out_of_scope=True)` 装饰器，且 docstring 里写着 `.. deprecated:: 1.18.0`，函数体一上来就 `warnings.warn(..., DeprecationWarning)`，计划在 SciPy 1.20.0 移除，替代品是 `scipy.spatial.distance.minkowski`。**本讲为了讲清原理仍以它们为入口，但新代码请改用 `scipy.spatial.distance.minkowski`。** 见 [_kdtree.py:L52-L56](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L52-L56)。

#### 4.1.4 代码实践

**实践目标**：亲手验证三种 \(p\) 取值的距离公式，并与「先求幂、再开根」的两步法对齐。

**操作步骤**（示例代码，非项目原有代码）：

```python
import warnings, numpy as np
from scipy.spatial import minkowski_distance, minkowski_distance_p

x, y = [0, 0], [1, 1]
with warnings.catch_warnings():              # 压住弃用警告，便于观察数值
    warnings.simplefilter("ignore", DeprecationWarning)
    print("p=1  :", minkowski_distance(x, y, 1))      # 曼哈顿
    print("p=2  :", minkowski_distance(x, y, 2))      # 欧氏
    print("p=inf:", minkowski_distance(x, y, np.inf)) # 切比雪夫
    # 两步法：先求 p 次幂，再开 p 次根，应与上面 p=2 一致
    print("two-step:", minkowski_distance_p(x, y, 2) ** 0.5)
```

**需要观察的现象**：四行输出里，`p=1` 与 `p=inf` 两种情况下 `minkowski_distance` 的值等于 `minkowski_distance_p` 的值（无需开根）；`p=2` 时 `minkowski_distance_p` 返回的是 2（即 \(1^2+1^2\)），开根后才得到 \(\sqrt{2}\)。

**预期结果**（据公式推得，并与项目测试 `test_distance_l2` / `test_distance_l1` / `test_distance_linf` 的断言一致，见 [test_kdtree.py:L599-L613](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_kdtree.py#L599-L613)）：

```
p=1  : 2.0
p=2  : 1.4142135623730951
p=inf: 1.0
two-step: 1.4142135623730951
```

若实际运行数值有出入，请以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `minkowski_distance_p` 在 kd-tree 剪枝里比 `minkowski_distance` 更「划算」？
**答案**：剪枝只需要**比较两个距离的大小**，而 \(t \mapsto t^{1/p}\)（\(p\ge1\)）是单调递增的，所以比较 \(d_p^p\) 和比较 \(d_p\) 结论完全一样，却省掉了每次开方的开销。

**练习 2**：对 \(p=\infty\)，`np.sum(np.abs(y-x)**p, axis=-1)` 这种「一般情况」写法为何不被采用？
**答案**：因为 `np.inf` 作指数会让结果变成 0 或 inf（取决于该项是否为 0），数值上不等于 \(\max_i |x_i-y_i|\)，所以代码必须把 \(\infty\) 单独走 `np.amax` 分支。

### 4.2 Rectangle 超矩形基元：构造、体积、切分

#### 4.2.1 概念说明

**轴对齐超矩形**是若干个区间的「笛卡尔积」：

\[
R = [\mathrm{mins}_1, \mathrm{maxes}_1] \times [\mathrm{mins}_2, \mathrm{maxes}_2] \times \cdots \times [\mathrm{mins}_m, \mathrm{maxes}_m]
\]

在二维里它就是一个「边平行于坐标轴」的普通矩形；在三维里是长方体；更高维则是「盒子」。kd-tree 的每一个节点都对应这样一个盒子：建树时每切一刀，就把一个盒子沿某条坐标轴分成两个更小的盒子。

`_kdtree.py` 的 `Rectangle` 类就是这种盒子的 Python 表示，对外公开（见 `__all__`），也常被当成几何工具单独使用。

#### 4.2.2 核心流程

`Rectangle` 的三个基础能力：

```
构造 Rectangle(maxes, mins):
    自动把每维的 max/min 对齐（防止传反），记录维度 m

volume():
    各维边长连乘 = ∏(maxes_i - mins_i)

split(d, split):
    在第 d 维、坐标 split 处一刀切，返回 (less, greater) 两个新矩形
    less   的第 d 维上界改成 split
    greater 的第 d 维下界改成 split
```

切分对应 kd-tree 建树的核心动作：选一个维度 `d` 和一个切分点 `split`，把当前节点的点集一分为二。

#### 4.2.3 源码精读

构造函数会自动「摆正」上下界，并强制转成浮点：

[_kdtree.py:L149-L153](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L149-L153) —— 用 `np.maximum`/`np.minimum` 保证 `maxes >= mins`（即使你传反了），`.astype(float)` 统一类型，`self.m, = self.maxes.shape` 取出维度。docstring 也明确说明「若任一维上界小于下界会自动交换」（见 [_kdtree.py:L144-L148](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L144-L148)）。

体积就是边长连乘：

[_kdtree.py:L158-L166](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L158-L166) —— `np.prod(self.maxes - self.mins)`。

切分用拷贝避免污染原数组：

[_kdtree.py:L189-L195](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L189-L195) —— 先 `mid = np.copy(self.maxes)` 再把 `mid[d] = split`，构造 `less`；再 `mid = np.copy(self.mins)` 改 `mid[d] = split` 构造 `greater`。两份 `np.copy` 是关键：直接改 `self.maxes[d]` 会把当前矩形也改坏。

#### 4.2.4 代码实践

**实践目标**：复现官方测试 `test_split` 的断言，直观看到切分结果。

**操作步骤**（示例代码）：

```python
import numpy as np
from scipy.spatial import Rectangle

rect = Rectangle([0, 0], [1, 1])      # 单位正方形（注意：先传 maxes 再传 mins）
less, greater = rect.split(0, 0.1)    # 沿第 0 维、坐标 0.1 处切一刀
print("less.mins  :", less.mins, " less.maxes:", less.maxes)    # 期望 [0,0] / [0.1,1]
print("greater.mins:", greater.mins, "greater.maxes:", greater.maxes)  # [0.1,0] / [1,1]
print("volume     :", rect.volume())  # 1.0
```

**需要观察的现象**：`less` 与 `greater` 在第 0 维上「无缝拼接」（一个上界、一个下界都等于 0.1），其余维度不变；原矩形 `rect` 自身没被破坏（因为内部用了 `np.copy`）。

**预期结果**（与 [test_kdtree.py:L591-L596](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_kdtree.py#L591-L596) 的断言一致）：`less.maxes=[0.1,1]`、`less.mins=[0,0]`、`greater.maxes=[1,1]`、`greater.mins=[0.1,0]`、`volume=1.0`。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `split` 里的两处 `np.copy` 去掉、直接改 `self.maxes[d]`，会发生什么？
**答案**：构造 `greater` 时 `self.maxes` 已被改成 `split`，于是 `greater` 会用错误的边界构造，同时原 `rect` 也被永久污染，后续查询全部出错。这正是源码坚持 `np.copy` 的原因。

**练习 2**：一个三维矩形 `Rectangle([1,1,1],[2,2,2])` 的 `volume()` 是多少？
**答案**：\((2-1)^3 = 1.0\)（注意构造函数会把传入的 maxes/mins 摆正，这里本就已摆正）。

### 4.3 点与矩形、矩形与矩形的距离

#### 4.3.1 概念说明

剪枝的核心问题是两类「最值距离」：

1. **点到矩形的最近距离** \(d_{\min}(x, R)\)：查询点 \(x\) 到矩形 \(R\) 内任意点的最小距离。这是「这棵子树里最好情况下能多近」的下界。
2. **点到矩形的最远距离** \(d_{\max}(x, R)\)：\(x\) 到 \(R\) 内任意点的最大距离。这是「这棵子树里最坏情况下能多远」的上界。

对轴对齐矩形，这两个量都有**逐维闭式解**，不需要真的去枚举矩形里的点。

**最近距离**的关键观察：把 \(x\) 沿每一维「夹（clamp）」进区间，就得到矩形里离 \(x\) 最近的点。于是逐维的「超出量」为

\[
\delta_i = \max\bigl(0,\; \mathrm{mins}_i - x_i,\; x_i - \mathrm{maxes}_i\bigr)
\]

（\(x_i\) 落在区间内时 \(\delta_i=0\)；落在外面时 \(\delta_i\) 是到最近边界的距离）。最近距离就是这些超出量的 \(L_p\) 范数：

\[
d_{\min}(x, R) = \|\delta\|_p
\]

**最远距离**则相反：每一维都往离 \(x\) 最远的那一端靠，逐维取

\[
\delta_i^{\max} = \max\bigl(\mathrm{maxes}_i - x_i,\; x_i - \mathrm{mins}_i\bigr)
\]

（这里不再 `max(0,·)`，因为总是非负），再求 \(L_p\) 范数。

矩形与矩形的距离是同样的逐维逻辑，只是把「点 \(x\)」换成「另一个矩形」的对应边界。

#### 4.3.2 核心流程

```
min_distance_point(x, p):
    delta = max(0, max(mins - x, x - maxes))   # 逐维超出量
    return minkowski(0, delta, p)              # = ||delta||_p

max_distance_point(x, p):
    delta = max(maxes - x, x - mins)           # 逐维最远端距离
    return minkowski(0, delta, p)

min_distance_rectangle(other, p):  # 把"点"换成 other 的远端边界
    delta = max(0, max(self.mins - other.maxes, other.mins - self.maxes))
    return minkowski(0, delta, p)

max_distance_rectangle(other, p):
    delta = max(self.maxes - other.mins, other.maxes - self.mins)
    return minkowski(0, delta, p)
```

#### 4.3.3 源码精读

最近距离到点：

[_kdtree.py:L213-L215](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L213-L215) —— `delta = np.maximum(0, np.maximum(self.mins - x, x - self.maxes))`，再 `minkowski(np.zeros_like(delta), delta, p)`，等价于 \(\|\delta\|_p\)。

最远距离到点：

[_kdtree.py:L233-L234](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L233-L234) —— `delta = np.maximum(self.maxes - x, x - self.mins)`，每一维挑离 \(x\) 最远的端点。

矩形对矩形的最近/最远距离是完全平行的写法：

[_kdtree.py:L252-L254](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L252-L254) —— `min_distance_rectangle` 用「自己的下界 − 对方上界」与「对方下界 − 自己上界」取正部分，衡量两矩形之间的「间隙」。

[_kdtree.py:L272-L273](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L272-L273) —— `max_distance_rectangle` 用「自己上界 − 对方下界」与「对方上界 − 自己下界」取大，衡量两矩形最远两角的距离。

> ⚠️ 关键事实：这四个方法调用的 `minkowski` 是第 8 行 `from .distance import minkowski` 进来的那个**会开根**的版本（实现见 [distance.py:L433-L501](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L433-L501)），**不是**本模块顶部那个已弃用的 `minkowski_distance`/`minkowski_distance_p`。也就是说 `Rectangle` 返回的是「真正的距离」而非「距离的 \(p\) 次幂」。这点很容易看错，务必留意。

#### 4.3.4 代码实践

**实践目标**：用官方测试里的三个查询点，验证 `min_distance_point` 的几何含义。

**操作步骤**（示例代码）：

```python
import numpy as np
from scipy.spatial import Rectangle

rect = Rectangle([0, 0], [1, 1])           # 单位正方形
print(rect.min_distance_point([0.5, 0.5])) # 点在内部 -> 0
print(rect.min_distance_point([0.5, 1.5])) # 只在一维越界 -> 0.5
print(rect.min_distance_point([2, 2]))     # 两维都越界 -> sqrt(2)
```

**需要观察的现象**：点在矩形内时距离为 0；只在一维「探出去」时距离就是那一维的超出量；两维都探出去时距离是直角三角形的斜边（欧氏情况 \(p=2\)）。

**预期结果**：`0`、`0.5`、`1.41421356...`（即 \(\sqrt{2}\)），与 [test_kdtree.py:L572-L579](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_kdtree.py#L572-L579) 的断言完全一致。

#### 4.3.5 小练习与答案

**练习 1**：对 `Rectangle([0,0],[1,1])` 和查询点 `[0.5, 0.5]`（正中心），`max_distance_point` 的值是多少？为什么？
**答案**：\(1/\sqrt{2}\approx 0.7071\)。中心点到四个角等距，最远距离就是到任一角的距离 \(\sqrt{0.5^2+0.5^2}\)。这与 `test_max_inside` 的断言一致（见 [test_kdtree.py:L581-L582](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_kdtree.py#L581-L582)）。

**练习 2**：`min_distance_rectangle(R1, R2)` 在两个矩形「相交」时应返回什么？
**答案**：0。因为相交时每一维的间隙都是负数，被 `np.maximum(0, ·)` 截断为 0，范数自然为 0——这正是「两矩形可能有重合点，最近距离为 0」的几何含义。

### 4.4 超矩形距离如何支撑 kd-tree 剪枝

#### 4.4.1 概念说明

把前两节拼起来，就得到了 kd-tree 加速的全部秘密。建树时每个节点对应一个 `Rectangle`（该节点所辖点集的最小包围盒）。查询最近邻时，我们维护一个「当前已知最近距离」\(d_{\text{best}}\)。递归到一个节点时，先问一个问题：

> 这个节点的盒子里，**最好情况下**能离查询点多近？

答案正是 \(d_{\min}(x, R_{\text{node}})\)。如果它已经 \(\ge d_{\text{best}}\)，那么盒子里**任何一个点**都不可能比当前答案更近——整棵子树直接剪掉，不必再往下走。这就是把朴素 \(O(n)\) 查询压成约 \(O(\log n)\) 的关键。

类似地：

- **球形邻域查询**（`query_ball_point`，半径 \(r\)）：若 \(d_{\min}(x, R) > r\)，整子树无点在球内 → 剪掉；若 \(d_{\max}(x, R) \le r\)，整子树所有点都在球内 → 整批收入，不必再分。
- **双树查询**（`query_ball_tree` / `count_neighbors`）：用 `min_distance_rectangle` / `max_distance_rectangle` 同时对两棵树的节点对做剪枝。

#### 4.4.2 核心流程

单点最近邻查询的剪枝骨架（伪代码，仅为说明思想）：

```
search(node, x):
    if min_distance_point(x, node.rect) >= d_best:   # 下界剪枝
        return                                        # 整子树不可能更优
    if node 是叶子:
        对 node 里的每个点暴力算距离，更新 d_best
        return
    否则:
        先搜"更可能近"的那个孩子，再搜另一个
```

注意「先搜更可能近的孩子」这一步：它让 \(d_{\text{best}}\) 尽早变小，从而使后一个孩子在剪枝判断时更容易被砍掉。这是 kd-tree 实战性能的另一关键。

#### 4.4.3 源码精读

本节的 Python `Rectangle` 是「讲原理用的参考实现」。需要诚实说明的是：**默认的 `KDTree.query` 并不调用这个 Python `Rectangle`**——它通过 `super().query(...)` 把查询交给 Cython/C++ 内核 `cKDTree`（见 u2-l1 与 u8）。真正在大规模查询热路径上做剪枝的，是 C++ 里另一套**同名**的 `Rectangle`（定义在 `ckdtree/src/rectangle.h`，本讲不展开，留到 u8-l2）。

但两者的**数学含义完全一致**：都是「逐维超出量 + \(L_p\) 范数」。本讲的 Python 版胜在可读，C++ 版胜在快。你可以把 [Rectangle 类（_kdtree.py:L123-L273）](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L123-L273) 当成读懂 C++ 剪枝的「注释版」。

> 旁证：C++ 侧 `query_ball_point.cxx` 里也是先构造一个 `Rectangle` 再做 `min_distance` / `max_distance` 判断（见 `ckdtree/src/query_ball_point.cxx` 中对 `Rectangle` 的使用），逻辑与本节伪代码一一对应。

#### 4.4.4 代码实践

**实践目标**：用 `min_distance_point` 模拟一次「该不该剪掉这个子树」的判断。

**操作步骤**（示例代码）：

```python
import numpy as np
from scipy.spatial import Rectangle

# 假装某棵子树所辖点集的最小包围盒是 [10,10]-[11,11] 的小方块
subtree_rect = Rectangle([10, 10], [11, 11])
x = np.array([0.0, 0.0])          # 查询点在原点
d_best = 5.0                       # 我们手上已经有一个距离为 5 的近邻

# 下界：这个盒子里最好情况下离 x 多近？
d_min = subtree_rect.min_distance_point(x, 2)
print("d_min =", d_min, " 是否剪枝?", d_min >= d_best)
```

**需要观察的现象**：`d_min` 约为 \(\sqrt{10^2+10^2}\approx 14.14\)，远大于 `d_best=5`，于是判定「剪枝」——即这个子树里不可能存在比当前答案更近的点，应当跳过。

**预期结果**：`d_min ≈ 14.1421356`，`是否剪枝? True`。把 `d_best` 调大到 20 后，应输出 `是否剪枝? False`（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么最近邻查询用的是 `min_distance_point`（下界）而不是 `max_distance_point`？
**答案**：因为我们要回答「盒子里**最好**能多近」。只有当下界 \(d_{\min}\) 都不小于当前最优 \(d_{\text{best}}\) 时，才能保证盒内绝无更优解，从而安全剪枝。用上界做这个判断会错误地剪掉还可能有更优解的子树。

**练习 2**：在球形邻域查询（半径 \(r\)）里，「整批收入整棵子树」用的应该是 `min_distance_point` 还是 `max_distance_point`？
**答案**：用 `max_distance_point`。若 \(d_{\max}(x, R) \le r\)，说明盒子里**最远**的点都在球内，自然所有点都在球内，可以整批收入而不再细分。

## 5. 综合实践

把本讲三件事——\(L_p\) 距离、`Rectangle` 的逐维超出量、剪枝下界——串成一个可运行的小任务。

**任务**：自己实现一个朴素函数 `naive_min_distance(x, rect, p)`，它**只用** `minkowski_distance_p`（即「不开根」的那个函数）来复现 `Rectangle.min_distance_point`，并验证它满足剪枝所依赖的**下界性质**：对矩形内任意点 \(y\)，都有 \(d(x,y) \ge \mathrm{naive\_min\_distance}(x, R)\)。

**操作步骤**（示例代码，非项目原有代码）：

```python
import warnings
import numpy as np
from scipy.spatial import Rectangle, minkowski_distance_p

def naive_min_distance(x, rect, p=2.0):
    """仅用 minkowski_distance_p 复现 rect.min_distance_point(x, p)。"""
    # 逐维超出量：x 在区间内为 0，在外面为到最近边界的距离
    delta = np.maximum(0, np.maximum(rect.mins - x, x - rect.maxes))
    with warnings.catch_warnings():               # 压住弃用警告
        warnings.simplefilter("ignore", DeprecationWarning)
        powered = minkowski_distance_p(np.zeros_like(delta), delta, p)
    # 关键：minkowski_distance_p 返回的是 Lp 距离的 p 次幂，要补开根才是真实距离
    if p == 1 or p == np.inf:
        return powered                            # 这两种 p 无需开根
    return powered ** (1.0 / p)

rect = Rectangle([0, 0], [1, 1])                  # 单位正方形
x = np.array([2.0, 2.0])                          # 查询点在右上方外侧

# 1) 与官方实现对照
print("naive :", naive_min_distance(x, rect, 2))  # 期望 sqrt(2)
print("official:", rect.min_distance_point(x, 2)) # 期望 sqrt(2)

# 2) 验证下界性质：矩形内任意采样点 y 到 x 的距离都 >= naive_min_distance
rng = np.random.default_rng(0)
ys = rng.uniform(rect.mins, rect.maxes, size=(5000, rect.m))   # 在矩形内撒点
lower = naive_min_distance(x, rect, 2)
true_dists = np.sqrt(((ys - x) ** 2).sum(axis=1))              # 真实欧氏距离
print("下界成立?", np.all(true_dists >= lower - 1e-12))        # 期望 True
```

**需要观察的现象与预期结果**：

1. `naive` 与 `official` 两个值应当**完全相等**（都是 \(\sqrt{2}\)）。这验证了你用 `minkowski_distance_p`（含「补开根」）正确复现了 `min_distance_point`，也印证了 4.3 里「`Rectangle` 用的是会开根的 `minkowski`」这一事实。
2. 「下界成立?」应当为 `True`：矩形内 5000 个随机点到 \(x=[2,2]\) 的距离，没有任何一个小于 \(\sqrt{2}\)（最近的那个就是角落 \([1,1]\)，距离恰为 \(\sqrt{2}\)）。这正是 kd-tree 敢于「一刀剪掉整棵子树」的数学保证。

**思考延伸**：把 `x` 改成 `[0.5, 0.5]`（落在矩形内部），`naive_min_distance` 应当变成 0，下界依然成立——因为盒子里就包含 \(x\) 自己最近的点。再把 `p` 改成 1 或 `np.inf`，观察 `naive` 与 `official` 是否仍一致（应当一致，且此时 `minkowski_distance_p` 不开根就直接等于距离）。（数值以本地运行为准，待本地验证。）

## 6. 本讲小结

- 闵可夫斯基距离 \(d_p\) 是一族距离：\(p=1\) 曼哈顿、\(p=2\) 欧氏、\(p=\infty\) 切比雪夫；`_kdtree.py` 里的 `minkowski_distance_p` 返回 \(d_p^p\)（省开方），`minkowski_distance` 再补开根，二者均自 1.18.0 起弃用、1.20.0 移除。
- `Rectangle` 是轴对齐超矩形的 Python 表示：构造时自动摆正上下界，`volume()` 求边长积，`split(d, split)` 沿某维切成两个矩形（靠 `np.copy` 不污染原对象）。
- 点到矩形的最近/最远距离有逐维闭式解：最近距离用「超出量」\(\delta_i=\max(0,\mathrm{mins}_i-x_i,x_i-\mathrm{maxes}_i)\) 再取 \(\|\delta\|_p\);四个 `min/max_distance_point/rectangle` 方法共享这套逻辑。
- 一个易错点：`Rectangle` 的距离方法调用的是 `from .distance import minkowski`（会开根），**不是**本模块那两个已弃用函数。
- 剪枝本质：用 \(d_{\min}(x,R)\) 当下界——若它已不小于当前最优 \(d_{\text{best}}\)，整棵子树可安全跳过，这正是 kd-tree 把查询压到约 \(O(\log n)\) 的根因。
- 本讲的 Python `Rectangle` 是「可读的参考实现」;查询热路径上真正做剪枝的是 C++ 里同名的 `Rectangle`（`ckdtree/src/rectangle.h`），数学含义一致，将在 u8 详讲。

## 7. 下一步学习建议

- **紧接着读 u2-l3（KDTree 查询方法全景）**：本讲给了剪枝的「几何字典」，u2-l3 会把 `query` / `query_ball_point` / `query_pairs` / `count_neighbors` / `sparse_distance_matrix` 等查询 API 的语义和参数（`eps`、`distance_upper_bound`、`workers`）讲透，你会看到本讲的 `min/max_distance` 如何具体出现在每一种查询里。
- **想看「剪枝在生产代码里长什么样」**：跳到 u8-l2（C++ 内核 `ckdtree/src`），对照 `rectangle.h` / `distance_base.h` / `query.cxx`，体会同一套逐维超出量逻辑在 C++ 里的高性能写法。
- **想彻底搞懂距离函数族**：本讲的 `minkowski` 只是 `distance.py` 的一员，u4-l1 会系统讲欧氏、余弦、马氏等一整套向量距离，并把「\(p\) 范数」放到更大的度量家族里。

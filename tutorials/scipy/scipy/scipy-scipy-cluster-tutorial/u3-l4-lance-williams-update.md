# Lance-Williams 距离更新公式

## 1. 本讲目标

本讲是 hierarchy 子模块里最「数学」的一讲。读完本讲，你应当能够：

- 说清楚凝聚式聚类在「合并两簇之后，新簇到其它簇的距离该怎么算」这件事，为什么能用**一套统一的递推公式**来表达；
- 在 `hierarchy/_hierarchy_distance_update.pxi` 中为 `_single / _complete / _average / _centroid / _median / _ward / _weighted` 这七个 C 函数分别标注它对应的方法名、系数与代数形式，并和 `linkage()` 的 docstring 数学定义一一对照；
- 理解 `linkage_distance_update` 这个**函数指针 typedef** 与 `linkage_methods` **函数指针表**如何让三种完全不同的聚类算法（`linkage` / `nn_chain` / `fast_linkage`）复用同一份距离更新代码；
- 解释为什么 `centroid / median / ward` 三个函数要「先平方、最后开方」，以及这为什么和它们「必须配欧氏度量」直接相关。

## 2. 前置知识

本讲默认你已经掌握（来自 u3-l1、u3-l2）：

- **凝聚式聚类**：每个观测先自成一簇，每一步把当前最相似的两簇合并，共 \(n-1\) 步，产物是 (n−1)×4 的 linkage matrix `Z`。
- **七种链接方法** `single / complete / average / weighted / centroid / median / ward`，以及 `_LINKAGE_METHODS` 把它们编码为 `0..6` 的整数；`_EUCLIDEAN_METHODS = ('centroid','median','ward')` 这三种只允许欧氏距离。
- **三种 Cython 后端**：`single → mst_single_linkage`，`complete/average/weighted/ward → nn_chain`，`centroid/median → fast_linkage`。

本讲要补的关键背景是 **Lance-Williams 公式**。它的核心问题是：当我们把簇 \(x\) 和簇 \(y\) 合并成新簇 \((x \cup y)\) 时，对于森林里任意一个**还没被合并**的簇 \(i\)，如何只凭「合并前」的三个距离 \(d(x,i)\)、\(d(y,i)\)、\(d(x,y)\)（以及三个簇的大小），算出合并后的 \(d((x\cup y), i)\)，而**不必回到原始观测重新两两算距离**。

这一点至关重要：正因为距离可以这样「增量更新」，凝聚式聚类的距离矩阵维护成本才从「每步 \(O(n^2)\) 重算」降为「每步 \(O(n)\) 更新一列」，这是整套算法能在 \(O(n^2)\) 到 \(O(n^3)\) 完成的根基。

## 3. 本讲源码地图

本讲只涉及两个 Cython 源文件，且重心在第一个：

| 文件 | 作用 |
|------|------|
| `hierarchy/_hierarchy_distance_update.pxi` | 定义函数指针 typedef `linkage_distance_update`，以及七个 `_xxx` 距离更新函数。这是本讲主角。 |
| `hierarchy/_hierarchy.pyx` | 用 `include` 引入上面的 `.pxi`，定义 `linkage_methods` 函数指针表；并在 `linkage` / `fast_linkage` / `nn_chain` 三处以**完全相同的调用契约**调用 `new_dist(...)`。 |

`.pxi`（Cython include 文件）不是独立编译单元，它通过 `include "_hierarchy_distance_update.pxi"` 被原文「贴」进 `_hierarchy.pyx`，和后者共享同一个编译作用域——这正是七个 `cdef` 函数能被 `.pyx` 里的 `linkage_methods` 表直接引用的原因。

## 4. 核心概念与源码讲解

### 4.1 统一的 Lance-Williams 递推框架与函数指针 typedef

#### 4.1.1 概念说明

Lance 与 Williams（1966）提出：几乎所有凝聚式链接方法的距离更新，都能写成下面这个**统一递推式**：

\[
d((x\cup y), i) = \alpha_x\, d(x,i) + \alpha_y\, d(y,i) + \beta\, d(x,y) + \gamma\, \big| d(x,i) - d(y,i) \big|
\]

不同的方法，只是选择**不同的四个系数** \((\alpha_x, \alpha_y, \beta, \gamma)\) 而已。比如：

- `single` 取 \(\alpha_x=\alpha_y=\tfrac12,\ \beta=0,\ \gamma=-\tfrac12\)，化简后就是 \(\min(d(x,i),d(y,i))\)；
- `complete` 把 \(\gamma\) 翻成 \(+\tfrac12\)，化简后是 \(\max(d(x,i),d(y,i))\)；
- `average`（UPGMA）取按大小加权的 \(\alpha\)、\(\beta=\gamma=0\)。

scipy 的设计巧妙地利用了这种统一性：既然所有方法「输入相同、系数不同、输出一个 double」，那就把它们写成**签名完全一致**的一族 C 函数，再用一个**函数指针表**按 `method` 编号取用。这样三种聚类算法完全不必关心自己跑的是哪种方法——它们只管「拿到一个 `new_dist` 指针，合并时调用它」。

#### 4.1.2 核心流程

```
对 method ∈ {single, complete, average, centroid, median, ward, weighted}：
    编码 code = _LINKAGE_METHODS[method]            # 0..6
    取函数 new_dist = linkage_methods[code]          # 七个 _xxx 之一

聚类主循环（在 linkage / nn_chain / fast_linkage 中都一样）：
    1. 找到当前距离最小的两簇 x, y，记 d(x,y)=current_min
    2. 读出三簇大小 size_x, size_y, size_i
    3. 对每个未合并的簇 i：
         d((x∪y), i) = new_dist(d(x,i), d(y,i), d(x,y),
                                size_x, size_y, size_i)
         把结果写回距离矩阵
```

注意一个被刻意统一的细节：函数签名里有 6 个参数（3 个 double 距离 + 3 个 int 大小），但**没有任何一个 `_xxx` 函数会用到全部 6 个**。比如 `_single` 只用两个距离、`_median` 一个大小都不用。统一签名是「为了让函数指针表能装下它们」而付出的代价，换来的是主循环里一行代码通吃七种方法。

#### 4.1.3 源码精读

typedef 把这族函数的签名钉死在文件最上方（注意 docstring 对每个参数语义的精确约定：`d_xi` 是「簇 x 到簇 i」，`size_x` 是「簇 x 的大小」）：

[hierarchy/_hierarchy_distance_update.pxi:1-27](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_distance_update.pxi#L1-L27)

```cython
ctypedef double (*linkage_distance_update)(double d_xi, double d_yi,
                                           double d_xy, int size_x,
                                           int size_y, int size_i) noexcept
```

- `d_xi = d(x,i)`、`d_yi = d(y,i)`、`d_xy = d(x,y)`，其中 \(x,y\) 是被合并的两簇、\(i\) 是任一剩余簇；
- `size_x / size_y / size_i` 是三簇各自的原始观测数；
- 返回值是合并后新簇到 \(i\) 的距离 \(d((x\cup y), i)\)；
- `noexcept` 表示这些纯算术函数不抛 Python 异常（也意味着主循环要保证输入合法，比如距离非负）。

而「按编号取函数」就靠这张函数指针表（顺序与 `_LINKAGE_METHODS` 的 0..6 编码严格一致：`0:_single, 1:_complete, 2:_average, 3:_centroid, 4:_median, 5:_ward, 6:_weighted`）：

[hierarchy/_hierarchy.pyx:13-18](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L13-L18)

```cython
include "_hierarchy_distance_update.pxi"
cdef linkage_distance_update *linkage_methods = [
    _single, _complete, _average, _centroid, _median, _ward, _weighted]
```

主循环里取用它的样子（以朴素 `linkage` 为例，先取指针、再调用）：

[hierarchy/_hierarchy.pyx:717-719](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L717-L719)

```cython
cdef linkage_distance_update new_dist
new_dist = linkage_methods[method]
```

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：亲手验证「统一签名」与「顺序对应」这两件事。
2. **步骤**：
   - 打开 `hierarchy/_hierarchy_impl.py`，找到 `_LINKAGE_METHODS = {'single': 0, 'complete': 1, 'average': 2, 'centroid': 3, 'median': 4, 'ward': 5, 'weighted': 6}`。
   - 打开 `hierarchy/_hierarchy.pyx` 第 16–17 行的 `linkage_methods` 表，按下标 0–6 列出七个函数名。
   - 把两份清单逐行对齐，确认下标 `i` 处的方法名完全一致。
3. **观察**：例如下标 5 在 `_LINKAGE_METHODS` 里是 `'ward'`，在 `linkage_methods` 里是 `_ward`——这正是 `linkage(dists, n, method=5)` 会调到 `_ward` 的原因。
4. **预期结果**：七行全部一一对应，没有错位。这也是为什么改方法只需改一个整数编码，主循环代码一行都不用动。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `linkage_methods` 表里 `_centroid` 和 `_median` 的位置写反了（即下标 3、4 互换），用户调用 `linkage(..., method='centroid')` 时会发生什么？

**答案**：`method='centroid'` 的编码是 3，主循环取 `linkage_methods[3]`，此时实际拿到的是 `_median` 的实现，于是「centroid」名义下算出的其实是 median（WPGMC）的距离更新。由于二者系数接近，小数据上结果可能「看着差不多」而难以察觉——这正是函数指针表「按位置取用」带来的隐患：顺序必须和编码字典严格对齐。

---

### 4.2 七个 `_xxx` 函数的代数形式

#### 4.2.1 概念说明

七个函数可以分成截然不同的两组：

- **线性组**（`single / complete / average / weighted`）：递推式是真实距离 \(d\) 的线性组合，可以直接写成 Lance-Williams 标准形。它们对**任意**距离度量（不限于欧氏）都成立。
- **平方-开方组**（`centroid / median / ward`）：递推式作用在**平方距离** \(d^2\) 上，最后再 `sqrt`。它们来自「簇心 / 方差」这类几何量，依赖欧氏空间里的平行四边形恒等式，所以**只允许欧氏度量**——这正是 `_EUCLIDEAN_METHODS` 的由来。

这种二分法是本讲最重要的洞见：不是 scipy 「顺手」加了 `sqrt`，而是 centroid/median/ward 的数学定义本身就在平方距离空间里。下面把每个函数的代数形式和 docstring 数学定义对照列出。

#### 4.2.2 核心流程：系数对照表

记 \(s_x=\)`size_x`，\(s_y=\)`size_y`，\(s_i=\)`size_i`，\(T = s_x+s_y+s_i\)。下表给出每个函数对应的 Lance-Williams 系数（线性组作用在 \(d\) 上；平方组作用在 \(d^2\) 上）：

| 方法 | 函数 | \(\alpha_x\) | \(\alpha_y\) | \(\beta\) | \(\gamma\) | 作用对象 | 是否用 \(s_i\) |
|------|------|--------------|--------------|-----------|------------|----------|----------------|
| single | `_single` | 1/2 | 1/2 | 0 | −1/2 | \(d\) | 否 |
| complete | `_complete` | 1/2 | 1/2 | 0 | +1/2 | \(d\) | 否 |
| average (UPGMA) | `_average` | \(s_x/(s_x+s_y)\) | \(s_y/(s_x+s_y)\) | 0 | 0 | \(d\) | 否 |
| weighted (WPGMA) | `_weighted` | 1/2 | 1/2 | 0 | 0 | \(d\) | 否 |
| centroid (UPGMC) | `_centroid` | \(s_x/(s_x+s_y)\) | \(s_y/(s_x+s_y)\) | \(-s_x s_y/(s_x+s_y)^2\) | 0 | \(d^2\) | 否 |
| median (WPGMC) | `_median` | 1/2 | 1/2 | −1/4 | 0 | \(d^2\) | 否 |
| ward | `_ward` | \((s_i+s_x)/T\) | \((s_i+s_y)/T\) | \(-s_i/T\) | 0 | \(d^2\) | **是** |

两个要点：

1. **`size_i` 只在 `ward` 里出现**。尽管七个函数签名都带 `size_i`，但 `centroid / median` 完全忽略它（它们的几何递推只跟被合并两簇有关），`single / complete / weighted` 连大小都不看。`ward` 之所以需要 \(s_i\)，是因为 Ward 方差增量是一个「三方」量——它衡量「把 \(i\) 并进来能减少多少总方差」，自然涉及第三簇的大小。
2. **UPGMA vs WPGMA、UPGMC vs WPGMC 的对偶**：`average` 与 `centroid` 都是「按簇大小加权」（`size_x`、`size_y` 出现在分子），对应 `UPG-` 前缀（Unweighted Pair Group …，注意统计学里 "unweighted" 指「对原始观测等权」，因而对大小敏感）；`weighted` 与 `median` 都是「等权 1/2」，对应 `WPG-` 前缀。

#### 4.2.3 源码精读

**线性组**（真实距离的线性组合，无 `sqrt`）：

[hierarchy/_hierarchy_distance_update.pxi:30-42](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_distance_update.pxi#L30-L42)

```cython
cdef double _single(...) noexcept:
    return min(d_xi, d_yi)                       # α=1/2, γ=-1/2 化简

cdef double _complete(...) noexcept:
    return max(d_xi, d_yi)                       # α=1/2, γ=+1/2 化简

cdef double _average(...) noexcept:
    return (size_x * d_xi + size_y * d_yi) / (size_x + size_y)   # UPGMA
```

- `_single`：`min(d_xi, d_yi)` 即最近点距离，对应 docstring `d(u,v)=min(dist(u[i],v[j]))`。
- `_complete`：`max(...)` 即最远点距离，对应 `d(u,v)=max(...)`。
- `_average`：分子是 \(s_x d(x,i) + s_y d(y,i)\)，除以 \(s_x+s_y\)。这正是 UPGMA 的递推形式——它等价于 docstring 里「所有跨簇点对距离的平均」\(\frac{1}{|u||v|}\sum_{ij} d(u[i],v[j])\)。之所以能用递推式表达，是因为合并后 \(u = x \cup y\) 的跨簇点对可以拆成「\(x\)-\(i\)」和「\(y\)-\(i\)」两组，按各自点数加权平均。
- `_weighted`（见下）：

[hierarchy/_hierarchy_distance_update.pxi:65-67](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_distance_update.pxi#L65-L67)

```cython
cdef double _weighted(...) noexcept:
    return 0.5 * (d_xi + d_yi)                   # WPGMA: (dist(s,v)+dist(t,v))/2
```

**平方-开方组**（递推作用在 \(d^2\) 上，外面套 `sqrt`）：

[hierarchy/_hierarchy_distance_update.pxi:45-62](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_distance_update.pxi#L45-L62)

```cython
cdef double _centroid(...) noexcept:
    return sqrt((((size_x*d_xi*d_xi) + (size_y*d_yi*d_yi))
                 - (size_x*size_y*d_xy*d_xy)/(size_x+size_y))
                / (size_x+size_y))               # UPGMC

cdef double _median(...) noexcept:
    return sqrt(0.5*(d_xi*d_xi + d_yi*d_yi) - 0.25*d_xy*d_xy)   # WPGMC

cdef double _ward(...) noexcept:
    cdef double t = 1.0 / (size_x + size_y + size_i)   # T = s_x+s_y+s_i
    return sqrt((size_i+size_x)*t*d_xi*d_xi +
                (size_i+size_y)*t*d_yi*d_yi -
                size_i*t*d_xy*d_xy)
```

把 `_centroid` 根号内展开：

\[
\frac{s_x\, d_{xi}^2 + s_y\, d_{yi}^2}{s_x+s_y} \;-\; \frac{s_x s_y}{(s_x+s_y)^2}\, d_{xy}^2
\]

这正是 UPGMC 在平方距离上的 Lance-Williams 形式（\(\beta = -s_x s_y/(s_x+s_y)^2\)）。`_median` 把权重一律取 1/2、\(\beta=-1/4\)，是 centroid 的「等权」孪生（WPGMC）。

`_ward` 与 docstring 公式逐项对照（令 \(s=x,\ t=y,\ v=i\)）：

\[
d(u,v) = \sqrt{\tfrac{s_i+s_x}{T}\,d(x,i)^2 + \tfrac{s_i+s_y}{T}\,d(y,i)^2 - \tfrac{s_i}{T}\,d(x,y)^2},\qquad T=s_x+s_y+s_i
\]

代码里 `t = 1/T`，三项系数正是 \((s_i+s_x)/T\)、\((s_i+s_y)/T\)、\(-s_i/T\)，与 docstring 完全一致。**这里的开方与加权都来自 Ward 的方差增量定义**：Ward 算法每步选择使「簇内总方差增量」最小的合并，而这个增量在欧氏空间里恰好等于上式平方，所以最后要开方还原成「距离」语义，才能和距离矩阵里其它 (非 ward) 步骤在同一把尺子上比较。

#### 4.2.4 代码实践（数值验证型）

本实践是规格指定的核心任务：验证 `_average` 与 UPGMA 定义一致，并顺带把七个函数和 docstring 对齐。

1. **目标**：用真实数值确认 `_average` 的递推式 = 跨簇点对距离的平均；并验证 `ward` 必须在平方空间递推。
2. **操作步骤**（示例代码，可直接运行）：
   ```python
   import numpy as np
   from scipy.cluster.hierarchy import linkage
   from scipy.spatial.distance import pdist

   # 4 个一维观测，便于手算
   X = np.array([[0.0], [1.0], [5.0], [9.0]])
   y = pdist(X)                       # 压缩距离矩阵

   Z_avg = linkage(y, method='average')
   print(Z_avg)
   ```
3. **手工核验第一次合并**：最近两点是 0 和 1（距离 1）。合并成簇 \(\{0,1\}\) 后，它到点 5 的 UPGMA 距离应为 \((d(0,5)+d(1,5))/2 = (5+4)/2 = 4.5\)；这正是 `_average` 在 `size_x=size_y=1` 时 `(1*5+1*4)/(1+1)=4.5` 的结果。
4. **预期结果**：`Z_avg` 中合并 \(\{0,1\}\) 与 \(\{5\}\) 那一行的距离列等于 4.5，与手算一致；若改用 `method='centroid'`，因 `size` 仍都是 1，`\_centroid` 化简为 `sqrt((d_xi^2+d_yi^2)/2 - d_xy^2/4)`，与 `_median` 数值相同（可自行打印对比）。
5. **若本地未装 scipy**：以上数值「待本地验证」，但代数推导（步骤 3 的等式）不依赖运行环境。

#### 4.2.5 小练习与答案

**练习 1**：用 Lance-Williams 标准形（含 \(\gamma|d_{xi}-d_{yi}|\) 那一项）写出 `single` 的系数，并手工化简到 `min(d_xi, d_yi)`。

**答案**：\(\alpha_x=\alpha_y=\tfrac12,\ \beta=0,\ \gamma=-\tfrac12\)。当 \(d_{xi}\le d_{yi}\) 时，\(|d_{xi}-d_{yi}|=d_{yi}-d_{xi}\)，于是
\(\tfrac12 d_{xi}+\tfrac12 d_{yi}-\tfrac12(d_{yi}-d_{xi})=d_{xi}=\min\)。\(d_{xi}>d_{yi}\) 时对称得 \(d_{yi}\)。故化简为 `min`。

**练习 2**：为什么 `_ward` 需要 `size_i` 而 `_centroid` 不需要？

**答案**：`_centroid` 计算的是「新簇心到 \(i\) 的簇心距离」，由平行四边形恒等式，它只依赖被合并两簇 \(x,y\) 与 \(i\) 的两两距离及 \(x,y\) 的大小（用于算新簇心），与 \(i\) 自己有多大无关。而 `_ward` 算的是「把 \(i\) 并入 \((x\cup y)\) 所减少的 Ward 方差」，方差增量按观测数加权，\(i\) 越大其方差贡献越大，故系数里出现 \(s_i\)。

---

### 4.3 `linkage_methods` 函数指针表与统一调用契约

#### 4.3.1 概念说明

函数指针表的真正威力在于「**调用契约统一**」。三种聚类算法——朴素的 `linkage`（O(n³)）、最近邻链 `nn_chain`（O(n²)，用于 complete/average/weighted/ward）、最小生成树 `mst_single_linkage`（O(n²)，专用于 single）——在「合并两簇后更新距离矩阵」这件事上，写出的代码**几乎一字不差**：都是「读出 `d_xi/d_yi/d_xy` 和三簇大小，调用 `new_dist(...)`，把结果写回 `D[i,y]`」。差别只在「怎么找到最近的两簇 \(x,y\)」，而距离更新这一步被完全抽离到了七个 `_xxx` 函数里。

这也解释了 u3-l2 提到的一个关键结论：方法到算法的分派依据是「方法的**可还原性（reducibility）**」，而不是方法名本身。`centroid / median` 在平方空间递推，合并后距离可能「倒挂」（新距离比旧的还小，即出现 reversal），不满足 nn_chain 所要求的 reducibility，因此只能走朴素的 `fast_linkage`；而 `ward` 虽在平方空间递推，却满足可还原性，所以仍能用 `nn_chain`。这套可还原性成立的数学前提，正是本讲的 Lance-Williams 系数。

#### 4.3.2 核心流程：三处调用点的同构

三个算法在「距离更新」这一段的结构完全同构：

```
new_dist = linkage_methods[method]          # 取函数指针（每算法各一次）

# 主循环里，每合并一对 (x, y) 后：
for 每个未合并簇 i/z:
    D[i, y] = new_dist(D[i, x],            # d_xi
                       D[i, y],            # d_yi
                       current_min,        # d_xy（本次合并距离）
                       nx, ny, ni)         # size_x, size_y, size_i
```

参数顺序严格遵循 4.1.3 的 typedef：`(d_xi, d_yi, d_xy, size_x, size_y, size_i)`。

#### 4.3.3 源码精读

**朴素 `linkage`**（取指针 + 调用）：

[hierarchy/_hierarchy.pyx:759-762](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L759-L762)

```cython
ni = 1 if id_i < n else <int>Z[id_i - n, 3]
D[condensed_index(n, i, y)] = new_dist(
    D[condensed_index(n, i, x)], D[condensed_index(n, i, y)],
    current_min, nx, ny, ni)
```

注意它如何取 `size`：原始观测（`id < n`）大小为 1，合并簇（`id >= n`）的大小取自 `Z[id-n, 3]`（即 linkage matrix 第四列）。`current_min` 是本次合并的距离 \(d(x,y)\)。

**`fast_linkage`**（同样形态，只是循环变量叫 `z`）：

[hierarchy/_hierarchy.pyx:891-893](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L891-L893)

```cython
D[condensed_index(n, z, y)] = new_dist(
    D[condensed_index(n, z, x)], D[condensed_index(n, z, y)],
    dist, nx, ny, nz)
```

**`nn_chain`**（也是同一形态）：

[hierarchy/_hierarchy.pyx:1017-1020](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1017-L1020)

```cython
D[condensed_index(n, i, y)] = new_dist(
    D[condensed_index(n, i, x)],
    D[condensed_index(n, i, y)],
    current_min, nx, ny, ni)
```

三段代码「换皮不换骨」——这正是函数指针表带来的复用：七种方法的数学差异被封装进七个 `_xxx`，而三种算法的工程差异（找最近对的方式）与数学差异（距离如何更新）被彻底解耦。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：确认三个算法的「距离更新」段在参数顺序与语义上完全一致。
2. **步骤**：
   - 在 `_hierarchy.pyx` 中分别定位 `linkage`（约 L759）、`fast_linkage`（约 L891）、`nn_chain`（约 L1017）三处 `new_dist(` 调用。
   - 对照 4.1.3 的 typedef，把每个实参标注成 `d_xi / d_yi / d_xy / size_x / size_y / size_i`。
3. **观察**：三处的第 3 个实参分别是 `current_min / dist / current_min`（都是本次合并距离 \(d(x,y)\)），后三个实参都是「被合并两簇大小 + 剩余簇大小」。语义完全同构。
4. **预期结果**：你能用同一张「参数对照表」解释这三段代码，无需分别记忆。
5. **延伸**：注意 `mst_single_linkage` **没有** `new_dist` 调用——因为 single linkage 等价于最小生成树，它直接用「到当前树的最小距离」`D[i] = min(D[i], dist(x,i))` 来增量维护，这正是 `_single = min` 的几何体现，只是被 MST 的 Prim 算法「内联」了。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `centroid / median` 必须用 `fast_linkage`，而 `ward` 可以用 `nn_chain`？

**答案**：`nn_chain` 算法的正确性依赖方法的「可还原性」（reducibility）：若某簇不是另一簇的最近邻，则合并后它仍不是。`centroid / median` 在平方空间递推、带负的 \(\beta\) 项，合并后到某簇的距离可能变小（出现 reversal/倒挂），破坏可还原性，故只能用「每步老老实实重找全局最近对」的朴素 `fast_linkage`。`ward` 虽然也带负 \(\beta\)，但其系数结构恰好满足可还原性，因此 `nn_chain` 对它仍正确。

**练习 2**：`mst_single_linkage` 里为什么找不到 `new_dist` 调用？

**答案**：single linkage 的距离更新是 \(d((x\cup y),i)=\min(d(x,i),d(y,i))\)，而 MST（Prim）维护的 `D[i]` 正是「点 \(i\) 到当前已合并集合的最近距离」。每加入一个新点 \(x\)，只需 `D[i] = min(D[i], dist(x,i))`——这等价于 `_single` 的递推，但被 Prim 的松弛操作直接内联，无需显式调用 `_single`。这是「single 享专属算法」的根本原因。

---

## 5. 综合实践

把本讲三块知识（统一框架、七个函数、函数指针表）串起来，完成一份「Lance-Williams 速查手册」：

1. **填表**：对照 `linkage()` 的 docstring（`hierarchy/_hierarchy_impl.py` 约 L773–L841 的七段数学定义），在 `_hierarchy_distance_update.pxi` 的每个 `_xxx` 函数上方写一行中文注释，写明：方法名、UPGMA/WPGMA/UPGMC/WPGMC 等别名、对应的 docstring 公式、属于「线性组」还是「平方-开方组」。（这是「阅读 + 标注」型任务，不要改函数体。）
2. **核验 `_average`**：在注释里明确写出「`_average = (size_x*d_xi + size_y*d_yi)/(size_x+size_y)` 与 UPGMA 定义 \(\frac{1}{|u||v|}\sum_{ij}d(u[i],v[j])\) 一致」，并用 4.2.4 的小数据集 `linkage(y, method='average')` 数值佐证。
3. **核验 `_ward`**：把 docstring 公式（\(T=|v|+|s|+|t|\)）与代码 `t = 1/(size_x+size_y+size_i)` 逐项对齐，标注三个系数 \((s_i+s_x)/T\)、\((s_i+s_y)/T\)、\(-s_i/T\)。
4. **画调用链**：画一张 `linkage() / nn_chain / fast_linkage  →  new_dist = linkage_methods[method]  →  七个 _xxx 之一` 的图，并在 `mst_single_linkage` 旁标注「内联了 `_single`，不走函数指针」。
5. **思考题**（写进手册末尾）：如果有人想新增第 8 种链接方法，需要改哪几处？（提示：`_LINKAGE_METHODS` 加一项、`.pxi` 加一个 `_xxx` 函数、`linkage_methods` 表追加一项、必要时更新 `_EUCLIDEAN_METHODS` 与 `linkage()` 的方法分派——主循环代码不用动。）

完成这份手册后，你应当能在不看源码的情况下，凭系数表默写出任意一种方法的距离更新式，并说清它为什么走哪个后端算法。

## 6. 本讲小结

- 凝聚式聚类「合并两簇后到其余簇的距离」可用**统一的 Lance-Williams 递推式** \(d((x\cup y),i)=\alpha_x d_{xi}+\alpha_y d_{yi}+\beta d_{xy}+\gamma|d_{xi}-d_{yi}|\) 表达，七种方法只是系数不同。
- `linkage_distance_update` 这个**函数指针 typedef** 把七种方法钉成统一签名 `(d_xi, d_yi, d_xy, size_x, size_y, size_i) → double`，即使多数函数用不到全部 6 个参数——统一签名是函数指针表的前提。
- 七个 `_xxx` 函数分两组：`single/complete/average/weighted` 是真实距离的**线性**组合；`centroid/median/ward` 在**平方距离**上递推再 `sqrt`，故只允许欧氏度量（即 `_EUCLIDEAN_METHODS`）。
- `_average = (size_x*d_xi + size_y*d_yi)/(size_x+size_y)` 与 UPGMA 定义一致；`_ward` 的开方与 `size_i` 加权源自 Ward 方差增量，\(T=s_x+s_y+s_i\)。
- `linkage_methods` **函数指针表**按 0..6 与 `_LINKAGE_METHODS` 一一对应，让 `linkage / nn_chain / fast_linkage` 三处以**同一调用契约**复用距离更新代码；`mst_single_linkage` 把 `_single=min` 内联进 Prim 松弛，是唯一例外。
- 方法到算法的分派依据是「**可还原性**」而非方法名：`centroid/median` 因平方递推可能距离倒挂、破坏可还原性，只能用 `fast_linkage`；`ward` 满足可还原性，仍可用 `nn_chain`。

## 7. 下一步学习建议

本讲搞定了「距离更新的数学」，接下来建议：

- **u4-l1**：进入 `fast_linkage` 依赖的底层——压缩距离矩阵的下标编码 `condensed_index`、支持改值的最小堆 `Heap`（`_structures.pxi`）与并查集 `LinkageUnionFind`。你会看到 `new_dist` 的结果如何被写回压缩矩阵、堆如何加速「找最近对」。
- **u4-l2 / u4-l3**：分别精读 `mst_single_linkage`（看 `_single` 如何被 Prim 内联）与 `nn_chain` / `fast_linkage`（看 `new_dist` 的统一调用契约在三处如何落地，并印证本讲的可还原性讨论）。
- 若想横向对照 Lance-Williams 系数，可阅读 Müllner 的论文 *"Modern hierarchical, agglomerative clustering algorithms"*（arXiv:1109.2378），正是 `fast_linkage` docstring 引用的 [1]，里面对 reducibility 有严格证明。

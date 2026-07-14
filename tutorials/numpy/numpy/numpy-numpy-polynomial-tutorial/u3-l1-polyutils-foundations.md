# polyutils 工具函数基石

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `polyutils.py` 在整个 `numpy.polynomial` 子包中的「地基」地位：它为六大正交族模块提供共享的输入规整、去零、坐标映射与浮点格式化工具。
- 理解 `as_series` 如何把任意混合输入统一规整成「同 dtype 的 1-D 数组列表」，并能解释 `[c] = pu.as_series([c])` 这一全包通用惯用法。
- 区分 `trimseq`（结构去零）与 `trimcoef`（数值去零，带容差 `tol`）两套去尾零机制的本质差异。
- 掌握 `getdomain / mapdomain / mapparms` 三者如何共同实现 domain→window 的线性映射，并能在源码层面把它们与 `_polybase.py` 里的 `__call__ / roots / fit` 对应起来。
- 理解 `format_float` 作为打印子系统底层原语的作用，以及它如何尊重 NumPy 全局打印选项。

本讲是「幂级数实现细节」单元的第一篇，定位是**打地基**：后续 u3-l2 到 u3-l6 讲到的创建、算术、求值、微积分、拟合、求根，几乎每一处都会回调本讲的这些工具函数。

## 2. 前置知识

本讲假设你已经掌握前置讲义 u1-l4 建立的认知：

- **三层委托链**：便捷类（`_polybase.py`）→ 函数式 API（如 `polynomial.py` 的 `polyadd`）→ 通用工具（`polyutils.py` 的 `_add` 等）。本讲进入这条链的最底层。
- **「前缀=基、后缀=功能」命名规律**：`polytrim = pu.trimcoef` 这种模块级别名，正是为了让 `polynomial.py` 暴露一个符合命名规律的名字，同时复用 `polyutils` 的共享实现。
- **系数升幂约定**：`c[i]` 是第 `i` 次项的系数，`p(x)=c[0]+c[1]·x+c[2]·x²+…`。

此外，你需要一点关于 **domain/window 双区间** 的直觉——这部分在 u2-l2 已建立。本讲**不重复**它的设计动机，而是聚焦于支撑它的三个底层算术函数的**实现细节**。如果你对「为什么要做 domain→window 映射」还不清楚，建议先读 u2-l2。

通俗解释两个本讲会用到的术语：

- **规整（normalize）**：把形形色色的输入（Python 标量、列表、元组、各种 dtype 的 ndarray）统一变成同一种「干净」的内部表示。
- **仿射映射（affine map）**：形如 \(L(x)=\text{off}+\text{scl}\cdot x\) 的变换，它把一条线段等比例地搬到另一条线段。本讲的核心数学就是它。

## 3. 本讲源码地图

本讲只涉及一个核心源文件，但会跨文件引用它的调用点：

| 文件 | 作用 | 本讲用到的地方 |
|------|------|----------------|
| [polyutils.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py) | 全包共享工具集，本讲的主角 | `__all__`、`trimseq`、`as_series`、`trimcoef`、`getdomain`、`mapparms`、`mapdomain`、`format_float` |
| [polynomial.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py) | 幂级数函数式 API 与便捷类，是 `polyutils` 的直接消费者 | `polytrim = pu.trimcoef`、`polyadd`、`polyval` 等对 `pu` 的委托调用 |
| [_polybase.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py) | 抽象基类 ABCPolyBase，在更高层调用这些工具 | `__call__`、`roots`、`fit`、`mapparms` 方法 |

`polyutils.py` 的模块文档字符串和 `__all__` 一起，构成了它的公开契约：

[polyutils.py:27-29](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L27-L29) 列出了七个公开名字：`as_series`、`trimseq`、`trimcoef`、`getdomain`、`mapdomain`、`mapparms`、`format_float`。本讲按「规整 → 去零 → 映射 → 格式化」四个最小模块逐一拆解。

注意：`polyutils.py` 里还有一批**下划线开头**的私有函数（`_add`、`_sub`、`_div`、`_pow`、`_fit`、`_fromroots`、`_valnd`、`_gridnd`、`_vander_nd` 等），它们是六大正交族函数式 API 的共享实现骨架，属于后续讲义（u3-l2、u3-l3、u3-l5）的内容，本讲只在「源码地图」里点到为止。

## 4. 核心概念与源码讲解

### 4.1 as_series：输入规整与公共 dtype

#### 4.1.1 概念说明

`as_series(alist, trim=True)` 是整个子包的**输入总闸**。任何接收系数数组的函数——`polyadd`、`polyval`、`polyder`、`polyfit`……——最终都会把传入的系数送进 `as_series`。

它解决三个问题：

1. **形状不一**：调用者可能传 Python 标量 `2`、元组 `(1,2,3)`、列表、各种 dtype 的 ndarray。`as_series` 把它们全部变成「1-D ndarray」。
2. **dtype 不齐**：两个相加的多项式可能一个是 int、一个是 float、甚至一个是 `Decimal`（object dtype）。`as_series` 把它们提升到**同一个公共 dtype**。
3. **尾部冗余零**：`[1, 2, 0, 0]` 和 `[1, 2]` 在数学上是同一个多项式，但长度不同会让后续逐项运算变复杂。默认 `trim=True` 时去掉这些尾部零。

关键惯用法：全包随处可见的 `[c] = pu.as_series([c])`，意思是「把单个系数数组包进列表、规整、再解包成单个数组」。这一行同时完成了「转 1-D、统一 dtype、去尾零、保证非空」四件事。

#### 4.1.2 核心流程

`as_series` 的执行可以概括为四步：

```text
输入 alist（任意可迭代，元素为 array_like）
   │
   ▼  每个元素 np.array(a, ndmin=1)        ← 标量变成长度1的1-D数组
[数组列表]
   │
   ▼  校验：size != 0 且 ndim == 1          ← 否则抛 ValueError
   ▼  (可选) 对每个数组调用 trimseq 去尾零
   │
   ▼  np.common_type(*arrays) 求公共浮点/复数 dtype
   │      ├─ 成功 → 全部复制到该 dtype
   │      └─ 失败(含 object dtype) → 退化为 object dtype
   │            └─ 若没有任何数组是 object dtype → 抛 "no common type"
   ▼
返回 [1-D 数组列表]
```

注意 `np.common_type` 只返回**非精确浮点类型**（float64 / complex128 等），它不接受 object dtype。这正是后面 `try/except` 分支存在的根本原因：当输入里混有 `Decimal` 等 object 类型时，`common_type` 会抛异常，于是退而求其次把所有数组都转成 object dtype（前提是至少有一个数组本来就是 object dtype，否则报错）。

#### 4.1.3 源码精读

第一步——把每个元素变成 1-D 数组：

[polyutils.py:114](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L114) 用 `ndmin=1` 保证标量（如 `2`）被提升成长度为 1 的数组 `array([2])`，而非零维数组。

第二步——校验空数组与非 1-D：

[polyutils.py:115-119](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L115-L119) 遍历检查 `size==0`（空）和 `ndim!=1`（如传了二维矩阵却没先 reshape），任一不满足都抛 `ValueError`。这也是文档里「2-D 输入必须先 reshape」的来源——注意 `as_series` 的「按行解析 2-D」语义其实是由调用方决定的，函数内部一律拒绝 ndim>1。

第三步——可选去尾零：

[polyutils.py:120-121](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L120-L121) 仅当 `trim=True` 时对每个数组调用 `trimseq`（见 4.2）。这就是实践任务里 `trim=True/False` 差别的根源。

第四步——公共 dtype 与 object 退化：

[polyutils.py:123-141](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L123-L141) 是本函数最精妙的部分。`try` 分支用 `np.common_type` 求公共类型；`except` 分支处理 object dtype 退化。看这两个关键判断：

- [polyutils.py:130](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L130) 「如果当前数组还不是 object dtype，就新建一个 object 数组把数据拷过去」——这是把数值数组「升格」为 object，以便和 `Decimal` 共存。
- [polyutils.py:137-138](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L137-L138) 「如果从头到尾没有任何一个数组本来就是 object dtype，却还是触发了异常，那才是真的『没有公共类型』」——比如传入了两个互不兼容的 object 子类型时。

最后 [polyutils.py:140](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L140) 用 `copy=True` 复制到公共 dtype，**保证返回的是副本**，这样后续就地运算（如 `_add` 里的 `c1[:c2.size] += c2`）不会污染调用者的原始数组。

调用点示例：`polynomial.polyadd` 把两个系数交给 `pu._add`，而 `pu._add` 第一行就是 `as_series`：

[polyutils.py:555-558](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L555-L558)，以及上层 [polynomial.py:249](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L249)（`return pu._add(c1, c2)`）。`polyval` 内部同样有 [polynomial.py:320](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L320) 的 `[c] = pu.as_series([c])`。

#### 4.1.4 代码实践

**实践目标**：亲手感受 `as_series` 的「混合输入规整」「trim 开关差异」「dtype 提升」三件事。

**操作步骤**：

```python
import numpy as np
from numpy.polynomial import polyutils as pu
from decimal import Decimal

# 1) 混合输入：标量 + 列表，默认 trim=True
print(pu.as_series([2, [1.1, 0.0]]))
# 2) 同样的输入，但 trim=False
print(pu.as_series([2, [1.1, 0.0]], trim=False))
# 3) 整型输入会被提升成浮点
a = pu.as_series([[1, 2, 3]])
print(a, a[0].dtype)
# 4) 与 Decimal 混合 → object dtype 退化
d = pu.as_series([[Decimal("0.1"), Decimal("0.2")], [1, 2]])
print(d, [x.dtype for x in d])
```

**需要观察的现象 / 预期结果**：

- 第 1 行应得到 `[array([2.]), array([1.1])]`——`[1.1, 0.0]` 的尾部 `0.0` 被去掉。这正是 `polyutils.py` 文档字符串里给出的真实示例（见源码 [polyutils.py:107-108](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L107-L108)），属于 NumPy doctest 验证过的结果。
- 第 2 行应得到 `[array([2.]), array([1.1, 0.])]`，尾部零被保留——印证 [polyutils.py:120-121](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L120-L121) 的 `trim` 分支（对应源码示例 [polyutils.py:110-111](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L110-L111)）。
- 第 3 行 dtype 应为 `float64`，体现 `np.common_type` 把整型提升为非精确浮点。
- 第 4 行两个数组都应为 `object` dtype，且 `Decimal` 与 int 能共存——这是 `except` 分支 [polyutils.py:123-138](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L123-L138) 在起作用。

具体数值以本地运行为准（**待本地验证**），但上述定性结论可由源码直接推出。

#### 4.1.5 小练习与答案

**练习 1**：`pu.as_series([[1, 2, 3]])` 返回的数组为什么是 `float64` 而非 `int64`，即使输入是整数？

**参考答案**：因为 [polyutils.py:124](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L124) 用的是 `np.common_type`，它按设计只返回非精确（浮点/复数）类型，会把整型提升为 float64。这是为了让后续的除法、拟合等运算有合理的数值类型。

**练习 2**：解释 `[c] = pu.as_series([c])` 这一行的三个动作。

**参考答案**：把单个系数 `c` 包进列表 `[c]` 传入；`as_series` 对它做「转 1-D + 校验非空 + 去 tail 零 + 统一 dtype + 复制」；再用 `[c] =` 解包回单个数组。这一行是全包对「单个系数输入」做规整的标准写法。

**练习 3**：如果把两个**互不兼容**的 object 子类型（既非数值也非彼此可转换）传给 `as_series`，会发生什么？

**参考答案**：`np.common_type` 抛异常进入 `except` 分支，但因为没有任何一个数组本身就是 object dtype（假设都是某种自定义 object），`has_one_object_type` 保持 `False`，最终在 [polyutils.py:137-138](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L137-L138) 抛出 `ValueError("Coefficient arrays have no common type")`。

---

### 4.2 trimseq 与 trimcoef：两套去尾零机制

#### 4.2.1 概念说明

子包里有两个名字相近、用途不同的「去尾零」函数，初学者极易混淆：

- **`trimseq(seq)`**：**结构性**去零。它只看「值是否严格等于 0」，从尾部砍掉连续的零。它是 `as_series` 默认调用的函数（见 4.1）。它**不复制、不改 dtype**，文档明确写道「Do not lose the type info」。
- **`trimcoef(c, tol=0)`**：**数值性**去零。它砍掉的是「绝对值 ≤ `tol`」的尾部系数，用来清理拟合/运算产生的**接近零**的高次项。默认 `tol=0` 时退化为「只砍精确零」，但仍与 `trimseq` 有细微差别（见下）。

两者的「保底」约定相同：若结果会变空，就保留一个零，保证返回的多项式至少有一项（代表零多项式 `[0]`）。

为什么需要两套？`trimseq` 用于把输入「规整成最短规范形」，速度快、不复制；`trimcoef` 用于「数值清洗」，需要先 `as_series` 规整、再按容差判断、还要 `.copy()` 返回干净副本。

#### 4.2.2 核心流程

`trimseq` 的流程：

```text
若 seq 为空 或 最后一项 != 0 → 原样返回 seq
否则从尾部向前扫，找到最后一个 != 0 的下标 i
返回 seq[:i+1]   ← 若全程没找到非零，i 停在 0，返回 seq[:1]（单个零）
```

`trimcoef` 的流程：

```text
若 tol < 0 → 抛 ValueError
[c] = as_series([c])              ← 先规整（顺带保证非空）
ind = nonzero(|c| > tol)          ← 注意是严格大于 >
若 ind 为空 → 返回 c[:1] * 0      ← 单个零，且保留 dtype
否则 → 返回 c[:ind[-1]+1].copy()  ← 截到最后一个超阈值的系数
```

一个关键细节：`trimcoef` 用的是**严格大于** `> tol`（见 [polyutils.py:188](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L188)），因此**恰好等于 tol** 的系数也会被砍掉。

#### 4.2.3 源码精读

`trimseq` 的全部逻辑只有几行：

[polyutils.py:54-60](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L54-L60)。重点看「全零」情况的巧妙处理：当所有元素都是 0 时，`for` 循环走到底也不会 `break`，循环变量 `i` 停在 `0`（`range` 的最后一个值），于是 `seq[:0+1]` 即 `seq[:1]`，恰好返回第一个零元素——这就是文档承诺的「If the resulting sequence would be empty, return the first element」。

`trimcoef` 的逻辑：

[polyutils.py:184-192](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L184-L192)。三个要点：

1. [polyutils.py:187](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L187) 先 `[c] = as_series([c])` 规整——注意这里**没有**传 `trim=False`，所以默认会先用 `trimseq` 去掉精确零，再做容差判断。
2. [polyutils.py:188](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L188) 用 `np.abs(c) > tol` 找「显著」系数的下标，严格大于。
3. [polyutils.py:190](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L190) 全空时返回 `c[:1] * 0`——乘以 `0` 而非直接写 `np.array([0.])`，是为了**保留原 dtype**（例如复数系数会得到 `array([0.+0.j])`）。

「恰好等于 tol 被砍」的真实示例来自源码 docstring：

[polyutils.py:177-178](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L177-L178) 给出 `trimcoef((0,0,1e-3,0,1e-5,0,0), 1e-3)` 的结果为 `array([0.])`——因为 `1e-3` 不满足 `> 1e-3`，被砍，剩下的全零塌缩成单个零。

最后看它在 `polynomial.py` 里的别名，这是 u1-l4 提到的「模块级别名」的源码出处：

[polynomial.py:90](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L90) `polytrim = pu.trimcoef`——为了让幂级数模块拥有一个 `poly` 前缀的名字、符合「前缀=基」命名规律，同时复用 `polyutils` 的共享实现。六大正交族每个模块都有自己的 `<前缀>trim = pu.trimcoef` 别名。

#### 4.2.4 代码实践

**实践目标**：对比 `trimseq` 与 `trimcoef`，并验证「恰好等于 tol 被砍」这一容易踩坑的行为。

**操作步骤**：

```python
import numpy as np
from numpy.polynomial import polyutils as pu

c = np.array([0.0, 0.0, 3.0, 0.0, 5.0, 0.0, 0.0])

# (a) trimseq：只砍精确零
print(pu.trimseq(c))                 # 期望 [0, 0, 3, 0, 5]

# (b) trimcoef 默认 tol=0：与 trimseq 结果一致（结构层面）
print(pu.trimcoef(c))                # 期望 [0, 0, 3, 0, 5]

# (c) trimcoef 带容差：恰好等于 tol 会被砍
print(pu.trimcoef((0, 0, 1e-3, 0, 1e-5, 0, 0), 1e-3))   # 期望 [0.]

# (d) trimcoef 处理复数：保留 dtype
print(pu.trimcoef((3e-4, 1e-3*(1-1j), 5e-4, 2e-5*(1+1j)), 1e-3))
```

**需要观察的现象 / 预期结果**：

- (a)(b) 都应得到 `array([0., 0., 3., 0., 5.])`——注意中间的 `0`（在 `5` 之前）**不**被砍，因为只砍**尾部连续**零；这与源码示例 [polyutils.py:175-176](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L175-L176) 一致。
- (c) 应得到 `array([0.])`，印证严格 `>` 的语义（源码示例 [polyutils.py:177-178](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L177-L178)）。
- (d) 应得到两个复数系数，dtype 为 complex（源码示例 [polyutils.py:179-181](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L179-L181)）。

具体数值**待本地验证**，定性结论由源码确定。

#### 4.2.5 小练习与答案

**练习 1**：对 `c = [1, 0, 0]` 分别调用 `trimseq` 和 `trimcoef`，结果分别是什么？

**参考答案**：两者都返回 `array([1.])`。`trimseq` 直接砍掉两个尾部零；`trimcoef` 先经 `as_series` 规整（默认也会 `trimseq`），再用 `tol=0` 判断，结果相同。

**练习 2**：为什么 `trimcoef` 在全空时返回 `c[:1] * 0` 而不是 `np.array([0.0])`？

**参考答案**：为了**保留输入 dtype**。`c[:1] * 0` 会得到与 `c` 同 dtype 的零元素（如复数输入得到 `0.+0.j`），而写死 `array([0.0])` 会把复数降级为 float。见 [polyutils.py:190](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L190)。

**练习 3**：`trimcoef` 与 `trimseq` 在「默认 tol=0」下结果是否完全等价？

**参考答案**：在大多数情况下结果一致，但并非完全等价。`trimcoef` 会先经 `as_series`（统一 dtype 并复制），且对「全零」用 `c[:1]*0` 构造零、对非空用 `.copy()` 返回副本；`trimseq` 则尽量返回视图、不改 dtype。因此语义上 `trimcoef` 是「数值清洗 + 返回干净副本」，`trimseq` 是「结构裁剪 + 轻量」。

---

### 4.3 getdomain / mapdomain / mapparms：坐标映射三件套

#### 4.3.1 概念说明

这三个函数是 u2-l2 讲过的「domain ↔ window 线性映射」的**底层算术实现**。u2-l2 讲了**为什么**要映射、映射方向如何（口诀「输入往 window 走，输出往 domain 回」）；本讲讲**怎么算**。

- **`mapparms(old, new)`**：给定两个区间 `old=[old0, old1]`、`new=[new0, new1]`，求出把它们对应起来的仿射映射 \(L(x)=\text{off}+\text{scl}\cdot x\) 的两个参数，返回元组 `(off, scl)`。要求 `L(old0)=new0`、`L(old1)=new1`。
- **`mapdomain(x, old, new)`**：把 `mapparms` 求出的映射**作用到一组点** `x` 上，返回映射后的点集。
- **`getdomain(x)`**：给定一组横坐标 `x`，返回一个「合适的」2 元素 domain——实数时是最小区间 `[min, max]`，复数时是包围所有点的最小轴对齐矩形的左下、右上两个角。

合起来：`getdomain` 帮你「猜」一个 domain，`mapparms` 算映射参数，`mapdomain` 把点搬过去。`fit` 正是这三者的典型用户——先用 `getdomain` 取数据范围，再用 `mapdomain` 把数据搬到 `window` 上做最小二乘（这是数值稳定的关键，详见 u5-l1）。

#### 4.3.2 核心流程

**`mapparms` 的数学**：设 \(L(x)=\text{off}+\text{scl}\cdot x\)，由两个边界条件：

\[
\begin{cases}
\text{off}+\text{scl}\cdot old_0 = new_0 \\
\text{off}+\text{scl}\cdot old_1 = new_1
\end{cases}
\]

两式相减得：

\[
\text{scl} = \frac{new_1-new_0}{old_1-old_0} = \frac{\text{newlen}}{\text{oldlen}}
\]

代回求 off：

\[
\text{off} = new_0 - \text{scl}\cdot old_0 = \frac{old_1\cdot new_0 - old_0\cdot new_1}{\text{oldlen}}
\]

（最后一个等式可通过展开并消去交叉项 \(old_0\cdot new_0\) 得到，与源码 [polyutils.py:284](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L284) 完全一致。）

**`mapdomain` 的数学**：文档（[polyutils.py:316-324](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L316-L324)）给出的等价形式是：

\[
x_{\text{out}} = new_0 + m\,(x - old_0),\qquad m=\frac{new_1-new_0}{old_1-old_0}
\]

容易验证它等于 \(\text{off}+\text{scl}\cdot x\)。源码实现直接复用 `mapparms` 的结果：`return off + scl * x`。

**`getdomain` 的分支**：

```text
用 as_series 规整 x（trim=False，保留极值点）
若 x 是复数 dtype:
    取 real 的 min/max 与 imag 的 min/max
    返回 [complex(rmin, imin), complex(rmax, imax)]   ← 包围矩形两角
否则:
    返回 [x.min(), x.max()]                            ← 最小区间两端
```

#### 4.3.3 源码精读

`mapparms` 的实现极简：

[polyutils.py:282-286](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L282-L286)。三行分别算 `oldlen`、`newlen`、`off`、`scl`，对应上面的公式。注意它**不做除零检查**——若 `old==new`（两点重合，oldlen=0）会自然抛 `ZeroDivisionError`，这是有意为之的「让错误暴露」风格。

`mapdomain` 的实现：

[polyutils.py:352-355](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L352-L355)。重点在 [polyutils.py:352](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L352) 的类型守卫：只有当 `x` 不是 Python 标量、也不是 `np.generic` 标量时，才用 `np.asanyarray(x)` 转换。`asanyarray`（而非 `asarray`）会**保留 ndarray 子类型**（如 matrix），与文档承诺一致。然后 `off + scl * x` 对标量直接算、对数组按广播算。

`getdomain` 的分支：

[polyutils.py:233-239](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L233-L239)。先 [polyutils.py:233](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L233) 用 `as_series([x], trim=False)` 规整——注意这里**显式 `trim=False`**，否则尾部恰好为零的极值点可能被误删，导致 domain 算错。复数判断用 `x.dtype.char in np.typecodes['Complex']`（[polyutils.py:234](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L234)），分别对 `.real` 和 `.imag` 取极值。

**把它们与 ABCPolyBase 串起来**（u2-l2 的上层视角，这里给源码坐标）：

- 求值 `__call__`：[_polybase.py:511](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L511) `arg = pu.mapdomain(arg, self.domain, self.window)` ——输入 domain→window。
- 求根 `roots`：[_polybase.py:913](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L913) `return pu.mapdomain(roots, self.window, self.domain)` ——输出 window→domain（反向）。
- 拟合 `fit`：[_polybase.py:1015](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L1015) `domain = pu.getdomain(x)`，再 [_polybase.py:1025](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L1025) `xnew = pu.mapdomain(x, domain, window)`——正是「猜 domain → 搬到 window」的完整链路。
- 便捷类的 `mapparms()` 方法：[_polybase.py:843](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L843) `return pu.mapparms(self.domain, self.window)`——把 self 的 domain/window 直接交给底层。

#### 4.3.4 代码实践

**实践目标**：用 `getdomain` 对一组随机点求合适 domain，再用 `mapdomain` 把它映射到 `[-1,1]`，并验证映射的「端点对齐」性质。

**操作步骤**：

```python
import numpy as np
from numpy.polynomial import polyutils as pu

# 1) 一组分布在 [10, 20) 的随机点
rng = np.random.default_rng(0)
x = rng.uniform(10, 20, size=8)

# 2) getdomain 求合适 domain
dom = pu.getdomain(x)
print("domain =", dom)                       # 期望接近 [min(x), max(x)]

# 3) 读出把 dom -> [-1,1] 的仿射参数
off, scl = pu.mapparms(dom, (-1.0, 1.0))
print("off, scl =", off, scl)

# 4) 把点映射到 [-1,1]
xw = pu.mapdomain(x, dom, (-1.0, 1.0))
print("mapped min/max =", xw.min(), xw.max())  # 期望接近 -1 / 1

# 5) 验证端点对齐：dom 的两端应分别映到 -1 和 1
print(pu.mapdomain(dom, dom, (-1.0, 1.0)))     # 期望 [-1., 1.]
```

**需要观察的现象 / 预期结果**：

- `dom` 应近似等于 `[min(x), max(x)]`（实数分支 [polyutils.py:238-239](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L238-L239)）。
- `mapped min/max` 应非常接近 `-1.0` 和 `1.0`——因为 `getdomain` 取的就是数据端点，映射后必落在新区间端点上。
- 第 5 步应精确得到 `[-1., 1.]`（由 `mapparms` 的定义保证）。

具体随机值**待本地验证**，但「端点对齐」是数学上必然的。

#### 4.3.5 小练习与答案

**练习 1**：`pu.mapparms((-1, 1), (-1, 1))` 返回什么？为什么？

**参考答案**：返回 `(0.0, 1.0)`——恒等映射。因为 old==new，`newlen/oldlen = 1`，`off = new0 - scl·old0 = -1 - 1·(-1) = 0`。对应源码示例 [polyutils.py:273-274](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L273-L274)。这解释了为何默认 domain==window 时求值不需要任何缩放。

**练习 2**：为什么 `getdomain` 内部调用 `as_series` 时要传 `trim=False`？

**参考答案**：因为极值点（min/max）可能恰好是 0，若 `trim=True` 会把尾部的零当冗余删掉，导致算出的 domain 缺失真实的边界。见 [polyutils.py:233](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L233)。

**练习 3**：复数输入 `c = np.exp(1j*np.pi*np.arange(12)/6)`（单位圆上的点）经 `getdomain` 后返回什么？

**参考答案**：返回 `array([-1.-1.j, 1.+1.j])`——单位圆点集的实部范围是 `[-1,1]`、虚部范围也是 `[-1,1]`，故包围矩形的左下角 `complex(-1,-1)`、右上角 `complex(1,1)`。对应源码示例 [polyutils.py:228-230](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L228-L230)。

---

### 4.4 format_float：打印子系统的浮点格式化原语

#### 4.4.1 概念说明

`format_float(x, parens=False)` 是 ABCPolyBase 打印管线（u2-l4 已讲其上层）的**最底层原语**：给定一个系数（标量），返回一段用于 `__str__`/`__repr__`/`_repr_latex_` 的字符串。它解决两个问题：

1. **尊重全局打印选项**：读取 `np.get_printoptions()`，让多项式打印风格与用户 `np.set_printoptions(...)` 的设置（precision、sign、floatmode、nanstr、infstr）保持一致。
2. **正确的最短表示**：用 Dragon4 算法（`dragon4_positional` / `dragon4_scientific`）生成「正确且最短」的浮点字符串，而不是 C 库的 `printf` 风格。

`parens=True` 会在科学计数法输出外层包一对括号，这是多项式打印时的语法需要（避免如 `1.5e-10` 这样的系数在拼接时产生歧义）。

它对**非浮点**输入（如 int、object）直接返回 `str(x)`，因此 `Polynomial([1,2,3])` 的整数系数也能正确显示。

#### 4.4.2 核心流程

```text
x 是否为 np.floating 子类型?
   否 → 直接返回 str(x)
是 NaN 或 Inf? → 返回全局 nanstr / infstr
判断是否用科学计数法 exp_format:
   |x| >= 1e8  或  |x| 小于一个随 precision 变化的阈值 → 科学计数法
取 floatmode:
   'fixed' → trim='k', unique=False   ← 固定宽度
   其他    → trim='0', unique=True    ← 最短正确表示
exp_format?
   是 → dragon4_scientific(...) ; 若 parens → 外包括号
   否 → dragon4_positional(...)
返回字符串
```

#### 4.4.3 源码精读

非浮点守卫：

[polyutils.py:728-729](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L728-L729)——`np.issubdtype(type(x), np.floating)` 检查的是 **Python 类型** `type(x)` 是否属于 floating 族。Python 内置 `int`、`Decimal`、`complex` 等都不是，于是直接 `str(x)` 返回。这就是为什么整数系数打印成 `1` 而非 `1.0`。

NaN/Inf 与全局选项：

[polyutils.py:731-736](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L731-L736) 读取 `np.get_printoptions()`，对 NaN/Inf 返回用户配置的 `nanstr`/`infstr`（默认 `'nan'`/`'inf'`，可被 `np.set_printoptions` 改写）。

科学计数法判定：

[polyutils.py:738-742](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L738-L742)——当 `|x| >= 1e8`，或 `|x|` 小于一个由 `precision` 决定的阈值时，切到科学计数法。注意这里 `x != 0` 的前置判断（避免对 0 取 `abs` 后误判，也跳过 0 的科学计数）。

floatmode 与 Dragon4 调用：

[polyutils.py:744-746](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L744-L746) 根据 `floatmode` 调整 `trim`/`unique`：`'fixed'` 模式下要求定宽（`trim='k'`、不取最短），其余模式取最短正确表示（`trim='0'`、`unique=True`）。

[polyutils.py:748-758](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L748-L758) 分别调用 `dragon4_scientific`（科学）或 `dragon4_positional`（定点），并把 `sign=opts['sign']=='+'` 透传——即若用户 `np.set_printoptions(sign='+')`，正数也会带 `+` 号。`parens=True` 仅在科学分支 [polyutils.py:752-753](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L752-L753) 生效。

上层调用点（属于 u2-l4 / u5-l2 的打印管线，这里给出坐标）：

- [_polybase.py:357](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L357) 格式化常数项；
- [_polybase.py:375](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L375) 与 [_polybase.py:377](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L377) 用 `parens=True` 格式化各次项系数。

#### 4.4.4 代码实践

**实践目标**：观察 `format_float` 如何随「输入类型」与「全局打印选项」变化，并理解 `parens` 的作用。

**操作步骤**：

```python
import numpy as np
from numpy.polynomial import polyutils as pu

# (a) 非浮点输入：直接 str()
print(pu.format_float(2))            # 期望 "2"
print(pu.format_float(2.0))          # 期望 "2.0"（浮点走 Dragon4）

# (b) 大数触发科学计数法
print(pu.format_float(1.5e9))        # 期望科学计数法
print(pu.format_float(1.5e9, parens=True))   # 期望外层包括号

# (c) NaN / Inf 走全局字符串
print(pu.format_float(np.nan))
print(pu.format_float(np.inf))

# (d) 修改全局 precision 观察变化
np.set_printoptions(precision=3)
print(pu.format_float(1.0/3.0))      # 期望受 precision=3 影响
np.set_printoptions(precision=8)     # 还原
```

**需要观察的现象 / 预期结果**：

- (a) `format_float(2)` 返回 `"2"`（int 走 `str`），`format_float(2.0)` 返回 `"2.0"`（float 走 Dragon4）——印证 [polyutils.py:728-729](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L728-L729) 的类型守卫。
- (b) `1.5e9` 因 `>= 1e8` 触发科学计数法；`parens=True` 时输出形如 `(1.5e+09)`——印证 [polyutils.py:752-753](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L752-L753)。
- (c) NaN/Inf 返回当前 `nanstr`/`infstr`（默认 `'nan'`/`'inf'`）——印证 [polyutils.py:733-736](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L733-L736)。
- (d) `1/3` 在 `precision=3` 下位数变少——印证它读取 `np.get_printoptions()`。

精确字符串形式**待本地验证**（依赖 NumPy 版本与 Dragon4 细节）。

#### 4.4.5 小练习与答案

**练习 1**：`format_float(3)` 为什么返回 `"3"` 而不是 `"3.0"`？

**参考答案**：因为 `3` 是 Python `int`，不属于 `np.floating` 子类型，[polyutils.py:728-729](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L728-L729) 直接走 `str(x)` 分支。这让整型系数的多项式打印更干净。

**练习 2**：`parens=True` 在什么情况下才会真正加上括号？

**参考答案**：仅当进入**科学计数法分支**时才加（[polyutils.py:752-753](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L752-L753)）。定点分支不受 `parens` 影响。这是因为科学计数法的 `e` 在多项式拼接里容易和变量符号混淆，需要括号隔离。

**练习 3**：如果用户执行了 `np.set_printoptions(sign='+')`，`format_float(2.5)` 会变成什么？

**参考答案**：会返回带正号的字符串（如 `+2.5`）。因为 [polyutils.py:751](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L751) 与 [polyutils.py:758](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L758) 把 `sign=opts['sign']=='+'` 透传给了 Dragon4。

---

## 5. 综合实践

**综合任务**：用本讲的四个工具，亲手模拟一遍 `Polynomial.fit` 内部「猜 domain → 搬到 window → 在 window 上工作」的坐标预处理链路（拟合本身留给 u3-l5，这里只做坐标搬移）。

**背景**：u2-l2 讲过 `fit` 会用 `getdomain` 取数据范围、再用 `mapdomain` 搬到 `window`。本任务让你在「裸函数」层面复现这一步，并体会它为何能改善数值稳定性（详细对比见 u5-l1）。

**操作步骤**：

```python
import numpy as np
from numpy.polynomial import polyutils as pu

# 1) 构造一组横坐标在 [100, 110] 上的「真实数据」
x = np.linspace(100, 110, 11)

# 2) 用 getdomain 求它的自然 domain
dom = pu.getdomain(x)
print("natural domain:", dom)

# 3) 读出 dom -> [-1, 1] 的仿射参数 off, scl
window = np.array([-1.0, 1.0])
off, scl = pu.mapparms(dom, window)
print("off, scl:", off, scl)

# 4) 用 mapdomain 把数据搬到 [-1, 1]
xw = pu.mapdomain(x, dom, window)
print("window coords:", xw)

# 5) 用 as_series 规整一段「待拟合」的系数（模拟 fit 返回的系数清洗）
raw_coef = [1e-12, 2.0, 3.0, 0.0, 0.0]
print("as_series(trim=True) :", pu.as_series([raw_coef]))
print("trimcoef(tol=1e-6)   :", pu.trimcoef(raw_coef, 1e-6))
```

**需要观察的现象 / 解释**：

- `dom` 应为 `[100., 110.]`。
- `off` 应约为 `-105.0`（即 `-(100+110)/2`，把区间中点搬到 0），`scl` 应约为 `0.2`（即 `2/10`，把半宽 5 搬到 1）——你可用 4.3 的公式手算验证。
- `xw` 应落在 `[-1, 1]` 内，端点恰为 `-1` 与 `1`。
- `as_series(trim=True)` 会把 `[1e-12, 2, 3, 0, 0]` 的尾部两个精确零去掉，得到 `[1e-12, 2, 3]`；而 `trimcoef(tol=1e-6)` 会**连 `1e-12` 一起砍掉**（因为 `1e-12 ≤ 1e-6`），得到 `[0.]`。**体会两者差别**：`as_series` 只做结构清理，`trimcoef` 做数值清理。

**思考题**（选做）：把上面 `x` 换成复数（如 `x = np.exp(1j*np.linspace(0,np.pi,8))`），重新跑一遍步骤 2-4，观察 `getdomain` 返回的复数 domain 与映射后的 `xw` 是否仍落在该 domain 决定的矩形内。具体结果**待本地验证**。

## 6. 本讲小结

- `polyutils.py` 是整个 `numpy.polynomial` 子包的**地基**：`__all__` 暴露七个公开工具（`as_series / trimseq / trimcoef / getdomain / mapdomain / mapparms / format_float`），外加一批 `_` 开头的共享实现骨架供后续讲义展开。
- **`as_series`** 是输入总闸：转 1-D、校验非空、可选去尾零、用 `np.common_type` 求公共 dtype（失败时退化为 object dtype 以支持 `Decimal`），并返回副本；`[c] = pu.as_series([c])` 是全包通用惯用法。
- **两套去尾零**：`trimseq` 是轻量的结构去零（不复制、保 dtype、保底返回单个零）；`trimcoef` 是数值去零（带 `tol`、严格 `>`、先 `as_series` 再清洗、返回干净副本），并通过 `polytrim = pu.trimcoef` 别名进入各正交族模块。
- **坐标映射三件套**：`mapparms(old,new)` 算仿射参数 \(\text{off}+\text{scl}\cdot x\)；`mapdomain` 把它作用到点集（保留 ndarray 子类型）；`getdomain` 用 `min/max`（复数用包围矩形）猜 domain——三者共同支撑 `_polybase.py` 的 `__call__/roots/fit`。
- **`format_float`** 是打印子系统底层原语：非浮点走 `str`、NaN/Inf 走全局字符串、浮点走 Dragon4 并尊重 `np.get_printoptions()`；`parens` 仅在科学计数法分支加括号。

## 7. 下一步学习建议

本讲打好了地基，接下来 u3-l2「幂级数的创建与算术运算」会用到本讲的 `as_series`、`trimseq` 以及私有的 `_add/_sub/_div/_pow/_fromroots`，建议重点观察 `polyfromroots` 如何用 `_fromroots` 的分治乘法构造首一多项式、`polydiv` 如何用 `_div` 做长除法——你会看到本讲的规整函数在这些算法的**第一步**被反复调用。

进阶路线：

- 想深入「映射为何改善数值稳定性」→ 直接跳读 u5-l1，那里会基于本讲的 `getdomain/mapdomain` 做条件数对比实验。
- 想理解「`format_float` 之上的完整打印管线」→ 阅读 u2-l4（基础）与 u5-l2（symbol 自定义、mapparms 缩放如何体现在自变量符号上）。
- 想看这些工具的多维推广（`_vander_nd`、`_valnd`、`_gridnd`）→ 等 u3-l3、u3-l5 与 u5-l3。

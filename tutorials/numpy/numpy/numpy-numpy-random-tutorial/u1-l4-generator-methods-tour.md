# Generator 常用方法速览

## 1. 本讲目标

学完本讲后，你应当能够：

- 熟练使用 `Generator` 最常用的一组方法：`random`、`integers`、`standard_normal`、`uniform`、`choice`、`shuffle`、`permutation`。
- 理解几乎所有方法共享的「公共参数」：`size`（输出形状）、`dtype`（输出数值类型）、`out`（写入已有数组）、`endpoint`（是否包含上界）、`axis`（沿哪条轴操作）。
- 区分三类容易混淆的操作：抽样（产新值）、洗牌（原地改）、排列（返回副本），并知道何时该用哪一个。
- 用同一个 `Generator` 完成一次小型数据任务：模拟掷骰子并统计频次、原地打乱数组、无放回抽取样本。

本讲是「使用直觉」导向，重在让你把这些方法用顺、看清它们的参数共性；具体的采样算法（Ziggurat 正态、Lemire 区间整数、Fisher-Yates 洗牌）只点到为止，深入实现留到第 4、5 单元。

## 2. 前置知识

本讲默认你已经读过 `u1-l3`，掌握了下面几个结论（这里一句话回顾）：

- **`default_rng()` 是推荐入口**：调用 `np.random.default_rng(seed)` 会返回一个 `Generator` 实例，默认底层用 `PCG64` 作为 BitGenerator，且不维护任何全局状态。
- **`Generator` 是「持有 BitGenerator 的容器」**：它自己不产生随机比特，而是把底层比特流「翻译」成各种分布的样本。
- **可复现性边界**：「相同 seed + 相同调用顺序 + 同一 NumPy 版本 ⇒ 相同输出」。同一个 `Generator` 连续调用两次会得到不同结果，因为内部状态在推进。

你还需要能用 Python 3 运行 `import numpy as np`。本讲所有代码都可以在 REPL、脚本或 Jupyter 里直接试。

> 名词提示：本讲会频繁出现「半开区间 \([a, b)\)」这个说法，它表示「包含 \(a\)、不包含 \(b\)」。例如 \([0, 1)\) 就是「从 0（含）到 1（不含）」。

## 3. 本讲源码地图

本讲只涉及两个关键源码文件：

| 文件 | 作用 |
| --- | --- |
| [_generator.pyx](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx) | Cython 源码，定义 `Generator` 类。本讲涉及的每个方法都写在这里。 |
| [_generator.pyi](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyi) | `Generator` 的类型存根（type stub），用 `@overload` 精确描述每个方法的多种参数形态，是查看「公共参数」最直观的地方。 |

> 旁路文件（本讲会顺带引用一两行，不必通读）：[_common.pyx](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_common.pyx) 提供了 `double_fill`、`cont` 等通用采样模板，`Generator` 的方法最终都委托给它们；这部分的深入讲解留到第 5 单元。

---

## 4. 核心概念与源码讲解

本讲按下面的顺序展开：先用一节建立「公共参数」的全局直觉（4.1），再依次进入三个最小模块——**random 与 integers**（4.2）、**standard_normal 与 uniform**（4.3）、**choice / shuffle / permutation**（4.4）。这样安排是因为：几乎所有方法都共享同一套参数语言，先认清这套语言，后面读任何方法的签名都不会卡壳。

### 4.1 公共参数速览：size / dtype / out / endpoint / axis

#### 4.1.1 概念说明

`Generator` 的方法虽多，但它们的「参数长相」高度一致。掌握下面五个公共参数，就能举一反三地看懂绝大部分方法：

| 参数 | 含义 | 典型默认值 | 出现在哪些方法 |
| --- | --- | --- | --- |
| `size` | 输出形状。`None` 表示返回单个标量；填整数或元组则返回数组 | `None` | 几乎所有抽样方法 |
| `dtype` | 输出的数值类型（如 `np.float64`、`np.int64`、`np.uint8`） | 方法相关 | `random`、`integers`、`standard_normal` 等 |
| `out` | 把结果直接写入你提供的数组，而不是新建一个 | `None` | `random`、`standard_normal`、`permuted` 等 |
| `endpoint` | 区间整数是否包含上界。`False`→\([low, high)\)，`True`→\([low, high]\) | `False` | `integers` |
| `axis` | 沿哪一条轴做洗牌/选择 | `0` | `shuffle`、`permutation`、`permuted`、`choice` |

一个关键直觉：**`size=None` 时返回标量，`size` 给定值时返回数组**。这是「单值 vs 批量」的开关。比如 `rng.random()` 返回一个 `float`，而 `rng.random(5)` 返回长度为 5 的数组。

#### 4.1.2 核心流程

公共参数的处理可以概括为：

1. 解析 `size`：`None` → 标量；整数 → 1 维数组；元组 → 多维数组，元素总数 = 各维乘积。
2. 解析 `dtype`：决定走哪条 C 采样分支（如 `float64` 走 `double_fill`，`float32` 走 `float_fill`）。
3. 若给定 `out`：校验它的形状与 `size` 一致、类型匹配，然后把样本直接写进去（省一次内存分配）。
4. 若涉及 `axis`：先做轴归一化（`normalize_axis_index`），再沿该轴操作。

#### 4.1.3 源码精读

这些公共参数首先体现在类型存根里。以 `random` 为例，`_generator.pyi` 用多个 `@overload` 描述了「`size=None` 返回 `float`」「`size` 给定且 `dtype=f64` 返回 `_ArrayF64`」「给定 `out` 则返回该 `out`」等形态：

[_generator.pyi:64-77] `random` 的重载签名：覆盖了 `size=None`、`dtype=float64/float32`、`out=` 等所有公共参数组合。
[查看 random 重载](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyi#L64-L77)

```python
@overload  # size=None (default);  NOTE: dtype is ignored
def random(self, size: None = None, dtype: _DTypeLikeFloat = ..., out: None = None) -> float: ...
@overload  # size=<given>, dtype=f64 (default)
def random(self, size: _ShapeLike, dtype: _DTypeLikeF64 = ..., out: None = None) -> _ArrayF64: ...
```

注意第一行注释里的 `NOTE: dtype is ignored`：当 `size=None` 时返回的是 Python `float`，此时 `dtype` 没有意义——这是「单值路径」的一个细节。

`integers` 的重载则集中展示了 `endpoint` 参数：

[_generator.pyi:467-470] `integers` 的标量重载：`endpoint: bool = False` 决定区间是 \([low, high)\) 还是 \([low, high]\)。
[查看 integers 重载](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyi#L467-L470)

```python
def integers[AnyIntT: (bool, int)](
    self, low: int, high: int | None = None, size: None = None, *, dtype: type[AnyIntT], endpoint: bool = False
) -> AnyIntT: ...
```

而 `axis` 在洗牌类方法里出现，例如 `shuffle`：

[_generator.pyi:672-675] `shuffle` 的重载：`axis: int = 0`（对 `MutableSequence` 只允许 `axis=0`）。
[查看 shuffle 重载](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyi#L672-L675)

记住这张「公共参数表」，下面三个模块里你会反复看到它们。

---

### 4.2 最小模块一：random 与 integers（均匀浮点与区间整数）

#### 4.2.1 概念说明

这是两类最基础的「均匀」抽样：

- **`random(size, dtype, out)`**：从标准均匀分布 \([0, 1)\) 抽样，返回浮点数。它是所有连续分布的「原料」——很多分布算法在底层都是「先产生 \([0,1)\) 均匀数，再做变换」。
- **`integers(low, high, size, dtype, endpoint)`**：从离散均匀分布抽样，返回整数。可以理解为「等概率地掷一个骰子」。它取代了旧 API 的 `randint`（`endpoint=False`）和 `random_integers`（`endpoint=True`），用同一个方法 + 一个 `endpoint` 开关统一了两者的行为。

两者都支持 `size` 批量生成、`dtype` 选择类型。`integers` 多了一个 `endpoint`：默认 `False`（半开区间），设为 `True` 则包含上界。

#### 4.2.2 核心流程

**`random`**：

1. 把 `dtype` 解析成 `np.dtype`。
2. 若是 `float64` → 调用通用模板 `double_fill`，把 C 函数 `random_standard_uniform_fill`（一次填一批）绑到 `self._bitgen` 上。
3. 若是 `float32` → 走 `float_fill` + `random_standard_uniform_fill_f`。
4. 其他 `dtype` → 抛 `TypeError`。

**`integers`**：

1. 若 `high is None`：把 `high = low; low = 0`（即「只给一个参数」表示 \([0, low)\)）。
2. 按 `dtype` 分派到一组特化函数 `_rand_int8/16/32/64`、`_rand_uint8/16/32/64`、`_rand_bool`。
3. 内部统一使用 **Lemire 无偏区间整数算法**（`_masked=False`，源码注释明确说它比旧的「掩码法」更快）。
4. 特殊情况：`size is None` 且 `dtype` 是 Python `bool`/`int` 时，把结果再包回对应的 Python 标量类型。

#### 4.2.3 源码精读

先看 `random` 的实现，非常短，整段就是「按 dtype 选模板」：

[_generator.pyx:301-361] `random` 方法体：按 `dtype` 分派到 `double_fill`（float64）或 `float_fill`（float32），其余报错。
[查看 random 实现](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L301-L361)

```cython
def random(self, size=None, dtype=np.float64, out=None):
    ...
    _dtype = np.dtype(dtype)
    if _dtype == np.float64:
        return double_fill(&random_standard_uniform_fill, &self._bitgen, size, self.lock, out)
    elif _dtype == np.float32:
        return float_fill(&random_standard_uniform_fill_f, &self._bitgen, size, self.lock, out)
    else:
        raise TypeError('Unsupported dtype %r for random' % _dtype)
```

这里的 `double_fill` 就是 4.1 里说的「通用模板」，定义在 `_common.pyx`：它负责分配输出数组、在持锁状态下调用 C 函数一次性填满 `n` 个值：

[_common.pyx:293-315] `double_fill` 模板：`size=None` 时返回单个标量；否则分配/复用数组，在 `with lock, nogil` 块里调用 C 的 `fill` 函数批量写入。
[查看 double_fill 模板](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_common.pyx#L293-L315)

`nogil` 表示「释放 GIL」，这意味着批量采样可以在多线程里并行——不过你现在只需知道它「一次填一批、很高效」即可，深入留到第 5 单元。

再看 `integers`。「只给一个参数」的便利写法就来自开头这三行：

[_generator.pyx:668-670] `integers` 的参数归一化：`high is None` 时，把 `low` 当作上界、下界置 0。
[查看 integers 参数归一化](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L668-L670)

```cython
if high is None:
    high = low
    low = 0
```

接着是按 `dtype` 的分派表，以及一句重要的实现注释——它解释了为什么新 API 默认不用旧的「掩码法」：

[_generator.pyx:674-696] `integers` 的 dtype 分派：统一令 `_masked = False`（即采用 Lemire 法），再按 int8/16/32/64、uint 系列、bool 分派到对应特化函数。
[查看 integers 分派](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L674-L696)

```cython
# Implementation detail: the old API used a masked method to generate
# bounded uniform integers. Lemire's method is preferable since it is
# faster. randomgen allows a choice, we will always use the faster one.
cdef bint _masked = False

if _dtype == np.int32:
    ret = _rand_int32(low, high, size, _masked, endpoint, &self._bitgen, self.lock)
elif _dtype == np.int64:
    ret = _rand_int64(low, high, size, _masked, endpoint, &self._bitgen, self.lock)
...
elif _dtype == np.bool:
    ret = _rand_bool(low, high, size, _masked, endpoint, &self._bitgen, self.lock)
```

最后是返回标量的特殊处理：当 `size=None` 且用户显式要求 Python `bool`/`int` 类型时，把 NumPy 标量包回 Python 内建类型：

[_generator.pyx:706-709] `integers` 返回值收尾：`size=None` 时若用户要 `bool`/`int`，则转成对应的 Python 标量。
[查看 integers 返回收尾](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L706-L709)

> 名词解释：**Lemire 算法**是 Daniel Lemire 提出的「在区间内快速生成无偏随机整数」的方法，相比朴素的「拒绝采样/掩码法」更快且无偏。它的 C 实现细节留到 `u4-l4` 讲。这里你只需记住结论：`integers` 默认就是用它。

#### 4.2.4 代码实践

**实践目标**：体会 `size`/`dtype`/`endpoint` 三个公共参数，并验证区间整数的边界。

**操作步骤**：

```python
import numpy as np
rng = np.random.default_rng(0)

# 1) random：标量 vs 数组
print("标量:", rng.random())          # 返回单个 float
print("数组:", rng.random(3))         # 返回长度 3 的 float64 数组
print("矩阵:", rng.random((2, 2)))    # 返回 2x2 矩阵
print("float32:", rng.random(3, dtype=np.float32).dtype)  # dtype 控制

# 2) integers：endpoint 与「只给一个参数」的写法
print("半开:", rng.integers(1, 5, size=8))              # 取值范围 {1,2,3,4}
print("含上界:", rng.integers(1, 5, size=8, endpoint=True))  # 取值范围 {1,2,3,4,5}
print("只给上界:", rng.integers(10, size=5))            # 等价于 integers(0, 10, size=5)
print("uint8:", rng.integers(0, 256, size=5, dtype=np.uint8))
```

**需要观察的现象**：

- `rng.random()` 输出落在 \([0, 1)\)，不会等于 1。
- `integers(1, 5, ...)` 的结果最大是 4（半开）；加 `endpoint=True` 后最大可以是 5。
- `integers(10, size=5)` 与 `integers(0, 10, size=5)` 行为一致。
- `dtype=np.uint8` 的结果确实是 `uint8` 类型。

**预期结果**：由于种子固定为 0，输出可复现；具体数值「待本地验证」（不同 NumPy 版本的 `default_rng` 比特流可能不同，但结构与边界一定符合上述描述）。

#### 4.2.5 小练习与答案

**练习 1**：如何用一行代码生成 10 个「掷骰子」结果（面值 1–6）？

> 参考答案：`rng.integers(1, 7, size=10)`（半开 \([1,7)\) 即 \(\{1,2,3,4,5,6\}\)），或 `rng.integers(1, 6, size=10, endpoint=True)`。

**练习 2**：`rng.integers(5, size=4)` 和 `rng.integers(0, 5, size=4)` 有什么关系？

> 参考答案：完全等价。前者触发了 `high is None` 的归一化（源码 L668-670），把 `low=5` 当作上界、下界置 0，因此两者都是从 \([0, 5)\) 抽样。

**练习 3**：为什么 `random` 不支持 `dtype=np.float16`？

> 参考答案：`random` 只特化了 `float64` 与 `float32` 两条分支（源码 L356-361），其他 dtype 直接抛 `TypeError('Unsupported dtype ...')`。这是性能与精度权衡下的设计选择。

---

### 4.3 最小模块二：standard_normal 与 uniform（标准正态与任意区间均匀）

#### 4.3.1 概念说明

- **`standard_normal(size, dtype, out)`**：从「标准正态分布」\(N(0,1)\) 抽样，即均值 0、标准差 1 的高斯分布。它是构造任意正态分布 \(N(\mu,\sigma)\) 的原料：\(\mu + \sigma\cdot Z\)（\(Z\sim N(0,1)\)）。这也是 `u1-l3` 综合实践里用到的那个方法。
- **`uniform(low, high, size)`**：从任意区间 \([low, high)\) 的均匀分布抽样。它和 `random` 的关系是：`random()` ≡ `uniform(0.0, 1.0)`，`uniform` 只是把区间一般化了。

两者都是「连续分布」，所以返回浮点数（且都只支持 `float64`/`float32` 思路）。

#### 4.3.2 核心流程

**`standard_normal`**：与 `random` 几乎同构，只是把 C 函数换成 `random_standard_normal_fill`（内部用 **Ziggurat 算法**把均匀比特流变换成正态样本）。

**`uniform`**：稍微复杂一点，因为它要对 `low`/`high` 做广播：

1. 把 `low`、`high` 转成对齐的 double 数组。
2. 若两者都是标量（0 维）：计算区间宽度 `rng = high - low`，调用通用模板 `cont`，传入 C 函数 `random_uniform` 和两个参数 `(_low, rng)`，并对 `rng` 加上「非负」约束（宽度不能为负）。
3. 否则（数组情况）：先算出 `arange = high - low` 的数组，再走带广播的 `cont`。

数学上，每个样本满足：

\[
x = \text{low} + (\text{high}-\text{low})\cdot U,\qquad U\sim \text{Unif}[0,1)
\]

这正是源码里 `cont(... _low ..., rng ...) ` 所做的：以 `_low` 为偏移、`rng=high-low` 为缩放。

#### 4.3.3 源码精读

`standard_normal` 的实现与 `random` 是「镜像」关系，只差换了一个 C 函数：

[_generator.pyx:1123-1193] `standard_normal` 方法体：float64 走 `double_fill(&random_standard_normal_fill, ...)`，float32 走 `float_fill(&random_standard_normal_fill_f, ...)`。
[查看 standard_normal 实现](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L1123-L1193)

```cython
def standard_normal(self, size=None, dtype=np.float64, out=None):
    ...
    _dtype = np.dtype(dtype)
    if _dtype == np.float64:
        return double_fill(&random_standard_normal_fill, &self._bitgen, size, self.lock, out)
    elif _dtype == np.float32:
        return float_fill(&random_standard_normal_fill_f, &self._bitgen, size, self.lock, out)
    else:
        raise TypeError('Unsupported dtype %r for standard_normal' % _dtype)
```

`uniform` 则展示了「约束」机制。标量路径里，宽度 `rng` 必须非负，否则没有意义：

[_generator.pyx:1095-1106] `uniform` 标量路径：计算 `rng = high - low`，调用 `cont` 模板，其中第二个参数 `rng` 被标记为 `CONS_NON_NEGATIVE`（宽度非负约束）。
[查看 uniform 标量路径](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L1095-L1106)

```cython
if np.PyArray_NDIM(alow) == np.PyArray_NDIM(ahigh) == 0:
    _low = PyFloat_AsDouble(low)
    _high = PyFloat_AsDouble(high)
    rng = _high - _low
    if not np.isfinite(rng):
        raise OverflowError('high - low range exceeds valid bounds')

    return cont(&random_uniform, &self._bitgen, size, self.lock, 2,
                _low, '', CONS_NONE,
                rng, 'high - low', CONS_NON_NEGATIVE,
                0.0, '', CONS_NONE,
                None)
```

`cont` 是 `_common.pyx` 里另一个通用模板（比 `double_fill` 多了「带参数 + 带约束 + 带广播」的能力），它的签名在存根文件里：

[_common.pxd:86-90] `cont` 模板签名：接收一个 C 采样函数、状态、size、锁、参数个数，以及三组「参数值 + 参数名 + 约束类型」。
[查看 cont 签名](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_common.pxd#L86-L90)

约束类型是一个枚举，`CONS_NON_NEGATIVE` 就是其中之一：

[_common.pxd:14-26] `ConstraintType` 枚举：定义了 `CONS_NONE`、`CONS_NON_NEGATIVE`、`CONS_POSITIVE` 等约束，用于在采样前校验分布参数。
[查看约束枚举](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_common.pxd#L14-L26)

> 这里的 `cont` 模板与约束机制是第 5 单元（`u5-l1`）的主题，本讲你只要看出「`uniform` 把 `high-low` 交给 `cont` 并要求它非负」即可。

#### 4.3.4 代码实践

**实践目标**：验证 `standard_normal` 的统计特性（均值≈0、标准差≈1），以及 `uniform` 的区间边界与广播。

**操作步骤**：

```python
import numpy as np
rng = np.random.default_rng(42)

# 1) standard_normal：大样本统计
z = rng.standard_normal(100000)
print("均值(应≈0):", z.mean())
print("标准差(应≈1):", z.std())

# 2) normal = loc + scale * standard_normal 的等价性
a = rng.normal(100, 5, size=100000)
b = 100 + 5 * rng.standard_normal(100000)
print("normal 均值(应≈100):", a.mean())

# 3) uniform：区间与广播
u = rng.uniform(-2, 3, size=100000)
print("uniform 最小(应≥-2):", u.min(), " 最大(应<3):", u.max())

# low/high 都是数组时按元素广播
print("广播:", rng.uniform(low=[0, 10, 100], high=[1, 20, 200]))
```

**需要观察的现象**：

- 10 万个标准正态样本的均值非常接近 0、标准差非常接近 1（大数定律）。
- `uniform(-2, 3, ...)` 的所有值都落在 \([-2, 3)\)，最小值 ≥ -2，最大值严格小于 3（理论上；极少数情况下因浮点舍入可能刚好等于 3，源码文档也提示了这一点）。
- 当 `low`/`high` 是数组时，输出形状由它们广播后的形状决定。

**预期结果**：具体数值「待本地验证」，但统计量与边界一定符合上述规律。

#### 4.3.5 小练习与答案

**练习 1**：`rng.uniform()`（不传参）等价于什么？

> 参考答案：等价于 `rng.random()`。因为 `uniform` 的默认是 `low=0.0, high=1.0`，而 \(0 + (1-0)\cdot U = U\)，正是 `random` 产出的 \([0,1)\) 均匀数。

**练习 2**：要生成均值 50、标准差 3 的正态样本，有哪些写法？

> 参考答案：两种等价写法——`rng.normal(50, 3, size=...)`，或 `50 + 3 * rng.standard_normal(size=...)`（后者正是源码文档 L1160-1161 推荐的写法）。

**练习 3**：`rng.uniform(5, 5, size=3)` 会得到什么？为什么？

> 参考答案：得到 3 个 `5.0`。因为区间宽度 `high - low = 0`，\(x = 5 + 0\cdot U = 5\)。注意源码里 `rng=0` 仍满足 `CONS_NON_NEGATIVE`（0 是非负的），所以不会报错。

---

### 4.4 最小模块三：choice / shuffle / permutation（抽样与洗牌）

#### 4.4.1 概念说明

这一组方法不「凭空造数」，而是对**已有数据**做随机重排或抽取：

- **`choice(a, size, replace, p, axis, shuffle)`**：从一个数组（或整数 `a`，表示 `np.arange(a)`）里随机选元素。`replace=True`（默认）有放回、可重复；`replace=False` 无放回、不重复。`p` 可指定每个元素的概率（默认等概率）。
- **`shuffle(x, axis)`**：**原地**打乱数组（或可变序列），返回 `None`，原对象被修改。
- **`permutation(x, axis)`**：返回一个**打乱后的副本**，不改原数据；若 `x` 是整数，等价于先 `np.arange(x)` 再打乱。
- **`permuted(x, axis, out)`**（补充）：与 `shuffle` 的关键区别是「沿 axis 的每个切片**独立**打乱」且默认返回副本。本讲点到为止。

一句话区分：`shuffle` 改原件、`permutation`/`permuted` 给副本；`choice` 是「按需抽取」（可抽样可子集），后两者是「整体打乱」。

#### 4.4.2 核心流程

**`choice`**（核心是构造一组索引 `idx`，再用它去索引 `a`）：

1. 把 `a` 转成数组；若是 0 维（即整数），用 `operator.index` 取出 `pop_size`，否则 `pop_size = a.shape[axis]`。
2. 若给了 `p`：校验它是一维、长度匹配、非负、和为 1（容差内）。
3. 解析 `size`：`None`→标量；否则 `size = np.prod(shape)`。
4. **有放回**：有 `p` 时用「CDF + searchsorted」反演；无 `p` 时直接 `self.integers(0, pop_size, ...)`。
5. **无放回**：若 `size > pop_size` 直接报错。无 `p` 时用 **Floyd 算法**（小样本）或「尾部洗牌」（大样本）生成 `size` 个不重复索引。
6. 用 `idx` 去索引 `a`，返回抽样结果。

**`shuffle`**（经典 **Fisher-Yates** 算法）：

1. 对 1 维 `ndarray`：走快速路径，直接在底层字节缓冲上做两两交换（`_shuffle_raw`）。
2. 对多维 `ndarray`：沿 `axis` 交换整条切片。
3. 对普通可变序列（如 `list`）：逐对交换 `x[i], x[j]`。
4. 每次交换的位置 `j` 由 `random_interval(bitgen, i)` 在 \([0, i]\) 内产生。

**`permutation`**：

1. 若 `x` 是整数：`arr = np.arange(x)`，对其 `shuffle` 后返回。
2. 若 `x` 是数组：复制一份再 `shuffle`，返回副本（不改原件）。

#### 4.4.3 源码精读

先看 `choice` 无放回、等概率这条最常用的路径。它先做容量校验，再在「大样本尾部洗牌」和「Floyd 算法」之间二选一：

[_generator.pyx:923-993] `choice` 的无放回分支：先校验 `size <= pop_size`，再按样本量选择「尾部洗牌」（大样本）或「Floyd 算法」（小样本）生成不重复索引。
[查看 choice 无放回分支](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L923-L993)

其中 Floyd 算法那段（小样本、无 `p`）很巧妙——它用一个哈希集合去重，循环地从 \([0, j]\) 抽 `val`：

[_generator.pyx:967-993] `choice` 的 Floyd 算法实现：从 `pop_size - size` 到 `pop_size` 逐步推进，用 `random_bounded_uint64` 在 \([0, j]\) 抽样并借哈希集合去重，最终得到 `size` 个不重复索引。
[查看 Floyd 算法](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L967-L993)

```cython
# Floyd's algorithm
idx = np.empty(size, dtype=np.int64)
...
with self.lock, cython.wraparound(False), nogil:
    for j in range(pop_size_i - size_i, pop_size_i):
        val = random_bounded_uint64(&self._bitgen, 0, j, 0, 0)
        ...
```

有放回、等概率的路径则极其简洁——直接复用我们 4.2 学过的 `integers`：

[_generator.pyx:921-922] `choice` 有放回、等概率分支：直接调用 `self.integers(0, pop_size, ...)` 生成索引。
[查看 choice 有放回等概率分支](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L921-L922)

```cython
else:
    idx = self.integers(0, pop_size, size=shape, dtype=np.int64)
```

这正是为什么文档说「`rng.choice(n, k)` 等价于 `rng.integers(0, n, k)`」——底层就是同一段代码。

再看 `shuffle`。对 1 维 `ndarray` 它有一条「在字节缓冲上直接交换」的快速路径：

[_generator.pyx:4860-4881] `shuffle` 的 1 维 ndarray 快速路径：取底层数据指针、步长、元素大小，调用 `_shuffle_raw_wrap` 在 `nogil` 下做字节级两两交换。
[查看 shuffle 1 维快速路径](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L4860-L4881)

```cython
if type(x) is np.ndarray and x.ndim == 1 and x.size:
    ...
    with self.lock, nogil:
        _shuffle_raw_wrap(&self._bitgen, n, 1, itemsize, stride, x_ptr, buf_ptr)
```

`_shuffle_raw_wrap` 内部最终走到 `_shuffle_raw`，它就是教科书式的 Fisher-Yates——从后往前，每个位置 `i` 与一个随机位置 `j ∈ [0, i]` 交换：

[_generator.pyx:101-105] `_shuffle_raw` 的核心循环：`for i in reversed(range(first, n))`，用 `random_interval(bitgen, i)` 选 `j`，再做三次 `memcpy` 完成两元素交换。
[查看 Fisher-Yates 核心](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L101-L105)

```cython
for i in reversed(range(first, n)):
    j = random_interval(bitgen, i)
    string.memcpy(buf, data + j * stride, itemsize)
    string.memcpy(data + j * stride, data + i * stride, itemsize)
    string.memcpy(data + i * stride, buf, itemsize)
```

`random_interval(bitgen, i)` 的作用是「在 \([0, i]\) 内产生一个无偏随机整数」，它声明在 C 桥接文件里：

[c_distributions.pxd:99] `random_interval` 声明：在 \([0, max]\) 内生成无偏随机 uint64。
[查看 random_interval 声明](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/c_distributions.pxd#L99)

多维 `ndarray` 的 `shuffle` 则把目标轴交换到第 0 轴，再逐条切片交换：

[_generator.pyx:4882-4897] `shuffle` 的多维 ndarray 路径：`swapaxes(x, 0, axis)` 后，逆序遍历，用 `random_interval` 选 `j`，交换 `x[j, ...]` 与 `x[i, ...]`。
[查看 shuffle 多维路径](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L4882-L4897)

最后是 `permutation`，它的整数分支恰好印证了「整数 `x` 等价于 `np.arange(x)` 再洗牌」：

[_generator.pyx:4965-4968] `permutation` 的整数分支：`arr = np.arange(x)`，调用 `self.shuffle(arr)` 后返回。
[查看 permutation 整数分支](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L4965-L4968)

```cython
if isinstance(x, (int, np.integer)):
    arr = np.arange(x)
    self.shuffle(arr)
    return arr
```

注意它返回的是 `arr` 本身（已被原地打乱），而数组分支会复制一份，所以 `permutation` 不影响你传入的原始数组（整数情况除外，因为 `np.arange` 本就是新建的）。

#### 4.4.4 代码实践

**实践目标**：对比 `shuffle`（原地）与 `permutation`（副本），并体会 `choice` 的有放回/无放回差异。

**操作步骤**：

```python
import numpy as np
rng = np.random.default_rng(7)

# 1) shuffle 是原地操作：返回 None，原数组被改
a = np.arange(10)
ret = rng.shuffle(a)
print("shuffle 返回值:", ret)   # None
print("原数组已被打乱:", a)

# 2) permutation 返回副本，原数组不变
b = np.arange(10)
c = rng.permutation(b)
print("原数组不变:", b)
print("得到打乱副本:", c)

# permutation 传整数 = np.arange(n) 再打乱
print("整数版:", rng.permutation(6))

# 3) choice：有放回 vs 无放回
pool = np.array(['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'])
print("有放回(可能重复):", rng.choice(pool, size=5, replace=True))
print("无放回(必不重复):", rng.choice(pool, size=5, replace=False))

# 4) choice 带概率
print("加权抽样:", rng.choice(5, size=6, p=[0.5, 0.2, 0.1, 0.1, 0.1]))
```

**需要观察的现象**：

- `shuffle` 后 `ret is None`，但 `a` 的顺序变了。
- `permutation` 不改 `b`，`c` 是新的打乱数组。
- 有放回抽样里同一个元素可能出现多次；无放回抽样里 5 个结果两两不同。
- 加权抽样里，概率高的下标（这里是 0）出现得更频繁。

**预期结果**：具体输出「待本地验证」（取决于种子与版本），但上述「是否修改原件」「是否允许重复」的行为是确定的。

#### 4.4.5 小练习与答案

**练习 1**：你想「随机抽取训练集/测试集的下标」，要求同一份样本里不重复，该用 `choice(..., replace=?)` 还是 `shuffle`？

> 参考答案：用 `choice(idx_array, size=k, replace=False)`，或 `permutation(idx_array)[:k]`。两者都能得到 `k` 个不重复下标；前者语义更直接。务必 `replace=False`，否则会重复。

**练习 2**：`rng.shuffle(arr)` 之后 `arr` 变了，但 `print(rng.permutation(arr))` 之后 `arr` 没变，为什么？

> 参考答案：`shuffle` 是**原地**操作（源码 L4789 文档明确「Modify an array ... in-place」，返回 `None`）；`permutation` 的数组分支会**复制**一份再洗牌（源码 L4970 起的 `np.asarray(x)` + 后续复制逻辑），所以原件不动。

**练习 3**：`rng.choice(10, 5)` 和 `rng.permutation(10)[:5]` 产生的结果有什么相同与不同？

> 参考答案：两者都得到 5 个来自 \(\{0..9\}\) 的不重复整数。不同点：`choice(10, 5)` 默认 `replace=False` 走 Floyd/尾部洗牌，结果是「无序」的抽样；`permutation(10)[:5]` 是「完整洗牌后取前 5」，结果也是无序的。两者的随机序列一般不同（消耗底层比特的方式不同），但统计上都满足「5 个不重复值」。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「迷你数据分析」任务。用**同一个** `Generator`（保证整段流程的状态是连续推进的）：

```python
import numpy as np
rng = np.random.default_rng(2024)

# 任务 1：模拟掷 1000 次骰子（面值 1-6）并统计每个面出现的频次
rolls = rng.integers(1, 7, size=1000)            # [1,7) => {1,2,3,4,5,6}
# 用 bincount 统计；minlength=7 让下标 0..6 都有位置，再丢掉下标 0
face_counts = np.bincount(rolls, minlength=7)[1:]
print("各面频次(1..6):", face_counts)
print("频次总和(应=1000):", face_counts.sum())
print("理论期望(应≈166):", 1000 / 6)

# 任务 2：原地打乱一个数组
data = np.arange(20)
rng.shuffle(data)                                # 原地，返回 None
print("打乱后的数组:", data)

# 任务 3：无放回抽取 5 个样本
sampled = rng.choice(data, size=5, replace=False)  # 5 个互不相同的元素
print("无放回抽取 5 个:", sampled)
print("是否两两不同:", len(set(sampled.tolist())) == 5)  # 应为 True
```

**逐步解读**：

1. **掷骰子**：`integers(1, 7, size=1000)` 生成 1000 个 \([1,7)\) 的整数，即面值 1–6。`np.bincount` 按值统计频次；由于面是等概率的，每个面期望约 \(1000/6 \approx 166.7\) 次，实际频次会在其附近波动（大数定律）。
2. **原地打乱**：`shuffle(data)` 直接修改 `data`，不返回新数组。这正是 4.4 学到的「`shuffle` 改原件」。
3. **无放回抽取**：`choice(data, size=5, replace=False)` 从（已被打乱的）`data` 里取 5 个互不相同元素。注意此时 `rng` 的内部状态已经被前两步推进过，所以这次抽样接续在前面的随机流之后。

**需要观察的现象**：

- 频次总和恰为 1000，每个面频次在 166 上下波动。
- `data` 被打乱（不再是 `0,1,2,...,19`）。
- 抽取出的 5 个元素两两不同（`replace=False` 的保证）。

**预期结果**：具体数值「待本地验证」。如果你把 `default_rng(2024)` 的种子固定下来，整段输出在同一 NumPy 版本下完全可复现——这正是 `u1-l3` 强调的「可复现性边界」。

**进阶变式**（可选）：

- 把 `integers(1, 7, ...)` 换成 `integers(1, 6, size=1000, endpoint=True)`，验证结果分布不变（两种写法等价）。
- 把任务 3 的 `replace=False` 改成 `replace=True`，观察 `set` 长度可能小于 5（出现重复）。
- 用 `rng.permutation(data)[:5]` 替代任务 3 的 `choice`，对比两种「取 5 个不重复元素」写法。

## 6. 本讲小结

- `Generator` 的常用方法共享一套**公共参数语言**：`size`（形状）、`dtype`（类型）、`out`（写已有数组）、`endpoint`（含上界）、`axis`（轴向）。认清这套语言，就能举一反三。
- `random` 产 \([0,1)\) 浮点、`integers` 产区间整数；两者都按 `dtype` 分派，`integers` 默认用更快的 **Lemire** 算法（`_masked=False`）。
- `standard_normal` 与 `random` 是「镜像」结构（只换 C 函数）；`uniform(low, high)` 把 \([0,1)\) 均匀数线性变换到 \([low,high)\)，公式 \(x = \text{low}+(\text{high}-\text{low})U\)，并对区间宽度施加「非负」约束。
- `choice` 的核心是「先造索引再索引数组」：有放回+等概率直接复用 `integers`，无放回+等概率用 Floyd 算法或尾部洗牌。
- `shuffle` 是**原地** Fisher-Yates（返回 `None`），`permutation` 返回**副本**（整数入参等价 `np.arange` 再洗牌）；`permuted` 则沿 axis 独立打乱。
- 所有方法最终都把工作委托给 `_common.pyx` 的通用模板（`double_fill`/`cont` 等）和 C 层采样函数——这套「Python 方法 → 通用模板 → C 函数」的三层结构是第 5 单元的主线。

## 7. 下一步学习建议

- **横向扩展**：在本讲 7 个方法之外，挑两三个「带参数」的分布方法上手试，例如 `normal(loc, scale)`、`binomial(n, p)`、`poisson(lam)`、`exponential(scale)`，巩固「参数 + size」的用法。它们都定义在同一个 [_generator.pyx](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx) 里。
- **进入第 2 单元**：本讲多次出现 `self._bitgen`、`self.lock`、`double_fill`、`cont` 这些「桥」。下一讲 `u2-l1` 会拆开 `bitgen_t` 这个 C 结构，解释「函数指针表」如何让分布层与具体生成器解耦。
- **深入采样算法**：如果你想搞懂 `standard_normal` 背后的 Ziggurat、`integers` 背后的 Lemire、`shuffle` 背后的 Fisher-Yates，可以直接跳到第 4 单元（`u4-l2` 均匀、`u4-l3` 正态/指数、`u4-l4` 区间整数）。
- **推荐先读源码**：把 [_generator.pyx 的 `random`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L301-L361) 和 [`standard_normal`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/random/_generator.pyx#L1123-L1193) 并排读一遍，你会立刻看出它们的「同构」关系——这是理解整个 `Generator` 类的最佳起点。

# NaN 均值、方差与分位

## 1. 本讲目标

本讲承接 [u9-l1](u9-l1-nan-infra-aggregation.md)（NaN 基础设施与聚合）与 [u7-l2](u7-l2-percentile-interpolation.md)（百分位与分位数插值算法），讲解 `_nanfunctions_impl.py` 中**「需要做除法或排序」的一类 NaN 感知函数**：均值、方差、标准差、中位数、百分位、分位数。

学完本讲，你应当能够：

- 说清 `nanmean` 为何对整数输入要提升为 `float64`，以及它如何用 `_divide_by_count` 安全地做「求和 ÷ 非零个数」。
- 理解 `nanvar`/`nanstd` 的「先算均值、再算平方偏差、最后除以自由度」三步就地计算，以及 `dof <= 0` 时如何回退为 NaN。
- 看懂 `_nanmedian` 对「1D / 小数组 / 大数组」三条路径的分发，以及 `_nanmedian_small` 为何用 `np.ma`（掩码数组）偷懒。
- 解释 `nanpercentile`/`nanquantile` 几乎不做任何「分位计算」，而是把活儿全部委托给 u7-l2 的 `_quantile_unchecked`，本讲只负责「剔除 NaN」这一步。

## 2. 前置知识

本讲默认你已经掌握以下概念（在前置讲义中已建立）：

- **「替换-聚合-还原」三段式范式**（u9-l1）：用 `_replace_nan` 把 NaN 换成单位元（sum 的 0、prod 的 1），跑普通聚合，再用掩码把「全 NaN 切片」的结果还原回 NaN。
- **五个 NaN 基础设施**（u9-l1）：`_nan_mask`、`_replace_nan`、`_copyto`、`_remove_nan_1d`、`_divide_by_count`。本讲会**直接复用**它们，不再重复实现细节。
- **`_ureduce` 通用归约框架**（u7-l1）：统一处理 `axis`/`keepdims`/`out`，把多轴归约翻译成单轴归约，使底层内核只需懂「沿一个轴」。
- **分位数插值算法**（u7-l2）：虚索引、alpha-beta 公式、`_lerp` 线性插值、13 种 `method`。本讲的 `nanpercentile`/`nanquantile` **完全不重写**这套算法，只做一层「先剔除 NaN」的包装。

一个关键直觉：本讲的六个函数（`nanmean`/`nanvar`/`nanstd`/`nanmedian`/`nanpercentile`/`nanquantile`）与 u9-l1 的 `nansum`/`nanprod` 有本质区别——后者只需把 NaN 换成单位元即可（聚合后单位元天然被吸收），而前者要做**除法**或**排序**，必须**先数清楚有几个有效元素**（`cnt`）或**先把 NaN 物理删除**，否则分母或排序结果都会被污染。这个差异是本讲全部设计的出发点。

另需补充两个统计学小概念：

- **自由度（degrees of freedom, dof）**：样本方差的无偏估计除以 `N - ddof` 而非 `N`，其中 `ddof`（Delta Degrees of Freedom）默认为 0（最大似然估计），常取 1（无偏估计）。
- **百分位 vs 分位**：`nanpercentile` 的 `q` 取值范围是 `[0, 100]`，`nanquantile` 的 `q` 是 `[0, 1]`，两者只差一个 `÷100`。

## 3. 本讲源码地图

本讲几乎全部源码集中在一个文件：

| 文件 | 作用 |
|------|------|
| [numpy/lib/_nanfunctions_impl.py](_nanfunctions_impl.py) | 全部六个公开函数与 `_nanmedian`/`_nanmedian_small`/`_nanquantile_unchecked` 等私有内核的实现 |

此外会引用 u7-l1/u7-l2 所在文件中**被复用**的函数作为对照（不重复讲解其内部）：

| 文件 | 被复用的函数 |
|------|--------------|
| [numpy/lib/_function_base_impl.py](_function_base_impl.py) | `_ureduce`、`_quantile_unchecked`、`_quantile_is_valid`、`_weights_are_valid` |

测试依据来自：

| 文件 | 作用 |
|------|------|
| `numpy/lib/tests/test_nanfunctions.py` | `TestNanFunctions_MeanVarStd`、`TestNanFunctions_Median`、`TestNanFunctions_Percentile`、`TestNanFunctions_Quantile` 四个测试类，分别覆盖本讲的四组函数 |

所有公开函数都遵循前几讲反复出现的 **dispatcher + impl 双函数写法**（NEP-18 `__array_function__` 派发）：公开函数以 `@array_function_dispatch(_xxx_dispatcher)` 装饰，dispatcher 只返回参与运算的数组参数（如 `(a, out)`），本讲不再逐个赘述装饰器机制，重点放在实现逻辑。

## 4. 核心概念与源码讲解

### 4.1 nanmean：求和 ÷ 有效个数，与 dtype 提升

#### 4.1.1 概念说明

`nanmean` 计算「忽略 NaN 的算术平均」。直觉上很简单：

\[
\text{nanmean}(x) = \frac{\sum_{x_i \neq \text{NaN}} x_i}{\text{cnt}}, \quad \text{cnt} = \#\{x_i \neq \text{NaN}\}
\]

但有两个细节决定了它的实现不能简单写成 `nansum(a) / (... 非NaN个数)`：

1. **分母必须自己数**。直接用 `a.shape[axis]` 当分母是错的，因为其中混着 NaN（已在 `_replace_nan` 步被换成 0，会让分子偏小）。所以必须单独统计非 NaN 的个数 `cnt`。
2. **整数要提升为 `float64`**。整数数组虽然不可能含 NaN（`_replace_nan` 会因 `dtype` 非浮点而 `mask=None` 直接走 `np.mean` 分支），但 NumPy 规定整数求平均必须返回浮点，否则 `1/2` 这类结果会被截断。

#### 4.1.2 核心流程

`nanmean` 的实现分两条路径：

```
_replace_nan(a, 0)  →  arr(NaN→0), mask(非None表示是浮点输入)
│
├─ mask is None（整数/非浮点）→ 直接 np.mean(arr, ...)，整数自动升 float64
│
└─ mask 非 None（浮点输入）→
      ① 校验 dtype/out 必须是 inexact（浮点）
      ② cnt = sum(~mask)         # 非 NaN 的个数
      ③ tot = sum(arr)           # NaN 已被替换为 0，不影响求和
      ④ avg = _divide_by_count(tot, cnt)   # 安全除法
      ⑤ 若 cnt==0（全 NaN 切片）→ 报 "Mean of empty slice" 警告，结果自动是 NaN
```

关键点：步骤 ③ 把 NaN 替换成 0 后求和，等价于「只对非 NaN 元素求和」——因为 0 加进去不改变和。这就是「替换-求和」复用 `np.sum` 的妙处。但分母 `cnt` 必须另算，步骤 ② 用 `~mask`（mask 中 `True` 表示「是 NaN」）取反后求和得到非 NaN 个数。

#### 4.1.3 源码精读

公开函数签名与实现主体：

[numpy/lib/_nanfunctions_impl.py:952-1054](_nanfunctions_impl.py#L952-L1054) —— `nanmean` 公开函数。开头先做替换：

```python
arr, mask = _replace_nan(a, 0)
if mask is None:
    return np.mean(arr, axis=axis, dtype=dtype, out=out, keepdims=keepdims,
                   where=where)
```

这里 `_replace_nan(a, 0)` 把 NaN 换成 0；若 `mask is None`，说明输入是整数/非浮点类型（不可能含 NaN），直接交给 `np.mean`，整数输入会被 `np.mean` 自动提升为 `float64`，无需特殊处理。

随后是浮点输入的专属逻辑，先做类型校验：

```python
if dtype is not None:
    dtype = np.dtype(dtype)
if dtype is not None and not issubclass(dtype.type, np.inexact):
    raise TypeError("If a is inexact, then dtype must be inexact")
if out is not None and not issubclass(out.dtype.type, np.inexact):
    raise TypeError("If a is inexact, then out must be inexact")
```

这两条校验是「NaN 感知函数」的共性约定：浮点输入必须配浮点的 `dtype`/`out`，否则 NaN 无法表示。**`nanmean`/`nanvar` 都有这段几乎一字不差的校验**。

然后是「求和 ÷ 个数」的核心三行：

```python
cnt = np.sum(~mask, axis=axis, dtype=np.intp, keepdims=keepdims, where=where)
tot = np.sum(arr, axis=axis, dtype=dtype, out=out, keepdims=keepdims, where=where)
avg = _divide_by_count(tot, cnt, out=out)
```

- `~mask` 把「是 NaN」翻转为「不是 NaN」，求和得有效个数 `cnt`，用 `np.intp` 节省内存。
- `tot` 是替换后的求和（NaN 已是 0）。
- `_divide_by_count`（[u9-l1 讲过](_nanfunctions_impl.py#L204-L244)）在 `errstate(invalid='ignore', divide='ignore')` 下做除法，抑制「0/0」警告，并允许就地写回 `out`。

最后处理全 NaN 切片：

```python
isbad = (cnt == 0)
if isbad.any():
    warnings.warn("Mean of empty slice", RuntimeWarning, stacklevel=2)
return avg
```

注意这里**没有显式把结果改成 NaN**——注释 `# NaN is the only possible bad value` 点明：当 `cnt==0` 时，`tot` 也是 0（全替换成 0 的求和），`0/0` 在 IEEE 754 下自然就是 NaN，所以只报警告即可。这是对浮点语义的精妙利用。

#### 4.1.4 代码实践

1. **实践目标**：验证 `nanmean` 的「求和 ÷ 有效个数」语义，并观察全 NaN 切片的警告。
2. **操作步骤**：

```python
import numpy as np
import warnings

a = np.array([[1.0, np.nan], [3.0, 4.0]])
print("整体:", np.nanmean(a))          # (1+3+4)/3 ≈ 2.6667
print("axis=0:", np.nanmean(a, axis=0))  # [2.0, 4.0]
print("axis=1:", np.nanmean(a, axis=1))  # [1.0, 3.5]

# 整数输入：观察返回类型
ai = np.array([[1, 2], [3, 4]])
print("整数 dtype:", np.nanmean(ai).dtype)  # float64

# 全 NaN 切片
b = np.array([[np.nan, np.nan], [1.0, 2.0]])
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    r = np.nanmean(b, axis=0)
    print("结果:", r)                    # [nan, 2.0]
    print("警告类别:", w[0].category.__name__)  # RuntimeWarning
```

3. **需要观察的现象**：整数输入返回 `float64`；全 NaN 的那一列结果为 `nan` 并触发 `RuntimeWarning`。
4. **预期结果**：与注释一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把 `cnt` 直接取 `a.shape[axis]`，而要费力 `sum(~mask)`？

**答案**：因为 NaN 被替换成 0 后仍占据数组位置，`a.shape[axis]` 包含了这些 NaN 位置，会让分母偏大、结果偏小。必须只数真正非 NaN 的元素。

**练习 2**：`nanmean(np.array([np.nan, np.nan]))` 返回什么？为何不用显式赋值 NaN？

**答案**：返回 `nan` 并报警告。因为 `cnt=0`、`tot=0`，`0/0` 在 IEEE 754 下天然是 NaN，故无需显式写回。

---

### 4.2 nanvar / nanstd：先算均值，再算平方偏差，最后除以自由度

#### 4.2.1 概念说明

方差衡量数据围绕均值的离散程度：

\[
\text{var}(x) = \frac{1}{N - \text{ddof}} \sum_{i} (x_i - \bar{x})^2, \quad \bar{x} = \text{nanmean}(x)
\]

标准差就是方差的平方根：`std = sqrt(var)`。所以 `nanstd` 只是 `nanvar` 的一层薄包装——这是 NumPy 的明确设计：只实现一遍方差逻辑，开方即可。

NaN 感知版本的难点在于：分子和分母都要用「非 NaN 元素」来算，并且要**就地（in-place）**操作以省内存。这导致 `nanvar` 的代码比 `nanmean` 长得多。

#### 4.2.2 核心流程

```
_replace_nan(a, 0)  →  arr, mask
│
├─ mask is None（整数）→ 直接 np.var(...)，整数自动升 float64
│
└─ mask 非 None（浮点）→
      ① cnt = sum(~mask)                    # 有效个数
      ② avg = sum(arr) / cnt                # 复用 nanmean 思路算均值
      ③ arr -= avg   (就地，where=where)    # 偏差
      ④ arr[mask] = 0                       # NaN 位置的偏差强制清零（关键！）
      ⑤ sqr = arr²  (复数用 arr*conj(arr))  # 平方偏差，就地
      ⑥ var = sum(sqr)                       # 平方偏差之和
      ⑦ dof = cnt - ddof
      ⑧ var = var / dof                      # 除以自由度
      ⑨ 若 dof <= 0 → 报警告，显式写回 NaN
```

最关键也最易错的是**步骤 ④**：步骤 ③ 做了 `arr - avg` 后，原本是 NaN（已被替换成 0）的位置变成了 `0 - avg = -avg`，这是一个**非零的虚假偏差**！如果不把它清零，方差会被严重高估。所以必须用 `_copyto(arr, 0, mask)` 把这些位置重新置 0。

#### 4.2.3 源码精读

`nanvar` 的就地计算主体（精简版）：

[numpy/lib/_nanfunctions_impl.py:1799-1871](_nanfunctions_impl.py#L1799-L1871) —— `nanvar` 浮点分支核心。

先算均值（与 `nanmean` 同构，但注意它内部用 `_keepdims=True` 以便广播）：

```python
cnt = np.sum(~mask, axis=axis, dtype=np.intp, keepdims=_keepdims, where=where)
...
avg = np.sum(arr, axis=axis, dtype=dtype, keepdims=_keepdims, where=where)
avg = _divide_by_count(avg, cnt)
```

这里的 `_keepdims` 是为了兼容 `np.matrix` 子类的一个特判（[numpy/lib/_nanfunctions_impl.py:1821-1824](_nanfunctions_impl.py#L1821-L1824)）：`matrix` 不接受 `keepdims=True`，但它的语义本身就等价于 `keepdims=True`。

接着是「偏差-清零-平方」三步，全部**就地**写回 `arr`：

```python
np.subtract(arr, avg, out=arr, casting='unsafe', where=where)
arr = _copyto(arr, 0, mask)
if issubclass(arr.dtype.type, np.complexfloating):
    sqr = np.multiply(arr, arr.conj(), out=arr, where=where).real
else:
    sqr = np.multiply(arr, arr, out=arr, where=where)
```

复数方差取模的平方 `arr * conj(arr)` 再取实部，保证结果非负实数。

最后算方差并处理自由度：

```python
var = np.sum(sqr, axis=axis, dtype=dtype, out=out, keepdims=keepdims, where=where)
...
dof = cnt - ddof
var = _divide_by_count(var, dof)

isbad = (dof <= 0)
if np.any(isbad):
    warnings.warn("Degrees of freedom <= 0 for slice.", RuntimeWarning, stacklevel=2)
    var = _copyto(var, np.nan, isbad)
return var
```

注意与 `nanmean` 的区别：`dof <= 0` 时 `var` 可能是 `inf`、负数或 NaN（注释 `# NaN, inf, or negative numbers are all possible bad values`），所以这里**必须显式** `_copyto(var, np.nan, isbad)`，不能像 `nanmean` 那样依赖 `0/0` 自然产生 NaN。

`correction` 参数是 NumPy 2.0 引入的 Array API 兼容名，与 `ddof` 二选一（[numpy/lib/_nanfunctions_impl.py:1812-1818](_nanfunctions_impl.py#L1812-L1818)）：同时传会报错，只传 `correction` 则赋值给 `ddof`。

`nanstd` 极简，只是开方：

[numpy/lib/_nanfunctions_impl.py:1994-2003](_nanfunctions_impl.py#L1994-L2003) —— `nanstd` 把活儿全交给 `nanvar`：

```python
var = nanvar(a, axis=axis, dtype=dtype, out=out, ddof=ddof,
             keepdims=keepdims, where=where, mean=mean, correction=correction)
if isinstance(var, np.ndarray):
    std = np.sqrt(var, out=var)          # 就地开方
elif hasattr(var, 'dtype'):
    std = var.dtype.type(np.sqrt(var))   # 保持 numpy 标量类型
else:
    std = np.sqrt(var)
return std
```

`out=var` 让开方就地完成，再次体现「省内存」的设计取向。

#### 4.2.4 代码实践

1. **实践目标**：验证 `nanvar` 的自由度语义，并体会 `ddof` 对结果的影响。
2. **操作步骤**：

```python
import numpy as np

a = np.array([[1.0, np.nan], [3.0, 4.0]])
# axis=1: 第一行有效 [1]，dof=1-0=1 → var=0；第二行有效 [3,4]，var=0.25
print("axis=1, ddof=0:", np.nanvar(a, axis=1))   # [0.   0.25]
print("axis=1, ddof=1:", np.nanvar(a, axis=1, ddof=1))  # [nan 0.5]
print("std:", np.nanstd(a, axis=1))               # [0.   0.5]
print("验证:", np.sqrt(np.nanvar(a, axis=1)))     # 与 nanstd 一致

# correction 与 ddof 等价
print("correction:", np.nanvar(a, axis=1, correction=1))  # 同 ddof=1
```

3. **需要观察的现象**：第一行只有 1 个有效元素，`ddof=1` 时 `dof=0`，结果为 `nan` 并触发 `RuntimeWarning`；`correction` 与 `ddof` 结果一致。
4. **预期结果**：`ddof=1` 的第一行为 `nan`，第二行为 `0.5`；`nanstd` 是 `nanvar` 的开方。

#### 4.2.5 小练习与答案

**练习 1**：步骤 ④ `_copyto(arr, 0, mask)` 若删除，方差会怎样？

**答案**：NaN 位置替换成 0 后，减去均值得到 `-avg`，平方后变成 `avg²`，这个虚假偏差会被计入平方和，使方差被严重高估。所以清零是必须的。

**练习 2**：为何 `nanvar` 在 `dof<=0` 时必须显式写回 NaN，而 `nanmean` 在 `cnt==0` 时不用？

**答案**：`nanmean` 的 `0/0` 天然是 NaN；但 `nanvar` 的分母是 `dof=cnt-ddof`，当 `dof<=0` 时分子可能是有限值或 `inf`，`有限/0` 得 `inf`、`0/负数` 得 `0` 或负数，不一定是 NaN，所以必须显式覆盖。

---

### 4.3 nanmedian / _nanmedian / _nanmedian_small：先剔除 NaN，再取中位数

#### 4.3.1 概念说明

中位数需要**排序**，所以「替换成单位元」的招数彻底失效——你不能把 NaN 替换成 0 再排序（0 会跑到前面污染中位数），只能**把 NaN 物理删除**再对剩余元素取中位数。

NumPy 的策略是把问题化简为**一维问题**：先剔除 NaN 得到一维「干净」数组，再调用普通的 `np.median`。难点在于多维沿轴时如何高效处理。`_nanmedian` 用三条路径权衡了「简单」「快速」「通用」：

- **路径 A（最简单）**：`axis is None` 或数组本身就是 1D → `ravel()` 成一维，直接调 `_nanmedian1d`。
- **路径 B（小数组快路）**：沿轴长度 `< 600` → 用 `_nanmedian_small`（基于 `np.ma` 掩码数组）。
- **路径 C（大数组通用）**：否则用 `apply_along_axis(_nanmedian1d, ...)` 逐切片处理。

#### 4.3.2 核心流程

公开 `nanmedian` 只是个壳：

```
nanmedian(a, ...)
  ├─ a.size == 0 → 退回 nanmean 处理空数组
  └─ fnb._ureduce(a, func=_nanmedian, ...)   # 用 _ureduce 统一 axis/keepdims/out
```

真正逻辑在私有的 `_nanmedian`（注意它「不支持扩展 axis 与 keepdims」，这些由 `_ureduce` 在外层补齐——这正是 u7-l1 讲过的 `_ureduce` 套路）：

```
_nanmedian(a, axis, ...)   # 这里的 axis 已是规整后的单轴
  ├─ axis is None 或 a.ndim==1 → ravel 后 _nanmedian1d
  ├─ a.shape[axis] < 600 → _nanmedian_small（np.ma 快路）
  └─ 否则 → apply_along_axis(_nanmedian1d, axis, a)
```

而 `_nanmedian1d` 是所有路径最终汇聚的「一维带 NaN 中位数」内核：

```
_nanmedian1d(arr1d):
  ① arr1d_parsed = _remove_nan_1d(arr1d)   # 物理剔除 NaN（u9-l1 基础设施）
  ② 若剔光（size==0）→ 返回原数组最后一个元素（保留 timedelta64/complex 的 NaN 类型）
  ③ 否则 → np.median(arr1d_parsed)
```

#### 4.3.3 源码精读

公开壳函数：

[numpy/lib/_nanfunctions_impl.py:1208-1216](_nanfunctions_impl.py#L1208-L1216) —— `nanmedian` 主体：

```python
a = np.asanyarray(a)
# apply_along_axis in _nanmedian doesn't handle empty arrays well,
# so deal them upfront
if a.size == 0:
    return np.nanmean(a, axis, out=out, keepdims=keepdims)

return fnb._ureduce(a, func=_nanmedian, keepdims=keepdims,
                    axis=axis, out=out,
                    overwrite_input=overwrite_input)
```

注意空数组的兜底：`apply_along_axis` 处理空数组有 bug，所以提前退回 `nanmean`（它会安全返回 NaN）。`fnb._ureduce` 即 [u7-l1 讲过的通用归约框架](_function_base_impl.py#L3827-L3827)。

三路径分发内核：

[numpy/lib/_nanfunctions_impl.py:1074-1097](_nanfunctions_impl.py#L1074-L1097) —— `_nanmedian`：

```python
if axis is None or a.ndim == 1:
    part = a.ravel()
    if out is None:
        return _nanmedian1d(part, overwrite_input)
    else:
        out[...] = _nanmedian1d(part, overwrite_input)
        return out
else:
    # for small medians use sort + indexing which is still faster than
    # apply_along_axis
    # benchmarked with shuffled (50, 50, x) containing a few NaN
    if a.shape[axis] < 600:
        return _nanmedian_small(a, axis, out, overwrite_input)
    result = np.apply_along_axis(_nanmedian1d, axis, a, overwrite_input)
    if out is not None:
        out[...] = result
    return result
```

注释里的 `600` 是 benchmark 出来的经验阈值：小数组时 `apply_along_axis` 的 Python 层开销大于排序本身，故改用基于 `np.ma` 的 `_nanmedian_small`。

`_nanmedian_small` 的「偷懒」实现：

[numpy/lib/_nanfunctions_impl.py:1100-1117](_nanfunctions_impl.py#L1100-L1117) —— 用掩码数组把 NaN 挡掉，直接调 `np.ma.median`：

```python
a = np.ma.masked_array(a, np.isnan(a))
m = np.ma.median(a, axis=axis, overwrite_input=overwrite_input)
for i in range(np.count_nonzero(m.mask.ravel())):
    warnings.warn("All-NaN slice encountered", RuntimeWarning, stacklevel=5)

fill_value = np.timedelta64("NaT") if m.dtype.kind == "m" else np.nan
if out is not None:
    out[...] = m.filled(fill_value)
    return out
return m.filled(fill_value)
```

这里复用了 `numpy.ma` 子包的 `median`——它天生忽略被 mask 的元素。`m.mask` 标记了哪些切片是「全 NaN」（结果不可靠），据此报警告。最后用 `m.filled(fill_value)` 把掩码位置填回合适的「缺失值」（时间增量类型用 `NaT`，其余用 `nan`）。

一维内核 `_nanmedian1d`：

[numpy/lib/_nanfunctions_impl.py:1057-1071](_nanfunctions_impl.py#L1057-L1071)：

```python
arr1d_parsed, _, overwrite_input = _remove_nan_1d(
    arr1d, overwrite_input=overwrite_input,
)
if arr1d_parsed.size == 0:
    # Ensure that a nan-esque scalar of the appropriate type (and unit)
    # is returned for `timedelta64` and `complexfloating`
    return arr1d[-1]
return np.median(arr1d_parsed, overwrite_input=overwrite_input)
```

`_remove_nan_1d` 是 [u9-l1 讲过的「不保序就地剔除 NaN」](_nanfunctions_impl.py#L144-L201) 基础设施，返回去掉 NaN 的一维数组。剔光时返回 `arr1d[-1]`（一定是 NaN，但保留了原 dtype，这对 `timedelta64`/`complex` 很重要——否则类型会丢）。

#### 4.3.4 代码实践

1. **实践目标**：观察 `nanmedian` 沿轴剔除 NaN 后取中位数的行为，并验证它与「手动剔除再 median」等价。
2. **操作步骤**：

```python
import numpy as np

a = np.array([[10.0, np.nan, 4.0], [3.0, 2.0, 1.0]])
print("整体:", np.nanmedian(a))       # 排序 [1,2,3,4,10] → 3.0
print("axis=0:", np.nanmedian(a, axis=0))  # [6.5, 2.0, 2.5]
print("axis=1:", np.nanmedian(a, axis=1))  # [7.0, 2.0]

# 手动验证第一列：[10, 3] 剔除后 median = (10+3)/2 = 6.5
col0 = a[:, 0]
print("手动:", np.median(col0))       # 6.5

# nanpercentile(..., 50) 等价于 nanmedian
print("等价:", np.nanpercentile(a, 50, axis=0))  # 与 nanmedian(axis=0) 相同
```

3. **需要观察的现象**：`nanmedian` 与「手动 median」结果一致；`nanpercentile(a, 50)` 与 `nanmedian(a)` 完全等价（这揭示了下一节的设计：中位数就是 50% 分位数）。
4. **预期结果**：第一列为 6.5，整体为 3.0。

#### 4.3.5 小练习与答案

**练习 1**：`_nanmedian` 为何要把 `axis is None` 和 `a.ndim==1` 合并到同一条路径？

**答案**：两者都可以 `ravel()` 成一维数组，然后用同一个一维内核 `_nanmedian1d` 处理，避免为「全展平」和「本就一维」写两套逻辑。

**练习 2**：`_nanmedian_small` 为什么用 `np.ma.median` 而不是自己写排序？

**答案**：`np.ma.median` 已经实现了「忽略被 mask 元素取中位数」的全部逻辑，直接复用能避免重复造轮子；对沿轴长度 < 600 的小数组，复用的开销低于 `apply_along_axis` 的 Python 循环开销。

---

### 4.4 nanpercentile / nanquantile / _nanquantile_unchecked：分位数的纯委托复用

#### 4.4.1 概念说明

百分位（`nanpercentile`，q∈[0,100]）与分位（`nanquantile`，q∈[0,1]）是统计中比中位数更一般的概念——中位数就是 50% 分位数。本讲的精彩之处在于：**这两个函数几乎不写任何「分位计算」代码**，全部复用 u7-l2 的 `_quantile_unchecked`。它们只负责两件事：

1. **参数规整**：把 `q` 缩放到 `[0,1]`（`nanpercentile` 除以 100）、做范围校验、处理 `weights`。
2. **剔除 NaN**：沿轴逐切片剔除 NaN，再对干净切片调 `_quantile_unchecked`。

这种「分层委托」是 NumPy 代码组织的典范：插值算法（虚索引、`_lerp`、13 种 method）只在 `_function_base_impl.py` 实现一次，NaN 版本只加一层「清洗」外壳。

#### 4.4.2 核心流程

两个公开函数（`nanpercentile`/`nanquantile`）的差别仅在 q 的预处理：

```
nanpercentile(a, q∈[0,100], ...)      nanquantile(a, q∈[0,1], ...)
  │                                       │
  ├─ 拒绝复数输入                            ├─ 拒绝复数输入
  ├─ weak_q = (type(q) in (int,float))     ├─ weak_q = (type(q) in (int,float))
  ├─ q = q / 100                            ├─ q = asanyarray(q)
  ├─ _quantile_is_valid(q) 校验 [0,1]       ├─ _quantile_is_valid(q) 校验 [0,1]
  ├─ weights 合法性校验                      ├─ weights 合法性校验
  └──────────────┬──────────────────────────┘
                 ▼
   _nanquantile_unchecked(a, q, axis, out, overwrite_input, method,
                          keepdims, weights, weak_q)
                 │
                 ├─ a.size==0 → 退回 nanmean
                 └─ fnb._ureduce(a, func=_nanquantile_ureduce_func, ...)
                           │
                           ▼  （_ureduce 把多轴翻译成单轴后调用）
                   _nanquantile_ureduce_func(a, q, ...)
                           │
                  ┌────────┴─────────┐
                  ▼                  ▼
        axis None/1D:             多维:
        _nanquantile_1d(ravel)     apply_along_axis(_nanquantile_1d, axis)
                           │
                           ▼
                   _nanquantile_1d(arr1d, q):
                     ① _remove_nan_1d(arr1d)   # 剔除 NaN
                     ② 若剔光 → 返回全 NaN
                     ③ fnb._quantile_unchecked(arr1d, q, method, ...)  # ← u7-l2 算法
```

最底层的 `_nanquantile_1d` 就是「剔除 NaN + 调 `_quantile_unchecked`」，逻辑极其清爽。

#### 4.4.3 源码精读

`nanpercentile` 的参数预处理：

[numpy/lib/_nanfunctions_impl.py:1374-1395](_nanfunctions_impl.py#L1374-L1395)：

```python
a = np.asanyarray(a)
if a.dtype.kind == "c":
    raise TypeError("a must be an array of real numbers")

weak_q = type(q) in (int, float)  # use weak promotion for final result type
q = np.true_divide(q, 100, out=...)
if not fnb._quantile_is_valid(q):
    raise ValueError("Percentiles must be in the range [0, 100]")

if weights is not None:
    if method != "inverted_cdf":
        ...  # 只有 inverted_cdf 支持 weights
    ...
    weights = _weights_are_valid(weights=weights, a=a, axis=axis)
    if np.any(weights < 0):
        raise ValueError("Weights must be non-negative.")

return _nanquantile_unchecked(
    a, q, axis, out, overwrite_input, method, keepdims, weights, weak_q)
```

注意 `q = np.true_divide(q, 100, out=...)`——`out=...` 里的 `...`（Ellipsis）是个占位技巧，保证 `true_divide` 返回数组而非就地操作。`weak_q` 标记 q 是否是纯 Python `int/float`（而非数组），用于最后决定结果类型用「弱提升」。`_quantile_is_valid` 是 [u7-l2 讲过的范围校验](_function_base_impl.py#L4531-L4531)，对小于 10 个元素的小数组用循环逐个查、否则用 `min/max`，避免大开销的归约。

`nanquantile` 与之几乎一致，唯一差别是不除以 100，而是 `q = np.asanyarray(q)`（[numpy/lib/_nanfunctions_impl.py:1553-1554](_nanfunctions_impl.py#L1553-L1554)）。

`_nanquantile_unchecked` 的委托：

[numpy/lib/_nanfunctions_impl.py:1574-1599](_nanfunctions_impl.py#L1574-L1599)：

```python
"""Assumes that q is in [0, 1], and is an ndarray"""
# apply_along_axis in _nanpercentile doesn't handle empty arrays well,
# so deal them upfront
if a.size == 0:
    return np.nanmean(a, axis, out=out, keepdims=keepdims)
return fnb._ureduce(a,
                    func=_nanquantile_ureduce_func,
                    q=q, weights=weights, keepdims=keepdims,
                    axis=axis, out=out, overwrite_input=overwrite_input,
                    method=method, weak_q=weak_q)
```

又是「空数组退回 `nanmean` + `_ureduce` 套壳」，与 `nanmedian` 同构。

最底层的一维内核 `_nanquantile_1d`：

[numpy/lib/_nanfunctions_impl.py:1659-1681](_nanfunctions_impl.py#L1659-L1681)：

```python
arr1d, weights, overwrite_input = _remove_nan_1d(arr1d,
    second_arr1d=weights, overwrite_input=overwrite_input)
if arr1d.size == 0:
    # convert to scalar
    return np.full(q.shape, np.nan, dtype=arr1d.dtype)[()]

return fnb._quantile_unchecked(
    arr1d, q, overwrite_input=overwrite_input, method=method,
    weights=weights, weak_q=weak_q,
)
```

这就是本讲与 u7-l2 的交汇点：剔除 NaN 后，直接调用 [u7-l2 的 `_quantile_unchecked`](_function_base_impl.py#L4509-L4509)，所有虚索引、插值、13 种 method 的复杂度都藏在里面。`_remove_nan_1d` 还顺带同步剔除了 `weights`（`second_arr1d` 参数），保证权重与数据对齐。剔光时用 `np.full(q.shape, nan, dtype=arr1d.dtype)[()]` 造一个与 q 形状一致的全 NaN 结果，末尾 `[()]` 把 0 维数组转成标量。

#### 4.4.4 代码实践

1. **实践目标**：对含 NaN 的二维数据沿轴计算 `nanmean` 与 `nanpercentile(q=90)`，体会分位数与均值的差异（这是本讲综合要求的实践）。
2. **操作步骤**：

```python
import numpy as np

np.random.seed(0)
a = np.random.rand(4, 5)
a[0, 1] = np.nan
a[2, 3] = np.nan
a[2, 4] = np.nan
print("数据:\n", a)

# 沿 axis=1（每行）算均值与 90 分位
print("每行 nanmean:", np.nanmean(a, axis=1))
print("每行 nanp90 :", np.nanpercentile(a, 90, axis=1))

# 验证：第一行手动剔 NaN 后算 90 分位
row0 = a[0][~np.isnan(a[0])]
print("手动 row0 p90:", np.percentile(row0, 90))

# nanquantile 与 nanpercentile 等价（q 差 100 倍）
print("nanquantile 0.9:", np.nanquantile(a, 0.9, axis=1))
print("nanpercentile 90:", np.nanpercentile(a, 90, axis=1))  # 相同

# 对比不同 method
print("linear  :", np.nanpercentile(a[0], 90, method="linear"))
print("lower   :", np.nanpercentile(a[0], 90, method="lower"))
print("higher  :", np.nanpercentile(a[0], 90, method="higher"))
```

3. **需要观察的现象**：`nanpercentile(90)` 恒大于等于 `nanmean`（分位偏高）；`nanquantile(0.9)` 与 `nanpercentile(90)` 数值完全相同；不同 `method` 在非整数位置上取值不同（`linear` 插值，`lower`/`higher` 取相邻样本）。
4. **预期结果**：`nanmean(axis=1)` 与 `nanpercentile(90, axis=1)` 长度均为 4；`nanquantile(0.9)` == `nanpercentile(90)`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `nanpercentile`/`nanquantile` 不像 `nansum` 那样用「替换成单位元」的招数？

**答案**：分位数依赖排序，把 NaN 替换成任何固定值（0 或 ±inf）都会插入到排序序列的某个位置，污染分位点。必须**物理剔除** NaN 再排序。

**练习 2**：`_nanquantile_unchecked` 的名字里 `unchecked` 指什么「未检查」？

**答案**：指**不再检查 q 的范围合法性**（`_quantile_is_valid` 已在公开函数里做过）。它假设 q 已是 `[0,1]` 内的 ndarray，直接进入计算。这是把校验与计算分离的常见模式，避免 `_ureduce` 内层重复校验。

---

## 5. 综合实践

把本讲四组函数串起来，模拟一次「带缺失值的数据清洗与统计」：

```python
import numpy as np
import warnings

# 1. 构造一份带缺失值的二维「成绩单」（5 学生 × 4 科目）
np.random.seed(42)
scores = np.random.uniform(50, 100, size=(5, 4))
scores[scores < 60] = np.nan   # 不及格的记为缺失
print("原始成绩（含缺失）:\n", scores)

# 2. 用本讲四个函数做沿科目轴（axis=0）的统计
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    mean   = np.nanmean(scores, axis=0)
    std    = np.nanstd(scores, axis=0, ddof=1)
    median = np.nanmedian(scores, axis=0)
    p90    = np.nanpercentile(scores, 90, axis=0)

print("每科 nanmean  :", mean)
print("每科 nanstd   :", std)
print("每科 nanmedian:", median)
print("每科 p90      :", p90)

# 3. 解释统计量之间的关系
#    - median 应接近 mean（若分布对称）
#    - p90 应大于 mean（高分尾部）
#    - 全 NaN 的科目会触发 "Mean of empty slice" 警告并得到 nan
print("\n触发的警告数:", len(w))
```

**实践要点**：

1. 观察 `axis=0` 让每个统计量变成「每科一个值」（长度 4）。
2. 若某科全是 NaN，`nanmean` 报 `Mean of empty slice`、`nanstd` 报 `Degrees of freedom <= 0`，对应位置均为 `nan`——这正是 4.1 与 4.2 讲的「全 NaN 切片」处理。
3. 试着把 `nanpercentile(scores, 90, axis=0, method="lower")` 改成不同 `method`，对比 `median`（即 `method` 不影响 50% 时的中位，但影响非整数分位）。
4. **进阶**：阅读 `numpy/lib/tests/test_nanfunctions.py` 中 `TestNanFunctions_Median`（约 843 行起）与 `TestNanFunctions_Percentile`（约 1052 行起）的测试断言，找出 NumPy 用哪些不变量钉住「NaN 剔除后结果 == 干净数组直接算」这一等价关系。

## 6. 本讲小结

- **`nanmean`** 用「替换 NaN 为 0 → `sum` 求和 → `sum(~mask)` 数有效个数 → `_divide_by_count` 安全除」三段式；整数输入因 `mask=None` 直接走 `np.mean` 自动升 `float64`；`cnt==0` 时靠 `0/0` 天然得 NaN。
- **`nanvar`** 在 `nanmean` 基础上多了「偏差-清零-平方」就地三步，关键是 `_copyto(arr, 0, mask)` 必须把 NaN 位置的虚假偏差清零；`dof<=0` 时显式写回 NaN（因结果可能是 inf/负数而非天然 NaN）；`correction` 是 `ddof` 的 Array API 别名。
- **`nanstd`** 是 `nanvar` 的一层开方薄包装（`np.sqrt(var, out=var)` 就地完成）。
- **`nanmedian`** 走 `_ureduce` 套壳，内核 `_nanmedian` 分三条路径：1D/展平直调 `_nanmedian1d`、沿轴长度 < 600 用 `_nanmedian_small`（复用 `np.ma.median`）、否则 `apply_along_axis`；一维内核用 `_remove_nan_1d` 物理剔除 NaN 再 `np.median`。
- **`nanpercentile`/`nanquantile`** 几乎不做分位计算，只做参数规整（q÷100 或 asanyarray、范围校验、weights 校验）后委托 `_nanquantile_unchecked` → `_ureduce` → `_nanquantile_1d`（剔除 NaN）→ **u7-l2 的 `_quantile_unchecked`**，是「清洗外壳 + 复用算法内核」的分层典范。
- 贯穿本讲的两条主线：① 需要**除法/排序**的函数不能靠「替换单位元」，必须**先数有效个数（`cnt`）或先物理剔除 NaN（`_remove_nan_1d`）**；② `_ureduce`（u7-l1）+ `_divide_by_count`/`_remove_nan_1d`（u9-l1）+ `_quantile_unchecked`（u7-l2）三大基础设施被反复复用，本讲几乎不发明新机制。

## 7. 下一步学习建议

- 本讲已覆盖 `_nanfunctions_impl.py` 的全部「统计型」NaN 函数。结合 u9-l1，`numpy/lib` 的 NaN 子主题已基本读完。建议回头重读 [_nanfunctions_impl.py 的模块文档字符串](_nanfunctions_impl.py#L1-L22)，对照 14 个公开函数，确认每个都能讲清实现。
- 接下来可进入 **u10-l1（类型检查与标量判定）**，转向 `_type_check_impl.py`/`_ufunclike_impl.py`，其中 `nan_to_num`（把 inf/nan 替换为有限值）与本讲的「NaN 清洗」主题呼应。
- 若对「分层委托」设计感兴趣，可对比阅读 [_function_base_impl.py 的 `percentile`/`quantile`](_function_base_impl.py) 公开函数与 `_quantile_unchecked` 的关系，体会「公开校验层 + `_unchecked` 计算层 + `_ureduce` 归约层」的三层拆分——本讲的 `nanpercentile` 正是在最外又套了一层「NaN 清洗」。
- 想验证理解，可运行 `numpy.lib.test('test_nanfunctions')`（注意 u1-l3 讲过的 `label` 与 `tests=` 区别），重点看 `TestNanFunctions_MeanVarStd`、`TestNanFunctions_Median`、`TestNanFunctions_Percentile`、`TestNanFunctions_Quantile` 四组。

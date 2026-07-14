# 归约框架、中位数与协方差/相关系数

## 1. 本讲目标

本讲聚焦 `numpy/lib/_function_base_impl.py` 中的五个核心函数，它们都属于「统计归约」家族，但分工不同。读完本讲，你应当能够：

- 说清 `_ureduce` 这个**通用归约框架**如何把 `axis`、`keepdims`、`out` 三个参数统一处理好，并把「多轴归约」翻译成「单轴归约」。
- 理解 `median` 为什么用 `partition`（分区）而非全排序来取中位数，以及 `overwrite_input` 如何影响内存与输入数组。
- 读懂 `cov` 的 `rowvar` / `bias` / `ddof` / `fweights` / `aweights` 五个参数，并能手算出归一化因子 `fact`。
- 理解 `corrcoef` 如何在 `cov` 的基础上做对角归一化得到皮尔逊相关系数矩阵。

本讲承接 [u1-l2](u1-l2-module-organization.md) 已经建立的「dispatcher + impl 双函数写法」「实现藏 `_impl`」认知框架，是后续 [u7-l2（百分位与分位数）](u7-l2-percentile-interpolation.md)与 [u7-l3（直方图）](u7-l3-histogram-binning.md) 的基础。

## 2. 前置知识

### 归约（reduction）

「归约」指把数组沿某些轴**压缩**：例如把一个 `(3, 4, 5)` 的数组沿 `axis=1` 求和，结果形状变成 `(3, 5)`，第 1 维被「消灭」。`sum` / `mean` / `median` / `var` 都是归约。

归约函数有三个共同的「形状控制」参数，理解它们是本讲的关键：

| 参数 | 作用 | 不传时的默认 |
|------|------|--------------|
| `axis` | 沿哪些轴归约；`None` 表示拍平所有轴 | `None` |
| `keepdims` | 归约后是否把被消灭的轴保留为长度 1 | `False` |
| `out` | 把结果写入这个预分配数组 | `None` |

这三个参数看似简单，组合起来却要处理很多边界（多轴、与 `out` 的形状匹配等）。`_ureduce` 就是把它们统一管理起来的私有框架。

### 中位数与分区（partition）

中位数要求把数据排序后取「中间值」。但**取中位数并不需要把整组数排好序**——只需要保证「第 k 小的数」落到第 k 个位置即可。numpy 的 `partition` 就是干这件事的，平均时间复杂度 \(O(n)\)，比全排序 `sort` 的 \(O(n \log n)\) 快。

### 协方差与相关系数

给定 \(N\) 个观测、\(D\) 个变量，把数据排成矩阵 \(X\)（每行一个变量，每列一次观测）。协方差矩阵的第 \(ij\) 个元素衡量变量 \(i\) 与变量 \(j\)「同涨同跌」的程度：

\[ C_{ij} = \frac{1}{N - \text{ddof}} \sum_{n} (x_i^{(n)} - \bar{x}_i)(x_j^{(n)} - \bar{x}_j) \]

把它按对角线归一化，就得到相关系数（落在 \([-1, 1]\)）：

\[ R_{ij} = \frac{C_{ij}}{\sqrt{C_{ii} \, C_{jj}}} \]

## 3. 本讲源码地图

本讲所有代码都在同一个文件里：

| 符号 | 角色 | 可见性 |
|------|------|--------|
| `_ureduce` | 通用归约框架，统一处理 axis/keepdims/out | 私有（不在 `__all__`） |
| `median` | 公开中位数函数，是 `_ureduce` 的薄包装 | 公开（在 `__all__`） |
| `_median` | 中位数的真正实现（partition 取中） | 私有 |
| `cov` | 公开协方差矩阵函数 | 公开 |
| `corrcoef` | 公开相关系数矩阵函数，复用 `cov` | 公开 |
| `_median_dispatcher` / `_cov_dispatcher` / `_corrcoef_dispatcher` | NEP-18 派发器 | 私有 |

补充：`median` 末尾会用到的 NaN 检测函数 `_median_nancheck` 位于同目录的 `_utils_impl.py`。

## 4. 核心概念与源码讲解

### 4.1 `_ureduce`：通用归约框架

#### 4.1.1 概念说明

很多归约函数（`median`、`percentile`、`nanmedian`……）本身只支持「沿单一轴」归约。但用户常常想沿**多个轴**一起归约（例如 `np.median(a, axis=(0, 2))`）。如果让每个函数各自实现多轴逻辑，会有大量重复代码。

`_ureduce` 解决的就是这个问题：它接收一个**只需要懂单轴**的底层函数 `func`，由框架负责：

1. 把任意 `axis`（标量、元组、`None`）规整。
2. 当 `axis` 是多个轴时，通过「移轴 + reshape」把它们**合并成一个轴**，再让 `func` 按单轴（`axis=-1`）处理。
3. 统一处理 `keepdims`：归约完之后，把被消灭的轴「补回」成长度 1 的轴。
4. 统一处理 `out`：把预分配数组切出正确视图写入。

一句话：**底层 `func` 只需懂单轴，多轴合并、补轴、写 `out` 全交给 `_ureduce`**。

#### 4.1.2 核心流程

```text
_ureduce(a, func, keepdims=False, **kwargs):
    a = asanyarray(a)
    axis = kwargs['axis']; out = kwargs['out']
    若 keepdims 是 _NoValue 哨兵 → 当作 False

    若 axis 不是 None:
        axis = normalize_axis_tuple(axis, a.ndim)   # 规整成非负整数元组
        若 keepdims 且 out 非 None:
            从 out 里切出「降维视图」给 kwargs['out']
        若 len(axis) == 1:
            kwargs['axis'] = axis[0]                # 单轴：原样传递
        否则:                                        # 多轴：合并
            keep = 不参与归约的轴
            reshape_arr: moveaxis(keep→前) → reshape(合并待归约轴为 -1)
            a = reshape_arr(a); weights 也同步 reshape
            kwargs['axis'] = -1
    否则若 keepdims 且 out 非 None:
        kwargs['out'] = out 切成全 0 索引视图

    r = func(a, **kwargs)                            # ← 真正做归约
    若 out 非 None: return out

    若 keepdims:                                      # 把消灭的轴补回长度 1
        在 r 上 newaxis 插入对应位置
    return r
```

多轴合并的核心招数是这段：把「不该归约的轴」`moveaxis` 挪到最前面，再把剩下的「待归约轴」`reshape` 成一整条 `-1` 维。这样原本的 `(2, 3, 4, 5)` 沿 `axis=(0, 2)` 归约，会被重排成 `(3, 5, 8)` 然后沿最后一轴归约——对底层 `func` 而言，它「看到的」永远只是单轴。

#### 4.1.3 源码精读

入口与参数规整：[_function_base_impl.py:L3853-L3862](_function_base_impl.py#L3853-L3862)

```python
a = np.asanyarray(a)
axis = kwargs.get('axis')
out = kwargs.get('out')

if keepdims is np._NoValue:
    keepdims = False

nd = a.ndim
if axis is not None:
    axis = _nx.normalize_axis_tuple(axis, nd)
```

注意 `keepdims is np._NoValue`：numpy 用一个特殊的 `_NoValue` 哨兵来区分「用户没传」与「用户显式传了 `False`」，这样能在文档里隐藏 `keepdims` 的存在感，又能给警告（未传 `keepdims` 时给出 deprecation 提示）。

多轴合并的关键：[_function_base_impl.py:L3869-L3887](_function_base_impl.py#L3869-L3887)

```python
if len(axis) == 1:
    kwargs['axis'] = axis[0]
else:
    keep = sorted(set(range(nd)) - set(axis))
    nkeep = len(keep)

    def reshape_arr(a):
        # move axis that should not be reduced to front
        a = np.moveaxis(a, keep, range(nkeep))
        # merge reduced axis
        return a.reshape(a.shape[:nkeep] + (-1,))

    a = reshape_arr(a)
    weights = kwargs.get("weights")
    if weights is not None:
        kwargs["weights"] = reshape_arr(weights)
    kwargs['axis'] = -1
```

`keep` 是「幸存轴」（不归约的轴）。`reshape_arr` 先把它们挪到最前，再把剩余维度拍扁——这就是「多轴 → 单轴」的全部魔法。注意 `weights` 也要按同样规则变形，否则与数据对不上。

`out` 与 `keepdims` 的补轴收尾：[_function_base_impl.py:L3892-L3906](_function_base_impl.py#L3892-L3906)

```python
r = func(a, **kwargs)

if out is not None:
    return out

if keepdims:
    if axis is None:
        index_r = (np.newaxis, ) * nd
    else:
        index_r = tuple(
            np.newaxis if i in axis else slice(None)
            for i in range(nd))
    r = r[(Ellipsis, ) + index_r]

return r
```

`keepdims` 的补轴就是在结果上用 `np.newaxis` 把对应位置「撑开」成长度 1。`Ellipsis` 前缀是为了兼容结果可能是多维的情形。注意传了 `out` 时直接返回 `out`（结果已经写进去了，不再补轴）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `_ureduce` 如何把多轴归约转成单轴调用。

**操作步骤**：

```python
# 示例代码（不是 numpy 原有代码）
import numpy as np
from numpy.lib._function_base_impl import _ureduce

a = np.arange(2*3*4*5).reshape(2, 3, 4, 5)

def my_mean(a, axis=None, **kw):
    print(f"  func 收到 shape={a.shape}, axis={axis}")
    return np.mean(a, axis=axis)

# 单轴：func 看到原样 axis
print("axis=(1):")
_ureduce(a, my_mean, axis=(1,))

# 多轴：func 看到合并后的单轴 -1
print("axis=(0,2):")
_ureduce(a, my_mean, axis=(0, 2))

# keepdims：结果补回长度为 1 的轴
r = _ureduce(a, my_mean, axis=(0, 2), keepdims=True)
print("keepdims 结果 shape:", r.shape)
```

**需要观察的现象**：

- `axis=(1,)` 时，`func` 收到 `shape=(2,3,4,5), axis=1`，原样传递。
- `axis=(0,2)` 时，`func` 收到的 `shape` 应是 `(3,5,8)`（2×4=8），`axis=-1`——多轴被合并了。
- `keepdims=True` 时，最终结果形状应为 `(1,3,1,5)`。

**预期结果**：以上三条全部符合。`_ureduce` 是私有函数，但可经完整模块路径访问；这只是用来观察框架行为，不建议在生产代码中直接调用。

#### 4.1.5 小练习与答案

**练习 1**：当 `axis=None` 时，`_ureduce` 为什么不调用 `reshape_arr`？

**参考答案**：`axis=None` 表示对所有维度归约，此时 `func` 自身会被传 `axis=None`（拍平处理），框架不需要合并轴；只需在 `keepdims=True` 时把结果用 `(np.newaxis,)*nd` 全部撑开即可（见源码 `if axis is None` 分支）。

**练习 2**：如果把一个带 `weights` 的归约交给 `_ureduce`，为什么框架要对 `weights` 也调用 `reshape_arr`？

**参考答案**：因为 `a` 被移轴 + reshape 成了新形状，`weights` 若不跟着做同样的变形，就会与新形状的 `a` 对不齐而出错。框架在合并轴时同步变形权重，正是为了保证二者轴结构一致。

---

### 4.2 `median` 与 `_median`：排序取中

#### 4.2.1 概念说明

中位数的定义很简单：排序后取中间值。但工程实现有两个讲究：

1. **用 `partition` 而非 `sort`**：取中位数只需要保证「中间那个（或两个）数」就位，不必把整个数组排好。`partition` 平均 \(O(n)\)，比 `sort` 的 \(O(n\log n)\) 快，尤其对大数组。
2. **用 `mean` 来「强制类型」**：整数输入的中位数可能是小数（如 `[1, 2]` 的中位数是 `1.5`），所以结果要升为 `float64`。numpy 的做法不是手动 `astype`，而是**对取出的中间值调用 `mean`**——`mean` 天然会把整数升成浮点。源码注释明说："Use mean in both odd and even case to coerce data type"。

`median` 是 u1-l2 讲过的「dispatcher + impl」双函数写法的典型：公开的 `median` 只是 `_ureduce` 的薄包装，真正的取中逻辑藏在私有的 `_median` 里。

`overwrite_input=True` 时，`_median` 会**就地**调用 `a.partition(kth)`，从而复用输入数组的内存、不再分配副本——代价是输入数组会被破坏（变成「部分排序」状态）。

#### 4.2.2 核心流程

```text
_median(a, axis, out, overwrite_input):
    sz = 沿 axis 的长度
    # 1. 算分区点 kth
    若 sz 偶数: kth = [sz//2 - 1, sz//2]        # 中间两个
    若 sz 奇数: kth = [(sz-1)//2]               # 中间一个
    若 dtype 可能含 NaN（浮点/日期时间）: kth 追加 -1   # 把最大值（含NaN）顶到末尾便于检测

    # 2. 分区
    若 overwrite_input: 就地 a.partition(kth)   # 破坏输入
    否则:                part = partition(a, kth)  # 返回新数组

    # 3. 取中间值
    indexer[axis] = 切出中间一个(奇数)或两个(偶数)
    rout = mean(part[indexer], axis=axis, out=out)   # mean 顺带把 int→float64

    # 4. NaN 检测
    若可能含 NaN: rout = _median_nancheck(part, rout, axis)
    return rout
```

为什么对浮点类型要给 `kth` 追加 `-1`？因为 `partition` 的 `kth=-1` 表示「把最大的元素放到末尾」。当数据含 `NaN` 时，`NaN` 在比较中被当作「最大」，会被顶到末尾；这样 `_median_nancheck` 只要扫一眼末尾就能判断是否含 `NaN`，决定是否把结果也替换为 `NaN`。

#### 4.2.3 源码精读

公开函数 `median` 的全部「实现」就是把活儿转给 `_ureduce`：[_function_base_impl.py:L3998-L3999](_function_base_impl.py#L3998-L3999)

```python
return _ureduce(a, func=_median, keepdims=keepdims, axis=axis, out=out,
                overwrite_input=overwrite_input)
```

可以看到，`median` 自身不处理 `keepdims`/多轴/`out`，全交给 `_ureduce`，自己只指定「真正干活的底层函数是 `_median`」。这正是 `_ureduce` 框架的价值。

分区点 `kth` 的计算：[_function_base_impl.py:L4007-L4021](_function_base_impl.py#L4007-L4021)

```python
# Set the partition indexes
if axis is None:
    sz = a.size
else:
    sz = a.shape[axis]
if sz % 2 == 0:
    szh = sz // 2
    kth = [szh - 1, szh]
else:
    kth = [(sz - 1) // 2]

# We have to check for NaNs (as of writing 'M' doesn't actually work).
supports_nans = np.issubdtype(a.dtype, np.inexact) or a.dtype.kind in 'Mm'
if supports_nans:
    kth.append(-1)
```

注意 `supports_nans` 同时覆盖浮点（`np.inexact`）和日期时间类型（`dtype.kind in 'Mm'`），但注释指出对 `'M'`（datetime）NaN 支持当时其实并未真正生效。

分区与 `overwrite_input` 的两条路径：[_function_base_impl.py:L4023-L4031](_function_base_impl.py#L4023-L4031)

```python
if overwrite_input:
    if axis is None:
        part = a.ravel()
        part.partition(kth)
    else:
        a.partition(kth, axis=axis)
        part = a
else:
    part = partition(a, kth, axis=axis)
```

`overwrite_input=True` 时直接 `a.partition(...)`，输入数组被改写；`=False` 时调用模块级的 `partition`（来自 `numpy._core.fromnumeric`），返回副本。

取中间值并强制类型：[_function_base_impl.py:L4039-L4055](_function_base_impl.py#L4039-L4055)

```python
indexer = [slice(None)] * part.ndim
index = part.shape[axis] // 2
if part.shape[axis] % 2 == 1:
    # index with slice to allow mean (below) to work
    indexer[axis] = slice(index, index + 1)
else:
    indexer[axis] = slice(index - 1, index + 1)
indexer = tuple(indexer)

# Use mean in both odd and even case to coerce data type,
# using out array if needed.
rout = mean(part[indexer], axis=axis, out=out)
if supports_nans and sz > 0:
    # If nans are possible, warn and replace by nans like mean would.
    rout = np.lib._utils_impl._median_nancheck(part, rout, axis)

return rout
```

奇数情况也用 `slice(index, index+1)` 切出长度 1 的切片（而非单个标量），是为了让随后的 `mean` 仍能沿该轴归约。这样奇偶两种情况统一走 `mean`，既算出了中位数，又顺手把整数升为 `float64`。

#### 4.2.4 代码实践

**实践目标**：验证 `overwrite_input=True` 确实破坏了输入数组，并观察整数输入被提升为浮点。

**操作步骤**：

```python
# 示例代码
import numpy as np

a = np.array([[10, 7, 4], [3, 2, 1]])
b = a.copy()
print("归约前 b=\n", b)
med = np.median(b, axis=1, overwrite_input=True)
print("median =", med)
print("归约后 b=\n", b)
print("原数组 a 是否还等于 b？", np.array_equal(a, b))

# 整数提升
print(np.median(np.array([1, 2])))        # 期望 1.5（float64）
```

**需要观察的现象**：

- `med` 应为 `[7., 2.]`。
- 归约后的 `b` 不再是原来的 `[[10,7,4],[3,2,1]]`——它被 `partition` 改成了「部分排序」状态。
- `np.array_equal(a, b)` 应为 `False`，证明 `overwrite_input` 破坏了输入。
- `np.median([1,2])` 返回 `1.5`，dtype 是 `float64`，证明整数被提升。

**预期结果**：以上全部符合。这正是 `overwrite_input` 文档里「Treat the input as undefined」的含义。

#### 4.2.5 小练习与答案

**练习 1**：对长度为 6 的偶数数组 `[3,1,4,1,5,9]`，`_median` 算出的 `kth` 是什么？

**参考答案**：`sz=6`，`sz%2==0`，`szh=3`，故 `kth=[2, 3]`（即 `szh-1` 与 `szh`），对应排序后中间两个位置。浮点类型还会再追加 `-1`。

**练习 2**：为什么 `_median` 要用 `mean` 计算，而不是直接对两个中间值用 `(a+b)/2`？

**参考答案**：注释明说 "to coerce data type"：`mean` 会自动把整数结果提升为 `float64`，而 `(a+b)/2` 在整数输入下可能仍是整数除法或需要额外显式转换；用 `mean` 一行同时完成「取平均」和「类型提升」，奇偶两路也能共用同一段代码。

---

### 4.3 `cov` 与 `corrcoef`：协方差与相关系数

#### 4.3.1 概念说明

`cov` 估计协方差矩阵，`corrcoef` 在它基础上做对角归一化得到相关系数矩阵。二者都不走 `_ureduce` 框架（因为它们不是「沿轴消灭维度」那种归约，而是矩阵运算），但同样采用「dispatcher + impl」写法。

`cov` 的参数多，但各有其位：

| 参数 | 含义 | 默认 |
|------|------|------|
| `m` | 数据，1D 或 2D | 必填 |
| `y` | 另一组变量，会与 `m` 拼到一起 | `None` |
| `rowvar` | `True`=每行一个变量；`False`=每列一个变量 | `True` |
| `bias` | `False`=无偏（除以 N-1）；`True`=有偏（除以 N） | `False` |
| `ddof` | 自由度修正，覆盖 `bias` 隐含值 | `None` |
| `fweights` | 整数频率权重（每个观测重复几次） | `None` |
| `aweights` | 相对权重（观测的「重要程度」） | `None` |

关键约定：`ddof=None` 时，`bias=False` → `ddof=1`（无偏），`bias=True` → `ddof=0`（有偏）。所以 `bias` 和 `ddof` 是同一件事的两种写法，`ddof` 优先。

#### 4.3.2 核心流程

`cov` 的流程（不含权重的简版）：

```text
cov(m, y, rowvar, bias, ddof, fweights, aweights, dtype):
    校验 ddof 为整数；m = asarray；维度 ≤ 2
    处理 y：concatenate 拼到 m 上
    X = array(m, ndmin=2)            # 保证至少 2 维
    若 not rowvar: X = X.T           # 统一成「行=变量」
    默认 dtype 至少 float64
    ddof 默认: bias=False→1, bias=True→0
    w = fweights * aweights          # 合成最终权重
    avg = average(X, axis=1, weights=w)        # 每个变量的加权均值
    fact = 归一化因子（见下）
    X -= avg[:, None]                # 中心化
    c = dot(X, (X*w).T) * (1/fact)   # 加权协方差
    return c.squeeze()
```

归一化因子 `fact` 有四种情况（这是 `cov` 最容易看晕的地方）：

- 无权重：\( \text{fact} = N - \text{ddof} \)
- `ddof=0`：\( \text{fact} = \sum w \)（即 `w_sum`）
- 只有 `fweights`：\( \text{fact} = \sum w - \text{ddof} \)
- 有 `aweights`：\( \text{fact} = \sum w - \text{ddof} \cdot \frac{\sum w \cdot a}{\sum w} \)

对应的加权协方差公式（记 \(v_1=\sum w\)，\(v_2=\sum w\cdot a\)）：

\[ C = \frac{v_1}{v_1^2 - \text{ddof}\cdot v_2} \, (X-\bar{X}) \, \text{diag}(w) \, (X-\bar{X})^{\mathsf{T}} \]

当 \(a\equiv 1\)（无 `aweights`）时，\(v_2=v_1\)，分母化为 \(v_1^2 - \text{ddof}\cdot v_1 = v_1(v_1-\text{ddof})\)，于是整个因子退化为 \(\frac{1}{v_1-\text{ddof}}\)，与「无权重的 \(1/(N-\text{ddof})\)」一致。

`corrcoef` 极简，全部建立在 `cov` 之上：

```text
corrcoef(x, y, rowvar, dtype):
    c = cov(x, y, rowvar, dtype=dtype)
    若 c 是标量（单变量）: return c / c     # 0→nan, 其他→1
    d = diag(c)                       # 各变量方差
    stddev = sqrt(d.real)
    c /= stddev[:, None]              # 每行除以对应 stddev
    c /= stddev[None, :]              # 每列除以对应 stddev → 即除以 √(Cii*Cjj)
    clip(c.real, -1, 1)               # 修正浮点误差
    若复数: clip(c.imag, -1, 1)
    return c
```

#### 4.3.3 源码精读

`cov` 的数据规整与 `rowvar`/`y` 处理：[_function_base_impl.py:L2821-L2836](_function_base_impl.py#L2821-L2836)

```python
if dtype is None:
    if y is None:
        dtype = np.result_type(m, np.float64)
    else:
        dtype = np.result_type(m, y, np.float64)

X = array(m, ndmin=2, dtype=dtype)
if not rowvar and m.ndim != 1:
    X = X.T
if X.shape[0] == 0:
    return np.array([]).reshape(0, 0)
if y is not None:
    y = array(y, copy=None, ndmin=2, dtype=dtype)
    if not rowvar and y.shape[0] != 1:
        y = y.T
    X = np.concatenate((X, y), axis=0)
```

要点：`ndmin=2` 保证 1D 输入也被当作「一个变量、多次观测」（即形状 `(1, N)`）；`rowvar=False` 时转置；`y` 作为额外变量沿 `axis=0` 拼进来，所以 `np.cov(x, y)` 与先 `np.stack` 再 `cov` 等价。

`ddof` 默认值由 `bias` 推导：[_function_base_impl.py:L2838-L2842](_function_base_impl.py#L2838-L2842)

```python
if ddof is None:
    if bias == 0:
        ddof = 1
    else:
        ddof = 0
```

四种 `fact` 分支：[_function_base_impl.py:L2880-L2893](_function_base_impl.py#L2880-L2893)

```python
# Determine the normalization
if w is None:
    fact = X.shape[1] - ddof
elif ddof == 0:
    fact = w_sum
elif aweights is None:
    fact = w_sum - ddof
else:
    fact = w_sum - ddof * sum(w * aweights) / w_sum

if fact <= 0:
    warnings.warn("Degrees of freedom <= 0 for slice",
                  RuntimeWarning, stacklevel=2)
    fact = 0.0
```

`fact <= 0`（自由度耗尽，例如只有 1 个观测却用默认 `ddof=1`）会发 `RuntimeWarning` 并把 `fact` 置 0，结果相应为 nan/inf。

中心化、加权点积与归一：[_function_base_impl.py:L2895-L2902](_function_base_impl.py#L2895-L2902)

```python
X -= avg[:, None]
if w is None:
    X_T = X.T
else:
    X_T = (X * w).T
c = dot(X, X_T.conj())
c *= np.true_divide(1, fact)
return c.squeeze()
```

`X -= avg[:, None]` 即每行（每个变量）减去自己的均值；`(X * w).T` 把权重塞进点积；`conj()` 是为了支持复数（保证 Hermitian）。最后的 `squeeze()` 把单变量情形的 `(1,1)` 压成标量。

`corrcoef` 全部实现：[_function_base_impl.py:L3028-L3046](_function_base_impl.py#L3028-L3046)

```python
c = cov(x, y, rowvar, dtype=dtype)
try:
    d = diag(c)
except ValueError:
    # scalar covariance
    # nan if incorrect value (nan, inf, 0), 1 otherwise
    return c / c
stddev = sqrt(d.real)
c /= stddev[:, None]
c /= stddev[None, :]

# Clip real and imaginary parts to [-1, 1].  ...
np.clip(c.real, -1, 1, out=c.real)
if np.iscomplexobj(c):
    np.clip(c.imag, -1, 1, out=c.imag)

return c
```

`c /= stddev[:, None]` 再 `c /= stddev[None, :]` 等价于除以外积 \(\sqrt{C_{ii}}\sqrt{C_{jj}}\)，正好实现 \(R_{ij}=C_{ij}/\sqrt{C_{ii}C_{jj}}\)。`clip` 是对浮点误差的兜底——理论上对角线应是 1、off-diagonal 应在 \([-1,1]\)，但浮点运算可能让它略微越界。

#### 4.3.4 代码实践

**实践目标**：用 `cov` 与 `corrcoef` 计算两列数据的协方差矩阵与相关系数，并验证对称性（这是本讲的指定实践任务）。

**操作步骤**：

```python
# 示例代码
import numpy as np

rng = np.random.default_rng(0)
# x 与 y 故意做成强正相关
x = np.arange(10, dtype=np.float64)
y = 2.0 * x + rng.normal(0, 0.5, size=10)   # y ≈ 2x + 噪声

# 方式一：当作两个变量（行），各 10 次观测
C = np.cov(np.vstack([x, y]))
print("协方差矩阵 C =\n", C)
print("C 是否对称：", np.allclose(C, C.T))
print("对角线 C[0,0], C[1,1]（即各自方差）:", C[0, 0], C[1, 1])

# 相关系数矩阵
R = np.corrcoef(x, y)
print("相关系数矩阵 R =\n", R)
print("R 是否对称：", np.allclose(R, R.T))
print("R 对角线是否为 1：", np.allclose(np.diag(R), 1.0))
print("x 与 y 的相关系数 R[0,1] =", R[0, 1])

# 验证 bias/ddof：除以 N 还是 N-1
C_unbiased = np.cov(x, y, ddof=1)      # 默认（无偏，除以 N-1=9）
C_biased   = np.cov(x, y, bias=True)   # 有偏，除以 N=10
print("无偏/有偏比值（应为 10/9 ≈ 1.111）:", (C_unbiased/C_biased)[0, 0])
```

**需要观察的现象**：

- `C` 是 2×2 对称矩阵，对角线是 `x` 和 `y` 各自的方差，off-diagonal 是协方差。
- `R` 对角线为 `1.0`，`R[0,1]` 接近 `1`（因为 `y≈2x`，强正相关）。
- 无偏/有偏比值应为 \( N/(N-1) = 10/9 \approx 1.111 \)。推导：方差是「平方偏差和 ÷ 除数」，无偏除以 \(N-1=9\)、有偏除以 \(N=10\)，分子相同，故 \(C_{\text{unbiased}} = C_{\text{biased}} \times \tfrac{10}{9}\)，比值正是 \(10/9\)。

**预期结果**：`C` 与 `R` 都对称，`R` 对角线为 1，`R[0,1]` 接近 1，无偏/有偏比值约为 1.111。

#### 4.3.5 小练习与答案

**练习 1**：`np.cov(x)`（`x` 是 1D）为什么返回标量而不是矩阵？

**参考答案**：1D 输入被 `ndmin=2` 当作「1 个变量、N 次观测」，即形状 `(1, N)`，于是协方差矩阵是 `1×1`；最后的 `c.squeeze()` 把它压成标量，返回的就是 `x` 的方差。

**练习 2**：`corrcoef` 为什么要 `np.clip(c.real, -1, 1)`？

**参考答案**：理论上相关系数应在 \([-1,1]\)，但浮点除法与开方的累积误差可能让结果略微越界（如 `1.0000000002`）。`clip` 把实部（以及复数的虚部）夹回区间，让结果在数值上满足定义，避免下游代码因「超出 1」而误判。

**练习 3**：给定 `fweights=[2,1,1]`，它与「把第一个观测复制一份」在协方差计算上等价吗？

**参考答案**：等价。`fweights` 就是「整数频率权重」，含义正是每个观测向量重复的次数。`cov` 把它合进 `w` 并在归一化因子里用 `w_sum = sum(f)`，与物理上展开成重复观测后再求协方差得到相同结果。

---

## 5. 综合实践

把本讲的三个主题（归约框架、中位数、协方差）串成一个完整任务。

**任务**：模拟一次小型数据分析——生成一组二维数据，分别计算每列的中位数与整体的协方差/相关系数，并验证 `_ureduce` 框架的 `keepdims` 行为。

```python
# 示例代码
import numpy as np
from numpy.lib._function_base_impl import _ureduce

rng = np.random.default_rng(42)
# 两个变量，各 8 次观测，行=变量
data = rng.normal(size=(2, 8))
data[1] = data[0] * 1.5 + rng.normal(0, 0.2, size=8)   # 让两行强相关

# (1) 用 median 沿 axis=1 求每个变量的中位数
med = np.median(data, axis=1)
print("每个变量的中位数:", med)

# (2) 协方差与相关系数
C = np.cov(data)
R = np.corrcoef(data)
print("协方差矩阵:\n", C)
print("相关系数矩阵:\n", R)
print("C 对称?", np.allclose(C, C.T), " R 对称?", np.allclose(R, R.T))

# (3) 观察 _ureduce 的 keepdims：median 的 keepdims=True
med_kd = np.median(data, axis=1, keepdims=True)
print("keepdims=True 的中位数 shape:", med_kd.shape)   # 期望 (2, 1)

# (4) 多轴归约：把 (2,8) 整体求中位数
med_all = np.median(data, axis=(0, 1))
print("整体中位数:", med_all)
```

**验证清单**：

- `med` 长度为 2，是每行的中位数。
- `C` 是 2×2 对称矩阵；`R` 对角线为 1，`R[0,1]` 接近 1（因为第二行是第一行的强线性函数）。
- `med_kd.shape` 为 `(2, 1)`——这正是 `_ureduce` 在 `keepdims=True` 时补回长度 1 轴的效果。
- `med_all` 是单个标量（`_ureduce` 把两个轴合并成一个后取中位数）。

## 6. 本讲小结

- `_ureduce` 是 numpy.lib 的**通用归约框架**：它把 `axis`（含多轴）、`keepdims`、`out` 统一处理好，并通过「移轴 + reshape」把**多轴归约合并成单轴归约**，让底层 `func` 只需懂单轴即可。
- `median` 是 `_ureduce` 的薄包装，真正逻辑在私有 `_median`；`_median` 用 **`partition`（而非 `sort`）** 取中，分区点 `kth` 在偶数时取中间两个、浮点时追加 `-1` 以便 NaN 检测，最后用 `mean` 顺手把整数提升为 `float64`。
- `overwrite_input=True` 会让 `median` **就地分区**、破坏输入数组，换取内存节省。
- `cov` 的归一化因子 `fact` 有四种分支，对应无权重 / `ddof=0` / 仅 `fweights` / 含 `aweights` 四种情形，核心运算是中心化后的加权点积 `dot(X, (X*w).T) * (1/fact)`。
- `corrcoef` 完全建立在 `cov` 之上，靠「除以 stddev 外积」做对角归一化，并用 `clip` 兜底浮点误差，结果落在 `[-1, 1]`。
- 五个函数（`_ureduce`、`_median`、`median`、`cov`、`corrcoef`）体现了 numpy.lib 的两种复用方式：横向的 `_ureduce` 框架复用（共享 axis/keepdims/out 处理）与纵向的 `corrcoef`→`cov` 调用复用。

## 7. 下一步学习建议

- 继续学习 [u7-l2 百分位与分位数插值算法](u7-l2-percentile-interpolation.md)：`percentile`/`quantile` 与本讲的 `median` 共用 `_ureduce` 框架，并复用同样的 `partition` 思路，但额外引入了「虚索引 + 插值权重」机制，是 `_median` 的「插值增强版」。
- 学习 [u7-l3 直方图与自动分箱估计器](u7-l3-histogram-binning.md)：另一类基于分箱的统计归约。
- 推荐阅读的真实源码：
  - [_function_base_impl.py:L3827-L3906](_function_base_impl.py#L3827-L3906) ——`_ureduce` 全文。
  - [_function_base_impl.py:L2687-L2902](_function_base_impl.py#L2687-L2902) ——`cov` 全文，重点看 `fact` 四分支。
  - [_utils_impl.py:L407](_utils_impl.py#L407) ——`_median_nancheck`，理解 `kth.append(-1)` 的检测原理。

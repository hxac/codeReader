# unique 家族与集合运算

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `np.unique` 内部有「哈希快路」和「排序慢路」两套引擎，并能判断什么情况下走哪条路。
- 解释 `unique` 的三个可选返回值 `return_index` / `return_inverse` / `return_counts` 各自的含义与实现方式，理解 `inverse` 如何重建原数组。
- 区分 `unique_all` / `unique_counts` / `unique_inverse` / `unique_values` 这四个 Array API 形式与经典 `unique` 的差异（尤其是 `equal_nan=False`）。
- 说清 `isin` 的 `kind` 参数（`None` / `'sort'` / `'table'`）三种算法的取舍，以及 `assume_unique` 的优化作用。
- 读懂 `intersect1d` / `union1d` / `setdiff1d` / `setxor1d` 四个二元集合运算如何由 `unique` 与 `_isin` 组合而成，以及它们对「输入是否已排序/去重」的假设。

---

## 2. 前置知识

本讲默认你已经读过 [u1-l2 模块组织与导入机制](u1-l2-module-organization.md)，知道以下两点：

1. **dispatcher + impl 双函数写法**：`numpy.lib` 中几乎每个公开函数都以 `@array_function_dispatch(_xxx_dispatcher)` 装饰，dispatcher 只返回「参与运算的数组参数」（如 `(ar,)`），背后接的是 NEP-18 的 `__array_function__` 协议。本讲涉及的 11 个公开函数全部遵循这一写法。

2. **再导出分层**：本文件 `numpy/lib/_arraysetops_impl.py` 是藏实现的私有模块（文件名带下划线前缀）。集合运算函数**没有**独立的薄再导出模块，而是由顶层 `numpy/__init__.py` 直接取名暴露，最终挂在 `np.` 命名空间。

此外需要一点关于「集合」的常识：数学上的集合元素**无序、不重复**；而 numpy 的集合运算出于实现原因，结果几乎都是**排序后**的一维数组。这个「排序」并非偶然，而是算法本身（基于排序）的副产物——这是理解整篇讲义的关键直觉。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `numpy/lib/_arraysetops_impl.py` | **唯一的核心源码**。全部 11 个公开函数 + 内部辅助函数都在此文件，约 1160 行。开头注释点明主题：`Set operations for arrays based on sorting.`（基于排序的数组集合运算）。 |
| `numpy/__init__.py` | 顶层导入：把 `_arraysetops_impl` 的公开函数搬到 `np.` 命名空间。 |
| `numpy/lib/tests/test_arraysetops.py` | 测试文件，与实现一一对应，可用于验证行为。 |

本文件顶部声明了公开 API 名单：

[_arraysetops_impl.py:L29-L33](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L29-L33) —— 这是 `__all__`，定义了模块对外的 10 个名字（`ediff1d` 属于差分函数，本讲不展开）。

---

## 4. 核心概念与源码讲解

本讲把 11 个最小模块组织成四个递进的小节：

1. **去重内核**：`unique` + `_unique1d`（最核心，其余都依赖它）。
2. **Array API 形式**：`unique_all` / `unique_counts` / `unique_inverse` / `unique_values`。
3. **成员资格**：`isin` + `_isin`（独立的一元判定，三套算法）。
4. **二元集合运算**：`intersect1d` / `union1d` / `setdiff1d` / `setxor1d`（全部组合 1 和 3）。

---

### 4.1 去重内核：`unique` 与 `_unique1d`

#### 4.1.1 概念说明

「去重」（deduplication）是所有集合运算的基础：给定一个数组，返回其中**不重复的元素**。朴素做法是「对每个元素，检查它是否已经出现过」，复杂度 \(O(n^2)\)。numpy 用两种更快的办法：

- **哈希快路**：用一张哈希表记录见过的值，一次遍历即可去重，期望 \(O(n)\)。要求元素类型可哈希、且不需要附带位置信息。
- **排序慢路**：先排序，则相等元素必相邻，只要比较相邻元素是否相等就能标出每个「新值」的首次出现位置，\(O(n \log n)\)。排序还能顺便给出「每个值在原数组的位置」，因此需要 `return_index` / `return_inverse` 时只能走这条路。

`unique` 是公开入口，`_unique1d` 是忽略形状的真正内核（名字里的 `1d` 表示它只处理扁平化后的一维数组）。`unique` 负责「形状规整」（处理 `axis`），`_unique1d` 负责「去重计算」。

#### 4.1.2 核心流程

`unique` 的分发逻辑：

```
unique(ar, ..., axis=None)
├─ 若 axis is None 或 ar 本身 1D
│    └─ 直接调用 _unique1d（扁平化处理）
└─ 否则（指定了 axis）
     ├─ moveaxis(axis → 0)：把目标轴挪到最前
     ├─ reshape 成 2D (n, m)，再 view 成「m 个字段的结构化 dtype」
     │   —— 把「一行」当成「一个元素」，从而能用一维去重逻辑
     ├─ _unique1d（在结构化 dtype 上去重）
     └─ reshape_uniq：把结果还原成原来的子数组形状
```

`_unique1d` 的去重计算：

```
_unique1d(ar, return_index, return_inverse, return_counts, equal_nan)
├─ 若「四不」：不需要 index/inverse/counts，且不是 masked 数组
│    └─ 试哈希快路 _unique_hash（C 实现）
│         ├─ 成功 → sorted 则 sort，直接返回
│         └─ 返回 NotImplemented → 落到排序慢路
└─ 排序慢路
     ├─ 需要位置信息 → argsort 得 perm，aux = ar[perm]
     │  否则 → 就地 sort，aux = ar
     ├─ 构造布尔掩码 mask：mask[0]=True，其余 mask[i] = (aux[i] != aux[i-1])
     │   （NaN 特判：equal_nan 时把末尾所有 NaN 视为一个）
     ├─ ret = (aux[mask],)                      # 去重后的值
     ├─ return_index  → ret += (perm[mask],)    # 首次出现的下标
     ├─ return_inverse→ imask=cumsum(mask)-1; inv[perm]=imask  # 重建下标
     └─ return_counts → diff(下标边界)           # 每个值的计数
```

`return_inverse` 的数学含义最值得记住：设去重后值为 \(u\)（长度 \(k\)），逆下标为 \(inv\)（长度 \(n\)，与输入同形），则恒有

\[
u[inv] = \text{原数组}
\]

即逆下标是把原数组「编码」成对去重表的引用，用它可以无损还原输入。

#### 4.1.3 源码精读

**公开入口 `unique` 与 dispatcher**：

[_arraysetops_impl.py:L139-L148](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L139-L148) —— `_unique_dispatcher` 只返回 `(ar,)`；`unique` 的签名注意 `sorted=True` 是关键字参数（NumPy 2.3 新增），`equal_nan=True` 默认把多个 NaN 合并成一个。

**1D / 无 axis 分支**：

[_arraysetops_impl.py:L292-L299](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L292-L299) —— `axis is None` 或 `ndim==1` 时直接交给 `_unique1d`，并把 `inverse_shape=ar.shape` 透传，让 `return_inverse` 的结果能还原成输入形状（NumPy 2.0 改动）。

**带 axis 的「结构化视图」技巧**：

[_arraysetops_impl.py:L310-L329](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L310-L329) —— 这是 `axis` 参数的实现精髓：把 `axis` 轴上的每个子数组（如每一行）**整体**当成一个元素。做法是 reshape 成 `(n, m)` 后，用 `ar.view(dtype)` 把每行 `m` 个标量重新解释为一个「有 `m` 个字段的结构化记录」。这样比较两条记录是否相等，就等价于比较两行是否完全相同，于是二维去重被规约成一维去重。

**内核 `_unique1d` 的哈希快路**：

[_arraysetops_impl.py:L366-L378](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L366-L378) —— 关键条件：`not optional_indices and not return_counts and not np.ma.is_masked(ar)`。也就是说，**只要你请求了 `return_index` / `return_inverse` / `return_counts` 中的任何一个，就强制走排序慢路**（因为哈希表不记录位置）。`_unique_hash` 是 C 实现（从 `_multiarray_umath` 导入），对不支持哈希的 dtype 返回 `NotImplemented` 从而优雅回退。

**排序慢路 + 掩码构造**：

[_arraysetops_impl.py:L381-L401](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L381-L401) —— 三处要点：(1) `return_index` 时用 `mergesort`（稳定排序，保证取到「首次出现」），否则用 `quicksort`（更快）；(2) 掩码核心就一句 `mask[1:] = aux[1:] != aux[:-1]`——排序后相邻相等者标 False，只剩每组第一个为 True；(3) NaN 特判块（L389-L399）：若末尾有 NaN 且 `equal_nan`，用 `searchsorted` 找到首个 NaN 位置，把整段 NaN 压成一个。

**三个可选返回值的装配**：

[_arraysetops_impl.py:L403-L414](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L403-L414) —— 最巧妙的是 `return_inverse`：`imask = np.cumsum(mask) - 1` 利用「掩码前缀和」给每个排序后的位置打上「它属于第几个唯一值」的编号，再用 `inv_idx[perm] = imask` 按 `perm` 散播回原始顺序。`return_counts` 则用 `np.diff` 算「相邻 True 位置之间的间距」得到每组计数。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `return_index` / `return_inverse` / `return_counts` 的语义，并观察「请求任一可选返回值会强制走排序路」。

**操作步骤**：

```python
import numpy as np

a = np.array([1, 2, 6, 4, 2, 3, 2])

# 1) 仅去重（走哈希快路，不保证排序语义）
print(np.unique(a))                       # [1 2 3 4 6]

# 2) 三个可选返回值
u, idx, inv, cnt = np.unique(
    a, return_index=True, return_inverse=True, return_counts=True)
print("values :", u)      # [1 2 3 4 6]
print("indices:", idx)    # [0 1 5 3 2]  每个 unique 值在原数组的首次出现下标
print("inverse:", inv)    # [0 1 4 3 1 2 1]
print("counts :", cnt)    # [1 3 1 1 1]

# 3) 验证恒等式 u[inv] == a
print("重建是否等于原数组:", np.array_equal(u[inv], a))   # True
```

**需要观察的现象**：

- `a[idx]` 应等于 `u`（`idx` 是首次出现的下标）。
- `inv` 的长度与 `a` 相同，`u[inv]` 能无损还原 `a`。
- `cnt.sum()` 应等于 `a.size`，且 `np.repeat(u, cnt)` 给出去重值的重复展开（顺序不等于原数组）。

**预期结果**：重建恒等式输出 `True`；`idx` 因 `mergesort` 稳定排序，保证指向「首次出现」。若想确认「哈希快路存在」，可在源码 L366-L378 处临时加一行 `print("hash path")` 再跑步骤 1（仅去重），与步骤 2（带返回值）对比打印次数。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `np.unique(a, return_index=True)` 用的是 `mergesort`，而不带 `return_index` 时用 `quicksort`？

> **答案**：`return_index` 要求 `idx` 指向「每个唯一值的**首次**出现」。只有稳定排序（`mergesort`）才能在遇到重复元素时保留先出现者的下标；`quicksort` 不稳定，可能把后出现者排到前面，导致 `idx` 指向错误位置。不带 `return_index` 时不需要追踪位置，用更快的 `quicksort` 即可。

**练习 2**：给定 `b = np.array([[1,0,0],[1,0,0],[2,3,4]])`，如何用 `axis=0` 取出「不重复的行」？底层是怎么做到的？

> **答案**：`np.unique(b, axis=0)` 返回 `[[1,0,0],[2,3,4]]`。底层先把 `axis=0` moveaxis 到最前、reshape 成 `(3,3)`，再 `view` 成「3 个字段的结构化 dtype」，于是每一行被当成一个整体元素参与一维去重，相等的两行 `[1,0,0]` 被合并。

**练习 3**：`_unique1d` 的哈希快路在什么条件下会被跳过？至少举出两种。

> **答案**：(1) 调用者请求了 `return_index` / `return_inverse` / `return_counts` 中的任何一个（`optional_indices` 或 `return_counts` 为真）；(2) 输入是 masked 数组（`np.ma.is_masked(ar)` 为真）；(3) `_unique_hash` 对该 dtype 返回 `NotImplemented`（不支持哈希的类型）。

---

### 4.2 Array API 形式：`unique_all` / `unique_counts` / `unique_inverse` / `unique_values`

#### 4.2.1 概念说明

[Array API 标准](https://data-apis.org/array-api/) 是一个跨数组库（numpy / CuPy / PyTorch 等）的统一接口规范。为了兼容它，numpy 提供了四个「Array API 形式」的 unique 函数。它们与经典 `unique` 有两点关键差异：

1. **返回命名元组而非裸元组**：经典 `unique` 用 `return_*` 开关，返回长度可变的元组，调用者要靠位置解包；Array API 形式固定返回一个 `NamedTuple`，用 `.values` / `.indices` 等字段名访问，更安全、更可读。
2. **`equal_nan=False`**：经典 `unique` 默认 `equal_nan=True`（多个 NaN 合并为一个）；Array API 形式一律 `equal_nan=False`（每个 NaN 视为不同元素），符合 Array API 规范的语义。

#### 4.2.2 核心流程

四个函数都只是 `unique` 的薄封装：

```
unique_all(x)     = unique(x, return_index=True, return_inverse=True,
                           return_counts=True, equal_nan=False)
                   → UniqueAllResult(values, indices, inverse_indices, counts)

unique_counts(x)  = unique(x, return_counts=True, equal_nan=False)
                   → UniqueCountsResult(values, counts)

unique_inverse(x) = unique(x, return_inverse=True, equal_nan=False)
                   → UniqueInverseResult(values, inverse_indices)

unique_values(x)  = unique(x, equal_nan=False, sorted=False)
                   → ndarray  （注意 sorted=False，走哈希快路，结果不保证排序）
```

注意 `unique_values` 是唯一一个传 `sorted=False` 的，因此它**不保证返回排序结果**（NumPy 2.3 起），这是它与 `unique(x)` 最实质的区别。

#### 4.2.3 源码精读

**三个结果类型 NamedTuple**：

[_arraysetops_impl.py:L419-L433](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L419-L433) —— 用 `typing.NamedTuple` 定义，字段名严格遵循 Array API 规范（注意是 `inverse_indices` 而非 `inverse`，`indices` 而非 `index`）。

**`unique_all` 的封装**：

[_arraysetops_impl.py:L440-L497](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L440-L497) —— 真正的逻辑只有 L490-L497：调用 `unique` 打开全部三个开关、`equal_nan=False`，然后用 `UniqueAllResult(*result)` 把裸元组包装成命名元组。

**`unique_values` 的 `sorted=False`**：

[_arraysetops_impl.py:L617-L658](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L617-L658) —— 关键在 L651-L658 传入 `sorted=False`。由于不带任何 `return_*`，这会命中 `_unique1d` 的哈希快路（4.1 节），结果顺序由哈希表决定，文档明确写 `array([1, 2])  # may vary`。

#### 4.2.4 代码实践

**实践目标**：对比 Array API 形式与经典 `unique` 在 NaN 处理上的差异。

**操作步骤**：

```python
import numpy as np

x = [1.0, 1.0, np.nan, np.nan, 2.0]

# 经典 unique：equal_nan=True，两个 NaN 合并
print(np.unique(x))                       # [ 1.  2. nan]

# Array API：equal_nan=False，NaN 不合并
r = np.unique_counts(x)
print(r.values)                           # [ 1.  2. nan nan]
print(r.counts)                           # [2 1 1 1]

# unique_values 不保证排序（may vary）
print(np.unique_values(x))
```

**需要观察的现象**：经典 `unique` 的 NaN 计数为 1（合并），`unique_counts` 的两个 NaN 各计数 1（不合并）。字段名访问 `r.values` / `r.counts` 比位置解包更清晰。

**预期结果**：如上注释。`unique_values` 的输出顺序「可能变化」，这正是 `sorted=False` 的语义。

#### 4.2.5 小练习与答案

**练习 1**：`unique_all` 返回的命名元组有哪四个字段？它们与经典 `unique` 的哪个 `return_*` 开关对应？

> **答案**：四个字段是 `values` / `indices` / `inverse_indices` / `counts`，分别对应经典 `unique` 的（无开关，主返回值）/ `return_index` / `return_inverse` / `return_counts`。

**练习 2**：为什么 `unique_values` 不保证返回排序结果，而 `unique_counts` 却「目前总是排序的」？

> **答案**：`unique_values` 显式传 `sorted=False`，命中哈希快路，顺序由哈希表决定；`unique_counts` 请求了 `return_counts`，命中排序慢路，排序是算法副产物。源码注释也提醒：`unique_counts`「currently always returns a sorted result, however, this could change」，即排序并非契约保证。

---

### 4.3 成员资格测试：`isin` 与 `_isin`

#### 4.3.1 概念说明

`isin(element, test_elements)` 回答一个问题：`element` 里的每个值，是否出现在 `test_elements` 中？它返回一个与 `element` **同形**的布尔数组，相当于向量化的 `in` 运算符。

与 `unique` 不同，`isin` 是**一元判定**（输出布尔），不是去重。但因为「判断一个值是否属于某集合」本质上也是集合运算，所以放在同一文件。

`isin` 的特别之处在于它有**三套算法**，通过 `kind` 参数选择：

| `kind` | 算法 | 适用场景 | 复杂度特征 |
|--------|------|----------|-----------|
| `'table'` | 查表法（类似计数排序） | 仅整数/布尔数组，且值域跨度不大 | \(O(n + r)\)，\(r\) 为值域宽度 |
| `'sort'` | 排序法（合并两个数组再排序） | 任意可比较类型 | \(O((n+m)\log(n+m))\) |
| `None`（默认） | 自动选择 | 根据内存预算自动选 table 或 sort | —— |

此外还有一条隐藏的**小数组快路**：当 `test_elements` 很小（`len(ar2) < 10 * len(ar1)**0.145`）时，直接逐元素比较，避免排序开销。

#### 4.3.2 核心流程

```
isin(element, test_elements, assume_unique=False, invert=False, kind=None)
└─ _isin(ar1, ar2, ...)  （ar1=element.ravel(), ar2=test_elements.ravel()）
   ├─ 若 ar1/ar2 都是整数或布尔，且 kind∈{None,'table'}
   │    └─ table 法：建一张 (ar2_max-ar2_min+1) 的布尔查找表，
   │         受「内存 ≤ 6×(n+m)」约束；不满足且 kind='table' 则报错
   ├─ 否则若 ar2 很小 或 含 object
   │    └─ 小数组法：mask|=（ar1==a）逐个累加
   └─ 否则 sort 法：concatenate→mergesort→比较相邻→按 order 散播回原位
```

`assume_unique=True` 的优化作用：跳过对输入的 `unique` 预处理，省一次去重。但若输入实际有重复，`sort` 法的结果下标映射可能出错（文档明确警告）。

#### 4.3.3 源码精读

**table 法的内存约束与查找表**：

[_arraysetops_impl.py:L819-L871](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L819-L871) —— 关键判定 L843：`below_memory_constraint = ar2_range <= 6 * (ar1.size + ar2.size)`，即查找表大小不能超过两个原数组合计内存的 6 倍。L867-L871 建表：`isin_helper_ar = np.zeros(ar2_range+1, dtype=bool); isin_helper_ar[ar2-ar2_min] = 1`，把 `ar2` 中存在的值在表里置 1，查询时用 `isin_helper_ar[ar1-ar2_min]` 一步完成。

**小数组快路**：

[_arraysetops_impl.py:L917-L926](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L917-L926) —— 条件 `len(ar2) < 10 * len(ar1) ** 0.145 or contains_object`。注意指数 `0.145` 是经验阈值：当 `ar2` 相对 `ar1` 足够小时，逐元素 `mask |= (ar1 == a)` 比排序更快。`contains_object` 时也走这条路，因为 object 数组排序不可靠。

**sort 法的核心**：

[_arraysetops_impl.py:L929-L950](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L929-L950) —— 思路：把 `ar1`（先去重）和 `ar2` 拼起来排序，若 `ar1` 的某个值与 `ar2` 的某值相邻（相等），则它「在集合中」。L937 强制 `mergesort` 是为了让 `ar1` 的值排在 `ar2` 的同值前面，保证 `sar[1:]==sar[:-1]` 能正确识别跨数组的相等对。最后 L945 `ret[order] = flag` 把排序后的布尔结果按 `order` 散播回原顺序。

**公开入口 `isin` 的形状还原**：

[_arraysetops_impl.py:L958-L960](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L958-L960) 与 [_arraysetops_impl.py:L1074-L1076](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L1074-L1076) —— `_isin` 内部把 `element` ravel 成 1D，公开 `isin` 在 L1074 先记下原始形状，最后 `.reshape(element.shape)` 还原，保证输出与输入同形（支持广播语义）。

#### 4.3.4 代码实践

**实践目标**：观察 `kind` 参数对同一问题的不同算法选择，并验证 `invert` 的等价性。

**操作步骤**：

```python
import numpy as np

element = 2 * np.arange(6).reshape((2, 3))   # shape (2,3)
test_elements = [1, 2, 4, 8]

for kind in [None, 'sort', 'table']:
    m = np.isin(element, test_elements, kind=kind)
    print(f"kind={kind!r:7} ->\n{m}")

# invert 等价性
m1 = np.isin(element, test_elements, invert=True)
m2 = ~np.isin(element, test_elements)
print("invert 等价:", np.array_equal(m1, m2))    # True
```

**需要观察的现象**：三种 `kind` 的**结果完全相同**（`kind` 只影响速度和内存，不影响正确性）；输出 shape 与 `element` 一致 `(2,3)`；`invert=True` 与 `~isin(...)` 结果相同但更快。

**预期结果**：三种 kind 输出一致。由于 `element` 含小整数，`kind=None` 时会自动选 `table` 法（值域 0~8，远小于 6×10 的内存预算）。

#### 4.3.5 小练习与答案

**练习 1**：`kind='table'` 为什么只支持整数和布尔数组？

> **答案**：table 法用「值」直接作为查找表的下标（`isin_helper_ar[ar1 - ar2_min]`），这要求值是离散的整数。浮点数无法做下标，字符串更不行；布尔数组会被转成 `uint8`（L831-L834）后按整数处理。

**练习 2**：默认 `kind=None` 是依据什么在 table 和 sort 之间选择的？

> **答案**：依据内存预算。table 法需要一张大小为 `ar2_range+1`（`ar2_max-ar2_min+1`）的查找表，仅当 `ar2_range <= 6*(ar1.size+ar2.size)`（即表大小不超过两数组总和的 6 倍）时才用 table，否则用 sort。这样避免在值域跨度极大（如 `[0, 10**9]`）时分配巨型查找表。详见 L843 与 L855-L858。

**练习 3**：把一个 Python `set` 直接传给 `test_elements` 会发生什么？为什么？

> **答案**：得不到预期结果。`np.asarray({1,2,4})` 会把整个 set 当成**一个 object 元素**（长度 1），而非拆成三个值，因此 `isin` 几乎全 False。文档建议先 `list(test_set)` 转成列表再传入。这是因为 `np.asarray` 对非序列集合的处理方式决定的（见 L1016-L1021 注释）。

---

### 4.4 二元集合运算：`intersect1d` / `union1d` / `setdiff1d` / `setxor1d`

#### 4.4.1 概念说明

这四个函数实现两个数组之间的集合二元运算，对应数学上的：

| 函数 | 数学含义 | 口诀 |
|------|----------|------|
| `intersect1d(ar1, ar2)` | \(A \cap B\) 交集 | 两者都有 |
| `union1d(ar1, ar2)` | \(A \cup B\) 并集 | 任一就有 |
| `setdiff1d(ar1, ar2)` | \(A \setminus B\) 差集 | 在 A 但不在 B |
| `setxor1d(ar1, ar2)` | \(A \triangle B\) 对称差 | 恰在一个中 |

它们的实现高度一致：**先把两边去重（调 `unique`），再拼接排序，靠比较相邻元素来分类**。其中 `setdiff1d` 复用了 `_isin`。理解了 4.1 的 `unique` 和 4.3 的 `isin`，这四个函数几乎是「积木拼装」。

一个贯穿四者的参数是 `assume_unique`：若调用者**保证**输入已经去重，传 `True` 可跳过 `unique` 预处理以加速；但若实际有重复，结果可能错误（尤其涉及下标映射时）。

#### 4.4.2 核心流程

**交集 `intersect1d`**：拼接两个去重数组 → 排序 → 相邻相等者即为交集。

```
若 not assume_unique: ar1=unique(ar1), ar2=unique(ar2)
aux = concatenate(ar1, ar2); aux.sort()
mask = aux[1:] == aux[:-1]      # 相邻相等说明该值在两边都出现
int1d = aux[:-1][mask]
```

**并集 `union1d`**：拼接后整体去重，一行搞定：`unique(concatenate(ar1, ar2))`。

**差集 `setdiff1d`**：先去重 ar1，再用 `_isin(ar1, ar2, invert=True)` 取「不在 ar2」的掩码筛选。

**对称差 `setxor1d`**：拼接去重数组 → 排序 → 标记「只出现一次」（即前后都不相等）的元素。

#### 4.4.3 源码精读

**`intersect1d` 的核心**：

[_arraysetops_impl.py:L721-L744](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L721-L744) —— L725-L734 处理 `assume_unique`：否则先 `unique` 去重。L736-L744 是算法精髓：拼接后排序，则「在两边都出现的值」必然在排序后的数组中成对相邻，`aux[1:]==aux[:-1]` 找出这些相邻相等对，`aux[:-1][mask]` 即交集。

**`union1d` 一行实现**：

[_arraysetops_impl.py:L1113](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L1113) —— `return unique(np.concatenate((ar1, ar2), axis=None))`。`axis=None` 把输入扁平化，整个并集就是「拼接后去重」，排序由 `unique` 的排序路保证。

**`setdiff1d` 复用 `_isin`**：

[_arraysetops_impl.py:L1152-L1158](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L1152-L1158) —— 注意 L1158 调用的是内部 `_isin(ar1, ar2, assume_unique=True, invert=True)`，传 `assume_unique=True` 是因为前面已经手动 `unique` 过 ar1/ar2，避免 `_isin` 重复去重。差集 = 在 ar1 中但 `invert=True`（不在 ar2）的部分。

**`setxor1d` 的「只出现一次」掩码**：

[_arraysetops_impl.py:L793-L803](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraysetops_impl.py#L793-L803) —— 技巧在 L802-L803：`flag = concatenate([True], aux[1:]!=aux[:-1], [True])` 在首尾各补一个 `True` 作为边界哨兵，于是「某位置前后都为 True」等价于「该值只出现一次」（既不与前一个相等，也不与后一个相等），`aux[flag[1:] & flag[:-1]]` 取出这些「恰在一个数组中」的值。

#### 4.4.4 代码实践

**实践目标**：用四个二元运算覆盖集合论的全部基本关系，并验证 `assume_unique` 的加速效果。

**操作步骤**：

```python
import numpy as np

a = np.array([1, 2, 3, 2, 4])     # 含重复 2
b = np.array([2, 3, 5, 7, 5])     # 含重复 5

print("交集:", np.intersect1d(a, b))   # [2 3]
print("并集:", np.union1d(a, b))       # [1 2 3 4 5 7]
print("差集:", np.setdiff1d(a, b))     # [1 4]
print("对称差:", np.setxor1d(a, b))    # [1 4 5 7]

# 验证集合恒等式：A△B = (A∪B) \ (A∩B)
sym = np.setxor1d(a, b)
identity = np.setdiff1d(np.union1d(a, b), np.intersect1d(a, b))
print("对称差恒等式:", np.array_equal(sym, identity))   # True
```

**需要观察的现象**：四个结果都是**排序且去重**的，即使输入 `a`/`b` 含重复元素；对称差恒等式成立，说明四个运算在数学上一致。

**预期结果**：如注释所示。注意 `setdiff1d` 是「在 a 不在 b」，方向敏感（`setdiff1d(b,a)` 结果不同）。

#### 4.4.5 小练习与答案

**练习 1**：`union1d` 的实现只有一行 `unique(concatenate(...))`，它为什么不需要 `assume_unique` 参数？

> **答案**：并集的语义本就要求结果去重，无论输入是否唯一，都要过一次 `unique`，所以没有跳过去重的优化空间，自然不提供 `assume_unique`。而 `intersect1d` / `setdiff1d` / `setxor1d` 的实现依赖「输入已去重」来简化算法（如 `intersect1d` 靠相邻相等判定），所以提供该参数让调用者声明免去预处理。

**练习 2**：`setdiff1d` 内部调用 `_isin` 时为什么传 `assume_unique=True`？

> **答案**：因为 `setdiff1d` 在 L1155-L1157 已经对 `ar1`、`ar2` 做过 `unique`（除非用户显式 `assume_unique`），传给 `_isin` 的两个数组此时必然已去重。传 `assume_unique=True` 告诉 `_isin` 不必再做一次 `unique`，省去重复劳动。

**练习 3**：`setxor1d` 用「首尾各补一个 True 哨兵」的掩码技巧来找出只出现一次的元素。请手推 `aux=[2,2,3,5,7]`（已排序）时 `flag` 和最终结果。

> **答案**：`aux[1:]!=aux[:-1]` = `[False, True, True, True]`，首尾补 True 得 `flag=[True, False, True, True, True, True]`（长度比 aux 多 1）。`flag[1:] & flag[:-1]` 逐位与：`[False&True, True&False, True&True, True&True, True&True]` = `[False, False, True, True, True]`，对应 `aux[[2,3,4]]` = `[3,5,7]`。即两个 2 被排除（出现两次），3/5/7 各保留一次。

---

## 5. 综合实践

**任务**：模拟一个「学生选课」场景，综合运用交集、并集、差集与 `isin` 成员资格判定。

**背景**：有两份选课名单（学号数组），需要回答四个问题。

```python
import numpy as np

# 课程 A 的选课学号（含重复录入）
course_a = np.array([101, 103, 105, 101, 107, 109, 103])
# 课程 B 的选课学号
course_b = np.array([103, 105, 108, 109, 110, 105])

# 任务 1：两门课都选了的学生（交集）
both = np.intersect1d(course_a, course_b)
print("两门都选:", both)                      # [103 105 109]

# 任务 2：至少选了一门的学生（并集）
either = np.union1d(course_a, course_b)
print("至少选一门:", either)                   # [101 103 105 107 108 109 110]

# 任务 3：只选了 A 没选 B 的学生（差集）
only_a = np.setdiff1d(course_a, course_b)
print("只选 A:", only_a)                       # [101 107]

# 任务 4：教务处给出一组学号，判断哪些人选了 A（成员资格掩码）
roster = np.array([101, 102, 107, 200, 109])
mask = np.isin(roster, course_a)
print("在 A 中的学号:", roster[mask])          # [101 107 109]
print("掩码:", mask)                           # [ True False  True False  True]

# 进阶：用 unique 的 return_counts 统计 A 的「重复录入」次数
u, cnt = np.unique(course_a, return_counts=True)
print("重复录入的学生:", u[cnt > 1], "次数:", cnt[cnt > 1])  # [101 103] [2 2]
```

**验证要点**：

1. 四个集合运算的结果都自动去重且排序，即便输入有重复录入（101、103、105 各出现两次）。
2. `isin` 返回的掩码与 `roster` 同形，`roster[mask]` 直接筛出选了 A 的学号——这正是「成员资格布尔掩码」的典型用法（本讲代码实践任务的核心）。
3. `unique(..., return_counts=True)` 能顺便发现「重复录入」的数据质量问题，体现 `counts` 的实用价值。

> **待本地验证**：以上输出基于源码逻辑推断，请在本地安装 numpy 后运行确认。注意 `isin` 默认 `kind=None` 会自动选算法，对小整数输入应选 table 法。

---

## 6. 本讲小结

- **两套去重引擎**：`_unique1d` 优先走 C 实现的 `_unique_hash` 哈希快路（仅当不请求任何 `return_*` 且非 masked 数组），否则走排序慢路。请求 `return_index` / `return_inverse` / `return_counts` 任一即强制排序。
- **排序是算法副产物**：掩码 `aux[1:] != aux[:-1]` 依赖「相等元素排序后相邻」，因此 numpy 集合运算结果天然有序。`return_inverse` 靠 `cumsum(mask)-1` + `perm` 散播实现无损重建。
- **axis 的结构化视图技巧**：把目标轴的子数组 `view` 成结构化 dtype，把「多维去重」规约为「一维去重」。
- **Array API 形式**：`unique_all/counts/inverse/values` 是 `unique` 的薄封装，返回 NamedTuple，且固定 `equal_nan=False`；`unique_values` 额外传 `sorted=False` 走哈希快路、不保证排序。
- **isin 三套算法**：`table`（整数/布尔、值域受限，\(O(n+r)\)）、`sort`（通用、\(O((n+m)\log(n+m))\)）、小数组逐元素快路；默认 `None` 按 6 倍内存预算自动选。
- **四个二元运算都是积木**：`union1d` = `unique(concatenate)`；`intersect1d` / `setxor1d` = 拼接排序后比较相邻；`setdiff1d` = `unique` + `_isin(invert=True)`。`assume_unique` 让调用者声明免去预处理。

---

## 7. 下一步学习建议

- **深入哈希快路的 C 实现**：`_unique_hash` 来自 `numpy._core._multiarray_umath`，想了解哪些 dtype 支持哈希去重，可阅读 `numpy/_core/src/multiarray/` 下的相关 C 源码，并参考测试 `test_unique_byte_string_hash_based` / `test_unique_unicode_string_hash_based`（`tests/test_arraysetops.py`）。
- **NaN 处理的全局视角**：本讲看到 `unique` 的 `equal_nan` 与 NaN 特判（L389-L399），可与 [u9 NaN 感知函数](u9-l1-nan-infra-aggregation.md) 对照，理解 numpy 对 NaN 的统一处理哲学。
- **`ediff1d` 与差分家族**：同文件的 `ediff1d`（L40-L128）在概念上属于 [u6-l1 数值微分](u6-l1-diff-gradient-trapezoid.md) 的 `diff` 家族，可作为延伸阅读，对比二者对「扁平化」与「边界追加」的不同处理。
- **测试驱动学习**：`numpy/lib/tests/test_arraysetops.py` 中 `test_isin`（L218）、`test_unique_1d`（L700）等用 `@pytest.mark.parametrize` 覆盖了 `kind`、dtype、边界情形，阅读这些测试能快速建立对函数行为的精确认知。

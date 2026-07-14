# 直方图与自动分箱估计器

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `np.histogram`、`np.histogram2d`、`np.histogramdd` 三个直方图函数之间的层次关系，以及它们各自接受什么样的「分箱（bins）」参数。
- 读懂私有内核 `_get_bin_edges` 如何把三种异质的 `bins` 输入（字符串估计器、整数、边数组）统一成同一份「边数组 + 是否等距」的中间结果。
- 理解 `density=True` 时直方图为什么不是「让和等于 1」，而是「让面积积分等于 1」，并能对应到源码里那一行归一化公式。
- 掌握 `_hist_bin_sturges` / `_hist_bin_scott` / `_hist_bin_fd` / `_hist_bin_auto` 等自动分箱估计器各自的统计直觉与公式差异，并知道 `_hist_bin_auto` 是如何把 FD 与 Sturges 融合成「开箱即用」默认值的。

## 2. 前置知识

在进入源码前，先用大白话约定几个词：

- **直方图（histogram）**：把一串数按取值范围切成若干个「桶」（bin），数一数每个桶里落了几个数。结果是一个「每个桶的计数」数组 + 一组「桶边界」。
- **桶 / 分箱（bin）**：一个左闭右开的区间 `[edge_i, edge_{i+1})`。直方图有一个特殊约定：**最后一个桶是双闭的**，即 `[edge_{n-1}, edge_n]`，右端点也算进去。
- **桶宽（bin width）**：相邻两条边的差。等宽分箱时所有桶宽相同；不等宽分箱时桶宽可变。
- **密度（density）**：把「计数」换算成「概率密度」。注意密度直方图的高度之**和**一般不是 1，是「高度 × 桶宽」的总和（即面积）等于 1。
- **IQR（四分位距，interquartile range）**：第 75 百分位减第 25 百分位，衡量数据散布的稳健指标，对离群点不敏感。
- **ptp（peak-to-peak）**：数据的最大值减最小值，即数据跨度。源码里写成 `_ptp`。
- **dispatcher + impl 双函数写法**：本仓库的公开函数都用 `@array_function_dispatch(...)` 装饰，dispatcher 只负责把参与运算的数组参数交出来供 NEP-18 的 `__array_function__` 协议拦截，真正的实现写在被装饰的同名函数体里。这一点在前置讲义 u1-l2 中已建立。

本仓库里直方图相关代码几乎全部集中在私有文件 `numpy/lib/_histograms_impl.py`，唯一的例外是二维直方图 `histogram2d`，它住在 `numpy/lib/_twodim_base_impl.py` 里（因为二维几何上更贴近矩阵视角），但内部只是转手调用 `histogramdd`。这两个文件就是本讲的全部源码地图。

## 3. 本讲源码地图

| 文件 | 关键内容 | 本讲涉及行 |
| --- | --- | --- |
| `numpy/lib/_histograms_impl.py` | 直方图主战场：8 个自动分箱估计器、边解析内核 `_get_bin_edges`、三个公开函数 `histogram` / `histogramdd` / `histogram_bin_edges` | 全文 1–1085 行 |
| `numpy/lib/_twodim_base_impl.py` | 二维直方图 `histogram2d`，是 `histogramdd` 的薄封装 | `_histogram2d_dispatcher` 与 `histogram2d` 两段 |
| `numpy/lib/tests/test_histograms.py` | 直方图测试套件，含 `TestHistogramOptimBinNums`（各估计器箱数基准值表） | 432 行起 |

贯穿全讲的设计线索有三条，请先记住它们，看代码时会处处对上：

1. **「边解析」与「计数」解耦**：`_get_bin_edges` 只负责把任意 `bins` 输入算成一组边；`histogram` 的函数体只负责把数塞进桶里数数。两者用 `(bin_edges, uniform_bins)` 这个元组通信。
2. **「等距」走快路、「不等距」走慢路**：当桶等宽时，`histogram` 用一条把数值映射成桶下标的公式 + `bincount` 直接统计（快路）；否则用「排序 + searchsorted 的累积直方图」兜底（慢路）。
3. **「只取边」与「连计数一起」共用同一套边解析**：`histogram_bin_edges` 就是 `histogram` 的「半个身子」——只跑 `_get_bin_edges`，不跑计数部分。

## 4. 核心概念与源码讲解

### 4.1 一维直方图主函数 `histogram`：快慢双路径与 density 归一化

#### 4.1.1 概念说明

`np.histogram` 是直方图家族里最常用的入口。它的签名是：

```
histogram(a, bins=10, range=None, density=None, weights=None)
```

`bins` 有三种形态：

- 整数 `n`：把数据范围等分成 `n` 个等宽桶。
- 边数组 `[e0, e1, ..., em]`：直接指定每条边，桶宽可不等，必须单调递增。
- 字符串 `'auto' / 'fd' / 'scott' / ...`：让 numpy 根据数据自动估计一个最优桶宽，再反推桶数（见 4.5）。

`density` 是初学者最容易误解的参数。**`density=True` 不是让计数的和等于 1**，而是让「直方图的面积积分」等于 1。也就是说，把每个桶的计数先除以桶宽、再除以总计数，使得「高度 × 桶宽」的总和是 1。这对等宽分箱就退化成「除以一个常数」，但对不等宽分箱是必须的——否则高桶窄桶的「高度」不可比。

#### 4.1.2 核心流程

`histogram` 的整体执行可以拆成三大步：

```text
1. 规整输入：_ravel_and_check_weights 把 a 与 weights 拉平、校验形状、把布尔数组转 uint8
2. 解析分箱：_get_bin_edges(a, bins, range, weights) → (bin_edges, uniform_bins)
   - uniform_bins 非空  ⇒  桶等宽  ⇒  走「快路」
   - uniform_bins 为 None ⇒  桶不等宽 ⇒  走「慢路」
3. 统计计数：
   快路：把每个值换算成桶下标（一条公式），分块喂给 bincount
   慢路：对数据排序，用 searchsorted 算累积直方图，再 diff
4. 可选 density 归一化：n / diff(bin_edges) / n.sum()
```

快路之所以快，是因为「等宽」这个前提让我们不必对每个值做二分查找，而是直接用一个一次式把数值换算成桶下标。

#### 4.1.3 源码精读

函数入口先做输入规整与边解析，注意它只调用 `_get_bin_edges` 一次，就同时拿到了「边」和「是否等距」两个信息：

[histogram 的入口与边解析 — _histograms_impl.py:790-792](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L790-L792) 把 `a` 与 `weights` 拉平校验，再取 `(bin_edges, uniform_bins)`，其中 `uniform_bins` 为 `None` 即表示不等距、要走慢路。

紧接着根据是否有 `weights` 决定计数数组的 dtype（无权重用 `intp`，有权重沿用 weights 的 dtype），并设定一个分块大小 `BLOCK = 65536`：

[计数 dtype 与分块大小 — _histograms_impl.py:795-802](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L795-L802) 说明直方图在大数组上会按 65536 一块地分批处理，既提速又降内存。

**快路**的核心是这一段：先把每个落在范围内的值减去首边、除以总跨度、乘以桶数，得到浮点下标；再处理三类边界微调（恰好等于末边的退一格、因 ULP 误差落到边上的退一格、落在右边沿的进一格）；最后用 `bincount` 计数：

[快路：等宽桶的下标换算与 bincount — _histograms_impl.py:816-873](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L816-L873) 关键是一次式 `((_unsigned_subtract(tmp_a, first_edge) / norm_denom) * norm_numerator)` 把数值映射成桶下标；复数权重时分别对实部、虚部各做一次 `bincount` 再相加。

注释明确解释了 ULP 修正的来由——「索引计算不保证在桶边 ±1 ULP 内给出完全一致的结果」：

[边界 ULP 修正 — _histograms_impl.py:856-863](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L856-L863) 用 `tmp_a < bin_edges[indices]` 退一格、用右沿判断进一格，保证落在桶边附近的值不被错分。

**慢路**面向「用户给了一组任意边」的不等距情形。它对每个块排序，借助辅助函数 `_search_sorted_inclusive` 在边上做「左闭右开、末边右闭」的 searchsorted，从而得到一个累积直方图，最后 `np.diff` 还原成每个桶的计数：

[慢路：累积直方图法 — _histograms_impl.py:874-893](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L874-L893) 对带权重的情况，先按值排序，再用权重前缀和 `sw.cumsum()` 配合 searchsorted 下标取值，得到「到每条边为止的累积权重」，最后 `diff`。

辅助函数 `_search_sorted_inclusive` 只有四行，但它是「最后一个桶双闭」这一约定的实现所在——只有最后一条边用 `side='right'`，其余用 `side='left'`：

[_search_sorted_inclusive — _histograms_impl.py:457-466](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L457-L466) 把最后一条边单独用 `'right'` 查，等价于「末边算进最后一个桶」。

最后是 `density` 归一化，注意它除的是 `diff(bin_edges)`（桶宽）而不是常数，再除 `n.sum()`（总计数）。这就是「面积积分为 1」的实现：

[density 归一化 — _histograms_impl.py:895-897](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L895-L897) 公式 `n / db / n.sum()`：先除桶宽得到密度高度，再除总计数使其积分（高度对桶宽求和）为 1。

#### 4.1.4 代码实践

**实践目标**：直观看到 `density` 的「面积为 1」语义，以及快路（等距）与慢路（不等距）结果一致。

操作步骤：

1. 构造一个 0–9 的数组，分别用等距（`bins=5`）与不等距（自定义边）求直方图。
2. 对同一组数据用 `density=False` 与 `density=True`，验证「计数之和」与「面积之和」。

```python
import numpy as np

a = np.arange(10)                      # [0,1,...,9]
n1, e1 = np.histogram(a, bins=5)        # 等距，走快路
n2, e2 = np.histogram(a, bins=[0, 1, 4, 9])  # 不等距，走慢路
print("等距计数：", n1, "边：", e1)
print("不等距计数：", n2, "边：", e2)

# density 语义
h, edges = np.histogram(a, bins=5, density=True)
area = np.sum(h * np.diff(edges))      # 高度 × 桶宽 再求和
print("density 高度：", h)
print("高度之和：", h.sum(), " 面积之和：", area)
```

**需要观察的现象**：

- `n1` 是等宽 5 桶的计数；`n2` 是 `[0,1), [1,4), [4,9]` 三桶的计数。
- `h.sum()` 一般 ≠ 1（除非桶宽恰好为 1）；但 `area` 恒为 1.0（可能有 1e-16 级浮点误差）。

**预期结果**：`area` 打印出 `1.0`（或 `0.9999...`）。这是理解 `density` 的判据。

> 说明：上述代码基于本讲源码公开语义编写，未在本机实跑；如出现轻微浮点偏差属正常。

#### 4.1.5 小练习与答案

**练习 1**：若把 `a` 改成含 NaN 的数组，`np.histogram` 会怎么处理？

**参考答案**：`_get_outer_edges` 在自动探测 `a.min()/a.max()` 时，若结果非有限会直接抛 `ValueError`（见 [_get_outer_edges — _histograms_impl.py:316-318](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L316-L318)）。所以默认情况下含 NaN 的数据会报错；要避免，需显式传 `range=` 把 NaN 排除在范围外。

**练习 2**：为什么快路里要写 `indices[indices == n_equal_bins] -= 1`？

**参考答案**：恰好等于末边的值，经一次式换算会得到 `n_equal_bins`（越界下标）。但末边应归入最后一个桶（下标 `n_equal_bins-1`），所以必须把等于 `n_equal_bins` 的下标减 1，这正是「最后一个桶双闭」的体现。

---

### 4.2 边解析内核 `_get_bin_edges`：三种 bins 输入的统一解析

#### 4.2.1 概念说明

`histogram` 能同时接受「整数 / 边数组 / 字符串」三种异质输入，靠的就是 `_get_bin_edges` 这个内部内核。它把所有输入归一成同一种返回形态：

```
(bin_edges, uniform_bins)
```

- `bin_edges`：一组边数组，永远是最终要用的边。
- `uniform_bins`：要么是 `(first_edge, last_edge, n_equal_bins)` 三元组，表示「桶等距」、可走快路；要么是 `None`，表示「桶不等距」、要走慢路。

这个「两段返回」的设计是 4.1 里快慢路径分流的开关。

#### 4.2.2 核心流程

```text
若 bins 是字符串：
    1. 校验是否合法估计器名（_hist_bin_selectors 里查表）
    2. 有权重则报 TypeError（自动估计不支持带权）
    3. _get_outer_edges 取首尾边（range 优先，否则用 min/max）
    4. 若给了 range，先把范围外的数据剔掉再估计
    5. 调用对应估计器得到「最优桶宽 width」
    6. 桶数 = ceil(总跨度 / width)；整数数据且 width<1 时桶宽抬到 1
    ⇒ uniform_bins 非空（等距）

若 bins 是标量整数：
    1. operator.index 取整，校验 ≥1
    2. _get_outer_edges 取首尾边
    ⇒ uniform_bins 非空（等距）

若 bins 是一维数组：
    1. asarray 化
    2. 校验单调递增
    ⇒ bin_edges 直接用，uniform_bins = None（不等距）
```

对字符串与整数这两种「等距」分支，函数末尾还会统一用 `np.linspace(first_edge, last_edge, n_equal_bins + 1)` 生成边，并做一道「边数过多导致出现退化（相邻边相等）」的校验：

[用 linspace 生成等距边并校验退化 — _histograms_impl.py:436-452](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L436-L452) 选定一个固定 `bin_type` 避免 gh-10322 的类型提升不确定性，整数结果会提升为浮点，最后若相邻边 `>=` 则报「Too many bins」。

#### 4.2.3 源码精读

字符串分支是自动分箱的入口，它做完范围裁剪后调用 `_hist_bin_selectors[bin_name](a, (first_edge, last_edge))` 得到桶宽，再反推桶数：

[字符串分支：估计器→桶宽→桶数 — _histograms_impl.py:381-414](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L381-L414) 注意两个保护：空数组直接给 1 个桶；估计器返回 0（如 FD 遇到 IQR=0）时也落到 1 个桶；整数数据桶宽不得小于 1（保证至少一格一格地分）。

标量整数分支最简单，只是取边：

[标量整数分支 — _histograms_impl.py:416-425](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L416-L425) `operator.index` 兼容 numpy 整数标量，`<1` 报错。

数组分支只校验单调性，不做任何计算：

[数组分支 — _histograms_impl.py:427-434](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L427-L434) 只要 `bin_edges[:-1] > bin_edges[1:]` 有任何一处即报错。

`_get_outer_edges` 是「首尾边从哪来」的统一回答：优先用 `range`，否则用数据的 min/max；首尾相等时各扩 0.5 避免除零；并对非有限值（inf/nan）报错：

[_get_outer_edges — _histograms_impl.py:298-325](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L298-L325) 三段 `if range / elif 空 / else min,max`，外加 `first_edge == last_edge` 的扩边保护。

另一个值得留意的小工具是 `_unsigned_subtract`。它专门解决「有符号整数数组的 ptp 无法用自身 dtype 表示」的问题（例如 `int8` 的 `[0, 200]` 跨度 200 超出 int8 范围）。它把「最大减最小」转成无符号运算：

[_unsigned_subtract — _histograms_impl.py:328-353](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L328-L353) 用一张 `signed_to_unsigned` 字典把 `int8/16/32/64` 映射到对应无符号类型再减；非整数类型直接按 `result_type` 减。

#### 4.2.4 代码实践

**实践目标**：跟踪三种 `bins` 输入分别走了 `_get_bin_edges` 的哪条分支。

操作步骤：阅读源码后，按下表「脑补」对每种输入会命中的分支与最终 `uniform_bins` 是否为 `None`，再用 `np.histogram_bin_edges`（它内部就是 `_get_bin_edges`）打印实际边做对照：

```python
import numpy as np
a = np.array([0, 1, 2, 3, 4, 5])

print(np.histogram_bin_edges(a, bins=3))          # 标量整数分支 → 等距
print(np.histogram_bin_edges(a, bins=[0, 2, 5]))  # 数组分支 → 不等距，原样返回
print(np.histogram_bin_edges(a, bins='auto'))     # 字符串分支 → 自动估计
```

**需要观察的现象**：

- `bins=3` → 4 条等距边。
- `bins=[0,2,5]` → 原样 `[0, 2, 5]`，不插值。
- `bins='auto'` → 边数由估计器决定。

**预期结果**：三行输出分别给出长度 4、3、若干的边数组。`[0,2,5]` 这一组的边与输入完全一致是关键判据。

#### 4.2.5 小练习与答案

**练习 1**：为什么字符串分支里要 `if weights is not None: raise TypeError`？

**参考答案**：自动估计器的输入是「数据本身」（用来算 std/IQR/分位数等），加权重后这些统计量就不再有意义。源码在 [histogram_bin_edges 文档 — _histograms_impl.py:545-546](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L545-L546) 也注明「This is currently not used by any of the bin estimators」。所以直接禁止，避免给出误导性结果。

**练习 2**：`bins=1000` 但数据只有 3 个不同值时，`_get_bin_edges` 末尾的校验会怎样？

**参考答案**：`np.linspace` 生成 1001 条边时会出现大量相邻边相等，触发 `np.any(bin_edges[:-1] >= bin_edges[1:])` 为真，抛 `ValueError: Too many bins for data range`（见 [退化校验 — _histograms_impl.py:448-451](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L448-L451)）。

---

### 4.3 `histogram_bin_edges` 与 `histogram2d`：只取边 / 2D 薄封装

#### 4.3.1 概念说明

这两个函数一个是「只算边不算数」，一个是「二维的快捷方式」，但都建立在已有内核之上：

- **`histogram_bin_edges`**：直方图的「半个身子」。它只跑 `_get_bin_edges`，把边数组返回给你。典型用途是**多个直方图共用同一组边**，使它们可比。
- **`histogram2d`**：二维直方图。它的实现极短——本质是把 `bins` 参数规整一下，转手调用 `histogramdd`。它住在 `_twodim_base_impl.py` 而非 `_histograms_impl.py`，纯粹是出于「二维 ≈ 矩阵」的历史归类，与实现复杂度无关。

#### 4.3.2 核心流程

`histogram_bin_edges` 的函数体只有两行：

```text
a, weights = _ravel_and_check_weights(a, weights)
bin_edges, _ = _get_bin_edges(a, bins, range, weights)
return bin_edges
```

与 `histogram` 相比，它**只少了「计数」那一段**——`_get_bin_edges` 的第二个返回值 `uniform_bins` 被直接丢弃。

`histogram2d` 的核心则是把 `bins` 参数归一成「每维一份」的形式后调用 `histogramdd([x, y], bins, ...)`，再把返回的 `edges`（一个含 2 个数组的元组）拆成 `xedges, yedges`：

[histogram2d 实现 — _twodim_base_impl.py:848-862](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L848-L862) 先校验 `len(x)==len(y)`，再用 `len(bins)` 区分「单个值/一对值/一个数组」，最后 `histogramdd([x, y], ...)`。

#### 4.3.3 源码精读

`histogram_bin_edges` 完整体现了「与 `histogram` 共用边解析」的设计：

[histogram_bin_edges 函数体 — _histograms_impl.py:675-677](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L675-L677) 调一次 `_get_bin_edges` 后丢弃计数信息，仅返回 `bin_edges`。

它的文档里有一段示例恰好演示了「共用边」的用法：先 `shared_bins = np.histogram_bin_edges(arr, bins='auto')`，再分别对两个子集用 `np.histogram(..., bins=shared_bins)`，使两条直方图落在同一坐标上可比：

[共用边示例 — _histograms_impl.py:650-672](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L650-L672) 对比「各自 auto」会得到不同边、不同桶数，不可比。

`histogram2d` 对 `bins` 的归一逻辑值得一看——它用 `len(bins)` 是否能取到、取到是否为 2 来分情况：

[histogram2d 的 bins 归一 — _twodim_base_impl.py:853-862](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L853-L862) `len(bins)` 抛 TypeError 说明是单个整数/标量 → 两维同值；为 2 → 每维一份；其余（如 `len==3`）→ 把这个数组同时当成两维的边。这保证了传给 `histogramdd` 的 `bins` 一定是长度 2 的序列。

注意 `histogram2d` 的 dispatcher `_histogram2d_dispatcher` 有一段被源码自嘲为「terrible logic」的分支，目的就是模仿上述 `bins` 解析、把正确的数组参数交给 NEP-18：

[_histogram2d_dispatcher — _twodim_base_impl.py:677-692](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L677-L692) 注释直言 "This terrible logic is adapted from the checks in histogram2d"。

#### 4.3.4 代码实践

**实践目标**：用 `histogram_bin_edges` 让两个子集共用同一组边，体会「可比性」。

操作步骤：

```python
import numpy as np
rng = np.random.default_rng(0)
all_data = rng.standard_normal(200)
group = rng.integers(0, 2, 200)            # 0/1 两组标签

shared = np.histogram_bin_edges(all_data, bins='auto')   # 全局自动边
h0, _ = np.histogram(all_data[group == 0], bins=shared)
h1, _ = np.histogram(all_data[group == 1], bins=shared)
print("共享边桶数：", len(shared) - 1)
print("组0：", h0)
print("组1：", h1)
# 对比：各自 auto
print("各自 auto 桶数：",
      len(np.histogram(all_data[group == 0], bins='auto')[0]),
      len(np.histogram(all_data[group == 1], bins='auto')[0]))
```

**需要观察的现象**：`h0` 与 `h1` 长度相同、桶边界相同，可以逐桶相减比较；而「各自 auto」两条长度往往不同，无法直接对齐。

**预期结果**：`h0` 与 `h1` 是两个等长的计数数组。

#### 4.3.5 小练习与答案

**练习**：`np.histogram2d(x, y, bins=20)` 与 `np.histogram2d(x, y, bins=[20, 20])` 结果相同吗？

**参考答案**：相同。前者 `len(20)` 抛 TypeError → 退到「两维同值 20」；后者 `len([20,20])==2` → 每维 20。两者最终都给 `histogramdd` 传 `[20, 20]`，故结果一致（见 [bins 归一 — _twodim_base_impl.py:853-861](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_twodim_base_impl.py#L853-L861)）。

---

### 4.4 多维直方图 `histogramdd`：`ravel_multi_index` + `bincount`

#### 4.4.1 概念说明

`histogramdd` 处理 D 维空间的样本分布。它的输入 `sample` 有两种形态（这是它最容易踩坑的地方）：

- **`(N, D)` 数组**：每一行是一个 D 维坐标点（推荐写法）。
- **D 个 1D 数组的序列**：`(X, Y, Z)`，每个给出某一维的所有坐标。

输出是一个 D 维计数数组 `H`（形状 `(n_x, n_y, ..., n_d)`）和一组每维的边。

它的核心技巧是：**把每个样本的 D 个「桶坐标」压成一个一维下标，再用 `bincount` 一次性计数**。这就是 `np.ravel_multi_index` 的用武之地——它把多维下标映射成一维下标。

#### 4.4.2 核心流程

```text
1. 规整 sample：(N,D) 数组直接用；序列形态转成 (N,D)（atleast_2d().T）
2. 为每一维造边：
   - bins[i] 为标量 → linspace(该维 min, max, n+1)
   - bins[i] 为数组 → 原样用，校验单调
   - nbin[i] = len(edges[i]) + 1   ← 注意 +1，给「越界哨兵桶」留位
3. 对每一维用 searchsorted(side='right') 算每个样本落在第几桶
4. 把「恰好在末边」的样本退一格（最后一个桶双闭）
5. ravel_multi_index 把 D 维桶号压成一维
6. bincount 一维计数 → reshape 回 D 维
7. 切掉每维的首尾「哨兵桶」(slice(1,-1))
8. 可选 density：除以每维桶宽（构成桶体积）、再除以总样本数
```

`nbin[i] = len(edges[i]) + 1` 这个 +1 是关键：它在每一维的首尾各留一个「哨兵桶」用来接住落在范围之外的离群点，最后第 7 步再切掉。

#### 4.4.3 源码精读

输入规整的两条分支：

[sample 形态归一 — _histograms_impl.py:981-987](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L981-L987) `(N,D)` 数组直接解包；否则 `np.atleast_2d(sample).T` 把 `(X,Y,Z)` 转成 `(N,D)`。注释明确建议优先用第一种形态。

逐维造边与「+1 哨兵桶」：

[每维造边与 nbin — _histograms_impl.py:1012-1037](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L1012-L1037) `nbin[i] = len(edges[i]) + 1`，同时预计算每维桶宽 `dedges[i] = np.diff(edges[i])` 供 density 用。

桶号计算与末边修正：

[searchsorted 桶号 — _histograms_impl.py:1040-1053](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L1040-L1053) 注释说「avoid np.digitize to work around gh-11022」，改用 `searchsorted(side='right')`；再对落在末边的样本 `-= 1`。

把多维桶号压成一维并计数：

[ravel_multi_index + bincount — _histograms_impl.py:1055-1064](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L1055-L1064) `ravel_multi_index` 把 D 维下标压平，`bincount` 计数后 `reshape(nbin)` 还原成 D 维。`ravel_multi_index` 还顺带做了「数组过大」的溢出校验。

切掉哨兵桶：

[切除离群哨兵桶 — _histograms_impl.py:1069-1071](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L1069-L1071) `core = D * (slice(1, -1),)` 在每一维都切掉首尾。

density 归一化——多维下要除的是「桶体积」（各维桶宽的乘积），所以循环对每一维各除一次：

[多维 density 归一化 — _histograms_impl.py:1073-1080](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L1073-L1080) 用 `shape` 广播，逐维把 `hist` 除以该维的桶宽数组，最后再除总样本数 `s`。这样「高度 × 各维桶宽」的 D 重积分等于 1。

#### 4.4.4 代码实践

**实践目标**：体会 `sample` 两种输入形态等价，并验证 density 的「体积积分为 1」。

操作步骤：

```python
import numpy as np
rng = np.random.default_rng(1)
pts = rng.standard_normal(size=(1000, 2))   # (N, D) 形态，D=2

H1, (ex, ey) = np.histogramdd(pts, bins=(5, 8))
# 等价的「序列」形态：传两个 1D 数组
H2, _ = np.histogramdd((pts[:, 0], pts[:, 1]), bins=(5, 8))
print("形态1 == 形态2：", np.array_equal(H1, H2))
print("H.shape：", H1.shape, " 边数：", ex.size, ey.size)

Hd, _ = np.histogramdd(pts, bins=(5, 8), density=True)
dx = np.diff(ex); dy = np.diff(ey)
# 对每个二维桶，高度 × dx × dy，再求和 ⇒ 应为 1
vol = (Hd * np.outer(dx, dy)).sum()
print("二维体积积分：", vol)
```

**需要观察的现象**：`H1` 与 `H2` 完全相同（证明两种输入形态等价）；`vol` 约为 1.0。

**预期结果**：`形态1 == 形态2：True`，`二维体积积分：1.0`（±1e-15）。

> 说明：`np.outer(dx, dy)` 的形状 `(len(dx), len(dy))` 与 `Hd` 一致，相乘即得每桶体积权重。

#### 4.4.5 小练习与答案

**练习**：为什么 `histogramdd` 在每维都要 `len(edges[i]) + 1` 而不是直接用 `len(edges[i]) - 1`？

**参考答案**：多出来的两个位置（每维首尾各一个）是「哨兵桶」，用来容纳落在 `range` 之外的离群点，避免它们污染真实桶或触发越界。计数完成后用 `hist[core]`（`core = D*(slice(1,-1),)`）切掉它们（见 [切除哨兵桶 — _histograms_impl.py:1069-1071](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L1069-L1071)）。

---

### 4.5 自动分箱估计器：`_hist_bin_*` 家族与 `_hist_bin_selectors`

#### 4.5.1 概念说明

当 `bins` 是字符串时，numpy 会调一个「估计器」算出**最优桶宽** `h`，再用 `ceil(总跨度 / h)` 反推桶数。本仓库内置 8 个估计器，全部以 `_hist_bin_<name>(x, range)` 的统一签名实现，集中在 `_histograms_impl.py` 顶部，并用一张字典 `_hist_bin_selectors` 注册：

| 名字 | 公式（桶宽 h，n 为样本数） | 直觉 | 主要依赖 |
| --- | --- | --- | --- |
| `sqrt` | \(h = \mathrm{ptp}/\sqrt{n}\) | 最朴素，Excel 默认 | 仅样本量 |
| `sturges` | \(h = \mathrm{ptp}/(\log_2 n + 1)\) | 假设正态，R 默认 | 仅样本量 |
| `rice` | \(h = \mathrm{ptp}/(2 n^{1/3})\) | 渐近最优但常高估桶数 | 仅样本量 |
| `scott` | \(h = \sigma \sqrt[3]{24\sqrt{\pi}/n}\) | 用标准差，对离群点敏感 | 标准差 |
| `fd` | \(h = 2\,\mathrm{IQR}/n^{1/3}\) | 用四分位距，稳健 | IQR |
| `doane` | Sturges + 偏度修正 | 对非正态更好 | 偏度 |
| `stone` | 最小化交叉验证 ISE | Scott 的推广，最复杂 | 全数据 |
| `auto` | \(\min(\max(\mathrm{fd}, \sqrt{\mathrm{ptp}/\sqrt n}/2), \mathrm{sturges})\) | FD 与 Sturges 的融合 | fd/sturges/sqrt |

一个贯穿性的数学事实是：**桶数与 \(n^{1/3}\) 成正比是渐近最优的**，所以 `rice`/`scott`/`fd` 里都出现了 \(n^{1/3}\)（即 `n ** (1.0/3)`）。这也是 `histogram_bin_edges` 文档 Notes 里强调的一点。

#### 4.5.2 核心流程

```text
_get_bin_edges（字符串分支）
   → _hist_bin_selectors[name](a, (first_edge, last_edge))   # 返回桶宽 width
   → n_equal_bins = ceil(总跨度 / width)
```

每个估计器内部都接收「裁剪到 range 内的数据 `x`」与 `(first_edge, last_edge)`，返回一个**桶宽**（不是桶数！）。注意：绝大多数估计器只用 `x`、忽略 `range` 参数（函数体里 `del range`），只有 `stone` 真正用到 `range`（因为它内部要反复跑 `np.histogram` 试不同桶数）。

`auto` 是融合型：它先算 FD、Sturges、sqrt 三个桶宽，做一个「FD 不能太小」的下界修正，再取 FD（修正后）与 Sturges 的较小者。

#### 4.5.3 源码精读

**Sturges** —— 最简单、R 的默认值，只依赖样本量：

[_hist_bin_sturges — _histograms_impl.py:53-73](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L53-L73) `del range` 表明忽略 range；返回 `ptp / (log2(size) + 1)`。注释指出它假设正态、对非正态大样本偏保守。

**Scott** —— 用标准差，桶宽与 \(n^{1/3}\) 成反比：

[_hist_bin_scott — _histograms_impl.py:100-119](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L100-L119) 返回 `(24.0 * np.pi**0.5 / x.size) ** (1/3) * np.std(x)`，即 \(h = \sigma\sqrt[3]{24\sqrt{\pi}/n}\)。

**Freedman-Diaconis（fd）** —— 用 IQR 代替标准差，稳健：

[_hist_bin_fd — _histograms_impl.py:200-227](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L200-L227) `iqr = np.subtract(*np.percentile(x, [75, 25]))`，返回 `2.0 * iqr * x.size ** (-1.0/3.0)`，即 \(h = 2\,\mathrm{IQR}/n^{1/3}\)。IQR=0 时返回 0（触发桶数兜底为 1）。

**auto** —— 融合 FD、Sturges、sqrt：

[_hist_bin_auto — _histograms_impl.py:230-264](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L230-L264) 三步：算 fd/sturges/sqrt 三宽；`fd_bw_corrected = max(fd_bw, sqrt_bw / 2)` 防止 FD 给出过小桶；`return min(fd_bw_corrected, sturges_bw)`。注释说小数据集（约 <1000）会偏向 Sturges、大数据集偏向 FD，切换点约在 `size ≈ 1000`。

> 这正是 `bins='auto'`（也是 matplotlib `plt.hist` 的默认）能「开箱即用」的原因：它在「FD 太保守（小数据桶太少）」和「Sturges 太保守（大数据桶太少）」之间各取所长。

**stone** —— 唯一用到 range、且会自调用 `np.histogram` 做交叉验证的估计器：

[_hist_bin_stone — _histograms_impl.py:122-162](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L122-L162) 在 `[1, max(100, sqrt(n))]` 范围内遍历候选桶数，对每个算留一法 ISE 估计 `jhat`，取使 `jhat` 最小的桶数；若触到上界则发 `RuntimeWarning` 提示「may be suboptimal」。

**注册表** —— 用字典把名字映射到函数：

[_hist_bin_selectors — _histograms_impl.py:268-275](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L268-L275) 这是 `_get_bin_edges` 字符串分支里 `_hist_bin_selectors[bin_name](a, ...)` 查表的依据，也是「非法估计器名」报错的依据（不在表里即 `ValueError`）。

`_ptp` 与 `_unsigned_subtract` 是多数估计器的共用底座——它们都需要「数据跨度」，而有符号整数要用无符号减法避免溢出：

[_ptp — _histograms_impl.py:22-29](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L22-L29) `_ptp = _unsigned_subtract(x.max(), x.min())`。

#### 4.5.4 代码实践（本讲主实践）

**实践目标**：在同一组数据上，用 `density=True` 对比 `'fd'` 与 `'auto'` 两种自动分箱的**桶数**与**密度曲线**差异，直观体会 auto 对 FD 的修正。

操作步骤：

```python
import numpy as np

rng = np.random.default_rng(42)
# 构造一个「双峰」混合：小样本，正是 FD 容易给太少桶的场景
x = np.concatenate([rng.normal(0, 1, 150), rng.normal(6, 1, 150)])

for method in ['fd', 'auto', 'sturges', 'scott', 'sqrt']:
    h, edges = np.histogram(x, bins=method, density=True)
    area = np.sum(h * np.diff(edges))
    print(f"{method:8s} 桶数={len(h):3d}  密度面积={area:.4f}")

# 也可以直接只取边，对比 auto 与 fd 的桶宽
e_fd   = np.histogram_bin_edges(x, bins='fd')
e_auto = np.histogram_bin_edges(x, bins='auto')
print("fd   桶宽(首三):", np.round(np.diff(e_fd)[:3], 3))
print("auto 桶宽(首三):", np.round(np.diff(e_auto)[:3], 3))
```

**需要观察的现象**：

1. 每个估计器的「密度面积」都应是 1.0（density 的不变量，与方法无关）。
2. 在这个约 300 点、双峰的数据上，`fd` 与 `auto` 桶数很可能不同：当 FD 给出的桶宽偏大（桶太少）时，`auto` 会落到 Sturges 一侧，桶数更接近 Sturges。
3. `sqrt` 通常桶数最多，`sturges`/`scott` 居中。

**预期结果**（具体桶数随数据而变，重点是关系而非数值）：

- 五个方法的 `密度面积` 均为 `1.0000`。
- `auto` 的桶数等于 `min(fd 修正后桶数, sturges 桶数)` 对应的那个，往往 ≠ `fd`。

> 说明：上述桶数未在本机实跑，属「待本地验证」；但「密度面积恒为 1」「auto 在 fd 与 sturges 间取小」这两点是源码确定的。

**如何用测试套件复核**：仓库测试 `TestHistogramOptimBinNums.test_simple` 固化了一组基准桶数，可直接对照。它对 50/500/5000 三种规模的双峰 linspace 数据列出了每个估计器应有的桶数：

[test_simple 基准桶数表 — tests/test_histograms.py:447-471](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_histograms.py#L447-L471) 例如 5000 点时 `fd=17, scott=17, rice=35, sturges=14, doane=17, sqrt=71, auto=17, stone=20`。这张表是理解各估计器「桶数随 n 增长速率」最直观的参照。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `_hist_bin_fd` 在 IQR=0 时返回 0，而 `_get_bin_edges` 不报错？

**参考答案**：IQR=0 意味着数据中间 50% 完全集中在一个点（如大量重复值）。`fd` 返回 0 后，`_get_bin_edges` 的字符串分支检测到 `width` 为假值，把桶数兜底为 1（见 [width==0 兜底 — _histograms_impl.py:411-414](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_histograms_impl.py#L411-L414)）。测试 `test_novariance` 正好固化了这一点（见 [tests/test_histograms.py:501-513](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_histograms.py#L501-L513)）。

**练习 2**：`auto` 里 `fd_bw_corrected = max(fd_bw, sqrt_bw / 2)` 这一行去掉会怎样？

**参考答案**：这一行是「限制最大桶数」的启发式护栏——当 FD 给出极小桶宽（极多桶）时，用 `sqrt` 桶宽的一半作为下界把它抬高，避免桶数爆炸。去掉后，对某些长尾或含离群点的数据，`auto` 可能给出非常多的桶，失去「开箱即用」的稳健性。

---

## 5. 综合实践

把本讲四个知识点（边解析、快慢路径、density、自动分箱）串起来，完成一个小任务：**比较五种自动分箱在同一数据上的密度估计质量**。

任务步骤：

1. 用 `rng.standard_normal(2000)` 生成数据，并人工注入少量离群点（如把最后 20 个值乘 10）。
2. 对 `['sturges', 'scott', 'fd', 'auto', 'sqrt']` 五种方法，分别求 `density=True` 直方图。
3. 对每条结果验证「面积积分 == 1」。
4. 把边统一成 `'auto'` 的边（用 `histogram_bin_edges`），再用同一组边重算另外四种方法的计数，观察「同边」下计数差异消失、只剩密度归一化差异。
5. 记录每种方法的桶数，对照 [tests/test_histograms.py:447-471](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_histograms.py#L447-L471) 的增长规律，判断哪种对离群点最稳健（提示：看 `fd` 与 `scott` 的差距）。

参考骨架：

```python
import numpy as np
rng = np.random.default_rng(7)
x = rng.standard_normal(2000)
x[-20:] *= 10                       # 注入离群点

for m in ['sturges', 'scott', 'fd', 'auto', 'sqrt']:
    h, e = np.histogram(x, bins=m, density=True)
    print(f"{m:8s} 桶数={len(h):3d}  面积={np.sum(h*np.diff(e)):.4f}")

# 统一边后再比
shared = np.histogram_bin_edges(x, bins='auto')
print("共享边桶数：", len(shared) - 1)
for m in ['sturges', 'scott', 'fd', 'auto', 'sqrt']:
    h, _ = np.histogram(x, bins=shared)        # 注意：这里 bins 是边数组，m 失效
    # ↑ 这一行说明：一旦用边数组，"方法"就不再起作用，只剩 density 归一化差异
```

**思考题**：第 5 步里，为什么一旦改用 `bins=shared`（边数组），`m` 这个循环变量就完全不起作用了？这印证了本讲哪条核心结论？

> 参考答案：这印证了「4.2 边解析」的核心——当 `bins` 是数组时，`_get_bin_edges` 走数组分支，**原样返回**，根本不经过任何估计器。所以分箱方法只在「桶宽由数据决定」时才有意义；一旦边被固定，所有方法的区别就只剩 density 归一化（而边相同时归一化也无差别）。

## 6. 本讲小结

- 直方图家族是**分层**的：`histogram`（1D）与 `histogramdd`（ND）是两个真正计数的主函数；`histogram2d` 只是 `histogramdd` 的薄封装（住在 `_twodim_base_impl.py`）；`histogram_bin_edges` 是 `histogram` 的「半身」——只跑 `_get_bin_edges`、不计数。
- **边解析与计数解耦**：`_get_bin_edges` 把「整数/边数组/字符串」三种 `bins` 统一成 `(bin_edges, uniform_bins)`；`uniform_bins` 非 `None` 即等距、走快路（一次式下标 + `bincount`），为 `None` 即不等距、走慢路（排序 + searchsorted 累积直方图）。
- **最后一个桶双闭**是全家族约定，由 `_search_sorted_inclusive`（1D 慢路）与 `histogramdd` 里「末边样本退一格」共同实现。
- `density=True` 的不变量是**面积/体积积分等于 1**，不是计数和等于 1；1D 实现 `n / diff(bin_edges) / n.sum()`，ND 实现「逐维除桶宽、再除总样本数」。
- `histogramdd` 用 `ravel_multi_index` 把多维桶号压平、`bincount` 计数，并为每维预留「哨兵桶」接离群点、最后 `slice(1,-1)` 切掉。
- 8 个自动分箱估计器统一签名 `_hist_bin_<name>(x, range)`、用字典 `_hist_bin_selectors` 注册；多数只依赖样本量或简单统计量（std/IQR），桶数与 \(n^{1/3}\) 成正比是渐近最优；`auto` = 融合 FD 与 Sturges，是「开箱即用」的默认。

## 7. 下一步学习建议

- 本讲反复出现的「自动分箱返回**桶宽**而非桶数」「桶数与 \(n^{1/3}\) 成正比」等统计直觉，可以结合前置讲义 **u7-l1（`_ureduce` 归约框架与 `median`/`cov`）** 里的 `partition`、`percentile` 思路一起看——`_hist_bin_fd` 内部用的就是 `np.percentile(x, [75, 25])`。
- 若想进一步看「分位数插值」本身的实现细节，可读 **u7-l2（百分位与分位数插值算法）**，它精讲了 `_quantile_unchecked` / `_lerp` 等，与 `_hist_bin_fd` 调用的 `percentile` 是同一套机制。
- 直方图是「把连续值离散化到桶」的代表；下一类「把值离散化」的工具是 **u6-l3 的 `digitize`**（用 `searchsorted` 把值归入给定桶），与本讲的 `searchsorted` 计桶号一脉相承，建议对照阅读。
- 想深入「直方图反问题」（从直方图估回分布）可继续阅读 `numpy.lib` 的 `interp`（u6-l2）与统计模块外部资料；本仓库内可重点跟踪 `tests/test_histograms.py` 的 `TestHistogramdd` 与密度相关用例（如 `test_density_non_uniform_2d`，第 821 行），它们对 density 在不等宽桶下的行为有精确断言。

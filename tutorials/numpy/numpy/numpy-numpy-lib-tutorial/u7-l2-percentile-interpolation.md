# 百分位与分位数插值算法

## 1. 本讲目标

本讲精读 `numpy/lib/_function_base_impl.py` 中分位数计算的核心实现。`np.percentile` 与 `np.quantile` 看似只是「排序后取第几个值」，但 numpy 支持 **13 种** `method`，它们对应不同的统计学估计量。读完本讲，你应当能够：

- 说清 `percentile` 与 `quantile` 这两个公开入口如何只做参数规整、再把活儿全部转交给 `_quantile_unchecked`。
- 理解**虚索引（virtual index）**这一核心抽象：先在排序数组上算出一个「浮点位置」，再决定是直接取整还是做线性插值。
- 手算 **alpha-beta 公式** \(\,h = q(n+1-\alpha-\beta)+\alpha-1\,\)，并能把它对应到 H&F 论文的第 4–9 号连续估计量。
- 区分**连续方法**（`linear`/`hazen`/`weibull`/`median_unbiased`/`normal_unbiased`/`interpolated_inverted_cdf`）与**离散方法**（`lower`/`higher`/`midpoint`/`nearest`/`inverted_cdf`/`closest_observation`），看懂它们在「取整数索引」还是「取小数索引 + 插值」上的分叉。
- 读懂 `_lerp` 里那个看似多余的 `where=t>=0.5` 分支——它其实是**数值稳定性**的精妙设计。
- 跟踪从 `_quantile_unchecked` → `_ureduce` → `_quantile_ureduce_func` → `_quantile` 的完整执行链。

本讲承接 [u7-l1](u7-l1-ureduce-median-cov.md) 已建立的 `_ureduce` 通用归约框架认知：分位数计算复用了 `_ureduce` 来处理 `axis`/`keepdims`/`out`，本讲只聚焦 `_ureduce` **内部那个真正算分位数的函数**。后续 [u7-l3（直方图）](u7-l3-histogram-binning.md)会再复用排序与分区思路，[u9（NaN 感知函数）](u9-l1-nan-infra-aggregation.md)的 `nanpercentile` 将直接调用本讲的 `_quantile_unchecked`。

## 2. 前置知识

### 分位数与百分位

给定一组数 \(a\)，其 \(q\) 分位数（\(q\in[0,1]\)）是「按从小到大排，有 \(q\) 比例的数据小于等于它」的那个值。百分位就是把它乘 100：`percentile(a, 25)` 等价于 `quantile(a, 0.25)`。numpy 里二者的关系就是这一句除法（见后文 L4241）。

### 核心难点：当位置落在两个数之间怎么办

关键问题来了：假设有 10 个数，想求 0.25 分位。排序后，「第 25% 个位置」对应 \(0.25\times(10-1)=2.25\)，是个**小数**，落在第 2 个和第 3 个数（0 基索引）之间。怎么取值？这就是分位数算法的全部分歧所在：

1. **线性插值**：取 \(x_2 + 0.25\times(x_3-x_2)\)，即两个邻居按小数部分加权平均。
2. **取下界 `lower`**：直接取 \(x_2\)。
3. **取上界 `higher`**：直接取 \(x_3\)。
4. **取中点 `midpoint`**：取 \((x_2+x_3)/2\)。
5. **取最近 `nearest`**：四舍五入到最近的一个。

不同的统计学派还会对「这个小数位置到底应该是几」给出不同公式。Hyndman & Fan（1996）在论文 *Sample Quantiles in Statistical Packages* 里系统整理了 9 种约定，numpy 全部支持，外加 4 种向后兼容变体。

### 虚索引（virtual index）与 gamma

numpy 用两个量把上面所有分歧统一起来：

- **虚索引 \(h\)**：在排序数组里「应该取」的浮点位置。整数部分 \(j=\lfloor h\rfloor\) 指向左邻居，小数部分 \(g=h-j\)（numpy 内部叫 **gamma**）是插值权重。
- 最终值 \(=(1-g)\cdot x_j + g\cdot x_{j+1}\)。当 \(g=0\) 时退化成直接取 \(x_j\)。

所有 13 种 `method` 的区别，本质上就是**两个选择**：

1. 用什么公式算 \(h\)（连续方法）或直接算成一个整数索引（离散方法）；
2. 对 \(g\) 做什么修正（`fix_gamma`）。

这就是本讲的全部分析框架。

### 复习：`partition` 与 `_ureduce`

- `partition(kth)` 只保证「第 k 小的值落到第 k 位」，平均 \(O(n)\)，比全排序快——上一讲 `median` 用过，本讲 `_quantile` 也用它。
- `_ureduce` 是通用归约框架，统一处理 `axis`/`keepdims`/`out`，把多轴归约翻译成单轴，再把真正的计算委托给它收到的 `func`。本讲里那个 `func` 就是 `_quantile_ureduce_func`。

## 3. 本讲源码地图

本讲所有源码都在同一个文件：

| 符号 | 角色 | 行号区间 | 可见性 |
|------|------|----------|--------|
| `percentile` | 公开入口，参数规整 | L4064–L4257 | 公开（`np.percentile`） |
| `quantile` | 公开入口，参数规整 | L4266–L4506 | 公开（`np.quantile`） |
| `_quantile_unchecked` | 把活儿转交给 `_ureduce` | L4509–L4528 | 私有 |
| `_quantile_ureduce_func` | 处理 axis/copy，调用 `_quantile` | L4648–L4685 | 私有 |
| `_QuantileMethods` | 13 种方法的「两插槽」配置表 | L93–L172 | 私有 |
| `_compute_virtual_index` | 连续方法的 alpha-beta 虚索引公式 | L4542–L4564 | 私有 |
| `_inverted_cdf` / `_closest_observation` | 离散 H&F 方法的索引计算 | L4634–L4645 | 私有 |
| `_discrete_interpolation_to_boundaries` | 离散索引的公共底座 | L4620–L4631 | 私有 |
| `_get_gamma` | 算插值权重 gamma（含 `fix_gamma` 修正） | L4567–L4588 | 私有 |
| `_get_gamma_mask` | gamma 修正用的「按条件填值」工具 | L4614–L4617 | 私有 |
| `_lerp` | 线性插值内核（含数值稳定性分支） | L4591–L4611 | 私有 |
| `_quantile` | 真正干活的内核（三分支） | L4723–L4912 | 私有 |

> 提示：本讲引用的永久链接 base 为 `https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/`，HEAD 为 `b21650c4f6`。

---

## 4. 核心概念与源码讲解

### 4.1 公开入口：percentile / quantile 与 _quantile_unchecked

#### 4.1.1 概念说明

`np.percentile` 与 `np.quantile` 是分位数计算的两个公开入口。它们的**唯一差别**是 `q` 的取值范围：`percentile` 用 \([0,100]\)，`quantile` 用 \([0,1]\)。除此之外，二者的参数（`axis`/`out`/`overwrite_input`/`method`/`keepdims`/`weights`）与内部流程**完全一致**。

这两个公开函数本身**不做任何分位数计算**，它们只负责三件事：把输入转成数组、校验 `q` 的范围、校验 `weights`，然后把所有参数原封不动地传给私有函数 `_quantile_unchecked`。这是一种典型的「入口薄、实现深」的分层——和 [u1-l2](u1-l2-module-organization.md) 讲的 dispatcher+impl 分层一脉相承。

#### 4.1.2 核心流程

以 `percentile` 为例，入口流程是：

```
percentile(a, q, ...)
  ├─ a = asanyarray(a)                      # 转数组，复数报错
  ├─ weak_q = type(q) in (int, float)       # 标记 q 是否为 Python 标量（影响结果 dtype）
  ├─ q = q / 100                            # ★ 百分位→分位，这里是与 quantile 的唯一差别
  ├─ _quantile_is_valid(q)                  # 校验 q ∈ [0,1]
  ├─ 若有 weights：只允许 method="inverted_cdf"，且权重非负
  └─ return _quantile_unchecked(...)        # 全部转交
```

`quantile` 流程几乎相同，只是**省去了除以 100 那一步**，且 `q = asanyarray(q)`（不除法）。

#### 4.1.3 源码精读

`percentile` 把 `q` 缩放到 \([0,1]\) 的关键一行：

[_function_base_impl.py:L4240-L4243](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4240-L4243) — 计算 `weak_q` 标志，把百分位除以 100 转成分位，并校验范围。`weak_q` 记录 `q` 是否为 Python 原生 `int`/`float`（而非 numpy 标量），它会被一路传递到内核，用于决定离散方法的整数结果是否「弱提升」——这是后续 `_quantile` 内核里 `weak_q` 参数的来源。

`percentile` 末尾把所有参数转交：

[_function_base_impl.py:L4256-L4257](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4256-L4257) — 调用 `_quantile_unchecked`，把缩放后的 `q` 与所有形状控制参数传下去。

`quantile` 与之对应，区别仅在 `q = asanyarray(q)` 而非除法：

[_function_base_impl.py:L4488-L4492](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4488-L4492) — `quantile` 算 `weak_q`、转数组、校验 `q ∈ [0,1]`（注意范围是 1 不是 100）。

而 `_quantile_unchecked` 本身只有「一句话」——它把真正的归约框架 `_ureduce` 请出来，把 `_quantile_ureduce_func` 作为 `func` 传进去：

[_function_base_impl.py:L4509-L4528](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4509-L4528) — `_quantile_unchecked` 把 `_quantile_ureduce_func` 包进 `_ureduce`。它的 docstring 直白地写着「假设 q 已在 \([0,1]\) 且是 ndarray」，所以校验全部由上游两个公开入口负责，这里不再重复。

#### 4.1.4 代码实践

**目标**：验证 `percentile` 与 `quantile` 的「除以 100」等价关系。

**步骤**：

```python
import numpy as np
a = np.array([10, 7, 4, 3, 2, 1])

print(np.percentile(a, 25))      # 百分位 25
print(np.quantile(a, 0.25))      # 分位 0.25
print(np.percentile(a, 25, method="lower"))
print(np.quantile(a, 0.25, method="lower"))
```

**预期结果**：前两行相等，后两行相等。`percentile(a, 25)` ≡ `quantile(a, 0.25)`。

**观察要点**：`method="linear"`（默认）会得到小数 `3.25`，而 `method="lower"` 得到整数 `3`。这个差异正是本讲要解释的核心。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `percentile` 里要先算 `weak_q = type(q) in (int, float)`，再 `q = q/100`？顺序能反过来吗？

> **答案**：必须先判 `type(q)`。因为 `q/100` 会把 Python `int`/`float` 变成 numpy 浮点数组或 numpy 标量（`type` 改变），`weak_q` 的本意是「用户原始传的是不是 Python 原生标量」，所以必须在除法之前采样。`weak_q` 影响离散方法的整数结果是否做弱类型提升。

**练习 2**：`np.percentile(a, 50)` 报错 `Percentiles must be in the range [0, 100]`，可能的原因是什么？

> **答案**：几乎不可能由 50 本身触发。这个错来自 `_quantile_is_valid(q)` 在 `q/100` 之后检查。若 `a` 是复数数组会先在 L4237–L4238 抛 `TypeError`（"a must be an array of real numbers"）。范围错误通常意味着传入的 `q`（如数组）含越界元素，例如 `np.percentile(a, [25, 120])` 中的 120。

---

### 4.2 归约调度：_quantile_ureduce_func 与 _ureduce 的衔接

#### 4.2.1 概念说明

`_ureduce`（上一讲详细讲过）负责把任意 `axis`（标量、元组、`None`）的归约统一翻译成「单轴归约」，并管理 `keepdims`/`out`。但 `_ureduce` **不懂分位数**——它只提供一个「在已经规整好的单轴数组上干活」的回调 `func`。`_quantile_ureduce_func` 就是这个回调：它在 `_ureduce` 已经把待归约轴挪到「最后一维」、处理好 `keepdims` 之后，负责真正计算分位数。

这一层的工作很「脏」但很重要：处理 `overwrite_input`（是否允许就地修改输入以省内存）、`axis=None`（拍平）、以及把 `a` 复制成 `arr` 避免误伤。它算完之后把控制权交给最底层的 `_quantile`。

#### 4.2.2 核心流程

```
_quantile_ureduce_func(a, q, weights, axis, out, overwrite_input, method, weak_q)
  ├─ 校验 q.ndim <= 1                          # 只支持标量或一维 q
  ├─ 根据 overwrite_input 与 axis 决定 arr：
  │    · overwrite_input=True → 直接用 a（会被破坏）
  │    · axis=None → arr = a.flatten()/ravel()
  │    · 否则 → arr = a.copy()                  # ★ 默认拷贝，保护原数组
  └─ return _quantile(arr, q, axis, method, out, weights, weak_q)
```

关键细节：**默认会拷贝一份**。分位数计算要排序/分区，会重排元素，若不拷贝就会破坏用户的原数组。只有显式 `overwrite_input=True` 才会跳过拷贝（此时函数返回后 `a` 的内容「未定义」）。

#### 4.2.3 源码精读

[_function_base_impl.py:L4648-L4685](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4648-L4685) — 完整的 `_quantile_ureduce_func`。注意四个分支：`overwrite_input` 与 `axis is None` 的组合决定了 `arr` 到底是 `a.ravel()`（就地、拍平）、`a`（就地、保留轴）还是 `a.copy()`/`a.flatten()`（拷贝）。最后统一调用 `_quantile`。

特别看 `axis is None` 时的拍平：

[_function_base_impl.py:L4671-L4677](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4671-L4677) — 当 `axis=None` 且不覆写时，`arr = a.flatten()`（返回新数组）；覆写时 `arr = a.ravel()`（可能返回视图，从而真的改到原数组）。`flatten` 与 `ravel` 的视图语义差异在这里被刻意利用。

> 说明：本函数是 `_ureduce` 的 `func` 回调。`_ureduce`（[L3827 起](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L3827)）会在调用本函数前后做 `moveaxis`/`reshape`/`keepdims` 补维等操作，详见 [u7-l1](u7-l1-ureduce-median-cov.md)。

#### 4.2.4 代码实践

**目标**：观察 `overwrite_input` 对输入数组的影响。

**步骤**：

```python
import numpy as np
a = np.array([[10, 7, 4], [3, 2, 1]])
b = a.copy()

r1 = np.percentile(a, 50, axis=1)                      # 默认 overwrite_input=False
r2 = np.percentile(b, 50, axis=1, overwrite_input=True) # 就地

print("默认调用后 a 是否改变：", np.array_equal(a, [[10,7,4],[3,2,1]]))  # True，未被破坏
print("覆写调用后 b 是否改变：", np.array_equal(b, [[10,7,4],[3,2,1]]))  # False，被破坏
print(r1, r2)  # 两者结果一致：[7. 2.]
```

**预期结果**：`a` 保持原样，`b` 的元素顺序被打乱（因为内部做了 `partition`）。两个结果相同。

**观察要点**：这解释了为什么 `overwrite_input` 能省内存——它省掉的就是 `_quantile_ureduce_func` 里那次 `a.copy()`。

#### 4.2.5 小练习与答案

**练习 1**：`_quantile_ureduce_func` 为什么要检查 `q.ndim > 2` 并报错？注释说「代码对 nd 也工作，只是语义可能没用」。

> **答案**：当前公开 API 只允许 `q` 为标量或一维数组（即对每个分位水平产生一个结果，结果的第一维对应 `q`）。更高维 `q` 在内部循环里技术上能跑，但输出形状的语义不清晰、无人测试，所以用显式 `ValueError("q must be a scalar or 1d")` 防御。

**练习 2**：`overwrite_input=True` 时，函数返回后输入数组 `a` 处于什么状态？

> **答案**：`a` 的内容「未定义」（docstring 原话：`the contents of the input a after this function completes is undefined`）。它已被 `partition` 部分重排，元素还在但顺序乱了，不应再依赖其值。

---

### 4.3 连续方法的虚索引：_compute_virtual_index 与 alpha-beta 公式

#### 4.3.1 概念说明

这是本讲最核心的数学。Hyndman & Fan 论文里的连续估计量（第 4–9 号）都可以写成一个**统一的虚索引公式**，只由两个常数 \(\alpha\)、\(\beta\) 决定：

\[
h = q\,(n + 1 - \alpha - \beta) + \alpha - 1
\]

等价地（展开后重组）：

\[
h = n\,q + \bigl(\alpha + q\,(1-\alpha-\beta)\bigr) - 1
\]

numpy 源码用的就是第二种形式。其中：

- \(n\) 是样本量（待归约轴的长度）；
- \(q\) 是分位水平（已归一化到 \([0,1]\)）；
- \(h\) 是排序数组里的浮点位置（0 基）。

不同的 \((\alpha,\beta)\) 对应不同的 H&F 方法。numpy 代码里把每个方法写成一个 lambda，调用 `_compute_virtual_index(n, q, alpha, beta)`：

| 方法 | H&F 编号 | \(\alpha\) | \(\beta\) | 直觉 |
|------|----------|-----------|-----------|------|
| `interpolated_inverted_cdf` | 4 | 0 | 1 | 极端偏向小值 |
| `hazen` | 5 | 0.5 | 0.5 | 折中 |
| `weibull` | 6 | 0 | 0 | 偏向大值 |
| `linear`（默认） | 7 | 1 | 1 | 退化为 \((n-1)q\) |
| `median_unbiased` | 8 | 1/3 | 1/3 | 中位无偏 |
| `normal_unbiased` | 9 | 3/8 | 3/8 | 正态无偏 |

注意 `linear` 的 \(\alpha=\beta=1\) 代入公式：\(h = nq + (1 + q(1-1-1)) - 1 = nq - q = (n-1)q\)。所以 `linear` 是「在 0 和 \(n-1\) 之间均匀插值」，最直观。源码注释因此说：为了避免浮点误差，`linear` 直接用 `(n-1)*q`，而**不走** `_compute_virtual_index`——两者数学等价但前者更稳。

#### 4.3.2 核心流程

```
对连续方法（fix_gamma 非空）：
  h = _compute_virtual_index(n, q, alpha, beta)    # 浮点虚索引
  j = floor(h)                                      # 左邻居整数下标
  g = h - j                                         # gamma 插值权重
  对大多数连续方法：gamma 不修正
  结果 = _lerp(x_j, x_{j+1}, g)                     # 线性插值
```

#### 4.3.3 源码精读

虚索引公式本体（注意它是纯数学，无副作用）：

[_function_base_impl.py:L4542-L4564](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4542-L4564) — `_compute_virtual_index`。docstring 直接引用了 H&F 论文 DOI。函数体就一行 `return`，对应公式 \(h=nq+\alpha+q(1-\alpha-\beta)-1\)。

方法表里这些 lambda 把 \((\alpha,\beta)\) 焊死进公式：

[_function_base_impl.py:L113-L145](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L113-L145) — 6 个连续方法的 `get_virtual_index` 配置。每个 lambda 调用 `_compute_virtual_index(n, quantiles, alpha, beta)`，`fix_gamma` 都是恒等函数 `lambda gamma, _: gamma`（不修正权重）。

而 `linear` 是特例，跳过 `_compute_virtual_index`：

[_function_base_impl.py:L128-L135](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L128-L135) — `linear` 的 `get_virtual_index` 直接用 `(n-1)*quantiles`，注释说明这是为了避免 `_compute_virtual_index(n, q, 1, 1)` 的浮点问题，二者数学等价。

> 公式校验示例：`median_unbiased` \(\alpha=\beta=1/3\)，代入得 \(h = q(n+\tfrac13)-\tfrac23\)，正是 H&F 第 8 号。`normal_unbiased` 得 \(h=q(n+\tfrac14)-\tfrac58\)，对应第 9 号。

#### 4.3.4 代码实践

**目标**：手算并验证 alpha-beta 公式。

**步骤**：取 `a = [1,2,3,4,5,6,7,8,9,10]`（\(n=10\)），求 `q=0.5` 的 `median_unbiased` 分位。

1. 按公式算虚索引：\(h = 0.5\times(10+\tfrac13)-\tfrac23 = 0.5\times 10.333 - 0.667 = 5.167 - 0.667 = 4.5\)。
2. \(j=\lfloor 4.5\rfloor=4\)，\(g=0.5\)。
3. 结果 \(= x_4 + 0.5\times(x_5-x_4) = 5 + 0.5\times(6-5) = 5.5\)。

```python
import numpy as np
a = np.arange(1, 11)
print(np.quantile(a, 0.5, method="median_unbiased"))  # 应为 5.5
print(np.quantile(a, 0.5, method="linear"))           # (n-1)*0.5 = 4.5 → 5.5（巧合相同）
```

**预期结果**：`5.5`。本例 `linear` 与 `median_unbiased` 恰好相同，换个 `q`（如 0.25）就会分开。

**观察要点**：把 `method` 换成 `normal_unbiased`、`hazen`、`weibull`，结果都会不同——它们都是对「中位数」的不同估计。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `linear` 不直接调用 `_compute_virtual_index(n, q, 1, 1)`？

> **答案**：`_compute_virtual_index(n, q, 1, 1)` 算的是 \(nq + 1 + q(1-1-1) - 1 = nq - q\)，数学上等于 \((n-1)q\)。但前者多做了一次 `1 + q*(1-1-1)` 的浮点运算，可能引入微小舍入误差；直接写 `(n-1)*q` 更精确、更快。源码注释明确把这一点记为「to avoid some rounding issues」。

**练习 2**：`weibull`（\(\alpha=\beta=0\)）对 `q=0` 给出什么虚索引？这意味着什么？

> **答案**：\(h = n\times 0 + (0 + 0) - 1 = -1\)。虚索引为负！这会触发后文 `_get_indexes` 的「索引低于下界」分支，把 `previous`/`next` 都钳到 0，即取数组最小值。所以 `weibull` 的 0 分位就是最小值本身（合理：0 分位本就应是下界）。

---

### 4.4 离散方法的索引：_inverted_cdf 与 _closest_observation

#### 4.4.1 概念说明

前面 4.3 讲的连续方法都返回**浮点虚索引**，然后做线性插值。但 H&F 论文的前三号方法是**离散的**——它们直接挑排序数组里的某一个具体元素，不插值。numpy 实现它们时，`get_virtual_index` 返回的就是**整数索引**（而非浮点位置），`fix_gamma` 设为 `None`（永远不会被调用）。

本模块聚焦其中两个**真正的离散函数**（`lower`/`higher`/`nearest` 只是 `floor`/`ceil`/`around` 一行，放在 4.5 一起讲）：

- **`inverted_cdf`**（H&F 1）：\(m=0\)，即虚索引初值 \(nq-1\)。规则是「向上取整，但恰好落在整数时向下」：若小数部分 \(>0\) 取 `ceil`，否则取 `floor`。
- **`closest_observation`**（H&F 3）：\(m=-1/2\)，虚索引初值 \(nq-1-0.5\)。规则最微妙——当恰好落在整数位置时，取**最近的偶数阶顺序统计量**。

二者共享同一个公共底座 `_discrete_interpolation_to_boundaries`，它把「浮点索引 + 一个 gamma 条件函数」翻译成「整数下标」。

#### 4.4.2 核心流程

`_discrete_interpolation_to_boundaries` 的统一逻辑：

```
给定浮点 index 和条件函数 gamma_condition_fun(gamma, index):
  previous = floor(index)          # 左邻居
  next     = previous + 1          # 右邻居
  gamma    = index - previous      # 小数部分
  默认选 next（ceil 方向）
  当 gamma_condition_fun(gamma, index) 为真 → 改选 previous（floor 方向）
  把负索引钳到 0
```

两个方法只差「条件函数」和「初值」：

| 方法 | 初值 index | 条件（为真时取 floor） |
|------|-----------|------------------------|
| `inverted_cdf` | \(nq-1\) | `gamma == 0` |
| `closest_observation` | \(nq-1-0.5\) | `gamma == 0` 且 `floor(index)` 为奇数 |

#### 4.4.3 源码精读

公共底座：

[_function_base_impl.py:L4620-L4631](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4620-L4631) — `_discrete_interpolation_to_boundaries`。关键设计：`_get_gamma_mask` 默认填 `next`，仅在条件为真处用 `copyto` 覆盖成 `previous`，最后 `.astype(np.intp)` 转整数并把负值钳 0。这样向量化地一次性算出所有分位水平对应的整数下标。

`inverted_cdf`：

[_function_base_impl.py:L4642-L4645](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4642-L4645) — `_inverted_cdf`。条件 `gamma == 0`：仅当虚索引恰为整数时取 floor，否则取 ceil。等价于「向上取整，整数 ties 归下」。

`closest_observation`：

[_function_base_impl.py:L4634-L4639](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4634-L4639) — `_closest_observation`。docstring 指明其语义「choose the nearest even order statistic at g=0」（H&F 1996 p.362）。顺序统计量是 1 基的，所以 0 基下「最近的偶数阶」对应「最近的奇数下标」——条件里 `np.floor(index) % 2 == 1` 正是这个换算。

方法表里它们的 `fix_gamma` 是 `None`（不会被调用）：

[_function_base_impl.py:L96-L111](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L96-L111) — `inverted_cdf` 与 `closest_observation` 的配置，`get_virtual_index` 直接返回整数，`fix_gamma: None` 标记「离散方法，不进入插值路径」。

#### 4.4.4 代码实践

**目标**：手算 `inverted_cdf` 并与 `higher`/`lower` 对比，体会「整数 ties 归下」的规则。

**步骤**：`a = [1,2,3,4]`（\(n=4\)），逐个分析 `q=0.5`：

- `inverted_cdf`：初值 \(nq-1 = 4\times0.5-1 = 1\)，gamma=0 → 取 floor=1 → `a[1]=2`。
- `higher`：`ceil((n-1)*q) = ceil(1.5) = 2` → `a[2]=3`。
- `lower`：`floor((n-1)*q) = floor(1.5) = 1` → `a[1]=2`。

```python
import numpy as np
a = [1, 2, 3, 4]
print("inverted_cdf:", np.quantile(a, 0.5, method="inverted_cdf"))  # 2
print("higher:      ", np.quantile(a, 0.5, method="higher"))        # 3
print("lower:       ", np.quantile(a, 0.5, method="lower"))         # 2
```

**预期结果**：`inverted_cdf=2`、`higher=3`、`lower=2`。

**观察要点**：换 `q=0.6` 再试。此时 `inverted_cdf` 初值 \(4\times0.6-1=1.4\)，gamma=0.4≠0 → 取 ceil=2 → `a[2]=3`，与 `higher` 相同。可见 `inverted_cdf` 的「整数 ties 归下」只在虚索引恰为整数时区别于 `higher`。

#### 4.4.5 小练习与答案

**练习 1**：`_discrete_interpolation_to_boundaries` 最后一行 `res[res < 0] = 0` 为什么必要？

> **答案**：某些方法的虚索引初值可能为负（如 `weibull` 的 `q=0` 给 -1，`closest_observation` 因 -0.5 偏移也可能为负）。`floor` 后 `previous` 可能为 -1，作为数组下标会从末尾取（Python 负索引语义），这会错取到最大值。钳 0 确保负索引一律指向数组第一个元素（最小值）。

**练习 2**：`closest_observation` 为什么要在条件里额外要求 `floor(index) % 2 == 1`？

> **答案**：H&F 第 3 号方法规定「当虚索引恰落在整数位置时，取最近的**偶数阶**顺序统计量」。顺序统计量是 1 基编号，偶数阶 = 0 基奇数下标。所以仅当 gamma=0（落在整数）且该整数下标为奇数时，才「向下取整」取偶数阶；否则维持默认的「向上」。这保证 ties 总是归到偶数阶。

---

### 4.5 插值权重与线性插值：_get_gamma、_lerp 与方法表

#### 4.5.1 概念说明

对**连续方法**和 `midpoint`，虚索引是浮点数，需要做线性插值 \((1-g)\,x_j + g\,x_{j+1}\)。这里有两个关键函数：

- **`_get_gamma`**：算出插值权重 \(g\)。默认就是虚索引的小数部分，但 `fix_gamma` 可以改写它：
  - `midpoint`：当虚索引恰为整数时强制 \(g=0\)（否则 0.5）；
  - `averaged_inverted_cdf`：当 \(g=0\)（落在整数）时强制 \(g=0.5\)（取两邻居平均）；
  - 其余连续方法：恒等，\(g\) 不变。
- **`_lerp`**：执行线性插值。它有两个代数等价的形式，并按 \(g\) 的大小切换以保证**数值稳定**。

至于 `lower`/`higher`/`nearest`，它们的 `get_virtual_index` 直接返回整数（`floor`/`ceil`/`around`），`fix_gamma=None`，根本不进入插值路径——和 4.4 的离散方法走同一条「整数快车道」。

`averaged_inverted_cdf`（H&F 2）是个有趣的混合体：它的虚索引公式和 `inverted_cdf` 一样是 \(nq-1\)（浮点），但当落在整数时不是取该元素，而是取它与下一个邻居的**平均**（\(g=0.5\)），所以它走的是**插值路径**而非整数快车道。

#### 4.5.2 核心流程

**`_get_gamma`**：

```
gamma = virtual_indexes - previous_indexes   # 小数部分
gamma = method["fix_gamma"](gamma, virtual_indexes)   # 方法专属修正
return gamma（保持 dtype）
```

**`_lerp`**（数值稳定的线性插值）：

```
给定左 a、右 b、权重 t：
  diff = b - a
  # 形式 1（t 接近 0 时精确）：result = a + diff*t
  # 形式 2（t 接近 1 时精确）：result = b - diff*(1-t)
  先全用形式 1 填 result
  在 t >= 0.5 处，用 where 改写为形式 2
```

为什么两种形式？因为浮点运算里 `a + (b-a)*t` 当 \(t\to 1\) 时会因 `b-a` 和 `t` 的舍入而丢失精度；改写为 `b - (b-a)*(1-t)` 后，`1-t` 接近 0，相减更稳。两者代数相等，但数值误差分布不同，按 \(t\) 大小择优。

#### 4.5.3 源码精读

`_get_gamma`（注意它强制保留 `virtual_indexes.dtype`）：

[_function_base_impl.py:L4567-L4588](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4567-L4588) — `_get_gamma`。`fix_gamma` 来自方法表，连续方法是恒等，`averaged_inverted_cdf` 与 `midpoint` 各有修正逻辑。末尾 `np.asanyarray(gamma, dtype=virtual_indexes.dtype)` 保证权重与输入 dtype 一致（这对整数输入的弱提升很关键）。

`_get_gamma_mask`——条件填值的工具，被 `fix_gamma` 复用：

[_function_base_impl.py:L4614-L4617](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4614-L4617) — 先 `full` 填默认值，再 `copyto` 在 `where` 处覆盖条件值。`averaged_inverted_cdf` 的 `fix_gamma` 用它把 `gamma==0` 处填 0.5；`midpoint` 用它把「整数索引」处填 0。

`_lerp`——线性插值内核，含数值稳定分支：

[_function_base_impl.py:L4591-L4611](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4591-L4611) — `_lerp`。第一行 `add(a, diff_b_a*t, out=...)` 用形式 1 填满；第二行 `subtract(b, diff_b_a*(1-t), where=t>=0.5, ...)` 在 \(t\ge 0.5\) 处用形式 2 覆盖。末尾把 0 维数组解包成标量。

方法表里 `midpoint`/`averaged_inverted_cdf` 的 `fix_gamma`：

[_function_base_impl.py:L100-L107](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L100-L107) — `averaged_inverted_cdf`：`get_virtual_index` 返回浮点 \(nq-1\)，`fix_gamma` 在 `gamma==0` 处填 0.5。

[_function_base_impl.py:L157-L166](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L157-L166) — `midpoint`：`get_virtual_index` 返回 `0.5*(floor+ceil)`，`fix_gamma` 在「整数索引」处填 0。

`lower`/`higher`/`nearest` 直接返回整数索引：

[_function_base_impl.py:L147-L172](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L147-L172) — `lower`（`floor`）、`higher`（`ceil`）、`midpoint`、`nearest`（`around`）。前三者 `fix_gamma=None`（整数快车道），只有 `midpoint` 需要 `fix_gamma`。

#### 4.5.4 代码实践

**目标**：对比 `linear` 与 `lower` 求 0.25 分位（这是本讲指定的核心实践任务），并体会 `_lerp` 的数值切换。

**步骤**：

```python
import numpy as np
a = np.arange(1, 11)   # [1,2,3,4,5,6,7,8,9,10], n=10

lin   = np.quantile(a, 0.25, method="linear")
low   = np.quantile(a, 0.25, method="lower")
high  = np.quantile(a, 0.25, method="higher")
mid   = np.quantile(a, 0.25, method="midpoint")
near  = np.quantile(a, 0.25, method="nearest")
print(f"linear={lin}, lower={low}, higher={high}, midpoint={mid}, nearest={near}")
```

**手算对照**（\(n=10, q=0.25\)）：

- `linear`：虚索引 \((n-1)q = 9\times0.25 = 2.25\)，\(j=2, g=0.25\) → \(x_2 + 0.25(x_3-x_2) = 3 + 0.25 = 3.25\)。
- `lower`：\(\lfloor 2.25\rfloor = 2\) → `a[2]=3`。
- `higher`：\(\lceil 2.25\rceil = 3\) → `a[3]=4`。
- `midpoint`：\((x_2+x_3)/2 = 3.5\)。
- `nearest`：\(\mathrm{round}(2.25)=2\) → `a[2]=3`。

**预期结果**：`linear=3.25, lower=3, higher=4, midpoint=3.5, nearest=3`。

**观察要点**：`linear`（3.25）是 5 个值里唯一的小数，它落在 `lower`（3）和 `higher`（4）之间，且恰是 0.25:0.75 的加权。这正是 `_lerp(x_2, x_3, 0.25)` 的结果。本例 \(g=0.25 < 0.5\)，所以 `_lerp` 走形式 1 `a + (b-a)*t`。

#### 4.5.5 小练习与答案

**练习 1**：`_lerp` 里 `where=t>=0.5` 这个分支去掉会怎样？结果还对吗？

> **答案**：结果**数学上仍正确**（两种形式代数等价），但在 \(t\) 接近 1 时**数值精度下降**。例如 \(a=0, b=1, t=1-10^{-16}\)，形式 1 `0 + 1*(1-1e-16)` 可能因 `1-1e-16` 舍入为 1 而得 1.0；形式 2 `1 - 1*1e-16` 同样受限于浮点。但对更大的 \(t\)（如 0.9），形式 2 显著更准。这个分支是**精度优化**，不是正确性必需。

**练习 2**：`averaged_inverted_cdf` 与 `inverted_cdf` 在 `q=0.5, a=[1,2,3,4]` 时结果分别是什么？为什么不同？

> **答案**：`inverted_cdf` 初值 \(4\times0.5-1=1\)，gamma=0 → 取 `a[1]=2`（离散，整数快车道）。`averaged_inverted_cdf` 初值同为 1.0，但走插值路径且 `fix_gamma` 把 gamma=0 改成 0.5 → `_lerp(a[1], a[2], 0.5) = (2+3)/2 = 2.5`。差别就在「落在整数时，前者取该元素，后者取它与右邻居的平均」。

---

### 4.6 内核 _quantile：三分支总调度

#### 4.6.1 概念说明

前面三个模块讲了「算索引」「算 gamma」「做插值」三个零件。`_quantile` 是把它们装配起来的总调度器，也是 `_quantile_ureduce_func` 直接调用的最底层函数。它有三个分支：

1. **整数快车道**（`supports_integers`）：当虚索引的 **dtype 是整数**时，**无需插值**，直接 `partition` + `take` 取值。这覆盖两种情况：所有「纯离散方法」（`fix_gamma is None`，`get_virtual_index` 返回整数索引，如 `lower`/`inverted_cdf`），以及 `linear` 在 `q` 本身是整数 dtype 的罕见情形（如 `np.quantile(a, np.array([0,1]))`）。注意：判定看的是**dtype**而非**数值**——`linear` 即便虚索引的值恰为整数（如 `(n-1)*0.5=1.5` 不行，但若算出 `2.0`），只要 `q` 是浮点就**仍走插值路径**（此时 gamma≈0，`_lerp` 退化为取左邻居，结果相同但路径不同）。
2. **插值路径**：连续方法和 `midpoint`/`averaged_inverted_cdf`，虚索引是浮点，要走 `_get_indexes` → `partition` → `_get_gamma` → `_lerp` 全套流程。
3. **加权路径**（`weights is not None`）：只支持 `inverted_cdf`，按权重算经验累积分布函数（CDF），用 `searchsorted` 找分位。本讲只点出它的存在，重点是前两条无权重的路径。

#### 4.6.2 核心流程

无权重路径（本讲重点）：

```
_quantile(arr, q, axis, method, out, weights, weak_q):
  把采样轴 moveaxis 到 0
  method_props = _QuantileMethods[method]            # 查方法表
  virtual_indexes = method_props["get_virtual_index"](n, q)

  if 方法是离散（fix_gamma is None）或 (linear 且虚索引恰为整数):
      ★ 整数快车道
      partition(virtual_indexes)                      # 把所需位置排好
      result = take(arr, virtual_indexes)             # 直接取
  else:
      ★ 插值路径
      prev, next = _get_indexes(arr, virtual_indexes) # 找左右邻居下标（含越界钳制）
      partition(去重后的 prev∪next)                   # 一次性把邻居都排好
      gamma = _get_gamma(virtual_indexes, prev, method_props)
      result = _lerp(arr[prev], arr[next], gamma)
  若有 NaN：用 copyto 把 NaN 写入对应切片
```

`_get_indexes` 负责把浮点虚索引转成「左右邻居的整数下标」，并处理三类边界：索引高于上界（取最大值）、低于下界（取最小值）、含 NaN（虚索引变 NaN 时取末尾）。

#### 4.6.3 源码精读

`_quantile` 的设置与方法表查找：

[_function_base_impl.py:L4744-L4768](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4744-L4768) — `moveaxis` 把采样轴挪到 0；`_QuantileMethods[method]` 查表（查不到抛 `ValueError` 列出所有合法方法名）；`get_virtual_index` 算出虚索引。

判断走哪条路径：

[_function_base_impl.py:L4770-L4789](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4770-L4789) — `supports_integers` 的判定：`fix_gamma is None`（纯离散方法）恒为真；否则仅当 `method=='linear'` 且虚索引确实是整数 dtype 时才为真。为真时走「整数快车道」：对可能含 NaN 的浮点类型追加 `-1` 作为哨兵参与 `partition`（把 NaN 顶到末尾），再 `take` 取值。

插值路径：

[_function_base_impl.py:L4790-L4819](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4790-L4819) — `_get_indexes` 找左右邻居；`partition` 一次性把所有用到的邻居下标（含 `0` 和 `-1`）排好；`_get_gamma` 算权重（注意 `weak_q` 为真时把 gamma 降为 Python float，否则 reshape 到正确广播形状）；最后 `_lerp` 出结果。

邻居下标与边界钳制（`_get_indexes`）：

[_function_base_impl.py:L4688-L4720](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4688-L4720) — `previous=floor`、`next=previous+1`；虚索引高于上界时把两者都设 -1（取最大值）；低于 0 时都设 0（取最小值）；浮点类型且虚索引为 NaN 时都设 -1。

加权路径（`inverted_cdf` + weights）：

[_function_base_impl.py:L4820-L4904](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4820-L4904) — 加权分支。按值排序后用 `weights.cumsum` 构造经验 CDF，归一化到 1，再用 `searchsorted` 在 CDF 上找满足 \(F(i-1)<q\le F(i)\) 的下标。注意 L4864–L4865 把 CDF 转成与 `quantiles` 同 dtype，规避 0.4 这类不可精确表示的二进制浮点导致的 `searchsorted` 误判；L4871–L4872 把 CDF 前导零改成 -1，保证 `q=0` 时跳过零权重元素。

NaN 回填：

[_function_base_impl.py:L4906-L4912](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4906-L4912) — 若有切片含 NaN（`slices_having_nans`），把结果对应位置用 `copyto` 覆盖成 `arr[-1]`（即 NaN，因 NaN 排序后落在末尾）。

#### 4.6.4 代码实践

**目标**：跟踪一条完整调用链，验证「整数快车道」与「插值路径」的分支选择，并厘清一个易错点：判定看的是虚索引的 **dtype**，不是数值。

**步骤**：

```python
import numpy as np
a = np.array([1, 2, 3, 4])   # n=4

# 1) lower → 纯离散方法（fix_gamma=None），整数快车道
print("lower q=0.5:        ", np.quantile(a, 0.5, method="lower"))      # a[1]=2

# 2) linear q=0.5 → q 是 float，虚索引 1.5 浮点 → 插值路径
print("linear q=0.5:       ", np.quantile(a, 0.5, method="linear"))     # (2+3)/2=2.5

# 3) linear 但 q 是整数 dtype → 虚索引 (n-1)*q 为整数 dtype → 整数快车道
print("linear q=int[0,1]:  ", np.quantile(a, np.array([0, 1]), method="linear"))  # [1, 4]
```

**手算**：
- `lower q=0.5`：虚索引 `floor((4-1)*0.5)=floor(1.5)=1`，且 `lower` 的 `fix_gamma=None` → 走快车道 → `a[1]=2`。
- `linear q=0.5`：虚索引 `(4-1)*0.5=1.5`，`q` 是 `float` → 虚索引 dtype 为 float → `int_virtual_indices=False` → **走插值路径** → `_lerp(a[1], a[2], 0.5)=(2+3)/2=2.5`。
- `linear q=int[0,1]`：`q=np.array([0,1])` 是 `int64` → 虚索引 `3*[0,1]=[0,3]` 也是 `int64` → `int_virtual_indices=True` → 走快车道 → `[a[0], a[3]]=[1,4]`。

**预期结果**：`2`、`2.5`、`[1 4]`。

**观察要点**：关键易错点是第 2、3 个对比——同样是 `method="linear"`，第 2 个走插值路径、第 3 个走快车道，差别**只在 `q` 的 dtype**（float vs int），而非虚索引的数值。这正是 `supports_integers = method == 'linear' and int_virtual_indices`（[L4775](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4775)）的精确语义。注意第 2 个例子即便虚索引值 `1.5` 不是整数也无所谓——真正决定路径的是 dtype。

> **源码阅读型实践**：在 `_quantile` 的 [L4777](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4777) 与 [L4790](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4790) 各加一行 `print(f"branch: {'fast' if supports_integers else 'interp'}")`（仅用于学习，勿提交），重跑上面三个例子，可直接看到分支切换：`fast`、`interp`、`fast`。**待本地验证**。

#### 4.6.5 小练习与答案

**练习 1**：为什么整数快车道里，对浮点类型输入要 `concatenate((virtual_indexes.ravel(), [-1]))` 多塞一个 -1 参与分区？

> **答案**：浮点数组可能含 NaN。`partition` 会把 NaN 排到末尾，但若不显式把 `-1`（末尾位置）纳入分区点，末尾的 NaN 不保证被定位，后续 `slices_having_nans = np.isnan(arr[-1, ...])` 就检测不到。塞入 -1 强制把最后一个位置也排好，使 NaN 检测可靠。

**练习 2**：加权路径里为什么要把 CDF 的前导零改成 -1（[L4871-L4872](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4871-L4872)）？

> **答案**：当权重数组开头有零权重时，经验 CDF 会以一串 0 开头。`searchsorted(cdf, 0, side="left")` 会命中第一个 0 的位置，但 `q=0` 的分位应当是「第一个权重 > 0 的元素」，而非任意一个零权重元素。把 CDF 中的 0 改成 -1，使 `searchsorted` 跳过它们，落到第一个正值。

---

## 5. 综合实践

**任务**：用一张表把同一组数据在所有 13 种 `method` 下的 0.25 分位结果列出来，并按结果分类，验证你对虚索引公式的理解。

**数据**：`a = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])`（\(n=10\)），\(q=0.25\)。

**步骤**：

```python
import numpy as np
a = np.arange(1, 11)
methods = [
    # 连续方法（H&F 4–9）
    "interpolated_inverted_cdf", "hazen", "weibull",
    "linear", "median_unbiased", "normal_unbiased",
    # 离散 H&F 方法（1–3）
    "inverted_cdf", "averaged_inverted_cdf", "closest_observation",
    # numpy 向后兼容离散变体
    "lower", "higher", "midpoint", "nearest",
]
for m in methods:
    v = np.quantile(a, 0.25, method=m)
    print(f"{m:28s} -> {v}")
```

**预期结果（手算校验）**（虚索引初值以 `linear` 系 \((n-1)q=2.25\) 或各方法公式为准）：

| 方法 | 0.25 分位 | 路径 |
|------|----------|------|
| `linear` | 3.25 | 插值 |
| `lower` | 3 | 整数 |
| `higher` | 4 | 整数 |
| `midpoint` | 3.5 | 插值 |
| `nearest` | 3 | 整数 |

> 其余方法的结果**待本地运行确认**。重点不是记数字，而是对照公式：对每个连续方法，先用 \(\alpha,\beta\) 算虚索引 \(h\)，再算 \(j=\lfloor h\rfloor\)、\(g=h-j\)，最后 `_lerp(a[j], a[j+1], g)` 验证；对每个离散方法，确认它挑了哪个整数下标。

**进阶**：把 `q` 换成数组 `np.linspace(0, 1, 101)`，对每个方法画一条「分位水平 → 估计值」的曲线（这正是 `percentile` docstring 里 [L4195–L4227](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4195-L4227) 那张图）。连续方法会得到平滑折线，离散方法会得到阶梯线——直观体现「连续 vs 离散」的本质区别。

**关联测试**：参考 [`tests/test_function_base.py` 的 `TestQuantile`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_function_base.py#L3984)。其中 [`test_quantile_monotonic`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_function_base.py#L4081) 验证「对所有方法，有序的 q 必产生单调不减的输出」——这是所有插值方法必须满足的不变量；[`test_quantile_preserve_int_type`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_function_base.py#L4068) 验证离散方法对整数输入保持整数 dtype（正是 `weak_q` 的作用）。

## 6. 本讲小结

- `percentile` 与 `quantile` 是**纯参数规整入口**，唯一差别是 `q/100` 那一步除法；二者都把活儿转交给 `_quantile_unchecked`，后者再包进 `_ureduce`（处理 `axis`/`keepdims`/`out`）。
- 核心抽象是**虚索引 \(h\)**：连续方法用 alpha-beta 公式 \(h=q(n+1-\alpha-\beta)+\alpha-1\) 算浮点位置，离散方法直接算成整数下标。
- 13 种 `method` 全部配置在 `_QuantileMethods` 表里，每个方法有两个插槽：`get_virtual_index`（算索引）和 `fix_gamma`（修正插值权重，离散方法为 `None`）。
- `_inverted_cdf` 与 `_closest_observation` 是真正的离散 H&F 方法，共享 `_discrete_interpolation_to_boundaries` 底座，靠「条件函数」决定 ties 归上还是归下。
- `_lerp` 用两种代数等价的插值形式并按 \(t\ge 0.5\) 切换，是**数值稳定性**设计，而非正确性所需。
- `_quantile` 内核有三条路径：整数快车道（最快，直接 `partition`+`take`）、插值路径（`_get_indexes`→`partition`→`_get_gamma`→`_lerp`）、加权路径（经验 CDF + `searchsorted`，仅 `inverted_cdf`）。

## 7. 下一步学习建议

- **横向对比**：回头看 [u7-l1](u7-l1-ureduce-median-cov.md) 的 `median`，你会发现它其实是 `quantile(a, 0.5)` 的特例——但 `median` 用 `partition` 单点分区，而 `quantile` 要同时算多个分位水平，分区点更多。对比二者的 `partition` 调用能加深理解。
- **纵向延伸到 NaN**：[u9-l2](u9-l2-nan-mean-var-quantile.md) 的 `nanpercentile`/`nanquantile` 会复用本讲的 `_quantile_unchecked`，只是在调用前用 `_remove_nan_1d` 等工具剔除 NaN。理解了本讲的 `_get_indexes` 对 NaN 的钳制（L4712–L4717），就能预见 `nan*` 版本的行为。
- **直方图与分箱**：[u7-l3](u7-l3-histogram-binning.md) 的 `histogram` 在概念上是分位数的「对偶」——分位数问「这个概率对应什么值」，直方图问「这些值落在哪些桶」。两者都依赖排序/分区。
- **读 H&F 原论文**：`_compute_virtual_index` 的 docstring 给了 DOI（10.1080/00031305.1996.10473566）。对照论文的 9 种估计量与 `_QuantileMethods` 表，能彻底厘清每个 `method` 的统计含义。
- **加权分位数**：若关心加权场景，精读 `_quantile` 的 [L4820–L4904](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4820-L4904)，重点理解经验 CDF 的构造与 `searchsorted` 的两个浮点陷阱（dtype 对齐、前导零改 -1）。

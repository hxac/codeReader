# 归约、统计与排序

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `MaskedArray` 的 `sum`/`mean`/`var`/`std`/`cumsum`/`cumprod` 是如何「跳过被屏蔽元素」的，并理解它们之间的关键差别。
- 解释 `min`/`max`/`argmin`/`argmax`/`ptp` 为什么必须借助「极值填充值」而不是 0/1。
- 掌握 `sort`/`argsort` 中 `endwith` 与 `fill_value` 参数如何决定被屏蔽元素的落点。
- 会用 `numpy.ma.extras` 中的 `average`/`median`/`cov`/`corrcoef` 做带权平均与协方差/相关分析。

本讲承接 u2-l3（填充值系统）建立的 `minimum_fill_value` / `maximum_fill_value` 概念，并把 u1-l4、u2-l1 中的 `filled` / `nomask` / `mask.all` 等工具串成一条完整的「掩码归约」主线。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：归约的本质是「填充 + 普通 ndarray 运算 + 重算掩码」。**
`MaskedArray` 是 `ndarray` 的子类，但它并不重写底层的 C 求和循环。它的做法是：先用一个「不改变结果的值」把被屏蔽的位置填掉，得到一个普通 `ndarray`，调用普通 `ndarray.sum()`，最后再根据原始掩码算出结果的掩码。所以你会反复看到 `self.filled(某值).sum(...)` 这样的写法。

**直觉二：「不改变结果的填充值」取决于运算。**
这是本讲最核心的一句话：

| 运算 | 应填的值 | 为什么 |
|------|----------|--------|
| `sum` | `0` | \( x + 0 = x \)，0 是加法幺元 |
| `prod` | `1` | \( x \times 1 = x \)，1 是乘法幺元 |
| `min` | dtype 的最大值 | 被屏蔽值填成「最大」，就不会赢得最小值 |
| `max` | dtype 的最小值 | 被屏蔽值填成「最小」，就不会赢得最大值 |

注意 `mean` 不能填 0——那会拉低分母。它的做法是「先 `sum` 再除以真实计数 `count`」。

**直觉三：结果掩码来自 `mask.all(axis)`，而不是 `mask.any(axis)`。**
归约是把一整条轴「压扁」。一条轴上有 5 个元素、其中 3 个被屏蔽、2 个有效，那么 `sum` 当然能算出这 2 个有效元素的和——结果**不应该**被屏蔽。只有当一条轴上**全部**元素都被屏蔽时，结果才无意义、才被屏蔽。所以归约结果掩码用的是 `mask.all(axis)`（全屏蔽才屏蔽），而不是 `mask.any(axis)`。

> 名词复习（来自前序讲义）：`nomask` 是表示「无屏蔽」的省内存单例（即 `False`）；`masked` 是表示「单个被屏蔽标量」的全局单例；`filled(v)` 把被屏蔽位替换成 `v` 并返回普通 `ndarray`；`minimum_fill_value(a)` 返回 `a` 的 dtype 能表示的**最大**值（用于 `min` 归约），`maximum_fill_value(a)` 返回**最小**值（用于 `max` 归约）——名字说的是「给哪种归约用」，不是值的大小。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `numpy/ma/core.py` | `MaskedArray` 的所有归约/极值/排序方法都在这里，以及模块级函数 `sum`/`mean`/`min`/`max`/`ptp` 等（多数由 `_frommethod` 工厂生成） |
| `numpy/ma/extras.py` | 依赖 core 的上层统计工具：`average`（带权平均）、`median`（中位数）、`cov`/`corrcoef`（协方差/相关系数） |

本讲涉及的关键源码点：

- `_check_mask_axis`（core.py）——归约结果掩码的计算器
- `count`（core.py）——非屏蔽元素计数，`mean`/`var` 的分母来源
- `sum` / `cumsum` / `prod` / `cumprod` / `mean` / `var` / `std`（core.py）——加法/乘法族归约
- `min` / `max` / `argmin` / `argmax` / `ptp`（core.py）——极值族归约
- `sort` / `argsort`（core.py）——排序
- `_frommethod`（core.py）——把方法变成模块级函数的工厂
- `average` / `median` / `cov` / `corrcoef` / `_covhelper`（extras.py）——extras 统计工具

## 4. 核心概念与源码讲解

### 4.1 加法/乘法族归约：sum、mean、var、std、cumsum、cumprod

#### 4.1.1 概念说明

这一族归约要回答的问题是：给定一条轴上有有效值也有被屏蔽值，怎么求和、求均值、求方差？

关键在于区分两种归约：

- **「点归约」**（`sum`/`prod`/`mean`/`var`/`std`）：把整条轴压成一个数。被屏蔽元素当作「不存在」，用幺元（0 或 1）填掉后参与普通运算，结果掩码 = 该轴是否全屏蔽。
- **「累积归约」**（`cumsum`/`cumprod`）：不压扁轴，而是沿轴累积，输出形状与输入相同。此时被屏蔽元素的**位置**必须保留——结果在原屏蔽位置仍然屏蔽。

一个微妙但重要的差别：`mean` 不能用「填 0 再除以元素总数」的套路（那会把分母算大、把均值算小）。它的正确做法是 `sum(有效元素) / count(有效元素)`，分母用的是**真实非屏蔽计数**。

#### 4.1.2 核心流程

**`sum` 的流程**（点归约的代表）：

```
1. newmask = _check_mask_axis(self._mask, axis)   # = mask.all(axis)：全屏蔽才屏蔽
2. result  = self.filled(0).sum(axis, dtype)       # 填 0 后调普通 ndarray.sum
3. 若 result 是数组：view 成 MaskedArray，把 newmask 贴上去
   若 result 是标量且 newmask 为真：返回 masked 单例
```

**`mean` 的流程**（注意分母）：

```
1. 若 nomask：直接走 ndarray.mean（无屏蔽，最快路径）
2. 否则：
   dsum = self.sum(axis, dtype)        # 复用上面的掩码 sum（填 0）
   cnt  = self.count(axis)             # 真实非屏蔽计数
   若 cnt==0（标量情形）：返回 masked
   否则：result = dsum * 1. / cnt
```

**`cumsum` 的流程**（累积归约的代表，位置必须保留）：

```
1. result = self.filled(0).cumsum(axis)   # 内部填 0 参与累加
2. result.__setmask__(self._mask)         # 把原始掩码原封不动贴回！
```

第 2 步是累积归约与点归约的根本区别：点归约重算 `mask.all`，累积归约直接复用原始掩码——所以累加结果在原本被屏蔽的位置依旧是 `--`。

**`var` 的流程**（演示计数与 ddof 的配合）：

方差定义为

\[
\mathrm{Var}(X)=\frac{1}{N-\mathrm{ddof}}\sum_{i}(x_i-\bar{x})^{2}
\]

带掩码时 \(N\) 用非屏蔽计数，求和项用掩码 `sum`：

```
cnt   = self.count(axis) - ddof
danom = self - self.mean(axis, keepdims=True)   # 离差（掩码减法）
danom = danom * danom                            # 平方（复数取 |.|^2）
dvar  = danom.sum(axis) / cnt
dvar._mask = mask_or(self._mask.all(axis), cnt <= 0)   # 全屏蔽 或 数据不足 都屏蔽
```

#### 4.1.3 源码精读

先看归约结果掩码的「计算器」`_check_mask_axis`，它就是一句 `mask.all(axis)`：

[core.py:1873-1878](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1873-L1878) —— `nomask` 时直接返回 `nomask`（省一次分配），否则返回「沿轴是否全屏蔽」。这是 `sum`/`min`/`max` 等点归约结果掩码的统一来源。

再看 `count`，它是 `mean`/`var` 的分母：

[core.py:4594-4688](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4594-L4688) —— 无掩码时返回该轴的元素个数（`self.size` 或逐轴乘积），有掩码时返回 `(~m).sum(axis)`，即「非屏蔽位」的个数。

`sum` 的实现印证「填 0 + 普通 sum + 贴 all 掩码」三步：

[core.py:5198-5259](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5198-L5259) —— 关键两行是 `newmask = _check_mask_axis(_mask, axis, ...)` 与 `result = self.filled(0).sum(axis, dtype=dtype, ...)`。`prod`（[core.py:5303-5343](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5303-L5343)）结构完全相同，只是把 `0` 换成 `1`。

`mean` 展示了「分母用真实计数」的细节：

[core.py:5376-5430](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5376-L5430) —— `nomask` 时走 `super().mean(...)` 快速路径；否则 `dsum = self.sum(...)`、`cnt = self.count(...)`，当 `cnt == 0` 时返回 `masked`。整数/布尔输入默认提升到 `f8`、`float16` 提升到 `f4` 后再转回，避免精度损失。

`cumsum` 与点归约的关键差别就在最后一行：

[core.py:5261-5301](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5261-L5301) —— 注意它用 `result.__setmask__(self._mask)` 把**原始掩码**贴回，而不是 `mask.all`。所以累加结果在被屏蔽位置仍是 `--`。`cumprod`（[core.py:5345-5374](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5345-L5374)）同理，内部填 1。

`var` 把计数、离差、平方、掩码合并串起来：

[core.py:5470-5543](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5470-L5543) —— 重点看 `cnt = self.count(axis) - ddof`、`danom = self - self.mean(...)`，以及 `dvar._mask = mask_or(self._mask.all(axis), (cnt <= 0))`：全屏蔽或样本数不足（`cnt <= 0`）都会被屏蔽。`std`（[core.py:5546-5568](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5546-L5568)）就是对 `var` 取 `sqrt`。

最后，这些方法都通过 `_frommethod` 工厂暴露成模块级函数 `ma.sum` / `ma.mean` / `ma.var` / `ma.std`：

[core.py:7119-7123](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7119-L7123) —— `sum = _frommethod('sum')` 等。`_frommethod`（[core.py:7053-7092](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7053-L7092)）的作用是：把第一个参数名从 `self` 改写成 `a`，然后 `getattr(asanyarray(a), methodname)(...)`。所以 `ma.sum(x)` 等价于 `asanyarray(x).sum()`，调用最终都落回上面的 `MaskedArray.sum`。

#### 4.1.4 代码实践

**实践目标**：亲手验证「填 0 + count 分母」如何让 `mean` 跳过屏蔽值，并对比 `sum` 与 `cumsum` 对屏蔽位的处理差异。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

a = ma.array([10.0, 20.0, 30.0, 40.0], mask=[0, 1, 0, 0])
print("data      :", a)                  # [10.0, --, 30.0, 40.0]
print("sum       :", a.sum())            # 80.0  (= 10+30+40，屏蔽位填 0)
print("count     :", a.count())          # 3
print("mean      :", a.mean())           # 26.666... (= 80/3，不是 80/4)
print("手算 mean :", a.sum() / a.count())  # 与上一行一致
print("var       :", a.var())            # 离差平方和 / (3-0)

c = ma.arange(6.0)
c[3] = ma.masked
print("cumsum    :", c.cumsum())         # [0, 1, 3, --, 7, 12]：屏蔽位仍是 --，但累加照常
```

**需要观察的现象**：

1. `a.sum()` 是 `80.0` 而不是 `100.0`——屏蔽的 `20.0` 被当成 0，没有进入求和。
2. `a.mean()` ≈ `26.67`（分母是 3），证明 `mean` 用的是 `count` 而非元素总数 4。
3. `c.cumsum()` 中索引 3 的位置显示 `--`（掩码保留），但索引 4 的值是 `7.0`（= 0+1+2+0+4，屏蔽位内部按 0 累加），说明累积归约「内部填 0、外部保留掩码」。

**预期结果**：上述注释中给出的数值。如果你环境里 `a.mean()` 出现 `26.666666666666668` 之类浮点表示，属正常。

#### 4.1.5 小练习与答案

**练习 1**：若把 `a` 改成 `ma.array([10.0, 20.0, 30.0, 40.0], mask=[1,1,1,1])`（全屏蔽），`a.sum()` 和 `a.mean()` 分别返回什么？为什么？

**答案**：`a.sum()` 返回 `masked` 单例（标量结果且 `newmask` 为真，见 [core.py:5249-5250](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5249-L5250)）；`a.mean()` 也返回 `masked`，因为 `cnt == 0`（见 [core.py:5416-5417](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5416-L5417)）。

**练习 2**：为什么 `prod` 内部填的是 `1` 而不是 `0`？

**答案**：因为 \( x \times 1 = x \)，1 是乘法幺元，填 1 不改变乘积；若填 0，整个乘积会变成 0，结果错误。见 [core.py:5327](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5327)。

---

### 4.2 极值族归约：min、max、argmin、argmax、ptp 与填充值

#### 4.2.1 概念说明

极值归约不能用 0/1 当填充值——那会污染结果（比如求最小值时，把屏蔽位填成 0 可能让 0 错误地成为最小值）。正确做法是**用 dtype 的极端值填充，让被屏蔽元素注定「输掉」**：

- 求 `min`：把屏蔽位填成 dtype 的**最大**值 → 它永远不会是最小。
- 求 `max`：把屏蔽位填成 dtype 的**最小**值 → 它永远不会是最大。

这正好对应 u2-l3 讲过的两个函数：`minimum_fill_value(a)` 返回的是 dtype 最大值（给 `min` 用），`maximum_fill_value(a)` 返回的是 dtype 最小值（给 `max` 用）。浮点型分别是 `+inf` 和 `-inf`。

`argmin`/`argmax` 返回的是**下标**而非值，所以结果没有掩码（下标永远是合法整数）。但填充值同样决定了「被屏蔽元素能不能赢得极值」——默认填充极端值，保证它们不赢。

`ptp`（peak-to-peak，峰峰值）= `max - min`。

#### 4.2.2 核心流程

**`min`/`max` 的流程**（以 `min` 为例）：

```
1. newmask = _check_mask_axis(self._mask, axis)      # 全屏蔽才屏蔽
2. fill_value = minimum_fill_value(self)             # dtype 最大值
3. result = self.filled(fill_value).min(axis).view(type(self))
4. 若 result 是数组：
       result.__setmask__(newmask)
       np.copyto(result, result.fill_value, where=newmask)   # 清掉屏蔽位的 inf
   若 result 是标量且 newmask 为真：返回 masked
```

第 4 步的 `copyto` 很关键：屏蔽位被填成了 `+inf`（浮点情形），在「整条轴全屏蔽」的结果位置上，这个 `inf` 是垃圾值，需要用数组自身的 `fill_value` 覆盖掉，否则打印出来会是 `inf` 而不是 `--` 对应的填充值。

**`argmin`/`argmax` 的流程**：

```
1. fill_value = minimum_fill_value(self)   # argmin；argmax 用 maximum_fill_value
2. d = self.filled(fill_value).view(ndarray)   # 退回普通 ndarray
3. return d.argmin(axis)                    # 直接用 ndarray 的 argmin，返回下标数组
```

**`ptp` 的流程**：`result = self.max(...) - self.min(...)`，复用上面的掩码 `max`/`min`。

#### 4.2.3 源码精读

`min` 的实现完整展示了「极端填充 + 重算掩码 + 清 inf」三步：

[core.py:5875-5971](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5875-L5971) —— 重点看 `fill_value = minimum_fill_value(self)`、`self.filled(fill_value).min(...)`，以及 `np.copyto(result, result.fill_value, where=newmask)`。`max`（[core.py:5973-6077](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5973-L6077)）结构镜像对称，只是换成 `maximum_fill_value`。

`argmin` 简单得多——填充后退回普通 `ndarray` 直接取下标：

[core.py:5694-5740](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5694-L5740) —— `d = self.filled(fill_value).view(ndarray)` 后 `d.argmin(axis)`。注意 `view(ndarray)` 把 `MaskedArray` 退化为普通 `ndarray`，丢弃掩码，因为下标结果不需要掩码。`argmax`（[core.py:5742-5780](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5742-L5780)）同理。

`ptp` 就是 `max - min`：

[core.py:6079-6166](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6079-L6166) —— `result = self.max(...) ; result -= self.min(...)`。注意文档里的警告：`ptp` 保留 dtype，所以 `int8` 的峰峰值超过 `127` 会溢出成负数。

模块级 `ma.min`/`ma.max`/`ma.ptp` 用 try/except 兜底，比 `_frommethod` 多了一层「对象没有 `fill_value` 参数就先 `asanyarray`」的容错：

[core.py:7005-7045](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7005-L7045) —— 这样 `ma.min([3,1,2])`（传入普通列表）也能工作。

#### 4.2.4 代码实践

**实践目标**：验证极端填充值如何让被屏蔽元素「输掉」极值，并理解 `argmin` 的 `fill_value` 参数对结果的影响。

**操作步骤**：

```python
import numpy.ma as ma

x = ma.array([[1.0, -2.0, 3.0],
              [0.2, -0.7, 0.1]],
             mask=[[1, 1, 0],
                   [0, 0, 1]])
print("min 全局 :", ma.min(x))            # -0.7（屏蔽的 1,-2,0.1 都不参与）
print("min axis=0:", x.min(axis=0))       # [0.2, -0.7, 3.0]
print("argmin axis=0:", x.argmin(axis=0)) # [1, 1, 0]

# 演示 fill_value 对 argmin 的控制
y = ma.arange(4).reshape(2, 2)
y[0, :] = ma.masked                      # 第一行全屏蔽
print("argmin fill=-1:", y.argmin(axis=0, fill_value=-1))  # [0, 0]：屏蔽位填 -1 后赢了最小
print("argmin fill=9 :", y.argmin(axis=0, fill_value=9))    # [1, 1]：屏蔽位填 9 后输了
```

**需要观察的现象**：

1. `ma.min(x)` 是 `-0.7`，屏蔽的 `1.0`、`-2.0`、`0.1` 都没有参与——它们被填成 `+inf`（浮点最大），不可能赢得最小值。
2. `x.min(axis=0)` 第 0 列只有 `0.2` 有效（`1.0` 被屏蔽），所以结果是 `0.2`；该位置结果**不**屏蔽，因为并非全屏蔽。
3. `argmin` 的 `fill_value` 直接改变下标结果：填 `-1` 时屏蔽位变成最小、赢得 `argmin`（返回行号 0）；填 `9` 时屏蔽位输掉（返回行号 1）。这正是 [core.py:5730-5733](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5730-L5733) 文档示例的行为。

**预期结果**：上述注释数值。若不确定浮点 `minimum_fill_value` 的具体值，可在实践里加一行 `print(ma.minimum_fill_value(x))` 应看到 `inf`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `min` 在「整条轴全屏蔽」时要用 `copyto` 把结果位覆盖成 `fill_value`？

**答案**：因为全屏蔽位被填成了 `+inf`（浮点的 `minimum_fill_value`）。如果该位置最终要被屏蔽，其 `.data` 里的 `inf` 是垃圾值；用数组自身 `fill_value` 覆盖后，`filled()` 或打印时才表现出一致的缺失语义。见 [core.py:5953-5954](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5953-L5954)。

**练习 2**：`ma.argmax` 默认用 `maximum_fill_value`（dtype 最小值）填充屏蔽位。对一个整条轴除一个元素外全屏蔽的数组调 `argmax`，结果下标会指向哪个位置？

**答案**：指向那个唯一未屏蔽元素的位置——因为所有屏蔽位被填成最小值，唯一的有效值最大、赢得 `argmax`。见 [core.py:5776-5780](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5776-L5780)。

---

### 4.3 排序：sort、argsort 与 endwith

#### 4.3.1 概念说明

排序必须回答一个新问题：被屏蔽元素该排在**最前面**还是**最后面**？这由 `endwith` 参数决定：

- `endwith=True`（默认）：被屏蔽元素当作「最大」，排在末尾。
- `endwith=False`：被屏蔽元素当作「最小」，排在开头。

实现思路与极值归约一脉相承——**把屏蔽位填成一个值，再调普通 `ndarray` 排序**。`endwith` 只是决定了「填什么值」：

- `endwith=True`（排末尾）：浮点填 `nan`（在 NumPy 排序里 `nan` 比任何值都大，连 `inf` 都排在它前面，源码注释写作 `nan > inf`）；非浮点填 `minimum_fill_value`（dtype 最大值）。
- `endwith=False`（排开头）：填 `maximum_fill_value`（dtype 最小值）。

可以用 `fill_value` 参数显式覆盖，此时 `endwith` 失效（见文档「If `fill_value` is not None, it supersedes `endwith`」）。

另一个要点：掩码版排序**保留被屏蔽元素的掩码标记**——屏蔽元素只是被挪到了首/尾，它依然是 `--`，而不是变成了填充值。这与「填值后调 ndarray.sort」的朴素想象不同，需要特别留意源码怎么做到的。

#### 4.3.2 核心流程

**`argsort` 的流程**（返回排序下标）：

```
1. 拒绝 stable / descending（掩码数组不支持）
2. 根据 endwith 选 fill_value：
     endwith=True  -> 浮点 nan / 非浮点 minimum_fill_value
     endwith=False -> maximum_fill_value
3. filled = self.filled(fill_value)
4. return filled.argsort(axis, kind, order)    # 普通 ndarray.argsort，返回下标
```

**`sort` 的流程**（原地排序，关键在于「下标搬运 data 与 mask 一起走」）：

```
1. 若 nomask：直接 ndarray.sort（快速路径）
2. 若 self is masked（单例）：直接 return
3. sidx = self.argsort(axis, ..., endwith, fill_value)   # 复用上面的 argsort
4. self[...] = np.take_along_axis(self, sidx, axis)       # 沿 sidx 重排
```

第 4 步是精髓：`self[...] = ...` 走的是 `MaskedArray.__setitem__`（见 u2-l6），它对 `_data` 和 `_mask` **同步**赋值。而 `np.take_along_axis(self, sidx, axis)` 会按 `sidx` 同时重排 data 与 mask（因为 `self` 是带掩码的）。两者合起来，被屏蔽元素带着它的掩码标记一起被搬到首/尾——所以排序后屏蔽元素仍是 `--`。

#### 4.3.3 源码精读

`argsort` 中 `endwith` 与 `fill_value` 的取值逻辑：

[core.py:5607-5692](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5607-L5692) —— 重点看 [core.py:5681-5692](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5681-L5692)：`endwith` 为真时浮点取 `np.nan`、否则取 `minimum_fill_value`；`endwith` 为假时取 `maximum_fill_value`；最后 `filled.argsort(...)`。注意 `stable`/`descending` 会直接抛 `ValueError`，掩码数组不支持这两个参数。

`sort` 的原地重排：

[core.py:5782-5873](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5782-L5873) —— `nomask` 时走 `ndarray.sort` 快速路径（[core.py:5863-5865](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5863-L5865)）；否则 `sidx = self.argsort(...)` 再 `self[...] = np.take_along_axis(self, sidx, axis=axis)`（[core.py:5870-5873](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5870-L5873)）。文档示例（[core.py:5826-5850](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5826-L5850)）清楚展示了 `endwith=True/False` 与显式 `fill_value` 三种情形下屏蔽元素的落点。

#### 4.3.4 代码实践

**实践目标**：切换 `endwith`，观察被屏蔽元素在首尾的落点差异，并验证排序后屏蔽标记是否保留。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

a = ma.array([1, 2, 5, 4, 3], mask=[0, 1, 0, 1, 0])

# argsort：返回下标，屏蔽位默认(endwith=True)填最大值 -> 排末尾
print("argsort 默认    :", a.argsort())            # 屏蔽元素的下标在末尾

# sort 原地排序，对比 endwith
s1 = ma.array([1, 2, 5, 4, 3], mask=[0, 1, 0, 1, 0])
s1.sort()                                          # endwith=True
print("sort endwith=True :", s1)                   # [1, 3, 5, --, --]

s2 = ma.array([1, 2, 5, 4, 3], mask=[0, 1, 0, 1, 0])
s2.sort(endwith=False)
print("sort endwith=False:", s2)                   # [--, --, 1, 3, 5]

# 显式 fill_value 会覆盖 endwith
s3 = ma.array([1, 2, 5, 4, 3], mask=[0, 1, 0, 1, 0])
s3.sort(endwith=False, fill_value=3)
print("sort fill_value=3 :", s3)                   # [1, --, --, 3, 5]
```

**需要观察的现象**：

1. `endwith=True` 时，两个屏蔽元素（原下标 1、3）被搬到数组末尾，且仍是 `--`（掩码保留），有效值 `[1,3,5]` 升序在前。
2. `endwith=False` 时，屏蔽元素搬到开头：`[--, --, 1, 3, 5]`。
3. `fill_value=3` 让屏蔽位按值 3 参与排序，于是排在 1 之后、5 之前（与 3 相等的位置顺序未定义，故两个 `--` 落在 `3` 附近），印证「`fill_value` 覆盖 `endwith`」。

**预期结果**：与注释一致。`argsort` 返回的下标顺序取决于排序算法（`kind`），但屏蔽元素的下标一定落在末尾（`endwith=True` 时）。

#### 4.3.5 小练习与答案

**练习 1**：为什么浮点型 `argsort` 在 `endwith=True` 时填 `nan` 而不是 `minimum_fill_value`（`+inf`）？

**答案**：因为 NumPy 的排序约定 `nan` 比任何值都大（包括 `inf`，源码注释 `nan > inf`）。如果填 `inf`，那么数组里真实存在的 `inf` 会与填充值平局，屏蔽元素的相对位置就不可控；填 `nan` 能保证屏蔽元素严格排在所有有限值乃至 `inf` 之后。见 [core.py:5682-5689](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5682-L5689)。

**练习 2**：`sort` 为什么在 `nomask` 时单独走 `ndarray.sort` 快速路径，而不统一走 `argsort + take_along_axis`？

**答案**：没有屏蔽位时无需选填充值、无需重排掩码，直接 `ndarray.sort` 一次原地排序最快、最省内存；`argsort + take_along_axis` 多一次下标计算和一次搬运，仅在需要处理掩码时才值得。见 [core.py:5863-5865](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5863-L5865)。

---

### 4.4 extras 统计：average、median、cov、corrcoef

#### 4.4.1 概念说明

`numpy.ma.extras` 在 core 之上提供更「统计味」的工具。它们都遵循同一条原则：**被屏蔽元素不参与计算**，但每个函数的处理细节不同。

- **`average`**：带权平均。`weights=None` 时退化为 `mean`；有权重时，被屏蔽数据点的权重也被置 0（`wgt = wgt * (~a.mask)`），所以屏蔽点对分子分母都不贡献。公式：
  \[
  \mathrm{avg}=\frac{\sum_i a_i w_i}{\sum_i w_i}
  \]
  其中 \(i\) 只遍历非屏蔽元素。

- **`median`**：中位数。先把屏蔽元素填到末尾（`fill_value=inf`，等价 `endwith=True`）再排序，然后按**非屏蔽计数** `count` 取中间元素。这样中位数只反映有效数据。

- **`cov`**：协方差矩阵。把数据中心化（减去均值）后做矩阵内积，除以 \(N-\mathrm{ddof}\)。`allow_masked=True`（默认）时，`x` 和 `y` 的掩码会**逐对合并**——`x[i,j]` 被屏蔽则 `y[i,j]` 也屏蔽（配对剔除，pairwise）。

- **`corrcoef`**：在 `cov` 的基础上，用对角线的标准差归一化：\(\rho_{ij}=\mathrm{cov}_{ij}/(\sigma_i\sigma_j)\)。

#### 4.4.2 核心流程

**`average` 的流程**（有权重时）：

```
1. wgt = asarray(weights)
2. 若有掩码：wgt = wgt * (~a.mask)   # 屏蔽点权重清零
3. scl = wgt.sum(axis)               # 分母：有效权重和
4. avg = (a * wgt).sum(axis) / scl   # 分子：加权和
```

无权重时直接 `avg = a.mean(axis); scl = a.count(axis)`。

**`median` 的流程**（委托给 `_median`）：

```
1. asorted = sort(a, fill_value=inf)   # 屏蔽位排末尾
2. counts = count(asorted, axis)       # 每条轴的非屏蔽数
3. h = counts // 2 ; l = where(odd, h, h-1)
4. low_high = take_along_axis(asorted, [l, h])   # 取中间一个或两个
5. s = low_high 沿轴求和 / 2（偶数）或直接取（奇数）
```

**`cov` 的流程**（由 `_covhelper` 预处理后）：

```
1. _covhelper：x 中心化 x -= x.mean(...)；x 与 y 合并公共掩码
2. xnotmask = ~mask（每对观测是否都有效）
3. fact = xnotmask · xnotmaskᵀ - ddof      # 有效配对数 - ddof
4. data = filled(x,0) · filled(x,0)ᵀ / fact
5. result = array(data, mask=(fact<=0))
```

#### 4.4.3 源码精读

`average` 中「屏蔽点权重清零」是理解它的钥匙：

[extras.py:536-701](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L536-L701) —— 无权重分支 `avg = a.mean(axis)`（[extras.py:661-663](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L661-L663)）；有权重分支 `wgt = wgt * (~a.mask)`（[extras.py:688-694](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L688-L694)），再 `(a*wgt).sum / wgt.sum`。注意它还做了权重形状与广播的校验。

`median` 通过 `_ureduce`（从 `numpy.lib._function_base_impl` 导入）归约轴，核心在 `_median`：

[extras.py:704-780](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L704-L780) —— 无掩码输入直接走 `np.median`；有掩码则委托 `_median`。`_median`（[extras.py:783-830](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L783-L830)）先把数组按 `fill_value=inf` 排序（屏蔽位沉底），再用 `count` 取真实中点。一维分支（[extras.py:813-830](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L813-L830)）用 `divmod(count(asorted), 2)` 算中间下标，直观清晰。

`cov` 与它的预处理 `_covhelper`：

[extras.py:1548-1604](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1548-L1604) —— `_covhelper` 做三件事：把输入转成 2 维 float、合并 `x`/`y` 的公共掩码（`common_mask = logical_or(xmask, ymask)`，[extras.py:1584-1591](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1584-L1591)）、中心化 `x -= x.mean(...)`。注意它按维度大小选 `float32`/`float64` 存计数矩阵（[extras.py:1574-1578](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1574-L1578)），大数组才用 float64 保证精度。

[extras.py:1607-1699](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1607-L1699) —— `cov` 用 `fact = dot(xnotmask, xnotmask.T) - ddof` 算有效配对数，`data = dot(filled(x,0), filled(x,0).T) / fact`，并用 `errstate` 抑制除零警告、把 `fact<=0` 的位置屏蔽。

`corrcoef` 在 `cov` 之上做对角归一化：

[extras.py:1702-1757](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1702-L1757) —— `std = sqrt(diagonal(cov))`，再 `corr /= multiply.outer(std, std)`。对角线含屏蔽（方差无法估计）时返回 `MaskedConstant()`。

#### 4.4.4 代码实践

**实践目标**：对比 `.mean()` 与 `ma.average(weights=...)` 在带权情形下的差异，并用 `median` 体会「只对有效数据取中位数」。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

a = ma.array([1.0, 2.0, 3.0, 4.0], mask=[0, 0, 1, 1])   # 只有前两个有效
print("mean         :", a.mean())                       # 1.5  = (1+2)/2
print("average 等权 :", ma.average(a))                  # 1.5（与 mean 一致）
print("average 带权 :", ma.average(a, weights=[3, 1, 0, 0]))  # 1.25 = (1*3+2*1)/(3+1)

# median：屏蔽位不参与
m = ma.array(np.arange(8.0), mask=[0]*4 + [1]*4)        # 有效值 0,1,2,3
print("median       :", ma.median(m))                   # 1.5 = (1+2)/2

# cov / corrcoef：两变量带缺失
x = ma.array([[0.0, 1.0, 2.0, 3.0],
              [0.0, 2.0, 4.0, 6.0]])                    # 第二行是第一行的 2 倍
print("corrcoef     :\n", ma.corrcoef(x))               # 非对角接近 1（完全正相关）
```

**需要观察的现象**：

1. `a.mean()` 与等权 `ma.average(a)` 都是 `1.5`，证明无权重时 `average` 退化为 `mean`（见 [extras.py:661-663](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L661-L663)）。
2. 带权 `ma.average(a, weights=[3,1,0,0])` 是 `1.25`：被屏蔽的 `3.0`、`4.0` 即便给了 0 权重也不影响，因为 `wgt = wgt * (~a.mask)` 已把它们的权重清零；有效部分 \((1\times3 + 2\times1)/(3+1) = 1.25\)。
3. `median(m)` 是 `1.5`：有效值 `[0,1,2,3]` 的中位数，屏蔽的 `[4,5,6,7]` 完全不参与。
4. `corrcoef` 非对角元接近 `1.0`（完全线性正相关），对角线为 `1.0`。

**预期结果**：上述数值。`corrcoef` 因浮点可能显示 `0.999...` 或 `1.0`。

#### 4.4.5 小练习与答案

**练习 1**：`ma.average(a, weights=w)` 中，如果 `a` 的某元素被屏蔽但 `w` 对应位置非零，结果会受影响吗？

**答案**：不会。因为源码执行了 `wgt = wgt * (~a.mask)`，被屏蔽位置的权重被强制清零（[extras.py:688-689](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L688-L689)），所以它在分子（加权和）和分母（权重和）里都不贡献。

**练习 2**：`_covhelper` 为什么要在 `x` 和 `y` 之间建立 `common_mask`？

**答案**：协方差衡量的是「配对观测」的协同变化。若 `x[i]` 缺失而 `y[i]` 有效，这一对无法用于估计协方差，所以必须把缺失**逐对传播**：`x[i]` 屏蔽则 `y[i]` 也屏蔽（反之亦然）。`common_mask = logical_or(xmask, ymask)` 实现了这种配对剔除（[extras.py:1584-1591](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1584-L1591)）。

---

## 5. 综合实践

把本讲四个模块串起来，模拟一个「带缺失值的学生成绩分析」小任务。

```python
import numpy as np
import numpy.ma as ma

# 4 名学生、3 门课，部分成绩缺失
scores = ma.array([[90, 85, 78],
                   [70, ma.masked, 88],
                   [ma.masked, 92, 65],
                   [80, 70, ma.masked]])

# 任务 1：每人有效平均分（.mean 自动跳过缺失）
print("每人均分 :", scores.mean(axis=1))

# 任务 2：用 ma.average 按 [0.3, 0.3, 0.4] 的权重算加权平均（缺失课权重清零）
w = np.array([0.3, 0.3, 0.4])
print("加权均分 :", ma.average(scores, axis=1, weights=w))

# 任务 3：每门课的最高分与取得最高分的学生下标（argmax）
print("科目最高 :", scores.max(axis=0))
print("最高分学生 :", scores.argmax(axis=0))

# 任务 4：把第 0 个学生的成绩排序，观察缺失值的落点
s = scores[0].copy()
s.sort()                       # 默认 endwith=True，缺失在末尾
print("排序(末尾):", s)
s2 = scores[0].copy()
s2.sort(endwith=False)         # 缺失在开头
print("排序(开头):", s2)

# 任务 5：把三门课当作三个变量，算它们之间的相关系数
print("科目相关矩阵:\n", ma.corrcoef(scores.T))
```

**需要观察与思考**：

1. 任务 1 与任务 2 的每人均分为何不同？加权平均是否合理地跳过了缺失课目？
2. 任务 3 的 `argmax` 在某门课有缺失时，返回的下标是否指向真实最高分的学生？结合 [core.py:5776-5780](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5776-L5780) 解释（缺失位被填成最小值，不会赢得 argmax）。
3. 任务 4 中两次排序的差异，验证 `endwith` 的作用。
4. 任务 5 的相关矩阵对角线是否为 1？非对角元的符号与三门课分数的同向/反向关系是否吻合？

> 提示：若某门课有效数据太少或全缺失，`corrcoef` 对应行列会被屏蔽（显示 `--`），这正是 `cov` 中 `fact <= 0` 触发屏蔽的体现。

## 6. 本讲小结

- 掩码归约的通用范式是「**填充 + 普通 ndarray 运算 + 重算掩码**」：`sum`/`prod` 填幺元 0/1，`min`/`max` 填极端值，结果掩码一律来自 `_check_mask_axis`（即 `mask.all(axis)`，全屏蔽才屏蔽）。
- `mean` 是特例：它用 `sum / count`，分母是真实非屏蔽计数 `count`，不能用元素总数，否则会把均值算小。
- 累积归约 `cumsum`/`cumprod` 与点归约相反：它**直接复用原始掩码**（`__setmask__(self._mask)`），被屏蔽位置在结果里依旧屏蔽。
- 极值归约借助 u2-l3 的 `minimum_fill_value`（dtype 最大值，给 `min` 用）与 `maximum_fill_value`（dtype 最小值，给 `max` 用），让被屏蔽元素注定「输掉」；`argmin`/`argmax` 同理，但返回无掩码的下标数组。
- 排序用 `endwith` 决定屏蔽元素落点：填 `nan`/最大值排末尾（`endwith=True`），填最小值排开头（`endwith=False`）；显式 `fill_value` 覆盖 `endwith`。`sort` 通过 `argsort + take_along_axis` 让 data 与 mask 同步重排，屏蔽标记得以保留。
- extras 的 `average`/`median`/`cov`/`corrcoef` 都遵循「屏蔽元素不参与」，但细节各异：`average` 把屏蔽点权重清零、`median` 按 `count` 取真实中点、`cov`/`corrcoef` 对 `x`/`y` 做配对掩码合并。

## 7. 下一步学习建议

- 本讲的归约/排序都建立在 u2-l2 的 `__array_wrap__` 与 u2-l3 的填充值系统之上，如果你对「为什么 `np.sum(掩码数组)` 与 `ma.sum(掩码数组)` 行为不同」仍有疑惑，建议回看 u2-l4/u2-l5 关于掩码 ufunc 两条调用路径的讨论。
- 下一讲 u2-l8 将进入 `extras` 的其余实用工具（`apply_along_axis`、集合运算、`clump_masked` 等），本讲的 `average`/`median`/`cov` 只是 `extras` 的统计子集。
- 想深入「硬掩码/软掩码」如何影响归约与赋值，可提前阅读 u3-l1；想理解归约结果如何在不同 `MaskedArray` 子类间传播类型，可结合 u3-l2 的 `subok` 机制。
- 建议阅读源码顺序：先重读 [core.py:1873-1878](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1873-L1878) 的 `_check_mask_axis`，再对照 `sum`/`min`/`sort` 三个代表性实现，体会「同一范式、不同填充值」的设计美感。
